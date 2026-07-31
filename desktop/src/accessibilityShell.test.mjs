import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const shellSource = await readFile(
  new URL("./components/AppShell.tsx", import.meta.url),
  "utf8",
);
const appSource = await readFile(new URL("./App.tsx", import.meta.url), "utf8");
const importSource = await readFile(
  new URL("./screens/ImportScreen.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(new URL("./styles.css", import.meta.url), "utf8");

test("collapsed navigation retains an explicit accessible name", () => {
  assert.match(
    shellSource,
    /aria-label=\{`\$\{item\.label\} \(\$\{item\.shortcut\}\)`\}/,
  );
});

test("keyboard users can skip directly to the workspace", () => {
  assert.match(shellSource, /className="skip-link" href="#main-content"/);
  assert.match(
    stylesSource,
    /\.skip-link:focus\s*\{[^}]*transform:\s*translateY\(0\)/s,
  );
});

test("screen navigation focuses a bounded heading instead of outlining the whole workspace", () => {
  assert.match(
    appSource,
    /querySelector<HTMLElement>\("\.screen > \.screen-header h1"\)/,
  );
  assert.match(appSource, /heading\.tabIndex = -1;/);
  assert.match(appSource, /heading\.focus\(\{ preventScroll: true \}\)/);
  assert.match(
    stylesSource,
    /\.workspace:focus-visible\s*\{[^}]*outline:\s*0;[^}]*box-shadow:\s*inset 3px 0 0/s,
  );
  assert.match(
    stylesSource,
    /\.screen > \.screen-header h1\[tabindex="-1"\]:focus-visible\s*\{[^}]*outline-width:\s*2px/s,
  );
});

test("the desktop shell is an edge-to-edge application surface", () => {
  const canvasRule = stylesSource.match(
    /^\.app-canvas\s*\{(?<body>[^}]*)\}/m,
  );
  const shellRule = stylesSource.match(
    /^\.app-shell\s*\{(?<body>[^}]*)\}/m,
  );
  assert.ok(canvasRule?.groups?.body);
  assert.ok(shellRule?.groups?.body);
  assert.match(canvasRule.groups.body, /width:\s*100%/);
  assert.match(canvasRule.groups.body, /height:\s*100dvh/);
  assert.match(canvasRule.groups.body, /padding:\s*0/);
  assert.match(canvasRule.groups.body, /overflow:\s*clip/);
  assert.match(canvasRule.groups.body, /contain:\s*layout paint size/);
  assert.match(shellRule.groups.body, /width:\s*100%/);
  assert.match(shellRule.groups.body, /border:\s*0/);
  assert.match(shellRule.groups.body, /border-radius:\s*0/);
  assert.match(shellRule.groups.body, /box-shadow:\s*none/);
  assert.doesNotMatch(
    stylesSource,
    /@media \(min-width:\s*1100px\) and \(min-height:\s*840px\)/,
  );
});

test("native caption controls do not overlap draggable product controls", () => {
  assert.match(
    stylesSource,
    /@media \(min-width:\s*961px\)[\s\S]*?\.titlebar\s*\{[^}]*padding-right:\s*156px/,
  );
  assert.match(
    stylesSource,
    /\.titlebar button,[\s\S]*?\.titlebar select,[\s\S]*?\.titlebar label\s*\{[^}]*-webkit-app-region:\s*no-drag/,
  );
});

test("desktop workspace stays fixed while responsive layouts restore document flow", () => {
  const workspaceRule = stylesSource.match(
    /^\.workspace\s*\{(?<body>[^}]*)\}/m,
  );
  const screenRule = stylesSource.match(/^\.screen\s*\{(?<body>[^}]*)\}/m);
  assert.ok(workspaceRule?.groups?.body);
  assert.ok(screenRule?.groups?.body);
  assert.match(workspaceRule.groups.body, /height:\s*100%/);
  assert.match(workspaceRule.groups.body, /overflow:\s*hidden/);
  assert.match(screenRule.groups.body, /height:\s*100%/);
  assert.match(screenRule.groups.body, /overflow:\s*hidden/);
  assert.match(
    stylesSource,
    /@media \(max-width:\s*960px\)[\s\S]*?\.workspace\s*\{[^}]*height:\s*auto;[^}]*overflow:\s*visible/,
  );
  assert.match(
    stylesSource,
    /@media \(max-width:\s*960px\)[\s\S]*?\.screen\s*\{[^}]*height:\s*auto;[^}]*overflow:\s*visible/,
  );
});

test("the project switcher is a real labeled select instead of a disabled promise", () => {
  assert.match(shellSource, /<select\s+aria-label="활성 프로젝트"/);
  assert.match(shellSource, /selectProject\(event\.target\.value\)/);
  assert.doesNotMatch(
    shellSource,
    /다중 프로젝트 전환은 저장소 연결 후 활성화/,
  );
});

test("closed mobile navigation is inert and removed from the accessibility tree", () => {
  assert.match(shellSource, /inert=\{mobileNav && !navOpen\}/);
  assert.match(
    shellSource,
    /aria-hidden=\{mobileNav && !navOpen \? true : undefined\}/,
  );
  assert.match(
    stylesSource,
    /\.side-nav\s*\{[^}]*visibility:\s*hidden;[^}]*pointer-events:\s*none;/s,
  );
  assert.match(
    stylesSource,
    /\.side-nav--open\s*\{[^}]*visibility:\s*visible;[^}]*pointer-events:\s*auto;/s,
  );
});

