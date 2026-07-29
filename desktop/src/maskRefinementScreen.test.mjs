import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const directory = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
  path.resolve(directory, "screens", "LabelingScreen.tsx"),
  "utf8",
);

test("labeling region edit is capability-gated and never draws a fake box", () => {
  assert.match(source, /maskRefinements[\s\S]*?\.capability/);
  assert.match(
    source,
    /maskRefinementState\.status !== "ready"/,
  );
  assert.match(source, /maskRefinements[\s\S]*?\.refine/);
  assert.match(source, /parseMaskRefinementProposal/);
  assert.match(source, /sam2-mask-overlay/);
  assert.doesNotMatch(source, /임시 박스|수동 임시 영역|방향키로 임시/);
  assert.doesNotMatch(
    source,
    /editMode[\s\S]{0,100}\?\s*\{\s*x:\s*point\.x/,
  );
});

test("SAM2 proposal requires explicit reconfirmation before edited answer", () => {
  assert.match(
    source,
    /!maskRefinement \|\| !serverQuestionId/,
  );
  assert.match(source, /새 마스크 확인/);
  assert.match(
    source,
    /answer === "edited" && maskRefinement && serverQuestionId/,
  );
  assert.match(source, /maskRefinementId:\s*maskRefinement\.refinementId/);
  assert.match(
    source,
    /positive && geometry && answer !== "edited"/,
  );
});

