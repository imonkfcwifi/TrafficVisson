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
const SNAPSHOT_ID = "22222222-2222-4222-8222-222222222222";
const JOB_ID = "33333333-3333-4333-8333-333333333333";
const SOURCE_RECORD_SHA256 = "b".repeat(64);
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(directory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

test("pipeline profile is immutable, opaque and local-model explicit", () => {
  const validated = toBridgePayload(
    IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE,
    {
      projectId: PROJECT_ID,
      questionPolicy: "learning_assist",
      reviewMode: "precision",
      labelConfirmationAuthority:
        "structured_llm_training_assist",
      defaultTemplateId: "sample-4q-v1",
      defaultTemplateSha256: "a".repeat(64),
      detectorPackId: "road-detector-v1",
      ocrPackId: "korean-ocr-v1",
      sam2PackId: "sam2-v1",
      reportProvider: "codex",
      reportTransmissionConsent: true,
      templateSemanticMapId: "sample-q4-semantic-v1",
      outputLocationId: "project-default",
      candidateRescanFps: 8,
    },
  );
  assert.deepEqual(validated, {
    project_id: PROJECT_ID,
    question_policy: "learning_assist",
    review_mode: "precision",
    label_confirmation_authority:
      "structured_llm_training_assist",
    default_template_id: "sample-4q-v1",
    default_template_sha256: "a".repeat(64),
    detector_pack_id: "road-detector-v1",
    ocr_pack_id: "korean-ocr-v1",
    sam2_pack_id: "sam2-v1",
    report_provider: "codex",
    report_transmission_consent: true,
    template_semantic_map_id: "sample-q4-semantic-v1",
    report_route_name: "unconfigured",
    report_inspector_name: "unconfigured",
    report_evidence_mode: "internal_original",
    report_inspection_date_policy:
      "source_filename_yyyymmdd_or_block",
    output_location_id: "project-default",
    candidate_rescan_fps: 8,
  });
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE, {
      projectId: PROJECT_ID,
      questionPolicy: "learning_assist",
      reviewMode: "precision",
      labelConfirmationAuthority: "human_review",
      defaultTemplateId: "sample-4q-v1",
      defaultTemplateSha256: "a".repeat(64),
      detectorPackId: "road-detector-v1",
      ocrPackId: "korean-ocr-v1",
      sam2PackId: "sam2-v1",
      reportProvider: "codex",
      reportTransmissionConsent: true,
      templateSemanticMapId: "sample-q4-semantic-v1",
      reportRouteName: "  서울북부고속도로 (구리-포천)  ",
      reportInspectorName: "  홍길동  ",
      reportEvidenceMode: "shared_deidentified",
      reportInspectionDatePolicy:
        "source_filename_yyyymmdd_or_block",
      outputLocationId: "project-default",
      candidateRescanFps: 8,
    }),
    {
      project_id: PROJECT_ID,
      question_policy: "learning_assist",
      review_mode: "precision",
      label_confirmation_authority: "human_review",
      default_template_id: "sample-4q-v1",
      default_template_sha256: "a".repeat(64),
      detector_pack_id: "road-detector-v1",
      ocr_pack_id: "korean-ocr-v1",
      sam2_pack_id: "sam2-v1",
      report_provider: "codex",
      report_transmission_consent: true,
      template_semantic_map_id: "sample-q4-semantic-v1",
      report_route_name: "서울북부고속도로 (구리-포천)",
      report_inspector_name: "홍길동",
      report_evidence_mode: "shared_deidentified",
      report_inspection_date_policy:
        "source_filename_yyyymmdd_or_block",
      output_location_id: "project-default",
      candidate_rescan_fps: 8,
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE,
        {
          projectId: PROJECT_ID,
          questionPolicy: "learning_assist",
          reviewMode: "precision",
          labelConfirmationAuthority: "human_review",
          defaultTemplateId: "C:\\private\\template.hwpx",
          defaultTemplateSha256: "a".repeat(64),
          detectorPackId: "road-detector-v1",
          ocrPackId: "korean-ocr-v1",
          sam2PackId: "sam2-v1",
          reportProvider: "codex",
          reportTransmissionConsent: true,
          templateSemanticMapId: "sample-q4-semantic-v1",
          outputLocationId: "project-default",
          candidateRescanFps: 8,
        },
      ),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE,
        {
          projectId: PROJECT_ID,
          questionPolicy: "learning_assist",
          reviewMode: "precision",
          labelConfirmationAuthority: "human_review",
          defaultTemplateId: "sample-4q-v1",
          defaultTemplateSha256: "a".repeat(64),
          detectorPackId: "road-detector-v1",
          ocrPackId: "korean-ocr-v1",
          sam2PackId: "sam2-v1",
          reportProvider: "codex",
          reportTransmissionConsent: "yes",
          templateSemanticMapId: "sample-q4-semantic-v1",
          outputLocationId: "project-default",
          candidateRescanFps: 8,
        },
      ),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE,
        {
          projectId: PROJECT_ID,
          questionPolicy: "learning_assist",
          reviewMode: "precision",
          labelConfirmationAuthority: "human_review",
          defaultTemplateId: "sample-4q-v1",
          defaultTemplateSha256: "a".repeat(64),
          detectorPackId: "road-detector-v1",
          ocrPackId: "korean-ocr-v1",
          sam2PackId: "sam2-v1",
          reportProvider: "codex",
          reportTransmissionConsent: true,
          templateSemanticMapId: "C:\\private\\semantic-map.json",
          outputLocationId: "project-default",
          candidateRescanFps: 8,
        },
      ),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE,
        {
          projectId: PROJECT_ID,
          questionPolicy: "learning_assist",
          reviewMode: "precision",
          labelConfirmationAuthority: "human_review",
          defaultTemplateId: "sample-4q-v1",
          defaultTemplateSha256: "a".repeat(64),
          detectorPackId: "road-detector-v1",
          ocrPackId: "korean-ocr-v1",
          sam2PackId: "sam2-v1",
          reportProvider: "codex",
          reportTransmissionConsent: true,
          templateSemanticMapId: "sample-q4-semantic-v1",
          reportRouteName: "서울\n북부",
          outputLocationId: "project-default",
          candidateRescanFps: 8,
        },
      ),
    IpcValidationError,
  );
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE,
        {
          projectId: PROJECT_ID,
          questionPolicy: "learning_assist",
          reviewMode: "precision",
          labelConfirmationAuthority: "provider_decides_truth",
          defaultTemplateId: "sample-4q-v1",
          defaultTemplateSha256: "a".repeat(64),
          detectorPackId: "road-detector-v1",
          ocrPackId: "korean-ocr-v1",
          sam2PackId: "sam2-v1",
          reportProvider: "codex",
          reportTransmissionConsent: true,
          templateSemanticMapId: "sample-q4-semantic-v1",
          outputLocationId: "project-default",
          candidateRescanFps: 8,
        },
      ),
    IpcValidationError,
  );
});

