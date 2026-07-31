import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const PIPELINE_JOB_ID = "22222222-2222-4222-8222-222222222222";
const ANALYSIS_JOB_ID = "33333333-3333-4333-8333-333333333333";
const SOURCE_SHA = "a".repeat(64);
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const pipeline = await vite.ssrLoadModule("/src/pipelineJobs.ts");
const completion = await vite.ssrLoadModule(
  "/src/pipelineReviewCompletion.ts",
);
const labelingSource = await readFile(
  new URL("./screens/LabelingScreen.tsx", import.meta.url),
  "utf8",
);

after(async () => {
  await vite.close();
});

function waitingJob({ promoted = false, zero = false } = {}) {
  return {
    jobId: PIPELINE_JOB_ID,
    projectId: PROJECT_ID,
    snapshotId: "44444444-4444-4444-8444-444444444444",
    fileName: "road.mp4",
    idempotencyKey: "pipeline-start-0001",
    inputFingerprint: "b".repeat(64),
    profileSnapshot: {
      schemaVersion: "trafficvisson.pipeline-profile.v1",
      projectId: PROJECT_ID,
      questionPolicy: "learning_assist",
      reviewMode: "precision",
      defaultTemplateId: "sample-q3",
      defaultTemplateSha256: "c".repeat(64),
      detectorPackId: "detector",
      ocrPackId: "ocr",
      sam2PackId: "sam2",
      reportProvider: "codex",
      outputLocationId: "project-default",
      candidateRescanFps: 8,
      revision: 1,
      updatedAt: "2026-07-29T00:00:00+00:00",
    },
    capabilitySnapshot: {},
    status: "awaiting_review",
    currentStage: "question_queue",
    progressPercent: 91,
    outcomeCode: null,
    errorCode: null,
    retryCount: 0,
    resumeCount: 0,
    reviewCompletionId: null,
    stages: pipeline.PIPELINE_STAGES.map((name) => ({
      name,
      status:
        name === "question_queue"
          ? "success"
          : pipeline.PIPELINE_STAGES.indexOf(name) <
              pipeline.PIPELINE_STAGES.indexOf("question_queue")
            ? "success"
            : "pending",
      outcome: null,
      attempts: 1,
      startedAt: "2026-07-29T00:00:00+00:00",
      completedAt:
        name === "derived_outputs"
          ? null
          : "2026-07-29T00:00:01+00:00",
      errorCode: null,
      checkpoint:
        name === "question_queue"
          ? {
              ...(zero
                ? {
                    schema_version:
                      "trafficvisson.zero-candidate-promoted-survey-queue.v1",
                    question_candidate_ids: [],
                  }
                : {}),
              analysis_job_id: ANALYSIS_JOB_ID,
              source_sha256: SOURCE_SHA,
              question_count: zero ? 0 : 2,
              question_queue_revision: "questions-v1",
            }
          : name === "candidate_rescan_5_10fps" && promoted
            ? {
                dense_candidate_discovery: {
                  provider_states: [
                    {
                      provider: "promoted_project_model",
                      state: "ready",
                      independent_survey_verified: true,
                    },
                  ],
                },
              }
          : {},
      metrics: {},
    })),
    provenance: {},
    createdAt: "2026-07-29T00:00:00+00:00",
    updatedAt: "2026-07-29T00:00:01+00:00",
    recoveredAfterRestart: false,
    recordSha256: "d".repeat(64),
  };
}

function record(candidateId, index) {
  return {
    question: index,
    questionId: `question:${candidateId}`,
    candidateId,
    answer: "yes",
    selectedType: "POTHOLE",
    reviewedAt: "2026-07-29T00:00:02+00:00",
    trainingStatus: "approved",
    recordId: `${index + 5}5555555-5555-4555-8555-555555555555`,
    recordHash: String(index + 1).repeat(64),
    answerRevision: 1,
  };
}

test("question queue binding requires server analysis, source and count provenance", () => {
  const job = waitingJob();
  assert.deepEqual(pipeline.pipelineQuestionQueueBinding(job), {
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCount: 2,
    queueRevision: "questions-v1",
    surveyQaRequired: false,
    zeroCandidateSurvey: false,
  });
  delete job.stages[10].checkpoint.analysis_job_id;
  assert.equal(pipeline.pipelineQuestionQueueBinding(job), null);
});

test("bootstrap review completion omits promoted-survey QA choice", async () => {
  const records = [record("candidate-1", 0), record("candidate-2", 1)];
  const result = await completion.buildPipelineReviewCompletion({
    pipelineJob: waitingJob(),
    projectId: PROJECT_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCandidateIds: ["candidate-1", "candidate-2"],
    records,
    finalQaChoice: "no_miss_complete",
  });
  assert.equal(result.ok, true);
  assert.match(result.request.reviewCompletionId, /^review:[a-f0-9]{64}$/);
  assert.equal("finalQaChoice" in result.request, false);
  const replay = await completion.buildPipelineReviewCompletion({
    pipelineJob: waitingJob(),
    projectId: PROJECT_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCandidateIds: ["candidate-1", "candidate-2"],
    records: [...records].reverse(),
  });
  assert.equal(
    replay.request.reviewCompletionId,
    result.request.reviewCompletionId,
  );
});

