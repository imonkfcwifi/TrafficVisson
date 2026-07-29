import { EventEmitter } from "node:events";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { BridgeClient } from "./bridge-client.mjs";

const READY_PREFIX = "TRAFFICSUPERVISION_BRIDGE_READY ";
const MAX_HANDSHAKE_BYTES = 16 * 1024;
const MAX_LOG_LINES = 200;
const SAFE_ENVIRONMENT_KEYS = new Set([
  "APPDATA",
  "COMSPEC",
  "LANG",
  "LC_ALL",
  "LOCALAPPDATA",
  "NUMBER_OF_PROCESSORS",
  "PATH",
  "PATHEXT",
  "PROGRAMDATA",
  "PROGRAMFILES",
  "PROGRAMFILES(X86)",
  "SYSTEMDRIVE",
  "SYSTEMROOT",
  "TEMP",
  "TRAFFICSUPERVISION_VISION_MODEL_PACK",
  "TMP",
  "TMPDIR",
  "WINDIR",
]);

export function parseReadyLine(line) {
  if (typeof line !== "string" || !line.startsWith(READY_PREFIX)) return null;
  let value;
  try {
    value = JSON.parse(line.slice(READY_PREFIX.length));
  } catch {
    throw new Error("Bridge readiness payload was malformed");
  }
  if (
    !value ||
    value.host !== "127.0.0.1" ||
    !Number.isInteger(value.port) ||
    value.port < 1 ||
    value.port > 65_535 ||
    value.protocol !== "http"
  ) {
    throw new Error("Bridge readiness payload failed validation");
  }
  return Object.freeze({ host: value.host, port: value.port, protocol: value.protocol });
}

export function resolvePythonExecutable({ isPackaged = false, resourcesPath, configured } = {}) {
  if (isPackaged) {
    if (!resourcesPath) throw new Error("resourcesPath is required for packaged bridge startup");
    const embedded = path.join(resourcesPath, "python", "python.exe");
    if (!existsSync(embedded)) throw new Error("Embedded Python runtime is missing");
    return embedded;
  }
  return configured || process.env.TRAFFICSUPERVISION_PYTHON || (process.platform === "win32" ? "python" : "python3");
}

export function resolveFrozenBridgeExecutable({ resourcesPath } = {}) {
  if (!resourcesPath) {
    throw new Error("resourcesPath is required for the packaged bridge");
  }
  const executable = path.join(
    resourcesPath,
    "bridge",
    process.platform === "win32" ? "TrafficSuperVisionBridge.exe" : "TrafficSuperVisionBridge",
  );
  if (!existsSync(executable)) {
    throw new Error("Packaged bridge executable is missing");
  }
  return executable;
}

export function buildBridgeEnvironment(baseEnvironment, token) {
  const safeEnvironment = {};
  for (const [key, value] of Object.entries(baseEnvironment ?? {})) {
    if (SAFE_ENVIRONMENT_KEYS.has(key.toUpperCase()) && typeof value === "string") {
      safeEnvironment[key] = value;
    }
  }
  safeEnvironment.PYTHONDONTWRITEBYTECODE = "1";
  safeEnvironment.PYTHONUNBUFFERED = "1";
  safeEnvironment.TRAFFICSUPERVISION_BRIDGE_TOKEN = token;
  return safeEnvironment;
}

export class BridgeSupervisor extends EventEmitter {
  #bridgeRoot;
  #commandProposalRunnerRoot;
  #pythonExecutable;
  #frozenBridgeExecutable;
  #electronVersion;
  #spawn;
  #fetch;
  #startupTimeoutMs;
  #child = null;
  #client = null;
  #token = null;
  #state = "stopped";
  #logs = [];
  #startedAt = null;

  constructor({
    bridgeRoot,
    commandProposalRunnerRoot,
    pythonExecutable,
    frozenBridgeExecutable,
    electronVersion,
    spawnImpl = spawn,
    fetchImpl = globalThis.fetch,
    startupTimeoutMs = 15_000,
  }) {
    super();
    this.#bridgeRoot = path.resolve(bridgeRoot);
    this.#commandProposalRunnerRoot = commandProposalRunnerRoot
      ? path.resolve(commandProposalRunnerRoot)
      : null;
    this.#pythonExecutable = pythonExecutable;
    this.#frozenBridgeExecutable = frozenBridgeExecutable;
    this.#electronVersion =
      typeof electronVersion === "string" &&
      /^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$/.test(electronVersion)
        ? electronVersion
        : null;
    this.#spawn = spawnImpl;
    this.#fetch = fetchImpl;
    this.#startupTimeoutMs = startupTimeoutMs;
  }

  publicStatus() {
    return Object.freeze({
      state: this.#state,
      startedAt: this.#startedAt,
    });
  }

