import hashlib
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline_jobs import (
    PIPELINE_RERUN_MEDIA_PROOF_SCHEMA,
    PipelineStageResult,
)
from app.source_candidate_probe import SourceCandidateProbe
from app.store import DEMO_PROJECT_ID


TOKEN = "p" * 64


class ApiRuntime:
    def capability_snapshot(self, profile):
        detector_pack_id = profile["detector_pack_id"]
        ocr_pack_id = profile["ocr_pack_id"]
        sam2_pack_id = profile["sam2_pack_id"]
        return {
            "local_models": "ready",
            "selected_report_provider": profile["report_provider"],
            "bulk_frame_provider": "local_models_only",
            "requested_detector_pack_id": detector_pack_id,
            "requested_ocr_pack_id": ocr_pack_id,
            "requested_sam2_pack_id": sam2_pack_id,
            "installed_detector_pack_id": detector_pack_id,
            "installed_ocr_pack_id": ocr_pack_id,
            "installed_sam2_pack_id": sam2_pack_id,
            "detector_model_manifest_sha256": hashlib.sha256(
                f"detector:{detector_pack_id}".encode()
            ).hexdigest(),
            "ocr_model_manifest_sha256": hashlib.sha256(
                f"ocr:{ocr_pack_id}".encode()
            ).hexdigest(),
            "sam2_model_manifest_sha256": hashlib.sha256(
                f"sam2:{sam2_pack_id}".encode()
            ).hexdigest(),
        }

    def execute(self, stage, context, _cancel):
        if stage == "intake_validation":
            return PipelineStageResult(
                "success",
                checkpoint={
                    "intake_validation_id": "intake_validation-api-v1",
                    "duration_seconds": 120.0,
                    "size_bytes": 1_000_000,
                },
                metrics={"completed": 1},
            )
        if stage == "sha256":
            return PipelineStageResult(
                "success",
                checkpoint={
                    "hash_job_id": (
                        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                    ),
                    "source_sha256": hashlib.sha256(
                        b"pipeline-api-rerun-source"
                    ).hexdigest(),
                    "total_bytes": 1_000_000,
                },
                metrics={"bytes_hashed": 1_000_000},
            )
        if stage == "metadata_gps_preflight":
            return PipelineStageResult(
                "blocked_gpx_required",
                checkpoint={
                    "timed_track_status": "missing",
                    "capture_start_coordinate_used_as_route": False,
                },
                metrics={"route_point_count": 0},
                error_code="gpx_required",
                non_blocking=True,
            )
        if stage == "question_queue":
            return PipelineStageResult(
                "success",
                checkpoint={"question_queue_revision": "api-q1"},
                metrics={"question_count": 1},
            )
        if stage == "derived_outputs":
            assert context["review_completion_id"] == "api-review-v1"
        return PipelineStageResult(
            "success",
            checkpoint={f"{stage}_id": f"{stage}-api-v1"},
            metrics={"completed": 1},
        )

    def cancel(self, _context):
        return None

    def validate_review_completion(
        self, _context, review_completion_id
    ):
        return {
            "review_completion_id": review_completion_id,
            "all_required_questions_answered": True,
            "answered_question_count": 1,
            "server_answer_ledger_revision": "api-answers-v1",
        }

    def validate_server_owned_rerun_source(
        self,
        *,
        project_id,
        snapshot_id,
        file_name,
        sha_checkpoint,
    ):
        return {
            "schema_version": PIPELINE_RERUN_MEDIA_PROOF_SCHEMA,
            "project_id": str(project_id),
            "snapshot_id": str(snapshot_id),
            "hash_job_id": sha_checkpoint["hash_job_id"],
            "file_name": file_name,
            "source_sha256": sha_checkpoint["source_sha256"],
            "size_bytes": sha_checkpoint["total_bytes"],
            "path_exposed": False,
        }


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def _wait(client, url, status):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(url, headers=_headers())
        assert response.status_code == 200, response.text
        if response.json()["status"] == status:
            return response.json()
        time.sleep(0.01)
    raise AssertionError(client.get(url, headers=_headers()).json())


