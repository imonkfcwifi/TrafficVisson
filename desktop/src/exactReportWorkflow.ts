import type { PipelineJob } from "./pipelineJobs";

const UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const PREVIEW_ID = /^report-preview-attempt-(?:0|[1-9][0-9]?)$/;
const DRAFT_ID = /^current-report-[a-f0-9]{32}$/;
const APPLICATION_ID =
  /^current-report-application-[a-f0-9]{32}$/;

export type ExactPipelineReportContext = {
  projectId: string;
  pipelineJobId: string;
  previewId: string;
  operationCount: number;
};

export type ExactReportDocumentTargetAddress = {
  targetId: string;
  targetKind: "cell" | "picture" | "paragraph";
  pagePairIndex: number;
  pageNumber: number;
  sectionPath: string;
  sectionIndex: number;
  tableId: string | null;
  tableIndex: number | null;
  cellTargetId: string | null;
  cellIndex: number | null;
  row: number | null;
  column: number | null;
  rowSpan: number | null;
  columnSpan: number | null;
  pictureIndex: number | null;
  paragraphAddressSha256: string | null;
  photoSlotIds: readonly string[];
};

export type ExactReportCheckboxTarget = {
  pagePairIndex: number;
  pageNumber: number;
  rowOrder: number;
  row: number;
  grade: string;
  anchorColumn: number;
  selected: boolean;
  selectedGrade: string;
};

export type ExactReportApprovedIncident = {
  incidentId: string;
  incidentOrder: number;
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  sourceSha256: string;
  approvalActor: "local_user";
  approvalRecordId: string;
  approvalRecordSha256: string;
  selectedEvidenceRef: string;
  selectedEvidenceSha256: string;
  evidencePolicy: "internal_original" | "shared_deidentified";
  photoSlotId: string;
  photoBindingSha256: string;
  provenanceSha256: string;
};

export type ExactReportDocumentEditOperation = {
  operationId: string;
  exactOperationKind:
    | "replace_cell_text"
    | "replace_paragraph_text"
    | "replace_picture";
  semanticRoles: readonly string[];
  bindingGroups: readonly string[];
  target: ExactReportDocumentTargetAddress;
  expectedSampleValueSha256: string;
  proposedValueSha256: string;
  proposedText: string | null;
  proposedEvidenceSha256: string | null;
  currentSourceRefs: readonly string[];
  approvedIncidentIds: readonly string[];
  checkboxTargets: readonly ExactReportCheckboxTarget[];
  opinionTarget: boolean;
  neutralizesHistoricalSampleFact: boolean;
  unusedSampleFactualSlotNeutralized: boolean;
};

export type ExactReportDocumentEditPlan = {
  schemaVersion: "trafficvisson.current-report-document-edit-plan.v2";
  state: "exact_targets_bound_user_review_required";
  planId: string;
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  sourceSha256: string;
  templateId: string;
  templateQuarter: 3 | 4;
  documentSnapshotId: string;
  templateSha256: string;
  structureTargetSummarySha256: string;
  bindingManifestSha256: string;
  exactPlanSha256: string;
  approvedIncidentSetSha256: string;
  approvedIncidents: readonly ExactReportApprovedIncident[];
  operations: readonly ExactReportDocumentEditOperation[];
  staleSampleFactualTargetCount: number;
  staleSampleFactualTargetCoverageComplete: true;
  unusedSampleNeutralizationOperationIds: readonly string[];
  documentEditPlanSha256: string;
  acceptRejectRollbackBindingSha256: string;
  expectedRenderedPageCount: 48;
  addressAuthority: "server_audited_sample_layout";
  factualAuthority: "current_local_user_approved_incidents";
  providerMaySelectTargetAddresses: false;
  applyApprovalRequired: true;
  acceptRejectRequiredAfterApply: true;
  rollbackSupportedAfterApply: true;
  providerInvocationCount: 0;
  exactWriterInvocationCount: 0;
  comInvocationCount: 0;
  documentWritten: false;
  pathExposed: false;
};

export type ExactReportPreview = {
  schemaVersion: "trafficvisson.current-report-exact-preview.v1";
  mode: "exact_current_report_preview";
  state:
    "exact_current_report_draft_ready_apply_approval_required";
  draftId: string;
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  approvedIncidentCount: number;
  bindingManifestSha256: string;
  exactPlanSha256: string;
  documentEditPlan: ExactReportDocumentEditPlan;
  documentEditPlanSha256: string;
  acceptRejectRollbackBindingSha256: string;
  outputDocumentSha256: string;
  renderBindingSha256: string;
  recordSha256: string;
  operationCount: number;
  expectedRenderedPageCount: 48;
  approvalToken: string;
  approvalExpiresAt: string;
  idempotentReplay: boolean;
};

