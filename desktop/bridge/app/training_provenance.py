"""Canonical project-scoped provenance from review truth to model training.

The objects in this module are path-free and self-digested.  An annotation
graph binds one answer revision to the immutable PipelineJob, AnalysisJob,
ReviewEventSet, candidate, image, geometry, and approval authority that
produced it.  A dataset graph freezes those annotation graphs with their
TRAIN/validation split and rejects cross-boundary reuse before a trainer can
run.

This module deliberately does not resolve files or mutate truth state.  It
only validates immutable identifiers and SHA-256 lineage supplied by the
server-side review boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional
from uuid import UUID, uuid5


ANNOTATION_PROVENANCE_SCHEMA = (
    "trafficvisson.annotation-training-provenance.v1"
)
DATASET_PROVENANCE_SCHEMA = (
    "trafficvisson.dataset-training-provenance.v1"
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_OVERLAY_ID = re.compile(r"^ceo_[a-f0-9]{32}$")
_REVIEW_SET_ID = re.compile(r"^res_[a-f0-9]{32}$")
_REVIEW_EVENT_ID = re.compile(r"^rve_[a-f0-9]{32}$")
_MAX_ANNOTATION_BYTES = 64 * 1024
_MAX_DATASET_BYTES = 16 * 1024 * 1024
_MAX_DATASET_MEMBERS = 100_000


class TrainingProvenanceError(ValueError):
    """Immutable training provenance is incomplete or inconsistent."""


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TrainingProvenanceError(
            "training provenance is not canonical JSON"
        ) from error


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TrainingProvenanceError(f"{field} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError) as error:
        raise TrainingProvenanceError(
            f"{field} must be a canonical UUID"
        ) from error
    if str(parsed) != value:
        raise TrainingProvenanceError(
            f"{field} must be a canonical UUID"
        )
    return value


def _sha(
    value: object,
    field: str,
    *,
    optional: bool = False,
) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TrainingProvenanceError(
            f"{field} must be a SHA-256 digest"
        )
    return value


def _bounded_identifier(
    value: object,
    field: str,
    pattern: re.Pattern[str],
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise TrainingProvenanceError(f"{field} is invalid")
    return value


def _answer_authority(value: object) -> dict[str, Any]:
    expected = {
        "answered_by",
        "include_in_training",
        "user_confirmed",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise TrainingProvenanceError(
            "answer authority fields are invalid"
        )
    answered_by = value["answered_by"]
    if answered_by not in {
        "local_user",
        "structured_llm_assisted",
    }:
        raise TrainingProvenanceError(
            "answer authority actor is invalid"
        )
    include_in_training = value["include_in_training"]
    user_confirmed = value["user_confirmed"]
    if (
        not isinstance(include_in_training, bool)
        or not isinstance(user_confirmed, bool)
        or user_confirmed != (answered_by == "local_user")
    ):
        raise TrainingProvenanceError(
            "answer authority state is invalid"
        )
    return {
        "answered_by": answered_by,
        "include_in_training": include_in_training,
        "user_confirmed": user_confirmed,
    }


def _delegation_authority(
    value: object,
    *,
    answered_by: str,
) -> Optional[dict[str, Any]]:
    if value is None:
        if answered_by != "local_user":
            raise TrainingProvenanceError(
                "structured answer delegation authority is missing"
            )
        return None
    expected = {
        "assist_provenance_sha256",
        "authority_kind",
        "authority_sha256",
        "delegated_proposal_sha256",
    }
    if (
        answered_by != "structured_llm_assisted"
        or not isinstance(value, Mapping)
        or set(value) != expected
        or value["authority_kind"]
        not in {"project_profile_delegation", "per_item_user_grant"}
    ):
        raise TrainingProvenanceError(
            "delegation authority fields are invalid"
        )
    return {
        "assist_provenance_sha256": _sha(
            value["assist_provenance_sha256"],
            "delegation_authority.assist_provenance_sha256",
        ),
        "authority_kind": value["authority_kind"],
        "authority_sha256": _sha(
            value["authority_sha256"],
            "delegation_authority.authority_sha256",
        ),
        "delegated_proposal_sha256": _sha(
            value["delegated_proposal_sha256"],
            "delegation_authority.delegated_proposal_sha256",
        ),
    }


def canonical_annotation_provenance_graph(
    value: object,
) -> dict[str, Any]:
    """Validate and return one canonical annotation provenance graph."""

    expected = {
        "schema_version",
        "project_id",
        "pipeline_job_id",
        "analysis_job_id",
        "overlay_id",
        "overlay_manifest_sha256",
        "candidate_set_metadata_sha256",
        "review_event_set_id",
        "review_event_set_revision",
        "review_event_set_manifest_sha256",
        "review_event_id",
        "review_event_revision",
        "question_id",
        "candidate_id",
        "answer_record_id",
        "answer_revision",
        "source_sha256",
        "candidate_metadata_sha256",
        "image_sha256",
        "mask_sha256",
        "proposal_sha256",
        "geometry_sha256",
        "answer_authority",
        "delegation_authority",
        "provenance_graph_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise TrainingProvenanceError(
            "annotation provenance fields are invalid"
        )
    if value["schema_version"] != ANNOTATION_PROVENANCE_SCHEMA:
        raise TrainingProvenanceError(
            "annotation provenance schema is unsupported"
        )
    project_id = _uuid(value["project_id"], "project_id")
    pipeline_job_id = _uuid(
        value["pipeline_job_id"], "pipeline_job_id"
    )
    analysis_job_id = _uuid(
        value["analysis_job_id"], "analysis_job_id"
    )
    question_id = _uuid(value["question_id"], "question_id")
    answer_record_id = _uuid(
        value["answer_record_id"], "answer_record_id"
    )
    candidate_id = _bounded_identifier(
        value["candidate_id"], "candidate_id", _CANDIDATE_ID
    )
    if question_id != str(uuid5(UUID(analysis_job_id), candidate_id)):
        raise TrainingProvenanceError(
            "annotation provenance question_id is not deterministic"
        )
    answer_revision = value["answer_revision"]
    if (
        isinstance(answer_revision, bool)
        or not isinstance(answer_revision, int)
        or answer_revision < 1
    ):
        raise TrainingProvenanceError(
            "annotation provenance answer_revision is invalid"
        )
    authority = _answer_authority(value["answer_authority"])
    delegation = _delegation_authority(
        value["delegation_authority"],
        answered_by=authority["answered_by"],
    )
    canonical = {
        "schema_version": ANNOTATION_PROVENANCE_SCHEMA,
        "project_id": project_id,
        "pipeline_job_id": pipeline_job_id,
        "analysis_job_id": analysis_job_id,
        "overlay_id": _bounded_identifier(
            value["overlay_id"], "overlay_id", _OVERLAY_ID
        ),
        "overlay_manifest_sha256": _sha(
            value["overlay_manifest_sha256"],
            "overlay_manifest_sha256",
        ),
        "candidate_set_metadata_sha256": _sha(
            value["candidate_set_metadata_sha256"],
            "candidate_set_metadata_sha256",
        ),
        "review_event_set_id": _bounded_identifier(
            value["review_event_set_id"],
            "review_event_set_id",
            _REVIEW_SET_ID,
        ),
        "review_event_set_revision": _sha(
            value["review_event_set_revision"],
            "review_event_set_revision",
        ),
        "review_event_set_manifest_sha256": _sha(
            value["review_event_set_manifest_sha256"],
            "review_event_set_manifest_sha256",
        ),
        "review_event_id": _bounded_identifier(
            value["review_event_id"],
            "review_event_id",
            _REVIEW_EVENT_ID,
        ),
        "review_event_revision": _sha(
            value["review_event_revision"],
            "review_event_revision",
        ),
        "question_id": question_id,
        "candidate_id": candidate_id,
        "answer_record_id": answer_record_id,
        "answer_revision": answer_revision,
        "source_sha256": _sha(
            value["source_sha256"], "source_sha256"
        ),
        "candidate_metadata_sha256": _sha(
            value["candidate_metadata_sha256"],
            "candidate_metadata_sha256",
        ),
        "image_sha256": _sha(
            value["image_sha256"], "image_sha256"
        ),
        "mask_sha256": _sha(
            value["mask_sha256"], "mask_sha256", optional=True
        ),
        "proposal_sha256": _sha(
            value["proposal_sha256"],
            "proposal_sha256",
            optional=True,
        ),
        "geometry_sha256": _sha(
            value["geometry_sha256"],
            "geometry_sha256",
            optional=True,
        ),
        "answer_authority": authority,
        "delegation_authority": delegation,
    }
    digest = _digest(canonical)
    if value["provenance_graph_sha256"] != digest:
        raise TrainingProvenanceError(
            "annotation provenance digest does not match"
        )
    result = {**canonical, "provenance_graph_sha256": digest}
    if len(_canonical_bytes(result)) > _MAX_ANNOTATION_BYTES:
        raise TrainingProvenanceError(
            "annotation provenance is too large"
        )
    return result


def build_annotation_provenance_graph(
    *,
    project_id: str,
    analysis_job_id: str,
    question_id: str,
    candidate_id: str,
    answer_record_id: str,
    answer_revision: int,
    include_in_training: bool,
    answered_by: str,
    candidate_metadata: Mapping[str, Any],
    source_sha256: Optional[str],
    candidate_metadata_sha256: Optional[str],
    training_geometry: Optional[Mapping[str, Any]],
    assist_provenance: Optional[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    """Build a graph from freshly resolved review evidence.

    ``None`` is returned only when the candidate has no PipelineJob /
    ReviewEventSet binding at all.  Partially present or contradictory
    provenance raises instead of silently degrading.
    """

    evidence = candidate_metadata.get("evidence_overlay")
    review = candidate_metadata.get("review_event_binding")
    image = candidate_metadata.get("representative_image")
    if evidence is None and review is None:
        return None
    if (
        not isinstance(evidence, Mapping)
        or not isinstance(review, Mapping)
        or not isinstance(image, Mapping)
        or source_sha256 is None
        or candidate_metadata_sha256 is None
    ):
        raise TrainingProvenanceError(
            "candidate review provenance is incomplete"
        )
    if (
        evidence.get("review_only") is not True
        or review.get("review_only") is not True
        or review.get("schema_version")
        != "trafficvisson.review-event-binding.v1"
        or review.get("representative_candidate_id") != candidate_id
    ):
        raise TrainingProvenanceError(
            "candidate review provenance crossed an approval boundary"
        )

    geometry_image_sha256 = None
    geometry_mask_sha256 = None
    geometry_proposal_sha256 = None
    geometry_sha256 = None
    if training_geometry is not None:
        geometry_image_sha256 = training_geometry.get("image_sha256")
        geometry_mask_sha256 = training_geometry.get("mask_sha256")
        geometry_proposal_sha256 = training_geometry.get(
            "proposal_sha256"
        )
        geometry_sha256 = training_geometry.get("geometry_sha256")

    image_sha256 = _sha(image.get("sha256"), "image.sha256")
    if (
        geometry_image_sha256 is not None
        and geometry_image_sha256 != image_sha256
    ):
        raise TrainingProvenanceError(
            "training geometry image crossed the reviewed candidate"
        )
    candidate_proposal_sha256 = candidate_metadata.get(
        "proposal_provenance_sha256"
    )
    if candidate_proposal_sha256 is not None:
        candidate_proposal_sha256 = _sha(
            candidate_proposal_sha256,
            "candidate.proposal_provenance_sha256",
        )
    if (
        geometry_proposal_sha256 is not None
        and candidate_proposal_sha256 is not None
        and geometry_proposal_sha256 != candidate_proposal_sha256
    ):
        raise TrainingProvenanceError(
            "training proposal crossed the reviewed candidate"
        )
    proposal_sha256 = (
        geometry_proposal_sha256 or candidate_proposal_sha256
    )

    delegation = None
    if answered_by == "structured_llm_assisted":
        if not isinstance(assist_provenance, Mapping):
            raise TrainingProvenanceError(
                "structured answer delegation provenance is missing"
            )
        assist_review = assist_provenance.get("review_event")
        if not isinstance(assist_review, Mapping):
            raise TrainingProvenanceError(
                "structured answer review provenance is missing"
            )
        expected_review = {
            "pipeline_job_id": evidence.get("pipeline_job_id"),
            "overlay_id": evidence.get("overlay_id"),
            "review_event_set_id": review.get(
                "review_event_set_id"
            ),
            "review_event_set_revision": review.get(
                "review_event_set_revision"
            ),
            "review_event_set_manifest_sha256": review.get(
                "review_event_set_manifest_sha256"
            ),
            "review_event_set_source_sha256": source_sha256,
            "review_event_id": review.get("review_event_id"),
            "review_event_revision": review.get(
                "review_event_revision"
            ),
        }
        if dict(assist_review) != expected_review:
            raise TrainingProvenanceError(
                "structured answer used another review event revision"
            )
        authority = assist_provenance.get("authority")
        if not isinstance(authority, Mapping):
            raise TrainingProvenanceError(
                "structured answer authority is missing"
            )
        delegation = {
            "assist_provenance_sha256": assist_provenance.get(
                "provenance_sha256"
            ),
            "authority_kind": authority.get("kind"),
            "authority_sha256": _digest(dict(authority)),
            "delegated_proposal_sha256": assist_provenance.get(
                "proposal_sha256"
            ),
        }
    elif assist_provenance is not None:
        raise TrainingProvenanceError(
            "human answer cannot claim delegation provenance"
        )

    raw = {
        "schema_version": ANNOTATION_PROVENANCE_SCHEMA,
        "project_id": project_id,
        "pipeline_job_id": evidence.get("pipeline_job_id"),
        "analysis_job_id": analysis_job_id,
        "overlay_id": evidence.get("overlay_id"),
        "overlay_manifest_sha256": evidence.get("manifest_sha256"),
        "candidate_set_metadata_sha256": evidence.get(
            "candidate_set_metadata_sha256"
        ),
        "review_event_set_id": review.get("review_event_set_id"),
        "review_event_set_revision": review.get(
            "review_event_set_revision"
        ),
        "review_event_set_manifest_sha256": review.get(
            "review_event_set_manifest_sha256"
        ),
        "review_event_id": review.get("review_event_id"),
        "review_event_revision": review.get("review_event_revision"),
        "question_id": question_id,
        "candidate_id": candidate_id,
        "answer_record_id": answer_record_id,
        "answer_revision": answer_revision,
        "source_sha256": source_sha256,
        "candidate_metadata_sha256": candidate_metadata_sha256,
        "image_sha256": image_sha256,
        "mask_sha256": geometry_mask_sha256,
        "proposal_sha256": proposal_sha256,
        "geometry_sha256": geometry_sha256,
        "answer_authority": {
            "answered_by": answered_by,
            "include_in_training": include_in_training,
            "user_confirmed": answered_by == "local_user",
        },
        "delegation_authority": delegation,
    }
    raw["provenance_graph_sha256"] = _digest(raw)
    return canonical_annotation_provenance_graph(raw)


def _dataset_member(value: object) -> dict[str, Any]:
    expected = {
        "annotation_id",
        "answer_record_id",
        "answer_revision",
        "analysis_job_id",
        "candidate_id",
        "question_id",
        "pipeline_job_id",
        "overlay_id",
        "overlay_manifest_sha256",
        "candidate_set_metadata_sha256",
        "review_event_set_id",
        "review_event_set_revision",
        "review_event_set_manifest_sha256",
        "review_event_id",
        "review_event_revision",
        "source_sha256",
        "candidate_metadata_sha256",
        "image_sha256",
        "mask_sha256",
        "proposal_sha256",
        "geometry_sha256",
        "provenance_graph_sha256",
        "split",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise TrainingProvenanceError(
            "dataset provenance member fields are invalid"
        )
    split = value["split"]
    if split not in {"train", "validation"}:
        raise TrainingProvenanceError(
            "dataset provenance member split is invalid"
        )
    revision = value["answer_revision"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
    ):
        raise TrainingProvenanceError(
            "dataset provenance answer revision is invalid"
        )
    return {
        "annotation_id": _uuid(
            value["annotation_id"], "member.annotation_id"
        ),
        "answer_record_id": _uuid(
            value["answer_record_id"], "member.answer_record_id"
        ),
        "answer_revision": revision,
        "analysis_job_id": _uuid(
            value["analysis_job_id"], "member.analysis_job_id"
        ),
        "candidate_id": _bounded_identifier(
            value["candidate_id"],
            "member.candidate_id",
            _CANDIDATE_ID,
        ),
        "question_id": _uuid(
            value["question_id"], "member.question_id"
        ),
        "pipeline_job_id": _uuid(
            value["pipeline_job_id"], "member.pipeline_job_id"
        ),
        "overlay_id": _bounded_identifier(
            value["overlay_id"], "member.overlay_id", _OVERLAY_ID
        ),
        "overlay_manifest_sha256": _sha(
            value["overlay_manifest_sha256"],
            "member.overlay_manifest_sha256",
        ),
        "candidate_set_metadata_sha256": _sha(
            value["candidate_set_metadata_sha256"],
            "member.candidate_set_metadata_sha256",
        ),
        "review_event_set_id": _bounded_identifier(
            value["review_event_set_id"],
            "member.review_event_set_id",
            _REVIEW_SET_ID,
        ),
        "review_event_set_revision": _sha(
            value["review_event_set_revision"],
            "member.review_event_set_revision",
        ),
        "review_event_set_manifest_sha256": _sha(
            value["review_event_set_manifest_sha256"],
            "member.review_event_set_manifest_sha256",
        ),
        "review_event_id": _bounded_identifier(
            value["review_event_id"],
            "member.review_event_id",
            _REVIEW_EVENT_ID,
        ),
        "review_event_revision": _sha(
            value["review_event_revision"],
            "member.review_event_revision",
        ),
        "source_sha256": _sha(
            value["source_sha256"], "member.source_sha256"
        ),
        "candidate_metadata_sha256": _sha(
            value["candidate_metadata_sha256"],
            "member.candidate_metadata_sha256",
        ),
        "image_sha256": _sha(
            value["image_sha256"], "member.image_sha256"
        ),
        "mask_sha256": _sha(
            value["mask_sha256"],
            "member.mask_sha256",
            optional=True,
        ),
        "proposal_sha256": _sha(
            value["proposal_sha256"],
            "member.proposal_sha256",
            optional=True,
        ),
        "geometry_sha256": _sha(
            value["geometry_sha256"],
            "member.geometry_sha256",
            optional=True,
        ),
        "provenance_graph_sha256": _sha(
            value["provenance_graph_sha256"],
            "member.provenance_graph_sha256",
        ),
        "split": split,
    }


def _lineage_values(member: Mapping[str, Any]) -> set[tuple[str, str]]:
    values: set[tuple[str, str]] = {
        ("candidate_id", str(member["candidate_id"])),
    }
    for field in (
        "source_sha256",
        "candidate_metadata_sha256",
        "image_sha256",
        "mask_sha256",
        "proposal_sha256",
        "geometry_sha256",
    ):
        value = member.get(field)
        if isinstance(value, str):
            values.add((field, value))
    return values


def canonical_dataset_provenance_graph(
    value: object,
) -> dict[str, Any]:
    """Validate one dataset graph and its split-leak/staleness invariants."""

    expected = {
        "schema_version",
        "project_id",
        "members",
        "train_lineage_sha256",
        "validation_lineage_sha256",
        "provenance_graph_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise TrainingProvenanceError(
            "dataset provenance fields are invalid"
        )
    if value["schema_version"] != DATASET_PROVENANCE_SCHEMA:
        raise TrainingProvenanceError(
            "dataset provenance schema is unsupported"
        )
    project_id = _uuid(value["project_id"], "project_id")
    raw_members = value["members"]
    if (
        not isinstance(raw_members, Sequence)
        or isinstance(raw_members, (str, bytes, bytearray))
        or not raw_members
        or len(raw_members) > _MAX_DATASET_MEMBERS
    ):
        raise TrainingProvenanceError(
            "dataset provenance members are invalid"
        )
    members = [_dataset_member(member) for member in raw_members]
    expected_order = sorted(
        members,
        key=lambda member: (
            member["annotation_id"],
            member["answer_revision"],
        ),
    )
    if members != expected_order:
        raise TrainingProvenanceError(
            "dataset provenance members are not canonical"
        )
    if len({member["annotation_id"] for member in members}) != len(
        members
    ):
        raise TrainingProvenanceError(
            "dataset provenance annotations are duplicated"
        )
    if any(
        member["question_id"]
        != str(
            uuid5(
                UUID(member["analysis_job_id"]),
                member["candidate_id"],
            )
        )
        for member in members
    ):
        raise TrainingProvenanceError(
            "dataset provenance question identity is invalid"
        )

    pipeline_analyses: dict[str, str] = {}
    overlay_revisions: dict[str, tuple[str, str, str]] = {}
    review_set_revisions: dict[str, tuple[str, str]] = {}
    review_event_revisions: dict[str, tuple[str, str]] = {}
    for member in members:
        pipeline = member["pipeline_job_id"]
        analysis = member["analysis_job_id"]
        previous_analysis = pipeline_analyses.setdefault(
            pipeline, analysis
        )
        if previous_analysis != analysis:
            raise TrainingProvenanceError(
                "pipeline job crossed analysis jobs"
            )
        overlay_id = member["overlay_id"]
        overlay_identity = (
            pipeline,
            member["overlay_manifest_sha256"],
            member["candidate_set_metadata_sha256"],
        )
        previous_overlay = overlay_revisions.setdefault(
            overlay_id, overlay_identity
        )
        if previous_overlay != overlay_identity:
            raise TrainingProvenanceError(
                "candidate overlay revision is stale or cross-job"
            )
        set_id = member["review_event_set_id"]
        set_identity = (
            member["review_event_set_revision"],
            member["review_event_set_manifest_sha256"],
        )
        previous_set_revision = review_set_revisions.setdefault(
            set_id, set_identity
        )
        if previous_set_revision != set_identity:
            raise TrainingProvenanceError(
                "review event set revision is stale"
            )
        event_id = member["review_event_id"]
        event_identity = (
            member["analysis_job_id"],
            member["review_event_revision"],
        )
        previous_event = review_event_revisions.setdefault(
            event_id, event_identity
        )
        if previous_event != event_identity:
            raise TrainingProvenanceError(
                "review event revision is stale or cross-job"
            )

    train_members = [
        member for member in members if member["split"] == "train"
    ]
    validation_members = [
        member
        for member in members
        if member["split"] == "validation"
    ]
    train_lineage = set().union(
        *(_lineage_values(member) for member in train_members)
    )
    validation_lineage = set().union(
        *(_lineage_values(member) for member in validation_members)
    )
    if train_lineage.intersection(validation_lineage):
        raise TrainingProvenanceError(
            "training and fixed-validation provenance overlap"
        )
    canonical_train_lineage = sorted(
        [list(item) for item in train_lineage]
    )
    canonical_validation_lineage = sorted(
        [list(item) for item in validation_lineage]
    )
    train_sha256 = _digest(canonical_train_lineage)
    validation_sha256 = _digest(canonical_validation_lineage)
    if (
        value["train_lineage_sha256"] != train_sha256
        or value["validation_lineage_sha256"]
        != validation_sha256
    ):
        raise TrainingProvenanceError(
            "dataset provenance split lineage digest does not match"
        )
    canonical = {
        "schema_version": DATASET_PROVENANCE_SCHEMA,
        "project_id": project_id,
        "members": members,
        "train_lineage_sha256": train_sha256,
        "validation_lineage_sha256": validation_sha256,
    }
    digest = _digest(canonical)
    if value["provenance_graph_sha256"] != digest:
        raise TrainingProvenanceError(
            "dataset provenance digest does not match"
        )
    result = {**canonical, "provenance_graph_sha256": digest}
    if len(_canonical_bytes(result)) > _MAX_DATASET_BYTES:
        raise TrainingProvenanceError(
            "dataset provenance is too large"
        )
    return result


def build_dataset_provenance_graph(
    *,
    project_id: str,
    members: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze annotation graphs and their assigned dataset splits."""

    values = []
    for member in members:
        graph = canonical_annotation_provenance_graph(
            member.get("provenance_graph")
        )
        if (
            graph["project_id"] != project_id
            or graph["analysis_job_id"]
            != member.get("analysis_job_id")
            or graph["question_id"] != member.get("question_id")
            or graph["candidate_id"] != member.get("candidate_id")
            or graph["answer_record_id"]
            != member.get("answer_record_id")
            or graph["answer_revision"]
            != member.get("answer_revision")
        ):
            raise TrainingProvenanceError(
                "annotation provenance crossed a dataset member boundary"
            )
        values.append(
            {
                "annotation_id": member.get("annotation_id"),
                "answer_record_id": graph["answer_record_id"],
                "answer_revision": graph["answer_revision"],
                "analysis_job_id": graph["analysis_job_id"],
                "candidate_id": graph["candidate_id"],
                "question_id": graph["question_id"],
                "pipeline_job_id": graph["pipeline_job_id"],
                "overlay_id": graph["overlay_id"],
                "overlay_manifest_sha256": graph[
                    "overlay_manifest_sha256"
                ],
                "candidate_set_metadata_sha256": graph[
                    "candidate_set_metadata_sha256"
                ],
                "review_event_set_id": graph[
                    "review_event_set_id"
                ],
                "review_event_set_revision": graph[
                    "review_event_set_revision"
                ],
                "review_event_set_manifest_sha256": graph[
                    "review_event_set_manifest_sha256"
                ],
                "review_event_id": graph["review_event_id"],
                "review_event_revision": graph[
                    "review_event_revision"
                ],
                "source_sha256": graph["source_sha256"],
                "candidate_metadata_sha256": graph[
                    "candidate_metadata_sha256"
                ],
                "image_sha256": graph["image_sha256"],
                "mask_sha256": graph["mask_sha256"],
                "proposal_sha256": graph["proposal_sha256"],
                "geometry_sha256": graph["geometry_sha256"],
                "provenance_graph_sha256": graph[
                    "provenance_graph_sha256"
                ],
                "split": member.get("split"),
            }
        )
    values.sort(
        key=lambda member: (
            str(member["annotation_id"]),
            int(member["answer_revision"]),
        )
    )
    train_lineage = set().union(
        *(
            _lineage_values(member)
            for member in values
            if member["split"] == "train"
        )
    )
    validation_lineage = set().union(
        *(
            _lineage_values(member)
            for member in values
            if member["split"] == "validation"
        )
    )
    raw = {
        "schema_version": DATASET_PROVENANCE_SCHEMA,
        "project_id": project_id,
        "members": values,
        "train_lineage_sha256": _digest(
            sorted([list(item) for item in train_lineage])
        ),
        "validation_lineage_sha256": _digest(
            sorted([list(item) for item in validation_lineage])
        ),
    }
    raw["provenance_graph_sha256"] = _digest(raw)
    return canonical_dataset_provenance_graph(raw)


__all__ = [
    "ANNOTATION_PROVENANCE_SCHEMA",
    "DATASET_PROVENANCE_SCHEMA",
    "TrainingProvenanceError",
    "build_annotation_provenance_graph",
    "build_dataset_provenance_graph",
    "canonical_annotation_provenance_graph",
    "canonical_dataset_provenance_graph",
]
