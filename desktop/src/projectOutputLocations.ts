export type ProjectOutputLocation = {
  outputLocationId: string;
  registryRevision: number;
  displayName: string;
  volumeLabel: string;
  availableFreeBytes: number | null;
  isDefault: boolean;
  state: "ready" | "unavailable";
  pathExposed: false;
};

const LOCATION_KEYS = [
  "output_location_id",
  "registry_revision",
  "display_name",
  "volume_label",
  "available_free_bytes",
  "is_default",
  "state",
  "path_exposed",
] as const;

function object(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
  label: string,
) {
  const actual = Object.keys(value).sort();
  const required = [...expected].sort();
  if (
    actual.length !== required.length ||
    actual.some((key, index) => key !== required[index])
  ) {
    throw new Error(`${label} 응답에 허용되지 않은 필드가 있습니다.`);
  }
}

export function parseProjectOutputLocation(
  value: unknown,
): ProjectOutputLocation {
  const row = object(value, "결과 저장 위치");
  exactKeys(row, LOCATION_KEYS, "결과 저장 위치");
  if (
    typeof row.output_location_id !== "string" ||
    !/^(?:project-default|project-output-[a-f0-9]{32})$/.test(
      row.output_location_id,
    ) ||
    !Number.isSafeInteger(row.registry_revision) ||
    Number(row.registry_revision) < 1 ||
    typeof row.display_name !== "string" ||
    row.display_name.length < 1 ||
    row.display_name.length > 120 ||
    typeof row.volume_label !== "string" ||
    row.volume_label.length < 1 ||
    row.volume_label.length > 32 ||
    (row.available_free_bytes !== null &&
      (!Number.isSafeInteger(row.available_free_bytes) ||
        Number(row.available_free_bytes) < 0)) ||
    typeof row.is_default !== "boolean" ||
    !["ready", "unavailable"].includes(String(row.state)) ||
    row.path_exposed !== false
  ) {
    throw new Error("결과 저장 위치 응답 값이 올바르지 않습니다.");
  }
  return {
    outputLocationId: row.output_location_id,
    registryRevision: Number(row.registry_revision),
    displayName: row.display_name,
    volumeLabel: row.volume_label,
    availableFreeBytes:
      row.available_free_bytes === null
        ? null
        : Number(row.available_free_bytes),
    isDefault: row.is_default,
    state: row.state as "ready" | "unavailable",
    pathExposed: false,
  };
}

export function parseProjectOutputLocationList(
  value: unknown,
): ProjectOutputLocation[] {
  const envelope = object(value, "결과 저장 위치 목록");
  exactKeys(
    envelope,
    ["schema_version", "locations", "path_exposed"],
    "결과 저장 위치 목록",
  );
  if (
    envelope.schema_version !==
      "trafficvisson.project-output-location-list.v1" ||
    envelope.path_exposed !== false ||
    !Array.isArray(envelope.locations) ||
    envelope.locations.length > 64
  ) {
    throw new Error("결과 저장 위치 목록 응답이 올바르지 않습니다.");
  }
  return envelope.locations.map(parseProjectOutputLocation);
}

export function parseProjectOutputLocationRegistration(
  value: unknown,
): ProjectOutputLocation {
  const envelope = object(value, "결과 저장 위치 등록");
  exactKeys(
    envelope,
    ["schema_version", "location"],
    "결과 저장 위치 등록",
  );
  if (
    envelope.schema_version !==
    "trafficvisson.project-output-location.v1"
  ) {
    throw new Error("결과 저장 위치 등록 응답이 올바르지 않습니다.");
  }
  return parseProjectOutputLocation(envelope.location);
}
