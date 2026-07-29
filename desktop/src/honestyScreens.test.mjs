import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const importSource = await readFile(
  new URL("./screens/ImportScreen.tsx", import.meta.url),
  "utf8",
);
const analysisSource = await readFile(
  new URL("./screens/AnalysisScreen.tsx", import.meta.url),
  "utf8",
);
const reviewSource = await readFile(
  new URL("./screens/ReviewScreen.tsx", import.meta.url),
  "utf8",
);
const historySource = await readFile(
  new URL("./screens/HistoryScreen.tsx", import.meta.url),
  "utf8",
);
const historyMapSource = await readFile(
  new URL("./components/HistoryMap.tsx", import.meta.url),
  "utf8",
);
const candidateSelectionSource = await readFile(
  new URL("./candidateSelection.ts", import.meta.url),
  "utf8",
);
const persistentIncidentsSource = await readFile(
  new URL("./persistentIncidents.ts", import.meta.url),
  "utf8",
);
const labelingSource = await readFile(
  new URL("./screens/LabelingScreen.tsx", import.meta.url),
  "utf8",
);
const labelingDecisionSource = await readFile(
  new URL("./labelingDecision.ts", import.meta.url),
  "utf8",
);
const experimentalProposalSource = await readFile(
  new URL("./experimentalProposals.ts", import.meta.url),
  "utf8",
);
const reportSource = await readFile(
  new URL("./screens/ReportScreen.tsx", import.meta.url),
  "utf8",
);

