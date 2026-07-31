import type { AnalysisVisionIssueType } from "./runtimeState";
import { CANONICAL_ISSUE_TYPES } from "./issueTypes";

export type WeakCaseReference = {
  sourceId: string;
  caseId: string;
  rank: number;
  similarityScore: number;
  matchedTerms: string[];
  referenceState: "weak_unconfirmed" | "approved_reference";
  reviewRequired: boolean;
  approvedReference: boolean;
  trainingEligible: boolean;
  quarter: 3 | 4;
  documentName: string;
  documentSha256: string;
  structureTargetSummarySha256: string;
  indexManifestSha256: string;
  sectionPath: string;
  sectionIndex: number;
  pictureTargetId: string;
  pageHint: number;
  cellTargetId: string | null;
  tableIndex: number | null;
  cellRow: number | null;
  cellColumn: number | null;
  pictureIndex: number;
  sampleLayoutBindingSchema:
    | "trafficvisson.hwpx-sample-layout.v2"
    | null;
  sampleLayoutBindingRevision: 2 | null;
  semanticProjectionRevision:
    | "audited-photo-caption-evidence-group-v2"
    | null;
  sampleEvidenceGroupId: string | null;
  groupPhotoIndex: number | null;
  groupPhotoCount: number | null;
  photoSlotId: string | null;
  sampleLayoutBindingSha256: string | null;
  semanticAttestationSha256: string | null;
  duplicateImageOccurrenceCount: number;
  duplicateImageReviewRequired: boolean;
  duplicateImageRegistryComplete: boolean;
  duplicateImageCaseRefs: string[];
  caption: string | null;
  route: string | null;
  location: string | null;
  evaluation: string | null;
  opinion: string | null;
  imageAvailable: boolean;
  imageSha256: string | null;
  imageMediaType:
    | "image/bmp"
    | "image/png"
    | "image/jpeg"
    | "image/gif"
    | "image/webp"
    | null;
  approvalId: string | null;
  approvalRevision: number | null;
  approvalRecordSha256: string | null;
  approvedIssueType: AnalysisVisionIssueType | null;
  approvedAction: string | null;
};

export type WeakCaseSearchResult = {
  capabilityState:
    | "approved_reference_available"
    | "weak_reference_only"
    | "unavailable";
  searchMode: "bounded_text_and_structure_metadata";
  semanticImageEmbeddingUsed: false;
  cases: WeakCaseReference[];
  readySourceCount: number;
  sourceCount: number;
  approvedReferenceCount: number;
  annotationCount: 0;
  incidentCount: 0;
  trainingEligibleCount: number;
};

export type WeakCaseApprovalRequest = {
  projectId: string;
  sourceId: string;
  caseId: string;
  expectedDocumentSha256: string;
  expectedIndexManifestSha256: string;
  expectedImageSha256: string;
  issueType: Exclude<AnalysisVisionIssueType, "other">;
  maintenanceAction: string;
  caption?: string;
  route?: string;
  location?: string;
  evaluation?: string;
  opinion?: string;
  expectedRevision?: number;
  duplicateImageReviewConfirmed?: boolean;
  userConfirmed: true;
};

export type WeakCaseStandardizationRequest = {
  projectId: string;
  sourceId: string;
  caseId: string;
  provider: "codex" | "claude";
  expectedDocumentSha256: string;
  expectedIndexManifestSha256: string;
  expectedImageSha256: string;
  expectedRevision?: number;
  metadataTransmissionConfirmed: true;
};

export type WeakCaseStandardizationPhotoProvenance = {
  sourceId: string;
  caseId: string;
  photoSlotId: string;
  pictureTargetId: string;
  groupPhotoIndex: number;
  groupPhotoCount: number;
  imageSha256: string;
  sampleLayoutBindingSha256: string;
  semanticAttestationSha256: string;
  duplicateImageOccurrenceCount: number;
  duplicateImageReviewRequired: boolean;
  duplicateImageCaseRefs: string[];
};

export type WeakCaseStandardizationProposal = {
  proposalId: string;
  projectId: string;
  revision: number;
  provider: "codex" | "claude";
  providerModel: string | null;
  providerUsage: Record<string, number> | null;
  sourceSnapshot: {
    quarter: 3 | 4;
    caption: string | null;
    opinion: string | null;
    route: string | null;
    documentSha256: string;
    indexManifestSha256: string;
  };
  sampleEvidenceGroupId: string;
  caseRefs: string[];
  photoProvenance: WeakCaseStandardizationPhotoProvenance[];
  suggestion: {
    issueType: Exclude<AnalysisVisionIssueType, "other">;
    maintenanceAction: string;
  };
  sourceSnapshotSha256: string;
  photoProvenanceSha256: string;
  proposalSha256: string;
  inputSha256: string;
  recordSha256: string;
  createdAt: string;
  updatedAt: string;
  idempotentReplay: boolean;
  annotationCount: 0;
  incidentCount: 0;
  reportFactCount: 0;
};

export type WeakCaseStandardizationProposalList = {
  proposals: WeakCaseStandardizationProposal[];
  proposalCount: number;
  evidenceEventCount: number;
  approvedReferenceCount: 0;
  trainingEligibleCount: 0;
  annotationCount: 0;
  incidentCount: 0;
  reportFactCount: 0;
};

export type WeakCaseStandardizationProviderStatus = {
  provider: "codex" | "claude";
  displayName: string;
  availability:
    | "ready"
    | "not_installed"
    | "not_logged_in"
    | "expired"
    | "network_unavailable"
    | "unavailable";
  diagnosticState:
    | "not_installed"
    | "installed"
    | "not_connected"
    | "ready"
    | "expired"
    | "network_unavailable"
    | "error"
    | "coming_soon";
  installationState: "installed" | "not_installed" | "disabled";
  authenticationState:
    | "ready"
    | "signed_out"
    | "expired"
    | "unknown"
    | "not_applicable";
  networkState: "not_checked" | "offline" | "unknown" | "not_applicable";
  version: string | null;
  message: string;
  structuredJudgment: boolean;
};

