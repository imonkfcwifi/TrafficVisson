import type { AnalysisVisionIssueType } from "./runtimeState";

export type WeakCaseReference = {
  sourceId: string;
  caseId: string;
  rank: number;
  similarityScore: number;
  matchedTerms: string[];
  quarter: 3 | 4;
  documentName: string;
  documentSha256: string;
  pageHint: number;
  tableIndex: number | null;
  cellRow: number | null;
  cellColumn: number | null;
  pictureIndex: number;
  caption: string | null;
  route: string | null;
  location: string | null;
  evaluation: string | null;
  opinion: string | null;
  imageAvailable: boolean;
  imageMediaType:
    | "image/bmp"
    | "image/png"
    | "image/jpeg"
    | "image/gif"
    | "image/webp"
    | null;
};

export type WeakCaseSearchResult = {
  capabilityState: "weak_reference_only" | "unavailable";
  searchMode: "bounded_text_and_structure_metadata";
  semanticImageEmbeddingUsed: false;
  cases: WeakCaseReference[];
  readySourceCount: number;
  sourceCount: number;
  approvedReferenceCount: 0;
  annotationCount: 0;
  incidentCount: 0;
  trainingEligibleCount: 0;
};

const SHA_PATTERN = /^[a-f0-9]{64}$/;
const SOURCE_ID_PATTERN = /^sample-hwpx-[34]q-[a-f0-9]{16}$/;
const CASE_ID_PATTERN = /^hwpx-weak-case-[a-f0-9]{32}$/;
const KNOWN_SOURCES = Object.freeze({
  "sample-hwpx-3q-24ffb4bc62cd86a6": Object.freeze({
    documentName: "3분기 점검 내용 정리.hwpx",
    documentSha256:
      "24ffb4bc62cd86a6c72d3a3829b8f1ddf1c824d7bf42452e7c495877a227bcb9",
    quarter: 3 as const,
  }),
  "sample-hwpx-4q-bbdd61a6ef192a95": Object.freeze({
    documentName: "4분기 점검 내용 정리.hwpx",
    documentSha256:
      "bbdd61a6ef192a957a5e9480b0c2eedbfe956dd184abd5d76f59474254941539",
    quarter: 4 as const,
  }),
});
const SOURCE_STATUSES = new Set([
  "ready",
  "source_missing",
  "source_sha256_mismatch",
  "source_changed",
  "source_invalid",
  "source_unavailable",
  "source_path_rejected",
  "source_contract_mismatch",
]);
const IMAGE_MEDIA_TYPES = new Set([
  "image/bmp",
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);
const RESPONSE_KEYS = [
  "annotation_count",
  "approved_reference_count",
  "capability_state",
  "cases",
  "incident_count",
  "issue_type",
  "query_text",
  "requested_limit",
  "result_count",
  "review_required_before_promotion",
  "schema_version",
  "search_mode",
  "semantic_image_embedding_used",
  "source_states",
  "training_eligible_count",
];
const SOURCE_KEYS = [
  "annotation_count",
  "approved_reference_count",
  "authorization_purpose",
  "case_count",
  "document_name",
  "document_sha256",
  "incident_count",
  "index_manifest_sha256",
  "quarter",
  "resolved_case_count",
  "review_required_count",
  "source_id",
  "status",
  "training_eligible_count",
];
const CASE_KEYS = [
  "annotation_created",
  "approved_reference",
  "caption",
  "case_id",
  "cell_column",
  "cell_row",
  "document_name",
  "document_sha256",
  "evaluation",
  "image_available",
  "image_height",
  "image_media_type",
  "image_width",
  "incident_created",
  "location",
  "matched_terms",
  "opinion",
  "page_hint",
  "page_hint_source",
  "picture_index",
  "quarter",
  "rank",
  "reference_state",
  "review_required",
  "route",
  "similarity_score",
  "source_id",
  "table_index",
  "training_eligible",
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: string[]) {
  const actual = Object.keys(value).sort();
  return (
    actual.length === keys.length &&
    actual.every((key, index) => key === keys[index])
  );
}

function boundedString(
  value: unknown,
  maximum: number,
  { nullable = false }: { nullable?: boolean } = {},
): value is string | null {
  if (nullable && value === null) return true;
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= maximum &&
    !value.includes("\0")
  );
}

function boundedInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  { nullable = false }: { nullable?: boolean } = {},
): value is number | null {
  if (nullable && value === null) return true;
  return (
    Number.isSafeInteger(value) &&
    Number(value) >= minimum &&
    Number(value) <= maximum
  );
}

function knownSource(value: unknown) {
  if (typeof value !== "string" || !SOURCE_ID_PATTERN.test(value)) {
    return null;
  }
  return (
    KNOWN_SOURCES[value as keyof typeof KNOWN_SOURCES] ??
    null
  );
}

function parseSource(value: unknown) {
  if (!isRecord(value) || !hasExactKeys(value, SOURCE_KEYS)) {
    return null;
  }
  const source = knownSource(value.source_id);
  if (
    source === null ||
    value.document_name !== source.documentName ||
    value.document_sha256 !== source.documentSha256 ||
    value.authorization_purpose !==
      "sample_hwpx_read_only_weak_case_index" ||
    value.quarter !== source.quarter ||
    typeof value.status !== "string" ||
    !SOURCE_STATUSES.has(value.status) ||
    !boundedInteger(value.case_count, 0, 10_000) ||
    !boundedInteger(value.resolved_case_count, 0, 10_000) ||
    !boundedInteger(value.review_required_count, 0, 10_000) ||
    value.approved_reference_count !== 0 ||
    value.training_eligible_count !== 0 ||
    value.annotation_count !== 0 ||
    value.incident_count !== 0 ||
    !(
      value.index_manifest_sha256 === null ||
      (typeof value.index_manifest_sha256 === "string" &&
        SHA_PATTERN.test(value.index_manifest_sha256))
    )
  ) {
    return null;
  }
  const ready = value.status === "ready";
  if (
    (ready &&
      (value.index_manifest_sha256 === null ||
        value.case_count !== value.review_required_count ||
        Number(value.resolved_case_count) > Number(value.case_count))) ||
    (!ready &&
      (value.index_manifest_sha256 !== null ||
        value.case_count !== 0 ||
        value.resolved_case_count !== 0 ||
        value.review_required_count !== 0))
  ) {
    return null;
  }
  return value;
}

function parseCase(value: unknown): WeakCaseReference | null {
  if (!isRecord(value) || !hasExactKeys(value, CASE_KEYS)) {
    return null;
  }
  const source = knownSource(value.source_id);
  if (
    source === null ||
    typeof value.case_id !== "string" ||
    !CASE_ID_PATTERN.test(value.case_id) ||
    !boundedInteger(value.rank, 1, 5) ||
    !boundedInteger(value.similarity_score, 1, 10_000) ||
    !Array.isArray(value.matched_terms) ||
    value.matched_terms.length > 8 ||
    value.matched_terms.some(
      (item) => !boundedString(item, 240),
    ) ||
    value.reference_state !== "weak_unconfirmed" ||
    value.review_required !== true ||
    value.approved_reference !== false ||
    value.training_eligible !== false ||
    value.annotation_created !== false ||
    value.incident_created !== false ||
    value.quarter !== source.quarter ||
    value.document_name !== source.documentName ||
    value.document_sha256 !== source.documentSha256 ||
    !boundedInteger(value.page_hint, 1, 10_000) ||
    !boundedString(value.page_hint_source, 120) ||
    !boundedInteger(value.table_index, 0, 100_000, { nullable: true }) ||
    !boundedInteger(value.cell_row, 0, 100_000, { nullable: true }) ||
    !boundedInteger(value.cell_column, 0, 100_000, { nullable: true }) ||
    !boundedInteger(value.picture_index, 0, 100_000) ||
    !boundedString(value.caption, 320, { nullable: true }) ||
    !boundedString(value.route, 320, { nullable: true }) ||
    !boundedString(value.location, 320, { nullable: true }) ||
    !boundedString(value.evaluation, 320, { nullable: true }) ||
    !boundedString(value.opinion, 320, { nullable: true }) ||
    typeof value.image_available !== "boolean" ||
    !(
      value.image_media_type === null ||
      (typeof value.image_media_type === "string" &&
        IMAGE_MEDIA_TYPES.has(value.image_media_type))
    ) ||
    !boundedInteger(value.image_width, 1, 65_535, { nullable: true }) ||
    !boundedInteger(value.image_height, 1, 65_535, { nullable: true })
  ) {
    return null;
  }
  if (
    value.image_available &&
    (value.image_media_type === null ||
      value.image_width === null ||
      value.image_height === null)
  ) {
    return null;
  }
  return {
    sourceId: value.source_id as string,
    caseId: value.case_id,
    rank: Number(value.rank),
    similarityScore: Number(value.similarity_score),
    matchedTerms: value.matched_terms as string[],
    quarter: source.quarter,
    documentName: source.documentName,
    documentSha256: source.documentSha256,
    pageHint: Number(value.page_hint),
    tableIndex: value.table_index as number | null,
    cellRow: value.cell_row as number | null,
    cellColumn: value.cell_column as number | null,
    pictureIndex: Number(value.picture_index),
    caption: value.caption as string | null,
    route: value.route as string | null,
    location: value.location as string | null,
    evaluation: value.evaluation as string | null,
    opinion: value.opinion as string | null,
    imageAvailable: value.image_available,
    imageMediaType: value.image_media_type as WeakCaseReference["imageMediaType"],
  };
}

