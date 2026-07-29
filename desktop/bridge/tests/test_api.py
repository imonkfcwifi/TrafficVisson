from uuid import UUID

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import (
    DEMO_INCIDENT_A,
    DEMO_PROJECT_ID,
    DEMO_QUESTION_A,
    DEMO_TRIP_ID,
)


def test_health_requires_token_and_sets_security_headers(client, auth):
    assert client.get("/health").status_code == 401
    assert (
        client.get(
            "/health", headers={"Authorization": "Bearer " + "wrong" * 12}
        ).status_code
        == 401
    )

    response = client.get("/health", headers=auth)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "trafficvisson-bridge",
        "api_version": "1.0",
        "fixture_mode": True,
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert client.get("/docs", headers=auth).status_code == 404
    assert client.get("/openapi.json", headers=auth).status_code == 404


def test_source_candidates_are_authenticated_read_only_metadata(client, auth):
    assert client.get("/source-candidates").status_code == 401

    response = client.get("/source-candidates", headers=auth)
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == (
        "trafficvisson.source-candidate-catalog.v1"
    )
    assert payload["catalog_mode"] == "read_only_inactive"
    assert payload["network_access_performed"] is False
    assert payload["project_training_linked"] is False
    assert payload["automatic_labels_created"] is False
    assert payload["count"] == 7
    assert [
        candidate["display_name"] for candidate in payload["candidates"]
    ] == [
        "Grounding DINO Tiny",
        "LAION CLIP ViT-B/32 Road Baseline",
        "SAM2.1 Hiera Small",
        "Pothole Detection YOLOv8",
        "Road Issues Detection Dataset",
        "RDD2022 공식 도로 파손 데이터셋",
        "LostAndFound Road Obstacles",
    ]

    grounding, clip, sam, pothole, dataset, rdd2022, lost_and_found = (
        payload["candidates"]
    )
    assert grounding["license_spdx_id"] == "Apache-2.0"
    assert grounding["catalog_status"] == "review_candidate"
    assert grounding["installation_status"] == "not_installed"
    assert grounding["evaluation"] is None
    assert clip["license_spdx_id"] == "MIT"
    assert clip["catalog_status"] == "review_candidate"
    assert clip["installation_status"] == "not_installed"
    assert sam["license_spdx_id"] == "Apache-2.0"
    assert sam["catalog_status"] == "review_candidate"
    assert sam["installation_status"] == "not_installed"
    assert pothole["catalog_status"] == "rejected"
    assert pothole["installation_status"] == "not_installed"
    assert pothole["evaluation"] is None
    assert "OpenRAIL" in pothole["decision_reason"]
    assert dataset["source_type"] == "labeled_dataset"
    assert dataset["license_spdx_id"] == "CC0-1.0"
    assert dataset["installation_status"] == "not_installed"
    assert "9,660" in dataset["decision_reason"]
    assert rdd2022["source_id"] == "sekilab-rdd2022"
    assert rdd2022["license_spdx_id"] == "CC-BY-SA-4.0"
    assert rdd2022["intended_use"] == "evaluation_reference"
    assert rdd2022["installation_status"] == "not_installed"
    assert "Pascal VOC" in rdd2022["decision_reason"]
    assert lost_and_found["source_id"] == (
        "daimler-lost-and-found-road-obstacles"
    )
    assert lost_and_found["license_spdx_id"] == "LicenseRef-NeedsReview"
    assert lost_and_found["catalog_status"] == "review_candidate"
    assert lost_and_found["installation_status"] == "not_installed"
    assert lost_and_found["rights_review_required"] is True
    assert "training blocked" in lost_and_found["decision_reason"]

    assert "verified_inactive" not in response.text
    assert "project_id" not in response.text
    assert "registration_status" not in response.text
    for candidate in payload["candidates"]:
        assert candidate["automatic_download"] is False
        assert candidate["automatic_activation"] is False
        assert candidate["weight_execution_enabled"] is False
        assert candidate["project_training_linked"] is False
        assert candidate["human_review_required"] is True
        assert candidate["rights_review_required"] is True


