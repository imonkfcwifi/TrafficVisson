"""Deterministic current-Incident binding for the audited sample layout.

This module is the fail-closed boundary between reviewed pipeline truth and a
future exact HWPX writer.  It does not call a Provider, COM, or a document
writer.  The completed 3Q/4Q samples are used only as byte-exact layout
authority and as ``expected_value`` guards for replacement/neutralization.

The binder deliberately produces a server-only manifest.  A future writer
must consume every replacement and neutralization entry, then reopen and
render-validate the output before an apply can be accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, fields
from datetime import date
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from uuid import UUID

from .approved_incident_authority import (
    ApprovedIncidentAuthorityError,
    validate_approved_incident_output_authority,
)
from .hwpx_sample_layout import (
    HwpxSampleLayoutError,
    SampleLayoutInspection,
    bind_sample_reference_case,
)
from .hwpx_stale_factual_targets import (
    HwpxStaleFactualTargetAudit,
    HwpxStaleFactualTargetError,
    audit_stale_factual_targets,
    require_stale_factual_target_coverage,
)

if TYPE_CHECKING:
    from .pipeline_report_preview import SampleTemplateSemanticMap


CURRENT_INCIDENT_REPORT_BINDING_SCHEMA = (
    "trafficvisson.current-incident-report-binding.v1"
)
DEIDENTIFICATION_VERIFICATION_SCHEMA = (
    "trafficvisson.deidentification-verification.v1"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_GRADE_NAMES = (
    "very_good",
    "good",
    "normal",
    "poor",
    "very_poor",
)
_GRADE_LABELS = (
    "매우 우수",
    "우수",
    "보통",
    "미흡",
    "매우 미흡",
)
LEGACY_DOCUMENT_EDIT_PLAN_OPERATION_LIMIT = 100
EXACT_HWPX_WRITER_OPERATION_LIMIT = 5_000


class CurrentIncidentReportBindingError(RuntimeError):
    """A caller-safe, apply-blocking binder failure."""

    provider_called = False
    com_invoked = False
    writer_invoked = False
    apply_blocked = True

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReportTargetMutation:
    role: str
    operation: str
    target_id: str
    page_pair_index: int
    page_number: int
    expected_value: Mapping[str, Any]
    current_value: Mapping[str, Any]
    current_source_refs: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationRowBinding:
    page_pair_index: int
    page_number: int
    row_order: int
    row: int
    row_label_sha256: str
    selected_grade: str
    selected_target_id: str | None
    selected_anchor_column: int | None
    five_grade_grid_available: bool
    grade_targets: tuple[Mapping[str, Any], ...]
    current_source_refs: tuple[str, ...]


@dataclass(frozen=True)
class IncidentSlotBinding:
    incident_id: str
    incident_order: int
    page_pair_index: int
    inspection_page_number: int
    photo_page_number: int
    photo_slot_id: str
    photo_cell_target_id: str
    primary_picture_target_id: str
    additional_picture_target_ids: tuple[str, ...]
    caption_cell_target_id: str
    selected_evidence_sha256: str
    selected_evidence_ref: str
    evidence_policy: str
    approved_incident_authority_sha256: str
    photo_binding_sha256: str


@dataclass(frozen=True)
class CurrentIncidentReportBindingManifest:
    schema_version: str
    state: str
    project_id: str
    pipeline_job_id: str
    analysis_job_id: str
    source_sha256: str
    template_id: str
    document_snapshot_id: str
    template_sha256: str
    template_semantic_map_sha256: str
    structure_target_summary_sha256: str
    incident_capacity: int
    approved_incident_count: int
    approved_incident_ids: tuple[str, ...]
    occupied_page_pair_indices: tuple[int, ...]
    incident_bindings: tuple[IncidentSlotBinding, ...]
    replacements: tuple[ReportTargetMutation, ...]
    neutralizations: tuple[ReportTargetMutation, ...]
    evaluation_rows: tuple[EvaluationRowBinding, ...]
    survey_incident_authority_sha256: str
    approved_incident_authority_set_sha256: str
    input_sha256: str
    content_sha256: str
    slot_manifest_sha256: str
    binding_manifest_sha256: str
    required_writer_operation_count: int
    legacy_document_edit_plan_operation_limit: int
    exact_hwpx_writer_operation_limit: int
    legacy_plan_expansion_required: bool
    writer_batching_required: bool
    approved_incident_bound: bool = True
    content_provenance_bound: bool = True
    writer_required: bool = True
    apply_eligible: bool = False
    provider_called: bool = False
    com_invoked: bool = False
    document_written: bool = False
    weak_reference_approval_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the complete server-only record."""

        return asdict(self)

    def public_metadata(self) -> dict[str, Any]:
        """Return path-free status metadata suitable for renderer exposure."""

        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "template_id": self.template_id,
            "document_snapshot_id": self.document_snapshot_id,
            "template_sha256": self.template_sha256,
            "incident_capacity": self.incident_capacity,
            "approved_incident_count": self.approved_incident_count,
            "survey_incident_authority_sha256": (
                self.survey_incident_authority_sha256
            ),
            "approved_incident_authority_set_sha256": (
                self.approved_incident_authority_set_sha256
            ),
            "input_sha256": self.input_sha256,
            "content_sha256": self.content_sha256,
            "slot_manifest_sha256": self.slot_manifest_sha256,
            "binding_manifest_sha256": self.binding_manifest_sha256,
            "required_writer_operation_count": (
                self.required_writer_operation_count
            ),
            "legacy_document_edit_plan_operation_limit": (
                self.legacy_document_edit_plan_operation_limit
            ),
            "exact_hwpx_writer_operation_limit": (
                self.exact_hwpx_writer_operation_limit
            ),
            "legacy_plan_expansion_required": (
                self.legacy_plan_expansion_required
            ),
            "writer_batching_required": self.writer_batching_required,
            "approved_incident_bound": self.approved_incident_bound,
            "content_provenance_bound": self.content_provenance_bound,
            "writer_required": self.writer_required,
            "apply_eligible": self.apply_eligible,
            "provider_called": self.provider_called,
            "com_invoked": self.com_invoked,
            "document_written": self.document_written,
        }


def current_incident_report_binding_from_dict(
    value: Any,
) -> CurrentIncidentReportBindingManifest:
    """Strictly reconstruct one persisted server-owned binding manifest."""

    payload = _require_dataclass_payload(
        value,
        dataclass_type=CurrentIncidentReportBindingManifest,
        label="binding manifest",
    )
    payload["approved_incident_ids"] = tuple(
        payload["approved_incident_ids"]
    )
    payload["occupied_page_pair_indices"] = tuple(
        payload["occupied_page_pair_indices"]
    )
    payload["incident_bindings"] = tuple(
        IncidentSlotBinding(
            **{
                **_require_dataclass_payload(
                    item,
                    dataclass_type=IncidentSlotBinding,
                    label="incident slot binding",
                ),
                "additional_picture_target_ids": tuple(
                    item["additional_picture_target_ids"]
                ),
            }
        )
        for item in payload["incident_bindings"]
    )
    payload["replacements"] = tuple(
        _report_target_mutation_from_dict(item)
        for item in payload["replacements"]
    )
    payload["neutralizations"] = tuple(
        _report_target_mutation_from_dict(item)
        for item in payload["neutralizations"]
    )
    payload["evaluation_rows"] = tuple(
        EvaluationRowBinding(
            **{
                **_require_dataclass_payload(
                    item,
                    dataclass_type=EvaluationRowBinding,
                    label="evaluation row binding",
                ),
                "grade_targets": tuple(
                    dict(target) for target in item["grade_targets"]
                ),
                "current_source_refs": tuple(
                    item["current_source_refs"]
                ),
            }
        )
        for item in payload["evaluation_rows"]
    )
    return CurrentIncidentReportBindingManifest(**payload)


