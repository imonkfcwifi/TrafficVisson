import type {
  ActiveImport,
  AnalysisCandidate,
  AnalysisCandidateInference,
  AnalysisSummary,
} from "./runtimeState";
import { normalizeIssueTypeApi } from "./issueTypes";

type AnalysisNormalizedBoxResponse = {
  x: number;
  y: number;
  width: number;
  height: number;
};

type AnalysisCandidateInferenceResponse = {
  schema_version: "trafficvisson.vision-inference.v1";
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
  pack_id: string | null;
  pack_version: string | null;
  manifest_sha256: string | null;
  capabilities: Array<
    "defect_detection" | "candidate_classification" | "ocr"
  >;
  predictions: Array<{
    issue_type: string;
    confidence: number;
    box: AnalysisNormalizedBoxResponse | null;
    source_label: string;
    calibration: "model_score" | "zero_shot_relative";
  }>;
  ocr_observations: Array<{
    text: string;
    confidence: number;
    box: AnalysisNormalizedBoxResponse;
    kind: "sign_or_tunnel_text_unverified";
  }>;
  duration_ms: number;
  warning_code:
    | "model_pack_not_configured"
    | "model_pack_invalid"
    | "model_timeout"
    | "model_inference_failed"
    | null;
};

export type AnalysisJobResponse = {
  job_id: string;
  project_id: string;
  snapshot_id: string;
  hash_job_id: string;
  file_name: string;
  source_sha256: string;
  status:
    | "queued"
    | "running"
    | "cancelling"
    | "completed"
    | "cancelled"
    | "failed";
  stage:
    | "queued"
    | "validating_source"
    | "extracting_candidates"
    | "cancelling"
    | "completed"
    | "cancelled"
    | "failed";
  progress_percent: number | null;
  pause_supported: false;
  error_code:
    | "source_changed"
    | "timeout"
    | "output_limit"
    | "extraction_failed"
    | "internal_error"
    | null;
  created_at: string;
  updated_at: string;
  result: null | {
    artifact_set_id: string;
    candidate_count: number;
    artifact_count: number;
    total_artifact_bytes: number;
    provenance: {
      source_size_bytes: number;
      duration_seconds: number;
      source_location?: null | {
        latitude: number;
        longitude: number;
        altitude_meters: number | null;
        raw_iso6709: string;
        scope: "capture_start_only";
      };
    };
    candidates: Array<{
      candidate_id: string;
      start_seconds: number;
      end_seconds: number;
      representative_timestamp_seconds: number;
      scene_score: number;
      sharpness_score: number;
      triggers: string[];
      observations: Array<{
        timestamp_seconds: number;
        scene_score: number;
        sharpness_score: number;
        triggers: string[];
      }>;
      representative_image: {
        relative_path: string;
        size_bytes: number;
        sha256: string;
        media_type: string;
        artifact_ref: string;
      };
      preview_clip: {
        relative_path: string;
        size_bytes: number;
        sha256: string;
        media_type: string;
        artifact_ref: string;
      };
      preview_start_seconds: number;
      preview_duration_seconds: number;
      /**
       * Older persisted jobs may omit this field. FastAPI's response model
       * serializes the optional backend contract as null when it is absent.
       */
      ai_inference?: AnalysisCandidateInferenceResponse | null;
      candidate_kind?: "dense_candidate_evidence_overlay" | null;
      dense_proposal?: null | {
        issue_type: string;
        score: number;
        normalized_box: AnalysisNormalizedBoxResponse | null;
        ocr_text: string | null;
        urgent_review: boolean;
        proposal_sources: string[];
      };
      evidence_overlay?: null | {
        schema_version: "trafficvisson.candidate-evidence-overlay.v1";
        overlay_id: string;
        pipeline_job_id: string;
        manifest_sha256: string;
        discovery_revision: string;
        dense_index_sha256: string;
        provider_provenance_sha256: string;
        model_provenance_sha256: string;
        candidate_set_metadata_sha256: string;
        review_only: true;
      };
    }>;
  };
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA_PATTERN = /^[a-f0-9]{64}$/;
const CANDIDATE_PATTERN = /^[a-f0-9]{20}$/;
const RELATIVE_ARTIFACT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$/;
const CANDIDATE_EVIDENCE_ARTIFACT_PATTERN =
  /^candidate-evidence:(ceo_[a-f0-9]{32}):cea_[a-f0-9]{32}$/;
const MAX_MEDIA_SECONDS = 2_678_400;
const ERROR_CODES = new Set([
  "source_changed",
  "timeout",
  "output_limit",
  "extraction_failed",
  "internal_error",
]);
const VISION_PROVIDERS = new Set([
  "none",
  "onnx_yolo_v8",
  "transformers_grounding_dino",
  "transformers_clip_zero_shot",
  "easyocr_ocr_only",
]);
const VISION_CAPABILITIES = new Set([
  "defect_detection",
  "candidate_classification",
  "ocr",
]);
const VISION_WARNINGS = new Set([
  "model_pack_not_configured",
  "model_pack_invalid",
  "model_timeout",
  "model_inference_failed",
]);
const PACK_ID_PATTERN = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const PACK_VERSION_PATTERN = /^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function boundedNumber(
  value: unknown,
  minimum: number,
  maximum: number,
): value is number {
  return finiteNumber(value) && value >= minimum && value <= maximum;
}

function stringArray(
  value: unknown,
  { minimum = 0, maximum = 512 } = {},
): value is string[] {
  return (
    Array.isArray(value) &&
    value.length >= minimum &&
    value.length <= maximum &&
    value.every((item) => typeof item === "string")
  );
}

function validArtifact(
  value: unknown,
  jobId: string,
  expectedMediaType: "image/jpeg" | "video/mp4",
) {
  if (
    !isRecord(value) ||
    typeof value.relative_path !== "string" ||
    !RELATIVE_ARTIFACT_PATTERN.test(value.relative_path) ||
    !Number.isInteger(value.size_bytes) ||
    Number(value.size_bytes) <= 0 ||
    typeof value.sha256 !== "string" ||
    !SHA_PATTERN.test(value.sha256) ||
    value.media_type !== expectedMediaType ||
    !(
      value.artifact_ref ===
        `analysis:${jobId}:${value.relative_path}` ||
      CANDIDATE_EVIDENCE_ARTIFACT_PATTERN.test(
        String(value.artifact_ref),
      )
    )
  ) {
    return false;
  }
  return (
    expectedMediaType === "image/jpeg"
      ? value.relative_path.toLowerCase().endsWith(".jpg")
      : value.relative_path.toLowerCase().endsWith(".mp4")
  );
}

function validNormalizedBox(value: unknown) {
  if (
    !isRecord(value) ||
    !boundedNumber(value.x, 0, 1) ||
    !boundedNumber(value.y, 0, 1) ||
    !boundedNumber(value.width, Number.MIN_VALUE, 1) ||
    !boundedNumber(value.height, Number.MIN_VALUE, 1)
  ) {
    return false;
  }
  return (
    Number(value.x) + Number(value.width) <= 1.00000001 &&
    Number(value.y) + Number(value.height) <= 1.00000001
  );
}

function validVisionPrediction(value: unknown) {
  return (
    isRecord(value) &&
    normalizeIssueTypeApi(value.issue_type) !== null &&
    boundedNumber(value.confidence, 0, 1) &&
    (value.box === null || validNormalizedBox(value.box)) &&
    typeof value.source_label === "string" &&
    value.source_label.length >= 1 &&
    value.source_label.length <= 128 &&
    (value.calibration === "model_score" ||
      value.calibration === "zero_shot_relative")
  );
}

function validOcrObservation(value: unknown) {
  return (
    isRecord(value) &&
    typeof value.text === "string" &&
    value.text.length >= 1 &&
    value.text.length <= 500 &&
    boundedNumber(value.confidence, 0, 1) &&
    validNormalizedBox(value.box) &&
    value.kind === "sign_or_tunnel_text_unverified"
  );
}

function validCandidateInference(value: unknown) {
  if (value === undefined || value === null) return true;
  if (
    !isRecord(value) ||
    value.schema_version !== "trafficvisson.vision-inference.v1" ||
    !["local_model", "scene_fallback"].includes(String(value.mode)) ||
    ![
      "classified",
      "no_detection",
      "provider_unavailable",
      "timed_out",
      "failed",
    ].includes(String(value.status)) ||
    typeof value.provider !== "string" ||
    !VISION_PROVIDERS.has(value.provider) ||
    !(
      value.pack_id === null ||
      (typeof value.pack_id === "string" &&
        PACK_ID_PATTERN.test(value.pack_id))
    ) ||
    !(
      value.pack_version === null ||
      (typeof value.pack_version === "string" &&
        PACK_VERSION_PATTERN.test(value.pack_version))
    ) ||
    !(
      value.manifest_sha256 === null ||
      (typeof value.manifest_sha256 === "string" &&
        SHA_PATTERN.test(value.manifest_sha256))
    ) ||
    !Array.isArray(value.capabilities) ||
    value.capabilities.length > 3 ||
    !value.capabilities.every(
      (item) =>
        typeof item === "string" && VISION_CAPABILITIES.has(item),
    ) ||
    !Array.isArray(value.predictions) ||
    value.predictions.length > 32 ||
    !value.predictions.every(validVisionPrediction) ||
    !Array.isArray(value.ocr_observations) ||
    value.ocr_observations.length > 32 ||
    !value.ocr_observations.every(validOcrObservation) ||
    !Number.isInteger(value.duration_ms) ||
    Number(value.duration_ms) < 0 ||
    Number(value.duration_ms) > 3_600_000 ||
    !(
      value.warning_code === null ||
      (typeof value.warning_code === "string" &&
        VISION_WARNINGS.has(value.warning_code))
    )
  ) {
    return false;
  }

  if (value.mode === "local_model") {
    return (
      value.provider !== "none" &&
      value.pack_id !== null &&
      value.pack_version !== null &&
      value.manifest_sha256 !== null &&
      value.warning_code === null &&
      (value.status === "classified" || value.status === "no_detection")
    );
  }

  return (
    value.predictions.length === 0 &&
    value.ocr_observations.length === 0 &&
    value.warning_code !== null &&
    (value.status === "provider_unavailable" ||
      value.status === "timed_out" ||
      value.status === "failed")
  );
}

function validDenseCandidateEvidence(value: Record<string, unknown>) {
  const kind = value.candidate_kind;
  const proposal = value.dense_proposal;
  const evidence = value.evidence_overlay;
  if (kind === undefined || kind === null) {
    return (
      (proposal === undefined || proposal === null) &&
      (evidence === undefined || evidence === null)
    );
  }
  if (
    kind !== "dense_candidate_evidence_overlay" ||
    !isRecord(proposal) ||
    normalizeIssueTypeApi(proposal.issue_type) === null ||
    !boundedNumber(proposal.score, 0, 1) ||
    !(
      proposal.normalized_box === null ||
      validNormalizedBox(proposal.normalized_box)
    ) ||
    !(
      proposal.ocr_text === null ||
      (typeof proposal.ocr_text === "string" &&
        proposal.ocr_text.trim().length > 0 &&
        proposal.ocr_text.length <= 500)
    ) ||
    typeof proposal.urgent_review !== "boolean" ||
    !stringArray(proposal.proposal_sources, {
      minimum: 1,
      maximum: 8,
    }) ||
    !isRecord(evidence) ||
    evidence.schema_version !==
      "trafficvisson.candidate-evidence-overlay.v1" ||
    typeof evidence.overlay_id !== "string" ||
    !/^ceo_[a-f0-9]{32}$/.test(evidence.overlay_id) ||
    typeof evidence.pipeline_job_id !== "string" ||
    !UUID_PATTERN.test(evidence.pipeline_job_id) ||
    typeof evidence.manifest_sha256 !== "string" ||
    !SHA_PATTERN.test(evidence.manifest_sha256) ||
    typeof evidence.discovery_revision !== "string" ||
    !SHA_PATTERN.test(evidence.discovery_revision) ||
    typeof evidence.dense_index_sha256 !== "string" ||
    !SHA_PATTERN.test(evidence.dense_index_sha256) ||
    typeof evidence.provider_provenance_sha256 !== "string" ||
    !SHA_PATTERN.test(evidence.provider_provenance_sha256) ||
    typeof evidence.model_provenance_sha256 !== "string" ||
    !SHA_PATTERN.test(evidence.model_provenance_sha256) ||
    typeof evidence.candidate_set_metadata_sha256 !== "string" ||
    !SHA_PATTERN.test(evidence.candidate_set_metadata_sha256) ||
    evidence.review_only !== true
  ) {
    return false;
  }
  return true;
}

function validCandidate(value: unknown, jobId: string) {
  if (
    !isRecord(value) ||
    typeof value.candidate_id !== "string" ||
    !CANDIDATE_PATTERN.test(value.candidate_id) ||
    !boundedNumber(value.start_seconds, 0, MAX_MEDIA_SECONDS) ||
    !boundedNumber(value.end_seconds, 0, MAX_MEDIA_SECONDS) ||
    !boundedNumber(
      value.representative_timestamp_seconds,
      0,
      MAX_MEDIA_SECONDS,
    ) ||
    !boundedNumber(value.scene_score, 0, 1) ||
    !boundedNumber(value.sharpness_score, 0, 1) ||
    !stringArray(value.triggers, { minimum: 1, maximum: 8 }) ||
    !Array.isArray(value.observations) ||
    value.observations.length < 1 ||
    value.observations.length > 512 ||
    !validArtifact(value.representative_image, jobId, "image/jpeg") ||
    !validArtifact(value.preview_clip, jobId, "video/mp4") ||
    !boundedNumber(value.preview_start_seconds, 0, MAX_MEDIA_SECONDS) ||
    !boundedNumber(value.preview_duration_seconds, 0, 5) ||
    !validCandidateInference(value.ai_inference) ||
    !validDenseCandidateEvidence(value)
  ) {
    return false;
  }
  const image = value.representative_image;
  const clip = value.preview_clip;
  if (!isRecord(image) || !isRecord(clip)) return false;
  if (value.candidate_kind === "dense_candidate_evidence_overlay") {
    const evidence = value.evidence_overlay;
    if (!isRecord(evidence)) return false;
    const prefix = `candidate-evidence:${String(
      evidence.overlay_id,
    )}:`;
    if (
      typeof image.artifact_ref !== "string" ||
      typeof clip.artifact_ref !== "string" ||
      !image.artifact_ref.startsWith(prefix) ||
      !clip.artifact_ref.startsWith(prefix)
    ) {
      return false;
    }
  } else if (
    image.artifact_ref !==
      `analysis:${jobId}:${String(image.relative_path)}` ||
    clip.artifact_ref !==
      `analysis:${jobId}:${String(clip.relative_path)}`
  ) {
    return false;
  }
  return value.observations.every(
    (observation) =>
      isRecord(observation) &&
      boundedNumber(observation.timestamp_seconds, 0, MAX_MEDIA_SECONDS) &&
      boundedNumber(observation.scene_score, 0, 1) &&
      boundedNumber(observation.sharpness_score, 0, 1) &&
      stringArray(observation.triggers, { minimum: 1, maximum: 8 }),
  );
}

function validTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length <= 64 &&
    Number.isFinite(Date.parse(value))
  );
}

