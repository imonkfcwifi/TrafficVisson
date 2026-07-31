"""Incrementally append one local-user-discovered candidate to review.

The service consumes the already persisted pre-review PipelineJob checkpoints,
adds exactly one review-only candidate, re-hashes every affected checkpoint,
and delegates immutable event construction to :class:`ReviewEventSetStore`.
It never records an answer, Annotation, Incident, DatasetVersion membership,
maintenance decision, report fact, model promotion, or HWP edit.

The candidate evidence itself is resolved by ``ReviewEventSetStore`` through
the server-owned composite evidence resolver.  Consequently this module
accepts only path-free identities and provenance, not renderer paths or bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from uuid import UUID

from .review_event_set import ReviewEventSetResult, ReviewEventSetStore
from .user_discovered_candidate import (
    SAM2_USER_DISCOVERY_REQUEST_SCHEMA,
    UNCLASSIFIED_REVIEW_ISSUE_TYPE,
    USER_DISCOVERED_CANDIDATE_SCHEMA,
    USER_DISCOVERED_PROPOSAL_SOURCE,
)


USER_DISCOVERED_REVIEW_QUEUE_SCHEMA = (
    "trafficvisson.user-discovered-review-queue.v1"
)
USER_DISCOVERED_OCR_INCREMENT_SCHEMA = (
    "trafficvisson.user-discovered-ocr-increment.v1"
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_OVERLAY_ID = re.compile(r"^ceo_[a-f0-9]{32}$")
_ISSUE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUIRED_STAGE_KEYS = {
    "detection_ocr",
    "sam2_segmentation_tracking",
    "temporal_merge",
    "similar_case_search",
    "vision_proposal",
    "question_queue",
}
_TRUTH_COUNTS = {
    "annotation_count",
    "annotations_created",
    "incident_count",
    "incidents_created",
    "training_membership_count",
    "training_members_created",
    "report_incident_count",
    "report_truth_records_created",
}


class UserDiscoveredReviewQueueError(RuntimeError):
    """Stable, caller-safe failure for incremental review materialization."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UserDiscoveredReviewQueueResult:
    schema_version: str
    project_id: str
    analysis_job_id: str
    pipeline_job_id: str
    source_sha256: str
    composite_overlay_id: str
    candidate_id: str
    prior_question_candidate_ids: tuple[str, ...]
    question_candidate_ids: tuple[str, ...]
    delta_question_candidate_ids: tuple[str, ...]
    user_issue_type_proposal: Optional[str]
    review_issue_type: str
    delta_stage_checkpoints: Mapping[str, Mapping[str, Any]]
    question_queue_checkpoint: Mapping[str, Any]
    delta_review_event_set: Mapping[str, Any]
    delta_event_binding: Mapping[str, Any]
    idempotent_replay: bool
    review_only: bool = True
    annotations_created: int = 0
    incidents_created: int = 0
    training_members_created: int = 0
    report_truth_records_created: int = 0

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "analysis_job_id": self.analysis_job_id,
            "pipeline_job_id": self.pipeline_job_id,
            "source_sha256": self.source_sha256,
            "composite_overlay_id": self.composite_overlay_id,
            "candidate_id": self.candidate_id,
            "prior_question_candidate_ids": list(
                self.prior_question_candidate_ids
            ),
            "question_candidate_ids": list(self.question_candidate_ids),
            "delta_question_candidate_ids": list(
                self.delta_question_candidate_ids
            ),
            "user_issue_type_proposal": self.user_issue_type_proposal,
            "review_issue_type": self.review_issue_type,
            "delta_stage_checkpoints": {
                key: dict(value)
                for key, value in self.delta_stage_checkpoints.items()
            },
            "question_queue_checkpoint": dict(
                self.question_queue_checkpoint
            ),
            "delta_review_event_set": dict(
                self.delta_review_event_set
            ),
            "delta_event_binding": dict(self.delta_event_binding),
            "idempotent_replay": self.idempotent_replay,
            "review_only": True,
            "annotations_created": 0,
            "incidents_created": 0,
            "training_members_created": 0,
            "report_truth_records_created": 0,
        }


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "review queue input is not canonical JSON",
        ) from error


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _clone_mapping(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            f"{label} must be a mapping",
        )
    parsed = json.loads(_canonical_json(value).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            f"{label} must be a JSON object",
        )
    return parsed


