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
const labelingSource = await readFile(
  new URL("./screens/LabelingScreen.tsx", import.meta.url),
  "utf8",
);
const polishStyles = await readFile(
  new URL("./styles-visual-polish.css", import.meta.url),
  "utf8",
);
const productStyles = await readFile(
  new URL("./styles-product-shell.css", import.meta.url),
  "utf8",
);

test("the full SUIT variable face and final polish layer load last", () => {
  assert.match(
    entrySource,
    /@sun-typeface\/suit\/fonts\/variable\/woff2\/SUIT-Variable\.css";[\s\S]*styles-compact-analysis-labeling\.css";\s*import "\.\/styles-visual-polish\.css";/,
  );
  assert.doesNotMatch(entrySource, /pretendard/i);
  assert.doesNotMatch(entrySource, /jetbrains|fontsource-variable/i);
  assert.match(
    polishStyles,
    /#root \*\s*,[\s\S]*\.maplibregl-ctrl\s*\{[^}]*font-family:\s*var\(--font-sans\)\s*!important;/s,
  );
});

test("the edge-to-edge app restores restrained liquid glass inside the product surface", () => {
  assert.match(
    productStyles,
    /--glass-shell:\s*rgba\(230,\s*238,\s*248,\s*0\.8\)/,
  );
  assert.match(
    productStyles,
    /\.app-shell\s*\{[^}]*linear-gradient[^}]*backdrop-filter:\s*blur\(22px\) saturate\(116%\)/s,
  );
  assert.match(
    productStyles,
    /\.titlebar\s*\{[^}]*border-bottom-color:\s*rgba\(52,\s*67,\s*112,\s*0\.055\);[^}]*background:\s*rgba\(246,\s*249,\s*253,\s*0\.44\);[^}]*backdrop-filter:\s*blur\(18px\)/s,
  );
  assert.match(
    productStyles,
    /\.side-nav\s*\{[^}]*border-right-color:\s*rgba\(52,\s*67,\s*112,\s*0\.055\);[^}]*background:\s*rgba\(246,\s*249,\s*253,\s*0\.13\);[^}]*backdrop-filter:\s*blur\(12px\) saturate\(110%\)/s,
  );
  assert.match(
    productStyles,
    /\.statusbar\s*\{[^}]*border-top-color:\s*rgba\(52,\s*67,\s*112,\s*0\.05\);[^}]*background:\s*rgba\(246,\s*249,\s*253,\s*0\.24\);[^}]*backdrop-filter:\s*blur\(12px\) saturate\(110%\)/s,
  );
  assert.match(
    polishStyles,
    /\.glass-panel\s*\{[^}]*backdrop-filter:\s*blur\(14px\) saturate\(112%\)/s,
  );
});

test("home hierarchy is asymmetric without a hidden metric scroller", () => {
  assert.match(
    polishStyles,
    /\.home-overview\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1\.72fr\) repeat\(2,\s*minmax\(210px,\s*0\.62fr\)\);/s,
  );
  assert.doesNotMatch(
    homeSource,
    /프로젝트 입력 열기|후보 추출[\s\S]*metric-link-icon/,
  );
  assert.match(homeSource, /title: "영상 넣기"/);
  assert.match(homeSource, /title: "자동 분석"/);
  assert.match(homeSource, /title: "질문 검토"/);
  assert.match(homeSource, /title: "결과 자동 갱신"/);
  assert.match(homeSource, /영상 하나만 넣으면 질문 준비까지 자동으로 진행됩니다/);
  assert.match(homeSource, /화면을 옮겨 다니며 중간 분석 버튼을 누를 필요가 없습니다/);
  assert.match(
    polishStyles,
    /\.home-grid\s*\{[^}]*height:\s*auto;[^}]*flex:\s*1 1 0;[^}]*align-self:\s*stretch;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--home\s*\{[^}]*gap:\s*8px;[^}]*grid-template-rows:\s*auto 198px minmax\(0,\s*1fr\);/s,
  );
  assert.match(
    polishStyles,
    /\.home-pipeline-hero,\s*\.home-summary-card\s*\{[^}]*min-height:\s*0;[^}]*height:\s*auto;/s,
  );
  assert.match(
    polishStyles,
    /\.home-pipeline-hero\s*\{[^}]*overflow:\s*visible;/s,
  );
  assert.match(
    polishStyles,
    /\.home-pipeline-hero__top,\s*\.home-pipeline-steps,\s*\.home-pipeline-footer\s*\{[^}]*flex:\s*0 0 auto;/s,
  );
  assert.match(
    polishStyles,
    /\.home-pipeline-hero__top p\s*\{[^}]*display:\s*-webkit-box;[^}]*overflow:\s*hidden;[^}]*-webkit-line-clamp:\s*2;/s,
  );
  assert.match(
    polishStyles,
    /\.home-summary-card > p\s*\{[^}]*display:\s*-webkit-box;[^}]*overflow:\s*hidden;[^}]*-webkit-line-clamp:\s*2;/s,
  );
  assert.match(
    polishStyles,
    /\.home-summary-card > \.home-summary-card__status\s*\{[^}]*display:\s*-webkit-box;[^}]*overflow:\s*hidden;[^}]*-webkit-line-clamp:\s*2;/s,
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
  assert.match(
    homeSource,
    /\.filter\(\(candidate\) => !reviewedCandidateIds\.has\(candidate\.candidateId\)\)[\s\S]*\.slice\(0, 3\)/,
  );
  assert.doesNotMatch(homeSource, /realTunnelEvidenceImage/);
  assert.match(
    polishStyles,
    /\.home-pipeline-steps\s*\{[^}]*gap:\s*0;[^}]*border-radius:\s*13px;[^}]*background:\s*rgba\(232,\s*238,\s*248,\s*0\.5\)/s,
  );
  assert.match(
    polishStyles,
    /\.home-pipeline-step\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent/s,
  );
  assert.match(
    polishStyles,
    /\.screen--pipeline-analysis \.pipeline-job-stage-grid\s*\{[^}]*gap:\s*0;[^}]*border-radius:\s*12px/s,
  );
  assert.match(
    polishStyles,
    /\.screen--pipeline-analysis \.pipeline-job-stage\s*\{[^}]*border:\s*0;[^}]*border-bottom:\s*1px solid/s,
  );
});

