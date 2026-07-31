import { randomUUID } from "node:crypto";
import { realpath, stat, statfs } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const VIDEO_EXTENSIONS = new Set([".mp4", ".mov"]);
const DOCUMENT_EXTENSIONS = new Set([".hwp", ".hwpx"]);
const GPX_EXTENSIONS = new Set([".gpx"]);

function safeFileName(filePath) {
  return path.basename(filePath).replace(/[\u0000-\u001f\u007f]/g, "\uFFFD");
}

export class LocalVideoSelectionRegistry {
  constructor({
    ttlMs = 4 * 60 * 60 * 1_000,
    maxSelections = 64,
    maxFiles = 100,
    now = () => Date.now(),
    idFactory = randomUUID,
  } = {}) {
    this.ttlMs = ttlMs;
    this.maxSelections = maxSelections;
    this.maxFiles = maxFiles;
    this.now = now;
    this.idFactory = idFactory;
    this.selections = new Map();
  }

  #discardExpired() {
    const currentTime = this.now();
    for (const [selectionId, selection] of this.selections) {
      if (currentTime - selection.createdAtMs >= this.ttlMs) {
        this.selections.delete(selectionId);
      }
    }
  }

  async register(filePaths) {
    if (
      !Array.isArray(filePaths) ||
      filePaths.length < 1 ||
      filePaths.length > this.maxFiles
    ) {
      throw new Error(`A selection must contain 1 to ${this.maxFiles} video files`);
    }

    const selected = [];
    const seenPaths = new Set();
    for (let index = 0; index < filePaths.length; index += 1) {
      const candidate = filePaths[index];
      if (typeof candidate !== "string" || !path.isAbsolute(candidate)) {
        throw new Error(`Selected video ${index + 1} is not an absolute local path`);
      }
      let canonicalPath;
      try {
        canonicalPath = await realpath(candidate);
      } catch {
        throw new Error(`Selected video ${index + 1} is no longer available`);
      }
      if (seenPaths.has(canonicalPath)) continue;
      const extension = path.extname(canonicalPath).toLowerCase();
      if (!VIDEO_EXTENSIONS.has(extension)) {
        throw new Error(`Selected video ${index + 1} has an unsupported extension`);
      }
      let fileStat;
      try {
        fileStat = await stat(canonicalPath);
      } catch {
        throw new Error(`Selected video ${index + 1} is no longer available`);
      }
      if (!fileStat.isFile()) {
        throw new Error(`Selected video ${index + 1} is not a regular file`);
      }
      seenPaths.add(canonicalPath);
      selected.push({
        path: canonicalPath,
        metadata: Object.freeze({
          name: safeFileName(canonicalPath),
          extension: extension.slice(1),
          sizeBytes: fileStat.size,
          lastModifiedMs: Math.trunc(fileStat.mtimeMs),
        }),
      });
    }

    if (selected.length < 1) {
      throw new Error("The selection did not contain a usable video file");
    }

    this.#discardExpired();
    while (this.selections.size >= this.maxSelections) {
      const oldestSelectionId = this.selections.keys().next().value;
      this.selections.delete(oldestSelectionId);
    }

    const selectionId = this.idFactory();
    if (!UUID_RE.test(selectionId) || this.selections.has(selectionId)) {
      throw new Error("The local selection ID generator returned an invalid ID");
    }
    this.selections.set(selectionId, {
      createdAtMs: this.now(),
      paths: Object.freeze(selected.map((file) => file.path)),
    });

    return Object.freeze({
      selectionId,
      files: Object.freeze(selected.map((file) => file.metadata)),
    });
  }

  resolvePaths(selectionId) {
    if (typeof selectionId !== "string" || !UUID_RE.test(selectionId)) {
      throw new Error("Local selection ID is invalid");
    }
    this.#discardExpired();
    const selection = this.selections.get(selectionId);
    if (!selection) {
      throw new Error("Local video selection is missing or expired");
    }
    return [...selection.paths];
  }

  resolveSinglePath(selectionId) {
    const paths = this.resolvePaths(selectionId);
    if (paths.length !== 1) {
      throw new Error("Exactly one local video must be selected");
    }
    return paths[0];
  }

  clear() {
    this.selections.clear();
  }
}

export class LocalDocumentSelectionRegistry {
  constructor({
    ttlMs = 4 * 60 * 60 * 1_000,
    maxSelections = 32,
    now = () => Date.now(),
    idFactory = randomUUID,
  } = {}) {
    this.ttlMs = ttlMs;
    this.maxSelections = maxSelections;
    this.now = now;
    this.idFactory = idFactory;
    this.selections = new Map();
  }

