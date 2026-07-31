export const PROJECT_MODEL_ISSUE_TYPES = [
  "pothole",
  "road_surface_damage",
  "road_debris",
  "median_barrier_damage",
  "sign_damage",
  "sign_vegetation_occlusion",
  "roadside_mowing_required",
  "guide_sign_identity",
  "tunnel_identity",
  "tunnel_name_occlusion",
  "lane_marking_faded",
  "colored_guidance_faded",
] as const;

export type ProjectModelIssueType =
  (typeof PROJECT_MODEL_ISSUE_TYPES)[number];
export type ProjectModelTrainingStatus =
  | "queued"
  | "running"
  | "interrupted"
  | "cancelled"
  | "succeeded"
  | "failed"
  | "blocked";
export type ProjectModelVersionState =
  | "candidate"
  | "active"
  | "previous"
  | "rejected"
  | "archived";
export const PROJECT_MODEL_FORMATS = [
  "trafficvisson.visual-embedding-centroid-head.v1",
  "trafficvisson.local-object-detector.v1",
] as const;
export type ProjectModelFormat = (typeof PROJECT_MODEL_FORMATS)[number];

export type ProjectModelBlocker = Readonly<{
  code: string;
  issueType: ProjectModelIssueType | null;
  observed: number | null;
  required: number | null;
}>;

export type ProjectModelTrainingProgress = Readonly<{
  stage: string;
  attemptNumber: number;
  updatedAt: string;
  epoch: number | null;
  epochs: number | null;
  current: number | null;
  total: number | null;
  loss: number | null;
}>;

export type ProjectModelTrainingCheckpoint = Readonly<{
  attemptNumber: number;
  relativePath: string;
  sha256: string;
  sizeBytes: number;
  modifiedTimeNs: string;
  requestManifestSha256: string;
  createdAt: string;
}>;

export type ProjectModelTrainingJob = Readonly<{
  trainingJobId: string;
  projectId: string;
  datasetVersionId: string;
  datasetHash: string;
  baseModelVersion: string;
  requiredIssueTypes: readonly ProjectModelIssueType[];
  currentConfirmedLabelCount: number;
  approvedReferenceTrainingMemberCount: number;
  approvedReferenceTrainingEventCount: number;
  trainingGateMemberCount: number;
  currentTrainMemberCount: number;
  currentValidationMemberCount: number;
  fixedValidationSha256: string;
  fixedValidationMembershipSha256: string;
  fixedValidationSetVersion: number;
  fixedValidationSetSchemaVersion:
    | "trafficvisson.project-fixed-validation.v1"
    | "trafficvisson.project-fixed-validation.v2";
  fixedValidationParentVersion: number | null;
  fixedValidationExtensionApprovalSha256: string | null;
  validationSampleCounts: Readonly<
    Partial<Record<ProjectModelIssueType, number>>
  >;
  auxiliaryReferenceCount: number;
  trainerId: string;
  trainerVersion: string;
  trainerAvailable: boolean;
  trainerTestOnly: boolean;
  status: ProjectModelTrainingStatus;
  attemptCount: number;
  blockers: readonly ProjectModelBlocker[];
  error: Readonly<{ code: string; message: string }> | null;
  progress: ProjectModelTrainingProgress;
  checkpoint: ProjectModelTrainingCheckpoint | null;
  cancelRequested: boolean;
  resumeCount: number;
  recoveryCount: number;
  modelVersionId: string | null;
  createdAt: string;
  updatedAt: string;
  idempotentReplay: boolean;
}>;

export type ProjectModelVersion = Readonly<{
  modelVersionId: string;
  projectId: string;
  trainingJobId: string;
  modelVersion: string;
  modelFormat: ProjectModelFormat;
  baseModelVersion: string;
  datasetVersionId: string;
  datasetHash: string;
  fixedValidationSha256: string;
  fixedValidationMembershipSha256: string;
  fixedValidationSetVersion: number;
  trainingAnalysisJobIds: readonly string[];
  fixedValidationAnalysisJobIds: readonly string[];
  sourceAnalysisLineageSha256: string;
  primaryArtifactSha256: string;
  artifactManifestSha256: string;
  artifactFileCount: number;
  artifactTotalSizeBytes: number;
  trainerId: string;
  trainerVersion: string;
  trainerTestOnly: boolean;
  validationDecision: "eligible" | "rejected";
  promotionDecision: "eligible" | "rejected";
  promotionReasons: readonly string[];
  auxiliaryReferenceCount: number;
  state: ProjectModelVersionState;
  automaticRejection: boolean;
  userPromotionRequired: boolean;
  createdAt: string;
  updatedAt: string;
}>;

export type ProjectModelState = Readonly<{
  projectId: string;
  revision: number;
  activeModelId: string | null;
  activeModelVersion: string;
  candidateModelId: string | null;
  candidateModelVersion: string | null;
  previousModelId: string | null;
  previousModelVersion: string | null;
  updatedAt: string;
}>;

export type ProjectFixedValidationVersion = Readonly<{
  schemaVersion:
    | "trafficvisson.project-fixed-validation.v1"
    | "trafficvisson.project-fixed-validation.v2";
  projectId: string;
  validationSetVersion: number;
  parentValidationSetVersion: number | null;
  parentMembershipSha256: string | null;
  parentFixedValidationSha256: string | null;
  originDatasetVersionId: string;
  requiredIssueTypes: readonly ProjectModelIssueType[];
  addedIssueTypes: readonly ProjectModelIssueType[];
  addedValidationAnalysisJobIds: readonly string[];
  validationSampleCounts: Readonly<
    Partial<Record<ProjectModelIssueType, number>>
  >;
  extensionPositiveSampleCounts: Readonly<
    Partial<Record<ProjectModelIssueType, number>>
  >;
  membershipSha256: string;
  fixedValidationSha256: string;
  extensionApprovalSha256: string | null;
  minimumPositiveSamplesPerType: 30;
  createdAt: string;
}>;

export type ProjectFixedValidationExtensionApproval = Readonly<{
  schemaVersion:
    "trafficvisson.fixed-validation-extension-approval.v1";
  expectedParentVersion: number;
  expectedParentMembershipSha256: string;
  expectedParentFixedValidationSha256: string;
  addedIssueTypes: readonly ProjectModelIssueType[];
  addedValidationAnalysisJobIds: readonly string[];
  userConfirmed: true;
}>;

export type ProjectModelSurveyQaSummary = Readonly<{
  surveyModelQaSha256: string;
  pipelineJobId: string;
  analysisJobId: string;
  modelVersionId: string;
  datasetVersionId: string;
  qaScopeSha256: string;
  modelCandidateCount: number;
  userDiscoveredCandidateCount: number;
  otherDetectorCandidateCount: number;
  resolvedTrainingFeedbackCount: number;
  truePositiveCount: number;
  falsePositiveCount: number;
  falseNegativeCount: number;
  heldCount: number;
  driftAssessed: boolean;
  baselineSurveyModelQaSha256: string | null;
}>;

export type ProjectModelIterationSummary = Readonly<{
  modelIterationSha256: string;
  parentIterationSha256: string | null;
  surveyModelQaSha256: string;
  parentModelVersionId: string;
  parentDatasetVersionId: string;
  nextDatasetVersionId: string;
  feedbackAnnotationCount: number;
  auditOnlyMemberCount: number;
  fixedValidationSha256: string;
  nextTrainingRequiresUserConfirmation: true;
}>;

