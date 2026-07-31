"""Read-only layout attestation for the exact 3Q/4Q sample reports.

The two TrafficSuperVision sample HWPX files have no named fields or bookmarks.
Their stable editing surface is therefore the exact HWPML table/cell/picture
target graph, not a renderer-supplied list of convenient target names.

This module provides a deliberately preview-only safety slice:

* recognize only the byte-exact audited sample documents;
* verify their complete structure snapshot against a pinned baseline;
* map the 72-table repeating structure to 24 inspection/photo page pairs;
* validate every DocumentEditPlan target against that server-owned graph;
* keep output apply blocked until an exact-target writer and a 48-page
  post-save renderer/validator are both implemented.

It never invokes COM, writes a document, extracts the HWPX archive, or claims
that explicit HWPML page breaks are rendered page numbers.  In particular,
the 4Q sample contains 46 explicit breaks even though its audited PDF has 48
pages; the page-pair mapping below is bound to the exact three-table repeating
contract confirmed by the 48-page visual audit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple, Union

from .contracts import DocumentEditPlan
from .hwpx_structure import (
    CellSnapshot,
    HwpxStructureSnapshot,
    PictureSnapshot,
    inspect_hwpx_structure,
)


HWPX_SAMPLE_LAYOUT_SCHEMA = "trafficvisson.hwpx-sample-layout.v2"
HWPX_SAMPLE_LAYOUT_PREVIEW_SCHEMA = (
    "trafficvisson.hwpx-sample-layout-preview.v1"
)
SAMPLE_REFERENCE_BINDING_REVISION = (
    "audited-photo-caption-evidence-group-v2"
)
SAMPLE_LAYOUT_BINDING_REVISION = 2
EXPECTED_RENDERED_PAGE_COUNT = 48
EXPECTED_PAGE_PAIR_COUNT = 24
EXPECTED_TABLE_COUNT = 72


class HwpxSampleLayoutError(RuntimeError):
    """Caller-safe layout validation failure raised before COM."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SampleLayoutBaseline:
    template_id: str
    quarter: int
    expected_document_name: str
    document_sha256: str
    target_summary_sha256: str
    pdf_sha256: str
    weak_case_index_manifest_sha256: str
    section_count: int
    table_count: int
    cell_count: int
    picture_count: int
    explicit_page_break_count: int
    fixed_inspection_picture_count: int
    existing_photo_picture_count: int
    rendered_page_count: int = EXPECTED_RENDERED_PAGE_COUNT
    page_pair_count: int = EXPECTED_PAGE_PAIR_COUNT
    page_pattern: str = "odd_inspection_even_photo"
    page_mapping: str = "audited_three_table_pair_contract"


SAMPLE_LAYOUT_BASELINES: Tuple[SampleLayoutBaseline, ...] = (
    SampleLayoutBaseline(
        template_id="sample-3q-v1",
        quarter=3,
        expected_document_name="3분기 점검 내용 정리.hwpx",
        document_sha256=(
            "24ffb4bc62cd86a6c72d3a3829b8f1ddf1c824d7bf42452e7c495877a227bcb9"
        ),
        target_summary_sha256=(
            "88238874c4508480d620493112a2b46fac96c311999f57f3df8f9582a5e1a7f5"
        ),
        pdf_sha256=(
            "9a4b3f093b9fd8c320c55b1b80ac29a359a32c458f814ef6f3bc1cb2763c1e89"
        ),
        weak_case_index_manifest_sha256=(
            "4d06ede9c320090c1aec107eb9c41877ea2b9d924e2d3724821dbe42f520694f"
        ),
        section_count=1,
        table_count=72,
        cell_count=6_353,
        picture_count=147,
        explicit_page_break_count=47,
        fixed_inspection_picture_count=24,
        existing_photo_picture_count=123,
    ),
    SampleLayoutBaseline(
        template_id="sample-4q-v1",
        quarter=4,
        expected_document_name="4분기 점검 내용 정리.hwpx",
        document_sha256=(
            "bbdd61a6ef192a957a5e9480b0c2eedbfe956dd184abd5d76f59474254941539"
        ),
        target_summary_sha256=(
            "582d835ccf71aaf04f9a8ee42dbc2e5c508c9cde4b21bc8d9ed175d789ec8f4a"
        ),
        pdf_sha256=(
            "0b705c9f1961f0ca5bde1cb34cffc4f21893ab024a9aee001fe4c37fa2267856"
        ),
        weak_case_index_manifest_sha256=(
            "31f3e04d5fecf64078afebf4bc21312d701dadc3eac5f2302a4bb4b236247041"
        ),
        section_count=1,
        table_count=72,
        cell_count=6_456,
        picture_count=173,
        explicit_page_break_count=46,
        fixed_inspection_picture_count=24,
        existing_photo_picture_count=149,
    ),
)

