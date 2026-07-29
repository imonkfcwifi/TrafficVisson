"""Normalized label projections and immutable dataset snapshot helpers.

The append-only candidate-label JSONL file remains the audit authority.  This
module validates one of those records without mutating it, derives the
normalized rows stored by :mod:`project_catalog`, and builds a deterministic
dataset snapshot from the latest answer revision for every question.

Dataset splitting is performed at ``analysis_job_id`` grain.  All candidates
from one source analysis job therefore remain in the same train/validation
partition and cannot leak across the validation boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from uuid import UUID, uuid5


LABEL_RECORD_SCHEMA_VERSION = "1.0"
DATASET_SCHEMA_VERSION = "trafficvisson.dataset-version.v1"
SPLIT_STRATEGY = "analysis_job_hash_v1"
DEFAULT_VALIDATION_FRACTION = 0.2

PARTITION_BY_ANSWER = {
    "yes": "approved",
    "edit": "approved",
    "no": "negative",
    "unsure": "held",
    "skipped": "excluded",
}
TRAINING_BY_ANSWER = {
    "yes": True,
    "edit": True,
    "no": True,
    "unsure": False,
    "skipped": False,
}

_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_RECORD_BYTES = 384 * 1024
_MAX_CANDIDATE_BYTES = 256 * 1024
_MAX_TEXT = 500
_MAX_ACTOR = 128


class LabelProjectionError(ValueError):
    """An audit record cannot be safely projected into normalized storage."""


class DatasetVersionError(ValueError):
    """The current normalized labels cannot form a dataset snapshot."""


@dataclass(frozen=True)
class LabelProjection:
    record_id: str
    project_id: str
    analysis_job_id: str
    candidate_id: str
    question_id: str
    answer_revision: int
    answer: str
    partition: str
    include_in_training: bool
    selected_issue_type: Optional[str]
    severity: Optional[str]
    maintenance_action: Optional[str]
    ocr_text: Optional[str]
    normalized_box_json: Optional[str]
    answered_by: str
    recorded_at: str
    previous_record_hash: Optional[str]
    record_hash: str
    record_json: str
    candidate_json: str
    candidate_sha256: str
    annotation_id: str
    annotation_hash: str
    annotation_json: str


@dataclass(frozen=True)
class DatasetMember:
    annotation_id: str
    question_id: str
    analysis_job_id: str
    candidate_id: str
    answer_record_id: str
    answer_revision: int
    partition: str
    include_in_training: bool
    split: str
    source_record_hash: str
    annotation_hash: str

    def canonical_value(self) -> Dict[str, Any]:
        return {
            "analysis_job_id": self.analysis_job_id,
            "annotation_hash": self.annotation_hash,
            "annotation_id": self.annotation_id,
            "answer_record_id": self.answer_record_id,
            "answer_revision": self.answer_revision,
            "candidate_id": self.candidate_id,
            "include_in_training": self.include_in_training,
            "partition": self.partition,
            "question_id": self.question_id,
            "source_record_hash": self.source_record_hash,
            "split": self.split,
        }


@dataclass(frozen=True)
class DatasetSnapshot:
    dataset_id: str
    project_id: str
    dataset_hash: str
    validation_fraction: float
    members: Tuple[DatasetMember, ...]
    manifest_json: str

    @property
    def source_answer_count(self) -> int:
        return len(self.members)

    @property
    def training_member_count(self) -> int:
        return sum(member.include_in_training for member in self.members)

    @property
    def train_count(self) -> int:
        return sum(member.split == "train" for member in self.members)

    @property
    def validation_count(self) -> int:
        return sum(member.split == "validation" for member in self.members)

    @property
    def excluded_count(self) -> int:
        return sum(member.split == "excluded" for member in self.members)


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LabelProjectionError("label record is not canonical JSON") from error


def canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise LabelProjectionError(f"{label} must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise LabelProjectionError(f"{label} must be a canonical UUID") from error
    if str(parsed) != value:
        raise LabelProjectionError(f"{label} must be a canonical UUID")
    return value


def _bounded_optional_text(
    value: object,
    label: str,
    *,
    maximum: int = _MAX_TEXT,
) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LabelProjectionError(f"{label} must be text")
    if not value or value != value.strip() or len(value) > maximum:
        raise LabelProjectionError(f"{label} is invalid")
    if any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"}
        for character in value
    ):
        raise LabelProjectionError(f"{label} contains unsupported controls")
    return value


def _normalized_box_json(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise LabelProjectionError("normalized_box is invalid")
    normalized: Dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = value[key]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
        ):
            raise LabelProjectionError("normalized_box is invalid")
        normalized[key] = float(number)
    if (
        not 0 <= normalized["x"] <= 1
        or not 0 <= normalized["y"] <= 1
        or not 0 < normalized["width"] <= 1
        or not 0 < normalized["height"] <= 1
        or normalized["x"] + normalized["width"] > 1 + 1e-12
        or normalized["y"] + normalized["height"] > 1 + 1e-12
    ):
        raise LabelProjectionError("normalized_box is outside the image")
    return canonical_json_text(normalized)


def _validate_recorded_at(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise LabelProjectionError("recorded_at must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise LabelProjectionError("recorded_at must be a UTC timestamp") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise LabelProjectionError("recorded_at must be a UTC timestamp")
    return value


def normalize_label_record(record: Mapping[str, Any]) -> LabelProjection:
    """Validate one candidate-label JSONL record and derive normalized rows."""

    if not isinstance(record, Mapping):
        raise LabelProjectionError("label record must be an object")
    required = {
        "schema_version",
        "record_id",
        "project_id",
        "analysis_job_id",
        "candidate_id",
        "question_id",
        "answer_revision",
        "partition",
        "ai_original_candidate",
        "final_answer",
        "recorded_at",
        "previous_record_hash",
        "record_hash",
    }
    if set(record) != required:
        raise LabelProjectionError("label record fields are invalid")
    if record["schema_version"] != LABEL_RECORD_SCHEMA_VERSION:
        raise LabelProjectionError("label record schema is unsupported")

    record_id = _canonical_uuid(record["record_id"], "record_id")
    project_id = _canonical_uuid(record["project_id"], "project_id")
    analysis_job_id = _canonical_uuid(
        record["analysis_job_id"], "analysis_job_id"
    )
    question_id = _canonical_uuid(record["question_id"], "question_id")
    candidate_id = record["candidate_id"]
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(
        candidate_id
    ):
        raise LabelProjectionError("candidate_id is invalid")
    expected_question_id = str(uuid5(UUID(analysis_job_id), candidate_id))
    if question_id != expected_question_id:
        raise LabelProjectionError("question_id is not deterministic")

    revision = record["answer_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise LabelProjectionError("answer_revision must be positive")

    final_answer = record["final_answer"]
    if not isinstance(final_answer, Mapping) or set(final_answer) != {
        "answer",
        "selected_issue_type",
        "severity",
        "maintenance_action",
        "ocr_text",
        "normalized_box",
        "include_in_training",
        "answered_by",
    }:
        raise LabelProjectionError("final_answer fields are invalid")
    answer = final_answer["answer"]
    if not isinstance(answer, str) or answer not in PARTITION_BY_ANSWER:
        raise LabelProjectionError("answer is unsupported")
    partition = record["partition"]
    if partition != PARTITION_BY_ANSWER[answer]:
        raise LabelProjectionError("answer partition is inconsistent")
    include_in_training = final_answer["include_in_training"]
    if (
        not isinstance(include_in_training, bool)
        or include_in_training != TRAINING_BY_ANSWER[answer]
    ):
        raise LabelProjectionError("training inclusion is inconsistent")

    selected_issue_type = _bounded_optional_text(
        final_answer["selected_issue_type"], "selected_issue_type", maximum=128
    )
    severity = _bounded_optional_text(
        final_answer["severity"], "severity", maximum=32
    )
    maintenance_action = _bounded_optional_text(
        final_answer["maintenance_action"], "maintenance_action"
    )
    ocr_text = _bounded_optional_text(final_answer["ocr_text"], "ocr_text")
    normalized_box_json = _normalized_box_json(final_answer["normalized_box"])
    answered_by = _bounded_optional_text(
        final_answer["answered_by"], "answered_by", maximum=_MAX_ACTOR
    )
    if answered_by is None:
        raise LabelProjectionError("answered_by is required")
    if answer in {"yes", "edit"} and selected_issue_type is None:
        raise LabelProjectionError("approved answers require an issue type")
    if answer == "edit" and normalized_box_json is None:
        raise LabelProjectionError("edited answers require a box")
    if answer in {"no", "unsure", "skipped"} and any(
        value is not None
        for value in (
            selected_issue_type,
            severity,
            maintenance_action,
            ocr_text,
            normalized_box_json,
        )
    ):
        raise LabelProjectionError("non-approved answers contain positive details")

    candidate = record["ai_original_candidate"]
    if not isinstance(candidate, Mapping) or candidate.get(
        "candidate_id"
    ) != candidate_id:
        raise LabelProjectionError("candidate metadata is invalid")
    candidate_bytes = canonical_json_bytes(candidate)
    if len(candidate_bytes) > _MAX_CANDIDATE_BYTES:
        raise LabelProjectionError("candidate metadata is too large")
    candidate_json = candidate_bytes.decode("utf-8")
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()

    previous_record_hash = record["previous_record_hash"]
    if previous_record_hash is not None and (
        not isinstance(previous_record_hash, str)
        or not _SHA256.fullmatch(previous_record_hash)
    ):
        raise LabelProjectionError("previous_record_hash is invalid")
    record_hash = record["record_hash"]
    if not isinstance(record_hash, str) or not _SHA256.fullmatch(record_hash):
        raise LabelProjectionError("record_hash is invalid")
    record_without_hash = dict(record)
    record_without_hash.pop("record_hash")
    expected_record_hash = hashlib.sha256(
        canonical_json_bytes(record_without_hash)
    ).hexdigest()
    if record_hash != expected_record_hash:
        raise LabelProjectionError("record_hash does not match the audit record")
    record_bytes = canonical_json_bytes(record)
    if len(record_bytes) > _MAX_RECORD_BYTES:
        raise LabelProjectionError("label record is too large")

    recorded_at = _validate_recorded_at(record["recorded_at"])
    annotation_id = record_id
    annotation_value = {
        "analysis_job_id": analysis_job_id,
        "answer_record_id": record_id,
        "answer_revision": revision,
        "candidate_id": candidate_id,
        "include_in_training": include_in_training,
        "maintenance_action": maintenance_action,
        "normalized_box": (
            json.loads(normalized_box_json)
            if normalized_box_json is not None
            else None
        ),
        "ocr_text": ocr_text,
        "partition": partition,
        "project_id": project_id,
        "question_id": question_id,
        "selected_issue_type": selected_issue_type,
        "severity": severity,
        "source_record_hash": record_hash,
    }
    annotation_bytes = canonical_json_bytes(annotation_value)

    return LabelProjection(
        record_id=record_id,
        project_id=project_id,
        analysis_job_id=analysis_job_id,
        candidate_id=candidate_id,
        question_id=question_id,
        answer_revision=revision,
        answer=answer,
        partition=partition,
        include_in_training=include_in_training,
        selected_issue_type=selected_issue_type,
        severity=severity,
        maintenance_action=maintenance_action,
        ocr_text=ocr_text,
        normalized_box_json=normalized_box_json,
        answered_by=answered_by,
        recorded_at=recorded_at,
        previous_record_hash=previous_record_hash,
        record_hash=record_hash,
        record_json=record_bytes.decode("utf-8"),
        candidate_json=candidate_json,
        candidate_sha256=candidate_sha256,
        annotation_id=annotation_id,
        annotation_hash=hashlib.sha256(annotation_bytes).hexdigest(),
        annotation_json=annotation_bytes.decode("utf-8"),
    )


def _validation_fraction(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) < 1
    ):
        raise DatasetVersionError("validation_fraction must be between 0 and 1")
    return round(float(value), 6)


def build_dataset_snapshot(
    project_id: UUID | str,
    current_rows: Sequence[Mapping[str, Any]],
    *,
    validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
) -> DatasetSnapshot:
    """Create a deterministic snapshot from current normalized annotations."""

    project_text = str(project_id)
    try:
        project_uuid = UUID(project_text)
    except (ValueError, AttributeError) as error:
        raise DatasetVersionError("project_id must be a UUID") from error
    if str(project_uuid) != project_text:
        raise DatasetVersionError("project_id must be canonical")
    fraction = _validation_fraction(validation_fraction)
    if not current_rows:
        raise DatasetVersionError("at least one current label is required")

    normalized_rows = []
    seen_questions = set()
    for row in current_rows:
        if row.get("project_id") != project_text:
            raise DatasetVersionError("dataset rows crossed the project boundary")
        question_id = str(row.get("question_id", ""))
        if question_id in seen_questions:
            raise DatasetVersionError("current labels contain duplicate questions")
        seen_questions.add(question_id)
        include = row.get("include_in_training")
        partition = row.get("partition")
        if not isinstance(include, bool):
            raise DatasetVersionError("dataset row training inclusion is invalid")
        if include != (partition in {"approved", "negative"}):
            raise DatasetVersionError("dataset row partition is inconsistent")
        normalized_rows.append(dict(row))

    trainable_jobs = sorted(
        {
            str(row["analysis_job_id"])
            for row in normalized_rows
            if row["include_in_training"]
        }
    )
    ordered_jobs = sorted(
        trainable_jobs,
        key=lambda job_id: (
            hashlib.sha256(
                f"{DATASET_SCHEMA_VERSION}:{project_text}:{job_id}".encode(
                    "utf-8"
                )
            ).hexdigest(),
            job_id,
        ),
    )
    validation_jobs = set()
    if len(ordered_jobs) >= 2:
        validation_count = round(len(ordered_jobs) * fraction)
        validation_count = max(1, min(len(ordered_jobs) - 1, validation_count))
        validation_jobs.update(ordered_jobs[:validation_count])

    members = []
    for row in sorted(
        normalized_rows,
        key=lambda item: (
            str(item["question_id"]),
            int(item["answer_revision"]),
            str(item["annotation_id"]),
        ),
    ):
        if not row["include_in_training"]:
            split = "excluded"
        elif str(row["analysis_job_id"]) in validation_jobs:
            split = "validation"
        else:
            split = "train"
        members.append(
            DatasetMember(
                annotation_id=str(row["annotation_id"]),
                question_id=str(row["question_id"]),
                analysis_job_id=str(row["analysis_job_id"]),
                candidate_id=str(row["candidate_id"]),
                answer_record_id=str(row["answer_record_id"]),
                answer_revision=int(row["answer_revision"]),
                partition=str(row["partition"]),
                include_in_training=bool(row["include_in_training"]),
                split=split,
                source_record_hash=str(row["source_record_hash"]),
                annotation_hash=str(row["annotation_hash"]),
            )
        )

    fingerprint = {
        "members": [member.canonical_value() for member in members],
        "project_id": project_text,
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": SPLIT_STRATEGY,
        "validation_fraction": fraction,
    }
    dataset_hash = hashlib.sha256(canonical_json_bytes(fingerprint)).hexdigest()
    dataset_id = str(uuid5(project_uuid, f"{DATASET_SCHEMA_VERSION}:{dataset_hash}"))
    manifest = {
        **fingerprint,
        "dataset_hash": dataset_hash,
        "dataset_id": dataset_id,
        "counts": {
            "excluded": sum(member.split == "excluded" for member in members),
            "source_answers": len(members),
            "train": sum(member.split == "train" for member in members),
            "training_members": sum(
                member.include_in_training for member in members
            ),
            "validation": sum(
                member.split == "validation" for member in members
            ),
        },
    }
    return DatasetSnapshot(
        dataset_id=dataset_id,
        project_id=project_text,
        dataset_hash=dataset_hash,
        validation_fraction=fraction,
        members=tuple(members),
        manifest_json=canonical_json_text(manifest),
    )


__all__ = [
    "DATASET_SCHEMA_VERSION",
    "DEFAULT_VALIDATION_FRACTION",
    "DatasetMember",
    "DatasetSnapshot",
    "DatasetVersionError",
    "LabelProjection",
    "LabelProjectionError",
    "SPLIT_STRATEGY",
    "build_dataset_snapshot",
    "canonical_json_bytes",
    "canonical_json_text",
    "normalize_label_record",
]
