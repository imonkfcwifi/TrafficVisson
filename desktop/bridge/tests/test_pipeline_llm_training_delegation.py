import hashlib
import json
from uuid import uuid4

import pytest

from app.candidate_labels import CandidateLabelStore
from app.pipeline_llm_training_delegation import (
    PipelineStructuredLabelDelegationError,
    PipelineStructuredLabelDelegationService,
)
from app.structured_llm_labels import (
    structured_llm_training_answer,
)
from structured_assist_fixtures import (
    assist_evidence,
    structured_profile,
)


def _proposal(candidate_id, issue_type, *, severity="moderate"):
    value = {
        "schema_version": (
            "trafficvisson.structured-vision-proposal.v1"
        ),
        "candidate_id": candidate_id,
        "issue_type": issue_type,
        "maintenance_required": True,
        "action": "차선 재도색",
        "recommended_severity": severity,
        "rationale": "검증된 프레임과 유사 사례를 비교한 구조화 제안",
        "ocr_verification": {
            "status": "not_applicable",
            "normalized_text": None,
            "observation_refs": [],
        },
        "uncertainty": {
            "score": 0.2,
            "reasons": ["검지와 승인 사례가 일치함"],
        },
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
    digest = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "status": "proposed",
        "provider": "codex",
        "proposal_sha256": digest,
        "proposal": value,
    }


def _service(tmp_path, project_id, analysis_job_id, candidates):
    metadata = {
        candidate_id: {
            "candidate_id": candidate_id,
            "review_event_binding": {
                "review_only": True,
                "representative_candidate_id": candidate_id,
            },
        }
        for candidate_id in candidates
    }

    def resolve_candidate(
        requested_project_id,
        requested_analysis_job_id,
        candidate_id,
    ):
        assert requested_project_id == project_id
        assert requested_analysis_job_id == analysis_job_id
        return {
            "status": "completed",
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "candidate_id": candidate_id,
            "metadata": metadata[candidate_id],
        }

    labels = CandidateLabelStore(
        tmp_path.resolve(),
        resolve_candidate,
    )

    def resolve_proposal(
        requested_project_id,
        requested_analysis_job_id,
        candidate_id,
    ):
        assert requested_project_id == project_id
        assert requested_analysis_job_id == analysis_job_id
        row = candidates[candidate_id]
        answer = structured_llm_training_answer(
            candidate_id=candidate_id,
            event_issue_type=row["event_issue_type"],
            urgent_review=row.get("urgent_review", False),
            structured_proposal=row["proposal"],
        )
        answer["assist_evidence"] = assist_evidence(
            project_id,
            analysis_job_id,
            candidate_id,
            proposal_sha256=answer["proposal_sha256"],
        )
        return answer

    projections = []

    def project(
        requested_project_id,
        records,
        analysis_job_ids,
    ):
        assert requested_project_id == project_id
        assert analysis_job_ids == [analysis_job_id]
        projections.append(list(records))
        return []

    return (
        PipelineStructuredLabelDelegationService(
            candidate_labels=labels,
            proposal_resolver=resolve_proposal,
            projection_sink=project,
            delegation_authority_resolver=(
                lambda requested_project_id: (
                    structured_profile(requested_project_id)
                )
            ),
        ),
        labels,
        projections,
        resolve_candidate,
    )


