from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from app.current_incident_report_binder import (
    CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
    CurrentIncidentReportBindingManifest,
    IncidentSlotBinding,
)
from app.current_report_approval import (
    CurrentReportApprovalError,
    CurrentReportApprovalRegistry,
)
from app.hwpx_exact_writer import (
    ExactHwpxTextOperation,
    ExactHwpxTargetVerification,
    ExactHwpxWritePlan,
    ExactHwpxWriteResult,
    HWPX_EXACT_WRITER_SCHEMA,
)
from current_report_document_plan_fixture import (
    document_edit_plan_fixture,
)


PROJECT_ID = UUID("00000000-0000-0000-0000-000000000001")
PIPELINE_JOB_ID = UUID("00000000-0000-0000-0000-000000000002")
ANALYSIS_JOB_ID = UUID("00000000-0000-0000-0000-000000000003")
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _binding() -> CurrentIncidentReportBindingManifest:
    incident = IncidentSlotBinding(
        incident_id="incident-1",
        incident_order=0,
        page_pair_index=0,
        inspection_page_number=1,
        photo_page_number=2,
        photo_slot_id="slot-1",
        photo_cell_target_id="cell-photo-1",
        primary_picture_target_id="picture-1",
        additional_picture_target_ids=(),
        caption_cell_target_id="cell-caption-1",
        selected_evidence_sha256=SHA_A,
        selected_evidence_ref="evidence-1",
        evidence_policy="internal_original",
        approved_incident_authority_sha256=SHA_C,
        photo_binding_sha256=SHA_B,
    )
    return CurrentIncidentReportBindingManifest(
        schema_version=CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
        state="current_incidents_bound_writer_required",
        project_id=str(PROJECT_ID),
        pipeline_job_id=str(PIPELINE_JOB_ID),
        analysis_job_id=str(ANALYSIS_JOB_ID),
        source_sha256=SHA_A,
        template_id="template-3q",
        document_snapshot_id="snapshot-1",
        template_sha256=SHA_B,
        template_semantic_map_sha256=SHA_C,
        structure_target_summary_sha256=SHA_D,
        incident_capacity=123,
        approved_incident_count=1,
        approved_incident_ids=("incident-1",),
        occupied_page_pair_indices=(0,),
        incident_bindings=(incident,),
        replacements=(),
        neutralizations=(),
        evaluation_rows=(),
        survey_incident_authority_sha256=SHA_A,
        approved_incident_authority_set_sha256=SHA_B,
        input_sha256=SHA_A,
        content_sha256=SHA_B,
        slot_manifest_sha256=SHA_C,
        binding_manifest_sha256=SHA_D,
        required_writer_operation_count=1,
        legacy_document_edit_plan_operation_limit=100,
        exact_hwpx_writer_operation_limit=5_000,
        legacy_plan_expansion_required=False,
        writer_batching_required=False,
    )


def _plan() -> ExactHwpxWritePlan:
    return ExactHwpxWritePlan(
        plan_id="current-report-plan-1",
        source_document_sha256=SHA_B,
        source_target_summary_sha256=SHA_D,
        incident_source_refs=("incident:incident-1",),
        operations=(
            ExactHwpxTextOperation(
                operation_id="op-1",
                target_id="cell-1",
                expected_current_value="과거",
                expected_current_sha256=SHA_A,
                value="현재",
                source_ref="incident:incident-1",
            ),
        ),
    )


def _writer_result(path: Path, plan: ExactHwpxWritePlan):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ExactHwpxWriteResult(
        schema_version=HWPX_EXACT_WRITER_SCHEMA,
        state="writer_completed_com_validation_required",
        plan_id=plan.plan_id,
        source_document_sha256=plan.source_document_sha256,
        output_document_sha256=digest,
        output_path=str(path),
        operation_count=1,
        target_verifications=(
            ExactHwpxTargetVerification(
                operation_id="op-1",
                target_id="cell-1",
                kind="replace_cell_text",
                source_ref="incident:incident-1",
                output_value_sha256=SHA_B,
            ),
        ),
        table_count=72,
        expected_rendered_page_count=48,
        output_rendered_page_count=None,
        page_contract_proxy_verified=True,
        output_render_validation_status=(
            "not_run_com_validation_required"
        ),
        structure_projection_sha256=SHA_C,
        source_unchanged=True,
        mimetype_first_and_stored=True,
    )


