import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Clock3,
  History,
  Image,
  MapPin,
  MapPinOff,
  Tags,
  Video,
} from "lucide-react";
import {
  loadSelectedCandidateId,
  saveSelectedCandidateId,
} from "../candidateSelection";
import {
  Badge,
  Button,
  DefinitionList,
  GlassPanel,
  PanelHeader,
} from "../components/ui";
import type { ScreenId } from "../demo";
import { buildCandidateEvidenceUrl } from "../evidence";
import {
  parseActivePersistentIncidents,
  persistentIssueLabel,
  persistentSeverityLabel,
  type PersistentIncident,
} from "../persistentIncidents";
import { useRuntimeState } from "../runtimeState";

type EvidenceMode = "frame" | "clip";

type LatestAnswer = {
  answer?: "yes" | "no" | "unsure" | "edit" | "skipped";
  selected_issue_type?: string;
  severity?: string;
  maintenance_action?: string;
  ocr_text?: string;
  normalized_box?: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
};

type CandidateReview = {
  questionId?: string;
  status?: string;
  latestAnswer: LatestAnswer | null;
  answeredAt?: string | null;
  answerRevision?: number;
};

function formatTimecode(seconds: number) {
  const totalMilliseconds = Math.max(0, Math.round(seconds * 1_000));
  const hours = Math.floor(totalMilliseconds / 3_600_000);
  const minutes = Math.floor((totalMilliseconds % 3_600_000) / 60_000);
  const wholeSeconds = Math.floor((totalMilliseconds % 60_000) / 1_000);
  const milliseconds = totalMilliseconds % 1_000;
  return [hours, minutes, wholeSeconds]
    .map((value) => String(value).padStart(2, "0"))
    .join(":")
    .concat(`.${String(milliseconds).padStart(3, "0")}`);
}

function answerLabel(answer?: LatestAnswer["answer"]) {
  if (answer === "yes") return "예";
  if (answer === "no") return "아니오";
  if (answer === "unsure") return "판단 어려움";
  if (answer === "edit") return "영역 수정";
  if (answer === "skipped") return "건너뜀";
  return "답변 없음";
}

function statusLabel(status?: string) {
  if (status === "approved") return "승인 라벨";
  if (status === "negative") return "음성 라벨";
  if (status === "held") return "보류 라벨";
  if (status === "excluded") return "학습 제외";
  return "검토 대기";
}

