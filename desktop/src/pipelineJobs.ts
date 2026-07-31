import { useSyncExternalStore } from "react";

export const PIPELINE_STAGES = [
  "intake_validation",
  "sha256",
  "metadata_gps_preflight",
  "dense_index_2fps",
  "candidate_rescan_5_10fps",
  "detection_ocr",
  "sam2_segmentation_tracking",
  "temporal_merge",
  "similar_case_search",
  "vision_proposal",
  "question_queue",
  "derived_outputs",
] as const;

export type PipelineStageName = (typeof PIPELINE_STAGES)[number];
export type PipelineJobStatus =
  | "queued"
  | "running"
  | "cancelling"
  | "awaiting_review"
  | "blocked"
  | "failed"
  | "cancelled"
  | "completed";
export type PipelineStageStatus =
  | "pending"
  | "running"
  | "success"
  | "blocked"
  | "failed"
  | "skipped"
  | "cancelled";
export type PipelineOutcome =
  | "success"
  | "zero_detections"
  | "unusable_visual_evidence"
  | "model_unavailable"
  | "blocked_gpx_required"
  | "partial"
  | "report_preview_blocked"
  | "failed"
  | "cancelled";

export type PipelineProfile = {
  schemaVersion:
    | "trafficvisson.pipeline-profile.v1"
    | "trafficvisson.pipeline-profile.v2"
    | "trafficvisson.pipeline-profile.v3"
    | "trafficvisson.pipeline-profile.v4";
  projectId: string;
  questionPolicy: "learning_assist" | "full_review" | "quick_review";
  reviewMode: "precision" | "quick";
  labelConfirmationAuthority:
    | "human_review"
    | "structured_llm_training_assist";
  defaultTemplateId: string;
  defaultTemplateSha256: string;
  detectorPackId: string;
  ocrPackId: string;
  sam2PackId: string;
  reportProvider: "codex" | "claude" | "local" | "none";
  reportTransmissionConsent: boolean;
  templateSemanticMapId: string;
  templateSemanticMapSha256: string;
  templateSemanticMapReady: boolean;
  reportProviderReadyAtConfiguration: boolean;
  reportRouteName: string;
  reportInspectorName: string;
  reportEvidenceMode: "internal_original" | "shared_deidentified";
  reportInspectionDatePolicy: "source_filename_yyyymmdd_or_block";
  outputLocationId: string;
  candidateRescanFps: number;
  revision: number;
  updatedAt: string;
};

export type PipelineStage = {
  name: PipelineStageName;
  status: PipelineStageStatus;
  outcome: PipelineOutcome | null;
  attempts: number;
  startedAt: string | null;
  completedAt: string | null;
  errorCode: string | null;
  checkpoint: Readonly<Record<string, unknown>>;
  metrics: Readonly<Record<string, unknown>>;
};

export type PipelineJob = {
  jobId: string;
  projectId: string;
  snapshotId: string;
  fileName: string;
  idempotencyKey: string;
  inputFingerprint: string;
  profileSnapshot: PipelineProfile;
  capabilitySnapshot: Readonly<Record<string, unknown>>;
  status: PipelineJobStatus;
  currentStage: PipelineStageName;
  progressPercent: number;
  outcomeCode: PipelineOutcome | null;
  errorCode: string | null;
  retryCount: number;
  resumeCount: number;
  reviewCompletionId: string | null;
  stages: readonly PipelineStage[];
  provenance: Readonly<Record<string, unknown>>;
  createdAt: string;
  updatedAt: string;
  recoveredAfterRestart: boolean;
  recordSha256: string;
};

export type PipelineQuestionQueueBinding = {
  analysisJobId: string;
  sourceSha256: string;
  questionCount: number;
  queueRevision: string;
  surveyQaRequired: boolean;
  zeroCandidateSurvey: boolean;
};

export type PipelineStartResult = {
  job: PipelineJob;
  idempotentReplay: boolean;
};