export type ProjectModelIterationLedger = Readonly<{
  surveyQa: readonly ProjectModelSurveyQaSummary[];
  iterations: readonly ProjectModelIterationSummary[];
}>;

export type ProjectModelLifecycleSnapshot = Readonly<{
  trainingJobs: readonly ProjectModelTrainingJob[];
  modelVersions: readonly ProjectModelVersion[];
  fixedValidationVersions: readonly ProjectFixedValidationVersion[];
  modelIterations: ProjectModelIterationLedger;
  state: ProjectModelState | null;
}>;

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{0,127}$/;
const IDEMPOTENCY_PATTERN =
  /^[A-Za-z0-9][A-Za-z0-9._:@-]{7,127}$/;
const ISSUE_TYPE_SET = new Set<string>(PROJECT_MODEL_ISSUE_TYPES);
const TRAINING_STATUS_SET = new Set<ProjectModelTrainingStatus>([
  "queued",
  "running",
  "interrupted",
  "cancelled",
  "succeeded",
  "failed",
  "blocked",
]);
const MODEL_STATE_SET = new Set<ProjectModelVersionState>([
  "candidate",
  "active",
  "previous",
  "rejected",
  "archived",
]);
const MODEL_FORMAT_SET = new Set<ProjectModelFormat>(
  PROJECT_MODEL_FORMATS,
);
const TRAINING_JOB_KEYS = [
  "schema_version",
  "training_job_id",
  "project_id",
  "idempotency_key",
  "request_sha256",
  "dataset_version_id",
  "dataset_hash",
  "base_model_version",
  "required_issue_types",
  "current_confirmed_label_count",
  "approved_reference_training_member_count",
  "approved_reference_training_event_count",
  "training_gate_member_count",
  "current_train_member_count",
  "current_validation_member_count",
  "fixed_validation_sha256",
  "fixed_validation_membership_sha256",
  "fixed_validation_set_version",
  "fixed_validation_set_schema_version",
  "fixed_validation_parent_version",
  "fixed_validation_extension_approval_sha256",
  "validation_sample_counts",
  "auxiliary_reference_count",
  "approved_references_count_toward_training_gate",
  "auxiliary_references_count_toward_validation",
  "trainer_id",
  "trainer_version",
  "trainer_available",
  "trainer_test_only",
  "status",
  "attempt_count",
  "blockers",
  "error",
  "progress",
  "checkpoint",
  "cancel_requested",
  "resume_count",
  "recovery_count",
  "model_version_id",
  "automatic_training_started",
  "user_confirmed_start",
  "created_at",
  "updated_at",
  "idempotent_replay",
] as const;
const MODEL_VERSION_KEYS = [
  "schema_version",
  "model_version_id",
  "project_id",
  "training_job_id",
  "model_version",
  "model_format",
  "base_model_version",
  "dataset_version_id",
  "dataset_hash",
  "fixed_validation_sha256",
  "fixed_validation_membership_sha256",
  "fixed_validation_set_version",
  "training_analysis_job_ids",
  "fixed_validation_analysis_job_ids",
  "source_analysis_lineage_sha256",
  "primary_artifact_sha256",
  "artifact_manifest_sha256",
  "artifact_file_count",
  "artifact_total_size_bytes",
  "trainer_id",
  "trainer_version",
  "trainer_test_only",
  "promotion_review",
  "validation_decision",
  "auxiliary_reference_count",
  "auxiliary_reference_manifest_sha256",
  "state",
  "automatic_promotion",
  "automatic_rejection",
  "user_promotion_required",
  "created_at",
  "updated_at",
] as const;
const MODEL_STATE_KEYS = [
  "schema_version",
  "project_id",
  "revision",
  "active_model_id",
  "active_model_version",
  "candidate_model_id",
  "candidate_model_version",
  "previous_model_id",
  "previous_model_version",
  "automatic_promotion",
  "updated_at",
] as const;

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
  label: string,
) {
  const actual = Object.keys(value).sort();
  const expected = [...expectedKeys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
}

function text(
  value: unknown,
  label: string,
  pattern?: RegExp,
  maximum = 512,
) {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > maximum ||
    value !== value.trim() ||
    value.includes("\0") ||
    (pattern && !pattern.test(value))
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function uuid(value: unknown, label: string) {
  return text(value, label, UUID_PATTERN, 36).toLowerCase();
}

function integer(
  value: unknown,
  label: string,
  minimum = 0,
  maximum = Number.MAX_SAFE_INTEGER,
) {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function boolean(value: unknown, label: string) {
  if (typeof value !== "boolean") {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function dateTime(value: unknown, label: string) {
  const result = text(value, label, undefined, 64);
  if (!Number.isFinite(Date.parse(result))) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return result;
}

function optionalUuid(value: unknown, label: string): string | null {
  return value === null ? null : uuid(value, label);
}

function optionalIdentifier(
  value: unknown,
  label: string,
): string | null {
  return value === null
    ? null
    : text(value, label, IDENTIFIER_PATTERN, 128);
}

function issueType(value: unknown, label: string): ProjectModelIssueType {
  if (typeof value !== "string" || !ISSUE_TYPE_SET.has(value)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value as ProjectModelIssueType;
}

function parseBlocker(value: unknown): ProjectModelBlocker {
  const source = record(value, "학습 차단 사유");
  const allowedKeys = new Set([
    "code",
    "issue_type",
    "observed",
    "required",
    "trainer_id",
    "candidate_model_id",
  ]);
  if (
    Object.keys(source).length < 1 ||
    Object.keys(source).some((key) => !allowedKeys.has(key))
  ) {
    throw new Error("학습 차단 사유 응답 형식이 올바르지 않습니다.");
  }
  return Object.freeze({
    code: text(source.code, "학습 차단 코드", ERROR_CODE_PATTERN, 128),
    issueType:
      source.issue_type === undefined
        ? null
        : issueType(source.issue_type, "학습 차단 유형"),
    observed:
      source.observed === undefined
        ? null
        : integer(source.observed, "학습 차단 현재값"),
    required:
      source.required === undefined
        ? null
        : integer(source.required, "학습 차단 기준값"),
  });
}

function parseErrorState(
  value: unknown,
): Readonly<{ code: string; message: string }> | null {
  if (value === null) return null;
  const source = record(value, "학습 오류");
  exactKeys(source, ["code", "message"], "학습 오류");
  return Object.freeze({
    code: text(source.code, "학습 오류 코드", ERROR_CODE_PATTERN, 128),
    message: text(source.message, "학습 오류 메시지", undefined, 1_000),
  });
}

function nullableInteger(
  value: unknown,
  label: string,
  minimum = 0,
  maximum = 10_000_000,
) {
  return value === null
    ? null
    : integer(value, label, minimum, maximum);
}

function parseTrainingProgress(
  value: unknown,
): ProjectModelTrainingProgress {
  const source = record(value, "학습 진행률");
  exactKeys(
    source,
    [
      "stage",
      "attempt_number",
      "updated_at",
      "epoch",
      "epochs",
      "current",
      "total",
      "loss",
    ],
    "학습 진행률",
  );
  const epoch = nullableInteger(source.epoch, "학습 epoch");
  const epochs = nullableInteger(source.epochs, "전체 epoch");
  const current = nullableInteger(source.current, "학습 현재 진행값");
  const total = nullableInteger(source.total, "학습 전체 진행값");
  if (
    (epoch !== null && epochs !== null && epoch > epochs) ||
    (current !== null && total !== null && current > total)
  ) {
    throw new Error("학습 진행률이 전체 범위를 벗어났습니다.");
  }
  let loss: number | null = null;
  if (source.loss !== null) {
    if (
      typeof source.loss !== "number" ||
      !Number.isFinite(source.loss)
    ) {
      throw new Error("학습 손실값 응답 형식이 올바르지 않습니다.");
    }
    loss = source.loss;
  }
  return Object.freeze({
    stage: text(source.stage, "학습 진행 단계", IDENTIFIER_PATTERN, 128),
    attemptNumber: integer(
      source.attempt_number,
      "학습 진행 시도 횟수",
      0,
      1_000,
    ),
    updatedAt: dateTime(source.updated_at, "학습 진행 갱신 시각"),
    epoch,
    epochs,
    current,
    total,
    loss,
  });
}

function parseTrainingCheckpoint(
  value: unknown,
): ProjectModelTrainingCheckpoint | null {
  if (value === null) return null;
  const source = record(value, "학습 체크포인트");
  exactKeys(
    source,
    [
      "schema_version",
      "attempt_number",
      "relative_path",
      "sha256",
      "size_bytes",
      "modified_time_ns",
      "request_manifest_sha256",
      "created_at",
    ],
    "학습 체크포인트",
  );
  if (
    source.schema_version !==
    "trafficvisson.project-model-training-checkpoint.v1"
  ) {
    throw new Error("학습 체크포인트 스키마가 올바르지 않습니다.");
  }
  const relativePath = text(
    source.relative_path,
    "학습 체크포인트 상대 경로",
    /^[A-Za-z0-9._/-]+$/,
    512,
  );
  if (
    relativePath.startsWith("/") ||
    relativePath
      .split("/")
      .some((part) => part === "" || part === "." || part === "..")
  ) {
    throw new Error("학습 체크포인트 상대 경로가 올바르지 않습니다.");
  }
  return Object.freeze({
    attemptNumber: integer(
      source.attempt_number,
      "학습 체크포인트 시도 횟수",
      1,
      1_000,
    ),
    relativePath,
    sha256: text(
      source.sha256,
      "학습 체크포인트 해시",
      SHA256_PATTERN,
      64,
    ),
    sizeBytes: integer(
      source.size_bytes,
      "학습 체크포인트 크기",
      1,
      2 * 1024 * 1024 * 1024,
    ),
    modifiedTimeNs: text(
      source.modified_time_ns,
      "학습 체크포인트 수정 시각",
      /^[0-9]{1,32}$/,
      32,
    ),
    requestManifestSha256: text(
      source.request_manifest_sha256,
      "학습 요청 체크포인트 해시",
      SHA256_PATTERN,
      64,
    ),
    createdAt: dateTime(
      source.created_at,
      "학습 체크포인트 생성 시각",
    ),
  });
}

export function parseProjectModelTrainingJob(
  value: unknown,
  expectedProjectId?: string,
): ProjectModelTrainingJob {
  const source = record(value, "프로젝트 학습 작업");
  exactKeys(source, TRAINING_JOB_KEYS, "프로젝트 학습 작업");
  if (
    source.schema_version !== "trafficvisson.training-job.v1" ||
    source.automatic_training_started !== false ||
    source.user_confirmed_start !== true ||
    source.approved_references_count_toward_training_gate !== false ||
    source.auxiliary_references_count_toward_validation !== false
  ) {
    throw new Error("프로젝트 학습 작업 응답 형식이 올바르지 않습니다.");
  }
  const projectId = uuid(source.project_id, "프로젝트 ID");
  if (expectedProjectId && projectId !== expectedProjectId.toLowerCase()) {
    throw new Error("프로젝트 학습 작업의 프로젝트가 일치하지 않습니다.");
  }
  if (
    !Array.isArray(source.required_issue_types) ||
    source.required_issue_types.length < 1 ||
    source.required_issue_types.length > 12
  ) {
    throw new Error("프로젝트 학습 유형 응답 형식이 올바르지 않습니다.");
  }
  const requiredIssueTypes = source.required_issue_types.map((item, index) =>
    issueType(item, `프로젝트 학습 유형 ${index + 1}`),
  );
  if (new Set(requiredIssueTypes).size !== requiredIssueTypes.length) {
    throw new Error("프로젝트 학습 유형이 중복되었습니다.");
  }
  const rawCounts = record(
    source.validation_sample_counts,
    "고정 검증 표본 분포",
  );
  const validationSampleCounts: Partial<
    Record<ProjectModelIssueType, number>
  > = {};
  for (const [key, count] of Object.entries(rawCounts)) {
    const canonical = issueType(key, "고정 검증 유형");
    validationSampleCounts[canonical] = integer(
      count,
      "고정 검증 표본 수",
    );
  }
  const fixedValidationSetVersion = integer(
    source.fixed_validation_set_version,
    "고정 검증 세트 버전",
    1,
  );
  const fixedValidationSetSchemaVersion =
    source.fixed_validation_set_schema_version;
  if (
    fixedValidationSetSchemaVersion !==
      "trafficvisson.project-fixed-validation.v1" &&
    fixedValidationSetSchemaVersion !==
      "trafficvisson.project-fixed-validation.v2"
  ) {
    throw new Error("고정 검증 세트 스키마가 올바르지 않습니다.");
  }
  const fixedValidationParentVersion =
    source.fixed_validation_parent_version === null
      ? null
      : integer(
          source.fixed_validation_parent_version,
          "부모 고정 검증 세트 버전",
          1,
        );
  const fixedValidationExtensionApprovalSha256 =
    source.fixed_validation_extension_approval_sha256 === null
      ? null
      : text(
          source.fixed_validation_extension_approval_sha256,
          "고정 검증 확장 승인 해시",
          SHA256_PATTERN,
          64,
        );
  if (
    (fixedValidationSetVersion === 1 &&
      (fixedValidationParentVersion !== null ||
        fixedValidationExtensionApprovalSha256 !== null)) ||
    (fixedValidationSetVersion > 1 &&
      (fixedValidationParentVersion !== fixedValidationSetVersion - 1 ||
        fixedValidationExtensionApprovalSha256 === null))
  ) {
    throw new Error("고정 검증 세트 계보가 올바르지 않습니다.");
  }
  const status = source.status as ProjectModelTrainingStatus;
  if (!TRAINING_STATUS_SET.has(status)) {
    throw new Error("프로젝트 학습 상태 응답 형식이 올바르지 않습니다.");
  }
  if (!Array.isArray(source.blockers) || source.blockers.length > 64) {
    throw new Error("프로젝트 학습 차단 사유 응답 형식이 올바르지 않습니다.");
  }
  const trainerAvailable = boolean(
    source.trainer_available,
    "학습기 준비 상태",
  );
  const blockers = source.blockers.map(parseBlocker);
  const attemptCount = integer(
    source.attempt_count,
    "학습 시도 횟수",
    0,
    1_000,
  );
  const progress = parseTrainingProgress(source.progress);
  const checkpoint = parseTrainingCheckpoint(source.checkpoint);
  const requestSha256 = text(
    source.request_sha256,
    "학습 요청 해시",
    SHA256_PATTERN,
    64,
  );
  const cancelRequested = boolean(
    source.cancel_requested,
    "학습 취소 요청 여부",
  );
  const resumeCount = integer(
    source.resume_count,
    "학습 재개 횟수",
    0,
    1_000,
  );
  const recoveryCount = integer(
    source.recovery_count,
    "학습 복구 횟수",
    0,
    1_000,
  );
  if (
    (status === "blocked" && blockers.length === 0) ||
    (status !== "blocked" && blockers.length > 0) ||
    (!trainerAvailable &&
      !blockers.some((blocker) => blocker.code === "trainer_unavailable"))
  ) {
    throw new Error("프로젝트 학습 상태와 차단 사유가 일치하지 않습니다.");
  }
  if (
    progress.attemptNumber > attemptCount ||
    (checkpoint !== null &&
      (checkpoint.attemptNumber > attemptCount ||
        checkpoint.requestManifestSha256 !== requestSha256)) ||
    (cancelRequested &&
      status !== "running" &&
      status !== "cancelled") ||
    (status === "cancelled" && !cancelRequested)
  ) {
    throw new Error("프로젝트 학습 실행 상태가 일치하지 않습니다.");
  }
  const modelVersionId = optionalUuid(
    source.model_version_id,
    "모델 버전 ID",
  );
  if ((status === "succeeded") !== (modelVersionId !== null)) {
    throw new Error("프로젝트 학습 결과가 상태와 일치하지 않습니다.");
  }
  const currentConfirmedLabelCount = integer(
    source.current_confirmed_label_count,
    "현재 확정 라벨 수",
  );
  const approvedReferenceTrainingMemberCount = integer(
    source.approved_reference_training_member_count,
    "승인 사례 학습 구성원 수",
  );
  const approvedReferenceTrainingEventCount = integer(
    source.approved_reference_training_event_count,
    "승인 사례 학습 사건 수",
  );
  const trainingGateMemberCount = integer(
    source.training_gate_member_count,
    "학습 기준 구성원 수",
  );
  const auxiliaryReferenceCount = integer(
    source.auxiliary_reference_count,
    "승인 사례 수",
  );
  if (
    approvedReferenceTrainingMemberCount !== auxiliaryReferenceCount ||
    approvedReferenceTrainingEventCount >
      approvedReferenceTrainingMemberCount ||
    trainingGateMemberCount !== currentConfirmedLabelCount
  ) {
    throw new Error("프로젝트 학습 구성원 집계가 일치하지 않습니다.");
  }
  return Object.freeze({
    trainingJobId: uuid(source.training_job_id, "학습 작업 ID"),
    projectId,
    datasetVersionId: uuid(
      source.dataset_version_id,
      "DatasetVersion ID",
    ),
    datasetHash: text(source.dataset_hash, "데이터셋 해시", SHA256_PATTERN, 64),
    baseModelVersion: text(
      source.base_model_version,
      "기반 모델 버전",
      IDENTIFIER_PATTERN,
      128,
    ),
    requiredIssueTypes: Object.freeze(requiredIssueTypes),
    currentConfirmedLabelCount,
    approvedReferenceTrainingMemberCount,
    approvedReferenceTrainingEventCount,
    trainingGateMemberCount,
    currentTrainMemberCount: integer(
      source.current_train_member_count,
      "현재 학습 구성원 수",
    ),
    currentValidationMemberCount: integer(
      source.current_validation_member_count,
      "현재 검증 구성원 수",
    ),
    fixedValidationSha256: text(
      source.fixed_validation_sha256,
      "고정 검증 해시",
      SHA256_PATTERN,
      64,
    ),
    fixedValidationMembershipSha256: text(
      source.fixed_validation_membership_sha256,
      "고정 검증 구성원 해시",
      SHA256_PATTERN,
      64,
    ),
    fixedValidationSetVersion,
    fixedValidationSetSchemaVersion,
    fixedValidationParentVersion,
    fixedValidationExtensionApprovalSha256,
    validationSampleCounts: Object.freeze(validationSampleCounts),
    auxiliaryReferenceCount,
    trainerId: text(
      source.trainer_id,
      "학습기 ID",
      IDENTIFIER_PATTERN,
      128,
    ),
    trainerVersion: text(
      source.trainer_version,
      "학습기 버전",
      IDENTIFIER_PATTERN,
      128,
    ),
    trainerAvailable,
    trainerTestOnly: boolean(
      source.trainer_test_only,
      "학습기 테스트 전용 여부",
    ),
    status,
    attemptCount,
    blockers: Object.freeze(blockers),
    error: parseErrorState(source.error),
    progress,
    checkpoint,
    cancelRequested,
    resumeCount,
    recoveryCount,
    modelVersionId,
    createdAt: dateTime(source.created_at, "학습 생성 시각"),
    updatedAt: dateTime(source.updated_at, "학습 갱신 시각"),
    idempotentReplay: boolean(
      source.idempotent_replay,
      "학습 멱등 재사용 여부",
    ),
  });
}

export function parseProjectModelTrainingJobList(
  value: unknown,
  expectedProjectId: string,
) {
  const source = record(value, "프로젝트 학습 작업 목록");
  exactKeys(
    source,
    ["schema_version", "project_id", "count", "jobs"],
    "프로젝트 학습 작업 목록",
  );
  if (
    source.schema_version !== "trafficvisson.training-job-list.v1" ||
    uuid(source.project_id, "프로젝트 ID") !== expectedProjectId.toLowerCase() ||
    !Array.isArray(source.jobs) ||
    source.jobs.length > 256 ||
    integer(source.count, "학습 작업 수", 0, 256) !== source.jobs.length
  ) {
    throw new Error("프로젝트 학습 작업 목록 응답 형식이 올바르지 않습니다.");
  }
  return Object.freeze(
    source.jobs.map((job) =>
      parseProjectModelTrainingJob(job, expectedProjectId),
    ),
  );
}

function parsePromotionReview(value: unknown) {
  const source = record(value, "모델 승격 검증");
  const decision = source.decision;
  if (
    (decision !== "eligible" && decision !== "rejected") ||
    source.auto_promotion_allowed !== false ||
    source.requires_user_review !== true ||
    !Array.isArray(source.reasons) ||
    source.reasons.length > 128
  ) {
    throw new Error("모델 승격 검증 응답 형식이 올바르지 않습니다.");
  }
  const reasons = source.reasons.map((value) => {
    const reason = record(value, "모델 승격 검증 사유");
    return text(reason.message, "모델 승격 검증 사유", undefined, 500);
  });
  return {
    decision,
    reasons: Object.freeze(reasons),
  } as const;
}

export function parseProjectModelVersion(
  value: unknown,
  expectedProjectId?: string,
): ProjectModelVersion {
  const source = record(value, "프로젝트 모델 버전");
  exactKeys(source, MODEL_VERSION_KEYS, "프로젝트 모델 버전");
  const modelFormat = source.model_format as ProjectModelFormat;
  if (
    source.schema_version !==
      "trafficvisson.project-model-version.v1" ||
    !MODEL_FORMAT_SET.has(modelFormat) ||
    source.automatic_promotion !== false
  ) {
    throw new Error("프로젝트 모델 버전 응답 형식이 올바르지 않습니다.");
  }
  const projectId = uuid(source.project_id, "프로젝트 ID");
  if (expectedProjectId && projectId !== expectedProjectId.toLowerCase()) {
    throw new Error("프로젝트 모델 버전의 프로젝트가 일치하지 않습니다.");
  }
  const versionState = source.state as ProjectModelVersionState;
  if (!MODEL_STATE_SET.has(versionState)) {
    throw new Error("프로젝트 모델 버전 상태가 올바르지 않습니다.");
  }
  const validationDecision = source.validation_decision;
  if (
    validationDecision !== "eligible" &&
    validationDecision !== "rejected"
  ) {
    throw new Error("프로젝트 모델 검증 결과가 올바르지 않습니다.");
  }
  const promotion = parsePromotionReview(source.promotion_review);
  if (promotion.decision !== validationDecision) {
    throw new Error("프로젝트 모델 검증 결과가 서로 일치하지 않습니다.");
  }
  const automaticRejection = boolean(
    source.automatic_rejection,
    "고정 검증 자동 거부 여부",
  );
  const userPromotionRequired = boolean(
    source.user_promotion_required,
    "사용자 승격 승인 필요 여부",
  );
  if (
    automaticRejection !== (validationDecision === "rejected") ||
    userPromotionRequired !== (validationDecision === "eligible")
  ) {
    throw new Error("프로젝트 모델 검증 전이 형식이 올바르지 않습니다.");
  }
  const parseAnalysisIds = (
    value: unknown,
    label: string,
  ): readonly string[] => {
    if (!Array.isArray(value) || value.length === 0 || value.length > 100_000) {
      throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
    }
    const ids = value.map((item) => uuid(item, label));
    if (
      new Set(ids).size !== ids.length ||
      ids.some((item, index) => index > 0 && ids[index - 1] >= item)
    ) {
      throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
    }
    return Object.freeze(ids);
  };
  const trainingAnalysisJobIds = parseAnalysisIds(
    source.training_analysis_job_ids,
    "학습 원본 분석 작업",
  );
  const fixedValidationAnalysisJobIds = parseAnalysisIds(
    source.fixed_validation_analysis_job_ids,
    "고정 검증 원본 분석 작업",
  );
  const trainingSet = new Set(trainingAnalysisJobIds);
  if (
    fixedValidationAnalysisJobIds.some((analysisId) =>
      trainingSet.has(analysisId),
    )
  ) {
    throw new Error("학습과 고정 검증 원본 영상이 겹칩니다.");
  }
  const fixedValidationSetVersion = integer(
    source.fixed_validation_set_version,
    "모델 고정 검증 세트 버전",
    1,
  );
  return Object.freeze({
    modelVersionId: uuid(source.model_version_id, "모델 버전 ID"),
    projectId,
    trainingJobId: uuid(source.training_job_id, "학습 작업 ID"),
    modelVersion: text(
      source.model_version,
      "모델 버전",
      IDENTIFIER_PATTERN,
      128,
    ),
    modelFormat,
    baseModelVersion: text(
      source.base_model_version,
      "기반 모델 버전",
      IDENTIFIER_PATTERN,
      128,
    ),
    datasetVersionId: uuid(
      source.dataset_version_id,
      "DatasetVersion ID",
    ),
    datasetHash: text(source.dataset_hash, "데이터셋 해시", SHA256_PATTERN, 64),
    fixedValidationSha256: text(
      source.fixed_validation_sha256,
      "고정 검증 해시",
      SHA256_PATTERN,
      64,
    ),
    fixedValidationMembershipSha256: text(
      source.fixed_validation_membership_sha256,
      "고정 검증 구성원 해시",
      SHA256_PATTERN,
      64,
    ),
    fixedValidationSetVersion,
    trainingAnalysisJobIds,
    fixedValidationAnalysisJobIds,
    sourceAnalysisLineageSha256: text(
      source.source_analysis_lineage_sha256,
      "원본 분석 계보 해시",
      SHA256_PATTERN,
      64,
    ),
    primaryArtifactSha256: text(
      source.primary_artifact_sha256,
      "모델 산출물 해시",
      SHA256_PATTERN,
      64,
    ),
    artifactManifestSha256: text(
      source.artifact_manifest_sha256,
      "모델 매니페스트 해시",
      SHA256_PATTERN,
      64,
    ),
    artifactFileCount: integer(
      source.artifact_file_count,
      "모델 산출물 파일 수",
      1,
      128,
    ),
    artifactTotalSizeBytes: integer(
      source.artifact_total_size_bytes,
      "모델 산출물 크기",
      1,
    ),
    trainerId: text(
      source.trainer_id,
      "학습기 ID",
      IDENTIFIER_PATTERN,
      128,
    ),
    trainerVersion: text(
      source.trainer_version,
      "학습기 버전",
      IDENTIFIER_PATTERN,
      128,
    ),
    trainerTestOnly: boolean(
      source.trainer_test_only,
      "학습기 테스트 전용 여부",
    ),
    validationDecision,
    promotionDecision: promotion.decision,
    promotionReasons: promotion.reasons,
    auxiliaryReferenceCount: integer(
      source.auxiliary_reference_count,
      "보조 승인 사례 수",
    ),
    state: versionState,
    automaticRejection,
    userPromotionRequired,
    createdAt: dateTime(source.created_at, "모델 생성 시각"),
    updatedAt: dateTime(source.updated_at, "모델 갱신 시각"),
  });
}

export function parseProjectModelVersionList(
  value: unknown,
  expectedProjectId: string,
) {
  const source = record(value, "프로젝트 모델 버전 목록");
  exactKeys(
    source,
    ["schema_version", "project_id", "count", "models"],
    "프로젝트 모델 버전 목록",
  );
  if (
    source.schema_version !== "trafficvisson.model-version-list.v1" ||
    uuid(source.project_id, "프로젝트 ID") !== expectedProjectId.toLowerCase() ||
    !Array.isArray(source.models) ||
    source.models.length > 256 ||
    integer(source.count, "모델 버전 수", 0, 256) !== source.models.length
  ) {
    throw new Error("프로젝트 모델 버전 목록 응답 형식이 올바르지 않습니다.");
  }
  return Object.freeze(
    source.models.map((model) =>
      parseProjectModelVersion(model, expectedProjectId),
    ),
  );
}

export function parseProjectModelState(
  value: unknown,
  expectedProjectId?: string,
): ProjectModelState {
  const source = record(value, "프로젝트 모델 상태");
  exactKeys(source, MODEL_STATE_KEYS, "프로젝트 모델 상태");
  if (
    source.schema_version !== "trafficvisson.project-model-state.v1" ||
    source.automatic_promotion !== false
  ) {
    throw new Error("프로젝트 모델 상태 응답 형식이 올바르지 않습니다.");
  }
  const projectId = uuid(source.project_id, "프로젝트 ID");
  if (expectedProjectId && projectId !== expectedProjectId.toLowerCase()) {
    throw new Error("프로젝트 모델 상태의 프로젝트가 일치하지 않습니다.");
  }
  const candidateModelId = optionalUuid(
    source.candidate_model_id,
    "후보 모델 ID",
  );
  const candidateModelVersion = optionalIdentifier(
    source.candidate_model_version,
    "후보 모델 버전",
  );
  const previousModelId = optionalUuid(
    source.previous_model_id,
    "이전 모델 ID",
  );
  const previousModelVersion = optionalIdentifier(
    source.previous_model_version,
    "이전 모델 버전",
  );
  if (
    (candidateModelId === null) !== (candidateModelVersion === null) ||
    (previousModelId === null) !== (previousModelVersion === null)
  ) {
    throw new Error("프로젝트 모델 상태의 모델 식별자가 일치하지 않습니다.");
  }
  return Object.freeze({
    projectId,
    revision: integer(source.revision, "모델 상태 리비전"),
    activeModelId: optionalUuid(source.active_model_id, "활성 모델 ID"),
    activeModelVersion: text(
      source.active_model_version,
      "활성 모델 버전",
      IDENTIFIER_PATTERN,
      128,
    ),
    candidateModelId,
    candidateModelVersion,
    previousModelId,
    previousModelVersion,
    updatedAt: dateTime(source.updated_at, "모델 상태 갱신 시각"),
  });
}

export function parseProjectModelTransition(
  value: unknown,
  expectedProjectId: string,
) {
  const source = record(value, "프로젝트 모델 전환");
  exactKeys(
    source,
    [
      "schema_version",
      "event_id",
      "project_id",
      "action",
      "target_model_id",
      "request_sha256",
      "user_confirmed",
      "automatic_transition",
      "before_state",
      "after_state",
      "created_at",
      "idempotent_replay",
    ],
    "프로젝트 모델 전환",
  );
  if (
    source.schema_version !== "trafficvisson.project-model-event.v1" ||
    (source.action !== "promote" &&
      source.action !== "reject" &&
      source.action !== "rollback") ||
    source.user_confirmed !== true ||
    source.automatic_transition !== false
  ) {
    throw new Error("프로젝트 모델 전환 응답 형식이 올바르지 않습니다.");
  }
  const projectId = uuid(source.project_id, "프로젝트 ID");
  if (projectId !== expectedProjectId.toLowerCase()) {
    throw new Error("프로젝트 모델 전환의 프로젝트가 일치하지 않습니다.");
  }
  return Object.freeze({
    action: source.action,
    targetModelId: optionalUuid(source.target_model_id, "대상 모델 ID"),
    beforeState: parseProjectModelState(source.before_state, projectId),
    afterState: parseProjectModelState(source.after_state, projectId),
    idempotentReplay: boolean(
      source.idempotent_replay,
      "모델 전환 멱등 재사용 여부",
    ),
  });
}

export function parseProjectFixedValidationVersion(
  value: unknown,
  expectedProjectId?: string,
): ProjectFixedValidationVersion {
  const source = record(value, "고정 검증 세트 버전");
  exactKeys(
    source,
    [
      "schema_version",
      "project_id",
      "validation_set_version",
      "parent_validation_set_version",
      "parent_membership_sha256",
      "parent_fixed_validation_sha256",
      "origin_dataset_version_id",
      "required_issue_types",
      "added_issue_types",
      "added_validation_analysis_job_ids",
      "validation_sample_counts",
      "extension_positive_sample_counts",
      "membership_sha256",
      "fixed_validation_sha256",
      "extension_approval_sha256",
      "minimum_positive_samples_per_type",
      "persisted",
      "created_at",
    ],
    "고정 검증 세트 버전",
  );
  const schemaVersion = source.schema_version;
  if (
    schemaVersion !== "trafficvisson.project-fixed-validation.v1" &&
    schemaVersion !== "trafficvisson.project-fixed-validation.v2"
  ) {
    throw new Error("고정 검증 세트 버전 응답 형식이 올바르지 않습니다.");
  }
  const projectId = uuid(source.project_id, "프로젝트 ID");
  if (expectedProjectId && projectId !== expectedProjectId.toLowerCase()) {
    throw new Error("고정 검증 세트의 프로젝트가 일치하지 않습니다.");
  }
  const parseIssueTypes = (
    raw: unknown,
    label: string,
  ): readonly ProjectModelIssueType[] => {
    if (!Array.isArray(raw) || raw.length < 1 || raw.length > 12) {
      throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
    }
    const parsed = raw.map((item, index) =>
      issueType(item, `${label} ${index + 1}`),
    );
    if (new Set(parsed).size !== parsed.length) {
      throw new Error(`${label}에 중복 항목이 있습니다.`);
    }
    return Object.freeze(parsed);
  };
  const requiredIssueTypes = parseIssueTypes(
    source.required_issue_types,
    "고정 검증 유형",
  );
  const addedIssueTypes = parseIssueTypes(
    source.added_issue_types,
    "추가 검증 유형",
  );
  const requiredSet = new Set(requiredIssueTypes);
  if (addedIssueTypes.some((item) => !requiredSet.has(item))) {
    throw new Error("추가 검증 유형이 전체 검증 유형과 일치하지 않습니다.");
  }
  if (
    !Array.isArray(source.added_validation_analysis_job_ids) ||
    source.added_validation_analysis_job_ids.length > 100_000
  ) {
    throw new Error("추가 검증 AnalysisJob 응답 형식이 올바르지 않습니다.");
  }
  const addedValidationAnalysisJobIds =
    source.added_validation_analysis_job_ids.map((item, index) =>
      uuid(item, `추가 검증 AnalysisJob ${index + 1}`),
    );
  if (
    addedValidationAnalysisJobIds.some(
      (item, index) =>
        index > 0 && addedValidationAnalysisJobIds[index - 1] >= item,
    )
  ) {
    throw new Error("추가 검증 AnalysisJob이 정렬·중복 검증에 실패했습니다.");
  }
  const parseCounts = (
    raw: unknown,
    label: string,
  ): Readonly<Partial<Record<ProjectModelIssueType, number>>> => {
    const counts = record(raw, label);
    const result: Partial<Record<ProjectModelIssueType, number>> = {};
    for (const [key, count] of Object.entries(counts)) {
      result[issueType(key, label)] = integer(count, label);
    }
    return Object.freeze(result);
  };
  const validationSampleCounts = parseCounts(
    source.validation_sample_counts,
    "고정 검증 양성 표본 수",
  );
  if (
    Object.keys(validationSampleCounts).length !==
      requiredIssueTypes.length ||
    requiredIssueTypes.some(
      (item) => validationSampleCounts[item] === undefined,
    )
  ) {
    throw new Error("고정 검증 표본 분포가 유형과 일치하지 않습니다.");
  }
  const extensionPositiveSampleCounts = parseCounts(
    source.extension_positive_sample_counts,
    "확장 양성 표본 수",
  );
  const validationSetVersion = integer(
    source.validation_set_version,
    "고정 검증 세트 버전",
    1,
  );
  const parentValidationSetVersion =
    source.parent_validation_set_version === null
      ? null
      : integer(
          source.parent_validation_set_version,
          "부모 고정 검증 세트 버전",
          1,
        );
  const optionalSha = (raw: unknown, label: string) =>
    raw === null ? null : text(raw, label, SHA256_PATTERN, 64);
  const parentMembershipSha256 = optionalSha(
    source.parent_membership_sha256,
    "부모 구성원 해시",
  );
  const parentFixedValidationSha256 = optionalSha(
    source.parent_fixed_validation_sha256,
    "부모 검증 해시",
  );
  const extensionApprovalSha256 = optionalSha(
    source.extension_approval_sha256,
    "확장 승인 해시",
  );
  if (
    source.minimum_positive_samples_per_type !== 30 ||
    source.persisted !== true ||
    (validationSetVersion === 1 &&
      (parentValidationSetVersion !== null ||
        parentMembershipSha256 !== null ||
        parentFixedValidationSha256 !== null ||
        extensionApprovalSha256 !== null ||
        addedValidationAnalysisJobIds.length !== 0 ||
        Object.keys(extensionPositiveSampleCounts).length !== 0)) ||
    (validationSetVersion > 1 &&
      (parentValidationSetVersion !== validationSetVersion - 1 ||
        parentMembershipSha256 === null ||
        parentFixedValidationSha256 === null ||
        extensionApprovalSha256 === null ||
        addedValidationAnalysisJobIds.length === 0 ||
        Object.keys(extensionPositiveSampleCounts).length !==
          addedIssueTypes.length ||
        addedIssueTypes.some(
          (item) => extensionPositiveSampleCounts[item] === undefined,
        )))
  ) {
    throw new Error("고정 검증 세트 계보 응답 형식이 올바르지 않습니다.");
  }
  return Object.freeze({
    schemaVersion,
    projectId,
    validationSetVersion,
    parentValidationSetVersion,
    parentMembershipSha256,
    parentFixedValidationSha256,
    originDatasetVersionId: uuid(
      source.origin_dataset_version_id,
      "원본 DatasetVersion ID",
    ),
    requiredIssueTypes,
    addedIssueTypes,
    addedValidationAnalysisJobIds: Object.freeze(
      addedValidationAnalysisJobIds,
    ),
    validationSampleCounts,
    extensionPositiveSampleCounts,
    membershipSha256: text(
      source.membership_sha256,
      "고정 검증 구성원 해시",
      SHA256_PATTERN,
      64,
    ),
    fixedValidationSha256: text(
      source.fixed_validation_sha256,
      "고정 검증 해시",
      SHA256_PATTERN,
      64,
    ),
    extensionApprovalSha256,
    minimumPositiveSamplesPerType: 30,
    createdAt: dateTime(source.created_at, "고정 검증 생성 시각"),
  });
}

export function parseProjectFixedValidationVersionList(
  value: unknown,
  expectedProjectId: string,
): readonly ProjectFixedValidationVersion[] {
  const source = record(value, "고정 검증 세트 이력");
  exactKeys(
    source,
    ["schema_version", "project_id", "count", "versions"],
    "고정 검증 세트 이력",
  );
  if (
    source.schema_version !==
      "trafficvisson.project-fixed-validation-list.v2" ||
    uuid(source.project_id, "프로젝트 ID") !==
      expectedProjectId.toLowerCase() ||
    !Array.isArray(source.versions) ||
    source.versions.length > 1_000 ||
    integer(source.count, "고정 검증 세트 버전 수", 0, 1_000) !==
      source.versions.length
  ) {
    throw new Error("고정 검증 세트 이력 응답 형식이 올바르지 않습니다.");
  }
  const versions = source.versions.map((item) =>
    parseProjectFixedValidationVersion(item, expectedProjectId),
  );
  if (
    versions.some(
      (item, index) => item.validationSetVersion !== index + 1,
    )
  ) {
    throw new Error("고정 검증 세트 이력이 연속적이지 않습니다.");
  }
  return Object.freeze(versions);
}

function rejectModelLedgerFilesystemFields(
  value: unknown,
  label: string,
  depth = 0,
) {
  if (depth > 24) {
    throw new Error(`${label} 응답 중첩이 너무 깊습니다.`);
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) =>
      rejectModelLedgerFilesystemFields(
        item,
        `${label}[${index}]`,
        depth + 1,
      ),
    );
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, item] of Object.entries(
    value as Record<string, unknown>,
  )) {
    if (
      key === "path" ||
      key.endsWith("_path") ||
      key.endsWith("_directory") ||
      key === "absolute_paths"
    ) {
      throw new Error(`${label}에 파일 경로 필드가 포함되었습니다.`);
    }
    rejectModelLedgerFilesystemFields(
      item,
      `${label}.${key}`,
      depth + 1,
    );
  }
}

export function parseProjectModelIterationLedger(
  value: unknown,
  expectedProjectId: string,
): ProjectModelIterationLedger {
  const source = record(value, "모델 반복 QA 원장");
  exactKeys(
    source,
    [
      "schema_version",
      "project_id",
      "survey_qa_count",
      "iteration_count",
      "survey_qa",
      "iterations",
    ],
    "모델 반복 QA 원장",
  );
  rejectModelLedgerFilesystemFields(source, "모델 반복 QA 원장");
  const projectId = uuid(source.project_id, "프로젝트 ID");
  if (
    source.schema_version !==
      "trafficvisson.model-iteration-ledger.v1" ||
    projectId !== expectedProjectId.toLowerCase() ||
    !Array.isArray(source.survey_qa) ||
    !Array.isArray(source.iterations) ||
    source.survey_qa.length > 10_000 ||
    source.iterations.length > 10_000 ||
    integer(
      source.survey_qa_count,
      "조사 QA 수",
      0,
      10_000,
    ) !== source.survey_qa.length ||
    integer(
      source.iteration_count,
      "모델 반복 수",
      0,
      10_000,
    ) !== source.iterations.length
  ) {
    throw new Error("모델 반복 QA 원장 응답 형식이 올바르지 않습니다.");
  }
  const surveyQa = source.survey_qa.map((raw, index) => {
    const item = record(raw, `조사 QA ${index + 1}`);
    exactKeys(
      item,
      [
        "schema_version",
        "survey_model_qa_sha256",
        "pipeline_job_id",
        "analysis_job_id",
        "model_version_id",
        "dataset_version_id",
        "qa_scope_sha256",
        "model_candidate_count",
        "user_discovered_candidate_count",
        "other_detector_candidate_count",
        "resolved_training_feedback_count",
        "true_positive_count",
        "false_positive_count",
        "false_negative_count",
        "held_count",
        "drift_assessed",
        "baseline_survey_model_qa_sha256",
        "promotion_gate_authority",
        "starts_training",
        "promotes_model",
      ],
      `조사 QA ${index + 1}`,
    );
    if (
      item.schema_version !==
        "trafficvisson.model-survey-qa-summary.v1" ||
      item.promotion_gate_authority !== false ||
      item.starts_training !== false ||
      item.promotes_model !== false
    ) {
      throw new Error("조사 QA 권한 경계가 올바르지 않습니다.");
    }
    const baselineSurveyModelQaSha256 =
      item.baseline_survey_model_qa_sha256 === null
        ? null
        : text(
            item.baseline_survey_model_qa_sha256,
            "기준 조사 QA 해시",
            SHA256_PATTERN,
            64,
          );
    return Object.freeze({
      surveyModelQaSha256: text(
        item.survey_model_qa_sha256,
        "조사 QA 해시",
        SHA256_PATTERN,
        64,
      ),
      pipelineJobId: uuid(item.pipeline_job_id, "PipelineJob ID"),
      analysisJobId: uuid(item.analysis_job_id, "AnalysisJob ID"),
      modelVersionId: text(
        item.model_version_id,
        "부모 모델 ID",
        IDENTIFIER_PATTERN,
        128,
      ),
      datasetVersionId: uuid(
        item.dataset_version_id,
        "승격 모델 DatasetVersion ID",
      ),
      qaScopeSha256: text(
        item.qa_scope_sha256,
        "조사 QA 범위 해시",
        SHA256_PATTERN,
        64,
      ),
      modelCandidateCount: integer(
        item.model_candidate_count,
        "승격 모델 후보 수",
      ),
      userDiscoveredCandidateCount: integer(
        item.user_discovered_candidate_count,
        "사용자 발견 누락 후보 수",
      ),
      otherDetectorCandidateCount: integer(
        item.other_detector_candidate_count,
        "다른 검지기 후보 수",
      ),
      resolvedTrainingFeedbackCount: integer(
        item.resolved_training_feedback_count,
        "확정 TRAIN 피드백 수",
      ),
      truePositiveCount: integer(
        item.true_positive_count,
        "TP 수",
      ),
      falsePositiveCount: integer(
        item.false_positive_count,
        "FP 수",
      ),
      falseNegativeCount: integer(
        item.false_negative_count,
        "FN 수",
      ),
      heldCount: integer(item.held_count, "보류 수"),
      driftAssessed: boolean(
        item.drift_assessed,
        "드리프트 평가 여부",
      ),
      baselineSurveyModelQaSha256,
    });
  });
  const iterations = source.iterations.map((raw, index) => {
    const item = record(raw, `모델 반복 ${index + 1}`);
    exactKeys(
      item,
      [
        "schema_version",
        "model_iteration_sha256",
        "parent_iteration_sha256",
        "survey_model_qa_sha256",
        "parent_model_version_id",
        "parent_dataset_version_id",
        "next_dataset_version_id",
        "feedback_annotation_count",
        "audit_only_member_count",
        "fixed_validation_sha256",
        "next_training_requires_user_confirmation",
        "automatic_training_start",
        "automatic_model_promotion",
        "iteration_creates_incident",
        "iteration_creates_report_truth",
      ],
      `모델 반복 ${index + 1}`,
    );
    if (
      item.schema_version !==
        "trafficvisson.model-iteration-summary.v1" ||
      item.next_training_requires_user_confirmation !== true ||
      item.automatic_training_start !== false ||
      item.automatic_model_promotion !== false ||
      item.iteration_creates_incident !== false ||
      item.iteration_creates_report_truth !== false
    ) {
      throw new Error("모델 반복 권한 경계가 올바르지 않습니다.");
    }
    const parentIterationSha256 =
      item.parent_iteration_sha256 === null
        ? null
        : text(
            item.parent_iteration_sha256,
            "부모 모델 반복 해시",
            SHA256_PATTERN,
            64,
          );
    return Object.freeze({
      modelIterationSha256: text(
        item.model_iteration_sha256,
        "모델 반복 해시",
        SHA256_PATTERN,
        64,
      ),
      parentIterationSha256,
      surveyModelQaSha256: text(
        item.survey_model_qa_sha256,
        "조사 QA 해시",
        SHA256_PATTERN,
        64,
      ),
      parentModelVersionId: text(
        item.parent_model_version_id,
        "부모 모델 ID",
        IDENTIFIER_PATTERN,
        128,
      ),
      parentDatasetVersionId: uuid(
        item.parent_dataset_version_id,
        "부모 DatasetVersion ID",
      ),
      nextDatasetVersionId: uuid(
        item.next_dataset_version_id,
        "다음 DatasetVersion ID",
      ),
      feedbackAnnotationCount: integer(
        item.feedback_annotation_count,
        "TRAIN 피드백 수",
      ),
      auditOnlyMemberCount: integer(
        item.audit_only_member_count,
        "감사 전용 멤버 수",
      ),
      fixedValidationSha256: text(
        item.fixed_validation_sha256,
        "고정 검증 해시",
        SHA256_PATTERN,
        64,
      ),
      nextTrainingRequiresUserConfirmation: true as const,
    });
  });
  return Object.freeze({
    surveyQa: Object.freeze(surveyQa),
    iterations: Object.freeze(iterations),
  });
}

export function buildProjectFixedValidationExtensionApproval(
  parent: ProjectFixedValidationVersion,
  addedIssueType: ProjectModelIssueType,
  addedValidationAnalysisJobIds: readonly string[],
): ProjectFixedValidationExtensionApproval {
  if (parent.requiredIssueTypes.includes(addedIssueType)) {
    throw new Error("이미 현재 고정 검증 세트에 포함된 유형입니다.");
  }
  const jobs = addedValidationAnalysisJobIds.map((item, index) =>
    uuid(item, `추가 검증 AnalysisJob ${index + 1}`),
  );
  const sortedJobs = [...new Set(jobs)].sort();
  if (
    sortedJobs.length === 0 ||
    sortedJobs.length !== jobs.length
  ) {
    throw new Error("추가 검증 AnalysisJob을 중복 없이 입력하세요.");
  }
  return Object.freeze({
    schemaVersion:
      "trafficvisson.fixed-validation-extension-approval.v1",
    expectedParentVersion: parent.validationSetVersion,
    expectedParentMembershipSha256: parent.membershipSha256,
    expectedParentFixedValidationSha256:
      parent.fixedValidationSha256,
    addedIssueTypes: Object.freeze([addedIssueType]),
    addedValidationAnalysisJobIds: Object.freeze(sortedJobs),
    userConfirmed: true,
  });
}

export function buildProjectModelStartRequest(
  projectId: string,
  datasetVersionId: string,
  idempotencyKey: string,
  fixedValidationExtensionApproval?: ProjectFixedValidationExtensionApproval,
): TrafficSuperVisionProjectModelTrainingStartRequest {
  return {
    projectId,
    datasetVersionId,
    idempotencyKey,
    userConfirmed: true,
    ...(fixedValidationExtensionApproval
      ? { fixedValidationExtensionApproval }
      : {}),
  };
}

export function buildProjectModelResumeRequest(
  projectId: string,
  trainingJobId: string,
): TrafficSuperVisionProjectModelTrainingResumeRequest {
  return { projectId, trainingJobId, userConfirmed: true };
}

export function buildProjectModelCancelRequest(
  projectId: string,
  trainingJobId: string,
): TrafficSuperVisionProjectModelTrainingCancelRequest {
  return { projectId, trainingJobId, userConfirmed: true };
}

export function buildProjectModelTransitionRequest(
  projectId: string,
  modelVersionId: string,
  expectedRevision: number,
  idempotencyKey: string,
): TrafficSuperVisionProjectModelTransitionRequest {
  return {
    projectId,
    modelVersionId,
    expectedRevision,
    idempotencyKey,
    userConfirmed: true,
  };
}

export function buildProjectModelRollbackRequest(
  projectId: string,
  expectedRevision: number,
  idempotencyKey: string,
): TrafficSuperVisionProjectModelRollbackRequest {
  return {
    projectId,
    expectedRevision,
    idempotencyKey,
    userConfirmed: true,
  };
}

export function createProjectModelIdempotencyKey(action: string) {
  const safeAction = action.replace(/[^A-Za-z0-9._:@-]/g, "-").slice(0, 40);
  const key = `project-model:${safeAction}:${crypto.randomUUID()}`;
  if (!IDEMPOTENCY_PATTERN.test(key)) {
    throw new Error("모델 작업 멱등 키를 만들지 못했습니다.");
  }
  return key;
}

export async function readProjectModelLifecycle(
  projectId: string,
): Promise<ProjectModelLifecycleSnapshot> {
  const api = window.trafficSuperVision?.projectModels;
  if (!api) {
    throw new Error("프로젝트 모델 수명주기는 설치형 Electron 앱에서 연결됩니다.");
  }
  const request = { projectId };
  const [
    jobsValue,
    versionsValue,
    fixedValidationValue,
    iterationsValue,
  ] = await Promise.all([
    api.listTrainingJobs(request),
    api.listVersions(request),
    api.listFixedValidationSets(request),
    api.listIterations(request),
  ]);
  const trainingJobs = parseProjectModelTrainingJobList(
    jobsValue,
    projectId,
  );
  const modelVersions = parseProjectModelVersionList(
    versionsValue,
    projectId,
  );
  const fixedValidationVersions =
    parseProjectFixedValidationVersionList(
      fixedValidationValue,
      projectId,
    );
  const modelIterations = parseProjectModelIterationLedger(
    iterationsValue,
    projectId,
  );
  const state =
    trainingJobs.length === 0 && modelVersions.length === 0
      ? null
      : parseProjectModelState(await api.getState(request), projectId);
  return Object.freeze({
    trainingJobs,
    modelVersions,
    fixedValidationVersions,
    modelIterations,
    state,
  });
}