function validResult(value: unknown, jobId: string) {
  if (
    !isRecord(value) ||
    typeof value.artifact_set_id !== "string" ||
    !UUID_PATTERN.test(value.artifact_set_id) ||
    !Number.isInteger(value.candidate_count) ||
    Number(value.candidate_count) < 0 ||
    Number(value.candidate_count) > 512 ||
    !Number.isInteger(value.artifact_count) ||
    Number(value.artifact_count) < 0 ||
    Number(value.artifact_count) > 1024 ||
    !Number.isInteger(value.total_artifact_bytes) ||
    Number(value.total_artifact_bytes) < 0 ||
    !isRecord(value.provenance) ||
    !Number.isInteger(value.provenance.source_size_bytes) ||
    Number(value.provenance.source_size_bytes) <= 0 ||
    !boundedNumber(
      value.provenance.duration_seconds,
      Number.MIN_VALUE,
      MAX_MEDIA_SECONDS,
    ) ||
    !validSourceLocation(value.provenance.source_location) ||
    !Array.isArray(value.candidates) ||
    value.candidates.length > 512 ||
    !value.candidates.every((candidate) => validCandidate(candidate, jobId))
  ) {
    return false;
  }
  const candidateCount = Number(value.candidate_count);
  const artifactCount = Number(value.artifact_count);
  const calculatedBytes = value.candidates.reduce((total, candidate) => {
    if (!isRecord(candidate)) return Number.NaN;
    const image = candidate.representative_image;
    const clip = candidate.preview_clip;
    if (!isRecord(image) || !isRecord(clip)) return Number.NaN;
    return total + Number(image.size_bytes) + Number(clip.size_bytes);
  }, 0);
  return (
    candidateCount === value.candidates.length &&
    artifactCount === candidateCount * 2 &&
    Number(value.total_artifact_bytes) === calculatedBytes
  );
}

