from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.current_incident_report_binder import (
    CurrentIncidentReportBindingError,
    _EvaluationRow,
    _PagePair,
    _PhotoSlot,
    _classify_page_pair,
    bind_current_incidents_to_sample,
    deidentification_record_sha256,
)
from app.hwpx_sample_layout import (
    SampleLayoutBaseline,
    SampleLayoutCounts,
    SampleLayoutInspection,
)
from app.pipeline_report_preview import SampleTemplateSemanticMap
from approved_incident_authority_fixture import (
    fixture_approval_record_id,
    fixture_incident_id,
    fixture_output_authority,
)


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
PIPELINE_JOB_ID = UUID("22222222-2222-4222-8222-222222222222")
ANALYSIS_JOB_ID = UUID("33333333-3333-4333-8333-333333333333")
SOURCE_SHA256 = "4" * 64
TEMPLATE_SHA256 = "5" * 64
TARGET_SHA256 = "6" * 64
SEMANTIC_SHA256 = "7" * 64
RATINGS = (
    "very_good",
    "good",
    "normal",
    "poor",
    "very_poor",
) * 4


def _inspection() -> SampleLayoutInspection:
    baseline = SampleLayoutBaseline(
        template_id="sample-3q-v1",
        quarter=3,
        expected_document_name="sample.hwpx",
        document_sha256=TEMPLATE_SHA256,
        target_summary_sha256=TARGET_SHA256,
        pdf_sha256="8" * 64,
        weak_case_index_manifest_sha256="9" * 64,
        section_count=1,
        table_count=6,
        cell_count=1,
        picture_count=3,
        explicit_page_break_count=3,
        fixed_inspection_picture_count=2,
        existing_photo_picture_count=3,
        rendered_page_count=4,
        page_pair_count=2,
    )
    snapshot = SimpleNamespace(
        document_sha256=TEMPLATE_SHA256,
        target_summary_sha256=TARGET_SHA256,
    )
    counts = SampleLayoutCounts(
        rendered_page_count_baseline=4,
        page_pair_count=2,
        section_count=1,
        table_count=6,
        cell_count=1,
        picture_count=3,
        fixed_inspection_picture_count=2,
        existing_photo_picture_count=3,
        explicit_page_break_count=3,
    )
    return SampleLayoutInspection(
        baseline=baseline,
        snapshot=snapshot,
        document_snapshot_id="snapshot-current-test",
        baseline_manifest_sha256="a" * 64,
        counts=counts,
    )


def _semantic_map() -> SampleTemplateSemanticMap:
    return SampleTemplateSemanticMap(
        requested_template_id="sample-q3",
        canonical_template_id="sample-3q-v1",
        semantic_map_id="sample-q3-semantic-v1",
        semantic_map_sha256=SEMANTIC_SHA256,
        template_sha256=TEMPLATE_SHA256,
        document_snapshot_id="snapshot-current-test",
        structure_target_summary_sha256=TARGET_SHA256,
        document_structure_summary={
            "image_targets": [],
            "cell_targets": [],
        },
        source_path=Path(r"C:\not-exposed\sample.hwpx"),
    )


def _evaluation_rows(pair: int) -> tuple[_EvaluationRow, ...]:
    rows = []
    for row_order in range(len(RATINGS)):
        targets = tuple(
            {
                "grade": grade,
                "target_id": f"pair-{pair}-row-{row_order}-{grade}",
                "anchor_column": grade_order + 3,
                "expected_value": {
                    "text": "■" if grade_order == row_order % 5 else "",
                    "text_sha256": f"{grade_order + 1}" * 64,
                },
            }
            for grade_order, grade in enumerate(
                (
                    "very_good",
                    "good",
                    "normal",
                    "poor",
                    "very_poor",
                )
            )
        )
        rows.append(
            _EvaluationRow(
                row_order=row_order,
                row=row_order + 2,
                label_sha256=f"{row_order % 10}" * 64,
                five_grade_grid_available=True,
                grade_targets=targets,
            )
        )
    return tuple(rows)


