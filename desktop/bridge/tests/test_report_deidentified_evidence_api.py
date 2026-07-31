from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from app.pipeline_jobs import PipelineJobNotFound
from app.report_deidentified_evidence import (
    OpenCvReportPrivacyDetector,
    ReportDeidentifiedEvidenceService,
)
from app.store import DEMO_PROJECT_ID


TOKEN = "report-deidentified-api-token-" + ("a" * 32)
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ANALYSIS_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
INCIDENT_ID = "44444444-4444-4444-8444-444444444444"
CANDIDATE_ID = "a" * 20
SOURCE_SHA256 = "b" * 64
RECORD_HASH = "c" * 64


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeLabels:
    def __init__(self, record):
        self.record = record

    def audit_records(self, _project_id):
        return [self.record]

    def latest_answers(self, _project_id, *, analysis_job_id=None):
        assert analysis_job_id == ANALYSIS_JOB_ID
        return [self.record]


class FakeHwpService:
    def __init__(self):
        self.write_calls = []

    def apply_document(self, *args, **kwargs):
        self.write_calls.append((args, kwargs))
        raise AssertionError("report privacy review must not invoke COM")

    def close(self):
        return None


def _job():
    return {
        "job_id": str(PIPELINE_JOB_ID),
        "project_id": str(DEMO_PROJECT_ID),
        "profile_snapshot": {
            "report_evidence_mode": "shared_deidentified",
        },
        "status": "blocked",
        "current_stage": "derived_outputs",
        "outcome_code": "report_preview_blocked",
        "error_code": "deidentified_evidence_required",
        "review_completion_id": f"review:{'d' * 64}",
        "stages": [
            {
                "name": "question_queue",
                "checkpoint": {
                    "analysis_job_id": str(ANALYSIS_JOB_ID),
                    "source_sha256": SOURCE_SHA256,
                    "question_candidate_ids": [CANDIDATE_ID],
                },
                "metrics": {},
            },
            {
                "name": "derived_outputs",
                "checkpoint": {
                    "report": {
                        "blocking_reasons": [
                            "deidentified_evidence_required"
                        ]
                    }
                },
                "metrics": {"incident_count": 1},
            },
        ],
    }


def test_report_deidentified_api_prepares_reviews_and_resumes(tmp_path):
    image_path = (tmp_path / "candidate.jpg").resolve()
    Image.new("RGB", (200, 120), color=(150, 170, 190)).save(
        image_path,
        format="JPEG",
        quality=83,
    )
    detector = OpenCvReportPrivacyDetector(
        injected_regions=lambda _image: (
            {
                "kind": "license_plate",
                "x": 70,
                "y": 72,
                "width": 58,
                "height": 18,
            },
        )
    )
    registry = ReportDeidentifiedEvidenceService(
        (tmp_path / "report-registry").resolve(),
        detector=detector,
    )
    hwp = FakeHwpService()
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        report_deidentified_evidence_service=registry,
        hwp_service=hwp,
    )
    job = _job()
    resume_calls = []

    def get_job(project_id, job_id):
        if project_id != DEMO_PROJECT_ID or job_id != PIPELINE_JOB_ID:
            raise PipelineJobNotFound("job not found")
        return dict(job)

    def resume_job(project_id, job_id):
        assert project_id == DEMO_PROJECT_ID
        assert job_id == PIPELINE_JOB_ID
        resume_calls.append((project_id, job_id))
        resumed = dict(job)
        resumed["status"] = "queued"
        resumed["outcome_code"] = None
        resumed["error_code"] = None
        return resumed

    app.state.pipeline_jobs.get = get_job
    app.state.pipeline_jobs.resume = resume_job
    app.state.pipeline_jobs.is_analysis_projection_gated = (
        lambda _project_id, _analysis_job_id: False
    )
    record = {
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "candidate_id": CANDIDATE_ID,
        "record_id": "label-record-1",
        "record_hash": RECORD_HASH,
        "partition": "approved",
        "final_answer": {
            "answer": "yes",
            "answered_by": "local_user",
        },
    }
    incident = {
        "incident_id": INCIDENT_ID,
        "project_id": str(DEMO_PROJECT_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "candidate_id": CANDIDATE_ID,
        "status": "active",
        "source_answer_record_id": "label-record-1",
        "source_record_hash": RECORD_HASH,
        "representative_time_ms": 12_000,
    }
    app.state.candidate_labels = FakeLabels(record)
    app.state.store.project_label_records = (
        lambda *_args, **_kwargs: []
    )
    app.state.store.list_incidents = (
        lambda _project_id, **_kwargs: [incident]
    )
    app.state.candidate_evidence = SimpleNamespace(
        resolve_artifact=lambda *_args: SimpleNamespace(
            path=image_path,
            media_type="image/jpeg",
            size_bytes=image_path.stat().st_size,
            sha256=_sha(image_path),
        )
    )
    headers = {"Authorization": f"Bearer {TOKEN}"}

    with TestClient(app) as client:
        prepared_response = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline/jobs/"
                f"{PIPELINE_JOB_ID}/report-deidentified-evidence/"
                "prepare"
            ),
            headers=headers,
            json={},
        )
        assert prepared_response.status_code == 200
        prepared_body = prepared_response.json()
        assert prepared_body["path_exposed"] is False
        assert prepared_body["count"] == 1
        preparation = prepared_body["preparations"][0]
        assert (
            preparation["state"]
            == "awaiting_deidentification_review"
        )
        assert preparation["automatic_privacy_verification"] is False
        assert "original_path" not in preparation

        artifact_response = client.get(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline/jobs/"
                f"{PIPELINE_JOB_ID}/report-deidentified-evidence/"
                f"{INCIDENT_ID}/{preparation['derivative_id']}/artifact"
            ),
            headers=headers,
        )
        assert artifact_response.status_code == 200
        assert artifact_response.headers["content-type"] == "image/jpeg"
        assert artifact_response.headers["etag"] == (
            f"\"sha256-{preparation['deidentified_sha256']}\""
        )

        review_response = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/pipeline/jobs/"
                f"{PIPELINE_JOB_ID}/report-deidentified-evidence/"
                "review"
            ),
            headers=headers,
            json={
                "incident_id": INCIDENT_ID,
                "derivative_id": preparation["derivative_id"],
                "expected_manifest_sha256": preparation[
                    "manifest_sha256"
                ],
                "approved": True,
                "explicit_user_confirmation": True,
            },
        )

    assert review_response.status_code == 200
    reviewed = review_response.json()
    assert (
        reviewed["preparation"]["state"]
        == "verified_for_shared_report"
    )
    assert reviewed["all_required_derivatives_verified"] is True
    assert reviewed["pipeline_resume_requested"] is True
    assert reviewed["pipeline_job"]["status"] == "queued"
    assert resume_calls == [(DEMO_PROJECT_ID, PIPELINE_JOB_ID)]
    assert hwp.write_calls == []
