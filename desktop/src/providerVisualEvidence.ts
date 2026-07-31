import {
  parsePipelineJob,
  type PipelineJob,
} from "./pipelineJobs";

const UUID_PATTERN =
  /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const CANDIDATE_ID_PATTERN = /^[a-f0-9]{20}$/;
const PREPARATION_ID_PATTERN = /^[a-f0-9]{24}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;

export type ProviderVisualEvidenceState =
  | "awaiting_deidentification_review"
  | "verified_for_provider_transmission"
  | "rejected_by_privacy_review";

export type ProviderVisualEvidenceAttachment = {
  order: number;
  role: "candidate_representative" | "similar_case";
  sourceSha256: string;
  outputSha256: string;
  mediaType: "image/png";
  sizeBytes: number;
  width: number;
  height: number;
  faceDetectionCount: number;
  plateDetectionCount: number;
};

export type ProviderVisualEvidencePreparation = {
  schemaVersion:
    "trafficvisson.provider-visual-evidence-preparation.v1";
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  candidateId: string;
  preparationId: string;
  inputRevision: string;
  state: ProviderVisualEvidenceState;
  manifestSha256: string;
  engineIdentitySha256: string;
  attachmentCount: number;
  attachments: readonly ProviderVisualEvidenceAttachment[];
  automaticRedactionPerformed: boolean;
  automaticPrivacyVerification: false;
  humanVerified: boolean;
  reviewRequired: boolean;
  reviewRecordSha256: string | null;
  preparationRevision: string;
};

export type ProviderVisualEvidenceArtifact = {
  mediaType: "image/png";
  sha256: string;
  bytes: Uint8Array;
};

export type ProviderVisualEvidencePrivacyReviewRequest = {
  projectId: string;
  jobId: string;
  candidateId: string;
  preparationId: string;
  expectedManifestSha256: string;
  approved: boolean;
  explicitUserConfirmation: true;
};

export type ProviderVisualEvidencePrivacyReviewResponse = {
  preparation: ProviderVisualEvidencePreparation;
  pipelineResumeRequested: boolean;
  pipelineJob: PipelineJob;
};

export type ProviderVisualPrivacyReviewJob = PipelineJob & {
  status: "blocked";
  currentStage: "vision_proposal";
  outcomeCode: "partial";
  errorCode: "awaiting_deidentification_review";
};

const PREPARATION_KEYS = [
  "schema_version",
  "project_id",
  "pipeline_job_id",
  "analysis_job_id",
  "candidate_id",
  "preparation_id",
  "input_revision",
  "state",
  "manifest_sha256",
  "engine_identity_sha256",
  "attachment_count",
  "attachments",
  "automatic_redaction_performed",
  "automatic_privacy_verification",
  "human_verified",
  "review_required",
  "review_record_sha256",
  "preparation_revision",
] as const;

const ATTACHMENT_KEYS = [
  "order",
  "role",
  "source_sha256",
  "output_sha256",
  "media_type",
  "size_bytes",
  "width",
  "height",
  "face_detection_count",
  "plate_detection_count",
] as const;

function object(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
  label: string,
) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
}

