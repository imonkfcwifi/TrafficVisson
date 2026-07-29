export type DenseIndexJobResponse = {
  job_id: string;
  project_id: string;
  analysis_job_id: string;
  source_sha256: string;
  sampling_fps: 1 | 2 | 3 | 4 | 5;
  status:
    | "queued"
    | "running"
    | "cancelling"
    | "completed"
    | "cancelled"
    | "failed";
  stage:
    | "queued"
    | "resolving_source"
    | "indexing_frames"
    | "finalizing"
    | "cancelling"
    | "completed"
    | "cancelled"
    | "failed";
  progress_percent: number | null;
  progress_state:
    | "waiting"
    | "determinate"
    | "indeterminate"
    | "terminal";
  progress_basis: "stage_only_no_fabricated_frame_progress";
  pause_supported: false;
  error_code:
    | "source_changed"
    | "timeout"
    | "output_limit"
    | "indexing_failed"
    | "persistence_failed"
    | "internal_error"
    | null;
  created_at: string;
  updated_at: string;
  record_sha256: string | null;
  safety: {
    review_only: true;
    automatic_incident_creation: false;
    automatic_annotation_creation: false;
    automatic_training_ingest: false;
    automatic_maintenance_decision: false;
    automatic_model_promotion: false;
  };
  result: null | {
    artifact_set_id: string;
    frame_count: number;
    total_frame_bytes: number;
    index_artifact_ref: string;
  };
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA_PATTERN = /^[a-f0-9]{64}$/;
const STATUSES = new Set([
  "queued",
  "running",
  "cancelling",
  "completed",
  "cancelled",
  "failed",
]);
const STAGES = new Set([
  "queued",
  "resolving_source",
  "indexing_frames",
  "finalizing",
  "cancelling",
  "completed",
  "cancelled",
  "failed",
]);
const PROGRESS_STATES = new Set([
  "waiting",
  "determinate",
  "indeterminate",
  "terminal",
]);
const ERROR_CODES = new Set([
  "source_changed",
  "timeout",
  "output_limit",
  "indexing_failed",
  "persistence_failed",
  "internal_error",
]);
const JOB_KEYS = [
  "analysis_job_id",
  "created_at",
  "error_code",
  "job_id",
  "pause_supported",
  "progress_basis",
  "progress_percent",
  "progress_state",
  "project_id",
  "record_sha256",
  "result",
  "safety",
  "sampling_fps",
  "source_sha256",
  "stage",
  "status",
  "updated_at",
];
const SAFETY_KEYS = [
  "automatic_annotation_creation",
  "automatic_incident_creation",
  "automatic_maintenance_decision",
  "automatic_model_promotion",
  "automatic_training_ingest",
  "review_only",
];
const RESULT_KEYS = [
  "artifact_set_id",
  "frame_count",
  "index_artifact_ref",
  "total_frame_bytes",
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expected: string[],
) {
  const keys = Object.keys(value).sort();
  return (
    keys.length === expected.length &&
    keys.every((key, index) => key === expected[index])
  );
}

function isTimestamp(value: unknown) {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 64 &&
    Number.isFinite(Date.parse(value))
  );
}

function validSafety(value: unknown) {
  return (
    isRecord(value) &&
    hasExactKeys(value, SAFETY_KEYS) &&
    value.review_only === true &&
    value.automatic_incident_creation === false &&
    value.automatic_annotation_creation === false &&
    value.automatic_training_ingest === false &&
    value.automatic_maintenance_decision === false &&
    value.automatic_model_promotion === false
  );
}

function validResult(value: unknown, jobId: string) {
  return (
    isRecord(value) &&
    hasExactKeys(value, RESULT_KEYS) &&
    value.artifact_set_id === jobId &&
    Number.isSafeInteger(value.frame_count) &&
    Number(value.frame_count) >= 1 &&
    Number(value.frame_count) <= 30_000 &&
    Number.isSafeInteger(value.total_frame_bytes) &&
    Number(value.total_frame_bytes) >= 1 &&
    Number(value.total_frame_bytes) <= 16 * 1024 ** 3 &&
    value.index_artifact_ref ===
      `dense-index:${jobId}:frame-index`
  );
}

