import type { AnalysisJobResponse } from "./analysisJobs";
import type { RuntimeStatus } from "./runtimeState";

export const candidateExtractionSteps = [
  { stage: "queued", label: "대기열 등록" },
  { stage: "validating_source", label: "원본 해시·파일 변경 여부 검증" },
  {
    stage: "extracting_candidates",
    label: "장면 변화·선명도 후보와 근거 생성",
  },
  { stage: "completed", label: "후보·JPEG·미리보기 클립 저장" },
] as const;

export type CandidateExtractionStepState = "done" | "active" | "waiting";

export function savedJobStatusLabel(
  status: AnalysisJobResponse["status"],
): string {
  if (status === "completed") return "후보 추출 완료";
  if (status === "failed") return "후보 추출 실패";
  if (status === "cancelled") return "후보 추출 취소됨";
  if (status === "cancelling") return "취소 요청됨";
  if (status === "running") return "후보 추출 중";
  return "대기열";
}

export function runtimeStatusLabel(
  status: RuntimeStatus,
  jobStatus?: AnalysisJobResponse["status"],
): string {
  if (jobStatus === "cancelling") return "취소 요청됨";
  if (status === "complete") return "후보 추출 완료";
  if (status === "failed") return "후보 추출 실패";
  if (status === "cancelled") return "후보 추출 취소됨";
  if (status === "running") return "후보 추출 중";
  if (status === "queued") return "대기열";
  if (status === "intake_ready") return "시작 대기";
  return "입력 대기";
}

export function extractionStepState(
  stepIndex: number,
  stage: AnalysisJobResponse["stage"] | null,
  status: AnalysisJobResponse["status"] | RuntimeStatus,
): CandidateExtractionStepState {
  if (status === "completed" || status === "complete") return "done";
  if (
    status === "failed" ||
    status === "cancelled" ||
    stage === "failed" ||
    stage === "cancelled"
  ) {
    return "waiting";
  }
  const currentIndex = candidateExtractionSteps.findIndex(
    (step) => step.stage === stage,
  );
  if (currentIndex < 0) return "waiting";
  if (stepIndex < currentIndex) return "done";
  return stepIndex === currentIndex ? "active" : "waiting";
}

export function formatArtifactBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value >= 1_000_000_000) {
    return `${(value / 1_000_000_000).toFixed(2)} GB`;
  }
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(2)} MB`;
  }
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)} KB`;
  return `${Math.round(value).toLocaleString("ko-KR")} B`;
}