_BASELINES_BY_SHA = {
    baseline.document_sha256: baseline
    for baseline in SAMPLE_LAYOUT_BASELINES
}


@dataclass(frozen=True)
class SampleLayoutCounts:
    rendered_page_count_baseline: int
    page_pair_count: int
    section_count: int
    table_count: int
    cell_count: int
    picture_count: int
    fixed_inspection_picture_count: int
    existing_photo_picture_count: int
    explicit_page_break_count: int


@dataclass(frozen=True)
class SampleOperationBinding:
    operation_id: str
    operation_type: str
    target_id: str
    target_kind: str
    table_index: int
    page_number: int
    page_kind: str
    layout_role: str
    cell_row: Optional[int]
    cell_column: Optional[int]
    source_ref: str


@dataclass(frozen=True)
class SampleSquareMarkerBinding:
    cell_target_id: str
    table_id: str
    table_index: int
    page_number: int
    row: int
    column: int
    marker: str
    text_sha256: str


@dataclass(frozen=True)
class SampleSemanticCellBinding:
    role: str
    cell_target_id: str
    table_id: str
    table_index: int
    page_number: int
    row: int
    column: int
    row_span: int
    column_span: int
    text_excerpt: str
    text_sha256: str


@dataclass(frozen=True)
class SampleReferenceCaseBinding:
    sample_layout_binding_schema: str
    sample_layout_binding_revision: int
    semantic_projection_revision: str
    picture_target_id: str
    picture_index: int
    page_pair_index: int
    inspection_page_number: int
    photo_page_number: int
    photo_table_index: int
    photo_cell_target_id: str
    photo_cell_row: int
    photo_cell_column: int
    photo_slot_id: str
    sample_evidence_group_id: str
    group_photo_index: int
    group_photo_count: int
    photo_caption: SampleSemanticCellBinding
    inspection_route: SampleSemanticCellBinding
    inspection_opinion_label: SampleSemanticCellBinding
    inspection_opinion_body: SampleSemanticCellBinding
    square_markers: Tuple[SampleSquareMarkerBinding, ...]
    binding_sha256: str


@dataclass(frozen=True)
class SampleLayoutInspection:
    baseline: SampleLayoutBaseline
    snapshot: HwpxStructureSnapshot
    document_snapshot_id: str
    baseline_manifest_sha256: str
    counts: SampleLayoutCounts


@dataclass(frozen=True)
class SampleLayoutPlanPreview:
    schema_version: str
    state: str
    template_id: str
    quarter: int
    document_snapshot_id: str
    document_sha256: str
    structure_target_summary_sha256: str
    baseline_manifest_sha256: str
    baseline_pdf_sha256: str
    weak_case_index_manifest_sha256: str
    plan_sha256: str
    layout_contract_sha256: str
    expected_rendered_page_count: int
    output_rendered_page_count: Optional[int]
    output_render_validation_status: str
    page_pattern: str
    page_mapping: str
    counts: SampleLayoutCounts
    operation_bindings: Tuple[SampleOperationBinding, ...]
    apply_eligible: bool
    blocking_reasons: Tuple[str, ...]
    source_renamed: bool
    read_only: bool = True
    no_com: bool = True
    no_document_write: bool = True

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _baseline_manifest_sha256(
    baseline: SampleLayoutBaseline,
) -> str:
    return _canonical_sha256(
        {
            "schema_version": HWPX_SAMPLE_LAYOUT_SCHEMA,
            **asdict(baseline),
        }
    )


