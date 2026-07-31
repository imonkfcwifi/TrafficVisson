import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const analysisSource = await readFile(
  new URL("./screens/AnalysisScreen.tsx", import.meta.url),
  "utf8",
);
const labelingSource = await readFile(
  new URL("./screens/LabelingScreen.tsx", import.meta.url),
  "utf8",
);
const candidateProposalSource = await readFile(
  new URL("./candidateProposal.ts", import.meta.url),
  "utf8",
);
const mainSource = await readFile(new URL("./main.tsx", import.meta.url), "utf8");
const compactStyles = await readFile(
  new URL("./styles-compact-analysis-labeling.css", import.meta.url),
  "utf8",
);
const polishStyles = await readFile(
  new URL("./styles-visual-polish.css", import.meta.url),
  "utf8",
);
const responsiveStyles = await readFile(
  new URL("./styles-responsive-workbench.css", import.meta.url),
  "utf8",
);

test("short-desktop overrides are loaded after the shared stylesheet", () => {
  assert.match(
    mainSource,
    /import "\.\/styles\.css";[\s\S]*import "\.\/styles-compact-analysis-labeling\.css";\s*import "\.\/styles-visual-polish\.css";/,
  );
  assert.match(
    compactStyles,
    /@media \(min-width:\s*961px\) and \(max-height:\s*820px\)/,
  );
});

test("final short-desktop safety copy is readable at the 1280 workbench geometry", () => {
  assert.match(
    labelingSource,
    /className="auto-label-command__guardrail"[\s\S]*낙하물·이정표\/터널 OCR은 항상 검토하며,[\s\S]*사용자가 질문 카드에서 확정합니다\./,
  );
  assert.match(
    labelingSource,
    /return question\?\.rationale \?\? "분석 후보를 기다리고 있습니다\.";/,
  );
  assert.match(
    candidateProposalSource,
    /이 제안만으로 자동 승인·긴급도·유지보수 조치·보고서 반영을 결정하지 않습니다\./,
  );
  assert.match(
    polishStyles,
    /@media \(min-width:\s*961px\) and \(max-height:\s*820px\)[\s\S]*?\.screen--labeling \.auto-label-command__body\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\) minmax\(430px,\s*0\.72fr\);/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.auto-label-command__guardrail span\s*\{[^}]*display:\s*block;[^}]*overflow:\s*visible;[^}]*font-family:\s*var\(--font-sans\);[^}]*text-overflow:\s*clip;[^}]*white-space:\s*normal;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.auto-label-command__identity small,\s*\.screen--labeling \.auto-label-command__guardrail span,[\s\S]*?\{[^}]*font-size:\s*11px;[^}]*line-height:\s*1\.35;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.experimental-proposal ~ \.explanation\s*\{[^}]*min-height:\s*50px;[^}]*max-height:\s*none;[^}]*flex:\s*0 0 auto;[^}]*margin:\s*2px 0 1px;[^}]*padding:\s*3px 6px;[^}]*overflow:\s*visible;[^}]*font-family:\s*var\(--font-sans\);[^}]*font-size:\s*11px;[^}]*line-height:\s*1\.3;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.experimental-proposal ~ \.explanation p\s*\{[^}]*display:\s*block;[^}]*overflow:\s*visible;[^}]*text-overflow:\s*clip;[^}]*white-space:\s*normal;[^}]*-webkit-line-clamp:\s*unset;/s,
  );
});

