import assert from "node:assert/strict";
import fs from "node:fs";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const DATASET_ID = "22222222-2222-4222-8222-222222222222";
const TRAINING_JOB_ID = "33333333-3333-4333-8333-333333333333";
const MODEL_ID = "44444444-4444-4444-8444-444444444444";
const TRAIN_ANALYSIS_ID = "55555555-5555-4555-8555-555555555555";
const VALIDATION_ANALYSIS_ID =
  "66666666-6666-4666-8666-666666666666";
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const modelScreenSource = fs.readFileSync(
  fileURLToPath(new URL("./screens/ModelScreen.tsx", import.meta.url)),
  "utf8",
);
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const projectModels = await vite.ssrLoadModule("/src/projectModels.ts");

after(async () => {
  await vite.close();
});

function trainingJob(overrides = {}) {
  return {
    schema_version: "trafficvisson.training-job.v1",
    training_job_id: TRAINING_JOB_ID,
    project_id: PROJECT_ID,
    idempotency_key: "project-model:train:01234567",
    request_sha256: "a".repeat(64),
    dataset_version_id: DATASET_ID,
    dataset_hash: "b".repeat(64),
    base_model_version: "baseline-v1",
    required_issue_types: ["pothole"],
    current_confirmed_label_count: 100,
    approved_reference_training_member_count: 5,
    approved_reference_training_event_count: 5,
    training_gate_member_count: 100,
    current_train_member_count: 70,
    current_validation_member_count: 30,
    fixed_validation_sha256: "c".repeat(64),
    fixed_validation_membership_sha256: "8".repeat(64),
    fixed_validation_set_version: 1,
    fixed_validation_set_schema_version:
      "trafficvisson.project-fixed-validation.v2",
    fixed_validation_parent_version: null,
    fixed_validation_extension_approval_sha256: null,
    validation_sample_counts: { pothole: 30 },
    auxiliary_reference_count: 5,
    approved_references_count_toward_training_gate: false,
    auxiliary_references_count_toward_validation: false,
    trainer_id: "visual-centroid",
    trainer_version: "1.0.0",
    trainer_available: true,
    trainer_test_only: false,
    status: "succeeded",
    attempt_count: 1,
    blockers: [],
    error: null,
    progress: {
      stage: "completed",
      attempt_number: 0,
      updated_at: "2026-07-29T00:01:00Z",
      epoch: null,
      epochs: null,
      current: null,
      total: null,
      loss: null,
    },
    checkpoint: null,
    cancel_requested: false,
    resume_count: 0,
    recovery_count: 0,
    model_version_id: MODEL_ID,
    automatic_training_started: false,
    user_confirmed_start: true,
    created_at: "2026-07-29T00:00:00Z",
    updated_at: "2026-07-29T00:01:00Z",
    idempotent_replay: false,
    ...overrides,
  };
}

function modelState(overrides = {}) {
  return {
    schema_version: "trafficvisson.project-model-state.v1",
    project_id: PROJECT_ID,
    revision: 2,
    active_model_id: null,
    active_model_version: "baseline-v1",
    candidate_model_id: MODEL_ID,
    candidate_model_version: "project-candidate-v1",
    previous_model_id: null,
    previous_model_version: null,
    automatic_promotion: false,
    updated_at: "2026-07-29T00:01:00Z",
    ...overrides,
  };
}

