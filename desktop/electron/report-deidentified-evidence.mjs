import { createHash } from "node:crypto";

const UUID_PATTERN =
  /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const CANDIDATE_ID_PATTERN = /^[a-f0-9]{20}$/;
const DERIVATIVE_ID_PATTERN = /^rde_[a-f0-9]{32}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const DEIDENTIFIED_REF_PATTERN =
  /^report-deid:[a-f0-9]{32}:[a-f0-9]{32}$/;

const PREPARATION_KEYS = Object.freeze([
  "schema_version",
  "project_id",
  "pipeline_job_id",
  "analysis_job_id",
  "source_sha256",
  "incident_id",
  "candidate_id",
  "derivative_id",
  "deidentified_ref",
  "state",
  "input_revision",
  "manifest_sha256",
  "original_sha256",
  "deidentified_sha256",
  "media_type",
  "size_bytes",
  "width",
  "height",
  "detector",
  "detector_identity_sha256",
  "redaction_regions",
  "redaction_region_count",
  "face_detection_count",
  "plate_detection_count",
  "automatic_redaction_performed",
  "automatic_privacy_verification",
  "human_verified",
  "review_required",
  "reviewer_decision",
  "reviewer_actor",
  "reviewed_at",
  "review_revision",
  "review_record_sha256",
  "deidentification_record_sha256",
  "path_exposed",
  "preparation_revision",
]);

const DETECTOR_KEYS = Object.freeze([
  "detector_name",
  "detector_version",
  "model_id",
  "model_sha256",
  "coverage_guaranteed",
  "human_review_required",
]);

const REGION_KEYS = Object.freeze([
  "kind",
  "x",
  "y",
  "width",
  "height",
]);

function object(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}

function exactKeys(value, keys, label) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new TypeError(`${label} has unexpected fields`);
  }
}

function identity(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}

