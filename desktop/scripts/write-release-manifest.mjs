import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(scriptDirectory, "..");
const releaseRoot = path.join(desktopRoot, "release");
const runnerResourceRoot = path.join(
  "win-unpacked",
  "resources",
  "command-proposal-runner",
);
const runnerExecutableRelativePath = path.join(
  runnerResourceRoot,
  "trafficsupervision-command-proposal-runner.exe",
);
const runnerManifestRelativePath = path.join(
  runnerResourceRoot,
  "runner-manifest.json",
);
const modelManifestRelativePath = path.join(
  runnerResourceRoot,
  "model-pack",
  "manifest.json",
);
const selectedModelRelativePath = path.join(
  runnerResourceRoot,
  "model-pack",
  "onnx",
  "model_q4.onnx",
);

export function releaseArtifactPaths(version) {
  return [
    `TrafficSuperVision-Setup-${version}-x64.exe`,
    `TrafficSuperVision-Setup-${version}-x64.exe.blockmap`,
    path.join("win-unpacked", "TrafficSuperVision.exe"),
    path.join("win-unpacked", "resources", "app.asar"),
    path.join(
      "win-unpacked",
      "resources",
      "bridge",
      "TrafficSuperVisionBridge.exe",
    ),
    path.join("win-unpacked", "resources", "bridge", "ffmpeg.exe"),
    path.join("win-unpacked", "resources", "bridge", "ffprobe.exe"),
    runnerExecutableRelativePath,
    runnerManifestRelativePath,
    modelManifestRelativePath,
    selectedModelRelativePath,
  ];
}

export async function sha256(filePath) {
  const hash = createHash("sha256");
  const handle = await fs.open(filePath, "r");
  try {
    for await (const chunk of handle.createReadStream()) hash.update(chunk);
  } finally {
    await handle.close();
  }
  return hash.digest("hex");
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
  if (typeof value !== "string" || !/^[a-f0-9]{64}$/.test(value)) {
    throw new Error(`${label} must be a lowercase SHA-256 digest`);
  }
  return value;
}

function requireHttpsUrl(value, label) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(`${label} is invalid`);
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error(`${label} is invalid`);
  }
  return value;
}

function requireModelPackMetadata(modelManifest) {
  if (
    modelManifest.schema_version !==
    "trafficvisson.command-proposal-model-pack.v2"
  ) {
    throw new Error("model-pack manifest schema is unsupported");
  }
  const model = modelManifest.model;
  if (
    !model ||
    typeof model !== "object" ||
    typeof model.name !== "string" ||
    model.name.length < 1 ||
    model.name.length > 128 ||
    typeof model.version !== "string" ||
    !/^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$/.test(model.version) ||
    model.task !== "zero_shot_object_detection" ||
    model.format !== "onnx" ||
    model.selected_artifact !== "onnx/model_q4.onnx"
  ) {
    throw new Error("model-pack model identity is incomplete");
  }

  const source = modelManifest.source;
  if (
    !source ||
    typeof source !== "object" ||
    typeof source.repo_id !== "string" ||
    typeof source.resolved_revision !== "string" ||
    !/^[a-f0-9]{40,64}$/.test(source.resolved_revision) ||
    source.selected_model !== model.selected_artifact
  ) {
    throw new Error("model-pack source provenance is incomplete");
  }
  requireHttpsUrl(source.repository_url, "model-pack source URL");

  const license = modelManifest.license;
  if (
    !license ||
    typeof license !== "object" ||
    !/^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$/.test(
      license.weights_spdx ?? "",
    ) ||
    !/^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$/.test(
      license.runtime_spdx ?? "",
    ) ||
    license.commercial_use_confirmed !== true ||
    typeof license.redistribution_allowed !== "boolean" ||
    license.notice_artifact !== "THIRD-PARTY-NOTICES.md"
  ) {
    throw new Error("model-pack license metadata is incomplete");
  }
  requireHttpsUrl(license.license_url, "model-pack license URL");

  const gpu = modelManifest.gpu_requirements;
  if (
    !gpu ||
    typeof gpu !== "object" ||
    typeof gpu.required !== "boolean" ||
    typeof gpu.cpu_fallback !== "boolean" ||
    !Number.isSafeInteger(gpu.recommended_min_vram_mb) ||
    gpu.recommended_min_vram_mb < 0 ||
    gpu.recommended_min_vram_mb > 131072 ||
    !(
      gpu.cuda_min_version === null ||
      (typeof gpu.cuda_min_version === "string" &&
        /^[0-9]{1,2}\.[0-9]{1,2}$/.test(gpu.cuda_min_version))
    ) ||
    (gpu.required && gpu.cuda_min_version === null)
  ) {
    throw new Error("model-pack GPU requirements are incomplete");
  }
  return { model, source, license, gpu };
}