def test_source_candidate_endpoint_uses_injected_local_observation(
    token, auth
):
    class StaticProbe:
        def snapshot(self):
            return {
                "schema_version": (
                    "trafficvisson.local-source-observation.v1"
                ),
                "sources": {
                    "idea-research-grounding-dino-tiny": {
                        "state": "verified_snapshot",
                        "evaluation": {
                            "evaluated_frame_count": 30,
                            "proposal_frame_count": 25,
                            "proposal_count": 47,
                            "false_positive_count": None,
                            "verdict": "review_only",
                        },
                    },
                    "peterhdd-pothole-yolov8-local-cache": {
                        "state": "verified_snapshot",
                        "evaluation": {
                            "evaluated_frame_count": 349,
                            "proposal_frame_count": 19,
                            "proposal_count": 19,
                            "false_positive_count": 19,
                            "verdict": "rejected",
                        },
                    },
                },
            }

    with TestClient(
        create_app(
            token,
            fixture_mode=True,
            allow_test_client=True,
            source_candidate_probe=StaticProbe(),
        )
    ) as injected_client:
        payload = injected_client.get(
            "/source-candidates",
            headers=auth,
        ).json()

    grounding = payload["candidates"][0]
    pothole = payload["candidates"][3]
    assert grounding["installation_status"] == "installed_validation_only"
    assert grounding["observation_basis"] == "verified_local_snapshot"
    assert grounding["evaluation"]["proposal_count"] == 47
    assert pothole["installation_status"] == "local_cache_quarantined"
    assert pothole["evaluation"]["false_positive_count"] == 19


def test_unknown_fields_and_untrusted_hosts_are_rejected(client, auth):
    response = client.post(
        "/projects",
        headers=auth,
        json={"name": "프로젝트", "unexpected": "no"},
    )
    assert response.status_code == 422

    response = client.get(
        "/health", headers={**auth, "Host": "attacker.example"}
    )
    assert response.status_code == 400


