from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.command_proposal_generation import (
    CommandProposalGenerationResult,
)
from app.main import (
    _AUTO_LABEL_DISPLAY_NAMES,
    _MAINTENANCE_JUDGMENT_TYPES,
    create_app,
)
from app.store import DEMO_PROJECT_ID


TOKEN = "t" * 64
JOB_ID = UUID("77777777-7777-4777-8777-777777777777")
CANDIDATE_ID = "0123456789abcdefabcd"
IMAGE_SHA = "1" * 64
CANDIDATE_SHA = "2" * 64


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _job() -> dict:
    return {
        "job_id": str(JOB_ID),
        "status": "completed",
        "result": {
            "candidate_count": 1,
            "candidates": [{"candidate_id": CANDIDATE_ID}],
        },
    }


def _proposal() -> dict:
    return {
        "schema_version": "trafficvisson.experimental-candidate-proposal.v1",
        "run_id": str(uuid4()),
        "project_id": str(DEMO_PROJECT_ID),
        "analysis_job_id": str(JOB_ID),
        "candidate_id": CANDIDATE_ID,
        "created_at": "2026-07-28T12:00:00.000Z",
        "status": "classified",
        "review_only": True,
        "automatic_labeling_allowed": False,
        "score_semantics": "model_score_not_probability",
        "model": {
            "provider": "onnx_yolo_v8",
            "pack_id": "trafficvisson-road-test",
            "pack_version": "0.1.0",
            "manifest_sha256": "3" * 64,
        },
        "dataset": None,
        "evaluation": {
            "evaluation_id": "api-auto-label-test",
            "producer": "trafficvisson-test",
            "producer_version": "1.0.0",
        },
        "source": {
            "representative_image_sha256": IMAGE_SHA,
            "analysis_candidate_sha256": CANDIDATE_SHA,
        },
        "predictions": [
            {
                "issue_type": "pothole",
                "relative_score": None,
                "model_score": 0.81,
                "box": {
                    "x": 0.2,
                    "y": 0.5,
                    "width": 0.2,
                    "height": 0.1,
                },
                "source_label": "포트홀 선라벨",
                "calibration": "model_score",
            }
        ],
    }


def test_auto_label_api_uses_only_canonical_issue_labels_and_review_sets():
    assert _AUTO_LABEL_DISPLAY_NAMES == {
        "pothole": "포트홀",
        "road_surface_damage": "노면 파손",
        "road_debris": "낙하물",
        "median_barrier_damage": "중앙분리대 파손",
        "sign_damage": "표지판 파손",
        "sign_vegetation_occlusion": "표지판 식생 가림",
        "roadside_mowing_required": "삭초 필요",
        "lane_marking_faded": "차선 재도색 필요",
        "colored_guidance_faded": "색깔유도선 재도색 필요",
        "guide_sign_identity": "이정표 식별",
        "tunnel_identity": "터널명 식별",
        "tunnel_name_occlusion": "터널명 식생 가림",
        "other": "사용자 지정 도로 문제",
    }
    assert _MAINTENANCE_JUDGMENT_TYPES == {
        "road_surface_damage",
        "median_barrier_damage",
        "sign_damage",
        "sign_vegetation_occlusion",
        "roadside_mowing_required",
        "lane_marking_faded",
        "colored_guidance_faded",
        "tunnel_name_occlusion",
    }
    assert "sign_identity" not in _AUTO_LABEL_DISPLAY_NAMES
    assert "color_guidance_line_faded" not in _MAINTENANCE_JUDGMENT_TYPES


@pytest.mark.parametrize(
    (
        "ingress_issue_type",
        "command",
        "canonical_issue_type",
        "display_name",
        "ocr_required",
        "relationship_required",
        "maintenance_required",
    ),
    [
        (
            "sign_identity",
            "이정표 글자를 읽고 검증",
            "guide_sign_identity",
            "이정표 식별",
            True,
            False,
            False,
        ),
        (
            "color_guidance_line_faded",
            "색깔유도선이 지워졌는지",
            "colored_guidance_faded",
            "색깔유도선 재도색 필요",
            False,
            False,
            True,
        ),
        (
            "sign_damage",
            "파손된 표지판 확인",
            "sign_damage",
            "표지판 파손",
            False,
            False,
            True,
        ),
        (
            "tunnel_name_occlusion",
            "터널 이름이 수풀에 가려졌는지",
            "tunnel_name_occlusion",
            "터널명 식생 가림",
            True,
            True,
            True,
        ),
    ],
)
def test_compile_endpoint_emits_canonical_issue_contract(
    tmp_path,
    ingress_issue_type,
    command,
    canonical_issue_type,
    display_name,
    ocr_required,
    relationship_required,
    maintenance_required,
):
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
    )
    with TestClient(app) as client:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/auto-labeling/compile",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "command": command,
                "issue_type": ingress_issue_type,
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["issue_type"] == canonical_issue_type
    assert payload["display_name_ko"] == display_name
    assert payload["capabilities"]["ocr_required"] is ocr_required
    assert (
        payload["capabilities"]["relationship_reasoning_required"]
        is relationship_required
    )
    assert (
        payload["capabilities"]["maintenance_judgment_required"]
        is maintenance_required
    )


