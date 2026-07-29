import assert from "node:assert/strict";
import test, { after, beforeEach } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }

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
const HASH_JOB_ID = "44444444-4444-4444-8444-444444444444";
const ARTIFACT_SET_ID = "55555555-5555-4555-8555-555555555555";
const SHA = "a".repeat(64);
const IMAGE_SHA = "b".repeat(64);
const CLIP_SHA = "c".repeat(64);
const MANIFEST_SHA = "d".repeat(64);
const CANDIDATE_ID = "0123456789abcdefabcd";
const STORAGE_KEY = "trafficvisson:runtime:v1";

function completedJob() {
  return {
    job_id: JOB_ID,
    project_id: PROJECT_ID,
    snapshot_id: SNAPSHOT_ID,
    hash_job_id: HASH_JOB_ID,
    file_name: "천안논산 천안방향.mp4",
    source_sha256: SHA,
    status: "completed",
    stage: "completed",
    progress_percent: 100,
    pause_supported: false,
    error_code: null,
    created_at: "2026-07-28T01:00:00+00:00",
    updated_at: "2026-07-28T01:05:00+00:00",
    result: {
      artifact_set_id: ARTIFACT_SET_ID,
      candidate_count: 1,
      artifact_count: 2,
      total_artifact_bytes: 3600,
      provenance: {
        source_size_bytes: 152347244,
        duration_seconds: 583.448,
        source_location: {
          latitude: 37.0545,
          longitude: 126.8899,
          altitude_meters: null,
          raw_iso6709: "+37.0545+126.8899/",
          scope: "capture_start_only",
        },
      },
      candidates: [
        {
          candidate_id: CANDIDATE_ID,
          start_seconds: 11,
          end_seconds: 13,
          representative_timestamp_seconds: 12,
          scene_score: 0.72,
          sharpness_score: 0.81,
          triggers: ["scene_change", "sharp_frame"],
          observations: [
            {
              timestamp_seconds: 12,
              scene_score: 0.72,
              sharpness_score: 0.81,
              triggers: ["scene_change"],
            },
          ],
          representative_image: {
            relative_path: `${CANDIDATE_ID}.jpg`,
            size_bytes: 1200,
            sha256: IMAGE_SHA,
            media_type: "image/jpeg",
            artifact_ref: `analysis:${JOB_ID}:${CANDIDATE_ID}.jpg`,
          },
          preview_clip: {
            relative_path: `${CANDIDATE_ID}.mp4`,
            size_bytes: 2400,
            sha256: CLIP_SHA,
            media_type: "video/mp4",
            artifact_ref: `analysis:${JOB_ID}:${CANDIDATE_ID}.mp4`,
          },
          preview_start_seconds: 10,
          preview_duration_seconds: 3,
          ai_inference: {
            schema_version: "trafficvisson.vision-inference.v1",
            mode: "local_model",
            status: "classified",
            provider: "transformers_clip_zero_shot",
            pack_id: "road-clip-experimental",
            pack_version: "0.1.0",
            manifest_sha256: MANIFEST_SHA,
            capabilities: ["candidate_classification"],
            predictions: [
              {
                issue_type: "roadside_mowing_required",
                confidence: 0.09,
                box: null,
                source_label: "삭초 필요",
                calibration: "zero_shot_relative",
              },
            ],
            ocr_observations: [],
            duration_ms: 37,
            warning_code: null,
          },
        },
      ],
    },
  };
}

function clone(value) {
  return structuredClone(value);
}

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
const analysisJobs = await vite.ssrLoadModule("/src/analysisJobs.ts");
const analysisPresentation = await vite.ssrLoadModule(
  "/src/analysisPresentation.ts",
);
const candidateProposal = await vite.ssrLoadModule(
  "/src/candidateProposal.ts",
);
const experimentalProposals = await vite.ssrLoadModule(
  "/src/experimentalProposals.ts",
);
const runtimeState = await vite.ssrLoadModule("/src/runtimeState.ts");
const reviewRecords = await vite.ssrLoadModule("/src/reviewRecords.ts");

after(async () => {
  await vite.close();
  if (previousWindow === undefined) {
    delete globalThis.window;
  } else {
    globalThis.window = previousWindow;
  }
});

beforeEach(() => {
  memoryStorage.clear();
  runtimeState.clearRuntime();
});

