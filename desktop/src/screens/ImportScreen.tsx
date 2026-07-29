import { useEffect, useRef, useState } from "react";
import {
  Check,
  CircleAlert,
  FileSearch,
  FileVideo2,
  FolderOpen,
  Link2,
  Network,
  Settings2,
  ShieldCheck,
  UploadCloud,
} from "lucide-react";
import type { ScreenId } from "../demo";
import {
  requireActiveProjectId,
  useProjectCatalog,
} from "../projectCatalog";
import {
  parsePipelineJob,
  parsePipelineList,
  parsePipelineProfile,
  parsePipelineStart,
  pipelineStageLabel,
  pipelineStatusLabel,
  publishPipelineJob,
  usePipelineUi,
  type PipelineJob,
  type PipelineProfile,
} from "../pipelineJobs";
import {
  Badge,
  Button,
  GlassPanel,
  PanelHeader,
  ProgressBar,
  StatusDot,
} from "../components/ui";

const NETWORK_PATH =
  "\\\\192.168.0.234\\도로연구실\\9. 한국교통연구원\\01.2025년 민자고속도로 기술부문 실사 위탁용역\\2. 연구수행\\03. 3분기 실사점검\\새 폴더";
const SAMPLE_TEMPLATES = {
  "sample-q3": {
    label: "3분기 실사 양식",
    sha256:
      "24ffb4bc62cd86a6c72d3a3829b8f1ddf1c824d7bf42452e7c495877a227bcb9",
  },
  "sample-q4": {
    label: "4분기 실사 양식",
    sha256:
      "bbdd61a6ef192a957a5e9480b0c2eedbfe956dd184abd5d76f59474254941539",
  },
} as const;
const COMMON_MODEL_PACK_ID = "trafficvisson-road-common";
const MAX_FOLDER_PIPELINE_JOBS = 128;

type InspectionState = "idle" | "checking" | "waiting" | "ready";

type MediaCandidate = {
  name: string;
  size: string;
  changed: string;
  state: string;
};

type SnapshotResponse = {
  snapshot_id: string;
  snapshot_digest: string;
  file_count: number;
  total_size_bytes: number;
  files: Array<{
    name: string;
    size_bytes: number;
    created_time_ns: number;
    modified_time_ns: number;
  }>;
};

type StabilityResponse = {
  stable: boolean;
  reason: string;
  stable_snapshot_id: string | null;
  file_count: number;
  total_size_bytes: number;
  observed_interval_seconds: [number, number];
};

type ProbeResponse = {
  snapshot_id?: string;
  snapshot_digest?: string;
  file_name: string;
  size_bytes: number;
  duration_seconds: number;
  video_streams: Array<{
    codec_name: string;
    width: number;
    height: number;
    frame_rate: string;
  }>;
  ffprobe_status: "validated";
  content_hash_status: "not_computed";
};