const SHA_PATTERN = /^[a-f0-9]{64}$/;
const SOURCE_ID_PATTERN = /^sample-hwpx-[34]q-[a-f0-9]{16}$/;
const CASE_ID_PATTERN = /^hwpx-weak-case-[a-f0-9]{32}$/;
const CELL_TARGET_ID_PATTERN = /^hwpx-cell-[a-f0-9]{24}$/;
const PICTURE_TARGET_ID_PATTERN = /^hwpx-picture-[a-f0-9]{24}$/;
const APPROVAL_ID_PATTERN = /^approved-reference-[a-f0-9]{24}$/;
const STANDARDIZATION_PROPOSAL_ID_PATTERN =
  /^weak-case-standardization-[a-f0-9]{24}$/;
const PHOTO_SLOT_ID_PATTERN = /^sample-photo-slot-[a-f0-9]{24}$/;
const EVIDENCE_GROUP_ID_PATTERN =
  /^sample-evidence-group-[a-f0-9]{24}$/;
const DUPLICATE_CASE_REF_PATTERN =
  /^sample-hwpx-[34]q-[a-f0-9]{16}\/hwpx-weak-case-[a-f0-9]{32}$/;
const SECTION_PATH_PATTERN = /^Contents\/section[0-9]{1,5}\.xml$/;
const ISSUE_TYPES = new Set<string>([
  ...CANONICAL_ISSUE_TYPES.map(({ apiValue }) => apiValue),
  "other",
]);
const STANDARDIZATION_ISSUE_TYPES = new Set<string>(
  CANONICAL_ISSUE_TYPES.map(({ apiValue }) => apiValue),
);

export function buildWeakCaseApprovalRequest({
  projectId,
  reference,
  issueType,
  maintenanceAction,
  duplicateImageReviewConfirmed = false,
}: {
  projectId: string;
  reference: WeakCaseReference;
  issueType: Exclude<AnalysisVisionIssueType, "other">;
  maintenanceAction: string;
  duplicateImageReviewConfirmed?: boolean;
}): WeakCaseApprovalRequest {
  if (
    reference.approvedReference ||
    !reference.reviewRequired ||
    reference.trainingEligible
  ) {
    throw new Error("이미 승인된 3·4분기 사례입니다.");
  }
  if (
    reference.duplicateImageReviewRequired &&
    !duplicateImageReviewConfirmed
  ) {
    throw new Error(
      "다른 노선·쪽에 재사용된 동일 사진임을 확인해야 승인할 수 있습니다.",
    );
  }
  if (
    !reference.imageAvailable ||
    !reference.imageSha256 ||
    !SHA_PATTERN.test(reference.imageSha256)
  ) {
    throw new Error(
      "사진 SHA-256을 검증할 수 없어 이 사례를 승인할 수 없습니다.",
    );
  }
  if (
    !CANONICAL_ISSUE_TYPES.some(
      ({ apiValue }) => apiValue === issueType,
    )
  ) {
    throw new Error("승인할 표준 도로 문제 유형이 필요합니다.");
  }
  const action = maintenanceAction.trim();
  if (!action || action.length > 500 || action.includes("\0")) {
    throw new Error("승인할 유지보수 조치를 확인해 주세요.");
  }
  const optional = (value: string | null) =>
    value?.trim() ? value.trim() : undefined;
  return {
    projectId,
    sourceId: reference.sourceId,
    caseId: reference.caseId,
    expectedDocumentSha256: reference.documentSha256,
    expectedIndexManifestSha256: reference.indexManifestSha256,
    expectedImageSha256: reference.imageSha256,
    issueType,
    maintenanceAction: action,
    ...(optional(reference.caption)
      ? { caption: optional(reference.caption) }
      : {}),
    ...(optional(reference.route) ? { route: optional(reference.route) } : {}),
    ...(optional(reference.location)
      ? { location: optional(reference.location) }
      : {}),
    ...(optional(reference.evaluation)
      ? { evaluation: optional(reference.evaluation) }
      : {}),
    ...(optional(reference.opinion)
      ? { opinion: optional(reference.opinion) }
      : {}),
    duplicateImageReviewConfirmed,
    userConfirmed: true,
  };
}

