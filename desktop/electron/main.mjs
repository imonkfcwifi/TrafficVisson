import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  net,
  Notification,
  protocol,
  session,
} from "electron";
import { existsSync } from "node:fs";
import path from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import {
  BridgeSupervisor,
  resolveFrozenBridgeExecutable,
  resolvePythonExecutable,
} from "./bridge-supervisor.mjs";
import { IPC_CHANNELS, toBridgePayload, validateIpcPayload } from "./contracts.mjs";
import {
  LocalDirectorySelectionRegistry,
  LocalDocumentSelectionRegistry,
  LocalGpxSelectionRegistry,
  ProjectOutputDirectorySelectionRegistry,
  LocalVideoSelectionRegistry,
} from "./local-selections.mjs";
import {
  CONTENT_SECURITY_POLICY,
  VITE_DEVELOPMENT_CONTENT_SECURITY_POLICY,
  EVIDENCE_SCHEME,
  RENDERER_HOST,
  RENDERER_SCHEME,
  evidenceBridgePath,
  isAllowedRendererNavigation,
  parseEvidenceUrl,
  parseWeakCaseImageUrl,
  parseDevServerUrl,
  resolveDevServerUrl,
  resolveRendererAsset,
  weakCaseImageBridgePath,
} from "./security.mjs";
import { applyProductIdentity } from "./product-identity.mjs";
import { UserActionNotificationService } from "./user-action-notifications.mjs";
import {
  providerVisualPreparationIdentities,
  sanitizeProviderVisualArtifactResponse,
  sanitizeProviderVisualPreparation,
} from "./provider-visual-evidence.mjs";
import {
  sanitizeReportDeidentifiedArtifactResponse,
  sanitizeReportDeidentifiedList,
  sanitizeReportDeidentifiedPreparation,
} from "./report-deidentified-evidence.mjs";
import {
  sanitizeUserDiscoveredReviewFrameArtifact,
  sanitizeUserDiscoveredReviewFrameMetadata,
} from "./user-discovered-review-frame.mjs";

applyProductIdentity(app);

const currentDirectory = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(currentDirectory, "..");
const bridgeRoot = app.isPackaged
  ? path.join(process.resourcesPath, "bridge")
  : path.join(desktopRoot, "bridge");
const developmentBridgeDataRoot =
  !app.isPackaged &&
  typeof process.env.TRAFFICSUPERVISION_DEV_BRIDGE_DATA_ROOT === "string" &&
  process.env.TRAFFICSUPERVISION_DEV_BRIDGE_DATA_ROOT.trim()
    ? path.resolve(
        process.env.TRAFFICSUPERVISION_DEV_BRIDGE_DATA_ROOT.trim(),
      )
    : undefined;
if (
  developmentBridgeDataRoot !== undefined &&
  !existsSync(developmentBridgeDataRoot)
) {
  throw new Error(
    "TRAFFICSUPERVISION_DEV_BRIDGE_DATA_ROOT must point to an existing directory",
  );
}
const developmentCommandProposalRunnerRoot = path.join(
  desktopRoot,
  "build",
  "command-proposal-runner",
);
const commandProposalRunnerRoot = app.isPackaged
  ? path.join(process.resourcesPath, "command-proposal-runner")
  : existsSync(developmentCommandProposalRunnerRoot)
    ? developmentCommandProposalRunnerRoot
    : undefined;
const rendererRoot = path.join(desktopRoot, "dist", "client");
const devServerUrl = resolveDevServerUrl(
  app.isPackaged,
  process.env.TRAFFICSUPERVISION_RENDERER_URL,
);
const smokeTestMode =
  !app.isPackaged && process.env.TRAFFICSUPERVISION_SMOKE_TEST === "1";
const EXPORT_TIMEOUT_MS = 30 * 60 * 1000;
const AUTO_LABELING_RUN_TIMEOUT_MS = 10 * 60 * 1000;
const PROJECT_MODEL_TRAINING_TIMEOUT_MS = 30 * 60 * 1000;
const EXACT_REPORT_APPLY_TIMEOUT_MS = 30 * 60 * 1000;
// A cold sample index verifies both multi-page HWPX documents and every
// embedded image hash before it becomes cacheable. Keep that one-time proof
// inside the automatic question flow instead of surfacing a false timeout.
const WEAK_CASE_SEARCH_TIMEOUT_MS = 2 * 60 * 1000;
const WEAK_CASE_STANDARDIZATION_TIMEOUT_MS = 2 * 60 * 1000;
const nativeWindowChrome =
  process.platform === "win32"
    ? {
        // Keep the Windows frame (snap layouts, resize shadow, and native
        // caption buttons) while allowing the product titlebar to be the
        // visible application chrome. `frame: false` deliberately is not used.
        titleBarStyle: "hidden",
        titleBarOverlay: {
          color: "#00000000",
          symbolColor: "#111827",
        },
        thickFrame: true,
      }
    : {};
let bridgeShutdownStarted = false;
let mainWindow = null;
const userActionNotifications = new UserActionNotificationService({
  NotificationCtor: Notification,
  windowResolver: () => mainWindow,
});
const videoSelections = new LocalVideoSelectionRegistry();
const gpxSelections = new LocalGpxSelectionRegistry();
const documentSelections = new LocalDocumentSelectionRegistry();
const exportDirectorySelections = new LocalDirectorySelectionRegistry();
const projectOutputDirectorySelections =
  new ProjectOutputDirectorySelectionRegistry();

protocol.registerSchemesAsPrivileged([
  {
    scheme: RENDERER_SCHEME,
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: false,
      stream: true,
    },
  },
  {
    scheme: EVIDENCE_SCHEME,
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: false,
      corsEnabled: false,
      stream: true,
    },
  },
]);

