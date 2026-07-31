import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const [homeSource, analysisSource, labelingSource] = await Promise.all([
  readFile(new URL("./screens/HomeScreen.tsx", import.meta.url), "utf8"),
  readFile(new URL("./screens/AnalysisScreen.tsx", import.meta.url), "utf8"),
  readFile(new URL("./screens/LabelingScreen.tsx", import.meta.url), "utf8"),
]);

test("home and analysis fail closed while the durable PipelineJob is loading or unavailable", () => {
  for (const source of [homeSource, analysisSource]) {
    assert.match(source, /pipelineRecoveryPending/);
    assert.match(source, /!pipeline\.hydrated/);
    assert.match(source, /pipeline\.projectId !== activeProject\.id/);
    assert.match(source, /pipelineRecoveryError/);
    assert.match(source, /pipeline\.error/);
    assert.match(source, /작업 복구 오류 · 새 분석 시작 안 함/);
    assert.match(source, /상태 다시 확인/);
    assert.match(source, /window\.location\.reload\(\)/);
  }

  assert.match(homeSource, /기존 영상과 PipelineJob을 확인하기 전에는 새 입력을 안내하지\s*않습니다/);
  assert.match(analysisSource, /저장된 PipelineJob을 확인하지 못한 상태를 새 작업 없음으로\s*처리하지 않습니다/);
});

test("question empty states expose only real pipeline state and never a sample evidence image", () => {
  assert.doesNotMatch(labelingSource, /realTunnelEvidenceImage/);
  assert.doesNotMatch(labelingSource, /참고 이미지 · 라벨 큐 아님/);
  assert.doesNotMatch(labelingSource, /분석 화면에서 후보 추출을\s*완료하세요/);
  assert.match(labelingSource, /useState\(true\)/);
  assert.match(labelingSource, /pipelinePreparing/);
  assert.match(labelingSource, /pipelineAwaitingQueue/);
  assert.match(labelingSource, /pipelineInterrupted/);
  assert.match(labelingSource, /zeroDetections/);
  assert.match(labelingSource, /unusableVisualEvidence/);
  assert.match(labelingSource, /질문 큐를 불러오지 못했습니다/);
  assert.match(labelingSource, /자동 질문을 준비하고 있습니다/);
  assert.match(labelingSource, /검지 후보 0건으로 정상 완료했습니다/);
  assert.match(labelingSource, /도로 상태를 판정할 수 없는 영상입니다/);
  assert.match(labelingSource, /질문·지도·CSV·HWPX 사실은 0건입니다/);
  assert.match(labelingSource, /영상 하나를 넣으면 질문 준비까지 자동으로 진행됩니다/);
  assert.match(labelingSource, /영상 넣고 자동 점검/);
  assert.match(labelingSource, /\{showAutoLabelingCommand \? autoLabelingPanel : null\}/);
  assert.match(analysisSource, /도로 상태를 판정할 수 없는 영상으로 분리했습니다/);
  assert.match(analysisSource, /질문·지도·CSV·HWPX 사실을 만들지 않았습니다/);
});
