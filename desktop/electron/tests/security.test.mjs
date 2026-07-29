import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import {
  CONTENT_SECURITY_POLICY,
  VITE_DEVELOPMENT_CONTENT_SECURITY_POLICY,
  buildEvidenceUrl,
  buildWeakCaseImageUrl,
  evidenceBridgePath,
  isAllowedRendererNavigation,
  parseEvidenceUrl,
  parseWeakCaseImageUrl,
  parseDevServerUrl,
  resolveDevServerUrl,
  resolveRendererAsset,
  weakCaseImageBridgePath,
} from "../security.mjs";

const PROJECT_ID = "11111111-1111-4111-8111-11111111111a";
const JOB_ID = "22222222-2222-4222-8222-222222222222";
const CANDIDATE_ID = "0123456789abcdefabcd";
const WEAK_SOURCE_ID = "sample-hwpx-3q-24ffb4bc62cd86a6";
const WEAK_CASE_ID =
  "hwpx-weak-case-0123456789abcdef0123456789abcdef";

test("development renderer is restricted to loopback HTTP", () => {
  assert.equal(parseDevServerUrl("http://127.0.0.1:5173").hostname, "127.0.0.1");
  assert.throws(() => parseDevServerUrl("https://example.com"), /loopback/);
  assert.throws(() => parseDevServerUrl("http://user:pass@localhost:5173"), /loopback/);
});

test("packaged builds ignore renderer URL environment overrides", () => {
  assert.equal(
    resolveDevServerUrl(true, "http://127.0.0.1:5173"),
    null,
  );
  assert.equal(
    resolveDevServerUrl(false, "http://127.0.0.1:5173"),
    "http://127.0.0.1:5173",
  );
});

test("renderer navigation stays on the application origin", () => {
  assert.equal(isAllowedRendererNavigation("trafficvisson://app/index.html"), true);
  assert.equal(isAllowedRendererNavigation("https://example.com/"), false);
  assert.equal(
    isAllowedRendererNavigation(
      "http://localhost:5173/questions",
      "http://localhost:5173",
    ),
    true,
  );
  assert.equal(
    isAllowedRendererNavigation("http://localhost:9999", "http://localhost:5173"),
    false,
  );
});

test("renderer asset resolution rejects traversal and foreign origins", () => {
  const root = path.resolve("fixture-renderer");
  assert.equal(
    resolveRendererAsset(root, "trafficvisson://app/assets/index.js"),
    path.join(root, "assets", "index.js"),
  );
  assert.throws(
    () => resolveRendererAsset(root, "trafficvisson://app/%2e%2e/secret.txt"),
    /escaped/,
  );
  assert.throws(
    () => resolveRendererAsset(root, "trafficvisson://evil/index.html"),
    /origin/,
  );
});

test("CSP does not enable remote scripts or unsafe eval", () => {
  assert.match(CONTENT_SECURITY_POLICY, /script-src 'self'/);
  assert.match(
    CONTENT_SECURITY_POLICY,
    /img-src 'self' data: blob: trafficvisson-evidence:/,
  );
  assert.match(
    CONTENT_SECURITY_POLICY,
    /media-src 'self' blob: trafficvisson-evidence:/,
  );
  assert.doesNotMatch(CONTENT_SECURITY_POLICY, /unsafe-eval/);
  assert.doesNotMatch(CONTENT_SECURITY_POLICY, /https?:/);
});

test("development CSP allows only the reviewed Vite React refresh preamble", () => {
  assert.match(
    VITE_DEVELOPMENT_CONTENT_SECURITY_POLICY,
    /script-src 'self' 'sha256-Z2\/iFzh9VMlVkEOar1f\/oSHWwQk3ve1qk\/C2WdsC4Xk='/,
  );
  assert.doesNotMatch(
    VITE_DEVELOPMENT_CONTENT_SECURITY_POLICY,
    /script-src[^;]*'unsafe-inline'/,
  );
  assert.doesNotMatch(VITE_DEVELOPMENT_CONTENT_SECURITY_POLICY, /unsafe-eval/);
});

