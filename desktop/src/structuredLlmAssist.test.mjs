import assert from "node:assert/strict";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const CANDIDATE_ID = "0123456789abcdefabcd";
const QUESTION_ID = "33333333-3333-4333-8333-333333333333";
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true },
});
const {
  parseStructuredLlmAssistAnswerResponse,
  structuredLlmTrainingAssistsFromQuestionList,
} = await vite.ssrLoadModule("/src/structuredLlmAssist.ts");

after(async () => {
  await vite.close();
});

function assist() {
  return {
    provider: "codex",
    proposal_sha256: "a".repeat(64),
    suggested_answer: "yes",
    selected_issue_type: "pothole",
    recommended_severity: "moderate",
    maintenance_action: "노면 보수",
    ocr_text: null,
    rationale: "승인 사례와 검지 근거가 유사합니다.",
    maintenance_required: true,
    user_authorization_required: true,
    training_only: true,
    label_source: "structured_llm_assisted",
    incident_truth_allowed: false,
    report_truth_allowed: false,
  };
}

test("question parser exposes only fail-closed training-only structured assists", () => {
  const value = {
    project_id: PROJECT_ID,
    analysis_job_id: JOB_ID,
    questions: [
      {
        candidate_id: CANDIDATE_ID,
        question_id: QUESTION_ID,
        structured_llm_training_assist: assist(),
      },
    ],
  };
  const parsed = structuredLlmTrainingAssistsFromQuestionList(value, {
    projectId: PROJECT_ID,
    jobId: JOB_ID,
    candidateIds: [CANDIDATE_ID],
  });
  assert.equal(parsed[CANDIDATE_ID].provider, "codex");
  assert.equal(parsed[CANDIDATE_ID].suggestedAnswer, "yes");
  assert.equal(parsed[CANDIDATE_ID].selectedIssueType, "pothole");

  const unsafe = structuredClone(value);
  unsafe.questions[0].structured_llm_training_assist.incident_truth_allowed =
    true;
  assert.deepEqual(
    structuredLlmTrainingAssistsFromQuestionList(unsafe, {
      projectId: PROJECT_ID,
      jobId: JOB_ID,
      candidateIds: [CANDIDATE_ID],
    }),
    {},
  );
});

test("answer receipt requires server-owned actor and training source", () => {
  const response = {
    project_id: PROJECT_ID,
    analysis_job_id: JOB_ID,
    candidate_id: CANDIDATE_ID,
    question_id: QUESTION_ID,
    record_id: "44444444-4444-4444-8444-444444444444",
    answer_revision: 1,
    partition: "approved",
    include_in_training: true,
    answered_by: "structured_llm_assisted",
    label_source: "structured_llm_assisted",
    answered_at: "2026-07-30T00:00:00Z",
    record_hash: "b".repeat(64),
  };
  const receipt = parseStructuredLlmAssistAnswerResponse(response, {
    projectId: PROJECT_ID,
    jobId: JOB_ID,
    candidateId: CANDIDATE_ID,
    questionId: QUESTION_ID,
  });
  assert.equal(receipt.answerRevision, 1);

  assert.throws(
    () =>
      parseStructuredLlmAssistAnswerResponse(
        { ...response, answered_by: "local_user" },
        {
          projectId: PROJECT_ID,
          jobId: JOB_ID,
          candidateId: CANDIDATE_ID,
          questionId: QUESTION_ID,
        },
      ),
    /응답을 검증/,
  );
});
