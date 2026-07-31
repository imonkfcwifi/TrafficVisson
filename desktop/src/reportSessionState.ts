import { useCallback, useSyncExternalStore } from "react";

export type ProviderId = "codex" | "claude" | "local";
export type OpState = "accepted" | "rejected" | "pending";
export type HwpWorkflow =
  | "idle"
  | "previewing"
  | "previewed"
  | "applying"
  | "applied"
  | "rolling_back"
  | "rolled_back"
  | "error";

export type DocumentEditOperation = {
  operationId: string;
  type: "fill_text" | "fill_cell" | "insert_image";
  target: string;
  expectedValue?: string;
  value: string;
  sourceRef: string;
  stylePolicy: "preserve" | "inherit_target";
  preview: string;
};

export type DocumentEditPlan = {
  schemaVersion: "1.0";
  planId: string;
  documentSnapshotId: string;
  operations: DocumentEditOperation[];
};

export type LegacyHwpPreviewResult = {
  mode: "preview";
  operation_count: number;
  operation_types: Record<string, number>;
  structure: {
    observed_characters: number;
    truncated: boolean;
    paragraph_count: number;
    table_count: number;
    picture_count: number;
    field_names: string[];
    bookmark_names: string[];
  };
  message: string;
  approval_token: string;
  approval_expires_at: string;
  document_sha256: string;
  plan_sha256: string;
  target_summary: {
    operation_count: number;
    operation_types: Record<string, number>;
    targets: string[];
  };
};

export type HwpSampleTargetStructureBlockingReason =
  | "exact_hwpml_target_writer_not_implemented"
  | "output_reopen_and_48_page_render_validation_not_implemented";

export type HwpSampleTargetStructureOperationBinding = {
  operation_id: string;
  approval_id: string;
  operation_type: "fill_cell" | "insert_image";
  target_id: string;
  semantic_role:
    | "photo_slot_image"
    | "photo_caption"
    | "inspection_route"
    | "inspection_opinion_body"
    | "evaluation_square_marker";
  page_number: number;
  table_index: number;
  row: number;
  column: number;
};

export type HwpSampleTargetStructurePreviewResult = {
  schema_version: "trafficvisson.hwpx-sample-target-plan.v1";
  mode: "sample_target_structure_preview";
  state: "target_structure_preview_validated_apply_unavailable";
  document_snapshot_id: string;
  document_sha256: string;
  structure_target_summary_sha256: string;
  weak_case_index_manifest_sha256: string;
  plan_sha256: string;
  validation_sha256: string;
  approved_references_sha256: string;
  approved_reference_count: number;
  operation_count: number;
  page_pair_indices: number[];
  touched_pages: number[];
  expected_rendered_page_count: 48;
  expected_page_range: [1, 48];
  page_range_preserved_by_structure: true;
  table_growth_allowed: false;
  target_structure_validation_passed: true;
  approved_incident_bound: false;
  content_provenance_bound: false;
  com_invocation_performed: false;
  operation_bindings: HwpSampleTargetStructureOperationBinding[];
  approval_scope: "sample_target_structure_hash_binding_only";
  approval_token: string;
  approval_expires_at: string;
  apply_eligible: false;
  writer_state: "unavailable";
  output_render_validation_status: "not_run";
  blocking_reasons: HwpSampleTargetStructureBlockingReason[];
  no_com: true;
  no_document_write: true;
};

export type HwpPreviewResult =
  | LegacyHwpPreviewResult
  | HwpSampleTargetStructurePreviewResult;

export type HwpApplyResult = {
  mode: "applied";
  rollback_token: string;
  output_file_name: string;
  size_bytes: number;
  sha256: string;
  readback: LegacyHwpPreviewResult["structure"];
  message: string;
};

export type GeneratedDraft = {
  title: string;
  sections: Array<{
    heading: string;
    body: string;
    incidentIds: string[];
  }>;
};

export type ReportGenerationResult = {
  generationId: string;
  provider: "codex" | "claude";
  state: "generated_preview_required";
  draft: GeneratedDraft;
  plan: DocumentEditPlan;
  model?: string;
  usage?: { inputTokens?: number; outputTokens?: number };
  transmittedFields: string[];
};

export type ReportSessionState = {
  analysisScopeKey: string | null;
  provider: ProviderId;
  consent: boolean;
  operationStates: Record<string, OpState>;
  documentSelection: TrafficSuperVisionDocumentSelection | null;
  workflow: HwpWorkflow;
  workflowMessage: string | null;
  previewResult: HwpPreviewResult | null;
  previewedPlan: DocumentEditPlan | null;
  applyResult: HwpApplyResult | null;
  generation: ReportGenerationResult | null;
  generationMessage: string | null;
};

function initialReportSessionState(
  provider: ProviderId = "claude",
  consent = false,
  analysisScopeKey: string | null = null,
): ReportSessionState {
  return {
    analysisScopeKey,
    provider,
    consent,
    operationStates: {},
    documentSelection: null,
    workflow: "idle",
    workflowMessage: null,
    previewResult: null,
    previewedPlan: null,
    applyResult: null,
    generation: null,
    generationMessage: null,
  };
}

// This intentionally lives only in the renderer module process. It survives
// screen navigation, but no CLI credential, document handle, rollback token,
// or generated content is serialized to browser storage or disk.
let reportSessionState = initialReportSessionState();
const reportSessionListeners = new Set<() => void>();

export function prepareReportSessionScope(scopeKey: string | null) {
  if (reportSessionState.analysisScopeKey === scopeKey) return;
  reportSessionState = initialReportSessionState(
    reportSessionState.provider,
    reportSessionState.consent,
    scopeKey,
  );
}

export function getReportSessionState(): Readonly<ReportSessionState> {
  return reportSessionState;
}

export function setReportSessionField<K extends keyof ReportSessionState>(
  key: K,
  action:
    | ReportSessionState[K]
    | ((current: ReportSessionState[K]) => ReportSessionState[K]),
): ReportSessionState[K] {
  const current = reportSessionState[key];
  const next =
    typeof action === "function"
      ? (
          action as (
            value: ReportSessionState[K],
          ) => ReportSessionState[K]
        )(current)
      : action;
  reportSessionState = { ...reportSessionState, [key]: next };
  reportSessionListeners.forEach((listener) => listener());
  return next;
}

export function useReportSessionField<K extends keyof ReportSessionState>(
  key: K,
): [
  ReportSessionState[K],
  (
    action:
      | ReportSessionState[K]
      | ((current: ReportSessionState[K]) => ReportSessionState[K]),
  ) => void,
] {
  const snapshot = useSyncExternalStore(
    (listener) => {
      reportSessionListeners.add(listener);
      return () => reportSessionListeners.delete(listener);
    },
    () => reportSessionState,
    () => reportSessionState,
  );
  const setSessionValue = useCallback(
    (
      action:
        | ReportSessionState[K]
        | ((current: ReportSessionState[K]) => ReportSessionState[K]),
    ) => {
      setReportSessionField(key, action);
    },
    [key],
  );
  return [snapshot[key], setSessionValue];
}

export function resetReportSessionStateForTests() {
  reportSessionState = initialReportSessionState();
  reportSessionListeners.forEach((listener) => listener());
}