export type ExactReportApplicationState =
  | "render_validated_accept_or_reject_required"
  | "render_validation_failed_rolled_back"
  | "render_validation_failed_rollback_blocked"
  | "accepted_render_validated_report"
  | "rejected_report_rolled_back"
  | "accepted_report_rolled_back";

export type ExactReportApplication = {
  schemaVersion: "trafficvisson.current-report-application.v1";
  applicationId: string;
  state: ExactReportApplicationState;
  projectId: string;
  pipelineJobId: string;
  analysisJobId: string;
  draftId: string;
  bindingManifestSha256: string;
  exactPlanSha256: string;
  documentEditPlanSha256: string;
  acceptRejectRollbackBindingSha256: string;
  outputFileName: string;
  outputSha256: string | null;
  pdfFileName: string;
  pdfSha256: string | null;
  renderValidationStatus: string;
  renderedPageCount: number | null;
  renderFailureCodes: string[];
  outputReopenStructureSha256: string | null;
  rasterArtifactManifestSha256: string | null;
  comPublishCompleted: boolean;
  pdfExportCompleted: boolean;
  renderValidationCompleted: boolean;
  rollbackCompleted: boolean;
  acceptRejectRequired: boolean;
  accepted: boolean;
  rejected: boolean;
};

function record(
  value: unknown,
  label: string,
): Record<string, unknown> {
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

function boundedInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  label: string,
) {
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function string(
  value: unknown,
  pattern: RegExp,
  label: string,
  maximum = 255,
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

function optionalSha(value: unknown, label: string) {
  return value === null ? null : string(value, SHA256, label, 64);
}

function boundedText(
  value: unknown,
  label: string,
  maximum = 2_000,
  minimum = 1,
) {
  if (
    typeof value !== "string" ||
    value.length < minimum ||
    value.length > maximum ||
    /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value)
  ) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value;
}

function nullableInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  label: string,
) {
  return value === null
    ? null
    : boundedInteger(value, minimum, maximum, label);
}

function boundedStringArray(
  value: unknown,
  label: string,
  maximumItems: number,
  maximumLength = 512,
) {
  if (!Array.isArray(value) || value.length > maximumItems) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value.map((item, index) =>
    boundedText(
      item,
      `${label} ${index + 1}`,
      maximumLength,
    ),
  );
}

function hasAbsolutePath(value: unknown): boolean {
  if (typeof value === "string") {
    return (
      /^[a-z]:[\\/]/iu.test(value) ||
      /^\\\\/u.test(value) ||
      /^\/\//u.test(value) ||
      /^file:/iu.test(value) ||
      /^\//u.test(value)
    );
  }
  if (Array.isArray(value)) return value.some(hasAbsolutePath);
  if (value && typeof value === "object") {
    return Object.values(value).some(hasAbsolutePath);
  }
  return false;
}

function parseExactDocumentTargetAddress(
  value: unknown,
): ExactReportDocumentTargetAddress {
  const source = record(value, "정확 문서 대상 주소");
  exactKeys(
    source,
    [
      "target_id",
      "target_kind",
      "page_pair_index",
      "page_number",
      "section_path",
      "section_index",
      "table_id",
      "table_index",
      "cell_target_id",
      "cell_index",
      "row",
      "column",
      "row_span",
      "column_span",
      "picture_index",
      "paragraph_address_sha256",
      "photo_slot_ids",
    ],
    "정확 문서 대상 주소",
  );
  if (
    source.target_kind !== "cell" &&
    source.target_kind !== "picture" &&
    source.target_kind !== "paragraph"
  ) {
    throw new Error("정확 문서 대상 주소 응답 형식이 올바르지 않습니다.");
  }
  const cellTargetId =
    source.cell_target_id === null
      ? null
      : boundedText(source.cell_target_id, "셀 대상 ID", 255);
  const cellIndex = nullableInteger(
    source.cell_index,
    0,
    100_000,
    "셀 번호",
  );
  if (
    (source.target_kind === "cell" ||
      source.target_kind === "picture") &&
    (cellTargetId === null || cellIndex === null)
  ) {
    throw new Error(
      "표/사진 대상의 정확한 포함 셀 주소가 누락되었습니다.",
    );
  }
  if (
    source.target_kind === "paragraph" &&
    (cellTargetId !== null || cellIndex !== null)
  ) {
    throw new Error(
      "문단 대상에 잘못된 셀 주소가 포함되었습니다.",
    );
  }
  return {
    targetId: boundedText(source.target_id, "대상 ID", 512),
    targetKind: source.target_kind,
    pagePairIndex: boundedInteger(
      source.page_pair_index,
      0,
      47,
      "페이지 쌍",
    ),
    pageNumber: boundedInteger(source.page_number, 1, 48, "페이지"),
    sectionPath: boundedText(
      source.section_path,
      "섹션 주소",
      255,
    ),
    sectionIndex: boundedInteger(
      source.section_index,
      0,
      47,
      "섹션 번호",
    ),
    tableId:
      source.table_id === null
        ? null
        : boundedText(source.table_id, "표 ID", 255),
    tableIndex: nullableInteger(
      source.table_index,
      0,
      10_000,
      "표 번호",
    ),
    cellTargetId,
    cellIndex,
    row: nullableInteger(source.row, 0, 10_000, "행"),
    column: nullableInteger(source.column, 0, 10_000, "열"),
    rowSpan: nullableInteger(source.row_span, 1, 10_000, "행 병합"),
    columnSpan: nullableInteger(
      source.column_span,
      1,
      10_000,
      "열 병합",
    ),
    pictureIndex: nullableInteger(
      source.picture_index,
      0,
      100_000,
      "사진 번호",
    ),
    paragraphAddressSha256: optionalSha(
      source.paragraph_address_sha256,
      "문단 주소 해시",
    ),
    photoSlotIds: boundedStringArray(
      source.photo_slot_ids,
      "사진 슬롯 ID",
      8,
      255,
    ),
  };
}

