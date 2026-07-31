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
const SOURCE_ID = "sample-hwpx-3q-24ffb4bc62cd86a6";
const CASE_ID =
  "hwpx-weak-case-0123456789abcdef0123456789abcdef";
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

test("weak-case approval is explicit, hash-bound and path-free", () => {
  const request = {
    projectId: PROJECT_ID,
    sourceId: SOURCE_ID,
    caseId: CASE_ID,
    expectedDocumentSha256: "a".repeat(64),
    expectedIndexManifestSha256: "b".repeat(64),
    expectedImageSha256: "c".repeat(64),
    issueType: "roadside_mowing_required",
    maintenanceAction: "표지판 시인성 확보를 위한 선택적 삭초",
    caption: "표지판 주변 수풀",
    route: "시험고속도로",
    location: "12.3km",
    evaluation: "삭초 필요",
    opinion: "수목 제거 필요",
    userConfirmed: true,
  };
  assert.deepEqual(
    toBridgePayload(IPC_CHANNELS.WEAK_CASE_APPROVE, request),
    {
      project_id: PROJECT_ID,
      source_id: SOURCE_ID,
      case_id: CASE_ID,
      expected_document_sha256: "a".repeat(64),
      expected_index_manifest_sha256: "b".repeat(64),
      expected_image_sha256: "c".repeat(64),
      issue_type: "roadside_mowing_required",
      maintenance_action: "표지판 시인성 확보를 위한 선택적 삭초",
      caption: "표지판 주변 수풀",
      route: "시험고속도로",
      location: "12.3km",
      evaluation: "삭초 필요",
      opinion: "수목 제거 필요",
      duplicate_image_review_confirmed: false,
      user_confirmed: true,
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.WEAK_CASE_APPROVE, {
        ...request,
        userConfirmed: false,
      }),
    /userConfirmed must be true/,
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.WEAK_CASE_APPROVE, {
        ...request,
        sourcePath: "C:\\secret\\sample.hwpx",
      }),
    IpcValidationError,
  );
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.WEAK_CASE_APPROVED_LIST, {
      projectId: PROJECT_ID,
    }),
    { projectId: PROJECT_ID },
  );
});

test("weak-case standardization sends only confirmed metadata and exact hashes", () => {
  const request = {
    projectId: PROJECT_ID,
    sourceId: SOURCE_ID,
    caseId: CASE_ID,
    provider: "codex",
    expectedDocumentSha256: "a".repeat(64),
    expectedIndexManifestSha256: "b".repeat(64),
    expectedImageSha256: "c".repeat(64),
    metadataTransmissionConfirmed: true,
  };
  assert.deepEqual(
    toBridgePayload(
      IPC_CHANNELS.WEAK_CASE_STANDARDIZATION_PROPOSE,
      request,
    ),
    {
      project_id: PROJECT_ID,
      source_id: SOURCE_ID,
      case_id: CASE_ID,
      provider: "codex",
      expected_document_sha256: "a".repeat(64),
      expected_index_manifest_sha256: "b".repeat(64),
      expected_image_sha256: "c".repeat(64),
      metadata_transmission_confirmed: true,
    },
  );
  assert.deepEqual(
    validateIpcPayload(
      IPC_CHANNELS.WEAK_CASE_STANDARDIZATION_LIST,
      { projectId: PROJECT_ID },
    ),
    { projectId: PROJECT_ID },
  );
  for (const invalid of [
    { metadataTransmissionConfirmed: false },
    { provider: "local" },
    { imagePath: "C:\\secret\\sample.png" },
    { imageBytes: "base64" },
    { attachment: "opaque" },
  ]) {
    assert.throws(
      () =>
        validateIpcPayload(
          IPC_CHANNELS.WEAK_CASE_STANDARDIZATION_PROPOSE,
          { ...request, ...invalid },
        ),
      IpcValidationError,
    );
  }
});

test("weak-case IPC exposes safe search, proposal, approval and listing", () => {
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
    /WEAK_CASE_STANDARDIZATION_PROPOSE[\s\S]*\/reference-cases\/standardization-proposals[\s\S]*timeoutMs:\s*WEAK_CASE_STANDARDIZATION_TIMEOUT_MS/,
  );
  assert.match(
    mainSource,
    /WEAK_CASE_STANDARDIZATION_LIST[\s\S]*\/reference-cases\/standardization-proposals/,
  );
  assert.match(
    mainSource,
    /WEAK_CASE_APPROVE[\s\S]*\/reference-cases\/approvals/,
  );
  assert.match(
    mainSource,
    /WEAK_CASE_APPROVED_LIST[\s\S]*\/reference-cases\/approved/,
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
  assert.match(surface, /proposeStandardization:/);
  assert.match(surface, /standardizationProposals:/);
  assert.match(surface, /approve:/);
  assert.match(surface, /approved:/);
  assert.doesNotMatch(
    surface,
    /path|file|shell|annotation|incident|training/i,
  );
});
