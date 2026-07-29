import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PROJECT_A = "11111111-1111-4111-8111-111111111111";
const PROJECT_B = "22222222-2222-4222-8222-222222222222";
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const registry = await vite.ssrLoadModule("/src/modelRegistry.ts");

after(async () => {
  await vite.close();
});

function apiStats(overrides = {}) {
  return {
    project_id: PROJECT_A,
    question_count: 3,
    answer_revision_count: 4,
    current_answer_count: 3,
    training_included_count: 2,
    source_analysis_job_count: 3,
    partitions: {
      approved: 1,
      negative: 1,
      held: 1,
      excluded: 0,
    },
    ...overrides,
  };
}

function membership(index, partition, split) {
  const digit = String(index);
  const includeInTraining =
    partition === "approved" || partition === "negative";
  return {
    annotation_id: `10000000-0000-4000-8000-00000000000${digit}`,
    question_id: `20000000-0000-5000-8000-00000000000${digit}`,
    analysis_job_id: `30000000-0000-4000-8000-00000000000${digit}`,
    candidate_id: digit.repeat(20),
    answer_record_id: `10000000-0000-4000-8000-00000000000${digit}`,
    answer_revision: 1,
    partition,
    include_in_training: includeInTraining,
    split,
    source_record_hash: digit.repeat(64),
    annotation_hash: String(index + 3).repeat(64),
  };
}

function apiVersion(overrides = {}) {
  return {
    dataset_version_id: "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa",
    project_id: PROJECT_A,
    schema_version: "trafficvisson.dataset-version.v1",
    dataset_hash: "d".repeat(64),
    split_strategy: "analysis_job_hash_v1",
    validation_fraction: 0.2,
    source_answer_count: 3,
    training_member_count: 2,
    train_count: 1,
    validation_count: 1,
    excluded_count: 1,
    manifest: {
      schema_version: "trafficvisson.dataset-version.v1",
      split_strategy: "analysis_job_hash_v1",
    },
    created_at: "2026-07-28T06:00:00.000Z",
    reused: false,
    memberships: [
      membership(1, "approved", "train"),
      membership(2, "negative", "validation"),
      membership(3, "held", "excluded"),
    ],
    ...overrides,
  };
}