def is_protected_sample_document_sha256(value: str) -> bool:
    return isinstance(value, str) and value in _BASELINES_BY_SHA


def sample_document_snapshot_id(
    snapshot: HwpxStructureSnapshot,
) -> str:
    baseline = _BASELINES_BY_SHA.get(snapshot.document_sha256)
    if baseline is None:
        raise HwpxSampleLayoutError(
            "sample_template_not_recognized",
            "감사된 3·4분기 HWPX 양식이 아닙니다.",
        )
    return (
        f"hwpx-sample-{baseline.quarter}q-"
        f"{snapshot.target_summary_sha256[:24]}"
    )


def identify_sample_layout(
    source_path: Union[str, Path],
) -> Optional[SampleLayoutInspection]:
    """Return a verified exact-sample inspection, or None for another HWPX."""

    snapshot = inspect_hwpx_structure(source_path)
    baseline = _BASELINES_BY_SHA.get(snapshot.document_sha256)
    if baseline is None:
        return None
    _validate_exact_layout(snapshot, baseline)
    return SampleLayoutInspection(
        baseline=baseline,
        snapshot=snapshot,
        document_snapshot_id=sample_document_snapshot_id(snapshot),
        baseline_manifest_sha256=_baseline_manifest_sha256(baseline),
        counts=_layout_counts(snapshot, baseline),
    )


def inspect_sample_layout(
    source_path: Union[str, Path],
) -> SampleLayoutInspection:
    inspection = identify_sample_layout(source_path)
    if inspection is None:
        raise HwpxSampleLayoutError(
            "sample_template_not_recognized",
            "감사된 3·4분기 HWPX 양식이 아닙니다.",
        )
    return inspection


def _normalized_role_text(value: str) -> str:
    return " ".join(value.replace("：", ":").split()).replace(
        " :", ":"
    )


def _semantic_cell_binding(
    cell: CellSnapshot,
    *,
    table,
    page_number: int,
    role: str,
) -> SampleSemanticCellBinding:
    return SampleSemanticCellBinding(
        role=role,
        cell_target_id=cell.target_id,
        table_id=table.table_id,
        table_index=table.table_index,
        page_number=page_number,
        row=cell.row,
        column=cell.column,
        row_span=cell.row_span,
        column_span=cell.column_span,
        text_excerpt=cell.text_excerpt,
        text_sha256=cell.text_sha256,
    )


