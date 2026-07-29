import assert from "node:assert/strict";
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
const module = await vite.ssrLoadModule("/src/labelingDecision.ts");

after(async () => {
  await vite.close();
});

test("빠른 판정은 모든 Y/N/U/E 답변을 심각도 없이 즉시 저장·진행한다", () => {
  for (const answer of ["yes", "no", "unsure", "edited"]) {
    assert.deepEqual(
      module.decideLabelingSubmission({
        reviewMode: "quick",
        answer,
        issueType: "POTHOLE",
      }),
      {
        kind: "save_and_advance",
        answer,
        includeSeverity: false,
      },
    );
  }
});

test("정밀 판정의 긍정 답변만 같은 카드 심각도 단계로 이동한다", () => {
  for (const answer of ["yes", "edited"]) {
    assert.deepEqual(
      module.decideLabelingSubmission({
        reviewMode: "precision",
        answer,
        issueType: "POTHOLE",
      }),
      {
        kind: "require_user_severity",
        answer,
      },
    );
  }
  for (const answer of ["no", "unsure", "skipped"]) {
    assert.deepEqual(
      module.decideLabelingSubmission({
        reviewMode: "precision",
        answer,
        issueType: "POTHOLE",
      }),
      {
        kind: "save_and_advance",
        answer,
        includeSeverity: false,
      },
    );
  }
});

test("이정표·터널명 확인은 정밀 모드에서도 심각도를 만들지 않는다", () => {
  for (const issueType of ["TUNNEL_IDENTITY", "GUIDE_SIGN_IDENTITY"]) {
    assert.deepEqual(
      module.decideLabelingSubmission({
        reviewMode: "precision",
        answer: "yes",
        issueType,
      }),
      {
        kind: "save_and_advance",
        answer: "yes",
        includeSeverity: false,
      },
    );
  }
});

test("심각도 단축키는 1·2·3만 사용자 최종값으로 변환한다", () => {
  assert.equal(module.severityFromShortcut("1"), "낮음");
  assert.equal(module.severityFromShortcut("2"), "보통");
  assert.equal(module.severityFromShortcut("3"), "높음");
  assert.equal(module.severityFromShortcut("4"), null);
  assert.equal(module.severityFromShortcut("y"), null);
});
