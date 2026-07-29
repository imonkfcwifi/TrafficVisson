import type { ReviewAnswer } from "./reviewRecords";
import type { ReviewModePreference } from "./reviewModePreference";

export type LabelingSubmissionDecision =
  | {
      kind: "require_user_severity";
      answer: "yes" | "edited";
    }
  | {
      kind: "save_and_advance";
      answer: ReviewAnswer;
      includeSeverity: false;
    };

const IDENTITY_TYPES = new Set([
  "TUNNEL_IDENTITY",
  "GUIDE_SIGN_IDENTITY",
]);

export function decideLabelingSubmission(input: {
  reviewMode: ReviewModePreference;
  answer: ReviewAnswer;
  issueType: string;
}): LabelingSubmissionDecision {
  const positive = input.answer === "yes" || input.answer === "edited";
  if (
    input.reviewMode === "precision" &&
    positive &&
    !IDENTITY_TYPES.has(input.issueType)
  ) {
    return {
      kind: "require_user_severity",
      answer: input.answer === "yes" ? "yes" : "edited",
    };
  }
  return {
    kind: "save_and_advance",
    answer: input.answer,
    includeSeverity: false,
  };
}

export function severityFromShortcut(
  key: string,
): "낮음" | "보통" | "높음" | null {
  if (key === "1") return "낮음";
  if (key === "2") return "보통";
  if (key === "3") return "높음";
  return null;
}