function validSourceLocation(value: unknown) {
  if (value === undefined || value === null) return true;
  return (
    isRecord(value) &&
    boundedNumber(value.latitude, -90, 90) &&
    boundedNumber(value.longitude, -180, 180) &&
    !(Number(value.latitude) === 0 && Number(value.longitude) === 0) &&
    (value.altitude_meters === null ||
      boundedNumber(value.altitude_meters, -20_000, 20_000)) &&
    typeof value.raw_iso6709 === "string" &&
    value.raw_iso6709.length >= 8 &&
    value.raw_iso6709.length <= 64 &&
    value.scope === "capture_start_only"
  );
}

export function parseAnalysisJob(value: unknown): AnalysisJobResponse {
  const validStatuses = [
    "queued",
    "running",
    "cancelling",
    "completed",
    "cancelled",
    "failed",
  ];
  const validStages = [
    "queued",
    "validating_source",
    "extracting_candidates",
    "cancelling",
    "completed",
    "cancelled",
    "failed",
  ];
  if (
    !isRecord(value) ||
    typeof value.job_id !== "string" ||
    !UUID_PATTERN.test(value.job_id) ||
    typeof value.project_id !== "string" ||
    !UUID_PATTERN.test(value.project_id) ||
    typeof value.snapshot_id !== "string" ||
    !UUID_PATTERN.test(value.snapshot_id) ||
    typeof value.hash_job_id !== "string" ||
    !UUID_PATTERN.test(value.hash_job_id) ||
    typeof value.file_name !== "string" ||
    value.file_name.length < 1 ||
    value.file_name.length > 255 ||
    typeof value.source_sha256 !== "string" ||
    !SHA_PATTERN.test(value.source_sha256) ||
    !validStatuses.includes(String(value.status)) ||
    !validStages.includes(String(value.stage)) ||
    !(
      value.progress_percent === null ||
      (Number.isInteger(value.progress_percent) &&
        Number(value.progress_percent) >= 0 &&
        Number(value.progress_percent) <= 100)
    ) ||
    value.pause_supported !== false ||
    !(
      value.error_code === null ||
      (typeof value.error_code === "string" &&
        ERROR_CODES.has(value.error_code))
    ) ||
    !validTimestamp(value.created_at) ||
    !validTimestamp(value.updated_at)
  ) {
    throw new Error("분석 작업 응답 형식을 검증하지 못했습니다.");
  }
  if (value.result !== null && !validResult(value.result, value.job_id)) {
    throw new Error("분석 결과 응답 형식을 검증하지 못했습니다.");
  }
  if (
    (value.status === "completed" &&
      (value.stage !== "completed" ||
        value.progress_percent !== 100 ||
        value.result === null ||
        value.error_code !== null)) ||
    (value.status === "failed" &&
      (value.stage !== "failed" ||
        value.result !== null ||
        value.error_code === null)) ||
    (value.status === "cancelled" &&
      (value.stage !== "cancelled" ||
        value.result !== null ||
        value.error_code !== null)) ||
    (!["completed", "failed", "cancelled"].includes(String(value.status)) &&
      (value.result !== null || value.error_code !== null))
  ) {
    throw new Error("분석 작업 상태와 결과가 일치하지 않습니다.");
  }
  return value as AnalysisJobResponse;
}