def _fake_audit(*_args):
    slots = (
        _PhotoSlot(
            page_pair_index=0,
            inspection_page_number=1,
            photo_page_number=2,
            photo_slot_id="slot-0",
            photo_cell_target_id="photo-cell-0",
            photo_cell_row=0,
            photo_cell_column=0,
            picture_target_ids=("picture-0",),
            picture_indexes=(0,),
            caption_target_id="caption-0",
            caption_expected_text="old caption zero",
            caption_expected_sha256="b" * 64,
        ),
        _PhotoSlot(
            page_pair_index=0,
            inspection_page_number=1,
            photo_page_number=2,
            photo_slot_id="slot-1",
            photo_cell_target_id="photo-cell-1",
            photo_cell_row=0,
            photo_cell_column=1,
            picture_target_ids=("picture-1", "picture-overlay-1"),
            picture_indexes=(1, 2),
            caption_target_id="caption-1",
            caption_expected_text="old caption one",
            caption_expected_sha256="c" * 64,
        ),
        _PhotoSlot(
            page_pair_index=1,
            inspection_page_number=3,
            photo_page_number=4,
            photo_slot_id="slot-2",
            photo_cell_target_id="photo-cell-2",
            photo_cell_row=2,
            photo_cell_column=0,
            picture_target_ids=("picture-2",),
            picture_indexes=(3,),
            caption_target_id="caption-2",
            caption_expected_text="old caption two",
            caption_expected_sha256="d" * 64,
        ),
    )
    pages = tuple(
        _PagePair(
            page_pair_index=pair,
            inspection_page_number=pair * 2 + 1,
            photo_page_number=pair * 2 + 2,
            route_target_id=f"route-{pair}",
            route_expected_text=f"old route {pair}",
            route_expected_sha256="e" * 64,
            date_target_id=f"date-{pair}",
            date_expected_text="old date",
            date_expected_sha256="f" * 64,
            opinion_target_id=f"opinion-{pair}",
            opinion_expected_text=f"old opinion {pair}",
            opinion_expected_sha256="1" * 64,
            old_marker_targets=(
                {
                    "target_id": f"pair-{pair}-row-0-very_good",
                    "row": 2,
                    "column": 3,
                    "expected_value": {
                        "text": "■",
                        "text_sha256": "2" * 64,
                    },
                },
            ),
            evaluation_rows=_evaluation_rows(pair),
        )
        for pair in range(2)
    )
    return slots, pages


def _fake_stale_audit(*_args):
    def text_target(role, pair, page, old_text):
        return SimpleNamespace(
            role=role,
            target_id=f"static-{role}-{pair}",
            page_pair_index=pair,
            page_number=page,
            expected_text=old_text,
            expected_text_sha256=hashlib.sha256(
                old_text.encode("utf-8")
            ).hexdigest(),
        )

    pairs = []
    required = []
    for pair in range(2):
        inspection_page = pair * 2 + 1
        photo_page = pair * 2 + 2
        title_group = text_target(
            "title_group",
            pair,
            inspection_page,
            f"그룹 {pair + 1}",
        )
        title_route = text_target(
            "title_route_heading",
            pair,
            inspection_page,
            f"3분기 과거 노선 {pair}",
        )
        inspector = text_target(
            "inspection_inspector",
            pair,
            inspection_page,
            f"점검자 : 과거 {pair} (인)",
        )
        photo_header = text_target(
            "photo_page_header",
            pair,
            photo_page,
            f"□ 과거 노선 {pair} 현황사진",
        )
        signature = SimpleNamespace(
            role="inspector_signature",
            target_id=f"static-inspector_signature-{pair}",
            page_pair_index=pair,
            page_number=inspection_page,
            expected_binding_sha256=f"{pair + 3}" * 64,
            binary_item_refs=(f"signature-{pair}",),
            binaries=(
                SimpleNamespace(sha256=f"{pair + 5}" * 64),
            ),
        )
        rows = (
            title_group,
            title_route,
            inspector,
            photo_header,
            signature,
        )
        required.extend(item.target_id for item in rows)
        pairs.append(
            SimpleNamespace(
                page_pair_index=pair,
                title_group=title_group,
                title_route_heading=title_route,
                inspection_inspector=inspector,
                photo_page_header=photo_header,
                inspector_signature=signature,
            )
        )
    return SimpleNamespace(
        document_sha256=TEMPLATE_SHA256,
        document_snapshot_id="snapshot-current-test",
        page_pairs=tuple(pairs),
        required_target_ids=tuple(required),
        counts=SimpleNamespace(required_target_count=len(required)),
        audit_sha256="a" * 64,
    )