def _registry(root: Path, *, clock=lambda: 100.0):
    return CurrentReportApprovalRegistry(
        owned_preview_root=root,
        clock=clock,
        token_factory=lambda: "f" * 64,
    )


def _issue(
    registry,
    binding,
    plan,
    result,
    document_edit_plan=None,
):
    return registry.issue(
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        binding=binding,
        plan=plan,
        document_edit_plan=(
            document_edit_plan
            or document_edit_plan_fixture(binding, plan)
        ),
        writer_result=result,
    )


def _consume(
    registry,
    token,
    binding,
    plan,
    result,
    document_edit_plan=None,
    expected_document_edit_plan_sha256=None,
    expected_decision_binding_sha256=None,
):
    active_document_plan = (
        document_edit_plan
        or document_edit_plan_fixture(binding, plan)
    )
    return registry.consume(
        token,
        project_id=PROJECT_ID,
        pipeline_job_id=PIPELINE_JOB_ID,
        analysis_job_id=ANALYSIS_JOB_ID,
        binding=binding,
        plan=plan,
        document_edit_plan=active_document_plan,
        expected_document_edit_plan_sha256=(
            expected_document_edit_plan_sha256
            or active_document_plan.document_edit_plan_sha256
        ),
        expected_accept_reject_rollback_binding_sha256=(
            expected_decision_binding_sha256
            or active_document_plan
            .accept_reject_rollback_binding_sha256
        ),
        writer_result=result,
    )


def test_issue_and_consume_bind_exact_owned_preview(tmp_path):
    preview = tmp_path / "preview.hwpx"
    preview.write_bytes(b"PK\x03\x04exact-current-report")
    binding = _binding()
    plan = _plan()
    result = _writer_result(preview, plan)
    registry = _registry(tmp_path)
    document_plan = document_edit_plan_fixture(binding, plan)

    grant = _issue(
        registry, binding, plan, result, document_plan
    )
    authorization = _consume(
        registry,
        grant.token,
        binding,
        plan,
        result,
        document_plan,
    )

    assert grant.exact_preview_sha256 == result.output_document_sha256
    assert grant.document_edit_plan_sha256 == (
        document_plan.document_edit_plan_sha256
    )
    assert grant.accept_reject_rollback_binding_sha256 == (
        document_plan.accept_reject_rollback_binding_sha256
    )
    assert authorization.approval.granted is True
    assert authorization.approval.approval_id == "f" * 64
    assert (
        authorization.exact_preview_fingerprint.sha256
        == result.output_document_sha256
    )
    assert authorization.document_edit_plan_sha256 == (
        document_plan.document_edit_plan_sha256
    )


def test_changed_document_plan_burns_one_shot_approval(tmp_path):
    preview = tmp_path / "preview.hwpx"
    preview.write_bytes(b"PK\x03\x04exact-current-report")
    binding = _binding()
    plan = _plan()
    result = _writer_result(preview, plan)
    document_plan = document_edit_plan_fixture(binding, plan)
    registry = _registry(tmp_path)
    token = _issue(
        registry, binding, plan, result, document_plan
    ).token
    changed = replace(
        document_plan,
        accept_reject_rollback_binding_sha256="0" * 64,
    )

    with pytest.raises(CurrentReportApprovalError):
        _consume(
            registry,
            token,
            binding,
            plan,
            result,
            changed,
        )
    with pytest.raises(CurrentReportApprovalError) as caught:
        _consume(
            registry,
            token,
            binding,
            plan,
            result,
            document_plan,
        )

    assert "이미 사용" in str(caught.value)


