import assert from "node:assert/strict";
import test, { after } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const values = new Map();
globalThis.window = {
  localStorage: {
    getItem(key) {
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      values.set(key, value);
    },
  },
};

const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const module = await vite.ssrLoadModule("/src/reviewModePreference.ts");

after(async () => {
  await vite.close();
  delete globalThis.window;
});

test("빠른 질문 정책은 빠른 판정을, 나머지는 정밀 판정을 기본값으로 쓴다", () => {
  assert.equal(module.reviewModeForQuestionPolicy("quick_review"), "quick");
  assert.equal(module.reviewModeForQuestionPolicy("learning_assist"), "precision");
  assert.equal(module.reviewModeForQuestionPolicy("full_review"), "precision");
});

test("프로젝트별 판정 프로필은 서로 섞이지 않고 재시작 뒤 복구된다", () => {
  const projectA = "11111111-1111-4111-8111-111111111111";
  const projectB = "22222222-2222-4222-8222-222222222222";
  module.saveReviewModePreference(projectA, "quick");
  module.saveReviewModePreference(projectB, "precision");
  assert.equal(module.loadReviewModePreference(projectA, "precision"), "quick");
  assert.equal(module.loadReviewModePreference(projectB, "quick"), "precision");
});

test("손상된 캐시 값은 프로젝트 정책 기본값으로 되돌린다", () => {
  const project = "33333333-3333-4333-8333-333333333333";
  values.set(`trafficvisson:project-review-mode:v1:${project}`, "attacker");
  assert.equal(module.loadReviewModePreference(project, "quick"), "quick");
});
