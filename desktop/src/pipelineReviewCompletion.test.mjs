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

function waitingJob() {
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
              analysis_job_id: ANALYSIS_JOB_ID,
              source_sha256: SOURCE_SHA,
              question_count: 2,
              question_queue_revision: "questions-v1",
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
  });
  delete job.stages[10].checkpoint.analysis_job_id;
  assert.equal(pipeline.pipelineQuestionQueueBinding(job), null);
});

test("only a complete server-persisted answer ledger builds an idempotent completion request", async () => {
  const records = [record("candidate-1", 0), record("candidate-2", 1)];
  const result = await completion.buildPipelineReviewCompletion({
    pipelineJob: waitingJob(),
    projectId: PROJECT_ID,
    analysisJobId: ANALYSIS_JOB_ID,
    sourceSha256: SOURCE_SHA,
    questionCandidateIds: ["candidate-1", "candidate-2"],
    records,
  });
  assert.equal(result.ok, true);
  assert.match(result.request.reviewCompletionId, /^review:[a-f0-9]{64}$/);
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

test("labeling completes the pipeline only after a persisted answer proof", () => {
  assert.match(
    labelingSource,
    /typeof response\.record_id !== "string"[\s\S]*typeof response\.record_hash !== "string"[\s\S]*typeof response\.answer_revision !== "number"/,
  );
  assert.match(labelingSource, /buildPipelineReviewCompletion\(\{/);
  assert.match(labelingSource, /service\.completeReview\(result\.request\)/);
  assert.match(labelingSource, /publishPipelineJob\(next\)/);
  assert.match(
    labelingSource,
    /void requestPipelineReviewCompletion\(nextHistory\)/,
  );
  assert.doesNotMatch(
    labelingSource,
    /setQueueComplete\(true\)[\s\S]{0,240}(createIncident|createAnnotation)/,
  );
});
