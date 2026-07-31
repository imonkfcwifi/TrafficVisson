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

interface TrafficSuperVisionProjectOutputDirectorySelection {
  readonly selectionId: string | null;
  readonly directory: null | {
    readonly name: string;
    readonly volumeLabel: string;
    readonly availableFreeBytes: number;
    readonly pathExposed: false;
  };
}

interface TrafficSuperVisionProjectOutputLocationRequest {
  readonly projectId: string;
  readonly selectionId: string;
}

interface TrafficSuperVisionQuestionReviewNotificationRequest {
  projectId: string;
  pipelineJobId: string;
  queueRevision: string;
  questionCount: number;
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

interface TrafficSuperVisionWeakCaseStandardizationRequest
  extends TrafficSuperVisionProjectRequest {
  readonly sourceId: string;
  readonly caseId: string;
  readonly provider: "codex" | "claude";
  readonly expectedDocumentSha256: string;
  readonly expectedIndexManifestSha256: string;
  readonly expectedImageSha256: string;
  readonly expectedRevision?: number;
  readonly metadataTransmissionConfirmed: true;
}

interface TrafficSuperVisionWeakCaseApprovalRequest
  extends TrafficSuperVisionProjectRequest {
  readonly sourceId: string;
  readonly caseId: string;
  readonly expectedDocumentSha256: string;
  readonly expectedIndexManifestSha256: string;
  readonly expectedImageSha256: string;
  readonly issueType:
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
  readonly maintenanceAction: string;
  readonly caption?: string;
  readonly route?: string;
  readonly location?: string;
  readonly evaluation?: string;
  readonly opinion?: string;
  readonly expectedRevision?: number;
  readonly duplicateImageReviewConfirmed?: boolean;
  readonly userConfirmed: true;
}

interface TrafficSuperVisionIncidentListRequest
  extends TrafficSuperVisionProjectRequest {
  readonly analysisJobId?: string;
  readonly includeInactive?: boolean;
}

type TrafficSuperVisionProjectModelIssueType =
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

interface TrafficSuperVisionFixedValidationExtensionApproval {
  readonly schemaVersion:
    "trafficvisson.fixed-validation-extension-approval.v1";
  readonly expectedParentVersion: number;
  readonly expectedParentMembershipSha256: string;
  readonly expectedParentFixedValidationSha256: string;
  readonly addedIssueTypes: readonly TrafficSuperVisionProjectModelIssueType[];
  readonly addedValidationAnalysisJobIds: readonly string[];
  readonly userConfirmed: true;
}

interface TrafficSuperVisionDatasetCreateRequest {
  readonly projectId: string;
  readonly userConfirmed: true;
  readonly fixedValidationExtensionApproval?:
    TrafficSuperVisionFixedValidationExtensionApproval;
}

interface TrafficSuperVisionProjectModelTrainingStartRequest
  extends TrafficSuperVisionProjectRequest {
  readonly datasetVersionId: string;
  readonly idempotencyKey: string;
  readonly userConfirmed: true;
  readonly fixedValidationExtensionApproval?:
    TrafficSuperVisionFixedValidationExtensionApproval;
}

interface TrafficSuperVisionProjectModelTrainingResumeRequest
  extends TrafficSuperVisionProjectRequest {
  readonly trainingJobId: string;
  readonly userConfirmed: true;
}

interface TrafficSuperVisionProjectModelTrainingCancelRequest
  extends TrafficSuperVisionProjectRequest {
  readonly trainingJobId: string;
  readonly userConfirmed: true;
}

interface TrafficSuperVisionProjectModelTransitionRequest
  extends TrafficSuperVisionProjectRequest {
  readonly modelVersionId: string;
  readonly expectedRevision: number;
  readonly idempotencyKey: string;
  readonly userConfirmed: true;
}

interface TrafficSuperVisionProjectModelRollbackRequest
  extends TrafficSuperVisionProjectRequest {
  readonly expectedRevision: number;
  readonly idempotencyKey: string;
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
  readonly labelConfirmationAuthority:
    | "human_review"
    | "structured_llm_training_assist";
  readonly defaultTemplateId: string;
  readonly defaultTemplateSha256: string;
  readonly detectorPackId: string;
  readonly ocrPackId: string;
  readonly sam2PackId: string;
  readonly reportProvider: "codex" | "claude" | "local" | "none";
  readonly reportTransmissionConsent: boolean;
  readonly templateSemanticMapId: string;
  readonly reportRouteName?: string;
  readonly reportInspectorName?: string;
  readonly reportEvidenceMode?:
    | "internal_original"
    | "shared_deidentified";
  readonly reportInspectionDatePolicy?:
    "source_filename_yyyymmdd_or_block";
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

interface TrafficSuperVisionFolderBatchStartRequest {
  readonly projectId: string;
  readonly snapshotId: string;
  readonly rightsConfirmed: true;
  readonly idempotencyKey: string;
}

interface TrafficSuperVisionFolderBatchRequest {
  readonly projectId: string;
  readonly batchId: string;
}

interface TrafficSuperVisionFolderBatchRetryRequest
  extends TrafficSuperVisionFolderBatchRequest {
  readonly userConfirmed: true;
}

interface TrafficSuperVisionPipelineReviewCompleteRequest
  extends TrafficSuperVisionPipelineJobRequest {
  readonly reviewCompletionId: string;
  readonly finalQaChoice?:
    | "no_miss_complete"
    | "miss_candidates_reviewed";
}

interface TrafficSuperVisionUserDiscoveredCandidateRequest {
  readonly projectId: string;
  readonly pipelineJobId: string;
  readonly analysisJobId: string;
  readonly timestampSeconds: number;
  readonly selection:
    | {
        readonly kind: "point";
        readonly x: number;
        readonly y: number;
      }
    | {
        readonly kind: "box";
        readonly x: number;
        readonly y: number;
        readonly width: number;
        readonly height: number;
      };
  readonly issueTypeProposal?:
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
  readonly requestSam2Mask: boolean;
  readonly expectedQuestionQueueRevision: string;
}

interface TrafficSuperVisionUserDiscoveredReviewFrameRequest {
  readonly projectId: string;
  readonly pipelineJobId: string;
  readonly analysisJobId: string;
  readonly timestampSeconds: number;
}

interface TrafficSuperVisionProviderVisualEvidenceIdentityRequest
  extends TrafficSuperVisionPipelineJobRequest {
  readonly candidateId: string;
  readonly preparationId: string;
}

interface TrafficSuperVisionProviderVisualEvidenceArtifactRequest
  extends TrafficSuperVisionProviderVisualEvidenceIdentityRequest {
  readonly order: number;
}

interface TrafficSuperVisionProviderVisualEvidencePrivacyReviewRequest
  extends TrafficSuperVisionProviderVisualEvidenceIdentityRequest {
  readonly expectedManifestSha256: string;
  readonly approved: boolean;
  readonly explicitUserConfirmation: true;
}

interface TrafficSuperVisionReportDeidentifiedEvidenceArtifactRequest
  extends TrafficSuperVisionPipelineJobRequest {
  readonly incidentId: string;
  readonly derivativeId: string;
}

interface TrafficSuperVisionReportDeidentifiedEvidenceReviewRequest
  extends TrafficSuperVisionReportDeidentifiedEvidenceArtifactRequest {
  readonly expectedManifestSha256: string;
  readonly approved: boolean;
  readonly explicitUserConfirmation: true;
}

interface TrafficSuperVisionStructuredLlmAssistRequest {
  readonly projectId: string;
  readonly jobId: string;
  readonly candidateId: string;
  readonly questionId: string;
  readonly userAuthorizedTraining: true;
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

type TrafficSuperVisionDocumentEditOperationType =
  | "fill_text"
  | "fill_cell"
  | "insert_image";

interface TrafficSuperVisionDocumentEditOperation {
  readonly operationId: string;
  readonly type: TrafficSuperVisionDocumentEditOperationType;
  readonly target: string;
  readonly expectedValue?: string;
  readonly value: string;
  readonly sourceRef: string;
  readonly stylePolicy: "preserve" | "inherit_target";
  readonly preview: string;
}

interface TrafficSuperVisionDocumentEditPlan {
  readonly schemaVersion: "1.0";
  readonly planId: string;
  readonly documentSnapshotId: string;
  readonly operations: readonly TrafficSuperVisionDocumentEditOperation[];
}

interface TrafficSuperVisionHwpPreviewRequest {
  readonly projectId: string;
  readonly documentSelectionId: string;
  readonly plan: TrafficSuperVisionDocumentEditPlan;
}

interface TrafficSuperVisionHwpApplyRequest
  extends TrafficSuperVisionHwpPreviewRequest {
  readonly approvalToken: string;
}

interface TrafficSuperVisionExactReportPreviewRequest {
  readonly projectId: string;
  readonly pipelineJobId: string;
  readonly previewId: string;
}

interface TrafficSuperVisionExactReportApplyRequest {
  readonly projectId: string;
  readonly pipelineJobId: string;
  readonly draftId: string;
  readonly approvalToken: string;
  readonly documentEditPlanSha256: string;
  readonly acceptRejectRollbackBindingSha256: string;
  readonly userConfirmed: true;
}

interface TrafficSuperVisionExactReportDecisionRequest {
  readonly projectId: string;
  readonly pipelineJobId: string;
  readonly applicationId: string;
  readonly documentEditPlanSha256: string;
  readonly acceptRejectRollbackBindingSha256: string;
  readonly userConfirmed: true;
}

interface TrafficSuperVisionLegacyHwpPreviewResponse {
  readonly mode: "preview";
  readonly operation_count: number;
  readonly operation_types: Readonly<Record<string, number>>;
  readonly structure: {
    readonly observed_characters: number;
    readonly truncated: boolean;
    readonly paragraph_count: number;
    readonly table_count: number;
    readonly picture_count: number;
    readonly field_names: readonly string[];
    readonly bookmark_names: readonly string[];
  };
  readonly message: string;
  readonly approval_token: string;
  readonly approval_expires_at: string;
  readonly document_sha256: string;
  readonly plan_sha256: string;
  readonly target_summary: {
    readonly operation_count: number;
    readonly operation_types: Readonly<Record<string, number>>;
    readonly targets: readonly string[];
  };
}

interface TrafficSuperVisionSampleTargetStructureHwpPreviewResponse {
  readonly schema_version: "trafficvisson.hwpx-sample-target-plan.v1";
  readonly mode: "sample_target_structure_preview";
  readonly state: "target_structure_preview_validated_apply_unavailable";
  readonly document_snapshot_id: string;
  readonly document_sha256: string;
  readonly structure_target_summary_sha256: string;
  readonly weak_case_index_manifest_sha256: string;
  readonly plan_sha256: string;
  readonly validation_sha256: string;
  readonly approved_references_sha256: string;
  readonly approved_reference_count: number;
  readonly operation_count: number;
  readonly page_pair_indices: readonly number[];
  readonly touched_pages: readonly number[];
  readonly expected_rendered_page_count: 48;
  readonly expected_page_range: readonly [1, 48];
  readonly page_range_preserved_by_structure: true;
  readonly table_growth_allowed: false;
  readonly target_structure_validation_passed: true;
  readonly approved_incident_bound: false;
  readonly content_provenance_bound: false;
  readonly com_invocation_performed: false;
  readonly operation_bindings: readonly {
    readonly operation_id: string;
    readonly approval_id: string;
    readonly operation_type: "fill_cell" | "insert_image";
    readonly target_id: string;
    readonly semantic_role:
      | "photo_slot_image"
      | "photo_caption"
      | "inspection_route"
      | "inspection_opinion_body"
      | "evaluation_square_marker";
    readonly page_number: number;
    readonly table_index: number;
    readonly row: number;
    readonly column: number;
  }[];
  readonly approval_scope: "sample_target_structure_hash_binding_only";
  readonly approval_token: string;
  readonly approval_expires_at: string;
  readonly apply_eligible: false;
  readonly writer_state: "unavailable";
  readonly output_render_validation_status: "not_run";
  readonly blocking_reasons: readonly [
    "exact_hwpml_target_writer_not_implemented",
    "output_reopen_and_48_page_render_validation_not_implemented",
  ];
  readonly no_com: true;
  readonly no_document_write: true;
}

type TrafficSuperVisionHwpPreviewResponse =
  | TrafficSuperVisionLegacyHwpPreviewResponse
  | TrafficSuperVisionSampleTargetStructureHwpPreviewResponse;

interface TrafficSuperVisionDesktopApi {
  notifications: {
    questionReviewReady(
      request: TrafficSuperVisionQuestionReviewNotificationRequest,
    ): Promise<unknown>;
  };
  health(): Promise<unknown>;
  dialogs: {
    selectVideos(): Promise<TrafficSuperVisionVideoSelection>;
    selectGpxFile(): Promise<TrafficSuperVisionGpxSelection>;
    selectHwpDocument(): Promise<TrafficSuperVisionDocumentSelection>;
    selectExportDirectory(): Promise<TrafficSuperVisionDirectorySelection>;
    selectProjectOutputDirectory(): Promise<
      TrafficSuperVisionProjectOutputDirectorySelection
    >;
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
    startFolderBatch(
      request: TrafficSuperVisionFolderBatchStartRequest,
    ): Promise<unknown>;
    listFolderBatches(
      request: TrafficSuperVisionProjectRequest,
    ): Promise<unknown>;
    getFolderBatch(
      request: TrafficSuperVisionFolderBatchRequest,
    ): Promise<unknown>;
    retryFolderBatch(
      request: TrafficSuperVisionFolderBatchRetryRequest,
    ): Promise<unknown>;
  };
  userDiscoveredCandidates: {
    reviewFrame(
      request: TrafficSuperVisionUserDiscoveredReviewFrameRequest,
    ): Promise<unknown>;
    create(
      request: TrafficSuperVisionUserDiscoveredCandidateRequest,
    ): Promise<unknown>;
  };
  projectOutputLocations: {
    register(
      request: TrafficSuperVisionProjectOutputLocationRequest,
    ): Promise<unknown>;
    list(
      request: TrafficSuperVisionProjectRequest,
    ): Promise<unknown>;
  };
  providerVisualEvidence: {
    list(
      request: TrafficSuperVisionPipelineJobRequest,
    ): Promise<unknown>;
    get(
      request: TrafficSuperVisionProviderVisualEvidenceIdentityRequest,
    ): Promise<unknown>;
    artifact(
      request: TrafficSuperVisionProviderVisualEvidenceArtifactRequest,
    ): Promise<unknown>;
    review(
      request: TrafficSuperVisionProviderVisualEvidencePrivacyReviewRequest,
    ): Promise<unknown>;
  };
  reportDeidentifiedEvidence: {
    prepare(
      request: TrafficSuperVisionPipelineJobRequest,
    ): Promise<unknown>;
    list(
      request: TrafficSuperVisionPipelineJobRequest,
    ): Promise<unknown>;
    artifact(
      request: TrafficSuperVisionReportDeidentifiedEvidenceArtifactRequest,
    ): Promise<unknown>;
    review(
      request: TrafficSuperVisionReportDeidentifiedEvidenceReviewRequest,
    ): Promise<unknown>;
  };
  candidateQuestions: {
    list(request: unknown): Promise<unknown>;
    answer(request: unknown): Promise<unknown>;
    useStructuredLlmForTraining(
      request: TrafficSuperVisionStructuredLlmAssistRequest,
    ): Promise<unknown>;
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
  projectModels: {
    startTraining(
      request: TrafficSuperVisionProjectModelTrainingStartRequest,
    ): Promise<unknown>;
    listTrainingJobs(
      request: TrafficSuperVisionProjectRequest,
    ): Promise<unknown>;
    resumeTraining(
      request: TrafficSuperVisionProjectModelTrainingResumeRequest,
    ): Promise<unknown>;
    cancelTraining(
      request: TrafficSuperVisionProjectModelTrainingCancelRequest,
    ): Promise<unknown>;
    listFixedValidationSets(
      request: TrafficSuperVisionProjectRequest,
    ): Promise<unknown>;
    listVersions(
      request: TrafficSuperVisionProjectRequest,
    ): Promise<unknown>;
    listIterations(
      request: TrafficSuperVisionProjectRequest,
    ): Promise<unknown>;
    getState(request: TrafficSuperVisionProjectRequest): Promise<unknown>;
    promote(
      request: TrafficSuperVisionProjectModelTransitionRequest,
    ): Promise<unknown>;
    reject(
      request: TrafficSuperVisionProjectModelTransitionRequest,
    ): Promise<unknown>;
    rollback(
      request: TrafficSuperVisionProjectModelRollbackRequest,
    ): Promise<unknown>;
  };
  sources: {
    candidates(): Promise<unknown>;
  };
  referenceCases: {
    sources(request: TrafficSuperVisionProjectRequest): Promise<unknown>;
    search(
      request: TrafficSuperVisionWeakCaseSearchRequest,
    ): Promise<unknown>;
    proposeStandardization(
      request: TrafficSuperVisionWeakCaseStandardizationRequest,
    ): Promise<unknown>;
    standardizationProposals(
      request: TrafficSuperVisionProjectRequest,
    ): Promise<unknown>;
    approve(
      request: TrafficSuperVisionWeakCaseApprovalRequest,
    ): Promise<unknown>;
    approved(
      request: TrafficSuperVisionProjectRequest,
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
    prepareExactPreview(
      request: TrafficSuperVisionExactReportPreviewRequest,
    ): Promise<unknown>;
    applyExact(
      request: TrafficSuperVisionExactReportApplyRequest,
    ): Promise<unknown>;
    acceptExact(
      request: TrafficSuperVisionExactReportDecisionRequest,
    ): Promise<unknown>;
    rejectExact(
      request: TrafficSuperVisionExactReportDecisionRequest,
    ): Promise<unknown>;
    rollbackExact(
      request: TrafficSuperVisionExactReportDecisionRequest,
    ): Promise<unknown>;
  };
  hwp: {
    preflight(): Promise<unknown>;
    preview(
      request: TrafficSuperVisionHwpPreviewRequest,
    ): Promise<TrafficSuperVisionHwpPreviewResponse>;
    apply(request: TrafficSuperVisionHwpApplyRequest): Promise<unknown>;
    rollback(request: unknown): Promise<unknown>;
  };
  exports: {
    create(request: unknown): Promise<unknown>;
  };
}

interface Window {
  trafficSuperVision?: TrafficSuperVisionDesktopApi;
}