def _candidate_ids(value: Any, *, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) != len(set(value))
        or any(
            not isinstance(item, str)
            or _CANDIDATE_ID.fullmatch(item) is None
            for item in value
        )
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            f"{label} is invalid",
        )
    return list(value)


def _rebind_identity(
    checkpoint: dict[str, Any],
    *,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    composite_overlay_id: str,
    source_sha256: str,
) -> None:
    checkpoint.update(
        {
            "analysis_job_id": str(analysis_job_id),
            "pipeline_job_id": str(pipeline_job_id),
            "overlay_id": composite_overlay_id,
            "source_sha256": source_sha256,
            "review_only": True,
        }
    )
    for field in _TRUTH_COUNTS:
        if field in checkpoint:
            checkpoint[field] = 0


def _validated_user_evidence(
    value: Mapping[str, Any],
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    source_sha256: str,
) -> tuple[dict[str, Any], str, Optional[str], str, float]:
    evidence = _clone_mapping(value, label="user-discovered evidence")
    candidate_id = evidence.get("candidate_id")
    issue_type = evidence.get("issue_type_proposal")
    declared_review_issue_type = evidence.get(
        "review_issue_type"
    )
    timestamp = evidence.get("selected_frame_timestamp_seconds")
    if (
        evidence.get("schema_version")
        != USER_DISCOVERED_CANDIDATE_SCHEMA
        or evidence.get("candidate_kind") != "user_discovered"
        or evidence.get("project_id") != str(project_id)
        or evidence.get("analysis_job_id") != str(analysis_job_id)
        or evidence.get("pipeline_job_id") != str(pipeline_job_id)
        or evidence.get("source_sha256") != source_sha256
        or not isinstance(candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate_id) is None
        or not isinstance(evidence.get("overlay_id"), str)
        or _OVERLAY_ID.fullmatch(evidence["overlay_id"]) is None
        or not isinstance(
            evidence.get("overlay_manifest_sha256"), str
        )
        or _SHA256.fullmatch(
            evidence["overlay_manifest_sha256"]
        )
        is None
        or evidence.get("review_only") is not True
        or any(evidence.get(field, 0) != 0 for field in _TRUTH_COUNTS)
        or isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
        or float(timestamp) < 0
        or (
            issue_type is not None
            and (
                not isinstance(issue_type, str)
                or _ISSUE_TYPE.fullmatch(issue_type) is None
            )
        )
        or (
            evidence.get("sam2_refinement_request") is not None
            and not isinstance(
                evidence.get("sam2_refinement_request"),
                Mapping,
            )
        )
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "user-discovered evidence crossed its review-only binding",
        )
    review_issue_type = (
        issue_type or UNCLASSIFIED_REVIEW_ISSUE_TYPE
    )
    if (
        declared_review_issue_type is not None
        and declared_review_issue_type != review_issue_type
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "user-discovered issue proposal was rebound",
        )
    return (
        evidence,
        candidate_id,
        issue_type,
        review_issue_type,
        round(float(timestamp), 6),
    )


