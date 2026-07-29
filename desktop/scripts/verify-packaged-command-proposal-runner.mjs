#!/usr/bin/env node
import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const defaultDesktopRoot = path.resolve(scriptDirectory, "..");
const SHA256 = /^[a-f0-9]{64}$/;

async function sha256(filePath) {
  const hash = createHash("sha256");
  const handle = await fs.open(filePath, "r");
  try {
    for await (const chunk of handle.createReadStream()) hash.update(chunk);
  } finally {
    await handle.close();
  }
  return hash.digest("hex");
}

async function requiredFile(filePath, label) {
  let stat;
  try {
    stat = await fs.stat(filePath);
  } catch {
    throw new Error(`${label} is missing`);
  }
  if (!stat.isFile()) throw new Error(`${label} is not a regular file`);
  return filePath;
}

async function readJson(filePath, label) {
  let value;
  try {
    value = JSON.parse(await fs.readFile(filePath, "utf8"));
  } catch (error) {
    throw new Error(`${label} is not valid JSON: ${error.message}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return value;
}

function requireSha256(value, label) {
  if (typeof value !== "string" || !SHA256.test(value)) {
    throw new Error(`${label} must be a lowercase SHA-256 digest`);
  }
  return value;
}

export async function verifyPackagedCommandProposalRunner({
  desktopRoot = defaultDesktopRoot,
  packagedRunnerRoot = path.join(
    desktopRoot,
    "release",
    "win-unpacked",
    "resources",
    "command-proposal-runner",
  ),
} = {}) {
  const resolvedDesktopRoot = path.resolve(desktopRoot);
  const buildRoot = path.join(resolvedDesktopRoot, "build");
  const trustedRunnerRoot = path.join(buildRoot, "command-proposal-runner");
  const expectedHashPath = path.join(
    buildRoot,
    "command-proposal-runner.sha256",
  );
  const bridgeTrustSourcePath = path.join(
    resolvedDesktopRoot,
    "bridge",
    "app",
    "command_proposal_generation.py",
  );
  const trustedRunnerManifestPath = path.join(
    trustedRunnerRoot,
    "runner-manifest.json",
  );
  const trustedModelManifestPath = path.join(
    trustedRunnerRoot,
    "model-pack",
    "manifest.json",
  );
  const packagedRunnerManifestPath = path.join(
    path.resolve(packagedRunnerRoot),
    "runner-manifest.json",
  );
  const packagedModelManifestPath = path.join(
    path.resolve(packagedRunnerRoot),
    "model-pack",
    "manifest.json",
  );

  for (const [filePath, label] of [
    [expectedHashPath, "trusted runner release hash"],
    [bridgeTrustSourcePath, "frozen bridge trust source"],
    [trustedRunnerManifestPath, "trusted runner manifest"],
    [trustedModelManifestPath, "trusted model-pack manifest"],
    [packagedRunnerManifestPath, "packaged runner manifest"],
    [packagedModelManifestPath, "packaged model-pack manifest"],
  ]) {
    await requiredFile(filePath, label);
  }

  const expectedRunnerManifestSha256 = requireSha256(
    (await fs.readFile(expectedHashPath, "utf8")).trim(),
    "trusted runner release hash",
  );
  const trustedRunnerManifestSha256 = await sha256(
    trustedRunnerManifestPath,
  );
  if (trustedRunnerManifestSha256 !== expectedRunnerManifestSha256) {
    throw new Error(
      "trusted source runner manifest does not match its release hash",
    );
  }

  const bridgeTrustSource = await fs.readFile(
    bridgeTrustSourcePath,
    "utf8",
  );
  if (!bridgeTrustSource.includes(`"${expectedRunnerManifestSha256}"`)) {
    throw new Error(
      "frozen bridge source does not pin the trusted runner manifest hash",
    );
  }

  const packagedRunnerManifestSha256 = await sha256(
    packagedRunnerManifestPath,
  );
  if (packagedRunnerManifestSha256 !== expectedRunnerManifestSha256) {
    throw new Error(
      "packaged runner manifest is stale or differs from the trusted source build",
    );
  }

  const trustedRunnerManifest = await readJson(
    trustedRunnerManifestPath,
    "trusted runner manifest",
  );
  const packagedRunnerManifest = await readJson(
    packagedRunnerManifestPath,
    "packaged runner manifest",
  );
  const expectedModelManifestSha256 = requireSha256(
    trustedRunnerManifest.model_manifest_sha256,
    "trusted model-pack manifest hash",
  );
  if (
    packagedRunnerManifest.model_manifest_sha256 !==
      expectedModelManifestSha256 ||
    packagedRunnerManifest.model?.manifest_sha256 !==
      expectedModelManifestSha256
  ) {
    throw new Error(
      "packaged runner does not reference the trusted model-pack manifest",
    );
  }

  const trustedModelManifestSha256 = await sha256(
    trustedModelManifestPath,
  );
  if (trustedModelManifestSha256 !== expectedModelManifestSha256) {
    throw new Error(
      "trusted model-pack manifest does not match the trusted runner",
    );
  }
  const packagedModelManifestSha256 = await sha256(
    packagedModelManifestPath,
  );
  if (packagedModelManifestSha256 !== expectedModelManifestSha256) {
    throw new Error(
      "packaged model-pack manifest is stale or differs from the trusted source build",
    );
  }

  return {
    schema_version:
      "trafficvisson.packaged-command-proposal-runner-trust.v1",
    runner_manifest_sha256: expectedRunnerManifestSha256,
    model_manifest_sha256: expectedModelManifestSha256,
    source_build_match: true,
    frozen_bridge_pin_match: true,
    packaged_copy_match: true,
  };
}

const isDirectExecution =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));

if (isDirectExecution) {
  console.log(
    JSON.stringify(await verifyPackagedCommandProposalRunner()),
  );
}