export async function verifyBundledRunnerIntegrity(packagedRunnerRoot) {
  const runnerExecutablePath = path.join(
    packagedRunnerRoot,
    "trafficsupervision-command-proposal-runner.exe",
  );
  const runnerManifestPath = path.join(packagedRunnerRoot, "runner-manifest.json");
  const modelPackRoot = path.join(packagedRunnerRoot, "model-pack");
  const modelManifestPath = path.join(modelPackRoot, "manifest.json");
  const selectedModelPath = path.join(modelPackRoot, "onnx", "model_q4.onnx");

  const runnerManifest = await readJson(runnerManifestPath, "runner manifest");
  const modelManifest = await readJson(modelManifestPath, "model-pack manifest");

  if (
    runnerManifest.schema_version !==
    "trafficvisson.command-proposal-runner.v1"
  ) {
    throw new Error("runner manifest schema is unsupported");
  }
  const modelPackMetadata = requireModelPackMetadata(modelManifest);

  const actualRunnerSha256 = await sha256(runnerExecutablePath);
  const expectedRunnerSha256 = requireSha256(
    runnerManifest.executable_sha256,
    "runner executable hash",
  );
  if (actualRunnerSha256 !== expectedRunnerSha256) {
    throw new Error("packaged command-proposal runner hash does not match its manifest");
  }

  const actualModelManifestSha256 = await sha256(modelManifestPath);
  const expectedModelManifestSha256 = requireSha256(
    runnerManifest.model_manifest_sha256,
    "model-pack manifest hash",
  );
  if (actualModelManifestSha256 !== expectedModelManifestSha256) {
    throw new Error("packaged model-pack manifest hash does not match the runner manifest");
  }
  if (
    runnerManifest.model?.manifest_sha256 !== expectedModelManifestSha256
  ) {
    throw new Error("runner model identity does not match the packaged model-pack manifest");
  }
  if (
    runnerManifest.model?.pack_id !== modelManifest.pack_id ||
    runnerManifest.model?.pack_version !== modelManifest.pack_version
  ) {
    throw new Error("runner model identity does not match the packaged model-pack");
  }

  if (modelManifest.source?.selected_model !== "onnx/model_q4.onnx") {
    throw new Error("model-pack selected artifact is not the approved q4 ONNX model");
  }
  if (
    modelManifest.review_policy?.review_only !== true ||
    modelManifest.review_policy?.automatic_labeling_allowed !== false
  ) {
    throw new Error("packaged model-pack is not constrained to review-only proposals");
  }
  if (!Array.isArray(modelManifest.artifacts)) {
    throw new Error("model-pack artifact inventory is missing");
  }
  const noticeArtifact = modelManifest.artifacts.find(
    (artifact) =>
      artifact?.path === modelPackMetadata.license.notice_artifact,
  );
  if (
    !noticeArtifact ||
    !Number.isSafeInteger(noticeArtifact.size_bytes) ||
    noticeArtifact.size_bytes <= 0
  ) {
    throw new Error("model-pack license notice is not inventoried");
  }
  const selectedArtifact = modelManifest.artifacts.find(
    (artifact) => artifact?.path === "onnx/model_q4.onnx",
  );
  if (
    !selectedArtifact ||
    !Number.isSafeInteger(selectedArtifact.size_bytes) ||
    selectedArtifact.size_bytes <= 0
  ) {
    throw new Error("selected ONNX model is missing from the model-pack inventory");
  }

  const selectedModelStat = await fs.stat(selectedModelPath);
  if (selectedModelStat.size !== selectedArtifact.size_bytes) {
    throw new Error("packaged ONNX model size does not match the model-pack manifest");
  }
  const expectedModelSha256 = requireSha256(
    selectedArtifact.sha256,
    "selected ONNX model hash",
  );
  const actualModelSha256 = await sha256(selectedModelPath);
  if (actualModelSha256 !== expectedModelSha256) {
    throw new Error("packaged ONNX model hash does not match the model-pack manifest");
  }

  return {
    runner: {
      runner_id: runnerManifest.runner_id,
      runner_version: runnerManifest.runner_version,
      executable_sha256: actualRunnerSha256,
      manifest_sha256: await sha256(runnerManifestPath),
    },
    model_pack: {
      provider: runnerManifest.model?.provider,
      pack_id: modelManifest.pack_id,
      pack_version: modelManifest.pack_version,
      manifest_sha256: actualModelManifestSha256,
      selected_artifact: "onnx/model_q4.onnx",
      selected_artifact_size_bytes: selectedModelStat.size,
      selected_artifact_sha256: actualModelSha256,
      model_name: modelPackMetadata.model.name,
      model_version: modelPackMetadata.model.version,
      source_url: modelPackMetadata.source.repository_url,
      source_revision: modelPackMetadata.source.resolved_revision,
      weights_spdx: modelPackMetadata.license.weights_spdx,
      runtime_spdx: modelPackMetadata.license.runtime_spdx,
      redistribution_allowed:
        modelPackMetadata.license.redistribution_allowed,
      gpu_requirements: {
        required: modelPackMetadata.gpu.required,
        cpu_fallback: modelPackMetadata.gpu.cpu_fallback,
        recommended_min_vram_mb:
          modelPackMetadata.gpu.recommended_min_vram_mb,
        cuda_min_version: modelPackMetadata.gpu.cuda_min_version,
      },
      review_only: true,
      automatic_labeling_allowed: false,
    },
  };
}

