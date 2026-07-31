import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Check,
  ChevronLeft,
  ChevronRight,
  FileCheck2,
  FileText,
  FolderOpen,
  LoaderCircle,
  LockKeyhole,
  LogIn,
  Maximize2,
  Minus,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  X,
} from "lucide-react";
import { buildCandidateEvidenceUrl } from "../evidence";
import {
  Badge,
  Button,
  GlassPanel,
  IconButton,
  PanelHeader,
  StatusDot,
} from "../components/ui";
import { reviewStorageKey, useReviewHistory } from "../reviewRecords";
import { useRuntimeState } from "../runtimeState";
import {
  prepareReportSessionScope,
  useReportSessionField,
  type DocumentEditOperation,
  type DocumentEditPlan,
  type GeneratedDraft,
  type HwpApplyResult,
  type HwpPreviewResult,
  type HwpSampleTargetStructureBlockingReason,
  type HwpWorkflow,
  type OpState,
  type ProviderId,
  type ReportGenerationResult,
} from "../reportSessionState";
import { parseActivePersistentIncidents } from "../persistentIncidents";
import { usePipelineUi } from "../pipelineJobs";
import {
  exactReportContextFromPipeline,
  parseExactReportApplication,
  parseExactReportPreview,
  type ExactReportApplication,
  type ExactReportPreview,
} from "../exactReportWorkflow";
import { ReportDeidentifiedEvidenceReview } from "../components/ReportDeidentifiedEvidenceReview";
import { isReportDeidentifiedReviewInterruption } from "../reportDeidentifiedEvidence";

type ProviderState = "not_connected" | "ready" | "unavailable" | "coming_soon";

type ProviderConnection = {
  provider: ProviderId;
  display_name: string;
  state: ProviderState;
  authentication_owner: "official_cli" | "not_applicable";
  capabilities: {
    structured_judgment: boolean;
    bulk_frame_detection: false;
    report_draft: boolean;
    document_edit_plan: boolean;
    usage_information: boolean;
  };
  message: string;
  diagnostic_state:
    | "not_installed"
    | "installed"
    | "not_connected"
    | "ready"
    | "expired"
    | "network_unavailable"
    | "error"
    | "coming_soon";
  installation_state: "installed" | "not_installed" | "disabled";
  authentication_state:
    | "ready"
    | "signed_out"
    | "expired"
    | "unknown"
    | "not_applicable";
  network_state: "not_checked" | "offline" | "unknown" | "not_applicable";
  version?: string;
};

type HwpPreflight = {
  available: boolean;
  prog_id_registered: boolean;
  security_module_registered: boolean;
  message: string;
};

type ExactReportWorkflowState =
  | "idle"
  | "preparing"
  | "ready"
  | "applying"
  | "awaiting_decision"
  | "accepted"
  | "rejected"
  | "rolled_back"
  | "validation_failed"
  | "error";

const providerOrder: ProviderId[] = ["codex", "claude", "local"];
const DOCUMENT_SKETCH_WIDTH = 680;
const DOCUMENT_ZOOM_MIN = 40;
const DOCUMENT_ZOOM_MAX = 150;

