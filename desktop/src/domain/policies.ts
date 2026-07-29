import type {
  AnnotationBundle,
  AnnotationSource,
  ConditionAssessment,
  DatasetVersion,
  GeometryAnnotation,
  IncidentType,
  LabelAnswer,
  LabelQuestion,
  LearningArtifact,
  MaintenanceDecision,
  MandatoryQuestionReason,
  ModelVersion,
  OcrGroundTruth,
  ProjectScoped,
  QuestionPolicyMode,
  Severity,
} from "./types";

export interface ProjectScopedReference extends ProjectScoped {
  entityType: string;
  entityId: string;
}

export interface ProjectIsolationResult {
  ok: boolean;
  expectedProjectId: string;
  violations: ProjectScopedReference[];
}

export function evaluateProjectIsolation(
  expectedProjectId: string,
  references: ProjectScopedReference[],
): ProjectIsolationResult {
  const violations = references.filter(
    (reference) => reference.projectId !== expectedProjectId,
  );

  return {
    ok: violations.length === 0,
    expectedProjectId,
    violations,
  };
}

export function getMandatoryQuestionReasons(
  question: LabelQuestion,
): MandatoryQuestionReason[] {
  const reasons = new Set(question.mandatoryReasons);

  if (question.proposedClass === "ROAD_DEBRIS") {
    reasons.add("ROAD_DEBRIS");
  }
  if (question.proposedSeverity === "URGENT") {
    reasons.add("URGENT_SEVERITY");
  }

  return [...reasons];
}

export function isMandatoryQuestion(question: LabelQuestion): boolean {
  return getMandatoryQuestionReasons(question).length > 0;
}

export interface QuestionQueueContext {
  policyMode: QuestionPolicyMode;
  reviewedCountForClass: number;
  recentAgreementRate: number;
  confidenceThreshold: number;
  modelDisagreement: boolean;
  newCaptureEnvironment: boolean;
  qualitySampleSelected: boolean;
}

export function shouldQueueQuestion(
  question: LabelQuestion,
  context: QuestionQueueContext,
): boolean {
  if (isMandatoryQuestion(question)) {
    return true;
  }
  if (context.policyMode === "REVIEW_ALL") {
    return true;
  }

  const uncertain = question.confidence < context.confidenceThreshold;
  if (context.policyMode === "REVIEW_UNCERTAIN") {
    return uncertain || context.modelDisagreement;
  }

  if (context.reviewedCountForClass < 30) {
    return true;
  }
  if (context.recentAgreementRate < 0.9) {
    return true;
  }

  return (
    uncertain ||
    context.modelDisagreement ||
    context.newCaptureEnvironment ||
    context.qualitySampleSelected
  );
}

function sourceForAnswer(answer: LabelAnswer): AnnotationSource {
  if (answer.response === "REJECT") {
    return "USER_NEGATIVE";
  }
  if (
    answer.response === "EDIT_GEOMETRY" ||
    answer.response === "CHANGE_CLASS"
  ) {
    return "USER_EDITED";
  }
  return "AI_ACCEPTED";
}

function conditionForIncidentType(
  incidentType: IncidentType,
): ConditionAssessment["condition"] {
  if (
    incidentType === "SIGN_VEGETATION_OCCLUSION" ||
    incidentType === "TUNNEL_NAME_OCCLUSION"
  ) {
    return "OCCLUDED";
  }
  if (
    incidentType === "LANE_MARKING_FADED" ||
    incidentType === "COLORED_GUIDANCE_FADED"
  ) {
    return "FADED";
  }
  if (incidentType === "ROADSIDE_MOWING_REQUIRED") {
    return "OVERGROWN";
  }
  if (incidentType === "ROAD_DEBRIS") {
    return "OBSTRUCTION";
  }
  return "DAMAGED";
}

function artifactBase(
  question: LabelQuestion,
  answer: LabelAnswer,
): {
  projectId: string;
  createdAt: string;
  updatedAt: string;
  revision: number;
  questionId: string;
  answerId: string;
  incidentId: string;
  findingId: string;
} {
  return {
    projectId: question.projectId,
    createdAt: answer.updatedAt,
    updatedAt: answer.updatedAt,
    revision: 1,
    questionId: question.questionId,
    answerId: answer.answerId,
    incidentId: question.incidentId,
    findingId: question.findingId,
  };
}

function ensureCompatibleAnswer(
  question: LabelQuestion,
  answer: LabelAnswer,
): void {
  if (question.projectId !== answer.projectId) {
    throw new Error(
      `Project mismatch: question ${question.projectId}, answer ${answer.projectId}`,
    );
  }
  if (question.questionId !== answer.questionId) {
    throw new Error(
      `Question mismatch: expected ${question.questionId}, got ${answer.questionId}`,
    );
  }
}

