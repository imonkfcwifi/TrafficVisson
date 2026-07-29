export const EXPORT_ARTIFACTS = [
  {
    artifact: "raw_detections",
    fileName: "raw_detections.csv",
    description: "AI 원시 후보값",
  },
  {
    artifact: "reviewed_incidents",
    fileName: "reviewed_incidents.csv",
    description: "사용자 확정 사건",
  },
  {
    artifact: "label_questions",
    fileName: "label_questions.csv",
    description: "질문·정책·답변 연결",
  },
  {
    artifact: "annotations",
    fileName: "annotations.csv",
    description: "학습 라벨",
  },
  {
    artifact: "ocr_observations",
    fileName: "ocr_observations.csv",
    description: "OCR 원문·검증",
  },
  {
    artifact: "route_points",
    fileName: "route_points.csv",
    description: "GPS 경로점 · 없으면 헤더만 기록",
  },
  {
    artifact: "maintenance_actions",
    fileName: "maintenance_actions.csv",
    description: "제안·확정 조치",
  },
  {
    artifact: "report_payload",
    fileName: "report_payload.json",
    description: "보고서 입력",
  },
  {
    artifact: "manifest",
    fileName: "manifest.json",
    description: "provenance·SHA-256",
  },
] as const;

export type ExportArtifactName =
  (typeof EXPORT_ARTIFACTS)[number]["artifact"];

export type CompletedExportArtifact = {
  artifact: ExportArtifactName;
  fileName: string;
  rowCount: number;
  sizeBytes: number;
  sha256: string;
};

export type CompletedExport = {
  status: "completed";
  exportId: string;
  archiveFileName: string;
  archiveSizeBytes: number;
  archiveSha256: string;
  artifactCount: number;
  evidenceIncidentCount: number;
  evidenceFileCount: number;
  evidenceSizeBytes: number;
  artifacts: CompletedExportArtifact[];
};

export type ExportDisplayRow = {
  artifact: ExportArtifactName;
  fileName: string;
  description: string;
  rowCount: number | null;
  sizeBytes: number | null;
  sha256: string | null;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA_PATTERN = /^[a-f0-9]{64}$/;
const SAFE_FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$/;
const allowedArtifacts = new Set(
  EXPORT_ARTIFACTS.map((item) => item.artifact),
);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function parseCompletedExport(value: unknown): CompletedExport {
  if (
    !isRecord(value) ||
    value.status !== "completed" ||
    typeof value.export_id !== "string" ||
    !UUID_PATTERN.test(value.export_id) ||
    typeof value.archive_file_name !== "string" ||
    !SAFE_FILE_NAME.test(value.archive_file_name) ||
    !Number.isInteger(value.archive_size_bytes) ||
    Number(value.archive_size_bytes) <= 0 ||
    typeof value.archive_sha256 !== "string" ||
    !SHA_PATTERN.test(value.archive_sha256) ||
    !Number.isInteger(value.artifact_count) ||
    !Number.isInteger(value.evidence_incident_count) ||
    Number(value.evidence_incident_count) < 0 ||
    Number(value.evidence_incident_count) > 512 ||
    !Number.isInteger(value.evidence_file_count) ||
    Number(value.evidence_file_count) < 0 ||
    Number(value.evidence_file_count) > 1024 ||
    !Number.isInteger(value.evidence_size_bytes) ||
    Number(value.evidence_size_bytes) < 0 ||
    Number(value.evidence_size_bytes) > 8 * 1024 * 1024 * 1024 ||
    !Array.isArray(value.artifacts)
  ) {
    throw new Error("내보내기 완료 응답 형식을 검증하지 못했습니다.");
  }

  const seen = new Set<string>();
  const artifacts: CompletedExportArtifact[] = value.artifacts.map((raw) => {
    if (
      !isRecord(raw) ||
      typeof raw.artifact !== "string" ||
      !allowedArtifacts.has(raw.artifact as ExportArtifactName) ||
      seen.has(raw.artifact) ||
      typeof raw.file_name !== "string" ||
      !SAFE_FILE_NAME.test(raw.file_name) ||
      !Number.isInteger(raw.row_count) ||
      Number(raw.row_count) < 0 ||
      !Number.isInteger(raw.size_bytes) ||
      Number(raw.size_bytes) <= 0 ||
      typeof raw.sha256 !== "string" ||
      !SHA_PATTERN.test(raw.sha256)
    ) {
      throw new Error("산출물 행·크기·SHA-256 응답을 검증하지 못했습니다.");
    }
    const contract = EXPORT_ARTIFACTS.find(
      (item) => item.artifact === raw.artifact,
    );
    if (!contract || contract.fileName !== raw.file_name) {
      throw new Error("산출물 이름이 내보내기 계약과 일치하지 않습니다.");
    }
    seen.add(raw.artifact);
    return {
      artifact: raw.artifact as ExportArtifactName,
      fileName: raw.file_name,
      rowCount: Number(raw.row_count),
      sizeBytes: Number(raw.size_bytes),
      sha256: raw.sha256,
    };
  });
  if (
    artifacts.length !== EXPORT_ARTIFACTS.length ||
    EXPORT_ARTIFACTS.some((item) => !seen.has(item.artifact)) ||
    Number(value.evidence_file_count) !==
      Number(value.evidence_incident_count) * 2 ||
    Number(value.artifact_count) !==
      artifacts.length + 1 + Number(value.evidence_file_count)
  ) {
    throw new Error("전체 추적 패키지 산출물 수가 계약과 일치하지 않습니다.");
  }
  return {
    status: "completed",
    exportId: value.export_id,
    archiveFileName: value.archive_file_name,
    archiveSizeBytes: Number(value.archive_size_bytes),
    archiveSha256: value.archive_sha256,
    artifactCount: Number(value.artifact_count),
    evidenceIncidentCount: Number(value.evidence_incident_count),
    evidenceFileCount: Number(value.evidence_file_count),
    evidenceSizeBytes: Number(value.evidence_size_bytes),
    artifacts,
  };
}

export function buildExportDisplayRows(
  completed: CompletedExport | null,
): ExportDisplayRow[] {
  const completedByArtifact = new Map(
    completed?.artifacts.map((item) => [item.artifact, item]) ?? [],
  );
  return EXPORT_ARTIFACTS.map((contract) => {
    const actual = completedByArtifact.get(contract.artifact);
    return {
      artifact: contract.artifact,
      fileName: actual?.fileName ?? contract.fileName,
      description: contract.description,
      rowCount: actual?.rowCount ?? null,
      sizeBytes: actual?.sizeBytes ?? null,
      sha256: actual?.sha256 ?? null,
    };
  });
}
