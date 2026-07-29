import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const mainSource = await readFile(
  new URL("../main.mjs", import.meta.url),
  "utf8",
);
const preloadSource = await readFile(
  new URL("../preload.cjs", import.meta.url),
  "utf8",
);

test("source candidates use a narrow read-only IPC route", () => {
  assert.match(
    mainSource,
    /registerHandler\(IPC_CHANNELS\.SOURCE_CANDIDATES_LIST,[\s\S]*?supervisor\.request\("GET", "\/source-candidates"\)/,
  );
  assert.match(
    preloadSource,
    /sources:\s*Object\.freeze\(\{\s*candidates:\s*\(\)\s*=>\s*invoke\(CHANNELS\.SOURCE_CANDIDATES_LIST\)/,
  );
  assert.doesNotMatch(
    preloadSource,
    /sources:\s*Object\.freeze\(\{[\s\S]*?(download|activate|install|execute)\s*:/,
  );
});
