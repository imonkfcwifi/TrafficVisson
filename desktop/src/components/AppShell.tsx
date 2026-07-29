import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  BrainCircuit,
  ChevronDown,
  CircleGauge,
  ClipboardCheck,
  FileOutput,
  FileText,
  FolderKanban,
  HardDrive,
  History,
  Import,
  Menu,
  Tags,
  X,
} from "lucide-react";
import type { ScreenId } from "../demo";
import {
  parseAnalysisJob,
  parseAnalysisJobList,
  toRestoredRuntime,
} from "../analysisJobs";
import {
  refreshProjects,
  selectProject,
  useProjectCatalog,
} from "../projectCatalog";
import {
  markPipelineHydrated,
  parsePipelineJob,
  parsePipelineList,
  pipelineQuestionQueueBinding,
  pipelinePointerForProject,
  pipelineStatusLabel,
  publishPipelineError,
  publishPipelineJob,
  resetPipelineUiState,
  usePipelineUi,
} from "../pipelineJobs";
import {
  reviewRecordsFromQuestionList,
  reviewStorageKey,
  saveReviewHistory,
} from "../reviewRecords";
import {
  clearRuntime,
  restoreCompletedAnalysis,
  useRuntimeState,
} from "../runtimeState";
import { IconButton, StatusDot } from "./ui";

const MOBILE_NAV_QUERY = "(max-width: 960px)";
const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

type ShellProviderStatus = {
  state: string;
  version: string | null;
  diagnosticState: string;
  authenticationState: string;
  networkState: string;
};

type RuntimeReadinessLoadState = "checking" | "loaded" | "unavailable";
type RuntimeComponentState =
  | "ready"
  | "incomplete"
  | "unavailable"
  | "failed"
  | "external_required"
  | "disabled";

type ShellRuntimeComponent = {
  componentId: string;
  displayName: string;
  state: RuntimeComponentState;
  message: string;
  version: string | null;
};

const RUNTIME_COMPONENT_STATES = new Set<RuntimeComponentState>([
  "ready",
  "incomplete",
  "unavailable",
  "failed",
  "external_required",
  "disabled",
]);

const ANALYSIS_RUNTIME_COMPONENTS = [
  ["pytorch_cuda", "PyTorch CUDA"],
  ["grounding_dino", "Grounding DINO"],
  ["sam2", "SAM 2"],
  ["opencv", "OpenCV"],
  ["korean_ocr", "한국어 OCR"],
  ["embedding_search", "유사 사례 검색"],
] as const;

function parseRuntimeReadiness(value: unknown): ShellRuntimeComponent[] | null {
  if (!value || typeof value !== "object") return null;
  const response = value as {
    schema_version?: unknown;
    components?: unknown;
  };
  if (
    response.schema_version !== "trafficvisson.runtime-readiness.v1" ||
    !Array.isArray(response.components)
  ) {
    return null;
  }
  const parsed: ShellRuntimeComponent[] = [];
  const componentIds = new Set<string>();
  for (const candidate of response.components) {
    if (!candidate || typeof candidate !== "object") return null;
    const row = candidate as {
      component_id?: unknown;
      display_name?: unknown;
      state?: unknown;
      message?: unknown;
      version?: unknown;
    };
    if (
      typeof row.component_id !== "string" ||
      componentIds.has(row.component_id) ||
      typeof row.display_name !== "string" ||
      typeof row.state !== "string" ||
      !RUNTIME_COMPONENT_STATES.has(row.state as RuntimeComponentState) ||
      typeof row.message !== "string" ||
      (row.version !== undefined && typeof row.version !== "string")
    ) {
      return null;
    }
    componentIds.add(row.component_id);
    parsed.push({
      componentId: row.component_id,
      displayName: row.display_name,
      state: row.state as RuntimeComponentState,
      message: row.message,
      version: typeof row.version === "string" ? row.version : null,
    });
  }
  return parsed;
}

function runtimeStateLabel(
  loadState: RuntimeReadinessLoadState,
  component: ShellRuntimeComponent | null,
) {
  if (loadState === "checking") return "확인 중";
  if (loadState === "unavailable" || !component) return "확인 불가";
  switch (component.state) {
    case "ready":
      return "준비됨";
    case "incomplete":
      return "설치됨 · 추가 구성 필요";
    case "unavailable":
      return "사용 불가";
    case "failed":
      return "확인 실패";
    case "external_required":
      return "외부 설치 필요";
    case "disabled":
      return "비활성";
  }
}

function runtimeStateTone(
  loadState: RuntimeReadinessLoadState,
  component: ShellRuntimeComponent | null,
): "success" | "warning" | "danger" | "neutral" {
  if (loadState !== "loaded" || !component) return "neutral";
  if (component.state === "ready") return "success";
  if (component.state === "failed") return "danger";
  if (
    component.state === "incomplete" ||
    component.state === "unavailable" ||
    component.state === "external_required"
  ) {
    return "warning";
  }
  return "neutral";
}