function parseExactCheckboxTarget(
  value: unknown,
): ExactReportCheckboxTarget {
  const source = record(value, "평가 네모 대상");
  exactKeys(
    source,
    [
      "page_pair_index",
      "page_number",
      "row_order",
      "row",
      "grade",
      "anchor_column",
      "selected",
      "selected_grade",
    ],
    "평가 네모 대상",
  );
  if (typeof source.selected !== "boolean") {
    throw new Error("평가 네모 대상 응답 형식이 올바르지 않습니다.");
  }
  return {
    pagePairIndex: boundedInteger(
      source.page_pair_index,
      0,
      47,
      "평가 페이지 쌍",
    ),
    pageNumber: boundedInteger(
      source.page_number,
      1,
      48,
      "평가 페이지",
    ),
    rowOrder: boundedInteger(source.row_order, 0, 1_000, "평가 행 순서"),
    row: boundedInteger(source.row, 0, 10_000, "평가 행"),
    grade: boundedText(source.grade, "평가 등급", 80),
    anchorColumn: boundedInteger(
      source.anchor_column,
      0,
      10_000,
      "평가 기준 열",
    ),
    selected: source.selected,
    selectedGrade: boundedText(
      source.selected_grade,
      "선택 평가 등급",
      80,
    ),
  };
}

function parseExactApprovedIncident(
  value: unknown,
  scope: {
    projectId: string;
    pipelineJobId: string;
    analysisJobId: string;
    sourceSha256: string;
  },
): ExactReportApprovedIncident {
  const source = record(value, "승인 사건 문서 provenance");
  exactKeys(
    source,
    [
      "incident_id",
      "incident_order",
      "project_id",
      "pipeline_job_id",
      "analysis_job_id",
      "source_sha256",
      "approval_actor",
      "approval_record_id",
      "approval_record_sha256",
      "selected_evidence_ref",
      "selected_evidence_sha256",
      "evidence_policy",
      "photo_slot_id",
      "photo_binding_sha256",
      "provenance_sha256",
    ],
    "승인 사건 문서 provenance",
  );
  if (
    source.approval_actor !== "local_user" ||
    (source.evidence_policy !== "internal_original" &&
      source.evidence_policy !== "shared_deidentified") ||
    source.project_id !== scope.projectId ||
    source.pipeline_job_id !== scope.pipelineJobId ||
    source.analysis_job_id !== scope.analysisJobId ||
    source.source_sha256 !== scope.sourceSha256
  ) {
    throw new Error(
      "승인 사건 문서 provenance 응답 형식이 올바르지 않습니다.",
    );
  }
  return {
    incidentId: boundedText(source.incident_id, "승인 사건 ID", 255),
    incidentOrder: boundedInteger(
      source.incident_order,
      0,
      499,
      "승인 사건 순서",
    ),
    projectId: string(source.project_id, UUID, "프로젝트 ID", 36),
    pipelineJobId: string(
      source.pipeline_job_id,
      UUID,
      "파이프라인 작업 ID",
      36,
    ),
    analysisJobId: string(
      source.analysis_job_id,
      UUID,
      "분석 작업 ID",
      36,
    ),
    sourceSha256: string(
      source.source_sha256,
      SHA256,
      "원본 영상 해시",
      64,
    ),
    approvalActor: "local_user",
    approvalRecordId: boundedText(
      source.approval_record_id,
      "사용자 승인 레코드 ID",
      255,
    ),
    approvalRecordSha256: string(
      source.approval_record_sha256,
      SHA256,
      "사용자 승인 레코드 해시",
      64,
    ),
    selectedEvidenceRef: boundedText(
      source.selected_evidence_ref,
      "선택 증거 참조",
      512,
    ),
    selectedEvidenceSha256: string(
      source.selected_evidence_sha256,
      SHA256,
      "선택 증거 해시",
      64,
    ),
    evidencePolicy: source.evidence_policy,
    photoSlotId: boundedText(
      source.photo_slot_id,
      "사진 슬롯 ID",
      255,
    ),
    photoBindingSha256: string(
      source.photo_binding_sha256,
      SHA256,
      "사진 바인딩 해시",
      64,
    ),
    provenanceSha256: string(
      source.provenance_sha256,
      SHA256,
      "사건 provenance 해시",
      64,
    ),
  };
}

