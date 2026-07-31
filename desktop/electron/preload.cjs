"use strict";

const { contextBridge, ipcRenderer } = require("electron");

const CHANNELS = Object.freeze({
  DIALOG_VIDEO_FILES: "trafficvisson:dialogs:video-files",
  DIALOG_GPX_FILE: "trafficvisson:dialogs:gpx-file",
  DIALOG_HWP_DOCUMENT: "trafficvisson:dialogs:hwp-document",
  DIALOG_EXPORT_DIRECTORY: "trafficvisson:dialogs:export-directory",
  DIALOG_PROJECT_OUTPUT_DIRECTORY:
    "trafficvisson:dialogs:project-output-directory",
  NOTIFICATION_QUESTION_REVIEW_READY:
    "trafficvisson:notifications:question-review-ready",
  HEALTH: "trafficvisson:health",
  PROJECT_CREATE: "trafficvisson:projects:create",
  PROJECT_GET: "trafficvisson:projects:get",
  PROJECT_LIST: "trafficvisson:projects:list",
  IMPORT_INSPECT: "trafficvisson:imports:inspect",
  IMPORT_CREATE: "trafficvisson:imports:create",
  MEDIA_LOCAL_REGISTER: "trafficvisson:media:local-register",
  MEDIA_SNAPSHOT: "trafficvisson:media:snapshot",
  MEDIA_STABILITY: "trafficvisson:media:stability",
  MEDIA_PROBE: "trafficvisson:media:probe",
  MEDIA_HASH_START: "trafficvisson:media:hash-start",
  MEDIA_HASH_GET: "trafficvisson:media:hash-get",
  MEDIA_HASH_CANCEL: "trafficvisson:media:hash-cancel",
  ANALYSIS_START: "trafficvisson:analysis:start",
  ANALYSIS_LIST: "trafficvisson:analysis:list",
  ANALYSIS_GET: "trafficvisson:analysis:get",
  ANALYSIS_CANCEL: "trafficvisson:analysis:cancel",
  ANALYSIS_PAUSE: "trafficvisson:analysis:pause",
  DENSE_INDEX_START: "trafficvisson:dense-index:start",
  DENSE_INDEX_LIST: "trafficvisson:dense-index:list",
  DENSE_INDEX_GET: "trafficvisson:dense-index:get",
  DENSE_INDEX_CANCEL: "trafficvisson:dense-index:cancel",
  PIPELINE_PROFILE_CONFIGURE: "trafficvisson:pipeline:profile-configure",
  PIPELINE_PROFILE_GET: "trafficvisson:pipeline:profile-get",
  PROJECT_OUTPUT_LOCATION_REGISTER:
    "trafficvisson:project-output-locations:register",
  PROJECT_OUTPUT_LOCATION_LIST:
    "trafficvisson:project-output-locations:list",
  PIPELINE_START: "trafficvisson:pipeline:start",
  PIPELINE_RERUN_CURRENT_PROFILE:
    "trafficvisson:pipeline:rerun-current-profile",
  PIPELINE_LIST: "trafficvisson:pipeline:list",
  PIPELINE_GET: "trafficvisson:pipeline:get",
  PIPELINE_CANCEL: "trafficvisson:pipeline:cancel",
  PIPELINE_RESUME: "trafficvisson:pipeline:resume",
  PIPELINE_REVIEW_COMPLETE: "trafficvisson:pipeline:review-complete",
  USER_DISCOVERED_CANDIDATE_CREATE:
    "trafficvisson:pipeline:user-discovered-candidate-create",
  USER_DISCOVERED_REVIEW_FRAME_GET:
    "trafficvisson:pipeline:user-discovered-review-frame-get",
  PIPELINE_FOLDER_BATCH_START:
    "trafficvisson:pipeline:folder-batch-start",
  PIPELINE_FOLDER_BATCH_LIST:
    "trafficvisson:pipeline:folder-batch-list",
  PIPELINE_FOLDER_BATCH_GET:
    "trafficvisson:pipeline:folder-batch-get",
  PIPELINE_FOLDER_BATCH_RETRY:
    "trafficvisson:pipeline:folder-batch-retry",
  PROVIDER_VISUAL_EVIDENCE_LIST:
    "trafficvisson:pipeline:provider-visual-evidence-list",
  PROVIDER_VISUAL_EVIDENCE_GET:
    "trafficvisson:pipeline:provider-visual-evidence-get",
  PROVIDER_VISUAL_EVIDENCE_ARTIFACT:
    "trafficvisson:pipeline:provider-visual-evidence-artifact",
  PROVIDER_VISUAL_EVIDENCE_PRIVACY_REVIEW:
    "trafficvisson:pipeline:provider-visual-evidence-privacy-review",
  REPORT_DEIDENTIFIED_EVIDENCE_PREPARE:
    "trafficvisson:pipeline:report-deidentified-evidence-prepare",
  REPORT_DEIDENTIFIED_EVIDENCE_LIST:
    "trafficvisson:pipeline:report-deidentified-evidence-list",
  REPORT_DEIDENTIFIED_EVIDENCE_ARTIFACT:
    "trafficvisson:pipeline:report-deidentified-evidence-artifact",
  REPORT_DEIDENTIFIED_EVIDENCE_REVIEW:
    "trafficvisson:pipeline:report-deidentified-evidence-review",
  CANDIDATE_QUESTIONS_LIST: "trafficvisson:candidate-questions:list",
  CANDIDATE_ANSWER: "trafficvisson:candidate-questions:answer",
  CANDIDATE_STRUCTURED_LLM_ASSIST:
    "trafficvisson:candidate-questions:structured-llm-assist",
  MASK_REFINEMENT_CAPABILITY:
    "trafficvisson:mask-refinement:capability",
  MASK_REFINEMENT_REQUEST:
    "trafficvisson:mask-refinement:request",
  INCIDENT_LIST: "trafficvisson:incidents:list",
  AUTO_LABEL_COMPILE: "trafficvisson:auto-labeling:compile",
  AUTO_LABEL_RUN: "trafficvisson:auto-labeling:run",
  REVIEW_TEMPORAL_INCIDENTS:
    "trafficvisson:review:temporal-incident-proposals",
  REVIEW_MODEL_VALIDATION: "trafficvisson:review:model-validation",
  REVIEW_MODEL_PROMOTION: "trafficvisson:review:model-promotion",
  LABEL_STATS: "trafficvisson:labels:stats",
  DATASET_LIST: "trafficvisson:datasets:list",
  DATASET_CREATE: "trafficvisson:datasets:create",
  MODEL_TRAINING_START: "trafficvisson:project-models:training-start",
  MODEL_TRAINING_LIST: "trafficvisson:project-models:training-list",
  MODEL_TRAINING_RESUME: "trafficvisson:project-models:training-resume",
  MODEL_TRAINING_CANCEL: "trafficvisson:project-models:training-cancel",
  MODEL_FIXED_VALIDATION_LIST:
    "trafficvisson:project-models:fixed-validation-list",
  MODEL_VERSIONS_LIST: "trafficvisson:project-models:versions-list",
  MODEL_ITERATIONS_LIST:
    "trafficvisson:project-models:iterations-list",
  MODEL_STATE_GET: "trafficvisson:project-models:state-get",
  MODEL_PROMOTE: "trafficvisson:project-models:promote",
  MODEL_REJECT: "trafficvisson:project-models:reject",
  MODEL_ROLLBACK: "trafficvisson:project-models:rollback",
  SOURCE_CANDIDATES_LIST: "trafficvisson:source-candidates:list",
  WEAK_CASE_SOURCES_LIST:
    "trafficvisson:weak-cases:sources-list",
  WEAK_CASE_SEARCH: "trafficvisson:weak-cases:search",
  WEAK_CASE_STANDARDIZATION_PROPOSE:
    "trafficvisson:weak-cases:standardization-propose",
  WEAK_CASE_STANDARDIZATION_LIST:
    "trafficvisson:weak-cases:standardization-list",
  WEAK_CASE_APPROVE: "trafficvisson:weak-cases:approve",
  WEAK_CASE_APPROVED_LIST:
    "trafficvisson:weak-cases:approved-list",
  QUESTIONS_LIST: "trafficvisson:questions:list",
  QUESTION_ANSWER: "trafficvisson:questions:answer",
  HISTORY_GET: "trafficvisson:history:get",
  RUNTIME_READINESS: "trafficvisson:runtime:readiness",
  GPX_TRACK_IMPORT: "trafficvisson:gpx-track:import",
  GPX_TRACK_GET: "trafficvisson:gpx-track:get",
  GPX_TRACK_OFFSET: "trafficvisson:gpx-track:offset",
  PROVIDERS_LIST: "trafficvisson:providers:list",
  PROVIDER_LOGIN: "trafficvisson:providers:login",
  REPORT_GENERATE: "trafficvisson:reports:generate",
  REPORT_DRAFT: "trafficvisson:reports:draft",
  REPORT_PLAN: "trafficvisson:reports:plan",
  REPORT_APPLY: "trafficvisson:reports:apply",
  REPORT_ACCEPT: "trafficvisson:reports:accept",
  REPORT_REJECT: "trafficvisson:reports:reject",
  REPORT_EXACT_PREVIEW: "trafficvisson:reports:exact-preview",
  REPORT_EXACT_APPLY: "trafficvisson:reports:exact-apply",
  REPORT_EXACT_ACCEPT: "trafficvisson:reports:exact-accept",
  REPORT_EXACT_REJECT: "trafficvisson:reports:exact-reject",
  REPORT_EXACT_ROLLBACK: "trafficvisson:reports:exact-rollback",
  HWP_PREFLIGHT: "trafficvisson:hwp:preflight",
  HWP_PREVIEW: "trafficvisson:hwp:preview",
  HWP_APPLY: "trafficvisson:hwp:apply",
  HWP_ROLLBACK: "trafficvisson:hwp:rollback",
  EXPORT_CREATE: "trafficvisson:exports:create",
});

