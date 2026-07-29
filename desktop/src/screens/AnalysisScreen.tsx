import { useEffect, useRef, useState } from "react";
import {
  Activity,
  ChevronDown,
  Clock3,
  HardDrive,
  Play,
  RotateCcw,
  Square,
} from "lucide-react";
import type { ScreenId } from "../demo";
import {
  parseAnalysisJob,
  parseAnalysisJobList,
  toRestoredRuntime,
  type AnalysisJobResponse,
} from "../analysisJobs";
import {
  denseIndexStatusLabel,
  parseDenseIndexJob,
  parseDenseIndexJobList,
  type DenseIndexJobResponse,
} from "../denseIndexJobs";
import {
  requireActiveProjectId,
  useProjectCatalog,
} from "../projectCatalog";
import {
  pipelineStageLabel,
  pipelineStatusLabel,
  usePipelineUi,
  type PipelineJob,
  type PipelineStageStatus,
} from "../pipelineJobs";
import {
  candidateExtractionSteps,
  extractionStepState,
  formatArtifactBytes,
  runtimeStatusLabel,
  savedJobStatusLabel,
} from "../analysisPresentation";
import {
  restoreCompletedAnalysis,
  startAnalysis,
  updateAnalysis,
  useRuntimeState,
} from "../runtimeState";
import {
  Badge,
  Button,
  GlassPanel,
  PanelHeader,
  ProgressBar,
  StepState,
} from "../components/ui";

function stageIndex(stage: AnalysisJobResponse["stage"]) {
  if (stage === "completed") return 3;
  if (stage === "extracting_candidates") return 2;
  if (stage === "validating_source") return 1;
  return 0;
}

function jobMessage(job: AnalysisJobResponse) {
  if (job.status === "completed") {
    return `후보 ${job.result?.candidate_count ?? 0}건 · 실제 근거 산출물 ${
      job.result?.artifact_count ?? 0
    }개`;
  }
  if (job.status === "cancelled") return "사용자가 후보 추출을 취소했습니다.";
  if (job.status === "failed") return `후보 추출 실패 · ${job.error_code ?? "unknown"}`;
  if (job.stage === "validating_source") return "원본 해시와 파일 변경 여부 재검증";
  if (job.stage === "extracting_candidates") return "장면 변화·선명도 기반 후보와 근거 생성";
  if (job.stage === "cancelling") return "현재 미디어 읽기를 마친 뒤 취소";
  return "실제 후보 추출 작업이 대기열에 등록됨";
}

function pipelineJobTone(
  job: PipelineJob,
): "success" | "warning" | "danger" | "accent" | "neutral" {
  if (job.status === "completed") return "success";
  if (job.status === "failed") return "danger";
  if (
    job.status === "blocked" ||
    job.status === "cancelled" ||
    job.status === "cancelling"
  ) {
    return "warning";
  }
  if (job.status === "running" || job.status === "awaiting_review") {
    return "accent";
  }
  return "neutral";
}

function pipelineStageTone(
  status: PipelineStageStatus,
): "success" | "warning" | "danger" | "accent" | "neutral" {
  if (status === "success") return "success";
  if (status === "failed") return "danger";
  if (status === "running") return "accent";
  if (
    status === "blocked" ||
    status === "cancelled"
  ) {
    return "warning";
  }
  return "neutral";
}

function pipelineStageStatusLabel(status: PipelineStageStatus) {
  return {
    pending: "대기",
    running: "진행 중",
    success: "완료",
    blocked: "준비 필요",
    failed: "실패",
    skipped: "해당 없음",
    cancelled: "취소",
  }[status];
}

function pipelineHeadline(job: PipelineJob) {
  if (job.status === "awaiting_review") {
    return "질문 큐 준비 완료 · 이제 질문만 확인하세요";
  }
  if (job.status === "completed") {
    return job.outcomeCode === "zero_detections"
      ? "검지 후보 0건으로 정상 완료했습니다"
      : "지도·CSV·증거·보고서 초안까지 자동 갱신했습니다";
  }
  if (job.status === "blocked") {
    return job.outcomeCode === "partial"
      ? "완료된 산출물은 보존하고 보고서 준비 경계에서 멈췄습니다"
      : "필요한 모델팩이 없어 현재 단계에서 안전하게 멈췄습니다";
  }
  if (job.status === "failed") {
    return "실패를 빈 성공으로 저장하지 않고 재개 지점을 보존했습니다";
  }
  if (job.status === "cancelled") {
    return "취소 지점과 provenance를 보존했습니다";
  }
  if (job.status === "cancelling") {
    return "현재 작업을 안전하게 종료하고 있습니다";
  }
  if (job.status === "queued") {
    return "자동 점검 대기열에서 순서를 기다리고 있습니다";
  }
  return `${pipelineStageLabel(job.currentStage)} 자동 실행 중`;
}

