import {
  CANONICAL_ISSUE_TYPES,
  normalizeIssueTypeApi,
  type CanonicalIssueTypeApi,
  type SupportedIssueTypeApi,
} from "./issueTypes";

export type NormalizedReviewBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type TemporalReviewDetectionRequest = {
  detectionId: string;
  timestampSeconds: number;
  issueType: SupportedIssueTypeApi;
  score: number;
  reviewOnly: true;
  automaticLabelingAllowed: false;
  normalizedBox?: NormalizedReviewBox;
  trackHint?: string;
  frameIndex?: number;
  scoreSemantics?:
    | "model_score_not_probability"
    | "zero_shot_relative_not_probability";
};

export type TemporalGroupingPolicyRequest = {
  maxGapSeconds: number;
  minIou: number;
  maxCenterDistance: number;
  strictTrackSeparation: boolean;
  allowTemporalOnlyWithoutSpatialHints: boolean;
};

export type TemporalIncidentReviewRequest = {
  projectId: string;
  detections: TemporalReviewDetectionRequest[];
  provenance: {
    projectId: string;
    analysisJobId: string;
    sourceFileSha256: string;
    source: "classified_review_proposals";
  };
  policy: TemporalGroupingPolicyRequest;
};

export type TemporalPeakEvidence = {
  detectionId: string;
  timestampSeconds: number;
  score: number;
  normalizedBox: NormalizedReviewBox | null;
  trackHint: string | null;
  frameIndex: number | null;
  scoreSemantics:
    | "model_score_not_probability"
    | "zero_shot_relative_not_probability"
    | null;
};

export type TemporalMergeReason =
  | "same_track_hint"
  | "spatial_iou"
  | "spatial_center_distance"
  | "bounded_gap_without_comparable_spatial_hint";

export type TemporalMergeLink = {
  fromDetectionId: string;
  toDetectionId: string;
  gapSeconds: number;
  reason: TemporalMergeReason;
  iou: number | null;
  centerDistance: number | null;
};

export type TemporalIncidentReviewProposal = {
  proposalId: string;
  issueType: SupportedIssueTypeApi;
  startSeconds: number;
  endSeconds: number;
  durationSeconds: number;
  detectionCount: number;
  evidenceDetectionIds: string[];
  peakEvidence: TemporalPeakEvidence;
  mergeReasons: Array<TemporalMergeReason | "single_detection">;
  mergeLinks: TemporalMergeLink[];
  source: {
    projectId: string;
    analysisJobId: string;
    sourceFileSha256: string;
    source: "classified_review_proposals";
  };
  sourceFingerprintSha256: string;
  evidenceFingerprintSha256: string;
  groupingPolicy: TemporalGroupingPolicyRequest;
};

export type TemporalIncidentReviewResult = {
  projectId: string;
  proposalCount: number;
  proposals: TemporalIncidentReviewProposal[];
  stateChangesPerformed: false;
  incidentsCreated: 0;
  annotationsCreated: 0;
  trainingMembersCreated: 0;
  modelVersionsChanged: 0;
};

export type TemporalQuestionGroup = {
  proposalId: string;
  representativeQuestionId: string;
  evidenceQuestionIds: string[];
  peakEvidenceIndex: number;
  startSeconds: number;
  endSeconds: number;
  durationSeconds: number;
  detectionCount: number;
};

export type TemporalQuestionOrder = {
  orderedQuestionIds: string[];
  groupsByRepresentativeId: Record<string, TemporalQuestionGroup>;
};

export type ValidationTruthRecordRequest = {
  sampleId: string;
  issueType: CanonicalIssueTypeApi;
  truthEventIds: string[];
};

export type ValidationPredictionRecordRequest = {
  predictionEventId: string;
  sampleId: string;
  issueType: CanonicalIssueTypeApi;
  matchedTruthEventId?: string;
};

export type ModelValidationReviewRequest = {
  projectId: string;
  modelVersion: string;
  truthRecords: ValidationTruthRecordRequest[];
  predictionRecords: ValidationPredictionRecordRequest[];
};

export type PerTypeValidationMetrics = {
  issueType: CanonicalIssueTypeApi;
  validationSampleCount: number;
  positiveSampleCount: number;
  negativeSampleCount: number;
  truthEventCount: number;
  predictionEventCount: number;
  truePositiveCount: number;
  falsePositiveCount: number;
  falseNegativeCount: number;
  duplicatePredictionCount: number;
  precision: number;
  recall: number;
  falsePositiveRate: number;
  duplicateBurden: number;
};

export type ModelValidationReport = {
  modelVersion: string;
  validationSetSha256: string;
  predictionSetSha256: string;
  perType: PerTypeValidationMetrics[];
};

export type ModelValidationReviewResult = {
  projectId: string;
  report: ModelValidationReport;
  stateChangesPerformed: false;
  annotationsCreated: 0;
  trainingMembersCreated: 0;
  modelVersionsChanged: 0;
};

export type ModelPromotionReviewRequest = {
  projectId: string;
  activeModelVersion: string;
  candidateModelVersion: string;
  truthRecords: ValidationTruthRecordRequest[];
  activePredictionRecords: ValidationPredictionRecordRequest[];
  candidatePredictionRecords: ValidationPredictionRecordRequest[];
  requiredIssueTypes?: CanonicalIssueTypeApi[];
  minimumSamplesPerType: number;
  maximumRecallDrop: number;
};

export type PromotionReviewReason = {
  code:
    | "insufficient_validation_samples"
    | "missing_positive_truth"
    | "recall_drop_at_or_above_limit"
    | "false_positive_rate_increased"
    | "duplicate_burden_increased";
  issueType: CanonicalIssueTypeApi;
  message: string;
  activeValue: number | null;
  candidateValue: number | null;
  limitValue: number | null;
};

export type PromotionReviewDecision = {
  decision: "eligible" | "rejected";
  activeModelVersion: string;
  candidateModelVersion: string;
  validationSetSha256: string;
  requiredIssueTypes: CanonicalIssueTypeApi[];
  minimumSamplesPerType: number;
  maximumRecallDrop: number;
  activeReport: ModelValidationReport;
  candidateReport: ModelValidationReport;
  reasons: PromotionReviewReason[];
  autoPromotionAllowed: false;
  requiresUserReview: true;
};