export function convertAnswerToAnnotations(
  question: LabelQuestion,
  answer: LabelAnswer,
): AnnotationBundle {
  ensureCompatibleAnswer(question, answer);

  if (answer.response === "UNSURE" || !answer.includeInTraining) {
    return {
      projectId: question.projectId,
      questionId: question.questionId,
      answerId: answer.answerId,
      artifacts: [],
      excludedFromTraining: true,
      exclusionReason:
        answer.response === "UNSURE" ? "UNSURE" : "USER_EXCLUDED",
    };
  }

  const artifacts: LearningArtifact[] = [];
  const common = artifactBase(question, answer);
  const selectedClass = answer.selectedClass ?? question.proposedClass;
  const source = sourceForAnswer(answer);
  const positive = answer.response !== "REJECT";

  if (question.annotationIntents.includes("GEOMETRY")) {
    const geometry = answer.finalGeometry ?? question.proposedGeometry;
    const frameAssetId = question.mediaRefs[0];

    if (!frameAssetId) {
      throw new Error("Geometry annotation requires a frame media reference");
    }
    if (positive && !geometry) {
      throw new Error("Positive geometry annotation requires geometry");
    }

    const annotation: GeometryAnnotation = {
      ...common,
      annotationId: `ga_${answer.answerId}`,
      tripId: question.tripId,
      incidentType: selectedClass,
      verdict: positive ? "POSITIVE" : "NEGATIVE",
      geometry: positive ? geometry : undefined,
      frameAssetId,
      source,
      reviewState: "CONFIRMED",
    };
    artifacts.push(annotation);
  }

  if (question.annotationIntents.includes("CONDITION")) {
    const assessment: ConditionAssessment = {
      ...common,
      assessmentId: `ca_${answer.answerId}`,
      condition: conditionForIncidentType(selectedClass),
      outcome: positive ? "PRESENT" : "ABSENT",
      severity: positive
        ? (answer.selectedSeverity ?? question.proposedSeverity)
        : undefined,
      source,
      reviewState: "CONFIRMED",
    };
    artifacts.push(assessment);
  }

  if (question.annotationIntents.includes("MAINTENANCE")) {
    const decision: MaintenanceDecision = {
      ...common,
      decisionId: `md_${answer.answerId}`,
      required: positive,
      action: positive
        ? (answer.selectedAction ?? question.proposedMaintenanceAction)
        : undefined,
      severity: positive
        ? (answer.selectedSeverity ?? question.proposedSeverity)
        : undefined,
      decidedBy: answer.answeredBy,
      reviewState: "CONFIRMED",
    };
    artifacts.push(decision);
  }

  if (question.annotationIntents.includes("OCR")) {
    const correctedText = answer.ocrText?.trim();
    const proposedText = question.proposedOcrText?.trim();

    if (positive && !correctedText && !proposedText) {
      throw new Error("Accepted OCR answer requires proposed or corrected text");
    }

    if (positive || correctedText) {
      const groundTruth: OcrGroundTruth = {
        ...common,
        ocrGroundTruthId: `ocr_${answer.answerId}`,
        rawText: proposedText ?? correctedText ?? "",
        normalizedText: correctedText ?? proposedText,
        language: "ko",
        verificationStatus: "MANUAL_CONFIRMED",
        reviewState: "CONFIRMED",
      };
      artifacts.push(groundTruth);
    }
  }

  return {
    projectId: question.projectId,
    questionId: question.questionId,
    answerId: answer.answerId,
    artifacts,
    excludedFromTraining: artifacts.length === 0,
    exclusionReason: artifacts.length === 0 ? "USER_EXCLUDED" : undefined,
  };
}

export interface TrainingGateInput {
  projectId: string;
  newConfirmedLabelCount: number;
  targetClasses: IncidentType[];
  validationSamplesByClass: Partial<Record<IncidentType, number>>;
  hasFixedValidationSet: boolean;
  hasFixedTestSet: boolean;
  datasetProjectId: string;
  minimumNewLabels?: number;
  minimumValidationSamplesPerClass?: number;
}

export type TrainingGateReasonCode =
  | "INSUFFICIENT_NEW_LABELS"
  | "INSUFFICIENT_VALIDATION_SAMPLES"
  | "MISSING_FIXED_VALIDATION_SET"
  | "MISSING_FIXED_TEST_SET"
  | "PROJECT_MISMATCH"
  | "NO_TARGET_CLASSES";

export interface TrainingGateReason {
  code: TrainingGateReasonCode;
  message: string;
  incidentType?: IncidentType;
}

export interface TrainingGateResult {
  canTrain: boolean;
  reasons: TrainingGateReason[];
}

