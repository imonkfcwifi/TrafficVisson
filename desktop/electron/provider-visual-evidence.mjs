import { createHash } from "node:crypto";

const UUID_PATTERN =
  /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const CANDIDATE_ID_PATTERN = /^[a-f0-9]{20}$/;
const PREPARATION_ID_PATTERN = /^[a-f0-9]{24}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const PREPARATION_STATES = new Set([
  "awaiting_deidentification_review",
  "verified_for_provider_transmission",
  "rejected_by_privacy_review",
]);
const ATTACHMENT_ROLES = new Set([
  "candidate_representative",
  "similar_case",
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

function boolean(value, label) {
  if (typeof value !== "boolean") {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}

const PUBLIC_PREPARATION_KEYS = Object.freeze([
  "schema_version",
  "project_id",
  "pipeline_job_id",
  "analysis_job_id",
  "candidate_id",
  "preparation_id",
  "input_revision",
  "state",
  "manifest_sha256",
  "engine_identity_sha256",
  "attachment_count",
  "attachments",
  "automatic_redaction_performed",
  "automatic_privacy_verification",
  "human_verified",
  "review_required",
  "review_record_sha256",
  "path_exposed",
  "preparation_revision",
]);

const PUBLIC_ATTACHMENT_KEYS = Object.freeze([
  "order",
  "role",
  "source_ref",
  "source_sha256",
  "output_sha256",
  "media_type",
  "size_bytes",
  "width",
  "height",
  "face_detection_count",
  "plate_detection_count",
]);

/**
 * Reduce the authenticated bridge snapshot to the only privacy-review fields
 * the sandboxed renderer needs. Source references and every path-like field
 * deliberately stop in the main process.
 */
export function sanitizeProviderVisualPreparation(
  raw,
  { projectId, jobId, candidateId, preparationId } = {},
) {
  const source = object(raw, "provider visual preparation");
  exactKeys(source, PUBLIC_PREPARATION_KEYS, "provider visual preparation");
  if (
    source.schema_version !==
    "trafficvisson.provider-visual-evidence-preparation.v1"
  ) {
    throw new TypeError("provider visual preparation schema is invalid");
  }
  const project = identity(
    source.project_id,
    UUID_PATTERN,
    "project_id",
  );
  const job = identity(
    source.pipeline_job_id,
    UUID_PATTERN,
    "pipeline_job_id",
  );
  const candidate = identity(
    source.candidate_id,
    CANDIDATE_ID_PATTERN,
    "candidate_id",
  );
  const preparation = identity(
    source.preparation_id,
    PREPARATION_ID_PATTERN,
    "preparation_id",
  );
  if (
    (projectId && project !== projectId) ||
    (jobId && job !== jobId) ||
    (candidateId && candidate !== candidateId) ||
    (preparationId && preparation !== preparationId)
  ) {
    throw new TypeError("provider visual preparation identity changed");
  }
  if (source.path_exposed !== false) {
    throw new TypeError("provider visual preparation exposed a path");
  }
  if (
    source.automatic_privacy_verification !== false ||
    !PREPARATION_STATES.has(source.state)
  ) {
    throw new TypeError("provider visual privacy state is invalid");
  }
  const attachmentCount = boundedInteger(
    source.attachment_count,
    4,
    6,
    "attachment_count",
  );
  if (
    !Array.isArray(source.attachments) ||
    source.attachments.length !== attachmentCount
  ) {
    throw new TypeError("provider visual attachments are invalid");
  }
  const attachments = source.attachments.map((rawAttachment, index) => {
    const row = object(rawAttachment, `attachments[${index}]`);
    exactKeys(
      row,
      PUBLIC_ATTACHMENT_KEYS,
      `attachments[${index}]`,
    );
    const order = boundedInteger(
      row.order,
      0,
      5,
      `attachments[${index}].order`,
    );
    if (
      order !== index ||
      !ATTACHMENT_ROLES.has(row.role) ||
      (index === 0) !== (row.role === "candidate_representative") ||
      row.media_type !== "image/png"
    ) {
      throw new TypeError(`attachments[${index}] role is invalid`);
    }
    return Object.freeze({
      order,
      role: row.role,
      source_sha256: identity(
        row.source_sha256,
        SHA256_PATTERN,
        `attachments[${index}].source_sha256`,
      ),
      output_sha256: identity(
        row.output_sha256,
        SHA256_PATTERN,
        `attachments[${index}].output_sha256`,
      ),
      media_type: "image/png",
      size_bytes: boundedInteger(
        row.size_bytes,
        1,
        8 * 1024 * 1024,
        `attachments[${index}].size_bytes`,
      ),
      width: boundedInteger(
        row.width,
        1,
        2_048,
        `attachments[${index}].width`,
      ),
      height: boundedInteger(
        row.height,
        1,
        2_048,
        `attachments[${index}].height`,
      ),
      face_detection_count: boundedInteger(
        row.face_detection_count,
        0,
        1_024,
        `attachments[${index}].face_detection_count`,
      ),
      plate_detection_count: boundedInteger(
        row.plate_detection_count,
        0,
        1_024,
        `attachments[${index}].plate_detection_count`,
      ),
    });
  });
  const humanVerified = boolean(source.human_verified, "human_verified");
  const reviewRequired = boolean(source.review_required, "review_required");
  if (
    (source.state === "awaiting_deidentification_review" &&
      (humanVerified || !reviewRequired)) ||
    (source.state === "verified_for_provider_transmission" &&
      (!humanVerified || reviewRequired)) ||
    (source.state === "rejected_by_privacy_review" &&
      (humanVerified || reviewRequired))
  ) {
    throw new TypeError("provider visual review flags are invalid");
  }
  const reviewRecordSha256 =
    source.review_record_sha256 === null
      ? null
      : identity(
          source.review_record_sha256,
          SHA256_PATTERN,
          "review_record_sha256",
        );
  if (
    (source.state === "awaiting_deidentification_review") !==
    (reviewRecordSha256 === null)
  ) {
    throw new TypeError("provider visual review record is invalid");
  }
  return Object.freeze({
    schema_version: source.schema_version,
    project_id: project,
    pipeline_job_id: job,
    analysis_job_id: identity(
      source.analysis_job_id,
      UUID_PATTERN,
      "analysis_job_id",
    ),
    candidate_id: candidate,
    preparation_id: preparation,
    input_revision: identity(
      source.input_revision,
      SHA256_PATTERN,
      "input_revision",
    ),
    state: source.state,
    manifest_sha256: identity(
      source.manifest_sha256,
      SHA256_PATTERN,
      "manifest_sha256",
    ),
    engine_identity_sha256: identity(
      source.engine_identity_sha256,
      SHA256_PATTERN,
      "engine_identity_sha256",
    ),
    attachment_count: attachmentCount,
    attachments: Object.freeze(attachments),
    automatic_redaction_performed: boolean(
      source.automatic_redaction_performed,
      "automatic_redaction_performed",
    ),
    automatic_privacy_verification: false,
    human_verified: humanVerified,
    review_required: reviewRequired,
    review_record_sha256: reviewRecordSha256,
    preparation_revision: identity(
      source.preparation_revision,
      SHA256_PATTERN,
      "preparation_revision",
    ),
  });
}

/**
 * Discover immutable preparation identities from the durable vision stage.
 * The returned records are used only to call the dedicated authenticated
 * preparation endpoint; checkpoint content itself is never sent to renderer.
 */
export function providerVisualPreparationIdentities(
  rawJob,
  { projectId, jobId },
) {
  const job = object(rawJob, "pipeline job");
  if (job.project_id !== projectId || job.job_id !== jobId) {
    throw new TypeError("pipeline job identity changed");
  }
  if (!Array.isArray(job.stages) || job.stages.length > 64) {
    throw new TypeError("pipeline stages are invalid");
  }
  const stage = job.stages.find((row) => row?.name === "vision_proposal");
  const checkpoint = object(
    stage?.checkpoint,
    "vision proposal checkpoint",
  );
  const rows = checkpoint.provider_visual_evidence_by_candidate;
  if (rows === undefined) return Object.freeze([]);
  const preparations = object(rows, "provider visual preparations");
  const entries = Object.entries(preparations);
  if (entries.length > 256) {
    throw new TypeError("provider visual preparation list is too large");
  }
  return Object.freeze(
    entries
      .map(([candidateId, rawPreparation]) => {
        const row = object(
          rawPreparation,
          `provider visual preparation ${candidateId}`,
        );
        return Object.freeze({
          candidateId: identity(
            candidateId,
            CANDIDATE_ID_PATTERN,
            "candidateId",
          ),
          preparationId: identity(
            row.preparation_id,
            PREPARATION_ID_PATTERN,
            "preparationId",
          ),
        });
      })
      .sort((left, right) =>
        left.candidateId.localeCompare(right.candidateId),
      ),
  );
}

export async function sanitizeProviderVisualArtifactResponse(response) {
  if (!(response instanceof Response) || response.status !== 200) {
    throw new TypeError("provider visual artifact response is invalid");
  }
  const mediaType = (
    response.headers.get("content-type") ?? ""
  ).toLowerCase();
  if (mediaType !== "image/png") {
    throw new TypeError("provider visual artifact media type is invalid");
  }
  const etag = response.headers.get("etag");
  const match = /^"sha256-([a-f0-9]{64})"$/.exec(etag ?? "");
  if (!match) {
    throw new TypeError("provider visual artifact hash is missing");
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength < 8 || bytes.byteLength > 8 * 1024 * 1024) {
    throw new TypeError("provider visual artifact size is invalid");
  }
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  if (sha256 !== match[1]) {
    throw new TypeError("provider visual artifact hash changed");
  }
  return Object.freeze({
    media_type: "image/png",
    sha256,
    bytes,
  });
}
