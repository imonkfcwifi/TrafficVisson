import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const PIPELINE_JOB_ID = "22222222-2222-4222-8222-222222222222";
const ANALYSIS_JOB_ID = "33333333-3333-4333-8333-333333333333";
const PREVIEW_ID = "report-preview-attempt-0";
const DRAFT_ID = `current-report-${"a".repeat(32)}`;
const APPLICATION_ID =
  `current-report-application-${"b".repeat(32)}`;
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const exact = await vite.ssrLoadModule("/src/exactReportWorkflow.ts");
const reportScreenSource = await readFile(
  new URL("./screens/ReportScreen.tsx", import.meta.url),
  "utf8",
);

after(async () => {
  await vite.close();
});

function completedJob(report = {}) {
  return {
    jobId: PIPELINE_JOB_ID,
    projectId: PROJECT_ID,
    status: "completed",
    stages: [
      {
        name: "derived_outputs",
        status: "success",
        outcome: "success",
        checkpoint: {
          report: {
            state: "validated_preview",
            preview_id: PREVIEW_ID,
            plan_sha256: "c".repeat(64),
            draft_sha256: "d".repeat(64),
            operation_count: 1257,
            blocking_reasons: [],
            provider_called: false,
            document_edit_plan_prepared: true,
            com_invoked: false,
            ...report,
          },
        },
      },
    ],
  };
}

function exactPreview() {
  const incidentId = "incident-current-video-one";
  const documentEditPlanSha256 = "b".repeat(64);
  const decisionBindingSha256 = "c".repeat(64);
  return {
    schema_version: "trafficvisson.current-report-exact-preview.v1",
    mode: "exact_current_report_preview",
    state: "exact_current_report_draft_ready_apply_approval_required",
    draft_id: DRAFT_ID,
    project_id: PROJECT_ID,
    pipeline_job_id: PIPELINE_JOB_ID,
    analysis_job_id: ANALYSIS_JOB_ID,
    approved_incident_count: 1,
    binding_manifest_sha256: "1".repeat(64),
    exact_plan_sha256: "2".repeat(64),
    document_edit_plan: {
      schema_version:
        "trafficvisson.current-report-document-edit-plan.v2",
      state: "exact_targets_bound_user_review_required",
      plan_id: `exact-document-edit-plan-${"b".repeat(32)}`,
      project_id: PROJECT_ID,
      pipeline_job_id: PIPELINE_JOB_ID,
      analysis_job_id: ANALYSIS_JOB_ID,
      source_sha256: "0".repeat(64),
      template_id: "sample-3q-v1",
      template_quarter: 3,
      document_snapshot_id: "snapshot-current-report",
      template_sha256: "d".repeat(64),
      structure_target_summary_sha256: "e".repeat(64),
      binding_manifest_sha256: "1".repeat(64),
      exact_plan_sha256: "2".repeat(64),
      approved_incident_set_sha256: "f".repeat(64),
      approved_incidents: [
        {
          incident_id: incidentId,
          incident_order: 0,
          project_id: PROJECT_ID,
          pipeline_job_id: PIPELINE_JOB_ID,
          analysis_job_id: ANALYSIS_JOB_ID,
          source_sha256: "0".repeat(64),
          approval_actor: "local_user",
          approval_record_id: "review-current-video-one",
          approval_record_sha256: "7".repeat(64),
          selected_evidence_ref: "original:incident-current-video-one",
          selected_evidence_sha256: "8".repeat(64),
          evidence_policy: "internal_original",
          photo_slot_id: "photo-slot-1",
          photo_binding_sha256: "9".repeat(64),
          provenance_sha256: "a".repeat(64),
        },
      ],
      operations: [
        {
          operation_id: "replace-opinion-1",
          exact_operation_kind: "replace_cell_text",
          semantic_roles: ["inspection_opinion"],
          binding_groups: ["replacement"],
          target: {
            target_id: "cell:1",
            target_kind: "cell",
            page_pair_index: 0,
            page_number: 1,
            section_path: "Contents/section0.xml",
            section_index: 0,
            table_id: "table:1",
            table_index: 0,
            cell_target_id: "cell:1",
            cell_index: 1,
            row: 0,
            column: 1,
            row_span: 1,
            column_span: 1,
            picture_index: null,
            paragraph_address_sha256: null,
            photo_slot_ids: ["photo-slot-1"],
          },
          expected_sample_value_sha256: "3".repeat(64),
          proposed_value_sha256: "4".repeat(64),
          proposed_text: "포트홀 보수 필요",
          proposed_evidence_sha256: null,
          current_source_refs: [`incident:${incidentId}`],
          approved_incident_ids: [incidentId],
          checkbox_targets: [],
          opinion_target: true,
          neutralizes_historical_sample_fact: true,
          unused_sample_factual_slot_neutralized: false,
        },
      ],
      stale_sample_factual_target_count: 1,
      stale_sample_factual_target_coverage_complete: true,
      unused_sample_neutralization_operation_ids: [],
      document_edit_plan_sha256: documentEditPlanSha256,
      accept_reject_rollback_binding_sha256: decisionBindingSha256,
      expected_rendered_page_count: 48,
      address_authority: "server_audited_sample_layout",
      factual_authority: "current_local_user_approved_incidents",
      provider_may_select_target_addresses: false,
      apply_approval_required: true,
      accept_reject_required_after_apply: true,
      rollback_supported_after_apply: true,
      provider_invocation_count: 0,
      exact_writer_invocation_count: 0,
      com_invocation_count: 0,
      document_written: false,
      path_exposed: false,
    },
    document_edit_plan_sha256: documentEditPlanSha256,
    accept_reject_rollback_binding_sha256: decisionBindingSha256,
    output_document_sha256: "3".repeat(64),
    render_binding_sha256: "4".repeat(64),
    record_sha256: "5".repeat(64),
    operation_count: 1,
    expected_rendered_page_count: 48,
    approval_token: "6".repeat(64),
    approval_expires_at: "2026-07-30T01:00:00+00:00",
    apply_approval_required: true,
    apply_endpoint_available: true,
    remaining_blocker_codes: [],
    exact_writer_completed: true,
    output_render_validation_status: "not_run_com_validation_required",
    provider_invocation_count: 0,
    com_invocation_count: 0,
    document_published: false,
    path_exposed: false,
    idempotent_replay: false,
  };
}

