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
const {
  buildWeakCaseApprovalRequest,
  buildWeakCaseStandardizationRequest,
  findWeakCaseStandardizationProposal,
  parseWeakCaseStandardizationProposalList,
  parseWeakCaseStandardizationProposalResponse,
  parseWeakCaseStandardizationProviderList,
  parseWeakCaseSearchResponse,
} =
  await vite.ssrLoadModule("/src/weakCases.ts");

after(async () => {
  await vite.close();
});

const SOURCE_ID = "sample-hwpx-3q-24ffb4bc62cd86a6";
const DOCUMENT_SHA256 =
  "24ffb4bc62cd86a6c72d3a3829b8f1ddf1c824d7bf42452e7c495877a227bcb9";
const CASE_ID =
  "hwpx-weak-case-0123456789abcdef0123456789abcdef";

function sourceState(approvedCount = 0) {
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
    review_required_count: 147 - approvedCount,
    approved_reference_count: approvedCount,
    training_eligible_count: approvedCount,
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
    structure_target_summary_sha256: "c".repeat(64),
    index_manifest_sha256: "b".repeat(64),
    section_path: "Contents/section0.xml",
    section_index: 0,
    picture_target_id: `hwpx-picture-${"1".repeat(24)}`,
    page_hint: 15,
    page_hint_source: "explicit_break_not_rendered_page",
    cell_target_id: `hwpx-cell-${"2".repeat(24)}`,
    table_index: 7,
    cell_row: 2,
    cell_column: 1,
    picture_index: 12,
    sample_layout_binding_schema:
      "trafficvisson.hwpx-sample-layout.v2",
    sample_layout_binding_revision: 2,
    semantic_projection_revision:
      "audited-photo-caption-evidence-group-v2",
    sample_evidence_group_id:
      `sample-evidence-group-${"4".repeat(24)}`,
    group_photo_index: 1,
    group_photo_count: 1,
    photo_slot_id: `sample-photo-slot-${"5".repeat(24)}`,
    sample_layout_binding_sha256: "6".repeat(64),
    semantic_attestation_sha256: "7".repeat(64),
    duplicate_image_occurrence_count: 1,
    duplicate_image_review_required: false,
    duplicate_image_registry_complete: true,
    duplicate_image_case_refs: [`${SOURCE_ID}/${CASE_ID}`],
    caption: "표지판 주변 수풀",
    route: "시험고속도로",
    location: "12.3km",
    evaluation: "삭초 필요",
    opinion: "표지판 시인성 확보를 위해 수목 제거 필요",
    image_available: true,
    image_sha256: "d".repeat(64),
    image_media_type: "image/bmp",
    image_width: 640,
    image_height: 480,
    approval_id: null,
    approval_revision: null,
    approval_record_sha256: null,
    approved_issue_type: null,
    approved_action: null,
  };
}

