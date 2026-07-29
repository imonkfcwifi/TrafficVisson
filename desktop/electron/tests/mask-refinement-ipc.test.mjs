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
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const QUESTION_ID = "44444444-4444-4444-8444-444444444441";
const CANDIDATE_ID = "0123456789abcdefabcd";
const REFINEMENT_ID = "smr_0123456789abcdef01234567";
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(directory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

const identity = {
  projectId: PROJECT_ID,
  jobId: JOB_ID,
  questionId: QUESTION_ID,
  candidateId: CANDIDATE_ID,
};

test("mask refinement IPC accepts only opaque identities and a normalized click", () => {
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.MASK_REFINEMENT_CAPABILITY, identity),
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      question_id: QUESTION_ID,
      candidate_id: CANDIDATE_ID,
    },
  );
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.MASK_REFINEMENT_REQUEST, {
      ...identity,
      click: { x: 0.375, y: 0.625 },
    }),
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      question_id: QUESTION_ID,
      candidate_id: CANDIDATE_ID,
      click: { x: 0.375, y: 0.625 },
    },
  );
  for (const click of [
    { x: -0.001, y: 0.5 },
    { x: 1.001, y: 0.5 },
    { x: Number.NaN, y: 0.5 },
  ]) {
    assert.throws(
      () =>
        validateIpcPayload(IPC_CHANNELS.MASK_REFINEMENT_REQUEST, {
          ...identity,
          click,
        }),
      IpcValidationError,
    );
  }
});

test("mask refinement IPC rejects renderer paths, bytes, polygons, and commands", () => {
  for (const leakedField of [
    { imagePath: "C:\\private\\candidate.jpg" },
    { imageBytes: "data:image/jpeg;base64,AAAA" },
    { polygon: [[0.1, 0.1]] },
    { command: "python sam2.py" },
  ]) {
    assert.throws(
      () =>
        validateIpcPayload(IPC_CHANNELS.MASK_REFINEMENT_REQUEST, {
          ...identity,
          click: { x: 0.5, y: 0.5 },
          ...leakedField,
        }),
      /not allowed/,
    );
  }
});

test("edited candidate answer references server-owned SAM2 refinement", () => {
  const payload = {
    ...identity,
    answer: "edit",
    maskRefinementId: REFINEMENT_ID,
    selectedIssueType: "pothole",
    severity: "moderate",
    maintenanceAction: "노면 보수",
  };
  delete payload.click;
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.CANDIDATE_ANSWER, payload),
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      question_id: QUESTION_ID,
      candidate_id: CANDIDATE_ID,
      answer: "edit",
      mask_refinement_id: REFINEMENT_ID,
      selected_issue_type: "pothole",
      severity: "moderate",
      maintenance_action: "노면 보수",
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.CANDIDATE_ANSWER, {
        ...payload,
        normalizedBox: { x: 0.1, y: 0.1, width: 0.2, height: 0.2 },
      }),
    /exactly one trusted geometry source/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.CANDIDATE_ANSWER, {
        ...payload,
        questionId: undefined,
      }),
    /questionId is required/,
  );
});

test("main and preload expose only the bounded SAM2 refinement surface", () => {
  for (const channel of [
    "MASK_REFINEMENT_CAPABILITY",
    "MASK_REFINEMENT_REQUEST",
  ]) {
    assert.match(mainSource, new RegExp(channel));
    assert.match(preloadSource, new RegExp(channel));
  }
  const start = preloadSource.indexOf("maskRefinements: Object.freeze");
  const end = preloadSource.indexOf("incidents: Object.freeze", start);
  assert.ok(start >= 0 && end > start);
  const surface = preloadSource.slice(start, end);
  assert.match(surface, /capability/);
  assert.match(surface, /refine/);
  assert.doesNotMatch(
    surface,
    /path|bytes|polygon|shell|command|annotation|incident/,
  );
});