def _increment_detection_and_ocr(
    checkpoint: Mapping[str, Any],
    *,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    composite_overlay_id: str,
    source_sha256: str,
    candidate_id: str,
    review_issue_type: str,
) -> dict[str, Any]:
    value = _clone_mapping(checkpoint, label="detection/OCR checkpoint")
    previous_proposal_ids = _candidate_ids(
        value.get("proposal_candidate_ids"),
        label="detector candidate ids",
    )
    if candidate_id in previous_proposal_ids:
        raise UserDiscoveredReviewQueueError(
            "user_discovered_candidate_already_queued",
            "user-discovered candidate is already in the detector checkpoint",
        )
    previous_ocr_candidate_ids = _candidate_ids(
        value.get("ocr_candidate_ids", []),
        label="OCR candidate ids",
    )
    previous_ocr_revision = value.get("ocr_revision")
    if (
        not isinstance(previous_ocr_revision, str)
        or _SHA256.fullmatch(previous_ocr_revision) is None
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "OCR revision is invalid",
        )
    _rebind_identity(
        value,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        composite_overlay_id=composite_overlay_id,
        source_sha256=source_sha256,
    )
    # This is a delta materialization checkpoint.  It carries the immutable
    # model/runtime identities from the original stage, but candidate rows
    # describe only the local-user-discovered miss.  It must never claim that
    # DINO, OCR, or the dedicated detector found this candidate.
    proposal_ids = [candidate_id]
    issue_types = [review_issue_type]
    ocr_candidate_ids: list[str] = []
    value.update(
        {
            "proposal_candidate_ids": proposal_ids,
            "proposal_sources": [USER_DISCOVERED_PROPOSAL_SOURCE],
            "detected_issue_types": issue_types,
            "dino_evidence_source": (
                "local_user_discovered_review_delta"
            ),
            "high_frequency_evidence_count": 0,
            "high_frequency_manifest_sha256": None,
            "ocr_candidate_ids": ocr_candidate_ids,
            "ocr_observation_count": 0,
            "ocr_observations_by_candidate": {},
            "observations_by_candidate": {},
            "review_only": True,
            "automatic_labeling_allowed": False,
            "automatic_annotation_allowed": False,
        }
    )
    value.pop("dino_review_proposals_revision", None)
    value.pop("dino_review_proposals_by_candidate", None)
    detection_core = {
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "local_inference_revision": value.get(
            "local_inference_revision"
        ),
        "dino_revision": value.get("dino_revision"),
        "dino_evidence_source": value.get("dino_evidence_source"),
        "high_frequency_evidence_count": value.get(
            "high_frequency_evidence_count"
        ),
        "high_frequency_manifest_sha256": value.get(
            "high_frequency_manifest_sha256"
        ),
        "proposal_candidate_ids": proposal_ids,
        "detected_issue_types": issue_types,
        "ocr_candidate_ids": sorted(ocr_candidate_ids),
    }
    value["detection_revision"] = _sha256_json(detection_core)

    pending = {candidate_id: "not_run"}
    value["user_discovered_ocr_status_by_candidate"] = pending
    ocr_increment = {
        "schema_version": USER_DISCOVERED_OCR_INCREMENT_SCHEMA,
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "previous_ocr_revision": previous_ocr_revision,
        "ocr_candidate_ids": ocr_candidate_ids,
        "prior_ocr_candidate_ids": previous_ocr_candidate_ids,
        "user_discovered_status_by_candidate": pending,
        "review_only": True,
    }
    new_ocr_revision = _sha256_json(ocr_increment)
    value["ocr_revision"] = new_ocr_revision
    provenance = _clone_mapping(
        value.get("ocr_provenance", value.get("provenance")),
        label="OCR provenance",
    )
    provenance.update(
        {
            "schema_version": (
                "trafficvisson.pipeline-ocr-provenance.v1"
            ),
            "engine_role": "ocr_only",
            "input_revision": previous_ocr_revision,
            "result_revision": new_ocr_revision,
            "review_only": True,
            "path_exposed": False,
            "user_discovered_increment": ocr_increment,
        }
    )
    value["ocr_provenance"] = provenance
    return value


