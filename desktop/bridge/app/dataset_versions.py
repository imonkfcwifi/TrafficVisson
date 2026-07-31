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

from .training_geometry import (
    TrainingGeometryError,
    canonical_training_geometry,
)
from .structured_assist_provenance import (
    StructuredAssistProvenanceError,
    canonical_structured_assist_provenance,
)
from .training_provenance import (
    TrainingProvenanceError,
    build_dataset_provenance_graph,
    canonical_annotation_provenance_graph,
)

LABEL_RECORD_SCHEMA_VERSION = "1.2"
PROVENANCE_LABEL_RECORD_SCHEMA_VERSION = "1.3"
GEOMETRY_LABEL_RECORD_SCHEMA_VERSION = "1.1"
LEGACY_LABEL_RECORD_SCHEMA_VERSION = "1.0"
DATASET_SCHEMA_VERSION = "trafficvisson.dataset-version.v1"
SPLIT_STRATEGY = "analysis_job_hash_v1"
FIXED_VALIDATION_SPLIT_STRATEGY = (
    "project_fixed_analysis_job_v1"
)
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
LABEL_SOURCE_BY_ANSWERED_BY = {
    "local_user": "local_user_review",
    "structured_llm_assisted": "structured_llm_assisted",
}

_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_APPROVAL_ID = re.compile(r"^approved-reference-[a-f0-9]{24}$")
_APPROVED_REFERENCE_SCHEMA = "trafficvisson.approved-reference.v2"
_SAMPLE_LAYOUT_BINDING_SCHEMA = (
    "trafficvisson.hwpx-sample-layout.v2"
)
_SAMPLE_LAYOUT_BINDING_REVISION = 2
_SEMANTIC_PROJECTION_REVISION = (
    "audited-photo-caption-evidence-group-v2"
)
_SAMPLE_SOURCE_ID = re.compile(
    r"^sample-hwpx-[34]q-[a-f0-9]{16}$"
)
_SAMPLE_CASE_ID = re.compile(
    r"^hwpx-weak-case-[a-f0-9]{32}$"
)
_PHOTO_SLOT_ID = re.compile(
    r"^sample-photo-slot-[a-f0-9]{24}$"
)
_PICTURE_TARGET_ID = re.compile(
    r"^hwpx-picture-[a-f0-9]{24}$"
)
_CELL_TARGET_ID = re.compile(
    r"^hwpx-cell-[a-f0-9]{24}$"
)
_SECTION_PATH = re.compile(
    r"^Contents/section[0-9]{1,5}\.xml$"
)
_SAMPLE_EVIDENCE_GROUP_ID = re.compile(
    r"^sample-evidence-group-[a-f0-9]{24}$"
)
_DUPLICATE_IMAGE_CASE_REF = re.compile(
    r"^sample-hwpx-[34]q-[a-f0-9]{16}/"
    r"hwpx-weak-case-[a-f0-9]{32}$"
)
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
    training_geometry_json: Optional[str]
    assist_provenance_json: Optional[str]
    provenance_graph_json: Optional[str]
    answered_by: str
    label_source: str
    source_kind: str
    user_confirmed: bool
    training_authorized: bool
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
class ApprovedReferenceMember:
    """One user-approved 3Q/4Q case frozen into a DatasetVersion.

    These members are training examples with an explicit historical-reference
    source. They remain lower-weight auxiliary seeds and never satisfy the
    current-video training-member gate or enter the project-fixed validation
    split.
    """

    schema_version: str
    approval_id: str
    approval_revision: int
    source_id: str
    case_id: str
    document_name: str
    document_sha256: str
    structure_target_summary_sha256: str
    index_manifest_sha256: str
    image_sha256: str
    image_media_type: str
    image_width: int
    image_height: int
    record_sha256: str
    quarter: int
    section_path: str
    section_index: int
    picture_target_id: str
    picture_index: int
    picture_xml_id: Optional[str]
    table_id: str
    table_index: int
    cell_target_id: str
    cell_row: int
    cell_column: int
    page_hint: int
    page_hint_source: str
    photo_page_number: int
    inspection_page_number: int
    sample_layout_binding_schema: str
    sample_layout_binding_revision: int
    sample_layout_binding_sha256: str
    semantic_projection_revision: str
    semantic_attestation_sha256: str
    photo_slot_id: str
    sample_evidence_group_id: str
    group_photo_index: int
    group_photo_count: int
    duplicate_image_occurrence_count: int
    duplicate_image_review_required: bool
    duplicate_image_review_confirmed: bool
    duplicate_image_case_refs: Tuple[str, ...]
    square_markers_json: str
    semantic_cells_json: str
    weak_candidates_json: str
    issue_type: str
    maintenance_action: str
    caption: Optional[str]
    route: Optional[str]
    location: Optional[str]
    evaluation: Optional[str]
    opinion: Optional[str]
    source_address_sha256: str

    def canonical_value(self) -> Dict[str, Any]:
        payload = {
            "approval_id": self.approval_id,
            "approval_revision": self.approval_revision,
            "caption": self.caption,
            "case_id": self.case_id,
            "cell_column": self.cell_column,
            "cell_row": self.cell_row,
            "cell_target_id": self.cell_target_id,
            "document_sha256": self.document_sha256,
            "document_name": self.document_name,
            "duplicate_image_case_refs": list(
                self.duplicate_image_case_refs
            ),
            "duplicate_image_occurrence_count": (
                self.duplicate_image_occurrence_count
            ),
            "duplicate_image_review_confirmed": (
                self.duplicate_image_review_confirmed
            ),
            "duplicate_image_review_required": (
                self.duplicate_image_review_required
            ),
            "evaluation_marker_target_ids": [
                marker["cell_target_id"]
                for marker in json.loads(self.square_markers_json)
            ],
            "group_photo_count": self.group_photo_count,
            "group_photo_index": self.group_photo_index,
            "evaluation": self.evaluation,
            "image_height": self.image_height,
            "image_media_type": self.image_media_type,
            "image_sha256": self.image_sha256,
            "image_width": self.image_width,
            "index_manifest_sha256": self.index_manifest_sha256,
            "issue_type": self.issue_type,
            "inspection_page_number": self.inspection_page_number,
            "location": self.location,
            "maintenance_action": self.maintenance_action,
            "opinion": self.opinion,
            "page_hint": self.page_hint,
            "page_hint_source": self.page_hint_source,
            "photo_page_number": self.photo_page_number,
            "photo_slot_id": self.photo_slot_id,
            "picture_index": self.picture_index,
            "picture_target_id": self.picture_target_id,
            "picture_xml_id": self.picture_xml_id,
            "quarter": self.quarter,
            "record_sha256": self.record_sha256,
            "route": self.route,
            "sample_evidence_group_id": (
                self.sample_evidence_group_id
            ),
            "sample_layout_binding_revision": (
                self.sample_layout_binding_revision
            ),
            "sample_layout_binding_schema": (
                self.sample_layout_binding_schema
            ),
            "sample_layout_binding_sha256": (
                self.sample_layout_binding_sha256
            ),
            "schema_version": self.schema_version,
            "semantic_attestation_sha256": (
                self.semantic_attestation_sha256
            ),
            "semantic_cells": json.loads(
                self.semantic_cells_json
            ),
            "semantic_projection_revision": (
                self.semantic_projection_revision
            ),
            "section_index": self.section_index,
            "section_path": self.section_path,
            "source_id": self.source_id,
            "source_address_sha256": self.source_address_sha256,
            "source_kind": "approved_reference",
            "square_marker_target_ids": [
                marker["cell_target_id"]
                for marker in json.loads(self.square_markers_json)
            ],
            "square_markers": json.loads(
                self.square_markers_json
            ),
            "split": "train",
            "structure_target_summary_sha256": (
                self.structure_target_summary_sha256
            ),
            "table_id": self.table_id,
            "table_index": self.table_index,
            "user_confirmed": True,
            "weak_candidates": json.loads(
                self.weak_candidates_json
            ),
        }
        return {
            **payload,
            "approved_reference_member_sha256": hashlib.sha256(
                canonical_json_bytes(payload)
            ).hexdigest(),
        }