def _resolved_incident_id(label_or_id: str) -> str:
    try:
        if str(UUID(label_or_id)) == label_or_id:
            return label_or_id
    except ValueError:
        pass
    return fixture_incident_id(label_or_id, ANALYSIS_JOB_ID)


def _incident(label: str, **overrides):
    incident_id = _resolved_incident_id(label)
    approval_record_id = fixture_approval_record_id(
        label,
        PIPELINE_JOB_ID,
    )
    evidence_ref = f"original:{incident_id}"
    value = {
        "incident_id": incident_id,
        "project_id": str(PROJECT_ID),
        "pipeline_job_id": str(PIPELINE_JOB_ID),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "source_sha256": SOURCE_SHA256,
        "review_status": "approved",
        "approval_actor": "local_user",
        "approval_record_id": approval_record_id,
        "approval_record_sha256": "9" * 64,
        "incident_type": "pothole",
        "severity": "moderate",
        "maintenance_action": "포트홀 보수 필요",
        "ocr_text": None,
        "time_ms": 1_000,
        "evidence_refs": [evidence_ref],
        "approved_output_authority": fixture_output_authority(
            label=label,
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_sha256=SOURCE_SHA256,
            approval_record_id=approval_record_id,
            approval_record_sha256="9" * 64,
            representative_image_ref=evidence_ref,
            representative_image_sha256="a" * 64,
        ),
    }
    value.update(overrides)
    return value


def _evidence(
    incident_id: str,
    *,
    pipeline_job_id: UUID = PIPELINE_JOB_ID,
    verified_deidentified: bool = False,
    rename_only: bool = False,
):
    incident_id = _resolved_incident_id(incident_id)
    original_ref = f"original:{incident_id}"
    original_sha = "a" * 64
    if not verified_deidentified:
        return {
            "incident_id": incident_id,
            "project_id": str(PROJECT_ID),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(ANALYSIS_JOB_ID),
            "source_sha256": SOURCE_SHA256,
            "original_ref": original_ref,
            "original_sha256": original_sha,
            "deidentified_ref": None,
            "deidentified_sha256": None,
            "deidentification_record_sha256": None,
            "deidentified_verified": False,
        }
    deid_ref = (
        original_ref if rename_only else f"deidentified:{incident_id}"
    )
    deid_sha = original_sha if rename_only else "b" * 64
    record_sha = deidentification_record_sha256(
        incident_id=incident_id,
        project_id=PROJECT_ID,
        pipeline_job_id=pipeline_job_id,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        original_ref=original_ref,
        original_sha256=original_sha,
        deidentified_ref=deid_ref,
        deidentified_sha256=deid_sha,
    )
    return {
        "incident_id": incident_id,
        "project_id": str(PROJECT_ID),
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(ANALYSIS_JOB_ID),
        "source_sha256": SOURCE_SHA256,
        "original_ref": original_ref,
        "original_sha256": original_sha,
        "deidentified_ref": deid_ref,
        "deidentified_sha256": deid_sha,
        "deidentification_record_sha256": record_sha,
        "deidentified_verified": True,
    }


def _bind(monkeypatch, incidents, evidence, **overrides):
    monkeypatch.setattr(
        "app.current_incident_report_binder._audit_template",
        _fake_audit,
    )
    monkeypatch.setattr(
        "app.current_incident_report_binder._load_stale_factual_audit",
        _fake_stale_audit,
    )
    values = {
        "semantic_map": _semantic_map(),
        "inspection": _inspection(),
        "project_id": PROJECT_ID,
        "pipeline_job_id": PIPELINE_JOB_ID,
        "analysis_job_id": ANALYSIS_JOB_ID,
        "source_sha256": SOURCE_SHA256,
        "approved_incidents": incidents,
        "evidence_by_incident_id": {
            _resolved_incident_id(key): value
            for key, value in evidence.items()
        },
        "route_name": "테스트 고속도로",
        "inspection_date": "2026-07-29",
        "report_inspector_name": "현재 점검자",
        "locations_by_incident_id": {
            item["incident_id"]: f"{index + 1}.0km"
            for index, item in enumerate(incidents)
        },
        "report_policy": {
            "evidence_mode": "internal_original",
            "evaluation_ratings": RATINGS,
        },
    }
    values.update(overrides)
    if isinstance(values.get("locations_by_incident_id"), dict):
        values["locations_by_incident_id"] = {
            _resolved_incident_id(key): value
            for key, value in values[
                "locations_by_incident_id"
            ].items()
        }
    return bind_current_incidents_to_sample(**values)


