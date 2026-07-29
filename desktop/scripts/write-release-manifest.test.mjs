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
    "trafficsupervision-command-proposal-runner.exe",
  );
  const modelPath = path.join(modelDirectory, "model_q4.onnx");
  const noticePath = path.join(modelPackRoot, "THIRD-PARTY-NOTICES.md");
  await fs.writeFile(executablePath, "fixed runner fixture", "utf8");
  await fs.writeFile(modelPath, "fixed model fixture", "utf8");
  await fs.writeFile(noticePath, "fixed license notice", "utf8");

  const modelStat = await fs.stat(modelPath);
  const noticeStat = await fs.stat(noticePath);
  const modelManifestPath = path.join(modelPackRoot, "manifest.json");
  const modelManifest = {
    schema_version: "trafficvisson.command-proposal-model-pack.v2",
    pack_id: "trafficvisson-test-pack",
    pack_version: "0.0.1",
    model: {
      name: "Grounding DINO fixture",
      version: "0123456789abcdef0123456789abcdef01234567",
      task: "zero_shot_object_detection",
      format: "onnx",
      selected_artifact: "onnx/model_q4.onnx",
    },
    source: {
      repository_url: "https://models.example.test/grounding-dino",
      repo_id: "example/grounding-dino",
      resolved_revision: "0123456789abcdef0123456789abcdef01234567",
      selected_model: "onnx/model_q4.onnx",
    },
    license: {
      weights_spdx: "Apache-2.0",
      runtime_spdx: "MIT",
      license_url: "https://www.apache.org/licenses/LICENSE-2.0",
      commercial_use_confirmed: true,
      redistribution_allowed: false,
      notice_artifact: "THIRD-PARTY-NOTICES.md",
    },
    gpu_requirements: {
      required: false,
      cpu_fallback: true,
      recommended_min_vram_mb: 4096,
      cuda_min_version: null,
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
      {
        path: "THIRD-PARTY-NOTICES.md",
        size_bytes: noticeStat.size,
        sha256: await sha256(noticePath),
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

  return { root, modelPath, modelManifestPath };
}

test("release artifact inventory includes the runner trust chain", () => {
  const artifacts = releaseArtifactPaths("9.8.7-test").map((entry) =>
    entry.replaceAll("\\", "/"),
  );

  assert.equal(artifacts.length, 11);
  assert.equal(new Set(artifacts).size, artifacts.length);
  assert.ok(
    artifacts.includes(
      "win-unpacked/resources/command-proposal-runner/trafficsupervision-command-proposal-runner.exe",
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
  assert.equal(verified.model_pack.model_name, "Grounding DINO fixture");
  assert.equal(
    verified.model_pack.source_revision,
    "0123456789abcdef0123456789abcdef01234567",
  );
  assert.equal(verified.model_pack.weights_spdx, "Apache-2.0");
  assert.deepEqual(verified.model_pack.gpu_requirements, {
    required: false,
    cpu_fallback: true,
    recommended_min_vram_mb: 4096,
    cuda_min_version: null,
  });
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

test("packaged runner integrity rejects incomplete model provenance", async (t) => {
  const fixture = await createRunnerFixture();
  t.after(() => fs.rm(fixture.root, { recursive: true, force: true }));
  const manifest = JSON.parse(
    await fs.readFile(fixture.modelManifestPath, "utf8"),
  );
  delete manifest.gpu_requirements;
  await fs.writeFile(
    fixture.modelManifestPath,
    JSON.stringify(manifest),
    "utf8",
  );

  await assert.rejects(
    verifyBundledRunnerIntegrity(fixture.root),
    /GPU requirements are incomplete/,
  );
});