@dataclass(frozen=True)
class DatasetSnapshot:
    dataset_id: str
    project_id: str
    dataset_hash: str
    validation_fraction: float
    members: Tuple[DatasetMember, ...]
    approved_reference_members: Tuple[ApprovedReferenceMember, ...]
    manifest_json: str

    @property
    def source_answer_count(self) -> int:
        return len(self.members)

    @property
    def training_member_count(self) -> int:
        return sum(member.include_in_training for member in self.members)

    @property
    def approved_reference_member_count(self) -> int:
        return len(self.approved_reference_members)

    @property
    def approved_reference_training_event_count(self) -> int:
        return len(
            {
                member.sample_evidence_group_id
                for member in self.approved_reference_members
            }
        )

    @property
    def training_gate_member_count(self) -> int:
        # Fixed-validation members measure model quality and must never also
        # satisfy the minimum amount of current-video training evidence.
        return sum(
            member.include_in_training and member.split == "train"
            for member in self.members
        )

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
    record_schema_version = record.get("schema_version")
    if record_schema_version == PROVENANCE_LABEL_RECORD_SCHEMA_VERSION:
        required.update(
            {
                "training_geometry",
                "assist_provenance",
                "provenance_graph",
            }
        )
    elif record_schema_version == LABEL_RECORD_SCHEMA_VERSION:
        required.update({"training_geometry", "assist_provenance"})
    elif record_schema_version == GEOMETRY_LABEL_RECORD_SCHEMA_VERSION:
        required.add("training_geometry")
    elif record_schema_version != LEGACY_LABEL_RECORD_SCHEMA_VERSION:
        raise LabelProjectionError("label record schema is unsupported")
    if set(record) != required:
        raise LabelProjectionError("label record fields are invalid")

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
    training_geometry_json: Optional[str] = None
    training_geometry = (
        record.get("training_geometry")
        if record_schema_version
        in {
            PROVENANCE_LABEL_RECORD_SCHEMA_VERSION,
            LABEL_RECORD_SCHEMA_VERSION,
            GEOMETRY_LABEL_RECORD_SCHEMA_VERSION,
        }
        else None
    )
    if training_geometry is not None:
        try:
            canonical_geometry = canonical_training_geometry(
                training_geometry
            )
        except TrainingGeometryError as error:
            raise LabelProjectionError(
                "training geometry is invalid"
            ) from error
        if canonical_geometry["approval_authority"] != answered_by:
            raise LabelProjectionError(
                "training geometry authority is inconsistent"
            )
        if answer == "no":
            if canonical_geometry["source_kind"] != "negative_evidence":
                raise LabelProjectionError(
                    "negative answer geometry is inconsistent"
                )
        elif answer in {"yes", "edit"}:
            if (
                canonical_geometry["source_kind"] == "negative_evidence"
                or canonical_geometry["normalized_box"]
                != (
                    json.loads(normalized_box_json)
                    if normalized_box_json is not None
                    else None
                )
            ):
                raise LabelProjectionError(
                    "approved answer geometry is inconsistent"
                )
        else:
            raise LabelProjectionError(
                "held or excluded answers cannot carry geometry"
            )
        training_geometry_json = canonical_json_text(canonical_geometry)
    assist_provenance_json: Optional[str] = None
    assist_provenance = (
        record.get("assist_provenance")
        if record_schema_version
        in {
            PROVENANCE_LABEL_RECORD_SCHEMA_VERSION,
            LABEL_RECORD_SCHEMA_VERSION,
        }
        else None
    )
    if assist_provenance is not None:
        try:
            canonical_assist = (
                canonical_structured_assist_provenance(
                    assist_provenance
                )
            )
        except StructuredAssistProvenanceError as error:
            raise LabelProjectionError(
                "structured assist provenance is invalid"
            ) from error
        if (
            canonical_assist["project_id"] != project_id
            or canonical_assist["analysis_job_id"]
            != analysis_job_id
            or canonical_assist["question_id"] != question_id
            or canonical_assist["candidate_id"] != candidate_id
        ):
            raise LabelProjectionError(
                "structured assist provenance identity is inconsistent"
            )
        assist_provenance_json = canonical_json_text(
            canonical_assist
        )
    provenance_graph_json: Optional[str] = None
    provenance_graph: Optional[Dict[str, Any]] = None
    if record_schema_version == PROVENANCE_LABEL_RECORD_SCHEMA_VERSION:
        raw_provenance_graph = record.get("provenance_graph")
        if raw_provenance_graph is not None:
            try:
                provenance_graph = (
                    canonical_annotation_provenance_graph(
                        raw_provenance_graph
                    )
                )
            except TrainingProvenanceError as error:
                raise LabelProjectionError(
                    "annotation training provenance is invalid"
                ) from error
            if (
                provenance_graph["project_id"] != project_id
                or provenance_graph["analysis_job_id"]
                != analysis_job_id
                or provenance_graph["question_id"] != question_id
                or provenance_graph["candidate_id"] != candidate_id
                or provenance_graph["answer_record_id"] != record_id
                or provenance_graph["answer_revision"] != revision
                or provenance_graph["answer_authority"][
                    "answered_by"
                ]
                != answered_by
                or provenance_graph["answer_authority"][
                    "include_in_training"
                ]
                != include_in_training
            ):
                raise LabelProjectionError(
                    "annotation training provenance crossed a label boundary"
                )
            provenance_graph_json = canonical_json_text(
                provenance_graph
            )
    label_source = LABEL_SOURCE_BY_ANSWERED_BY.get(answered_by)
    if label_source is None:
        raise LabelProjectionError("answered_by is unsupported")
    user_confirmed = answered_by == "local_user"
    source_kind = (
        "current_video_confirmed"
        if user_confirmed
        else "structured_llm_assisted"
    )
    if user_confirmed:
        if assist_provenance is not None:
            raise LabelProjectionError(
                "human labels cannot carry structured assist provenance"
            )
        training_authorized = (
            provenance_graph is not None
            if record_schema_version
            == PROVENANCE_LABEL_RECORD_SCHEMA_VERSION
            else True
        )
    else:
        # Legacy structured answers remain auditable, but they are never
        # eligible for DatasetVersion membership until explicitly re-granted
        # under the v1.2 authority and execution lineage contract.
        training_authorized = assist_provenance is not None
        if (
            record_schema_version
            == PROVENANCE_LABEL_RECORD_SCHEMA_VERSION
        ):
            training_authorized = (
                training_authorized
                and provenance_graph is not None
            )
        if (
            record_schema_version
            in {
                PROVENANCE_LABEL_RECORD_SCHEMA_VERSION,
                LABEL_RECORD_SCHEMA_VERSION,
            }
            and assist_provenance is None
        ):
            raise LabelProjectionError(
                "structured labels require assist provenance"
            )
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
    if (
        provenance_graph is not None
        and provenance_graph["candidate_metadata_sha256"]
        != hashlib.sha256(
            canonical_json_bytes(
                {
                    key: item
                    for key, item in candidate.items()
                    if key != "review_event_binding"
                }
            )
        ).hexdigest()
    ):
        raise LabelProjectionError(
            "candidate metadata lineage digest does not match"
        )

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
        "answered_by": answered_by,
        "answer_record_id": record_id,
        "answer_revision": revision,
        "candidate_id": candidate_id,
        "include_in_training": include_in_training,
        "label_source": label_source,
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
        "source_kind": source_kind,
        "source_record_hash": record_hash,
        "training_authorized": training_authorized,
        "training_geometry": (
            json.loads(training_geometry_json)
            if training_geometry_json is not None
            else None
        ),
        "assist_provenance": (
            json.loads(assist_provenance_json)
            if assist_provenance_json is not None
            else None
        ),
        "user_confirmed": user_confirmed,
    }
    if (
        record_schema_version
        == PROVENANCE_LABEL_RECORD_SCHEMA_VERSION
    ):
        annotation_value["provenance_graph"] = (
            json.loads(provenance_graph_json)
            if provenance_graph_json is not None
            else None
        )
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
        training_geometry_json=training_geometry_json,
        assist_provenance_json=assist_provenance_json,
        provenance_graph_json=provenance_graph_json,
        answered_by=answered_by,
        label_source=label_source,
        source_kind=source_kind,
        user_confirmed=user_confirmed,
        training_authorized=training_authorized,
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