def _require_dataclass_payload(
    value: Any,
    *,
    dataclass_type: type,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    expected = {field.name for field in fields(dataclass_type)}
    if set(value) != expected:
        raise ValueError(f"{label} fields are invalid")
    return dict(value)


def _report_target_mutation_from_dict(
    value: Any,
) -> ReportTargetMutation:
    payload = _require_dataclass_payload(
        value,
        dataclass_type=ReportTargetMutation,
        label="report target mutation",
    )
    payload["expected_value"] = dict(payload["expected_value"])
    payload["current_value"] = dict(payload["current_value"])
    payload["current_source_refs"] = tuple(
        payload["current_source_refs"]
    )
    return ReportTargetMutation(**payload)


@dataclass(frozen=True)
class _PhotoSlot:
    page_pair_index: int
    inspection_page_number: int
    photo_page_number: int
    photo_slot_id: str
    photo_cell_target_id: str
    photo_cell_row: int
    photo_cell_column: int
    picture_target_ids: tuple[str, ...]
    picture_indexes: tuple[int, ...]
    caption_target_id: str
    caption_expected_text: str
    caption_expected_sha256: str


@dataclass(frozen=True)
class _EvaluationRow:
    row_order: int
    row: int
    label_sha256: str
    five_grade_grid_available: bool
    grade_targets: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _PagePair:
    page_pair_index: int
    inspection_page_number: int
    photo_page_number: int
    route_target_id: str
    route_expected_text: str
    route_expected_sha256: str
    date_target_id: str
    date_expected_text: str
    date_expected_sha256: str
    opinion_target_id: str
    opinion_expected_text: str
    opinion_expected_sha256: str
    old_marker_targets: tuple[Mapping[str, Any], ...]
    evaluation_rows: tuple[_EvaluationRow, ...]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_text(value: Any, label: str, *, maximum: int = 2_000) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or _CONTROL.search(value) is not None
    ):
        raise CurrentIncidentReportBindingError(
            "current_report_content_invalid",
            f"{label} is invalid",
        )
    if (
        re.match(r"^[A-Za-z]:[\\/]", value)
        or value.startswith(("\\\\", "file://", "file:\\\\"))
    ):
        raise CurrentIncidentReportBindingError(
            "current_report_content_invalid",
            f"{label} must not contain a local path",
        )
    return value


def _opaque_ref(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or _IDENTIFIER.fullmatch(value) is None
    ):
        raise CurrentIncidentReportBindingError(
            "incident_evidence_invalid",
            f"{label} must be an opaque server reference",
        )
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise CurrentIncidentReportBindingError(
            "incident_evidence_invalid",
            f"{label} must be a lowercase SHA-256",
        )
    return value


def deidentification_record_sha256(
    *,
    incident_id: str,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
    original_ref: str,
    original_sha256: str,
    deidentified_ref: str,
    deidentified_sha256: str,
) -> str:
    """Hash the exact verified deidentification transition."""

    return _sha256_value(
        {
            "schema_version": DEIDENTIFICATION_VERIFICATION_SCHEMA,
            "incident_id": incident_id,
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "original_ref": original_ref,
            "original_sha256": original_sha256,
            "deidentified_ref": deidentified_ref,
            "deidentified_sha256": deidentified_sha256,
            "verified": True,
        }
    )


def _normalized_role_text(value: str) -> str:
    return " ".join(value.replace("：", ":").split()).replace(
        " :", ":"
    )


def _structure_error(message: str) -> CurrentIncidentReportBindingError:
    return CurrentIncidentReportBindingError(
        "stale_sample_content_neutralization_unavailable",
        message,
    )


def _rating_vector(
    value: Any,
    *,
    expected_count: int,
) -> tuple[str, ...]:
    if (
        isinstance(value, (str, bytes, bytearray, Mapping))
        or not isinstance(value, Sequence)
        or len(value) != expected_count
        or any(
            rating not in {*_GRADE_NAMES, "clear_unassessed"}
            for rating in value
        )
    ):
        raise CurrentIncidentReportBindingError(
            "evaluation_matrix_incomplete",
            "each occupied page pair requires one explicit rating or "
            "clear_unassessed value for every evaluation row",
        )
    return tuple(value)


def _normalize_rating_policy(
    value: Any,
    *,
    page_pair_count: int,
    expected_count: int,
) -> tuple[
    tuple[str, ...] | None,
    dict[int, tuple[str, ...]],
]:
    """Accept one shared vector or explicit page-pair-specific vectors.

    ``clear_unassessed`` is a real final state: it removes the historical
    marker without asserting that an unobserved road condition was good.
    """

    if isinstance(value, Mapping):
        by_pair: dict[int, tuple[str, ...]] = {}
        for raw_pair, raw_vector in value.items():
            try:
                pair = int(raw_pair)
            except (TypeError, ValueError) as error:
                raise CurrentIncidentReportBindingError(
                    "evaluation_matrix_incomplete",
                    "evaluation page-pair identity is invalid",
                ) from error
            if (
                isinstance(raw_pair, bool)
                or not 0 <= pair < page_pair_count
                or pair in by_pair
                or str(pair) != str(raw_pair)
            ):
                raise CurrentIncidentReportBindingError(
                    "evaluation_matrix_incomplete",
                    "evaluation page-pair identity is invalid or duplicate",
                )
            by_pair[pair] = _rating_vector(
                raw_vector,
                expected_count=expected_count,
            )
        if not by_pair:
            raise CurrentIncidentReportBindingError(
                "evaluation_matrix_incomplete",
                "evaluation page-pair ratings are empty",
            )
        return None, by_pair
    return (
        _rating_vector(value, expected_count=expected_count),
        {},
    )


def _one_role_cell(table, prefix: str):
    cells = tuple(
        cell
        for cell in table.cells
        if _normalized_role_text(cell.text_excerpt).startswith(prefix)
    )
    if len(cells) != 1:
        raise _structure_error(
            f"sample {prefix} cell classification is incomplete"
        )
    return cells[0]


