import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const RUN_ID = "33333333-3333-4333-8333-333333333333";
const RECIPE_ID = "a".repeat(64);

const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const module = await vite.ssrLoadModule("/src/autoLabeling.ts");
const issueTypesModule = await vite.ssrLoadModule("/src/issueTypes.ts");

after(async () => {
  await vite.close();
});

function compileResponse(overrides = {}) {
  return {
    recipe_id: RECIPE_ID,
    project_id: PROJECT_ID,
    command: "표지판을 가린 수풀을 찾아줘",
    issue_type: "sign_vegetation_occlusion",
    display_name_ko: "표지판 식생 가림",
    positive_prompts: ["표지판을 덮은 나뭇가지", "식생으로 글자가 가려진 표지판"],
    negative_prompts: ["표지판 뒤쪽의 나무", "정상적으로 보이는 표지판"],
    capabilities: {
      object_detection: true,
      segmentation_required: true,
      ocr_required: false,
      relationship_reasoning_required: true,
      maintenance_judgment_required: true,
    },
    default_mode: "proposal_only",
    warnings: ["AI 제안은 사용자 검토 전 라벨이 아닙니다."],
    ...overrides,
  };
}

function runResponse(overrides = {}) {
  return {
    run_id: RUN_ID,
    recipe_id: RECIPE_ID,
    project_id: PROJECT_ID,
    scope: "current_job",
    mode_requested: "proposal_only",
    mode_applied: "proposal_only",
    status: "completed",
    proposal_origin: "local_model_generated",
    proposal_generation_status: "generated",
    jobs_scanned: 1,
    candidates_evaluated: 8,
    proposed_count: 3,
    auto_accepted_count: 0,
    review_required_count: 3,
    training_labels_created: 0,
    first_match: {
      analysis_job_id: JOB_ID,
      candidate_id: "0123456789abcdefabcd",
      issue_type: "pothole",
      review_status: "pending",
      decision_hash: "b".repeat(64),
    },
    message_ko: "AI 제안 3건을 검토 큐에 추가했습니다.",
    ...overrides,
  };
}

test("compile 응답을 엄격히 파싱해 renderer 계약으로 변환한다", () => {
  const parsed = module.parseAutoLabelingCompileResponse(compileResponse(), {
    projectId: PROJECT_ID,
    command: "표지판을 가린 수풀을 찾아줘",
    issueType: "sign_vegetation_occlusion",
  });

  assert.equal(parsed.recipe.recipeId, RECIPE_ID);
  assert.equal(parsed.recipe.displayNameKo, "표지판 식생 가림");
  assert.equal(parsed.capabilities.segmentationRequired, true);
  assert.equal(parsed.capabilities.ocrRequired, false);
  assert.equal(parsed.defaultMode, "proposal_only");
});

