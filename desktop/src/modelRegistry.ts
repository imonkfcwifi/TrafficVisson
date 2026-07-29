export type LabelPartitionCounts = {
  approved: number;
  negative: number;
  held: number;
  excluded: number;
};

export type ProjectLabelStats = {
  projectId: string;
  questionCount: number;
  answerRevisionCount: number;
  currentAnswerCount: number;
  trainingIncludedCount: number;
  sourceAnalysisJobCount: number;
  partitions: LabelPartitionCounts;
};

export type DatasetPartition =
  | "approved"
  | "negative"
  | "held"
  | "excluded";
export type DatasetSplit = "train" | "validation" | "excluded";

export type DatasetMembership = {
  annotationId: string;
  questionId: string;
  analysisJobId: string;
  candidateId: string;
  answerRecordId: string;
  answerRevision: number;
  partition: DatasetPartition;
  includeInTraining: boolean;
  split: DatasetSplit;
  sourceRecordHash: string;
  annotationHash: string;
};

export type DatasetVersion = {
  datasetVersionId: string;
  projectId: string;
  schemaVersion: "trafficvisson.dataset-version.v1";
  datasetHash: string;
  splitStrategy: "analysis_job_hash_v1";
  validationFraction: number;
  sourceAnswerCount: number;
  trainingMemberCount: number;
  trainCount: number;
  validationCount: number;
  excludedCount: number;
  manifest: Record<string, unknown>;
  createdAt: string;
  reused: boolean;
  memberships: DatasetMembership[];
};

export type DatasetVersionList = {
  projectId: string;
  versions: DatasetVersion[];
  count: number;
};

export type SourceCandidateCatalogStatus =
  | "review_candidate"
  | "evaluation_complete"
  | "unverified_local"
  | "rejected";
export type SourceCandidateInstallationStatus =
  | "not_installed"
  | "installed_validation_only"
  | "local_cache_quarantined";

export type SourceCandidateEvaluation = {
  evaluatedFrameCount: number;
  proposalFrameCount: number;
  proposalCount: number;
  falsePositiveCount: number | null;
  verdict: "review_only" | "rejected";
};

export type SourceCandidate = {
  sourceId: string;
  displayName: string;
  sourceType: "model_pack" | "labeled_dataset";
  publisher: string;
  originUrl: string;
  licenseSpdxId: string;
  intendedUse:
    | "candidate_proposals"
    | "mask_refinement"
    | "training_seed"
    | "evaluation_reference"
    | "retrieval_reference";
  capability:
    | "natural_language_box_proposals"
    | "prompted_mask_refinement"
    | "semantic_candidate_scoring"
    | "pothole_box_proposals"
    | "labeled_training_reference";
  catalogStatus: SourceCandidateCatalogStatus;
  installationStatus: SourceCandidateInstallationStatus;
  evaluation: SourceCandidateEvaluation | null;
  observationBasis:
    | "verified_local_snapshot"
    | "unverified_local_files"
    | "not_observed"
    | "catalog_policy_only";
  rightsReviewRequired: true;
  automaticDownload: false;
  automaticActivation: false;
  weightExecutionEnabled: false;
  projectTrainingLinked: false;
  humanReviewRequired: true;
  decisionReason: string;
};