def test_wrong_reviewed_optimistic_hash_burns_approval(tmp_path):
    preview = tmp_path / "preview.hwpx"
    preview.write_bytes(b"PK\x03\x04exact-current-report")
    binding = _binding()
    plan = _plan()
    result = _writer_result(preview, plan)
    document_plan = document_edit_plan_fixture(binding, plan)
    registry = _registry(tmp_path)
    token = _issue(
        registry, binding, plan, result, document_plan
    ).token

    with pytest.raises(CurrentReportApprovalError):
        _consume(
            registry,
            token,
            binding,
            plan,
            result,
            document_plan,
            expected_document_edit_plan_sha256="0" * 64,
        )
    with pytest.raises(CurrentReportApprovalError) as caught:
        _consume(
            registry,
            token,
            binding,
            plan,
            result,
            document_plan,
        )

    assert "이미 사용" in str(caught.value)


def test_token_is_burned_before_changed_preview_check(tmp_path):
    preview = tmp_path / "preview.hwpx"
    preview.write_bytes(b"PK\x03\x04exact-current-report")
    binding = _binding()
    plan = _plan()
    result = _writer_result(preview, plan)
    registry = _registry(tmp_path)
    token = _issue(registry, binding, plan, result).token
    preview.write_bytes(b"PK\x03\x04changed-current-report")

    with pytest.raises(CurrentReportApprovalError):
        _consume(registry, token, binding, plan, result)
    with pytest.raises(CurrentReportApprovalError) as caught:
        _consume(registry, token, binding, plan, result)

    assert "이미 사용" in str(caught.value)


def test_rejects_preview_outside_owned_root(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    outside = tmp_path / "outside.hwpx"
    outside.write_bytes(b"PK\x03\x04outside")
    binding = _binding()
    plan = _plan()

    with pytest.raises(CurrentReportApprovalError) as caught:
        _issue(
            _registry(owned),
            binding,
            plan,
            _writer_result(outside, plan),
        )

    assert "서버 소유 경로 밖" in str(caught.value)


def test_rejects_unfinished_writer_state_without_issuing_token(tmp_path):
    preview = tmp_path / "preview.hwpx"
    preview.write_bytes(b"PK\x03\x04exact-current-report")
    binding = _binding()
    plan = _plan()
    result = replace(
        _writer_result(preview, plan),
        page_contract_proxy_verified=False,
    )

    with pytest.raises(CurrentReportApprovalError) as caught:
        _issue(_registry(tmp_path), binding, plan, result)

    assert "검증이 완료되지" in str(caught.value)


def test_expired_token_is_one_shot(tmp_path):
    now = [100.0]
    preview = tmp_path / "preview.hwpx"
    preview.write_bytes(b"PK\x03\x04exact-current-report")
    binding = _binding()
    plan = _plan()
    result = _writer_result(preview, plan)
    registry = CurrentReportApprovalRegistry(
        owned_preview_root=tmp_path,
        ttl_seconds=5,
        clock=lambda: now[0],
        token_factory=lambda: "e" * 64,
    )
    token = _issue(registry, binding, plan, result).token
    now[0] = 106.0

    with pytest.raises(CurrentReportApprovalError) as caught:
        _consume(registry, token, binding, plan, result)

    assert "만료" in str(caught.value)


def test_rejects_cross_job_use_and_burns_token(tmp_path):
    preview = tmp_path / "preview.hwpx"
    preview.write_bytes(b"PK\x03\x04exact-current-report")
    binding = _binding()
    plan = _plan()
    result = _writer_result(preview, plan)
    registry = _registry(tmp_path)
    token = _issue(registry, binding, plan, result).token

    with pytest.raises(CurrentReportApprovalError):
        registry.consume(
            token,
            project_id=PROJECT_ID,
            pipeline_job_id=UUID(
                "00000000-0000-0000-0000-000000000099"
            ),
            analysis_job_id=ANALYSIS_JOB_ID,
            binding=binding,
            plan=plan,
            document_edit_plan=(
                document_edit_plan_fixture(binding, plan)
            ),
            expected_document_edit_plan_sha256=(
                document_edit_plan_fixture(binding, plan)
                .document_edit_plan_sha256
            ),
            expected_accept_reject_rollback_binding_sha256=(
                document_edit_plan_fixture(binding, plan)
                .accept_reject_rollback_binding_sha256
            ),
            writer_result=result,
        )
    with pytest.raises(CurrentReportApprovalError) as caught:
        _consume(registry, token, binding, plan, result)

    assert "이미 사용" in str(caught.value)
