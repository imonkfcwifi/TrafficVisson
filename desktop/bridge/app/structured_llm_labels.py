"""Convert one server-owned structured vision proposal into training-only label data.

The renderer is never allowed to send the proposed answer, issue type,
severity, action, OCR text, provider, or provenance digest.  It can only grant
explicit permission to use the proposal already bound to an immutable
``ReviewEventSet``.  This module performs the deterministic, fail-closed
conversion used by the bridge route.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, Mapping

from .candidate_labels import CANONICAL_ISSUE_TYPES


_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_PROVIDERS = frozenset({"codex", "claude"})
_SEVERITIES = frozenset(
    {"minor", "moderate", "major", "urgent", "unassessed"}
)
_IDENTITY_TYPES = frozenset({"guide_sign_identity", "tunnel_identity"})
_PROPOSAL_SCHEMA = "trafficvisson.structured-vision-proposal.v1"
_COMPARISON_MODES = frozenset(
    {
        "metadata_comparison_only",
        "verified_deidentified_visual_comparison",
        # Older persisted ReviewEventSets used this spelling before the
        # transport contract was renamed.  They remain auditable.
        "direct_visual_comparison",
    }
)
_OCR_STATUSES = frozenset(
    {"not_applicable", "verified", "conflict", "uncertain"}
)
_PROPOSAL_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "issue_type",
        "maintenance_required",
        "action",
        "recommended_severity",
        "rationale",
        "ocr_verification",
        "uncertainty",
        "source_refs",
        "comparison_mode",
        "review_state",
        "review_only",
        "automatic_annotation_allowed",
        "automatic_incident_allowed",
        "automatic_training_membership_allowed",
        "automatic_report_truth_allowed",
    }
)


class StructuredLlmTrainingLabelError(ValueError):
    """A structured proposal cannot safely become training-only label data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid_proposal(message: str) -> StructuredLlmTrainingLabelError:
    return StructuredLlmTrainingLabelError("proposal_invalid", message)


def _validated_proposal(
    proposal: Mapping[str, Any],
    *,
    candidate_id: str,
) -> Dict[str, Any]:
    """Validate the exact review-only JSON before training conversion.

    The original provider output was schema-checked when it was produced and
    again when the immutable ReviewEventSet was written.  This third,
    intentionally small boundary prevents a stale or hand-built proposal from
    bypassing those guarantees when an Annotation is finally authorized.
    """

    if set(proposal) != _PROPOSAL_FIELDS:
        raise _invalid_proposal(
            "structured LLM proposal fields are invalid"
        )
    issue_type = proposal.get("issue_type")
    maintenance_required = proposal.get("maintenance_required")
    action = proposal.get("action")
    severity = proposal.get("recommended_severity")
    rationale = proposal.get("rationale")
    ocr = proposal.get("ocr_verification")
    uncertainty = proposal.get("uncertainty")
    source_refs = proposal.get("source_refs")
    if (
        proposal.get("schema_version") != _PROPOSAL_SCHEMA
        or proposal.get("candidate_id") != candidate_id
        or issue_type not in CANONICAL_ISSUE_TYPES
        or not isinstance(maintenance_required, bool)
        or not isinstance(action, str)
        or not action
        or len(action) > 500
        or not isinstance(rationale, str)
        or not rationale
        or len(rationale) > 1_500
        or any(ord(character) < 32 for character in rationale)
        or severity not in _SEVERITIES
        or proposal.get("comparison_mode") not in _COMPARISON_MODES
        or proposal.get("review_state") != "proposed"
        or proposal.get("review_only") is not True
        or proposal.get("automatic_annotation_allowed") is not False
        or proposal.get("automatic_incident_allowed") is not False
        or proposal.get(
            "automatic_training_membership_allowed"
        )
        is not False
        or proposal.get("automatic_report_truth_allowed") is not False
    ):
        raise _invalid_proposal(
            "structured LLM proposal content is invalid"
        )
    if (
        maintenance_required
        and (action == "none" or severity == "unassessed")
    ) or (
        not maintenance_required
        and (action != "none" or severity != "unassessed")
    ):
        raise _invalid_proposal(
            "structured LLM maintenance recommendation is inconsistent"
        )
    if not isinstance(ocr, Mapping) or set(ocr) != {
        "status",
        "normalized_text",
        "observation_refs",
    }:
        raise _invalid_proposal(
            "structured LLM OCR proposal is invalid"
        )
    ocr_status = ocr.get("status")
    normalized_text = ocr.get("normalized_text")
    observation_refs = ocr.get("observation_refs")
    if (
        ocr_status not in _OCR_STATUSES
        or (
            normalized_text is not None
            and (
                not isinstance(normalized_text, str)
                or not normalized_text
                or len(normalized_text) > 500
                or any(ord(character) < 32 for character in normalized_text)
            )
        )
        or not isinstance(observation_refs, list)
        or len(observation_refs) > 16
        or len(observation_refs) != len(set(observation_refs))
        or any(
            not isinstance(item, str) or not item or len(item) > 180
            for item in observation_refs
        )
        or (
            ocr_status == "not_applicable"
            and (normalized_text is not None or observation_refs)
        )
        or (
            ocr_status != "not_applicable"
            and not observation_refs
        )
        or (
            ocr_status == "verified"
            and normalized_text is None
        )
    ):
        raise _invalid_proposal(
            "structured LLM OCR proposal is invalid"
        )
    if not isinstance(uncertainty, Mapping) or set(uncertainty) != {
        "score",
        "reasons",
    }:
        raise _invalid_proposal(
            "structured LLM uncertainty is invalid"
        )
    score = uncertainty.get("score")
    reasons = uncertainty.get("reasons")
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(float(score))
        or not 0 <= float(score) <= 1
        or not isinstance(reasons, list)
        or not 1 <= len(reasons) <= 5
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 300
            or any(ord(character) < 32 for character in item)
            for item in reasons
        )
        or not isinstance(source_refs, list)
        or not 1 <= len(source_refs) <= 32
        or len(source_refs) != len(set(source_refs))
        or any(
            not isinstance(item, str) or not item or len(item) > 180
            for item in source_refs
        )
    ):
        raise _invalid_proposal(
            "structured LLM evidence proposal is invalid"
        )
    return dict(proposal)