test("persisted candidate answers hydrate a project and job scoped review cache", () => {
  const key = reviewRecords.reviewStorageKey(PROJECT_ID, JOB_ID);
  const rows = reviewRecords.reviewRecordsFromQuestionList(
    {
      questions: [
        {
          question_id: "question-1",
          candidate_id: CANDIDATE_ID,
          status: "approved",
          latest_answer: {
            answer: "edit",
            selected_issue_type: "pothole",
            severity: "major",
            maintenance_action: "포트홀 긴급 보수",
            ocr_text: "",
            normalized_box: {
              x: 0.1,
              y: 0.2,
              width: 0.3,
              height: 0.4,
            },
          },
          answered_at: "2026-07-28T03:00:00+00:00",
          answer_revision: 2,
        },
      ],
    },
    JOB_ID,
  );

  assert.deepEqual(rows, [
    {
      question: 0,
      questionId: "question-1",
      candidateId: CANDIDATE_ID,
      answer: "edited",
      selectedType: "POTHOLE",
      finalGeometry: { x: 10, y: 20, width: 30, height: 40 },
      severity: "높음",
      action: "포트홀 긴급 보수",
      ocrText: "",
      reviewedAt: "2026-07-28T03:00:00+00:00",
      trainingStatus: "approved",
      answerRevision: 2,
    },
  ]);
  reviewRecords.saveReviewHistory(key, rows);
  assert.deepEqual(reviewRecords.loadReviewHistory(key), rows);
  assert.equal(
    memoryStorage.getItem(key),
    JSON.stringify(rows),
  );
});

test("answer hydration rejects malformed answers and unsafe normalized boxes", () => {
  const rows = reviewRecords.reviewRecordsFromQuestionList(
    {
      questions: [
        {
          candidate_id: CANDIDATE_ID,
          status: "held",
          latest_answer: {
            answer: "unsure",
            normalized_box: {
              x: 0.9,
              y: 0.9,
              width: 0.2,
              height: 0.2,
            },
          },
        },
        {
          candidate_id: "second-candidate",
          status: "approved",
          latest_answer: { answer: "attacker-value" },
        },
      ],
    },
    JOB_ID,
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].trainingStatus, "held");
  assert.equal(rows[0].finalGeometry, undefined);
});

test("saved analysis responses parse and map every runtime evidence field", () => {
  const job = completedJob();
  const parsed = analysisJobs.parseAnalysisJob(job);
  assert.deepEqual(
    analysisJobs.parseAnalysisJobList({ jobs: [job] }),
    [parsed],
  );
  assert.equal(
    analysisJobs.firstProjectId({
      projects: [{ id: PROJECT_ID.toUpperCase() }],
    }),
    PROJECT_ID,
  );

  const restored = analysisJobs.toRestoredRuntime(parsed);
  assert.deepEqual(restored.activeImport, {
    projectId: PROJECT_ID,
    fileName: "천안논산 천안방향.mp4",
    sizeBytes: 152347244,
    durationSeconds: 583.448,
    snapshotId: SNAPSHOT_ID,
    sha256: SHA,
    hashJobId: HASH_JOB_ID,
    registeredAt: "2026-07-28T01:05:00+00:00",
  });
  assert.deepEqual(restored.analysisSummary, {
    artifactSetId: ARTIFACT_SET_ID,
    projectId: PROJECT_ID,
    jobId: JOB_ID,
    sourceFileName: "천안논산 천안방향.mp4",
    sourceSha256: SHA,
    candidateCount: 1,
    artifactCount: 2,
    totalArtifactBytes: 3600,
    completedAt: "2026-07-28T01:05:00+00:00",
    sourceLocation: {
      latitude: 37.0545,
      longitude: 126.8899,
      altitudeMeters: null,
      rawIso6709: "+37.0545+126.8899/",
      scope: "capture_start_only",
    },
    candidates: [
      {
        candidateId: CANDIDATE_ID,
        startSeconds: 11,
        endSeconds: 13,
        representativeTimestampSeconds: 12,
        sceneScore: 0.72,
        sharpnessScore: 0.81,
        triggers: ["scene_change", "sharp_frame"],
        observations: [
          {
            timestampSeconds: 12,
            sceneScore: 0.72,
            sharpnessScore: 0.81,
            triggers: ["scene_change"],
          },
        ],
        representativeImage: {
          relativePath: `${CANDIDATE_ID}.jpg`,
          sizeBytes: 1200,
          sha256: IMAGE_SHA,
          mediaType: "image/jpeg",
          artifactRef: `analysis:${JOB_ID}:${CANDIDATE_ID}.jpg`,
        },
        previewClip: {
          relativePath: `${CANDIDATE_ID}.mp4`,
          sizeBytes: 2400,
          sha256: CLIP_SHA,
          mediaType: "video/mp4",
          artifactRef: `analysis:${JOB_ID}:${CANDIDATE_ID}.mp4`,
        },
        previewStartSeconds: 10,
        previewDurationSeconds: 3,
        aiInference: {
          schemaVersion: "trafficvisson.vision-inference.v1",
          mode: "local_model",
          status: "classified",
          provider: "transformers_clip_zero_shot",
          packId: "road-clip-experimental",
          packVersion: "0.1.0",
          manifestSha256: MANIFEST_SHA,
          capabilities: ["candidate_classification"],
          predictions: [
            {
              issueType: "roadside_mowing_required",
              confidence: 0.09,
              box: null,
              sourceLabel: "삭초 필요",
              calibration: "zero_shot_relative",
            },
          ],
          ocrObservations: [],
          durationMs: 37,
          warningCode: null,
        },
      },
    ],
  });

  const legacy = completedJob();
  delete legacy.result.provenance.source_location;
  delete legacy.result.candidates[0].ai_inference;
  assert.equal(
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(legacy),
    ).analysisSummary.sourceLocation,
    null,
  );
  assert.equal(
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(legacy),
    ).analysisSummary.candidates[0].aiInference,
    undefined,
  );

  const explicitNull = completedJob();
  explicitNull.result.candidates[0].ai_inference = null;
  assert.equal(
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(explicitNull),
    ).analysisSummary.candidates[0].aiInference,
    undefined,
  );
});

