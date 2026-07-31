from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image

from app.current_incident_hwpx_plan import (
    ServerOwnedEvidenceFile,
    build_exact_hwpx_write_plan,
)
from app.current_incident_report_binder import (
    _audit_template,
    bind_current_incidents_to_sample,
)
from app.current_report_document_edit_plan import (
    CURRENT_REPORT_DOCUMENT_EDIT_PLAN_STATE,
    CurrentReportDocumentEditPlanError,
    build_current_report_document_edit_plan,
    validate_current_report_document_edit_plan,
)
from app.hwpx_exact_writer import (
    ExactHwpxTextOperation,
)
from app.hwpx_sample_layout import inspect_sample_layout
from app.pipeline_report_preview import (
    SampleTemplateSemanticMapRegistry,
)
from approved_incident_authority_fixture import (
    fixture_approval_record_id,
    fixture_incident_id,
    fixture_output_authority,
)


ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = ROOT.parent / "sample"
SAMPLE_3Q = (SAMPLE_ROOT / "3분기 점검 내용 정리.hwpx").resolve()
SAMPLE_4Q = (SAMPLE_ROOT / "4분기 점검 내용 정리.hwpx").resolve()
PROJECT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PIPELINE_JOB_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ANALYSIS_JOB_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SOURCE_SHA256 = "d" * 64
INCIDENT_LABEL = "current-video-one"
INCIDENT_ID = fixture_incident_id(
    INCIDENT_LABEL,
    ANALYSIS_JOB_ID,
)
EVIDENCE_REF = f"original:{INCIDENT_ID}"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_context(
    work: Path,
    *,
    sample_path: Path = SAMPLE_3Q,
    template_id: str = "sample-3q-v1",
    inspection_date: str = "2026-07-29",
):
    evidence_root = (work / "server-evidence").resolve()
    staging_root = (work / "staged").resolve()
    evidence_root.mkdir(parents=True)
    staging_root.mkdir(parents=True)
    evidence_path = (evidence_root / "current-frame.png").resolve()
    Image.new("RGB", (640, 360), (20, 110, 170)).save(
        evidence_path, format="PNG"
    )
    evidence_sha256 = _sha(evidence_path)
    approval_record_id = fixture_approval_record_id(
        INCIDENT_LABEL,
        PIPELINE_JOB_ID,
    )

    inspection = inspect_sample_layout(sample_path)
    semantic_map = SampleTemplateSemanticMapRegistry(
        SAMPLE_ROOT.resolve()
    ).resolve(
        template_id=template_id,
        template_sha256=inspection.snapshot.document_sha256,
    )
    _slots, pages = _audit_template(semantic_map, inspection)
    ratings = tuple(
        "normal"
        if row.five_grade_grid_available
        else "clear_unassessed"
        for row in pages[0].evaluation_rows
    )
    binding = bind_current_incidents_to_sample(
        semantic_map=semantic_map,
        inspection=inspection,
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
        approved_incidents=(
            {
                "incident_id": INCIDENT_ID,
                "project_id": str(PROJECT_ID),
                "pipeline_job_id": str(PIPELINE_JOB_ID),
                "analysis_job_id": str(ANALYSIS_JOB_ID),
                "source_sha256": SOURCE_SHA256,
                "review_status": "approved",
                "approval_actor": "local_user",
                "approval_record_id": approval_record_id,
                "approval_record_sha256": "e" * 64,
                "incident_type": "pothole",
                "maintenance_action": "포트홀 보수 필요",
                "severity": "moderate",
                "ocr_text": "",
                "time_ms": 1_000,
                "evidence_refs": [EVIDENCE_REF],
                "approved_output_authority": (
                    fixture_output_authority(
                        label=INCIDENT_LABEL,
                        project_id=PROJECT_ID,
                        pipeline_job_id=PIPELINE_JOB_ID,
                        analysis_job_id=ANALYSIS_JOB_ID,
                        source_sha256=SOURCE_SHA256,
                        approval_record_id=approval_record_id,
                        approval_record_sha256="e" * 64,
                        representative_image_ref=EVIDENCE_REF,
                        representative_image_sha256=evidence_sha256,
                    )
                ),
            },
        ),
        evidence_by_incident_id={
            INCIDENT_ID: {
                "incident_id": INCIDENT_ID,
                "project_id": str(PROJECT_ID),
                "pipeline_job_id": str(PIPELINE_JOB_ID),
                "analysis_job_id": str(ANALYSIS_JOB_ID),
                "source_sha256": SOURCE_SHA256,
                "original_ref": EVIDENCE_REF,
                "original_sha256": evidence_sha256,
                "deidentified_ref": None,
                "deidentified_sha256": None,
                "deidentification_record_sha256": None,
                "deidentified_verified": False,
            }
        },
        route_name="테스트고속도로",
        inspection_date=inspection_date,
        report_inspector_name="현재 점검자",
        locations_by_incident_id={INCIDENT_ID: "상행 1.0km"},
        report_policy={
            "evidence_mode": "internal_original",
            "evaluation_ratings": ratings,
        },
    )
    plan = build_exact_hwpx_write_plan(
        source_path=sample_path,
        binding_manifest=binding,
        evidence_by_ref={
            EVIDENCE_REF: ServerOwnedEvidenceFile(
                evidence_ref=EVIDENCE_REF,
                absolute_path=str(evidence_path),
                sha256=evidence_sha256,
            )
        },
        server_evidence_root=evidence_root,
        transcode_root=staging_root,
    )
    return {
        "binding": binding,
        "plan": plan,
        "staging_root": staging_root,
        "source_path": sample_path,
    }


