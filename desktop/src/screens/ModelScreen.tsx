import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  BrainCircuit,
  Check,
  Database,
  ShieldAlert,
  Sparkles,
  X,
} from "lucide-react";
import {
  Badge,
  Button,
  GlassPanel,
  PanelHeader,
} from "../components/ui";
import {
  buildProjectScopeRequest,
  parseDatasetVersionList,
  parseProjectLabelStats,
  parseSourceCandidateCatalog,
  type DatasetVersion,
  type ProjectLabelStats,
  type SourceCandidate,
  type SourceCandidateCatalog,
} from "../modelRegistry";
import {
  buildProjectModelCancelRequest,
  buildProjectModelResumeRequest,
  buildProjectModelRollbackRequest,
  buildProjectModelStartRequest,
  buildProjectModelTransitionRequest,
  buildProjectFixedValidationExtensionApproval,
  createProjectModelIdempotencyKey,
  PROJECT_MODEL_ISSUE_TYPES,
  parseProjectModelTrainingJob,
  parseProjectModelTransition,
  readProjectModelLifecycle,
  type ProjectFixedValidationExtensionApproval,
  type ProjectModelLifecycleSnapshot,
  type ProjectModelIssueType,
  type ProjectModelTrainingJob,
  type ProjectModelVersion,
} from "../projectModels";
import { getActiveProjectId, useProjectCatalog } from "../projectCatalog";

const ISSUE_TYPE_LABELS: Readonly<
  Record<ProjectModelIssueType, string>
> = {
  pothole: "포트홀",
  road_surface_damage: "노면 파손",
  road_debris: "낙하물",
  median_barrier_damage: "중앙분리대 파손",
  sign_damage: "표지판 파손",
  sign_vegetation_occlusion: "표지판 식생 가림",
  roadside_mowing_required: "삭초 필요",
  guide_sign_identity: "이정표 식별",
  tunnel_identity: "터널명 식별",
  tunnel_name_occlusion: "터널명 가림",
  lane_marking_faded: "차선 재도색",
  colored_guidance_faded: "색깔유도선 재도색",
};

async function readModelRegistry(projectId: string) {
  const api = window.trafficSuperVision;
  if (!api?.labels || !api.datasets) {
    throw new Error("모델 저장소는 설치형 Electron 앱에서 연결됩니다.");
  }
  const request = buildProjectScopeRequest(projectId);
  const [statsValue, versionsValue] = await Promise.all([
    api.labels.stats(request),
    api.datasets.list(request),
  ]);
  return {
    stats: parseProjectLabelStats(statsValue, projectId),
    versions: parseDatasetVersionList(versionsValue, projectId).versions,
    lifecycle: await readProjectModelLifecycle(projectId),
  };
}

async function readSourceCandidates() {
  const api = window.trafficSuperVision;
  if (!api?.sources) {
    throw new Error("외부 소스 카탈로그는 설치형 Electron 앱에서 연결됩니다.");
  }
  return parseSourceCandidateCatalog(await api.sources.candidates());
}

function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "프로젝트 모델 정보를 불러오지 못했습니다.";
}

function fixedValidationErrorMessage(error: unknown) {
  const message = errorMessage(error);
  if (message.includes("fixed_validation_extension_approval_stale")) {
    return "승인한 부모 검증 세트가 변경되었습니다. 최신 버전과 해시를 다시 불러온 뒤 승인하세요.";
  }
  if (message.includes("fixed_validation_extension_duplicate_evidence")) {
    return "추가 검증 근거가 부모 검증 세트와 중복됩니다. 다른 AnalysisJob의 독립 표본을 선택하세요.";
  }
  if (message.includes("fixed_validation_extension_insufficient")) {
    const observed = message.match(/\(([a-z0-9_=, ]+)\)/)?.[1];
    return `새 유형의 사용자 확정 양성 validation 표본이 30건 미만입니다${observed ? ` (${observed})` : ""}. 해당 AnalysisJob의 표본을 보강하세요.`;
  }
  if (
    message.includes("fixed_validation_extension_approval_invalid") ||
    message.includes("fixed_validation_extension_not_required")
  ) {
    return "새 유형 또는 추가 validation AnalysisJob 구성이 올바르지 않습니다.";
  }
  if (message.includes("fixed_validation_binding_stale")) {
    return "DatasetVersion의 부모 검증 구성원이 변경되었거나 누락되었습니다. 최신 DatasetVersion으로 다시 시도하세요.";
  }
  return message;
}