function parseExactDocumentEditOperation(
  value: unknown,
): ExactReportDocumentEditOperation {
  const source = record(value, "정확 문서 편집 작업");
  exactKeys(
    source,
    [
      "operation_id",
      "exact_operation_kind",
      "semantic_roles",
      "binding_groups",
      "target",
      "expected_sample_value_sha256",
      "proposed_value_sha256",
      "proposed_text",
      "proposed_evidence_sha256",
      "current_source_refs",
      "approved_incident_ids",
      "checkbox_targets",
      "opinion_target",
      "neutralizes_historical_sample_fact",
      "unused_sample_factual_slot_neutralized",
    ],
    "정확 문서 편집 작업",
  );
  const operationKinds = new Set([
    "replace_cell_text",
    "replace_paragraph_text",
    "replace_picture",
  ]);
  if (
    typeof source.exact_operation_kind !== "string" ||
    !operationKinds.has(source.exact_operation_kind) ||
    typeof source.opinion_target !== "boolean" ||
    typeof source.neutralizes_historical_sample_fact !== "boolean" ||
    typeof source.unused_sample_factual_slot_neutralized !== "boolean" ||
    !Array.isArray(source.checkbox_targets) ||
    source.checkbox_targets.length > 16
  ) {
    throw new Error("정확 문서 편집 작업 응답 형식이 올바르지 않습니다.");
  }
  const proposedText =
    source.proposed_text === null
      ? null
      : boundedText(source.proposed_text, "제안 문구", 20_000, 0);
  return {
    operationId: boundedText(source.operation_id, "편집 작업 ID", 255),
    exactOperationKind:
      source.exact_operation_kind as ExactReportDocumentEditOperation["exactOperationKind"],
    semanticRoles: boundedStringArray(
      source.semantic_roles,
      "편집 의미 역할",
      32,
      120,
    ),
    bindingGroups: boundedStringArray(
      source.binding_groups,
      "편집 바인딩 그룹",
      32,
      120,
    ),
    target: parseExactDocumentTargetAddress(source.target),
    expectedSampleValueSha256: string(
      source.expected_sample_value_sha256,
      SHA256,
      "기존 샘플 값 해시",
      64,
    ),
    proposedValueSha256: string(
      source.proposed_value_sha256,
      SHA256,
      "제안 값 해시",
      64,
    ),
    proposedText,
    proposedEvidenceSha256: optionalSha(
      source.proposed_evidence_sha256,
      "제안 증거 해시",
    ),
    currentSourceRefs: boundedStringArray(
      source.current_source_refs,
      "현재 출처 참조",
      32,
      512,
    ),
    approvedIncidentIds: boundedStringArray(
      source.approved_incident_ids,
      "승인 사건 ID",
      500,
      255,
    ),
    checkboxTargets: source.checkbox_targets.map(
      parseExactCheckboxTarget,
    ),
    opinionTarget: source.opinion_target,
    neutralizesHistoricalSampleFact:
      source.neutralizes_historical_sample_fact,
    unusedSampleFactualSlotNeutralized:
      source.unused_sample_factual_slot_neutralized,
  };
}