test("compile 응답의 미등록 필드와 잘못된 recipe/프롬프트를 거부한다", () => {
  assert.throws(() =>
    module.parseAutoLabelingCompileResponse({
      ...compileResponse(),
      unexpected: true,
    }),
  );
  assert.throws(() =>
    module.parseAutoLabelingCompileResponse(
      compileResponse({ recipe_id: "A".repeat(64) }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingCompileResponse(
      compileResponse({ negative_prompts: ["하나뿐인 음성 프롬프트"] }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingCompileResponse(
      compileResponse({
        capabilities: {
          ...compileResponse().capabilities,
          unexpected: true,
        },
      }),
    ),
  );
});

test("compile 응답이 요청 프로젝트·명령·명시 유형과 다르면 거부한다", () => {
  assert.throws(() =>
    module.parseAutoLabelingCompileResponse(compileResponse(), {
      projectId: JOB_ID,
    }),
  );
  assert.throws(() =>
    module.parseAutoLabelingCompileResponse(compileResponse(), {
      command: "다른 명령",
    }),
  );
  assert.throws(() =>
    module.parseAutoLabelingCompileResponse(compileResponse(), {
      issueType: "pothole",
    }),
  );
});

test("자동 판독 compile 요청은 issueType을 생략하고 명령 공백을 정리한다", () => {
  assert.deepEqual(
    module.buildAutoLabelingCompileRequest({
      projectId: PROJECT_ID,
      command: "  표지판을   가린 수풀을 찾아줘  ",
      issueType: "auto",
    }),
    {
      projectId: PROJECT_ID,
      command: "표지판을 가린 수풀을 찾아줘",
    },
  );

  assert.equal(
    module.buildAutoLabelingCompileRequest({
      projectId: PROJECT_ID,
      command: "포트홀을 찾아줘",
      issueType: "pothole",
    }).issueType,
    "pothole",
  );
});

test("run 요청은 확인 플래그와 명령·유형을 포함하고 범위를 정확히 매핑한다", () => {
  assert.deepEqual(
    module.buildAutoLabelingRunRequest({
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      recipeId: RECIPE_ID,
      command: " 낙하물을  찾아줘 ",
      issueType: "road_debris",
      scope: "current_job",
      mode: "proposal_only",
    }),
    {
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      recipeId: RECIPE_ID,
      command: "낙하물을 찾아줘",
      issueType: "road_debris",
      scope: "current_job",
      mode: "proposal_only",
      userConfirmed: true,
    },
  );

  assert.equal(
    "jobId" in
      module.buildAutoLabelingRunRequest({
        projectId: PROJECT_ID,
        jobId: JOB_ID,
        recipeId: RECIPE_ID,
        command: "차선 마모를 찾아줘",
        issueType: "lane_marking_faded",
        scope: "project",
        mode: "assisted_auto",
      }),
    false,
  );
});

test("현재 영상 run은 jobId가 필수이고 허용되지 않은 유형을 거부한다", () => {
  assert.throws(() =>
    module.buildAutoLabelingRunRequest({
      projectId: PROJECT_ID,
      recipeId: RECIPE_ID,
      command: "포트홀을 찾아줘",
      issueType: "pothole",
      scope: "current_job",
      mode: "proposal_only",
    }),
  );
  assert.throws(() =>
    module.buildAutoLabelingRunRequest({
      projectId: PROJECT_ID,
      recipeId: RECIPE_ID,
      command: "알 수 없는 것을 찾아줘",
      issueType: "unknown",
      scope: "project",
      mode: "proposal_only",
    }),
  );
  assert.throws(() =>
    module.buildAutoLabelingRunRequest({
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      recipeId: RECIPE_ID,
      command: "낙하물을 찾아줘",
      issueType: "road_debris",
      scope: "current_job",
      mode: "assisted_auto",
    }),
  );
});

test("run 응답을 파싱하고 assisted_auto의 검토 전용 하향 적용을 보존한다", () => {
  const parsed = module.parseAutoLabelingRunResponse(
    runResponse({
      mode_requested: "assisted_auto",
      mode_applied: "proposal_only",
    }),
    {
      projectId: PROJECT_ID,
      recipeId: RECIPE_ID,
      scope: "current_job",
      modeRequested: "assisted_auto",
    },
  );

  assert.equal(parsed.modeRequested, "assisted_auto");
  assert.equal(parsed.modeApplied, "proposal_only");
  assert.equal(parsed.proposalOrigin, "local_model_generated");
  assert.equal(parsed.proposalGenerationStatus, "generated");
  assert.equal(parsed.trainingLabelsCreated, 0);
});

test("run 응답의 모순된 집계, 출처, 즉시 학습 라벨, 미등록 필드를 거부한다", () => {
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({ auto_accepted_count: 1 }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({ review_required_count: 2 }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({ mode_applied: "assisted_auto" }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({ training_labels_created: 1 }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse({
      ...runResponse(),
      unexpected: true,
    }),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({
        status: "no_candidates",
        proposed_count: 1,
      }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({
        status: "completed",
        proposed_count: 0,
        review_required_count: 0,
        first_match: null,
      }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({
        status: "model_unavailable",
        candidates_evaluated: 1,
        proposed_count: 0,
        review_required_count: 0,
        first_match: null,
      }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({ jobs_scanned: 101 }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({ first_match: null }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({
        proposal_origin: "cached_existing",
        proposal_generation_status: "generated",
      }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(
      runResponse({
        status: "model_unavailable",
        proposal_origin: "local_model_generated",
        proposal_generation_status: "generated",
        candidates_evaluated: 0,
        proposed_count: 0,
        review_required_count: 0,
        first_match: null,
      }),
    ),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(runResponse(), {
      scope: "project",
    }),
  );
  assert.throws(() =>
    module.parseAutoLabelingRunResponse(runResponse(), {
      modeRequested: "assisted_auto",
    }),
  );
});

test("run 응답은 저장 제안 재사용과 모델 미사용 상태를 분리한다", () => {
  const cached = module.parseAutoLabelingRunResponse(
    runResponse({
      proposal_origin: "cached_existing",
      proposal_generation_status: "cached",
    }),
  );
  assert.equal(cached.proposalOrigin, "cached_existing");
  assert.equal(cached.proposalGenerationStatus, "cached");

  const unavailable = module.parseAutoLabelingRunResponse(
    runResponse({
      status: "model_unavailable",
      proposal_origin: "none",
      proposal_generation_status: "unavailable",
      candidates_evaluated: 0,
      proposed_count: 0,
      review_required_count: 0,
      first_match: null,
    }),
  );
  assert.equal(unavailable.proposalOrigin, "none");
  assert.equal(unavailable.proposalGenerationStatus, "unavailable");
});

test("낙하물·이정표·터널·OCR 유형은 항상 검토 대상으로 판정한다", () => {
  assert.equal(module.requiresMandatoryReview("road_debris"), true);
  assert.equal(module.requiresMandatoryReview("guide_sign_identity"), true);
  assert.equal(module.requiresMandatoryReview("tunnel_identity"), true);
  assert.equal(
    module.requiresMandatoryReview("other", {
      objectDetection: true,
      segmentationRequired: false,
      ocrRequired: true,
      relationshipReasoningRequired: false,
      maintenanceJudgmentRequired: false,
    }),
    true,
  );
  assert.equal(module.requiresMandatoryReview("pothole"), false);
});

test("12개 표준 결함 유형은 한 계약으로 노출되고 이전 코드는 정규화한다", () => {
  assert.equal(issueTypesModule.CANONICAL_ISSUE_TYPES.length, 12);
  assert.deepEqual(
    issueTypesModule.CANONICAL_ISSUE_TYPES.map((row) => row.incidentType),
    [
      "POTHOLE",
      "ROAD_SURFACE_DAMAGE",
      "ROAD_DEBRIS",
      "MEDIAN_BARRIER_DAMAGE",
      "SIGN_DAMAGE",
      "SIGN_VEGETATION_OCCLUSION",
      "ROADSIDE_MOWING_REQUIRED",
      "GUIDE_SIGN_IDENTITY",
      "TUNNEL_IDENTITY",
      "TUNNEL_NAME_OCCLUSION",
      "LANE_MARKING_FADED",
      "COLORED_GUIDANCE_FADED",
    ],
  );
  assert.equal(
    issueTypesModule.normalizeIssueTypeApi("sign_identity"),
    "guide_sign_identity",
  );
  assert.equal(
    issueTypesModule.normalizeIssueTypeApi("color_guidance_line_faded"),
    "colored_guidance_faded",
  );
  assert.equal(
    issueTypesModule.apiIssueTypeFromIncident("TUNNEL_NAME_OCCLUSION"),
    "tunnel_name_occlusion",
  );
  assert.equal(
    issueTypesModule.incidentTypeFromApi("colored_guidance_line_faded"),
    "COLORED_GUIDANCE_FADED",
  );
  assert.equal(issueTypesModule.normalizeIssueTypeApi("unknown"), null);
});

test("라벨링 화면은 짧은 데스크톱에서 인라인 계획, 단일 미디어 스크롤, 검토 전용 문구를 유지한다", async () => {
  const [styles, compactStyles, screen] = await Promise.all([
    readFile(new URL("./styles.css", import.meta.url), "utf8"),
    readFile(
      new URL("./styles-compact-analysis-labeling.css", import.meta.url),
      "utf8",
    ),
    readFile(new URL("./screens/LabelingScreen.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(
    styles,
    /\.auto-label-command__body\s*\{[^}]*position:\s*absolute;[^}]*overflow:\s*auto;/s,
  );
  assert.match(styles, /\.auto-label-command__guardrail\s*\{[^}]*color:\s*var\(--text\)/s);
  assert.match(
    compactStyles,
    /\.screen--labeling \.auto-label-command__body\s*\{[^}]*position:\s*static;[^}]*overflow:\s*visible/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.candidate-media-stack\s*\{[^}]*overflow-y:\s*auto/s,
  );
  assert.match(screen, /aria-expanded=\{autoPanelOpen\}/);
  assert.match(screen, /className="auto-label-command__outcome"/);
  assert.match(screen, /className="candidate-media-stack"/);
  assert.match(screen, /AI 제안은 사용자 확정 라벨이 아니며 학습에 즉시 포함되지 않습니다\./);
  assert.match(screen, /service\.compile/);
  assert.match(screen, /service\.run/);
  assert.match(screen, /className="auto-label-command__progress"/);
  assert.match(screen, /검지 후보를 준비하고 있습니다\./);
  assert.match(screen, /proposalGenerationStatus !== "unavailable"/);
  assert.match(screen, /setProposalRefreshRevision/);
  assert.match(screen, /이번 실행의 로컬 AI 검지/);
  assert.match(screen, /무결성 검증된 기존 제안/);
  assert.match(screen, /자동 생성\s*라벨 \{autoRunResult\.trainingLabelsCreated\}건/);
});
