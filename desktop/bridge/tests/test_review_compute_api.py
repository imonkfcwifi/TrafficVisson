from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import DEMO_PROJECT_ID


TOKEN = "t" * 64
OTHER_PROJECT_ID = "22222222-2222-4222-8222-222222222222"
COMPLETED_JOB_ID = "33333333-3333-4333-8333-333333333333"
COMPLETED_SOURCE_SHA256 = "a" * 64


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _app(tmp_path):
    return create_app(
        TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        media_stability_min_interval_seconds=0,
    )


def _state_snapshot(app) -> dict:
    return deepcopy(
        {
            "project": app.state.store.get_project(DEMO_PROJECT_ID),
            "label_stats": app.state.store.current_label_stats(
                DEMO_PROJECT_ID
            ),
            "incidents": app.state.store.list_incidents(
                DEMO_PROJECT_ID,
                include_inactive=True,
            ),
            "datasets": app.state.store.list_dataset_versions(
                DEMO_PROJECT_ID
            ),
        }
    )


def _forbid_mutating_services(app) -> None:
    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("review-only computation attempted a state write")

    for service, method_names in (
        (
            app.state.store,
            (
                "answer_question",
                "create_dataset_version",
                "project_label_record",
                "project_label_records",
            ),
        ),
        (app.state.candidate_labels, ("record_answer",)),
        (app.state.experimental_proposals, ("record_run",)),
        (app.state.auto_label_runs, ("record_run",)),
    ):
        for method_name in method_names:
            setattr(service, method_name, forbidden)


def _temporal_detection(
    detection_id: str,
    timestamp_seconds: float,
    issue_type: str,
) -> dict:
    return {
        "detection_id": detection_id,
        "timestamp_seconds": timestamp_seconds,
        "issue_type": issue_type,
        "score": 0.8,
        "review_only": True,
        "automatic_labeling_allowed": False,
        "track_hint": "track-1",
        "score_semantics": "model_score_not_probability",
    }


def _temporal_request(
    *,
    analysis_job_id: str = COMPLETED_JOB_ID,
    source_sha256: str = COMPLETED_SOURCE_SHA256,
) -> dict:
    return {
        "project_id": str(DEMO_PROJECT_ID),
        "detections": [
            _temporal_detection("det-a", 1.0, "pothole"),
        ],
        "provenance": {
            "project_id": str(DEMO_PROJECT_ID),
            "analysis_job_id": analysis_job_id,
            "source_file_sha256": source_sha256,
            "model": {
                "provider": "local_review_only",
                "version": "road-model@0.1.0",
            },
        },
    }


def test_temporal_review_api_normalizes_aliases_without_state_changes(
    tmp_path,
):
    app = _app(tmp_path)
    app.state.analysis_jobs.get = lambda project_id, job_id: {
        "project_id": str(project_id),
        "job_id": str(job_id),
        "status": "completed",
        "source_sha256": COMPLETED_SOURCE_SHA256,
        "result": {"candidates": []},
    }
    before = _state_snapshot(app)
    _forbid_mutating_services(app)

    with TestClient(app) as client:
        response = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/review/"
                "temporal-incident-proposals"
            ),
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "detections": [
                    _temporal_detection(
                        "det-a",
                        1.0,
                        "color_guidance_line_faded",
                    ),
                    _temporal_detection(
                        "det-b",
                        2.0,
                        "colored_guidance_faded",
                    ),
                ],
                "provenance": {
                    "project_id": str(DEMO_PROJECT_ID),
                    "analysis_job_id": COMPLETED_JOB_ID,
                    "source_file_sha256": COMPLETED_SOURCE_SHA256,
                    "model": {
                        "provider": "local_review_only",
                        "version": "road-model@0.1.0",
                    },
                },
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == (
        "trafficvisson.temporal-review-api.v1"
    )
    assert payload["computation_mode"] == "pure_review_only"
    assert payload["proposal_count"] == 1
    proposal = payload["proposals"][0]
    assert proposal["issue_type"] == "colored_guidance_faded"
    assert proposal["evidence_detection_ids"] == ["det-a", "det-b"]
    assert proposal["proposal_status"] == "awaiting_user_review"
    assert proposal["review_only"] is True
    assert proposal["automatic_incident_creation_allowed"] is False
    assert proposal["automatic_annotation_allowed"] is False
    assert proposal["automatic_training_allowed"] is False
    assert payload["state_changes_performed"] is False
    assert payload["incidents_created"] == 0
    assert payload["annotations_created"] == 0
    assert payload["training_members_created"] == 0
    assert payload["model_versions_changed"] == 0
    assert _state_snapshot(app) == before