export type ModelPromotionReviewResult = {
  projectId: string;
  decision: PromotionReviewDecision;
  stateChangesPerformed: false;
  annotationsCreated: 0;
  trainingMembersCreated: 0;
  modelVersionsChanged: 0;
  automaticPromotionPerformed: false;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const PROPOSAL_ID_PATTERN = /^tip_[a-f0-9]{24}$/;
const CANONICAL_ORDER = CANONICAL_ISSUE_TYPES.map(({ apiValue }) => apiValue);
const CANONICAL_SET = new Set<string>(CANONICAL_ORDER);
const SCORE_SEMANTICS = new Set([
  "model_score_not_probability",
  "zero_shot_relative_not_probability",
]);
const MERGE_REASONS = new Set<TemporalMergeReason>([
  "same_track_hint",
  "spatial_iou",
  "spatial_center_distance",
  "bounded_gap_without_comparable_spatial_hint",
]);
const PROMOTION_REASON_CODES = new Set([
  "insufficient_validation_samples",
  "missing_positive_truth",
  "recall_drop_at_or_above_limit",
  "false_positive_rate_increased",
  "duplicate_burden_increased",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
) {
  const keys = Object.keys(value);
  return (
    keys.length === expected.length &&
    keys.every((key) => expected.includes(key))
  );
}

function requireUuid(value: unknown, label: string): string {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) {
    throw new Error(`${label} 식별자가 올바르지 않습니다.`);
  }
  return value.toLowerCase();
}

function requireIdentifier(value: unknown, label: string): string {
  if (typeof value !== "string" || !IDENTIFIER_PATTERN.test(value)) {
    throw new Error(`${label} 식별자가 올바르지 않습니다.`);
  }
  return value;
}

function requireSha256(value: unknown, label: string): string {
  if (typeof value !== "string" || !SHA256_PATTERN.test(value)) {
    throw new Error(`${label} 해시가 올바르지 않습니다.`);
  }
  return value;
}

function requireBoundedNumber(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
) {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new Error(`${label} 값이 허용 범위를 벗어났습니다.`);
  }
  return value;
}

function requireCount(value: unknown, label: string, maximum: number) {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0 ||
    value > maximum
  ) {
    throw new Error(`${label} 집계가 올바르지 않습니다.`);
  }
  return value;
}

function requirePositiveCount(value: unknown, label: string, maximum: number) {
  const count = requireCount(value, label, maximum);
  if (count === 0) throw new Error(`${label} 값은 1 이상이어야 합니다.`);
  return count;
}

function requireCanonicalIssueType(
  value: unknown,
  label: string,
): CanonicalIssueTypeApi {
  const normalized = normalizeIssueTypeApi(value);
  if (!normalized || normalized === "other" || !CANONICAL_SET.has(normalized)) {
    throw new Error(`${label} 유형이 표준 12개 유형에 속하지 않습니다.`);
  }
  return normalized as CanonicalIssueTypeApi;
}

function requireSupportedIssueType(
  value: unknown,
  label: string,
): SupportedIssueTypeApi {
  const normalized = normalizeIssueTypeApi(value);
  if (!normalized) throw new Error(`${label} 유형이 올바르지 않습니다.`);
  return normalized;
}

function parseNormalizedBox(
  value: unknown,
  label: string,
  nullable: boolean,
): NormalizedReviewBox | null {
  if (nullable && value === null) return null;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, ["x", "y", "width", "height"])
  ) {
    throw new Error(`${label} 영역 형식이 올바르지 않습니다.`);
  }
  const x = requireBoundedNumber(value.x, `${label}.x`, 0, 1);
  const y = requireBoundedNumber(value.y, `${label}.y`, 0, 1);
  const width = requireBoundedNumber(value.width, `${label}.width`, 0, 1);
  const height = requireBoundedNumber(value.height, `${label}.height`, 0, 1);
  if (width === 0 || height === 0 || x + width > 1 || y + height > 1) {
    throw new Error(`${label} 영역이 프레임 안에 있지 않습니다.`);
  }
  return { x, y, width, height };
}

function strictOptionalIdentifier(
  value: unknown,
  label: string,
): string | undefined {
  return value === undefined
    ? undefined
    : requireIdentifier(value, label);
}

function strictOptionalFrameIndex(
  value: unknown,
  label: string,
): number | undefined {
  if (value === undefined) return undefined;
  return requireCount(value, label, Number.MAX_SAFE_INTEGER);
}

function strictOptionalScoreSemantics(
  value: unknown,
  label: string,
):
  | "model_score_not_probability"
  | "zero_shot_relative_not_probability"
  | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !SCORE_SEMANTICS.has(value)) {
    throw new Error(`${label} 점수 의미가 올바르지 않습니다.`);
  }
  return value as
    | "model_score_not_probability"
    | "zero_shot_relative_not_probability";
}

export function buildTemporalIncidentReviewRequest(input: {
  projectId: string;
  analysisJobId: string;
  sourceFileSha256: string;
  detections: Array<{
    detectionId: string;
    timestampSeconds: number;
    issueType: unknown;
    score: number;
    normalizedBox?: unknown;
    trackHint?: string;
    frameIndex?: number;
    scoreSemantics?: unknown;
  }>;
  policy?: Partial<TemporalGroupingPolicyRequest>;
}): TemporalIncidentReviewRequest {
  const projectId = requireUuid(input.projectId, "프로젝트");
  const analysisJobId = requireUuid(input.analysisJobId, "분석 작업");
  const sourceFileSha256 = requireSha256(
    input.sourceFileSha256,
    "입력 영상",
  );
  if (!Array.isArray(input.detections) || input.detections.length > 100_000) {
    throw new Error("시간축 검토 입력은 최대 100,000건이어야 합니다.");
  }
  const detectionIds = new Set<string>();
  const detections = input.detections.map((raw, index) => {
    const detectionId = requireIdentifier(
      raw.detectionId,
      `검지 ${index + 1}`,
    );
    if (detectionIds.has(detectionId)) {
      throw new Error("시간축 검지 식별자는 중복될 수 없습니다.");
    }
    detectionIds.add(detectionId);
    const normalizedBox =
      raw.normalizedBox === undefined
        ? undefined
        : (parseNormalizedBox(
            raw.normalizedBox,
            `검지 ${index + 1}`,
            false,
          ) ?? undefined);
    return {
      detectionId,
      timestampSeconds: requireBoundedNumber(
        raw.timestampSeconds,
        `검지 ${index + 1} 시각`,
        0,
        2_678_400,
      ),
      issueType: requireSupportedIssueType(
        raw.issueType,
        `검지 ${index + 1}`,
      ),
      score: requireBoundedNumber(
        raw.score,
        `검지 ${index + 1} 점수`,
        0,
        1,
      ),
      reviewOnly: true as const,
      automaticLabelingAllowed: false as const,
      ...(normalizedBox ? { normalizedBox } : {}),
      ...(raw.trackHint === undefined
        ? {}
        : {
            trackHint: strictOptionalIdentifier(
              raw.trackHint,
              `검지 ${index + 1} track`,
            ),
          }),
      ...(raw.frameIndex === undefined
        ? {}
        : {
            frameIndex: strictOptionalFrameIndex(
              raw.frameIndex,
              `검지 ${index + 1} frame`,
            ),
          }),
      ...(raw.scoreSemantics === undefined
        ? {}
        : {
            scoreSemantics: strictOptionalScoreSemantics(
              raw.scoreSemantics,
              `검지 ${index + 1}`,
            ),
          }),
    };
  });
  const rawPolicy = {
    maxGapSeconds: 2,
    minIou: 0.1,
    maxCenterDistance: 0.15,
    strictTrackSeparation: true,
    allowTemporalOnlyWithoutSpatialHints: false,
    ...input.policy,
  };
  if (
    typeof rawPolicy.strictTrackSeparation !== "boolean" ||
    typeof rawPolicy.allowTemporalOnlyWithoutSpatialHints !== "boolean"
  ) {
    throw new Error("시간축 병합 정책의 안전 플래그가 올바르지 않습니다.");
  }
  return {
    projectId,
    detections,
    provenance: {
      projectId,
      analysisJobId,
      sourceFileSha256,
      source: "classified_review_proposals",
    },
    policy: {
      maxGapSeconds: requireBoundedNumber(
        rawPolicy.maxGapSeconds,
        "최대 시간 간격",
        0.001,
        60,
      ),
      minIou: requireBoundedNumber(rawPolicy.minIou, "최소 IoU", 0, 1),
      maxCenterDistance: requireBoundedNumber(
        rawPolicy.maxCenterDistance,
        "최대 중심 거리",
        0,
        Math.SQRT2,
      ),
      strictTrackSeparation: rawPolicy.strictTrackSeparation,
      allowTemporalOnlyWithoutSpatialHints:
        rawPolicy.allowTemporalOnlyWithoutSpatialHints,
    },
  };
}

