/// <reference types="vite/client" />

interface TrafficVissonVideoFileMetadata {
  readonly name: string;
  readonly extension: "mp4" | "mov";
  readonly sizeBytes: number;
  readonly lastModifiedMs: number;
}

interface TrafficVissonVideoSelection {
  readonly selectionId: string | null;
  readonly files: readonly TrafficVissonVideoFileMetadata[];
}

interface TrafficVissonDocumentSelection {
  readonly selectionId: string | null;
  readonly file: null | {
    readonly name: string;
    readonly extension: "hwp" | "hwpx";
    readonly sizeBytes: number;
    readonly lastModifiedMs: number;
  };
}

interface TrafficVissonGpxSelection {
  readonly selectionId: string | null;
  readonly file: null | {
    readonly name: string;
    readonly extension: "gpx";
    readonly sizeBytes: number;
    readonly lastModifiedMs: number;
  };
}

interface TrafficVissonDirectorySelection {
  readonly selectionId: string | null;
  readonly directory: null | {
    readonly name: string;
  };
}

type TrafficVissonAutoLabelingIssueType =
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

interface TrafficVissonAutoLabelingCompileRequest {
  readonly projectId: string;
  readonly command: string;
  readonly issueType?: TrafficVissonAutoLabelingIssueType;
}

interface TrafficVissonAutoLabelingRunRequest {
  readonly projectId: string;
  readonly jobId?: string;
  readonly recipeId: string;
  readonly command: string;
  readonly issueType: TrafficVissonAutoLabelingIssueType;
  readonly scope: "current_job" | "project";
  readonly mode: "proposal_only" | "assisted_auto";
  readonly userConfirmed: true;
}

interface TrafficVissonProjectRequest {
  readonly projectId: string;
}

interface TrafficVissonIncidentListRequest
  extends TrafficVissonProjectRequest {
  readonly analysisJobId?: string;
  readonly includeInactive?: boolean;
}

interface TrafficVissonDatasetCreateRequest {
  readonly projectId: string;
  readonly userConfirmed: true;
}

interface TrafficVissonDesktopApi {
  health(): Promise<unknown>;
  dialogs: {
    selectVideos(): Promise<TrafficVissonVideoSelection>;
    selectGpxFile(): Promise<TrafficVissonGpxSelection>;
    selectHwpDocument(): Promise<TrafficVissonDocumentSelection>;
    selectExportDirectory(): Promise<TrafficVissonDirectorySelection>;
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
  candidateQuestions: {
    list(request: unknown): Promise<unknown>;
    answer(request: unknown): Promise<unknown>;
  };
  incidents: {
    list(request: TrafficVissonIncidentListRequest): Promise<unknown>;
  };
  labels: {
    stats(request: TrafficVissonProjectRequest): Promise<unknown>;
  };
  datasets: {
    list(request: TrafficVissonProjectRequest): Promise<unknown>;
    create(request: TrafficVissonDatasetCreateRequest): Promise<unknown>;
  };
  sources: {
    candidates(): Promise<unknown>;
  };
  autoLabeling: {
    compile(
      request: TrafficVissonAutoLabelingCompileRequest,
    ): Promise<unknown>;
    run(request: TrafficVissonAutoLabelingRunRequest): Promise<unknown>;
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
    generate(request: unknown): Promise<unknown>;
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
  trafficVisson?: TrafficVissonDesktopApi;
}