def _classify_page_pair(
    inspection: SampleLayoutInspection,
    pair: int,
) -> _PagePair:
    detail_index = pair * 3 + 1
    try:
        table = inspection.snapshot.tables[detail_index]
    except IndexError as error:
        raise _structure_error(
            "sample inspection page sequence is incomplete"
        ) from error
    route = _one_role_cell(table, "노선명:")
    inspected_date = _one_role_cell(table, "점검일:")
    opinion_labels = tuple(
        cell
        for cell in table.cells
        if _normalized_role_text(cell.text_excerpt) == "점검의견"
    )
    if len(opinion_labels) != 1:
        raise _structure_error(
            "sample opinion label classification is incomplete"
        )
    opinion_candidates = tuple(
        cell
        for cell in table.cells
        if (
            cell.row == opinion_labels[0].row + 1
            and cell.column == opinion_labels[0].column
        )
    )
    if len(opinion_candidates) != 1:
        raise _structure_error(
            "sample opinion body classification is incomplete"
        )
    opinion = opinion_candidates[0]

    headers = tuple(
        sorted(
            (
                cell
                for cell in table.cells
                if (
                    cell.row == 1
                    and cell.text_excerpt.strip() in _GRADE_LABELS
                )
            ),
            key=lambda item: (item.column, item.target_id),
        )
    )
    if (
        len(headers) != len(_GRADE_NAMES)
        or tuple(cell.text_excerpt.strip() for cell in headers)
        != _GRADE_LABELS
    ):
        raise _structure_error(
            "sample evaluation grade header classification is incomplete"
        )
    anchors = tuple(
        cell.column + cell.column_span - 1 for cell in headers
    )
    grade_left = min(cell.column for cell in headers)
    evaluation_rows: list[_EvaluationRow] = []
    old_markers: list[Mapping[str, Any]] = []
    for row in range(2, opinion_labels[0].row):
        labels = tuple(
            cell
            for cell in table.cells
            if (
                cell.row == row
                and cell.column < grade_left
                and cell.text_excerpt.strip()
            )
        )
        if not labels:
            continue
        row_cells = tuple(
            cell for cell in table.cells if cell.row == row
        )
        targets = []
        for grade, anchor in zip(_GRADE_NAMES, anchors):
            covering = tuple(
                cell
                for cell in row_cells
                if cell.column <= anchor < cell.column + cell.column_span
            )
            if len(covering) != 1:
                raise _structure_error(
                    "sample evaluation row target classification is incomplete"
                )
            cell = covering[0]
            targets.append(
                {
                    "grade": grade,
                    "target_id": cell.target_id,
                    "anchor_column": anchor,
                    "expected_value": {
                        "text": cell.text_excerpt,
                        "text_sha256": cell.text_sha256,
                    },
                }
            )
        label_text = " / ".join(
            cell.text_excerpt.strip() for cell in labels
        )
        evaluation_rows.append(
            _EvaluationRow(
                row_order=len(evaluation_rows),
                row=row,
                label_sha256=_text_sha256(label_text),
                five_grade_grid_available=(
                    len(
                        {
                            item["target_id"]
                            for item in targets
                        }
                    )
                    == len(_GRADE_NAMES)
                ),
                grade_targets=tuple(targets),
            )
        )
    if not 20 <= len(evaluation_rows) <= 40:
        raise _structure_error(
            "sample evaluation row classification is incomplete"
        )
    evaluation_target_ids = {
        target["target_id"]
        for row in evaluation_rows
        for target in row.grade_targets
    }
    for cell in table.cells:
        if cell.text_excerpt != "■":
            continue
        if cell.target_id not in evaluation_target_ids:
            raise _structure_error(
                "sample evaluation marker classification is incomplete"
            )
        old_markers.append(
            {
                "target_id": cell.target_id,
                "row": cell.row,
                "column": cell.column,
                "expected_value": {
                    "text": cell.text_excerpt,
                    "text_sha256": cell.text_sha256,
                },
            }
        )
    if not old_markers:
        raise _structure_error(
            "sample evaluation marker classification is incomplete"
        )
    return _PagePair(
        page_pair_index=pair,
        inspection_page_number=pair * 2 + 1,
        photo_page_number=pair * 2 + 2,
        route_target_id=route.target_id,
        route_expected_text=route.text_excerpt,
        route_expected_sha256=route.text_sha256,
        date_target_id=inspected_date.target_id,
        date_expected_text=inspected_date.text_excerpt,
        date_expected_sha256=inspected_date.text_sha256,
        opinion_target_id=opinion.target_id,
        opinion_expected_text=opinion.text_excerpt,
        opinion_expected_sha256=opinion.text_sha256,
        old_marker_targets=tuple(
            sorted(
                old_markers,
                key=lambda item: (
                    item["row"],
                    item["column"],
                    item["target_id"],
                ),
            )
        ),
        evaluation_rows=tuple(evaluation_rows),
    )


def _audit_template(
    semantic_map: "SampleTemplateSemanticMap",
    inspection: SampleLayoutInspection,
) -> tuple[tuple[_PhotoSlot, ...], tuple[_PagePair, ...]]:
    if (
        semantic_map.template_sha256
        != inspection.snapshot.document_sha256
        or semantic_map.document_snapshot_id
        != inspection.document_snapshot_id
        or semantic_map.structure_target_summary_sha256
        != inspection.snapshot.target_summary_sha256
        or semantic_map.canonical_template_id
        != inspection.baseline.template_id
        or not _SHA256.fullmatch(semantic_map.semantic_map_sha256)
    ):
        raise CurrentIncidentReportBindingError(
            "sample_template_provenance_conflict",
            "the semantic map and exact sample inspection do not match",
        )
    structure = semantic_map.document_structure_summary
    if not isinstance(structure, Mapping):
        raise _structure_error("sample semantic target summary is unavailable")
    mapped_pictures = structure.get("image_targets")
    mapped_cells = structure.get("cell_targets")
    if (
        not isinstance(mapped_pictures, list)
        or not isinstance(mapped_cells, list)
        or len(mapped_pictures) != len(set(mapped_pictures))
        or len(mapped_cells) != len(set(mapped_cells))
    ):
        raise _structure_error("sample semantic target summary is incomplete")

    page_pairs = tuple(
        _classify_page_pair(inspection, pair)
        for pair in range(inspection.baseline.page_pair_count)
    )
    expected_row_count = len(page_pairs[0].evaluation_rows)
    if any(
        len(page.evaluation_rows) != expected_row_count
        for page in page_pairs
    ):
        raise _structure_error(
            "sample evaluation row count is inconsistent"
        )

    grouped: dict[tuple[int, str], list[Any]] = {}
    all_photo_picture_targets = set()
    required_mapped_cells = set()
    for picture in inspection.snapshot.pictures:
        if (
            picture.nearest_table_index is None
            or picture.nearest_table_index % 3 != 2
        ):
            continue
        try:
            binding = bind_sample_reference_case(
                inspection, picture.target_id
            )
        except HwpxSampleLayoutError as error:
            raise _structure_error(
                "sample photo/caption classification is incomplete"
            ) from error
        key = (
            binding.page_pair_index,
            binding.photo_cell_target_id,
        )
        grouped.setdefault(key, []).append((picture, binding))
        all_photo_picture_targets.add(picture.target_id)
        required_mapped_cells.update(
            {
                binding.photo_caption.cell_target_id,
                binding.inspection_route.cell_target_id,
                binding.inspection_opinion_body.cell_target_id,
                *(
                    marker.cell_target_id
                    for marker in binding.square_markers
                ),
            }
        )
    if set(mapped_pictures) != all_photo_picture_targets:
        raise _structure_error(
            "sample photo target coverage is incomplete"
        )
    if not required_mapped_cells.issubset(set(mapped_cells)):
        raise _structure_error(
            "sample factual cell target coverage is incomplete"
        )

    slots = []
    for (_, _), members in grouped.items():
        members.sort(
            key=lambda item: (
                item[0].picture_index,
                item[0].target_id,
            )
        )
        first = members[0][1]
        if any(
            (
                binding.page_pair_index,
                binding.photo_cell_target_id,
                binding.photo_caption.cell_target_id,
            )
            != (
                first.page_pair_index,
                first.photo_cell_target_id,
                first.photo_caption.cell_target_id,
            )
            for _, binding in members
        ):
            raise _structure_error(
                "sample overlaid photo slot classification is inconsistent"
            )
        slot_payload = {
            "document_sha256": inspection.snapshot.document_sha256,
            "page_pair_index": first.page_pair_index,
            "photo_cell_target_id": first.photo_cell_target_id,
            "picture_target_ids": [
                picture.target_id for picture, _ in members
            ],
            "caption_target_id": (
                first.photo_caption.cell_target_id
            ),
        }
        slots.append(
            _PhotoSlot(
                page_pair_index=first.page_pair_index,
                inspection_page_number=(
                    first.inspection_page_number
                ),
                photo_page_number=first.photo_page_number,
                photo_slot_id=(
                    "current-photo-slot-"
                    + _sha256_value(slot_payload)[:24]
                ),
                photo_cell_target_id=first.photo_cell_target_id,
                photo_cell_row=first.photo_cell_row,
                photo_cell_column=first.photo_cell_column,
                picture_target_ids=tuple(
                    picture.target_id for picture, _ in members
                ),
                picture_indexes=tuple(
                    picture.picture_index for picture, _ in members
                ),
                caption_target_id=(
                    first.photo_caption.cell_target_id
                ),
                caption_expected_text=(
                    first.photo_caption.text_excerpt
                ),
                caption_expected_sha256=(
                    first.photo_caption.text_sha256
                ),
            )
        )
    slots.sort(
        key=lambda slot: (
            slot.page_pair_index,
            slot.photo_cell_row,
            slot.photo_cell_column,
            slot.picture_indexes,
            slot.picture_target_ids,
        )
    )
    if not slots:
        raise _structure_error("sample photo slots are unavailable")
    return tuple(slots), page_pairs


