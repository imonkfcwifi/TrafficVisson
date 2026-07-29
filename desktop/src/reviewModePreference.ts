export type ReviewModePreference = "precision" | "quick";

const STORAGE_PREFIX = "trafficvisson:project-review-mode:v1:";
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function storageKey(projectId: string) {
  if (!UUID_PATTERN.test(projectId)) {
    throw new Error("판정 프로필의 프로젝트 식별자가 올바르지 않습니다.");
  }
  return `${STORAGE_PREFIX}${projectId.toLowerCase()}`;
}

export function reviewModeForQuestionPolicy(
  questionPolicy:
    | "learning_assist"
    | "full_review"
    | "quick_review"
    | null
    | undefined,
): ReviewModePreference {
  return questionPolicy === "quick_review" ? "quick" : "precision";
}

export function loadReviewModePreference(
  projectId: string,
  fallback: ReviewModePreference,
): ReviewModePreference {
  try {
    const stored = window.localStorage.getItem(storageKey(projectId));
    return stored === "precision" || stored === "quick" ? stored : fallback;
  } catch {
    return fallback;
  }
}

export function saveReviewModePreference(
  projectId: string,
  reviewMode: ReviewModePreference,
) {
  if (reviewMode !== "precision" && reviewMode !== "quick") {
    throw new Error("저장할 판정 프로필이 올바르지 않습니다.");
  }
  try {
    window.localStorage.setItem(storageKey(projectId), reviewMode);
  } catch {
    // A blocked local cache must not prevent the bridge-backed review flow.
  }
}
