"""Cryptographic authority contracts for final road-survey facts.

Candidate proposals, training Annotations, structured-LLM-assisted labels and
promoted models are all useful inputs, but none of them is a maintenance fact.
This module preserves the additional authority needed to cross that boundary:

* an independently promoted-model survey;
* immutable 2 fps discovery and ReviewEvent/question provenance;
* the user's final missed-scene QA choice;
* one exact local-user answer and its resolved image/clip evidence.

The complete authority object is retained through the report binder.  HWPX
mutation may keep only its verified digest because the binder has already
checked every field against the current Incident and evidence descriptor.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Optional
from uuid import UUID, uuid5


SURVEY_INCIDENT_AUTHORITY_SCHEMA = (
    "trafficvisson.survey-incident-authority.v2"
)
APPROVED_INCIDENT_OUTPUT_AUTHORITY_SCHEMA = (
    "trafficvisson.approved-incident-output-authority.v1"
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_REVIEW_EVENT_SET_ID = re.compile(r"^res_[a-f0-9]{32}$")
_REVIEW_EVENT_ID = re.compile(r"^rve_[a-f0-9]{32}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_SURVEY_AUTHORITY_KEYS = frozenset(
    {
        "schema_version",
        "project_id",
        "pipeline_job_id",
        "analysis_job_id",
        "source_sha256",
        "question_queue_revision",
        "review_completion_id",
        "answer_ledger_sha256",
        "survey_qa_revision",
        "model_version_id",
        "model_promotion_state",
        "dataset_version_id",
        "dataset_hash",
        "dataset_provenance_graph_sha256",
        "fixed_validation_sha256",
        "primary_artifact_sha256",
        "artifact_manifest_sha256",
        "source_analysis_lineage_sha256",
        "promoted_model_inference_sha256",
        "dense_index_sha256",
        "dense_candidate_discovery_revision",
        "sampling_fps",
        "scanned_frame_count",
        "event_binding",
        "question_catalog_binding",
        "final_qa_choice",
        "user_discovered_registrations_sha256",
        "user_discovered_candidate_count",
        "actor",
        "independent_survey_verified",
    }
)

_OUTPUT_AUTHORITY_KEYS = frozenset(
    {
        "schema_version",
        "project_id",
        "pipeline_job_id",
        "analysis_job_id",
        "source_sha256",
        "incident_id",
        "question_id",
        "candidate_id",
        "survey_incident_authority_sha256",
        "model_version_id",
        "dataset_version_id",
        "dataset_hash",
        "dataset_provenance_graph_sha256",
        "fixed_validation_sha256",
        "primary_artifact_sha256",
        "artifact_manifest_sha256",
        "source_analysis_lineage_sha256",
        "promoted_model_inference_sha256",
        "question_queue_revision",
        "review_completion_id",
        "answer_ledger_sha256",
        "review_event_binding",
        "approval_actor",
        "approval_record_id",
        "approval_record_sha256",
        "answer_revision",
        "candidate_metadata_sha256",
        "representative_image_ref",
        "representative_image_sha256",
        "preview_clip_ref",
        "preview_clip_sha256",
        "final_missed_scene_qa_revision",
        "final_qa_choice",
        "authority_sha256",
    }
)


class ApprovedIncidentAuthorityError(ValueError):
    """An attempted final-fact authority is missing, stale, or inconsistent."""


def canonical_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ApprovedIncidentAuthorityError(
            "authority is not canonical JSON"
        ) from error
    return hashlib.sha256(encoded).hexdigest()


def _uuid_text(value: Any, label: str) -> str:
    try:
        canonical = str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise ApprovedIncidentAuthorityError(
            f"{label} is invalid"
        ) from error
    if value != canonical:
        raise ApprovedIncidentAuthorityError(f"{label} is invalid")
    return canonical


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ApprovedIncidentAuthorityError(f"{label} is invalid")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ApprovedIncidentAuthorityError(f"{label} is invalid")
    return value


def _review_event_binding(value: Any) -> dict[str, Any]:
    exact_keys = {
        "schema_version",
        "review_event_set_id",
        "review_event_set_revision",
        "review_event_set_manifest_sha256",
        "review_event_id",
        "review_event_revision",
        "representative_candidate_id",
        "review_only",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != exact_keys
        or value.get("schema_version")
        != "trafficvisson.review-event-binding.v1"
        or not isinstance(value.get("review_event_set_id"), str)
        or _REVIEW_EVENT_SET_ID.fullmatch(
            value["review_event_set_id"]
        )
        is None
        or not isinstance(value.get("review_event_id"), str)
        or _REVIEW_EVENT_ID.fullmatch(value["review_event_id"]) is None
        or not isinstance(
            value.get("representative_candidate_id"), str
        )
        or _CANDIDATE_ID.fullmatch(
            value["representative_candidate_id"]
        )
        is None
        or value.get("review_only") is not True
    ):
        raise ApprovedIncidentAuthorityError(
            "review-event binding is invalid"
        )
    for field in (
        "review_event_set_revision",
        "review_event_set_manifest_sha256",
        "review_event_revision",
    ):
        _sha(value.get(field), field)
    return dict(value)


def _survey_event_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ApprovedIncidentAuthorityError(
            "survey event binding is invalid"
        )
    state = value.get("state")
    if state == "materialized":
        if set(value) != {
            "state",
            "review_event_set_id",
            "review_event_set_revision",
            "review_event_set_manifest_sha256",
        }:
            raise ApprovedIncidentAuthorityError(
                "survey event binding is invalid"
            )
        if (
            not isinstance(value.get("review_event_set_id"), str)
            or _REVIEW_EVENT_SET_ID.fullmatch(
                value["review_event_set_id"]
            )
            is None
        ):
            raise ApprovedIncidentAuthorityError(
                "survey event binding is invalid"
            )
        _sha(
            value.get("review_event_set_revision"),
            "review_event_set_revision",
        )
        _sha(
            value.get("review_event_set_manifest_sha256"),
            "review_event_set_manifest_sha256",
        )
    elif state == "materialized_collection":
        if set(value) != {
            "state",
            "review_event_set_collection_revision",
        }:
            raise ApprovedIncidentAuthorityError(
                "survey event binding is invalid"
            )
        _sha(
            value.get("review_event_set_collection_revision"),
            "review_event_set_collection_revision",
        )
    else:
        raise ApprovedIncidentAuthorityError(
            "partial or legacy ReviewEvent authority is not final truth"
        )
    return dict(value)


def _question_catalog_binding(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or value.get("state") != "registered"
    ):
        raise ApprovedIncidentAuthorityError(
            "question catalog binding is invalid"
        )
    if set(value) == {"state", "question_catalog_revision"}:
        _sha(
            value.get("question_catalog_revision"),
            "question_catalog_revision",
        )
    elif set(value) == {
        "state",
        "question_catalog_collection_revision",
    }:
        _sha(
            value.get("question_catalog_collection_revision"),
            "question_catalog_collection_revision",
        )
    else:
        raise ApprovedIncidentAuthorityError(
            "question catalog binding is invalid"
        )
    return dict(value)


def validate_survey_incident_authority(
    value: Any,
    *,
    expected_sha256: Any,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
    question_queue_revision: str,
    review_completion_id: str,
    answer_ledger_sha256: str,
) -> dict[str, Any]:
    """Validate one complete promoted-survey/final-QA authority manifest."""

    if not isinstance(value, Mapping) or set(value) != _SURVEY_AUTHORITY_KEYS:
        raise ApprovedIncidentAuthorityError(
            "complete survey authority manifest is required"
        )
    manifest = dict(value)
    expected_digest = _sha(
        expected_sha256, "survey_incident_authority_sha256"
    )
    if canonical_sha256(manifest) != expected_digest:
        raise ApprovedIncidentAuthorityError(
            "survey authority digest does not verify"
        )
    if (
        manifest.get("schema_version")
        != SURVEY_INCIDENT_AUTHORITY_SCHEMA
        or _uuid_text(manifest.get("project_id"), "project_id")
        != str(project_id)
        or _uuid_text(
            manifest.get("pipeline_job_id"), "pipeline_job_id"
        )
        != str(pipeline_job_id)
        or _uuid_text(
            manifest.get("analysis_job_id"), "analysis_job_id"
        )
        != str(analysis_job_id)
        or _sha(manifest.get("source_sha256"), "source_sha256")
        != source_sha256
        or _sha(
            manifest.get("question_queue_revision"),
            "question_queue_revision",
        )
        != question_queue_revision
        or manifest.get("review_completion_id") != review_completion_id
        or _sha(
            manifest.get("answer_ledger_sha256"),
            "answer_ledger_sha256",
        )
        != answer_ledger_sha256
        or manifest.get("model_promotion_state") != "promoted"
        or manifest.get("actor") != "local_user"
        or manifest.get("independent_survey_verified") is not True
        or manifest.get("sampling_fps") != 2
    ):
        raise ApprovedIncidentAuthorityError(
            "survey authority crossed its project or review boundary"
        )
    for field in (
        "survey_qa_revision",
        "dataset_hash",
        "dataset_provenance_graph_sha256",
        "fixed_validation_sha256",
        "primary_artifact_sha256",
        "artifact_manifest_sha256",
        "source_analysis_lineage_sha256",
        "promoted_model_inference_sha256",
        "dense_index_sha256",
        "dense_candidate_discovery_revision",
        "user_discovered_registrations_sha256",
    ):
        _sha(manifest.get(field), field)
    for field in ("model_version_id", "dataset_version_id"):
        _identifier(manifest.get(field), field)
    scanned = manifest.get("scanned_frame_count")
    discovered = manifest.get("user_discovered_candidate_count")
    final_choice = manifest.get("final_qa_choice")
    if (
        isinstance(scanned, bool)
        or not isinstance(scanned, int)
        or scanned < 1
        or isinstance(discovered, bool)
        or not isinstance(discovered, int)
        or discovered < 0
        or final_choice
        not in {"no_miss_complete", "miss_candidates_reviewed"}
        or (final_choice == "no_miss_complete" and discovered != 0)
        or (
            final_choice == "miss_candidates_reviewed"
            and discovered < 1
        )
    ):
        raise ApprovedIncidentAuthorityError(
            "final missed-scene QA authority is invalid"
        )
    _survey_event_binding(manifest.get("event_binding"))
    _question_catalog_binding(manifest.get("question_catalog_binding"))
    return manifest


def build_approved_incident_output_authority(
    *,
    survey_authority: Mapping[str, Any],
    survey_incident_authority_sha256: str,
    incident_id: str,
    question_id: str,
    candidate_id: str,
    review_event_binding: Mapping[str, Any],
    approval_record_id: str,
    approval_record_sha256: str,
    answer_revision: int,
    candidate_metadata_sha256: str,
    representative_image_ref: str,
    representative_image_sha256: str,
    preview_clip_ref: str,
    preview_clip_sha256: str,
) -> dict[str, Any]:
    """Create the exact final-fact authority for one approved Incident."""

    event_binding = _review_event_binding(review_event_binding)
    body = {
        "schema_version": APPROVED_INCIDENT_OUTPUT_AUTHORITY_SCHEMA,
        "project_id": survey_authority["project_id"],
        "pipeline_job_id": survey_authority["pipeline_job_id"],
        "analysis_job_id": survey_authority["analysis_job_id"],
        "source_sha256": survey_authority["source_sha256"],
        "incident_id": incident_id,
        "question_id": question_id,
        "candidate_id": candidate_id,
        "survey_incident_authority_sha256": (
            survey_incident_authority_sha256
        ),
        "model_version_id": survey_authority["model_version_id"],
        "dataset_version_id": survey_authority[
            "dataset_version_id"
        ],
        "dataset_hash": survey_authority["dataset_hash"],
        "dataset_provenance_graph_sha256": survey_authority[
            "dataset_provenance_graph_sha256"
        ],
        "fixed_validation_sha256": survey_authority[
            "fixed_validation_sha256"
        ],
        "primary_artifact_sha256": survey_authority[
            "primary_artifact_sha256"
        ],
        "artifact_manifest_sha256": survey_authority[
            "artifact_manifest_sha256"
        ],
        "source_analysis_lineage_sha256": survey_authority[
            "source_analysis_lineage_sha256"
        ],
        "promoted_model_inference_sha256": survey_authority[
            "promoted_model_inference_sha256"
        ],
        "question_queue_revision": survey_authority[
            "question_queue_revision"
        ],
        "review_completion_id": survey_authority[
            "review_completion_id"
        ],
        "answer_ledger_sha256": survey_authority[
            "answer_ledger_sha256"
        ],
        "review_event_binding": event_binding,
        "approval_actor": "local_user",
        "approval_record_id": approval_record_id,
        "approval_record_sha256": approval_record_sha256,
        "answer_revision": answer_revision,
        "candidate_metadata_sha256": candidate_metadata_sha256,
        "representative_image_ref": representative_image_ref,
        "representative_image_sha256": representative_image_sha256,
        "preview_clip_ref": preview_clip_ref,
        "preview_clip_sha256": preview_clip_sha256,
        "final_missed_scene_qa_revision": survey_authority[
            "survey_qa_revision"
        ],
        "final_qa_choice": survey_authority["final_qa_choice"],
    }
    return {**body, "authority_sha256": canonical_sha256(body)}


def validate_approved_incident_output_authority(
    value: Any,
    *,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
    incident_id: str,
    approval_record_id: str,
    approval_record_sha256: str,
    representative_image_ref: Optional[str] = None,
    representative_image_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Validate a final-fact authority at the CSV/report/HWP boundary."""

    if not isinstance(value, Mapping) or set(value) != _OUTPUT_AUTHORITY_KEYS:
        raise ApprovedIncidentAuthorityError(
            "complete approved-Incident authority is required"
        )
    authority = dict(value)
    digest = authority.pop("authority_sha256")
    if (
        _sha(digest, "authority_sha256")
        != canonical_sha256(authority)
        or authority.get("schema_version")
        != APPROVED_INCIDENT_OUTPUT_AUTHORITY_SCHEMA
        or _uuid_text(authority.get("project_id"), "project_id")
        != str(project_id)
        or _uuid_text(
            authority.get("pipeline_job_id"), "pipeline_job_id"
        )
        != str(pipeline_job_id)
        or _uuid_text(
            authority.get("analysis_job_id"), "analysis_job_id"
        )
        != str(analysis_job_id)
        or _sha(authority.get("source_sha256"), "source_sha256")
        != source_sha256
        or _uuid_text(authority.get("incident_id"), "incident_id")
        != incident_id
        or authority.get("question_id") != incident_id
        or authority.get("approval_actor") != "local_user"
        or _uuid_text(
            authority.get("approval_record_id"),
            "approval_record_id",
        )
        != approval_record_id
        or _sha(
            authority.get("approval_record_sha256"),
            "approval_record_sha256",
        )
        != approval_record_sha256
    ):
        raise ApprovedIncidentAuthorityError(
            "approved-Incident authority crossed its exact approval boundary"
        )
    candidate_id = authority.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
        or str(uuid5(analysis_job_id, candidate_id)) != incident_id
        or isinstance(authority.get("answer_revision"), bool)
        or not isinstance(authority.get("answer_revision"), int)
        or authority["answer_revision"] < 1
        or authority.get("final_qa_choice")
        not in {"no_miss_complete", "miss_candidates_reviewed"}
    ):
        raise ApprovedIncidentAuthorityError(
            "approved-Incident question or final QA authority is invalid"
        )
    event_binding = _review_event_binding(
        authority.get("review_event_binding")
    )
    if event_binding["representative_candidate_id"] != candidate_id:
        raise ApprovedIncidentAuthorityError(
            "approved-Incident ReviewEvent crossed its candidate boundary"
        )
    for field in (
        "survey_incident_authority_sha256",
        "dataset_hash",
        "dataset_provenance_graph_sha256",
        "fixed_validation_sha256",
        "primary_artifact_sha256",
        "artifact_manifest_sha256",
        "source_analysis_lineage_sha256",
        "promoted_model_inference_sha256",
        "question_queue_revision",
        "answer_ledger_sha256",
        "candidate_metadata_sha256",
        "representative_image_sha256",
        "preview_clip_sha256",
        "final_missed_scene_qa_revision",
    ):
        _sha(authority.get(field), field)
    for field in ("model_version_id", "dataset_version_id"):
        _identifier(authority.get(field), field)
    for field in ("representative_image_ref", "preview_clip_ref"):
        _identifier(authority.get(field), field)
    if (
        representative_image_ref is not None
        and authority["representative_image_ref"]
        != representative_image_ref
    ) or (
        representative_image_sha256 is not None
        and authority["representative_image_sha256"]
        != representative_image_sha256
    ):
        raise ApprovedIncidentAuthorityError(
            "approved-Incident evidence authority changed"
        )
    return {**authority, "authority_sha256": digest}


__all__ = [
    "APPROVED_INCIDENT_OUTPUT_AUTHORITY_SCHEMA",
    "SURVEY_INCIDENT_AUTHORITY_SCHEMA",
    "ApprovedIncidentAuthorityError",
    "build_approved_incident_output_authority",
    "canonical_sha256",
    "validate_approved_incident_output_authority",
    "validate_survey_incident_authority",
]
