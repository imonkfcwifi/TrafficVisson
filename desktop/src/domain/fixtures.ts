import type {
  DatasetVersion,
  Detection,
  DocumentEditPlan,
  EvidenceAsset,
  ExportArtifact,
  GpsGap,
  Incident,
  IncidentFinding,
  LabelAnswer,
  LabelQuestion,
  MediaAsset,
  ModelVersion,
  Project,
  ProviderConnection,
  ReportDraft,
  ReviewAction,
  RouteSegment,
  TrafficVissonManifest,
  Trip,
} from "./types";
import type { TrainingGateInput } from "./policies";

export const DEMO_PROJECT_ID = "project-seoul-ring-road-2026";
export const DEMO_TRIP_ID = "trip-2026-07-18-am";
export const DEMO_INCIDENT_ID = "incident-road-debris-001";
export const DEMO_FINDING_ID = "finding-road-debris-001";
export const DEMO_QUESTION_ID = "question-road-debris-001";

const CREATED_AT = "2026-07-18T00:10:00.000Z";
const UPDATED_AT = "2026-07-18T01:25:00.000Z";
const SHA_VIDEO = "a".repeat(64);
const SHA_GPX = "b".repeat(64);
const SHA_FRAME = "c".repeat(64);
const SHA_CLIP = "d".repeat(64);
const SHA_MAP = "e".repeat(64);
const SHA_DATASET = "f".repeat(64);
const SHA_ACTIVE_MODEL = "1".repeat(64);
const SHA_CANDIDATE_MODEL = "2".repeat(64);

export const demoProject: Project = {
  projectId: DEMO_PROJECT_ID,
  schemaVersion: 1,
  name: "서울외곽순환로 2026년 7월 정기점검",
  rootPath: "D:\\TrafficVisson\\projects\\seoul-ring-road-2026",
  status: "ACTIVE",
  questionPolicyMode: "ASSISTED_LEARNING",
  activeModelVersionId: "model-road-v3.4.0",
  createdAt: CREATED_AT,
  updatedAt: UPDATED_AT,
};

export const demoVideoAsset: MediaAsset = {
  projectId: DEMO_PROJECT_ID,
  assetId: "asset-video-trip-001",
  kind: "VIDEO",
  relativePath: "inputs\\trip-2026-07-18-am\\front-camera.mp4",
  mimeType: "video/mp4",
  byteSize: 4_281_774_592,
  sha256: SHA_VIDEO,
  source: {
    kind: "REMOTE_URL",
    provider: "Google Drive",
    sourceUrl: "https://drive.google.com/file/d/example/view",
    downloadedAt: "2026-07-18T00:08:00.000Z",
  },
  durationMs: 2_742_000,
  width: 3840,
  height: 2160,
  frameRate: 29.97,
  createdAt: CREATED_AT,
  updatedAt: CREATED_AT,
  revision: 1,
};

export const demoGpsAsset: MediaAsset = {
  projectId: DEMO_PROJECT_ID,
  assetId: "asset-gpx-trip-001",
  kind: "GPX",
  relativePath: "inputs\\trip-2026-07-18-am\\route.gpx",
  mimeType: "application/gpx+xml",
  byteSize: 1_248_920,
  sha256: SHA_GPX,
  source: {
    kind: "LOCAL_FILE",
    originalName: "20260718_0900_route.gpx",
  },
  createdAt: CREATED_AT,
  updatedAt: CREATED_AT,
  revision: 1,
};

export const demoFrameAsset: MediaAsset = {
  projectId: DEMO_PROJECT_ID,
  assetId: "asset-frame-18432",
  kind: "FRAME",
  relativePath:
    "evidence\\incident-road-debris-001\\frame-18432-original.jpg",
  mimeType: "image/jpeg",
  byteSize: 1_873_024,
  sha256: SHA_FRAME,
  source: {
    kind: "GENERATED",
    producer: "analysis-run-2026-07-18-001",
  },
  width: 3840,
  height: 2160,
  createdAt: "2026-07-18T00:40:00.000Z",
  updatedAt: "2026-07-18T00:40:00.000Z",
  revision: 1,
};