test("analysis model proposals normalize legacy issue codes to the 12-type contract", () => {
  const legacy = completedJob();
  legacy.result.candidates[0].ai_inference.predictions[0].issue_type =
    "sign_identity";
  assert.equal(
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(legacy),
    ).analysisSummary.candidates[0].aiInference.predictions[0].issueType,
    "guide_sign_identity",
  );

  const current = completedJob();
  current.result.candidates[0].ai_inference.predictions[0].issue_type =
    "tunnel_name_occlusion";
  assert.equal(
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(current),
    ).analysisSummary.candidates[0].aiInference.predictions[0].issueType,
    "tunnel_name_occlusion",
  );
});

test("classified local-model output becomes an explicit unapproved question proposal", () => {
  const candidate =
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(completedJob()),
    ).analysisSummary.candidates[0];
  const proposal = candidateProposal.buildCandidateProposal(candidate);

  assert.deepEqual(proposal, {
    typeCode: "ROADSIDE_MOWING_REQUIRED",
    displayName: "삭초 필요",
    prompt:
      "AI가 이 장면에서 삭초 필요 가능성을 제안했습니다. 해당 유형이 맞나요?",
    confidence: 0.09,
    scoreKind: "classification",
    scoreText: "상대 점수 0.090 · 확률 또는 확정 판정이 아님",
    experimental: true,
    forced: false,
    rationale:
      "실험적 로컬 모델 road-clip-experimental@0.1.0의 상대 점수입니다. 장면 단위 제안이며 AI가 제안한 영역은 없습니다. 이 제안만으로 자동 승인·긴급도·유지보수 조치·보고서 반영을 결정하지 않습니다.",
  });
  assert.equal(candidate.aiInference.predictions[0].box, null);
});