function exactApplication() {
  return {
    schema_version: "trafficvisson.current-report-application.v1",
    application_id: APPLICATION_ID,
    state: "render_validated_accept_or_reject_required",
    project_id: PROJECT_ID,
    pipeline_job_id: PIPELINE_JOB_ID,
    analysis_job_id: ANALYSIS_JOB_ID,
    draft_id: DRAFT_ID,
    binding_manifest_sha256: "1".repeat(64),
    exact_plan_sha256: "2".repeat(64),
    document_edit_plan_sha256: "b".repeat(64),
    accept_reject_rollback_binding_sha256: "c".repeat(64),
    output_file_name: "current-report.hwpx",
    output_sha256: "7".repeat(64),
    pdf_file_name: "current-report.pdf",
    pdf_sha256: "8".repeat(64),
    render_validation_status: "validated_48_page_render",
    rendered_page_count: 48,
    render_failure_codes: [],
    output_reopen_structure_sha256: "9".repeat(64),
    raster_artifact_manifest_sha256: "a".repeat(64),
    com_publish_completed: true,
    pdf_export_completed: true,
    render_validation_completed: true,
    rollback_completed: false,
    accept_reject_required: true,
    accepted: false,
    rejected: false,
    path_exposed: false,
  };
}

test("completed PipelineJob exposes only its exact prepared report identity", () => {
  assert.deepEqual(
    exact.exactReportContextFromPipeline(completedJob()),
    {
      projectId: PROJECT_ID,
      pipelineJobId: PIPELINE_JOB_ID,
      previewId: PREVIEW_ID,
      operationCount: 1257,
    },
  );
  assert.equal(
    exact.exactReportContextFromPipeline(
      completedJob({ com_invoked: true }),
    ),
    null,
  );
  assert.equal(
    exact.exactReportContextFromPipeline({
      ...completedJob(),
      status: "awaiting_review",
    }),
    null,
  );
});