function pipelineOutcomeCopy(job: PipelineJob) {
  if (job.status === "awaiting_review") {
    return "분석 단계는 끝났습니다. 준비된 질문에 답하면 승인 결과가 지도·CSV·증거·보고서 초안에 자동 반영됩니다.";
  }
  if (job.status === "completed") {
    return job.outcomeCode === "zero_detections"
      ? "분석은 정상 실행됐지만 검지 후보가 없었습니다. 모델 미설치나 분석 실패와 구분해 기록했습니다."
      : "검토 결과를 바탕으로 파생 산출물을 같은 PipelineJob provenance에 연결했습니다.";
  }
  if (job.status === "blocked") {
    return job.outcomeCode === "partial"
      ? "CSV와 증거는 저장됐습니다. 보고서 Provider 또는 양식을 복구하면 같은 단계에서 이어갑니다."
      : "환경 상태에서 필요한 모델팩을 확인한 뒤 영상 가져오기 화면에서 같은 작업을 재개할 수 있습니다.";
  }
  if (job.status === "failed") {
    return `오류 ${job.errorCode ?? "unknown"} · 완료된 단계 기록은 유지되며 같은 입력과 설정은 멱등 처리됩니다.`;
  }
  if (job.status === "cancelled" || job.status === "cancelling") {
    return "부분 성공을 전체 성공으로 표시하지 않습니다. 새 업로드 없이 저장된 작업 기록에서 재개할 수 있습니다.";
  }
  return "현재 화면을 열어 두지 않아도 단계는 순서대로 진행됩니다. 중간 분석 버튼을 누를 필요가 없습니다.";
}

