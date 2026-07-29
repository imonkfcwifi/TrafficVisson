import { useSyncExternalStore } from "react";

export type ProjectCatalogItem = {
  id: string;
  name: string;
  description: string | null;
  questionPolicy: "learning_assist" | "full_review" | "quick_review";
  activeModelVersion: string;
  createdAt: string;
  fixture: boolean;
};

type ProjectCatalogState = {
  projects: ProjectCatalogItem[];
  activeProjectId: string | null;
  loading: boolean;
  error: string | null;
};

const STORAGE_KEY = "trafficvisson:active-project:v1";
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const POLICY_MODES = new Set([
  "learning_assist",
  "full_review",
  "quick_review",
]);
const listeners = new Set<() => void>();
let loadGeneration = 0;
let state: ProjectCatalogState = {
  projects: [],
  activeProjectId: readStoredProjectId(),
  loading: false,
  error: null,
};

function readStoredProjectId() {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value && UUID_PATTERN.test(value) ? value.toLowerCase() : null;
  } catch {
    return null;
  }
}

function saveStoredProjectId(projectId: string | null) {
  try {
    if (projectId) {
      window.localStorage.setItem(STORAGE_KEY, projectId);
    } else {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  } catch {
    // A blocked local cache must not make the bridge-backed catalog unusable.
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function validTimestamp(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length <= 64 &&
    Number.isFinite(Date.parse(value))
  );
}

export function parseProject(value: unknown): ProjectCatalogItem {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    !UUID_PATTERN.test(value.id) ||
    typeof value.name !== "string" ||
    value.name.trim().length < 1 ||
    value.name.length > 120 ||
    !(
      value.description === null ||
      (typeof value.description === "string" &&
        value.description.length <= 2_000)
    ) ||
    typeof value.question_policy !== "string" ||
    !POLICY_MODES.has(value.question_policy) ||
    typeof value.active_model_version !== "string" ||
    value.active_model_version.length < 1 ||
    value.active_model_version.length > 128 ||
    !validTimestamp(value.created_at) ||
    typeof value.fixture !== "boolean"
  ) {
    throw new Error("프로젝트 응답 형식을 검증하지 못했습니다.");
  }
  return {
    id: value.id.toLowerCase(),
    name: value.name,
    description: value.description,
    questionPolicy: value.question_policy as ProjectCatalogItem["questionPolicy"],
    activeModelVersion: value.active_model_version,
    createdAt: value.created_at,
    fixture: value.fixture,
  };
}

export function parseProjectList(value: unknown): ProjectCatalogItem[] {
  if (
    !isRecord(value) ||
    !Array.isArray(value.projects) ||
    value.projects.length > 512
  ) {
    throw new Error("프로젝트 목록 응답 형식을 검증하지 못했습니다.");
  }
  const projects = value.projects.map(parseProject);
  if (new Set(projects.map((project) => project.id)).size !== projects.length) {
    throw new Error("프로젝트 목록에 중복 식별자가 있습니다.");
  }
  return projects;
}

function publish(next: ProjectCatalogState) {
  state = next;
  listeners.forEach((listener) => listener());
}

export function getProjectCatalogState() {
  return state;
}

export function subscribeProjectCatalog(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useProjectCatalog() {
  return useSyncExternalStore(
    subscribeProjectCatalog,
    getProjectCatalogState,
    getProjectCatalogState,
  );
}

export function getActiveProjectId() {
  const active = state.activeProjectId;
  return active && state.projects.some((project) => project.id === active)
    ? active
    : null;
}

export function selectProject(projectId: string) {
  const normalized = projectId.toLowerCase();
  if (!state.projects.some((project) => project.id === normalized)) {
    throw new Error("선택할 프로젝트를 찾지 못했습니다.");
  }
  saveStoredProjectId(normalized);
  publish({ ...state, activeProjectId: normalized, error: null });
}

export async function refreshProjects() {
  if (!window.trafficVisson) {
    publish({
      ...state,
      loading: false,
      error: "프로젝트 저장소는 설치형 Electron 앱에서 연결됩니다.",
    });
    return state.projects;
  }
  const generation = ++loadGeneration;
  publish({ ...state, loading: true, error: null });
  try {
    const projects = parseProjectList(
      await window.trafficVisson.projects.list(),
    );
    if (generation !== loadGeneration) return projects;
    const preferred = readStoredProjectId() ?? state.activeProjectId;
    const activeProjectId =
      preferred && projects.some((project) => project.id === preferred)
        ? preferred
        : projects[0]?.id ?? null;
    saveStoredProjectId(activeProjectId);
    publish({
      projects,
      activeProjectId,
      loading: false,
      error: null,
    });
    return projects;
  } catch (error) {
    if (generation === loadGeneration) {
      publish({
        ...state,
        loading: false,
        error:
          error instanceof Error
            ? error.message
            : "프로젝트 목록을 읽지 못했습니다.",
      });
    }
    throw error;
  }
}

export async function requireActiveProjectId() {
  const current = getActiveProjectId();
  if (current) return current;
  await refreshProjects();
  const restored = getActiveProjectId();
  if (!restored) {
    throw new Error("입력을 연결할 로컬 프로젝트가 없습니다.");
  }
  return restored;
}

export async function createProject(
  name: string,
  description?: string,
): Promise<ProjectCatalogItem> {
  const trimmedName = name.trim();
  const trimmedDescription = description?.trim();
  if (!trimmedName || trimmedName.length > 120) {
    throw new Error("프로젝트 이름은 1~120자로 입력하세요.");
  }
  if (trimmedDescription && trimmedDescription.length > 2_000) {
    throw new Error("프로젝트 설명은 2,000자 이내로 입력하세요.");
  }
  if (!window.trafficVisson) {
    throw new Error("새 프로젝트는 설치형 Electron 앱에서 만들 수 있습니다.");
  }
  const project = parseProject(
    await window.trafficVisson.projects.create({
      name: trimmedName,
      ...(trimmedDescription ? { description: trimmedDescription } : {}),
    }),
  );
  const projects = [
    ...state.projects.filter((item) => item.id !== project.id),
    project,
  ];
  saveStoredProjectId(project.id);
  publish({
    projects,
    activeProjectId: project.id,
    loading: false,
    error: null,
  });
  return project;
}

export function resetProjectCatalogForTests() {
  loadGeneration += 1;
  saveStoredProjectId(null);
  state = {
    projects: [],
    activeProjectId: null,
    loading: false,
    error: null,
  };
  listeners.clear();
}
