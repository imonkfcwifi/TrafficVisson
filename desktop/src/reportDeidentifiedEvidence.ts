import {
  parsePipelineJob,
  type PipelineJob,
} from "./pipelineJobs";

const UUID_PATTERN =
  /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const CANDIDATE_ID_PATTERN = /^[a-f0-9]{20}$/;
const DERIVATIVE_ID_PATTERN = /^rde_[a-f0-9]{32}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const DEIDENTIFIED_REF_PATTERN =
  /^report-deid:[a-f0-9]{32}:[a-f0-9]{32}$/;

export type ReportDeidentifiedEvidenceState =
  | "awaiting_deidentification_review"
  | "verified_for_shared_report"
  | "rejected_by_deidentification_review";

export type ReportRedactionRegion = {
  kind: "face" | "license_plate";
  x: number;
  y: number;
  width: number;
  height: number;
};

export type ReportPrivacyDetector = {
  detectorName: string;
  detectorVersion: string;
  modelId: string;
  modelSha256: string;
  coverageGuaranteed: false;
  humanReviewRequired: true;
};

export type ReportDeidentifiedPreparation = {
  schemaVersion: "trafficvisson.report-deidentified-evidence.v1";
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  sourceSha256: string;
  incidentId: string;
  candidateId: string;
  derivativeId: string;
  deidentifiedRef: string;
  state: ReportDeidentifiedEvidenceState;
  inputRevision: string;
  manifestSha256: string;
  originalSha256: string;
  deidentifiedSha256: string;
  mediaType: "image/jpeg";
  sizeBytes: number;
  width: number;
  height: number;
  detector: ReportPrivacyDetector;
  detectorIdentitySha256: string;
  redactionRegions: readonly ReportRedactionRegion[];
  redactionRegionCount: number;
  faceDetectionCount: number;
  plateDetectionCount: number;
  automaticRedactionPerformed: boolean;
  automaticPrivacyVerification: false;
  humanVerified: boolean;
  reviewRequired: boolean;
  reviewerDecision: "approved" | "rejected" | null;
  reviewerActor: "local_user" | null;
  reviewedAt: string | null;
  reviewRevision: 0 | 1;
  reviewRecordSha256: string | null;
  deidentificationRecordSha256: string | null;
  preparationRevision: string;
};

export type ReportDeidentifiedEvidenceList = {
  schemaVersion:
    "trafficvisson.report-deidentified-evidence-list.v1";
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  state: "prepared_review_required" | "listed";
  count: number;
  preparations: readonly ReportDeidentifiedPreparation[];
};

export type ReportDeidentifiedArtifact = {
  mediaType: "image/jpeg";
  sha256: string;
  bytes: Uint8Array;
};

export type ReportDeidentifiedReviewRequest = {
  projectId: string;
  jobId: string;
  incidentId: string;
  derivativeId: string;
  expectedManifestSha256: string;
  approved: boolean;
  explicitUserConfirmation: true;
};

export type ReportDeidentifiedReviewResponse = {
  schemaVersion:
    "trafficvisson.report-deidentified-evidence-review-response.v1";
  preparation: ReportDeidentifiedPreparation;
  allRequiredDerivativesVerified: boolean;
  pipelineResumeRequested: boolean;
  pipelineJob: PipelineJob;
};

export type ReportDeidentifiedReviewJob = PipelineJob & {
  status: "blocked";
  currentStage: "derived_outputs";
  outcomeCode: "report_preview_blocked";
  errorCode: "deidentified_evidence_required";
};