@pytest.fixture(scope="module")
def exact_context(tmp_path_factory):
    return _exact_context(
        tmp_path_factory.mktemp(
            "current-report-document-edit-plan"
        )
    )


def _build_review(context, **overrides):
    arguments = {
        "source_path": context["source_path"],
        "binding_manifest": context["binding"],
        "exact_plan": context["plan"],
        "staged_evidence_root": context["staging_root"],
        "project_id": PROJECT_ID,
        "pipeline_job_id": PIPELINE_JOB_ID,
        "analysis_job_id": ANALYSIS_JOB_ID,
        "source_sha256": SOURCE_SHA256,
    }
    arguments.update(overrides)
    return build_current_report_document_edit_plan(**arguments)


def test_exact_plan_exposes_server_owned_addresses_and_current_provenance(
    exact_context,
):
    review = _build_review(exact_context)
    validate_current_report_document_edit_plan(
        review,
        binding_manifest=exact_context["binding"],
        exact_plan=exact_context["plan"],
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        source_sha256=SOURCE_SHA256,
    )

    assert review.state == CURRENT_REPORT_DOCUMENT_EDIT_PLAN_STATE
    assert review.template_sha256 == (
        exact_context["binding"].template_sha256
    )
    assert review.template_quarter == 3
    assert len(review.operations) == len(
        exact_context["plan"].operations
    )
    assert review.provider_may_select_target_addresses is False
    assert review.apply_approval_required is True
    assert review.provider_invocation_count == 0
    assert review.exact_writer_invocation_count == 0
    assert review.com_invocation_count == 0
    assert review.document_written is False

    provenance = review.approved_incidents[0]
    incident_binding = exact_context["binding"].incident_bindings[0]
    assert provenance.incident_id == INCIDENT_ID
    assert provenance.approval_actor == "local_user"
    assert provenance.approval_record_id == (
        fixture_approval_record_id(
            INCIDENT_LABEL,
            PIPELINE_JOB_ID,
        )
    )
    assert provenance.approval_record_sha256 == "e" * 64
    assert provenance.photo_slot_id == (
        incident_binding.photo_slot_id
    )
    assert provenance.selected_evidence_sha256 == (
        incident_binding.selected_evidence_sha256
    )

    photo = next(
        operation
        for operation in review.operations
        if "incident_photo" in operation.semantic_roles
    )
    assert photo.target.target_kind == "picture"
    assert photo.target.page_number == 2
    assert photo.target.table_index == 2
    assert photo.target.cell_target_id == (
        incident_binding.photo_cell_target_id
    )
    assert photo.target.cell_index is not None
    assert photo.target.row is not None
    assert photo.target.column is not None
    assert photo.target.row_span is not None
    assert photo.target.column_span is not None
    assert photo.target.photo_slot_ids == (
        incident_binding.photo_slot_id,
    )
    assert photo.approved_incident_ids == (INCIDENT_ID,)

    opinion = next(
        operation
        for operation in review.operations
        if "inspection_opinion" in operation.semantic_roles
    )
    assert opinion.opinion_target is True
    assert opinion.target.page_number == 1
    assert opinion.target.table_index == 1
    assert opinion.target.cell_target_id == opinion.target.target_id
    assert opinion.target.cell_index is not None
    assert opinion.target.row is not None
    assert opinion.target.column is not None
    assert "포트홀 보수 필요" in (opinion.proposed_text or "")

    selected_checkbox = next(
        operation
        for operation in review.operations
        if any(
            checkbox.selected
            for checkbox in operation.checkbox_targets
        )
    )
    checkbox = next(
        checkbox
        for checkbox in selected_checkbox.checkbox_targets
        if checkbox.selected
    )
    assert checkbox.grade == "normal"
    assert selected_checkbox.target.cell_target_id == (
        selected_checkbox.target.target_id
    )
    assert selected_checkbox.target.cell_index is not None
    assert checkbox.anchor_column == (
        selected_checkbox.target.column
    )
    assert selected_checkbox.proposed_text == "■"