function modelVersion(overrides = {}) {
  return {
    schema_version: "trafficvisson.project-model-version.v1",
    model_version_id: MODEL_ID,
    project_id: PROJECT_ID,
    training_job_id: TRAINING_JOB_ID,
    model_version: "project-candidate-v1",
    model_format: "trafficvisson.visual-embedding-centroid-head.v1",
    base_model_version: "baseline-v1",
    dataset_version_id: DATASET_ID,
    dataset_hash: "b".repeat(64),
    fixed_validation_sha256: "c".repeat(64),
    fixed_validation_membership_sha256: "8".repeat(64),
    fixed_validation_set_version: 1,
    training_analysis_job_ids: [TRAIN_ANALYSIS_ID],
    fixed_validation_analysis_job_ids: [VALIDATION_ANALYSIS_ID],
    source_analysis_lineage_sha256: "9".repeat(64),
    primary_artifact_sha256: "d".repeat(64),
    artifact_manifest_sha256: "e".repeat(64),
    artifact_file_count: 2,
    artifact_total_size_bytes: 1024,
    trainer_id: "visual-centroid",
    trainer_version: "1.0.0",
    trainer_test_only: false,
    promotion_review: {
      schema_version: "trafficvisson.promotion-review-policy.v1",
      decision: "eligible",
      active_model_version: "baseline-v1",
      candidate_model_version: "project-candidate-v1",
      validation_set_sha256: "c".repeat(64),
      required_issue_types: ["pothole"],
      minimum_samples_per_type: 30,
      maximum_recall_drop: 0.02,
      active_report: {},
      candidate_report: {},
      reasons: [],
      auto_promotion_allowed: false,
      requires_user_review: true,
    },
    validation_decision: "eligible",
    auxiliary_reference_count: 5,
    auxiliary_reference_manifest_sha256: "f".repeat(64),
    state: "candidate",
    automatic_promotion: false,
    automatic_rejection: false,
    user_promotion_required: true,
    created_at: "2026-07-29T00:01:00Z",
    updated_at: "2026-07-29T00:01:00Z",
    ...overrides,
  };
}

function modelIterationLedger(overrides = {}) {
  const survey = {
    schema_version: "trafficvisson.model-survey-qa-summary.v1",
    survey_model_qa_sha256: "a".repeat(64),
    pipeline_job_id: "77777777-7777-4777-8777-777777777777",
    analysis_job_id: "88888888-8888-4888-8888-888888888888",
    model_version_id: "model-promoted-v1",
    dataset_version_id: DATASET_ID,
    qa_scope_sha256: "8".repeat(64),
    model_candidate_count: 1,
    user_discovered_candidate_count: 0,
    other_detector_candidate_count: 1,
    resolved_training_feedback_count: 2,
    true_positive_count: 1,
    false_positive_count: 0,
    false_negative_count: 0,
    held_count: 1,
    drift_assessed: false,
    baseline_survey_model_qa_sha256: null,
    promotion_gate_authority: false,
    starts_training: false,
    promotes_model: false,
  };
  const iteration = {
    schema_version: "trafficvisson.model-iteration-summary.v1",
    model_iteration_sha256: "d".repeat(64),
    parent_iteration_sha256: null,
    survey_model_qa_sha256: "a".repeat(64),
    parent_model_version_id: "model-promoted-v1",
    parent_dataset_version_id: DATASET_ID,
    next_dataset_version_id: "99999999-9999-4999-8999-999999999999",
    feedback_annotation_count: 1,
    audit_only_member_count: 1,
    fixed_validation_sha256: "3".repeat(64),
    next_training_requires_user_confirmation: true,
    automatic_training_start: false,
    automatic_model_promotion: false,
    iteration_creates_incident: false,
    iteration_creates_report_truth: false,
  };
  return {
    schema_version: "trafficvisson.model-iteration-ledger.v1",
    project_id: PROJECT_ID,
    survey_qa_count: 1,
    iteration_count: 1,
    survey_qa: [survey],
    iterations: [iteration],
    ...overrides,
  };
}

test("model iteration ledger adapter exposes QA counts without automatic authority", () => {
  const ledger = projectModels.parseProjectModelIterationLedger(
    modelIterationLedger(),
    PROJECT_ID,
  );
  assert.equal(ledger.surveyQa[0].truePositiveCount, 1);
  assert.equal(ledger.surveyQa[0].otherDetectorCandidateCount, 1);
  assert.equal(ledger.iterations[0].feedbackAnnotationCount, 1);
  assert.equal(
    ledger.iterations[0].nextTrainingRequiresUserConfirmation,
    true,
  );
  const forged = structuredClone(modelIterationLedger());
  forged.iterations[0].automatic_training_start = true;
  assert.throws(
    () =>
      projectModels.parseProjectModelIterationLedger(
        forged,
        PROJECT_ID,
      ),
    /권한 경계/,
  );
  const pathLeak = structuredClone(modelIterationLedger());
  pathLeak.survey_qa[0].debug_path = "C:\\secret\\qa.json";
  assert.throws(
    () =>
      projectModels.parseProjectModelIterationLedger(
        pathLeak,
        PROJECT_ID,
      ),
    /파일 경로/,
  );
});

