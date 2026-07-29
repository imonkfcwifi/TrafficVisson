export type Id = string;
export type IsoTimestamp = string;
export type Sha256 = string;
export type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  | JsonPrimitive
  | JsonValue[]
  | { [key: string]: JsonValue };

export type ProjectStatus = "ACTIVE" | "ARCHIVED";
export type QuestionPolicyMode =
  | "ASSISTED_LEARNING"
  | "REVIEW_ALL"
  | "REVIEW_UNCERTAIN";
export type JobState =
  | "QUEUED"
  | "RUNNING"
  | "PAUSED"
  | "WAITING_FOR_USER"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";
export type Severity = "LOW" | "MEDIUM" | "HIGH" | "URGENT";
export type IncidentType =
  | "POTHOLE"
  | "ROAD_SURFACE_DAMAGE"
  | "ROAD_DEBRIS"
  | "MEDIAN_BARRIER_DAMAGE"
  | "SIGN_DAMAGE"
  | "SIGN_VEGETATION_OCCLUSION"
  | "ROADSIDE_MOWING_REQUIRED"
  | "GUIDE_SIGN_IDENTITY"
  | "TUNNEL_IDENTITY"
  | "TUNNEL_NAME_OCCLUSION"
  | "LANE_MARKING_FADED"
  | "COLORED_GUIDANCE_FADED";

export type GpsStatus =
  | "OK"
  | "MISSING"
  | "OUT_OF_RANGE"
  | "NO_TRACK";
export type ReviewStatus =
  | "UNREVIEWED"
  | "ANSWERED"
  | "CONFIRMED"
  | "REJECTED"
  | "CONFLICT";
export type VerificationStatus =
  | "VERIFIED"
  | "PROBABLE"
  | "UNVERIFIED"
  | "CONFLICT"
  | "MANUAL_CONFIRMED";

export interface ProjectScoped {
  projectId: Id;
}

export interface AuditedEntity extends ProjectScoped {
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
  revision: number;
}

