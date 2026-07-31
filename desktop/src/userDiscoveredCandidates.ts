import {
  normalizeIssueTypeApi,
  type SupportedIssueTypeApi,
} from "./issueTypes";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const CANDIDATE_ID_PATTERN = /^[a-f0-9]{20}$/;
const FRAME_ID_PATTERN = /^[a-f0-9]{24}$/;
const ARTIFACT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;

export type UserDiscoveredPointSelection = {
  kind: "point";
  x: number;
  y: number;
};

export type UserDiscoveredBoxSelection = {
  kind: "box";
  x: number;
  y: number;
  width: number;
  height: number;
};

export type UserDiscoveredSelection =
  | UserDiscoveredPointSelection
  | UserDiscoveredBoxSelection;

export type UserDiscoveredCandidateRequest = {
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  timestampSeconds: number;
  selection: UserDiscoveredSelection;
  issueTypeProposal?: SupportedIssueTypeApi;
  requestSam2Mask: boolean;
  expectedQuestionQueueRevision: string;
};

export type UserDiscoveredArtifact = {
  artifactId: string;
  mediaType: "image/jpeg" | "video/mp4";
  sizeBytes: number;
  sha256: string;
  durationSeconds: number | null;
};

export type UserDiscoveredCandidateReceipt = {
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  candidateId: string;
  questionId: string;
  requestedTimestampSeconds: number;
  selectedFrameTimestampSeconds: number;
  selectedFrameId: string;
  selectedFrameSha256: string;
  selection: UserDiscoveredSelection;
  issueTypeProposal: SupportedIssueTypeApi | null;
  reviewIssueType: SupportedIssueTypeApi | "issue_type_unclassified";
  requestSha256: string;
  denseIndexSha256: string;
  promotedModelVersionId: string;
  promotedModelArtifactManifestSha256: string;
  promotedModelInferenceSha256: string;
  datasetVersionId: string;
  datasetHash: string;
  sourceAnalysisLineageSha256: string;
  overlayId: string;
  overlayManifestSha256: string;
  discoveryRevision: string;
  representativeImage: UserDiscoveredArtifact;
  previewClip: UserDiscoveredArtifact;
  sam2RefinementRequested: boolean;
  reviewEventSetId: string;
  reviewEventSetRevision: string;
  reviewEventSetManifestSha256: string;
  reviewEventId: string;
  reviewEventRevision: string;
  questionQueueRevision: string;
  questionCatalogRevision: string;
  questionCatalogCollectionRevision: string;
  idempotentReplay: boolean;
  reviewOnly: true;
};

export type UserDiscoveredReviewFrame = {
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  requestedTimestampSeconds: number;
  selectedFrameId: string;
  selectedFrameIndex: number;
  selectedFrameTimestampSeconds: number;
  selectedFrameSha256: string;
  selectedFrameSizeBytes: number;
  sourceDurationSeconds: number;
  indexedDurationSeconds: number;
  samplingFps: 2;
  denseIndexSha256: string;
  promotedModelVersionId: string;
  sourceAnalysisLineageSha256: string;
  reviewFrameSha256: string;
  questionQueueRevision: string;
  bytes: Uint8Array;
};

type ParseExpected = {
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  requestedTimestampSeconds: number;
  selection: UserDiscoveredSelection;
};

type VideoPointInput = {
  clientX: number;
  clientY: number;
  left: number;
  top: number;
  width: number;
  height: number;
  videoWidth: number;
  videoHeight: number;
  objectFit: "contain" | "cover" | "fill";
};

