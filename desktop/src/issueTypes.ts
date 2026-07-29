import type { IncidentType } from "./domain/types";

/**
 * One renderer-side source of truth for the twelve maintenance types in the
 * product plan. API values are stable lowercase identifiers; IncidentType is
 * the uppercase domain/export identifier shown to operators.
 */
export const CANONICAL_ISSUE_TYPES = [
  { apiValue: "pothole", incidentType: "POTHOLE", labelKo: "포트홀" },
  {
    apiValue: "road_surface_damage",
    incidentType: "ROAD_SURFACE_DAMAGE",
    labelKo: "노면 균열·파손",
  },
  {
    apiValue: "road_debris",
    incidentType: "ROAD_DEBRIS",
    labelKo: "낙하물·장애물",
  },
  {
    apiValue: "median_barrier_damage",
    incidentType: "MEDIAN_BARRIER_DAMAGE",
    labelKo: "중앙분리대 파손",
  },
  {
    apiValue: "sign_damage",
    incidentType: "SIGN_DAMAGE",
    labelKo: "표지판 파손",
  },
  {
    apiValue: "sign_vegetation_occlusion",
    incidentType: "SIGN_VEGETATION_OCCLUSION",
    labelKo: "표지판 식생 가림",
  },
  {
    apiValue: "roadside_mowing_required",
    incidentType: "ROADSIDE_MOWING_REQUIRED",
    labelKo: "삭초 필요",
  },
  {
    apiValue: "guide_sign_identity",
    incidentType: "GUIDE_SIGN_IDENTITY",
    labelKo: "이정표 이름 확인",
  },
  {
    apiValue: "tunnel_identity",
    incidentType: "TUNNEL_IDENTITY",
    labelKo: "터널 이름 확인",
  },
  {
    apiValue: "tunnel_name_occlusion",
    incidentType: "TUNNEL_NAME_OCCLUSION",
    labelKo: "터널명 식생 가림",
  },
  {
    apiValue: "lane_marking_faded",
    incidentType: "LANE_MARKING_FADED",
    labelKo: "차선 재도색 필요",
  },
  {
    apiValue: "colored_guidance_faded",
    incidentType: "COLORED_GUIDANCE_FADED",
    labelKo: "색깔유도선 재도색 필요",
  },
] as const satisfies ReadonlyArray<{
  apiValue: string;
  incidentType: IncidentType;
  labelKo: string;
}>;

export type CanonicalIssueTypeApi =
  (typeof CANONICAL_ISSUE_TYPES)[number]["apiValue"];
export type SupportedIssueTypeApi = CanonicalIssueTypeApi | "other";
export type SupportedIncidentType = IncidentType | "OTHER";

export const ISSUE_TYPE_ALIASES = {
  surface_damage: "road_surface_damage",
  debris: "road_debris",
  sign_vegetation: "sign_vegetation_occlusion",
  mowing: "roadside_mowing_required",
  sign_tunnel_identity: "guide_sign_identity",
  sign_identity: "guide_sign_identity",
  lane_color_line_fading: "colored_guidance_faded",
  color_guidance_line_faded: "colored_guidance_faded",
  colored_guidance_line_faded: "colored_guidance_faded",
} as const satisfies Record<string, CanonicalIssueTypeApi>;

const canonicalApiValues = new Set<string>(
  CANONICAL_ISSUE_TYPES.map(({ apiValue }) => apiValue),
);
const definitionByApi = new Map(
  CANONICAL_ISSUE_TYPES.map((definition) => [
    definition.apiValue,
    definition,
  ]),
);
const definitionByIncident = new Map(
  CANONICAL_ISSUE_TYPES.map((definition) => [
    definition.incidentType,
    definition,
  ]),
);

export function normalizeIssueTypeApi(
  value: unknown,
): SupportedIssueTypeApi | null {
  if (value === "other") return "other";
  if (typeof value !== "string") return null;
  if (canonicalApiValues.has(value)) return value as CanonicalIssueTypeApi;
  return ISSUE_TYPE_ALIASES[value as keyof typeof ISSUE_TYPE_ALIASES] ?? null;
}

export function incidentTypeFromApi(
  value: unknown,
): SupportedIncidentType | null {
  const normalized = normalizeIssueTypeApi(value);
  if (normalized === "other") return "OTHER";
  return normalized
    ? (definitionByApi.get(normalized)?.incidentType ?? null)
    : null;
}

export function apiIssueTypeFromIncident(
  value: unknown,
): SupportedIssueTypeApi | null {
  if (value === "OTHER") return "other";
  if (typeof value !== "string") return null;
  return definitionByIncident.get(value as IncidentType)?.apiValue ?? null;
}

export function issueLabelKo(value: unknown): string | null {
  const normalized = normalizeIssueTypeApi(value);
  if (normalized === "other") return "기타 도로 문제";
  return normalized ? (definitionByApi.get(normalized)?.labelKo ?? null) : null;
}

export const REVIEW_ISSUE_TYPE_OPTIONS = [
  ["UNCLASSIFIED_ROAD_EVENT", "유형을 직접 선택하세요"],
  ...CANONICAL_ISSUE_TYPES.map(
    ({ incidentType, labelKo }) => [incidentType, labelKo] as const,
  ),
  ["OTHER", "기타 도로 문제"],
] as const;
