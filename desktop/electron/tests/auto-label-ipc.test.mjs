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
const desktopDirectory = path.resolve(testDirectory, "..", "..");
const packageJson = JSON.parse(
  fs.readFileSync(path.join(desktopDirectory, "package.json"), "utf8"),
);

test("AI proposal generation receives a bounded main-process timeout", () => {
  assert.match(
    mainSource,
    /const AUTO_LABELING_RUN_TIMEOUT_MS = 10 \* 60 \* 1000;/,
  );

  const start = mainSource.indexOf(
    "registerHandler(IPC_CHANNELS.AUTO_LABEL_RUN",
  );
  const end = mainSource.indexOf(
    "registerHandler(IPC_CHANNELS.LABEL_STATS",
    start,
  );
  assert.ok(start >= 0 && end > start);
  const handlerSource = mainSource.slice(start, end);

  assert.match(
    handlerSource,
    /supervisor\.request\([\s\S]*timeoutMs:\s*AUTO_LABELING_RUN_TIMEOUT_MS/,
  );
  assert.doesNotMatch(handlerSource, /payload\.(?:timeout|timeoutMs)/);
});

test("renderer cannot choose a runner, executable, or timeout", () => {
  const start = preloadSource.indexOf("autoLabeling: Object.freeze");
  const end = preloadSource.indexOf("labels: Object.freeze", start);
  assert.ok(start >= 0 && end > start);
  const autoLabelingSource = preloadSource.slice(start, end);

  assert.match(
    autoLabelingSource,
    /compile:\s*\(request\)\s*=>\s*invoke\(CHANNELS\.AUTO_LABEL_COMPILE,\s*request\)/,
  );
  assert.match(
    autoLabelingSource,
    /run:\s*\(request\)\s*=>\s*invoke\(CHANNELS\.AUTO_LABEL_RUN,\s*request\)/,
  );
  assert.doesNotMatch(
    autoLabelingSource,
    /(?:timeout|runner|executable|script|path|commandLine)/i,
  );
});

test("packaged builds ship one fixed command proposal runner resource", () => {
  const resources = packageJson.build?.extraResources ?? [];
  const runner = resources.find(
    (entry) => entry.to === "command-proposal-runner",
  );
  assert.deepEqual(runner, {
    from: "build/command-proposal-runner",
    to: "command-proposal-runner",
    filter: ["**/*"],
  });
  assert.match(
    mainSource,
    /path\.join\(process\.resourcesPath, "command-proposal-runner"\)/,
  );
  assert.doesNotMatch(mainSource, /payload\.(?:runner|executable|script|path)/);
});