type PipelineUiState = {
  projectId: string | null;
  job: PipelineJob | null;
  error: string | null;
  hydrated: boolean;
};

type PipelinePointer = {
  projectId: string;
  jobId: string;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{2,63}$/;
export const ZERO_CANDIDATE_PROMOTED_SURVEY_QUEUE_SCHEMA =
  "trafficvisson.zero-candidate-promoted-survey-queue.v1";
const STORAGE_KEY = "trafficvisson:pipeline-pointers:v1";
const JOB_STATUSES = new Set<PipelineJobStatus>([
  "queued",
  "running",
  "cancelling",
  "awaiting_review",
  "blocked",
  "failed",
  "cancelled",
  "completed",
]);
const STAGE_STATUSES = new Set<PipelineStageStatus>([
  "pending",
  "running",
  "success",
  "blocked",
  "failed",
  "skipped",
  "cancelled",
]);
const OUTCOMES = new Set<PipelineOutcome>([
  "success",
  "zero_detections",
  "unusable_visual_evidence",
  "model_unavailable",
  "blocked_gpx_required",
  "partial",
  "report_preview_blocked",
  "failed",
  "cancelled",
]);
const LEGACY_PROFILE_KEYS = [
  "schema_version",
  "project_id",
  "question_policy",
  "review_mode",
  "default_template_id",
  "default_template_sha256",
  "detector_pack_id",
  "ocr_pack_id",
  "sam2_pack_id",
  "report_provider",
  "output_location_id",
  "candidate_rescan_fps",
  "gps_policy",
  "bulk_frame_provider_policy",
  "revision",
  "updated_at",
] as const;
const PROFILE_V2_KEYS = [
  ...LEGACY_PROFILE_KEYS,
  "report_transmission_consent",
  "template_semantic_map_id",
  "template_semantic_map_sha256",
  "template_semantic_map_ready",
  "report_provider_ready_at_configuration",
] as const;
const PROFILE_V3_KEYS = [
  ...PROFILE_V2_KEYS,
  "report_route_name",
  "report_inspector_name",
  "report_evidence_mode",
  "report_inspection_date_policy",
] as const;
const PROFILE_KEYS = [
  ...PROFILE_V3_KEYS,
  "label_confirmation_authority",
] as const;
const STAGE_KEYS = [
  "name",
  "status",
  "outcome",
  "attempts",
  "started_at",
  "completed_at",
  "error_code",
  "checkpoint",
  "metrics",
] as const;
const JOB_KEYS = [
  "job_id",
  "project_id",
  "snapshot_id",
  "file_name",
  "idempotency_key",
  "input_fingerprint",
  "profile_snapshot",
  "capability_snapshot",
  "status",
  "current_stage",
  "progress_percent",
  "outcome_code",
  "error_code",
  "retry_count",
  "resume_count",
  "review_completion_id",
  "stages",
  "provenance",
  "created_at",
  "updated_at",
  "recovered_after_restart",
  "record_sha256",
] as const;
const listeners = new Set<() => void>();
let state: PipelineUiState = {
  projectId: null,
  job: null,
  error: null,
  hydrated: false,
};

function record(value: unknown, label: string): Record<string, unknown> {
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

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return (
    actual.length === expected.length &&
    actual.every((key, index) => key === expected[index])
  );
}

function string(
  value: unknown,
  label: string,
  pattern?: RegExp,
  maximum = 512,
) {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > maximum ||
    (pattern && !pattern.test(value))
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function reportContextString(
  value: unknown,
  label: string,
  maximum: number,
) {
  const result = string(value, label, undefined, maximum);
  if (
    result !== result.trim() ||
    /[\u0000-\u001f\u007f-\u009f]/u.test(result)
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return result;
}

function optionalString(
  value: unknown,
  label: string,
  pattern?: RegExp,
): string | null {
  if (value === null) return null;
  return string(value, label, pattern, 128);
}

function integer(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
) {
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function dateTime(value: unknown, label: string): string {
  const result = string(value, label, undefined, 64);
  if (!Number.isFinite(Date.parse(result))) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return result;
}

function optionalDateTime(value: unknown, label: string): string | null {
  return value === null ? null : dateTime(value, label);
}

function uuid(value: unknown, label: string) {
  return string(value, label, UUID_PATTERN, 36).toLowerCase();
}

function safeJson(
  value: unknown,
  label: string,
  depth = 0,
): unknown {
  if (depth > 8) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return value;
  }
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value)) {
    if (value.length > 10_000) {
      throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
    }
    return value.map((item) => safeJson(item, label, depth + 1));
  }
  const source = record(value, label);
  const keys = Object.keys(source);
  if (keys.length > 1_024) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  const result: Record<string, unknown> = {};
  for (const key of keys) {
    const isExplicitNoPathProof =
      key === "path_exposed" && source[key] === false;
    if (
      key.length < 1 ||
      key.length > 128 ||
      (!isExplicitNoPathProof &&
        /(?:^|_)(?:path|token|secret|password|credential|auth)(?:_|$)/i.test(
          key,
        ))
    ) {
      throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
    }
    result[key] = safeJson(source[key], label, depth + 1);
  }
  return result;
}

function safeRecord(
  value: unknown,
  label: string,
): Readonly<Record<string, unknown>> {
  return Object.freeze(
    safeJson(record(value, label), label) as Record<string, unknown>,
  );
}

export function parsePipelineProfile(value: unknown): PipelineProfile {
  const source = record(value, "파이프라인 프로필");
  const legacyShape = hasExactKeys(source, LEGACY_PROFILE_KEYS);
  const v2Shape = hasExactKeys(source, PROFILE_V2_KEYS);
  const v3Shape = hasExactKeys(source, PROFILE_V3_KEYS);
  const currentShape = hasExactKeys(source, PROFILE_KEYS);
  const schemaVersion = source.schema_version;
  if (
    (schemaVersion !== "trafficvisson.pipeline-profile.v1" &&
      schemaVersion !== "trafficvisson.pipeline-profile.v2" &&
      schemaVersion !== "trafficvisson.pipeline-profile.v3" &&
      schemaVersion !== "trafficvisson.pipeline-profile.v4") ||
    (!legacyShape && !v2Shape && !v3Shape && !currentShape) ||
    (schemaVersion === "trafficvisson.pipeline-profile.v2" &&
      !v2Shape &&
      !v3Shape &&
      !currentShape) ||
    (schemaVersion === "trafficvisson.pipeline-profile.v3" &&
      !v3Shape &&
      !currentShape) ||
    (schemaVersion === "trafficvisson.pipeline-profile.v4" && !currentShape)
  ) {
    throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
  }
  const questionPolicy = source.question_policy;
  if (
    questionPolicy !== "learning_assist" &&
    questionPolicy !== "full_review" &&
    questionPolicy !== "quick_review"
  ) {
    throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
  }
  const reviewMode = source.review_mode;
  if (reviewMode !== "precision" && reviewMode !== "quick") {
    throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
  }
  let labelConfirmationAuthority:
    | "human_review"
    | "structured_llm_training_assist" = "human_review";
  if (currentShape) {
    if (
      source.label_confirmation_authority !== "human_review" &&
      source.label_confirmation_authority !==
        "structured_llm_training_assist"
    ) {
      throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
    }
    labelConfirmationAuthority = source.label_confirmation_authority;
  }
  const reportProvider = source.report_provider;
  if (
    reportProvider !== "codex" &&
    reportProvider !== "claude" &&
    reportProvider !== "local" &&
    reportProvider !== "none"
  ) {
    throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
  }
  if (
    source.gps_policy !== "timed_track_required_for_route" ||
    source.bulk_frame_provider_policy !== "local_models_only"
  ) {
    throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
  }
  let reportTransmissionConsent = false;
  let templateSemanticMapId = "unconfigured";
  let templateSemanticMapSha256 = "0".repeat(64);
  let templateSemanticMapReady = false;
  let reportProviderReadyAtConfiguration = false;
  if (v2Shape || v3Shape || currentShape) {
    if (
      typeof source.report_transmission_consent !== "boolean" ||
      typeof source.template_semantic_map_ready !== "boolean" ||
      typeof source.report_provider_ready_at_configuration !== "boolean"
    ) {
      throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
    }
    reportTransmissionConsent = source.report_transmission_consent;
    templateSemanticMapId = string(
      source.template_semantic_map_id,
      "양식 의미맵 ID",
      IDENTIFIER_PATTERN,
      128,
    );
    templateSemanticMapSha256 = string(
      source.template_semantic_map_sha256,
      "양식 의미맵 해시",
      SHA256_PATTERN,
      64,
    );
    templateSemanticMapReady = source.template_semantic_map_ready;
    reportProviderReadyAtConfiguration =
      source.report_provider_ready_at_configuration;
  }
  let reportRouteName = "unconfigured";
  let reportInspectorName = "unconfigured";
  let reportEvidenceMode:
    | "internal_original"
    | "shared_deidentified" = "internal_original";
  const reportInspectionDatePolicy =
    "source_filename_yyyymmdd_or_block" as const;
  if (v3Shape || currentShape) {
    reportRouteName = reportContextString(
      source.report_route_name,
      "보고서 노선명",
      300,
    );
    reportInspectorName = reportContextString(
      source.report_inspector_name,
      "보고서 점검자명",
      120,
    );
    if (
      source.report_evidence_mode !== "internal_original" &&
      source.report_evidence_mode !== "shared_deidentified"
    ) {
      throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
    }
    reportEvidenceMode = source.report_evidence_mode;
    if (
      source.report_inspection_date_policy !==
      reportInspectionDatePolicy
    ) {
      throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
    }
  }
  if (
    (schemaVersion !== "trafficvisson.pipeline-profile.v4" &&
      currentShape &&
      labelConfirmationAuthority !== "human_review") ||
    (schemaVersion === "trafficvisson.pipeline-profile.v1" &&
      (v2Shape || v3Shape || currentShape) &&
      (reportTransmissionConsent ||
        templateSemanticMapId !== "unconfigured" ||
        templateSemanticMapSha256 !== "0".repeat(64) ||
        templateSemanticMapReady ||
        reportProviderReadyAtConfiguration)) ||
    ((schemaVersion === "trafficvisson.pipeline-profile.v2" ||
      schemaVersion === "trafficvisson.pipeline-profile.v3" ||
      schemaVersion === "trafficvisson.pipeline-profile.v4") &&
      templateSemanticMapReady &&
      (templateSemanticMapId === "unconfigured" ||
        templateSemanticMapSha256 === "0".repeat(64))) ||
    ((schemaVersion === "trafficvisson.pipeline-profile.v2" ||
      schemaVersion === "trafficvisson.pipeline-profile.v3" ||
      schemaVersion === "trafficvisson.pipeline-profile.v4") &&
      reportTransmissionConsent &&
      (reportProvider !== "codex" && reportProvider !== "claude")) ||
    ((schemaVersion === "trafficvisson.pipeline-profile.v2" ||
      schemaVersion === "trafficvisson.pipeline-profile.v3" ||
      schemaVersion === "trafficvisson.pipeline-profile.v4") &&
      reportTransmissionConsent &&
      (!templateSemanticMapReady ||
        !reportProviderReadyAtConfiguration)) ||
    ((schemaVersion === "trafficvisson.pipeline-profile.v1" ||
      schemaVersion === "trafficvisson.pipeline-profile.v2") &&
      (v3Shape || currentShape) &&
      (reportRouteName !== "unconfigured" ||
        reportInspectorName !== "unconfigured" ||
        reportEvidenceMode !== "internal_original"))
  ) {
    throw new Error("파이프라인 프로필 응답 형식이 올바르지 않습니다.");
  }
  return {
    schemaVersion,
    projectId: uuid(source.project_id, "프로젝트 ID"),
    questionPolicy,
    reviewMode,
    labelConfirmationAuthority,
    defaultTemplateId: string(
      source.default_template_id,
      "기본 양식 ID",
      IDENTIFIER_PATTERN,
      128,
    ),
    defaultTemplateSha256: string(
      source.default_template_sha256,
      "기본 양식 해시",
      SHA256_PATTERN,
      64,
    ),
    detectorPackId: string(
      source.detector_pack_id,
      "검지 모델팩 ID",
      IDENTIFIER_PATTERN,
      128,
    ),
    ocrPackId: string(
      source.ocr_pack_id,
      "OCR 모델팩 ID",
      IDENTIFIER_PATTERN,
      128,
    ),
    sam2PackId: string(
      source.sam2_pack_id,
      "SAM2 모델팩 ID",
      IDENTIFIER_PATTERN,
      128,
    ),
    reportProvider,
    reportTransmissionConsent,
    templateSemanticMapId,
    templateSemanticMapSha256,
    templateSemanticMapReady,
    reportProviderReadyAtConfiguration,
    reportRouteName,
    reportInspectorName,
    reportEvidenceMode,
    reportInspectionDatePolicy,
    outputLocationId: string(
      source.output_location_id,
      "결과 위치 ID",
      IDENTIFIER_PATTERN,
      128,
    ),
    candidateRescanFps: integer(
      source.candidate_rescan_fps,
      "후보 재분석 FPS",
      5,
      10,
    ),
    revision: integer(source.revision, "프로필 리비전", 1, 1_000_000),
    updatedAt: dateTime(source.updated_at, "프로필 갱신 시각"),
  };
}

function parseStage(
  value: unknown,
  expectedName: PipelineStageName,
): PipelineStage {
  const source = record(value, "파이프라인 단계");
  exactKeys(source, STAGE_KEYS, "파이프라인 단계");
  if (source.name !== expectedName || !PIPELINE_STAGES.includes(expectedName)) {
    throw new Error("파이프라인 단계 순서가 올바르지 않습니다.");
  }
  const status = source.status as PipelineStageStatus;
  if (!STAGE_STATUSES.has(status)) {
    throw new Error("파이프라인 단계 응답 형식이 올바르지 않습니다.");
  }
  const outcome = source.outcome as PipelineOutcome | null;
  if (outcome !== null && !OUTCOMES.has(outcome)) {
    throw new Error("파이프라인 단계 응답 형식이 올바르지 않습니다.");
  }
  return {
    name: expectedName,
    status,
    outcome,
    attempts: integer(source.attempts, "단계 시도 횟수", 0, 64),
    startedAt: optionalDateTime(source.started_at, "단계 시작 시각"),
    completedAt: optionalDateTime(source.completed_at, "단계 완료 시각"),
    errorCode: optionalString(
      source.error_code,
      "단계 오류 코드",
      ERROR_CODE_PATTERN,
    ),
    checkpoint: safeRecord(source.checkpoint, "단계 체크포인트"),
    metrics: safeRecord(source.metrics, "단계 지표"),
  };
}

export function parsePipelineJob(value: unknown): PipelineJob {
  const source = record(value, "파이프라인 작업");
  exactKeys(source, JOB_KEYS, "파이프라인 작업");
  const projectId = uuid(source.project_id, "프로젝트 ID");
  const profileSnapshot = parsePipelineProfile(source.profile_snapshot);
  if (profileSnapshot.projectId !== projectId) {
    throw new Error("파이프라인 프로필의 프로젝트가 일치하지 않습니다.");
  }
  const status = source.status as PipelineJobStatus;
  if (!JOB_STATUSES.has(status)) {
    throw new Error("파이프라인 작업 응답 형식이 올바르지 않습니다.");
  }
  const currentStage = source.current_stage as PipelineStageName;
  if (!PIPELINE_STAGES.includes(currentStage)) {
    throw new Error("파이프라인 작업 응답 형식이 올바르지 않습니다.");
  }
  const outcomeCode = source.outcome_code as PipelineOutcome | null;
  if (outcomeCode !== null && !OUTCOMES.has(outcomeCode)) {
    throw new Error("파이프라인 작업 응답 형식이 올바르지 않습니다.");
  }
  const rawStages = source.stages;
  if (!Array.isArray(rawStages) || rawStages.length !== 12) {
    throw new Error("파이프라인 단계 응답 형식이 올바르지 않습니다.");
  }
  const stages = PIPELINE_STAGES.map((name, index) =>
    parseStage(rawStages[index], name),
  );
  const progressPercent = integer(
    source.progress_percent,
    "파이프라인 진행률",
    0,
    100,
  );
  if (
    (status === "completed" &&
      (progressPercent !== 100 ||
        (outcomeCode !== "success" &&
          outcomeCode !== "zero_detections" &&
          outcomeCode !== "unusable_visual_evidence"))) ||
    (status === "blocked" &&
      outcomeCode !== "model_unavailable" &&
      outcomeCode !== "partial" &&
      outcomeCode !== "report_preview_blocked") ||
    (status === "blocked" &&
      (outcomeCode === "partial" ||
        outcomeCode === "report_preview_blocked") &&
      currentStage !== "derived_outputs") ||
    (status === "failed" && outcomeCode !== "failed") ||
    (status === "cancelled" && outcomeCode !== "cancelled") ||
    (status === "awaiting_review" && currentStage !== "question_queue")
  ) {
    throw new Error("파이프라인 상태와 결과가 일치하지 않습니다.");
  }
  return {
    jobId: uuid(source.job_id, "파이프라인 작업 ID"),
    projectId,
    snapshotId: uuid(source.snapshot_id, "미디어 스냅샷 ID"),
    fileName: string(source.file_name, "영상 파일명", undefined, 255),
    idempotencyKey: string(
      source.idempotency_key,
      "멱등 키",
      /^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/,
      128,
    ),
    inputFingerprint: string(
      source.input_fingerprint,
      "입력 지문",
      SHA256_PATTERN,
      64,
    ),
    profileSnapshot,
    capabilitySnapshot: safeRecord(
      source.capability_snapshot,
      "파이프라인 기능 스냅샷",
    ),
    status,
    currentStage,
    progressPercent,
    outcomeCode,
    errorCode: optionalString(
      source.error_code,
      "파이프라인 오류 코드",
      ERROR_CODE_PATTERN,
    ),
    retryCount: integer(source.retry_count, "재시도 횟수", 0, 5),
    resumeCount: integer(source.resume_count, "재개 횟수", 0, 5),
    reviewCompletionId: optionalString(
      source.review_completion_id,
      "검토 완료 ID",
      IDENTIFIER_PATTERN,
    ),
    stages: Object.freeze(stages),
    provenance: safeRecord(source.provenance, "파이프라인 provenance"),
    createdAt: dateTime(source.created_at, "파이프라인 생성 시각"),
    updatedAt: dateTime(source.updated_at, "파이프라인 갱신 시각"),
    recoveredAfterRestart:
      typeof source.recovered_after_restart === "boolean"
        ? source.recovered_after_restart
        : (() => {
            throw new Error("파이프라인 작업 응답 형식이 올바르지 않습니다.");
          })(),
    recordSha256: string(
      source.record_sha256,
      "파이프라인 레코드 해시",
      SHA256_PATTERN,
      64,
    ),
  };
}

export function parsePipelineStart(value: unknown): PipelineStartResult {
  const source = record(value, "파이프라인 시작");
  exactKeys(source, ["job", "idempotent_replay"], "파이프라인 시작");
  if (typeof source.idempotent_replay !== "boolean") {
    throw new Error("파이프라인 시작 응답 형식이 올바르지 않습니다.");
  }
  return {
    job: parsePipelineJob(source.job),
    idempotentReplay: source.idempotent_replay,
  };
}

export function parsePipelineList(value: unknown): PipelineJob[] {
  const source = record(value, "파이프라인 목록");
  exactKeys(source, ["jobs"], "파이프라인 목록");
  if (!Array.isArray(source.jobs) || source.jobs.length > 128) {
    throw new Error("파이프라인 목록 응답 형식이 올바르지 않습니다.");
  }
  return source.jobs.map(parsePipelineJob);
}

function parsePointer(value: unknown): PipelinePointer | null {
  try {
    const source = record(value, "파이프라인 포인터");
    exactKeys(source, ["projectId", "jobId"], "파이프라인 포인터");
    return {
      projectId: uuid(source.projectId, "프로젝트 ID"),
      jobId: uuid(source.jobId, "파이프라인 작업 ID"),
    };
  } catch {
    return null;
  }
}

function readPointers(): Record<string, PipelinePointer> {
  if (typeof window === "undefined") return {};
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}");
    const source = record(parsed, "파이프라인 포인터 목록");
    const result: Record<string, PipelinePointer> = {};
    for (const [key, value] of Object.entries(source).slice(0, 512)) {
      const pointer = parsePointer(value);
      if (pointer && key.toLowerCase() === pointer.projectId) {
        result[pointer.projectId] = pointer;
      }
    }
    return result;
  } catch {
    return {};
  }
}

