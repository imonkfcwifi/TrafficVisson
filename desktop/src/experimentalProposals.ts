import type {
  AnalysisCandidate,
  AnalysisCandidateInference,
  AnalysisNormalizedBox,
  AnalysisVisionIssueType,
} from "./runtimeState";
import { normalizeIssueTypeApi } from "./issueTypes";

export type ExperimentalProposalOverlay = {
  runId: string;
  createdAt: string;
  modelIdentity: string;
  inference: AnalysisCandidateInference;
};

export type ExperimentalProposalOverlayResult = {
  byCandidateId: Record<string, ExperimentalProposalOverlay>;
  declaredCount: number;
  ignoredCount: number;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA_PATTERN = /^[a-f0-9]{64}$/;
const PACK_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const PACK_VERSION_PATTERN = /^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$/;
const DATASET_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,127}$/;
const DATASET_VERSION_PATTERN = /^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$/;
const EVALUATION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const PRODUCER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
type ExperimentalProposalProvider =
  | "onnx_yolo_v8"
  | "transformers_grounding_dino"
  | "transformers_clip_zero_shot";

const MODEL_PROVIDERS = new Set<ExperimentalProposalProvider>([
  "onnx_yolo_v8",
  "transformers_grounding_dino",
  "transformers_clip_zero_shot",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function unitScore(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= 0 &&
    value <= 1
  );
}

function normalizedBox(value: unknown): AnalysisNormalizedBox | null {
  if (value === undefined || value === null) return null;
  if (!isRecord(value)) return null;
  const { x, y, width, height } = value;
  if (
    !unitScore(x) ||
    !unitScore(y) ||
    !unitScore(width) ||
    !unitScore(height) ||
    width <= 0 ||
    height <= 0 ||
    x + width > 1.00000001 ||
    y + height > 1.00000001
  ) {
    return null;
  }
  return { x, y, width, height };
}

function validOptionalDataset(value: unknown) {
  if (value === undefined || value === null) return true;
  if (!isRecord(value)) return false;
  return (
    typeof value.dataset_id === "string" &&
    DATASET_ID_PATTERN.test(value.dataset_id) &&
    typeof value.dataset_version === "string" &&
    DATASET_VERSION_PATTERN.test(value.dataset_version) &&
    typeof value.dataset_sha256 === "string" &&
    SHA_PATTERN.test(value.dataset_sha256)
  );
}

function parseOverlay(
  value: unknown,
  expected: {
    projectId: string;
    jobId: string;
    candidate: AnalysisCandidate;
  },
): ExperimentalProposalOverlay | null {
  if (!isRecord(value)) return null;
  const model = value.model;
  const evaluation = value.evaluation;
  const source = value.source;
  const predictions = value.predictions;
  if (
    value.schema_version !==
      "trafficvisson.experimental-candidate-proposal.v1" ||
    typeof value.run_id !== "string" ||
    !UUID_PATTERN.test(value.run_id) ||
    value.project_id !== expected.projectId ||
    value.analysis_job_id !== expected.jobId ||
    value.candidate_id !== expected.candidate.candidateId ||
    typeof value.created_at !== "string" ||
    !Number.isFinite(Date.parse(value.created_at)) ||
    (value.status !== "classified" && value.status !== "no_suggestion") ||
    value.review_only !== true ||
    value.automatic_labeling_allowed !== false ||
    !isRecord(model) ||
    typeof model.provider !== "string" ||
    !MODEL_PROVIDERS.has(model.provider as ExperimentalProposalProvider) ||
    typeof model.pack_id !== "string" ||
    !PACK_ID_PATTERN.test(model.pack_id) ||
    typeof model.pack_version !== "string" ||
    !PACK_VERSION_PATTERN.test(model.pack_version) ||
    typeof model.manifest_sha256 !== "string" ||
    !SHA_PATTERN.test(model.manifest_sha256) ||
    !validOptionalDataset(value.dataset) ||
    !isRecord(evaluation) ||
    typeof evaluation.evaluation_id !== "string" ||
    !EVALUATION_ID_PATTERN.test(evaluation.evaluation_id) ||
    typeof evaluation.producer !== "string" ||
    !PRODUCER_PATTERN.test(evaluation.producer) ||
    typeof evaluation.producer_version !== "string" ||
    !PACK_VERSION_PATTERN.test(evaluation.producer_version) ||
    !isRecord(source) ||
    source.representative_image_sha256 !==
      expected.candidate.representativeImage.sha256 ||
    typeof source.analysis_candidate_sha256 !== "string" ||
    !SHA_PATTERN.test(source.analysis_candidate_sha256) ||
    !Array.isArray(predictions) ||
    predictions.length > 32
  ) {
    return null;
  }

  const provider = model.provider as ExperimentalProposalProvider;
  const expectedCalibration: "zero_shot_relative" | "model_score" =
    provider === "transformers_clip_zero_shot"
      ? "zero_shot_relative"
      : "model_score";
  const expectedSemantics =
    expectedCalibration === "zero_shot_relative"
      ? "zero_shot_relative_not_probability"
      : "model_score_not_probability";
  if (value.score_semantics !== expectedSemantics) return null;

  const parsedPredictions = predictions.flatMap((raw) => {
    const normalizedIssueType = isRecord(raw)
      ? normalizeIssueTypeApi(raw.issue_type)
      : null;
    if (
      !isRecord(raw) ||
      normalizedIssueType === null ||
      typeof raw.source_label !== "string" ||
      raw.source_label.length < 1 ||
      raw.source_label.length > 128 ||
      raw.calibration !== expectedCalibration
    ) {
      return [];
    }
    if (!Object.prototype.hasOwnProperty.call(raw, "box")) return [];
    const box = normalizedBox(raw.box);
    const confidence =
      expectedCalibration === "zero_shot_relative"
        ? raw.relative_score
        : raw.model_score;
    if (
      !unitScore(confidence) ||
      (expectedCalibration === "zero_shot_relative"
        ? raw.model_score !== null || raw.box !== null
        : raw.relative_score !== null || box === null)
    ) {
      return [];
    }
    return [
      {
        issueType: normalizedIssueType as AnalysisVisionIssueType,
        confidence,
        box,
        sourceLabel: raw.source_label,
        calibration: expectedCalibration,
      },
    ];
  });
  if (
    parsedPredictions.length !== predictions.length ||
    parsedPredictions.some(
      (prediction, index) =>
        index > 0 &&
        parsedPredictions[index - 1].confidence < prediction.confidence,
    ) ||
    (value.status === "classified" && parsedPredictions.length === 0) ||
    (value.status === "no_suggestion" && parsedPredictions.length !== 0)
  ) {
    return null;
  }

  return {
    runId: value.run_id.toLowerCase(),
    createdAt: value.created_at,
    modelIdentity: `${model.pack_id}@${model.pack_version}`,
    inference: {
      schemaVersion: "trafficvisson.vision-inference.v1",
      mode: "local_model",
      status: value.status === "classified" ? "classified" : "no_detection",
      provider,
      packId: model.pack_id,
      packVersion: model.pack_version,
      manifestSha256: model.manifest_sha256,
      capabilities:
        provider === "onnx_yolo_v8" ||
        provider === "transformers_grounding_dino"
          ? ["defect_detection"]
          : ["candidate_classification"],
      predictions: parsedPredictions,
      ocrObservations: [],
      durationMs: 0,
      warningCode: null,
    },
  };
}

/**
 * Reads review-only model suggestions from the label-question response.
 *
 * Older responses have no `experimental_proposal`; malformed or mismatched
 * proposals are ignored so a review card safely falls back to the immutable
 * scene candidate. Nothing in this parser creates an answer or annotation.
 */
export function experimentalProposalOverlaysFromQuestionList(
  value: unknown,
  context: {
    projectId: string;
    jobId: string;
    candidates: AnalysisCandidate[];
  },
): ExperimentalProposalOverlayResult {
  const empty = {
    byCandidateId: {},
    declaredCount: 0,
    ignoredCount: 0,
  } satisfies ExperimentalProposalOverlayResult;
  if (!isRecord(value) || !Array.isArray(value.questions)) return empty;
  if (
    (typeof value.project_id === "string" &&
      value.project_id !== context.projectId) ||
    (typeof value.analysis_job_id === "string" &&
      value.analysis_job_id !== context.jobId)
  ) {
    return empty;
  }

  const candidates = new Map(
    context.candidates.map((candidate) => [candidate.candidateId, candidate]),
  );
  const byCandidateId: Record<string, ExperimentalProposalOverlay> = {};
  let declaredCount = 0;
  let ignoredCount = 0;
  for (const rawQuestion of value.questions.slice(0, 512)) {
    if (!isRecord(rawQuestion)) continue;
    const rawProposal = rawQuestion.experimental_proposal;
    if (rawProposal === undefined || rawProposal === null) continue;
    declaredCount += 1;
    const candidateId = rawQuestion.candidate_id;
    if (typeof candidateId !== "string") {
      ignoredCount += 1;
      continue;
    }
    const candidate = candidates.get(candidateId);
    const responseCandidate = rawQuestion.candidate;
    if (
      !candidate ||
      !isRecord(responseCandidate) ||
      responseCandidate.candidate_id !== candidateId ||
      !isRecord(responseCandidate.representative_image) ||
      responseCandidate.representative_image.sha256 !==
        candidate.representativeImage.sha256 ||
      Object.prototype.hasOwnProperty.call(byCandidateId, candidateId)
    ) {
      ignoredCount += 1;
      continue;
    }
    const overlay = parseOverlay(rawProposal, {
      projectId: context.projectId,
      jobId: context.jobId,
      candidate,
    });
    if (!overlay) {
      ignoredCount += 1;
      continue;
    }
    byCandidateId[candidateId] = overlay;
  }

  return { byCandidateId, declaredCount, ignoredCount };
}