const supervisor = new BridgeSupervisor({
  bridgeRoot,
  bridgeDataRoot: developmentBridgeDataRoot,
  commandProposalRunnerRoot,
  electronVersion: process.versions.electron,
  // A first activation hashes the complete external ML runtime and performs a
  // real CUDA/VRAM import smoke before the bridge may claim it. The minimal
  // frozen bridge retains its short startup bound when no runtime is selected.
  startupTimeoutMs: process.env
    .TRAFFICSUPERVISION_EXTERNAL_MODEL_RUNTIME_PACK
    ? 5 * 60 * 1000
    : 15_000,
  pythonExecutable: app.isPackaged
    ? undefined
    : resolvePythonExecutable({
        configured: process.env.TRAFFICSUPERVISION_PYTHON,
      }),
  frozenBridgeExecutable: app.isPackaged
    ? resolveFrozenBridgeExecutable({ resourcesPath: process.resourcesPath })
    : undefined,
});

function encodeId(value) {
  return encodeURIComponent(value);
}

function registerHandler(channel, callback) {
  ipcMain.handle(channel, async (event, rawPayload) => {
    const senderUrl = event.senderFrame?.url || event.sender.getURL();
    if (
      event.senderFrame !== event.sender.mainFrame ||
      !isAllowedRendererNavigation(senderUrl, devServerUrl)
    ) {
      throw new Error("IPC sender is not trusted");
    }
    const payload = validateIpcPayload(channel, rawPayload);
    return callback(payload, event);
  });
}