function writePointers(pointers: Record<string, PipelinePointer>) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(pointers));
}

export function pipelinePointerForProject(
  projectId: string,
): PipelinePointer | null {
  const normalized = projectId.toLowerCase();
  if (!UUID_PATTERN.test(normalized)) return null;
  return readPointers()[normalized] ?? null;
}

function publish(next: PipelineUiState) {
  state = next;
  listeners.forEach((listener) => listener());
}

export function publishPipelineJob(job: PipelineJob) {
  const pointers = readPointers();
  pointers[job.projectId] = {
    projectId: job.projectId,
    jobId: job.jobId,
  };
  writePointers(pointers);
  publish({
    projectId: job.projectId,
    job,
    error: null,
    hydrated: true,
  });
}

export function publishPipelineError(projectId: string, error: string) {
  publish({
    projectId: projectId.toLowerCase(),
    job: state.projectId === projectId.toLowerCase() ? state.job : null,
    error: error.slice(0, 500),
    hydrated: true,
  });
}

export function markPipelineHydrated(
  projectId: string,
  job: PipelineJob | null,
) {
  if (job) {
    publishPipelineJob(job);
    return;
  }
  publish({
    projectId: projectId.toLowerCase(),
    job: null,
    error: null,
    hydrated: true,
  });
}

