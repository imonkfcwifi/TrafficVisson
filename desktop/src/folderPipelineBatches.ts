const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;

export type FolderBatchStatus =
  | "enqueuing"
  | "queued"
  | "active"
  | "awaiting_review"
  | "completed"
  | "blocked_storage"
  | "partial_failed"
  | "failed";

export interface FolderPipelineBatchFile {
  readonly order: number;
  readonly fileName: string;
  readonly sizeBytes: number;
  readonly intakeState: string;
  readonly durationSeconds: number | null;
  readonly estimatedDenseFrames: number;
  readonly estimatedOutputBytes: number;
  readonly validationAttempts: number;
  readonly enqueueAttempts: number;
  readonly pipelineJobId: string | null;
  readonly pipelineStatus: string | null;
  readonly idempotentReplay: boolean;
  readonly lastErrorCode: string | null;
  readonly recoveredAfterRestart: boolean;
  readonly retryAvailable: boolean;
  readonly resumeAvailable: boolean;
}

export interface FolderStorageStageEstimate {
  readonly stage: string;
  readonly categories: readonly string[];
  readonly plannedFrameCount: number;
  readonly averageBytesPerFrameCeiling: number;
  readonly fixedBytes: number;
  readonly hardCeilingBytes: number | null;
  readonly estimatedBytes: number;
}