export function parseDenseIndexJob(
  value: unknown,
): DenseIndexJobResponse {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, JOB_KEYS) ||
    typeof value.job_id !== "string" ||
    !UUID_PATTERN.test(value.job_id) ||
    typeof value.project_id !== "string" ||
    !UUID_PATTERN.test(value.project_id) ||
    typeof value.analysis_job_id !== "string" ||
    !UUID_PATTERN.test(value.analysis_job_id) ||
    typeof value.source_sha256 !== "string" ||
    !SHA_PATTERN.test(value.source_sha256) ||
    !Number.isSafeInteger(value.sampling_fps) ||
    Number(value.sampling_fps) < 1 ||
    Number(value.sampling_fps) > 5 ||
    typeof value.status !== "string" ||
    !STATUSES.has(value.status) ||
    typeof value.stage !== "string" ||
    !STAGES.has(value.stage) ||
    !(
      value.progress_percent === null ||
      (Number.isSafeInteger(value.progress_percent) &&
        Number(value.progress_percent) >= 0 &&
        Number(value.progress_percent) <= 100)
    ) ||
    typeof value.progress_state !== "string" ||
    !PROGRESS_STATES.has(value.progress_state) ||
    value.progress_basis !==
      "stage_only_no_fabricated_frame_progress" ||
    value.pause_supported !== false ||
    !(
      value.error_code === null ||
      (typeof value.error_code === "string" &&
        ERROR_CODES.has(value.error_code))
    ) ||
    !isTimestamp(value.created_at) ||
    !isTimestamp(value.updated_at) ||
    !(
      value.record_sha256 === null ||
      (typeof value.record_sha256 === "string" &&
        SHA_PATTERN.test(value.record_sha256))
    ) ||
    !validSafety(value.safety) ||
    !(
      value.result === null ||
      validResult(value.result, value.job_id)
    )
  ) {
    throw new Error(
      "밀집 프레임 작업 응답 형식을 검증하지 못했습니다.",
    );
  }

  const terminal = ["completed", "cancelled", "failed"].includes(
    value.status,
  );
  if (
    (value.status === "completed" &&
      (value.stage !== "completed" ||
        value.progress_percent !== 100 ||
        value.progress_state !== "determinate" ||
        value.result === null ||
        value.error_code !== null ||
        value.record_sha256 === null)) ||
    (value.status === "failed" &&
      (value.stage !== "failed" ||
        value.progress_percent !== null ||
        value.progress_state !== "terminal" ||
        value.result !== null ||
        value.error_code === null)) ||
    (value.status === "cancelled" &&
      (value.stage !== "cancelled" ||
        value.progress_percent !== null ||
        value.progress_state !== "terminal" ||
        value.result !== null ||
        value.error_code !== null)) ||
    (!terminal &&
      (value.result !== null ||
        value.error_code !== null ||
        value.record_sha256 !== null)) ||
    (value.stage === "queued" &&
      (value.progress_percent !== 0 ||
        value.progress_state !== "waiting")) ||
    (["indexing_frames", "cancelling"].includes(value.stage) &&
      (value.progress_percent !== null ||
        value.progress_state !== "indeterminate"))
  ) {
    throw new Error(
      "밀집 프레임 작업 상태와 결과가 일치하지 않습니다.",
    );
  }
  return value as DenseIndexJobResponse;
}

export function parseDenseIndexJobList(
  value: unknown,
): DenseIndexJobResponse[] {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["jobs"]) ||
    !Array.isArray(value.jobs) ||
    value.jobs.length > 128
  ) {
    throw new Error(
      "밀집 프레임 작업 목록 형식을 검증하지 못했습니다.",
    );
  }
  return value.jobs.map(parseDenseIndexJob);
}

export function denseIndexStatusLabel(
  job: DenseIndexJobResponse | null,
) {
  if (!job) return "준비 안 됨";
  if (job.status === "completed") {
    return `${job.result?.frame_count ?? 0}프레임 준비됨`;
  }
  if (job.status === "failed") {
    return job.error_code === "source_changed"
      ? "원본 재선택 필요"
      : `준비 실패 · ${job.error_code ?? "unknown"}`;
  }
  if (job.status === "cancelled") return "준비 취소됨";
  if (job.stage === "resolving_source") return "원본 재검증";
  if (job.stage === "indexing_frames") return "프레임 생성 중";
  if (job.stage === "finalizing") return "해시 검증·인덱스 저장";
  if (job.stage === "cancelling") return "취소 처리 중";
  return "대기열";
}
