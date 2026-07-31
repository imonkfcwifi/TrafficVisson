import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const sourceRoot = path.dirname(fileURLToPath(import.meta.url));
const read = (relativePath) =>
  fs.readFileSync(path.join(sourceRoot, relativePath), "utf8");

const appSource = read("App.tsx");
const shellSource = read("components/AppShell.tsx");
const analysisSource = read("screens/AnalysisScreen.tsx");
const homeSource = read("screens/HomeScreen.tsx");
const historySource = read("screens/HistoryScreen.tsx");
const importSource = read("screens/ImportScreen.tsx");
const labelingSource = read("screens/LabelingScreen.tsx");
const entrySource = read("main.tsx");
const productStyles = read("styles-product-shell.css");
const polishStyles = read("styles-visual-polish.css");
const appIcon = fs.readFileSync(
  path.join(sourceRoot, "../public/trafficsupervision-app-icon.png"),
);

test("desktop navigation stays compact while exposing legible screen labels and location", () => {
  assert.match(productStyles, /@media \(min-width: 961px\)[\s\S]*#root \.side-nav\s*\{[\s\S]*width:\s*96px/);
  assert.match(productStyles, /\.nav-label-compact\s*\{[\s\S]*display:\s*block/);
  assert.match(
    productStyles,
    /\.nav-label-compact\s*\{[^}]*font-size:\s*11px/s,
  );
  assert.match(
    productStyles,
    /@media \(min-width: 961px\)[\s\S]*?\.nav-section-label\s*\{[^}]*font-size:\s*11px/s,
  );
  assert.match(
    productStyles,
    /\.shell-screen-context > span\s*\{[^}]*font-size:\s*11px/s,
  );
  assert.match(
    productStyles,
    /\.shell-screen-context > kbd\s*\{[^}]*font-size:\s*11px/s,
  );
  assert.match(shellSource, /compactLabel:\s*"영상"/);
  assert.match(shellSource, /compactLabel:\s*"질문"/);
  assert.match(shellSource, /const activeNavigationSection =/);
  assert.match(shellSource, /const currentTaskLabel =/);
  assert.match(shellSource, /useReviewHistory\(/);
  assert.match(
    shellSource,
    /currentQuestionQueue\.questionCount - reviewedQuestionCount/,
  );
  assert.match(shellSource, /남은 질문 \$\{remainingQuestionCount\}건 답변/);
  assert.match(
    shellSource,
    /notifications[\s\S]*questionReviewReady[\s\S]*queueRevision:\s*currentQuestionQueue\.queueRevision/,
  );
  assert.match(
    shellSource,
    /현재 업무 영역: \$\{activeNavigationSection\}, 화면: \$\{activeNavigationItem\.label\}, 지금 할 일: \$\{currentTaskLabel\}/,
  );
  assert.doesNotMatch(shellSource, /업무 화면 \{activeNavigationIndex \+ 1\}/);
  assert.match(shellSource, /<span>\{activeNavigationSection\}<\/span>/);
  assert.match(shellSource, /<strong>\{activeNavigationItem\.label\}<\/strong>/);
  assert.match(
    shellSource,
    /<small title=\{currentTaskLabel\}>\{currentTaskLabel\}<\/small>/,
  );
  assert.match(shellSource, /sectionLabel:\s*"자동 점검"/);
  assert.match(shellSource, /sectionLabel:\s*"결과 확인"/);
  assert.match(shellSource, /sectionLabel:\s*"관리"/);
  assert.match(
    productStyles,
    /@media \(min-width: 961px\) and \(max-height: 800px\)[\s\S]*?\.nav-section-label\s*\{[^}]*min-height:\s*16px;[^}]*color:\s*rgba\(68, 82, 118, 0\.68\);[^}]*font-size:\s*11px/s,
  );
  assert.doesNotMatch(
    productStyles,
    /@media \(min-width: 961px\) and \(max-height: 800px\)[\s\S]*?\.nav-section-label\s*\{[^}]*(?:color:\s*transparent|font-size:\s*0)/s,
  );
  assert.match(
    productStyles,
    /\.nav-item--active::before\s*\{[^}]*background:\s*var\(--accent-action\)/s,
  );
  assert.match(
    productStyles,
    /\.nav-item--active \.nav-icon\s*\{[\s\S]*background:\s*transparent/,
  );
});

test("project switching returns keyboard control to the active workbench", () => {
  assert.match(
    shellSource,
    /selectProject\(event\.target\.value\);[\s\S]*?event\.currentTarget\.blur\(\);/,
  );
});

test("the product header does not expose development or provider jargon", () => {
  assert.doesNotMatch(shellSource, /Desktop alpha/);
  assert.match(shellSource, /로컬 준비/);
  assert.match(shellSource, /현재 작업 상태/);
  assert.match(shellSource, /연결·환경 상태 열기/);
});

test("home review rows are actionable and preserve cross-screen selection", () => {
  assert.match(homeSource, /<button[\s\S]*className="question-list-row"/);
  assert.match(homeSource, /saveSelectedCandidateId\(/);
  assert.match(
    homeSource,
    /\{hasDurableInput \? \([\s\S]*variant="primary"[\s\S]*\{pipelineActionLabel\}/,
  );
  assert.match(
    homeSource,
    /className="question-queue"[\s\S]*variant="ghost"[\s\S]*"질문 열기"/,
  );
});

test("home presents one automatic video pipeline instead of separate manual tools", () => {
  assert.match(homeSource, /영상 한 번으로 도로점검/);
  assert.match(homeSource, /영상 넣고 자동 점검/);
  assert.match(homeSource, /title: "영상 넣기"/);
  assert.match(homeSource, /title: "자동 분석"/);
  assert.match(homeSource, /title: "질문 검토"/);
  assert.match(homeSource, /title: "결과 자동 갱신"/);
  assert.match(homeSource, /중간 분석 버튼을 누를 필요가 없습니다/);
  assert.match(homeSource, /승인 전 사건 0건/);
  assert.equal(
    (homeSource.match(/\{pipelineActionLabel\}/g) ?? []).length,
    1,
  );
  assert.doesNotMatch(homeSource, /프로젝트 입력 열기/);
  assert.doesNotMatch(homeSource, /metric-link-icon/);
});

test("the analysis destination presents a durable automatic PipelineJob, not manual modules", () => {
  assert.match(analysisSource, /function PipelineAnalysisOverview/);
  assert.match(analysisSource, /const pipeline = usePipelineUi\(\)/);
  assert.match(
    analysisSource,
    /const currentPipelineJob =[\s\S]*pipeline\.projectId === projectCatalog\.activeProjectId[\s\S]*pipeline\.job/,
  );
  assert.match(
    analysisSource,
    /if \(currentPipelineJob\) \{[\s\S]*<PipelineAnalysisOverview[\s\S]*job=\{currentPipelineJob\}/,
  );
  const pipelineOverview = analysisSource.slice(
    analysisSource.indexOf("function PipelineAnalysisOverview"),
    analysisSource.indexOf("function SavedAnalysisJobs"),
  );
  assert.match(pipelineOverview, /<h1>자동 분석 진행<\/h1>/);
  assert.match(
    analysisSource,
    /현재 화면을 열어 두지 않아도 단계는 순서대로 진행됩니다\. 중간 분석 버튼을 누를 필요가 없습니다\./,
  );
  assert.match(pipelineOverview, /aria-label="자동 파이프라인 단계"/);
  assert.match(pipelineOverview, /영상[\s\S]*자동 분석[\s\S]*질문 검토[\s\S]*결과 자동 갱신/);
  assert.doesNotMatch(
    pipelineOverview,
    /startActualAnalysis|startDenseIndex|후보 추출 시작|밀집 프레임 준비/,
  );
  const routedAnalysis = analysisSource.slice(
    analysisSource.indexOf("export function AnalysisScreen"),
    analysisSource.indexOf("export function LegacyAnalysisDiagnosticsScreen"),
  );
  assert.match(routedAnalysis, /<PipelineAnalysisOverview/);
  assert.match(routedAnalysis, /연결된 자동 점검 작업 없음/);
  assert.match(routedAnalysis, /영상 가져오기로 이동/);
  assert.match(
    routedAnalysis,
    /이 화면에서는 후보 추출·밀집 프레임·검지 단계를 따로 시작하지\s*않습니다/,
  );
  assert.doesNotMatch(
    routedAnalysis,
    /startActualAnalysis|startDenseIndex|후보 추출 시작|밀집 프레임 준비/,
  );
  assert.doesNotMatch(appSource, /LegacyAnalysisDiagnosticsScreen/);
  assert.match(
    polishStyles,
    /\.screen--pipeline-analysis \.pipeline-job-stage-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);/s,
  );
  assert.match(
    polishStyles,
    /\.screen--pipeline-analysis \.pipeline-job-stage__copy strong\s*\{[^}]*font-size:\s*12\.5px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--pipeline-analysis \.pipeline-job-stage__copy small\s*\{[^}]*font-size:\s*11\.5px;/s,
  );
});

test("home previews at most three unanswered questions and exposes the remaining count", () => {
  assert.match(homeSource, /reviewedCandidateIds = new Set/);
  assert.match(
    homeSource,
    /\.filter\(\(candidate\) => !reviewedCandidateIds\.has\(candidate\.candidateId\)\)[\s\S]*\.slice\(0, 3\)/,
  );
  assert.match(homeSource, /hiddenQuestionCount = Math\.max/);
  assert.match(homeSource, /대표 질문 \$\{candidatePreview\.length\}건 표시/);
  assert.match(homeSource, /나머지 \$\{hiddenQuestionCount\}건/);
});

test("home restores durable pipeline input without suggesting another upload", () => {
  assert.match(
    homeSource,
    /hasDurableInput = Boolean\(runtime\.activeImport \|\| currentPipeline\)/,
  );
  assert.match(
    homeSource,
    /runtime\.activeImport \? \([\s\S]*\) : currentPipeline \? \(/,
  );
  assert.match(homeSource, /새 영상을 다시 넣지 않습니다/);
  assert.match(homeSource, /새 업로드 아님/);
  assert.match(homeSource, /currentPipeline\.recordSha256\.slice\(0, 8\)/);
  assert.doesNotMatch(
    homeSource,
    /currentPipeline[\s\S]{0,240}(?:durationSeconds|sourceSha256)/,
  );
});

test("video intake restores a durable pipeline without repeating transient inspection", () => {
  assert.match(
    importSource,
    /currentPipeline && inspection !== "ready"/,
  );
  assert.match(importSource, /재시작 전 자동 점검 작업을 복원했습니다/);
  assert.match(importSource, /같은 PipelineJob에서\s*이어갑니다/);
  assert.match(importSource, /중단 단계에서 재개/);
});

test("pipeline questions expose the immutable review profile as status, not a settings toggle", () => {
  assert.match(
    labelingSource,
    /setReviewMode\(currentPipelineJob\.profileSnapshot\.reviewMode\)/,
  );
  assert.match(labelingSource, /className="review-profile-summary"/);
  assert.match(labelingSource, /aria-label="현재 검토 모드"/);
  assert.match(labelingSource, /영상 시작 시 확정/);
  assert.match(
    labelingSource,
    /className="labeling-macro-flow"[\s\S]*aria-label="자동 점검 전체 흐름"/,
  );
  for (const label of ["영상", "자동 분석", "질문 검토", "결과 자동 갱신"]) {
    assert.match(labelingSource, new RegExp(label));
  }
  assert.match(labelingSource, /data-state=\{state\}/);
  assert.match(
    labelingSource,
    /aria-current=\{state === "current" \? "step" : undefined\}/,
  );
  assert.doesNotMatch(labelingSource, /프로젝트 설정/);
});

test("screen order follows the one-input automatic journey before secondary management", () => {
  assert.match(
    appSource,
    /const SCREEN_ORDER: ScreenId\[\] = \[\s*"home",\s*"import",\s*"analysis",\s*"label",\s*"review",\s*"history",\s*"report",\s*"export",\s*"models",\s*\];/,
  );
  assert.ok(
    shellSource.indexOf('id: "analysis"') <
      shellSource.indexOf('id: "label"'),
  );
  assert.ok(
    shellSource.indexOf('id: "export"') <
      shellSource.indexOf('id: "models"'),
  );
});

test("screen announcements use Korean product labels instead of internal route ids", () => {
  for (const [id, label] of [
    ["home", "프로젝트 홈"],
    ["import", "영상 가져오기"],
    ["analysis", "자동 분석 진행"],
    ["label", "질문 검토"],
    ["review", "사건 검토"],
    ["history", "지도·영상 히스토리"],
    ["report", "보고서"],
    ["export", "내보내기"],
    ["models", "모델 관리"],
  ]) {
    assert.match(
      appSource,
      new RegExp(`${id}:\\s*"${label}"`),
    );
  }
  assert.match(appSource, /\{SCREEN_LABELS\[screen\]\} 화면/);
  assert.match(
    appSource,
    /document\.title = `\$\{SCREEN_LABELS\[screen\]\} · TrafficSuperVision`/,
  );
  assert.doesNotMatch(appSource, /\{screen\} 화면/);
});

test("active pipeline polling continues through long stages and auto-navigation can observe completion", () => {
  assert.match(shellSource, /const timer = window\.setInterval\(refresh, 800\)/);
  assert.match(shellSource, /let fetching = false/);
  assert.match(shellSource, /document\.visibilityState === "hidden"/);
  assert.match(
    shellSource,
    /document\.addEventListener\("visibilitychange", resumeVisiblePolling\)/,
  );
  assert.match(
    shellSource,
    /document\.removeEventListener\("visibilitychange", resumeVisiblePolling\)/,
  );
  assert.match(shellSource, /window\.clearInterval\(timer\)/);
  assert.doesNotMatch(
    shellSource,
    /const timer = window\.setTimeout\(\(\) => \{[\s\S]*pipeline\s*\.get/,
  );
});

test("heavy screens are lazy and unused map CSS is not in the initial renderer", () => {
  for (const screen of [
    "AnalysisScreen",
    "ExportScreen",
    "HistoryScreen",
    "ImportScreen",
    "LabelingScreen",
    "ModelScreen",
    "ReportScreen",
    "ReviewScreen",
  ]) {
    assert.match(appSource, new RegExp(`const ${screen} = lazy`));
  }
  assert.doesNotMatch(entrySource, /maplibre-gl\/dist\/maplibre-gl\.css/);
  assert.doesNotMatch(
    historySource,
    /import\s*\{\s*HistoryMap\s*\}\s*from\s*"\.\.\/components\/HistoryMap"/,
  );
  assert.match(
    historySource,
    /const HistoryMap = lazy\(\(\) =>\s*import\("\.\.\/components\/HistoryMap"\)/,
  );
  assert.match(
    historySource,
    /\{gpxTrack \? \(\s*<Suspense[\s\S]*지도 준비 중[\s\S]*<HistoryMap/,
  );
  assert.match(
    historySource,
    /\) : \(\s*<div[\s\S]*<MapPinOff[\s\S]*GPX 필요[\s\S]*촬영 시작 좌표 한 점으로 경로를 만들지 않습니다/,
  );
});

test("lazy screen loaders prefetch only the next workflow screen and navigation intent", () => {
  assert.match(appSource, /function retryableLoader/);
  assert.match(appSource, /const SCREEN_LOADERS:/);
  assert.match(appSource, /const NEXT_SCREEN_PREFETCH:/);
  for (const [screen, next] of [
    ["home", "import"],
    ["import", "analysis"],
    ["analysis", "label"],
    ["label", "review"],
    ["review", "history"],
    ["history", "report"],
    ["report", "export"],
    ["export", "models"],
  ]) {
    assert.match(appSource, new RegExp(`${screen}:\\s*"${next}"`));
  }
  assert.match(appSource, /models:\s*null/);
  assert.match(appSource, /const nextScreen = NEXT_SCREEN_PREFETCH\[screen\]/);
  assert.match(appSource, /prefetchScreen\(nextScreen\)/);
  assert.doesNotMatch(appSource, /prefetchAllScreens|PREFETCHABLE_SCREENS/);
  assert.match(appSource, /requestIdleCallback/);
  assert.match(appSource, /cancelIdleCallback/);
  assert.match(appSource, /window\.setTimeout\(runPrefetch, 250\)/);
  assert.match(appSource, /window\.clearTimeout\(fallbackHandle\)/);
  assert.match(appSource, /window\.cancelAnimationFrame\(firstPaintFrame\)/);
  assert.match(appSource, /window\.cancelAnimationFrame\(afterPaintFrame\)/);
  assert.match(appSource, /}, \[screen\]\);/);
  assert.match(shellSource, /onPointerEnter=\{\(\) => onScreenPrefetch\(item\.id\)\}/);
  assert.match(shellSource, /onFocus=\{\(\) => onScreenPrefetch\(item\.id\)\}/);
});

test("the compact brand uses a bounded async-decoded icon", () => {
  assert.equal(appIcon.subarray(1, 4).toString("ascii"), "PNG");
  assert.equal(appIcon.readUInt32BE(16), 96);
  assert.equal(appIcon.readUInt32BE(20), 96);
  assert.ok(appIcon.byteLength < 100_000);
  assert.match(
    shellSource,
    /<img[\s\S]*src="\/trafficsupervision-app-icon\.png"[\s\S]*width=\{96\}[\s\S]*height=\{96\}[\s\S]*decoding="async"[\s\S]*draggable=\{false\}/,
  );
});
