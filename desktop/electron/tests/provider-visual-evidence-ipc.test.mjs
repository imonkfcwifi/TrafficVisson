import assert from "node:assert/strict";
import { createHash } from "node:crypto";
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
import {
  providerVisualPreparationIdentities,
  sanitizeProviderVisualArtifactResponse,
  sanitizeProviderVisualPreparation,
} from "../provider-visual-evidence.mjs";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const ANALYSIS_JOB_ID = "33333333-3333-4333-8333-333333333333";
const CANDIDATE_ID = "c".repeat(20);
const PREPARATION_ID = "d".repeat(24);
const MANIFEST_SHA256 = "e".repeat(64);
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(directory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

function preparation() {
  return {
    schema_version:
      "trafficvisson.provider-visual-evidence-preparation.v1",
    project_id: PROJECT_ID,
    pipeline_job_id: JOB_ID,
    analysis_job_id: ANALYSIS_JOB_ID,
    candidate_id: CANDIDATE_ID,
    preparation_id: PREPARATION_ID,
    input_revision: "1".repeat(64),
    state: "awaiting_deidentification_review",
    manifest_sha256: MANIFEST_SHA256,
    engine_identity_sha256: "2".repeat(64),
    attachment_count: 4,
    attachments: [
      {
        order: 0,
        role: "candidate_representative",
        source_ref: `deid:${CANDIDATE_ID}:crop`,
        source_sha256: "3".repeat(64),
        output_sha256: "4".repeat(64),
        media_type: "image/png",
        size_bytes: 1200,
        width: 320,
        height: 180,
        face_detection_count: 1,
        plate_detection_count: 1,
      },
      ...[1, 2, 3].map((order) => ({
        order,
        role: "similar_case",
        source_ref: `reference:source-${order}:case-${order}`,
        source_sha256: ["5", "6", "7"][order - 1].repeat(64),
        output_sha256: ["8", "9", "a"][order - 1].repeat(64),
        media_type: "image/png",
        size_bytes: 900 + order,
        width: 240,
        height: 160,
        face_detection_count: 0,
        plate_detection_count: 0,
      })),
    ],
    automatic_redaction_performed: true,
    automatic_privacy_verification: false,
    human_verified: false,
    review_required: true,
    review_record_sha256: null,
    path_exposed: false,
    preparation_revision: "f".repeat(64),
  };
}

test("privacy-review IPC accepts only opaque identities and explicit confirmation", () => {
  const identity = {
    projectId: PROJECT_ID,
    jobId: JOB_ID,
    candidateId: CANDIDATE_ID,
    preparationId: PREPARATION_ID,
  };
  assert.deepEqual(
    validateIpcPayload(
      IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_LIST,
      { projectId: PROJECT_ID, jobId: JOB_ID },
    ),
    { projectId: PROJECT_ID, jobId: JOB_ID },
  );
  assert.deepEqual(
    validateIpcPayload(
      IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_GET,
      identity,
    ),
    identity,
  );
  assert.deepEqual(
    validateIpcPayload(
      IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_ARTIFACT,
      { ...identity, order: 5 },
    ),
    { ...identity, order: 5 },
  );
  assert.deepEqual(
    toBridgePayload(
      IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_PRIVACY_REVIEW,
      {
        ...identity,
        expectedManifestSha256: MANIFEST_SHA256,
        approved: true,
        explicitUserConfirmation: true,
      },
    ),
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      candidate_id: CANDIDATE_ID,
      preparation_id: PREPARATION_ID,
      expected_manifest_sha256: MANIFEST_SHA256,
      approved: true,
      explicit_user_confirmation: true,
    },
  );
  for (const invalid of [
    { originalPath: "C:\\private\\frame.png" },
    { rawVideo: new Uint8Array([1, 2, 3]) },
    { authToken: "secret" },
    { shell: "powershell.exe" },
    { COM: "HWPFrame.HwpObject" },
  ]) {
    assert.throws(
      () =>
        validateIpcPayload(
          IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_PRIVACY_REVIEW,
          {
            ...identity,
            expectedManifestSha256: MANIFEST_SHA256,
            approved: true,
            explicitUserConfirmation: true,
            ...invalid,
          },
        ),
      IpcValidationError,
    );
  }
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_PRIVACY_REVIEW,
        {
          ...identity,
          expectedManifestSha256: MANIFEST_SHA256,
          approved: true,
          explicitUserConfirmation: false,
        },
      ),
    /explicitly true/,
  );
});