function parsePolicy(
  value: unknown,
  label: string,
): TemporalGroupingPolicyRequest {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "max_gap_seconds",
      "min_iou",
      "max_center_distance",
      "strict_track_separation",
      "allow_temporal_only_without_spatial_hints",
    ]) ||
    typeof value.strict_track_separation !== "boolean" ||
    typeof value.allow_temporal_only_without_spatial_hints !== "boolean"
  ) {
    throw new Error(`${label} 병합 정책 응답이 올바르지 않습니다.`);
  }
  return {
    maxGapSeconds: requireBoundedNumber(
      value.max_gap_seconds,
      `${label} 최대 시간 간격`,
      0.001,
      60,
    ),
    minIou: requireBoundedNumber(
      value.min_iou,
      `${label} 최소 IoU`,
      0,
      1,
    ),
    maxCenterDistance: requireBoundedNumber(
      value.max_center_distance,
      `${label} 최대 중심 거리`,
      0,
      Math.SQRT2,
    ),
    strictTrackSeparation: value.strict_track_separation,
    allowTemporalOnlyWithoutSpatialHints:
      value.allow_temporal_only_without_spatial_hints,
  };
}

function parseNullableNumber(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
) {
  return value === null
    ? null
    : requireBoundedNumber(value, label, minimum, maximum);
}