test("review sidecar proposals overlay legacy candidates without creating a label", () => {
  const job = completedJob();
  delete job.result.candidates[0].ai_inference;
  const restored = analysisJobs.toRestoredRuntime(
    analysisJobs.parseAnalysisJob(job),
  );
  const candidate = restored.analysisSummary.candidates[0];
  const value = {
    project_id: PROJECT_ID,
    analysis_job_id: JOB_ID,
    questions: [
      {
        candidate_id: CANDIDATE_ID,
        candidate: job.result.candidates[0],
        latest_answer: null,
        experimental_proposal: {
          schema_version: "trafficvisson.experimental-candidate-proposal.v1",
          run_id: "66666666-6666-4666-8666-666666666666",
          project_id: PROJECT_ID,
          analysis_job_id: JOB_ID,
          candidate_id: CANDIDATE_ID,
          created_at: "2026-07-28T09:00:00+00:00",
          status: "classified",
          review_only: true,
          automatic_labeling_allowed: false,
          score_semantics: "zero_shot_relative_not_probability",
          model: {
            provider: "transformers_clip_zero_shot",
            pack_id: "trafficvisson-road-clip",
            pack_version: "0.1.0",
            manifest_sha256: MANIFEST_SHA,
          },
          dataset: {
            dataset_id: "candidate-batch-349",
            dataset_version: "2026-07-28",
            dataset_sha256: SHA,
          },
          evaluation: {
            evaluation_id: "clip-road-baseline-20260728-all-candidates-v1",
            producer: "trafficvisson-batch-evaluator",
            producer_version: "0.1.0",
          },
          source: {
            representative_image_sha256: IMAGE_SHA,
            analysis_candidate_sha256: CLIP_SHA,
          },
          predictions: [
            {
              issue_type: "roadside_mowing_required",
              relative_score: 0.42,
              model_score: null,
              box: null,
              source_label: "삭초 필요",
              calibration: "zero_shot_relative",
            },
          ],
        },
      },
    ],
  };

  const overlays =
    experimentalProposals.experimentalProposalOverlaysFromQuestionList(value, {
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      candidates: restored.analysisSummary.candidates,
    });
  assert.equal(overlays.declaredCount, 1);
  assert.equal(overlays.ignoredCount, 0);
  assert.equal(
    overlays.byCandidateId[CANDIDATE_ID].modelIdentity,
    "trafficvisson-road-clip@0.1.0",
  );
  assert.equal(candidate.aiInference, undefined);

  const proposal = candidateProposal.buildCandidateProposal({
    ...candidate,
    aiInference: overlays.byCandidateId[CANDIDATE_ID].inference,
  });
  assert.equal(proposal.typeCode, "ROADSIDE_MOWING_REQUIRED");
  assert.equal(proposal.scoreText, "상대 점수 0.420 · 확률 또는 확정 판정이 아님");
  assert.equal(proposal.experimental, true);
  assert.equal(proposal.forced, false);
});

