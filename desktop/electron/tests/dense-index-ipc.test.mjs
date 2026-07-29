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
const ANALYSIS_JOB_ID = "22222222-2222-4222-8222-222222222222";
const DENSE_JOB_ID = "33333333-3333-4333-8333-333333333333";
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(directory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

test("dense-index start accepts only opaque analysis identity and explicit review confirmation", () => {
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.DENSE_INDEX_START, {
      projectId: PROJECT_ID,
      analysisJobId: ANALYSIS_JOB_ID,
      samplingFps: 2,
      confirmReviewOnly: true,
    }),
    {
      project_id: PROJECT_ID,
      analysis_job_id: ANALYSIS_JOB_ID,
      sampling_fps: 2,
      confirm_review_only: true,
    },
  );
  for (const samplingFps of [0, 6, 1.5, true, "2"]) {
    assert.throws(
      () =>
        validateIpcPayload(IPC_CHANNELS.DENSE_INDEX_START, {
          projectId: PROJECT_ID,
          analysisJobId: ANALYSIS_JOB_ID,
          samplingFps,
          confirmReviewOnly: true,
        }),
      IpcValidationError,
    );
  }
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.DENSE_INDEX_START, {
        projectId: PROJECT_ID,
        analysisJobId: ANALYSIS_JOB_ID,
        samplingFps: 2,
        confirmReviewOnly: false,
      }),
    /explicitly true/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.DENSE_INDEX_START, {
        projectId: PROJECT_ID,
        analysisJobId: ANALYSIS_JOB_ID,
        samplingFps: 2,
        confirmReviewOnly: true,
        sourcePath: "C:\\secret\\road.mp4",
      }),
    /not allowed/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.DENSE_INDEX_START, {
        projectId: PROJECT_ID,
        analysisJobId: ANALYSIS_JOB_ID,
        samplingFps: 2,
        confirmReviewOnly: true,
        sourceSha256: "a".repeat(64),
      }),
    /not allowed/,
  );
});

test("dense-index list/get/cancel retain project UUID isolation", () => {
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.DENSE_INDEX_LIST, {
      projectId: PROJECT_ID,
    }),
    { projectId: PROJECT_ID },
  );
  for (const channel of [
    IPC_CHANNELS.DENSE_INDEX_GET,
    IPC_CHANNELS.DENSE_INDEX_CANCEL,
  ]) {
    assert.deepEqual(
      validateIpcPayload(channel, {
        projectId: PROJECT_ID,
        jobId: DENSE_JOB_ID,
      }),
      { projectId: PROJECT_ID, jobId: DENSE_JOB_ID },
    );
    assert.throws(
      () =>
        validateIpcPayload(channel, {
          projectId: PROJECT_ID,
          jobId: "../../other-project",
        }),
      /UUID/,
    );
  }
});

test("main and preload expose only bounded dense-index job operations", () => {
  assert.match(
    mainSource,
    /DENSE_INDEX_START[\s\S]*\/dense-index\/jobs/,
  );
  assert.match(mainSource, /DENSE_INDEX_LIST/);
  assert.match(mainSource, /DENSE_INDEX_GET/);
  assert.match(mainSource, /DENSE_INDEX_CANCEL/);

  const start = preloadSource.indexOf("denseIndex: Object.freeze");
  const end = preloadSource.indexOf(
    "candidateQuestions: Object.freeze",
    start,
  );
  assert.ok(start >= 0 && end > start);
  const surface = preloadSource.slice(start, end);
  assert.match(surface, /start:/);
  assert.match(surface, /list:/);
  assert.match(surface, /get:/);
  assert.match(surface, /cancel:/);
  assert.doesNotMatch(surface, /path|shell|incident|annotation|training/i);
});
