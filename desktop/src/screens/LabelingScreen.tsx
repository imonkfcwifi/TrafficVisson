import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  BookOpenCheck,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Crosshair,
  LoaderCircle,
  MousePointer2,
  Play,
  RotateCcw,
  ShieldCheck,
  SkipForward,
  Sparkles,
  ImageOff,
  X,
  ZoomIn,
} from "lucide-react";
import type { ScreenId } from "../demo";
import {
  loadSelectedCandidateId,
  saveSelectedCandidateId,
} from "../candidateSelection";
import {
  parseAnalysisJobList,
  toRestoredRuntime,
  type AnalysisJobResponse,
} from "../analysisJobs";
import { buildCandidateProposal } from "../candidateProposal";
import {
  apiIssueTypeFromIncident,
  REVIEW_ISSUE_TYPE_OPTIONS,
} from "../issueTypes";
import {
  buildTemporalIncidentReviewRequest,
  buildTemporalQuestionOrder,
  parseTemporalIncidentReviewResponse,
  type TemporalQuestionGroup,
} from "../reviewComputations";
import {
  AUTO_LABELING_ISSUE_OPTIONS,
  buildAutoLabelingCompileRequest,
  buildAutoLabelingRunRequest,
  parseAutoLabelingCompileResponse,
  parseAutoLabelingRunResponse,
  requiresMandatoryReview,
  type AutoLabelingCompileResult,
  type AutoLabelingIssueSelection,
  type AutoLabelingMode,
  type AutoLabelingRunResult,
  type AutoLabelingScope,
} from "../autoLabeling";
import { realTunnelEvidenceImage } from "../demo";
import {
  buildCandidateEvidenceUrl,
  buildWeakCaseImageUrl,
} from "../evidence";
import {
  experimentalProposalOverlaysFromQuestionList,
  type ExperimentalProposalOverlay,
} from "../experimentalProposals";
import {
  loadReviewHistory,
  reviewRecordsFromQuestionList,
  reviewStorageKey,
  saveReviewHistory,
  type ReviewAnswer,
  type ReviewRecord,
} from "../reviewRecords";
import {
  loadReviewModePreference,
  reviewModeForQuestionPolicy,
  saveReviewModePreference,
  type ReviewModePreference,
} from "../reviewModePreference";
import {
  decideLabelingSubmission,
  severityFromShortcut,
} from "../labelingDecision";
import {
  candidateQuestionIds,
  parseMaskRefinementCapability,
  parseMaskRefinementProposal,
  type MaskRefinementCapability,
  type MaskRefinementIdentity,
  type MaskRefinementProposal,
} from "../maskRefinement";
import {
  parseWeakCaseSearchResponse,
  type WeakCaseReference,
} from "../weakCases";
import {
  restoreCompletedAnalysis,
  useRuntimeState,
  type AnalysisSummary,
} from "../runtimeState";
import { useProjectCatalog } from "../projectCatalog";
import {
  parsePipelineJob,
  publishPipelineJob,
  usePipelineUi,
} from "../pipelineJobs";
import { buildPipelineReviewCompletion } from "../pipelineReviewCompletion";
import {
  Badge,
  Button,
  GlassPanel,
  IconButton,
  PanelHeader,
  ProgressBar,
} from "../components/ui";

type Answer = ReviewAnswer;
type ReviewMode = ReviewModePreference;
type UserSeverity = "낮음" | "보통" | "높음";

const REVIEW_MODES: ReadonlyArray<{
  id: ReviewMode;
  label: string;
  description: string;
}> = [
  {
    id: "precision",
    label: "정밀 판정",
    description: "예 선택 후 1·2·3으로 심각도를 확정",
  },
  {
    id: "quick",
    label: "빠른 판정",
    description: "심각도 없이 즉시 저장하고 다음 후보로 이동",
  },
];

const USER_SEVERITIES: ReadonlyArray<{
  value: UserSeverity;
  key: "1" | "2" | "3";
  description: string;
}> = [
  { value: "낮음", key: "1", description: "관찰·일상 보수" },
  { value: "보통", key: "2", description: "계획 보수 필요" },
  { value: "높음", key: "3", description: "우선 현장 확인" },
];

type ReviewQuestion = {
  id: string;
  prompt: string;
  typeCode: string;
  confidence: number | null;
  scoreLabel: "classification" | "scene";
  scoreText: string;
  displayName?: string;
  proposalTypeCode: string;
  proposalDisplayName?: string;
  proposalOrigin: "analysis_record" | "review_sidecar" | "scene_candidate";
  modelIdentity?: string;
  modelRunId?: string;
  experimental: boolean;
  actualCandidate: boolean;
  forced: boolean;
  rationale: string;
  timestampSeconds: number;
  timecode?: string;
  sourceFile?: string;
  evidenceImage: string;
  previewVideo?: string;
  locationStatus: string;
  box?: { x: number; y: number; width: number; height: number };
  adjacentFrames?: string[];
  roiImage?: string;
  ocrCandidate?: string;
  scoreSemantics?:
    | "model_score_not_probability"
    | "zero_shot_relative_not_probability";
  temporalGroup?: TemporalQuestionGroup & {
    evidence: Array<{
      questionId: string;
      evidenceImage: string;
      timecode: string;
      timestampSeconds: number;
    }>;
  };
};

type TemporalReviewUiState = {
  jobId: string | null;
  status: "idle" | "loading" | "ready" | "not_applicable" | "failed";
  orderedQuestionIds: string[] | null;
  groupsByRepresentativeId: Record<string, TemporalQuestionGroup>;
  warning: string | null;
};

type WeakCaseUiState = {
  status: "idle" | "loading" | "ready" | "unavailable" | "failed";
  cases: WeakCaseReference[];
  readySourceCount: number;
  sourceCount: number;
  message: string | null;
};

type MaskRefinementUiState =
  | { status: "idle" | "loading"; capability: null; message: string }
  | {
      status: "ready" | "model_unavailable";
      capability: MaskRefinementCapability;
      message: string;
    }
  | { status: "failed"; capability: null; message: string };

const LABEL_PROGRESS_TYPES = [
  ["포트홀", "POTHOLE"],
  ["노면 파손", "ROAD_SURFACE_DAMAGE"],
  ["낙하물", "ROAD_DEBRIS"],
  ["차선 마모", "LANE_MARKING_FADED"],
  ["식생 가림", "SIGN_VEGETATION_OCCLUSION"],
  ["삭초", "ROADSIDE_MOWING_REQUIRED"],
] as const;

const ISSUE_TYPE_OPTIONS = REVIEW_ISSUE_TYPE_OPTIONS;

function questionPoint(questions: ReviewQuestion[], questionIndex: number) {
  const box = questions[questionIndex]?.box;
  return box
    ? { x: box.x + box.width / 2, y: box.y + box.height / 2 }
    : { x: 66, y: 75 };
}

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

function reviewQuestionsForSummary(
  summary: AnalysisSummary,
  overlays: Record<string, ExperimentalProposalOverlay>,
): ReviewQuestion[] {
  return summary.candidates.map((candidate) => {
    const overlay = overlays[candidate.candidateId];
    const proposalCandidate = overlay
      ? { ...candidate, aiInference: overlay.inference }
      : candidate;
    const proposal = buildCandidateProposal(proposalCandidate);
    const proposalIssueType = apiIssueTypeFromIncident(proposal.typeCode);
    const sourcePrediction = proposalCandidate.aiInference?.predictions
      .filter(
        (prediction) =>
          prediction.issueType === proposalIssueType &&
          prediction.confidence === proposal.confidence,
      )
      .at(0);
    const embeddedModel = candidate.aiInference;
    const embeddedIdentity =
      typeof embeddedModel?.packId === "string" &&
      typeof embeddedModel.packVersion === "string"
        ? `${embeddedModel.packId}@${embeddedModel.packVersion}`
        : undefined;
    return {
      id: `${summary.jobId}:${candidate.candidateId}`,
      prompt: proposal.prompt,
      typeCode: proposal.typeCode,
      confidence: proposal.confidence,
      scoreLabel: proposal.scoreKind,
      scoreText: proposal.scoreText,
      displayName: proposal.displayName ?? undefined,
      proposalTypeCode: proposal.typeCode,
      proposalDisplayName: proposal.displayName ?? undefined,
      proposalOrigin: proposal.experimental
        ? overlay
          ? "review_sidecar"
          : "analysis_record"
        : "scene_candidate",
      modelIdentity: overlay?.modelIdentity ?? embeddedIdentity,
      modelRunId: overlay?.runId,
      experimental: proposal.experimental,
      actualCandidate: true,
      forced: proposal.forced,
      rationale: proposal.rationale,
      timestampSeconds: candidate.representativeTimestampSeconds,
      timecode: formatTimecode(candidate.representativeTimestampSeconds),
      sourceFile: summary.sourceFileName,
      evidenceImage: buildCandidateEvidenceUrl(
        summary.projectId,
        summary.jobId,
        candidate.candidateId,
        "image",
      ),
      previewVideo: buildCandidateEvidenceUrl(
        summary.projectId,
        summary.jobId,
        candidate.candidateId,
        "clip",
      ),
      locationStatus: summary.sourceLocation
        ? `촬영 시작 ${summary.sourceLocation.latitude.toFixed(6)}, ${summary.sourceLocation.longitude.toFixed(6)} · 이 후보 위치는 수동 검증 필요`
        : "GPS 자료가 연결되지 않음 · 수동 위치 검증 필요",
      ...(proposal.box ? { box: proposal.box } : {}),
      ...(sourcePrediction
        ? {
            scoreSemantics:
              sourcePrediction.calibration === "zero_shot_relative"
                ? ("zero_shot_relative_not_probability" as const)
                : ("model_score_not_probability" as const),
          }
        : {}),
    };
  });
}

function severityToApi(value: string) {
  if (value === "높음") return "major";
  if (value === "낮음") return "minor";
  return "moderate";
}

function maintenanceActionForType(typeCode: string) {
  if (typeCode === "ROAD_DEBRIS") return "긴급 제거·안전 확인";
  if (typeCode === "POTHOLE" || typeCode === "ROAD_SURFACE_DAMAGE") {
    return "노면 보수";
  }
  if (typeCode === "ROADSIDE_MOWING_REQUIRED") return "삭초 필요";
  if (typeCode === "LANE_MARKING_FADED") return "차선 재도색";
  if (typeCode === "COLORED_GUIDANCE_FADED") return "색깔유도선 재도색";
  if (
    typeCode === "SIGN_VEGETATION_OCCLUSION" ||
    typeCode === "TUNNEL_NAME_OCCLUSION"
  ) {
    return "표지판·터널명 가림 제거";
  }
  if (
    typeCode === "MEDIAN_BARRIER_DAMAGE" ||
    typeCode === "SIGN_DAMAGE"
  ) {
    return "시설물 보수";
  }
  if (typeCode === "TUNNEL_IDENTITY" || typeCode === "GUIDE_SIGN_IDENTITY") {
    return "명칭·위치 검증";
  }
  return "현장 확인 후 적정 유지보수 조치";
}

function isTextEntryTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement
  );
}