test("diagnostics dialog traps focus, closes on Escape, and restores background state", () => {
  assert.match(shellSource, /FOCUSABLE_SELECTOR/);
  assert.match(shellSource, /event\.key === "Escape"/);
  assert.match(shellSource, /event\.key !== "Tab"/);
  assert.match(shellSource, /background\.setAttribute\("inert", ""\)/);
  assert.match(shellSource, /background\.removeAttribute\("inert"\)/);
  assert.match(shellSource, /document\.addEventListener\("focusin"/);
  assert.match(shellSource, /returnTarget\?\.isConnected/);
  assert.match(appSource, /if \(diagnosticsOpen\)/);
});

test("one-time project settings trap focus and provide a keyboard exit after setup", () => {
  assert.match(importSource, /PROFILE_FOCUSABLE_SELECTOR/);
  assert.match(importSource, /ref=\{profileDialogRef\}/);
  assert.match(importSource, /aria-describedby="pipeline-profile-description"/);
  assert.match(importSource, /tabIndex=\{-1\}/);
  assert.match(
    importSource,
    /element\.classList\.contains\("pipeline-profile-backdrop"\)/,
  );
  assert.match(importSource, /item\.element\.setAttribute\("inert", ""\)/);
  assert.match(importSource, /document\.addEventListener\("focusin"/);
  assert.match(importSource, /event\.key === "Escape"/);
  assert.match(importSource, /event\.key !== "Tab"/);
  assert.match(importSource, /returnTarget\?\.isConnected/);
  assert.match(
    importSource,
    /\{profile \? \([\s\S]*onClick=\{\(\) => setProfileOpen\(false\)\}[\s\S]*취소/,
  );
  assert.match(
    importSource,
    /if \(profileState === "ready" && profile !== null\)[\s\S]*setProfileOpen\(false\);[\s\S]*onNavigate\("home"\);/,
  );
  assert.match(importSource, /새 프로젝트 · 1 \/ 2/);
  assert.match(importSource, /다음 단계 · 2 \/ 2/);
  assert.match(importSource, /기본 설정 저장 → 영상 선택/);
  assert.match(importSource, /profileDialogMode === "initial"[\s\S]*새 프로젝트 준비[\s\S]*프로젝트 설정/);
  assert.match(importSource, /profile === null[\s\S]*기본 설정 저장 → 영상 선택[\s\S]*변경사항 저장/);
  assert.doesNotMatch(importSource, /1회 설정 변경/);
});

test("project profile loading is project-scoped and fails closed", () => {
  assert.match(
    importSource,
    /function isMissingPipelineProfileError[\s\S]*includes\("blocked_configuration_required"\)/,
  );
  assert.match(
    importSource,
    /setProfile\(null\);[\s\S]*setProfileOpen\(false\);[\s\S]*setReviewMode\(DEFAULT_REVIEW_MODE\);[\s\S]*setQuestionPolicy\(DEFAULT_QUESTION_POLICY\);[\s\S]*setTemplateId\(DEFAULT_TEMPLATE_ID\);[\s\S]*setReportProvider\(DEFAULT_REPORT_PROVIDER\);[\s\S]*setReportTransmissionConsent\(false\);[\s\S]*setCandidateRescanFps\(DEFAULT_CANDIDATE_RESCAN_FPS\);/,
  );
  assert.match(
    importSource,
    /if \(value\.projectId !== projectId\)[\s\S]*프로젝트 설정 응답이 현재 프로젝트와 일치하지 않습니다/,
  );
  assert.match(
    importSource,
    /\.catch\(\(error\) => \{[\s\S]*isMissingPipelineProfileError\(error\)[\s\S]*setProfileState\("missing"\)[\s\S]*setProfileState\("error"\)/,
  );
  assert.match(
    importSource,
    /const profileControlsDisabled =[\s\S]*profileState === "loading"[\s\S]*profileState === "saving"[\s\S]*profileState === "error"/,
  );
  assert.match(
    importSource,
    /const configurationStateIsValid =[\s\S]*profileState === "missing"[\s\S]*profileState === "ready"[\s\S]*activeProjectIdRef\.current !== projectId/,
  );
  assert.match(
    importSource,
    /profileDialogMode === "error"[\s\S]*설정을 읽지 못했습니다[\s\S]*프로젝트 홈[\s\S]*다시 시도/,
  );
  assert.match(
    importSource,
    /saved\.projectId !== projectId[\s\S]*activeProjectIdRef\.current !== projectId/,
  );
});

test("labeling severity shortcuts never fall through to screen navigation", () => {
  assert.match(appSource, /if \(event\.defaultPrevented\) return;/);
  assert.match(
    appSource,
    /if \(screen === "label" && \/\^\[123\]\$\/\.test\(event\.key\)\)\s*\{\s*return;/,
  );
  assert.match(appSource, /\}, \[diagnosticsOpen, screen\]\);/);
});

test("history timeline candidate targets keep a minimum 32px hit area", () => {
  const rule = stylesSource.match(/\.timeline-event\s*\{(?<body>[^}]*)\}/s);
  assert.ok(rule?.groups?.body);
  const width = Number(rule.groups.body.match(/width:\s*(\d+)px/)?.[1]);
  const height = Number(rule.groups.body.match(/height:\s*(\d+)px/)?.[1]);
  assert.ok(width >= 32, `expected width >= 32px, received ${width}px`);
  assert.ok(height >= 32, `expected height >= 32px, received ${height}px`);
});