def test_pipeline_api_profile_idempotency_review_gate_and_project_scope(
    tmp_path,
    monkeypatch,
):
    fixed_disk_usage = lambda _path: SimpleNamespace(
        total=1 << 50,
        used=0,
        free=1 << 50,
    )
    monkeypatch.setattr(
        "app.pipeline_jobs.shutil.disk_usage",
        fixed_disk_usage,
    )
    with TestClient(
        create_app(
            TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(tmp_path.resolve()),
            pipeline_runtime=ApiRuntime(),
            source_candidate_probe=SourceCandidateProbe(None),
        )
    ) as client:
        # This test exercises API orchestration, not the host machine's current
        # disk pressure. Keep its capacity observation deterministic so a
        # developer drive filling up cannot turn the fixture into a production
        # storage-preflight test.
        client.app.state.project_artifact_roots._disk_usage = fixed_disk_usage
        assert client.app.state.derived_outputs is not None
        assert (
            client.app.state.derived_outputs._report_preparer
            is client.app.state.pipeline_report_previews
        )
        assert client.app.state.report_semantic_maps is not None
        base = f"/projects/{DEMO_PROJECT_ID}/pipeline"
        missing = client.get(f"{base}/profile", headers=_headers())
        assert missing.status_code == 409
        assert "blocked_configuration_required" in missing.json()["detail"]

        profile_payload = {
            "project_id": str(DEMO_PROJECT_ID),
            "question_policy": "learning_assist",
            "review_mode": "precision",
            "label_confirmation_authority": "human_review",
            "default_template_id": "sample-4q-v1",
            "default_template_sha256": hashlib.sha256(
                b"template"
            ).hexdigest(),
            "detector_pack_id": "road-detector-v1",
            "ocr_pack_id": "korean-ocr-v1",
            "sam2_pack_id": "sam2-v1",
            "report_provider": "codex",
            "output_location_id": "project-default",
            "candidate_rescan_fps": 8,
        }
        configured = client.put(
            f"{base}/profile",
            headers=_headers(),
            json=profile_payload,
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["schema_version"] == (
            "trafficvisson.pipeline-profile.v4"
        )
        assert configured.json()["label_confirmation_authority"] == (
            "human_review"
        )
        assert configured.json()["report_transmission_consent"] is False
        assert configured.json()["template_semantic_map_id"] == (
            "unconfigured"
        )
        assert configured.json()["template_semantic_map_ready"] is False
        assert configured.json()["report_route_name"] == "unconfigured"
        assert configured.json()["report_inspector_name"] == (
            "unconfigured"
        )
        assert configured.json()["report_evidence_mode"] == (
            "internal_original"
        )
        assert configured.json()["report_inspection_date_policy"] == (
            "source_filename_yyyymmdd_or_block"
        )
        assert configured.json()["revision"] == 1
        assert configured.json()["bulk_frame_provider_policy"] == (
            "local_models_only"
        )

        configured = client.put(
            f"{base}/profile",
            headers=_headers(),
            json={
                **profile_payload,
                "report_route_name": (
                    "  서울북부고속도로 (구리-포천)  "
                ),
                "report_inspector_name": "  홍길동  ",
                "report_evidence_mode": "shared_deidentified",
                "report_inspection_date_policy": (
                    "source_filename_yyyymmdd_or_block"
                ),
            },
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["report_route_name"] == (
            "서울북부고속도로 (구리-포천)"
        )
        assert configured.json()["report_inspector_name"] == "홍길동"
        assert configured.json()["report_evidence_mode"] == (
            "shared_deidentified"
        )
        assert configured.json()["revision"] == 2

        snapshot_id = uuid4()
        start_payload = {
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": str(snapshot_id),
            "file_name": "drive.mp4",
            "rights_confirmed": True,
            "idempotency_key": "pipeline-api-idempotency-01",
        }
        started = client.post(
            f"{base}/jobs",
            headers=_headers(),
            json=start_payload,
        )
        assert started.status_code == 202, started.text
        assert started.json()["idempotent_replay"] is False
        job_id = started.json()["job"]["job_id"]
        replay = client.post(
            f"{base}/jobs",
            headers=_headers(),
            json=start_payload,
        )
        assert replay.status_code == 202
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["job"]["job_id"] == job_id

        job_url = f"{base}/jobs/{job_id}"
        waiting = _wait(client, job_url, "awaiting_review")
        assert waiting["current_stage"] == "question_queue"
        assert waiting["provenance"]["route_status"] == (
            "blocked_gpx_required"
        )
        assert (
            waiting["provenance"]["provider_policy"]
            == "codex_claude_structured_proposal_not_bulk_frames"
        )
        assert waiting["profile_snapshot"]["sam2_pack_id"] == "sam2-v1"
        assert len(waiting["provenance"]["profile_sha256"]) == 64

        too_early = client.post(
            f"{base}/jobs/{job_id}/resume",
            headers=_headers(),
            json={},
        )
        assert too_early.status_code == 409
        accepted = client.post(
            f"{job_url}/review-complete",
            headers=_headers(),
            json={"review_completion_id": "api-review-v1"},
        )
        assert accepted.status_code == 202, accepted.text
        completed = _wait(client, job_url, "completed")
        assert completed["outcome_code"] == "success"
        assert completed["progress_percent"] == 100
        source_record_sha256 = completed["record_sha256"]

        current_profile_payload = {
            **profile_payload,
            "detector_pack_id": "road-detector-v2",
            "ocr_pack_id": "korean-ocr-v2",
            "sam2_pack_id": "sam2-v2",
            "candidate_rescan_fps": 9,
        }
        current_profile = client.put(
            f"{base}/profile",
            headers=_headers(),
            json=current_profile_payload,
        )
        assert current_profile.status_code == 200, current_profile.text
        assert current_profile.json()["revision"] == 3
        rerun_url = f"{base}/jobs/{job_id}/reruns"
        rerun_payload = {
            "project_id": str(DEMO_PROJECT_ID),
            "source_job_id": job_id,
            "expected_source_record_sha256": source_record_sha256,
            "expected_profile_revision": 3,
            "rights_confirmed": True,
            "user_confirmed": True,
            "idempotency_key": "pipeline-api-rerun-current-0001",
        }
        for invalid in (
            {"snapshot_id": str(snapshot_id)},
            {"model_pack_id": "renderer-selected-pack"},
            {
                "expected_source_record_sha256": (
                    source_record_sha256.upper()
                )
            },
            {
                "expected_source_record_sha256": (
                    f" {source_record_sha256}"
                )
            },
            {"expected_profile_revision": True},
            {"user_confirmed": False},
            {"idempotency_key": "explicit-new:rerun-source-0001"},
        ):
            rejected = client.post(
                rerun_url,
                headers=_headers(),
                json={**rerun_payload, **invalid},
            )
            assert rejected.status_code == 422, rejected.text

        mismatched_source = client.post(
            rerun_url,
            headers=_headers(),
            json={
                **rerun_payload,
                "source_job_id": str(uuid4()),
            },
        )
        assert mismatched_source.status_code == 409
        stale_profile = client.post(
            rerun_url,
            headers=_headers(),
            json={
                **rerun_payload,
                "expected_profile_revision": 2,
                "idempotency_key": "pipeline-api-rerun-stale-0001",
            },
        )
        assert stale_profile.status_code == 409

        rerun_started = client.post(
            rerun_url,
            headers=_headers(),
            json=rerun_payload,
        )
        assert rerun_started.status_code == 202, rerun_started.text
        assert rerun_started.json()["idempotent_replay"] is False
        rerun_job_id = rerun_started.json()["job"]["job_id"]
        assert rerun_job_id != job_id
        rerun_job = _wait(
            client,
            f"{base}/jobs/{rerun_job_id}",
            "awaiting_review",
        )
        assert rerun_job["snapshot_id"] == str(snapshot_id)
        assert rerun_job["profile_snapshot"]["revision"] == 3
        assert rerun_job["profile_snapshot"]["detector_pack_id"] == (
            "road-detector-v2"
        )
        assert rerun_job["capability_snapshot"][
            "detector_model_manifest_sha256"
        ] == hashlib.sha256(b"detector:road-detector-v2").hexdigest()
        assert rerun_job["stages"][0]["attempts"] == 0
        assert rerun_job["stages"][1]["attempts"] == 0
        assert rerun_job["provenance"]["rerun"]["source_job_id"] == job_id
        assert (
            rerun_job["provenance"]["rerun"]["source_record_sha256"]
            == source_record_sha256
        )
        assert (
            rerun_job["provenance"]["rerun"]["media_proof"]["path_exposed"]
            is False
        )
        source_after_rerun = client.get(job_url, headers=_headers())
        assert source_after_rerun.status_code == 200
        assert (
            source_after_rerun.json()["record_sha256"]
            == source_record_sha256
        )
        assert source_after_rerun.json()["profile_snapshot"]["revision"] == 2

        rerun_replay = client.post(
            rerun_url,
            headers=_headers(),
            json=rerun_payload,
        )
        assert rerun_replay.status_code == 202
        assert rerun_replay.json()["idempotent_replay"] is True
        assert rerun_replay.json()["job"]["job_id"] == rerun_job_id

        wrong_project = uuid4()
        missing_cross_project = client.get(
            f"/projects/{wrong_project}/pipeline/jobs/{job_id}",
            headers=_headers(),
        )
        assert missing_cross_project.status_code == 404
