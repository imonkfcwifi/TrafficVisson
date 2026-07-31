from __future__ import annotations

import hashlib
import json

import pytest

from app.structured_llm_labels import (
    StructuredLlmTrainingLabelError,
    structured_llm_training_answer,
)


CANDIDATE_ID = "0123456789abcdefabcd"


def proposal(
    *,
    issue_type: str = "pothole",
    maintenance_required: bool = True,
    severity: str = "moderate",
    action: str = "노면 보수",
    provider: str = "codex",
    ocr_verification=None,
):
    value = {
        "schema_version": "trafficvisson.structured-vision-proposal.v1",
        "candidate_id": CANDIDATE_ID,
        "issue_type": issue_type,
        "maintenance_required": maintenance_required,
        "action": action,
        "recommended_severity": severity,
        "rationale": "3·4분기 승인 사례와 검지 근거가 유사합니다.",
        "ocr_verification": (
            ocr_verification
            if ocr_verification is not None
            else {
                "status": "not_applicable",
                "normalized_text": None,
                "observation_refs": [],
            }
        ),
        "uncertainty": {"score": 0.2, "reasons": ["bounded"]},
        "source_refs": ["reference:approved-case-01"],
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
        "provider": provider,
        "proposal_sha256": digest,
        "proposal": value,
        "error_code": None,
    }


def test_structured_proposal_becomes_training_only_server_answer():
    answer = structured_llm_training_answer(
        candidate_id=CANDIDATE_ID,
        event_issue_type="pothole",
        urgent_review=False,
        structured_proposal=proposal(),
    )

    assert answer["answer"] == "yes"
    assert answer["selected_issue_type"] == "pothole"
    assert answer["severity"] is None
    assert answer["recommended_severity"] == "moderate"
    assert answer["answered_by"] == "structured_llm_assisted"
    assert answer["label_source"] == "structured_llm_assisted"


@pytest.mark.parametrize(
    ("event_issue_type", "urgent_review", "issue_type", "severity"),
    [
        ("road_debris", False, "road_surface_damage", "moderate"),
        ("pothole", False, "road_debris", "moderate"),
        ("pothole", True, "pothole", "moderate"),
        ("pothole", False, "pothole", "urgent"),
    ],
)
def test_debris_and_urgent_proposals_always_require_human_review(
    event_issue_type,
    urgent_review,
    issue_type,
    severity,
):
    with pytest.raises(
        StructuredLlmTrainingLabelError,
        match="require a human answer",
    ):
        structured_llm_training_answer(
            candidate_id=CANDIDATE_ID,
            event_issue_type=event_issue_type,
            urgent_review=urgent_review,
            structured_proposal=proposal(
                issue_type=issue_type,
                severity=severity,
            ),
        )


def test_non_maintenance_proposal_is_a_training_negative():
    answer = structured_llm_training_answer(
        candidate_id=CANDIDATE_ID,
        event_issue_type="pothole",
        urgent_review=False,
        structured_proposal=proposal(
            maintenance_required=False,
            severity="unassessed",
            action="none",
        ),
    )

    assert answer["answer"] == "no"
    assert answer["selected_issue_type"] is None
    assert answer["severity"] is None


def test_verified_identity_ocr_remains_a_positive_class_label():
    structured = proposal(
        issue_type="tunnel_identity",
        maintenance_required=False,
        severity="unassessed",
        action="none",
        provider="claude",
        ocr_verification={
            "status": "verified",
            "normalized_text": "○○터널",
            "observation_refs": ["ocr-observation-01"],
        },
    )
    answer = structured_llm_training_answer(
        candidate_id=CANDIDATE_ID,
        event_issue_type="tunnel_identity",
        urgent_review=False,
        structured_proposal=structured,
    )

    assert answer["answer"] == "yes"
    assert answer["selected_issue_type"] == "tunnel_identity"
    assert answer["ocr_text"] == "○○터널"
    assert answer["maintenance_action"] is None


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("review_only", False),
        ("automatic_annotation_allowed", True),
        ("automatic_incident_allowed", True),
        ("automatic_training_membership_allowed", True),
        ("automatic_report_truth_allowed", True),
    ],
)
def test_truth_escalation_flags_fail_the_conversion_boundary(
    field,
    unsafe_value,
):
    structured = proposal()
    structured["proposal"][field] = unsafe_value
    structured["proposal_sha256"] = hashlib.sha256(
        json.dumps(
            structured["proposal"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(
        StructuredLlmTrainingLabelError,
        match="content is invalid",
    ):
        structured_llm_training_answer(
            candidate_id=CANDIDATE_ID,
            event_issue_type="pothole",
            urgent_review=False,
            structured_proposal=structured,
        )


def test_unexpected_json_field_fails_the_conversion_boundary():
    structured = proposal()
    structured["proposal"]["incident_id"] = (
        "11111111-1111-4111-8111-111111111111"
    )
    structured["proposal_sha256"] = hashlib.sha256(
        json.dumps(
            structured["proposal"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(
        StructuredLlmTrainingLabelError,
        match="fields are invalid",
    ):
        structured_llm_training_answer(
            candidate_id=CANDIDATE_ID,
            event_issue_type="pothole",
            urgent_review=False,
            structured_proposal=structured,
        )
