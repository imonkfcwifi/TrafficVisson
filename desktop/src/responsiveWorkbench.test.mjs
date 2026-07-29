import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const entrySource = await readFile(new URL("./main.tsx", import.meta.url), "utf8");
const responsiveStyles = await readFile(
  new URL("./styles-responsive-workbench.css", import.meta.url),
  "utf8",
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
    /\.screen--labeling \.question-card > \.question-skip\s*\{[^}]*min-height:\s*24px;[^}]*flex-basis:\s*24px/s,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.label-progress\s*\{[^}]*min-height:\s*34px;[^}]*height:\s*34px/s,
  );
  assert.match(
    responsiveStyles,
    /\.screen--labeling \.experimental-proposal ~ \.explanation p,[\s\S]*white-space:\s*nowrap/s,
  );
});