function PipelineAnalysisOverview({
  job,
  onNavigate,
}: {
  job: PipelineJob;
  onNavigate: (screen: ScreenId) => void;
}) {
  const completedStageCount = job.stages.filter(
    ({ status }) => status === "success" || status === "skipped",
  ).length;
  const action =
    job.status === "awaiting_review"
      ? { label: "준비된 질문 검토", screen: "label" as ScreenId }
      : job.status === "completed"
        ? {
            label:
              job.outcomeCode === "zero_detections"
                ? "프로젝트 홈 보기"
                : "자동 갱신 결과 보기",
            screen:
              job.outcomeCode === "zero_detections"
                ? ("home" as ScreenId)
                : ("history" as ScreenId),
          }
        : ["blocked", "failed", "cancelled"].includes(job.status)
          ? { label: "입력·재개 상태 보기", screen: "import" as ScreenId }
          : null;

  return (
    <div className="screen screen--analysis screen--pipeline-analysis">
      <header className="screen-header">
        <div>
          <p className="eyebrow">PIPELINE JOB {job.jobId.slice(0, 8)}</p>
          <h1>자동 분석 진행</h1>
          <p>
            영상 입력 뒤 하나의 영속 작업이 질문 준비까지 자동으로 이어집니다.
          </p>
        </div>
        <div className="header-actions">
          {action ? (
            <Button
              variant={
                job.status === "awaiting_review" ? "primary" : "ghost"
              }
              onClick={() => onNavigate(action.screen)}
            >
              {action.label}
            </Button>
          ) : (
            <Badge tone={pipelineJobTone(job)}>
              자동 실행 · 화면 조작 불필요
            </Badge>
          )}
        </div>
      </header>

      <div className="analysis-summary">
        <GlassPanel className="analysis-hero">
          <div className="analysis-hero-top">
            <div>
              <Badge tone={pipelineJobTone(job)}>
                {pipelineStatusLabel(job)}
              </Badge>
              <h2>{pipelineHeadline(job)}</h2>
              <p>{job.fileName}</p>
            </div>
            <strong>
              {job.progressPercent.toFixed(0)}
              <small>%</small>
            </strong>
          </div>
          <ProgressBar
            value={job.progressPercent}
            label={`자동 점검 진행률 ${job.progressPercent}%`}
          />
          <div className="analysis-meta">
            <span>
              <Activity size={16} /> {pipelineStageLabel(job.currentStage)}
            </span>
            <span>
              <Clock3 size={16} /> 재시도 {job.retryCount} · 재개{" "}
              {job.resumeCount}
            </span>
            <span>
              <HardDrive size={16} /> 입력·설정 해시 기반 멱등 처리
            </span>
          </div>
        </GlassPanel>

        <GlassPanel className="stat-tile">
          <Activity size={20} />
          <span>자동 단계</span>
          <strong>
            {completedStageCount}
            <small>/{job.stages.length}</small>
          </strong>
          <small>완료 또는 해당 없음</small>
        </GlassPanel>
        <GlassPanel className="stat-tile">
          <Clock3 size={20} />
          <span>재시도</span>
          <strong>{job.retryCount}</strong>
          <small>실패 시 단계 단위 기록</small>
        </GlassPanel>
        <GlassPanel className="stat-tile">
          <HardDrive size={20} />
          <span>재시작 복구</span>
          <strong>{job.recoveredAfterRestart ? "복구" : job.resumeCount}</strong>
          <small>정확한 저장 단계에서 재개</small>
        </GlassPanel>
      </div>

      <div className="analysis-layout analysis-layout--pipeline">
        <GlassPanel className="pipeline-panel pipeline-job-panel">
          <PanelHeader
            eyebrow="ONE AUTOMATIC PIPELINE"
            title="영상부터 결과까지 한 작업"
            action={
              <Badge tone="neutral">
                {completedStageCount}/{job.stages.length}
              </Badge>
            }
          />
          <ol
            className="pipeline-job-stage-grid"
            aria-label="자동 파이프라인 단계"
          >
            {job.stages.map((stage, index) => (
              <li
                key={stage.name}
                className={
                  stage.name === job.currentStage
                    ? "pipeline-job-stage pipeline-job-stage--current"
                    : "pipeline-job-stage"
                }
                aria-current={
                  stage.name === job.currentStage ? "step" : undefined
                }
              >
                <span className="step-number">{index + 1}</span>
                <span className="pipeline-job-stage__copy">
                  <strong>{pipelineStageLabel(stage.name)}</strong>
                  <small>
                    시도 {stage.attempts}
                    {stage.errorCode ? ` · ${stage.errorCode}` : ""}
                  </small>
                </span>
                <Badge tone={pipelineStageTone(stage.status)}>
                  {pipelineStageStatusLabel(stage.status)}
                </Badge>
              </li>
            ))}
          </ol>
        </GlassPanel>

        <GlassPanel className="resource-panel pipeline-job-evidence">
          <PanelHeader eyebrow="PROVENANCE" title="현재 작업 기록" />
          <div className="resource-grid">
            <div>
              <span>현재 단계</span>
              <strong>{pipelineStageLabel(job.currentStage)}</strong>
            </div>
            <div>
              <span>판정 모드</span>
              <strong>
                {job.profileSnapshot.reviewMode === "precision"
                  ? "정밀 판정"
                  : "빠른 판정"}
              </strong>
            </div>
            <div>
              <span>질문 정책</span>
              <strong>
                {
                  {
                    learning_assist: "학습 보조",
                    full_review: "전수 검토",
                    quick_review: "빠른 검토",
                  }[job.profileSnapshot.questionPolicy]
                }
              </strong>
            </div>
            <div>
              <span>작업 ID</span>
              <strong className="mono">{job.jobId.slice(0, 12)}…</strong>
            </div>
            <div>
              <span>프로필</span>
              <strong>revision {job.profileSnapshot.revision}</strong>
            </div>
            <div>
              <span>레코드 SHA</span>
              <strong className="mono">
                {job.recordSha256.slice(0, 12)}…
              </strong>
            </div>
          </div>
          <div className="pipeline-journey-strip" aria-label="자동 처리 흐름">
            <span>영상</span>
            <i aria-hidden="true">→</i>
            <span>자동 분석</span>
            <i aria-hidden="true">→</i>
            <span>질문 검토</span>
            <i aria-hidden="true">→</i>
            <span>결과 자동 갱신</span>
          </div>
          <div
            className={`notice ${
              job.status === "failed" ? "notice--danger" : ""
            }`}
            role={job.status === "failed" ? "alert" : "status"}
          >
            <Activity size={19} />
            <div>
              <strong>{pipelineStatusLabel(job)}</strong>
              <p>{pipelineOutcomeCopy(job)}</p>
            </div>
          </div>
          <p className="pipeline-job-boundary">
            사용자에게 남는 동작은 권리·분석 시작 승인, 질문 답변, 선택적
            모델 학습 승인, HWP 실제 적용 승인뿐입니다.
          </p>
        </GlassPanel>
      </div>
    </div>
  );
}

