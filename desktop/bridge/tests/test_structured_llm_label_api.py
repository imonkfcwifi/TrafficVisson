from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid5

from app.store import DEMO_PROJECT_ID
from test_analysis_jobs import (
    _auth,
    _client,
    _complete_analysis,
    _make_video,
)
from structured_assist_fixtures import structured_profile


def _structured_proposal(candidate_id: str, *, issue_type="pothole"):
    proposal = {
        "schema_version": "trafficvisson.structured-vision-proposal.v1",
        "candidate_id": candidate_id,
        "issue_type": issue_type,
        "maintenance_required": True,
        "action": "노면 보수",
        "recommended_severity": "moderate",
        "rationale": "승인 사례와 검지 근거가 유사합니다.",
        "ocr_verification": {
            "status": "not_applicable",
            "normalized_text": None,
            "observation_refs": [],
        },
        "uncertainty": {"score": 0.2, "reasons": ["bounded"]},
        "source_refs": [
            f"analysis:{candidate_id}:dino-01",
            "reference:sample-3q:approved-case-01",
        ],
        "comparison_mode": "metadata_comparison_only",
        "review_state": "proposed",
        "review_only": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_allowed": False,
        "automatic_training_membership_allowed": False,
        "automatic_report_truth_allowed": False,
    }
    proposal_sha256 = hashlib.sha256(
        json.dumps(
            proposal,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "status": "proposed",
        "provider": "codex",
        "proposal_sha256": proposal_sha256,
        "proposal": proposal,
        "execution_provenance": {
            "provider": "codex",
            "model": "test-structured-vision-model",
            "input_sha256": "2" * 64,
            "prompt_sha256": "3" * 64,
            "idempotency_key": "4" * 64,
            "request_provenance_sha256": "5" * 64,
            "execution_provenance_sha256": "6" * 64,
            "attachment_manifest_sha256": "7" * 64,
            "attachment_sha256s": [
                f"{index:064x}" for index in range(1, 5)
            ],
            "attachment_verification_record_sha256s": [
                f"{index:064x}" for index in range(11, 15)
            ],
            "comparison_mode": "direct_visual_comparison",
        },
        "error_code": None,
    }


def _install_review_event(
    client,
    *,
    terminal,
    issue_type="pothole",
    urgent_review=False,
):
    candidate = dict(terminal["result"]["candidates"][0])
    candidate_id = candidate["candidate_id"]
    pipeline_job_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    overlay_id = "ceo_" + "b" * 32
    candidate["candidate_kind"] = "dense_candidate_evidence_overlay"
    candidate["evidence_overlay"] = {
        "schema_version": "trafficvisson.candidate-evidence-overlay.v1",
        "pipeline_job_id": pipeline_job_id,
        "overlay_id": overlay_id,
        "review_only": True,
    }
    for key, suffix, media_type, artifact_id in (
        (
            "representative_image",
            ".jpg",
            "image/jpeg",
            "cea_" + "1" * 32,
        ),
        (
            "preview_clip",
            ".mp4",
            "video/mp4",
            "cea_" + "2" * 32,
        ),
    ):
        candidate[key] = {
            **candidate[key],
            "artifact_id": artifact_id,
            "artifact_ref": (
                f"candidate-evidence:{overlay_id}:{artifact_id}"
            ),
            "relative_path": f"{candidate_id}{suffix}",
            "media_type": media_type,
        }

    class Evidence:
        def resolve_candidate(
            self,
            project_id,
            analysis_job_id,
            requested_candidate_id,
        ):
            assert requested_candidate_id == candidate_id
            return {
                "status": "completed",
                "project_id": str(project_id),
                "analysis_job_id": str(analysis_job_id),
                "candidate_id": candidate_id,
                "metadata": candidate,
            }

    event = SimpleNamespace(
        representative_candidate_id=candidate_id,
        event_id="rve_" + "c" * 32,
        event_revision="d" * 64,
        issue_type=issue_type,
        urgent_review=urgent_review,
        similar_cases=(
            {
                "source_id": "sample-3q",
                "case_id": "approved-case-01",
                "quarter": "3Q",
                "reference_state": "approved_reference",
                "approved_reference": True,
                "review_required": False,
                "training_eligible": True,
                "approval_record_sha256": "8" * 64,
                "document_sha256": "9" * 64,
                "photo_sha256": "a" * 64,
            },
        ),
        checkpoint_revisions={
            "visual_reference_revision": "b" * 64,
        },
        structured_proposal=_structured_proposal(
            candidate_id,
            issue_type=issue_type,
        ),
    )
    event_set = SimpleNamespace(
        review_event_set_id="res_" + "e" * 32,
        review_event_set_revision="f" * 64,
        manifest_sha256="1" * 64,
        source_sha256=terminal["source_sha256"],
        pipeline_job_id=UUID(pipeline_job_id),
        overlay_id=overlay_id,
        events=(event,),
    )

    class ReviewSets:
        def list(
            self,
            project_id,
            analysis_job_id,
            requested_pipeline_job_id,
            requested_overlay_id,
        ):
            assert str(requested_pipeline_job_id) == pipeline_job_id
            assert requested_overlay_id == overlay_id
            return (event_set,)

        def find_candidate_binding(
            self,
            project_id,
            analysis_job_id,
            requested_pipeline_job_id,
            requested_candidate_id,
        ):
            assert str(requested_pipeline_job_id) == pipeline_job_id
            assert requested_candidate_id == candidate_id
            return event_set, event

    client.app.state.candidate_evidence = Evidence()
    client.app.state.review_event_sets = ReviewSets()
    return candidate


def test_explicit_structured_llm_label_and_human_revision_remain_training_only(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        candidate = _install_review_event(client, terminal=terminal)
        job_id = terminal["job_id"]
        candidate_id = candidate["candidate_id"]
        question_id = str(uuid5(UUID(job_id), candidate_id))
        route = (
            f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}/"
            f"candidates/{candidate_id}/structured-llm-assist"
        )

        assisted = client.post(
            route,
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "question_id": question_id,
                "user_authorized_training": True,
            },
        )
        assert assisted.status_code == 201, assisted.text
        assert assisted.json()["answered_by"] == "structured_llm_assisted"
        assert assisted.json()["label_source"] == "structured_llm_assisted"
        incidents = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=_auth(),
        )
        assert incidents.status_code == 200
        assert incidents.json()["count"] == 0

        dataset = client.post(
            f"/projects/{DEMO_PROJECT_ID}/datasets",
            headers=_auth(),
            json={
                "user_confirmed": True,
            },
        )
        assert dataset.status_code == 201, dataset.text
        assert dataset.json()["training_member_count"] == 1
        assert dataset.json()["memberships"][0]["split"] == "train"

        human = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}/"
                f"candidates/{candidate_id}/answer"
            ),
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "answer": "yes",
                "selected_issue_type": "pothole",
                "severity": "major",
                "maintenance_action": "노면 보수",
            },
        )
        assert human.status_code == 201, human.text
        assert human.json()["answer_revision"] == 2
        assert human.json()["answered_by"] == "local_user"
        incidents = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=_auth(),
        )
        assert incidents.json()["count"] == 0

        rejected = client.post(
            route,
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "question_id": question_id,
                "user_authorized_training": True,
                "answered_by": "structured_llm_assisted",
            },
        )
        assert rejected.status_code == 422