test("training job parser keeps approved references outside the current-video gate and rejects hidden paths", () => {
  const parsed = projectModels.parseProjectModelTrainingJob(
    trainingJob(),
    PROJECT_ID,
  );
  assert.equal(parsed.status, "succeeded");
  assert.equal(parsed.auxiliaryReferenceCount, 5);
  assert.equal(parsed.approvedReferenceTrainingMemberCount, 5);
  assert.equal(parsed.approvedReferenceTrainingEventCount, 5);
  assert.equal(parsed.trainingGateMemberCount, 100);
  assert.equal(parsed.currentConfirmedLabelCount, 100);
  assert.equal(parsed.modelVersionId, MODEL_ID);
  assert.equal(parsed.fixedValidationSetVersion, 1);
  assert.equal(parsed.fixedValidationMembershipSha256, "8".repeat(64));

  const grouped = projectModels.parseProjectModelTrainingJob(
    trainingJob({
      approved_reference_training_event_count: 4,
      training_gate_member_count: 100,
    }),
    PROJECT_ID,
  );
  assert.equal(grouped.approvedReferenceTrainingMemberCount, 5);
  assert.equal(grouped.approvedReferenceTrainingEventCount, 4);
  assert.equal(grouped.trainingGateMemberCount, 100);

  assert.throws(
    () =>
      projectModels.parseProjectModelTrainingJob(
        trainingJob({ artifact_path: "C:\\secret\\model.bin" }),
        PROJECT_ID,
      ),
    /응답 형식/,
  );
  assert.throws(
    () =>
      projectModels.parseProjectModelTrainingJob(
        trainingJob({
          status: "blocked",
          model_version_id: null,
          trainer_available: true,
          blockers: [],
        }),
        PROJECT_ID,
      ),
    /차단 사유/,
  );
});

test("training parser preserves durable progress, checkpoint, cancel and resume state", () => {
  const checkpoint = {
    schema_version:
      "trafficvisson.project-model-training-checkpoint.v1",
    attempt_number: 1,
    relative_path: "attempt-1/staging/checkpoint.pt",
    sha256: "7".repeat(64),
    size_bytes: 4096,
    modified_time_ns: "1785380000000000000",
    request_manifest_sha256: "a".repeat(64),
    created_at: "2026-07-29T00:00:30Z",
  };
  const running = projectModels.parseProjectModelTrainingJob(
    trainingJob({
      status: "running",
      attempt_count: 1,
      model_version_id: null,
      progress: {
        stage: "fine_tune",
        attempt_number: 1,
        updated_at: "2026-07-29T00:00:30Z",
        epoch: 1,
        epochs: 2,
        current: 3,
        total: 10,
        loss: 0.25,
      },
      checkpoint,
      resume_count: 1,
      recovery_count: 1,
    }),
    PROJECT_ID,
  );
  assert.equal(running.progress.stage, "fine_tune");
  assert.equal(running.progress.current, 3);
  assert.equal(running.checkpoint.sha256, "7".repeat(64));
  assert.equal(running.resumeCount, 1);
  assert.equal(running.recoveryCount, 1);

  const cancelled = projectModels.parseProjectModelTrainingJob(
    trainingJob({
      status: "cancelled",
      attempt_count: 1,
      model_version_id: null,
      cancel_requested: true,
      error: {
        code: "training_cancelled",
        message: "training was cancelled by the user",
      },
      progress: {
        stage: "fine_tune",
        attempt_number: 1,
        updated_at: "2026-07-29T00:00:30Z",
        epoch: 1,
        epochs: 2,
        current: 3,
        total: 10,
        loss: 0.25,
      },
      checkpoint,
    }),
    PROJECT_ID,
  );
  assert.equal(cancelled.status, "cancelled");
  assert.equal(cancelled.cancelRequested, true);
  assert.throws(
    () =>
      projectModels.parseProjectModelTrainingJob(
        trainingJob({
          status: "interrupted",
          model_version_id: null,
          checkpoint: {
            ...checkpoint,
            relative_path: "../../secret.pt",
          },
        }),
        PROJECT_ID,
      ),
    /상대 경로/,
  );
});