test("1280 labeling keeps the evidence frame and yields duplicate proposal chrome before controls", () => {
  assert.match(
    polishStyles,
    /\.screen--labeling \.evidence-image\s*\{[^}]*height:\s*clamp\(240px,\s*34vh,\s*260px\);[^}]*min-height:\s*240px;[^}]*flex-basis:\s*clamp\(240px,\s*34vh,\s*260px\);/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.experimental-proposal\s*\{[^}]*min-height:\s*48px;[^}]*max-height:\s*48px;[^}]*flex:\s*0 0 48px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.proposal-facts\s*\{[^}]*height:\s*27px;[^}]*display:\s*grid;[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\);[^}]*overflow:\s*hidden;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.proposal-facts dt,[\s\S]*\.screen--labeling \.proposal-facts dd \.mono[\s\S]*\{[^}]*font-size:\s*11px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.weak-case-strip__identity,[\s\S]*\{[^}]*font-size:\s*11px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.ocr-roi\s*\{[^}]*min-height:\s*42px;[^}]*max-height:\s*42px;[^}]*display:\s*grid;[^}]*grid-template-columns:\s*72px minmax\(0,\s*1fr\);/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.ocr-roi > img\s*\{[^}]*width:\s*72px;[^}]*height:\s*42px;[^}]*display:\s*block;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.ocr-roi figcaption strong,[\s\S]*\.screen--labeling \.ocr-roi > p[\s\S]*\{[^}]*font-size:\s*11px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.ocr-roi > p\s*\{[^}]*display:\s*block;[^}]*padding:\s*0 5px 3px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.question-card:has\(> \.ocr-roi\)\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);/s,
  );
  assert.match(
    labelingSource,
    /className="question-metadata"[\s\S]*question-metadata__type[\s\S]*question\.scoreText/,
    "type and score remain visible after the redundant model-identity strip yields",
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.question-card > \.experimental-proposal\s*\{[^}]*display:\s*none/s,
  );
  assert.match(
    responsiveStyles,
    /\.question-card:has\(> \.ocr-roi\)[\s\S]*> \.ocr-roi\s*\{[^}]*min-height:\s*32px;[^}]*max-height:\s*32px;/s,
  );
});