const RESPONSE_KEYS = [
  "schema_version",
  "project_id",
  "pipeline_job_id",
  "analysis_job_id",
  "candidate_id",
  "question_id",
  "candidate_kind",
  "requested_timestamp_seconds",
  "selected_frame_timestamp_seconds",
  "selected_frame_id",
  "selected_frame_sha256",
  "selection",
  "issue_type_proposal",
  "review_issue_type",
  "request_sha256",
  "dense_index_sha256",
  "promoted_model_version_id",
  "promoted_model_artifact_manifest_sha256",
  "promoted_model_inference_sha256",
  "dataset_version_id",
  "dataset_hash",
  "source_analysis_lineage_sha256",
  "overlay_id",
  "overlay_manifest_sha256",
  "discovery_revision",
  "representative_image",
  "preview_clip",
  "sam2_refinement_request",
  "review_event_set_id",
  "review_event_set_revision",
  "review_event_set_manifest_sha256",
  "review_event_binding",
  "question_queue_revision",
  "question_catalog_revision",
  "question_catalog_collection_revision",
  "idempotent_replay",
  "review_only",
  "annotations_created",
  "incidents_created",
  "training_members_created",
  "report_truth_records_created",
] as const;
const REVIEW_FRAME_KEYS = [
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
] as const;

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
  label: string,
) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
}