function parseTemporalProposal(
  value: unknown,
  index: number,
): TemporalIncidentReviewProposal {
  const label = `시간축 제안 ${index + 1}`;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "schema_version",
      "proposal_id",
      "proposal_status",
      "issue_type",
      "start_seconds",
      "end_seconds",
      "duration_seconds",
      "detection_count",
      "evidence_detection_ids",
      "peak_evidence",
      "merge_reasons",
      "merge_links",
      "review_only",
      "automatic_incident_creation_allowed",
      "automatic_annotation_allowed",
      "automatic_maintenance_action_allowed",
      "automatic_training_allowed",
      "provenance",
    ]) ||
    value.schema_version !== "trafficvisson.temporal-incident-proposal.v1" ||
    typeof value.proposal_id !== "string" ||
    !PROPOSAL_ID_PATTERN.test(value.proposal_id) ||
    value.proposal_status !== "awaiting_user_review" ||
    value.review_only !== true ||
    value.automatic_incident_creation_allowed !== false ||
    value.automatic_annotation_allowed !== false ||
    value.automatic_maintenance_action_allowed !== false ||
    value.automatic_training_allowed !== false
  ) {
    throw new Error(`${label}의 검토 전용 안전 계약이 올바르지 않습니다.`);
  }
  const startSeconds = requireBoundedNumber(
    value.start_seconds,
    `${label} 시작 시각`,
    0,
    2_678_400,
  );
  const endSeconds = requireBoundedNumber(
    value.end_seconds,
    `${label} 종료 시각`,
    0,
    2_678_400,
  );
  const durationSeconds = requireBoundedNumber(
    value.duration_seconds,
    `${label} 길이`,
    0,
    2_678_400,
  );
  const detectionCount = requirePositiveCount(
    value.detection_count,
    `${label} 검지 수`,
    100_000,
  );
  if (
    startSeconds > endSeconds ||
    Math.abs(endSeconds - startSeconds - durationSeconds) > 1e-8 ||
    !Array.isArray(value.evidence_detection_ids) ||
    value.evidence_detection_ids.length !== detectionCount
  ) {
    throw new Error(`${label}의 시간 범위 또는 근거 수가 일치하지 않습니다.`);
  }
  const evidenceDetectionIds = value.evidence_detection_ids.map(
    (item, itemIndex) =>
      requireIdentifier(item, `${label} 근거 ${itemIndex + 1}`),
  );
  if (new Set(evidenceDetectionIds).size !== evidenceDetectionIds.length) {
    throw new Error(`${label}의 근거 식별자가 중복되었습니다.`);
  }

  const peak = value.peak_evidence;
  if (
    !isRecord(peak) ||
    !hasExactKeys(peak, [
      "detection_id",
      "timestamp_seconds",
      "score",
      "normalized_box",
      "track_hint",
      "frame_index",
      "score_semantics",
    ])
  ) {
    throw new Error(`${label}의 대표 근거 형식이 올바르지 않습니다.`);
  }
  const peakDetectionId = requireIdentifier(
    peak.detection_id,
    `${label} 대표 근거`,
  );
  const peakTimestamp = requireBoundedNumber(
    peak.timestamp_seconds,
    `${label} 대표 시각`,
    0,
    2_678_400,
  );
  if (
    !evidenceDetectionIds.includes(peakDetectionId) ||
    peakTimestamp < startSeconds ||
    peakTimestamp > endSeconds ||
    !(
      peak.track_hint === null ||
      (typeof peak.track_hint === "string" &&
        IDENTIFIER_PATTERN.test(peak.track_hint))
    ) ||
    !(
      peak.frame_index === null ||
      (typeof peak.frame_index === "number" &&
        Number.isSafeInteger(peak.frame_index) &&
        peak.frame_index >= 0)
    ) ||
    !(
      peak.score_semantics === null ||
      (typeof peak.score_semantics === "string" &&
        SCORE_SEMANTICS.has(peak.score_semantics))
    )
  ) {
    throw new Error(`${label}의 대표 근거가 사건 범위와 일치하지 않습니다.`);
  }

  if (
    !Array.isArray(value.merge_reasons) ||
    value.merge_reasons.length < 1 ||
    value.merge_reasons.length > 4 ||
    value.merge_reasons.some(
      (reason) => reason !== "single_detection" && !MERGE_REASONS.has(reason),
    ) ||
    !Array.isArray(value.merge_links) ||
    value.merge_links.length !== detectionCount - 1
  ) {
    throw new Error(`${label}의 병합 근거가 올바르지 않습니다.`);
  }
  if (
    (detectionCount === 1 &&
      (value.merge_reasons.length !== 1 ||
        value.merge_reasons[0] !== "single_detection")) ||
    (detectionCount > 1 &&
      value.merge_reasons.includes("single_detection"))
  ) {
    throw new Error(`${label}의 단일·다중 프레임 병합 사유가 모순됩니다.`);
  }
  const mergeLinks = value.merge_links.map((raw, linkIndex) => {
    if (
      !isRecord(raw) ||
      !hasExactKeys(raw, [
        "from_detection_id",
        "to_detection_id",
        "gap_seconds",
        "reason",
        "iou",
        "center_distance",
      ]) ||
      typeof raw.reason !== "string" ||
      !MERGE_REASONS.has(raw.reason as TemporalMergeReason)
    ) {
      throw new Error(`${label}의 병합 연결 ${linkIndex + 1}이 올바르지 않습니다.`);
    }
    const fromDetectionId = requireIdentifier(
      raw.from_detection_id,
      `${label} 병합 시작`,
    );
    const toDetectionId = requireIdentifier(
      raw.to_detection_id,
      `${label} 병합 종료`,
    );
    if (
      !evidenceDetectionIds.includes(fromDetectionId) ||
      !evidenceDetectionIds.includes(toDetectionId)
    ) {
      throw new Error(`${label}의 병합 연결이 근거 집합 밖을 참조합니다.`);
    }
    return {
      fromDetectionId,
      toDetectionId,
      gapSeconds: requireBoundedNumber(
        raw.gap_seconds,
        `${label} 병합 간격`,
        0,
        60,
      ),
      reason: raw.reason as TemporalMergeReason,
      iou: parseNullableNumber(raw.iou, `${label} IoU`, 0, 1),
      centerDistance: parseNullableNumber(
        raw.center_distance,
        `${label} 중심 거리`,
        0,
        Math.SQRT2,
      ),
    };
  });

  const provenance = value.provenance;
  if (
    !isRecord(provenance) ||
    !hasExactKeys(provenance, [
      "algorithm",
      "source",
      "source_fingerprint_sha256",
      "evidence_fingerprint_sha256",
      "grouping_policy",
    ]) ||
    provenance.algorithm !== "temporal-spatial-review-grouping.v1" ||
    !isRecord(provenance.source) ||
    !hasExactKeys(provenance.source, [
      "project_id",
      "analysis_job_id",
      "source_file_sha256",
      "source",
    ]) ||
    provenance.source.source !== "classified_review_proposals"
  ) {
    throw new Error(`${label}의 provenance 형식이 올바르지 않습니다.`);
  }
  return {
    proposalId: value.proposal_id,
    issueType: requireSupportedIssueType(value.issue_type, label),
    startSeconds,
    endSeconds,
    durationSeconds,
    detectionCount,
    evidenceDetectionIds,
    peakEvidence: {
      detectionId: peakDetectionId,
      timestampSeconds: peakTimestamp,
      score: requireBoundedNumber(
        peak.score,
        `${label} 대표 점수`,
        0,
        1,
      ),
      normalizedBox: parseNormalizedBox(
        peak.normalized_box,
        `${label} 대표`,
        true,
      ),
      trackHint: peak.track_hint as string | null,
      frameIndex: peak.frame_index as number | null,
      scoreSemantics: peak.score_semantics as
        | "model_score_not_probability"
        | "zero_shot_relative_not_probability"
        | null,
    },
    mergeReasons: value.merge_reasons as Array<
      TemporalMergeReason | "single_detection"
    >,
    mergeLinks,
    source: {
      projectId: requireUuid(
        provenance.source.project_id,
        `${label} 원본 프로젝트`,
      ),
      analysisJobId: requireUuid(
        provenance.source.analysis_job_id,
        `${label} 원본 분석 작업`,
      ),
      sourceFileSha256: requireSha256(
        provenance.source.source_file_sha256,
        `${label} 입력 영상`,
      ),
      source: "classified_review_proposals",
    },
    sourceFingerprintSha256: requireSha256(
      provenance.source_fingerprint_sha256,
      `${label} 원본`,
    ),
    evidenceFingerprintSha256: requireSha256(
      provenance.evidence_fingerprint_sha256,
      `${label} 근거`,
    ),
    groupingPolicy: parsePolicy(
      provenance.grouping_policy,
      `${label} provenance`,
    ),
  };
}

export function parseTemporalIncidentReviewResponse(
  value: unknown,
  expected?: {
    projectId?: string;
    analysisJobId?: string;
    sourceFileSha256?: string;
    detectionIds?: readonly string[];
  },
): TemporalIncidentReviewResult {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "schema_version",
      "project_id",
      "computation_mode",
      "proposal_count",
      "proposals",
      "state_changes_performed",
      "incidents_created",
      "annotations_created",
      "training_members_created",
      "model_versions_changed",
    ]) ||
    value.schema_version !== "trafficvisson.temporal-review-api.v1" ||
    value.computation_mode !== "pure_review_only" ||
    value.state_changes_performed !== false ||
    value.incidents_created !== 0 ||
    value.annotations_created !== 0 ||
    value.training_members_created !== 0 ||
    value.model_versions_changed !== 0 ||
    !Array.isArray(value.proposals)
  ) {
    throw new Error("시간축 사건 검토 응답의 읽기 전용 계약이 올바르지 않습니다.");
  }
  const projectId = requireUuid(value.project_id, "시간축 응답 프로젝트");
  if (
    expected?.projectId &&
    projectId !== requireUuid(expected.projectId, "요청 프로젝트")
  ) {
    throw new Error("시간축 사건 검토 응답이 다른 프로젝트에 속합니다.");
  }
  const proposalCount = requireCount(
    value.proposal_count,
    "시간축 제안 수",
    100_000,
  );
  if (proposalCount !== value.proposals.length) {
    throw new Error("시간축 제안 집계와 목록이 일치하지 않습니다.");
  }
  const proposals = value.proposals.map(parseTemporalProposal);
  if (
    proposals.some(
      ({ source }) =>
        source.projectId !== projectId ||
        (expected?.analysisJobId !== undefined &&
          source.analysisJobId !==
            requireUuid(expected.analysisJobId, "요청 분석 작업")) ||
        (expected?.sourceFileSha256 !== undefined &&
          source.sourceFileSha256 !==
            requireSha256(expected.sourceFileSha256, "요청 입력 영상")),
    )
  ) {
    throw new Error("시간축 제안 provenance가 요청 원본과 일치하지 않습니다.");
  }
  const proposalIds = proposals.map(({ proposalId }) => proposalId);
  if (new Set(proposalIds).size !== proposalIds.length) {
    throw new Error("시간축 제안 식별자가 중복되었습니다.");
  }
  const allEvidence = proposals.flatMap(
    ({ evidenceDetectionIds }) => evidenceDetectionIds,
  );
  if (new Set(allEvidence).size !== allEvidence.length) {
    throw new Error("하나의 프레임 검지가 여러 사건 제안에 중복되었습니다.");
  }
  if (expected?.detectionIds) {
    const allowed = new Set(expected.detectionIds);
    if (allEvidence.some((detectionId) => !allowed.has(detectionId))) {
      throw new Error("시간축 응답이 요청하지 않은 프레임 근거를 참조합니다.");
    }
  }
  return {
    projectId,
    proposalCount,
    proposals,
    stateChangesPerformed: false,
    incidentsCreated: 0,
    annotationsCreated: 0,
    trainingMembersCreated: 0,
    modelVersionsChanged: 0,
  };
}