test("training list distinguishes trainer unavailable from insufficient labels", () => {
  const trainerBlocked = trainingJob({
    status: "blocked",
    trainer_available: false,
    model_version_id: null,
    attempt_count: 0,
    blockers: [
      { code: "trainer_unavailable", trainer_id: "visual-centroid" },
    ],
  });
  const labelBlocked = trainingJob({
    training_job_id: "55555555-5555-4555-8555-555555555555",
    status: "blocked",
    model_version_id: null,
    attempt_count: 0,
    current_confirmed_label_count: 23,
    training_gate_member_count: 23,
    blockers: [
      {
        code: "insufficient_current_confirmed_labels",
        observed: 23,
        required: 100,
      },
    ],
  });
  const parsed = projectModels.parseProjectModelTrainingJobList(
    {
      schema_version: "trafficvisson.training-job-list.v1",
      project_id: PROJECT_ID,
      count: 2,
      jobs: [trainerBlocked, labelBlocked],
    },
    PROJECT_ID,
  );
  assert.equal(parsed[0].blockers[0].code, "trainer_unavailable");
  assert.equal(
    parsed[1].blockers[0].code,
    "insufficient_current_confirmed_labels",
  );
});

test("model versions and state preserve candidate review without paths", () => {
  const version = projectModels.parseProjectModelVersion(
    modelVersion(),
    PROJECT_ID,
  );
  const state = projectModels.parseProjectModelState(
    modelState(),
    PROJECT_ID,
  );
  assert.equal(version.state, "candidate");
  assert.equal(
    version.modelFormat,
    "trafficvisson.visual-embedding-centroid-head.v1",
  );
  assert.equal(version.promotionDecision, "eligible");
  assert.equal(version.fixedValidationSetVersion, 1);
  assert.deepEqual(version.trainingAnalysisJobIds, [TRAIN_ANALYSIS_ID]);
  assert.deepEqual(version.fixedValidationAnalysisJobIds, [
    VALIDATION_ANALYSIS_ID,
  ]);
  assert.equal(state.candidateModelId, MODEL_ID);

  assert.throws(
    () =>
      projectModels.parseProjectModelVersion(
        modelVersion({ automatic_promotion: true }),
        PROJECT_ID,
      ),
    /응답 형식/,
  );
  assert.throws(
    () =>
      projectModels.parseProjectModelVersion(
        modelVersion({
          fixed_validation_analysis_job_ids: [TRAIN_ANALYSIS_ID],
        }),
        PROJECT_ID,
      ),
    /겹칩니다/,
  );
  const rejected = projectModels.parseProjectModelVersion(
    modelVersion({
      validation_decision: "rejected",
      promotion_review: {
        ...modelVersion().promotion_review,
        decision: "rejected",
        reasons: [{ code: "recall_drop", message: "recall dropped" }],
      },
      state: "rejected",
      automatic_rejection: true,
      user_promotion_required: false,
    }),
    PROJECT_ID,
  );
  assert.equal(rejected.state, "rejected");
  assert.equal(rejected.automaticRejection, true);
  assert.throws(
    () =>
      projectModels.parseProjectModelState(
        modelState({ candidate_model_version: null }),
        PROJECT_ID,
      ),
    /식별자/,
  );
});

test("model version parser accepts detector and centroid roles but rejects unknown formats", () => {
  const detector = projectModels.parseProjectModelVersion(
    modelVersion({
      model_format: "trafficvisson.local-object-detector.v1",
      trainer_id: "offline-dfine-small",
      state: "active",
    }),
    PROJECT_ID,
  );
  assert.equal(
    detector.modelFormat,
    "trafficvisson.local-object-detector.v1",
  );
  assert.equal(detector.state, "active");
  assert.equal(detector.trainerId, "offline-dfine-small");

  const [listedDetector] = projectModels.parseProjectModelVersionList(
    {
      schema_version: "trafficvisson.model-version-list.v1",
      project_id: PROJECT_ID,
      count: 1,
      models: [
        modelVersion({
          model_format: "trafficvisson.local-object-detector.v1",
          trainer_id: "offline-dfine-small",
          state: "active",
        }),
      ],
    },
    PROJECT_ID,
  );
  assert.equal(
    listedDetector.modelFormat,
    "trafficvisson.local-object-detector.v1",
  );

  assert.throws(
    () =>
      projectModels.parseProjectModelVersion(
        modelVersion({
          model_format: "trafficvisson.future-unknown-model.v1",
        }),
        PROJECT_ID,
      ),
    /응답 형식/,
  );
});

