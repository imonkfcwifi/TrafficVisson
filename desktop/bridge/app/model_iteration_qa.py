"""Immutable project-model survey QA and learning-iteration contracts.

The model lifecycle already owns DatasetVersion, CUDA TrainingJob,
fixed-validation, ModelVersion, explicit promotion, and rollback.  The video
pipeline separately owns promoted-model survey review and final missed-scene
QA.  This module closes the *audit* gap between those two systems without
granting either side new authority:

* reviewed promoted-model proposals become event-level TP/FP observations;
* locally discovered, reviewed missed scenes become event-level FN
  observations;
* those observations are frozen into a survey QA snapshot;
* training-only feedback Annotation identities are bound to the next immutable
  DatasetVersion while the parent's fixed validation membership is preserved.

Nothing here starts training, promotes a model, creates an Incident, supplies a
final severity, or creates report truth.  Survey drift is observational review
burden evidence only.  Fixed-validation metrics remain the sole model
promotion quality gate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from .training_provenance import (
    TrainingProvenanceError,
    canonical_annotation_provenance_graph,
    canonical_dataset_provenance_graph,
)
from .candidate_labels import CandidateLabelError, canonical_issue_type
from .validation_metrics import CANONICAL_ISSUE_TYPES


SURVEY_MODEL_QA_SCHEMA = "trafficvisson.model-survey-qa.v1"
MODEL_ITERATION_SCHEMA = "trafficvisson.model-iteration.v1"

_SURVEY_COMPLETION_SCHEMA = (
    "trafficvisson.promoted-model-survey-qa-completion.v1"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_EVENT_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,191}$"
)
_REVIEW_ANSWERS = {"yes", "no", "edit", "unsure", "skipped"}
_ORIGINS = {
    "promoted_model_candidate",
    "local_user_discovered_false_negative",
    "other_detector_candidate",
}
_FINAL_QA_CHOICES = {
    "no_miss_complete",
    "miss_candidates_reviewed",
}
_ISSUE_ORDER = {
    issue_type: index
    for index, issue_type in enumerate(CANONICAL_ISSUE_TYPES)
}
_MAX_OUTCOMES = 100_000
_MAX_MEMBERS = 1_000_000
_PROMOTED_MODEL_ARTIFACT_FIELDS = (
    "project_id",
    "model_version_id",
    "model_version",
    "model_format",
    "dataset_version_id",
    "dataset_hash",
    "dataset_provenance_graph_sha256",
    "fixed_validation_sha256",
    "primary_artifact_sha256",
    "artifact_manifest_sha256",
    "training_analysis_job_ids",
    "fixed_validation_analysis_job_ids",
    "source_analysis_lineage_sha256",
    "fixed_validation_set_version",
)


class ModelIterationQaError(ValueError):
    """Survey QA or model-iteration provenance is invalid."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ModelIterationQaError(f"{label} must be a UUID")
    try:
        canonical = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ModelIterationQaError(f"{label} must be a UUID") from error
    if canonical != value:
        raise ModelIterationQaError(f"{label} must be canonical")
    return canonical


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ModelIterationQaError(
            f"{label} must be a lowercase SHA-256"
        )
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ModelIterationQaError(f"{label} is invalid")
    return value


def _event_identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or _EVENT_IDENTIFIER.fullmatch(value) is None
    ):
        raise ModelIterationQaError(f"{label} is invalid")
    return value


def _issue_type(value: object, label: str) -> str:
    if value not in CANONICAL_ISSUE_TYPES:
        raise ModelIterationQaError(f"{label} is not a canonical type")
    return str(value)


def _optional_issue_type(value: object, label: str) -> Optional[str]:
    if value is None:
        return None
    return _issue_type(value, label)