export const demoClipAsset: MediaAsset = {
  projectId: DEMO_PROJECT_ID,
  assetId: "asset-clip-road-debris-001",
  kind: "CLIP",
  relativePath:
    "evidence\\incident-road-debris-001\\context-clip-redacted.mp4",
  mimeType: "video/mp4",
  byteSize: 8_413_220,
  sha256: SHA_CLIP,
  source: {
    kind: "GENERATED",
    producer: "evidence-pipeline-2026-07-18-001",
  },
  durationMs: 4_000,
  width: 1920,
  height: 1080,
  frameRate: 29.97,
  createdAt: "2026-07-18T00:41:00.000Z",
  updatedAt: "2026-07-18T00:41:00.000Z",
  revision: 1,
};

export const demoMapAsset: MediaAsset = {
  projectId: DEMO_PROJECT_ID,
  assetId: "asset-map-road-debris-001",
  kind: "IMAGE",
  relativePath:
    "evidence\\incident-road-debris-001\\route-location-map.png",
  mimeType: "image/png",
  byteSize: 441_920,
  sha256: SHA_MAP,
  source: {
    kind: "GENERATED",
    producer: "map-renderer-2026-07-18-001",
  },
  width: 1600,
  height: 900,
  createdAt: "2026-07-18T00:41:30.000Z",
  updatedAt: "2026-07-18T00:41:30.000Z",
  revision: 1,
};

export const demoTrip: Trip = {
  projectId: DEMO_PROJECT_ID,
  tripId: DEMO_TRIP_ID,
  name: "판교JC → 구리IC 오전 점검",
  videoAssetId: demoVideoAsset.assetId,
  gpsAssetId: demoGpsAsset.assetId,
  startTime: "2026-07-18T00:12:00.000Z",
  endTime: "2026-07-18T00:57:42.000Z",
  durationMs: 2_742_000,
  timezone: "Asia/Seoul",
  gpsStatus: "OK",
  gpsOffsetMs: -850,
  routeName: "수도권제1순환고속도로",
  distanceMeters: 58_420,
  analysisState: "COMPLETED",
  createdAt: CREATED_AT,
  updatedAt: UPDATED_AT,
  revision: 4,
};

export const demoDetections: Detection[] = [
  {
    projectId: DEMO_PROJECT_ID,
    detectionId: "detection-road-debris-001-a",
    modelRunId: "analysis-run-2026-07-18-001",
    modelVersionId: "model-road-v3.4.0",
    tripId: DEMO_TRIP_ID,
    frameAssetId: demoFrameAsset.assetId,
    frameNumber: 18_432,
    timeMs: 615_015,
    incidentType: "ROAD_DEBRIS",
    subtype: "FALLEN_CARGO",
    confidence: 0.84,
    geometry: {
      kind: "BOUNDING_BOX",
      x: 0.432,
      y: 0.584,
      width: 0.126,
      height: 0.168,
    },
    trackId: "track-road-debris-73",
    measurements: {
      apparentWidthPixels: 484,
      lanePosition: "2차로 중앙",
      opticalFlowStable: true,
    },
    createdAt: "2026-07-18T00:38:12.000Z",
    updatedAt: "2026-07-18T00:38:12.000Z",
    revision: 1,
  },
  {
    projectId: DEMO_PROJECT_ID,
    detectionId: "detection-road-debris-001-b",
    modelRunId: "analysis-run-2026-07-18-001",
    modelVersionId: "model-road-v3.4.0",
    tripId: DEMO_TRIP_ID,
    frameAssetId: demoFrameAsset.assetId,
    frameNumber: 18_440,
    timeMs: 615_282,
    incidentType: "ROAD_DEBRIS",
    subtype: "FALLEN_CARGO",
    confidence: 0.87,
    geometry: {
      kind: "BOUNDING_BOX",
      x: 0.419,
      y: 0.563,
      width: 0.148,
      height: 0.191,
    },
    trackId: "track-road-debris-73",
    measurements: {
      apparentWidthPixels: 568,
      lanePosition: "2차로 중앙",
      opticalFlowStable: true,
    },
    createdAt: "2026-07-18T00:38:12.000Z",
    updatedAt: "2026-07-18T00:38:12.000Z",
    revision: 1,
  },
];

