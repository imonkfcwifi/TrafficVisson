import type {
  AnalysisCandidate,
  AnalysisVisionIssueType,
} from "./runtimeState";
import {
  CANONICAL_ISSUE_TYPES,
  incidentTypeFromApi,
  issueLabelKo,
} from "./issueTypes";

export type CandidateProposal = {
  typeCode: string;
  displayName: string | null;
  prompt: string;
  confidence: number | null;
  scoreKind: "classification" | "scene";
  scoreText: string;
  experimental: boolean;
  forced: boolean;
  rationale: string;
  box?: { x: number; y: number; width: number; height: number };
};

const ISSUE_PRESENTATION = Object.fromEntries([
  ...CANONICAL_ISSUE_TYPES.map(({ apiValue, incidentType, labelKo }) => [
    apiValue,
    { typeCode: incidentType, displayName: labelKo },
  ]),
  ["other", { typeCode: "OTHER", displayName: "기타 도로 문제" }],
]) as Record<
  AnalysisVisionIssueType,
  { typeCode: string; displayName: string }
>;

function sceneFallback(
  candidate: AnalysisCandidate,
  modelStatus?: string,
): CandidateProposal {
  const modelNote =
    modelStatus === "no_detection"
      ? " 실험 모델은 이 장면에서 유형을 제안하지 않았습니다."
      : modelStatus === "classified"
        ? " 실험 모델 응답에는 표시 가능한 도로 문제 유형이 없습니다."
      : "";
  return {
    typeCode: "UNCLASSIFIED_ROAD_EVENT",
    displayName: null,
    prompt: "이 장면에 점검이 필요한 도로 문제가 있나요?",
    confidence: candidate.sceneScore,
    scoreKind: "scene",
    scoreText: `장면 변화 ${candidate.sceneScore.toFixed(2)}`,
    experimental: false,
    forced: false,
    rationale: `장면 변화 ${candidate.sceneScore.toFixed(
      2,
    )} · 선명도 ${candidate.sharpnessScore.toFixed(
      2,
    )}를 기준으로 묶은 미분류 후보입니다.${modelNote} 실제 결함 유형은 사용자가 확인해야 합니다.`,
  };
}

type ProposalBox = {
  x: number;
  y: number;
  width: number;
  height: number;
};

function validProposalBox(value: unknown): value is ProposalBox {
  if (!value || typeof value !== "object") return false;
  const box = value as Partial<ProposalBox>;
  return (
    Number.isFinite(box.x) &&
    Number.isFinite(box.y) &&
    Number.isFinite(box.width) &&
    Number.isFinite(box.height) &&
    Number(box.x) >= 0 &&
    Number(box.y) >= 0 &&
    Number(box.width) > 0 &&
    Number(box.height) > 0 &&
    Number(box.x) + Number(box.width) <= 1.00000001 &&
    Number(box.y) + Number(box.height) <= 1.00000001
  );
}

function validProposalScore(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= 0 &&
    value <= 1
  );
}

function percentBox(box: ProposalBox) {
  return {
    x: box.x * 100,
    y: box.y * 100,
    width: box.width * 100,
    height: box.height * 100,
  };
}

function abbreviatedOcrText(text: string) {
  const singleLine = text.replace(/\s+/g, " ").trim();
  return singleLine.length <= 80
    ? singleLine
    : `${singleLine.slice(0, 79)}…`;
}