test("main process strips source references and path proof before renderer", () => {
  const sanitized = sanitizeProviderVisualPreparation(preparation(), {
    projectId: PROJECT_ID,
    jobId: JOB_ID,
    candidateId: CANDIDATE_ID,
    preparationId: PREPARATION_ID,
  });
  assert.equal(sanitized.automatic_privacy_verification, false);
  assert.equal(sanitized.attachments.length, 4);
  assert.equal(sanitized.attachments[0].role, "candidate_representative");
  assert.equal("source_ref" in sanitized.attachments[0], false);
  assert.equal("path_exposed" in sanitized, false);
  assert.doesNotMatch(JSON.stringify(sanitized), /[A-Z]:\\|source_ref|path/i);

  assert.throws(
    () =>
      sanitizeProviderVisualPreparation({
        ...preparation(),
        original_path: "C:\\private\\frame.png",
      }),
    /unexpected fields/,
  );
  assert.throws(
    () =>
      sanitizeProviderVisualPreparation({
        ...preparation(),
        path_exposed: true,
      }),
    /exposed a path/,
  );
});

test("preparation discovery emits only immutable candidate/preparation identities", () => {
  const identities = providerVisualPreparationIdentities(
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      stages: [
        {
          name: "vision_proposal",
          checkpoint: {
            provider_visual_evidence_by_candidate: {
              [CANDIDATE_ID]: preparation(),
            },
          },
        },
      ],
    },
    { projectId: PROJECT_ID, jobId: JOB_ID },
  );
  assert.deepEqual(identities, [
    { candidateId: CANDIDATE_ID, preparationId: PREPARATION_ID },
  ]);
  assert.doesNotMatch(JSON.stringify(identities), /path|source|sha256/i);
});

test("artifact IPC verifies response bytes against the bridge ETag", async () => {
  const bytes = new Uint8Array([
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3,
  ]);
  const digest = createHash("sha256").update(bytes).digest("hex");
  const result = await sanitizeProviderVisualArtifactResponse(
    new Response(bytes, {
      status: 200,
      headers: {
        "Content-Type": "image/png",
        ETag: `"sha256-${digest}"`,
      },
    }),
  );
  assert.equal(result.sha256, digest);
  assert.deepEqual(result.bytes, bytes);
  await assert.rejects(
    sanitizeProviderVisualArtifactResponse(
      new Response(bytes, {
        status: 200,
        headers: {
          "Content-Type": "image/png",
          ETag: `"sha256-${"0".repeat(64)}"`,
        },
      }),
    ),
    /hash changed/,
  );
});

test("main and preload expose only the bounded privacy-review surface", () => {
  for (const channel of [
    "PROVIDER_VISUAL_EVIDENCE_LIST",
    "PROVIDER_VISUAL_EVIDENCE_GET",
    "PROVIDER_VISUAL_EVIDENCE_ARTIFACT",
    "PROVIDER_VISUAL_EVIDENCE_PRIVACY_REVIEW",
  ]) {
    assert.match(mainSource, new RegExp(channel));
  }
  assert.match(
    mainSource,
    /PROVIDER_VISUAL_EVIDENCE_ARTIFACT[\s\S]*requestBinary[\s\S]*maximumBytes:\s*8 \* 1024 \* 1024/,
  );
  assert.match(
    mainSource,
    /PROVIDER_VISUAL_EVIDENCE_PRIVACY_REVIEW[\s\S]*delete body\.project_id;[\s\S]*delete body\.job_id;/,
  );
  const start = preloadSource.indexOf(
    "providerVisualEvidence: Object.freeze",
  );
  const end = preloadSource.indexOf(
    "candidateQuestions: Object.freeze",
    start,
  );
  assert.ok(start >= 0 && end > start);
  const surface = preloadSource.slice(start, end);
  for (const method of ["list", "get", "artifact", "review"]) {
    assert.match(surface, new RegExp(`${method}:`));
  }
  assert.doesNotMatch(
    surface,
    /(?:original|rawVideo|path|shell|\bCOM\b|token|auth)/i,
  );
});