export const demoIncident: Incident = {
  projectId: DEMO_PROJECT_ID,
  incidentId: DEMO_INCIDENT_ID,
  tripId: DEMO_TRIP_ID,
  startTimeMs: 614_700,
  representativeTimeMs: 615_282,
  endTimeMs: 616_100,
  latitude: 37.401862,
  longitude: 127.115391,
  altitudeMeters: 71.4,
  gpsStatus: "OK",
  roadName: "수도권제1순환고속도로 판교방향 2차로",
  travelHeadingDegrees: 31.8,
  detectionIds: demoDetections.map((detection) => detection.detectionId),
  findingIds: [DEMO_FINDING_ID],
  representativeEvidenceId: "evidence-road-debris-overlay",
  reviewStatus: "ANSWERED",
  createdAt: "2026-07-18T00:38:15.000Z",
  updatedAt: UPDATED_AT,
  revision: 3,
};

export const demoFinding: IncidentFinding = {
  projectId: DEMO_PROJECT_ID,
  findingId: DEMO_FINDING_ID,
  incidentId: DEMO_INCIDENT_ID,
  incidentType: "ROAD_DEBRIS",
  subtype: "FALLEN_CARGO",
  aiConfidence: 0.87,
  aiSeverity: "URGENT",
  finalSeverity: "URGENT",
  aiMaintenanceAction: "즉시 도로 순찰대 출동 및 낙하물 제거",
  finalMaintenanceAction: "즉시 도로 순찰대 출동 및 낙하물 제거",
  aiGeometry: demoDetections[1].geometry,
  finalGeometry: {
    kind: "POLYGON",
    points: [
      { x: 0.421, y: 0.586 },
      { x: 0.492, y: 0.561 },
      { x: 0.563, y: 0.673 },
      { x: 0.467, y: 0.754 },
    ],
  },
  conditionAssessmentId: "ca-answer-road-debris-001",
  maintenanceDecisionId: "md-answer-road-debris-001",
  placeVerificationStatus: "VERIFIED",
  reviewStatus: "CONFIRMED",
  createdAt: "2026-07-18T00:38:15.000Z",
  updatedAt: UPDATED_AT,
  revision: 3,
};

export const demoQuestion: LabelQuestion = {
  projectId: DEMO_PROJECT_ID,
  questionId: DEMO_QUESTION_ID,
  tripId: DEMO_TRIP_ID,
  incidentId: DEMO_INCIDENT_ID,
  findingId: DEMO_FINDING_ID,
  questionType: "OBJECT_PRESENCE",
  annotationIntents: ["GEOMETRY", "CONDITION", "MAINTENANCE"],
  prompt: "도로 위에 표시된 물체가 낙하물이 맞나요?",
  policyMode: "ASSISTED_LEARNING",
  proposedClass: "ROAD_DEBRIS",
  proposedGeometry: demoDetections[1].geometry,
  proposedSeverity: "URGENT",
  proposedMaintenanceAction: "즉시 도로 순찰대 출동 및 낙하물 제거",
  confidence: 0.87,
  mediaRefs: [demoFrameAsset.assetId, demoClipAsset.assetId],
  gpsRef: {
    latitude: 37.401862,
    longitude: 127.115391,
    timeMs: 615_282,
  },
  mandatoryReasons: ["ROAD_DEBRIS", "URGENT_SEVERITY"],
  status: "CONFIRMED",
  createdAt: "2026-07-18T00:39:00.000Z",
  updatedAt: UPDATED_AT,
  revision: 2,
};

export const demoLaneQuestion: LabelQuestion = {
  projectId: DEMO_PROJECT_ID,
  questionId: "question-lane-faded-002",
  tripId: DEMO_TRIP_ID,
  incidentId: "incident-lane-faded-002",
  findingId: "finding-lane-faded-002",
  questionType: "CONDITION_ASSESSMENT",
  annotationIntents: ["GEOMETRY", "CONDITION", "MAINTENANCE"],
  prompt: "이 구간은 차선 재도색이 필요한 상태인가요?",
  policyMode: "ASSISTED_LEARNING",
  proposedClass: "LANE_MARKING_FADED",
  proposedGeometry: {
    kind: "POLYGON",
    points: [
      { x: 0.22, y: 0.78 },
      { x: 0.29, y: 0.78 },
      { x: 0.48, y: 0.44 },
      { x: 0.46, y: 0.44 },
    ],
  },
  proposedSeverity: "MEDIUM",
  proposedMaintenanceAction: "차선 재도색",
  confidence: 0.94,
  mediaRefs: ["asset-frame-lane-002", "asset-clip-lane-002"],
  gpsRef: {
    latitude: 37.413224,
    longitude: 127.132131,
    timeMs: 792_500,
  },
  mandatoryReasons: [],
  status: "QUEUED",
  createdAt: "2026-07-18T00:43:00.000Z",
  updatedAt: "2026-07-18T00:43:00.000Z",
  revision: 1,
};

