import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after, beforeEach } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

class MemoryStorage {
  values = new Map();
  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }
  setItem(key, value) {
    this.values.set(String(key), String(value));
  }
  removeItem(key) {
    this.values.delete(String(key));
  }
  clear() {
    this.values.clear();
  }
}

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const SNAPSHOT_ID = "33333333-3333-4333-8333-333333333333";
const SHA = "a".repeat(64);
const memoryStorage = new MemoryStorage();
const previousWindow = globalThis.window;
globalThis.window = { localStorage: memoryStorage };
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const pipeline = await vite.ssrLoadModule("/src/pipelineJobs.ts");
const appShellSource = await readFile(
  new URL("./components/AppShell.tsx", import.meta.url),
  "utf8",
);
const importScreenSource = await readFile(
  new URL("./screens/ImportScreen.tsx", import.meta.url),
  "utf8",
);

after(async () => {
  await vite.close();
  if (previousWindow === undefined) delete globalThis.window;
  else globalThis.window = previousWindow;
});

beforeEach(() => {
  memoryStorage.clear();
  pipeline.resetPipelineUiState();
});

function profile() {
  return {
    schema_version: "trafficvisson.pipeline-profile.v1",
    project_id: PROJECT_ID,
    question_policy: "learning_assist",
    review_mode: "precision",
    default_template_id: "sample-q3-q4",
    default_template_sha256: "b".repeat(64),
    detector_pack_id: "trafficvisson-road-detector",
    ocr_pack_id: "trafficvisson-korean-ocr",
    sam2_pack_id: "trafficvisson-sam2",
    report_provider: "codex",
    output_location_id: "project-default",
    candidate_rescan_fps: 8,
    gps_policy: "timed_track_required_for_route",
    bulk_frame_provider_policy: "local_models_only",
    revision: 1,
    updated_at: "2026-07-29T00:00:00+00:00",
  };
}

function stages(current = "dense_index_2fps") {
  return pipeline.PIPELINE_STAGES.map((name) => ({
    name,
    status:
      pipeline.PIPELINE_STAGES.indexOf(name) <
      pipeline.PIPELINE_STAGES.indexOf(current)
        ? "success"
        : name === current
          ? "running"
          : "pending",
    outcome: null,
    attempts: name === current ? 1 : 0,
    started_at:
      name === current ? "2026-07-29T00:01:00+00:00" : null,
    completed_at: null,
    error_code: null,
    checkpoint: {},
    metrics: {},
  }));
}

function job() {
  return {
    job_id: JOB_ID,
    project_id: PROJECT_ID,
    snapshot_id: SNAPSHOT_ID,
    file_name: "road.mp4",
    idempotency_key: "pipeline-start-0001",
    input_fingerprint: SHA,
    profile_snapshot: profile(),
    capability_snapshot: {
      detector: "missing",
      bulk_frame_provider: "local_models_only",
    },
    status: "running",
    current_stage: "dense_index_2fps",
    progress_percent: 25,
    outcome_code: null,
    error_code: null,
    retry_count: 0,
    resume_count: 0,
    review_completion_id: null,
    stages: stages(),
    provenance: {
      profile_sha256: "c".repeat(64),
      source_sha256: SHA,
    },
    created_at: "2026-07-29T00:00:00+00:00",
    updated_at: "2026-07-29T00:01:00+00:00",
    recovered_after_restart: false,
    record_sha256: "d".repeat(64),
  };
}

test("strict parser preserves one canonical 12-stage DAG", () => {
  const parsed = pipeline.parsePipelineJob(job());
  assert.equal(parsed.jobId, JOB_ID);
  assert.equal(parsed.stages.length, 12);
  assert.equal(parsed.profileSnapshot.reviewMode, "precision");
  assert.equal(
    pipeline.pipelineStatusLabel(parsed),
    "기본 2fps 프레임 인덱스",
  );
});

test("parser separates model unavailable, failure, zero detections and review wait", () => {
  const blocked = job();
  blocked.status = "blocked";
  blocked.current_stage = "candidate_rescan_5_10fps";
  blocked.progress_percent = 33;
  blocked.outcome_code = "model_unavailable";
  blocked.error_code = "candidate_rescan_model_unavailable";
  blocked.stages = stages("candidate_rescan_5_10fps");
  blocked.stages[4].status = "blocked";
  blocked.stages[4].outcome = "model_unavailable";
  blocked.stages[4].error_code = blocked.error_code;
  assert.equal(
    pipeline.pipelineStatusLabel(pipeline.parsePipelineJob(blocked)),
    "모델팩 준비 필요",
  );

  const partial = job();
  partial.status = "blocked";
  partial.current_stage = "derived_outputs";
  partial.progress_percent = 100;
  partial.outcome_code = "partial";
  partial.error_code = "derived_outputs_partial";
  partial.stages = stages("derived_outputs");
  partial.stages[11].status = "blocked";
  partial.stages[11].outcome = "partial";
  partial.stages[11].error_code = partial.error_code;
  partial.stages[11].checkpoint = {
    state: "partial",
    export_bundle_id: "bundle-safe-v1",
    absolute_paths_exposed: false,
    com_invoked: false,
  };
  assert.equal(
    pipeline.pipelineStatusLabel(pipeline.parsePipelineJob(partial)),
    "CSV·증거 저장 완료 · 보고서 준비 대기",
  );

  const awaiting = job();
  awaiting.status = "awaiting_review";
  awaiting.current_stage = "question_queue";
  awaiting.progress_percent = 91;
  awaiting.stages = stages("question_queue");
  assert.equal(
    pipeline.pipelineStatusLabel(pipeline.parsePipelineJob(awaiting)),
    "질문 검토 대기",
  );

  const zero = job();
  zero.status = "completed";
  zero.current_stage = "derived_outputs";
  zero.progress_percent = 100;
  zero.outcome_code = "zero_detections";
  zero.stages = stages("derived_outputs");
  zero.stages[11].status = "success";
  zero.stages[11].outcome = "zero_detections";
  assert.equal(
    pipeline.pipelineStatusLabel(pipeline.parsePipelineJob(zero)),
    "검지 0건 · 정상 완료",
  );
});