function formatCreatedAt(value: string) {
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function sourceCapabilityLabel(candidate: SourceCandidate) {
  switch (candidate.capability) {
    case "natural_language_box_proposals":
      return "자연어 박스 후보";
    case "prompted_mask_refinement":
      return "클릭·박스 마스크 보정";
    case "semantic_candidate_scoring":
      return "자연어 의미 후보 점수";
    case "pothole_box_proposals":
      return "포트홀 박스 후보";
    case "labeled_training_reference":
      return "라벨 학습 참고";
  }
}

function sourceDecisionLabel(candidate: SourceCandidate) {
  switch (candidate.catalogStatus) {
    case "evaluation_complete":
      return "평가 완료 · 검증용";
    case "unverified_local":
      return "검증 실패 · 격리";
    case "rejected":
      return candidate.installationStatus === "not_installed"
        ? "거부됨 · 미설치"
        : "거부됨 · 격리";
    case "review_candidate":
      return "검토 후보 · 미설치";
  }
}

function sourceDecisionSummary(candidate: SourceCandidate) {
  if (candidate.evaluation?.verdict === "rejected") {
    return `오탐 ${candidate.evaluation.falsePositiveCount} / ${candidate.evaluation.proposalCount} · 사용 거부`;
  }
  if (candidate.evaluation) {
    return `평가 ${candidate.evaluation.evaluatedFrameCount}프레임 · 후보 ${candidate.evaluation.proposalCount} · 자동확정 금지`;
  }
  if (candidate.observationBasis === "unverified_local_files") {
    return "고정 증거 해시 불일치 · 사용 금지";
  }
  if (candidate.catalogStatus === "rejected") {
    return "배포 후보 거부 · 자동 사용 금지";
  }
  return candidate.sourceType === "labeled_dataset"
    ? "미설치 · 품질·중복·분할 검증 전"
    : "미설치 · 자동 활성화 금지";
}

function latestTrainingJob(
  lifecycle: ProjectModelLifecycleSnapshot | null,
) {
  return (
    lifecycle?.trainingJobs.reduce<ProjectModelTrainingJob | null>(
      (latest, job) => {
        if (!latest) return job;
        return Date.parse(job.updatedAt) >= Date.parse(latest.updatedAt)
          ? job
          : latest;
      },
      null,
    ) ?? null
  );
}

function modelStatusLabel(job: ProjectModelTrainingJob | null) {
  if (!job) return "학습 시작 전";
  if (job.status === "queued") return "학습 대기";
  if (job.status === "running") return "학습 실행 중";
  if (job.status === "interrupted") return "재개 필요";
  if (job.status === "cancelled") return "학습 취소됨";
  if (job.status === "succeeded") return "후보 모델 생성";
  if (job.status === "failed") return "학습 실패";
  if (job.blockers.some(({ code }) => code === "trainer_unavailable")) {
    return "학습기 준비 필요";
  }
  if (
    job.blockers.some(
      ({ code }) =>
        code === "insufficient_current_confirmed_labels" ||
        code === "insufficient_validation_samples",
    )
  ) {
    return "라벨 기준 미충족";
  }
  if (
    job.blockers.some(({ code }) => code === "candidate_review_required")
  ) {
    return "후보 모델 검토 필요";
  }
  return "학습 차단";
}

function modelStatusMessage(job: ProjectModelTrainingJob | null) {
  if (!job) {
    return "DatasetVersion과 라벨 기준을 충족한 뒤 사용자가 직접 학습을 시작합니다.";
  }
  const trainerUnavailable = job.blockers.some(
    ({ code }) => code === "trainer_unavailable",
  );
  if (trainerUnavailable) {
    return "학습 런타임이 준비되지 않았습니다. 이 상태는 라벨 부족이나 검지 0건과 별도로 기록됩니다.";
  }
  const labelBlocker = job.blockers.find(
    ({ code }) => code === "insufficient_current_confirmed_labels",
  );
  if (labelBlocker) {
    return `현재 영상에서 학습이 승인된 Annotation ${job.currentConfirmedLabelCount}건입니다. 100건이 필요합니다. 사용자 직접 확정과 프로젝트 범위에서 명시적으로 위임된 structured LLM 학습 라벨만 포함합니다. 사용자 승인 3·4분기 사례 ${job.approvedReferenceTrainingEventCount}건(근거 사진 ${job.approvedReferenceTrainingMemberCount}장)은 낮은 가중치 보조 seed일 뿐 100건 기준이나 고정 검증에는 합산하지 않습니다.`;
  }
  const validationBlockers = job.blockers.filter(
    ({ code }) => code === "insufficient_validation_samples",
  );
  if (validationBlockers.length > 0) {
    return `고정 검증 표본이 부족한 유형 ${validationBlockers.length}개가 있습니다. 유형별 30건을 충족해야 합니다.`;
  }
  if (job.status === "queued") {
    return `DatasetVersion ${job.datasetVersionId.slice(0, 8)}… 학습 요청을 저장했고 CUDA 작업 순서를 기다립니다. 자동 승격하지 않습니다.`;
  }
  if (job.status === "running") {
    return job.cancelRequested
      ? "취소 요청을 저장했습니다. 현재 CUDA 단계가 안전한 중단 지점에서 체크포인트를 남기는 중입니다."
      : `DatasetVersion ${job.datasetVersionId.slice(0, 8)}…로 CUDA 학습 중입니다. HTTP 요청과 분리된 작업이며 자동 승격하지 않습니다.`;
  }
  if (job.status === "interrupted") {
    return "앱 또는 PC 재시작으로 중단된 작업입니다. 사용자 확인 후 같은 작업을 재개할 수 있습니다.";
  }
  if (job.status === "failed") {
    return job.error?.message ?? "학습 실행이 실패했습니다. 실패를 빈 성공으로 저장하지 않았습니다.";
  }
  if (job.status === "cancelled") {
    return job.checkpoint
      ? "사용자가 학습을 취소했습니다. 저장된 체크포인트와 같은 provenance로 명시적으로 재개할 수 있습니다."
      : "사용자가 대기 중인 학습을 취소했습니다. 명시적으로 재개하면 같은 DatasetVersion으로 다시 대기합니다.";
  }
  if (job.status === "succeeded") {
    return "고정 검증을 통과한 후보 모델입니다. 사용자가 승격하거나 거절하기 전까지 활성 모델을 바꾸지 않습니다.";
  }
  return "현재 차단 사유를 해소한 뒤 새 DatasetVersion으로 다시 시작하세요.";
}

function trainingProgressPercent(job: ProjectModelTrainingJob | null) {
  if (!job) return null;
  const { current, total, epoch, epochs } = job.progress;
  if (current !== null && total !== null && total > 0) {
    const stageFraction = current / total;
    if (epoch !== null && epochs !== null && epochs > 0) {
      return Math.max(
        0,
        Math.min(
          100,
          (((Math.max(1, epoch) - 1) + stageFraction) / epochs) * 100,
        ),
      );
    }
    return Math.max(0, Math.min(100, stageFraction * 100));
  }
  if (epoch !== null && epochs !== null && epochs > 0) {
    return Math.max(0, Math.min(100, (epoch / epochs) * 100));
  }
  return null;
}

function trainingProgressLabel(job: ProjectModelTrainingJob | null) {
  if (!job) return null;
  const { stage, current, total, epoch, epochs } = job.progress;
  const stageLabels: Readonly<Record<string, string>> = {
    queued: "작업 대기",
    starting: "CUDA 작업 준비",
    fine_tune: "모델 미세조정",
    training_embedding: "학습 이미지 임베딩",
    reference_embedding: "승인 사례 임베딩",
    fixed_validation_embedding: "고정 검증 임베딩",
    candidate_fixed_validation: "후보 고정 검증",
    completed: "학습 완료",
    cancelled: "학습 취소",
  };
  const stageLabel = stageLabels[stage] ?? stage;
  const epochLabel =
    epoch !== null && epochs !== null ? ` · epoch ${epoch}/${epochs}` : "";
  const counterLabel =
    current !== null && total !== null ? ` · ${current}/${total}` : "";
  return `${stageLabel}${epochLabel}${counterLabel}`;
}

function modelVersionById(
  lifecycle: ProjectModelLifecycleSnapshot | null,
  modelId: string | null | undefined,
): ProjectModelVersion | null {
  if (!modelId) return null;
  return (
    lifecycle?.modelVersions.find(
      ({ modelVersionId }) => modelVersionId === modelId,
    ) ?? null
  );
}

function projectModelRoleLabel(model: ProjectModelVersion) {
  switch (model.modelFormat) {
    case "trafficvisson.local-object-detector.v1":
      return "전용 객체 검출기";
    case "trafficvisson.visual-embedding-centroid-head.v1":
      return "보조 의미 분류 헤드";
    default: {
      const unsupportedFormat: never = model.modelFormat;
      return unsupportedFormat;
    }
  }
}

export function ModelScreen() {
  const projectCatalog = useProjectCatalog();
  const activeProject = projectCatalog.projects.find(
    (project) => project.id === projectCatalog.activeProjectId,
  );
  const projectId = activeProject?.id ?? null;
  const [stats, setStats] = useState<ProjectLabelStats | null>(null);
  const [versions, setVersions] = useState<DatasetVersion[]>([]);
  const [lifecycle, setLifecycle] =
    useState<ProjectModelLifecycleSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [modelAction, setModelAction] = useState<
    | "start"
    | "extend-validation"
    | "resume"
    | "cancel"
    | "promote"
    | "reject"
    | "rollback"
    | null
  >(null);
  const [validationDialogOpen, setValidationDialogOpen] = useState(false);
  const [extensionIssueType, setExtensionIssueType] =
    useState<ProjectModelIssueType>("pothole");
  const [extensionAnalysisJobs, setExtensionAnalysisJobs] = useState("");
  const [extensionConfirmed, setExtensionConfirmed] = useState(false);
  const [extensionError, setExtensionError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sourceCatalog, setSourceCatalog] =
    useState<SourceCandidateCatalog | null>(null);
  const [sourceLoading, setSourceLoading] = useState(true);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [message, setMessage] = useState(
    "선택한 프로젝트의 라벨과 데이터셋 상태를 확인합니다.",
  );

  useEffect(() => {
    let active = true;
    setSourceLoading(true);
    setSourceError(null);
    void readSourceCandidates()
      .then((catalog) => {
        if (active) setSourceCatalog(catalog);
      })
      .catch((reason) => {
        if (!active) return;
        setSourceCatalog(null);
        setSourceError(
          reason instanceof Error
            ? reason.message
            : "외부 소스 카탈로그를 불러오지 못했습니다.",
        );
      })
      .finally(() => {
        if (active) setSourceLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    let active = true;
    setStats(null);
    setVersions([]);
    setLifecycle(null);
    setModelAction(null);
    setValidationDialogOpen(false);
    setExtensionAnalysisJobs("");
    setExtensionConfirmed(false);
    setExtensionError(null);
    setError(null);
    if (!projectId) {
      setLoading(false);
      setMessage("프로젝트를 선택하면 모델 준비 상태를 확인할 수 있습니다.");
      return () => {
        active = false;
      };
    }
    setLoading(true);
    setMessage("현재 프로젝트의 라벨과 데이터셋을 동기화하는 중입니다.");
    void readModelRegistry(projectId)
      .then((registry) => {
        if (!active) return;
        setStats(registry.stats);
        setVersions(registry.versions);
        setLifecycle(registry.lifecycle);
        setMessage(
          registry.versions.length > 0
            ? "검토 완료 후 자동 준비된 DatasetVersion을 불러왔습니다."
            : registry.stats.currentAnswerCount > 0
              ? "확정 답변은 있으나 자동 DatasetVersion이 없습니다. PipelineJob 오류를 확인하세요."
              : "질문 검토가 완료되면 DatasetVersion이 자동 준비됩니다.",
        );
      })
      .catch((reason) => {
        if (!active) return;
        setError(errorMessage(reason));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [projectId]);

  const latestDataset = useMemo(
    () =>
      versions.reduce<DatasetVersion | null>((latest, version) => {
        if (!latest) return version;
        return Date.parse(version.createdAt) >= Date.parse(latest.createdAt)
          ? version
          : latest;
      }, null),
    [versions],
  );
  const trainingIncludedCount =
    latestDataset?.trainingGateMemberCount ?? 0;
  const enoughConfirmed = trainingIncludedCount >= 100;
  const currentTrainingJob = useMemo(
    () => latestTrainingJob(lifecycle),
    [lifecycle],
  );
  const currentFixedValidation =
    lifecycle?.fixedValidationVersions.at(-1) ?? null;
  const latestSurveyQa =
    lifecycle?.modelIterations.surveyQa.at(-1) ?? null;
  const validationSampleCounts =
    currentFixedValidation?.validationSampleCounts ??
    currentTrainingJob?.validationSampleCounts ??
    {};
  const validationCountEntries = Object.entries(
    validationSampleCounts,
  ) as [ProjectModelIssueType, number][];
  const validationReadinessKnown = validationCountEntries.length > 0;
  const validationReady =
    validationReadinessKnown &&
    validationCountEntries.every(([, count]) => count >= 30);
  const minimumValidationCount = validationReadinessKnown
    ? Math.min(...validationCountEntries.map(([, count]) => count))
    : 0;
  const quantityGateReady =
    enoughConfirmed && (!validationReadinessKnown || validationReady);
  const availableExtensionIssueTypes = PROJECT_MODEL_ISSUE_TYPES.filter(
    (issueType) =>
      !currentFixedValidation?.requiredIssueTypes.includes(issueType),
  );
  useEffect(() => {
    if (
      availableExtensionIssueTypes.length > 0 &&
      !availableExtensionIssueTypes.includes(extensionIssueType)
    ) {
      setExtensionIssueType(availableExtensionIssueTypes[0]);
    }
  }, [
    extensionIssueType,
    availableExtensionIssueTypes.join("|"),
  ]);
  const projectModelState = lifecycle?.state ?? null;
  const candidateModel = modelVersionById(
    lifecycle,
    projectModelState?.candidateModelId,
  );
  const activeModel = modelVersionById(
    lifecycle,
    projectModelState?.activeModelId,
  );
  const rejectedModelCount =
    lifecycle?.modelVersions.filter(({ state }) => state === "rejected")
      .length ?? 0;
  const trainingInProgress =
    currentTrainingJob?.status === "queued" ||
    currentTrainingJob?.status === "running";
  const currentTrainingProgressPercent =
    trainingProgressPercent(currentTrainingJob);
  const currentTrainingProgressLabel =
    trainingProgressLabel(currentTrainingJob);
  useEffect(() => {
    if (!projectId || !trainingInProgress) return;
    let disposed = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await readProjectModelLifecycle(projectId);
        if (
          !disposed &&
          getActiveProjectId() === projectId
        ) {
          setLifecycle(next);
        }
      } catch (reason) {
        if (!disposed && getActiveProjectId() === projectId) {
          setError(errorMessage(reason));
        }
      } finally {
        if (!disposed) timer = window.setTimeout(poll, 1_000);
      }
    };
    timer = window.setTimeout(poll, 500);
    return () => {
      disposed = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [
    projectId,
    trainingInProgress,
    currentTrainingJob?.trainingJobId,
  ]);
  const trainerUnavailable = Boolean(
    currentTrainingJob?.blockers.some(
      ({ code }) => code === "trainer_unavailable",
    ),
  );
  const currentDatasetAlreadyAttempted =
    Boolean(currentTrainingJob && latestDataset) &&
    currentTrainingJob?.datasetVersionId === latestDataset?.datasetVersionId &&
    !trainerUnavailable;
  const canStartTraining =
    Boolean(projectId && latestDataset) &&
    quantityGateReady &&
    !candidateModel &&
    !trainingInProgress &&
    !currentDatasetAlreadyAttempted &&
    modelAction === null;
  const trainingTitle = !projectId
    ? "먼저 프로젝트를 선택하세요."
    : !latestDataset
      ? "질문 검토 완료 후 자동 DatasetVersion을 확인하세요."
      : !quantityGateReady
        ? "TRAIN split의 현행 학습 승인 Annotation 100건과 유형별 독립 고정 검증 양성 30건이 필요합니다. 3·4분기 승인 사례는 이 기준에 포함되지 않습니다."
        : candidateModel
          ? "기존 후보 모델을 먼저 승격하거나 거절하세요."
          : trainingInProgress
            ? "현재 프로젝트 학습이 진행 중입니다."
            : currentDatasetAlreadyAttempted
              ? "이 DatasetVersion은 이미 사용했습니다. 다음 검토 완료 후 자동 생성되는 새 버전을 기다리세요."
              : validationReadinessKnown
                ? "현재 DatasetVersion과 유형별 고정 검증 세트로 학습을 시작합니다."
                : "서버가 DatasetVersion의 유형별 고정 검증 양성 30건을 먼저 검증합니다.";

  async function refreshProjectModelState(project: string) {
    const registry = await readModelRegistry(project);
    if (getActiveProjectId() !== project) return;
    setStats(registry.stats);
    setVersions(registry.versions);
    setLifecycle(registry.lifecycle);
  }

  async function startProjectTraining() {
    const api = window.trafficSuperVision?.projectModels;
    if (!api || !projectId || !latestDataset || !canStartTraining) return;
    if (
      !window.confirm(
        "현재 DatasetVersion에서 새 프로젝트 모델 학습을 시작할까요? 100건 기준은 TRAIN split의 현행 학습 승인 Annotation만 계산합니다. 사용자 승인 3·4분기 사례는 학습 구성원 중 낮은 가중치의 보조 seed이지만 100건 기준과 독립 검증 세트에는 포함되지 않습니다. 모델은 자동 승격되지 않습니다.",
      )
    ) {
      return;
    }
    setModelAction("start");
    setError(null);
    setMessage("학습 사전점검을 거쳐 CUDA 작업 요청을 저장하는 중입니다.");
    try {
      const request = buildProjectModelStartRequest(
        projectId,
        latestDataset.datasetVersionId,
        createProjectModelIdempotencyKey("train"),
      );
      const job = parseProjectModelTrainingJob(
        await api.startTraining(request),
        projectId,
      );
      await refreshProjectModelState(projectId);
      if (getActiveProjectId() !== projectId) return;
      setMessage(modelStatusMessage(job));
    } catch (reason) {
      if (getActiveProjectId() === projectId) {
        setError(errorMessage(reason));
      }
    } finally {
      if (getActiveProjectId() === projectId) setModelAction(null);
    }
  }

  async function extendFixedValidationAndTrain() {
    const api = window.trafficSuperVision;
    if (
      !api?.projectModels ||
      !projectId ||
      !currentFixedValidation ||
      !latestDataset ||
      candidateModel ||
      trainingInProgress ||
      modelAction !== null
    ) {
      return;
    }
    if (!extensionConfirmed) {
      setExtensionError(
        "부모 버전·구성원 해시·검증 해시를 확인하고 명시적 승인에 체크하세요.",
      );
      return;
    }
    let approval: ProjectFixedValidationExtensionApproval;
    try {
      const analysisJobIds = extensionAnalysisJobs
        .split(/[\s,;]+/)
        .map((value) => value.trim())
        .filter(Boolean);
      approval = buildProjectFixedValidationExtensionApproval(
        currentFixedValidation,
        extensionIssueType,
        analysisJobIds,
      );
    } catch (reason) {
      setExtensionError(fixedValidationErrorMessage(reason));
      return;
    }
    if (
      !window.confirm(
        `고정 검증 세트 v${currentFixedValidation.validationSetVersion}의 불변 구성원을 보존하고 ${ISSUE_TYPE_LABELS[extensionIssueType]} 유형을 자식 버전으로 확장한 뒤 모델 학습 사전점검을 시작할까요? 선택한 AnalysisJob에서 사용자 확정 양성 validation 표본 30건 이상을 서버가 다시 검증합니다.`,
      )
    ) {
      return;
    }
    setModelAction("extend-validation");
    setExtensionError(null);
    setError(null);
    setMessage("현재 DatasetVersion과 고정 검증 자식 버전을 검증해 CUDA 작업 요청을 저장하는 중입니다.");
    try {
      const job = parseProjectModelTrainingJob(
        await api.projectModels.startTraining(
          buildProjectModelStartRequest(
            projectId,
            latestDataset.datasetVersionId,
            createProjectModelIdempotencyKey("extend-validation"),
            approval,
          ),
        ),
        projectId,
      );
      await refreshProjectModelState(projectId);
      if (getActiveProjectId() !== projectId) return;
      setValidationDialogOpen(false);
      setExtensionAnalysisJobs("");
      setExtensionConfirmed(false);
      setMessage(
        `고정 검증 자식 버전을 서버가 확인했습니다. ${modelStatusMessage(job)}`,
      );
    } catch (reason) {
      if (getActiveProjectId() === projectId) {
        setExtensionError(fixedValidationErrorMessage(reason));
        await refreshProjectModelState(projectId).catch(() => undefined);
      }
    } finally {
      if (getActiveProjectId() === projectId) {
        setModelAction(null);
      }
    }
  }

  async function resumeProjectTraining() {
    const api = window.trafficSuperVision?.projectModels;
    if (
      !api ||
      !projectId ||
      !currentTrainingJob ||
      (currentTrainingJob.status !== "interrupted" &&
        currentTrainingJob.status !== "failed" &&
        currentTrainingJob.status !== "cancelled")
    ) {
      return;
    }
    if (
      !window.confirm(
        `중단되거나 취소된 프로젝트 모델 학습을 같은 DatasetVersion과 provenance로 재개할까요? ${
          currentTrainingJob.checkpoint
            ? "저장된 체크포인트 해시를 검증한 뒤 이어서 실행합니다."
            : "사용 가능한 체크포인트가 없으면 같은 요청으로 새 시도를 시작합니다."
        }`,
      )
    ) {
      return;
    }
    setModelAction("resume");
    setError(null);
    try {
      const job = parseProjectModelTrainingJob(
        await api.resumeTraining(
          buildProjectModelResumeRequest(
            projectId,
            currentTrainingJob.trainingJobId,
          ),
        ),
        projectId,
      );
      await refreshProjectModelState(projectId);
      if (getActiveProjectId() === projectId) {
        setMessage(modelStatusMessage(job));
      }
    } catch (reason) {
      if (getActiveProjectId() === projectId) setError(errorMessage(reason));
    } finally {
      if (getActiveProjectId() === projectId) setModelAction(null);
    }
  }

  async function cancelProjectTraining() {
    const api = window.trafficSuperVision?.projectModels;
    if (
      !api ||
      !projectId ||
      !currentTrainingJob ||
      (currentTrainingJob.status !== "queued" &&
        currentTrainingJob.status !== "running") ||
      currentTrainingJob.cancelRequested ||
      modelAction !== null
    ) {
      return;
    }
    if (
      !window.confirm(
        currentTrainingJob.status === "queued"
          ? "대기 중인 CUDA 학습 요청을 취소할까요? 취소 상태는 저장되며 사용자 확인 후 다시 재개할 수 있습니다."
          : "실행 중인 CUDA 학습을 취소할까요? 안전한 중단 지점까지 진행한 뒤 가능한 체크포인트를 저장합니다.",
      )
    ) {
      return;
    }
    setModelAction("cancel");
    setError(null);
    try {
      const job = parseProjectModelTrainingJob(
        await api.cancelTraining(
          buildProjectModelCancelRequest(
            projectId,
            currentTrainingJob.trainingJobId,
          ),
        ),
        projectId,
      );
      await refreshProjectModelState(projectId);
      if (getActiveProjectId() === projectId) {
        setMessage(modelStatusMessage(job));
      }
    } catch (reason) {
      if (getActiveProjectId() === projectId) setError(errorMessage(reason));
    } finally {
      if (getActiveProjectId() === projectId) setModelAction(null);
    }
  }

  async function transitionProjectModel(
    action: "promote" | "reject" | "rollback",
  ) {
    const api = window.trafficSuperVision?.projectModels;
    if (!api || !projectId || !projectModelState) return;
    const target =
      action === "rollback" ? null : projectModelState.candidateModelId;
    if (action !== "rollback" && !target) return;
    const confirmation =
      action === "promote"
        ? "고정 검증을 통과한 후보 모델을 이 프로젝트의 활성 모델로 승격할까요? 이후 새 영상 조사는 이 모델을 사용합니다."
        : action === "reject"
          ? "후보 모델을 거절할까요? 현재 활성 모델은 그대로 유지됩니다."
          : "현재 활성 모델을 이전 모델로 롤백할까요?";
    if (!window.confirm(confirmation)) return;
    setModelAction(action);
    setError(null);
    try {
      const idempotencyKey = createProjectModelIdempotencyKey(action);
      const response =
        action === "rollback"
          ? await api.rollback(
              buildProjectModelRollbackRequest(
                projectId,
                projectModelState.revision,
                idempotencyKey,
              ),
            )
          : await api[action](
              buildProjectModelTransitionRequest(
                projectId,
                target!,
                projectModelState.revision,
                idempotencyKey,
              ),
            );
      const transition = parseProjectModelTransition(response, projectId);
      await refreshProjectModelState(projectId);
      if (getActiveProjectId() === projectId) {
        setMessage(
          transition.action === "promote"
            ? "후보 모델을 활성 모델로 승격했습니다."
            : transition.action === "reject"
              ? "후보 모델을 거절하고 활성 모델을 유지했습니다."
              : "이전 프로젝트 모델로 롤백했습니다.",
        );
      }
    } catch (reason) {
      if (getActiveProjectId() === projectId) setError(errorMessage(reason));
    } finally {
      if (getActiveProjectId() === projectId) setModelAction(null);
    }
  }

  return (
    <div className="screen screen--model">
      <header className="screen-header">
        <div>
          <p className="eyebrow">프로젝트 모델 · 버전 관리</p>
          <h1>모델 관리</h1>
          <p>프로젝트의 확정 라벨만 사용하며 다른 프로젝트 데이터는 자동으로 섞지 않습니다.</p>
        </div>
        <div className="header-actions">
          <Button
            disabled={
              !currentFixedValidation ||
              availableExtensionIssueTypes.length === 0 ||
              modelAction !== null
            }
            onClick={() => {
              setExtensionError(null);
              setValidationDialogOpen(true);
            }}
            title={
              currentFixedValidation
                ? availableExtensionIssueTypes.length > 0
                  ? "새 문제 유형용 불변 고정 검증 자식 버전을 승인합니다."
                  : "모든 지원 유형이 현재 검증 세트에 포함되어 있습니다."
                : "첫 모델 학습에서 고정 검증 세트 v1을 먼저 만드세요."
            }
          >
            검증 세트{" "}
            {currentFixedValidation
              ? `v${currentFixedValidation.validationSetVersion}`
              : "생성 전"}
          </Button>
          <Button
            variant="primary"
            disabled={!canStartTraining}
            onClick={startProjectTraining}
            title={trainingTitle}
          >
            <Sparkles size={17} />{" "}
            {modelAction === "start" ? "학습 확인 중" : "새 모델 학습"}
          </Button>
        </div>
      </header>

      <div className="model-layout">
        <div className="model-sidebar">
          <GlassPanel>
            <PanelHeader
              eyebrow="학습 시작 조건"
              title="학습 가능 조건"
              action={
                <Badge tone={quantityGateReady ? "success" : "warning"}>
                  {loading
                    ? "동기화 중"
                    : modelStatusLabel(currentTrainingJob)}
                </Badge>
              }
            />
            <ul className="gate-list">
              <li className={enoughConfirmed ? "pass" : "warn"}>
                {enoughConfirmed ? <Check size={17} /> : <AlertTriangle size={17} />}
                <span>TRAIN 현행 승인 Annotation 100건</span>
                <strong>{trainingIncludedCount} / 100</strong>
              </li>
              <li className={validationReady ? "pass" : "warn"}>
                {validationReady ? <Check size={17} /> : <AlertTriangle size={17} />}
                <span
                  title={
                    validationReadinessKnown
                      ? validationCountEntries
                          .map(
                            ([issueType, count]) =>
                              `${ISSUE_TYPE_LABELS[issueType]} ${count}/30`,
                          )
                          .join(" · ")
                      : "첫 학습 사전점검에서 유형별 수를 서버가 확인합니다."
                  }
                >
                  고정 검증 양성 · 유형별 30건
                </span>
                <strong>
                  {validationReadinessKnown
                    ? `최소 ${minimumValidationCount} / 30`
                    : "확인 전"}
                </strong>
              </li>
              <li className={latestDataset ? "pass" : "warn"}>
                {latestDataset ? <Check size={17} /> : <AlertTriangle size={17} />}
                <span>불변 DatasetVersion</span>
                <strong>{latestDataset ? `${versions.length}개` : "생성 전"}</strong>
              </li>
              <li className={activeProject ? "pass" : "warn"}>
                {activeProject ? <Check size={17} /> : <AlertTriangle size={17} />}
                <span>프로젝트 데이터 격리</span>
                <strong>
                  {activeProject ? `${activeProject.name}만` : "선택 필요"}
                </strong>
              </li>
            </ul>
            <div className="notice notice--warning model-training-notice">
              <ShieldAlert size={19} />
              <div>
                <strong>{modelStatusLabel(currentTrainingJob)}</strong>
                <p>{modelStatusMessage(currentTrainingJob)}</p>
                {currentTrainingProgressLabel ? (
                  <div className="model-training-progress">
                    <span>
                      {currentTrainingProgressLabel}
                      {currentTrainingProgressPercent !== null
                        ? ` · ${Math.round(currentTrainingProgressPercent)}%`
                        : ""}
                    </span>
                    {currentTrainingProgressPercent !== null ? (
                      <progress
                        aria-label="프로젝트 모델 학습 진행률"
                        max={100}
                        value={currentTrainingProgressPercent}
                      />
                    ) : null}
                  </div>
                ) : null}
                {currentTrainingJob?.checkpoint ? (
                  <p className="model-training-checkpoint">
                    체크포인트 #{currentTrainingJob.checkpoint.attemptNumber}
                    {" · "}
                    해시 {currentTrainingJob.checkpoint.sha256.slice(0, 10)}…
                    {" · "}
                    {(currentTrainingJob.checkpoint.sizeBytes / 1_048_576).toFixed(
                      1,
                    )}{" "}
                    MB
                  </p>
                ) : null}
                {currentTrainingJob?.status === "queued" ||
                currentTrainingJob?.status === "running" ? (
                  <Button
                    variant="danger"
                    disabled={
                      modelAction !== null ||
                      currentTrainingJob.cancelRequested
                    }
                    onClick={cancelProjectTraining}
                  >
                    {currentTrainingJob.cancelRequested ||
                    modelAction === "cancel"
                      ? "취소 요청됨"
                      : "학습 취소"}
                  </Button>
                ) : null}
                {currentTrainingJob?.status === "interrupted" ||
                currentTrainingJob?.status === "failed" ||
                currentTrainingJob?.status === "cancelled" ? (
                  <Button
                    disabled={modelAction !== null}
                    onClick={resumeProjectTraining}
                  >
                    {modelAction === "resume" ? "재개 요청 중" : "학습 재개"}
                  </Button>
                ) : null}
              </div>
            </div>
          </GlassPanel>

          <GlassPanel>
            <PanelHeader
              eyebrow="현재 확정 라벨"
              title="현재 라벨 분류"
              action={
                <Badge tone={stats?.currentAnswerCount ? "success" : "neutral"}>
                  최신 답변 {stats?.currentAnswerCount ?? 0}
                </Badge>
              }
            />
            <ul className="gate-list">
              <li className={stats?.partitions.approved ? "pass" : "warn"}>
                <Check size={17} />
                <span>승인 라벨</span>
                <strong>{stats?.partitions.approved ?? 0}</strong>
              </li>
              <li className={stats?.partitions.negative ? "pass" : "warn"}>
                <Check size={17} />
                <span>음성 라벨 · 해당 없음</span>
                <strong>{stats?.partitions.negative ?? 0}</strong>
              </li>
              <li className="warn">
                <AlertTriangle size={17} />
                <span>판단 보류</span>
                <strong>{stats?.partitions.held ?? 0}</strong>
              </li>
              <li className="warn">
                <AlertTriangle size={17} />
                <span>학습 제외</span>
                <strong>{stats?.partitions.excluded ?? 0}</strong>
              </li>
            </ul>
            <p className="model-message">
              질문 {stats?.questionCount ?? 0} · 답변 이력{" "}
              {stats?.answerRevisionCount ?? 0} · 출처 분석{" "}
              {stats?.sourceAnalysisJobCount ?? 0}건
            </p>
          </GlassPanel>
        </div>

        <div className="model-main">
          <GlassPanel className="model-comparison">
            <PanelHeader
              eyebrow="읽기 전용 출처 목록"
              title="외부 모델·데이터 소스 후보"
              action={
                <Badge
                  tone={
                    sourceError
                      ? "danger"
                      : sourceCatalog
                        ? "warning"
                        : "neutral"
                  }
                >
                  {sourceLoading
                    ? "불러오는 중"
                    : sourceError
                      ? "연결 오류"
                      : `${sourceCatalog?.count ?? 0}개 · 비활성`}
                </Badge>
              }
            />
            {sourceLoading ? (
              <p className="model-message" role="status">
                검토 전용 후보 메타데이터를 불러오는 중입니다.
              </p>
            ) : sourceError ? (
              <p className="model-message" role="alert">
                {sourceError}
              </p>
            ) : (
              <>
                <div className="model-table-wrap">
                  <table className="data-table model-table">
                    <thead>
                      <tr>
                        <th scope="col">소스</th>
                        <th scope="col">라이선스</th>
                        <th scope="col">용도</th>
                        <th scope="col">현재 결정 · 이유</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sourceCatalog?.candidates.map((candidate) => (
                        <tr
                          key={candidate.sourceId}
                          className={
                            candidate.catalogStatus === "rejected"
                              ? "regression"
                              : undefined
                          }
                        >
                          <th scope="row" title={candidate.originUrl}>
                            <strong>{candidate.displayName}</strong>
                            <p>{candidate.publisher}</p>
                          </th>
                          <td>
                            <code>{candidate.licenseSpdxId}</code>
                            {candidate.decisionReason.includes("OpenRAIL") ? (
                              <p>원 데이터셋 OpenRAIL 검토</p>
                            ) : null}
                          </td>
                          <td>{sourceCapabilityLabel(candidate)}</td>
                          <td title={candidate.decisionReason}>
                            <Badge
                              tone={
                                candidate.catalogStatus === "rejected"
                                  ? "danger"
                                  : candidate.catalogStatus ===
                                      "unverified_local"
                                    ? "danger"
                                  : candidate.catalogStatus ===
                                      "evaluation_complete"
                                    ? "accent"
                                    : "warning"
                              }
                            >
                              {sourceDecisionLabel(candidate)}
                            </Badge>
                            <p
                              className={
                                candidate.catalogStatus === "rejected" ||
                                candidate.catalogStatus === "unverified_local"
                                  ? "negative"
                                  : undefined
                              }
                            >
                              {sourceDecisionSummary(candidate)}
                            </p>
                            <span className="sr-only">
                              {candidate.decisionReason}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="notice notice--warning">
                  <ShieldAlert size={19} />
                  <div>
                    <strong>현재 프로젝트의 활성 모델이 아닙니다.</strong>
                    <p>
                      설치된 검증용 팩도 자동 활성화하지 않습니다. 이 조회는
                      다운로드·가중치 실행·자동 라벨을 하지 않으며 외부 소스를
                      프로젝트 학습과 분리합니다.
                    </p>
                  </div>
                </div>
              </>
            )}
          </GlassPanel>

          <div className="model-versions">
            <GlassPanel className="model-version-card">
              <BrainCircuit size={19} />
              <div>
                <strong>활성 모델</strong>
                <Badge tone={activeModel ? "success" : "neutral"}>
                  {activeModel ? "ACTIVE" : "미학습"}
                </Badge>
              </div>
              <p>
                {activeModel
                  ? `${projectModelState?.activeModelVersion ?? activeProject?.activeModelVersion} · ${projectModelRoleLabel(activeModel)}`
                  : "전용 모델 없음 · 후보 수집 단계"}{" "}
                · 자동 승격 없음
              </p>
              <code>
                {activeModel
                  ? `고정 검증 ${activeModel.fixedValidationSha256.slice(0, 10)}… · 산출물 ${activeModel.primaryArtifactSha256.slice(0, 10)}…`
                  : "프로젝트 후보 모델을 검증·승격하기 전"}
              </code>
              {projectModelState?.previousModelId ? (
                <Button
                  variant="ghost"
                  disabled={modelAction !== null}
                  onClick={() => transitionProjectModel("rollback")}
                >
                  {modelAction === "rollback" ? "롤백 중" : "이전 모델로 롤백"}
                </Button>
              ) : null}
            </GlassPanel>
            <GlassPanel className="model-version-card">
              <Database size={19} />
              <div>
                <strong>
                  {latestDataset
                    ? `DatasetVersion ${versions.length}`
                    : "데이터셋 없음"}
                </strong>
                <Badge tone={validationReady ? "success" : "warning"}>
                  {validationReadinessKnown
                    ? `검증 ${validationCountEntries.length}유형`
                    : "검증 확인 전"}
                </Badge>
              </div>
              <p>
                {latestDataset
                  ? `TRAIN ${latestDataset.trainCount} · validation ${latestDataset.validationCount} · 제외 ${latestDataset.excludedCount} · 독립 조사 QA ${lifecycle?.modelIterations.surveyQa.length ?? 0}회`
                  : "사용자 확정 답변으로 첫 스냅샷을 만드세요."}
              </p>
              <code>
                {latestDataset && latestSurveyQa
                  ? `최근 QA TP ${latestSurveyQa.truePositiveCount} · FP ${latestSurveyQa.falsePositiveCount} · FN ${latestSurveyQa.falseNegativeCount} · 보류 ${latestSurveyQa.heldCount}`
                  : latestDataset
                    ? `${formatCreatedAt(latestDataset.createdAt)} · 무결성 ${latestDataset.datasetHash.slice(0, 10)}…`
                  : "아직 저장된 DatasetVersion이 없습니다."}
              </code>
            </GlassPanel>
            <GlassPanel className="model-version-card">
              <Sparkles size={19} />
              <div>
                <strong>후보 모델</strong>
                <Badge
                  tone={
                    candidateModel
                      ? candidateModel.validationDecision === "eligible"
                        ? "warning"
                        : "danger"
                      : rejectedModelCount > 0
                        ? "danger"
                        : "neutral"
                  }
                >
                  {candidateModel
                    ? "사용자 결정 대기"
                    : rejectedModelCount > 0
                      ? `거절 ${rejectedModelCount}`
                      : "없음"}
                </Badge>
              </div>
              <p>
                {candidateModel
                  ? `${candidateModel.modelVersion} · ${projectModelRoleLabel(candidateModel)} · 검증 ${candidateModel.validationDecision === "eligible" ? "통과" : "거절"}`
                  : currentTrainingJob?.status === "succeeded"
                    ? "후보 모델 상태를 다시 동기화하세요."
                    : "학습·고정 검증을 통과한 모델만 후보가 됩니다."}
              </p>
              <code>
                {candidateModel
                  ? `Dataset ${candidateModel.datasetHash.slice(0, 10)}… · 사용자 승인 3·4분기 학습 사례 ${candidateModel.auxiliaryReferenceCount}`
                  : "활성 모델은 자동으로 바뀌지 않습니다."}
              </code>
              {candidateModel ? (
                <div className="header-actions">
                  <Button
                    variant="positive"
                    disabled={
                      modelAction !== null ||
                      candidateModel.validationDecision !== "eligible"
                    }
                    onClick={() => transitionProjectModel("promote")}
                  >
                    {modelAction === "promote" ? "승격 중" : "활성 모델로 승격"}
                  </Button>
                  <Button
                    variant="danger"
                    disabled={modelAction !== null}
                    onClick={() => transitionProjectModel("reject")}
                  >
                    {modelAction === "reject" ? "거절 중" : "후보 거절"}
                  </Button>
                </div>
              ) : null}
            </GlassPanel>
          </div>
          <p className="model-message" role={error ? "alert" : "status"}>
            {error ??
              (projectCatalog.loading
                ? "프로젝트 목록을 불러오는 중입니다."
                : message)}
          </p>
        </div>
      </div>
      {validationDialogOpen && currentFixedValidation ? (
        <div
          className="weak-case-standardization-backdrop"
          role="presentation"
          onMouseDown={(event) => {
            if (
              event.target === event.currentTarget &&
              modelAction !== "extend-validation"
            ) {
              setValidationDialogOpen(false);
            }
          }}
        >
          <section
            className="fixed-validation-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="fixed-validation-dialog-title"
            aria-describedby="fixed-validation-dialog-boundary"
          >
            <header className="fixed-validation-dialog__header">
              <div>
                <p className="eyebrow">불변 고정 검증 세트</p>
                <h2 id="fixed-validation-dialog-title">
                  v{currentFixedValidation.validationSetVersion}에서 새 유형 확장
                </h2>
              </div>
              <Button
                variant="ghost"
                disabled={modelAction === "extend-validation"}
                onClick={() => setValidationDialogOpen(false)}
                aria-label="고정 검증 확장 창 닫기"
              >
                <X size={16} /> 닫기
              </Button>
            </header>

            <div
              id="fixed-validation-dialog-boundary"
              className="fixed-validation-dialog__boundary"
            >
              <ShieldAlert size={18} />
              <p>
                부모 truth는 수정·삭제하지 않습니다. 새 문제 유형과 독립
                validation AnalysisJob만 별도 승인하며, 유형별 사용자 확정
                양성 30건을 서버가 다시 확인합니다.
              </p>
            </div>

            <div className="fixed-validation-dialog__grid">
              <section aria-label="현재 고정 검증 세트">
                <h3>현재 부모 세트</h3>
                <dl className="fixed-validation-lineage">
                  <div>
                    <dt>버전</dt>
                    <dd>v{currentFixedValidation.validationSetVersion}</dd>
                  </div>
                  <div>
                    <dt>membership</dt>
                    <dd title={currentFixedValidation.membershipSha256}>
                      {currentFixedValidation.membershipSha256.slice(0, 14)}…
                    </dd>
                  </div>
                  <div>
                    <dt>validation</dt>
                    <dd title={currentFixedValidation.fixedValidationSha256}>
                      {currentFixedValidation.fixedValidationSha256.slice(
                        0,
                        14,
                      )}
                      …
                    </dd>
                  </div>
                </dl>
                <ul className="fixed-validation-counts">
                  {Object.entries(
                    currentFixedValidation.validationSampleCounts,
                  ).map(([issueType, count]) => (
                    <li key={issueType}>
                      <span>
                        {ISSUE_TYPE_LABELS[
                          issueType as ProjectModelIssueType
                        ]}
                      </span>
                      <strong className={count >= 30 ? "positive" : "negative"}>
                        {count} / 30
                      </strong>
                    </li>
                  ))}
                </ul>
              </section>

              <section aria-label="새 검증 유형 승인">
                <label className="fixed-validation-field">
                  <span>새 issue type</span>
                  <select
                    value={extensionIssueType}
                    disabled={modelAction === "extend-validation"}
                    onChange={(event) =>
                      setExtensionIssueType(
                        event.target.value as ProjectModelIssueType,
                      )
                    }
                  >
                    {availableExtensionIssueTypes.map((issueType) => (
                      <option key={issueType} value={issueType}>
                        {ISSUE_TYPE_LABELS[issueType]} · {issueType}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="fixed-validation-field">
                  <span>추가 validation AnalysisJob UUID</span>
                  <textarea
                    value={extensionAnalysisJobs}
                    disabled={modelAction === "extend-validation"}
                    onChange={(event) =>
                      setExtensionAnalysisJobs(event.target.value)
                    }
                    rows={3}
                    spellCheck={false}
                    placeholder="UUID를 줄바꿈 또는 쉼표로 입력"
                  />
                  <small>
                    부모 영상과 겹치지 않는 사용자 확정 validation
                    AnalysisJob만 입력합니다.
                  </small>
                </label>
                <div className="fixed-validation-new-count">
                  <span>{ISSUE_TYPE_LABELS[extensionIssueType]}</span>
                  <strong>
                    서버 확인 대기 / 30
                  </strong>
                </div>
                <label className="fixed-validation-confirmation">
                  <input
                    type="checkbox"
                    checked={extensionConfirmed}
                    disabled={modelAction === "extend-validation"}
                    onChange={(event) =>
                      setExtensionConfirmed(event.target.checked)
                    }
                  />
                  <span>
                    위 부모 버전과 두 해시를 확인했으며 불변 자식 버전
                    생성을 명시적으로 승인합니다.
                  </span>
                </label>
              </section>
            </div>

            <footer className="fixed-validation-dialog__footer">
              <p role={extensionError ? "alert" : "status"}>
                {extensionError ??
                  "중복·stale·부족 검사는 서버가 DatasetVersion과 원본 계보로 최종 판정합니다."}
              </p>
              <Button
                variant="primary"
                disabled={
                  !extensionConfirmed ||
                  extensionAnalysisJobs.trim().length === 0 ||
                  availableExtensionIssueTypes.length === 0 ||
                  modelAction !== null
                }
                onClick={extendFixedValidationAndTrain}
              >
                {modelAction === "extend-validation"
                  ? "서버 검증 중"
                  : "확장 승인 후 학습 사전점검"}
              </Button>
            </footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}
