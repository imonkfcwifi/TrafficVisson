from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INSTALLER = _load(
    "trafficvisson_install_grounding_dino_pack",
    SCRIPTS / "install-experimental-grounding-dino-pack.py",
)
EVALUATOR = _load(
    "trafficvisson_evaluate_grounding_dino_candidates",
    SCRIPTS / "evaluate_grounding_dino_candidates.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_fixture_pack(tmp_path: Path, monkeypatch):
    model_bytes = b"fixture-safetensors-model"
    config_bytes = b'{"model_type":"grounding-dino"}\n'
    snapshot_files = {
        "config.json": len(config_bytes),
        "model.safetensors": len(model_bytes),
    }
    monkeypatch.setattr(INSTALLER, "SNAPSHOT_FILES", snapshot_files)
    monkeypatch.setattr(
        INSTALLER,
        "SNAPSHOT_TOTAL_BYTES",
        sum(snapshot_files.values()),
    )
    monkeypatch.setattr(
        INSTALLER,
        "MODEL_FILE_SHA256",
        hashlib.sha256(model_bytes).hexdigest(),
    )
    monkeypatch.setattr(
        INSTALLER,
        "MIN_FREE_RESERVE_BYTES",
        1,
    )
    monkeypatch.setattr(
        EVALUATOR,
        "MODEL_FILE_SHA256",
        hashlib.sha256(model_bytes).hexdigest(),
    )

    payloads = {
        "config.json": config_bytes,
        "model.safetensors": model_bytes,
    }
    calls = []

    def downloader(remote_name, destination, *, expected_size, deadline):
        assert deadline > 0
        calls.append(remote_name)
        content = payloads[remote_name]
        assert len(content) == expected_size
        destination.write_bytes(content)

    def info_provider():
        return {
            "repo_id": INSTALLER.REPO_ID,
            "resolved_revision": INSTALLER.IMMUTABLE_REVISION,
            "private": False,
            "gated": False,
            "tags": ["license:apache-2.0"],
            "files": snapshot_files,
        }

    program_data = tmp_path / "ProgramData"
    target, manifest = INSTALLER.install_pack(
        program_data=program_data,
        maximum_download_seconds=60,
        info_provider=info_provider,
        downloader=downloader,
    )
    return {
        "program_data": program_data,
        "target": target,
        "manifest": manifest,
        "manifest_path": target / "trafficvisson-grounding-dino-pack.json",
        "calls": calls,
        "info_provider": info_provider,
        "downloader": downloader,
    }


def test_installer_pins_snapshot_hashes_runtime_and_review_only_contract(
    tmp_path,
    monkeypatch,
):
    fixture = _install_fixture_pack(tmp_path, monkeypatch)
    manifest = fixture["manifest"]
    assert manifest["source"]["repo_id"] == "IDEA-Research/grounding-dino-tiny"
    assert (
        manifest["source"]["resolved_revision"]
        == "a2bb814dd30d776dcf7e30523b00659f4f141c71"
    )
    assert manifest["license"]["weights_spdx"] == "Apache-2.0"
    assert manifest["installer_distribution"] == "excluded"
    assert manifest["review_only"] is True
    assert manifest["automatic_labeling_allowed"] is False
    assert manifest["incident_creation_allowed"] is False
    assert manifest["model_promotion_allowed"] is False
    assert manifest["sidecar_recording_allowed"] is False
    assert manifest["inference_contract"]["local_files_only"] is True
    assert manifest["inference_contract"]["trust_remote_code"] is False
    assert manifest["inference_contract"]["use_safetensors"] is True
    assert (
        manifest["inference_contract"]["score_semantics"]
        == "model_score_not_probability"
    )
    assert "lane_marking_repaint_needed" in manifest[
        "inference_contract"
    ]["excluded_state_claims"]
    assert fixture["calls"] == ["config.json", "model.safetensors"]
    for row in manifest["artifacts"]:
        path = fixture["target"] / row["path"]
        assert path.stat().st_size == row["size_bytes"]
        assert _sha256(path) == row["sha256"]

    def fail_info():
        pytest.fail("verified existing packs must not access the network")

    target, again = INSTALLER.install_pack(
        program_data=fixture["program_data"],
        maximum_download_seconds=60,
        info_provider=fail_info,
        downloader=lambda *_args, **_kwargs: pytest.fail(
            "verified existing packs must not redownload"
        ),
    )
    assert target == fixture["target"]
    assert again == manifest


def test_installer_rejects_tampered_existing_snapshot(tmp_path, monkeypatch):
    fixture = _install_fixture_pack(tmp_path, monkeypatch)
    (fixture["target"] / "snapshot" / "config.json").write_bytes(b"tampered")
    with pytest.raises(
        INSTALLER.PackInstallError,
        match="failed verification",
    ):
        INSTALLER.install_pack(
            program_data=fixture["program_data"],
            maximum_download_seconds=60,
            info_provider=fixture["info_provider"],
            downloader=fixture["downloader"],
        )


def _candidate_fixture(tmp_path: Path):
    data_root = tmp_path / "bridge-data"
    project_id = "11111111-1111-4111-8111-111111111111"
    job_id = "22222222-2222-4222-8222-222222222222"
    record_root = (
        data_root
        / "projects"
        / project_id
        / "analysis-job-records"
    )
    artifact_root = (
        data_root
        / "projects"
        / project_id
        / "candidate-extractions"
        / "candidate-extraction-grounding-fixture"
    )
    record_root.mkdir(parents=True)
    artifact_root.mkdir(parents=True)
    record = record_root / f"{job_id}.json"
    record.write_text('{"fixture":"analysis-record"}\n', encoding="utf-8")
    record_digest = _sha256(record)
    record.with_suffix(".sha256").write_text(record_digest, encoding="ascii")
    (data_root / "candidate-labels").mkdir()
    (data_root / "candidate-labels" / ".lock").write_bytes(b"labels-unchanged")
    (data_root / "experimental-proposals").mkdir()
    (data_root / "experimental-proposals" / ".keep").write_bytes(
        b"sidecars-unchanged"
    )
    (data_root / "catalog.sqlite3").write_bytes(b"sqlite-unchanged")
    (data_root / "catalog.pre-v2.sqlite3").write_bytes(
        b"sqlite-backup-unchanged"
    )
    candidates = []
    for index, color in enumerate(((20, 90, 120), (80, 140, 40)), start=1):
        candidate_id = f"{index:020x}"
        image = artifact_root / f"{candidate_id}.jpg"
        Image.new("RGB", (320, 240), color=color).save(
            image,
            format="JPEG",
            quality=86,
        )
        digest = _sha256(image)
        evaluation_id = hashlib.sha256(
            f"{candidate_id}\0{digest}".encode("ascii")
        ).hexdigest()[:20]
        candidates.append(
            EVALUATOR.candidate_evaluator.SourceCandidate(
                project_id=project_id,
                job_id=job_id,
                source_file_name="fixture.mp4",
                source_candidate_id=candidate_id,
                evaluation_candidate_id=evaluation_id,
                representative_timestamp_seconds=float(index),
                scene_score=0.5,
                sharpness_score=100.0,
                triggers=("fixture",),
                record_path=record,
                record_relative_path=record.relative_to(data_root).as_posix(),
                record_sha256=record_digest,
                image_path=image,
                image_relative_path=image.relative_to(data_root).as_posix(),
                image_size_bytes=image.stat().st_size,
                image_sha256=digest,
            )
        )
    protected = [
        path
        for path in data_root.rglob("*")
        if path.is_file()
    ]
    before = {path: path.read_bytes() for path in protected}
    return data_root, candidates, before


def _fake_worker(request_path, output_path, *, timeout_seconds):
    assert timeout_seconds == 60
    request = json.loads(request_path.read_text("utf-8"))
    query = next(
        (
            row
            for row in request["queries"]
            if row["query_id"] == "road_debris"
        ),
        request["queries"][0],
    )
    results = []
    for index, row in enumerate(request["candidates"]):
        predictions = []
        if index == 0:
            predictions = [
                {
                    "query_id": query["query_id"],
                    "query_prompt": query["prompt"],
                    "candidate_type": query["candidate_type"],
                    "source_text_label": query["prompt"],
                    "model_score": 0.61,
                    "score_semantics": "model_score_not_probability",
                    "is_probability": False,
                    "box": {
                        "x": 0.2,
                        "y": 0.4,
                        "width": 0.25,
                        "height": 0.2,
                    },
                }
            ]
        results.append(
            {
                "candidate_id": row["candidate_id"],
                "duration_ms": 12,
                "status": "classified" if predictions else "no_detection",
                "predictions": predictions,
            }
        )
    response = {
        "schema_version": EVALUATOR.WORKER_SCHEMA,
        "status": "completed",
        "pack_id": EVALUATOR.PACK_ID,
        "pack_version": EVALUATOR.PACK_VERSION,
        "pack_manifest_sha256": request["pack_manifest_sha256"],
        "score_semantics": EVALUATOR.SCORE_LABEL,
        "runtime": {
            "python": "fixture",
            "torch": "fixture",
            "transformers": "fixture",
            "device": "cuda:0",
            "cuda_available": True,
            "cuda_version": "fixture",
            "cuda_device_name": "NVIDIA RTX A2000 12GB",
            "cuda_total_memory_bytes": 12_000_000_000,
            "maximum_cuda_allocator_bytes": request["max_vram_bytes"],
            "peak_cuda_memory_allocated_bytes": 2_000_000_000,
            "peak_cuda_memory_reserved_bytes": 2_100_000_000,
            "dtype": "torch.float16",
            "cpu_fallback_used": False,
            "local_files_only": True,
            "trust_remote_code": False,
            "use_safetensors": True,
        },
        "results": results,
    }
    if "command_recipe" in request:
        response["command_recipe"] = request["command_recipe"]
        response["query_contract_sha256"] = (
            EVALUATOR._canonical_json_sha256(request["queries"])
        )
    return response


def test_evaluator_exports_review_only_boxes_and_preserves_all_stores(
    tmp_path,
    monkeypatch,
):
    pack = _install_fixture_pack(tmp_path / "pack", monkeypatch)
    data_root, candidates, protected_before = _candidate_fixture(tmp_path)
    discovery = SimpleNamespace(candidates=tuple(candidates))
    envelope = EVALUATOR.run_evaluation(
        data_root=data_root,
        pack_manifest=pack["manifest_path"],
        run_name="grounding-dino-fixture",
        limit=2,
        maximum_wall_seconds=60,
        maximum_vram_bytes=4 * 1024**3,
        maximum_output_bytes=64 * 1024 * 1024,
        maximum_evidence=2,
        program_data=tmp_path / "EvaluationProgramData",
        worker_runner=_fake_worker,
        discovery_function=lambda *_args, **_kwargs: discovery,
    )
    assert envelope["status"] == "completed"
    assert envelope["summary"] == {
        "candidate_count": 2,
        "proposal_frame_count": 1,
        "prediction_count": 1,
        "evidence_frame_count": 1,
        "contact_sheet_count": 1,
        "status_counts": {"classified": 1, "no_detection": 1},
    }
    assert envelope["runtime"]["device"] == "cuda:0"
    assert envelope["score_semantics"]["is_probability"] is False
    assert envelope["side_effects"] == {
        "analysis_records_modified": 0,
        "label_records_modified": 0,
        "sqlite_databases_modified": 0,
        "automatic_labels_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "sidecars_created": 0,
    }
    proposal = next(
        row["predictions"][0]
        for row in envelope["results"]
        if row["predictions"]
    )
    assert proposal["query_id"] == "road_debris"
    assert proposal["score_semantics"] == "model_score_not_probability"
    assert proposal["is_probability"] is False

    output = (
        tmp_path
        / "EvaluationProgramData"
        / "TrafficVisson"
        / "ModelEvaluations"
        / "grounding-dino-fixture"
    )
    assert not (output / ".staging").exists()
    assert not (output / ".worker-request.json").exists()
    assert not (output / ".worker-output.json").exists()
    for name in (
        "predictions.json",
        "candidate_scores.csv",
        "selection_manifest.json",
        "provenance.json",
        "README.md",
        "contact-sheet-001.jpg",
        "manifest.json",
    ):
        assert (output / name).is_file()
    with (output / "candidate_scores.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    assert {row["is_probability"] for row in rows} == {"False"}
    assert {row["automatic_label_created"] for row in rows} == {"False"}
    assert {row["incident_created"] for row in rows} == {"False"}
    assert {row["model_promoted"] for row in rows} == {"False"}
    assert {row["sidecar_created"] for row in rows} == {"False"}
    for path, content in protected_before.items():
        assert path.read_bytes() == content
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    for artifact in manifest["artifacts"]:
        path = output / artifact["path"]
        assert path.stat().st_size == artifact["size_bytes"]
        assert _sha256(path) == artifact["sha256"]


def test_deterministic_selection_is_order_independent(tmp_path):
    _, candidates, _ = _candidate_fixture(tmp_path)
    first = EVALUATOR._select_candidates(candidates, limit=2)
    second = EVALUATOR._select_candidates(list(reversed(candidates)), limit=2)
    assert [
        row.evaluation_candidate_id for row in first
    ] == [
        row.evaluation_candidate_id for row in second
    ]


def test_completed_job_command_uses_all_candidates_and_only_fixed_prompts(
    tmp_path,
    monkeypatch,
):
    pack = _install_fixture_pack(tmp_path / "pack", monkeypatch)
    data_root, candidates, protected_before = _candidate_fixture(tmp_path)
    project_id = candidates[0].project_id
    job_id = candidates[0].job_id
    foreign_job = replace(
        candidates[0],
        job_id="33333333-3333-4333-8333-333333333333",
        source_candidate_id="f" * 20,
        evaluation_candidate_id="e" * 20,
    )
    discovery = SimpleNamespace(
        candidates=tuple([foreign_job, *reversed(candidates)])
    )
    command = "영상에서 낙하물만 찾아서 후보로 검지해줘"
    captured = {}

    def worker(request_path, output_path, *, timeout_seconds):
        raw_request = request_path.read_text("utf-8")
        assert command not in raw_request
        captured["request"] = json.loads(raw_request)
        return _fake_worker(
            request_path,
            output_path,
            timeout_seconds=timeout_seconds,
        )

    envelope = EVALUATOR.run_evaluation(
        data_root=data_root,
        pack_manifest=pack["manifest_path"],
        run_name="grounding-dino-job-command-fixture",
        limit=1,
        project_id=project_id,
        job_id=job_id,
        command=command,
        maximum_wall_seconds=60,
        maximum_vram_bytes=4 * 1024**3,
        maximum_output_bytes=64 * 1024 * 1024,
        maximum_evidence=2,
        program_data=tmp_path / "EvaluationProgramData",
        worker_runner=worker,
        discovery_function=lambda *_args, **_kwargs: discovery,
    )

    request = captured["request"]
    expected_queries = [
        {
            "query_id": "road_debris",
            "prompt": "foreign object or debris on the roadway",
            "candidate_type": "road_debris_candidate",
        }
    ]
    assert request["queries"] == expected_queries
    assert {
        row["candidate_id"] for row in request["candidates"]
    } == {
        candidate.evaluation_candidate_id for candidate in candidates
    }
    assert len(request["candidates"]) == 2
    assert request["command_recipe"]["raw_command_stored"] is False
    assert (
        request["command_recipe"]["grounding_dino_positive_prompts"]
        == ["foreign object or debris on the roadway"]
    )
    assert envelope["selection"] == {
        "method": "completed_analysis_job_all_candidates",
        "seed": None,
        "available_candidate_count": 3,
        "selected_candidate_count": 2,
        "requested_limit_applied": False,
        "project_id": project_id,
        "analysis_job_id": job_id,
        "source_file_name": "fixture.mp4",
        "source_record_ref": (
            f"projects/{project_id}/analysis-job-records/{job_id}.json"
        ),
        "source_record_sha256": candidates[0].record_sha256,
    }
    assert envelope["queries"] == expected_queries
    assert envelope["summary"]["candidate_count"] == 2
    assert envelope["limits"]["maximum_frames"] == 2
    assert envelope["command_recipe"]["issue_type"] == "road_debris"
    assert envelope["command_recipe"]["command_sha256"] == (
        request["command_recipe"]["command_sha256"]
    )
    assert envelope["side_effects"] == {
        "analysis_records_modified": 0,
        "label_records_modified": 0,
        "sqlite_databases_modified": 0,
        "automatic_labels_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "sidecars_created": 0,
    }

    output = (
        tmp_path
        / "EvaluationProgramData"
        / "TrafficVisson"
        / "ModelEvaluations"
        / "grounding-dino-job-command-fixture"
    )
    for name in (
        "predictions.json",
        "selection_manifest.json",
        "provenance.json",
        "manifest.json",
    ):
        payload = json.loads((output / name).read_text("utf-8"))
        assert payload["command_recipe"] == envelope["command_recipe"]
        assert command not in (output / name).read_text("utf-8")
    readme = (output / "README.md").read_text("utf-8")
    assert envelope["command_recipe"]["recipe_id"] in readme
    assert "foreign object or debris on the roadway" in readme
    assert command not in readme
    with (output / "candidate_scores.csv").open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as stream:
        rows = list(csv.DictReader(stream))
    assert {row["recipe_id"] for row in rows} == {
        envelope["command_recipe"]["recipe_id"]
    }
    assert {row["issue_type"] for row in rows} == {"road_debris"}
    assert {row["command_sha256"] for row in rows} == {
        envelope["command_recipe"]["command_sha256"]
    }
    for path, content in protected_before.items():
        assert path.read_bytes() == content


def test_completed_job_selection_and_command_preconditions_fail_closed(
    tmp_path,
):
    data_root, candidates, _ = _candidate_fixture(tmp_path)
    project_id = candidates[0].project_id
    job_id = candidates[0].job_id

    with pytest.raises(
        EVALUATOR.GroundingDinoEvaluationError,
        match="requires a canonical --project-id",
    ):
        EVALUATOR.run_evaluation(
            data_root=tmp_path / "missing",
            pack_manifest=tmp_path / "missing-pack.json",
            run_name="missing-project",
            job_id=job_id,
        )
    with pytest.raises(
        EVALUATOR.GroundingDinoEvaluationError,
        match="--command requires",
    ):
        EVALUATOR.run_evaluation(
            data_root=tmp_path / "missing",
            pack_manifest=tmp_path / "missing-pack.json",
            run_name="missing-job",
            project_id=project_id,
            command="낙하물을 찾아줘",
        )
    with pytest.raises(
        EVALUATOR.GroundingDinoEvaluationError,
        match="has no valid persisted candidates",
    ):
        EVALUATOR._select_completed_job_candidates(
            candidates,
            data_root=data_root,
            project_id=project_id,
            job_id="33333333-3333-4333-8333-333333333333",
        )
    with pytest.raises(
        EVALUATOR.GroundingDinoEvaluationError,
        match="crossed the selected project boundary",
    ):
        EVALUATOR._select_completed_job_candidates(
            [
                replace(
                    candidates[0],
                    project_id="44444444-4444-4444-8444-444444444444",
                )
            ],
            data_root=data_root,
            project_id=project_id,
            job_id=job_id,
        )


def test_command_recipe_rejects_unmapped_or_multi_issue_text():
    project_id = "11111111-1111-4111-8111-111111111111"
    for command in (
        "도로 상태를 알아서 판단해줘",
        "포트홀과 낙하물을 같이 찾아줘",
    ):
        with pytest.raises(
            EVALUATOR.GroundingDinoEvaluationError,
            match="exactly one supported road issue type",
        ):
            EVALUATOR._compile_command_recipe(
                command,
                project_id=project_id,
            )


def test_evaluator_rejects_probability_like_boolean_score(tmp_path):
    _, candidates, _ = _candidate_fixture(tmp_path)
    candidate = candidates[0]
    worker = {
        "schema_version": EVALUATOR.WORKER_SCHEMA,
        "status": "completed",
        "pack_id": EVALUATOR.PACK_ID,
        "pack_version": EVALUATOR.PACK_VERSION,
        "pack_manifest_sha256": "a" * 64,
        "score_semantics": EVALUATOR.SCORE_LABEL,
        "runtime": {},
        "results": [
            {
                "candidate_id": candidate.evaluation_candidate_id,
                "duration_ms": 1,
                "status": "classified",
                "predictions": [
                    {
                        "query_id": "pothole",
                        "query_prompt": "a pothole in the road",
                        "candidate_type": "pothole_candidate",
                        "source_text_label": "pothole",
                        "model_score": True,
                        "score_semantics": EVALUATOR.SCORE_LABEL,
                        "is_probability": False,
                        "box": {
                            "x": 0.1,
                            "y": 0.1,
                            "width": 0.2,
                            "height": 0.2,
                        },
                    }
                ],
            }
        ],
    }
    with pytest.raises(
        EVALUATOR.GroundingDinoEvaluationError,
        match="violates its contract",
    ):
        EVALUATOR._validate_worker_result(
            worker,
            [candidate],
            pack={"manifest_sha256": "a" * 64},
        )
