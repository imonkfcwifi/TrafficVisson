"""Fail-closed semantic plan validation for audited sample HWPX reports.

This layer runs after the language model has produced a DocumentEditPlan and
after the server has resolved project-owned approved-reference records, but
before any deterministic writer or HWP COM call.  It does not edit a file.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Mapping, Sequence, Tuple, Union

from .contracts import DocumentEditPlan
from .hwpx_sample_layout import (
    EXPECTED_RENDERED_PAGE_COUNT,
    HwpxSampleLayoutError,
    SampleLayoutInspection,
    SampleReferenceCaseBinding,
    bind_sample_reference_case,
    inspect_sample_layout,
)


HWPX_SAMPLE_SEMANTIC_PLAN_SCHEMA = (
    "trafficvisson.hwpx-sample-semantic-plan.v1"
)
_APPROVAL_ID = re.compile(r"^approved-reference-[a-f0-9]{24}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_OPAQUE_EVIDENCE_REF = re.compile(
    r"^(?:deid|evidence):[A-Za-z0-9][A-Za-z0-9._:-]{0,117}$"
)
_SEMANTIC_ROLES = (
    "photo_caption",
    "inspection_route",
    "inspection_opinion_label",
    "inspection_opinion_body",
)


@dataclass(frozen=True)
class SampleSemanticOperationBinding:
    operation_id: str
    approval_id: str
    operation_type: str
    target_id: str
    semantic_role: str
    page_number: int
    table_index: int
    row: int
    column: int


@dataclass(frozen=True)
class SampleSemanticPlanValidation:
    schema_version: str
    state: str
    document_snapshot_id: str
    document_sha256: str
    structure_target_summary_sha256: str
    weak_case_index_manifest_sha256: str
    plan_sha256: str
    validation_sha256: str
    approved_reference_count: int
    operation_count: int
    page_pair_indices: Tuple[int, ...]
    touched_pages: Tuple[int, ...]
    expected_rendered_page_count: int
    expected_page_range: Tuple[int, int]
    page_range_preserved_by_structure: bool
    table_growth_allowed: bool
    semantic_validation_passed: bool
    com_invocation_performed: bool
    operation_bindings: Tuple[SampleSemanticOperationBinding, ...]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _ValidatedReference:
    record: Mapping[str, object]
    binding: SampleReferenceCaseBinding


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


def validate_sample_semantic_plan(
    source_path: Union[str, Path],
    plan: DocumentEditPlan,
    *,
    approved_references: Sequence[Mapping[str, object]],
) -> SampleSemanticPlanValidation:
    """Validate exact semantic targets without invoking COM or writing HWPX."""

    inspection = inspect_sample_layout(source_path)
    if plan.document_snapshot_id != inspection.document_snapshot_id:
        raise HwpxSampleLayoutError(
            "document_snapshot_mismatch",
            "편집 계획과 감사된 HWPX 구조 스냅샷이 일치하지 않습니다.",
        )
    if not approved_references:
        raise HwpxSampleLayoutError(
            "approved_reference_required",
            "의미 편집 계획에는 사용자 승인 표본 사례가 필요합니다.",
        )

    references: Dict[str, _ValidatedReference] = {}
    for raw in approved_references:
        validated = _validate_reference(inspection, raw)
        approval_id = str(validated.record["approval_id"])
        if approval_id in references:
            raise HwpxSampleLayoutError(
                "duplicate_approved_reference",
                "같은 승인 표본 사례를 두 번 결속할 수 없습니다.",
            )
        references[approval_id] = validated

    operation_bindings = []
    seen_targets = set()
    roles_by_reference: Dict[str, Dict[str, set[str]]] = {
        approval_id: {}
        for approval_id in references
    }
    roles_by_pair: Dict[int, Dict[str, set[str]]] = {}
    pair_marker_targets: Dict[int, set[str]] = {}

    for approval_id, reference in references.items():
        pair = reference.binding.page_pair_index
        marker_targets = {
            marker.cell_target_id
            for marker in reference.binding.square_markers
        }
        previous = pair_marker_targets.setdefault(pair, marker_targets)
        if previous != marker_targets:
            raise HwpxSampleLayoutError(
                "approved_marker_sets_conflict",
                "같은 페이지 쌍의 승인 사례 네모 위치가 서로 다릅니다.",
            )

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
                "표본 양식 의미 편집은 기존 서식을 보존해야 합니다.",
            )
        reference = references.get(operation.source_ref)
        if reference is None:
            raise HwpxSampleLayoutError(
                "operation_reference_not_approved",
                "편집 작업의 source_ref가 서버 승인 표본 사례가 아닙니다.",
            )
        role, page, table, row, column, expected = (
            _resolve_operation_role(reference, operation.target)
        )
        _validate_operation(
            operation,
            role=role,
            expected_value=expected,
        )
        pair = reference.binding.page_pair_index
        roles_by_reference[operation.source_ref].setdefault(
            role, set()
        ).add(operation.target)
        roles_by_pair.setdefault(pair, {}).setdefault(
            role, set()
        ).add(operation.target)
        operation_bindings.append(
            SampleSemanticOperationBinding(
                operation_id=str(operation.operation_id),
                approval_id=operation.source_ref,
                operation_type=operation.type,
                target_id=operation.target,
                semantic_role=role,
                page_number=page,
                table_index=table,
                row=row,
                column=column,
            )
        )

    _validate_role_completeness(
        references,
        roles_by_reference=roles_by_reference,
        roles_by_pair=roles_by_pair,
        pair_marker_targets=pair_marker_targets,
    )
    touched_pages = tuple(
        sorted({binding.page_number for binding in operation_bindings})
    )
    if (
        not touched_pages
        or touched_pages[0] < 1
        or touched_pages[-1] > EXPECTED_RENDERED_PAGE_COUNT
        or inspection.counts.rendered_page_count_baseline
        != EXPECTED_RENDERED_PAGE_COUNT
    ):
        raise HwpxSampleLayoutError(
            "sample_page_range_invalid",
            "편집 계획이 감사된 1~48쪽 범위를 벗어났습니다.",
        )

    plan_sha256 = _canonical_sha256(plan.model_dump(mode="json"))
    payload = {
        "schema_version": HWPX_SAMPLE_SEMANTIC_PLAN_SCHEMA,
        "document_snapshot_id": inspection.document_snapshot_id,
        "document_sha256": inspection.snapshot.document_sha256,
        "structure_target_summary_sha256": (
            inspection.snapshot.target_summary_sha256
        ),
        "weak_case_index_manifest_sha256": (
            inspection.baseline.weak_case_index_manifest_sha256
        ),
        "plan_sha256": plan_sha256,
        "approved_reference_record_sha256s": sorted(
            str(item.record["record_sha256"])
            for item in references.values()
        ),
        "operation_bindings": [
            asdict(binding) for binding in operation_bindings
        ],
        "expected_rendered_page_count": EXPECTED_RENDERED_PAGE_COUNT,
        "expected_page_range": [1, EXPECTED_RENDERED_PAGE_COUNT],
    }
    return SampleSemanticPlanValidation(
        schema_version=HWPX_SAMPLE_SEMANTIC_PLAN_SCHEMA,
        state="semantic_plan_validated_com_not_invoked",
        document_snapshot_id=inspection.document_snapshot_id,
        document_sha256=inspection.snapshot.document_sha256,
        structure_target_summary_sha256=(
            inspection.snapshot.target_summary_sha256
        ),
        weak_case_index_manifest_sha256=(
            inspection.baseline.weak_case_index_manifest_sha256
        ),
        plan_sha256=plan_sha256,
        validation_sha256=_canonical_sha256(payload),
        approved_reference_count=len(references),
        operation_count=len(operation_bindings),
        page_pair_indices=tuple(
            sorted(
                {
                    reference.binding.page_pair_index
                    for reference in references.values()
                }
            )
        ),
        touched_pages=touched_pages,
        expected_rendered_page_count=EXPECTED_RENDERED_PAGE_COUNT,
        expected_page_range=(1, EXPECTED_RENDERED_PAGE_COUNT),
        page_range_preserved_by_structure=True,
        table_growth_allowed=False,
        semantic_validation_passed=True,
        com_invocation_performed=False,
        operation_bindings=tuple(operation_bindings),
    )


def _validate_reference(
    inspection: SampleLayoutInspection,
    raw: Mapping[str, object],
) -> _ValidatedReference:
    if not isinstance(raw, Mapping):
        raise HwpxSampleLayoutError(
            "approved_reference_invalid",
            "승인 표본 사례 레코드가 올바르지 않습니다.",
        )
    record = dict(raw)
    record.pop("idempotent_replay", None)
    supplied_hash = record.pop("record_sha256", None)
    if (
        not isinstance(supplied_hash, str)
        or _SHA256.fullmatch(supplied_hash) is None
        or _canonical_sha256(record) != supplied_hash
    ):
        raise HwpxSampleLayoutError(
            "approved_reference_hash_mismatch",
            "승인 표본 사례 레코드 해시가 일치하지 않습니다.",
        )
    record["record_sha256"] = supplied_hash
    approval_id = record.get("approval_id")
    if (
        not isinstance(approval_id, str)
        or _APPROVAL_ID.fullmatch(approval_id) is None
        or record.get("schema_version")
        != "trafficvisson.approved-reference.v1"
        or record.get("reference_state") != "approved_reference"
        or record.get("approved_reference") is not True
        or record.get("user_confirmed") is not True
        or record.get("training_scope") != "approved_reference_only"
        or record.get("annotation_created") is not False
        or record.get("incident_created") is not False
    ):
        raise HwpxSampleLayoutError(
            "approved_reference_state_invalid",
            "표본 사례가 사용자 승인 reference 상태가 아닙니다.",
        )
    if (
        record.get("document_sha256")
        != inspection.snapshot.document_sha256
        or record.get("structure_target_summary_sha256")
        != inspection.snapshot.target_summary_sha256
        or record.get("index_manifest_sha256")
        != inspection.baseline.weak_case_index_manifest_sha256
    ):
        raise HwpxSampleLayoutError(
            "approved_reference_structure_mismatch",
            "승인 사례가 현재 sample 구조 해시와 일치하지 않습니다.",
        )
    picture_target_id = record.get("picture_target_id")
    if not isinstance(picture_target_id, str):
        raise HwpxSampleLayoutError(
            "approved_reference_picture_missing",
            "승인 사례에 정확한 사진 target이 없습니다.",
        )
    binding = bind_sample_reference_case(
        inspection,
        picture_target_id,
    )
    if (
        record.get("sample_layout_binding_sha256")
        != binding.binding_sha256
        or record.get("photo_slot_id") != binding.photo_slot_id
        or record.get("photo_page_number")
        != binding.photo_page_number
        or record.get("inspection_page_number")
        != binding.inspection_page_number
        or record.get("table_index") != binding.photo_table_index
        or record.get("cell_target_id")
        != binding.photo_cell_target_id
        or record.get("cell_row") != binding.photo_cell_row
        or record.get("cell_column") != binding.photo_cell_column
    ):
        raise HwpxSampleLayoutError(
            "approved_reference_slot_mismatch",
            "승인 사례의 사진 슬롯·페이지·표·셀 결속이 일치하지 않습니다.",
        )
    if record.get("square_markers") != [
        asdict(marker) for marker in binding.square_markers
    ]:
        raise HwpxSampleLayoutError(
            "approved_reference_marker_mismatch",
            "승인 사례의 평가 네모 위치가 sample 구조와 일치하지 않습니다.",
        )
    semantic_cells = record.get("semantic_cells")
    expected_semantic = [
        asdict(getattr(binding, role)) for role in _SEMANTIC_ROLES
    ]
    if semantic_cells != expected_semantic:
        raise HwpxSampleLayoutError(
            "approved_reference_semantic_cells_mismatch",
            "승인 사례의 캡션·노선·점검의견 셀 역할이 일치하지 않습니다.",
        )
    image_sha256 = record.get("image_sha256")
    if (
        not isinstance(image_sha256, str)
        or _SHA256.fullmatch(image_sha256) is None
    ):
        raise HwpxSampleLayoutError(
            "approved_reference_image_hash_invalid",
            "승인 사례 사진 해시가 올바르지 않습니다.",
        )
    return _ValidatedReference(record=record, binding=binding)


def _resolve_operation_role(
    reference: _ValidatedReference,
    target_id: str,
) -> Tuple[str, int, int, int, int, str]:
    binding = reference.binding
    if target_id == binding.picture_target_id:
        return (
            "photo_slot_image",
            binding.photo_page_number,
            binding.photo_table_index,
            binding.photo_cell_row,
            binding.photo_cell_column,
            str(reference.record["image_sha256"]),
        )
    semantic_cells = {
        binding.photo_caption.cell_target_id: binding.photo_caption,
        binding.inspection_route.cell_target_id: (
            binding.inspection_route
        ),
        binding.inspection_opinion_body.cell_target_id: (
            binding.inspection_opinion_body
        ),
    }
    semantic = semantic_cells.get(target_id)
    if semantic is not None:
        return (
            semantic.role,
            semantic.page_number,
            semantic.table_index,
            semantic.row,
            semantic.column,
            semantic.text_excerpt,
        )
    marker = next(
        (
            candidate
            for candidate in binding.square_markers
            if candidate.cell_target_id == target_id
        ),
        None,
    )
    if marker is not None:
        return (
            "evaluation_square_marker",
            marker.page_number,
            marker.table_index,
            marker.row,
            marker.column,
            marker.marker,
        )
    if target_id == binding.inspection_opinion_label.cell_target_id:
        raise HwpxSampleLayoutError(
            "opinion_label_is_immutable",
            "점검의견 라벨 셀은 본문 입력 대상으로 사용할 수 없습니다.",
        )
    raise HwpxSampleLayoutError(
        "operation_target_outside_approved_binding",
        "편집 target이 승인 사례의 의미별 허용 위치가 아닙니다.",
    )


def _validate_operation(
    operation,
    *,
    role: str,
    expected_value: str,
) -> None:
    expected_type = (
        "insert_image" if role == "photo_slot_image" else "fill_cell"
    )
    if operation.type != expected_type:
        raise HwpxSampleLayoutError(
            "operation_type_role_mismatch",
            "편집 작업 유형과 sample 셀·사진 역할이 일치하지 않습니다.",
        )
    if operation.expected_value != expected_value:
        raise HwpxSampleLayoutError(
            "operation_expected_value_mismatch",
            "편집 작업의 적용 전 기대값이 현재 sample 내용과 다릅니다.",
        )
    value = operation.value.strip()
    if role == "photo_slot_image":
        if _OPAQUE_EVIDENCE_REF.fullmatch(value) is None:
            raise HwpxSampleLayoutError(
                "photo_source_must_be_opaque_evidence",
                "사진 작업은 비식별화된 opaque evidence 참조만 사용할 수 있습니다.",
            )
    elif role == "evaluation_square_marker":
        if value != "■":
            raise HwpxSampleLayoutError(
                "evaluation_marker_value_invalid",
                "평가 위치에는 기존 양식과 같은 ■ 표시만 사용할 수 있습니다.",
            )
    elif role == "inspection_route":
        normalized = " ".join(value.replace("：", ":").split()).replace(
            " :", ":"
        )
        if not normalized.startswith("노선명:"):
            raise HwpxSampleLayoutError(
                "inspection_route_value_invalid",
                "노선 셀 값은 노선명 라벨을 보존해야 합니다.",
            )
    elif not value:
        raise HwpxSampleLayoutError(
            "semantic_cell_value_empty",
            "캡션과 점검의견 셀에는 빈 값만 입력할 수 없습니다.",
        )


def _validate_role_completeness(
    references: Mapping[str, _ValidatedReference],
    *,
    roles_by_reference: Mapping[str, Mapping[str, set[str]]],
    roles_by_pair: Mapping[int, Mapping[str, set[str]]],
    pair_marker_targets: Mapping[int, set[str]],
) -> None:
    for approval_id, reference in references.items():
        roles = roles_by_reference[approval_id]
        if roles.get("photo_slot_image") != {
            reference.binding.picture_target_id
        }:
            raise HwpxSampleLayoutError(
                "reference_photo_incomplete",
                "각 승인 사례는 정확한 사진 슬롯을 한 번씩 포함해야 합니다.",
            )
    for pair, marker_targets in pair_marker_targets.items():
        roles = roles_by_pair.get(pair, {})
        expected_caption_targets = {
            reference.binding.photo_caption.cell_target_id
            for reference in references.values()
            if reference.binding.page_pair_index == pair
        }
        if roles.get("photo_caption", set()) != (
            expected_caption_targets
        ):
            raise HwpxSampleLayoutError(
                "reference_photo_caption_incomplete",
                "각 사진 또는 병합 사진 묶음의 정확한 캡션 셀을 한 번씩 포함해야 합니다.",
            )
        if len(roles.get("inspection_route", ())) != 1:
            raise HwpxSampleLayoutError(
                "inspection_route_role_incomplete",
                "편집하는 페이지 쌍마다 노선명 셀을 정확히 한 번 포함해야 합니다.",
            )
        if len(roles.get("inspection_opinion_body", ())) != 1:
            raise HwpxSampleLayoutError(
                "inspection_opinion_role_incomplete",
                "편집하는 페이지 쌍마다 점검의견 본문 셀을 정확히 한 번 포함해야 합니다.",
            )
        if roles.get("evaluation_square_marker", set()) != marker_targets:
            raise HwpxSampleLayoutError(
                "evaluation_marker_set_incomplete",
                "편집하는 페이지 쌍의 승인된 평가 ■ 위치를 정확히 보존해야 합니다.",
            )


__all__ = [
    "HWPX_SAMPLE_SEMANTIC_PLAN_SCHEMA",
    "SampleSemanticOperationBinding",
    "SampleSemanticPlanValidation",
    "validate_sample_semantic_plan",
]