function registerIpcHandlers() {
  registerHandler(
    IPC_CHANNELS.NOTIFICATION_QUESTION_REVIEW_READY,
    async (payload) =>
      userActionNotifications.notifyQuestionReviewReady(payload),
  );
  registerHandler(IPC_CHANNELS.DIALOG_VIDEO_FILES, async (_payload, event) => {
    const parentWindow = BrowserWindow.fromWebContents(event.sender);
    const options = {
      title: "분석할 도로 영상을 선택하세요",
      buttonLabel: "영상 선택",
      properties: ["openFile", "dontAddToRecent"],
      filters: [
        { name: "도로 영상", extensions: ["mp4", "mov"] },
      ],
    };
    const result = parentWindow
      ? await dialog.showOpenDialog(parentWindow, options)
      : await dialog.showOpenDialog(options);
    if (result.canceled || result.filePaths.length === 0) {
      return { selectionId: null, files: [] };
    }
    return videoSelections.register(result.filePaths);
  });
  registerHandler(IPC_CHANNELS.DIALOG_GPX_FILE, async (_payload, event) => {
    const parentWindow = BrowserWindow.fromWebContents(event.sender);
    const options = {
      title: "영상과 동기화할 GPX 경로를 선택하세요",
      buttonLabel: "GPX 연결",
      properties: ["openFile", "dontAddToRecent"],
      filters: [{ name: "GPS 교환 형식", extensions: ["gpx"] }],
    };
    const result = parentWindow
      ? await dialog.showOpenDialog(parentWindow, options)
      : await dialog.showOpenDialog(options);
    if (result.canceled || result.filePaths.length !== 1) {
      return { selectionId: null, file: null };
    }
    return gpxSelections.register(result.filePaths[0]);
  });
  registerHandler(IPC_CHANNELS.DIALOG_HWP_DOCUMENT, async (_payload, event) => {
    const parentWindow = BrowserWindow.fromWebContents(event.sender);
    const options = {
      title: "TrafficSuperVision 보고서 양식을 선택하세요",
      buttonLabel: "양식 선택",
      properties: ["openFile", "dontAddToRecent"],
      filters: [{ name: "한/글 문서", extensions: ["hwp", "hwpx"] }],
    };
    const result = parentWindow
      ? await dialog.showOpenDialog(parentWindow, options)
      : await dialog.showOpenDialog(options);
    if (result.canceled || result.filePaths.length !== 1) {
      return { selectionId: null, file: null };
    }
    return documentSelections.register(result.filePaths[0]);
  });
  registerHandler(
    IPC_CHANNELS.DIALOG_EXPORT_DIRECTORY,
    async (_payload, event) => {
      const parentWindow = BrowserWindow.fromWebContents(event.sender);
      const options = {
        title: "TrafficSuperVision 추적 패키지를 저장할 폴더를 선택하세요",
        buttonLabel: "저장 폴더 선택",
        properties: ["openDirectory", "createDirectory", "dontAddToRecent"],
      };
      const result = parentWindow
        ? await dialog.showOpenDialog(parentWindow, options)
        : await dialog.showOpenDialog(options);
      if (result.canceled || result.filePaths.length !== 1) {
        return { selectionId: null, directory: null };
      }
      return exportDirectorySelections.register(result.filePaths[0]);
    },
  );
  registerHandler(
    IPC_CHANNELS.DIALOG_PROJECT_OUTPUT_DIRECTORY,
    async (_payload, event) => {
      const parentWindow = BrowserWindow.fromWebContents(event.sender);
      const options = {
        title: "프로젝트 분석 결과를 저장할 전용 폴더를 선택하세요",
        buttonLabel: "결과 폴더 선택",
        properties: [
          "openDirectory",
          "createDirectory",
          "dontAddToRecent",
        ],
      };
      const result = parentWindow
        ? await dialog.showOpenDialog(parentWindow, options)
        : await dialog.showOpenDialog(options);
      if (result.canceled || result.filePaths.length !== 1) {
        return { selectionId: null, directory: null };
      }
      return projectOutputDirectorySelections.register(
        result.filePaths[0],
      );
    },
  );
  registerHandler(IPC_CHANNELS.HEALTH, async () => {
    if (supervisor.publicStatus().state !== "ready") return supervisor.publicStatus();
    return supervisor.request("GET", "/health");
  });
  registerHandler(IPC_CHANNELS.SOURCE_CANDIDATES_LIST, () =>
    supervisor.request("GET", "/source-candidates"),
  );
  registerHandler(IPC_CHANNELS.WEAK_CASE_SOURCES_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/reference-cases/sources`,
    ),
  );
  registerHandler(IPC_CHANNELS.WEAK_CASE_SEARCH, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/reference-cases/search`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.WEAK_CASE_SEARCH,
          payload,
        ),
        timeoutMs: WEAK_CASE_SEARCH_TIMEOUT_MS,
      },
    ),
  );
  registerHandler(IPC_CHANNELS.WEAK_CASE_STANDARDIZATION_PROPOSE, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/reference-cases/standardization-proposals`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.WEAK_CASE_STANDARDIZATION_PROPOSE,
          payload,
        ),
        timeoutMs: WEAK_CASE_STANDARDIZATION_TIMEOUT_MS,
      },
    ),
  );
  registerHandler(IPC_CHANNELS.WEAK_CASE_STANDARDIZATION_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/reference-cases/standardization-proposals`,
    ),
  );
  registerHandler(IPC_CHANNELS.WEAK_CASE_APPROVE, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/reference-cases/approvals`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.WEAK_CASE_APPROVE,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.WEAK_CASE_APPROVED_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/reference-cases/approved`,
    ),
  );
  registerHandler(IPC_CHANNELS.PROJECT_LIST, () => supervisor.request("GET", "/projects"));
  registerHandler(IPC_CHANNELS.PROJECT_CREATE, (_payload) =>
    supervisor.request("POST", "/projects", {
      body: toBridgePayload(IPC_CHANNELS.PROJECT_CREATE, _payload),
    }),
  );
  registerHandler(IPC_CHANNELS.PROJECT_GET, ({ projectId }) =>
    supervisor.request("GET", `/projects/${encodeId(projectId)}`),
  );
  registerHandler(IPC_CHANNELS.IMPORT_INSPECT, (payload) => {
    if (payload.sourceType === "local_selection") {
      videoSelections.resolvePaths(payload.localSelectionId);
    }
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/imports/inspect`,
      {
        body: toBridgePayload(IPC_CHANNELS.IMPORT_INSPECT, payload),
      },
    );
  });
  registerHandler(IPC_CHANNELS.IMPORT_CREATE, (payload) =>
    supervisor.request("POST", `/projects/${encodeId(payload.projectId)}/imports`, {
      body: toBridgePayload(IPC_CHANNELS.IMPORT_CREATE, payload),
    }),
  );
  registerHandler(IPC_CHANNELS.MEDIA_LOCAL_REGISTER, (payload) => {
    const selectedPath = videoSelections.resolveSinglePath(
      payload.localSelectionId,
    );
    const bridgePayload = toBridgePayload(
      IPC_CHANNELS.MEDIA_LOCAL_REGISTER,
      payload,
    );
    delete bridgePayload.local_selection_id;
    bridgePayload.selected_path = selectedPath;
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/media/local-file`,
      { body: bridgePayload },
    );
  });
  registerHandler(IPC_CHANNELS.MEDIA_SNAPSHOT, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/media/snapshots`,
      { body: toBridgePayload(IPC_CHANNELS.MEDIA_SNAPSHOT, payload) },
    ),
  );
  registerHandler(IPC_CHANNELS.MEDIA_STABILITY, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/media/stability`,
      { body: toBridgePayload(IPC_CHANNELS.MEDIA_STABILITY, payload) },
    ),
  );
  registerHandler(IPC_CHANNELS.MEDIA_PROBE, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/media/probe`,
      { body: toBridgePayload(IPC_CHANNELS.MEDIA_PROBE, payload) },
    ),
  );
  registerHandler(IPC_CHANNELS.MEDIA_HASH_START, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/media/hash-jobs`,
      { body: toBridgePayload(IPC_CHANNELS.MEDIA_HASH_START, payload) },
    ),
  );
  registerHandler(IPC_CHANNELS.MEDIA_HASH_GET, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/media/hash-jobs/${encodeId(payload.jobId)}`,
    ),
  );
  registerHandler(IPC_CHANNELS.MEDIA_HASH_CANCEL, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/media/hash-jobs/${encodeId(payload.jobId)}/cancel`,
      { body: {} },
    ),
  );
  registerHandler(IPC_CHANNELS.ANALYSIS_START, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/analysis/jobs`,
      { body: toBridgePayload(IPC_CHANNELS.ANALYSIS_START, payload) },
    ),
  );
  registerHandler(IPC_CHANNELS.ANALYSIS_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/analysis/jobs?limit=100`,
    ),
  );
  registerHandler(IPC_CHANNELS.ANALYSIS_GET, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/analysis/jobs/${encodeId(payload.jobId)}`,
    ),
  );
  registerHandler(IPC_CHANNELS.ANALYSIS_CANCEL, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/analysis/jobs/${encodeId(payload.jobId)}/cancel`,
      { body: {} },
    ),
  );
  registerHandler(IPC_CHANNELS.ANALYSIS_PAUSE, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/analysis/jobs/${encodeId(payload.jobId)}/pause`,
      { body: {} },
    ),
  );
  registerHandler(IPC_CHANNELS.DENSE_INDEX_START, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/dense-index/jobs`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.DENSE_INDEX_START,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.DENSE_INDEX_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/dense-index/jobs?limit=100`,
    ),
  );
  registerHandler(IPC_CHANNELS.DENSE_INDEX_GET, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/dense-index/jobs/${encodeId(payload.jobId)}`,
    ),
  );
  registerHandler(IPC_CHANNELS.DENSE_INDEX_CANCEL, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/dense-index/jobs/${encodeId(payload.jobId)}/cancel`,
      { body: {} },
    ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE, (payload) =>
    supervisor.request(
      "PUT",
      `/projects/${encodeId(payload.projectId)}/pipeline/profile`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.PIPELINE_PROFILE_CONFIGURE,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_PROFILE_GET, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/pipeline/profile`,
    ),
  );
  registerHandler(
    IPC_CHANNELS.PROJECT_OUTPUT_LOCATION_REGISTER,
    async (payload) => {
      const selectedDirectory =
        projectOutputDirectorySelections.consumePath(
          payload.selectionId,
        );
      return supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/output-locations`,
        {
          body: {
            project_id: payload.projectId,
            selected_directory: selectedDirectory,
          },
        },
      );
    },
  );
  registerHandler(
    IPC_CHANNELS.PROJECT_OUTPUT_LOCATION_LIST,
    (payload) =>
      supervisor.request(
        "GET",
        `/projects/${encodeId(payload.projectId)}/output-locations`,
      ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_START, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/pipeline/jobs`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.PIPELINE_START,
          payload,
        ),
      },
    ),
  );
  registerHandler(
    IPC_CHANNELS.PIPELINE_RERUN_CURRENT_PROFILE,
    (payload) =>
      supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.sourceJobId)}/reruns`,
        {
          body: toBridgePayload(
            IPC_CHANNELS.PIPELINE_RERUN_CURRENT_PROFILE,
            payload,
          ),
        },
      ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/pipeline/jobs?limit=128`,
    ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_FOLDER_BATCH_START, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/pipeline/folder-batches`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.PIPELINE_FOLDER_BATCH_START,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_FOLDER_BATCH_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/pipeline/folder-batches?limit=128`,
    ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_FOLDER_BATCH_GET, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/pipeline/folder-batches/${encodeId(payload.batchId)}`,
    ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_FOLDER_BATCH_RETRY, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/pipeline/folder-batches/${encodeId(payload.batchId)}/retry`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.PIPELINE_FOLDER_BATCH_RETRY,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_GET, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}`,
    ),
  );
  registerHandler(
    IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_LIST,
    async (payload) => {
      const job = await supervisor.request(
        "GET",
        `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}`,
      );
      const identities = providerVisualPreparationIdentities(job, {
        projectId: payload.projectId,
        jobId: payload.jobId,
      });
      const preparations = [];
      for (const identity of identities) {
        const raw = await supervisor.request(
          "GET",
          `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/provider-visual-evidence/${encodeId(identity.candidateId)}/${encodeId(identity.preparationId)}`,
        );
        preparations.push(
          sanitizeProviderVisualPreparation(raw, {
            projectId: payload.projectId,
            jobId: payload.jobId,
            candidateId: identity.candidateId,
            preparationId: identity.preparationId,
          }),
        );
      }
      return Object.freeze({
        preparations: Object.freeze(preparations),
      });
    },
  );
  registerHandler(
    IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_GET,
    async (payload) => {
      const raw = await supervisor.request(
        "GET",
        `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/provider-visual-evidence/${encodeId(payload.candidateId)}/${encodeId(payload.preparationId)}`,
      );
      return sanitizeProviderVisualPreparation(raw, {
        projectId: payload.projectId,
        jobId: payload.jobId,
        candidateId: payload.candidateId,
        preparationId: payload.preparationId,
      });
    },
  );
  registerHandler(
    IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_ARTIFACT,
    async (payload) => {
      const response = await supervisor.requestBinary(
        `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/provider-visual-evidence/${encodeId(payload.candidateId)}/${encodeId(payload.preparationId)}/artifacts/${payload.order}`,
        { maximumBytes: 8 * 1024 * 1024 },
      );
      return sanitizeProviderVisualArtifactResponse(response);
    },
  );
  registerHandler(
    IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_PRIVACY_REVIEW,
    async (payload) => {
      const body = toBridgePayload(
        IPC_CHANNELS.PROVIDER_VISUAL_EVIDENCE_PRIVACY_REVIEW,
        payload,
      );
      delete body.project_id;
      delete body.job_id;
      const raw = await supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/provider-visual-evidence/privacy-review`,
        { body },
      );
      if (
        !raw ||
        typeof raw !== "object" ||
        Array.isArray(raw) ||
        Object.keys(raw).length !== 3 ||
        !Object.hasOwn(raw, "preparation") ||
        !Object.hasOwn(raw, "pipeline_resume_requested") ||
        !Object.hasOwn(raw, "pipeline_job") ||
        typeof raw.pipeline_resume_requested !== "boolean"
      ) {
        throw new TypeError(
          "provider visual privacy review response is invalid",
        );
      }
      return Object.freeze({
        preparation: sanitizeProviderVisualPreparation(
          raw.preparation,
          {
            projectId: payload.projectId,
            jobId: payload.jobId,
            candidateId: payload.candidateId,
            preparationId: payload.preparationId,
          },
        ),
        pipeline_resume_requested: raw.pipeline_resume_requested,
        pipeline_job: raw.pipeline_job,
      });
    },
  );
  registerHandler(
    IPC_CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_PREPARE,
    async (payload) => {
      const raw = await supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/report-deidentified-evidence/prepare`,
        { body: {} },
      );
      return sanitizeReportDeidentifiedList(raw, {
        projectId: payload.projectId,
        jobId: payload.jobId,
      });
    },
  );
  registerHandler(
    IPC_CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_LIST,
    async (payload) => {
      const raw = await supervisor.request(
        "GET",
        `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/report-deidentified-evidence`,
      );
      return sanitizeReportDeidentifiedList(raw, {
        projectId: payload.projectId,
        jobId: payload.jobId,
      });
    },
  );
  registerHandler(
    IPC_CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_ARTIFACT,
    async (payload) => {
      const response = await supervisor.requestBinary(
        `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/report-deidentified-evidence/${encodeId(payload.incidentId)}/${encodeId(payload.derivativeId)}/artifact`,
        { maximumBytes: 16 * 1024 * 1024 },
      );
      return sanitizeReportDeidentifiedArtifactResponse(response);
    },
  );
  registerHandler(
    IPC_CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_REVIEW,
    async (payload) => {
      const body = toBridgePayload(
        IPC_CHANNELS.REPORT_DEIDENTIFIED_EVIDENCE_REVIEW,
        payload,
      );
      delete body.project_id;
      delete body.job_id;
      const raw = await supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/report-deidentified-evidence/review`,
        { body },
      );
      if (
        !raw ||
        typeof raw !== "object" ||
        Array.isArray(raw) ||
        Object.keys(raw).length !== 5 ||
        raw.schema_version !==
          "trafficvisson.report-deidentified-evidence-review-response.v1" ||
        typeof raw.all_required_derivatives_verified !== "boolean" ||
        typeof raw.pipeline_resume_requested !== "boolean" ||
        !raw.pipeline_job ||
        typeof raw.pipeline_job !== "object" ||
        Array.isArray(raw.pipeline_job)
      ) {
        throw new TypeError(
          "report deidentified review response is invalid",
        );
      }
      return Object.freeze({
        schema_version: raw.schema_version,
        preparation: sanitizeReportDeidentifiedPreparation(
          raw.preparation,
          {
            projectId: payload.projectId,
            jobId: payload.jobId,
            incidentId: payload.incidentId,
            derivativeId: payload.derivativeId,
          },
        ),
        all_required_derivatives_verified:
          raw.all_required_derivatives_verified,
        pipeline_resume_requested: raw.pipeline_resume_requested,
        pipeline_job: raw.pipeline_job,
      });
    },
  );
  registerHandler(IPC_CHANNELS.PIPELINE_CANCEL, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/cancel`,
      { body: {} },
    ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_RESUME, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/resume`,
      { body: {} },
    ),
  );
  registerHandler(IPC_CHANNELS.PIPELINE_REVIEW_COMPLETE, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/pipeline/jobs/${encodeId(payload.jobId)}/review-complete`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.PIPELINE_REVIEW_COMPLETE,
          payload,
        ),
      },
    ),
  );
  registerHandler(
    IPC_CHANNELS.USER_DISCOVERED_REVIEW_FRAME_GET,
    async (payload) => {
      const basePath =
        `/projects/${encodeId(payload.projectId)}` +
        `/pipeline-jobs/${encodeId(payload.pipelineJobId)}` +
        `/analysis/${encodeId(payload.analysisJobId)}`;
      const metadata = sanitizeUserDiscoveredReviewFrameMetadata(
        await supervisor.request(
          "GET",
          `${basePath}/user-discovered-review-frame`,
          {
            query: {
              timestamp_seconds: payload.timestampSeconds,
            },
          },
        ),
        payload,
      );
      const response = await supervisor.requestBinary(
        `${basePath}/user-discovered-review-frames/${encodeId(metadata.selected_frame_id)}`,
        { maximumBytes: 32 * 1024 * 1024 },
      );
      return sanitizeUserDiscoveredReviewFrameArtifact(
        response,
        metadata,
      );
    },
  );
  registerHandler(
    IPC_CHANNELS.USER_DISCOVERED_CANDIDATE_CREATE,
    (payload) => {
      const body = toBridgePayload(
        IPC_CHANNELS.USER_DISCOVERED_CANDIDATE_CREATE,
        payload,
      );
      delete body.project_id;
      delete body.pipeline_job_id;
      delete body.analysis_job_id;
      return supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/pipeline-jobs/${encodeId(payload.pipelineJobId)}/analysis/${encodeId(payload.analysisJobId)}/user-discovered-candidates`,
        { body },
      );
    },
  );
  registerHandler(IPC_CHANNELS.CANDIDATE_QUESTIONS_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/analysis/jobs/${encodeId(payload.jobId)}/label-questions`,
    ),
  );
  registerHandler(IPC_CHANNELS.MASK_REFINEMENT_CAPABILITY, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/analysis/jobs/${encodeId(payload.jobId)}/candidates/${encodeId(payload.candidateId)}/mask-refinement-capability`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.MASK_REFINEMENT_CAPABILITY,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.MASK_REFINEMENT_REQUEST, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/analysis/jobs/${encodeId(payload.jobId)}/candidates/${encodeId(payload.candidateId)}/mask-refinements`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.MASK_REFINEMENT_REQUEST,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.CANDIDATE_ANSWER, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/analysis/jobs/${encodeId(payload.jobId)}/candidates/${encodeId(payload.candidateId)}/answer`,
      { body: toBridgePayload(IPC_CHANNELS.CANDIDATE_ANSWER, payload) },
    ),
  );
  registerHandler(
    IPC_CHANNELS.CANDIDATE_STRUCTURED_LLM_ASSIST,
    (payload) =>
      supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/analysis/jobs/${encodeId(payload.jobId)}/candidates/${encodeId(payload.candidateId)}/structured-llm-assist`,
        {
          body: toBridgePayload(
            IPC_CHANNELS.CANDIDATE_STRUCTURED_LLM_ASSIST,
            payload,
          ),
        },
      ),
  );
  registerHandler(IPC_CHANNELS.INCIDENT_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/incidents`,
      {
        query: {
          analysis_job_id: payload.analysisJobId,
          include_inactive: payload.includeInactive,
        },
      },
    ),
  );
  registerHandler(IPC_CHANNELS.AUTO_LABEL_COMPILE, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/auto-labeling/compile`,
      { body: toBridgePayload(IPC_CHANNELS.AUTO_LABEL_COMPILE, payload) },
    ),
  );
  registerHandler(IPC_CHANNELS.AUTO_LABEL_RUN, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/auto-labeling/runs`,
      {
        body: toBridgePayload(IPC_CHANNELS.AUTO_LABEL_RUN, payload),
        timeoutMs: AUTO_LABELING_RUN_TIMEOUT_MS,
      },
    ),
  );
  registerHandler(IPC_CHANNELS.REVIEW_TEMPORAL_INCIDENTS, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/review/temporal-incident-proposals`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.REVIEW_TEMPORAL_INCIDENTS,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.REVIEW_MODEL_VALIDATION, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/review/model-validation`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.REVIEW_MODEL_VALIDATION,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.REVIEW_MODEL_PROMOTION, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/review/model-promotion`,
      {
        body: toBridgePayload(
          IPC_CHANNELS.REVIEW_MODEL_PROMOTION,
          payload,
        ),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.LABEL_STATS, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/labels/stats`,
    ),
  );
  registerHandler(IPC_CHANNELS.DATASET_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/datasets`,
    ),
  );
  registerHandler(IPC_CHANNELS.DATASET_CREATE, (payload) => {
    const bridgePayload = toBridgePayload(
      IPC_CHANNELS.DATASET_CREATE,
      payload,
    );
    delete bridgePayload.project_id;
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/datasets`,
      { body: bridgePayload },
    );
  });
  registerHandler(IPC_CHANNELS.MODEL_TRAINING_START, (payload) => {
    const bridgePayload = toBridgePayload(
      IPC_CHANNELS.MODEL_TRAINING_START,
      payload,
    );
    delete bridgePayload.project_id;
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/model-training-jobs`,
      {
        body: bridgePayload,
        timeoutMs: PROJECT_MODEL_TRAINING_TIMEOUT_MS,
      },
    );
  });
  registerHandler(IPC_CHANNELS.MODEL_TRAINING_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/model-training-jobs`,
    ),
  );
  registerHandler(IPC_CHANNELS.MODEL_FIXED_VALIDATION_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/fixed-validation-sets`,
    ),
  );
  registerHandler(IPC_CHANNELS.MODEL_TRAINING_RESUME, (payload) => {
    const bridgePayload = toBridgePayload(
      IPC_CHANNELS.MODEL_TRAINING_RESUME,
      payload,
    );
    delete bridgePayload.project_id;
    delete bridgePayload.training_job_id;
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/model-training-jobs/${encodeId(payload.trainingJobId)}/resume`,
      {
        body: bridgePayload,
        timeoutMs: PROJECT_MODEL_TRAINING_TIMEOUT_MS,
      },
    );
  });
  registerHandler(IPC_CHANNELS.MODEL_TRAINING_CANCEL, (payload) => {
    const bridgePayload = toBridgePayload(
      IPC_CHANNELS.MODEL_TRAINING_CANCEL,
      payload,
    );
    delete bridgePayload.project_id;
    delete bridgePayload.training_job_id;
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/model-training-jobs/${encodeId(payload.trainingJobId)}/cancel`,
      {
        body: bridgePayload,
        timeoutMs: PROJECT_MODEL_TRAINING_TIMEOUT_MS,
      },
    );
  });
  registerHandler(IPC_CHANNELS.MODEL_VERSIONS_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/model-versions`,
    ),
  );
  registerHandler(IPC_CHANNELS.MODEL_ITERATIONS_LIST, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/model-iterations`,
    ),
  );
  registerHandler(IPC_CHANNELS.MODEL_STATE_GET, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/model-state`,
    ),
  );
  for (const [channel, action] of [
    [IPC_CHANNELS.MODEL_PROMOTE, "promote"],
    [IPC_CHANNELS.MODEL_REJECT, "reject"],
  ]) {
    registerHandler(channel, (payload) => {
      const bridgePayload = toBridgePayload(channel, payload);
      delete bridgePayload.project_id;
      delete bridgePayload.model_version_id;
      return supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/model-versions/${encodeId(payload.modelVersionId)}/${action}`,
        { body: bridgePayload },
      );
    });
  }
  registerHandler(IPC_CHANNELS.MODEL_ROLLBACK, (payload) => {
    const bridgePayload = toBridgePayload(
      IPC_CHANNELS.MODEL_ROLLBACK,
      payload,
    );
    delete bridgePayload.project_id;
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/model-state/rollback`,
      { body: bridgePayload },
    );
  });
  registerHandler(IPC_CHANNELS.QUESTIONS_LIST, (payload) =>
    supervisor.request("GET", `/projects/${encodeId(payload.projectId)}/questions`, {
      query: {
        status: payload.status,
        policy_mode: payload.policyMode,
      },
    }),
  );
  registerHandler(IPC_CHANNELS.QUESTION_ANSWER, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/questions/${encodeId(payload.questionId)}/answer`,
      { body: toBridgePayload(IPC_CHANNELS.QUESTION_ANSWER, payload) },
    ),
  );
  registerHandler(IPC_CHANNELS.HISTORY_GET, (payload) =>
    supervisor.request("GET", `/projects/${encodeId(payload.projectId)}/history`, {
      query: { trip_id: payload.tripId },
    }),
  );
  registerHandler(IPC_CHANNELS.GPX_TRACK_IMPORT, (payload) => {
    const selectedPath = gpxSelections.resolvePath(payload.gpxSelectionId);
    const bridgePayload = toBridgePayload(
      IPC_CHANNELS.GPX_TRACK_IMPORT,
      payload,
    );
    delete bridgePayload.gpx_selection_id;
    delete bridgePayload.project_id;
    delete bridgePayload.trip_id;
    bridgePayload.selected_path = selectedPath;
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/trips/${encodeId(payload.tripId)}/gpx`,
      { body: bridgePayload },
    );
  });
  registerHandler(IPC_CHANNELS.GPX_TRACK_GET, (payload) =>
    supervisor.request(
      "GET",
      `/projects/${encodeId(payload.projectId)}/trips/${encodeId(payload.tripId)}/gpx`,
    ),
  );
  registerHandler(IPC_CHANNELS.GPX_TRACK_OFFSET, (payload) => {
    const bridgePayload = toBridgePayload(
      IPC_CHANNELS.GPX_TRACK_OFFSET,
      payload,
    );
    delete bridgePayload.project_id;
    delete bridgePayload.trip_id;
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/trips/${encodeId(payload.tripId)}/gpx/offset`,
      { body: bridgePayload },
    );
  });
  registerHandler(IPC_CHANNELS.RUNTIME_READINESS, () =>
    supervisor.request("GET", "/runtime/readiness"),
  );
  registerHandler(IPC_CHANNELS.PROVIDERS_LIST, () => supervisor.request("GET", "/providers"));
  registerHandler(IPC_CHANNELS.PROVIDER_LOGIN, (payload) =>
    supervisor.request("POST", `/providers/${encodeId(payload.provider)}/login`, {
      body: {},
    }),
  );
  registerHandler(IPC_CHANNELS.REPORT_GENERATE, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/reports/generate`,
      {
        body: toBridgePayload(IPC_CHANNELS.REPORT_GENERATE, payload),
      },
    ),
  );
  registerHandler(IPC_CHANNELS.REPORT_DRAFT, (payload) =>
    supervisor.request("POST", `/projects/${encodeId(payload.projectId)}/reports/draft`, {
      body: toBridgePayload(IPC_CHANNELS.REPORT_DRAFT, payload),
    }),
  );
  registerHandler(IPC_CHANNELS.REPORT_PLAN, (payload) =>
    supervisor.request("POST", `/projects/${encodeId(payload.projectId)}/reports/plan`, {
      body: toBridgePayload(IPC_CHANNELS.REPORT_PLAN, payload),
    }),
  );
  registerHandler(IPC_CHANNELS.REPORT_APPLY, (payload) =>
    supervisor.request("POST", `/projects/${encodeId(payload.projectId)}/reports/apply`, {
      body: toBridgePayload(IPC_CHANNELS.REPORT_APPLY, payload),
    }),
  );
  registerHandler(IPC_CHANNELS.REPORT_ACCEPT, (payload) =>
    supervisor.request("POST", `/projects/${encodeId(payload.projectId)}/reports/accept`, {
      body: toBridgePayload(IPC_CHANNELS.REPORT_ACCEPT, payload),
    }),
  );
  registerHandler(IPC_CHANNELS.REPORT_REJECT, (payload) =>
    supervisor.request("POST", `/projects/${encodeId(payload.projectId)}/reports/reject`, {
      body: toBridgePayload(IPC_CHANNELS.REPORT_REJECT, payload),
    }),
  );
  registerHandler(IPC_CHANNELS.REPORT_EXACT_PREVIEW, (payload) => {
    const body = toBridgePayload(
      IPC_CHANNELS.REPORT_EXACT_PREVIEW,
      payload,
    );
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/pipeline-jobs/${encodeId(
        payload.pipelineJobId,
      )}/reports/exact-preview`,
      { body },
    );
  });
  registerHandler(IPC_CHANNELS.REPORT_EXACT_APPLY, (payload) => {
    const body = toBridgePayload(
      IPC_CHANNELS.REPORT_EXACT_APPLY,
      payload,
    );
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/pipeline-jobs/${encodeId(
        payload.pipelineJobId,
      )}/reports/exact-apply`,
      {
        body,
        timeoutMs: EXACT_REPORT_APPLY_TIMEOUT_MS,
      },
    );
  });
  for (const [channel, action] of [
    [IPC_CHANNELS.REPORT_EXACT_ACCEPT, "exact-accept"],
    [IPC_CHANNELS.REPORT_EXACT_REJECT, "exact-reject"],
    [IPC_CHANNELS.REPORT_EXACT_ROLLBACK, "exact-rollback"],
  ]) {
    registerHandler(channel, (payload) => {
      const body = toBridgePayload(channel, payload);
      return supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/pipeline-jobs/${encodeId(
          payload.pipelineJobId,
        )}/reports/${action}`,
        { body },
      );
    });
  }
  registerHandler(IPC_CHANNELS.HWP_PREFLIGHT, () =>
    supervisor.request("GET", "/hwp/preflight"),
  );
  for (const [channel, action] of [
    [IPC_CHANNELS.HWP_PREVIEW, "preview"],
    [IPC_CHANNELS.HWP_APPLY, "apply"],
  ]) {
    registerHandler(channel, (payload) => {
      const templatePath = documentSelections.resolvePath(
        payload.documentSelectionId,
      );
      const bridgePayload = toBridgePayload(channel, payload);
      delete bridgePayload.document_selection_id;
      bridgePayload.template_path = templatePath;
      return supervisor.request(
        "POST",
        `/projects/${encodeId(payload.projectId)}/hwp/${action}`,
        { body: bridgePayload },
      );
    });
  }
  registerHandler(IPC_CHANNELS.HWP_ROLLBACK, (payload) =>
    supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/hwp/rollback`,
      { body: toBridgePayload(IPC_CHANNELS.HWP_ROLLBACK, payload) },
    ),
  );
  registerHandler(IPC_CHANNELS.EXPORT_CREATE, (payload) => {
    const bridgePayload = toBridgePayload(
      IPC_CHANNELS.EXPORT_CREATE,
      payload,
    );
    if (payload.destinationSelectionId) {
      bridgePayload.destination_path = exportDirectorySelections.consumePath(
        payload.destinationSelectionId,
      );
      delete bridgePayload.destination_selection_id;
    }
    return supervisor.request(
      "POST",
      `/projects/${encodeId(payload.projectId)}/exports`,
      {
        body: bridgePayload,
        timeoutMs: EXPORT_TIMEOUT_MS,
      },
    );
  });
}

