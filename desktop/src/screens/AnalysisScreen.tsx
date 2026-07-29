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
import { requireActiveProjectId } from "../projectCatalog";
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
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [job, setJob] = useState<AnalysisJobResponse | null>(null);
  const [savedJobs, setSavedJobs] = useState<AnalysisJobResponse[]>([]);
  const [savedJobsLoading, setSavedJobsLoading] = useState(false);
  const [jobError, setJobError] = useState<string | null>(null);
  const [requesting, setRequesting] = useState(false);
  const pollGeneration = useRef(0);
  const running = runtime.status === "running" || runtime.status === "queued";
  const cancelled = runtime.status === "cancelled";
  const progress = runtime.progress;

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
    if (!window.trafficVisson) return;
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
      const response = await window.trafficVisson!.analysis.list({
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
      !window.trafficVisson ||
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
          await window.trafficVisson!.analysis.get({
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

  async function startActualAnalysis() {
    if (!runtime.activeImport) return;
    if (!window.trafficVisson) {
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
        await window.trafficVisson.analysis.start({
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
    if (!window.trafficVisson || !runtime.activeImport || !runtime.jobId) return;
    setRequesting(true);
    setJobError(null);
    try {
      const next = parseAnalysisJob(
        await window.trafficVisson.analysis.cancel({
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
