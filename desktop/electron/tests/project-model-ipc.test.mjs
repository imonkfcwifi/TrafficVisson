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
const DATASET_ID = "22222222-2222-4222-8222-222222222222";
const TRAINING_JOB_ID = "33333333-3333-4333-8333-333333333333";
const MODEL_ID = "44444444-4444-4444-8444-444444444444";
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(directory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

test("project training start sends only user choices and explicit confirmation", () => {
  const request = {
    projectId: PROJECT_ID,
    datasetVersionId: DATASET_ID,
    idempotencyKey: "project-model:train:01234567",
    userConfirmed: true,
  };
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.MODEL_TRAINING_START, request),
    {
      project_id: PROJECT_ID,
      dataset_version_id: DATASET_ID,
      idempotency_key: "project-model:train:01234567",
      user_confirmed: true,
    },
  );
  for (const forbidden of [
    "annotations",
    "approvedReferences",
    "requiredIssueTypes",
    "baseModelVersion",
    "artifactPath",
    "trainerExecutable",
  ]) {
    assert.throws(
      () =>
        validateIpcPayload(IPC_CHANNELS.MODEL_TRAINING_START, {
          ...request,
          [forbidden]: "C:\\secret\\model.bin",
        }),
      IpcValidationError,
    );
  }
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.MODEL_TRAINING_START, {
        ...request,
        userConfirmed: false,
      }),
    /must be true/,
  );

  const extensionApproval = {
    schemaVersion:
      "trafficvisson.fixed-validation-extension-approval.v1",
    expectedParentVersion: 1,
    expectedParentMembershipSha256: "a".repeat(64),
    expectedParentFixedValidationSha256: "b".repeat(64),
    addedIssueTypes: ["sign_damage"],
    addedValidationAnalysisJobIds: [
      "55555555-5555-4555-8555-555555555555",
    ],
    userConfirmed: true,
  };
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.MODEL_TRAINING_START, {
      ...request,
      fixedValidationExtensionApproval: extensionApproval,
    }).fixed_validation_extension_approval,
    {
      schema_version:
        "trafficvisson.fixed-validation-extension-approval.v1",
      expected_parent_version: 1,
      expected_parent_membership_sha256: "a".repeat(64),
      expected_parent_fixed_validation_sha256: "b".repeat(64),
      added_issue_types: ["sign_damage"],
      added_validation_analysis_job_ids: [
        "55555555-5555-4555-8555-555555555555",
      ],
      user_confirmed: true,
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.MODEL_TRAINING_START, {
        ...request,
        fixedValidationExtensionApproval: {
          ...extensionApproval,
          expectedParentMembershipSha256: "0".repeat(63),
        },
      }),
    IpcValidationError,
  );
});

test("project model state transitions are revision-bound and user-confirmed", () => {
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.MODEL_ITERATIONS_LIST, {
      projectId: PROJECT_ID,
    }),
    { projectId: PROJECT_ID },
  );
  for (const channel of [
    IPC_CHANNELS.MODEL_TRAINING_RESUME,
    IPC_CHANNELS.MODEL_TRAINING_CANCEL,
  ]) {
    assert.deepEqual(
      validateIpcPayload(channel, {
        projectId: PROJECT_ID,
        trainingJobId: TRAINING_JOB_ID,
        userConfirmed: true,
      }),
      {
        projectId: PROJECT_ID,
        trainingJobId: TRAINING_JOB_ID,
        userConfirmed: true,
      },
    );
    assert.throws(
      () =>
        validateIpcPayload(channel, {
          projectId: PROJECT_ID,
          trainingJobId: TRAINING_JOB_ID,
          userConfirmed: false,
        }),
      /must be true/,
    );
  }
  for (const channel of [
    IPC_CHANNELS.MODEL_PROMOTE,
    IPC_CHANNELS.MODEL_REJECT,
  ]) {
    assert.deepEqual(
      validateIpcPayload(channel, {
        projectId: PROJECT_ID,
        modelVersionId: MODEL_ID,
        expectedRevision: 3,
        idempotencyKey: "project-model:transition:01234567",
        userConfirmed: true,
      }),
      {
        projectId: PROJECT_ID,
        modelVersionId: MODEL_ID,
        expectedRevision: 3,
        idempotencyKey: "project-model:transition:01234567",
        userConfirmed: true,
      },
    );
  }
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.MODEL_ROLLBACK, {
      projectId: PROJECT_ID,
      expectedRevision: 4,
      idempotencyKey: "project-model:rollback:01234567",
      userConfirmed: true,
    }),
    {
      projectId: PROJECT_ID,
      expectedRevision: 4,
      idempotencyKey: "project-model:rollback:01234567",
      userConfirmed: true,
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.MODEL_ROLLBACK, {
        projectId: PROJECT_ID,
        expectedRevision: 4,
        idempotencyKey: "project-model:rollback:01234567",
        userConfirmed: true,
        modelVersionId: MODEL_ID,
      }),
    /not allowed/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.MODEL_PROMOTE, {
        projectId: PROJECT_ID,
        modelVersionId: MODEL_ID,
        expectedRevision: -1,
        idempotencyKey: "project-model:promote:01234567",
        userConfirmed: true,
      }),
    IpcValidationError,
  );
});

test("main and preload expose the bounded project model lifecycle", () => {
  assert.match(
    mainSource,
    /MODEL_TRAINING_START[\s\S]*\/model-training-jobs/,
  );
  assert.match(
    mainSource,
    /MODEL_TRAINING_RESUME[\s\S]*\/model-training-jobs\/\$\{encodeId\(payload\.trainingJobId\)\}\/resume/,
  );
  assert.match(
    mainSource,
    /MODEL_TRAINING_CANCEL[\s\S]*\/model-training-jobs\/\$\{encodeId\(payload\.trainingJobId\)\}\/cancel/,
  );
  assert.match(mainSource, /MODEL_VERSIONS_LIST[\s\S]*\/model-versions/);
  assert.match(
    mainSource,
    /MODEL_ITERATIONS_LIST[\s\S]*\/model-iterations/,
  );
  assert.match(
    mainSource,
    /MODEL_FIXED_VALIDATION_LIST[\s\S]*\/fixed-validation-sets/,
  );
  assert.match(mainSource, /MODEL_STATE_GET[\s\S]*\/model-state/);
  assert.match(mainSource, /MODEL_PROMOTE/);
  assert.match(mainSource, /MODEL_REJECT/);
  assert.match(mainSource, /MODEL_ROLLBACK[\s\S]*\/model-state\/rollback/);
  assert.match(
    mainSource,
    /PROJECT_MODEL_TRAINING_TIMEOUT_MS = 30 \* 60 \* 1000/,
  );

  const start = preloadSource.indexOf("projectModels: Object.freeze");
  const end = preloadSource.indexOf("sources: Object.freeze", start);
  assert.ok(start >= 0 && end > start);
  const surface = preloadSource.slice(start, end);
  for (const operation of [
    "startTraining",
    "listTrainingJobs",
    "resumeTraining",
    "cancelTraining",
    "listFixedValidationSets",
    "listVersions",
    "listIterations",
    "getState",
    "promote",
    "reject",
    "rollback",
  ]) {
    assert.match(surface, new RegExp(`${operation}:`));
  }
  assert.doesNotMatch(
    surface,
    /path|shell|artifact|annotation|reference|baseModel/i,
  );
});
