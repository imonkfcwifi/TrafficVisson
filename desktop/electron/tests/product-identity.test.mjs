import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  PRODUCT_IDENTITY,
  applyProductIdentity,
} from "../product-identity.mjs";

const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const electronDirectory = path.resolve(testDirectory, "..");
const desktopDirectory = path.resolve(electronDirectory, "..");
const packageJsonPath = path.join(desktopDirectory, "package.json");
const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));

function createFakeApp() {
  const calls = [];
  return {
    calls,
    setName(value) {
      calls.push(["setName", value]);
    },
    setAppUserModelId(value) {
      calls.push(["setAppUserModelId", value]);
    },
  };
}

test("Windows runtime identity is owned by TrafficSuperVision", () => {
  const fakeApp = createFakeApp();

  applyProductIdentity(fakeApp, "win32");

  assert.deepEqual(fakeApp.calls, [
    ["setName", "TrafficSuperVision"],
    ["setAppUserModelId", "kr.co.trafficsupervision.desktop"],
  ]);
});

test("non-Windows runtime keeps the TrafficSuperVision product name", () => {
  const fakeApp = createFakeApp();

  applyProductIdentity(fakeApp, "darwin");

  assert.deepEqual(fakeApp.calls, [["setName", "TrafficSuperVision"]]);
});

test("packaged executable, installer, shortcuts, and uninstall entry share one identity", () => {
  assert.deepEqual(PRODUCT_IDENTITY, {
    appUserModelId: "kr.co.trafficsupervision.desktop",
    productName: "TrafficSuperVision",
  });
  assert.equal(packageJson.build.appId, PRODUCT_IDENTITY.appUserModelId);
  assert.equal(packageJson.build.productName, PRODUCT_IDENTITY.productName);
  assert.equal(packageJson.build.executableName, PRODUCT_IDENTITY.productName);
  assert.equal(
    packageJson.build.artifactName,
    "TrafficSuperVision-Setup-${version}-${arch}.${ext}",
  );
  assert.equal(
    packageJson.build.nsis.shortcutName,
    PRODUCT_IDENTITY.productName,
  );
  assert.equal(
    packageJson.build.nsis.uninstallDisplayName,
    PRODUCT_IDENTITY.productName,
  );
});

test("configured Windows icon paths must resolve to a real asset", () => {
  const configuredIcon = packageJson.build.win?.icon;
  if (configuredIcon === undefined) return;

  const iconPath = path.resolve(desktopDirectory, configuredIcon);
  assert.ok(
    fs.existsSync(iconPath),
    `configured Windows icon does not exist: ${iconPath}`,
  );
});

test("runtime and packaging sources contain no legacy application identity", () => {
  const legacyIdentity = ["com", "superkorea", "editor"].join(".");
  const mainSource = fs.readFileSync(
    path.join(electronDirectory, "main.mjs"),
    "utf8",
  );
  const identitySource = fs.readFileSync(
    path.join(electronDirectory, "product-identity.mjs"),
    "utf8",
  );

  assert.match(mainSource, /applyProductIdentity\(app\);/);
  assert.doesNotMatch(mainSource, new RegExp(legacyIdentity, "i"));
  assert.doesNotMatch(identitySource, new RegExp(legacyIdentity, "i"));
  assert.doesNotMatch(JSON.stringify(packageJson.build), new RegExp(legacyIdentity, "i"));
});