export type SourceCandidateCatalog = {
  schemaVersion: "trafficvisson.source-candidate-catalog.v1";
  catalogMode: "read_only_inactive";
  networkAccessPerformed: false;
  projectTrainingLinked: false;
  automaticLabelsCreated: false;
  candidates: SourceCandidate[];
  count: number;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const CANDIDATE_ID_PATTERN = /^[0-9a-f]{20}$/;
const SOURCE_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const SPDX_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$/;
const PARTITIONS = new Set<DatasetPartition>([
  "approved",
  "negative",
  "held",
  "excluded",
]);
const SPLITS = new Set<DatasetSplit>([
  "train",
  "validation",
  "excluded",
]);
const SOURCE_TYPES = new Set(["model_pack", "labeled_dataset"]);
const SOURCE_INTENDED_USES = new Set([
  "candidate_proposals",
  "mask_refinement",
  "training_seed",
  "evaluation_reference",
  "retrieval_reference",
]);
const SOURCE_CAPABILITIES = new Set([
  "natural_language_box_proposals",
  "prompted_mask_refinement",
  "semantic_candidate_scoring",
  "pothole_box_proposals",
  "labeled_training_reference",
]);
const SOURCE_CATALOG_STATUSES = new Set<SourceCandidateCatalogStatus>([
  "review_candidate",
  "evaluation_complete",
  "unverified_local",
  "rejected",
]);
const SOURCE_INSTALLATION_STATUSES =
  new Set<SourceCandidateInstallationStatus>([
    "not_installed",
    "installed_validation_only",
    "local_cache_quarantined",
  ]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
) {
  const keys = Object.keys(value);
  return (
    keys.length === expectedKeys.length &&
    keys.every((key) => expectedKeys.includes(key))
  );
}

function isCount(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0
  );
}

function isPositiveCount(value: unknown): value is number {
  return isCount(value) && value >= 1;
}

function isBoundedText(value: unknown, maximum: number): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= maximum &&
    value === value.trim() &&
    !/[\u0000-\u001f\u007f-\u009f]/u.test(value)
  );
}

function isHttpsMetadataUrl(value: unknown): value is string {
  if (!isBoundedText(value, 2_048)) return false;
  try {
    const url = new URL(value);
    return (
      url.protocol === "https:" &&
      Boolean(url.hostname) &&
      url.username === "" &&
      url.password === "" &&
      url.hash === "" &&
      (url.port === "" || url.port === "443")
    );
  } catch {
    return false;
  }
}

function isTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length <= 64 &&
    Number.isFinite(Date.parse(value))
  );
}

function requireUuid(value: string, label: string) {
  if (!UUID_PATTERN.test(value)) {
    throw new Error(`${label} 식별자가 올바르지 않습니다.`);
  }
}

function requireExpectedProject(projectId: string, expectedProjectId?: string) {
  if (!expectedProjectId) return;
  requireUuid(expectedProjectId, "요청 프로젝트");
  if (projectId !== expectedProjectId) {
    throw new Error("모델 데이터가 다른 프로젝트에 속합니다.");
  }
}

function parseDatasetMembership(value: unknown): DatasetMembership {
  const keys = [
    "annotation_id",
    "question_id",
    "analysis_job_id",
    "candidate_id",
    "answer_record_id",
    "answer_revision",
    "partition",
    "include_in_training",
    "split",
    "source_record_hash",
    "annotation_hash",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys)) {
    throw new Error("데이터셋 구성원 응답 형식이 올바르지 않습니다.");
  }
  if (
    typeof value.annotation_id !== "string" ||
    !UUID_PATTERN.test(value.annotation_id) ||
    typeof value.question_id !== "string" ||
    !UUID_PATTERN.test(value.question_id) ||
    typeof value.analysis_job_id !== "string" ||
    !UUID_PATTERN.test(value.analysis_job_id) ||
    typeof value.candidate_id !== "string" ||
    !CANDIDATE_ID_PATTERN.test(value.candidate_id) ||
    typeof value.answer_record_id !== "string" ||
    !UUID_PATTERN.test(value.answer_record_id) ||
    !isPositiveCount(value.answer_revision) ||
    typeof value.partition !== "string" ||
    !PARTITIONS.has(value.partition as DatasetPartition) ||
    typeof value.include_in_training !== "boolean" ||
    typeof value.split !== "string" ||
    !SPLITS.has(value.split as DatasetSplit) ||
    typeof value.source_record_hash !== "string" ||
    !SHA256_PATTERN.test(value.source_record_hash) ||
    typeof value.annotation_hash !== "string" ||
    !SHA256_PATTERN.test(value.annotation_hash)
  ) {
    throw new Error("데이터셋 구성원 응답을 검증하지 못했습니다.");
  }

  const partition = value.partition as DatasetPartition;
  const split = value.split as DatasetSplit;
  const expectedTraining =
    partition === "approved" || partition === "negative";
  if (
    value.annotation_id !== value.answer_record_id ||
    value.include_in_training !== expectedTraining ||
    (value.include_in_training && split === "excluded") ||
    (!value.include_in_training && split !== "excluded")
  ) {
    throw new Error("데이터셋 구성원의 라벨 출처와 분할이 일치하지 않습니다.");
  }

  return {
    annotationId: value.annotation_id,
    questionId: value.question_id,
    analysisJobId: value.analysis_job_id,
    candidateId: value.candidate_id,
    answerRecordId: value.answer_record_id,
    answerRevision: value.answer_revision,
    partition,
    includeInTraining: value.include_in_training,
    split,
    sourceRecordHash: value.source_record_hash,
    annotationHash: value.annotation_hash,
  };
}

