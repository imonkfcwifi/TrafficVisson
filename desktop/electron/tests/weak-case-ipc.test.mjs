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
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(directory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

test("weak-case search accepts only project, canonical issue text and a 3-5 bound", () => {
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.WEAK_CASE_SEARCH, {
      projectId: PROJECT_ID,
      issueType: "roadside_mowing_required",
      queryText: "표지판 주변 수풀과 삭초",
      limit: 5,
    }),
    {
      project_id: PROJECT_ID,
      issue_type: "roadside_mowing_required",
      query_text: "표지판 주변 수풀과 삭초",
      limit: 5,
    },
  );
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.WEAK_CASE_SOURCES_LIST, {
      projectId: PROJECT_ID,
    }),
    { projectId: PROJECT_ID },
  );
  for (const key of [
    "path",
    "sourcePath",
    "documentPath",
    "packagePath",
    "archiveMember",
    "sha256",
  ]) {
    assert.throws(
      () =>
        validateIpcPayload(IPC_CHANNELS.WEAK_CASE_SEARCH, {
          projectId: PROJECT_ID,
          issueType: "roadside_mowing_required",
          limit: 5,
          [key]: "C:\\secret\\sample.hwpx",
        }),
      IpcValidationError,
    );
  }
  for (const limit of [0, 2, 6, true, "5"]) {
    assert.throws(
      () =>
        validateIpcPayload(IPC_CHANNELS.WEAK_CASE_SEARCH, {
          projectId: PROJECT_ID,
          issueType: "pothole",
          limit,
        }),
      IpcValidationError,
    );
  }
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.WEAK_CASE_SEARCH, {
        projectId: PROJECT_ID,
        limit: 5,
      }),
    /requires issueType or queryText/,
  );
});

test("weak-case IPC exposes list/search only and resolves images through the safe protocol", () => {
  assert.match(
    mainSource,
    /const WEAK_CASE_SEARCH_TIMEOUT_MS = 2 \* 60 \* 1000;/,
  );
  assert.match(
    mainSource,
    /WEAK_CASE_SOURCES_LIST[\s\S]*\/reference-cases\/sources/,
  );
  assert.match(
    mainSource,
    /WEAK_CASE_SEARCH[\s\S]*\/reference-cases\/search[\s\S]*timeoutMs:\s*WEAK_CASE_SEARCH_TIMEOUT_MS/,
  );
  assert.match(
    mainSource,
    /parseWeakCaseImageUrl[\s\S]*weakCaseImageBridgePath/,
  );

  const start = preloadSource.indexOf(
    "referenceCases: Object.freeze",
  );
  const end = preloadSource.indexOf("questions: Object.freeze", start);
  assert.ok(start >= 0 && end > start);
  const surface = preloadSource.slice(start, end);
  assert.match(surface, /sources:/);
  assert.match(surface, /search:/);
  assert.doesNotMatch(
    surface,
    /path|file|shell|promote|annotation|incident|training/i,
  );
});
