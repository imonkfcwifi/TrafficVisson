import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const historySource = await readFile(
  new URL("./screens/HistoryScreen.tsx", import.meta.url),
  "utf8",
);
const labelingSource = await readFile(
  new URL("./screens/LabelingScreen.tsx", import.meta.url),
  "utf8",
);

test("missed-candidate QA uses the full immutable 2fps timeline, not candidate clips", () => {
  assert.match(
    historySource,
    /userDiscoveredCandidates[\s\S]*\.reviewFrame\(\{[\s\S]*timestampSeconds/,
  );
  assert.match(
    historySource,
    /aria-label="전체 주행 2fps QA 시각"[\s\S]*step=\{0\.5\}/,
  );
  assert.match(
    historySource,
    /missed-qa-frame__image[\s\S]*submitMissedCandidate\(event\)/,
  );
  assert.doesNotMatch(
    historySource,
    /<video[\s\S]{0,1400}onClick=\{[\s\S]{0,160}submitMissedCandidate/,
  );
  assert.match(
    historySource,
    /missReviewFrame\.selectedFrameTimestampSeconds[\s\S]*missReviewFrame\.questionQueueRevision/,
  );
});

test("delta question creation refreshes pipeline and analysis before returning to review", () => {
  assert.match(
    historySource,
    /service\.create\(\{[\s\S]*expectedQuestionQueueRevision:[\s\S]*Promise\.all\(\[[\s\S]*bridge\.pipeline\.get[\s\S]*bridge\.analysis\.get/,
  );
  assert.match(
    historySource,
    /nextBinding\.queueRevision !== receipt\.questionQueueRevision/,
  );
  assert.match(
    historySource,
    /restoreCompletedAnalysis\([\s\S]*saveSelectedCandidateId\([\s\S]*onNavigate\("label"\)/,
  );
  assert.match(
    historySource,
    /누락 후보 질문은 생성됐지만 최신 화면을 불러오지 못했습니다[\s\S]*중복 제출하지 말고/,
  );
});

test("final answer no longer auto-completes before explicit missed-scene QA", () => {
  assert.match(
    labelingSource,
    /모델 누락 장면 확인[\s\S]*누락 없음 · 검토 완료/,
  );
  assert.doesNotMatch(
    labelingSource,
    /function goNext[\s\S]{0,700}requestPipelineReviewCompletion/,
  );
  assert.match(
    labelingSource,
    /아직 Incident·학습 데이터·보고서 사실을 확정하지 않았습니다/,
  );
});

test("zero-candidate promoted survey can browse immutable frames before any candidate exists", () => {
  assert.match(
    historySource,
    /function ZeroCandidateSurveyHistory[\s\S]*\.reviewFrame\(\{[\s\S]*aria-label="전체 주행 2fps QA 시각"[\s\S]*service\.create\(\{/,
  );
  assert.match(
    historySource,
    /questionQueueBinding\?\.zeroCandidateSurvey[\s\S]*<ZeroCandidateSurveyHistory/,
  );
  assert.match(
    historySource,
    /누락 없음 · 질문 검토로/,
  );
  assert.match(
    labelingSource,
    /zeroCandidateSurvey[\s\S]*승격 모델 0건 조사 QA[\s\S]*requestPipelineReviewCompletion\(\[\]\)/,
  );
});

test("bootstrap queues do not display or send promoted-model missed-scene QA", () => {
  assert.match(
    labelingSource,
    /const promotedSurveyQaRequired[\s\S]*const finalSurveyQaChoice =[\s\S]*promotedSurveyQaRequired/,
  );
  assert.match(
    labelingSource,
    /\{promotedSurveyQaRequired \? \([\s\S]*모델 누락 장면 확인[\s\S]*\) : null\}/,
  );
  assert.match(
    labelingSource,
    /promotedSurveyQaRequired[\s\S]*"검토 완료 · 결과 갱신"/,
  );
});
