/// <reference types="vite/client" />

interface TrafficSuperVisionVideoFileMetadata {
  readonly name: string;
  readonly extension: "mp4" | "mov";
  readonly sizeBytes: number;
  readonly lastModifiedMs: number;
}

interface TrafficSuperVisionVideoSelection {
  readonly selectionId: string | null;
  readonly files: readonly TrafficSuperVisionVideoFileMetadata[];
}

interface TrafficSuperVisionDocumentSelection {
  readonly selectionId: string | null;
  readonly file: null | {
    readonly name: string;
    readonly extension: "hwp" | "hwpx";
    readonly sizeBytes: number;
    readonly lastModifiedMs: number;
  };
}

interface TrafficSuperVisionGpxSelection {
  readonly selectionId: string | null;
  readonly file: null | {
    readonly name: string;
    readonly extension: "gpx";
    readonly sizeBytes: number;
    readonly lastModifiedMs: number;
  };
}

interface TrafficSuperVisionDirectorySelection {
  readonly selectionId: string | null;
  readonly directory: null | {
    readonly name: string;
  };
}

type TrafficSuperVisionAutoLabelingIssueType =
  | "pothole"
  | "road_surface_damage"
  | "road_debris"
  | "median_barrier_damage"
  | "sign_damage"
  | "sign_vegetation_occlusion"
  | "roadside_mowing_required"
  | "guide_sign_identity"
  | "tunnel_identity"
  | "tunnel_name_occlusion"
  | "lane_marking_faded"
  | "colored_guidance_faded"
  | "other";

interface TrafficSuperVisionAutoLabelingCompileRequest {
  readonly projectId: string;
  readonly command: string;
  readonly issueType?: TrafficSuperVisionAutoLabelingIssueType;
}

interface TrafficSuperVisionAutoLabelingRunRequest {
  readonly projectId: string;
  readonly jobId?: string;
  readonly recipeId: string;
  readonly command: string;
  readonly issueType: TrafficSuperVisionAutoLabelingIssueType;
  readonly scope: "current_job" | "project";
  readonly mode: "proposal_only" | "assisted_auto";
  readonly userConfirmed: true;
}

interface TrafficSuperVisionProjectRequest {
  readonly projectId: string;
}

interface TrafficSuperVisionWeakCaseSearchRequest
  extends TrafficSuperVisionProjectRequest {
  readonly issueType?:
    | "pothole"
    | "road_surface_damage"
    | "road_debris"
    | "median_barrier_damage"
    | "sign_damage"
    | "sign_vegetation_occlusion"
    | "roadside_mowing_required"
    | "guide_sign_identity"
    | "tunnel_identity"
    | "tunnel_name_occlusion"
    | "lane_marking_faded"
    | "colored_guidance_faded";
  readonly queryText?: string;
  readonly limit: 3 | 4 | 5;
}

interface TrafficSuperVisionIncidentListRequest
  extends TrafficSuperVisionProjectRequest {
  readonly analysisJobId?: string;
  readonly includeInactive?: boolean;
}

interface TrafficSuperVisionDatasetCreateRequest {
  readonly projectId: string;
  readonly userConfirmed: true;
}

interface TrafficSuperVisionDenseIndexStartRequest {
  readonly projectId: string;
  readonly analysisJobId: string;
  readonly samplingFps: 1 | 2 | 3 | 4 | 5;
  readonly confirmReviewOnly: true;
}

interface TrafficSuperVisionDenseIndexJobRequest {
  readonly projectId: string;
  readonly jobId: string;
}

interface TrafficSuperVisionPipelineProfileRequest {
  readonly projectId: string;
  readonly questionPolicy:
    | "learning_assist"
    | "full_review"
    | "quick_review";
  readonly reviewMode: "precision" | "quick";
  readonly defaultTemplateId: string;
  readonly defaultTemplateSha256: string;
  readonly detectorPackId: string;
  readonly ocrPackId: string;
  readonly sam2PackId: string;
  readonly reportProvider: "codex" | "claude" | "local" | "none";
  readonly outputLocationId: string;
  readonly candidateRescanFps: 5 | 6 | 7 | 8 | 9 | 10;
}

interface TrafficSuperVisionPipelineStartRequest {
  readonly projectId: string;
  readonly snapshotId: string;
  readonly fileName: string;
  readonly rightsConfirmed: true;
  readonly idempotencyKey: string;
}

interface TrafficSuperVisionPipelineJobRequest {
  readonly projectId: string;
  readonly jobId: string;
}