export function buildWeakCaseStandardizationRequest({
  projectId,
  reference,
  provider,
  metadataTransmissionConfirmed,
  expectedRevision,
}: {
  projectId: string;
  reference: WeakCaseReference;
  provider: "codex" | "claude";
  metadataTransmissionConfirmed: true;
  expectedRevision?: number;
}): WeakCaseStandardizationRequest {
  if (
    reference.referenceState !== "weak_unconfirmed" ||
    reference.approvedReference ||
    !reference.reviewRequired ||
    reference.trainingEligible
  ) {
    throw new Error(
      "승인 전 3·4분기 약한 사례만 표준화 제안을 받을 수 있습니다.",
    );
  }
  if (
    !reference.imageSha256 ||
    !SHA_PATTERN.test(reference.imageSha256) ||
    !SHA_PATTERN.test(reference.documentSha256) ||
    !SHA_PATTERN.test(reference.indexManifestSha256) ||
    !reference.sampleEvidenceGroupId ||
    !EVIDENCE_GROUP_ID_PATTERN.test(reference.sampleEvidenceGroupId)
  ) {
    throw new Error(
      "표준화 제안에 필요한 문서·사진·사건 provenance가 없습니다.",
    );
  }
  if (metadataTransmissionConfirmed !== true) {
    throw new Error(
      "원문과 구조 메타데이터의 Provider 전송 범위를 확인해 주세요.",
    );
  }
  if (
    expectedRevision !== undefined &&
    (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1)
  ) {
    throw new Error("표준화 제안 revision이 올바르지 않습니다.");
  }
  return {
    projectId,
    sourceId: reference.sourceId,
    caseId: reference.caseId,
    provider,
    expectedDocumentSha256: reference.documentSha256,
    expectedIndexManifestSha256: reference.indexManifestSha256,
    expectedImageSha256: reference.imageSha256,
    ...(expectedRevision === undefined ? {} : { expectedRevision }),
    metadataTransmissionConfirmed: true,
  };
}

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
  "approval_id",
  "approval_record_sha256",
  "approval_revision",
  "approved_action",
  "approved_issue_type",
  "approved_reference",
  "caption",
  "case_id",
  "cell_column",
  "cell_row",
  "cell_target_id",
  "document_name",
  "document_sha256",
  "duplicate_image_case_refs",
  "duplicate_image_occurrence_count",
  "duplicate_image_registry_complete",
  "duplicate_image_review_required",
  "evaluation",
  "group_photo_count",
  "group_photo_index",
  "image_available",
  "image_height",
  "image_media_type",
  "image_sha256",
  "image_width",
  "incident_created",
  "index_manifest_sha256",
  "location",
  "matched_terms",
  "opinion",
  "page_hint",
  "page_hint_source",
  "photo_slot_id",
  "picture_index",
  "picture_target_id",
  "quarter",
  "rank",
  "reference_state",
  "review_required",
  "route",
  "sample_evidence_group_id",
  "sample_layout_binding_revision",
  "sample_layout_binding_schema",
  "sample_layout_binding_sha256",
  "section_index",
  "section_path",
  "semantic_attestation_sha256",
  "semantic_projection_revision",
  "similarity_score",
  "source_id",
  "structure_target_summary_sha256",
  "table_index",
  "training_eligible",
];
const STANDARDIZATION_PROPOSAL_KEYS = [
  "annotation_count",
  "approved_reference",
  "case_refs",
  "created_at",
  "evidence_transport_policy",
  "idempotent_replay",
  "incident_count",
  "input_sha256",
  "photo_provenance",
  "photo_provenance_sha256",
  "project_id",
  "proposal",
  "proposal_id",
  "proposal_sha256",
  "provider",
  "provider_model",
  "provider_usage",
  "record_sha256",
  "reference_state",
  "report_fact_count",
  "review_required",
  "revision",
  "sample_evidence_group_id",
  "schema_version",
  "source_snapshot",
  "source_snapshot_sha256",
  "status",
  "training_eligible",
  "updated_at",
  "user_confirmed",
];
const STANDARDIZATION_SOURCE_SNAPSHOT_KEYS = [
  "caption",
  "document_sha256",
  "index_manifest_sha256",
  "opinion",
  "quarter",
  "route",
];
const STANDARDIZATION_PHOTO_KEYS = [
  "case_id",
  "duplicate_image_case_refs",
  "duplicate_image_occurrence_count",
  "duplicate_image_review_required",
  "group_photo_count",
  "group_photo_index",
  "image_sha256",
  "photo_slot_id",
  "picture_target_id",
  "sample_layout_binding_sha256",
  "semantic_attestation_sha256",
  "source_id",
];
const STANDARDIZATION_TRANSPORT_KEYS = [
  "document_bytes_sent",
  "image_attachments_allowed",
  "image_pixels_sent",
  "local_paths_sent",
  "mode",
  "raw_video_sent",
];
const STANDARDIZATION_SUGGESTION_KEYS = [
  "issue_type",
  "maintenance_action",
];
const STANDARDIZATION_PROPOSAL_LIST_KEYS = [
  "annotation_count",
  "approved_reference_count",
  "evidence_event_count",
  "incident_count",
  "project_id",
  "proposal_count",
  "records",
  "report_fact_count",
  "schema_version",
  "training_eligible_count",
];
const PROVIDER_LIST_KEYS = ["providers"];
const PROVIDER_ROW_KEYS = [
  "authentication_owner",
  "authentication_state",
  "capabilities",
  "diagnostic_state",
  "display_name",
  "installation_state",
  "message",
  "network_state",
  "provider",
  "state",
  "version",
];
const PROVIDER_CAPABILITY_KEYS = [
  "bulk_frame_detection",
  "document_edit_plan",
  "report_draft",
  "structured_judgment",
  "usage_information",
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

function validTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= 64 &&
    Number.isFinite(Date.parse(value))
  );
}

function parseStandardizationPhoto(
  value: unknown,
): WeakCaseStandardizationPhotoProvenance | null {
  if (!isRecord(value) || !hasExactKeys(value, STANDARDIZATION_PHOTO_KEYS)) {
    return null;
  }
  const duplicateRefs = value.duplicate_image_case_refs;
  if (
    typeof value.source_id !== "string" ||
    !SOURCE_ID_PATTERN.test(value.source_id) ||
    typeof value.case_id !== "string" ||
    !CASE_ID_PATTERN.test(value.case_id) ||
    typeof value.photo_slot_id !== "string" ||
    !PHOTO_SLOT_ID_PATTERN.test(value.photo_slot_id) ||
    typeof value.picture_target_id !== "string" ||
    !PICTURE_TARGET_ID_PATTERN.test(value.picture_target_id) ||
    !boundedInteger(value.group_photo_index, 1, 10_000) ||
    !boundedInteger(value.group_photo_count, 1, 10_000) ||
    Number(value.group_photo_index) > Number(value.group_photo_count) ||
    typeof value.image_sha256 !== "string" ||
    !SHA_PATTERN.test(value.image_sha256) ||
    typeof value.sample_layout_binding_sha256 !== "string" ||
    !SHA_PATTERN.test(value.sample_layout_binding_sha256) ||
    typeof value.semantic_attestation_sha256 !== "string" ||
    !SHA_PATTERN.test(value.semantic_attestation_sha256) ||
    !boundedInteger(value.duplicate_image_occurrence_count, 1, 10_000) ||
    typeof value.duplicate_image_review_required !== "boolean" ||
    !Array.isArray(duplicateRefs) ||
    duplicateRefs.length < 1 ||
    duplicateRefs.length > 10_000 ||
    duplicateRefs.some(
      (caseRef) =>
        typeof caseRef !== "string" ||
        !DUPLICATE_CASE_REF_PATTERN.test(caseRef),
    )
  ) {
    return null;
  }
  const normalizedDuplicateRefs = duplicateRefs as string[];
  if (
    normalizedDuplicateRefs.length !==
      Number(value.duplicate_image_occurrence_count) ||
    normalizedDuplicateRefs.join("\0") !==
      [...new Set(normalizedDuplicateRefs)].sort().join("\0") ||
    !normalizedDuplicateRefs.includes(`${value.source_id}/${value.case_id}`) ||
    value.duplicate_image_review_required !==
      (normalizedDuplicateRefs.length > 1)
  ) {
    return null;
  }
  return {
    sourceId: value.source_id,
    caseId: value.case_id,
    photoSlotId: value.photo_slot_id,
    pictureTargetId: value.picture_target_id,
    groupPhotoIndex: Number(value.group_photo_index),
    groupPhotoCount: Number(value.group_photo_count),
    imageSha256: value.image_sha256,
    sampleLayoutBindingSha256: value.sample_layout_binding_sha256,
    semanticAttestationSha256: value.semantic_attestation_sha256,
    duplicateImageOccurrenceCount: Number(
      value.duplicate_image_occurrence_count,
    ),
    duplicateImageReviewRequired:
      value.duplicate_image_review_required,
    duplicateImageCaseRefs: normalizedDuplicateRefs,
  };
}