function response() {
  return {
    schema_version: "trafficvisson.weak-case-search.v2",
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

function standardizationProposal() {
  return {
    schema_version:
      "trafficvisson.weak-case-standardization-proposal.v1",
    proposal_id: `weak-case-standardization-${"8".repeat(24)}`,
    project_id: "11111111-1111-4111-8111-111111111111",
    revision: 1,
    status: "proposed",
    reference_state: "weak_unconfirmed",
    review_required: true,
    approved_reference: false,
    training_eligible: false,
    annotation_count: 0,
    incident_count: 0,
    report_fact_count: 0,
    user_confirmed: false,
    provider: "codex",
    provider_model: "gpt-test",
    provider_usage: {
      input_tokens: 20,
      output_tokens: 4,
    },
    evidence_transport_policy: {
      mode: "text_and_structure_metadata_only",
      image_pixels_sent: false,
      image_attachments_allowed: false,
      local_paths_sent: false,
      raw_video_sent: false,
      document_bytes_sent: false,
    },
    source_snapshot: {
      quarter: 3,
      caption: "표지판 주변 수풀",
      opinion: "표지판 시인성 확보를 위해 수목 제거 필요",
      route: "시험고속도로",
      document_sha256: DOCUMENT_SHA256,
      index_manifest_sha256: "b".repeat(64),
    },
    source_snapshot_sha256: "9".repeat(64),
    sample_evidence_group_id:
      `sample-evidence-group-${"4".repeat(24)}`,
    case_refs: [`${SOURCE_ID}/${CASE_ID}`],
    photo_provenance: [
      {
        source_id: SOURCE_ID,
        case_id: CASE_ID,
        photo_slot_id: `sample-photo-slot-${"5".repeat(24)}`,
        picture_target_id: `hwpx-picture-${"1".repeat(24)}`,
        group_photo_index: 1,
        group_photo_count: 1,
        image_sha256: "d".repeat(64),
        sample_layout_binding_sha256: "6".repeat(64),
        semantic_attestation_sha256: "7".repeat(64),
        duplicate_image_occurrence_count: 1,
        duplicate_image_review_required: false,
        duplicate_image_case_refs: [`${SOURCE_ID}/${CASE_ID}`],
      },
    ],
    photo_provenance_sha256: "a".repeat(64),
    proposal: {
      issue_type: "roadside_mowing_required",
      maintenance_action: "갓길 및 비탈면 삭초",
    },
    proposal_sha256: "b".repeat(64),
    input_sha256: "c".repeat(64),
    created_at: "2026-07-30T00:00:00Z",
    updated_at: "2026-07-30T00:00:01Z",
    record_sha256: "e".repeat(64),
    idempotent_replay: false,
  };
}

function providerListResponse() {
  const provider = ({
    id,
    displayName,
    diagnosticState,
    installationState = "installed",
    authenticationState = "ready",
    networkState = "not_checked",
    state = "ready",
  }) => ({
    provider: id,
    display_name: displayName,
    state,
    authentication_owner: "official_cli",
    capabilities: {
      structured_judgment: true,
      bulk_frame_detection: false,
      report_draft: true,
      document_edit_plan: true,
      usage_information: false,
    },
    message: `${displayName} 상태`,
    diagnostic_state: diagnosticState,
    installation_state: installationState,
    authentication_state: authenticationState,
    network_state: networkState,
    version: "1.2.3",
  });
  return {
    providers: [
      provider({
        id: "codex",
        displayName: "Codex CLI",
        diagnosticState: "ready",
      }),
      provider({
        id: "claude",
        displayName: "Claude Code",
        diagnosticState: "not_connected",
        authenticationState: "signed_out",
        state: "not_connected",
      }),
    ],
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

test("weak-case parser accepts project-scoped approved references with exact provenance", () => {
  const approved = response();
  approved.capability_state = "approved_reference_available";
  approved.source_states = [sourceState(1)];
  approved.cases[0] = {
    ...weakCase(),
    reference_state: "approved_reference",
    review_required: false,
    approved_reference: true,
    training_eligible: true,
    approval_id: `approved-reference-${"3".repeat(24)}`,
    approval_revision: 1,
    approval_record_sha256: "e".repeat(64),
    approved_issue_type: "roadside_mowing_required",
    approved_action: "표지판 시인성 확보를 위한 선택적 삭초",
  };
  approved.approved_reference_count = 1;
  approved.training_eligible_count = 1;

  const parsed = parseWeakCaseSearchResponse(approved, {
    issueType: "roadside_mowing_required",
    requestedLimit: 5,
  });
  assert.equal(
    parsed.capabilityState,
    "approved_reference_available",
  );
  assert.equal(parsed.approvedReferenceCount, 1);
  assert.equal(parsed.trainingEligibleCount, 1);
  assert.equal(parsed.cases[0].approvedReference, true);
  assert.equal(
    parsed.cases[0].approvedAction,
    "표지판 시인성 확보를 위한 선택적 삭초",
  );
  assert.equal(parsed.cases[0].approvalRevision, 1);
});

test("weak-case approval request preserves audited hashes without creating current-video truth", () => {
  const parsed = parseWeakCaseSearchResponse(response(), {
    issueType: "roadside_mowing_required",
    requestedLimit: 5,
  });
  const request = buildWeakCaseApprovalRequest({
    projectId: "11111111-1111-4111-8111-111111111111",
    reference: parsed.cases[0],
    issueType: "roadside_mowing_required",
    maintenanceAction: "표지판 시인성 확보를 위한 선택적 삭초",
  });
  assert.deepEqual(request, {
    projectId: "11111111-1111-4111-8111-111111111111",
    sourceId: SOURCE_ID,
    caseId: CASE_ID,
    expectedDocumentSha256: DOCUMENT_SHA256,
    expectedIndexManifestSha256: "b".repeat(64),
    expectedImageSha256: "d".repeat(64),
    issueType: "roadside_mowing_required",
    maintenanceAction: "표지판 시인성 확보를 위한 선택적 삭초",
    caption: "표지판 주변 수풀",
    route: "시험고속도로",
    location: "12.3km",
    evaluation: "삭초 필요",
    opinion: "표지판 시인성 확보를 위해 수목 제거 필요",
    duplicateImageReviewConfirmed: false,
    userConfirmed: true,
  });
  assert.equal("annotationCreated" in request, false);
  assert.equal("incidentCreated" in request, false);
});

test("weak-case standardization request is metadata-consent and hash bound", () => {
  const parsed = parseWeakCaseSearchResponse(response(), {
    issueType: "roadside_mowing_required",
    requestedLimit: 5,
  });
  const request = buildWeakCaseStandardizationRequest({
    projectId: "11111111-1111-4111-8111-111111111111",
    reference: parsed.cases[0],
    provider: "codex",
    metadataTransmissionConfirmed: true,
  });
  assert.deepEqual(request, {
    projectId: "11111111-1111-4111-8111-111111111111",
    sourceId: SOURCE_ID,
    caseId: CASE_ID,
    provider: "codex",
    expectedDocumentSha256: DOCUMENT_SHA256,
    expectedIndexManifestSha256: "b".repeat(64),
    expectedImageSha256: "d".repeat(64),
    metadataTransmissionConfirmed: true,
  });
  assert.equal("caption" in request, false);
  assert.equal("opinion" in request, false);
  assert.equal("imagePath" in request, false);
  assert.throws(
    () =>
      buildWeakCaseStandardizationRequest({
        projectId: "11111111-1111-4111-8111-111111111111",
        reference: parsed.cases[0],
        provider: "codex",
        metadataTransmissionConfirmed: false,
      }),
    /전송 범위/,
  );
});

test("weak-case standardization proposal stays review-only and is bound to the exact sample photo", () => {
  const reference = parseWeakCaseSearchResponse(response(), {
    issueType: "roadside_mowing_required",
    requestedLimit: 5,
  }).cases[0];
  const proposal = parseWeakCaseStandardizationProposalResponse(
    standardizationProposal(),
    {
      projectId: "11111111-1111-4111-8111-111111111111",
      reference,
    },
  );
  assert.equal(
    proposal.suggestion.issueType,
    "roadside_mowing_required",
  );
  assert.equal(proposal.suggestion.maintenanceAction, "갓길 및 비탈면 삭초");
  assert.equal(proposal.annotationCount, 0);
  assert.equal(proposal.incidentCount, 0);
  assert.equal(proposal.reportFactCount, 0);
  assert.deepEqual(proposal.caseRefs, [`${SOURCE_ID}/${CASE_ID}`]);
  assert.equal(
    findWeakCaseStandardizationProposal([proposal], reference)?.proposalId,
    proposal.proposalId,
  );

  const list = parseWeakCaseStandardizationProposalList(
    {
      schema_version:
        "trafficvisson.weak-case-standardization-proposal-list.v1",
      project_id: "11111111-1111-4111-8111-111111111111",
      proposal_count: 1,
      evidence_event_count: 1,
      approved_reference_count: 0,
      training_eligible_count: 0,
      annotation_count: 0,
      incident_count: 0,
      report_fact_count: 0,
      records: [standardizationProposal()],
    },
    { projectId: "11111111-1111-4111-8111-111111111111" },
  );
  assert.equal(list.proposalCount, 1);
  assert.equal(list.approvedReferenceCount, 0);
  assert.equal(list.trainingEligibleCount, 0);
});

test("weak-case standardization parser rejects path injection, truth promotion and photo drift", () => {
  const reference = parseWeakCaseSearchResponse(response(), {
    issueType: "roadside_mowing_required",
    requestedLimit: 5,
  }).cases[0];
  const injected = standardizationProposal();
  injected.image_path = "C:\\private\\sample.bmp";
  assert.throws(
    () =>
      parseWeakCaseStandardizationProposalResponse(injected, {
        projectId: "11111111-1111-4111-8111-111111111111",
        reference,
      }),
    /provenance/,
  );

  const promoted = standardizationProposal();
  promoted.annotation_count = 1;
  assert.throws(
    () =>
      parseWeakCaseStandardizationProposalResponse(promoted, {
        projectId: "11111111-1111-4111-8111-111111111111",
        reference,
      }),
    /provenance/,
  );

  const drifted = standardizationProposal();
  drifted.photo_provenance[0].image_sha256 = "f".repeat(64);
  assert.throws(
    () =>
      parseWeakCaseStandardizationProposalResponse(drifted, {
        projectId: "11111111-1111-4111-8111-111111111111",
        reference,
      }),
    /provenance/,
  );
});

test("weak-case standardization provider parser distinguishes login, install, expiry and network states", () => {
  const responseValue = providerListResponse();
  let parsed = parseWeakCaseStandardizationProviderList(responseValue);
  assert.equal(
    parsed.find(({ provider }) => provider === "codex")?.availability,
    "ready",
  );
  assert.equal(
    parsed.find(({ provider }) => provider === "claude")?.availability,
    "not_logged_in",
  );

  responseValue.providers[1] = {
    ...responseValue.providers[1],
    state: "unavailable",
    diagnostic_state: "not_installed",
    installation_state: "not_installed",
    authentication_state: "unknown",
  };
  parsed = parseWeakCaseStandardizationProviderList(responseValue);
  assert.equal(parsed[1].availability, "not_installed");

  responseValue.providers[1] = {
    ...responseValue.providers[1],
    diagnostic_state: "expired",
    installation_state: "installed",
    authentication_state: "expired",
  };
  parsed = parseWeakCaseStandardizationProviderList(responseValue);
  assert.equal(parsed[1].availability, "expired");

  responseValue.providers[1] = {
    ...responseValue.providers[1],
    diagnostic_state: "network_unavailable",
    authentication_state: "unknown",
    network_state: "offline",
  };
  parsed = parseWeakCaseStandardizationProviderList(responseValue);
  assert.equal(parsed[1].availability, "network_unavailable");
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
  crossSourceCase.cases[0].duplicate_image_case_refs = [
    `sample-hwpx-4q-bbdd61a6ef192a95/${CASE_ID}`,
  ];
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
  const dialogSource = fs.readFileSync(
    new URL(
      "./components/WeakCaseStandardizationDialog.tsx",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(source, /referenceCases\s*;/);
  assert.match(source, /\.referenceCases/);
  assert.match(source, /\.search\(\{/);
  assert.match(source, /\.approve\(request\)/);
  assert.match(source, /limit:\s*5/);
  assert.match(source, /buildWeakCaseImageUrl/);
  assert.match(source, /3·4분기 유사 참고/);
  assert.match(source, /이 유형으로 사례 승인/);
  assert.match(source, /\.proposeStandardization\(request\)/);
  assert.match(source, /\.standardizationProposals\(\{/);
  assert.match(source, /approveWeakCaseStandardizationProposal/);
  assert.match(
    source,
    /Annotation·Incident는 생성하지 않았습니다/,
  );
  assert.match(dialogSource, /변경하지 않는 원본/);
  assert.match(dialogSource, /LLM 표준화 제안/);
  assert.match(dialogSource, /사진 픽셀 없이/);
  assert.match(dialogSource, /사용자 확인 후 승인 사례로 승격/);
  assert.match(dialogSource, /CLI 미설치/);
  assert.match(dialogSource, /로그인 필요/);
  assert.match(dialogSource, /로그인 만료/);
  assert.match(dialogSource, /네트워크 연결 실패/);
  assert.doesNotMatch(
    dialogSource,
    /imagePath|source_ref|auth(?:entication)?[_A-Z]?token|photoBytes/,
  );
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