def _increment_sam2(
    checkpoint: Mapping[str, Any],
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    composite_overlay_id: str,
    source_sha256: str,
    candidate_id: str,
    sam2_request: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    value = _clone_mapping(checkpoint, label="SAM2 checkpoint")
    _rebind_identity(
        value,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        composite_overlay_id=composite_overlay_id,
        source_sha256=source_sha256,
    )
    # The delta ReviewEventSet must not re-bind the immutable SAM2 outputs of
    # older questions.  Only the new candidate's optional pending request is
    # represented here.
    value.update(
        {
            "state": "pending_user_discovered_refinement",
            "proposals": [],
            "failures": [],
            "dino_prompt_bindings": [],
            "dino_prompt_count": 0,
            "dino_mask_count": 0,
            "dino_failure_count": 0,
            "dino_box_only_review_count": 0,
            "mask_generated": False,
            "tracking_performed": False,
            "review_only": True,
            "requires_user_confirmation": True,
            "automatic_annotation_allowed": False,
            "automatic_incident_creation_allowed": False,
            "training_membership_allowed": False,
            "annotation_count": 0,
            "incident_count": 0,
        }
    )
    pending: dict[str, Any] = {}
    if sam2_request is not None:
        request = _clone_mapping(sam2_request, label="pending SAM2 request")
        request_revision = request.get("request_revision")
        request_core = dict(request)
        request_core.pop("request_revision", None)
        if (
            request.get("schema_version")
            != SAM2_USER_DISCOVERY_REQUEST_SCHEMA
            or request.get("project_id") != str(project_id)
            or request.get("analysis_job_id") != str(analysis_job_id)
            or request.get("pipeline_job_id") != str(pipeline_job_id)
            or request.get("review_only") is not True
            or request.get("automatic_annotation_creation") is not False
            or request.get("automatic_incident_creation") is not False
            or request.get("automatic_training_ingest") is not False
            or not isinstance(request_revision, str)
            or _SHA256.fullmatch(request_revision) is None
            or _sha256_json(request_core) != request_revision
        ):
            raise UserDiscoveredReviewQueueError(
                "user_discovered_review_queue_invalid",
                "pending SAM2 request is invalid",
            )
        pending[candidate_id] = request
        status = "pending"
    else:
        status = "not_requested"
    value["user_discovered_pending_requests_by_candidate"] = pending
    statuses = {candidate_id: status}
    value["user_discovered_sam2_status_by_candidate"] = statuses
    value.pop("sam2_revision", None)
    value["sam2_revision"] = _sha256_json(value)
    return value


def _increment_temporal(
    checkpoint: Mapping[str, Any],
    *,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    composite_overlay_id: str,
    source_sha256: str,
    candidate_id: str,
    prior_candidate_ids: list[str],
    detection_revision: str,
    review_issue_type: str,
    timestamp_seconds: float,
) -> dict[str, Any]:
    value = _clone_mapping(checkpoint, label="temporal checkpoint")
    _rebind_identity(
        value,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        composite_overlay_id=composite_overlay_id,
        source_sha256=source_sha256,
    )
    temporal_ids = _candidate_ids(
        value.get("question_candidate_ids"),
        label="temporal question candidate ids",
    )
    if temporal_ids != prior_candidate_ids:
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_conflict",
            "question queue and temporal order differ",
        )
    groups = value.get("groups")
    if not isinstance(groups, list):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "temporal groups are invalid",
        )
    previous_temporal_revision = value.get(
        "temporal_merge_revision"
    )
    if (
        not isinstance(previous_temporal_revision, str)
        or _SHA256.fullmatch(previous_temporal_revision) is None
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "temporal revision is invalid",
        )
    groups = [
        {
            "proposal_id": f"user-discovered-{candidate_id}",
            "representative_candidate_id": candidate_id,
            "evidence_candidate_ids": [candidate_id],
            "issue_type": review_issue_type,
            "start_seconds": timestamp_seconds,
            "end_seconds": timestamp_seconds,
            "detection_count": 1,
        }
    ]
    question_ids = [candidate_id]
    value["detection_revision"] = detection_revision
    value["question_candidate_ids"] = question_ids
    value["groups"] = groups
    value["previous_temporal_merge_revision"] = (
        previous_temporal_revision
    )
    value.pop("dino_temporal_groups_by_candidate", None)
    value.pop("dino_candidate_evidence_overlay", None)
    core: dict[str, Any] = {
        "analysis_job_id": str(analysis_job_id),
        "evaluation_id": value.get("evaluation_id"),
        "detection_revision": detection_revision,
        "question_candidate_ids": question_ids,
        "groups": groups,
    }
    value["temporal_merge_revision"] = _sha256_json(core)
    return value


