import {
  ArrowRight,
  BrainCircuit,
  CircleAlert,
  FilePenLine,
  Play,
  Plus,
  Route,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { saveSelectedCandidateId } from "../candidateSelection";
import { realTunnelEvidenceImage, type ScreenId } from "../demo";
import { buildCandidateEvidenceUrl } from "../evidence";
import { issueLabelKo } from "../issueTypes";
import {
  createProject,
  useProjectCatalog,
} from "../projectCatalog";
import { reviewStorageKey, useReviewHistory } from "../reviewRecords";
import {
  clearRuntime,
  useRuntimeState,
  type AnalysisCandidate,
} from "../runtimeState";
import {
  Badge,
  Button,
  GlassPanel,
  PanelHeader,
  ProgressBar,
} from "../components/ui";

function formatBytes(value: number) {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
  if (value < 1_073_741_824) return `${(value / 1_048_576).toFixed(1)} MB`;
  return `${(value / 1_073_741_824).toFixed(2)} GB`;
}

function formatDuration(seconds: number) {
  const whole = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(whole / 3_600);
  const minutes = Math.floor((whole % 3_600) / 60);
  const remainder = whole % 60;
  return [hours, minutes, remainder]
    .map((value) => String(value).padStart(2, "0"))
    .join(":");
}

function candidateQueueTitle(candidate: AnalysisCandidate, index: number) {
  const proposal = candidate.aiInference?.predictions
    .slice()
    .sort((left, right) => right.confidence - left.confidence)[0];
  return proposal
    ? `${issueLabelKo(proposal.issueType) ?? proposal.issueType} 제안 · 사용자 확인 필요`
    : `장면 후보 ${index + 1} · 실제 도로 문제인지 확인`;
}

function candidateQueueScore(candidate: AnalysisCandidate) {
  const proposal = candidate.aiInference?.predictions
    .slice()
    .sort((left, right) => right.confidence - left.confidence)[0];
  return proposal
    ? `AI 점수 ${proposal.confidence.toFixed(2)}`
    : `장면 점수 ${candidate.sceneScore.toFixed(2)}`;
}

export function HomeScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const runtime = useRuntimeState();
  const projectCatalog = useProjectCatalog();
  const [createOpen, setCreateOpen] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [projectDescription, setProjectDescription] = useState("");
  const [projectSaving, setProjectSaving] = useState(false);
  const [projectError, setProjectError] = useState<string | null>(null);
  const summary = runtime.analysisSummary;
  const reviews = useReviewHistory(
    summary
      ? reviewStorageKey(summary.projectId, summary.jobId)
      : "trafficvisson:review:no-active-analysis",
  );
  const reviewedCount = reviews.length;
  const approvedCount = reviews.filter(
    (row) => row.trainingStatus === "approved",
  ).length;
  const remainingQuestions = Math.max(
    0,
    (summary?.candidateCount ?? 0) - reviewedCount,
  );
  const candidatePreview = summary?.candidates.slice(0, 7) ?? [];
  const featuredCandidates = candidatePreview.slice(0, 3);
  const activeProject = projectCatalog.projects.find(
    (project) => project.id === projectCatalog.activeProjectId,
  );
  const projectLabel = activeProject?.name ?? "현재 프로젝트";

  async function submitNewProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setProjectSaving(true);
    setProjectError(null);
    try {
      await createProject(projectName, projectDescription);
      clearRuntime();
      setProjectName("");
      setProjectDescription("");
      setCreateOpen(false);
      onNavigate("import");
    } catch (error) {
      setProjectError(
        error instanceof Error
          ? error.message
          : "새 프로젝트를 만들지 못했습니다.",
      );
    } finally {
      setProjectSaving(false);
    }
  }

  return (
    <div className="screen screen--home">
      <header className="screen-header">
        <div>
          <p className="eyebrow">{projectLabel}</p>
          <h1>{activeProject ? "점검 개요" : "도로 영상 점검 시작"}</h1>
          <p>원본 영상부터 사용자 검토, 모델, 보고서까지 한 프로젝트에서 추적합니다.</p>
        </div>
        <div className="header-actions">
          <Button variant="primary" onClick={() => onNavigate("import")}>
            {runtime.activeImport ? "입력 영상 보기" : "프로젝트 입력 열기"}
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setProjectError(null);
              setCreateOpen((value) => !value);
            }}
            aria-expanded={createOpen}
          >
            <Plus size={17} aria-hidden="true" /> 새 프로젝트
          </Button>
        </div>
      </header>

      {createOpen ? (
        <GlassPanel className="project-create-card">
          <form onSubmit={submitNewProject}>
            <div>
              <p className="eyebrow">NEW LOCAL PROJECT</p>
              <h2>분리된 프로젝트 만들기</h2>
              <p>
                이 프로젝트의 영상·답변·라벨·모델은 다른 프로젝트와 자동으로
                섞이지 않습니다.
              </p>
            </div>
            <label className="compact-field">
              <span>프로젝트 이름</span>
              <input
                autoFocus
                value={projectName}
                onChange={(event) => setProjectName(event.target.value)}
                maxLength={120}
                placeholder="예: 2026 영동선 3분기 점검"
                required
              />
            </label>
            <label className="compact-field">
              <span>설명 · 선택</span>
              <input
                value={projectDescription}
                onChange={(event) => setProjectDescription(event.target.value)}
                maxLength={2_000}
                placeholder="노선, 방향, 점검 회차"
              />
            </label>
            <div className="project-create-actions">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setCreateOpen(false)}
                disabled={projectSaving}
              >
                취소
              </Button>
              <Button
                type="submit"
                variant="primary"
                disabled={projectSaving || !projectName.trim()}
              >
                {projectSaving ? "저장 중…" : "프로젝트 만들고 입력 열기"}
              </Button>
            </div>
            {projectError ? (
              <p className="form-error" role="alert">{projectError}</p>
            ) : null}
          </form>
        </GlassPanel>
      ) : null}

      <section className="hero-metrics" aria-label="프로젝트 핵심 지표">
        <GlassPanel className="metric-card metric-card--primary">
          <div className="metric-topline">
            <span>후보 추출</span>
            <button
              type="button"
              className="metric-link-icon"
              aria-label="분석 결과 보기"
              onClick={() => onNavigate("analysis")}
            >
              <Play size={18} aria-hidden="true" />
            </button>
          </div>
          <strong>
            {runtime.status === "complete"
              ? 100
              : Math.round(runtime.progress)}
            <small>%</small>
          </strong>
          <ProgressBar
            value={runtime.status === "complete" ? 100 : runtime.progress}
            label={`후보 추출 진행률 ${
              runtime.status === "complete" ? 100 : Math.round(runtime.progress)
            }%`}
          />
          <p>{runtime.activeImport ? runtime.message : "검증된 영상 입력 대기"}</p>
        </GlassPanel>

        <GlassPanel className="metric-card">
          <div className="metric-topline">
            <span>검토 대기 후보</span>
            <CircleAlert size={18} aria-hidden="true" />
          </div>
          <strong>
            {summary ? remainingQuestions : "—"}
            {summary ? <small>건</small> : null}
          </strong>
          <div className="metric-inline">
            <Badge tone={remainingQuestions ? "warning" : "neutral"}>
              검토 {reviewedCount}
            </Badge>
            <Badge tone="neutral">확정 전</Badge>
          </div>
          <p>장면 후보는 사용자 답변 전까지 도로 사건으로 확정하지 않습니다.</p>
        </GlassPanel>

        <GlassPanel className="metric-card metric-card--blue">
          <div className="metric-topline">
            <span>프로젝트 모델</span>
            <BrainCircuit size={18} aria-hidden="true" />
          </div>
          <strong className="metric-status">학습 전</strong>
          <p>공통 초기 슬롯 · 장면 후보 추출 전용</p>
          <div className="metric-inline">
            <Badge tone="neutral">BASELINE</Badge>
            <span>확정 라벨 100건부터 학습 가능</span>
          </div>
        </GlassPanel>

        <GlassPanel className="metric-card">
          <div className="metric-topline">
            <span>보고서</span>
            <FilePenLine size={18} aria-hidden="true" />
          </div>
          <strong className="metric-status">
            {approvedCount ? `승인 사건 ${approvedCount}건` : "승인 사건 대기"}
          </strong>
          <p>AI 초안·한/글 편집은 명시적 검토 후 실행됩니다.</p>
          <div className="metric-inline">
            <Badge tone="neutral">자동 저장 안 함</Badge>
          </div>
        </GlassPanel>
      </section>

      <div className="home-grid">
        <GlassPanel className="trips-panel">
          <PanelHeader
            eyebrow="현재 입력"
            title="현재 주행 증거"
            action={
              <Button variant="ghost" onClick={() => onNavigate("history")}>
                히스토리 보기 <ArrowRight size={16} />
              </Button>
            }
          />
          {summary && featuredCandidates.length ? (
            <div
              className={`trip-evidence-showcase trip-evidence-showcase--count-${featuredCandidates.length}`}
              aria-label="현재 영상의 실제 후보 프레임"
            >
              {featuredCandidates.map((candidate, index) => (
                <button
                  type="button"
                  className={`trip-evidence-tile ${
                    index === 0 ? "trip-evidence-tile--primary" : ""
                  }`}
                  key={candidate.candidateId}
                  onClick={() => {
                    saveSelectedCandidateId(
                      summary.projectId,
                      summary.jobId,
                      candidate.candidateId,
                    );
                    onNavigate("label");
                  }}
                  aria-label={`${index + 1}번 실제 후보 프레임 검토 열기`}
                >
                  <img
                    src={buildCandidateEvidenceUrl(
                      summary.projectId,
                      summary.jobId,
                      candidate.candidateId,
                      "image",
                    )}
                    alt=""
                  />
                  <span className="trip-evidence-tile__shade" aria-hidden="true" />
                  <span className="trip-evidence-tile__copy">
                    <strong>실제 후보 {index + 1}</strong>
                    <small>
                      {formatDuration(candidate.representativeTimestampSeconds)}
                      {" · "}
                      장면 {candidate.sceneScore.toFixed(2)}
                    </small>
                  </span>
                </button>
              ))}
            </div>
          ) : null}
          {runtime.activeImport ? (
            <div className="trip-table-wrap">
              <table className="data-table trip-table">
                <thead>
                  <tr>
                    <th>영상</th>
                    <th>길이</th>
                    <th>크기</th>
                    <th>GPS</th>
                    <th>후보</th>
                    <th>상태</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>
                      <strong>{runtime.activeImport.fileName}</strong>
                      <span className="mono">
                        SHA {runtime.activeImport.sha256.slice(0, 12)}…
                      </span>
                    </td>
                    <td className="mono">
                      {formatDuration(runtime.activeImport.durationSeconds)}
                    </td>
                    <td>{formatBytes(runtime.activeImport.sizeBytes)}</td>
                    <td>
                      {summary?.sourceLocation
                        ? "촬영 시작 좌표만"
                        : "연결 자료 없음"}
                    </td>
                    <td>{summary?.candidateCount ?? "—"}</td>
                    <td>
                      <Badge
                        tone={
                          runtime.status === "complete"
                            ? "success"
                            : runtime.status === "failed"
                              ? "danger"
                              : "accent"
                        }
                      >
                        {runtime.message}
                      </Badge>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : (
            <div className="home-empty-row">
              아직 프로젝트에 검증된 영상이 없습니다. 네트워크 폴더나 로컬 파일을
              먼저 검사하세요.
            </div>
          )}
        </GlassPanel>

        <GlassPanel className="question-queue">
          <PanelHeader
            eyebrow="다음 검토"
            title="검토 대기"
            action={
              <Button
                variant="primary"
                onClick={() => onNavigate("label")}
                disabled={!summary}
              >
                {remainingQuestions ? `${remainingQuestions}건 검토 시작` : "검토 열기"}{" "}
                <ArrowRight size={16} />
              </Button>
            }
          />
          {candidatePreview.length ? (
            <ul className="question-list">
              {candidatePreview.map((candidate, index) => (
                <li key={candidate.candidateId}>
                  <button
                    type="button"
                    className="question-list-row"
                    onClick={() => {
                      if (summary) {
                        saveSelectedCandidateId(
                          summary.projectId,
                          summary.jobId,
                          candidate.candidateId,
                        );
                      }
                      onNavigate("label");
                    }}
                    aria-label={`${index + 1}번 후보 라벨링 열기`}
                  >
                    <img
                      src={buildCandidateEvidenceUrl(
                        summary!.projectId,
                        summary!.jobId,
                        candidate.candidateId,
                        "image",
                      )}
                      alt=""
                    />
                    <span>
                      <strong>{candidateQueueTitle(candidate, index)}</strong>
                      <small>
                        {formatDuration(candidate.representativeTimestampSeconds)} ·
                        {candidateQueueScore(candidate)}
                      </small>
                    </span>
                    <span>{index + 1}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <div className="question-sample">
              <img src={realTunnelEvidenceImage} alt="" />
              <div>
                <Badge tone="neutral">참고용 실제 프레임 · 검토 큐 아님</Badge>
                <strong>분석을 완료하면 실제 후보 질문이 여기에 나타납니다.</strong>
                <p>
                  이 이미지는 업로드된 천안논산 영상에서 추출했지만 현재 프로젝트
                  답변이나 학습 라벨로 사용되지 않습니다.
                </p>
              </div>
            </div>
          )}
          <div className="queue-summary">
            <Route size={18} aria-hidden="true" />
            <span>질문·원본 SHA-256·분석 작업·사용자 답변을 같은 증거 체인으로 묶습니다.</span>
          </div>
        </GlassPanel>
      </div>
    </div>
  );
}
