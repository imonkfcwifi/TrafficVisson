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
const entrySource = read("main.tsx");
const productStyles = read("styles-product-shell.css");
const appIcon = fs.readFileSync(
  path.join(sourceRoot, "../public/trafficvisson-app-icon.png"),
);

test("desktop navigation stays a stable compact rail", () => {
  assert.match(productStyles, /@media \(min-width: 961px\)[\s\S]*\.side-nav\s*\{[\s\S]*width:\s*82px/);
  assert.match(
    productStyles,
    /\.nav-item--active \.nav-icon\s*\{[\s\S]*background:\s*transparent/,
  );
});

test("the product header does not expose development or provider jargon", () => {
  assert.doesNotMatch(shellSource, /Desktop alpha/);
  assert.match(shellSource, /로컬 준비/);
  assert.match(shellSource, /현재 작업 상태/);
});

test("home review rows are actionable and preserve cross-screen selection", () => {
  assert.match(homeSource, /<button[\s\S]*className="question-list-row"/);
  assert.match(homeSource, /saveSelectedCandidateId\(/);
  assert.match(homeSource, /variant="primary"[\s\S]*건 검토 시작/);
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
    /<img[\s\S]*src="\/trafficvisson-app-icon\.png"[\s\S]*width=\{96\}[\s\S]*height=\{96\}[\s\S]*decoding="async"[\s\S]*draggable=\{false\}/,
  );
});
