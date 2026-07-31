import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const compactStyles = await readFile(
  new URL("./styles-compact-secondary.css", import.meta.url),
  "utf8",
);
const entrySource = await readFile(new URL("./main.tsx", import.meta.url), "utf8");
const historySource = await readFile(
  new URL("./screens/HistoryScreen.tsx", import.meta.url),
  "utf8",
);
const reviewSource = await readFile(
  new URL("./screens/ReviewScreen.tsx", import.meta.url),
  "utf8",
);
const exportSource = await readFile(
  new URL("./screens/ExportScreen.tsx", import.meta.url),
  "utf8",
);
const importSource = await readFile(
  new URL("./screens/ImportScreen.tsx", import.meta.url),
  "utf8",
);
const reportSource = await readFile(
  new URL("./screens/ReportScreen.tsx", import.meta.url),
  "utf8",
);

test("compact overrides load after the shared and product shell styles", () => {
  assert.match(
    entrySource,
    /import "\.\/styles\.css";\s*import "\.\/styles-product-shell\.css";\s*import "\.\/styles-compact-secondary\.css";/,
  );
});

test("secondary workbenches expose scoped screen modifiers", () => {
  assert.match(historySource, /className="screen screen--history"/);
  assert.match(reviewSource, /className="screen screen--review"/);
  assert.match(exportSource, /className="screen screen--export"/);
  assert.match(importSource, /className="screen screen--import"/);
});

test("desktop history header follows its text block inside the screen", () => {
  assert.match(
    compactStyles,
    /@media \(min-width: 961px\)\s*\{\s*\.screen\.screen--history > \.screen-header\s*\{[^}]*flex:\s*0 0 auto;[^}]*align-items:\s*flex-start;/s,
  );
});

test("short desktop report keeps provider, edit plan, and document in one viewport", () => {
  assert.match(
    compactStyles,
    /\.screen--report \.report-layout\s*\{[^}]*height:\s*100%;[^}]*grid-template-columns:\s*minmax\(320px,\s*34fr\)\s*minmax\(0,\s*66fr\);[^}]*gap:\s*8px;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--report \.report-controls\s*\{[^}]*grid-template-rows:[^;}]*minmax\(380px,\s*1fr\)[^;}]*140px/s,
  );
  assert.match(
    compactStyles,
    /\.screen--report \.edit-plan-empty\s*\{[^}]*min-height:\s*0;[^}]*flex:\s*1;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--report \.provider-panel\s*\{[^}]*overflow:\s*hidden;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--report \.edit-plan-panel:has\(\.edit-plan-empty\) \.edit-op-list\s*\{[^}]*overflow:\s*hidden;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--report \.document-stage\s*\{[^}]*padding:\s*14px 16px 34px;/s,
  );
});

test("report sketch controls expose honest one-page navigation and real zoom state", () => {
  assert.match(
    reportSource,
    /role="toolbar"[\s\S]*aria-label="문서 구성 스케치 보기 도구"/,
  );
  assert.match(reportSource, /구성 스케치 1 \/ 1/);
  assert.match(reportSource, /실제 페이지 렌더링 전/);
  assert.match(reportSource, /changeDocumentZoom\(-10\)/);
  assert.match(reportSource, /changeDocumentZoom\(10\)/);
  assert.match(reportSource, /onClick=\{fitDocumentWidth\}/);
  assert.match(reportSource, /style=\{\{ zoom: `\$\{documentZoom\}%` \}\}/);
  assert.doesNotMatch(reportSource, /구성 스케치 1 \/ 48|페이지 1 \/ 48/);
  assert.match(
    reportSource,
    /document-runtime-notice notice notice--warning" role="status"/,
  );
});

test("short desktop history keeps all four synchronized regions in a fixed grid", () => {
  assert.match(
    compactStyles,
    /@media \(min-width: 961px\) and \(max-height: 780px\)/,
  );
  assert.match(
    compactStyles,
    /\.screen--history\s*\{[^}]*display:\s*grid;[^}]*grid-template-rows:[^;}]*auto[^;}]*minmax\(230px,\s*1\.35fr\)[^;}]*66px[^;}]*minmax\(148px,\s*0\.8fr\)[^}]*overflow:\s*hidden;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--history \.incident-list\s*\{[^}]*flex:\s*1;[^}]*overflow-x:\s*hidden;[^}]*overflow-y:\s*auto;[^}]*overscroll-behavior:\s*contain;[^}]*scrollbar-gutter:\s*stable;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--history \.evidence-details\s*\{[^}]*overflow:\s*hidden;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--history \.history-map-unavailable\s*\{[^}]*overflow:\s*hidden;/s,
  );
  assert.match(
    historySource,
    /className="incident-list history-scroll-owner"[\s\S]*role="region"[\s\S]*aria-label="사건과 후보 목록"[\s\S]*tabIndex=\{0\}/,
  );
  assert.match(
    compactStyles,
    /\.screen--history \.evidence-details \.definition-list\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/s,
  );
});

test("review and export confine long data to their own scrollports", () => {
  assert.match(
    compactStyles,
    /\.screen--review \.review-table-wrap\s*\{[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*auto;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--export \.artifact-checks\s*\{[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*auto;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--export \.artifact-table-wrap\s*\{[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*auto;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--export\s+\.artifact-table-panel--completed\s+\.artifact-table-wrap\s*\{[^}]*overflow:\s*hidden;/s,
  );
  assert.match(
    exportSource,
    /artifact-table-panel--completed/,
  );
  assert.match(
    exportSource,
    /className="mono"[\s\S]*title=\{[\s\S]*row\.sha256/,
  );
  assert.match(
    compactStyles,
    /\.screen--export \.artifact-checks label\s*\{[^}]*min-height:\s*37px;/s,
  );
  assert.match(
    compactStyles,
    /\.screen--review \.review-evidence-column\s*\{[^}]*grid-template-rows:[^;}]*minmax\(300px,\s*1fr\)[^;}]*190px/s,
  );
  assert.match(
    compactStyles,
    /\.screen--review \.review-evidence-cards \.glass-panel\s*\{[^}]*overflow:\s*hidden;/s,
  );
});

test("large import directories are bounded to the file candidate list", () => {
  assert.match(
    compactStyles,
    /\.screen--import \.file-candidates\s*\{[^}]*max-height:\s*none;[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*auto;/s,
  );
});