export function buildProjectScopeRequest(projectId: string) {
  requireUuid(projectId, "프로젝트");
  return { projectId };
}

export function buildDatasetCreateRequest(projectId: string) {
  requireUuid(projectId, "프로젝트");
  return { projectId, userConfirmed: true as const };
}

export function parseProjectLabelStats(
  value: unknown,
  expectedProjectId?: string,
): ProjectLabelStats {
  const keys = [
    "project_id",
    "question_count",
    "answer_revision_count",
    "current_answer_count",
    "training_included_count",
    "source_analysis_job_count",
    "partitions",
  ] as const;
  const partitionKeys = [
    "approved",
    "negative",
    "held",
    "excluded",
  ] as const;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, keys) ||
    typeof value.project_id !== "string" ||
    !UUID_PATTERN.test(value.project_id) ||
    !isCount(value.question_count) ||
    !isCount(value.answer_revision_count) ||
    !isCount(value.current_answer_count) ||
    !isCount(value.training_included_count) ||
    !isCount(value.source_analysis_job_count) ||
    !isRecord(value.partitions) ||
    !hasExactKeys(value.partitions, partitionKeys) ||
    !isCount(value.partitions.approved) ||
    !isCount(value.partitions.negative) ||
    !isCount(value.partitions.held) ||
    !isCount(value.partitions.excluded)
  ) {
    throw new Error("프로젝트 라벨 통계 응답을 검증하지 못했습니다.");
  }
  requireExpectedProject(value.project_id, expectedProjectId);

  const partitionTotal =
    value.partitions.approved +
    value.partitions.negative +
    value.partitions.held +
    value.partitions.excluded;
  const expectedTraining =
    value.partitions.approved + value.partitions.negative;
  if (
    partitionTotal !== value.current_answer_count ||
    expectedTraining !== value.training_included_count ||
    value.question_count < value.current_answer_count ||
    value.answer_revision_count < value.current_answer_count ||
    value.source_analysis_job_count > value.current_answer_count
  ) {
    throw new Error("프로젝트 라벨 통계의 집계가 서로 일치하지 않습니다.");
  }

  return {
    projectId: value.project_id,
    questionCount: value.question_count,
    answerRevisionCount: value.answer_revision_count,
    currentAnswerCount: value.current_answer_count,
    trainingIncludedCount: value.training_included_count,
    sourceAnalysisJobCount: value.source_analysis_job_count,
    partitions: {
      approved: value.partitions.approved,
      negative: value.partitions.negative,
      held: value.partitions.held,
      excluded: value.partitions.excluded,
    },
  };
}