def _increment_similar_cases(
    checkpoint: Mapping[str, Any],
    *,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    composite_overlay_id: str,
    source_sha256: str,
    candidate_id: str,
) -> dict[str, Any]:
    value = _clone_mapping(checkpoint, label="similar-case checkpoint")
    _rebind_identity(
        value,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        composite_overlay_id=composite_overlay_id,
        source_sha256=source_sha256,
    )
    index_sha = value.get("visual_index_manifest_sha256")
    matches = value.get("matches_by_candidate")
    if (
        not isinstance(index_sha, str)
        or _SHA256.fullmatch(index_sha) is None
        or not isinstance(matches, Mapping)
        or candidate_id in matches
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "similar-case checkpoint is invalid",
        )
    previous_visual_revision = value.get(
        "visual_reference_revision"
    )
    if (
        not isinstance(previous_visual_revision, str)
        or _SHA256.fullmatch(previous_visual_revision) is None
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "visual-reference revision is invalid",
        )
    updated_matches = {candidate_id: []}
    value["matches_by_candidate"] = updated_matches
    value["visual_reference_revision"] = _sha256_json(
        {
            "visual_index_manifest_sha256": index_sha,
            "matches_by_candidate": updated_matches,
        }
    )
    value["previous_visual_reference_revision"] = (
        previous_visual_revision
    )
    value["user_discovered_status_by_candidate"] = {
        candidate_id: "pending_review"
    }
    return value


def _increment_structured(
    checkpoint: Mapping[str, Any],
    *,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    composite_overlay_id: str,
    source_sha256: str,
    candidate_id: str,
    detection_revision: str,
    temporal_revision: str,
    visual_revision: str,
) -> dict[str, Any]:
    value = _clone_mapping(
        checkpoint, label="structured proposal checkpoint"
    )
    _rebind_identity(
        value,
        analysis_job_id=analysis_job_id,
        pipeline_job_id=pipeline_job_id,
        composite_overlay_id=composite_overlay_id,
        source_sha256=source_sha256,
    )
    judgments = value.get("structured_judgments_by_candidate")
    if not isinstance(judgments, Mapping) or candidate_id in judgments:
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "structured proposal checkpoint is invalid",
        )
    previous_structured_revision = value.get(
        "structured_vision_revision"
    )
    if (
        not isinstance(previous_structured_revision, str)
        or _SHA256.fullmatch(previous_structured_revision) is None
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "structured proposal revision is invalid",
    )
    updated_judgments = {
        candidate_id: {
            "schema_version": (
                "trafficvisson.structured-vision-proposal.v1"
            ),
            "status": "not_run",
            "provider": "not_invoked",
            "candidate_id": candidate_id,
            "comparison_mode": "metadata_comparison_only",
            "review_state": "none",
            "review_only": True,
            "automatic_annotation_allowed": False,
            "automatic_incident_allowed": False,
            "automatic_training_membership_allowed": False,
            "automatic_report_truth_allowed": False,
            "llm_bulk_frame_detection_used": False,
            "direct_visual_inspection_claimed": False,
            "proposal": None,
            "proposal_sha256": None,
            "error_code": "user_discovered_pending_review",
        }
    }
    value["detection_revision"] = detection_revision
    value["temporal_merge_revision"] = temporal_revision
    value["structured_judgments_by_candidate"] = updated_judgments
    value["structured_vision_status"] = "partial"
    value["structured_llm_semantics_status"] = "partial"
    value["structured_proposal_count"] = 0
    value["structured_not_run_count"] = 1
    value["structured_failure_count"] = 0
    value["previous_structured_vision_revision"] = (
        previous_structured_revision
    )
    value.pop("provider_visual_evidence_by_candidate", None)
    core: dict[str, Any] = {
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "detection_revision": detection_revision,
        "temporal_merge_revision": temporal_revision,
        "visual_reference_revision": visual_revision,
        "provider": value.get("selected_provider"),
        "provider_state": value.get("provider_state"),
        "transmission_consent": value.get("transmission_consent"),
        "judgments": updated_judgments,
    }
    value["structured_vision_revision"] = _sha256_json(core)
    return value