const NAV_ITEMS: Array<{
  id: ScreenId;
  label: string;
  compactLabel: string;
  shortcut: string;
  icon: typeof FolderKanban;
}> = [
  {
    id: "home",
    label: "프로젝트 홈",
    compactLabel: "홈",
    shortcut: "1",
    icon: FolderKanban,
  },
  {
    id: "import",
    label: "영상 가져오기",
    compactLabel: "영상",
    shortcut: "2",
    icon: Import,
  },
  {
    id: "label",
    label: "질문 라벨링",
    compactLabel: "질문",
    shortcut: "3",
    icon: Tags,
  },
  {
    id: "analysis",
    label: "분석",
    compactLabel: "진행",
    shortcut: "4",
    icon: Activity,
  },
  {
    id: "review",
    label: "사건 검토",
    compactLabel: "사건",
    shortcut: "5",
    icon: ClipboardCheck,
  },
  {
    id: "models",
    label: "모델 관리",
    compactLabel: "모델",
    shortcut: "6",
    icon: BrainCircuit,
  },
  {
    id: "history",
    label: "GPS 히스토리",
    compactLabel: "지도",
    shortcut: "7",
    icon: History,
  },
  {
    id: "report",
    label: "보고서",
    compactLabel: "보고서",
    shortcut: "8",
    icon: FileText,
  },
  {
    id: "export",
    label: "내보내기",
    compactLabel: "내보내기",
    shortcut: "9",
    icon: FileOutput,
  },
];

