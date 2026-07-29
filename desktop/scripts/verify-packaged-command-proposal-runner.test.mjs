import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { verifyPackagedCommandProposalRunner } from "./verify-packaged-command-proposal-runner.mjs";

const scriptRoot = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(scriptRoot, "..");

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

async function createTrustFixture() {
  const desktopRoot = await fs.mkdtemp(
    path.join(os.tmpdir(), "trafficsupervision-runner-trust-"),
  );
  const trustedRoot = path.join(
    desktopRoot,
    "build",
    "command-proposal-runner",
  );
  const packagedRoot = path.join(
    desktopRoot,
    "release",
    "win-unpacked",
    "resources",
    "command-proposal-runner",
  );
  const bridgeRoot = path.join(desktopRoot, "bridge", "app");
  await fs.mkdir(path.join(trustedRoot, "model-pack"), { recursive: true });
  await fs.mkdir(path.join(packagedRoot, "model-pack"), { recursive: true });
  await fs.mkdir(bridgeRoot, { recursive: true });

  const modelManifestBytes = Buffer.from(
    JSON.stringify({
      schema_version: "trafficvisson.command-proposal-model-pack.v2",
      pack_id: "fixture-grounding-dino",
      pack_version: "1.0.0",
    }),
    "utf8",
  );
  const modelManifestSha256 = digest(modelManifestBytes);
  const runnerManifestBytes = Buffer.from(
    JSON.stringify({
      schema_version: "trafficvisson.command-proposal-runner.v1",
      runner_id: "fixture-runner",
      runner_version: "1.0.0",
      model_manifest_sha256: modelManifestSha256,
      model: {
        manifest_sha256: modelManifestSha256,
      },
    }),
    "utf8",
  );
  const runnerManifestSha256 = digest(runnerManifestBytes);

  for (const root of [trustedRoot, packagedRoot]) {
    await fs.writeFile(
      path.join(root, "runner-manifest.json"),
      runnerManifestBytes,
    );
    await fs.writeFile(
      path.join(root, "model-pack", "manifest.json"),
      modelManifestBytes,
    );
  }
  await fs.writeFile(
    path.join(desktopRoot, "build", "command-proposal-runner.sha256"),
    `${runnerManifestSha256}\n`,
    "utf8",
  );
  await fs.writeFile(
    path.join(bridgeRoot, "command_proposal_generation.py"),
    `PINNED_PROGRAMDATA_RUNNER_MANIFEST_SHA256 = "${runnerManifestSha256}"\n`,
    "utf8",
  );
  return {
    desktopRoot,
    packagedRoot,
    runnerManifestSha256,
    modelManifestSha256,
  };
}

test("packaged runner trust accepts the exact source build and frozen bridge pin", async (t) => {
  const fixture = await createTrustFixture();
  t.after(() =>
    fs.rm(fixture.desktopRoot, { recursive: true, force: true }),
  );

  const result = await verifyPackagedCommandProposalRunner({
    desktopRoot: fixture.desktopRoot,
    packagedRunnerRoot: fixture.packagedRoot,
  });

  assert.deepEqual(result, {
    schema_version:
      "trafficvisson.packaged-command-proposal-runner-trust.v1",
    runner_manifest_sha256: fixture.runnerManifestSha256,
    model_manifest_sha256: fixture.modelManifestSha256,
    source_build_match: true,
    frozen_bridge_pin_match: true,
    packaged_copy_match: true,
  });
});

test("packaged runner trust rejects an internally plausible stale runner manifest", async (t) => {
  const fixture = await createTrustFixture();
  t.after(() =>
    fs.rm(fixture.desktopRoot, { recursive: true, force: true }),
  );
  const packagedManifestPath = path.join(
    fixture.packagedRoot,
    "runner-manifest.json",
  );
  const stale = JSON.parse(
    await fs.readFile(packagedManifestPath, "utf8"),
  );
  stale.runner_version = "0.9.0-stale";
  await fs.writeFile(packagedManifestPath, JSON.stringify(stale), "utf8");

  await assert.rejects(
    verifyPackagedCommandProposalRunner({
      desktopRoot: fixture.desktopRoot,
      packagedRunnerRoot: fixture.packagedRoot,
    }),
    /packaged runner manifest is stale/,
  );
});

test("packaged runner trust rejects a stale model-pack copy", async (t) => {
  const fixture = await createTrustFixture();
  t.after(() =>
    fs.rm(fixture.desktopRoot, { recursive: true, force: true }),
  );
  await fs.appendFile(
    path.join(fixture.packagedRoot, "model-pack", "manifest.json"),
    "\n",
    "utf8",
  );

  await assert.rejects(
    verifyPackagedCommandProposalRunner({
      desktopRoot: fixture.desktopRoot,
      packagedRunnerRoot: fixture.packagedRoot,
    }),
    /packaged model-pack manifest is stale/,
  );
});

test("packaged runner trust rejects a frozen bridge pin mismatch", async (t) => {
  const fixture = await createTrustFixture();
  t.after(() =>
    fs.rm(fixture.desktopRoot, { recursive: true, force: true }),
  );
  await fs.writeFile(
    path.join(
      fixture.desktopRoot,
      "bridge",
      "app",
      "command_proposal_generation.py",
    ),
    `PINNED_PROGRAMDATA_RUNNER_MANIFEST_SHA256 = "${"f".repeat(64)}"\n`,
    "utf8",
  );

  await assert.rejects(
    verifyPackagedCommandProposalRunner({
      desktopRoot: fixture.desktopRoot,
      packagedRunnerRoot: fixture.packagedRoot,
    }),
    /frozen bridge source does not pin/,
  );
});

test("Windows packaging and release-manifest generation both enforce the trust gate", async () => {
  const packageJson = JSON.parse(
    await fs.readFile(path.join(desktopRoot, "package.json"), "utf8"),
  );
  const packageCommand = packageJson.scripts["package:win"];
  assert.match(
    packageCommand,
    /electron-builder[\s\S]*npm run verify:packaged-command-runner[\s\S]*write-release-manifest\.mjs/,
  );
  assert.equal(
    packageJson.scripts["verify:packaged-command-runner"],
    "node scripts/verify-packaged-command-proposal-runner.mjs",
  );

  const releaseManifestSource = await fs.readFile(
    path.join(scriptRoot, "write-release-manifest.mjs"),
    "utf8",
  );
  assert.match(
    releaseManifestSource,
    /verifyPackagedCommandProposalRunner\(\{[\s\S]*desktopRoot,[\s\S]*packagedRunnerRoot,[\s\S]*\}\)/,
  );
  assert.match(releaseManifestSource, /trust_chain: packagedRunnerTrust/);
});
