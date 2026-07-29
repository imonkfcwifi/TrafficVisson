from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from app.contracts import DocumentEditPlan
from app.hwpx_sample_layout import (
    HwpxSampleLayoutError,
    bind_sample_reference_case,
    inspect_sample_layout,
)
from app.hwpx_sample_plan_validator import (
    validate_sample_semantic_plan,
)


WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q = WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"
SAMPLE_4Q = WORKSPACE / "sample" / "4분기 점검 내용 정리.hwpx"
PROJECT_ID = str(uuid4())


@pytest.fixture(scope="module")
def sample_3q():
    return inspect_sample_layout(SAMPLE_3Q)


@pytest.fixture(scope="module")
def sample_4q():
    return inspect_sample_layout(SAMPLE_4Q)


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


def _rehash(record: dict) -> dict:
    refreshed = copy.deepcopy(record)
    refreshed.pop("record_sha256", None)
    refreshed["record_sha256"] = _canonical_sha256(refreshed)
    return refreshed


def _approved_reference(
    inspection,
    picture_target_id: str,
    *,
    token: str = "a",
) -> dict:
    binding = bind_sample_reference_case(
        inspection,
        picture_target_id,
    )
    quarter = inspection.baseline.quarter
    record = {
        "schema_version": "trafficvisson.approved-reference.v1",
        "approval_id": "approved-reference-" + token * 24,
        "project_id": PROJECT_ID,
        "source_id": (
            f"sample-hwpx-{quarter}q-"
            + inspection.snapshot.document_sha256[:16]
        ),
        "case_id": "hwpx-weak-case-" + token * 32,
        "revision": 1,
        "reference_state": "approved_reference",
        "approved_reference": True,
        "training_eligible": True,
        "training_scope": "approved_reference_only",
        "annotation_created": False,
        "incident_created": False,
        "document_name": inspection.baseline.expected_document_name,
        "document_sha256": inspection.snapshot.document_sha256,
        "structure_target_summary_sha256": (
            inspection.snapshot.target_summary_sha256
        ),
        "index_manifest_sha256": (
            inspection.baseline.weak_case_index_manifest_sha256
        ),
        "image_sha256": token * 64,
        "image_media_type": "image/jpeg",
        "image_width": 1920,
        "image_height": 1080,
        "quarter": quarter,
        "section_path": "Contents/section0.xml",
        "section_index": 0,
        "picture_target_id": binding.picture_target_id,
        "picture_index": binding.picture_index,
        "picture_xml_id": None,
        "table_id": inspection.snapshot.tables[
            binding.photo_table_index
        ].table_id,
        "table_index": binding.photo_table_index,
        "cell_target_id": binding.photo_cell_target_id,
        "cell_row": binding.photo_cell_row,
        "cell_column": binding.photo_cell_column,
        "page_hint": binding.photo_page_number,
        "page_hint_source": "audited_three_table_pair_contract",
        "photo_slot_id": binding.photo_slot_id,
        "photo_page_number": binding.photo_page_number,
        "inspection_page_number": binding.inspection_page_number,
        "sample_layout_binding_sha256": binding.binding_sha256,
        "square_markers": [
            asdict(marker) for marker in binding.square_markers
        ],
        "semantic_cells": [
            asdict(binding.photo_caption),
            asdict(binding.inspection_route),
            asdict(binding.inspection_opinion_label),
            asdict(binding.inspection_opinion_body),
        ],
        "weak_candidates": {
            "caption": None,
            "route": None,
            "location": None,
            "evaluation": None,
            "opinion": None,
        },
        "issue_type": "roadside_mowing_required",
        "maintenance_action": "삭초 및 수목 제거",
        "caption": "삭초 필요 구간",
        "route": "검증 노선",
        "location": "검증 위치",
        "evaluation": "보통",
        "opinion": "시야 확보를 위한 삭초가 필요함",
        "semantic_sha256": "d" * 64,
        "approved_at": "2026-07-29T00:00:00Z",
        "updated_at": "2026-07-29T00:00:00Z",
        "user_confirmed": True,
    }
    return _rehash(record)


def _operation(
    *,
    operation_type: str,
    target: str,
    expected_value: str,
    value: str,
    approval_id: str,
) -> dict:
    return {
        "operation_id": str(uuid4()),
        "type": operation_type,
        "target": target,
        "expected_value": expected_value,
        "value": value,
        "source_ref": approval_id,
        "style_policy": "preserve",
        "preview": "승인된 sample 의미 위치 미리보기",
    }