function parseProviderUsage(value: unknown): Record<string, number> | null {
  if (value === null) return null;
  if (!isRecord(value) || Object.keys(value).length > 16) return null;
  const entries = Object.entries(value);
  if (
    entries.some(
      ([key, count]) =>
        !/^[a-z][a-z0-9_]{0,63}$/.test(key) ||
        !boundedInteger(count, 0, 1_000_000_000),
    )
  ) {
    return null;
  }
  return Object.fromEntries(
    entries.map(([key, count]) => [key, Number(count)]),
  );
}

function parseStandardizationProposal(
  value: unknown,
  expectedProjectId: string,
): WeakCaseStandardizationProposal | null {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, STANDARDIZATION_PROPOSAL_KEYS) ||
    value.schema_version !==
      "trafficvisson.weak-case-standardization-proposal.v1" ||
    typeof value.proposal_id !== "string" ||
    !STANDARDIZATION_PROPOSAL_ID_PATTERN.test(value.proposal_id) ||
    value.project_id !== expectedProjectId ||
    !boundedInteger(value.revision, 1, 1_000_000_000) ||
    value.status !== "proposed" ||
    value.reference_state !== "weak_unconfirmed" ||
    value.review_required !== true ||
    value.approved_reference !== false ||
    value.training_eligible !== false ||
    value.annotation_count !== 0 ||
    value.incident_count !== 0 ||
    value.report_fact_count !== 0 ||
    value.user_confirmed !== false ||
    (value.provider !== "codex" && value.provider !== "claude") ||
    !(
      value.provider_model === null ||
      (typeof value.provider_model === "string" &&
        /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/.test(value.provider_model))
    ) ||
    !isRecord(value.evidence_transport_policy) ||
    !hasExactKeys(
      value.evidence_transport_policy,
      STANDARDIZATION_TRANSPORT_KEYS,
    ) ||
    value.evidence_transport_policy.mode !==
      "text_and_structure_metadata_only" ||
    value.evidence_transport_policy.image_pixels_sent !== false ||
    value.evidence_transport_policy.image_attachments_allowed !== false ||
    value.evidence_transport_policy.local_paths_sent !== false ||
    value.evidence_transport_policy.raw_video_sent !== false ||
    value.evidence_transport_policy.document_bytes_sent !== false ||
    !isRecord(value.source_snapshot) ||
    !hasExactKeys(
      value.source_snapshot,
      STANDARDIZATION_SOURCE_SNAPSHOT_KEYS,
    ) ||
    (value.source_snapshot.quarter !== 3 &&
      value.source_snapshot.quarter !== 4) ||
    !boundedString(value.source_snapshot.caption, 320, { nullable: true }) ||
    !boundedString(value.source_snapshot.opinion, 320, { nullable: true }) ||
    !boundedString(value.source_snapshot.route, 320, { nullable: true }) ||
    typeof value.source_snapshot.document_sha256 !== "string" ||
    !SHA_PATTERN.test(value.source_snapshot.document_sha256) ||
    typeof value.source_snapshot.index_manifest_sha256 !== "string" ||
    !SHA_PATTERN.test(value.source_snapshot.index_manifest_sha256) ||
    typeof value.source_snapshot_sha256 !== "string" ||
    !SHA_PATTERN.test(value.source_snapshot_sha256) ||
    typeof value.sample_evidence_group_id !== "string" ||
    !EVIDENCE_GROUP_ID_PATTERN.test(value.sample_evidence_group_id) ||
    !Array.isArray(value.case_refs) ||
    value.case_refs.length < 1 ||
    value.case_refs.length > 10_000 ||
    value.case_refs.some(
      (caseRef) =>
        typeof caseRef !== "string" ||
        !DUPLICATE_CASE_REF_PATTERN.test(caseRef),
    ) ||
    !Array.isArray(value.photo_provenance) ||
    value.photo_provenance.length !== value.case_refs.length ||
    typeof value.photo_provenance_sha256 !== "string" ||
    !SHA_PATTERN.test(value.photo_provenance_sha256) ||
    !isRecord(value.proposal) ||
    !hasExactKeys(value.proposal, STANDARDIZATION_SUGGESTION_KEYS) ||
    typeof value.proposal.issue_type !== "string" ||
    !STANDARDIZATION_ISSUE_TYPES.has(value.proposal.issue_type) ||
    !boundedString(value.proposal.maintenance_action, 500) ||
    typeof value.proposal_sha256 !== "string" ||
    !SHA_PATTERN.test(value.proposal_sha256) ||
    typeof value.input_sha256 !== "string" ||
    !SHA_PATTERN.test(value.input_sha256) ||
    !validTimestamp(value.created_at) ||
    !validTimestamp(value.updated_at) ||
    Date.parse(value.created_at) > Date.parse(value.updated_at) ||
    typeof value.record_sha256 !== "string" ||
    !SHA_PATTERN.test(value.record_sha256) ||
    typeof value.idempotent_replay !== "boolean"
  ) {
    return null;
  }
  const providerUsage = parseProviderUsage(value.provider_usage);
  if (value.provider_usage !== null && providerUsage === null) return null;
  const photoProvenance = value.photo_provenance.map(
    parseStandardizationPhoto,
  );
  if (photoProvenance.some((photo) => photo === null)) return null;
  const photos =
    photoProvenance as WeakCaseStandardizationPhotoProvenance[];
  const caseRefs = value.case_refs as string[];
  if (
    caseRefs.join("\0") !==
      photos
        .map(({ sourceId, caseId }) => `${sourceId}/${caseId}`)
        .join("\0") ||
    caseRefs.length !== new Set(caseRefs).size ||
    photos.some(
      (photo, index) =>
        photo.groupPhotoIndex !== index + 1 ||
        photo.groupPhotoCount !== photos.length,
    )
  ) {
    return null;
  }
  return {
    proposalId: value.proposal_id,
    projectId: expectedProjectId,
    revision: Number(value.revision),
    provider: value.provider,
    providerModel: value.provider_model as string | null,
    providerUsage,
    sourceSnapshot: {
      quarter: value.source_snapshot.quarter,
      caption: value.source_snapshot.caption as string | null,
      opinion: value.source_snapshot.opinion as string | null,
      route: value.source_snapshot.route as string | null,
      documentSha256: value.source_snapshot.document_sha256,
      indexManifestSha256:
        value.source_snapshot.index_manifest_sha256,
    },
    sampleEvidenceGroupId: value.sample_evidence_group_id,
    caseRefs,
    photoProvenance: photos,
    suggestion: {
      issueType: value.proposal
        .issue_type as Exclude<AnalysisVisionIssueType, "other">,
      maintenanceAction: value.proposal.maintenance_action as string,
    },
    sourceSnapshotSha256: value.source_snapshot_sha256,
    photoProvenanceSha256: value.photo_provenance_sha256,
    proposalSha256: value.proposal_sha256,
    inputSha256: value.input_sha256,
    recordSha256: value.record_sha256,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    idempotentReplay: value.idempotent_replay,
    annotationCount: 0,
    incidentCount: 0,
    reportFactCount: 0,
  };
}

