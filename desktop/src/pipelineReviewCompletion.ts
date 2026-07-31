import type { PipelineJob } from "./pipelineJobs";
import { pipelineQuestionQueueBinding } from "./pipelineJobs";
import type { ReviewRecord } from "./reviewRecords";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const CANDIDATE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export type PipelineReviewCompletionRequest = {
  projectId: string;
  jobId: string;
  reviewCompletionId: string;
  finalQaChoice?:
    | "no_miss_complete"
    | "miss_candidates_reviewed";
};

export type PipelineReviewCompletionResult =
  | {
      ok: true;
      request: PipelineReviewCompletionRequest;
      answerLedgerSha256: string;
    }
  | {
      ok: false;
      reason:
        | "pipeline_not_waiting"
        | "analysis_not_bound"
        | "question_set_not_server_owned"
        | "answer_ledger_incomplete"
        | "final_qa_required"
        | "digest_unavailable";
      message: string;
    };

type BuildReviewCompletionInput = {
  pipelineJob: PipelineJob | null;
  projectId: string | null;
  analysisJobId: string | null;
  sourceSha256: string | null;
  questionCandidateIds: readonly (string | null)[];
  records: readonly ReviewRecord[];
  finalQaChoice?:
    | "no_miss_complete"
    | "miss_candidates_reviewed";
};

function unavailable(
  reason: Extract<PipelineReviewCompletionResult, { ok: false }>["reason"],
  message: string,
): PipelineReviewCompletionResult {
  return { ok: false, reason, message };
}

async function sha256Hex(value: string) {
  if (!globalThis.crypto?.subtle) {
    throw new Error("Web Crypto is unavailable");
  }
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

/**
 * Builds only an idempotency request. The renderer never declares review
 * complete: the bridge re-reads its own question/answer ledger and rejects an
 * incomplete or mismatched queue before derived outputs can start.
 */
export async function buildPipelineReviewCompletion(
  input: BuildReviewCompletionInput,
): Promise<PipelineReviewCompletionResult> {
  const job = input.pipelineJob;
  const projectId = input.projectId?.toLowerCase() ?? null;
  const analysisJobId = input.analysisJobId?.toLowerCase() ?? null;
  if (
    !job ||
    !projectId ||
    job.projectId !== projectId ||
    job.status !== "awaiting_review"
  ) {
    return unavailable(
      "pipeline_not_waiting",
      "현재 프로젝트의 자동 파이프라인이 질문 검토 완료를 기다리는 상태가 아닙니다.",
    );
  }
  const binding = pipelineQuestionQueueBinding(job);
  if (
    !binding ||
    !analysisJobId ||
    !UUID_PATTERN.test(analysisJobId) ||
    binding.analysisJobId !== analysisJobId ||
    !input.sourceSha256 ||
    !SHA256_PATTERN.test(input.sourceSha256) ||
    binding.sourceSha256 !== input.sourceSha256
  ) {
    return unavailable(
      "analysis_not_bound",
      "질문 큐와 현재 분석 영상의 서버 provenance가 일치하지 않습니다.",
    );
  }
  const candidateIds = input.questionCandidateIds;
  const invalidQuestionSet =
    candidateIds.length !== binding.questionCount ||
    (binding.zeroCandidateSurvey
      ? candidateIds.length !== 0
      : candidateIds.length < 1) ||
    candidateIds.some(
      (value) =>
        typeof value !== "string" || !CANDIDATE_ID_PATTERN.test(value),
    ) ||
    new Set(candidateIds).size !== candidateIds.length;
  if (invalidQuestionSet) {
    return unavailable(
      "question_set_not_server_owned",
      "서버가 만든 질문 큐와 현재 화면의 질문 집합이 일치하지 않습니다.",
    );
  }
  if (
    binding.surveyQaRequired &&
    (input.finalQaChoice !== "no_miss_complete" &&
      input.finalQaChoice !== "miss_candidates_reviewed")
  ) {
    return unavailable(
      "final_qa_required",
      "승격 모델 조사 결과는 전체 2fps 누락 확인을 마쳐야 완료할 수 있습니다.",
    );
  }
  if (
    binding.zeroCandidateSurvey &&
    input.finalQaChoice !== "no_miss_complete"
  ) {
    return unavailable(
      "final_qa_required",
      "후보 0건 조사는 전체 2fps를 확인한 뒤 ‘누락 없음’으로만 완료할 수 있습니다.",
    );
  }

  const latestByCandidate = new Map<string, ReviewRecord>();
  for (const record of input.records) {
    if (!record.candidateId) continue;
    const current = latestByCandidate.get(record.candidateId);
    if (
      !current ||
      (record.answerRevision ?? 0) > (current.answerRevision ?? 0)
    ) {
      latestByCandidate.set(record.candidateId, record);
    }
  }
  const proofs: string[] = [];
  for (const candidateId of candidateIds as readonly string[]) {
    const record = latestByCandidate.get(candidateId);
    if (
      !record ||
      !record.recordId ||
      !UUID_PATTERN.test(record.recordId.toLowerCase()) ||
      !record.recordHash ||
      !SHA256_PATTERN.test(record.recordHash) ||
      typeof record.answerRevision !== "number" ||
      !Number.isInteger(record.answerRevision) ||
      record.answerRevision < 1
    ) {
      return unavailable(
        "answer_ledger_incomplete",
        "모든 질문 답변이 서버에 영속 저장된 뒤에만 결과를 갱신할 수 있습니다.",
      );
    }
    proofs.push(
      [
        candidateId,
        record.recordId.toLowerCase(),
        record.recordHash,
        String(record.answerRevision),
      ].join("\0"),
    );
  }
  try {
    const answerLedgerSha256 = await sha256Hex(proofs.sort().join("\n"));
    const finalQaChoice = binding.surveyQaRequired
      ? input.finalQaChoice
      : undefined;
    return {
      ok: true,
      request: {
        projectId,
        jobId: job.jobId,
        reviewCompletionId: binding.zeroCandidateSurvey
          ? `zero-detections:${answerLedgerSha256}`
          : `review:${answerLedgerSha256}`,
        ...(finalQaChoice ? { finalQaChoice } : {}),
      },
      answerLedgerSha256,
    };
  } catch {
    return unavailable(
      "digest_unavailable",
      "검토 기록의 멱등 식별자를 만들 수 없어 자동 결과 갱신을 시작하지 않았습니다.",
    );
  }
}