def _bounded_reference_text(
    value: object,
    *,
    maximum: int,
    required: bool = False,
) -> Optional[str]:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or (required and (not value or value != value.strip()))
        or len(value) > maximum
    ):
        raise DatasetVersionError(
            "approved reference text snapshot is invalid"
        )
    return value


def _reference_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
    field: str,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise DatasetVersionError(
            f"approved reference {field} is invalid"
        )
    return value


def _reference_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DatasetVersionError(
            f"approved reference {field} is invalid"
        )
    return value


def _reference_semantic_cells(value: object) -> list[Dict[str, Any]]:
    expected_keys = {
        "role",
        "cell_target_id",
        "table_id",
        "table_index",
        "page_number",
        "row",
        "column",
        "row_span",
        "column_span",
        "text_excerpt",
        "text_sha256",
    }
    expected_roles = {
        "photo_caption",
        "inspection_route",
        "inspection_opinion_label",
        "inspection_opinion_body",
    }
    if (
        not isinstance(value, list)
        or len(value) != len(expected_roles)
        or any(
            not isinstance(cell, Mapping)
            or set(cell) != expected_keys
            for cell in value
        )
    ):
        raise DatasetVersionError(
            "approved reference semantic cell addresses are invalid"
        )
    cells: list[Dict[str, Any]] = []
    for raw in value:
        role = raw.get("role")
        cell_target_id = raw.get("cell_target_id")
        table_id = raw.get("table_id")
        if (
            role not in expected_roles
            or not isinstance(cell_target_id, str)
            or _CELL_TARGET_ID.fullmatch(cell_target_id) is None
            or not isinstance(table_id, str)
            or not table_id
            or len(table_id) > 120
        ):
            raise DatasetVersionError(
                "approved reference semantic cell address is invalid"
            )
        cells.append(
            {
                "role": role,
                "cell_target_id": cell_target_id,
                "table_id": table_id,
                "table_index": _reference_int(
                    raw.get("table_index"),
                    minimum=0,
                    maximum=100_000,
                    field="semantic cell table_index",
                ),
                "page_number": _reference_int(
                    raw.get("page_number"),
                    minimum=1,
                    maximum=48,
                    field="semantic cell page_number",
                ),
                "row": _reference_int(
                    raw.get("row"),
                    minimum=0,
                    maximum=100_000,
                    field="semantic cell row",
                ),
                "column": _reference_int(
                    raw.get("column"),
                    minimum=0,
                    maximum=100_000,
                    field="semantic cell column",
                ),
                "row_span": _reference_int(
                    raw.get("row_span"),
                    minimum=1,
                    maximum=100_000,
                    field="semantic cell row_span",
                ),
                "column_span": _reference_int(
                    raw.get("column_span"),
                    minimum=1,
                    maximum=100_000,
                    field="semantic cell column_span",
                ),
                "text_excerpt": _bounded_reference_text(
                    raw.get("text_excerpt"),
                    maximum=320,
                ),
                "text_sha256": _reference_sha256(
                    raw.get("text_sha256"),
                    "semantic cell text_sha256",
                ),
            }
        )
    if {cell["role"] for cell in cells} != expected_roles:
        raise DatasetVersionError(
            "approved reference semantic cell roles are invalid"
        )
    return sorted(cells, key=lambda item: item["role"])