test("review sidecar parser ignores legacy, mismatched, and unsafe proposals", () => {
  const job = completedJob();
  delete job.result.candidates[0].ai_inference;
  const candidates =
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(job),
    ).analysisSummary.candidates;
  const baseProposal = {
    schema_version: "trafficvisson.experimental-candidate-proposal.v1",
    run_id: "66666666-6666-4666-8666-666666666666",
    project_id: PROJECT_ID,
    analysis_job_id: JOB_ID,
    candidate_id: CANDIDATE_ID,
    created_at: "2026-07-28T09:00:00+00:00",
    status: "classified",
    review_only: true,
    automatic_labeling_allowed: false,
    score_semantics: "zero_shot_relative_not_probability",
    model: {
      provider: "transformers_clip_zero_shot",
      pack_id: "trafficvisson-road-clip",
      pack_version: "0.1.0",
      manifest_sha256: MANIFEST_SHA,
    },
    dataset: null,
    evaluation: {
      evaluation_id: "clip-road-baseline-20260728-all-candidates-v1",
      producer: "trafficvisson-batch-evaluator",
      producer_version: "0.1.0",
    },
    source: {
      representative_image_sha256: IMAGE_SHA,
      analysis_candidate_sha256: CLIP_SHA,
    },
    predictions: [
      {
        issue_type: "road_debris",
        relative_score: 0.88,
        model_score: null,
        box: null,
        source_label: "낙하물",
        calibration: "zero_shot_relative",
      },
    ],
  };
  const parse = (experimentalProposal) =>
    experimentalProposals.experimentalProposalOverlaysFromQuestionList(
      {
        project_id: PROJECT_ID,
        analysis_job_id: JOB_ID,
        questions: [
          {
            candidate_id: CANDIDATE_ID,
            candidate: job.result.candidates[0],
            experimental_proposal: experimentalProposal,
          },
        ],
      },
      { projectId: PROJECT_ID, jobId: JOB_ID, candidates },
    );

  const legacy =
    experimentalProposals.experimentalProposalOverlaysFromQuestionList(
      {
        project_id: PROJECT_ID,
        analysis_job_id: JOB_ID,
        questions: [
          {
            candidate_id: CANDIDATE_ID,
            candidate: job.result.candidates[0],
          },
        ],
      },
      { projectId: PROJECT_ID, jobId: JOB_ID, candidates },
    );
  assert.deepEqual(legacy, {
    byCandidateId: {},
    declaredCount: 0,
    ignoredCount: 0,
  });

  for (const unsafe of [
    { ...baseProposal, automatic_labeling_allowed: true },
    {
      ...baseProposal,
      score_semantics: "model_score_not_probability",
    },
    {
      ...baseProposal,
      source: {
        representative_image_sha256: "f".repeat(64),
        analysis_candidate_sha256: CLIP_SHA,
      },
    },
    { ...baseProposal, candidate_id: "f".repeat(20) },
    {
      ...baseProposal,
      predictions: [
        {
          ...baseProposal.predictions[0],
          relative_score: 2,
        },
      ],
    },
    {
      ...baseProposal,
      predictions: [
        {
          ...baseProposal.predictions[0],
          box: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
        },
      ],
    },
    {
      ...baseProposal,
      model: {
        ...baseProposal.model,
        provider: "onnx_yolo_v8",
      },
    },
  ]) {
    const result = parse(unsafe);
    assert.equal(result.declaredCount, 1);
    assert.equal(result.ignoredCount, 1);
    assert.deepEqual(result.byCandidateId, {});
  }

  const safe = parse(baseProposal);
  const inference = safe.byCandidateId[CANDIDATE_ID].inference;
  const proposal = candidateProposal.buildCandidateProposal({
    ...candidates[0],
    aiInference: inference,
  });
  assert.equal(proposal.typeCode, "ROAD_DEBRIS");
  assert.equal(proposal.forced, true);

  const detector = parse({
    ...baseProposal,
    score_semantics: "model_score_not_probability",
    model: {
      ...baseProposal.model,
      provider: "onnx_yolo_v8",
      pack_id: "trafficvisson-road-yolo",
    },
    predictions: [
      {
        issue_type: "pothole",
        relative_score: null,
        model_score: 0.61,
        box: { x: 0.2, y: 0.5, width: 0.3, height: 0.2 },
        source_label: "pothole",
        calibration: "model_score",
      },
    ],
  });
  assert.equal(detector.ignoredCount, 0);
  assert.deepEqual(
    detector.byCandidateId[CANDIDATE_ID].inference.predictions[0],
    {
      issueType: "pothole",
      confidence: 0.61,
      box: { x: 0.2, y: 0.5, width: 0.3, height: 0.2 },
      sourceLabel: "pothole",
      calibration: "model_score",
    },
  );
  const detectorProposal = candidateProposal.buildCandidateProposal({
    ...candidates[0],
    aiInference: detector.byCandidateId[CANDIDATE_ID].inference,
  });
  assert.equal(detectorProposal.typeCode, "POTHOLE");
  assert.equal(
    detectorProposal.scoreText,
    "모델 점수 0.610 · 확률 또는 확정 판정이 아님",
  );
  assert.deepEqual(detectorProposal.box, {
    x: 20,
    y: 50,
    width: 30,
    height: 20,
  });

  const groundingDetector = parse({
    ...baseProposal,
    score_semantics: "model_score_not_probability",
    model: {
      ...baseProposal.model,
      provider: "transformers_grounding_dino",
      pack_id: "trafficvisson-grounding-dino-tiny",
    },
    predictions: [
      {
        issue_type: "sign_vegetation_occlusion",
        relative_score: null,
        model_score: 0.47,
        box: { x: 0.12, y: 0.18, width: 0.34, height: 0.28 },
        source_label: "road sign obscured by vegetation",
        calibration: "model_score",
      },
    ],
  });
  assert.equal(groundingDetector.ignoredCount, 0);
  assert.equal(
    groundingDetector.byCandidateId[CANDIDATE_ID].inference.provider,
    "transformers_grounding_dino",
  );
  assert.deepEqual(
    groundingDetector.byCandidateId[CANDIDATE_ID].inference.capabilities,
    ["defect_detection"],
  );
});

test("object-model boxes are preserved and road debris stays mandatory", () => {
  const job = completedJob();
  const inference = job.result.candidates[0].ai_inference;
  inference.provider = "onnx_yolo_v8";
  inference.capabilities = ["defect_detection"];
  inference.predictions[0] = {
    issue_type: "road_debris",
    confidence: 0.88,
    box: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
    source_label: "낙하물",
    calibration: "model_score",
  };
  const candidate =
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(job),
    ).analysisSummary.candidates[0];
  const proposal = candidateProposal.buildCandidateProposal(candidate);

  assert.equal(proposal.typeCode, "ROAD_DEBRIS");
  assert.equal(proposal.forced, true);
  assert.equal(proposal.scoreText, "모델 점수 0.880 · 확률 또는 확정 판정이 아님");
  assert.deepEqual(proposal.box, {
    x: 10,
    y: 20,
    width: 30,
    height: 40,
  });
});

