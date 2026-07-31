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
  sanitizeUserDiscoveredReviewFrameArtifact,
  sanitizeUserDiscoveredReviewFrameMetadata,
} from "../user-discovered-review-frame.mjs";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const PIPELINE_JOB_ID = "22222222-2222-4222-8222-222222222222";
const ANALYSIS_JOB_ID = "33333333-3333-4333-8333-333333333333";
const QUEUE_REVISION = "a".repeat(64);
const directory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(directory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(directory, "..", "preload.cjs"),
  "utf8",
);

function request(overrides = {}) {
  return {
    projectId: PROJECT_ID,
    pipelineJobId: PIPELINE_JOB_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    timestampSeconds: 123.5,
    selection: { kind: "point", x: 0.25, y: 0.75 },
    requestSam2Mask: true,
    expectedQuestionQueueRevision: QUEUE_REVISION,
    ...overrides,
  };
}

test("user-discovered candidate IPC carries only opaque scope and bounded selection", () => {
  assert.deepEqual(
    toBridgePayload(
      IPC_CHANNELS.USER_DISCOVERED_CANDIDATE_CREATE,
      request(),
    ),
    {
      project_id: PROJECT_ID,
      pipeline_job_id: PIPELINE_JOB_ID,
      analysis_job_id: ANALYSIS_JOB_ID,
      timestamp_seconds: 123.5,
      selection: { kind: "point", x: 0.25, y: 0.75 },
      request_sam2_mask: true,
      expected_question_queue_revision: QUEUE_REVISION,
    },
  );
  assert.deepEqual(
    validateIpcPayload(
      IPC_CHANNELS.USER_DISCOVERED_CANDIDATE_CREATE,
      request({
        selection: {
          kind: "box",
          x: 0.1,
          y: 0.2,
          width: 0.3,
          height: 0.4,
        },
        issueTypeProposal: "pothole",
      }),
    ).selection,
    {
      kind: "box",
      x: 0.1,
      y: 0.2,
      width: 0.3,
      height: 0.4,
    },
  );
});

test("user-discovered candidate IPC rejects paths, model identity and stale-shaped revisions", () => {
  for (const invalid of [
    request({ sourcePath: "C:\\private\\road.mp4" }),
    request({ modelVersionId: "44444444-4444-4444-8444-444444444444" }),
    request({ expectedQuestionQueueRevision: "questions-v1" }),
    request({ timestampSeconds: Number.NaN }),
    request({ selection: { kind: "point", x: 1.1, y: 0.5 } }),
    request({
      selection: {
        kind: "box",
        x: 0.8,
        y: 0.8,
        width: 0.3,
        height: 0.3,
      },
    }),
  ]) {
    assert.throws(
      () =>
        validateIpcPayload(
          IPC_CHANNELS.USER_DISCOVERED_CANDIDATE_CREATE,
          invalid,
        ),
      IpcValidationError,
    );
  }
});

test("full-timeline review frame IPC accepts only opaque scope and bounded time", () => {
  assert.deepEqual(
    toBridgePayload(
      IPC_CHANNELS.USER_DISCOVERED_REVIEW_FRAME_GET,
      {
        projectId: PROJECT_ID,
        pipelineJobId: PIPELINE_JOB_ID,
        analysisJobId: ANALYSIS_JOB_ID,
        timestampSeconds: 7200.5,
      },
    ),
    {
      project_id: PROJECT_ID,
      pipeline_job_id: PIPELINE_JOB_ID,
      analysis_job_id: ANALYSIS_JOB_ID,
      timestamp_seconds: 7200.5,
    },
  );
  assert.throws(
    () =>
      validateIpcPayload(
        IPC_CHANNELS.USER_DISCOVERED_REVIEW_FRAME_GET,
        {
          projectId: PROJECT_ID,
          pipelineJobId: PIPELINE_JOB_ID,
          analysisJobId: ANALYSIS_JOB_ID,
          timestampSeconds: 1,
          framePath: "E:\\private\\frame.jpg",
        },
      ),
    IpcValidationError,
  );
});

