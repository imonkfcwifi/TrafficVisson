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

test("evidence export receives the bounded 30 minute timeout only in main", () => {
  assert.match(
    mainSource,
    /const EXPORT_TIMEOUT_MS = 30 \* 60 \* 1000;/,
  );

  const start = mainSource.indexOf(
    "registerHandler(IPC_CHANNELS.EXPORT_CREATE",
  );
  const end = mainSource.indexOf("\n  });\n}", start);
  assert.ok(start >= 0 && end > start);
  const handlerSource = mainSource.slice(start, end);

  assert.match(
    handlerSource,
    /supervisor\.request\([\s\S]*timeoutMs:\s*EXPORT_TIMEOUT_MS/,
  );
  assert.doesNotMatch(handlerSource, /payload\.(?:timeout|timeoutMs)/);
});

test("renderer preload has no generic timeout or bridge request escape hatch", () => {
  assert.match(
    preloadSource,
    /exports:\s*Object\.freeze\(\{\s*create:\s*\(request\)\s*=>\s*invoke\(CHANNELS\.EXPORT_CREATE,\s*request\),\s*\}\)/s,
  );
  assert.doesNotMatch(
    preloadSource,
    /(?:timeoutMs|requestRaw|invokeRaw|bridgeClient)/,
  );
});
