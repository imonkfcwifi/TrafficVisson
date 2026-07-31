from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from app.pipeline_jobs import PipelineJobNotFound
from app.provider_visual_evidence import (
    OpenCvPrivacyRedactor,
    ProviderVisualEvidencePreparationService,
)
from app.source_candidate_probe import SourceCandidateProbe
from app.store import DEMO_PROJECT_ID


TOKEN = "v" * 64
CANDIDATE_ID = "c" * 20


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _png(
    size: tuple[int, int],
    color: tuple[int, int, int],
) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _prepare(
    tmp_path: Path,
    service: ProviderVisualEvidencePreparationService,
    pipeline_job_id: UUID,
) -> dict:
    candidate = _png((240, 160), (140, 90, 50))
    candidate_path = tmp_path / "candidate.png"
    candidate_path.write_bytes(candidate)
    similar_cases = []
    resolved = {}
    for index in range(3):
        body = _png((180, 120), (50 + index * 20, 80, 130))
        digest = hashlib.sha256(body).hexdigest()
        source_id = f"sample-source-{index}"
        case_id = f"sample-case-{index}"
        similar_cases.append(
            {
                "source_id": source_id,
                "case_id": case_id,
                "source_ref": f"reference:{source_id}:{case_id}",
                "photo_sha256": digest,
                "reference_state": "approved_reference",
            }
        )
        resolved[(source_id, case_id)] = SimpleNamespace(
            body=body,
            media_type="image/png",
            size_bytes=len(body),
            sha256=digest,
        )
    return dict(
        service.prepare(
            project_id=DEMO_PROJECT_ID,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=uuid4(),
            candidate_id=CANDIDATE_ID,
            candidate_path=candidate_path.resolve(),
            candidate_media_type="image/png",
            candidate_size_bytes=len(candidate),
            candidate_sha256=hashlib.sha256(candidate).hexdigest(),
            candidate_source_ref=(
                f"deid:{CANDIDATE_ID}:provider-crop"
            ),
            target_box={
                "x": 0.25,
                "y": 0.2,
                "width": 0.5,
                "height": 0.6,
            },
            similar_cases=similar_cases,
            resolve_reference=lambda source_id, case_id: resolved[
                (source_id, case_id)
            ],
        )
    )


class _PipelineRegistry:
    def __init__(self, job_id: UUID, preparation: dict) -> None:
        self.job_id = job_id
        self.resume_calls = 0
        self.job = {
            "job_id": str(job_id),
            "project_id": str(DEMO_PROJECT_ID),
            "status": "blocked",
            "current_stage": "vision_proposal",
            "outcome_code": "partial",
            "stages": [
                {
                    "name": "vision_proposal",
                    "checkpoint": {
                        "provider_visual_evidence_by_candidate": {
                            CANDIDATE_ID: preparation,
                        },
                        "structured_judgments_by_candidate": {
                            CANDIDATE_ID: {
                                "error_code": (
                                    "awaiting_deidentification_review"
                                ),
                            },
                        },
                    },
                }
            ],
        }

    def get(self, project_id: UUID, job_id: UUID) -> dict:
        if project_id != DEMO_PROJECT_ID or job_id != self.job_id:
            raise PipelineJobNotFound("pipeline job not found")
        return dict(self.job)

    def resume(self, project_id: UUID, job_id: UUID) -> dict:
        self.get(project_id, job_id)
        self.resume_calls += 1
        self.job["status"] = "queued"
        self.job["outcome_code"] = None
        return dict(self.job)


