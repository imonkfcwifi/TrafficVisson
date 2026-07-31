import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  IPC_CHANNELS,
  IpcValidationError,
  toBridgePayload,
  validateIpcPayload,
} from "../contracts.mjs";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const PIPELINE_JOB_ID = "22222222-2222-4222-8222-222222222222";
const PREVIEW_ID = "report-preview-attempt-0";
const DRAFT_ID = `current-report-${"a".repeat(32)}`;
const APPLICATION_ID =
  `current-report-application-${"b".repeat(32)}`;
const DOCUMENT_EDIT_PLAN_SHA256 = "d".repeat(64);
const DECISION_BINDING_SHA256 = "e".repeat(64);
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(directory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

test("exact current-report IPC keeps the project and PipelineJob scope immutable", () => {
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.REPORT_EXACT_PREVIEW, {
      projectId: PROJECT_ID,
      pipelineJobId: PIPELINE_JOB_ID,
      previewId: PREVIEW_ID,
    }),
    {
      project_id: PROJECT_ID,
      pipeline_job_id: PIPELINE_JOB_ID,
      preview_id: PREVIEW_ID,
    },
  );
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.REPORT_EXACT_APPLY, {
      projectId: PROJECT_ID,
      pipelineJobId: PIPELINE_JOB_ID,
      draftId: DRAFT_ID,
      approvalToken: "c".repeat(64),
      documentEditPlanSha256: DOCUMENT_EDIT_PLAN_SHA256,
      acceptRejectRollbackBindingSha256: DECISION_BINDING_SHA256,
      userConfirmed: true,
    }),
    {
      project_id: PROJECT_ID,
      pipeline_job_id: PIPELINE_JOB_ID,
      draft_id: DRAFT_ID,
      approval_token: "c".repeat(64),
      document_edit_plan_sha256: DOCUMENT_EDIT_PLAN_SHA256,
      accept_reject_rollback_binding_sha256: DECISION_BINDING_SHA256,
      user_confirmed: true,
    },
  );
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.REPORT_EXACT_ACCEPT, {
      projectId: PROJECT_ID,
      pipelineJobId: PIPELINE_JOB_ID,
      applicationId: APPLICATION_ID,
      documentEditPlanSha256: DOCUMENT_EDIT_PLAN_SHA256,
      acceptRejectRollbackBindingSha256: DECISION_BINDING_SHA256,
      userConfirmed: true,
    }),
    {
      project_id: PROJECT_ID,
      pipeline_job_id: PIPELINE_JOB_ID,
      application_id: APPLICATION_ID,
      document_edit_plan_sha256: DOCUMENT_EDIT_PLAN_SHA256,
      accept_reject_rollback_binding_sha256: DECISION_BINDING_SHA256,
      user_confirmed: true,
    },
  );
});

test("exact report decisions reject renderer facts, implicit confirmation, and cross-scope IDs", () => {
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.REPORT_EXACT_PREVIEW, {
        projectId: PROJECT_ID,
        pipelineJobId: PIPELINE_JOB_ID,
        previewId: PREVIEW_ID,
        approvedIncidents: [{ severity: "urgent" }],
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.REPORT_EXACT_APPLY, {
        projectId: PROJECT_ID,
        pipelineJobId: "../other-job",
        draftId: DRAFT_ID,
        approvalToken: "c".repeat(64),
        documentEditPlanSha256: DOCUMENT_EDIT_PLAN_SHA256,
        acceptRejectRollbackBindingSha256: DECISION_BINDING_SHA256,
        userConfirmed: true,
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.REPORT_EXACT_APPLY, {
        projectId: PROJECT_ID,
        pipelineJobId: PIPELINE_JOB_ID,
        draftId: DRAFT_ID,
        approvalToken: "c".repeat(64),
        documentEditPlanSha256: DOCUMENT_EDIT_PLAN_SHA256,
        acceptRejectRollbackBindingSha256: DECISION_BINDING_SHA256,
        userConfirmed: false,
      }),
    /explicit/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.REPORT_EXACT_REJECT, {
        projectId: PROJECT_ID,
        pipelineJobId: PIPELINE_JOB_ID,
        applicationId: APPLICATION_ID,
        documentEditPlanSha256: DOCUMENT_EDIT_PLAN_SHA256,
        acceptRejectRollbackBindingSha256: DECISION_BINDING_SHA256,
        userConfirmed: false,
      }),
    /explicit/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.REPORT_EXACT_APPLY, {
        projectId: PROJECT_ID,
        pipelineJobId: PIPELINE_JOB_ID,
        draftId: DRAFT_ID,
        approvalToken: "c".repeat(64),
        userConfirmed: true,
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.REPORT_EXACT_APPLY, {
        projectId: PROJECT_ID,
        pipelineJobId: PIPELINE_JOB_ID,
        draftId: DRAFT_ID,
        approvalToken: "c".repeat(64),
        documentEditPlanSha256: "../stale-plan",
        acceptRejectRollbackBindingSha256: DECISION_BINDING_SHA256,
        userConfirmed: true,
      }),
    IpcValidationError,
  );
});

test("Electron exposes and routes only opaque exact-report identities", () => {
  for (const symbol of [
    "REPORT_EXACT_PREVIEW",
    "REPORT_EXACT_APPLY",
    "REPORT_EXACT_ACCEPT",
    "REPORT_EXACT_REJECT",
    "REPORT_EXACT_ROLLBACK",
  ]) {
    assert.match(mainSource, new RegExp(`IPC_CHANNELS\\.${symbol}`));
    assert.match(preloadSource, new RegExp(`CHANNELS\\.${symbol}`));
  }
  assert.match(mainSource, /pipeline-jobs\/\$\{encodeId\(/);
  assert.match(mainSource, /reports\/exact-preview/);
  assert.match(mainSource, /reports\/exact-apply/);
  assert.match(mainSource, /"exact-accept"/);
  assert.match(mainSource, /"exact-reject"/);
  assert.match(mainSource, /"exact-rollback"/);
  assert.doesNotMatch(
    preloadSource,
    /approvedIncidents|maintenanceAction|sourcePath/,
  );
});
