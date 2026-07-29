#!/usr/bin/env node
import { createHash } from "node:crypto";
import {
  existsSync,
  readFileSync,
  statSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const buildRoot = path.join(desktopRoot, "build");
const runnerRoot = path.join(buildRoot, "command-proposal-runner");
const runnerManifestPath = path.join(runnerRoot, "runner-manifest.json");
const modelRoot = path.join(runnerRoot, "model-pack");
const modelManifestPath = path.join(modelRoot, "manifest.json");
const expectedPath = path.join(buildRoot, "command-proposal-runner.sha256");
const trustSourcePath = path.join(
  desktopRoot,
  "bridge",
  "app",
  "command_proposal_generation.py",
);

function sha256Buffer(value) {
  return createHash("sha256").update(value).digest("hex");
}

function sha256File(filePath) {
  return sha256Buffer(readFileSync(filePath));
}

function requiredFile(filePath) {
  if (!existsSync(filePath) || !statSync(filePath).isFile()) {
    throw new Error(`Required command proposal runner file is missing: ${filePath}`);
  }
}

for (const filePath of [
  runnerManifestPath,
  modelManifestPath,
  expectedPath,
  trustSourcePath,
]) {
  requiredFile(filePath);
}
const manifestBytes = readFileSync(runnerManifestPath);
const manifestSha256 = sha256Buffer(manifestBytes);
const expectedSha256 = readFileSync(expectedPath, "utf8").trim();
if (!/^[a-f0-9]{64}$/.test(expectedSha256) || expectedSha256 !== manifestSha256) {
  throw new Error("Command proposal runner manifest does not match its release hash");
}
const trustSource = readFileSync(trustSourcePath, "utf8");
if (!trustSource.includes(`"${manifestSha256}"`)) {
  throw new Error("Frozen bridge source does not pin the runner manifest hash");
}
const manifest = JSON.parse(manifestBytes.toString("utf8"));
const executablePath = path.join(
  runnerRoot,
  "trafficvisson-command-proposal-runner.exe",
);
requiredFile(executablePath);
if (sha256File(executablePath) !== manifest.executable_sha256) {
  throw new Error("Command proposal runner executable hash does not match");
}
const modelManifestBytes = readFileSync(modelManifestPath);
const modelManifestSha256 = sha256Buffer(modelManifestBytes);
if (
  modelManifestSha256 !== manifest.model_manifest_sha256 ||
  manifest.model?.manifest_sha256 !== modelManifestSha256
) {
  throw new Error("Command proposal model manifest hash does not match");
}
const modelManifest = JSON.parse(modelManifestBytes.toString("utf8"));
if (
  modelManifest.schema_version !==
    "trafficvisson.command-proposal-model-pack.v1" ||
  !Array.isArray(modelManifest.artifacts) ||
  modelManifest.review_policy?.bridge_review_proposal_import_allowed !== true ||
  modelManifest.review_policy?.automatic_labeling_allowed !== false
) {
  throw new Error("Command proposal model review policy is invalid");
}
for (const artifact of modelManifest.artifacts) {
  if (
    !artifact ||
    typeof artifact.path !== "string" ||
    path.isAbsolute(artifact.path) ||
    artifact.path.split(/[\\/]/).includes("..") ||
    !Number.isSafeInteger(artifact.size_bytes) ||
    !/^[a-f0-9]{64}$/.test(artifact.sha256)
  ) {
    throw new Error("Command proposal model artifact inventory is invalid");
  }
  const artifactPath = path.join(modelRoot, ...artifact.path.split("/"));
  requiredFile(artifactPath);
  if (
    statSync(artifactPath).size !== artifact.size_bytes ||
    sha256File(artifactPath) !== artifact.sha256
  ) {
    throw new Error(`Command proposal model artifact changed: ${artifact.path}`);
  }
}
console.log(`Verified command proposal runner manifest: ${manifestSha256}`);