export function evaluateTrainingGate(
  input: TrainingGateInput,
): TrainingGateResult {
  const minimumNewLabels = input.minimumNewLabels ?? 100;
  const minimumValidationSamples = input.minimumValidationSamplesPerClass ?? 30;
  const reasons: TrainingGateReason[] = [];

  if (input.targetClasses.length === 0) {
    reasons.push({
      code: "NO_TARGET_CLASSES",
      message: "Select at least one target class.",
    });
  }
  if (input.newConfirmedLabelCount < minimumNewLabels) {
    reasons.push({
      code: "INSUFFICIENT_NEW_LABELS",
      message: `Need ${minimumNewLabels} confirmed labels; found ${input.newConfirmedLabelCount}.`,
    });
  }
  for (const incidentType of input.targetClasses) {
    const count = input.validationSamplesByClass[incidentType] ?? 0;
    if (count < minimumValidationSamples) {
      reasons.push({
        code: "INSUFFICIENT_VALIDATION_SAMPLES",
        incidentType,
        message: `${incidentType} needs ${minimumValidationSamples} validation incidents; found ${count}.`,
      });
    }
  }
  if (!input.hasFixedValidationSet) {
    reasons.push({
      code: "MISSING_FIXED_VALIDATION_SET",
      message: "A frozen validation set is required.",
    });
  }
  if (!input.hasFixedTestSet) {
    reasons.push({
      code: "MISSING_FIXED_TEST_SET",
      message: "A frozen test set is required.",
    });
  }
  if (input.datasetProjectId !== input.projectId) {
    reasons.push({
      code: "PROJECT_MISMATCH",
      message: "The dataset belongs to another project.",
    });
  }

  return { canTrain: reasons.length === 0, reasons };
}

export type PromotionBlockCode =
  | "PROJECT_MISMATCH"
  | "NOT_A_CANDIDATE"
  | "MISSING_CLASS_METRICS"
  | "INSUFFICIENT_TEST_SAMPLE"
  | "RECALL_REGRESSION"
  | "DUPLICATE_RATE_INCREASE"
  | "FALSE_POSITIVE_RATE_INCREASE";

export interface PromotionBlock {
  code: PromotionBlockCode;
  incidentType?: IncidentType;
  message: string;
}

export interface PromotionPolicyOptions {
  targetClasses?: IncidentType[];
  maximumRecallRegression?: number;
  minimumTestIncidents?: number;
  metricTolerance?: number;
}

export interface PromotionDecision {
  canPromote: boolean;
  blocks: PromotionBlock[];
}

export function evaluateModelPromotion(
  active: ModelVersion,
  candidate: ModelVersion,
  options: PromotionPolicyOptions = {},
): PromotionDecision {
  const blocks: PromotionBlock[] = [];
  const targetClasses =
    options.targetClasses ??
    candidate.classMetrics.map((metric) => metric.incidentType);
  const maximumRecallRegression = options.maximumRecallRegression ?? 0.02;
  const minimumTestIncidents = options.minimumTestIncidents ?? 50;
  const tolerance = options.metricTolerance ?? 0.000001;

  if (active.projectId !== candidate.projectId) {
    blocks.push({
      code: "PROJECT_MISMATCH",
      message: "Active and candidate models belong to different projects.",
    });
  }
  if (candidate.state !== "CANDIDATE") {
    blocks.push({
      code: "NOT_A_CANDIDATE",
      message: `Expected CANDIDATE state; found ${candidate.state}.`,
    });
  }

  for (const incidentType of targetClasses) {
    const activeMetric = active.classMetrics.find(
      (metric) => metric.incidentType === incidentType,
    );
    const candidateMetric = candidate.classMetrics.find(
      (metric) => metric.incidentType === incidentType,
    );

    if (!activeMetric || !candidateMetric) {
      blocks.push({
        code: "MISSING_CLASS_METRICS",
        incidentType,
        message: `${incidentType} metrics are missing from one of the models.`,
      });
      continue;
    }

    if (candidateMetric.testIncidentCount < minimumTestIncidents) {
      blocks.push({
        code: "INSUFFICIENT_TEST_SAMPLE",
        incidentType,
        message: `${incidentType} needs ${minimumTestIncidents} test incidents; found ${candidateMetric.testIncidentCount}.`,
      });
    }

    const recallRegression = activeMetric.recall - candidateMetric.recall;
    if (recallRegression + tolerance >= maximumRecallRegression) {
      blocks.push({
        code: "RECALL_REGRESSION",
        incidentType,
        message: `${incidentType} recall regressed by ${(recallRegression * 100).toFixed(1)} percentage points.`,
      });
    }
    if (
      candidateMetric.duplicateRate >
      activeMetric.duplicateRate + tolerance
    ) {
      blocks.push({
        code: "DUPLICATE_RATE_INCREASE",
        incidentType,
        message: `${incidentType} duplicate rate increased.`,
      });
    }
    if (
      candidateMetric.falsePositiveRate >
      activeMetric.falsePositiveRate + tolerance
    ) {
      blocks.push({
        code: "FALSE_POSITIVE_RATE_INCREASE",
        incidentType,
        message: `${incidentType} false-positive rate increased.`,
      });
    }
  }

  return { canPromote: blocks.length === 0, blocks };
}

export function datasetBelongsToProject(
  dataset: DatasetVersion,
  projectId: string,
): boolean {
  return dataset.projectId === projectId;
}

export function severityRank(severity: Severity): number {
  return {
    LOW: 0,
    MEDIUM: 1,
    HIGH: 2,
    URGENT: 3,
  }[severity];
}