def test_unused_historical_slots_are_explicitly_neutralized_and_path_free(
    exact_context,
):
    review = _build_review(exact_context)
    unused = tuple(
        operation
        for operation in review.operations
        if operation.unused_sample_factual_slot_neutralized
    )

    assert review.stale_sample_factual_target_count == 120
    assert review.stale_sample_factual_target_coverage_complete is True
    assert len(unused) == len(
        review.unused_sample_neutralization_operation_ids
    )
    assert {item.operation_id for item in unused} == set(
        review.unused_sample_neutralization_operation_ids
    )
    assert any(
        "unassigned_stale_incident_photo"
        in operation.semantic_roles
        for operation in unused
    )
    assert any(
        "unassigned_stale_incident_caption"
        in operation.semantic_roles
        for operation in unused
    )
    assert any(
        "unassigned_stale_opinion"
        in operation.semantic_roles
        for operation in unused
    )
    assert all(
        operation.proposed_text is None
        if operation.target.target_kind != "picture"
        else operation.proposed_evidence_sha256 is not None
        for operation in unused
    )

    serialized = json.dumps(
        review.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
    )
    assert str(exact_context["staging_root"]) not in serialized
    assert "evidence_path" not in serialized


def test_review_and_decision_hashes_are_deterministic(exact_context):
    first = _build_review(exact_context)
    second = _build_review(exact_context)

    assert second == first
    assert len(first.document_edit_plan_sha256) == 64
    assert len(first.accept_reject_rollback_binding_sha256) == 64
    assert first.plan_id.endswith(
        first.document_edit_plan_sha256[:32]
    )
    assert (
        first.accept_reject_rollback_binding_sha256
        != first.document_edit_plan_sha256
    )


def test_same_wrapper_accepts_the_exact_4q_sample(tmp_path):
    context = _exact_context(
        tmp_path / "fourth-quarter",
        sample_path=SAMPLE_4Q,
        template_id="sample-4q-v1",
        inspection_date="2026-10-29",
    )

    review = _build_review(context)

    assert review.template_quarter == 4
    assert review.template_id == "sample-4q-v1"
    assert review.template_sha256 == _sha(SAMPLE_4Q)
    assert len(review.operations) == len(context["plan"].operations)
    incident_binding = context["binding"].incident_bindings[0]
    photo = next(
        operation
        for operation in review.operations
        if "incident_photo" in operation.semantic_roles
    )
    assert photo.target.cell_target_id == (
        incident_binding.photo_cell_target_id
    )
    assert photo.target.cell_index is not None
    assert review.com_invocation_count == 0


def test_cross_job_context_is_rejected_before_review(exact_context):
    with pytest.raises(
        CurrentReportDocumentEditPlanError
    ) as caught:
        _build_review(
            exact_context,
            pipeline_job_id=UUID(
                "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            ),
        )

    assert caught.value.code == "cross_job_report_plan"
    assert caught.value.provider_called is False
    assert caught.value.writer_invoked is False
    assert caught.value.com_invoked is False
    assert caught.value.apply_blocked is True


def test_stale_exact_plan_and_unneutralized_placeholder_are_blocked(
    exact_context,
):
    stale = replace(
        exact_context["plan"],
        source_document_sha256="0" * 64,
    )
    with pytest.raises(
        CurrentReportDocumentEditPlanError
    ) as caught:
        _build_review(exact_context, exact_plan=stale)
    assert caught.value.code == "stale_exact_plan"

    binding = exact_context["binding"]
    target_id = next(
        mutation.target_id
        for mutation in binding.neutralizations
        if mutation.role == "unassigned_stale_incident_caption"
    )
    changed = tuple(
        replace(operation, value="과거 표본 문구")
        if (
            operation.target_id == target_id
            and isinstance(operation, ExactHwpxTextOperation)
        )
        else operation
        for operation in exact_context["plan"].operations
    )
    unneutralized = replace(
        exact_context["plan"],
        operations=changed,
    )
    with pytest.raises(
        CurrentReportDocumentEditPlanError
    ) as caught:
        _build_review(
            exact_context,
            exact_plan=unneutralized,
        )
    assert caught.value.code == "stale_sample_content"