test("ImportScreen renders every discovered file without browser verification fixtures", () => {
  assert.doesNotMatch(importSource, /candidates\.slice\(/);
  assert.match(importSource, /candidates\.map\(/);
  assert.doesNotMatch(importSource, /VERIFIED_PREVIEW_SHA256/);
  assert.doesNotMatch(importSource, /preview-only|browser-preview/);
  assert.doesNotMatch(importSource, /112_976_270_871|file_count:\s*51/);
  assert.doesNotMatch(importSource, /importFiles/);
});

test("ImportScreen invalidates changed input and marks URL as inspect-only", () => {
  assert.match(importSource, /inspectionRun\.current \+= 1/);
  assert.doesNotMatch(importSource, /hashJob|cancelStaleHash/);
  assert.match(importSource, /trafficSuperVision\.pipeline\.start/);
  assert.match(importSource, /run !== inspectionRun\.current/g);
  assert.match(importSource, /URL 기능은 검사 전용/);
  assert.match(importSource, /URL 다운로드, 영상 저장, 프로젝트 등록은 아직 연결되지 않았습니다/);
  assert.match(importSource, /trafficSuperVision\.imports\.inspect/);
  assert.match(importSource, /inspected_source_id/);
  assert.match(importSource, /trafficSuperVision\.media\.registerLocalSelection/);
});

test("AnalysisScreen contains no fabricated pipeline, GPU, or checkpoint claims", () => {
  assert.doesNotMatch(analysisSource, /pipelineSteps|15-STAGE|LOCAL GPU/);
  assert.doesNotMatch(
    analysisSource,
    /VRAM|GPU 사용률|체크포인트|자동 회복|batch/,
  );
  assert.match(analysisSource, /candidateExtractionSteps\.map/);
  assert.match(analysisSource, /savedJobStatusLabel/);
  assert.match(analysisSource, /runtimeStatusLabel/);
  assert.match(analysisSource, /장면 변화·선명도 기반 후보 추출/);
  assert.match(
    analysisSource,
    /runtime\.analysisSummary[\s\S]*"브리지 단계 · 완료"[\s\S]*"작업 시작 전"/,
  );
});

test("ImportScreen uses user-facing copy for the renderer security boundary", () => {
  assert.match(importSource, /선택 파일을 안전한 미디어 입력으로 등록하는 중/);
  assert.doesNotMatch(importSource, /renderer 파일 권한 없음/);
  assert.match(importSource, /대상 · \{compactSourceTail\(source\)\}/);
  assert.match(importSource, /title=\{source \|\| undefined\}/);
});

test("ReviewScreen is read-only over real candidates and delegates writes to labeling", () => {
  assert.match(reviewSource, /useRuntimeState/);
  assert.match(reviewSource, /buildCandidateEvidenceUrl/);
  assert.match(reviewSource, /candidateQuestions/);
  assert.match(
    reviewSource,
    /trafficSuperVision\.incidents[\s\S]*analysisJobId:\s*summary\.jobId/,
  );
  assert.match(reviewSource, /incident\?\.incidentId/);
  assert.match(reviewSource, /persistentIssueLabel\(incident\.issueType\)/);
  assert.match(reviewSource, /incident\?\.maintenanceAction/);
  assert.match(reviewSource, /incident\?\.ocrText/);
  assert.match(reviewSource, /질문형 라벨링에서 검토/);
  assert.doesNotMatch(reviewSource, /import\s*\{\s*incidents\s*\}/);
  assert.doesNotMatch(reviewSource, /trafficvisson\.review-decisions/);
  assert.doesNotMatch(reviewSource, /후보 승인.*onClick|후보 반려.*onClick/s);
  assert.match(
    reviewSource,
    /if \(!summary \|\| !candidate\)[\s\S]*CANDIDATE REVIEW[\s\S]*사건 검토/,
  );
});

test("HistoryScreen uses evidence clips and candidate timestamps without a synthetic clock", () => {
  assert.match(historySource, /buildCandidateEvidenceUrl/);
  assert.match(historySource, /representativeTimestampSeconds/);
  assert.match(historySource, /<video/);
  assert.match(
    historySource,
    /trafficSuperVision\.incidents[\s\S]*analysisJobId:\s*summary\.jobId/,
  );
  assert.match(historySource, /incidentsByCandidateId/);
  assert.match(historySource, /selectedIncident\.incidentId/);
  assert.match(historySource, /selectedIncident\.maintenanceAction/);
  assert.match(historySource, /selectedIncident\.ocrText/);
  assert.doesNotMatch(historySource, /DURATION_MS|setInterval|routeSegments/);
  assert.doesNotMatch(historySource, /import\s*\{\s*incidents/);
  assert.match(
    historySource,
    /if \(!summary \|\| !selectedCandidate\)[\s\S]*screen--history[\s\S]*SYNCHRONIZED EVIDENCE/,
  );
});

test("persistent incident responses are rejected across project and job boundaries", () => {
  assert.match(
    persistentIncidentsSource,
    /responseProject !== expectedProject/,
  );
  assert.match(
    persistentIncidentsSource,
    /projectId !== expectedProject \|\| analysisJobId !== expectedJob/,
  );
  assert.match(persistentIncidentsSource, /row\.status !== "active"/);
  assert.match(
    persistentIncidentsSource,
    /seenIncidentIds\.has\(incidentId\)[\s\S]*seenQuestionIds\.has\(questionId\)[\s\S]*seenCandidateIds\.has\(row\.candidate_id\)/,
  );
});

test("HistoryMap draws only an attached GPX route and keeps the honest empty state", () => {
  assert.match(historyMapSource, /GPS 자료 없음/);
  assert.match(historyMapSource, /경로선,/);
  assert.match(historyMapSource, /from "maplibre-gl"/);
  assert.match(historyMapSource, /if \(track\)/);
  assert.match(historyMapSource, /routeFeature\(track\.routePoints\)/);
  assert.match(historyMapSource, /interpolateGpxPosition/);
  assert.match(historyMapSource, /gapFromPrevious/);
  assert.doesNotMatch(historyMapSource, /37\.646|127\.059|setInterval/);
});

test("cross-screen candidate selection is scoped by project and job", () => {
  assert.match(
    candidateSelectionSource,
    /trafficvisson:selected-candidate:\$\{projectId\}:\$\{jobId\}/,
  );
  assert.match(candidateSelectionSource, /candidate\.candidateId === candidateId/);
});

test("LabelingScreen keeps experimental proposals review-only and understandable", () => {
  assert.match(
    labelingSource,
    /className="experimental-proposal__summary"[\s\S]*question\.scoreText/,
  );
  assert.match(labelingSource, /확률 또는 확정 판정이 아님|scoreText/);
  assert.match(labelingSource, /아직 저장되지 않음/);
  assert.match(labelingSource, /typeCode === "UNCLASSIFIED_ROAD_EVENT"/);
  assert.match(labelingSource, /question\?\.forced/);
  assert.match(labelingSource, /typeCode === "ROAD_DEBRIS"/);
  assert.match(labelingSource, /id:\s*"precision"[\s\S]*id:\s*"quick"/);
  assert.match(labelingSource, /AI 신뢰도와 별개인 사용자 최종값입니다/);
  assert.match(labelingSource, /severitySource:\s*finalSeverity \? "user"/);
  assert.match(labelingSource, /decideLabelingSubmission/);
  assert.match(labelingDecisionSource, /kind:\s*"save_and_advance"/);
  assert.match(labelingDecisionSource, /includeSeverity:\s*false/);
  assert.match(labelingSource, /USER_SEVERITIES[\s\S]*key:\s*"1"[\s\S]*key:\s*"2"[\s\S]*key:\s*"3"/);
  assert.match(labelingSource, /event\.key\.toLowerCase\(\)/);
  assert.match(labelingSource, /key === "n" \|\| key === "u"/);
  assert.match(labelingSource, /key === "e"/);
  assert.match(
    experimentalProposalSource,
    /value\.automatic_labeling_allowed !== false/,
  );
  assert.match(
    experimentalProposalSource,
    /Nothing in this parser creates an answer or annotation/,
  );
});

test("LabelingScreen can switch among every completed project video without mixing review state", () => {
  assert.match(labelingSource, /bridge\.analysis\s*\.list/);
  assert.match(labelingSource, /parseAnalysisJobList/);
  assert.match(labelingSource, /검토할 분석 영상/);
  assert.match(labelingSource, /completedJobs\.map/);
  assert.match(labelingSource, /더 최신 영상/);
  assert.match(labelingSource, /더 이전 영상/);
  assert.match(labelingSource, /다음 AI 제안 영상/);
  assert.match(labelingSource, /제안 영상 \$\{proposalJobCount\}[\s\S]*영상별 기록/);
  assert.match(labelingSource, /\{jobSwitcher\}\s*<GlassPanel className="label-empty-state">/);
});

test("ReportScreen does not offer login when an official CLI is unavailable", () => {
  assert.match(
    reportSource,
    /selectedProvider\?\.state === "not_connected"[\s\S]*공식 CLI에서 로그인/,
  );
  assert.match(
    reportSource,
    /selectedProvider\?\.state === "unavailable"[\s\S]*CLI 설치 필요/,
  );
  assert.match(reportSource, /id === "local"[\s\S]*"Local Model"/);
  assert.match(
    reportSource,
    /\{displayOperations\.length \? \([\s\S]*className="bulk-plan-actions"[\s\S]*\) : null\}/,
  );
  assert.match(
    reportSource,
    /trafficSuperVision\.incidents\.list\([\s\S]*analysisJobId:\s*summary\.jobId/,
  );
  assert.match(
    reportSource,
    /sourceFileSha256:\s*summary\.sourceSha256[\s\S]*incidentIds/,
  );
  assert.doesNotMatch(reportSource, /approvedIncidents/);
  assert.doesNotMatch(reportSource, /severityToContract|deterministicIncidentId/);
});
