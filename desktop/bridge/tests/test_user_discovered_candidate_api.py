from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import create_app
from app.source_candidate_probe import SourceCandidateProbe
from app.store import DEMO_PROJECT_ID
from app.user_discovered_candidate import UserDiscoveredCandidateError


TOKEN = "user-discovered-api-" + ("x" * 48)
ANALYSIS_JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
MODEL_VERSION_ID = UUID("33333333-3333-4333-8333-333333333333")
DATASET_VERSION_ID = UUID("44444444-4444-4444-8444-444444444444")
CANDIDATE_ID = "a" * 20
SOURCE_SHA256 = "1" * 64
QUEUE_REVISION = "2" * 64


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _job(question_revision: str = QUEUE_REVISION) -> dict:
    stage_names = (
        "detection_ocr",
        "sam2_segmentation_tracking",
        "temporal_merge",
        "similar_case_search",
        "vision_proposal",
        "question_queue",
    )
    stages = []
    for name in stage_names:
        checkpoint = {
            "analysis_job_id": str(ANALYSIS_JOB_ID),
            "source_sha256": SOURCE_SHA256,
        }
        if name == "question_queue":
            checkpoint["question_queue_revision"] = question_revision
        stages.append(
            {
                "name": name,
                "status": "success",
                "checkpoint": checkpoint,
            }
        )
    return {
        "project_id": str(DEMO_PROJECT_ID),
        "job_id": str(PIPELINE_JOB_ID),
        "status": "awaiting_review",
        "current_stage": "question_queue",
        "stages": stages,
    }


def _evidence_checkpoint() -> dict:
    return {
        "schema_version": "trafficvisson.user-discovered-candidate.v1",
        "candidate_kind": "user_discovered",
        "candidate_id": CANDIDATE_ID,
        "request_sha256": "3" * 64,
        "project_id": str(DEMO_PROJECT_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "source_sha256": SOURCE_SHA256,
        "dense_index_sha256": "4" * 64,
        "selected_frame_id": "b" * 24,
        "selected_frame_timestamp_seconds": 12.5,
        "selected_frame_sha256": "5" * 64,
        "requested_timestamp_seconds": 12.6,
        "selection": {"kind": "point", "x": 0.4, "y": 0.5},
        "issue_type_proposal": "pothole",
        "review_issue_type": "pothole",
        "promoted_model_version_id": str(MODEL_VERSION_ID),
        "promoted_model_artifact_manifest_sha256": "6" * 64,
        "promoted_model_inference_sha256": "7" * 64,
        "dataset_version_id": str(DATASET_VERSION_ID),
        "dataset_hash": "8" * 64,
        "source_analysis_lineage_sha256": "9" * 64,
        "overlay_id": "ceo_" + ("c" * 32),
        "overlay_manifest_sha256": "a" * 64,
        "discovery_revision": "b" * 64,
        "representative_image": {
            "artifact_id": "image-1",
            "media_type": "image/jpeg",
            "size_bytes": 128,
            "sha256": "c" * 64,
        },
        "preview_clip": {
            "artifact_id": "clip-1",
            "media_type": "video/mp4",
            "size_bytes": 256,
            "sha256": "d" * 64,
            "duration_seconds": 3.0,
            "duration_contract": (
                "candidate_overlay_limit_2_to_5_seconds"
            ),
        },
        "sam2_refinement_request": None,
        "review_only": True,
        "annotations_created": 0,
        "incidents_created": 0,
        "training_members_created": 0,
        "report_truth_records_created": 0,
    }


class _Evidence:
    def __init__(self) -> None:
        self.row = _evidence_checkpoint()
        self.candidate_id = CANDIDATE_ID
        self.overlay_id = self.row["overlay_id"]
        self.source_sha256 = SOURCE_SHA256
        self.request_sha256 = self.row["request_sha256"]

    def to_checkpoint(self) -> dict:
        return dict(self.row)


class _CandidateService:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, request) -> _Evidence:
        self.calls += 1
        assert request.expected_model_version_id == MODEL_VERSION_ID
        assert request.expected_dense_index_sha256 == "4" * 64
        return _Evidence()


class _Labels:
    def __init__(self, answers: list | None = None) -> None:
        self.answers = list(answers or [])

    def latest_answers(self, *_args, **_kwargs) -> list:
        return list(self.answers)

    def resolve_question(
        self, _project_id, analysis_job_id, candidate_id
    ) -> dict:
        return {
            "question_id": str(uuid5(analysis_job_id, candidate_id)),
            "candidate_id": candidate_id,
            "ai_original_candidate": {
                "candidate_id": candidate_id,
            },
        }


class _EvidenceRegistry:
    def bind_overlay(self, *_args) -> None:
        return None


class _OverlayStore:
    def get(self, *_args):
        return SimpleNamespace(
            to_checkpoint=lambda: {
                "overlay_id": "ceo_" + ("c" * 32),
            }
        )


