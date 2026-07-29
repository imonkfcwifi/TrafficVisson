#!/usr/bin/env node
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const buildRoot = path.join(desktopRoot, "build");
const finalRoot = path.join(buildRoot, "command-proposal-runner");
const workRoot = path.join(buildRoot, "command-proposal-runner-work");
const distRoot = path.join(buildRoot, "command-proposal-runner-dist");
const sourceRoot = path.join(desktopRoot, "vision-runner");
const sourceModelRoot = path.resolve(
  process.env.TRAFFICVISSON_ONNX_SOURCE_PACK ||
    "C:\\ProgramData\\TrafficVisson\\ModelPacks\\trafficvisson-onnx-grounding-dino-tiny-0.1.0",
);
const runnerName = "trafficvisson-command-proposal-runner.exe";
const expectedSourceHashes = new Map([
  ["config.json", "f4b79138d4a83efb56ebad85c194a5a99a57102204c9829f76c0cd109acb0aeb"],
  [
    "preprocessor_config.json",
    "c96f8ed67114865efb85b544ab865db5a5c67a4e022ca0d8c2185e463a992cb4",
  ],
  ["tokenizer.json", "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"],
  [
    "tokenizer_config.json",
    "5723ed14feabd5c974010123a7c93005116bdc5e3d69bcce3d9334987241f53c",
  ],
  [
    "special_tokens_map.json",
    "5d5b662e421ea9fac075174bb0688ee0d9431699900b90662acd44b2a350503a",
  ],
  ["vocab.txt", "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3"],
  [
    "onnx/model_q4.onnx",
    "2ed98a91b75f00d325c7581bee83b92b5b98460981772edf5ed844370ac25c11",
  ],
]);

function assertChild(candidate, parent) {
  const relative = path.relative(parent, candidate);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`Unsafe build path: ${candidate}`);
  }
}

