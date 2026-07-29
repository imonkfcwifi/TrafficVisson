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
const homeSource = read("screens/HomeScreen.tsx");
const historySource = read("screens/HistoryScreen.tsx");
const importSource = read("screens/ImportScreen.tsx");
const labelingSource = read("screens/LabelingScreen.tsx");
const entrySource = read("main.tsx");
const productStyles = read("styles-product-shell.css");
const appIcon = fs.readFileSync(
  path.join(sourceRoot, "../public/trafficsupervision-app-icon.png"),
);

test("desktop navigation stays compact while exposing legible screen labels and location", () => {
  assert.match(productStyles, /@media \(min-width: 961px\)[\s\S]*#root \.side-nav\s*\{[\s\S]*width:\s*96px/);
  assert.match(productStyles, /\.nav-label-compact\s*\{[\s\S]*display:\s*block/);
  assert.match(
    productStyles,
    /\.nav-label-compact\s*\{[^}]*font-size:\s*10\.5px/s,
  );
  assert.match(shellSource, /compactLabel:\s*"영상"/);
  assert.match(shellSource, /compactLabel:\s*"질문"/);
  assert.match(shellSource, /현재 작업 화면: \$\{activeNavigationItem\.label\}/);
  assert.match(shellSource, /<span>현재 화면<\/span>/);
  assert.match(shellSource, /<strong>\{activeNavigationItem\.label\}<\/strong>/);
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

test("pipeline questions use the immutable project review profile instead of a local toggle cache", () => {
  assert.match(
    labelingSource,
    /setReviewMode\(currentPipelineJob\.profileSnapshot\.reviewMode\)/,
  );
  assert.match(labelingSource, /disabled=\{Boolean\(currentPipelineJob\)\}/);
  assert.match(labelingSource, /프로젝트 판정 방식입니다/);
});

test("active pipeline polling continues through long stages and auto-navigation can observe completion", () => {
  assert.match(shellSource, /const timer = window\.setInterval\(refresh, 800\)/);
  assert.match(shellSource, /let fetching = false/);
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
  assert.match(historySource, /<Suspense[\s\S]*지도 준비 중[\s\S]*<HistoryMap/);
});

test("lazy screen loaders are shared by idle and navigation-intent prefetch", () => {
  assert.match(appSource, /function retryableLoader/);
  assert.match(appSource, /const SCREEN_LOADERS:/);
  assert.match(appSource, /requestIdleCallback/);
  assert.match(appSource, /cancelIdleCallback/);
  assert.match(appSource, /window\.setTimeout\(runPrefetch, 250\)/);
  assert.match(appSource, /window\.clearTimeout\(fallbackHandle\)/);
  assert.match(appSource, /window\.cancelAnimationFrame\(firstPaintFrame\)/);
  assert.match(appSource, /window\.cancelAnimationFrame\(afterPaintFrame\)/);
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
