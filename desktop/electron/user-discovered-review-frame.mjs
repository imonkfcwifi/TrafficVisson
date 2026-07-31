import { createHash } from "node:crypto";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const FRAME_ID_PATTERN = /^[a-f0-9]{24}$/;
const METADATA_KEYS = Object.freeze([
  "schema_version",
  "project_id",
  "pipeline_job_id",
  "analysis_job_id",
  "requested_timestamp_seconds",
  "selected_frame_id",
  "selected_frame_index",
  "selected_frame_timestamp_seconds",
  "selected_frame_sha256",
  "selected_frame_size_bytes",
  "media_type",
  "source_duration_seconds",
  "indexed_duration_seconds",
  "sampling_fps",
  "dense_index_sha256",
  "promoted_model_version_id",
  "source_analysis_lineage_sha256",
  "review_frame_sha256",
  "question_queue_revision",
  "path_exposed",
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
    throw new TypeError(`${label} is invalid`);
  }
}

function identity(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new TypeError(`${label} is invalid`);
  }
  return value.toLowerCase();
}

function number(value, label, minimum, maximum) {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}

function integer(value, label, minimum, maximum) {
  if (
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}

export function sanitizeUserDiscoveredReviewFrameMetadata(
  raw,
  { projectId, pipelineJobId, analysisJobId, timestampSeconds },
) {
  const source = object(raw, "user-discovered review frame");
  exactKeys(
    source,
    METADATA_KEYS,
    "user-discovered review frame",
  );
  const project = identity(
    source.project_id,
    UUID_PATTERN,
    "project_id",
  );
  const pipeline = identity(
    source.pipeline_job_id,
    UUID_PATTERN,
    "pipeline_job_id",
  );
  const analysis = identity(
    source.analysis_job_id,
    UUID_PATTERN,
    "analysis_job_id",
  );
  const requested = number(
    source.requested_timestamp_seconds,
    "requested_timestamp_seconds",
    0,
    86_400,
  );
  const selected = number(
    source.selected_frame_timestamp_seconds,
    "selected_frame_timestamp_seconds",
    0,
    86_400,
  );
  const sourceDuration = number(
    source.source_duration_seconds,
    "source_duration_seconds",
    Number.EPSILON,
    86_400,
  );
  const indexedDuration = number(
    source.indexed_duration_seconds,
    "indexed_duration_seconds",
    Number.EPSILON,
    86_400,
  );
  if (
    project !== projectId.toLowerCase() ||
    pipeline !== pipelineJobId.toLowerCase() ||
    analysis !== analysisJobId.toLowerCase() ||
    Math.abs(requested - timestampSeconds) > 0.000001 ||
    indexedDuration > sourceDuration + 0.000001 ||
    requested > indexedDuration + 0.000001 ||
    selected > indexedDuration + 0.000001 ||
    Math.abs(selected - requested) > 0.500001 ||
    source.schema_version !==
      "trafficvisson.user-discovered-review-frame.v1" ||
    source.media_type !== "image/jpeg" ||
    source.sampling_fps !== 2 ||
    source.path_exposed !== false
  ) {
    throw new TypeError(
      "user-discovered review frame binding changed",
    );
  }
  return Object.freeze({
    schema_version: source.schema_version,
    project_id: project,
    pipeline_job_id: pipeline,
    analysis_job_id: analysis,
    requested_timestamp_seconds: requested,
    selected_frame_id: identity(
      source.selected_frame_id,
      FRAME_ID_PATTERN,
      "selected_frame_id",
    ),
    selected_frame_index: integer(
      source.selected_frame_index,
      "selected_frame_index",
      0,
      172_800,
    ),
    selected_frame_timestamp_seconds: selected,
    selected_frame_sha256: identity(
      source.selected_frame_sha256,
      SHA256_PATTERN,
      "selected_frame_sha256",
    ),
    selected_frame_size_bytes: integer(
      source.selected_frame_size_bytes,
      "selected_frame_size_bytes",
      1,
      32 * 1024 * 1024,
    ),
    media_type: "image/jpeg",
    source_duration_seconds: sourceDuration,
    indexed_duration_seconds: indexedDuration,
    sampling_fps: 2,
    dense_index_sha256: identity(
      source.dense_index_sha256,
      SHA256_PATTERN,
      "dense_index_sha256",
    ),
    promoted_model_version_id: identity(
      source.promoted_model_version_id,
      UUID_PATTERN,
      "promoted_model_version_id",
    ),
    source_analysis_lineage_sha256: identity(
      source.source_analysis_lineage_sha256,
      SHA256_PATTERN,
      "source_analysis_lineage_sha256",
    ),
    review_frame_sha256: identity(
      source.review_frame_sha256,
      SHA256_PATTERN,
      "review_frame_sha256",
    ),
    question_queue_revision: identity(
      source.question_queue_revision,
      SHA256_PATTERN,
      "question_queue_revision",
    ),
    path_exposed: false,
  });
}

export async function sanitizeUserDiscoveredReviewFrameArtifact(
  response,
  metadata,
) {
  if (!(response instanceof Response) || response.status !== 200) {
    throw new TypeError(
      "user-discovered review frame artifact is invalid",
    );
  }
  const mediaType = (
    response.headers.get("content-type") ?? ""
  ).toLowerCase();
  const etag = response.headers.get("etag");
  if (
    mediaType !== "image/jpeg" ||
    etag !== `"sha256-${metadata.selected_frame_sha256}"` ||
    response.headers.get(
      "x-trafficsupervision-artifact-sha256",
    ) !== metadata.selected_frame_sha256 ||
    response.headers.get(
      "x-trafficsupervision-dense-frame-id",
    ) !== metadata.selected_frame_id ||
    response.headers.get(
      "x-trafficsupervision-dense-index-sha256",
    ) !== metadata.dense_index_sha256
  ) {
    throw new TypeError(
      "user-discovered review frame artifact binding changed",
    );
  }
  const headerTime = Number(
    response.headers.get(
      "x-trafficsupervision-dense-frame-time",
    ),
  );
  if (
    !Number.isFinite(headerTime) ||
    Math.abs(
      headerTime - metadata.selected_frame_timestamp_seconds,
    ) > 0.000001
  ) {
    throw new TypeError(
      "user-discovered review frame artifact time changed",
    );
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (
    bytes.byteLength !== metadata.selected_frame_size_bytes ||
    bytes.byteLength < 4 ||
    bytes[0] !== 0xff ||
    bytes[1] !== 0xd8 ||
    bytes.at(-2) !== 0xff ||
    bytes.at(-1) !== 0xd9
  ) {
    throw new TypeError(
      "user-discovered review frame artifact size is invalid",
    );
  }
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  if (sha256 !== metadata.selected_frame_sha256) {
    throw new TypeError(
      "user-discovered review frame artifact hash changed",
    );
  }
  return Object.freeze({
    metadata,
    bytes,
  });
}