interface TrafficSuperVisionPipelineReviewCompleteRequest
  extends TrafficSuperVisionPipelineJobRequest {
  readonly reviewCompletionId: string;
}

interface TrafficSuperVisionMaskRefinementIdentityRequest {
  readonly projectId: string;
  readonly jobId: string;
  readonly questionId: string;
  readonly candidateId: string;
}

interface TrafficSuperVisionMaskRefinementRequest
  extends TrafficSuperVisionMaskRefinementIdentityRequest {
  readonly click: {
    readonly x: number;
    readonly y: number;
  };
}

interface TrafficSuperVisionMaskRefinementCapabilityResponse {
  readonly schema_version:
    "trafficvisson.sam2-mask-refinement-capability.v1";
  readonly project_id: string;
  readonly analysis_job_id: string;
  readonly question_id: string;
  readonly candidate_id: string;
  readonly state: "ready" | "model_unavailable";
  readonly error_code: "model_pack_not_configured" | null;
  readonly message: string;
  readonly click_prompt_enabled: boolean;
  readonly model_manifest: null | {
    readonly pack_id: string;
    readonly pack_version: string;
    readonly manifest_sha256: string;
  };
}

interface TrafficSuperVisionMaskRefinementResponse {
  readonly schema_version: "trafficvisson.sam2-mask-refinement.v1";
  readonly project_id: string;
  readonly analysis_job_id: string;
  readonly question_id: string;
  readonly candidate_id: string;
  readonly status: "refined" | "model_unavailable";
  readonly error_code: "model_pack_not_configured" | null;
  readonly message: string;
  readonly refinement_id: string | null;
  readonly click: { readonly x: number; readonly y: number };
  readonly polygon: readonly (readonly [number, number])[] | null;
  readonly normalized_box: null | {
    readonly x: number;
    readonly y: number;
    readonly width: number;
    readonly height: number;
  };
  readonly mask_artifact_ref: string | null;
  readonly tracked_frame_refs: readonly string[];
  readonly input_sha256: string | null;
  readonly output_sha256: string | null;
  readonly model_manifest: TrafficSuperVisionMaskRefinementCapabilityResponse["model_manifest"];
  readonly requires_user_confirmation: true;
  readonly automatic_annotation_allowed: false;
  readonly automatic_incident_creation_allowed: false;
  readonly training_membership_allowed: false;
}

interface TrafficSuperVisionReportDocumentStructureSummary {
  readonly documentSnapshotId: string;
  readonly fieldTargets: readonly string[];
  readonly cellTargets: readonly string[];
  readonly tableTargets: readonly string[];
  readonly imageTargets: readonly string[];
  readonly mapTargets: readonly string[];
  readonly sectionHeadings: readonly string[];
}

interface TrafficSuperVisionReportGenerateRequest {
  readonly projectId: string;
  readonly analysisJobId: string;
  readonly sourceFileSha256: string;
  readonly pipelineJobId?: string;
  readonly pipelineInputFingerprint?: string;
  readonly pipelineProfileSha256?: string;
  readonly provider: "codex" | "claude";
  readonly consent: boolean;
  readonly projectLabel?: string;
  /**
   * Incident content is deliberately absent. The bridge resolves these IDs
   * against its project/job-scoped active incident catalog.
   */
  readonly incidentIds: readonly string[];
  readonly documentStructureSummary:
    TrafficSuperVisionReportDocumentStructureSummary;
  readonly mapRefs: readonly string[];
}

