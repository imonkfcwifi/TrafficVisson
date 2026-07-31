import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ChevronRight,
  Crosshair,
  Eye,
  FastForward,
  MapPin,
  MapPinOff,
  Minus,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Route,
  Rewind,
  Tags,
  Video,
  X,
} from "lucide-react";
import {
  parseAnalysisJob,
  toRestoredRuntime,
} from "../analysisJobs";
import {
  loadSelectedCandidateId,
  saveSelectedCandidateId,
} from "../candidateSelection";
import {
  Badge,
  Button,
  DefinitionList,
  GlassPanel,
  IconButton,
} from "../components/ui";
import type { ScreenId } from "../demo";
import { buildCandidateEvidenceUrl } from "../evidence";
import {
  interpolateGpxPosition,
  parseGpxTrack,
  type GpxTrack,
} from "../gpsTracks";
import {
  parsePipelineJob,
  pipelineQuestionQueueBinding,
  publishPipelineJob,
  usePipelineUi,
  type PipelineJob,
  type PipelineQuestionQueueBinding,
} from "../pipelineJobs";
import {
  parseActivePersistentIncidents,
  persistentIssueLabel,
  persistentSeverityLabel,
  type PersistentIncident,
} from "../persistentIncidents";
import type { AnalysisCandidate, AnalysisSummary } from "../runtimeState";
import {
  restoreCompletedAnalysis,
  useRuntimeState,
} from "../runtimeState";
import {
  normalizedVideoPoint,
  parseUserDiscoveredCandidateReceipt,
  parseUserDiscoveredReviewFrame,
  userDiscoveredCandidateErrorMessage,
  type UserDiscoveredPointSelection,
  type UserDiscoveredReviewFrame,
} from "../userDiscoveredCandidates";

const HistoryMap = lazy(() =>
  import("../components/HistoryMap").then((module) => ({
    default: module.HistoryMap,
  })),
);