function parseExactReportDocumentEditPlan(
  value: unknown,
): ExactReportDocumentEditPlan {
  const source = record(value, "정확 DocumentEditPlan");
  exactKeys(
    source,
    [
      "schema_version",
      "state",
      "plan_id",
      "project_id",
      "pipeline_job_id",
      "analysis_job_id",
      "source_sha256",
      "template_id",
      "template_quarter",
      "document_snapshot_id",
      "template_sha256",
      "structure_target_summary_sha256",
      "binding_manifest_sha256",
      "exact_plan_sha256",
      "approved_incident_set_sha256",
      "approved_incidents",
      "operations",
      "stale_sample_factual_target_count",
      "stale_sample_factual_target_coverage_complete",
      "unused_sample_neutralization_operation_ids",
      "document_edit_plan_sha256",
      "accept_reject_rollback_binding_sha256",
      "expected_rendered_page_count",
      "address_authority",
      "factual_authority",
      "provider_may_select_target_addresses",
      "apply_approval_required",
      "accept_reject_required_after_apply",
      "rollback_supported_after_apply",
      "provider_invocation_count",
      "exact_writer_invocation_count",
      "com_invocation_count",
      "document_written",
      "path_exposed",
    ],
    "정확 DocumentEditPlan",
  );
  if (
    hasAbsolutePath(source) ||
    source.schema_version !==
      "trafficvisson.current-report-document-edit-plan.v2" ||
    source.state !== "exact_targets_bound_user_review_required" ||
    (source.template_quarter !== 3 && source.template_quarter !== 4) ||
    !Array.isArray(source.approved_incidents) ||
    source.approved_incidents.length < 1 ||
    source.approved_incidents.length > 500 ||
    !Array.isArray(source.operations) ||
    source.operations.length < 1 ||
    source.operations.length > 5_000 ||
    source.stale_sample_factual_target_coverage_complete !== true ||
    source.expected_rendered_page_count !== 48 ||
    source.address_authority !== "server_audited_sample_layout" ||
    source.factual_authority !==
      "current_local_user_approved_incidents" ||
    source.provider_may_select_target_addresses !== false ||
    source.apply_approval_required !== true ||
    source.accept_reject_required_after_apply !== true ||
    source.rollback_supported_after_apply !== true ||
    source.provider_invocation_count !== 0 ||
    source.exact_writer_invocation_count !== 0 ||
    source.com_invocation_count !== 0 ||
    source.document_written !== false ||
    source.path_exposed !== false
  ) {
    throw new Error("정확 DocumentEditPlan 응답 형식이 올바르지 않습니다.");
  }
  const projectId = string(source.project_id, UUID, "프로젝트 ID", 36);
  const pipelineJobId = string(
    source.pipeline_job_id,
    UUID,
    "파이프라인 작업 ID",
    36,
  );
  const analysisJobId = string(
    source.analysis_job_id,
    UUID,
    "분석 작업 ID",
    36,
  );
  const sourceSha256 = string(
    source.source_sha256,
    SHA256,
    "원본 영상 해시",
    64,
  );
  const approvedIncidents = source.approved_incidents.map((item) =>
    parseExactApprovedIncident(item, {
      projectId,
      pipelineJobId,
      analysisJobId,
      sourceSha256,
    }),
  );
  const operations = source.operations.map(
    parseExactDocumentEditOperation,
  );
  const approvedIncidentIds = new Set(
    approvedIncidents.map((item) => item.incidentId),
  );
  if (
    approvedIncidentIds.size !== approvedIncidents.length ||
    approvedIncidents.some(
      (item, index) => item.incidentOrder !== index,
    ) ||
    operations.some((operation) =>
      operation.approvedIncidentIds.some(
        (incidentId) => !approvedIncidentIds.has(incidentId),
      ),
    )
  ) {
    throw new Error("정확 DocumentEditPlan 사건 결속이 올바르지 않습니다.");
  }
  return {
    schemaVersion:
      "trafficvisson.current-report-document-edit-plan.v2",
    state: "exact_targets_bound_user_review_required",
    planId: string(
      source.plan_id,
      /^exact-document-edit-plan-[a-f0-9]{32}$/,
      "DocumentEditPlan ID",
      57,
    ),
    projectId,
    pipelineJobId,
    analysisJobId,
    sourceSha256,
    templateId: boundedText(source.template_id, "양식 ID", 255),
    templateQuarter: source.template_quarter,
    documentSnapshotId: boundedText(
      source.document_snapshot_id,
      "문서 스냅샷 ID",
      255,
    ),
    templateSha256: string(
      source.template_sha256,
      SHA256,
      "양식 해시",
      64,
    ),
    structureTargetSummarySha256: string(
      source.structure_target_summary_sha256,
      SHA256,
      "양식 구조 해시",
      64,
    ),
    bindingManifestSha256: string(
      source.binding_manifest_sha256,
      SHA256,
      "바인딩 명세 해시",
      64,
    ),
    exactPlanSha256: string(
      source.exact_plan_sha256,
      SHA256,
      "정확 계획 해시",
      64,
    ),
    approvedIncidentSetSha256: string(
      source.approved_incident_set_sha256,
      SHA256,
      "승인 사건 집합 해시",
      64,
    ),
    approvedIncidents,
    operations,
    staleSampleFactualTargetCount: boundedInteger(
      source.stale_sample_factual_target_count,
      0,
      5_000,
      "이전 샘플 사실 대상 수",
    ),
    staleSampleFactualTargetCoverageComplete: true,
    unusedSampleNeutralizationOperationIds: boundedStringArray(
      source.unused_sample_neutralization_operation_ids,
      "미사용 샘플 중립화 작업 ID",
      5_000,
      255,
    ),
    documentEditPlanSha256: string(
      source.document_edit_plan_sha256,
      SHA256,
      "DocumentEditPlan 해시",
      64,
    ),
    acceptRejectRollbackBindingSha256: string(
      source.accept_reject_rollback_binding_sha256,
      SHA256,
      "수락·거절·롤백 바인딩 해시",
      64,
    ),
    expectedRenderedPageCount: 48,
    addressAuthority: "server_audited_sample_layout",
    factualAuthority: "current_local_user_approved_incidents",
    providerMaySelectTargetAddresses: false,
    applyApprovalRequired: true,
    acceptRejectRequiredAfterApply: true,
    rollbackSupportedAfterApply: true,
    providerInvocationCount: 0,
    exactWriterInvocationCount: 0,
    comInvocationCount: 0,
    documentWritten: false,
    pathExposed: false,
  };
}