def _load_stale_factual_audit(
    semantic_map: "SampleTemplateSemanticMap",
    inspection: SampleLayoutInspection,
) -> HwpxStaleFactualTargetAudit:
    """Load and bind the 120 exact stale-factual targets to this sample."""

    try:
        audit = audit_stale_factual_targets(semantic_map.source_path)
    except (HwpxStaleFactualTargetError, OSError, ValueError) as error:
        raise _structure_error(
            "the exact stale-factual target audit is unavailable"
        ) from error
    expected_pairs = inspection.baseline.page_pair_count
    if (
        audit.template_id != inspection.baseline.template_id
        or audit.document_sha256 != inspection.snapshot.document_sha256
        or audit.document_snapshot_id != inspection.document_snapshot_id
        or audit.structure_target_summary_sha256
        != inspection.snapshot.target_summary_sha256
        or audit.counts.page_pair_count != expected_pairs
        or len(audit.page_pairs) != expected_pairs
        or audit.counts.required_target_count != expected_pairs * 5
        or len(audit.required_target_ids) != expected_pairs * 5
        or len(set(audit.required_target_ids))
        != len(audit.required_target_ids)
        or not audit.complete_page_pair_coverage
        or not audit.pre_save_full_residual_scan_required
    ):
        raise _structure_error(
            "the stale-factual target audit does not match the exact sample"
        )
    return audit


def _normalize_incidents(
    incidents: Sequence[Mapping[str, Any]],
    *,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
) -> tuple[dict[str, Any], ...]:
    if (
        isinstance(incidents, (str, bytes, bytearray, Mapping))
        or not isinstance(incidents, Sequence)
        or not incidents
        or len(incidents) > 500
    ):
        raise CurrentIncidentReportBindingError(
            "approved_incident_set_invalid",
            "the complete ordered approved Incident set is required",
        )
    normalized = []
    seen = set()
    required_keys = {
        "incident_id",
        "project_id",
        "pipeline_job_id",
        "analysis_job_id",
        "source_sha256",
        "review_status",
        "approval_actor",
        "approval_record_id",
        "approval_record_sha256",
        "incident_type",
        "severity",
        "maintenance_action",
        "ocr_text",
        "time_ms",
        "evidence_refs",
        "approved_output_authority",
    }
    for order, incident in enumerate(incidents):
        if (
            not isinstance(incident, Mapping)
            or set(incident) != required_keys
        ):
            raise CurrentIncidentReportBindingError(
                "approved_incident_set_invalid",
                "an approved Incident is missing its exact review provenance",
            )
        incident_id = incident.get("incident_id")
        if (
            not isinstance(incident_id, str)
            or _IDENTIFIER.fullmatch(incident_id) is None
        ):
            raise CurrentIncidentReportBindingError(
                "approved_incident_set_invalid",
                "an approved Incident identity is invalid",
            )
        if incident_id in seen:
            raise CurrentIncidentReportBindingError(
                "approved_incident_duplicate",
                "an approved Incident appears more than once",
            )
        seen.add(incident_id)
        approval_record_id = incident["approval_record_id"]
        approval_record_sha256 = incident["approval_record_sha256"]
        if (
            incident["review_status"] != "approved"
            or incident["approval_actor"] != "local_user"
            or incident["project_id"] != str(project_id)
            or incident["pipeline_job_id"] != str(pipeline_job_id)
            or incident["analysis_job_id"] != str(analysis_job_id)
            or incident["source_sha256"] != source_sha256
            or not isinstance(approval_record_id, str)
            or _IDENTIFIER.fullmatch(approval_record_id) is None
            or not isinstance(approval_record_sha256, str)
            or _SHA256.fullmatch(approval_record_sha256) is None
        ):
            raise CurrentIncidentReportBindingError(
                "approved_incident_provenance_conflict",
                "an Incident is not user-approved for the selected pipeline job",
            )
        evidence_refs = incident["evidence_refs"]
        if (
            not isinstance(evidence_refs, list)
            or len(evidence_refs) != 1
            or not isinstance(evidence_refs[0], str)
        ):
            raise CurrentIncidentReportBindingError(
                "approved_incident_provenance_conflict",
                "an approved Incident evidence reference is invalid",
            )
        approved_evidence_ref = _opaque_ref(
            evidence_refs[0],
            "approved incident evidence reference",
        )
        try:
            output_authority = (
                validate_approved_incident_output_authority(
                    incident["approved_output_authority"],
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    analysis_job_id=analysis_job_id,
                    source_sha256=source_sha256,
                    incident_id=incident_id,
                    approval_record_id=approval_record_id,
                    approval_record_sha256=approval_record_sha256,
                    representative_image_ref=approved_evidence_ref,
                )
            )
        except ApprovedIncidentAuthorityError as error:
            raise CurrentIncidentReportBindingError(
                "approved_incident_authority_invalid",
                "an Incident lacks exact promoted-survey output authority",
            ) from error
        incident_type = incident["incident_type"]
        action = incident["maintenance_action"]
        incident_type = _safe_text(
            incident_type, "incident type", maximum=200
        )
        action = _safe_text(
            action, "maintenance action", maximum=1_500
        )
        severity = incident["severity"]
        if severity not in {
            "minor",
            "moderate",
            "major",
            "urgent",
            "unassessed",
            "",
        }:
            raise CurrentIncidentReportBindingError(
                "approved_incident_content_incomplete",
                "an approved Incident severity is invalid",
            )
        time_ms = incident["time_ms"]
        if (
            isinstance(time_ms, bool)
            or not isinstance(time_ms, int)
            or not 0 <= time_ms <= 2_678_400_000
        ):
            raise CurrentIncidentReportBindingError(
                "approved_incident_content_incomplete",
                "an approved Incident time is invalid",
            )
        normalized.append(
            {
                "incident_id": incident_id,
                "incident_order": order,
                "project_id": str(project_id),
                "pipeline_job_id": str(pipeline_job_id),
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "review_status": "approved",
                "approval_actor": "local_user",
                "approval_record_id": approval_record_id,
                "approval_record_sha256": approval_record_sha256,
                "approved_evidence_ref": approved_evidence_ref,
                "approved_output_authority": output_authority,
                "survey_incident_authority_sha256": (
                    output_authority[
                        "survey_incident_authority_sha256"
                    ]
                ),
                "approved_incident_authority_sha256": (
                    output_authority["authority_sha256"]
                ),
                "incident_type": incident_type,
                "severity": severity or "unassessed",
                "maintenance_action": action,
                "ocr_text": (
                    _safe_text(
                        incident["ocr_text"],
                        "incident OCR",
                        maximum=1_000,
                    )
                    if incident.get("ocr_text")
                    else None
                ),
                "time_ms": time_ms,
            }
        )
    return tuple(normalized)