const PREPARATION_KEYS = [
  "schemaVersion",
  "projectId",
  "pipelineJobId",
  "analysisJobId",
  "sourceSha256",
  "incidentId",
  "candidateId",
  "derivativeId",
  "deidentifiedRef",
  "state",
  "inputRevision",
  "manifestSha256",
  "originalSha256",
  "deidentifiedSha256",
  "mediaType",
  "sizeBytes",
  "width",
  "height",
  "detector",
  "detectorIdentitySha256",
  "redactionRegions",
  "redactionRegionCount",
  "faceDetectionCount",
  "plateDetectionCount",
  "automaticRedactionPerformed",
  "automaticPrivacyVerification",
  "humanVerified",
  "reviewRequired",
  "reviewerDecision",
  "reviewerActor",
  "reviewedAt",
  "reviewRevision",
  "reviewRecordSha256",
  "deidentificationRecordSha256",
  "preparationRevision",
] as const;

function object(
  value: unknown,
  label: string,
): Record<string, unknown> {
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

function bool(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function nullableHash(value: unknown, label: string): string | null {
  return value === null
    ? null
    : identity(value, SHA256_PATTERN, label);
}

function nullableText(
  value: unknown,
  allowed: readonly string[],
  label: string,
): string | null {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 128 ||
    (allowed.length > 0 && !allowed.includes(value))
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

export function parseReportDeidentifiedPreparation(
  value: unknown,
): ReportDeidentifiedPreparation {
  const source = object(value, "공유 보고서 비식별 증거");
  exactKeys(
    source,
    PREPARATION_KEYS,
    "공유 보고서 비식별 증거",
  );
  if (
    source.schemaVersion !==
      "trafficvisson.report-deidentified-evidence.v1" ||
    source.mediaType !== "image/jpeg" ||
    source.automaticPrivacyVerification !== false
  ) {
    throw new Error(
      "공유 보고서 비식별 증거 응답 형식이 올바르지 않습니다.",
    );
  }
  const projectId = identity(
    source.projectId,
    UUID_PATTERN,
    "프로젝트 ID",
  );
  const pipelineJobId = identity(
    source.pipelineJobId,
    UUID_PATTERN,
    "PipelineJob ID",
  );
  const analysisJobId = identity(
    source.analysisJobId,
    UUID_PATTERN,
    "분석 작업 ID",
  );
  const incidentId = identity(
    source.incidentId,
    UUID_PATTERN,
    "사건 ID",
  );
  const derivativeId = identity(
    source.derivativeId,
    DERIVATIVE_ID_PATTERN,
    "비식별 파생본 ID",
  );
  const deidentifiedRef = identity(
    source.deidentifiedRef,
    DEIDENTIFIED_REF_PATTERN,
    "비식별 증거 참조",
  );
  if (
    deidentifiedRef !==
    `report-deid:${incidentId.replaceAll("-", "")}:${derivativeId.slice(4)}`
  ) {
    throw new Error("비식별 증거 참조가 사건과 일치하지 않습니다.");
  }
  const state = source.state;
  if (
    state !== "awaiting_deidentification_review" &&
    state !== "verified_for_shared_report" &&
    state !== "rejected_by_deidentification_review"
  ) {
    throw new Error("비식별 증거 검토 상태가 올바르지 않습니다.");
  }
  const width = integer(source.width, 16, 4096, "이미지 너비");
  const height = integer(source.height, 16, 4096, "이미지 높이");
  const redactionRegionCount = integer(
    source.redactionRegionCount,
    0,
    4096,
    "가림 영역 수",
  );
  if (
    !Array.isArray(source.redactionRegions) ||
    source.redactionRegions.length !== redactionRegionCount
  ) {
    throw new Error("가림 영역 응답 형식이 올바르지 않습니다.");
  }
  const redactionRegions = source.redactionRegions.map(
    (raw, index) => {
      const row = object(raw, `가림 영역 ${index + 1}`);
      exactKeys(
        row,
        ["kind", "x", "y", "width", "height"],
        `가림 영역 ${index + 1}`,
      );
      if (row.kind !== "face" && row.kind !== "license_plate") {
        throw new Error("가림 영역 유형이 올바르지 않습니다.");
      }
      const x = integer(row.x, 0, width - 1, "가림 영역 x");
      const y = integer(row.y, 0, height - 1, "가림 영역 y");
      const regionWidth = integer(
        row.width,
        1,
        width,
        "가림 영역 너비",
      );
      const regionHeight = integer(
        row.height,
        1,
        height,
        "가림 영역 높이",
      );
      if (x + regionWidth > width || y + regionHeight > height) {
        throw new Error("가림 영역이 이미지 범위를 벗어났습니다.");
      }
      return Object.freeze({
        kind: row.kind,
        x,
        y,
        width: regionWidth,
        height: regionHeight,
      });
    },
  );
  const faceDetectionCount = redactionRegions.filter(
    (row) => row.kind === "face",
  ).length;
  const plateDetectionCount = redactionRegions.filter(
    (row) => row.kind === "license_plate",
  ).length;
  if (
    source.faceDetectionCount !== faceDetectionCount ||
    source.plateDetectionCount !== plateDetectionCount ||
    source.automaticRedactionPerformed !==
      (redactionRegionCount > 0)
  ) {
    throw new Error("자동 가림 영역 수가 일치하지 않습니다.");
  }
  const detectorSource = object(source.detector, "비식별 검지기");
  exactKeys(
    detectorSource,
    [
      "detectorName",
      "detectorVersion",
      "modelId",
      "modelSha256",
      "coverageGuaranteed",
      "humanReviewRequired",
    ],
    "비식별 검지기",
  );
  if (
    detectorSource.coverageGuaranteed !== false ||
    detectorSource.humanReviewRequired !== true
  ) {
    throw new Error("비식별 검지기는 사용자 확인이 필요합니다.");
  }
  const detector: ReportPrivacyDetector = Object.freeze({
    detectorName: nullableText(
      detectorSource.detectorName,
      [],
      "검지기 이름",
    ) as string,
    detectorVersion: nullableText(
      detectorSource.detectorVersion,
      [],
      "검지기 버전",
    ) as string,
    modelId: nullableText(
      detectorSource.modelId,
      [],
      "모델 ID",
    ) as string,
    modelSha256: identity(
      detectorSource.modelSha256,
      SHA256_PATTERN,
      "모델 해시",
    ),
    coverageGuaranteed: false,
    humanReviewRequired: true,
  });
  const humanVerified = bool(source.humanVerified, "사용자 승인");
  const reviewRequired = bool(source.reviewRequired, "검토 필요");
  const reviewerDecision = nullableText(
    source.reviewerDecision,
    ["approved", "rejected"],
    "사용자 판정",
  ) as "approved" | "rejected" | null;
  const reviewerActor = nullableText(
    source.reviewerActor,
    ["local_user"],
    "검토자",
  ) as "local_user" | null;
  const reviewedAt = nullableText(
    source.reviewedAt,
    [],
    "검토 시각",
  );
  const reviewRevision = integer(
    source.reviewRevision,
    0,
    1,
    "검토 리비전",
  ) as 0 | 1;
  const reviewRecordSha256 = nullableHash(
    source.reviewRecordSha256,
    "검토 기록 해시",
  );
  const deidentificationRecordSha256 = nullableHash(
    source.deidentificationRecordSha256,
    "비식별 전이 기록 해시",
  );
  if (
    (state === "awaiting_deidentification_review" &&
      (humanVerified ||
        !reviewRequired ||
        reviewerDecision !== null ||
        reviewerActor !== null ||
        reviewedAt !== null ||
        reviewRevision !== 0 ||
        reviewRecordSha256 !== null ||
        deidentificationRecordSha256 !== null)) ||
    (state === "verified_for_shared_report" &&
      (!humanVerified ||
        reviewRequired ||
        reviewerDecision !== "approved" ||
        reviewerActor !== "local_user" ||
        reviewedAt === null ||
        reviewRevision !== 1 ||
        reviewRecordSha256 === null ||
        deidentificationRecordSha256 === null)) ||
    (state === "rejected_by_deidentification_review" &&
      (humanVerified ||
        reviewRequired ||
        reviewerDecision !== "rejected" ||
        reviewerActor !== "local_user" ||
        reviewedAt === null ||
        reviewRevision !== 1 ||
        reviewRecordSha256 === null ||
        deidentificationRecordSha256 !== null))
  ) {
    throw new Error("비식별 검토 상태가 서로 일치하지 않습니다.");
  }
  return Object.freeze({
    schemaVersion: source.schemaVersion,
    projectId,
    pipelineJobId,
    analysisJobId,
    sourceSha256: identity(
      source.sourceSha256,
      SHA256_PATTERN,
      "원본 영상 해시",
    ),
    incidentId,
    candidateId: identity(
      source.candidateId,
      CANDIDATE_ID_PATTERN,
      "후보 ID",
    ),
    derivativeId,
    deidentifiedRef,
    state,
    inputRevision: identity(
      source.inputRevision,
      SHA256_PATTERN,
      "입력 리비전",
    ),
    manifestSha256: identity(
      source.manifestSha256,
      SHA256_PATTERN,
      "manifest 해시",
    ),
    originalSha256: identity(
      source.originalSha256,
      SHA256_PATTERN,
      "원본 증거 해시",
    ),
    deidentifiedSha256: identity(
      source.deidentifiedSha256,
      SHA256_PATTERN,
      "비식별 증거 해시",
    ),
    mediaType: "image/jpeg",
    sizeBytes: integer(
      source.sizeBytes,
      1,
      16 * 1024 * 1024,
      "이미지 크기",
    ),
    width,
    height,
    detector,
    detectorIdentitySha256: identity(
      source.detectorIdentitySha256,
      SHA256_PATTERN,
      "검지기 해시",
    ),
    redactionRegions: Object.freeze(redactionRegions),
    redactionRegionCount,
    faceDetectionCount,
    plateDetectionCount,
    automaticRedactionPerformed: redactionRegionCount > 0,
    automaticPrivacyVerification: false,
    humanVerified,
    reviewRequired,
    reviewerDecision,
    reviewerActor,
    reviewedAt,
    reviewRevision,
    reviewRecordSha256,
    deidentificationRecordSha256,
    preparationRevision: identity(
      source.preparationRevision,
      SHA256_PATTERN,
      "준비 리비전",
    ),
  });
}

export function parseReportDeidentifiedEvidenceList(
  value: unknown,
): ReportDeidentifiedEvidenceList {
  const source = object(value, "공유 보고서 비식별 증거 목록");
  exactKeys(
    source,
    [
      "schemaVersion",
      "projectId",
      "pipelineJobId",
      "analysisJobId",
      "state",
      "count",
      "preparations",
    ],
    "공유 보고서 비식별 증거 목록",
  );
  if (
    source.schemaVersion !==
      "trafficvisson.report-deidentified-evidence-list.v1" ||
    (source.state !== "prepared_review_required" &&
      source.state !== "listed")
  ) {
    throw new Error(
      "공유 보고서 비식별 증거 목록 응답 형식이 올바르지 않습니다.",
    );
  }
  const count = integer(source.count, 0, 500, "비식별 증거 수");
  if (
    !Array.isArray(source.preparations) ||
    source.preparations.length !== count
  ) {
    throw new Error("비식별 증거 목록 수가 일치하지 않습니다.");
  }
  const preparations = source.preparations.map(
    parseReportDeidentifiedPreparation,
  );
  const incidentIds = preparations.map((row) => row.incidentId);
  if (new Set(incidentIds).size !== incidentIds.length) {
    throw new Error("비식별 증거 목록에 중복 사건이 있습니다.");
  }
  return Object.freeze({
    schemaVersion: source.schemaVersion,
    projectId: identity(
      source.projectId,
      UUID_PATTERN,
      "프로젝트 ID",
    ),
    pipelineJobId: identity(
      source.pipelineJobId,
      UUID_PATTERN,
      "PipelineJob ID",
    ),
    analysisJobId: identity(
      source.analysisJobId,
      UUID_PATTERN,
      "분석 작업 ID",
    ),
    state: source.state,
    count,
    preparations: Object.freeze(preparations),
  });
}

export function parseReportDeidentifiedArtifact(
  value: unknown,
): ReportDeidentifiedArtifact {
  const source = object(value, "공유 보고서 비식별 이미지");
  exactKeys(
    source,
    ["media_type", "sha256", "bytes"],
    "공유 보고서 비식별 이미지",
  );
  if (
    source.media_type !== "image/jpeg" ||
    !(source.bytes instanceof Uint8Array) ||
    source.bytes.byteLength < 3 ||
    source.bytes.byteLength > 16 * 1024 * 1024 ||
    source.bytes[0] !== 0xff ||
    source.bytes[1] !== 0xd8 ||
    source.bytes[2] !== 0xff
  ) {
    throw new Error(
      "공유 보고서 비식별 이미지 응답 형식이 올바르지 않습니다.",
    );
  }
  return Object.freeze({
    mediaType: "image/jpeg",
    sha256: identity(
      source.sha256,
      SHA256_PATTERN,
      "비식별 이미지 해시",
    ),
    bytes: source.bytes,
  });
}

export function buildReportDeidentifiedReviewRequest(
  preparation: ReportDeidentifiedPreparation,
  approved: boolean,
): ReportDeidentifiedReviewRequest {
  if (
    preparation.state !== "awaiting_deidentification_review" ||
    preparation.automaticPrivacyVerification !== false ||
    typeof approved !== "boolean"
  ) {
    throw new Error("현재 비식별 증거에는 판정을 저장할 수 없습니다.");
  }
  return Object.freeze({
    projectId: preparation.projectId,
    jobId: preparation.pipelineJobId,
    incidentId: preparation.incidentId,
    derivativeId: preparation.derivativeId,
    expectedManifestSha256: preparation.manifestSha256,
    approved,
    explicitUserConfirmation: true,
  });
}

export function parseReportDeidentifiedReviewResponse(
  value: unknown,
): ReportDeidentifiedReviewResponse {
  const source = object(value, "공유 보고서 비식별 판정");
  exactKeys(
    source,
    [
      "schema_version",
      "preparation",
      "all_required_derivatives_verified",
      "pipeline_resume_requested",
      "pipeline_job",
    ],
    "공유 보고서 비식별 판정",
  );
  if (
    source.schema_version !==
      "trafficvisson.report-deidentified-evidence-review-response.v1" ||
    typeof source.all_required_derivatives_verified !== "boolean" ||
    typeof source.pipeline_resume_requested !== "boolean"
  ) {
    throw new Error(
      "공유 보고서 비식별 판정 응답 형식이 올바르지 않습니다.",
    );
  }
  const preparation = parseReportDeidentifiedPreparation(
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
      "공유 보고서 비식별 판정의 PipelineJob이 일치하지 않습니다.",
    );
  }
  return Object.freeze({
    schemaVersion: source.schema_version,
    preparation,
    allRequiredDerivativesVerified:
      source.all_required_derivatives_verified,
    pipelineResumeRequested: source.pipeline_resume_requested,
    pipelineJob,
  });
}

export function isReportDeidentifiedReviewInterruption(
  job: PipelineJob | null,
): job is ReportDeidentifiedReviewJob {
  return Boolean(
    job &&
      job.status === "blocked" &&
      job.currentStage === "derived_outputs" &&
      job.outcomeCode === "report_preview_blocked" &&
      job.errorCode === "deidentified_evidence_required" &&
      job.profileSnapshot.reportEvidenceMode === "shared_deidentified",
  );
}