/**
 * Replaces each validated temporal group with its peak-evidence question at
 * the position of the group's first frame. Every evidence question id remains
 * attached to the representative so the UI can expose the complete group.
 */
export function buildTemporalQuestionOrder(
  baseQuestionIds: readonly string[],
  detectionToQuestionId: Readonly<Record<string, string>>,
  result: TemporalIncidentReviewResult,
): TemporalQuestionOrder {
  const baseSet = new Set(baseQuestionIds);
  const insertionByQuestionId = new Map<string, string>();
  const groupedQuestionIds = new Set<string>();
  const groupsByRepresentativeId: Record<string, TemporalQuestionGroup> = {};

  for (const proposal of [...result.proposals].sort(
    (left, right) =>
      left.startSeconds - right.startSeconds ||
      left.proposalId.localeCompare(right.proposalId),
  )) {
    const evidenceQuestionIds = proposal.evidenceDetectionIds.map(
      (detectionId) => detectionToQuestionId[detectionId],
    );
    const representativeQuestionId =
      detectionToQuestionId[proposal.peakEvidence.detectionId];
    if (
      !representativeQuestionId ||
      evidenceQuestionIds.some(
        (questionId) =>
          !questionId ||
          !baseSet.has(questionId) ||
          groupedQuestionIds.has(questionId),
      ) ||
      !evidenceQuestionIds.includes(representativeQuestionId)
    ) {
      continue;
    }
    const firstQuestionId = baseQuestionIds.find((questionId) =>
      evidenceQuestionIds.includes(questionId),
    );
    if (!firstQuestionId) continue;
    evidenceQuestionIds.forEach((questionId) =>
      groupedQuestionIds.add(questionId),
    );
    insertionByQuestionId.set(firstQuestionId, representativeQuestionId);
    groupsByRepresentativeId[representativeQuestionId] = {
      proposalId: proposal.proposalId,
      representativeQuestionId,
      evidenceQuestionIds,
      peakEvidenceIndex: evidenceQuestionIds.indexOf(
        representativeQuestionId,
      ),
      startSeconds: proposal.startSeconds,
      endSeconds: proposal.endSeconds,
      durationSeconds: proposal.durationSeconds,
      detectionCount: proposal.detectionCount,
    };
  }

  const orderedQuestionIds: string[] = [];
  for (const questionId of baseQuestionIds) {
    const insertion = insertionByQuestionId.get(questionId);
    if (insertion) orderedQuestionIds.push(insertion);
    if (!groupedQuestionIds.has(questionId)) orderedQuestionIds.push(questionId);
  }
  return { orderedQuestionIds, groupsByRepresentativeId };
}

function normalizeTruthRecords(
  records: Array<{
    sampleId: string;
    issueType: unknown;
    truthEventIds?: unknown;
  }>,
): ValidationTruthRecordRequest[] {
  if (!Array.isArray(records) || records.length > 1_000_000) {
    throw new Error("검증 정답 레코드 수가 허용 범위를 벗어났습니다.");
  }
  const identities = new Set<string>();
  return records.map((raw, index) => {
    const sampleId = requireIdentifier(
      raw.sampleId,
      `검증 표본 ${index + 1}`,
    );
    const issueType = requireCanonicalIssueType(
      raw.issueType,
      `검증 표본 ${index + 1}`,
    );
    if (
      raw.truthEventIds !== undefined &&
      (!Array.isArray(raw.truthEventIds) ||
        raw.truthEventIds.length > 256)
    ) {
      throw new Error(`검증 표본 ${index + 1}의 정답 사건 목록이 올바르지 않습니다.`);
    }
    const truthEventIds = (raw.truthEventIds ?? []).map((item, eventIndex) =>
      requireIdentifier(
        item,
        `검증 표본 ${index + 1} 정답 ${eventIndex + 1}`,
      ),
    );
    if (new Set(truthEventIds).size !== truthEventIds.length) {
      throw new Error(`검증 표본 ${index + 1}의 정답 사건이 중복되었습니다.`);
    }
    const identity = `${issueType}\0${sampleId}`;
    if (identities.has(identity)) {
      throw new Error("동일한 검증 표본과 유형 조합이 중복되었습니다.");
    }
    identities.add(identity);
    return { sampleId, issueType, truthEventIds };
  });
}

function normalizePredictionRecords(
  records: Array<{
    predictionEventId: string;
    sampleId: string;
    issueType: unknown;
    matchedTruthEventId?: unknown;
  }>,
): ValidationPredictionRecordRequest[] {
  if (!Array.isArray(records) || records.length > 2_000_000) {
    throw new Error("검증 예측 레코드 수가 허용 범위를 벗어났습니다.");
  }
  const predictionIds = new Set<string>();
  return records.map((raw, index) => {
    const predictionEventId = requireIdentifier(
      raw.predictionEventId,
      `검증 예측 ${index + 1}`,
    );
    if (predictionIds.has(predictionEventId)) {
      throw new Error("검증 예측 사건 식별자가 중복되었습니다.");
    }
    predictionIds.add(predictionEventId);
    return {
      predictionEventId,
      sampleId: requireIdentifier(
        raw.sampleId,
        `검증 예측 ${index + 1} 표본`,
      ),
      issueType: requireCanonicalIssueType(
        raw.issueType,
        `검증 예측 ${index + 1}`,
      ),
      ...(raw.matchedTruthEventId === undefined
        ? {}
        : {
            matchedTruthEventId: requireIdentifier(
              raw.matchedTruthEventId,
              `검증 예측 ${index + 1} 정답 연결`,
            ),
          }),
    };
  });
}