test("parser rejects reordered stages, leaked paths and inconsistent completion", () => {
  const reordered = job();
  [reordered.stages[0], reordered.stages[1]] = [
    reordered.stages[1],
    reordered.stages[0],
  ];
  assert.throws(() => pipeline.parsePipelineJob(reordered), /단계 순서/);

  const leaked = job();
  leaked.provenance = { source_path: "C:\\secret\\road.mp4" };
  assert.throws(() => pipeline.parsePipelineJob(leaked), /응답 형식/);

  const falseComplete = job();
  falseComplete.status = "completed";
  falseComplete.current_stage = "derived_outputs";
  falseComplete.progress_percent = 100;
  falseComplete.outcome_code = "failed";
  assert.throws(
    () => pipeline.parsePipelineJob(falseComplete),
    /상태와 결과/,
  );
});

test("only an opaque project and job pointer survives renderer restart", () => {
  const parsed = pipeline.parsePipelineJob(job());
  pipeline.publishPipelineJob(parsed);
  assert.deepEqual(pipeline.pipelinePointerForProject(PROJECT_ID), {
    projectId: PROJECT_ID,
    jobId: JOB_ID,
  });
  const stored = memoryStorage.getItem(
    "trafficvisson:pipeline-pointers:v1",
  );
  assert.equal(stored.includes("source_sha256"), false);
  assert.equal(stored.includes("capabilitySnapshot"), false);
});

test("start and list envelopes are strict", () => {
  assert.equal(
    pipeline.parsePipelineStart({
      job: job(),
      idempotent_replay: true,
    }).idempotentReplay,
    true,
  );
  assert.equal(pipeline.parsePipelineList({ jobs: [job()] }).length, 1);
  assert.throws(
    () =>
      pipeline.parsePipelineStart({
        job: job(),
        idempotent_replay: false,
        source_path: "C:\\secret",
      }),
    /응답 형식/,
  );
});

test("renderer restores and polls one project-scoped pipeline then enters review automatically", () => {
  assert.match(appShellSource, /pipelinePointerForProject\(projectId\)/);
  assert.match(appShellSource, /\.pipeline\.get\(\{/);
  assert.match(appShellSource, /\.pipeline\.list\(\{\s*projectId\s*\}\)/);
  assert.match(
    appShellSource,
    /\["queued",\s*"running",\s*"cancelling"\]\.includes\(job\.status\)/,
  );
  assert.match(appShellSource, /job\.status !== "awaiting_review"/);
  assert.match(appShellSource, /pipelineQuestionQueueBinding\(job\)/);
  assert.match(
    appShellSource,
    /persisted\.source_sha256 !== binding\.sourceSha256/,
  );
  assert.match(appShellSource, /onScreenChange\("label"\)/);
  assert.match(
    appShellSource,
    /job\.status !== "completed"[\s\S]*candidate\.snapshotId === job\.snapshotId[\s\S]*publishPipelineJob\(next\)/,
  );
});

test("normal intake is one rights-confirmed automatic start, not screen-by-screen execution", () => {
  assert.match(importScreenSource, /권리 확인 · 분석 시작/);
  assert.match(importScreenSource, /inspectSource\(true\)/);
  assert.match(
    importScreenSource,
    /async function startPersistentPipelines\([\s\S]*for \(let index = 0; index < uniqueFileNames\.length; index \+= 1\)/,
  );
  assert.match(importScreenSource, /pipeline\.start\(\{/);
  assert.match(importScreenSource, /rightsConfirmed:\s*true/);
  assert.match(
    importScreenSource,
    /idempotencyKey:\s*`pipeline-start-\$\{snapshotId\}-\$\{String\(/,
  );
  assert.match(
    importScreenSource,
    /폴더 영상 \$\{startedJobs\.length\}개를 모두 자동 분석 대기열에 등록했습니다/,
  );
  assert.match(importScreenSource, /폴더 전체 업로드 확인/);
  assert.match(
    importScreenSource,
    /폴더 전체 A\/B\/C 검사 · 전체 자동 분석 시작/,
  );
  assert.doesNotMatch(importScreenSource, /onNavigate\("analysis"\)/);
  assert.doesNotMatch(importScreenSource, /hashAndRegister|startActualAnalysis/);
});

test("one-time profile binds review policy, exact sample template hash, local model pack and provider", () => {
  assert.match(importScreenSource, /pipeline\.configureProfile\(\{/);
  assert.match(importScreenSource, /questionPolicy,/);
  assert.match(importScreenSource, /reviewMode,/);
  assert.match(importScreenSource, /defaultTemplateSha256:\s*template\.sha256/);
  assert.match(importScreenSource, /detectorPackId:\s*COMMON_MODEL_PACK_ID/);
  assert.match(importScreenSource, /ocrPackId:\s*COMMON_MODEL_PACK_ID/);
  assert.match(importScreenSource, /sam2PackId:\s*COMMON_MODEL_PACK_ID/);
  assert.match(importScreenSource, /reportProvider,/);
  assert.match(importScreenSource, /outputLocationId:\s*"project-default"/);
  assert.match(importScreenSource, /후보 재분석/);
});