export function parseDatasetVersion(
  value: unknown,
  expectedProjectId?: string,
): DatasetVersion {
  const keys = [
    "dataset_version_id",
    "project_id",
    "schema_version",
    "dataset_hash",
    "split_strategy",
    "validation_fraction",
    "source_answer_count",
    "training_member_count",
    "train_count",
    "validation_count",
    "excluded_count",
    "manifest",
    "created_at",
    "reused",
    "memberships",
  ] as const;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, keys) ||
    typeof value.dataset_version_id !== "string" ||
    !UUID_PATTERN.test(value.dataset_version_id) ||
    typeof value.project_id !== "string" ||
    !UUID_PATTERN.test(value.project_id) ||
    value.schema_version !== "trafficvisson.dataset-version.v1" ||
    typeof value.dataset_hash !== "string" ||
    !SHA256_PATTERN.test(value.dataset_hash) ||
    value.split_strategy !== "analysis_job_hash_v1" ||
    typeof value.validation_fraction !== "number" ||
    !Number.isFinite(value.validation_fraction) ||
    value.validation_fraction <= 0 ||
    value.validation_fraction >= 1 ||
    !isPositiveCount(value.source_answer_count) ||
    !isCount(value.training_member_count) ||
    !isCount(value.train_count) ||
    !isCount(value.validation_count) ||
    !isCount(value.excluded_count) ||
    !isRecord(value.manifest) ||
    !isTimestamp(value.created_at) ||
    typeof value.reused !== "boolean" ||
    !Array.isArray(value.memberships)
  ) {
    throw new Error("데이터셋 버전 응답을 검증하지 못했습니다.");
  }
  requireExpectedProject(value.project_id, expectedProjectId);

  const memberships = value.memberships.map(parseDatasetMembership);
  const trainCount = memberships.filter((row) => row.split === "train").length;
  const validationCount = memberships.filter(
    (row) => row.split === "validation",
  ).length;
  const excludedCount = memberships.filter(
    (row) => row.split === "excluded",
  ).length;
  const trainingMemberCount = memberships.filter(
    (row) => row.includeInTraining,
  ).length;
  if (
    value.source_answer_count !== memberships.length ||
    value.training_member_count !== trainingMemberCount ||
    value.train_count !== trainCount ||
    value.validation_count !== validationCount ||
    value.excluded_count !== excludedCount ||
    value.training_member_count !== value.train_count + value.validation_count ||
    value.source_answer_count !==
      value.training_member_count + value.excluded_count
  ) {
    throw new Error("데이터셋 버전의 집계가 구성원과 일치하지 않습니다.");
  }

  const annotationIds = new Set(memberships.map((row) => row.annotationId));
  const questionIds = new Set(memberships.map((row) => row.questionId));
  if (
    annotationIds.size !== memberships.length ||
    questionIds.size !== memberships.length
  ) {
    throw new Error("데이터셋 버전에 중복 라벨이 포함되어 있습니다.");
  }

  return {
    datasetVersionId: value.dataset_version_id,
    projectId: value.project_id,
    schemaVersion: value.schema_version,
    datasetHash: value.dataset_hash,
    splitStrategy: value.split_strategy,
    validationFraction: value.validation_fraction,
    sourceAnswerCount: value.source_answer_count,
    trainingMemberCount: value.training_member_count,
    trainCount: value.train_count,
    validationCount: value.validation_count,
    excludedCount: value.excluded_count,
    manifest: value.manifest,
    createdAt: value.created_at,
    reused: value.reused,
    memberships,
  };
}

export function parseDatasetVersionList(
  value: unknown,
  expectedProjectId?: string,
): DatasetVersionList {
  const keys = ["project_id", "versions", "count"] as const;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, keys) ||
    typeof value.project_id !== "string" ||
    !UUID_PATTERN.test(value.project_id) ||
    !Array.isArray(value.versions) ||
    !isCount(value.count) ||
    value.count !== value.versions.length
  ) {
    throw new Error("데이터셋 버전 목록 응답을 검증하지 못했습니다.");
  }
  requireExpectedProject(value.project_id, expectedProjectId);
  const projectId = value.project_id;
  const versions = value.versions.map((version) =>
    parseDatasetVersion(version, projectId),
  );
  const versionIds = new Set(
    versions.map((version) => version.datasetVersionId),
  );
  const hashes = new Set(versions.map((version) => version.datasetHash));
  if (versionIds.size !== versions.length || hashes.size !== versions.length) {
    throw new Error("데이터셋 버전 목록에 중복 항목이 있습니다.");
  }
  return {
    projectId,
    versions,
    count: value.count,
  };
}