def _updated_question_queue(
    checkpoint: Mapping[str, Any],
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    source_sha256: str,
    candidate_id: str,
    question_candidate_ids: list[str],
    structured_checkpoint: Mapping[str, Any],
    review_event_result: ReviewEventSetResult,
) -> dict[str, Any]:
    value = _clone_mapping(checkpoint, label="question queue checkpoint")
    previous_revision = value.get("question_queue_revision")
    if (
        not isinstance(previous_revision, str)
        or _SHA256.fullmatch(previous_revision) is None
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "question queue revision is invalid",
        )
    if len(review_event_result.events) != 1:
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_materialization_invalid",
            "delta review-event set must contain exactly one event",
        )
    event = review_event_result.events[0]
    if event.representative_candidate_id != candidate_id:
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_materialization_invalid",
            "delta review-event binding does not match the new candidate",
        )
    event_checkpoint = review_event_result.to_checkpoint()
    delta_binding = {
        "review_event_set_id": (
            review_event_result.review_event_set_id
        ),
        "review_event_set_revision": (
            review_event_result.review_event_set_revision
        ),
        "review_event_set_manifest_sha256": (
            review_event_result.manifest_sha256
        ),
        "event_id": event.event_id,
        "representative_candidate_id": (
            event.representative_candidate_id
        ),
        "event_revision": event.event_revision,
        "review_only": True,
    }
    prior_bindings = value.get("review_event_bindings", [])
    prior_event_ids = value.get("review_event_ids", [])
    if (
        not isinstance(prior_bindings, list)
        or not isinstance(prior_event_ids, list)
        or any(not isinstance(item, Mapping) for item in prior_bindings)
        or any(not isinstance(item, str) for item in prior_event_ids)
        or candidate_id
        in {
            str(item.get("representative_candidate_id"))
            for item in prior_bindings
        }
        or event.event_id in prior_event_ids
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "existing review-event bindings are invalid",
        )
    event_bindings = [
        *[dict(item) for item in prior_bindings],
        delta_binding,
    ]
    event_ids = [*prior_event_ids, event.event_id]

    prior_question_ids = question_candidate_ids[:-1]
    human_ids = _candidate_ids(
        value.get(
            "human_question_candidate_ids",
            prior_question_ids,
        ),
        label="human question candidate ids",
    )
    delegated_ids = _candidate_ids(
        value.get(
            "structured_training_label_candidate_ids",
            [],
        ),
        label="delegated training candidate ids",
    )
    mandatory_ids = _candidate_ids(
        value.get("mandatory_human_candidate_ids", []),
        label="mandatory human candidate ids",
    )
    if (
        set(human_ids).intersection(delegated_ids)
        or set(human_ids).union(delegated_ids)
        != set(prior_question_ids)
        or not set(mandatory_ids).issubset(human_ids)
    ):
        raise UserDiscoveredReviewQueueError(
            "user_discovered_review_queue_invalid",
            "existing question partitions are invalid",
        )
    human_ids = [*human_ids, candidate_id]
    mandatory_ids = [*mandatory_ids, candidate_id]
    aggregate_truth_counts = {}
    for field in (
        "annotation_count",
        "incident_count",
        "training_membership_count",
        "report_incident_count",
    ):
        count = value.get(field, 0)
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise UserDiscoveredReviewQueueError(
                "user_discovered_review_queue_invalid",
                "existing question truth counts are invalid",
            )
        aggregate_truth_counts[field] = count
    value.update(
        {
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "question_count": len(question_candidate_ids),
            "question_candidate_ids": question_candidate_ids,
            "human_question_count": len(human_ids),
            "human_question_candidate_ids": human_ids,
            "structured_training_label_count": len(
                delegated_ids
            ),
            "structured_training_label_candidate_ids": (
                delegated_ids
            ),
            "mandatory_human_candidate_ids": mandatory_ids,
            "review_event_count": len(event_bindings),
            "review_event_ids": event_ids,
            "review_event_bindings": event_bindings,
            "user_discovered_delta_review_event_set": (
                event_checkpoint
            ),
            "user_discovered_delta_event_binding": (
                delta_binding
            ),
            "user_discovered_delta_checkpoint_revisions": {
                "detection_revision": structured_checkpoint[
                    "detection_revision"
                ],
                "temporal_merge_revision": structured_checkpoint[
                    "temporal_merge_revision"
                ],
                "structured_vision_revision": structured_checkpoint[
                    "structured_vision_revision"
                ],
            },
            "user_discovered_structured_judgment": dict(
                structured_checkpoint[
                    "structured_judgments_by_candidate"
                ][candidate_id]
            ),
            "review_event_collection_refresh_required": True,
            # The enlarged set must be re-registered atomically by the
            # integration layer before any answer can project.
            "question_catalog_state": "sidecar_only",
            "question_catalog_revision": None,
            "question_catalog_refresh_required": True,
            "review_only": True,
            **aggregate_truth_counts,
            "user_discovered_annotation_count": 0,
            "user_discovered_incident_count": 0,
            "user_discovered_training_membership_count": 0,
            "user_discovered_report_incident_count": 0,
        }
    )
    revision_core = {
        "schema_version": USER_DISCOVERED_REVIEW_QUEUE_SCHEMA,
        "project_id": str(project_id),
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "previous_question_queue_revision": previous_revision,
        "question_candidate_ids": question_candidate_ids,
        "human_question_candidate_ids": human_ids,
        "structured_training_label_candidate_ids": delegated_ids,
        "mandatory_human_candidate_ids": mandatory_ids,
        "delta_review_event_set_id": (
            review_event_result.review_event_set_id
        ),
        "delta_review_event_set_revision": (
            review_event_result.review_event_set_revision
        ),
        "delta_review_event_set_manifest_sha256": (
            review_event_result.manifest_sha256
        ),
        "prior_event_bindings": [
            dict(item) for item in prior_bindings
        ],
        "delta_event_binding": delta_binding,
        "aggregate_truth_counts": aggregate_truth_counts,
        "new_candidate_truth_counts": {
            "annotation_count": 0,
            "incident_count": 0,
            "training_membership_count": 0,
            "report_incident_count": 0,
        },
        "review_only": True,
    }
    value["user_discovered_queue_revision_core"] = revision_core
    value["question_queue_revision"] = _sha256_json(revision_core)
    return value


