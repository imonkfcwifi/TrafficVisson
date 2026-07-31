"""Exact evaluation-grid addresses for the audited 3Q/4Q HWPX samples.

The completed sample reports contain historical evaluation markers.  This
module treats those markers as stale factual content, never as a default for a
new report.  It provides two read-only/deterministic operations:

* map every inspection table to its route/date/inspector/opinion cells and
  evaluation rows; and
* build a final-value mutation set that clears every historical ``■`` while
  setting only caller-supplied, current ratings.

Most evaluation rows expose five distinct HWPML cells.  Two rows in every
inspection table use only three physical cells: the middle cell spans the
visual good/normal/poor columns.  Those rows are classified as
``merged_not_five_grade``.  They remain fully neutralizable, but this module
will not guess a five-grade target for them.  A separate, explicit binary
semantic contract is required before a new marker may be written there.

This module does not call a Provider, COM, or a document writer and does not
decide whether a current rating is user-approved.  Its target IDs and expected
values are exact-source guards for the server-owned binder/writer boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from .hwpx_sample_layout import (
    SampleLayoutInspection,
    inspect_sample_layout,
)


HWPX_EVALUATION_GRID_SCHEMA = "trafficvisson.hwpx-evaluation-grid.v1"
HWPX_CURRENT_RATING_PLAN_SCHEMA = (
    "trafficvisson.hwpx-current-rating-plan.v1"
)
GRADE_NAMES: Tuple[str, ...] = (
    "very_good",
    "good",
    "normal",
    "poor",
    "very_poor",
)
GRADE_LABELS: Tuple[str, ...] = (
    "매우 우수",
    "우수",
    "보통",
    "미흡",
    "매우 미흡",
)


class HwpxEvaluationGridError(RuntimeError):
    """Fail-closed evaluation-grid mapping or rating-plan error."""

    provider_called = False
    com_invoked = False
    writer_invoked = False
    apply_blocked = True

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EvaluationCellTarget:
    target_id: str
    row: int
    column: int
    row_span: int
    column_span: int
    expected_text: str
    expected_text_sha256: str


@dataclass(frozen=True)
class LogicalGradeTarget:
    grade: str
    header_label: str
    anchor_column: int
    cell: EvaluationCellTarget


@dataclass(frozen=True)
class EvaluationRowMap:
    row_order: int
    row: int
    label: str
    label_sha256: str
    label_target_ids: Tuple[str, ...]
    rating_mode: str
    logical_grade_targets: Tuple[LogicalGradeTarget, ...]
    five_grade_target_ids: Optional[Tuple[str, ...]]
    neutralization_targets: Tuple[EvaluationCellTarget, ...]
    existing_marker_targets: Tuple[EvaluationCellTarget, ...]
    existing_marker_grade: Optional[str]


@dataclass(frozen=True)
class InspectionRoleCells:
    route: EvaluationCellTarget
    inspected_date: EvaluationCellTarget
    inspector: EvaluationCellTarget
    opinion_label: EvaluationCellTarget
    opinion_body: EvaluationCellTarget


@dataclass(frozen=True)
class InspectionEvaluationTableMap:
    page_pair_index: int
    inspection_page_number: int
    table_id: str
    table_index: int
    header_anchor_columns: Tuple[int, ...]
    role_cells: InspectionRoleCells
    evaluation_rows: Tuple[EvaluationRowMap, ...]
    existing_marker_targets: Tuple[EvaluationCellTarget, ...]


@dataclass(frozen=True)
class HwpxEvaluationGridAudit:
    schema_version: str
    template_id: str
    quarter: int
    document_snapshot_id: str
    document_sha256: str
    structure_target_summary_sha256: str
    inspection_table_count: int
    evaluation_row_count: int
    five_grade_row_count: int
    merged_row_count: int
    existing_marker_count: int
    inspection_tables: Tuple[InspectionEvaluationTableMap, ...]
    audit_sha256: str
    read_only: bool = True
    no_provider: bool = True
    no_com: bool = True
    no_document_write: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RatingMarkerMutation:
    target_id: str
    page_pair_index: int
    inspection_page_number: int
    table_index: int
    row: int
    column: int
    expected_text: str
    expected_text_sha256: str
    value: str
    action: str
    explicit_grade: Optional[str]


@dataclass(frozen=True)
class CurrentRatingMutationPlan:
    schema_version: str
    document_snapshot_id: str
    document_sha256: str
    evaluation_grid_sha256: str
    explicit_rating_count: int
    historical_marker_count: int
    cleared_historical_marker_count: int
    selected_marker_count: int
    mutation_count: int
    mutations: Tuple[RatingMarkerMutation, ...]
    plan_sha256: str
    complete_historical_marker_coverage: bool = True
    merged_rows_neutralized_only: bool = True
    no_provider: bool = True
    no_com: bool = True
    no_document_write: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_role_text(value: str) -> str:
    return " ".join(value.replace("：", ":").split()).replace(
        " :", ":"
    )


def _cell_target(cell: Any) -> EvaluationCellTarget:
    return EvaluationCellTarget(
        target_id=cell.target_id,
        row=cell.row,
        column=cell.column,
        row_span=cell.row_span,
        column_span=cell.column_span,
        expected_text=cell.text_excerpt,
        expected_text_sha256=cell.text_sha256,
    )


def _one_prefixed_cell(table: Any, prefix: str) -> Any:
    cells = tuple(
        cell
        for cell in table.cells
        if _normalized_role_text(cell.text_excerpt).startswith(prefix)
    )
    if len(cells) != 1:
        raise HwpxEvaluationGridError(
            "inspection_role_cell_ambiguous",
            "the exact sample inspection role cell is missing or ambiguous",
        )
    return cells[0]


def _inspection_roles(table: Any) -> InspectionRoleCells:
    route = _one_prefixed_cell(table, "노선명:")
    inspected_date = _one_prefixed_cell(table, "점검일:")
    inspector = _one_prefixed_cell(table, "점검자:")
    opinion_labels = tuple(
        cell
        for cell in table.cells
        if _normalized_role_text(cell.text_excerpt) == "점검의견"
    )
    if len(opinion_labels) != 1:
        raise HwpxEvaluationGridError(
            "inspection_role_cell_ambiguous",
            "the exact sample opinion label is missing or ambiguous",
        )
    opinion_label = opinion_labels[0]
    opinion_bodies = tuple(
        cell
        for cell in table.cells
        if (
            cell.row == opinion_label.row + 1
            and cell.column == opinion_label.column
        )
    )
    if len(opinion_bodies) != 1:
        raise HwpxEvaluationGridError(
            "inspection_role_cell_ambiguous",
            "the exact sample opinion body is missing or ambiguous",
        )
    return InspectionRoleCells(
        route=_cell_target(route),
        inspected_date=_cell_target(inspected_date),
        inspector=_cell_target(inspector),
        opinion_label=_cell_target(opinion_label),
        opinion_body=_cell_target(opinion_bodies[0]),
    )


def _grade_headers(table: Any) -> Tuple[Any, ...]:
    headers = tuple(
        sorted(
            (
                cell
                for cell in table.cells
                if (
                    cell.row == 1
                    and cell.text_excerpt.strip() in GRADE_LABELS
                )
            ),
            key=lambda cell: (cell.column, cell.target_id),
        )
    )
    if (
        len(headers) != len(GRADE_LABELS)
        or tuple(cell.text_excerpt.strip() for cell in headers)
        != GRADE_LABELS
    ):
        raise HwpxEvaluationGridError(
            "evaluation_grade_header_invalid",
            "the exact five evaluation headers cannot be mapped",
        )
    anchors = tuple(
        cell.column + cell.column_span - 1 for cell in headers
    )
    if len(set(anchors)) != len(anchors):
        raise HwpxEvaluationGridError(
            "evaluation_grade_header_invalid",
            "the evaluation header anchors are not distinct",
        )
    return headers


def _logical_targets(
    row_cells: Sequence[Any],
    headers: Sequence[Any],
) -> Tuple[LogicalGradeTarget, ...]:
    targets = []
    for grade, label, header in zip(
        GRADE_NAMES, GRADE_LABELS, headers
    ):
        anchor = header.column + header.column_span - 1
        covering = tuple(
            cell
            for cell in row_cells
            if cell.column <= anchor < cell.column + cell.column_span
        )
        if len(covering) != 1:
            raise HwpxEvaluationGridError(
                "evaluation_row_target_ambiguous",
                "an evaluation header does not resolve to one physical cell",
            )
        cell = covering[0]
        if cell.text_excerpt not in {"", "■"}:
            raise HwpxEvaluationGridError(
                "evaluation_target_contains_unexpected_text",
                "an evaluation marker cell contains unexpected sample text",
            )
        targets.append(
            LogicalGradeTarget(
                grade=grade,
                header_label=label,
                anchor_column=anchor,
                cell=_cell_target(cell),
            )
        )
    return tuple(targets)


def _unique_physical_targets(
    targets: Sequence[LogicalGradeTarget],
) -> Tuple[EvaluationCellTarget, ...]:
    unique: Dict[str, EvaluationCellTarget] = {}
    for target in targets:
        existing = unique.setdefault(
            target.cell.target_id, target.cell
        )
        if existing != target.cell:
            raise HwpxEvaluationGridError(
                "evaluation_target_identity_conflict",
                "one target ID resolves to conflicting physical cells",
            )
    return tuple(
        sorted(
            unique.values(),
            key=lambda cell: (
                cell.column,
                cell.column_span,
                cell.target_id,
            ),
        )
    )


def _evaluation_rows(
    table: Any,
    headers: Sequence[Any],
    roles: InspectionRoleCells,
) -> Tuple[EvaluationRowMap, ...]:
    grade_left = min(cell.column for cell in headers)
    rows = []
    for row in range(2, roles.opinion_label.row):
        row_cells = tuple(
            cell for cell in table.cells if cell.row == row
        )
        labels = tuple(
            sorted(
                (
                    cell
                    for cell in row_cells
                    if (
                        cell.column < grade_left
                        and cell.text_excerpt.strip()
                    )
                ),
                key=lambda cell: (cell.column, cell.target_id),
            )
        )
        if not labels:
            continue
        logical_targets = _logical_targets(row_cells, headers)
        physical_targets = _unique_physical_targets(logical_targets)
        if len(physical_targets) == len(GRADE_NAMES):
            rating_mode = "five_grade"
            five_grade_ids: Optional[Tuple[str, ...]] = tuple(
                target.cell.target_id for target in logical_targets
            )
        elif (
            len(physical_targets) == 3
            and logical_targets[1].cell.target_id
            == logical_targets[2].cell.target_id
            == logical_targets[3].cell.target_id
            and logical_targets[0].cell.target_id
            != logical_targets[4].cell.target_id
        ):
            rating_mode = "merged_not_five_grade"
            five_grade_ids = None
        else:
            raise HwpxEvaluationGridError(
                "evaluation_row_merge_pattern_unknown",
                "an evaluation row has an unaudited merged-cell pattern",
            )
        marker_targets = tuple(
            target
            for target in physical_targets
            if target.expected_text == "■"
        )
        if len(marker_targets) > 1:
            raise HwpxEvaluationGridError(
                "multiple_historical_markers_in_row",
                "an evaluation row contains multiple historical markers",
            )
        marker_grade: Optional[str] = None
        if marker_targets:
            marker_id = marker_targets[0].target_id
            logical_grades = tuple(
                target.grade
                for target in logical_targets
                if target.cell.target_id == marker_id
            )
            if len(logical_grades) == 1:
                marker_grade = logical_grades[0]
            elif rating_mode == "merged_not_five_grade":
                marker_grade = "merged_middle"
            else:
                raise HwpxEvaluationGridError(
                    "historical_marker_grade_ambiguous",
                    "a historical marker cannot be mapped to its visual grade",
                )
        label = " / ".join(
            cell.text_excerpt.strip() for cell in labels
        )
        rows.append(
            EvaluationRowMap(
                row_order=len(rows),
                row=row,
                label=label,
                label_sha256=_text_sha256(label),
                label_target_ids=tuple(
                    cell.target_id for cell in labels
                ),
                rating_mode=rating_mode,
                logical_grade_targets=logical_targets,
                five_grade_target_ids=five_grade_ids,
                neutralization_targets=physical_targets,
                existing_marker_targets=marker_targets,
                existing_marker_grade=marker_grade,
            )
        )
    if len(rows) != 27:
        raise HwpxEvaluationGridError(
            "evaluation_row_count_invalid",
            "the audited inspection table must expose exactly 27 rows",
        )
    if sum(row.rating_mode == "five_grade" for row in rows) != 25:
        raise HwpxEvaluationGridError(
            "five_grade_row_count_invalid",
            "the audited inspection table must expose 25 five-grade rows",
        )
    merged_rows = tuple(
        row for row in rows if row.rating_mode != "five_grade"
    )
    if (
        len(merged_rows) != 2
        or tuple(row.row for row in merged_rows) != (21, 27)
    ):
        raise HwpxEvaluationGridError(
            "merged_evaluation_row_contract_invalid",
            "the audited merged rows must remain rows 21 and 27",
        )
    return tuple(rows)


def map_sample_evaluation_grid(
    inspection: SampleLayoutInspection,
) -> HwpxEvaluationGridAudit:
    """Map one already-inspected exact sample to deterministic cell targets."""

    if not isinstance(inspection, SampleLayoutInspection):
        raise HwpxEvaluationGridError(
            "sample_layout_inspection_required",
            "an exact audited sample inspection is required",
        )
    tables = []
    all_marker_ids = set()
    for pair in range(inspection.baseline.page_pair_count):
        table_index = pair * 3 + 1
        try:
            table = inspection.snapshot.tables[table_index]
        except IndexError as error:
            raise HwpxEvaluationGridError(
                "inspection_table_sequence_invalid",
                "the exact sample inspection-table sequence is incomplete",
            ) from error
        if table.table_index != table_index:
            raise HwpxEvaluationGridError(
                "inspection_table_sequence_invalid",
                "the exact sample inspection-table index has drifted",
            )
        headers = _grade_headers(table)
        roles = _inspection_roles(table)
        rows = _evaluation_rows(table, headers, roles)
        markers = tuple(
            sorted(
                (
                    marker
                    for row in rows
                    for marker in row.existing_marker_targets
                ),
                key=lambda cell: (
                    cell.row,
                    cell.column,
                    cell.target_id,
                ),
            )
        )
        marker_ids = {marker.target_id for marker in markers}
        table_square_ids = {
            cell.target_id
            for cell in table.cells
            if cell.text_excerpt == "■"
        }
        if marker_ids != table_square_ids:
            raise HwpxEvaluationGridError(
                "historical_marker_coverage_incomplete",
                "not every historical marker is bound to an evaluation row",
            )
        if all_marker_ids.intersection(marker_ids):
            raise HwpxEvaluationGridError(
                "historical_marker_target_reused",
                "a historical marker target is reused across tables",
            )
        all_marker_ids.update(marker_ids)
        tables.append(
            InspectionEvaluationTableMap(
                page_pair_index=pair,
                inspection_page_number=pair * 2 + 1,
                table_id=table.table_id,
                table_index=table_index,
                header_anchor_columns=tuple(
                    cell.column + cell.column_span - 1
                    for cell in headers
                ),
                role_cells=roles,
                evaluation_rows=rows,
                existing_marker_targets=markers,
            )
        )
    if len(tables) != 24:
        raise HwpxEvaluationGridError(
            "inspection_table_count_invalid",
            "the exact sample must expose 24 inspection tables",
        )

    payload = {
        "schema_version": HWPX_EVALUATION_GRID_SCHEMA,
        "template_id": inspection.baseline.template_id,
        "quarter": inspection.baseline.quarter,
        "document_snapshot_id": inspection.document_snapshot_id,
        "document_sha256": inspection.snapshot.document_sha256,
        "structure_target_summary_sha256": (
            inspection.snapshot.target_summary_sha256
        ),
        "inspection_tables": [asdict(table) for table in tables],
    }
    row_count = sum(len(table.evaluation_rows) for table in tables)
    five_grade_count = sum(
        row.rating_mode == "five_grade"
        for table in tables
        for row in table.evaluation_rows
    )
    marker_count = sum(
        len(table.existing_marker_targets) for table in tables
    )
    return HwpxEvaluationGridAudit(
        schema_version=HWPX_EVALUATION_GRID_SCHEMA,
        template_id=inspection.baseline.template_id,
        quarter=inspection.baseline.quarter,
        document_snapshot_id=inspection.document_snapshot_id,
        document_sha256=inspection.snapshot.document_sha256,
        structure_target_summary_sha256=(
            inspection.snapshot.target_summary_sha256
        ),
        inspection_table_count=len(tables),
        evaluation_row_count=row_count,
        five_grade_row_count=five_grade_count,
        merged_row_count=row_count - five_grade_count,
        existing_marker_count=marker_count,
        inspection_tables=tuple(tables),
        audit_sha256=_canonical_sha256(payload),
    )


def inspect_sample_evaluation_grid(
    source_path: Union[str, Path],
) -> HwpxEvaluationGridAudit:
    """Inspect and map one pinned 3Q/4Q sample HWPX read-only."""

    return map_sample_evaluation_grid(
        inspect_sample_layout(source_path)
    )


def _parse_rating_key(key: Any) -> Tuple[int, int]:
    if (
        not isinstance(key, tuple)
        or len(key) != 2
        or isinstance(key[0], bool)
        or isinstance(key[1], bool)
        or not isinstance(key[0], int)
        or not isinstance(key[1], int)
    ):
        raise HwpxEvaluationGridError(
            "explicit_rating_key_invalid",
            "an explicit rating key must be (page_pair_index, row)",
        )
    return key


def build_current_rating_mutation_plan(
    audit: HwpxEvaluationGridAudit,
    explicit_current_ratings: Mapping[Tuple[int, int], str],
) -> CurrentRatingMutationPlan:
    """Clear stale markers and set only explicit five-grade current ratings.

    ``explicit_current_ratings`` must already be server-validated current truth.
    Merged rows are deliberately rejected for selection while their historical
    markers are still cleared.  No duplicate target operation is emitted.
    """

    if not isinstance(audit, HwpxEvaluationGridAudit):
        raise HwpxEvaluationGridError(
            "evaluation_grid_audit_required",
            "an exact evaluation-grid audit is required",
        )
    if not isinstance(explicit_current_ratings, Mapping):
        raise HwpxEvaluationGridError(
            "explicit_ratings_invalid",
            "explicit current ratings must be a mapping",
        )
    rows: Dict[Tuple[int, int], EvaluationRowMap] = {
        (table.page_pair_index, row.row): row
        for table in audit.inspection_tables
        for row in table.evaluation_rows
    }
    table_by_pair = {
        table.page_pair_index: table
        for table in audit.inspection_tables
    }
    parsed: Dict[Tuple[int, int], str] = {}
    for raw_key, grade in explicit_current_ratings.items():
        key = _parse_rating_key(raw_key)
        row = rows.get(key)
        if row is None:
            raise HwpxEvaluationGridError(
                "explicit_rating_row_unknown",
                "an explicit rating does not resolve to an audited row",
            )
        if not isinstance(grade, str) or grade not in GRADE_NAMES:
            raise HwpxEvaluationGridError(
                "explicit_rating_grade_invalid",
                "an explicit current rating uses an unsupported grade",
            )
        if row.rating_mode != "five_grade":
            raise HwpxEvaluationGridError(
                "merged_rating_requires_explicit_binary_contract",
                "a merged row cannot be forced into the five-grade contract",
            )
        parsed[key] = grade

    mutations: Dict[str, RatingMarkerMutation] = {}
    for table in audit.inspection_tables:
        for row in table.evaluation_rows:
            for marker in row.existing_marker_targets:
                mutations[marker.target_id] = RatingMarkerMutation(
                    target_id=marker.target_id,
                    page_pair_index=table.page_pair_index,
                    inspection_page_number=(
                        table.inspection_page_number
                    ),
                    table_index=table.table_index,
                    row=marker.row,
                    column=marker.column,
                    expected_text=marker.expected_text,
                    expected_text_sha256=(
                        marker.expected_text_sha256
                    ),
                    value="",
                    action="clear_historical_marker",
                    explicit_grade=None,
                )

    for key in sorted(parsed):
        pair, row_number = key
        grade = parsed[key]
        row = rows[key]
        table = table_by_pair[pair]
        logical = next(
            target
            for target in row.logical_grade_targets
            if target.grade == grade
        )
        cell = logical.cell
        prior = mutations.get(cell.target_id)
        action = (
            "retain_as_explicit_current_rating"
            if prior is not None and prior.expected_text == "■"
            else "set_explicit_current_rating"
        )
        mutations[cell.target_id] = RatingMarkerMutation(
            target_id=cell.target_id,
            page_pair_index=pair,
            inspection_page_number=table.inspection_page_number,
            table_index=table.table_index,
            row=row_number,
            column=cell.column,
            expected_text=cell.expected_text,
            expected_text_sha256=cell.expected_text_sha256,
            value="■",
            action=action,
            explicit_grade=grade,
        )

    ordered = tuple(
        sorted(
            mutations.values(),
            key=lambda item: (
                item.page_pair_index,
                item.row,
                item.column,
                item.target_id,
            ),
        )
    )
    historical_marker_ids = {
        marker.target_id
        for table in audit.inspection_tables
        for marker in table.existing_marker_targets
    }
    covered_marker_ids = {
        mutation.target_id
        for mutation in ordered
        if mutation.target_id in historical_marker_ids
    }
    if covered_marker_ids != historical_marker_ids:
        raise HwpxEvaluationGridError(
            "historical_marker_coverage_incomplete",
            "the mutation plan does not cover every historical marker",
        )
    payload = {
        "schema_version": HWPX_CURRENT_RATING_PLAN_SCHEMA,
        "document_snapshot_id": audit.document_snapshot_id,
        "document_sha256": audit.document_sha256,
        "evaluation_grid_sha256": audit.audit_sha256,
        "explicit_current_ratings": [
            {
                "page_pair_index": key[0],
                "row": key[1],
                "grade": parsed[key],
            }
            for key in sorted(parsed)
        ],
        "mutations": [asdict(mutation) for mutation in ordered],
    }
    return CurrentRatingMutationPlan(
        schema_version=HWPX_CURRENT_RATING_PLAN_SCHEMA,
        document_snapshot_id=audit.document_snapshot_id,
        document_sha256=audit.document_sha256,
        evaluation_grid_sha256=audit.audit_sha256,
        explicit_rating_count=len(parsed),
        historical_marker_count=len(historical_marker_ids),
        cleared_historical_marker_count=sum(
            mutation.value == ""
            and mutation.target_id in historical_marker_ids
            for mutation in ordered
        ),
        selected_marker_count=sum(
            mutation.value == "■" for mutation in ordered
        ),
        mutation_count=len(ordered),
        mutations=ordered,
        plan_sha256=_canonical_sha256(payload),
    )


__all__ = [
    "GRADE_LABELS",
    "GRADE_NAMES",
    "HWPX_CURRENT_RATING_PLAN_SCHEMA",
    "HWPX_EVALUATION_GRID_SCHEMA",
    "CurrentRatingMutationPlan",
    "EvaluationCellTarget",
    "EvaluationRowMap",
    "HwpxEvaluationGridAudit",
    "HwpxEvaluationGridError",
    "InspectionEvaluationTableMap",
    "InspectionRoleCells",
    "LogicalGradeTarget",
    "RatingMarkerMutation",
    "build_current_rating_mutation_plan",
    "inspect_sample_evaluation_grid",
    "map_sample_evaluation_grid",
]
