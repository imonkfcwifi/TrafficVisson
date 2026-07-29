import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const mainSource = fs.readFileSync(
  path.resolve(testDirectory, "..", "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.resolve(testDirectory, "..", "preload.cjs"),
  "utf8",
);

test("report generation IPC forwards only the validated scoped contract", () => {
  const start = mainSource.indexOf(
    "registerHandler(IPC_CHANNELS.REPORT_GENERATE",
  );
  const end = mainSource.indexOf(
    "registerHandler(IPC_CHANNELS.REPORT_DRAFT",
    start,
  );
  assert.ok(start >= 0 && end > start);
  const handlerSource = mainSource.slice(start, end);
  assert.match(
    handlerSource,
    /`\/projects\/\$\{encodeId\(payload\.projectId\)\}\/reports\/generate`[\s\S]*toBridgePayload\(IPC_CHANNELS\.REPORT_GENERATE,\s*payload\)/,
  );
  assert.doesNotMatch(
    handlerSource,
    /approvedIncidents|severity|maintenanceAction|ocrText|evidenceRefs/,
  );
});

test("preload exposes no report generation content or generic bridge escape hatch", () => {
  assert.match(
    preloadSource,
    /reports:\s*Object\.freeze\(\{[\s\S]*generate:\s*\(request\)\s*=>\s*invoke\(CHANNELS\.REPORT_GENERATE,\s*request\)/,
  );
  assert.doesNotMatch(
    preloadSource,
    /reports:[\s\S]{0,800}(?:fetch|requestRaw|invokeRaw)/,
  );
});
