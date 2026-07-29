import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const electronDirectory = path.resolve(testDirectory, "..");
const mainSource = fs.readFileSync(
  path.join(electronDirectory, "main.mjs"),
  "utf8",
);
const preloadSource = fs.readFileSync(
  path.join(electronDirectory, "preload.cjs"),
  "utf8",
);

test("GPX picker keeps filesystem paths inside Electron main", () => {
  assert.match(
    mainSource,
    /registerHandler\(IPC_CHANNELS\.DIALOG_GPX_FILE,[\s\S]*?gpxSelections\.register\(result\.filePaths\[0\]\)/,
  );
  assert.match(
    mainSource,
    /gpxSelections\.resolvePath\(payload\.gpxSelectionId\)/,
  );
  assert.match(
    mainSource,
    /delete bridgePayload\.gpx_selection_id;[\s\S]*bridgePayload\.selected_path = selectedPath;/,
  );
  assert.doesNotMatch(preloadSource, /selected_path|selectedPath/);
});

test("GPX preload exposes only typed operations and opaque handles", () => {
  assert.match(
    preloadSource,
    /selectGpxFile:\s*\(\)\s*=>\s*invoke\(CHANNELS\.DIALOG_GPX_FILE\)/,
  );
  assert.match(
    preloadSource,
    /gps:\s*Object\.freeze\(\{[\s\S]*importTrack:[\s\S]*getTrack:[\s\S]*updateOffset:/,
  );
  assert.doesNotMatch(preloadSource, /showOpenDialog|node:fs|child_process/);
});