test("pipeline analysis reads as two calm work surfaces instead of a field of cards", () => {
  assert.match(
    polishStyles,
    /@media \(min-width:\s*961px\)\s*\{[\s\S]*?\.screen--pipeline-analysis \.analysis-summary\s*\{[^}]*gap:\s*0;[^}]*overflow:\s*hidden;[^}]*border-radius:\s*var\(--radius-card\);[^}]*backdrop-filter:\s*blur\(14px\) saturate\(110%\)/s,
  );
  assert.match(
    polishStyles,
    /\.screen--pipeline-analysis \.analysis-summary > \.glass-panel\s*\{[^}]*border:\s*0;[^}]*border-radius:\s*0;[^}]*background:\s*transparent;[^}]*box-shadow:\s*none;[^}]*backdrop-filter:\s*none;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--pipeline-analysis \.analysis-summary > \.stat-tile\s*\{[^}]*border-left:\s*1px solid rgba\(52,\s*67,\s*112,\s*0\.08\);/s,
  );
  assert.match(
    polishStyles,
    /@media \(min-width:\s*961px\) and \(max-height:\s*820px\)\s*\{[\s\S]*?\.screen--pipeline-analysis \.pipeline-job-stage-grid\s*\{[^}]*gap:\s*0;[^}]*margin-top:\s*6px;/s,
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

test("label review uses one quiet decision surface instead of nested status cards", () => {
  assert.match(labelingSource, /className="question-card__header"/);
  assert.doesNotMatch(labelingSource, /eyebrow="LABEL QUESTION"/);
  assert.match(labelingSource, /className="experimental-proposal__summary"/);
  assert.doesNotMatch(labelingSource, /className="proposal-facts"/);
  assert.match(
    polishStyles,
    /\.screen--labeling \.answer-grid\s*\{[^}]*grid-template-columns:\s*repeat\(4,[^}]*border-radius:\s*14px;[^}]*background:\s*rgba\(226,\s*232,\s*243,\s*0\.5\);/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.answer--yes\s*\{[^}]*color:\s*#fff;[^}]*background:\s*#17191f;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.severity-followup\s*\{[^}]*border-radius:\s*14px;[^}]*background:\s*rgba\(226,\s*232,\s*243,\s*0\.5\);/s,
  );
});

test("history keeps one bounded vertical scroll owner while evidence stays in-frame", () => {
  assert.match(
    polishStyles,
    /@media \(min-width:\s*961px\) and \(max-height:\s*780px\)[\s\S]*\.screen--history \.evidence-details\s*\{[^}]*overflow:\s*hidden;/s,
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
    /@media \(min-width:\s*961px\)[\s\S]*\.screen--history \.incident-list\s*\{[^}]*overflow-y:\s*auto;[^}]*scrollbar-gutter:\s*stable;[\s\S]*\.screen--history \.evidence-details\s*\{[^}]*overflow:\s*hidden;/s,
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
