"""Fail-closed lineage from approved sample references to one Annotation.

The structured vision service already constrains a Provider response to the
source references supplied in its request.  This module re-verifies the other
side of that relationship at the training-label boundary: at least one cited
reference must be a user-approved 3Q/4Q sample case retained in the immutable
``ReviewEvent``.  The resulting proof is derived from the same event and
Provider hashes that the Annotation's structured-assist provenance stores.

No label, Incident, report fact, Provider call, document write, or COM action
is performed here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_VERSION = (
    "trafficvisson.approved-reference-annotation-lineage.v1"
)

_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REVIEW_EVENT_SET_ID = re.compile(r"^res_[a-f0-9]{32}$")
_REVIEW_EVENT_ID = re.compile(r"^rve_[a-f0-9]{32}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_PROVIDERS = frozenset({"codex", "claude"})


class ApprovedReferenceAnnotationLineageError(ValueError):
    """An LLM-assisted Annotation cannot prove an approved sample lineage."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _attribute(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ApprovedReferenceAnnotationLineageError(
            f"{field} is not a SHA-256 digest"
        )
    return value


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ApprovedReferenceAnnotationLineageError(
            f"{field} is invalid"
        )
    return value


def _sequence(value: object, field: str, *, maximum: int) -> Sequence[Any]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > maximum
    ):
        raise ApprovedReferenceAnnotationLineageError(
            f"{field} is invalid"
        )
    return value


def build_approved_reference_annotation_lineage(
    *,
    candidate_id: str,
    review_event_set: object,
    review_event: object,
) -> dict[str, Any]:
    """Build a self-digested proof for one LLM-assisted Annotation candidate."""

    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
        or _attribute(review_event, "representative_candidate_id")
        != candidate_id
    ):
        raise ApprovedReferenceAnnotationLineageError(
            "review event candidate identity does not match"
        )

    review_event_set_id = _attribute(
        review_event_set, "review_event_set_id"
    )
    review_event_id = _attribute(review_event, "event_id")
    if (
        not isinstance(review_event_set_id, str)
        or _REVIEW_EVENT_SET_ID.fullmatch(review_event_set_id) is None
        or not isinstance(review_event_id, str)
        or _REVIEW_EVENT_ID.fullmatch(review_event_id) is None
    ):
        raise ApprovedReferenceAnnotationLineageError(
            "review event identity is invalid"
        )

    structured = _attribute(review_event, "structured_proposal")
    similar_cases = _sequence(
        _attribute(review_event, "similar_cases"),
        "similar_cases",
        maximum=5,
    )
    checkpoint_revisions = _attribute(
        review_event, "checkpoint_revisions"
    )
    if (
        not isinstance(structured, Mapping)
        or structured.get("status") != "proposed"
        or not isinstance(checkpoint_revisions, Mapping)
    ):
        raise ApprovedReferenceAnnotationLineageError(
            "structured review evidence is unavailable"
        )

    proposal = structured.get("proposal")
    execution = structured.get("execution_provenance")
    if not isinstance(proposal, Mapping) or not isinstance(
        execution, Mapping
    ):
        raise ApprovedReferenceAnnotationLineageError(
            "structured proposal execution provenance is unavailable"
        )
    if proposal.get("candidate_id") != candidate_id:
        raise ApprovedReferenceAnnotationLineageError(
            "structured proposal crossed candidate scope"
        )

    source_refs = _sequence(
        proposal.get("source_refs"),
        "structured proposal source_refs",
        maximum=64,
    )
    if (
        not source_refs
        or any(
            not isinstance(item, str)
            or _IDENTIFIER.fullmatch(item) is None
            for item in source_refs
        )
        or len(source_refs) != len(set(source_refs))
    ):
        raise ApprovedReferenceAnnotationLineageError(
            "structured proposal source_refs are invalid"
        )

    cited: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for raw_case in similar_cases:
        if not isinstance(raw_case, Mapping):
            raise ApprovedReferenceAnnotationLineageError(
                "similar case lineage is invalid"
            )
        if (
            raw_case.get("reference_state") != "approved_reference"
            or raw_case.get("approved_reference") is not True
            or raw_case.get("training_eligible") is not True
            or raw_case.get("review_required") is not False
        ):
            continue
        source_id = _identifier(
            raw_case.get("source_id"), "approved reference source_id"
        )
        case_id = _identifier(
            raw_case.get("case_id"), "approved reference case_id"
        )
        identity = (source_id, case_id)
        if identity in identities:
            raise ApprovedReferenceAnnotationLineageError(
                "approved reference identity is duplicated"
            )
        identities.add(identity)
        source_ref = f"reference:{source_id}:{case_id}"
        if source_ref not in source_refs:
            continue
        quarter = raw_case.get("quarter")
        if quarter not in {"3Q", "4Q"}:
            raise ApprovedReferenceAnnotationLineageError(
                "approved reference is not a 3Q/4Q sample case"
            )
        cited.append(
            {
                "source_id": source_id,
                "case_id": case_id,
                "source_ref": source_ref,
                "quarter": quarter,
                "approval_record_sha256": _sha(
                    raw_case.get("approval_record_sha256"),
                    "approved reference approval_record_sha256",
                ),
                "document_sha256": _sha(
                    raw_case.get("document_sha256"),
                    "approved reference document_sha256",
                ),
                "photo_sha256": _sha(
                    raw_case.get("photo_sha256"),
                    "approved reference photo_sha256",
                ),
            }
        )
    if not cited:
        raise ApprovedReferenceAnnotationLineageError(
            "structured proposal did not cite an approved 3Q/4Q reference"
        )

    provider = structured.get("provider")
    model = execution.get("model")
    if (
        provider not in _PROVIDERS
        or execution.get("provider") != provider
        or not isinstance(model, str)
        or not model
        or model != model.strip()
        or len(model) > 200
        or any(ord(character) < 32 for character in model)
    ):
        raise ApprovedReferenceAnnotationLineageError(
            "structured Provider identity is invalid"
        )

    proposal_sha256 = _sha(
        structured.get("proposal_sha256"), "proposal_sha256"
    )
    if hashlib.sha256(_canonical_bytes(dict(proposal))).hexdigest() != (
        proposal_sha256
    ):
        raise ApprovedReferenceAnnotationLineageError(
            "structured proposal digest does not match"
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "review_event_set_id": review_event_set_id,
        "review_event_set_revision": _sha(
            _attribute(review_event_set, "review_event_set_revision"),
            "review_event_set_revision",
        ),
        "review_event_set_manifest_sha256": _sha(
            _attribute(review_event_set, "manifest_sha256"),
            "review_event_set_manifest_sha256",
        ),
        "review_event_id": review_event_id,
        "review_event_revision": _sha(
            _attribute(review_event, "event_revision"),
            "review_event_revision",
        ),
        "visual_reference_revision": _sha(
            checkpoint_revisions.get("visual_reference_revision"),
            "visual_reference_revision",
        ),
        "provider": provider,
        "model": model,
        "structured_input_sha256": _sha(
            execution.get("input_sha256"), "structured_input_sha256"
        ),
        "structured_proposal_sha256": proposal_sha256,
        "cited_approved_references": sorted(
            cited,
            key=lambda item: (
                item["source_id"],
                item["case_id"],
            ),
        ),
    }
    return {
        **payload,
        "lineage_sha256": hashlib.sha256(
            _canonical_bytes(payload)
        ).hexdigest(),
    }


