import {
  CANONICAL_ISSUE_TYPES,
  normalizeIssueTypeApi,
  type SupportedIssueTypeApi,
} from "./issueTypes";

export const AUTO_LABELING_ISSUE_OPTIONS = [
  { value: "auto", label: "자동 판독" },
  ...CANONICAL_ISSUE_TYPES.map(({ apiValue, labelKo }) => ({
    value: apiValue,
    label: labelKo,
  })),
  { value: "other", label: "기타 도로 문제" },
] as const;

export type AutoLabelingIssueSelection =
  (typeof AUTO_LABELING_ISSUE_OPTIONS)[number]["value"];
export type AutoLabelingIssueType = SupportedIssueTypeApi;
export type AutoLabelingScope = "current_job" | "project";
export type AutoLabelingMode = "proposal_only" | "assisted_auto";

export type AutoLabelingCapabilities = {
  objectDetection: boolean;
  segmentationRequired: boolean;
  ocrRequired: boolean;
  relationshipReasoningRequired: boolean;
  maintenanceJudgmentRequired: boolean;
};

export type AutoLabelingRecipe = {
  recipeId: string;
  projectId: string;
  command: string;
  issueType: AutoLabelingIssueType;
  displayNameKo: string;
  positivePrompts: string[];
  negativePrompts: string[];
};

export type AutoLabelingCompileResult = {
  recipe: AutoLabelingRecipe;
  capabilities: AutoLabelingCapabilities;
  defaultMode: "proposal_only";
  warnings: string[];
};

export type AutoLabelingRunStatus =
  | "completed"
  | "no_candidates"
  | "model_unavailable";

export type AutoLabelingProposalOrigin =
  | "local_model_generated"
  | "cached_existing"
  | "none";

export type AutoLabelingProposalGenerationStatus =
  | "generated"
  | "cached"
  | "unavailable";

export type AutoLabelingMatchReference = {
  analysisJobId: string;
  candidateId: string;
  issueType: AutoLabelingIssueType;
  reviewStatus: "pending" | "auto_accepted";
  decisionHash: string;
};

export type AutoLabelingRunResult = {
  runId: string;
  recipeId: string;
  projectId: string;
  scope: AutoLabelingScope;
  modeRequested: AutoLabelingMode;
  modeApplied: AutoLabelingMode;
  status: AutoLabelingRunStatus;
  proposalOrigin: AutoLabelingProposalOrigin;
  proposalGenerationStatus: AutoLabelingProposalGenerationStatus;
  jobsScanned: number;
  candidatesEvaluated: number;
  proposedCount: number;
  autoAcceptedCount: number;
  reviewRequiredCount: number;
  trainingLabelsCreated: 0;
  firstMatch: AutoLabelingMatchReference | null;
  messageKo: string;
};

export type AutoLabelingCompileRequest = {
  projectId: string;
  command: string;
  issueType?: AutoLabelingIssueType;
};

