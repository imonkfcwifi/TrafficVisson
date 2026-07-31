import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  IPC_CHANNELS,
  IpcValidationError,
  toBridgePayload,
} from "../contracts.mjs";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const SNAPSHOT_ID = "22222222-2222-4222-8222-222222222222";
const BATCH_ID = "33333333-3333-4333-8333-333333333333";
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(directory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

test("folder batch IPC requires one explicit rights approval and opaque ids", () => {
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.PIPELINE_FOLDER_BATCH_START, {
      projectId: PROJECT_ID,
      snapshotId: SNAPSHOT_ID,
      rightsConfirmed: true,
      idempotencyKey: `folder-start-${SNAPSHOT_ID}`,
    }),
    {
      project_id: PROJECT_ID,
      snapshot_id: SNAPSHOT_ID,
      rights_confirmed: true,
      idempotency_key: `folder-start-${SNAPSHOT_ID}`,
    },
  );
  assert.throws(
    () =>
      toBridgePayload(IPC_CHANNELS.PIPELINE_FOLDER_BATCH_START, {
        projectId: PROJECT_ID,
        snapshotId: SNAPSHOT_ID,
        rightsConfirmed: false,
        idempotencyKey: `folder-start-${SNAPSHOT_ID}`,
      }),
    IpcValidationError,
  );
  assert.throws(
    () =>
      toBridgePayload(IPC_CHANNELS.PIPELINE_FOLDER_BATCH_START, {
        projectId: PROJECT_ID,
        snapshotId: SNAPSHOT_ID,
        rightsConfirmed: true,
        idempotencyKey: `folder-start-${SNAPSHOT_ID}`,
        directory: "\\\\server\\private",
      }),
    /not allowed/,
  );
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.PIPELINE_FOLDER_BATCH_RETRY, {
      projectId: PROJECT_ID,
      batchId: BATCH_ID,
      userConfirmed: true,
    }),
    {
      project_id: PROJECT_ID,
      batch_id: BATCH_ID,
      user_confirmed: true,
    },
  );
});

test("main and preload expose start/list/get/retry without filesystem paths", () => {
  for (const channel of [
    "PIPELINE_FOLDER_BATCH_START",
    "PIPELINE_FOLDER_BATCH_LIST",
    "PIPELINE_FOLDER_BATCH_GET",
    "PIPELINE_FOLDER_BATCH_RETRY",
  ]) {
    assert.match(mainSource, new RegExp(`IPC_CHANNELS\\.${channel}`));
    assert.match(preloadSource, new RegExp(`CHANNELS\\.${channel}`));
  }
  assert.match(mainSource, /pipeline\/folder-batches\?limit=128/);
  assert.match(mainSource, /pipeline\/folder-batches\/\$\{encodeId\(payload\.batchId\)\}\/retry/);
  assert.doesNotMatch(
    preloadSource.slice(
      preloadSource.indexOf("pipeline: Object.freeze"),
      preloadSource.indexOf("providerVisualEvidence: Object.freeze"),
    ),
    /directory|selectedPath|sourcePath/,
  );
});

