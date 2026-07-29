import hashlib
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline_jobs import PipelineStageResult
from app.source_candidate_probe import SourceCandidateProbe
from app.store import DEMO_PROJECT_ID


TOKEN = "p" * 64


class ApiRuntime:
    def capability_snapshot(self, profile):
        return {
            "local_models": "ready",
            "selected_report_provider": profile["report_provider"],
            "bulk_frame_provider": "local_models_only",
        }

    def execute(self, stage, context, _cancel):
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
):
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
        assert client.app.state.derived_outputs is not None
        base = f"/projects/{DEMO_PROJECT_ID}/pipeline"
        missing = client.get(f"{base}/profile", headers=_headers())
        assert missing.status_code == 409
        assert "blocked_configuration_required" in missing.json()["detail"]

        profile_payload = {
            "project_id": str(DEMO_PROJECT_ID),
            "question_policy": "learning_assist",
            "review_mode": "precision",
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
        assert configured.json()["revision"] == 1
        assert configured.json()["bulk_frame_provider_policy"] == (
            "local_models_only"
        )

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

        wrong_project = uuid4()
        missing_cross_project = client.get(
            f"/projects/{wrong_project}/pipeline/jobs/{job_id}",
            headers=_headers(),
        )
        assert missing_cross_project.status_code == 404
