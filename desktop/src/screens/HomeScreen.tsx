import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  FileSpreadsheet,
  FileText,
  MapPinned,
  MessageSquareText,
  Plus,
  Route,
  ScanSearch,
  Settings2,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { saveSelectedCandidateId } from "../candidateSelection";
import type { ScreenId } from "../demo";
import { buildCandidateEvidenceUrl } from "../evidence";
import { issueLabelKo } from "../issueTypes";
import {
  pipelineStageLabel,
  pipelineStatusLabel,
  usePipelineUi,
  type PipelineJob,
  type PipelineStageName,
} from "../pipelineJobs";
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

const PIPELINE_GROUPS: ReadonlyArray<{
  title: string;
  detail: string;
  icon: typeof ShieldCheck;
  stages: readonly PipelineStageName[];
}> = [
  {
    title: "영상 넣기",
    detail: "권리·MIME·SHA·GPS",
    icon: ShieldCheck,
    stages: [
      "intake_validation",
      "sha256",
      "metadata_gps_preflight",
    ],
  },
  {
    title: "자동 분석",
    detail: "2fps·OCR·SAM2",
    icon: ScanSearch,
    stages: [
      "dense_index_2fps",
      "candidate_rescan_5_10fps",
      "detection_ocr",
      "sam2_segmentation_tracking",
      "temporal_merge",
      "similar_case_search",
      "vision_proposal",
    ],
  },
  {
    title: "질문 검토",
    detail: "Y/N/U/E · 1/2/3",
    icon: MessageSquareText,
    stages: ["question_queue"],
  },
  {
    title: "결과 자동 갱신",
    detail: "지도·CSV·HWPX",
    icon: FileText,
    stages: ["derived_outputs"],
  },
] as const;

function pipelineGroupIndex(job: PipelineJob | null) {
  if (!job) return -1;
  if (job.status === "completed") return PIPELINE_GROUPS.length;
  const index = PIPELINE_GROUPS.findIndex((group) =>
    group.stages.includes(job.currentStage),
  );
  return Math.max(0, index);
}

function pipelineTone(job: PipelineJob | null) {
  if (!job) return "neutral" as const;
  if (job.status === "completed") return "success" as const;
  if (job.status === "blocked" || job.status === "failed") {
    return "warning" as const;
  }
  return "accent" as const;
}

function pipelineHeadline(
  job: PipelineJob | null,
  remainingQuestions: number,
) {
  if (!job) {
    return "영상 하나만 넣으면 질문 준비까지 자동으로 진행됩니다";
  }
  if (job.status === "awaiting_review") {
    return remainingQuestions
      ? `준비된 질문 ${remainingQuestions}건만 확인하면 됩니다`
      : "질문 검토 결과를 확인하고 있습니다";
  }
  if (job.status === "completed") {
    return "지도·CSV·증거·보고서 초안이 자동으로 갱신되었습니다";
  }
  if (job.status === "blocked") {
    return job.outcomeCode === "partial"
      ? "CSV·증거는 저장했고 보고서 준비 경계에서 재개를 기다립니다"
      : "중단 지점을 보존했습니다. 필요한 모델팩을 확인해 주세요";
  }
  if (job.status === "failed") {
    return "실패한 단계에서 안전하게 멈췄습니다";
  }
  if (job.status === "cancelled") {
    return "취소된 지점에서 다시 시작할 수 있습니다";
  }
  return `${pipelineStageLabel(job.currentStage)} 처리 중입니다`;
}

