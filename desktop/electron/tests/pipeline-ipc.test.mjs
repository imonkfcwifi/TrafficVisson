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
      defaultTemplateId: "sample-4q-v1",
      defaultTemplateSha256: "a".repeat(64),
      detectorPackId: "road-detector-v1",
      ocrPackId: "korean-ocr-v1",
      sam2PackId: "sam2-v1",
      reportProvider: "codex",
      outputLocationId: "project-default",
      candidateRescanFps: 8,
    },
  );
  assert.deepEqual(validated, {
    project_id: PROJECT_ID,
    question_policy: "learning_assist",
    review_mode: "precision",
    default_template_id: "sample-4q-v1",
    default_template_sha256: "a".repeat(64),
    detector_pack_id: "road-detector-v1",
    ocr_pack_id: "korean-ocr-v1",
    sam2_pack_id: "sam2-v1",
    report_provider: "codex",
    output_location_id: "project-default",
    candidate_rescan_fps: 8,
  });
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE,
        {
          projectId: PROJECT_ID,
          questionPolicy: "learning_assist",
          reviewMode: "precision",
          defaultTemplateId: "C:\\private\\template.hwpx",
          defaultTemplateSha256: "a".repeat(64),
          detectorPackId: "road-detector-v1",
          ocrPackId: "korean-ocr-v1",
          sam2PackId: "sam2-v1",
          reportProvider: "codex",
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
    }),
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      review_completion_id: "review-revision-v1",
    },
  );
});

test("main and preload expose the persistent pipeline surface", () => {
  for (const channel of [
    "PIPELINE_PROFILE_CONFIGURE",
    "PIPELINE_PROFILE_GET",
    "PIPELINE_START",
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
  assert.match(surface, /completeReview/);
  assert.doesNotMatch(
    surface,
    /sourcePath|shell|\bCOM\b|trainingStart/,
  );
});