def test_compile_and_run_use_existing_proposals_without_training_labels(
    tmp_path,
):
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
    )
    app.state.analysis_jobs.get = lambda project_id, job_id: _job()
    app.state.analysis_jobs.list = lambda project_id, limit=100: [_job()]
    app.state.experimental_proposals.latest_for_job = (
        lambda project_id, job_id, **kwargs: (
            {}
            if kwargs.get("evaluation_id")
            else {CANDIDATE_ID: _proposal()}
        )
    )

    with TestClient(app) as client:
        compiled = client.post(
            f"/projects/{DEMO_PROJECT_ID}/auto-labeling/compile",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "command": "포트홀을 찾아줘",
            },
        )
        assert compiled.status_code == 200, compiled.text
        recipe = compiled.json()
        assert recipe["issue_type"] == "pothole"
        assert recipe["default_mode"] == "proposal_only"
        assert len(recipe["negative_prompts"]) >= 2

        executed = client.post(
            f"/projects/{DEMO_PROJECT_ID}/auto-labeling/runs",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "job_id": str(JOB_ID),
                "recipe_id": recipe["recipe_id"],
                "command": "포트홀을 찾아줘",
                "issue_type": "pothole",
                "scope": "current_job",
                "mode": "assisted_auto",
                "user_confirmed": True,
            },
        )
        assert executed.status_code == 201, executed.text
        payload = executed.json()
        assert payload["status"] == "completed"
        assert payload["candidates_evaluated"] == 1
        assert payload["proposed_count"] == 1
        assert payload["auto_accepted_count"] == 0
        assert payload["review_required_count"] == 1
        assert payload["training_labels_created"] == 0
        assert payload["mode_requested"] == "assisted_auto"
        assert payload["mode_applied"] == "proposal_only"
        assert payload["first_match"] == {
            "analysis_job_id": str(JOB_ID),
            "candidate_id": CANDIDATE_ID,
            "issue_type": "pothole",
            "review_status": "pending",
            "decision_hash": payload["first_match"]["decision_hash"],
        }
        assert len(payload["first_match"]["decision_hash"]) == 64
        recorded = app.state.auto_label_runs.get(
            DEMO_PROJECT_ID,
            UUID(payload["run_id"]),
        )
        assert recorded["recipe_id"] == recipe["recipe_id"]
        assert recorded["mode_requested"] == "assisted_auto"
        assert recorded["mode_applied"] == "proposal_only"
        assert recorded["match_count"] == 1
        assert recorded["matches"][0]["include_in_training"] is False


def test_auto_label_api_rejects_unknown_command_and_changed_recipe(tmp_path):
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
    )
    app.state.analysis_jobs.get = lambda project_id, job_id: _job()
    app.state.experimental_proposals.latest_for_job = (
        lambda project_id, job_id, **kwargs: (
            {}
            if kwargs.get("evaluation_id")
            else {CANDIDATE_ID: _proposal()}
        )
    )
    with TestClient(app) as client:
        unknown = client.post(
            f"/projects/{DEMO_PROJECT_ID}/auto-labeling/compile",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "command": "무언가 이상한 것을 알아서 찾아줘",
            },
        )
        assert unknown.status_code == 422

        changed = client.post(
            f"/projects/{DEMO_PROJECT_ID}/auto-labeling/runs",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "job_id": str(JOB_ID),
                "recipe_id": "f" * 64,
                "command": "포트홀을 찾아줘",
                "issue_type": "pothole",
                "scope": "current_job",
                "mode": "proposal_only",
                "user_confirmed": True,
            },
        )
        assert changed.status_code == 409