function parseSourceCandidateEvaluation(
  value: unknown,
): SourceCandidateEvaluation | null {
  if (value === null) return null;
  const keys = [
    "evaluated_frame_count",
    "proposal_frame_count",
    "proposal_count",
    "false_positive_count",
    "verdict",
  ] as const;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, keys) ||
    !isPositiveCount(value.evaluated_frame_count) ||
    value.evaluated_frame_count > 100_000 ||
    !isPositiveCount(value.proposal_frame_count) ||
    value.proposal_frame_count > value.evaluated_frame_count ||
    !isPositiveCount(value.proposal_count) ||
    value.proposal_count < value.proposal_frame_count ||
    value.proposal_count > 100_000 ||
    !(
      value.false_positive_count === null ||
      (isCount(value.false_positive_count) &&
        value.false_positive_count <= value.proposal_count)
    ) ||
    (value.verdict !== "review_only" && value.verdict !== "rejected") ||
    (value.verdict === "review_only" &&
      value.false_positive_count !== null) ||
    (value.verdict === "rejected" &&
      value.false_positive_count !== value.proposal_count)
  ) {
    throw new Error("외부 소스 평가 응답을 검증하지 못했습니다.");
  }
  return {
    evaluatedFrameCount: value.evaluated_frame_count,
    proposalFrameCount: value.proposal_frame_count,
    proposalCount: value.proposal_count,
    falsePositiveCount: value.false_positive_count,
    verdict: value.verdict,
  };
}

