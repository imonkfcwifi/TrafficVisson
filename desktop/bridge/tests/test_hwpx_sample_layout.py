from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.contracts import DocumentEditPlan
from app.hwpx_sample_layout import (
    HwpxSampleLayoutError,
    inspect_sample_layout,
    preview_sample_layout_plan,
)


WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q = (
    WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"
)
SAMPLE_4Q = (
    WORKSPACE / "sample" / "4분기 점검 내용 정리.hwpx"
)


@pytest.fixture(scope="module")
def sample_3q():
    return inspect_sample_layout(SAMPLE_3Q)


@pytest.fixture(scope="module")
def sample_4q():
    return inspect_sample_layout(SAMPLE_4Q)


def _plan(
    snapshot_id: str,
    *,
    operation_type: str,
    target: str,
    style_policy: str = "preserve",
    duplicate: bool = False,
) -> DocumentEditPlan:
    operation = {
        "operation_id": str(uuid4()),
        "type": operation_type,
        "target": target,
        "expected_value": None,
        "value": "검증된 보고서 값",
        "source_ref": "incident:approved-1",
        "style_policy": style_policy,
        "preview": "표본 양식 위치 미리보기",
    }
    operations = [operation]
    if duplicate:
        operations.append(
            {
                **operation,
                "operation_id": str(uuid4()),
                "value": "두 번째 값",
            }
        )
    return DocumentEditPlan.model_validate(
        {
            "schema_version": "1.0",
            "plan_id": str(uuid4()),
            "document_snapshot_id": snapshot_id,
            "operations": operations,
        }
    )


@pytest.mark.parametrize(
    ("fixture_name", "quarter", "cells", "pictures", "breaks", "photos"),
    (
        ("sample_3q", 3, 6_353, 147, 47, 123),
        ("sample_4q", 4, 6_456, 173, 46, 149),
    ),
)
def test_real_samples_match_pinned_48_page_pair_contract(
    request,
    fixture_name,
    quarter,
    cells,
    pictures,
    breaks,
    photos,
):
    inspection = request.getfixturevalue(fixture_name)

    assert inspection.baseline.quarter == quarter
    assert inspection.counts.rendered_page_count_baseline == 48
    assert inspection.counts.page_pair_count == 24
    assert inspection.counts.section_count == 1
    assert inspection.counts.table_count == 72
    assert inspection.counts.cell_count == cells
    assert inspection.counts.picture_count == pictures
    assert inspection.counts.explicit_page_break_count == breaks
    assert inspection.counts.fixed_inspection_picture_count == 24
    assert inspection.counts.existing_photo_picture_count == photos
    assert inspection.snapshot.field_count == 0
    assert inspection.snapshot.bookmark_count == 0
    assert len(inspection.baseline_manifest_sha256) == 64


def test_server_owned_plan_targets_map_to_exact_page_and_slot(
    sample_3q,
):
    inspection_cell = sample_3q.snapshot.tables[0].cells[0]
    cell_plan = _plan(
        sample_3q.document_snapshot_id,
        operation_type="fill_cell",
        target=inspection_cell.target_id,
    )
    cell_preview = preview_sample_layout_plan(
        SAMPLE_3Q, cell_plan
    )

    assert cell_preview.state == (
        "validated_structure_preview_apply_blocked"
    )
    assert cell_preview.apply_eligible is False
    assert cell_preview.output_rendered_page_count is None
    assert cell_preview.output_render_validation_status == "not_run"
    assert cell_preview.expected_rendered_page_count == 48
    assert cell_preview.baseline_pdf_sha256 == (
        sample_3q.baseline.pdf_sha256
    )
    assert cell_preview.weak_case_index_manifest_sha256 == (
        sample_3q.baseline.weak_case_index_manifest_sha256
    )
    assert len(cell_preview.blocking_reasons) == 3
    binding = cell_preview.operation_bindings[0]
    assert binding.target_kind == "cell"
    assert binding.table_index == 0
    assert binding.page_number == 1
    assert binding.page_kind == "inspection"

    last_photo = next(
        picture
        for picture in reversed(sample_3q.snapshot.pictures)
        if picture.nearest_table_index == 71
    )
    photo_plan = _plan(
        sample_3q.document_snapshot_id,
        operation_type="insert_image",
        target=last_photo.target_id,
    )
    photo_preview = preview_sample_layout_plan(
        SAMPLE_3Q, photo_plan
    )
    photo_binding = photo_preview.operation_bindings[0]
    assert photo_binding.target_kind == "picture"
    assert photo_binding.table_index == 71
    assert photo_binding.page_number == 48
    assert photo_binding.page_kind == "photo"
    assert photo_binding.cell_column in {0, 1}


def test_4q_table_pair_mapping_reaches_page_48_despite_46_breaks(
    sample_4q,
):
    last_photo = next(
        picture
        for picture in reversed(sample_4q.snapshot.pictures)
        if picture.nearest_table_index == 71
    )
    plan = _plan(
        sample_4q.document_snapshot_id,
        operation_type="insert_image",
        target=last_photo.target_id,
    )

    preview = preview_sample_layout_plan(SAMPLE_4Q, plan)

    assert preview.counts.explicit_page_break_count == 46
    assert preview.page_mapping == (
        "audited_three_table_pair_contract"
    )
    assert preview.operation_bindings[0].page_number == 48
    assert preview.operation_bindings[0].page_kind == "photo"


def test_plan_cannot_target_fixed_picture_duplicate_or_other_snapshot(
    sample_3q,
):
    fixed_picture = next(
        picture
        for picture in sample_3q.snapshot.pictures
        if picture.nearest_table_index == 1
    )
    with pytest.raises(
        HwpxSampleLayoutError,
        match="고정 그림",
    ):
        preview_sample_layout_plan(
            SAMPLE_3Q,
            _plan(
                sample_3q.document_snapshot_id,
                operation_type="insert_image",
                target=fixed_picture.target_id,
            ),
        )

    cell = sample_3q.snapshot.tables[0].cells[0]
    with pytest.raises(
        HwpxSampleLayoutError,
        match="같은 HWPX 위치",
    ):
        preview_sample_layout_plan(
            SAMPLE_3Q,
            _plan(
                sample_3q.document_snapshot_id,
                operation_type="fill_cell",
                target=cell.target_id,
                duplicate=True,
            ),
        )

    with pytest.raises(
        HwpxSampleLayoutError,
        match="구조 스냅샷",
    ):
        preview_sample_layout_plan(
            SAMPLE_3Q,
            _plan(
                "hwpx-sample-3q-" + ("0" * 24),
                operation_type="fill_cell",
                target=cell.target_id,
            ),
        )


@pytest.mark.parametrize(
    ("operation_type", "style_policy", "message"),
    (
        ("fill_text", "preserve", "fill_text"),
        ("add_table_row", "preserve", "표 행"),
        ("fill_cell", "inherit_target", "기존 서식"),
    ),
)
def test_growth_named_targets_and_style_changes_are_blocked(
    sample_3q,
    operation_type,
    style_policy,
    message,
):
    cell = sample_3q.snapshot.tables[0].cells[0]
    with pytest.raises(HwpxSampleLayoutError, match=message):
        preview_sample_layout_plan(
            SAMPLE_3Q,
            _plan(
                sample_3q.document_snapshot_id,
                operation_type=operation_type,
                target=cell.target_id,
                style_policy=style_policy,
            ),
        )
