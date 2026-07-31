"""Immutable authority and provider lineage for LLM-assisted training labels.

An LLM-assisted answer is training-only, but it still needs stronger
provenance than an actor string.  This contract binds the explicit delegation,
the exact ReviewEventSet, provider execution, prompt/input/model identities,
deidentified attachments, and proposal to one deterministic question.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid5


SCHEMA_VERSION = "trafficvisson.structured-assist-provenance.v1"
PER_ITEM_GRANT_SCHEMA = (
    "trafficvisson.structured-assist-per-item-grant.v1"
)
AUTHORITY_KINDS = frozenset(
    {"project_profile_delegation", "per_item_user_grant"}
)
PROVIDERS = frozenset({"codex", "claude"})

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_REVIEW_SET_ID = re.compile(r"^res_[a-f0-9]{32}$")
_REVIEW_EVENT_ID = re.compile(r"^rve_[a-f0-9]{32}$")
_OVERLAY_ID = re.compile(r"^ceo_[a-f0-9]{32}$")
_MAX_BYTES = 64 * 1024


class StructuredAssistProvenanceError(ValueError):
    """Structured training delegation provenance is incomplete or corrupt."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def per_item_user_grant_sha256(
    *,
    project_id: UUID | str,
    analysis_job_id: UUID | str,
    question_id: UUID | str,
    candidate_id: str,
) -> str:
    """Return the deterministic digest for one explicit UI authorization."""

    project_text = _uuid(str(project_id), "project_id")
    analysis_text = _uuid(str(analysis_job_id), "analysis_job_id")
    question_text = _uuid(str(question_id), "question_id")
    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
    ):
        raise StructuredAssistProvenanceError("candidate_id is invalid")
    if question_text != str(uuid5(UUID(analysis_text), candidate_id)):
        raise StructuredAssistProvenanceError(
            "question_id is not deterministic"
        )
    return hashlib.sha256(
        _canonical_bytes(
            {
                "schema_version": PER_ITEM_GRANT_SCHEMA,
                "project_id": project_text,
                "analysis_job_id": analysis_text,
                "question_id": question_text,
                "candidate_id": candidate_id,
                "user_authorized_training": True,
            }
        )
    ).hexdigest()


def project_profile_delegation_authority(
    profile: Mapping[str, Any],
    *,
    project_id: UUID | str,
) -> dict[str, Any]:
    """Bind an explicit v4 project profile to automatic training assistance.

    ``report_transmission_consent`` is the persisted profile field currently
    used for every bounded Codex/Claude transmission, including structured
    candidate judgment.  Requiring it here keeps label authority separate
    from privacy/transmission authority: selecting LLM-assisted labeling alone
    is never sufficient to authorize provider use.
    """

    if not isinstance(profile, Mapping):
        raise StructuredAssistProvenanceError(
            "project delegation profile is unavailable"
        )
    project_text = _uuid(str(project_id), "project_id")
    revision = profile.get("revision")
    provider = profile.get("report_provider")
    if (
        profile.get("schema_version")
        != "trafficvisson.pipeline-profile.v4"
        or profile.get("project_id") != project_text
        or profile.get("label_confirmation_authority")
        != "structured_llm_training_assist"
        or provider not in PROVIDERS
        or profile.get("report_transmission_consent") is not True
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
    ):
        raise StructuredAssistProvenanceError(
            "project delegation profile is not authorized"
        )
    try:
        profile_sha256 = hashlib.sha256(
            _canonical_bytes(dict(profile))
        ).hexdigest()
    except (TypeError, ValueError) as error:
        raise StructuredAssistProvenanceError(
            "project delegation profile is not canonical JSON"
        ) from error
    return {
        "kind": "project_profile_delegation",
        "profile_schema_version": (
            "trafficvisson.pipeline-profile.v4"
        ),
        "profile_revision": revision,
        "profile_sha256": profile_sha256,
        "grant_sha256": None,
    }


def per_item_user_grant_authority(
    *,
    project_id: UUID | str,
    analysis_job_id: UUID | str,
    question_id: UUID | str,
    candidate_id: str,
) -> dict[str, Any]:
    """Build the authority object for a single explicit user grant."""

    return {
        "kind": "per_item_user_grant",
        "profile_schema_version": None,
        "profile_revision": None,
        "profile_sha256": None,
        "grant_sha256": per_item_user_grant_sha256(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            question_id=question_id,
            candidate_id=candidate_id,
        ),
    }