def _valid_plan(inspection, record: dict) -> DocumentEditPlan:
    binding = bind_sample_reference_case(
        inspection,
        record["picture_target_id"],
    )
    approval_id = record["approval_id"]
    operations = [
        _operation(
            operation_type="insert_image",
            target=binding.picture_target_id,
            expected_value=record["image_sha256"],
            value="deid:incident-photo-1",
            approval_id=approval_id,
        ),
        _operation(
            operation_type="fill_cell",
            target=binding.photo_caption.cell_target_id,
            expected_value=binding.photo_caption.text_excerpt,
            value="삭초 필요 구간",
            approval_id=approval_id,
        ),
        _operation(
            operation_type="fill_cell",
            target=binding.inspection_route.cell_target_id,
            expected_value=binding.inspection_route.text_excerpt,
            value="노선명 : 검증 노선",
            approval_id=approval_id,
        ),
        _operation(
            operation_type="fill_cell",
            target=binding.inspection_opinion_body.cell_target_id,
            expected_value=(
                binding.inspection_opinion_body.text_excerpt
            ),
            value="ㅇ 시야 확보를 위한 삭초가 필요함",
            approval_id=approval_id,
        ),
    ]
    operations.extend(
        _operation(
            operation_type="fill_cell",
            target=marker.cell_target_id,
            expected_value="■",
            value="■",
            approval_id=approval_id,
        )
        for marker in binding.square_markers
    )
    return DocumentEditPlan.model_validate(
        {
            "schema_version": "1.0",
            "plan_id": str(uuid4()),
            "document_snapshot_id": (
                inspection.document_snapshot_id
            ),
            "operations": operations,
        }
    )


def _replace_operation(
    plan: DocumentEditPlan,
    index: int,
    **changes,
) -> DocumentEditPlan:
    payload = plan.model_dump(mode="json")
    payload["operations"][index].update(changes)
    return DocumentEditPlan.model_validate(payload)


def test_exact_3q_semantic_plan_passes_without_com(sample_3q):
    photo = next(
        picture
        for picture in sample_3q.snapshot.pictures
        if picture.nearest_table_index == 2
    )
    record = _approved_reference(sample_3q, photo.target_id)
    plan = _valid_plan(sample_3q, record)

    result = validate_sample_semantic_plan(
        SAMPLE_3Q,
        plan,
        approved_references=[record],
    )

    assert result.state == "semantic_plan_validated_com_not_invoked"
    assert result.semantic_validation_passed is True
    assert result.com_invocation_performed is False
    assert result.table_growth_allowed is False
    assert result.page_range_preserved_by_structure is True
    assert result.expected_rendered_page_count == 48
    assert result.expected_page_range == (1, 48)
    assert result.page_pair_indices == (0,)
    assert result.touched_pages == (1, 2)
    assert result.operation_count == 4 + len(
        record["square_markers"]
    )
    assert {
        operation.semantic_role
        for operation in result.operation_bindings
    } == {
        "photo_slot_image",
        "photo_caption",
        "inspection_route",
        "inspection_opinion_body",
        "evaluation_square_marker",
    }


def test_exact_4q_last_pair_preserves_audited_48_page_range(
    sample_4q,
):
    photo = next(
        picture
        for picture in reversed(sample_4q.snapshot.pictures)
        if picture.nearest_table_index == 71
    )
    record = _approved_reference(sample_4q, photo.target_id)
    plan = _valid_plan(sample_4q, record)

    result = validate_sample_semantic_plan(
        SAMPLE_4Q,
        plan,
        approved_references=[record],
    )

    assert result.page_pair_indices == (23,)
    assert result.touched_pages == (47, 48)
    assert result.expected_page_range == (1, 48)
    assert result.page_range_preserved_by_structure is True
    assert result.com_invocation_performed is False


def test_merged_caption_for_left_right_photo_pair_is_written_once(
    sample_4q,
):
    photos_by_caption = {}
    for photo in sample_4q.snapshot.pictures:
        if (
            photo.nearest_table_index is None
            or photo.nearest_table_index % 3 != 2
        ):
            continue
        binding = bind_sample_reference_case(
            sample_4q,
            photo.target_id,
        )
        photos_by_caption.setdefault(
            binding.photo_caption.cell_target_id,
            [],
        ).append(photo)
    left, right = next(
        photos[:2]
        for photos in photos_by_caption.values()
        if len(photos) >= 2
    )
    left_record = _approved_reference(
        sample_4q,
        left.target_id,
        token="a",
    )
    right_record = _approved_reference(
        sample_4q,
        right.target_id,
        token="b",
    )
    left_plan = _valid_plan(sample_4q, left_record)
    right_plan = _valid_plan(sample_4q, right_record)
    payload = left_plan.model_dump(mode="json")
    existing_targets = {
        operation["target"] for operation in payload["operations"]
    }
    payload["operations"].extend(
        operation
        for operation in right_plan.model_dump(mode="json")[
            "operations"
        ]
        if operation["target"] not in existing_targets
    )
    payload["plan_id"] = str(uuid4())
    plan = DocumentEditPlan.model_validate(payload)

    result = validate_sample_semantic_plan(
        SAMPLE_4Q,
        plan,
        approved_references=[left_record, right_record],
    )

    assert result.approved_reference_count == 2
    assert sum(
        binding.semantic_role == "photo_slot_image"
        for binding in result.operation_bindings
    ) == 2
    assert sum(
        binding.semantic_role == "photo_caption"
        for binding in result.operation_bindings
    ) == 1
    assert result.com_invocation_performed is False