export function parseAnalysisJobList(value: unknown): AnalysisJobResponse[] {
  if (!isRecord(value) || !Array.isArray(value.jobs) || value.jobs.length > 128) {
    throw new Error("분석 기록 목록 형식을 검증하지 못했습니다.");
  }
  return value.jobs.map(parseAnalysisJob);
}

export function firstProjectId(value: unknown): string | null {
  if (!isRecord(value) || !Array.isArray(value.projects)) return null;
  for (const project of value.projects) {
    if (
      isRecord(project) &&
      typeof project.id === "string" &&
      UUID_PATTERN.test(project.id)
    ) {
      return project.id.toLowerCase();
    }
  }
  return null;
}

export function toRuntimeCandidate(
  candidate: NonNullable<
    AnalysisJobResponse["result"]
  >["candidates"][number],
): AnalysisCandidate {
  const toArtifact = (
    artifact: typeof candidate.representative_image,
  ) => ({
    relativePath: artifact.relative_path,
    sizeBytes: artifact.size_bytes,
    sha256: artifact.sha256,
    mediaType: artifact.media_type,
    artifactRef: artifact.artifact_ref,
  });
  const toInference = (
    inference: AnalysisCandidateInferenceResponse,
  ): AnalysisCandidateInference => ({
    schemaVersion: inference.schema_version,
    mode: inference.mode,
    status: inference.status,
    provider: inference.provider,
    packId: inference.pack_id,
    packVersion: inference.pack_version,
    manifestSha256: inference.manifest_sha256,
    capabilities: inference.capabilities,
    predictions: inference.predictions.map((prediction) => ({
      issueType: normalizeIssueTypeApi(prediction.issue_type)!,
      confidence: prediction.confidence,
      box: prediction.box,
      sourceLabel: prediction.source_label,
      calibration: prediction.calibration,
    })),
    ocrObservations: inference.ocr_observations.map((observation) => ({
      text: observation.text,
      confidence: observation.confidence,
      box: observation.box,
      kind: observation.kind,
    })),
    durationMs: inference.duration_ms,
    warningCode: inference.warning_code,
  });
  return {
    candidateId: candidate.candidate_id,
    startSeconds: candidate.start_seconds,
    endSeconds: candidate.end_seconds,
    representativeTimestampSeconds:
      candidate.representative_timestamp_seconds,
    sceneScore: candidate.scene_score,
    sharpnessScore: candidate.sharpness_score,
    triggers: candidate.triggers,
    observations: candidate.observations.map((observation) => ({
      timestampSeconds: observation.timestamp_seconds,
      sceneScore: observation.scene_score,
      sharpnessScore: observation.sharpness_score,
      triggers: observation.triggers,
    })),
    representativeImage: toArtifact(candidate.representative_image),
    previewClip: toArtifact(candidate.preview_clip),
    previewStartSeconds: candidate.preview_start_seconds,
    previewDurationSeconds: candidate.preview_duration_seconds,
    ...(candidate.ai_inference
      ? { aiInference: toInference(candidate.ai_inference) }
      : {}),
    ...(candidate.candidate_kind ===
      "dense_candidate_evidence_overlay" &&
    candidate.dense_proposal &&
    candidate.evidence_overlay
      ? {
          candidateKind: candidate.candidate_kind,
          denseProposal: {
            issueType: normalizeIssueTypeApi(
              candidate.dense_proposal.issue_type,
            )!,
            score: candidate.dense_proposal.score,
            normalizedBox:
              candidate.dense_proposal.normalized_box,
            ocrText: candidate.dense_proposal.ocr_text,
            urgentReview:
              candidate.dense_proposal.urgent_review,
            proposalSources:
              candidate.dense_proposal.proposal_sources,
          },
          evidenceOverlay: {
            schemaVersion:
              candidate.evidence_overlay.schema_version,
            overlayId: candidate.evidence_overlay.overlay_id,
            pipelineJobId:
              candidate.evidence_overlay.pipeline_job_id,
            manifestSha256:
              candidate.evidence_overlay.manifest_sha256,
            discoveryRevision:
              candidate.evidence_overlay.discovery_revision,
            denseIndexSha256:
              candidate.evidence_overlay.dense_index_sha256,
            providerProvenanceSha256:
              candidate.evidence_overlay
                .provider_provenance_sha256,
            modelProvenanceSha256:
              candidate.evidence_overlay
                .model_provenance_sha256,
            candidateSetMetadataSha256:
              candidate.evidence_overlay
                .candidate_set_metadata_sha256,
            reviewOnly: true as const,
          },
        }
      : {}),
  };
}

