import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const entrySource = await readFile(new URL("./main.tsx", import.meta.url), "utf8");
const responsiveStyles = await readFile(
  new URL("./styles-responsive-workbench.css", import.meta.url),
  "utf8",
);
const rendererStyles = await Promise.all(
  [
    "./styles.css",
    "./styles-compact-secondary.css",
    "./styles-compact-analysis-labeling.css",
    "./styles-visual-polish.css",
    "./styles-responsive-workbench.css",
  ].map((path) => readFile(new URL(path, import.meta.url), "utf8")),
);

test("responsive workbench is the final renderer cascade", () => {
  assert.match(
    entrySource,
    /import "\.\/styles-visual-polish\.css";\s*import "\.\/styles-responsive-workbench\.css";/,
  );
});

test("native desktop client width enters compact layout before controls crowd", () => {
  assert.match(
    responsiveStyles,
    /@media \(min-width: 961px\) and \(max-width: 1399px\)/,
  );
  assert.match(
    responsiveStyles,
    /grid-template-areas:\s*"brand project context diagnostics"/,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.labeling-layout\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1\.72fr\) minmax\(380px, 0\.9fr\)/s,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.labeling-macro-flow small\s*\{[^}]*display:\s*none/s,
  );
});

test("window resizing uses bounded layout motion and respects reduced motion", () => {
  assert.match(
    responsiveStyles,
    /--layout-motion-duration:\s*180ms/,
  );
  assert.match(
    responsiveStyles,
    /grid-template-columns var\(--layout-motion-duration\) var\(--layout-motion-easing\)/,
  );
  assert.match(
    responsiveStyles,
    /@media \(prefers-reduced-motion: reduce\)[\s\S]*--layout-motion-duration:\s*0ms[\s\S]*transition:\s*none/s,
  );
});

test("minimum Windows client keeps the full decision bar inside the question card", () => {
  assert.match(
    responsiveStyles,
    /@media \(min-width: 961px\) and \(max-height: 820px\)/,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.question-card > \.question-skip\s*\{[^}]*min-height:\s*32px;[^}]*flex-basis:\s*32px/s,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.label-progress\s*\{[^}]*min-height:\s*34px;[^}]*height:\s*34px/s,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.experimental-proposal ~ \.explanation p,[\s\S]*text-overflow:\s*ellipsis;[\s\S]*white-space:\s*nowrap/s,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.question-prompt,[\s\S]*min-height:\s*54px;[\s\S]*font-size:\s*15\.5px;[\s\S]*-webkit-line-clamp:\s*3/s,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.weak-case-reference__detail p\s*\{[^}]*white-space:\s*normal;[^}]*-webkit-line-clamp:\s*2/s,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.question-card > \.experimental-proposal\s*\{[^}]*display:\s*none/s,
    "the duplicate model-identity row yields before core review controls at the minimum window",
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.explanation,[\s\S]*min-height:\s*20px;[\s\S]*max-height:\s*20px;[\s\S]*flex-basis:\s*20px/s,
  );
  assert.match(
    responsiveStyles,
    /\.question-card:has\(> \.ocr-roi\)[\s\S]*> \.weak-case-reference\s*\{[^}]*min-height:\s*56px;[^}]*max-height:\s*56px/s,
  );
  assert.match(
    responsiveStyles,
    /\.question-card:has\(> \.severity-followup\)[\s\S]*> \.severity-followup\s*\{[^}]*min-height:\s*48px;[^}]*max-height:\s*48px;[^}]*flex:\s*0 0 48px/s,
  );
  assert.match(
    responsiveStyles,
    /\.question-card:has\(> \.ocr-roi\)\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/s,
  );
  assert.match(
    responsiveStyles,
    /\.question-card:has\(> \.edit-actions\)[\s\S]*> \.answer-grid\s*\{[^}]*display:\s*none/s,
  );
  assert.match(
    responsiveStyles,
    /\.question-card:has\(> \.ocr-roi\):has\(> \.proposal-load-warning\)[\s\S]*> \.weak-case-reference\s*\{[^}]*min-height:\s*46px;[^}]*max-height:\s*46px/s,
  );
  assert.match(
    responsiveStyles,
    /\.question-card:has\(> \.ocr-roi\):has\(> \.proposal-load-warning\)[\s\S]*> \.explanation\s*\{[^}]*display:\s*none/s,
  );
});

test("compact layouts never shrink visible product copy below 11px", () => {
  for (const stylesheet of rendererStyles) {
    const fontSizes = stylesheet.matchAll(/font-size:\s*([0-9.]+)px/g);
    for (const match of fontSizes) {
      assert.ok(
        Number(match[1]) >= 11,
        `found unreadable ${match[1]}px product text`,
      );
    }
  }
});