test("every question exposes honest GPS state and separates AI severity from the user final value", () => {
  assert.match(
    labelingSource,
    /const gpsHeadline = summary\.sourceLocation[\s\S]*"촬영 시작 좌표만 있음"[\s\S]*"시간대별 GPS 없음"/,
  );
  assert.match(
    labelingSource,
    /"GPX를 추가하기 전에는 실제 이동 경로를 생성하지 않음"/,
  );
  assert.match(
    labelingSource,
    /후보 위치나 이동 경로로 사용하지 않음/,
  );
  assert.match(
    labelingSource,
    /className="question-decision-context"[\s\S]*aria-label="위치와 심각도 판단 근거"[\s\S]*<span>GPS<\/span>[\s\S]*\{question\.gpsHeadline\}[\s\S]*\{question\.gpsDetail\}/,
  );
  assert.ok(
    labelingSource.indexOf('className="question-decision-context"') <
      labelingSource.indexOf('className={`weak-case-reference'),
  );
  assert.match(
    labelingSource,
    /<span>AI 추천 심각도<\/span>[\s\S]*\{question\.aiSeverityProposal \?\? "미제안"\}[\s\S]*\{question\.aiSeverityRationale\}/,
  );
  assert.match(
    labelingSource,
    /aria-label="사용자 최종 심각도"[\s\S]*사용자 최종 ·/,
  );
  assert.match(labelingSource, /aiSeverityProposal:\s*null/);
  assert.doesNotMatch(
    labelingSource,
    /setSeverity\(\s*question\.(?:ai|proposed|recommended)Severity/,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.question-decision-context__row > small\s*\{[^}]*font-size:\s*11px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.question-decision-context__user\s*\{[^}]*font-size:\s*11px;/s,
  );
});

test("experimental safety copy reuses spacing without displacing four answers or skip", () => {
  assert.match(
    polishStyles,
    /\.screen--labeling \.question-card:has\(> \.experimental-proposal\)\s*\{[^}]*padding:\s*6px 7px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling[\s\S]*?\.question-card:has\(> \.experimental-proposal\)[\s\S]*?\.question-prompt\s*\{[^}]*margin-block:\s*1px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling[\s\S]*?\.question-card:has\(> \.experimental-proposal\)[\s\S]*?> \.experimental-proposal\s*\{[^}]*margin-top:\s*1px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling[\s\S]*?\.question-card:has\(> \.experimental-proposal\)[\s\S]*?> \.compact-field\s*\{[^}]*margin-top:\s*2px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling[\s\S]*?\.question-card:has\(> \.experimental-proposal\)[\s\S]*?> \.answer-grid\s*\{[^}]*margin-top:\s*2px;/s,
  );
  assert.equal(
    labelingSource.match(/className="answer answer--(?:yes|no|unsure|edit)"/g)
      ?.length,
    4,
  );
  assert.match(
    labelingSource,
    /className="answer-grid"[\s\S]*className="explanation"[\s\S]*onClick=\{\(\) => submit\("skipped"\)\}/,
  );
});

test("temporal and proposal warnings share one bounded review surface", () => {
  assert.equal(
    labelingSource.match(
      /className="notice notice--warning proposal-load-warning"/g,
    )?.length,
    1,
  );
  assert.match(
    labelingSource,
    /\{temporalReview\.warning \|\| proposalWarning \? \([\s\S]*temporalReview\.warning \?\? proposalWarning/,
  );
});

test("analysis owns a modifier and bounds persisted jobs inside the viewport", () => {
  assert.match(analysisSource, /className="screen screen--analysis"/);
  assert.match(
    compactStyles,
    /\.screen--analysis \.analysis-records-panel\s*\{[^}]*flex:\s*0 0 132px;[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--analysis \.analysis-records-table-wrap\s*\{[^}]*height:\s*78px;[^}]*overflow:\s*auto/s,
  );
  assert.match(
    compactStyles,
    /\.screen--analysis \.analysis-layout\s*\{[^}]*flex:\s*1 1 0;[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--analysis \.pipeline-panel,\s*\.screen--analysis \.resource-panel\s*\{[^}]*overflow:\s*hidden/s,
  );
});

test("labeling assigns the only short-screen vertical scroll to candidate media", () => {
  assert.match(labelingSource, /className="screen screen--labeling"/);
  assert.match(
    labelingSource,
    /className="candidate-media-stack"[\s\S]*aria-label="현재 후보 근거 미디어"/,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.candidate-media-stack\s*\{[^}]*overflow-x:\s*hidden;[^}]*overflow-y:\s*auto;[^}]*scrollbar-gutter:\s*stable/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.question-card\s*\{[^}]*display:\s*flex;[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.labeling-sidebar\s*\{[^}]*grid-template-rows:\s*minmax\(0,\s*1fr\) 34px;[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.label-progress\s*\{[^}]*display:\s*grid;[^}]*overflow:\s*hidden/s,
  );
});

test("question selection stays one row and the four primary responses stay visible", () => {
  assert.match(
    compactStyles,
    /\.screen--labeling \.label-queue-controls\s*\{[^}]*grid-template-columns:\s*minmax\(260px,\s*1fr\) auto minmax\(210px,\s*0\.72fr\)/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.label-queue-controls\s*\{[^}]*height:\s*48px;[^}]*max-height:\s*48px;[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.answer-grid\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.question-prompt\s*\{[^}]*min-height:\s*44px;[^}]*flex:\s*0 0 auto;[^}]*-webkit-line-clamp:\s*2/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.question-card > \.button\s*\{[^}]*flex:\s*0 0 auto/s,
  );
});

test("precision severity follow-up keeps 1/2/3 and skip inside the same 720px card", () => {
  assert.match(
    labelingSource,
    /className="followup-card severity-followup"[\s\S]*choice\.key[\s\S]*className="severity-followup__action"/,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.question-card:has\(> \.severity-followup\)\s*\{[^}]*padding:\s*5px 6px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.severity-followup\s*\{[^}]*min-height:\s*54px;[^}]*max-height:\s*54px;[^}]*flex:\s*0 0 54px;[^}]*grid-template-columns:\s*minmax\(128px,\s*0\.68fr\) minmax\(0,\s*1\.32fr\);[^}]*overflow:\s*hidden;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.severity-choice\s*\{[^}]*min-height:\s*42px;[^}]*padding:\s*4px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling[\s\S]*\.question-card:has\(> \.severity-followup\)[\s\S]*> \.experimental-proposal\s*\{[^}]*min-height:\s*48px;[^}]*max-height:\s*48px;[^}]*flex-basis:\s*48px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling[\s\S]*\.question-card:has\(> \.severity-followup\)[\s\S]*> \.ocr-roi\s*\{[^}]*min-height:\s*36px;[^}]*max-height:\s*36px;[^}]*height:\s*36px;[^}]*flex-basis:\s*36px;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling \.severity-followup__intro > small,[\s\S]*\.screen--labeling \.severity-choice small\s*\{[^}]*display:\s*none;/s,
  );
  assert.match(
    polishStyles,
    /\.screen--labeling[\s\S]*\.question-card:has\(> \.severity-followup\)[\s\S]*> \.explanation\s*\{[^}]*min-height:\s*34px;[^}]*max-height:\s*34px;[^}]*overflow:\s*hidden;/s,
  );
  assert.doesNotMatch(
    polishStyles,
    /\.screen--labeling \.question-card:has\(> \.severity-followup\)[^}]*overflow-y:\s*(?:auto|scroll)/s,
  );
});

