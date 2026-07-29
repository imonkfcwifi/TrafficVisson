import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronRight,
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
} from "lucide-react";
import {
  loadSelectedCandidateId,
  saveSelectedCandidateId,
} from "../candidateSelection";
import { HistoryMap } from "../components/HistoryMap";
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
  parseActivePersistentIncidents,
  persistentIssueLabel,
  persistentSeverityLabel,
  type PersistentIncident,
} from "../persistentIncidents";
import type { AnalysisCandidate } from "../runtimeState";
import { useRuntimeState } from "../runtimeState";

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

export function HistoryScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const runtime = useRuntimeState();
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
    if (!summary || !window.trafficVisson) {
      setGpxTrack(null);
      setGpxLoading(false);
      return;
    }
    let active = true;
    setGpxLoading(true);
    setGpxError(null);
    void window.trafficVisson.gps
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
    if (!summary || !window.trafficVisson) {
      setIncidents([]);
      return;
    }
    let active = true;
    setIncidentError(null);
    void window.trafficVisson.incidents
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

  if (!summary || !selectedCandidate) {
    return (
      <div className="screen screen--history">
        <header className="screen-header">
          <div>
            <p className="eyebrow">SYNCHRONIZED EVIDENCE</p>
            <h1>영상·후보 히스토리</h1>
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
  const currentGpxPosition = gpxTrack
    ? interpolateGpxPosition(gpxTrack, absoluteTimeSeconds)
    : null;
  const selectedGpxPosition = gpxTrack
    ? interpolateGpxPosition(
        gpxTrack,
        selectedCandidate.representativeTimestampSeconds,
      )
    : null;
  const tripProgress = Math.max(
    0,
    Math.min(100, (absoluteTimeSeconds / durationSeconds) * 100),
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
    if (!window.trafficVisson || !summary) return;
    setGpxError(null);
    try {
      const selection = await window.trafficVisson.dialogs.selectGpxFile();
      if (!selection.selectionId || !selection.file) return;
      setGpxLoading(true);
      const value = await window.trafficVisson.gps.importTrack({
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
    if (!window.trafficVisson || !summary || !gpxTrack) return;
    setGpxLoading(true);
    setGpxError(null);
    try {
      const value = await window.trafficVisson.gps.updateOffset({
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
          <p className="eyebrow">SYNCHRONIZED EVIDENCE</p>
          <h1>영상·후보 히스토리</h1>
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
            disabled={gpxLoading || !window.trafficVisson}
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
          <div className="video-frame">
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
                    event.currentTarget.duration || selectedCandidate.previewDurationSeconds,
                    selectedCandidate.representativeTimestampSeconds -
                      selectedCandidate.previewStartSeconds,
                  ),
                );
                event.currentTarget.currentTime = target;
                event.currentTarget.playbackRate = rate;
                setClipTimeSeconds(target);
              }}
              onTimeUpdate={(event) => setClipTimeSeconds(event.currentTarget.currentTime)}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onEnded={() => setPlaying(false)}
              onError={() => {
                setPlaying(false);
                setPlaybackError("저장된 후보 클립을 불러오지 못했습니다.");
              }}
            >
              이 환경에서는 후보 미리보기 영상을 재생할 수 없습니다.
            </video>
            <div className="video-status video-status--left">
              <span className="record-dot" />
              <span className="mono">
                {formatTime(absoluteTimeSeconds, true)} / {formatTime(durationSeconds)}
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
              <span>후보 클립 {selectedIndex + 1}/{candidates.length}</span>
            </div>
          </div>
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
            <IconButton label="클립 2초 뒤로" onClick={() => setVideoTime(clipTimeSeconds - 2)}>
              <Rewind size={18} />
            </IconButton>
            <input
              className="video-scrubber"
              aria-label="현재 후보 클립 재생 위치"
              type="range"
              min={0}
              max={Math.max(selectedCandidate.previewDurationSeconds, 0.001)}
              step={0.01}
              value={clipTimeSeconds}
              onChange={(event) => setVideoTime(Number(event.target.value))}
              style={{ "--progress": `${clipProgress}%` } as React.CSSProperties}
            />
            <IconButton label="클립 2초 앞으로" onClick={() => setVideoTime(clipTimeSeconds + 2)}>
              <FastForward size={18} />
            </IconButton>
            <span className="mono">{formatTime(clipTimeSeconds, true)}</span>
          </div>
          {playbackError ? <p className="inline-error">{playbackError}</p> : null}
        </GlassPanel>

        <GlassPanel className="map-panel">
          <HistoryMap
            sourceFileName={summary.sourceFileName}
            candidateCount={candidates.length}
            sourceLocation={sourceLocation}
            track={gpxTrack}
            currentVideoTimeSeconds={absoluteTimeSeconds}
            candidates={candidates}
            selectedCandidateId={selectedCandidate.candidateId}
            approvedCandidateIds={approvedCandidateIds}
            onSelectCandidate={selectCandidateById}
          />
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
            {formatTime(absoluteTimeSeconds, true)} / {formatTime(durationSeconds)}
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
              <p className="eyebrow">INCIDENTS · CANDIDATES</p>
              <h2>사건·후보 목록</h2>
            </div>
            <Badge tone={incidents.length ? "success" : "warning"}>
              승인 {mappedIncidentCount} · 후보{" "}
              {candidates.length - mappedIncidentCount}
            </Badge>
          </header>
          <div className="incident-list">
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
                  ? `INCIDENT · ${selectedIncident.incidentId}`
                  : `CANDIDATE · ${selectedCandidate.candidateId.slice(0, 10)}…`}
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
                <Tags size={16} /> 질문형 라벨링
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
