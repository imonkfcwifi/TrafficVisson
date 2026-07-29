import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  IPC_CHANNELS,
  validateIpcPayload,
} from "../contracts.mjs";

const mainSource = await readFile(
  new URL("../main.mjs", import.meta.url),
  "utf8",
);
const preloadSource = await readFile(
  new URL("../preload.cjs", import.meta.url),
  "utf8",
);

test("runtime readiness is a read-only no-payload IPC route", () => {
  assert.equal(
    IPC_CHANNELS.RUNTIME_READINESS,
    "trafficvisson:runtime:readiness",
  );
  assert.deepEqual(
    validateIpcPayload(IPC_CHANNELS.RUNTIME_READINESS),
    {},
  );
  assert.match(
    mainSource,
    /registerHandler\(IPC_CHANNELS\.RUNTIME_READINESS,[\s\S]*?supervisor\.request\("GET", "\/runtime\/readiness"\)/,
  );
  assert.match(
    preloadSource,
    /runtime:\s*Object\.freeze\(\{\s*readiness:\s*\(\)\s*=>\s*invoke\(CHANNELS\.RUNTIME_READINESS\)/,
  );
  assert.doesNotMatch(
    preloadSource,
    /runtime:\s*Object\.freeze\(\{[\s\S]*?(install|download|activate|execute)\s*:/,
  );
});
