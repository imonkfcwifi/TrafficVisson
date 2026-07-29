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
  Settings2,
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

const NAV_ITEMS: Array<{
  id: ScreenId;
  label: string;
  shortcut: string;
  icon: typeof FolderKanban;
}> = [
  { id: "home", label: "프로젝트 홈", shortcut: "1", icon: FolderKanban },
  { id: "import", label: "영상 가져오기", shortcut: "2", icon: Import },
  { id: "label", label: "질문 라벨링", shortcut: "3", icon: Tags },
  { id: "analysis", label: "분석", shortcut: "4", icon: Activity },
  { id: "review", label: "사건 검토", shortcut: "5", icon: ClipboardCheck },
  { id: "models", label: "모델 관리", shortcut: "6", icon: BrainCircuit },
  { id: "history", label: "GPS 히스토리", shortcut: "7", icon: History },
  { id: "report", label: "보고서", shortcut: "8", icon: FileText },
  { id: "export", label: "내보내기", shortcut: "9", icon: FileOutput },
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
  const projectCatalog = useProjectCatalog();
  const hydratedAnalysisJobRef = useRef<string | null>(null);
  const appShellRef = useRef<HTMLDivElement>(null);
  const diagnosticsDrawerRef = useRef<HTMLElement>(null);
  const diagnosticsReturnFocusRef = useRef<HTMLElement | null>(null);
  const [mobileNav, setMobileNav] = useState(() =>
    window.matchMedia(MOBILE_NAV_QUERY).matches,
  );
  const [bridgeHealthy, setBridgeHealthy] = useState<boolean | null>(null);
  const [providerState, setProviderState] = useState<{
    codex: string | null;
    claude: string | null;
  }>({ codex: null, claude: null });
  const [hwpAvailable, setHwpAvailable] = useState<boolean | null>(null);

  useEffect(() => {
    if (!window.trafficVisson) return;
    void refreshProjects().catch(() => {
      // The project selector exposes the catalog error without blocking the shell.
    });
  }, []);

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
    if (!window.trafficVisson) {
      setBridgeHealthy(false);
      return () => {
        active = false;
      };
    }
    window.trafficVisson
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
    if (!window.trafficVisson) return;
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
        const response = await window.trafficVisson!.analysis.get({
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
      const response = await window.trafficVisson!.analysis.list({
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
    if (!summary || !window.trafficVisson) return;
    let active = true;
    void window.trafficVisson.candidateQuestions
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
    if (!window.trafficVisson) return;
    void window.trafficVisson.providers
      .list()
      .then((response) => {
        if (!active) return;
        const rows = (
          response as {
            providers?: Array<{ provider?: string; state?: string }>;
          }
        ).providers;
        if (!Array.isArray(rows)) return;
        setProviderState({
          codex: rows.find((row) => row.provider === "codex")?.state ?? null,
          claude: rows.find((row) => row.provider === "claude")?.state ?? null,
        });
      })
      .catch(() => {
        if (active) setProviderState({ codex: null, claude: null });
      });
    void window.trafficVisson.hwp
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

  const analysisBadge =
    runtime.status === "running" || runtime.status === "paused"
      ? `${Math.round(runtime.progress)}%`
      : runtime.status === "complete"
        ? "완료"
        : runtime.status === "failed"
          ? "오류"
          : runtime.activeImport
            ? "준비"
            : undefined;
  const labelBadge = runtime.analysisSummary
    ? String(runtime.analysisSummary.candidateCount)
    : undefined;

  function badgeFor(screen: ScreenId) {
    if (screen === "analysis") return analysisBadge;
    if (screen === "label") return labelBadge;
    if (screen === "models") return "초기";
    return undefined;
  }

  const providerReady =
    providerState.codex === "ready" || providerState.claude === "ready";
  const activeProject = projectCatalog.projects.find(
    (project) => project.id === projectCatalog.activeProjectId,
  );

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
                src="/trafficvisson-app-icon.png"
                alt=""
                width={96}
                height={96}
                decoding="async"
                draggable={false}
              />
            </span>
            <span className="brand-name">TrafficVisson</span>
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
              <small>
                {runtime.activeImport
                  ? runtime.activeImport.fileName
                  : projectCatalog.error ?? "입력 대기"}
              </small>
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
                clearRuntime();
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

          <div className="runtime-strip" aria-label="현재 작업 상태">
            <StatusDot tone={bridgeHealthy ? "success" : "neutral"}>
              {bridgeHealthy ? "로컬 준비" : "로컬 확인 중"}
            </StatusDot>
            <StatusDot tone={runtime.status === "running" ? "accent" : "neutral"}>
              {runtime.status === "running"
                ? `분석 ${Math.round(runtime.progress)}%`
                : runtime.analysisSummary
                  ? `검토 ${runtime.analysisSummary.candidateCount}건`
                  : "입력 대기"}
            </StatusDot>
          </div>

          <IconButton
            id="diagnostics-trigger"
            label="진단 패널 열기"
            onClick={() => onDiagnosticsOpenChange(true)}
          >
            <Settings2 size={19} />
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
                  {runtime.activeImport
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
              {runtime.activeImport
                ? `${runtime.activeImport.fileName} · ${runtime.message}`
                : "입력 대기 · 영상 가져오기를 시작하세요"}
            </span>
            {runtime.activeImport ? (
              <span className="statusbar-progress" aria-hidden="true">
                <i style={{ width: `${runtime.progress}%` }} />
              </span>
            ) : null}
          </div>
          <div>
            <HardDrive size={14} aria-hidden="true" />
            <span>
              {runtime.status === "running"
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
              <StatusDot tone="neutral">FFmpeg · 작업 전</StatusDot>
              <code>미디어 검증을 실행해야 준비 상태를 확정합니다.</code>
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
                Codex {providerState.codex ?? "미확인"} · Claude{" "}
                {providerState.claude ?? "미확인"}. 인증 파일은 읽지 않습니다.
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