export function buildCandidateProposal(
  candidate: AnalysisCandidate,
): CandidateProposal {
  const dense = candidate.denseProposal;
  if (
    candidate.candidateKind ===
      "dense_candidate_evidence_overlay" &&
    dense &&
    candidate.evidenceOverlay?.reviewOnly === true &&
    validProposalScore(dense.score) &&
    Object.prototype.hasOwnProperty.call(
      ISSUE_PRESENTATION,
      dense.issueType,
    ) &&
    (dense.normalizedBox === null ||
      validProposalBox(dense.normalizedBox))
  ) {
    const presentation = ISSUE_PRESENTATION[dense.issueType];
    const sourceSummary = dense.proposalSources.join(", ");
    const ocrText =
      typeof dense.ocrText === "string" && dense.ocrText.trim()
        ? abbreviatedOcrText(dense.ocrText)
        : null;
    return {
      typeCode: presentation.typeCode,
      displayName: presentation.displayName,
      prompt: ocrText
        ? `2fps 전체 영상 분석이 ${presentation.displayName} 후보와 “${ocrText}” OCR을 제안했습니다. 실제 유형과 글자를 확인해 주세요.`
        : `2fps 전체 영상 분석이 ${presentation.displayName} 후보를 제안했습니다. 실제 유지보수 대상이 맞나요?`,
      confidence: dense.score,
      scoreKind: "classification",
      scoreText: `후보 점수 ${dense.score.toFixed(
        3,
      )} · 확률 또는 확정 판정이 아님`,
      experimental: true,
      forced:
        dense.urgentReview ||
        dense.issueType === "road_debris",
      rationale: `전체 2fps 인덱스의 검지·프로젝트 모델·승인 사례 신호(${sourceSummary})를 병합한 검토용 제안입니다. 사용자 승인 전에는 Annotation, Incident, 학습 데이터 또는 보고서 사실로 반영되지 않습니다.`,
      ...(dense.normalizedBox
        ? { box: percentBox(dense.normalizedBox) }
        : {}),
    };
  }
  const inference = candidate.aiInference;
  const hasModelIdentity =
    inference?.schemaVersion === "trafficvisson.vision-inference.v1" &&
    typeof inference?.packId === "string" &&
    inference.packId.length > 0 &&
    typeof inference.packVersion === "string" &&
    inference.packVersion.length > 0;
  if (
    !inference ||
    inference.mode !== "local_model" ||
    inference.status !== "classified" ||
    !hasModelIdentity ||
    !Array.isArray(inference.predictions) ||
    !Array.isArray(inference.ocrObservations)
  ) {
    return sceneFallback(candidate, inference?.status);
  }

  const usablePredictions = inference.predictions.filter(
    (prediction) =>
      prediction &&
      validProposalScore(prediction.confidence) &&
      Object.prototype.hasOwnProperty.call(
        ISSUE_PRESENTATION,
        prediction.issueType,
      ) &&
      (prediction.box === null || validProposalBox(prediction.box)),
  );
  const usableOcr = inference.ocrObservations.filter(
    (observation) =>
      observation &&
      typeof observation.text === "string" &&
      observation.text.trim().length > 0 &&
      validProposalScore(observation.confidence) &&
      validProposalBox(observation.box),
  );

  if (usablePredictions.length === 0 && usableOcr.length > 0) {
    const observation = usableOcr.reduce((best, current) =>
      current.confidence > best.confidence ? current : best,
    );
    const text = abbreviatedOcrText(observation.text);
    return {
      typeCode: "UNCLASSIFIED_ROAD_EVENT",
      displayName: "이정표·터널명 OCR",
      prompt: `AI OCR이 “${text}”로 읽었습니다. 실제 이정표 또는 터널명인지 확인해 주세요.`,
      confidence: observation.confidence,
      scoreKind: "classification",
      scoreText: `OCR 모델 점수 ${observation.confidence.toFixed(
        3,
      )} · 글자 또는 시설명 확정 아님`,
      experimental: true,
      forced: false,
      rationale: `실험적 로컬 모델 ${inference.packId}@${inference.packVersion}의 검증 전 OCR 결과입니다. 이정표인지 터널명인지 자동 판정하지 않으며, 사용자가 유형과 실제 글자를 직접 확인해야 합니다.`,
      box: percentBox(observation.box),
    };
  }

  if (usablePredictions.length === 0) {
    return sceneFallback(candidate, inference.status);
  }

  const prediction = usablePredictions.reduce((best, current) =>
    current.confidence > best.confidence ? current : best,
  );
  const normalizedTypeCode = incidentTypeFromApi(prediction.issueType);
  const normalizedDisplayName = issueLabelKo(prediction.issueType);
  const presentation =
    normalizedTypeCode && normalizedDisplayName
      ? {
          typeCode: normalizedTypeCode,
          displayName: normalizedDisplayName,
        }
      : ISSUE_PRESENTATION[prediction.issueType];
  const scoreName =
    prediction.calibration === "zero_shot_relative"
      ? "상대 점수"
      : "모델 점수";
  const regionNote = prediction.box
    ? "AI가 제안한 영역이 함께 표시됩니다."
    : "장면 단위 제안이며 AI가 제안한 영역은 없습니다.";
  const identity = `${inference.packId}@${inference.packVersion}`;

  return {
    typeCode: presentation.typeCode,
    displayName: presentation.displayName,
    prompt: `AI가 이 장면에서 ${presentation.displayName} 가능성을 제안했습니다. 해당 유형이 맞나요?`,
    confidence: prediction.confidence,
    scoreKind: "classification",
    scoreText: `${scoreName} ${prediction.confidence.toFixed(
      3,
    )} · 확률 또는 확정 판정이 아님`,
    experimental: true,
    forced: prediction.issueType === "road_debris",
    rationale: `실험적 로컬 모델 ${identity}의 ${scoreName.toLowerCase()}입니다. ${regionNote} 이 제안만으로 자동 승인·긴급도·유지보수 조치·보고서 반영을 결정하지 않습니다.`,
    ...(prediction.box
      ? {
          box: percentBox(prediction.box),
        }
      : {}),
  };
}
