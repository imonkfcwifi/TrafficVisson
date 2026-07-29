const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SAFE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export const IPC_CHANNELS = Object.freeze({
  DIALOG_VIDEO_FILES: "trafficvisson:dialogs:video-files",
  DIALOG_GPX_FILE: "trafficvisson:dialogs:gpx-file",
  DIALOG_HWP_DOCUMENT: "trafficvisson:dialogs:hwp-document",
  DIALOG_EXPORT_DIRECTORY: "trafficvisson:dialogs:export-directory",
  HEALTH: "trafficvisson:health",
  PROJECT_CREATE: "trafficvisson:projects:create",
  PROJECT_GET: "trafficvisson:projects:get",
  PROJECT_LIST: "trafficvisson:projects:list",
  IMPORT_INSPECT: "trafficvisson:imports:inspect",
  IMPORT_CREATE: "trafficvisson:imports:create",
  MEDIA_LOCAL_REGISTER: "trafficvisson:media:local-register",
  MEDIA_SNAPSHOT: "trafficvisson:media:snapshot",
  MEDIA_STABILITY: "trafficvisson:media:stability",
  MEDIA_PROBE: "trafficvisson:media:probe",
  MEDIA_HASH_START: "trafficvisson:media:hash-start",
  MEDIA_HASH_GET: "trafficvisson:media:hash-get",
  MEDIA_HASH_CANCEL: "trafficvisson:media:hash-cancel",
  ANALYSIS_START: "trafficvisson:analysis:start",
  ANALYSIS_LIST: "trafficvisson:analysis:list",
  ANALYSIS_GET: "trafficvisson:analysis:get",
  ANALYSIS_CANCEL: "trafficvisson:analysis:cancel",
  ANALYSIS_PAUSE: "trafficvisson:analysis:pause",
  DENSE_INDEX_START: "trafficvisson:dense-index:start",
  DENSE_INDEX_LIST: "trafficvisson:dense-index:list",
  DENSE_INDEX_GET: "trafficvisson:dense-index:get",
  DENSE_INDEX_CANCEL: "trafficvisson:dense-index:cancel",
  PIPELINE_PROFILE_CONFIGURE: "trafficvisson:pipeline:profile-configure",
  PIPELINE_PROFILE_GET: "trafficvisson:pipeline:profile-get",
  PIPELINE_START: "trafficvisson:pipeline:start",
  PIPELINE_LIST: "trafficvisson:pipeline:list",
  PIPELINE_GET: "trafficvisson:pipeline:get",
  PIPELINE_CANCEL: "trafficvisson:pipeline:cancel",
  PIPELINE_RESUME: "trafficvisson:pipeline:resume",
  PIPELINE_REVIEW_COMPLETE: "trafficvisson:pipeline:review-complete",
  CANDIDATE_QUESTIONS_LIST:
    "trafficvisson:candidate-questions:list",
  CANDIDATE_ANSWER: "trafficvisson:candidate-questions:answer",
  MASK_REFINEMENT_CAPABILITY:
    "trafficvisson:mask-refinement:capability",
  MASK_REFINEMENT_REQUEST:
    "trafficvisson:mask-refinement:request",
  INCIDENT_LIST: "trafficvisson:incidents:list",
  AUTO_LABEL_COMPILE: "trafficvisson:auto-labeling:compile",
  AUTO_LABEL_RUN: "trafficvisson:auto-labeling:run",
  REVIEW_TEMPORAL_INCIDENTS:
    "trafficvisson:review:temporal-incident-proposals",
  REVIEW_MODEL_VALIDATION: "trafficvisson:review:model-validation",
  REVIEW_MODEL_PROMOTION: "trafficvisson:review:model-promotion",
  LABEL_STATS: "trafficvisson:labels:stats",
  DATASET_LIST: "trafficvisson:datasets:list",
  DATASET_CREATE: "trafficvisson:datasets:create",
  SOURCE_CANDIDATES_LIST: "trafficvisson:source-candidates:list",
  WEAK_CASE_SOURCES_LIST:
    "trafficvisson:weak-cases:sources-list",
  WEAK_CASE_SEARCH: "trafficvisson:weak-cases:search",
  QUESTIONS_LIST: "trafficvisson:questions:list",
  QUESTION_ANSWER: "trafficvisson:questions:answer",
  HISTORY_GET: "trafficvisson:history:get",
  RUNTIME_READINESS: "trafficvisson:runtime:readiness",
  GPX_TRACK_IMPORT: "trafficvisson:gpx-track:import",
  GPX_TRACK_GET: "trafficvisson:gpx-track:get",
  GPX_TRACK_OFFSET: "trafficvisson:gpx-track:offset",
  PROVIDERS_LIST: "trafficvisson:providers:list",
  PROVIDER_LOGIN: "trafficvisson:providers:login",
  REPORT_GENERATE: "trafficvisson:reports:generate",
  REPORT_DRAFT: "trafficvisson:reports:draft",
  REPORT_PLAN: "trafficvisson:reports:plan",
  REPORT_APPLY: "trafficvisson:reports:apply",
  REPORT_ACCEPT: "trafficvisson:reports:accept",
  REPORT_REJECT: "trafficvisson:reports:reject",
  HWP_PREFLIGHT: "trafficvisson:hwp:preflight",
  HWP_PREVIEW: "trafficvisson:hwp:preview",
  HWP_APPLY: "trafficvisson:hwp:apply",
  HWP_ROLLBACK: "trafficvisson:hwp:rollback",
  EXPORT_CREATE: "trafficvisson:exports:create",
});

const CHANNEL_SET = new Set(Object.values(IPC_CHANNELS));

export class IpcValidationError extends Error {
  constructor(message) {
    super(message);
    this.name = "IpcValidationError";
    this.code = "INVALID_IPC_PAYLOAD";
  }
}

function record(value, allowedKeys, label = "payload") {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new IpcValidationError(`${label} must be an object`);
  }
  for (const key of Object.keys(value)) {
    if (!allowedKeys.includes(key)) {
      throw new IpcValidationError(`${label}.${key} is not allowed`);
    }
  }
  return value;
}

function text(value, label, { min = 1, max = 256, optional = false } = {}) {
  if (optional && (value === undefined || value === null || value === "")) {
    return undefined;
  }
  if (typeof value !== "string") {
    throw new IpcValidationError(`${label} must be a string`);
  }
  const normalized = value.trim();
  if (normalized.length < min || normalized.length > max || normalized.includes("\0")) {
    throw new IpcValidationError(`${label} has an invalid length or character`);
  }
  return normalized;
}

function uuid(value, label) {
  const normalized = text(value, label, { max: 64 });
  if (!UUID_RE.test(normalized)) {
    throw new IpcValidationError(`${label} must be a UUID`);
  }
  return normalized.toLowerCase();
}

function safeId(value, label, { optional = false } = {}) {
  const normalized = text(value, label, { max: 128, optional });
  if (normalized === undefined) return undefined;
  if (!SAFE_ID_RE.test(normalized)) {
    throw new IpcValidationError(`${label} must be an opaque identifier`);
  }
  return normalized;
}