function proposalMatchesReference(
  proposal: WeakCaseStandardizationProposal,
  reference: WeakCaseReference,
) {
  const expectedRef = `${reference.sourceId}/${reference.caseId}`;
  const photo = proposal.photoProvenance.find(
    ({ sourceId, caseId }) =>
      sourceId === reference.sourceId && caseId === reference.caseId,
  );
  return Boolean(
    reference.referenceState === "weak_unconfirmed" &&
      !reference.approvedReference &&
      reference.reviewRequired &&
      !reference.trainingEligible &&
      reference.sampleEvidenceGroupId &&
      reference.photoSlotId &&
      reference.sampleLayoutBindingSha256 &&
      reference.semanticAttestationSha256 &&
      reference.groupPhotoIndex &&
      reference.groupPhotoCount &&
      reference.imageSha256 &&
      proposal.sampleEvidenceGroupId === reference.sampleEvidenceGroupId &&
      proposal.caseRefs.includes(expectedRef) &&
      proposal.sourceSnapshot.quarter === reference.quarter &&
      proposal.sourceSnapshot.caption === reference.caption &&
      proposal.sourceSnapshot.opinion === reference.opinion &&
      proposal.sourceSnapshot.route === reference.route &&
      proposal.sourceSnapshot.documentSha256 === reference.documentSha256 &&
      proposal.sourceSnapshot.indexManifestSha256 ===
        reference.indexManifestSha256 &&
      photo &&
      photo.photoSlotId === reference.photoSlotId &&
      photo.pictureTargetId === reference.pictureTargetId &&
      photo.groupPhotoIndex === reference.groupPhotoIndex &&
      photo.groupPhotoCount === reference.groupPhotoCount &&
      photo.imageSha256 === reference.imageSha256 &&
      photo.sampleLayoutBindingSha256 ===
        reference.sampleLayoutBindingSha256 &&
      photo.semanticAttestationSha256 ===
        reference.semanticAttestationSha256 &&
      photo.duplicateImageOccurrenceCount ===
        reference.duplicateImageOccurrenceCount &&
      photo.duplicateImageReviewRequired ===
        reference.duplicateImageReviewRequired &&
      photo.duplicateImageCaseRefs.join("\0") ===
        reference.duplicateImageCaseRefs.join("\0"),
  );
}

export function parseWeakCaseStandardizationProposalResponse(
  value: unknown,
  expected: {
    projectId: string;
    reference: WeakCaseReference;
  },
): WeakCaseStandardizationProposal {
  const proposal = parseStandardizationProposal(value, expected.projectId);
  if (!proposal || !proposalMatchesReference(proposal, expected.reference)) {
    throw new Error(
      "3·4분기 표준화 제안의 문서·사진 provenance를 검증하지 못했습니다.",
    );
  }
  return proposal;
}