def test_project_delegation_service_projects_training_only_idempotently(
    tmp_path,
):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        candidate = _install_review_event(client, terminal=terminal)
        analysis_job_id = UUID(terminal["job_id"])
        candidate_id = candidate["candidate_id"]
        service = (
            client.app.state.pipeline_structured_label_delegation
        )
        service._delegation_authority_resolver = (
            lambda requested_project_id: structured_profile(
                requested_project_id
            )
        )

        first = service.apply(
            DEMO_PROJECT_ID,
            analysis_job_id,
            [candidate_id],
        )
        second = service.apply(
            DEMO_PROJECT_ID,
            analysis_job_id,
            [candidate_id],
        )
        assert first["delegated_candidate_ids"] == [candidate_id]
        assert second["delegation_revision"] == first[
            "delegation_revision"
        ]
        assert first["incident_count"] == 0
        assert first["report_incident_count"] == 0
        assert first["fixed_validation_membership_count"] == 0

        answers = (
            client.app.state.candidate_labels.latest_answers(
                DEMO_PROJECT_ID,
                analysis_job_id=analysis_job_id,
            )
        )
        assert len(answers) == 1
        assert answers[0]["final_answer"]["answered_by"] == (
            "structured_llm_assisted"
        )
        assert answers[0]["answer_revision"] == 1

        incidents = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=_auth(),
        )
        assert incidents.status_code == 200
        assert incidents.json()["count"] == 0
        dataset = client.post(
            f"/projects/{DEMO_PROJECT_ID}/datasets",
            headers=_auth(),
            json={"user_confirmed": True},
        )
        assert dataset.status_code == 201, dataset.text
        assert dataset.json()["training_member_count"] == 1
        assert dataset.json()["memberships"][0]["split"] == "train"


def test_debris_or_urgent_structured_llm_label_is_never_accepted(tmp_path):
    source, ffmpeg, ffprobe = _make_video(tmp_path)
    with _client(tmp_path, ffmpeg, ffprobe) as client:
        terminal = _complete_analysis(client, source)
        candidate = _install_review_event(
            client,
            terminal=terminal,
            issue_type="road_debris",
            urgent_review=True,
        )
        job_id = terminal["job_id"]
        candidate_id = candidate["candidate_id"]
        response = client.post(
            (
                f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}/"
                f"candidates/{candidate_id}/structured-llm-assist"
            ),
            headers=_auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": job_id,
                "question_id": str(uuid5(UUID(job_id), candidate_id)),
                "user_authorized_training": True,
            },
        )

        assert response.status_code == 409, response.text
        assert (
            client.app.state.candidate_labels.audit_records(
                DEMO_PROJECT_ID,
                analysis_job_id=UUID(job_id),
            )
            == []
        )
