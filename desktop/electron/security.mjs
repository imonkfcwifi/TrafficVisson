import path from "node:path";

export const RENDERER_SCHEME = "trafficvisson";
export const RENDERER_HOST = "app";
export const EVIDENCE_SCHEME = "trafficvisson-evidence";
export const EVIDENCE_HOST = "artifact";
const UUID_PATTERN =
  /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const CANDIDATE_ID_PATTERN = /^[a-f0-9]{20}$/;
const EVIDENCE_KINDS = new Set(["image", "clip"]);
export const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "font-src 'self' data:",
  `img-src 'self' data: blob: ${EVIDENCE_SCHEME}:`,
  `media-src 'self' blob: ${EVIDENCE_SCHEME}:`,
  "connect-src 'self'",
  "worker-src 'self' blob:",
].join("; ");

// @vitejs/plugin-react injects one fixed React Refresh bootstrap block into
// development HTML. Allow only that reviewed block; production keeps the
// stricter policy above and never enables arbitrary inline scripts.
export const VITE_DEVELOPMENT_CONTENT_SECURITY_POLICY =
  CONTENT_SECURITY_POLICY.replace(
    "script-src 'self'",
    "script-src 'self' 'sha256-Z2/iFzh9VMlVkEOar1f/oSHWwQk3ve1qk/C2WdsC4Xk='",
  );

function evidenceIdentity(value, pattern, label) {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}

/**
 * Build the only renderer-visible candidate evidence URL.
 *
 * Shape:
 * trafficvisson-evidence://artifact/projects/{projectUUID}/jobs/{jobUUID}/
 * candidates/{candidateId}/{image|clip}
 */
export function buildEvidenceUrl({ projectId, jobId, candidateId, kind }) {
  const project = evidenceIdentity(projectId, UUID_PATTERN, "projectId");
  const job = evidenceIdentity(jobId, UUID_PATTERN, "jobId");
  const candidate = evidenceIdentity(
    candidateId,
    CANDIDATE_ID_PATTERN,
    "candidateId",
  );
  if (!EVIDENCE_KINDS.has(kind)) {
    throw new TypeError("kind is invalid");
  }
  return (
    `${EVIDENCE_SCHEME}://${EVIDENCE_HOST}/projects/${project}/` +
    `jobs/${job}/candidates/${candidate}/${kind}`
  );
}

export function parseEvidenceUrl(candidate) {
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new TypeError("Evidence URL is malformed");
  }
  if (
    parsed.protocol !== `${EVIDENCE_SCHEME}:` ||
    parsed.hostname !== EVIDENCE_HOST ||
    parsed.username ||
    parsed.password ||
    parsed.port ||
    parsed.search ||
    parsed.hash
  ) {
    throw new TypeError("Evidence URL origin or components are invalid");
  }
  const segments = parsed.pathname.split("/");
  if (
    segments.length !== 8 ||
    segments[0] !== "" ||
    segments[1] !== "projects" ||
    segments[3] !== "jobs" ||
    segments[5] !== "candidates"
  ) {
    throw new TypeError("Evidence URL path is invalid");
  }
  const projectId = evidenceIdentity(segments[2], UUID_PATTERN, "projectId");
  const jobId = evidenceIdentity(segments[4], UUID_PATTERN, "jobId");
  const candidateId = evidenceIdentity(
    segments[6],
    CANDIDATE_ID_PATTERN,
    "candidateId",
  );
  const kind = segments[7];
  if (!EVIDENCE_KINDS.has(kind)) {
    throw new TypeError("kind is invalid");
  }
  return Object.freeze({ projectId, jobId, candidateId, kind });
}

export function evidenceBridgePath(value) {
  const { projectId, jobId, candidateId, kind } =
    typeof value === "string"
      ? parseEvidenceUrl(value)
      : parseEvidenceUrl(buildEvidenceUrl(value));
  return (
    `/projects/${projectId}/analysis/jobs/${jobId}/` +
    `candidates/${candidateId}/${kind}`
  );
}

export function parseDevServerUrl(candidate) {
  if (!candidate) return null;
  const parsed = new URL(candidate);
  if (
    parsed.protocol !== "http:" ||
    !["127.0.0.1", "localhost"].includes(parsed.hostname) ||
    parsed.username ||
    parsed.password
  ) {
    throw new Error("Development renderer URL must be loopback HTTP");
  }
  return parsed;
}

export function resolveDevServerUrl(isPackaged, candidate) {
  return isPackaged ? null : candidate || null;
}

export function isAllowedRendererNavigation(candidate, devServerUrl = null) {
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    return false;
  }
  if (parsed.protocol === `${RENDERER_SCHEME}:` && parsed.hostname === RENDERER_HOST) {
    return true;
  }
  const dev = parseDevServerUrl(devServerUrl);
  return Boolean(dev && parsed.origin === dev.origin);
}

export function resolveRendererAsset(rendererRoot, requestUrl) {
  const rawPath = String(requestUrl).split(/[?#]/, 1)[0];
  if (
    /%2e/i.test(rawPath) ||
    /(?:^|\/)\.\.(?:\/|$)/.test(rawPath)
  ) {
    throw new Error("Renderer asset escaped the application root");
  }
  const parsed = new URL(requestUrl);
  if (parsed.protocol !== `${RENDERER_SCHEME}:` || parsed.hostname !== RENDERER_HOST) {
    throw new Error("Renderer request origin is not allowed");
  }
  let decodedPath;
  try {
    decodedPath = decodeURIComponent(parsed.pathname);
  } catch {
    throw new Error("Renderer asset path is malformed");
  }
  if (decodedPath.includes("\0") || decodedPath.includes("\\")) {
    throw new Error("Renderer asset path is invalid");
  }
  if (decodedPath.split("/").some((segment) => segment === "..")) {
    throw new Error("Renderer asset escaped the application root");
  }
  const normalizedRelative = path.posix.normalize(decodedPath).replace(/^\/+/, "");
  if (normalizedRelative.startsWith("../") || normalizedRelative === "..") {
    throw new Error("Renderer asset escaped the application root");
  }
  const requested = normalizedRelative || "index.html";
  const target = path.resolve(rendererRoot, requested);
  const relative = path.relative(path.resolve(rendererRoot), target);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error("Renderer asset escaped the application root");
  }
  return target;
}
