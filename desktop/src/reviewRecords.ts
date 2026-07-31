import { useSyncExternalStore } from "react";
import { incidentTypeFromApi } from "./issueTypes";

export type ReviewAnswer = "yes" | "no" | "unsure" | "edited" | "skipped";

export type ReviewRecord = {
  question: number;
  questionId: string;
  candidateId?: string;
  answer: ReviewAnswer;
  selectedType: string;
  finalGeometry?: { x: number; y: number; width: number; height: number };
  severity?: string;
  severitySource?: "user";
  reviewMode?: "precision" | "quick";
  action?: string;
  ocrText?: string;
  reviewedAt: string;
  trainingStatus: "approved" | "negative" | "held" | "excluded";
  recordId?: string;
  recordHash?: string;
  answerRevision?: number;
  answeredBy?: "local_user" | "structured_llm_assisted";
  labelSource?: "local_user_review" | "structured_llm_assisted";
};

const EMPTY_REVIEW_RECORDS: ReviewRecord[] = [];
const reviewListeners = new Set<() => void>();
const reviewCache = new Map<
  string,
  { serialized: string | null; records: ReviewRecord[] }
>();

function severityFromApi(value?: string) {
  if (value === "urgent" || value === "major") return "높음";
  if (value === "minor") return "낮음";
  return "보통";
}

function finiteUnit(value: unknown) {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= 0 &&
    value <= 1
  );
}

export function reviewStorageKey(projectId: string, jobId: string) {
  return `trafficvisson:review:${projectId}:${jobId}`;
}

export function loadReviewHistory(storageKey: string): ReviewRecord[] {
  if (typeof window === "undefined") return EMPTY_REVIEW_RECORDS;
  try {
    const value = window.localStorage.getItem(storageKey);
    const cached = reviewCache.get(storageKey);
    if (cached?.serialized === value) return cached.records;
    if (!value) {
      reviewCache.set(storageKey, {
        serialized: null,
        records: EMPTY_REVIEW_RECORDS,
      });
      return EMPTY_REVIEW_RECORDS;
    }
    const parsed = JSON.parse(value);
    const records = Array.isArray(parsed)
      ? (parsed as ReviewRecord[])
      : EMPTY_REVIEW_RECORDS;
    reviewCache.set(storageKey, { serialized: value, records });
    return records;
  } catch {
    return EMPTY_REVIEW_RECORDS;
  }
}

export function saveReviewHistory(storageKey: string, records: ReviewRecord[]) {
  const serialized = JSON.stringify(records);
  window.localStorage.setItem(storageKey, serialized);
  reviewCache.set(storageKey, { serialized, records });
  reviewListeners.forEach((listener) => listener());
}

export function useReviewHistory(storageKey: string) {
  return useSyncExternalStore(
    (listener) => {
      reviewListeners.add(listener);
      return () => reviewListeners.delete(listener);
    },
    () => loadReviewHistory(storageKey),
    () => EMPTY_REVIEW_RECORDS,
  );
}

export function reviewRecordsFromQuestionList(
  value: unknown,
  jobId: string,
): ReviewRecord[] {
  if (!value || typeof value !== "object") return [];
  const rows = (value as { questions?: unknown[] }).questions;
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((raw, index) => {
    if (!raw || typeof raw !== "object") return [];
    const row = raw as {
      candidate_id?: string;
      question_id?: string;
      status?: string;
      latest_answer?: {
        answer?: "yes" | "no" | "unsure" | "edit" | "skipped";
        selected_issue_type?: string;
        severity?: string;
        maintenance_action?: string;
        ocr_text?: string;
        answered_by?: "local_user" | "structured_llm_assisted";
        normalized_box?: {
          x?: number;
          y?: number;
          width?: number;
          height?: number;
        };
      } | null;
      answered_at?: string | null;
      answer_revision?: number;
    };
    if (
      !row.latest_answer ||
      typeof row.candidate_id !== "string" ||
      row.candidate_id.length === 0
    ) {
      return [];
    }
    const answer =
      row.latest_answer.answer === "edit"
        ? "edited"
        : row.latest_answer.answer;
    if (
      answer !== "yes" &&
      answer !== "no" &&
      answer !== "unsure" &&
      answer !== "skipped" &&
      answer !== "edited"
    ) {
      return [];
    }
    const box = row.latest_answer.normalized_box;
    const finalGeometry =
      box &&
      finiteUnit(box.x) &&
      finiteUnit(box.y) &&
      finiteUnit(box.width) &&
      finiteUnit(box.height) &&
      Number(box.x) + Number(box.width) <= 1 &&
      Number(box.y) + Number(box.height) <= 1
        ? {
            x: Number(box.x) * 100,
            y: Number(box.y) * 100,
            width: Number(box.width) * 100,
            height: Number(box.height) * 100,
          }
        : undefined;
    const trainingStatus =
      row.status === "approved"
        ? "approved"
        : row.status === "negative"
          ? "negative"
          : row.status === "held"
            ? "held"
            : "excluded";
    return [
      {
        question: index,
        questionId:
          typeof row.question_id === "string" && row.question_id
            ? row.question_id
            : `${jobId}:${row.candidate_id}`,
        candidateId: row.candidate_id,
        answer,
        selectedType:
          incidentTypeFromApi(row.latest_answer.selected_issue_type) ??
          "UNCLASSIFIED_ROAD_EVENT",
        finalGeometry,
        severity: severityFromApi(row.latest_answer.severity),
        action:
          typeof row.latest_answer.maintenance_action === "string"
            ? row.latest_answer.maintenance_action
            : undefined,
        ocrText:
          typeof row.latest_answer.ocr_text === "string"
            ? row.latest_answer.ocr_text
            : undefined,
        reviewedAt:
          typeof row.answered_at === "string" ? row.answered_at : "",
        trainingStatus,
        answerRevision:
          Number.isInteger(row.answer_revision) && Number(row.answer_revision) >= 0
            ? Number(row.answer_revision)
            : undefined,
        ...(row.latest_answer.answered_by === "structured_llm_assisted" ||
        row.latest_answer.answered_by === "local_user"
          ? {
              answeredBy: row.latest_answer.answered_by,
              labelSource:
                row.latest_answer.answered_by ===
                "structured_llm_assisted"
                  ? ("structured_llm_assisted" as const)
                  : ("local_user_review" as const),
            }
          : {}),
      } satisfies ReviewRecord,
    ];
  });
}