export function ReviewScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const runtime = useRuntimeState();
  const summary = runtime.analysisSummary;
  const candidates = summary?.candidates ?? [];
  const sourceLocation = summary?.sourceLocation ?? null;
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(() =>
    summary
      ? loadSelectedCandidateId(summary.projectId, summary.jobId, summary.candidates)
      : null,
  );
  const [evidenceMode, setEvidenceMode] = useState<EvidenceMode>("frame");
  const [reviews, setReviews] = useState<Record<string, CandidateReview>>({});
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [incidents, setIncidents] = useState<PersistentIncident[]>([]);
  const [incidentError, setIncidentError] = useState<string | null>(null);

  useEffect(() => {
    if (!summary?.candidates.length) {
      setSelectedCandidateId(null);
      return;
    }
    setSelectedCandidateId((current) => {
      if (summary.candidates.some((candidate) => candidate.candidateId === current)) {
        return current;
      }
      return (
        loadSelectedCandidateId(summary.projectId, summary.jobId, summary.candidates) ??
        summary.candidates[0].candidateId
      );
    });
  }, [summary]);

  useEffect(() => {
    if (!summary || !window.trafficSuperVision) {
      setReviews({});
      return;
    }
    let active = true;
    setReviewError(null);
    void window.trafficSuperVision.candidateQuestions
      .list({ projectId: summary.projectId, jobId: summary.jobId })
      .then((value) => {
        if (!active || !value || typeof value !== "object") return;
        const rows = (value as { questions?: unknown[] }).questions;
        if (!Array.isArray(rows)) return;
        const next: Record<string, CandidateReview> = {};
        for (const raw of rows) {
          if (!raw || typeof raw !== "object") continue;
          const row = raw as {
            candidate_id?: string;
            question_id?: string;
            status?: string;
            latest_answer?: LatestAnswer | null;
            answered_at?: string | null;
            answer_revision?: number;
          };
          if (!row.candidate_id) continue;
          next[row.candidate_id] = {
            questionId: row.question_id,
            status: row.status,
            latestAnswer: row.latest_answer ?? null,
            answeredAt: row.answered_at,
            answerRevision: row.answer_revision,
          };
        }
        setReviews(next);
      })
      .catch((error) => {
        if (active) {
          setReviewError(
            error instanceof Error
              ? error.message
              : "저장된 라벨 기록을 읽지 못했습니다.",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [summary]);

  useEffect(() => {
    if (!summary || !window.trafficSuperVision) {
      setIncidents([]);
      return;
    }
    let active = true;
    setIncidentError(null);
    void window.trafficSuperVision.incidents
      .list({
        projectId: summary.projectId,
        analysisJobId: summary.jobId,
      })
      .then((value) => {
        if (!active) return;
        setIncidents(
          parseActivePersistentIncidents(
            value,
            summary.projectId,
            summary.jobId,
          ),
        );
      })
      .catch((error) => {
        if (!active) return;
        setIncidents([]);
        setIncidentError(
          error instanceof Error
            ? error.message
            : "영구 Incident 기록을 읽지 못했습니다.",
        );
      });
    return () => {
      active = false;
    };
  }, [summary]);

  const candidateIndex = Math.max(
    0,
    candidates.findIndex((candidate) => candidate.candidateId === selectedCandidateId),
  );
  const candidate = candidates[candidateIndex];
  const review = candidate ? reviews[candidate.candidateId] : undefined;
  const incident = useMemo(() => {
    if (!candidate) return undefined;
    return incidents.find(
      (item) =>
        item.candidateId === candidate.candidateId ||
        (review?.questionId !== undefined &&
          item.questionId === review.questionId),
    );
  }, [candidate, incidents, review?.questionId]);

  useEffect(() => {
    setEvidenceMode("frame");
  }, [candidate?.candidateId]);

  const beforeAfter = useMemo(() => {
    if (!candidate) return [];
    const answer = review?.latestAnswer;
    const finalIssue = incident
      ? persistentIssueLabel(incident.issueType)
      : answer?.selected_issue_type ?? "미확정";
    const finalSeverity = incident
      ? persistentSeverityLabel(incident.severity)
      : answer?.severity ?? "미확정";
    const finalAction = incident?.maintenanceAction ?? answer?.maintenance_action;
    const finalOcr = incident?.ocrText ?? answer?.ocr_text;
    return [
      [
        "후보 / 사건 ID",
        candidate.candidateId,
        review?.questionId ?? answerLabel(answer?.answer),
        incident?.incidentId ?? "영구 사건 미생성",
      ],
      [
        "대표 시각",
        formatTimecode(candidate.representativeTimestampSeconds),
        "시간 수정 없음",
        formatTimecode(candidate.representativeTimestampSeconds),
      ],
      [
        "후보 점수",
        `장면 ${candidate.sceneScore.toFixed(3)}`,
        "결함 신뢰도 아님",
        "유형 미확정",
      ],
      [
        "선명도",
        candidate.sharpnessScore.toFixed(1),
        "사용자 점수 없음",
        candidate.sharpnessScore.toFixed(1),
      ],
      [
        "문제 유형",
        "미분류 장면 후보",
        answer?.selected_issue_type ?? "선택 없음",
        finalIssue,
      ],
      ["심각도", "미제안", answer?.severity ?? "선택 없음", finalSeverity],
      [
        "유지보수 조치",
        "미제안",
        answer?.maintenance_action ?? "입력 없음",
        finalAction ?? "미확정",
      ],
      ["OCR 원문", "없음", answer?.ocr_text ?? "입력 없음", finalOcr ?? "미확정"],
    ];
  }, [candidate, incident, review]);

  if (!summary || !candidate) {
    return (
      <div className="screen screen--review">
        <header className="screen-header">
          <div>
            <p className="eyebrow">CANDIDATE REVIEW</p>
            <h1>사건 검토</h1>
            <p>원본 후보·사용자 답변·최종 판정을 같은 증거 체계로 비교합니다.</p>
          </div>
        </header>
        <GlassPanel className="analysis-empty">
          <Image size={34} />
          <h1>검토할 실제 분석 후보가 없습니다</h1>
          <p>
            영상을 가져와 후보 추출을 완료하면 대표 프레임과 미리보기 클립이 이 화면에
            표시됩니다.
          </p>
          <Button variant="primary" onClick={() => onNavigate("import")}>
            영상 가져오기로 이동
          </Button>
        </GlassPanel>
      </div>
    );
  }

  const selectCandidate = (index: number) => {
    const next = candidates[index];
    if (!next) return;
    setSelectedCandidateId(next.candidateId);
    saveSelectedCandidateId(summary.projectId, summary.jobId, next.candidateId);
  };

  const navigateWithCandidate = (screen: ScreenId) => {
    saveSelectedCandidateId(
      summary.projectId,
      summary.jobId,
      candidate.candidateId,
    );
    onNavigate(screen);
  };

  const imageUrl = buildCandidateEvidenceUrl(
    summary.projectId,
    summary.jobId,
    candidate.candidateId,
    "image",
  );
  const clipUrl = buildCandidateEvidenceUrl(
    summary.projectId,
    summary.jobId,
    candidate.candidateId,
    "clip",
  );
  const finalBox = review?.latestAnswer?.normalized_box;
  const reviewStatus = incident ? "승인 사건" : statusLabel(review?.status);
  const reviewTone =
    incident || review?.status === "approved"
      ? "success"
      : review?.status === "negative"
        ? "danger"
        : review?.status === "held"
          ? "warning"
          : "neutral";

  return (
    <div className="screen screen--review">
      <header className="screen-header">
        <div>
          <p className="eyebrow">CANDIDATE REVIEW</p>
          <h1>사건 검토</h1>
          <p>
            복구된 분석 후보의 실제 프레임·클립과 질문형 라벨링의 저장 결과를 함께
            확인합니다.
          </p>
        </div>
        <div className="header-actions">
          <span className="mono">
            {candidate.candidateId.slice(0, 8)}… · {candidateIndex + 1} / {candidates.length}
          </span>
          <Button
            aria-label="이전 후보"
            onClick={() => selectCandidate(candidateIndex - 1)}
            disabled={candidateIndex === 0}
          >
            <ArrowLeft size={17} /> 이전
          </Button>
          <Button
            aria-label="다음 후보"
            onClick={() => selectCandidate(candidateIndex + 1)}
            disabled={candidateIndex === candidates.length - 1}
          >
            다음 <ArrowRight size={17} />
          </Button>
        </div>
      </header>

      <div className="review-layout">
        <div className="review-evidence-column">
          <GlassPanel className="review-viewer">
            <div className="segmented-control" role="group" aria-label="실제 증거 보기">
              <button
                type="button"
                className={evidenceMode === "frame" ? "active" : ""}
                onClick={() => setEvidenceMode("frame")}
                aria-pressed={evidenceMode === "frame"}
              >
                대표 프레임
              </button>
              <button
                type="button"
                className={evidenceMode === "clip" ? "active" : ""}
                onClick={() => setEvidenceMode("clip")}
                aria-pressed={evidenceMode === "clip"}
              >
                미리보기 클립
              </button>
            </div>
            <div className="review-image">
              {evidenceMode === "frame" ? (
                <>
                  <img src={imageUrl} alt="실제 분석 후보 대표 프레임" />
                  {finalBox ? (
                    <span
                      className="review-box review-box--readonly"
                      aria-label="사용자가 저장한 최종 영역"
                      style={{
                        left: `${finalBox.x * 100}%`,
                        top: `${finalBox.y * 100}%`,
                        width: `${finalBox.width * 100}%`,
                        height: `${finalBox.height * 100}%`,
                      }}
                    >
                      <span>저장된 최종 영역</span>
                    </span>
                  ) : null}
                </>
              ) : (
                <video
                  className="candidate-clip"
                  src={clipUrl}
                  poster={imageUrl}
                  controls
                  preload="metadata"
                >
                  이 환경에서는 후보 미리보기 영상을 재생할 수 없습니다.
                </video>
              )}
              <div className="review-caption">
                <Badge tone={incident ? "success" : "warning"}>
                  {incident
                    ? persistentIssueLabel(incident.issueType)
                    : "미분류 장면 후보"}
                </Badge>
                <span>
                  {formatTimecode(candidate.representativeTimestampSeconds)} · {summary.sourceFileName}
                </span>
              </div>
            </div>
          </GlassPanel>

          <div className="review-evidence-cards">
            <GlassPanel>
              <PanelHeader eyebrow="PROVENANCE" title="실제 추출 근거" />
              <p>
                장면 변화와 선명도 기준으로 묶은 후보입니다. 이 점수만으로 도로 결함이나
                유지보수 유형을 확정하지 않습니다.
              </p>
              <div className="chip-row">
                <code>{formatTimecode(candidate.startSeconds)}</code>
                <code>{candidate.previewDurationSeconds.toFixed(1)}초 클립</code>
                <code>sha256:{summary.sourceSha256.slice(0, 12)}…</code>
              </div>
              <DefinitionList
                rows={[
                  { label: "장면 점수", value: candidate.sceneScore.toFixed(3) },
                  { label: "선명도", value: candidate.sharpnessScore.toFixed(1) },
                  {
                    label: "추출 트리거",
                    value: candidate.triggers.length ? candidate.triggers.join(", ") : "기록 없음",
                  },
                ]}
              />
            </GlassPanel>
            <GlassPanel>
              <PanelHeader eyebrow="LOCATION EVIDENCE" title="GPS·위치 근거" />
              <div className="gps-unavailable-note">
                {sourceLocation ? <MapPin size={22} /> : <MapPinOff size={22} />}
                <div>
                  <strong>
                    {sourceLocation ? "촬영 시작 좌표만 있음" : "GPS 자료 없음"}
                  </strong>
                  <p>
                    {sourceLocation
                      ? "영상 메타데이터의 실제 시작 좌표이며 이 후보의 위치나 이동 경로로 사용하지 않습니다."
                      : "좌표·도로명·이동 경로를 추정하거나 표시하지 않습니다."}
                  </p>
                </div>
              </div>
              <DefinitionList
                rows={[
                  {
                    label: "OCR",
                    value:
                      incident?.ocrText ||
                      review?.latestAnswer?.ocr_text ||
                      "확정값 없음",
                  },
                  {
                    label: "좌표",
                    value: sourceLocation
                      ? `촬영 시작 ${sourceLocation.latitude.toFixed(6)}, ${sourceLocation.longitude.toFixed(6)} · 후보 위치 미확정`
                      : "자료 없음",
                  },
                  { label: "도로명", value: "검증되지 않음" },
                ]}
              />
            </GlassPanel>
          </div>
        </div>

        <GlassPanel className="review-data">
          <PanelHeader
            eyebrow="EXTRACTED · ANSWER · FINAL"
            title={incident ? "영구 사건 판정" : "판정 변경 이력"}
            action={<Badge tone={reviewTone}>{reviewStatus}</Badge>}
          />

          <div className="review-table-wrap">
            <table className="data-table review-table">
              <thead>
                <tr>
                  <th>필드</th>
                  <th>추출 원본</th>
                  <th>사용자 답변</th>
                  <th>현재 저장값</th>
                </tr>
              </thead>
              <tbody>
                {beforeAfter.map(([field, origin, answer, final]) => (
                  <tr key={field}>
                    <th title={String(field)}>{field}</th>
                    <td className="mono" title={String(origin)}>{origin}</td>
                    <td title={String(answer)}>{answer}</td>
                    <td title={String(final)}><strong>{final}</strong></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="review-location-card">
            <Clock3 size={20} />
            <div>
              <strong>{formatTimecode(candidate.representativeTimestampSeconds)}</strong>
              <p>
                원본 영상 {summary.sourceFileName}의 대표 프레임 시각입니다.
              </p>
            </div>
            <Button variant="ghost" onClick={() => navigateWithCandidate("history")}>
              히스토리에서 열기
            </Button>
          </div>

          <div className="review-audit">
            <Tags size={20} />
            <div>
              <strong>
                {incident ? "영구 Incident 기록" : "권위 있는 라벨 기록"}
              </strong>
              {incident ? (
                <p>
                  <code>{incident.incidentId}</code> ·{" "}
                  {new Date(incident.updatedAt).toLocaleString("ko-KR")} · 답변 개정{" "}
                  {incident.answerRevision}
                </p>
              ) : (
                <p>
                  {review?.answeredAt
                    ? `${new Date(review.answeredAt).toLocaleString("ko-KR")} · 답변 개정 ${
                        review.answerRevision ?? 1
                      }`
                    : "이 후보에 저장된 사용자 답변이 없습니다."}
                </p>
              )}
              {reviewError ? <p className="inline-error">{reviewError}</p> : null}
              {incidentError ? <p className="inline-error">{incidentError}</p> : null}
            </div>
          </div>

          <div className="review-actions review-actions--linked">
            <Button variant="primary" onClick={() => navigateWithCandidate("label")}>
              <Tags size={18} /> 질문형 라벨링에서 검토
            </Button>
            <Button onClick={() => navigateWithCandidate("history")}>
              <History size={18} /> 같은 시각 히스토리
            </Button>
            <Button
              disabled
              title={
                sourceLocation
                  ? "촬영 시작 좌표만 있으며 이 후보의 위치는 확인되지 않았습니다."
                  : "GPS 원본이 연결되지 않았습니다."
              }
            >
              {sourceLocation ? <MapPin size={18} /> : <MapPinOff size={18} />}
              {sourceLocation ? "시작 좌표만" : "GPS 없음"}
            </Button>
            <Button disabled title="비식별화 파생본이 아직 생성되지 않았습니다.">
              {evidenceMode === "clip" ? <Video size={18} /> : <Image size={18} />} 공유본 없음
            </Button>
          </div>
        </GlassPanel>
      </div>
    </div>
  );
}