export const demoAcceptedAnswer: LabelAnswer = {
  projectId: DEMO_PROJECT_ID,
  answerId: "answer-road-debris-001",
  questionId: DEMO_QUESTION_ID,
  response: "EDIT_GEOMETRY",
  selectedClass: "ROAD_DEBRIS",
  selectedSeverity: "URGENT",
  selectedAction: "즉시 도로 순찰대 출동 및 낙하물 제거",
  finalGeometry: demoFinding.finalGeometry,
  includeInTraining: true,
  answeredBy: "inspector-kim",
  note: "목재 적재물로 확인. 두 번째 프레임 기준 경계를 수정함.",
  createdAt: "2026-07-18T01:21:00.000Z",
  updatedAt: "2026-07-18T01:21:30.000Z",
  revision: 2,
};

export const demoRouteSegments: RouteSegment[] = [
  {
    projectId: DEMO_PROJECT_ID,
    routeSegmentId: "route-segment-001",
    tripId: DEMO_TRIP_ID,
    startTimeMs: 0,
    endTimeMs: 613_000,
    quality: "ORIGINAL",
    points: [
      {
        timeMs: 0,
        capturedAt: "2026-07-18T00:12:00.000Z",
        latitude: 37.394849,
        longitude: 127.110449,
        altitudeMeters: 68.1,
        speedKph: 71.2,
        headingDegrees: 28.5,
        interpolated: false,
      },
      {
        timeMs: 613_000,
        capturedAt: "2026-07-18T00:22:13.000Z",
        latitude: 37.401711,
        longitude: 127.115214,
        altitudeMeters: 71.1,
        speedKph: 65.3,
        headingDegrees: 31.1,
        interpolated: false,
      },
    ],
  },
  {
    projectId: DEMO_PROJECT_ID,
    routeSegmentId: "route-segment-002",
    tripId: DEMO_TRIP_ID,
    startTimeMs: 617_800,
    endTimeMs: 1_200_000,
    quality: "ORIGINAL",
    points: [
      {
        timeMs: 617_800,
        capturedAt: "2026-07-18T00:22:17.800Z",
        latitude: 37.402032,
        longitude: 127.115601,
        altitudeMeters: 71.5,
        speedKph: 62.9,
        headingDegrees: 32.4,
        interpolated: false,
      },
      {
        timeMs: 1_200_000,
        capturedAt: "2026-07-18T00:32:00.000Z",
        latitude: 37.451261,
        longitude: 127.154081,
        altitudeMeters: 84.2,
        speedKph: 77.4,
        headingDegrees: 18.2,
        interpolated: false,
      },
    ],
  },
];

export const demoGpsGaps: GpsGap[] = [
  {
    projectId: DEMO_PROJECT_ID,
    gpsGapId: "gps-gap-001",
    tripId: DEMO_TRIP_ID,
    fromTimeMs: 613_000,
    toTimeMs: 617_800,
    durationMs: 4_800,
    reason: "NO_FIX",
    renderMode: "DASHED_WARNING",
  },
];