function enumValue(value, label, allowed, { optional = false } = {}) {
  if (optional && (value === undefined || value === null || value === "")) {
    return undefined;
  }
  if (!allowed.includes(value)) {
    throw new IpcValidationError(`${label} is not supported`);
  }
  return value;
}

function bool(value, label, { optional = false } = {}) {
  if (optional && value === undefined) return undefined;
  if (typeof value !== "boolean") {
    throw new IpcValidationError(`${label} must be a boolean`);
  }
  return value;
}

function stringArray(value, label, { maxItems = 100, itemMax = 128 } = {}) {
  if (!Array.isArray(value) || value.length > maxItems) {
    throw new IpcValidationError(`${label} must be a bounded array`);
  }
  return value.map((item, index) => text(item, `${label}[${index}]`, { max: itemMax }));
}

function optionalGeometry(value) {
  if (value === undefined || value === null) return undefined;
  const geometry = record(value, ["kind", "points"], "finalGeometry");
  const kind = enumValue(geometry.kind, "finalGeometry.kind", ["box", "polygon", "mask_ref"]);
  if (!Array.isArray(geometry.points) || geometry.points.length > 256) {
    throw new IpcValidationError("finalGeometry.points must be a bounded array");
  }
  const points = geometry.points.map((point, pointIndex) => {
    if (
      !Array.isArray(point) ||
      point.length !== 2 ||
      point.some((coordinate) => typeof coordinate !== "number" || !Number.isFinite(coordinate))
    ) {
      throw new IpcValidationError(`finalGeometry.points[${pointIndex}] is invalid`);
    }
    return point;
  });
  return { kind, points };
}

const REVIEW_CANONICAL_ISSUE_TYPES = Object.freeze([
  "pothole",
  "road_surface_damage",
  "road_debris",
  "median_barrier_damage",
  "sign_damage",
  "sign_vegetation_occlusion",
  "roadside_mowing_required",
  "guide_sign_identity",
  "tunnel_identity",
  "tunnel_name_occlusion",
  "lane_marking_faded",
  "colored_guidance_faded",
]);
const REVIEW_CANONICAL_ISSUE_SET = new Set(REVIEW_CANONICAL_ISSUE_TYPES);
const REVIEW_ISSUE_ALIASES = Object.freeze({
  surface_damage: "road_surface_damage",
  debris: "road_debris",
  sign_vegetation: "sign_vegetation_occlusion",
  mowing: "roadside_mowing_required",
  sign_tunnel_identity: "guide_sign_identity",
  sign_identity: "guide_sign_identity",
  lane_color_line_fading: "colored_guidance_faded",
  color_guidance_line_faded: "colored_guidance_faded",
  colored_guidance_line_faded: "colored_guidance_faded",
});

function reviewIssueType(
  value,
  label,
  { allowOther = false, optional = false } = {},
) {
  if (
    optional &&
    (value === undefined || value === null || value === "")
  ) {
    return undefined;
  }
  const normalized =
    typeof value === "string"
      ? REVIEW_ISSUE_ALIASES[value] ?? value
      : value;
  if (
    typeof normalized !== "string" ||
    (!REVIEW_CANONICAL_ISSUE_SET.has(normalized) &&
      !(allowOther && normalized === "other"))
  ) {
    throw new IpcValidationError(`${label} is not a canonical issue type`);
  }
  return normalized;
}

function finiteNumber(value, label, { minimum, maximum }) {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new IpcValidationError(`${label} is outside the safe range`);
  }
  return value;
}

function safeCount(value, label, { minimum = 0, maximum }) {
  if (
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new IpcValidationError(`${label} must be a bounded integer`);
  }
  return value;
}

function reviewIdentifier(value, label, { optional = false } = {}) {
  const normalized = text(value, label, { max: 128, optional });
  if (normalized === undefined) return undefined;
  if (!/^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/.test(normalized)) {
    throw new IpcValidationError(`${label} must be an opaque identifier`);
  }
  return normalized;
}

function reviewNormalizedBox(value, label) {
  const box = record(value, ["x", "y", "width", "height"], label);
  const x = finiteNumber(box.x, `${label}.x`, { minimum: 0, maximum: 1 });
  const y = finiteNumber(box.y, `${label}.y`, { minimum: 0, maximum: 1 });
  const width = finiteNumber(box.width, `${label}.width`, {
    minimum: Number.MIN_VALUE,
    maximum: 1,
  });
  const height = finiteNumber(box.height, `${label}.height`, {
    minimum: Number.MIN_VALUE,
    maximum: 1,
  });
  if (x + width > 1 || y + height > 1) {
    throw new IpcValidationError(`${label} must remain inside the image`);
  }
  return { x, y, width, height };
}

function validationTruthRecords(value, label) {
  if (!Array.isArray(value) || value.length > 1_000_000) {
    throw new IpcValidationError(`${label} must be a bounded array`);
  }
  return value.map((raw, index) => {
    const item = record(
      raw,
      ["sampleId", "issueType", "truthEventIds"],
      `${label}[${index}]`,
    );
    const truthEventIds = stringArray(
      item.truthEventIds ?? [],
      `${label}[${index}].truthEventIds`,
      { maxItems: 256 },
    ).map((eventId, eventIndex) =>
      safeId(eventId, `${label}[${index}].truthEventIds[${eventIndex}]`),
    );
    if (new Set(truthEventIds).size !== truthEventIds.length) {
      throw new IpcValidationError(
        `${label}[${index}].truthEventIds must be unique`,
      );
    }
    return {
      sampleId: safeId(item.sampleId, `${label}[${index}].sampleId`),
      issueType: reviewIssueType(
        item.issueType,
        `${label}[${index}].issueType`,
      ),
      truthEventIds,
    };
  });
}

function validationPredictionRecords(value, label) {
  if (!Array.isArray(value) || value.length > 2_000_000) {
    throw new IpcValidationError(`${label} must be a bounded array`);
  }
  return value.map((raw, index) => {
    const item = record(
      raw,
      [
        "predictionEventId",
        "sampleId",
        "issueType",
        "matchedTruthEventId",
      ],
      `${label}[${index}]`,
    );
    return {
      predictionEventId: safeId(
        item.predictionEventId,
        `${label}[${index}].predictionEventId`,
      ),
      sampleId: safeId(item.sampleId, `${label}[${index}].sampleId`),
      issueType: reviewIssueType(
        item.issueType,
        `${label}[${index}].issueType`,
      ),
      matchedTruthEventId: safeId(
        item.matchedTruthEventId,
        `${label}[${index}].matchedTruthEventId`,
        { optional: true },
      ),
    };
  });
}