test("exact preview parser requires full fail-closed provenance and no COM write", () => {
  const parsed = exact.parseExactReportPreview(exactPreview());
  assert.equal(parsed.approvedIncidentCount, 1);
  assert.equal(parsed.operationCount, 1);
  assert.equal(parsed.expectedRenderedPageCount, 48);
  assert.equal(parsed.documentEditPlan.operations.length, 1);
  assert.equal(
    parsed.documentEditPlan.operations[0].target.cellTargetId,
    "cell:1",
  );
  assert.equal(
    parsed.documentEditPlanSha256,
    parsed.documentEditPlan.documentEditPlanSha256,
  );

  const extraFact = exactPreview();
  extraFact.incidents = [{ incident_type: "POTHOLE" }];
  assert.throws(
    () => exact.parseExactReportPreview(extraFact),
    /응답 형식/,
  );

  const prepublished = exactPreview();
  prepublished.document_published = true;
  assert.throws(
    () => exact.parseExactReportPreview(prepublished),
    /응답 형식/,
  );

  const nestedExtra = exactPreview();
  nestedExtra.document_edit_plan.operations[0].renderer_fact = "urgent";
  assert.throws(
    () => exact.parseExactReportPreview(nestedExtra),
    /응답 형식/,
  );

  const missingContainingCell = exactPreview();
  missingContainingCell.document_edit_plan.operations[0].target
    .cell_target_id = null;
  assert.throws(
    () => exact.parseExactReportPreview(missingContainingCell),
    /포함 셀 주소/,
  );

  const legacyCellAddressContract = exactPreview();
  legacyCellAddressContract.document_edit_plan.schema_version =
    "trafficvisson.current-report-document-edit-plan.v1";
  assert.throws(
    () => exact.parseExactReportPreview(legacyCellAddressContract),
    /응답 형식/,
  );

  const pathLeak = exactPreview();
  pathLeak.document_edit_plan.approved_incidents[0].selected_evidence_ref =
    String.raw`C:\private\frame.jpg`;
  assert.throws(
    () => exact.parseExactReportPreview(pathLeak),
    /DocumentEditPlan/,
  );

  const staleDecisionBinding = exactPreview();
  staleDecisionBinding.accept_reject_rollback_binding_sha256 =
    "0".repeat(64);
  assert.throws(
    () => exact.parseExactReportPreview(staleDecisionBinding),
    /provenance/,
  );
});

test("exact application parser accepts only internally consistent 48-page evidence", () => {
  const parsed = exact.parseExactReportApplication(exactApplication());
  assert.equal(
    parsed.state,
    "render_validated_accept_or_reject_required",
  );
  assert.equal(parsed.renderedPageCount, 48);
  assert.equal(parsed.acceptRejectRequired, true);

  const truncated = exactApplication();
  truncated.rendered_page_count = 47;
  assert.throws(
    () => exact.parseExactReportApplication(truncated),
    /48쪽 검증 증거/,
  );

  const pathLeak = exactApplication();
  pathLeak.output_file_name = String.raw`C:\reports\current-report.hwpx`;
  assert.throws(
    () => exact.parseExactReportApplication(pathLeak),
    /파일명/,
  );
});

test("report screen auto-prepares exact preview and leaves only explicit COM decisions", () => {
  assert.match(
    reportScreenSource,
    /trafficApi\.reports\.prepareExactPreview\(\{[\s\S]*projectId:\s*exactReportContext\.projectId[\s\S]*previewId:\s*exactReportContext\.previewId/,
  );
  assert.match(
    reportScreenSource,
    /reports\.applyExact\(\{[\s\S]*approvalToken,[\s\S]*documentEditPlanSha256:[\s\S]*acceptRejectRollbackBindingSha256:[\s\S]*userConfirmed:\s*true/,
  );
  assert.match(
    reportScreenSource,
    /reports\.acceptExact\(request\)[\s\S]*reports\.rejectExact\(request\)/,
  );
  assert.match(
    reportScreenSource,
    /reports\.rollbackExact\(\{[\s\S]*userConfirmed:\s*true/,
  );
  assert.match(
    reportScreenSource,
    /\{!exactReportContext \? \([\s\S]*className="report-controls"/,
  );
  assert.match(reportScreenSource, /한\/글 COM 적용 승인/);
  assert.match(reportScreenSource, /48쪽 검증 결과 수락/);
});