export type AutoLabelingRunRequest = {
  projectId: string;
  jobId?: string;
  recipeId: string;
  command: string;
  issueType: AutoLabelingIssueType;
  scope: AutoLabelingScope;
  mode: AutoLabelingMode;
  userConfirmed: true;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const RECIPE_ID_PATTERN = /^[0-9a-f]{64}$/;
const CANDIDATE_ID_PATTERN = /^[0-9a-f]{20}$/;
const ISSUE_TYPES = new Set<AutoLabelingIssueType>(
  AUTO_LABELING_ISSUE_OPTIONS.flatMap((option) =>
    option.value === "auto" ? [] : [option.value],
  ),
);
const SCOPES = new Set<AutoLabelingScope>(["current_job", "project"]);
const MODES = new Set<AutoLabelingMode>([
  "proposal_only",
  "assisted_auto",
]);
const RUN_STATUSES = new Set<AutoLabelingRunStatus>([
  "completed",
  "no_candidates",
  "model_unavailable",
]);
const PROPOSAL_ORIGINS = new Set<AutoLabelingProposalOrigin>([
  "local_model_generated",
  "cached_existing",
  "none",
]);
const PROPOSAL_GENERATION_STATUSES =
  new Set<AutoLabelingProposalGenerationStatus>([
    "generated",
    "cached",
    "unavailable",
  ]);
const REVIEW_STATUSES = new Set(["pending", "auto_accepted"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
): boolean {
  const keys = Object.keys(value);
  return (
    keys.length === expectedKeys.length &&
    keys.every((key) => expectedKeys.includes(key))
  );
}

function isBoundedText(
  value: unknown,
  minimum: number,
  maximum: number,
): value is string {
  return (
    typeof value === "string" &&
    value.trim().length >= minimum &&
    value.length <= maximum
  );
}

function isTextList(
  value: unknown,
  minimumItems: number,
  maximumItems: number,
  maximumLength: number,
): value is string[] {
  return (
    Array.isArray(value) &&
    value.length >= minimumItems &&
    value.length <= maximumItems &&
    value.every((item) => isBoundedText(item, 1, maximumLength))
  );
}

function isCount(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0
  );
}

function isBoundedCount(value: unknown, maximum: number): value is number {
  return isCount(value) && value <= maximum;
}

function requireUuid(value: string, label: string) {
  if (!UUID_PATTERN.test(value)) {
    throw new Error(`${label} 식별자가 올바르지 않습니다.`);
  }
}

function requireRecipeId(value: string) {
  if (!RECIPE_ID_PATTERN.test(value)) {
    throw new Error("AI 선라벨 recipe 식별자가 올바르지 않습니다.");
  }
}

function normalizedCommand(command: string) {
  const value = command.replace(/\s+/g, " ").trim();
  if (value.length < 3 || value.length > 500) {
    throw new Error("AI 선라벨 명령은 3자 이상 500자 이하로 입력하세요.");
  }
  return value;
}

export function isAutoLabelingIssueType(
  value: unknown,
): value is AutoLabelingIssueType {
  return (
    typeof value === "string" &&
    normalizeIssueTypeApi(value) === value &&
    ISSUE_TYPES.has(value as AutoLabelingIssueType)
  );
}

export function requiresMandatoryReview(
  issueType: AutoLabelingIssueType,
  capabilities?: AutoLabelingCapabilities,
) {
  return (
    issueType === "road_debris" ||
    issueType === "guide_sign_identity" ||
    issueType === "tunnel_identity" ||
    capabilities?.ocrRequired === true
  );
}

export function buildAutoLabelingCompileRequest(input: {
  projectId: string;
  command: string;
  issueType: AutoLabelingIssueSelection;
}): AutoLabelingCompileRequest {
  requireUuid(input.projectId, "프로젝트");
  const command = normalizedCommand(input.command);
  if (input.issueType !== "auto" && !isAutoLabelingIssueType(input.issueType)) {
    throw new Error("허용되지 않은 도로 문제 유형입니다.");
  }
  return {
    projectId: input.projectId,
    command,
    ...(input.issueType === "auto" ? {} : { issueType: input.issueType }),
  };
}

export function buildAutoLabelingRunRequest(input: {
  projectId: string;
  jobId?: string;
  recipeId: string;
  command: string;
  issueType: AutoLabelingIssueType;
  scope: AutoLabelingScope;
  mode: AutoLabelingMode;
}): AutoLabelingRunRequest {
  requireUuid(input.projectId, "프로젝트");
  requireRecipeId(input.recipeId);
  if (!isAutoLabelingIssueType(input.issueType)) {
    throw new Error("허용되지 않은 도로 문제 유형입니다.");
  }
  if (!SCOPES.has(input.scope)) {
    throw new Error("AI 선라벨 실행 범위가 올바르지 않습니다.");
  }
  if (!MODES.has(input.mode)) {
    throw new Error("AI 선라벨 실행 방식이 올바르지 않습니다.");
  }
  if (
    input.mode === "assisted_auto" &&
    requiresMandatoryReview(input.issueType)
  ) {
    throw new Error(
      "낙하물·이정표·터널 유형은 보조 자동 확정 대신 검토 전용으로 실행해야 합니다.",
    );
  }
  if (input.scope === "current_job") {
    if (!input.jobId) {
      throw new Error("현재 영상 범위에는 분석 작업 식별자가 필요합니다.");
    }
    requireUuid(input.jobId, "분석 작업");
  }
  return {
    projectId: input.projectId,
    ...(input.scope === "current_job" ? { jobId: input.jobId } : {}),
    recipeId: input.recipeId,
    command: normalizedCommand(input.command),
    issueType: input.issueType,
    scope: input.scope,
    mode: input.mode,
    userConfirmed: true,
  };
}

export function parseAutoLabelingCompileResponse(
  value: unknown,
  expected?: {
    projectId?: string;
    command?: string;
    issueType?: AutoLabelingIssueType;
  },
): AutoLabelingCompileResult {
  const keys = [
    "recipe_id",
    "project_id",
    "command",
    "issue_type",
    "display_name_ko",
    "positive_prompts",
    "negative_prompts",
    "capabilities",
    "default_mode",
    "warnings",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys)) {
    throw new Error("AI 선라벨 명령 해석 응답 형식이 올바르지 않습니다.");
  }
  if (
    typeof value.recipe_id !== "string" ||
    !RECIPE_ID_PATTERN.test(value.recipe_id) ||
    typeof value.project_id !== "string" ||
    !UUID_PATTERN.test(value.project_id) ||
    !isBoundedText(value.command, 3, 500) ||
    !isAutoLabelingIssueType(value.issue_type) ||
    !isBoundedText(value.display_name_ko, 1, 120) ||
    !isTextList(value.positive_prompts, 1, 8, 300) ||
    !isTextList(value.negative_prompts, 2, 8, 300) ||
    value.default_mode !== "proposal_only" ||
    !isTextList(value.warnings, 0, 16, 500) ||
    !isRecord(value.capabilities) ||
    !hasExactKeys(value.capabilities, [
      "object_detection",
      "segmentation_required",
      "ocr_required",
      "relationship_reasoning_required",
      "maintenance_judgment_required",
    ]) ||
    typeof value.capabilities.object_detection !== "boolean" ||
    typeof value.capabilities.segmentation_required !== "boolean" ||
    typeof value.capabilities.ocr_required !== "boolean" ||
    typeof value.capabilities.relationship_reasoning_required !== "boolean" ||
    typeof value.capabilities.maintenance_judgment_required !== "boolean"
  ) {
    throw new Error("AI 선라벨 명령 해석 응답을 검증하지 못했습니다.");
  }

  const command = normalizedCommand(value.command);
  if (expected?.projectId && value.project_id !== expected.projectId) {
    throw new Error("AI 선라벨 recipe가 다른 프로젝트에 속합니다.");
  }
  if (expected?.command && command !== normalizedCommand(expected.command)) {
    throw new Error("AI 선라벨 recipe의 원문 명령이 요청과 다릅니다.");
  }
  if (expected?.issueType && value.issue_type !== expected.issueType) {
    throw new Error("AI 선라벨 recipe의 유형이 요청과 다릅니다.");
  }

  return {
    recipe: {
      recipeId: value.recipe_id,
      projectId: value.project_id,
      command,
      issueType: value.issue_type,
      displayNameKo: value.display_name_ko.trim(),
      positivePrompts: value.positive_prompts.map((item) => item.trim()),
      negativePrompts: value.negative_prompts.map((item) => item.trim()),
    },
    capabilities: {
      objectDetection: value.capabilities.object_detection,
      segmentationRequired: value.capabilities.segmentation_required,
      ocrRequired: value.capabilities.ocr_required,
      relationshipReasoningRequired:
        value.capabilities.relationship_reasoning_required,
      maintenanceJudgmentRequired:
        value.capabilities.maintenance_judgment_required,
    },
    defaultMode: "proposal_only",
    warnings: value.warnings.map((item) => item.trim()),
  };
}