export function exactReportContextFromPipeline(
  job: PipelineJob | null,
): ExactPipelineReportContext | null {
  if (!job || job.status !== "completed") return null;
  const stage = job.stages.find(
    (item) => item.name === "derived_outputs",
  );
  if (
    !stage ||
    stage.status !== "success" ||
    stage.outcome !== "success"
  ) {
    return null;
  }
  const reportValue = stage?.checkpoint.report;
  if (!reportValue) return null;
  let report: Record<string, unknown>;
  try {
    report = record(reportValue, "정확 보고서 파이프라인");
    exactKeys(
      report,
      [
        "state",
        "preview_id",
        "plan_sha256",
        "draft_sha256",
        "operation_count",
        "blocking_reasons",
        "provider_called",
        "document_edit_plan_prepared",
        "com_invoked",
      ],
      "정확 보고서 파이프라인",
    );
  } catch {
    return null;
  }
  if (
    report.state !== "validated_preview" ||
    typeof report.preview_id !== "string" ||
    !PREVIEW_ID.test(report.preview_id) ||
    typeof report.operation_count !== "number" ||
    !Number.isInteger(report.operation_count) ||
    report.operation_count < 1 ||
    report.operation_count > 5_000 ||
    typeof report.plan_sha256 !== "string" ||
    !SHA256.test(report.plan_sha256) ||
    typeof report.draft_sha256 !== "string" ||
    !SHA256.test(report.draft_sha256) ||
    !Array.isArray(report.blocking_reasons) ||
    report.blocking_reasons.length !== 0 ||
    typeof report.provider_called !== "boolean" ||
    report.document_edit_plan_prepared !== true ||
    report.com_invoked !== false
  ) {
    return null;
  }
  return {
    projectId: job.projectId,
    pipelineJobId: job.jobId,
    previewId: report.preview_id,
    operationCount: report.operation_count,
  };
}

export function parseExactReportPreview(
  value: unknown,
): ExactReportPreview {
  const source = record(value, "정확 HWPX 미리보기");
  exactKeys(
    source,
    [
      "schema_version",
      "mode",
      "state",
      "draft_id",
      "project_id",
      "pipeline_job_id",
      "analysis_job_id",
      "approved_incident_count",
      "binding_manifest_sha256",
      "exact_plan_sha256",
      "document_edit_plan",
      "document_edit_plan_sha256",
      "accept_reject_rollback_binding_sha256",
      "output_document_sha256",
      "render_binding_sha256",
      "record_sha256",
      "operation_count",
      "expected_rendered_page_count",
      "approval_token",
      "approval_expires_at",
      "apply_approval_required",
      "apply_endpoint_available",
      "remaining_blocker_codes",
      "exact_writer_completed",
      "output_render_validation_status",
      "provider_invocation_count",
      "com_invocation_count",
      "document_published",
      "path_exposed",
      "idempotent_replay",
    ],
    "정확 HWPX 미리보기",
  );
  if (
    source.schema_version !==
      "trafficvisson.current-report-exact-preview.v1" ||
    source.mode !== "exact_current_report_preview" ||
    source.state !==
      "exact_current_report_draft_ready_apply_approval_required" ||
    source.apply_approval_required !== true ||
    source.apply_endpoint_available !== true ||
    !Array.isArray(source.remaining_blocker_codes) ||
    source.remaining_blocker_codes.length !== 0 ||
    source.exact_writer_completed !== true ||
    source.output_render_validation_status !==
      "not_run_com_validation_required" ||
    source.provider_invocation_count !== 0 ||
    source.com_invocation_count !== 0 ||
    source.document_published !== false ||
    source.path_exposed !== false ||
    typeof source.idempotent_replay !== "boolean" ||
    source.expected_rendered_page_count !== 48 ||
    typeof source.approval_expires_at !== "string" ||
    !Number.isFinite(Date.parse(source.approval_expires_at))
  ) {
    throw new Error("정확 HWPX 미리보기 응답 형식이 올바르지 않습니다.");
  }
  const documentEditPlan = parseExactReportDocumentEditPlan(
    source.document_edit_plan,
  );
  const bindingManifestSha256 = string(
    source.binding_manifest_sha256,
    SHA256,
    "바인딩 해시",
    64,
  );
  const exactPlanSha256 = string(
    source.exact_plan_sha256,
    SHA256,
    "편집 계획 해시",
    64,
  );
  const documentEditPlanSha256 = string(
    source.document_edit_plan_sha256,
    SHA256,
    "DocumentEditPlan 해시",
    64,
  );
  const acceptRejectRollbackBindingSha256 = string(
    source.accept_reject_rollback_binding_sha256,
    SHA256,
    "수락·거절·롤백 바인딩 해시",
    64,
  );
  if (
    documentEditPlan.projectId !== source.project_id ||
    documentEditPlan.pipelineJobId !== source.pipeline_job_id ||
    documentEditPlan.analysisJobId !== source.analysis_job_id ||
    documentEditPlan.approvedIncidents.length !==
      source.approved_incident_count ||
    documentEditPlan.operations.length !== source.operation_count ||
    documentEditPlan.bindingManifestSha256 !== bindingManifestSha256 ||
    documentEditPlan.exactPlanSha256 !== exactPlanSha256 ||
    documentEditPlan.documentEditPlanSha256 !==
      documentEditPlanSha256 ||
    documentEditPlan.acceptRejectRollbackBindingSha256 !==
      acceptRejectRollbackBindingSha256
  ) {
    throw new Error(
      "정확 HWPX 미리보기와 DocumentEditPlan provenance가 일치하지 않습니다.",
    );
  }
  return {
    schemaVersion: source.schema_version,
    mode: source.mode,
    state: source.state,
    draftId: string(source.draft_id, DRAFT_ID, "초안 ID", 47),
    projectId: string(source.project_id, UUID, "프로젝트 ID", 36),
    pipelineJobId: string(
      source.pipeline_job_id,
      UUID,
      "파이프라인 작업 ID",
      36,
    ),
    analysisJobId: string(
      source.analysis_job_id,
      UUID,
      "분석 작업 ID",
      36,
    ),
    approvedIncidentCount: boundedInteger(
      source.approved_incident_count,
      1,
      500,
      "승인 사건 수",
    ),
    bindingManifestSha256,
    exactPlanSha256,
    documentEditPlan,
    documentEditPlanSha256,
    acceptRejectRollbackBindingSha256,
    outputDocumentSha256: string(
      source.output_document_sha256,
      SHA256,
      "미리보기 문서 해시",
      64,
    ),
    renderBindingSha256: string(
      source.render_binding_sha256,
      SHA256,
      "렌더 바인딩 해시",
      64,
    ),
    recordSha256: string(
      source.record_sha256,
      SHA256,
      "미리보기 레코드 해시",
      64,
    ),
    operationCount: boundedInteger(
      source.operation_count,
      1,
      5_000,
      "정확 편집 작업 수",
    ),
    expectedRenderedPageCount: 48,
    approvalToken: string(
      source.approval_token,
      SHA256,
      "COM 적용 승인 토큰",
      64,
    ),
    approvalExpiresAt: source.approval_expires_at,
    idempotentReplay: source.idempotent_replay,
  };
}