def bind_sample_reference_case(
    inspection: SampleLayoutInspection,
    picture_target_id: str,
) -> SampleReferenceCaseBinding:
    """Bind one existing sample photo to its exact page pair and markers.

    The returned binding is read-only provenance for reference-case approval.
    It does not authorize the existing photo slot or any marker cell to be
    edited.
    """

    picture = next(
        (
            candidate
            for candidate in inspection.snapshot.pictures
            if candidate.target_id == picture_target_id
        ),
        None,
    )
    if (
        picture is None
        or picture.nearest_table_index is None
        or picture.nearest_cell_target_id is None
        or picture.nearest_cell_row is None
        or picture.nearest_cell_column is None
    ):
        raise HwpxSampleLayoutError(
            "sample_reference_picture_unresolved",
            "표본 사례 사진을 정확한 표·셀 슬롯에 결속하지 못했습니다.",
        )
    photo_page_number, page_kind, _ = _page_for_table(
        picture.nearest_table_index
    )
    if page_kind != "photo":
        raise HwpxSampleLayoutError(
            "sample_reference_picture_not_photo_evidence",
            "점검표의 고정 그림은 승인 사례 사진으로 승격할 수 없습니다.",
        )
    page_pair_index = picture.nearest_table_index // 3
    inspection_table_index = page_pair_index * 3 + 1
    inspection_page_number = page_pair_index * 2 + 1
    inspection_table = inspection.snapshot.tables[
        inspection_table_index
    ]
    photo_table = inspection.snapshot.tables[
        picture.nearest_table_index
    ]
    caption_cells = tuple(
        cell
        for cell in photo_table.cells
        if (
            cell.row == picture.nearest_cell_row + 1
            and cell.column <= picture.nearest_cell_column
            < cell.column + cell.column_span
        )
    )
    if len(caption_cells) != 1:
        raise HwpxSampleLayoutError(
            "sample_photo_caption_mapping_invalid",
            "표본 사례 사진과 같은 열의 캡션 셀을 하나로 결속하지 못했습니다.",
        )
    route_cells = tuple(
        cell
        for cell in inspection_table.cells
        if _normalized_role_text(cell.text_excerpt).startswith(
            "노선명:"
        )
    )
    if len(route_cells) != 1:
        raise HwpxSampleLayoutError(
            "sample_route_cell_mapping_invalid",
            "표본 점검표의 노선명 셀을 하나로 결속하지 못했습니다.",
        )
    opinion_labels = tuple(
        cell
        for cell in inspection_table.cells
        if _normalized_role_text(cell.text_excerpt) == "점검의견"
    )
    if len(opinion_labels) != 1:
        raise HwpxSampleLayoutError(
            "sample_opinion_label_mapping_invalid",
            "표본 점검표의 점검의견 라벨을 하나로 결속하지 못했습니다.",
        )
    opinion_label_cell = opinion_labels[0]
    opinion_bodies = tuple(
        cell
        for cell in inspection_table.cells
        if (
            cell.row == opinion_label_cell.row + 1
            and cell.column == opinion_label_cell.column
        )
    )
    if len(opinion_bodies) != 1:
        raise HwpxSampleLayoutError(
            "sample_opinion_body_mapping_invalid",
            "표본 점검표의 점검의견 본문 셀을 하나로 결속하지 못했습니다.",
        )
    photo_caption = _semantic_cell_binding(
        caption_cells[0],
        table=photo_table,
        page_number=photo_page_number,
        role="photo_caption",
    )
    inspection_route = _semantic_cell_binding(
        route_cells[0],
        table=inspection_table,
        page_number=inspection_page_number,
        role="inspection_route",
    )
    inspection_opinion_label = _semantic_cell_binding(
        opinion_label_cell,
        table=inspection_table,
        page_number=inspection_page_number,
        role="inspection_opinion_label",
    )
    inspection_opinion_body = _semantic_cell_binding(
        opinion_bodies[0],
        table=inspection_table,
        page_number=inspection_page_number,
        role="inspection_opinion_body",
    )
    marker_cells = tuple(
        sorted(
            (
                cell
                for cell in inspection_table.cells
                if cell.text_excerpt == "■"
            ),
            key=lambda cell: (
                cell.row,
                cell.column,
                cell.target_id,
            ),
        )
    )
    if not marker_cells:
        raise HwpxSampleLayoutError(
            "sample_square_marker_mapping_missing",
            "표본 사례와 같은 점검표 쪽의 네모 표시 위치를 찾지 못했습니다.",
        )
    marker_bindings = tuple(
        SampleSquareMarkerBinding(
            cell_target_id=cell.target_id,
            table_id=inspection_table.table_id,
            table_index=inspection_table_index,
            page_number=inspection_page_number,
            row=cell.row,
            column=cell.column,
            marker="■",
            text_sha256=cell.text_sha256,
        )
        for cell in marker_cells
    )
    group_pictures = tuple(
        sorted(
            (
                candidate
                for candidate in inspection.snapshot.pictures
                if (
                    candidate.section_path == picture.section_path
                    and candidate.section_index == picture.section_index
                    and candidate.nearest_table_index
                    == picture.nearest_table_index
                    and candidate.nearest_cell_target_id
                    == picture.nearest_cell_target_id
                    and candidate.nearest_cell_row
                    == picture.nearest_cell_row
                    and candidate.nearest_cell_column
                    == picture.nearest_cell_column
                )
            ),
            key=lambda candidate: (
                candidate.picture_index,
                candidate.target_id,
            ),
        )
    )
    try:
        group_photo_index = (
            next(
                index
                for index, candidate in enumerate(
                    group_pictures,
                    start=1,
                )
                if candidate.target_id == picture.target_id
            )
        )
    except StopIteration as error:
        raise HwpxSampleLayoutError(
            "sample_evidence_group_mapping_invalid",
            "표본 사진을 같은 캡션 셀의 근거 묶음에 결속하지 못했습니다.",
        ) from error
    group_photo_count = len(group_pictures)
    group_payload: Mapping[str, object] = {
        "sample_layout_binding_schema": HWPX_SAMPLE_LAYOUT_SCHEMA,
        "sample_layout_binding_revision": (
            SAMPLE_LAYOUT_BINDING_REVISION
        ),
        "semantic_projection_revision": (
            SAMPLE_REFERENCE_BINDING_REVISION
        ),
        "document_sha256": inspection.snapshot.document_sha256,
        "section_path": picture.section_path,
        "section_index": picture.section_index,
        "photo_table_id": photo_table.table_id,
        "photo_table_index": picture.nearest_table_index,
        "photo_cell_target_id": picture.nearest_cell_target_id,
        "photo_cell_row": picture.nearest_cell_row,
        "photo_cell_column": picture.nearest_cell_column,
        "caption_cell_target_id": photo_caption.cell_target_id,
        "caption_text_sha256": photo_caption.text_sha256,
    }
    sample_evidence_group_id = (
        "sample-evidence-group-"
        + _canonical_sha256(group_payload)[:24]
    )
    slot_payload: Mapping[str, object] = {
        "sample_layout_binding_schema": HWPX_SAMPLE_LAYOUT_SCHEMA,
        "sample_layout_binding_revision": (
            SAMPLE_LAYOUT_BINDING_REVISION
        ),
        "semantic_projection_revision": (
            SAMPLE_REFERENCE_BINDING_REVISION
        ),
        "document_sha256": inspection.snapshot.document_sha256,
        "picture_target_id": picture.target_id,
        "photo_table_index": picture.nearest_table_index,
        "photo_cell_target_id": picture.nearest_cell_target_id,
        "photo_cell_row": picture.nearest_cell_row,
        "photo_cell_column": picture.nearest_cell_column,
        "photo_caption": asdict(photo_caption),
        "inspection_route": asdict(inspection_route),
        "inspection_opinion_label": asdict(
            inspection_opinion_label
        ),
        "inspection_opinion_body": asdict(
            inspection_opinion_body
        ),
        "sample_evidence_group_id": sample_evidence_group_id,
        "group_photo_index": group_photo_index,
        "group_photo_count": group_photo_count,
    }
    photo_slot_id = (
        "sample-photo-slot-" + _canonical_sha256(slot_payload)[:24]
    )
    binding_payload: Mapping[str, object] = {
        **slot_payload,
        "photo_slot_id": photo_slot_id,
        "page_pair_index": page_pair_index,
        "inspection_page_number": inspection_page_number,
        "photo_page_number": photo_page_number,
        "square_markers": [
            asdict(marker) for marker in marker_bindings
        ],
    }
    return SampleReferenceCaseBinding(
        sample_layout_binding_schema=HWPX_SAMPLE_LAYOUT_SCHEMA,
        sample_layout_binding_revision=(
            SAMPLE_LAYOUT_BINDING_REVISION
        ),
        semantic_projection_revision=(
            SAMPLE_REFERENCE_BINDING_REVISION
        ),
        picture_target_id=picture.target_id,
        picture_index=picture.picture_index,
        page_pair_index=page_pair_index,
        inspection_page_number=inspection_page_number,
        photo_page_number=photo_page_number,
        photo_table_index=picture.nearest_table_index,
        photo_cell_target_id=picture.nearest_cell_target_id,
        photo_cell_row=picture.nearest_cell_row,
        photo_cell_column=picture.nearest_cell_column,
        photo_slot_id=photo_slot_id,
        sample_evidence_group_id=sample_evidence_group_id,
        group_photo_index=group_photo_index,
        group_photo_count=group_photo_count,
        photo_caption=photo_caption,
        inspection_route=inspection_route,
        inspection_opinion_label=inspection_opinion_label,
        inspection_opinion_body=inspection_opinion_body,
        square_markers=marker_bindings,
        binding_sha256=_canonical_sha256(binding_payload),
    )


