import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const shellSource = await readFile(
  new URL("./components/AppShell.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(
  new URL("./styles.css", import.meta.url),
  "utf8",
);

test("diagnostics refreshes the authenticated runtime readiness snapshot", () => {
  assert.match(
    shellSource,
    /if \(!diagnosticsOpen\) return;[\s\S]*trafficSuperVision\?\.runtime\?\.readiness/,
  );
  assert.match(shellSource, /void readiness\(\)/);
  assert.match(
    shellSource,
    /schema_version !== "trafficvisson\.runtime-readiness\.v1"/,
  );
  assert.match(shellSource, /setRuntimeReadinessLoadState\("checking"\)/);
  assert.match(shellSource, /setRuntimeReadinessLoadState\("loaded"\)/);
  assert.match(shellSource, /setRuntimeReadinessLoadState\("unavailable"\)/);
});

test("FFmpeg and local analysis components render bridge-owned states", () => {
  assert.doesNotMatch(shellSource, /FFmpeg · 작업 전/);
  assert.match(
    shellSource,
    /runtimeComponentsById\.get\("ffmpeg"\) \?\? null/,
  );
  for (const componentId of [
    "pytorch_cuda",
    "grounding_dino",
    "sam2",
    "opencv",
    "korean_ocr",
    "embedding_search",
  ]) {
    assert.match(shellSource, new RegExp(`"${componentId}"`));
  }
  for (const label of [
    "준비됨",
    "설치됨 · 추가 구성 필요",
    "사용 불가",
    "확인 실패",
    "외부 설치 필요",
    "확인 중",
    "확인 불가",
  ]) {
    assert.match(shellSource, new RegExp(label));
  }
  assert.match(shellSource, /ffmpegComponent\?\.message/);
  assert.match(shellSource, /component\?\.version/);
});

test("HWP readiness remains gated by the real read-only COM preflight", () => {
  assert.match(shellSource, /trafficSuperVision\.hwp\s*\.preflight\(\)/);
  assert.match(
    shellSource,
    /setHwpAvailable\(\(response as \{ available\?: boolean \}\)\.available === true\)/,
  );
  assert.match(
    shellSource,
    /hwpAvailable === null[\s\S]*"확인 중"[\s\S]*hwpAvailable[\s\S]*"준비됨"[\s\S]*"사용 불가"/,
  );
});

test("runtime inventory stays compact inside the bounded diagnostics drawer", () => {
  assert.match(
    stylesSource,
    /\.diagnostics-drawer\s*\{[^}]*overflow:\s*auto/s,
  );
  assert.match(
    stylesSource,
    /\.diagnostic-runtime-list\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/s,
  );
  assert.match(stylesSource, /\.diagnostic-runtime-state--ready/);
  assert.match(stylesSource, /\.diagnostic-runtime-state--failed/);
});

test("active project secondary text prefers the durable PipelineJob state", () => {
  assert.match(
    shellSource,
    /activeProjectSecondaryStatus = currentPipeline[\s\S]*pipelineStatusLabel\(currentPipeline\)[\s\S]*currentPipeline\.progressPercent[\s\S]*runtime\.activeImport[\s\S]*"입력 대기"/,
  );
  assert.match(
    shellSource,
    /<small>\{activeProjectSecondaryStatus\}<\/small>/,
  );
  assert.doesNotMatch(
    shellSource,
    /<small>\s*\{runtime\.activeImport[\s\S]*"입력 대기"\}\s*<\/small>/,
  );
});