test("renderer request builders expose only user confirmation and identifiers", () => {
  assert.deepEqual(
    projectModels.buildProjectModelStartRequest(
      PROJECT_ID,
      DATASET_ID,
      "project-model:train:01234567",
    ),
    {
      projectId: PROJECT_ID,
      datasetVersionId: DATASET_ID,
      idempotencyKey: "project-model:train:01234567",
      userConfirmed: true,
    },
  );
  assert.deepEqual(
    projectModels.buildProjectModelResumeRequest(
      PROJECT_ID,
      TRAINING_JOB_ID,
    ),
    {
      projectId: PROJECT_ID,
      trainingJobId: TRAINING_JOB_ID,
      userConfirmed: true,
    },
  );
  assert.deepEqual(
    projectModels.buildProjectModelCancelRequest(
      PROJECT_ID,
      TRAINING_JOB_ID,
    ),
    {
      projectId: PROJECT_ID,
      trainingJobId: TRAINING_JOB_ID,
      userConfirmed: true,
    },
  );
  assert.deepEqual(
    projectModels.buildProjectModelTransitionRequest(
      PROJECT_ID,
      MODEL_ID,
      2,
      "project-model:promote:01234567",
    ),
    {
      projectId: PROJECT_ID,
      modelVersionId: MODEL_ID,
      expectedRevision: 2,
      idempotencyKey: "project-model:promote:01234567",
      userConfirmed: true,
    },
  );
});

test("fixed-validation history keeps parent hashes and builds a stale-safe extension approval", () => {
  const parent = projectModels.parseProjectFixedValidationVersion(
    {
      schema_version: "trafficvisson.project-fixed-validation.v2",
      project_id: PROJECT_ID,
      validation_set_version: 1,
      parent_validation_set_version: null,
      parent_membership_sha256: null,
      parent_fixed_validation_sha256: null,
      origin_dataset_version_id: DATASET_ID,
      required_issue_types: ["pothole"],
      added_issue_types: ["pothole"],
      added_validation_analysis_job_ids: [],
      validation_sample_counts: { pothole: 30 },
      extension_positive_sample_counts: {},
      membership_sha256: "8".repeat(64),
      fixed_validation_sha256: "c".repeat(64),
      extension_approval_sha256: null,
      minimum_positive_samples_per_type: 30,
      persisted: true,
      created_at: "2026-07-29T00:00:00Z",
    },
    PROJECT_ID,
  );
  assert.equal(parent.validationSetVersion, 1);
  assert.equal(parent.validationSampleCounts.pothole, 30);

  const approval =
    projectModels.buildProjectFixedValidationExtensionApproval(
      parent,
      "sign_damage",
      [VALIDATION_ANALYSIS_ID],
    );
  assert.deepEqual(approval.addedIssueTypes, ["sign_damage"]);
  assert.equal(
    approval.expectedParentMembershipSha256,
    "8".repeat(64),
  );
  assert.deepEqual(
    projectModels.buildProjectModelStartRequest(
      PROJECT_ID,
      DATASET_ID,
      "project-model:extend:01234567",
      approval,
    ).fixedValidationExtensionApproval,
    approval,
  );
  assert.throws(
    () =>
      projectModels.buildProjectFixedValidationExtensionApproval(
        parent,
        "pothole",
        [VALIDATION_ANALYSIS_ID],
      ),
    /이미 현재/,
  );
});

test("model screen exposes immutable validation extension without counting approved references in the 100 gate", () => {
  assert.match(modelScreenSource, /TRAIN 현행 승인 Annotation 100건/);
  assert.match(modelScreenSource, /고정 검증 양성 · 유형별 30건/);
  assert.match(
    modelScreenSource,
    /buildProjectFixedValidationExtensionApproval/,
  );
  assert.match(modelScreenSource, /부모 truth는 수정·삭제하지 않습니다/);
  assert.match(
    modelScreenSource,
    /fixed_validation_extension_approval_stale/,
  );
  assert.match(
    modelScreenSource,
    /fixed_validation_extension_duplicate_evidence/,
  );
  assert.match(
    modelScreenSource,
    /fixed_validation_extension_insufficient/,
  );
  assert.doesNotMatch(
    modelScreenSource,
    /학습 구성원 100건 · 영상\+승인 사례/,
  );
  assert.match(modelScreenSource, /cancelProjectTraining/);
  assert.match(modelScreenSource, /checkpoint/);
  assert.match(modelScreenSource, /progress/);
});