def preview_sample_layout_plan(
    source_path: Union[str, Path],
    plan: DocumentEditPlan,
) -> SampleLayoutPlanPreview:
    """Validate one model plan without COM and without granting apply."""

    inspection = inspect_sample_layout(source_path)
    snapshot = inspection.snapshot
    baseline = inspection.baseline
    if plan.document_snapshot_id != inspection.document_snapshot_id:
        raise HwpxSampleLayoutError(
            "document_snapshot_mismatch",
            "편집 계획이 현재 감사 양식의 구조 스냅샷과 일치하지 않습니다.",
        )

    cells: Dict[str, Tuple[CellSnapshot, int]] = {}
    for table in snapshot.tables:
        for cell in table.cells:
            cells[cell.target_id] = (cell, table.table_index)
    pictures: Dict[str, PictureSnapshot] = {
        picture.target_id: picture for picture in snapshot.pictures
    }
    seen_targets = set()
    bindings = []
    for operation in plan.operations:
        if operation.target in seen_targets:
            raise HwpxSampleLayoutError(
                "duplicate_plan_target",
                "한 편집 계획에서 같은 HWPX 위치를 두 번 수정할 수 없습니다.",
            )
        seen_targets.add(operation.target)
        if operation.style_policy != "preserve":
            raise HwpxSampleLayoutError(
                "sample_style_must_be_preserved",
                "3·4분기 양식 작업은 기존 서식을 보존해야 합니다.",
            )
        if operation.type == "add_table_row":
            raise HwpxSampleLayoutError(
                "table_growth_not_allowed",
                "48쪽 고정 양식에는 표 행을 자동 추가할 수 없습니다.",
            )
        if operation.type == "fill_text":
            raise HwpxSampleLayoutError(
                "named_text_target_unavailable",
                "표본 양식에는 이름 필드와 책갈피가 없어 fill_text를 사용할 수 없습니다.",
            )

        if operation.type == "fill_cell":
            target = cells.get(operation.target)
            if target is None:
                raise HwpxSampleLayoutError(
                    "cell_target_not_found",
                    "편집 계획의 표 셀이 현재 감사 양식에 존재하지 않습니다.",
                )
            cell, table_index = target
            page_number, page_kind, table_role = _page_for_table(
                table_index
            )
            bindings.append(
                SampleOperationBinding(
                    operation_id=str(operation.operation_id),
                    operation_type=operation.type,
                    target_id=operation.target,
                    target_kind="cell",
                    table_index=table_index,
                    page_number=page_number,
                    page_kind=page_kind,
                    layout_role=f"{table_role}_cell_unclassified",
                    cell_row=cell.row,
                    cell_column=cell.column,
                    source_ref=operation.source_ref,
                )
            )
            continue

        if operation.type not in {"insert_image", "insert_map"}:
            raise HwpxSampleLayoutError(
                "operation_type_not_supported",
                "표본 양식에서 검증할 수 없는 편집 작업입니다.",
            )
        picture = pictures.get(operation.target)
        if picture is None or picture.nearest_table_index is None:
            raise HwpxSampleLayoutError(
                "picture_target_not_found",
                "편집 계획의 사진 슬롯이 현재 감사 양식에 존재하지 않습니다.",
            )
        page_number, page_kind, table_role = _page_for_table(
            picture.nearest_table_index
        )
        if page_kind != "photo":
            raise HwpxSampleLayoutError(
                "fixed_inspection_picture_is_immutable",
                "점검표 쪽의 고정 그림은 사건 사진 슬롯으로 사용할 수 없습니다.",
            )
        bindings.append(
            SampleOperationBinding(
                operation_id=str(operation.operation_id),
                operation_type=operation.type,
                target_id=operation.target,
                target_kind="picture",
                table_index=picture.nearest_table_index,
                page_number=page_number,
                page_kind=page_kind,
                layout_role=f"{table_role}_existing_picture",
                cell_row=picture.nearest_cell_row,
                cell_column=picture.nearest_cell_column,
                source_ref=operation.source_ref,
            )
        )

    plan_sha256 = _canonical_sha256(plan.model_dump(mode="json"))
    contract_payload: Mapping[str, object] = {
        "schema_version": HWPX_SAMPLE_LAYOUT_PREVIEW_SCHEMA,
        "document_sha256": snapshot.document_sha256,
        "target_summary_sha256": snapshot.target_summary_sha256,
        "baseline_manifest_sha256": inspection.baseline_manifest_sha256,
        "baseline_pdf_sha256": baseline.pdf_sha256,
        "weak_case_index_manifest_sha256": (
            baseline.weak_case_index_manifest_sha256
        ),
        "plan_sha256": plan_sha256,
        "expected_rendered_page_count": baseline.rendered_page_count,
        "page_pattern": baseline.page_pattern,
        "operation_bindings": [asdict(binding) for binding in bindings],
    }
    return SampleLayoutPlanPreview(
        schema_version=HWPX_SAMPLE_LAYOUT_PREVIEW_SCHEMA,
        state="validated_structure_preview_apply_blocked",
        template_id=baseline.template_id,
        quarter=baseline.quarter,
        document_snapshot_id=inspection.document_snapshot_id,
        document_sha256=snapshot.document_sha256,
        structure_target_summary_sha256=(
            snapshot.target_summary_sha256
        ),
        baseline_manifest_sha256=inspection.baseline_manifest_sha256,
        baseline_pdf_sha256=baseline.pdf_sha256,
        weak_case_index_manifest_sha256=(
            baseline.weak_case_index_manifest_sha256
        ),
        plan_sha256=plan_sha256,
        layout_contract_sha256=_canonical_sha256(contract_payload),
        expected_rendered_page_count=baseline.rendered_page_count,
        output_rendered_page_count=None,
        output_render_validation_status="not_run",
        page_pattern=baseline.page_pattern,
        page_mapping=baseline.page_mapping,
        counts=inspection.counts,
        operation_bindings=tuple(bindings),
        apply_eligible=False,
        blocking_reasons=(
            "exact_hwpml_target_writer_not_implemented",
            "semantic_cell_role_map_not_approved",
            "output_reopen_and_48_page_render_validation_not_implemented",
        ),
        source_renamed=(
            snapshot.document_name != baseline.expected_document_name
        ),
    )