  #discardExpired() {
    const currentTime = this.now();
    for (const [selectionId, selection] of this.selections) {
      if (currentTime - selection.createdAtMs >= this.ttlMs) {
        this.selections.delete(selectionId);
      }
    }
  }

  async register(filePath) {
    if (typeof filePath !== "string" || !path.isAbsolute(filePath)) {
      throw new Error("Selected document is not an absolute local path");
    }
    let canonicalPath;
    try {
      canonicalPath = await realpath(filePath);
    } catch {
      throw new Error("Selected document is no longer available");
    }
    const extension = path.extname(canonicalPath).toLowerCase();
    if (!DOCUMENT_EXTENSIONS.has(extension)) {
      throw new Error("Selected document has an unsupported extension");
    }
    let documentStat;
    try {
      documentStat = await stat(canonicalPath);
    } catch {
      throw new Error("Selected document is no longer available");
    }
    if (!documentStat.isFile()) {
      throw new Error("Selected document is not a regular file");
    }

    this.#discardExpired();
    while (this.selections.size >= this.maxSelections) {
      const oldestSelectionId = this.selections.keys().next().value;
      this.selections.delete(oldestSelectionId);
    }
    const selectionId = this.idFactory();
    if (!UUID_RE.test(selectionId) || this.selections.has(selectionId)) {
      throw new Error("The document selection ID generator returned an invalid ID");
    }
    this.selections.set(selectionId, {
      createdAtMs: this.now(),
      path: canonicalPath,
    });
    return Object.freeze({
      selectionId,
      file: Object.freeze({
        name: safeFileName(canonicalPath),
        extension: extension.slice(1),
        sizeBytes: documentStat.size,
        lastModifiedMs: Math.trunc(documentStat.mtimeMs),
      }),
    });
  }

  resolvePath(selectionId) {
    if (typeof selectionId !== "string" || !UUID_RE.test(selectionId)) {
      throw new Error("Document selection ID is invalid");
    }
    this.#discardExpired();
    const selection = this.selections.get(selectionId);
    if (!selection) {
      throw new Error("Document selection is missing or expired");
    }
    return selection.path;
  }

  clear() {
    this.selections.clear();
  }
}

export class LocalGpxSelectionRegistry {
  constructor({
    ttlMs = 4 * 60 * 60 * 1_000,
    maxSelections = 32,
    maxSizeBytes = 25 * 1024 * 1024,
    now = () => Date.now(),
    idFactory = randomUUID,
  } = {}) {
    this.ttlMs = ttlMs;
    this.maxSelections = maxSelections;
    this.maxSizeBytes = maxSizeBytes;
    this.now = now;
    this.idFactory = idFactory;
    this.selections = new Map();
  }

  #discardExpired() {
    const currentTime = this.now();
    for (const [selectionId, selection] of this.selections) {
      if (currentTime - selection.createdAtMs >= this.ttlMs) {
        this.selections.delete(selectionId);
      }
    }
  }

  async register(filePath) {
    if (typeof filePath !== "string" || !path.isAbsolute(filePath)) {
      throw new Error("Selected GPX source is not an absolute local path");
    }
    let canonicalPath;
    try {
      canonicalPath = await realpath(filePath);
    } catch {
      throw new Error("Selected GPX source is no longer available");
    }
    const extension = path.extname(canonicalPath).toLowerCase();
    if (!GPX_EXTENSIONS.has(extension)) {
      throw new Error("Selected GPX source has an unsupported extension");
    }
    let sourceStat;
    try {
      sourceStat = await stat(canonicalPath);
    } catch {
      throw new Error("Selected GPX source is no longer available");
    }
    if (
      !sourceStat.isFile() ||
      sourceStat.size < 1 ||
      sourceStat.size > this.maxSizeBytes
    ) {
      throw new Error("Selected GPX source is empty, too large, or not a regular file");
    }

    this.#discardExpired();
    while (this.selections.size >= this.maxSelections) {
      const oldestSelectionId = this.selections.keys().next().value;
      this.selections.delete(oldestSelectionId);
    }
    const selectionId = this.idFactory();
    if (!UUID_RE.test(selectionId) || this.selections.has(selectionId)) {
      throw new Error("The GPX selection ID generator returned an invalid ID");
    }
    this.selections.set(selectionId, {
      createdAtMs: this.now(),
      path: canonicalPath,
    });
    return Object.freeze({
      selectionId,
      file: Object.freeze({
        name: safeFileName(canonicalPath),
        extension: "gpx",
        sizeBytes: sourceStat.size,
        lastModifiedMs: Math.trunc(sourceStat.mtimeMs),
      }),
    });
  }

  resolvePath(selectionId) {
    if (typeof selectionId !== "string" || !UUID_RE.test(selectionId)) {
      throw new Error("GPX selection ID is invalid");
    }
    this.#discardExpired();
    const selection = this.selections.get(selectionId);
    if (!selection) {
      throw new Error("GPX selection is missing or expired");
    }
    return selection.path;
  }

  clear() {
    this.selections.clear();
  }
}

