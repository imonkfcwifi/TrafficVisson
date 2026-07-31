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
  sanitizeReportDeidentifiedArtifactResponse,
  sanitizeReportDeidentifiedList,
  sanitizeReportDeidentifiedPreparation,
} from "../report-deidentified-evidence.mjs";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const ANALYSIS_JOB_ID = "33333333-3333-4333-8333-333333333333";
const INCIDENT_ID = "44444444-4444-4444-8444-444444444444";
const CANDIDATE_ID = "c".repeat(20);
const DERIVATIVE_ID = `rde_${"d".repeat(32)}`;
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
      "trafficvisson.report-deidentified-evidence.v1",
    project_id: PROJECT_ID,
    pipeline_job_id: JOB_ID,
    analysis_job_id: ANALYSIS_JOB_ID,
    source_sha256: "1".repeat(64),
    incident_id: INCIDENT_ID,
    candidate_id: CANDIDATE_ID,
    derivative_id: DERIVATIVE_ID,
    deidentified_ref: `report-deid:${INCIDENT_ID.replaceAll("-", "")}:${DERIVATIVE_ID.slice(4)}`,
    state: "awaiting_deidentification_review",
    input_revision: "2".repeat(64),
    manifest_sha256: MANIFEST_SHA256,
    original_sha256: "3".repeat(64),
    deidentified_sha256: "4".repeat(64),
    media_type: "image/jpeg",
    size_bytes: 2048,
    width: 640,
    height: 360,
    detector: {
      detector_name: "opencv_haar_report_privacy_proposal",
      detector_version: "4.10.0",
      model_id: "opencv-haar-face-license-plate-cascade-set",
      model_sha256: "5".repeat(64),
      coverage_guaranteed: false,
      human_review_required: true,
    },
    detector_identity_sha256: "6".repeat(64),
    redaction_regions: [
      {
        kind: "license_plate",
        x: 220,
        y: 250,
        width: 120,
        height: 32,
      },
    ],
    redaction_region_count: 1,
    face_detection_count: 0,
    plate_detection_count: 1,
    automatic_redaction_performed: true,
    automatic_privacy_verification: false,
    human_verified: false,
    review_required: true,
    reviewer_decision: null,
    reviewer_actor: null,
    reviewed_at: null,
    review_revision: 0,
    review_record_sha256: null,
    deidentification_record_sha256: null,
    path_exposed: false,
    preparation_revision: "7".repeat(64),
  };
}

test("report derivative IPC accepts only opaque scope and explicit review", () => {
  const base = { projectId: PROJECT_ID, jobId: JOB_ID };
  assert.deepEqual(
    validateIpcPayload(
      IPC_CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_PREPARE,
      base,
    ),
    base,
  );
  assert.deepEqual(
    validateIpcPayload(
      IPC_CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_ARTIFACT,
      {
        ...base,
        incidentId: INCIDENT_ID,
        derivativeId: DERIVATIVE_ID,
      },
    ),
    {
      ...base,
      incidentId: INCIDENT_ID,
      derivativeId: DERIVATIVE_ID,
    },
  );
  assert.deepEqual(
    toBridgePayload(
      IPC_CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_REVIEW,
      {
        ...base,
        incidentId: INCIDENT_ID,
        derivativeId: DERIVATIVE_ID,
        expectedManifestSha256: MANIFEST_SHA256,
        approved: true,
        explicitUserConfirmation: true,
      },
    ),
    {
      project_id: PROJECT_ID,
      job_id: JOB_ID,
      incident_id: INCIDENT_ID,
      derivative_id: DERIVATIVE_ID,
      expected_manifest_sha256: MANIFEST_SHA256,
      approved: true,
      explicit_user_confirmation: true,
    },
  );
  for (const forbidden of [
    { originalPath: "C:\\private\\frame.jpg" },
    { derivativePath: "\\\\server\\share\\frame.jpg" },
    { authToken: "secret" },
    { shell: "powershell.exe" },
    { COM: "HWPFrame.HwpObject" },
  ]) {
    assert.throws(
      () =>
        validateIpcPayload(
          IPC_CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_REVIEW,
          {
            ...base,
            incidentId: INCIDENT_ID,
            derivativeId: DERIVATIVE_ID,
            expectedManifestSha256: MANIFEST_SHA256,
            approved: true,
            explicitUserConfirmation: true,
            ...forbidden,
          },
        ),
      IpcValidationError,
    );
  }
});