def _layout_counts(
    snapshot: HwpxStructureSnapshot,
    baseline: SampleLayoutBaseline,
) -> SampleLayoutCounts:
    fixed = sum(
        picture.nearest_table_index is not None
        and picture.nearest_table_index % 3 == 1
        for picture in snapshot.pictures
    )
    photos = sum(
        picture.nearest_table_index is not None
        and picture.nearest_table_index % 3 == 2
        for picture in snapshot.pictures
    )
    return SampleLayoutCounts(
        rendered_page_count_baseline=baseline.rendered_page_count,
        page_pair_count=baseline.page_pair_count,
        section_count=snapshot.section_count,
        table_count=len(snapshot.tables),
        cell_count=sum(len(table.cells) for table in snapshot.tables),
        picture_count=len(snapshot.pictures),
        fixed_inspection_picture_count=fixed,
        existing_photo_picture_count=photos,
        explicit_page_break_count=len(snapshot.page_breaks),
    )


def _validate_exact_layout(
    snapshot: HwpxStructureSnapshot,
    baseline: SampleLayoutBaseline,
) -> None:
    counts = _layout_counts(snapshot, baseline)
    expected_counts = SampleLayoutCounts(
        rendered_page_count_baseline=baseline.rendered_page_count,
        page_pair_count=baseline.page_pair_count,
        section_count=baseline.section_count,
        table_count=baseline.table_count,
        cell_count=baseline.cell_count,
        picture_count=baseline.picture_count,
        fixed_inspection_picture_count=(
            baseline.fixed_inspection_picture_count
        ),
        existing_photo_picture_count=(
            baseline.existing_photo_picture_count
        ),
        explicit_page_break_count=(
            baseline.explicit_page_break_count
        ),
    )
    if (
        counts != expected_counts
        or snapshot.target_summary_sha256
        != baseline.target_summary_sha256
        or snapshot.field_count != 0
        or snapshot.bookmark_count != 0
    ):
        raise HwpxSampleLayoutError(
            "sample_layout_baseline_mismatch",
            "HWPX 구조가 감사된 48쪽 표본 양식 기준과 일치하지 않습니다.",
        )
    if [table.table_index for table in snapshot.tables] != list(
        range(EXPECTED_TABLE_COUNT)
    ):
        raise HwpxSampleLayoutError(
            "sample_table_sequence_mismatch",
            "HWPX 표 순서가 감사된 24개 페이지 쌍과 일치하지 않습니다.",
        )

    fixed_by_pair = {pair: 0 for pair in range(EXPECTED_PAGE_PAIR_COUNT)}
    for picture in snapshot.pictures:
        table_index = picture.nearest_table_index
        if (
            table_index is None
            or picture.nearest_cell_target_id is None
            or picture.nearest_cell_row is None
            or picture.nearest_cell_column is None
        ):
            raise HwpxSampleLayoutError(
                "sample_picture_slot_unresolved",
                "HWPX 그림을 정확한 표·셀 슬롯에 결속하지 못했습니다.",
            )
        pair = table_index // 3
        role = table_index % 3
        if not 0 <= pair < EXPECTED_PAGE_PAIR_COUNT or role == 0:
            raise HwpxSampleLayoutError(
                "sample_picture_page_role_invalid",
                "HWPX 그림이 감사된 점검표/사진 대지 역할과 일치하지 않습니다.",
            )
        if role == 1:
            fixed_by_pair[pair] += 1
        elif (
            picture.nearest_cell_row not in {0, 2, 4, 6}
            or picture.nearest_cell_column not in {0, 1}
        ):
            raise HwpxSampleLayoutError(
                "sample_photo_grid_mismatch",
                "사진 대지 그림이 좌우 2열 슬롯 격자를 벗어났습니다.",
            )
    if any(count != 1 for count in fixed_by_pair.values()):
        raise HwpxSampleLayoutError(
            "sample_inspection_fixed_picture_mismatch",
            "각 점검표 쪽의 고정 그림 구조가 감사 기준과 다릅니다.",
        )