export function buildModelValidationReviewRequest(input: {
  projectId: string;
  modelVersion: string;
  truthRecords: Array<{
    sampleId: string;
    issueType: unknown;
    truthEventIds?: unknown;
  }>;
  predictionRecords: Array<{
    predictionEventId: string;
    sampleId: string;
    issueType: unknown;
    matchedTruthEventId?: unknown;
  }>;
}): ModelValidationReviewRequest {
  return {
    projectId: requireUuid(input.projectId, "프로젝트"),
    modelVersion: requireIdentifier(input.modelVersion, "모델 버전"),
    truthRecords: normalizeTruthRecords(input.truthRecords),
    predictionRecords: normalizePredictionRecords(input.predictionRecords),
  };
}

export function buildModelPromotionReviewRequest(input: {
  projectId: string;
  activeModelVersion: string;
  candidateModelVersion: string;
  truthRecords: Array<{
    sampleId: string;
    issueType: unknown;
    truthEventIds?: unknown;
  }>;
  activePredictionRecords: Array<{
    predictionEventId: string;
    sampleId: string;
    issueType: unknown;
    matchedTruthEventId?: unknown;
  }>;
  candidatePredictionRecords: Array<{
    predictionEventId: string;
    sampleId: string;
    issueType: unknown;
    matchedTruthEventId?: unknown;
  }>;
  requiredIssueTypes?: unknown[];
  minimumSamplesPerType?: number;
  maximumRecallDrop?: number;
}): ModelPromotionReviewRequest {
  const activeModelVersion = requireIdentifier(
    input.activeModelVersion,
    "활성 모델 버전",
  );
  const candidateModelVersion = requireIdentifier(
    input.candidateModelVersion,
    "후보 모델 버전",
  );
  if (activeModelVersion === candidateModelVersion) {
    throw new Error("활성 모델과 후보 모델 버전은 달라야 합니다.");
  }
  let requiredIssueTypes: CanonicalIssueTypeApi[] | undefined;
  if (input.requiredIssueTypes !== undefined) {
    if (
      !Array.isArray(input.requiredIssueTypes) ||
      input.requiredIssueTypes.length < 1 ||
      input.requiredIssueTypes.length > 12
    ) {
      throw new Error("승격 검토 유형은 1개 이상 12개 이하여야 합니다.");
    }
    requiredIssueTypes = input.requiredIssueTypes.map((value, index) =>
      requireCanonicalIssueType(value, `승격 검토 유형 ${index + 1}`),
    );
    if (new Set(requiredIssueTypes).size !== requiredIssueTypes.length) {
      throw new Error("승격 검토 유형은 중복될 수 없습니다.");
    }
    requiredIssueTypes = CANONICAL_ORDER.filter((issueType) =>
      requiredIssueTypes?.includes(issueType),
    );
  }
  const minimumSamplesPerType = requirePositiveCount(
    input.minimumSamplesPerType ?? 30,
    "유형별 최소 검증 표본",
    Number.MAX_SAFE_INTEGER,
  );
  const maximumRecallDrop = requireBoundedNumber(
    input.maximumRecallDrop ?? 0.02,
    "최대 재현율 하락",
    Number.MIN_VALUE,
    1,
  );
  return {
    projectId: requireUuid(input.projectId, "프로젝트"),
    activeModelVersion,
    candidateModelVersion,
    truthRecords: normalizeTruthRecords(input.truthRecords),
    activePredictionRecords: normalizePredictionRecords(
      input.activePredictionRecords,
    ),
    candidatePredictionRecords: normalizePredictionRecords(
      input.candidatePredictionRecords,
    ),
    ...(requiredIssueTypes ? { requiredIssueTypes } : {}),
    minimumSamplesPerType,
    maximumRecallDrop,
  };
}

function ratio(numerator: number, denominator: number) {
  return denominator === 0 ? 0 : numerator / denominator;
}

function closeEnough(left: number, right: number) {
  return Math.abs(left - right) <= 1e-12;
}

function parsePerTypeMetric(
  value: unknown,
  index: number,
): PerTypeValidationMetrics {
  const label = `유형별 검증 지표 ${index + 1}`;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "issue_type",
      "validation_sample_count",
      "positive_sample_count",
      "negative_sample_count",
      "truth_event_count",
      "prediction_event_count",
      "true_positive_count",
      "false_positive_count",
      "false_negative_count",
      "duplicate_prediction_count",
      "precision",
      "recall",
      "false_positive_rate",
      "duplicate_burden",
    ])
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  const issueType = requireCanonicalIssueType(value.issue_type, label);
  if (issueType !== CANONICAL_ORDER[index]) {
    throw new Error("검증 지표가 표준 12개 유형 순서와 일치하지 않습니다.");
  }
  const validationSampleCount = requireCount(
    value.validation_sample_count,
    `${label} 표본 수`,
    1_000_000,
  );
  const positiveSampleCount = requireCount(
    value.positive_sample_count,
    `${label} 양성 표본 수`,
    1_000_000,
  );
  const negativeSampleCount = requireCount(
    value.negative_sample_count,
    `${label} 음성 표본 수`,
    1_000_000,
  );
  const truthEventCount = requireCount(
    value.truth_event_count,
    `${label} 정답 사건 수`,
    256_000_000,
  );
  const predictionEventCount = requireCount(
    value.prediction_event_count,
    `${label} 예측 사건 수`,
    2_000_000,
  );
  const truePositiveCount = requireCount(
    value.true_positive_count,
    `${label} TP`,
    2_000_000,
  );
  const falsePositiveCount = requireCount(
    value.false_positive_count,
    `${label} FP`,
    2_000_000,
  );
  const falseNegativeCount = requireCount(
    value.false_negative_count,
    `${label} FN`,
    256_000_000,
  );
  const duplicatePredictionCount = requireCount(
    value.duplicate_prediction_count,
    `${label} 중복 예측`,
    2_000_000,
  );
  const precision = requireBoundedNumber(
    value.precision,
    `${label} 정밀도`,
    0,
    1,
  );
  const recall = requireBoundedNumber(
    value.recall,
    `${label} 재현율`,
    0,
    1,
  );
  const falsePositiveRate = requireBoundedNumber(
    value.false_positive_rate,
    `${label} 오탐 비율`,
    0,
    1,
  );
  const duplicateBurden = requireBoundedNumber(
    value.duplicate_burden,
    `${label} 중복 부담`,
    0,
    1,
  );
  if (
    validationSampleCount !== positiveSampleCount + negativeSampleCount ||
    truthEventCount !== truePositiveCount + falseNegativeCount ||
    predictionEventCount !== truePositiveCount + falsePositiveCount ||
    duplicatePredictionCount > falsePositiveCount ||
    !closeEnough(
      precision,
      ratio(truePositiveCount, truePositiveCount + falsePositiveCount),
    ) ||
    !closeEnough(
      recall,
      ratio(truePositiveCount, truePositiveCount + falseNegativeCount),
    ) ||
    !closeEnough(
      falsePositiveRate,
      ratio(falsePositiveCount, predictionEventCount),
    ) ||
    !closeEnough(
      duplicateBurden,
      ratio(duplicatePredictionCount, predictionEventCount),
    )
  ) {
    throw new Error(`${label}의 집계와 비율이 서로 일치하지 않습니다.`);
  }
  return {
    issueType,
    validationSampleCount,
    positiveSampleCount,
    negativeSampleCount,
    truthEventCount,
    predictionEventCount,
    truePositiveCount,
    falsePositiveCount,
    falseNegativeCount,
    duplicatePredictionCount,
    precision,
    recall,
    falsePositiveRate,
    duplicateBurden,
  };
}