def _uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise StructuredAssistProvenanceError(f"{field} must be a UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise StructuredAssistProvenanceError(
            f"{field} must be a UUID"
        ) from error
    if str(parsed) != value:
        raise StructuredAssistProvenanceError(
            f"{field} must be canonical"
        )
    return value


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise StructuredAssistProvenanceError(
            f"{field} must be a SHA-256 digest"
        )
    return value


def _authority(value: object) -> dict[str, Any]:
    expected = {
        "kind",
        "profile_schema_version",
        "profile_revision",
        "profile_sha256",
        "grant_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise StructuredAssistProvenanceError(
            "delegation authority fields are invalid"
        )
    kind = value["kind"]
    if kind not in AUTHORITY_KINDS:
        raise StructuredAssistProvenanceError(
            "delegation authority kind is invalid"
        )
    if kind == "project_profile_delegation":
        revision = value["profile_revision"]
        if (
            value["profile_schema_version"]
            != "trafficvisson.pipeline-profile.v4"
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or value["grant_sha256"] is not None
        ):
            raise StructuredAssistProvenanceError(
                "project profile delegation is invalid"
            )
        return {
            "kind": kind,
            "profile_schema_version": value[
                "profile_schema_version"
            ],
            "profile_revision": revision,
            "profile_sha256": _sha(
                value["profile_sha256"],
                "authority.profile_sha256",
            ),
            "grant_sha256": None,
        }
    if any(
        value[field] is not None
        for field in (
            "profile_schema_version",
            "profile_revision",
            "profile_sha256",
        )
    ):
        raise StructuredAssistProvenanceError(
            "per-item grant cannot claim a project profile"
        )
    return {
        "kind": kind,
        "profile_schema_version": None,
        "profile_revision": None,
        "profile_sha256": None,
        "grant_sha256": _sha(
            value["grant_sha256"],
            "authority.grant_sha256",
        ),
    }


def _review_event(value: object) -> dict[str, Any]:
    expected = {
        "pipeline_job_id",
        "overlay_id",
        "review_event_set_id",
        "review_event_set_revision",
        "review_event_set_manifest_sha256",
        "review_event_set_source_sha256",
        "review_event_id",
        "review_event_revision",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise StructuredAssistProvenanceError(
            "review event binding fields are invalid"
        )
    overlay_id = value["overlay_id"]
    set_id = value["review_event_set_id"]
    event_id = value["review_event_id"]
    if not isinstance(overlay_id, str) or _OVERLAY_ID.fullmatch(
        overlay_id
    ) is None:
        raise StructuredAssistProvenanceError("overlay_id is invalid")
    if not isinstance(set_id, str) or _REVIEW_SET_ID.fullmatch(set_id) is None:
        raise StructuredAssistProvenanceError(
            "review_event_set_id is invalid"
        )
    if (
        not isinstance(event_id, str)
        or _REVIEW_EVENT_ID.fullmatch(event_id) is None
    ):
        raise StructuredAssistProvenanceError(
            "review_event_id is invalid"
        )
    return {
        "pipeline_job_id": _uuid(
            value["pipeline_job_id"], "review_event.pipeline_job_id"
        ),
        "overlay_id": overlay_id,
        "review_event_set_id": set_id,
        "review_event_set_revision": _sha(
            value["review_event_set_revision"],
            "review_event.review_event_set_revision",
        ),
        "review_event_set_manifest_sha256": _sha(
            value["review_event_set_manifest_sha256"],
            "review_event.review_event_set_manifest_sha256",
        ),
        "review_event_set_source_sha256": _sha(
            value["review_event_set_source_sha256"],
            "review_event.review_event_set_source_sha256",
        ),
        "review_event_id": event_id,
        "review_event_revision": _sha(
            value["review_event_revision"],
            "review_event.review_event_revision",
        ),
    }


def _hash_list(value: object, field: str) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > 6
    ):
        raise StructuredAssistProvenanceError(f"{field} is invalid")
    values = [_sha(item, f"{field}[{index}]") for index, item in enumerate(value)]
    if len(values) != len(set(values)):
        raise StructuredAssistProvenanceError(
            f"{field} contains duplicate hashes"
        )
    return values