test("pipeline start contains only opaque media identity and explicit rights", () => {
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.PIPELINE_START, {
      projectId: PROJECT_ID,
      snapshotId: SNAPSHOT_ID,
      fileName: "drive.mp4",
      rightsConfirmed: true,
      idempotencyKey: "pipeline-start-key-0001",
    }),
    {
      project_id: PROJECT_ID,
      snapshot_id: SNAPSHOT_ID,
      file_name: "drive.mp4",
      rights_confirmed: true,
      idempotency_key: "pipeline-start-key-0001",
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.PIPELINE_START, {
        projectId: PROJECT_ID,
        snapshotId: SNAPSHOT_ID,
        fileName: "drive.mp4",
        rightsConfirmed: false,
        idempotencyKey: "pipeline-start-key-0001",
      }),
    /explicitly true/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.PIPELINE_START, {
        projectId: PROJECT_ID,
        snapshotId: SNAPSHOT_ID,
        fileName: "drive.mp4",
        rightsConfirmed: true,
        idempotencyKey: "pipeline-start-key-0001",
        sourcePath: "C:\\secret\\drive.mp4",
      }),
    /not allowed/,
  );
});

test("pipeline rerun is current-profile only and source-record pinned", () => {
  const request = {
    projectId: PROJECT_ID,
    sourceJobId: JOB_ID,
    expectedSourceRecordSha256: SOURCE_RECORD_SHA256,
    expectedProfileRevision: 4,
    rightsConfirmed: true,
    userConfirmed: true,
    idempotencyKey: "pipeline-rerun-key-0001",
  };
  assert.deepEqual(
    toBridgePayload(
      IPC_CHANNELS.PIPELINE_RERUN_CURRENT_PROFILE,
      request,
    ),
    {
      project_id: PROJECT_ID,
      source_job_id: JOB_ID,
      expected_source_record_sha256: SOURCE_RECORD_SHA256,
      expected_profile_revision: 4,
      rights_confirmed: true,
      user_confirmed: true,
      idempotency_key: "pipeline-rerun-key-0001",
    },
  );

  for (const forbidden of [
    ["snapshotId", SNAPSHOT_ID],
    ["fileName", "drive.mp4"],
    ["sourcePath", "C:\\secret\\drive.mp4"],
    ["modelPackId", "detector-v2"],
  ]) {
    assert.throws(
      () =>
        validateIpcPayload(
          IPC_CHANNELS.PIPELINE_RERUN_CURRENT_PROFILE,
          { ...request, [forbidden[0]]: forbidden[1] },
        ),
      /not allowed/,
    );
  }

  for (const invalid of [
    { rightsConfirmed: false },
    { userConfirmed: false },
    { expectedSourceRecordSha256: "B".repeat(64) },
    { expectedSourceRecordSha256: ` ${SOURCE_RECORD_SHA256}` },
    { expectedProfileRevision: 0 },
    { expectedProfileRevision: 1.5 },
    { idempotencyKey: "short" },
    { idempotencyKey: "x".repeat(129) },
    { idempotencyKey: " pipeline-rerun-key-0001" },
    { idempotencyKey: `x${"/".repeat(15)}` },
  ]) {
    assert.throws(() =>
      validateIpcPayload(
        IPC_CHANNELS.PIPELINE_RERUN_CURRENT_PROFILE,
        { ...request, ...invalid },
      ),
    );
  }

  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.PIPELINE_RERUN_CURRENT_PROFILE,
        {
          ...request,
          idempotencyKey: "explicit-new:source-run-0001",
        },
      ),
    /reserved prefix/,
  );
});

