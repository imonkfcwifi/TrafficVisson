import assert from "node:assert/strict";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const {
  parseProjectOutputLocationList,
  parseProjectOutputLocationRegistration,
} = await vite.ssrLoadModule("/src/projectOutputLocations.ts");

after(async () => {
  await vite.close();
});

const LOCATION = {
  output_location_id:
    "project-output-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  registry_revision: 2,
  display_name: "분석 결과",
  volume_label: "E:",
  available_free_bytes: 98_765_432_100,
  is_default: false,
  state: "ready",
  path_exposed: false,
};

test("output location parser keeps only path-free capacity metadata", () => {
  const location = parseProjectOutputLocationRegistration({
    schema_version: "trafficvisson.project-output-location.v1",
    location: LOCATION,
  });
  assert.equal(
    location.outputLocationId,
    LOCATION.output_location_id,
  );
  assert.equal(location.volumeLabel, "E:");
  assert.equal(location.availableFreeBytes, 98_765_432_100);
  assert.equal(JSON.stringify(location).includes("\\"), false);

  const rows = parseProjectOutputLocationList({
    schema_version:
      "trafficvisson.project-output-location-list.v1",
    locations: [LOCATION],
    path_exposed: false,
  });
  assert.equal(rows.length, 1);
});

test("output location parser rejects a bridge response containing a raw path", () => {
  assert.throws(
    () =>
      parseProjectOutputLocationList({
        schema_version:
          "trafficvisson.project-output-location-list.v1",
        locations: [
          {
            ...LOCATION,
            artifact_root_path: "E:\\private\\results",
          },
        ],
        path_exposed: false,
      }),
    /허용되지 않은 필드/,
  );
});