def test_project_data_does_not_cross_project_boundaries(client, auth):
    created = client.post(
        "/projects",
        headers=auth,
        json={"name": "별도 프로젝트", "description": "격리 확인"},
    )
    assert created.status_code == 201
    project_id = created.json()["id"]

    questions = client.get(
        f"/projects/{project_id}/questions", headers=auth
    )
    assert questions.status_code == 200
    assert questions.json()["questions"] == []

    history = client.get(
        f"/projects/{project_id}/history",
        headers=auth,
        params={"trip_id": str(DEMO_TRIP_ID)},
    )
    assert history.status_code == 404

    mismatched_import = client.post(
        f"/projects/{project_id}/imports/inspect",
        headers=auth,
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "source_type": "local_selection",
            "local_selection_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    )
    assert mismatched_import.status_code == 409


def test_import_contract_accepts_opaque_selection_and_rejects_ssrf(client, auth):
    project_id = str(DEMO_PROJECT_ID)
    local_selection_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    inspected = client.post(
        f"/projects/{project_id}/imports/inspect",
        headers=auth,
        json={
            "project_id": project_id,
            "source_type": "local_selection",
            "local_selection_id": local_selection_id,
        },
    )
    assert inspected.status_code == 200
    assert inspected.json()["source_display"] == "선택한 로컬 영상"
    assert "path" not in inspected.text.lower()

    denied = client.post(
        f"/projects/{project_id}/imports/inspect",
        headers=auth,
        json={
            "project_id": project_id,
            "source_type": "direct_url",
            "source_url": "http://127.0.0.1/admin",
        },
    )
    assert denied.status_code == 422
    assert "special-use" in denied.json()["detail"].lower()

    wrong_provider = client.post(
        f"/projects/{project_id}/imports/inspect",
        headers=auth,
        json={
            "project_id": project_id,
            "source_type": "youtube",
            "source_url": "https://example.com/video",
        },
    )
    assert wrong_provider.status_code == 422

    no_rights = client.post(
        f"/projects/{project_id}/imports",
        headers=auth,
        json={
            "project_id": project_id,
            "inspected_source_id": inspected.json()["inspected_source_id"],
            "rights_confirmed": False,
        },
    )
    assert no_rights.status_code == 412

    queued = client.post(
        f"/projects/{project_id}/imports",
        headers=auth,
        json={
            "project_id": project_id,
            "inspected_source_id": inspected.json()["inspected_source_id"],
            "rights_confirmed": True,
        },
    )
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"


def test_question_policy_answer_conversion_and_history(client, auth):
    project_id = str(DEMO_PROJECT_ID)
    quick = client.get(
        f"/projects/{project_id}/questions",
        headers=auth,
        params={"policy_mode": "quick_review"},
    )
    assert quick.status_code == 200
    assert quick.json()["count"] == 2
    assert any(
        item["emergency_review_required"] for item in quick.json()["questions"]
    )

    missing_geometry = client.post(
        f"/projects/{project_id}/questions/{DEMO_QUESTION_A}/answer",
        headers=auth,
        json={
            "project_id": project_id,
            "question_id": str(DEMO_QUESTION_A),
            "answer": "edit",
        },
    )
    assert missing_geometry.status_code == 422

    answer = client.post(
        f"/projects/{project_id}/questions/{DEMO_QUESTION_A}/answer",
        headers=auth,
        json={
            "project_id": project_id,
            "question_id": str(DEMO_QUESTION_A),
            "answer": "edit",
            "selected_class": "vegetation_occlusion",
            "selected_action": "삭초 필요",
            "final_geometry": {
                "kind": "box",
                "points": [[100.0, 120.0], [250.0, 280.0]],
            },
        },
    )
    assert answer.status_code == 200
    assert answer.json()["annotation_state"] == "approved"
    assert answer.json()["include_in_training"] is True
    assert answer.json()["final_geometry"]["kind"] == "box"

    history = client.get(
        f"/projects/{project_id}/history",
        headers=auth,
        params={"trip_id": str(DEMO_TRIP_ID)},
    )
    assert history.status_code == 200
    payload = history.json()
    assert len(payload["route_points"]) >= 3
    assert len(payload["incidents"]) == 2
    assert all("evidence" in item for item in payload["incidents"])


def test_provider_responses_never_include_credentials(client, auth):
    response = client.get("/providers", headers=auth)
    assert response.status_code == 200
    payload = response.json()
    assert {item["provider"] for item in payload["providers"]} == {
        "codex",
        "claude",
        "local",
    }

    forbidden_key_parts = {"token", "secret", "credential", "auth_file", "path"}

    def walk(value):
        if isinstance(value, dict):
            for key, nested in value.items():
                assert not any(part in key.lower() for part in forbidden_key_parts)
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(payload)
    login = client.post("/providers/codex/login", headers=auth, json={})
    assert login.status_code == 200
    assert login.json()["state"] == "external_cli_owned"
    assert client.post("/providers/local/login", headers=auth, json={}).status_code == 404


def test_report_plan_is_schema_limited_and_export_is_allowlisted(client, auth):
    project_id = str(DEMO_PROJECT_ID)
    draft = client.post(
        f"/projects/{project_id}/reports/draft",
        headers=auth,
        json={
            "project_id": project_id,
            "provider": "codex",
            "incident_ids": [str(DEMO_INCIDENT_A)],
            "consent": True,
        },
    )
    assert draft.status_code == 200

    plan = client.post(
        f"/projects/{project_id}/reports/plan",
        headers=auth,
        json={
            "project_id": project_id,
            "draft_id": draft.json()["draft_id"],
            "document_snapshot_id": "snapshot:demo",
        },
    )
    assert plan.status_code == 200
    edit_plan = plan.json()["plan"]
    assert edit_plan["operations"][0]["type"] == "fill_text"

    malicious = dict(edit_plan)
    malicious["operations"] = [
        {
            **edit_plan["operations"][0],
            "type": "run_macro",
            "target": "C:\\Windows\\System32",
        }
    ]
    denied = client.post(
        f"/projects/{project_id}/reports/apply",
        headers=auth,
        json={"project_id": project_id, "plan": malicious},
    )
    assert denied.status_code == 422

    applied = client.post(
        f"/projects/{project_id}/reports/apply",
        headers=auth,
        json={"project_id": project_id, "plan": edit_plan},
    )
    assert applied.status_code == 200
    assert applied.json()["state"] == "preview_validation_only"

    bad_export = client.post(
        f"/projects/{project_id}/exports",
        headers=auth,
        json={
            "project_id": project_id,
            "artifacts": ["raw_detections", "arbitrary_file"],
        },
    )
    assert bad_export.status_code == 422

    exported = client.post(
        f"/projects/{project_id}/exports",
        headers=auth,
        json={
            "project_id": project_id,
            "artifacts": ["raw_detections", "manifest"],
        },
    )
    assert exported.status_code == 200
    assert [item["file_name"] for item in exported.json()["artifacts"]] == [
        "raw_detections.csv",
        "manifest.json",
    ]
    assert "destination" not in exported.text.lower()


def test_request_body_limit(client, auth):
    response = client.post(
        "/projects",
        headers={**auth, "Content-Type": "application/json"},
        content=b'{"name":"' + b"x" * (1024 * 1024) + b'"}',
    )
    assert response.status_code == 413
