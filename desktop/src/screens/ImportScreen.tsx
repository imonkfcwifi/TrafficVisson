import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
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
import { ProviderVisualEvidenceReview } from "../components/ProviderVisualEvidenceReview";
import { isProviderVisualPrivacyReviewInterruption } from "../providerVisualEvidence";
import {
  parseFolderPipelineBatch,
  parseFolderPipelineBatchList,
  parseFolderPipelineBatchStart,
  type FolderPipelineBatch,
} from "../folderPipelineBatches";
import {
  parseProjectOutputLocationList,
  parseProjectOutputLocationRegistration,
  type ProjectOutputLocation,
} from "../projectOutputLocations";

const NETWORK_PATH =
  "\\\\192.168.0.234\\도로연구실\\9. 한국교통연구원\\01.2025년 민자고속도로 기술부문 실사 위탁용역\\2. 연구수행\\03. 3분기 실사점검\\새 폴더";
const SAMPLE_TEMPLATES = {
  "sample-q3": {
    label: "3분기 실사 양식",
    semanticMapId: "sample-q3-semantic-v1",
    sha256:
      "24ffb4bc62cd86a6c72d3a3829b8f1ddf1c824d7bf42452e7c495877a227bcb9",
  },
  "sample-q4": {
    label: "4분기 실사 양식",
    semanticMapId: "sample-q4-semantic-v1",
    sha256:
      "bbdd61a6ef192a957a5e9480b0c2eedbfe956dd184abd5d76f59474254941539",
  },
} as const;
const DEFAULT_DETECTOR_PACK_ID =
  "trafficvisson-project-dedicated-detector-pending";
const DEFAULT_OCR_PACK_ID =
  "trafficvisson-jaidedai-easyocr-ko-en";
const DEFAULT_SAM2_PACK_ID =
  "trafficvisson-facebook-sam2.1-hiera-tiny";
const MAX_FOLDER_PIPELINE_JOBS = 128;
const DEFAULT_REVIEW_MODE = "precision" as const;
const DEFAULT_QUESTION_POLICY = "learning_assist" as const;
const DEFAULT_LABEL_CONFIRMATION_AUTHORITY = "human_review" as const;
const DEFAULT_TEMPLATE_ID = "sample-q3" as const;
const DEFAULT_REPORT_PROVIDER = "codex" as const;
const DEFAULT_REPORT_EVIDENCE_MODE = "internal_original" as const;
const REPORT_INSPECTION_DATE_POLICY =
  "source_filename_yyyymmdd_or_block" as const;
const DEFAULT_CANDIDATE_RESCAN_FPS = 8 as const;
const PROFILE_FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

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
    modified_time_ns: string;
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

function formatNanosecondTimestamp(value: string) {
  if (!/^[0-9]{1,32}$/.test(value)) return "시간 정보 오류";
  try {
    const milliseconds = BigInt(value) / 1_000_000n;
    if (milliseconds > 8_640_000_000_000_000n) {
      return "시간 범위 오류";
    }
    const date = new Date(Number(milliseconds));
    return Number.isNaN(date.getTime())
      ? "시간 정보 오류"
      : date.toLocaleString("ko-KR");
  } catch {
    return "시간 정보 오류";
  }
}

function safeErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : "입력 검사를 완료하지 못했습니다.";
}

function isMissingPipelineProfileError(error: unknown) {
  return safeErrorMessage(error).includes("blocked_configuration_required");
}

function compactSourceTail(value: string) {
  const parts = value.split(/[\\/]+/).filter(Boolean);
  if (parts.length === 0) return value;
  return parts.at(-1) ?? value;
}