test("OCR-only model output stays unverified and requires a user-selected identity type", () => {
  const job = completedJob();
  const inference = job.result.candidates[0].ai_inference;
  inference.provider = "easyocr_ocr_only";
  inference.capabilities = ["ocr"];
  inference.predictions = [];
  inference.ocr_observations = [
    {
      text: "  천안   터널  ",
      confidence: 0.71,
      box: { x: 0.2, y: 0.1, width: 0.5, height: 0.2 },
      kind: "sign_or_tunnel_text_unverified",
    },
  ];
  const candidate =
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(job),
    ).analysisSummary.candidates[0];
  const proposal = candidateProposal.buildCandidateProposal(candidate);

  assert.deepEqual(proposal, {
    typeCode: "UNCLASSIFIED_ROAD_EVENT",
    displayName: "이정표·터널명 OCR",
    prompt:
      "AI OCR이 “천안 터널”로 읽었습니다. 실제 이정표 또는 터널명인지 확인해 주세요.",
    confidence: 0.71,
    scoreKind: "classification",
    scoreText: "OCR 모델 점수 0.710 · 글자 또는 시설명 확정 아님",
    experimental: true,
    forced: false,
    rationale:
      "실험적 로컬 모델 road-clip-experimental@0.1.0의 검증 전 OCR 결과입니다. 이정표인지 터널명인지 자동 판정하지 않으며, 사용자가 유형과 실제 글자를 직접 확인해야 합니다.",
    box: { x: 20, y: 10, width: 50, height: 20 },
  });
});

test("fallback, no-detection, and legacy candidates stay unclassified", () => {
  const base =
    analysisJobs.toRestoredRuntime(
      analysisJobs.parseAnalysisJob(completedJob()),
    ).analysisSummary.candidates[0];
  const cases = [];

  const noDetection = clone(base);
  noDetection.aiInference.status = "no_detection";
  noDetection.aiInference.predictions = [];
  cases.push(noDetection);

  const fallback = clone(base);
  fallback.aiInference = {
    schemaVersion: "trafficvisson.vision-inference.v1",
    mode: "scene_fallback",
    status: "provider_unavailable",
    provider: "none",
    packId: null,
    packVersion: null,
    manifestSha256: null,
    capabilities: [],
    predictions: [],
    ocrObservations: [],
    durationMs: 0,
    warningCode: "model_pack_not_configured",
  };
  cases.push(fallback);

  const legacy = clone(base);
  delete legacy.aiInference;
  cases.push(legacy);

  for (const candidate of cases) {
    const proposal = candidateProposal.buildCandidateProposal(candidate);
    assert.equal(proposal.typeCode, "UNCLASSIFIED_ROAD_EVENT");
    assert.equal(proposal.experimental, false);
    assert.equal(proposal.scoreKind, "scene");
    assert.equal(proposal.box, undefined);
  }
});

test("restoreCompletedAnalysis publishes and durably caches the restored state", () => {
  const restored = analysisJobs.toRestoredRuntime(
    analysisJobs.parseAnalysisJob(completedJob()),
  );
  let notifications = 0;
  const unsubscribe = runtimeState.subscribeRuntime(() => {
    notifications += 1;
  });

  runtimeState.restoreCompletedAnalysis(
    restored.activeImport,
    restored.analysisSummary,
  );
  unsubscribe();

  const state = runtimeState.getRuntimeState();
  assert.equal(notifications, 1);
  assert.equal(state.status, "complete");
  assert.equal(state.progress, 100);
  assert.equal(state.stage, 3);
  assert.equal(state.jobId, JOB_ID);
  assert.equal(state.message, "후보 1건 · 저장된 분석 복구");
  assert.deepEqual(state.activeImport, restored.activeImport);
  assert.deepEqual(state.analysisSummary, restored.analysisSummary);
  assert.deepEqual(
    JSON.parse(memoryStorage.getItem(STORAGE_KEY)),
    state,
  );
});