interface TrafficSuperVisionDesktopApi {
  health(): Promise<unknown>;
  dialogs: {
    selectVideos(): Promise<TrafficSuperVisionVideoSelection>;
    selectGpxFile(): Promise<TrafficSuperVisionGpxSelection>;
    selectHwpDocument(): Promise<TrafficSuperVisionDocumentSelection>;
    selectExportDirectory(): Promise<TrafficSuperVisionDirectorySelection>;
  };
  /**
   * Compatibility adapter for the current import screen. Values are safe
   * display names returned by the native picker, never filesystem paths.
   */
  chooseVideoFiles(): Promise<string[]>;
  projects: {
    create(request: unknown): Promise<unknown>;
    get(request: unknown): Promise<unknown>;
    list(): Promise<unknown>;
  };
  imports: {
    inspect(request: unknown): Promise<unknown>;
    create(request: unknown): Promise<unknown>;
  };
  media: {
    registerLocalSelection(request: unknown): Promise<unknown>;
    captureSnapshot(request: unknown): Promise<unknown>;
    compareStability(request: unknown): Promise<unknown>;
    probe(request: unknown): Promise<unknown>;
    startHash(request: unknown): Promise<unknown>;
    getHash(request: unknown): Promise<unknown>;
    cancelHash(request: unknown): Promise<unknown>;
  };
  analysis: {
    start(request: unknown): Promise<unknown>;
    list(request: unknown): Promise<unknown>;
    get(request: unknown): Promise<unknown>;
    cancel(request: unknown): Promise<unknown>;
    pause(request: unknown): Promise<unknown>;
  };
  denseIndex: {
    start(
      request: TrafficSuperVisionDenseIndexStartRequest,
    ): Promise<unknown>;
    list(request: TrafficSuperVisionProjectRequest): Promise<unknown>;
    get(request: TrafficSuperVisionDenseIndexJobRequest): Promise<unknown>;
    cancel(
      request: TrafficSuperVisionDenseIndexJobRequest,
    ): Promise<unknown>;
  };
  pipeline: {
    configureProfile(
      request: TrafficSuperVisionPipelineProfileRequest,
    ): Promise<unknown>;
    getProfile(request: TrafficSuperVisionProjectRequest): Promise<unknown>;
    start(request: TrafficSuperVisionPipelineStartRequest): Promise<unknown>;
    list(request: TrafficSuperVisionProjectRequest): Promise<unknown>;
    get(request: TrafficSuperVisionPipelineJobRequest): Promise<unknown>;
    cancel(request: TrafficSuperVisionPipelineJobRequest): Promise<unknown>;
    resume(request: TrafficSuperVisionPipelineJobRequest): Promise<unknown>;
    completeReview(
      request: TrafficSuperVisionPipelineReviewCompleteRequest,
    ): Promise<unknown>;
  };
  candidateQuestions: {
    list(request: unknown): Promise<unknown>;
    answer(request: unknown): Promise<unknown>;
  };
  maskRefinements: {
    capability(
      request: TrafficSuperVisionMaskRefinementIdentityRequest,
    ): Promise<TrafficSuperVisionMaskRefinementCapabilityResponse>;
    refine(
      request: TrafficSuperVisionMaskRefinementRequest,
    ): Promise<TrafficSuperVisionMaskRefinementResponse>;
  };
  incidents: {
    list(request: TrafficSuperVisionIncidentListRequest): Promise<unknown>;
  };
  labels: {
    stats(request: TrafficSuperVisionProjectRequest): Promise<unknown>;
  };
  datasets: {
    list(request: TrafficSuperVisionProjectRequest): Promise<unknown>;
    create(request: TrafficSuperVisionDatasetCreateRequest): Promise<unknown>;
  };
  sources: {
    candidates(): Promise<unknown>;
  };
  referenceCases: {
    sources(request: TrafficSuperVisionProjectRequest): Promise<unknown>;
    search(
      request: TrafficSuperVisionWeakCaseSearchRequest,
    ): Promise<unknown>;
  };
  autoLabeling: {
    compile(
      request: TrafficSuperVisionAutoLabelingCompileRequest,
    ): Promise<unknown>;
    run(request: TrafficSuperVisionAutoLabelingRunRequest): Promise<unknown>;
  };
  reviewComputations: {
    temporalIncidents(request: unknown): Promise<unknown>;
    modelValidation(request: unknown): Promise<unknown>;
    modelPromotion(request: unknown): Promise<unknown>;
  };
  questions: {
    list(request: unknown): Promise<unknown>;
    answer(request: unknown): Promise<unknown>;
  };
  history: {
    get(request: unknown): Promise<unknown>;
  };
  runtime: {
    readiness(): Promise<unknown>;
  };
  gps: {
    importTrack(request: unknown): Promise<unknown>;
    getTrack(request: unknown): Promise<unknown>;
    updateOffset(request: unknown): Promise<unknown>;
  };
  providers: {
    list(): Promise<unknown>;
    login(request: unknown): Promise<unknown>;
  };
  reports: {
    generate(request: TrafficSuperVisionReportGenerateRequest): Promise<unknown>;
    draft(request: unknown): Promise<unknown>;
    plan(request: unknown): Promise<unknown>;
    apply(request: unknown): Promise<unknown>;
    accept(request: unknown): Promise<unknown>;
    reject(request: unknown): Promise<unknown>;
  };
  hwp: {
    preflight(): Promise<unknown>;
    preview(request: unknown): Promise<unknown>;
    apply(request: unknown): Promise<unknown>;
    rollback(request: unknown): Promise<unknown>;
  };
  exports: {
    create(request: unknown): Promise<unknown>;
  };
}

interface Window {
  trafficSuperVision?: TrafficSuperVisionDesktopApi;
}