export function parseWeakCaseStandardizationProposalList(
  value: unknown,
  expected: { projectId: string },
): WeakCaseStandardizationProposalList {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, STANDARDIZATION_PROPOSAL_LIST_KEYS) ||
    value.schema_version !==
      "trafficvisson.weak-case-standardization-proposal-list.v1" ||
    value.project_id !== expected.projectId ||
    !boundedInteger(value.proposal_count, 0, 10_000) ||
    !boundedInteger(value.evidence_event_count, 0, 10_000) ||
    value.approved_reference_count !== 0 ||
    value.training_eligible_count !== 0 ||
    value.annotation_count !== 0 ||
    value.incident_count !== 0 ||
    value.report_fact_count !== 0 ||
    !Array.isArray(value.records) ||
    value.records.length !== value.proposal_count
  ) {
    throw new Error("3·4분기 표준화 제안 목록을 검증하지 못했습니다.");
  }
  const proposals = value.records.map((record) =>
    parseStandardizationProposal(record, expected.projectId),
  );
  if (
    proposals.some((proposal) => proposal === null) ||
    new Set(proposals.map((proposal) => proposal?.proposalId)).size !==
      proposals.length ||
    new Set(
      proposals.map((proposal) => proposal?.sampleEvidenceGroupId),
    ).size !== value.evidence_event_count
  ) {
    throw new Error(
      "3·4분기 표준화 제안 목록의 provenance가 일치하지 않습니다.",
    );
  }
  return {
    proposals: proposals as WeakCaseStandardizationProposal[],
    proposalCount: Number(value.proposal_count),
    evidenceEventCount: Number(value.evidence_event_count),
    approvedReferenceCount: 0,
    trainingEligibleCount: 0,
    annotationCount: 0,
    incidentCount: 0,
    reportFactCount: 0,
  };
}

export function findWeakCaseStandardizationProposal(
  proposals: readonly WeakCaseStandardizationProposal[],
  reference: WeakCaseReference,
) {
  return (
    proposals.find((proposal) =>
      proposalMatchesReference(proposal, reference),
    ) ?? null
  );
}

