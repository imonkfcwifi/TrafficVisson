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
  type: "fill_text" | "fill_cell";
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

export type HwpPreviewResult = {
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

export type HwpApplyResult = {
  mode: "applied";
  rollback_token: string;
  output_file_name: string;
  size_bytes: number;
  sha256: string;
  readback: HwpPreviewResult["structure"];
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
