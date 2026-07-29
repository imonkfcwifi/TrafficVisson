import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronLeft,
  CircleHelp,
  Crosshair,
  LoaderCircle,
  MousePointer2,
  Play,
  RotateCcw,
  ShieldCheck,
  SkipForward,
  Sparkles,
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
import { buildCandidateEvidenceUrl } from "../evidence";
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
  restoreCompletedAnalysis,
  useRuntimeState,
  type AnalysisSummary,
} from "../runtimeState";
import { useProjectCatalog } from "../projectCatalog";
import {
  Badge,
  Button,
  GlassPanel,
  IconButton,
  PanelHeader,
  ProgressBar,
} from "../components/ui";

type Answer = ReviewAnswer;
type Policy = "학습 보조" | "전수 검토" | "빠른 검토";

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
  if (value === "긴급") return "urgent";
  if (value === "심각") return "major";
  if (value === "경미") return "minor";
  return "moderate";
}

export function LabelingScreen({
  onNavigate,
}: {
  onNavigate: (screen: ScreenId) => void;
}) {
  const runtime = useRuntimeState();
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
  const [policy] = useState<Policy>("전수 검토");
  const [questionIndex, setQuestionIndex] = useState(initialQuestionIndex);
  const [history, setHistory] = useState<ReviewRecord[]>(restoredHistory);
  const [queueComplete, setQueueComplete] = useState(
    questions.length > 0 && restoredHistory.length >= questions.length,
  );
  const [editMode, setEditMode] = useState(false);
  const [point, setPoint] = useState(() =>
    questionPoint(questions, initialQuestionIndex),
  );
  const [typeCode, setTypeCode] = useState(
    questions[initialQuestionIndex]?.typeCode ?? "UNCLASSIFIED_ROAD_EVENT",
  );
  const [followup, setFollowup] = useState(false);
  const [severity, setSeverity] = useState("보통");
  const [action, setAction] = useState("현장 확인 후 적정 유지보수 조치");
  const [ocrText, setOcrText] = useState("");
  const [frameIndex, setFrameIndex] = useState(3);
  const [zoomed, setZoomed] = useState(false);
  const [positiveAnswer, setPositiveAnswer] = useState<"yes" | "edited">("yes");
  const [savingAnswer, setSavingAnswer] = useState(false);
  const [answerError, setAnswerError] = useState<string | null>(null);
  const [previewRetry, setPreviewRetry] = useState(0);
  const [previewError, setPreviewError] = useState(false);
  const questionHeadingRef = useRef<HTMLHeadingElement>(null);
  const activeProjectId =
    projectCatalog.activeProjectId ?? runtime.analysisSummary?.projectId ?? null;
  const activeAnalysisForProject =
    runtime.analysisSummary?.projectId === activeProjectId
      ? runtime.analysisSummary
      : null;
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
  }, [activeProjectId]);

  const question = questions[questionIndex];
  const mandatoryQuestion = Boolean(
    question?.forced ||
      typeCode === "ROAD_DEBRIS" ||
      (followup && severity === "긴급"),
  );
  const currentEvidenceImage =
    question?.adjacentFrames?.[frameIndex] ??
    question?.evidenceImage ??
    realTunnelEvidenceImage;
  const currentBox = question?.box ??
    (editMode
      ? { x: point.x - 7, y: point.y - 8, width: 14, height: 16 }
      : undefined);
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
    saveReviewHistory(reviewKey, history);
  }, [history, reviewKey]);

  useEffect(() => {
    const bridge = window.trafficVisson;
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
  }, [question?.id]);

  useEffect(() => {
    if (!previewError || previewRetry >= 2) return;
    const timer = window.setTimeout(() => {
      setPreviewError(false);
      setPreviewRetry((value) => value + 1);
    }, 1_200);
    return () => window.clearTimeout(timer);
  }, [previewError, previewRetry]);

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
    if (!summary || !window.trafficVisson) return;
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
    void window.trafficVisson.candidateQuestions
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
        const service = window.trafficVisson?.reviewComputations;
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

  const stageText = useMemo(() => {
    if (editMode) {
      return "대상 중심을 클릭하면 임시 박스를 표시합니다. 자동 분할 모델은 아직 연결되지 않았으므로 저장 전에 영역을 직접 확인하세요.";
    }
    if (followup) return "심각도와 권장 조치를 확인하면 승인 라벨이 완성됩니다.";
    return question?.rationale ?? "분석 후보를 기다리고 있습니다.";
  }, [editMode, followup, question?.rationale]);

  async function appendRecord(
    answer: Answer,
    details: { severity?: string; action?: string } = {},
  ) {
    const trainingStatus =
      answer === "yes" || answer === "edited"
        ? "approved"
        : answer === "no"
          ? "negative"
          : answer === "unsure"
            ? "held"
            : "excluded";
    const positive = answer === "yes" || answer === "edited";
    const geometry = currentBox
      ? {
          x: Math.max(0, point.x - currentBox.width / 2),
          y: Math.max(0, point.y - currentBox.height / 2),
          width: currentBox.width,
          height: currentBox.height,
        }
      : undefined;
    const candidateId = question.actualCandidate
      ? question.id.split(":").at(-1)
      : undefined;
    const record: ReviewRecord = {
      question: questionIndex,
      questionId: question.id,
      candidateId,
      answer,
      selectedType: typeCode,
      finalGeometry: geometry,
      severity: positive ? details.severity ?? severity : undefined,
      action: positive ? details.action ?? action : undefined,
      ocrText: ocrText.trim() || undefined,
      reviewedAt: new Date().toISOString(),
      trainingStatus,
    };
    const summary = runtime.analysisSummary;
    if (
      question.actualCandidate &&
      summary &&
      candidateId &&
      window.trafficVisson
    ) {
      setSavingAnswer(true);
      setAnswerError(null);
      try {
        const response = (await window.trafficVisson.candidateQuestions.answer({
          projectId: summary.projectId,
          jobId: summary.jobId,
          candidateId,
          answer: answer === "edited" ? "edit" : answer,
          selectedIssueType: positive
            ? (apiIssueTypeFromIncident(typeCode) ?? undefined)
            : undefined,
          severity: positive
            ? severityToApi(details.severity ?? severity)
            : undefined,
          maintenanceAction: positive
            ? details.action ?? action
            : undefined,
          ocrText: positive ? ocrText.trim() || undefined : undefined,
          normalizedBox:
            positive && geometry
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
        return false;
      } finally {
        setSavingAnswer(false);
      }
    }
    setHistory((current) =>
      [
        ...current.filter((item) => item.questionId !== record.questionId),
        record,
      ].sort((left, right) => left.question - right.question),
    );
    return true;
  }

  async function submit(answer: Answer) {
    setEditMode(false);
    if (
      (answer === "yes" || answer === "edited") &&
      typeCode !== "TUNNEL_IDENTITY" &&
      typeCode !== "GUIDE_SIGN_IDENTITY"
    ) {
      setPositiveAnswer(answer);
      setFollowup(true);
      return;
    }
    if (await appendRecord(answer)) goNext();
  }

  function goNext() {
    const next = questionIndex + 1;
    if (next >= questions.length) {
      setQueueComplete(true);
      setFollowup(false);
      setEditMode(false);
      return;
    }
    setQuestionIndex(next);
    setTypeCode(questions[next].typeCode);
    setPoint(questionPoint(questions, next));
    setFrameIndex(3);
    setZoomed(false);
    setOcrText("");
    setSeverity("보통");
    setAction("현장 확인 후 적정 유지보수 조치");
    setEditMode(false);
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
        setSeverity(last.severity ?? "보통");
        setAction(
          last.action ?? "현장 확인 후 적정 유지보수 조치",
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
  }

  async function confirmFollowup() {
    if (await appendRecord(positiveAnswer, { severity, action })) goNext();
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
    const service = window.trafficVisson?.autoLabeling;
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
    const service = window.trafficVisson?.autoLabeling;
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
    return (
      <div className="screen screen--labeling">
        <header className="screen-header">
          <div>
            <p className="eyebrow">HUMAN-IN-THE-LOOP</p>
            <h1>질문형 라벨링</h1>
            <p>현재 프로젝트의 질문 큐를 모두 검토했습니다.</p>
          </div>
        </header>
        {jobSwitcher}
        {autoLabelingPanel}
        <GlassPanel className="labeling-complete" role="status">
          <Check size={38} />
          <Badge tone="success">로컬 검토 기록 저장됨</Badge>
          <h2>{questions.length}개 질문 검토 완료</h2>
          <p>
            승인·거절·보류·수정 결과가 이 기기의 프로젝트 검토 기록에 보존됐습니다.
            모델 재학습은 자동으로 시작되지 않습니다.
          </p>
          <div>
            <Button onClick={undo}>
              <RotateCcw size={17} /> 마지막 답변 다시 검토
            </Button>
            <Button
              variant="ghost"
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
        <div className="policy-toggle" role="group" aria-label="질문 정책">
          {(["학습 보조", "전수 검토", "빠른 검토"] as Policy[]).map((mode) => (
            <button
              type="button"
              key={mode}
              className={policy === mode ? "active" : ""}
              aria-pressed={policy === mode}
              disabled
              title="검증 세트로 질문 정책의 안전성을 입증하기 전까지 실제 후보는 전수 검토합니다."
            >
              {mode}
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
                  ? "영역 수정 캔버스. 방향키로 임시 박스의 기준점을 이동할 수 있습니다."
                  : undefined
              }
              onClick={(event) => {
                if (!editMode) return;
                const rect = event.currentTarget.getBoundingClientRect();
                setPoint({
                  x: ((event.clientX - rect.left) / rect.width) * 100,
                  y: ((event.clientY - rect.top) / rect.height) * 100,
                });
              }}
              onKeyDown={(event) => {
                if (!editMode || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
                  return;
                }
                event.preventDefault();
                const amount = event.shiftKey ? 5 : 1;
                setPoint((current) => ({
                  x: Math.max(
                    2,
                    Math.min(
                      98,
                      current.x +
                        (event.key === "ArrowLeft"
                          ? -amount
                          : event.key === "ArrowRight"
                            ? amount
                            : 0),
                    ),
                  ),
                  y: Math.max(
                    2,
                    Math.min(
                      98,
                      current.y +
                        (event.key === "ArrowUp"
                          ? -amount
                          : event.key === "ArrowDown"
                            ? amount
                            : 0),
                    ),
                  ),
                }));
              }}
            >
              <img
                src={currentEvidenceImage}
                alt={`분석 후보 ${question.id}의 대표 근거 프레임`}
              />
              {currentBox ? (
                <button
                  type="button"
                  className="detection-box"
                  aria-label={`표시된 ${typeCode} 영역`}
                  style={{
                    left: `${Math.max(2, point.x - currentBox.width / 2)}%`,
                    top: `${Math.max(2, point.y - currentBox.height / 2)}%`,
                    width: `${currentBox.width}%`,
                    height: `${currentBox.height}%`,
                  }}
                  onClick={(event) => {
                    event.stopPropagation();
                    setEditMode(true);
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
              {editMode ? (
                <span
                  className="point-prompt"
                  aria-label="임시 영역 중심점"
                  style={{ left: `${point.x}%`, top: `${point.y}%` }}
                >
                  <Crosshair size={26} />
                </span>
              ) : null}
              <div className="evidence-overlay">
                <span>
                  {currentBox
                    ? editMode
                      ? "수동 임시 영역"
                      : question.experimental
                        ? "AI 제안 영역"
                        : "표시 영역"
                    : question.experimental
                      ? "장면 단위 AI 제안 · 영역 없음"
                      : "미분류 장면 후보"}
                </span>
                <span>원본 근거</span>
              </div>
              {editMode ? (
                <div className="editing-hint" role="status">
                  <MousePointer2 size={18} />
                  클릭 또는 방향키로 기준점 이동 · Shift+방향키 5칸
                </div>
              ) : null}
            </div>

            {question.adjacentFrames?.length ? (
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
              <span>정책 · {policy}</span>
            </div>
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
                      