export interface FolderPipelineBatch {
  readonly batchId: string;
  readonly projectId: string;
  readonly snapshotId: string;
  readonly snapshotDigest: string;
  readonly idempotencyKey: string;
  readonly inputFingerprint: string;
  readonly outputLocationId: string;
  readonly outputPreflight: {
    readonly state: "pending" | "computing" | "ready" | "blocked";
    readonly capacityModelVersion: string | null;
    readonly denseSamplingFps: number;
    readonly candidateRescanFps: number | null;
    readonly planningAssumptions: {
      readonly candidateWindowRatioBasisPoints: number;
      readonly candidateDensitySeconds: number;
      readonly minimumPlannedCandidates: number;
      readonly maximumPlannedCandidates: number;
      readonly maximumPlannedWindowSeconds: number;
    } | null;
    readonly plannedCandidateWindowSeconds: number;
    readonly estimatedJpegBytesPerFrame: number;
    readonly derivedOutputOverheadRatio: number;
    readonly durationSeconds: number;
    readonly denseFrameCount: number;
    readonly candidateRescanFrameCount: number;
    readonly plannedCandidateCount: number;
    readonly averageFrameBytesCeiling: number;
    readonly stageEstimates: readonly FolderStorageStageEstimate[];
    readonly separateCapacityPreflights: readonly string[];
    readonly estimatedArtifactBytes: number;
    readonly safetyReserveBytes: number;
    readonly requiredFreeBytes: number;
    readonly availableFreeBytes: number | null;
    readonly errorCode: string | null;
  };
  readonly status: FolderBatchStatus;
  readonly enqueueCompleted: boolean;
  readonly recoveredAfterRestart: boolean;
  readonly retryCount: number;
  readonly resumeCount: number;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly counts: {
    readonly total: number;
    readonly pending: number;
    readonly validationFailed: number;
    readonly enqueueFailed: number;
    readonly storageBlocked: number;
    readonly queuedOrRunning: number;
    readonly awaitingReview: number;
    readonly completed: number;
    readonly pipelineFailed: number;
  };
  readonly files: readonly FolderPipelineBatchFile[];
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} 응답 형식이 올바르지 않습니다.`);
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
  label: string,
) {
  const expected = new Set(keys);
  if (
    Object.keys(value).length !== expected.size ||
    Object.keys(value).some((key) => !expected.has(key))
  ) {
    throw new Error(`${label} 응답에 허용되지 않은 필드가 있습니다.`);
  }
}

function stringValue(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length < 1) {
    throw new Error(`${label} 값이 올바르지 않습니다.`);
  }
  return value;
}

function uuid(value: unknown, label: string): string {
  const parsed = stringValue(value, label);
  if (!UUID_PATTERN.test(parsed)) {
    throw new Error(`${label} 식별자가 올바르지 않습니다.`);
  }
  return parsed;
}

function sha256(value: unknown, label: string): string {
  const parsed = stringValue(value, label);
  if (!SHA256_PATTERN.test(parsed)) {
    throw new Error(`${label} 무결성 값이 올바르지 않습니다.`);
  }
  return parsed;
}

function count(value: unknown, label: string): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0
  ) {
    throw new Error(`${label} 개수가 올바르지 않습니다.`);
  }
  return value;
}

function finiteNumber(value: unknown, label: string): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0
  ) {
    throw new Error(`${label} 값이 올바르지 않습니다.`);
  }
  return value;
}

function booleanValue(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`${label} 상태가 올바르지 않습니다.`);
  }
  return value;
}

function nullableString(value: unknown, label: string): string | null {
  if (value === null) return null;
  return stringValue(value, label);
}

function parseOutputPreflight(
  value: unknown,
  schemaVersion: string,
): FolderPipelineBatch["outputPreflight"] {
  const source = record(value, "결과 저장공간 사전점검");
  const modernV2 =
    schemaVersion === "trafficvisson.folder-pipeline-batch.v5";
  const modernV1 =
    schemaVersion === "trafficvisson.folder-pipeline-batch.v4";
  const modern = modernV1 || modernV2;
  exactKeys(
    source,
    modernV2
      ? [
          "state",
          "capacity_model_version",
          "dense_sampling_fps",
          "candidate_rescan_fps",
          "planning_assumptions",
          "planned_candidate_window_seconds",
          "estimated_jpeg_bytes_per_frame",
          "derived_output_overhead_ratio",
          "duration_seconds",
          "dense_frame_count",
          "candidate_rescan_frame_count",
          "planned_candidate_count",
          "average_frame_bytes_ceiling",
          "stage_estimates",
          "separate_capacity_preflights",
          "estimated_artifact_bytes",
          "safety_reserve_bytes",
          "required_free_bytes",
          "available_free_bytes",
          "error_code",
        ]
      : modernV1
        ? [
            "state",
            "capacity_model_version",
            "dense_sampling_fps",
            "candidate_rescan_fps",
            "estimated_jpeg_bytes_per_frame",
            "derived_output_overhead_ratio",
            "duration_seconds",
            "dense_frame_count",
            "candidate_rescan_frame_count",
            "planned_candidate_count",
            "average_frame_bytes_ceiling",
            "stage_estimates",
            "separate_capacity_preflights",
            "estimated_artifact_bytes",
            "safety_reserve_bytes",
            "required_free_bytes",
            "available_free_bytes",
            "error_code",
          ]
      : [
          "state",
          "dense_sampling_fps",
          "estimated_jpeg_bytes_per_frame",
          "derived_output_overhead_ratio",
          "duration_seconds",
          "dense_frame_count",
          "estimated_artifact_bytes",
          "safety_reserve_bytes",
          "required_free_bytes",
          "available_free_bytes",
          "error_code",
        ],
    "결과 저장공간 사전점검",
  );
  const state = stringValue(source.state, "저장공간 사전점검 상태");
  if (!["pending", "computing", "ready", "blocked"].includes(state)) {
    throw new Error("저장공간 사전점검 상태가 올바르지 않습니다.");
  }
  let stageEstimates: FolderStorageStageEstimate[] = [];
  let separateCapacityPreflights: string[] = [];
  if (modern) {
    if (
      source.capacity_model_version !==
        (modernV2
          ? "trafficvisson.pipeline-capacity-plan.v2"
          : "trafficvisson.pipeline-capacity-plan.v1") ||
      !Array.isArray(source.stage_estimates) ||
      ![0, 12].includes(source.stage_estimates.length) ||
      !Array.isArray(source.separate_capacity_preflights) ||
      source.separate_capacity_preflights.length !== 2
    ) {
      throw new Error("저장공간 용량 계획이 올바르지 않습니다.");
    }
    const stageOrder = [
      "intake_validation",
      "sha256",
      "metadata_gps_preflight",
      "dense_index_2fps",
      "candidate_rescan_5_10fps",
      "detection_ocr",
      "sam2_segmentation_tracking",
      "temporal_merge",
      "similar_case_search",
      "vision_proposal",
      "question_queue",
      "derived_outputs",
    ];
    stageEstimates = source.stage_estimates.map((value, index) => {
      const row = record(value, "단계별 저장공간 계획");
      exactKeys(
        row,
        [
          "stage",
          "categories",
          "planned_frame_count",
          "average_bytes_per_frame_ceiling",
          "fixed_bytes",
          "hard_ceiling_bytes",
          "estimated_bytes",
        ],
        "단계별 저장공간 계획",
      );
      if (
        row.stage !== stageOrder[index] ||
        !Array.isArray(row.categories) ||
        row.categories.length < 1 ||
        row.categories.some(
          (category) =>
            typeof category !== "string" ||
            !/^[a-z0-9][a-z0-9-]{1,63}$/.test(category),
        )
      ) {
        throw new Error("단계별 저장공간 범위가 올바르지 않습니다.");
      }
      return {
        stage: String(row.stage),
        categories: [...row.categories] as string[],
        plannedFrameCount: count(
          row.planned_frame_count,
          "계획 프레임",
        ),
        averageBytesPerFrameCeiling: count(
          row.average_bytes_per_frame_ceiling,
          "프레임 평균 상한",
        ),
        fixedBytes: count(row.fixed_bytes, "고정 산출물 상한"),
        hardCeilingBytes:
          row.hard_ceiling_bytes === null
            ? null
            : count(row.hard_ceiling_bytes, "절대 엔진 상한"),
        estimatedBytes: count(row.estimated_bytes, "단계 예상 크기"),
      };
    });
    separateCapacityPreflights =
      source.separate_capacity_preflights.map((value) =>
        stringValue(value, "별도 용량 점검"),
      );
    if (
      separateCapacityPreflights.join("|") !==
      "project_model_training|hwp_com_application_and_48_page_render"
    ) {
      throw new Error("별도 승인 용량 점검 범위가 올바르지 않습니다.");
    }
  }
  let planningAssumptions:
    | NonNullable<
        FolderPipelineBatch["outputPreflight"]["planningAssumptions"]
      >
    | null = null;
  if (modernV2) {
    const assumptions = record(
      source.planning_assumptions,
      "용량 계획 가정",
    );
    exactKeys(
      assumptions,
      [
        "candidate_window_ratio_basis_points",
        "candidate_density_seconds",
        "minimum_planned_candidates",
        "maximum_planned_candidates",
        "maximum_planned_window_seconds",
      ],
      "용량 계획 가정",
    );
    planningAssumptions = {
      candidateWindowRatioBasisPoints: count(
        assumptions.candidate_window_ratio_basis_points,
        "후보 구간 비율",
      ),
      candidateDensitySeconds: count(
        assumptions.candidate_density_seconds,
        "후보 밀도",
      ),
      minimumPlannedCandidates: count(
        assumptions.minimum_planned_candidates,
        "최소 계획 후보",
      ),
      maximumPlannedCandidates: count(
        assumptions.maximum_planned_candidates,
        "최대 계획 후보",
      ),
      maximumPlannedWindowSeconds: count(
        assumptions.maximum_planned_window_seconds,
        "최대 후보 구간",
      ),
    };
  }
  return {
    state: state as FolderPipelineBatch["outputPreflight"]["state"],
    capacityModelVersion: modern
      ? String(source.capacity_model_version)
      : null,
    denseSamplingFps: finiteNumber(
      source.dense_sampling_fps,
      "밀집 프레임 추출 빈도",
    ),
    candidateRescanFps: modern
      ? count(source.candidate_rescan_fps, "후보 재분석 빈도")
      : null,
    planningAssumptions,
    plannedCandidateWindowSeconds: modernV2
      ? finiteNumber(
          source.planned_candidate_window_seconds,
          "계획 후보 구간",
        )
      : 0,
    estimatedJpegBytesPerFrame: count(
      source.estimated_jpeg_bytes_per_frame,
      "프레임당 예상 크기",
    ),
    derivedOutputOverheadRatio: finiteNumber(
      source.derived_output_overhead_ratio,
      "파생 결과 오버헤드 비율",
    ),
    durationSeconds: finiteNumber(source.duration_seconds, "총 영상 길이"),
    denseFrameCount: count(source.dense_frame_count, "예상 2fps 프레임"),
    candidateRescanFrameCount: modern
      ? count(source.candidate_rescan_frame_count, "후보 재분석 프레임")
      : 0,
    plannedCandidateCount: modern
      ? count(source.planned_candidate_count, "계획 후보")
      : 0,
    averageFrameBytesCeiling: modern
      ? count(source.average_frame_bytes_ceiling, "프레임 평균 상한")
      : 0,
    stageEstimates,
    separateCapacityPreflights,
    estimatedArtifactBytes: count(
      source.estimated_artifact_bytes,
      "예상 결과 크기",
    ),
    safetyReserveBytes: count(
      source.safety_reserve_bytes,
      "안전 여유 공간",
    ),
    requiredFreeBytes: count(
      source.required_free_bytes,
      "필요 여유 공간",
    ),
    availableFreeBytes:
      source.available_free_bytes === null
        ? null
        : count(source.available_free_bytes, "현재 여유 공간"),
    errorCode: nullableString(source.error_code, "사전점검 오류"),
  };
}

export function parseFolderPipelineBatch(
  value: unknown,
): FolderPipelineBatch {
  const source = record(value, "폴더 배치");
  exactKeys(
    source,
    [
      "schema_version",
      "batch_id",
      "project_id",
      "snapshot_id",
      "snapshot_digest",
      "idempotency_key",
      "input_fingerprint",
      "output_location_id",
      "output_preflight",
      "status",
      "enqueue_completed",
      "recovered_after_restart",
      "retry_count",
      "resume_count",
      "created_at",
      "updated_at",
      "counts",
      "files",
    ],
    "폴더 배치",
  );
  if (
    ![
      "trafficvisson.folder-pipeline-batch.v2",
      "trafficvisson.folder-pipeline-batch.v3",
      "trafficvisson.folder-pipeline-batch.v4",
      "trafficvisson.folder-pipeline-batch.v5",
    ].includes(String(source.schema_version))
  ) {
    throw new Error("지원하지 않는 폴더 배치 응답입니다.");
  }
  const status = stringValue(source.status, "폴더 배치 상태");
  if (
    ![
      "enqueuing",
      "queued",
      "active",
      "awaiting_review",
      "completed",
      "blocked_storage",
      "partial_failed",
      "failed",
    ].includes(status)
  ) {
    throw new Error("폴더 배치 상태가 올바르지 않습니다.");
  }
  if (!Array.isArray(source.files) || source.files.length < 1) {
    throw new Error("폴더 배치 파일 목록이 비어 있습니다.");
  }
  const files = source.files.map((value, index) => {
    const row = record(value, "폴더 배치 파일");
    exactKeys(
      row,
      [
        "order",
        "file_name",
        "size_bytes",
        "intake_state",
        "duration_seconds",
        "estimated_dense_frames",
        "estimated_output_bytes",
        "validation_attempts",
        "enqueue_attempts",
        "pipeline_job_id",
        "pipeline_status",
        "idempotent_replay",
        "last_error_code",
        "recovered_after_restart",
        "retry_available",
        "resume_available",
      ],
      "폴더 배치 파일",
    );
    const fileName = stringValue(row.file_name, "파일명");
    if (
      /[\\/]/.test(fileName) ||
      !/\.(?:mp4|mov)$/i.test(fileName) ||
      row.order !== index + 1
    ) {
      throw new Error("폴더 배치 파일 순서 또는 이름이 올바르지 않습니다.");
    }
    return {
      order: count(row.order, "파일 순서"),
      fileName,
      sizeBytes: count(row.size_bytes, "파일 크기"),
      intakeState: stringValue(row.intake_state, "입력 상태"),
      durationSeconds:
        row.duration_seconds === null
          ? null
          : finiteNumber(row.duration_seconds, "영상 길이"),
      estimatedDenseFrames: count(
        row.estimated_dense_frames,
        "예상 2fps 프레임",
      ),
      estimatedOutputBytes: count(
        row.estimated_output_bytes,
        "예상 결과 크기",
      ),
      validationAttempts: count(
        row.validation_attempts,
        "검증 시도",
      ),
      enqueueAttempts: count(row.enqueue_attempts, "등록 시도"),
      pipelineJobId:
        row.pipeline_job_id === null
          ? null
          : uuid(row.pipeline_job_id, "파이프라인 작업"),
      pipelineStatus:
        row.pipeline_status === null
          ? null
          : stringValue(row.pipeline_status, "파이프라인 상태"),
      idempotentReplay: booleanValue(
        row.idempotent_replay,
        "멱등 복구",
      ),
      lastErrorCode: nullableString(
        row.last_error_code,
        "마지막 오류",
      ),
      recoveredAfterRestart: booleanValue(
        row.recovered_after_restart,
        "재시작 복구",
      ),
      retryAvailable: booleanValue(row.retry_available, "재시도"),
      resumeAvailable: booleanValue(row.resume_available, "재개"),
    };
  });
  const countsSource = record(source.counts, "폴더 배치 집계");
  exactKeys(
    countsSource,
    [
      "total",
      "pending",
      "validation_failed",
      "enqueue_failed",
      "storage_blocked",
      "queued_or_running",
      "awaiting_review",
      "completed",
      "pipeline_failed",
    ],
    "폴더 배치 집계",
  );
  const counts = {
    total: count(countsSource.total, "전체"),
    pending: count(countsSource.pending, "검증 대기"),
    validationFailed: count(
      countsSource.validation_failed,
      "검증 실패",
    ),
    enqueueFailed: count(countsSource.enqueue_failed, "등록 실패"),
    storageBlocked: count(
      countsSource.storage_blocked,
      "저장 공간 차단",
    ),
    queuedOrRunning: count(
      countsSource.queued_or_running,
      "처리 중",
    ),
    awaitingReview: count(
      countsSource.awaiting_review,
      "질문 대기",
    ),
    completed: count(countsSource.completed, "완료"),
    pipelineFailed: count(
      countsSource.pipeline_failed,
      "파이프라인 실패",
    ),
  };
  if (counts.total !== files.length) {
    throw new Error("폴더 배치 집계가 파일 목록과 일치하지 않습니다.");
  }
  return {
    batchId: uuid(source.batch_id, "폴더 배치"),
    projectId: uuid(source.project_id, "프로젝트"),
    snapshotId: uuid(source.snapshot_id, "미디어 스냅샷"),
    snapshotDigest: sha256(source.snapshot_digest, "스냅샷"),
    idempotencyKey: stringValue(source.idempotency_key, "멱등 키"),
    inputFingerprint: sha256(source.input_fingerprint, "입력"),
    outputLocationId: stringValue(
      source.output_location_id,
      "결과 저장 위치",
    ),
    outputPreflight: parseOutputPreflight(
      source.output_preflight,
      String(source.schema_version),
    ),
    status: status as FolderBatchStatus,
    enqueueCompleted: booleanValue(
      source.enqueue_completed,
      "등록 완료",
    ),
    recoveredAfterRestart: booleanValue(
      source.recovered_after_restart,
      "재시작 복구",
    ),
    retryCount: count(source.retry_count, "재시도"),
    resumeCount: count(source.resume_count, "재개"),
    createdAt: stringValue(source.created_at, "생성 시각"),
    updatedAt: stringValue(source.updated_at, "갱신 시각"),
    counts,
    files,
  };
}

export function parseFolderPipelineBatchStart(value: unknown): {
  readonly batch: FolderPipelineBatch;
  readonly idempotentReplay: boolean;
} {
  const source = record(value, "폴더 배치 시작");
  exactKeys(
    source,
    ["batch", "idempotent_replay"],
    "폴더 배치 시작",
  );
  return {
    batch: parseFolderPipelineBatch(source.batch),
    idempotentReplay: booleanValue(
      source.idempotent_replay,
      "멱등 복구",
    ),
  };
}

export function parseFolderPipelineBatchList(
  value: unknown,
): FolderPipelineBatch[] {
  const source = record(value, "폴더 배치 목록");
  exactKeys(
    source,
    ["schema_version", "batches"],
    "폴더 배치 목록",
  );
  if (
    source.schema_version !==
      "trafficvisson.folder-pipeline-batch-list.v1" ||
    !Array.isArray(source.batches)
  ) {
    throw new Error("폴더 배치 목록 응답이 올바르지 않습니다.");
  }
  return source.batches.map(parseFolderPipelineBatch);
}