export function toRestoredRuntime(job: AnalysisJobResponse): {
  activeImport: ActiveImport;
  analysisSummary: AnalysisSummary;
} {
  if (job.status !== "completed" || job.result === null) {
    throw new Error("완료된 분석 작업만 복구할 수 있습니다.");
  }
  return {
    activeImport: {
      projectId: job.project_id,
      fileName: job.file_name,
      sizeBytes: job.result.provenance.source_size_bytes,
      durationSeconds: job.result.provenance.duration_seconds,
      snapshotId: job.snapshot_id,
      sha256: job.source_sha256,
      hashJobId: job.hash_job_id,
      registeredAt: job.updated_at,
    },
    analysisSummary: {
      artifactSetId: job.result.artifact_set_id,
      projectId: job.project_id,
      jobId: job.job_id,
      sourceFileName: job.file_name,
      sourceSha256: job.source_sha256,
      candidateCount: job.result.candidate_count,
      artifactCount: job.result.artifact_count,
      totalArtifactBytes: job.result.total_artifact_bytes,
      completedAt: job.updated_at,
      candidates: job.result.candidates.map(toRuntimeCandidate),
      sourceLocation: job.result.provenance.source_location
        ? {
            latitude: job.result.provenance.source_location.latitude,
            longitude: job.result.provenance.source_location.longitude,
            altitudeMeters:
              job.result.provenance.source_location.altitude_meters,
            rawIso6709: job.result.provenance.source_location.raw_iso6709,
            scope: job.result.provenance.source_location.scope,
          }
        : null,
    },
  };
}