function SavedAnalysisJobs({
  jobs,
  loading,
  activeJobId,
  onOpen,
}: {
  jobs: AnalysisJobResponse[];
  loading: boolean;
  activeJobId: string | null;
  onOpen: (job: AnalysisJobResponse) => void;
}) {
  return (
    <GlassPanel className="analysis-records-panel">
      <PanelHeader
        eyebrow="PERSISTED ANALYSIS"
        title="저장된 영상 분석"
        action={
          <Badge tone={jobs.length ? "success" : "neutral"}>
            {loading ? "불러오는 중" : `${jobs.length}개 작업`}
          </Badge>
        }
      />
      {jobs.length ? (
        <div className="analysis-records-table-wrap">
          <table className="data-table analysis-records-table">
            <thead>
              <tr>
                <th>영상</th>
                <th>상태</th>
                <th>후보</th>
                <th>갱신 시각</th>
                <th>
                  <span className="sr-only">작업 열기</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((saved) => (
                <tr
                  key={saved.job_id}
                  className={
                    saved.job_id === activeJobId
                      ? "analysis-records-row--active"
                      : undefined
                  }
                >
                  <td>
                    <strong>{saved.file_name}</strong>
                    <span className="mono">
                      {saved.source_sha256.slice(0, 12)}… ·{" "}
                      {saved.job_id.slice(0, 8)}
                    </span>
                  </td>
                  <td>
                    <Badge
                      tone={
                        saved.status === "completed"
                          ? "success"
                          : saved.status === "failed"
                            ? "danger"
                            : saved.status === "cancelled"
                              ? "warning"
                              : "accent"
                      }
                    >
                      {savedJobStatusLabel(saved.status)}
                    </Badge>
                  </td>
                  <td>{saved.result?.candidate_count ?? "—"}</td>
                  <td>
                    {new Intl.DateTimeFormat("ko-KR", {
                      month: "2-digit",
                      day: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                    }).format(new Date(saved.updated_at))}
                  </td>
                  <td>
                    <Button
                      variant="ghost"
                      disabled={
                        saved.status !== "completed" ||
                        saved.result === null ||
                        saved.job_id === activeJobId
                      }
                      onClick={() => onOpen(saved)}
                    >
                      {saved.job_id === activeJobId
                        ? "현재 작업"
                        : "증거 열기"}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="analysis-records-empty">
          {loading
            ? "로컬 프로젝트의 분석 기록을 확인하고 있습니다."
            : "아직 저장된 후보 추출 작업이 없습니다."}
        </div>
      )}
    </GlassPanel>
  );
}

export function AnalysisScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const runtime = useRuntimeState();
  const projectCatalog = useProjectCatalog();
  const pipeline = usePipelineUi();
  const currentPipelineJob =
    pipeline.projectId === projectCatalog.activeProjectId
      ? pipeline.job
      : null;
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [job, setJob] = useState<AnalysisJobResponse | null>(null);
  const [savedJobs, setSavedJobs] = useState<AnalysisJobResponse[]>([]);
  const [savedJobsLoading, setSavedJobsLoading] = useState(false);
  const [jobError, setJobError] = useState<string | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [denseJob, setDenseJob] =
    useState<DenseIndexJobResponse | null>(null);
  const [denseError, setDenseError] = useState<string | null>(null);
  const [denseRequesting, setDenseRequesting] = useState(false);
  const [denseSamplingFps, setDenseSamplingFps] =
    useState<1 | 2 | 3 | 4 | 5>(2);
  const pollGeneration = useRef(0);
  const densePollGeneration = useRef(0);
  const running = runtime.status === "running" || runtime.status === "queued";
  const cancelled = runtime.status === "cancelled";
  const progress = runtime.progress;
  const denseActive = Boolean(
    denseJob &&
      ["queued", "running", "cancelling"].includes(denseJob.status),
  );

  function syncJob(next: AnalysisJobResponse) {
    setJob(next);
    const message = jobMessage(next);
    if (next.status === "completed" && next.result) {
      const restored = toRestoredRuntime(next);
      updateAnalysis({
        status: "complete",
        progress: 100,
        stage: 3,
        jobId: next.job_id,
        message,
        analysisSummary: restored.analysisSummary,
      });
      return;
    }
    if (next.status === "failed" || next.status === "cancelled") {
      updateAnalysis({
        status: next.status,
        progress: next.progress_percent ?? 0,
        stage: stageIndex(next.stage),
        jobId: next.job_id,
        message,
      });
      return;
    }
    startAnalysis(next.job_id);
    updateAnalysis({
      status: next.status === "queued" ? "queued" : "running",
      progress: next.progress_percent ?? 0,
      stage: stageIndex(next.stage),
      jobId: next.job_id,
      message,
    });
  }

  useEffect(() => {
    if (!window.trafficSuperVision) return;
    let active = true;
    setSavedJobsLoading(true);
    void (async () => {
      let projectId = runtime.activeImport?.projectId;
      if (!projectId) {
        projectId = await requireActiveProjectId();
      }
      if (!projectId) {
        if (active) setSavedJobs([]);
        return;
      }
      const response = await window.trafficSuperVision!.analysis.list({
        projectId,
      });
      if (active) setSavedJobs(parseAnalysisJobList(response));
    })()
      .catch((error) => {
        if (active) {
          setJobError(
            error instanceof Error
              ? error.message
              : "저장된 분석 기록을 읽지 못했습니다.",
          );
        }
      })
      .finally(() => {
        if (active) setSavedJobsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [
    runtime.activeImport?.projectId,
    runtime.analysisSummary?.jobId,
  ]);

  useEffect(() => {
    if (
      !window.trafficSuperVision ||
      !runtime.activeImport ||
      !runtime.analysisSummary
    ) {
      setDenseJob(null);
      return;
    }
    let active = true;
    void window.trafficSuperVision.denseIndex
      .list({ projectId: runtime.activeImport.projectId })
      .then(parseDenseIndexJobList)
      .then((jobs) => {
        if (!active) return;
        const matching =
          jobs.find(
            (item) =>
              item.analysis_job_id === runtime.analysisSummary?.jobId,
          ) ?? null;
        setDenseJob(matching);
        setDenseSamplingFps(matching?.sampling_fps ?? 2);
        setDenseError(null);
      })
      .catch((error) => {
        if (!active) return;
        setDenseError(
          error instanceof Error
            ? error.message
            : "밀집 프레임 작업 기록을 읽지 못했습니다.",
        );
      });
    return () => {
      active = false;
    };
  }, [
    runtime.activeImport?.projectId,
    runtime.analysisSummary?.jobId,
  ]);

  useEffect(() => {
    if (
      !window.trafficSuperVision ||
      !runtime.activeImport ||
      !runtime.jobId ||
      !["queued", "running"].includes(runtime.status)
    ) {
      return;
    }
    const generation = pollGeneration.current + 1;
    pollGeneration.current = generation;
    let timer: number | undefined;
    let disposed = false;

    const poll = async () => {
      try {
        const next = parseAnalysisJob(
          await window.trafficSuperVision!.analysis.get({
            projectId: runtime.activeImport!.projectId,
            jobId: runtime.jobId!,
          }),
        );
        if (disposed || generation !== pollGeneration.current) return;
        syncJob(next);
        if (["queued", "running", "cancelling"].includes(next.status)) {
          timer = window.setTimeout(poll, 750);
        }
      } catch (error) {
        if (!disposed) {
          setJobError(
            error instanceof Error ? error.message : "분석 작업 상태를 읽지 못했습니다.",
          );
        }
      }
    };
    void poll();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runtime.activeImport, runtime.jobId, runtime.status]);

  useEffect(() => {
    if (
      !window.trafficSuperVision ||
      !runtime.activeImport ||
      !denseJob ||
      !["queued", "running", "cancelling"].includes(denseJob.status)
    ) {
      return;
    }
    const generation = densePollGeneration.current + 1;
    densePollGeneration.current = generation;
    let timer: number | undefined;
    let disposed = false;
    const projectId = runtime.activeImport.projectId;
    const denseJobId = denseJob.job_id;

    const poll = async () => {
      try {
        const next = parseDenseIndexJob(
          await window.trafficSuperVision!.denseIndex.get({
            projectId,
            jobId: denseJobId,
          }),
        );
        if (disposed || generation !== densePollGeneration.current) return;
        setDenseJob(next);
        if (["queued", "running", "cancelling"].includes(next.status)) {
          timer = window.setTimeout(poll, 900);
        }
      } catch (error) {
        if (!disposed) {
          setDenseError(
            error instanceof Error
              ? error.message
              : "밀집 프레임 작업 상태를 읽지 못했습니다.",
          );
        }
      }
    };
    void poll();
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [
    denseJob?.job_id,
    denseJob?.status,
    runtime.activeImport?.projectId,
  ]);

  async function startActualAnalysis() {
    if (!runtime.activeImport) return;
    if (!window.trafficSuperVision) {
      setJobError("실제 후보 추출은 설치형 Electron 앱에서 실행할 수 있습니다.");
      return;
    }
    if (!runtime.activeImport.hashJobId) {
      setJobError("이 입력에는 브리지가 발급한 완료 해시 작업 ID가 없습니다. 영상을 다시 검사하세요.");
      return;
    }
    setRequesting(true);
    setJobError(null);
    try {
      const next = parseAnalysisJob(
        await window.trafficSuperVision.analysis.start({
          projectId: runtime.activeImport.projectId,
          snapshotId: runtime.activeImport.snapshotId,
          hashJobId: runtime.activeImport.hashJobId,
          fileName: runtime.activeImport.fileName,
          sha256Lower: runtime.activeImport.sha256,
        }),
      );
      syncJob(next);
    } catch (error) {
      setJobError(error instanceof Error ? error.message : "후보 추출을 시작하지 못했습니다.");
    } finally {
      setRequesting(false);
    }
  }

  async function cancelActualAnalysis() {
    if (!window.trafficSuperVision || !runtime.activeImport || !runtime.jobId) return;
    setRequesting(true);
    setJobError(null);
    try {
      const next = parseAnalysisJob(
        await window.trafficSuperVision.analysis.cancel({
          projectId: runtime.activeImport.projectId,
          jobId: runtime.jobId,
        }),
      );
      syncJob(next);
    } catch (error) {
      setJobError(error instanceof Error ? error.message : "후보 추출을 취소하지 못했습니다.");
    } finally {
      setRequesting(false);
    }
  }

  async function startDenseIndex() {
    if (
      !window.trafficSuperVision ||
      !runtime.activeImport ||
      !runtime.analysisSummary ||
      runtime.status !== "complete"
    ) {
      setDenseError(
        "현재 런타임에서 완료된 영상 분석을 먼저 열어야 합니다.",
      );
      return;
    }
    setDenseRequesting(true);
    setDenseError(null);
    try {
      const next = parseDenseIndexJob(
        await window.trafficSuperVision.denseIndex.start({
          projectId: runtime.activeImport.projectId,
          analysisJobId: runtime.analysisSummary.jobId,
          samplingFps: denseSamplingFps,
          confirmReviewOnly: true,
        }),
      );
      setDenseJob(next);
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "밀집 프레임 준비를 시작하지 못했습니다.";
      setDenseError(
        message.includes("source_unavailable_reselect_required")
          ? "재시작 후 원본 경로를 보존하지 않습니다. 영상을 다시 선택하고 해시 검증을 완료해 주세요."
          : message,
      );
    } finally {
      setDenseRequesting(false);
    }
  }

  async function cancelDenseIndex() {
    if (
      !window.trafficSuperVision ||
      !runtime.activeImport ||
      !denseJob
    ) {
      return;
    }
    setDenseRequesting(true);
    setDenseError(null);
    try {
      setDenseJob(
        parseDenseIndexJob(
          await window.trafficSuperVision.denseIndex.cancel({
            projectId: runtime.activeImport.projectId,
            jobId: denseJob.job_id,
          }),
        ),
      );
    } catch (error) {
      setDenseError(
        error instanceof Error
          ? error.message
          : "밀집 프레임 준비를 취소하지 못했습니다.",
      );
    } finally {
      setDenseRequesting(false);
    }
  }

  function openSavedAnalysis(saved: AnalysisJobResponse) {
    try {
      const restored = toRestoredRuntime(saved);
      restoreCompletedAnalysis(
        restored.activeImport,
        restored.analysisSummary,
      );
      setJob(saved);
      setJobError(null);
    } catch (error) {
      setJobError(
        error instanceof Error
          ? error.message
          : "저장된 분석 작업을 열지 못했습니다.",
      );
    }
  }

  if (currentPipelineJob) {
    return (
      <PipelineAnalysisOverview
        job={currentPipelineJob}
        onNavigate={onNavigate}
      />
    );
  }

  if (!runtime.activeImport) {
    return (
      <div className="screen screen--analysis">
        <header className="screen-header">
          <div>
            <p className="eyebrow">LOCAL ANALYSIS</p>
            <h1>분석</h1>
            <p>안정성·스트림·SHA-256 검증을 통과한 입력만 분석할 수 있습니다.</p>
          </div>
        </header>
        <GlassPanel className="analysis-empty">
          <Activity size={40} />
          <Badge tone="warning">검증된 입력 없음</Badge>
          <h2>먼저 영상을 가져오세요</h2>
          <p>분석 화면에 들어온 것만으로 후보 추출 작업을 시작하지 않습니다.</p>
          <Button variant="primary" onClick={() => onNavigate("import")}>
            영상 가져오기로 이동
          </Button>
        </GlassPanel>
        <SavedAnalysisJobs
          jobs={savedJobs}
          loading={savedJobsLoading}
          activeJobId={runtime.jobId}
          onOpen={openSavedAnalysis}
        />
      </div>
    );
  }

  const statusLabel = runtimeStatusLabel(runtime.status, job?.status);
  const statusTone =
    runtime.status === "complete"
      ? "success"
      : runtime.status === "failed"
        ? "danger"
        : cancelled
          ? "warning"
          : running
            ? "accent"
            : "neutral";
  const displayedStage =
    job?.stage ??
    (runtime.status === "complete"
      ? "completed"
      : runtime.status === "queued"
        ? "queued"
        : runtime.status === "running"
          ? "extracting_candidates"
          : null);

  return (
    <div className="screen screen--analysis">
      <header className="screen-header">
        <div>
          <p className="eyebrow">
            ANALYSIS JOB {runtime.jobId ? runtime.jobId.slice(0, 8) : "NOT STARTED"}
          </p>
          <h1>후보 추출</h1>
          <p>브리지가 반환한 실제 작업 상태·진행률·근거 산출물만 표시합니다.</p>
        </div>
        <div className="header-actions">
          {cancelled ? (
            <Button
              variant="primary"
              onClick={startActualAnalysis}
              disabled={requesting}
            >
              <RotateCcw size={17} /> 새 후보 추출 작업 시작
            </Button>
          ) : (
            <>
              <Button
                onClick={startActualAnalysis}
                disabled={running || requesting || runtime.status === "complete"}
                title={running ? "현재 엔진은 안전한 일시정지를 지원하지 않습니다." : undefined}
              >
                {running ? <Activity size={17} /> : <Play size={17} />}
                {running ? "작업 중 · 일시정지 미지원" : runtime.status === "complete" ? "추출 완료" : "후보 추출 시작"}
              </Button>
              <Button
                variant="danger"
                onClick={cancelActualAnalysis}
                disabled={!running || requesting}
              >
                <Square size={15} /> 작업 취소
              </Button>
            </>
          )}
        </div>
      </header>

      {jobError ? (
        <div className="notice notice--danger" role="alert">
          <Activity size={19} />
          <div>
            <strong>실제 작업을 완료하지 못했습니다</strong>
            <p>{jobError}</p>
          </div>
        </div>
      ) : null}

      <div className="analysis-summary">
        <GlassPanel className="analysis-hero">
          <div className="analysis-hero-top">
            <div>
              <Badge tone={statusTone}>{statusLabel}</Badge>
              <h2>
                {runtime.status === "intake_ready"
                    ? "실제 후보 추출을 시작할 준비가 됐습니다"
                    : runtime.message}
              </h2>
              <p>{runtime.activeImport.fileName}</p>
            </div>
            <strong>
              {job?.progress_percent == null && running ? "—" : progress.toFixed(1)}
              <small>{job?.progress_percent == null && running ? "" : "%"}</small>
            </strong>
          </div>
          <ProgressBar value={progress} label={`분석 진행률 ${progress.toFixed(1)}%`} />
          <div className="analysis-meta">
            <span>
              <Clock3 size={16} />{" "}
              {job
                ? `브리지 단계 · ${job.stage}`
                : runtime.analysisSummary
                  ? "브리지 단계 · 완료"
                  : "작업 시작 전"}
            </span>
            <span><Activity size={16} /> 장면 변화·선명도 기반 후보 추출</span>
            <span><HardDrive size={16} /> 원본 읽기 전용</span>
          </div>
        </GlassPanel>

        <GlassPanel className="stat-tile">
          <Activity size={20} />
          <span>장면 후보</span>
          <strong>{runtime.analysisSummary ? runtime.analysisSummary.candidateCount : "—"}</strong>
          <small>{runtime.analysisSummary ? "중복 구간으로 묶인 후보" : "완료 결과 없음"}</small>
        </GlassPanel>
        <GlassPanel className="stat-tile">
          <HardDrive size={20} />
          <span>근거 산출물</span>
          <strong>{runtime.analysisSummary ? runtime.analysisSummary.artifactCount : "—"}</strong>
          <small>{runtime.analysisSummary ? "JPEG + MP4 미리보기" : "완료 결과 없음"}</small>
        </GlassPanel>
        <GlassPanel className="stat-tile">
          <Clock3 size={20} />
          <span>근거 용량</span>
          <strong>
            {runtime.analysisSummary
              ? formatArtifactBytes(runtime.analysisSummary.totalArtifactBytes)
              : "—"}
          </strong>
          <small>검증된 후보 이미지·클립 합계</small>
        </GlassPanel>
      </div>

      <div className="analysis-layout">
        <GlassPanel className="pipeline-panel">
          <PanelHeader eyebrow="CANDIDATE EXTRACTION" title="실제 브리지 단계" />
          <ol className="pipeline-list">
            {candidateExtractionSteps.map((step, index) => {
              const state = extractionStepState(
                index,
                displayedStage,
                job?.status ?? runtime.status,
              );
              return (
                <li key={step.stage} className={state === "active" ? "active" : ""}>
                  <span className="step-number">{index + 1}</span>
                  <span className="step-name">{step.label}</span>
                  <StepState state={state} />
                </li>
              );
            })}
          </ol>
        </GlassPanel>

        <GlassPanel className="resource-panel">
          <PanelHeader eyebrow="PROVENANCE" title="실제 작업 정보" />
          <div className="resource-grid">
            <div>
              <span>입력 길이</span>
              <strong>{runtime.activeImport.durationSeconds.toFixed(1)}초</strong>
            </div>
            <div>
              <span>입력 용량</span>
              <strong>{formatArtifactBytes(runtime.activeImport.sizeBytes)}</strong>
            </div>
            <div>
              <span>SHA-256</span>
              <strong className="mono">{runtime.activeImport.sha256.slice(0, 12)}…</strong>
            </div>
          </div>
          <div className="notice">
            <Activity size={19} />
            <div>
              <strong>후보 추출 결과입니다.</strong>
              <p>장면과 선명도를 기준으로 검토할 구간과 근거를 만들며, 사용자 검토 전에는 도로 문제로 확정하지 않습니다.</p>
            </div>
          </div>

          <section
            className="dense-index-control"
            aria-labelledby="dense-index-title"
          >
            <div className="dense-index-control__head">
              <div>
                <span className="eyebrow">REVIEW FRAME INDEX</span>
                <strong id="dense-index-title">
                  밀집 프레임 준비
                </strong>
              </div>
              <Badge
                tone={
                  denseJob?.status === "completed"
                    ? "success"
                    : denseJob?.status === "failed"
                      ? "danger"
                      : denseActive
                        ? "accent"
                        : "neutral"
                }
              >
                {denseIndexStatusLabel(denseJob)}
              </Badge>
            </div>
            <div className="dense-index-control__actions">
              <label>
                <span>샘플링</span>
                <select
                  value={denseSamplingFps}
                  disabled={denseActive || denseRequesting}
                  onChange={(event) =>
                    setDenseSamplingFps(
                      Number(event.target.value) as 1 | 2 | 3 | 4 | 5,
                    )
                  }
                >
                  {[1, 2, 3, 4, 5].map((fps) => (
                    <option key={fps} value={fps}>
                      {fps} FPS
                    </option>
                  ))}
                </select>
              </label>
              {denseActive ? (
                <Button
                  variant="danger"
                  onClick={cancelDenseIndex}
                  disabled={denseRequesting}
                >
                  <Square size={14} /> 준비 취소
                </Button>
              ) : (
                <Button
                  onClick={startDenseIndex}
                  disabled={
                    denseRequesting ||
                    runtime.status !== "complete" ||
                    !runtime.analysisSummary ||
                    (denseJob?.status === "completed" &&
                      denseJob.sampling_fps === denseSamplingFps)
                  }
                >
                  <Play size={14} />
                  {denseJob?.status === "completed" &&
                  denseJob.sampling_fps === denseSamplingFps
                    ? "준비 완료"
                    : "밀집 프레임 준비"}
                </Button>
              )}
            </div>
            {denseJob?.progress_state === "indeterminate" ? (
              <div
                className="dense-index-control__progress"
                role="status"
                aria-live="polite"
              >
                <Activity className="spin" size={14} />
                프레임 수는 완료 후 확정됩니다 · 임의 진행률 표시 안 함
              </div>
            ) : null}
            {denseError ? (
              <p className="dense-index-control__error" role="alert">
                {denseError}
              </p>
            ) : null}
            <p className="dense-index-control__note">
              검토용 프레임 인덱스만 만듭니다. 검지·라벨·Incident·학습
              데이터는 자동 생성하지 않습니다.
            </p>
          </section>

          <button
            type="button"
            className="details-toggle"
            aria-expanded={detailsOpen}
            onClick={() => setDetailsOpen((value) => !value)}
          >
            기술 로그 {detailsOpen ? "접기" : "보기"}
            <ChevronDown className={detailsOpen ? "rotated" : ""} size={17} />
          </button>
          {detailsOpen ? (
            <pre className="job-log" aria-label="분석 기술 로그">
{`입력: ${runtime.activeImport.fileName}
SHA-256: ${runtime.activeImport.sha256}
작업 ID: ${runtime.jobId ?? "시작 전"}
상태: ${job?.status ?? runtime.status}
브리지 단계: ${job?.stage ?? "응답 없음"}
진행률: ${job?.progress_percent ?? "응답 없음"}
후보: ${runtime.analysisSummary?.candidateCount ?? "완료 결과 없음"}
근거 세트: ${runtime.analysisSummary?.artifactSetId ?? "완료 결과 없음"}
오류 코드: ${job?.error_code ?? "없음"}`}
            </pre>
          ) : null}
        </GlassPanel>
      </div>
      <SavedAnalysisJobs
        jobs={savedJobs}
        loading={savedJobsLoading}
        activeJobId={runtime.jobId}
        onOpen={openSavedAnalysis}
      />
    </div>
  );
}