function sourceCandidateCatalog() {
  const safeCandidate = {
    source_type: "model_pack",
    license_spdx_id: "Apache-2.0",
    automatic_download: false,
    automatic_activation: false,
    weight_execution_enabled: false,
    project_training_linked: false,
    human_review_required: true,
    rights_review_required: true,
  };
  return {
    schema_version: "trafficvisson.source-candidate-catalog.v1",
    catalog_mode: "read_only_inactive",
    network_access_performed: false,
    project_training_linked: false,
    automatic_labels_created: false,
    candidates: [
      {
        ...safeCandidate,
        source_id: "idea-research-grounding-dino-tiny",
        display_name: "Grounding DINO Tiny",
        publisher: "IDEA Research",
        origin_url:
          "https://huggingface.co/IDEA-Research/grounding-dino-tiny",
        intended_use: "candidate_proposals",
        capability: "natural_language_box_proposals",
        catalog_status: "evaluation_complete",
        installation_status: "installed_validation_only",
        observation_basis: "verified_local_snapshot",
        evaluation: {
          evaluated_frame_count: 30,
          proposal_frame_count: 25,
          proposal_count: 47,
          false_positive_count: null,
          verdict: "review_only",
        },
        decision_reason: "30프레임 평가 후 사용자 검토 전용으로 제한했습니다.",
      },
      {
        ...safeCandidate,
        source_id: "laion-clip-b32-road-baseline",
        display_name: "LAION CLIP ViT-B/32 Road Baseline",
        publisher: "LAION / local evaluation baseline",
        origin_url:
          "https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
        license_spdx_id: "MIT",
        intended_use: "candidate_proposals",
        capability: "semantic_candidate_scoring",
        catalog_status: "evaluation_complete",
        installation_status: "installed_validation_only",
        observation_basis: "verified_local_snapshot",
        evaluation: {
          evaluated_frame_count: 349,
          proposal_frame_count: 29,
          proposal_count: 29,
          false_positive_count: null,
          verdict: "review_only",
        },
        decision_reason: "상대 점수는 확률이 아니며 사용자 확인이 필요합니다.",
      },
      {
        ...safeCandidate,
        source_id: "meta-sam2.1-hiera-small",
        display_name: "SAM2.1 Hiera Small",
        publisher: "Meta",
        origin_url:
          "https://huggingface.co/facebook/sam2.1-hiera-small",
        intended_use: "mask_refinement",
        capability: "prompted_mask_refinement",
        catalog_status: "review_candidate",
        installation_status: "not_installed",
        observation_basis: "catalog_policy_only",
        evaluation: null,
        decision_reason: "마스크 보정 후보이며 자동 라벨을 만들지 않습니다.",
      },
      {
        ...safeCandidate,
        source_id: "peterhdd-pothole-yolov8-local-cache",
        display_name: "Pothole Detection YOLOv8",
        publisher: "peterhdd / local evaluation cache",
        origin_url:
          "https://huggingface.co/peterhdd/pothole-detection-yolov8",
        intended_use: "candidate_proposals",
        capability: "pothole_box_proposals",
        catalog_status: "rejected",
        installation_status: "local_cache_quarantined",
        observation_basis: "verified_local_snapshot",
        evaluation: {
          evaluated_frame_count: 349,
          proposal_frame_count: 19,
          proposal_count: 19,
          false_positive_count: 19,
          verdict: "rejected",
        },
        decision_reason:
          "19개 제안이 모두 오탐이며 원 데이터셋 OpenRAIL 검토가 필요합니다.",
      },
      {
        ...safeCandidate,
        source_id: "programmer-rd-ai-road-issues-detection-dataset",
        display_name: "Road Issues Detection Dataset",
        source_type: "labeled_dataset",
        publisher: "Programmer-RD-AI",
        origin_url:
          "https://huggingface.co/datasets/Programmer-RD-AI/road-issues-detection-dataset",
        license_spdx_id: "CC0-1.0",
        intended_use: "retrieval_reference",
        capability: "labeled_training_reference",
        catalog_status: "review_candidate",
        installation_status: "not_installed",
        observation_basis: "catalog_policy_only",
        evaluation: null,
        decision_reason:
          "9,660장 후보이며 품질·중복·한국 도로·분할 검증이 필요합니다.",
      },
      {
        ...safeCandidate,
        source_id: "sekilab-rdd2022",
        display_name: "RDD2022 공식 도로 파손 데이터셋",
        source_type: "labeled_dataset",
        publisher: "Sekimoto Lab / CRDDC 2022",
        origin_url: "https://github.com/sekilab/RoadDamageDetector",
        license_spdx_id: "CC-BY-SA-4.0",
        intended_use: "evaluation_reference",
        capability: "labeled_training_reference",
        catalog_status: "review_candidate",
        installation_status: "not_installed",
        observation_basis: "catalog_policy_only",
        evaluation: null,
        decision_reason:
          "47,420장·55,000건 이상 Pascal VOC D00/D10/D20/D40 평가 참고 후보입니다.",
      },
      {
        ...safeCandidate,
        source_id: "daimler-lost-and-found-road-obstacles",
        display_name: "LostAndFound Road Obstacles",
        source_type: "labeled_dataset",
        publisher: "Daimler AG",
        origin_url:
          "https://www.tensorflow.org/datasets/catalog/lost_and_found",
        license_spdx_id: "LicenseRef-NeedsReview",
        intended_use: "evaluation_reference",
        capability: "labeled_training_reference",
        catalog_status: "review_candidate",
        installation_status: "not_installed",
        observation_basis: "catalog_policy_only",
        evaluation: null,
        decision_reason:
          "112개 스테레오 시퀀스·2,104장 후보이며 권리 검토 전까지 training blocked입니다.",
      },
    ],
    count: 7,
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

test("project label stats are strict, project-bound, and internally consistent", () => {
  assert.deepEqual(
    registry.parseProjectLabelStats(apiStats(), PROJECT_A),
    {
      projectId: PROJECT_A,
      questionCount: 3,
      answerRevisionCount: 4,
      currentAnswerCount: 3,
      trainingIncludedCount: 2,
      sourceAnalysisJobCount: 3,
      partitions: {
        approved: 1,
        negative: 1,
        held: 1,
        excluded: 0,
      },
    },
  );
  assert.throws(
    () =>
      registry.parseProjectLabelStats(
        { ...apiStats(), unexpected: true },
        PROJECT_A,
      ),
    /검증하지 못했습니다/,
  );
  assert.throws(
    () =>
      registry.parseProjectLabelStats(
        apiStats({ training_included_count: 1 }),
        PROJECT_A,
      ),
    /집계가 서로 일치/,
  );
  assert.throws(
    () => registry.parseProjectLabelStats(apiStats(), PROJECT_B),
    /다른 프로젝트/,
  );
});

test("dataset versions validate every member and aggregate", () => {
  const parsed = registry.parseDatasetVersion(apiVersion(), PROJECT_A);
  assert.equal(parsed.projectId, PROJECT_A);
  assert.equal(parsed.trainingMemberCount, 2);
  assert.equal(parsed.validationCount, 1);
  assert.equal(parsed.excludedCount, 1);
  assert.equal(parsed.memberships[2].includeInTraining, false);

  const leaked = clone(apiVersion());
  leaked.memberships[2].split = "train";
  assert.throws(
    () => registry.parseDatasetVersion(leaked, PROJECT_A),
    /라벨 출처와 분할이 일치/,
  );

  const badCount = apiVersion({ validation_count: 0 });
  assert.throws(
    () => registry.parseDatasetVersion(badCount, PROJECT_A),
    /집계가 구성원과 일치/,
  );

  const duplicate = clone(apiVersion());
  duplicate.memberships[1].annotation_id =
    duplicate.memberships[0].annotation_id;
  duplicate.memberships[1].answer_record_id =
    duplicate.memberships[0].answer_record_id;
  assert.throws(
    () => registry.parseDatasetVersion(duplicate, PROJECT_A),
    /중복 라벨/,
  );
});

test("dataset version lists reject count drift and cross-project data", () => {
  const parsed = registry.parseDatasetVersionList(
    {
      project_id: PROJECT_A,
      versions: [apiVersion()],
      count: 1,
    },
    PROJECT_A,
  );
  assert.equal(parsed.count, 1);
  assert.equal(parsed.versions[0].datasetHash, "d".repeat(64));

  assert.throws(
    () =>
      registry.parseDatasetVersionList(
        {
          project_id: PROJECT_A,
          versions: [apiVersion()],
          count: 2,
        },
        PROJECT_A,
      ),
    /목록 응답을 검증하지 못했습니다/,
  );
  assert.throws(
    () =>
      registry.parseDatasetVersionList(
        {
          project_id: PROJECT_A,
          versions: [apiVersion({ project_id: PROJECT_B })],
          count: 1,
        },
        PROJECT_A,
      ),
    /다른 프로젝트/,
  );
});

test("dataset creation request always carries explicit user confirmation", () => {
  assert.deepEqual(registry.buildProjectScopeRequest(PROJECT_A), {
    projectId: PROJECT_A,
  });
  assert.deepEqual(registry.buildDatasetCreateRequest(PROJECT_A), {
    projectId: PROJECT_A,
    userConfirmed: true,
  });
  assert.throws(
    () => registry.buildDatasetCreateRequest("not-a-project"),
    /프로젝트 식별자/,
  );
});

test("source candidate catalogue is strict and preserves inactive safety gates", () => {
  const parsed = registry.parseSourceCandidateCatalog(
    sourceCandidateCatalog(),
  );
  assert.equal(parsed.count, 7);
  assert.equal(
    parsed.candidates[0].capability,
    "natural_language_box_proposals",
  );
  assert.equal(
    parsed.candidates[0].installationStatus,
    "installed_validation_only",
  );
  assert.equal(parsed.candidates[0].evaluation.evaluatedFrameCount, 30);
  assert.equal(parsed.candidates[0].evaluation.proposalFrameCount, 25);
  assert.equal(parsed.candidates[0].evaluation.proposalCount, 47);
  assert.equal(parsed.candidates[1].capability, "semantic_candidate_scoring");
  assert.equal(parsed.candidates[2].installationStatus, "not_installed");
  assert.equal(parsed.candidates[3].catalogStatus, "rejected");
  assert.equal(parsed.candidates[3].evaluation.falsePositiveCount, 19);
  assert.equal(parsed.candidates[4].sourceType, "labeled_dataset");
  assert.equal(parsed.candidates[4].licenseSpdxId, "CC0-1.0");
  assert.equal(parsed.candidates[5].sourceId, "sekilab-rdd2022");
  assert.equal(parsed.candidates[5].intendedUse, "evaluation_reference");
  assert.equal(parsed.candidates[5].licenseSpdxId, "CC-BY-SA-4.0");
  assert.equal(
    parsed.candidates[6].sourceId,
    "daimler-lost-and-found-road-obstacles",
  );
  assert.equal(parsed.candidates[6].licenseSpdxId, "LicenseRef-NeedsReview");
  assert.match(parsed.candidates[6].decisionReason, /training blocked/);
  assert.equal(parsed.networkAccessPerformed, false);
  assert.equal(parsed.projectTrainingLinked, false);
  assert.equal(parsed.automaticLabelsCreated, false);

  const unknownField = clone(sourceCandidateCatalog());
  unknownField.candidates[0].download_url = "https://attacker.example/model";
  assert.throws(
    () => registry.parseSourceCandidateCatalog(unknownField),
    /후보 응답을 검증하지 못했습니다/,
  );

  const unsafe = clone(sourceCandidateCatalog());
  unsafe.candidates[0].automatic_activation = true;
  assert.throws(
    () => registry.parseSourceCandidateCatalog(unsafe),
    /후보 응답을 검증하지 못했습니다/,
  );

  const countDrift = clone(sourceCandidateCatalog());
  countDrift.count = 6;
  assert.throws(
    () => registry.parseSourceCandidateCatalog(countDrift),
    /카탈로그 응답을 검증하지 못했습니다/,
  );

  const duplicate = clone(sourceCandidateCatalog());
  duplicate.candidates[1].source_id = duplicate.candidates[0].source_id;
  assert.throws(
    () => registry.parseSourceCandidateCatalog(duplicate),
    /중복 후보/,
  );

  const statusDrift = clone(sourceCandidateCatalog());
  statusDrift.candidates[0].installation_status =
    "local_cache_quarantined";
  assert.throws(
    () => registry.parseSourceCandidateCatalog(statusDrift),
    /검토 상태가 서로 일치하지 않습니다/,
  );

  const impossibleEvaluation = clone(sourceCandidateCatalog());
  impossibleEvaluation.candidates[3].evaluation.false_positive_count = 20;
  assert.throws(
    () => registry.parseSourceCandidateCatalog(impossibleEvaluation),
    /평가 응답을 검증하지 못했습니다/,
  );
});

test("model screen uses bridge registry data and keeps training disabled", async () => {
  const screenSource = await readFile(
    new URL("./screens/ModelScreen.tsx", import.meta.url),
    "utf8",
  );
  const typeSource = await readFile(
    new URL("./vite-env.d.ts", import.meta.url),
    "utf8",
  );
  assert.match(screenSource, /api\.labels\.stats\(request\)/);
  assert.match(screenSource, /api\.datasets\.list\(request\)/);
  assert.match(screenSource, /api\.datasets\.create\(/);
  assert.match(screenSource, /api\.sources\.candidates\(\)/);
  assert.match(screenSource, /parseSourceCandidateCatalog/);
  assert.match(screenSource, /sourceCatalog\?\.candidates\.map/);
  assert.match(screenSource, /candidate\.licenseSpdxId/);
  assert.match(screenSource, /candidate\.decisionReason/);
  assert.match(screenSource, /현재 프로젝트의 활성 모델이 아닙니다/);
  assert.match(screenSource, /검증용 팩도 자동 활성화하지 않습니다/);
  assert.match(screenSource, /stats\.currentAnswerCount > 0/);
  assert.match(screenSource, /disabled=\{!canCreateSnapshot\}/);
  assert.match(screenSource, /variant="primary"\s+disabled/);
  assert.match(screenSource, /프로젝트 데이터 격리/);
  assert.doesNotMatch(screenSource, /useReviewHistory|reviewStorageKey/);
  assert.doesNotMatch(screenSource, /localStorage|projectId\.slice/);
  assert.match(typeSource, /labels:\s*\{\s*stats\(/);
  assert.match(typeSource, /datasets:\s*\{[\s\S]*create\(/);
  assert.match(typeSource, /sources:\s*\{\s*candidates\(\)/);
  assert.match(typeSource, /readonly userConfirmed: true/);
  assert.doesNotMatch(screenSource, /api\.sources\.(download|install|activate)/);
});