def _page_for_table(table_index: int) -> Tuple[int, str, str]:
    if not 0 <= table_index < EXPECTED_TABLE_COUNT:
        raise HwpxSampleLayoutError(
            "table_index_outside_sample_layout",
            "편집 대상 표가 48쪽 표본 양식 범위를 벗어났습니다.",
        )
    pair = table_index // 3
    role = table_index % 3
    if role in {0, 1}:
        return 2 * pair + 1, "inspection", (
            "inspection_header" if role == 0 else "inspection_detail"
        )
    return 2 * pair + 2, "photo", "photo_grid"


__all__ = [
    "EXPECTED_PAGE_PAIR_COUNT",
    "EXPECTED_RENDERED_PAGE_COUNT",
    "HWPX_SAMPLE_LAYOUT_PREVIEW_SCHEMA",
    "HWPX_SAMPLE_LAYOUT_SCHEMA",
    "HwpxSampleLayoutError",
    "SAMPLE_REFERENCE_BINDING_REVISION",
    "SAMPLE_LAYOUT_BINDING_REVISION",
    "SAMPLE_LAYOUT_BASELINES",
    "SampleLayoutInspection",
    "SampleLayoutPlanPreview",
    "SampleReferenceCaseBinding",
    "SampleSemanticCellBinding",
    "SampleSquareMarkerBinding",
    "bind_sample_reference_case",
    "identify_sample_layout",
    "inspect_sample_layout",
    "is_protected_sample_document_sha256",
    "preview_sample_layout_plan",
    "sample_document_snapshot_id",
]