def test_review_api_streams_only_redacted_artifacts_and_resumes_after_review(
    tmp_path: Path,
) -> None:
    service = ProviderVisualEvidencePreparationService(
        tmp_path / "provider-evidence",
        redactor=OpenCvPrivacyRedactor(
            detected_boxes=lambda _image: {
                "faces": [(4, 5, 18, 16)],
                "plates": [(28, 30, 24, 10)],
            }
        ),
    )
    pipeline_job_id = uuid4()
    preparation = _prepare(tmp_path, service, pipeline_job_id)
    fake_jobs = _PipelineRegistry(pipeline_job_id, preparation)
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str((tmp_path / "bridge").resolve()),
        provider_visual_evidence_service=service,
        source_candidate_probe=SourceCandidateProbe(None),
    )
    with TestClient(app) as client:
        real_jobs = client.app.state.pipeline_jobs
        client.app.state.pipeline_jobs = fake_jobs
        try:
            base = (
                f"/projects/{DEMO_PROJECT_ID}/pipeline/jobs/"
                f"{pipeline_job_id}/provider-visual-evidence"
            )
            status = client.get(
                (
                    f"{base}/{CANDIDATE_ID}/"
                    f"{preparation['preparation_id']}"
                ),
                headers=_headers(),
            )
            assert status.status_code == 200, status.text
            assert status.json()["state"] == (
                "awaiting_deidentification_review"
            )
            assert status.json()["path_exposed"] is False
            assert (
                status.json()["attachments"][0]["width"],
                status.json()["attachments"][0]["height"],
            ) == (240, 160)

            artifact = client.get(
                (
                    f"{base}/{CANDIDATE_ID}/"
                    f"{preparation['preparation_id']}/artifacts/0"
                ),
                headers=_headers(),
            )
            assert artifact.status_code == 200, artifact.text
            assert artifact.content.startswith(b"\x89PNG\r\n\x1a\n")
            assert artifact.headers["cache-control"] == "no-store"
            assert artifact.headers[
                "x-trafficsupervision-deidentified-review-artifact"
            ] == "1"
            assert "candidate.png" not in artifact.text
            with Image.open(io.BytesIO(artifact.content)) as image:
                assert image.size == (240, 160)

            missing_confirmation = client.post(
                f"{base}/privacy-review",
                headers=_headers(),
                json={
                    "candidate_id": CANDIDATE_ID,
                    "preparation_id": preparation["preparation_id"],
                    "expected_manifest_sha256": preparation[
                        "manifest_sha256"
                    ],
                    "approved": True,
                    "explicit_user_confirmation": False,
                },
            )
            assert missing_confirmation.status_code == 422
            assert service.get(
                project_id=DEMO_PROJECT_ID,
                pipeline_job_id=pipeline_job_id,
                candidate_id=CANDIDATE_ID,
                preparation_id=preparation["preparation_id"],
            )["state"] == "awaiting_deidentification_review"

            reviewed = client.post(
                f"{base}/privacy-review",
                headers=_headers(),
                json={
                    "candidate_id": CANDIDATE_ID,
                    "preparation_id": preparation["preparation_id"],
                    "expected_manifest_sha256": preparation[
                        "manifest_sha256"
                    ],
                    "approved": True,
                    "explicit_user_confirmation": True,
                },
            )
            assert reviewed.status_code == 200, reviewed.text
            payload = reviewed.json()
            assert payload["preparation"]["state"] == (
                "verified_for_provider_transmission"
            )
            assert payload["preparation"]["human_verified"] is True
            assert payload["pipeline_resume_requested"] is True
            assert payload["pipeline_job"]["status"] == "queued"
            assert fake_jobs.resume_calls == 1
        finally:
            client.app.state.pipeline_jobs = real_jobs


def test_review_api_fails_closed_for_wrong_job_and_manifest(
    tmp_path: Path,
) -> None:
    service = ProviderVisualEvidencePreparationService(
        tmp_path / "provider-evidence",
        redactor=OpenCvPrivacyRedactor(
            detected_boxes=lambda _image: {
                "faces": [],
                "plates": [],
            }
        ),
    )
    pipeline_job_id = uuid4()
    preparation = _prepare(tmp_path, service, pipeline_job_id)
    fake_jobs = _PipelineRegistry(pipeline_job_id, preparation)
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str((tmp_path / "bridge").resolve()),
        provider_visual_evidence_service=service,
        source_candidate_probe=SourceCandidateProbe(None),
    )
    with TestClient(app) as client:
        real_jobs = client.app.state.pipeline_jobs
        client.app.state.pipeline_jobs = fake_jobs
        try:
            wrong_job = client.get(
                (
                    f"/projects/{DEMO_PROJECT_ID}/pipeline/jobs/"
                    f"{uuid4()}/provider-visual-evidence/"
                    f"{CANDIDATE_ID}/{preparation['preparation_id']}"
                ),
                headers=_headers(),
            )
            assert wrong_job.status_code == 404

            changed = client.post(
                (
                    f"/projects/{DEMO_PROJECT_ID}/pipeline/jobs/"
                    f"{pipeline_job_id}/provider-visual-evidence/"
                    "privacy-review"
                ),
                headers=_headers(),
                json={
                    "candidate_id": CANDIDATE_ID,
                    "preparation_id": preparation["preparation_id"],
                    "expected_manifest_sha256": "0" * 64,
                    "approved": True,
                    "explicit_user_confirmation": True,
                },
            )
            assert changed.status_code == 409
            assert changed.json()["detail"] == (
                "privacy_review_preparation_changed"
            )
            assert fake_jobs.resume_calls == 0
        finally:
            client.app.state.pipeline_jobs = real_jobs
