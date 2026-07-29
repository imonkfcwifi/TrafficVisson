#!/usr/bin/env node
import { copyFileSync, existsSync, mkdirSync, realpathSync, rmSync, statSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const buildRoot = path.join(desktopRoot, "build");
const distributionRoot = path.join(buildRoot, "bridge");
const workRoot = path.join(buildRoot, "pyinstaller-work");
const expectedBundle = path.join(distributionRoot, "TrafficSuperVisionBridge");

function assertChildPath(candidate, parent) {
  const relative = path.relative(parent, candidate);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`Refusing to modify a build path outside ${parent}`);
  }
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: desktopRoot,
    encoding: "utf8",
    stdio: "inherit",
    windowsHide: true,
    shell: false,
    ...options,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} exited with status ${result.status}`);
  }
}

function findExecutable(name) {
  const result = spawnSync("where.exe", [name], {
    cwd: desktopRoot,
    encoding: "utf8",
    windowsHide: true,
    shell: false,
  });
  if (result.status !== 0) {
    throw new Error(`${name} is required to build the offline desktop package`);
  }
  const candidates = result.stdout
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean);
  for (const candidate of candidates) {
    if (!path.isAbsolute(candidate) || !existsSync(candidate)) continue;
    // Scoop's small *.exe shim depends on a user-specific *.shim sidecar and
    // is not a redistributable FFmpeg binary. Prefer the next `where.exe`
    // candidate (for example a WinGet symbolic link to a static build).
    const shimSidecar = path.join(
      path.dirname(candidate),
      `${path.basename(candidate, path.extname(candidate))}.shim`,
    );
    if (existsSync(shimSidecar)) continue;
    const resolved = realpathSync(candidate);
    if (
      path.basename(resolved).toLowerCase() !== name.toLowerCase() ||
      !statSync(resolved).isFile()
    ) {
      continue;
    }
    return resolved;
  }
  throw new Error(
    `${name} could not be resolved to a self-contained executable; shell shims are not packaged`,
  );
}

function verifyBundledMediaTool(toolPath, expectedPrefix) {
  const safeEnvironment = {
    PATH: expectedBundle,
    SYSTEMROOT: process.env.SYSTEMROOT || "C:\\Windows",
    WINDIR: process.env.WINDIR || "C:\\Windows",
  };
  const result = spawnSync(toolPath, ["-hide_banner", "-version"], {
    cwd: expectedBundle,
    env: safeEnvironment,
    encoding: "utf8",
    windowsHide: true,
    shell: false,
    timeout: 15_000,
  });
  const output = `${result.stdout || ""}\n${result.stderr || ""}`.trim();
  if (
    result.error ||
    result.status !== 0 ||
    !output.toLowerCase().startsWith(`${expectedPrefix.toLowerCase()} version `)
  ) {
    throw new Error(`${expectedPrefix} failed its isolated bundled-tool smoke test`);
  }
}

function verifyBundledFfmpegEncoder(ffmpegPath, encoderName) {
  const safeEnvironment = {
    PATH: expectedBundle,
    SYSTEMROOT: process.env.SYSTEMROOT || "C:\\Windows",
    WINDIR: process.env.WINDIR || "C:\\Windows",
  };
  const result = spawnSync(ffmpegPath, ["-hide_banner", "-encoders"], {
    cwd: expectedBundle,
    env: safeEnvironment,
    encoding: "utf8",
    windowsHide: true,
    shell: false,
    timeout: 15_000,
    maxBuffer: 2 * 1024 * 1024,
  });
  const output = `${result.stdout || ""}\n${result.stderr || ""}`;
  const encoderPattern = new RegExp(
    `^\\s*V\\S*\\s+${encoderName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s`,
    "m",
  );
  if (result.error || result.status !== 0 || !encoderPattern.test(output)) {
    throw new Error(
      `Bundled ffmpeg does not expose required ${encoderName} video encoding`,
    );
  }
}

mkdirSync(buildRoot, { recursive: true });
for (const target of [distributionRoot, workRoot]) {
  assertChildPath(target, desktopRoot);
  rmSync(target, { recursive: true, force: true });
}
mkdirSync(distributionRoot, { recursive: true });
mkdirSync(workRoot, { recursive: true });

// The developer workstation also contains unrelated game, notebook, Qt and ML
// stacks.  The default desktop bridge is intentionally the minimal,
// no-model-pack flavor: road video evidence extraction uses FFmpeg, while a
// configured optional vision pack fails closed until its selected runtime is
// explicitly added to a separate reviewed build flavor.  Excluding ambient
// packages makes the frozen result reproducible and prevents accidental AGPL
// or multi-Qt collection from the machine-wide Python installation.
const excludedAmbientModules = [
  "IPython",
  "PIL",
  "PyQt5",
  "PyQt6",
  "PySide2",
  "PySide6",
  "_pytest",
  "cv2",
  "easyocr",
  "jedi",
  "kivy",
  "kivymd",
  "matplotlib",
  "numpy",
  "onnxruntime",
  "pandas",
  "parso",
  "pygame",
  "pygame_gui",
  "pytest",
  "torch",
  "torchvision",
  "transformers",
  "ultralytics",
];

const pyinstallerArguments = [
  "-m",
  "PyInstaller",
  "--noconfirm",
  "--clean",
  "--onedir",
  "--name",
  "TrafficSuperVisionBridge",
  "--distpath",
  distributionRoot,
  "--workpath",
  workRoot,
  "--specpath",
  workRoot,
  "--hidden-import",
  "win32com.client",
  "--hidden-import",
  "pythoncom",
  "--hidden-import",
  "pywintypes",
  "--hidden-import",
  "win32api",
  "--hidden-import",
  "win32con",
  "--hidden-import",
  "win32job",
  "--hidden-import",
  "app.vision_worker",
  path.join(desktopRoot, "bridge", "run_bridge.py"),
];
for (const moduleName of excludedAmbientModules) {
  pyinstallerArguments.splice(
    pyinstallerArguments.length - 1,
    0,
    "--exclude-module",
    moduleName,
  );
}
run("python", pyinstallerArguments);

const bridgeExecutable = path.join(expectedBundle, "TrafficSuperVisionBridge.exe");
if (!existsSync(bridgeExecutable)) {
  throw new Error("PyInstaller did not create TrafficSuperVisionBridge.exe");
}

for (const tool of ["ffmpeg.exe", "ffprobe.exe"]) {
  const bundledPath = path.join(expectedBundle, tool);
  copyFileSync(findExecutable(tool), bundledPath);
  verifyBundledMediaTool(
    bundledPath,
    path.basename(tool, path.extname(tool)),
  );
}
verifyBundledFfmpegEncoder(
  path.join(expectedBundle, "ffmpeg.exe"),
  "libx264",
);

console.log(`Prepared frozen bridge: ${expectedBundle}`);