def test_temporal_review_api_rejects_invalid_or_untrusted_provenance(
    tmp_path,
):
    app = _app(tmp_path)
    endpoint = (
        f"/projects/{DEMO_PROJECT_ID}/review/"
        "temporal-incident-proposals"
    )
    trusted_job = {
        "project_id": str(DEMO_PROJECT_ID),
        "job_id": COMPLETED_JOB_ID,
        "status": "completed",
        "source_sha256": COMPLETED_SOURCE_SHA256,
        "result": {"candidates": []},
    }

    with TestClient(app) as client:
        app.state.analysis_jobs.get = lambda project_id, job_id: trusted_job
        invalid_job = client.post(
            endpoint,
            headers=_headers(),
            json=_temporal_request(analysis_job_id="not-a-uuid"),
        )
        invalid_sha = client.post(
            endpoint,
            headers=_headers(),
            json=_temporal_request(source_sha256="not-a-sha"),
        )

        app.state.analysis_jobs.get = lambda project_id, job_id: {
            **trusted_job,
            "project_id": OTHER_PROJECT_ID,
        }
        mismatched_identity = client.post(
            endpoint,
            headers=_headers(),
            json=_temporal_request(),
        )

        app.state.analysis_jobs.get = lambda project_id, job_id: {
            **trusted_job,
            "status": "running",
            "result": None,
        }
        incomplete_job = client.post(
            endpoint,
            headers=_headers(),
            json=_temporal_request(),
        )

        app.state.analysis_jobs.get = lambda project_id, job_id: {
            **trusted_job,
            "source_sha256": "b" * 64,
        }
        mismatched_sha = client.post(
            endpoint,
            headers=_headers(),
            json=_temporal_request(),
        )

    assert invalid_job.status_code == 422
    assert invalid_sha.status_code == 422
    assert mismatched_identity.status_code == 409
    assert incomplete_job.status_code == 409
    assert mismatched_sha.status_code == 409
    assert "identity" in mismatched_identity.json()["detail"]
    assert "completed analysis job" in incomplete_job.json()["detail"]
    assert "SHA-256 does not match" in mismatched_sha.json()["detail"]


def test_model_validation_api_normalizes_legacy_types_and_is_read_only(
    tmp_path,
):
    app = _app(tmp_path)
    before = _state_snapshot(app)
    _forbid_mutating_services(app)

    with TestClient(app) as client:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/review/model-validation",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "model_version": "road-model@0.1.0",
                "truth_records": [
                    {
                        "sample_id": "sign-sample",
                        "issue_type": "sign_identity",
                        "truth_event_ids": ["sign-truth"],
                    },
                    {
                        "sample_id": "color-sample",
                        "issue_type": "color_guidance_line_faded",
                        "truth_event_ids": [],
                    },
                ],
                "prediction_records": [
                    {
                        "prediction_event_id": "sign-prediction",
                        "sample_id": "sign-sample",
                        "issue_type": "guide_sign_identity",
                        "matched_truth_event_id": "sign-truth",
                    }
                ],
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["computation_mode"] == "pure_review_only"
    metrics = {
        item["issue_type"]: item for item in payload["report"]["per_type"]
    }
    assert len(metrics) == 12
    assert metrics["guide_sign_identity"]["validation_sample_count"] == 1
    assert metrics["guide_sign_identity"]["true_positive_count"] == 1
    assert metrics["colored_guidance_faded"]["validation_sample_count"] == 1
    assert "sign_identity" not in metrics
    assert "color_guidance_line_faded" not in metrics
    assert payload["state_changes_performed"] is False
    assert payload["annotations_created"] == 0
    assert payload["training_members_created"] == 0
    assert payload["model_versions_changed"] == 0
    assert _state_snapshot(app) == before


def test_model_promotion_review_never_promotes_an_eligible_candidate(
    tmp_path,
):
    app = _app(tmp_path)
    before = _state_snapshot(app)
    _forbid_mutating_services(app)
    truth = []
    predictions = []
    for index in range(30):
        sample_id = f"sample-{index:03d}"
        truth_id = f"truth-{index:03d}"
        truth.append(
            {
                "sample_id": sample_id,
                "issue_type": "debris",
                "truth_event_ids": [truth_id],
            }
        )
        predictions.append(
            {
                "prediction_event_id": f"prediction-{index:03d}",
                "sample_id": sample_id,
                "issue_type": "debris",
                "matched_truth_event_id": truth_id,
            }
        )

    with TestClient(app) as client:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/review/model-promotion",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "active_model_version": (
                    before["project"]["active_model_version"]
                ),
                "candidate_model_version": "road-candidate@2.0.0",
                "truth_records": truth,
                "active_prediction_records": predictions,
                "candidate_prediction_records": predictions,
                "required_issue_types": ["debris"],
                "minimum_samples_per_type": 30,
                "maximum_recall_drop": 0.02,
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    decision = payload["decision"]
    assert decision["decision"] == "eligible"
    assert decision["required_issue_types"] == ["road_debris"]
    assert decision["auto_promotion_allowed"] is False
    assert decision["requires_user_review"] is True
    assert payload["automatic_promotion_performed"] is False
    assert payload["state_changes_performed"] is False
    assert payload["model_versions_changed"] == 0
    assert _state_snapshot(app) == before
    assert (
        app.state.store.get_project(DEMO_PROJECT_ID)[
            "active_model_version"
        ]
        == "common-initial@1.0.0"
    )


def test_review_compute_api_fails_closed_on_cross_project_or_other_type(
    tmp_path,
):
    app = _app(tmp_path)
    before = _state_snapshot(app)
    _forbid_mutating_services(app)

    with TestClient(app) as client:
        cross_project = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/review/"
                "temporal-incident-proposals"
            ),
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "detections": [
                    _temporal_detection("det-a", 1.0, "pothole")
                ],
                "provenance": {
                    "project_id": OTHER_PROJECT_ID,
                },
            },
        )
        unsupported_validation_type = client.post(
            f"/projects/{DEMO_PROJECT_ID}/review/model-validation",
            headers=_headers(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "model_version": "road-model@0.1.0",
                "truth_records": [
                    {
                        "sample_id": "other-sample",
                        "issue_type": "other",
                        "truth_event_ids": [],
                    }
                ],
                "prediction_records": [],
            },
        )

    assert cross_project.status_code == 409
    assert unsupported_validation_type.status_code == 422
    assert _state_snapshot(app) == before
