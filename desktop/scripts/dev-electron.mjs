import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const currentDirectory = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(currentDirectory, "..");
const require = createRequire(import.meta.url);
const electronExecutable = require("electron");
const vitePackageRoot = path.dirname(require.resolve("vite/package.json"));
const viteCli = path.join(vitePackageRoot, "bin", "vite.js");

const portText = process.env.TRAFFICVISSON_DEV_PORT || "5173";
const port = Number(portText);
if (!Number.isInteger(port) || port < 1024 || port > 65_535) {
  throw new Error("TRAFFICVISSON_DEV_PORT must be an integer between 1024 and 65535");
}

const rendererUrl = `http://127.0.0.1:${port}`;
const children = new Set();
let stopping = false;

function spawnChild(command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: desktopRoot,
    stdio: "inherit",
    windowsHide: true,
    shell: false,
    ...options,
  });
  children.add(child);
  child.once("exit", () => children.delete(child));
  return child;
}

async function waitForRenderer(child, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Vite exited before becoming ready (code ${child.exitCode})`);
    }
    try {
      const response = await fetch(rendererUrl, {
        redirect: "error",
        signal: AbortSignal.timeout(1_000),
      });
      if (response.ok) return;
    } catch {
      // Vite has not started listening yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`Vite did not become ready at ${rendererUrl} within ${timeoutMs}ms`);
}

function stopChild(child) {
  if (!child || child.exitCode !== null || child.killed) return;
  child.kill();
}

function stopAll(exitCode = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children) stopChild(child);
  setTimeout(() => process.exit(exitCode), 1_000);
}

const vite = spawnChild(process.execPath, [
  viteCli,
  "--host",
  "127.0.0.1",
  "--port",
  String(port),
  "--strictPort",
]);

try {
  await waitForRenderer(vite);
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  stopAll(1);
  await new Promise(() => {});
}

const electron = spawnChild(electronExecutable, [path.join("electron", "main.mjs")], {
  env: {
    ...process.env,
    TRAFFICVISSON_RENDERER_URL: rendererUrl,
  },
});

electron.once("exit", (code) => stopAll(code ?? 1));
vite.once("exit", (code) => {
  if (!stopping) {
    console.error(`Vite stopped while Electron was running (code ${code ?? "unknown"})`);
    stopAll(code ?? 1);
  }
});

process.once("SIGINT", () => stopAll(130));
process.once("SIGTERM", () => stopAll(143));
process.once("uncaughtException", (error) => {
  console.error(error);
  stopAll(1);
});
process.once("unhandledRejection", (error) => {
  console.error(error);
  stopAll(1);
});