test("main-side review frame sanitizer binds JPEG bytes to server metadata", async () => {
  const bytes = new Uint8Array([
    0xff, 0xd8, 0x10, 0x20, 0x30, 0xff, 0xd9,
  ]);
  const digest = createHash("sha256").update(bytes).digest("hex");
  const raw = {
    schema_version: "trafficvisson.user-discovered-review-frame.v1",
    project_id: PROJECT_ID,
    pipeline_job_id: PIPELINE_JOB_ID,
    analysis_job_id: ANALYSIS_JOB_ID,
    requested_timestamp_seconds: 123.5,
    selected_frame_id: "b".repeat(24),
    selected_frame_index: 247,
    selected_frame_timestamp_seconds: 123.5,
    selected_frame_sha256: digest,
    selected_frame_size_bytes: bytes.byteLength,
    media_type: "image/jpeg",
    source_duration_seconds: 200,
    indexed_duration_seconds: 199.5,
    sampling_fps: 2,
    dense_index_sha256: "c".repeat(64),
    promoted_model_version_id:
      "44444444-4444-4444-8444-444444444444",
    source_analysis_lineage_sha256: "d".repeat(64),
    review_frame_sha256: "e".repeat(64),
    question_queue_revision: QUEUE_REVISION,
    path_exposed: false,
  };
  const expected = {
    projectId: PROJECT_ID,
    pipelineJobId: PIPELINE_JOB_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    timestampSeconds: 123.5,
  };
  const metadata = sanitizeUserDiscoveredReviewFrameMetadata(
    raw,
    expected,
  );
  const response = new Response(bytes, {
    status: 200,
    headers: {
      "Content-Type": "image/jpeg",
      ETag: `"sha256-${digest}"`,
      "X-TrafficSuperVision-Artifact-SHA256": digest,
      "X-TrafficSuperVision-Dense-Frame-ID":
        metadata.selected_frame_id,
      "X-TrafficSuperVision-Dense-Frame-Time": "123.500000",
      "X-TrafficSuperVision-Dense-Index-SHA256":
        metadata.dense_index_sha256,
    },
  });
  const sanitized =
    await sanitizeUserDiscoveredReviewFrameArtifact(
      response,
      metadata,
    );
  assert.deepEqual(sanitized.bytes, bytes);
  await assert.rejects(
    () =>
      sanitizeUserDiscoveredReviewFrameArtifact(
        new Response(bytes, {
          status: 200,
          headers: {
            "Content-Type": "image/jpeg",
            ETag: `"sha256-${"0".repeat(64)}"`,
          },
        }),
        metadata,
      ),
    /binding changed/,
  );
});

test("main owns the exact route and preload exposes no raw request escape hatch", () => {
  assert.match(
    mainSource,
    /USER_DISCOVERED_CANDIDATE_CREATE[\s\S]*\/projects\/\$\{encodeId\(payload\.projectId\)\}\/pipeline-jobs\/\$\{encodeId\(payload\.pipelineJobId\)\}\/analysis\/\$\{encodeId\(payload\.analysisJobId\)\}\/user-discovered-candidates/,
  );
  assert.match(mainSource, /delete body\.project_id/);
  assert.match(mainSource, /delete body\.pipeline_job_id/);
  assert.match(mainSource, /delete body\.analysis_job_id/);
  assert.match(
    mainSource,
    /USER_DISCOVERED_REVIEW_FRAME_GET[\s\S]*user-discovered-review-frame[\s\S]*requestBinary[\s\S]*user-discovered-review-frames/,
  );
  assert.match(
    preloadSource,
    /userDiscoveredCandidates:\s*Object\.freeze\(\{[\s\S]*reviewFrame:\s*\(request\)\s*=>[\s\S]*USER_DISCOVERED_REVIEW_FRAME_GET[\s\S]*create:\s*\(request\)\s*=>[\s\S]*USER_DISCOVERED_CANDIDATE_CREATE/,
  );
  const start = preloadSource.indexOf(
    "userDiscoveredCandidates: Object.freeze",
  );
  const end = preloadSource.indexOf(
    "projectOutputLocations: Object.freeze",
    start,
  );
  const surface = preloadSource.slice(start, end);
  assert.doesNotMatch(
    surface,
    /fetch|requestRaw|sourcePath|modelVersionId|artifactPath|shell/,
  );
});
