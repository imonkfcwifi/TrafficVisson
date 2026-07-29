import { lazy, Suspense, useEffect, useState } from "react";
import { AppShell } from "./components/AppShell";
import type { ScreenId } from "./demo";
import { HomeScreen } from "./screens/HomeScreen";

function retryableLoader<T>(load: () => Promise<T>) {
  let request: Promise<T> | undefined;
  return () => {
    if (!request) {
      request = load().catch((error) => {
        request = undefined;
        throw error;
      });
    }
    return request;
  };
}

const loadAnalysisScreen = retryableLoader(() =>
  import("./screens/AnalysisScreen").then((module) => ({ default: module.AnalysisScreen })),
);
const loadExportScreen = retryableLoader(() =>
  import("./screens/ExportScreen").then((module) => ({ default: module.ExportScreen })),
);
const loadHistoryScreen = retryableLoader(() =>
  import("./screens/HistoryScreen").then((module) => ({ default: module.HistoryScreen })),
);
const loadImportScreen = retryableLoader(() =>
  import("./screens/ImportScreen").then((module) => ({ default: module.ImportScreen })),
);
const loadLabelingScreen = retryableLoader(() =>
  import("./screens/LabelingScreen").then((module) => ({ default: module.LabelingScreen })),
);
const loadModelScreen = retryableLoader(() =>
  import("./screens/ModelScreen").then((module) => ({ default: module.ModelScreen })),
);
const loadReportScreen = retryableLoader(() =>
  import("./screens/ReportScreen").then((module) => ({ default: module.ReportScreen })),
);
const loadReviewScreen = retryableLoader(() =>
  import("./screens/ReviewScreen").then((module) => ({ default: module.ReviewScreen })),
);

const AnalysisScreen = lazy(loadAnalysisScreen);
const ExportScreen = lazy(loadExportScreen);
const HistoryScreen = lazy(loadHistoryScreen);
const ImportScreen = lazy(loadImportScreen);
const LabelingScreen = lazy(loadLabelingScreen);
const ModelScreen = lazy(loadModelScreen);
const ReportScreen = lazy(loadReportScreen);
const ReviewScreen = lazy(loadReviewScreen);

type PrefetchableScreen = Exclude<ScreenId, "home">;

const SCREEN_LOADERS: Record<PrefetchableScreen, () => Promise<unknown>> = {
  analysis: loadAnalysisScreen,
  export: loadExportScreen,
  history: loadHistoryScreen,
  import: loadImportScreen,
  label: loadLabelingScreen,
  models: loadModelScreen,
  report: loadReportScreen,
  review: loadReviewScreen,
};
const PREFETCHABLE_SCREENS = Object.keys(
  SCREEN_LOADERS,
) as PrefetchableScreen[];

const SCREEN_ORDER: ScreenId[] = [
  "home",
  "import",
  "label",
  "analysis",
  "review",
  "models",
  "history",
  "report",
  "export",
];

type IdleCapableWindow = Window & {
  requestIdleCallback?: (
    callback: () => void,
    options?: { timeout: number },
  ) => number;
  cancelIdleCallback?: (handle: number) => void;
};

function prefetchScreen(screen: ScreenId) {
  if (screen === "home") return;
  void SCREEN_LOADERS[screen]().catch(() => {
    // retryableLoader clears failed requests so a later navigation can retry.
  });
}

function prefetchAllScreens() {
  for (const screen of PREFETCHABLE_SCREENS) prefetchScreen(screen);
}

function screenFromHash(): ScreenId {
  const value = window.location.hash.replace("#/", "") as ScreenId;
  return SCREEN_ORDER.includes(value) ? value : "home";
}

export function App() {
  const [screen, setScreen] = useState<ScreenId>(screenFromHash);
  const [navOpen, setNavOpen] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);

  function navigate(next: ScreenId) {
    prefetchScreen(next);
    setScreen(next);
    window.location.hash = `/${next}`;
    window.requestAnimationFrame(() => {
      const workspace = document.querySelector<HTMLElement>(".workspace");
      workspace?.scrollTo({ top: 0, left: 0, behavior: "auto" });
      workspace?.focus({ preventScroll: true });
    });
  }

  useEffect(() => {
    const onHashChange = () => setScreen(screenFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    const idleWindow = window as IdleCapableWindow;
    let firstPaintFrame = 0;
    let afterPaintFrame = 0;
    let idleHandle: number | undefined;
    let fallbackHandle: number | undefined;
    let disposed = false;

    const runPrefetch = () => {
      if (!disposed) prefetchAllScreens();
    };

    firstPaintFrame = window.requestAnimationFrame(() => {
      afterPaintFrame = window.requestAnimationFrame(() => {
        if (disposed) return;
        if (idleWindow.requestIdleCallback) {
          idleHandle = idleWindow.requestIdleCallback(runPrefetch, {
            timeout: 2_000,
          });
        } else {
          fallbackHandle = window.setTimeout(runPrefetch, 250);
        }
      });
    });

    return () => {
      disposed = true;
      window.cancelAnimationFrame(firstPaintFrame);
      window.cancelAnimationFrame(afterPaintFrame);
      if (idleHandle !== undefined) {
        idleWindow.cancelIdleCallback?.(idleHandle);
      }
      if (fallbackHandle !== undefined) {
        window.clearTimeout(fallbackHandle);
      }
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (diagnosticsOpen) {
        if (event.key === "Escape") setDiagnosticsOpen(false);
        return;
      }
      if (event.key === "Escape") {
        setNavOpen(false);
        return;
      }
      const target = event.target as HTMLElement | null;
      if (
        target?.matches("input, textarea, select, button, [contenteditable='true']") ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey
      ) {
        return;
      }
      const index = Number(event.key) - 1;
      if (index >= 0 && index < SCREEN_ORDER.length) {
        event.preventDefault();
        navigate(SCREEN_ORDER[index]);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [diagnosticsOpen]);

  let content;
  switch (screen) {
    case "import":
      content = <ImportScreen onNavigate={navigate} />;
      break;
    case "label":
      content = <LabelingScreen onNavigate={navigate} />;
      break;
    case "analysis":
      content = <AnalysisScreen onNavigate={navigate} />;
      break;
    case "review":
      content = <ReviewScreen onNavigate={navigate} />;
      break;
    case "models":
      content = <ModelScreen />;
      break;
    case "history":
      content = <HistoryScreen onNavigate={navigate} />;
      break;
    case "report":
      content = <ReportScreen />;
      break;
    case "export":
      content = <ExportScreen />;
      break;
    default:
      content = <HomeScreen onNavigate={navigate} />;
  }

  return (
    <AppShell
      activeScreen={screen}
      onScreenChange={navigate}
      onScreenPrefetch={prefetchScreen}
      navOpen={navOpen}
      onNavOpenChange={setNavOpen}
      diagnosticsOpen={diagnosticsOpen}
      onDiagnosticsOpenChange={setDiagnosticsOpen}
    >
      <div className="screen-announcer" aria-live="polite">
        {screen} 화면
      </div>
      <Suspense fallback={<div className="screen-loading">화면을 준비하는 중…</div>}>
        {content}
      </Suspense>
    </AppShell>
  );
}