function validatePlan(value) {
  const plan = record(
    value,
    ["schemaVersion", "planId", "documentSnapshotId", "operations"],
    "plan",
  );
  if (!Array.isArray(plan.operations) || plan.operations.length < 1 || plan.operations.length > 100) {
    throw new IpcValidationError("plan.operations must contain 1 to 100 operations");
  }
  return {
    schemaVersion: enumValue(plan.schemaVersion, "plan.schemaVersion", ["1.0"]),
    planId: uuid(plan.planId, "plan.planId"),
    documentSnapshotId: safeId(plan.documentSnapshotId, "plan.documentSnapshotId"),
    operations: plan.operations.map((rawOperation, index) => {
      const operation = record(
        rawOperation,
        [
          "operationId",
          "type",
          "target",
          "expectedValue",
          "value",
          "sourceRef",
          "stylePolicy",
          "preview",
        ],
        `plan.operations[${index}]`,
      );
      return {
        operationId: uuid(operation.operationId, `plan.operations[${index}].operationId`),
        type: enumValue(operation.type, `plan.operations[${index}].type`, [
          "fill_text",
          "add_table_row",
          "fill_cell",
          "insert_image",
          "insert_map",
        ]),
        target: safeId(operation.target, `plan.operations[${index}].target`),
        expectedValue: text(
          operation.expectedValue,
          `plan.operations[${index}].expectedValue`,
          { max: 8_000, optional: true },
        ),
        value: text(operation.value, `plan.operations[${index}].value`, { max: 20_000 }),
        sourceRef: safeId(operation.sourceRef, `plan.operations[${index}].sourceRef`),
        stylePolicy: enumValue(
          operation.stylePolicy,
          `plan.operations[${index}].stylePolicy`,
          ["preserve", "inherit_target"],
        ),
        preview: text(operation.preview, `plan.operations[${index}].preview`, {
          max: 1_000,
        }),
      };
    }),
  };
}

export function assertKnownChannel(channel) {
  if (!CHANNEL_SET.has(channel)) {
    throw new IpcValidationError("IPC channel is not allowlisted");
  }
  return channel;
}

