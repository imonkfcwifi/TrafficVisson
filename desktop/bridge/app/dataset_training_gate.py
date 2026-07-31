"""Deterministic DatasetVersion readiness evidence.

The DatasetVersion is allowed to exist before model training is eligible.  This
module derives a path-free, hash-bound readiness snapshot from the immutable
DatasetVersion memberships and their Annotation rows.  Historical 3Q/4Q
approved references remain useful auxiliary seeds, but they never satisfy the
100-current-video TRAIN gate or the fixed-validation 30-per-type gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Mapping, Sequence

from .candidate_labels import CANONICAL_ISSUE_TYPES
from .dataset_versions import DatasetVersionError, canonical_json_bytes


DATASET_TRAINING_GATE_SCHEMA = (
    "trafficvisson.dataset-training-gate.v1"
)
MINIMUM_CURRENT_VIDEO_TRAIN_ANNOTATIONS = 100
MINIMUM_FIXED_VALIDATION_SAMPLES_PER_TYPE = 30

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_TRAINING_SOURCES = frozenset(
    {"current_video_confirmed", "structured_llm_assisted"}
)


def _annotation_value(row: Mapping[str, Any]) -> Dict[str, Any]:
    raw = row.get("annotation_json")
    if not isinstance(raw, str):
        raise DatasetVersionError(
            "DatasetVersion gate Annotation provenance is unavailable"
        )
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise DatasetVersionError(
            "DatasetVersion gate Annotation provenance is invalid"
        ) from error
    if not isinstance(value, dict):
        raise DatasetVersionError(
            "DatasetVersion gate Annotation provenance is invalid"
        )
    return value


def _canonical_issue_types(
    rows: Sequence[Mapping[str, Any]],
    approved_reference_members: Sequence[Mapping[str, Any]],
) -> list[str]:
    observed = {
        str(row["selected_issue_type"])
        for row in rows
        if (
            row.get("include_in_training") in {1, True}
            and row.get("partition") == "approved"
            and row.get("selected_issue_type")
            in CANONICAL_ISSUE_TYPES
        )
    }
    observed.update(
        str(reference["issue_type"])
        for reference in approved_reference_members
        if (
            isinstance(reference, Mapping)
            and reference.get("issue_type")
            in CANONICAL_ISSUE_TYPES
        )
    )
    return [
        issue_type
        for issue_type in CANONICAL_ISSUE_TYPES
        if issue_type in observed
    ]


def build_dataset_training_gate(
    *,
    dataset_version_id: str,
    dataset_hash: str,
    membership_rows: Sequence[Mapping[str, Any]],
    approved_reference_members: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build the immutable readiness view for one DatasetVersion."""

    if (
        not isinstance(dataset_version_id, str)
        or not dataset_version_id
        or not isinstance(dataset_hash, str)
        or _SHA256.fullmatch(dataset_hash) is None
        or isinstance(membership_rows, (str, bytes, bytearray))
        or not isinstance(membership_rows, Sequence)
        or isinstance(
            approved_reference_members,
            (str, bytes, bytearray),
        )
        or not isinstance(approved_reference_members, Sequence)
    ):
        raise DatasetVersionError(
            "DatasetVersion training gate identity is invalid"
        )

    rows = [dict(row) for row in membership_rows]
    annotation_ids = [str(row.get("annotation_id")) for row in rows]
    question_ids = [str(row.get("question_id")) for row in rows]
    if (
        len(annotation_ids) != len(set(annotation_ids))
        or len(question_ids) != len(set(question_ids))
    ):
        raise DatasetVersionError(
            "DatasetVersion training gate membership is duplicated"
        )

    annotations = {
        str(row["annotation_id"]): _annotation_value(row)
        for row in rows
    }
    train_rows = []
    fixed_validation_rows = []
    for row in rows:
        annotation = annotations[str(row["annotation_id"])]
        source_kind = annotation.get(
            "source_kind", "current_video_confirmed"
        )
        training_authorized = annotation.get(
            "training_authorized", True
        )
        label_source = annotation.get(
            "label_source", "local_user_review"
        )
        user_confirmed = annotation.get("user_confirmed", True)
        if (
            row.get("include_in_training") in {1, True}
            and row.get("split") == "train"
        ):
            if (
                source_kind not in _TRAINING_SOURCES
                or training_authorized is not True
            ):
                raise DatasetVersionError(
                    "DatasetVersion TRAIN member lacks label authority"
                )
            train_rows.append(row)
        if (
            row.get("include_in_training") in {1, True}
            and row.get("split") == "validation"
        ):
            if (
                source_kind != "current_video_confirmed"
                or label_source != "local_user_review"
                or user_confirmed is not True
                or training_authorized is not True
            ):
                raise DatasetVersionError(
                    "fixed validation requires local-user-confirmed evidence"
                )
            fixed_validation_rows.append(row)

    required_issue_types = _canonical_issue_types(
        rows,
        approved_reference_members,
    )
    type_statuses = []
    for issue_type in required_issue_types:
        sample_count = len(fixed_validation_rows)
        positive_count = sum(
            row.get("partition") == "approved"
            and row.get("selected_issue_type") == issue_type
            for row in fixed_validation_rows
        )
        type_statuses.append(
            {
                "issue_type": issue_type,
                "sample_count": sample_count,
                "positive_count": positive_count,
                "required_sample_count": (
                    MINIMUM_FIXED_VALIDATION_SAMPLES_PER_TYPE
                ),
                "ready": (
                    sample_count
                    >= MINIMUM_FIXED_VALIDATION_SAMPLES_PER_TYPE
                ),
            }
        )

    train_annotation_count = len(train_rows)
    train_question_count = len(
        {str(row["question_id"]) for row in train_rows}
    )
    payload = {
        "schema_version": DATASET_TRAINING_GATE_SCHEMA,
        "dataset_version_id": dataset_version_id,
        "dataset_hash": dataset_hash,
        "required_current_video_train_unique_annotations": (
            MINIMUM_CURRENT_VIDEO_TRAIN_ANNOTATIONS
        ),
        "current_video_train_unique_annotation_count": (
            train_annotation_count
        ),
        "current_video_train_unique_question_count": (
            train_question_count
        ),
        "required_fixed_validation_samples_per_type": (
            MINIMUM_FIXED_VALIDATION_SAMPLES_PER_TYPE
        ),
        "required_issue_types": required_issue_types,
        "fixed_validation_type_statuses": type_statuses,
        "approved_reference_member_count": len(
            approved_reference_members
        ),
        "approved_references_count_toward_training_gate": False,
        "approved_references_count_toward_fixed_validation": False,
        "ready_for_explicit_training": (
            train_annotation_count
            >= MINIMUM_CURRENT_VIDEO_TRAIN_ANNOTATIONS
            and bool(type_statuses)
            and all(status["ready"] for status in type_statuses)
        ),
    }
    return {
        **payload,
        "gate_snapshot_sha256": hashlib.sha256(
            canonical_json_bytes(payload)
        ).hexdigest(),
    }


__all__ = [
    "DATASET_TRAINING_GATE_SCHEMA",
    "MINIMUM_CURRENT_VIDEO_TRAIN_ANNOTATIONS",
    "MINIMUM_FIXED_VALIDATION_SAMPLES_PER_TYPE",
    "build_dataset_training_gate",
]