def test_auto_label_run_persists_an_empty_audit_when_model_is_unavailable(
    tmp_path,
):
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
    )
    app.state.analysis_jobs.get = lambda project_id, job_id: _job()
    app.state.experimental_proposals.latest_for_job = (
        lambda project_id, job_id, **kwargs: {}
    )
    with TestClient(app) as client:
        compiled = client.post(
            f"/projects/{DEMO_PROJECT_ID}/auto-labeling/compile",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "command": "포트홀을 찾아줘",
            },
        ).json()
        executed = client.post(
            f"/projects/{DEMO_PROJECT_ID}/auto-labeling/runs",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "job_id": str(JOB_ID),
                "recipe_id": compiled["recipe_id"],
                "command": "포트홀을 찾아줘",
                "issue_type": "pothole",
                "scope": "current_job",
                "mode": "proposal_only",
                "user_confirmed": True,
            },
        )
        assert executed.status_code == 201, executed.text
        payload = executed.json()
        assert payload["status"] == "model_unavailable"
        assert payload["first_match"] is None
        recorded = app.state.auto_label_runs.get(
            DEMO_PROJECT_ID,
            UUID(payload["run_id"]),
        )
        assert recorded["match_count"] == 0
        assert recorded["matches"] == []


def test_auto_label_run_generates_and_persists_review_only_sidecars(tmp_path):
    class FakeCommandGenerator:
        def generate(
            self,
            *,
            project_id,
            analysis_job_id,
            recipe_id,
            command_plan,
            candidates,
            resolve_image,
        ):
            del project_id, analysis_job_id, resolve_image
            assert command_plan["issue_type"] == "pothole"
            assert [item["candidate_id"] for item in candidates] == [
                CANDIDATE_ID
            ]
            return CommandProposalGenerationResult(
                available=True,
                generated=True,
                message_ko="로컬 명령형 AI 테스트 실행을 완료했습니다.",
                evaluated_candidate_count=1,
                model={
                    "provider": "transformers_grounding_dino",
                    "pack_id": "trafficvisson-grounding-dino-test",
                    "pack_version": "0.1.0",
                    "manifest_sha256": "3" * 64,
                },
                evaluation={
                    "evaluation_id": f"cmd-{recipe_id}",
                    "producer": "trafficvisson-test-runner",
                    "producer_version": "0.1.0",
                },
                proposals=(
                    {
                        "candidate_id": CANDIDATE_ID,
                        "status": "classified",
                        "predictions": [
                            {
                                "issue_type": "pothole",
                                "relative_score": None,
                                "model_score": 0.77,
                                "box": {
                                    "x": 0.2,
                                    "y": 0.5,
                                    "width": 0.2,
                                    "height": 0.1,
                                },
                                "source_label": "pothole",
                                "calibration": "model_score",
                            }
                        ],
                    },
                ),
            )

    completed_job = {
        "job_id": str(JOB_ID),
        "status": "completed",
        "result": {
            "candidate_count": 1,
            "candidates": [
                {
                    "candidate_id": CANDIDATE_ID,
                    "representative_image": {
                        "sha256": IMAGE_SHA,
                    },
                }
            ],
        },
    }
    app = create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
        command_proposal_generator=FakeCommandGenerator(),
    )
    app.state.analysis_jobs.get = (
        lambda project_id, job_id: completed_job
    )
    with TestClient(app) as client:
        recipe = client.post(
            f"/projects/{DEMO_PROJECT_ID}/auto-labeling/compile",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "command": "포트홀을 찾아줘",
            },
        ).json()
        executed = client.post(
            f"/projects/{DEMO_PROJECT_ID}/auto-labeling/runs",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "job_id": str(JOB_ID),
                "recipe_id": recipe["recipe_id"],
                "command": "포트홀을 찾아줘",
                "issue_type": "pothole",
                "scope": "current_job",
                "mode": "assisted_auto",
                "user_confirmed": True,
            },
        )
    assert executed.status_code == 201, executed.text
    payload = executed.json()
    assert payload["status"] == "completed"
    assert payload["proposal_origin"] == "local_model_generated"
    assert payload["proposal_generation_status"] == "generated"
    assert payload["candidates_evaluated"] == 1
    assert payload["proposed_count"] == 1
    assert payload["auto_accepted_count"] == 0
    assert payload["review_required_count"] == 1
    assert payload["training_labels_created"] == 0
    sidecars = app.state.experimental_proposals.latest_for_job(
        DEMO_PROJECT_ID,
        JOB_ID,
        evaluation_id=f"cmd-{recipe['recipe_id']}",
    )
    assert list(sidecars) == [CANDIDATE_ID]
    sidecar = sidecars[CANDIDATE_ID]
    assert sidecar["review_only"] is True
    assert sidecar["automatic_labeling_allowed"] is False
    assert sidecar["model"]["provider"] == "transformers_grounding_dino"
