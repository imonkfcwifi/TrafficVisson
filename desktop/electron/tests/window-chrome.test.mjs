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

test("Windows uses the integrated native caption overlay instead of a frameless window", () => {
  assert.match(mainSource, /process\.platform === "win32"/);
  assert.match(mainSource, /titleBarStyle:\s*"hidden"/);
  assert.match(mainSource, /titleBarOverlay:\s*\{/);
  assert.match(mainSource, /thickFrame:\s*true/);
  assert.doesNotMatch(mainSource, /^\s*frame:\s*false/m);
});

test("the generic Electron application menu is removed outside macOS", () => {
  assert.match(mainSource, /autoHideMenuBar:\s*process\.platform !== "darwin"/);
  assert.match(mainSource, /Menu\.setApplicationMenu\(null\)/);
  assert.match(mainSource, /window\.removeMenu\(\)/);
});

test("native chrome keeps the renderer process security boundary intact", () => {
  assert.match(mainSource, /nodeIntegration:\s*false/);
  assert.match(mainSource, /contextIsolation:\s*true/);
  assert.match(mainSource, /sandbox:\s*true/);
  assert.match(mainSource, /webSecurity:\s*true/);
});

test("the main window cannot miss ready-to-show while navigation is loading", () => {
  const readyListener = mainSource.indexOf(
    'window.once("ready-to-show", () => window.show())',
  );
  const navigation = mainSource.indexOf("if (devServerUrl) {", readyListener);

  assert.notEqual(readyListener, -1);
  assert.notEqual(navigation, -1);
  assert.ok(
    readyListener < navigation,
    "ready-to-show must be registered before loadURL starts",
  );
  assert.match(
    mainSource,
    /if \(!smokeTestMode && !window\.isVisible\(\)\) \{\s*window\.show\(\);/s,
  );
});