export function validateIpcPayload(channel, payload) {
  assertKnownChannel(channel);

  if (
    channel === IPC_CHANNELS.DIALOG_VIDEO_FILES ||
    channel === IPC_CHANNELS.DIALOG_GPX_FILE ||
    channel === IPC_CHANNELS.DIALOG_HWP_DOCUMENT ||
    channel === IPC_CHANNELS.DIALOG_EXPORT_DIRECTORY ||
    channel === IPC_CHANNELS.HEALTH ||
    channel === IPC_CHANNELS.PROJECT_LIST ||
    channel === IPC_CHANNELS.SOURCE_CANDIDATES_LIST ||
    channel === IPC_CHANNELS.RUNTIME_READINESS ||
    channel === IPC_CHANNELS.PROVIDERS_LIST ||
    channel === IPC_CHANNELS.HWP_PREFLIGHT
  ) {
    if (payload !== undefined && payload !== null) {
      record(payload, [], "payload");
    }
    return {};
  }

  if (channel === IPC_CHANNELS.PROJECT_CREATE) {
    const value = record(payload, ["name", "description"]);
    return {
      name: text(value.name, "name", { max: 120 }),
      description: text(value.description, "description", { max: 2_000, optional: true }),
    };
  }

  if (channel === IPC_CHANNELS.PROJECT_GET) {
    const value = record(payload, ["projectId"]);
    return { projectId: uuid(value.projectId, "projectId") };
  }

  if (channel === IPC_CHANNELS.IMPORT_INSPECT) {
    const value = record(payload, [
      "projectId",
      "sourceType",
      "sourceUrl",
      "localSelectionId",
    ]);
    const sourceType = enumValue(value.sourceType, "sourceType", [
      "local_selection",
      "direct_url",
      "google_drive",
      "dropbox",
      "onedrive",
      "youtube",
      "webpage",
    ]);
    const sourceUrl = text(value.sourceUrl, "sourceUrl", {
      max: 2_048,
      optional: sourceType === "local_selection",
    });
    const localSelectionId =
      sourceType === "local_selection"
        ? uuid(value.localSelectionId, "localSelectionId")
        : undefined;
    if (sourceType !== "local_selection" && !sourceUrl) {
      throw new IpcValidationError("sourceUrl is required for URL imports");
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      sourceType,
      sourceUrl,
      localSelectionId,
    };
  }

  if (channel === IPC_CHANNELS.IMPORT_CREATE) {
    const value = record(payload, ["projectId", "inspectedSourceId", "rightsConfirmed"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      inspectedSourceId: uuid(value.inspectedSourceId, "inspectedSourceId"),
      rightsConfirmed: bool(value.rightsConfirmed, "rightsConfirmed"),
    };
  }

  if (channel === IPC_CHANNELS.MEDIA_LOCAL_REGISTER) {
    const value = record(payload, ["projectId", "localSelectionId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      localSelectionId: uuid(
        value.localSelectionId,
        "localSelectionId",
      ),
    };
  }

  if (channel === IPC_CHANNELS.MEDIA_SNAPSHOT) {
    const value = record(payload, ["projectId", "directory"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      directory: text(value.directory, "directory", { min: 3, max: 4_096 }),
    };
  }

  if (channel === IPC_CHANNELS.MEDIA_STABILITY) {
    const value = record(payload, ["projectId", "snapshotIds"]);
    if (
      !Array.isArray(value.snapshotIds) ||
      value.snapshotIds.length !== 3
    ) {
      throw new IpcValidationError("snapshotIds must contain exactly three items");
    }
    const snapshotIds = value.snapshotIds.map((item, index) =>
      uuid(item, `snapshotIds[${index}]`),
    );
    if (new Set(snapshotIds).size !== 3) {
      throw new IpcValidationError("snapshotIds must be distinct");
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      snapshotIds,
    };
  }

  if (channel === IPC_CHANNELS.MEDIA_PROBE) {
    const value = record(payload, [
      "projectId",
      "snapshotId",
      "fileName",
      "timeoutSeconds",
    ]);
    const fileName = text(value.fileName, "fileName", { max: 255 });
    if (/[\\/]/.test(fileName)) {
      throw new IpcValidationError("fileName must not contain a path");
    }
    const timeoutSeconds = value.timeoutSeconds ?? 15;
    if (
      !Number.isInteger(timeoutSeconds) ||
      timeoutSeconds < 1 ||
      timeoutSeconds > 30
    ) {
      throw new IpcValidationError("timeoutSeconds must be between 1 and 30");
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      snapshotId: uuid(value.snapshotId, "snapshotId"),
      fileName,
      timeoutSeconds,
    };
  }

  if (channel === IPC_CHANNELS.MEDIA_HASH_START) {
    const value = record(payload, ["projectId", "snapshotId", "fileName"]);
    const fileName = text(value.fileName, "fileName", { max: 255 });
    if (/[\\/]/.test(fileName)) {
      throw new IpcValidationError("fileName must not contain a path");
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      snapshotId: uuid(value.snapshotId, "snapshotId"),
      fileName,
    };
  }

  if (
    channel === IPC_CHANNELS.MEDIA_HASH_GET ||
    channel === IPC_CHANNELS.MEDIA_HASH_CANCEL
  ) {
    const value = record(payload, ["projectId", "jobId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      jobId: uuid(value.jobId, "jobId"),
    };
  }

  if (channel === IPC_CHANNELS.ANALYSIS_START) {
    const value = record(payload, [
      "projectId",
      "snapshotId",
      "hashJobId",
      "fileName",
      "sha256Lower",
    ]);
    const fileName = text(value.fileName, "fileName", { max: 255 });
    if (/[\\/]/.test(fileName)) {
      throw new IpcValidationError("fileName must not contain a path");
    }
    const sha256Lower = text(value.sha256Lower, "sha256Lower", {
      min: 64,
      max: 64,
    });
    if (!/^[a-f0-9]{64}$/.test(sha256Lower)) {
      throw new IpcValidationError("sha256Lower must be a lowercase SHA-256 value");
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      snapshotId: uuid(value.snapshotId, "snapshotId"),
      hashJobId: uuid(value.hashJobId, "hashJobId"),
      fileName,
      sha256Lower,
    };
  }

  if (channel === IPC_CHANNELS.ANALYSIS_LIST) {
    const value = record(payload, ["projectId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
    };
  }

  if (
    channel === IPC_CHANNELS.ANALYSIS_GET ||
    channel === IPC_CHANNELS.ANALYSIS_CANCEL ||
    channel === IPC_CHANNELS.ANALYSIS_PAUSE
  ) {
    const value = record(payload, ["projectId", "jobId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      jobId: uuid(value.jobId, "jobId"),
    };
  }

  if (channel === IPC_CHANNELS.DENSE_INDEX_START) {
    const value = record(payload, [
      "projectId",
      "analysisJobId",
      "samplingFps",
      "confirmReviewOnly",
    ]);
    if (
      !Number.isSafeInteger(value.samplingFps) ||
      value.samplingFps < 1 ||
      value.samplingFps > 5
    ) {
      throw new IpcValidationError(
        "samplingFps must be an integer from 1 to 5",
      );
    }
    if (value.confirmReviewOnly !== true) {
      throw new IpcValidationError(
        "confirmReviewOnly must be explicitly true",
      );
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      analysisJobId: uuid(
        value.analysisJobId,
        "analysisJobId",
      ),
      samplingFps: value.samplingFps,
      confirmReviewOnly: true,
    };
  }

  if (channel === IPC_CHANNELS.DENSE_INDEX_LIST) {
    const value = record(payload, ["projectId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
    };
  }

  if (
    channel === IPC_CHANNELS.DENSE_INDEX_GET ||
    channel === IPC_CHANNELS.DENSE_INDEX_CANCEL
  ) {
    const value = record(payload, ["projectId", "jobId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      jobId: uuid(value.jobId, "jobId"),
    };
  }

  if (channel === IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE) {
    const value = record(payload, [
      "projectId",
      "questionPolicy",
      "reviewMode",
      "defaultTemplateId",
      "defaultTemplateSha256",
      "detectorPackId",
      "ocrPackId",
      "sam2PackId",
      "reportProvider",
      "outputLocationId",
      "candidateRescanFps",
    ]);
    const digest = text(
      value.defaultTemplateSha256,
      "defaultTemplateSha256",
      { min: 64, max: 64 },
    );
    if (!/^[a-f0-9]{64}$/.test(digest)) {
      throw new IpcValidationError(
        "defaultTemplateSha256 must be a lowercase SHA-256 value",
      );
    }
    if (
      !Number.isSafeInteger(value.candidateRescanFps) ||
      value.candidateRescanFps < 5 ||
      value.candidateRescanFps > 10
    ) {
      throw new IpcValidationError(
        "candidateRescanFps must be an integer from 5 to 10",
      );
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      questionPolicy: enumValue(
        value.questionPolicy,
        "questionPolicy",
        ["learning_assist", "full_review", "quick_review"],
      ),
      reviewMode: enumValue(
        value.reviewMode,
        "reviewMode",
        ["precision", "quick"],
      ),
      defaultTemplateId: safeId(
        value.defaultTemplateId,
        "defaultTemplateId",
      ),
      defaultTemplateSha256: digest,
      detectorPackId: safeId(value.detectorPackId, "detectorPackId"),
      ocrPackId: safeId(value.ocrPackId, "ocrPackId"),
      sam2PackId: safeId(value.sam2PackId, "sam2PackId"),
      reportProvider: enumValue(
        value.reportProvider,
        "reportProvider",
        ["codex", "claude", "local", "none"],
      ),
      outputLocationId: safeId(
        value.outputLocationId,
        "outputLocationId",
      ),
      candidateRescanFps: value.candidateRescanFps,
    };
  }

  if (
    channel === IPC_CHANNELS.PIPELINE_PROFILE_GET ||
    channel === IPC_CHANNELS.PIPELINE_LIST
  ) {
    const value = record(payload, ["projectId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
    };
  }

  if (channel === IPC_CHANNELS.PIPELINE_START) {
    const value = record(payload, [
      "projectId",
      "snapshotId",
      "fileName",
      "rightsConfirmed",
      "idempotencyKey",
    ]);
    const fileName = text(value.fileName, "fileName", { max: 255 });
    if (/[\\/]/.test(fileName)) {
      throw new IpcValidationError("fileName must not contain a path");
    }
    if (value.rightsConfirmed !== true) {
      throw new IpcValidationError(
        "rightsConfirmed must be explicitly true",
      );
    }
    const idempotencyKey = text(
      value.idempotencyKey,
      "idempotencyKey",
      { min: 16, max: 128 },
    );
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$/.test(idempotencyKey)) {
      throw new IpcValidationError("idempotencyKey is invalid");
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      snapshotId: uuid(value.snapshotId, "snapshotId"),
      fileName,
      rightsConfirmed: true,
      idempotencyKey,
    };
  }

  if (
    channel === IPC_CHANNELS.PIPELINE_GET ||
    channel === IPC_CHANNELS.PIPELINE_CANCEL ||
    channel === IPC_CHANNELS.PIPELINE_RESUME
  ) {
    const value = record(payload, ["projectId", "jobId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      jobId: uuid(value.jobId, "jobId"),
    };
  }

  if (channel === IPC_CHANNELS.PIPELINE_REVIEW_COMPLETE) {
    const value = record(payload, [
      "projectId",
      "jobId",
      "reviewCompletionId",
    ]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      jobId: uuid(value.jobId, "jobId"),
      reviewCompletionId: safeId(
        value.reviewCompletionId,
        "reviewCompletionId",
      ),
    };
  }

  if (channel === IPC_CHANNELS.CANDIDATE_QUESTIONS_LIST) {
    const value = record(payload, ["projectId", "jobId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      jobId: uuid(value.jobId, "jobId"),
    };
  }

  if (channel === IPC_CHANNELS.INCIDENT_LIST) {
    const value = record(payload, [
      "projectId",
      "analysisJobId",
      "includeInactive",
    ]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      analysisJobId: value.analysisJobId
        ? uuid(value.analysisJobId, "analysisJobId")
        : undefined,
      includeInactive: bool(
        value.includeInactive,
        "includeInactive",
        { optional: true },
      ),
    };
  }

  if (
    channel === IPC_CHANNELS.MASK_REFINEMENT_CAPABILITY ||
    channel === IPC_CHANNELS.MASK_REFINEMENT_REQUEST
  ) {
    const allowedKeys = [
      "projectId",
      "jobId",
      "questionId",
      "candidateId",
      ...(channel === IPC_CHANNELS.MASK_REFINEMENT_REQUEST
        ? ["click"]
        : []),
    ];
    const value = record(payload, allowedKeys);
    const candidateId = text(value.candidateId, "candidateId", {
      min: 20,
      max: 20,
    });
    if (!/^[a-f0-9]{20}$/.test(candidateId)) {
      throw new IpcValidationError(
        "candidateId must be a lowercase candidate identifier",
      );
    }
    const result = {
      projectId: uuid(value.projectId, "projectId"),
      jobId: uuid(value.jobId, "jobId"),
      questionId: uuid(value.questionId, "questionId"),
      candidateId,
    };
    if (channel === IPC_CHANNELS.MASK_REFINEMENT_REQUEST) {
      const click = record(value.click, ["x", "y"], "click");
      result.click = {
        x: finiteNumber(click.x, "click.x", {
          minimum: 0,
          maximum: 1,
        }),
        y: finiteNumber(click.y, "click.y", {
          minimum: 0,
          maximum: 1,
        }),
      };
    }
    return result;
  }

  if (channel === IPC_CHANNELS.CANDIDATE_ANSWER) {
    const value = record(payload, [
      "projectId",
      "jobId",
      "candidateId",
      "questionId",
      "maskRefinementId",
      "answer",
      "selectedIssueType",
      "severity",
      "maintenanceAction",
      "ocrText",
      "normalizedBox",
    ]);
    const candidateId = text(value.candidateId, "candidateId", {
      min: 20,
      max: 20,
    });
    if (!/^[a-f0-9]{20}$/.test(candidateId)) {
      throw new IpcValidationError(
        "candidateId must be a lowercase candidate identifier",
      );
    }
    let normalizedBox;
    if (value.normalizedBox !== undefined && value.normalizedBox !== null) {
      const box = record(
        value.normalizedBox,
        ["x", "y", "width", "height"],
        "normalizedBox",
      );
      for (const key of ["x", "y", "width", "height"]) {
        if (
          typeof box[key] !== "number" ||
          !Number.isFinite(box[key]) ||
          box[key] < 0 ||
          box[key] > 1 ||
          ((key === "width" || key === "height") && box[key] === 0)
        ) {
          throw new IpcValidationError(`normalizedBox.${key} is invalid`);
        }
      }
      if (box.x + box.width > 1 || box.y + box.height > 1) {
        throw new IpcValidationError(
          "normalizedBox must remain inside the image",
        );
      }
      normalizedBox = {
        x: box.x,
        y: box.y,
        width: box.width,
        height: box.height,
      };
    }
    const answer = enumValue(value.answer, "answer", [
      "yes",
      "no",
      "unsure",
      "edit",
      "skipped",
    ]);
    const maskRefinementId = text(
      value.maskRefinementId,
      "maskRefinementId",
      { min: 28, max: 28, optional: true },
    );
    if (
      maskRefinementId !== undefined &&
      !/^smr_[a-f0-9]{24}$/.test(maskRefinementId)
    ) {
      throw new IpcValidationError("maskRefinementId is invalid");
    }
    const questionId =
      value.questionId === undefined
        ? undefined
        : uuid(value.questionId, "questionId");
    if (
      answer === "edit" &&
      (Boolean(normalizedBox) === Boolean(maskRefinementId))
    ) {
      throw new IpcValidationError(
        "edited answer requires exactly one trusted geometry source",
      );
    }
    if (maskRefinementId && !questionId) {
      throw new IpcValidationError(
        "questionId is required for a mask refinement",
      );
    }
    if (questionId && !maskRefinementId) {
      throw new IpcValidationError(
        "questionId is accepted only with a mask refinement",
      );
    }
    const selectedIssueType = reviewIssueType(
      value.selectedIssueType,
      "selectedIssueType",
      { allowOther: true, optional: true },
    );
    const severity = enumValue(
      value.severity,
      "severity",
      ["minor", "moderate", "major", "urgent"],
      { optional: true },
    );
    const maintenanceAction = text(
      value.maintenanceAction,
      "maintenanceAction",
      { max: 500, optional: true },
    );
    const ocrText = text(value.ocrText, "ocrText", {
      max: 500,
      optional: true,
    });
    if (["yes", "edit"].includes(answer) && !selectedIssueType) {
      throw new IpcValidationError(
        "selectedIssueType is required for an approved answer",
      );
    }
    if (
      ["no", "unsure", "skipped"].includes(answer) &&
      (selectedIssueType ||
        severity ||
        maintenanceAction ||
        ocrText ||
        normalizedBox ||
        maskRefinementId ||
        questionId)
    ) {
      throw new IpcValidationError(
        "non-positive answers cannot contain positive label details",
      );
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      jobId: uuid(value.jobId, "jobId"),
      candidateId,
      questionId,
      maskRefinementId,
      answer,
      selectedIssueType,
      severity,
      maintenanceAction,
      ocrText,
      normalizedBox,
    };
  }

  if (channel === IPC_CHANNELS.AUTO_LABEL_COMPILE) {
    const value = record(payload, ["projectId", "command", "issueType"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      command: text(value.command, "command", { min: 3, max: 500 }),
      issueType: reviewIssueType(
        value.issueType,
        "issueType",
        { allowOther: true, optional: true },
      ),
    };
  }

  if (channel === IPC_CHANNELS.AUTO_LABEL_RUN) {
    const value = record(payload, [
      "projectId",
      "jobId",
      "recipeId",
      "command",
      "issueType",
      "scope",
      "mode",
      "userConfirmed",
    ]);
    const recipeId = text(value.recipeId, "recipeId", { min: 64, max: 64 });
    if (!/^[a-f0-9]{64}$/.test(recipeId)) {
      throw new IpcValidationError(
        "recipeId must be a lowercase SHA-256 value",
      );
    }
    const scope = enumValue(value.scope, "scope", [
      "current_job",
      "project",
    ]);
    const jobId =
      value.jobId === undefined || value.jobId === null || value.jobId === ""
        ? undefined
        : uuid(value.jobId, "jobId");
    if (scope === "current_job" && !jobId) {
      throw new IpcValidationError(
        "jobId is required for current_job auto-labeling",
      );
    }
    const userConfirmed = bool(value.userConfirmed, "userConfirmed");
    if (userConfirmed !== true) {
      throw new IpcValidationError(
        "userConfirmed must be true before auto-labeling starts",
      );
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      jobId,
      recipeId,
      command: text(value.command, "command", { min: 3, max: 500 }),
      issueType: reviewIssueType(
        value.issueType,
        "issueType",
        { allowOther: true },
      ),
      scope,
      mode: enumValue(value.mode, "mode", [
        "proposal_only",
        "assisted_auto",
      ]),
      userConfirmed,
    };
  }

  if (channel === IPC_CHANNELS.REVIEW_TEMPORAL_INCIDENTS) {
    const value = record(payload, [
      "projectId",
      "detections",
      "provenance",
      "policy",
    ]);
    const projectId = uuid(value.projectId, "projectId");
    if (!Array.isArray(value.detections) || value.detections.length > 100_000) {
      throw new IpcValidationError("detections must be a bounded array");
    }
    const detections = value.detections.map((raw, index) => {
      const item = record(
        raw,
        [
          "detectionId",
          "timestampSeconds",
          "issueType",
          "score",
          "reviewOnly",
          "automaticLabelingAllowed",
          "normalizedBox",
          "trackHint",
          "frameIndex",
          "scoreSemantics",
        ],
        `detections[${index}]`,
      );
      if (
        bool(item.reviewOnly, `detections[${index}].reviewOnly`) !== true ||
        bool(
          item.automaticLabelingAllowed,
          `detections[${index}].automaticLabelingAllowed`,
        ) !== false
      ) {
        throw new IpcValidationError(
          `detections[${index}] must remain review-only`,
        );
      }
      return {
        detectionId: reviewIdentifier(
          item.detectionId,
          `detections[${index}].detectionId`,
        ),
        timestampSeconds: finiteNumber(
          item.timestampSeconds,
          `detections[${index}].timestampSeconds`,
          { minimum: 0, maximum: 2_678_400 },
        ),
        issueType: reviewIssueType(
          item.issueType,
          `detections[${index}].issueType`,
          { allowOther: true },
        ),
        score: finiteNumber(item.score, `detections[${index}].score`, {
          minimum: 0,
          maximum: 1,
        }),
        reviewOnly: true,
        automaticLabelingAllowed: false,
        normalizedBox:
          item.normalizedBox === undefined
            ? undefined
            : reviewNormalizedBox(
                item.normalizedBox,
                `detections[${index}].normalizedBox`,
              ),
        trackHint: reviewIdentifier(
          item.trackHint,
          `detections[${index}].trackHint`,
          { optional: true },
        ),
        frameIndex:
          item.frameIndex === undefined
            ? undefined
            : safeCount(item.frameIndex, `detections[${index}].frameIndex`, {
                maximum: Number.MAX_SAFE_INTEGER,
              }),
        scoreSemantics: enumValue(
          item.scoreSemantics,
          `detections[${index}].scoreSemantics`,
          [
            "model_score_not_probability",
            "zero_shot_relative_not_probability",
          ],
          { optional: true },
        ),
      };
    });
    if (
      new Set(detections.map((item) => item.detectionId)).size !==
      detections.length
    ) {
      throw new IpcValidationError("detections must have unique identifiers");
    }
    const provenance = record(
      value.provenance,
      ["projectId", "analysisJobId", "sourceFileSha256", "source"],
      "provenance",
    );
    const provenanceProjectId = uuid(
      provenance.projectId,
      "provenance.projectId",
    );
    if (provenanceProjectId !== projectId) {
      throw new IpcValidationError(
        "provenance.projectId must match projectId",
      );
    }
    const sourceFileSha256 = text(
      provenance.sourceFileSha256,
      "provenance.sourceFileSha256",
      { min: 64, max: 64 },
    );
    if (!/^[a-f0-9]{64}$/.test(sourceFileSha256)) {
      throw new IpcValidationError(
        "provenance.sourceFileSha256 must be a lowercase SHA-256",
      );
    }
    const policy = record(
      value.policy,
      [
        "maxGapSeconds",
        "minIou",
        "maxCenterDistance",
        "strictTrackSeparation",
        "allowTemporalOnlyWithoutSpatialHints",
      ],
      "policy",
    );
    return {
      projectId,
      detections,
      provenance: {
        projectId: provenanceProjectId,
        analysisJobId: uuid(
          provenance.analysisJobId,
          "provenance.analysisJobId",
        ),
        sourceFileSha256,
        source: enumValue(provenance.source, "provenance.source", [
          "classified_review_proposals",
        ]),
      },
      policy: {
        maxGapSeconds: finiteNumber(
          policy.maxGapSeconds,
          "policy.maxGapSeconds",
          { minimum: 0.001, maximum: 60 },
        ),
        minIou: finiteNumber(policy.minIou, "policy.minIou", {
          minimum: 0,
          maximum: 1,
        }),
        maxCenterDistance: finiteNumber(
          policy.maxCenterDistance,
          "policy.maxCenterDistance",
          { minimum: 0, maximum: Math.SQRT2 },
        ),
        strictTrackSeparation: bool(
          policy.strictTrackSeparation,
          "policy.strictTrackSeparation",
        ),
        allowTemporalOnlyWithoutSpatialHints: bool(
          policy.allowTemporalOnlyWithoutSpatialHints,
          "policy.allowTemporalOnlyWithoutSpatialHints",
        ),
      },
    };
  }

  if (channel === IPC_CHANNELS.REVIEW_MODEL_VALIDATION) {
    const value = record(payload, [
      "projectId",
      "modelVersion",
      "truthRecords",
      "predictionRecords",
    ]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      modelVersion: reviewIdentifier(value.modelVersion, "modelVersion"),
      truthRecords: validationTruthRecords(
        value.truthRecords,
        "truthRecords",
      ),
      predictionRecords: validationPredictionRecords(
        value.predictionRecords,
        "predictionRecords",
      ),
    };
  }

  if (channel === IPC_CHANNELS.REVIEW_MODEL_PROMOTION) {
    const value = record(payload, [
      "projectId",
      "activeModelVersion",
      "candidateModelVersion",
      "truthRecords",
      "activePredictionRecords",
      "candidatePredictionRecords",
      "requiredIssueTypes",
      "minimumSamplesPerType",
      "maximumRecallDrop",
    ]);
    const activeModelVersion = reviewIdentifier(
      value.activeModelVersion,
      "activeModelVersion",
    );
    const candidateModelVersion = reviewIdentifier(
      value.candidateModelVersion,
      "candidateModelVersion",
    );
    if (activeModelVersion === candidateModelVersion) {
      throw new IpcValidationError(
        "activeModelVersion and candidateModelVersion must differ",
      );
    }
    let requiredIssueTypes;
    if (value.requiredIssueTypes !== undefined) {
      if (
        !Array.isArray(value.requiredIssueTypes) ||
        value.requiredIssueTypes.length < 1 ||
        value.requiredIssueTypes.length > 12
      ) {
        throw new IpcValidationError(
          "requiredIssueTypes must contain 1 to 12 types",
        );
      }
      requiredIssueTypes = value.requiredIssueTypes.map((item, index) =>
        reviewIssueType(item, `requiredIssueTypes[${index}]`),
      );
      if (new Set(requiredIssueTypes).size !== requiredIssueTypes.length) {
        throw new IpcValidationError("requiredIssueTypes must be unique");
      }
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      activeModelVersion,
      candidateModelVersion,
      truthRecords: validationTruthRecords(
        value.truthRecords,
        "truthRecords",
      ),
      activePredictionRecords: validationPredictionRecords(
        value.activePredictionRecords,
        "activePredictionRecords",
      ),
      candidatePredictionRecords: validationPredictionRecords(
        value.candidatePredictionRecords,
        "candidatePredictionRecords",
      ),
      requiredIssueTypes,
      minimumSamplesPerType: safeCount(
        value.minimumSamplesPerType,
        "minimumSamplesPerType",
        { minimum: 1, maximum: Number.MAX_SAFE_INTEGER },
      ),
      maximumRecallDrop: finiteNumber(
        value.maximumRecallDrop,
        "maximumRecallDrop",
        { minimum: Number.MIN_VALUE, maximum: 1 },
      ),
    };
  }

  if (
    channel === IPC_CHANNELS.LABEL_STATS ||
    channel === IPC_CHANNELS.DATASET_LIST ||
    channel === IPC_CHANNELS.WEAK_CASE_SOURCES_LIST
  ) {
    const value = record(payload, ["projectId"]);
    return { projectId: uuid(value.projectId, "projectId") };
  }

  if (channel === IPC_CHANNELS.WEAK_CASE_SEARCH) {
    const value = record(payload, [
      "projectId",
      "issueType",
      "queryText",
      "limit",
    ]);
    const issueType = reviewIssueType(
      value.issueType,
      "issueType",
      { optional: true },
    );
    const queryText = text(value.queryText, "queryText", {
      max: 240,
      optional: true,
    });
    if (!issueType && !queryText) {
      throw new IpcValidationError(
        "weak-case search requires issueType or queryText",
      );
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      issueType,
      queryText,
      limit: safeCount(value.limit ?? 5, "limit", {
        minimum: 3,
        maximum: 5,
      }),
    };
  }

  if (channel === IPC_CHANNELS.DATASET_CREATE) {
    const value = record(payload, ["projectId", "userConfirmed"]);
    const userConfirmed = bool(value.userConfirmed, "userConfirmed");
    if (userConfirmed !== true) {
      throw new IpcValidationError(
        "userConfirmed must be true before creating a dataset snapshot",
      );
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      userConfirmed,
    };
  }

  if (channel === IPC_CHANNELS.QUESTIONS_LIST) {
    const value = record(payload, ["projectId", "status", "policyMode"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      status: enumValue(
        value.status,
        "status",
        ["QUEUED", "ANSWERED_ACCEPT", "ANSWERED_REJECT", "ANSWERED_UNSURE", "CONFIRMED"],
        { optional: true },
      ),
      policyMode: enumValue(
        value.policyMode,
        "policyMode",
        ["learning_assist", "full_review", "quick_review"],
        { optional: true },
      ),
    };
  }

  if (channel === IPC_CHANNELS.QUESTION_ANSWER) {
    const value = record(payload, [
      "projectId",
      "questionId",
      "answer",
      "selectedClass",
      "selectedSeverity",
      "selectedAction",
      "finalGeometry",
      "ocrText",
      "includeInTraining",
    ]);
    const answer = enumValue(value.answer, "answer", ["yes", "no", "unsure", "edit"]);
    const geometry = optionalGeometry(value.finalGeometry);
    if (answer === "edit" && !geometry) {
      throw new IpcValidationError("finalGeometry is required for edit answers");
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      questionId: uuid(value.questionId, "questionId"),
      answer,
      selectedClass: safeId(value.selectedClass, "selectedClass", { optional: true }),
      selectedSeverity: enumValue(
        value.selectedSeverity,
        "selectedSeverity",
        ["minor", "moderate", "major", "urgent"],
        { optional: true },
      ),
      selectedAction: text(value.selectedAction, "selectedAction", {
        max: 500,
        optional: true,
      }),
      finalGeometry: geometry,
      ocrText: text(value.ocrText, "ocrText", { max: 500, optional: true }),
      includeInTraining: bool(value.includeInTraining, "includeInTraining", {
        optional: true,
      }),
    };
  }

  if (channel === IPC_CHANNELS.HISTORY_GET) {
    const value = record(payload, ["projectId", "tripId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      tripId: uuid(value.tripId, "tripId"),
    };
  }

  if (channel === IPC_CHANNELS.GPX_TRACK_IMPORT) {
    const value = record(payload, [
      "projectId",
      "tripId",
      "gpxSelectionId",
      "videoOffsetSeconds",
    ]);
    const videoOffsetSeconds = value.videoOffsetSeconds ?? 0;
    if (
      typeof videoOffsetSeconds !== "number" ||
      !Number.isFinite(videoOffsetSeconds) ||
      Math.abs(videoOffsetSeconds) > 86_400
    ) {
      throw new IpcValidationError(
        "videoOffsetSeconds must be a finite value within plus or minus 24 hours",
      );
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      tripId: uuid(value.tripId, "tripId"),
      gpxSelectionId: uuid(value.gpxSelectionId, "gpxSelectionId"),
      videoOffsetSeconds: Math.round(videoOffsetSeconds * 1_000) / 1_000,
    };
  }

  if (channel === IPC_CHANNELS.GPX_TRACK_GET) {
    const value = record(payload, ["projectId", "tripId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      tripId: uuid(value.tripId, "tripId"),
    };
  }

  if (channel === IPC_CHANNELS.GPX_TRACK_OFFSET) {
    const value = record(payload, [
      "projectId",
      "tripId",
      "videoOffsetSeconds",
    ]);
    if (
      typeof value.videoOffsetSeconds !== "number" ||
      !Number.isFinite(value.videoOffsetSeconds) ||
      Math.abs(value.videoOffsetSeconds) > 86_400
    ) {
      throw new IpcValidationError(
        "videoOffsetSeconds must be a finite value within plus or minus 24 hours",
      );
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      tripId: uuid(value.tripId, "tripId"),
      videoOffsetSeconds:
        Math.round(value.videoOffsetSeconds * 1_000) / 1_000,
    };
  }

  if (channel === IPC_CHANNELS.PROVIDER_LOGIN) {
    const value = record(payload, ["provider"]);
    return {
      provider: enumValue(value.provider, "provider", ["codex", "claude"]),
    };
  }

  if (channel === IPC_CHANNELS.REPORT_DRAFT) {
    const value = record(payload, ["projectId", "provider", "incidentIds", "consent"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      provider: enumValue(value.provider, "provider", ["codex", "claude", "local"]),
      incidentIds: stringArray(value.incidentIds ?? [], "incidentIds", { maxItems: 500 }),
      consent: bool(value.consent, "consent"),
    };
  }

  if (channel === IPC_CHANNELS.REPORT_GENERATE) {
    const value = record(payload, [
      "projectId",
      "analysisJobId",
      "sourceFileSha256",
      "pipelineJobId",
      "pipelineInputFingerprint",
      "pipelineProfileSha256",
      "provider",
      "consent",
      "projectLabel",
      "incidentIds",
      "documentStructureSummary",
      "mapRefs",
    ]);
    const incidentIds = stringArray(value.incidentIds, "incidentIds", {
      maxItems: 200,
      itemMax: 36,
    }).map((incidentId, index) =>
      uuid(incidentId, `incidentIds[${index}]`),
    );
    if (incidentIds.length < 1) {
      throw new IpcValidationError(
        "incidentIds must contain 1 to 200 approved incident IDs",
      );
    }
    if (
      new Set(incidentIds).size !== incidentIds.length
    ) {
      throw new IpcValidationError("incidentIds must be unique");
    }
    const structure = record(
      value.documentStructureSummary,
      [
        "documentSnapshotId",
        "fieldTargets",
        "cellTargets",
        "tableTargets",
        "imageTargets",
        "mapTargets",
        "sectionHeadings",
      ],
      "documentStructureSummary",
    );
    const targetArray = (key) => {
      const items = stringArray(
        structure[key] ?? [],
        `documentStructureSummary.${key}`,
        { maxItems: 100 },
      ).map((item, index) =>
        safeId(item, `documentStructureSummary.${key}[${index}]`),
      );
      if (new Set(items).size !== items.length) {
        throw new IpcValidationError(
          `documentStructureSummary.${key} must be unique`,
        );
      }
      return items;
    };
    const sectionHeadings = stringArray(
      structure.sectionHeadings ?? [],
      "documentStructureSummary.sectionHeadings",
      { maxItems: 100, itemMax: 200 },
    );
    if (new Set(sectionHeadings).size !== sectionHeadings.length) {
      throw new IpcValidationError(
        "documentStructureSummary.sectionHeadings must be unique",
      );
    }
    const mapRefs = stringArray(value.mapRefs ?? [], "mapRefs", {
      maxItems: 20,
      itemMax: 128,
    });
    if (
      new Set(mapRefs).size !== mapRefs.length ||
      mapRefs.some(
        (item) => !/^map:[A-Za-z0-9][A-Za-z0-9._:-]{0,122}$/.test(item),
      )
    ) {
      throw new IpcValidationError("mapRefs contains an invalid reference");
    }
    const sourceFileSha256 = text(
      value.sourceFileSha256,
      "sourceFileSha256",
      { min: 64, max: 64 },
    );
    if (!/^[a-f0-9]{64}$/.test(sourceFileSha256)) {
      throw new IpcValidationError(
        "sourceFileSha256 must be a lowercase SHA-256 value",
      );
    }
    const rawPipelineBinding = [
      value.pipelineJobId,
      value.pipelineInputFingerprint,
      value.pipelineProfileSha256,
    ];
    if (
      rawPipelineBinding.some((item) => item !== undefined) &&
      rawPipelineBinding.some((item) => item === undefined)
    ) {
      throw new IpcValidationError(
        "pipeline provenance must be supplied as one complete binding",
      );
    }
    const pipelineJobId =
      value.pipelineJobId === undefined
        ? undefined
        : uuid(value.pipelineJobId, "pipelineJobId");
    const pipelineDigest = (raw, label) => {
      if (raw === undefined) return undefined;
      const digest = text(raw, label, { min: 64, max: 64 });
      if (!/^[a-f0-9]{64}$/.test(digest)) {
        throw new IpcValidationError(
          `${label} must be a lowercase SHA-256 value`,
        );
      }
      return digest;
    };
    return {
      projectId: uuid(value.projectId, "projectId"),
      analysisJobId: uuid(value.analysisJobId, "analysisJobId"),
      sourceFileSha256,
      pipelineJobId,
      pipelineInputFingerprint: pipelineDigest(
        value.pipelineInputFingerprint,
        "pipelineInputFingerprint",
      ),
      pipelineProfileSha256: pipelineDigest(
        value.pipelineProfileSha256,
        "pipelineProfileSha256",
      ),
      provider: enumValue(value.provider, "provider", ["codex", "claude"]),
      consent: bool(value.consent, "consent"),
      projectLabel: text(value.projectLabel, "projectLabel", {
        max: 200,
        optional: true,
      }),
      incidentIds,
      documentStructureSummary: {
        documentSnapshotId: safeId(
          structure.documentSnapshotId,
          "documentStructureSummary.documentSnapshotId",
        ),
        fieldTargets: targetArray("fieldTargets"),
        cellTargets: targetArray("cellTargets"),
        tableTargets: targetArray("tableTargets"),
        imageTargets: targetArray("imageTargets"),
        mapTargets: targetArray("mapTargets"),
        sectionHeadings,
      },
      mapRefs,
    };
  }

  if (channel === IPC_CHANNELS.REPORT_PLAN) {
    const value = record(payload, ["projectId", "draftId", "documentSnapshotId"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      draftId: uuid(value.draftId, "draftId"),
      documentSnapshotId: safeId(value.documentSnapshotId, "documentSnapshotId"),
    };
  }

  if (channel === IPC_CHANNELS.REPORT_APPLY) {
    const value = record(payload, ["projectId", "plan"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      plan: validatePlan(value.plan),
    };
  }

  if (channel === IPC_CHANNELS.REPORT_ACCEPT || channel === IPC_CHANNELS.REPORT_REJECT) {
    const value = record(payload, ["projectId", "reportId", "operationIds"]);
    return {
      projectId: uuid(value.projectId, "projectId"),
      reportId: uuid(value.reportId, "reportId"),
      operationIds: stringArray(value.operationIds ?? [], "operationIds", { maxItems: 100 }),
    };
  }

  if (channel === IPC_CHANNELS.HWP_PREVIEW || channel === IPC_CHANNELS.HWP_APPLY) {
    const value = record(
      payload,
      channel === IPC_CHANNELS.HWP_APPLY
        ? ["projectId", "documentSelectionId", "plan", "approvalToken"]
        : ["projectId", "documentSelectionId", "plan"],
    );
    const normalized = {
      projectId: uuid(value.projectId, "projectId"),
      documentSelectionId: uuid(
        value.documentSelectionId,
        "documentSelectionId",
      ),
      plan: validatePlan(value.plan),
    };
    if (channel === IPC_CHANNELS.HWP_APPLY) {
      const approvalToken = text(value.approvalToken, "approvalToken", {
        min: 64,
        max: 64,
      });
      if (!/^[a-f0-9]{64}$/.test(approvalToken)) {
        throw new IpcValidationError(
          "approvalToken must be an opaque lowercase token",
        );
      }
      normalized.approvalToken = approvalToken;
    }
    return normalized;
  }

  if (channel === IPC_CHANNELS.HWP_ROLLBACK) {
    const value = record(payload, ["projectId", "rollbackToken"]);
    const rollbackToken = text(value.rollbackToken, "rollbackToken", {
      min: 32,
      max: 32,
    });
    if (!/^[a-f0-9]{32}$/.test(rollbackToken)) {
      throw new IpcValidationError("rollbackToken must be an opaque lowercase token");
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      rollbackToken,
    };
  }

  if (channel === IPC_CHANNELS.EXPORT_CREATE) {
    const value = record(payload, [
      "projectId",
      "analysisJobId",
      "artifacts",
      "destinationSelectionId",
    ]);
    const allowedArtifacts = new Set([
      "raw_detections",
      "reviewed_incidents",
      "label_questions",
      "annotations",
      "ocr_observations",
      "route_points",
      "maintenance_actions",
      "report_payload",
      "manifest",
    ]);
    const artifacts = stringArray(value.artifacts, "artifacts", { maxItems: 9 });
    if (artifacts.length === 0 || artifacts.some((artifact) => !allowedArtifacts.has(artifact))) {
      throw new IpcValidationError("artifacts contains an unsupported export");
    }
    return {
      projectId: uuid(value.projectId, "projectId"),
      analysisJobId: uuid(value.analysisJobId, "analysisJobId"),
      artifacts: [...new Set(artifacts)],
      destinationSelectionId: value.destinationSelectionId
        ? uuid(value.destinationSelectionId, "destinationSelectionId")
        : undefined,
    };
  }

  throw new IpcValidationError("No validator is registered for this channel");
}

export function toBridgePayload(channel, payload) {
  const value = validateIpcPayload(channel, payload);
  const snake = (input) => {
    if (Array.isArray(input)) return input.map(snake);
    if (!input || typeof input !== "object") return input;
    return Object.fromEntries(
      Object.entries(input)
        .filter(([, item]) => item !== undefined)
        .map(([key, item]) => [
          key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`),
          snake(item),
        ]),
    );
  };
  return snake(value);
}