export const demoEvidenceAssets: EvidenceAsset[] = [
  {
    projectId: DEMO_PROJECT_ID,
    evidenceAssetId: "evidence-road-debris-original",
    incidentId: DEMO_INCIDENT_ID,
    variant: "ORIGINAL_FRAME",
    mediaAssetId: demoFrameAsset.assetId,
    generatedBy: "analysis-run-2026-07-18-001",
    redactionStatus: "PENDING",
    createdAt: "2026-07-18T00:40:00.000Z",
    updatedAt: "2026-07-18T00:40:00.000Z",
    revision: 1,
  },
  {
    projectId: DEMO_PROJECT_ID,
    evidenceAssetId: "evidence-road-debris-overlay",
    incidentId: DEMO_INCIDENT_ID,
    variant: "AI_OVERLAY",
    mediaAssetId: demoFrameAsset.assetId,
    generatedBy: "overlay-renderer-2026-07-18-001",
    redactionStatus: "PENDING",
    parentEvidenceAssetId: "evidence-road-debris-original",
    createdAt: "2026-07-18T00:40:10.000Z",
    updatedAt: "2026-07-18T00:40:10.000Z",
    revision: 1,
  },
  {
    projectId: DEMO_PROJECT_ID,
    evidenceAssetId: "evidence-road-debris-redacted",
    incidentId: DEMO_INCIDENT_ID,
    variant: "SHARED_REDACTED",
    mediaAssetId: demoFrameAsset.assetId,
    generatedBy: "privacy-redactor-2026-07-18-001",
    redactionStatus: "CONFIRMED",
    parentEvidenceAssetId: "evidence-road-debris-original",
    createdAt: "2026-07-18T00:42:00.000Z",
    updatedAt: "2026-07-18T01:12:00.000Z",
    revision: 2,
  },
  {
    projectId: DEMO_PROJECT_ID,
    evidenceAssetId: "evidence-road-debris-clip",
    incidentId: DEMO_INCIDENT_ID,
    variant: "CONTEXT_CLIP",
    mediaAssetId: demoClipAsset.assetId,
    generatedBy: "evidence-pipeline-2026-07-18-001",
    redactionStatus: "CONFIRMED",
    createdAt: "2026-07-18T00:41:00.000Z",
    updatedAt: "2026-07-18T01:12:00.000Z",
    revision: 2,
  },
  {
    projectId: DEMO_PROJECT_ID,
    evidenceAssetId: "evidence-road-debris-map",
    incidentId: DEMO_INCIDENT_ID,
    variant: "ROUTE_MAP",
    mediaAssetId: demoMapAsset.assetId,
    generatedBy: "map-renderer-2026-07-18-001",
    redactionStatus: "NOT_REQUIRED",
    createdAt: "2026-07-18T00:41:30.000Z",
    updatedAt: "2026-07-18T00:41:30.000Z",
    revision: 1,
  },
];

export const demoReviewActions: ReviewAction[] = [
  {
    projectId: DEMO_PROJECT_ID,
    reviewActionId: "review-action-question-001",
    targetType: "QUESTION",
    targetId: DEMO_QUESTION_ID,
    action: "EDIT",
    actorId: "inspector-kim",
    expectedRevision: 1,
    before: {
      response: "ACCEPT",
      geometrySource: "AI",
    },
    after: {
      response: "EDIT_GEOMETRY",
      geometrySource: "USER",
    },
    reason: "낙하물 외곽을 더 정확히 포함하도록 경계를 수정함",
    createdAt: "2026-07-18T01:21:30.000Z",
    updatedAt: "2026-07-18T01:21:30.000Z",
    revision: 1,
  },
];

const potholeTrainIds = Array.from(
  { length: 70 },
  (_, index) => `ga-pothole-train-${String(index + 1).padStart(3, "0")}`,
);
const debrisTrainIds = Array.from(
  { length: 70 },
  (_, index) => `ga-debris-train-${String(index + 1).padStart(3, "0")}`,
);
const potholeValidationIds = Array.from(
  { length: 30 },
  (_, index) => `ga-pothole-val-${String(index + 1).padStart(3, "0")}`,
);
const debrisValidationIds = Array.from(
  { length: 30 },
  (_, index) => `ga-debris-val-${String(index + 1).padStart(3, "0")}`,
);
const potholeTestIds = Array.from(
  { length: 50 },
  (_, index) => `ga-pothole-test-${String(index + 1).padStart(3, "0")}`,
);
const debrisTestIds = Array.from(
  { length: 50 },
  (_, index) => `ga-debris-test-${String(index + 1).padStart(3, "0")}`,
);
const allGeometryAnnotationIds = [
  ...potholeTrainIds,
  ...debrisTrainIds,
  ...potholeValidationIds,
  ...debrisValidationIds,
  ...potholeTestIds,
  ...debrisTestIds,
];

export const demoDatasetVersion: DatasetVersion = {
  projectId: DEMO_PROJECT_ID,
  datasetVersionId: "dataset-road-v3.5.0",
  version: "3.5.0",
  geometryAnnotationIds: allGeometryAnnotationIds,
  conditionAssessmentIds: ["ca-answer-road-debris-001"],
  maintenanceDecisionIds: ["md-answer-road-debris-001"],
  ocrGroundTruthIds: [],
  split: {
    trainAnnotationIds: [...potholeTrainIds, ...debrisTrainIds],
    validationAnnotationIds: [
      ...potholeValidationIds,
      ...debrisValidationIds,
    ],
    testAnnotationIds: [...potholeTestIds, ...debrisTestIds],
    groupedBy: ["CAPTURE_DATE", "ROUTE", "VIDEO"],
  },
  classDistribution: {
    POTHOLE: 150,
    ROAD_DEBRIS: 150,
  },
  sha256: SHA_DATASET,
  basedOnDatasetVersionId: "dataset-road-v3.4.0",
  state: "FROZEN",
  createdAt: "2026-07-18T01:15:00.000Z",
  updatedAt: "2026-07-18T01:15:00.000Z",
  revision: 1,
};