async function registerRendererProtocol() {
  protocol.handle(RENDERER_SCHEME, async (request) => {
    try {
      let assetPath = resolveRendererAsset(rendererRoot, request.url);
      if (!path.extname(assetPath)) assetPath = path.join(rendererRoot, "index.html");
      const response = await net.fetch(pathToFileURL(assetPath).toString());
      const headers = new Headers(response.headers);
      headers.set("Content-Security-Policy", CONTENT_SECURITY_POLICY);
      headers.set("Cache-Control", assetPath.endsWith("index.html") ? "no-store" : "public, max-age=31536000, immutable");
      headers.set("X-Content-Type-Options", "nosniff");
      headers.set("Referrer-Policy", "no-referrer");
      return new Response(response.body, {
        status: response.status,
        statusText: response.statusText,
        headers,
      });
    } catch {
      return new Response("Not found", {
        status: 404,
        headers: {
          "Content-Type": "text/plain; charset=utf-8",
          "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        },
      });
    }
  });
  protocol.handle(EVIDENCE_SCHEME, async (request) => {
    try {
      if (request.method !== "GET") {
        return new Response("Method not allowed", {
          status: 405,
          headers: { Allow: "GET" },
        });
      }
      let bridgePath;
      try {
        const identity = parseEvidenceUrl(request.url);
        bridgePath = evidenceBridgePath(identity);
      } catch {
        const identity = parseWeakCaseImageUrl(request.url);
        bridgePath = weakCaseImageBridgePath(identity);
      }
      const bridgeResponse = await supervisor.requestBinary(
        bridgePath,
        { range: request.headers.get("range") ?? undefined },
      );
      const headers = new Headers(bridgeResponse.headers);
      headers.set("Cache-Control", "no-store");
      headers.set("Cross-Origin-Resource-Policy", "cross-origin");
      headers.set("Referrer-Policy", "no-referrer");
      headers.set("X-Content-Type-Options", "nosniff");
      return new Response(bridgeResponse.body, {
        status: bridgeResponse.status,
        statusText: bridgeResponse.statusText,
        headers,
      });
    } catch (error) {
      const status = [404, 409, 410, 416].includes(error?.status)
        ? error.status
        : 503;
      console.error(
        `[evidence] ${error?.code ?? error?.name ?? "UNKNOWN"} status=${error?.status ?? 0}`,
      );
      return new Response("Evidence unavailable", {
        status,
        headers: {
          "Cache-Control": "no-store",
          "Content-Type": "text/plain; charset=utf-8",
          "Cross-Origin-Resource-Policy": "cross-origin",
          "Referrer-Policy": "no-referrer",
          "X-Content-Type-Options": "nosniff",
        },
      });
    }
  });
}

function installSessionSecurity() {
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });
  session.defaultSession.setPermissionCheckHandler(() => false);
  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    if (!devServerUrl) return callback({ responseHeaders: details.responseHeaders });
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [
          VITE_DEVELOPMENT_CONTENT_SECURITY_POLICY,
        ],
        "X-Content-Type-Options": ["nosniff"],
        "Referrer-Policy": ["no-referrer"],
      },
    });
  });
}