export function HomeScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const runtime = useRuntimeState();
  const pipeline = usePipelineUi();
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
  const reviewedCandidateIds = new Set(
    reviews.flatMap((review) => (review.candidateId ? [review.candidateId] : [])),
  );
  const candidatePreview =
    summary?.candidates
      .filter((candidate) => !reviewedCandidateIds.has(candidate.candidateId))
      .slice(0, 3) ?? [];
  const hiddenQuestionCount = Math.max(
    0,
    remainingQuestions - candidatePreview.length,
  );
  const featuredCandidates = candidatePreview.slice(0, 3);
  const activeProject = projectCatalog.projects.find(
    (project) => project.id === projectCatalog.activeProjectId,
  );
  const projectLabel = activeProject?.name ?? "현재 프로젝트";
  const currentPipeline =
    pipeline.projectId === projectCatalog.activeProjectId
      ? pipeline.job
      : null;
  const hasDurableInput = Boolean(runtime.activeImport || currentPipeline);
  const activePipelineGroup = pipelineGroupIndex(currentPipeline);
  const pipelineActionScreen: ScreenId =
    currentPipeline?.status === "awaiting_review"
      ? "label"
      : currentPipeline?.status === "completed"
        ? "history"
        : "import";
  const pipelineActionLabel =
    currentPipeline?.status === "awaiting_review"
      ? "질문 검토 시작"
      : currentPipeline?.status === "completed"
        ? "결과 확인"
        : currentPipeline
          ? "현재 작업 보기"
          : "영상 넣고 자동 점검";
  const evidenceActionScreen: ScreenId =
    currentPipeline?.status === "completed" ? "history" : "import";
  const evidenceActionLabel = currentPipeline
    ? currentPipeline.status === "completed"
      ? "히스토리 보기"
      : "현재 작업 보기"
    : runtime.activeImport
      ? "입력 영상 보기"
      : "영상 선택";
  const profileSummary = currentPipeline
    ? [
        currentPipeline.profileSnapshot.reviewMode === "precision"
          ? "정밀 판정"
          : "빠른 판정",
        currentPipeline.profileSnapshot.reportProvider === "none"
          ? "문장 Provider 없음"
          : currentPipeline.profileSnapshot.reportProvider === "codex"
            ? "Codex"
            : currentPipeline.profileSnapshot.reportProvider === "claude"
              ? "Claude"
              : "Local 준비 중",
        "3·4분기 양식",
      ].join(" · ")
    : "프로젝트별 최초 1회 설정 · 영상마다 다시 묻지 않음";

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
          <h1>{activeProject ? "영상 한 번으로 도로점검" : "첫 도로점검 시작"}</h1>
          <p>영상 검증부터 질문 준비까지 자동으로 처리하고, 검토 뒤 결과를 한 번에 갱신합니다.</p>
        </div>
        <div className="header-actions">
          {hasDurableInput ? (
            <Button
              variant="primary"
              onClick={() => onNavigate(pipelineActionScreen)}
            >
              <Upload size={16} aria-hidden="true" />
              {pipelineActionLabel}
            </Button>
          ) : null}
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

      <section className="home-overview" aria-label="단일 자동 점검 흐름">
        <GlassPanel className="home-pipeline-hero">
          <div className="home-pipeline-hero__top">
            <div>
              <div className="home-pipeline-kicker">
                <Badge tone={pipelineTone(currentPipeline)}>
                  {currentPipeline
                    ? pipelineStatusLabel(currentPipeline)
                    : "영상 입력 대기"}
                </Badge>
                <span>
                  {currentPipeline
                    ? `${currentPipeline.progressPercent}% · ${currentPipeline.fileName}`
                    : "한 프로그램 · 하나의 자동 파이프라인"}
                </span>
              </div>
              <h2>{pipelineHeadline(currentPipeline, remainingQuestions)}</h2>
              <p>
                화면을 옮겨 다니며 중간 분석 버튼을 누를 필요가 없습니다.
                질문이 준비되면 프로그램이 바로 검토 화면으로 안내합니다.
              </p>
            </div>
          </div>

          <div className="home-pipeline-steps" aria-label="자동 처리 단계">
            {PIPELINE_GROUPS.map((group, index) => {
              const Icon = group.icon;
              const state =
                activePipelineGroup > index
                  ? "done"
                  : activePipelineGroup === index
                    ? currentPipeline?.status === "blocked" ||
                      currentPipeline?.status === "failed"
                      ? "attention"
                      : "active"
                    : "waiting";
              return (
                <div
                  className="home-pipeline-step"
                  data-state={state}
                  key={group.title}
                >
                  <span className="home-pipeline-step__icon">
                    {state === "done" ? (
                      <CheckCircle2 size={16} aria-hidden="true" />
                    ) : (
                      <Icon size={16} aria-hidden="true" />
                    )}
                  </span>
                  <span>
                    <strong>{group.title}</strong>
                    <small>{group.detail}</small>
                  </span>
                </div>
              );
            })}
          </div>

          <div className="home-pipeline-footer">
            <ProgressBar
              value={currentPipeline?.progressPercent ?? 0}
              tone={
                currentPipeline?.status === "failed"
                  ? "danger"
                  : currentPipeline?.status === "blocked"
                    ? "warning"
                    : currentPipeline?.status === "completed"
                      ? "success"
                      : "accent"
              }
              label={`자동 점검 진행률 ${currentPipeline?.progressPercent ?? 0}%`}
            />
            <span>
              <Settings2 size={14} aria-hidden="true" />
              {profileSummary}
            </span>
          </div>
        </GlassPanel>

        <GlassPanel className="home-summary-card">
          <div className="home-summary-card__top">
            <span>사용자 확인</span>
            <MessageSquareText size={18} aria-hidden="true" />
          </div>
          <strong>
            {summary ? remainingQuestions : "—"}
            {summary ? <small>건</small> : null}
          </strong>
          <p>
            {remainingQuestions
              ? "Y/N/U/E 또는 한 번 클릭으로 저장하고 다음 질문으로 이동"
              : "질문이 준비되면 자동으로 검토 화면을 엽니다."}
          </p>
          <div className="home-summary-card__badges">
            <Badge tone={reviewedCount ? "success" : "neutral"}>
              답변 {reviewedCount}
            </Badge>
            <Badge tone="neutral">승인 전 사건 0건</Badge>
          </div>
        </GlassPanel>

        <GlassPanel className="home-summary-card">
          <div className="home-summary-card__top">
            <span>자동 산출물</span>
            <FileSpreadsheet size={18} aria-hidden="true" />
          </div>
          <strong className="home-summary-card__status">
            {approvedCount ? `${approvedCount}건 반영` : "검토 후 준비"}
          </strong>
          <p>지도·raw/확정 CSV·증거 묶음·HWPX 편집 미리보기를 함께 갱신합니다.</p>
          <div className="home-summary-card__badges">
            <Badge tone={approvedCount ? "success" : "neutral"}>
              {approvedCount ? "자동 갱신됨" : "확정 대기"}
            </Badge>
            <Badge tone="neutral">HWP 적용은 승인 필요</Badge>
          </div>
        </GlassPanel>
      </section>

      <div className="home-grid">
        <GlassPanel className="trips-panel">
          <PanelHeader
            eyebrow="현재 입력"
            title="현재 영상과 근거"
            action={
              <Button
                variant="ghost"
                onClick={() => onNavigate(evidenceActionScreen)}
              >
                {evidenceActionLabel}{" "}
                <ArrowRight size={16} />
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
          ) : currentPipeline ? (
            <div className="home-intake-empty">
              <div className="home-intake-empty__start">
                <span className="home-intake-empty__icon">
                  <Route size={23} aria-hidden="true" />
                </span>
                <div>
                  <strong>{currentPipeline.fileName}</strong>
                  <p>
                    {pipelineStatusLabel(currentPipeline)} ·{" "}
                    {pipelineStageLabel(currentPipeline.currentStage)} ·{" "}
                    {currentPipeline.progressPercent}%에서 작업 기록을 복구했습니다.
                    새 영상을 다시 넣지 않습니다.
                  </p>
                </div>
                <Button
                  variant="primary"
                  onClick={() => onNavigate(evidenceActionScreen)}
                >
                  {evidenceActionLabel}{" "}
                  <ArrowRight size={16} aria-hidden="true" />
                </Button>
              </div>
              <div
                className="home-evidence-promises"
                aria-label="복구된 영속 작업 근거"
              >
                <span>
                  <ShieldCheck size={15} aria-hidden="true" />
                  영속 PipelineJob
                </span>
                <span>
                  <Route size={15} aria-hidden="true" />
                  작업 {currentPipeline.jobId.slice(0, 8)}…
                </span>
                <span>
                  <ScanSearch size={15} aria-hidden="true" />
                  {pipelineStageLabel(currentPipeline.currentStage)}
                </span>
                <span>
                  <FileText size={15} aria-hidden="true" />
                  레코드 SHA {currentPipeline.recordSha256.slice(0, 8)}…
                </span>
              </div>
            </div>
          ) : (
            <div className="home-intake-empty">
              <div className="home-intake-empty__start">
                <span className="home-intake-empty__icon">
                  <Upload size={23} aria-hidden="true" />
                </span>
                <div>
                  <strong>영상 파일 또는 링크를 한 번만 넣으세요</strong>
                  <p>
                    권리 확인과 분석 시작만 승인하면 질문 준비까지 자동으로
                    이어집니다.
                  </p>
                </div>
                <Button variant="primary" onClick={() => onNavigate("import")}>
                  영상 넣기 <ArrowRight size={16} aria-hidden="true" />
                </Button>
              </div>
              <div className="home-evidence-promises" aria-label="자동으로 보존되는 근거">
                <span>
                  <ShieldCheck size={15} aria-hidden="true" />
                  원본 SHA·프레임
                </span>
                <span>
                  <MapPinned size={15} aria-hidden="true" />
                  GPS 또는 없음 기록
                </span>
                <span>
                  <MessageSquareText size={15} aria-hidden="true" />
                  질문·답변 이력
                </span>
                <span>
                  <FileText size={15} aria-hidden="true" />
                  보고서 셀 위치
                </span>
              </div>
            </div>
          )}
        </GlassPanel>

        <GlassPanel className="question-queue">
            <PanelHeader
              eyebrow="지금 할 일"
              title={
                candidatePreview.length
                  ? "질문 검토"
                  : currentPipeline
                    ? pipelineStatusLabel(currentPipeline)
                    : hasDurableInput
                      ? "입력 영상 준비됨"
                      : "영상 입력 대기"
            }
            action={
              <Button
                variant="ghost"
                onClick={() =>
                  onNavigate(
                    candidatePreview.length
                      ? "label"
                      : currentPipeline
                        ? pipelineActionScreen
                        : "import",
                  )
                }
              >
                {remainingQuestions
                  ? "질문 열기"
                  : currentPipeline
                    ? "작업 열기"
                    : hasDurableInput
                      ? "입력 영상 보기"
                      : "영상 넣기"}{" "}
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
            <div className="home-next-empty">
              <div className="home-next-empty__lead">
                <CircleAlert size={18} aria-hidden="true" />
                <div>
                  <strong>
                    {currentPipeline
                      ? pipelineHeadline(currentPipeline, remainingQuestions)
                      : "지금은 영상만 선택하면 됩니다"}
                  </strong>
                  <p>
                    {currentPipeline
                      ? `${pipelineStageLabel(currentPipeline.currentStage)} · ${currentPipeline.progressPercent}% · 저장된 작업에서 이어갑니다.`
                      : "분석 중에는 다른 화면을 돌아다니거나 단계 버튼을 누르지 않습니다."}
                  </p>
                </div>
              </div>
              <ol className="home-user-actions">
                <li data-state="current">
                  <span>1</span>
                  <div>
                    <strong>
                      {currentPipeline
                        ? "현재 작업 상태 확인"
                        : "영상 권리 확인·분석 시작"}
                    </strong>
                    <small>
                      {currentPipeline ? "새 업로드 아님" : "이번 단계"}
                    </small>
                  </div>
                </li>
                <li>
                  <span>2</span>
                  <div>
                    <strong>준비된 질문에 답변</strong>
                    <small>자동 안내</small>
                  </div>
                </li>
                <li>
                  <span>3</span>
                  <div>
                    <strong>필요할 때 학습·HWP 적용 승인</strong>
                    <small>선택 동작</small>
                  </div>
                </li>
              </ol>
            </div>
          )}
          <div className="queue-summary">
            <Route size={18} aria-hidden="true" />
            <span>
              {candidatePreview.length
                ? hiddenQuestionCount
                  ? `대표 질문 ${candidatePreview.length}건 표시 · 나머지 ${hiddenQuestionCount}건은 검토 화면에서 이어집니다.`
                  : `남은 질문 ${candidatePreview.length}건을 모두 표시했습니다.`
                : "원본·질문·답변·지도·CSV·보고서를 하나의 작업 기록으로 묶습니다."}
            </span>
          </div>
        </GlassPanel>
      </div>
    </div>
  );
}