export const demoActiveModel: ModelVersion = {
  projectId: DEMO_PROJECT_ID,
  modelVersionId: "model-road-v3.4.0",
  version: "3.4.0",
  basedOnModelVersionId: "model-road-v3.3.1",
  datasetVersionId: "dataset-road-v3.4.0",
  state: "ACTIVE",
  framework: "PyTorch 2.7 / Ultralytics",
  inputSize: 1280,
  weightsSha256: SHA_ACTIVE_MODEL,
  supportedClasses: ["POTHOLE", "ROAD_DEBRIS"],
  classMetrics: [
    {
      incidentType: "POTHOLE",
      precision: 0.904,
      recall: 0.91,
      falsePositiveRate: 0.061,
      duplicateRate: 0.028,
      testIncidentCount: 64,
    },
    {
      incidentType: "ROAD_DEBRIS",
      precision: 0.852,
      recall: 0.806,
      falsePositiveRate: 0.074,
      duplicateRate: 0.039,
      testIncidentCount: 58,
    },
  ],
  createdBy: "training-job-2026-06-25-002",
  createdAt: "2026-06-25T08:20:00.000Z",
  updatedAt: "2026-06-25T08:20:00.000Z",
  revision: 1,
};

export const demoCandidateModel: ModelVersion = {
  projectId: DEMO_PROJECT_ID,
  modelVersionId: "model-road-v3.5.0-rc1",
  version: "3.5.0-rc1",
  basedOnModelVersionId: demoActiveModel.modelVersionId,
  datasetVersionId: demoDatasetVersion.datasetVersionId,
  state: "CANDIDATE",
  framework: "PyTorch 2.7 / Ultralytics",
  inputSize: 1280,
  weightsSha256: SHA_CANDIDATE_MODEL,
  supportedClasses: ["POTHOLE", "ROAD_DEBRIS"],
  classMetrics: [
    {
      incidentType: "POTHOLE",
      precision: 0.918,
      recall: 0.934,
      falsePositiveRate: 0.052,
      duplicateRate: 0.024,
      testIncidentCount: 64,
    },
    {
      incidentType: "ROAD_DEBRIS",
      precision: 0.879,
      recall: 0.848,
      falsePositiveRate: 0.068,
      duplicateRate: 0.033,
      testIncidentCount: 58,
    },
  ],
  createdBy: "training-job-2026-07-18-003",
  createdAt: "2026-07-18T02:10:00.000Z",
  updatedAt: "2026-07-18T02:10:00.000Z",
  revision: 1,
};

export const demoTrainingGateInput: TrainingGateInput = {
  projectId: DEMO_PROJECT_ID,
  newConfirmedLabelCount: 126,
  targetClasses: ["POTHOLE", "ROAD_DEBRIS"],
  validationSamplesByClass: {
    POTHOLE: 30,
    ROAD_DEBRIS: 30,
  },
  hasFixedValidationSet: true,
  hasFixedTestSet: true,
  datasetProjectId: DEMO_PROJECT_ID,
};

export const demoReportDraft: ReportDraft = {
  projectId: DEMO_PROJECT_ID,
  reportDraftId: "report-draft-trip-001",
  tripId: DEMO_TRIP_ID,
  providerId: "provider-codex-cli",
  title: "수도권제1순환고속도로 도로시설 점검보고서",
  sections: [
    {
      sectionId: "overview",
      title: "1. 점검 개요",
      body:
        "2026년 7월 18일 판교JC에서 구리IC 방향을 차량 영상과 GPS로 점검하였다.",
      incidentIds: [],
      evidenceAssetIds: [],
    },
    {
      sectionId: "urgent-findings",
      title: "2. 긴급 조치 필요 사항",
      body:
        "판교방향 2차로에서 목재 적재물로 판단되는 낙하물 1건이 확인되어 즉시 제거가 필요하다.",
      incidentIds: [DEMO_INCIDENT_ID],
      evidenceAssetIds: [
        "evidence-road-debris-redacted",
        "evidence-road-debris-map",
      ],
    },
  ],
  generatedFromIncidentIds: [DEMO_INCIDENT_ID],
  state: "REVIEWED",
  createdAt: "2026-07-18T03:00:00.000Z",
  updatedAt: "2026-07-18T03:12:00.000Z",
  revision: 2,
};