def _reference_square_markers(value: object) -> list[Dict[str, Any]]:
    expected_keys = {
        "cell_target_id",
        "table_id",
        "table_index",
        "page_number",
        "row",
        "column",
        "marker",
        "text_sha256",
    }
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 64
        or any(
            not isinstance(marker, Mapping)
            or set(marker) != expected_keys
            for marker in value
        )
    ):
        raise DatasetVersionError(
            "approved reference square-marker addresses are invalid"
        )
    markers: list[Dict[str, Any]] = []
    seen = set()
    for raw in value:
        cell_target_id = raw.get("cell_target_id")
        table_id = raw.get("table_id")
        if (
            not isinstance(cell_target_id, str)
            or _CELL_TARGET_ID.fullmatch(cell_target_id) is None
            or not isinstance(table_id, str)
            or not table_id
            or len(table_id) > 120
            or raw.get("marker") != "■"
        ):
            raise DatasetVersionError(
                "approved reference square-marker address is invalid"
            )
        marker = {
            "cell_target_id": cell_target_id,
            "table_id": table_id,
            "table_index": _reference_int(
                raw.get("table_index"),
                minimum=0,
                maximum=100_000,
                field="square marker table_index",
            ),
            "page_number": _reference_int(
                raw.get("page_number"),
                minimum=1,
                maximum=48,
                field="square marker page_number",
            ),
            "row": _reference_int(
                raw.get("row"),
                minimum=0,
                maximum=100_000,
                field="square marker row",
            ),
            "column": _reference_int(
                raw.get("column"),
                minimum=0,
                maximum=100_000,
                field="square marker column",
            ),
            "marker": "■",
            "text_sha256": _reference_sha256(
                raw.get("text_sha256"),
                "square marker text_sha256",
            ),
        }
        identity = (
            marker["cell_target_id"],
            marker["table_id"],
            marker["table_index"],
            marker["page_number"],
            marker["row"],
            marker["column"],
        )
        if identity in seen:
            raise DatasetVersionError(
                "approved reference square-marker address is duplicated"
            )
        seen.add(identity)
        markers.append(marker)
    return sorted(
        markers,
        key=lambda item: (
            item["page_number"],
            item["table_index"],
            item["row"],
            item["column"],
            item["cell_target_id"],
        ),
    )