export function parseExactReportApplication(
  value: unknown,
): ExactReportApplication {
  const source = record(value, "정확 HWPX 적용");
  exactKeys(
    source,
    [
      "schema_version",
      "application_id",
      "state",
      "project_id",
      "pipeline_job_id",
      "analysis_job_id",
      "draft_id",
      "binding_manifest_sha256",
      "exact_plan_sha256",
      "document_edit_plan_sha256",
      "accept_reject_rollback_binding_sha256",
      "output_file_name",
      "output_sha256",
      "pdf_file_name",
      "pdf_sha256",
      "render_validation_status",
      "rendered_page_count",
      "render_failure_codes",
      "output_reopen_structure_sha256",
      "raster_artifact_manifest_sha256",
      "com_publish_completed",
      "pdf_export_completed",
      "render_validation_completed",
      "rollback_completed",
      "accept_reject_required",
      "accepted",
      "rejected",
      "path_exposed",
    ],
    "정확 HWPX 적용",
  );
  const states = new Set<ExactReportApplicationState>([
    "render_validated_accept_or_reject_required",
    "render_validation_failed_rolled_back",
    "render_validation_failed_rollback_blocked",
    "accepted_render_validated_report",
    "rejected_report_rolled_back",
    "accepted_report_rolled_back",
  ]);
  if (
    source.schema_version !==
      "trafficvisson.current-report-application.v1" ||
    !states.has(source.state as ExactReportApplicationState) ||
    source.path_exposed !== false ||
    typeof source.render_validation_status !== "string" ||
    source.render_validation_status.length < 1 ||
    source.render_validation_status.length > 80 ||
    !Array.isArray(source.render_failure_codes) ||
    source.render_failure_codes.length > 8 ||
    source.render_failure_codes.some(
      (item) =>
        typeof item !== "string" ||
        !/^[a-z][a-z0-9_]{2,79}$/.test(item),
    ) ||
    (source.rendered_page_count !== null &&
      (typeof source.rendered_page_count !== "number" ||
        !Number.isInteger(source.rendered_page_count) ||
        source.rendered_page_count < 0 ||
        source.rendered_page_count > 48)) ||
    typeof source.com_publish_completed !== "boolean" ||
    typeof source.pdf_export_completed !== "boolean" ||
    typeof source.render_validation_completed !== "boolean" ||
    typeof source.rollback_completed !== "boolean" ||
    typeof source.accept_reject_required !== "boolean" ||
    typeof source.accepted !== "boolean" ||
    typeof source.rejected !== "boolean"
  ) {
    throw new Error("정확 HWPX 적용 응답 형식이 올바르지 않습니다.");
  }
  const outputFileName = string(
    source.output_file_name,
    /^[^\\/\u0000-\u001f\u007f]{1,255}$/,
    "HWPX 파일명",
  );
  const pdfFileName = string(
    source.pdf_file_name,
    /^[^\\/\u0000-\u001f\u007f]{1,255}$/,
    "PDF 파일명",
  );
  const outputSha256 = optionalSha(source.output_sha256, "HWPX 해시");
  const pdfSha256 = optionalSha(source.pdf_sha256, "PDF 해시");
  const outputReopenStructureSha256 = optionalSha(
    source.output_reopen_structure_sha256,
    "재열기 구조 해시",
  );
  const rasterArtifactManifestSha256 = optionalSha(
    source.raster_artifact_manifest_sha256,
    "48쪽 래스터 명세 해시",
  );
  const bindingManifestSha256 = string(
    source.binding_manifest_sha256,
    SHA256,
    "바인딩 명세 해시",
    64,
  );
  const exactPlanSha256 = string(
    source.exact_plan_sha256,
    SHA256,
    "정확 계획 해시",
    64,
  );
  const documentEditPlanSha256 = string(
    source.document_edit_plan_sha256,
    SHA256,
    "DocumentEditPlan 해시",
    64,
  );
  const acceptRejectRollbackBindingSha256 = string(
    source.accept_reject_rollback_binding_sha256,
    SHA256,
    "수락·거절·롤백 바인딩 해시",
    64,
  );
  const passedAndComplete =
    source.render_validation_status === "validated_48_page_render" &&
    source.rendered_page_count === 48 &&
    source.com_publish_completed === true &&
    source.pdf_export_completed === true &&
    source.render_validation_completed === true &&
    outputSha256 !== null &&
    pdfSha256 !== null &&
    outputReopenStructureSha256 !== null &&
    rasterArtifactManifestSha256 !== null;
  const state = source.state as ExactReportApplicationState;
  const stateInvariant =
    state === "render_validated_accept_or_reject_required"
      ? passedAndComplete &&
        source.rollback_completed === false &&
        source.accept_reject_required === true &&
        source.accepted === false &&
        source.rejected === false
      : state === "accepted_render_validated_report"
        ? passedAndComplete &&
          source.rollback_completed === false &&
          source.accept_reject_required === false &&
          source.accepted === true &&
          source.rejected === false
        : state === "rejected_report_rolled_back"
          ? passedAndComplete &&
            source.rollback_completed === true &&
            source.accept_reject_required === false &&
            source.accepted === false &&
            source.rejected === true
          : state === "accepted_report_rolled_back"
            ? passedAndComplete &&
              source.rollback_completed === true &&
              source.accept_reject_required === false &&
              source.accepted === true &&
              source.rejected === false
            : source.render_validation_status === "render_validation_failed" &&
              source.render_validation_completed === true &&
              source.accept_reject_required === false &&
              source.accepted === false &&
              source.rejected === true &&
              source.rollback_completed ===
                (state === "render_validation_failed_rolled_back");
  if (!stateInvariant) {
    throw new Error(
      "정확 HWPX 적용 상태와 48쪽 검증 증거가 일치하지 않습니다.",
    );
  }
  return {
    schemaVersion: source.schema_version,
    applicationId: string(
      source.application_id,
      APPLICATION_ID,
      "적용 ID",
      59,
    ),
    state,
    projectId: string(source.project_id, UUID, "프로젝트 ID", 36),
    pipelineJobId: string(
      source.pipeline_job_id,
      UUID,
      "파이프라인 작업 ID",
      36,
    ),
    analysisJobId: string(
      source.analysis_job_id,
      UUID,
      "분석 작업 ID",
      36,
    ),
    draftId: string(source.draft_id, DRAFT_ID, "초안 ID", 47),
    bindingManifestSha256,
    exactPlanSha256,
    documentEditPlanSha256,
    acceptRejectRollbackBindingSha256,
    outputFileName,
    outputSha256,
    pdfFileName,
    pdfSha256,
    renderValidationStatus: source.render_validation_status,
    renderedPageCount: source.rendered_page_count as number | null,
    renderFailureCodes: [...source.render_failure_codes] as string[],
    outputReopenStructureSha256,
    rasterArtifactManifestSha256,
    comPublishCompleted: source.com_publish_completed,
    pdfExportCompleted: source.pdf_export_completed,
    renderValidationCompleted: source.render_validation_completed,
    rollbackCompleted: source.rollback_completed,
    acceptRejectRequired: source.accept_reject_required,
    accepted: source.accepted,
    rejected: source.rejected,
  };
}
