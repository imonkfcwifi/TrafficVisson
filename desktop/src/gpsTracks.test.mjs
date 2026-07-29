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
const gps = await vite.ssrLoadModule("/src/gpsTracks.ts");

const PROJECT_ID = "11111111-1111-4111-8111-111111111111";
const TRIP_ID = "22222222-2222-4222-8222-222222222222";

function response() {
  return {
    schema_version: "trafficvisson.gpx-track.v1",
    track_id: "33333333-3333-4333-8333-333333333333",
    project_id: PROJECT_ID,
    trip_id: TRIP_ID,
    source_name: "주행 경로.gpx",
    source_size_bytes: 2048,
    source_sha256: "a".repeat(64),
    point_count: 3,
    displayed_point_count: 3,
    sampled_for_display: false,
    started_at: "2026-07-29T00:00:00+00:00",
    ended_at: "2026-07-29T00:00:20+00:00",
    duration_seconds: 20,
    total_distance_meters: 150.25,
    video_offset_seconds: 2,
    revision: 1,
    replaces_track_id: null,
    imported_at: "2026-07-29T00:01:00+00:00",
    updated_at: "2026-07-29T00:01:00+00:00",
    route_points: [
      {
        sequence: 0,
        source_sequence: 0,
        recorded_at: "2026-07-29T00:00:00+00:00",
        elapsed_seconds: 0,
        video_time_seconds: 2,
        latitude: 37.5,
        longitude: 127,
        altitude_meters: 10,
        speed_kph: null,
        distance_from_previous_meters: 0,
        gap_from_previous: false,
      },
      {
        sequence: 1,
        source_sequence: 1,
        recorded_at: "2026-07-29T00:00:05+00:00",
        elapsed_seconds: 5,
        video_time_seconds: 7,
        latitude: 37.5005,
        longitude: 127.0005,
        altitude_meters: 11,
        speed_kph: 35,
        distance_from_previous_meters: 75,
        gap_from_previous: false,
      },
      {
        sequence: 2,
        source_sequence: 2,
        recorded_at: "2026-07-29T00:00:20+00:00",
        elapsed_seconds: 20,
        video_time_seconds: 22,
        latitude: 37.501,
        longitude: 127.001,
        altitude_meters: 12,
        speed_kph: 30,
        distance_from_previous_meters: 75.25,
        gap_from_previous: true,
      },
    ],
  };
}

after(async () => {
  await vite.close();
});

test("GPX responses preserve project scope and provenance", () => {
  const track = gps.parseGpxTrack(response(), PROJECT_ID, TRIP_ID);
  assert.equal(track.projectId, PROJECT_ID);
  assert.equal(track.tripId, TRIP_ID);
  assert.equal(track.sourceName, "주행 경로.gpx");
  assert.equal(track.routePoints[1].videoTimeSeconds, 7);
  assert.equal(track.routePoints[2].gapFromPrevious, true);
});

test("position interpolation does not invent coordinates across GPS gaps", () => {
  const track = gps.parseGpxTrack(response(), PROJECT_ID, TRIP_ID);
  const interpolated = gps.interpolateGpxPosition(track, 4.5);
  assert.ok(interpolated);
  assert.equal(interpolated.latitude, 37.50025);
  assert.equal(interpolated.longitude, 127.00025);
  assert.equal(gps.interpolateGpxPosition(track, 12), null);
  assert.equal(gps.interpolateGpxPosition(track, 1), null);
});

test("GPX parser rejects crossed projects and non-monotonic points", () => {
  assert.throws(
    () =>
      gps.parseGpxTrack(
        { ...response(), project_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" },
        PROJECT_ID,
        TRIP_ID,
      ),
    /다른 프로젝트/,
  );
  const broken = response();
  broken.route_points[1].video_time_seconds = 1;
  assert.throws(
    () => gps.parseGpxTrack(broken, PROJECT_ID, TRIP_ID),
    /순서/,
  );
});