def canonical_structured_assist_provenance(
    value: object,
) -> dict[str, Any]:
    """Validate one self-digested structured-assist lineage object."""

    expected = {
        "schema_version",
        "project_id",
        "analysis_job_id",
        "question_id",
        "candidate_id",
        "authority",
        "review_event",
        "provider",
        "model",
        "input_sha256",
        "prompt_sha256",
        "request_provenance_sha256",
        "execution_provenance_sha256",
        "attachment_manifest_sha256",
        "attachment_sha256s",
        "attachment_verification_record_sha256s",
        "proposal_sha256",
        "provenance_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise StructuredAssistProvenanceError(
            "structured assist provenance fields are invalid"
        )
    if value["schema_version"] != SCHEMA_VERSION:
        raise StructuredAssistProvenanceError(
            "structured assist provenance schema is unsupported"
        )
    project_id = _uuid(value["project_id"], "project_id")
    analysis_job_id = _uuid(
        value["analysis_job_id"], "analysis_job_id"
    )
    question_id = _uuid(value["question_id"], "question_id")
    candidate_id = value["candidate_id"]
    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
    ):
        raise StructuredAssistProvenanceError("candidate_id is invalid")
    if question_id != str(uuid5(UUID(analysis_job_id), candidate_id)):
        raise StructuredAssistProvenanceError(
            "question_id is not deterministic"
        )
    provider = value["provider"]
    model = value["model"]
    if provider not in PROVIDERS:
        raise StructuredAssistProvenanceError("provider is invalid")
    if (
        not isinstance(model, str)
        or not model
        or model != model.strip()
        or len(model) > 200
        or any(ord(character) < 32 for character in model)
    ):
        raise StructuredAssistProvenanceError("model identity is invalid")
    attachment_hashes = _hash_list(
        value["attachment_sha256s"], "attachment_sha256s"
    )
    verification_hashes = _hash_list(
        value["attachment_verification_record_sha256s"],
        "attachment_verification_record_sha256s",
    )
    if len(attachment_hashes) != len(verification_hashes):
        raise StructuredAssistProvenanceError(
            "attachment provenance is incomplete"
        )
    if not 4 <= len(attachment_hashes) <= 6:
        raise StructuredAssistProvenanceError(
            "visual comparison requires four to six attachments"
        )
    authority = _authority(value["authority"])
    if (
        authority["kind"] == "per_item_user_grant"
        and authority["grant_sha256"]
        != per_item_user_grant_sha256(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            question_id=question_id,
            candidate_id=candidate_id,
        )
    ):
        raise StructuredAssistProvenanceError(
            "per-item user grant digest does not match"
        )
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "analysis_job_id": analysis_job_id,
        "question_id": question_id,
        "candidate_id": candidate_id,
        "authority": authority,
        "review_event": _review_event(value["review_event"]),
        "provider": provider,
        "model": model,
        "input_sha256": _sha(value["input_sha256"], "input_sha256"),
        "prompt_sha256": _sha(value["prompt_sha256"], "prompt_sha256"),
        "request_provenance_sha256": _sha(
            value["request_provenance_sha256"],
            "request_provenance_sha256",
        ),
        "execution_provenance_sha256": _sha(
            value["execution_provenance_sha256"],
            "execution_provenance_sha256",
        ),
        "attachment_manifest_sha256": _sha(
            value["attachment_manifest_sha256"],
            "attachment_manifest_sha256",
        ),
        "attachment_sha256s": attachment_hashes,
        "attachment_verification_record_sha256s": verification_hashes,
        "proposal_sha256": _sha(
            value["proposal_sha256"], "proposal_sha256"
        ),
    }
    digest = hashlib.sha256(_canonical_bytes(canonical)).hexdigest()
    if value["provenance_sha256"] != digest:
        raise StructuredAssistProvenanceError(
            "structured assist provenance digest does not match"
        )
    result = {**canonical, "provenance_sha256": digest}
    if len(_canonical_bytes(result)) > _MAX_BYTES:
        raise StructuredAssistProvenanceError(
            "structured assist provenance is too large"
        )
    return result


def seal_structured_assist_provenance(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach the canonical digest to a server-built provenance object."""

    if not isinstance(value, Mapping):
        raise StructuredAssistProvenanceError(
            "structured assist provenance must be an object"
        )
    raw = dict(value)
    raw.pop("provenance_sha256", None)
    raw["provenance_sha256"] = hashlib.sha256(
        _canonical_bytes(raw)
    ).hexdigest()
    return canonical_structured_assist_provenance(raw)


__all__ = [
    "AUTHORITY_KINDS",
    "PER_ITEM_GRANT_SCHEMA",
    "PROVIDERS",
    "SCHEMA_VERSION",
    "StructuredAssistProvenanceError",
    "canonical_structured_assist_provenance",
    "per_item_user_grant_authority",
    "per_item_user_grant_sha256",
    "project_profile_delegation_authority",
    "seal_structured_assist_provenance",
]