export function parseWeakCaseStandardizationProviderList(
  value: unknown,
): WeakCaseStandardizationProviderStatus[] {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, PROVIDER_LIST_KEYS) ||
    !Array.isArray(value.providers) ||
    value.providers.length < 2 ||
    value.providers.length > 3
  ) {
    throw new Error("표준화 Provider 상태 응답을 검증하지 못했습니다.");
  }
  const providers: WeakCaseStandardizationProviderStatus[] = [];
  const seen = new Set<string>();
  for (const raw of value.providers) {
    if (
      !isRecord(raw) ||
      !hasExactKeys(raw, PROVIDER_ROW_KEYS) ||
      !isRecord(raw.capabilities) ||
      !hasExactKeys(raw.capabilities, PROVIDER_CAPABILITY_KEYS) ||
      (raw.provider !== "codex" &&
        raw.provider !== "claude" &&
        raw.provider !== "local") ||
      seen.has(raw.provider) ||
      !boundedString(raw.display_name, 120) ||
      (raw.state !== "not_connected" &&
        raw.state !== "ready" &&
        raw.state !== "unavailable" &&
        raw.state !== "coming_soon") ||
      (raw.authentication_owner !== "official_cli" &&
        raw.authentication_owner !== "not_applicable") ||
      typeof raw.capabilities.structured_judgment !== "boolean" ||
      raw.capabilities.bulk_frame_detection !== false ||
      typeof raw.capabilities.report_draft !== "boolean" ||
      typeof raw.capabilities.document_edit_plan !== "boolean" ||
      typeof raw.capabilities.usage_information !== "boolean" ||
      !boundedString(raw.message, 1_000) ||
      ![
        "not_installed",
        "installed",
        "not_connected",
        "ready",
        "expired",
        "network_unavailable",
        "error",
        "coming_soon",
      ].includes(String(raw.diagnostic_state)) ||
      !["installed", "not_installed", "disabled"].includes(
        String(raw.installation_state),
      ) ||
      !["ready", "signed_out", "expired", "unknown", "not_applicable"].includes(
        String(raw.authentication_state),
      ) ||
      !["not_checked", "offline", "unknown", "not_applicable"].includes(
        String(raw.network_state),
      ) ||
      !(
        raw.version === null ||
        (typeof raw.version === "string" &&
          /^[vV]?[0-9][0-9A-Za-z.+-]{0,63}$/.test(raw.version))
      ) ||
      (raw.provider === "local"
        ? raw.authentication_owner !== "not_applicable"
        : raw.authentication_owner !== "official_cli")
    ) {
      throw new Error(
        "표준화 Provider 상태 응답을 검증하지 못했습니다.",
      );
    }
    seen.add(raw.provider);
    if (raw.provider === "local") continue;
    const availability =
      raw.installation_state === "not_installed" ||
      raw.diagnostic_state === "not_installed"
        ? "not_installed"
        : raw.authentication_state === "expired" ||
            raw.diagnostic_state === "expired"
          ? "expired"
          : raw.authentication_state === "signed_out" ||
              raw.diagnostic_state === "not_connected"
            ? "not_logged_in"
            : raw.network_state === "offline" ||
                raw.diagnostic_state === "network_unavailable"
              ? "network_unavailable"
              : raw.state === "ready" &&
                  raw.installation_state === "installed" &&
                  raw.authentication_state === "ready" &&
                  raw.capabilities.structured_judgment === true
                ? "ready"
                : "unavailable";
    providers.push({
      provider: raw.provider,
      displayName: raw.display_name as string,
      availability,
      diagnosticState:
        raw.diagnostic_state as WeakCaseStandardizationProviderStatus["diagnosticState"],
      installationState:
        raw.installation_state as WeakCaseStandardizationProviderStatus["installationState"],
      authenticationState:
        raw.authentication_state as WeakCaseStandardizationProviderStatus["authenticationState"],
      networkState:
        raw.network_state as WeakCaseStandardizationProviderStatus["networkState"],
      version: raw.version as string | null,
      message: raw.message as string,
      structuredJudgment: raw.capabilities.structured_judgment,
    });
  }
  if (
    providers.length !== 2 ||
    !providers.some(({ provider }) => provider === "codex") ||
    !providers.some(({ provider }) => provider === "claude")
  ) {
    throw new Error("공식 표준화 Provider 상태가 불완전합니다.");
  }
  return providers;
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
    !boundedInteger(value.approved_reference_count, 0, 10_000) ||
    !boundedInteger(value.training_eligible_count, 0, 10_000) ||
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
        value.case_count !==
          Number(value.review_required_count) +
            Number(value.approved_reference_count) ||
        value.training_eligible_count !== value.approved_reference_count ||
        Number(value.resolved_case_count) > Number(value.case_count))) ||
    (!ready &&
      (value.index_manifest_sha256 !== null ||
        value.case_count !== 0 ||
        value.resolved_case_count !== 0 ||
        value.review_required_count !== 0 ||
        value.approved_reference_count !== 0 ||
        value.training_eligible_count !== 0))
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
    (value.reference_state !== "weak_unconfirmed" &&
      value.reference_state !== "approved_reference") ||
    typeof value.review_required !== "boolean" ||
    typeof value.approved_reference !== "boolean" ||
    typeof value.training_eligible !== "boolean" ||
    value.annotation_created !== false ||
    value.incident_created !== false ||
    value.quarter !== source.quarter ||
    value.document_name !== source.documentName ||
    value.document_sha256 !== source.documentSha256 ||
    typeof value.structure_target_summary_sha256 !== "string" ||
    !SHA_PATTERN.test(value.structure_target_summary_sha256) ||
    typeof value.index_manifest_sha256 !== "string" ||
    !SHA_PATTERN.test(value.index_manifest_sha256) ||
    typeof value.section_path !== "string" ||
    !SECTION_PATH_PATTERN.test(value.section_path) ||
    !boundedInteger(value.section_index, 0, 100_000) ||
    typeof value.picture_target_id !== "string" ||
    !PICTURE_TARGET_ID_PATTERN.test(value.picture_target_id) ||
    !boundedInteger(value.page_hint, 1, 10_000) ||
    !boundedString(value.page_hint_source, 120) ||
    !(
      value.cell_target_id === null ||
      (typeof value.cell_target_id === "string" &&
        CELL_TARGET_ID_PATTERN.test(value.cell_target_id))
    ) ||
    !boundedInteger(value.table_index, 0, 100_000, { nullable: true }) ||
    !boundedInteger(value.cell_row, 0, 100_000, { nullable: true }) ||
    !boundedInteger(value.cell_column, 0, 100_000, { nullable: true }) ||
    !boundedInteger(value.picture_index, 0, 100_000) ||
    !(
      value.sample_layout_binding_schema === null ||
      value.sample_layout_binding_schema ===
        "trafficvisson.hwpx-sample-layout.v2"
    ) ||
    !(
      value.sample_layout_binding_revision === null ||
      value.sample_layout_binding_revision === 2
    ) ||
    !(
      value.semantic_projection_revision === null ||
      value.semantic_projection_revision ===
        "audited-photo-caption-evidence-group-v2"
    ) ||
    !(
      value.sample_evidence_group_id === null ||
      (typeof value.sample_evidence_group_id === "string" &&
        EVIDENCE_GROUP_ID_PATTERN.test(
          value.sample_evidence_group_id,
        ))
    ) ||
    !boundedInteger(value.group_photo_index, 1, 10_000, {
      nullable: true,
    }) ||
    !boundedInteger(value.group_photo_count, 1, 10_000, {
      nullable: true,
    }) ||
    !(
      value.photo_slot_id === null ||
      (typeof value.photo_slot_id === "string" &&
        PHOTO_SLOT_ID_PATTERN.test(value.photo_slot_id))
    ) ||
    !(
      value.sample_layout_binding_sha256 === null ||
      (typeof value.sample_layout_binding_sha256 === "string" &&
        SHA_PATTERN.test(value.sample_layout_binding_sha256))
    ) ||
    !(
      value.semantic_attestation_sha256 === null ||
      (typeof value.semantic_attestation_sha256 === "string" &&
        SHA_PATTERN.test(value.semantic_attestation_sha256))
    ) ||
    !boundedInteger(
      value.duplicate_image_occurrence_count,
      1,
      10_000,
    ) ||
    typeof value.duplicate_image_review_required !== "boolean" ||
    typeof value.duplicate_image_registry_complete !== "boolean" ||
    !Array.isArray(value.duplicate_image_case_refs) ||
    value.duplicate_image_case_refs.length < 1 ||
    value.duplicate_image_case_refs.length > 10_000 ||
    value.duplicate_image_case_refs.some(
      (caseRef) =>
        typeof caseRef !== "string" ||
        !DUPLICATE_CASE_REF_PATTERN.test(caseRef),
    ) ||
    !boundedString(value.caption, 320, { nullable: true }) ||
    !boundedString(value.route, 320, { nullable: true }) ||
    !boundedString(value.location, 320, { nullable: true }) ||
    !boundedString(value.evaluation, 320, { nullable: true }) ||
    !boundedString(value.opinion, 320, { nullable: true }) ||
    typeof value.image_available !== "boolean" ||
    !(
      value.image_sha256 === null ||
      (typeof value.image_sha256 === "string" &&
        SHA_PATTERN.test(value.image_sha256))
    ) ||
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
    (value.image_sha256 === null ||
      value.image_media_type === null ||
      value.image_width === null ||
      value.image_height === null)
  ) {
    return null;
  }
  const layoutFields = [
    value.sample_layout_binding_schema,
    value.sample_layout_binding_revision,
    value.semantic_projection_revision,
    value.sample_evidence_group_id,
    value.group_photo_index,
    value.group_photo_count,
    value.photo_slot_id,
    value.sample_layout_binding_sha256,
    value.semantic_attestation_sha256,
  ];
  const duplicateRefs = value.duplicate_image_case_refs as string[];
  if (
    (layoutFields.some((field) => field !== null) &&
      layoutFields.some((field) => field === null)) ||
    (value.group_photo_index !== null &&
      value.group_photo_count !== null &&
      Number(value.group_photo_index) >
        Number(value.group_photo_count)) ||
    Number(value.duplicate_image_occurrence_count) !==
      duplicateRefs.length ||
    duplicateRefs.join("\0") !==
      [...new Set(duplicateRefs)].sort().join("\0") ||
    !duplicateRefs.includes(
      `${String(value.source_id)}/${String(value.case_id)}`,
    ) ||
    value.duplicate_image_review_required !==
      (Number(value.duplicate_image_occurrence_count) > 1 ||
        !value.duplicate_image_registry_complete)
  ) {
    return null;
  }
  const approvalFields = [
    value.approval_id,
    value.approval_revision,
    value.approval_record_sha256,
    value.approved_issue_type,
    value.approved_action,
  ];
  if (
    value.approved_reference
      ? (
          value.reference_state !== "approved_reference" ||
          value.review_required ||
          !value.training_eligible ||
          typeof value.approval_id !== "string" ||
          !APPROVAL_ID_PATTERN.test(value.approval_id) ||
          !boundedInteger(value.approval_revision, 1, 1_000_000_000) ||
          typeof value.approval_record_sha256 !== "string" ||
          !SHA_PATTERN.test(value.approval_record_sha256) ||
          typeof value.approved_issue_type !== "string" ||
          !ISSUE_TYPES.has(value.approved_issue_type) ||
          !boundedString(value.approved_action, 500)
        )
      : (
          value.reference_state !== "weak_unconfirmed" ||
          !value.review_required ||
          value.training_eligible ||
          approvalFields.some((field) => field !== null)
        )
  ) {
    return null;
  }
  return {
    sourceId: value.source_id as string,
    caseId: value.case_id,
    rank: Number(value.rank),
    similarityScore: Number(value.similarity_score),
    matchedTerms: value.matched_terms as string[],
    referenceState: value.reference_state,
    reviewRequired: value.review_required,
    approvedReference: value.approved_reference,
    trainingEligible: value.training_eligible,
    quarter: source.quarter,
    documentName: source.documentName,
    documentSha256: source.documentSha256,
    structureTargetSummarySha256:
      value.structure_target_summary_sha256,
    indexManifestSha256: value.index_manifest_sha256,
    sectionPath: value.section_path,
    sectionIndex: Number(value.section_index),
    pictureTargetId: value.picture_target_id,
    pageHint: Number(value.page_hint),
    cellTargetId: value.cell_target_id as string | null,
    tableIndex: value.table_index as number | null,
    cellRow: value.cell_row as number | null,
    cellColumn: value.cell_column as number | null,
    pictureIndex: Number(value.picture_index),
    sampleLayoutBindingSchema:
      value.sample_layout_binding_schema as
        | "trafficvisson.hwpx-sample-layout.v2"
        | null,
    sampleLayoutBindingRevision:
      value.sample_layout_binding_revision as 2 | null,
    semanticProjectionRevision:
      value.semantic_projection_revision as
        | "audited-photo-caption-evidence-group-v2"
        | null,
    sampleEvidenceGroupId:
      value.sample_evidence_group_id as string | null,
    groupPhotoIndex: value.group_photo_index as number | null,
    groupPhotoCount: value.group_photo_count as number | null,
    photoSlotId: value.photo_slot_id as string | null,
    sampleLayoutBindingSha256:
      value.sample_layout_binding_sha256 as string | null,
    semanticAttestationSha256:
      value.semantic_attestation_sha256 as string | null,
    duplicateImageOccurrenceCount: Number(
      value.duplicate_image_occurrence_count,
    ),
    duplicateImageReviewRequired:
      value.duplicate_image_review_required,
    duplicateImageRegistryComplete:
      value.duplicate_image_registry_complete,
    duplicateImageCaseRefs: duplicateRefs,
    caption: value.caption as string | null,
    route: value.route as string | null,
    location: value.location as string | null,
    evaluation: value.evaluation as string | null,
    opinion: value.opinion as string | null,
    imageAvailable: value.image_available,
    imageSha256: value.image_sha256 as string | null,
    imageMediaType: value.image_media_type as WeakCaseReference["imageMediaType"],
    approvalId: value.approval_id as string | null,
    approvalRevision: value.approval_revision as number | null,
    approvalRecordSha256: value.approval_record_sha256 as string | null,
    approvedIssueType:
      value.approved_issue_type as AnalysisVisionIssueType | null,
    approvedAction: value.approved_action as string | null,
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
    value.schema_version !== "trafficvisson.weak-case-search.v2" ||
    value.search_mode !== "bounded_text_and_structure_metadata" ||
    value.semantic_image_embedding_used !== false ||
    (value.capability_state !== "approved_reference_available" &&
      value.capability_state !== "weak_reference_only" &&
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
    !boundedInteger(value.approved_reference_count, 0, 5) ||
    value.annotation_count !== 0 ||
    value.incident_count !== 0 ||
    !boundedInteger(value.training_eligible_count, 0, 5)
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
    ((value.capability_state === "weak_reference_only" ||
      value.capability_state === "approved_reference_available") &&
      readySourceCount < 1) ||
    (value.capability_state === "unavailable" &&
      readySourceCount !== 0) ||
    Number(value.approved_reference_count) !==
      cases.filter((item) => item?.approvedReference).length ||
    Number(value.training_eligible_count) !==
      cases.filter((item) => item?.trainingEligible).length ||
    (value.capability_state === "approved_reference_available" &&
      Number(value.approved_reference_count) < 1) ||
    (value.capability_state !== "approved_reference_available" &&
      Number(value.approved_reference_count) !== 0) ||
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
    approvedReferenceCount: Number(value.approved_reference_count),
    annotationCount: 0,
    incidentCount: 0,
    trainingEligibleCount: Number(value.training_eligible_count),
  };
}