export function parseAutoLabelingRunResponse(
  value: unknown,
  expected?: {
    projectId?: string;
    recipeId?: string;
    scope?: AutoLabelingScope;
    modeRequested?: AutoLabelingMode;
  },
): AutoLabelingRunResult {
  const keys = [
    "run_id",
    "recipe_id",
    "project_id",
    "scope",
    "mode_requested",
    "mode_applied",
    "status",
    "proposal_origin",
    "proposal_generation_status",
    "jobs_scanned",
    "candidates_evaluated",
    "proposed_count",
    "auto_accepted_count",
    "review_required_count",
    "training_labels_created",
    "first_match",
    "message_ko",
  ] as const;
  if (!isRecord(value) || !hasExactKeys(value, keys)) {
    throw new Error("AI 선라벨 실행 응답 형식이 올바르지 않습니다.");
  }
  if (
    typeof value.run_id !== "string" ||
    !UUID_PATTERN.test(value.run_id) ||
    typeof value.recipe_id !== "string" ||
    !RECIPE_ID_PATTERN.test(value.recipe_id) ||
    typeof value.project_id !== "string" ||
    !UUID_PATTERN.test(value.project_id) ||
    typeof value.scope !== "string" ||
    !SCOPES.has(value.scope as AutoLabelingScope) ||
    typeof value.mode_requested !== "string" ||
    !MODES.has(value.mode_requested as AutoLabelingMode) ||
    typeof value.mode_applied !== "string" ||
    !MODES.has(value.mode_applied as AutoLabelingMode) ||
    typeof value.status !== "string" ||
    !RUN_STATUSES.has(value.status as AutoLabelingRunStatus) ||
    typeof value.proposal_origin !== "string" ||
    !PROPOSAL_ORIGINS.has(
      value.proposal_origin as AutoLabelingProposalOrigin,
    ) ||
    typeof value.proposal_generation_status !== "string" ||
    !PROPOSAL_GENERATION_STATUSES.has(
      value.proposal_generation_status as AutoLabelingProposalGenerationStatus,
    ) ||
    !isBoundedCount(value.jobs_scanned, 100) ||
    !isBoundedCount(value.candidates_evaluated, 10_000) ||
    !isBoundedCount(value.proposed_count, 10_000) ||
    !isBoundedCount(value.auto_accepted_count, 10_000) ||
    !isBoundedCount(value.review_required_count, 10_000) ||
    value.training_labels_created !== 0 ||
    !(
      value.first_match === null ||
      (isRecord(value.first_match) &&
        hasExactKeys(value.first_match, [
          "analysis_job_id",
          "candidate_id",
          "issue_type",
          "review_status",
          "decision_hash",
        ]) &&
        typeof value.first_match.analysis_job_id === "string" &&
        UUID_PATTERN.test(value.first_match.analysis_job_id) &&
        typeof value.first_match.candidate_id === "string" &&
        CANDIDATE_ID_PATTERN.test(value.first_match.candidate_id) &&
        isAutoLabelingIssueType(value.first_match.issue_type) &&
        typeof value.first_match.review_status === "string" &&
        REVIEW_STATUSES.has(value.first_match.review_status) &&
        typeof value.first_match.decision_hash === "string" &&
        RECIPE_ID_PATTERN.test(value.first_match.decision_hash))
    ) ||
    !isBoundedText(value.message_ko, 1, 500)
  ) {
    throw new Error("AI 선라벨 실행 응답을 검증하지 못했습니다.");
  }
  if (
    value.proposed_count > value.candidates_evaluated ||
    value.auto_accepted_count > value.proposed_count ||
    value.review_required_count > value.proposed_count ||
    value.auto_accepted_count + value.review_required_count !==
      value.proposed_count ||
    (value.mode_requested === "proposal_only" &&
      value.mode_applied !== "proposal_only") ||
    (value.mode_applied === "proposal_only" &&
      value.auto_accepted_count !== 0) ||
    (value.mode_applied === "assisted_auto" &&
      value.auto_accepted_count === 0) ||
    (value.status === "completed" && value.proposed_count === 0) ||
    (value.status === "no_candidates" &&
      (value.candidates_evaluated === 0 ||
        value.proposed_count !== 0 ||
        value.auto_accepted_count !== 0 ||
        value.review_required_count !== 0)) ||
    (value.status === "model_unavailable" &&
      (value.candidates_evaluated !== 0 ||
        value.proposed_count !== 0 ||
        value.auto_accepted_count !== 0 ||
        value.review_required_count !== 0)) ||
    (value.proposal_origin === "local_model_generated" &&
      value.proposal_generation_status !== "generated") ||
    (value.proposal_origin === "cached_existing" &&
      value.proposal_generation_status !== "cached") ||
    (value.proposal_origin === "none" &&
      value.proposal_generation_status !== "unavailable") ||
    (value.proposal_generation_status === "generated" &&
      value.proposal_origin !== "local_model_generated") ||
    (value.proposal_generation_status === "cached" &&
      value.proposal_origin !== "cached_existing") ||
    (value.proposal_generation_status === "unavailable" &&
      value.proposal_origin !== "none") ||
    (value.status === "model_unavailable" &&
      value.proposal_generation_status !== "unavailable") ||
    (value.status !== "model_unavailable" &&
      value.proposal_generation_status === "unavailable") ||
    (value.proposed_count === 0) !== (value.first_match === null)
  ) {
    throw new Error("AI 선라벨 실행 집계가 서로 일치하지 않습니다.");
  }
  if (expected?.projectId && value.project_id !== expected.projectId) {
    throw new Error("AI 선라벨 실행 결과가 다른 프로젝트에 속합니다.");
  }
  if (expected?.recipeId && value.recipe_id !== expected.recipeId) {
    throw new Error("AI 선라벨 실행 결과의 recipe가 요청과 다릅니다.");
  }
  if (expected?.scope && value.scope !== expected.scope) {
    throw new Error("AI 선라벨 실행 결과의 범위가 요청과 다릅니다.");
  }
  if (
    expected?.modeRequested &&
    value.mode_requested !== expected.modeRequested
  ) {
    throw new Error("AI 선라벨 실행 결과의 요청 방식이 원래 요청과 다릅니다.");
  }

  return {
    runId: value.run_id,
    recipeId: value.recipe_id,
    projectId: value.project_id,
    scope: value.scope as AutoLabelingScope,
    modeRequested: value.mode_requested as AutoLabelingMode,
    modeApplied: value.mode_applied as AutoLabelingMode,
    status: value.status as AutoLabelingRunStatus,
    proposalOrigin: value.proposal_origin as AutoLabelingProposalOrigin,
    proposalGenerationStatus:
      value.proposal_generation_status as AutoLabelingProposalGenerationStatus,
    jobsScanned: value.jobs_scanned,
    candidatesEvaluated: value.candidates_evaluated,
    proposedCount: value.proposed_count,
    autoAcceptedCount: value.auto_accepted_count,
    reviewRequiredCount: value.review_required_count,
    trainingLabelsCreated: 0,
    firstMatch:
      value.first_match === null
        ? null
        : {
            analysisJobId: value.first_match.analysis_job_id as string,
            candidateId: value.first_match.candidate_id as string,
            issueType: value.first_match
              .issue_type as AutoLabelingIssueType,
            reviewStatus: value.first_match.review_status as
              | "pending"
              | "auto_accepted",
            decisionHash: value.first_match.decision_hash as string,
          },
    messageKo: value.message_ko.trim(),
  };
}