const invoke = (channel, payload) => ipcRenderer.invoke(channel, payload);
const selectVideos = () => invoke(CHANNELS.DIALOG_VIDEO_FILES);

const api = Object.freeze({
  health: () => invoke(CHANNELS.HEALTH),
  notifications: Object.freeze({
    questionReviewReady: (request) =>
      invoke(
        CHANNELS.NOTIFICATION_QUESTION_REVIEW_READY,
        request,
      ),
  }),
  dialogs: Object.freeze({
    selectVideos,
    selectGpxFile: () => invoke(CHANNELS.DIALOG_GPX_FILE),
    selectHwpDocument: () => invoke(CHANNELS.DIALOG_HWP_DOCUMENT),
    selectExportDirectory: () =>
      invoke(CHANNELS.DIALOG_EXPORT_DIRECTORY),
    selectProjectOutputDirectory: () =>
      invoke(CHANNELS.DIALOG_PROJECT_OUTPUT_DIRECTORY),
  }),
  // Temporary UI compatibility: names are display metadata, never filesystem paths.
  chooseVideoFiles: async () => {
    const selection = await selectVideos();
    return selection.files.map((file) => file.name);
  },
  projects: Object.freeze({
    create: (request) => invoke(CHANNELS.PROJECT_CREATE, request),
    get: (request) => invoke(CHANNELS.PROJECT_GET, request),
    list: () => invoke(CHANNELS.PROJECT_LIST),
  }),
  imports: Object.freeze({
    inspect: (request) => invoke(CHANNELS.IMPORT_INSPECT, request),
    create: (request) => invoke(CHANNELS.IMPORT_CREATE, request),
  }),
  media: Object.freeze({
    registerLocalSelection: (request) =>
      invoke(CHANNELS.MEDIA_LOCAL_REGISTER, request),
    captureSnapshot: (request) => invoke(CHANNELS.MEDIA_SNAPSHOT, request),
    compareStability: (request) => invoke(CHANNELS.MEDIA_STABILITY, request),
    probe: (request) => invoke(CHANNELS.MEDIA_PROBE, request),
    startHash: (request) => invoke(CHANNELS.MEDIA_HASH_START, request),
    getHash: (request) => invoke(CHANNELS.MEDIA_HASH_GET, request),
    cancelHash: (request) => invoke(CHANNELS.MEDIA_HASH_CANCEL, request),
  }),
  analysis: Object.freeze({
    start: (request) => invoke(CHANNELS.ANALYSIS_START, request),
    list: (request) => invoke(CHANNELS.ANALYSIS_LIST, request),
    get: (request) => invoke(CHANNELS.ANALYSIS_GET, request),
    cancel: (request) => invoke(CHANNELS.ANALYSIS_CANCEL, request),
    pause: (request) => invoke(CHANNELS.ANALYSIS_PAUSE, request),
  }),
  denseIndex: Object.freeze({
    start: (request) => invoke(CHANNELS.DENSE_INDEX_START, request),
    list: (request) => invoke(CHANNELS.DENSE_INDEX_LIST, request),
    get: (request) => invoke(CHANNELS.DENSE_INDEX_GET, request),
    cancel: (request) => invoke(CHANNELS.DENSE_INDEX_CANCEL, request),
  }),
  pipeline: Object.freeze({
    configureProfile: (request) =>
      invoke(CHANNELS.PIPELINE_PROFILE_CONFIGURE, request),
    getProfile: (request) =>
      invoke(CHANNELS.PIPELINE_PROFILE_GET, request),
    start: (request) => invoke(CHANNELS.PIPELINE_START, request),
    rerunWithCurrentProfile: (request) =>
      invoke(CHANNELS.PIPELINE_RERUN_CURRENT_PROFILE, request),
    list: (request) => invoke(CHANNELS.PIPELINE_LIST, request),
    get: (request) => invoke(CHANNELS.PIPELINE_GET, request),
    cancel: (request) => invoke(CHANNELS.PIPELINE_CANCEL, request),
    resume: (request) => invoke(CHANNELS.PIPELINE_RESUME, request),
    completeReview: (request) =>
      invoke(CHANNELS.PIPELINE_REVIEW_COMPLETE, request),
    startFolderBatch: (request) =>
      invoke(CHANNELS.PIPELINE_FOLDER_BATCH_START, request),
    listFolderBatches: (request) =>
      invoke(CHANNELS.PIPELINE_FOLDER_BATCH_LIST, request),
    getFolderBatch: (request) =>
      invoke(CHANNELS.PIPELINE_FOLDER_BATCH_GET, request),
    retryFolderBatch: (request) =>
      invoke(CHANNELS.PIPELINE_FOLDER_BATCH_RETRY, request),
  }),
  userDiscoveredCandidates: Object.freeze({
    reviewFrame: (request) =>
      invoke(
        CHANNELS.USER_DISCOVERED_REVIEW_FRAME_GET,
        request,
      ),
    create: (request) =>
      invoke(
        CHANNELS.USER_DISCOVERED_CANDIDATE_CREATE,
        request,
      ),
  }),
  projectOutputLocations: Object.freeze({
    register: (request) =>
      invoke(CHANNELS.PROJECT_OUTPUT_LOCATION_REGISTER, request),
    list: (request) =>
      invoke(CHANNELS.PROJECT_OUTPUT_LOCATION_LIST, request),
  }),
  providerVisualEvidence: Object.freeze({
    list: (request) =>
      invoke(CHANNELS.PROVIDER_VISUAL_EVIDENCE_LIST, request),
    get: (request) =>
      invoke(CHANNELS.PROVIDER_VISUAL_EVIDENCE_GET, request),
    artifact: (request) =>
      invoke(CHANNELS.PROVIDER_VISUAL_EVIDENCE_ARTIFACT, request),
    review: (request) =>
      invoke(
        CHANNELS.PROVIDER_VISUAL_EVIDENCE_PRIVACY_REVIEW,
        request,
      ),
  }),
  reportDeidentifiedEvidence: Object.freeze({
    prepare: (request) =>
      invoke(CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_PREPARE, request),
    list: (request) =>
      invoke(CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_LIST, request),
    artifact: (request) =>
      invoke(CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_ARTIFACT, request),
    review: (request) =>
      invoke(CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_REVIEW, request),
  }),
  candidateQuestions: Object.freeze({
    list: (request) => invoke(CHANNELS.CANDIDATE_QUESTIONS_LIST, request),
    answer: (request) => invoke(CHANNELS.CANDIDATE_ANSWER, request),
    useStructuredLlmForTraining: (request) =>
      invoke(CHANNELS.CANDIDATE_STRUCTURED_LLM_ASSIST, request),
  }),
  maskRefinements: Object.freeze({
    capability: (request) =>
      invoke(CHANNELS.MASK_REFINEMENT_CAPABILITY, request),
    refine: (request) =>
      invoke(CHANNELS.MASK_REFINEMENT_REQUEST, request),
  }),
  incidents: Object.freeze({
    list: (request) => invoke(CHANNELS.INCIDENT_LIST, request),
  }),
  autoLabeling: Object.freeze({
    compile: (request) => invoke(CHANNELS.AUTO_LABEL_COMPILE, request),
    run: (request) => invoke(CHANNELS.AUTO_LABEL_RUN, request),
  }),
  reviewComputations: Object.freeze({
    temporalIncidents: (request) =>
      invoke(CHANNELS.REVIEW_TEMPORAL_INCIDENTS, request),
    modelValidation: (request) =>
      invoke(CHANNELS.REVIEW_MODEL_VALIDATION, request),
    modelPromotion: (request) =>
      invoke(CHANNELS.REVIEW_MODEL_PROMOTION, request),
  }),
  labels: Object.freeze({
    stats: (request) => invoke(CHANNELS.LABEL_STATS, request),
  }),
  datasets: Object.freeze({
    list: (request) => invoke(CHANNELS.DATASET_LIST, request),
    create: (request) => invoke(CHANNELS.DATASET_CREATE, request),
  }),
  projectModels: Object.freeze({
    startTraining: (request) =>
      invoke(CHANNELS.MODEL_TRAINING_START, request),
    listTrainingJobs: (request) =>
      invoke(CHANNELS.MODEL_TRAINING_LIST, request),
    resumeTraining: (request) =>
      invoke(CHANNELS.MODEL_TRAINING_RESUME, request),
    cancelTraining: (request) =>
      invoke(CHANNELS.MODEL_TRAINING_CANCEL, request),
    listFixedValidationSets: (request) =>
      invoke(CHANNELS.MODEL_FIXED_VALIDATION_LIST, request),
    listVersions: (request) =>
      invoke(CHANNELS.MODEL_VERSIONS_LIST, request),
    listIterations: (request) =>
      invoke(CHANNELS.MODEL_ITERATIONS_LIST, request),
    getState: (request) => invoke(CHANNELS.MODEL_STATE_GET, request),
    promote: (request) => invoke(CHANNELS.MODEL_PROMOTE, request),
    reject: (request) => invoke(CHANNELS.MODEL_REJECT, request),
    rollback: (request) => invoke(CHANNELS.MODEL_ROLLBACK, request),
  }),
  sources: Object.freeze({
    candidates: () => invoke(CHANNELS.SOURCE_CANDIDATES_LIST),
  }),
  referenceCases: Object.freeze({
    sources: (request) =>
      invoke(CHANNELS.WEAK_CASE_SOURCES_LIST, request),
    search: (request) =>
      invoke(CHANNELS.WEAK_CASE_SEARCH, request),
    proposeStandardization: (request) =>
      invoke(CHANNELS.WEAK_CASE_STANDARDIZATION_PROPOSE, request),
    standardizationProposals: (request) =>
      invoke(CHANNELS.WEAK_CASE_STANDARDIZATION_LIST, request),
    approve: (request) =>
      invoke(CHANNELS.WEAK_CASE_APPROVE, request),
    approved: (request) =>
      invoke(CHANNELS.WEAK_CASE_APPROVED_LIST, request),
  }),
  questions: Object.freeze({
    list: (request) => invoke(CHANNELS.QUESTIONS_LIST, request),
    answer: (request) => invoke(CHANNELS.QUESTION_ANSWER, request),
  }),
  history: Object.freeze({
    get: (request) => invoke(CHANNELS.HISTORY_GET, request),
  }),
  runtime: Object.freeze({
    readiness: () => invoke(CHANNELS.RUNTIME_READINESS),
  }),
  gps: Object.freeze({
    importTrack: (request) => invoke(CHANNELS.GPX_TRACK_IMPORT, request),
    getTrack: (request) => invoke(CHANNELS.GPX_TRACK_GET, request),
    updateOffset: (request) => invoke(CHANNELS.GPX_TRACK_OFFSET, request),
  }),
  providers: Object.freeze({
    list: () => invoke(CHANNELS.PROVIDERS_LIST),
    login: (request) => invoke(CHANNELS.PROVIDER_LOGIN, request),
  }),
  reports: Object.freeze({
    generate: (request) => invoke(CHANNELS.REPORT_GENERATE, request),
    draft: (request) => invoke(CHANNELS.REPORT_DRAFT, request),
    plan: (request) => invoke(CHANNELS.REPORT_PLAN, request),
    apply: (request) => invoke(CHANNELS.REPORT_APPLY, request),
    accept: (request) => invoke(CHANNELS.REPORT_ACCEPT, request),
    reject: (request) => invoke(CHANNELS.REPORT_REJECT, request),
    prepareExactPreview: (request) =>
      invoke(CHANNELS.REPORT_EXACT_PREVIEW, request),
    applyExact: (request) =>
      invoke(CHANNELS.REPORT_EXACT_APPLY, request),
    acceptExact: (request) =>
      invoke(CHANNELS.REPORT_EXACT_ACCEPT, request),
    rejectExact: (request) =>
      invoke(CHANNELS.REPORT_EXACT_REJECT, request),
    rollbackExact: (request) =>
      invoke(CHANNELS.REPORT_EXACT_ROLLBACK, request),
  }),
  hwp: Object.freeze({
    preflight: () => invoke(CHANNELS.HWP_PREFLIGHT),
    preview: (request) => invoke(CHANNELS.HWP_PREVIEW, request),
    apply: (request) => invoke(CHANNELS.HWP_APPLY, request),
    rollback: (request) => invoke(CHANNELS.HWP_ROLLBACK, request),
  }),
  exports: Object.freeze({
    create: (request) => invoke(CHANNELS.EXPORT_CREATE, request),
  }),
});

contextBridge.exposeInMainWorld("trafficSuperVision", api);