  async start() {
    if (this.#state === "ready") return this.publicStatus();
    if (this.#state === "starting") throw new Error("Bridge is already starting");
    this.#state = "starting";
    this.#token = randomBytes(32).toString("base64url");
    const launcherPath = path.join(this.#bridgeRoot, "run_bridge.py");
    if (!this.#frozenBridgeExecutable && !existsSync(launcherPath)) {
      this.#state = "failed";
      throw new Error("Bridge launcher is missing");
    }
    if (
      this.#frozenBridgeExecutable &&
      (!path.isAbsolute(this.#frozenBridgeExecutable) ||
        !existsSync(this.#frozenBridgeExecutable))
    ) {
      this.#state = "failed";
      throw new Error("Packaged bridge executable is missing");
    }
    const command = this.#frozenBridgeExecutable || this.#pythonExecutable;
    const args = this.#frozenBridgeExecutable ? [] : ["-I", "-B", "-u", launcherPath];
    const childEnvironment = buildBridgeEnvironment(process.env, this.#token);
    if (this.#electronVersion) {
      childEnvironment.TRAFFICSUPERVISION_ELECTRON_VERSION =
        this.#electronVersion;
    }
    if (this.#commandProposalRunnerRoot) {
      if (
        !path.isAbsolute(this.#commandProposalRunnerRoot) ||
        !existsSync(this.#commandProposalRunnerRoot)
      ) {
        this.#state = "failed";
        throw new Error("Packaged command proposal runner is missing");
      }
      childEnvironment.TRAFFICSUPERVISION_COMMAND_PROPOSAL_RUNNER_ROOT =
        this.#commandProposalRunnerRoot;
    }
    if (this.#frozenBridgeExecutable) {
      childEnvironment.PATH = [
        this.#bridgeRoot,
        childEnvironment.PATH,
      ].filter(Boolean).join(path.delimiter);
    }

    const child = this.#spawn(command, args, {
      cwd: this.#bridgeRoot,
      env: childEnvironment,
      shell: false,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.#child = child;

    try {
      const ready = await this.#waitForReady(child);
      this.#client = new BridgeClient({
        baseUrl: `${ready.protocol}://${ready.host}:${ready.port}`,
        token: this.#token,
        fetchImpl: this.#fetch,
      });
      await this.#client.request("GET", "/health");
      this.#state = "ready";
      this.#startedAt = new Date().toISOString();
      this.emit("ready", this.publicStatus());
      return this.publicStatus();
    } catch (error) {
      this.#state = "failed";
      this.#client = null;
      this.#terminateChild();
      throw error;
    }
  }

  async request(method, pathname, options) {
    if (this.#state !== "ready" || !this.#client) {
      throw new Error("Bridge is not ready");
    }
    return this.#client.request(method, pathname, options);
  }

  async requestBinary(pathname, options) {
    if (this.#state !== "ready" || !this.#client) {
      throw new Error("Bridge is not ready");
    }
    return this.#client.requestBinary(pathname, options);
  }

  async restart() {
    await this.stop();
    return this.start();
  }

  async stop() {
    if (!this.#child) {
      this.#state = "stopped";
      this.#client = null;
      this.#token = null;
      return;
    }
    const child = this.#child;
    this.#state = "stopping";
    if (child.exitCode !== null || child.signalCode !== null) {
      this.#child = null;
      this.#client = null;
      this.#token = null;
      this.#state = "stopped";
      this.#startedAt = null;
      return;
    }
    await new Promise((resolve) => {
      const timeout = setTimeout(() => {
        if (!child.killed) child.kill("SIGKILL");
        resolve();
      }, 3_000);
      child.once("exit", () => {
        clearTimeout(timeout);
        resolve();
      });
      if (!child.killed) child.kill();
    });
    this.#child = null;
    this.#client = null;
    this.#token = null;
    this.#state = "stopped";
    this.#startedAt = null;
  }

  #recordLog(level, line) {
    const message = String(line)
      .replaceAll(this.#token ?? "__never__", "[REDACTED]")
      .slice(0, 2_000);
    if (!message) return;
    this.#logs.push({ level, message, time: new Date().toISOString() });
    if (this.#logs.length > MAX_LOG_LINES) this.#logs.shift();
    this.emit("log", { level, message });
  }

  #waitForReady(child) {
    return new Promise((resolve, reject) => {
      let buffer = "";
      let settled = false;
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        callback(value);
      };
      const timer = setTimeout(
        () => finish(reject, new Error("Bridge startup timed out")),
        this.#startupTimeoutMs,
      );
      child.once("error", (error) => finish(reject, error));
      child.once("exit", (code, signal) => {
        if (!settled) {
          finish(reject, new Error(`Bridge exited before readiness (${code ?? signal ?? "unknown"})`));
        } else if (this.#state === "ready") {
          this.#state = "failed";
          this.#client = null;
          this.emit("exit", { code, signal });
        }
      });
      child.stderr?.setEncoding("utf8");
      child.stderr?.on("data", (chunk) => {
        for (const line of String(chunk).split(/\r?\n/)) this.#recordLog("stderr", line);
      });
      child.stdout?.setEncoding("utf8");
      child.stdout?.on("data", (chunk) => {
        buffer += String(chunk);
        if (Buffer.byteLength(buffer, "utf8") > MAX_HANDSHAKE_BYTES) {
          finish(reject, new Error("Bridge readiness output exceeded the limit"));
          return;
        }
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          const ready = parseReadyLine(line);
          if (ready) {
            finish(resolve, ready);
            return;
          }
          this.#recordLog("stdout", line);
        }
      });
    });
  }

  #terminateChild() {
    if (this.#child && !this.#child.killed) this.#child.kill();
    this.#child = null;
  }
}