function sha256(filePath) {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

function canonicalJson(value) {
  return Buffer.from(JSON.stringify(value), "utf8");
}

function run(command, args) {
  const completed = spawnSync(command, args, {
    cwd: desktopRoot,
    encoding: "utf8",
    stdio: "inherit",
    shell: false,
    windowsHide: true,
  });
  if (completed.error) throw completed.error;
  if (completed.status !== 0) {
    throw new Error(`${command} exited with status ${completed.status}`);
  }
}

for (const required of [
  path.join(sourceRoot, "command_proposal_runner.py"),
  path.join(sourceRoot, "THIRD-PARTY-NOTICES.md"),
]) {
  if (!existsSync(required) || !statSync(required).isFile()) {
    throw new Error(`Missing runner source: ${required}`);
  }
}
if (!existsSync(sourceModelRoot) || !statSync(sourceModelRoot).isDirectory()) {
  throw new Error(`Missing reviewed ONNX source pack: ${sourceModelRoot}`);
}
for (const [relative, expectedHash] of expectedSourceHashes) {
  const source = path.join(sourceModelRoot, ...relative.split("/"));
  if (
    !existsSync(source) ||
    !statSync(source).isFile() ||
    sha256(source) !== expectedHash
  ) {
    throw new Error(`ONNX source artifact failed its pinned hash: ${relative}`);
  }
}

mkdirSync(buildRoot, { recursive: true });
for (const target of [finalRoot, workRoot, distRoot]) {
  assertChild(target, buildRoot);
  rmSync(target, { recursive: true, force: true });
}
mkdirSync(finalRoot, { recursive: true });
mkdirSync(workRoot, { recursive: true });
mkdirSync(distRoot, { recursive: true });

const excludedModules = [
  "IPython",
  "PyQt5",
  "PyQt6",
  "PySide2",
  "PySide6",
  "_pytest",
  "cv2",
  "easyocr",
  "flax",
  "jax",
  "matplotlib",
  "pandas",
  "pytest",
  "tensorflow",
  "torch",
  "torchvision",
  "transformers",
  "ultralytics",
];
const pyinstallerArgs = [
  "-m",
  "PyInstaller",
  "--noconfirm",
  "--clean",
  "--onefile",
  "--name",
  "trafficvisson-command-proposal-runner",
  "--distpath",
  distRoot,
  "--workpath",
  workRoot,
  "--specpath",
  workRoot,
  "--hidden-import",
  "onnxruntime.capi._pybind_state",
  "--hidden-import",
  "tokenizers",
  "--hidden-import",
  "numpy",
  "--hidden-import",
  "numpy._core",
  "--hidden-import",
  "PIL.Image",
];
for (const moduleName of excludedModules) {
  pyinstallerArgs.push("--exclude-module", moduleName);
}
pyinstallerArgs.push(path.join(sourceRoot, "command_proposal_runner.py"));
run("python", pyinstallerArgs);

const builtExecutable = path.join(distRoot, runnerName);
if (!existsSync(builtExecutable) || !statSync(builtExecutable).isFile()) {
  throw new Error("PyInstaller did not produce the command proposal runner");
}
copyFileSync(builtExecutable, path.join(finalRoot, runnerName));

const modelRoot = path.join(finalRoot, "model-pack");
mkdirSync(path.join(modelRoot, "onnx"), { recursive: true });
for (const relative of expectedSourceHashes.keys()) {
  const source = path.join(sourceModelRoot, ...relative.split("/"));
  const target = path.join(modelRoot, ...relative.split("/"));
  mkdirSync(path.dirname(target), { recursive: true });
  copyFileSync(source, target);
}
copyFileSync(
  path.join(sourceRoot, "THIRD-PARTY-NOTICES.md"),
  path.join(modelRoot, "THIRD-PARTY-NOTICES.md"),
);
const apacheLicense = path.join(
  desktopRoot,
  "node_modules",
  "node-gyp",
  "gyp",
  "pylib",
  "packaging",
  "LICENSE.APACHE",
);
if (!existsSync(apacheLicense)) {
  throw new Error("A reviewed Apache-2.0 license text is unavailable");
}
copyFileSync(apacheLicense, path.join(modelRoot, "LICENSE-APACHE-2.0.txt"));

const artifactRelatives = [
  ...expectedSourceHashes.keys(),
  "THIRD-PARTY-NOTICES.md",
  "LICENSE-APACHE-2.0.txt",
].sort();
const artifacts = artifactRelatives.map((relative) => {
  const filePath = path.join(modelRoot, ...relative.split("/"));
  return {
    path: relative,
    size_bytes: statSync(filePath).size,
    sha256: sha256(filePath),
  };
});
const modelManifest = {
  schema_version: "trafficvisson.command-proposal-model-pack.v1",
  pack_id: "trafficvisson-onnx-grounding-dino-tiny",
  pack_version: "0.2.0-review",
  source: {
    repo_id: "onnx-community/grounding-dino-tiny-ONNX",
    resolved_revision: "11778337bb87ff84af72a64928c86616d86c495e",
    selected_model: "onnx/model_q4.onnx",
    license_spdx: "Apache-2.0",
  },
  inference: {
    task: "zero_shot_object_detection",
    image_width: 800,
    image_height: 800,
    box_threshold: 0.25,
    text_threshold: 0.25,
    maximum_predictions_per_frame: 32,
    score_semantics: "model_score_not_probability",
  },
  review_policy: {
    review_only: true,
    automatic_labeling_allowed: false,
    incident_creation_allowed: false,
    model_promotion_allowed: false,
    runner_sidecar_write_allowed: false,
    bridge_review_proposal_import_allowed: true,
  },
  artifacts,
};
const modelManifestBytes = canonicalJson(modelManifest);
writeFileSync(path.join(modelRoot, "manifest.json"), modelManifestBytes);
const modelManifestSha256 = createHash("sha256")
  .update(modelManifestBytes)
  .digest("hex");
const executablePath = path.join(finalRoot, runnerName);
const runnerManifest = {
  schema_version: "trafficvisson.command-proposal-runner.v1",
  runner_id: "trafficvisson.command-proposal.onnx-grounding-dino",
  runner_version: "0.1.0",
  protocol_version: "trafficvisson.command-proposal-request.v1",
  executable_sha256: sha256(executablePath),
  model_manifest_sha256: modelManifestSha256,
  model: {
    provider: "transformers_grounding_dino",
    pack_id: "trafficvisson-onnx-grounding-dino-tiny",
    pack_version: "0.2.0-review",
    manifest_sha256: modelManifestSha256,
  },
  evaluation: {
    evaluation_id: "bundled-q4-review-v1",
    producer: "trafficvisson_onnx_runner",
    producer_version: "0.1.0",
  },
};
const runnerManifestBytes = canonicalJson(runnerManifest);
writeFileSync(path.join(finalRoot, "runner-manifest.json"), runnerManifestBytes);
const runnerManifestSha256 = createHash("sha256")
  .update(runnerManifestBytes)
  .digest("hex");
writeFileSync(
  path.join(buildRoot, "command-proposal-runner.sha256"),
  `${runnerManifestSha256}\n`,
);

for (const [relative, expectedHash] of expectedSourceHashes) {
  const copied = path.join(modelRoot, ...relative.split("/"));
  if (sha256(copied) !== expectedHash) {
    throw new Error(`Copied ONNX artifact changed: ${relative}`);
  }
}
if (realpathSync(finalRoot) !== path.resolve(finalRoot)) {
  throw new Error("Runner build root is not canonical");
}
console.log(`Prepared command proposal runner: ${finalRoot}`);
console.log(`Pinned runner manifest SHA-256: ${runnerManifestSha256}`);