type ImportInspectionResponse = {
  inspected_source_id: string;
};

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function wait(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

function formatBytes(value: number) {
  if (value >= 1_000_000_000) return `${(value / 1_000_000_000).toFixed(2)} GB`;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)} MB`;
  return `${Math.max(0, value).toLocaleString("ko-KR")} B`;
}

function safeErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : "입력 검사를 완료하지 못했습니다.";
}

function compactSourceTail(value: string) {
  const parts = value.split(/[\\/]+/).filter(Boolean);
  if (parts.length === 0) return value;
  return parts.at(-1) ?? value;
}

export function ImportScreen(_props: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const pipeline = usePipelineUi();
  const projectCatalog = useProjectCatalog();
  const currentPipeline =
    pipeline.projectId === projectCatalog.activeProjectId
      ? pipeline.job
      : null;
  const [sourceType, setSourceType] = useState<"network" | "local" | "url">("network");
  const [source, setSource] = useState(NETWORK_PATH);
  const [rightsConfirmed, setRightsConfirmed] = useState(false);
  const [inspection, setInspection] = useState<InspectionState>("idle");
  const [selectedFile, setSelectedFile] = useState("");
  const [candidates, setCandidates] = useState<MediaCandidate[]>([]);
  const [inspectionProgress, setInspectionProgress] = useState(0);
  const [inspectionMessage, setInspectionMessage] = useState("검사 전");
  const [inspectionError, setInspectionError] = useState<string | null>(null);
  const [probeResult, setProbeResult] = useState<ProbeResponse | null>(null);
  const [stableSummary, setStableSummary] = useState<StabilityResponse | null>(null);
  const [localSelectionId, setLocalSelectionId] = useState<string | null>(null);
  const [validatedFiles, setValidatedFiles] = useState<string[]>([]);
  const [batchSnapshotId, setBatchSnapshotId] = useState<string | null>(null);
  const [batchJobs, setBatchJobs] = useState<PipelineJob[]>([]);
  const [pipelineStarting, setPipelineStarting] = useState(false);
  const [pipelineError, setPipelineError] = useState<string | null>(null);
  const [profile, setProfile] = useState<PipelineProfile | null>(null);
  const [profileState, setProfileState] = useState<
    "loading" | "ready" | "missing" | "saving" | "error"
  >("loading");
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [reviewMode, setReviewMode] =
    useState<"precision" | "quick">("precision");
  const [questionPolicy, setQuestionPolicy] = useState<
    "learning_assist" | "full_review" | "quick_review"
  >("learning_assist");
  const [templateId, setTemplateId] =
    useState<keyof typeof SAMPLE_TEMPLATES>("sample-q3");
  const [reportProvider, setReportProvider] =
    useState<"codex" | "claude" | "none">("codex");
  const [candidateRescanFps, setCandidateRescanFps] =
    useState<5 | 8 | 10>(8);
  const fileInput = useRef<HTMLInputElement>(null);
  const inspectionRun = useRef(0);

  useEffect(() => {
    const projectId = projectCatalog.activeProjectId;
    if (!window.trafficSuperVision?.pipeline || !projectId) {
      setProfile(null);
      setProfileState(window.trafficSuperVision ? "error" : "loading");
      if (window.trafficSuperVision) {
        setProfileError(
          "새 PipelineJob 연결을 적용하려면 개발용 Electron을 다시 시작해야 합니다.",
        );
      }
      return;
    }
    let active = true;
    setProfileState("loading");
    setProfileError(null);
    void window.trafficSuperVision.pipeline
      .getProfile({ projectId })
      .then(parsePipelineProfile)
      .then((value) => {
        if (!active) return;
        setProfile(value);
        setReviewMode(value.reviewMode);
        setQuestionPolicy(value.questionPolicy);
        if (value.defaultTemplateId in SAMPLE_TEMPLATES) {
          setTemplateId(
            value.defaultTemplateId as keyof typeof SAMPLE_TEMPLATES,
          );
        }
        if (
          value.reportProvider === "codex" ||
          value.reportProvider === "claude" ||
          value.reportProvider === "none"
        ) {
          setReportProvider(value.reportProvider);
        }
        if ([5, 8, 10].includes(value.candidateRescanFps)) {
          setCandidateRescanFps(value.candidateRescanFps as 5 | 8 | 10);
        }
        setProfileState("ready");
      })
      .catch(() => {
        if (!active) return;
        setProfile(null);
        setProfileState("missing");
        setProfileOpen(true);
      });
    return () => {
      active = false;
    };
  }, [projectCatalog.activeProjectId]);

  useEffect(() => {
    const projectId = projectCatalog.activeProjectId;
    const snapshotId = batchSnapshotId ?? currentPipeline?.snapshotId ?? null;
    const service = window.trafficSuperVision?.pipeline;
    if (!projectId || !snapshotId || !service) return;
    let active = true;
    let fetching = false;
    const refresh = () => {
      if (!active || fetching) return;
      fetching = true;
      void service
        .list({ projectId })
        .then(parsePipelineList)
        .then((jobs) => {
          if (!active) return;
          const matching = jobs
            .filter((job) => job.snapshotId === snapshotId)
            .sort(
              (left, right) =>
                Date.parse(left.createdAt) - Date.parse(right.createdAt),
            );
          setBatchJobs(matching);
          if (!batchSnapshotId && matching.length) {
            setBatchSnapshotId(snapshotId);
          }
        })
        .catch(() => {
          // The selected PipelineJob remains usable if the batch summary
          // cannot be refreshed temporarily.
        })
        .finally(() => {
          fetching = false;
        });
    };
    refresh();
    const timer = window.setInterval(refresh, 1_200);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [
    batchSnapshotId,
    currentPipeline?.snapshotId,
    projectCatalog.activeProjectId,
  ]);

  async function resolveProjectId() {
    return requireActiveProjectId();
  }

  function invalidateInspection({
    preserveCandidates = false,
    message = "입력이 변경되었습니다. 다시 검사하세요.",
  }: {
    preserveCandidates?: boolean;
    message?: string;
  } = {}) {
    inspectionRun.current += 1;
    setInspection("idle");
    setInspectionProgress(0);
    setInspectionMessage(message);
    setInspectionError(null);
    setProbeResult(null);
    setStableSummary(null);
    setValidatedFiles([]);
    if (!preserveCandidates) {
      setCandidates([]);
      setBatchSnapshotId(null);
      setBatchJobs([]);
    }
  }

  async function startPersistentPipelines(
    projectId: string,
    snapshotId: string,
    fileNames: readonly string[],
  ) {
    if (!window.trafficSuperVision?.pipeline) {
      setPipelineError(
        "자동 파이프라인 브리지를 사용할 수 없습니다. 앱을 다시 시작해 주세요.",
      );
      return;
    }
    if (profileState !== "ready" || !profile) {
      setProfileOpen(true);
      setPipelineError(
        "영상 분석 전에 이 프로젝트의 최초 설정을 한 번 저장해야 합니다.",
      );
      return;
    }
    const uniqueFileNames = Array.from(new Set(fileNames));
    if (
      uniqueFileNames.length < 1 ||
      uniqueFileNames.length > MAX_FOLDER_PIPELINE_JOBS
    ) {
      setPipelineError(
        uniqueFileNames.length > MAX_FOLDER_PIPELINE_JOBS
          ? `한 폴더에서 최대 ${MAX_FOLDER_PIPELINE_JOBS}개 영상까지 안전하게 대기열에 등록할 수 있습니다.`
          : "분석할 수 있는 영상이 없습니다.",
      );
      return;
    }
    setPipelineStarting(true);
    setPipelineError(null);
    try {
      const startedJobs: PipelineJob[] = [];
      const failedFiles: string[] = [];
      let replayedCount = 0;
      for (let index = 0; index < uniqueFileNames.length; index += 1) {
        const fileName = uniqueFileNames[index];
        setInspectionMessage(
          `폴더 전체 자동 분석 대기열 등록 ${index + 1}/${uniqueFileNames.length}`,
        );
        try {
          const started = parsePipelineStart(
            await window.trafficSuperVision.pipeline.start({
              projectId,
              snapshotId,
              fileName,
              rightsConfirmed: true,
              idempotencyKey: `pipeline-start-${snapshotId}-${String(
                index + 1,
              ).padStart(3, "0")}`,
            }),
          );
          startedJobs.push(started.job);
          if (started.idempotentReplay) replayedCount += 1;
          setCandidates((current) =>
            current.map((candidate) =>
              candidate.name === fileName
                ? {
                    ...candidate,
                    state: started.idempotentReplay
                      ? "기존 작업 복구"
                      : `대기열 ${index + 1}/${uniqueFileNames.length}`,
                  }
                : candidate,
            ),
          );
        } catch {
          failedFiles.push(fileName);
          setCandidates((current) =>
            current.map((candidate) =>
              candidate.name === fileName
                ? { ...candidate, state: "대기열 등록 실패" }
                : candidate,
            ),
          );
        }
      }
      if (startedJobs.length < 1) {
        throw new Error("폴더의 영상을 자동 분석 대기열에 등록하지 못했습니다.");
      }
      setBatchSnapshotId(snapshotId);
      setBatchJobs(startedJobs);
      publishPipelineJob(startedJobs[0]);
      setInspectionMessage(
        failedFiles.length
          ? `${startedJobs.length}개 등록 · ${failedFiles.length}개 등록 실패`
          : replayedCount === startedJobs.length
            ? `${startedJobs.length}개 기존 자동 분석 작업을 복구했습니다.`
            : `폴더 영상 ${startedJobs.length}개를 모두 자동 분석 대기열에 등록했습니다.`,
      );
      if (failedFiles.length) {
        setPipelineError(
          `${failedFiles.length}개 영상은 대기열 등록에 실패했습니다. 실패 항목을 확인한 뒤 다시 시도하세요.`,
        );
      }
    } catch (error) {
      const message = safeErrorMessage(error);
      setPipelineError(
        /profile|프로필|configuration/i.test(message)
          ? "이 프로젝트의 최초 설정이 필요합니다. 판정 방식·질문 정책·양식·모델팩·Provider·저장 위치를 한 번 저장해 주세요."
          : message,
      );
    } finally {
      setPipelineStarting(false);
    }
  }

  async function savePipelineProfile() {
    if (!window.trafficSuperVision?.pipeline) {
      setProfileError(
        "자동 파이프라인 브리지를 사용할 수 없습니다. 앱을 다시 시작해 주세요.",
      );
      return;
    }
    const projectId = await resolveProjectId();
    const template = SAMPLE_TEMPLATES[templateId];
    setProfileState("saving");
    setProfileError(null);
    try {
      const saved = parsePipelineProfile(
        await window.trafficSuperVision.pipeline.configureProfile({
          projectId,
          questionPolicy,
          reviewMode,
          defaultTemplateId: templateId,
          defaultTemplateSha256: template.sha256,
          detectorPackId: COMMON_MODEL_PACK_ID,
          ocrPackId: COMMON_MODEL_PACK_ID,
          sam2PackId: COMMON_MODEL_PACK_ID,
          reportProvider,
          outputLocationId: "project-default",
          candidateRescanFps,
        }),
      );
      setProfile(saved);
      setProfileState("ready");
      setProfileOpen(false);
      setPipelineError(null);
    } catch (error) {
      setProfileState("error");
      setProfileError(
        error instanceof Error
          ? error.message
          : "프로젝트 설정을 저장하지 못했습니다.",
      );
    }
  }

  async function inspectSource(autoStartPipeline = false) {
    if (!rightsConfirmed || !source.trim()) return;
    const run = inspectionRun.current + 1;
    inspectionRun.current = run;
    setInspection("checking");
    setInspectionError(null);
    setProbeResult(null);
    setStableSummary(null);
    setInspectionProgress(8);
    setInspectionMessage("첫 번째 메타데이터 스냅샷을 읽는 중");

    try {
      if (!window.trafficSuperVision) {
        if (run !== inspectionRun.current) return;
        setInspectionProgress(0);
        setInspectionMessage("설치형 앱에서 실제 입력 검사가 필요합니다.");
        setInspectionError(
          "브라우저 화면은 파일·프로젝트·SHA-256 검증 결과를 만들지 않습니다. TrafficSuperVision 설치형 앱에서 다시 검사하세요.",
        );
        setInspection("waiting");
        return;
      }

      if (sourceType === "network") {
        const projectId = await resolveProjectId();
        if (run !== inspectionRun.current) return;
        const snapshots: SnapshotResponse[] = [];
        for (let index = 0; index < 3; index += 1) {
          if (run !== inspectionRun.current) return;
          const snapshot = (await window.trafficSuperVision.media.captureSnapshot({
            projectId,
            directory: source,
          })) as SnapshotResponse;
          if (run !== inspectionRun.current) return;
          snapshots.push(snapshot);
          setCandidates(
            snapshot.files.map((file) => ({
              name: file.name,
              size: formatBytes(file.size_bytes),
              changed: new Date(file.modified_time_ns / 1_000_000).toLocaleString("ko-KR"),
              state: `${index + 1}/3 스냅샷`,
            })),
          );
          setInspectionProgress(20 + index * 28);
          if (index < 2) {
            setInspectionMessage(
              `${index + 1}/3 완료 · 다음 크기 비교까지 10초`,
            );
            await wait(10_050);
          }
        }
        if (run !== inspectionRun.current) return;
        setInspectionMessage("A/B/C 안정성을 비교하는 중");
        const stability = (await window.trafficSuperVision.media.compareStability({
          projectId,
          snapshotIds: snapshots.map((snapshot) => snapshot.snapshot_id),
        })) as StabilityResponse;
        if (run !== inspectionRun.current) return;
        setStableSummary(stability);
        if (!stability.stable || !stability.stable_snapshot_id) {
          setInspection("waiting");
          setInspectionProgress(100);
          setInspectionMessage(`안정성 미확인 · ${stability.reason}`);
          return;
        }
        const names = snapshots[2].files.map((file) => file.name);
        if (names.length < 1) {
          setInspection("waiting");
          setInspectionMessage("지원되는 영상이 없습니다.");
          return;
        }
        if (names.length > MAX_FOLDER_PIPELINE_JOBS) {
          setInspection("waiting");
          setInspectionProgress(100);
          setInspectionMessage(
            `지원 영상 ${names.length}개 · 한 폴더 최대 ${MAX_FOLDER_PIPELINE_JOBS}개를 초과했습니다.`,
          );
          setInspectionError(
            `현재 안전 대기열 한도는 ${MAX_FOLDER_PIPELINE_JOBS}개입니다. 폴더를 나누어 다시 선택하세요.`,
          );
          return;
        }
        setSelectedFile(names.includes(selectedFile) ? selectedFile : names[0]);
        setCandidates((current) =>
          current.map((file) => ({ ...file, state: "A/B/C 안정" })),
        );
        const probes: ProbeResponse[] = [];
        const rejected: string[] = [];
        for (let index = 0; index < names.length; index += 1) {
          if (run !== inspectionRun.current) return;
          const fileName = names[index];
          setInspectionMessage(
            `폴더 전체 스트림 검증 ${index + 1}/${names.length} · ${fileName}`,
          );
          setInspectionProgress(
            86 + Math.round(((index + 1) / names.length) * 10),
          );
          try {
            const probe = (await window.trafficSuperVision.media.probe({
              projectId,
              snapshotId: stability.stable_snapshot_id,
              fileName,
              timeoutSeconds: 30,
            })) as ProbeResponse;
            if (run !== inspectionRun.current) return;
            probes.push(probe);
            setCandidates((current) =>
              current.map((file) =>
                file.name === fileName
                  ? { ...file, state: "스트림 검증 완료" }
                  : file,
              ),
            );
          } catch {
            rejected.push(fileName);
            setCandidates((current) =>
              current.map((file) =>
                file.name === fileName
                  ? { ...file, state: "스트림 검증 실패" }
                  : file,
              ),
            );
          }
        }
        if (probes.length < 1) {
          setInspection("waiting");
          setInspectionProgress(100);
          setInspectionMessage("폴더에서 검증 가능한 영상이 없습니다.");
          setInspectionError(
            `${rejected.length}개 영상이 모두 ffprobe 검증을 통과하지 못했습니다.`,
          );
          return;
        }
        const validatedNames = probes.map((probe) => probe.file_name);
        setValidatedFiles(validatedNames);
        setProbeResult(probes[0]);
        setInspectionProgress(100);
        setInspectionMessage(
          rejected.length
            ? `${validatedNames.length}개 검증 완료 · ${rejected.length}개 제외`
            : `폴더 영상 ${validatedNames.length}개 전체 검증 완료`,
        );
        setInspection("ready");
        if (autoStartPipeline) {
          await startPersistentPipelines(
            projectId,
            stability.stable_snapshot_id,
            validatedNames,
          );
        }
        return;
      }

      if (sourceType === "local" && localSelectionId) {
        const projectId = await resolveProjectId();
        if (run !== inspectionRun.current) return;
        const inspected = (await window.trafficSuperVision.imports.inspect({
          projectId,
          sourceType: "local_selection",
          localSelectionId,
        })) as ImportInspectionResponse;
        if (
          typeof inspected.inspected_source_id !== "string" ||
          !UUID_PATTERN.test(inspected.inspected_source_id)
        ) {
          throw new Error("입력 검사 식별자를 검증하지 못했습니다.");
        }
        if (run !== inspectionRun.current) return;
        setInspectionProgress(35);
        setInspectionMessage("선택 파일을 안전한 미디어 입력으로 등록하는 중");
        const snapshot =
          (await window.trafficSuperVision.media.registerLocalSelection({
            projectId,
            localSelectionId,
          })) as SnapshotResponse;
        if (run !== inspectionRun.current) return;
        const selected = snapshot.files[0];
        if (!selected || snapshot.file_count !== 1) {
          throw new Error("선택한 로컬 영상을 등록하지 못했습니다.");
        }
        setSelectedFile(selected.name);
        setCandidates([
          {
            name: selected.name,
            size: formatBytes(selected.size_bytes),
            changed: new Date(
              selected.modified_time_ns / 1_000_000,
            ).toLocaleString("ko-KR"),
            state: "로컬 선택 등록",
          },
        ]);
        const stability: StabilityResponse = {
          stable: true,
          reason: "stable",
          stable_snapshot_id: snapshot.snapshot_id,
          file_count: 1,
          total_size_bytes: selected.size_bytes,
          observed_interval_seconds: [0, 0],
        };
        setStableSummary(stability);
        setInspectionProgress(65);
        setInspectionMessage(`${selected.name} 미디어 스트림 검증 중`);
        const probe = (await window.trafficSuperVision.media.probe({
          projectId,
          snapshotId: snapshot.snapshot_id,
          fileName: selected.name,
          timeoutSeconds: 30,
        })) as ProbeResponse;
        if (run !== inspectionRun.current) return;
        setProbeResult(probe);
        setValidatedFiles([selected.name]);
        setInspectionProgress(100);
        setInspectionMessage("로컬 선택·스트림 검증 완료");
        setInspection("ready");
        if (autoStartPipeline) {
          await startPersistentPipelines(
            projectId,
            snapshot.snapshot_id,
            [selected.name],
          );
        }
        return;
      }

      if (sourceType === "url") {
        const projectId = await resolveProjectId();
        if (run !== inspectionRun.current) return;
        const inspected = (await window.trafficSuperVision.imports.inspect({
          projectId,
          sourceType: "direct_url",
          sourceUrl: source,
        })) as ImportInspectionResponse;
        if (
          typeof inspected.inspected_source_id !== "string" ||
          !UUID_PATTERN.test(inspected.inspected_source_id)
        ) {
          throw new Error("URL 검사 식별자를 검증하지 못했습니다.");
        }
        if (run !== inspectionRun.current) return;
        setCandidates([]);
        setStableSummary(null);
        setProbeResult(null);
        setInspectionProgress(100);
        setInspectionMessage(
          "URL 형식·접근 정책 검사 완료 · 다운로드와 프로젝트 등록은 아직 연결되지 않음",
        );
        setInspection("waiting");
        return;
      }

      throw new Error("선택한 로컬 파일 핸들이 없습니다. 파일을 다시 선택하세요.");
    } catch (error) {
      if (run !== inspectionRun.current) return;
      setInspectionError(safeErrorMessage(error));
      setInspectionMessage("검사 실패");
      setInspection("waiting");
    }
  }

  async function cancelPipeline() {
    const projectId = await resolveProjectId();
    const job =
      currentPipeline?.projectId === projectId ? currentPipeline : null;
    if (!window.trafficSuperVision?.pipeline || !job) return;
    setPipelineError(null);
    try {
      publishPipelineJob(
        parsePipelineJob(
          await window.trafficSuperVision.pipeline.cancel({
            projectId: job.projectId,
            jobId: job.jobId,
          }),
        ),
      );
    } catch (error) {
      setPipelineError(safeErrorMessage(error));
    }
  }

  async function resumePipeline() {
    const projectId = await resolveProjectId();
    const job =
      currentPipeline?.projectId === projectId ? currentPipeline : null;
    if (!window.trafficSuperVision?.pipeline || !job) return;
    setPipelineError(null);
    try {
      publishPipelineJob(
        parsePipelineJob(
          await window.trafficSuperVision.pipeline.resume({
            projectId: job.projectId,
            jobId: job.jobId,
          }),
        ),
      );
    } catch (error) {
      setPipelineError(safeErrorMessage(error));
    }
  }

  async function chooseLocalFile() {
    if (window.trafficSuperVision) {
      const selection = await window.trafficSuperVision.dialogs.selectVideos();
      if (selection.files.length > 0) {
        invalidateInspection();
        setLocalSelectionId(selection.selectionId);
        setSource(selection.files[0].name);
        setSourceType("local");
      }
      return;
    }
    fileInput.current?.click();
  }

  const batchRunningCount = batchJobs.filter((job) =>
    ["queued", "running", "cancelling"].includes(job.status),
  ).length;
  const batchReviewCount = batchJobs.filter(
    (job) => job.status === "awaiting_review",
  ).length;
  const batchCompletedCount = batchJobs.filter(
    (job) => job.status === "completed",
  ).length;
  const batchProblemCount = batchJobs.filter((job) =>
    ["blocked", "failed", "cancelled"].includes(job.status),
  ).length;
  const batchJobByFile = new Map(
    batchJobs.map((job) => [job.fileName, job] as const),
  );

  return (
    <div className="screen screen--import">
      <header className="screen-header">
        <div>
          <p className="eyebrow">SAFE MEDIA INTAKE</p>
          <h1>영상 가져오기</h1>
          <p>입력 위치를 먼저 검사하고 권리·무결성·업로드 완료 여부를 확인합니다.</p>
        </div>
        <div className="header-actions">
          <Badge tone={profileState === "ready" ? "success" : "warning"}>
            {profileState === "ready" ? "1회 설정 완료" : "1회 설정 필요"}
          </Badge>
          <Button variant="ghost" onClick={() => setProfileOpen(true)}>
            <Settings2 size={16} />
            1회 설정 변경
          </Button>
        </div>
      </header>

      <div className="import-layout">
        <GlassPanel className="source-panel">
          <PanelHeader eyebrow="1 · SOURCE" title="어디에서 가져올까요?" />
          <div className="source-tabs" role="group" aria-label="입력 유형">
            <button
              type="button"
              className={sourceType === "network" ? "selected" : ""}
              onClick={() => {
                invalidateInspection();
                setSourceType("network");
                setSource(NETWORK_PATH);
                setLocalSelectionId(null);
              }}
            >
              <Network size={19} /> 네트워크 폴더
            </button>
            <button
              type="button"
              className={sourceType === "local" ? "selected" : ""}
              onClick={chooseLocalFile}
            >
              <FolderOpen size={19} /> 로컬 파일
            </button>
            <button
              type="button"
              className={sourceType === "url" ? "selected" : ""}
              onClick={() => {
                invalidateInspection();
                setSourceType("url");
                setSource("");
                setLocalSelectionId(null);
              }}
            >
              <Link2 size={19} /> 영상 링크 · 검사 전용
            </button>
          </div>

          <label className="field">
            <span className="field-label-row">
              <span>{sourceType === "url" ? "영상 URL" : "파일 또는 폴더 경로"}</span>
              {sourceType !== "url" && source.trim() ? (
                <code className="source-path-tail" title={source}>
                  대상 · {compactSourceTail(source)}
                </code>
              ) : null}
            </span>
            <div className="input-with-icon">
              {sourceType === "url" ? <Link2 size={18} /> : <Network size={18} />}
              <input
                value={source}
                title={source || undefined}
                readOnly={sourceType === "local"}
                onChange={(event) => {
                  invalidateInspection();
                  setSource(event.target.value);
                }}
                placeholder={
                  sourceType === "url"
                    ? "https://drive.google.com/…"
                    : "영상이 있는 경로를 선택하세요"
                }
              />
            </div>
          </label>
          <input
            ref={fileInput}
            hidden
            type="file"
            accept="video/mp4,video/quicktime,.mp4,.mov"
            onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              if (file) {
                invalidateInspection();
                setSource(file.name);
                setSourceType("local");
              }
            }}
          />

          <label className="rights-check">
            <input
              type="checkbox"
              checked={rightsConfirmed}
              onChange={(event) => {
                if (!event.target.checked) invalidateInspection();
                setRightsConfirmed(event.target.checked);
              }}
            />
            <span className="check-visual" aria-hidden="true">
              <Check size={14} />
            </span>
            <span>
              <strong>이 영상을 다운로드·분석할 권리가 있습니다.</strong>
              <small>필수 확인 · 원본 사이트 스크립트나 실행 파일은 절대 실행하지 않습니다.</small>
            </span>
          </label>

          <Button
            variant="primary"
            className="full-width"
            onClick={() => void inspectSource(true)}
            disabled={
              !rightsConfirmed ||
              !source.trim() ||
              inspection === "checking" ||
              pipelineStarting ||
              Boolean(
                currentPipeline &&
                  ["queued", "running", "cancelling"].includes(
                    currentPipeline.status,
                  ),
              )
            }
          >
            <FileSearch size={18} />
            {currentPipeline &&
            ["queued", "running", "cancelling"].includes(
              currentPipeline.status,
            )
              ? "자동 분석 진행 중"
              : inspection === "checking" || pipelineStarting
              ? "검사·자동 분석 준비 중…"
              : "권리 확인 · 분석 시작"}
          </Button>

          <div className="support-tiers">
            <h3>지원 등급</h3>
            {[
              ["연결됨", "로컬 MP4/MOV · 네트워크 폴더", "success"],
              ["검사 전용", "직접 URL · Drive · YouTube · 일반 웹페이지", "warning"],
              ["미구현", "URL 다운로드·프로젝트 등록", "neutral"],
              ["제외", "DRM · 암호화 · 권한 없는 비공개", "danger"],
            ].map(([tier, description, tone]) => (
              <div key={tier}>
                <Badge tone={tone as "success" | "warning" | "neutral" | "danger"}>
                  {tier}
                </Badge>
                <span>{description}</span>
              </div>
            ))}
          </div>
        </GlassPanel>

        <GlassPanel className="inspection-panel">
          <PanelHeader
            eyebrow="2 · INSPECT"
            title={
              sourceType === "network"
                ? "폴더 전체 업로드 확인"
                : "업로드 완료 확인"
            }
            action={
              inspection === "idle" ? (
                <Badge tone="neutral">검사 전</Badge>
              ) : inspection === "checking" ? (
                <Badge tone="accent">검사 중</Badge>
              ) : inspection === "ready" ? (
                <Badge tone="success">
                  입력 안정 · {validatedFiles.length || candidates.length}개
                </Badge>
              ) : sourceType === "url" && !inspectionError ? (
                <Badge tone="warning">URL 검사만 완료</Badge>
              ) : (
                <Badge tone="warning">대기 필요</Badge>
              )
            }
          />

          {inspection === "idle" ? (
            <div className="empty-state">
              <UploadCloud size={36} strokeWidth={1.5} />
              <h3>안전 검사를 시작하세요</h3>
              <p>파일명·용량·MIME·magic·수정 시각을 읽고 완료된 영상만 후보로 표시합니다.</p>
            </div>
          ) : inspection === "checking" ? (
            <div className="inspection-loading" aria-live="polite">
              <FileSearch size={32} />
              <h3>{inspectionMessage}</h3>
              <ProgressBar
                value={inspectionProgress}
                label={`입력 검사 진행률 ${inspectionProgress}%`}
              />
              <p>A/B/C 간격 비교 중에는 영상 본문을 읽지 않고 메타데이터만 확인합니다.</p>
            </div>
          ) : (
            <>
              <div
                className={`notice ${
                  inspection === "ready" ? "notice--success" : "notice--warning"
                }`}
              >
                {inspection === "ready" ? <Check size={20} /> : <CircleAlert size={20} />}
                <div>
                  <strong>
                    {inspection === "ready"
                      ? "업로드 안정성 확인 완료"
                      : sourceType === "url" && !inspectionError
                        ? "URL 안전성 검사 완료"
                        : "재검사 필요"}
                  </strong>
                  <p>
                    {inspectionError ??
                      (stableSummary
                        ? `${stableSummary.file_count}개 · ${formatBytes(
                            stableSummary.total_size_bytes,
                          )} · 간격 ${stableSummary.observed_interval_seconds
                            .map((value) => value.toFixed(1))
                            .join("초 / ")}초`
                        : inspectionMessage)}
                  </p>
                </div>
              </div>

              {batchJobs.length ? (
                <div className="notice notice--success" aria-live="polite">
                  <Check size={20} />
                  <div>
                    <strong>
                      폴더 영상 {batchJobs.length}개가 자동 분석 대기열에 있습니다.
                    </strong>
                    <p>
                      처리 중 {batchRunningCount} · 질문 대기 {batchReviewCount} ·
                      완료 {batchCompletedCount} · 확인 필요 {batchProblemCount}
                    </p>
                  </div>
                </div>
              ) : null}

              {candidates.length ? (
                <div className="file-candidates" aria-label={`발견된 영상 ${candidates.length}개`}>
                  {candidates.map((file) => {
                    const pipelineJob = batchJobByFile.get(file.name);
                    return (
                  <button
                    type="button"
                    key={file.name}
                    className={selectedFile === file.name ? "selected" : ""}
                    onClick={() => {
                      if (file.name === selectedFile) return;
                      setSelectedFile(file.name);
                    }}
                  >
                    <FileVideo2 size={20} />
                    <span>
                      <strong>{file.name}</strong>
                      <small>{file.size} · {file.changed}</small>
                    </span>
                    <Badge
                      tone={
                        pipelineJob?.status === "completed"
                          ? "success"
                          : pipelineJob &&
                              ["blocked", "failed", "cancelled"].includes(
                                pipelineJob.status,
                              )
                            ? "danger"
                            : pipelineJob
                              ? "accent"
                              : file.state.includes("실패")
                                ? "danger"
                                : "warning"
                      }
                    >
                      {pipelineJob
                        ? pipelineStatusLabel(pipelineJob)
                        : file.state}
                    </Badge>
                  </button>
                    );
                  })}
                </div>
              ) : null}

              {sourceType !== "url" ? (
                <div className="stability-rule">
                  <ShieldCheck size={20} />
                  <div>
                    <strong>완료 판정 규칙</strong>
                    <p>2회 연속 크기·수정 시각 동일 → 파일 잠금 해제 → ffprobe 성공 → SHA-256 확정</p>
                  </div>
                </div>
              ) : (
                <div className="stability-rule">
                  <CircleAlert size={20} />
                  <div>
                    <strong>현재 URL 기능은 검사 전용입니다.</strong>
                    <p>URL 다운로드, 영상 저장, 프로젝트 등록은 아직 연결되지 않았습니다.</p>
                  </div>
                </div>
              )}

              {!currentPipeline ? (
                <Button
                  variant="primary"
                  className="full-width"
                  onClick={() => void inspectSource(true)}
                  disabled={pipelineStarting}
                >
                  {sourceType === "url"
                    ? "URL 다시 검사"
                    : sourceType === "local"
                    ? "선택 파일 검사 · 자동 분석"
                    : "폴더 전체 A/B/C 검사 · 전체 자동 분석 시작"}
                </Button>
              ) : null}
            </>
          )}

          {pipelineError ? (
            <div className="notice notice--danger" role="alert">
              <CircleAlert size={20} />
              <div>
                <strong>자동 파이프라인을 시작하지 못했습니다</strong>
                <p>{pipelineError}</p>
              </div>
            </div>
          ) : null}

          {currentPipeline && inspection !== "ready" ? (
            <div className="notice notice--success" aria-live="polite">
              <Check size={20} />
              <div>
                <strong>재시작 전 자동 점검 작업을 복원했습니다</strong>
                <p>
                  {currentPipeline.fileName} ·{" "}
                  {pipelineStageLabel(currentPipeline.currentStage)} ·{" "}
                  {currentPipeline.progressPercent}%
                </p>
                <div className="pipeline-intake-status">
                  <div>
                    <strong>{pipelineStatusLabel(currentPipeline)}</strong>
                    <small>
                      새 영상을 다시 선택하지 않고 같은 PipelineJob에서
                      이어갑니다.
                    </small>
                  </div>
                  <ProgressBar
                    value={currentPipeline.progressPercent}
                    label={`자동 파이프라인 진행률 ${currentPipeline.progressPercent}%`}
                  />
                  {["queued", "running"].includes(currentPipeline.status) ? (
                    <Button variant="danger" onClick={cancelPipeline}>
                      파이프라인 취소
                    </Button>
                  ) : null}
                  {["blocked", "failed", "cancelled"].includes(
                    currentPipeline.status,
                  ) ? (
                    <Button variant="positive" onClick={resumePipeline}>
                      중단 단계에서 재개
                    </Button>
                  ) : null}
                </div>
              </div>
            </div>
          ) : null}

          {inspection === "ready" ? (
            <div className="notice notice--success" aria-live="polite">
              <Check size={20} />
              <div>
                <strong>입력 등록 준비 완료</strong>
                <p>
                  {probeResult
                    ? `${
                        sourceType === "network" && validatedFiles.length > 1
                          ? `폴더 전체 ${validatedFiles.length}개 · 대표 ${probeResult.file_name}`
                          : probeResult.file_name
                      } · ${probeResult.video_streams[0]?.codec_name.toUpperCase()} · ${
                        probeResult.video_streams[0]?.width
                      }×${probeResult.video_streams[0]?.height} · ${Math.round(
                        probeResult.duration_seconds,
                      )}초`
                    : `${selectedFile} · 메타데이터 검사 완료`}
                </p>
                {currentPipeline ? (
                  <div className="pipeline-intake-status">
                    <div>
                      <strong>{pipelineStatusLabel(currentPipeline)}</strong>
                      <small>
                        {pipelineStageLabel(currentPipeline.currentStage)} ·{" "}
                        {currentPipeline.progressPercent}%
                      </small>
                    </div>
                    <ProgressBar
                      value={currentPipeline.progressPercent}
                      label={`자동 파이프라인 진행률 ${currentPipeline.progressPercent}%`}
                    />
                    {["queued", "running"].includes(currentPipeline.status) ? (
                      <Button variant="danger" onClick={cancelPipeline}>
                        파이프라인 취소
                      </Button>
                    ) : null}
                    {["blocked", "failed", "cancelled"].includes(
                      currentPipeline.status,
                    ) ? (
                      <Button variant="positive" onClick={resumePipeline}>
                        준비 후 중단 단계에서 재개
                      </Button>
                    ) : null}
                  </div>
                ) : (
                  <small>
                    버튼을 다시 누르지 않아도 무결성 검사부터 질문 큐까지 한
                    작업으로 이어집니다.
                  </small>
                )}
              </div>
            </div>
          ) : null}
        </GlassPanel>

        <GlassPanel className="gps-panel">
          <PanelHeader eyebrow="3 · GPS" title="시간 동기화" />
          <div className="gps-illustration">
            <FileVideo2 size={34} />
            <span>
              {probeResult
                ? `${probeResult.file_name} · 첫 프레임 미생성`
                : "검증된 영상 없음"}
            </span>
          </div>
          <div className="notice">
            <CircleAlert size={19} />
            <div>
              <strong>GPS 자료 미연결</strong>
              <p>현재 입력에는 GPS 트랙을 등록하지 않았습니다. 오프셋과 경로 누락 구간을 아직 판정하지 않습니다.</p>
            </div>
          </div>
          <StatusDot tone="neutral">
            GPS 가져오기 기능 연결 대기
          </StatusDot>
        </GlassPanel>
      </div>

      {profileOpen ? (
        <div
          className="pipeline-profile-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (
              event.currentTarget === event.target &&
              profile !== null
            ) {
              setProfileOpen(false);
            }
          }}
        >
          <section
            className="pipeline-profile-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="pipeline-profile-title"
          >
            <header>
              <div>
                <p className="eyebrow">PROJECT DEFAULTS · 최초 1회</p>
                <h2 id="pipeline-profile-title">자동 점검 기본 설정</h2>
                <p>
                  이후에는 영상과 권리 확인만 받으며, 단계별 실행 버튼을
                  요구하지 않습니다.
                </p>
              </div>
              {profile !== null ? (
                <Button variant="ghost" onClick={() => setProfileOpen(false)}>
                  닫기
                </Button>
              ) : null}
            </header>

            <div className="pipeline-profile-grid">
              <fieldset>
                <legend>판정 방식</legend>
                <div className="profile-choice-grid profile-choice-grid--two">
                  <button
                    type="button"
                    className={reviewMode === "quick" ? "selected" : ""}
                    onClick={() => setReviewMode("quick")}
                  >
                    <strong>빠른 판정</strong>
                    <span>Y/N/U/E 즉시 저장 · 심각도 없음</span>
                  </button>
                  <button
                    type="button"
                    className={reviewMode === "precision" ? "selected" : ""}
                    onClick={() => setReviewMode("precision")}
                  >
                    <strong>정밀 판정</strong>
                    <span>Y 후 1/2/3 심각도 확정</span>
                  </button>
                </div>
              </fieldset>

              <fieldset>
                <legend>질문 정책</legend>
                <div className="profile-choice-grid profile-choice-grid--three">
                  {[
                    ["learning_assist", "학습 보조", "초기 전수 + 품질 표본"],
                    ["full_review", "전수 검토", "모든 후보 질문"],
                    ["quick_review", "빠른 검토", "불확실·긴급 중심"],
                  ].map(([value, label, description]) => (
                    <button
                      type="button"
                      key={value}
                      className={questionPolicy === value ? "selected" : ""}
                      onClick={() =>
                        setQuestionPolicy(
                          value as typeof questionPolicy,
                        )
                      }
                    >
                      <strong>{label}</strong>
                      <span>{description}</span>
                    </button>
                  ))}
                </div>
              </fieldset>

              <fieldset>
                <legend>기본 HWPX 양식</legend>
                <div className="profile-choice-grid profile-choice-grid--two">
                  {Object.entries(SAMPLE_TEMPLATES).map(([value, template]) => (
                    <button
                      type="button"
                      key={value}
                      className={templateId === value ? "selected" : ""}
                      onClick={() =>
                        setTemplateId(
                          value as keyof typeof SAMPLE_TEMPLATES,
                        )
                      }
                    >
                      <strong>{template.label}</strong>
                      <span>48쪽 · 표/네모/2열 사진 구조 보존</span>
                    </button>
                  ))}
                </div>
              </fieldset>

              <fieldset>
                <legend>보고서 Provider</legend>
                <div className="profile-choice-grid profile-choice-grid--three">
                  {[
                    ["codex", "Codex CLI"],
                    ["claude", "Claude Code"],
                    ["none", "사용 안 함"],
                  ].map(([value, label]) => (
                    <button
                      type="button"
                      key={value}
                      className={reportProvider === value ? "selected" : ""}
                      onClick={() =>
                        setReportProvider(value as typeof reportProvider)
                      }
                    >
                      <strong>{label}</strong>
                      <span>공식 CLI가 로그인 소유</span>
                    </button>
                  ))}
                </div>
              </fieldset>

              <fieldset>
                <legend>모델팩·후보 재분석</legend>
                <div className="profile-inline-summary">
                  <span>
                    <strong>공통 로컬 Model Pack</strong>
                    DINO/도로 검지·OCR·SAM2 capability 검증
                  </span>
                  <div className="profile-fps">
                    {[5, 8, 10].map((fps) => (
                      <button
                        type="button"
                        key={fps}
                        className={candidateRescanFps === fps ? "selected" : ""}
                        onClick={() =>
                          setCandidateRescanFps(fps as 5 | 8 | 10)
                        }
                      >
                        {fps}fps
                      </button>
                    ))}
                  </div>
                </div>
              </fieldset>

              <fieldset>
                <legend>결과 저장 위치</legend>
                <div className="profile-inline-summary">
                  <span>
                    <strong>프로젝트 로컬 보관소</strong>
                    원본과 분리된 프로젝트별 기본 위치
                  </span>
                  <Badge tone="success">경로 비공개</Badge>
                </div>
              </fieldset>
            </div>

            {profileError ? (
              <div className="notice notice--danger" role="alert">
                <CircleAlert size={19} />
                <div>
                  <strong>설정을 저장하지 못했습니다</strong>
                  <p>{profileError}</p>
                </div>
              </div>
            ) : null}

            <footer>
              <p>
                GPS가 없으면 경로만 만들지 않고, 검지·질문·CSV·보고서는
                계속합니다. 모델 미설치는 빈 성공과 구분됩니다.
              </p>
              <Button
                variant="primary"
                onClick={savePipelineProfile}
                disabled={profileState === "saving"}
              >
                {profileState === "saving"
                  ? "프로젝트 설정 저장 중…"
                  : "이 설정을 저장하고 영상 받기"}
              </Button>
            </footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}