export const demoDocumentEditPlan: DocumentEditPlan = {
  projectId: DEMO_PROJECT_ID,
  documentEditPlanId: "document-edit-plan-trip-001",
  reportDraftId: demoReportDraft.reportDraftId,
  documentSnapshotId: "hwp-snapshot-before-trip-001",
  templateAssetId: "asset-template-standard-inspection-hwpx",
  operations: [
    {
      operationId: "edit-op-title",
      kind: "FILL_TEXT",
      target: "bookmark:report_title",
      expectedValue: "도로시설 점검보고서",
      value: demoReportDraft.title,
      source: "report-draft-trip-001/overview",
    },
    {
      operationId: "edit-op-incident-row",
      kind: "ADD_ROW",
      target: "table:urgent_findings",
      values: [
        "00:10:15.282",
        "낙하물",
        "긴급",
        "즉시 도로 순찰대 출동 및 낙하물 제거",
      ],
      source: DEMO_INCIDENT_ID,
    },
    {
      operationId: "edit-op-evidence-image",
      kind: "INSERT_IMAGE",
      target: "picture_slot:urgent_finding_1",
      evidenceAssetId: "evidence-road-debris-redacted",
      source: DEMO_INCIDENT_ID,
    },
    {
      operationId: "edit-op-route-map",
      kind: "INSERT_MAP",
      target: "picture_slot:route_map_1",
      evidenceAssetId: "evidence-road-debris-map",
      source: DEMO_INCIDENT_ID,
    },
  ],
  acceptedOperationIds: [
    "edit-op-title",
    "edit-op-incident-row",
    "edit-op-evidence-image",
    "edit-op-route-map",
  ],
  rejectedOperationIds: [],
  state: "APPROVED",
  createdAt: "2026-07-18T03:15:00.000Z",
  updatedAt: "2026-07-18T03:20:00.000Z",
  revision: 2,
};

export const demoProviderConnections: ProviderConnection[] = [
  {
    projectId: DEMO_PROJECT_ID,
    providerConnectionId: "provider-codex-cli",
    provider: "CODEX_CLI",
    status: "CONNECTED",
    version: "0.49.0",
    capabilities: [
      "REPORT_DRAFT",
      "DOCUMENT_EDIT_PLAN",
      "USAGE_SUMMARY",
    ],
    lastCheckedAt: "2026-07-18T02:55:00.000Z",
    createdAt: "2026-07-18T02:54:00.000Z",
    updatedAt: "2026-07-18T02:55:00.000Z",
    revision: 2,
  },
  {
    projectId: DEMO_PROJECT_ID,
    providerConnectionId: "provider-claude-code",
    provider: "CLAUDE_CODE",
    status: "SIGNED_OUT",
    capabilities: ["REPORT_DRAFT", "DOCUMENT_EDIT_PLAN", "USAGE_SUMMARY"],
    lastCheckedAt: "2026-07-18T02:55:00.000Z",
    createdAt: "2026-07-18T02:54:00.000Z",
    updatedAt: "2026-07-18T02:55:00.000Z",
    revision: 2,
  },
  {
    projectId: DEMO_PROJECT_ID,
    providerConnectionId: "provider-local-model",
    provider: "LOCAL_MODEL",
    status: "DISABLED",
    capabilities: [],
    disabledReason:
      "준비 중: 한국어 보고서 생성, 스키마 준수 및 HWP 편집계획 품질 기준 충족 후 활성화",
    createdAt: "2026-07-18T02:54:00.000Z",
    updatedAt: "2026-07-18T02:54:00.000Z",
    revision: 1,
  },
];