export function calculateDocumentFitZoom(
  viewportWidth: number,
  horizontalPadding: number,
) {
  const usableWidth = Math.max(0, viewportWidth - horizontalPadding);
  const fitZoom = Math.floor((usableWidth / DOCUMENT_SKETCH_WIDTH) * 100);
  return Math.max(
    DOCUMENT_ZOOM_MIN,
    Math.min(DOCUMENT_ZOOM_MAX, fitZoom),
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseProviders(value: unknown): ProviderConnection[] {
  if (!isRecord(value) || !Array.isArray(value.providers)) {
    throw new Error("Provider 상태 응답 형식이 올바르지 않습니다.");
  }
  return value.providers.map((row) => {
    if (
      !isRecord(row) ||
      !isRecord(row.capabilities) ||
      !(
      providerOrder.includes(row.provider as ProviderId) &&
      ["not_connected", "ready", "unavailable", "coming_soon"].includes(
        String(row.state),
      ) &&
      typeof row.display_name === "string" &&
      typeof row.message === "string" &&
      typeof row.capabilities.structured_judgment === "boolean" &&
      row.capabilities.bulk_frame_detection === false &&
      typeof row.capabilities.report_draft === "boolean" &&
      typeof row.capabilities.document_edit_plan === "boolean" &&
      typeof row.capabilities.usage_information === "boolean" &&
      [
        "not_installed",
        "installed",
        "not_connected",
        "ready",
        "expired",
        "network_unavailable",
        "error",
        "coming_soon",
      ].includes(String(row.diagnostic_state)) &&
      ["installed", "not_installed", "disabled"].includes(
        String(row.installation_state),
      ) &&
      [
        "ready",
        "signed_out",
        "expired",
        "unknown",
        "not_applicable",
      ].includes(String(row.authentication_state)) &&
      ["not_checked", "offline", "unknown", "not_applicable"].includes(
        String(row.network_state),
      ) &&
      (row.version === undefined ||
        (typeof row.version === "string" &&
          /^[vV]?[0-9][0-9A-Za-z.+-]{0,63}$/.test(row.version)))
      )
    ) {
      throw new Error("Provider 상태 응답 형식이 올바르지 않습니다.");
    }
    return row as ProviderConnection;
  });
}

function providerDiagnosticSummary(item: ProviderConnection | undefined) {
  if (!item) return "상태 확인 전";
  const installation =
    item.installation_state === "installed"
      ? "설치됨"
      : item.installation_state === "disabled"
        ? "비활성"
        : "미설치";
  const authentication = {
    ready: "로그인 정상",
    signed_out: "로그인 필요",
    expired: "로그인 만료",
    unknown: "인증 미확인",
    not_applicable: "인증 해당 없음",
  }[item.authentication_state];
  const network = {
    not_checked: "네트워크 미검사",
    offline: "네트워크 끊김",
    unknown: "네트워크 미확인",
    not_applicable: "네트워크 해당 없음",
  }[item.network_state];
  return [
    item.version ? `v${item.version.replace(/^[vV]/, "")}` : "버전 미확인",
    installation,
    authentication,
    network,
  ].join(" · ");
}

function parsePreflight(value: unknown): HwpPreflight {
  if (
    !isRecord(value) ||
    typeof value.available !== "boolean" ||
    typeof value.prog_id_registered !== "boolean" ||
    typeof value.security_module_registered !== "boolean" ||
    typeof value.message !== "string"
  ) {
    throw new Error("한/글 COM 사전점검 응답 형식이 올바르지 않습니다.");
  }
  return value as HwpPreflight;
}

const SAMPLE_PREVIEW_BLOCKERS: readonly HwpSampleTargetStructureBlockingReason[] = [
  "exact_hwpml_target_writer_not_implemented",
  "output_reopen_and_48_page_render_validation_not_implemented",
];

const SAMPLE_PREVIEW_BLOCKER_MESSAGES: Record<
  HwpSampleTargetStructureBlockingReason,
  string
> = {
  exact_hwpml_target_writer_not_implemented:
    "샘플 양식의 정확한 표·사진 슬롯을 쓰는 결정적 HWPML 작성기가 아직 연결되지 않았습니다.",
  output_reopen_and_48_page_render_validation_not_implemented:
    "저장 후 문서를 다시 열어 48쪽 전체를 렌더링·검증하는 단계가 아직 연결되지 않았습니다.",
};

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isBoundedInteger(
  value: unknown,
  minimum: number,
  maximum: number,
): value is number {
  return (
    typeof value === "number" &&
    Number.isInteger(value) &&
    value >= minimum &&
    value <= maximum
  );
}

function isCountMap(value: unknown): value is Record<string, number> {
  return (
    isRecord(value) &&
    Object.values(value).every((count) => isNonNegativeInteger(count))
  );
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isBoundedSortedUniqueIntegers(
  value: unknown,
  minimum: number,
  maximum: number,
): value is number[] {
  if (
    !Array.isArray(value) ||
    value.length < 1 ||
    !value.every((item) => isBoundedInteger(item, minimum, maximum))
  ) {
    return false;
  }
  return value.every(
    (item, index) => index === 0 || item > (value[index - 1] as number),
  );
}

function parseLegacyPreview(value: Record<string, unknown>): HwpPreviewResult {
  if (
    value.mode !== "preview" ||
    !isBoundedInteger(value.operation_count, 1, 500) ||
    !isCountMap(value.operation_types) ||
    !isRecord(value.structure) ||
    !isNonNegativeInteger(value.structure.observed_characters) ||
    typeof value.structure.truncated !== "boolean" ||
    !isNonNegativeInteger(value.structure.paragraph_count) ||
    !isNonNegativeInteger(value.structure.table_count) ||
    !isNonNegativeInteger(value.structure.picture_count) ||
    !isStringArray(value.structure.field_names) ||
    !isStringArray(value.structure.bookmark_names) ||
    !isSha256(value.approval_token) ||
    typeof value.approval_expires_at !== "string" ||
    Number.isNaN(Date.parse(value.approval_expires_at)) ||
    !isSha256(value.document_sha256) ||
    !isSha256(value.plan_sha256) ||
    !isRecord(value.target_summary) ||
    !isBoundedInteger(value.target_summary.operation_count, 1, 100) ||
    value.target_summary.operation_count !== value.operation_count ||
    !isCountMap(value.target_summary.operation_types) ||
    !isStringArray(value.target_summary.targets) ||
    value.target_summary.targets.length < 1 ||
    typeof value.message !== "string"
  ) {
    throw new Error("한/글 구조 미리보기 응답을 검증하지 못했습니다.");
  }
  return value as HwpPreviewResult;
}

function parseSampleTargetStructurePreview(
  value: Record<string, unknown>,
): HwpPreviewResult {
  const bindings = value.operation_bindings;
  const blockers = value.blocking_reasons;
  if (
    value.schema_version !==
      "trafficvisson.hwpx-sample-target-plan.v1" ||
    value.mode !== "sample_target_structure_preview" ||
    value.state !==
      "target_structure_preview_validated_apply_unavailable" ||
    typeof value.document_snapshot_id !== "string" ||
    !/^hwpx-sample-[34]q-[a-f0-9]{24}$/.test(value.document_snapshot_id) ||
    !isSha256(value.document_sha256) ||
    !isSha256(value.structure_target_summary_sha256) ||
    !isSha256(value.weak_case_index_manifest_sha256) ||
    !isSha256(value.plan_sha256) ||
    !isSha256(value.validation_sha256) ||
    !isSha256(value.approved_references_sha256) ||
    !isBoundedInteger(value.approved_reference_count, 1, 100) ||
    !isBoundedInteger(value.operation_count, 1, 100) ||
    !isBoundedSortedUniqueIntegers(value.page_pair_indices, 0, 23) ||
    !isBoundedSortedUniqueIntegers(value.touched_pages, 1, 48) ||
    value.expected_rendered_page_count !== 48 ||
    !Array.isArray(value.expected_page_range) ||
    value.expected_page_range.length !== 2 ||
    value.expected_page_range[0] !== 1 ||
    value.expected_page_range[1] !== 48 ||
    value.page_range_preserved_by_structure !== true ||
    value.table_growth_allowed !== false ||
    value.target_structure_validation_passed !== true ||
    value.approved_incident_bound !== false ||
    value.content_provenance_bound !== false ||
    value.com_invocation_performed !== false ||
    !Array.isArray(bindings) ||
    bindings.length !== value.operation_count ||
    bindings.some(
      (binding) =>
        !isRecord(binding) ||
        typeof binding.operation_id !== "string" ||
        typeof binding.approval_id !== "string" ||
        !/^approved-reference-[a-f0-9]{24}$/.test(binding.approval_id) ||
        !["fill_cell", "insert_image"].includes(
          String(binding.operation_type),
        ) ||
        typeof binding.target_id !== "string" ||
        !/^hwpx-(?:cell|picture)-[a-f0-9]{24}$/.test(binding.target_id) ||
        ![
          "photo_slot_image",
          "photo_caption",
          "inspection_route",
          "inspection_opinion_body",
          "evaluation_square_marker",
        ].includes(String(binding.semantic_role)) ||
        !isBoundedInteger(binding.page_number, 1, 48) ||
        !isBoundedInteger(binding.table_index, 0, 71) ||
        !isNonNegativeInteger(binding.row) ||
        !isNonNegativeInteger(binding.column),
    ) ||
    value.approval_scope !==
      "sample_target_structure_hash_binding_only" ||
    !isSha256(value.approval_token) ||
    typeof value.approval_expires_at !== "string" ||
    Number.isNaN(Date.parse(value.approval_expires_at)) ||
    value.apply_eligible !== false ||
    value.writer_state !== "unavailable" ||
    value.output_render_validation_status !== "not_run" ||
    !Array.isArray(blockers) ||
    blockers.length !== SAMPLE_PREVIEW_BLOCKERS.length ||
    blockers.some(
      (blocker, index) => blocker !== SAMPLE_PREVIEW_BLOCKERS[index],
    ) ||
    value.no_com !== true ||
    value.no_document_write !== true
  ) {
    throw new Error(
      "샘플 양식 대상·구조 미리보기 응답을 검증하지 못했습니다.",
    );
  }
  return value as HwpPreviewResult;
}

export function parseHwpPreviewResult(value: unknown): HwpPreviewResult {
  if (!isRecord(value)) {
    throw new Error("한/글 구조 미리보기 응답을 검증하지 못했습니다.");
  }
  if (value.mode === "preview") return parseLegacyPreview(value);
  if (value.mode === "sample_target_structure_preview") {
    return parseSampleTargetStructurePreview(value);
  }
  throw new Error("지원되지 않는 한/글 미리보기 응답입니다.");
}

export function isHwpPreviewApplyEligible(
  value: HwpPreviewResult | null,
): boolean {
  return value?.mode === "preview";
}

export function hwpPreviewBlockerMessages(
  value: HwpPreviewResult | null,
): string[] {
  return value?.mode === "sample_target_structure_preview"
    ? value.blocking_reasons.map(
        (reason) => SAMPLE_PREVIEW_BLOCKER_MESSAGES[reason],
      )
    : [];
}

function previewWorkflowMessage(value: HwpPreviewResult): string {
  if (value.mode === "preview") return value.message;
  return [
    `샘플 양식의 편집 대상 ${value.operation_count}건과 1~48쪽 원본 구조를 검증했습니다.`,
    "승인 사건 내용과 사진 근거는 아직 이 미리보기에 결합되지 않았습니다.",
    ...hwpPreviewBlockerMessages(value),
    "한/글 COM 호출과 문서 쓰기는 수행하지 않았습니다.",
  ].join(" ");
}

function parseApply(value: unknown): HwpApplyResult {
  if (
    !isRecord(value) ||
    value.mode !== "applied" ||
    typeof value.output_file_name !== "string" ||
    typeof value.size_bytes !== "number" ||
    typeof value.sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(value.sha256) ||
    typeof value.rollback_token !== "string" ||
    !/^[a-f0-9]{32}$/.test(value.rollback_token) ||
    !isRecord(value.readback) ||
    typeof value.message !== "string"
  ) {
    throw new Error("한/글 저장·재검증 완료 응답을 검증하지 못했습니다.");
  }
  return value as HwpApplyResult;
}

function resemblesLocalFilePath(value: string) {
  return (
    /^[A-Za-z]:[\\/]/.test(value) ||
    /^\\\\/.test(value) ||
    /^file:\/\//i.test(value) ||
    /^\/(?:Users|home|mnt|tmp)\//i.test(value)
  );
}

export function parseReportGenerationResult(
  value: unknown,
): ReportGenerationResult {
  if (
    !isRecord(value) ||
    typeof value.generation_id !== "string" ||
    !["codex", "claude"].includes(String(value.provider)) ||
    value.state !== "generated_preview_required" ||
    !isRecord(value.draft) ||
    typeof value.draft.title !== "string" ||
    !Array.isArray(value.draft.sections) ||
    !isRecord(value.plan) ||
    !Array.isArray(value.plan.operations) ||
    !Array.isArray(value.transmitted_fields)
  ) {
    throw new Error("보고서 생성 응답 형식을 검증하지 못했습니다.");
  }
  const sections = value.draft.sections.map((raw) => {
    if (
      !isRecord(raw) ||
      typeof raw.heading !== "string" ||
      typeof raw.body !== "string" ||
      !Array.isArray(raw.incident_ids)
    ) {
      throw new Error("보고서 초안 섹션 형식을 검증하지 못했습니다.");
    }
    return {
      heading: raw.heading,
      body: raw.body,
      incidentIds: raw.incident_ids.map(String),
    };
  });
  const rawPlan = value.plan;
  const rawOperations = rawPlan.operations;
  if (
    !Array.isArray(rawOperations) ||
    typeof rawPlan.schema_version !== "string" ||
    typeof rawPlan.plan_id !== "string" ||
    typeof rawPlan.document_snapshot_id !== "string"
  ) {
    throw new Error("문서 편집 계획 형식을 검증하지 못했습니다.");
  }
  const operations: DocumentEditOperation[] = rawOperations.map((raw: unknown) => {
    if (
      !isRecord(raw) ||
      typeof raw.operation_id !== "string" ||
      !["fill_text", "fill_cell", "insert_image"].includes(String(raw.type)) ||
      typeof raw.target !== "string" ||
      typeof raw.value !== "string" ||
      typeof raw.source_ref !== "string" ||
      !["preserve", "inherit_target"].includes(String(raw.style_policy)) ||
      typeof raw.preview !== "string"
    ) {
      throw new Error("지원되지 않거나 잘못된 편집 작업이 포함됐습니다.");
    }
    if (
      raw.type === "insert_image" &&
      (resemblesLocalFilePath(raw.value) ||
        resemblesLocalFilePath(raw.source_ref))
    ) {
      throw new Error(
        "이미지 편집 작업에는 로컬 파일 경로를 포함할 수 없습니다.",
      );
    }
    return {
      operationId: raw.operation_id,
      type: raw.type as "fill_text" | "fill_cell" | "insert_image",
      target: raw.target,
      expectedValue:
        typeof raw.expected_value === "string"
          ? raw.expected_value
          : undefined,
      value: raw.value,
      sourceRef: raw.source_ref,
      stylePolicy: raw.style_policy as "preserve" | "inherit_target",
      preview: raw.preview,
    };
  });
  return {
    generationId: value.generation_id,
    provider: value.provider as "codex" | "claude",
    state: "generated_preview_required",
    draft: { title: value.draft.title, sections },
    plan: {
      schemaVersion: "1.0",
      planId: rawPlan.plan_id,
      documentSnapshotId: rawPlan.document_snapshot_id,
      operations,
    },
    model: typeof value.model === "string" ? value.model : undefined,
    usage: isRecord(value.usage)
      ? {
          inputTokens:
            typeof value.usage.input_tokens === "number"
              ? value.usage.input_tokens
              : undefined,
          outputTokens:
            typeof value.usage.output_tokens === "number"
              ? value.usage.output_tokens
              : undefined,
        }
      : undefined,
    transmittedFields: value.transmitted_fields.map(String),
  };
}

export function reportOperationDisplayValue(
  operation: DocumentEditOperation,
): string {
  if (operation.type === "insert_image") {
    return "승인 사건의 비식별화 근거 이미지를 지정된 사진 슬롯에 배치합니다.";
  }
  return operation.value;
}

function messageFrom(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function providerStatusLabel(state: ProviderState | undefined) {
  if (state === "ready") return "연결됨";
  if (state === "not_connected") return "로그인 필요";
  if (state === "coming_soon") return "준비 중";
  if (state === "unavailable") return "사용 불가";
  return "확인 중";
}

function providerTone(
  state: ProviderState | undefined,
): "success" | "warning" | "danger" | "neutral" {
  if (state === "ready") return "success";
  if (state === "not_connected") return "warning";
  if (state === "unavailable") return "danger";
  return "neutral";
}

function formatBytes(value: number) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)} MB`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)} KB`;
  return `${value.toLocaleString("ko-KR")} B`;
}

export function isCurrentReportOperation(
  requestOperationId: number,
  activeOperationId: number,
  requestScopeKey: string | null,
  activeScopeKey: string | null,
) {
  return (
    requestOperationId === activeOperationId &&
    requestScopeKey === activeScopeKey
  );
}

export function ReportScreen() {
  const runtime = useRuntimeState();
  const pipeline = usePipelineUi();
  const reportScopeKey =
    runtime.analysisSummary &&
    runtime.activeImport?.projectId === runtime.analysisSummary.projectId
    ? `${runtime.analysisSummary.projectId}:${runtime.analysisSummary.jobId}`
    : null;
  prepareReportSessionScope(reportScopeKey);
  const latestReportScopeKeyRef = useRef(reportScopeKey);
  const activeReportOperationRef = useRef(0);
  const exactPreviewRequestRef = useRef<{
    key: string;
    promise: Promise<unknown>;
  } | null>(null);
  if (latestReportScopeKeyRef.current !== reportScopeKey) {
    // Increment synchronously during the scope transition.  A request from A
    // must stay stale even if the user later returns from B to A before that
    // request resolves.
    latestReportScopeKeyRef.current = reportScopeKey;
    activeReportOperationRef.current += 1;
  }
  const [provider, setProvider] = useReportSessionField("provider");
  const [providers, setProviders] = useState<ProviderConnection[]>([]);
  const [providerLoading, setProviderLoading] = useState(true);
  const [providerAction, setProviderAction] = useState<ProviderId | null>(null);
  const [providerMessage, setProviderMessage] = useState<string | null>(null);
  const [providerError, setProviderError] = useState<string | null>(null);
  const [consent, setConsent] = useReportSessionField("consent");
  const [states, setStates] = useReportSessionField("operationStates");
  const [preflight, setPreflight] = useState<HwpPreflight | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(true);
  const [documentSelection, setDocumentSelection] =
    useReportSessionField("documentSelection");
  const [workflow, setWorkflow] = useReportSessionField("workflow");
  const [workflowMessage, setWorkflowMessage] =
    useReportSessionField("workflowMessage");
  const [previewResult, setPreviewResult] =
    useReportSessionField("previewResult");
  const [previewedPlan, setPreviewedPlan] =
    useReportSessionField("previewedPlan");
  const [applyResult, setApplyResult] =
    useReportSessionField("applyResult");
  const [generation, setGeneration] = useReportSessionField("generation");
  const [generationLoading, setGenerationLoading] = useState(false);
  const [generationMessage, setGenerationMessage] =
    useReportSessionField("generationMessage");
  const documentStageRef = useRef<HTMLDivElement>(null);
  const [documentZoom, setDocumentZoom] = useState(100);
  const [documentZoomMode, setDocumentZoomMode] =
    useState<"fit-width" | "manual">("fit-width");
  const [exactPreview, setExactPreview] =
    useState<ExactReportPreview | null>(null);
  const [exactApplication, setExactApplication] =
    useState<ExactReportApplication | null>(null);
  const [exactWorkflow, setExactWorkflow] =
    useState<ExactReportWorkflowState>("idle");
  const [exactWorkflowMessage, setExactWorkflowMessage] =
    useState<string | null>(null);
  const [exactApprovalConsumed, setExactApprovalConsumed] =
    useState(false);
  const [exactPreviewRevision, setExactPreviewRevision] = useState(0);

  const selectedProvider = providers.find((item) => item.provider === provider);
  const reviewSummary = runtime.analysisSummary;
  const reportDeidentifiedReviewJob = useMemo(() => {
    const job =
      pipeline.projectId === reviewSummary?.projectId
        ? pipeline.job
        : null;
    return isReportDeidentifiedReviewInterruption(job)
      ? job
      : null;
  }, [
    pipeline.job,
    pipeline.projectId,
    reviewSummary?.projectId,
  ]);
  const reportPipeline = useMemo(() => {
    const job =
      pipeline.projectId === reviewSummary?.projectId ? pipeline.job : null;
    const profileSha = job?.provenance.profile_sha256;
    const sourceSha = job?.provenance.source_sha256;
    if (
      !job ||
      job.status !== "completed" ||
      runtime.activeImport?.snapshotId !== job.snapshotId ||
      sourceSha !== reviewSummary?.sourceSha256 ||
      typeof profileSha !== "string" ||
      !/^[a-f0-9]{64}$/.test(profileSha)
    ) {
      return null;
    }
    return {
      pipelineJobId: job.jobId,
      pipelineInputFingerprint: job.inputFingerprint,
      pipelineProfileSha256: profileSha,
    };
  }, [
    pipeline.job,
    pipeline.projectId,
    reviewSummary?.projectId,
    reviewSummary?.sourceSha256,
    runtime.activeImport?.snapshotId,
  ]);
  const exactReportContext = useMemo(() => {
    if (!reportPipeline) return null;
    const context = exactReportContextFromPipeline(pipeline.job);
    if (
      !context ||
      context.pipelineJobId !== reportPipeline.pipelineJobId ||
      context.projectId !== reviewSummary?.projectId
    ) {
      return null;
    }
    return context;
  }, [
    pipeline.job,
    reportPipeline,
    reviewSummary?.projectId,
  ]);
  const exactReportScopeKey = exactReportContext
    ? `${exactReportContext.projectId}:${exactReportContext.pipelineJobId}:${exactReportContext.previewId}`
    : null;
  const reviewRows = useReviewHistory(
    reviewSummary
      ? reviewStorageKey(reviewSummary.projectId, reviewSummary.jobId)
      : "trafficvisson:review:no-active-analysis",
  );
  const approvedReviews = useMemo(
    () =>
      reviewRows.filter(
        (item) =>
          item.trainingStatus === "approved" &&
          item.selectedType !== "UNCLASSIFIED_ROAD_EVENT",
      ),
    [reviewRows],
  );
  const displayOperations = useMemo(
    () =>
      generation
        ? generation.plan.operations.map((operation) => ({
            id: operation.operationId,
            op:
              operation.type === "fill_cell"
                ? "표 셀 채우기"
                : operation.type === "insert_image"
                  ? "근거 이미지 배치"
                  : "문단 채우기",
            target: operation.target,
            value: reportOperationDisplayValue(operation),
            operation,
            supported: true,
          }))
        : [],
    [generation],
  );
  const approvedTypeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const review of approvedReviews) {
      counts.set(
        review.selectedType,
        (counts.get(review.selectedType) ?? 0) + 1,
      );
    }
    return [...counts.entries()];
  }, [approvedReviews]);
  const approvedEvidenceImages = useMemo(() => {
    const summary = runtime.analysisSummary;
    if (!summary) return [];
    return approvedReviews.flatMap((review) => {
      const candidateId = review.questionId.split(":").at(-1);
      const candidate = summary.candidates.find(
        (item) => item.candidateId === candidateId,
      );
      if (!candidate) return [];
      return [
        {
          candidateId: candidate.candidateId,
          timeSeconds: candidate.representativeTimestampSeconds,
          src: buildCandidateEvidenceUrl(
            summary.projectId,
            summary.jobId,
            candidate.candidateId,
            "image",
          ),
        },
      ];
    }).slice(0, 2);
  }, [approvedReviews, runtime.analysisSummary]);
  const accepted = useMemo(
    () => Object.values(states).filter((state) => state === "accepted").length,
    [states],
  );
  const pending = Object.values(states).filter((state) => state === "pending").length;
  const busy =
    generationLoading ||
    ["previewing", "applying", "rolling_back"].includes(workflow);
  const exactBusy =
    exactWorkflow === "preparing" || exactWorkflow === "applying";

  function readFitZoom() {
    const stage = documentStageRef.current;
    if (!stage) return 100;
    const style = window.getComputedStyle(stage);
    const horizontalPadding =
      Number.parseFloat(style.paddingLeft) +
      Number.parseFloat(style.paddingRight);
    return calculateDocumentFitZoom(stage.clientWidth, horizontalPadding);
  }

  function fitDocumentWidth() {
    setDocumentZoomMode("fit-width");
    setDocumentZoom(readFitZoom());
  }

  function changeDocumentZoom(delta: number) {
    setDocumentZoomMode("manual");
    setDocumentZoom((current) =>
      Math.max(
        DOCUMENT_ZOOM_MIN,
        Math.min(DOCUMENT_ZOOM_MAX, current + delta),
      ),
    );
  }

  function beginReportOperation() {
    const requestOperationId = activeReportOperationRef.current + 1;
    activeReportOperationRef.current = requestOperationId;
    return {
      requestOperationId,
      requestScopeKey: reportScopeKey,
    };
  }

  function reportOperationIsCurrent(request: {
    requestOperationId: number;
    requestScopeKey: string | null;
  }) {
    return isCurrentReportOperation(
      request.requestOperationId,
      activeReportOperationRef.current,
      request.requestScopeKey,
      latestReportScopeKeyRef.current,
    );
  }

  async function refreshProviders() {
    setProviderLoading(true);
    setProviderError(null);
    try {
      if (!window.trafficSuperVision) {
        throw new Error("Provider 확인은 설치형 Electron 앱에서 사용할 수 있습니다.");
      }
      const next = parseProviders(await window.trafficSuperVision.providers.list());
      setProviders(next);
      const currentProvider = next.find((item) => item.provider === provider);
      const firstReady = next.find((item) => item.state === "ready");
      if (
        !currentProvider ||
        currentProvider.state === "coming_soon" ||
        (currentProvider.state !== "ready" && firstReady)
      ) {
        if (firstReady) setProvider(firstReady.provider);
      }
    } catch (error) {
      setProviders([]);
      setProviderError(messageFrom(error, "Provider 상태를 확인하지 못했습니다."));
    } finally {
      setProviderLoading(false);
    }
  }

  async function refreshPreflight() {
    setPreflightLoading(true);
    try {
      if (!window.trafficSuperVision) {
        throw new Error("한/글 COM 확인은 설치형 Electron 앱에서 사용할 수 있습니다.");
      }
      setPreflight(parsePreflight(await window.trafficSuperVision.hwp.preflight()));
    } catch (error) {
      setPreflight({
        available: false,
        prog_id_registered: false,
        security_module_registered: false,
        message: messageFrom(error, "한/글 COM 상태를 확인하지 못했습니다."),
      });
    } finally {
      setPreflightLoading(false);
    }
  }

  useEffect(() => {
    void refreshProviders();
    void refreshPreflight();
    // Provider and COM state are intentionally checked, never inferred.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    return () => {
      // Navigation unmounts this screen without necessarily changing the
      // analysis scope.  Invalidate its outstanding callbacks as well.
      activeReportOperationRef.current += 1;
    };
  }, []);

  useEffect(() => {
    // Local loading indicators are not stored in the scoped report session.
    // Reset them as soon as another project/job becomes active.
    setGenerationLoading(false);
    setProviderAction(null);
  }, [reportScopeKey]);

  useEffect(() => {
    let cancelled = false;
    setExactPreview(null);
    setExactApplication(null);
    setExactApprovalConsumed(false);
    setExactWorkflowMessage(null);
    if (!exactReportContext || !exactReportScopeKey) {
      setExactWorkflow("idle");
      return () => {
        cancelled = true;
      };
    }
    const trafficApi = window.trafficSuperVision;
    if (!trafficApi) {
      setExactWorkflow("error");
      setExactWorkflowMessage(
        "정확 HWPX 미리보기는 설치형 Electron 앱에서 준비됩니다.",
      );
      return () => {
        cancelled = true;
      };
    }
    setExactWorkflow("preparing");
    const requestKey = `${exactReportScopeKey}:${exactPreviewRevision}`;
    if (exactPreviewRequestRef.current?.key !== requestKey) {
      exactPreviewRequestRef.current = {
        key: requestKey,
        promise: trafficApi.reports.prepareExactPreview({
          projectId: exactReportContext.projectId,
          pipelineJobId: exactReportContext.pipelineJobId,
          previewId: exactReportContext.previewId,
        }),
      };
    }
    const previewRequest = exactPreviewRequestRef.current.promise;
    void (async () => {
      try {
        const result = parseExactReportPreview(
          await previewRequest,
        );
        if (
          cancelled ||
          result.projectId !== exactReportContext.projectId ||
          result.pipelineJobId !== exactReportContext.pipelineJobId ||
          result.operationCount !== exactReportContext.operationCount
        ) {
          if (!cancelled) {
            throw new Error(
              "현재 PipelineJob과 HWPX 미리보기 provenance가 일치하지 않습니다.",
            );
          }
          return;
        }
        setExactPreview(result);
        setExactWorkflow("ready");
        setExactWorkflowMessage(
          `사용자 승인 사건 ${result.approvedIncidentCount}건을 샘플 양식의 정확한 위치에 결속했습니다. COM 적용 전에는 최종 보고서가 아닙니다.`,
        );
      } catch (error) {
        if (cancelled) return;
        setExactWorkflow("error");
        setExactWorkflowMessage(
          messageFrom(
            error,
            "현재 PipelineJob의 정확 HWPX 미리보기를 준비하지 못했습니다.",
          ),
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [exactPreviewRevision, exactReportScopeKey]);

  useEffect(() => {
    if (documentZoomMode !== "fit-width") return;
    const stage = documentStageRef.current;
    if (!stage) return;

    const updateFitZoom = () => {
      const style = window.getComputedStyle(stage);
      const horizontalPadding =
        Number.parseFloat(style.paddingLeft) +
        Number.parseFloat(style.paddingRight);
      setDocumentZoom(
        calculateDocumentFitZoom(stage.clientWidth, horizontalPadding),
      );
    };

    updateFitZoom();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(updateFitZoom);
    observer.observe(stage);
    return () => observer.disconnect();
  }, [documentZoomMode]);

  function invalidatePreview(message?: string) {
    setPreviewResult(null);
    setPreviewedPlan(null);
    setApplyResult(null);
    setWorkflow("idle");
    setWorkflowMessage(message ?? null);
  }

  function updateOperation(id: string, state: OpState) {
    setStates((current) => ({ ...current, [id]: state }));
    invalidatePreview("편집 계획이 변경되었습니다. 한/글 구조 미리보기를 다시 실행하세요.");
  }

  function setAll(state: OpState) {
    setStates(
      Object.fromEntries(
        displayOperations.map((op) => [
          op.id,
          state === "accepted" && !op.supported ? "pending" : state,
        ]),
      ),
    );
    invalidatePreview(
      state === "accepted"
        ? "현재 COM 편집기가 지원하는 문단·필드 작업만 수락했습니다."
        : "편집 계획을 대기 상태로 초기화했습니다.",
    );
  }

  async function launchLogin() {
    if (provider === "local" || !window.trafficSuperVision) return;
    setProviderAction(provider);
    setProviderError(null);
    setProviderMessage(null);
    try {
      const result = await window.trafficSuperVision.providers.login({ provider });
      if (!isRecord(result) || result.state !== "external_cli_owned") {
        throw new Error("공식 CLI 로그인 실행 응답을 확인하지 못했습니다.");
      }
      setProviderMessage(
        typeof result.message === "string"
          ? result.message
          : "공식 CLI 로그인 창을 열었습니다. 완료 후 상태를 새로 확인하세요.",
      );
    } catch (error) {
      setProviderError(messageFrom(error, "공식 CLI 로그인 흐름을 시작하지 못했습니다."));
    } finally {
      setProviderAction(null);
    }
  }

  async function selectDocument() {
    if (!window.trafficSuperVision) {
      setWorkflow("error");
      setWorkflowMessage("문서 선택은 설치형 Electron 앱에서 사용할 수 있습니다.");
      return;
    }
    const request = beginReportOperation();
    try {
      const selection = await window.trafficSuperVision.dialogs.selectHwpDocument();
      if (!reportOperationIsCurrent(request)) return;
      if (!selection.selectionId || !selection.file) return;
      setDocumentSelection(selection);
      setGeneration(null);
      setGenerationMessage(null);
      setStates({});
      invalidatePreview("새 양식을 선택했습니다. 수락 항목의 구조 미리보기를 실행하세요.");
    } catch (error) {
      if (!reportOperationIsCurrent(request)) return;
      setWorkflow("error");
      setWorkflowMessage(messageFrom(error, "HWP/HWPX 양식을 선택하지 못했습니다."));
    }
  }

  function buildPlan(): DocumentEditPlan {
    if (!documentSelection?.selectionId) {
      throw new Error("먼저 HWP/HWPX 양식을 선택하세요.");
    }
    if (!generation) {
      throw new Error("승인 사건으로 보고서 초안과 편집 계획을 먼저 생성하세요.");
    }
    const operations = generation.plan.operations.filter(
      (operation) => states[operation.operationId] === "accepted",
    );
    if (operations.length === 0) {
      throw new Error("미리볼 편집 작업을 한 건 이상 수락하세요.");
    }
    return {
      ...generation.plan,
      operations,
    };
  }

  async function generateReport() {
    const summary = runtime.analysisSummary;
    if (
      !window.trafficSuperVision ||
      !runtime.activeImport ||
      !summary ||
      !reportScopeKey ||
      !documentSelection?.selectionId ||
      provider === "local"
    ) {
      setProviderError(
        "검증된 분석 결과, 승인 라벨, HWP/HWPX 양식이 모두 필요합니다.",
      );
      return;
    }
    setGenerationLoading(true);
    setProviderError(null);
    setGenerationMessage(null);
    const request = beginReportOperation();
    try {
      const persistentIncidents = parseActivePersistentIncidents(
        await window.trafficSuperVision.incidents.list({
          projectId: summary.projectId,
          analysisJobId: summary.jobId,
          includeInactive: false,
        }),
        summary.projectId,
        summary.jobId,
      );
      const approvedCandidateIds = new Set(
        approvedReviews.flatMap((review) => {
          const candidateId =
            review.candidateId ?? review.questionId.split(":").at(-1);
          return candidateId ? [candidateId] : [];
        }),
      );
      const incidentIds = persistentIncidents
        .filter((incident) => approvedCandidateIds.has(incident.candidateId))
        .map((incident) => incident.incidentId);
      if (!incidentIds.length) {
        throw new Error(
          "현재 분석 작업에 서버가 확인한 활성 승인 사건이 없습니다.",
        );
      }
      const result = parseReportGenerationResult(
        await window.trafficSuperVision.reports.generate({
          projectId: summary.projectId,
          analysisJobId: summary.jobId,
          sourceFileSha256: summary.sourceSha256,
          provider,
          consent,
          projectLabel: "도로 영상 점검 프로젝트",
          incidentIds,
          ...(reportPipeline ?? {}),
          documentStructureSummary: {
            documentSnapshotId: `selection:${documentSelection.selectionId}`,
            fieldTargets: ["bookmark:inspection_summary"],
            cellTargets: ["field:inspection_date"],
            tableTargets: [],
            imageTargets: [],
            mapTargets: [],
            sectionHeadings: ["조사 개요", "점검 결과"],
          },
          mapRefs: [],
        }),
      );
      if (!reportOperationIsCurrent(request)) return;
      setGeneration(result);
      setStates(
        Object.fromEntries(
          result.plan.operations.map((operation) => [
            operation.operationId,
            "pending",
          ]),
        ),
      );
      invalidatePreview();
      setGenerationMessage(
        `${result.provider}가 서버 확인 승인 사건 ${incidentIds.length}건으로 초안과 편집 계획 ${result.plan.operations.length}건을 생성했습니다.`,
      );
    } catch (error) {
      if (!reportOperationIsCurrent(request)) return;
      setGeneration(null);
      setProviderError(
        messageFrom(error, "안전한 보고서 초안을 생성하지 못했습니다."),
      );
    } finally {
      if (reportOperationIsCurrent(request)) {
        setGenerationLoading(false);
      }
    }
  }

  async function previewDocument() {
    if (
      !window.trafficSuperVision ||
      !runtime.activeImport ||
      !reportScopeKey ||
      !documentSelection?.selectionId
    ) {
      setWorkflow("error");
      setWorkflowMessage(
        !runtime.activeImport
          ? "먼저 검증된 영상을 프로젝트에 등록하세요."
          : "먼저 HWP/HWPX 양식을 선택하세요.",
      );
      return;
    }
    setWorkflow("previewing");
    setWorkflowMessage(null);
    setApplyResult(null);
    const request = beginReportOperation();
    try {
      const plan = buildPlan();
      const result = parseHwpPreviewResult(
        await window.trafficSuperVision.hwp.preview({
          projectId: runtime.activeImport.projectId,
          documentSelectionId: documentSelection.selectionId,
          plan,
        }),
      );
      if (!reportOperationIsCurrent(request)) return;
      setPreviewedPlan(plan);
      setPreviewResult(result);
      setWorkflow("previewed");
      setWorkflowMessage(previewWorkflowMessage(result));
    } catch (error) {
      if (!reportOperationIsCurrent(request)) return;
      setPreviewedPlan(null);
      setPreviewResult(null);
      setWorkflow("error");
      setWorkflowMessage(messageFrom(error, "한/글 구조 미리보기를 완료하지 못했습니다."));
    }
  }

  async function applyDocument() {
    if (
      !window.trafficSuperVision ||
      !runtime.activeImport ||
      !reportScopeKey ||
      !documentSelection?.selectionId ||
      !previewedPlan ||
      !previewResult ||
      !isHwpPreviewApplyEligible(previewResult)
    ) {
      setWorkflow("error");
      setWorkflowMessage(
        previewResult?.mode === "sample_target_structure_preview"
          ? "샘플 양식은 정확한 작성기와 저장 후 48쪽 전체 검증이 준비되기 전까지 적용할 수 없습니다."
          : "같은 양식과 편집 계획으로 구조 미리보기를 먼저 완료하세요.",
      );
      return;
    }
    setWorkflow("applying");
    setWorkflowMessage(null);
    const request = beginReportOperation();
    try {
      const result = parseApply(
        await window.trafficSuperVision.hwp.apply({
          projectId: runtime.activeImport.projectId,
          documentSelectionId: documentSelection.selectionId,
          plan: previewedPlan,
          approvalToken: previewResult.approval_token,
        }),
      );
      if (!reportOperationIsCurrent(request)) return;
      setApplyResult(result);
      setWorkflow("applied");
      setWorkflowMessage(result.message);
    } catch (error) {
      if (!reportOperationIsCurrent(request)) return;
      setPreviewedPlan(null);
      setPreviewResult(null);
      setWorkflow("error");
      setWorkflowMessage(messageFrom(error, "한/글 문서에 편집 계획을 적용하지 못했습니다."));
    }
  }

  async function applyExactReport() {
    if (
      !window.trafficSuperVision ||
      !exactReportContext ||
      !exactPreview ||
      exactApprovalConsumed ||
      exactWorkflow !== "ready"
    ) {
      return;
    }
    const approvalToken = exactPreview.approvalToken;
    setExactApprovalConsumed(true);
    setExactWorkflow("applying");
    setExactWorkflowMessage(
      "한/글 COM으로 부분 편집한 뒤 문서를 다시 열고 48쪽 전체를 검증하고 있습니다.",
    );
    try {
      const result = parseExactReportApplication(
        await window.trafficSuperVision.reports.applyExact({
          projectId: exactReportContext.projectId,
          pipelineJobId: exactReportContext.pipelineJobId,
          draftId: exactPreview.draftId,
          approvalToken,
          documentEditPlanSha256:
            exactPreview.documentEditPlanSha256,
          acceptRejectRollbackBindingSha256:
            exactPreview.acceptRejectRollbackBindingSha256,
          userConfirmed: true,
        }),
      );
      if (
        result.projectId !== exactReportContext.projectId ||
        result.pipelineJobId !== exactReportContext.pipelineJobId ||
        result.draftId !== exactPreview.draftId ||
        result.bindingManifestSha256 !==
          exactPreview.bindingManifestSha256 ||
        result.exactPlanSha256 !== exactPreview.exactPlanSha256 ||
        result.documentEditPlanSha256 !==
          exactPreview.documentEditPlanSha256 ||
        result.acceptRejectRollbackBindingSha256 !==
          exactPreview.acceptRejectRollbackBindingSha256
      ) {
        throw new Error(
          "현재 PipelineJob과 HWPX 적용 결과 provenance가 일치하지 않습니다.",
        );
      }
      setExactApplication(result);
      if (
        result.state === "render_validated_accept_or_reject_required"
      ) {
        setExactWorkflow("awaiting_decision");
        setExactWorkflowMessage(
          `저장·재열기·${result.renderedPageCount ?? 0}/48쪽 렌더 검증을 통과했습니다. 결과를 수락하거나 거절하세요.`,
        );
      } else {
        setExactWorkflow("validation_failed");
        setExactWorkflowMessage(
          result.rollbackCompleted
            ? "48쪽 검증을 통과하지 못해 생성 파일을 자동 롤백했습니다."
            : "48쪽 검증 실패 후 안전한 롤백도 완료하지 못했습니다. 결과를 사용하지 마세요.",
        );
      }
    } catch (error) {
      setExactWorkflow("error");
      setExactWorkflowMessage(
        messageFrom(
          error,
          "한/글 COM 적용 또는 48쪽 전체 검증을 완료하지 못했습니다. 승인 토큰은 다시 사용하지 않습니다.",
        ),
      );
    }
  }

  async function decideExactReport(accept: boolean) {
    if (
      !window.trafficSuperVision ||
      !exactReportContext ||
      !exactApplication ||
      exactWorkflow !== "awaiting_decision"
    ) {
      return;
    }
    setExactWorkflow("applying");
    setExactWorkflowMessage(
      accept ? "검증된 보고서를 수락하고 있습니다." : "검증된 보고서를 거절하고 롤백하고 있습니다.",
    );
    try {
      const request = {
        projectId: exactReportContext.projectId,
        pipelineJobId: exactReportContext.pipelineJobId,
        applicationId: exactApplication.applicationId,
        documentEditPlanSha256:
          exactApplication.documentEditPlanSha256,
        acceptRejectRollbackBindingSha256:
          exactApplication.acceptRejectRollbackBindingSha256,
        userConfirmed: true as const,
      };
      const result = parseExactReportApplication(
        accept
          ? await window.trafficSuperVision.reports.acceptExact(request)
          : await window.trafficSuperVision.reports.rejectExact(request),
      );
      if (
        result.projectId !== exactReportContext.projectId ||
        result.pipelineJobId !== exactReportContext.pipelineJobId ||
        result.applicationId !== exactApplication.applicationId ||
        result.bindingManifestSha256 !==
          exactApplication.bindingManifestSha256 ||
        result.exactPlanSha256 !== exactApplication.exactPlanSha256 ||
        result.documentEditPlanSha256 !==
          exactApplication.documentEditPlanSha256 ||
        result.acceptRejectRollbackBindingSha256 !==
          exactApplication.acceptRejectRollbackBindingSha256
      ) {
        throw new Error(
          "현재 PipelineJob과 HWPX 최종 결정 provenance가 일치하지 않습니다.",
        );
      }
      setExactApplication(result);
      setExactWorkflow(accept ? "accepted" : "rejected");
      setExactWorkflowMessage(
        accept
          ? "48쪽 검증을 통과한 보고서를 수락했습니다. 승인 이력과 해시가 보존됩니다."
          : "보고서를 거절했고 검증 산출물을 롤백했습니다. 거절 이력은 보존됩니다.",
      );
    } catch (error) {
      setExactWorkflow("awaiting_decision");
      setExactWorkflowMessage(
        messageFrom(error, "HWPX 수락·거절 결정을 완료하지 못했습니다."),
      );
    }
  }

  async function rollbackAcceptedExactReport() {
    if (
      !window.trafficSuperVision ||
      !exactReportContext ||
      !exactApplication ||
      exactWorkflow !== "accepted"
    ) {
      return;
    }
    setExactWorkflow("applying");
    setExactWorkflowMessage("수락한 보고서의 해시를 재검증하고 롤백하고 있습니다.");
    try {
      const result = parseExactReportApplication(
        await window.trafficSuperVision.reports.rollbackExact({
          projectId: exactReportContext.projectId,
          pipelineJobId: exactReportContext.pipelineJobId,
          applicationId: exactApplication.applicationId,
          documentEditPlanSha256:
            exactApplication.documentEditPlanSha256,
          acceptRejectRollbackBindingSha256:
            exactApplication.acceptRejectRollbackBindingSha256,
          userConfirmed: true,
        }),
      );
      if (
        result.projectId !== exactReportContext.projectId ||
        result.pipelineJobId !== exactReportContext.pipelineJobId ||
        result.applicationId !== exactApplication.applicationId ||
        result.bindingManifestSha256 !==
          exactApplication.bindingManifestSha256 ||
        result.exactPlanSha256 !== exactApplication.exactPlanSha256 ||
        result.documentEditPlanSha256 !==
          exactApplication.documentEditPlanSha256 ||
        result.acceptRejectRollbackBindingSha256 !==
          exactApplication.acceptRejectRollbackBindingSha256 ||
        result.state !== "accepted_report_rolled_back"
      ) {
        throw new Error(
          "현재 PipelineJob과 HWPX 롤백 provenance가 일치하지 않습니다.",
        );
      }
      setExactApplication(result);
      setExactWorkflow("rolled_back");
      setExactWorkflowMessage(
        "수락했던 보고서 파일을 롤백했습니다. 수락·롤백 이력과 해시는 보존됩니다.",
      );
    } catch (error) {
      setExactWorkflow("accepted");
      setExactWorkflowMessage(
        messageFrom(error, "수락한 HWPX 보고서를 롤백하지 못했습니다."),
      );
    }
  }

  async function rollbackDocument() {
    if (
      !window.trafficSuperVision ||
      !runtime.activeImport ||
      !reportScopeKey ||
      !applyResult
    ) {
      return;
    }
    setWorkflow("rolling_back");
    setWorkflowMessage(null);
    const request = beginReportOperation();
    try {
      const result = await window.trafficSuperVision.hwp.rollback({
        projectId: runtime.activeImport.projectId,
        rollbackToken: applyResult.rollback_token,
      });
      if (!reportOperationIsCurrent(request)) return;
      if (
        !isRecord(result) ||
        result.rolled_back !== true ||
        typeof result.output_file_name !== "string" ||
        typeof result.message !== "string"
      ) {
        throw new Error("롤백 완료 응답을 검증하지 못했습니다.");
      }
      setApplyResult(null);
      setWorkflow("rolled_back");
      setWorkflowMessage(result.message);
    } catch (error) {
      if (!reportOperationIsCurrent(request)) return;
      setWorkflow("error");
      setWorkflowMessage(messageFrom(error, "생성된 문서를 롤백하지 못했습니다."));
    }
  }

  const providerFeedback = providerError
    ? {
        tone: "danger" as const,
        role: "alert" as const,
        title: "Provider 작업을 완료하지 못했습니다.",
        message: providerError,
        icon: <AlertTriangle size={17} />,
      }
    : generationMessage
      ? {
          tone: "success" as const,
          role: "status" as const,
          title: "구조화 초안 생성 완료",
          message: generationMessage,
          icon: <FileCheck2 size={17} />,
        }
      : providerMessage
        ? {
            tone: "success" as const,
            role: "status" as const,
            title: "공식 CLI가 인증을 소유합니다.",
            message: providerMessage,
            icon: <ShieldCheck size={17} />,
          }
        : selectedProvider?.message
          ? {
              tone: "neutral" as const,
              role: "status" as const,
              title: "현재 Provider 상태",
              message: selectedProvider.message,
              icon: <Bot size={17} />,
            }
          : null;

  return (
    <div className="screen screen--report">
      <header className="screen-header">
        <div>
          <p className="eyebrow">결정적 문서 편집</p>
          <h1>HWP/HWPX 보고서</h1>
          <p>AI는 구조화된 편집 계획만 만들고, 승인된 작업만 한/글 COM 편집기가 적용합니다.</p>
        </div>
        <div className="provider-status">
          <StatusDot
            tone={
              preflightLoading
                ? "neutral"
                : preflight?.available
                  ? "success"
                  : "warning"
            }
          >
            한/글 COM ·{" "}
            {preflightLoading ? "확인 중" : preflight?.available ? "사용 가능" : "사용 불가"}
          </StatusDot>
          {providers
            .filter((item) => item.provider !== "local")
            .map((item) => (
              <StatusDot key={item.provider} tone={providerTone(item.state)}>
                {item.display_name} · {providerStatusLabel(item.state)}
                {item.version
                  ? ` · v${item.version.replace(/^[vV]/, "")}`
                  : ""}
              </StatusDot>
            ))}
          {!providerLoading && providers.length === 0 ? (
            <StatusDot tone="danger">Provider · 확인 실패</StatusDot>
          ) : null}
        </div>
      </header>

      {reportDeidentifiedReviewJob ? (
        <ReportDeidentifiedEvidenceReview
          job={reportDeidentifiedReviewJob}
        />
      ) : (
      <div
        className={`report-layout ${
          exactReportContext ? "report-layout--exact" : ""
        }`}
      >
        {!exactReportContext ? (
          <div className="report-controls">
          <GlassPanel className="provider-panel">
            <PanelHeader
              eyebrow="보고서 문장 Provider"
              title="보고서 문장 생성"
              action={
                <Button
                  variant="ghost"
                  onClick={() => void refreshProviders()}
                  disabled={providerLoading}
                >
                  <RefreshCw className={providerLoading ? "spin" : ""} size={15} />
                  상태 확인
                </Button>
              }
            />
            <div className="provider-tabs" role="group" aria-label="언어모델 Provider">
              {providerOrder.map((id) => {
                const item = providers.find((row) => row.provider === id);
                const displayName =
                  id === "local"
                    ? "Local Model"
                    : item?.display_name ??
                      (id === "codex" ? "Codex CLI" : "Claude Code");
                return (
                  <button
                    type="button"
                    key={id}
                    className={provider === id ? "active" : ""}
                    onClick={() => setProvider(id)}
                    disabled={item?.state === "coming_soon" || id === "local"}
                  >
                    <Bot size={18} />
                    <span>
                      <strong>{displayName}</strong>
                      <small title={item?.message}>
                        {item
                          ? providerDiagnosticSummary(item)
                          : "상태 확인 전"}
                      </small>
                    </span>
                  </button>
                );
              })}
            </div>

            <div className="send-scope">
              <h3>모델에 전송할 범위</h3>
              <div className="chip-row">
                <Badge tone="success">사용자 승인 사건 JSON</Badge>
                <Badge tone="success">확정 OCR 결과</Badge>
                <Badge tone="success">비식별 근거 참조 ID</Badge>
                <Badge tone="success">문서 구조 요약</Badge>
                <Badge tone="danger">원본 영상 전송 안 함</Badge>
                <Badge tone={reportPipeline ? "success" : "warning"}>
                  {reportPipeline
                    ? "Pipeline provenance 결속"
                    : "Pipeline provenance 미결속"}
                </Badge>
              </div>
              <label className="rights-check">
                <input
                  type="checkbox"
                  checked={consent}
                  onChange={(event) => setConsent(event.target.checked)}
                />
                <span className="check-visual" aria-hidden="true"><Check size={14} /></span>
                <span>
                  <strong>표시된 범위만 {selectedProvider?.display_name ?? "선택 Provider"}에 전송합니다.</strong>
                  <small>인증 파일이나 CLI 토큰은 TrafficSuperVision이 읽지 않습니다.</small>
                </span>
              </label>
              <div className="provider-actions">
                {provider !== "local" &&
                selectedProvider?.state === "not_connected" ? (
                  <Button
                    onClick={() => void launchLogin()}
                    disabled={providerAction !== null || !window.trafficSuperVision}
                  >
                    {providerAction === provider ? (
                      <LoaderCircle className="spin" size={16} />
                    ) : (
                      <LogIn size={16} />
                    )}
                    공식 CLI에서 로그인
                  </Button>
                ) : null}
                {provider !== "local" &&
                selectedProvider?.state === "unavailable" ? (
                  <Button
                    disabled
                    title={`${selectedProvider.display_name}를 먼저 설치해야 합니다.`}
                  >
                    <LogIn size={16} />
                    CLI 설치 필요
                  </Button>
                ) : null}
                <Button
                  variant="primary"
                  className="provider-generate"
                  disabled={
                    generationLoading ||
                    !consent ||
                    selectedProvider?.state !== "ready" ||
                    !runtime.analysisSummary ||
                    approvedReviews.length === 0 ||
                    !documentSelection?.selectionId
                  }
                  title={
                    !documentSelection?.selectionId
                      ? "보고서 대상 HWP/HWPX 양식을 먼저 선택하세요."
                      : approvedReviews.length === 0
                        ? "질문 검토에서 사건을 한 건 이상 승인하세요."
                        : !consent
                          ? "전송 범위를 확인하고 동의하세요."
                          : selectedProvider?.state !== "ready"
                            ? "공식 CLI 로그인을 완료하세요."
                            : undefined
                  }
                  onClick={() => void generateReport()}
                >
                  {generationLoading ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : null}
                  보고서 초안과 편집 계획 생성
                </Button>
              </div>
              {providerFeedback ? (
                <div
                  className={`notice provider-feedback notice--${providerFeedback.tone}`}
                  role={providerFeedback.role}
                  title={providerFeedback.message}
                >
                  {providerFeedback.icon}
                  <div>
                    <strong>{providerFeedback.title}</strong>
                    <p>{providerFeedback.message}</p>
                  </div>
                </div>
              ) : null}
            </div>
          </GlassPanel>

          <GlassPanel className="edit-plan-panel">
            <PanelHeader
              eyebrow="문서 편집 계획"
              title="편집 계획"
              action={<span className="mono">수락 {accepted} · 대기 {pending}</span>}
            />
            {displayOperations.length ? (
              <div className="bulk-plan-actions">
                <Button
                  variant="ghost"
                  onClick={() => setAll("accepted")}
                  disabled={busy}
                >
                  전체 항목 수락
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => setAll("pending")}
                  disabled={busy}
                >
                  <RotateCcw size={16} /> 초기화
                </Button>
              </div>
            ) : null}
            <div className="edit-op-list">
              {displayOperations.length ? displayOperations.map((op) => {
                const state = states[op.id];
                const supported = op.supported;
                return (
                  <article
                    key={op.id}
                    className={`edit-op edit-op--${state} ${supported ? "" : "edit-op--unsupported"}`}
                  >
                    <header>
                      <Badge tone={supported ? "neutral" : "warning"}>{op.op}</Badge>
                      <span className="mono">{op.id}</span>
                      <span className="op-state">
                        {!supported
                          ? "근거 핸들 연계 대기"
                          : state === "accepted"
                            ? "수락"
                            : state === "rejected"
                              ? "거절"
                              : "대기"}
                      </span>
                    </header>
                    <strong>{op.target}</strong>
                    <p>{op.value}</p>
                    <div>
                      <Button
                        variant="positive"
                        aria-pressed={state === "accepted"}
                        disabled={!supported || busy}
                        title={!supported ? "현재 COM 브리지는 문단·필드 편집만 적용합니다." : undefined}
                        onClick={() => updateOperation(op.id, "accepted")}
                      >
                        <Check size={15} /> 수락
                      </Button>
                      <Button
                        variant="danger"
                        aria-pressed={state === "rejected"}
                        disabled={busy}
                        onClick={() => updateOperation(op.id, "rejected")}
                      >
                        <X size={15} /> 거절
                      </Button>
                    </div>
                  </article>
                );
              }) : (
                <div className="edit-plan-empty">
                  <FileText size={28} />
                  <strong>생성된 편집 계획이 없습니다.</strong>
                  <p>
                    승인 사건·전송 동의·Provider·양식을 확인한 뒤 구조화
                    초안을 생성하세요.
                  </p>
                </div>
              )}
            </div>
            <Button
              variant="primary"
              className="full-width"
              disabled={
                accepted === 0 ||
                busy ||
                !runtime.activeImport ||
                !documentSelection?.selectionId ||
                !preflight?.available
              }
              onClick={() => void previewDocument()}
            >
              {workflow === "previewing" ? (
                <LoaderCircle className="spin" size={18} />
              ) : (
                <FileCheck2 size={18} />
              )}
              수락 항목을 한/글 구조로 미리보기
            </Button>
          </GlassPanel>
          </div>
        ) : null}

        <GlassPanel className="document-preview-panel">
          <header className="document-toolbar">
            <div className="document-source">
              <FileText size={18} />
              <span>
                {exactReportContext
                  ? "프로젝트 기본 3·4분기 HWPX 양식"
                  : documentSelection?.file?.name ??
                    "HWP/HWPX 양식을 선택하세요"}
              </span>
              <Badge
                tone={
                  exactReportContext &&
                  ["ready", "awaiting_decision", "accepted"].includes(
                    exactWorkflow,
                  )
                    ? "success"
                    : exactReportContext
                      ? exactWorkflow === "error" ||
                        exactWorkflow === "validation_failed"
                        ? "danger"
                        : "warning"
                  : previewResult
                    ? "success"
                    : documentSelection
                      ? "warning"
                      : "neutral"
                }
              >
                {exactReportContext
                  ? exactWorkflow === "preparing"
                    ? "정확 위치 미리보기 자동 준비"
                    : exactWorkflow === "ready"
                      ? "정확 위치 결속 완료 · COM 승인 대기"
                      : exactWorkflow === "awaiting_decision"
                        ? "48쪽 검증 완료 · 수락/거절 대기"
                        : exactWorkflow === "accepted"
                          ? "48쪽 검증·수락 완료"
                          : exactWorkflow === "rejected"
                            ? "거절·롤백 완료"
                            : exactWorkflow === "rolled_back"
                              ? "수락 후 롤백 완료"
                              : exactWorkflow === "validation_failed"
                                ? "48쪽 검증 실패"
                                : exactWorkflow === "error"
                                  ? "정확 보고서 준비 실패"
                                  : "PipelineJob 결속 확인"
                  : previewResult
                   ? previewResult.mode === "sample_target_structure_preview"
                     ? "샘플 대상·구조 검증 · 적용 차단"
                    : "COM 구조 읽기 완료 · 아래는 구성 스케치"
                  : documentSelection
                    ? "COM 구조 미리보기 전"
                    : "양식 미선택"}
              </Badge>
            </div>
            <div>
              <span className="mono">
                {exactReportContext
                  ? "현재 PipelineJob provenance 고정"
                  : "페이지 렌더링 전 · 구성 스케치"}
              </span>
              {!exactReportContext ? (
                <Button onClick={() => void selectDocument()} disabled={busy}>
                  <FolderOpen size={16} /> 양식 선택
                </Button>
              ) : null}
            </div>
          </header>

          {preflight && !preflight.available ? (
            <div className="document-runtime-notice notice notice--warning" role="status">
              <AlertTriangle size={17} />
              <div>
                <strong>한/글 COM을 사용할 수 없습니다.</strong>
                <p>{preflight.message}</p>
              </div>
              <Button variant="ghost" onClick={() => void refreshPreflight()}>
                다시 확인
              </Button>
            </div>
          ) : null}

          {exactReportContext ? (
            <div
              className={`document-runtime-notice notice ${
                exactWorkflow === "error" ||
                exactWorkflow === "validation_failed"
                  ? "notice--warning"
                  : "notice--success"
              }`}
              role={exactWorkflow === "error" ? "alert" : "status"}
            >
              {exactWorkflow === "preparing" ||
              exactWorkflow === "applying" ? (
                <LoaderCircle className="spin" size={17} />
              ) : exactWorkflow === "error" ||
                exactWorkflow === "validation_failed" ? (
                <AlertTriangle size={17} />
              ) : (
                <ShieldCheck size={17} />
              )}
              <div>
                <strong>
                  {exactPreview
                    ? `승인 사건 ${exactPreview.approvedIncidentCount}건 · 정확 편집 ${exactPreview.operationCount}건`
                    : `정확 편집 ${exactReportContext.operationCount}건 · 준비 상태 확인 중`}
                </strong>
                <p>
                  {exactWorkflowMessage ??
                    "현재 영상과 같은 PipelineJob의 사용자 승인 사건만 사용합니다."}
                  {exactPreview
                    ? ` 계획 ${exactPreview.exactPlanSha256.slice(0, 12)}… · 문서 ${exactPreview.outputDocumentSha256.slice(0, 12)}… · COM 승인 후 48쪽 전체 검증`
                    : ""}
                </p>
              </div>
              {exactWorkflow === "error" ? (
                <Button
                  variant="ghost"
                  onClick={() =>
                    setExactPreviewRevision((current) => current + 1)
                  }
                >
                  <RefreshCw size={15} />
                  미리보기 다시 준비
                </Button>
              ) : null}
            </div>
          ) : null}

          {previewResult?.mode === "preview" ? (
            <div className="document-runtime-summary" role="status">
              <ShieldCheck size={16} />
              <span>
                실제 양식 구조: 문단 {previewResult.structure.paragraph_count.toLocaleString("ko-KR")} ·
                표 {previewResult.structure.table_count.toLocaleString("ko-KR")} ·
                그림 {previewResult.structure.picture_count.toLocaleString("ko-KR")} ·
                적용 예정 {previewResult.operation_count}건
              </span>
            </div>
          ) : null}

          {!exactReportContext &&
          previewResult?.mode === "sample_target_structure_preview" ? (
            <div
              className="document-runtime-notice notice notice--warning"
              role="status"
            >
              <AlertTriangle size={17} />
              <div>
                <strong>
                  샘플 양식의 대상·구조는 검증했지만 실제 적용은 차단되어
                  있습니다.
                </strong>
                <p>
                  승인 표본 {previewResult.approved_reference_count}건 · 편집
                  대상 {previewResult.operation_count}건 · 원본 1~48쪽 구조
                  보존 확인. 승인 사건 내용·사진 근거 결합은 아직 수행하지
                  않았습니다. {hwpPreviewBlockerMessages(previewResult).join(" ")}
                </p>
              </div>
            </div>
          ) : null}

          <div
            className="document-preview-controls"
            role="toolbar"
            aria-label="문서 구성 스케치 보기 도구"
          >
            <div className="document-page-navigation">
              <IconButton
                label="이전 구성 스케치"
                disabled
                title="현재 구성 스케치는 한 장입니다."
              >
                <ChevronLeft size={16} />
              </IconButton>
              <div className="document-page-indicator">
                <strong>구성 스케치 1 / 1</strong>
                <span>실제 페이지 렌더링 전</span>
              </div>
              <IconButton
                label="다음 구성 스케치"
                disabled
                title="현재 구성 스케치는 한 장입니다."
              >
                <ChevronRight size={16} />
              </IconButton>
            </div>
            <div className="document-zoom-controls">
              <IconButton
                label="구성 스케치 축소"
                onClick={() => changeDocumentZoom(-10)}
                disabled={documentZoom <= DOCUMENT_ZOOM_MIN}
              >
                <Minus size={15} />
              </IconButton>
              <output aria-label="현재 구성 스케치 확대율">
                {documentZoom}%
              </output>
              <IconButton
                label="구성 스케치 확대"
                onClick={() => changeDocumentZoom(10)}
                disabled={documentZoom >= DOCUMENT_ZOOM_MAX}
              >
                <Plus size={15} />
              </IconButton>
              <Button
                variant="ghost"
                className="document-fit-button"
                aria-pressed={documentZoomMode === "fit-width"}
                onClick={fitDocumentWidth}
              >
                <Maximize2 size={15} />
                너비 맞춤
              </Button>
            </div>
          </div>

          <div className="document-stage" ref={documentStageRef}>
            {exactReportContext ? (
              <article
                className="document-page"
                aria-label="현재 PipelineJob 정확 HWPX 검증 상태"
                style={{ zoom: `${documentZoom}%` }}
              >
                <h2>도 로 시 설 물 점 검 보 고 서</h2>
                <table>
                  <tbody>
                    <tr>
                      <th>입력 범위</th>
                      <td>현재 PipelineJob 고정</td>
                      <th>승인 사건</th>
                      <td>
                        {exactPreview
                          ? `${exactPreview.approvedIncidentCount}건`
                          : "검증 중"}
                      </td>
                    </tr>
                    <tr>
                      <th>양식</th>
                      <td>선택된 3·4분기 sample 원형</td>
                      <th>편집 대상</th>
                      <td>
                        {exactPreview
                          ? `${exactPreview.operationCount}건`
                          : `${exactReportContext.operationCount}건`}
                      </td>
                    </tr>
                    <tr>
                      <th>COM 적용</th>
                      <td>
                        {exactApplication?.comPublishCompleted
                          ? "완료"
                          : "사용자 승인 전"}
                      </td>
                      <th>페이지 검증</th>
                      <td>
                        {exactApplication?.renderValidationCompleted
                          ? `${exactApplication.renderedPageCount ?? 0}/48쪽`
                          : "적용 후 48쪽 전체 검증"}
                      </td>
                    </tr>
                  </tbody>
                </table>
                <h3>현재 문서 상태</h3>
                <p className="document-change">
                  {exactWorkflowMessage ??
                    "사용자 승인 사건의 위치·사진·점검 의견·평가 네모를 정확한 양식 위치에 결속하고 있습니다."}
                </p>
                <div className="document-evidence-empty">
                  과거 sample의 사실 내용은 최종 자료로 재사용하지 않습니다.
                  현재 영상의 승인 Incident 전체가 정확히 결속되지 않거나
                  48쪽 검증을 통과하지 않으면 문서 수락을 차단합니다.
                </div>
                <div className="change-legend">
                  <span>
                    <i /> 표·글꼴·문단·좌우 2열 사진 구조 보존
                  </span>
                  <span>
                    <LockKeyhole size={13} /> COM 적용은 명시적 승인 후 실행
                  </span>
                </div>
              </article>
            ) : (
              <article
                className="document-page"
                aria-label="한글 보고서 화면 구성 예시"
                style={{ zoom: `${documentZoom}%` }}
              >
              <h2>{generation?.draft.title ?? "도 로 시 설 물 점 검 보 고 서"}</h2>
              <table>
                <tbody>
                  <tr>
                    <th>점검일자</th><td>{new Date().toLocaleDateString("ko-KR")}</td>
                    <th>점검구간</th><td>GPS 미연결 · 위치 검증 필요</td>
                  </tr>
                  <tr>
                    <th>입력영상</th><td>{runtime.activeImport ? "SHA-256 검증 완료" : "미등록"}</td>
                    <th>승인사건</th><td>{approvedReviews.length}건</td>
                  </tr>
                </tbody>
              </table>
              {generation ? (
                generation.draft.sections.map((section, index) => (
                  <section key={`${section.heading}-${index}`}>
                    <h3>{index + 1}. {section.heading}</h3>
                    <p className="document-change">{section.body}</p>
                  </section>
                ))
              ) : (
                <>
                  <h3>1. 조사 개요</h3>
                  <p>
                    생성된 보고서 문장이 없습니다. 승인 사건과 문서 구조를 확인한
                    뒤 공식 CLI로 초안을 생성하면 이 구성 스케치에 표시됩니다.
                  </p>
                </>
              )}
              {approvedEvidenceImages.length ? (
                <div className="document-images">
                  {approvedEvidenceImages.map((evidence, index) => (
                    <figure key={evidence.candidateId}>
                      <img
                        src={evidence.src}
                        alt={`승인 사건 후보 ${evidence.candidateId}의 대표 근거`}
                      />
                      <figcaption>
                        그림 {index + 1}. 승인 사건 연결 근거 ·{" "}
                        {evidence.timeSeconds.toFixed(1)}초
                      </figcaption>
                    </figure>
                  ))}
                </div>
              ) : (
                <div className="document-evidence-empty">
                  승인 사건에 연결된 근거 이미지가 없습니다.
                </div>
              )}
              <h3>2. 유형별 집계</h3>
              <table>
                <thead>
                  <tr><th>유형</th><th>건수</th><th>심각</th><th>조치</th></tr>
                </thead>
                <tbody>
                  {approvedTypeCounts.length ? (
                    approvedTypeCounts.map(([type, count]) => (
                      <tr key={type}>
                        <td>{type}</td>
                        <td>{count}</td>
                        <td>사용자 판정</td>
                        <td>승인 답변 참조</td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={4}>승인 사건이 아직 없습니다.</td>
                    </tr>
                  )}
                </tbody>
              </table>
              <div className="change-legend">
                <span><i /> 수락된 편집 계획의 구성 스케치 표시</span>
                <span><LockKeyhole size={13} /> 실제 양식 서식은 COM 편집기가 보존</span>
              </div>
              </article>
            )}
          </div>

          <footer className="document-footer">
            {exactReportContext ? (
              <>
                <div>
                  <ShieldCheck size={17} />
                  <span>
                    {exactApplication?.outputSha256
                      ? `${exactApplication.outputFileName} · SHA-256 ${exactApplication.outputSha256.slice(0, 12)}… · ${exactApplication.renderedPageCount ?? 0}/48쪽`
                      : exactWorkflowMessage ??
                        "현재 PipelineJob의 정확 HWPX 미리보기를 자동 준비하고 있습니다."}
                  </span>
                </div>
                <div>
                  {exactWorkflow === "awaiting_decision" ? (
                    <>
                      <Button
                        onClick={() => void decideExactReport(false)}
                        disabled={exactBusy}
                      >
                        <X size={16} />
                        거절하고 롤백
                      </Button>
                      <Button
                        variant="primary"
                        onClick={() => void decideExactReport(true)}
                        disabled={exactBusy}
                      >
                        <Check size={17} />
                        48쪽 검증 결과 수락
                      </Button>
                    </>
                  ) : exactWorkflow === "accepted" ? (
                    <Button
                      onClick={() => void rollbackAcceptedExactReport()}
                      disabled={exactBusy}
                    >
                      <RotateCcw size={16} />
                      수락 결과 롤백
                    </Button>
                  ) : exactWorkflow === "ready" ? (
                    <Button
                      variant="primary"
                      onClick={() => void applyExactReport()}
                      disabled={
                        exactBusy ||
                        exactApprovalConsumed ||
                        !preflight?.available
                      }
                      title={
                        !preflight?.available
                          ? "한/글 COM 사전점검을 먼저 통과해야 합니다."
                          : "사용자 승인 사건만 sample 양식에 적용하고 재열기·48쪽 렌더 검증을 실행합니다."
                      }
                    >
                      <Save size={17} />
                      한/글 COM 적용 승인
                    </Button>
                  ) : exactWorkflow === "applying" ? (
                    <Button variant="primary" disabled>
                      <LoaderCircle className="spin" size={17} />
                      COM 적용·48쪽 검증 중
                    </Button>
                  ) : exactWorkflow === "validation_failed" &&
                    exactApplication?.rollbackCompleted ? (
                    <Button
                      onClick={() =>
                        setExactPreviewRevision((current) => current + 1)
                      }
                    >
                      <RefreshCw size={16} />
                      롤백 확인 후 다시 준비
                    </Button>
                  ) : null}
                </div>
              </>
            ) : (
              <>
                <div>
                  <ShieldCheck size={17} />
                  <span>
                    {applyResult
                      ? `${applyResult.output_file_name} · ${formatBytes(applyResult.size_bytes)} · SHA-256 ${applyResult.sha256.slice(0, 12)}…`
                      : workflowMessage ??
                        "정적 화면은 구성 예시입니다. 실제 양식은 COM 구조 미리보기 후 별도 파일로 저장합니다."}
                  </span>
                </div>
                <div>
                  <Button
                    onClick={() => void rollbackDocument()}
                    disabled={!applyResult || busy}
                  >
                    {workflow === "rolling_back" ? (
                      <LoaderCircle className="spin" size={16} />
                    ) : (
                      <RotateCcw size={16} />
                    )}
                    생성 결과 롤백
                  </Button>
                  <Button
                    variant="primary"
                    disabled={
                      !previewedPlan ||
                      workflow !== "previewed" ||
                      !isHwpPreviewApplyEligible(previewResult)
                    }
                    title={
                      previewResult?.mode === "sample_target_structure_preview"
                        ? "결정적 HWPML 작성기와 저장 후 48쪽 전체 렌더 검증이 준비되어야 적용할 수 있습니다."
                        : undefined
                    }
                    onClick={() => void applyDocument()}
                  >
                    {workflow === "applying" ? (
                      <LoaderCircle className="spin" size={17} />
                    ) : (
                      <Save size={17} />
                    )}
                    승인하고 새 파일로 저장
                  </Button>
                </div>
              </>
            )}
          </footer>
        </GlassPanel>
      </div>
      )}
    </div>
  );
}