def _normalize_evidence(
    *,
    incident_ids: tuple[str, ...],
    descriptors: Mapping[str, Mapping[str, Any]],
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
    evidence_mode: str,
) -> dict[str, dict[str, str]]:
    if (
        not isinstance(descriptors, Mapping)
        or set(descriptors) != set(incident_ids)
    ):
        raise CurrentIncidentReportBindingError(
            "incident_evidence_missing",
            "every approved Incident requires exactly one evidence descriptor",
        )
    selected = {}
    exact_keys = {
        "incident_id",
        "project_id",
        "pipeline_job_id",
        "analysis_job_id",
        "source_sha256",
        "original_ref",
        "original_sha256",
        "deidentified_ref",
        "deidentified_sha256",
        "deidentification_record_sha256",
        "deidentified_verified",
    }
    for incident_id in incident_ids:
        item = descriptors[incident_id]
        if not isinstance(item, Mapping) or set(item) != exact_keys:
            raise CurrentIncidentReportBindingError(
                "incident_evidence_invalid",
                "an evidence descriptor does not match the server contract",
            )
        if (
            item["incident_id"] != incident_id
            or item["project_id"] != str(project_id)
            or item["pipeline_job_id"] != str(pipeline_job_id)
            or item["analysis_job_id"] != str(analysis_job_id)
            or item["source_sha256"] != source_sha256
        ):
            raise CurrentIncidentReportBindingError(
                "incident_evidence_provenance_conflict",
                "evidence crossed the selected pipeline provenance",
            )
        original_ref = _opaque_ref(
            item["original_ref"], "original_ref"
        )
        original_sha = _sha256(
            item["original_sha256"], "original_sha256"
        )
        deid_values = (
            item["deidentified_ref"],
            item["deidentified_sha256"],
            item["deidentification_record_sha256"],
        )
        has_any_deid = any(value is not None for value in deid_values)
        has_all_deid = all(value is not None for value in deid_values)
        verified = item["deidentified_verified"] is True
        if has_any_deid != has_all_deid or verified != has_all_deid:
            raise CurrentIncidentReportBindingError(
                "incident_evidence_invalid",
                "deidentified evidence verification is incomplete",
            )
        deid_ref = None
        deid_sha = None
        if has_all_deid:
            deid_ref = _opaque_ref(
                item["deidentified_ref"], "deidentified_ref"
            )
            deid_sha = _sha256(
                item["deidentified_sha256"],
                "deidentified_sha256",
            )
            record_sha = _sha256(
                item["deidentification_record_sha256"],
                "deidentification_record_sha256",
            )
            expected_record_sha = deidentification_record_sha256(
                incident_id=incident_id,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                source_sha256=source_sha256,
                original_ref=original_ref,
                original_sha256=original_sha,
                deidentified_ref=deid_ref,
                deidentified_sha256=deid_sha,
            )
            if (
                record_sha != expected_record_sha
                or deid_ref == original_ref
                or deid_sha == original_sha
            ):
                raise CurrentIncidentReportBindingError(
                    "deidentified_evidence_not_verified",
                    "a renamed original is not verified deidentification",
                )
        if evidence_mode == "shared_deidentified":
            if not has_all_deid:
                raise CurrentIncidentReportBindingError(
                    "deidentified_evidence_required",
                    "sharing requires verified deidentified evidence",
                )
            selected[incident_id] = {
                "ref": deid_ref,
                "sha256": deid_sha,
                "policy": evidence_mode,
                "approved_ref": original_ref,
                "approved_sha256": original_sha,
            }
        else:
            selected[incident_id] = {
                "ref": original_ref,
                "sha256": original_sha,
                "policy": evidence_mode,
                "approved_ref": original_ref,
                "approved_sha256": original_sha,
            }
    return selected


def _mutation(
    *,
    role: str,
    operation: str,
    target_id: str,
    pair: int,
    page: int,
    expected: Mapping[str, Any],
    current: Mapping[str, Any],
    refs: Sequence[str],
) -> ReportTargetMutation:
    return ReportTargetMutation(
        role=role,
        operation=operation,
        target_id=target_id,
        page_pair_index=pair,
        page_number=page,
        expected_value=dict(expected),
        current_value=dict(current),
        current_source_refs=tuple(refs),
    )


def _bind_stale_factual_targets(
    *,
    audit: HwpxStaleFactualTargetAudit,
    route_name: str,
    inspection_day: date,
    inspector_name: str,
    source_sha256: str,
    pipeline_job_id: UUID,
) -> tuple[
    tuple[ReportTargetMutation, ...],
    tuple[ReportTargetMutation, ...],
]:
    """Project current facts or explicit neutralization onto all five surfaces."""

    quarter = (inspection_day.month - 1) // 3 + 1
    title_group_value = "현재 점검"
    title_route_value = (
        f"{quarter}분기 일반평가(불시점검) 양식({route_name})"
    )
    photo_header_value = f"□ {route_name} 현황사진"
    inspector_value = f"점검자 : {inspector_name} (인)"
    base_refs = (
        f"source-video:{source_sha256}",
        f"pipeline-job:{pipeline_job_id}",
        f"inspection-date:{inspection_day.isoformat()}",
    )
    replacements: list[ReportTargetMutation] = []
    neutralizations: list[ReportTargetMutation] = []
    supplied_target_ids: list[str] = []

    def add_text(
        *,
        role: str,
        target,
        value: str,
        refs: Sequence[str],
    ) -> None:
        replacements.append(
            _mutation(
                role=role,
                operation="replace_text",
                target_id=target.target_id,
                pair=target.page_pair_index,
                page=target.page_number,
                expected={
                    "text": target.expected_text,
                    "text_sha256": target.expected_text_sha256,
                },
                current={
                    "text": value,
                    "text_sha256": _text_sha256(value),
                },
                refs=refs,
            )
        )
        supplied_target_ids.append(target.target_id)

    for pair in audit.page_pairs:
        add_text(
            role="current_title_group",
            target=pair.title_group,
            value=title_group_value,
            refs=(*base_refs, "report-profile:title-group"),
        )
        add_text(
            role="current_title_route_heading",
            target=pair.title_route_heading,
            value=title_route_value,
            refs=(*base_refs, "report-profile:route"),
        )
        add_text(
            role="current_inspection_inspector",
            target=pair.inspection_inspector,
            value=inspector_value,
            refs=(*base_refs, "report-profile:inspector"),
        )
        add_text(
            role="current_photo_page_header",
            target=pair.photo_page_header,
            value=photo_header_value,
            refs=(*base_refs, "report-profile:route"),
        )
        signature = pair.inspector_signature
        neutralizations.append(
            _mutation(
                role="neutralized_stale_inspector_signature",
                operation="remove_picture",
                target_id=signature.target_id,
                pair=signature.page_pair_index,
                page=signature.page_number,
                expected={
                    "picture_target_id": signature.target_id,
                    "template_sha256": audit.document_sha256,
                    "expected_binding_sha256": (
                        signature.expected_binding_sha256
                    ),
                    "binary_item_refs": list(
                        signature.binary_item_refs
                    ),
                    "binary_sha256s": [
                        binary.sha256 for binary in signature.binaries
                    ],
                },
                current={"neutralized": True},
                refs=(
                    *base_refs,
                    "report-policy:no-current-signature-evidence",
                ),
            )
        )
        supplied_target_ids.append(signature.target_id)

    try:
        coverage = require_stale_factual_target_coverage(
            audit,
            supplied_target_ids,
        )
    except HwpxStaleFactualTargetError as error:
        raise _structure_error(
            "historical title, route, inspector, header, or signature can persist"
        ) from error
    if (
        not coverage.complete
        or coverage.covered_target_count
        != audit.counts.required_target_count
    ):
        raise _structure_error(
            "stale-factual target coverage is incomplete"
        )
    return tuple(replacements), tuple(neutralizations)


