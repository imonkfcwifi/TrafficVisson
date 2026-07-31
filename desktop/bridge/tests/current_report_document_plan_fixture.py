from __future__ import annotations

from dataclasses import asdict

from app.current_incident_report_binder import (
    CurrentIncidentReportBindingManifest,
)
from app.current_report_document_edit_plan import (
    CURRENT_REPORT_DOCUMENT_EDIT_PLAN_SCHEMA,
    CURRENT_REPORT_DOCUMENT_EDIT_PLAN_STATE,
    ApprovedIncidentDocumentProvenance,
    CurrentReportDocumentEditPlan,
    ExactDocumentEditReviewOperation,
    ExactDocumentTargetAddress,
    _canonical_sha256,
    _decision_binding_sha256,
    _exact_plan_sha256,
    _plan_identity_payload,
    _text_sha256,
)
from app.hwpx_exact_writer import (
    ExactHwpxParagraphTextOperation,
    ExactHwpxPictureOperation,
    ExactHwpxTextOperation,
    ExactHwpxWritePlan,
)


def document_edit_plan_fixture(
    binding: CurrentIncidentReportBindingManifest,
    exact_plan: ExactHwpxWritePlan,
) -> CurrentReportDocumentEditPlan:
    approved = []
    for item in binding.incident_bindings:
        provenance = {
            "incident_id": item.incident_id,
            "incident_order": item.incident_order,
            "project_id": binding.project_id,
            "pipeline_job_id": binding.pipeline_job_id,
            "analysis_job_id": binding.analysis_job_id,
            "source_sha256": binding.source_sha256,
            "approval_actor": "local_user",
            "approval_record_id": f"review-{item.incident_id}",
            "approval_record_sha256": "9" * 64,
            "selected_evidence_ref": item.selected_evidence_ref,
            "selected_evidence_sha256": (
                item.selected_evidence_sha256
            ),
            "evidence_policy": item.evidence_policy,
            "photo_slot_id": item.photo_slot_id,
            "photo_binding_sha256": item.photo_binding_sha256,
        }
        approved.append(
            ApprovedIncidentDocumentProvenance(
                **provenance,
                provenance_sha256=_canonical_sha256(provenance),
            )
        )
    approved_tuple = tuple(approved)
    review_operations = []
    for index, operation in enumerate(exact_plan.operations):
        if isinstance(
            operation,
            (ExactHwpxTextOperation, ExactHwpxParagraphTextOperation),
        ):
            proposed_text = operation.value
            proposed_evidence_sha256 = None
            proposed_value_sha256 = _text_sha256(operation.value)
        elif isinstance(operation, ExactHwpxPictureOperation):
            proposed_text = None
            proposed_evidence_sha256 = operation.evidence_sha256
            proposed_value_sha256 = operation.evidence_sha256
        else:
            raise AssertionError("unsupported exact operation fixture")
        incident_ids = tuple(
            source_ref.removeprefix("incident:")
            for source_ref in (operation.source_ref,)
            if source_ref.startswith("incident:")
        )
        review_operations.append(
            ExactDocumentEditReviewOperation(
                operation_id=operation.operation_id,
                exact_operation_kind=operation.kind,
                semantic_roles=("inspection_opinion",),
                binding_groups=("replacement",),
                target=ExactDocumentTargetAddress(
                    target_id=operation.target_id,
                    target_kind="fixture_exact_target",
                    page_pair_index=0,
                    page_number=1,
                    section_path="Contents/section0.xml",
                    section_index=0,
                    table_id="fixture-table",
                    table_index=0,
                    cell_target_id=(
                        operation.target_id
                        if operation.target_id.startswith("hwpx-cell-")
                        else None
                    ),
                    cell_index=index,
                    row=0,
                    column=index,
                    row_span=1,
                    column_span=1,
                    picture_index=None,
                    paragraph_address_sha256=None,
                    photo_slot_ids=(),
                ),
                expected_sample_value_sha256=(
                    operation.expected_current_sha256
                ),
                proposed_value_sha256=proposed_value_sha256,
                proposed_text=proposed_text,
                proposed_evidence_sha256=(
                    proposed_evidence_sha256
                ),
                current_source_refs=(operation.source_ref,),
                approved_incident_ids=incident_ids,
                checkbox_targets=(),
                opinion_target=True,
                neutralizes_historical_sample_fact=False,
                unused_sample_factual_slot_neutralized=False,
            )
        )
    operations = tuple(review_operations)
    exact_sha256 = _exact_plan_sha256(exact_plan)
    approved_sha256 = _canonical_sha256(
        [asdict(item) for item in approved_tuple]
    )
    identity = _plan_identity_payload(
        project_id=binding.project_id,
        pipeline_job_id=binding.pipeline_job_id,
        analysis_job_id=binding.analysis_job_id,
        source_sha256=binding.source_sha256,
        template_id=binding.template_id,
        template_quarter=3,
        document_snapshot_id=binding.document_snapshot_id,
        template_sha256=binding.template_sha256,
        structure_target_summary_sha256=(
            binding.structure_target_summary_sha256
        ),
        binding_manifest_sha256=binding.binding_manifest_sha256,
        exact_plan_sha256=exact_sha256,
        approved_incident_set_sha256=approved_sha256,
        approved_incidents=approved_tuple,
        operations=operations,
        stale_sample_factual_target_count=0,
        unused_sample_neutralization_operation_ids=(),
        address_authority="server_audited_sample_layout",
        factual_authority="current_local_user_approved_incidents",
    )
    document_sha256 = _canonical_sha256(identity)
    plan_id = (
        "exact-document-edit-plan-" + document_sha256[:32]
    )
    decision_sha256 = _decision_binding_sha256(
        plan_id=plan_id,
        project_id=binding.project_id,
        pipeline_job_id=binding.pipeline_job_id,
        analysis_job_id=binding.analysis_job_id,
        binding_manifest_sha256=binding.binding_manifest_sha256,
        exact_plan_sha256=exact_sha256,
        document_edit_plan_sha256=document_sha256,
    )
    return CurrentReportDocumentEditPlan(
        schema_version=CURRENT_REPORT_DOCUMENT_EDIT_PLAN_SCHEMA,
        state=CURRENT_REPORT_DOCUMENT_EDIT_PLAN_STATE,
        plan_id=plan_id,
        project_id=binding.project_id,
        pipeline_job_id=binding.pipeline_job_id,
        analysis_job_id=binding.analysis_job_id,
        source_sha256=binding.source_sha256,
        template_id=binding.template_id,
        template_quarter=3,
        document_snapshot_id=binding.document_snapshot_id,
        template_sha256=binding.template_sha256,
        structure_target_summary_sha256=(
            binding.structure_target_summary_sha256
        ),
        binding_manifest_sha256=binding.binding_manifest_sha256,
        exact_plan_sha256=exact_sha256,
        approved_incident_set_sha256=approved_sha256,
        approved_incidents=approved_tuple,
        operations=operations,
        stale_sample_factual_target_count=0,
        stale_sample_factual_target_coverage_complete=True,
        unused_sample_neutralization_operation_ids=(),
        document_edit_plan_sha256=document_sha256,
        accept_reject_rollback_binding_sha256=decision_sha256,
    )
