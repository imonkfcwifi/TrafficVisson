import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const PIPELINE_JOB_ID = "22222222-2222-4222-8222-222222222222";
const ANALYSIS_JOB_ID = "33333333-3333-4333-8333-333333333333";
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const subject = await vite.ssrLoadModule(
  "/src/userDiscoveredCandidates.ts",
);

after(async () => {
  await vite.close();
});

function response() {
  return {
    schema_version: "trafficvisson.user-discovered-candidate-api.v1",
    project_id: PROJECT_ID,
    pipeline_job_id: PIPELINE_JOB_ID,
    analysis_job_id: ANALYSIS_JOB_ID,
    candidate_id: "a".repeat(20),
    question_id: "44444444-4444-4444-8444-444444444444",
    candidate_kind: "user_discovered",
    requested_timestamp_seconds: 12.6,
    selected_frame_timestamp_seconds: 12.5,
    selected_frame_id: "b".repeat(24),
    selected_frame_sha256: "1".repeat(64),
    selection: { kind: "point", x: 0.4, y: 0.5 },
    issue_type_proposal: null,
    review_issue_type: "issue_type_unclassified",
    request_sha256: "2".repeat(64),
    dense_index_sha256: "3".repeat(64),
    promoted_model_version_id:
      "55555555-5555-4555-8555-555555555555",
    promoted_model_artifact_manifest_sha256: "4".repeat(64),
    promoted_model_inference_sha256: "5".repeat(64),
    dataset_version_id: "66666666-6666-4666-8666-666666666666",
    dataset_hash: "6".repeat(64),
    source_analysis_lineage_sha256: "7".repeat(64),
    overlay_id: `ceo_${"8".repeat(32)}`,
    overlay_manifest_sha256: "9".repeat(64),
    discovery_revision: "a".repeat(64),
    representative_image: {
      artifact_id: "image-1",
      media_type: "image/jpeg",
      size_bytes: 128,
      sha256: "b".repeat(64),
      duration_seconds: null,
      duration_contract: null,
    },
    preview_clip: {
      artifact_id: "clip-1",
      media_type: "video/mp4",
      size_bytes: 256,
      sha256: "c".repeat(64),
      duration_seconds: 3,
      duration_contract: "candidate_overlay_limit_2_to_5_seconds",
    },
    sam2_refinement_request: null,
    review_event_set_id: `res_${"d".repeat(32)}`,
    review_event_set_revision: "e".repeat(64),
    review_event_set_manifest_sha256: "f".repeat(64),
    review_event_binding: {
      event_id: `rve_${"0".repeat(32)}`,
      event_revision: "1".repeat(64),
      representative_candidate_id: "a".repeat(20),
    },
    question_queue_revision: "2".repeat(64),
    question_catalog_revision: "3".repeat(64),
    question_catalog_collection_revision: "4".repeat(64),
    idempotent_replay: false,
    review_only: true,
    annotations_created: 0,
    incidents_created: 0,
    training_members_created: 0,
    report_truth_records_created: 0,
  };
}

const expected = {
  projectId: PROJECT_ID,
  pipelineJobId: PIPELINE_JOB_ID,
  analysisJobId: ANALYSIS_JOB_ID,
  requestedTimestampSeconds: 12.6,
  selection: { kind: "point", x: 0.4, y: 0.5 },
};

test("strict receipt preserves review-only zero-truth and queue revision", () => {
  const parsed = subject.parseUserDiscoveredCandidateReceipt(
    response(),
    expected,
  );
  assert.equal(parsed.candidateId, "a".repeat(20));
  assert.equal(parsed.questionQueueRevision, "2".repeat(64));
  assert.equal(parsed.reviewOnly, true);
  assert.equal(parsed.reviewIssueType, "issue_type_unclassified");
  assert.equal(parsed.sam2RefinementRequested, false);
});

