from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.sam2_refinement import (
    Sam2MaskRefinementService,
    Sam2RefinementError,
    Sam2RefinementNotFound,
)
from app.store import DEMO_PROJECT_ID
from conftest import TEST_TOKEN
from test_analysis_jobs import (
    _auth,
    _complete_analysis,
    _limits,
    _make_video,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_PROJECT_ID = UUID("99999999-9999-4999-8999-999999999999")
JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
CANDIDATE_ID = "0123456789abcdefabcd"
QUESTION_ID = uuid5(JOB_ID, CANDIDATE_ID)
MANIFEST = {
    "pack_id": "sam2.1-hiera-small",
    "pack_version": "1.0.0",
    "manifest_sha256": "a" * 64,
}


class FakeSam2Adapter:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"state": "ready", "model_manifest": MANIFEST}

    def refine(self, *, image_path: Path, point: tuple[float, float]):
        self.calls.append((image_path, point))
        return {
            "polygon": [
                [0.20, 0.30],
                [0.70, 0.30],
                [0.72, 0.75],
                [0.18, 0.72],
            ],
            "tracked_frame_refs": [
                "track_frame_before_01",
                "track_frame_after_01",
            ],
        }


def build_service(tmp_path: Path, *, adapter):
    image_path = (tmp_path / "private-candidate-image.jpg").resolve()
    image_bytes = b"\xff\xd8\xff" + b"candidate" * 16
    image_path.write_bytes(image_bytes)
    side_effects = {
        "annotations": 0,
        "incidents": 0,
        "training_members": 0,
    }

    def resolve_question(project_id, analysis_job_id, candidate_id):
        if (
            project_id != PROJECT_ID
            or analysis_job_id != JOB_ID
            or candidate_id != CANDIDATE_ID
        ):
            raise LookupError("candidate is outside this project")
        return {
            "project_id": str(PROJECT_ID),
            "analysis_job_id": str(JOB_ID),
            "candidate_id": CANDIDATE_ID,
            "question_id": str(QUESTION_ID),
        }

    def resolve_artifact(project_id, analysis_job_id, candidate_id):
        assert project_id == PROJECT_ID
        assert analysis_job_id == JOB_ID
        assert candidate_id == CANDIDATE_ID
        return SimpleNamespace(
            path=image_path,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
        )

    return (
        Sam2MaskRefinementService(
            adapter=adapter,
            artifact_resolver=resolve_artifact,
            question_resolver=resolve_question,
        ),
        image_path,
        side_effects,
    )


def identity():
    return {
        "project_id": PROJECT_ID,
        "analysis_job_id": JOB_ID,
        "question_id": QUESTION_ID,
        "candidate_id": CANDIDATE_ID,
    }


def test_unavailable_model_is_distinct_and_creates_no_refinement(tmp_path):
    service, image_path, side_effects = build_service(tmp_path, adapter=None)

    capability = service.capability(**identity())
    result = service.refine(**identity(), click_x=0.4, click_y=0.6)

    assert capability["state"] == "model_unavailable"
    assert capability["click_prompt_enabled"] is False
    assert result["status"] == "model_unavailable"
    assert result["error_code"] == "model_pack_not_configured"
    assert result["polygon"] is None
    assert result["mask_artifact_ref"] is None
    assert service.record_count() == 0
    assert side_effects == {
        "annotations": 0,
        "incidents": 0,
        "training_members": 0,
    }
    assert str(image_path) not in repr(capability)
    assert str(image_path) not in repr(result)


def test_injected_adapter_returns_bounded_path_free_review_proposal(tmp_path):
    adapter = FakeSam2Adapter()
    service, image_path, side_effects = build_service(
        tmp_path,
        adapter=adapter,
    )

    result = service.refine(**identity(), click_x=0.45, click_y=0.61)

    assert result["status"] == "refined"
    assert result["refinement_id"].startswith("smr_")
    assert result["model_manifest"] == MANIFEST
    assert result["input_sha256"] != result["output_sha256"]
    assert result["normalized_box"] == pytest.approx(
        {"x": 0.18, "y": 0.30, "width": 0.54, "height": 0.45}
    )
    assert result["requires_user_confirmation"] is True
    assert result["automatic_annotation_allowed"] is False
    assert result["automatic_incident_creation_allowed"] is False
    assert result["training_membership_allowed"] is False
    assert adapter.calls == [(image_path, (0.45, 0.61))]
    assert str(image_path) not in repr(result)
    assert service.record_count() == 1
    assert side_effects == {
        "annotations": 0,
        "incidents": 0,
        "training_members": 0,
    }

    resolved = service.resolve_for_answer(
        **identity(),
        refinement_id=result["refinement_id"],
    )
    assert resolved["output_sha256"] == result["output_sha256"]