def bind_current_incidents_to_sample(
    *,
    semantic_map: "SampleTemplateSemanticMap",
    inspection: SampleLayoutInspection,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
    approved_incidents: Sequence[Mapping[str, Any]],
    evidence_by_incident_id: Mapping[str, Mapping[str, Any]],
    route_name: str,
    inspection_date: str,
    report_inspector_name: str,
    locations_by_incident_id: Mapping[str, str],
    report_policy: Mapping[str, Any],
) -> CurrentIncidentReportBindingManifest:
    """Bind all current approved Incidents exactly once to audited slots."""

    # Local import avoids a module cycle when the preview preparer later owns
    # this binder while retaining an exact runtime contract.
    from .pipeline_report_preview import SampleTemplateSemanticMap

    if not all(
        isinstance(value, UUID)
        for value in (project_id, pipeline_job_id, analysis_job_id)
    ) or not isinstance(
        inspection, SampleLayoutInspection
    ) or not isinstance(semantic_map, SampleTemplateSemanticMap):
        raise CurrentIncidentReportBindingError(
            "current_report_provenance_invalid",
            "selected report provenance is invalid",
        )
    if not isinstance(source_sha256, str) or not _SHA256.fullmatch(
        source_sha256
    ):
        raise CurrentIncidentReportBindingError(
            "current_report_provenance_invalid",
            "source_sha256 is invalid",
        )
    if (
        not isinstance(report_policy, Mapping)
        or set(report_policy)
        != {"evidence_mode", "evaluation_ratings"}
        or report_policy["evidence_mode"]
        not in {"internal_original", "shared_deidentified"}
    ):
        raise CurrentIncidentReportBindingError(
            "current_report_policy_invalid",
            "report policy is invalid",
        )
    route_name = _safe_text(route_name, "route name", maximum=300)
    inspector_name = _safe_text(
        report_inspector_name,
        "report inspector name",
        maximum=100,
    )
    if inspector_name.casefold() == "unconfigured":
        raise CurrentIncidentReportBindingError(
            "current_report_inspector_unconfigured",
            "a current report inspector name is required",
        )
    try:
        inspection_day = date.fromisoformat(inspection_date)
        normalized_date = inspection_day.isoformat()
    except (TypeError, ValueError) as error:
        raise CurrentIncidentReportBindingError(
            "current_report_content_invalid",
            "inspection_date must be an ISO date",
        ) from error

    slots, page_pairs = _audit_template(semantic_map, inspection)
    stale_factual_audit = _load_stale_factual_audit(
        semantic_map,
        inspection,
    )
    expected_rating_count = len(page_pairs[0].evaluation_rows)
    shared_ratings, ratings_by_pair = _normalize_rating_policy(
        report_policy["evaluation_ratings"],
        page_pair_count=len(page_pairs),
        expected_count=expected_rating_count,
    )
    incidents = _normalize_incidents(
        approved_incidents,
        project_id=project_id,
        pipeline_job_id=pipeline_job_id,
        analysis_job_id=analysis_job_id,
        source_sha256=source_sha256,
    )
    if len(incidents) > len(slots):
        raise CurrentIncidentReportBindingError(
            "sample_incident_capacity_exceeded",
            "approved Incidents exceed the exact audited photo-slot capacity",
        )
    incident_ids = tuple(item["incident_id"] for item in incidents)
    if (
        not isinstance(locations_by_incident_id, Mapping)
        or set(locations_by_incident_id) != set(incident_ids)
    ):
        raise CurrentIncidentReportBindingError(
            "approved_incident_content_incomplete",
            "every approved Incident requires one current location",
        )
    locations = {
        incident_id: _safe_text(
            locations_by_incident_id[incident_id],
            "incident location",
            maximum=500,
        )
        for incident_id in incident_ids
    }
    evidence = _normalize_evidence(
        incident_ids=incident_ids,
        descriptors=evidence_by_incident_id,
        project_id=project_id,
        pipeline_job_id=pipeline_job_id,
        analysis_job_id=analysis_job_id,
        source_sha256=source_sha256,
        evidence_mode=report_policy["evidence_mode"],
    )
    if any(
        evidence[item["incident_id"]]["approved_ref"]
        != item["approved_evidence_ref"]
        for item in incidents
    ):
        raise CurrentIncidentReportBindingError(
            "incident_evidence_provenance_conflict",
            "approved Incident evidence does not match the verified descriptor",
        )
    try:
        for item in incidents:
            selected = evidence[item["incident_id"]]
            validate_approved_incident_output_authority(
                item["approved_output_authority"],
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                source_sha256=source_sha256,
                incident_id=item["incident_id"],
                approval_record_id=item["approval_record_id"],
                approval_record_sha256=item[
                    "approval_record_sha256"
                ],
                representative_image_ref=selected["approved_ref"],
                representative_image_sha256=selected[
                    "approved_sha256"
                ],
            )
    except ApprovedIncidentAuthorityError as error:
        raise CurrentIncidentReportBindingError(
            "incident_evidence_authority_conflict",
            "approved Incident evidence changed after final survey approval",
        ) from error

    survey_authority_digests = {
        item["survey_incident_authority_sha256"] for item in incidents
    }
    if len(survey_authority_digests) != 1:
        raise CurrentIncidentReportBindingError(
            "approved_incident_authority_invalid",
            "approved Incidents crossed promoted-survey authorities",
        )
    survey_incident_authority_sha256 = next(
        iter(survey_authority_digests)
    )
    approved_incident_authority_set_sha256 = _sha256_value(
        [
            item["approved_incident_authority_sha256"]
            for item in incidents
        ]
    )

    static_replacements, static_neutralizations = (
        _bind_stale_factual_targets(
            audit=stale_factual_audit,
            route_name=route_name,
            inspection_day=inspection_day,
            inspector_name=inspector_name,
            source_sha256=source_sha256,
            pipeline_job_id=pipeline_job_id,
        )
    )
    incident_bindings = []
    replacements = list(static_replacements)
    neutralizations = list(static_neutralizations)
    assigned_by_pair: dict[int, list[dict[str, Any]]] = {}
    for index, incident in enumerate(incidents):
        slot = slots[index]
        selected = evidence[incident["incident_id"]]
        assigned_by_pair.setdefault(slot.page_pair_index, []).append(
            incident
        )
        source_refs = (
            f"incident:{incident['incident_id']}",
            f"approval-record:{incident['approval_record_id']}",
            (
                "approval-record-sha256:"
                f"{incident['approval_record_sha256']}"
            ),
            (
                "approved-incident-authority-sha256:"
                f"{incident['approved_incident_authority_sha256']}"
            ),
            (
                "survey-incident-authority-sha256:"
                f"{incident['survey_incident_authority_sha256']}"
            ),
            f"source-video:{source_sha256}",
            f"evidence-sha256:{selected['sha256']}",
        )
        photo_payload = {
            "incident_id": incident["incident_id"],
            "incident_order": index,
            "photo_slot_id": slot.photo_slot_id,
            "primary_picture_target_id": (
                slot.picture_target_ids[0]
            ),
            "additional_picture_target_ids": (
                slot.picture_target_ids[1:]
            ),
            "caption_target_id": slot.caption_target_id,
            "selected_evidence_ref": selected["ref"],
            "selected_evidence_sha256": selected["sha256"],
            "evidence_policy": selected["policy"],
            "source_sha256": source_sha256,
            "approved_incident_authority_sha256": incident[
                "approved_incident_authority_sha256"
            ],
        }
        incident_bindings.append(
            IncidentSlotBinding(
                incident_id=incident["incident_id"],
                incident_order=index,
                page_pair_index=slot.page_pair_index,
                inspection_page_number=(
                    slot.inspection_page_number
                ),
                photo_page_number=slot.photo_page_number,
                photo_slot_id=slot.photo_slot_id,
                photo_cell_target_id=slot.photo_cell_target_id,
                primary_picture_target_id=(
                    slot.picture_target_ids[0]
                ),
                additional_picture_target_ids=(
                    slot.picture_target_ids[1:]
                ),
                caption_cell_target_id=slot.caption_target_id,
                selected_evidence_sha256=selected["sha256"],
                selected_evidence_ref=selected["ref"],
                evidence_policy=selected["policy"],
                approved_incident_authority_sha256=incident[
                    "approved_incident_authority_sha256"
                ],
                photo_binding_sha256=_sha256_value(photo_payload),
            )
        )
        replacements.append(
            _mutation(
                role="incident_photo",
                operation="replace_picture",
                target_id=slot.picture_target_ids[0],
                pair=slot.page_pair_index,
                page=slot.photo_page_number,
                expected={
                    "picture_target_id": slot.picture_target_ids[0],
                    "template_sha256": (
                        inspection.snapshot.document_sha256
                    ),
                },
                current={
                    "evidence_ref": selected["ref"],
                    "evidence_sha256": selected["sha256"],
                    "evidence_policy": selected["policy"],
                },
                refs=source_refs,
            )
        )
        for extra_target in slot.picture_target_ids[1:]:
            neutralizations.append(
                _mutation(
                    role="overlaid_stale_incident_photo",
                    operation="remove_picture",
                    target_id=extra_target,
                    pair=slot.page_pair_index,
                    page=slot.photo_page_number,
                    expected={
                        "picture_target_id": extra_target,
                        "template_sha256": (
                            inspection.snapshot.document_sha256
                        ),
                    },
                    current={"neutralized": True},
                    refs=source_refs,
                )
            )
        caption = (
            f"{locations[incident['incident_id']]} · "
            f"{incident['maintenance_action']}"
        )
        replacements.append(
            _mutation(
                role="incident_caption",
                operation="replace_text",
                target_id=slot.caption_target_id,
                pair=slot.page_pair_index,
                page=slot.photo_page_number,
                expected={
                    "text": slot.caption_expected_text,
                    "text_sha256": slot.caption_expected_sha256,
                },
                current={
                    "text": caption,
                    "text_sha256": _text_sha256(caption),
                },
                refs=source_refs,
            )
        )

    for slot in slots[len(incidents) :]:
        for picture_target in slot.picture_target_ids:
            neutralizations.append(
                _mutation(
                    role="unassigned_stale_incident_photo",
                    operation="remove_picture",
                    target_id=picture_target,
                    pair=slot.page_pair_index,
                    page=slot.photo_page_number,
                    expected={
                        "picture_target_id": picture_target,
                        "template_sha256": (
                            inspection.snapshot.document_sha256
                        ),
                    },
                    current={"neutralized": True},
                    refs=(f"source-video:{source_sha256}",),
                )
            )
        neutralizations.append(
            _mutation(
                role="unassigned_stale_incident_caption",
                operation="clear_text",
                target_id=slot.caption_target_id,
                pair=slot.page_pair_index,
                page=slot.photo_page_number,
                expected={
                    "text": slot.caption_expected_text,
                    "text_sha256": slot.caption_expected_sha256,
                },
                current={"text": "", "neutralized": True},
                refs=(f"source-video:{source_sha256}",),
            )
        )

    occupied_pairs = tuple(sorted(assigned_by_pair))
    evaluation_bindings = []
    route_current = f"노선명 : {route_name}"
    date_current = (
        "점검일 : "
        + normalized_date.replace("-", ".")
        + "."
    )
    for page in page_pairs:
        assigned = assigned_by_pair.get(page.page_pair_index, [])
        base_refs = (
            f"source-video:{source_sha256}",
            f"pipeline-job:{pipeline_job_id}",
        )
        for role, target, expected_text, expected_sha, current in (
            (
                "inspection_route",
                page.route_target_id,
                page.route_expected_text,
                page.route_expected_sha256,
                route_current,
            ),
            (
                "inspection_date",
                page.date_target_id,
                page.date_expected_text,
                page.date_expected_sha256,
                date_current,
            ),
        ):
            replacements.append(
                _mutation(
                    role=role,
                    operation="replace_text",
                    target_id=target,
                    pair=page.page_pair_index,
                    page=page.inspection_page_number,
                    expected={
                        "text": expected_text,
                        "text_sha256": expected_sha,
                    },
                    current={
                        "text": current,
                        "text_sha256": _text_sha256(current),
                    },
                    refs=(
                        *base_refs,
                        "report-profile:route"
                        if role == "inspection_route"
                        else f"inspection-date:{normalized_date}",
                    ),
                )
            )
        if assigned:
            page_ratings = ratings_by_pair.get(
                page.page_pair_index,
                shared_ratings,
            )
            if page_ratings is None:
                raise CurrentIncidentReportBindingError(
                    "evaluation_matrix_incomplete",
                    "an occupied page pair has no current evaluation matrix",
                )
            assigned_refs = tuple(
                f"incident:{item['incident_id']}" for item in assigned
            )
            opinion = "\n".join(
                (
                    f"ㅇ {locations[item['incident_id']]} - "
                    f"{item['maintenance_action']}"
                )
                for item in assigned
            )
            replacements.append(
                _mutation(
                    role="inspection_opinion",
                    operation="replace_text",
                    target_id=page.opinion_target_id,
                    pair=page.page_pair_index,
                    page=page.inspection_page_number,
                    expected={
                        "text": page.opinion_expected_text,
                        "text_sha256": page.opinion_expected_sha256,
                    },
                    current={
                        "text": opinion,
                        "text_sha256": _text_sha256(opinion),
                    },
                    refs=(*base_refs, *assigned_refs),
                )
            )
            for old_marker in page.old_marker_targets:
                neutralizations.append(
                    _mutation(
                        role="stale_evaluation_marker",
                        operation="clear_text",
                        target_id=old_marker["target_id"],
                        pair=page.page_pair_index,
                        page=page.inspection_page_number,
                        expected=old_marker["expected_value"],
                        current={"text": "", "neutralized": True},
                        refs=(*base_refs, *assigned_refs),
                    )
                )
            for row, grade in zip(page.evaluation_rows, page_ratings):
                if grade == "clear_unassessed":
                    selected_target = None
                else:
                    if not row.five_grade_grid_available:
                        raise CurrentIncidentReportBindingError(
                            "evaluation_grid_unavailable",
                            "the selected sample row has no exact five-grade grid",
                        )
                    selected_target = next(
                        target
                        for target in row.grade_targets
                        if target["grade"] == grade
                    )
                evaluation_bindings.append(
                    EvaluationRowBinding(
                        page_pair_index=page.page_pair_index,
                        page_number=page.inspection_page_number,
                        row_order=row.row_order,
                        row=row.row,
                        row_label_sha256=row.label_sha256,
                        selected_grade=grade,
                        selected_target_id=(
                            selected_target["target_id"]
                            if selected_target is not None
                            else None
                        ),
                        selected_anchor_column=(
                            selected_target["anchor_column"]
                            if selected_target is not None
                            else None
                        ),
                        five_grade_grid_available=(
                            row.five_grade_grid_available
                        ),
                        grade_targets=row.grade_targets,
                        current_source_refs=(
                            *base_refs,
                            *assigned_refs,
                            "report-policy:evaluation-ratings",
                        ),
                    )
                )
        else:
            neutralizations.append(
                _mutation(
                    role="unassigned_stale_opinion",
                    operation="clear_text",
                    target_id=page.opinion_target_id,
                    pair=page.page_pair_index,
                    page=page.inspection_page_number,
                    expected={
                        "text": page.opinion_expected_text,
                        "text_sha256": page.opinion_expected_sha256,
                    },
                    current={"text": "", "neutralized": True},
                    refs=base_refs,
                )
            )
            for old_marker in page.old_marker_targets:
                neutralizations.append(
                    _mutation(
                        role="unassigned_stale_evaluation_marker",
                        operation="clear_text",
                        target_id=old_marker["target_id"],
                        pair=page.page_pair_index,
                        page=page.inspection_page_number,
                        expected=old_marker["expected_value"],
                        current={"text": "", "neutralized": True},
                        refs=base_refs,
                    )
                )

    bound_ids = tuple(
        binding.incident_id for binding in incident_bindings
    )
    if bound_ids != incident_ids or len(bound_ids) != len(set(bound_ids)):
        raise CurrentIncidentReportBindingError(
            "approved_incident_binding_incomplete",
            "every approved Incident must be bound exactly once",
        )
    replacement_targets = [
        item.target_id for item in replacements
    ]
    if len(replacement_targets) != len(set(replacement_targets)):
        raise _structure_error(
            "current replacements share an ambiguous sample target"
        )

    normalized_input = {
        "project_id": str(project_id),
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "approved_incidents": incidents,
        "evidence": evidence,
        "route_name": route_name,
        "inspection_date": normalized_date,
        "report_inspector_name": inspector_name,
        "locations": locations,
        "report_policy": {
            "evidence_mode": report_policy["evidence_mode"],
            "evaluation_ratings": (
                list(shared_ratings)
                if shared_ratings is not None
                else {
                    str(pair): list(vector)
                    for pair, vector in sorted(ratings_by_pair.items())
                }
            ),
        },
    }
    slot_payload = {
        "template_sha256": inspection.snapshot.document_sha256,
        "stale_factual_audit_sha256": stale_factual_audit.audit_sha256,
        "stale_factual_target_ids": list(
            stale_factual_audit.required_target_ids
        ),
        "slots": [asdict(slot) for slot in slots],
        "page_pairs": [
            {
                "page_pair_index": item.page_pair_index,
                "route_target_id": item.route_target_id,
                "date_target_id": item.date_target_id,
                "opinion_target_id": item.opinion_target_id,
                "evaluation_rows": [
                    asdict(row) for row in item.evaluation_rows
                ],
            }
            for item in page_pairs
        ],
    }
    content_payload = {
        "incidents": [asdict(item) for item in incident_bindings],
        "replacements": [asdict(item) for item in replacements],
        "neutralizations": [
            asdict(item) for item in neutralizations
        ],
        "evaluation_rows": [
            asdict(item) for item in evaluation_bindings
        ],
    }
    input_sha = _sha256_value(normalized_input)
    slot_sha = _sha256_value(slot_payload)
    content_sha = _sha256_value(content_payload)
    required_writer_operation_count = len(
        {
            *(
                item.target_id
                for item in (*replacements, *neutralizations)
            ),
            *(
                target["target_id"]
                for row in evaluation_bindings
                for target in row.grade_targets
            ),
        }
    )
    legacy_operation_limit = LEGACY_DOCUMENT_EDIT_PLAN_OPERATION_LIMIT
    exact_writer_operation_limit = EXACT_HWPX_WRITER_OPERATION_LIMIT
    if required_writer_operation_count > exact_writer_operation_limit:
        raise CurrentIncidentReportBindingError(
            "exact_writer_operation_capacity_exceeded",
            "the complete report exceeds the exact writer operation capacity",
        )
    manifest_payload = {
        "schema_version": CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
        "project_id": str(project_id),
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "template_id": semantic_map.canonical_template_id,
        "document_snapshot_id": inspection.document_snapshot_id,
        "template_sha256": inspection.snapshot.document_sha256,
        "template_semantic_map_sha256": (
            semantic_map.semantic_map_sha256
        ),
        "structure_target_summary_sha256": (
            inspection.snapshot.target_summary_sha256
        ),
        "incident_capacity": len(slots),
        "approved_incident_ids": incident_ids,
        "occupied_page_pair_indices": occupied_pairs,
        "survey_incident_authority_sha256": (
            survey_incident_authority_sha256
        ),
        "approved_incident_authority_set_sha256": (
            approved_incident_authority_set_sha256
        ),
        "input_sha256": input_sha,
        "content_sha256": content_sha,
        "slot_manifest_sha256": slot_sha,
        "required_writer_operation_count": (
            required_writer_operation_count
        ),
        "legacy_document_edit_plan_operation_limit": (
            legacy_operation_limit
        ),
        "exact_hwpx_writer_operation_limit": (
            exact_writer_operation_limit
        ),
        "legacy_plan_expansion_required": (
            required_writer_operation_count > legacy_operation_limit
        ),
        "writer_batching_required": (
            required_writer_operation_count > exact_writer_operation_limit
        ),
        "approved_incident_bound": True,
        "content_provenance_bound": True,
        "writer_required": True,
    }
    return CurrentIncidentReportBindingManifest(
        schema_version=CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
        state="current_incidents_bound_writer_required",
        project_id=str(project_id),
        pipeline_job_id=str(pipeline_job_id),
        analysis_job_id=str(analysis_job_id),
        source_sha256=source_sha256,
        template_id=semantic_map.canonical_template_id,
        document_snapshot_id=inspection.document_snapshot_id,
        template_sha256=inspection.snapshot.document_sha256,
        template_semantic_map_sha256=(
            semantic_map.semantic_map_sha256
        ),
        structure_target_summary_sha256=(
            inspection.snapshot.target_summary_sha256
        ),
        incident_capacity=len(slots),
        approved_incident_count=len(incidents),
        approved_incident_ids=incident_ids,
        occupied_page_pair_indices=occupied_pairs,
        incident_bindings=tuple(incident_bindings),
        replacements=tuple(replacements),
        neutralizations=tuple(neutralizations),
        evaluation_rows=tuple(evaluation_bindings),
        survey_incident_authority_sha256=(
            survey_incident_authority_sha256
        ),
        approved_incident_authority_set_sha256=(
            approved_incident_authority_set_sha256
        ),
        input_sha256=input_sha,
        content_sha256=content_sha,
        slot_manifest_sha256=slot_sha,
        binding_manifest_sha256=_sha256_value(manifest_payload),
        required_writer_operation_count=(
            required_writer_operation_count
        ),
        legacy_document_edit_plan_operation_limit=(
            legacy_operation_limit
        ),
        exact_hwpx_writer_operation_limit=(
            exact_writer_operation_limit
        ),
        legacy_plan_expansion_required=(
            required_writer_operation_count > legacy_operation_limit
        ),
        writer_batching_required=(
            required_writer_operation_count > exact_writer_operation_limit
        ),
    )


__all__ = [
    "CURRENT_INCIDENT_REPORT_BINDING_SCHEMA",
    "EXACT_HWPX_WRITER_OPERATION_LIMIT",
    "LEGACY_DOCUMENT_EDIT_PLAN_OPERATION_LIMIT",
    "CurrentIncidentReportBindingError",
    "CurrentIncidentReportBindingManifest",
    "EvaluationRowBinding",
    "IncidentSlotBinding",
    "ReportTargetMutation",
    "bind_current_incidents_to_sample",
    "current_incident_report_binding_from_dict",
    "deidentification_record_sha256",
]