test("candidate evidence URLs use one deterministic opaque contract", () => {
  const url = buildEvidenceUrl({
    projectId: PROJECT_ID,
    jobId: JOB_ID,
    candidateId: CANDIDATE_ID,
    kind: "image",
  });
  assert.equal(
    url,
    (
      "trafficvisson-evidence://artifact/projects/" +
      `${PROJECT_ID}/jobs/${JOB_ID}/candidates/${CANDIDATE_ID}/image`
    ),
  );
  assert.deepEqual(parseEvidenceUrl(url), {
    projectId: PROJECT_ID,
    jobId: JOB_ID,
    candidateId: CANDIDATE_ID,
    kind: "image",
  });
  assert.equal(
    evidenceBridgePath(url),
    (
      `/projects/${PROJECT_ID}/analysis/jobs/${JOB_ID}/` +
      `candidates/${CANDIDATE_ID}/image`
    ),
  );
});

test("candidate evidence URL parser rejects ambiguous or attacker-controlled forms", () => {
  const valid = buildEvidenceUrl({
    projectId: PROJECT_ID,
    jobId: JOB_ID,
    candidateId: CANDIDATE_ID,
    kind: "clip",
  });
  assert.throws(() => parseEvidenceUrl(`${valid}?token=secret`), /components/);
  assert.throws(() => parseEvidenceUrl(`${valid}#fragment`), /components/);
  assert.throws(
    () =>
      parseEvidenceUrl(
        valid.replace("://artifact/", "://user:password@artifact/"),
      ),
    /components/,
  );
  assert.throws(
    () => parseEvidenceUrl(valid.replace(CANDIDATE_ID, "../escape")),
    /path|candidateId/,
  );
  assert.throws(
    () => parseEvidenceUrl(valid.replace("/clip", "/arbitrary")),
    /kind/,
  );
  assert.throws(
    () => buildEvidenceUrl({
      projectId: PROJECT_ID.toUpperCase(),
      jobId: JOB_ID,
      candidateId: CANDIDATE_ID,
      kind: "image",
    }),
    /projectId/,
  );
});

test("weak-case image URLs contain only project and server-issued opaque ids", () => {
  const url = buildWeakCaseImageUrl({
    projectId: PROJECT_ID,
    sourceId: WEAK_SOURCE_ID,
    caseId: WEAK_CASE_ID,
  });
  assert.equal(
    url,
    (
      "trafficvisson-evidence://artifact/projects/" +
      `${PROJECT_ID}/reference-sources/${WEAK_SOURCE_ID}/` +
      `cases/${WEAK_CASE_ID}/image`
    ),
  );
  assert.deepEqual(parseWeakCaseImageUrl(url), {
    projectId: PROJECT_ID,
    sourceId: WEAK_SOURCE_ID,
    caseId: WEAK_CASE_ID,
  });
  assert.equal(
    weakCaseImageBridgePath(url),
    (
      `/projects/${PROJECT_ID}/reference-cases/sources/` +
      `${WEAK_SOURCE_ID}/cases/${WEAK_CASE_ID}/image`
    ),
  );
  assert.doesNotMatch(url, /(?:[A-Za-z]:\\|\/sample\/|BinData)/);
});

test("weak-case image URL rejects paths, unknown sources and ambiguous components", () => {
  const valid = buildWeakCaseImageUrl({
    projectId: PROJECT_ID,
    sourceId: WEAK_SOURCE_ID,
    caseId: WEAK_CASE_ID,
  });
  assert.throws(
    () => parseWeakCaseImageUrl(`${valid}?path=C:%5Csecret.hwpx`),
    /components/,
  );
  assert.throws(
    () =>
      buildWeakCaseImageUrl({
        projectId: PROJECT_ID,
        sourceId: "arbitrary-source",
        caseId: WEAK_CASE_ID,
      }),
    /sourceId/,
  );
  assert.throws(
    () =>
      buildWeakCaseImageUrl({
        projectId: PROJECT_ID,
        sourceId: WEAK_SOURCE_ID,
        caseId: "../../BinData/image1.bmp",
      }),
    /caseId/,
  );
});