def verify_annotation_assist_reference_lineage(
    *,
    assist_provenance: Mapping[str, Any],
    review_event_set: object,
    review_event: object,
) -> dict[str, Any]:
    """Re-resolve and verify the approved-reference path from an Annotation."""

    if not isinstance(assist_provenance, Mapping):
        raise ApprovedReferenceAnnotationLineageError(
            "Annotation assist provenance is unavailable"
        )
    candidate_id = assist_provenance.get("candidate_id")
    proof = build_approved_reference_annotation_lineage(
        candidate_id=candidate_id,
        review_event_set=review_event_set,
        review_event=review_event,
    )
    binding = assist_provenance.get("review_event")
    if not isinstance(binding, Mapping):
        raise ApprovedReferenceAnnotationLineageError(
            "Annotation review-event binding is unavailable"
        )
    expected_binding = {
        "review_event_set_id": proof["review_event_set_id"],
        "review_event_set_revision": proof[
            "review_event_set_revision"
        ],
        "review_event_set_manifest_sha256": proof[
            "review_event_set_manifest_sha256"
        ],
        "review_event_id": proof["review_event_id"],
        "review_event_revision": proof["review_event_revision"],
    }
    if any(
        binding.get(field) != expected
        for field, expected in expected_binding.items()
    ):
        raise ApprovedReferenceAnnotationLineageError(
            "Annotation points to a different review event"
        )
    if (
        assist_provenance.get("provider") != proof["provider"]
        or assist_provenance.get("model") != proof["model"]
        or assist_provenance.get("input_sha256")
        != proof["structured_input_sha256"]
        or assist_provenance.get("proposal_sha256")
        != proof["structured_proposal_sha256"]
    ):
        raise ApprovedReferenceAnnotationLineageError(
            "Annotation Provider lineage does not match the review event"
        )
    return proof


__all__ = [
    "ApprovedReferenceAnnotationLineageError",
    "SCHEMA_VERSION",
    "build_approved_reference_annotation_lineage",
    "verify_annotation_assist_reference_lineage",
]