@pytest.mark.parametrize(
    ("click_x", "click_y"),
    [
        (-0.001, 0.5),
        (1.001, 0.5),
        (0.5, float("nan")),
        (True, 0.5),
    ],
)
def test_click_coordinates_must_be_finite_and_normalized(
    tmp_path,
    click_x,
    click_y,
):
    service, _, _ = build_service(tmp_path, adapter=FakeSam2Adapter())
    with pytest.raises(Sam2RefinementError):
        service.refine(
            **identity(),
            click_x=click_x,
            click_y=click_y,
        )
    assert service.record_count() == 0


def test_cross_project_or_question_identity_cannot_reuse_refinement(tmp_path):
    service, _, _ = build_service(tmp_path, adapter=FakeSam2Adapter())
    result = service.refine(**identity(), click_x=0.5, click_y=0.5)

    with pytest.raises(Sam2RefinementNotFound):
        service.resolve_for_answer(
            project_id=OTHER_PROJECT_ID,
            analysis_job_id=JOB_ID,
            question_id=QUESTION_ID,
            candidate_id=CANDIDATE_ID,
            refinement_id=result["refinement_id"],
        )
    with pytest.raises(Sam2RefinementNotFound):
        service.resolve_for_answer(
            project_id=PROJECT_ID,
            analysis_job_id=JOB_ID,
            question_id=uuid4(),
            candidate_id=CANDIDATE_ID,
            refinement_id=result["refinement_id"],
        )


def test_adapter_cannot_return_unbounded_or_degenerate_geometry(tmp_path):
    class BadAdapter(FakeSam2Adapter):
        def refine(self, *, image_path: Path, point: tuple[float, float]):
            return {"polygon": [[0.2, 0.2], [0.2, 0.2], [0.2, 0.2]]}

    service, _, _ = build_service(tmp_path, adapter=BadAdapter())
    with pytest.raises(Sam2RefinementError, match="degenerate"):
        service.refine(**identity(), click_x=0.5, click_y=0.5)
    assert service.record_count() == 0


def test_adapter_output_cannot_smuggle_a_filesystem_path(tmp_path):
    class PathLeakingAdapter(FakeSam2Adapter):
        def refine(self, *, image_path: Path, point: tuple[float, float]):
            return {
                "polygon": [[0.2, 0.2], [0.6, 0.2], [0.6, 0.6]],
                "mask_path": "C:\\private\\mask.png",
            }

    service, _, _ = build_service(
        tmp_path,
        adapter=PathLeakingAdapter(),
    )
    with pytest.raises(Sam2RefinementError, match="unsupported field"):
        service.refine(**identity(), click_x=0.5, click_y=0.5)
    assert service.record_count() == 0


def test_api_refinement_requires_reconfirmation_before_label_projection(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    adapter = FakeSam2Adapter()
    with TestClient(
        create_app(
            TEST_TOKEN,
            fixture_mode=True,
            allow_test_client=True,
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            bridge_data_root=str(tmp_path / "bridge-data"),
            candidate_extraction_limits=_limits(),
            sam2_refinement_adapter=adapter,
        )
    ) as client:
        terminal = _complete_analysis(client, source)
        job_id = terminal["job_id"]
        candidate_id = terminal["result"]["candidates"][0]["candidate_id"]
        questions_path = (
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
            "/label-questions"
        )
        questions = client.get(questions_path, headers=_auth())
        assert questions.status_code == 200, questions.text
        question = questions.json()["questions"][0]
        assert question["status"] == "unanswered"

        identity_payload = {
            "project_id": str(DEMO_PROJECT_ID),
            "analysis_job_id": job_id,
            "question_id": question["question_id"],
            "candidate_id": candidate_id,
        }
        route = (
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}/candidates/"
            f"{candidate_id}"
        )
        capability = client.post(
            f"{route}/mask-refinement-capability",
            headers=_auth(),
            json=identity_payload,
        )
        assert capability.status_code == 200, capability.text
        assert capability.json()["state"] == "ready"

        refined = client.post(
            f"{route}/mask-refinements",
            headers=_auth(),
            json={**identity_payload, "click": {"x": 0.45, "y": 0.61}},
        )
        assert refined.status_code == 200, refined.text
        refinement = refined.json()
        assert refinement["status"] == "refined"
        assert str(source.resolve()) not in refined.text
        assert "private-candidate-image" not in refined.text

        # Refinement is a proposal only: no label projection exists yet.
        still_unanswered = client.get(questions_path, headers=_auth())
        assert still_unanswered.status_code == 200
        assert still_unanswered.json()["questions"][0]["status"] == "unanswered"
        assert still_unanswered.json()["answered_count"] == 0

        cross_project = client.post(
            f"{route}/mask-refinements",
            headers=_auth(),
            json={
                **identity_payload,
                "project_id": str(OTHER_PROJECT_ID),
                "click": {"x": 0.45, "y": 0.61},
            },
        )
        assert cross_project.status_code == 409

        answered = client.post(
            f"{route}/answer",
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "question_id": question["question_id"],
                "mask_refinement_id": refinement["refinement_id"],
                "answer": "edit",
                "selected_issue_type": "pothole",
                "severity": "moderate",
                "maintenance_action": "노면 보수",
            },
        )
        assert answered.status_code == 201, answered.text
        assert answered.json()["partition"] == "approved"