export class LocalDirectorySelectionRegistry {
  constructor({
    ttlMs = 4 * 60 * 60 * 1_000,
    maxSelections = 32,
    now = () => Date.now(),
    idFactory = randomUUID,
  } = {}) {
    this.ttlMs = ttlMs;
    this.maxSelections = maxSelections;
    this.now = now;
    this.idFactory = idFactory;
    this.selections = new Map();
  }

  #discardExpired() {
    const currentTime = this.now();
    for (const [selectionId, selection] of this.selections) {
      if (currentTime - selection.createdAtMs >= this.ttlMs) {
        this.selections.delete(selectionId);
      }
    }
  }

  async register(directoryPath) {
    if (
      typeof directoryPath !== "string" ||
      !path.isAbsolute(directoryPath)
    ) {
      throw new Error("Selected export directory is not an absolute path");
    }
    let canonicalPath;
    let directoryStat;
    try {
      canonicalPath = await realpath(directoryPath);
      directoryStat = await stat(canonicalPath);
    } catch {
      throw new Error("Selected export directory is no longer available");
    }
    if (!directoryStat.isDirectory()) {
      throw new Error("Selected export destination is not a directory");
    }
    const parsed = path.parse(canonicalPath);
    if (
      path.resolve(canonicalPath) === path.resolve(parsed.root) ||
      path.resolve(canonicalPath) === path.resolve(homedir())
    ) {
      throw new Error("Select a dedicated export folder, not a filesystem root");
    }

    this.#discardExpired();
    while (this.selections.size >= this.maxSelections) {
      const oldestSelectionId = this.selections.keys().next().value;
      this.selections.delete(oldestSelectionId);
    }
    const selectionId = this.idFactory();
    if (!UUID_RE.test(selectionId) || this.selections.has(selectionId)) {
      throw new Error("The export directory selection ID is invalid");
    }
    this.selections.set(selectionId, {
      createdAtMs: this.now(),
      path: canonicalPath,
    });
    return Object.freeze({
      selectionId,
      directory: Object.freeze({
        name: safeFileName(canonicalPath) || "선택한 폴더",
      }),
    });
  }

  resolvePath(selectionId) {
    if (typeof selectionId !== "string" || !UUID_RE.test(selectionId)) {
      throw new Error("Export directory selection ID is invalid");
    }
    this.#discardExpired();
    const selection = this.selections.get(selectionId);
    if (!selection) {
      throw new Error("Export directory selection is missing or expired");
    }
    return selection.path;
  }

  consumePath(selectionId) {
    const selectedPath = this.resolvePath(selectionId);
    this.selections.delete(selectionId);
    return selectedPath;
  }

  clear() {
    this.selections.clear();
  }
}

function safeVolumeLabel(directoryPath) {
  const root = path.parse(directoryPath).root.replace(/[\\/]+$/, "");
  if (/^[A-Za-z]:$/.test(root)) return root.toUpperCase();
  if (root.startsWith("\\\\")) return "네트워크 위치";
  return "로컬 저장소";
}

async function availableFreeBytes(directoryPath) {
  const capacity = await statfs(directoryPath, { bigint: true });
  const free = capacity.bavail * capacity.bsize;
  const maximum = BigInt(Number.MAX_SAFE_INTEGER);
  return Number(free > maximum ? maximum : free);
}

export class ProjectOutputDirectorySelectionRegistry extends LocalDirectorySelectionRegistry {
  constructor({ capacityResolver = availableFreeBytes, ...options } = {}) {
    super(options);
    if (typeof capacityResolver !== "function") {
      throw new Error("Output directory capacity resolver is invalid");
    }
    this.capacityResolver = capacityResolver;
  }

  async register(directoryPath) {
    const selection = await super.register(directoryPath);
    const canonicalPath = this.resolvePath(selection.selectionId);
    let freeBytes;
    try {
      freeBytes = await this.capacityResolver(canonicalPath);
    } catch {
      this.selections.delete(selection.selectionId);
      throw new Error("Selected output directory capacity is unavailable");
    }
    if (
      !Number.isSafeInteger(freeBytes) ||
      freeBytes < 0
    ) {
      this.selections.delete(selection.selectionId);
      throw new Error("Selected output directory capacity is invalid");
    }
    return Object.freeze({
      selectionId: selection.selectionId,
      directory: Object.freeze({
        name: selection.directory.name,
        volumeLabel: safeVolumeLabel(canonicalPath),
        availableFreeBytes: freeBytes,
        pathExposed: false,
      }),
    });
  }
}