export function AppShell({
  activeScreen,
  onScreenChange,
  onScreenPrefetch,
  navOpen,
  onNavOpenChange,
  diagnosticsOpen,
  onDiagnosticsOpenChange,
  children,
}: {
  activeScreen: ScreenId;
  onScreenChange: (screen: ScreenId) => void;
  onScreenPrefetch: (screen: ScreenId) => void;
  navOpen: boolean;
  onNavOpenChange: (open: boolean) => void;
  diagnosticsOpen: boolean;
  onDiagnosticsOpenChange: (open: boolean) => void;
  children: ReactNode;
}) {
  const runtime = useRuntimeState();
  const pipeline = usePipelineUi();
  const projectCatalog = useProjectCatalog();
  const hydratedAnalysisJobRef = useRef<string | null>(null);
  const pipelineAutoNavigationRef = useRef<string | null>(null);
  const appShellRef = useRef<HTMLDivElement>(null);
  const diagnosticsDrawerRef = useRef<HTMLElement>(null);
  const diagnosticsReturnFocusRef = useRef<HTMLElement | null>(null);
  const [mobileNav, setMobileNav] = useState(() =>
    window.matchMedia(MOBILE_NAV_QUERY).matches,
  );
  const [bridgeHealthy, setBridgeHealthy] = useState<boolean | null>(null);
  const [providerState, setProviderState] = useState<{
    codex: ShellProviderStatus | null;
    claude: ShellProviderStatus | null;
  }>({ codex: null, claude: null });
  const [hwpAvailable, setHwpAvailable] = useState<boolean | null>(null);
  const [runtimeReadinessLoadState, setRuntimeReadinessLoadState] =
    useState<RuntimeReadinessLoadState>("checking");
  const [runtimeComponents, setRuntimeComponents] = useState<
    ShellRuntimeComponent[]
  >([]);

  useEffect(() => {
    if (!window.trafficSuperVision) return;
    void refreshProjects().catch(() => {
      // The project selector exposes the catalog error without blocking the shell.
    });
  }, []);

  useEffect(() => {
    const projectId = projectCatalog.activeProjectId;
    if (!window.trafficSuperVision?.pipeline || !projectId) {
      resetPipelineUiState();
      return;
    }
    let active = true;
    void (async () => {
      const pointer = pipelinePointerForProject(projectId);
      if (pointer) {
        try {
          const restored = parsePipelineJob(
            await window.trafficSuperVision!.pipeline.get({
              projectId,
              jobId: pointer.jobId,
            }),
          );
          if (active) markPipelineHydrated(projectId, restored);
          return;
        } catch {
          // A stale renderer pointer is never trusted. Fall back to the
          // bridge-owned project list and persist only its opaque identity.
        }
      }
      const jobs = parsePipelineList(
        await window.trafficSuperVision!.pipeline.list({ projectId }),
      ).sort(
        (left, right) =>
          Date.parse(right.updatedAt) - Date.parse(left.updatedAt),
      );
      if (active) markPipelineHydrated(projectId, jobs[0] ?? null);
    })().catch((error) => {
      if (!active) return;
      publishPipelineError(
        projectId,
        error instanceof Error
          ? error.message
          : "자동 파이프라인 기록을 복구하지 못했습니다.",
      );
    });
    return () => {
      active = false;
    };
  }, [projectCatalog.activeProjectId]);

  useEffect(() => {
    const projectId = projectCatalog.activeProjectId;
    const job =
      pipeline.projectId === projectId ? pipeline.job : null;
    if (
      !window.trafficSuperVision?.pipeline ||
      !projectId ||
      !job ||
      !["queued", "running", "cancelling"].includes(job.status)
    ) {
      return;
    }
    let active = true;
    let fetching = false;
    const refresh = () => {
      if (!active || fetching) return;
      fetching = true;
      void window.trafficSuperVision!.pipeline
        .get({ projectId, jobId: job.jobId })
        .then(parsePipelineJob)
        .then((next) => {
          if (active) publishPipelineJob(next);
        })
        .catch((error) => {
          if (!active) return;
          publishPipelineError(
            projectId,
            error instanceof Error
              ? error.message
              : "자동 파이프라인 상태를 읽지 못했습니다.",
          );
        })
        .finally(() => {
          fetching = false;
        });
    };
    const timer = window.setInterval(refresh, 800);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [
    pipeline.job?.jobId,
    pipeline.job?.status,
    pipeline.job?.updatedAt,
    pipeline.projectId,
    projectCatalog.activeProjectId,
  ]);

  useEffect(() => {
    const projectId = projectCatalog.activeProjectId;
    const job =
      pipeline.projectId === projectId ? pipeline.job : null;
    if (
      !window.trafficSuperVision?.pipeline ||
      !projectId ||
      !job ||
      job.status !== "completed"
    ) {
      return;
    }
    let active = true;
    void window.trafficSuperVision.pipeline
      .list({ projectId })
      .then(parsePipelineList)
      .then((jobs) => {
        if (!active) return;
        const priority = (status: string) => {
          if (status === "awaiting_review") return 0;
          if (status === "blocked" || status === "failed") return 1;
          if (status === "running" || status === "cancelling") return 2;
          if (status === "queued") return 3;
          return 4;
        };
        const next = jobs
          .filter(
            (candidate) =>
              candidate.jobId !== job.jobId &&
              candidate.snapshotId === job.snapshotId &&
              candidate.status !== "completed" &&
              candidate.status !== "cancelled",
          )
          .sort(
            (left, right) =>
              priority(left.status) - priority(right.status) ||
              Date.parse(left.createdAt) - Date.parse(right.createdAt),
          )[0];
        if (next) publishPipelineJob(next);
      })
      .catch(() => {
        // A completed item remains selected if the next folder item cannot be
        // refreshed. The durable server queue is unaffected.
      });
    return () => {
      active = false;
    };
  }, [
    pipeline.job?.jobId,
    pipeline.job?.snapshotId,
    pipeline.job?.status,
    pipeline.job?.updatedAt,
    pipeline.projectId,
    projectCatalog.activeProjectId,
  ]);

  useEffect(() => {
    const job =
      pipeline.projectId === projectCatalog.activeProjectId
        ? pipeline.job
        : null;
    if (!job || job.status !== "awaiting_review") return;
    const navigationKey = `${job.jobId}:${job.updatedAt}`;
    if (pipelineAutoNavigationRef.current === navigationKey) return;
    pipelineAutoNavigationRef.current = navigationKey;
    if (activeScreen !== "label") onScreenChange("label");
  }, [
    activeScreen,
    onScreenChange,
    pipeline.job,
    pipeline.projectId,
    projectCatalog.activeProjectId,
  ]);

  useEffect(() => {
    const projectId = projectCatalog.activeProjectId;
    const job =
      pipeline.projectId === projectId ? pipeline.job : null;
    if (!window.trafficSuperVision || !projectId || !job) return;
    const binding = pipelineQuestionQueueBinding(job);
    if (!binding) return;
    if (
      runtime.analysisSummary?.jobId === binding.analysisJobId &&
      runtime.analysisSummary.sourceSha256 === binding.sourceSha256
    ) {
      return;
    }
    let active = true;
    void window.trafficSuperVision.analysis
      .get({ projectId, jobId: binding.analysisJobId })
      .then(parseAnalysisJob)
      .then((persisted) => {
        if (
          !active ||
          persisted.status !== "completed" ||
          persisted.result === null ||
          persisted.project_id !== projectId ||
          persisted.source_sha256 !== binding.sourceSha256
        ) {
          throw new Error(
            "파이프라인 질문 큐와 분석 증거의 provenance가 일치하지 않습니다.",
          );
        }
        const restored = toRestoredRuntime(persisted);
        hydratedAnalysisJobRef.current =
          `${projectId}:${binding.analysisJobId}`;
        restoreCompletedAnalysis(
          restored.activeImport,
          restored.analysisSummary,
        );
      })
      .catch((error) => {
        if (!active) return;
        publishPipelineError(
          projectId,
          error instanceof Error
            ? error.message
            : "파이프라인 질문 증거를 복구하지 못했습니다.",
        );
      });
    return () => {
      active = false;
    };
  }, [
    pipeline.job,
    pipeline.projectId,
    projectCatalog.activeProjectId,
    runtime.analysisSummary?.jobId,
    runtime.analysisSummary?.sourceSha256,
  ]);

  useEffect(() => {
    const mediaQuery = window.matchMedia(MOBILE_NAV_QUERY);
    const updateMobileNav = (event: MediaQueryListEvent) => {
      setMobileNav(event.matches);
    };
    setMobileNav(mediaQuery.matches);
    mediaQuery.addEventListener("change", updateMobileNav);
    return () => mediaQuery.removeEventListener("change", updateMobileNav);
  }, []);

  useEffect(() => {
    let active = true;
    if (!window.trafficSuperVision) {
      setBridgeHealthy(false);
      return () => {
        active = false;
      };
    }
    window.trafficSuperVision
      .health()
      .then((response) => {
        if (!active) return;
        const value = response as { status?: string; state?: string };
        setBridgeHealthy(value.status === "ok" || value.state === "ready");
      })
      .catch(() => {
        if (active) setBridgeHealthy(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!window.trafficSuperVision) return;
    const activeProjectId = projectCatalog.activeProjectId;
    if (!activeProjectId) return;
    const currentSummary = runtime.analysisSummary;
    const currentImport = runtime.activeImport;
    if (
      (currentSummary && currentSummary.projectId !== activeProjectId) ||
      (currentImport && currentImport.projectId !== activeProjectId)
    ) {
      hydratedAnalysisJobRef.current = null;
      clearRuntime();
      return;
    }
    if (!currentSummary && currentImport) return;
    const hydrationKey = `${activeProjectId}:${
      currentSummary?.jobId ?? "__latest__"
    }`;
    if (hydratedAnalysisJobRef.current === hydrationKey) return;
    hydratedAnalysisJobRef.current = hydrationKey;
    let active = true;
    void (async () => {
      if (currentSummary) {
        const response = await window.trafficSuperVision!.analysis.get({
          projectId: currentSummary.projectId,
          jobId: currentSummary.jobId,
        });
        const persisted = parseAnalysisJob(response);
        if (
          !active ||
          persisted.status !== "completed" ||
          persisted.result === null
        ) {
          return;
        }
        const restored = toRestoredRuntime(persisted);
        restoreCompletedAnalysis(
          restored.activeImport,
          restored.analysisSummary,
        );
        return;
      }
      const response = await window.trafficSuperVision!.analysis.list({
        projectId: activeProjectId,
      });
      const latest = parseAnalysisJobList(response).find(
        (item) => item.status === "completed" && item.result !== null,
      );
      if (!active || !latest) return;
      hydratedAnalysisJobRef.current = `${activeProjectId}:${latest.job_id}`;
      const restored = toRestoredRuntime(latest);
      restoreCompletedAnalysis(
        restored.activeImport,
        restored.analysisSummary,
      );
    })().catch(() => {
      // The shell remains usable when no persisted analysis can be restored.
    });
    return () => {
      active = false;
    };
  }, [
    runtime.activeImport?.snapshotId,
    runtime.analysisSummary?.jobId,
    runtime.analysisSummary?.projectId,
    projectCatalog.activeProjectId,
  ]);

  useEffect(() => {
    const summary = runtime.analysisSummary;
    if (!summary || !window.trafficSuperVision) return;
    let active = true;
    void window.trafficSuperVision.candidateQuestions
      .list({ projectId: summary.projectId, jobId: summary.jobId })
      .then((value) => {
        if (!active) return;
        saveReviewHistory(
          reviewStorageKey(summary.projectId, summary.jobId),
          reviewRecordsFromQuestionList(value, summary.jobId),
        );
      })
      .catch(() => {
        // A failed hydration must not replace an existing local review cache.
      });
    return () => {
      active = false;
    };
  }, [
    runtime.analysisSummary?.jobId,
    runtime.analysisSummary?.projectId,
  ]);

  useEffect(() => {
    let active = true;
    if (!window.trafficSuperVision) return;
    void window.trafficSuperVision.providers
      .list()
      .then((response) => {
        if (!active) return;
        const rows = (
          response as {
            providers?: Array<{
              provider?: string;
              state?: string;
              version?: string;
              diagnostic_state?: string;
              authentication_state?: string;
              network_state?: string;
            }>;
          }
        ).providers;
        if (!Array.isArray(rows)) return;
        const summary = (
          row:
            | {
                state?: string;
                version?: string;
                diagnostic_state?: string;
                authentication_state?: string;
                network_state?: string;
              }
            | undefined,
        ): ShellProviderStatus | null =>
          row && typeof row.state === "string"
            ? {
                state: row.state,
                version:
                  typeof row.version === "string" ? row.version : null,
                diagnosticState:
                  typeof row.diagnostic_state === "string"
                    ? row.diagnostic_state
                    : "unknown",
                authenticationState:
                  typeof row.authentication_state === "string"
                    ? row.authentication_state
                    : "unknown",
                networkState:
                  typeof row.network_state === "string"
                    ? row.network_state
                    : "unknown",
              }
            : null;
        setProviderState({
          codex: summary(rows.find((row) => row.provider === "codex")),
          claude: summary(rows.find((row) => row.provider === "claude")),
        });
      })
      .catch(() => {
        if (active) setProviderState({ codex: null, claude: null });
      });
    void window.trafficSuperVision.hwp
      .preflight()
      .then((response) => {
        if (active) {
          setHwpAvailable((response as { available?: boolean }).available === true);
        }
      })
      .catch(() => {
        if (active) setHwpAvailable(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!diagnosticsOpen) return;
    let active = true;
    const readiness = window.trafficSuperVision?.runtime?.readiness;
    setRuntimeReadinessLoadState("checking");
    setRuntimeComponents([]);
    if (!readiness) {
      setRuntimeReadinessLoadState("unavailable");
      return;
    }
    void readiness()
      .then((response) => {
        if (!active) return;
        const parsed = parseRuntimeReadiness(response);
        if (!parsed) {
          setRuntimeReadinessLoadState("unavailable");
          return;
        }
        setRuntimeComponents(parsed);
        setRuntimeReadinessLoadState("loaded");
      })
      .catch(() => {
        if (!active) return;
        setRuntimeComponents([]);
        setRuntimeReadinessLoadState("unavailable");
      });
    return () => {
      active = false;
    };
  }, [diagnosticsOpen]);

  useEffect(() => {
    if (!diagnosticsOpen) return;
    const drawer = diagnosticsDrawerRef.current;
    const background = appShellRef.current;
    if (!drawer || !background) return;

    diagnosticsReturnFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : document.querySelector<HTMLElement>("#diagnostics-trigger");

    const backgroundHadInert = background.hasAttribute("inert");
    const previousAriaHidden = background.getAttribute("aria-hidden");
    const previousBodyOverflow = document.body.style.overflow;

    drawer.focus({ preventScroll: true });
    background.setAttribute("inert", "");
    background.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "hidden";

    const focusFirstControl = window.requestAnimationFrame(() => {
      const first = drawer.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
      (first ?? drawer).focus({ preventScroll: true });
    });

    const focusableControls = () =>
      Array.from(
        drawer.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter(
        (element) =>
          !element.hasAttribute("hidden") &&
          element.getAttribute("aria-hidden") !== "true",
      );

    const onDialogKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onDiagnosticsOpenChange(false);
        return;
      }
      if (event.key !== "Tab") return;

      const controls = focusableControls();
      if (controls.length === 0) {
        event.preventDefault();
        drawer.focus({ preventScroll: true });
        return;
      }
      const first = controls[0];
      const last = controls[controls.length - 1];
      const activeElement = document.activeElement;
      const focusIsOutside = !drawer.contains(activeElement);
      if (event.shiftKey && (activeElement === first || focusIsOutside)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (activeElement === last || focusIsOutside)) {
        event.preventDefault();
        first.focus();
      }
    };

    const onDocumentFocus = (event: FocusEvent) => {
      if (drawer.contains(event.target as Node)) return;
      const first = focusableControls()[0];
      (first ?? drawer).focus({ preventScroll: true });
    };

    document.addEventListener("keydown", onDialogKeyDown, true);
    document.addEventListener("focusin", onDocumentFocus, true);
    return () => {
      window.cancelAnimationFrame(focusFirstControl);
      document.removeEventListener("keydown", onDialogKeyDown, true);
      document.removeEventListener("focusin", onDocumentFocus, true);
      if (!backgroundHadInert) background.removeAttribute("inert");
      if (previousAriaHidden === null) {
        background.removeAttribute("aria-hidden");
      } else {
        background.setAttribute("aria-hidden", previousAriaHidden);
      }
      document.body.style.overflow = previousBodyOverflow;
      const returnTarget = diagnosticsReturnFocusRef.current;
      diagnosticsReturnFocusRef.current = null;
      window.requestAnimationFrame(() => {
        if (returnTarget?.isConnected) returnTarget.focus({ preventScroll: true });
      });
    };
  }, [diagnosticsOpen, onDiagnosticsOpenChange]);

  const currentPipeline =
    pipeline.projectId === projectCatalog.activeProjectId
      ? pipeline.job
      : null;
  const analysisBadge = currentPipeline
    ? currentPipeline.status === "blocked"
      ? currentPipeline.outcomeCode === "partial"
        ? "산출"
        : "모델"
      : currentPipeline.status === "failed"
        ? "오류"
        : currentPipeline.status === "awaiting_review"
          ? "검토"
          : currentPipeline.status === "completed"
            ? "완료"
            : `${currentPipeline.progressPercent}%`
    : runtime.status === "running" || runtime.status === "paused"
      ? `${Math.round(runtime.progress)}%`
      : runtime.status === "complete"
        ? "완료"
        : runtime.status === "failed"
          ? "오류"
          : runtime.activeImport
            ? "준비"
            : undefined;
  const labelBadge =
    currentPipeline?.status === "awaiting_review"
      ? "대기"
      : runtime.analysisSummary
        ? String(runtime.analysisSummary.candidateCount)
        : undefined;

  function badgeFor(screen: ScreenId) {
    if (screen === "analysis") return analysisBadge;
    if (screen === "label") return labelBadge;
    if (screen === "models") return "초기";
    return undefined;
  }

  const providerReady =
    providerState.codex?.state === "ready" ||
    providerState.claude?.state === "ready";
  const runtimeComponentsById = new Map(
    runtimeComponents.map((component) => [component.componentId, component]),
  );
  const ffmpegComponent = runtimeComponentsById.get("ffmpeg") ?? null;
  const analysisRuntimeRows = ANALYSIS_RUNTIME_COMPONENTS.map(
    ([componentId, label]) => ({
      componentId,
      label,
      component: runtimeComponentsById.get(componentId) ?? null,
    }),
  );
  const analysisRuntimeReadyCount = analysisRuntimeRows.filter(
    ({ component }) => component?.state === "ready",
  ).length;
  const analysisRuntimeHasFailure = analysisRuntimeRows.some(
    ({ component }) => component?.state === "failed",
  );
  const analysisRuntimeTone =
    runtimeReadinessLoadState === "checking"
      ? "neutral"
      : runtimeReadinessLoadState === "unavailable" ||
          analysisRuntimeHasFailure
        ? "danger"
        : analysisRuntimeReadyCount === analysisRuntimeRows.length
          ? "success"
          : "warning";
  const analysisRuntimeSummary =
    runtimeReadinessLoadState === "checking"
      ? "확인 중"
      : runtimeReadinessLoadState === "unavailable"
        ? "확인 불가"
        : analysisRuntimeReadyCount === analysisRuntimeRows.length
          ? `${analysisRuntimeReadyCount}개 모두 준비됨`
          : `${analysisRuntimeReadyCount}개 준비 · ${
              analysisRuntimeRows.length - analysisRuntimeReadyCount
            }개 미준비`;
  const activeProject = projectCatalog.projects.find(
    (project) => project.id === projectCatalog.activeProjectId,
  );
  const activeNavigationItem =
    NAV_ITEMS.find((item) => item.id === activeScreen) ?? NAV_ITEMS[0];
  const activeProjectSecondaryStatus = currentPipeline
    ? `${pipelineStatusLabel(currentPipeline)} · ${currentPipeline.progressPercent}%`
    : runtime.activeImport
      ? runtime.activeImport.fileName
      : projectCatalog.error ?? "입력 대기";

  return (
    <div className="app-canvas">
      <a className="skip-link" href="#main-content">
        본문 바로가기
      </a>
      <div className="ambient ambient--one" aria-hidden="true" />
      <div className="ambient ambient--two" aria-hidden="true" />
      <div ref={appShellRef} className="app-shell">
        <header className="titlebar">
          <div className="brand">
            <IconButton
              label={navOpen ? "내비게이션 접기" : "내비게이션 펼치기"}
              onClick={() => onNavOpenChange(!navOpen)}
              className="mobile-menu"
            >
              <Menu size={19} />
            </IconButton>
            <span className="brand-mark" aria-hidden="true">
              <img
                src="/trafficsupervision-app-icon.png"
                alt=""
                width={96}
                height={96}
                decoding="async"
                draggable={false}
              />
            </span>
            <span className="brand-name">TrafficSuperVision</span>
          </div>

          <label
            className="project-switcher"
            title={
              projectCatalog.error ??
              "프로젝트를 바꾸면 해당 프로젝트의 최근 분석을 불러옵니다."
            }
          >
            <span>
              <strong>{activeProject?.name ?? "프로젝트 불러오는 중"}</strong>
              <small>{activeProjectSecondaryStatus}</small>
            </span>
            <select
              aria-label="활성 프로젝트"
              value={projectCatalog.activeProjectId ?? ""}
              disabled={
                projectCatalog.loading || projectCatalog.projects.length === 0
              }
              onChange={(event) => {
                if (!event.target.value) return;
                selectProject(event.target.value);
                hydratedAnalysisJobRef.current = null;
                pipelineAutoNavigationRef.current = null;
                clearRuntime();
                resetPipelineUiState();
                // Hand keyboard ownership back to the active workbench after
                // a deliberate project switch. A focused native select would
                // otherwise consume Y/N/U/E or 1/2/3 review shortcuts.
                event.currentTarget.blur();
              }}
            >
              {projectCatalog.projects.length === 0 ? (
                <option value="">프로젝트 없음</option>
              ) : null}
              {projectCatalog.projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
            <ChevronDown size={15} aria-hidden="true" />
          </label>

          <div
            className="shell-screen-context"
            aria-label={`현재 작업 화면: ${activeNavigationItem.label}`}
          >
            <span>현재 화면</span>
            <strong>{activeNavigationItem.label}</strong>
            <kbd aria-label={`${activeNavigationItem.shortcut}번 단축키`}>
              {activeNavigationItem.shortcut}
            </kbd>
          </div>

          <div className="runtime-strip" aria-label="현재 작업 상태">
            <StatusDot tone={bridgeHealthy ? "success" : "neutral"}>
              {bridgeHealthy ? "로컬 준비" : "로컬 확인 중"}
            </StatusDot>
            <StatusDot
              tone={
                currentPipeline?.status === "running" ||
                runtime.status === "running"
                  ? "accent"
                  : currentPipeline?.status === "blocked" ||
                      currentPipeline?.status === "failed"
                    ? "warning"
                    : "neutral"
              }
            >
              {currentPipeline
                ? `${pipelineStatusLabel(currentPipeline)} · ${currentPipeline.progressPercent}%`
                : runtime.status === "running"
                ? `분석 ${Math.round(runtime.progress)}%`
                : runtime.analysisSummary
                  ? `검토 ${runtime.analysisSummary.candidateCount}건`
                  : "입력 대기"}
            </StatusDot>
          </div>

          <IconButton
            id="diagnostics-trigger"
            label="연결·환경 상태 열기"
            className="shell-diagnostics-button"
            onClick={() => onDiagnosticsOpenChange(true)}
          >
            <CircleGauge size={17} />
            <span>환경 상태</span>
          </IconButton>
        </header>

        <div className="app-body">
          <nav
            className={`side-nav ${navOpen ? "side-nav--open" : ""}`}
            aria-label="주요 화면"
            aria-hidden={mobileNav && !navOpen ? true : undefined}
            inert={mobileNav && !navOpen}
          >
            <div className="nav-heading">
              <span>작업 메뉴</span>
              {navOpen ? (
                <IconButton
                  label="내비게이션 닫기"
                  className="nav-close"
                  onClick={() => onNavOpenChange(false)}
                >
                  <X size={17} />
                </IconButton>
              ) : null}
            </div>
            <div className="nav-list">
              {NAV_ITEMS.map((item) => {
                const Icon = item.icon;
                const active = activeScreen === item.id;
                return (
                  <button
                    key={item.id}
                    type="button"
                    className={`nav-item ${active ? "nav-item--active" : ""}`}
                    aria-label={`${item.label} (${item.shortcut})`}
                    aria-current={active ? "page" : undefined}
                    onPointerEnter={() => onScreenPrefetch(item.id)}
                    onFocus={() => onScreenPrefetch(item.id)}
                    onClick={() => {
                      onScreenChange(item.id);
                      if (window.innerWidth < 960) onNavOpenChange(false);
                    }}
                    title={`${item.label} (${item.shortcut})`}
                  >
                    <span className="nav-icon">
                      <Icon size={20} strokeWidth={1.8} aria-hidden="true" />
                    </span>
                    <span className="nav-label">{item.label}</span>
                    <span className="nav-label-compact" aria-hidden="true">
                      {item.compactLabel}
                    </span>
                    {badgeFor(item.id) ? (
                      <span className="nav-badge">
                        {badgeFor(item.id)}
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>

            <div className="nav-project-health">
              <CircleGauge size={18} aria-hidden="true" />
              <div>
                <strong>프로젝트 상태</strong>
                <span>
                  {currentPipeline
                    ? `${currentPipeline.fileName} · ${pipelineStatusLabel(currentPipeline)}`
                    : runtime.activeImport
                    ? `${runtime.activeImport.fileName} · ${runtime.message}`
                    : "검증된 입력 대기"}
                </span>
              </div>
            </div>
          </nav>

          <main className="workspace" id="main-content" tabIndex={-1}>
            {children}
          </main>
        </div>

        <footer className="statusbar">
          <div>
            <Activity size={14} aria-hidden="true" />
            <span>
              {currentPipeline
                ? `${currentPipeline.fileName} · ${pipelineStatusLabel(currentPipeline)}`
                : runtime.activeImport
                ? `${runtime.activeImport.fileName} · ${runtime.message}`
                : "입력 대기 · 영상 가져오기를 시작하세요"}
            </span>
            {currentPipeline || runtime.activeImport ? (
              <span className="statusbar-progress" aria-hidden="true">
                <i
                  style={{
                    width: `${
                      currentPipeline
                        ? currentPipeline.progressPercent
                        : runtime.progress
                    }%`,
                  }}
                />
              </span>
            ) : null}
          </div>
          <div>
            <HardDrive size={14} aria-hidden="true" />
            <span>
              {currentPipeline
                ? currentPipeline.status === "blocked"
                  ? currentPipeline.outcomeCode === "partial"
                    ? "CSV·증거 저장 완료 · 보고서 준비 경계에서 재개"
                    : "빈 성공 아님 · 필요한 로컬 모델팩을 설치한 뒤 재개"
                  : "한 PipelineJob · 재시작 복구 · 프로젝트 provenance"
                : runtime.status === "running"
                ? "로컬 후보 추출 · 장면 변화·선명도"
                : "분석 산출물·라벨은 로컬 프로젝트에 보관"}
            </span>
          </div>
        </footer>
      </div>

      {diagnosticsOpen ? (
        <div
          className="drawer-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (event.currentTarget === event.target) onDiagnosticsOpenChange(false);
          }}
        >
          <aside
            ref={diagnosticsDrawerRef}
            id="diagnostics-drawer"
            className="diagnostics-drawer"
            role="dialog"
            aria-modal="true"
            aria-labelledby="diagnostics-title"
            tabIndex={-1}
          >
            <header>
              <div>
                <p className="eyebrow">진단</p>
                <h2 id="diagnostics-title">로컬 실행 상태</h2>
              </div>
              <IconButton label="진단 패널 닫기" onClick={() => onDiagnosticsOpenChange(false)}>
                <X size={18} />
              </IconButton>
            </header>
            <div className="diagnostic-card">
              <StatusDot tone={bridgeHealthy ? "success" : "neutral"}>
                Python bridge · {bridgeHealthy ? "연결됨" : "확인되지 않음"}
              </StatusDot>
              <code>
                {bridgeHealthy
                  ? "127.0.0.1 · 임시 포트 · 토큰 보호"
                  : "Electron 실행 시 실제 상태를 확인합니다."}
              </code>
            </div>
            <div className="diagnostic-card">
              <StatusDot
                tone={runtimeStateTone(
                  runtimeReadinessLoadState,
                  ffmpegComponent,
                )}
              >
                FFmpeg ·{" "}
                {runtimeStateLabel(
                  runtimeReadinessLoadState,
                  ffmpegComponent,
                )}
              </StatusDot>
              <code>
                {runtimeReadinessLoadState === "checking"
                  ? "고정 실행 파일과 로컬 미디어 처리 구성을 확인하고 있습니다."
                  : runtimeReadinessLoadState === "unavailable"
                    ? "런타임 준비 상태를 읽지 못했습니다. Python bridge 연결을 확인하세요."
                    : ffmpegComponent?.message ??
                      "응답에 FFmpeg 준비 상태가 없습니다."}
              </code>
            </div>
            <div className="diagnostic-card diagnostic-card--runtime">
              <StatusDot tone={analysisRuntimeTone}>
                로컬 분석 구성 · {analysisRuntimeSummary}
              </StatusDot>
              <ul className="diagnostic-runtime-list" aria-label="로컬 분석 구성">
                {analysisRuntimeRows.map(
                  ({ componentId, label, component }) => (
                    <li key={componentId} title={component?.message}>
                      <span>{label}</span>
                      <strong
                        className={`diagnostic-runtime-state diagnostic-runtime-state--${
                          component?.state ?? runtimeReadinessLoadState
                        }`}
                      >
                        {runtimeStateLabel(
                          runtimeReadinessLoadState,
                          component,
                        )}
                        {component?.version ? ` · ${component.version}` : ""}
                      </strong>
                    </li>
                  ),
                )}
              </ul>
              <code>
                {runtimeReadinessLoadState === "checking"
                  ? "설치본과 Model Pack 무결성 상태를 읽고 있습니다."
                  : runtimeReadinessLoadState === "unavailable"
                    ? "준비 상태를 확인하지 못했으며 미설치를 완료로 간주하지 않습니다."
                    : "준비됨은 해당 구성의 로컬 무결성 계약을 통과한 상태입니다."}
              </code>
            </div>
            <div className="diagnostic-card">
              <StatusDot tone={hwpAvailable ? "success" : "neutral"}>
                한/글 COM ·{" "}
                {hwpAvailable === null
                  ? "확인 중"
                  : hwpAvailable
                    ? "준비됨"
                    : "사용 불가"}
              </StatusDot>
              <code>설치된 한/글과 보안 모듈 등록 상태를 읽기 전용으로 확인합니다.</code>
            </div>
            <div className="diagnostic-card">
              <StatusDot tone={providerReady ? "success" : "neutral"}>
                AI CLI · {providerReady ? "연결됨" : "미연결"}
              </StatusDot>
              <p>
                Codex{" "}
                {providerState.codex
                  ? `${providerState.codex.version ?? "버전 미확인"} · ${
                      providerState.codex.diagnosticState
                    } · 인증 ${providerState.codex.authenticationState} · 네트워크 ${
                      providerState.codex.networkState
                    }`
                  : "미확인"}
                <br />
                Claude{" "}
                {providerState.claude
                  ? `${providerState.claude.version ?? "버전 미확인"} · ${
                      providerState.claude.diagnosticState
                    } · 인증 ${providerState.claude.authenticationState} · 네트워크 ${
                      providerState.claude.networkState
                    }`
                  : "미확인"}
                . 인증 파일은 읽지 않습니다.
              </p>
            </div>
            <div className="diagnostic-card diagnostic-card--quiet">
              <CircleGauge size={18} aria-hidden="true" />
              <p>개발 진단 정보는 기본 작업 화면의 너비를 차지하지 않습니다.</p>
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
