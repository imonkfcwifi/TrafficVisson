import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  releaseArtifactPaths,
  sha256,
  verifyBundledRunnerIntegrity,
} from "./write-release-manifest.mjs";

async function createRunnerFixture() {
  const root = await fs.mkdtemp(
    path.join(os.tmpdir(), "trafficvisson-release-runner-"),
  );
  const modelPackRoot = path.join(root, "model-pack");
  const modelDirectory = path.join(modelPackRoot, "onnx");
  await fs.mkdir(modelDirectory, { recursive: true });

  const executablePath = path.join(
    root,
    "trafficvisson-command-proposal-runner.exe",
  );
  const modelPath = path.join(modelDirectory, "model_q4.onnx");
  await fs.writeFile(executablePath, "fixed runner fixture", "utf8");
  await fs.writeFile(modelPath, "fixed model fixture", "utf8");

  const modelStat = await fs.stat(modelPath);
  const modelManifestPath = path.join(modelPackRoot, "manifest.json");
  const modelManifest = {
    schema_version: "trafficvisson.command-proposal-model-pack.v1",
    pack_id: "trafficvisson-test-pack",
    pack_version: "0.0.1",
    source: {
      selected_model: "onnx/model_q4.onnx",
    },
    review_policy: {
      review_only: true,
      automatic_labeling_allowed: false,
    },
    artifacts: [
      {
        path: "onnx/model_q4.onnx",
        size_bytes: modelStat.size,
        sha256: await sha256(modelPath),
      },
    ],
  };
  await fs.writeFile(
    modelManifestPath,
    JSON.stringify(modelManifest),
    "utf8",
  );

  const modelManifestSha256 = await sha256(modelManifestPath);
  const runnerManifest = {
    schema_version: "trafficvisson.command-proposal-runner.v1",
    runner_id: "trafficvisson.test-runner",
    runner_version: "0.0.1",
    executable_sha256: await sha256(executablePath),
    model_manifest_sha256: modelManifestSha256,
    model: {
      provider: "fixture",
      pack_id: "trafficvisson-test-pack",
      pack_version: "0.0.1",
      manifest_sha256: modelManifestSha256,
    },
  };
  await fs.writeFile(
    path.join(root, "runner-manifest.json"),
    JSON.stringify(runnerManifest),
    "utf8",
  );

  return { root, modelPath };
}

test("release artifact inventory includes the runner trust chain", () => {
  const artifacts = releaseArtifactPaths("9.8.7-test").map((entry) =>
    entry.replaceAll("\\", "/"),
  );

  assert.equal(artifacts.length, 11);
  assert.equal(new Set(artifacts).size, artifacts.length);
  assert.ok(
    artifacts.includes(
      "win-unpacked/resources/command-proposal-runner/trafficvisson-command-proposal-runner.exe",
    ),
  );
  assert.ok(
    artifacts.includes(
      "win-unpacked/resources/command-proposal-runner/runner-manifest.json",
    ),
  );
  assert.ok(
    artifacts.includes(
      "win-unpacked/resources/command-proposal-runner/model-pack/manifest.json",
    ),
  );
  assert.ok(
    artifacts.includes(
      "win-unpacked/resources/command-proposal-runner/model-pack/onnx/model_q4.onnx",
    ),
  );
});

test("packaged runner integrity accepts a complete pinned trust chain", async (t) => {
  const fixture = await createRunnerFixture();
  t.after(() => fs.rm(fixture.root, { recursive: true, force: true }));

  const verified = await verifyBundledRunnerIntegrity(fixture.root);

  assert.equal(verified.runner.runner_id, "trafficvisson.test-runner");
  assert.equal(verified.model_pack.pack_id, "trafficvisson-test-pack");
  assert.equal(verified.model_pack.selected_artifact, "onnx/model_q4.onnx");
  assert.equal(verified.model_pack.review_only, true);
  assert.equal(verified.model_pack.automatic_labeling_allowed, false);
});

test("packaged runner integrity rejects a modified ONNX artifact", async (t) => {
  const fixture = await createRunnerFixture();
  t.after(() => fs.rm(fixture.root, { recursive: true, force: true }));
  await fs.appendFile(fixture.modelPath, "tampered", "utf8");

  await assert.rejects(
    verifyBundledRunnerIntegrity(fixture.root),
    /ONNX model size does not match/,
  );
});