class UserDiscoveredReviewQueueService:
    """Append one missed candidate without replacing prior immutable events."""

    def __init__(self, review_event_sets: ReviewEventSetStore) -> None:
        if not isinstance(review_event_sets, ReviewEventSetStore):
            raise ValueError(
                "review_event_sets must be a ReviewEventSetStore"
            )
        self._review_event_sets = review_event_sets

    def augment(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        composite_overlay_id: str,
        source_sha256: str,
        stage_checkpoints: Mapping[str, Mapping[str, Any]],
        user_discovered_evidence: Mapping[str, Any],
    ) -> UserDiscoveredReviewQueueResult:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(analysis_job_id, UUID)
            or not isinstance(pipeline_job_id, UUID)
            or not isinstance(composite_overlay_id, str)
            or _OVERLAY_ID.fullmatch(composite_overlay_id) is None
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(stage_checkpoints, Mapping)
            or not _REQUIRED_STAGE_KEYS.issubset(stage_checkpoints)
        ):
            raise UserDiscoveredReviewQueueError(
                "user_discovered_review_queue_invalid",
                "incremental review queue identity is invalid",
            )
        (
            evidence,
            candidate_id,
            issue_type_proposal,
            review_issue_type,
            timestamp_seconds,
        ) = _validated_user_evidence(
            user_discovered_evidence,
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            source_sha256=source_sha256,
        )
        question_queue = _clone_mapping(
            stage_checkpoints["question_queue"],
            label="question queue checkpoint",
        )
        prior_ids = _candidate_ids(
            question_queue.get("question_candidate_ids"),
            label="question queue candidate ids",
        )
        if candidate_id in prior_ids:
            raise UserDiscoveredReviewQueueError(
                "user_discovered_candidate_already_queued",
                "user-discovered candidate is already queued",
            )

        detection = _increment_detection_and_ocr(
            stage_checkpoints["detection_ocr"],
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            composite_overlay_id=composite_overlay_id,
            source_sha256=source_sha256,
            candidate_id=candidate_id,
            review_issue_type=review_issue_type,
        )
        sam2_request = evidence.get("sam2_refinement_request")
        sam2 = _increment_sam2(
            stage_checkpoints["sam2_segmentation_tracking"],
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            composite_overlay_id=composite_overlay_id,
            source_sha256=source_sha256,
            candidate_id=candidate_id,
            sam2_request=(
                sam2_request
                if isinstance(sam2_request, Mapping)
                else None
            ),
        )
        temporal = _increment_temporal(
            stage_checkpoints["temporal_merge"],
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            composite_overlay_id=composite_overlay_id,
            source_sha256=source_sha256,
            candidate_id=candidate_id,
            prior_candidate_ids=prior_ids,
            detection_revision=detection["detection_revision"],
            review_issue_type=review_issue_type,
            timestamp_seconds=timestamp_seconds,
        )
        similar = _increment_similar_cases(
            stage_checkpoints["similar_case_search"],
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            composite_overlay_id=composite_overlay_id,
            source_sha256=source_sha256,
            candidate_id=candidate_id,
        )
        structured = _increment_structured(
            stage_checkpoints["vision_proposal"],
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            composite_overlay_id=composite_overlay_id,
            source_sha256=source_sha256,
            candidate_id=candidate_id,
            detection_revision=detection["detection_revision"],
            temporal_revision=temporal["temporal_merge_revision"],
            visual_revision=similar["visual_reference_revision"],
        )
        review_events = self._review_event_sets.materialize(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            overlay_id=composite_overlay_id,
            source_sha256=source_sha256,
            dino_checkpoint=detection,
            ocr_checkpoint=detection,
            sam2_checkpoint=sam2,
            temporal_checkpoint=temporal,
            similar_case_checkpoint=similar,
            structured_proposal_checkpoint=structured,
            status="completed",
        )
        full_question_ids = [*prior_ids, candidate_id]
        delta_question_ids = [candidate_id]
        if (
            [event.representative_candidate_id for event in review_events.events]
            != delta_question_ids
            or review_events.review_only is not True
            or review_events.annotations_created != 0
            or review_events.incidents_created != 0
            or review_events.training_members_created != 0
            or review_events.report_truth_records_created != 0
        ):
            raise UserDiscoveredReviewQueueError(
                "user_discovered_review_queue_materialization_invalid",
                "incremental review-event set crossed its truth boundary",
            )
        updated_question_queue = _updated_question_queue(
            question_queue,
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            source_sha256=source_sha256,
            candidate_id=candidate_id,
            question_candidate_ids=full_question_ids,
            structured_checkpoint=structured,
            review_event_result=review_events,
        )
        delta_stages = {
            "detection_ocr": detection,
            "sam2_segmentation_tracking": sam2,
            "temporal_merge": temporal,
            "similar_case_search": similar,
            "vision_proposal": structured,
        }
        return UserDiscoveredReviewQueueResult(
            schema_version=USER_DISCOVERED_REVIEW_QUEUE_SCHEMA,
            project_id=str(project_id),
            analysis_job_id=str(analysis_job_id),
            pipeline_job_id=str(pipeline_job_id),
            source_sha256=source_sha256,
            composite_overlay_id=composite_overlay_id,
            candidate_id=candidate_id,
            prior_question_candidate_ids=tuple(prior_ids),
            question_candidate_ids=tuple(full_question_ids),
            delta_question_candidate_ids=tuple(
                delta_question_ids
            ),
            user_issue_type_proposal=issue_type_proposal,
            review_issue_type=review_issue_type,
            delta_stage_checkpoints=delta_stages,
            question_queue_checkpoint=updated_question_queue,
            delta_review_event_set={
                **review_events.to_checkpoint(),
                "event_bindings": [
                    {
                        "event_id": event.event_id,
                        "representative_candidate_id": (
                            event.representative_candidate_id
                        ),
                        "event_revision": event.event_revision,
                    }
                    for event in review_events.events
                ],
            },
            delta_event_binding=dict(
                updated_question_queue[
                    "user_discovered_delta_event_binding"
                ]
            ),
            idempotent_replay=review_events.idempotent_replay,
        )

    def materialize(
        self,
        **kwargs: Any,
    ) -> UserDiscoveredReviewQueueResult:
        """Compatibility alias; ``augment`` names the delta contract."""

        return self.augment(**kwargs)


__all__ = [
    "USER_DISCOVERED_OCR_INCREMENT_SCHEMA",
    "USER_DISCOVERED_REVIEW_QUEUE_SCHEMA",
    "UserDiscoveredReviewQueueError",
    "UserDiscoveredReviewQueueResult",
    "UserDiscoveredReviewQueueService",
]