test("receipt fails closed on unknown keys, mismatched scope and claimed truth", () => {
  assert.throws(
    () =>
      subject.parseUserDiscoveredCandidateReceipt(
        { ...response(), local_path: "C:\\private\\frame.jpg" },
        expected,
      ),
    /응답 형식/,
  );
  assert.throws(
    () =>
      subject.parseUserDiscoveredCandidateReceipt(
        { ...response(), annotations_created: 1 },
        expected,
      ),
    /응답 형식/,
  );
  assert.throws(
    () =>
      subject.parseUserDiscoveredCandidateReceipt(
        { ...response(), analysis_job_id: PIPELINE_JOB_ID },
        expected,
      ),
    /일치하지 않습니다/,
  );
});

test("video click normalization rejects letterbox bars and maps contain content", () => {
  const topBar = subject.normalizedVideoPoint({
    clientX: 500,
    clientY: 20,
    left: 0,
    top: 0,
    width: 1000,
    height: 1000,
    videoWidth: 1920,
    videoHeight: 1080,
    objectFit: "contain",
  });
  assert.equal(topBar, null);
  const center = subject.normalizedVideoPoint({
    clientX: 500,
    clientY: 500,
    left: 0,
    top: 0,
    width: 1000,
    height: 1000,
    videoWidth: 1920,
    videoHeight: 1080,
    objectFit: "contain",
  });
  assert.deepEqual(center, { kind: "point", x: 0.5, y: 0.5 });
});

test("full-timeline frame parser verifies scope, timing, queue revision and JPEG hash", async () => {
  const bytes = new Uint8Array([
    0xff, 0xd8, 0x10, 0x20, 0x30, 0xff, 0xd9,
  ]);
  const digest = createHash("sha256").update(bytes).digest("hex");
  const metadata = {
    schema_version: "trafficvisson.user-discovered-review-frame.v1",
    project_id: PROJECT_ID,
    pipeline_job_id: PIPELINE_JOB_ID,
    analysis_job_id: ANALYSIS_JOB_ID,
    requested_timestamp_seconds: 80.5,
    selected_frame_id: "b".repeat(24),
    selected_frame_index: 161,
    selected_frame_timestamp_seconds: 80.5,
    selected_frame_sha256: digest,
    selected_frame_size_bytes: bytes.byteLength,
    media_type: "image/jpeg",
    source_duration_seconds: 100,
    indexed_duration_seconds: 99.5,
    sampling_fps: 2,
    dense_index_sha256: "c".repeat(64),
    promoted_model_version_id:
      "77777777-7777-4777-8777-777777777777",
    source_analysis_lineage_sha256: "d".repeat(64),
    review_frame_sha256: "e".repeat(64),
    question_queue_revision: "f".repeat(64),
    path_exposed: false,
  };
  const parsed = await subject.parseUserDiscoveredReviewFrame(
    { metadata, bytes },
    {
      projectId: PROJECT_ID,
      pipelineJobId: PIPELINE_JOB_ID,
      analysisJobId: ANALYSIS_JOB_ID,
      requestedTimestampSeconds: 80.5,
    },
  );
  assert.equal(parsed.selectedFrameIndex, 161);
  assert.equal(parsed.questionQueueRevision, "f".repeat(64));
  await assert.rejects(
    () =>
      subject.parseUserDiscoveredReviewFrame(
        {
          metadata: {
            ...metadata,
            selected_frame_sha256: "0".repeat(64),
          },
          bytes,
        },
        {
          projectId: PROJECT_ID,
          pipelineJobId: PIPELINE_JOB_ID,
          analysisJobId: ANALYSIS_JOB_ID,
          requestedTimestampSeconds: 80.5,
        },
      ),
    /해시가 변경/,
  );
});

test("known conflicts receive honest user-facing messages", () => {
  assert.match(
    subject.userDiscoveredCandidateErrorMessage(
      new Error("question_queue_revision_stale"),
    ),
    /갱신/,
  );
  assert.match(
    subject.userDiscoveredCandidateErrorMessage(
      new Error("model_source_video_reuse_forbidden"),
    ),
    /독립 조사/,
  );
});