function parseSourceCandidate(value: unknown): SourceCandidate {
  const keys = [
    "source_id",
    "display_name",
    "source_type",
    "publisher",
    "origin_url",
    "license_spdx_id",
    "intended_use",
    "capability",
    "catalog_status",
    "installation_status",
    "evaluation",
    "observation_basis",
    "rights_review_required",
    "automatic_download",
    "automatic_activation",
    "weight_execution_enabled",
    "project_training_linked",
    "human_review_required",
    "decision_reason",
  ] as const;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, keys) ||
    !isBoundedText(value.source_id, 128) ||
    !SOURCE_ID_PATTERN.test(value.source_id) ||
    !isBoundedText(value.display_name, 120) ||
    typeof value.source_type !== "string" ||
    !SOURCE_TYPES.has(value.source_type) ||
    !isBoundedText(value.publisher, 256) ||
    !isHttpsMetadataUrl(value.origin_url) ||
    !isBoundedText(value.license_spdx_id, 64) ||
    !SPDX_ID_PATTERN.test(value.license_spdx_id) ||
    typeof value.intended_use !== "string" ||
    !SOURCE_INTENDED_USES.has(value.intended_use) ||
    typeof value.capability !== "string" ||
    !SOURCE_CAPABILITIES.has(value.capability) ||
    typeof value.catalog_status !== "string" ||
    !SOURCE_CATALOG_STATUSES.has(
      value.catalog_status as SourceCandidateCatalogStatus,
    ) ||
    typeof value.installation_status !== "string" ||
    !SOURCE_INSTALLATION_STATUSES.has(
      value.installation_status as SourceCandidateInstallationStatus,
    ) ||
    (value.observation_basis !== "verified_local_snapshot" &&
      value.observation_basis !== "unverified_local_files" &&
      value.observation_basis !== "not_observed" &&
      value.observation_basis !== "catalog_policy_only") ||
    value.rights_review_required !== true ||
    value.automatic_download !== false ||
    value.automatic_activation !== false ||
    value.weight_execution_enabled !== false ||
    value.project_training_linked !== false ||
    value.human_review_required !== true ||
    !isBoundedText(value.decision_reason, 800)
  ) {
    throw new Error("외부 소스 후보 응답을 검증하지 못했습니다.");
  }

  const evaluation = parseSourceCandidateEvaluation(value.evaluation);
  const catalogStatus =
    value.catalog_status as SourceCandidateCatalogStatus;
  const installationStatus =
    value.installation_status as SourceCandidateInstallationStatus;
  const observationBasis =
    value.observation_basis as SourceCandidate["observationBasis"];
  const verifiedStateValid =
    observationBasis !== "verified_local_snapshot" ||
    (catalogStatus === "evaluation_complete" &&
      installationStatus === "installed_validation_only" &&
      evaluation?.verdict === "review_only") ||
    (catalogStatus === "rejected" &&
      installationStatus === "local_cache_quarantined" &&
      evaluation?.verdict === "rejected");
  const unverifiedStateValid =
    observationBasis !== "unverified_local_files" ||
    ((catalogStatus === "unverified_local" ||
      catalogStatus === "rejected") &&
      installationStatus === "local_cache_quarantined" &&
      evaluation === null);
  const unobservedStateValid =
    (observationBasis !== "not_observed" &&
      observationBasis !== "catalog_policy_only") ||
    ((catalogStatus === "review_candidate" ||
      catalogStatus === "rejected") &&
      installationStatus === "not_installed" &&
      evaluation === null);
  if (
    !verifiedStateValid ||
    !unverifiedStateValid ||
    !unobservedStateValid
  ) {
    throw new Error("외부 소스 후보의 검토 상태가 서로 일치하지 않습니다.");
  }

  return {
    sourceId: value.source_id,
    displayName: value.display_name,
    sourceType: value.source_type as SourceCandidate["sourceType"],
    publisher: value.publisher,
    originUrl: value.origin_url,
    licenseSpdxId: value.license_spdx_id,
    intendedUse: value.intended_use as SourceCandidate["intendedUse"],
    capability: value.capability as SourceCandidate["capability"],
    catalogStatus,
    installationStatus,
    evaluation,
    observationBasis,
    rightsReviewRequired: true,
    automaticDownload: false,
    automaticActivation: false,
    weightExecutionEnabled: false,
    projectTrainingLinked: false,
    humanReviewRequired: true,
    decisionReason: value.decision_reason,
  };
}

export function parseSourceCandidateCatalog(
  value: unknown,
): SourceCandidateCatalog {
  const keys = [
    "schema_version",
    "catalog_mode",
    "network_access_performed",
    "project_training_linked",
    "automatic_labels_created",
    "candidates",
    "count",
  ] as const;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, keys) ||
    value.schema_version !== "trafficvisson.source-candidate-catalog.v1" ||
    value.catalog_mode !== "read_only_inactive" ||
    value.network_access_performed !== false ||
    value.project_training_linked !== false ||
    value.automatic_labels_created !== false ||
    !Array.isArray(value.candidates) ||
    value.candidates.length > 32 ||
    !isCount(value.count) ||
    value.count !== value.candidates.length
  ) {
    throw new Error("외부 소스 카탈로그 응답을 검증하지 못했습니다.");
  }
  const candidates = value.candidates.map(parseSourceCandidate);
  const sourceIds = new Set(
    candidates.map((candidate) => candidate.sourceId),
  );
  if (sourceIds.size !== candidates.length) {
    throw new Error("외부 소스 카탈로그에 중복 후보가 있습니다.");
  }

  return {
    schemaVersion: value.schema_version,
    catalogMode: value.catalog_mode,
    networkAccessPerformed: false,
    projectTrainingLinked: false,
    automaticLabelsCreated: false,
    candidates,
    count: value.count,
  };
}