def test_different_photo_slot_is_rejected_before_com(sample_3q):
    photos = [
        picture
        for picture in sample_3q.snapshot.pictures
        if picture.nearest_table_index == 2
    ]
    record = _approved_reference(sample_3q, photos[0].target_id)
    plan = _valid_plan(sample_3q, record)
    wrong = _replace_operation(
        plan,
        0,
        target=photos[1].target_id,
    )

    with pytest.raises(HwpxSampleLayoutError) as captured:
        validate_sample_semantic_plan(
            SAMPLE_3Q,
            wrong,
            approved_references=[record],
        )

    assert captured.value.code == (
        "operation_target_outside_approved_binding"
    )


def test_opinion_label_cannot_be_used_as_opinion_body(sample_3q):
    photo = next(
        picture
        for picture in sample_3q.snapshot.pictures
        if picture.nearest_table_index == 2
    )
    record = _approved_reference(sample_3q, photo.target_id)
    binding = bind_sample_reference_case(sample_3q, photo.target_id)
    plan = _valid_plan(sample_3q, record)
    wrong = _replace_operation(
        plan,
        3,
        target=binding.inspection_opinion_label.cell_target_id,
    )

    with pytest.raises(HwpxSampleLayoutError) as captured:
        validate_sample_semantic_plan(
            SAMPLE_3Q,
            wrong,
            approved_references=[record],
        )

    assert captured.value.code == "opinion_label_is_immutable"


def test_missing_evaluation_square_is_rejected(sample_3q):
    photo = next(
        picture
        for picture in sample_3q.snapshot.pictures
        if picture.nearest_table_index == 2
    )
    record = _approved_reference(sample_3q, photo.target_id)
    payload = _valid_plan(sample_3q, record).model_dump(
        mode="json"
    )
    payload["operations"].pop()
    wrong = DocumentEditPlan.model_validate(payload)

    with pytest.raises(HwpxSampleLayoutError) as captured:
        validate_sample_semantic_plan(
            SAMPLE_3Q,
            wrong,
            approved_references=[record],
        )

    assert captured.value.code == "evaluation_marker_set_incomplete"


def test_stale_expected_cell_value_is_rejected(sample_3q):
    photo = next(
        picture
        for picture in sample_3q.snapshot.pictures
        if picture.nearest_table_index == 2
    )
    record = _approved_reference(sample_3q, photo.target_id)
    plan = _valid_plan(sample_3q, record)
    wrong = _replace_operation(
        plan,
        2,
        expected_value="노선명 : 오래된 값",
    )

    with pytest.raises(HwpxSampleLayoutError) as captured:
        validate_sample_semantic_plan(
            SAMPLE_3Q,
            wrong,
            approved_references=[record],
        )

    assert captured.value.code == "operation_expected_value_mismatch"


def test_tampered_approval_record_hash_is_rejected(sample_3q):
    photo = next(
        picture
        for picture in sample_3q.snapshot.pictures
        if picture.nearest_table_index == 2
    )
    record = _approved_reference(sample_3q, photo.target_id)
    plan = _valid_plan(sample_3q, record)
    record["cell_column"] = record["cell_column"] + 1

    with pytest.raises(HwpxSampleLayoutError) as captured:
        validate_sample_semantic_plan(
            SAMPLE_3Q,
            plan,
            approved_references=[record],
        )

    assert captured.value.code == "approved_reference_hash_mismatch"


def test_rehashed_but_wrong_sample_structure_is_rejected(sample_3q):
    photo = next(
        picture
        for picture in sample_3q.snapshot.pictures
        if picture.nearest_table_index == 2
    )
    record = _approved_reference(sample_3q, photo.target_id)
    plan = _valid_plan(sample_3q, record)
    record["structure_target_summary_sha256"] = "e" * 64
    record = _rehash(record)

    with pytest.raises(HwpxSampleLayoutError) as captured:
        validate_sample_semantic_plan(
            SAMPLE_3Q,
            plan,
            approved_references=[record],
        )

    assert captured.value.code == (
        "approved_reference_structure_mismatch"
    )


def test_style_inheritance_is_rejected_before_writer(sample_3q):
    photo = next(
        picture
        for picture in sample_3q.snapshot.pictures
        if picture.nearest_table_index == 2
    )
    record = _approved_reference(sample_3q, photo.target_id)
    plan = _valid_plan(sample_3q, record)
    wrong = _replace_operation(
        plan,
        1,
        style_policy="inherit_target",
    )

    with pytest.raises(HwpxSampleLayoutError) as captured:
        validate_sample_semantic_plan(
            SAMPLE_3Q,
            wrong,
            approved_references=[record],
        )

    assert captured.value.code == "sample_style_must_be_preserved"
