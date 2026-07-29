import assert from "node:assert/strict";
import test, { after, beforeEach } from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

class MemoryStorage {
  values = new Map();
  getItem(key) {
    return this.values.get(String(key)) ?? null;
  }
  setItem(key, value) {
    this.values.set(String(key), String(value));
  }
  removeItem(key) {
    this.values.delete(String(key));
  }
  clear() {
    this.values.clear();
  }
}

const PROJECT_A = "11111111-1111-4111-8111-111111111111";
const PROJECT_B = "22222222-2222-4222-8222-222222222222";
const memoryStorage = new MemoryStorage();
const previousWindow = globalThis.window;
globalThis.window = { localStorage: memoryStorage };
const desktopRoot = fileURLToPath(new URL("..", import.meta.url));
const vite = await createServer({
  configFile: false,
  root: desktopRoot,
  appType: "custom",
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
});
const catalog = await vite.ssrLoadModule("/src/projectCatalog.ts");

function apiProject(id, name) {
  return {
    id,
    name,
    description: null,
    question_policy: "learning_assist",
    active_model_version: "common-initial-0.1.0",
    created_at: "2026-07-28T01:00:00+00:00",
    fixture: false,
  };
}

after(async () => {
  await vite.close();
  if (previousWindow === undefined) delete globalThis.window;
  else globalThis.window = previousWindow;
});

beforeEach(() => {
  memoryStorage.clear();
  catalog.resetProjectCatalogForTests();
});

test("project responses are strict and converted to renderer names", () => {
  assert.deepEqual(catalog.parseProject(apiProject(PROJECT_A, "도로 A")), {
    id: PROJECT_A,
    name: "도로 A",
    description: null,
    questionPolicy: "learning_assist",
    activeModelVersion: "common-initial-0.1.0",
    createdAt: "2026-07-28T01:00:00+00:00",
    fixture: false,
  });
  const duplicate = apiProject(PROJECT_A, "중복");
  assert.throws(
    () =>
      catalog.parseProjectList({
        projects: [apiProject(PROJECT_A, "A"), duplicate],
      }),
    /중복 식별자/,
  );
  assert.throws(
    () =>
      catalog.parseProject({
        ...apiProject(PROJECT_A, "A"),
        question_policy: "attacker-policy",
      }),
    /응답 형식/,
  );
});

test("refresh restores a valid choice and switching is project bounded", async () => {
  memoryStorage.setItem("trafficvisson:active-project:v1", PROJECT_B);
  globalThis.window.trafficVisson = {
    projects: {
      list: async () => ({
        projects: [
          apiProject(PROJECT_A, "프로젝트 A"),
          apiProject(PROJECT_B, "프로젝트 B"),
        ],
      }),
    },
  };
  await catalog.refreshProjects();
  assert.equal(catalog.getActiveProjectId(), PROJECT_B);
  catalog.selectProject(PROJECT_A);
  assert.equal(catalog.getActiveProjectId(), PROJECT_A);
  assert.equal(
    memoryStorage.getItem("trafficvisson:active-project:v1"),
    PROJECT_A,
  );
  assert.throws(
    () => catalog.selectProject("33333333-3333-4333-8333-333333333333"),
    /찾지 못했습니다/,
  );
});

test("creating a project selects the validated bridge response", async () => {
  globalThis.window.trafficVisson = {
    projects: {
      create: async (request) => {
        assert.deepEqual(request, {
          name: "신규 점검",
          description: "영동선",
        });
        return apiProject(PROJECT_B, "신규 점검");
      },
    },
  };
  const created = await catalog.createProject(" 신규 점검 ", " 영동선 ");
  assert.equal(created.id, PROJECT_B);
  assert.equal(catalog.getActiveProjectId(), PROJECT_B);
  assert.equal(catalog.getProjectCatalogState().projects.length, 1);
});