def test_binding_is_deterministic_complete_and_expands_legacy_plan_atomically(
    monkeypatch,
):
    incidents = (_incident("incident-a"), _incident("incident-b"))
    evidence = {
        item["incident_id"]: _evidence(item["incident_id"])
        for item in incidents
    }

    first = _bind(monkeypatch, incidents, evidence)
    second = _bind(monkeypatch, incidents, evidence)

    assert first == second
    assert first.approved_incident_ids == (
        _resolved_incident_id("incident-a"),
        _resolved_incident_id("incident-b"),
    )
    assert tuple(
        item.incident_id for item in first.incident_bindings
    ) == first.approved_incident_ids
    assert len(
        {item.incident_id for item in first.incident_bindings}
    ) == first.approved_incident_count
    assert first.incident_capacity == 3
    assert first.approved_incident_bound is True
    assert first.content_provenance_bound is True
    assert first.writer_required is True
    assert first.weak_reference_approval_required is False
    assert first.required_writer_operation_count > 100
    assert first.legacy_document_edit_plan_operation_limit == 100
    assert first.exact_hwpx_writer_operation_limit == 5_000
    assert first.legacy_plan_expansion_required is True
    assert first.writer_batching_required is False
    assert any(
        item.role == "overlaid_stale_incident_photo"
        for item in first.neutralizations
    )
    assert any(
        item.role == "unassigned_stale_incident_photo"
        for item in first.neutralizations
    )
    assert all(
        len({target["target_id"] for target in row.grade_targets})
        == 5
        for row in first.evaluation_rows
    )
    public_json = json.dumps(first.public_metadata())
    assert "not-exposed" not in public_json
    assert "source_path" not in public_json


def test_all_static_factual_surfaces_use_current_values_or_neutralization(
    monkeypatch,
):
    incidents = (_incident("incident-a"),)
    result = _bind(
        monkeypatch,
        incidents,
        {"incident-a": _evidence("incident-a")},
    )
    static_replacements = tuple(
        mutation
        for mutation in result.replacements
        if mutation.target_id.startswith("static-")
    )
    static_neutralizations = tuple(
        mutation
        for mutation in result.neutralizations
        if mutation.target_id.startswith("static-")
    )

    assert len(static_replacements) == 8
    assert len(static_neutralizations) == 2
    assert {
        mutation.role for mutation in static_replacements
    } == {
        "current_title_group",
        "current_title_route_heading",
        "current_inspection_inspector",
        "current_photo_page_header",
    }
    assert all(
        mutation.current_value["text"] == "현재 점검"
        for mutation in static_replacements
        if mutation.role == "current_title_group"
    )
    assert all(
        mutation.current_value["text"]
        == "3분기 일반평가(불시점검) 양식(테스트 고속도로)"
        for mutation in static_replacements
        if mutation.role == "current_title_route_heading"
    )
    assert all(
        mutation.current_value["text"]
        == "점검자 : 현재 점검자 (인)"
        for mutation in static_replacements
        if mutation.role == "current_inspection_inspector"
    )
    assert all(
        mutation.current_value["text"]
        == "□ 테스트 고속도로 현황사진"
        for mutation in static_replacements
        if mutation.role == "current_photo_page_header"
    )
    assert all(
        mutation.role == "neutralized_stale_inspector_signature"
        and mutation.operation == "remove_picture"
        and mutation.current_value == {"neutralized": True}
        for mutation in static_neutralizations
    )

    unoccupied_route_date = tuple(
        mutation
        for mutation in result.replacements
        if mutation.page_pair_index == 1
        and mutation.role in {"inspection_route", "inspection_date"}
    )
    assert len(unoccupied_route_date) == 2
    assert not any(
        mutation.role
        in {"unassigned_stale_route", "unassigned_stale_date"}
        for mutation in result.neutralizations
    )
    unoccupied_opinion = next(
        mutation
        for mutation in result.neutralizations
        if mutation.page_pair_index == 1
        and mutation.role == "unassigned_stale_opinion"
    )
    assert unoccupied_opinion.current_value == {
        "text": "",
        "neutralized": True,
    }