function parseValidationReport(
  value: unknown,
  label: string,
): ModelValidationReport {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "schema_version",
      "model_version",
      "validation_set_sha256",
      "prediction_set_sha256",
      "per_type",
    ]) ||
    value.schema_version !== "trafficvisson.validation-metrics.v1" ||
    !Array.isArray(value.per_type) ||
    value.per_type.length !== 12
  ) {
    throw new Error(`${label} 보고서 형식이 올바르지 않습니다.`);
  }
  return {
    modelVersion: requireIdentifier(value.model_version, `${label} 모델`),
    validationSetSha256: requireSha256(
      value.validation_set_sha256,
      `${label} 검증 세트`,
    ),
    predictionSetSha256: requireSha256(
      value.prediction_set_sha256,
      `${label} 예측 세트`,
    ),
    perType: value.per_type.map(parsePerTypeMetric),
  };
}

export function parseModelValidationReviewResponse(
  value: unknown,
  expected?: { projectId?: string; modelVersion?: string },
): ModelValidationReviewResult {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "schema_version",
      "project_id",
      "computation_mode",
      "report",
      "state_changes_performed",
      "annotations_created",
      "training_members_created",
      "model_versions_changed",
    ]) ||
    value.schema_version !==
      "trafficvisson.model-validation-review-api.v1" ||
    value.computation_mode !== "pure_review_only" ||
    value.state_changes_performed !== false ||
    value.annotations_created !== 0 ||
    value.training_members_created !== 0 ||
    value.model_versions_changed !== 0
  ) {
    throw new Error("모델 검증 응답의 읽기 전용 계약이 올바르지 않습니다.");
  }
  const projectId = requireUuid(value.project_id, "모델 검증 프로젝트");
  const report = parseValidationReport(value.report, "모델 검증");
  if (
    expected?.projectId &&
    projectId !== requireUuid(expected.projectId, "요청 프로젝트")
  ) {
    throw new Error("모델 검증 응답이 다른 프로젝트에 속합니다.");
  }
  if (
    expected?.modelVersion &&
    report.modelVersion !==
      requireIdentifier(expected.modelVersion, "요청 모델")
  ) {
    throw new Error("모델 검증 응답의 모델 버전이 요청과 다릅니다.");
  }
  return {
    projectId,
    report,
    stateChangesPerformed: false,
    annotationsCreated: 0,
    trainingMembersCreated: 0,
    modelVersionsChanged: 0,
  };
}

function parsePromotionReason(
  value: unknown,
  index: number,
): PromotionReviewReason {
  const label = `승격 검토 사유 ${index + 1}`;
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "code",
      "issue_type",
      "message",
      "active_value",
      "candidate_value",
      "limit_value",
    ]) ||
    typeof value.code !== "string" ||
    !PROMOTION_REASON_CODES.has(value.code) ||
    typeof value.message !== "string" ||
    value.message.length < 1 ||
    value.message.length > 500
  ) {
    throw new Error(`${label} 형식이 올바르지 않습니다.`);
  }
  const optionalFinite = (raw: unknown, field: string) => {
    if (raw === null) return null;
    if (typeof raw !== "number" || !Number.isFinite(raw)) {
      throw new Error(`${label}의 ${field} 값이 올바르지 않습니다.`);
    }
    return raw;
  };
  return {
    code: value.code as PromotionReviewReason["code"],
    issueType: requireCanonicalIssueType(value.issue_type, label),
    message: value.message,
    activeValue: optionalFinite(value.active_value, "활성 값"),
    candidateValue: optionalFinite(value.candidate_value, "후보 값"),
    limitValue: optionalFinite(value.limit_value, "기준 값"),
  };
}