function string(
  value: unknown,
  label: string,
  pattern: RegExp,
  maximum = 160,
) {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > maximum ||
    !pattern.test(value)
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function uuid(value: unknown, label: string) {
  return string(value, label, UUID_PATTERN, 64).toLowerCase();
}

function sha256(value: unknown, label: string) {
  return string(value, label, SHA256_PATTERN, 64);
}

function finiteNumber(
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
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function positiveInteger(value: unknown, label: string) {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function boundedInteger(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
) {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

async function byteSha256(bytes: Uint8Array) {
  if (!globalThis.crypto?.subtle) {
    throw new Error("검토 프레임 해시를 확인할 수 없습니다.");
  }
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    bytes as unknown as BufferSource,
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function parseSelection(
  value: unknown,
  label: string,
): UserDiscoveredSelection {
  const source = record(value, label);
  if (source.kind === "point") {
    exactKeys(source, ["kind", "x", "y"], label);
    return {
      kind: "point",
      x: finiteNumber(source.x, `${label} X`, 0, 1),
      y: finiteNumber(source.y, `${label} Y`, 0, 1),
    };
  }
  if (source.kind === "box") {
    exactKeys(source, ["kind", "x", "y", "width", "height"], label);
    const x = finiteNumber(source.x, `${label} X`, 0, 1);
    const y = finiteNumber(source.y, `${label} Y`, 0, 1);
    const width = finiteNumber(source.width, `${label} 너비`, Number.EPSILON, 1);
    const height = finiteNumber(
      source.height,
      `${label} 높이`,
      Number.EPSILON,
      1,
    );
    if (x + width > 1.00000001 || y + height > 1.00000001) {
      throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
    }
    return { kind: "box", x, y, width, height };
  }
  throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
}

function sameSelection(
  left: UserDiscoveredSelection,
  right: UserDiscoveredSelection,
) {
  if (left.kind !== right.kind) return false;
  const near = (a: number, b: number) => Math.abs(a - b) <= 0.000001;
  if (!near(left.x, right.x) || !near(left.y, right.y)) return false;
  return left.kind === "point" && right.kind === "point"
    ? true
    : left.kind === "box" &&
        right.kind === "box" &&
        near(left.width, right.width) &&
        near(left.height, right.height);
}

function parseIssueType(
  value: unknown,
  label: string,
  options: { nullable?: boolean; allowUnclassified?: boolean } = {},
) {
  if (options.nullable && value === null) return null;
  if (
    options.allowUnclassified &&
    value === "issue_type_unclassified"
  ) {
    return value;
  }
  const issueType = normalizeIssueTypeApi(value);
  if (!issueType) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return issueType;
}

function parseArtifact(
  value: unknown,
  expectedMediaType: "image/jpeg" | "video/mp4",
) {
  const source = record(value, "누락 후보 근거 파일");
  exactKeys(
    source,
    [
      "artifact_id",
      "media_type",
      "size_bytes",
      "sha256",
      "duration_seconds",
      "duration_contract",
    ],
    "누락 후보 근거 파일",
  );
  if (source.media_type !== expectedMediaType) {
    throw new Error("누락 후보 근거 파일 응답 형식이 올바르지 않습니다.");
  }
  const durationSeconds =
    expectedMediaType === "video/mp4"
      ? finiteNumber(
          source.duration_seconds,
          "누락 후보 클립 길이",
          2,
          5,
        )
      : null;
  if (
    (expectedMediaType === "video/mp4" &&
      source.duration_contract !==
        "candidate_overlay_limit_2_to_5_seconds") ||
    (expectedMediaType === "image/jpeg" &&
      (source.duration_seconds !== null ||
        source.duration_contract !== null))
  ) {
    throw new Error("누락 후보 근거 파일 응답 형식이 올바르지 않습니다.");
  }
  return {
    artifactId: string(
      source.artifact_id,
      "누락 후보 근거 ID",
      ARTIFACT_ID_PATTERN,
    ),
    mediaType: expectedMediaType,
    sizeBytes: positiveInteger(source.size_bytes, "누락 후보 근거 크기"),
    sha256: sha256(source.sha256, "누락 후보 근거 해시"),
    durationSeconds,
  } satisfies UserDiscoveredArtifact;
}

function boundedOpaqueJson(value: unknown) {
  if (value === null) return false;
  const encoded = JSON.stringify(value);
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    encoded.length > 16_384
  ) {
    throw new Error("SAM2 요청 응답 형식이 올바르지 않습니다.");
  }
  return true;
}

export function parseUserDiscoveredCandidateReceipt(
  value: unknown,
  expected: ParseExpected,
): UserDiscoveredCandidateReceipt {
  const source = record(value, "사용자 발견 누락 후보");
  exactKeys(source, RESPONSE_KEYS, "사용자 발견 누락 후보");
  if (
    source.schema_version !==
      "trafficvisson.user-discovered-candidate-api.v1" ||
    source.candidate_kind !== "user_discovered" ||
    source.review_only !== true ||
    source.annotations_created !== 0 ||
    source.incidents_created !== 0 ||
    source.training_members_created !== 0 ||
    source.report_truth_records_created !== 0 ||
    typeof source.idempotent_replay !== "boolean"
  ) {
    throw new Error("사용자 발견 누락 후보 응답 형식이 올바르지 않습니다.");
  }
  const projectId = uuid(source.project_id, "프로젝트 ID");
  const pipelineJobId = uuid(source.pipeline_job_id, "파이프라인 작업 ID");
  const analysisJobId = uuid(source.analysis_job_id, "분석 작업 ID");
  const requestedTimestampSeconds = finiteNumber(
    source.requested_timestamp_seconds,
    "요청 영상 시각",
    0,
    2_678_400,
  );
  const selection = parseSelection(source.selection, "누락 후보 선택 영역");
  if (
    projectId !== expected.projectId.toLowerCase() ||
    pipelineJobId !== expected.pipelineJobId.toLowerCase() ||
    analysisJobId !== expected.analysisJobId.toLowerCase() ||
    Math.abs(
      requestedTimestampSeconds - expected.requestedTimestampSeconds,
    ) > 0.001 ||
    !sameSelection(selection, expected.selection)
  ) {
    throw new Error(
      "사용자 발견 누락 후보가 현재 영상 선택과 일치하지 않습니다.",
    );
  }
  const binding = record(
    source.review_event_binding,
    "누락 후보 검토 이벤트",
  );
  exactKeys(
    binding,
    ["event_id", "event_revision", "representative_candidate_id"],
    "누락 후보 검토 이벤트",
  );
  const candidateId = string(
    source.candidate_id,
    "누락 후보 ID",
    CANDIDATE_ID_PATTERN,
  );
  if (
    string(
      binding.representative_candidate_id,
      "누락 후보 대표 ID",
      CANDIDATE_ID_PATTERN,
    ) !== candidateId
  ) {
    throw new Error("누락 후보 검토 이벤트 응답 형식이 올바르지 않습니다.");
  }
  const issueTypeProposal = parseIssueType(
    source.issue_type_proposal,
    "누락 후보 유형",
    { nullable: true },
  ) as SupportedIssueTypeApi | null;
  const reviewIssueType = parseIssueType(
    source.review_issue_type,
    "검토 질문 유형",
    { allowUnclassified: true },
  ) as SupportedIssueTypeApi | "issue_type_unclassified";
  return {
    projectId,
    pipelineJobId,
    analysisJobId,
    candidateId,
    questionId: uuid(source.question_id, "누락 후보 질문 ID"),
    requestedTimestampSeconds,
    selectedFrameTimestampSeconds: finiteNumber(
      source.selected_frame_timestamp_seconds,
      "선택 프레임 시각",
      0,
      2_678_400,
    ),
    selectedFrameId: string(
      source.selected_frame_id,
      "선택 프레임 ID",
      FRAME_ID_PATTERN,
    ),
    selectedFrameSha256: sha256(
      source.selected_frame_sha256,
      "선택 프레임 해시",
    ),
    selection,
    issueTypeProposal,
    reviewIssueType,
    requestSha256: sha256(source.request_sha256, "누락 후보 요청 해시"),
    denseIndexSha256: sha256(
      source.dense_index_sha256,
      "2fps 인덱스 해시",
    ),
    promotedModelVersionId: uuid(
      source.promoted_model_version_id,
      "승격 모델 버전 ID",
    ),
    promotedModelArtifactManifestSha256: sha256(
      source.promoted_model_artifact_manifest_sha256,
      "승격 모델 매니페스트 해시",
    ),
    promotedModelInferenceSha256: sha256(
      source.promoted_model_inference_sha256,
      "승격 모델 추론 해시",
    ),
    datasetVersionId: uuid(
      source.dataset_version_id,
      "데이터셋 버전 ID",
    ),
    datasetHash: sha256(source.dataset_hash, "데이터셋 해시"),
    sourceAnalysisLineageSha256: sha256(
      source.source_analysis_lineage_sha256,
      "독립 조사 분석 계보 해시",
    ),
    overlayId: string(
      source.overlay_id,
      "후보 오버레이 ID",
      /^ceo_[a-f0-9]{32}$/,
    ),
    overlayManifestSha256: sha256(
      source.overlay_manifest_sha256,
      "후보 오버레이 해시",
    ),
    discoveryRevision: sha256(
      source.discovery_revision,
      "누락 후보 발견 리비전",
    ),
    representativeImage: parseArtifact(
      source.representative_image,
      "image/jpeg",
    ),
    previewClip: parseArtifact(source.preview_clip, "video/mp4"),
    sam2RefinementRequested: boundedOpaqueJson(
      source.sam2_refinement_request,
    ),
    reviewEventSetId: string(
      source.review_event_set_id,
      "검토 이벤트 집합 ID",
      /^res_[a-f0-9]{32}$/,
    ),
    reviewEventSetRevision: sha256(
      source.review_event_set_revision,
      "검토 이벤트 집합 리비전",
    ),
    reviewEventSetManifestSha256: sha256(
      source.review_event_set_manifest_sha256,
      "검토 이벤트 집합 해시",
    ),
    reviewEventId: string(
      binding.event_id,
      "검토 이벤트 ID",
      /^rve_[a-f0-9]{32}$/,
    ),
    reviewEventRevision: sha256(
      binding.event_revision,
      "검토 이벤트 리비전",
    ),
    questionQueueRevision: sha256(
      source.question_queue_revision,
      "질문 큐 리비전",
    ),
    questionCatalogRevision: sha256(
      source.question_catalog_revision,
      "질문 카탈로그 리비전",
    ),
    questionCatalogCollectionRevision: sha256(
      source.question_catalog_collection_revision,
      "질문 카탈로그 모음 리비전",
    ),
    idempotentReplay: source.idempotent_replay,
    reviewOnly: true,
  };
}

export async function parseUserDiscoveredReviewFrame(
  value: unknown,
  expected: {
    projectId: string;
    pipelineJobId: string;
    analysisJobId: string;
    requestedTimestampSeconds: number;
  },
): Promise<UserDiscoveredReviewFrame> {
  const envelope = record(value, "전체 주행 2fps 검토 프레임");
  exactKeys(
    envelope,
    ["metadata", "bytes"],
    "전체 주행 2fps 검토 프레임",
  );
  const source = record(envelope.metadata, "2fps 검토 프레임 정보");
  exactKeys(source, REVIEW_FRAME_KEYS, "2fps 검토 프레임 정보");
  if (
    source.schema_version !==
      "trafficvisson.user-discovered-review-frame.v1" ||
    source.media_type !== "image/jpeg" ||
    source.sampling_fps !== 2 ||
    source.path_exposed !== false
  ) {
    throw new Error("2fps 검토 프레임 정보 응답 형식이 올바르지 않습니다.");
  }
  const projectId = uuid(source.project_id, "프로젝트 ID");
  const pipelineJobId = uuid(
    source.pipeline_job_id,
    "파이프라인 작업 ID",
  );
  const analysisJobId = uuid(source.analysis_job_id, "분석 작업 ID");
  const requestedTimestampSeconds = finiteNumber(
    source.requested_timestamp_seconds,
    "요청 영상 시각",
    0,
    86_400,
  );
  const selectedFrameTimestampSeconds = finiteNumber(
    source.selected_frame_timestamp_seconds,
    "2fps 프레임 시각",
    0,
    86_400,
  );
  const sourceDurationSeconds = finiteNumber(
    source.source_duration_seconds,
    "원본 영상 길이",
    Number.EPSILON,
    86_400,
  );
  const indexedDurationSeconds = finiteNumber(
    source.indexed_duration_seconds,
    "2fps 인덱스 길이",
    Number.EPSILON,
    86_400,
  );
  if (
    projectId !== expected.projectId.toLowerCase() ||
    pipelineJobId !== expected.pipelineJobId.toLowerCase() ||
    analysisJobId !== expected.analysisJobId.toLowerCase() ||
    Math.abs(
      requestedTimestampSeconds - expected.requestedTimestampSeconds,
    ) > 0.000001 ||
    indexedDurationSeconds > sourceDurationSeconds + 0.000001 ||
    requestedTimestampSeconds > indexedDurationSeconds + 0.000001 ||
    selectedFrameTimestampSeconds > indexedDurationSeconds + 0.000001 ||
    Math.abs(
      selectedFrameTimestampSeconds - requestedTimestampSeconds,
    ) > 0.500001
  ) {
    throw new Error(
      "2fps 검토 프레임이 현재 독립 조사 영상과 일치하지 않습니다.",
    );
  }
  if (!(envelope.bytes instanceof Uint8Array)) {
    throw new Error("2fps 검토 프레임 바이트 응답 형식이 올바르지 않습니다.");
  }
  const bytes = envelope.bytes;
  const selectedFrameSizeBytes = boundedInteger(
    source.selected_frame_size_bytes,
    "2fps 프레임 크기",
    1,
    32 * 1024 * 1024,
  );
  if (
    bytes.byteLength !== selectedFrameSizeBytes ||
    bytes.byteLength < 4 ||
    bytes[0] !== 0xff ||
    bytes[1] !== 0xd8 ||
    bytes.at(-2) !== 0xff ||
    bytes.at(-1) !== 0xd9
  ) {
    throw new Error("2fps 검토 프레임 바이트 응답 형식이 올바르지 않습니다.");
  }
  const selectedFrameSha256 = sha256(
    source.selected_frame_sha256,
    "2fps 프레임 해시",
  );
  if ((await byteSha256(bytes)) !== selectedFrameSha256) {
    throw new Error("2fps 검토 프레임 해시가 변경됐습니다.");
  }
  return {
    projectId,
    pipelineJobId,
    analysisJobId,
    requestedTimestampSeconds,
    selectedFrameId: string(
      source.selected_frame_id,
      "2fps 프레임 ID",
      FRAME_ID_PATTERN,
    ),
    selectedFrameIndex: boundedInteger(
      source.selected_frame_index,
      "2fps 프레임 번호",
      0,
      172_800,
    ),
    selectedFrameTimestampSeconds,
    selectedFrameSha256,
    selectedFrameSizeBytes,
    sourceDurationSeconds,
    indexedDurationSeconds,
    samplingFps: 2,
    denseIndexSha256: sha256(
      source.dense_index_sha256,
      "2fps 인덱스 해시",
    ),
    promotedModelVersionId: uuid(
      source.promoted_model_version_id,
      "승격 모델 버전 ID",
    ),
    sourceAnalysisLineageSha256: sha256(
      source.source_analysis_lineage_sha256,
      "독립 조사 분석 계보 해시",
    ),
    reviewFrameSha256: sha256(
      source.review_frame_sha256,
      "2fps 검토 프레임 바인딩 해시",
    ),
    questionQueueRevision: sha256(
      source.question_queue_revision,
      "질문 큐 리비전",
    ),
    bytes,
  };
}

export function normalizedVideoPoint(
  input: VideoPointInput,
): UserDiscoveredPointSelection | null {
  const values = [
    input.clientX,
    input.clientY,
    input.left,
    input.top,
    input.width,
    input.height,
    input.videoWidth,
    input.videoHeight,
  ];
  if (
    values.some((value) => !Number.isFinite(value)) ||
    input.width <= 0 ||
    input.height <= 0 ||
    input.videoWidth <= 0 ||
    input.videoHeight <= 0
  ) {
    return null;
  }
  let displayWidth = input.width;
  let displayHeight = input.height;
  let offsetX = 0;
  let offsetY = 0;
  if (input.objectFit !== "fill") {
    const scale =
      input.objectFit === "cover"
        ? Math.max(
            input.width / input.videoWidth,
            input.height / input.videoHeight,
          )
        : Math.min(
            input.width / input.videoWidth,
            input.height / input.videoHeight,
          );
    displayWidth = input.videoWidth * scale;
    displayHeight = input.videoHeight * scale;
    offsetX = (input.width - displayWidth) / 2;
    offsetY = (input.height - displayHeight) / 2;
  }
  const x = (input.clientX - input.left - offsetX) / displayWidth;
  const y = (input.clientY - input.top - offsetY) / displayHeight;
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  const stable = (value: number) =>
    Math.max(0, Math.min(1, Math.round(value * 1_000_000) / 1_000_000));
  return { kind: "point", x: stable(x), y: stable(y) };
}

export function userDiscoveredCandidateErrorMessage(error: unknown) {
  const code =
    error instanceof Error ? error.message : String(error ?? "");
  if (/question_queue_revision_stale/i.test(code)) {
    return "질문 큐가 다른 화면에서 갱신됐습니다. 최신 검토 상태를 다시 불러온 뒤 선택해 주세요.";
  }
  if (
    /user_discovered_candidate_already_queued|duplicate/i.test(code)
  ) {
    return "같은 프레임과 위치의 누락 후보가 이미 질문 큐에 있습니다.";
  }
  if (/question_queue_already_answered/i.test(code)) {
    return "이미 결과 갱신을 시작한 질문 큐에는 누락 후보를 추가할 수 없습니다.";
  }
  if (
    /model_source_video_reuse_forbidden|independent_survey_scope_mismatch/i.test(
      code,
    )
  ) {
    return "이 영상은 승격 모델의 독립 조사 영상으로 검증되지 않아 누락 QA에 사용할 수 없습니다.";
  }
  if (/user_discovered_candidate_service_unavailable/i.test(code)) {
    return "누락 후보 근거 생성 기능을 사용할 수 없습니다. 모델팩과 로컬 브리지를 확인해 주세요.";
  }
  return error instanceof Error
    ? error.message
    : "모델이 놓친 장면을 질문 큐에 추가하지 못했습니다.";
}
