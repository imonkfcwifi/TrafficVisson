import csv
import hashlib
import json
import shutil
import sqlite3
import subprocess
import threading
import time
import zipfile
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.candidate_extraction import (
    CandidateExtractionCancelled,
    ExtractionLimits,
)
from app.artifact_export import (
    MAX_EVIDENCE_INCIDENTS,
    MAX_EVIDENCE_TOTAL_BYTES,
)
from app.candidate_labels import CandidateLabelStore
from app.main import create_app
from app.store import DEMO_PROJECT_ID
from app.vision_inference import (
    MODEL_PACK_SCHEMA,
    WORKER_SCHEMA,
    LocalVisionInferenceService,
)
from conftest import TEST_TOKEN


def _tools():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required for analysis integration")
    return ffmpeg, ffprobe


def _make_video(tmp_path):
    ffmpeg, ffprobe = _tools()
    source_dir = tmp_path / "trusted source"
    source_dir.mkdir()
    source = source_dir / "tiny.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10:duration=2",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(source),
        ],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip("the installed ffmpeg cannot generate the tiny fixture")
    return source, ffmpeg, ffprobe


def _make_gpx(tmp_path):
    source_dir = tmp_path / "trusted gpx"
    source_dir.mkdir(exist_ok=True)
    source = source_dir / "route.gpx"
    source.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="TrafficSuperVision export test"
  xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>내보내기 검증</name><trkseg>
    <trkpt lat="37.500000" lon="127.000000">
      <ele>10.5</ele><time>2026-07-29T00:00:00Z</time>
    </trkpt>
    <trkpt lat="37.500100" lon="127.000100">
      <ele>11.0</ele><time>2026-07-29T00:00:01Z</time>
    </trkpt>
    <trkpt lat="37.500200" lon="127.000200">
      <ele>11.5</ele><time>2026-07-29T00:00:02Z</time>
    </trkpt>
  </trkseg></trk>