test("pipeline lifecycle IPC is project scoped and review completion is explicit", () => {
  for (const channel of [
    IPC_CHANNELS.PIPELINE_GET,
    IPC_CHANNELS.PIPELINE_CANCEL,
    IPC_CHANNELS.PIPELINE_RESUME,
  ]) {
    assert.deepEqual(
      validateIpcPayload(channel, {
        projectId: PROJECT_ID,
        jobId: JOB_ID,
      }),
      { projectId: PROJECT_ID, jobId: JOB_ID },
    );
  }
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.PIPELINE_REVIEW_COMPLETE, {
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      reviewCompletionId: "review-revision-v1",
      finalQaChoice: "no_miss_complete",
    }),
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      review_completion_id: "review-revision-v1",
      final_qa_choice: "no_miss_complete",
    },
  );
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.PIPELINE_REVIEW_COMPLETE, {
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      reviewCompletionId:
        "zero-detections:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      finalQaChoice: "no_miss_complete",
    }),
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      review_completion_id:
        "zero-detections:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      final_qa_choice: "no_miss_complete",
    },
  );
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.PIPELINE_REVIEW_COMPLETE, {
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      reviewCompletionId: "review-bootstrap-v1",
    }),
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      review_completion_id: "review-bootstrap-v1",
    },
  );
});

test("main and preload expose the persistent pipeline surface", () => {
  for (const channel of [
    "PIPELINE_PROFILE_CONFIGURE",
    "PIPELINE_PROFILE_GET",
    "PIPELINE_START",
    "PIPELINE_RERUN_CURRENT_PROFILE",
    "PIPELINE_LIST",
    "PIPELINE_GET",
    "PIPELINE_CANCEL",
    "PIPELINE_RESUME",
    "PIPELINE_REVIEW_COMPLETE",
  ]) {
    assert.match(mainSource, new RegExp(channel));
  }
  const start = preloadSource.indexOf("pipeline: Object.freeze");
  const end = preloadSource.indexOf(
    "candidateQuestions: Object.freeze",
    start,
  );
  assert.ok(start >= 0 && end > start);
  const surface = preloadSource.slice(start, end);
  assert.match(surface, /configureProfile/);
  assert.match(surface, /rerunWithCurrentProfile/);
  assert.match(surface, /completeReview/);
  assert.doesNotMatch(
    surface,
    /sourcePath|shell|\bCOM\b|trainingStart/,
  );
});

test("main posts reruns to the source-job-scoped bridge route", () => {
  const start = mainSource.indexOf(
    "IPC_CHANNELS.PIPELINE_RERUN_CURRENT_PROFILE",
  );
  const end = mainSource.indexOf(
    "registerHandler(IPC_CHANNELS.PIPELINE_LIST",
    start,
  );
  assert.ok(start >= 0 && end > start);
  const handler = mainSource.slice(start, end);
  assert.match(handler, /supervisor\.request\(\s*"POST"/);
  assert.match(
    handler,
    /pipeline\/jobs\/\$\{encodeId\(payload\.sourceJobId\)\}\/reruns/,
  );
  assert.match(
    handler,
    /toBridgePayload\(\s*IPC_CHANNELS\.PIPELINE_RERUN_CURRENT_PROFILE/,
  );
  assert.doesNotMatch(
    handler,
    /snapshotId|fileName|sourcePath|modelPack|explicit-new:/,
  );
});

test("output folder path remains main-only while renderer receives opaque registration", () => {
  assert.match(mainSource, /ProjectOutputDirectorySelectionRegistry/);
  assert.match(mainSource, /selected_directory: selectedDirectory/);
  assert.match(
    preloadSource,
    /projectOutputLocations: Object\.freeze/,
  );
  assert.match(preloadSource, /selectProjectOutputDirectory/);
  assert.doesNotMatch(
    preloadSource,
    /selectedDirectory|selectedPath|resolvePath|consumePath/,
  );
});