class _QueueService:
    def augment(self, **_kwargs):
        return SimpleNamespace(
            delta_review_event_set={
                "review_event_set_id": "res_" + ("e" * 32),
                "review_event_set_revision": "e" * 64,
                "manifest_sha256": "f" * 64,
            },
            delta_event_binding={
                "event_id": "rve_" + ("f" * 32),
                "event_revision": "0" * 64,
                "representative_candidate_id": CANDIDATE_ID,
            },
        )


class _PipelineJobs:
    def __init__(self, question_revision: str = QUEUE_REVISION) -> None:
        self.job = _job(question_revision)

    def get(self, *_args) -> dict:
        return self.job

    def shutdown(self) -> None:
        return None

    def get_user_discovered_candidate_registration(
        self, *_args, **_kwargs
    ):
        return None

    def register_user_discovered_candidate(
        self, *_args, **kwargs
    ):
        evidence = dict(kwargs["candidate_evidence"])
        question_id = kwargs["question_id"]
        return (
            {
                "candidate_evidence": evidence,
                "question_id": str(question_id),
                "delta_review_event_set": {
                    "review_event_set_id": "res_" + ("e" * 32),
                    "review_event_set_revision": "e" * 64,
                    "manifest_sha256": "f" * 64,
                },
                "delta_event_binding": {
                    "event_id": "rve_" + ("f" * 32),
                    "event_revision": "0" * 64,
                    "representative_candidate_id": CANDIDATE_ID,
                },
                "question_queue_revision": "a" * 64,
                "question_catalog_revision": "b" * 64,
                "question_catalog_collection_revision": "c" * 64,
            },
            False,
        )


def _grant():
    return SimpleNamespace(
        dense_index_sha256="4" * 64,
        promoted_model_version_id=MODEL_VERSION_ID,
        promoted_model_artifact_manifest_sha256="6" * 64,
        promoted_model_inference_sha256="7" * 64,
        source_analysis_lineage_sha256="9" * 64,
    )


def _configure_fakes(
    client: TestClient,
    monkeypatch,
    *,
    question_revision: str = QUEUE_REVISION,
    resolver=None,
    existing_answers: list | None = None,
) -> _CandidateService:
    candidate_service = _CandidateService()
    client.app.state.pipeline_jobs = _PipelineJobs(question_revision)
    client.app.state.user_discovered_candidates = candidate_service
    client.app.state.user_discovered_review_queue = _QueueService()
    client.app.state.user_discovered_survey_resolver = (
        resolver or (lambda *_args: _grant())
    )
    client.app.state.candidate_labels = _Labels(existing_answers)
    client.app.state.candidate_evidence = _EvidenceRegistry()
    client.app.state.candidate_evidence_overlays = _OverlayStore()
    monkeypatch.setattr(
        client.app.state.store,
        "register_label_questions",
        lambda *_args, **_kwargs: {
            "project_id": str(DEMO_PROJECT_ID),
            "analysis_job_id": str(ANALYSIS_JOB_ID),
            "source_sha256": SOURCE_SHA256,
            "question_count": 1,
            "question_set_sha256": "b" * 64,
        },
    )
    return candidate_service


def _request(question_revision: str = QUEUE_REVISION) -> dict:
    return {
        "timestamp_seconds": 12.6,
        "selection": {"kind": "point", "x": 0.4, "y": 0.5},
        "issue_type_proposal": "pothole",
        "request_sam2_mask": True,
        "expected_question_queue_revision": question_revision,
    }


def _url() -> str:
    return (
        f"/projects/{DEMO_PROJECT_ID}/pipeline-jobs/{PIPELINE_JOB_ID}/"
        f"analysis/{ANALYSIS_JOB_ID}/user-discovered-candidates"
    )


class _ReviewFrame:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.selected_frame_sha256 = "5" * 64
        self.selected_frame_id = "b" * 24
        self.selected_frame_timestamp_seconds = 12.5
        self.dense_index_sha256 = "4" * 64

    def to_response(self) -> dict:
        return {
            "schema_version": (
                "trafficvisson.user-discovered-review-frame.v1"
            ),
            "project_id": str(DEMO_PROJECT_ID),
            "analysis_job_id": str(ANALYSIS_JOB_ID),
            "pipeline_job_id": str(PIPELINE_JOB_ID),
            "requested_timestamp_seconds": 12.6,
            "selected_frame_id": self.selected_frame_id,
            "selected_frame_index": 25,
            "selected_frame_timestamp_seconds": (
                self.selected_frame_timestamp_seconds
            ),
            "selected_frame_sha256": self.selected_frame_sha256,
            "selected_frame_size_bytes": self.path.stat().st_size,
            "media_type": "image/jpeg",
            "source_duration_seconds": 81.167,
            "indexed_duration_seconds": 81.0,
            "sampling_fps": 2,
            "dense_index_sha256": self.dense_index_sha256,
            "promoted_model_version_id": str(MODEL_VERSION_ID),
            "source_analysis_lineage_sha256": "9" * 64,
            "review_frame_sha256": "e" * 64,
            "path_exposed": False,
        }