def test_capacity_is_actual_and_never_silently_truncates(monkeypatch):
    incidents = tuple(
        _incident(f"incident-{index}") for index in range(4)
    )
    evidence = {
        item["incident_id"]: _evidence(item["incident_id"])
        for item in incidents
    }

    with pytest.raises(
        CurrentIncidentReportBindingError,
        match="photo-slot capacity",
    ) as caught:
        _bind(monkeypatch, incidents, evidence)

    assert caught.value.code == "sample_incident_capacity_exceeded"
    assert caught.value.writer_invoked is False


def test_unconfigured_report_inspector_fails_before_binding(monkeypatch):
    incident = _incident("incident-a")
    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            (incident,),
            {"incident-a": _evidence("incident-a")},
            report_inspector_name="unconfigured",
        )

    assert caught.value.code == "current_report_inspector_unconfigured"


def test_duplicate_and_cross_job_incidents_fail_closed(monkeypatch):
    duplicate = (_incident("incident-a"), _incident("incident-a"))
    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            duplicate,
            {"incident-a": _evidence("incident-a")},
        )
    assert caught.value.code == "approved_incident_duplicate"

    crossed = (
        _incident(
            "incident-a",
            analysis_job_id=str(PROJECT_ID),
        ),
    )
    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            crossed,
            {"incident-a": _evidence("incident-a")},
        )
    assert caught.value.code == "approved_incident_provenance_conflict"

    correct = (_incident("incident-a"),)
    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            correct,
            {
                "incident-a": _evidence(
                    "incident-a", pipeline_job_id=PROJECT_ID
                )
            },
        )
    assert (
        caught.value.code
        == "incident_evidence_provenance_conflict"
    )


def test_missing_user_approval_provenance_never_defaults_to_current_job(
    monkeypatch,
):
    incident = _incident("incident-a")
    incident.pop("pipeline_job_id")

    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            (incident,),
            {"incident-a": _evidence("incident-a")},
        )

    assert caught.value.code == "approved_incident_set_invalid"
    assert caught.value.apply_blocked is True

    llm_only = _incident(
        "incident-a",
        approval_actor="structured_llm_assisted",
    )
    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            (llm_only,),
            {"incident-a": _evidence("incident-a")},
        )
    assert caught.value.code == "approved_incident_provenance_conflict"


def test_missing_evidence_or_location_never_creates_partial_binding(
    monkeypatch,
):
    incidents = (_incident("incident-a"), _incident("incident-b"))
    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            incidents,
            {"incident-a": _evidence("incident-a")},
        )
    assert caught.value.code == "incident_evidence_missing"

    evidence = {
        item["incident_id"]: _evidence(item["incident_id"])
        for item in incidents
    }
    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            incidents,
            evidence,
            locations_by_incident_id={"incident-a": "1.0km"},
        )
    assert caught.value.code == "approved_incident_content_incomplete"


def test_sharing_requires_real_verified_deidentification(monkeypatch):
    incidents = (_incident("incident-a"),)
    shared_policy = {
        "evidence_mode": "shared_deidentified",
        "evaluation_ratings": RATINGS,
    }
    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            incidents,
            {"incident-a": _evidence("incident-a")},
            report_policy=shared_policy,
        )
    assert caught.value.code == "deidentified_evidence_required"

    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            incidents,
            {
                "incident-a": _evidence(
                    "incident-a",
                    verified_deidentified=True,
                    rename_only=True,
                )
            },
            report_policy=shared_policy,
        )
    assert caught.value.code == "deidentified_evidence_not_verified"

    result = _bind(
        monkeypatch,
        incidents,
        {
            "incident-a": _evidence(
                "incident-a", verified_deidentified=True
            )
        },
        report_policy=shared_policy,
    )
    assert (
        result.incident_bindings[0].evidence_policy
        == "shared_deidentified"
    )
    assert (
        result.incident_bindings[0].selected_evidence_sha256
        == "b" * 64
    )