function boundedInteger(value, minimum, maximum, label) {
  if (
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}

function nullableHash(value, label) {
  return value === null
    ? null
    : identity(value, SHA256_PATTERN, label);
}

function nullableText(value, expected, label) {
  if (value === null) return null;
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 128 ||
    (expected && value !== expected)
  ) {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}

export function sanitizeReportDeidentifiedPreparation(
  raw,
  {
    projectId,
    jobId,
    incidentId,
    derivativeId,
  } = {},
) {
  const source = object(raw, "report deidentified preparation");
  exactKeys(
    source,
    PREPARATION_KEYS,
    "report deidentified preparation",
  );
  if (
    source.schema_version !==
      "trafficvisson.report-deidentified-evidence.v1" ||
    source.path_exposed !== false ||
    source.media_type !== "image/jpeg" ||
    source.automatic_privacy_verification !== false
  ) {
    throw new TypeError("report deidentified preparation is unsafe");
  }
  const project = identity(source.project_id, UUID_PATTERN, "project_id");
  const job = identity(
    source.pipeline_job_id,
    UUID_PATTERN,
    "pipeline_job_id",
  );
  const analysis = identity(
    source.analysis_job_id,
    UUID_PATTERN,
    "analysis_job_id",
  );
  const incident = identity(
    source.incident_id,
    UUID_PATTERN,
    "incident_id",
  );
  const candidate = identity(
    source.candidate_id,
    CANDIDATE_ID_PATTERN,
    "candidate_id",
  );
  const derivative = identity(
    source.derivative_id,
    DERIVATIVE_ID_PATTERN,
    "derivative_id",
  );
  if (
    (projectId && project !== projectId) ||
    (jobId && job !== jobId) ||
    (incidentId && incident !== incidentId) ||
    (derivativeId && derivative !== derivativeId)
  ) {
    throw new TypeError(
      "report deidentified preparation identity changed",
    );
  }
  const expectedRef = `report-deid:${incident.replaceAll("-", "")}:${derivative.slice(4)}`;
  const deidentifiedRef = identity(
    source.deidentified_ref,
    DEIDENTIFIED_REF_PATTERN,
    "deidentified_ref",
  );
  if (deidentifiedRef !== expectedRef) {
    throw new TypeError("report deidentified reference changed");
  }
  const width = boundedInteger(source.width, 16, 4096, "width");
  const height = boundedInteger(source.height, 16, 4096, "height");
  const count = boundedInteger(
    source.redaction_region_count,
    0,
    4096,
    "redaction_region_count",
  );
  if (
    !Array.isArray(source.redaction_regions) ||
    source.redaction_regions.length !== count
  ) {
    throw new TypeError("report redaction regions are invalid");
  }
  const regions = source.redaction_regions.map((rawRegion, index) => {
    const row = object(rawRegion, `redaction_regions[${index}]`);
    exactKeys(row, REGION_KEYS, `redaction_regions[${index}]`);
    if (row.kind !== "face" && row.kind !== "license_plate") {
      throw new TypeError(`redaction_regions[${index}] kind is invalid`);
    }
    const x = boundedInteger(row.x, 0, width - 1, "region.x");
    const y = boundedInteger(row.y, 0, height - 1, "region.y");
    const regionWidth = boundedInteger(
      row.width,
      1,
      width,
      "region.width",
    );
    const regionHeight = boundedInteger(
      row.height,
      1,
      height,
      "region.height",
    );
    if (x + regionWidth > width || y + regionHeight > height) {
      throw new TypeError("report redaction region exceeds image");
    }
    return Object.freeze({
      kind: row.kind,
      x,
      y,
      width: regionWidth,
      height: regionHeight,
    });
  });
  const faceCount = regions.filter((row) => row.kind === "face").length;
  const plateCount = regions.filter(
    (row) => row.kind === "license_plate",
  ).length;
  if (
    source.face_detection_count !== faceCount ||
    source.plate_detection_count !== plateCount ||
    source.automatic_redaction_performed !== (count > 0)
  ) {
    throw new TypeError("report redaction counts are inconsistent");
  }
  const detector = object(source.detector, "report detector");
  exactKeys(detector, DETECTOR_KEYS, "report detector");
  if (
    detector.coverage_guaranteed !== false ||
    detector.human_review_required !== true
  ) {
    throw new TypeError("report detector must require human review");
  }
  const publicDetector = Object.freeze({
    detectorName: nullableText(
      detector.detector_name,
      null,
      "detector_name",
    ),
    detectorVersion: nullableText(
      detector.detector_version,
      null,
      "detector_version",
    ),
    modelId: nullableText(detector.model_id, null, "model_id"),
    modelSha256: identity(
      detector.model_sha256,
      SHA256_PATTERN,
      "model_sha256",
    ),
    coverageGuaranteed: false,
    humanReviewRequired: true,
  });
  const state = source.state;
  if (
    state !== "awaiting_deidentification_review" &&
    state !== "verified_for_shared_report" &&
    state !== "rejected_by_deidentification_review"
  ) {
    throw new TypeError("report deidentification state is invalid");
  }
  const humanVerified = source.human_verified;
  const reviewRequired = source.review_required;
  if (
    typeof humanVerified !== "boolean" ||
    typeof reviewRequired !== "boolean"
  ) {
    throw new TypeError("report review flags are invalid");
  }
  const reviewerDecision = nullableText(
    source.reviewer_decision,
    state === "verified_for_shared_report"
      ? "approved"
      : state === "rejected_by_deidentification_review"
        ? "rejected"
        : null,
    "reviewer_decision",
  );
  const reviewerActor = nullableText(
    source.reviewer_actor,
    state === "awaiting_deidentification_review"
      ? null
      : "local_user",
    "reviewer_actor",
  );
  const reviewedAt = nullableText(
    source.reviewed_at,
    null,
    "reviewed_at",
  );
  const reviewRevision = boundedInteger(
    source.review_revision,
    0,
    1,
    "review_revision",
  );
  const reviewRecordSha256 = nullableHash(
    source.review_record_sha256,
    "review_record_sha256",
  );
  const deidentificationRecordSha256 = nullableHash(
    source.deidentification_record_sha256,
    "deidentification_record_sha256",
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
    throw new TypeError("report review state is inconsistent");
  }
  return Object.freeze({
    schemaVersion: source.schema_version,
    projectId: project,
    pipelineJobId: job,
    analysisJobId: analysis,
    sourceSha256: identity(
      source.source_sha256,
      SHA256_PATTERN,
      "source_sha256",
    ),
    incidentId: incident,
    candidateId: candidate,
    derivativeId: derivative,
    deidentifiedRef,
    state,
    inputRevision: identity(
      source.input_revision,
      SHA256_PATTERN,
      "input_revision",
    ),
    manifestSha256: identity(
      source.manifest_sha256,
      SHA256_PATTERN,
      "manifest_sha256",
    ),
    originalSha256: identity(
      source.original_sha256,
      SHA256_PATTERN,
      "original_sha256",
    ),
    deidentifiedSha256: identity(
      source.deidentified_sha256,
      SHA256_PATTERN,
      "deidentified_sha256",
    ),
    mediaType: "image/jpeg",
    sizeBytes: boundedInteger(
      source.size_bytes,
      1,
      16 * 1024 * 1024,
      "size_bytes",
    ),
    width,
    height,
    detector: publicDetector,
    detectorIdentitySha256: identity(
      source.detector_identity_sha256,
      SHA256_PATTERN,
      "detector_identity_sha256",
    ),
    redactionRegions: Object.freeze(regions),
    redactionRegionCount: count,
    faceDetectionCount: faceCount,
    plateDetectionCount: plateCount,
    automaticRedactionPerformed: count > 0,
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
      source.preparation_revision,
      SHA256_PATTERN,
      "preparation_revision",
    ),
  });
}