export function LabelingScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const runtime = useRuntimeState();
  const pipeline = usePipelineUi();
  const projectCatalog = useProjectCatalog();
  const [completedJobs, setCompletedJobs] = useState<AnalysisJobResponse[]>([]);
  const [proposalCountsByJobId, setProposalCountsByJobId] = useState<
    Record<string, number>
  >({});
  const [jobListError, setJobListError] = useState<string | null>(null);
  const [loadingJobs, setLoadingJobs] = useState(false);
  const [loadingProposalCounts, setLoadingProposalCounts] = useState(false);
  const [proposalOverlays, setProposalOverlays] = useState<{
    jobId: string | null;
    byCandidateId: Record<string, ExperimentalProposalOverlay>;
    ignoredCount: number;
  }>({
    jobId: null,
    byCandidateId: {},
    ignoredCount: 0,
  });
  const [proposalWarning, setProposalWarning] = useState<string | null>(null);
  const [temporalReview, setTemporalReview] =
    useState<TemporalReviewUiState>({
      jobId: null,
      status: "idle",
      orderedQuestionIds: null,
      groupsByRepresentativeId: {},
      warning: null,
    });
  const [autoPanelOpen, setAutoPanelOpen] = useState(false);
  const [autoCommand, setAutoCommand] = useState("");
  const [autoIssueType, setAutoIssueType] =
    useState<AutoLabelingIssueSelection>("auto");
  const [autoScope, setAutoScope] =
    useState<AutoLabelingScope>("current_job");
  const [autoMode, setAutoMode] =
    useState<AutoLabelingMode>("proposal_only");
  const [autoCompileResult, setAutoCompileResult] =
    useState<AutoLabelingCompileResult | null>(null);
  const [autoRunResult, setAutoRunResult] =
    useState<AutoLabelingRunResult | null>(null);
  const [autoBusy, setAutoBusy] = useState<"compile" | "run" | null>(null);
  const [autoError, setAutoError] = useState<string | null>(null);
  const [autoConfirming, setAutoConfirming] = useState(false);
  const [autoRunConsent, setAutoRunConsent] = useState(false);
  const [proposalRefreshRevision, setProposalRefreshRevision] = useState(0);
  const baseQuestions = useMemo<ReviewQuestion[]>(() => {
    const summary = runtime.analysisSummary;
    if (!summary?.candidates.length) return [];
    return reviewQuestionsForSummary(
      summary,
      proposalOverlays.jobId === summary.jobId
        ? proposalOverlays.byCandidateId
      : {},
    );
  }, [proposalOverlays, runtime.analysisSummary]);
  const questions = useMemo<ReviewQuestion[]>(() => {
    const summary = runtime.analysisSummary;
    if (
      !summary ||
      temporalReview.jobId !== summary.jobId ||
      !temporalReview.orderedQuestionIds
    ) {
      return baseQuestions;
    }
    const byId = new Map(baseQuestions.map((item) => [item.id, item]));
    return temporalReview.orderedQuestionIds.flatMap((questionId) => {
      const question = byId.get(questionId);
      if (!question) return [];
      const group =
        temporalReview.groupsByRepresentativeId[questionId];
      if (!group) return [question];
      const evidence = group.evidenceQuestionIds.flatMap((evidenceId) => {
        const item = byId.get(evidenceId);
        return item
          ? [
              {
                questionId: item.id,
                evidenceImage: item.evidenceImage,
                timecode:
                  item.timecode ?? formatTimecode(item.timestampSeconds),
                timestampSeconds: item.timestampSeconds,
              },
            ]
          : [];
      });
      return [
        {
          ...question,
          adjacentFrames: evidence.map((item) => item.evidenceImage),
          temporalGroup: { ...group, evidence },
        },
      ];
    });
  }, [baseQuestions, runtime.analysisSummary, temporalReview]);
  const reviewKey = runtime.analysisSummary
    ? reviewStorageKey(
        runtime.analysisSummary.projectId,
        runtime.analysisSummary.jobId,
      )
    : "trafficvisson:review:no-active-analysis";
  const restoredHistory = useMemo(
    () => loadReviewHistory(reviewKey),
    [reviewKey],
  );
  const linkedCandidateId = useMemo(() => {
    const summary = runtime.analysisSummary;
    return summary
      ? loadSelectedCandidateId(
          summary.projectId,
          summary.jobId,
          summary.candidates,
        )
      : null;
  }, [runtime.analysisSummary]);
  const linkedQuestionIndex = linkedCandidateId
    ? questions.findIndex((item) => item.id.endsWith(`:${linkedCandidateId}`))
    : -1;
  const initialQuestionIndex =
    linkedQuestionIndex >= 0
      ? linkedQuestionIndex
      : Math.min(
          restoredHistory.length,
          Math.max(questions.length - 1, 0),
        );
  const [reviewMode, setReviewMode] = useState<ReviewMode>("precision");
  const [questionIndex, setQuestionIndex] = useState(initialQuestionIndex);
  const [history, setHistory] = useState<ReviewRecord[]>(restoredHistory);
  const [queueComplete, setQueueComplete] = useState(
    questions.length > 0 && restoredHistory.length >= questions.length,
  );
  const [editMode, setEditMode] = useState(false);
  const [questionIdsByCandidateId, setQuestionIdsByCandidateId] = useState<
    Record<string, string>
  >({});
  const [maskRefinementState, setMaskRefinementState] =
    useState<MaskRefinementUiState>({
      status: "idle",
      capability: null,
      message: "질문 식별 정보를 확인하고 있습니다.",
    });
  const [maskRefinement, setMaskRefinement] =
    useState<MaskRefinementProposal | null>(null);
  const [maskRefinementBusy, setMaskRefinementBusy] = useState(false);
  const [point, setPoint] = useState(() =>
    questionPoint(questions, initialQuestionIndex),
  );
  const [typeCode, setTypeCode] = useState(
    questions[initialQuestionIndex]?.typeCode ?? "UNCLASSIFIED_ROAD_EVENT",
  );
  const [followup, setFollowup] = useState(false);
  const [severity, setSeverity] = useState<UserSeverity>("보통");
  const [ocrText, setOcrText] = useState("");
  const [frameIndex, setFrameIndex] = useState(3);
  const [zoomed, setZoomed] = useState(false);
  const [positiveAnswer, setPositiveAnswer] = useState<"yes" | "edited">("yes");
  const [savingAnswer, setSavingAnswer] = useState(false);
  const [answerError, setAnswerError] = useState<string | null>(null);
  const [pipelineCompletionState, setPipelineCompletionState] = useState<
    "idle" | "submitting" | "queued" | "not_applicable" | "failed"
  >("idle");
  const [pipelineCompletionError, setPipelineCompletionError] =
    useState<string | null>(null);
  const [previewRetry, setPreviewRetry] = useState(0);
  const [previewError, setPreviewError] = useState(false);
  const [weakCaseState, setWeakCaseState] =
    useState<WeakCaseUiState>({
      status: "idle",
      cases: [],
      readySourceCount: 0,
      sourceCount: 0,
      message: null,
    });
  const [selectedWeakCaseId, setSelectedWeakCaseId] =
    useState<string | null>(null);
  const [failedWeakCaseImages, setFailedWeakCaseImages] = useState<
    ReadonlySet<string>
  >(new Set());
  const questionHeadingRef = useRef<HTMLHeadingElement>(null);
  const pipelineCompletionAttemptRef = useRef<string | null>(null);
  const activeProjectId =
    projectCatalog.activeProjectId ?? runtime.analysisSummary?.projectId ?? null;
  const activeProject = projectCatalog.projects.find(
    ({ id }) => id === activeProjectId,
  );
  const activeAnalysisForProject =
    runtime.analysisSummary?.projectId === activeProjectId
      ? runtime.analysisSummary
      : null;
  const currentPipelineJob =
    pipeline.projectId === activeProjectId ? pipeline.job : null;
  const currentJobIndex = runtime.analysisSummary
    ? completedJobs.findIndex(
        (job) => job.job_id === runtime.analysisSummary?.jobId,
      )
    : -1;
  const proposalJobCount = Object.values(proposalCountsByJobId).filter(
    (count) => count > 0,
  ).length;

  useEffect(() => {
    setAutoCompileResult(null);
    setAutoRunResult(null);
    setAutoConfirming(false);
    setAutoRunConsent(false);
    setAutoError(null);
    setPipelineCompletionState("idle");
    setPipelineCompletionError(null);
    pipelineCompletionAttemptRef.current = null;
  }, [activeProjectId]);

  useEffect(() => {
    setPipelineCompletionState("idle");
    setPipelineCompletionError(null);
    pipelineCompletionAttemptRef.current = null;
  }, [runtime.analysisSummary?.jobId]);

  const question = questions[questionIndex];
  const candidateId = question?.actualCandidate
    ? question.id.split(":").at(-1)
    : undefined;
  const serverQuestionId = candidateId
    ? questionIdsByCandidateId[candidateId]
    : undefined;
  const maskRefinementIdentity: MaskRefinementIdentity | null =
    runtime.analysisSummary && candidateId && serverQuestionId
      ? {
          projectId: runtime.analysisSummary.projectId,
          jobId: runtime.analysisSummary.jobId,
          questionId: serverQuestionId,
          candidateId,
        }
      : null;
  const mandatoryQuestion = Boolean(
    question?.forced ||
      typeCode === "ROAD_DEBRIS",
  );
  const selectedWeakCase =
    weakCaseState.cases.find(
      ({ caseId }) => caseId === selectedWeakCaseId,
    ) ?? weakCaseState.cases[0] ?? null;
  const currentEvidenceImage =
    question?.adjacentFrames?.[frameIndex] ??
    question?.evidenceImage ??
    realTunnelEvidenceImage;
  const refinedBox = maskRefinement
    ? {
        x: maskRefinement.normalizedBox.x * 100,
        y: maskRefinement.normalizedBox.y * 100,
        width: maskRefinement.normalizedBox.width * 100,
        height: maskRefinement.normalizedBox.height * 100,
      }
    : undefined;
  const currentBox = refinedBox ?? question?.box;
  const visibleBox = maskRefinement
    ? undefined
    : question?.temporalGroup &&
        frameIndex !== question.temporalGroup.peakEvidenceIndex &&
        !editMode
      ? undefined
      : question?.box;
  const temporalEvidence = question?.temporalGroup?.evidence ?? [];
  const temporalFrameWindowStart = temporalEvidence.length
    ? Math.max(
        0,
        Math.min(
          frameIndex - 3,
          Math.max(temporalEvidence.length - 7, 0),
        ),
      )
    : 0;
  const temporalFrameWindow = temporalEvidence.slice(
    temporalFrameWindowStart,
    temporalFrameWindowStart + 7,
  );
  const currentTemporalEvidence = temporalEvidence[frameIndex];
  const reviewedQuestionIds = useMemo(
    () => new Set(history.map((record) => record.questionId)),
    [history],
  );
  const completed = questions.filter((item) =>
    reviewedQuestionIds.has(item.id),
  ).length;
  const progress = questions.length
    ? Math.round((completed / questions.length) * 100)
    : 0;
  const classCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const record of history) {
      if (record.trainingStatus !== "approved") continue;
      counts.set(
        record.selectedType,
        (counts.get(record.selectedType) ?? 0) + 1,
      );
    }
    return counts;
  }, [history]);

  useEffect(() => {
    setFailedWeakCaseImages(new Set());
    setSelectedWeakCaseId(null);
    if (!question || !activeProjectId) {
      setWeakCaseState({
        status: "idle",
        cases: [],
        readySourceCount: 0,
        sourceCount: 0,
        message: null,
      });
      return;
    }
    const service = window.trafficSuperVision?.referenceCases;
    if (!service) {
      setWeakCaseState({
        status: "unavailable",
        cases: [],
        readySourceCount: 0,
        sourceCount: 0,
        message: "3·4분기 사례 브릿지가 연결되지 않았습니다.",
      });
      return;
    }
    const proposedIssueType = apiIssueTypeFromIncident(typeCode);
    const issueType =
      proposedIssueType === "other" ? null : proposedIssueType;
    const queryText = [question.prompt, ocrText.trim()]
      .filter(Boolean)
      .join(" ")
      .slice(0, 240);
    let active = true;
    setWeakCaseState({
      status: "loading",
      cases: [],
      readySourceCount: 0,
      sourceCount: 0,
      message: "고정 SHA-256을 확인하고 참고 사례를 찾는 중입니다.",
    });
    const timer = window.setTimeout(() => {
      void service
        .search({
          projectId: activeProjectId,
          ...(issueType ? { issueType } : {}),
          queryText,
          limit: 5,
        })
        .then((value) => {
          if (!active) return;
          const parsed = parseWeakCaseSearchResponse(value, {
            issueType,
            requestedLimit: 5,
          });
          setWeakCaseState({
            status:
              parsed.capabilityState === "unavailable"
                ? "unavailable"
                : "ready",
            cases: parsed.cases,
            readySourceCount: parsed.readySourceCount,
            sourceCount: parsed.sourceCount,
            message:
              parsed.capabilityState === "unavailable"
                ? "고정 해시를 통과한 3·4분기 사례 원본이 없습니다."
                : parsed.cases.length
                  ? "문구·표 위치 기준 참고 후보이며 아직 확정 라벨이 아닙니다."
                  : "현재 유형과 일치하는 문구 기반 참고 후보를 찾지 못했습니다.",
          });
          setSelectedWeakCaseId(parsed.cases[0]?.caseId ?? null);
        })
        .catch((error) => {
          if (!active) return;
          setWeakCaseState({
            status: "failed",
            cases: [],
            readySourceCount: 0,
            sourceCount: 0,
            message:
              error instanceof Error
                ? error.message
                : "3·4분기 참고 사례를 안전하게 불러오지 못했습니다.",
          });
        });
    }, 180);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [activeProjectId, ocrText, question, typeCode]);

  useEffect(() => {
    saveReviewHistory(reviewKey, history);
  }, [history, reviewKey]);

  useEffect(() => {
    const bridge = window.trafficSuperVision;
    if (!activeProjectId || !bridge) {
      setCompletedJobs([]);
      setProposalCountsByJobId({});
      return;
    }
    let active = true;
    setLoadingJobs(true);
    setLoadingProposalCounts(true);
    setProposalCountsByJobId({});
    setJobListError(null);
    void bridge.analysis
      .list({ projectId: activeProjectId })
      .then(async (value) => {
        if (!active) return;
        const jobs = parseAnalysisJobList(value)
          .filter(
            (
              job,
            ): job is AnalysisJobResponse & {
              status: "completed";
              result: NonNullable<AnalysisJobResponse["result"]>;
            } => job.status === "completed" && job.result !== null,
          )
          .sort(
            (left, right) =>
              Date.parse(right.updated_at) - Date.parse(left.updated_at),
          );
        setCompletedJobs(jobs);
        setLoadingJobs(false);
        const current = runtime.analysisSummary;
        if (
          jobs.length > 0 &&
          (!current ||
            current.projectId !== activeProjectId ||
            !jobs.some((job) => job.job_id === current.jobId))
        ) {
          const restored = toRestoredRuntime(jobs[0]);
          restoreCompletedAnalysis(
            restored.activeImport,
            restored.analysisSummary,
          );
        }
        const counts: Record<string, number> = {};
        let countFailures = 0;
        for (let index = 0; index < jobs.length; index += 6) {
          const batch = jobs.slice(index, index + 6);
          const batchCounts = await Promise.all(
            batch.map(async (job) => {
              try {
                const value =
                  await bridge.candidateQuestions.list({
                    projectId: activeProjectId,
                    jobId: job.job_id,
                  });
                const summary = toRestoredRuntime(job).analysisSummary;
                const overlays =
                  experimentalProposalOverlaysFromQuestionList(value, {
                    projectId: activeProjectId,
                    jobId: job.job_id,
                    candidates: summary.candidates,
                  });
                return [
                  job.job_id,
                  Object.keys(overlays.byCandidateId).length,
                ] as const;
              } catch {
                countFailures += 1;
                return [job.job_id, 0] as const;
              }
            }),
          );
          if (!active) return;
          for (const [jobId, count] of batchCounts) counts[jobId] = count;
          setProposalCountsByJobId({ ...counts });
        }
        if (countFailures > 0 && active) {
          setJobListError(
            `AI 제안 수를 읽지 못한 분석 영상이 ${countFailures}개 있습니다. 영상 선택과 원본 후보 검토는 계속할 수 있습니다.`,
          );
        }
      })
      .catch((error) => {
        if (!active) return;
        setJobListError(
          error instanceof Error
            ? error.message
            : "저장된 분석 영상 목록을 읽지 못했습니다.",
        );
      })
      .finally(() => {
        if (active) {
          setLoadingJobs(false);
          setLoadingProposalCounts(false);
        }
      });
    return () => {
      active = false;
    };
  }, [activeProjectId, proposalRefreshRevision]);

  useEffect(() => {
    setPreviewRetry(0);
    setPreviewError(false);
    setFrameIndex(question?.temporalGroup?.peakEvidenceIndex ?? 3);
  }, [
    question?.id,
    question?.temporalGroup?.proposalId,
    question?.temporalGroup?.peakEvidenceIndex,
  ]);

  useEffect(() => {
    if (!activeProjectId) return;
    if (currentPipelineJob) {
      setReviewMode(currentPipelineJob.profileSnapshot.reviewMode);
      setFollowup(false);
      setEditMode(false);
      return;
    }
    const fallback = reviewModeForQuestionPolicy(
      activeProject?.questionPolicy,
    );
    setReviewMode(loadReviewModePreference(activeProjectId, fallback));
    setFollowup(false);
    setEditMode(false);
  }, [
    activeProject?.questionPolicy,
    activeProjectId,
    currentPipelineJob?.jobId,
    currentPipelineJob?.profileSnapshot.reviewMode,
  ]);

  useEffect(() => {
    if (!previewError || previewRetry >= 2) return;
    const timer = window.setTimeout(() => {
      setPreviewError(false);
      setPreviewRetry((value) => value + 1);
    }, 1_200);
    return () => window.clearTimeout(timer);
  }, [previewError, previewRetry]);

  useEffect(() => {
    setEditMode(false);
    setMaskRefinement(null);
    setMaskRefinementBusy(false);
    if (!maskRefinementIdentity) {
      setMaskRefinementState({
        status: "idle",
        capability: null,
        message: "질문 식별 정보를 확인하고 있습니다.",
      });
      return;
    }
    const service = window.trafficSuperVision?.maskRefinements;
    if (!service) {
      setMaskRefinementState({
        status: "failed",
        capability: null,
        message:
          "현재 데스크톱 브릿지에는 SAM2 영역 수정 기능이 연결되지 않았습니다.",
      });
      return;
    }
    let active = true;
    const expected = maskRefinementIdentity;
    setMaskRefinementState({
      status: "loading",
      capability: null,
      message: "SAM2 모델팩과 질문 근거를 확인하고 있습니다.",
    });
    void service
      .capability(expected)
      .then((value) => {
        if (!active) return;
        const capability = parseMaskRefinementCapability(value, expected);
        setMaskRefinementState({
          status: capability.state,
          capability,
          message: capability.message,
        });
      })
      .catch((error) => {
        if (!active) return;
        setMaskRefinementState({
          status: "failed",
          capability: null,
          message:
            error instanceof Error
              ? error.message
              : "SAM2 영역 수정 상태를 확인하지 못했습니다.",
        });
      });
    return () => {
      active = false;
    };
  }, [
    candidateId,
    runtime.analysisSummary?.jobId,
    runtime.analysisSummary?.projectId,
    serverQuestionId,
  ]);

  useEffect(() => {
    const summary = runtime.analysisSummary;
    const candidateId = question?.actualCandidate
      ? question.id.split(":").at(-1)
      : undefined;
    if (!summary || !candidateId) return;
    saveSelectedCandidateId(summary.projectId, summary.jobId, candidateId);
  }, [question, runtime.analysisSummary]);

  useEffect(() => {
    const summary = runtime.analysisSummary;
    if (!summary || !window.trafficSuperVision) return;
    let active = true;
    setHistory([]);
    setQuestionIndex(0);
    setTypeCode(
      summary.candidates[0]
        ? buildCandidateProposal(summary.candidates[0]).typeCode
        : "UNCLASSIFIED_ROAD_EVENT",
    );
    setQueueComplete(false);
    setFollowup(false);
    setEditMode(false);
    setQuestionIdsByCandidateId({});
    setMaskRefinement(null);
    setMaskRefinementBusy(false);
    setMaskRefinementState({
      status: "loading",
      capability: null,
      message: "SAM2 영역 수정 기능을 확인하고 있습니다.",
    });
    setProposalOverlays({
      jobId: summary.jobId,
      byCandidateId: {},
      ignoredCount: 0,
    });
    setProposalWarning(null);
    setTemporalReview({
      jobId: summary.jobId,
      status: "loading",
      orderedQuestionIds: null,
      groupsByRepresentativeId: {},
      warning: null,
    });
    setAnswerError(null);
    void window.trafficSuperVision.candidateQuestions
      .list({ projectId: summary.projectId, jobId: summary.jobId })
      .then(async (value) => {
        if (!active) return;
        const overlays = experimentalProposalOverlaysFromQuestionList(value, {
          projectId: summary.projectId,
          jobId: summary.jobId,
          candidates: summary.candidates,
        });
        setProposalOverlays({
          jobId: summary.jobId,
          byCandidateId: overlays.byCandidateId,
          ignoredCount: overlays.ignoredCount,
        });
        setProposalCountsByJobId((current) => ({
          ...current,
          [summary.jobId]: Object.keys(overlays.byCandidateId).length,
        }));
        setProposalWarning(
          overlays.ignoredCount > 0
            ? `검증에 실패한 실험 제안 ${overlays.ignoredCount}건은 표시하지 않고 원래 장면 후보로 되돌렸습니다.`
            : null,
        );
        const nextQuestions = reviewQuestionsForSummary(
          summary,
          overlays.byCandidateId,
        );
        setQuestionIdsByCandidateId(
          candidateQuestionIds(value, {
            projectId: summary.projectId,
            jobId: summary.jobId,
            candidateIds: summary.candidates.map(
              ({ candidateId }) => candidateId,
            ),
          }),
        );
        const restored = reviewRecordsFromQuestionList(value, summary.jobId);
        setHistory(restored);
        const restoredLinkedIndex = linkedCandidateId
          ? nextQuestions.findIndex((item) =>
              item.id.endsWith(`:${linkedCandidateId}`),
            )
          : -1;
        const nextIndex =
          restoredLinkedIndex >= 0
            ? restoredLinkedIndex
            : Math.min(
                restored.length,
                Math.max(nextQuestions.length - 1, 0),
              );
        setQuestionIndex(nextIndex);
        setTypeCode(
          nextQuestions[nextIndex]?.typeCode ?? "UNCLASSIFIED_ROAD_EVENT",
        );
        setPoint(questionPoint(nextQuestions, nextIndex));
        setQueueComplete(
          nextQuestions.length > 0 && restored.length >= nextQuestions.length,
        );

        const temporalInputs = nextQuestions.flatMap((item) => {
          const issueType = apiIssueTypeFromIncident(
            item.proposalTypeCode,
          );
          const detectionId = item.actualCandidate
            ? item.id.split(":").at(-1)
            : undefined;
          if (
            !item.experimental ||
            item.confidence === null ||
            !issueType ||
            !detectionId
          ) {
            return [];
          }
          return [
            {
              detectionId,
              timestampSeconds: item.timestampSeconds,
              issueType,
              score: item.confidence,
              ...(item.box
                ? {
                    normalizedBox: {
                      x: item.box.x / 100,
                      y: item.box.y / 100,
                      width: item.box.width / 100,
                      height: item.box.height / 100,
                    },
                  }
                : {}),
              ...(item.scoreSemantics
                ? { scoreSemantics: item.scoreSemantics }
                : {}),
            },
          ];
        });
        if (temporalInputs.length === 0) {
          if (active) {
            setTemporalReview({
              jobId: summary.jobId,
              status: "not_applicable",
              orderedQuestionIds: null,
              groupsByRepresentativeId: {},
              warning: null,
            });
          }
          return;
        }
        const service = window.trafficSuperVision?.reviewComputations;
        if (!service) {
          if (active) {
            setTemporalReview({
              jobId: summary.jobId,
              status: "failed",
              orderedQuestionIds: null,
              groupsByRepresentativeId: {},
              warning:
                "프레임 사건 병합 기능이 연결되지 않아 기존 질문 순서를 유지합니다.",
            });
          }
          return;
        }
        try {
          const request = buildTemporalIncidentReviewRequest({
            projectId: summary.projectId,
            analysisJobId: summary.jobId,
            sourceFileSha256: summary.sourceSha256,
            detections: temporalInputs,
          });
          const response = await service.temporalIncidents(request);
          const parsed = parseTemporalIncidentReviewResponse(response, {
            projectId: summary.projectId,
            analysisJobId: summary.jobId,
            sourceFileSha256: summary.sourceSha256,
            detectionIds: request.detections.map(
              ({ detectionId }) => detectionId,
            ),
          });
          if (!active) return;
          const detectionToQuestionId = Object.fromEntries(
            temporalInputs.map(({ detectionId }) => [
              detectionId,
              nextQuestions.find((item) =>
                item.id.endsWith(`:${detectionId}`),
              )?.id ?? "",
            ]),
          );
          const presentation = buildTemporalQuestionOrder(
            nextQuestions.map(({ id }) => id),
            detectionToQuestionId,
            parsed,
          );
          setTemporalReview({
            jobId: summary.jobId,
            status: "ready",
            orderedQuestionIds: presentation.orderedQuestionIds,
            groupsByRepresentativeId:
              presentation.groupsByRepresentativeId,
            warning: null,
          });
          const orderedQuestions =
            presentation.orderedQuestionIds.flatMap((id) => {
              const item = nextQuestions.find(
                (questionItem) => questionItem.id === id,
              );
              return item ? [item] : [];
            });
          const restoredIds = new Set(
            restored.map(({ questionId }) => questionId),
          );
          const linkedQuestionId = linkedCandidateId
            ? nextQuestions.find((item) =>
                item.id.endsWith(`:${linkedCandidateId}`),
              )?.id
            : undefined;
          const linkedRepresentativeId = linkedQuestionId
            ? Object.values(
                presentation.groupsByRepresentativeId,
              ).find(({ evidenceQuestionIds }) =>
                evidenceQuestionIds.includes(linkedQuestionId),
              )?.representativeQuestionId ?? linkedQuestionId
            : undefined;
          const preferredIndex = linkedRepresentativeId
            ? orderedQuestions.findIndex(
                ({ id }) => id === linkedRepresentativeId,
              )
            : -1;
          const firstUnansweredIndex = orderedQuestions.findIndex(
            ({ id }) => !restoredIds.has(id),
          );
          const groupedIndex =
            preferredIndex >= 0
              ? preferredIndex
              : firstUnansweredIndex >= 0
                ? firstUnansweredIndex
                : Math.max(orderedQuestions.length - 1, 0);
          setQuestionIndex(groupedIndex);
          setTypeCode(
            orderedQuestions[groupedIndex]?.typeCode ??
              "UNCLASSIFIED_ROAD_EVENT",
          );
          setPoint(questionPoint(orderedQuestions, groupedIndex));
          setQueueComplete(
            orderedQuestions.length > 0 &&
              orderedQuestions.every(({ id }) => restoredIds.has(id)),
          );
        } catch (error) {
          if (!active) return;
          setTemporalReview({
            jobId: summary.jobId,
            status: "failed",
            orderedQuestionIds: null,
            groupsByRepresentativeId: {},
            warning: `프레임 사건 병합을 완료하지 못해 기존 질문 순서를 유지합니다. ${
              error instanceof Error
                ? error.message
                : "응답을 검증하지 못했습니다."
            }`,
          });
        }
      })
      .catch((error) => {
        if (active) {
          setAnswerError(
            error instanceof Error
              ? error.message
              : "저장된 라벨 기록을 읽지 못했습니다.",
          );
        }
      });
    return () => {
      active = false;
    };
  }, [
    linkedCandidateId,
    proposalRefreshRevision,
    runtime.analysisSummary,
  ]);

  useEffect(() => {
    if (completed > 0 && !queueComplete) questionHeadingRef.current?.focus();
  }, [completed, queueComplete, questionIndex]);

  useEffect(() => {
    const job = currentPipelineJob;
    if (
      !queueComplete ||
      !job ||
      job.status !== "awaiting_review" ||
      questions.length < 1
    ) {
      return;
    }
    const attemptKey = [
      job.jobId,
      ...history.map(
        ({ candidateId, recordHash, answerRevision }) =>
          `${candidateId ?? "local"}:${recordHash ?? "unsaved"}:${
            answerRevision ?? 0
          }`,
      ),
    ].join("|");
    if (pipelineCompletionAttemptRef.current === attemptKey) return;
    pipelineCompletionAttemptRef.current = attemptKey;
    void requestPipelineReviewCompletion(history);
  }, [
    currentPipelineJob?.jobId,
    currentPipelineJob?.status,
    history,
    queueComplete,
    questions.length,
  ]);

  useEffect(() => {
    if (!question || queueComplete) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.repeat ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        isTextEntryTarget(event.target) ||
        savingAnswer
      ) {
        return;
      }
      if (editMode) {
        if (event.key === "Escape") {
          event.preventDefault();
          cancelMaskEdit();
        }
        return;
      }
      if (followup) {
        const nextSeverity = severityFromShortcut(event.key);
        if (nextSeverity) {
          event.preventDefault();
          void confirmSeverity(nextSeverity);
        }
        return;
      }
      const key = event.key.toLowerCase();
      if (key === "e") {
        event.preventDefault();
        beginMaskEdit();
        return;
      }
      if (key === "n" || key === "u") {
        event.preventDefault();
        void submit(key === "n" ? "no" : "unsure");
        return;
      }
      if (key !== "y") return;
      if (
        typeCode === "UNCLASSIFIED_ROAD_EVENT" ||
        ((typeCode === "TUNNEL_IDENTITY" ||
          typeCode === "GUIDE_SIGN_IDENTITY") &&
          !ocrText.trim())
      ) {
        return;
      }
      event.preventDefault();
      void submit("yes");
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    editMode,
    followup,
    maskRefinementState,
    ocrText,
    question,
    queueComplete,
    reviewMode,
    savingAnswer,
    severity,
    typeCode,
  ]);

  const stageText = useMemo(() => {
    if (editMode) {
      if (maskRefinementBusy) {
        return "로컬 SAM2가 선택한 점을 기준으로 마스크를 다시 계산하고 있습니다.";
      }
      if (maskRefinement) {
        return `${maskRefinement.modelIdentity} 제안입니다. 새 마스크 확인을 눌러야만 답변 저장 단계로 이동합니다.`;
      }
      return "대표 프레임에서 실제 대상 내부를 한 번 클릭하세요. SAM2가 새 마스크를 제안하며 확인 전에는 라벨·사건·학습 데이터가 생성되지 않습니다.";
    }
    if (followup) {
      return "AI 검지 신뢰도와 심각도는 다른 값입니다. 1·2·3 중 사용자가 고른 심각도만 최종 라벨로 저장됩니다.";
    }
    return question?.rationale ?? "분석 후보를 기다리고 있습니다.";
  }, [
    editMode,
    followup,
    maskRefinement,
    maskRefinementBusy,
    question?.rationale,
  ]);

  function beginMaskEdit() {
    if (
      maskRefinementState.status !== "ready" ||
      !maskRefinementIdentity
    ) {
      setAnswerError(maskRefinementState.message);
      return;
    }
    setAnswerError(null);
    setMaskRefinement(null);
    setMaskRefinementBusy(false);
    setFrameIndex(question.temporalGroup?.peakEvidenceIndex ?? 3);
    setEditMode(true);
  }

  function cancelMaskEdit() {
    setEditMode(false);
    setMaskRefinement(null);
    setMaskRefinementBusy(false);
    setPoint(questionPoint(questions, questionIndex));
  }

  async function requestMaskRefinement(clickX: number, clickY: number) {
    const service = window.trafficSuperVision?.maskRefinements;
    if (
      !service ||
      !maskRefinementIdentity ||
      maskRefinementState.status !== "ready" ||
      maskRefinementBusy
    ) {
      setAnswerError(maskRefinementState.message);
      return;
    }
    const boundedX = Math.max(0, Math.min(1, clickX));
    const boundedY = Math.max(0, Math.min(1, clickY));
    setPoint({ x: boundedX * 100, y: boundedY * 100 });
    setMaskRefinement(null);
    setMaskRefinementBusy(true);
    setAnswerError(null);
    try {
      const value = await service.refine({
        ...maskRefinementIdentity,
        click: { x: boundedX, y: boundedY },
      });
      const parsed = parseMaskRefinementProposal(
        value,
        maskRefinementIdentity,
      );
      if (parsed.status === "model_unavailable") {
        setMaskRefinementState({
          status: "model_unavailable",
          capability: {
            state: "model_unavailable",
            message: parsed.message,
            errorCode: "model_pack_not_configured",
          },
          message: parsed.message,
        });
        setEditMode(false);
        setAnswerError(parsed.message);
        return;
      }
      setMaskRefinement(parsed.proposal);
    } catch (error) {
      setMaskRefinement(null);
      setAnswerError(
        error instanceof Error
          ? error.message
          : "SAM2 영역 수정에 실패했습니다.",
      );
    } finally {
      setMaskRefinementBusy(false);
    }
  }

  function recordsWithSavedAnswer(
    records: readonly ReviewRecord[],
    record: ReviewRecord,
  ) {
    return [
      ...records.filter((item) => item.questionId !== record.questionId),
      record,
    ].sort((left, right) => left.question - right.question);
  }

  async function requestPipelineReviewCompletion(
    records: readonly ReviewRecord[],
  ) {
    const job = currentPipelineJob;
    if (job) {
      pipelineCompletionAttemptRef.current = [
        job.jobId,
        ...records.map(
          ({ candidateId, recordHash, answerRevision }) =>
            `${candidateId ?? "local"}:${recordHash ?? "unsaved"}:${
              answerRevision ?? 0
            }`,
        ),
      ].join("|");
    }
    if (
      job?.reviewCompletionId &&
      ["queued", "running", "completed"].includes(job.status)
    ) {
      setPipelineCompletionState("queued");
      setPipelineCompletionError(null);
      return true;
    }
    const result = await buildPipelineReviewCompletion({
      pipelineJob: job,
      projectId: activeProjectId,
      analysisJobId: runtime.analysisSummary?.jobId ?? null,
      sourceSha256: runtime.analysisSummary?.sourceSha256 ?? null,
      questionCandidateIds: questions.map((item) =>
        item.actualCandidate ? item.id.split(":").at(-1) ?? null : null,
      ),
      records,
    });
    if (!result.ok) {
      if (result.reason === "pipeline_not_waiting") {
        setPipelineCompletionState("not_applicable");
        setPipelineCompletionError(null);
        return false;
      }
      setPipelineCompletionState("failed");
      setPipelineCompletionError(result.message);
      return false;
    }
    const service = window.trafficSuperVision?.pipeline;
    if (!service) {
      setPipelineCompletionState("failed");
      setPipelineCompletionError(
        "자동 결과 갱신 기능을 불러오지 못했습니다. Electron을 다시 시작한 뒤 재시도하세요.",
      );
      return false;
    }
    setPipelineCompletionState("submitting");
    setPipelineCompletionError(null);
    try {
      const next = parsePipelineJob(
        await service.completeReview(result.request),
      );
      if (
        next.projectId !== result.request.projectId ||
        next.jobId !== result.request.jobId ||
        next.reviewCompletionId !== result.request.reviewCompletionId ||
        !["queued", "running", "completed"].includes(next.status)
      ) {
        throw new Error(
          "서버가 검토 완료와 자동 결과 갱신 상태를 확정하지 않았습니다.",
        );
      }
      publishPipelineJob(next);
      setPipelineCompletionState("queued");
      return true;
    } catch (error) {
      setPipelineCompletionState("failed");
      setPipelineCompletionError(
        error instanceof Error
          ? error.message
          : "검토 결과를 지도·CSV·증거·보고서 초안에 반영하지 못했습니다.",
      );
      return false;
    }
  }

  async function appendRecord(
    answer: Answer,
    details: {
      severity?: UserSeverity;
      action?: string;
      includeSeverity?: boolean;
    } = {},
  ) {
    if (
      answer === "edited" &&
      (!maskRefinement || !serverQuestionId)
    ) {
      setAnswerError(
        "SAM2 새 마스크를 받은 뒤 사용자가 다시 확인해야 영역 수정 답변을 저장할 수 있습니다.",
      );
      return null;
    }
    const trainingStatus =
      answer === "yes" || answer === "edited"
        ? "approved"
        : answer === "no"
          ? "negative"
          : answer === "unsure"
            ? "held"
            : "excluded";
    const positive = answer === "yes" || answer === "edited";
    const finalSeverity =
      positive && details.includeSeverity !== false
        ? details.severity ?? severity
        : undefined;
    const finalAction =
      positive
        ? details.action ?? maintenanceActionForType(typeCode)
        : undefined;
    const geometry = currentBox
      ? {
          x: Math.max(0, currentBox.x),
          y: Math.max(0, currentBox.y),
          width: currentBox.width,
          height: currentBox.height,
        }
      : undefined;
    const answerCandidateId = question.actualCandidate
      ? question.id.split(":").at(-1)
      : undefined;
    const record: ReviewRecord = {
      question: questionIndex,
      questionId: question.id,
      candidateId: answerCandidateId,
      answer,
      selectedType: typeCode,
      finalGeometry: geometry,
      severity: finalSeverity,
      severitySource: finalSeverity ? "user" : undefined,
      reviewMode,
      action: finalAction,
      ocrText: ocrText.trim() || undefined,
      reviewedAt: new Date().toISOString(),
      trainingStatus,
    };
    const summary = runtime.analysisSummary;
    if (
      question.actualCandidate &&
      summary &&
      answerCandidateId &&
      window.trafficSuperVision
    ) {
      setSavingAnswer(true);
      setAnswerError(null);
      try {
        const response = (await window.trafficSuperVision.candidateQuestions.answer({
          projectId: summary.projectId,
          jobId: summary.jobId,
          candidateId: answerCandidateId,
          ...(answer === "edited" && maskRefinement && serverQuestionId
            ? {
                questionId: serverQuestionId,
                maskRefinementId: maskRefinement.refinementId,
              }
            : {}),
          answer: answer === "edited" ? "edit" : answer,
          selectedIssueType: positive
            ? (apiIssueTypeFromIncident(typeCode) ?? undefined)
            : undefined,
          severity: finalSeverity ? severityToApi(finalSeverity) : undefined,
          maintenanceAction: finalAction,
          ocrText: positive ? ocrText.trim() || undefined : undefined,
          normalizedBox:
            positive && geometry && answer !== "edited"
              ? {
                  x: geometry.x / 100,
                  y: geometry.y / 100,
                  width: geometry.width / 100,
                  height: geometry.height / 100,
                }
              : undefined,
        })) as {
          record_id?: string;
          record_hash?: string;
          answer_revision?: number;
        };
        if (
          typeof response.record_id !== "string" ||
          typeof response.record_hash !== "string" ||
          typeof response.answer_revision !== "number"
        ) {
          throw new Error("라벨 저장 완료 응답을 검증하지 못했습니다.");
        }
        record.recordId = response.record_id;
        record.recordHash = response.record_hash;
        record.answerRevision = response.answer_revision;
      } catch (error) {
        setAnswerError(
          error instanceof Error
            ? error.message
            : "라벨 답변을 영속 저장하지 못했습니다.",
        );
        return null;
      } finally {
        setSavingAnswer(false);
      }
    }
    setHistory((current) => recordsWithSavedAnswer(current, record));
    return record;
  }

  async function submit(answer: Answer) {
    setEditMode(false);
    const decision = decideLabelingSubmission({
      reviewMode,
      answer,
      issueType: typeCode,
    });
    if (decision.kind === "require_user_severity") {
      setPositiveAnswer(decision.answer);
      setFollowup(true);
      return;
    }
    const saved = await appendRecord(decision.answer, {
      includeSeverity: decision.includeSeverity,
    });
    if (saved) {
      goNext(saved);
    }
  }

  function goNext(savedRecord: ReviewRecord) {
    const next = questionIndex + 1;
    if (next >= questions.length) {
      const nextHistory = recordsWithSavedAnswer(history, savedRecord);
      setQueueComplete(true);
      setFollowup(false);
      setEditMode(false);
      setMaskRefinement(null);
      setMaskRefinementBusy(false);
      void requestPipelineReviewCompletion(nextHistory);
      return;
    }
    setQuestionIndex(next);
    setTypeCode(questions[next].typeCode);
    setPoint(questionPoint(questions, next));
    setFrameIndex(3);
    setZoomed(false);
    setOcrText("");
    setSeverity("보통");
    setEditMode(false);
    setMaskRefinement(null);
    setMaskRefinementBusy(false);
    setFollowup(false);
  }

  function undo() {
    setHistory((current) => {
      const last = [...current]
        .reverse()
        .find((record) =>
          questions.some((item) => item.id === record.questionId),
        );
      const lastQuestionIndex = last
        ? questions.findIndex((item) => item.id === last.questionId)
        : -1;
      if (last && lastQuestionIndex >= 0) {
        setQuestionIndex(lastQuestionIndex);
        setTypeCode(
          last.selectedType || questions[lastQuestionIndex].typeCode,
        );
        setPoint(
          last.finalGeometry
            ? {
                x: last.finalGeometry.x + last.finalGeometry.width / 2,
                y: last.finalGeometry.y + last.finalGeometry.height / 2,
              }
            : questionPoint(questions, lastQuestionIndex),
        );
        setOcrText(last.ocrText ?? "");
        setSeverity(
          last.severity === "낮음" ||
            last.severity === "보통" ||
            last.severity === "높음"
            ? last.severity
            : "보통",
        );
      }
      return runtime.analysisSummary && last?.candidateId
        ? current
        : last
          ? current.filter((item) => item !== last)
          : current;
    });
    setQueueComplete(false);
    setFollowup(false);
    setEditMode(false);
    setMaskRefinement(null);
    setMaskRefinementBusy(false);
  }

  async function confirmSeverity(nextSeverity: UserSeverity) {
    setSeverity(nextSeverity);
    const saved = await appendRecord(positiveAnswer, {
      severity: nextSeverity,
      includeSeverity: true,
    });
    if (saved) {
      goNext(saved);
    }
  }

  function selectReviewMode(nextMode: ReviewMode) {
    if (currentPipelineJob) return;
    setReviewMode(nextMode);
    if (activeProjectId) {
      saveReviewModePreference(activeProjectId, nextMode);
    }
    setFollowup(false);
    setEditMode(false);
    setMaskRefinement(null);
    setMaskRefinementBusy(false);
  }

  function selectCompletedJob(jobId: string) {
    const selected = completedJobs.find((job) => job.job_id === jobId);
    if (!selected) {
      setJobListError("선택한 분석 영상을 찾지 못했습니다.");
      return;
    }
    const restored = toRestoredRuntime(selected);
    restoreCompletedAnalysis(restored.activeImport, restored.analysisSummary);
    setJobListError(null);
  }

  function selectNextProposalJob() {
    if (completedJobs.length === 0) return;
    const start = Math.max(currentJobIndex, -1);
    for (let offset = 1; offset <= completedJobs.length; offset += 1) {
      const index = (start + offset) % completedJobs.length;
      const job = completedJobs[index];
      if ((proposalCountsByJobId[job.job_id] ?? 0) > 0) {
        selectCompletedJob(job.job_id);
        return;
      }
    }
  }

  const autoMandatoryReview =
    (autoIssueType !== "auto" &&
      requiresMandatoryReview(autoIssueType)) ||
    Boolean(
      autoCompileResult &&
        requiresMandatoryReview(
          autoCompileResult.recipe.issueType,
          autoCompileResult.capabilities,
        ),
    );
  const effectiveAutoScope =
    autoScope === "current_job" && !activeAnalysisForProject
      ? "project"
      : autoScope;

  function invalidateAutoRecipe() {
    setAutoCompileResult(null);
    setAutoRunResult(null);
    setAutoConfirming(false);
    setAutoRunConsent(false);
    setAutoError(null);
  }

  async function compileAutoLabeling() {
    const service = window.trafficSuperVision?.autoLabeling;
    if (!activeProjectId) {
      setAutoError("먼저 프로젝트를 선택하거나 생성하세요.");
      return;
    }
    if (!service) {
      setAutoError(
        "현재 데스크톱 브릿지에는 AI 선라벨 기능이 연결되지 않았습니다. 프로그램을 다시 시작해 주세요.",
      );
      return;
    }
    setAutoBusy("compile");
    setAutoError(null);
    setAutoRunResult(null);
    setAutoConfirming(false);
    setAutoRunConsent(false);
    try {
      const request = buildAutoLabelingCompileRequest({
        projectId: activeProjectId,
        command: autoCommand,
        issueType: autoIssueType,
      });
      const response = await service.compile(request);
      const parsed = parseAutoLabelingCompileResponse(response, {
        projectId: request.projectId,
        command: request.command,
        issueType: request.issueType,
      });
      setAutoCompileResult(parsed);
      if (
        requiresMandatoryReview(
          parsed.recipe.issueType,
          parsed.capabilities,
        )
      ) {
        setAutoMode("proposal_only");
      }
      if (!activeAnalysisForProject) setAutoScope("project");
    } catch (error) {
      setAutoCompileResult(null);
      setAutoError(
        error instanceof Error
          ? error.message
          : "AI 선라벨 명령을 안전하게 해석하지 못했습니다.",
      );
    } finally {
      setAutoBusy(null);
    }
  }

  async function runAutoLabeling() {
    const service = window.trafficSuperVision?.autoLabeling;
    if (!activeProjectId || !autoCompileResult) {
      setAutoError("명령 해석 결과를 먼저 확인하세요.");
      return;
    }
    if (!autoRunConsent) {
      setAutoError("실행 범위와 검토 원칙을 확인해 주세요.");
      return;
    }
    if (!service) {
      setAutoError(
        "현재 데스크톱 브릿지에는 AI 선라벨 기능이 연결되지 않았습니다. 프로그램을 다시 시작해 주세요.",
      );
      return;
    }
    setAutoBusy("run");
    setAutoError(null);
    setAutoRunResult(null);
    try {
      const request = buildAutoLabelingRunRequest({
        projectId: activeProjectId,
        jobId: activeAnalysisForProject?.jobId,
        recipeId: autoCompileResult.recipe.recipeId,
        command: autoCompileResult.recipe.command,
        issueType: autoCompileResult.recipe.issueType,
        scope: effectiveAutoScope,
        mode: autoMandatoryReview ? "proposal_only" : autoMode,
      });
      const response = await service.run(request);
      const parsed = parseAutoLabelingRunResponse(response, {
        projectId: request.projectId,
        recipeId: request.recipeId,
        scope: request.scope,
        modeRequested: request.mode,
      });
      setAutoRunResult(parsed);
      if (parsed.proposalGenerationStatus !== "unavailable") {
        setProposalRefreshRevision((current) => current + 1);
      }
      setAutoConfirming(false);
      setAutoRunConsent(false);
    } catch (error) {
      setAutoError(
        error instanceof Error
          ? error.message
          : "AI 선라벨 실행 결과를 안전하게 확인하지 못했습니다.",
      );
    } finally {
      setAutoBusy(null);
    }
  }

  function openFirstAutoLabelMatch() {
    const match = autoRunResult?.firstMatch;
    if (!match || !activeProjectId) return;
    saveSelectedCandidateId(
      activeProjectId,
      match.analysisJobId,
      match.candidateId,
    );
    if (runtime.analysisSummary?.jobId !== match.analysisJobId) {
      selectCompletedJob(match.analysisJobId);
    } else {
      const nextIndex = questions.findIndex((item) =>
        item.id.endsWith(`:${match.candidateId}`),
      );
      if (nextIndex < 0) {
        setAutoError("선라벨 후보가 현재 질문 큐에 없습니다.");
        return;
      }
      setQuestionIndex(nextIndex);
      setTypeCode(
        questions[nextIndex]?.typeCode ?? "UNCLASSIFIED_ROAD_EVENT",
      );
      setPoint(questionPoint(questions, nextIndex));
      setQueueComplete(false);
    }
    setAutoPanelOpen(false);
  }

  const jobSwitcher =
    completedJobs.length > 0 ? (
      <section className="label-queue-controls" aria-label="검토 영상 선택">
        <label className="compact-field label-job-field">
          <span>검토할 분석 영상</span>
          <select
            value={runtime.analysisSummary?.jobId ?? completedJobs[0].job_id}
            onChange={(event) => selectCompletedJob(event.target.value)}
            disabled={loadingJobs}
          >
            {completedJobs.map((job) => (
              <option key={job.job_id} value={job.job_id}>
                {job.file_name} · 후보 {job.result?.candidate_count ?? 0}건
                {job.job_id in proposalCountsByJobId
                  ? ` · AI ${proposalCountsByJobId[job.job_id]}건`
                  : ""}
              </option>
            ))}
          </select>
        </label>
        <div className="label-job-actions">
          <span>
            {currentJobIndex >= 0
              ? `${currentJobIndex + 1} / ${completedJobs.length}번째 영상`
              : `${completedJobs.length}개 분석 영상`}
          </span>
          <Button
            variant="ghost"
            disabled={currentJobIndex <= 0}
            onClick={() =>
              selectCompletedJob(completedJobs[currentJobIndex - 1].job_id)
            }
          >
            더 최신 영상
          </Button>
          <Button
            variant="ghost"
            disabled={
              currentJobIndex < 0 ||
              currentJobIndex >= completedJobs.length - 1
            }
            onClick={() =>
              selectCompletedJob(completedJobs[currentJobIndex + 1].job_id)
            }
          >
            더 이전 영상
          </Button>
          <Button
            variant="ghost"
            disabled={loadingProposalCounts || proposalJobCount === 0}
            onClick={selectNextProposalJob}
          >
            다음 AI 제안 영상
          </Button>
        </div>
        <div className="label-job-status" aria-live="polite">
          <Badge tone={proposalOverlays.ignoredCount ? "warning" : "neutral"}>
            현재 영상 AI 제안{" "}
            {Object.keys(proposalOverlays.byCandidateId).length}건
          </Badge>
          {temporalReview.jobId === runtime.analysisSummary?.jobId &&
          temporalReview.status === "ready" ? (
            <Badge tone="success">
              사건 병합{" "}
              {Object.keys(
                temporalReview.groupsByRepresentativeId,
              ).length}
              건
            </Badge>
          ) : temporalReview.status === "loading" ? (
            <Badge tone="neutral">프레임 사건 병합 중</Badge>
          ) : null}
          {jobListError ? (
            <span className="inline-error" role="alert">
              {jobListError}
            </span>
          ) : (
            <span>
              {loadingProposalCounts
                ? `${completedJobs.length || "저장된"}개 영상의 AI 제안 수를 확인하는 중입니다.`
                : `AI 제안 영상 ${proposalJobCount} / ${completedJobs.length}개 · 원본 후보와 답변은 영상별로 분리 저장됩니다.`}
            </span>
          )}
        </div>
      </section>
    ) : null;

  const autoLabelingPanel = (
    <section
      className={`auto-label-command${autoPanelOpen ? " auto-label-command--open" : ""}`}
      aria-label="AI 선라벨 명령"
      aria-busy={autoBusy === "run"}
    >
      <button
        type="button"
        className="auto-label-command__toggle"
        aria-expanded={autoPanelOpen}
        aria-controls="auto-label-command-body"
        onClick={() => setAutoPanelOpen((open) => !open)}
      >
        <span className="auto-label-command__identity">
          <Sparkles size={17} aria-hidden="true" />
          <span>
            <strong>AI 선라벨 명령</strong>
            <small>자연어로 후보를 만들고 질문 큐에서 확정</small>
          </span>
        </span>
        <span className="auto-label-command__summary">
          <Badge tone="success">검토 전용 기본값</Badge>
          {autoCompileResult ? (
            <Badge tone={autoMandatoryReview ? "warning" : "accent"}>
              {autoCompileResult.recipe.displayNameKo}
            </Badge>
          ) : null}
          <ChevronDown size={17} aria-hidden="true" />
        </span>
      </button>

      {autoPanelOpen ? (
        <div
          id="auto-label-command-body"
          className="auto-label-command__body"
        >
          <div className="auto-label-command__guardrail" role="note">
            <ShieldCheck size={18} aria-hidden="true" />
            <div>
              <strong>
                AI 제안은 사용자 확정 라벨이 아니며 학습에 즉시 포함되지 않습니다.
              </strong>
              <span>
                낙하물·이정표/터널 OCR은 항상 검토하며, 긴급도는
                사용자가 질문 카드에서 확정합니다.
              </span>
            </div>
          </div>

          <div className="auto-label-command__form">
            <label className="compact-field auto-label-command__prompt">
              <span>찾을 문제를 자연어로 입력</span>
              <textarea
                value={autoCommand}
                rows={2}
                maxLength={500}
                placeholder="예: 표지판을 가린 수풀을 찾아줘"
                disabled={autoBusy !== null}
                onChange={(event) => {
                  setAutoCommand(event.target.value);
                  invalidateAutoRecipe();
                }}
              />
            </label>
            <label className="compact-field">
              <span>문제 유형</span>
              <select
                value={autoIssueType}
                disabled={autoBusy !== null}
                onChange={(event) => {
                  const value = event.target
                    .value as AutoLabelingIssueSelection;
                  setAutoIssueType(value);
                  if (
                    value !== "auto" &&
                    requiresMandatoryReview(value)
                  ) {
                    setAutoMode("proposal_only");
                  }
                  invalidateAutoRecipe();
                }}
              >
                {AUTO_LABELING_ISSUE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="compact-field">
              <span>실행 범위</span>
              <select
                value={
                  effectiveAutoScope
                }
                disabled={autoBusy !== null}
                onChange={(event) => {
                  setAutoScope(event.target.value as AutoLabelingScope);
                  setAutoRunResult(null);
                  setAutoConfirming(false);
                  setAutoRunConsent(false);
                }}
              >
                <option
                  value="current_job"
                  disabled={!activeAnalysisForProject}
                >
                  현재 분석 영상
                </option>
                <option value="project">프로젝트 전체 영상</option>
              </select>
            </label>
            <label className="compact-field">
              <span>제안 처리</span>
              <select
                value={autoMandatoryReview ? "proposal_only" : autoMode}
                disabled={autoBusy !== null}
                onChange={(event) => {
                  setAutoMode(event.target.value as AutoLabelingMode);
                  setAutoRunResult(null);
                  setAutoConfirming(false);
                  setAutoRunConsent(false);
                }}
              >
                <option value="proposal_only">검토 전용 제안</option>
                <option
                  value="assisted_auto"
                  disabled={autoMandatoryReview}
                >
                  고신뢰 후보 우선 검토
                </option>
              </select>
            </label>
            <Button
              type="button"
              variant="primary"
              className="auto-label-command__compile"
              disabled={autoBusy !== null || autoCommand.trim().length < 3}
              onClick={() => void compileAutoLabeling()}
            >
              {autoBusy === "compile" ? (
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
              ) : (
                <Sparkles size={16} aria-hidden="true" />
              )}
              명령 해석
            </Button>
          </div>

          <div className="auto-label-command__outcome">
            {autoCompileResult ? (
              <section
                className="auto-label-command__preview"
                aria-label="컴파일된 AI 선라벨 실행 계획"
              >
              <div className="auto-label-command__preview-head">
                <div>
                  <Badge tone={autoMandatoryReview ? "warning" : "accent"}>
                    {autoCompileResult.recipe.displayNameKo}
                  </Badge>
                  <span className="mono">
                    recipe {autoCompileResult.recipe.recipeId.slice(0, 12)}…
                  </span>
                </div>
                <span>
                  자동 판독 결과:{" "}
                  <strong>{autoCompileResult.recipe.issueType}</strong>
                </span>
              </div>
              <div
                className="auto-label-command__capabilities"
                aria-label="필요한 AI 판독 기능"
              >
                {autoCompileResult.capabilities.objectDetection ? (
                  <span>객체 검지</span>
                ) : null}
                {autoCompileResult.capabilities.segmentationRequired ? (
                  <span>영역 분할</span>
                ) : null}
                {autoCompileResult.capabilities.ocrRequired ? (
                  <span>OCR 필수 검토</span>
                ) : null}
                {autoCompileResult.capabilities
                  .relationshipReasoningRequired ? (
                  <span>가림 관계 판정</span>
                ) : null}
                {autoCompileResult.capabilities
                  .maintenanceJudgmentRequired ? (
                  <span>유지보수 판정</span>
                ) : null}
              </div>
              <details>
                <summary>
                  탐색 기준 보기 · 양성{" "}
                  {autoCompileResult.recipe.positivePrompts.length}개 / 제외{" "}
                  {autoCompileResult.recipe.negativePrompts.length}개
                </summary>
                <div className="auto-label-command__prompts">
                  <div>
                    <strong>찾음</strong>
                    <ul>
                      {autoCompileResult.recipe.positivePrompts.map(
                        (prompt) => (
                          <li key={prompt}>{prompt}</li>
                        ),
                      )}
                    </ul>
                  </div>
                  <div>
                    <strong>제외</strong>
                    <ul>
                      {autoCompileResult.recipe.negativePrompts.map(
                        (prompt) => (
                          <li key={prompt}>{prompt}</li>
                        ),
                      )}
                    </ul>
                  </div>
                </div>
              </details>
              {autoCompileResult.warnings.length ? (
                <ul className="auto-label-command__warnings">
                  {autoCompileResult.warnings.map((warning) => (
                    <li key={warning}>
                      <AlertTriangle size={14} aria-hidden="true" />
                      {warning}
                    </li>
                  ))}
                </ul>
              ) : null}

              <div className="auto-label-command__run">
                {autoMandatoryReview ? (
                  <span className="auto-label-command__mandatory">
                    <ShieldCheck size={15} aria-hidden="true" />
                    이 유형은 자동 확정할 수 없으며 전부 질문 큐로 이동합니다.
                  </span>
                ) : null}
                {!autoConfirming ? (
                  <Button
                    type="button"
                    disabled={autoBusy !== null}
                    onClick={() => {
                      setAutoConfirming(true);
                      setAutoRunConsent(false);
                      setAutoError(null);
                    }}
                  >
                    <Play size={15} aria-hidden="true" />
                    실행 확인
                  </Button>
                ) : (
                  <div
                    className="auto-label-command__confirmation"
                    role="group"
                    aria-label="AI 선라벨 실행 최종 확인"
                  >
                    <label>
                      <input
                        type="checkbox"
                        checked={autoRunConsent}
                        onChange={(event) =>
                          setAutoRunConsent(event.target.checked)
                        }
                      />
                      <span>
                        {effectiveAutoScope === "current_job"
                          ? "현재 분석 영상"
                          : "프로젝트 전체 영상"}
                        에서{" "}
                        {autoMandatoryReview ||
                        autoMode === "proposal_only"
                          ? "검토 전용 제안"
                          : "고신뢰 후보 우선 검토"}
                        로 로컬 AI 새 검지 또는 검증된 저장 제안 재사용을
                        실행합니다. 결과는 모두 질문 카드이며 확정 라벨이
                        아닙니다.
                      </span>
                    </label>
                    <div>
                      <Button
                        type="button"
                        variant="ghost"
                        disabled={autoBusy !== null}
                        onClick={() => {
                          setAutoConfirming(false);
                          setAutoRunConsent(false);
                        }}
                      >
                        취소
                      </Button>
                      <Button
                        type="button"
                        variant="primary"
                        disabled={!autoRunConsent || autoBusy !== null}
                        onClick={() => void runAutoLabeling()}
                      >
                        {autoBusy === "run" ? (
                          <LoaderCircle
                            className="spin"
                            size={16}
                            aria-hidden="true"
                          />
                        ) : (
                          <Play size={15} aria-hidden="true" />
                        )}
                        확인하고 후보 생성
                      </Button>
                    </div>
                  </div>
                )}
              </div>
              </section>
            ) : null}

            {autoBusy === "run" ? (
              <section
                className="auto-label-command__progress"
                role="status"
                aria-live="polite"
                aria-label="AI 검지 후보 생성 진행 상태"
              >
                <LoaderCircle className="spin" size={17} aria-hidden="true" />
                <div>
                  <strong>검지 후보를 준비하고 있습니다.</strong>
                  <span>
                    고정된 로컬 AI 실행기를 먼저 확인하고, 사용할 수 없으면
                    검증된 저장 제안만 확인합니다. 이 단계에서는 라벨을
                    만들지 않습니다.
                  </span>
                </div>
              </section>
            ) : null}

            {autoRunResult ? (
              <section
                className="auto-label-command__result"
                aria-live="polite"
              >
              {autoRunResult.modeRequested === "assisted_auto" &&
              autoRunResult.modeApplied === "proposal_only" ? (
                <p className="auto-label-command__downgrade">
                  <AlertTriangle size={15} aria-hidden="true" />
                  고신뢰 우선 검토 조건이 충족되지 않아 모든 결과를 일반
                  검토 순서로 배치했습니다.
                </p>
              ) : null}
              <div>
                <strong>
                  {autoRunResult.status === "completed"
                    ? autoRunResult.proposalGenerationStatus === "generated"
                      ? "로컬 AI 신규 검지 후보 준비"
                      : "저장된 검증 제안 불러옴"
                    : autoRunResult.status === "no_candidates"
                      ? autoRunResult.proposalGenerationStatus === "generated"
                        ? "로컬 AI 검지 결과 후보 없음"
                        : "저장 제안 중 조건 일치 없음"
                      : "실행 가능한 AI 모델 없음"}
                </strong>
                <span>
                  {autoRunResult.messageKo} · 제안 출처{" "}
                  {autoRunResult.proposalOrigin === "local_model_generated"
                    ? "이번 실행의 로컬 AI 검지"
                    : autoRunResult.proposalOrigin === "cached_existing"
                      ? "무결성 검증된 기존 제안"
                      : "없음"}
                  · 검토 방식{" "}
                  {autoRunResult.modeApplied === "assisted_auto"
                    ? "고신뢰 우선 검토"
                    : "검토 전용"}
                  · AI 우선 후보 {autoRunResult.autoAcceptedCount}건 · 자동 생성
                  라벨 {autoRunResult.trainingLabelsCreated}건
                </span>
                {autoRunResult.firstMatch ? (
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={openFirstAutoLabelMatch}
                  >
                    <Crosshair size={14} aria-hidden="true" />
                    첫 결과 보기
                  </Button>
                ) : null}
              </div>
              <dl>
                <div>
                  <dt>검사 영상</dt>
                  <dd>{autoRunResult.jobsScanned}</dd>
                </div>
                <div>
                  <dt>평가 후보</dt>
                  <dd>{autoRunResult.candidatesEvaluated}</dd>
                </div>
                <div>
                  <dt>선라벨 제안</dt>
                  <dd>{autoRunResult.proposedCount}</dd>
                </div>
                <div>
                  <dt>사용자 검토</dt>
                  <dd>{autoRunResult.reviewRequiredCount}</dd>
                </div>
              </dl>
              </section>
            ) : null}

            {autoError ? (
              <p className="auto-label-command__error" role="alert">
                <AlertTriangle size={15} aria-hidden="true" />
                {autoError}
              </p>
            ) : null}
          </div>
        </div>
      ) : null}
    </section>
  );

  if (!question) {
    return (
      <div className="screen screen--labeling">
        <header className="screen-header">
          <div>
            <p className="eyebrow">HUMAN-IN-THE-LOOP</p>
            <h1>질문형 라벨링</h1>
            <p>실제 분석 작업에서 생성된 후보만 프로젝트 라벨로 저장합니다.</p>
          </div>
        </header>
        {jobSwitcher}
        <GlassPanel className="label-empty-state">
          <img
            src={realTunnelEvidenceImage}
            alt="업로드 영상에서 추출한 참고용 터널 진입 프레임"
          />
          <div>
            <Badge tone="neutral">참고 이미지 · 라벨 큐 아님</Badge>
            <h2>검토할 실제 분석 후보가 없습니다.</h2>
            <p>
              영상 가져오기에서 입력을 검증한 뒤 분석 화면에서 후보 추출을
              완료하세요. 이 참고 이미지는 답변이나 학습 라벨로 저장되지 않습니다.
            </p>
            <div className="label-empty-steps" aria-label="라벨 큐 생성 순서">
              <span>1. 영상 검증</span>
              <span>2. 후보 추출</span>
              <span>3. 질문 검토</span>
            </div>
            <div className="label-empty-actions">
              <Button variant="primary" onClick={() => onNavigate("import")}>
                영상 가져오기
              </Button>
              <Button onClick={() => onNavigate("analysis")}>
                저장된 분석 열기
              </Button>
            </div>
          </div>
        </GlassPanel>
        {autoLabelingPanel}
      </div>
    );
  }

  if (queueComplete) {
    const pipelineReviewLocked = Boolean(
      currentPipelineJob?.reviewCompletionId &&
        ["queued", "running", "completed"].includes(
          currentPipelineJob.status,
        ),
    );
    return (
      <div className="screen screen--labeling">
        <header className="screen-header">
          <div>
            <p className="eyebrow">HUMAN-IN-THE-LOOP</p>
            <h1>질문형 라벨링</h1>
            <p>현재 프로젝트의 질문 큐를 모두 검토했습니다.</p>
          </div>
        </header>
        {!currentPipelineJob ? jobSwitcher : null}
        {!currentPipelineJob ? autoLabelingPanel : null}
        <GlassPanel className="labeling-complete" role="status">
          <Check size={38} />
          <Badge tone="success">로컬 검토 기록 저장됨</Badge>
          <h2>{questions.length}개 질문 검토 완료</h2>
          <p>
            승인·거절·보류·수정 결과가 이 기기의 프로젝트 검토 기록에 보존됐습니다.
            모델 재학습은 자동으로 시작되지 않습니다.
          </p>
          {currentPipelineJob?.status === "completed" ? (
            <p className="labeling-complete__pipeline">
              지도·확정 CSV·증거 묶음·HWPX 편집 미리보기가 같은 PipelineJob
              provenance로 갱신됐습니다.
            </p>
          ) : currentPipelineJob &&
            (pipelineCompletionState === "submitting" ||
              pipelineCompletionState === "queued" ||
              ["queued", "running"].includes(currentPipelineJob.status)) ? (
            <p className="labeling-complete__pipeline">
              답변 원장을 서버에서 검증했습니다. 지도·CSV·증거·보고서 초안을 자동
              갱신하고 있습니다.
            </p>
          ) : pipelineCompletionState === "failed" ? (
            <div className="inline-error labeling-complete__pipeline" role="alert">
              <AlertTriangle size={17} aria-hidden="true" />
              <span>
                {pipelineCompletionError ??
                  "자동 결과 갱신을 시작하지 못했습니다."}
              </span>
              <Button
                onClick={() => {
                  pipelineCompletionAttemptRef.current = null;
                  void requestPipelineReviewCompletion(history);
                }}
              >
                결과 갱신 재시도
              </Button>
            </div>
          ) : pipelineCompletionState === "not_applicable" ? (
            <p className="labeling-complete__pipeline">
              이 검토는 이전 분석 기록을 수동으로 연 것이므로 자동 PipelineJob
              결과에는 연결하지 않았습니다.
            </p>
          ) : null}
          <div>
            <Button
              onClick={undo}
              disabled={pipelineReviewLocked}
              title={
                pipelineReviewLocked
                  ? "자동 결과 갱신이 시작된 뒤에는 같은 PipelineJob의 답변을 되돌릴 수 없습니다."
                  : undefined
              }
            >
              <RotateCcw size={17} /> 마지막 답변 다시 검토
            </Button>
            <Button
              variant="ghost"
              disabled={pipelineReviewLocked}
              title="기존 감사 기록은 삭제되지 않으며, 다시 답하면 새 리비전으로 저장됩니다."
              onClick={() => {
                setHistory([]);
                setQuestionIndex(0);
                setTypeCode(questions[0].typeCode);
                setPoint(questionPoint(questions, 0));
                setQueueComplete(false);
              }}
            >
              모든 질문 새 검토 회차
            </Button>
          </div>
        </GlassPanel>
      </div>
    );
  }

  return (
    <div className="screen screen--labeling">
      <header className="screen-header">
        <div>
          <p className="eyebrow">HUMAN-IN-THE-LOOP</p>
          <h1>질문형 라벨링</h1>
          <p>AI 후보를 승인·거절·보류하거나 한 번의 클릭으로 영역을 수정합니다.</p>
        </div>
        <div className="policy-toggle review-mode-toggle" role="group" aria-label="라벨링 판정 방식">
          {REVIEW_MODES.map((mode) => (
            <button
              type="button"
              key={mode.id}
              className={reviewMode === mode.id ? "active" : ""}
              aria-pressed={reviewMode === mode.id}
              disabled={Boolean(currentPipelineJob)}
              title={
                currentPipelineJob
                  ? "이 PipelineJob을 시작할 때 확정한 프로젝트 판정 방식입니다."
                  : mode.description
              }
              onClick={() => selectReviewMode(mode.id)}
            >
              <strong>{mode.label}</strong>
              <small>
                {mode.id === "precision" ? "심각도 포함" : "심각도 없음"}
                {currentPipelineJob && reviewMode === mode.id
                  ? " · 프로젝트 설정"
                  : ""}
              </small>
            </button>
          ))}
        </div>
      </header>

      {jobSwitcher}
      {autoLabelingPanel}

      <div className="labeling-layout">
        <GlassPanel className="evidence-workbench">
          <div className="workbench-toolbar">
            <div>
              <Badge tone={mandatoryQuestion ? "danger" : "accent"}>
                {mandatoryQuestion
                  ? "필수 질문"
                  : question.experimental
                    ? "실험적 AI 제안"
                    : "실제 분석 후보"}
              </Badge>
              <span className="mono">
                {question.id} · {question.timecode ?? "시각 없음"} ·{" "}
                {question.proposalOrigin === "review_sidecar"
                  ? "검토 큐 실험 제안"
                  : question.proposalOrigin === "analysis_record"
                    ? "분석 기록 모델 제안"
                    : "장면·선명도 추출"}
                {question.temporalGroup
                  ? ` · 같은 사건 ${question.temporalGroup.detectionCount}프레임`
                  : ""}
              </span>
            </div>
            <div>
              <IconButton
                label={zoomed ? "원래 크기로 보기" : "확대"}
                aria-pressed={zoomed}
                onClick={() => setZoomed((value) => !value)}
              >
                <ZoomIn size={18} />
              </IconButton>
              <IconButton label="이전 답변 취소" onClick={undo} disabled={history.length === 0}>
                <RotateCcw size={18} />
              </IconButton>
            </div>
          </div>

          <div
            className="candidate-media-stack"
            aria-label="현재 후보 근거 미디어"
          >
            <div
              className={`evidence-image ${editMode ? "evidence-image--editing" : ""} ${
                zoomed ? "evidence-image--zoomed" : ""
              }`}
              role={editMode ? "group" : undefined}
              tabIndex={editMode ? 0 : undefined}
              aria-label={
                editMode
                  ? "SAM2 영역 수정 캔버스. 실제 대상 내부를 한 번 클릭하면 새 마스크를 요청합니다."
                  : undefined
              }
              onClick={(event) => {
                if (!editMode || maskRefinementBusy) return;
                const rect = event.currentTarget.getBoundingClientRect();
                void requestMaskRefinement(
                  (event.clientX - rect.left) / rect.width,
                  (event.clientY - rect.top) / rect.height,
                );
              }}
            >
              <img
                src={currentEvidenceImage}
                alt={`분석 후보 ${question.id}의 대표 근거 프레임`}
              />
              {visibleBox ? (
                <button
                  type="button"
                  className="detection-box"
                  aria-label={`표시된 ${typeCode} 영역`}
                  style={{
                    left: `${Math.max(2, point.x - visibleBox.width / 2)}%`,
                    top: `${Math.max(2, point.y - visibleBox.height / 2)}%`,
                    width: `${visibleBox.width}%`,
                    height: `${visibleBox.height}%`,
                  }}
                  onClick={(event) => {
                    event.stopPropagation();
                    beginMaskEdit();
                  }}
                >
                  <span>
                    {typeCode}
                    {question.confidence == null
                      ? ""
                      : ` · ${question.confidence.toFixed(2)}`}
                  </span>
                </button>
              ) : null}
              {maskRefinement ? (
                <svg
                  className="sam2-mask-overlay"
                  viewBox="0 0 100 100"
                  preserveAspectRatio="none"
                  aria-label="SAM2가 새로 제안한 확인 전 마스크"
                >
                  <polygon
                    points={maskRefinement.polygon
                      .map(([x, y]) => `${x * 100},${y * 100}`)
                      .join(" ")}
                  />
                </svg>
              ) : null}
              {editMode && maskRefinementBusy ? (
                <span
                  className="point-prompt"
                  aria-label="SAM2 마스크 계산 기준점"
                  style={{ left: `${point.x}%`, top: `${point.y}%` }}
                >
                  <LoaderCircle className="spin" size={26} />
                </span>
              ) : null}
              <div className="evidence-overlay">
                <span>
                  {maskRefinement
                    ? "SAM2 제안 마스크 · 확인 전"
                    : maskRefinementBusy
                      ? "SAM2 마스크 계산 중"
                      : editMode
                        ? "기존 제안 · 새 대상 클릭"
                        : visibleBox
                          ? question.experimental
                        ? "AI 제안 영역"
                        : "표시 영역"
                          : question.temporalGroup
                            ? "같은 사건의 추가 근거 · 이 프레임 영역 없음"
                            : question.experimental
                              ? "장면 단위 AI 제안 · 영역 없음"
                              : "미분류 장면 후보"}
                </span>
                <span>
                  {currentTemporalEvidence
                    ? `원본 ${currentTemporalEvidence.timecode}`
                    : "원본 근거"}
                </span>
              </div>
              {editMode ? (
                <div className="editing-hint" role="status">
                  {maskRefinementBusy ? (
                    <LoaderCircle className="spin" size={18} />
                  ) : maskRefinement ? (
                    <ShieldCheck size={18} />
                  ) : (
                    <MousePointer2 size={18} />
                  )}
                  {maskRefinementBusy
                    ? "로컬 SAM2 계산 중"
                    : maskRefinement
                      ? "새 마스크 확인 전 · 다시 클릭하면 재생성"
                      : "실제 대상 내부를 한 번 클릭"}
                </div>
              ) : null}
            </div>

            {question.temporalGroup ? (
              <div className="temporal-frame-evidence">
                <div className="temporal-group-summary">
                  <span>
                    같은 사건의 전체 근거{" "}
                    <strong>{question.temporalGroup.detectionCount}프레임</strong>
                    {" · "}
                    {formatTimecode(question.temporalGroup.startSeconds)}–
                    {formatTimecode(question.temporalGroup.endSeconds)}
                  </span>
                  <span className="mono">
                    {frameIndex + 1} / {temporalEvidence.length}
                  </span>
                </div>
                <div className="temporal-frame-strip-shell">
                  <IconButton
                    label="이전 사건 근거"
                    disabled={frameIndex <= 0}
                    onClick={() =>
                      setFrameIndex((current) => Math.max(0, current - 1))
                    }
                  >
                    <ChevronLeft size={17} />
                  </IconButton>
                  <div className="frame-strip" aria-label="같은 사건 전체 근거 프레임">
                    {temporalFrameWindow.map((evidence, windowIndex) => {
                      const evidenceIndex =
                        temporalFrameWindowStart + windowIndex;
                      return (
                        <button
                          type="button"
                          key={evidence.questionId}
                          className={
                            frameIndex === evidenceIndex ? "active" : ""
                          }
                          aria-label={`사건 근거 ${evidenceIndex + 1}, ${evidence.timecode}`}
                          onClick={() => setFrameIndex(evidenceIndex)}
                        >
                          <img src={evidence.evidenceImage} alt="" />
                          <span>
                            {evidenceIndex ===
                            question.temporalGroup?.peakEvidenceIndex
                              ? "대표"
                              : evidenceIndex + 1}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  <IconButton
                    label="다음 사건 근거"
                    disabled={frameIndex >= temporalEvidence.length - 1}
                    onClick={() =>
                      setFrameIndex((current) =>
                        Math.min(temporalEvidence.length - 1, current + 1),
                      )
                    }
                  >
                    <ChevronRight size={17} />
                  </IconButton>
                </div>
              </div>
            ) : question.adjacentFrames?.length ? (
              <div className="frame-strip" aria-label="앞뒤 프레임">
                {[-3, -2, -1, 0, 1, 2, 3].map((offset, index) => (
                  <button
                    type="button"
                    key={offset}
                    className={frameIndex === index ? "active" : ""}
                    aria-label={offset === 0 ? "대표 프레임" : `${offset > 0 ? "+" : ""}${offset} 프레임`}
                    onClick={() => setFrameIndex(index)}
                  >
                    <img
                      src={question.adjacentFrames?.[index] ?? currentEvidenceImage}
                      alt=""
                    />
                    <span>{offset === 0 ? "대표" : `${offset > 0 ? "+" : ""}${offset}`}</span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="candidate-frame-note">
                앞뒤 프레임 묶음은 후보 정밀 재분석 후 추가됩니다. 현재는 대표 프레임과
                실제 미리보기 클립을 검토하세요.
              </div>
            )}
            {question.previewVideo ? (
              <div className="candidate-preview">
                <div>
                  <Badge tone="success">실제 업로드 영상</Badge>
                  <span>{question.sourceFile} · 원본 시각 {question.timecode}</span>
                </div>
                <video
                  key={`${question.id}:${previewRetry}`}
                  controls
                  muted
                  preload="metadata"
                  poster={currentEvidenceImage}
                  src={question.previewVideo}
                  aria-label="현재 질문의 실제 미리보기 클립"
                  onCanPlay={() => setPreviewError(false)}
                  onError={() => setPreviewError(true)}
                />
                {previewError && previewRetry >= 2 ? (
                  <div className="notice notice--warning" role="alert">
                    <CircleHelp size={17} />
                    <div>
                      <strong>미리보기 클립을 불러오지 못했습니다.</strong>
                      <p>브릿지 연결을 확인한 뒤 다시 불러오세요.</p>
                    </div>
                    <Button
                      onClick={() => {
                        setPreviewError(false);
                        setPreviewRetry((value) => value + 1);
                      }}
                    >
                      다시 불러오기
                    </Button>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        </GlassPanel>

        <div className="labeling-sidebar">
          <GlassPanel className="question-card">
            <PanelHeader
              eyebrow="LABEL QUESTION"
              title={`${questionIndex + 1} / ${questions.length}`}
              action={
                <Badge tone={mandatoryQuestion ? "danger" : "neutral"}>
                  {mandatoryQuestion
                    ? "강제"
                    : question.experimental
                      ? "사용자 확인 필요"
                      : "표본"}
                </Badge>
              }
            />
            <h2
              ref={questionHeadingRef}
              className="question-prompt"
              tabIndex={-1}
              title={question.prompt}
            >
              {question.prompt}
            </h2>
            <div className="question-metadata">
              <Badge tone="neutral">
                {ISSUE_TYPE_OPTIONS.find(([value]) => value === typeCode)?.[1] ??
                  typeCode}
              </Badge>
              <span className="mono">{question.scoreText}</span>
              <span>
                판정 ·{" "}
                {reviewMode === "precision"
                  ? "정밀·사용자 심각도"
                  : "빠름·심각도 없음"}
              </span>
              {question.temporalGroup ? (
                <Badge tone="success">
                  {question.temporalGroup.detectionCount}프레임 ·{" "}
                  {question.temporalGroup.durationSeconds.toFixed(1)}초
                </Badge>
              ) : null}
            </div>
            <section
              className={`weak-case-reference weak-case-reference--${weakCaseState.status}`}
              aria-label="3·4분기 유사 참고 사례"
              aria-busy={weakCaseState.status === "loading"}
            >
              <div className="weak-case-reference__header">
                <span>
                  <BookOpenCheck size={15} aria-hidden="true" />
                  <strong>3·4분기 유사 참고</strong>
                  <small>문구·표 위치 기준</small>
                </span>
                <Badge
                  tone={
                    weakCaseState.status === "ready"
                      ? "warning"
                      : "neutral"
                  }
                >
                  {weakCaseState.status === "loading"
                    ? "찾는 중"
                    : weakCaseState.status === "ready"
                      ? `약한 사례 ${weakCaseState.cases.length}건`
                      : "사용 불가"}
                </Badge>
              </div>
              {weakCaseState.status === "loading" ? (
                <div
                  className="weak-case-reference__status"
                  role="status"
                >
                  <LoaderCircle
                    className="spin"
                    size={14}
                    aria-hidden="true"
                  />
                  <span>{weakCaseState.message}</span>
                </div>
              ) : weakCaseState.status === "ready" &&
                weakCaseState.cases.length ? (
                <>
                  <div
                    className="weak-case-strip"
                    role="list"
                    aria-label="문구·표 위치로 검색한 참고 사진"
                  >
                    {weakCaseState.cases.map((reference) => {
                      const imageFailed = failedWeakCaseImages.has(
                        reference.caseId,
                      );
                      const showImage =
                        reference.imageAvailable &&
                        !imageFailed &&
                        Boolean(activeProjectId);
                      return (
                        <button
                          type="button"
                          role="listitem"
                          key={reference.caseId}
                          className={
                            selectedWeakCase?.caseId === reference.caseId
                              ? "active"
                              : ""
                          }
                          aria-pressed={
                            selectedWeakCase?.caseId === reference.caseId
                          }
                          title={
                            reference.opinion ??
                            reference.evaluation ??
                            reference.caption ??
                            `${reference.quarter}분기 ${reference.pageHint}쪽 사례`
                          }
                          onClick={() =>
                            setSelectedWeakCaseId(reference.caseId)
                          }
                        >
                          {showImage && activeProjectId ? (
                            <img
                              src={buildWeakCaseImageUrl(
                                activeProjectId,
                                reference.sourceId,
                                reference.caseId,
                              )}
                              alt=""
                              onError={() =>
                                setFailedWeakCaseImages((current) => {
                                  const next = new Set(current);
                                  next.add(reference.caseId);
                                  return next;
                                })
                              }
                            />
                          ) : (
                            <span className="weak-case-strip__image-fallback">
                              <ImageOff size={15} aria-hidden="true" />
                            </span>
                          )}
                          <span className="weak-case-strip__identity">
                            {reference.quarter}Q · p.
                            {reference.pageHint}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  {selectedWeakCase ? (
                    <div className="weak-case-reference__detail">
                      <span>
                        <strong>
                          {selectedWeakCase.quarter}분기 ·{" "}
                          {selectedWeakCase.pageHint}쪽
                        </strong>
                        <small>
                          표 {selectedWeakCase.tableIndex ?? "—"} · 행{" "}
                          {selectedWeakCase.cellRow ?? "—"} · 열{" "}
                          {selectedWeakCase.cellColumn ?? "—"}
                        </small>
                      </span>
                      <p>
                        {selectedWeakCase.opinion ??
                          selectedWeakCase.evaluation ??
                          selectedWeakCase.caption ??
                          selectedWeakCase.location ??
                          "확인 가능한 주변 문구가 없습니다."}
                      </p>
                      <Badge tone="warning">확정 아님</Badge>
                    </div>
                  ) : null}
                </>
              ) : (
                <div
                  className="weak-case-reference__status"
                  role={
                    weakCaseState.status === "failed"
                      ? "alert"
                      : "status"
                  }
                >
                  <ImageOff size={14} aria-hidden="true" />
                  <span>
                    {weakCaseState.message ??
                      "현재 표시할 3·4분기 참고 사례가 없습니다."}
                  </span>
                </div>
              )}
            </section>
            {temporalReview.warning ? (
              <div className="notice notice--warning proposal-load-warning" role="status">
                <CircleHelp size={17} />
                <div>
                  <strong>기존 질문 순서를 유지합니다.</strong>
                  <p>{temporalReview.warning}</p>
                </div>
              </div>
            ) : null}
            {proposalWarning ? (
              <div className="notice notice--warning proposal-load-warning" role="status">
                <CircleHelp size={17} />
                <div>
                  <strong>일부 실험 제안을 안전하게 제외했습니다.</strong>
                  <p>{proposalWarning}</p>
                </div>
              </div>
            ) : null}
            {question.experimental ? (
              <div className="notice notice--warning experimental-proposal" role="note">
                <CircleHelp size={17} />
                <div>
                  <strong>
                    실험적 AI 제안 · {question.displayName ?? typeCode} 가능성
                  </strong>
                  <p>
                    제안만으로 라벨이 저장되지 않습니다. 사용자가 예·아니오·판단
                    어려움·영역 수정 중 하나를 선택해야 합니다.
                  </p>
                  <dl className="proposal-facts">
                    <div>
                      <dt>AI 원본 제안</dt>
                      <dd>
                        {question.proposalDisplayName ??
                          question.proposalTypeCode}{" "}
                        <span className="mono">{question.proposalTypeCode}</span>
                      </dd>
                    </div>
                    <div>
                      <dt>점수 해석</dt>
                      <dd>{question.scoreText}</dd>
                    </div>
                    <div>
                      <dt>제안 출처</dt>
                      <dd>
                        {question.modelIdentity ?? "모델 정보 없음"}
                        {question.modelRunId
                          ? ` · 실행 ${question.modelRunId.slice(0, 8)}`
                          : ""}
                      </dd>
                    </div>
                    <div>
                      <dt>현재 선택</dt>
                      <dd>
                        {ISSUE_TYPE_OPTIONS.find(
                          ([value]) => value === typeCode,
                        )?.[1] ?? typeCode}
                        {typeCode !== question.proposalTypeCode
                          ? " · AI 제안에서 변경됨"
                          : " · 아직 저장되지 않음"}
                      </dd>
                    </div>
                  </dl>
                </div>
              </div>
            ) : null}
            {answerError ? (
              <div className="notice notice--danger" role="alert">
                <CircleHelp size={17} />
                <div>
                  <strong>라벨 저장을 완료하지 못했습니다.</strong>
                  <p>{answerError}</p>
                </div>
              </div>
            ) : null}

            {question.roiImage ? (
              <figure className="ocr-roi">
                <img src={question.roiImage} alt="AI가 터널명 표지로 제안한 영역 확대" />
                <figcaption>
                  <span>
                    <strong>OCR 확대</strong>
                    <small>{question.ocrCandidate}</small>
                  </span>
                  <Badge tone="warning">저신뢰</Badge>
                </figcaption>
                <p>{question.locationStatus}</p>
              </figure>
            ) : null}

            <label className="compact-field">
              <span>
                {question.experimental
                  ? "AI 유형이 틀렸다면 변경"
                  : "실제 도로 문제 유형 선택"}
              </span>
              <select value={typeCode} onChange={(event) => setTypeCode(event.target.value)}>
                {ISSUE_TYPE_OPTIONS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>

            {typeCode === "TUNNEL_IDENTITY" ||
            typeCode === "GUIDE_SIGN_IDENTITY" ? (
              <label className="compact-field">
                <span>직접 읽은 이정표·터널명 · 읽을 수 있을 때만 입력</span>
                <input
                  value={ocrText}
                  onChange={(event) => setOcrText(event.target.value)}
                  placeholder="예: ○○터널"
                  maxLength={80}
                />
              </label>
            ) : null}

            {followup ? (
              <div className="followup-card severity-followup" aria-live="polite">
                <div className="severity-followup__intro">
                  <span>사용자 최종 심각도</span>
                  <strong>
                    AI 추천 미제공 · 검지 신뢰도와 심각도는 별개
                  </strong>
                  <small>
                    선택 즉시 사용자 확정값으로 저장하고 다음 카드로 이동합니다.
                  </small>
                </div>
                <div className="severity-choice-grid" role="group" aria-label="사용자 최종 심각도">
                  {USER_SEVERITIES.map((choice) => (
                    <button
                      type="button"
                      key={choice.value}
                      className={`severity-choice severity-choice--${choice.key}`}
                      onClick={() => void confirmSeverity(choice.value)}
                      disabled={savingAnswer}
                    >
                      <kbd>{choice.key}</kbd>
                      <span>
                        <strong>{choice.value}</strong>
                        <small>{choice.description}</small>
                      </span>
                    </button>
                  ))}
                </div>
                <p className="severity-followup__action">
                  권장 조치 · <strong>{maintenanceActionForType(typeCode)}</strong>
                </p>
              </div>
            ) : (
              <div className="answer-grid">
                <button
                  type="button"
                  className="answer answer--yes"
                  onClick={() => submit("yes")}
                  disabled={
                    savingAnswer ||
                    typeCode === "UNCLASSIFIED_ROAD_EVENT" ||
                    ((typeCode === "TUNNEL_IDENTITY" ||
                      typeCode === "GUIDE_SIGN_IDENTITY") &&
                      !ocrText.trim())
                  }
                  title={
                    typeCode === "UNCLASSIFIED_ROAD_EVENT"
                      ? "예를 선택하려면 실제 도로 문제 유형을 먼저 고르세요."
                      : (typeCode === "TUNNEL_IDENTITY" ||
                            typeCode === "GUIDE_SIGN_IDENTITY") &&
                          !ocrText.trim()
                        ? "예를 선택하려면 판독한 글자를 입력하세요."
                        : undefined
                  }
                >
                  <Check size={20} />
                  <span>
                    <strong>예 <kbd>Y</kbd></strong>
                    <small>
                      {reviewMode === "precision"
                        ? "심각도 선택으로 이동"
                        : currentBox
                          ? "표시 영역·유형 즉시 저장"
                          : "선택 유형 즉시 저장"}
                    </small>
                  </span>
                </button>
                <button
                  type="button"
                  className="answer answer--no"
                  onClick={() => submit("no")}
                  disabled={savingAnswer}
                >
                  <X size={20} />
                  <span>
                    <strong>아니오 <kbd>N</kbd></strong>
                    <small>어려운 음성 예제</small>
                  </span>
                </button>
                <button
                  type="button"
                  className="answer answer--unsure"
                  onClick={() => submit("unsure")}
                  disabled={savingAnswer}
                >
                  <CircleHelp size={20} />
                  <span>
                    <strong>판단 어려움 <kbd>U</kbd></strong>
                    <small>학습 제외·보류 큐</small>
                  </span>
                </button>
                <button
                  type="button"
                  className="answer answer--edit"
                  onClick={beginMaskEdit}
                  disabled={
                    savingAnswer ||
                    maskRefinementState.status !== "ready"
                  }
                  title={
                    maskRefinementState.status === "ready"
                      ? "대표 프레임을 한 번 클릭해 SAM2 새 마스크를 요청합니다."
                      : maskRefinementState.message
                  }
                >
                  <Crosshair size={20} />
                  <span>
                    <strong>영역 수정 <kbd>E</kbd></strong>
                    <small>
                      {maskRefinementState.status === "ready"
                        ? "한 번 클릭 → SAM2 마스크"
                        : maskRefinementState.status === "loading" ||
                            maskRefinementState.status === "idle"
                          ? "SAM2 상태 확인 중"
                          : "SAM2 모델팩 필요"}
                    </small>
                  </span>
                </button>
              </div>
            )}

            {editMode ? (
              <div className="edit-actions">
                <Button onClick={cancelMaskEdit}>
                  <ChevronLeft size={16} /> 취소
                </Button>
                <Button
                  variant="primary"
                  onClick={() => submit("edited")}
                  disabled={
                    savingAnswer ||
                    maskRefinementBusy ||
                    !maskRefinement ||
                    typeCode === "UNCLASSIFIED_ROAD_EVENT"
                  }
                  title={
                    typeCode === "UNCLASSIFIED_ROAD_EVENT"
                      ? "수정 영역을 저장하려면 실제 문제 유형을 먼저 고르세요."
                      : !maskRefinement
                        ? "먼저 대상 내부를 한 번 클릭해 SAM2 마스크를 받으세요."
                        : "이 확인 뒤 빠른 판정은 저장되고, 정밀 판정은 심각도 선택으로 이동합니다."
                  }
                >
                  {maskRefinementBusy
                    ? "마스크 계산 중"
                    : "새 마스크 확인"}
                </Button>
              </div>
            ) : null}

            <div className="explanation">
              <CircleHelp size={18} />
              <p>{stageText}</p>
            </div>

            <Button
              variant="ghost"
              className="full-width"
              onClick={() => submit("skipped")}
              disabled={mandatoryQuestion || savingAnswer}
              title={mandatoryQuestion ? "긴급·낙하물 질문은 건너뛸 수 없습니다." : undefined}
            >
              <SkipForward size={17} />
              {mandatoryQuestion ? "필수 질문 · 답변 필요" : "이번 질문 건너뛰기 · 학습 제외"}
            </Button>
          </GlassPanel>

          <GlassPanel className="label-progress">
            <PanelHeader eyebrow="QUALITY GATE" title="유형별 초기 30건" />
            <div className="progress-summary">
              <span>분류 모델 일치율</span>
              <strong>미산출</strong>
            </div>
            <p className="quality-gate-note">
              현재 후보 추출기는 유형을 예측하지 않으므로 사용자·AI 일치율을
              계산하지 않습니다.
            </p>
            <ProgressBar value={progress} label={`전체 질문 진행률 ${progress}%`} />
            {LABEL_PROGRESS_TYPES.map(([label, typeCodeValue]) => {
              const value = classCounts.get(typeCodeValue) ?? 0;
              const tone = value >= 30 ? "success" : "warning";
              return (
                <div className="class-progress" key={label}>
                  <span>{label}</span>
                  <strong>{value} / 30</strong>
                  <ProgressBar
                    value={(value / 30) * 100}
                    tone={tone}
                    label={`${label} ${value}건`}
                  />
                </div>
              );
            })}
          </GlassPanel>
        </div>
      </div>
    </div>
  );
}