export function ImportScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const pipeline = usePipelineUi();
  const projectCatalog = useProjectCatalog();
  const currentPipeline: PipelineJob | null =
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
  const [folderBatch, setFolderBatch] =
    useState<FolderPipelineBatch | null>(null);
  const [batchTrackingEnabled, setBatchTrackingEnabled] = useState(true);
  const [pipelineStarting, setPipelineStarting] = useState(false);
  const [pipelineError, setPipelineError] = useState<string | null>(null);
  const [profile, setProfile] = useState<PipelineProfile | null>(null);
  const [profileState, setProfileState] = useState<
    "loading" | "ready" | "missing" | "saving" | "error"
  >("loading");
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileDialogProjectId, setProfileDialogProjectId] = useState<
    string | null
  >(null);
  const [profileLoadAttempt, setProfileLoadAttempt] = useState(0);
  const profileDialogRef = useRef<HTMLElement>(null);
  const profileReturnFocusRef = useRef<HTMLElement | null>(null);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [reviewMode, setReviewMode] =
    useState<"precision" | "quick">(DEFAULT_REVIEW_MODE);
  const [questionPolicy, setQuestionPolicy] = useState<
    "learning_assist" | "full_review" | "quick_review"
  >(DEFAULT_QUESTION_POLICY);
  const [
    labelConfirmationAuthority,
    setLabelConfirmationAuthority,
  ] = useState<
    "human_review" | "structured_llm_training_assist"
  >(DEFAULT_LABEL_CONFIRMATION_AUTHORITY);
  const [templateId, setTemplateId] =
    useState<keyof typeof SAMPLE_TEMPLATES>(DEFAULT_TEMPLATE_ID);
  const [reportProvider, setReportProvider] =
    useState<"codex" | "claude" | "none">(DEFAULT_REPORT_PROVIDER);
  const [reportTransmissionConsent, setReportTransmissionConsent] =
    useState(false);
  const [reportRouteName, setReportRouteName] = useState("");
  const [reportInspectorName, setReportInspectorName] = useState("");
  const [reportEvidenceMode, setReportEvidenceMode] = useState<
    "internal_original" | "shared_deidentified"
  >(DEFAULT_REPORT_EVIDENCE_MODE);
  const [candidateRescanFps, setCandidateRescanFps] =
    useState<5 | 8 | 10>(DEFAULT_CANDIDATE_RESCAN_FPS);
  const [outputLocations, setOutputLocations] = useState<
    ProjectOutputLocation[]
  >([]);
  const [outputLocationId, setOutputLocationId] =
    useState("project-default");
  const [outputLocationState, setOutputLocationState] = useState<
    "loading" | "ready" | "selecting" | "error"
  >("loading");
  const [outputLocationError, setOutputLocationError] =
    useState<string | null>(null);
  const activeProjectIdRef = useRef(projectCatalog.activeProjectId);
  activeProjectIdRef.current = projectCatalog.activeProjectId;
  const fileInput = useRef<HTMLInputElement>(null);
  const inspectionRun = useRef(0);

  useEffect(() => {
    if (!profileOpen) return;
    const dialog = profileDialogRef.current;
    const screen = dialog?.closest<HTMLElement>(".screen--import");
    if (!dialog || !screen) return;

    profileReturnFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : document.querySelector<HTMLElement>("#pipeline-profile-trigger");

    const background = Array.from(screen.children)
      .filter(
        (element): element is HTMLElement =>
          element instanceof HTMLElement &&
          !element.classList.contains("pipeline-profile-backdrop"),
      )
      .map((element) => ({
        element,
        hadInert: element.hasAttribute("inert"),
        ariaHidden: element.getAttribute("aria-hidden"),
      }));
    const previousBodyOverflow = document.body.style.overflow;

    for (const item of background) {
      item.element.setAttribute("inert", "");
      item.element.setAttribute("aria-hidden", "true");
    }
    document.body.style.overflow = "hidden";
    dialog.focus({ preventScroll: true });

    const focusFirstControl = window.requestAnimationFrame(() => {
      const first = dialog.querySelector<HTMLElement>(
        PROFILE_FOCUSABLE_SELECTOR,
      );
      (first ?? dialog).focus({ preventScroll: true });
    });
    const focusableControls = () =>
      Array.from(
        dialog.querySelectorAll<HTMLElement>(PROFILE_FOCUSABLE_SELECTOR),
      ).filter(
        (element) =>
          !element.hasAttribute("hidden") &&
          element.getAttribute("aria-hidden") !== "true",
      );
    const closeProfile = () => {
      if (profileState === "saving") return;
      if (profileState === "ready" && profile !== null) {
        setProfileOpen(false);
        return;
      }
      onNavigate("home");
    };
    const onDialogKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeProfile();
        return;
      }
      if (event.key !== "Tab") return;
      const controls = focusableControls();
      if (controls.length === 0) {
        event.preventDefault();
        dialog.focus({ preventScroll: true });
        return;
      }
      const first = controls[0];
      const last = controls[controls.length - 1];
      const activeElement = document.activeElement;
      const focusIsOutside = !dialog.contains(activeElement);
      if (event.shiftKey && (activeElement === first || focusIsOutside)) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (activeElement === last || focusIsOutside)
      ) {
        event.preventDefault();
        first.focus();
      }
    };
    const onDocumentFocus = (event: FocusEvent) => {
      if (dialog.contains(event.target as Node)) return;
      const first = focusableControls()[0];
      (first ?? dialog).focus({ preventScroll: true });
    };

    document.addEventListener("keydown", onDialogKeyDown, true);
    document.addEventListener("focusin", onDocumentFocus, true);
    return () => {
      window.cancelAnimationFrame(focusFirstControl);
      document.removeEventListener("keydown", onDialogKeyDown, true);
      document.removeEventListener("focusin", onDocumentFocus, true);
      for (const item of background) {
        if (!item.hadInert) item.element.removeAttribute("inert");
        if (item.ariaHidden === null) {
          item.element.removeAttribute("aria-hidden");
        } else {
          item.element.setAttribute("aria-hidden", item.ariaHidden);
        }
      }
      document.body.style.overflow = previousBodyOverflow;
      const returnTarget = profileReturnFocusRef.current;
      profileReturnFocusRef.current = null;
      window.requestAnimationFrame(() => {
        if (returnTarget?.isConnected) {
          returnTarget.focus({ preventScroll: true });
        }
      });
    };
  }, [onNavigate, profile, profileOpen, profileState]);

  useEffect(() => {
    setProfile(null);
    setProfileState("loading");
    setProfileOpen(false);
    setProfileDialogProjectId(null);
    setProfileError(null);
    setReviewMode(DEFAULT_REVIEW_MODE);
    setQuestionPolicy(DEFAULT_QUESTION_POLICY);
    setLabelConfirmationAuthority(
      DEFAULT_LABEL_CONFIRMATION_AUTHORITY,
    );
    setTemplateId(DEFAULT_TEMPLATE_ID);
    setReportProvider(DEFAULT_REPORT_PROVIDER);
    setReportTransmissionConsent(false);
    setReportRouteName("");
    setReportInspectorName("");
    setReportEvidenceMode(DEFAULT_REPORT_EVIDENCE_MODE);
    setCandidateRescanFps(DEFAULT_CANDIDATE_RESCAN_FPS);
    setOutputLocations([]);
    setOutputLocationId("project-default");
    setOutputLocationState("loading");
    setOutputLocationError(null);
    setBatchSnapshotId(null);
    setBatchJobs([]);
    setFolderBatch(null);
    setBatchTrackingEnabled(true);
  }, [projectCatalog.activeProjectId]);

  useEffect(() => {
    const projectId = projectCatalog.activeProjectId;
    const service = window.trafficSuperVision?.pipeline;
    if (!projectId) return;
    if (!service) {
      if (!window.trafficSuperVision) return;
      setProfile(null);
      setProfileState("error");
      setProfileError(
        "프로젝트 설정 연결을 사용할 수 없습니다. 앱을 다시 시작한 뒤 다시 시도하세요.",
      );
      setProfileDialogProjectId(projectId);
      setProfileOpen(true);
      return;
    }
    let active = true;
    setProfileState("loading");
    setProfileError(null);
    void service
      .getProfile({ projectId })
      .then(parsePipelineProfile)
      .then((value) => {
        if (!active) return;
        if (value.projectId !== projectId) {
          throw new Error(
            "프로젝트 설정 응답이 현재 프로젝트와 일치하지 않습니다.",
          );
        }
        setProfile(value);
        setReviewMode(value.reviewMode);
        setQuestionPolicy(value.questionPolicy);
        setLabelConfirmationAuthority(
          value.labelConfirmationAuthority,
        );
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
        setReportTransmissionConsent(value.reportTransmissionConsent);
        setReportRouteName(
          value.reportRouteName === "unconfigured"
            ? ""
            : value.reportRouteName,
        );
        setReportInspectorName(
          value.reportInspectorName === "unconfigured"
            ? ""
            : value.reportInspectorName,
        );
        setReportEvidenceMode(value.reportEvidenceMode);
        setOutputLocationId(value.outputLocationId);
        if ([5, 8, 10].includes(value.candidateRescanFps)) {
          setCandidateRescanFps(value.candidateRescanFps as 5 | 8 | 10);
        }
        setProfileState("ready");
      })
      .catch((error) => {
        if (!active) return;
        setProfile(null);
        setProfileDialogProjectId(projectId);
        setProfileOpen(true);
        if (isMissingPipelineProfileError(error)) {
          setProfileState("missing");
          setProfileError(null);
          return;
        }
        setProfileState("error");
        setProfileError(
          `프로젝트 설정을 읽지 못했습니다. 저장하지 않고 다시 시도하세요. ${safeErrorMessage(
            error,
          )}`,
        );
      });
    return () => {
      active = false;
    };
  }, [profileLoadAttempt, projectCatalog.activeProjectId]);

  useEffect(() => {
    const projectId = projectCatalog.activeProjectId;
    const service =
      window.trafficSuperVision?.projectOutputLocations;
    if (!projectId) return;
    if (!service) {
      if (!window.trafficSuperVision) return;
      setOutputLocationState("error");
      setOutputLocationError(
        "결과 저장 위치 연결을 사용할 수 없습니다.",
      );
      return;
    }
    let active = true;
    setOutputLocationState("loading");
    setOutputLocationError(null);
    void service
      .list({ projectId })
      .then(parseProjectOutputLocationList)
      .then((locations) => {
        if (!active) return;
        setOutputLocations(locations);
        setOutputLocationId((current) =>
          locations.some(
            (location) =>
              location.outputLocationId === current,
          )
            ? current
            : "project-default",
        );
        setOutputLocationState("ready");
      })
      .catch((error) => {
        if (!active) return;
        setOutputLocationState("error");
        setOutputLocationError(safeErrorMessage(error));
      });
    return () => {
      active = false;
    };
  }, [projectCatalog.activeProjectId]);

  useEffect(() => {
    const projectId = projectCatalog.activeProjectId;
    const service = window.trafficSuperVision?.pipeline;
    if (!projectId || !service || !batchTrackingEnabled) return;
    let active = true;
    let batchId: string | null = null;
    let fetching = false;
    const applyBatch = (batch: FolderPipelineBatch) => {
      if (!active || batch.projectId !== projectId) return;
      batchId = batch.batchId;
      setFolderBatch(batch);
      setBatchSnapshotId(batch.snapshotId);
      setCandidates(
        batch.files.map((file) => ({
          name: file.fileName,
          size: formatBytes(file.sizeBytes),
          changed: batch.recoveredAfterRestart
            ? "재시작 뒤 복구"
            : "안정성 검증 원장",
          state:
            file.lastErrorCode ??
            file.pipelineStatus ??
            file.intakeState,
        })),
      );
      setValidatedFiles(
        batch.files
          .filter(
            (file) =>
              file.intakeState === "enqueued" ||
              file.pipelineJobId !== null,
          )
          .map((file) => file.fileName),
      );
      setSelectedFile((current) =>
        batch.files.some((file) => file.fileName === current)
          ? current
          : batch.files[0]?.fileName ?? "",
      );
    };
    const refresh = async () => {
      if (!active || fetching) return;
      fetching = true;
      try {
        if (batchId) {
          applyBatch(
            parseFolderPipelineBatch(
              await service.getFolderBatch({
                projectId,
                batchId,
              }),
            ),
          );
        } else {
          const batches = parseFolderPipelineBatchList(
            await service.listFolderBatches({ projectId }),
          );
          if (batches[0]) applyBatch(batches[0]);
        }
      } catch {
        // A selected PipelineJob remains usable while the aggregate ledger
        // is temporarily unavailable.
      } finally {
        fetching = false;
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 1_200);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [batchTrackingEnabled, projectCatalog.activeProjectId]);

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

  function openProfileDialog() {
    const projectId = projectCatalog.activeProjectId;
    if (!projectId) return;
    setProfileDialogProjectId(projectId);
    setProfileOpen(true);
  }

  function retryProfileLoad() {
    const projectId = projectCatalog.activeProjectId;
    if (!projectId) return;
    setProfile(null);
    setProfileState("loading");
    setProfileError(null);
    setProfileDialogProjectId(projectId);
    setProfileOpen(true);
    setProfileLoadAttempt((attempt) => attempt + 1);
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
      setFolderBatch(null);
      setBatchTrackingEnabled(false);
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
    if (
      profileState !== "ready" ||
      !profile ||
      profile.projectId !== projectId
    ) {
      openProfileDialog();
      setPipelineError(
        profileState === "error"
          ? "프로젝트 설정을 읽지 못해 분석을 시작하지 않았습니다. 설정을 다시 불러오세요."
          : "영상 분석 전에 이 프로젝트의 최초 설정을 한 번 저장해야 합니다.",
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
      if (sourceType === "network") {
        const started = parseFolderPipelineBatchStart(
          await window.trafficSuperVision.pipeline.startFolderBatch({
            projectId,
            snapshotId,
            rightsConfirmed: true,
            idempotencyKey: `folder-start-${snapshotId}-p${profile.revision}`,
          }),
        );
        setFolderBatch(started.batch);
        setBatchTrackingEnabled(true);
        setBatchSnapshotId(started.batch.snapshotId);
        setBatchJobs([]);
        setInspectionMessage(
          started.idempotentReplay
            ? `${started.batch.counts.total}개 영상의 기존 폴더 배치를 복구했습니다.`
            : `${started.batch.counts.total}개 영상을 서버 영속 배치에 등록했습니다.`,
        );
        setCandidates(
          started.batch.files.map((file) => ({
            name: file.fileName,
            size: formatBytes(file.sizeBytes),
            changed: "안정성 검증 원장",
            state: file.lastErrorCode ?? file.intakeState,
          })),
        );
        return;
      }
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

  async function chooseProjectOutputLocation() {
    const api = window.trafficSuperVision;
    const projectId = projectCatalog.activeProjectId;
    if (
      !api?.projectOutputLocations ||
      !api.dialogs.selectProjectOutputDirectory ||
      !projectId
    ) {
      setOutputLocationState("error");
      setOutputLocationError(
        "설치형 앱의 결과 폴더 선택 기능을 사용할 수 없습니다.",
      );
      return;
    }
    setOutputLocationState("selecting");
    setOutputLocationError(null);
    try {
      const selection =
        await api.dialogs.selectProjectOutputDirectory();
      if (!selection.selectionId || !selection.directory) {
        setOutputLocationState("ready");
        return;
      }
      const location = parseProjectOutputLocationRegistration(
        await api.projectOutputLocations.register({
          projectId,
          selectionId: selection.selectionId,
        }),
      );
      setOutputLocations((current) => [
        ...current.filter(
          (item) =>
            item.outputLocationId !== location.outputLocationId,
        ),
        location,
      ]);
      setOutputLocationId(location.outputLocationId);
      setOutputLocationState("ready");
    } catch (error) {
      setOutputLocationState("error");
      setOutputLocationError(safeErrorMessage(error));
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
    const configurationStateIsValid =
      (profileState === "missing" && profile === null) ||
      (profileState === "ready" && profile?.projectId === projectId);
    if (
      !configurationStateIsValid ||
      activeProjectIdRef.current !== projectId
    ) {
      setProfile(null);
      setProfileState("error");
      setProfileError(
        "현재 프로젝트 설정을 확인하지 못해 저장하지 않았습니다. 다시 불러온 뒤 시도하세요.",
      );
      setProfileDialogProjectId(projectId);
      setProfileOpen(true);
      return;
    }
    const template = SAMPLE_TEMPLATES[templateId];
    const selectedOutputLocation = outputLocations.find(
      (location) =>
        location.outputLocationId === outputLocationId,
    );
    if (
      outputLocationState !== "ready" ||
      !selectedOutputLocation ||
      selectedOutputLocation.state !== "ready"
    ) {
      setProfileError(
        "사용 가능한 결과 저장 위치를 확인한 뒤 저장해 주세요.",
      );
      return;
    }
    if (
      labelConfirmationAuthority ===
        "structured_llm_training_assist" &&
      reportProvider === "none"
    ) {
      setProfileError(
        "AI 학습 라벨 보조를 사용하려면 Codex CLI 또는 Claude Code를 AI 판단·보고서 Provider로 선택해 주세요.",
      );
      return;
    }
    if (
      labelConfirmationAuthority ===
        "structured_llm_training_assist" &&
      !reportTransmissionConsent
    ) {
      setProfileError(
        "AI 학습 라벨 보조를 사용하려면 비식별 후보·유사 사례 전송 범위를 확인하고 명시적으로 동의해 주세요.",
      );
      return;
    }
    if (reportProvider !== "none" && !reportTransmissionConsent) {
      setProfileError(
        "AI 판단·보고서 Provider를 사용하려면 전송 범위를 확인하고 명시적으로 동의해 주세요.",
      );
      return;
    }
    setProfileState("saving");
    setProfileError(null);
    try {
      const saved = parsePipelineProfile(
        await window.trafficSuperVision.pipeline.configureProfile({
          projectId,
          questionPolicy,
          reviewMode,
          labelConfirmationAuthority,
          defaultTemplateId: templateId,
          defaultTemplateSha256: template.sha256,
          detectorPackId: DEFAULT_DETECTOR_PACK_ID,
          ocrPackId: DEFAULT_OCR_PACK_ID,
          sam2PackId: DEFAULT_SAM2_PACK_ID,
          reportProvider,
          reportTransmissionConsent,
          templateSemanticMapId: template.semanticMapId,
          reportRouteName: reportRouteName.trim() || "unconfigured",
          reportInspectorName:
            reportInspectorName.trim() || "unconfigured",
          reportEvidenceMode,
          reportInspectionDatePolicy: REPORT_INSPECTION_DATE_POLICY,
          outputLocationId,
          candidateRescanFps,
        }),
      );
      if (saved.projectId !== projectId) {
        throw new Error(
          "저장된 프로젝트 설정이 현재 프로젝트와 일치하지 않습니다.",
        );
      }
      if (activeProjectIdRef.current !== projectId) return;
      setProfile(saved);
      setProfileState("ready");
      setProfileOpen(false);
      setProfileDialogProjectId(null);
      setPipelineError(null);
    } catch (error) {
      if (activeProjectIdRef.current !== projectId) return;
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
              changed: formatNanosecondTimestamp(
                file.modified_time_ns,
              ),
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
          current.map((file) => ({
            ...file,
            state: "A/B/C 안정 · 서버 검증 대기",
          })),
        );
        setValidatedFiles(names);
        setInspectionProgress(100);
        setInspectionMessage(
          `폴더 영상 ${names.length}개 안정성 확인 · 서버 영속 배치가 파일별 스트림 검증과 저장공간 사전점검을 이어서 수행합니다.`,
        );
        setInspection("ready");
        if (autoStartPipeline) {
          await startPersistentPipelines(
            projectId,
            stability.stable_snapshot_id,
            names,
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
            changed: formatNanosecondTimestamp(
              selected.modified_time_ns,
            ),
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

  async function retryFolderBatch() {
    const projectId = await resolveProjectId();
    if (
      !window.trafficSuperVision?.pipeline ||
      !folderBatch ||
      folderBatch.projectId !== projectId
    ) {
      return;
    }
    setPipelineError(null);
    try {
      const next = parseFolderPipelineBatch(
        await window.trafficSuperVision.pipeline.retryFolderBatch({
          projectId,
          batchId: folderBatch.batchId,
          userConfirmed: true,
        }),
      );
      setFolderBatch(next);
      setInspectionMessage(
        `폴더 배치 재시도 ${next.retryCount}회 · 실패 항목과 중단 작업을 다시 확인합니다.`,
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

  const batchRunningCount =
    folderBatch?.counts.queuedOrRunning ??
    batchJobs.filter((job) =>
      ["queued", "running", "cancelling"].includes(job.status),
    ).length;
  const batchReviewCount =
    folderBatch?.counts.awaitingReview ??
    batchJobs.filter((job) => job.status === "awaiting_review").length;
  const batchCompletedCount =
    folderBatch?.counts.completed ??
    batchJobs.filter((job) => job.status === "completed").length;
  const batchProblemCount =
    (folderBatch
      ? folderBatch.counts.validationFailed +
        folderBatch.counts.enqueueFailed +
        folderBatch.counts.storageBlocked +
        folderBatch.counts.pipelineFailed
      : batchJobs.filter((job) =>
          ["blocked", "failed", "cancelled"].includes(job.status),
        ).length);
  const batchJobByFile = new Map(
    batchJobs.map((job) => [job.fileName, job] as const),
  );
  const folderBatchFileByName = new Map(
    (folderBatch?.files ?? []).map((file) => [file.fileName, file] as const),
  );
  const profileBelongsToActiveProject =
    profile !== null &&
    profile.projectId === projectCatalog.activeProjectId;
  const profileControlsDisabled =
    !projectCatalog.activeProjectId ||
    profileState === "loading" ||
    profileState === "saving" ||
    profileState === "error" ||
    (profile !== null && !profileBelongsToActiveProject);
  const profileBadgeTone =
    profileState === "ready" && profileBelongsToActiveProject
      ? "success"
      : profileState === "error"
        ? "danger"
        : profileState === "loading" || profileState === "saving"
          ? "neutral"
          : "warning";
  const profileBadgeLabel =
    profileState === "ready" && profileBelongsToActiveProject
      ? "1회 설정 완료"
      : profileState === "error"
        ? "설정 확인 오류"
        : profileState === "loading"
          ? "설정 확인 중"
          : profileState === "saving"
            ? "설정 저장 중"
            : "1회 설정 필요";
  const profileDialogVisible =
    profileOpen &&
    profileDialogProjectId !== null &&
    profileDialogProjectId === projectCatalog.activeProjectId;
  const profileDialogMode =
    profileState === "loading"
      ? "loading"
      : profileState === "error"
        ? "error"
        : profile === null
          ? "initial"
          : "edit";
  const selectedOutputLocation =
    outputLocations.find(
      (location) =>
        location.outputLocationId === outputLocationId,
    ) ?? null;
  const providerPrivacyReviewInterrupted =
    isProviderVisualPrivacyReviewInterruption(currentPipeline);

  if (providerPrivacyReviewInterrupted) {
    return (
      <div className="screen screen--import screen--privacy-review">
        <header className="screen-header">
          <div>
            <p className="eyebrow">영상 입력 후 자동 분석</p>
            <h1>비식별 이미지 확인</h1>
            <p>
              분석 버튼을 다시 누르지 마세요. 이 확인이 끝나면 같은
              PipelineJob이 자동으로 이어집니다.
            </p>
          </div>
          <Badge tone="warning">사용자 확인 필요</Badge>
        </header>
        <ProviderVisualEvidenceReview job={currentPipeline} />
      </div>
    );
  }

  return (
    <div className="screen screen--import">
      <header className="screen-header">
        <div>
          <p className="eyebrow">안전한 영상 입력</p>
          <h1>영상 가져오기</h1>
          <p>입력 위치를 먼저 검사하고 권리·무결성·업로드 완료 여부를 확인합니다.</p>
        </div>
        <div className="header-actions">
          <Badge tone={profileBadgeTone}>{profileBadgeLabel}</Badge>
          <Button
            id="pipeline-profile-trigger"
            variant="ghost"
            onClick={openProfileDialog}
            disabled={profileControlsDisabled}
          >
            <Settings2 size={16} />
            프로젝트 설정
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

              {folderBatch || batchJobs.length ? (
                <div
                  className={`notice ${
                    folderBatch?.status === "blocked_storage"
                      ? "notice--danger"
                      : "notice--success"
                  }`}
                  aria-live="polite"
                >
                  {folderBatch?.status === "blocked_storage" ? (
                    <CircleAlert size={20} />
                  ) : (
                    <Check size={20} />
                  )}
                  <div>
                    <strong>
                      {folderBatch?.status === "blocked_storage"
                        ? "결과 저장공간 사전점검에서 분석 시작을 차단했습니다."
                        : `폴더 영상 ${folderBatch?.counts.total ?? batchJobs.length}개가 자동 분석 대기열에 있습니다.`}
                    </strong>
                    <p>
                      처리 중 {batchRunningCount} · 질문 대기 {batchReviewCount} ·
                      완료 {batchCompletedCount} · 확인 필요 {batchProblemCount}
                      {folderBatch?.counts.pending
                        ? ` · 검증/등록 대기 ${folderBatch.counts.pending}`
                        : ""}
                    </p>
                    {folderBatch ? (
                      <p>
                        {folderBatch.recoveredAfterRestart
                          ? `PC 재시작 복구 ${folderBatch.resumeCount}회 · `
                          : ""}
                        멱등 원장 {folderBatch.inputFingerprint.slice(0, 12)}…
                      </p>
                    ) : null}
                    {folderBatch?.outputPreflight.state === "ready" ||
                    folderBatch?.outputPreflight.state === "blocked" ? (
                      <p>
                        예상 {folderBatch.outputPreflight.denseSamplingFps}fps{" "}
                        {folderBatch.outputPreflight.denseFrameCount.toLocaleString("ko-KR")}장 ·
                        결과 {formatBytes(folderBatch.outputPreflight.estimatedArtifactBytes)} ·
                        필요 여유 {formatBytes(folderBatch.outputPreflight.requiredFreeBytes)} ·
                        현재 여유{" "}
                        {folderBatch.outputPreflight.availableFreeBytes === null
                          ? "확인 불가"
                          : formatBytes(
                              folderBatch.outputPreflight.availableFreeBytes,
                            )}
                        {" · "}프레임당{" "}
                        {formatBytes(
                          folderBatch.outputPreflight.estimatedJpegBytesPerFrame,
                        )}{" "}
                        + 파생{" "}
                        {Math.round(
                          folderBatch.outputPreflight.derivedOutputOverheadRatio *
                            100,
                        )}%
                      </p>
                    ) : null}
                    {folderBatch &&
                    ["blocked_storage", "partial_failed", "failed"].includes(
                      folderBatch.status,
                    ) ? (
                      <Button variant="positive" onClick={retryFolderBatch}>
                        실패 항목 재시도
                      </Button>
                    ) : null}
                  </div>
                </div>
              ) : null}

              {candidates.length ? (
                <div className="file-candidates" aria-label={`발견된 영상 ${candidates.length}개`}>
                  {candidates.map((file) => {
                    const pipelineJob = batchJobByFile.get(file.name);
                    const batchFile = folderBatchFileByName.get(file.name);
                    return (
                  <button
                    type="button"
                    key={file.name}
                    className={selectedFile === file.name ? "selected" : ""}
                    onClick={() => {
                      if (file.name !== selectedFile) {
                        setSelectedFile(file.name);
                      }
                      if (pipelineJob) publishPipelineJob(pipelineJob);
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
                            : batchFile?.retryAvailable
                              ? "danger"
                              : pipelineJob
                                ? "accent"
                                : batchFile?.intakeState === "enqueued"
                                  ? "accent"
                                  : file.state.includes("실패")
                                    ? "danger"
                                    : "warning"
                      }
                    >
                      {pipelineJob
                        ? pipelineStatusLabel(pipelineJob)
                        : batchFile?.lastErrorCode ??
                          batchFile?.pipelineStatus ??
                          batchFile?.intakeState ??
                          file.state}
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

      {profileDialogVisible ? (
        <div
          className="pipeline-profile-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (
              event.currentTarget === event.target &&
              profileDialogMode === "edit" &&
              profileState !== "saving"
            ) {
              setProfileOpen(false);
            }
          }}
        >
          <section
            ref={profileDialogRef}
            className="pipeline-profile-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="pipeline-profile-title"
            aria-describedby="pipeline-profile-description"
            tabIndex={-1}
          >
            <header>
              <div>
                <p className="eyebrow">
                  {profileDialogMode === "initial"
                    ? "새 프로젝트 · 1 / 2"
                    : profileDialogMode === "edit"
                      ? "프로젝트 설정"
                      : profileDialogMode === "loading"
                        ? "프로젝트 설정 확인"
                        : "프로젝트 설정 오류"}
                </p>
                <h2 id="pipeline-profile-title">
                  {profileDialogMode === "initial"
                    ? "새 프로젝트 준비"
                    : profileDialogMode === "edit"
                      ? "프로젝트 설정"
                      : profileDialogMode === "loading"
                        ? "설정을 확인하고 있습니다"
                        : "설정을 불러오지 못했습니다"}
                </h2>
                <p
                  id="pipeline-profile-description"
                  className="pipeline-profile-description"
                >
                  {profileDialogMode === "initial"
                    ? "이 프로젝트에서 한 번만 정합니다. 저장하면 영상 선택 화면으로 이어집니다."
                    : profileDialogMode === "edit"
                      ? "새로 넣는 영상부터 적용할 프로젝트 기본값을 변경합니다."
                      : profileDialogMode === "loading"
                        ? "현재 프로젝트와 일치하는 저장 설정을 읽는 중입니다."
                        : "기존 설정을 확인하지 못했으므로 새 값으로 저장하지 않습니다."}
                </p>
                {profileDialogMode === "initial" ? (
                  <div className="pipeline-profile-next" aria-label="다음 단계">
                    <strong>다음 단계 · 2 / 2</strong>
                    <span>영상 선택과 권리 확인 → 질문 준비까지 자동 진행</span>
                  </div>
                ) : null}
              </div>
              {profileDialogMode === "edit" ? (
                <Button
                  variant="ghost"
                  onClick={() => setProfileOpen(false)}
                  disabled={profileState === "saving"}
                >
                  닫기
                </Button>
              ) : (
                <Button variant="ghost" onClick={() => onNavigate("home")}>
                  <ArrowLeft size={16} aria-hidden="true" />
                  프로젝트 홈
                </Button>
              )}
            </header>

            {profileDialogMode === "initial" ||
            profileDialogMode === "edit" ? (
              <>
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
                <legend>학습 라벨 확정 주체</legend>
                <div className="profile-choice-grid profile-choice-grid--two">
                  <button
                    type="button"
                    className={
                      labelConfirmationAuthority === "human_review"
                        ? "selected"
                        : ""
                    }
                    onClick={() =>
                      setLabelConfirmationAuthority("human_review")
                    }
                  >
                    <strong>사용자가 모두 확인</strong>
                    <span>모든 후보를 질문으로 남기고 직접 확정</span>
                  </button>
                  <button
                    type="button"
                    className={
                      labelConfirmationAuthority ===
                      "structured_llm_training_assist"
                        ? "selected"
                        : ""
                    }
                    onClick={() =>
                      setLabelConfirmationAuthority(
                        "structured_llm_training_assist",
                      )
                    }
                  >
                    <strong>AI 학습 라벨 보조</strong>
                    <span>
                      안전한 비긴급 제안만 학습 전용으로 자동 저장 ·
                      낙하물·긴급은 반드시 질문 · Provider 선택과 전송 동의 필요
                    </span>
                  </button>
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
                <legend>AI 판단·보고서 Provider</legend>
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
                      onClick={() => {
                        const next = value as typeof reportProvider;
                        setReportProvider(next);
                        if (next === "none") {
                          setReportTransmissionConsent(false);
                        }
                      }}
                    >
                      <strong>{label}</strong>
                      <span>공식 CLI가 로그인 소유</span>
                    </button>
                  ))}
                </div>
                <label className="rights-check profile-consent-check">
                  <input
                    type="checkbox"
                    checked={reportTransmissionConsent}
                    disabled={reportProvider === "none"}
                    onChange={(event) =>
                      setReportTransmissionConsent(event.target.checked)
                    }
                  />
                  <span className="check-visual" aria-hidden="true">
                    <Check size={14} />
                  </span>
                  <span>
                    <strong>
                      승인한 범위만 선택한 Provider에 전송하는 데 동의합니다.
                    </strong>
                    <small>
                      개인정보 검토로 확인된 비식별 후보·유사 사례 crop은
                      Codex에만 전송 가능 · Claude는 이미지 없이 메타데이터만
                      사용 · 원본 영상·원본 이미지·인증 파일은 전송하지 않음
                    </small>
                  </span>
                </label>
              </fieldset>

              <fieldset className="profile-report-context">
                <legend>현재 영상 보고서 사실 정보</legend>
                <div className="profile-report-context-grid">
                  <label className="profile-report-text-field">
                    <span>노선명</span>
                    <input
                      type="text"
                      value={reportRouteName}
                      maxLength={300}
                      placeholder="예: 수도권제1순환고속도로"
                      onChange={(event) =>
                        setReportRouteName(event.target.value)
                      }
                    />
                  </label>
                  <label className="profile-report-text-field">
                    <span>점검자명</span>
                    <input
                      type="text"
                      value={reportInspectorName}
                      maxLength={120}
                      placeholder="예: 홍길동"
                      onChange={(event) =>
                        setReportInspectorName(event.target.value)
                      }
                    />
                  </label>
                  <div
                    className="profile-report-evidence"
                    role="group"
                    aria-label="보고서 증거 사용 방식"
                  >
                    <span>증거 사용</span>
                    <div className="profile-fps">
                      {[
                        ["internal_original", "내부 원본"],
                        ["shared_deidentified", "공유 비식별"],
                      ].map(([value, label]) => (
                        <button
                          type="button"
                          key={value}
                          className={
                            reportEvidenceMode === value
                              ? "selected"
                              : ""
                          }
                          onClick={() =>
                            setReportEvidenceMode(
                              value as typeof reportEvidenceMode,
                            )
                          }
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
                <p className="profile-report-context-note">
                  점검일은 영상 파일명의 YYYYMMDD만 검증해 사용합니다. 없으면
                  추정하지 않고 보고서 준비를 차단합니다. 공유 비식별은 별도
                  검증 증거가 없으면 적용하지 않습니다.
                </p>
              </fieldset>

              <fieldset>
                <legend>모델팩·후보 재분석</legend>
                <div className="profile-inline-summary">
                  <span>
                    <strong>로컬 후보 분석 Model Pack</strong>
                    DINO·OCR·SAM2로 시작 · 전용 도로 검지기는 학습·승격 후 사용
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
                    <strong>
                      {selectedOutputLocation?.displayName ??
                        "저장 위치 확인 중"}
                    </strong>
                    {selectedOutputLocation
                      ? `${selectedOutputLocation.volumeLabel} · ${
                          selectedOutputLocation.availableFreeBytes ===
                          null
                            ? "용량 확인 필요"
                            : `${formatBytes(
                                selectedOutputLocation.availableFreeBytes,
                              )} 사용 가능`
                        }`
                      : "브리지에서 프로젝트별 저장소를 확인합니다."}
                  </span>
                  <div className="profile-fps">
                    <Badge
                      tone={
                        selectedOutputLocation?.state === "ready"
                          ? "success"
                          : "warning"
                      }
                    >
                      경로 비공개
                    </Badge>
                    <Button
                      variant="ghost"
                      onClick={chooseProjectOutputLocation}
                      disabled={
                        outputLocationState === "loading" ||
                        outputLocationState === "selecting" ||
                        profileState === "saving"
                      }
                    >
                      <FolderOpen size={15} aria-hidden="true" />
                      {outputLocationState === "selecting"
                        ? "확인 중…"
                        : "결과 폴더 변경"}
                    </Button>
                  </div>
                </div>
                {outputLocationError ? (
                  <p className="profile-report-context-note">
                    {outputLocationError}
                  </p>
                ) : null}
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
                  <div className="pipeline-profile-actions">
                    {profile ? (
                      <Button
                        variant="ghost"
                        onClick={() => setProfileOpen(false)}
                        disabled={profileState === "saving"}
                      >
                        취소
                      </Button>
                    ) : null}
                    <Button
                      variant="primary"
                      onClick={savePipelineProfile}
                      disabled={
                        profileState === "saving" ||
                        outputLocationState !== "ready" ||
                        selectedOutputLocation?.state !== "ready"
                      }
                    >
                      {profileState === "saving"
                        ? "프로젝트 설정 저장 중…"
                        : profile === null
                          ? "기본 설정 저장 → 영상 선택"
                          : "변경사항 저장"}
                    </Button>
                  </div>
                </footer>
              </>
            ) : (
              <>
                <div
                  className={`notice ${
                    profileDialogMode === "error" ? "notice--danger" : ""
                  }`}
                  role={profileDialogMode === "error" ? "alert" : "status"}
                >
                  <CircleAlert size={19} />
                  <div>
                    <strong>
                      {profileDialogMode === "error"
                        ? "설정을 읽지 못했습니다"
                        : "프로젝트 설정 확인 중"}
                    </strong>
                    <p>
                      {profileDialogMode === "error"
                        ? profileError ??
                          "프로젝트 설정을 확인하지 못했습니다."
                        : "현재 프로젝트의 저장 설정과 무결성을 확인하고 있습니다."}
                    </p>
                  </div>
                </div>
                <footer>
                  <p>
                    읽기 실패를 새 프로젝트로 간주하지 않으며, 확인 전에는
                    설정을 저장하거나 분석을 시작하지 않습니다.
                  </p>
                  <div className="pipeline-profile-actions">
                    <Button variant="ghost" onClick={() => onNavigate("home")}>
                      <ArrowLeft size={16} aria-hidden="true" />
                      프로젝트 홈
                    </Button>
                    {profileDialogMode === "error" ? (
                      <Button variant="primary" onClick={retryProfileLoad}>
                        다시 시도
                      </Button>
                    ) : (
                      <Button variant="primary" disabled>
                        설정 확인 중…
                      </Button>
                    )}
                  </div>
                </footer>
              </>
            )}
          </section>
        </div>
      ) : null}
    </div>
  );
}