test("promoted survey requires and sends the explicit final missed-scene QA choice", async () => {
  const job = waitingJob({ promoted: true });
  assert.deepEqual(pipeline.pipelineQuestionQueueBinding(job), {
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCount: 2,
    queueRevision: "questions-v1",
    surveyQaRequired: true,
    zeroCandidateSurvey: false,
  });
  const records = [record("candidate-1", 0), record("candidate-2", 1)];
  const missing = await completion.buildPipelineReviewCompletion({
    pipelineJob: job,
    projectId: PROJECT_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCandidateIds: ["candidate-1", "candidate-2"],
    records,
  });
  assert.equal(missing.ok, false);
  assert.equal(missing.reason, "final_qa_required");
  const complete = await completion.buildPipelineReviewCompletion({
    pipelineJob: job,
    projectId: PROJECT_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCandidateIds: ["candidate-1", "candidate-2"],
    records,
    finalQaChoice: "no_miss_complete",
  });
  assert.equal(complete.ok, true);
  assert.equal(complete.request.finalQaChoice, "no_miss_complete");
});

test("zero-candidate promoted survey accepts only exact no-miss completion", async () => {
  const job = waitingJob({ promoted: true, zero: true });
  assert.deepEqual(pipeline.pipelineQuestionQueueBinding(job), {
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCount: 0,
    queueRevision: "questions-v1",
    surveyQaRequired: true,
    zeroCandidateSurvey: true,
  });
  const wrongChoice = await completion.buildPipelineReviewCompletion({
    pipelineJob: job,
    projectId: PROJECT_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCandidateIds: [],
    records: [],
    finalQaChoice: "miss_candidates_reviewed",
  });
  assert.equal(wrongChoice.ok, false);
  assert.equal(wrongChoice.reason, "final_qa_required");
  const complete = await completion.buildPipelineReviewCompletion({
    pipelineJob: job,
    projectId: PROJECT_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCandidateIds: [],
    records: [],
    finalQaChoice: "no_miss_complete",
  });
  assert.equal(complete.ok, true);
  assert.equal(
    complete.request.reviewCompletionId,
    "zero-detections:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  );
  assert.equal(complete.request.finalQaChoice, "no_miss_complete");
});

test("local history, synthetic questions and mismatched provenance cannot complete review", async () => {
  const common = {
    pipelineJob: waitingJob(),
    projectId: PROJECT_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCandidateIds: ["candidate-1", "candidate-2"],
  };
  const localOnly = await completion.buildPipelineReviewCompletion({
    ...common,
    records: [
      {
        ...record("candidate-1", 0),
        recordId: undefined,
        recordHash: undefined,
        answerRevision: undefined,
      },
      record("candidate-2", 1),
    ],
  });
  assert.deepEqual(localOnly.reason, "answer_ledger_incomplete");
  const synthetic = await completion.buildPipelineReviewCompletion({
    ...common,
    questionCandidateIds: ["candidate-1", null],
    records: [record("candidate-1", 0), record("candidate-2", 1)],
  });
  assert.deepEqual(synthetic.reason, "question_set_not_server_owned");
  const mismatched = await completion.buildPipelineReviewCompletion({
    ...common,
    analysisJobId: "66666666-6666-4666-8666-666666666666",
    records: [record("candidate-1", 0), record("candidate-2", 1)],
  });
  assert.deepEqual(mismatched.reason, "analysis_not_bound");
});

test("labeling delays completion until the explicit missed-scene QA decision", () => {
  assert.match(
    labelingSource,
    /typeof response\.record_id !== "string"[\s\S]*typeof response\.record_hash !== "string"[\s\S]*typeof response\.answer_revision !== "number"/,
  );
  assert.match(labelingSource, /buildPipelineReviewCompletion\(\{/);
  assert.match(labelingSource, /service\.completeReview\(result\.request\)/);
  assert.match(labelingSource, /publishPipelineJob\(next\)/);
  assert.match(
    labelingSource,
    /모델 누락 장면 확인[\s\S]*누락 없음 · 검토 완료/,
  );
  assert.match(
    labelingSource,
    /pipelineCompletionAttemptRef\.current = null;[\s\S]{0,160}void requestPipelineReviewCompletion\(history\);[\s\S]{0,240}누락 없음 · 검토 완료/,
  );
  assert.doesNotMatch(
    labelingSource,
    /function goNext[\s\S]{0,700}requestPipelineReviewCompletion/,
  );
  assert.doesNotMatch(
    labelingSource,
    /setQueueComplete\(true\)[\s\S]{0,240}(createIncident|createAnnotation)/,
  );
});

test("ordinary queues auto-start derived outputs after the final saved answer", () => {
  assert.match(
    labelingSource,
    /A normal training\/review queue has no extra confirmation action[\s\S]*!queueComplete[\s\S]*promotedSurveyQaRequired[\s\S]*job\.status !== "awaiting_review"[\s\S]*history\.length !== questions\.length[\s\S]*requestPipelineReviewCompletion\(history\)/,
  );
  assert.match(
    labelingSource,
    /pipelineCompletionAttemptKey\(job, history\)[\s\S]*pipelineCompletionAttemptRef\.current === attemptKey/,
  );
});