export function sanitizeReportDeidentifiedList(
  raw,
  { projectId, jobId } = {},
) {
  const source = object(raw, "report deidentified list");
  exactKeys(
    source,
    [
      "schema_version",
      "project_id",
      "pipeline_job_id",
      "analysis_job_id",
      "state",
      "count",
      "preparations",
      "path_exposed",
    ],
    "report deidentified list",
  );
  if (
    source.schema_version !==
      "trafficvisson.report-deidentified-evidence-list.v1" ||
    source.path_exposed !== false ||
    !["prepared_review_required", "listed"].includes(source.state)
  ) {
    throw new TypeError("report deidentified list is unsafe");
  }
  const project = identity(source.project_id, UUID_PATTERN, "project_id");
  const job = identity(
    source.pipeline_job_id,
    UUID_PATTERN,
    "pipeline_job_id",
  );
  const analysis = identity(
    source.analysis_job_id,
    UUID_PATTERN,
    "analysis_job_id",
  );
  if (
    (projectId && project !== projectId) ||
    (jobId && job !== jobId)
  ) {
    throw new TypeError("report deidentified list identity changed");
  }
  const count = boundedInteger(source.count, 0, 500, "count");
  if (
    !Array.isArray(source.preparations) ||
    source.preparations.length !== count
  ) {
    throw new TypeError("report deidentified list count changed");
  }
  const preparations = source.preparations.map((row) =>
    sanitizeReportDeidentifiedPreparation(row, {
      projectId: project,
      jobId: job,
    }),
  );
  const incidentIds = preparations.map((row) => row.incidentId);
  if (new Set(incidentIds).size !== incidentIds.length) {
    throw new TypeError("report deidentified incident list is duplicated");
  }
  return Object.freeze({
    schemaVersion: source.schema_version,
    projectId: project,
    pipelineJobId: job,
    analysisJobId: analysis,
    state: source.state,
    count,
    preparations: Object.freeze(preparations),
  });
}

export async function sanitizeReportDeidentifiedArtifactResponse(
  response,
) {
  if (!(response instanceof Response) || response.status !== 200) {
    throw new TypeError("report deidentified artifact response is invalid");
  }
  const mediaType = (
    response.headers.get("content-type") ?? ""
  ).toLowerCase();
  if (mediaType !== "image/jpeg") {
    throw new TypeError("report deidentified artifact media type is invalid");
  }
  const etag = response.headers.get("etag");
  const match = /^"sha256-([a-f0-9]{64})"$/.exec(etag ?? "");
  if (!match) {
    throw new TypeError("report deidentified artifact hash is missing");
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (
    bytes.byteLength < 3 ||
    bytes.byteLength > 16 * 1024 * 1024 ||
    bytes[0] !== 0xff ||
    bytes[1] !== 0xd8 ||
    bytes[2] !== 0xff
  ) {
    throw new TypeError("report deidentified artifact bytes are invalid");
  }
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  if (sha256 !== match[1]) {
    throw new TypeError("report deidentified artifact hash changed");
  }
  return Object.freeze({
    media_type: "image/jpeg",
    sha256,
    bytes,
  });
}
