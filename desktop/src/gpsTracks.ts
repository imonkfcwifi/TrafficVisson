export type GpxRoutePoint = {
  sequence: number;
  sourceSequence: number;
  recordedAt: string;
  elapsedSeconds: number;
  videoTimeSeconds: number;
  latitude: number;
  longitude: number;
  altitudeMeters: number | null;
  speedKph: number | null;
  distanceFromPreviousMeters: number;
  gapFromPrevious: boolean;
};

export type GpxTrack = {
  schemaVersion: "trafficvisson.gpx-track.v1";
  trackId: string;
  projectId: string;
  tripId: string;
  sourceName: string;
  sourceSizeBytes: number;
  sourceSha256: string;
  pointCount: number;
  displayedPointCount: number;
  sampledForDisplay: boolean;
  startedAt: string;
  endedAt: string;
  durationSeconds: number;
  totalDistanceMeters: number;
  videoOffsetSeconds: number;
  revision: number;
  replacesTrackId: string | null;
  importedAt: string;
  updatedAt: string;
  routePoints: GpxRoutePoint[];
};

export type InterpolatedGpxPosition = {
  latitude: number;
  longitude: number;
  altitudeMeters: number | null;
  speedKph: number | null;
  videoTimeSeconds: number;
  gap: false;
};

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256_RE = /^[a-f0-9]{64}$/;
const MAX_ROUTE_POINTS = 8_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function text(value: unknown, label: string, maximum = 4096): string {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > maximum ||
    value.includes("\0")
  ) {
    throw new Error(`${label}이(가) 올바르지 않습니다.`);
  }
  return value;
}

function uuid(value: unknown, label: string): string {
  const parsed = text(value, label, 64);
  if (!UUID_RE.test(parsed)) {
    throw new Error(`${label} 식별자가 올바르지 않습니다.`);
  }
  return parsed.toLowerCase();
}

function finite(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < minimum ||
    value > maximum
  ) {
    throw new Error(`${label} 값이 허용 범위를 벗어났습니다.`);
  }
  return value;
}

function integer(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
): number {
  const parsed = finite(value, label, minimum, maximum);
  if (!Number.isInteger(parsed)) {
    throw new Error(`${label} 값은 정수여야 합니다.`);
  }
  return parsed;
}

function nullableFinite(
  value: unknown,
  label: string,
  minimum: number,
  maximum: number,
): number | null {
  return value === null ? null : finite(value, label, minimum, maximum);
}

function timestamp(value: unknown, label: string): string {
  const parsed = text(value, label, 64);
  if (!Number.isFinite(Date.parse(parsed))) {
    throw new Error(`${label} 시각이 올바르지 않습니다.`);
  }
  return parsed;
}

function routePoint(
  value: unknown,
  index: number,
  previous: GpxRoutePoint | null,
): GpxRoutePoint {
  if (!isRecord(value)) {
    throw new Error(`route_points[${index}]가 올바르지 않습니다.`);
  }
  const point: GpxRoutePoint = {
    sequence: integer(value.sequence, "sequence", 0, MAX_ROUTE_POINTS - 1),
    sourceSequence: integer(
      value.source_sequence,
      "source_sequence",
      0,
      50_000 - 1,
    ),
    recordedAt: timestamp(value.recorded_at, "recorded_at"),
    elapsedSeconds: finite(
      value.elapsed_seconds,
      "elapsed_seconds",
      0,
      7 * 24 * 60 * 60,
    ),
    videoTimeSeconds: finite(
      value.video_time_seconds,
      "video_time_seconds",
      -86_400,
      8 * 24 * 60 * 60,
    ),
    latitude: finite(value.latitude, "latitude", -90, 90),
    longitude: finite(value.longitude, "longitude", -180, 180),
    altitudeMeters: nullableFinite(
      value.altitude_meters,
      "altitude_meters",
      -1_000,
      20_000,
    ),
    speedKph: nullableFinite(value.speed_kph, "speed_kph", 0, 300),
    distanceFromPreviousMeters: finite(
      value.distance_from_previous_meters,
      "distance_from_previous_meters",
      0,
      2_000_000,
    ),
    gapFromPrevious:
      typeof value.gap_from_previous === "boolean"
        ? value.gap_from_previous
        : (() => {
            throw new Error("gap_from_previous 값이 올바르지 않습니다.");
          })(),
  };
  if (
    point.sequence !== index ||
    (previous &&
      (point.sourceSequence <= previous.sourceSequence ||
        point.elapsedSeconds <= previous.elapsedSeconds ||
        point.videoTimeSeconds <= previous.videoTimeSeconds))
  ) {
    throw new Error("GPX 경로점의 순서가 올바르지 않습니다.");
  }
  return point;
}