function exportArtifact(
  exportArtifactId: string,
  kind: ExportArtifact["kind"],
  fileName: string,
  mimeType: string,
  shaCharacter: string,
  rowCount?: number,
): ExportArtifact {
  return {
    projectId: DEMO_PROJECT_ID,
    exportArtifactId,
    kind,
    fileName,
    relativePath: `exports\\trip-2026-07-18-am\\${fileName}`,
    mimeType,
    byteSize: rowCount === undefined ? 7_240 : Math.max(512, rowCount * 214),
    sha256: shaCharacter.repeat(64),
    rowCount,
    generatedBy: "export-job-2026-07-18-001",
    createdAt: "2026-07-18T03:45:00.000Z",
    updatedAt: "2026-07-18T03:45:00.000Z",
    revision: 1,
  };
}

export const demoExportArtifacts: ExportArtifact[] = [
  exportArtifact(
    "export-raw-detections",
    "RAW_DETECTIONS_CSV",
    "raw_detections.csv",
    "text/csv",
    "3",
    2,
  ),
  exportArtifact(
    "export-reviewed-incidents",
    "REVIEWED_INCIDENTS_CSV",
    "reviewed_incidents.csv",
    "text/csv",
    "4",
    1,
  ),
  exportArtifact(
    "export-label-questions",
    "LABEL_QUESTIONS_CSV",
    "label_questions.csv",
    "text/csv",
    "5",
    2,
  ),
  exportArtifact(
    "export-annotations",
    "ANNOTATIONS_CSV",
    "annotations.csv",
    "text/csv",
    "6",
    3,
  ),
  exportArtifact(
    "export-ocr-observations",
    "OCR_OBSERVATIONS_CSV",
    "ocr_observations.csv",
    "text/csv",
    "7",
    0,
  ),
  exportArtifact(
    "export-route-points",
    "ROUTE_POINTS_CSV",
    "route_points.csv",
    "text/csv",
    "8",
    4,
  ),
  exportArtifact(
    "export-maintenance-actions",
    "MAINTENANCE_ACTIONS_CSV",
    "maintenance_actions.csv",
    "text/csv",
    "9",
    1,
  ),
  exportArtifact(
    "export-report-payload",
    "REPORT_PAYLOAD_JSON",
    "report_payload.json",
    "application/json",
    "a",
  ),
  exportArtifact(
    "export-manifest",
    "MANIFEST_JSON",
    "manifest.json",
    "application/json",
    "b",
  ),
];

export const demoManifest: TrafficVissonManifest = {
  projectId: DEMO_PROJECT_ID,
  schemaVersion: 1,
  generatedAt: "2026-07-18T03:45:00.000Z",
  appVersion: "0.1.0",
  tripIds: [DEMO_TRIP_ID],
  inputs: [
    {
      assetId: demoVideoAsset.assetId,
      sha256: demoVideoAsset.sha256,
      sourceKind: demoVideoAsset.source.kind,
    },
    {
      assetId: demoGpsAsset.assetId,
      sha256: demoGpsAsset.sha256,
      sourceKind: demoGpsAsset.source.kind,
    },
  ],
  datasetVersionIds: [demoDatasetVersion.datasetVersionId],
  modelVersionIds: [
    demoActiveModel.modelVersionId,
    demoCandidateModel.modelVersionId,
  ],
  exportArtifactIds: demoExportArtifacts.map(
    (artifact) => artifact.exportArtifactId,
  ),
  provenance: {
    analysisRunIds: ["analysis-run-2026-07-18-001"],
    generatedBy: "TrafficVisson Desktop 0.1.0",
  },
};

export const demoDomainFixture = {
  project: demoProject,
  mediaAssets: [
    demoVideoAsset,
    demoGpsAsset,
    demoFrameAsset,
    demoClipAsset,
    demoMapAsset,
  ],
  trip: demoTrip,
  detections: demoDetections,
  incident: demoIncident,
  finding: demoFinding,
  questions: [demoQuestion, demoLaneQuestion],
  answers: [demoAcceptedAnswer],
  routeSegments: demoRouteSegments,
  gpsGaps: demoGpsGaps,
  evidenceAssets: demoEvidenceAssets,
  reviewActions: demoReviewActions,
  datasetVersion: demoDatasetVersion,
  models: [demoActiveModel, demoCandidateModel],
  trainingGateInput: demoTrainingGateInput,
  reportDraft: demoReportDraft,
  documentEditPlan: demoDocumentEditPlan,
  providerConnections: demoProviderConnections,
  exportArtifacts: demoExportArtifacts,
  manifest: demoManifest,
};
