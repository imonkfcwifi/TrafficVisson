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
const folderBatches = await vite.ssrLoadModule(
  "/src/folderPipelineBatches.ts",
);
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

function v2Profile() {
  return {
    ...profile(),
    schema_version: "trafficvisson.pipeline-profile.v2",
    report_transmission_consent: true,
    template_semantic_map_id: "sample-q3-semantic-v1",
    template_semantic_map_sha256: "e".repeat(64),
    template_semantic_map_ready: true,
    report_provider_ready_at_configuration: true,
  };
}

function currentProfile() {
  return {
    ...v2Profile(),
    schema_version: "trafficvisson.pipeline-profile.v3",
    report_route_name: "서울북부고속도로 (구리-포천)",
    report_inspector_name: "홍길동",
    report_evidence_mode: "shared_deidentified",
    report_inspection_date_policy:
      "source_filename_yyyymmdd_or_block",
  };
}

function v4Profile() {
  return {
    ...currentProfile(),
    schema_version: "trafficvisson.pipeline-profile.v4",
    label_confirmation_authority:
      "structured_llm_training_assist",
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
  assert.equal(parsed.profileSnapshot.reportTransmissionConsent, false);
  assert.equal(parsed.profileSnapshot.templateSemanticMapReady, false);
  assert.equal(
    parsed.profileSnapshot.labelConfirmationAuthority,
    "human_review",
  );
  assert.equal(
    pipeline.pipelineStatusLabel(parsed),
    "기본 2fps 프레임 인덱스",
  );
});

