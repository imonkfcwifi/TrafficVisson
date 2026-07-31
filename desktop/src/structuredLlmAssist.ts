export type StructuredLlmTrainingAssist = {
  provider: "codex" | "claude";
  proposalSha256: string;
  suggestedAnswer: "yes" | "no";
  selectedIssueType: string | null;
  recommendedSeverity: "minor" | "moderate" | "major" | null;
  maintenanceAction: string | null;
  ocrText: string | null;
  rationale: string;
  maintenanceRequired: boolean;
};

export type StructuredLlmAssistAnswerReceipt = {
  recordId: string;
  recordHash: string;
  answerRevision: number;
  partition: "approved" | "negative";
  answeredAt: string;
};

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CANDIDATE = /^[a-f0-9]{20}$/;
const SHA256 = /^[a-f0-9]{64}$/;

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return (
    actual.length === expected.length &&
    actual.every((key, index) => key === expected[index])
  );
}

function nullableText(value: unknown, maximum: number) {
  return (
    value === null ||
    (typeof value === "string" &&
      value.length >= 1 &&
      value.length <= maximum)
  );
}

export function structuredLlmTrainingAssistsFromQuestionList(
  value: unknown,
  expected: {
    projectId: string;
    jobId: string;
    candidateIds: readonly string[];
  },
) {
  const root = record(value);
  if (
    !root ||
    root.project_id !== expected.projectId ||
    root.analysis_job_id !== expected.jobId ||
    !Array.isArray(root.questions)
  ) {
    return {};
  }
  const allowedCandidates = new Set(
    expected.candidateIds.filter((item) => CANDIDATE.test(item)),
  );
  const result: Record<string, StructuredLlmTrainingAssist> = {};
  for (const raw of root.questions) {
    const question = record(raw);
    if (
      !question ||
      typeof question.candidate_id !== "string" ||
      !allowedCandidates.has(question.candidate_id) ||
      typeof question.question_id !== "string" ||
      !UUID.test(question.question_id) ||
      question.structured_llm_training_assist === null ||
      question.structured_llm_training_assist === undefined
    ) {
      continue;
    }
    const assist = record(question.structured_llm_training_assist);
    if (
      !assist ||
      !exactKeys(assist, [
        "provider",
        "proposal_sha256",
        "suggested_answer",
        "selected_issue_type",
        "recommended_severity",
        "maintenance_action",
        "ocr_text",
        "rationale",
        "maintenance_required",
        "user_authorization_required",
        "training_only",
        "label_source",
        "incident_truth_allowed",
        "report_truth_allowed",
      ]) ||
      !["codex", "claude"].includes(String(assist.provider)) ||
      typeof assist.proposal_sha256 !== "string" ||
      !SHA256.test(assist.proposal_sha256) ||
      !["yes", "no"].includes(String(assist.suggested_answer)) ||
      !nullableText(assist.selected_issue_type, 128) ||
      !(
        assist.recommended_severity === null ||
        ["minor", "moderate", "major"].includes(
          String(assist.recommended_severity),
        )
      ) ||
      !nullableText(assist.maintenance_action, 500) ||
      !nullableText(assist.ocr_text, 500) ||
      typeof assist.rationale !== "string" ||
      assist.rationale.length < 1 ||
      assist.rationale.length > 1500 ||
      typeof assist.maintenance_required !== "boolean" ||
      assist.user_authorization_required !== true ||
      assist.training_only !== true ||
      assist.label_source !== "structured_llm_assisted" ||
      assist.incident_truth_allowed !== false ||
      assist.report_truth_allowed !== false ||
      result[question.candidate_id]
    ) {
      continue;
    }
    result[question.candidate_id] = {
      provider: assist.provider as "codex" | "claude",
      proposalSha256: assist.proposal_sha256,
      suggestedAnswer: assist.suggested_answer as "yes" | "no",
      selectedIssueType: assist.selected_issue_type as string | null,
      recommendedSeverity: assist.recommended_severity as
        | "minor"
        | "moderate"
        | "major"
        | null,
      maintenanceAction: assist.maintenance_action as string | null,
      ocrText: assist.ocr_text as string | null,
      rationale: assist.rationale,
      maintenanceRequired: assist.maintenance_required,
    };
  }
  return result;
}

export function parseStructuredLlmAssistAnswerResponse(
  value: unknown,
  expected: {
    projectId: string;
    jobId: string;
    candidateId: string;
    questionId: string;
  },
): StructuredLlmAssistAnswerReceipt {
  const row = record(value);
  if (
    !row ||
    !exactKeys(row, [
      "project_id",
      "analysis_job_id",
      "candidate_id",
      "question_id",
      "record_id",
      "answer_revision",
      "partition",
      "include_in_training",
      "answered_by",
      "label_source",
      "answered_at",
      "record_hash",
    ]) ||
    row.project_id !== expected.projectId ||
    row.analysis_job_id !== expected.jobId ||
    row.candidate_id !== expected.candidateId ||
    row.question_id !== expected.questionId ||
    typeof row.record_id !== "string" ||
    !UUID.test(row.record_id) ||
    !Number.isInteger(row.answer_revision) ||
    Number(row.answer_revision) < 1 ||
    !["approved", "negative"].includes(String(row.partition)) ||
    row.include_in_training !== true ||
    row.answered_by !== "structured_llm_assisted" ||
    row.label_source !== "structured_llm_assisted" ||
    typeof row.answered_at !== "string" ||
    Number.isNaN(Date.parse(row.answered_at)) ||
    typeof row.record_hash !== "string" ||
    !SHA256.test(row.record_hash)
  ) {
    throw new Error("LLM 학습 보조 라벨 저장 응답을 검증하지 못했습니다.");
  }
  return {
    recordId: row.record_id,
    recordHash: row.record_hash,
    answerRevision: Number(row.answer_revision),
    partition: row.partition as "approved" | "negative",
    answeredAt: row.answered_at,
  };
}