async function createMainWindow() {
  const window = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 1280,
    minHeight: 720,
    show: false,
    backgroundColor: "#e8edf5",
    ...(app.isPackaged
      ? {}
      : {
          icon: path.join(
            currentDirectory,
            "..",
            "build",
            "icon.ico",
          ),
        }),
    autoHideMenuBar: process.platform !== "darwin",
    ...nativeWindowChrome,
    webPreferences: {
      preload: path.join(currentDirectory, "preload.cjs"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
      allowRunningInsecureContent: false,
      navigateOnDragDrop: false,
      spellcheck: false,
      devTools: !app.isPackaged,
    },
  });

  if (process.platform !== "darwin") {
    // Prevent Electron's development-style File/Edit/View menu from making
    // the installed product look like a web page inside a second window.
    window.removeMenu();
  }
  if (!app.isPackaged) {
    window.webContents.on(
      "console-message",
      (_event, levelOrDetails, legacyMessage, legacyLine, legacySource) => {
        const isStructured =
          levelOrDetails !== null && typeof levelOrDetails === "object";
        const level = isStructured ? levelOrDetails.level : levelOrDetails;
        const message = isStructured
          ? levelOrDetails.message
          : legacyMessage ?? String(levelOrDetails);
        const line = isStructured
          ? levelOrDetails.lineNumber
          : legacyLine;
        const source = isStructured
          ? levelOrDetails.sourceId
          : legacySource;
        console.log(
          `[renderer:${level ?? "info"}] ${message}` +
            `${source ? ` (${source}:${line ?? 0})` : ""}`,
        );
      },
    );
    window.webContents.on(
      "did-fail-load",
      (_event, code, description, validatedUrl, isMainFrame) => {
        if (isMainFrame) {
          console.error(
            `[renderer:load] ${code} ${description} ${validatedUrl}`,
          );
        }
      },
    );
    window.webContents.on("preload-error", (_event, preloadPath, error) => {
      console.error(
        `[renderer:preload] ${preloadPath}: ${error?.message ?? error}`,
      );
    });
    window.webContents.on("render-process-gone", (_event, details) => {
      console.error(
        `[renderer:gone] ${details?.reason ?? "unknown"} exit=${details?.exitCode ?? "unknown"}`,
      );
    });
  }
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());
  window.webContents.on("will-navigate", (event, url) => {
    if (!isAllowedRendererNavigation(url, devServerUrl)) event.preventDefault();
  });
  window.webContents.on("will-redirect", (event, url) => {
    if (!isAllowedRendererNavigation(url, devServerUrl)) event.preventDefault();
  });

  // Register before navigation. `ready-to-show` can fire while `loadURL`
  // resolves, which previously left the window alive but permanently hidden.
  if (!smokeTestMode) {
    window.once("ready-to-show", () => window.show());
  }

  if (devServerUrl) {
    const parsed = parseDevServerUrl(devServerUrl);
    await window.loadURL(parsed.toString());
  } else {
    await window.loadURL(`${RENDERER_SCHEME}://${RENDERER_HOST}/index.html`);
  }
  // Some Windows GPU/overlay combinations do not emit `ready-to-show`.
  // Navigation has completed here, so this fallback guarantees a usable app.
  if (!smokeTestMode && !window.isVisible()) {
    window.show();
  }
  return window;
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app
    .whenReady()
    .then(async () => {
      // macOS retains its conventional system application menu. TrafficSuperVision
      // ships for Windows, where the generic Electron menu is not product UI.
      if (process.platform !== "darwin") {
        Menu.setApplicationMenu(null);
      }
      await registerRendererProtocol();
      installSessionSecurity();
      registerIpcHandlers();
      try {
        await supervisor.start();
      } catch (error) {
        console.error(
          "[bridge] startup failed:",
          error instanceof Error ? error.message : "unknown",
        );
      }
      mainWindow = await createMainWindow();
      mainWindow.on("focus", () => {
        mainWindow?.flashFrame(false);
      });
      mainWindow.once("closed", () => {
        mainWindow = null;
      });
      if (smokeTestMode) {
        setTimeout(() => app.quit(), 500);
      }
    })
    .catch((error) => {
      console.error(
        "[electron] startup failed:",
        error instanceof Error ? error.message : "unknown",
      );
      app.exit(1);
    });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  app.on("before-quit", (event) => {
    if (bridgeShutdownStarted) return;
    bridgeShutdownStarted = true;
    videoSelections.clear();
    documentSelections.clear();
    exportDirectorySelections.clear();
    event.preventDefault();
    void supervisor.stop().finally(() => app.quit());
  });
}