test("open AI plan participates in layout and keeps plan and result scroll-free", () => {
  assert.match(
    labelingSource,
    /className="auto-label-command__outcome"[\s\S]*className="auto-label-command__preview"[\s\S]*className="auto-label-command__result"/,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.auto-label-command__body\s*\{[^}]*position:\s*static;[^}]*grid-template-areas:[^}]*"form guardrail"[^}]*"outcome outcome";[^}]*overflow:\s*visible/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.auto-label-command__outcome\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*minmax\(0,\s*1\.35fr\) minmax\(320px,\s*0\.65fr\)/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.auto-label-command__preview\s*\{[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.auto-label-command__result\s*\{[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.auto-label-command__progress\s*\{[^}]*overflow:\s*hidden/s,
  );
});

test("standard desktop workbench keeps labeling controls visible without an overlay", () => {
  assert.match(
    compactStyles,
    /@media \(min-width:\s*961px\) and \(min-height:\s*821px\) and \(max-height:\s*1100px\)/,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.auto-label-command__body\s*\{[^}]*position:\s*static;[^}]*grid-template-areas:[^}]*"form guardrail"[^}]*"outcome outcome";[^}]*overflow:\s*visible/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.labeling-sidebar\s*\{[^}]*grid-template-rows:\s*minmax\(0,\s*1fr\) 42px;[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.question-prompt\s*\{[^}]*min-height:\s*48px;[^}]*flex:\s*0 0 auto;[^}]*-webkit-line-clamp:\s*2/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.answer-grid\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)/s,
  );
  assert.match(
    compactStyles,
    /\.screen--labeling \.question-card > \.button\s*\{[^}]*min-height:\s*34px;[^}]*flex:\s*0 0 34px/s,
  );
});

test("standard desktop analysis exposes one bounded vertical scroll owner", () => {
  assert.match(
    compactStyles,
    /\.screen--analysis \.pipeline-panel,\s*\.screen--analysis \.resource-panel\s*\{[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--analysis \.analysis-records-panel\s*\{[^}]*flex:\s*0 0 250px;[^}]*overflow:\s*hidden/s,
  );
  assert.match(
    compactStyles,
    /\.screen--analysis \.analysis-records-table-wrap\s*\{[^}]*height:\s*176px;[^}]*overflow:\s*auto/s,
  );
});