def test_user_discovered_review_frame_covers_full_dense_timeline(
    tmp_path: Path, monkeypatch
) -> None:
    image_path = tmp_path / "review-frame.jpg"
    image_body = b"\xff\xd8review-frame\xff\xd9"
    image_path.write_bytes(image_body)
    review_frame = _ReviewFrame(image_path)

    with TestClient(
        create_app(
            TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(tmp_path.resolve()),
            source_candidate_probe=SourceCandidateProbe(None),
        )
    ) as client:
        _configure_fakes(client, monkeypatch)
        monkeypatch.setattr(
            main_module,
            "resolve_user_discovered_review_frame",
            lambda *_args, **_kwargs: review_frame,
        )
        monkeypatch.setattr(
            main_module,
            "resolve_user_discovered_review_frame_artifact",
            lambda *_args, **_kwargs: review_frame,
        )
        metadata = client.get(
            _url().replace(
                "user-discovered-candidates",
                "user-discovered-review-frame",
            ),
            headers=_headers(),
            params={"timestamp_seconds": 12.6},
        )
        artifact = client.get(
            _url().replace(
                "user-discovered-candidates",
                (
                    "user-discovered-review-frames/"
                    + review_frame.selected_frame_id
                ),
            ),
            headers=_headers(),
        )

    assert metadata.status_code == 200, metadata.text
    body = metadata.json()
    assert body["selected_frame_index"] == 25
    assert body["selected_frame_timestamp_seconds"] == 12.5
    assert body["question_queue_revision"] == QUEUE_REVISION
    assert body["path_exposed"] is False
    assert not any(
        "path" in key.casefold() and key != "path_exposed"
        for key in body
    )
    assert artifact.status_code == 200
    assert artifact.content == image_body
    assert artifact.headers["etag"] == (
        f'"sha256-{review_frame.selected_frame_sha256}"'
    )
    assert artifact.headers[
        "x-trafficsupervision-dense-frame-id"
    ] == review_frame.selected_frame_id


def test_user_discovered_candidate_route_appends_review_only_question(
    tmp_path: Path, monkeypatch
) -> None:
    with TestClient(
        create_app(
            TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(tmp_path.resolve()),
            source_candidate_probe=SourceCandidateProbe(None),
        )
    ) as client:
        service = _configure_fakes(client, monkeypatch)
        response = client.post(
            _url(), headers=_headers(), json=_request()
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert service.calls == 1
    assert body["candidate_kind"] == "user_discovered"
    assert body["review_only"] is True
    assert body["annotations_created"] == 0
    assert body["incidents_created"] == 0
    assert body["training_members_created"] == 0
    assert body["report_truth_records_created"] == 0


def test_user_discovered_candidate_route_rejects_stale_queue_revision(
    tmp_path: Path, monkeypatch
) -> None:
    with TestClient(
        create_app(
            TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(tmp_path.resolve()),
            source_candidate_probe=SourceCandidateProbe(None),
        )
    ) as client:
        service = _configure_fakes(
            client,
            monkeypatch,
            question_revision="3" * 64,
        )
        response = client.post(
            _url(), headers=_headers(), json=_request()
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "question_queue_revision_stale"
    assert service.calls == 0


def test_user_discovered_candidate_route_rejects_training_lineage_video(
    tmp_path: Path, monkeypatch
) -> None:
    def reject_lineage(*_args):
        raise UserDiscoveredCandidateError(
            "model_source_video_reuse_forbidden",
            "survey video belongs to model training lineage",
        )

    with TestClient(
        create_app(
            TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(tmp_path.resolve()),
            source_candidate_probe=SourceCandidateProbe(None),
        )
    ) as client:
        service = _configure_fakes(
            client,
            monkeypatch,
            resolver=reject_lineage,
        )
        response = client.post(
            _url(), headers=_headers(), json=_request()
        )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == "model_source_video_reuse_forbidden"
    )
    assert service.calls == 0


def test_user_discovered_candidate_route_preserves_partial_review_answers(
    tmp_path: Path, monkeypatch
) -> None:
    with TestClient(
        create_app(
            TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            bridge_data_root=str(tmp_path.resolve()),
            source_candidate_probe=SourceCandidateProbe(None),
        )
    ) as client:
        service = _configure_fakes(
            client,
            monkeypatch,
            existing_answers=[
                {
                    "question_id": str(uuid4()),
                    "candidate_id": "f" * 20,
                    "answer": "no",
                }
            ],
        )
        response = client.post(
            _url(), headers=_headers(), json=_request()
        )

    assert response.status_code == 201, response.text
    assert service.calls == 1
    body = response.json()
    assert body["question_queue_revision"] == "a" * 64
    assert body["annotations_created"] == 0
    assert body["incidents_created"] == 0