function commandText(command, args) {
  const result = spawnSync(command, args, {
    cwd: desktopRoot,
    encoding: "utf8",
    windowsHide: true,
  });
  const output = `${result.stdout ?? ""}\n${result.stderr ?? ""}`.trim();
  return {
    exit_code: result.status,
    output: output || null,
  };
}

function signatureStatus(filePath) {
  if (path.extname(filePath).toLowerCase() !== ".exe") return "not_applicable";
  const escapedPath = filePath.replaceAll("'", "''");
  const result = commandText("pwsh.exe", [
    "-NoProfile",
    "-Command",
    `(Get-AuthenticodeSignature -LiteralPath '${escapedPath}').Status.ToString()`,
  ]);
  return result.exit_code === 0 && result.output
    ? result.output.split(/\r?\n/).at(-1)
    : "check_failed";
}

async function directoryStats(root) {
  let fileCount = 0;
  let totalBytes = 0;
  const pending = [root];
  while (pending.length > 0) {
    const current = pending.pop();
    for (const entry of await fs.readdir(current, { withFileTypes: true })) {
      const absolute = path.join(current, entry.name);
      if (entry.isDirectory()) pending.push(absolute);
      if (entry.isFile()) {
        const stat = await fs.stat(absolute);
        fileCount += 1;
        totalBytes += stat.size;
      }
    }
  }
  return { file_count: fileCount, total_bytes: totalBytes };
}

