import { useSyncExternalStore } from "react";
import type { SupportedIssueTypeApi } from "./issueTypes";

export type RuntimeStatus =
  | "idle"
  | "intake_ready"
  | "queued"
  | "running"
  | "paused"
  | "cancelled"
  | "complete"
  | "failed";

export type ActiveImport = {
  projectId: string;
  fileName: string;
  sizeBytes: number;
  durationSeconds: number;
  snapshotId: string;
  snapshotDigest?: string;
  sha256: string;
  hashJobId?: string;
  registeredAt: string;
};

export type AnalysisArtifact = {
  relativePath: string;
  sizeBytes: number;
  sha256: string;
  mediaType: string;
  artifactRef: string;
};

export type AnalysisCandidateObservation = {
  timestampSeconds: number;
  sceneScore: number;
  sharpnessScore: number;
  triggers: string[];
};

export type AnalysisNormalizedBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type AnalysisVisionIssueType = SupportedIssueTypeApi;

export type AnalysisVisionPrediction = {
  issueType: AnalysisVisionIssueType;
  confidence: number;
  box: AnalysisNormalizedBox | null;
  sourceLabel: string;
  calibration: "model_score" | "zero_shot_relative";
};

export type AnalysisOcrObservation = {
  text: string;
  confidence: number;
  box: AnalysisNormalizedBox;
  kind: "sign_or_tunnel_text_unverified";
};

export type AnalysisCandidateInference = {
  schemaVersion: "trafficvisson.vision-inference.v1";
  mode: "local_model" | "scene_fallback";
  status:
    | "classified"
    | "no_detection"
    | "provider_unavailable"
    | "timed_out"
    | "failed";
  provider:
    | "none"
    | "onnx_yolo_v8"
    | "transformers_grounding_dino"
    | "transformers_clip_zero_shot"
    | "easyocr_ocr_only";
  packId: string | null;
  packVersion: string | null;
  manifestSha256: string | null;
  capabilities: Array<
    "defect_detection" | "candidate_classification" | "ocr"
  >;
  predictions: AnalysisVisionPrediction[];
  ocrObservations: AnalysisOcrObservation[];
  durationMs: number;
  warningCode:
    | "model_pack_not_configured"
    | "model_pack_invalid"
    | "model_timeout"
    | "model_inference_failed"
    | null;
};

export type DenseCandidateProposal = {
  issueType: AnalysisVisionIssueType;
  score: number;
  normalizedBox: AnalysisNormalizedBox | null;
  ocrText: string | null;
  urgentReview: boolean;
  proposalSources: string[];
};

export type CandidateEvidenceOverlayIdentity = {
  schemaVersion: "trafficvisson.candidate-evidence-overlay.v1";
  overlayId: string;
  pipelineJobId: string;
  manifestSha256: string;
  discoveryRevision: string;
  denseIndexSha256: string;
  providerProvenanceSha256: string;
  modelProvenanceSha256: string;
  candidateSetMetadataSha256: string;
  reviewOnly: true;
};

export type AnalysisCandidate = {
  candidateId: string;
  startSeconds: number;
  endSeconds: number;
  representativeTimestampSeconds: number;
  sceneScore: number;
  sharpnessScore: number;
  triggers: string[];
  observations: AnalysisCandidateObservation[];
  representativeImage: AnalysisArtifact;
  previewClip: AnalysisArtifact;
  previewStartSeconds: number;
  previewDurationSeconds: number;
  /**
   * Optional for analysis records created before the v1 inference contract.
   * Its presence is only a model proposal; it is never an approved incident.
   */
  aiInference?: AnalysisCandidateInference;
  candidateKind?: "dense_candidate_evidence_overlay";
  denseProposal?: DenseCandidateProposal;
  evidenceOverlay?: CandidateEvidenceOverlayIdentity;
};

export type SourceLocation = {
  latitude: number;
  longitude: number;
  altitudeMeters: number | null;
  rawIso6709: string;
  scope: "capture_start_only";
};

export type AnalysisSummary = {
  artifactSetId: string;
  projectId: string;
  jobId: string;
  sourceFileName: string;
  sourceSha256: string;
  candidateCount: number;
  artifactCount: number;
  totalArtifactBytes: number;
  completedAt: string;
  candidates: AnalysisCandidate[];
  sourceLocation: SourceLocation | null;
};

export type RuntimeState = {
  activeImport: ActiveImport | null;
  status: RuntimeStatus;
  progress: number;
  stage: number;
  jobId: string | null;
  message: string;
  analysisSummary: AnalysisSummary | null;
};

const STORAGE_KEY = "trafficvisson:runtime:v1";
const listeners = new Set<() => void>();

function initialState(): RuntimeState {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    if (value) {
      const parsed = JSON.parse(value) as Partial<RuntimeState>;
      if (parsed && typeof parsed === "object") {
        return {
          activeImport: parsed.activeImport ?? null,
          status: parsed.status ?? "idle",
          progress: Number.isFinite(parsed.progress) ? Number(parsed.progress) : 0,
          stage: Number.isInteger(parsed.stage) ? Number(parsed.stage) : 0,
          jobId: parsed.jobId ?? null,
          message: parsed.message ?? "입력 대기",
          analysisSummary:
            parsed.analysisSummary &&
            Array.isArray(parsed.analysisSummary.candidates) &&
            typeof parsed.analysisSummary.projectId === "string" &&
            typeof parsed.analysisSummary.jobId === "string"
              ? parsed.analysisSummary
              : null,
        };
      }
    }
  } catch {
    // A malformed local cache must never prevent the desktop shell from opening.
  }
  return {
    activeImport: null,
    status: "idle",
    progress: 0,
    stage: 0,
    jobId: null,
    message: "입력 대기",
    analysisSummary: null,
  };
}

let state = initialState();

function publish(next: RuntimeState) {
  state = next;
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  listeners.forEach((listener) => listener());
}

export function getRuntimeState() {
  return state;
}

export function subscribeRuntime(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useRuntimeState() {
  return useSyncExternalStore(subscribeRuntime, getRuntimeState, getRuntimeState);
}

export function registerImport(activeImport: ActiveImport) {
  publish({
    activeImport,
    status: "intake_ready",
    progress: 0,
    stage: 0,
    jobId: null,
    message: "분석 시작 대기",
    analysisSummary: null,
  });
}

export function restoreCompletedAnalysis(
  activeImport: ActiveImport,
  analysisSummary: AnalysisSummary,
) {
  publish({
    activeImport,
    status: "complete",
    progress: 100,
    stage: 3,
    jobId: analysisSummary.jobId,
    message: `후보 ${analysisSummary.candidateCount}건 · 저장된 분석 복구`,
    analysisSummary,
  });
}

export function startAnalysis(jobId?: string) {
  if (!state.activeImport) return;
  publish({
    ...state,
    status: "running",
    progress: state.status === "cancelled" ? state.progress : Math.max(0, state.progress),
    stage: Math.max(1, state.stage),
    jobId: jobId ?? state.jobId,
    message: "장면·선명도 후보 추출",
  });
}

export function updateAnalysis(
  patch: Partial<
    Pick<
      RuntimeState,
      "status" | "progress" | "stage" | "message" | "jobId" | "analysisSummary"
    >
  >,
) {
  publish({
    ...state,
    ...patch,
    progress: Math.max(0, Math.min(100, patch.progress ?? state.progress)),
  });
}

export function clearRuntime() {
  publish({
    activeImport: null,
    status: "idle",
    progress: 0,
    stage: 0,
    jobId: null,
    message: "입력 대기",
    analysisSummary: null,
  });
}
