import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const entrySource = await readFile(new URL("./main.tsx", import.meta.url), "utf8");
const homeSource = await readFile(
  new URL("./screens/HomeScreen.tsx", import.meta.url),
  "utf8",
);
const modelSource = await readFile(
  new URL("./screens/ModelScreen.tsx", import.meta.url),
  "utf8",
);
const polishStyles = await readFile(
  new URL("./styles-visual-polish.css", import.meta.url),
  "utf8",
);

test("the full SUIT variable face and final polish layer load last", () => {
  assert.match(
    entrySource,
    /@sun-typeface\/suit\/fonts\/variable\/woff2\/SUIT-Variable\.css";[\s\S]*styles-compact-analysis-labeling\.css";\s*import "\.\/styles-visual-polish\.css";/,
  );
  assert.doesNotMatch(entrySource, /pretendard/i);
});

test("home hierarchy is asymmetric without a hidden metric scroller", () => {
  assert.match(
    polishStyles,
    /\.hero-metrics\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1\.35fr\) repeat\(3,\s*minmax\(0,\s*1fr\)\);[^}]*overflow:\s*visible;/s,
  );
  assert.doesNotMatch(
    homeSource,
    /className="mono">공통 초기 슬롯 · 장면 후보 추출 전용/,
  );
  assert.match(
    polishStyles,
    /\.home-grid\s*\{[^}]*height:\s*auto;[^}]*flex:\s*1 1 0;[^}]*align-self:\s*stretch;/s,
  );
  assert.match(homeSource, /className=\{`trip-evidence-showcase/);
  assert.match(
    homeSource,
    /buildCandidateEvidenceUrl\([\s\S]*candidate\.candidateId,[\s\S]*"image"/,
  );
  assert.match(
    polishStyles,
    /@media \(min-width: 961px\) and \(max-height: 820px\)[\s\S]*\.question-list li:nth-child\(n \+ 4\)\s*\{\s*display:\s*none;/,
  );
  assert.doesNotMatch(
    polishStyles,
    /\.trip-evidence-showcase\s*\{\s*display:\s*none;/,
  );
});

test("Korean badges and short-window controls keep readable line boxes", () => {
  assert.match(
    polishStyles,
    /\.badge\s*\{[^}]*min-height:\s*24px;[^}]*line-height:\s*1\.3;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--import \.source-tabs button\s*\{[^}]*font-size:\s*12px;[^}]*line-height:\s*1\.35;[^}]*white-space:\s*nowrap;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.question-prompt\s*\{[^}]*font-size:\s*16px;[^}]*line-height:\s*1\.35;[^}]*-webkit-line-clamp:\s*2;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.label-progress\s*\{[^}]*min-height:\s*46px;/s,
  );
});

test("dense evidence owns a readable internal detail scrollport", () => {
  assert.match(
    polishStyles,
    /\.screen--history \.evidence-details\s*\{[^}]*overflow-y:\s*auto;[^}]*scrollbar-gutter:\s*stable;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--history \.evidence-details \.definition-list dt,[\s\S]*font-size:\s*11\.5px;[\s\S]*line-height:\s*1\.35;/,
  );
  assert.match(
    polishStyles,
    /\.screen--review \.review-evidence-cards p,[\s\S]*font-size:\s*11px;[\s\S]*line-height:\s*1\.35;/,
  );
  assert.match(
    polishStyles,
    /\.screen--history \.history-bottom\s*\{[^}]*min-height:\s*0;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--history \.incident-list-panel,[\s\S]*\.screen--history \.evidence-card-content\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--history \.evidence-details\s*\{[^}]*overflow-y:\s*auto;[^}]*overscroll-behavior:\s*contain;[^}]*scrollbar-gutter:\s*stable;/s,
  );
  assert.match(
    polishStyles,
    /@media \(min-width:\s*961px\) and \(min-height:\s*781px\)\s*\{[\s\S]*\.screen--history\s*\{[^}]*display:\s*grid;[^}]*grid-template-rows:\s*auto\s*minmax\(270px,\s*1fr\)\s*106px\s*minmax\(180px,\s*0\.6fr\);[^}]*gap:\s*10px;/s,
  );
  assert.match(
    polishStyles,
    /@media \(min-width:\s*961px\) and \(min-height:\s*781px\)[\s\S]*\.screen--history \.history-top,[\s\S]*\.screen--history \.history-bottom\s*\{[^}]*min-height:\s*0;[^}]*height:\s*100%;[^}]*gap:\s*10px;/s,
  );
});

test("desktop evidence metadata never falls back to legacy micro type", () => {
  assert.match(
    polishStyles,
    /\.question-list small,[\s\S]*\.screen--history \.incident-list small,[\s\S]*\.screen--history \.evidence-details \.definition-list dd\s*\{[^}]*font-size:\s*11\.5px;[^}]*line-height:\s*1\.35;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--history \.timeline-event span\s*\{[^}]*font-family:\s*var\(--font-sans\);[^}]*font-size:\s*11\.5px;[^}]*font-variant-numeric:\s*tabular-nums;[^}]*line-height:\s*1\.3;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.workbench-toolbar \.mono,[\s\S]*\.screen--labeling \.question-metadata \.badge,[\s\S]*\.screen--labeling \.progress-summary,[\s\S]*font-size:\s*11\.5px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.question-metadata > \*\s*\{[^}]*white-space:\s*nowrap;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.detection-box span\s*\{[^}]*font-size:\s*11\.5px;[^}]*line-height:\s*1\.3;/s,
  );
});

test("the model registry keeps its status line visible and gives overflow to the source table", () => {
  assert.match(modelSource, /className="screen screen--model"/);
  assert.match(
    polishStyles,
    /\.screen--model \.model-table-wrap\s*\{[^}]*min-height:\s*0;[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*auto;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--model \.model-main > \.model-message\s*\{[^}]*min-height:\s*17px;[^}]*text-overflow:\s*ellipsis;/s,
  );
});