def _approved_reference_source_address(
    record: Mapping[str, Any],
    *,
    semantic_cells: Sequence[Mapping[str, Any]],
    square_markers: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    return {
        "case_id": record["case_id"],
        "cell_column": record["cell_column"],
        "cell_row": record["cell_row"],
        "cell_target_id": record["cell_target_id"],
        "document_sha256": record["document_sha256"],
        "image_sha256": record["image_sha256"],
        "index_manifest_sha256": record["index_manifest_sha256"],
        "inspection_page_number": record[
            "inspection_page_number"
        ],
        "page_hint": record["page_hint"],
        "page_hint_source": record["page_hint_source"],
        "photo_page_number": record["photo_page_number"],
        "photo_slot_id": record["photo_slot_id"],
        "picture_index": record["picture_index"],
        "picture_target_id": record["picture_target_id"],
        "picture_xml_id": record.get("picture_xml_id"),
        "quarter": record["quarter"],
        "sample_evidence_group_id": record[
            "sample_evidence_group_id"
        ],
        "sample_layout_binding_revision": record[
            "sample_layout_binding_revision"
        ],
        "sample_layout_binding_schema": record[
            "sample_layout_binding_schema"
        ],
        "sample_layout_binding_sha256": record[
            "sample_layout_binding_sha256"
        ],
        "section_index": record["section_index"],
        "section_path": record["section_path"],
        "semantic_cells": list(semantic_cells),
        "semantic_projection_revision": record[
            "semantic_projection_revision"
        ],
        "source_id": record["source_id"],
        "square_markers": list(square_markers),
        "structure_target_summary_sha256": record[
            "structure_target_summary_sha256"
        ],
        "table_id": record["table_id"],
        "table_index": record["table_index"],
    }


def canonical_approved_reference_member_value(
    member: Mapping[str, Any],
) -> Dict[str, Any]:
    """Verify one frozen historical seed without consulting mutable stores."""

    expected_fields = {
        "approval_id",
        "approval_revision",
        "approved_reference_member_sha256",
        "caption",
        "case_id",
        "cell_column",
        "cell_row",
        "cell_target_id",
        "document_name",
        "document_sha256",
        "duplicate_image_case_refs",
        "duplicate_image_occurrence_count",
        "duplicate_image_review_confirmed",
        "duplicate_image_review_required",
        "evaluation_marker_target_ids",
        "evaluation",
        "group_photo_count",
        "group_photo_index",
        "image_height",
        "image_media_type",
        "image_sha256",
        "image_width",
        "index_manifest_sha256",
        "inspection_page_number",
        "issue_type",
        "location",
        "maintenance_action",
        "opinion",
        "page_hint",
        "page_hint_source",
        "photo_page_number",
        "photo_slot_id",
        "picture_index",
        "picture_target_id",
        "picture_xml_id",
        "quarter",
        "record_sha256",
        "route",
        "sample_evidence_group_id",
        "sample_layout_binding_revision",
        "sample_layout_binding_schema",
        "sample_layout_binding_sha256",
        "schema_version",
        "section_index",
        "section_path",
        "semantic_attestation_sha256",
        "semantic_cells",
        "semantic_projection_revision",
        "source_address_sha256",
        "source_id",
        "source_kind",
        "split",
        "square_marker_target_ids",
        "square_markers",
        "structure_target_summary_sha256",
        "table_id",
        "table_index",
        "user_confirmed",
        "weak_candidates",
    }
    if not isinstance(member, Mapping) or set(member) != expected_fields:
        raise DatasetVersionError(
            "approved reference member fields are invalid"
        )
    approval_id = member.get("approval_id")
    source_id = member.get("source_id")
    case_id = member.get("case_id")
    photo_slot_id = member.get("photo_slot_id")
    group_id = member.get("sample_evidence_group_id")
    picture_target_id = member.get("picture_target_id")
    cell_target_id = member.get("cell_target_id")
    section_path = member.get("section_path")
    if (
        member.get("schema_version") != _APPROVED_REFERENCE_SCHEMA
        or member.get("sample_layout_binding_schema")
        != _SAMPLE_LAYOUT_BINDING_SCHEMA
        or member.get("sample_layout_binding_revision")
        != _SAMPLE_LAYOUT_BINDING_REVISION
        or member.get("semantic_projection_revision")
        != _SEMANTIC_PROJECTION_REVISION
        or member.get("source_kind") != "approved_reference"
        or member.get("split") != "train"
        or member.get("user_confirmed") is not True
        or not isinstance(approval_id, str)
        or _APPROVAL_ID.fullmatch(approval_id) is None
        or not isinstance(source_id, str)
        or _SAMPLE_SOURCE_ID.fullmatch(source_id) is None
        or not isinstance(case_id, str)
        or _SAMPLE_CASE_ID.fullmatch(case_id) is None
        or not isinstance(photo_slot_id, str)
        or _PHOTO_SLOT_ID.fullmatch(photo_slot_id) is None
        or not isinstance(group_id, str)
        or _SAMPLE_EVIDENCE_GROUP_ID.fullmatch(group_id) is None
        or not isinstance(picture_target_id, str)
        or _PICTURE_TARGET_ID.fullmatch(picture_target_id) is None
        or not isinstance(cell_target_id, str)
        or _CELL_TARGET_ID.fullmatch(cell_target_id) is None
        or not isinstance(section_path, str)
        or _SECTION_PATH.fullmatch(section_path) is None
    ):
        raise DatasetVersionError(
            "approved reference member identity is invalid"
        )
    for field, minimum, maximum in (
        ("approval_revision", 1, 1_000_000_000),
        ("cell_column", 0, 100_000),
        ("cell_row", 0, 100_000),
        ("group_photo_count", 1, 10_000),
        ("group_photo_index", 1, 10_000),
        ("image_height", 1, 65_535),
        ("image_width", 1, 65_535),
        ("inspection_page_number", 1, 47),
        ("page_hint", 1, 10_000),
        ("photo_page_number", 2, 48),
        ("picture_index", 0, 100_000),
        ("quarter", 3, 4),
        ("section_index", 0, 100_000),
        ("table_index", 0, 100_000),
    ):
        _reference_int(
            member.get(field),
            minimum=minimum,
            maximum=maximum,
            field=field,
        )
    if (
        member["quarter"] not in {3, 4}
        or member["photo_page_number"] % 2 != 0
        or member["inspection_page_number"]
        != member["photo_page_number"] - 1
        or member["group_photo_index"]
        > member["group_photo_count"]
        or member.get("image_media_type")
        not in {
            "image/bmp",
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        }
    ):
        raise DatasetVersionError(
            "approved reference member source snapshot is invalid"
        )
    for field in (
        "document_sha256",
        "image_sha256",
        "index_manifest_sha256",
        "record_sha256",
        "sample_layout_binding_sha256",
        "semantic_attestation_sha256",
        "source_address_sha256",
        "structure_target_summary_sha256",
        "approved_reference_member_sha256",
    ):
        _reference_sha256(member.get(field), field)
    for field, maximum, required in (
        ("document_name", 120, True),
        ("issue_type", 128, True),
        ("maintenance_action", _MAX_TEXT, True),
        ("page_hint_source", 120, True),
        ("table_id", 120, True),
        ("caption", 320, False),
        ("route", 320, False),
        ("location", 320, False),
        ("evaluation", 320, False),
        ("opinion", 320, False),
    ):
        _bounded_reference_text(
            member.get(field),
            maximum=maximum,
            required=required,
        )
    picture_xml_id = member.get("picture_xml_id")
    if (
        picture_xml_id is not None
        and (
            not isinstance(picture_xml_id, str)
            or len(picture_xml_id) > 256
        )
    ):
        raise DatasetVersionError(
            "approved reference picture_xml_id is invalid"
        )
    duplicate_refs = member.get("duplicate_image_case_refs")
    duplicate_count = member.get(
        "duplicate_image_occurrence_count"
    )
    duplicate_required = member.get(
        "duplicate_image_review_required"
    )
    duplicate_confirmed = member.get(
        "duplicate_image_review_confirmed"
    )
    if (
        isinstance(duplicate_count, bool)
        or not isinstance(duplicate_count, int)
        or duplicate_count < 1
        or not isinstance(duplicate_required, bool)
        or not isinstance(duplicate_confirmed, bool)
        or not isinstance(duplicate_refs, list)
        or len(duplicate_refs) != duplicate_count
        or duplicate_refs != sorted(set(duplicate_refs))
        or f"{source_id}/{case_id}" not in duplicate_refs
        or duplicate_required != (duplicate_count > 1)
        or (duplicate_required and not duplicate_confirmed)
        or any(
            not isinstance(value, str)
            or _DUPLICATE_IMAGE_CASE_REF.fullmatch(value) is None
            for value in duplicate_refs
        )
    ):
        raise DatasetVersionError(
            "approved reference member duplicate-image review is invalid"
        )
    semantic_cells = _reference_semantic_cells(
        member.get("semantic_cells")
    )
    square_markers = _reference_square_markers(
        member.get("square_markers")
    )
    if (
        member["semantic_cells"] != semantic_cells
        or member["square_markers"] != square_markers
        or member.get("square_marker_target_ids")
        != [
            marker["cell_target_id"]
            for marker in square_markers
        ]
        or member.get("evaluation_marker_target_ids")
        != [
            marker["cell_target_id"]
            for marker in square_markers
        ]
    ):
        raise DatasetVersionError(
            "approved reference member address order is not canonical"
        )
    weak_candidates = member.get("weak_candidates")
    if (
        not isinstance(weak_candidates, Mapping)
        or set(weak_candidates)
        != {
            "caption",
            "route",
            "location",
            "evaluation",
            "opinion",
        }
        or any(
            _bounded_reference_text(value, maximum=320) != value
            for value in weak_candidates.values()
        )
    ):
        raise DatasetVersionError(
            "approved reference member weak snapshot is invalid"
        )
    source_address = _approved_reference_source_address(
        member,
        semantic_cells=semantic_cells,
        square_markers=square_markers,
    )
    if hashlib.sha256(
        canonical_json_bytes(source_address)
    ).hexdigest() != member["source_address_sha256"]:
        raise DatasetVersionError(
            "approved reference source address hash is invalid"
        )
    payload = dict(member)
    member_sha256 = payload.pop(
        "approved_reference_member_sha256"
    )
    if hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest() != member_sha256:
        raise DatasetVersionError(
            "approved reference member hash is invalid"
        )
    return dict(member)


def _approved_reference_members(
    project_id: str,
    records: Sequence[Mapping[str, Any]],
) -> Tuple[ApprovedReferenceMember, ...]:
    if isinstance(records, (str, bytes, bytearray)) or not isinstance(
        records, Sequence
    ):
        raise DatasetVersionError(
            "approved reference records must be a sequence"
        )
    members = []
    seen_approval_ids = set()
    seen_case_ids = set()
    seen_image_hashes = set()
    seen_group_photo_indexes = set()
    group_contracts: Dict[str, Tuple[int, str, str]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise DatasetVersionError(
                "approved reference record must be an object"
            )
        if (
            record.get("project_id") != project_id
            or record.get("reference_state") != "approved_reference"
            or record.get("approved_reference") is not True
            or record.get("training_eligible") is not True
            or record.get("training_scope")
            != "approved_reference_only"
            or record.get("user_confirmed") is not True
            or record.get("annotation_created") is not False
            or record.get("incident_created") is not False
        ):
            raise DatasetVersionError(
                "approved reference is outside the confirmed training boundary"
            )
        approval_id = record.get("approval_id")
        revision = record.get("revision")
        source_id = record.get("source_id")
        case_id = record.get("case_id")
        schema_version = record.get("schema_version")
        sample_layout_binding_schema = record.get(
            "sample_layout_binding_schema"
        )
        sample_layout_binding_revision = record.get(
            "sample_layout_binding_revision"
        )
        semantic_projection_revision = record.get(
            "semantic_projection_revision"
        )
        photo_slot_id = record.get("photo_slot_id")
        sample_evidence_group_id = record.get(
            "sample_evidence_group_id"
        )
        group_photo_index = record.get("group_photo_index")
        group_photo_count = record.get("group_photo_count")
        duplicate_image_occurrence_count = record.get(
            "duplicate_image_occurrence_count"
        )
        duplicate_image_review_required = record.get(
            "duplicate_image_review_required"
        )
        duplicate_image_review_confirmed = record.get(
            "duplicate_image_review_confirmed"
        )
        duplicate_image_case_refs = record.get(
            "duplicate_image_case_refs"
        )
        issue_type = record.get("issue_type")
        maintenance_action = record.get("maintenance_action")
        document_name = record.get("document_name")
        image_media_type = record.get("image_media_type")
        quarter = record.get("quarter")
        section_path = record.get("section_path")
        picture_target_id = record.get("picture_target_id")
        picture_xml_id = record.get("picture_xml_id")
        table_id = record.get("table_id")
        cell_target_id = record.get("cell_target_id")
        page_hint_source = record.get("page_hint_source")
        if (
            schema_version != _APPROVED_REFERENCE_SCHEMA
            or sample_layout_binding_schema
            != _SAMPLE_LAYOUT_BINDING_SCHEMA
            or sample_layout_binding_revision
            != _SAMPLE_LAYOUT_BINDING_REVISION
            or semantic_projection_revision
            != _SEMANTIC_PROJECTION_REVISION
            or not isinstance(approval_id, str)
            or _APPROVAL_ID.fullmatch(approval_id) is None
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(source_id, str)
            or _SAMPLE_SOURCE_ID.fullmatch(source_id) is None
            or not isinstance(case_id, str)
            or _SAMPLE_CASE_ID.fullmatch(case_id) is None
            or not isinstance(photo_slot_id, str)
            or _PHOTO_SLOT_ID.fullmatch(photo_slot_id) is None
            or not isinstance(sample_evidence_group_id, str)
            or _SAMPLE_EVIDENCE_GROUP_ID.fullmatch(
                sample_evidence_group_id
            )
            is None
            or isinstance(group_photo_index, bool)
            or not isinstance(group_photo_index, int)
            or group_photo_index < 1
            or isinstance(group_photo_count, bool)
            or not isinstance(group_photo_count, int)
            or group_photo_count < 1
            or group_photo_index > group_photo_count
            or isinstance(duplicate_image_occurrence_count, bool)
            or not isinstance(duplicate_image_occurrence_count, int)
            or duplicate_image_occurrence_count < 1
            or not isinstance(duplicate_image_review_required, bool)
            or not isinstance(duplicate_image_review_confirmed, bool)
            or not isinstance(issue_type, str)
            or not issue_type
            or len(issue_type) > 128
            or not isinstance(maintenance_action, str)
            or not maintenance_action
            or maintenance_action != maintenance_action.strip()
            or len(maintenance_action) > _MAX_TEXT
            or not isinstance(document_name, str)
            or not document_name
            or len(document_name) > 120
            or image_media_type
            not in {
                "image/bmp",
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
            }
            or quarter not in {3, 4}
            or not isinstance(section_path, str)
            or _SECTION_PATH.fullmatch(section_path) is None
            or not isinstance(picture_target_id, str)
            or _PICTURE_TARGET_ID.fullmatch(
                picture_target_id
            )
            is None
            or (
                picture_xml_id is not None
                and (
                    not isinstance(picture_xml_id, str)
                    or len(picture_xml_id) > 256
                )
            )
            or not isinstance(table_id, str)
            or not table_id
            or len(table_id) > 120
            or not isinstance(cell_target_id, str)
            or _CELL_TARGET_ID.fullmatch(cell_target_id) is None
            or not isinstance(page_hint_source, str)
            or not page_hint_source
            or len(page_hint_source) > 120
        ):
            raise DatasetVersionError(
                "approved reference identity or training label is invalid"
            )
        section_index = _reference_int(
            record.get("section_index"),
            minimum=0,
            maximum=100_000,
            field="section_index",
        )
        picture_index = _reference_int(
            record.get("picture_index"),
            minimum=0,
            maximum=100_000,
            field="picture_index",
        )
        table_index = _reference_int(
            record.get("table_index"),
            minimum=0,
            maximum=100_000,
            field="table_index",
        )
        cell_row = _reference_int(
            record.get("cell_row"),
            minimum=0,
            maximum=100_000,
            field="cell_row",
        )
        cell_column = _reference_int(
            record.get("cell_column"),
            minimum=0,
            maximum=100_000,
            field="cell_column",
        )
        page_hint = _reference_int(
            record.get("page_hint"),
            minimum=1,
            maximum=10_000,
            field="page_hint",
        )
        photo_page_number = _reference_int(
            record.get("photo_page_number"),
            minimum=2,
            maximum=48,
            field="photo_page_number",
        )
        inspection_page_number = _reference_int(
            record.get("inspection_page_number"),
            minimum=1,
            maximum=47,
            field="inspection_page_number",
        )
        image_width = _reference_int(
            record.get("image_width"),
            minimum=1,
            maximum=65_535,
            field="image_width",
        )
        image_height = _reference_int(
            record.get("image_height"),
            minimum=1,
            maximum=65_535,
            field="image_height",
        )
        if (
            photo_page_number % 2 != 0
            or inspection_page_number != photo_page_number - 1
        ):
            raise DatasetVersionError(
                "approved reference page-pair address is invalid"
            )
        semantic_cells = _reference_semantic_cells(
            record.get("semantic_cells")
        )
        square_markers = _reference_square_markers(
            record.get("square_markers")
        )
        weak_candidates = record.get("weak_candidates")
        if (
            not isinstance(weak_candidates, Mapping)
            or set(weak_candidates)
            != {
                "caption",
                "route",
                "location",
                "evaluation",
                "opinion",
            }
        ):
            raise DatasetVersionError(
                "approved reference weak source snapshot is invalid"
            )
        canonical_weak_candidates = {
            field: _bounded_reference_text(
                weak_candidates.get(field),
                maximum=320,
            )
            for field in (
                "caption",
                "route",
                "location",
                "evaluation",
                "opinion",
            )
        }
        selected_text = {
            field: _bounded_reference_text(
                record.get(field),
                maximum=320,
            )
            for field in (
                "caption",
                "route",
                "location",
                "evaluation",
                "opinion",
            )
        }
        if (
            not isinstance(duplicate_image_case_refs, list)
            or len(duplicate_image_case_refs)
            != duplicate_image_occurrence_count
            or duplicate_image_case_refs
            != sorted(set(duplicate_image_case_refs))
            or any(
                not isinstance(value, str)
                or _DUPLICATE_IMAGE_CASE_REF.fullmatch(value) is None
                for value in duplicate_image_case_refs
            )
            or f"{source_id}/{case_id}"
            not in duplicate_image_case_refs
            or duplicate_image_review_required
            != (duplicate_image_occurrence_count > 1)
            or (
                duplicate_image_review_required
                and not duplicate_image_review_confirmed
            )
        ):
            raise DatasetVersionError(
                "approved reference duplicate-image review is invalid"
            )
        hashes = {}
        for field in (
            "document_sha256",
            "structure_target_summary_sha256",
            "index_manifest_sha256",
            "image_sha256",
            "sample_layout_binding_sha256",
            "semantic_attestation_sha256",
            "record_sha256",
        ):
            value = record.get(field)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise DatasetVersionError(
                    f"approved reference {field} is invalid"
                )
            hashes[field] = value
        record_without_hash = dict(record)
        record_without_hash.pop("record_sha256", None)
        if hashlib.sha256(
            canonical_json_bytes(record_without_hash)
        ).hexdigest() != hashes["record_sha256"]:
            raise DatasetVersionError(
                "approved reference record hash is invalid"
            )
        source_address = _approved_reference_source_address(
            record,
            semantic_cells=semantic_cells,
            square_markers=square_markers,
        )
        source_address_sha256 = hashlib.sha256(
            canonical_json_bytes(source_address)
        ).hexdigest()
        if (
            approval_id in seen_approval_ids
            or (source_id, case_id) in seen_case_ids
            or hashes["image_sha256"] in seen_image_hashes
            or (
                sample_evidence_group_id,
                group_photo_index,
            )
            in seen_group_photo_indexes
        ):
            raise DatasetVersionError(
                "approved reference snapshot contains duplicate evidence"
            )
        group_contract = (
            group_photo_count,
            issue_type,
            maintenance_action,
        )
        previous_group_contract = group_contracts.setdefault(
            sample_evidence_group_id,
            group_contract,
        )
        if previous_group_contract != group_contract:
            raise DatasetVersionError(
                "approved reference evidence group is inconsistent"
            )
        seen_approval_ids.add(approval_id)
        seen_case_ids.add((source_id, case_id))
        seen_image_hashes.add(hashes["image_sha256"])
        seen_group_photo_indexes.add(
            (sample_evidence_group_id, group_photo_index)
        )
        members.append(
            ApprovedReferenceMember(
                schema_version=schema_version,
                approval_id=approval_id,
                approval_revision=revision,
                source_id=source_id,
                case_id=case_id,
                document_name=document_name,
                document_sha256=hashes["document_sha256"],
                structure_target_summary_sha256=hashes[
                    "structure_target_summary_sha256"
                ],
                index_manifest_sha256=hashes[
                    "index_manifest_sha256"
                ],
                image_sha256=hashes["image_sha256"],
                image_media_type=image_media_type,
                image_width=image_width,
                image_height=image_height,
                record_sha256=hashes["record_sha256"],
                quarter=quarter,
                section_path=section_path,
                section_index=section_index,
                picture_target_id=picture_target_id,
                picture_index=picture_index,
                picture_xml_id=picture_xml_id,
                table_id=table_id,
                table_index=table_index,
                cell_target_id=cell_target_id,
                cell_row=cell_row,
                cell_column=cell_column,
                page_hint=page_hint,
                page_hint_source=page_hint_source,
                photo_page_number=photo_page_number,
                inspection_page_number=inspection_page_number,
                sample_layout_binding_schema=(
                    sample_layout_binding_schema
                ),
                sample_layout_binding_revision=(
                    sample_layout_binding_revision
                ),
                sample_layout_binding_sha256=hashes[
                    "sample_layout_binding_sha256"
                ],
                semantic_projection_revision=(
                    semantic_projection_revision
                ),
                semantic_attestation_sha256=hashes[
                    "semantic_attestation_sha256"
                ],
                photo_slot_id=photo_slot_id,
                sample_evidence_group_id=(
                    sample_evidence_group_id
                ),
                group_photo_index=group_photo_index,
                group_photo_count=group_photo_count,
                duplicate_image_occurrence_count=(
                    duplicate_image_occurrence_count
                ),
                duplicate_image_review_required=(
                    duplicate_image_review_required
                ),
                duplicate_image_review_confirmed=(
                    duplicate_image_review_confirmed
                ),
                duplicate_image_case_refs=tuple(
                    duplicate_image_case_refs
                ),
                square_markers_json=canonical_json_text(
                    square_markers
                ),
                semantic_cells_json=canonical_json_text(
                    semantic_cells
                ),
                weak_candidates_json=canonical_json_text(
                    canonical_weak_candidates
                ),
                issue_type=issue_type,
                maintenance_action=maintenance_action,
                caption=selected_text["caption"],
                route=selected_text["route"],
                location=selected_text["location"],
                evaluation=selected_text["evaluation"],
                opinion=selected_text["opinion"],
                source_address_sha256=source_address_sha256,
            )
        )
    return tuple(
        sorted(
            members,
            key=lambda item: (
                item.approval_id,
                item.approval_revision,
                item.source_id,
                item.case_id,
            ),
        )
    )


def freeze_approved_reference_records(
    project_id: UUID | str,
    records: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    """Return the exact immutable auxiliary-seed members for one project."""

    project_text = str(project_id)
    try:
        canonical_project = str(UUID(project_text))
    except (TypeError, ValueError, AttributeError) as error:
        raise DatasetVersionError(
            "approved reference project_id must be a UUID"
        ) from error
    if canonical_project != project_text:
        raise DatasetVersionError(
            "approved reference project_id must be canonical"
        )
    return [
        member.canonical_value()
        for member in _approved_reference_members(
            project_text,
            records,
        )
    ]


def build_dataset_snapshot(
    project_id: UUID | str,
    current_rows: Sequence[Mapping[str, Any]],
    *,
    validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
    approved_reference_records: Sequence[Mapping[str, Any]] = (),
    fixed_validation_analysis_job_ids: Optional[
        Sequence[UUID | str]
    ] = None,
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
    reference_members = _approved_reference_members(
        project_text,
        approved_reference_records,
    )

    normalized_rows = []
    seen_questions = set()
    provenance_aware = any(
        isinstance(row, Mapping)
        and "provenance_graph" in row
        and row.get("include_in_training") is True
        for row in current_rows
    )
    for row in current_rows:
        if row.get("project_id") != project_text:
            raise DatasetVersionError("dataset rows crossed the project boundary")
        question_id = str(row.get("question_id", ""))
        if question_id in seen_questions:
            raise DatasetVersionError("current labels contain duplicate questions")
        seen_questions.add(question_id)
        include = row.get("include_in_training")
        partition = row.get("partition")
        label_source = row.get("label_source", "local_user_review")
        if not isinstance(include, bool):
            raise DatasetVersionError("dataset row training inclusion is invalid")
        if include != (partition in {"approved", "negative"}):
            raise DatasetVersionError("dataset row partition is inconsistent")
        if label_source not in {
            "local_user_review",
            "structured_llm_assisted",
        }:
            raise DatasetVersionError("dataset row label source is invalid")
        training_authorized = row.get(
            "training_authorized",
            label_source == "local_user_review",
        )
        if not isinstance(training_authorized, bool):
            raise DatasetVersionError(
                "dataset row training authorization is invalid"
            )
        if include and training_authorized is not True:
            raise DatasetVersionError(
                "dataset row is not authorized for training"
            )
        if provenance_aware and include:
            if (
                "provenance_graph" not in row
                or row.get("provenance_graph") is None
            ):
                raise DatasetVersionError(
                    "dataset row is missing immutable training provenance"
                )
            try:
                provenance_graph = (
                    canonical_annotation_provenance_graph(
                        row["provenance_graph"]
                    )
                )
            except TrainingProvenanceError as error:
                raise DatasetVersionError(
                    "dataset row training provenance is invalid"
                ) from error
            if (
                provenance_graph["project_id"] != project_text
                or provenance_graph["analysis_job_id"]
                != row.get("analysis_job_id")
                or provenance_graph["question_id"]
                != row.get("question_id")
                or provenance_graph["candidate_id"]
                != row.get("candidate_id")
                or provenance_graph["answer_record_id"]
                != row.get("answer_record_id")
                or provenance_graph["answer_revision"]
                != row.get("answer_revision")
                or provenance_graph["answer_authority"][
                    "include_in_training"
                ]
                is not True
            ):
                raise DatasetVersionError(
                    "dataset row provenance crossed an annotation boundary"
                )
        normalized_rows.append(dict(row))

    trainable_jobs = sorted(
        {
            str(row["analysis_job_id"])
            for row in normalized_rows
            if row["include_in_training"]
        }
    )
    structured_llm_jobs = {
        str(row["analysis_job_id"])
        for row in normalized_rows
        if (
            row["include_in_training"]
            and row.get("label_source", "local_user_review")
            == "structured_llm_assisted"
        )
    }
    eligible_validation_jobs = [
        job_id
        for job_id in trainable_jobs
        if job_id not in structured_llm_jobs
    ]
    if fixed_validation_analysis_job_ids is None:
        split_strategy = SPLIT_STRATEGY
        ordered_jobs = sorted(
            eligible_validation_jobs,
            key=lambda job_id: (
                hashlib.sha256(
                    (
                        f"{DATASET_SCHEMA_VERSION}:"
                        f"{project_text}:{job_id}"
                    ).encode("utf-8")
                ).hexdigest(),
                job_id,
            ),
        )
        validation_jobs = set()
        if len(ordered_jobs) >= 2:
            validation_count = round(len(ordered_jobs) * fraction)
            validation_count = max(
                1,
                min(len(ordered_jobs) - 1, validation_count),
            )
            validation_jobs.update(
                ordered_jobs[:validation_count]
            )
    else:
        if (
            isinstance(
                fixed_validation_analysis_job_ids,
                (str, bytes, bytearray),
            )
            or not isinstance(
                fixed_validation_analysis_job_ids, Sequence
            )
        ):
            raise DatasetVersionError(
                "fixed validation analysis jobs must be a sequence"
            )
        fixed_jobs = []
        for value in fixed_validation_analysis_job_ids:
            try:
                canonical = str(UUID(str(value)))
            except (TypeError, ValueError, AttributeError) as error:
                raise DatasetVersionError(
                    "fixed validation analysis job id is invalid"
                ) from error
            if canonical != str(value):
                raise DatasetVersionError(
                    "fixed validation analysis job id is not canonical"
                )
            fixed_jobs.append(canonical)
        if (
            not fixed_jobs
            or len(fixed_jobs) != len(set(fixed_jobs))
            or fixed_jobs != sorted(fixed_jobs)
        ):
            raise DatasetVersionError(
                "fixed validation analysis jobs are invalid"
            )
        validation_jobs = set(fixed_jobs)
        missing_jobs = validation_jobs.difference(
            eligible_validation_jobs
        )
        if missing_jobs:
            raise DatasetVersionError(
                "fixed validation evidence is unavailable or changed"
            )
        if validation_jobs == set(trainable_jobs):
            raise DatasetVersionError(
                "fixed validation cannot consume every training job"
            )
        split_strategy = FIXED_VALIDATION_SPLIT_STRATEGY

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
        "approved_reference_members": [
            member.canonical_value() for member in reference_members
        ],
        "members": [member.canonical_value() for member in members],
        "project_id": project_text,
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": split_strategy,
        "validation_fraction": fraction,
    }
    if provenance_aware:
        provenance_members = []
        rows_by_annotation = {
            str(row["annotation_id"]): row
            for row in normalized_rows
        }
        for member in members:
            if not member.include_in_training:
                continue
            row = rows_by_annotation.get(member.annotation_id)
            if row is None:
                raise DatasetVersionError(
                    "dataset provenance annotation is unavailable"
                )
            provenance_members.append(
                {
                    **member.canonical_value(),
                    "provenance_graph": row.get(
                        "provenance_graph"
                    ),
                }
            )
        try:
            fingerprint["provenance_graph"] = (
                build_dataset_provenance_graph(
                    project_id=project_text,
                    members=provenance_members,
                )
            )
        except TrainingProvenanceError as error:
            raise DatasetVersionError(
                "dataset training provenance is invalid"
            ) from error
    dataset_hash = hashlib.sha256(canonical_json_bytes(fingerprint)).hexdigest()
    dataset_id = str(uuid5(project_uuid, f"{DATASET_SCHEMA_VERSION}:{dataset_hash}"))
    manifest = {
        **fingerprint,
        "dataset_hash": dataset_hash,
        "dataset_id": dataset_id,
        "approved_reference_members_sha256": hashlib.sha256(
            canonical_json_bytes(
                [
                    member.canonical_value()
                    for member in reference_members
                ]
            )
        ).hexdigest(),
        "approved_references_count_toward_training_gate": False,
        "approved_references_count_toward_validation": False,
        "counts": {
            "approved_reference_training_events": (
                len(
                    {
                        member.sample_evidence_group_id
                        for member in reference_members
                    }
                )
            ),
            "approved_reference_training_members": len(
                reference_members
            ),
            "excluded": sum(member.split == "excluded" for member in members),
            "source_answers": len(members),
            "train": sum(member.split == "train" for member in members),
            "training_gate_members": (
                sum(
                    member.include_in_training and member.split == "train"
                    for member in members
                )
            ),
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
        approved_reference_members=reference_members,
        manifest_json=canonical_json_text(manifest),
    )


__all__ = [
    "DATASET_SCHEMA_VERSION",
    "DEFAULT_VALIDATION_FRACTION",
    "FIXED_VALIDATION_SPLIT_STRATEGY",
    "ApprovedReferenceMember",
    "DatasetMember",
    "DatasetSnapshot",
    "DatasetVersionError",
    "LabelProjection",
    "LabelProjectionError",
    "PROVENANCE_LABEL_RECORD_SCHEMA_VERSION",
    "SPLIT_STRATEGY",
    "build_dataset_snapshot",
    "canonical_approved_reference_member_value",
    "canonical_json_bytes",
    "canonical_json_text",
    "freeze_approved_reference_records",
    "normalize_label_record",
]
