import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after, beforeEach } from "node:test";
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
const reportSession = await vite.ssrLoadModule("/src/reportSessionState.ts");
const exportResults = await vite.ssrLoadModule("/src/exportResults.ts");
const exportScreen = await vite.ssrLoadModule("/src/screens/ExportScreen.tsx");
const reportScreen = await vite.ssrLoadModule("/src/screens/ReportScreen.tsx");
const reportSessionSource = await readFile(
  new URL("./reportSessionState.ts", import.meta.url),
  "utf8",
);
const exportScreenSource = await readFile(
  new URL("./screens/ExportScreen.tsx", import.meta.url),
  "utf8",
);
const reportScreenSource = await readFile(
  new URL("./screens/ReportScreen.tsx", import.meta.url),
  "utf8",
);

after(async () => {
  await vite.close();
});

beforeEach(() => {
  reportSession.resetReportSessionStateForTests();
});

function generatedReport() {
  return {
    generationId: "66666666-6666-4666-8666-666666666666",
    provider: "codex",
    state: "generated_preview_required",
    draft: {
      title: "도로 점검 보고서",
      sections: [
        {
          heading: "점검 결과",
          body: "승인 사건 한 건을 확인했습니다.",
          incidentIds: ["77777777-7777-4777-8777-777777777777"],
        },
      ],
    },
    plan: {
      schemaVersion: "1.0",
      planId: "88888888-8888-4888-8888-888888888888",
      documentSnapshotId: "selection:opaque",
      operations: [
        {
          operationId: "op-1",
          type: "fill_text",
          target: "bookmark:inspection_summary",
          value: "승인 사건 한 건",
          sourceRef: "incident:approved",
          stylePolicy: "preserve",
          preview: "조사 결과",
        },
      ],
    },
    transmittedFields: ["approved_incident_json"],
  };
}

test("report workflow survives navigation in memory for the same analysis", () => {
  const scope = "project-1:job-1";
  reportSession.prepareReportSessionScope(scope);
  reportSession.setReportSessionField("provider", "codex");
  reportSession.setReportSessionField("consent", true);
  reportSession.setReportSessionField("generation", generatedReport());
  reportSession.setReportSessionField("operationStates", {
    "op-1": "accepted",
  });
  reportSession.setReportSessionField("documentSelection", {
    selectionId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    file: { name: "점검 양식.hwpx", extension: ".hwpx" },
  });
  reportSession.setReportSessionField("workflow", "applied");
  reportSession.setReportSessionField("applyResult", {
    mode: "applied",
    rollback_token: "a".repeat(32),
    output_file_name: "점검 양식_TrafficSuperVision.hwpx",
    size_bytes: 1234,
    sha256: "b".repeat(64),
    readback: {
      observed_characters: 100,
      truncated: false,
      paragraph_count: 3,
      table_count: 1,
      picture_count: 0,
      field_names: [],
      bookmark_names: ["inspection_summary"],
    },
    message: "적용 완료",
  });

  reportSession.prepareReportSessionScope(scope);
  const restored = reportSession.getReportSessionState();
  assert.equal(restored.provider, "codex");
  assert.equal(restored.consent, true);
  assert.equal(restored.generation.draft.title, "도로 점검 보고서");
  assert.deepEqual(restored.operationStates, { "op-1": "accepted" });
  assert.equal(restored.documentSelection.file.name, "점검 양식.hwpx");
  assert.equal(restored.workflow, "applied");
  assert.equal(restored.applyResult.rollback_token, "a".repeat(32));
});

test("new analysis or renderer restart clears document and COM one-shot state", () => {
  reportSession.prepareReportSessionScope("project-1:job-1");
  reportSession.setReportSessionField("provider", "codex");
  reportSession.setReportSessionField("consent", true);
  reportSession.setReportSessionField("generation", generatedReport());
  reportSession.setReportSessionField("documentSelection", {
    selectionId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    file: { name: "점검 양식.hwpx", extension: ".hwpx" },
  });
  reportSession.setReportSessionField("applyResult", {
    mode: "applied",
    rollback_token: "a".repeat(32),
    output_file_name: "result.hwpx",
    size_bytes: 10,
    sha256: "b".repeat(64),
    readback: {
      observed_characters: 1,
      truncated: false,
      paragraph_count: 1,
      table_count: 0,
      picture_count: 0,
      field_names: [],
      bookmark_names: [],
    },
    message: "완료",
  });

  reportSession.prepareReportSessionScope("project-1:job-2");
  const changedAnalysis = reportSession.getReportSessionState();
  assert.equal(changedAnalysis.provider, "codex");
  assert.equal(changedAnalysis.consent, true);
  assert.equal(changedAnalysis.generation, null);
  assert.equal(changedAnalysis.documentSelection, null);
  assert.equal(changedAnalysis.applyResult, null);
  assert.equal(changedAnalysis.workflow, "idle");

  reportSession.resetReportSessionStateForTests();
  const restarted = reportSession.getReportSessionState();
  assert.equal(restarted.provider, "claude");
  assert.equal(restarted.consent, false);
  assert.equal(restarted.documentSelection, null);
  assert.equal(restarted.applyResult, null);
  assert.doesNotMatch(
    reportSessionSource,
    /localStorage|sessionStorage|indexedDB/,
  );
});