export function parseGpxTrack(
  value: unknown,
  expectedProjectId: string,
  expectedTripId: string,
): GpxTrack {
  if (!isRecord(value)) {
    throw new Error("GPX 응답이 올바르지 않습니다.");
  }
  if (value.schema_version !== "trafficvisson.gpx-track.v1") {
    throw new Error("지원하지 않는 GPX 응답 버전입니다.");
  }
  const projectId = uuid(value.project_id, "project_id");
  const tripId = uuid(value.trip_id, "trip_id");
  if (
    projectId !== expectedProjectId.toLowerCase() ||
    tripId !== expectedTripId.toLowerCase()
  ) {
    throw new Error("GPX 응답이 다른 프로젝트 또는 주행에 속합니다.");
  }
  if (
    !Array.isArray(value.route_points) ||
    value.route_points.length < 2 ||
    value.route_points.length > MAX_ROUTE_POINTS
  ) {
    throw new Error("표시할 GPX 경로점 수가 올바르지 않습니다.");
  }
  const routePoints: GpxRoutePoint[] = [];
  for (let index = 0; index < value.route_points.length; index += 1) {
    routePoints.push(
      routePoint(
        value.route_points[index],
        index,
        routePoints[index - 1] ?? null,
      ),
    );
  }
  const sourceName = text(value.source_name, "source_name", 255);
  if (/[\\/]/.test(sourceName)) {
    throw new Error("GPX 파일 이름에 경로가 포함되어 있습니다.");
  }
  const sourceSha256 = text(value.source_sha256, "source_sha256", 64);
  if (!SHA256_RE.test(sourceSha256)) {
    throw new Error("GPX SHA-256 값이 올바르지 않습니다.");
  }
  const pointCount = integer(value.point_count, "point_count", 2, 50_000);
  const displayedPointCount = integer(
    value.displayed_point_count,
    "displayed_point_count",
    2,
    MAX_ROUTE_POINTS,
  );
  if (
    displayedPointCount !== routePoints.length ||
    displayedPointCount > pointCount ||
    typeof value.sampled_for_display !== "boolean"
  ) {
    throw new Error("GPX 표시 표본 메타데이터가 올바르지 않습니다.");
  }
  return {
    schemaVersion: "trafficvisson.gpx-track.v1",
    trackId: uuid(value.track_id, "track_id"),
    projectId,
    tripId,
    sourceName,
    sourceSizeBytes: integer(
      value.source_size_bytes,
      "source_size_bytes",
      1,
      25 * 1024 * 1024,
    ),
    sourceSha256,
    pointCount,
    displayedPointCount,
    sampledForDisplay: value.sampled_for_display,
    startedAt: timestamp(value.started_at, "started_at"),
    endedAt: timestamp(value.ended_at, "ended_at"),
    durationSeconds: finite(
      value.duration_seconds,
      "duration_seconds",
      0.001,
      7 * 24 * 60 * 60,
    ),
    totalDistanceMeters: finite(
      value.total_distance_meters,
      "total_distance_meters",
      0,
      10_000_000,
    ),
    videoOffsetSeconds: finite(
      value.video_offset_seconds,
      "video_offset_seconds",
      -86_400,
      86_400,
    ),
    revision: integer(value.revision, "revision", 1, 1_000_000),
    replacesTrackId:
      value.replaces_track_id === null
        ? null
        : uuid(value.replaces_track_id, "replaces_track_id"),
    importedAt: timestamp(value.imported_at, "imported_at"),
    updatedAt: timestamp(value.updated_at, "updated_at"),
    routePoints,
  };
}

function interpolateNullable(
  before: number | null,
  after: number | null,
  ratio: number,
): number | null {
  if (before === null || after === null) return before ?? after;
  return before + (after - before) * ratio;
}

export function interpolateGpxPosition(
  track: GpxTrack,
  videoTimeSeconds: number,
): InterpolatedGpxPosition | null {
  if (!Number.isFinite(videoTimeSeconds)) return null;
  const points = track.routePoints;
  if (
    videoTimeSeconds < points[0].videoTimeSeconds ||
    videoTimeSeconds > points[points.length - 1].videoTimeSeconds
  ) {
    return null;
  }
  let lower = 0;
  let upper = points.length - 1;
  while (lower <= upper) {
    const middle = Math.floor((lower + upper) / 2);
    const point = points[middle];
    if (point.videoTimeSeconds === videoTimeSeconds) {
      return {
        latitude: point.latitude,
        longitude: point.longitude,
        altitudeMeters: point.altitudeMeters,
        speedKph: point.speedKph,
        videoTimeSeconds,
        gap: false,
      };
    }
    if (point.videoTimeSeconds < videoTimeSeconds) {
      lower = middle + 1;
    } else {
      upper = middle - 1;
    }
  }
  const after = points[Math.min(points.length - 1, lower)];
  const before = points[Math.max(0, lower - 1)];
  if (
    after === before ||
    after.gapFromPrevious ||
    after.videoTimeSeconds <= before.videoTimeSeconds
  ) {
    return null;
  }
  const ratio =
    (videoTimeSeconds - before.videoTimeSeconds) /
    (after.videoTimeSeconds - before.videoTimeSeconds);
  return {
    latitude: before.latitude + (after.latitude - before.latitude) * ratio,
    longitude: before.longitude + (after.longitude - before.longitude) * ratio,
    altitudeMeters: interpolateNullable(
      before.altitudeMeters,
      after.altitudeMeters,
      ratio,
    ),
    speedKph: interpolateNullable(before.speedKph, after.speedKph, ratio),
    videoTimeSeconds,
    gap: false,
  };
}