test("parser separates unavailable, unusable evidence, zero detections and review wait", () => {
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

  const reportBlocked = job();
  reportBlocked.status = "blocked";
  reportBlocked.current_stage = "derived_outputs";
  reportBlocked.progress_percent = 100;
  reportBlocked.outcome_code = "report_preview_blocked";
  reportBlocked.error_code = "report_preview_blocked";
  reportBlocked.stages = stages("derived_outputs");
  reportBlocked.stages[11].status = "blocked";
  reportBlocked.stages[11].outcome = "report_preview_blocked";
  reportBlocked.stages[11].error_code = reportBlocked.error_code;
  reportBlocked.stages[11].checkpoint = {
    state: "blocked",
    export_bundle_id: "bundle-safe-v2",
    absolute_paths_exposed: false,
    com_invoked: false,
  };
  assert.equal(
    pipeline.pipelineStatusLabel(pipeline.parsePipelineJob(reportBlocked)),
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

  const unusable = job();
  unusable.status = "completed";
  unusable.current_stage = "derived_outputs";
  unusable.progress_percent = 100;
  unusable.outcome_code = "unusable_visual_evidence";
  unusable.error_code = null;
  unusable.stages = stages("candidate_rescan_5_10fps");
  unusable.stages[4].status = "success";
  unusable.stages[4].outcome = "unusable_visual_evidence";
  for (let index = 5; index < unusable.stages.length; index += 1) {
    unusable.stages[index].status = "skipped";
    unusable.stages[index].outcome = "unusable_visual_evidence";
  }
  assert.equal(
    pipeline.pipelineStatusLabel(pipeline.parsePipelineJob(unusable)),
    "판정 불가 영상 · 재촬영 필요",
  );
});

test("profile parser defaults v1-v3 to human review and requires exact v4 delegation", () => {
  const legacy = pipeline.parsePipelineProfile(profile());
  assert.equal(legacy.schemaVersion, "trafficvisson.pipeline-profile.v1");
  assert.equal(legacy.reportTransmissionConsent, false);
  assert.equal(legacy.templateSemanticMapId, "unconfigured");
  assert.equal(legacy.templateSemanticMapSha256, "0".repeat(64));

  const augmentedLegacy = pipeline.parsePipelineProfile({
    ...profile(),
    report_transmission_consent: false,
    template_semantic_map_id: "unconfigured",
    template_semantic_map_sha256: "0".repeat(64),
    template_semantic_map_ready: false,
    report_provider_ready_at_configuration: false,
  });
  assert.equal(
    augmentedLegacy.schemaVersion,
    "trafficvisson.pipeline-profile.v1",
  );
  assert.equal(augmentedLegacy.reportTransmissionConsent, false);
  const serializedLegacy = pipeline.parsePipelineProfile({
    ...profile(),
    report_transmission_consent: false,
    template_semantic_map_id: "unconfigured",
    template_semantic_map_sha256: "0".repeat(64),
    template_semantic_map_ready: false,
    report_provider_ready_at_configuration: false,
    report_route_name: "unconfigured",
    report_inspector_name: "unconfigured",
    report_evidence_mode: "internal_original",
    report_inspection_date_policy:
      "source_filename_yyyymmdd_or_block",
    label_confirmation_authority: "human_review",
  });
  assert.equal(
    serializedLegacy.labelConfirmationAuthority,
    "human_review",
  );

  const v2 = pipeline.parsePipelineProfile(v2Profile());
  assert.equal(v2.schemaVersion, "trafficvisson.pipeline-profile.v2");
  assert.equal(v2.reportTransmissionConsent, true);
  assert.equal(v2.templateSemanticMapId, "sample-q3-semantic-v1");
  assert.equal(v2.reportRouteName, "unconfigured");
  assert.equal(v2.reportEvidenceMode, "internal_original");
  const serializedV2 = pipeline.parsePipelineProfile({
    ...v2Profile(),
    report_route_name: "unconfigured",
    report_inspector_name: "unconfigured",
    report_evidence_mode: "internal_original",
    report_inspection_date_policy:
      "source_filename_yyyymmdd_or_block",
  });
  assert.equal(
    serializedV2.schemaVersion,
    "trafficvisson.pipeline-profile.v2",
  );
  assert.equal(serializedV2.reportRouteName, "unconfigured");

  const current = pipeline.parsePipelineProfile(currentProfile());
  assert.equal(current.schemaVersion, "trafficvisson.pipeline-profile.v3");
  assert.equal(current.reportTransmissionConsent, true);
  assert.equal(current.templateSemanticMapId, "sample-q3-semantic-v1");
  assert.equal(current.templateSemanticMapReady, true);
  assert.equal(current.reportProviderReadyAtConfiguration, true);
  assert.equal(
    current.reportRouteName,
    "서울북부고속도로 (구리-포천)",
  );
  assert.equal(current.reportInspectorName, "홍길동");
  assert.equal(current.reportEvidenceMode, "shared_deidentified");
  assert.equal(
    current.reportInspectionDatePolicy,
    "source_filename_yyyymmdd_or_block",
  );
  assert.equal(current.labelConfirmationAuthority, "human_review");
  const serializedV3 = pipeline.parsePipelineProfile({
    ...currentProfile(),
    label_confirmation_authority: "human_review",
  });
  assert.equal(
    serializedV3.labelConfirmationAuthority,
    "human_review",
  );

  const v4 = pipeline.parsePipelineProfile(v4Profile());
  assert.equal(v4.schemaVersion, "trafficvisson.pipeline-profile.v4");
  assert.equal(
    v4.labelConfirmationAuthority,
    "structured_llm_training_assist",
  );

  const missingGate = currentProfile();
  delete missingGate.template_semantic_map_sha256;
  assert.throws(
    () => pipeline.parsePipelineProfile(missingGate),
    /응답 형식/,
  );

  const unsafeConsent = currentProfile();
  unsafeConsent.template_semantic_map_ready = false;
  assert.throws(
    () => pipeline.parsePipelineProfile(unsafeConsent),
    /응답 형식/,
  );

  const staleSchemaWithV3Facts = {
    ...currentProfile(),
    schema_version: "trafficvisson.pipeline-profile.v2",
  };
  assert.throws(
    () => pipeline.parsePipelineProfile(staleSchemaWithV3Facts),
    /응답 형식/,
  );

  const controls = currentProfile();
  controls.report_route_name = "서울\n북부";
  assert.throws(
    () => pipeline.parsePipelineProfile(controls),
    /응답 형식/,
  );

  const staleAuthority = {
    ...v4Profile(),
    schema_version: "trafficvisson.pipeline-profile.v3",
  };
  assert.throws(
    () => pipeline.parsePipelineProfile(staleAuthority),
    /응답 형식/,
  );

  const invalidAuthority = {
    ...v4Profile(),
    label_confirmation_authority: "provider_decides_truth",
  };
  assert.throws(
    () => pipeline.parsePipelineProfile(invalidAuthority),
    /응답 형식/,
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

  const explicitNoPath = job();
  explicitNoPath.stages[3].checkpoint = {
    provider_visual_evidence: {
      path_exposed: false,
    },
  };
  assert.equal(
    pipeline.parsePipelineJob(explicitNoPath).stages[3].checkpoint
      .provider_visual_evidence.path_exposed,
    false,
  );
  const falsePathProof = job();
  falsePathProof.stages[3].checkpoint = {
    provider_visual_evidence: {
      path_exposed: true,
    },
  };
  assert.throws(
    () => pipeline.parsePipelineJob(falsePathProof),
    /응답 형식/,
  );

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
  assert.match(importScreenSource, /pipeline\.startFolderBatch\(\{/);
  assert.doesNotMatch(
    importScreenSource,
    /폴더 전체 스트림 검증/,
  );
  assert.match(
    importScreenSource,
    /서버 영속 배치가 파일별 스트림 검증과 저장공간 사전점검을 이어서 수행합니다/,
  );
  assert.match(
    importScreenSource,
    /idempotencyKey:\s*`folder-start-\$\{snapshotId\}-p\$\{profile\.revision\}`/,
  );
  assert.match(importScreenSource, /service\.listFolderBatches\(\{/);
  assert.match(importScreenSource, /pipeline\.retryFolderBatch\(\{/);
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

test("folder batch parser preserves deterministic file order and rejects path-bearing fields", () => {
  const response = {
    schema_version: "trafficvisson.folder-pipeline-batch.v2",
    batch_id: JOB_ID,
    project_id: PROJECT_ID,
    snapshot_id: SNAPSHOT_ID,
    snapshot_digest: SHA,
    idempotency_key: `folder-start-${SNAPSHOT_ID}`,
    input_fingerprint: "b".repeat(64),
    output_location_id: "project-default",
    output_preflight: {
      state: "ready",
      dense_sampling_fps: 2,
      estimated_jpeg_bytes_per_frame: 114_688,
      derived_output_overhead_ratio: 1,
      duration_seconds: 25,
      dense_frame_count: 50,
      estimated_artifact_bytes: 7_680_000,
      safety_reserve_bytes: 4_294_967_296,
      required_free_bytes: 4_302_647_296,
      available_free_bytes: 8_589_934_592,
      error_code: null,
    },
    status: "active",
    enqueue_completed: true,
    recovered_after_restart: true,
    retry_count: 1,
    resume_count: 1,
    created_at: "2026-07-30T00:00:00+00:00",
    updated_at: "2026-07-30T00:00:01+00:00",
    counts: {
      total: 2,
      pending: 0,
      validation_failed: 0,
      enqueue_failed: 0,
      storage_blocked: 0,
      queued_or_running: 2,
      awaiting_review: 0,
      completed: 0,
      pipeline_failed: 0,
    },
    files: [
      {
        order: 1,
        file_name: "A.mov",
        size_bytes: 10,
        intake_state: "enqueued",
        duration_seconds: 12.5,
        estimated_dense_frames: 25,
        estimated_output_bytes: 3_840_000,
        validation_attempts: 1,
        enqueue_attempts: 1,
        pipeline_job_id: "44444444-4444-4444-8444-444444444444",
        pipeline_status: "queued",
        idempotent_replay: false,
        last_error_code: null,
        recovered_after_restart: true,
        retry_available: false,
        resume_available: false,
      },
      {
        order: 2,
        file_name: "b.mp4",
        size_bytes: 11,
        intake_state: "enqueued",
        duration_seconds: 12.5,
        estimated_dense_frames: 25,
        estimated_output_bytes: 3_840_000,
        validation_attempts: 1,
        enqueue_attempts: 1,
        pipeline_job_id: "55555555-5555-4555-8555-555555555555",
        pipeline_status: "queued",
        idempotent_replay: true,
        last_error_code: null,
        recovered_after_restart: true,
        retry_available: false,
        resume_available: false,
      },
    ],
  };
  const parsed = folderBatches.parseFolderPipelineBatch(response);
  assert.deepEqual(
    parsed.files.map((file) => file.fileName),
    ["A.mov", "b.mp4"],
  );
  assert.equal(parsed.recoveredAfterRestart, true);
  assert.throws(
    () =>
      folderBatches.parseFolderPipelineBatch({
        ...response,
        directory: "\\\\server\\private",
      }),
    /허용되지 않은 필드/,
  );
});

test("one-time profile binds review policy, exact sample template hash, local model pack and provider", () => {
  assert.match(importScreenSource, /pipeline\.configureProfile\(\{/);
  assert.match(importScreenSource, /questionPolicy,/);
  assert.match(importScreenSource, /reviewMode,/);
  assert.match(importScreenSource, /labelConfirmationAuthority,/);
  assert.match(importScreenSource, /사용자가 모두 확인/);
  assert.match(importScreenSource, /AI 학습 라벨 보조/);
  assert.match(importScreenSource, /defaultTemplateSha256:\s*template\.sha256/);
  assert.match(
    importScreenSource,
    /DEFAULT_DETECTOR_PACK_ID\s*=\s*[\s\S]*?"trafficvisson-project-dedicated-detector-pending"/,
  );
  assert.match(
    importScreenSource,
    /DEFAULT_OCR_PACK_ID\s*=\s*[\s\S]*?"trafficvisson-jaidedai-easyocr-ko-en"/,
  );
  assert.match(
    importScreenSource,
    /detectorPackId:\s*DEFAULT_DETECTOR_PACK_ID/,
  );
  assert.match(importScreenSource, /ocrPackId:\s*DEFAULT_OCR_PACK_ID/);
  assert.match(importScreenSource, /sam2PackId:\s*DEFAULT_SAM2_PACK_ID/);
  assert.match(importScreenSource, /reportProvider,/);
  assert.match(importScreenSource, /reportTransmissionConsent,/);
  assert.match(importScreenSource, /AI 판단·보고서 Provider/);
  assert.match(
    importScreenSource,
    /labelConfirmationAuthority ===[\s\S]*?"structured_llm_training_assist"[\s\S]*?reportProvider === "none"[\s\S]*?Codex CLI 또는 Claude Code/,
  );
  assert.match(
    importScreenSource,
    /labelConfirmationAuthority ===[\s\S]*?"structured_llm_training_assist"[\s\S]*?!reportTransmissionConsent[\s\S]*?비식별 후보·유사 사례 전송 범위를 확인하고 명시적으로 동의/,
  );
  assert.match(
    importScreenSource,
    /templateSemanticMapId:\s*template\.semanticMapId/,
  );
  assert.match(importScreenSource, /원본 영상·원본 이미지·인증 파일은 전송하지 않음/);
  assert.match(
    importScreenSource,
    /reportRouteName:\s*reportRouteName\.trim\(\)\s*\|\|\s*"unconfigured"/,
  );
  assert.match(
    importScreenSource,
    /reportInspectorName:\s*[\s\S]*reportInspectorName\.trim\(\)\s*\|\|\s*"unconfigured"/,
  );
  assert.match(importScreenSource, /reportEvidenceMode,/);
  assert.match(
    importScreenSource,
    /reportInspectionDatePolicy:\s*REPORT_INSPECTION_DATE_POLICY/,
  );
  assert.match(importScreenSource, /현재 영상 보고서 사실 정보/);
  assert.match(importScreenSource, /영상 파일명의 YYYYMMDD만 검증해 사용합니다/);
  assert.match(
    importScreenSource,
    /const \[outputLocationId,\s*setOutputLocationId\][\s\S]*?useState\("project-default"\)/,
  );
  assert.match(
    importScreenSource,
    /selectedOutputLocation\.state\s*!==\s*"ready"/,
  );
  assert.match(
    importScreenSource,
    /pipeline\.configureProfile\(\{[\s\S]*?\boutputLocationId,/,
  );
  assert.match(importScreenSource, /후보 재분석/);
});