test("saved analysis parser rejects corrupt state and evidence metadata", () => {
  const corruptions = [
    [
      "out-of-range progress",
      (job) => {
        job.progress_percent = 101;
      },
    ],
    [
      "pause support contract drift",
      (job) => {
        job.pause_supported = true;
      },
    ],
    [
      "completed state mismatch",
      (job) => {
        job.stage = "extracting_candidates";
      },
    ],
    [
      "candidate count mismatch",
      (job) => {
        job.result.candidate_count = 2;
      },
    ],
    [
      "artifact byte count mismatch",
      (job) => {
        job.result.total_artifact_bytes += 1;
      },
    ],
    [
      "out-of-range scene score",
      (job) => {
        job.result.candidates[0].scene_score = 1.01;
      },
    ],
    [
      "missing observations",
      (job) => {
        job.result.candidates[0].observations = [];
      },
    ],
    [
      "unsafe evidence relative path",
      (job) => {
        job.result.candidates[0].representative_image.relative_path =
          "../../secret.jpg";
      },
    ],
    [
      "unexpected evidence media type",
      (job) => {
        job.result.candidates[0].preview_clip.media_type =
          "text/html";
      },
    ],
    [
      "out-of-range model confidence",
      (job) => {
        job.result.candidates[0].ai_inference.predictions[0].confidence = 1.01;
      },
    ],
    [
      "unknown score calibration",
      (job) => {
        job.result.candidates[0].ai_inference.predictions[0].calibration =
          "probability";
      },
    ],
    [
      "model box outside image",
      (job) => {
        job.result.candidates[0].ai_inference.predictions[0].box = {
          x: 0.9,
          y: 0.2,
          width: 0.2,
          height: 0.4,
        };
      },
    ],
    [
      "fallback containing a prediction",
      (job) => {
        job.result.candidates[0].ai_inference.mode = "scene_fallback";
        job.result.candidates[0].ai_inference.status = "failed";
        job.result.candidates[0].ai_inference.warning_code =
          "model_inference_failed";
      },
    ],
  ];

  for (const [label, mutate] of corruptions) {
    const job = clone(completedJob());
    mutate(job);
    assert.throws(
      () => analysisJobs.parseAnalysisJob(job),
      Error,
      label,
    );
  }
});

test("only a completed parsed job can be restored", () => {
  const queued = completedJob();
  queued.status = "queued";
  queued.stage = "queued";
  queued.progress_percent = 0;
  queued.result = null;
  const parsed = analysisJobs.parseAnalysisJob(queued);

  assert.throws(
    () => analysisJobs.toRestoredRuntime(parsed),
    /완료된 분석 작업만 복구/,
  );
  assert.throws(
    () => analysisJobs.parseAnalysisJobList({ jobs: new Array(129).fill(queued) }),
    /분석 기록 목록/,
  );
});

test("analysis presentation labels and steps describe only real bridge states", () => {
  assert.deepEqual(
    [
      "queued",
      "running",
      "cancelling",
      "completed",
      "cancelled",
      "failed",
    ].map(analysisPresentation.savedJobStatusLabel),
    [
      "대기열",
      "후보 추출 중",
      "취소 요청됨",
      "후보 추출 완료",
      "후보 추출 취소됨",
      "후보 추출 실패",
    ],
  );
  assert.equal(
    analysisPresentation.runtimeStatusLabel("complete"),
    "후보 추출 완료",
  );
  assert.equal(
    analysisPresentation.runtimeStatusLabel("failed"),
    "후보 추출 실패",
  );
  assert.equal(
    analysisPresentation.runtimeStatusLabel("cancelled"),
    "후보 추출 취소됨",
  );
  assert.deepEqual(
    analysisPresentation.candidateExtractionSteps.map((step) => step.stage),
    ["queued", "validating_source", "extracting_candidates", "completed"],
  );
  assert.deepEqual(
    analysisPresentation.candidateExtractionSteps.map((_step, index) =>
      analysisPresentation.extractionStepState(
        index,
        "extracting_candidates",
        "running",
      ),
    ),
    ["done", "done", "active", "waiting"],
  );
  assert.deepEqual(
    analysisPresentation.candidateExtractionSteps.map((_step, index) =>
      analysisPresentation.extractionStepState(
        index,
        "completed",
        "completed",
      ),
    ),
    ["done", "done", "done", "done"],
  );
  assert.equal(analysisPresentation.formatArtifactBytes(3_600), "3.6 KB");
});