def test_safe_proposal_is_training_only_and_restart_is_idempotent(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    safe_id = "a" * 20
    candidates = {
        safe_id: {
            "event_issue_type": "lane_marking_faded",
            "proposal": _proposal(
                safe_id, "lane_marking_faded"
            ),
        }
    }
    service, labels, projections, resolver = _service(
        tmp_path,
        project_id,
        analysis_job_id,
        candidates,
    )

    first = service.apply(
        project_id, analysis_job_id, [safe_id]
    )
    assert first["delegated_candidate_ids"] == [safe_id]
    assert first["human_question_candidate_ids"] == []
    assert first["training_annotation_count"] == 1
    assert first["incident_count"] == 0
    assert first["report_incident_count"] == 0
    assert first["fixed_validation_membership_count"] == 0
    assert first["automatic_training_started"] is False
    assert len(projections) == 1
    records = labels.latest_answers(
        project_id, analysis_job_id=analysis_job_id
    )
    assert len(records) == 1
    assert records[0]["answer_revision"] == 1
    assert records[0]["final_answer"]["answered_by"] == (
        "structured_llm_assisted"
    )
    assert records[0]["final_answer"]["severity"] is None

    restarted_labels = CandidateLabelStore(
        tmp_path.resolve(),
        resolver,
    )
    restarted = PipelineStructuredLabelDelegationService(
        candidate_labels=restarted_labels,
        proposal_resolver=service._proposal_resolver,
        projection_sink=service._projection_sink,
        delegation_authority_resolver=(
            service._delegation_authority_resolver
        ),
    )
    second = restarted.apply(
        project_id, analysis_job_id, [safe_id]
    )
    assert second["delegation_revision"] == first[
        "delegation_revision"
    ]
    restarted_records = restarted_labels.latest_answers(
        project_id, analysis_job_id=analysis_job_id
    )
    assert len(restarted_records) == 1
    assert restarted_records[0]["answer_revision"] == 1


def test_debris_urgent_and_missing_proposals_remain_human_questions(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    debris_id = "b" * 20
    urgent_id = "c" * 20
    missing_id = "d" * 20
    candidates = {
        debris_id: {
            "event_issue_type": "road_debris",
            "proposal": _proposal(debris_id, "road_debris"),
        },
        urgent_id: {
            "event_issue_type": "lane_marking_faded",
            "urgent_review": True,
            "proposal": _proposal(
                urgent_id,
                "lane_marking_faded",
                severity="urgent",
            ),
        },
        missing_id: {
            "event_issue_type": "lane_marking_faded",
            "proposal": {"status": "not_run"},
        },
    }
    service, labels, projections, _resolver = _service(
        tmp_path,
        project_id,
        analysis_job_id,
        candidates,
    )

    result = service.apply(
        project_id,
        analysis_job_id,
        [debris_id, urgent_id, missing_id],
    )
    assert result["delegated_candidate_ids"] == []
    assert result["human_question_candidate_ids"] == [
        debris_id,
        urgent_id,
        missing_id,
    ]
    assert result["mandatory_human_candidate_ids"] == [
        debris_id,
        urgent_id,
    ]
    assert result["training_annotation_count"] == 0
    assert result["incident_count"] == 0
    assert result["report_incident_count"] == 0
    assert labels.latest_answers(
        project_id, analysis_job_id=analysis_job_id
    ) == []
    assert projections == []


def test_existing_human_answer_is_never_superseded(tmp_path):
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = "e" * 20
    candidates = {
        candidate_id: {
            "event_issue_type": "lane_marking_faded",
            "proposal": _proposal(
                candidate_id, "lane_marking_faded"
            ),
        }
    }
    service, labels, _projections, _resolver = _service(
        tmp_path,
        project_id,
        analysis_job_id,
        candidates,
    )
    labels.record_answer(
        project_id,
        analysis_job_id,
        candidate_id,
        answer="no",
        answered_by="local_user",
    )

    result = service.apply(
        project_id, analysis_job_id, [candidate_id]
    )
    assert result["delegated_candidate_ids"] == []
    assert result["human_question_candidate_ids"] == [
        candidate_id
    ]
    records = labels.latest_answers(
        project_id, analysis_job_id=analysis_job_id
    )
    assert len(records) == 1
    assert records[0]["answer_revision"] == 1
    assert records[0]["final_answer"]["answered_by"] == "local_user"


def test_project_delegation_fails_closed_without_transmission_consent(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = "f" * 20
    candidates = {
        candidate_id: {
            "event_issue_type": "lane_marking_faded",
            "proposal": _proposal(
                candidate_id, "lane_marking_faded"
            ),
        }
    }
    service, labels, projections, _resolver = _service(
        tmp_path,
        project_id,
        analysis_job_id,
        candidates,
    )
    service._delegation_authority_resolver = (
        lambda requested_project_id: {
            **structured_profile(requested_project_id),
            "report_transmission_consent": False,
        }
    )

    with pytest.raises(
        PipelineStructuredLabelDelegationError,
        match="not authorized",
    ):
        service.apply(
            project_id, analysis_job_id, [candidate_id]
        )
    assert labels.latest_answers(
        project_id, analysis_job_id=analysis_job_id
    ) == []
    assert projections == []


def test_project_delegation_does_not_reuse_another_provider_proposal(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = "1" * 20
    candidates = {
        candidate_id: {
            "event_issue_type": "lane_marking_faded",
            "proposal": _proposal(
                candidate_id, "lane_marking_faded"
            ),
        }
    }
    service, labels, projections, _resolver = _service(
        tmp_path,
        project_id,
        analysis_job_id,
        candidates,
    )
    service._delegation_authority_resolver = (
        lambda requested_project_id: {
            **structured_profile(requested_project_id),
            "report_provider": "claude",
        }
    )

    result = service.apply(
        project_id, analysis_job_id, [candidate_id]
    )
    assert result["delegated_candidate_ids"] == []
    assert result["human_question_candidate_ids"] == [
        candidate_id
    ]
    assert result["human_question_reasons"] == {
        candidate_id: "delegation_provider_mismatch"
    }
    assert result["incident_count"] == 0
    assert result["report_incident_count"] == 0
    assert labels.latest_answers(
        project_id, analysis_job_id=analysis_job_id
    ) == []
    assert projections == []


def test_metadata_only_proposal_remains_a_human_question(
    tmp_path,
):
    project_id = uuid4()
    analysis_job_id = uuid4()
    candidate_id = "2" * 20
    candidates = {
        candidate_id: {
            "event_issue_type": "lane_marking_faded",
            "proposal": _proposal(
                candidate_id, "lane_marking_faded"
            ),
        }
    }
    service, labels, projections, _resolver = _service(
        tmp_path,
        project_id,
        analysis_job_id,
        candidates,
    )
    original_resolver = service._proposal_resolver

    def metadata_only_resolver(
        requested_project_id,
        requested_analysis_job_id,
        requested_candidate_id,
    ):
        answer = original_resolver(
            requested_project_id,
            requested_analysis_job_id,
            requested_candidate_id,
        )
        answer["assist_evidence"]["attachment_sha256s"] = []
        answer["assist_evidence"][
            "attachment_verification_record_sha256s"
        ] = []
        return answer

    service._proposal_resolver = metadata_only_resolver
    result = service.apply(
        project_id, analysis_job_id, [candidate_id]
    )

    assert result["delegated_candidate_ids"] == []
    assert result["human_question_reasons"] == {
        candidate_id: "verified_visual_evidence_required"
    }
    assert result["incident_count"] == 0
    assert result["report_incident_count"] == 0
    assert labels.latest_answers(
        project_id, analysis_job_id=analysis_job_id
    ) == []
    assert projections == []