export function resetPipelineUiState() {
  publish({
    projectId: null,
    job: null,
    error: null,
    hydrated: false,
  });
}

export function getPipelineUiState() {
  return state;
}

export function subscribePipelineUi(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function usePipelineUi() {
  return useSyncExternalStore(
    subscribePipelineUi,
    getPipelineUiState,
    getPipelineUiState,
  );
}

export function pipelineQuestionQueueBinding(
  job: PipelineJob,
): PipelineQuestionQueueBinding | null {
  if (
    job.status !== "awaiting_review" ||
    job.currentStage !== "question_queue"
  ) {
    return null;
  }
  const stage = job.stages.find(({ name }) => name === "question_queue");
  if (!stage || stage.status !== "success") return null;
  const analysisJobId = stage.checkpoint.analysis_job_id;
  const sourceSha256 = stage.checkpoint.source_sha256;
  const questionCount = stage.checkpoint.question_count;
  const queueRevision = stage.checkpoint.question_queue_revision;
  const zeroCandidateSurvey =
    stage.checkpoint.schema_version ===
      ZERO_CANDIDATE_PROMOTED_SURVEY_QUEUE_SCHEMA &&
    questionCount === 0 &&
    Array.isArray(stage.checkpoint.question_candidate_ids) &&
    stage.checkpoint.question_candidate_ids.length === 0;
  const rescanStage = job.stages.find(
    ({ name }) => name === "candidate_rescan_5_10fps",
  );
  const denseDiscovery =
    rescanStage?.checkpoint.dense_candidate_discovery;
  const providerStates =
    denseDiscovery &&
    typeof denseDiscovery === "object" &&
    !Array.isArray(denseDiscovery)
      ? (denseDiscovery as Record<string, unknown>).provider_states
      : null;
  const promotedSurveyProviders = Array.isArray(providerStates)
    ? providerStates.filter(
        (value) =>
          value !== null &&
          typeof value === "object" &&
          !Array.isArray(value) &&
          (value as Record<string, unknown>).provider ===
            "promoted_project_model" &&
          (value as Record<string, unknown>).state === "ready" &&
          (value as Record<string, unknown>).independent_survey_verified ===
            true,
      )
    : [];
  const surveyQaRequired =
    promotedSurveyProviders.length === 1 || zeroCandidateSurvey;
  if (
    typeof analysisJobId !== "string" ||
    !UUID_PATTERN.test(analysisJobId.toLowerCase()) ||
    typeof sourceSha256 !== "string" ||
    !SHA256_PATTERN.test(sourceSha256) ||
    typeof questionCount !== "number" ||
    !Number.isInteger(questionCount) ||
    (questionCount < 1 && !zeroCandidateSurvey) ||
    questionCount > 100_000 ||
    typeof queueRevision !== "string" ||
    !IDENTIFIER_PATTERN.test(queueRevision)
  ) {
    return null;
  }
  return {
    analysisJobId: analysisJobId.toLowerCase(),
    sourceSha256,
    questionCount,
    queueRevision,
    surveyQaRequired,
    zeroCandidateSurvey,
  };
}

export function pipelineStageLabel(stage: PipelineStageName) {
  return {
    intake_validation: "원본 안전 검사",
    sha256: "SHA-256 무결성",
    metadata_gps_preflight: "메타데이터·GPS 사전점검",
    dense_index_2fps: "기본 2fps 프레임 인덱스",
    candidate_rescan_5_10fps: "후보 구간 5~10fps 재분석",
    detection_ocr: "도로 검지·한국어 OCR",
    sam2_segmentation_tracking: "SAM2 마스크·추적",
    temporal_merge: "동일 사건 시간축 병합",
    similar_case_search: "3·4분기 유사 사례 검색",
    vision_proposal: "구조화 AI 판정 제안",
    question_queue: "질문 큐 준비",
    derived_outputs: "지도·CSV·증거·보고서 초안",
  }[stage];
}

export function pipelineStatusLabel(job: PipelineJob) {
  if (job.status === "awaiting_review") return "질문 검토 대기";
  if (job.status === "blocked") {
    return job.outcomeCode === "partial" ||
      job.outcomeCode === "report_preview_blocked"
      ? "CSV·증거 저장 완료 · 보고서 준비 대기"
      : "모델팩 준비 필요";
  }
  if (job.status === "failed") return "단계 실패";
  if (job.status === "cancelled") return "사용자 취소";
  if (job.status === "completed") {
    if (job.outcomeCode === "zero_detections") {
      return "검지 0건 · 정상 완료";
    }
    if (job.outcomeCode === "unusable_visual_evidence") {
      return "판정 불가 영상 · 재촬영 필요";
    }
    return "파이프라인 완료";
  }
  if (job.status === "cancelling") return "취소 처리 중";
  if (job.status === "queued") return "대기열";
  return pipelineStageLabel(job.currentStage);
}