export interface Project {
  projectId: Id;
  schemaVersion: number;
  name: string;
  rootPath: string;
  status: ProjectStatus;
  questionPolicyMode: QuestionPolicyMode;
  activeModelVersionId?: Id;
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export interface MediaAsset extends AuditedEntity {
  assetId: Id;
  kind:
    | "VIDEO"
    | "GPX"
    | "FRAME"
    | "CLIP"
    | "IMAGE"
    | "REPORT"
    | "MODEL"
    | "DATASET";
  relativePath: string;
  mimeType: string;
  byteSize: number;
  sha256: Sha256;
  source:
    | { kind: "LOCAL_FILE"; originalName: string }
    | {
        kind: "REMOTE_URL";
        provider: string;
        sourceUrl: string;
        downloadedAt: IsoTimestamp;
      }
    | { kind: "GENERATED"; producer: string };
  durationMs?: number;
  width?: number;
  height?: number;
  frameRate?: number;
}

export interface Trip extends AuditedEntity {
  tripId: Id;
  name: string;
  videoAssetId: Id;
  gpsAssetId?: Id;
  startTime?: IsoTimestamp;
  endTime?: IsoTimestamp;
  durationMs: number;
  timezone: string;
  gpsStatus: GpsStatus;
  gpsOffsetMs: number;
  routeName?: string;
  distanceMeters?: number;
  analysisState: JobState;
}

export interface NormalizedPoint {
  x: number;
  y: number;
}

export interface NormalizedBoundingBox {
  kind: "BOUNDING_BOX";
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface NormalizedPolygon {
  kind: "POLYGON";
  points: NormalizedPoint[];
}

export interface RleMask {
  kind: "RLE_MASK";
  width: number;
  height: number;
  counts: string;
}

export type Geometry =
  | NormalizedBoundingBox
  | NormalizedPolygon
  | RleMask;

export interface Detection extends AuditedEntity {
  detectionId: Id;
  modelRunId: Id;
  modelVersionId: Id;
  tripId: Id;
  frameAssetId: Id;
  frameNumber: number;
  timeMs: number;
  incidentType: IncidentType;
  subtype?: string;
  confidence: number;
  geometry: Geometry;
  trackId?: string;
  measurements: Record<string, number | string | boolean | null>;
}

export interface Incident extends AuditedEntity {
  incidentId: Id;
  tripId: Id;
  startTimeMs: number;
  representativeTimeMs: number;
  endTimeMs: number;
  latitude?: number;
  longitude?: number;
  altitudeMeters?: number;
  gpsStatus: GpsStatus;
  roadName?: string;
  travelHeadingDegrees?: number;
  detectionIds: Id[];
  findingIds: Id[];
  representativeEvidenceId?: Id;
  reviewStatus: ReviewStatus;
}

export interface IncidentFinding extends AuditedEntity {
  findingId: Id;
  incidentId: Id;
  incidentType: IncidentType;
  subtype?: string;
  aiConfidence: number;
  aiSeverity?: Severity;
  finalSeverity?: Severity;
  aiMaintenanceAction?: string;
  finalMaintenanceAction?: string;
  aiGeometry?: Geometry;
  finalGeometry?: Geometry;
  conditionAssessmentId?: Id;
  maintenanceDecisionId?: Id;
  ocrGroundTruthId?: Id;
  placeVerificationStatus?: VerificationStatus;
  reviewStatus: ReviewStatus;
}

export type QuestionType =
  | "OBJECT_PRESENCE"
  | "GEOMETRY_CONFIRMATION"
  | "CONDITION_ASSESSMENT"
  | "MAINTENANCE_REQUIRED"
  | "OCR_VERIFICATION";
export type AnnotationIntent =
  | "GEOMETRY"
  | "CONDITION"
  | "MAINTENANCE"
  | "OCR";
export type MandatoryQuestionReason =
  | "ROAD_DEBRIS"
  | "URGENT_SEVERITY"
  | "QUALITY_SAMPLE"
  | "POLICY_OVERRIDE";
export type LabelQuestionStatus =
  | "QUEUED"
  | "ANSWERED_ACCEPT"
  | "ANSWERED_REJECT"
  | "ANSWERED_UNSURE"
  | "EDITING_GEOMETRY"
  | "CONFIRMED"
  | "SUPERSEDED";

export interface LabelQuestion extends AuditedEntity {
  questionId: Id;
  tripId: Id;
  incidentId: Id;
  findingId: Id;
  questionType: QuestionType;
  annotationIntents: AnnotationIntent[];
  prompt: string;
  policyMode: QuestionPolicyMode;
  proposedClass: IncidentType;
  proposedGeometry?: Geometry;
  proposedSeverity?: Severity;
  proposedMaintenanceAction?: string;
  proposedOcrText?: string;
  confidence: number;
  mediaRefs: Id[];
  gpsRef?: { latitude: number; longitude: number; timeMs: number };
  mandatoryReasons: MandatoryQuestionReason[];
  status: LabelQuestionStatus;
}

export type LabelResponse =
  | "ACCEPT"
  | "REJECT"
  | "UNSURE"
  | "EDIT_GEOMETRY"
  | "CHANGE_CLASS";

export interface LabelAnswer extends AuditedEntity {
  answerId: Id;
  questionId: Id;
  response: LabelResponse;
  selectedClass?: IncidentType;
  selectedSeverity?: Severity;
  selectedAction?: string;
  finalGeometry?: Geometry;
  ocrText?: string;
  includeInTraining: boolean;
  answeredBy: string;
  note?: string;
}

export type AnnotationSource =
  | "AI_ACCEPTED"
  | "USER_EDITED"
  | "USER_NEGATIVE"
  | "MANUAL";
export type AnnotationReviewState =
  | "DRAFT"
  | "CONFIRMED"
  | "EXCLUDED";

export interface GeometryAnnotation extends AuditedEntity {
  annotationId: Id;
  questionId: Id;
  answerId: Id;
  tripId: Id;
  incidentId: Id;
  findingId: Id;
  incidentType: IncidentType;
  verdict: "POSITIVE" | "NEGATIVE";
  geometry?: Geometry;
  frameAssetId: Id;
  source: AnnotationSource;
  reviewState: AnnotationReviewState;
}

export interface ConditionAssessment extends AuditedEntity {
  assessmentId: Id;
  questionId: Id;
  answerId: Id;
  incidentId: Id;
  findingId: Id;
  condition:
    | "DAMAGED"
    | "OCCLUDED"
    | "FADED"
    | "OVERGROWN"
    | "OBSTRUCTION";
  outcome: "PRESENT" | "ABSENT" | "UNSURE";
  severity?: Severity;
  source: AnnotationSource;
  reviewState: AnnotationReviewState;
}

export interface MaintenanceDecision extends AuditedEntity {
  decisionId: Id;
  questionId: Id;
  answerId: Id;
  incidentId: Id;
  findingId: Id;
  required: boolean | null;
  action?: string;
  severity?: Severity;
  decidedBy: string;
  reviewState: AnnotationReviewState;
}

export interface OcrGroundTruth extends AuditedEntity {
  ocrGroundTruthId: Id;
  questionId: Id;
  answerId: Id;
  incidentId: Id;
  findingId: Id;
  rawText: string;
  normalizedText?: string;
  language: "ko" | "en" | "mixed";
  verificationStatus: VerificationStatus;
  referenceAssetId?: Id;
  reviewState: AnnotationReviewState;
}

export interface DatasetSplit {
  trainAnnotationIds: Id[];
  validationAnnotationIds: Id[];
  testAnnotationIds: Id[];
  groupedBy: Array<"CAPTURE_DATE" | "ROUTE" | "VIDEO">;
}

export interface DatasetVersion extends AuditedEntity {
  datasetVersionId: Id;
  version: string;
  geometryAnnotationIds: Id[];
  conditionAssessmentIds: Id[];
  maintenanceDecisionIds: Id[];
  ocrGroundTruthIds: Id[];
  split: DatasetSplit;
  classDistribution: Partial<Record<IncidentType, number>>;
  sha256: Sha256;
  basedOnDatasetVersionId?: Id;
  state: "DRAFT" | "FROZEN" | "ARCHIVED";
}

export interface ClassMetrics {
  incidentType: IncidentType;
  precision: number;
  recall: number;
  falsePositiveRate: number;
  duplicateRate: number;
  testIncidentCount: number;
}

export interface ModelVersion extends AuditedEntity {
  modelVersionId: Id;
  version: string;
  basedOnModelVersionId?: Id;
  datasetVersionId?: Id;
  state:
    | "BASE"
    | "TRAINING"
    | "CANDIDATE"
    | "ACTIVE"
    | "REJECTED"
    | "ARCHIVED";
  framework: string;
  inputSize: number;
  weightsSha256: Sha256;
  supportedClasses: IncidentType[];
  classMetrics: ClassMetrics[];
  createdBy: string;
}

export interface GpsTrackPoint {
  timeMs: number;
  capturedAt?: IsoTimestamp;
  latitude: number;
  longitude: number;
  altitudeMeters?: number;
  speedKph?: number;
  headingDegrees?: number;
  interpolated: boolean;
}

export interface RouteSegment extends ProjectScoped {
  routeSegmentId: Id;
  tripId: Id;
  startTimeMs: number;
  endTimeMs: number;
  points: GpsTrackPoint[];
  quality: "ORIGINAL" | "INTERPOLATED";
}

export interface GpsGap extends ProjectScoped {
  gpsGapId: Id;
  tripId: Id;
  fromTimeMs: number;
  toTimeMs: number;
  durationMs: number;
  reason: "SAMPLE_INTERVAL_EXCEEDED" | "OUTSIDE_VIDEO_RANGE" | "NO_FIX";
  renderMode: "DASHED_WARNING";
}

export interface EvidenceAsset extends AuditedEntity {
  evidenceAssetId: Id;
  incidentId: Id;
  variant:
    | "ORIGINAL_FRAME"
    | "AI_OVERLAY"
    | "USER_FINAL"
    | "SHARED_REDACTED"
    | "CONTEXT_CLIP"
    | "ROUTE_MAP";
  mediaAssetId: Id;
  generatedBy: string;
  redactionStatus: "NOT_REQUIRED" | "PENDING" | "AUTO_REDACTED" | "CONFIRMED";
  parentEvidenceAssetId?: Id;
}

export interface ReviewAction extends AuditedEntity {
  reviewActionId: Id;
  targetType:
    | "QUESTION"
    | "INCIDENT"
    | "FINDING"
    | "EVIDENCE"
    | "MODEL"
    | "REPORT";
  targetId: Id;
  action:
    | "ACCEPT"
    | "REJECT"
    | "EDIT"
    | "MERGE"
    | "SPLIT"
    | "EXCLUDE_FROM_TRAINING"
    | "ADD_REDACTION"
    | "PROMOTE"
    | "ROLLBACK";
  actorId: string;
  expectedRevision: number;
  before: Record<string, JsonValue>;
  after: Record<string, JsonValue>;
  reason?: string;
}

export interface ReportSection {
  sectionId: string;
  title: string;
  body: string;
  incidentIds: Id[];
  evidenceAssetIds: Id[];
}

export interface ReportDraft extends AuditedEntity {
  reportDraftId: Id;
  tripId: Id;
  providerId?: Id;
  title: string;
  sections: ReportSection[];
  generatedFromIncidentIds: Id[];
  state: "DRAFT" | "REVIEWED" | "SUPERSEDED";
}

export type DocumentEditOperation =
  | {
      operationId: Id;
      kind: "FILL_TEXT";
      target: string;
      expectedValue?: string;
      value: string;
      source: string;
    }
  | {
      operationId: Id;
      kind: "FILL_CELL";
      target: string;
      expectedValue?: string;
      value: string;
      source: string;
    }
  | {
      operationId: Id;
      kind: "ADD_ROW";
      target: string;
      values: string[];
      source: string;
    }
  | {
      operationId: Id;
      kind: "INSERT_IMAGE" | "INSERT_MAP";
      target: string;
      evidenceAssetId: Id;
      source: string;
    };

export interface DocumentEditPlan extends AuditedEntity {
  documentEditPlanId: Id;
  reportDraftId: Id;
  documentSnapshotId: Id;
  templateAssetId: Id;
  operations: DocumentEditOperation[];
  acceptedOperationIds: Id[];
  rejectedOperationIds: Id[];
  state:
    | "PROPOSED"
    | "PARTIALLY_REVIEWED"
    | "APPROVED"
    | "APPLIED"
    | "REJECTED"
    | "ROLLED_BACK";
}

export type ProviderCapability =
  | "REPORT_DRAFT"
  | "DOCUMENT_EDIT_PLAN"
  | "USAGE_SUMMARY";

export interface ProviderConnection extends AuditedEntity {
  providerConnectionId: Id;
  provider: "CODEX_CLI" | "CLAUDE_CODE" | "LOCAL_MODEL";
  status:
    | "NOT_INSTALLED"
    | "SIGNED_OUT"
    | "CONNECTED"
    | "EXPIRED"
    | "DISABLED";
  version?: string;
  capabilities: ProviderCapability[];
  lastCheckedAt?: IsoTimestamp;
  disabledReason?: string;
}

export interface ExportArtifact extends AuditedEntity {
  exportArtifactId: Id;
  kind:
    | "RAW_DETECTIONS_CSV"
    | "REVIEWED_INCIDENTS_CSV"
    | "LABEL_QUESTIONS_CSV"
    | "ANNOTATIONS_CSV"
    | "OCR_OBSERVATIONS_CSV"
    | "ROUTE_POINTS_CSV"
    | "MAINTENANCE_ACTIONS_CSV"
    | "REPORT_PAYLOAD_JSON"
    | "MANIFEST_JSON"
    | "TRIP_PACKAGE"
    | "HWP"
    | "HWPX";
  fileName: string;
  relativePath: string;
  mimeType: string;
  byteSize: number;
  sha256: Sha256;
  rowCount?: number;
  generatedBy: string;
}

export interface ManifestInput {
  assetId: Id;
  sha256: Sha256;
  sourceKind: MediaAsset["source"]["kind"];
}

export interface TrafficVissonManifest extends ProjectScoped {
  schemaVersion: number;
  generatedAt: IsoTimestamp;
  appVersion: string;
  tripIds: Id[];
  inputs: ManifestInput[];
  datasetVersionIds: Id[];
  modelVersionIds: Id[];
  exportArtifactIds: Id[];
  provenance: {
    analysisRunIds: Id[];
    generatedBy: string;
  };
}

export type LearningArtifact =
  | GeometryAnnotation
  | ConditionAssessment
  | MaintenanceDecision
  | OcrGroundTruth;

export interface AnnotationBundle {
  projectId: Id;
  questionId: Id;
  answerId: Id;
  artifacts: LearningArtifact[];
  excludedFromTraining: boolean;
  exclusionReason?: "UNSURE" | "USER_EXCLUDED";
}
