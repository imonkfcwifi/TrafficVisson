import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  LocalDirectorySelectionRegistry,
  LocalDocumentSelectionRegistry,
  LocalGpxSelectionRegistry,
  LocalVideoSelectionRegistry,
} from "../local-selections.mjs";

const SELECTION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

test("video selections expose only an opaque ID and safe metadata", async (context) => {
  const fixtureRoot = await mkdtemp(path.join(os.tmpdir(), "trafficvisson-selection-"));
  context.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  const videoPath = path.join(fixtureRoot, "road-inspection.mp4");
  await writeFile(videoPath, Buffer.alloc(32));

  const registry = new LocalVideoSelectionRegistry({
    idFactory: () => SELECTION_ID,
  });
  const selection = await registry.register([videoPath]);

  assert.equal(selection.selectionId, SELECTION_ID);
  assert.deepEqual(Object.keys(selection.files[0]).sort(), [
    "extension",
    "lastModifiedMs",
    "name",
    "sizeBytes",
  ]);
  assert.equal(selection.files[0].name, "road-inspection.mp4");
  assert.equal(selection.files[0].sizeBytes, 32);
  assert.doesNotMatch(JSON.stringify(selection), /trafficvisson-selection-/);
  assert.deepEqual(registry.resolvePaths(SELECTION_ID), [await realPath(videoPath)]);
  assert.equal(
    registry.resolveSinglePath(SELECTION_ID),
    await realPath(videoPath),
  );
});

test("single-file resolution rejects a multi-file opaque selection", async (context) => {
  const fixtureRoot = await mkdtemp(path.join(os.tmpdir(), "trafficvisson-selection-"));
  context.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  const first = path.join(fixtureRoot, "first.mp4");
  const second = path.join(fixtureRoot, "second.mov");
  await Promise.all([writeFile(first, "one"), writeFile(second, "two")]);
  const registry = new LocalVideoSelectionRegistry({
    idFactory: () => SELECTION_ID,
  });
  await registry.register([first, second]);

  assert.throws(
    () => registry.resolveSinglePath(SELECTION_ID),
    /Exactly one local video/,
  );
});

test("video selections reject unsupported extensions and expire", async (context) => {
  const fixtureRoot = await mkdtemp(path.join(os.tmpdir(), "trafficvisson-selection-"));
  context.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  const invalidPath = path.join(fixtureRoot, "script.exe");
  const validPath = path.join(fixtureRoot, "road.mov");
  await Promise.all([
    writeFile(invalidPath, "not a video"),
    writeFile(validPath, "fixture"),
  ]);

  let now = 1_000;
  const registry = new LocalVideoSelectionRegistry({
    ttlMs: 50,
    now: () => now,
    idFactory: () => SELECTION_ID,
  });
  await assert.rejects(() => registry.register([invalidPath]), /unsupported extension/);
  await assert.rejects(
    () => registry.register([path.join(fixtureRoot, "missing.mp4")]),
    (error) => {
      assert.match(error.message, /no longer available/);
      assert.doesNotMatch(error.message, /trafficvisson-selection-/);
      return true;
    },
  );
  await registry.register([validPath]);
  now += 50;
  assert.throws(() => registry.resolvePaths(SELECTION_ID), /missing or expired/);
});

async function realPath(filePath) {
  const { realpath } = await import("node:fs/promises");
  return realpath(filePath);
}

test("document selections expose one opaque HWP/HWPX handle", async (context) => {
  const fixtureRoot = await mkdtemp(path.join(os.tmpdir(), "trafficvisson-document-"));
  context.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  const templatePath = path.join(fixtureRoot, "점검 양식.hwpx");
  await writeFile(templatePath, "fixture");
  const registry = new LocalDocumentSelectionRegistry({
    idFactory: () => SELECTION_ID,
  });

  const selection = await registry.register(templatePath);

  assert.equal(selection.selectionId, SELECTION_ID);
  assert.equal(selection.file.name, "점검 양식.hwpx");
  assert.doesNotMatch(JSON.stringify(selection), /trafficvisson-document-/);
  assert.equal(registry.resolvePath(SELECTION_ID), await realPath(templatePath));
});

test("GPX selections expose one bounded opaque file handle", async (context) => {
  const fixtureRoot = await mkdtemp(
    path.join(os.tmpdir(), "trafficvisson-gpx-selection-"),
  );
  context.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  const gpxPath = path.join(fixtureRoot, "주행 경로.gpx");
  await writeFile(gpxPath, "<gpx />");
  const registry = new LocalGpxSelectionRegistry({
    idFactory: () => SELECTION_ID,
  });

  const selection = await registry.register(gpxPath);

  assert.equal(selection.selectionId, SELECTION_ID);
  assert.equal(selection.file.name, "주행 경로.gpx");
  assert.equal(selection.file.extension, "gpx");
  assert.doesNotMatch(JSON.stringify(selection), /trafficvisson-gpx-selection-/);
  assert.equal(registry.resolvePath(SELECTION_ID), await realPath(gpxPath));
});

test("GPX selections reject unsupported and oversized files", async (context) => {
  const fixtureRoot = await mkdtemp(
    path.join(os.tmpdir(), "trafficvisson-gpx-selection-"),
  );
  context.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  const xmlPath = path.join(fixtureRoot, "route.xml");
  const gpxPath = path.join(fixtureRoot, "route.gpx");
  await Promise.all([
    writeFile(xmlPath, "<gpx />"),
    writeFile(gpxPath, Buffer.alloc(9)),
  ]);
  const registry = new LocalGpxSelectionRegistry({
    maxSizeBytes: 8,
    idFactory: () => SELECTION_ID,
  });

  await assert.rejects(() => registry.register(xmlPath), /unsupported extension/);
  await assert.rejects(() => registry.register(gpxPath), /too large/);
});

test("export directory selections expose only an opaque handle", async (context) => {
  const fixtureRoot = await mkdtemp(
    path.join(os.tmpdir(), "trafficsupervision-export-parent-"),
  );
  context.after(() => rm(fixtureRoot, { recursive: true, force: true }));
  const exportRoot = path.join(fixtureRoot, "내보내기");
  await mkdir(exportRoot);
  const registry = new LocalDirectorySelectionRegistry({
    idFactory: () => SELECTION_ID,
  });

  const selection = await registry.register(exportRoot);

  assert.equal(selection.selectionId, SELECTION_ID);
  assert.equal(selection.directory.name, "내보내기");
  assert.doesNotMatch(JSON.stringify(selection), /trafficsupervision-export-parent-/);
  assert.equal(registry.resolvePath(SELECTION_ID), await realPath(exportRoot));
  assert.equal(registry.consumePath(SELECTION_ID), await realPath(exportRoot));
  assert.throws(
    () => registry.resolvePath(SELECTION_ID),
    /missing or expired/,
  );
});
