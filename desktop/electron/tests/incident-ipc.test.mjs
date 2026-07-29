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

test("incident list IPC derives its route and filter only from validated fields", () => {
  const start = mainSource.indexOf(
    "registerHandler(IPC_CHANNELS.INCIDENT_LIST",
  );
  const end = mainSource.indexOf(
    "registerHandler(IPC_CHANNELS.AUTO_LABEL_COMPILE",
    start,
  );
  assert.ok(start >= 0 && end > start);
  const handlerSource = mainSource.slice(start, end);
  assert.match(
    handlerSource,
    /IPC_CHANNELS\.INCIDENT_LIST[\s\S]*`\/projects\/\$\{encodeId\(payload\.projectId\)\}\/incidents`[\s\S]*analysis_job_id:\s*payload\.analysisJobId[\s\S]*include_inactive:\s*payload\.includeInactive/,
  );
  assert.doesNotMatch(handlerSource, /toBridgePayload|body:/);
});

test("preload exposes a narrow incident list method without a generic request escape hatch", () => {
  assert.match(
    preloadSource,
    /incidents:\s*Object\.freeze\(\{\s*list:\s*\(request\)\s*=>\s*invoke\(CHANNELS\.INCIDENT_LIST,\s*request\),\s*\}\)/s,
  );
  assert.doesNotMatch(preloadSource, /incidents:[\s\S]{0,200}(?:fetch|requestRaw|invokeRaw)/);
});
