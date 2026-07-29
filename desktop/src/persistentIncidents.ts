import {
  AUTO_LABELING_ISSUE_OPTIONS,
  type AutoLabelingIssueType,
} from "./autoLabeling";
import { normalizeIssueTypeApi } from "./issueTypes";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const CANDIDATE_ID_PATTERN = /^[a-f0-9]{20}$/;
const RECORD_HASH_PATTERN = /^[a-f0-9]{64}$/;
const SEVERITIES = new Set(["minor", "moderate", "major", "urgent"]);

export type PersistentIncident = {
  incidentId: string;
  projectId: string;
  questionId: string;
  analysisJobId: string;
  candidateId: string;
  issueType: AutoLabelingIssueType;
  severity: "minor" | "moderate" | "major" | "urgent" | null;
  maintenanceAction: string | null;
  ocrText: string | null;
  answerRevision: number;
  sourceRecordHash: string;
  updatedAt: string;
};

export function persistentIssueLabel(issueType: AutoLabelingIssueType) {
  return (
    AUTO_LABELING_ISSUE_OPTIONS.find((option) => option.value === issueType)
      ?.label ?? issueType
  );
}

export function persistentSeverityLabel(
  severity: PersistentIncident["severity"],
) {
  if (severity === "minor") return "경미";
  if (severity === "moderate") return "보통";
  if (severity === "major") return "심각";
  if (severity === "urgent") return "긴급";
  return "미지정";
}

function objectValue(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value as Record<string, unknown>;
}

function expectedUuid(value: unknown, label: string) {
  if (typeof value !== "string" || !UUID_PATTERN.test(value)) {
    throw new Error(`${label} 식별자가 올바르지 않습니다.`);
  }
  return value.toLowerCase();
}

function optionalText(value: unknown, label: string) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || value.length > 500 || value.includes("\0")) {
    throw new Error(`${label} 값이 올바르지 않습니다.`);
  }
  return value;
}

/**
 * Treat the route project/job as the authority. A response that crosses either
 * boundary is rejected instead of being rendered under the active project.
 */
export function parseActivePersistentIncidents(
  value: unknown,
  expectedProjectId: string,
  expectedAnalysisJobId: string,
): PersistentIncident[] {
  const expectedProject = expectedUuid(expectedProjectId, "요청 프로젝트");
  const expectedJob = expectedUuid(expectedAnalysisJobId, "요청 분석 작업");
  const response = objectValue(value, "Incident 목록");
  const responseProject = expectedUuid(response.project_id, "응답 프로젝트");
  if (responseProject !== expectedProject) {
    throw new Error("Incident 응답의 프로젝트 경계가 일치하지 않습니다.");
  }
  if (!Array.isArray(response.incidents)) {
    throw new Error("Incident 목록이 배열이 아닙니다.");
  }
  if (
    !Number.isInteger(response.count) ||
    response.count !== response.incidents.length ||
    response.incidents.length > 10_000
  ) {
    throw new Error("Incident 개수 검증에 실패했습니다.");
  }

  const seenIncidentIds = new Set<string>();
  const seenQuestionIds = new Set<string>();
  const seenCandidateIds = new Set<string>();

  return response.incidents.map((rawIncident, index) => {
    const row = objectValue(rawIncident, `Incident ${index + 1}`);
    const incidentId = expectedUuid(row.incident_id, "Incident");
    const projectId = expectedUuid(row.project_id, "Incident 프로젝트");
    const questionId = expectedUuid(row.question_id, "Incident 질문");
    const analysisJobId = expectedUuid(
      row.analysis_job_id,
      "Incident 분석 작업",
    );
    if (projectId !== expectedProject || analysisJobId !== expectedJob) {
      throw new Error("Incident가 현재 프로젝트·분석 작업 경계를 벗어났습니다.");
    }
    if (incidentId !== questionId) {
      throw new Error("Incident와 질문의 영구 식별자가 일치하지 않습니다.");
    }
    if (
      typeof row.candidate_id !== "string" ||
      !CANDIDATE_ID_PATTERN.test(row.candidate_id)
    ) {
      throw new Error("Incident 후보 식별자가 올바르지 않습니다.");
    }
    if (row.status !== "active") {
      throw new Error("활성 목록에 비활성 Incident가 포함되었습니다.");
    }
    const normalizedIssueType = normalizeIssueTypeApi(row.issue_type);
    if (normalizedIssueType === null) {
      throw new Error("Incident 문제 유형이 올바르지 않습니다.");
    }
    if (
      row.severity !== null &&
      row.severity !== undefined &&
      (typeof row.severity !== "string" || !SEVERITIES.has(row.severity))
    ) {
      throw new Error("Incident 심각도가 올바르지 않습니다.");
    }
    if (
      !Number.isInteger(row.answer_revision) ||
      Number(row.answer_revision) < 1
    ) {
      throw new Error("Incident 답변 개정 번호가 올바르지 않습니다.");
    }
    if (
      typeof row.source_record_hash !== "string" ||
      !RECORD_HASH_PATTERN.test(row.source_record_hash)
    ) {
      throw new Error("Incident 출처 해시가 올바르지 않습니다.");
    }
    if (
      typeof row.updated_at !== "string" ||
      !Number.isFinite(Date.parse(row.updated_at))
    ) {
      throw new Error("Incident 갱신 시각이 올바르지 않습니다.");
    }
    if (
      seenIncidentIds.has(incidentId) ||
      seenQuestionIds.has(questionId) ||
      seenCandidateIds.has(row.candidate_id)
    ) {
      throw new Error("Incident 목록에 중복 식별자가 있습니다.");
    }
    seenIncidentIds.add(incidentId);
    seenQuestionIds.add(questionId);
    seenCandidateIds.add(row.candidate_id);

    return {
      incidentId,
      projectId,
      questionId,
      analysisJobId,
      candidateId: row.candidate_id,
      issueType: normalizedIssueType,
      severity:
        row.severity === null || row.severity === undefined
          ? null
          : (row.severity as PersistentIncident["severity"]),
      maintenanceAction: optionalText(
        row.maintenance_action,
        "Incident 유지보수 조치",
      ),
      ocrText: optionalText(row.ocr_text, "Incident OCR"),
      answerRevision: Number(row.answer_revision),
      sourceRecordHash: row.source_record_hash,
      updatedAt: row.updated_at,
    };
  });
}