def structured_llm_training_answer(
    *,
    candidate_id: str,
    event_issue_type: str,
    urgent_review: bool,
    structured_proposal: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return server-owned ``CandidateLabelStore.record_answer`` arguments."""

    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
        or not isinstance(event_issue_type, str)
        or not isinstance(urgent_review, bool)
        or not isinstance(structured_proposal, Mapping)
    ):
        raise StructuredLlmTrainingLabelError(
            "proposal_invalid",
            "structured LLM proposal identity is invalid",
        )
    if structured_proposal.get("status") != "proposed":
        raise StructuredLlmTrainingLabelError(
            "proposal_unavailable",
            "structured LLM proposal is unavailable",
        )
    provider = structured_proposal.get("provider")
    proposal_sha256 = structured_proposal.get("proposal_sha256")
    proposal = structured_proposal.get("proposal")
    if (
        provider not in _PROVIDERS
        or not isinstance(proposal_sha256, str)
        or _SHA256.fullmatch(proposal_sha256) is None
        or not isinstance(proposal, Mapping)
        or proposal.get("candidate_id") != candidate_id
        or hashlib.sha256(
            json.dumps(
                proposal,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        != proposal_sha256
    ):
        raise StructuredLlmTrainingLabelError(
            "proposal_invalid",
            "structured LLM proposal provenance is invalid",
        )

    proposal = _validated_proposal(
        proposal,
        candidate_id=candidate_id,
    )
    issue_type = proposal["issue_type"]
    maintenance_required = proposal["maintenance_required"]
    action = proposal["action"]
    severity = proposal["recommended_severity"]
    rationale = proposal["rationale"]
    ocr = proposal["ocr_verification"]

    # Debris and every emergency signal are mandatory human questions.  Even
    # an explicit bulk-assist action cannot turn them into LLM-authored labels.
    if (
        urgent_review
        or event_issue_type == "road_debris"
        or issue_type == "road_debris"
        or severity == "urgent"
    ):
        raise StructuredLlmTrainingLabelError(
            "mandatory_human_review",
            "road debris and urgent candidates require a human answer",
        )

    positive_identity = (
        not maintenance_required and issue_type in _IDENTITY_TYPES
    )
    positive = maintenance_required or positive_identity
    if positive:
        answer = "yes"
        selected_issue_type = issue_type
        selected_severity = (
            severity if severity != "unassessed" else None
        )
        maintenance_action = (
            action if maintenance_required and action != "none" else None
        )
        normalized_ocr = ocr.get("normalized_text")
        ocr_text = (
            normalized_ocr
            if (
                ocr.get("status") == "verified"
                and isinstance(normalized_ocr, str)
                and normalized_ocr
            )
            else None
        )
    else:
        answer = "no"
        selected_issue_type = None
        selected_severity = None
        maintenance_action = None
        ocr_text = None

    return {
        "answer": answer,
        "selected_issue_type": selected_issue_type,
        # The Provider's severity is always a recommendation.  Even an
        # explicit project-scoped training delegation cannot turn it into the
        # user's final severity; only a later local-user survey decision may
        # do that.  Keep the recommendation available for UI/rationale while
        # persisting no final severity on the training-only Annotation.
        "severity": None,
        "recommended_severity": selected_severity,
        "maintenance_action": maintenance_action,
        "ocr_text": ocr_text,
        "normalized_box": None,
        "answered_by": "structured_llm_assisted",
        "label_source": "structured_llm_assisted",
        "provider": provider,
        "proposal_sha256": proposal_sha256,
        "rationale": rationale,
        "maintenance_required": maintenance_required,
    }


__all__ = [
    "StructuredLlmTrainingLabelError",
    "structured_llm_training_answer",
]