</gpx>
""",
        encoding="utf-8",
    )
    return source


def _limits():
    return ExtractionLimits(
        cadence_seconds=1.0,
        scene_threshold=0.2,
        group_gap_seconds=0.5,
        preview_duration_seconds=2.0,
        max_sample_frames=3,
        max_candidate_groups=3,
        max_frame_width=160,
        max_frame_bytes=2 * 1024 * 1024,
        max_clip_bytes=8 * 1024 * 1024,
        max_output_bytes=10 * 1024 * 1024,
        min_free_disk_bytes=0,
        max_probe_output_bytes=64 * 1024,
        max_scan_output_bytes=256 * 1024,
        probe_timeout_seconds=10,
        scan_timeout_seconds=30,
        artifact_timeout_seconds=30,
        max_total_seconds=60,
    )


def _client(tmp_path, ffmpeg, ffprobe, vision_inference_service=None):
    return TestClient(
        create_app(
            TEST_TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            bridge_data_root=str(tmp_path / "bridge data"),
            candidate_extraction_limits=_limits(),
            vision_inference_service=vision_inference_service,
        )
    )


def _production_client(
    tmp_path, ffmpeg, ffprobe, vision_inference_service=None
):
    return TestClient(
        create_app(
            TEST_TOKEN,
            fixture_mode=False,
            allow_test_client=True,
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            bridge_data_root=str(tmp_path / "p"),
            candidate_extraction_limits=_limits(),
            vision_inference_service=vision_inference_service,
        )
    )


def _auth():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def _mock_road_model_service(tmp_path):
    pack_root = tmp_path / "road model pack"
    pack_root.mkdir()
    model = pack_root / "road-common.onnx"
    model.write_bytes(b"fixture model parsed only by the injected runner")
    model_sha256 = hashlib.sha256(model.read_bytes()).hexdigest()
    manifest = {
        "schema_version": MODEL_PACK_SCHEMA,
        "pack_id": "road-common-integration",
        "pack_version": "0.1.0",
        "engine": "onnx_yolo_v8",
        "license": {
            "runtime_spdx": "MIT",
            "weights_spdx": "Apache-2.0",
            "commercial_use_confirmed": True,
            "redistribution_allowed": False,
            "notice": "Test fixture only.",
        },
        "artifacts": [
            {
                "path": model.name,
                "size_bytes": model.stat().st_size,
                "sha256": model_sha256,
            }
        ],
        "model": {
            "file": model.name,
            "input_width": 640,
            "input_height": 640,
            "labels": [
                {
                    "class_id": 0,
                    "issue_type": "road_debris",
                    "display_name": "낙하물",
                }
            ],
            "confidence_threshold": 0.35,
            "nms_iou_threshold": 0.5,
            "max_detections": 8,
        },
    }
    manifest_path = pack_root / "trafficsupervision-model-pack.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    def runner(pack, candidates, _timeout_seconds, _cancel):
        return {
            "schema_version": WORKER_SCHEMA,
            "pack_id": pack.pack_id,
            "pack_version": pack.pack_version,
            "manifest_sha256": pack.manifest_sha256,
            "results": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "predictions": [
                        {
                            "issue_type": "road_debris",
                            "confidence": 0.88,
                            "box": {
                                "x": 0.1,
                                "y": 0.2,
                                "width": 0.3,
                                "height": 0.4,
                            },
                            "source_label": "낙하물",
                            "calibration": "model_score",
                        }
                    ],
                    "ocr_observations": [],
                    "duration_ms": 3,
                }
                for candidate in candidates
            ],
        }

    return LocalVisionInferenceService(
        model_pack_path=manifest_path,
        timeout_seconds=30,
        runner=runner,
    )


def _stable_snapshot(client, directory, project_id=DEMO_PROJECT_ID):
    snapshot_ids = []
    for _ in range(3):
        response = client.post(
            f"/projects/{project_id}/media/snapshots",
            headers=_auth(),
            json={"project_id": str(project_id), "directory": str(directory)},
        )
        assert response.status_code == 201, response.text
        snapshot_ids.append(response.json()["snapshot_id"])
    stable = client.post(
        f"/projects/{project_id}/media/stability",
        headers=_auth(),
        json={
            "project_id": str(project_id),
            "snapshot_ids": snapshot_ids,
        },
    )
    assert stable.status_code == 200, stable.text
    assert stable.json()["stable"] is True
    return stable.json()["stable_snapshot_id"]


def _local_file_snapshot(client, source, project_id=DEMO_PROJECT_ID):
    response = client.post(
        f"/projects/{project_id}/media/local-file",
        headers=_auth(),
        json={
            "project_id": str(project_id),
            "selected_path": str(source),
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["file_count"] == 1
    assert response.json()["files"][0]["name"] == source.name
    assert str(source.parent) not in response.text
    return response.json()["snapshot_id"]


def _completed_hash(client, snapshot_id, project_id=DEMO_PROJECT_ID):
    started = client.post(
        f"/projects/{project_id}/media/hash-jobs",
        headers=_auth(),
        json={
            "project_id": str(project_id),
            "snapshot_id": snapshot_id,
            "file_name": "tiny.mp4",
        },
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = client.get(
            f"/projects/{project_id}/media/hash-jobs/{job_id}",
            headers=_auth(),
        )
        assert current.status_code == 200, current.text
        if current.json()["status"] == "completed":
            return current.json()
        assert current.json()["status"] not in {"failed", "cancelled"}
        time.sleep(0.01)
    raise AssertionError("hash job did not complete")


def _wait_analysis(client, project_id, job_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = client.get(
            f"/projects/{project_id}/analysis/jobs/{job_id}",
            headers=_auth(),
        )
        assert current.status_code == 200, current.text
        if current.json()["status"] in {"completed", "failed", "cancelled"}:
            return current.json()
        time.sleep(0.02)
    raise AssertionError("analysis job did not reach a terminal state")


def _start_payload(snapshot_id, hash_job):
    return {
        "project_id": str(DEMO_PROJECT_ID),
        "snapshot_id": snapshot_id,
        "hash_job_id": hash_job["job_id"],
        "file_name": "tiny.mp4",
        "sha256_lower": hash_job["sha256_lower"],
    }


def _complete_analysis(client, source):
    snapshot_id = _stable_snapshot(client, source.parent)
    completed_hash = _completed_hash(client, snapshot_id)
    started = client.post(
        f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
        headers=_auth(),
        json=_start_payload(snapshot_id, completed_hash),
    )
    assert started.status_code == 202, started.text
    terminal = _wait_analysis(
        client, DEMO_PROJECT_ID, started.json()["job_id"]
    )
    assert terminal["status"] == "completed", terminal
    assert terminal["result"]["candidate_count"] >= 1
    return terminal


def _complete_analysis_for_project(client, source, project_id):
    snapshot_id = _stable_snapshot(
        client, source.parent, project_id=project_id
    )
    completed_hash = _completed_hash(
        client, snapshot_id, project_id=project_id
    )
    started = client.post(
        f"/projects/{project_id}/analysis/jobs",
        headers=_auth(),
        json={
            "project_id": str(project_id),
            "snapshot_id": snapshot_id,
            "hash_job_id": completed_hash["job_id"],
            "file_name": source.name,
            "sha256_lower": completed_hash["sha256_lower"],
        },
    )
    assert started.status_code == 202, started.text
    terminal = _wait_analysis(
        client, project_id, started.json()["job_id"]
    )
    assert terminal["status"] == "completed", terminal
    assert terminal["result"]["candidate_count"] >= 1
    return terminal


def _materialize_verified_survey_incident_for_export_test(client, job_id):
    """Emulate the already-verified survey projection boundary.

    Ordinary answer API calls are training Annotation-only. The evidence
    integrity tests below intentionally exercise the later export boundary, so
    they opt into the catalog's explicit Incident materialization seam after
    review, matching the durable PipelineJob projection used by the promoted
    model survey contract tests.
    """

    analysis_job_id = UUID(str(job_id))
    audit = client.app.state.candidate_labels.audit_records(
        DEMO_PROJECT_ID,
        analysis_job_id=analysis_job_id,
    )
    assert audit
    client.app.state.store.project_label_records(
        DEMO_PROJECT_ID,
        audit,
        materialize_analysis_job_ids=[analysis_job_id],
        incident_analysis_job_ids=[analysis_job_id],
    )
    incidents = client.app.state.store.list_incidents(
        DEMO_PROJECT_ID,
        analysis_job_id=analysis_job_id,
    )
    assert len(incidents) == 1
    return incidents[0]


def _electron_candidate_answer_payload(renderer_payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the Electron contract integration")
    desktop_root = Path(__file__).resolve().parents[2]
    contract_uri = (desktop_root / "electron" / "contracts.mjs").as_uri()
    script = (
        f'import {{ IPC_CHANNELS, toBridgePayload }} from '
        f'{json.dumps(contract_uri)};'
        "const rendererPayload = JSON.parse(process.argv[1]);"
        "process.stdout.write(JSON.stringify("
        "toBridgePayload(IPC_CHANNELS.CANDIDATE_ANSWER, rendererPayload)"
        "));"
    )
    completed = subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            script,
            json.dumps(renderer_payload, ensure_ascii=False),
        ],
        cwd=desktop_root,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_electron_candidate_answer_contract_reaches_fastapi_and_persists(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        created = client.post(
            "/projects",
            headers=_auth(),
            json={
                "name": "임시 Electron 계약 검증",
                "description": "실제 프로젝트와 분리된 일회성 통합 테스트",
            },
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        terminal = _complete_analysis_for_project(
            client, source, project_id
        )
        job_id = terminal["job_id"]
        candidate_id = terminal["result"]["candidates"][0]["candidate_id"]

        bridge_payload = _electron_candidate_answer_payload(
            {
                "projectId": project_id,
                "jobId": job_id,
                "candidateId": candidate_id,
                "answer": "yes",
                # The renderer's internal OTHER code crosses its API boundary
                # as the canonical lower-case value.
                "selectedIssueType": "other",
                "severity": "major",
                "maintenanceAction": "기타 도로 위험요소 정비",
                "normalizedBox": {
                    "x": 0.125,
                    "y": 0.25,
                    "width": 0.5,
                    "height": 0.375,
                },
            }
        )
        assert bridge_payload == {
            "project_id": project_id,
            "job_id": job_id,
            "candidate_id": candidate_id,
            "answer": "yes",
            "selected_issue_type": "other",
            "severity": "major",
            "maintenance_action": "기타 도로 위험요소 정비",
            "normalized_box": {
                "x": 0.125,
                "y": 0.25,
                "width": 0.5,
                "height": 0.375,
            },
        }

        answer_url = (
            f"/projects/{project_id}/analysis/jobs/{job_id}"
            f"/candidates/{candidate_id}/answer"
        )
        response = client.post(
            answer_url,
            headers=_auth(),
            json={
                "project_id": bridge_payload["project_id"],
                "analysis_job_id": bridge_payload["job_id"],
                "answer": bridge_payload["answer"],
                "selected_issue_type": bridge_payload[
                    "selected_issue_type"
                ],
                "severity": bridge_payload["severity"],
                "maintenance_action": bridge_payload[
                    "maintenance_action"
                ],
                "normalized_box": bridge_payload["normalized_box"],
            },
        )
        assert response.status_code == 201, response.text

        audit = client.app.state.candidate_labels.audit_records(
            project_id, analysis_job_id=UUID(job_id)
        )
        assert len(audit) == 1
        assert audit[0]["final_answer"] == {
            "answer": "yes",
            "selected_issue_type": "other",
            "severity": "major",
            "maintenance_action": "기타 도로 위험요소 정비",
            "ocr_text": None,
            "normalized_box": {
                "x": 0.125,
                "y": 0.25,
                "width": 0.5,
                "height": 0.375,
            },
            "include_in_training": True,
            "answered_by": "local_user",
        }

        reloaded = CandidateLabelStore(
            client.app.state.bridge_data_root,
            lambda *_args: None,
        )
        persisted = reloaded.latest_answers(
            project_id, analysis_job_id=UUID(job_id)
        )
        assert len(persisted) == 1
        assert persisted[0]["record_hash"] == response.json()["record_hash"]
        assert persisted[0]["final_answer"]["selected_issue_type"] == "other"
        assert persisted[0]["final_answer"]["severity"] == "major"
        assert persisted[0]["final_answer"]["normalized_box"] == {
            "x": 0.125,
            "y": 0.25,
            "width": 0.5,
            "height": 0.375,
        }


def test_analysis_rejects_missing_or_mismatched_completed_hash(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _client(tmp_path, ffmpeg, ffprobe) as client:
        snapshot_id = _stable_snapshot(client, source.parent)
        payload = {
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": snapshot_id,
            "hash_job_id": str(uuid4()),
            "file_name": source.name,
            "sha256_lower": hashlib.sha256(source.read_bytes()).hexdigest(),
        }
        rejected = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=payload,
        )
        assert rejected.status_code == 404

        completed_hash = _completed_hash(client, snapshot_id)
        payload = _start_payload(snapshot_id, completed_hash)
        payload["sha256_lower"] = "0" * 64
        mismatch = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=payload,
        )
        assert mismatch.status_code == 409

        # A path cannot be smuggled into the strict start contract.
        payload = _start_payload(snapshot_id, completed_hash)
        payload["media_path"] = str(source)
        path_rejected = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=payload,
        )
        assert path_rejected.status_code == 422


def test_native_picker_registered_file_can_drive_real_analysis(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _client(tmp_path, ffmpeg, ffprobe) as client:
        snapshot_id = _local_file_snapshot(client, source)
        completed_hash = _completed_hash(client, snapshot_id)
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=_start_payload(snapshot_id, completed_hash),
        )
        assert started.status_code == 202, started.text

        terminal = _wait_analysis(
            client, DEMO_PROJECT_ID, started.json()["job_id"]
        )
        assert terminal["status"] == "completed", terminal
        assert terminal["result"]["candidate_count"] >= 1
        assert terminal["result"]["provenance"]["source_name"] == source.name
        assert str(source.parent) not in json.dumps(
            terminal, ensure_ascii=False
        )


def test_analysis_job_attaches_strict_local_model_predictions(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    vision_service = _mock_road_model_service(tmp_path)
    with _client(
        tmp_path,
        ffmpeg,
        ffprobe,
        vision_inference_service=vision_service,
    ) as client:
        snapshot_id = _stable_snapshot(client, source.parent)
        completed_hash = _completed_hash(client, snapshot_id)
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=_start_payload(snapshot_id, completed_hash),
        )
        assert started.status_code == 202, started.text
        terminal = _wait_analysis(
            client, DEMO_PROJECT_ID, started.json()["job_id"]
        )

    assert terminal["status"] == "completed", terminal
    result = terminal["result"]
    assert result["vision_inference"]["mode"] == "local_model"
    assert result["vision_inference"]["engine"] == "onnx_yolo_v8"
    assert result["vision_inference"]["status_counts"] == {
        "classified": result["candidate_count"]
    }
    assert all(
        candidate["ai_inference"]["mode"] == "local_model"
        and candidate["ai_inference"]["predictions"][0]["issue_type"]
        == "road_debris"
        and candidate["ai_inference"]["predictions"][0]["confidence"]
        == 0.88
        for candidate in result["candidates"]
    )
    assert "road model pack" not in str(result)


def test_tiny_video_analysis_returns_real_artifact_metadata_and_provenance(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    data_root = tmp_path / "bridge data"
    with _client(tmp_path, ffmpeg, ffprobe) as client:
        snapshot_id = _stable_snapshot(client, source.parent)
        completed_hash = _completed_hash(client, snapshot_id)
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=_start_payload(snapshot_id, completed_hash),
        )
        assert started.status_code == 202, started.text
        assert started.json()["result"] is None
        assert started.json()["progress_percent"] == 0

        terminal = _wait_analysis(
            client, DEMO_PROJECT_ID, started.json()["job_id"]
        )
        assert terminal["status"] == "completed", terminal
        assert terminal["progress_percent"] == 100
        result = terminal["result"]
        live_result = client.app.state.analysis_jobs.get(
            DEMO_PROJECT_ID,
            UUID(started.json()["job_id"]),
        )["result"]
        assert isinstance(live_result["artifact_set_id"], str)
        assert live_result["artifact_set_id"] == started.json()["job_id"]
        assert result["candidate_count"] >= 1
        assert result["artifact_count"] == result["candidate_count"] * 2
        assert result["total_artifact_bytes"] > 0
        assert result["provenance"]["source_name"] == source.name
        assert (
            result["provenance"]["source_sha256"]
            == completed_hash["sha256_lower"]
        )
        assert result["vision_inference"] == {
            "schema_version": "trafficvisson.vision-inference.v1",
            "mode": "scene_fallback",
            "engine": "none",
            "pack_id": None,
            "pack_version": None,
            "manifest_sha256": None,
            "capabilities": [],
            "runtime_spdx": None,
            "weights_spdx": None,
            "redistribution_allowed": None,
            "candidate_count": result["candidate_count"],
            "status_counts": {
                "provider_unavailable": result["candidate_count"]
            },
            "warning_code": "model_pack_not_configured",
        }
        assert all(
            candidate["ai_inference"]["mode"] == "scene_fallback"
            and candidate["ai_inference"]["predictions"] == []
            and candidate["ai_inference"]["warning_code"]
            == "model_pack_not_configured"
            for candidate in result["candidates"]
        )
        assert "output_directory" not in result
        assert str(source.parent) not in str(result)
        output_root = (
            data_root
            / "projects"
            / str(DEMO_PROJECT_ID)
            / "candidate-extractions"
        )
        run_directories = list(output_root.glob("candidate-extraction-*"))
        assert len(run_directories) == 1
        artifact_names = {
            candidate["representative_image"]["relative_path"]
            for candidate in result["candidates"]
        } | {
            candidate["preview_clip"]["relative_path"]
            for candidate in result["candidates"]
        }
        assert artifact_names == {
            item.name
            for item in run_directories[0].iterdir()
            if item.is_file()
        }
        candidate = result["candidates"][0]
        candidate_id = candidate["candidate_id"]
        evidence_base = (
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/"
            f"{started.json()['job_id']}/candidates/{candidate_id}"
        )
        image = client.get(f"{evidence_base}/image", headers=_auth())
        assert image.status_code == 200, image.text
        assert image.headers["content-type"] == "image/jpeg"
        assert hashlib.sha256(image.content).hexdigest() == (
            candidate["representative_image"]["sha256"]
        )
        assert str(tmp_path) not in str(dict(image.headers))

        ranged_clip = client.get(
            f"{evidence_base}/clip",
            headers={**_auth(), "Range": "bytes=0-31"},
        )
        assert ranged_clip.status_code == 206, ranged_clip.text
        assert len(ranged_clip.content) == 32
        assert ranged_clip.headers["content-range"].startswith("bytes 0-31/")
        assert ranged_clip.headers["content-type"] == "video/mp4"

        unauthenticated = client.get(f"{evidence_base}/image")
        assert unauthenticated.status_code == 401
        missing_candidate = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/"
                f"{started.json()['job_id']}/candidates/"
                "00000000000000000000/image"
            ),
            headers=_auth(),
        )
        assert missing_candidate.status_code == 404

        image_path = (
            run_directories[0]
            / candidate["representative_image"]["relative_path"]
        )
        image_path.write_bytes(image_path.read_bytes() + b"tampered")
        tampered = client.get(f"{evidence_base}/image", headers=_auth())
        assert tampered.status_code == 410


def test_completed_analysis_drives_verified_review_only_dense_index(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _client(tmp_path, ffmpeg, ffprobe) as client:
        analysis = _complete_analysis(client, source)
        original_resolve = (
            client.app.state.media_intake.resolve_completed_media
        )
        resolver_calls = []

        def tracked_resolve(*args, **kwargs):
            resolver_calls.append((args, kwargs))
            return original_resolve(*args, **kwargs)

        client.app.state.media_intake.resolve_completed_media = (
            tracked_resolve
        )
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": analysis["job_id"],
                "sampling_fps": 2,
                "confirm_review_only": True,
            },
        )
        assert started.status_code == 202, started.text
        assert started.json()["analysis_job_id"] == analysis["job_id"]
        assert started.json()["safety"] == {
            "review_only": True,
            "automatic_incident_creation": False,
            "automatic_annotation_creation": False,
            "automatic_training_ingest": False,
            "automatic_maintenance_decision": False,
            "automatic_model_promotion": False,
        }
        assert str(source.parent) not in started.text

        dense_job_id = started.json()["job_id"]
        deadline = time.monotonic() + 30
        terminal = None
        while time.monotonic() < deadline:
            current = client.get(
                (
                    f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs/"
                    f"{dense_job_id}"
                ),
                headers=_auth(),
            )
            assert current.status_code == 200, current.text
            if current.json()["status"] in {
                "completed",
                "failed",
                "cancelled",
            }:
                terminal = current.json()
                break
            time.sleep(0.02)
        assert terminal is not None
        assert terminal["status"] == "completed", terminal
        assert terminal["progress_percent"] == 100
        assert terminal["result"]["frame_count"] >= 2
        assert terminal["result"]["total_frame_bytes"] > 0
        assert len(resolver_calls) == 2
        assert all(
            call[0][0] == DEMO_PROJECT_ID
            and str(call[0][1]) == analysis["snapshot_id"]
            and call[0][2] == source.name
            and str(call[0][3]) == analysis["hash_job_id"]
            and call[0][4] == analysis["source_sha256"]
            for call in resolver_calls
        )

        index = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs/"
                f"{dense_job_id}/artifacts/frame-index"
            ),
            headers=_auth(),
        )
        assert index.status_code == 200, index.text
        assert index.headers["content-type"] == "application/json"
        index_payload = index.json()
        assert index_payload["frame_count"] == (
            terminal["result"]["frame_count"]
        )
        first_frame_id = index_payload["frames"][0]["frame_id"]
        frame = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/dense-index/jobs/"
                f"{dense_job_id}/artifacts/{first_frame_id}"
            ),
            headers=_auth(),
        )
        assert frame.status_code == 200, frame.text
        assert frame.headers["content-type"] == "image/jpeg"
        assert str(tmp_path) not in str(dict(frame.headers))
        assert frame.content.startswith(b"\xff\xd8\xff")


def test_running_analysis_can_be_cancelled_and_pause_is_explicitly_unsupported(
    tmp_path, monkeypatch
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    entered = threading.Event()

    def blocked_extract(self, media_path, output_directory, **kwargs):
        entered.set()
        cancel = kwargs["cancel"]
        assert cancel is not None
        while not cancel():
            time.sleep(0.005)
        raise CandidateExtractionCancelled("cancelled by test")

    monkeypatch.setattr(
        "app.analysis_jobs.CandidateExtractionEngine.extract",
        blocked_extract,
    )
    with _client(tmp_path, ffmpeg, ffprobe) as client:
        snapshot_id = _stable_snapshot(client, source.parent)
        completed_hash = _completed_hash(client, snapshot_id)
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=_start_payload(snapshot_id, completed_hash),
        )
        assert started.status_code == 202
        assert entered.wait(timeout=2)

        unavailable = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/"
                f"{started.json()['job_id']}/candidates/"
                "00000000000000000000/image"
            ),
            headers=_auth(),
        )
        assert unavailable.status_code == 409

        paused = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/"
                f"{started.json()['job_id']}/pause"
            ),
            headers=_auth(),
            json={},
        )
        assert paused.status_code == 409
        assert "not supported" in paused.json()["detail"]

        cancelled = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/"
                f"{started.json()['job_id']}/cancel"
            ),
            headers=_auth(),
            json={},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] in {"cancelling", "cancelled"}
        terminal = _wait_analysis(
            client, DEMO_PROJECT_ID, started.json()["job_id"]
        )
        assert terminal["status"] == "cancelled"
        assert terminal["result"] is None
        assert terminal["error_code"] is None


def test_analysis_job_lookup_is_project_isolated(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _client(tmp_path, ffmpeg, ffprobe) as client:
        snapshot_id = _stable_snapshot(client, source.parent)
        completed_hash = _completed_hash(client, snapshot_id)
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=_start_payload(snapshot_id, completed_hash),
        )
        assert started.status_code == 202
        other = client.post(
            "/projects",
            headers=_auth(),
            json={"name": "격리 확인 프로젝트"},
        )
        assert other.status_code == 201
        other_project_id = other.json()["id"]
        hidden = client.get(
            (
                f"/projects/{other_project_id}/analysis/jobs/"
                f"{started.json()['job_id']}"
            ),
            headers=_auth(),
        )
        assert hidden.status_code == 404
        terminal = _wait_analysis(
            client, DEMO_PROJECT_ID, started.json()["job_id"]
        )
        assert terminal["status"] == "completed"
        candidate_id = terminal["result"]["candidates"][0]["candidate_id"]
        hidden_artifact = client.get(
            (
                f"/projects/{other_project_id}/analysis/jobs/"
                f"{started.json()['job_id']}/candidates/"
                f"{candidate_id}/image"
            ),
            headers=_auth(),
        )
        assert hidden_artifact.status_code == 404


def test_bridge_shutdown_cancels_running_analysis(tmp_path, monkeypatch):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    entered = threading.Event()
    observed_cancel = threading.Event()

    def blocked_extract(self, media_path, output_directory, **kwargs):
        entered.set()
        cancel = kwargs["cancel"]
        while not cancel():
            time.sleep(0.005)
        observed_cancel.set()
        raise CandidateExtractionCancelled("shutdown")

    monkeypatch.setattr(
        "app.analysis_jobs.CandidateExtractionEngine.extract",
        blocked_extract,
    )
    test_client = _client(tmp_path, ffmpeg, ffprobe)
    with test_client as client:
        snapshot_id = _stable_snapshot(client, source.parent)
        completed_hash = _completed_hash(client, snapshot_id)
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=_start_payload(snapshot_id, completed_hash),
        )
        assert started.status_code == 202
        job_id = started.json()["job_id"]
        assert entered.wait(timeout=2)
    assert observed_cancel.wait(timeout=2)
    terminal = test_client.app.state.analysis_jobs.get(
        DEMO_PROJECT_ID, UUID(job_id)
    )
    assert terminal["status"] == "cancelled"


def test_candidate_label_api_isolated_validated_and_hash_chain_persistent(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        candidate = terminal["result"]["candidates"][0]
        candidate_id = candidate["candidate_id"]
        questions_url = (
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
            "/label-questions"
        )
        answer_url = (
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
            f"/candidates/{candidate_id}/answer"
        )

        unanswered = client.get(questions_url, headers=_auth())
        assert unanswered.status_code == 200, unanswered.text
        unanswered_payload = unanswered.json()
        assert unanswered_payload["count"] == terminal["result"][
            "candidate_count"
        ]
        assert unanswered_payload["answered_count"] == 0
        first_question = next(
            question
            for question in unanswered_payload["questions"]
            if question["candidate_id"] == candidate_id
        )
        assert first_question["status"] == "unanswered"
        assert first_question["latest_answer"] is None
        assert first_question["answer_revision"] == 0
        assert first_question["candidate"] == candidate
        assert (
            first_question["candidate"]["ai_inference"]
            == candidate["ai_inference"]
        )
        assert str(source.parent) not in unanswered.text

        identity = {
            "project_id": str(DEMO_PROJECT_ID),
            "analysis_job_id": job_id,
        }
        missing_type = client.post(
            answer_url,
            headers=_auth(),
            json={**identity, "answer": "yes"},
        )
        assert missing_type.status_code == 422
        negative_with_details = client.post(
            answer_url,
            headers=_auth(),
            json={
                **identity,
                "answer": "no",
                "selected_issue_type": "road_debris",
            },
        )
        assert negative_with_details.status_code == 422
        edit_without_box = client.post(
            answer_url,
            headers=_auth(),
            json={
                **identity,
                "answer": "edit",
                "selected_issue_type": "tunnel_identity",
            },
        )
        assert edit_without_box.status_code == 422

        first_answer = client.post(
            answer_url,
            headers=_auth(),
            json={**identity, "answer": "no"},
        )
        assert first_answer.status_code == 201, first_answer.text
        first_record = first_answer.json()
        assert first_record["answer_revision"] == 1
        assert first_record["partition"] == "negative"
        assert first_record["include_in_training"] is True

        final_answer = client.post(
            answer_url,
            headers=_auth(),
            json={
                **identity,
                "answer": "edit",
                "selected_issue_type": "tunnel_identity",
                "severity": "major",
                "maintenance_action": "터널명 표지 주변 삭초",
                "ocr_text": "정안알밤휴게소터널",
                "normalized_box": {
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.4,
                    "height": 0.3,
                },
            },
        )
        assert final_answer.status_code == 201, final_answer.text
        final_record = final_answer.json()
        assert final_record["question_id"] == first_record["question_id"]
        assert final_record["record_id"] != first_record["record_id"]
        assert final_record["answer_revision"] == 2
        assert final_record["partition"] == "approved"
        assert final_record["include_in_training"] is True
        assert final_record["record_hash"] != first_record["record_hash"]

        reviewed = client.get(questions_url, headers=_auth())
        assert reviewed.status_code == 200, reviewed.text
        reviewed_payload = reviewed.json()
        assert reviewed_payload["answered_count"] == 1
        latest_question = next(
            question
            for question in reviewed_payload["questions"]
            if question["candidate_id"] == candidate_id
        )
        assert latest_question["status"] == "approved"
        assert latest_question["answer_revision"] == 2
        assert latest_question["latest_answer"] == {
            "answer": "edit",
            "selected_issue_type": "tunnel_identity",
            "severity": "major",
            "maintenance_action": "터널명 표지 주변 삭초",
            "ocr_text": "정안알밤휴게소터널",
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.4,
                "height": 0.3,
            },
            "include_in_training": True,
            "answered_by": "local_user",
        }

        stats = client.get(
            f"/projects/{DEMO_PROJECT_ID}/labels/stats",
            headers=_auth(),
        )
        assert stats.status_code == 200, stats.text
        assert stats.json() == {
            "project_id": str(DEMO_PROJECT_ID),
            "question_count": 1,
            "answer_revision_count": 2,
            "current_answer_count": 1,
            "training_included_count": 1,
            "source_analysis_job_count": 1,
            "partitions": {
                "approved": 1,
                "negative": 0,
                "held": 0,
                "excluded": 0,
            },
        }
        snapshot = client.post(
            f"/projects/{DEMO_PROJECT_ID}/datasets",
            headers=_auth(),
            json={"user_confirmed": True},
        )
        # A direct legacy AnalysisJob answer remains in the immutable audit
        # chain, but it has not crossed the PipelineJob review-completion
        # boundary.  It must not become a production Annotation or
        # DatasetVersion merely because one candidate was answered.
        assert snapshot.status_code == 409, snapshot.text
        assert snapshot.json()["detail"] == (
            "At least one reviewed label is required"
        )
        versions = client.get(
            f"/projects/{DEMO_PROJECT_ID}/datasets",
            headers=_auth(),
        )
        assert versions.status_code == 200
        assert versions.json()["count"] == 0

        created = client.post(
            "/projects",
            headers=_auth(),
            json={"name": "격리 프로젝트", "description": "후보 라벨 경계"},
        )
        assert created.status_code == 201, created.text
        other_project_id = created.json()["id"]
        other_stats = client.get(
            f"/projects/{other_project_id}/labels/stats",
            headers=_auth(),
        )
        assert other_stats.status_code == 200
        assert other_stats.json()["current_answer_count"] == 0
        assert other_stats.json()["training_included_count"] == 0
        cross_project_get = client.get(
            (
                f"/projects/{other_project_id}/analysis/jobs/{job_id}"
                "/label-questions"
            ),
            headers=_auth(),
        )
        assert cross_project_get.status_code == 404
        cross_project_answer = client.post(
            (
                f"/projects/{other_project_id}/analysis/jobs/{job_id}"
                f"/candidates/{candidate_id}/answer"
            ),
            headers=_auth(),
            json={
                "project_id": other_project_id,
                "analysis_job_id": job_id,
                "answer": "no",
            },
        )
        assert cross_project_answer.status_code == 404
        route_body_mismatch = client.post(
            answer_url,
            headers=_auth(),
            json={
                "project_id": other_project_id,
                "analysis_job_id": job_id,
                "answer": "no",
            },
        )
        assert route_body_mismatch.status_code == 409
        unknown_candidate = client.post(
            answer_url.replace(candidate_id, "0" * 20),
            headers=_auth(),
            json={**identity, "answer": "no"},
        )
        assert unknown_candidate.status_code == 404

        audit = client.app.state.candidate_labels.audit_records(
            DEMO_PROJECT_ID, analysis_job_id=UUID(job_id)
        )
        assert len(audit) == 2
        assert audit[0]["previous_record_hash"] is None
        assert audit[0]["record_hash"] == first_record["record_hash"]
        assert (
            audit[1]["previous_record_hash"] == first_record["record_hash"]
        )
        assert audit[1]["record_hash"] == final_record["record_hash"]

        reloaded = CandidateLabelStore(
            client.app.state.bridge_data_root,
            lambda *_args: None,
        )
        latest_after_reload = reloaded.latest_answers(
            DEMO_PROJECT_ID, analysis_job_id=UUID(job_id)
        )
        assert len(latest_after_reload) == 1
        assert (
            latest_after_reload[0]["record_hash"]
            == final_record["record_hash"]
        )
        assert (
            reloaded.audit_records(
                other_project_id, analysis_job_id=UUID(job_id)
            )
            == []
        )


def test_production_export_preserves_local_model_proposals(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / "model export"
    export_root.mkdir()
    vision_service = _mock_road_model_service(tmp_path)
    with _production_client(
        tmp_path,
        ffmpeg,
        ffprobe,
        vision_inference_service=vision_service,
    ) as client:
        terminal = _complete_analysis(client, source)
        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": terminal["job_id"],
                "artifacts": [
                    "raw_detections",
                    "reviewed_incidents",
                    "label_questions",
                    "annotations",
                    "ocr_observations",
                    "route_points",
                    "maintenance_actions",
                    "report_payload",
                    "manifest",
                ],
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 200, exported.text

    output = next(export_root.glob("trafficsupervision-export-*"))
    with (output / "raw_detections.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source_file:
        detections = list(csv.DictReader(source_file))
    with (output / "label_questions.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source_file:
        questions = list(csv.DictReader(source_file))
    assert len(detections) == terminal["result"]["candidate_count"]
    assert all(row["type"] == "road_debris" for row in detections)
    assert all(
        row["source"] == "local_model:onnx_yolo_v8"
        for row in detections
    )
    assert all(
        row["model_version_id"] == "road-common-integration@0.1.0"
        for row in detections
    )
    assert all(row["score_value"] == row["confidence"] for row in detections)
    assert all(
        row["score_semantics"] == "model_score_not_probability"
        for row in detections
    )
    assert all(row["score_calibration"] == "model_score" for row in detections)
    assert all(
        row["proposal_origin"] == "embedded_analysis_inference"
        for row in detections
    )
    assert all(row["review_only"] == "true" for row in detections)
    assert all(
        row["automatic_labeling_allowed"] == "false"
        for row in detections
    )
    assert all(row["proposed_type"] == "road_debris" for row in questions)
    assert all(
        row["proposed_score_value"] == row["proposed_confidence"]
        for row in questions
    )
    assert all(
        row["proposed_score_semantics"]
        == "model_score_not_probability"
        for row in questions
    )
    assert all(
        row["proposal_origin"] == "embedded_analysis_inference"
        for row in questions
    )
    assert all(row["mandatory"] == "true" for row in questions)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["provenance"]["candidate_method"] == (
        "local_model_plus_scene_candidate"
    )
    assert manifest["provenance"]["vision_inference"]["engine"] == (
        "onnx_yolo_v8"
    )
    assert "road model pack" not in json.dumps(manifest, ensure_ascii=False)


def test_production_export_contains_latest_reviewed_candidate_records(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / "사용자가 선택한 내보내기 폴더"
    export_root.mkdir()
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        candidate = terminal["result"]["candidates"][0]
        candidate_id = candidate["candidate_id"]
        identity = {
            "project_id": str(DEMO_PROJECT_ID),
            "analysis_job_id": job_id,
        }
        answer_url = (
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
            f"/candidates/{candidate_id}/answer"
        )
        superseded = client.post(
            answer_url,
            headers=_auth(),
            json={**identity, "answer": "unsure"},
        )
        assert superseded.status_code == 201, superseded.text
        approved = client.post(
            answer_url,
            headers=_auth(),
            json={
                **identity,
                "answer": "edit",
                "selected_issue_type": "sign_vegetation_occlusion",
                "severity": "urgent",
                "maintenance_action": "이정표 전면 수목 제거",
                "ocr_text": "논산 18km",
                "normalized_box": {
                    "x": 0.15,
                    "y": 0.1,
                    "width": 0.5,
                    "height": 0.6,
                },
            },
        )
        assert approved.status_code == 201, approved.text
        approved_payload = approved.json()

        artifacts = [
            "raw_detections",
            "reviewed_incidents",
            "label_questions",
            "annotations",
            "ocr_observations",
            "route_points",
            "maintenance_actions",
            "report_payload",
            "manifest",
        ]
        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "artifacts": artifacts,
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 200, exported.text
        response_payload = exported.json()
        assert response_payload["status"] == "completed"
        assert response_payload["artifact_count"] == 10
        assert response_payload["evidence_incident_count"] == 0
        assert response_payload["evidence_file_count"] == 0
        assert response_payload["evidence_size_bytes"] == 0
        assert str(export_root) not in exported.text
        assert str(source.parent) not in exported.text

        export_directories = list(
            export_root.glob("trafficsupervision-export-*")
        )
        assert len(export_directories) == 1
        output = export_directories[0]

        def csv_rows(file_name):
            with (output / file_name).open(
                "r", encoding="utf-8-sig", newline=""
            ) as source_file:
                return list(csv.DictReader(source_file))

        detections = csv_rows("raw_detections.csv")
        questions = csv_rows("label_questions.csv")
        annotations = csv_rows("annotations.csv")
        incidents = csv_rows("reviewed_incidents.csv")
        ocr_rows = csv_rows("ocr_observations.csv")
        actions = csv_rows("maintenance_actions.csv")
        route_points = csv_rows("route_points.csv")

        assert len(detections) == terminal["result"]["candidate_count"]
        detection = next(
            row
            for row in detections
            if row["detection_id"] == candidate_id
        )
        assert detection["type"] == "UNCLASSIFIED_SCENE_CANDIDATE"
        assert (
            detection["source"]
            == "scene_change_and_sharpness_non_classifying"
        )
        assert detection["score_value"] == ""
        assert detection["score_semantics"] == ""
        assert detection["score_calibration"] == ""
        assert detection["proposal_origin"] == ""
        assert detection["review_only"] == ""
        assert detection["automatic_labeling_allowed"] == ""
        assert detection["proposal_run_id"] == ""
        assert detection["model_manifest_sha256"] == ""
        assert len(questions) == terminal["result"]["candidate_count"]
        question = next(
            row
            for row in questions
            if row["candidate_incident_id"] == candidate_id
        )
        assert question["question_id"] == approved_payload["question_id"]
        assert question["policy"] == "learning_assist"
        assert question["mandatory"] == "true"
        assert question["proposed_score_value"] == ""
        assert question["proposed_score_semantics"] == ""
        assert question["proposed_score_calibration"] == ""
        assert question["proposal_origin"] == ""
        assert question["review_only"] == ""
        assert question["automatic_labeling_allowed"] == ""
        assert (
            question["evidence_image_ref"]
            == candidate["representative_image"]["artifact_ref"]
        )
        assert (
            question["preview_clip_ref"]
            == candidate["preview_clip"]["artifact_ref"]
        )

        # Only the latest answer revision is part of the point-in-time export.
        assert len(annotations) == 1
        annotation = annotations[0]
        assert annotation["annotation_id"] == approved_payload["record_id"]
        assert annotation["answer"] == "edit"
        assert annotation["type"] == "sign_vegetation_occlusion"
        assert annotation["review_status"] == "approved"
        assert annotation["ocr_text"] == "논산 18km"
        assert annotation["action_label"] == "이정표 전면 수목 제거"
        assert json.loads(annotation["geometry"]) == {
            "height": 0.6,
            "width": 0.5,
            "x": 0.15,
            "y": 0.1,
        }

        # An initial-video local-user answer is training truth only. Its OCR,
        # action, and geometry remain on the Annotation, but it cannot create
        # maintenance Incident/report/evidence truth before an independent
        # promoted-model survey is reviewed.
        assert incidents == []
        assert ocr_rows == []
        assert actions == []
        assert route_points == []

        report_payload = json.loads(
            (output / "report_payload.json").read_text(encoding="utf-8")
        )
        assert report_payload["trip"]["analysis_job_id"] == job_id
        assert report_payload["report"]["reviewed_count"] == 1
        assert report_payload["report"]["approved_incident_count"] == 0
        assert report_payload["report"]["approved_incidents"] == []
        manifest = json.loads(
            (output / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["record_counts"] == {
            "detections": terminal["result"]["candidate_count"],
            "incidents": 0,
            "label_questions": terminal["result"]["candidate_count"],
            "annotations": 1,
            "ocr_observations": 0,
            "route_points": 0,
            "maintenance_actions": 0,
        }
        assert manifest["provenance"]["analysis_job_id"] == job_id
        assert manifest["provenance"]["source_sha256"] == terminal[
            "source_sha256"
        ]
        assert manifest["provenance"]["label_record_hashes"] == [
            superseded.json()["record_hash"],
            approved_payload["record_hash"]
        ]
        assert manifest["provenance"]["label_audit_record_count"] == 2
        assert manifest["provenance"]["label_latest_record_count"] == 1
        assert manifest["provenance"]["gpx_track"] == {
            "schema_version": "trafficvisson.gpx-export-provenance.v1",
            "status": "not_attached",
            "project_id": str(DEMO_PROJECT_ID),
            "trip_id": job_id,
        }
        sidecars = manifest["provenance"]["experimental_sidecars"]
        assert sidecars["selected_proposal_count"] == 0
        assert sidecars["classified_count"] == 0
        assert sidecars["no_suggestion_count"] == 0
        assert sidecars["prediction_count"] == 0
        assert len(sidecars["selection_sha256"]) == 64
        assert sidecars["runs"] == []
        assert sidecars["models"] == []
        assert sidecars["evaluations"] == []
        assert sidecars["datasets"] == []
        assert sidecars["automatic_labeling_allowed"] is False
        assert (
            sidecars[
                "excluded_from_confirmed_outputs_without_user_approval"
            ]
            is True
        )
        assert sidecars["eligible_as_evidence_after_user_approval"] is True
        incident_projection = manifest["provenance"][
            "incident_projection"
        ]
        assert incident_projection["catalog_schema_version"] == 4
        assert (
            incident_projection["selection"]
            == "active_exact_analysis_job"
        )
        assert incident_projection["active_incident_count"] == 0
        assert incident_projection["incidents"] == []
        assert manifest["evidence"] == {
            "file_count": 0,
            "incident_count": 0,
            "members": [],
            "total_size_bytes": 0,
        }
        with zipfile.ZipFile(output / "trafficsupervision-export.zip") as archive:
            assert not any(
                name.startswith("evidence/") for name in archive.namelist()
            )
        assert str(tmp_path) not in json.dumps(
            manifest, ensure_ascii=False
        )

        expected_artifact_rows = {
            "raw_detections.csv": len(detections),
            "reviewed_incidents.csv": len(incidents),
            "label_questions.csv": len(questions),
            "annotations.csv": len(annotations),
            "ocr_observations.csv": len(ocr_rows),
            "route_points.csv": len(route_points),
            "maintenance_actions.csv": len(actions),
            "report_payload.json": 1,
            "manifest.json": 1,
        }
        response_artifacts = {
            item["file_name"]: item
            for item in response_payload["artifacts"]
        }
        assert set(response_artifacts) == set(expected_artifact_rows)
        for file_name, row_count in expected_artifact_rows.items():
            artifact_path = output / file_name
            metadata = response_artifacts[file_name]
            assert metadata["row_count"] == row_count
            assert metadata["size_bytes"] == artifact_path.stat().st_size
            assert metadata["sha256"] == hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest()


def test_production_export_requires_and_packages_completed_real_analysis(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        artifacts = [
            "raw_detections",
            "reviewed_incidents",
            "label_questions",
            "annotations",
            "ocr_observations",
            "route_points",
            "maintenance_actions",
            "report_payload",
            "manifest",
        ]
        before = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "artifacts": artifacts,
            },
        )
        assert before.status_code == 409

        snapshot_id = _stable_snapshot(client, source.parent)
        completed_hash = _completed_hash(client, snapshot_id)
        started = client.post(
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs",
            headers=_auth(),
            json=_start_payload(snapshot_id, completed_hash),
        )
        terminal = _wait_analysis(
            client, DEMO_PROJECT_ID, started.json()["job_id"]
        )
        assert terminal["status"] == "completed", terminal

        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": terminal["job_id"],
                "artifacts": artifacts,
            },
        )
        assert exported.status_code == 200, exported.text
        payload = exported.json()
        assert payload["status"] == "completed"
        assert payload["archive_file_name"] == "trafficsupervision-export.zip"
        assert payload["archive_size_bytes"] > 0
        assert len(payload["archive_sha256"]) == 64
        assert payload["artifact_count"] == 10
        assert str(tmp_path) not in exported.text
        archives = list(
            (tmp_path / "p" / "exports").rglob(
                "trafficsupervision-export.zip"
            )
        )
        assert len(archives) == 1
        assert archives[0].stat().st_size == payload["archive_size_bytes"]


def test_production_export_includes_exact_gpx_track_and_provenance(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    gpx_source = _make_gpx(tmp_path)
    export_root = tmp_path / "gpx export"
    export_root.mkdir()
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        attached = client.post(
            f"/projects/{DEMO_PROJECT_ID}/trips/{job_id}/gpx",
            headers=_auth(),
            json={
                "selected_path": str(gpx_source),
                "video_offset_seconds": 1.25,
            },
        )
        assert attached.status_code == 201, attached.text
        assert attached.json()["revision"] == 1

        shifted = client.post(
            f"/projects/{DEMO_PROJECT_ID}/trips/{job_id}/gpx/offset",
            headers=_auth(),
            json={"video_offset_seconds": -0.25},
        )
        assert shifted.status_code == 200, shifted.text
        track = shifted.json()
        assert track["revision"] == 2

        artifacts = [
            "raw_detections",
            "reviewed_incidents",
            "label_questions",
            "annotations",
            "ocr_observations",
            "route_points",
            "maintenance_actions",
            "report_payload",
            "manifest",
        ]
        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "artifacts": artifacts,
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 200, exported.text
        response_payload = exported.json()
        route_artifact = next(
            item
            for item in response_payload["artifacts"]
            if item["artifact"] == "route_points"
        )
        assert route_artifact["row_count"] == 3

    output_directories = [
        item
        for item in export_root.iterdir()
        if item.is_dir() and item.name.startswith("trafficsupervision-export-")
    ]
    assert len(output_directories) == 1
    output = output_directories[0]
    with (output / "route_points.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as source_file:
        route_points = list(csv.DictReader(source_file))
    assert len(route_points) == 3
    assert [row["trip_id"] for row in route_points] == [job_id] * 3
    assert [row["sequence"] for row in route_points] == ["0", "1", "2"]
    assert [row["timestamp_ms"] for row in route_points] == [
        "-250",
        "750",
        "1750",
    ]
    assert [row["source"] for row in route_points] == ["gpx"] * 3
    assert route_points[0]["latitude"] == "37.5"
    assert route_points[0]["longitude"] == "127.0"
    assert route_points[0]["altitude_m"] == "10.5"
    assert route_points[0]["timestamp_ms"] != "'-250"
    assert len(
        {UUID(row["route_point_id"]) for row in route_points}
    ) == 3

    manifest = json.loads(
        (output / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["record_counts"]["route_points"] == 3
    gpx_provenance = manifest["provenance"]["gpx_track"]
    assert gpx_provenance["status"] == "attached"
    assert gpx_provenance["project_id"] == str(DEMO_PROJECT_ID)
    assert gpx_provenance["trip_id"] == job_id
    assert gpx_provenance["track_id"] == track["track_id"]
    assert gpx_provenance["track_revision"] == 2
    assert gpx_provenance["source_sha256"] == hashlib.sha256(
        gpx_source.read_bytes()
    ).hexdigest()
    assert gpx_provenance["video_offset_seconds"] == -0.25
    assert gpx_provenance["point_count"] == 3
    assert gpx_provenance["exported_point_count"] == 3
    assert gpx_provenance["sampled_for_export"] is False
    assert gpx_provenance["points_before_video_count"] == 1
    assert len(gpx_provenance["track_record_sha256"]) == 64
    assert gpx_provenance["source_integrity_verified"] is True
    assert str(tmp_path) not in json.dumps(manifest, ensure_ascii=False)
    with zipfile.ZipFile(output / "trafficsupervision-export.zip") as archive:
        assert archive.read("route_points.csv") == (
            output / "route_points.csv"
        ).read_bytes()
        assert archive.read("manifest.json") == (
            output / "manifest.json"
        ).read_bytes()


def test_production_export_refuses_tampered_attached_gpx_track(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    gpx_source = _make_gpx(tmp_path)
    export_root = tmp_path / "tampered gpx export"
    export_root.mkdir()
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        attached = client.post(
            f"/projects/{DEMO_PROJECT_ID}/trips/{job_id}/gpx",
            headers=_auth(),
            json={
                "selected_path": str(gpx_source),
                "video_offset_seconds": 0,
            },
        )
        assert attached.status_code == 201, attached.text

        record_path = (
            tmp_path
            / "p"
            / "projects"
            / str(DEMO_PROJECT_ID)
            / "gps-tracks"
            / f"{job_id}.json"
        )
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["video_offset_seconds"] = 123.0
        record_path.write_text(
            json.dumps(record, ensure_ascii=False),
            encoding="utf-8",
        )

        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "artifacts": [
                    "raw_detections",
                    "reviewed_incidents",
                    "label_questions",
                    "annotations",
                    "ocr_observations",
                    "route_points",
                    "maintenance_actions",
                    "report_payload",
                    "manifest",
                ],
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 409, exported.text
        assert "integrity" in exported.json()["detail"].lower()
        assert str(tmp_path) not in exported.text
        assert list(export_root.iterdir()) == []


def test_production_export_uses_the_explicit_selected_analysis_job(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / "selected analysis export"
    export_root.mkdir()
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        selected = _complete_analysis(client, source)
        newer = _complete_analysis(client, source)
        assert selected["job_id"] != newer["job_id"]

        approved = []
        for terminal, issue_type in (
            (selected, "pothole"),
            (newer, "road_debris"),
        ):
            candidate_id = terminal["result"]["candidates"][0][
                "candidate_id"
            ]
            response = client.post(
                (
                    f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/"
                    f"{terminal['job_id']}/candidates/{candidate_id}/answer"
                ),
                headers=_auth(),
                json={
                    "project_id": str(DEMO_PROJECT_ID),
                    "analysis_job_id": terminal["job_id"],
                    "answer": "edit",
                    "selected_issue_type": issue_type,
                    "severity": "major",
                    "maintenance_action": f"{issue_type} 조치",
                    "normalized_box": {
                        "x": 0.1,
                        "y": 0.1,
                        "width": 0.5,
                        "height": 0.5,
                    },
                },
            )
            assert response.status_code == 201, response.text
            approved.append(response.json())

        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": selected["job_id"],
                "artifacts": [
                    "raw_detections",
                    "reviewed_incidents",
                    "label_questions",
                    "annotations",
                    "ocr_observations",
                    "route_points",
                    "maintenance_actions",
                    "report_payload",
                    "manifest",
                ],
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 200, exported.text
        assert exported.json()["evidence_incident_count"] == 0
        assert exported.json()["evidence_file_count"] == 0

    output = next(export_root.glob("trafficsupervision-export-*"))
    manifest = json.loads(
        (output / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["provenance"]["analysis_job_id"] == selected["job_id"]
    assert manifest["provenance"]["analysis_job_id"] != newer["job_id"]
    with (output / "annotations.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        annotations = list(csv.DictReader(handle))
    assert len(annotations) == 1
    assert annotations[0]["annotation_id"] == approved[0]["record_id"]
    assert annotations[0]["annotation_id"] != approved[1]["record_id"]
    with zipfile.ZipFile(output / "trafficsupervision-export.zip") as archive:
        evidence_names = [
            name for name in archive.namelist()
            if name.startswith("evidence/")
        ]
    assert evidence_names == []


def test_production_export_excludes_superseded_incident_evidence(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / "inactive incident export"
    export_root.mkdir()
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        candidate_id = terminal["result"]["candidates"][0]["candidate_id"]
        answer_url = (
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
            f"/candidates/{candidate_id}/answer"
        )
        identity = {
            "project_id": str(DEMO_PROJECT_ID),
            "analysis_job_id": job_id,
        }
        approved = client.post(
            answer_url,
            headers=_auth(),
            json={
                **identity,
                "answer": "edit",
                "selected_issue_type": "road_debris",
                "severity": "urgent",
                "maintenance_action": "즉시 제거",
                "normalized_box": {
                    "x": 0.1,
                    "y": 0.1,
                    "width": 0.5,
                    "height": 0.5,
                },
            },
        )
        assert approved.status_code == 201, approved.text
        superseded = client.post(
            answer_url,
            headers=_auth(),
            json={**identity, "answer": "no"},
        )
        assert superseded.status_code == 201, superseded.text

        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "artifacts": [
                    "raw_detections",
                    "reviewed_incidents",
                    "label_questions",
                    "annotations",
                    "ocr_observations",
                    "route_points",
                    "maintenance_actions",
                    "report_payload",
                    "manifest",
                ],
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 200, exported.text
        payload = exported.json()
        assert payload["evidence_incident_count"] == 0
        assert payload["evidence_file_count"] == 0
        assert payload["evidence_size_bytes"] == 0
        assert payload["artifact_count"] == 10

    output = next(export_root.glob("trafficsupervision-export-*"))
    with (output / "reviewed_incidents.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        assert list(csv.DictReader(handle)) == []
    with zipfile.ZipFile(output / "trafficsupervision-export.zip") as archive:
        assert not any(
            name.startswith("evidence/") for name in archive.namelist()
        )
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    assert manifest["evidence"] == {
        "file_count": 0,
        "incident_count": 0,
        "members": [],
        "total_size_bytes": 0,
    }
    assert manifest["provenance"]["incident_projection"][
        "active_incident_count"
    ] == 0


def test_production_export_rejects_tampered_approved_evidence_without_paths(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / "tampered evidence export"
    export_root.mkdir()
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        candidate = terminal["result"]["candidates"][0]
        candidate_id = candidate["candidate_id"]
        approved = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
                f"/candidates/{candidate_id}/answer"
            ),
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "answer": "edit",
                "selected_issue_type": "pothole",
                "severity": "major",
                "maintenance_action": "노면 보수",
                "normalized_box": {
                    "x": 0.1,
                    "y": 0.1,
                    "width": 0.5,
                    "height": 0.5,
                },
            },
        )
        assert approved.status_code == 201, approved.text
        _materialize_verified_survey_incident_for_export_test(
            client,
            job_id,
        )
        relative_name = candidate["representative_image"]["relative_path"]
        evidence_paths = list((tmp_path / "p").rglob(relative_name))
        assert len(evidence_paths) == 1
        evidence_paths[0].write_bytes(b"\xff\xd8\xfftampered")

        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "artifacts": [
                    "raw_detections",
                    "reviewed_incidents",
                    "label_questions",
                    "annotations",
                    "ocr_observations",
                    "route_points",
                    "maintenance_actions",
                    "report_payload",
                    "manifest",
                ],
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 410, exported.text
        assert str(tmp_path) not in exported.text
        assert "tampered evidence export" not in exported.text
    assert list(export_root.iterdir()) == []


def test_production_export_rejects_tampered_incident_projection(
    tmp_path, monkeypatch
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / "tampered incident export"
    export_root.mkdir()
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        candidate_id = terminal["result"]["candidates"][0]["candidate_id"]
        approved = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
                f"/candidates/{candidate_id}/answer"
            ),
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "answer": "edit",
                "selected_issue_type": "pothole",
                "severity": "major",
                "maintenance_action": "노면 보수",
                "normalized_box": {
                    "x": 0.1,
                    "y": 0.1,
                    "width": 0.5,
                    "height": 0.5,
                },
            },
        )
        assert approved.status_code == 201, approved.text
        _materialize_verified_survey_incident_for_export_test(
            client,
            job_id,
        )

        with sqlite3.connect(
            str(tmp_path / "p" / "catalog.sqlite3")
        ) as connection:
            connection.execute(
                """
                UPDATE incidents
                SET evidence_image_ref = ?
                WHERE project_id = ? AND id = ?
                """,
                (
                    f"analysis:{job_id}:forged.jpg",
                    str(DEMO_PROJECT_ID),
                    approved.json()["question_id"],
                ),
            )
            connection.commit()

        original_list_incidents = client.app.state.store.list_incidents

        def forged_list_incidents(*args, **kwargs):
            incidents = original_list_incidents(*args, **kwargs)
            assert len(incidents) == 1
            incidents[0]["evidence_image_ref"] = (
                f"analysis:{job_id}:forged.jpg"
            )
            return incidents

        monkeypatch.setattr(
            client.app.state.store,
            "list_incidents",
            forged_list_incidents,
        )
        exported = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "artifacts": [
                    "raw_detections",
                    "reviewed_incidents",
                    "label_questions",
                    "annotations",
                    "ocr_observations",
                    "route_points",
                    "maintenance_actions",
                    "report_payload",
                    "manifest",
                ],
                "destination_path": str(export_root),
            },
        )
        assert exported.status_code == 500, exported.text
        assert "failed audit verification" in exported.json()["detail"]
        assert str(tmp_path) not in exported.text
        assert list(export_root.iterdir()) == []


def test_production_export_rejects_evidence_bounds_before_resolving_files(
    tmp_path, monkeypatch
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / "bounded evidence export"
    export_root.mkdir()
    artifacts = [
        "raw_detections",
        "reviewed_incidents",
        "label_questions",
        "annotations",
        "ocr_observations",
        "route_points",
        "maintenance_actions",
        "report_payload",
        "manifest",
    ]
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        candidate_id = terminal["result"]["candidates"][0]["candidate_id"]
        approved = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
                f"/candidates/{candidate_id}/answer"
            ),
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "answer": "edit",
                "selected_issue_type": "pothole",
                "severity": "major",
                "maintenance_action": "노면 보수",
                "normalized_box": {
                    "x": 0.1,
                    "y": 0.1,
                    "width": 0.5,
                    "height": 0.5,
                },
            },
        )
        assert approved.status_code == 201, approved.text
        _materialize_verified_survey_incident_for_export_test(
            client,
            job_id,
        )

        resolver_calls = {"count": 0}
        original_resolver = client.app.state.analysis_jobs.resolve_artifact

        def tracking_resolver(*args, **kwargs):
            resolver_calls["count"] += 1
            return original_resolver(*args, **kwargs)

        monkeypatch.setattr(
            client.app.state.analysis_jobs,
            "resolve_artifact",
            tracking_resolver,
        )
        original_list_incidents = client.app.state.store.list_incidents
        active = original_list_incidents(
            DEMO_PROJECT_ID,
            analysis_job_id=UUID(job_id),
        )
        assert len(active) == 1
        monkeypatch.setattr(
            client.app.state.store,
            "list_incidents",
            lambda *_args, **_kwargs: active
            * (MAX_EVIDENCE_INCIDENTS + 1),
        )
        too_many = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "artifacts": artifacts,
                "destination_path": str(export_root),
            },
        )
        assert too_many.status_code == 409, too_many.text
        assert resolver_calls["count"] == 0
        assert list(export_root.iterdir()) == []

        monkeypatch.setattr(
            client.app.state.store,
            "list_incidents",
            original_list_incidents,
        )
        original_get = client.app.state.analysis_jobs.get

        def oversized_get(project_id, selected_job_id):
            result = deepcopy(original_get(project_id, selected_job_id))
            candidate = result["result"]["candidates"][0]
            candidate["representative_image"]["size_bytes"] = (
                MAX_EVIDENCE_TOTAL_BYTES
            )
            candidate["preview_clip"]["size_bytes"] = 1
            return result

        monkeypatch.setattr(
            client.app.state.analysis_jobs, "get", oversized_get
        )
        too_large = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "artifacts": artifacts,
                "destination_path": str(export_root),
            },
        )
        assert too_large.status_code == 409, too_large.text
        assert resolver_calls["count"] == 0
        assert list(export_root.iterdir()) == []


def test_production_export_cannot_select_another_projects_analysis_job(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    export_root = tmp_path / "cross project export"
    export_root.mkdir()
    with _production_client(tmp_path, ffmpeg, ffprobe) as client:
        own_job = _complete_analysis(client, source)
        created = client.post(
            "/projects",
            headers=_auth(),
            json={
                "name": "격리 프로젝트",
                "description": "근거 내보내기 격리 검증",
            },
        )
        assert created.status_code == 201, created.text
        other_project_id = created.json()["id"]
        other_job = _complete_analysis_for_project(
            client, source, other_project_id
        )

        artifacts = [
            "raw_detections",
            "reviewed_incidents",
            "label_questions",
            "annotations",
            "ocr_observations",
            "route_points",
            "maintenance_actions",
            "report_payload",
            "manifest",
        ]
        wrong_job = client.post(
            f"/projects/{DEMO_PROJECT_ID}/exports",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": other_job["job_id"],
                "artifacts": artifacts,
                "destination_path": str(export_root),
            },
        )
        assert wrong_job.status_code == 404, wrong_job.text
        assert str(tmp_path) not in wrong_job.text
        assert list(export_root.iterdir()) == []

        wrong_project = client.post(
            f"/projects/{other_project_id}/exports",
            headers=_auth(),
            json={
                "project_id": other_project_id,
                "analysis_job_id": own_job["job_id"],
                "artifacts": artifacts,
                "destination_path": str(export_root),
            },
        )
        assert wrong_project.status_code == 404, wrong_project.text
        assert str(tmp_path) not in wrong_project.text
        assert list(export_root.iterdir()) == []