function identity(
  value: unknown,
  pattern: RegExp,
  label: string,
): string {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function integer(
  value: unknown,
  minimum: number,
  maximum: number,
  label: string,
): number {
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < minimum ||
    (value as number) > maximum
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value as number;
}

function boolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

export function parseProviderVisualEvidencePreparation(
  value: unknown,
): ProviderVisualEvidencePreparation {
  const source = object(value, "Provider 개인정보 검토");
  exactKeys(source, PREPARATION_KEYS, "Provider 개인정보 검토");
  if (
    source.schema_version !==
    "trafficvisson.provider-visual-evidence-preparation.v1"
  ) {
    throw new Error(
      "Provider 개인정보 검토 응답 형식이 올바르지 않습니다.",
    );
  }
  const state = source.state;
  if (
    state !== "awaiting_deidentification_review" &&
    state !== "verified_for_provider_transmission" &&
    state !== "rejected_by_privacy_review"
  ) {
    throw new Error(
      "Provider 개인정보 검토 응답 형식이 올바르지 않습니다.",
    );
  }
  if (source.automatic_privacy_verification !== false) {
    throw new Error(
      "자동 개인정보 검증을 완료로 표시한 응답은 사용할 수 없습니다.",
    );
  }
  const attachmentCount = integer(
    source.attachment_count,
    4,
    6,
    "검토 이미지 수",
  );
  if (
    !Array.isArray(source.attachments) ||
    source.attachments.length !== attachmentCount
  ) {
    throw new Error("Provider 검토 이미지 응답 형식이 올바르지 않습니다.");
  }
  const attachments = source.attachments.map((raw, index) => {
    const item = object(raw, `Provider 검토 이미지 ${index + 1}`);
    exactKeys(
      item,
      ATTACHMENT_KEYS,
      `Provider 검토 이미지 ${index + 1}`,
    );
    const order = integer(item.order, 0, 5, "검토 이미지 순서");
    const role = item.role;
    if (
      order !== index ||
      (role !== "candidate_representative" &&
        role !== "similar_case") ||
      (index === 0) !== (role === "candidate_representative") ||
      item.media_type !== "image/png"
    ) {
      throw new Error(
        "Provider 검토 이미지 응답 형식이 올바르지 않습니다.",
      );
    }
    return Object.freeze({
      order,
      role,
      sourceSha256: identity(
        item.source_sha256,
        SHA256_PATTERN,
        "원본 바인딩 해시",
      ),
      outputSha256: identity(
        item.output_sha256,
        SHA256_PATTERN,
        "비식별 이미지 해시",
      ),
      mediaType: "image/png" as const,
      sizeBytes: integer(
        item.size_bytes,
        1,
        8 * 1024 * 1024,
        "검토 이미지 크기",
      ),
      width: integer(item.width, 1, 2_048, "검토 이미지 너비"),
      height: integer(item.height, 1, 2_048, "검토 이미지 높이"),
      faceDetectionCount: integer(
        item.face_detection_count,
        0,
        1_024,
        "얼굴 가림 후보 수",
      ),
      plateDetectionCount: integer(
        item.plate_detection_count,
        0,
        1_024,
        "번호판 가림 후보 수",
      ),
    });
  });
  const humanVerified = boolean(source.human_verified, "사용자 확인");
  const reviewRequired = boolean(source.review_required, "사용자 확인 필요");
  const reviewRecordSha256 =
    source.review_record_sha256 === null
      ? null
      : identity(
          source.review_record_sha256,
          SHA256_PATTERN,
          "개인정보 검토 기록 해시",
        );
  if (
    (state === "awaiting_deidentification_review" &&
      (humanVerified || !reviewRequired || reviewRecordSha256 !== null)) ||
    (state === "verified_for_provider_transmission" &&
      (!humanVerified || reviewRequired || reviewRecordSha256 === null)) ||
    (state === "rejected_by_privacy_review" &&
      (humanVerified || reviewRequired || reviewRecordSha256 === null))
  ) {
    throw new Error(
      "Provider 개인정보 검토 상태가 서로 일치하지 않습니다.",
    );
  }
  return Object.freeze({
    schemaVersion: source.schema_version,
    projectId: identity(source.project_id, UUID_PATTERN, "프로젝트 ID"),
    pipelineJobId: identity(
      source.pipeline_job_id,
      UUID_PATTERN,
      "PipelineJob ID",
    ),
    analysisJobId: identity(
      source.analysis_job_id,
      UUID_PATTERN,
      "분석 작업 ID",
    ),
    candidateId: identity(
      source.candidate_id,
      CANDIDATE_ID_PATTERN,
      "후보 ID",
    ),
    preparationId: identity(
      source.preparation_id,
      PREPARATION_ID_PATTERN,
      "준비 ID",
    ),
    inputRevision: identity(
      source.input_revision,
      SHA256_PATTERN,
      "입력 리비전",
    ),
    state,
    manifestSha256: identity(
      source.manifest_sha256,
      SHA256_PATTERN,
      "준비 manifest 해시",
    ),
    engineIdentitySha256: identity(
      source.engine_identity_sha256,
      SHA256_PATTERN,
      "비식별 엔진 해시",
    ),
    attachmentCount,
    attachments: Object.freeze(attachments),
    automaticRedactionPerformed: boolean(
      source.automatic_redaction_performed,
      "자동 가림 수행 여부",
    ),
    automaticPrivacyVerification: false,
    humanVerified,
    reviewRequired,
    reviewRecordSha256,
    preparationRevision: identity(
      source.preparation_revision,
      SHA256_PATTERN,
      "준비 리비전",
    ),
  });
}

export function parseProviderVisualEvidenceList(
  value: unknown,
): ProviderVisualEvidencePreparation[] {
  const source = object(value, "Provider 개인정보 검토 목록");
  exactKeys(source, ["preparations"], "Provider 개인정보 검토 목록");
  if (
    !Array.isArray(source.preparations) ||
    source.preparations.length > 256
  ) {
    throw new Error(
      "Provider 개인정보 검토 목록 응답 형식이 올바르지 않습니다.",
    );
  }
  const preparations = source.preparations.map(
    parseProviderVisualEvidencePreparation,
  );
  const keys = preparations.map(
    (item) => `${item.candidateId}:${item.preparationId}`,
  );
  if (new Set(keys).size !== keys.length) {
    throw new Error(
      "Provider 개인정보 검토 목록에 중복 항목이 있습니다.",
    );
  }
  return preparations;
}

export function parseProviderVisualEvidenceArtifact(
  value: unknown,
): ProviderVisualEvidenceArtifact {
  const source = object(value, "Provider 검토 이미지");
  exactKeys(
    source,
    ["media_type", "sha256", "bytes"],
    "Provider 검토 이미지",
  );
  if (
    source.media_type !== "image/png" ||
    !(source.bytes instanceof Uint8Array) ||
    source.bytes.byteLength < 8 ||
    source.bytes.byteLength > 8 * 1024 * 1024
  ) {
    throw new Error(
      "Provider 검토 이미지 응답 형식이 올바르지 않습니다.",
    );
  }
  return Object.freeze({
    mediaType: "image/png",
    sha256: identity(
      source.sha256,
      SHA256_PATTERN,
      "비식별 이미지 해시",
    ),
    bytes: source.bytes,
  });
}

export function buildProviderVisualEvidencePrivacyReviewRequest(
  preparation: ProviderVisualEvidencePreparation,
  approved: boolean,
): ProviderVisualEvidencePrivacyReviewRequest {
  if (
    preparation.state !== "awaiting_deidentification_review" ||
    preparation.automaticPrivacyVerification !== false ||
    typeof approved !== "boolean"
  ) {
    throw new Error("현재 개인정보 검토 항목에는 판정을 저장할 수 없습니다.");
  }
  return Object.freeze({
    projectId: preparation.projectId,
    jobId: preparation.pipelineJobId,
    candidateId: preparation.candidateId,
    preparationId: preparation.preparationId,
    expectedManifestSha256: preparation.manifestSha256,
    approved,
    explicitUserConfirmation: true,
  });
}

export function parseProviderVisualEvidencePrivacyReviewResponse(
  value: unknown,
): ProviderVisualEvidencePrivacyReviewResponse {
  const source = object(value, "Provider 개인정보 판정");
  exactKeys(
    source,
    ["preparation", "pipeline_resume_requested", "pipeline_job"],
    "Provider 개인정보 판정",
  );
  if (typeof source.pipeline_resume_requested !== "boolean") {
    throw new Error(
      "Provider 개인정보 판정 응답 형식이 올바르지 않습니다.",
    );
  }
  const preparation = parseProviderVisualEvidencePreparation(
    source.preparation,
  );
  const pipelineJob = parsePipelineJob(source.pipeline_job);
  if (
    pipelineJob.projectId !== preparation.projectId ||
    pipelineJob.jobId !== preparation.pipelineJobId ||
    (source.pipeline_resume_requested &&
      !["queued", "running"].includes(pipelineJob.status))
  ) {
    throw new Error(
      "Provider 개인정보 판정의 PipelineJob이 일치하지 않습니다.",
    );
  }
  return {
    preparation,
    pipelineResumeRequested: source.pipeline_resume_requested,
    pipelineJob,
  };
}

export function isProviderVisualPrivacyReviewInterruption(
  job: PipelineJob | null,
): job is ProviderVisualPrivacyReviewJob {
  return Boolean(
    job &&
      job.status === "blocked" &&
      job.currentStage === "vision_proposal" &&
      job.outcomeCode === "partial" &&
      job.errorCode === "awaiting_deidentification_review",
  );
}