def test_explicit_current_evaluation_matrix_is_mandatory(monkeypatch):
    incidents = (_incident("incident-a"),)
    evidence = {"incident-a": _evidence("incident-a")}

    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _bind(
            monkeypatch,
            incidents,
            evidence,
            report_policy={
                "evidence_mode": "internal_original",
                "evaluation_ratings": RATINGS[:-1],
            },
        )

    assert caught.value.code == "evaluation_matrix_incomplete"


def test_explicit_unassessed_clears_history_without_inventing_a_rating(
    monkeypatch,
):
    incidents = (_incident("incident-a"),)
    result = _bind(
        monkeypatch,
        incidents,
        {"incident-a": _evidence("incident-a")},
        report_policy={
            "evidence_mode": "internal_original",
            "evaluation_ratings": ("clear_unassessed",) * len(RATINGS),
        },
    )

    assert result.evaluation_rows
    assert all(
        row.selected_grade == "clear_unassessed"
        and row.selected_target_id is None
        for row in result.evaluation_rows
    )
    assert any(
        mutation.role == "stale_evaluation_marker"
        and mutation.current_value["text"] == ""
        for mutation in result.neutralizations
    )


def test_page_pair_specific_ratings_do_not_leak_between_groups(
    monkeypatch,
):
    incidents = tuple(
        _incident(f"incident-{index}") for index in range(3)
    )
    result = _bind(
        monkeypatch,
        incidents,
        {
            item["incident_id"]: _evidence(item["incident_id"])
            for item in incidents
        },
        report_policy={
            "evidence_mode": "internal_original",
            "evaluation_ratings": {
                "0": ("clear_unassessed",) * len(RATINGS),
                "1": RATINGS,
            },
        },
    )

    first_pair = tuple(
        row for row in result.evaluation_rows
        if row.page_pair_index == 0
    )
    second_pair = tuple(
        row for row in result.evaluation_rows
        if row.page_pair_index == 1
    )
    assert first_pair and second_pair
    assert all(row.selected_target_id is None for row in first_pair)
    assert all(row.selected_target_id is not None for row in second_pair)


def _cell(
    target: str,
    row: int,
    column: int,
    text: str = "",
    *,
    column_span: int = 1,
):
    return SimpleNamespace(
        target_id=target,
        row=row,
        column=column,
        row_span=1,
        column_span=column_span,
        text_excerpt=text,
        text_sha256=(target[0] if target else "0") * 64,
    )


def test_merged_grade_row_is_classified_without_copying_old_marker():
    cells = [
        _cell("route", 32, 4, "노선명 : 과거 노선"),
        _cell("date", 31, 4, "점검일 : 2025.01.01."),
        _cell("opinion-label", 29, 1, "점검의견"),
        _cell("opinion", 30, 1, "과거 의견"),
    ]
    header_specs = (
        ("very-good", 3, 2, "매우 우수"),
        ("good", 5, 1, "우수"),
        ("normal", 6, 2, "보통"),
        ("poor", 8, 2, "미흡"),
        ("very-poor", 10, 1, "매우 미흡"),
    )
    cells.extend(
        _cell(target, 1, column, text, column_span=span)
        for target, column, span, text in header_specs
    )
    for row in range(2, 22):
        cells.append(_cell(f"label-{row}", row, 1, f"평가 {row}"))
        if row == 2:
            cells.extend(
                (
                    _cell("merged-a", row, 3, "■", column_span=2),
                    _cell("merged-b", row, 5, "", column_span=5),
                    _cell("merged-c", row, 10, ""),
                )
            )
        else:
            cells.extend(
                _cell(
                    f"grade-{row}-{index}",
                    row,
                    column,
                    column_span=span,
                )
                for index, (column, span) in enumerate(
                    ((3, 2), (5, 2), (7, 2), (9, 1), (10, 1))
                )
            )
    inspection = SimpleNamespace(
        snapshot=SimpleNamespace(
            tables=(
                SimpleNamespace(cells=()),
                SimpleNamespace(cells=tuple(cells)),
            )
        )
    )

    page = _classify_page_pair(inspection, 0)

    assert len(page.evaluation_rows) == 20
    assert page.evaluation_rows[0].five_grade_grid_available is False
    assert len(
        {
            target["target_id"]
            for target in page.evaluation_rows[0].grade_targets
        }
    ) == 3
    assert page.old_marker_targets[0]["target_id"] == "merged-a"


