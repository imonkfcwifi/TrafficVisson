import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  IPC_CHANNELS,
  toBridgePayload,
  validateIpcPayload,
} from "../contracts.mjs";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(path.resolve(directory, "..", "main.mjs"), "utf8");
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

function temporalPayload(overrides = {}) {
  return {
    projectId: PROJECT_ID,
    detections: [
      {
        detectionId: "frame-001",
        timestampSeconds: 0,
        issueType: "sign_identity",
        score: 0,
        reviewOnly: true,
        automaticLabelingAllowed: false,
        frameIndex: 0,
      },
    ],
    provenance: {
      projectId: PROJECT_ID,
      analysisJobId: JOB_ID,
      sourceFileSha256: "a".repeat(64),
      source: "classified_review_proposals",
    },
    policy: {
      maxGapSeconds: 2,
      minIou: 0,
      maxCenterDistance: 0,
      strictTrackSeparation: true,
      allowTemporalOnlyWithoutSpatialHints: true,
    },
    ...overrides,
  };
}

test("review computation IPC canonicalizes aliases and preserves false/zero", () => {
  const payload = validateIpcPayload(
    IPC_CHANNELS.REVIEW_TEMPORAL_INCIDENTS,
    temporalPayload(),
  );
  assert.equal(payload.detections[0].issueType, "guide_sign_identity");
  assert.equal(payload.detections[0].timestampSeconds, 0);
  assert.equal(payload.detections[0].score, 0);
  assert.equal(payload.detections[0].automaticLabelingAllowed, false);

  const bridge = toBridgePayload(
    IPC_CHANNELS.REVIEW_TEMPORAL_INCIDENTS,
    temporalPayload(),
  );
  assert.equal(bridge.project_id, PROJECT_ID);
  assert.equal(bridge.detections[0].issue_type, "guide_sign_identity");
  assert.equal(bridge.detections[0].automatic_labeling_allowed, false);
  assert.equal(bridge.policy.min_iou, 0);
});

test("review computation IPC rejects attempts to relax review-only flags", () => {
  const unsafe = temporalPayload();
  unsafe.detections[0].automaticLabelingAllowed = true;
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.REVIEW_TEMPORAL_INCIDENTS,
        unsafe,
      ),
    /review-only/,
  );
});

test("validation and promotion IPC only accept canonical twelve issue types", () => {
  const validation = toBridgePayload(
    IPC_CHANNELS.REVIEW_MODEL_VALIDATION,
    {
      projectId: PROJECT_ID,
      modelVersion: "road-model@0.1.0",
      truthRecords: [
        {
          sampleId: "sample-1",
          issueType: "color_guidance_line_faded",
          truthEventIds: [],
        },
      ],
      predictionRecords: [],
    },
  );
  assert.equal(
    validation.truth_records[0].issue_type,
    "colored_guidance_faded",
  );
  assert.throws(
    () =>
      validateIpcPayload(IPC_CHANNELS.REVIEW_MODEL_PROMOTION, {
        projectId: PROJECT_ID,
        activeModelVersion: "same@1",
        candidateModelVersion: "same@1",
        truthRecords: [],
        activePredictionRecords: [],
        candidatePredictionRecords: [],
        minimumSamplesPerType: 30,
        maximumRecallDrop: 0.02,
      }),
    /must differ/,
  );
});

test("main and preload expose only the three bounded review computation calls", () => {
  assert.match(
    mainSource,
    /REVIEW_TEMPORAL_INCIDENTS[\s\S]*\/review\/temporal-incident-proposals/,
  );
  assert.match(
    mainSource,
    /REVIEW_MODEL_VALIDATION[\s\S]*\/review\/model-validation/,
  );
  assert.match(
    mainSource,
    /REVIEW_MODEL_PROMOTION[\s\S]*\/review\/model-promotion/,
  );
  const start = preloadSource.indexOf("reviewComputations: Object.freeze");
  const end = preloadSource.indexOf("labels: Object.freeze", start);
  assert.ok(start >= 0 && end > start);
  const surface = preloadSource.slice(start, end);
  assert.match(surface, /temporalIncidents:/);
  assert.match(surface, /modelValidation:/);
  assert.match(surface, /modelPromotion:/);
  assert.doesNotMatch(surface, /incidentCreate|annotation|train|promote/i);
});