test("main process removes all report evidence paths", () => {
  const sanitized = sanitizeReportDeidentifiedPreparation(
    preparation(),
    {
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      incidentId: INCIDENT_ID,
      derivativeId: DERIVATIVE_ID,
    },
  );
  assert.equal(sanitized.reviewRequired, true);
  assert.equal(sanitized.automaticPrivacyVerification, false);
  assert.equal("path_exposed" in sanitized, false);
  assert.doesNotMatch(JSON.stringify(sanitized), /[A-Z]:\\|original_path/i);
  const listed = sanitizeReportDeidentifiedList(
    {
      schema_version:
        "trafficvisson.report-deidentified-evidence-list.v1",
      project_id: PROJECT_ID,
      pipeline_job_id: JOB_ID,
      analysis_job_id: ANALYSIS_JOB_ID,
      state: "prepared_review_required",
      count: 1,
      preparations: [preparation()],
      path_exposed: false,
    },
    { projectId: PROJECT_ID, jobId: JOB_ID },
  );
  assert.equal(listed.count, 1);
  assert.throws(
    () =>
      sanitizeReportDeidentifiedPreparation({
        ...preparation(),
        original_path: "C:\\private\\frame.jpg",
      }),
    /unexpected fields/,
  );
});

test("report artifact bytes are bound to the bridge ETag", async () => {
  const bytes = new Uint8Array([0xff, 0xd8, 0xff, 0xdb, 1, 2, 3, 4]);
  const digest = createHash("sha256").update(bytes).digest("hex");
  const artifact = await sanitizeReportDeidentifiedArtifactResponse(
    new Response(bytes, {
      status: 200,
      headers: {
        "Content-Type": "image/jpeg",
        ETag: `"sha256-${digest}"`,
      },
    }),
  );
  assert.equal(artifact.sha256, digest);
  assert.deepEqual(artifact.bytes, bytes);
  await assert.rejects(
    sanitizeReportDeidentifiedArtifactResponse(
      new Response(bytes, {
        status: 200,
        headers: {
          "Content-Type": "image/jpeg",
          ETag: `"sha256-${"0".repeat(64)}"`,
        },
      }),
    ),
    /hash changed/,
  );
});

test("main and preload expose only report review and bounded JPEG reads", () => {
  for (const channel of [
    "REPORT_DEIDENTIFIED_EVIDENCE_PREPARE",
    "REPORT_DEIDENTIFIED_EVIDENCE_LIST",
    "REPORT_DEIDENTIFIED_EVIDENCE_ARTIFACT",
    "REPORT_DEIDENTIFIED_EVIDENCE_REVIEW",
  ]) {
    assert.match(mainSource, new RegExp(channel));
    assert.match(preloadSource, new RegExp(channel));
  }
  assert.match(
    mainSource,
    /REPORT_DEIDENTIFIED_EVIDENCE_ARTIFACT[\s\S]*requestBinary[\s\S]*maximumBytes:\s*16 \* 1024 \* 1024/,
  );
  assert.match(
    mainSource,
    /REPORT_DEIDENTIFIED_EVIDENCE_REVIEW[\s\S]*delete body\.project_id;[\s\S]*delete body\.job_id;/,
  );
  const start = preloadSource.indexOf(
    "reportDeidentifiedEvidence: Object.freeze",
  );
  const end = preloadSource.indexOf(
    "candidateQuestions: Object.freeze",
    start,
  );
  const surface = preloadSource.slice(start, end);
  assert.match(surface, /\bprepare:/);
  assert.match(surface, /\blist:/);
  assert.match(surface, /\bartifact:/);
  assert.match(surface, /\breview:/);
  assert.doesNotMatch(surface, /path|token|shell|COM/i);
});