export async function writeReleaseManifest() {
  const packageJson = JSON.parse(
    await fs.readFile(path.join(desktopRoot, "package.json"), "utf8"),
  );
  const version = packageJson.version;
  const artifactPaths = releaseArtifactPaths(version);
  const packagedRunnerRoot = path.join(
    releaseRoot,
    "win-unpacked",
    "resources",
    "command-proposal-runner",
  );
  const verifiedRunner = await verifyBundledRunnerIntegrity(packagedRunnerRoot);

  const artifacts = [];
  for (const relativePath of artifactPaths) {
    const absolutePath = path.join(releaseRoot, relativePath);
    const stat = await fs.stat(absolutePath);
    artifacts.push({
      path: relativePath.replaceAll("\\", "/"),
      size_bytes: stat.size,
      sha256: await sha256(absolutePath),
      authenticode_status: signatureStatus(absolutePath),
    });
  }

  const ffmpegResult = commandText(
    path.join(
      releaseRoot,
      "win-unpacked",
      "resources",
      "bridge",
      "ffmpeg.exe",
    ),
    ["-version"],
  );
  const pythonResult = commandText("python", ["--version"]);
  const pyinstallerResult = commandText("python", [
    "-m",
    "PyInstaller",
    "--version",
  ]);
  const bridgeStats = await directoryStats(
    path.join(releaseRoot, "win-unpacked", "resources", "bridge"),
  );
  const runnerStats = await directoryStats(packagedRunnerRoot);

  const manifest = {
    schema_version: "trafficvisson-windows-release-v2",
    generated_at: new Date().toISOString(),
    product: "TrafficSuperVision",
    version,
    channel: "alpha",
    architecture: "x64",
    release_readiness: "development_only_not_distribution_ready",
    source_revision: "unavailable_not_git_repository",
    packaging: {
      electron: packageJson.devDependencies.electron,
      electron_builder: packageJson.devDependencies["electron-builder"],
      node: process.version,
      python: pythonResult.output,
      pyinstaller: pyinstallerResult.output,
      bridge_bundle: bridgeStats,
      command_proposal_runner_bundle: runnerStats,
      ffmpeg_version:
        ffmpegResult.output?.split(/\r?\n/).slice(0, 3).join("\n") ?? null,
    },
    model_runtime: {
      packaged: true,
      purpose: "review_only_open_vocabulary_question_proposals",
      runtime: "onnxruntime_bundled_executable",
      fallback: "cached_existing_or_unavailable",
      runner: verifiedRunner.runner,
      model_pack: verifiedRunner.model_pack,
      note: "The bundled model creates review questions only. It cannot approve labels, create incidents, start training, or promote a model.",
    },
    artifacts,
    unresolved_release_gates: [
      "Authenticode signing and timestamping",
      "clean Windows installation, upgrade, rollback, and uninstall QA",
      "supported Python and OpenSSL build environment",
      "reproducible locked Python build dependencies",
      "FFmpeg GPL redistribution review and required notices/source offer",
      "product license, EULA, third-party notices, and SBOM",
      "validated production road-defect, OCR, and maintenance model packs with representative field evaluation",
      "Hancom Office and security-module clean-PC validation",
    ],
  };

  const manifestPath = path.join(releaseRoot, "release-manifest.json");
  await fs.writeFile(
    manifestPath,
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );

  const checksumLines = [
    ...artifacts.map((artifact) => `${artifact.sha256} *${artifact.path}`),
    `${await sha256(manifestPath)} *release-manifest.json`,
  ];
  const checksumPath = path.join(releaseRoot, "SHA256SUMS.txt");
  await fs.writeFile(checksumPath, `${checksumLines.join("\n")}\n`, "utf8");

  return {
    manifest: manifestPath,
    checksum_file: checksumPath,
    artifacts: artifacts.length,
    release_readiness: manifest.release_readiness,
  };
}

const isDirectExecution =
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));

if (isDirectExecution) {
  console.log(JSON.stringify(await writeReleaseManifest()));
}