test("HWP apply forwards only the server preview approval and burns local preview on failure", () => {
  assert.match(
    reportScreenSource,
    /approvalToken:\s*previewResult\.approval_token/,
  );
  assert.match(
    reportScreenSource,
    /\^\[a-f0-9\]\{64\}\$[\s\S]*approval_expires_at[\s\S]*document_sha256[\s\S]*plan_sha256[\s\S]*target_summary/,
  );
  assert.match(
    reportScreenSource,
    /catch \(error\) \{\s*if \(!reportOperationIsCurrent\(request\)\) return;\s*setPreviewedPlan\(null\);\s*setPreviewResult\(null\);\s*setWorkflow\("error"\);[\s\S]*한\/글 문서에 편집 계획을 적용하지 못했습니다/,
  );
});

test("report async completions are bound to one operation nonce and project session", () => {
  const scopeA = "project-a:job-a";
  const scopeB = "project-b:job-b";
  assert.equal(
    reportScreen.isCurrentReportOperation(7, 7, scopeA, scopeA),
    true,
  );
  assert.equal(
    reportScreen.isCurrentReportOperation(7, 8, scopeA, scopeA),
    false,
    "a newer operation in the same session invalidates an older response",
  );
  assert.equal(
    reportScreen.isCurrentReportOperation(7, 7, scopeA, scopeB),
    false,
    "a response from project A cannot update project B",
  );
  assert.equal(
    reportScreen.isCurrentReportOperation(7, 9, scopeA, scopeA),
    false,
    "A to B to A still rejects the first A nonce",
  );
  assert.match(
    reportScreenSource,
    /latestReportScopeKeyRef\.current !== reportScopeKey[\s\S]*activeReportOperationRef\.current \+= 1/,
  );
  assert.match(
    reportScreenSource,
    /return \(\) => \{[\s\S]*activeReportOperationRef\.current \+= 1;[\s\S]*\};/,
    "unmounting the report screen must invalidate same-scope callbacks",
  );
  assert.ok(
    (reportScreenSource.match(/!reportOperationIsCurrent\(request\)/g) ?? [])
      .length >= 9,
    "document selection plus generate, preview, apply, and rollback success/error paths must be guarded",
  );
});

test("document fit zoom is bounded and derived from the visible sketch width", () => {
  assert.equal(reportScreen.calculateDocumentFitZoom(712, 32), 100);
  assert.equal(reportScreen.calculateDocumentFitZoom(120, 32), 40);
  assert.equal(reportScreen.calculateDocumentFitZoom(1400, 32), 150);
});

function completedExportResponse() {
  const artifacts = exportResults.EXPORT_ARTIFACTS.map((item, index) => ({
    artifact: item.artifact,
    file_name: item.fileName,
    row_count: index === 5 ? 0 : index + 1,
    size_bytes: 1000 + index,
    sha256: (index + 1).toString(16).repeat(64),
  }));
  return {
    status: "completed",
    export_id: "99999999-9999-4999-8999-999999999999",
    archive_file_name: "trafficsupervision-export.zip",
    archive_size_bytes: 123456,
    archive_sha256: "f".repeat(64),
    artifact_count: 14,
    evidence_incident_count: 2,
    evidence_file_count: 4,
    evidence_size_bytes: 654321,
    artifacts,
  };
}

test("export rows remain pending before export and show returned metadata after", () => {
  assert.match(
    exportScreenSource,
    /analysisJobId:\s*runtime\.analysisSummary\.jobId/,
  );
  const pending = exportResults.buildExportDisplayRows(null);
  assert.equal(pending.length, 9);
  assert.ok(
    pending.every(
      (row) =>
        row.rowCount === null &&
        row.sizeBytes === null &&
        row.sha256 === null,
    ),
  );

  const completed = exportResults.parseCompletedExport(
    completedExportResponse(),
  );
  const rows = exportResults.buildExportDisplayRows(completed);
  assert.equal(rows.length, 9);
  assert.deepEqual(rows[0], {
    artifact: "raw_detections",
    fileName: "raw_detections.csv",
    description: "AI 원시 후보값",
    rowCount: 1,
    sizeBytes: 1000,
    sha256: "1".repeat(64),
  });
  assert.equal(rows[5].fileName, "route_points.csv");
  assert.equal(rows[5].rowCount, 0);
  assert.equal(rows[8].sha256, "9".repeat(64));
  assert.equal(completed.evidenceIncidentCount, 2);
  assert.equal(completed.evidenceFileCount, 4);
  assert.equal(completed.evidenceSizeBytes, 654321);
  assert.equal(completed.artifactCount, 14);
  assert.match(exportScreenSource, /승인 사건 근거 JPEG·MP4 포함/);
  assert.match(
    exportScreenSource,
    /currentCompletedExport\.evidenceIncidentCount/,
  );
  assert.match(exportScreenSource, /currentCompletedExport\.evidenceFileCount/);
  assert.match(exportScreenSource, /currentCompletedExport\.evidenceSizeBytes/);
  assert.match(exportScreenSource, /원본 전체 영상 제외/);
});

test("export workflow scopes local state and asynchronous completion to one analysis", () => {
  const projectA = "11111111-1111-4111-8111-111111111111";
  const projectB = "22222222-2222-4222-8222-222222222222";
  const jobA = "33333333-3333-4333-8333-333333333333";
  const jobB = "44444444-4444-4444-8444-444444444444";
  const scopeA = exportScreen.buildExportScopeKey(projectA, jobA);

  assert.equal(scopeA, `${projectA}:${jobA}`);
  assert.notEqual(scopeA, exportScreen.buildExportScopeKey(projectA, jobB));
  assert.notEqual(scopeA, exportScreen.buildExportScopeKey(projectB, jobA));
  assert.equal(
    exportScreen.isCurrentExportOperation(7, 7, scopeA, scopeA),
    true,
  );
  assert.equal(
    exportScreen.isCurrentExportOperation(
      7,
      7,
      scopeA,
      exportScreen.buildExportScopeKey(projectB, jobB),
    ),
    false,
  );
  assert.equal(
    exportScreen.isCurrentExportOperation(7, 8, scopeA, scopeA),
    false,
  );

  assert.match(
    exportScreenSource,
    /const stateBelongsToCurrentScope = stateScopeKey === exportScopeKey;/,
  );
  assert.match(
    exportScreenSource,
    /activeExportOperationRef\.current \+= 1;[\s\S]*setStateScopeKey\(exportScopeKey\);[\s\S]*setCompletedExport\(null\);[\s\S]*setDestination\(null\);[\s\S]*\[exportScopeKey\]/,
  );
  assert.ok(
    (exportScreenSource.match(/!isCurrentExportOperation\(/g) ?? []).length >=
      2,
    "both resolved and rejected in-flight exports must be scope guarded",
  );
  assert.equal(
    (
      exportScreenSource.match(
        /latestScopeKeyRef\.current !== requestScopeKey/g,
      ) ?? []
    ).length,
    2,
    "both resolved and rejected directory picker results must be scope guarded",
  );
});

test("export consumes a one-shot destination before IPC and requires reselection after failure", () => {
  const consumeIndex = exportScreenSource.indexOf(
    '? { ...current, selectionId: "" }',
  );
  const requestIndex = exportScreenSource.indexOf(
    "await window.trafficSuperVision.exports.create",
  );
  assert.ok(consumeIndex >= 0 && requestIndex > consumeIndex);
  assert.match(
    exportScreenSource,
    /저장 폴더를 다시 선택한 뒤 재시도하세요/,
  );
  assert.match(
    exportScreenSource,
    /currentExportState === "done"[\s\S]*`\$\{currentCompletedDestinationName\}에 생성 완료/,
  );
  assert.match(
    exportScreenSource,
    /currentExportState === "error"[\s\S]*생성 실패 · 다시 선택 필요/,
  );
});

test("export response rejects missing or unverified artifact metadata", () => {
  const missing = completedExportResponse();
  missing.artifacts.pop();
  assert.throws(
    () => exportResults.parseCompletedExport(missing),
    /전체 추적 패키지/,
  );

  const noHash = completedExportResponse();
  noHash.artifacts[0].sha256 = "";
  assert.throws(
    () => exportResults.parseCompletedExport(noHash),
    /산출물 행·크기·SHA-256/,
  );

  const renamed = completedExportResponse();
  renamed.artifacts[0].file_name = "other.csv";
  assert.throws(
    () => exportResults.parseCompletedExport(renamed),
    /산출물 이름/,
  );

  const inconsistentCount = completedExportResponse();
  inconsistentCount.artifact_count -= 1;
  assert.throws(
    () => exportResults.parseCompletedExport(inconsistentCount),
    /전체 추적 패키지/,
  );

  const incompleteIncidentPair = completedExportResponse();
  incompleteIncidentPair.evidence_file_count -= 1;
  incompleteIncidentPair.artifact_count -= 1;
  assert.throws(
    () => exportResults.parseCompletedExport(incompleteIncidentPair),
    /전체 추적 패키지/,
  );

  for (const [field, value] of [
    ["evidence_incident_count", 513],
    ["evidence_file_count", 1025],
    ["evidence_size_bytes", 8 * 1024 * 1024 * 1024 + 1],
  ]) {
    const outOfBounds = completedExportResponse();
    outOfBounds[field] = value;
    assert.throws(
      () => exportResults.parseCompletedExport(outOfBounds),
      /완료 응답 형식/,
    );
  }
});