def _canonical_id_list(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise ModelIterationQaError(f"{label} must be a sequence")
    result = [_uuid(item, label) for item in value]
    if (
        (not result and not allow_empty)
        or len(result) != len(set(result))
        or result != sorted(result)
    ):
        raise ModelIterationQaError(f"{label} is not canonical")
    return result


def _canonical_promoted_model(
    value: object,
    *,
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelIterationQaError(
            "promoted model binding is unavailable"
        )
    state = value.get("state", value.get("model_promotion_state"))
    if state not in {"promoted", "active", "ready"}:
        raise ModelIterationQaError(
            "model must be explicitly promoted"
        )
    if "binding_sha256" in value:
        binding = dict(value)
        supplied_binding_sha256 = binding.pop("binding_sha256")
        if (
            _sha(
                supplied_binding_sha256,
                "promoted model binding_sha256",
            )
            != _digest(binding)
        ):
            raise ModelIterationQaError(
                "promoted model PipelineJob binding is invalid"
            )
    model_project_id = _uuid(
        value.get("project_id"), "promoted model project_id"
    )
    if project_id is not None and model_project_id != project_id:
        raise ModelIterationQaError(
            "promoted model crossed project boundaries"
        )
    training_jobs = _canonical_id_list(
        value.get("training_analysis_job_ids"),
        "training_analysis_job_ids",
    )
    validation_jobs = _canonical_id_list(
        value.get("fixed_validation_analysis_job_ids"),
        "fixed_validation_analysis_job_ids",
    )
    if set(training_jobs).intersection(validation_jobs):
        raise ModelIterationQaError(
            "training and fixed validation jobs overlap"
        )
    state_revision = value.get("state_revision")
    fixed_validation_set_version = value.get(
        "fixed_validation_set_version"
    )
    if (
        isinstance(state_revision, bool)
        or not isinstance(state_revision, int)
        or state_revision < 1
        or isinstance(fixed_validation_set_version, bool)
        or not isinstance(fixed_validation_set_version, int)
        or fixed_validation_set_version < 1
    ):
        raise ModelIterationQaError(
            "promoted model revision is invalid"
        )
    source_lineage = {
        "training_analysis_job_ids": training_jobs,
        "fixed_validation_analysis_job_ids": validation_jobs,
    }
    source_lineage_sha256 = _sha(
        value.get("source_analysis_lineage_sha256"),
        "source_analysis_lineage_sha256",
    )
    if source_lineage_sha256 != _digest(source_lineage):
        raise ModelIterationQaError(
            "promoted model source lineage digest changed"
        )
    result = {
        "project_id": model_project_id,
        "state_revision": state_revision,
        "model_version_id": _identifier(
            value.get("model_version_id"), "model_version_id"
        ),
        "model_version": _identifier(
            value.get("model_version"), "model_version"
        ),
        "model_format": _identifier(
            value.get("model_format"), "model_format"
        ),
        "model_promotion_state": "promoted",
        "dataset_version_id": _uuid(
            value.get("dataset_version_id"), "dataset_version_id"
        ),
        "dataset_hash": _sha(
            value.get("dataset_hash"), "dataset_hash"
        ),
        "dataset_provenance_graph_sha256": _sha(
            value.get("dataset_provenance_graph_sha256"),
            "dataset_provenance_graph_sha256",
        ),
        "fixed_validation_sha256": _sha(
            value.get("fixed_validation_sha256"),
            "fixed_validation_sha256",
        ),
        "primary_artifact_sha256": _sha(
            value.get("primary_artifact_sha256"),
            "primary_artifact_sha256",
        ),
        "artifact_manifest_sha256": _sha(
            value.get("artifact_manifest_sha256"),
            "artifact_manifest_sha256",
        ),
        "training_analysis_job_ids": training_jobs,
        "fixed_validation_analysis_job_ids": validation_jobs,
        "source_analysis_lineage_sha256": source_lineage_sha256,
        "fixed_validation_set_version": fixed_validation_set_version,
    }
    result["promoted_model_binding_sha256"] = _digest(result)
    supplied_canonical_digest = value.get(
        "promoted_model_binding_sha256"
    )
    if (
        supplied_canonical_digest is not None
        and _sha(
            supplied_canonical_digest,
            "promoted_model_binding_sha256",
        )
        != result["promoted_model_binding_sha256"]
    ):
        raise ModelIterationQaError(
            "promoted model canonical binding digest changed"
        )
    return result


def same_promoted_model_artifact(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    """Compare immutable model truth while allowing state-revision drift."""

    try:
        canonical_left = _canonical_promoted_model(left)
        canonical_right = _canonical_promoted_model(right)
    except ModelIterationQaError:
        return False
    return all(
        canonical_left[field] == canonical_right[field]
        for field in _PROMOTED_MODEL_ARTIFACT_FIELDS
    )


def _canonical_survey_completion(
    value: object,
    *,
    project_id: str,
    promoted_model: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelIterationQaError(
            "survey QA completion is unavailable"
        )
    required = {
        "schema_version",
        "project_id",
        "pipeline_job_id",
        "analysis_job_id",
        "source_sha256",
        "dense_index_sha256",
        "dense_candidate_discovery_revision",
        "question_queue_revision",
        "model_version_id",
        "dataset_version_id",
        "primary_artifact_sha256",
        "artifact_manifest_sha256",
        "dataset_hash",
        "fixed_validation_sha256",
        "source_analysis_lineage_sha256",
        "dataset_provenance_graph_sha256",
        "promoted_model_inference_sha256",
        "sampling_fps",
        "scanned_frame_count",
        "final_qa_choice",
        "review_completion_id",
        "server_answer_ledger_revision",
        "review_event_set_collection_revision",
        "question_catalog_collection_revision",
        "user_discovered_registrations_sha256",
        "user_discovered_candidate_count",
        "actor",
        "recorded_at",
        "survey_qa_revision",
    }
    if set(value) != required:
        raise ModelIterationQaError(
            "survey QA completion fields are invalid"
        )
    base = dict(value)
    revision = base.pop("survey_qa_revision")
    if (
        _sha(revision, "survey_qa_revision") != _digest(base)
        or base.get("schema_version") != _SURVEY_COMPLETION_SCHEMA
        or base.get("project_id") != project_id
        or base.get("actor") != "local_user"
        or base.get("sampling_fps") != 2
        or base.get("final_qa_choice") not in _FINAL_QA_CHOICES
        or not isinstance(base.get("recorded_at"), str)
        or not base["recorded_at"]
        or isinstance(base.get("scanned_frame_count"), bool)
        or not isinstance(base.get("scanned_frame_count"), int)
        or base["scanned_frame_count"] < 1
        or isinstance(
            base.get("user_discovered_candidate_count"), bool
        )
        or not isinstance(
            base.get("user_discovered_candidate_count"), int
        )
        or base["user_discovered_candidate_count"] < 0
    ):
        raise ModelIterationQaError(
            "survey QA completion is invalid"
        )
    if (
        base["final_qa_choice"] == "no_miss_complete"
        and base["user_discovered_candidate_count"] != 0
    ) or (
        base["final_qa_choice"] == "miss_candidates_reviewed"
        and base["user_discovered_candidate_count"] < 1
    ):
        raise ModelIterationQaError(
            "survey missed-scene QA is inconsistent"
        )
    for field in ("pipeline_job_id", "analysis_job_id"):
        _uuid(base[field], field)
    _event_identifier(
        base["review_completion_id"], "review_completion_id"
    )
    for field in (
        "source_sha256",
        "dense_index_sha256",
        "dense_candidate_discovery_revision",
        "question_queue_revision",
        "primary_artifact_sha256",
        "artifact_manifest_sha256",
        "dataset_hash",
        "fixed_validation_sha256",
        "source_analysis_lineage_sha256",
        "dataset_provenance_graph_sha256",
        "promoted_model_inference_sha256",
        "server_answer_ledger_revision",
        "review_event_set_collection_revision",
        "question_catalog_collection_revision",
        "user_discovered_registrations_sha256",
    ):
        _sha(base[field], field)
    for field in ("model_version_id",):
        _identifier(base[field], field)
    for field in (
        "model_version_id",
        "dataset_version_id",
        "primary_artifact_sha256",
        "artifact_manifest_sha256",
        "dataset_hash",
        "fixed_validation_sha256",
        "dataset_provenance_graph_sha256",
    ):
        if base[field] != promoted_model[field]:
            raise ModelIterationQaError(
                "survey QA does not match the promoted model"
            )
    analysis_job_id = base["analysis_job_id"]
    if analysis_job_id in {
        *promoted_model["training_analysis_job_ids"],
        *promoted_model["fixed_validation_analysis_job_ids"],
    }:
        raise ModelIterationQaError(
            "survey reused model training or validation evidence"
        )
    return {**base, "survey_qa_revision": revision}


def _canonical_outcome(
    value: object,
    *,
    project_id: str,
    analysis_job_id: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelIterationQaError("survey outcome is invalid")
    expected = {
        "project_id",
        "analysis_job_id",
        "review_event_id",
        "review_event_revision",
        "question_id",
        "candidate_id",
        "candidate_metadata_sha256",
        "evidence_sha256",
        "origin",
        "proposed_issue_type",
        "review_answer",
        "final_issue_type",
        "annotation_id",
        "annotation_hash",
        "training_authorized",
        "answered_by",
    }
    if set(value) != expected:
        raise ModelIterationQaError("survey outcome fields are invalid")
    if (
        value.get("project_id") != project_id
        or value.get("analysis_job_id") != analysis_job_id
        or value.get("origin") not in _ORIGINS
        or value.get("review_answer") not in _REVIEW_ANSWERS
        or value.get("answered_by") != "local_user"
        or not isinstance(value.get("training_authorized"), bool)
    ):
        raise ModelIterationQaError(
            "survey outcome crossed its review boundary"
        )
    origin = str(value["origin"])
    answer = str(value["review_answer"])
    proposed = _optional_issue_type(
        value.get("proposed_issue_type"),
        "proposed_issue_type",
    )
    final = _optional_issue_type(
        value.get("final_issue_type"),
        "final_issue_type",
    )
    if origin == "promoted_model_candidate" and proposed is None:
        raise ModelIterationQaError(
            "promoted-model outcome requires a proposed type"
        )
    annotation_id = value.get("annotation_id")
    annotation_hash = value.get("annotation_hash")
    training_authorized = value["training_authorized"]
    if answer in {"unsure", "skipped"}:
        if (
            final is not None
            or annotation_id is not None
            or annotation_hash is not None
            or training_authorized is not False
        ):
            raise ModelIterationQaError(
                "held survey outcome cannot become training truth"
            )
    else:
        annotation_id = _uuid(annotation_id, "annotation_id")
        annotation_hash = _sha(annotation_hash, "annotation_hash")
        if training_authorized is not True:
            raise ModelIterationQaError(
                "resolved survey feedback must be training-authorized"
            )
        if answer == "yes":
            if final is None:
                raise ModelIterationQaError(
                    "yes outcome requires a final type"
                )
            if (
                origin == "promoted_model_candidate"
                and final != proposed
            ):
                raise ModelIterationQaError(
                    "type correction must use edit"
                )
        elif answer == "edit":
            if final is None:
                raise ModelIterationQaError(
                    "edit outcome requires a final type"
                )
        elif answer == "no" and final is not None:
            raise ModelIterationQaError(
                "negative outcome cannot carry a final type"
            )
    return {
        "project_id": project_id,
        "analysis_job_id": analysis_job_id,
        "review_event_id": _event_identifier(
            value.get("review_event_id"), "review_event_id"
        ),
        "review_event_revision": _sha(
            value.get("review_event_revision"),
            "review_event_revision",
        ),
        "question_id": _uuid(
            value.get("question_id"), "question_id"
        ),
        "candidate_id": _event_identifier(
            value.get("candidate_id"), "candidate_id"
        ),
        "candidate_metadata_sha256": _sha(
            value.get("candidate_metadata_sha256"),
            "candidate_metadata_sha256",
        ),
        "evidence_sha256": _sha(
            value.get("evidence_sha256"), "evidence_sha256"
        ),
        "origin": origin,
        "proposed_issue_type": proposed,
        "review_answer": answer,
        "final_issue_type": final,
        "annotation_id": annotation_id,
        "annotation_hash": annotation_hash,
        "training_authorized": training_authorized,
        "answered_by": "local_user",
    }


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return numerator / denominator


def _per_thousand(count: int, scanned_frames: int) -> float:
    return (count / scanned_frames) * 1000.0


def _metrics(
    outcomes: Sequence[Mapping[str, Any]],
    *,
    scanned_frames: int,
) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, int]] = {}

    def issue_counts(issue: str) -> dict[str, int]:
        return counts.setdefault(
            issue,
            {
                "true_positive_count": 0,
                "false_positive_count": 0,
                "false_negative_count": 0,
                "held_count": 0,
            },
        )

    for outcome in outcomes:
        proposed = outcome["proposed_issue_type"]
        final = outcome["final_issue_type"]
        origin = outcome["origin"]
        answer = outcome["review_answer"]
        if answer in {"unsure", "skipped"}:
            if proposed is not None:
                issue_counts(proposed)["held_count"] += 1
            continue
        if origin == "promoted_model_candidate":
            if answer == "yes":
                issue_counts(str(proposed))[
                    "true_positive_count"
                ] += 1
            elif answer == "no":
                issue_counts(str(proposed))[
                    "false_positive_count"
                ] += 1
            elif answer == "edit" and final == proposed:
                issue_counts(str(proposed))[
                    "true_positive_count"
                ] += 1
            elif answer == "edit":
                issue_counts(str(proposed))[
                    "false_positive_count"
                ] += 1
                issue_counts(str(final))[
                    "false_negative_count"
                ] += 1
        elif (
            origin == "local_user_discovered_false_negative"
            and answer in {"yes", "edit"}
        ):
            issue_counts(str(final))["false_negative_count"] += 1

    result = []
    for issue in sorted(counts, key=_ISSUE_ORDER.__getitem__):
        value = counts[issue]
        tp = value["true_positive_count"]
        fp = value["false_positive_count"]
        fn = value["false_negative_count"]
        result.append(
            {
                "issue_type": issue,
                **value,
                "precision": _ratio(tp, tp + fp),
                "recall": _ratio(tp, tp + fn),
                "false_positives_per_1000_frames": _per_thousand(
                    fp, scanned_frames
                ),
                "false_negatives_per_1000_frames": _per_thousand(
                    fn, scanned_frames
                ),
            }
        )
    return result


def _canonical_metric(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelIterationQaError("survey metric is invalid")
    expected = {
        "issue_type",
        "true_positive_count",
        "false_positive_count",
        "false_negative_count",
        "held_count",
        "precision",
        "recall",
        "false_positives_per_1000_frames",
        "false_negatives_per_1000_frames",
    }
    if set(value) != expected:
        raise ModelIterationQaError("survey metric fields are invalid")
    issue = _issue_type(value["issue_type"], "issue_type")
    counts = {}
    for field in (
        "true_positive_count",
        "false_positive_count",
        "false_negative_count",
        "held_count",
    ):
        raw = value[field]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ModelIterationQaError("survey metric count is invalid")
        counts[field] = raw
    for field in (
        "precision",
        "recall",
        "false_positives_per_1000_frames",
        "false_negatives_per_1000_frames",
    ):
        raw = value[field]
        if raw is not None and (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            or float(raw) < 0
        ):
            raise ModelIterationQaError("survey metric rate is invalid")
    return {
        "issue_type": issue,
        **counts,
        "precision": (
            None if value["precision"] is None
            else float(value["precision"])
        ),
        "recall": (
            None if value["recall"] is None
            else float(value["recall"])
        ),
        "false_positives_per_1000_frames": float(
            value["false_positives_per_1000_frames"]
        ),
        "false_negatives_per_1000_frames": float(
            value["false_negatives_per_1000_frames"]
        ),
    }


def _drift(
    current_metrics: Sequence[Mapping[str, Any]],
    baseline: Optional[Mapping[str, Any]],
    *,
    qa_scope_sha256: str,
) -> dict[str, Any]:
    if baseline is None:
        return {
            "assessed": False,
            "baseline_survey_model_qa_sha256": None,
            "per_type": [],
            "promotion_gate_authority": False,
        }
    canonical = canonical_survey_model_qa(baseline)
    if canonical["qa_scope_sha256"] != qa_scope_sha256:
        raise ModelIterationQaError(
            "survey drift baseline has a different QA scope"
        )
    baseline_by_type = {
        item["issue_type"]: item for item in canonical["per_type_metrics"]
    }
    current_by_type = {
        item["issue_type"]: item for item in current_metrics
    }
    rows = []
    for issue in sorted(
        set(baseline_by_type) | set(current_by_type),
        key=_ISSUE_ORDER.__getitem__,
    ):
        before = baseline_by_type.get(issue)
        after = current_by_type.get(issue)

        def delta(field: str) -> Optional[float]:
            before_value = None if before is None else before[field]
            after_value = None if after is None else after[field]
            if before_value is None or after_value is None:
                return None
            return float(after_value) - float(before_value)

        rows.append(
            {
                "issue_type": issue,
                "precision_delta": delta("precision"),
                "recall_delta": delta("recall"),
                "false_positives_per_1000_frames_delta": delta(
                    "false_positives_per_1000_frames"
                ),
                "false_negatives_per_1000_frames_delta": delta(
                    "false_negatives_per_1000_frames"
                ),
            }
        )
    return {
        "assessed": True,
        "baseline_survey_model_qa_sha256": canonical[
            "survey_model_qa_sha256"
        ],
        "per_type": rows,
        "promotion_gate_authority": False,
    }


def build_reviewed_survey_outcomes(
    *,
    project_id: UUID | str,
    analysis_job_id: UUID | str,
    question_candidate_ids: Sequence[str],
    latest_answer_records: Sequence[Mapping[str, Any]],
    next_dataset_version: Mapping[str, Any],
    promoted_model_binding: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project verified local-user answers into survey QA observations.

    Annotation identities come from the immutable DatasetVersion produced
    after review, never from an answer-record identifier.  Excluded/held
    memberships remain auditable in the DatasetVersion but deliberately do
    not become training feedback in the survey snapshot.
    """

    project_text = _uuid(str(project_id), "project_id")
    analysis_text = _uuid(str(analysis_job_id), "analysis_job_id")
    model = _canonical_promoted_model(
        promoted_model_binding,
        project_id=project_text,
    )
    dataset = _canonical_dataset(
        next_dataset_version, "next_dataset_version"
    )
    if dataset["project_id"] != project_text:
        raise ModelIterationQaError(
            "survey DatasetVersion crossed project boundaries"
        )
    if (
        not isinstance(question_candidate_ids, Sequence)
        or isinstance(
            question_candidate_ids, (str, bytes, bytearray)
        )
        or not isinstance(latest_answer_records, Sequence)
        or isinstance(
            latest_answer_records, (str, bytes, bytearray)
        )
    ):
        raise ModelIterationQaError(
            "survey answer projection inputs are invalid"
        )
    candidate_ids = [
        _event_identifier(value, "question_candidate_id")
        for value in question_candidate_ids
    ]
    if (
        not candidate_ids
        or len(candidate_ids) != len(set(candidate_ids))
        or len(latest_answer_records) != len(candidate_ids)
    ):
        raise ModelIterationQaError(
            "survey answer set does not match the question queue"
        )
    records_by_candidate: dict[str, Mapping[str, Any]] = {}
    for record in latest_answer_records:
        if not isinstance(record, Mapping):
            raise ModelIterationQaError(
                "survey answer record is invalid"
            )
        candidate_id = _event_identifier(
            record.get("candidate_id"), "candidate_id"
        )
        if candidate_id in records_by_candidate:
            raise ModelIterationQaError(
                "survey answer candidates are duplicated"
            )
        records_by_candidate[candidate_id] = record
    if set(records_by_candidate) != set(candidate_ids):
        raise ModelIterationQaError(
            "survey answer set crossed the question queue"
        )

    memberships: dict[
        tuple[str, str], Mapping[str, Any]
    ] = {}
    for member in dataset["memberships"]:
        if member["analysis_job_id"] != analysis_text:
            continue
        key = (member["question_id"], member["candidate_id"])
        if key in memberships:
            raise ModelIterationQaError(
                "survey DatasetVersion membership is duplicated"
            )
        memberships[key] = member

    outcomes = []
    for candidate_id in candidate_ids:
        record = records_by_candidate[candidate_id]
        answer = record.get("final_answer")
        metadata = record.get("ai_original_candidate")
        raw_graph = record.get("provenance_graph")
        if (
            record.get("project_id") != project_text
            or record.get("analysis_job_id") != analysis_text
            or not isinstance(answer, Mapping)
            or answer.get("answered_by") != "local_user"
            or not isinstance(metadata, Mapping)
            or metadata.get("candidate_id") != candidate_id
        ):
            raise ModelIterationQaError(
                "survey answer crossed the local-user boundary"
            )
        try:
            graph = canonical_annotation_provenance_graph(raw_graph)
        except TrainingProvenanceError as error:
            raise ModelIterationQaError(
                "survey answer provenance is unavailable"
            ) from error
        question_id = _uuid(
            record.get("question_id"), "question_id"
        )
        member = memberships.get((question_id, candidate_id))
        if (
            member is None
            or member["answer_record_id"] != record.get("record_id")
            or member["answer_revision"]
            != record.get("answer_revision")
            or member["source_record_hash"]
            != record.get("record_hash")
            or member["include_in_training"]
            is not answer.get("include_in_training")
            or graph["project_id"] != project_text
            or graph["analysis_job_id"] != analysis_text
            or graph["question_id"] != question_id
            or graph["candidate_id"] != candidate_id
            or graph["answer_record_id"] != record.get("record_id")
            or graph["answer_revision"]
            != record.get("answer_revision")
            or graph["answer_authority"]["answered_by"]
            != "local_user"
            or graph["answer_authority"]["include_in_training"]
            is not answer.get("include_in_training")
        ):
            raise ModelIterationQaError(
                "survey answer does not match its DatasetVersion membership"
            )

        raw_sources = metadata.get("proposal_sources", ())
        if isinstance(raw_sources, str):
            sources = {raw_sources}
        elif isinstance(raw_sources, Sequence):
            sources = {
                str(value)
                for value in raw_sources
                if isinstance(value, str)
            }
        else:
            sources = set()
        if isinstance(metadata.get("proposal_source"), str):
            sources.add(str(metadata["proposal_source"]))
        if (
            metadata.get("candidate_kind") == "user_discovered"
            or "local_user_discovered_false_negative" in sources
        ):
            origin = "local_user_discovered_false_negative"
        elif "promoted_project_model_dense_2fps" in sources:
            origin = "promoted_model_candidate"
            provenance = metadata.get(
                "representative_proposal_provenance"
            )
            expected_provenance = {
                "model_version_id": model["model_version_id"],
                "dataset_version_id": model["dataset_version_id"],
                "dataset_hash": model["dataset_hash"],
                "fixed_validation_sha256": model[
                    "fixed_validation_sha256"
                ],
                "primary_artifact_sha256": model[
                    "primary_artifact_sha256"
                ],
                "artifact_manifest_sha256": model[
                    "artifact_manifest_sha256"
                ],
                "source_analysis_lineage_sha256": model[
                    "source_analysis_lineage_sha256"
                ],
                "dataset_provenance_graph_sha256": model[
                    "dataset_provenance_graph_sha256"
                ],
            }
            if (
                not isinstance(provenance, Mapping)
                or any(
                    provenance.get(field) != expected
                    for field, expected in expected_provenance.items()
                )
                or provenance.get("survey_analysis_job_id")
                != analysis_text
                or provenance.get("independent_survey_verified")
                is not True
            ):
                raise ModelIterationQaError(
                    "promoted-model candidate provenance changed"
                )
        else:
            origin = "other_detector_candidate"

        proposed_raw = metadata.get("issue_type")
        try:
            proposed = (
                canonical_issue_type(proposed_raw)
                if proposed_raw is not None
                else None
            )
            final_raw = answer.get("selected_issue_type")
            final = (
                canonical_issue_type(final_raw)
                if final_raw is not None
                else None
            )
        except CandidateLabelError as error:
            raise ModelIterationQaError(
                "survey issue type is unsupported"
            ) from error
        if proposed == "other" or final == "other":
            raise ModelIterationQaError(
                "survey issue type is outside the model taxonomy"
            )
        review_answer = str(answer.get("answer"))
        training_authorized = answer.get("include_in_training")
        outcomes.append(
            {
                "project_id": project_text,
                "analysis_job_id": analysis_text,
                "review_event_id": graph["review_event_id"],
                "review_event_revision": graph[
                    "review_event_revision"
                ],
                "question_id": question_id,
                "candidate_id": candidate_id,
                "candidate_metadata_sha256": graph[
                    "candidate_metadata_sha256"
                ],
                "evidence_sha256": graph["image_sha256"],
                "origin": origin,
                "proposed_issue_type": proposed,
                "review_answer": review_answer,
                "final_issue_type": final,
                "annotation_id": (
                    member["annotation_id"]
                    if training_authorized is True
                    else None
                ),
                "annotation_hash": (
                    member["annotation_hash"]
                    if training_authorized is True
                    else None
                ),
                "training_authorized": training_authorized,
                "answered_by": "local_user",
            }
        )
    return [
        _canonical_outcome(
            item,
            project_id=project_text,
            analysis_job_id=analysis_text,
        )
        for item in outcomes
    ]


def build_survey_model_qa(
    *,
    project_id: UUID | str,
    promoted_model_binding: Mapping[str, Any],
    survey_qa_completion: Mapping[str, Any],
    reviewed_outcomes: Sequence[Mapping[str, Any]],
    qa_scope_sha256: str,
    baseline_snapshot: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Freeze reviewed TP/FP/FN evidence from one independent survey."""

    project_text = _uuid(str(project_id), "project_id")
    scope = _sha(qa_scope_sha256, "qa_scope_sha256")
    model = _canonical_promoted_model(
        promoted_model_binding,
        project_id=project_text,
    )
    completion = _canonical_survey_completion(
        survey_qa_completion,
        project_id=project_text,
        promoted_model=model,
    )
    if (
        not isinstance(reviewed_outcomes, Sequence)
        or isinstance(reviewed_outcomes, (str, bytes, bytearray))
        or len(reviewed_outcomes) > _MAX_OUTCOMES
    ):
        raise ModelIterationQaError(
            "reviewed survey outcomes are invalid"
        )
    outcomes = [
        _canonical_outcome(
            item,
            project_id=project_text,
            analysis_job_id=completion["analysis_job_id"],
        )
        for item in reviewed_outcomes
    ]
    outcomes.sort(
        key=lambda item: (
            item["review_event_id"],
            item["question_id"],
            item["candidate_id"],
        )
    )
    if len(
        {
            (
                item["review_event_id"],
                item["question_id"],
                item["candidate_id"],
            )
            for item in outcomes
        }
    ) != len(outcomes):
        raise ModelIterationQaError(
            "survey outcomes contain duplicate review identities"
        )
    discovered = [
        item
        for item in outcomes
        if item["origin"]
        == "local_user_discovered_false_negative"
    ]
    if len(discovered) != completion[
        "user_discovered_candidate_count"
    ]:
        raise ModelIterationQaError(
            "survey missed-scene registrations do not match outcomes"
        )
    metrics = _metrics(
        outcomes,
        scanned_frames=completion["scanned_frame_count"],
    )
    training_feedback = [
        {
            "annotation_id": item["annotation_id"],
            "annotation_hash": item["annotation_hash"],
            "analysis_job_id": item["analysis_job_id"],
            "question_id": item["question_id"],
            "candidate_id": item["candidate_id"],
            "review_event_id": item["review_event_id"],
            "review_event_revision": item["review_event_revision"],
            "origin": item["origin"],
            "training_only": True,
        }
        for item in outcomes
        if item["training_authorized"]
    ]
    base = {
        "schema_version": SURVEY_MODEL_QA_SCHEMA,
        "project_id": project_text,
        "pipeline_job_id": completion["pipeline_job_id"],
        "analysis_job_id": completion["analysis_job_id"],
        "source_sha256": completion["source_sha256"],
        "qa_scope_sha256": scope,
        "promoted_model_binding": model,
        "survey_qa_revision": completion["survey_qa_revision"],
        "final_qa_choice": completion["final_qa_choice"],
        "scanned_frame_count": completion["scanned_frame_count"],
        "reviewed_outcomes": outcomes,
        "per_type_metrics": metrics,
        "summary": {
            "model_candidate_count": sum(
                item["origin"] == "promoted_model_candidate"
                for item in outcomes
            ),
            "user_discovered_candidate_count": len(discovered),
            "other_detector_candidate_count": sum(
                item["origin"] == "other_detector_candidate"
                for item in outcomes
            ),
            "resolved_training_feedback_count": len(training_feedback),
            "held_count": sum(
                item["review_answer"] in {"unsure", "skipped"}
                for item in outcomes
            ),
            "true_positive_count": sum(
                item["true_positive_count"] for item in metrics
            ),
            "false_positive_count": sum(
                item["false_positive_count"] for item in metrics
            ),
            "false_negative_count": sum(
                item["false_negative_count"] for item in metrics
            ),
        },
        "training_feedback_annotations": training_feedback,
        "drift": _drift(
            metrics,
            baseline_snapshot,
            qa_scope_sha256=scope,
        ),
        "creates_incident": False,
        "creates_report_truth": False,
        "sets_final_severity": False,
        "starts_training": False,
        "promotes_model": False,
    }
    return canonical_survey_model_qa(
        {**base, "survey_model_qa_sha256": _digest(base)}
    )


def canonical_survey_model_qa(value: object) -> dict[str, Any]:
    """Validate a previously frozen survey model QA snapshot."""

    expected = {
        "schema_version",
        "project_id",
        "pipeline_job_id",
        "analysis_job_id",
        "source_sha256",
        "qa_scope_sha256",
        "promoted_model_binding",
        "survey_qa_revision",
        "final_qa_choice",
        "scanned_frame_count",
        "reviewed_outcomes",
        "per_type_metrics",
        "summary",
        "training_feedback_annotations",
        "drift",
        "creates_incident",
        "creates_report_truth",
        "sets_final_severity",
        "starts_training",
        "promotes_model",
        "survey_model_qa_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ModelIterationQaError(
            "survey model QA fields are invalid"
        )
    base = dict(value)
    digest = base.pop("survey_model_qa_sha256")
    if (
        value["schema_version"] != SURVEY_MODEL_QA_SCHEMA
        or _sha(digest, "survey_model_qa_sha256") != _digest(base)
        or any(
            value[field] is not False
            for field in (
                "creates_incident",
                "creates_report_truth",
                "sets_final_severity",
                "starts_training",
                "promotes_model",
            )
        )
    ):
        raise ModelIterationQaError(
            "survey model QA digest or authority is invalid"
        )
    project_id = _uuid(value["project_id"], "project_id")
    model = _canonical_promoted_model(
        value["promoted_model_binding"],
        project_id=project_id,
    )
    if model != value["promoted_model_binding"]:
        raise ModelIterationQaError(
            "survey promoted-model binding is not canonical"
        )
    for field in ("pipeline_job_id", "analysis_job_id"):
        _uuid(value[field], field)
    for field in (
        "source_sha256",
        "qa_scope_sha256",
        "survey_qa_revision",
    ):
        _sha(value[field], field)
    if value["final_qa_choice"] not in _FINAL_QA_CHOICES:
        raise ModelIterationQaError("survey final QA choice is invalid")
    scanned = value["scanned_frame_count"]
    if isinstance(scanned, bool) or not isinstance(scanned, int) or scanned < 1:
        raise ModelIterationQaError(
            "survey scanned frame count is invalid"
        )
    raw_outcomes = value["reviewed_outcomes"]
    if (
        not isinstance(raw_outcomes, Sequence)
        or isinstance(raw_outcomes, (str, bytes, bytearray))
        or len(raw_outcomes) > _MAX_OUTCOMES
    ):
        raise ModelIterationQaError("survey outcomes are invalid")
    outcomes = [
        _canonical_outcome(
            item,
            project_id=project_id,
            analysis_job_id=value["analysis_job_id"],
        )
        for item in raw_outcomes
    ]
    if outcomes != list(raw_outcomes):
        raise ModelIterationQaError(
            "survey outcomes are not canonical"
        )
    recomputed_metrics = _metrics(outcomes, scanned_frames=scanned)
    raw_metrics = value["per_type_metrics"]
    if (
        not isinstance(raw_metrics, Sequence)
        or isinstance(raw_metrics, (str, bytes, bytearray))
    ):
        raise ModelIterationQaError("survey metrics are invalid")
    metrics = [_canonical_metric(item) for item in raw_metrics]
    if metrics != list(raw_metrics) or metrics != recomputed_metrics:
        raise ModelIterationQaError(
            "survey metrics do not match outcomes"
        )
    feedback = [
        {
            "annotation_id": item["annotation_id"],
            "annotation_hash": item["annotation_hash"],
            "analysis_job_id": item["analysis_job_id"],
            "question_id": item["question_id"],
            "candidate_id": item["candidate_id"],
            "review_event_id": item["review_event_id"],
            "review_event_revision": item["review_event_revision"],
            "origin": item["origin"],
            "training_only": True,
        }
        for item in outcomes
        if item["training_authorized"]
    ]
    if value["training_feedback_annotations"] != feedback:
        raise ModelIterationQaError(
            "survey training feedback does not match outcomes"
        )
    summary = value["summary"]
    expected_summary = {
        "model_candidate_count": sum(
            item["origin"] == "promoted_model_candidate"
            for item in outcomes
        ),
        "user_discovered_candidate_count": sum(
            item["origin"]
            == "local_user_discovered_false_negative"
            for item in outcomes
        ),
        "other_detector_candidate_count": sum(
            item["origin"] == "other_detector_candidate"
            for item in outcomes
        ),
        "resolved_training_feedback_count": len(feedback),
        "held_count": sum(
            item["review_answer"] in {"unsure", "skipped"}
            for item in outcomes
        ),
        "true_positive_count": sum(
            item["true_positive_count"] for item in metrics
        ),
        "false_positive_count": sum(
            item["false_positive_count"] for item in metrics
        ),
        "false_negative_count": sum(
            item["false_negative_count"] for item in metrics
        ),
    }
    if summary != expected_summary:
        raise ModelIterationQaError(
            "survey summary does not match outcomes"
        )
    if (
        value["final_qa_choice"] == "no_miss_complete"
        and expected_summary["user_discovered_candidate_count"] != 0
    ) or (
        value["final_qa_choice"] == "miss_candidates_reviewed"
        and expected_summary["user_discovered_candidate_count"] < 1
    ):
        raise ModelIterationQaError(
            "survey outcome set contradicts final missed-scene QA"
        )
    drift = value["drift"]
    if not isinstance(drift, Mapping) or set(drift) != {
        "assessed",
        "baseline_survey_model_qa_sha256",
        "per_type",
        "promotion_gate_authority",
    }:
        raise ModelIterationQaError("survey drift is invalid")
    if (
        not isinstance(drift["assessed"], bool)
        or drift["promotion_gate_authority"] is not False
        or (
            drift["assessed"] is False
            and (
                drift["baseline_survey_model_qa_sha256"] is not None
                or drift["per_type"] != []
            )
        )
        or (
            drift["assessed"] is True
            and (
                not isinstance(
                    drift["baseline_survey_model_qa_sha256"], str
                )
                or _SHA256.fullmatch(
                    drift["baseline_survey_model_qa_sha256"]
                )
                is None
                or not isinstance(drift["per_type"], list)
            )
        )
    ):
        raise ModelIterationQaError("survey drift authority is invalid")
    if drift["assessed"] is True:
        drift_rows = drift["per_type"]
        expected_drift_fields = {
            "issue_type",
            "precision_delta",
            "recall_delta",
            "false_positives_per_1000_frames_delta",
            "false_negatives_per_1000_frames_delta",
        }
        observed_types = []
        for row in drift_rows:
            if (
                not isinstance(row, Mapping)
                or set(row) != expected_drift_fields
            ):
                raise ModelIterationQaError(
                    "survey drift metric fields are invalid"
                )
            issue = _issue_type(
                row["issue_type"], "drift issue_type"
            )
            observed_types.append(issue)
            for field in expected_drift_fields - {"issue_type"}:
                number = row[field]
                if number is not None and (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                ):
                    raise ModelIterationQaError(
                        "survey drift metric is invalid"
                    )
        if (
            observed_types
            != sorted(observed_types, key=_ISSUE_ORDER.__getitem__)
            or len(observed_types) != len(set(observed_types))
        ):
            raise ModelIterationQaError(
                "survey drift metrics are not canonical"
            )
    return {**base, "survey_model_qa_sha256": digest}


def _canonical_dataset(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelIterationQaError(f"{label} is unavailable")
    manifest = value.get("manifest")
    memberships = value.get("memberships")
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(memberships, Sequence)
        or isinstance(memberships, (str, bytes, bytearray))
        or not memberships
        or len(memberships) > _MAX_MEMBERS
    ):
        raise ModelIterationQaError(f"{label} is invalid")
    graph_raw = manifest.get("provenance_graph")
    try:
        graph = canonical_dataset_provenance_graph(graph_raw)
    except TrainingProvenanceError as error:
        raise ModelIterationQaError(
            f"{label} provenance is invalid"
        ) from error
    project_id = _uuid(value.get("project_id"), f"{label}.project_id")
    if graph["project_id"] != project_id:
        raise ModelIterationQaError(
            f"{label} provenance crossed projects"
        )
    canonical_members = []
    for member in memberships:
        if not isinstance(member, Mapping):
            raise ModelIterationQaError(
                f"{label} membership is invalid"
            )
        expected = {
            "annotation_id",
            "question_id",
            "analysis_job_id",
            "candidate_id",
            "answer_record_id",
            "answer_revision",
            "partition",
            "include_in_training",
            "split",
            "source_record_hash",
            "annotation_hash",
        }
        if set(member) != expected:
            raise ModelIterationQaError(
                f"{label} membership fields are invalid"
            )
        split = member["split"]
        include = member["include_in_training"]
        if (
            split not in {"train", "validation", "excluded"}
            or not isinstance(include, bool)
            or include != (split in {"train", "validation"})
        ):
            raise ModelIterationQaError(
                f"{label} membership split is invalid"
            )
        revision = member["answer_revision"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise ModelIterationQaError(
                f"{label} answer revision is invalid"
            )
        canonical_members.append(
            {
                "annotation_id": _uuid(
                    member["annotation_id"], "annotation_id"
                ),
                "question_id": _uuid(
                    member["question_id"], "question_id"
                ),
                "analysis_job_id": _uuid(
                    member["analysis_job_id"], "analysis_job_id"
                ),
                "candidate_id": _event_identifier(
                    member["candidate_id"], "candidate_id"
                ),
                "answer_record_id": _uuid(
                    member["answer_record_id"], "answer_record_id"
                ),
                "answer_revision": revision,
                "partition": str(member["partition"]),
                "include_in_training": include,
                "split": split,
                "source_record_hash": _sha(
                    member["source_record_hash"], "source_record_hash"
                ),
                "annotation_hash": _sha(
                    member["annotation_hash"], "annotation_hash"
                ),
            }
        )
    if canonical_members != list(memberships):
        raise ModelIterationQaError(
            f"{label} memberships are not canonical"
        )
    dataset_id = _uuid(
        value.get("dataset_version_id"),
        f"{label}.dataset_version_id",
    )
    dataset_hash = _sha(
        value.get("dataset_hash"), f"{label}.dataset_hash"
    )
    if (
        manifest.get("dataset_id") != dataset_id
        or manifest.get("dataset_hash") != dataset_hash
    ):
        raise ModelIterationQaError(
            f"{label} manifest identity does not match"
        )
    return {
        "project_id": project_id,
        "dataset_version_id": dataset_id,
        "dataset_hash": dataset_hash,
        "dataset_provenance_graph_sha256": graph[
            "provenance_graph_sha256"
        ],
        "split_strategy": str(value.get("split_strategy")),
        "memberships": canonical_members,
    }


def build_model_iteration(
    *,
    project_id: UUID | str,
    parent_promoted_model: Mapping[str, Any],
    parent_dataset_version: Mapping[str, Any],
    survey_model_qa: Mapping[str, Any],
    next_dataset_version: Mapping[str, Any],
    parent_iteration: Optional[Mapping[str, Any]] = None,
    parent_iteration_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Bind reviewed survey feedback to the next train-only dataset delta."""

    project_text = _uuid(str(project_id), "project_id")
    model = _canonical_promoted_model(
        parent_promoted_model,
        project_id=project_text,
    )
    parent = _canonical_dataset(
        parent_dataset_version, "parent_dataset_version"
    )
    next_dataset = _canonical_dataset(
        next_dataset_version, "next_dataset_version"
    )
    survey = canonical_survey_model_qa(survey_model_qa)
    if any(
        value["project_id"] != project_text
        for value in (parent, next_dataset, survey)
    ):
        raise ModelIterationQaError(
            "model iteration crossed project boundaries"
        )
    if survey["promoted_model_binding"] != model:
        raise ModelIterationQaError(
            "model iteration parent binding is invalid"
        )
    prior = (
        None
        if parent_iteration is None
        else canonical_model_iteration(parent_iteration)
    )
    if prior is not None:
        if parent_iteration_sha256 is not None:
            raise ModelIterationQaError(
                "model iteration parent was supplied twice"
            )
        if (
            prior["project_id"] != project_text
            or not same_promoted_model_artifact(
                prior["parent_promoted_model_binding"], model
            )
            or prior["next_dataset"]["dataset_version_id"]
            != parent["dataset_version_id"]
            or prior["next_dataset"]["dataset_hash"]
            != parent["dataset_hash"]
            or prior["next_dataset"][
                "dataset_provenance_graph_sha256"
            ]
            != parent["dataset_provenance_graph_sha256"]
        ):
            raise ModelIterationQaError(
                "model iteration parent graph is invalid"
            )
        parent_iteration_sha256 = prior["model_iteration_sha256"]
    elif parent_iteration_sha256 is not None:
        raise ModelIterationQaError(
            "model iteration parent manifest is required"
        )
    elif (
        model["dataset_version_id"] != parent["dataset_version_id"]
        or model["dataset_hash"] != parent["dataset_hash"]
        or model["dataset_provenance_graph_sha256"]
        != parent["dataset_provenance_graph_sha256"]
    ):
        raise ModelIterationQaError(
            "first model iteration must start from the promoted dataset"
        )
    if next_dataset["dataset_version_id"] == parent["dataset_version_id"]:
        raise ModelIterationQaError(
            "model iteration requires a new DatasetVersion"
        )
    if next_dataset["split_strategy"] != "project_fixed_analysis_job_v1":
        raise ModelIterationQaError(
            "next DatasetVersion must retain fixed validation"
        )

    def member_identity(member: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            member["annotation_id"],
            member["question_id"],
            member["analysis_job_id"],
            member["candidate_id"],
            member["answer_record_id"],
            member["answer_revision"],
            member["partition"],
            member["include_in_training"],
            member["split"],
            member["source_record_hash"],
            member["annotation_hash"],
        )

    parent_members = {
        member["annotation_id"]: member for member in parent["memberships"]
    }
    next_members = {
        member["annotation_id"]: member
        for member in next_dataset["memberships"]
    }
    if len(parent_members) != len(parent["memberships"]) or len(
        next_members
    ) != len(next_dataset["memberships"]):
        raise ModelIterationQaError(
            "dataset contains duplicate Annotation identities"
        )
    if any(
        annotation_id not in next_members
        or member_identity(next_members[annotation_id])
        != member_identity(member)
        for annotation_id, member in parent_members.items()
    ):
        raise ModelIterationQaError(
            "next DatasetVersion changed parent truth"
        )
    parent_validation = {
        member_identity(member)
        for member in parent["memberships"]
        if member["split"] == "validation"
    }
    next_validation = {
        member_identity(member)
        for member in next_dataset["memberships"]
        if member["split"] == "validation"
    }
    if parent_validation != next_validation:
        raise ModelIterationQaError(
            "next DatasetVersion changed fixed validation"
        )
    validation_jobs = sorted(
        {
            member["analysis_job_id"]
            for member in parent["memberships"]
            if member["split"] == "validation"
        }
    )
    if validation_jobs != model["fixed_validation_analysis_job_ids"]:
        raise ModelIterationQaError(
            "model fixed-validation lineage does not match parent dataset"
        )
    feedback = survey["training_feedback_annotations"]
    feedback_by_id = {
        item["annotation_id"]: item for item in feedback
    }
    if len(feedback_by_id) != len(feedback):
        raise ModelIterationQaError(
            "survey feedback contains duplicate Annotations"
        )
    added_ids = set(next_members).difference(parent_members)
    training_added_ids = {
        annotation_id
        for annotation_id in added_ids
        if next_members[annotation_id]["split"] == "train"
    }
    if training_added_ids != set(feedback_by_id):
        raise ModelIterationQaError(
            "next DatasetVersion TRAIN delta does not equal survey feedback"
        )
    for annotation_id, item in feedback_by_id.items():
        member = next_members[annotation_id]
        if (
            member["annotation_hash"] != item["annotation_hash"]
            or member["analysis_job_id"] != survey["analysis_job_id"]
            or member["analysis_job_id"] != item["analysis_job_id"]
            or member["question_id"] != item["question_id"]
            or member["candidate_id"] != item["candidate_id"]
            or member["split"] != "train"
            or member["include_in_training"] is not True
        ):
            raise ModelIterationQaError(
                "survey feedback is not an exact TRAIN membership"
            )
    audit_added_ids = added_ids.difference(training_added_ids)
    held_outcomes = {
        (item["question_id"], item["candidate_id"])
        for item in survey["reviewed_outcomes"]
        if item["training_authorized"] is False
    }
    audit_outcome_keys = set()
    audit_only_members = []
    for annotation_id in sorted(audit_added_ids):
        member = next_members[annotation_id]
        outcome_key = (
            member["question_id"],
            member["candidate_id"],
        )
        if (
            member["analysis_job_id"] != survey["analysis_job_id"]
            or member["split"] != "excluded"
            or member["include_in_training"] is not False
            or outcome_key not in held_outcomes
            or outcome_key in audit_outcome_keys
        ):
            raise ModelIterationQaError(
                "next DatasetVersion audit delta is not held survey evidence"
            )
        audit_outcome_keys.add(outcome_key)
        audit_only_members.append(
            {
                "annotation_id": member["annotation_id"],
                "annotation_hash": member["annotation_hash"],
                "analysis_job_id": member["analysis_job_id"],
                "question_id": member["question_id"],
                "candidate_id": member["candidate_id"],
                "answer_record_id": member["answer_record_id"],
                "answer_revision": member["answer_revision"],
                "source_record_hash": member["source_record_hash"],
                "split": "excluded",
                "training_only": False,
            }
        )
    if survey["analysis_job_id"] in {
        *model["training_analysis_job_ids"],
        *model["fixed_validation_analysis_job_ids"],
    }:
        raise ModelIterationQaError(
            "survey feedback reused parent model source evidence"
        )
    parent_iteration_digest = (
        None
        if parent_iteration_sha256 is None
        else _sha(
            parent_iteration_sha256, "parent_iteration_sha256"
        )
    )
    base = {
        "schema_version": MODEL_ITERATION_SCHEMA,
        "project_id": project_text,
        "parent_iteration_sha256": parent_iteration_digest,
        "parent_promoted_model_binding": model,
        "parent_dataset": {
            key: parent[key]
            for key in (
                "dataset_version_id",
                "dataset_hash",
                "dataset_provenance_graph_sha256",
            )
        },
        "survey_model_qa_sha256": survey[
            "survey_model_qa_sha256"
        ],
        "survey_analysis_job_id": survey["analysis_job_id"],
        "survey_qa_revision": survey["survey_qa_revision"],
        "feedback_annotations": feedback,
        "audit_only_members": audit_only_members,
        "next_dataset": {
            key: next_dataset[key]
            for key in (
                "dataset_version_id",
                "dataset_hash",
                "dataset_provenance_graph_sha256",
                "split_strategy",
            )
        },
        "fixed_validation_analysis_job_ids": validation_jobs,
        "fixed_validation_sha256": model[
            "fixed_validation_sha256"
        ],
        "next_training_requires_user_confirmation": True,
        "automatic_training_start": False,
        "automatic_model_promotion": False,
        "incident_authority_created": False,
        "report_truth_created": False,
    }
    return canonical_model_iteration(
        {**base, "model_iteration_sha256": _digest(base)}
    )


def canonical_model_iteration(value: object) -> dict[str, Any]:
    """Validate the digest and non-automatic authority of one iteration."""

    expected = {
        "schema_version",
        "project_id",
        "parent_iteration_sha256",
        "parent_promoted_model_binding",
        "parent_dataset",
        "survey_model_qa_sha256",
        "survey_analysis_job_id",
        "survey_qa_revision",
        "feedback_annotations",
        "audit_only_members",
        "next_dataset",
        "fixed_validation_analysis_job_ids",
        "fixed_validation_sha256",
        "next_training_requires_user_confirmation",
        "automatic_training_start",
        "automatic_model_promotion",
        "incident_authority_created",
        "report_truth_created",
        "model_iteration_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ModelIterationQaError(
            "model iteration fields are invalid"
        )
    base = dict(value)
    digest = base.pop("model_iteration_sha256")
    if (
        value["schema_version"] != MODEL_ITERATION_SCHEMA
        or _sha(digest, "model_iteration_sha256") != _digest(base)
        or value["next_training_requires_user_confirmation"] is not True
        or any(
            value[field] is not False
            for field in (
                "automatic_training_start",
                "automatic_model_promotion",
                "incident_authority_created",
                "report_truth_created",
            )
        )
    ):
        raise ModelIterationQaError(
            "model iteration digest or authority is invalid"
        )
    project_id = _uuid(value["project_id"], "project_id")
    model = _canonical_promoted_model(
        value["parent_promoted_model_binding"],
        project_id=project_id,
    )
    if model != value["parent_promoted_model_binding"]:
        raise ModelIterationQaError(
            "iteration model binding is not canonical"
        )
    for label in ("parent_dataset", "next_dataset"):
        dataset = value[label]
        expected_dataset_fields = {
            "dataset_version_id",
            "dataset_hash",
            "dataset_provenance_graph_sha256",
        }
        if label == "next_dataset":
            expected_dataset_fields.add("split_strategy")
        if (
            not isinstance(dataset, Mapping)
            or set(dataset) != expected_dataset_fields
        ):
            raise ModelIterationQaError(
                "iteration dataset binding is invalid"
            )
        for field in (
            "dataset_version_id",
            "dataset_hash",
            "dataset_provenance_graph_sha256",
        ):
            if field == "dataset_version_id":
                _uuid(dataset.get(field), field)
            else:
                _sha(dataset.get(field), field)
    if value["next_dataset"].get("split_strategy") != (
        "project_fixed_analysis_job_v1"
    ):
        raise ModelIterationQaError(
            "iteration fixed-validation strategy is invalid"
        )
    if value["parent_iteration_sha256"] is None and (
        value["parent_dataset"].get("dataset_version_id")
        != model["dataset_version_id"]
        or value["parent_dataset"].get("dataset_hash")
        != model["dataset_hash"]
        or value["parent_dataset"].get(
            "dataset_provenance_graph_sha256"
        )
        != model["dataset_provenance_graph_sha256"]
    ):
        raise ModelIterationQaError(
            "first iteration parent dataset does not match model"
        )
    if (
        value["next_dataset"]["dataset_version_id"]
        == value["parent_dataset"]["dataset_version_id"]
    ):
        raise ModelIterationQaError(
            "iteration next DatasetVersion is not new"
        )
    _sha(
        value["survey_model_qa_sha256"],
        "survey_model_qa_sha256",
    )
    _uuid(value["survey_analysis_job_id"], "survey_analysis_job_id")
    _sha(value["survey_qa_revision"], "survey_qa_revision")
    _sha(
        value["fixed_validation_sha256"],
        "fixed_validation_sha256",
    )
    validation_ids = _canonical_id_list(
        value["fixed_validation_analysis_job_ids"],
        "fixed_validation_analysis_job_ids",
    )
    if (
        validation_ids != model["fixed_validation_analysis_job_ids"]
        or value["fixed_validation_sha256"]
        != model["fixed_validation_sha256"]
    ):
        raise ModelIterationQaError(
            "iteration fixed validation changed"
        )
    if value["parent_iteration_sha256"] is not None:
        _sha(
            value["parent_iteration_sha256"],
            "parent_iteration_sha256",
        )
    feedback = value["feedback_annotations"]
    if not isinstance(feedback, list):
        raise ModelIterationQaError(
            "iteration feedback annotations are invalid"
        )
    expected_feedback_fields = {
        "annotation_id",
        "annotation_hash",
        "analysis_job_id",
        "question_id",
        "candidate_id",
        "review_event_id",
        "review_event_revision",
        "origin",
        "training_only",
    }
    annotation_ids = []
    for item in feedback:
        if (
            not isinstance(item, Mapping)
            or set(item) != expected_feedback_fields
            or item.get("training_only") is not True
            or item.get("origin") not in _ORIGINS
            or item.get("analysis_job_id")
            != value["survey_analysis_job_id"]
        ):
            raise ModelIterationQaError(
                "iteration feedback annotation boundary is invalid"
            )
        annotation_ids.append(
            _uuid(item["annotation_id"], "annotation_id")
        )
        _sha(item["annotation_hash"], "annotation_hash")
        _uuid(item["analysis_job_id"], "analysis_job_id")
        _uuid(item["question_id"], "question_id")
        _event_identifier(item["candidate_id"], "candidate_id")
        _event_identifier(
            item["review_event_id"], "review_event_id"
        )
        _sha(
            item["review_event_revision"],
            "review_event_revision",
        )
    if len(annotation_ids) != len(set(annotation_ids)):
        raise ModelIterationQaError(
            "iteration feedback annotations are duplicated"
        )
    audit_members = value["audit_only_members"]
    if not isinstance(audit_members, list):
        raise ModelIterationQaError(
            "iteration audit-only members are invalid"
        )
    expected_audit_fields = {
        "annotation_id",
        "annotation_hash",
        "analysis_job_id",
        "question_id",
        "candidate_id",
        "answer_record_id",
        "answer_revision",
        "source_record_hash",
        "split",
        "training_only",
    }
    audit_ids = []
    audit_keys = []
    for item in audit_members:
        if (
            not isinstance(item, Mapping)
            or set(item) != expected_audit_fields
            or item.get("split") != "excluded"
            or item.get("training_only") is not False
            or item.get("analysis_job_id")
            != value["survey_analysis_job_id"]
        ):
            raise ModelIterationQaError(
                "iteration audit-only membership boundary is invalid"
            )
        audit_ids.append(
            _uuid(item["annotation_id"], "annotation_id")
        )
        _sha(item["annotation_hash"], "annotation_hash")
        _uuid(item["analysis_job_id"], "analysis_job_id")
        question_id = _uuid(item["question_id"], "question_id")
        candidate_id = _event_identifier(
            item["candidate_id"], "candidate_id"
        )
        _uuid(item["answer_record_id"], "answer_record_id")
        revision = item["answer_revision"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise ModelIterationQaError(
                "iteration audit-only answer revision is invalid"
            )
        _sha(item["source_record_hash"], "source_record_hash")
        audit_keys.append((question_id, candidate_id))
    if (
        len(audit_ids) != len(set(audit_ids))
        or len(audit_keys) != len(set(audit_keys))
        or set(audit_ids).intersection(annotation_ids)
    ):
        raise ModelIterationQaError(
            "iteration audit-only members are duplicated"
        )
    if value["survey_analysis_job_id"] in {
        *model["training_analysis_job_ids"],
        *model["fixed_validation_analysis_job_ids"],
    }:
        raise ModelIterationQaError(
            "iteration survey reused parent model source evidence"
        )
    return {**base, "model_iteration_sha256": digest}


class ModelIterationQaStore:
    """Restart-safe, hash-verifying ledger for QA and iteration manifests."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path).resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS survey_model_qa (
                    sha256 TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    pipeline_job_id TEXT NOT NULL,
                    survey_qa_revision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(project_id, pipeline_job_id, survey_qa_revision)
                );
                CREATE INDEX IF NOT EXISTS survey_model_qa_project
                    ON survey_model_qa(project_id, pipeline_job_id);

                CREATE TABLE IF NOT EXISTS model_iterations (
                    sha256 TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    parent_model_version_id TEXT NOT NULL,
                    next_dataset_version_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(
                        project_id,
                        parent_model_version_id,
                        next_dataset_version_id
                    )
                );
                CREATE INDEX IF NOT EXISTS model_iterations_project
                    ON model_iterations(project_id);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._database_path),
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def put_survey_model_qa(
        self, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        canonical = canonical_survey_model_qa(value)
        payload = _canonical_bytes(canonical).decode("utf-8")
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO survey_model_qa(
                        sha256, project_id, pipeline_job_id,
                        survey_qa_revision, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        canonical["survey_model_qa_sha256"],
                        canonical["project_id"],
                        canonical["pipeline_job_id"],
                        canonical["survey_qa_revision"],
                        payload,
                    ),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT payload_json FROM survey_model_qa
                    WHERE project_id = ? AND pipeline_job_id = ?
                      AND survey_qa_revision = ?
                    """,
                    (
                        canonical["project_id"],
                        canonical["pipeline_job_id"],
                        canonical["survey_qa_revision"],
                    ),
                ).fetchone()
                if row is None or row["payload_json"] != payload:
                    raise ModelIterationQaError(
                        "survey QA identity was already used"
                    )
        return canonical

    def put_model_iteration(
        self, value: Mapping[str, Any]
    ) -> dict[str, Any]:
        canonical = canonical_model_iteration(value)
        payload = _canonical_bytes(canonical).decode("utf-8")
        model_id = canonical["parent_promoted_model_binding"][
            "model_version_id"
        ]
        dataset_id = canonical["next_dataset"][
            "dataset_version_id"
        ]
        with self._lock, self._connect() as connection:
            parent_sha256 = canonical["parent_iteration_sha256"]
            if parent_sha256 is not None:
                parent_row = connection.execute(
                    """
                    SELECT payload_json FROM model_iterations
                    WHERE sha256 = ? AND project_id = ?
                    """,
                    (parent_sha256, canonical["project_id"]),
                ).fetchone()
                if parent_row is None:
                    raise ModelIterationQaError(
                        "model iteration parent is not in the project ledger"
                    )
                parent = canonical_model_iteration(
                    json.loads(parent_row["payload_json"])
                )
                if (
                    not same_promoted_model_artifact(
                        parent["parent_promoted_model_binding"],
                        canonical["parent_promoted_model_binding"],
                    )
                    or parent["next_dataset"]
                    != {
                        **canonical["parent_dataset"],
                        "split_strategy": (
                            parent["next_dataset"]["split_strategy"]
                        ),
                    }
                ):
                    raise ModelIterationQaError(
                        "model iteration parent ledger graph is invalid"
                    )
            try:
                connection.execute(
                    """
                    INSERT INTO model_iterations(
                        sha256, project_id, parent_model_version_id,
                        next_dataset_version_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        canonical["model_iteration_sha256"],
                        canonical["project_id"],
                        model_id,
                        dataset_id,
                        payload,
                    ),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    """
                    SELECT payload_json FROM model_iterations
                    WHERE project_id = ?
                      AND parent_model_version_id = ?
                      AND next_dataset_version_id = ?
                    """,
                    (
                        canonical["project_id"],
                        model_id,
                        dataset_id,
                    ),
                ).fetchone()
                if row is None or row["payload_json"] != payload:
                    raise ModelIterationQaError(
                        "model iteration identity was already used"
                    )
        return canonical

    def list_survey_model_qa(
        self, project_id: UUID | str
    ) -> list[dict[str, Any]]:
        project_text = _uuid(str(project_id), "project_id")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sha256, payload_json FROM survey_model_qa
                WHERE project_id = ?
                ORDER BY rowid
                """,
                (project_text,),
            ).fetchall()
        result = []
        for row in rows:
            canonical = canonical_survey_model_qa(
                json.loads(row["payload_json"])
            )
            if (
                canonical["project_id"] != project_text
                or canonical["survey_model_qa_sha256"]
                != row["sha256"]
            ):
                raise ModelIterationQaError(
                    "persisted survey QA index is corrupt"
                )
            result.append(canonical)
        return result

    def list_model_iterations(
        self, project_id: UUID | str
    ) -> list[dict[str, Any]]:
        project_text = _uuid(str(project_id), "project_id")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sha256, payload_json FROM model_iterations
                WHERE project_id = ?
                ORDER BY rowid
                """,
                (project_text,),
            ).fetchall()
        result = []
        for row in rows:
            canonical = canonical_model_iteration(
                json.loads(row["payload_json"])
            )
            if (
                canonical["project_id"] != project_text
                or canonical["model_iteration_sha256"]
                != row["sha256"]
            ):
                raise ModelIterationQaError(
                    "persisted model iteration index is corrupt"
                )
            result.append(canonical)
        return result


__all__ = [
    "MODEL_ITERATION_SCHEMA",
    "SURVEY_MODEL_QA_SCHEMA",
    "ModelIterationQaError",
    "ModelIterationQaStore",
    "build_model_iteration",
    "build_reviewed_survey_outcomes",
    "build_survey_model_qa",
    "canonical_model_iteration",
    "canonical_survey_model_qa",
    "same_promoted_model_artifact",
]