def test_five_grade_choice_for_merged_row_is_explicitly_blocked(
    monkeypatch,
):
    slots, pages = _fake_audit()
    first = pages[0].evaluation_rows[0]
    merged_targets = tuple(
        {
            **target,
            "target_id": (
                "merged-middle"
                if target["grade"] in {"good", "normal", "poor"}
                else target["target_id"]
            ),
        }
        for target in first.grade_targets
    )
    merged = _EvaluationRow(
        row_order=first.row_order,
        row=first.row,
        label_sha256=first.label_sha256,
        five_grade_grid_available=False,
        grade_targets=merged_targets,
    )
    patched_page = _PagePair(
        **{
            **pages[0].__dict__,
            "evaluation_rows": (
                merged,
                *pages[0].evaluation_rows[1:],
            ),
        }
    )
    monkeypatch.setattr(
        "app.current_incident_report_binder._audit_template",
        lambda *_args: (slots, (patched_page, pages[1])),
    )
    monkeypatch.setattr(
        "app.current_incident_report_binder._load_stale_factual_audit",
        _fake_stale_audit,
    )
    incidents = (_incident("incident-a"),)
    incident_id = incidents[0]["incident_id"]
    evidence = {incident_id: _evidence("incident-a")}

    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        bind_current_incidents_to_sample(
            semantic_map=_semantic_map(),
            inspection=_inspection(),
            project_id=PROJECT_ID,
            pipeline_job_id=PIPELINE_JOB_ID,
            analysis_job_id=ANALYSIS_JOB_ID,
            source_sha256=SOURCE_SHA256,
            approved_incidents=incidents,
            evidence_by_incident_id=evidence,
            route_name="테스트 고속도로",
            inspection_date="2026-07-29",
            report_inspector_name="현재 점검자",
            locations_by_incident_id={incident_id: "1.0km"},
            report_policy={
                "evidence_mode": "internal_original",
                "evaluation_ratings": RATINGS,
            },
        )
    assert caught.value.code == "evaluation_grid_unavailable"

    cleared_ratings = ("clear_unassessed", *RATINGS[1:])
    result = bind_current_incidents_to_sample(
        semantic_map=_semantic_map(),
        inspection=_inspection(),
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        approved_incidents=incidents,
        evidence_by_incident_id=evidence,
        route_name="테스트 고속도로",
        inspection_date="2026-07-29",
        report_inspector_name="현재 점검자",
        locations_by_incident_id={incident_id: "1.0km"},
        report_policy={
            "evidence_mode": "internal_original",
            "evaluation_ratings": cleared_ratings,
        },
    )
    assert result.evaluation_rows[0].selected_target_id is None
    assert result.evaluation_rows[0].selected_grade == "clear_unassessed"


def test_unclassified_stale_opinion_keeps_apply_blocked():
    inspection = SimpleNamespace(
        snapshot=SimpleNamespace(
            tables=(
                SimpleNamespace(cells=()),
                SimpleNamespace(
                    cells=(
                        _cell("route", 32, 4, "노선명 : 과거 노선"),
                        _cell("date", 31, 4, "점검일 : 2025.01.01."),
                        _cell("opinion-label", 29, 1, "점검의견"),
                    )
                ),
            )
        )
    )

    with pytest.raises(CurrentIncidentReportBindingError) as caught:
        _classify_page_pair(inspection, 0)

    assert (
        caught.value.code
        == "stale_sample_content_neutralization_unavailable"
    )
    assert caught.value.apply_blocked is True