export function parseModelPromotionReviewResponse(
  value: unknown,
  expected?: {
    projectId?: string;
    activeModelVersion?: string;
    candidateModelVersion?: string;
  },
): ModelPromotionReviewResult {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "schema_version",
      "project_id",
      "computation_mode",
      "decision",
      "state_changes_performed",
      "annotations_created",
      "training_members_created",
      "model_versions_changed",
      "automatic_promotion_performed",
    ]) ||
    value.schema_version !==
      "trafficvisson.model-promotion-review-api.v1" ||
    value.computation_mode !== "pure_review_only" ||
    value.state_changes_performed !== false ||
    value.annotations_created !== 0 ||
    value.training_members_created !== 0 ||
    value.model_versions_changed !== 0 ||
    value.automatic_promotion_performed !== false
  ) {
    throw new Error("모델 승격 검토 응답의 읽기 전용 계약이 올바르지 않습니다.");
  }
  const projectId = requireUuid(value.project_id, "모델 승격 프로젝트");
  const raw = value.decision;
  if (
    !isRecord(raw) ||
    !hasExactKeys(raw, [
      "schema_version",
      "decision",
      "active_model_version",
      "candidate_model_version",
      "validation_set_sha256",
      "required_issue_types",
      "minimum_samples_per_type",
      "maximum_recall_drop",
      "active_report",
      "candidate_report",
      "reasons",
      "auto_promotion_allowed",
      "requires_user_review",
    ]) ||
    raw.schema_version !== "trafficvisson.promotion-review-policy.v1" ||
    (raw.decision !== "eligible" && raw.decision !== "rejected") ||
    raw.auto_promotion_allowed !== false ||
    raw.requires_user_review !== true ||
    !Array.isArray(raw.required_issue_types) ||
    raw.required_issue_types.length < 1 ||
    raw.required_issue_types.length > 12 ||
    !Array.isArray(raw.reasons)
  ) {
    throw new Error("모델 승격 검토 결정 형식이 올바르지 않습니다.");
  }
  const activeModelVersion = requireIdentifier(
    raw.active_model_version,
    "활성 모델",
  );
  const candidateModelVersion = requireIdentifier(
    raw.candidate_model_version,
    "후보 모델",
  );
  const requiredIssueTypes = raw.required_issue_types.map((item, index) =>
    requireCanonicalIssueType(item, `필수 검증 유형 ${index + 1}`),
  );
  const canonicalRequiredOrder = CANONICAL_ORDER.filter((item) =>
    requiredIssueTypes.includes(item),
  );
  if (
    new Set(requiredIssueTypes).size !== requiredIssueTypes.length ||
    requiredIssueTypes.some(
      (item, index) => item !== canonicalRequiredOrder[index],
    )
  ) {
    throw new Error("필수 검증 유형이 표준 순서와 일치하지 않습니다.");
  }
  const activeReport = parseValidationReport(
    raw.active_report,
    "활성 모델 검증",
  );
  const candidateReport = parseValidationReport(
    raw.candidate_report,
    "후보 모델 검증",
  );
  const validationSetSha256 = requireSha256(
    raw.validation_set_sha256,
    "승격 검증 세트",
  );
  const minimumSamplesPerType = requirePositiveCount(
    raw.minimum_samples_per_type,
    "유형별 최소 표본",
    Number.MAX_SAFE_INTEGER,
  );
  const maximumRecallDrop = requireBoundedNumber(
    raw.maximum_recall_drop,
    "최대 재현율 하락",
    Number.MIN_VALUE,
    1,
  );
  const reasons = raw.reasons.map(parsePromotionReason);
  if (
    activeModelVersion === candidateModelVersion ||
    activeReport.modelVersion !== activeModelVersion ||
    candidateReport.modelVersion !== candidateModelVersion ||
    activeReport.validationSetSha256 !== validationSetSha256 ||
    candidateReport.validationSetSha256 !== validationSetSha256 ||
    (raw.decision === "eligible" && reasons.length !== 0) ||
    (raw.decision === "rejected" && reasons.length === 0)
  ) {
    throw new Error("모델 승격 검토 결정의 보고서와 사유가 일치하지 않습니다.");
  }
  const activeByType = new Map(
    activeReport.perType.map((metric) => [metric.issueType, metric]),
  );
  const candidateByType = new Map(
    candidateReport.perType.map((metric) => [metric.issueType, metric]),
  );
  const ratioIncreased = (
    candidateNumerator: number,
    candidateDenominator: number,
    activeNumerator: number,
    activeDenominator: number,
  ) => {
    if (candidateDenominator === 0) return false;
    if (activeDenominator === 0) return candidateNumerator > 0;
    return (
      candidateNumerator * activeDenominator >
      activeNumerator * candidateDenominator
    );
  };
  const absoluteOrSampleRateIncreased = (
    candidateCount: number,
    candidateSampleCount: number,
    activeCount: number,
    activeSampleCount: number,
  ) =>
    candidateCount > activeCount ||
    ratioIncreased(
      candidateCount,
      candidateSampleCount,
      activeCount,
      activeSampleCount,
    );
  const expectedReasonKeys: string[] = [];
  for (const issueType of requiredIssueTypes) {
    const active = activeByType.get(issueType);
    const candidate = candidateByType.get(issueType);
    if (!active || !candidate) {
      throw new Error("승격 검토 보고서에 필수 유형이 없습니다.");
    }
    if (active.validationSampleCount < minimumSamplesPerType) {
      expectedReasonKeys.push(
        `insufficient_validation_samples:${issueType}`,
      );
    }
    if (active.truthEventCount === 0) {
      expectedReasonKeys.push(`missing_positive_truth:${issueType}`);
    }
    if (
      active.truthEventCount > 0 &&
      active.recall - candidate.recall >= maximumRecallDrop - 1e-15
    ) {
      expectedReasonKeys.push(
        `recall_drop_at_or_above_limit:${issueType}`,
      );
    }
    if (
      absoluteOrSampleRateIncreased(
        candidate.falsePositiveCount,
        candidate.validationSampleCount,
        active.falsePositiveCount,
        active.validationSampleCount,
      ) ||
      ratioIncreased(
        candidate.falsePositiveCount,
        candidate.predictionEventCount,
        active.falsePositiveCount,
        active.predictionEventCount,
      )
    ) {
      expectedReasonKeys.push(`false_positive_rate_increased:${issueType}`);
    }
    if (
      absoluteOrSampleRateIncreased(
        candidate.duplicatePredictionCount,
        candidate.validationSampleCount,
        active.duplicatePredictionCount,
        active.validationSampleCount,
      ) ||
      ratioIncreased(
        candidate.duplicatePredictionCount,
        candidate.predictionEventCount,
        active.duplicatePredictionCount,
        active.predictionEventCount,
      )
    ) {
      expectedReasonKeys.push(`duplicate_burden_increased:${issueType}`);
    }
  }
  const receivedReasonKeys = reasons.map(
    ({ code, issueType }) => `${code}:${issueType}`,
  );
  const expectedDecision =
    expectedReasonKeys.length === 0 ? "eligible" : "rejected";
  if (
    raw.decision !== expectedDecision ||
    receivedReasonKeys.length !== expectedReasonKeys.length ||
    receivedReasonKeys.some(
      (reasonKey, index) => reasonKey !== expectedReasonKeys[index],
    )
  ) {
    throw new Error("모델 승격 검토 사유가 고정 안전 정책과 일치하지 않습니다.");
  }
  if (
    expected?.projectId &&
    projectId !== requireUuid(expected.projectId, "요청 프로젝트")
  ) {
    throw new Error("모델 승격 검토 응답이 다른 프로젝트에 속합니다.");
  }
  if (
    expected?.activeModelVersion &&
    activeModelVersion !==
      requireIdentifier(expected.activeModelVersion, "요청 활성 모델")
  ) {
    throw new Error("모델 승격 응답의 활성 모델 버전이 요청과 다릅니다.");
  }
  if (
    expected?.candidateModelVersion &&
    candidateModelVersion !==
      requireIdentifier(expected.candidateModelVersion, "요청 후보 모델")
  ) {
    throw new Error("모델 승격 응답의 후보 모델 버전이 요청과 다릅니다.");
  }
  return {
    projectId,
    decision: {
      decision: raw.decision,
      activeModelVersion,
      candidateModelVersion,
      validationSetSha256,
      requiredIssueTypes,
      minimumSamplesPerType,
      maximumRecallDrop,
      activeReport,
      candidateReport,
      reasons,
      autoPromotionAllowed: false,
      requiresUserReview: true,
    },
    stateChangesPerformed: false,
    annotationsCreated: 0,
    trainingMembersCreated: 0,
    modelVersionsChanged: 0,
    automaticPromotionPerformed: false,
  };
}