function formatTime(seconds: number, milliseconds = false) {
  const safeSeconds = Math.max(0, seconds);
  const total = Math.floor(safeSeconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const wholeSeconds = total % 60;
  const base = [hours, minutes, wholeSeconds]
    .map((value) => String(value).padStart(2, "0"))
    .join(":");
  return milliseconds
    ? `${base}.${String(Math.round((safeSeconds - total) * 1_000)).padStart(3, "0")}`
    : base;
}

function ZeroCandidateSurveyHistory({
  summary,
  pipelineJob,
  binding,
  sourceDurationSeconds,
  onNavigate,
}: {
  summary: AnalysisSummary;
  pipelineJob: PipelineJob;
  binding: PipelineQuestionQueueBinding;
  sourceDurationSeconds: number;
  onNavigate: (screen: ScreenId) => void;
}) {
  const [timestampSeconds, setTimestampSeconds] = useState(0);
  const [frame, setFrame] =
    useState<UserDiscoveredReviewFrame | null>(null);
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [displayPoint, setDisplayPoint] = useState<{
    x: number;
    y: number;
  } | null>(null);
  const [state, setState] = useState<{
    phase:
      | "loading_frame"
      | "ready"
      | "submitting"
      | "created_refresh_failed"
      | "failed";
    message: string;
  }>({
    phase: "loading_frame",
    message:
      "전체 주행의 불변 2fps 인덱스에서 첫 프레임을 확인하고 있습니다.",
  });
  const durationSeconds = Math.max(
    frame?.indexedDurationSeconds ?? 0,
    sourceDurationSeconds,
    1,
  );

  useEffect(() => {
    const service =
      window.trafficSuperVision?.userDiscoveredCandidates;
    if (!service) {
      setState({
        phase: "failed",
        message:
          "설치형 앱의 전체 2fps 검토 프레임 연결을 찾지 못했습니다.",
      });
      return;
    }
    let active = true;
    let objectUrl: string | null = null;
    setState({
      phase: "loading_frame",
      message:
        "전체 주행의 불변 2fps 인덱스에서 가장 가까운 프레임을 확인하고 있습니다.",
    });
    setFrame(null);
    setFrameUrl(null);
    setDisplayPoint(null);
    const requestedTimestampSeconds =
      Math.round(timestampSeconds * 1_000_000) / 1_000_000;
    const timer = window.setTimeout(() => {
      void service
        .reviewFrame({
          projectId: summary.projectId,
          pipelineJobId: pipelineJob.jobId,
          analysisJobId: summary.jobId,
          timestampSeconds: requestedTimestampSeconds,
        })
        .then((value) =>
          parseUserDiscoveredReviewFrame(value, {
            projectId: summary.projectId,
            pipelineJobId: pipelineJob.jobId,
            analysisJobId: summary.jobId,
            requestedTimestampSeconds,
          }),
        )
        .then((nextFrame) => {
          if (!active) return;
          if (
            nextFrame.questionQueueRevision !== binding.queueRevision
          ) {
            throw new Error(
              "질문 큐가 갱신되어 현재 2fps 프레임 선택을 사용할 수 없습니다.",
            );
          }
          objectUrl = URL.createObjectURL(
            new Blob(
              [nextFrame.bytes.slice().buffer as ArrayBuffer],
              { type: "image/jpeg" },
            ),
          );
          setFrame(nextFrame);
          setFrameUrl(objectUrl);
          setState({
            phase: "ready",
            message:
              "모델이 놓친 도로 문제가 보이면 해당 위치를 한 번 클릭하세요.",
          });
        })
        .catch((error) => {
          if (!active) return;
          setState({
            phase: "failed",
            message: userDiscoveredCandidateErrorMessage(error),
          });
        });
    }, 180);
    return () => {
      active = false;
      window.clearTimeout(timer);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [
    binding.queueRevision,
    pipelineJob.jobId,
    summary.jobId,
    summary.projectId,
    timestampSeconds,
  ]);

  const submitMissedCandidate = async (
    event: React.MouseEvent<HTMLImageElement>,
  ) => {
    if (
      state.phase !== "ready" ||
      !frame ||
      !frameUrl
    ) {
      return;
    }
    if (frame.questionQueueRevision !== binding.queueRevision) {
      setState({
        phase: "failed",
        message:
          "질문 큐가 갱신됐습니다. 최신 전체 주행 프레임을 다시 불러와 주세요.",
      });
      return;
    }
    const image = event.currentTarget;
    const bounds = image.getBoundingClientRect();
    const objectFit = window.getComputedStyle(image).objectFit;
    const point = normalizedVideoPoint({
      clientX: event.clientX,
      clientY: event.clientY,
      left: bounds.left,
      top: bounds.top,
      width: bounds.width,
      height: bounds.height,
      videoWidth: image.naturalWidth,
      videoHeight: image.naturalHeight,
      objectFit:
        objectFit === "cover" || objectFit === "fill"
          ? objectFit
          : "contain",
    });
    if (!point) {
      setState({
        phase: "ready",
        message:
          "검은 여백이 아닌 실제 영상 안에서 문제 위치를 클릭해 주세요.",
      });
      return;
    }
    setDisplayPoint({
      x: ((event.clientX - bounds.left) / bounds.width) * 100,
      y: ((event.clientY - bounds.top) / bounds.height) * 100,
    });
    const bridge = window.trafficSuperVision;
    const service = bridge?.userDiscoveredCandidates;
    if (!bridge || !service) {
      setState({
        phase: "failed",
        message:
          "설치형 앱의 누락 후보 연결을 찾지 못했습니다. 앱을 다시 시작해 주세요.",
      });
      return;
    }
    const requestedTimestampSeconds =
      frame.selectedFrameTimestampSeconds;
    setState({
      phase: "submitting",
      message:
        "서버가 같은 영상의 2fps 프레임과 활성 승격 모델 계보를 다시 확인하고 있습니다.",
    });
    try {
      const receipt = parseUserDiscoveredCandidateReceipt(
        await service.create({
          projectId: summary.projectId,
          pipelineJobId: pipelineJob.jobId,
          analysisJobId: summary.jobId,
          timestampSeconds: requestedTimestampSeconds,
          selection: point,
          requestSam2Mask: true,
          expectedQuestionQueueRevision:
            frame.questionQueueRevision,
        }),
        {
          projectId: summary.projectId,
          pipelineJobId: pipelineJob.jobId,
          analysisJobId: summary.jobId,
          requestedTimestampSeconds,
          selection: point,
        },
      );
      const [pipelineValue, analysisValue] = await Promise.all([
        bridge.pipeline.get({
          projectId: summary.projectId,
          jobId: pipelineJob.jobId,
        }),
        bridge.analysis.get({
          projectId: summary.projectId,
          jobId: summary.jobId,
        }),
      ]);
      const nextPipeline = parsePipelineJob(pipelineValue);
      const nextBinding = pipelineQuestionQueueBinding(nextPipeline);
      if (
        nextPipeline.projectId !== receipt.projectId ||
        nextPipeline.jobId !== receipt.pipelineJobId ||
        !nextBinding ||
        nextBinding.analysisJobId !== receipt.analysisJobId ||
        nextBinding.queueRevision !== receipt.questionQueueRevision
      ) {
        throw new Error(
          "새 질문 리비전과 파이프라인 상태가 일치하지 않습니다.",
        );
      }
      const restored = toRestoredRuntime(parseAnalysisJob(analysisValue));
      if (
        restored.analysisSummary.projectId !== receipt.projectId ||
        restored.analysisSummary.jobId !== receipt.analysisJobId ||
        !restored.analysisSummary.candidates.some(
          ({ candidateId }) => candidateId === receipt.candidateId,
        )
      ) {
        throw new Error(
          "새 누락 후보 근거가 분석 화면에 투영되지 않았습니다.",
        );
      }
      publishPipelineJob(nextPipeline);
      restoreCompletedAnalysis(
        restored.activeImport,
        restored.analysisSummary,
      );
      saveSelectedCandidateId(
        receipt.projectId,
        receipt.analysisJobId,
        receipt.candidateId,
      );
      onNavigate("label");
    } catch (error) {
      setState({
        phase: "created_refresh_failed",
        message: `누락 후보 질문 생성 또는 최신 화면 갱신에 실패했습니다. 중복 제출하지 말고 질문 검토를 다시 여세요. ${
          error instanceof Error ? error.message : ""
        }`.trim(),
      });
    }
  };

  return (
    <div className="screen screen--history">
      <header className="screen-header">
        <div>
          <p className="eyebrow">승격 모델 0건 조사 QA</p>
          <h1>전체 주행 2fps 누락 확인</h1>
          <p>
            기존 후보가 없어도 원본 전체 시간축을 직접 확인합니다. 문제를
            찾으면 위치를 클릭하고, 없으면 질문 검토로 돌아가 완료하세요.
          </p>
        </div>
        <div className="playback-actions">
          <Button variant="primary" onClick={() => onNavigate("label")}>
            누락 없음 · 질문 검토로
          </Button>
        </div>
      </header>
      <GlassPanel className="video-player">
        <div className="video-frame video-frame--missed-qa">
          <div className="missed-qa-frame">
            {frameUrl ? (
              <img
                src={frameUrl}
                alt={`${formatTime(
                  frame?.selectedFrameTimestampSeconds ??
                    timestampSeconds,
                  true,
                )} 전체 주행 2fps 검토 프레임`}
                className={
                  state.phase === "ready"
                    ? "missed-qa-frame__image is-selectable"
                    : "missed-qa-frame__image"
                }
                onClick={(event) =>
                  void submitMissedCandidate(event)
                }
              />
            ) : (
              <div className="missed-qa-frame__loading" role="status">
                <Crosshair size={28} />
                <strong>전체 주행 2fps 프레임 확인 중</strong>
                <span>
                  후보 클립이 아니라 원본 전체 시간축의 불변 프레임을
                  불러옵니다.
                </span>
              </div>
            )}
            {displayPoint ? (
              <span
                className="missed-qa-frame__point"
                style={{
                  left: `${displayPoint.x}%`,
                  top: `${displayPoint.y}%`,
                }}
                aria-hidden="true"
              />
            ) : null}
            <div className="missed-qa-frame__instruction">
              {state.message}
            </div>
          </div>
          <div className="video-status video-status--left">
            <span className="qa-dot" />
            <span className="mono">
              {formatTime(
                frame?.selectedFrameTimestampSeconds ??
                  timestampSeconds,
                true,
              )}{" "}
              / {formatTime(durationSeconds)}
            </span>
          </div>
          <div className="video-status video-status--right">
            <span>{summary.sourceFileName}</span>
            <span>
              {frame
                ? `전체 2fps QA · frame ${frame.selectedFrameIndex}`
                : "전체 2fps QA"}
            </span>
          </div>
        </div>
        <div className="video-controls missed-qa-controls">
          <IconButton
            label="전체 주행 2fps 프레임 0.5초 뒤로"
            disabled={
              state.phase === "submitting" ||
              timestampSeconds <= 0
            }
            onClick={() =>
              setTimestampSeconds((current) =>
                Math.max(0, current - 0.5),
              )
            }
          >
            <Rewind size={16} />
          </IconButton>
          <input
            type="range"
            min={0}
            max={durationSeconds}
            step={0.5}
            value={Math.min(timestampSeconds, durationSeconds)}
            aria-label="전체 주행 2fps QA 시각"
            disabled={state.phase === "submitting"}
            onChange={(event) =>
              setTimestampSeconds(Number(event.currentTarget.value))
            }
          />
          <IconButton
            label="전체 주행 2fps 프레임 0.5초 앞으로"
            disabled={
              state.phase === "submitting" ||
              timestampSeconds >= durationSeconds
            }
            onClick={() =>
              setTimestampSeconds((current) =>
                Math.min(durationSeconds, current + 0.5),
              )
            }
          >
            <FastForward size={16} />
          </IconButton>
        </div>
        {state.phase === "failed" ||
        state.phase === "created_refresh_failed" ? (
          <div className="inline-error" role="alert">
            <AlertTriangle size={17} aria-hidden="true" />
            <span>{state.message}</span>
          </div>
        ) : (
          <p className="muted">{state.message}</p>
        )}
      </GlassPanel>
    </div>
  );
}

export function HistoryScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const runtime = useRuntimeState();
  const pipeline = usePipelineUi();
  const summary = runtime.analysisSummary;
  const candidates = summary?.candidates ?? [];
  const sourceLocation = summary?.sourceLocation ?? null;
  const videoRef = useRef<HTMLVideoElement>(null);
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(() =>
    summary
      ? loadSelectedCandidateId(summary.projectId, summary.jobId, summary.candidates)
      : null,
  );
  const [clipTimeSeconds, setClipTimeSeconds] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const [incidents, setIncidents] = useState<PersistentIncident[]>([]);
  const [incidentError, setIncidentError] = useState<string | null>(null);
  const [gpxTrack, setGpxTrack] = useState<GpxTrack | null>(null);
  const [gpxLoading, setGpxLoading] = useState(false);
  const [gpxError, setGpxError] = useState<string | null>(null);
  const [missQaOpen, setMissQaOpen] = useState(false);
  const [missReviewTimestampSeconds, setMissReviewTimestampSeconds] =
    useState(0);
  const [missReviewFrame, setMissReviewFrame] =
    useState<UserDiscoveredReviewFrame | null>(null);
  const [missReviewFrameUrl, setMissReviewFrameUrl] =
    useState<string | null>(null);
  const [missDisplayPoint, setMissDisplayPoint] = useState<{
    x: number;
    y: number;
  } | null>(null);
  const [missCandidateState, setMissCandidateState] = useState<{
    phase:
      | "idle"
      | "loading_frame"
      | "ready"
      | "submitting"
      | "created_refresh_failed"
      | "failed";
    message: string | null;
    point: UserDiscoveredPointSelection | null;
  }>({
    phase: "idle",
    message: null,
    point: null,
  });

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
      setGpxTrack(null);
      setGpxLoading(false);
      return;
    }
    let active = true;
    setGpxLoading(true);
    setGpxError(null);
    void window.trafficSuperVision.gps
      .getTrack({
        projectId: summary.projectId,
        tripId: summary.jobId,
      })
      .then((value) => {
        if (!active) return;
        setGpxTrack(parseGpxTrack(value, summary.projectId, summary.jobId));
      })
      .catch((error) => {
        if (!active) return;
        const message =
          error instanceof Error ? error.message : "GPX 상태를 확인하지 못했습니다.";
        setGpxTrack(null);
        if (!/No GPX track|attached to this trip|404/i.test(message)) {
          setGpxError(message);
        }
      })
      .finally(() => {
        if (active) setGpxLoading(false);
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

  const selectedIndex = Math.max(
    0,
    candidates.findIndex((candidate) => candidate.candidateId === selectedCandidateId),
  );
  const selectedCandidate = candidates[selectedIndex];
  const incidentsByCandidateId = useMemo(
    () => new Map(incidents.map((incident) => [incident.candidateId, incident])),
    [incidents],
  );
  const mappedIncidentCount = candidates.reduce(
    (count, candidate) =>
      count + (incidentsByCandidateId.has(candidate.candidateId) ? 1 : 0),
    0,
  );
  const selectedIncident = selectedCandidate
    ? incidentsByCandidateId.get(selectedCandidate.candidateId)
    : undefined;
  const approvedCandidateIds = useMemo(
    () => new Set(incidents.map((incident) => incident.candidateId)),
    [incidents],
  );
  const currentPipelineJob =
    summary && pipeline.projectId === summary.projectId
      ? pipeline.job
      : null;
  const questionQueueBinding = currentPipelineJob
    ? pipelineQuestionQueueBinding(currentPipelineJob)
    : null;
  const canAddMissedCandidate = Boolean(
    summary &&
      currentPipelineJob &&
      questionQueueBinding &&
      questionQueueBinding.surveyQaRequired &&
      questionQueueBinding.analysisJobId === summary.jobId &&
      /^[a-f0-9]{64}$/.test(questionQueueBinding.queueRevision),
  );

  useEffect(() => {
    if (!selectedCandidate) return;
    setPlaying(false);
    setPlaybackError(null);
    setClipTimeSeconds(
      Math.max(
        0,
        Math.min(
          selectedCandidate.previewDurationSeconds,
          selectedCandidate.representativeTimestampSeconds -
            selectedCandidate.previewStartSeconds,
        ),
      ),
    );
    setMissCandidateState({
      phase: "idle",
      message: null,
      point: null,
    });
    setMissQaOpen(false);
    setMissReviewFrame(null);
    setMissReviewFrameUrl(null);
    setMissDisplayPoint(null);
  }, [selectedCandidate]);

  useEffect(() => {
    if (videoRef.current) videoRef.current.playbackRate = rate;
  }, [rate]);

  const durationSeconds = useMemo(() => {
    const candidateEnd = candidates.reduce(
      (maximum, candidate) => Math.max(maximum, candidate.endSeconds),
      0,
    );
    return Math.max(runtime.activeImport?.durationSeconds ?? 0, candidateEnd, 1);
  }, [candidates, runtime.activeImport?.durationSeconds]);

  useEffect(() => {
    if (
      !missQaOpen ||
      !summary ||
      !currentPipelineJob ||
      !questionQueueBinding ||
      !canAddMissedCandidate
    ) {
      return;
    }
    const bridge = window.trafficSuperVision;
    if (!bridge?.userDiscoveredCandidates) {
      setMissCandidateState({
        phase: "failed",
        message:
          "설치형 앱의 전체 2fps 검토 프레임 연결을 찾지 못했습니다.",
        point: null,
      });
      return;
    }
    let active = true;
    let objectUrl: string | null = null;
    setMissCandidateState({
      phase: "loading_frame",
      message:
        "전체 주행의 불변 2fps 인덱스에서 가장 가까운 프레임을 확인하고 있습니다.",
      point: null,
    });
    setMissReviewFrame(null);
    setMissReviewFrameUrl(null);
    setMissDisplayPoint(null);
    const timer = window.setTimeout(() => {
      const requestedTimestampSeconds =
        Math.round(missReviewTimestampSeconds * 1_000_000) /
        1_000_000;
      void bridge.userDiscoveredCandidates
        .reviewFrame({
          projectId: summary.projectId,
          pipelineJobId: currentPipelineJob.jobId,
          analysisJobId: summary.jobId,
          timestampSeconds: requestedTimestampSeconds,
        })
        .then((value) =>
          parseUserDiscoveredReviewFrame(value, {
            projectId: summary.projectId,
            pipelineJobId: currentPipelineJob.jobId,
            analysisJobId: summary.jobId,
            requestedTimestampSeconds,
          }),
        )
        .then((frame) => {
          if (!active) return;
          if (
            frame.questionQueueRevision !==
            questionQueueBinding.queueRevision
          ) {
            throw new Error(
              "질문 큐가 갱신되어 현재 2fps 프레임 선택을 사용할 수 없습니다.",
            );
          }
          objectUrl = URL.createObjectURL(
            new Blob(
              [frame.bytes.slice().buffer as ArrayBuffer],
              { type: "image/jpeg" },
            ),
          );
          setMissReviewFrame(frame);
          setMissReviewFrameUrl(objectUrl);
          setMissCandidateState({
            phase: "ready",
            message:
              "이 프레임에서 모델이 놓친 도로 문제가 보이면 해당 위치를 한 번 클릭하세요.",
            point: null,
          });
        })
        .catch((error) => {
          if (!active) return;
          setMissCandidateState({
            phase: "failed",
            message: userDiscoveredCandidateErrorMessage(error),
            point: null,
          });
        });
    }, 180);
    return () => {
      active = false;
      window.clearTimeout(timer);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [
    canAddMissedCandidate,
    currentPipelineJob?.jobId,
    missQaOpen,
    missReviewTimestampSeconds,
    questionQueueBinding?.queueRevision,
    summary?.jobId,
    summary?.projectId,
  ]);

  if (
    summary &&
    !selectedCandidate &&
    currentPipelineJob &&
    questionQueueBinding?.zeroCandidateSurvey
  ) {
    return (
      <ZeroCandidateSurveyHistory
        summary={summary}
        pipelineJob={currentPipelineJob}
        binding={questionQueueBinding}
        sourceDurationSeconds={durationSeconds}
        onNavigate={onNavigate}
      />
    );
  }

  if (!summary || !selectedCandidate) {
    return (
      <div className="screen screen--history">
        <header className="screen-header">
          <div>
            <p className="eyebrow">지도·영상 동기화</p>
            <h1>지도·영상 히스토리</h1>
            <p>영상 시각·후보 근거·GPS 자료를 한 재생 흐름으로 확인합니다.</p>
          </div>
        </header>
        <GlassPanel className="analysis-empty">
          <Video size={34} />
          <h1>재생할 실제 분석 후보가 없습니다</h1>
          <p>
            영상을 가져와 후보 추출을 완료하면 실제 후보 클립과 원본 시각을 기준으로
            히스토리를 구성합니다.
          </p>
          <Button variant="primary" onClick={() => onNavigate("import")}>
            영상 가져오기로 이동
          </Button>
        </GlassPanel>
      </div>
    );
  }

  const imageUrl = buildCandidateEvidenceUrl(
    summary.projectId,
    summary.jobId,
    selectedCandidate.candidateId,
    "image",
  );
  const clipUrl = buildCandidateEvidenceUrl(
    summary.projectId,
    summary.jobId,
    selectedCandidate.candidateId,
    "clip",
  );
  const absoluteTimeSeconds =
    selectedCandidate.previewStartSeconds + clipTimeSeconds;
  const synchronizedTimeSeconds = missQaOpen
    ? (missReviewFrame?.selectedFrameTimestampSeconds ??
      missReviewTimestampSeconds)
    : absoluteTimeSeconds;
  const currentGpxPosition = gpxTrack
    ? interpolateGpxPosition(gpxTrack, synchronizedTimeSeconds)
    : null;
  const selectedGpxPosition = gpxTrack
    ? interpolateGpxPosition(
        gpxTrack,
        selectedCandidate.representativeTimestampSeconds,
      )
    : null;
  const tripProgress = Math.max(
    0,
    Math.min(
      100,
      (synchronizedTimeSeconds /
        Math.max(
          missReviewFrame?.indexedDurationSeconds ?? durationSeconds,
          0.001,
        )) *
        100,
    ),
  );
  const clipProgress = Math.max(
    0,
    Math.min(
      100,
      (clipTimeSeconds / Math.max(selectedCandidate.previewDurationSeconds, 0.001)) *
        100,
    ),
  );

  const selectCandidate = (candidate: AnalysisCandidate) => {
    setSelectedCandidateId(candidate.candidateId);
    saveSelectedCandidateId(summary.projectId, summary.jobId, candidate.candidateId);
  };

  const selectCandidateById = (candidateId: string) => {
    const candidate = candidates.find(
      (item) => item.candidateId === candidateId,
    );
    if (candidate) selectCandidate(candidate);
  };

  const importGpx = async () => {
    if (!window.trafficSuperVision || !summary) return;
    setGpxError(null);
    try {
      const selection = await window.trafficSuperVision.dialogs.selectGpxFile();
      if (!selection.selectionId || !selection.file) return;
      setGpxLoading(true);
      const value = await window.trafficSuperVision.gps.importTrack({
        projectId: summary.projectId,
        tripId: summary.jobId,
        gpxSelectionId: selection.selectionId,
        videoOffsetSeconds: gpxTrack?.videoOffsetSeconds ?? 0,
      });
      setGpxTrack(parseGpxTrack(value, summary.projectId, summary.jobId));
    } catch (error) {
      setGpxError(
        error instanceof Error
          ? error.message
          : "선택한 GPX를 연결하지 못했습니다.",
      );
    } finally {
      setGpxLoading(false);
    }
  };

  const updateGpxOffset = async (videoOffsetSeconds: number) => {
    if (!window.trafficSuperVision || !summary || !gpxTrack) return;
    setGpxLoading(true);
    setGpxError(null);
    try {
      const value = await window.trafficSuperVision.gps.updateOffset({
        projectId: summary.projectId,
        tripId: summary.jobId,
        videoOffsetSeconds,
      });
      setGpxTrack(parseGpxTrack(value, summary.projectId, summary.jobId));
    } catch (error) {
      setGpxError(
        error instanceof Error
          ? error.message
          : "GPS 시간 오프셋을 저장하지 못했습니다.",
      );
    } finally {
      setGpxLoading(false);
    }
  };

  const navigateWithCandidate = (screen: ScreenId) => {
    saveSelectedCandidateId(
      summary.projectId,
      summary.jobId,
      selectedCandidate.candidateId,
    );
    onNavigate(screen);
  };

  const setVideoTime = (seconds: number) => {
    const bounded = Math.max(
      0,
      Math.min(selectedCandidate.previewDurationSeconds, seconds),
    );
    setClipTimeSeconds(bounded);
    if (videoRef.current) videoRef.current.currentTime = bounded;
  };

  const togglePlayback = async () => {
    if (!videoRef.current) return;
    setPlaybackError(null);
    try {
      if (videoRef.current.paused) {
        await videoRef.current.play();
      } else {
        videoRef.current.pause();
      }
    } catch (error) {
      setPlaybackError(
        error instanceof Error ? error.message : "후보 클립을 재생하지 못했습니다.",
      );
    }
  };

  const startMissedCandidateSelection = () => {
    if (!canAddMissedCandidate || !questionQueueBinding) {
      setMissCandidateState({
        phase: "failed",
        message:
          "승격 모델의 독립 조사 질문 큐를 검토 중일 때만 누락 장면을 추가할 수 있습니다.",
        point: null,
      });
      return;
    }
    videoRef.current?.pause();
    setMissReviewTimestampSeconds(
      Math.max(
        0,
        Math.min(
          durationSeconds,
          selectedCandidate.previewStartSeconds +
            (videoRef.current?.currentTime ?? clipTimeSeconds),
        ),
      ),
    );
    setMissQaOpen(true);
    setMissCandidateState({
      phase: "loading_frame",
      message:
        "전체 주행의 2fps 프레임을 불러오고 있습니다. 아직 질문이나 확정 사건은 생기지 않습니다.",
      point: null,
    });
  };

  const submitMissedCandidate = async (
    event: React.MouseEvent<HTMLImageElement>,
  ) => {
    if (
      missCandidateState.phase !== "ready" ||
      !summary ||
      !currentPipelineJob ||
      !questionQueueBinding ||
      !missReviewFrame ||
      !missReviewFrameUrl
    ) {
      return;
    }
    if (
      missReviewFrame.questionQueueRevision !==
      questionQueueBinding.queueRevision
    ) {
      setMissCandidateState({
        phase: "failed",
        message:
          "질문 큐가 갱신됐습니다. 최신 전체 주행 프레임을 다시 불러와 주세요.",
        point: null,
      });
      return;
    }
    const image = event.currentTarget;
    const bounds = image.getBoundingClientRect();
    const objectFit = window.getComputedStyle(image).objectFit;
    const point = normalizedVideoPoint({
      clientX: event.clientX,
      clientY: event.clientY,
      left: bounds.left,
      top: bounds.top,
      width: bounds.width,
      height: bounds.height,
      videoWidth: image.naturalWidth,
      videoHeight: image.naturalHeight,
      objectFit:
        objectFit === "cover" || objectFit === "fill"
          ? objectFit
          : "contain",
    });
    if (!point) {
      setMissCandidateState({
        phase: "ready",
        message:
          "검은 여백이 아닌 실제 영상 안에서 문제 위치를 클릭해 주세요.",
        point: null,
      });
      return;
    }
    setMissDisplayPoint({
      x: ((event.clientX - bounds.left) / bounds.width) * 100,
      y: ((event.clientY - bounds.top) / bounds.height) * 100,
    });
    const requestedTimestampSeconds =
      missReviewFrame.selectedFrameTimestampSeconds;
    const expected = {
      projectId: summary.projectId,
      pipelineJobId: currentPipelineJob.jobId,
      analysisJobId: summary.jobId,
      requestedTimestampSeconds,
      selection: point,
    };
    const bridge = window.trafficSuperVision;
    const service = bridge?.userDiscoveredCandidates;
    if (!bridge || !service) {
      setMissCandidateState({
        phase: "failed",
        message:
          "설치형 앱의 누락 후보 연결을 찾지 못했습니다. 앱을 다시 시작해 주세요.",
        point,
      });
      return;
    }
    setMissCandidateState({
      phase: "submitting",
      message:
        "서버가 같은 영상의 2fps 프레임과 활성 승격 모델 계보를 다시 확인하고 있습니다.",
      point,
    });
    let receipt;
    try {
      receipt = parseUserDiscoveredCandidateReceipt(
        await service.create({
          projectId: summary.projectId,
          pipelineJobId: currentPipelineJob.jobId,
          analysisJobId: summary.jobId,
          timestampSeconds: requestedTimestampSeconds,
          selection: point,
          requestSam2Mask: true,
          expectedQuestionQueueRevision:
            missReviewFrame.questionQueueRevision,
        }),
        expected,
      );
    } catch (error) {
      setMissCandidateState({
        phase: "failed",
        message: userDiscoveredCandidateErrorMessage(error),
        point,
      });
      return;
    }
    try {
      const [pipelineValue, analysisValue] = await Promise.all([
        bridge.pipeline.get({
          projectId: summary.projectId,
          jobId: currentPipelineJob.jobId,
        }),
        bridge.analysis.get({
          projectId: summary.projectId,
          jobId: summary.jobId,
        }),
      ]);
      const nextPipeline = parsePipelineJob(pipelineValue);
      const nextBinding = pipelineQuestionQueueBinding(nextPipeline);
      if (
        nextPipeline.projectId !== receipt.projectId ||
        nextPipeline.jobId !== receipt.pipelineJobId ||
        !nextBinding ||
        nextBinding.analysisJobId !== receipt.analysisJobId ||
        nextBinding.queueRevision !== receipt.questionQueueRevision
      ) {
        throw new Error(
          "새 질문 리비전과 파이프라인 상태가 일치하지 않습니다.",
        );
      }
      const restored = toRestoredRuntime(parseAnalysisJob(analysisValue));
      if (
        restored.analysisSummary.projectId !== receipt.projectId ||
        restored.analysisSummary.jobId !== receipt.analysisJobId ||
        !restored.analysisSummary.candidates.some(
          ({ candidateId }) => candidateId === receipt.candidateId,
        )
      ) {
        throw new Error(
          "새 누락 후보 근거가 분석 화면에 투영되지 않았습니다.",
        );
      }
      publishPipelineJob(nextPipeline);
      restoreCompletedAnalysis(
        restored.activeImport,
        restored.analysisSummary,
      );
      saveSelectedCandidateId(
        receipt.projectId,
        receipt.analysisJobId,
        receipt.candidateId,
      );
      onNavigate("label");
    } catch (error) {
      setMissCandidateState({
        phase: "created_refresh_failed",
        message: `누락 후보 질문은 생성됐지만 최신 화면을 불러오지 못했습니다. 중복 제출하지 말고 질문 검토를 다시 여세요. ${
          error instanceof Error ? error.message : ""
        }`.trim(),
        point,
      });
    }
  };

  const closeMissedCandidateQa = () => {
    if (missCandidateState.phase === "submitting") return;
    setMissQaOpen(false);
    setMissReviewFrame(null);
    setMissReviewFrameUrl(null);
    setMissDisplayPoint(null);
    setMissCandidateState({
      phase: "idle",
      message: null,
      point: null,
    });
  };

  const evidenceRows = selectedIncident
    ? [
        {
          label: "incident_id",
          value: selectedIncident.incidentId,
        },
        {
          label: "판정 / 심각도",
          value: `${persistentIssueLabel(
            selectedIncident.issueType,
          )} · ${persistentSeverityLabel(selectedIncident.severity)}`,
        },
        {
          label: "조치 / OCR",
          value: `${
            selectedIncident.maintenanceAction ?? "권장 조치 미입력"
          } · OCR ${selectedIncident.ocrText ?? "없음"}`,
        },
        {
          label: "source / tc",
          value: `${summary.sourceFileName} · ${formatTime(
            selectedCandidate.representativeTimestampSeconds,
            true,
          )}`,
        },
        {
          label: "candidate_window",
          value: `${formatTime(
            selectedCandidate.startSeconds,
            true,
          )} — ${formatTime(selectedCandidate.endSeconds, true)}`,
        },
        {
          label: "gps",
          value: selectedGpxPosition
            ? `${selectedGpxPosition.latitude.toFixed(6)}, ${selectedGpxPosition.longitude.toFixed(6)} · GPX 보간`
            : gpxTrack
              ? "GPX 범위 밖 또는 10초 초과 누락 구간"
              : sourceLocation
                ? `촬영 시작 ${sourceLocation.latitude.toFixed(6)}, ${sourceLocation.longitude.toFixed(6)} · 후보 위치 미확정`
                : "자료 없음 · 경로/좌표 미표시",
        },
        {
          label: "provenance",
          value: `q:${selectedIncident.questionId} · c:${selectedCandidate.candidateId}`,
        },
      ]
    : [
        {
          label: "source / tc",
          value: `${summary.sourceFileName} · ${formatTime(
            selectedCandidate.representativeTimestampSeconds,
            true,
          )}`,
        },
        {
          label: "candidate_window",
          value: `${formatTime(
            selectedCandidate.startSeconds,
            true,
          )} — ${formatTime(selectedCandidate.endSeconds, true)}`,
        },
        {
          label: "scene_score",
          value: `${selectedCandidate.sceneScore.toFixed(3)} · 결함 신뢰도 아님`,
        },
        {
          label: "sharpness",
          value: selectedCandidate.sharpnessScore.toFixed(1),
        },
        {
          label: "triggers",
          value: selectedCandidate.triggers.length
            ? selectedCandidate.triggers.join(", ")
            : "기록 없음",
        },
        {
          label: "gps",
          value: selectedGpxPosition
            ? `${selectedGpxPosition.latitude.toFixed(6)}, ${selectedGpxPosition.longitude.toFixed(6)} · GPX 보간`
            : gpxTrack
              ? "GPX 범위 밖 또는 10초 초과 누락 구간"
              : sourceLocation
                ? `촬영 시작 ${sourceLocation.latitude.toFixed(6)}, ${sourceLocation.longitude.toFixed(6)} · 후보 위치 미확정`
                : "자료 없음 · 경로/좌표 미표시",
        },
        {
          label: "provenance",
          value: `sha256:${summary.sourceSha256.slice(0, 16)}… · ${selectedCandidate.candidateId}`,
        },
      ];

  return (
    <div className="screen screen--history">
      <header className="screen-header">
        <div>
          <p className="eyebrow">지도·영상 동기화</p>
          <h1>지도·영상 히스토리</h1>
          <p>
            승인 사건과 미확정 후보를 실제 클립·원본 타임코드에 동기화합니다.{" "}
            {gpxTrack
              ? `GPX ${gpxTrack.pointCount.toLocaleString("ko-KR")}개 지점으로 진행 경로와 후보 위치를 표시합니다.`
              : sourceLocation
                ? "현재는 촬영 시작 좌표만 있으며 GPX를 연결하면 실제 이동 경로를 볼 수 있습니다."
                : "GPX를 연결하기 전에는 경로와 위치를 표시하지 않습니다."}
          </p>
        </div>
        <div className="playback-actions">
          {!missQaOpen ? (
            <div className="rate-toggle" role="group" aria-label="후보 클립 재생 속도">
              {[0.5, 1, 2, 4].map((value) => (
                <button
                  type="button"
                  key={value}
                  className={rate === value ? "active" : ""}
                  aria-pressed={rate === value}
                  onClick={() => setRate(value)}
                >
                  {value}×
                </button>
              ))}
            </div>
          ) : null}
          <Button
            variant={missQaOpen ? "primary" : undefined}
            disabled={
              missCandidateState.phase === "submitting" ||
              (!missQaOpen && !canAddMissedCandidate)
            }
            onClick={() =>
              missQaOpen
                ? closeMissedCandidateQa()
                : startMissedCandidateSelection()
            }
            title={
              canAddMissedCandidate
                ? "전체 주행의 불변 2fps 프레임을 임의 시각에서 확인합니다."
                : "승격 모델의 독립 조사 질문 큐에서만 사용할 수 있습니다."
            }
          >
            {missQaOpen ? <X size={17} /> : <Crosshair size={17} />}
            {missQaOpen ? "2fps QA 닫기" : "모델 누락 장면 확인"}
          </Button>
          {gpxTrack ? (
            <div
              className="gps-sync-control"
              role="group"
              aria-label="영상과 GPX 시작 시각 오프셋"
            >
              <IconButton
                label="GPX를 영상보다 1초 앞당기기"
                disabled={gpxLoading}
                onClick={() =>
                  void updateGpxOffset(gpxTrack.videoOffsetSeconds - 1)
                }
              >
                <Minus size={15} />
              </IconButton>
              <span className="mono" title="GPX 시작점이 영상에서 나타나는 시각">
                GPS {gpxTrack.videoOffsetSeconds >= 0 ? "+" : ""}
                {gpxTrack.videoOffsetSeconds.toFixed(1)}s
              </span>
              <IconButton
                label="GPX를 영상보다 1초 늦추기"
                disabled={gpxLoading}
                onClick={() =>
                  void updateGpxOffset(gpxTrack.videoOffsetSeconds + 1)
                }
              >
                <Plus size={15} />
              </IconButton>
              <IconButton
                label="GPX 시간 오프셋 0초로 초기화"
                disabled={gpxLoading || gpxTrack.videoOffsetSeconds === 0}
                onClick={() => void updateGpxOffset(0)}
              >
                <RotateCcw size={15} />
              </IconButton>
            </div>
          ) : null}
          <Button
            onClick={() => void importGpx()}
            disabled={gpxLoading || !window.trafficSuperVision}
            title={
              gpxTrack
                ? "현재 주행의 GPX 원본을 교체합니다."
                : "시간 정보가 있는 GPX 원본을 현재 분석 주행에 연결합니다."
            }
          >
            <Route size={17} />
            {gpxLoading ? "GPS 확인 중" : gpxTrack ? "GPX 교체" : "GPX 연결"}
          </Button>
        </div>
      </header>

      <div className="history-top">
        <GlassPanel className="video-player">
          <div
            className={`video-frame ${
              missQaOpen ? "video-frame--missed-qa" : ""
            }`}
          >
            {missQaOpen ? (
              <div className="missed-qa-frame">
                {missReviewFrameUrl ? (
                  <img
                    src={missReviewFrameUrl}
                    alt={`${formatTime(
                      missReviewFrame?.selectedFrameTimestampSeconds ??
                        missReviewTimestampSeconds,
                      true,
                    )} 전체 주행 2fps 검토 프레임`}
                    className={
                      missCandidateState.phase === "ready"
                        ? "missed-qa-frame__image is-selectable"
                        : "missed-qa-frame__image"
                    }
                    onClick={(event) =>
                      void submitMissedCandidate(event)
                    }
                  />
                ) : (
                  <div className="missed-qa-frame__loading" role="status">
                    <Crosshair size={28} />
                    <strong>전체 주행 2fps 프레임 확인 중</strong>
                    <span>
                      후보 클립이 아니라 원본 전체 시간축의 불변 프레임을
                      불러옵니다.
                    </span>
                  </div>
                )}
                {missDisplayPoint ? (
                  <span
                    className="missed-qa-frame__point"
                    style={{
                      left: `${missDisplayPoint.x}%`,
                      top: `${missDisplayPoint.y}%`,
                    }}
                    aria-hidden="true"
                  />
                ) : null}
                {missReviewFrameUrl ? (
                  <div className="missed-qa-frame__instruction">
                    {missCandidateState.phase === "submitting"
                      ? "누락 후보 근거와 질문을 생성하고 있습니다."
                      : "문제가 보이면 위치를 한 번 클릭 · 확정 truth는 사용자 질문 승인 후 생성"}
                  </div>
                ) : null}
              </div>
            ) : (
              <video
                key={selectedCandidate.candidateId}
                ref={videoRef}
                className="candidate-clip"
                src={clipUrl}
                poster={imageUrl}
                preload="metadata"
                onLoadedMetadata={(event) => {
                  const target = Math.max(
                    0,
                    Math.min(
                      event.currentTarget.duration ||
                        selectedCandidate.previewDurationSeconds,
                      selectedCandidate.representativeTimestampSeconds -
                        selectedCandidate.previewStartSeconds,
                    ),
                  );
                  event.currentTarget.currentTime = target;
                  event.currentTarget.playbackRate = rate;
                  setClipTimeSeconds(target);
                }}
                onTimeUpdate={(event) =>
                  setClipTimeSeconds(event.currentTarget.currentTime)
                }
                onPlay={() => setPlaying(true)}
                onPause={() => setPlaying(false)}
                onEnded={() => setPlaying(false)}
                onError={() => {
                  setPlaying(false);
                  setPlaybackError(
                    "저장된 후보 클립을 불러오지 못했습니다.",
                  );
                }}
              >
                이 환경에서는 후보 미리보기 영상을 재생할 수 없습니다.
              </video>
            )}
            <div className="video-status video-status--left">
              <span
                className={missQaOpen ? "qa-dot" : "record-dot"}
              />
              <span className="mono">
                {formatTime(synchronizedTimeSeconds, true)} /{" "}
                {formatTime(
                  missReviewFrame?.indexedDurationSeconds ??
                    durationSeconds,
                )}
              </span>
            </div>
            <div className="video-status video-status--right">
              <span>
                {currentGpxPosition
                  ? `${currentGpxPosition.latitude.toFixed(5)}, ${currentGpxPosition.longitude.toFixed(5)}`
                  : gpxTrack
                    ? "GPS 누락·범위 밖"
                    : sourceLocation
                      ? "촬영 시작 좌표만"
                      : "GPS 자료 없음"}
              </span>
              <span>{summary.sourceFileName}</span>
              <span>
                {missQaOpen
                  ? missReviewFrame
                    ? `전체 2fps QA · frame ${missReviewFrame.selectedFrameIndex}`
                    : "전체 2fps QA"
                  : `후보 클립 ${selectedIndex + 1}/${candidates.length}`}
              </span>
            </div>
          </div>
          {missQaOpen ? (
            <div className="video-controls missed-qa-controls">
              <IconButton
                label="전체 주행 2fps 프레임 0.5초 뒤로"
                disabled={
                  missCandidateState.phase === "submitting" ||
                  missReviewTimestampSeconds <= 0
                }
                onClick={() =>
                  setMissReviewTimestampSeconds((current) =>
                    Math.max(0, current - 0.5),
                  )
                }
              >
                <Rewind size={18} />
              </IconButton>
              <input
                className="video-scrubber"
                aria-label="전체 주행 2fps QA 시각"
                type="range"
                min={0}
                max={Math.max(
                  missReviewFrame?.indexedDurationSeconds ??
                    durationSeconds,
                  0.001,
                )}
                step={0.5}
                value={Math.min(
                  missReviewTimestampSeconds,
                  missReviewFrame?.indexedDurationSeconds ??
                    durationSeconds,
                )}
                disabled={missCandidateState.phase === "submitting"}
                onChange={(event) =>
                  setMissReviewTimestampSeconds(
                    Number(event.target.value),
                  )
                }
                style={{ "--progress": `${tripProgress}%` } as React.CSSProperties}
              />
              <IconButton
                label="전체 주행 2fps 프레임 0.5초 앞으로"
                disabled={
                  missCandidateState.phase === "submitting" ||
                  missReviewTimestampSeconds >=
                    (missReviewFrame?.indexedDurationSeconds ??
                      durationSeconds)
                }
                onClick={() =>
                  setMissReviewTimestampSeconds((current) =>
                    Math.min(
                      missReviewFrame?.indexedDurationSeconds ??
                        durationSeconds,
                      current + 0.5,
                    ),
                  )
                }
              >
                <FastForward size={18} />
              </IconButton>
              <span className="mono">
                {formatTime(synchronizedTimeSeconds, true)}
              </span>
            </div>
          ) : (
            <div className="video-controls">
              <IconButton
                label={playing ? "재생 일시정지" : "재생"}
                className="play-button"
                onClick={() => void togglePlayback()}
              >
                {playing ? (
                  <Pause size={20} fill="currentColor" />
                ) : (
                  <Play size={20} fill="currentColor" />
                )}
              </IconButton>
              <IconButton
                label="클립 2초 뒤로"
                onClick={() => setVideoTime(clipTimeSeconds - 2)}
              >
                <Rewind size={18} />
              </IconButton>
              <input
                className="video-scrubber"
                aria-label="현재 후보 클립 재생 위치"
                type="range"
                min={0}
                max={Math.max(
                  selectedCandidate.previewDurationSeconds,
                  0.001,
                )}
                step={0.01}
                value={clipTimeSeconds}
                onChange={(event) =>
                  setVideoTime(Number(event.target.value))
                }
                style={{ "--progress": `${clipProgress}%` } as React.CSSProperties}
              />
              <IconButton
                label="클립 2초 앞으로"
                onClick={() => setVideoTime(clipTimeSeconds + 2)}
              >
                <FastForward size={18} />
              </IconButton>
              <span className="mono">
                {formatTime(clipTimeSeconds, true)}
              </span>
            </div>
          )}
          {missQaOpen && missCandidateState.message ? (
            <p
              className={
                missCandidateState.phase === "failed" ||
                missCandidateState.phase ===
                  "created_refresh_failed"
                  ? "inline-error missed-qa-message"
                  : "missed-qa-message"
              }
              role={
                missCandidateState.phase === "failed" ||
                missCandidateState.phase ===
                  "created_refresh_failed"
                  ? "alert"
                  : "status"
              }
            >
              {missCandidateState.phase === "failed" ||
              missCandidateState.phase ===
                "created_refresh_failed" ? (
                <AlertTriangle size={15} aria-hidden="true" />
              ) : (
                <Crosshair size={15} aria-hidden="true" />
              )}
              <span>{missCandidateState.message}</span>
              {missCandidateState.phase ===
              "created_refresh_failed" ? (
                <Button
                  onClick={() => onNavigate("label")}
                >
                  질문 검토 다시 열기
                </Button>
              ) : null}
            </p>
          ) : playbackError ? (
            <p className="inline-error">{playbackError}</p>
          ) : null}
        </GlassPanel>

        <GlassPanel className="map-panel">
          {gpxTrack ? (
            <Suspense
              fallback={
                <div
                  className="history-map-wrap history-map-unavailable"
                  role="status"
                  aria-live="polite"
                >
                  <div className="history-map-unavailable__icon">
                    <MapPin size={30} />
                  </div>
                  <Badge tone="neutral">지도 준비 중</Badge>
                  <h2>경로 모듈을 불러오고 있습니다</h2>
                  <p>
                    영상과 사건 타임라인은 유지한 채 로컬 지도 모듈만 분리해
                    준비합니다.
                  </p>
                </div>
              }
            >
              <HistoryMap
                sourceFileName={summary.sourceFileName}
                candidateCount={candidates.length}
                sourceLocation={sourceLocation}
                track={gpxTrack}
                currentVideoTimeSeconds={synchronizedTimeSeconds}
                candidates={candidates}
                selectedCandidateId={selectedCandidate.candidateId}
                approvedCandidateIds={approvedCandidateIds}
                onSelectCandidate={selectCandidateById}
              />
            </Suspense>
          ) : (
            <div
              className="history-map-wrap history-map-unavailable"
              role="status"
              aria-live="polite"
            >
              <div className="history-map-unavailable__icon">
                <MapPinOff size={30} />
              </div>
              <Badge tone={gpxLoading ? "accent" : "neutral"}>
                {gpxLoading ? "GPX 확인 중" : "GPX 필요"}
              </Badge>
              <h2>
                {gpxLoading
                  ? "실제 이동 경로를 확인하고 있습니다"
                  : "실제 이동 경로 자료가 없습니다"}
              </h2>
              <p>
                {gpxLoading
                  ? "선택한 GPX의 시간 정보와 현재 주행의 범위를 검증합니다."
                  : sourceLocation
                    ? "촬영 시작 좌표 한 점으로 경로를 만들지 않습니다. 오른쪽 위 GPX 연결에서 시간 정보가 있는 원본을 선택하세요."
                    : "오른쪽 위 GPX 연결에서 시간 정보가 있는 원본을 선택하면 실제 경로만 표시합니다."}
              </p>
            </div>
          )}
          {gpxError ? (
            <p className="map-inline-error" role="alert">
              {gpxError}
            </p>
          ) : null}
        </GlassPanel>
      </div>

      <GlassPanel className="timeline-panel">
        <div className="timeline-header">
          <div>
            <strong>원본 영상 후보 타임라인</strong>
            <span>
              {candidates.length}개 후보 ·{" "}
              {gpxTrack
                ? `GPX ${gpxTrack.pointCount.toLocaleString("ko-KR")}점 · 오프셋 ${gpxTrack.videoOffsetSeconds.toFixed(1)}초`
                : sourceLocation
                  ? "시작 좌표만 · 경로 없음"
                  : "GPS 자료 없음"}
            </span>
          </div>
          <span className="mono">
            {formatTime(synchronizedTimeSeconds, true)} /{" "}
            {formatTime(
              missReviewFrame?.indexedDurationSeconds ??
                durationSeconds,
            )}
          </span>
        </div>
        <div className="timeline-track">
          <span className="timeline-progress" style={{ width: `${tripProgress}%` }} />
          {candidates.map((candidate, index) => (
            <button
              type="button"
              key={candidate.candidateId}
              className={`timeline-event ${
                candidate.candidateId === selectedCandidate.candidateId ? "active" : ""
              }`}
              style={{
                left: `${Math.max(
                  0,
                  Math.min(
                    100,
                    (candidate.representativeTimestampSeconds / durationSeconds) * 100,
                  ),
                )}%`,
              }}
              onClick={() => selectCandidate(candidate)}
              aria-label={`${formatTime(
                candidate.representativeTimestampSeconds,
                true,
              )} ${
                incidentsByCandidateId.has(candidate.candidateId)
                  ? "승인 사건"
                  : "미확정 후보"
              } ${index + 1}`}
            >
              <span>C-{String(index + 1).padStart(3, "0")}</span>
            </button>
          ))}
        </div>
      </GlassPanel>

      <div className="history-bottom">
        <GlassPanel className="incident-list-panel">
          <header>
            <div>
              <p className="eyebrow">승인 사건 · 미확정 후보</p>
              <h2>사건·후보 목록</h2>
            </div>
            <Badge tone={incidents.length ? "success" : "warning"}>
              승인 {mappedIncidentCount} · 후보{" "}
              {candidates.length - mappedIncidentCount}
            </Badge>
          </header>
          <div
            className="incident-list history-scroll-owner"
            role="region"
            aria-label="사건과 후보 목록"
            tabIndex={0}
          >
            {candidates.map((candidate, index) => {
              const incident = incidentsByCandidateId.get(candidate.candidateId);
              return (
                <button
                  type="button"
                  key={candidate.candidateId}
                  className={
                    candidate.candidateId === selectedCandidate.candidateId
                      ? "active"
                      : ""
                  }
                  onClick={() => selectCandidate(candidate)}
                  title={
                    incident
                      ? `영구 Incident ${incident.incidentId}`
                      : `미확정 후보 ${candidate.candidateId}`
                  }
                >
                  <span className="mono">
                    {formatTime(candidate.representativeTimestampSeconds)}
                  </span>
                  <i
                    className={`severity ${
                      incident?.severity === "urgent"
                        ? "severity--긴급"
                        : incident
                          ? ""
                          : "severity--미판정"
                    }`}
                    aria-hidden="true"
                  />
                  <span>
                    <strong>
                      {incident
                        ? persistentIssueLabel(incident.issueType)
                        : `장면 후보 ${index + 1}`}
                    </strong>
                    <small>
                      {incident
                        ? `INC ${incident.incidentId} · ${persistentSeverityLabel(
                            incident.severity,
                          )}`
                        : `${candidate.candidateId.slice(0, 10)}… · 장면 ${candidate.sceneScore.toFixed(
                            3,
                          )}`}
                    </small>
                  </span>
                  <ChevronRight size={17} />
                </button>
              );
            })}
          </div>
        </GlassPanel>

        <GlassPanel className="evidence-card-panel">
          <header className="evidence-card-header">
            <div>
              <p className="eyebrow">
                {selectedIncident
                  ? `승인 사건 · ${selectedIncident.incidentId}`
                  : `장면 후보 · ${selectedCandidate.candidateId.slice(0, 10)}…`}
              </p>
              <h2>
                {selectedIncident
                  ? persistentIssueLabel(selectedIncident.issueType)
                  : `장면 후보 ${selectedIndex + 1}`}
              </h2>
            </div>
            <div className="header-actions">
              <Button onClick={() => navigateWithCandidate("review")}>
                <Eye size={16} /> 같은 시각 검토
              </Button>
              <Button variant="primary" onClick={() => navigateWithCandidate("label")}>
                <Tags size={16} /> 질문 검토
              </Button>
            </div>
          </header>
          <div className="evidence-card-content">
            <div className="evidence-variants evidence-variants--single">
              <div className="evidence-still">
                <img src={imageUrl} alt="선택한 실제 분석 후보 대표 프레임" />
                <span>실제 대표 프레임</span>
              </div>
            </div>
            <div className="evidence-details">
              <div className="evidence-title">
                <div>
                  <Badge tone={selectedIncident ? "success" : "warning"}>
                    {selectedIncident
                      ? `승인 · ${persistentSeverityLabel(
                          selectedIncident.severity,
                        )}`
                      : "미분류 장면 후보"}
                  </Badge>
                  <span className="mono">
                    {formatTime(selectedCandidate.representativeTimestampSeconds, true)}
                  </span>
                </div>
                {selectedGpxPosition ? (
                  <MapPin size={19} aria-label="후보의 GPX 위치 확인됨" />
                ) : sourceLocation ? (
                  <MapPin size={19} aria-label="촬영 시작 좌표만 있음" />
                ) : (
                  <MapPinOff size={19} aria-label="GPS 자료 없음" />
                )}
              </div>
              <DefinitionList rows={evidenceRows} />
              {incidentError ? (
                <p className="inline-error">{incidentError}</p>
              ) : null}
            </div>
          </div>
        </GlassPanel>
      </div>
    </div>
  );
}