export function parseWeakCaseSearchResponse(
  value: unknown,
  expected: {
    issueType: AnalysisVisionIssueType | null;
    requestedLimit: 3 | 4 | 5;
  },
): WeakCaseSearchResult {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, RESPONSE_KEYS) ||
    value.schema_version !== "trafficvisson.weak-case-search.v1" ||
    value.search_mode !== "bounded_text_and_structure_metadata" ||
    value.semantic_image_embedding_used !== false ||
    (value.capability_state !== "weak_reference_only" &&
      value.capability_state !== "unavailable") ||
    value.issue_type !== expected.issueType ||
    !(
      value.query_text === null ||
      boundedString(value.query_text, 240)
    ) ||
    value.requested_limit !== expected.requestedLimit ||
    !boundedInteger(value.result_count, 0, expected.requestedLimit) ||
    !Array.isArray(value.cases) ||
    value.cases.length !== value.result_count ||
    !Array.isArray(value.source_states) ||
    value.source_states.length > 4 ||
    value.review_required_before_promotion !== true ||
    value.approved_reference_count !== 0 ||
    value.annotation_count !== 0 ||
    value.incident_count !== 0 ||
    value.training_eligible_count !== 0
  ) {
    throw new Error(
      "3·4분기 참고 사례 검색 응답을 검증하지 못했습니다.",
    );
  }
  const sources = value.source_states.map(parseSource);
  const cases = value.cases.map(parseCase);
  if (sources.some((source) => source === null) ||
      cases.some((item) => item === null)) {
    throw new Error(
      "3·4분기 참고 사례의 안전 상태를 검증하지 못했습니다.",
    );
  }
  const readySourceCount = sources.filter(
    (source) => source?.status === "ready",
  ).length;
  const sourceById = new Map(
    sources
      .filter((source) => source !== null)
      .map((source) => [source.source_id, source]),
  );
  if (
    (value.capability_state === "weak_reference_only" &&
      readySourceCount < 1) ||
    (value.capability_state === "unavailable" &&
      readySourceCount !== 0) ||
    cases.some((item, index) => {
      if (item?.rank !== index + 1) return true;
      const source = item ? sourceById.get(item.sourceId) : null;
      return (
        !source ||
        source.status !== "ready" ||
        source.document_name !== item.documentName ||
        source.document_sha256 !== item.documentSha256 ||
        source.quarter !== item.quarter
      );
    })
  ) {
    throw new Error(
      "3·4분기 참고 사례 검색 상태가 일치하지 않습니다.",
    );
  }
  return {
    capabilityState: value.capability_state,
    searchMode: value.search_mode,
    semanticImageEmbeddingUsed: false,
    cases: cases as WeakCaseReference[],
    readySourceCount,
    sourceCount: sources.length,
    approvedReferenceCount: 0,
    annotationCount: 0,
    incidentCount: 0,
    trainingEligibleCount: 0,
  };
}
