import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const ANALYSIS_JOB_ID = "22222222-2222-4222-8222-222222222222";
const DENSE_JOB_ID = "33333333-3333-4333-8333-333333333333";
const SHA = "a".repeat(64);
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const module = await vite.ssrLoadModule("/src/denseIndexJobs.ts");
const analysisScreenSource = await readFile(
  new URL("./screens/AnalysisScreen.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(
  new URL("./styles.css", import.meta.url),
  "utf8",
);

after(async () => {
  await vite.close();
});

function safety() {
  return {
    review_only: true,
    automatic_incident_creation: false,
    automatic_annotation_creation: false,
    automatic_training_ingest: false,
    automatic_maintenance_decision: false,
    automatic_model_promotion: false,
  };
}

function queued() {
  return {
    job_id: DENSE_JOB_ID,
    project_id: PROJECT_ID,
    analysis_job_id: ANALYSIS_JOB_ID,
    source_sha256: SHA,
    sampling_fps: 2,
    status: "queued",
    stage: "queued",
    progress_percent: 0,
    progress_state: "waiting",
    progress_basis: "stage_only_no_fabricated_frame_progress",
    pause_supported: false,
    error_code: null,
    created_at: "2026-07-29T01:00:00+00:00",
    updated_at: "2026-07-29T01:00:00+00:00",
    record_sha256: null,
    safety: safety(),
    result: null,
  };
}

function completed() {
  return {
    ...queued(),
    status: "completed",
    stage: "completed",
    progress_percent: 100,
    progress_state: "determinate",
    updated_at: "2026-07-29T01:05:00+00:00",
    record_sha256: "b".repeat(64),
    result: {
      artifact_set_id: DENSE_JOB_ID,
      frame_count: 240,
      total_frame_bytes: 12_345_678,
      index_artifact_ref: `dense-index:${DENSE_JOB_ID}:frame-index`,
    },
  };
}

test("dense-index parser accepts honest waiting, indeterminate, and completed states", () => {
  assert.equal(module.parseDenseIndexJob(queued()).status, "queued");
  const indexing = {
    ...queued(),
    status: "running",
    stage: "indexing_frames",
    progress_percent: null,
    progress_state: "indeterminate",
  };
  assert.equal(
    module.parseDenseIndexJob(indexing).progress_percent,
    null,
  );
  assert.equal(
    module.parseDenseIndexJob(completed()).result.frame_count,
    240,
  );
  assert.equal(
    module.denseIndexStatusLabel(completed()),
    "240프레임 준비됨",
  );
});

test("dense-index parser rejects fabricated progress, unsafe flags, and path fields", () => {
  assert.throws(
    () =>
      module.parseDenseIndexJob({
        ...queued(),
        status: "running",
        stage: "indexing_frames",
        progress_percent: 47,
        progress_state: "determinate",
      }),
    /상태와 결과/,
  );
  assert.throws(
    () =>
      module.parseDenseIndexJob({
        ...queued(),
        safety: {
          ...safety(),
          automatic_incident_creation: true,
        },
      }),
    /응답 형식/,
  );
  assert.throws(
    () =>
      module.parseDenseIndexJob({
        ...queued(),
        source_path: "C:\\secret\\road.mp4",
      }),
    /응답 형식/,
  );
});

test("dense-index parser binds completed result and list to exact identities", () => {
  assert.throws(
    () =>
      module.parseDenseIndexJob({
        ...completed(),
        result: {
          ...completed().result,
          artifact_set_id: ANALYSIS_JOB_ID,
        },
      }),
    /응답 형식/,
  );
  assert.equal(
    module.parseDenseIndexJobList({ jobs: [completed()] }).length,
    1,
  );
  assert.throws(
    () =>
      module.parseDenseIndexJobList({
        jobs: [completed()],
        path: "C:\\secret",
      }),
    /목록 형식/,
  );
});

test("analysis UI starts only from the current completed analysis and shows honest indeterminate progress", () => {
  assert.match(
    analysisScreenSource,
    /analysisJobId:\s*runtime\.analysisSummary\.jobId/,
  );
  assert.match(
    analysisScreenSource,
    /confirmReviewOnly:\s*true/,
  );
  assert.match(
    analysisScreenSource,
    /progress_state === "indeterminate"/,
  );
  assert.match(
    analysisScreenSource,
    /임의 진행률 표시 안 함/,
  );
  assert.match(
    analysisScreenSource,
    /검지·라벨·Incident·학습\s*데이터는 자동 생성하지 않습니다/,
  );
  assert.doesNotMatch(
    analysisScreenSource,
    /denseIndex\.start\([\s\S]{0,500}(sourcePath|sourceSha256)/,
  );
  assert.match(stylesSource, /\.dense-index-control\s*\{/);
  assert.doesNotMatch(
    stylesSource.match(
      /\.dense-index-control\s*\{[\s\S]*?\}/,
    )?.[0] ?? "",
    /overflow/,
  );
});
