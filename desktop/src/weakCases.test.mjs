import assert from "node:assert/strict";
import fs from "node:fs";
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
const { parseWeakCaseSearchResponse } =
  await vite.ssrLoadModule("/src/weakCases.ts");

after(async () => {
  await vite.close();
});

const SOURCE_ID = "sample-hwpx-3q-24ffb4bc62cd86a6";
const DOCUMENT_SHA256 =
  "24ffb4bc62cd86a6c72d3a3829b8f1ddf1c824d7bf42452e7c495877a227bcb9";
const CASE_ID =
  "hwpx-weak-case-0123456789abcdef0123456789abcdef";

function sourceState() {
  return {
    source_id: SOURCE_ID,
    document_name: "3분기 점검 내용 정리.hwpx",
    document_sha256: DOCUMENT_SHA256,
    authorization_purpose:
      "sample_hwpx_read_only_weak_case_index",
    quarter: 3,
    status: "ready",
    case_count: 147,
    resolved_case_count: 147,
    review_required_count: 147,
    approved_reference_count: 0,
    training_eligible_count: 0,
    annotation_count: 0,
    incident_count: 0,
    index_manifest_sha256: "b".repeat(64),
  };
}

function weakCase() {
  return {
    source_id: SOURCE_ID,
    case_id: CASE_ID,
    rank: 1,
    similarity_score: 33,
    matched_terms: ["삭초", "수풀"],
    reference_state: "weak_unconfirmed",
    review_required: true,
    approved_reference: false,
    training_eligible: false,
    annotation_created: false,
    incident_created: false,
    quarter: 3,
    document_name: "3분기 점검 내용 정리.hwpx",
    document_sha256: DOCUMENT_SHA256,
    page_hint: 15,
    page_hint_source: "explicit_break_not_rendered_page",
    table_index: 7,
    cell_row: 2,
    cell_column: 1,
    picture_index: 12,
    caption: "표지판 주변 수풀",
    route: "시험고속도로",
    location: "12.3km",
    evaluation: "삭초 필요",
    opinion: "표지판 시인성 확보를 위해 수목 제거 필요",
    image_available: true,
    image_media_type: "image/bmp",
    image_width: 640,
    image_height: 480,
  };
}

function response() {
  return {
    schema_version: "trafficvisson.weak-case-search.v1",
    search_mode: "bounded_text_and_structure_metadata",
    semantic_image_embedding_used: false,
    capability_state: "weak_reference_only",
    issue_type: "roadside_mowing_required",
    query_text: "표지판 주변 수풀 삭초",
    requested_limit: 5,
    result_count: 1,
    cases: [weakCase()],
    source_states: [sourceState()],
    review_required_before_promotion: true,
    approved_reference_count: 0,
    annotation_count: 0,
    incident_count: 0,
    training_eligible_count: 0,
  };
}

test("weak-case parser keeps sample matches review-only and path-free", () => {
  const parsed = parseWeakCaseSearchResponse(response(), {
    issueType: "roadside_mowing_required",
    requestedLimit: 5,
  });
  assert.equal(parsed.capabilityState, "weak_reference_only");
  assert.equal(parsed.cases.length, 1);
  assert.equal(parsed.cases[0].caseId, CASE_ID);
  assert.equal(parsed.cases[0].opinion, weakCase().opinion);
  assert.equal(parsed.approvedReferenceCount, 0);
  assert.equal(parsed.annotationCount, 0);
  assert.equal(parsed.incidentCount, 0);
  assert.equal(parsed.trainingEligibleCount, 0);
  assert.doesNotMatch(JSON.stringify(parsed), /BinData|[A-Za-z]:\\/);
});

test("weak-case parser rejects promotion, path injection and identity drift", () => {
  const promoted = response();
  promoted.cases[0].approved_reference = true;
  assert.throws(
    () =>
      parseWeakCaseSearchResponse(promoted, {
        issueType: "roadside_mowing_required",
        requestedLimit: 5,
      }),
    /안전 상태/,
  );

  const injected = response();
  injected.cases[0].package_path = "BinData/image1.bmp";
  assert.throws(
    () =>
      parseWeakCaseSearchResponse(injected, {
        issueType: "roadside_mowing_required",
        requestedLimit: 5,
      }),
    /안전 상태/,
  );

  const sourceDrift = response();
  sourceDrift.source_states[0].document_sha256 = "a".repeat(64);
  assert.throws(
    () =>
      parseWeakCaseSearchResponse(sourceDrift, {
        issueType: "roadside_mowing_required",
        requestedLimit: 5,
      }),
    /안전 상태/,
  );

  const crossSourceCase = response();
  crossSourceCase.cases[0].source_id =
    "sample-hwpx-4q-bbdd61a6ef192a95";
  crossSourceCase.cases[0].quarter = 4;
  crossSourceCase.cases[0].document_name =
    "4분기 점검 내용 정리.hwpx";
  crossSourceCase.cases[0].document_sha256 =
    "bbdd61a6ef192a957a5e9480b0c2eedbfe956dd184abd5d76f59474254941539";
  assert.throws(
    () =>
      parseWeakCaseSearchResponse(crossSourceCase, {
        issueType: "roadside_mowing_required",
        requestedLimit: 5,
      }),
    /상태가 일치/,
  );

  assert.throws(
    () =>
      parseWeakCaseSearchResponse(response(), {
        issueType: "pothole",
        requestedLimit: 5,
      }),
    /응답을 검증/,
  );
});

test("labeling workbench presents 3-5 bounded sample thumbnails without a document scroll owner", () => {
  const source = fs.readFileSync(
    new URL("./screens/LabelingScreen.tsx", import.meta.url),
    "utf8",
  );
  const compactCss = fs.readFileSync(
    new URL(
      "./styles-compact-analysis-labeling.css",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(source, /referenceCases\s*;/);
  assert.match(source, /\.referenceCases/);
  assert.match(source, /\.search\(\{/);
  assert.match(source, /limit:\s*5/);
  assert.match(source, /buildWeakCaseImageUrl/);
  assert.match(source, /3·4분기 유사 참고/);
  assert.match(source, /확정 아님/);
  assert.match(
    compactCss,
    /@media \(min-width: 961px\) and \(max-height: 820px\)[\s\S]*\.screen--labeling \.weak-case-reference\s*\{[^}]*max-height:\s*86px/s,
  );
  assert.match(
    compactCss,
    /\.screen--labeling \.weak-case-strip\s*\{[^}]*grid-template-columns:\s*repeat\(5,\s*minmax\(0,\s*1fr\)\)/s,
  );
  assert.match(
    compactCss,
    /\.screen--labeling \.labeling-layout\s*\{[^}]*overflow:\s*hidden/s,
  );
  assert.doesNotMatch(
    source,
    /referenceCases[\s\S]{0,300}(?:sourcePath|documentPath|packagePath)/,
  );
});
