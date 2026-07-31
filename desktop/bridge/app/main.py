from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Union
from uuid import UUID
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .contracts import (
    AnalysisJobListResponse,
    AnalysisJobResponse,
    AnalysisJobStartRequest,
    ApprovedReferenceListResponse,
    ApprovedReportIncident,
    AutoLabelCompileRequest,
    AutoLabelCompileResponse,
    AutoLabelRunRequest,
    AutoLabelRunResponse,
    CandidateLabelAnswerRequest,
    CandidateLabelAnswerResponse,
    CandidateLabelQuestionList,
    CandidateStructuredLlmAssistRequest,
    CurrentReportApplicationResponse,
    CurrentReportDecisionRequest,
    CurrentReportExactApplyRequest,
    CurrentReportExactPreviewRequest,
    CurrentReportExactPreviewResponse,
    CurrentReportPublicationResponse,
    DatasetVersionCreateRequest,
    DatasetVersionListResponse,
    DatasetVersionResponse,
    DenseIndexJobListResponse,
    DenseIndexJobResponse,
    DenseIndexJobStartRequest,
    EmptyRequest,
    ExportCreateRequest,
    ExportCreateResponse,
    GpxTrackImportRequest,
    GpxTrackOffsetRequest,
    GpxTrackResponse,
    HealthResponse,
    HistoryResponse,
    HwpApplyDocumentRequest,
    HwpApplyResponse,
    HwpDocumentRequest,
    HwpPreflightResponse,
    HwpPreviewResponse,
    HwpRollbackRequest,
    HwpRollbackResponse,
    HwpSampleLayoutPreviewResponse,
    HwpSampleTargetStructurePreviewResponse,
    ImportCreateRequest,
    ImportCreateResponse,
    ImportInspectRequest,
    ImportInspectResponse,
    MediaHashJobResponse,
    MediaHashStartRequest,
    MediaLocalFileRegisterRequest,
    MediaProbeRequest,
    MediaProbeResponse,
    MediaSnapshotRequest,
    MediaSnapshotResponse,
    MediaStabilityRequest,
    MediaStabilityResponse,
    MaskRefinementCapabilityResponse,
    MaskRefinementIdentityRequest,
    MaskRefinementRequest,
    MaskRefinementResponse,
    ModelPromotionReviewRequest,
    ModelPromotionReviewResponse,
    ModelValidationReviewRequest,
    ModelValidationReviewResponse,
    PolicyMode,
    PipelineJobListResponse,
    PipelineJobRerunRequest,
    PipelineJobResponse,
    PipelineJobStartRequest,
    PipelineJobStartResponse,
    PipelineProfileConfigureRequest,
    PipelineProfileResponse,
    PipelineReviewCompleteRequest,
    ProviderVisualEvidencePrivacyReviewRequest,
    ReportDeidentifiedEvidenceReviewRequest,
    ProjectCreateRequest,
    ProjectIncidentListResponse,
    ProjectListResponse,
    ProjectLabelStatsResponse,
    ProjectFixedValidationVersionListResponse,
    ProjectModelStateResponse,
    ProjectModelTrainingJobListResponse,
    ProjectModelTrainingJobResponse,
    ProjectModelTrainingCancelRequest,
    ProjectModelTrainingResumeRequest,
    ProjectModelTrainingStartRequest,
    ProjectModelTransitionRequest,
    ProjectModelTransitionResponse,
    ProjectModelVersionListResponse,
    ProjectModelIterationLedgerResponse,
    ProjectResponse,
    ProviderListResponse,
    ProviderLoginResponse,
    RuntimeReadinessResponse,
    QuestionAnswerRequest,
    QuestionAnswerResponse,
    QuestionListResponse,
    QuestionStatus,
    ReportApplyRequest,
    ReportApplyResponse,
    ReportDecisionRequest,
    ReportDecisionResponse,
    ReportDraftRequest,
    ReportDraftResponse,
    ReportGenerateRequest,
    ReportGenerateResponse,
    ReportPlanRequest,
    ReportPlanResponse,
    SourceCandidateCatalogResponse,
    TemporalIncidentReviewRequest,
    TemporalIncidentReviewResponse,
    UserDiscoveredCandidateCreateRequest,
    UserDiscoveredCandidateCreateResponse,
    UserDiscoveredReviewFrameResponse,
    WeakCaseSearchRequest,
    WeakCaseSearchResponse,
    WeakCaseApprovalRequest,
    WeakCaseApprovalResponse,
    WeakCaseStandardizationProposalListResponse,
    WeakCaseStandardizationProposalRequest,
    WeakCaseStandardizationProposalResponse,
    WeakCaseSourceListResponse,
)
from .analysis_jobs import (
    ANALYSIS_ARTIFACT_CATEGORY,
    AnalysisJobService,
    ResolvedAnalysisArtifactRoot,
)
from .auto_labeling import (
    AutoLabelingError,
    HIGH_CONFIDENCE_AUTO_ACCEPT,
    PROPOSAL_ONLY,
    compile_labeling_command,
    create_provisional_auto_label_decision,
)
from .auto_label_runs import AutoLabelRunError, AutoLabelRunStore
from .artifact_export import (
    ArtifactExportError,
    EvidenceExportSource,
    MAX_EVIDENCE_FILES,
    MAX_EVIDENCE_INCIDENTS,
    MAX_EVIDENCE_TOTAL_BYTES,
    export_artifacts,
)
from .candidate_extraction import ExtractionLimits
from .candidate_labels import (
    CandidateLabelConflict,
    CandidateLabelCorrupt,
    CandidateLabelError,
    CandidateLabelStore,
    CandidateResolutionError,
)
from .candidate_evidence_overlay import (
    CandidateEvidenceOverlayStore,
    ResolvedCandidateEvidenceBinding,
    ResolvedDenseCandidateFrame,
)
from .candidate_evidence_registry import CandidateEvidenceRegistry
from .model_iteration_qa import (
    ModelIterationQaError,
    ModelIterationQaStore,
)
from .candidate_window_rescan import (
    CandidateWindowRescanError,
    load_candidate_window_rescan,
    resolve_candidate_window_frame,
)
from .current_incident_hwpx_plan import ServerOwnedEvidenceFile
from .current_report_approval import (
    CurrentReportApprovalError,
    CurrentReportApprovalRegistry,
)
from .current_report_application import (
    CurrentReportApplicationError,
    CurrentReportApplicationService,
)
from .current_report_publication import (
    CurrentReportPublicationError,
    CurrentReportPublicationService,
)
from .current_report_draft import (
    CurrentReportDraftError,
    CurrentReportDraftService,
)
from .composite_candidate_evidence import (
    CandidateEvidenceOverlayAdapter,
)
from .command_proposal_generation import (
    CommandProposalGenerationError,
    CommandProposalGenerator,
    FixedManifestCommandProposalGenerator,
    UnavailableCommandProposalGenerator,
)
from .approved_reference_annotation_lineage import (
    ApprovedReferenceAnnotationLineageError,
    build_approved_reference_annotation_lineage,
)
from .dataset_versions import DatasetVersionError, LabelProjectionError
from .structured_llm_labels import (
    StructuredLlmTrainingLabelError,
    structured_llm_training_answer,
)
from .structured_assist_provenance import (
    SCHEMA_VERSION as STRUCTURED_ASSIST_PROVENANCE_SCHEMA,
    StructuredAssistProvenanceError,
    per_item_user_grant_authority,
    seal_structured_assist_provenance,
)
from .training_geometry import (
    TrainingGeometryError,
    training_geometry_from_review,
)
from .pipeline_llm_training_delegation import (
    PipelineStructuredLabelDelegationService,
)
from .dense_index_jobs import (
    DenseIndexJobConflict,
    DenseIndexJobNotFound,
    DenseIndexJobService,
    DenseIndexJobSourceRejected,
    DenseIndexJobUnavailable,
    ResolvedDenseIndexSource,
)
from .dense_candidate_discovery import (
    ActiveProjectModelDenseProposalProvider,
    ApprovedReferenceDenseProposalProvider,
    DenseCandidateDiscoveryEngine,
    LocalVisionDenseProposalProvider,
)
from .local_object_detector_inference import (
    PromotedLocalObjectDetectorInference,
)
from .local_object_detector_trainer import (
    LocalCudaObjectDetectorTrainer,
    ResolvedDetectorArtifact,
    ResolvedDerivedDetectorArtifact,
)
from .external_dfine_training_backend import (
    ExternalDfineCudaDetectorBackend,
)
from .derived_detector_geometry import DerivedDetectorGeometrySample
from .dense_candidate_frame_registry import (
    DenseCandidateFrameRegistry,
)
from .derived_outputs import DerivedOutputsService
from .experimental_proposals import (
    ExperimentalProposalError,
    ExperimentalProposalStore,
)
from .review_event_set import (
    ResolvedReviewCandidateEvidence,
    ResolvedReviewCandidateEvidenceSet,
    ReviewEventSetIntegrityError,
    ReviewEventSetNotFound,
    ReviewEventSetStore,
)
from .hwp_com import (
    HwpAutomationError,
    HwpComService,
    PLAN_VERSION,
    registry_preflight,
)
from .hwp_approval import (
    HwpApprovalError,
    HwpApprovalRegistry,
    HwpRollbackScopeRegistry,
    HwpSampleApprovalRegistry,
)
from .hwpx_sample_layout import (
    HwpxSampleLayoutError,
    preview_sample_layout_plan,
)
from .hwpx_structure import HwpxStructureError
from .hwp_template_guard import (
    HwpTemplateGuardError,
    classify_hwp_template,
)
from .gpx_tracks import (
    GPX_EXPORT_PROVENANCE_SCHEMA,
    GpxTrackError,
    GpxTrackStore,
)
from .media_intake import MediaIntakeService
from .dense_frame_index import (
    DenseFrameIndexLimits,
    DenseFrameIndexStagingUnavailable,
    dense_staging_root_is_short,
    initialize_dense_staging_root,
)
from .stable_media_snapshots import StableMediaSnapshotStore
from .folder_pipeline_batches import (
    FolderBatchConflict,
    FolderBatchNotFound,
    FolderBatchRetryRequest,
    FolderBatchStartRequest,
    FolderBatchUnavailable,
    FolderPipelineBatchService,
)
from .pipeline_jobs import (
    BridgePipelineRuntime,
    PipelineJobConflict,
    PipelineJobNotFound,
    PipelineJobService,
    PipelineJobUnavailable,
    PipelineProfileMissing,
    PipelineRuntime,
)
from .pipeline_report_preview import (
    DurablePipelineReportPreviewPreparer,
    PipelineReportPreviewError,
    SampleTemplateSemanticMapRegistry,
)
from .provider_visual_evidence import (
    ProviderVisualEvidenceError,
    ProviderVisualEvidencePreparationService,
)
from .report_deidentified_evidence import (
    ReportDeidentifiedEvidenceError,
    ReportDeidentifiedEvidenceService,
    ReportEvidenceSource,
)
from .provider_status import ProviderStatusService
from .project_model_lifecycle import (
    PROJECT_MODEL_ARTIFACT_CATEGORY,
    ProjectModelLifecycleError,
    ProjectModelLifecycleStore,
    ResolvedModelArtifactRoot,
)
from .project_artifact_roots import (
    ProjectArtifactRootConflict,
    ProjectArtifactRootNotFound,
    ProjectArtifactRootRegistry,
    ProjectArtifactRootUnavailable,
    ProjectOutputLocationRegisterRequest,
)
from .report_generation import (
    InvalidModelOutput,
    InvalidRedactedPayload,
    ProviderAuthenticationExpiredError,
    ProviderNetworkError,
    ProviderNotReadyError,
    ReportGenerationError,
    ReportGenerationService,
)
from .structured_vision_judgment import (
    StructuredVisionJudgmentProvider,
    StructuredVisionJudgmentService,
)
from .external_model_runtime import external_model_runtime_status
from .runtime_readiness import RuntimeReadinessService
from .sam2_refinement import (
    Sam2MaskRefinementService,
    Sam2RefinementAdapter,
    Sam2RefinementError,
    Sam2RefinementNotFound,
)
from .sam2_transformers_adapter import (
    TransformersSam2Adapter,
    TransformersSam2AdapterError,
    default_program_data_pack_path as default_sam2_model_pack_path,
)
from .vision_inference import LocalVisionInferenceService
from .security import (
    BodyLimitMiddleware,
    BridgeTokenAuth,
    LoopbackOnlyMiddleware,
    SecurityHeadersMiddleware,
)
from .source_candidate_probe import SourceCandidateProbe
from .source_registry import build_source_candidate_catalog
from .store import TrafficSuperVisionStore
from .temporal_incidents import (
    TemporalGroupingPolicy,
    TemporalIncidentProposalError,
    group_temporal_incident_proposals,
)
from .validation_metrics import (
    ValidationMetricsError,
    compare_candidate_to_active,
    evaluate_model,
)
from .weak_case_catalog import (
    WeakCaseCatalog,
    WeakCaseCatalogError,
    default_sample_root,
)
from .weak_case_approvals import (
    WeakCaseApprovalError,
    WeakCaseApprovalStore,
)
from .weak_case_standardization import (
    OfficialCliWeakCaseStandardizationProvider,
    WeakCaseStandardizationError,
    WeakCaseStandardizationProposalStore,
)
from .weak_case_visual_index import (
    VerifiedLocalClipImageEmbedder,
    WeakCaseVisualIndexError,
    WeakCaseVisualIndexService,
)
from .visual_evidence_quality import LocalRoadSceneObserver
from .user_discovered_candidate import (
    ResolvedIndependentSurvey,
    UserDiscoveredCandidateError,
    UserDiscoveredCandidateRequest,
    UserDiscoveredCandidateService,
    resolve_user_discovered_review_frame,
    resolve_user_discovered_review_frame_artifact,
)
from .user_discovered_review_queue import (
    UserDiscoveredReviewQueueError,
    UserDiscoveredReviewQueueService,
)
from .visual_centroid_trainer import (
    VisualEmbeddingCentroidHeadInference,
    VisualEmbeddingCentroidHeadTrainer,
)


def _canonical_export_provenance_hash(value) -> str:
    """Hash already validated, path-free export provenance deterministically."""

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _report_location_summary(
    track: object,
    representative_time_ms: int,
) -> Optional[str]:
    """Return a bounded server-derived GPS summary for one video timestamp."""

    if not isinstance(track, dict):
        return None
    raw_points = track.get("route_points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        return None
    target_seconds = representative_time_ms / 1_000
    points = []
    for raw in raw_points:
        if not isinstance(raw, dict):
            return None
        video_time = raw.get("video_time_seconds")
        latitude = raw.get("latitude")
        longitude = raw.get("longitude")
        if (
            isinstance(video_time, bool)
            or not isinstance(video_time, (int, float))
            or isinstance(latitude, bool)
            or not isinstance(latitude, (int, float))
            or isinstance(longitude, bool)
            or not isinstance(longitude, (int, float))
            or not math.isfinite(float(video_time))
            or not math.isfinite(float(latitude))
            or not math.isfinite(float(longitude))
            or not -90 <= float(latitude) <= 90
            or not -180 <= float(longitude) <= 180
            or not isinstance(raw.get("gap_from_previous"), bool)
        ):
            return None
        points.append(
            (
                float(video_time),
                float(latitude),
                float(longitude),
                raw["gap_from_previous"],
            )
        )
    if any(
        points[index][0] <= points[index - 1][0]
        for index in range(1, len(points))
    ):
        return None
    if target_seconds < points[0][0] or target_seconds > points[-1][0]:
        return None
    lower = 0
    upper = len(points) - 1
    while lower <= upper:
        middle = (lower + upper) // 2
        point = points[middle]
        if point[0] == target_seconds:
            latitude, longitude = point[1], point[2]
            break
        if point[0] < target_seconds:
            lower = middle + 1
        else:
            upper = middle - 1
    else:
        after = points[min(len(points) - 1, lower)]
        before = points[max(0, lower - 1)]
        if (
            after == before
            or after[3]
            or after[0] <= before[0]
        ):
            return None
        ratio = (target_seconds - before[0]) / (after[0] - before[0])
        latitude = before[1] + (after[1] - before[1]) * ratio
        longitude = before[2] + (after[2] - before[2]) * ratio
    total_seconds = max(0, target_seconds)
    hours = int(total_seconds // 3_600)
    minutes = int((total_seconds % 3_600) // 60)
    seconds = total_seconds % 60
    return (
        f"GPS {latitude:.6f}, {longitude:.6f} · "
        f"영상 {hours:02d}:{minutes:02d}:{seconds:06.3f}"
    )


def _dense_index_job_response(
    row: dict[str, object],
) -> DenseIndexJobResponse:
    """Project a path-free service record into the public strict contract."""

    result = row.get("result")
    result_summary = None
    if isinstance(result, dict):
        index = result.get("index")
        if not isinstance(index, dict):
            raise HTTPException(
                status_code=500,
                detail="The dense-index result contract is invalid",
            )
        result_summary = {
            "artifact_set_id": result.get("artifact_set_id"),
            "frame_count": result.get("frame_count"),
            "total_frame_bytes": result.get("total_frame_bytes"),
            "index_artifact_ref": index.get("artifact_ref"),
        }
    try:
        analysis_job_id = UUID(str(row["opaque_source_identity"]))
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=500,
            detail="The dense-index source identity is invalid",
        ) from error
    return DenseIndexJobResponse(
        job_id=row.get("job_id"),
        project_id=row.get("project_id"),
        analysis_job_id=analysis_job_id,
        source_sha256=row.get("source_sha256"),
        sampling_fps=row.get("sampling_fps"),
        status=row.get("status"),
        stage=row.get("stage"),
        progress_percent=row.get("progress_percent"),
        progress_state=row.get("progress_state"),
        progress_basis=row.get("progress_basis"),
        pause_supported=row.get("pause_supported"),
        error_code=row.get("error_code"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        record_sha256=row.get("record_sha256"),
        safety=row.get("safety"),
        result=result_summary,
    )


async def _run_export_with_disconnect_cancellation(
    http_request: Request,
    export_root: Path,
    payload,
    evidence_sources,
):
    """Run blocking export work while propagating HTTP disconnect cancellation."""

    cancelled = threading.Event()
    task = asyncio.create_task(
        run_in_threadpool(
            export_artifacts,
            export_root,
            payload,
            evidence_sources=evidence_sources,
            cancelled=cancelled.is_set,
        )
    )
    try:
        while not task.done():
            if await http_request.is_disconnected():
                cancelled.set()
            await asyncio.wait({task}, timeout=0.1)
        return await task
    except asyncio.CancelledError:
        cancelled.set()
        try:
            await asyncio.shield(task)
        except (ArtifactExportError, asyncio.CancelledError):
            pass
        raise


_AUTO_LABEL_DISPLAY_NAMES = {
    "pothole": "포트홀",
    "road_surface_damage": "노면 파손",
    "road_debris": "낙하물",
    "median_barrier_damage": "중앙분리대 파손",
    "sign_damage": "표지판 파손",
    "sign_vegetation_occlusion": "표지판 식생 가림",
    "roadside_mowing_required": "삭초 필요",
    "lane_marking_faded": "차선 재도색 필요",
    "colored_guidance_faded": "색깔유도선 재도색 필요",
    "guide_sign_identity": "이정표 식별",
    "tunnel_identity": "터널명 식별",
    "tunnel_name_occlusion": "터널명 식생 가림",
    "other": "사용자 지정 도로 문제",
}
_MAINTENANCE_JUDGMENT_TYPES = {
    "road_surface_damage",
    "median_barrier_damage",
    "sign_damage",
    "sign_vegetation_occlusion",
    "roadside_mowing_required",
    "lane_marking_faded",
    "colored_guidance_faded",
    "tunnel_name_occlusion",
}
_DEFAULT_EASYOCR_PACK_DIRECTORY = (
    "trafficvisson-jaidedai-easyocr-ko-en-0.1.0"
)
_DEFAULT_PROJECT_DETECTOR_TRAINING_PACK_DIRECTORY = (
    "trafficvisson-ustc-dfine-small-road-training-0.1.0"
)


def _default_easyocr_model_pack_manifest() -> Optional[Path]:
    program_data = os.environ.get(
        "PROGRAMDATA", r"C:\ProgramData"
    )
    manifest = (
        Path(program_data)
        / "TrafficVisson"
        / "ModelPacks"
        / _DEFAULT_EASYOCR_PACK_DIRECTORY
        / "trafficvisson-model-pack.json"
    )
    return manifest if manifest.is_file() else None


def _default_project_detector_training_pack_manifest() -> Optional[Path]:
    """Resolve the separately installed, hash-verified CUDA training pack."""

    program_data = os.environ.get(
        "PROGRAMDATA", r"C:\ProgramData"
    )
    manifest = (
        Path(program_data)
        / "TrafficVisson"
        / "ModelPacks"
        / _DEFAULT_PROJECT_DETECTOR_TRAINING_PACK_DIRECTORY
        / "trafficsupervision-detector-training-pack.json"
    )
    return manifest if manifest.is_file() else None


def _select_app_managed_dense_staging_root(
    drive_roots: list[Path],
    *,
    drive_type: Callable[[Path], int],
    disk_usage: Callable[[Path], Any],
) -> Optional[Path]:
    """Select one safe eligible fixed-volume target without claiming it."""

    limits = DenseFrameIndexLimits(sampling_fps=2)
    required_free_bytes = (
        limits.max_total_output_bytes + limits.min_free_disk_bytes
    )
    candidates: list[tuple[int, str, Path]] = []
    for root in drive_roots:
        try:
            root_stat = root.lstat()
            if (
                int(drive_type(root)) != 3
                or not root.is_dir()
                or root.is_symlink()
                or bool(
                    getattr(root_stat, "st_file_attributes", 0)
                    & getattr(
                        stat,
                        "FILE_ATTRIBUTE_REPARSE_POINT",
                        0x400,
                    )
                )
            ):
                continue
            free_bytes = int(disk_usage(root).free)
        except (OSError, TypeError, ValueError):
            continue
        candidate = root / "TrafficSuperVisionDenseStage"
        if (
            free_bytes < required_free_bytes
            or not dense_staging_root_is_short(candidate)
        ):
            continue
        if candidate.exists() or candidate.is_symlink():
            try:
                initialize_dense_staging_root(candidate)
            except DenseFrameIndexStagingUnavailable:
                continue
        candidates.append(
            (free_bytes, str(root).casefold(), candidate)
        )
    if not candidates:
        return None
    candidates.sort(key=lambda row: (-row[0], row[1]))
    return candidates[0][2]


def _app_managed_dense_staging_root() -> Optional[Path]:
    """Choose the roomiest fixed Windows volume for bounded local staging.

    The packaged app cannot assume that the bridge-data volume has the
    16.5 GiB peak required by the dense writer. Explicit configuration still
    wins; this deterministic fallback considers fixed local volumes only,
    requires the full writer cap plus reserve, and exposes no selected path to
    the renderer.
    """

    if os.name != "nt":
        return None
    try:
        import ctypes

        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
    except (AttributeError, OSError):
        return None
    return _select_app_managed_dense_staging_root(
        [
            Path(f"{drive_letter}:\\")
            for drive_letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
        ],
        drive_type=lambda root: int(get_drive_type(str(root))),
        disk_usage=shutil.disk_usage,
    )


def _resolve_dense_staging_configuration(
    explicit_root: Optional[str],
    *,
    production_runtime: bool,
) -> tuple[Optional[Path], str]:
    if explicit_root:
        return Path(explicit_root), "configured"
    if production_runtime:
        managed = _app_managed_dense_staging_root()
        if managed is not None:
            return managed, "app_managed_fixed_volume"
    return None, "bridge_data_fallback"


def create_app(
    token: str,
    *,
    fixture_mode: bool = False,
    allow_test_client: bool = False,
    ffmpeg_path: Optional[str] = None,
    ffprobe_path: Optional[str] = None,
    dense_staging_root: Optional[str] = None,
    bridge_data_root: Optional[str] = None,
    candidate_extraction_limits: Optional[ExtractionLimits] = None,
    media_stability_min_interval_seconds: Optional[float] = None,
    provider_status_service: Optional[ProviderStatusService] = None,
    report_generation_service: Optional[ReportGenerationService] = None,
    structured_vision_judgment_service: Optional[
        StructuredVisionJudgmentProvider
    ] = None,
    hwp_service: Optional[HwpComService] = None,
    hwp_approval_registry: Optional[HwpApprovalRegistry] = None,
    hwp_sample_approval_registry: Optional[
        HwpSampleApprovalRegistry
    ] = None,
    current_report_draft_service: Optional[
        CurrentReportDraftService
    ] = None,
    current_report_approval_registry: Optional[
        CurrentReportApprovalRegistry
    ] = None,
    current_report_application_service: Optional[
        CurrentReportApplicationService
    ] = None,
    current_report_publication_service: Optional[
        CurrentReportPublicationService
    ] = None,
    vision_inference_service: Optional[LocalVisionInferenceService] = None,
    ocr_inference_service: Optional[LocalVisionInferenceService] = None,
    source_candidate_probe: Optional[SourceCandidateProbe] = None,
    command_proposal_generator: Optional[CommandProposalGenerator] = None,
    pipeline_runtime: Optional[PipelineRuntime] = None,
    runtime_readiness_service: Optional[RuntimeReadinessService] = None,
    weak_case_catalog: Optional[WeakCaseCatalog] = None,
    weak_case_approval_store: Optional[WeakCaseApprovalStore] = None,
    weak_case_standardization_store: Optional[
        WeakCaseStandardizationProposalStore
    ] = None,
    weak_case_visual_index_service: Optional[
        WeakCaseVisualIndexService
    ] = None,
    visual_evidence_semantic_observer: Optional[
        LocalRoadSceneObserver
    ] = None,
    project_model_lifecycle_store: Optional[
        ProjectModelLifecycleStore
    ] = None,
    project_detector_training_pack_manifest: Optional[str] = None,
    project_detector_external_runtime_manifest: Optional[str] = None,
    project_detector_external_runtime_manifest_sha256: Optional[str] = None,
    promoted_detector_inference: Optional[
        PromotedLocalObjectDetectorInference
    ] = None,
    provider_visual_evidence_service: Optional[
        ProviderVisualEvidencePreparationService
    ] = None,
    report_deidentified_evidence_service: Optional[
        ReportDeidentifiedEvidenceService
    ] = None,
    sam2_refinement_adapter: Optional[Sam2RefinementAdapter] = None,
) -> FastAPI:
    app = FastAPI(
        title="TrafficSuperVision Desktop Bridge",
        version="1.0",
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if media_stability_min_interval_seconds is None:
        media_stability_min_interval_seconds = (
            0.0 if allow_test_client else 10.0
        )
    app.state.media_intake = MediaIntakeService(
        ffprobe_path=ffprobe_path,
        minimum_interval_seconds=media_stability_min_interval_seconds,
    )
    bridge_data_root_was_explicit = bridge_data_root is not None
    if bridge_data_root is None:
        local_data = os.environ.get("LOCALAPPDATA")
        if local_data:
            current_root = (
                Path(local_data) / "TrafficSuperVision" / "bridge-data"
            )
            legacy_root = (
                Path(local_data)
                / ("Traffic" + "Visson")
                / "bridge-data"
            )
            # Existing installations keep using their original durable store
            # until an explicit data migration is performed. New installations
            # start under the TrafficSuperVision identity.
            bridge_data_root = str(
                legacy_root
                if legacy_root.exists() and not current_root.exists()
                else current_root
            )
        else:
            bridge_data_root = str(
                Path(tempfile.gettempdir()) / "TrafficSuperVision-bridge-data"
            )
    bridge_data_path = Path(bridge_data_root).resolve()
    bridge_data_path.mkdir(parents=True, exist_ok=True)
    app.state.bridge_data_root = bridge_data_path
    app.state.project_artifact_roots = (
        ProjectArtifactRootRegistry(data_root=bridge_data_path)
    )

    def resolve_analysis_artifact_root(
        project_id: UUID,
        pipeline_job_id: UUID,
        category: str,
        binding: Optional[Mapping[str, Any]],
    ) -> ResolvedAnalysisArtifactRoot:
        if (
            category != ANALYSIS_ARTIFACT_CATEGORY
            or not isinstance(binding, Mapping)
        ):
            raise RuntimeError(
                "analysis artifact-root binding is invalid"
            )
        resolved = (
            app.state.project_artifact_roots.resolve_job_binding(
                project_id, binding
            )
        )
        category_root = (
            resolved.root
            / "pipeline-jobs"
            / str(pipeline_job_id)
            / category
        )
        category_root.mkdir(parents=True, exist_ok=True)
        return ResolvedAnalysisArtifactRoot(
            root=category_root,
            binding=dict(binding),
        )

    def resolve_project_model_artifact_root(
        project_id: UUID,
        training_job_id: UUID,
        category: str,
        binding: Optional[Mapping[str, Any]],
    ) -> ResolvedModelArtifactRoot:
        if (
            category != PROJECT_MODEL_ARTIFACT_CATEGORY
            or not isinstance(binding, Mapping)
        ):
            raise RuntimeError(
                "project model artifact-root binding is invalid"
            )
        resolved = (
            app.state.project_artifact_roots.resolve_job_binding(
                project_id, binding
            )
        )
        category_root = (
            resolved.root / category / str(training_job_id)
        )
        category_root.mkdir(parents=True, exist_ok=True)
        return ResolvedModelArtifactRoot(
            root=category_root,
            binding=dict(binding),
        )
    app.state.stable_media_snapshots = StableMediaSnapshotStore(
        bridge_data_path
    )
    app.state.stable_media_snapshot_recovery = (
        app.state.stable_media_snapshots.restore_all(
            app.state.media_intake
        )
    )
    app.state.provider_visual_evidence_fixture_temp = None
    provider_visual_root = bridge_data_path
    if fixture_mode and not bridge_data_root_was_explicit:
        app.state.provider_visual_evidence_fixture_temp = (
            tempfile.TemporaryDirectory(
                prefix="trafficvisson-provider-visual-test-"
            )
        )
        provider_visual_root = Path(
            app.state.provider_visual_evidence_fixture_temp.name
        )

    def pipeline_artifact_category_resolver(
        category: str,
    ) -> Callable[[UUID, UUID, UUID], Path]:
        def resolve(
            project_id: UUID,
            pipeline_job_id: UUID,
            analysis_job_id: UUID,
        ) -> Path:
            service = getattr(app.state, "pipeline_jobs", None)
            if service is None:
                raise RuntimeError(
                    "pipeline artifact-root resolver is not ready"
                )
            return service.resolve_job_artifact_category(
                project_id,
                pipeline_job_id,
                analysis_job_id,
                category,
            )

        return resolve

    app.state.provider_visual_evidence_startup_error_code = None
    if provider_visual_evidence_service is not None:
        app.state.provider_visual_evidence = (
            provider_visual_evidence_service
        )
    else:
        try:
            app.state.provider_visual_evidence = (
                ProviderVisualEvidencePreparationService(
                    provider_visual_root
                    / "provider-visual-evidence",
                    artifact_root_resolver=(
                        pipeline_artifact_category_resolver(
                            "provider-visual-evidence"
                        )
                    ),
                )
            )
        except ProviderVisualEvidenceError as error:
            app.state.provider_visual_evidence = None
            app.state.provider_visual_evidence_startup_error_code = (
                error.code
            )
    app.state.report_deidentified_evidence_startup_error_code = None
    if report_deidentified_evidence_service is not None:
        app.state.report_deidentified_evidence = (
            report_deidentified_evidence_service
        )
    else:
        try:
            app.state.report_deidentified_evidence = (
                ReportDeidentifiedEvidenceService(
                    (
                        provider_visual_root
                        / "report-deidentified-evidence"
                    ).resolve(),
                    artifact_root_resolver=(
                        pipeline_artifact_category_resolver(
                            "report-deidentified-evidence"
                        )
                    ),
                )
            )
        except ReportDeidentifiedEvidenceError as error:
            app.state.report_deidentified_evidence = None
            app.state.report_deidentified_evidence_startup_error_code = (
                error.code
            )
    app.state.source_candidate_probe = (
        source_candidate_probe or SourceCandidateProbe.from_environment()
    )
    app.state.store = TrafficSuperVisionStore(
        fixture_mode=fixture_mode,
        data_root=(
            bridge_data_path
            if not fixture_mode or bridge_data_root_was_explicit
            else None
        ),
    )
    app.state.weak_case_catalog = (
        weak_case_catalog
        if weak_case_catalog is not None
        else WeakCaseCatalog(default_sample_root())
    )
    app.state.weak_case_approvals = (
        weak_case_approval_store
        if weak_case_approval_store is not None
        else WeakCaseApprovalStore(
            bridge_data_path,
            app.state.weak_case_catalog,
        )
    )
    # Anonymous fixture apps use an isolated temporary root so tests cannot
    # create or reuse a real user's project-model ledger. The actual lifecycle
    # store is constructed after the verified CLIP pack and analysis evidence
    # resolver are ready.
    app.state.project_model_fixture_temp = None
    project_model_root = bridge_data_path
    if fixture_mode and not bridge_data_root_was_explicit:
        app.state.project_model_fixture_temp = (
            tempfile.TemporaryDirectory(
                prefix="trafficvisson-project-model-test-"
            )
        )
        project_model_root = Path(
            app.state.project_model_fixture_temp.name
        )
    app.state.model_iteration_qa = ModelIterationQaStore(
        project_model_root / "model-iteration-qa.sqlite3"
    )
    external_runtime = external_model_runtime_status()
    external_runtime_selected = bool(
        os.environ.get(
            "TRAFFICSUPERVISION_EXTERNAL_MODEL_RUNTIME_PACK"
        )
    )

    def external_runtime_allows(capability: str) -> bool:
        if not external_runtime_selected:
            # Source development and the standalone command runner retain
            # their existing explicitly configured runtime behavior.
            return True
        return (
            external_runtime.get("state") == "ready"
            and capability
            in {
                value
                for value in (
                    external_runtime.get("capabilities") or ()
                )
                if isinstance(value, str)
            }
        )

    clip_image_embedder = None
    if (
        weak_case_visual_index_service is None
        and not fixture_mode
        and external_runtime_allows("embedding_search")
    ):
        configured_clip_manifest = os.environ.get(
            "TRAFFICSUPERVISION_CLIP_MODEL_PACK_MANIFEST"
        )
        program_data = os.environ.get(
            "PROGRAMDATA", r"C:\ProgramData"
        )
        clip_manifest_path = (
            Path(configured_clip_manifest)
            if configured_clip_manifest
            else Path(program_data)
            / "TrafficVisson"
            / "ModelPacks"
            / "trafficvisson-laion-clip-b32-road-baseline-0.1.0"
            / "trafficvisson-model-pack.json"
        )
        if clip_manifest_path.is_file():
            try:
                clip_image_embedder = (
                    VerifiedLocalClipImageEmbedder(
                        clip_manifest_path
                    )
                )
                weak_case_visual_index_service = (
                    WeakCaseVisualIndexService(
                        bridge_data_path
                        / "weak-case-visual-index",
                        catalog=app.state.weak_case_catalog,
                        approvals=app.state.weak_case_approvals,
                        embedder=clip_image_embedder,
                    )
                )
            except WeakCaseVisualIndexError:
                clip_image_embedder = None
                weak_case_visual_index_service = None
    app.state.weak_case_visual_index = (
        weak_case_visual_index_service
    )
    if vision_inference_service is None:
        configured_vision_manifest = os.environ.get(
            "TRAFFICSUPERVISION_VISION_MODEL_PACK"
        )
        vision_inference_service = LocalVisionInferenceService(
            # Rejected/research detector packs are never auto-activated merely
            # because their files exist in ProgramData. A dedicated detector
            # may be selected only through an explicit, reviewed environment
            # binding or a promoted project-model lineage.
            model_pack_path=configured_vision_manifest
        )
    app.state.vision_inference = vision_inference_service
    if ocr_inference_service is None:
        configured_ocr_manifest = os.environ.get(
            "TRAFFICSUPERVISION_OCR_MODEL_PACK"
        )
        default_ocr_manifest = (
            None
            if fixture_mode
            else _default_easyocr_model_pack_manifest()
        )
        ocr_inference_service = LocalVisionInferenceService(
            model_pack_path=(
                configured_ocr_manifest
                or default_ocr_manifest
            )
        )
    app.state.ocr_inference = ocr_inference_service
    app.state.analysis_jobs = AnalysisJobService(
        app.state.media_intake,
        data_root=Path(bridge_data_root),
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        extraction_limits=candidate_extraction_limits,
        vision_inference=vision_inference_service,
        artifact_root_resolver=resolve_analysis_artifact_root,
    )
    app.state.analysis_catalog_recovery = (
        app.state.store.bind_completed_analysis_resolver(
            app.state.analysis_jobs.get
        )
    )
    app.state.dense_candidate_frames = DenseCandidateFrameRegistry(
        bridge_data_path,
        artifact_root_registry=app.state.project_artifact_roots,
    )
    app.state.candidate_evidence_overlays = None
    app.state.candidate_evidence = None
    app.state.review_event_sets = None
    resolved_ffmpeg = (
        str(Path(ffmpeg_path).resolve())
        if ffmpeg_path
        else shutil.which("ffmpeg")
    )

    def resolve_candidate_evidence_binding(
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        source_sha256: str,
    ) -> ResolvedCandidateEvidenceBinding:
        analysis = app.state.analysis_jobs.get(
            project_id,
            analysis_job_id,
        )
        if (
            analysis.get("status") != "completed"
            or analysis.get("source_sha256") != source_sha256
        ):
            raise CandidateResolutionError(
                "candidate evidence analysis binding is invalid"
            )
        pipeline_service = getattr(
            app.state,
            "pipeline_jobs",
            None,
        )
        if pipeline_service is None:
            raise CandidateResolutionError(
                "candidate evidence PipelineJob binding is unavailable"
            )
        pipeline = pipeline_service.get(
            project_id,
            pipeline_job_id,
        )
        sha_stage = next(
            (
                stage
                for stage in pipeline.get("stages", ())
                if stage.get("name") == "sha256"
            ),
            None,
        )
        sha_checkpoint = (
            sha_stage.get("checkpoint")
            if isinstance(sha_stage, dict)
            else None
        )
        if (
            pipeline.get("project_id") != project_id
            or pipeline.get("job_id") != pipeline_job_id
            or pipeline.get("snapshot_id")
            != analysis.get("snapshot_id")
            or pipeline.get("file_name")
            != analysis.get("file_name")
            or not isinstance(sha_checkpoint, dict)
            or sha_checkpoint.get("source_sha256")
            != source_sha256
        ):
            raise CandidateResolutionError(
                "candidate evidence PipelineJob binding is invalid"
            )
        grant = pipeline_service.resolve_completed_media_grant(
            project_id,
            pipeline_job_id,
            analysis_job_id,
            source_sha256,
        )
        return ResolvedCandidateEvidenceBinding(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            analysis_status="completed",
            grant=grant,
        )

    if (
        resolved_ffmpeg
        and Path(resolved_ffmpeg).is_file()
        and (
            not fixture_mode
            or bridge_data_root_was_explicit
        )
    ):
        overlay_root = (
            bridge_data_path / "candidate-evidence-overlays"
        )

        def resolve_review_candidate_frame(
            project_id: UUID,
            analysis_job_id: UUID,
            pipeline_job_id: UUID,
            rescan_manifest_sha256: str,
            frame_id: str,
            provenance: Mapping[str, Any],
        ) -> ResolvedDenseCandidateFrame:
            evidence_source = provenance.get("evidence_source")
            if evidence_source == "analysis_representative_frames":
                candidate_id = provenance.get(
                    "analysis_candidate_id"
                )
                expected_sha256 = provenance.get(
                    "peak_evidence_image_sha256"
                )
                if (
                    not isinstance(candidate_id, str)
                    or re.fullmatch(
                        r"[a-f0-9]{20}", candidate_id
                    )
                    is None
                    or candidate_id != frame_id
                    or not isinstance(expected_sha256, str)
                    or re.fullmatch(
                        r"[a-f0-9]{64}", expected_sha256
                    )
                    is None
                ):
                    raise ReviewEventSetIntegrityError(
                        "analysis review frame provenance is invalid"
                    )
                evidence_backend = (
                    app.state.candidate_evidence
                    if app.state.candidate_evidence is not None
                    else app.state.analysis_jobs
                )
                try:
                    artifact = evidence_backend.resolve_artifact(
                        project_id,
                        analysis_job_id,
                        candidate_id,
                        "image",
                    )
                except Exception as error:
                    raise ReviewEventSetIntegrityError(
                        "analysis review frame is unavailable"
                    ) from error
                if artifact.sha256 != expected_sha256:
                    raise ReviewEventSetIntegrityError(
                        "analysis review frame changed"
                    )
                return ResolvedDenseCandidateFrame(
                    project_id=project_id,
                    dense_index_sha256=rescan_manifest_sha256,
                    frame_id=frame_id,
                    frames_root=artifact.path.parent,
                    path=artifact.path,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                )
            if evidence_source != (
                "candidate_window_rescan_5_10fps"
            ):
                raise ReviewEventSetIntegrityError(
                    "review frame evidence source is invalid"
                )
            run_id = provenance.get("rescan_run_id")
            input_revision = provenance.get(
                "rescan_input_revision"
            )
            source_sha256 = provenance.get("source_sha256")
            declared_manifest_sha256 = provenance.get(
                "rescan_manifest_sha256"
            )
            if (
                not isinstance(run_id, str)
                or re.fullmatch(
                    r"candidate-window-rescan-[a-f0-9]{24}",
                    run_id,
                )
                is None
                or not isinstance(input_revision, str)
                or re.fullmatch(r"[a-f0-9]{64}", input_revision)
                is None
                or not isinstance(source_sha256, str)
                or re.fullmatch(r"[a-f0-9]{64}", source_sha256)
                is None
                or not isinstance(
                    declared_manifest_sha256, str
                )
                or declared_manifest_sha256
                != rescan_manifest_sha256
                or re.fullmatch(
                    r"[a-f0-9]{64}", rescan_manifest_sha256
                )
                is None
                or not isinstance(frame_id, str)
                or re.fullmatch(r"[a-f0-9]{20}", frame_id)
                is None
            ):
                raise ReviewEventSetIntegrityError(
                    "review frame provenance is invalid"
                )
            try:
                pipeline_job = app.state.pipeline_jobs.get(
                    project_id, pipeline_job_id
                )
                root_binding = pipeline_job.get(
                    "provenance", {}
                ).get("artifact_root")
                if isinstance(root_binding, Mapping):
                    project_artifact_root = (
                        app.state.project_artifact_roots
                        .resolve_job_binding(
                            project_id, root_binding
                        )
                        .root
                    )
                    run_directory = (
                        project_artifact_root
                        / "pipeline-jobs"
                        / str(pipeline_job_id)
                        / "candidate-window-rescans"
                        / run_id
                    )
                else:
                    run_directory = (
                        bridge_data_path
                        / "projects"
                        / str(project_id)
                        / "pipeline"
                        / "candidate-window-rescans"
                        / str(pipeline_job_id)
                        / run_id
                    )
            except Exception as error:
                raise ReviewEventSetIntegrityError(
                    "review frame artifact root is unavailable"
                ) from error
            try:
                result = load_candidate_window_rescan(
                    run_directory,
                    expected_source_sha256=source_sha256,
                    expected_input_revision=input_revision,
                )
                if result.manifest_sha256 != rescan_manifest_sha256:
                    raise CandidateWindowRescanError(
                        "review frame rescan manifest changed"
                    )
                frame = next(
                    (
                        item
                        for item in result.frames
                        if item.evidence_id == frame_id
                    ),
                    None,
                )
                if frame is None:
                    raise CandidateWindowRescanError(
                        "review frame identity is unavailable"
                    )
                artifact = resolve_candidate_window_frame(
                    result, frame_id
                )
            except CandidateWindowRescanError as error:
                raise ReviewEventSetIntegrityError(
                    "review frame failed fresh verification"
                ) from error
            return ResolvedDenseCandidateFrame(
                project_id=project_id,
                dense_index_sha256=rescan_manifest_sha256,
                frame_id=frame_id,
                frames_root=artifact.path.parent,
                path=artifact.path,
                media_type=artifact.media_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
            )

        app.state.candidate_evidence_overlays = (
            CandidateEvidenceOverlayStore(
                overlay_root,
                binding_resolver=(
                    resolve_candidate_evidence_binding
                ),
                frame_resolver=(
                    app.state.dense_candidate_frames.resolve
                ),
                review_frame_resolver=(
                    resolve_review_candidate_frame
                ),
                artifact_root_resolver=(
                    pipeline_artifact_category_resolver(
                        "candidate-evidence"
                    )
                ),
                ffmpeg_path=Path(resolved_ffmpeg),
            )
        )
        app.state.candidate_evidence = CandidateEvidenceRegistry(
            app.state.analysis_jobs,
            app.state.candidate_evidence_overlays,
            overlay_root=overlay_root,
            batch_root=(
                bridge_data_path
                / "candidate-evidence-inference-batches"
            ),
        )

        def resolve_review_candidate_evidence(
            project_id: UUID,
            analysis_job_id: UUID,
            pipeline_job_id: UUID,
            overlay_id: str,
            source_sha256: str,
        ) -> ResolvedReviewCandidateEvidenceSet:
            overlay_store = app.state.candidate_evidence_overlays
            if overlay_store is None:
                raise ReviewEventSetIntegrityError(
                    "candidate evidence overlay store is unavailable"
                )
            registry = app.state.candidate_evidence
            if registry is not None:
                # External overlay locators intentionally persist no absolute
                # path. During early app construction they remain dormant
                # because PipelineJobService is not ready yet. At the first
                # review resolution after a restart, re-scan now that the
                # exact job/category resolver can re-pin every sibling
                # overlay. Otherwise the active overlay alone would make
                # detector candidates from the other verified overlay look
                # foreign.
                snapshot = registry.resolve_pipeline_snapshot(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    overlay_id,
                    source_sha256,
                )
                member_bindings = tuple(snapshot.bindings)
                envelopes = tuple(snapshot.candidates)
            else:
                adapter = CandidateEvidenceOverlayAdapter(
                    overlay_store,
                    pipeline_job_id=pipeline_job_id,
                    overlay_id=overlay_id,
                )
                result, envelopes = adapter.snapshot_candidates(
                    project_id,
                    analysis_job_id,
                )
                if (
                    result.project_id != str(project_id)
                    or result.analysis_job_id
                    != str(analysis_job_id)
                    or result.pipeline_job_id
                    != str(pipeline_job_id)
                    or result.overlay_id != overlay_id
                    or result.source_sha256 != source_sha256
                ):
                    raise ReviewEventSetIntegrityError(
                        "candidate evidence identity changed"
                    )
                member_bindings = (
                    {
                        "overlay_id": overlay_id,
                        "manifest_sha256": (
                            result.manifest_sha256
                        ),
                        "candidate_set_metadata_sha256": (
                            result.candidate_metadata_sha256
                        ),
                        "candidate_count": len(result.candidates),
                    },
                )

            members = []
            candidates_list = []
            seen_candidates = set()
            for envelope in envelopes:
                candidate_id = str(envelope["candidate_id"])
                if candidate_id in seen_candidates:
                    raise ReviewEventSetIntegrityError(
                        "candidate evidence identity collided"
                    )
                seen_candidates.add(candidate_id)
                candidates_list.append(
                    ResolvedReviewCandidateEvidence(
                        candidate_id=candidate_id,
                        candidate_metadata_sha256=str(
                            envelope[
                                "candidate_metadata_sha256"
                            ]
                        ),
                        metadata=dict(envelope["metadata"]),
                    )
                )

            for binding in member_bindings:
                if isinstance(binding, Mapping):
                    member_id = str(binding["overlay_id"])
                    manifest_sha256 = str(
                        binding["manifest_sha256"]
                    )
                    candidate_set_sha256 = str(
                        binding[
                            "candidate_set_metadata_sha256"
                        ]
                    )
                    candidate_count = int(
                        binding["candidate_count"]
                    )
                else:
                    member_id = str(binding.overlay_id)
                    manifest_sha256 = str(
                        binding.manifest_sha256
                    )
                    candidate_set_sha256 = str(
                        binding.candidate_set_metadata_sha256
                    )
                    candidate_count = int(
                        binding.candidate_count
                    )
                members.append(
                    {
                        "overlay_id": member_id,
                        "manifest_sha256": manifest_sha256,
                        "candidate_set_metadata_sha256": (
                            candidate_set_sha256
                        ),
                        "candidate_count": candidate_count,
                    }
                )
            candidates = tuple(
                sorted(
                    candidates_list,
                    key=lambda item: item.candidate_id,
                )
            )
            if len(members) == 1:
                overlay_manifest_sha256 = (
                    members[0]["manifest_sha256"]
                )
                candidate_set_metadata_sha256 = (
                    members[0][
                        "candidate_set_metadata_sha256"
                    ]
                )
                overlay_members = ()
            else:
                canonical_members = sorted(
                    members,
                    key=lambda item: item["overlay_id"],
                )
                overlay_manifest_sha256 = hashlib.sha256(
                    json.dumps(
                        canonical_members,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                candidate_set_metadata_sha256 = hashlib.sha256(
                    json.dumps(
                        [
                            {
                                "candidate_id": (
                                    item.candidate_id
                                ),
                                "candidate_metadata_sha256": (
                                    item.candidate_metadata_sha256
                                ),
                            }
                            for item in candidates
                        ],
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                overlay_members = tuple(canonical_members)
            return ResolvedReviewCandidateEvidenceSet(
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                pipeline_job_id=pipeline_job_id,
                overlay_id=overlay_id,
                source_sha256=source_sha256,
                overlay_manifest_sha256=(
                    overlay_manifest_sha256
                ),
                candidate_set_metadata_sha256=(
                    candidate_set_metadata_sha256
                ),
                candidates=candidates,
                overlay_members=overlay_members,
            )

        app.state.review_event_sets = ReviewEventSetStore(
            bridge_data_path / "review-event-sets",
            evidence_resolver=resolve_review_candidate_evidence,
        )
    app.state.promoted_detector_inference = (
        promoted_detector_inference
        if promoted_detector_inference is not None
        else PromotedLocalObjectDetectorInference()
    )

    def resolve_detector_training_artifact(
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        artifact_ref: str,
    ) -> ResolvedDetectorArtifact:
        registry = app.state.candidate_evidence
        if registry is None:
            raise ProjectModelLifecycleError(
                "training_evidence_unavailable",
                "candidate evidence registry is unavailable",
            )
        envelope = registry.resolve_candidate(
            project_id,
            analysis_job_id,
            candidate_id,
        )
        metadata = (
            envelope.get("metadata")
            if isinstance(envelope, Mapping)
            else None
        )
        image = (
            metadata.get("representative_image")
            if isinstance(metadata, Mapping)
            else None
        )
        if (
            not isinstance(image, Mapping)
            or image.get("artifact_ref") != artifact_ref
        ):
            raise ProjectModelLifecycleError(
                "training_evidence_boundary_violation",
                "the training image artifact reference changed",
            )
        resolved = registry.resolve_artifact(
            project_id,
            analysis_job_id,
            candidate_id,
            "image",
        )
        if (
            resolved.sha256 != image.get("sha256")
            or resolved.size_bytes != image.get("size_bytes")
            or resolved.media_type != image.get("media_type")
        ):
            raise ProjectModelLifecycleError(
                "training_evidence_integrity_failed",
                "the training image metadata changed after resolution",
            )
        return ResolvedDetectorArtifact(
            path=resolved.path,
            media_type=resolved.media_type,
            size_bytes=resolved.size_bytes,
            sha256=resolved.sha256,
            project_id=str(project_id),
            analysis_job_id=str(analysis_job_id),
            candidate_id=candidate_id,
            artifact_ref=artifact_ref,
        )

    def resolve_derived_detector_training_artifact(
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        sample: DerivedDetectorGeometrySample,
    ) -> ResolvedDerivedDetectorArtifact:
        registry = app.state.candidate_evidence
        if registry is None:
            raise ProjectModelLifecycleError(
                "training_evidence_unavailable",
                "candidate evidence registry is unavailable",
            )
        if (
            not isinstance(sample, DerivedDetectorGeometrySample)
            or sample.project_id != str(project_id)
            or sample.analysis_job_id != str(analysis_job_id)
            or sample.candidate_id != candidate_id
            or sample.source_frame_kind != "sam2_tracked_frame"
            or sample.box_sha256 is None
        ):
            raise ProjectModelLifecycleError(
                "training_evidence_boundary_violation",
                "derived detector evidence identity changed",
            )
        # Re-resolve both owner and evidence candidates.  The owner lookup
        # prevents a path-free track ref from being used after its approved
        # question candidate disappears or crosses analysis scope.
        registry.resolve_candidate(
            project_id,
            analysis_job_id,
            candidate_id,
        )
        envelope = registry.resolve_candidate(
            project_id,
            analysis_job_id,
            sample.evidence_candidate_id,
        )
        metadata = (
            envelope.get("metadata")
            if isinstance(envelope, Mapping)
            else None
        )
        image = (
            metadata.get("representative_image")
            if isinstance(metadata, Mapping)
            else None
        )
        if (
            not isinstance(image, Mapping)
            or image.get("artifact_ref") != sample.image_artifact_ref
            or image.get("sha256") != sample.image_sha256
        ):
            raise ProjectModelLifecycleError(
                "training_evidence_boundary_violation",
                "tracked-frame artifact binding changed",
            )
        resolved = registry.resolve_artifact(
            project_id,
            analysis_job_id,
            sample.evidence_candidate_id,
            "image",
        )
        if (
            resolved.sha256 != image.get("sha256")
            or resolved.size_bytes != image.get("size_bytes")
            or resolved.media_type != image.get("media_type")
        ):
            raise ProjectModelLifecycleError(
                "training_evidence_integrity_failed",
                "tracked-frame metadata changed after resolution",
            )
        return ResolvedDerivedDetectorArtifact(
            path=resolved.path,
            media_type=resolved.media_type,
            size_bytes=resolved.size_bytes,
            sha256=resolved.sha256,
            project_id=str(project_id),
            analysis_job_id=str(analysis_job_id),
            candidate_id=candidate_id,
            evidence_candidate_id=sample.evidence_candidate_id,
            artifact_ref=sample.image_artifact_ref,
            event_group_id=sample.event_group_id,
            frame_id=sample.frame_id,
            frame_sha256=sample.frame_sha256,
            box_sha256=sample.box_sha256,
            mask_sha256=sample.mask_sha256,
        )

    if project_model_lifecycle_store is None:
        project_model_trainer = None
        configured_detector_pack = (
            project_detector_training_pack_manifest
            or os.environ.get(
                "TRAFFICSUPERVISION_PROJECT_DETECTOR_TRAINING_PACK"
            )
        )
        if (
            configured_detector_pack is None
            and not fixture_mode
            and not allow_test_client
        ):
            default_detector_pack = (
                _default_project_detector_training_pack_manifest()
            )
            configured_detector_pack = (
                str(default_detector_pack)
                if default_detector_pack is not None
                else None
            )
        configured_external_detector_runtime = (
            project_detector_external_runtime_manifest
            or os.environ.get(
                "TRAFFICSUPERVISION_DFINE_TRAINING_RUNTIME"
            )
        )
        configured_external_detector_runtime_sha256 = (
            project_detector_external_runtime_manifest_sha256
            or os.environ.get(
                "TRAFFICSUPERVISION_DFINE_TRAINING_RUNTIME_SHA256"
            )
        )
        if configured_detector_pack is not None and not fixture_mode:
            project_model_trainer = LocalCudaObjectDetectorTrainer(
                model_pack_manifest=configured_detector_pack,
                artifact_resolver=resolve_detector_training_artifact,
                derived_artifact_resolver=(
                    resolve_derived_detector_training_artifact
                ),
                active_model_resolver=lambda project_id: (
                    app.state.project_models.resolve_active_model(
                        project_id
                    )
                ),
                active_model_evaluator=(
                    app.state.promoted_detector_inference
                ),
                backend=(
                    ExternalDfineCudaDetectorBackend(
                        configured_external_detector_runtime,
                        trusted_manifest_sha256=(
                            configured_external_detector_runtime_sha256
                        ),
                    )
                    if configured_external_detector_runtime is not None
                    else None
                ),
            )
        elif clip_image_embedder is not None and not fixture_mode:
            project_model_trainer = (
                VisualEmbeddingCentroidHeadTrainer(
                    embedder=clip_image_embedder,
                    candidate_artifact_resolver=(
                        (
                            app.state.candidate_evidence.resolve_artifact
                            if app.state.candidate_evidence is not None
                            else app.state.analysis_jobs.resolve_artifact
                        )
                    ),
                    approved_reference_catalog=(
                        app.state.weak_case_catalog
                    ),
                    require_gpu_execution=True,
                    active_model_resolver=lambda project_id: (
                        app.state.project_models.resolve_active_model(
                            project_id
                        )
                    ),
                )
            )
        project_model_lifecycle_store = ProjectModelLifecycleStore(
            project_model_root,
            trainer=project_model_trainer,
            artifact_root_resolver=(
                resolve_project_model_artifact_root
            ),
            start_worker=False,
        )
    app.state.project_models = project_model_lifecycle_store
    start_model_dispatcher = getattr(
        app.state.project_models,
        "start_dispatcher",
        None,
    )
    if callable(start_model_dispatcher):
        start_model_dispatcher()
    app.state.project_model_inference = (
        VisualEmbeddingCentroidHeadInference(
            clip_image_embedder
        )
        if clip_image_embedder is not None
        else None
    )
    dense_candidate_providers = [
        LocalVisionDenseProposalProvider(
            app.state.vision_inference,
            role="detector",
        ),
        LocalVisionDenseProposalProvider(
            app.state.ocr_inference,
            role="ocr",
        ),
    ]
    if (
        app.state.project_model_inference is not None
        or app.state.promoted_detector_inference is not None
    ):
        dense_candidate_providers.append(
            ActiveProjectModelDenseProposalProvider(
                app.state.project_models.resolve_active_model,
                app.state.project_model_inference,
                detector_inference=(
                    app.state.promoted_detector_inference
                ),
            )
        )
    if app.state.weak_case_visual_index is not None:
        dense_candidate_providers.append(
            ApprovedReferenceDenseProposalProvider(
                app.state.weak_case_visual_index,
            )
        )
    app.state.dense_candidate_discovery = (
        DenseCandidateDiscoveryEngine(
            dense_candidate_providers
        )
        if app.state.candidate_evidence_overlays is not None
        else None
    )

    def resolve_dense_index_source(
        project_id: UUID,
        opaque_source_identity: str,
        source_sha256: str,
    ) -> ResolvedDenseIndexSource:
        try:
            analysis_job_id = UUID(opaque_source_identity)
        except (TypeError, ValueError) as error:
            raise DenseIndexJobSourceRejected(
                "The dense-index analysis identity is invalid"
            ) from error
        if str(analysis_job_id) != opaque_source_identity:
            raise DenseIndexJobSourceRejected(
                "The dense-index analysis identity is not canonical"
            )
        try:
            grant = app.state.analysis_jobs.resolve_completed_source(
                project_id,
                analysis_job_id,
            )
        except HTTPException as error:
            raise DenseIndexJobSourceRejected(
                "source_unavailable_reselect_required"
            ) from error
        if grant.sha256_lower != source_sha256:
            raise DenseIndexJobSourceRejected(
                "The completed analysis source digest changed"
            )
        return ResolvedDenseIndexSource(
            project_id=grant.project_id,
            opaque_source_identity=opaque_source_identity,
            source_sha256=grant.sha256_lower,
            media_path=grant.media_path,
            size_bytes=grant.size_bytes,
            modified_time_ns=grant.modified_time_ns,
        )

    configured_dense_staging_root = (
        dense_staging_root
        or os.environ.get(
            "TRAFFICSUPERVISION_DENSE_STAGING_ROOT"
        )
    )
    (
        resolved_dense_staging_root,
        dense_staging_source,
    ) = _resolve_dense_staging_configuration(
        configured_dense_staging_root,
        production_runtime=(
            not fixture_mode and not allow_test_client
        ),
    )
    standalone_dense_staging_root = (
        resolved_dense_staging_root
        if resolved_dense_staging_root is not None
        else bridge_data_path / "dense-staging"
    )
    app.state.dense_index_jobs = DenseIndexJobService(
        resolve_dense_index_source,
        data_root=bridge_data_path / "dense-index",
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        dense_staging_root=standalone_dense_staging_root,
    )

    def resolve_trip_for_gpx(
        project_id: UUID,
        trip_id: UUID,
    ):
        job = app.state.analysis_jobs.get(project_id, trip_id)
        if job["status"] != "completed" or job.get("result") is None:
            raise GpxTrackError(
                "trip_unavailable",
                "A GPX track can only be attached to a completed analysis trip",
            )
        return {
            "project_id": str(project_id),
            "trip_id": str(trip_id),
            "duration_seconds": job["result"]["provenance"][
                "duration_seconds"
            ],
        }

    app.state.gpx_tracks = GpxTrackStore(
        bridge_data_path,
        resolve_trip_for_gpx,
    )

    def resolve_completed_candidate(
        project_id: UUID, analysis_job_id: UUID, candidate_id: str
    ):
        if app.state.candidate_evidence is not None:
            try:
                resolved = app.state.candidate_evidence.resolve_candidate(
                    project_id,
                    analysis_job_id,
                    candidate_id,
                )
                metadata = dict(resolved["metadata"])
                evidence = metadata.get("evidence_overlay")
                review_store = app.state.review_event_sets
                if (
                    isinstance(evidence, dict)
                    and review_store is not None
                ):
                    try:
                        pipeline_job_id = UUID(
                            str(evidence["pipeline_job_id"])
                        )
                        resolved_binding = (
                            review_store.find_candidate_binding(
                                project_id,
                                analysis_job_id,
                                pipeline_job_id,
                                candidate_id,
                            )
                        )
                    except ReviewEventSetNotFound:
                        resolved_binding = None
                    except Exception as error:
                        raise CandidateResolutionError(
                            "review event evidence failed integrity verification"
                        ) from error
                    if resolved_binding is not None:
                        event_set, event = resolved_binding
                        metadata["review_event_binding"] = {
                            "schema_version": (
                                "trafficvisson.review-event-binding.v1"
                            ),
                            "review_event_set_id": (
                                event_set.review_event_set_id
                            ),
                            "review_event_set_revision": (
                                event_set.review_event_set_revision
                            ),
                            "review_event_set_manifest_sha256": (
                                event_set.manifest_sha256
                            ),
                            "review_event_id": event.event_id,
                            "review_event_revision": (
                                event.event_revision
                            ),
                            "representative_candidate_id": (
                                event.representative_candidate_id
                            ),
                            "review_only": True,
                        }
                return {
                    **dict(resolved),
                    "metadata": metadata,
                }
            except HTTPException as error:
                if error.status_code != 404:
                    raise CandidateResolutionError(
                        "candidate evidence failed integrity verification"
                    ) from error
        job = app.state.analysis_jobs.get(project_id, analysis_job_id)
        if job["status"] != "completed" or job.get("result") is None:
            raise CandidateResolutionError(
                "the analysis candidate is not from a completed job"
            )
        candidate = next(
            (
                item
                for item in job["result"]["candidates"]
                if item["candidate_id"] == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise CandidateResolutionError(
                "the completed analysis candidate was not found"
            )
        return {
            "status": "completed",
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "candidate_id": candidate_id,
            "metadata": candidate,
        }

    app.state.candidate_labels = CandidateLabelStore(
        bridge_data_path,
        resolve_completed_candidate,
        audit_provenance=not fixture_mode,
    )

    def resolve_structured_llm_training_answer(
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> dict:
        """Resolve one immutable ReviewEvent proposal without renderer data."""

        resolved = resolve_completed_candidate(
            project_id,
            analysis_job_id,
            candidate_id,
        )
        metadata = resolved.get("metadata")
        evidence = (
            metadata.get("evidence_overlay")
            if isinstance(metadata, dict)
            else None
        )
        review_store = app.state.review_event_sets
        if not isinstance(evidence, dict) or review_store is None:
            raise StructuredLlmTrainingLabelError(
                "proposal_unavailable",
                "structured LLM review evidence is unavailable",
            )
        try:
            pipeline_job_id = UUID(str(evidence["pipeline_job_id"]))
            overlay_id = str(evidence["overlay_id"])
            event_sets = review_store.list(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                overlay_id,
            )
        except ReviewEventSetNotFound as error:
            raise StructuredLlmTrainingLabelError(
                "proposal_unavailable",
                "structured LLM review event was not found",
            ) from error
        except (
            KeyError,
            TypeError,
            ValueError,
            ReviewEventSetIntegrityError,
        ) as error:
            raise StructuredLlmTrainingLabelError(
                "proposal_invalid",
                "structured LLM review evidence failed verification",
            ) from error
        bindings = [
            (event_set, event)
            for event_set in event_sets
            for event in event_set.events
            if event.representative_candidate_id == candidate_id
        ]
        if len(bindings) != 1:
            raise StructuredLlmTrainingLabelError(
                (
                    "proposal_unavailable"
                    if not bindings
                    else "proposal_invalid"
                ),
                "structured LLM review event binding is not unique",
            )
        event_set, event = bindings[0]
        answer = structured_llm_training_answer(
            candidate_id=candidate_id,
            event_issue_type=event.issue_type,
            urgent_review=event.urgent_review,
            structured_proposal=event.structured_proposal,
        )
        execution = event.structured_proposal.get(
            "execution_provenance"
        )
        if not isinstance(execution, Mapping):
            raise StructuredLlmTrainingLabelError(
                "proposal_invalid",
                "structured LLM execution provenance is unavailable",
            )
        try:
            build_approved_reference_annotation_lineage(
                candidate_id=candidate_id,
                review_event_set=event_set,
                review_event=event,
            )
        except ApprovedReferenceAnnotationLineageError as error:
            raise StructuredLlmTrainingLabelError(
                "proposal_invalid",
                (
                    "structured LLM proposal lacks verified approved "
                    "3Q/4Q reference lineage"
                ),
            ) from error
        question_id = app.state.candidate_labels.question_id(
            analysis_job_id,
            candidate_id,
        )
        answer["assist_evidence"] = {
            "schema_version": STRUCTURED_ASSIST_PROVENANCE_SCHEMA,
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "question_id": str(question_id),
            "candidate_id": candidate_id,
            "review_event": {
                "pipeline_job_id": str(event_set.pipeline_job_id),
                "overlay_id": event_set.overlay_id,
                "review_event_set_id": (
                    event_set.review_event_set_id
                ),
                "review_event_set_revision": (
                    event_set.review_event_set_revision
                ),
                "review_event_set_manifest_sha256": (
                    event_set.manifest_sha256
                ),
                "review_event_set_source_sha256": (
                    event_set.source_sha256
                ),
                "review_event_id": event.event_id,
                "review_event_revision": event.event_revision,
            },
            "provider": execution["provider"],
            "model": execution["model"],
            "input_sha256": execution["input_sha256"],
            "prompt_sha256": execution["prompt_sha256"],
            "request_provenance_sha256": execution[
                "request_provenance_sha256"
            ],
            "execution_provenance_sha256": execution[
                "execution_provenance_sha256"
            ],
            "attachment_manifest_sha256": execution[
                "attachment_manifest_sha256"
            ],
            "attachment_sha256s": list(
                execution["attachment_sha256s"]
            ),
            "attachment_verification_record_sha256s": list(
                execution[
                    "attachment_verification_record_sha256s"
                ]
            ),
            "proposal_sha256": event.structured_proposal[
                "proposal_sha256"
            ],
        }
        return answer

    app.state.pipeline_structured_label_delegation = (
        PipelineStructuredLabelDelegationService(
            candidate_labels=app.state.candidate_labels,
            proposal_resolver=(
                resolve_structured_llm_training_answer
            ),
            projection_sink=(
                lambda project_id, records, analysis_job_ids:
                app.state.store.project_label_records(
                    project_id,
                    records,
                    materialize_analysis_job_ids=analysis_job_ids,
                )
            ),
            delegation_authority_resolver=(
                lambda project_id: (
                    app.state.pipeline_jobs.get_profile(project_id)
                )
            ),
        )
    )
    sam2_startup_error_code = None
    if (
        sam2_refinement_adapter is None
        and not fixture_mode
        and external_runtime_allows("sam2")
    ):
        configured_sam2_path = os.environ.get(
            "TRAFFICSUPERVISION_SAM2_MODEL_PACK"
        )
        sam2_pack_path = (
            Path(configured_sam2_path)
            if configured_sam2_path
            else default_sam2_model_pack_path()
        )
        if sam2_pack_path.exists():
            try:
                sam2_refinement_adapter = TransformersSam2Adapter(
                    sam2_pack_path
                )
            except TransformersSam2AdapterError:
                sam2_startup_error_code = "model_pack_invalid"
        else:
            sam2_startup_error_code = "model_pack_not_configured"
    elif (
        sam2_refinement_adapter is None
        and not fixture_mode
        and external_runtime_selected
    ):
        sam2_startup_error_code = "model_runtime_pack_invalid"
    app.state.sam2_refinements = Sam2MaskRefinementService(
        adapter=sam2_refinement_adapter,
        artifact_resolver=lambda project_id, job_id, candidate_id: (
            (
                app.state.candidate_evidence.resolve_artifact
                if app.state.candidate_evidence is not None
                else app.state.analysis_jobs.resolve_artifact
            )(
                project_id,
                job_id,
                candidate_id,
                "image",
            )
        ),
        question_resolver=app.state.candidate_labels.resolve_question,
    )
    app.state.sam2_startup_error_code = sam2_startup_error_code
    app.state.experimental_proposals = ExperimentalProposalStore(
        bridge_data_path,
        resolve_completed_candidate,
    )

    class DeferredAppReportGenerator:
        def generate(self, provider, payload, *, cancel_event=None):
            return app.state.report_generation.generate(
                provider,
                payload,
                cancel_event=cancel_event,
            )

    class DeferredAppStructuredVisionJudgment:
        def propose(
            self,
            provider,
            payload,
            *,
            transmission_consent,
            attachments=(),
            cancel_event=None,
        ):
            return app.state.structured_vision_judgment.propose(
                provider,
                payload,
                transmission_consent=transmission_consent,
                attachments=attachments,
                cancel_event=cancel_event,
            )

    app.state.report_semantic_maps = (
        SampleTemplateSemanticMapRegistry()
    )
    app.state.pipeline_report_previews = (
        DurablePipelineReportPreviewPreparer(
            data_root=bridge_data_path,
            semantic_maps=app.state.report_semantic_maps,
            report_generator=DeferredAppReportGenerator(),
        )
    )
    current_report_draft_root = (
        bridge_data_path / "current-report-drafts"
    )

    def resolve_current_report_evidence(
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        selected_ref: str,
        expected_sha256: str,
    ) -> ServerOwnedEvidenceFile:
        if (
            isinstance(selected_ref, str)
            and selected_ref.startswith("report-deid:")
        ):
            registry = app.state.report_deidentified_evidence
            if registry is None:
                raise CurrentReportDraftError(
                    "deidentified_evidence_registry_unavailable",
                    "the report deidentification registry is unavailable",
                )
            try:
                artifact = registry.resolve_verified_file(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    analysis_job_id=analysis_job_id,
                    deidentified_ref=selected_ref,
                    expected_sha256=expected_sha256,
                )
            except ReportDeidentifiedEvidenceError as error:
                raise CurrentReportDraftError(
                    "deidentified_evidence_not_verified",
                    "the selected report derivative is not verified",
                ) from error
            return ServerOwnedEvidenceFile(
                evidence_ref=selected_ref,
                absolute_path=str(artifact.path),
                sha256=artifact.sha256,
            )
        parts = (
            selected_ref.split(":")
            if isinstance(selected_ref, str)
            else []
        )
        if (
            len(parts) != 3
            or parts[0] != "analysis"
            or parts[2] != "representative"
            or len(parts[1]) != 20
            or any(
                character not in "0123456789abcdef"
                for character in parts[1]
            )
        ):
            code = (
                "deidentified_evidence_registry_unavailable"
                if isinstance(selected_ref, str)
                and selected_ref.startswith("deid:")
                else "report_evidence_reference_invalid"
            )
            raise CurrentReportDraftError(
                code,
                "the selected report evidence cannot be resolved by the server",
            )
        evidence_backend = (
            app.state.candidate_evidence
            if app.state.candidate_evidence is not None
            else app.state.analysis_jobs
        )
        try:
            artifact = evidence_backend.resolve_artifact(
                project_id,
                analysis_job_id,
                parts[1],
                "image",
            )
        except HTTPException as error:
            raise CurrentReportDraftError(
                "report_evidence_unavailable",
                "the selected report evidence is unavailable",
            ) from error
        if (
            artifact.sha256 != expected_sha256
            or not Path(artifact.path).is_absolute()
        ):
            raise CurrentReportDraftError(
                "report_evidence_provenance_conflict",
                "the selected report evidence changed after binding",
            )
        return ServerOwnedEvidenceFile(
            evidence_ref=selected_ref,
            absolute_path=str(artifact.path),
            sha256=artifact.sha256,
        )

    app.state.current_report_drafts = (
        current_report_draft_service
        if current_report_draft_service is not None
        else CurrentReportDraftService(
            owned_draft_root=current_report_draft_root,
            report_evidence_resolver=(
                resolve_current_report_evidence
            ),
            server_evidence_root=bridge_data_path,
            artifact_root_resolver=(
                pipeline_artifact_category_resolver("reports")
            ),
            template_source_resolver=(
                lambda template_id, template_sha256: (
                    app.state.report_semantic_maps.resolve(
                        template_id=template_id,
                        template_sha256=template_sha256,
                    ).source_path
                )
            ),
        )
    )
    app.state.current_report_approvals = (
        current_report_approval_registry
        if current_report_approval_registry is not None
        else CurrentReportApprovalRegistry(
            owned_preview_root=current_report_draft_root,
            preview_root_resolver=(
                pipeline_artifact_category_resolver("reports")
            ),
        )
    )
    app.state.current_report_applications = (
        current_report_application_service
        if current_report_application_service is not None
        else CurrentReportApplicationService(
            owned_application_root=(
                bridge_data_path / "current-report-applications"
            ),
            artifact_root_resolver=(
                pipeline_artifact_category_resolver("reports")
            ),
            artifact_root_registry=(
                app.state.project_artifact_roots
            ),
            pipeline_job_resolver=(
                lambda project_id, pipeline_job_id: (
                    app.state.pipeline_jobs.get(
                        project_id,
                        pipeline_job_id,
                    )
                )
            ),
            recover_capacity_on_startup=False,
        )
    )

    def resolve_fixed_validation_analysis_jobs(
        project_id: UUID,
        extension_approval: Optional[object] = None,
    ) -> Sequence[str]:
        """Keep the immutable head fixed or stage an approved child set."""

        head = app.state.project_models.resolve_fixed_validation_head(
            project_id
        )
        if head is None:
            if extension_approval is not None:
                raise ProjectModelLifecycleError(
                    "fixed_validation_extension_approval_stale",
                    (
                        "a fixed-validation extension has no persisted "
                        "parent set"
                    ),
                )
            return ()
        annotations = head.get("fixed_validation_annotations")
        if not isinstance(annotations, list):
            raise ProjectModelLifecycleError(
                "lifecycle_store_corrupt",
                "the fixed-validation head has no immutable membership",
            )
        fixed_jobs = tuple(
            sorted(
                {
                    str(UUID(str(item["analysis_job_id"])))
                    for item in annotations
                }
            )
        )
        if not fixed_jobs:
            raise ProjectModelLifecycleError(
                "fixed_validation_changed",
                "the project has no fixed-validation source lineage",
            )
        if extension_approval is None:
            return fixed_jobs
        approval = (
            extension_approval.model_dump(mode="json")
            if hasattr(extension_approval, "model_dump")
            else dict(extension_approval)
        )
        if (
            approval.get("expected_parent_version")
            != head["validation_set_version"]
            or approval.get("expected_parent_membership_sha256")
            != head["membership_sha256"]
            or approval.get("expected_parent_fixed_validation_sha256")
            != head["fixed_validation_sha256"]
        ):
            raise ProjectModelLifecycleError(
                "fixed_validation_extension_approval_stale",
                (
                    "fixed-validation extension approval does not match "
                    "the persisted parent set"
                ),
            )
        added_jobs = tuple(
            str(value)
            for value in approval.get(
                "added_validation_analysis_job_ids",
                (),
            )
        )
        if (
            not added_jobs
            or added_jobs != tuple(sorted(set(added_jobs)))
            or set(added_jobs).intersection(fixed_jobs)
        ):
            raise ProjectModelLifecycleError(
                "fixed_validation_extension_approval_invalid",
                "fixed-validation extension jobs are invalid",
            )
        return tuple(sorted((*fixed_jobs, *added_jobs)))

    def resolve_dataset_version_exact(
        project_id: UUID,
        dataset_version_id: UUID,
    ) -> Mapping[str, Any]:
        app.state.store.get_project(project_id)
        matches = [
            item
            for item in app.state.store.list_dataset_versions(
                project_id
            )
            if item.get("dataset_version_id")
            == str(dataset_version_id)
        ]
        if len(matches) != 1:
            raise ModelIterationQaError(
                "DatasetVersion was not found in this project"
            )
        return matches[0]

    app.state.derived_outputs = DerivedOutputsService(
        data_root=bridge_data_path,
        candidate_labels=app.state.candidate_labels,
        store=app.state.store,
        analysis_jobs=app.state.analysis_jobs,
        experimental_proposals=app.state.experimental_proposals,
        candidate_evidence=app.state.candidate_evidence,
        gpx_tracks=app.state.gpx_tracks,
        report_preparer=app.state.pipeline_report_previews,
        report_deidentified_evidence=(
            app.state.report_deidentified_evidence
        ),
        approved_reference_resolver=(
            lambda project_id: app.state.weak_case_approvals.list_project(
                project_id
            )["records"]
        ),
        fixed_validation_analysis_jobs_resolver=(
            resolve_fixed_validation_analysis_jobs
        ),
    )
    app.state.auto_label_runs = AutoLabelRunStore(bridge_data_path)
    app.state.command_proposal_generator = (
        command_proposal_generator
        if command_proposal_generator is not None
        else (
            UnavailableCommandProposalGenerator()
            if fixture_mode
            else FixedManifestCommandProposalGenerator
            .from_environment_or_program_data()
        )
    )
    if pipeline_runtime is None:
        pipeline_runtime = BridgePipelineRuntime(
            app.state.media_intake,
            data_root=bridge_data_path,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            dense_staging_root=resolved_dense_staging_root,
            dense_staging_source=dense_staging_source,
            vision_status=app.state.vision_inference.status,
            ocr_inference_service=app.state.ocr_inference,
            ocr_status=app.state.ocr_inference.status,
            analysis_jobs=app.state.analysis_jobs,
            command_proposal_generator=(
                app.state.command_proposal_generator
            ),
            experimental_proposals=app.state.experimental_proposals,
            candidate_labels=app.state.candidate_labels,
            question_catalog_projector=(
                app.state.store.register_label_questions
            ),
            weak_case_catalog=app.state.weak_case_catalog,
            weak_case_approvals=app.state.weak_case_approvals,
            weak_case_visual_index=(
                app.state.weak_case_visual_index
            ),
            visual_evidence_semantic_observer=(
                visual_evidence_semantic_observer
                or clip_image_embedder
            ),
            sam2_status=app.state.sam2_refinements.runtime_status,
            sam2_adapter=sam2_refinement_adapter,
            derived_outputs=app.state.derived_outputs,
            report_semantic_maps=app.state.report_semantic_maps,
            report_provider_status=lambda provider: (
                app.state.provider_status.get_status(provider)
            ),
            structured_vision_judgment=(
                DeferredAppStructuredVisionJudgment()
            ),
            dense_candidate_discovery=(
                app.state.dense_candidate_discovery
            ),
            candidate_evidence_overlays=(
                app.state.candidate_evidence_overlays
            ),
            candidate_evidence_registry=(
                app.state.candidate_evidence
            ),
            dense_index_registration=(
                app.state.dense_candidate_frames.register
            ),
            review_event_sets=app.state.review_event_sets,
            provider_visual_evidence=(
                app.state.provider_visual_evidence
            ),
            artifact_root_registry=(
                app.state.project_artifact_roots
            ),
            structured_label_delegator=(
                app.state.pipeline_structured_label_delegation.apply
            ),
            project_model_resolver=(
                app.state.project_models.resolve_active_model
            ),
            model_iteration_qa=app.state.model_iteration_qa,
            dataset_version_resolver=resolve_dataset_version_exact,
        )
    # Prefer the normal command lookup. Provider services safely resolve an
    # npm shim to a fixed JavaScript entrypoint plus Node and still execute
    # with shell=False; a WindowsApps .exe alias may be ACL-inaccessible.
    explicit_provider_paths = {}
    for provider_name in ("codex", "claude"):
        provider_path = (
            shutil.which(provider_name)
            or (
                shutil.which(f"{provider_name}.exe")
                if os.name == "nt"
                else None
            )
        )
        if provider_path:
            explicit_provider_paths[provider_name] = provider_path
    if provider_status_service is None:
        provider_status_paths = {
            name: value
            for name, value in explicit_provider_paths.items()
        }

        def visible_cli_launcher(argv) -> bool:
            if not argv or not Path(argv[0]).is_absolute():
                return False
            creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            subprocess.Popen(
                list(argv),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creation_flags,
            )
            return True

        provider_status_service = ProviderStatusService(
            explicit_paths=provider_status_paths,
            launcher=visible_cli_launcher,
            safe_path_directories=tuple(
                {
                    str(Path(value).resolve().parent)
                    for value in (
                        shutil.which("node.exe"),
                        shutil.which("node"),
                    )
                    if value
                }
            ),
        )
    app.state.provider_status = provider_status_service
    app.state.report_generation = (
        report_generation_service
        if report_generation_service is not None
        else ReportGenerationService(
            explicit_paths=explicit_provider_paths,
            provider_ready={"codex": True, "claude": True},
        )
    )
    app.state.structured_vision_judgment = (
        structured_vision_judgment_service
        if structured_vision_judgment_service is not None
        else StructuredVisionJudgmentService(
            explicit_paths=explicit_provider_paths,
            provider_ready={"codex": True, "claude": True},
            verified_attachment_roots=(
                [
                    app.state.provider_visual_evidence
                    .attachment_root
                ]
                if app.state.provider_visual_evidence is not None
                else None
            ),
        )
    )
    app.state.weak_case_standardization = (
        weak_case_standardization_store
        if weak_case_standardization_store is not None
        else WeakCaseStandardizationProposalStore(
            bridge_data_path,
            app.state.weak_case_catalog,
            OfficialCliWeakCaseStandardizationProvider(
                explicit_paths=explicit_provider_paths,
                status_resolver=(
                    app.state.provider_status.get_status
                ),
            ),
        )
    )
    # Construct the durable PipelineJob worker only after provider status and
    # generation services exist. Recovered derived-output stages can then
    # resume against the current provider capability instead of racing app
    # initialization and being persisted as a false unavailable result.
    app.state.pipeline_jobs = PipelineJobService(
        pipeline_runtime,
        data_root=bridge_data_path,
        artifact_root_registry=app.state.project_artifact_roots,
    )
    recover_current_report_capacity = getattr(
        app.state.current_report_applications,
        "recover_capacity_reservations",
        None,
    )
    app.state.current_report_capacity_recovery = (
        recover_current_report_capacity()
        if callable(recover_current_report_capacity)
        else {"restored": 0, "released": 0, "blocked": 0}
    )
    app.state.current_report_publications = (
        current_report_publication_service
        if current_report_publication_service is not None
        else CurrentReportPublicationService(
            owned_publication_root=(
                bridge_data_path / "current-report-publications"
            ),
            application_service=(
                app.state.current_report_applications
            ),
            artifact_root_registry=app.state.project_artifact_roots,
            pipeline_job_resolver=app.state.pipeline_jobs.get,
        )
    )

    def pipeline_stage_checkpoint(
        job: Mapping[str, Any],
        stage_name: str,
    ) -> Mapping[str, Any]:
        stage = next(
            (
                value
                for value in job.get("stages", ())
                if isinstance(value, Mapping)
                and value.get("name") == stage_name
            ),
            None,
        )
        checkpoint = (
            stage.get("checkpoint")
            if isinstance(stage, Mapping)
            else None
        )
        if not isinstance(checkpoint, Mapping):
            raise UserDiscoveredCandidateError(
                "independent_survey_binding_invalid",
                f"{stage_name} checkpoint is unavailable",
            )
        return checkpoint

    def resolve_independent_promoted_model_survey(
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
    ) -> ResolvedIndependentSurvey:
        try:
            job = app.state.pipeline_jobs.get(
                project_id, pipeline_job_id
            )
            analysis = app.state.analysis_jobs.get(
                project_id, analysis_job_id
            )
            dense = pipeline_stage_checkpoint(
                job, "dense_index_2fps"
            )
            rescan = pipeline_stage_checkpoint(
                job, "candidate_rescan_5_10fps"
            )
            question = pipeline_stage_checkpoint(
                job, "question_queue"
            )
            model = app.state.project_models.resolve_active_model(
                project_id
            )
        except UserDiscoveredCandidateError:
            raise
        except Exception as error:
            raise UserDiscoveredCandidateError(
                "independent_survey_unavailable",
                "the promoted-model survey could not be resolved",
            ) from error
        if (
            job.get("status") != "awaiting_review"
            or job.get("current_stage") != "question_queue"
            or question.get("analysis_job_id")
            != str(analysis_job_id)
            or analysis.get("status") != "completed"
            or analysis.get("source_sha256")
            != question.get("source_sha256")
        ):
            raise UserDiscoveredCandidateError(
                "independent_survey_incomplete",
                "only the exact prepared independent survey is eligible",
            )
        discovery = rescan.get("dense_candidate_discovery")
        provider_states = (
            discovery.get("provider_states")
            if isinstance(discovery, Mapping)
            else None
        )
        promoted_states = [
            value
            for value in (
                provider_states
                if isinstance(provider_states, list)
                else ()
            )
            if isinstance(value, Mapping)
            and value.get("provider")
            == "promoted_project_model"
            and value.get("state") == "ready"
        ]
        if len(promoted_states) != 1:
            raise UserDiscoveredCandidateError(
                "independent_survey_unavailable",
                "the survey did not run exactly one promoted project model",
            )
        provider = promoted_states[0]
        lineage = {
            "training_analysis_job_ids": list(
                model.training_analysis_job_ids
            ),
            "fixed_validation_analysis_job_ids": list(
                model.fixed_validation_analysis_job_ids
            ),
        }
        lineage_sha256 = _canonical_export_provenance_hash(
            lineage
        )
        if (
            provider.get("independent_survey_verified") is not True
            or provider.get("survey_analysis_job_id")
            != str(analysis_job_id)
            or provider.get("model_version_id")
            != model.model_version_id
            or provider.get("dataset_version_id")
            != model.dataset_version_id
            or provider.get("dataset_hash")
            != model.dataset_hash
            or provider.get("primary_artifact_sha256")
            != model.primary_artifact_sha256
            or provider.get("fixed_validation_sha256")
            != model.fixed_validation_sha256
            or provider.get("state_revision")
            != model.state_revision
            or provider.get("source_analysis_lineage_sha256")
            != model.source_analysis_lineage_sha256
            or lineage_sha256
            != model.source_analysis_lineage_sha256
        ):
            raise UserDiscoveredCandidateError(
                "independent_survey_revision_conflict",
                "the active model changed after dense survey inference",
            )
        dense_run_id = dense.get("dense_run_id")
        dense_sha256 = dense.get("dense_index_sha256")
        if (
            dense.get("analysis_job_id")
            != str(analysis_job_id)
            or dense.get("source_sha256")
            != analysis.get("source_sha256")
            or dense.get("sampling_fps") != 2
            or not isinstance(dense_run_id, str)
            or re.fullmatch(
                r"dense-frame-index-[A-Za-z0-9._-]{6,128}",
                dense_run_id,
            )
            is None
            or not isinstance(dense_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", dense_sha256)
            is None
        ):
            raise UserDiscoveredCandidateError(
                "independent_survey_binding_invalid",
                "the immutable 2 FPS index binding is invalid",
            )
        dense_root = (
            app.state.pipeline_jobs.resolve_job_artifact_category(
                project_id,
                pipeline_job_id,
                analysis_job_id,
                "dense",
            )
        )
        dense_index_path = (
            dense_root / dense_run_id / "frame-index.json"
        ).resolve()
        if (
            dense_index_path.parent.parent != dense_root.resolve()
        ):
            raise UserDiscoveredCandidateError(
                "independent_survey_binding_invalid",
                "the immutable dense index escaped its artifact root",
            )
        provenance = analysis.get("result", {}).get("provenance")
        duration = (
            provenance.get("duration_seconds")
            if isinstance(provenance, Mapping)
            else None
        )
        inference_sha256 = _canonical_export_provenance_hash(
            {
                "pipeline_job_id": str(pipeline_job_id),
                "analysis_job_id": str(analysis_job_id),
                "dense_candidate_discovery_revision": (
                    discovery.get("discovery_revision")
                ),
                "provider_state": dict(provider),
                "active_model_artifact_manifest_sha256": (
                    model.artifact_manifest_sha256
                ),
            }
        )
        try:
            return ResolvedIndependentSurvey(
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                pipeline_job_id=pipeline_job_id,
                analysis_status=str(analysis["status"]),
                survey_purpose=(
                    "independent_promoted_model_qa"
                ),
                source_sha256=str(analysis["source_sha256"]),
                source_duration_seconds=float(duration),
                dense_index_path=dense_index_path,
                dense_index_sha256=dense_sha256,
                dense_sampling_fps=2,
                promoted_model_state="active",
                promoted_model_version_id=UUID(
                    model.model_version_id
                ),
                promoted_model_artifact_manifest_sha256=(
                    model.artifact_manifest_sha256
                ),
                promoted_model_inference_sha256=(
                    inference_sha256
                ),
                dataset_version_id=UUID(
                    model.dataset_version_id
                ),
                dataset_hash=model.dataset_hash,
                training_analysis_job_ids=tuple(
                    UUID(value)
                    for value in model.training_analysis_job_ids
                ),
                fixed_validation_analysis_job_ids=tuple(
                    UUID(value)
                    for value
                    in model.fixed_validation_analysis_job_ids
                ),
                source_analysis_lineage_sha256=(
                    model.source_analysis_lineage_sha256
                ),
            )
        except (TypeError, ValueError) as error:
            raise UserDiscoveredCandidateError(
                "independent_survey_binding_invalid",
                "the active model survey lineage is invalid",
            ) from error

    class PipelineUserDiscoveredEvidenceMaterializer:
        def materialize(
            self,
            *,
            project_id: UUID,
            analysis_job_id: UUID,
            pipeline_job_id: UUID,
            discovery,
            cancel=None,
        ):
            root = (
                app.state.pipeline_jobs
                .resolve_job_artifact_category(
                    project_id,
                    pipeline_job_id,
                    analysis_job_id,
                    "candidate-evidence",
                )
            )
            return app.state.candidate_evidence_overlays.materialize(
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                pipeline_job_id=pipeline_job_id,
                discovery=discovery,
                cancel=cancel,
                artifact_root=root,
            )

    app.state.user_discovered_candidates = None
    app.state.user_discovered_review_queue = None
    app.state.user_discovered_survey_resolver = (
        resolve_independent_promoted_model_survey
    )
    if (
        app.state.candidate_evidence_overlays is not None
        and app.state.review_event_sets is not None
    ):
        app.state.user_discovered_candidates = (
            UserDiscoveredCandidateService(
                survey_resolver=(
                    app.state.user_discovered_survey_resolver
                ),
                evidence_materializer=(
                    PipelineUserDiscoveredEvidenceMaterializer()
                ),
                sam2_runtime_provenance_resolver=(
                    lambda *_args: (
                        app.state.sam2_refinements.runtime_status()
                    )
                ),
            )
        )
        app.state.user_discovered_review_queue = (
            UserDiscoveredReviewQueueService(
                app.state.review_event_sets
            )
        )
    app.state.folder_pipeline_batches = FolderPipelineBatchService(
        app.state.media_intake,
        app.state.pipeline_jobs,
        data_root=bridge_data_path,
        artifact_root_registry=app.state.project_artifact_roots,
    )
    app.state.hwp_service = hwp_service
    app.state.hwp_approvals = (
        hwp_approval_registry
        if hwp_approval_registry is not None
        else HwpApprovalRegistry()
    )
    app.state.hwp_sample_approvals = (
        hwp_sample_approval_registry
        if hwp_sample_approval_registry is not None
        else HwpSampleApprovalRegistry()
    )
    app.state.hwp_rollback_scopes = HwpRollbackScopeRegistry()
    if runtime_readiness_service is None:
        command_status = getattr(
            app.state.command_proposal_generator,
            "status",
            lambda: {
                "state": "unavailable",
                "error_code": "model_pack_not_configured",
            },
        )

        def runtime_hwp_preflight():
            return (
                app.state.hwp_service.preflight()
                if app.state.hwp_service is not None
                else registry_preflight()
            )

        runtime_readiness_service = RuntimeReadinessService(
            provider_status=app.state.provider_status,
            vision_status=app.state.vision_inference.status,
            command_model_status=command_status,
            sam2_status=(
                (
                    lambda: {
                        "state": "failed",
                        "error_code": "model_runtime_pack_invalid",
                    }
                )
                if sam2_startup_error_code
                == "model_runtime_pack_invalid"
                else app.state.sam2_refinements.runtime_status
            ),
            hwp_preflight=runtime_hwp_preflight,
            ffmpeg_path=ffmpeg_path,
            electron_version=os.environ.get(
                "TRAFFICSUPERVISION_ELECTRON_VERSION"
            ),
        )
    app.state.runtime_readiness = runtime_readiness_service

    def shutdown_workers() -> None:
        if app.state.hwp_service is not None:
            app.state.hwp_service.close()
        shutdown_model_dispatcher = getattr(
            app.state.project_models,
            "shutdown",
            None,
        )
        if callable(shutdown_model_dispatcher):
            shutdown_model_dispatcher()
        app.state.folder_pipeline_batches.shutdown()
        app.state.pipeline_jobs.shutdown()
        app.state.dense_index_jobs.shutdown()
        app.state.analysis_jobs.shutdown()
        app.state.media_intake.shutdown()
        if app.state.project_model_fixture_temp is not None:
            app.state.project_model_fixture_temp.cleanup()
        if (
            app.state.provider_visual_evidence_fixture_temp
            is not None
        ):
            app.state.provider_visual_evidence_fixture_temp.cleanup()

    app.add_event_handler("shutdown", shutdown_workers)

    allowed_hosts = ["127.0.0.1", "localhost"]
    allowed_test_clients = ()
    if allow_test_client:
        allowed_hosts.append("testserver")
        allowed_test_clients = ("testclient",)

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        LoopbackOnlyMiddleware, allowed_test_clients=allowed_test_clients
    )
    app.add_middleware(SecurityHeadersMiddleware)

    auth = BridgeTokenAuth(token)
    router = APIRouter(dependencies=[Depends(auth)])

    def latest_pipeline_overlays(
        project_id: UUID,
        analysis_job_id: UUID,
    ) -> Optional[dict]:
        if app.state.candidate_evidence is None:
            return None
        for pipeline in app.state.pipeline_jobs.list(
            project_id,
            limit=128,
        ):
            selected_overlays = []
            for stage_name, overlay_field in (
                (
                    "candidate_rescan_5_10fps",
                    "candidate_evidence_overlay",
                ),
                (
                    "temporal_merge",
                    "dino_candidate_evidence_overlay",
                ),
            ):
                stage = next(
                    (
                        item
                        for item in pipeline.get("stages", ())
                        if item.get("name") == stage_name
                    ),
                    None,
                )
                checkpoint = (
                    stage.get("checkpoint")
                    if isinstance(stage, dict)
                    else None
                )
                overlay = (
                    checkpoint.get(overlay_field)
                    if isinstance(checkpoint, dict)
                    else None
                )
                if (
                    not isinstance(checkpoint, dict)
                    or checkpoint.get("analysis_job_id")
                    != str(analysis_job_id)
                    or not isinstance(overlay, dict)
                    or not isinstance(
                        overlay.get("overlay_id"), str
                    )
                    or isinstance(
                        overlay.get("candidate_count"), bool
                    )
                    or not isinstance(
                        overlay.get("candidate_count"), int
                    )
                    or overlay["candidate_count"] < 1
                ):
                    continue
                selected_overlays.append(
                    {
                        "overlay_id": overlay["overlay_id"],
                        "candidate_count": overlay[
                            "candidate_count"
                        ],
                    }
                )
            question_stage = next(
                (
                    item
                    for item in pipeline.get("stages", ())
                    if item.get("name") == "question_queue"
                ),
                None,
            )
            question_checkpoint = (
                question_stage.get("checkpoint")
                if isinstance(question_stage, dict)
                else None
            )
            user_overlays = (
                question_checkpoint.get(
                    "user_discovered_candidate_overlays", []
                )
                if isinstance(question_checkpoint, dict)
                and question_checkpoint.get("analysis_job_id")
                == str(analysis_job_id)
                else []
            )
            if isinstance(user_overlays, list):
                known_overlay_ids = {
                    item["overlay_id"]
                    for item in selected_overlays
                }
                for overlay in user_overlays:
                    if (
                        not isinstance(overlay, Mapping)
                        or not isinstance(
                            overlay.get("overlay_id"), str
                        )
                        or overlay["overlay_id"]
                        in known_overlay_ids
                        or overlay.get("candidate_count") != 1
                    ):
                        continue
                    selected_overlays.append(
                        {
                            "overlay_id": overlay["overlay_id"],
                            "candidate_count": 1,
                        }
                    )
                    known_overlay_ids.add(overlay["overlay_id"])
            if not selected_overlays:
                continue
            return {
                "pipeline_job_id": pipeline["job_id"],
                "overlays": selected_overlays,
                "candidate_count": sum(
                    item["candidate_count"]
                    for item in selected_overlays
                ),
            }
        return None

    def project_overlay_candidate(
        job_id: UUID,
        metadata: dict,
    ) -> dict:
        evidence = metadata.get("evidence_overlay")
        image = metadata.get("representative_image")
        clip = metadata.get("preview_clip")
        sources = metadata.get("proposal_sources")
        timestamp = metadata.get(
            "representative_timestamp_seconds"
        )
        score = metadata.get("score")
        if (
            metadata.get("candidate_kind")
            not in {
                "dense_candidate_evidence_overlay",
                "dino_temporal_review_evidence_overlay",
                "user_discovered",
            }
            or not isinstance(evidence, dict)
            or not isinstance(image, dict)
            or not isinstance(clip, dict)
            or not isinstance(sources, list)
            or not sources
            or not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
        ):
            raise HTTPException(
                status_code=410,
                detail=(
                    "Candidate evidence failed projection validation"
                ),
            )
        candidate_id = str(metadata["candidate_id"])

        def artifact(value: dict, suffix: str, media_type: str):
            relative_path = value.get(
                "relative_path",
                f"{candidate_id}.{suffix}",
            )
            return {
                "relative_path": relative_path,
                "size_bytes": value["size_bytes"],
                "sha256": value["sha256"],
                "media_type": media_type,
                "artifact_ref": value["artifact_ref"],
            }

        trigger_rows = [
            str(value) for value in sources[:8]
        ]
        timestamp_value = float(timestamp)
        score_value = float(score)
        return {
            "candidate_id": candidate_id,
            "start_seconds": float(
                metadata["start_seconds"]
            ),
            "end_seconds": float(metadata["end_seconds"]),
            "representative_timestamp_seconds": (
                timestamp_value
            ),
            "scene_score": score_value,
            "sharpness_score": 0.0,
            "triggers": trigger_rows,
            "observations": [
                {
                    "timestamp_seconds": timestamp_value,
                    "scene_score": score_value,
                    "sharpness_score": 0.0,
                    "triggers": trigger_rows,
                }
            ],
            "representative_image": artifact(
                image,
                "jpg",
                "image/jpeg",
            ),
            "preview_clip": artifact(
                clip,
                "mp4",
                "video/mp4",
            ),
            "preview_start_seconds": max(
                0.0,
                timestamp_value - 1.5,
            ),
            "preview_duration_seconds": 3.0,
            "ai_inference": None,
            "candidate_kind": metadata["candidate_kind"],
            "dense_proposal": {
                "issue_type": metadata["issue_type"],
                "score": score_value,
                "normalized_box": metadata.get(
                    "normalized_box"
                ),
                "ocr_text": metadata.get("ocr_text"),
                "urgent_review": bool(
                    metadata.get("urgent_review")
                ),
                "proposal_sources": trigger_rows,
            },
            "evidence_overlay": {
                "schema_version": evidence["schema_version"],
                "overlay_id": evidence["overlay_id"],
                "pipeline_job_id": evidence[
                    "pipeline_job_id"
                ],
                "manifest_sha256": evidence[
                    "manifest_sha256"
                ],
                "discovery_revision": evidence[
                    "discovery_revision"
                ],
                "dense_index_sha256": evidence[
                    "dense_index_sha256"
                ],
                "provider_provenance_sha256": evidence[
                    "provider_provenance_sha256"
                ],
                "model_provenance_sha256": evidence[
                    "model_provenance_sha256"
                ],
                "candidate_set_metadata_sha256": evidence[
                    "candidate_set_metadata_sha256"
                ],
                "review_only": True,
            },
        }

    def project_analysis_candidate_evidence(
        project_id: UUID,
        row: dict,
    ) -> dict:
        if (
            app.state.candidate_evidence is None
            or row.get("status") != "completed"
            or not isinstance(row.get("result"), dict)
        ):
            return row
        analysis_job_id = UUID(str(row["job_id"]))
        selected = latest_pipeline_overlays(
            project_id,
            analysis_job_id,
        )
        if selected is None:
            return row
        envelopes = app.state.candidate_evidence.list_candidates(
            project_id,
            analysis_job_id,
        )
        selected_overlay_ids = {
            item["overlay_id"] for item in selected["overlays"]
        }
        candidates = []
        for envelope in envelopes:
            metadata = envelope.get("metadata")
            evidence = (
                metadata.get("evidence_overlay")
                if isinstance(metadata, dict)
                else None
            )
            if (
                not isinstance(evidence, dict)
                or evidence.get("overlay_id")
                not in selected_overlay_ids
            ):
                continue
            candidates.append(
                project_overlay_candidate(
                    analysis_job_id,
                    metadata,
                )
            )
        if (
            not candidates
            or len(candidates) != selected["candidate_count"]
        ):
            raise HTTPException(
                status_code=410,
                detail=(
                    "Candidate evidence overlay failed integrity validation"
                ),
            )
        result = dict(row["result"])
        result["candidate_count"] = len(candidates)
        result["artifact_count"] = len(candidates) * 2
        result["total_artifact_bytes"] = sum(
            candidate["representative_image"]["size_bytes"]
            + candidate["preview_clip"]["size_bytes"]
            for candidate in candidates
        )
        result["candidates"] = candidates
        result["vision_inference"] = None
        return {**row, "result": result}

    def gpx_http_error(error: GpxTrackError) -> HTTPException:
        if error.code in {"track_not_found", "trip_unavailable"}:
            status_code = 404
        elif error.code in {
            "source_changed",
            "track_record_hash_mismatch",
            "track_record_invalid",
        }:
            status_code = 409
        else:
            status_code = 422
        return HTTPException(status_code=status_code, detail=str(error))

    def weak_case_http_error(error: WeakCaseCatalogError) -> HTTPException:
        if error.code in {"source_not_found", "case_not_found"}:
            status_code = 404
        elif error.code in {
            "source_sha256_mismatch",
            "source_changed",
            "image_integrity_mismatch",
            "unsafe_case_state",
        }:
            status_code = 409
        elif error.code in {
            "source_missing",
            "source_unavailable",
            "image_unavailable",
            "image_too_large",
        }:
            status_code = 503
        else:
            status_code = 422
        return HTTPException(status_code=status_code, detail=str(error))

    def weak_case_approval_http_error(
        error: WeakCaseApprovalError,
    ) -> HTTPException:
        if error.code in {"source_not_found", "case_not_found"}:
            status_code = 404
        elif error.code in {
            "document_changed",
            "case_index_changed",
            "image_changed",
            "source_sha256_mismatch",
            "source_changed",
            "image_integrity_mismatch",
            "approval_revision_conflict",
            "approval_store_corrupt",
        }:
            status_code = 409
        elif error.code in {
            "source_missing",
            "source_unavailable",
            "image_unavailable",
            "image_too_large",
        }:
            status_code = 503
        else:
            status_code = 422
        return HTTPException(status_code=status_code, detail=str(error))

    def weak_case_standardization_http_error(
        error: WeakCaseStandardizationError,
    ) -> HTTPException:
        if error.code in {"source_not_found", "case_not_found"}:
            status_code = 404
        elif error.code in {
            "document_changed",
            "case_index_changed",
            "image_changed",
            "source_sha256_mismatch",
            "source_changed",
            "image_integrity_mismatch",
            "proposal_revision_conflict",
            "proposal_store_corrupt",
        }:
            status_code = 409
        elif error.code in {
            "source_missing",
            "source_unavailable",
            "image_unavailable",
            "image_too_large",
            "provider_not_ready",
            "provider_not_installed",
            "provider_status_unavailable",
            "provider_command_failed",
            "provider_timeout",
            "provider_output_limit",
            "provider_network_unavailable",
        }:
            status_code = 503
        elif error.code in {
            "provider_not_connected",
            "provider_authentication_expired",
        }:
            status_code = 409
        else:
            status_code = 422
        return HTTPException(status_code=status_code, detail=str(error))

    def project_model_http_error(
        error: ProjectModelLifecycleError,
    ) -> HTTPException:
        if error.code in {
            "training_job_not_found",
            "model_state_not_found",
            "dataset_version_not_found",
        }:
            status_code = 404
        elif error.code in {
            "invalid_identifier",
            "invalid_hash",
            "invalid_idempotency_key",
            "invalid_required_issue_types",
            "invalid_state_revision",
            "invalid_auxiliary_reference_weight",
            "invalid_auxiliary_references",
            "training_confirmation_required",
            "training_cancellation_confirmation_required",
            "model_transition_confirmation_required",
        }:
            status_code = 422
        elif error.code in {
            "lifecycle_store_corrupt",
            "candidate_persistence_failed",
        }:
            status_code = 500
        elif error.code == "trainer_unavailable":
            status_code = 503
        else:
            status_code = 409
        return HTTPException(
            status_code=status_code,
            detail={"code": error.code, "message": str(error)},
        )

    def project_model_job_api_payload(row: dict) -> dict:
        request_payload = row["request"]
        trainer = request_payload["trainer"]
        return {
            "schema_version": row["schema_version"],
            "training_job_id": row["training_job_id"],
            "project_id": row["project_id"],
            "idempotency_key": row["idempotency_key"],
            "request_sha256": row["request_sha256"],
            "dataset_version_id": request_payload[
                "dataset_version_id"
            ],
            "dataset_hash": request_payload["dataset_hash"],
            "base_model_version": request_payload[
                "base_model_version"
            ],
            "required_issue_types": request_payload[
                "required_issue_types"
            ],
            "current_confirmed_label_count": request_payload[
                "current_confirmed_label_count"
            ],
            "approved_reference_training_member_count": (
                request_payload[
                    "approved_reference_training_member_count"
                ]
            ),
            "approved_reference_training_event_count": (
                request_payload.get(
                    "approved_reference_training_event_count",
                    request_payload[
                        "approved_reference_training_member_count"
                    ],
                )
            ),
            "training_gate_member_count": request_payload[
                "training_gate_member_count"
            ],
            "current_train_member_count": request_payload[
                "current_train_member_count"
            ],
            "current_validation_member_count": request_payload[
                "current_validation_member_count"
            ],
            "fixed_validation_sha256": request_payload[
                "fixed_validation_sha256"
            ],
            "fixed_validation_membership_sha256": request_payload[
                "fixed_validation_membership_sha256"
            ],
            "fixed_validation_set_version": request_payload.get(
                "fixed_validation_set_version",
                1,
            ),
            "fixed_validation_set_schema_version": request_payload.get(
                "fixed_validation_set_schema_version",
                "trafficvisson.project-fixed-validation.v1",
            ),
            "fixed_validation_parent_version": request_payload.get(
                "fixed_validation_parent_version"
            ),
            "fixed_validation_extension_approval_sha256": (
                request_payload.get(
                    "fixed_validation_extension_approval_sha256"
                )
            ),
            "validation_sample_counts": request_payload[
                "validation_sample_counts"
            ],
            "auxiliary_reference_count": request_payload[
                "auxiliary_reference_count"
            ],
            "approved_references_count_toward_training_gate": (
                request_payload[
                    "approved_references_count_toward_training_gate"
                ]
            ),
            "auxiliary_references_count_toward_validation": (
                request_payload[
                    "auxiliary_references_count_toward_validation"
                ]
            ),
            "trainer_id": trainer["trainer_id"],
            "trainer_version": trainer["trainer_version"],
            "trainer_available": trainer["available"],
            "trainer_test_only": trainer["test_only"],
            "status": row["status"],
            "attempt_count": row["attempt_count"],
            "blockers": row["blockers"],
            "error": row["error"],
            "progress": row.get(
                "progress",
                {
                    "stage": row["status"],
                    "attempt_number": row["attempt_count"],
                    "updated_at": row["updated_at"],
                },
            ),
            "checkpoint": row.get("checkpoint"),
            "cancel_requested": row.get(
                "cancel_requested",
                False,
            ),
            "resume_count": row.get("resume_count", 0),
            "recovery_count": row.get("recovery_count", 0),
            "model_version_id": row["model_version_id"],
            "automatic_training_started": row[
                "automatic_training_started"
            ],
            "user_confirmed_start": row["user_confirmed_start"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "idempotent_replay": row.get("idempotent_replay", False),
        }

    def project_model_version_api_payload(row: dict) -> dict:
        safe_fields = {
            "schema_version",
            "model_version_id",
            "project_id",
            "training_job_id",
            "model_version",
            "model_format",
            "base_model_version",
            "dataset_version_id",
            "dataset_hash",
            "fixed_validation_sha256",
            "fixed_validation_membership_sha256",
            "fixed_validation_set_version",
            "training_analysis_job_ids",
            "fixed_validation_analysis_job_ids",
            "source_analysis_lineage_sha256",
            "primary_artifact_sha256",
            "artifact_manifest_sha256",
            "artifact_file_count",
            "artifact_total_size_bytes",
            "trainer_id",
            "trainer_version",
            "trainer_test_only",
            "promotion_review",
            "validation_decision",
            "auxiliary_reference_count",
            "auxiliary_reference_manifest_sha256",
            "state",
            "automatic_promotion",
            "automatic_rejection",
            "user_promotion_required",
            "created_at",
            "updated_at",
        }
        return {
            field: (
                row.get(field, 1)
                if field == "fixed_validation_set_version"
                else row[field]
            )
            for field in safe_fields
        }

    def project_model_transition_api_payload(row: dict) -> dict:
        return {
            "schema_version": row["schema_version"],
            "event_id": row["event_id"],
            "project_id": row["project_id"],
            "action": row["action"],
            "target_model_id": row["target_model_id"],
            "request_sha256": row["request_sha256"],
            "user_confirmed": row["user_confirmed"],
            "automatic_transition": row["automatic_transition"],
            "before_state": row["before_state"],
            "after_state": row["after_state"],
            "created_at": row["created_at"],
            "idempotent_replay": row.get("idempotent_replay", False),
        }

    def project_fixed_validation_api_payload(row: Mapping[str, Any]) -> dict:
        """Remove validation truth/evidence while preserving immutable lineage."""

        approval = row.get("extension_approval")
        added_jobs = (
            approval.get("added_validation_analysis_job_ids", ())
            if isinstance(approval, Mapping)
            else ()
        )
        return {
            "schema_version": row["schema_version"],
            "project_id": row["project_id"],
            "validation_set_version": row.get(
                "validation_set_version",
                1,
            ),
            "parent_validation_set_version": row.get(
                "parent_validation_set_version"
            ),
            "parent_membership_sha256": row.get(
                "parent_membership_sha256"
            ),
            "parent_fixed_validation_sha256": row.get(
                "parent_fixed_validation_sha256"
            ),
            "origin_dataset_version_id": row[
                "origin_dataset_version_id"
            ],
            "required_issue_types": row["required_issue_types"],
            "added_issue_types": row.get(
                "added_issue_types",
                row["required_issue_types"],
            ),
            "added_validation_analysis_job_ids": list(added_jobs),
            "validation_sample_counts": row[
                "validation_sample_counts"
            ],
            "extension_positive_sample_counts": row.get(
                "extension_positive_sample_counts",
                {},
            ),
            "membership_sha256": row["membership_sha256"],
            "fixed_validation_sha256": row[
                "fixed_validation_sha256"
            ],
            "extension_approval_sha256": row.get(
                "extension_approval_sha256"
            ),
            "minimum_positive_samples_per_type": 30,
            "persisted": row["persisted"],
            "created_at": row["created_at"],
        }

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="trafficvisson-bridge",
            api_version="1.0",
            fixture_mode=fixture_mode,
        )

    @router.get(
        "/source-candidates",
        response_model=SourceCandidateCatalogResponse,
    )
    async def source_candidates() -> SourceCandidateCatalogResponse:
        return SourceCandidateCatalogResponse(
            **build_source_candidate_catalog(
                app.state.source_candidate_probe.snapshot()
            )
        )

    @router.get("/projects", response_model=ProjectListResponse)
    async def list_projects() -> ProjectListResponse:
        return ProjectListResponse(projects=app.state.store.list_projects())

    @router.post("/projects", response_model=ProjectResponse, status_code=201)
    async def create_project(request: ProjectCreateRequest) -> ProjectResponse:
        return ProjectResponse(**app.state.store.create_project(request))

    @router.get("/projects/{project_id}", response_model=ProjectResponse)
    async def get_project(project_id: UUID) -> ProjectResponse:
        return ProjectResponse(**app.state.store.get_project(project_id))

    @router.get(
        "/projects/{project_id}/reference-cases/sources",
        response_model=WeakCaseSourceListResponse,
    )
    async def list_weak_case_sources(
        project_id: UUID,
    ) -> WeakCaseSourceListResponse:
        app.state.store.get_project(project_id)
        result = await run_in_threadpool(
            app.state.weak_case_catalog.list_sources
        )
        decorated = await run_in_threadpool(
            app.state.weak_case_approvals.decorate_source_list,
            project_id,
            result,
        )
        return WeakCaseSourceListResponse(**decorated)

    @router.post(
        "/projects/{project_id}/reference-cases/search",
        response_model=WeakCaseSearchResponse,
    )
    async def search_weak_cases(
        project_id: UUID,
        request: WeakCaseSearchRequest,
    ) -> WeakCaseSearchResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            result = await run_in_threadpool(
                app.state.weak_case_catalog.search,
                issue_type=request.issue_type,
                query_text=request.query_text,
                limit=request.limit,
            )
        except WeakCaseCatalogError as error:
            raise weak_case_http_error(error) from error
        decorated = await run_in_threadpool(
            app.state.weak_case_approvals.decorate_search,
            project_id,
            result,
        )
        return WeakCaseSearchResponse(**decorated)

    @router.post(
        (
            "/projects/{project_id}/reference-cases/"
            "standardization-proposals"
        ),
        response_model=WeakCaseStandardizationProposalResponse,
    )
    async def propose_weak_case_standardization(
        project_id: UUID,
        request: WeakCaseStandardizationProposalRequest,
    ) -> WeakCaseStandardizationProposalResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            result = await run_in_threadpool(
                app.state.weak_case_standardization.propose,
                project_id,
                request.model_dump(mode="json"),
            )
        except WeakCaseStandardizationError as error:
            raise weak_case_standardization_http_error(
                error
            ) from error
        return WeakCaseStandardizationProposalResponse(**result)

    @router.get(
        (
            "/projects/{project_id}/reference-cases/"
            "standardization-proposals"
        ),
        response_model=WeakCaseStandardizationProposalListResponse,
    )
    async def list_weak_case_standardization_proposals(
        project_id: UUID,
    ) -> WeakCaseStandardizationProposalListResponse:
        app.state.store.get_project(project_id)
        try:
            result = await run_in_threadpool(
                app.state.weak_case_standardization.list_project,
                project_id,
            )
        except WeakCaseStandardizationError as error:
            raise weak_case_standardization_http_error(
                error
            ) from error
        return WeakCaseStandardizationProposalListResponse(**result)

    @router.post(
        "/projects/{project_id}/reference-cases/approvals",
        response_model=WeakCaseApprovalResponse,
    )
    async def approve_weak_case(
        project_id: UUID,
        request: WeakCaseApprovalRequest,
    ) -> WeakCaseApprovalResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            result = await run_in_threadpool(
                app.state.weak_case_approvals.approve,
                project_id,
                request.model_dump(mode="json"),
            )
        except WeakCaseApprovalError as error:
            raise weak_case_approval_http_error(error) from error
        return WeakCaseApprovalResponse(**result)

    @router.get(
        "/projects/{project_id}/reference-cases/approved",
        response_model=ApprovedReferenceListResponse,
    )
    async def list_approved_weak_cases(
        project_id: UUID,
    ) -> ApprovedReferenceListResponse:
        app.state.store.get_project(project_id)
        try:
            result = await run_in_threadpool(
                app.state.weak_case_approvals.list_project,
                project_id,
            )
        except WeakCaseApprovalError as error:
            raise weak_case_approval_http_error(error) from error
        return ApprovedReferenceListResponse(**result)

    @router.get(
        (
            "/projects/{project_id}/reference-cases/sources/"
            "{source_id}/cases/{case_id}/image"
        )
    )
    async def weak_case_image(
        project_id: UUID,
        source_id: str,
        case_id: str,
    ) -> Response:
        app.state.store.get_project(project_id)
        try:
            image = await run_in_threadpool(
                app.state.weak_case_catalog.resolve_case_preview,
                source_id,
                case_id,
            )
        except WeakCaseCatalogError as error:
            raise weak_case_http_error(error) from error
        return Response(
            content=image.body,
            media_type=image.media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Length": str(image.size_bytes),
                "Content-Security-Policy": "default-src 'none'",
                "ETag": f'"sha256-{image.sha256}"',
                "Referrer-Policy": "no-referrer",
                "X-TrafficVisson-Derived-Thumbnail": (
                    "1" if image.derived_thumbnail else "0"
                ),
                "X-TrafficVisson-Source-SHA256": (
                    image.source_sha256 or image.sha256
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post(
        "/projects/{project_id}/imports/inspect",
        response_model=ImportInspectResponse,
    )
    async def inspect_import(
        project_id: UUID, request: ImportInspectRequest
    ) -> ImportInspectResponse:
        return ImportInspectResponse(
            **app.state.store.inspect_import(project_id, request)
        )

    @router.post(
        "/projects/{project_id}/imports",
        response_model=ImportCreateResponse,
        status_code=202,
    )
    async def create_import(
        project_id: UUID, request: ImportCreateRequest
    ) -> ImportCreateResponse:
        return ImportCreateResponse(
            **app.state.store.create_import(project_id, request)
        )

    @router.post(
        "/projects/{project_id}/media/snapshots",
        response_model=MediaSnapshotResponse,
        status_code=201,
    )
    def capture_media_snapshot(
        project_id: UUID, request: MediaSnapshotRequest
    ) -> MediaSnapshotResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        payload = app.state.media_intake.capture_snapshot(
            project_id, request.directory
        )
        payload["files"] = [
            {
                **item,
                "modified_time_ns": str(item["modified_time_ns"]),
            }
            for item in payload["files"]
        ]
        return MediaSnapshotResponse(
            **payload
        )

    @router.post(
        "/projects/{project_id}/media/local-file",
        response_model=MediaSnapshotResponse,
        status_code=201,
    )
    def register_local_media_file(
        project_id: UUID, request: MediaLocalFileRegisterRequest
    ) -> MediaSnapshotResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        payload = app.state.media_intake.register_local_file(
            project_id, request.selected_path
        )
        payload["files"] = [
            {
                **item,
                "modified_time_ns": str(item["modified_time_ns"]),
            }
            for item in payload["files"]
        ]
        return MediaSnapshotResponse(
            **payload
        )

    @router.post(
        "/projects/{project_id}/media/stability",
        response_model=MediaStabilityResponse,
    )
    def compare_media_snapshots(
        project_id: UUID, request: MediaStabilityRequest
    ) -> MediaStabilityResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        result = app.state.media_intake.compare_snapshots(
            project_id, request.snapshot_ids
        )
        stable_snapshot_id = result.get("stable_snapshot_id")
        if result.get("stable") is True and stable_snapshot_id is not None:
            app.state.stable_media_snapshots.persist(
                app.state.media_intake,
                project_id,
                UUID(str(stable_snapshot_id)),
            )
        return MediaStabilityResponse(**result)

    @router.post(
        "/projects/{project_id}/media/probe",
        response_model=MediaProbeResponse,
    )
    def probe_media(
        project_id: UUID, request: MediaProbeRequest
    ) -> MediaProbeResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        return MediaProbeResponse(
            **app.state.media_intake.probe(
                project_id,
                request.snapshot_id,
                request.file_name,
                request.timeout_seconds,
            )
        )

    @router.post(
        "/projects/{project_id}/media/hash-jobs",
        response_model=MediaHashJobResponse,
        status_code=202,
    )
    def start_media_hash_job(
        project_id: UUID, request: MediaHashStartRequest
    ) -> MediaHashJobResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        return MediaHashJobResponse(
            **app.state.media_intake.start_hash_job(
                project_id, request.snapshot_id, request.file_name
            )
        )

    @router.get(
        "/projects/{project_id}/media/hash-jobs/{job_id}",
        response_model=MediaHashJobResponse,
    )
    def get_media_hash_job(
        project_id: UUID, job_id: UUID
    ) -> MediaHashJobResponse:
        app.state.store.get_project(project_id)
        return MediaHashJobResponse(
            **app.state.media_intake.get_hash_job(project_id, job_id)
        )

    @router.post(
        "/projects/{project_id}/media/hash-jobs/{job_id}/cancel",
        response_model=MediaHashJobResponse,
    )
    def cancel_media_hash_job(
        project_id: UUID, job_id: UUID, _request: EmptyRequest
    ) -> MediaHashJobResponse:
        app.state.store.get_project(project_id)
        return MediaHashJobResponse(
            **app.state.media_intake.cancel_hash_job(project_id, job_id)
        )

    @router.post(
        "/projects/{project_id}/analysis/jobs",
        response_model=AnalysisJobResponse,
        status_code=202,
    )
    def start_analysis_job(
        project_id: UUID, request: AnalysisJobStartRequest
    ) -> AnalysisJobResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        return AnalysisJobResponse(
            **app.state.analysis_jobs.start(
                project_id,
                request.snapshot_id,
                request.file_name,
                request.hash_job_id,
                request.sha256_lower,
            )
        )

    @router.get(
        "/projects/{project_id}/analysis/jobs",
        response_model=AnalysisJobListResponse,
    )
    def list_analysis_jobs(
        project_id: UUID,
        limit: int = Query(default=100, ge=1, le=128),
    ) -> AnalysisJobListResponse:
        app.state.store.get_project(project_id)
        rows = app.state.analysis_jobs.list(
            project_id,
            limit=limit,
        )
        return AnalysisJobListResponse(
            jobs=[
                project_analysis_candidate_evidence(
                    project_id,
                    row,
                )
                for row in rows
            ]
        )

    @router.get(
        "/projects/{project_id}/analysis/jobs/{job_id}",
        response_model=AnalysisJobResponse,
    )
    def get_analysis_job(
        project_id: UUID, job_id: UUID
    ) -> AnalysisJobResponse:
        app.state.store.get_project(project_id)
        return AnalysisJobResponse(
            **project_analysis_candidate_evidence(
                project_id,
                app.state.analysis_jobs.get(
                    project_id,
                    job_id,
                ),
            )
        )

    @router.get(
        (
            "/projects/{project_id}/analysis/jobs/{job_id}/"
            "candidates/{candidate_id}/{kind}"
        ),
        response_class=FileResponse,
    )
    def get_analysis_candidate_artifact(
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        kind: str,
    ) -> FileResponse:
        app.state.store.get_project(project_id)
        evidence_backend = (
            app.state.candidate_evidence
            if app.state.candidate_evidence is not None
            else app.state.analysis_jobs
        )
        artifact = evidence_backend.resolve_artifact(
            project_id, job_id, candidate_id, kind
        )
        return FileResponse(
            artifact.path,
            media_type=artifact.media_type,
            headers={
                "ETag": f'"{artifact.sha256}"',
                "X-TrafficSuperVision-Artifact-SHA256": artifact.sha256,
            },
        )

    @router.post(
        "/projects/{project_id}/analysis/jobs/{job_id}/cancel",
        response_model=AnalysisJobResponse,
    )
    def cancel_analysis_job(
        project_id: UUID, job_id: UUID, _request: EmptyRequest
    ) -> AnalysisJobResponse:
        app.state.store.get_project(project_id)
        return AnalysisJobResponse(
            **app.state.analysis_jobs.cancel(project_id, job_id)
        )

    @router.post(
        "/projects/{project_id}/analysis/jobs/{job_id}/pause",
        response_model=AnalysisJobResponse,
    )
    def pause_analysis_job(
        project_id: UUID, job_id: UUID, _request: EmptyRequest
    ) -> AnalysisJobResponse:
        app.state.store.get_project(project_id)
        app.state.analysis_jobs.pause(project_id, job_id)
        raise AssertionError("pause always returns an HTTP error")

    def dense_index_http_error(
        error: Exception,
    ) -> HTTPException:
        if isinstance(error, DenseIndexJobNotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, DenseIndexJobConflict):
            return HTTPException(status_code=409, detail=str(error))
        if isinstance(error, DenseIndexJobSourceRejected):
            return HTTPException(
                status_code=409,
                detail=(
                    "source_unavailable_reselect_required: 원본 영상을 "
                    "다시 선택하고 해시 검증을 완료해 주세요."
                ),
            )
        if isinstance(error, DenseIndexJobUnavailable):
            return HTTPException(status_code=503, detail=str(error))
        return HTTPException(
            status_code=422,
            detail="The dense-index request is invalid",
        )

    @router.post(
        "/projects/{project_id}/dense-index/jobs",
        response_model=DenseIndexJobResponse,
        status_code=202,
    )
    def start_dense_index_job(
        project_id: UUID,
        request: DenseIndexJobStartRequest,
    ) -> DenseIndexJobResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        analysis = app.state.analysis_jobs.get(
            project_id,
            request.analysis_job_id,
        )
        if analysis["status"] != "completed" or analysis.get("result") is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Dense-frame indexing requires a completed analysis "
                    "from the current runtime"
                ),
            )
        try:
            row = app.state.dense_index_jobs.start(
                project_id,
                str(request.analysis_job_id),
                analysis["source_sha256"],
                sampling_fps=request.sampling_fps,
            )
        except (
            DenseIndexJobConflict,
            DenseIndexJobNotFound,
            DenseIndexJobSourceRejected,
            DenseIndexJobUnavailable,
            ValueError,
        ) as error:
            raise dense_index_http_error(error) from error
        return _dense_index_job_response(row)

    @router.get(
        "/projects/{project_id}/dense-index/jobs",
        response_model=DenseIndexJobListResponse,
    )
    def list_dense_index_jobs(
        project_id: UUID,
        limit: int = Query(default=100, ge=1, le=128),
    ) -> DenseIndexJobListResponse:
        app.state.store.get_project(project_id)
        try:
            rows = app.state.dense_index_jobs.list(
                project_id,
                limit=limit,
            )
        except (
            DenseIndexJobUnavailable,
            ValueError,
        ) as error:
            raise dense_index_http_error(error) from error
        return DenseIndexJobListResponse(
            jobs=[_dense_index_job_response(row) for row in rows]
        )

    @router.get(
        "/projects/{project_id}/dense-index/jobs/{job_id}",
        response_model=DenseIndexJobResponse,
    )
    def get_dense_index_job(
        project_id: UUID,
        job_id: UUID,
    ) -> DenseIndexJobResponse:
        app.state.store.get_project(project_id)
        try:
            row = app.state.dense_index_jobs.get(project_id, job_id)
        except (
            DenseIndexJobNotFound,
            DenseIndexJobUnavailable,
            ValueError,
        ) as error:
            raise dense_index_http_error(error) from error
        return _dense_index_job_response(row)

    @router.post(
        "/projects/{project_id}/dense-index/jobs/{job_id}/cancel",
        response_model=DenseIndexJobResponse,
    )
    def cancel_dense_index_job(
        project_id: UUID,
        job_id: UUID,
        _request: EmptyRequest,
    ) -> DenseIndexJobResponse:
        app.state.store.get_project(project_id)
        try:
            row = app.state.dense_index_jobs.cancel(project_id, job_id)
        except (
            DenseIndexJobNotFound,
            DenseIndexJobUnavailable,
            ValueError,
        ) as error:
            raise dense_index_http_error(error) from error
        return _dense_index_job_response(row)

    @router.get(
        (
            "/projects/{project_id}/dense-index/jobs/{job_id}/"
            "artifacts/{artifact_id}"
        ),
        response_class=FileResponse,
    )
    def get_dense_index_artifact(
        project_id: UUID,
        job_id: UUID,
        artifact_id: str,
    ) -> FileResponse:
        app.state.store.get_project(project_id)
        try:
            artifact = app.state.dense_index_jobs.resolve_artifact(
                project_id,
                job_id,
                artifact_id,
            )
        except DenseIndexJobUnavailable as error:
            raise HTTPException(
                status_code=410,
                detail=str(error),
            ) from error
        except (
            DenseIndexJobConflict,
            DenseIndexJobNotFound,
            ValueError,
        ) as error:
            raise dense_index_http_error(error) from error
        return FileResponse(
            artifact.path,
            media_type=artifact.media_type,
            headers={
                "Cache-Control": "no-store",
                "ETag": f'"{artifact.sha256}"',
                "X-TrafficSuperVision-Artifact-SHA256": artifact.sha256,
            },
        )

    def pipeline_http_error(error: Exception) -> HTTPException:
        if isinstance(error, PipelineJobNotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, PipelineProfileMissing):
            return HTTPException(
                status_code=409,
                detail=(
                    "blocked_configuration_required: 프로젝트의 판정 "
                    "프로필, 질문 정책, 기본 양식, 모델팩, Provider와 "
                    "출력 위치를 먼저 설정해 주세요."
                ),
            )
        if isinstance(error, PipelineJobConflict):
            return HTTPException(status_code=409, detail=str(error))
        if isinstance(error, PipelineJobUnavailable):
            return HTTPException(status_code=503, detail=str(error))
        return HTTPException(
            status_code=422,
            detail="The pipeline request is invalid",
        )

    def user_discovered_http_error(
        error: Exception,
    ) -> HTTPException:
        code = getattr(error, "code", None)
        if isinstance(error, PipelineJobNotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, PipelineJobConflict) or code in {
            "independent_survey_scope_mismatch",
            "independent_survey_revision_conflict",
            "model_source_video_reuse_forbidden",
            "model_source_lineage_tampered",
            "dense_index_hash_mismatch",
            "dense_frame_integrity_failed",
            "user_discovered_candidate_already_queued",
            "user_discovered_review_queue_conflict",
        }:
            return HTTPException(
                status_code=409,
                detail=code or str(error),
            )
        if isinstance(
            error, (PipelineJobUnavailable,)
        ) or code in {
            "independent_survey_unavailable",
            "user_discovered_evidence_materialization_failed",
            "user_discovered_review_queue_materialization_invalid",
        }:
            return HTTPException(
                status_code=503,
                detail=code or str(error),
            )
        return HTTPException(
            status_code=422,
            detail=code or "user_discovered_candidate_invalid",
        )

    def folder_batch_http_error(error: Exception) -> HTTPException:
        if isinstance(error, FolderBatchNotFound):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, FolderBatchConflict):
            return HTTPException(status_code=409, detail=str(error))
        if isinstance(error, FolderBatchUnavailable):
            return HTTPException(status_code=503, detail=str(error))
        if isinstance(error, PipelineProfileMissing):
            return pipeline_http_error(error)
        return HTTPException(
            status_code=422,
            detail="The folder pipeline batch request is invalid",
        )

    def provider_visual_evidence_http_error(
        error: ProviderVisualEvidenceError,
    ) -> HTTPException:
        code = error.code
        if code in {
            "visual_evidence_preparation_not_found",
            "visual_evidence_artifact_not_found",
        }:
            status_code = 404
        elif code in {
            "awaiting_deidentification_review",
            "privacy_review_already_recorded",
            "privacy_review_preparation_changed",
            "visual_evidence_preparation_changed",
            "visual_evidence_source_changed",
            "visual_evidence_reference_changed",
        }:
            status_code = 409
        elif code in {
            "privacy_redaction_runtime_unavailable",
            "visual_evidence_output_unavailable",
            "visual_evidence_record_unavailable",
            "visual_evidence_source_unavailable",
            "visual_evidence_reference_unavailable",
            "visual_evidence_storage_unavailable",
        }:
            status_code = 503
        else:
            status_code = 422
        return HTTPException(status_code=status_code, detail=code)

    def provider_visual_evidence_backend(
    ) -> ProviderVisualEvidencePreparationService:
        service = app.state.provider_visual_evidence
        if service is None:
            detail = (
                app.state.provider_visual_evidence_startup_error_code
                or "provider_visual_evidence_unavailable"
            )
            raise HTTPException(status_code=503, detail=detail)
        return service

    def report_deidentified_evidence_http_error(
        error: ReportDeidentifiedEvidenceError,
    ) -> HTTPException:
        code = error.code
        if code in {
            "report_deidentification_not_found",
        }:
            status_code = 404
        elif code in {
            "report_deidentification_source_changed",
            "report_deidentification_output_changed",
            "report_deidentification_preparation_changed",
            "report_deidentification_review_already_recorded",
            "report_deidentification_scope_conflict",
            "report_deidentification_provenance_conflict",
            "report_deidentification_not_verified",
        }:
            status_code = 409
        elif code in {
            "report_deidentification_storage_unavailable",
            "report_deidentification_record_unavailable",
            "report_deidentification_source_unavailable",
            "report_deidentification_output_unavailable",
            "report_privacy_detector_unavailable",
            "report_privacy_detection_failed",
            "report_video_frame_extraction_unavailable",
        }:
            status_code = 503
        else:
            status_code = 422
        return HTTPException(status_code=status_code, detail=code)

    def report_deidentified_evidence_backend(
    ) -> ReportDeidentifiedEvidenceService:
        service = app.state.report_deidentified_evidence
        if service is None:
            detail = (
                app.state
                .report_deidentified_evidence_startup_error_code
                or "report_deidentified_evidence_unavailable"
            )
            raise HTTPException(status_code=503, detail=detail)
        return service

    def report_deidentification_job_context(
        project_id: UUID,
        job_id: UUID,
        *,
        require_blocked_boundary: bool,
    ) -> tuple[dict[str, Any], UUID, str, tuple[str, ...]]:
        try:
            job = app.state.pipeline_jobs.get(project_id, job_id)
        except (
            PipelineJobNotFound,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        profile = job.get("profile_snapshot")
        if (
            not isinstance(profile, Mapping)
            or profile.get("report_evidence_mode")
            != "shared_deidentified"
        ):
            raise HTTPException(
                status_code=409,
                detail="shared_deidentified_report_not_configured",
            )
        if require_blocked_boundary and (
            job.get("status") != "blocked"
            or job.get("current_stage") != "derived_outputs"
            or job.get("outcome_code") != "report_preview_blocked"
            or job.get("error_code")
            != "deidentified_evidence_required"
        ):
            raise HTTPException(
                status_code=409,
                detail="report_deidentification_review_not_required",
            )
        stages = job.get("stages")
        if not isinstance(stages, list):
            raise HTTPException(
                status_code=409,
                detail="report_deidentification_pipeline_invalid",
            )
        question_stage = next(
            (
                stage
                for stage in stages
                if isinstance(stage, Mapping)
                and stage.get("name") == "question_queue"
            ),
            None,
        )
        question = (
            question_stage.get("checkpoint")
            if isinstance(question_stage, Mapping)
            else None
        )
        analysis_text = (
            question.get("analysis_job_id")
            if isinstance(question, Mapping)
            else None
        )
        source_sha256 = (
            question.get("source_sha256")
            if isinstance(question, Mapping)
            else None
        )
        candidate_ids = (
            question.get("question_candidate_ids")
            if isinstance(question, Mapping)
            else None
        )
        try:
            analysis_job_id = UUID(str(analysis_text))
        except (TypeError, ValueError, AttributeError) as error:
            raise HTTPException(
                status_code=409,
                detail="report_deidentification_pipeline_invalid",
            ) from error
        if (
            str(analysis_job_id) != analysis_text
            or not isinstance(source_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", source_sha256) is None
            or not isinstance(candidate_ids, list)
            or not candidate_ids
            or len(candidate_ids) != len(set(candidate_ids))
            or any(
                not isinstance(candidate_id, str)
                or re.fullmatch(r"[a-f0-9]{20}", candidate_id)
                is None
                for candidate_id in candidate_ids
            )
            or not isinstance(job.get("review_completion_id"), str)
        ):
            raise HTTPException(
                status_code=409,
                detail="report_deidentification_pipeline_invalid",
            )
        if require_blocked_boundary:
            derived_stage = next(
                (
                    stage
                    for stage in stages
                    if isinstance(stage, Mapping)
                    and stage.get("name") == "derived_outputs"
                ),
                None,
            )
            derived_checkpoint = (
                derived_stage.get("checkpoint")
                if isinstance(derived_stage, Mapping)
                else None
            )
            report = (
                derived_checkpoint.get("report")
                if isinstance(derived_checkpoint, Mapping)
                else None
            )
            reasons = (
                report.get("blocking_reasons")
                if isinstance(report, Mapping)
                else None
            )
            if reasons != ["deidentified_evidence_required"]:
                raise HTTPException(
                    status_code=409,
                    detail="report_deidentification_pipeline_invalid",
                )
        return (
            job,
            analysis_job_id,
            source_sha256,
            tuple(candidate_ids),
        )

    async def current_report_deidentification_sources(
        project_id: UUID,
        job_id: UUID,
        *,
        require_blocked_boundary: bool,
    ) -> tuple[
        dict[str, Any],
        UUID,
        str,
        tuple[ReportEvidenceSource, ...],
    ]:
        (
            job,
            analysis_job_id,
            source_sha256,
            candidate_ids,
        ) = report_deidentification_job_context(
            project_id,
            job_id,
            require_blocked_boundary=require_blocked_boundary,
        )
        await reconcile_normalized_labels(project_id)
        incidents = await run_in_threadpool(
            app.state.store.list_incidents,
            project_id,
            include_inactive=False,
            analysis_job_id=analysis_job_id,
        )
        latest_answers = await run_in_threadpool(
            app.state.candidate_labels.latest_answers,
            project_id,
            analysis_job_id=analysis_job_id,
        )
        latest_by_candidate = {
            str(record.get("candidate_id")): record
            for record in latest_answers
            if isinstance(record, Mapping)
        }
        candidate_set = set(candidate_ids)
        if not incidents:
            raise HTTPException(
                status_code=409,
                detail="report_deidentification_incidents_missing",
            )
        evidence_backend = (
            app.state.candidate_evidence
            if app.state.candidate_evidence is not None
            else app.state.analysis_jobs
        )
        sources = []
        seen_incidents = set()
        for incident in incidents:
            incident_id = str(incident.get("incident_id", ""))
            candidate_id = str(incident.get("candidate_id", ""))
            record = latest_by_candidate.get(candidate_id)
            final_answer = (
                record.get("final_answer")
                if isinstance(record, Mapping)
                else None
            )
            try:
                canonical_incident = str(UUID(incident_id))
            except (TypeError, ValueError, AttributeError):
                canonical_incident = ""
            if (
                canonical_incident != incident_id
                or incident_id in seen_incidents
                or candidate_id not in candidate_set
                or incident.get("project_id") != str(project_id)
                or incident.get("analysis_job_id")
                != str(analysis_job_id)
                or incident.get("status") != "active"
                or not isinstance(record, Mapping)
                or record.get("partition") != "approved"
                or not isinstance(final_answer, Mapping)
                or final_answer.get("answer") not in {"yes", "edit"}
                or final_answer.get("answered_by") != "local_user"
                or incident.get("source_answer_record_id")
                != record.get("record_id")
                or incident.get("source_record_hash")
                != record.get("record_hash")
            ):
                raise HTTPException(
                    status_code=409,
                    detail="report_deidentification_incident_provenance_conflict",
                )
            seen_incidents.add(incident_id)
            try:
                artifact = await run_in_threadpool(
                    evidence_backend.resolve_artifact,
                    project_id,
                    analysis_job_id,
                    candidate_id,
                    "image",
                )
            except HTTPException as error:
                raise HTTPException(
                    status_code=409,
                    detail="report_deidentification_source_unavailable",
                ) from error
            if (
                artifact.media_type != "image/jpeg"
                or not Path(artifact.path).is_absolute()
                or not isinstance(artifact.size_bytes, int)
                or not isinstance(artifact.sha256, str)
                or re.fullmatch(r"[a-f0-9]{64}", artifact.sha256)
                is None
            ):
                raise HTTPException(
                    status_code=409,
                    detail="report_deidentification_source_invalid",
                )
            sources.append(
                ReportEvidenceSource(
                    project_id=project_id,
                    pipeline_job_id=job_id,
                    analysis_job_id=analysis_job_id,
                    incident_id=incident_id,
                    candidate_id=candidate_id,
                    source_sha256=source_sha256,
                    original_ref=(
                        f"analysis:{candidate_id}:representative"
                    ),
                    path=Path(artifact.path),
                    original_sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                    media_type=artifact.media_type,
                    time_ms=int(
                        incident.get("representative_time_ms", 0)
                    ),
                )
            )
        derived_stage = next(
            (
                stage
                for stage in job.get("stages", ())
                if isinstance(stage, Mapping)
                and stage.get("name") == "derived_outputs"
            ),
            None,
        )
        derived_metrics = (
            derived_stage.get("metrics")
            if isinstance(derived_stage, Mapping)
            else None
        )
        if require_blocked_boundary:
            derived_incident_count = (
                derived_metrics.get("incident_count")
                if isinstance(derived_metrics, Mapping)
                else None
            )
            if (
                not isinstance(derived_incident_count, int)
                or isinstance(derived_incident_count, bool)
                or derived_incident_count != len(sources)
            ):
                raise HTTPException(
                    status_code=409,
                    detail="report_deidentification_incident_set_changed",
                )
        return (
            job,
            analysis_job_id,
            source_sha256,
            tuple(
                sorted(
                    sources,
                    key=lambda item: item.incident_id,
                )
            ),
        )

    @router.post(
        "/projects/{project_id}/output-locations",
        status_code=201,
    )
    def register_project_output_location(
        project_id: UUID,
        request: ProjectOutputLocationRegisterRequest,
    ) -> dict:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            location = app.state.project_artifact_roots.register(
                project_id,
                selected_directory=Path(
                    request.selected_directory
                ),
            )
        except ProjectArtifactRootConflict as error:
            raise HTTPException(
                status_code=409,
                detail="project_output_location_conflict",
            ) from error
        except ProjectArtifactRootUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail="project_output_location_unavailable",
            ) from error
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="project_output_location_invalid",
            ) from error
        return {
            "schema_version": (
                "trafficvisson.project-output-location.v1"
            ),
            "location": location,
        }

    @router.get(
        "/projects/{project_id}/output-locations",
    )
    def list_project_output_locations(
        project_id: UUID,
    ) -> dict:
        app.state.store.get_project(project_id)
        try:
            locations = app.state.project_artifact_roots.list(
                project_id
            )
        except ProjectArtifactRootUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail="project_output_locations_unavailable",
            ) from error
        return {
            "schema_version": (
                "trafficvisson.project-output-location-list.v1"
            ),
            "locations": locations,
            "path_exposed": False,
        }

    @router.put(
        "/projects/{project_id}/pipeline/profile",
        response_model=PipelineProfileResponse,
    )
    def configure_pipeline_profile(
        project_id: UUID,
        request: PipelineProfileConfigureRequest,
    ) -> PipelineProfileResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            row = app.state.pipeline_jobs.configure_profile(
                project_id,
                question_policy=request.question_policy,
                review_mode=request.review_mode,
                label_confirmation_authority=(
                    request.label_confirmation_authority
                ),
                default_template_id=request.default_template_id,
                default_template_sha256=request.default_template_sha256,
                detector_pack_id=request.detector_pack_id,
                ocr_pack_id=request.ocr_pack_id,
                sam2_pack_id=request.sam2_pack_id,
                report_provider=request.report_provider,
                report_transmission_consent=(
                    request.report_transmission_consent
                ),
                template_semantic_map_id=(
                    request.template_semantic_map_id
                ),
                report_route_name=request.report_route_name,
                report_inspector_name=(
                    request.report_inspector_name
                ),
                report_evidence_mode=request.report_evidence_mode,
                report_inspection_date_policy=(
                    request.report_inspection_date_policy
                ),
                output_location_id=request.output_location_id,
                candidate_rescan_fps=request.candidate_rescan_fps,
            )
        except (
            PipelineJobConflict,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        return PipelineProfileResponse(**row)

    @router.get(
        "/projects/{project_id}/pipeline/profile",
        response_model=PipelineProfileResponse,
    )
    def get_pipeline_profile(
        project_id: UUID,
    ) -> PipelineProfileResponse:
        app.state.store.get_project(project_id)
        try:
            row = app.state.pipeline_jobs.get_profile(project_id)
        except (
            PipelineProfileMissing,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        return PipelineProfileResponse(**row)

    @router.post(
        "/projects/{project_id}/pipeline/jobs",
        response_model=PipelineJobStartResponse,
        status_code=202,
    )
    def start_pipeline_job(
        project_id: UUID,
        request: PipelineJobStartRequest,
    ) -> PipelineJobStartResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            row, replayed = app.state.pipeline_jobs.start(
                project_id,
                snapshot_id=request.snapshot_id,
                file_name=request.file_name,
                rights_confirmed=request.rights_confirmed,
                idempotency_key=request.idempotency_key,
            )
        except (
            PipelineJobConflict,
            PipelineJobUnavailable,
            PipelineProfileMissing,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        return PipelineJobStartResponse(
            job=PipelineJobResponse(**row),
            idempotent_replay=replayed,
        )

    @router.post(
        "/projects/{project_id}/pipeline/jobs/{source_job_id}/reruns",
        response_model=PipelineJobStartResponse,
        status_code=202,
    )
    def rerun_pipeline_job_with_current_profile(
        project_id: UUID,
        source_job_id: UUID,
        request: PipelineJobRerunRequest,
    ) -> PipelineJobStartResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        if request.source_job_id != source_job_id:
            raise HTTPException(
                status_code=409,
                detail="Source pipeline identity does not match the route",
            )
        try:
            row, replayed = (
                app.state.pipeline_jobs.rerun_with_current_profile(
                    project_id,
                    source_job_id,
                    expected_source_record_sha256=(
                        request.expected_source_record_sha256
                    ),
                    expected_profile_revision=(
                        request.expected_profile_revision
                    ),
                    rights_confirmed=request.rights_confirmed,
                    user_confirmed=request.user_confirmed,
                    idempotency_key=request.idempotency_key,
                )
            )
        except (
            PipelineJobConflict,
            PipelineJobNotFound,
            PipelineJobUnavailable,
            PipelineProfileMissing,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        return PipelineJobStartResponse(
            job=PipelineJobResponse(**row),
            idempotent_replay=replayed,
        )

    @router.post(
        "/projects/{project_id}/pipeline/folder-batches",
        status_code=202,
    )
    def start_folder_pipeline_batch(
        project_id: UUID,
        request: FolderBatchStartRequest,
    ) -> dict:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            batch, replayed = app.state.folder_pipeline_batches.start(
                project_id,
                snapshot_id=request.snapshot_id,
                rights_confirmed=request.rights_confirmed,
                idempotency_key=request.idempotency_key,
            )
        except (
            FolderBatchConflict,
            FolderBatchUnavailable,
            PipelineProfileMissing,
            ValueError,
        ) as error:
            raise folder_batch_http_error(error) from error
        return {"batch": batch, "idempotent_replay": replayed}

    @router.get(
        "/projects/{project_id}/pipeline/folder-batches",
    )
    def list_folder_pipeline_batches(
        project_id: UUID,
        limit: int = Query(default=128, ge=1, le=128),
    ) -> dict:
        app.state.store.get_project(project_id)
        try:
            batches = app.state.folder_pipeline_batches.list(
                project_id, limit=limit
            )
        except (FolderBatchUnavailable, ValueError) as error:
            raise folder_batch_http_error(error) from error
        return {
            "schema_version": "trafficvisson.folder-pipeline-batch-list.v1",
            "batches": batches,
        }

    @router.get(
        "/projects/{project_id}/pipeline/folder-batches/{batch_id}",
    )
    def get_folder_pipeline_batch(
        project_id: UUID,
        batch_id: UUID,
    ) -> dict:
        app.state.store.get_project(project_id)
        try:
            return app.state.folder_pipeline_batches.get(
                project_id, batch_id
            )
        except (FolderBatchNotFound, ValueError) as error:
            raise folder_batch_http_error(error) from error

    @router.post(
        "/projects/{project_id}/pipeline/folder-batches/{batch_id}/retry",
    )
    def retry_folder_pipeline_batch(
        project_id: UUID,
        batch_id: UUID,
        request: FolderBatchRetryRequest,
    ) -> dict:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            return app.state.folder_pipeline_batches.retry(
                project_id,
                batch_id,
                user_confirmed=request.user_confirmed,
            )
        except (
            FolderBatchConflict,
            FolderBatchNotFound,
            FolderBatchUnavailable,
            ValueError,
        ) as error:
            raise folder_batch_http_error(error) from error

    @router.get(
        "/projects/{project_id}/pipeline/jobs",
        response_model=PipelineJobListResponse,
    )
    def list_pipeline_jobs(
        project_id: UUID,
        limit: int = Query(default=100, ge=1, le=128),
    ) -> PipelineJobListResponse:
        app.state.store.get_project(project_id)
        try:
            rows = app.state.pipeline_jobs.list(project_id, limit=limit)
        except (
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        return PipelineJobListResponse(
            jobs=[PipelineJobResponse(**row) for row in rows]
        )

    @router.get(
        "/projects/{project_id}/pipeline/jobs/{job_id}",
        response_model=PipelineJobResponse,
    )
    def get_pipeline_job(
        project_id: UUID,
        job_id: UUID,
    ) -> PipelineJobResponse:
        app.state.store.get_project(project_id)
        try:
            row = app.state.pipeline_jobs.get(project_id, job_id)
        except (
            PipelineJobNotFound,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        return PipelineJobResponse(**row)

    async def user_discovered_review_context(
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
    ) -> tuple[dict, dict, ResolvedIndependentSurvey]:
        app.state.store.get_project(project_id)
        if (
            app.state.user_discovered_candidates is None
            or app.state.user_discovered_review_queue is None
        ):
            raise HTTPException(
                status_code=503,
                detail="user_discovered_candidate_service_unavailable",
            )
        try:
            job = app.state.pipeline_jobs.get(
                project_id, pipeline_job_id
            )
            question_checkpoint = pipeline_stage_checkpoint(
                job, "question_queue"
            )
            if (
                job.get("status") != "awaiting_review"
                or job.get("current_stage") != "question_queue"
                or question_checkpoint.get("analysis_job_id")
                != str(analysis_job_id)
                or not isinstance(
                    question_checkpoint.get(
                        "question_queue_revision"
                    ),
                    str,
                )
            ):
                raise UserDiscoveredCandidateError(
                    "independent_survey_scope_mismatch",
                    "the pipeline is not exposing an independent survey review",
                )
            grant = await run_in_threadpool(
                app.state.user_discovered_survey_resolver,
                project_id,
                analysis_job_id,
                pipeline_job_id,
            )
        except (
            PipelineJobNotFound,
            PipelineJobUnavailable,
            UserDiscoveredCandidateError,
            ValueError,
        ) as error:
            raise user_discovered_http_error(error) from error
        return job, question_checkpoint, grant

    @router.get(
        (
            "/projects/{project_id}/pipeline-jobs/{pipeline_job_id}/"
            "analysis/{analysis_job_id}/user-discovered-review-frame"
        ),
        response_model=UserDiscoveredReviewFrameResponse,
    )
    async def get_user_discovered_review_frame(
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        timestamp_seconds: float = Query(ge=0, le=86_400),
    ) -> UserDiscoveredReviewFrameResponse:
        """Return one path-free frame from the full immutable 2 FPS survey."""

        (
            _job,
            question_checkpoint,
            grant,
        ) = await user_discovered_review_context(
            project_id,
            pipeline_job_id,
            analysis_job_id,
        )
        try:
            frame = await run_in_threadpool(
                resolve_user_discovered_review_frame,
                grant,
                timestamp_seconds=timestamp_seconds,
            )
        except UserDiscoveredCandidateError as error:
            raise user_discovered_http_error(error) from error
        return UserDiscoveredReviewFrameResponse(
            **frame.to_response(),
            question_queue_revision=question_checkpoint[
                "question_queue_revision"
            ],
        )

    @router.get(
        (
            "/projects/{project_id}/pipeline-jobs/{pipeline_job_id}/"
            "analysis/{analysis_job_id}/user-discovered-review-frames/"
            "{frame_id}"
        ),
        response_class=FileResponse,
    )
    async def get_user_discovered_review_frame_artifact(
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        frame_id: str,
    ) -> FileResponse:
        """Stream one verified dense JPEG without exposing its local path."""

        _job, _question_checkpoint, grant = (
            await user_discovered_review_context(
                project_id,
                pipeline_job_id,
                analysis_job_id,
            )
        )
        try:
            frame = await run_in_threadpool(
                resolve_user_discovered_review_frame_artifact,
                grant,
                frame_id=frame_id,
            )
        except UserDiscoveredCandidateError as error:
            if error.code == "dense_frame_not_found":
                raise HTTPException(
                    status_code=404,
                    detail=error.code,
                ) from error
            raise user_discovered_http_error(error) from error
        return FileResponse(
            frame.path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'",
                "ETag": f'"sha256-{frame.selected_frame_sha256}"',
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-TrafficSuperVision-Artifact-SHA256": (
                    frame.selected_frame_sha256
                ),
                "X-TrafficSuperVision-Dense-Frame-ID": (
                    frame.selected_frame_id
                ),
                "X-TrafficSuperVision-Dense-Frame-Time": (
                    f"{frame.selected_frame_timestamp_seconds:.6f}"
                ),
                "X-TrafficSuperVision-Dense-Index-SHA256": (
                    frame.dense_index_sha256
                ),
            },
        )

    @router.post(
        (
            "/projects/{project_id}/pipeline-jobs/{pipeline_job_id}/"
            "analysis/{analysis_job_id}/user-discovered-candidates"
        ),
        response_model=UserDiscoveredCandidateCreateResponse,
        status_code=201,
    )
    async def create_user_discovered_candidate(
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        request: UserDiscoveredCandidateCreateRequest,
    ) -> UserDiscoveredCandidateCreateResponse:
        """Append one promoted-model miss to review, never directly to truth."""

        app.state.store.get_project(project_id)
        service = app.state.user_discovered_candidates
        queue_service = app.state.user_discovered_review_queue
        if service is None or queue_service is None:
            raise HTTPException(
                status_code=503,
                detail="user_discovered_candidate_service_unavailable",
            )
        try:
            job = app.state.pipeline_jobs.get(
                project_id, pipeline_job_id
            )
            question_checkpoint = pipeline_stage_checkpoint(
                job, "question_queue"
            )
        except (
            PipelineJobNotFound,
            PipelineJobUnavailable,
            UserDiscoveredCandidateError,
            ValueError,
        ) as error:
            raise user_discovered_http_error(error) from error
        if (
            job.get("status") != "awaiting_review"
            or job.get("current_stage") != "question_queue"
            or question_checkpoint.get("analysis_job_id")
            != str(analysis_job_id)
        ):
            raise HTTPException(
                status_code=409,
                detail="independent_survey_scope_mismatch",
            )
        if (
            question_checkpoint.get("question_queue_revision")
            != request.expected_question_queue_revision
        ):
            raise HTTPException(
                status_code=409,
                detail="question_queue_revision_stale",
            )
        try:
            grant = await run_in_threadpool(
                app.state.user_discovered_survey_resolver,
                project_id,
                analysis_job_id,
                pipeline_job_id,
            )
            evidence = await run_in_threadpool(
                service.create,
                UserDiscoveredCandidateRequest(
                    project_id=project_id,
                    analysis_job_id=analysis_job_id,
                    pipeline_job_id=pipeline_job_id,
                    timestamp_seconds=request.timestamp_seconds,
                    selection=request.selection.model_dump(
                        exclude_none=True
                    ),
                    expected_dense_index_sha256=(
                        grant.dense_index_sha256
                    ),
                    expected_model_version_id=(
                        grant.promoted_model_version_id
                    ),
                    expected_model_artifact_manifest_sha256=(
                        grant.promoted_model_artifact_manifest_sha256
                    ),
                    expected_model_inference_sha256=(
                        grant.promoted_model_inference_sha256
                    ),
                    expected_source_analysis_lineage_sha256=(
                        grant.source_analysis_lineage_sha256
                    ),
                    issue_type_proposal=(
                        request.issue_type_proposal
                    ),
                    request_sam2_mask=(
                        request.request_sam2_mask
                    ),
                ),
            )
            app.state.candidate_evidence.bind_overlay(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                evidence.overlay_id,
            )
            replay = (
                app.state.pipeline_jobs
                .get_user_discovered_candidate_registration(
                    project_id,
                    pipeline_job_id,
                    candidate_id=evidence.candidate_id,
                    request_sha256=evidence.request_sha256,
                )
            )
            if replay is None:
                job = app.state.pipeline_jobs.get(
                    project_id, pipeline_job_id
                )
                stage_checkpoints = {
                    name: pipeline_stage_checkpoint(job, name)
                    for name in (
                        "detection_ocr",
                        "sam2_segmentation_tracking",
                        "temporal_merge",
                        "similar_case_search",
                        "vision_proposal",
                        "question_queue",
                    )
                }
                delta = await run_in_threadpool(
                    queue_service.augment,
                    project_id=project_id,
                    analysis_job_id=analysis_job_id,
                    pipeline_job_id=pipeline_job_id,
                    composite_overlay_id=evidence.overlay_id,
                    source_sha256=evidence.source_sha256,
                    stage_checkpoints=stage_checkpoints,
                    user_discovered_evidence=(
                        evidence.to_checkpoint()
                    ),
                )
                identity = await run_in_threadpool(
                    app.state.candidate_labels.resolve_question,
                    project_id,
                    analysis_job_id,
                    evidence.candidate_id,
                )
                catalog = await run_in_threadpool(
                    app.state.store.register_label_questions,
                    project_id,
                    analysis_job_id,
                    evidence.source_sha256,
                    [identity],
                )
                overlay = (
                    app.state.candidate_evidence_overlays.get(
                        project_id,
                        analysis_job_id,
                        pipeline_job_id,
                        evidence.overlay_id,
                    )
                )
                replay, pipeline_replayed = await run_in_threadpool(
                    app.state.pipeline_jobs
                    .register_user_discovered_candidate,
                    project_id,
                    pipeline_job_id,
                    expected_question_queue_revision=(
                        request.expected_question_queue_revision
                    ),
                    candidate_evidence=(
                        evidence.to_checkpoint()
                    ),
                    overlay_checkpoint=overlay.to_checkpoint(),
                    delta_review_event_set=(
                        delta.delta_review_event_set
                    ),
                    delta_event_binding=(
                        delta.delta_event_binding
                    ),
                    question_id=UUID(identity["question_id"]),
                    question_catalog_registration=catalog,
                )
            else:
                pipeline_replayed = True
        except (
            PipelineJobConflict,
            PipelineJobNotFound,
            PipelineJobUnavailable,
            UserDiscoveredCandidateError,
            UserDiscoveredReviewQueueError,
            CandidateLabelError,
            LabelProjectionError,
            ReviewEventSetIntegrityError,
            ValueError,
        ) as error:
            raise user_discovered_http_error(error) from error

        evidence_row = replay["candidate_evidence"]
        event_set = replay["delta_review_event_set"]
        event = replay["delta_event_binding"]
        return UserDiscoveredCandidateCreateResponse(
            schema_version=(
                "trafficvisson.user-discovered-candidate-api.v1"
            ),
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            candidate_id=evidence_row["candidate_id"],
            question_id=UUID(replay["question_id"]),
            candidate_kind="user_discovered",
            requested_timestamp_seconds=evidence_row[
                "requested_timestamp_seconds"
            ],
            selected_frame_timestamp_seconds=evidence_row[
                "selected_frame_timestamp_seconds"
            ],
            selected_frame_id=evidence_row["selected_frame_id"],
            selected_frame_sha256=evidence_row[
                "selected_frame_sha256"
            ],
            selection=evidence_row["selection"],
            issue_type_proposal=evidence_row[
                "issue_type_proposal"
            ],
            review_issue_type=evidence_row[
                "review_issue_type"
            ],
            request_sha256=evidence_row["request_sha256"],
            dense_index_sha256=evidence_row[
                "dense_index_sha256"
            ],
            promoted_model_version_id=UUID(
                evidence_row["promoted_model_version_id"]
            ),
            promoted_model_artifact_manifest_sha256=(
                evidence_row[
                    "promoted_model_artifact_manifest_sha256"
                ]
            ),
            promoted_model_inference_sha256=evidence_row[
                "promoted_model_inference_sha256"
            ],
            dataset_version_id=UUID(
                evidence_row["dataset_version_id"]
            ),
            dataset_hash=evidence_row["dataset_hash"],
            source_analysis_lineage_sha256=evidence_row[
                "source_analysis_lineage_sha256"
            ],
            overlay_id=evidence_row["overlay_id"],
            overlay_manifest_sha256=evidence_row[
                "overlay_manifest_sha256"
            ],
            discovery_revision=evidence_row[
                "discovery_revision"
            ],
            representative_image=evidence_row[
                "representative_image"
            ],
            preview_clip=evidence_row["preview_clip"],
            sam2_refinement_request=evidence_row[
                "sam2_refinement_request"
            ],
            review_event_set_id=event_set[
                "review_event_set_id"
            ],
            review_event_set_revision=event_set[
                "review_event_set_revision"
            ],
            review_event_set_manifest_sha256=event_set[
                "manifest_sha256"
            ],
            review_event_binding={
                "event_id": event["event_id"],
                "event_revision": event["event_revision"],
                "representative_candidate_id": event[
                    "representative_candidate_id"
                ],
            },
            question_queue_revision=replay[
                "question_queue_revision"
            ],
            question_catalog_revision=replay[
                "question_catalog_revision"
            ],
            question_catalog_collection_revision=replay[
                "question_catalog_collection_revision"
            ],
            idempotent_replay=bool(
                pipeline_replayed
                or evidence_row.get("idempotent_replay")
            ),
            review_only=True,
            annotations_created=0,
            incidents_created=0,
            training_members_created=0,
            report_truth_records_created=0,
        )

    @router.get(
        (
            "/projects/{project_id}/pipeline/jobs/{job_id}/"
            "provider-visual-evidence/{candidate_id}/{preparation_id}"
        )
    )
    async def get_provider_visual_evidence_preparation(
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        preparation_id: str,
    ) -> dict:
        app.state.store.get_project(project_id)
        try:
            app.state.pipeline_jobs.get(project_id, job_id)
        except (
            PipelineJobNotFound,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        service = provider_visual_evidence_backend()
        try:
            snapshot = await run_in_threadpool(
                service.get,
                project_id=project_id,
                pipeline_job_id=job_id,
                candidate_id=candidate_id,
                preparation_id=preparation_id,
            )
        except ProviderVisualEvidenceError as error:
            raise provider_visual_evidence_http_error(error) from error
        return dict(snapshot)

    @router.get(
        (
            "/projects/{project_id}/pipeline/jobs/{job_id}/"
            "provider-visual-evidence/{candidate_id}/{preparation_id}/"
            "artifacts/{order}"
        ),
        response_class=FileResponse,
    )
    async def get_provider_visual_evidence_artifact(
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        preparation_id: str,
        order: int,
    ) -> FileResponse:
        app.state.store.get_project(project_id)
        try:
            app.state.pipeline_jobs.get(project_id, job_id)
        except (
            PipelineJobNotFound,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        service = provider_visual_evidence_backend()
        try:
            artifact = await run_in_threadpool(
                service.resolve_review_artifact,
                project_id=project_id,
                pipeline_job_id=job_id,
                candidate_id=candidate_id,
                preparation_id=preparation_id,
                order=order,
            )
        except ProviderVisualEvidenceError as error:
            raise provider_visual_evidence_http_error(error) from error
        return FileResponse(
            artifact.path,
            media_type=artifact.media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'",
                "ETag": f'"sha256-{artifact.sha256}"',
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-TrafficSuperVision-Artifact-SHA256": artifact.sha256,
                "X-TrafficSuperVision-Deidentified-Review-Artifact": "1",
            },
        )

    @router.post(
        (
            "/projects/{project_id}/pipeline/jobs/{job_id}/"
            "provider-visual-evidence/privacy-review"
        )
    )
    async def review_provider_visual_evidence(
        project_id: UUID,
        job_id: UUID,
        request: ProviderVisualEvidencePrivacyReviewRequest,
    ) -> dict:
        app.state.store.get_project(project_id)
        try:
            pipeline_job = app.state.pipeline_jobs.get(
                project_id,
                job_id,
            )
        except (
            PipelineJobNotFound,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        service = provider_visual_evidence_backend()
        try:
            snapshot = await run_in_threadpool(
                service.record_privacy_review,
                project_id=project_id,
                pipeline_job_id=job_id,
                candidate_id=request.candidate_id,
                preparation_id=request.preparation_id,
                expected_manifest_sha256=(
                    request.expected_manifest_sha256
                ),
                approved=request.approved,
                explicit_user_confirmation=(
                    request.explicit_user_confirmation
                ),
            )
        except ProviderVisualEvidenceError as error:
            raise provider_visual_evidence_http_error(error) from error

        resume_requested = False
        resumed_job = pipeline_job
        if (
            request.approved
            and pipeline_job.get("status") == "blocked"
            and pipeline_job.get("current_stage") == "vision_proposal"
            and pipeline_job.get("outcome_code") == "partial"
        ):
            vision_stage = next(
                (
                    stage
                    for stage in pipeline_job.get("stages", ())
                    if stage.get("name") == "vision_proposal"
                ),
                None,
            )
            checkpoint = (
                vision_stage.get("checkpoint")
                if isinstance(vision_stage, dict)
                else None
            )
            preparations = (
                checkpoint.get(
                    "provider_visual_evidence_by_candidate"
                )
                if isinstance(checkpoint, dict)
                else None
            )
            judgments = (
                checkpoint.get("structured_judgments_by_candidate")
                if isinstance(checkpoint, dict)
                else None
            )
            awaiting_candidates = {
                candidate
                for candidate, judgment in (
                    judgments.items()
                    if isinstance(judgments, dict)
                    else ()
                )
                if isinstance(judgment, dict)
                and judgment.get("error_code")
                == "awaiting_deidentification_review"
            }
            preparation_states = {}
            if (
                awaiting_candidates
                and isinstance(preparations, dict)
                and awaiting_candidates.issubset(preparations)
            ):
                try:
                    for candidate in sorted(awaiting_candidates):
                        preparation = preparations[candidate]
                        if not isinstance(preparation, dict):
                            raise ProviderVisualEvidenceError(
                                "visual_evidence_preparation_invalid"
                            )
                        current = await run_in_threadpool(
                            service.get,
                            project_id=project_id,
                            pipeline_job_id=job_id,
                            candidate_id=candidate,
                            preparation_id=str(
                                preparation.get("preparation_id", "")
                            ),
                        )
                        preparation_states[candidate] = current.get(
                            "state"
                        )
                except ProviderVisualEvidenceError as error:
                    raise provider_visual_evidence_http_error(
                        error
                    ) from error
            if preparation_states and all(
                state == "verified_for_provider_transmission"
                for state in preparation_states.values()
            ):
                try:
                    resumed_job = app.state.pipeline_jobs.resume(
                        project_id,
                        job_id,
                    )
                    resume_requested = True
                except (
                    PipelineJobConflict,
                    PipelineJobNotFound,
                    PipelineJobUnavailable,
                    ValueError,
                ) as error:
                    raise pipeline_http_error(error) from error

        return {
            "preparation": dict(snapshot),
            "pipeline_resume_requested": resume_requested,
            "pipeline_job": resumed_job,
        }

    @router.post(
        (
            "/projects/{project_id}/pipeline/jobs/{job_id}/"
            "report-deidentified-evidence/prepare"
        )
    )
    async def prepare_report_deidentified_evidence(
        project_id: UUID,
        job_id: UUID,
        _request: EmptyRequest,
    ) -> dict:
        app.state.store.get_project(project_id)
        (
            _job,
            analysis_job_id,
            _source_sha256,
            sources,
        ) = await current_report_deidentification_sources(
            project_id,
            job_id,
            require_blocked_boundary=True,
        )
        service = report_deidentified_evidence_backend()
        preparations = []
        try:
            for source in sources:
                preparations.append(
                    await run_in_threadpool(service.prepare, source)
                )
        except ReportDeidentifiedEvidenceError as error:
            raise report_deidentified_evidence_http_error(
                error
            ) from error
        return {
            "schema_version": (
                "trafficvisson.report-deidentified-evidence-list.v1"
            ),
            "project_id": str(project_id),
            "pipeline_job_id": str(job_id),
            "analysis_job_id": str(analysis_job_id),
            "state": "prepared_review_required",
            "count": len(preparations),
            "preparations": [dict(row) for row in preparations],
            "path_exposed": False,
        }

    @router.get(
        (
            "/projects/{project_id}/pipeline/jobs/{job_id}/"
            "report-deidentified-evidence"
        )
    )
    async def list_report_deidentified_evidence(
        project_id: UUID,
        job_id: UUID,
    ) -> dict:
        app.state.store.get_project(project_id)
        (
            _job,
            analysis_job_id,
            _source_sha256,
            _candidate_ids,
        ) = report_deidentification_job_context(
            project_id,
            job_id,
            require_blocked_boundary=False,
        )
        service = report_deidentified_evidence_backend()
        try:
            preparations = await run_in_threadpool(
                service.list,
                project_id=project_id,
                pipeline_job_id=job_id,
                analysis_job_id=analysis_job_id,
            )
        except ReportDeidentifiedEvidenceError as error:
            raise report_deidentified_evidence_http_error(
                error
            ) from error
        return {
            "schema_version": (
                "trafficvisson.report-deidentified-evidence-list.v1"
            ),
            "project_id": str(project_id),
            "pipeline_job_id": str(job_id),
            "analysis_job_id": str(analysis_job_id),
            "state": "listed",
            "count": len(preparations),
            "preparations": [dict(row) for row in preparations],
            "path_exposed": False,
        }

    @router.get(
        (
            "/projects/{project_id}/pipeline/jobs/{job_id}/"
            "report-deidentified-evidence/{incident_id}/"
            "{derivative_id}/artifact"
        ),
        response_class=FileResponse,
    )
    async def get_report_deidentified_evidence_artifact(
        project_id: UUID,
        job_id: UUID,
        incident_id: UUID,
        derivative_id: str,
    ) -> FileResponse:
        app.state.store.get_project(project_id)
        (
            _job,
            analysis_job_id,
            _source_sha256,
            _candidate_ids,
        ) = report_deidentification_job_context(
            project_id,
            job_id,
            require_blocked_boundary=False,
        )
        service = report_deidentified_evidence_backend()
        try:
            artifact = await run_in_threadpool(
                service.resolve_review_artifact,
                project_id=project_id,
                pipeline_job_id=job_id,
                analysis_job_id=analysis_job_id,
                incident_id=str(incident_id),
                derivative_id=derivative_id,
            )
        except ReportDeidentifiedEvidenceError as error:
            raise report_deidentified_evidence_http_error(
                error
            ) from error
        return FileResponse(
            artifact.path,
            media_type=artifact.media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'",
                "ETag": f'"sha256-{artifact.sha256}"',
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                (
                    "X-TrafficSuperVision-Artifact-SHA256"
                ): artifact.sha256,
                (
                    "X-TrafficSuperVision-Report-"
                    "Deidentified-Review-Artifact"
                ): "1",
            },
        )

    @router.post(
        (
            "/projects/{project_id}/pipeline/jobs/{job_id}/"
            "report-deidentified-evidence/review"
        )
    )
    async def review_report_deidentified_evidence(
        project_id: UUID,
        job_id: UUID,
        request: ReportDeidentifiedEvidenceReviewRequest,
    ) -> dict:
        app.state.store.get_project(project_id)
        (
            pipeline_job,
            analysis_job_id,
            _source_sha256,
            sources,
        ) = await current_report_deidentification_sources(
            project_id,
            job_id,
            require_blocked_boundary=False,
        )
        expected_incident_ids = {
            source.incident_id for source in sources
        }
        if str(request.incident_id) not in expected_incident_ids:
            raise HTTPException(
                status_code=409,
                detail="report_deidentification_incident_scope_conflict",
            )
        service = report_deidentified_evidence_backend()
        try:
            snapshot = await run_in_threadpool(
                service.record_review,
                project_id=project_id,
                pipeline_job_id=job_id,
                analysis_job_id=analysis_job_id,
                incident_id=str(request.incident_id),
                derivative_id=request.derivative_id,
                expected_manifest_sha256=(
                    request.expected_manifest_sha256
                ),
                approved=request.approved,
                explicit_user_confirmation=(
                    request.explicit_user_confirmation
                ),
            )
            current_preparations = await run_in_threadpool(
                service.list,
                project_id=project_id,
                pipeline_job_id=job_id,
                analysis_job_id=analysis_job_id,
            )
        except ReportDeidentifiedEvidenceError as error:
            raise report_deidentified_evidence_http_error(
                error
            ) from error
        states_by_incident: dict[str, set[str]] = {}
        for row in current_preparations:
            states_by_incident.setdefault(
                str(row["incident_id"]),
                set(),
            ).add(str(row["state"]))
        all_verified = bool(expected_incident_ids) and all(
            states_by_incident.get(incident_id)
            == {"verified_for_shared_report"}
            for incident_id in expected_incident_ids
        )
        resume_requested = False
        resumed_job = pipeline_job
        if (
            all_verified
            and pipeline_job.get("status") == "blocked"
            and pipeline_job.get("current_stage") == "derived_outputs"
            and pipeline_job.get("outcome_code")
            == "report_preview_blocked"
            and pipeline_job.get("error_code")
            == "deidentified_evidence_required"
        ):
            try:
                resumed_job = app.state.pipeline_jobs.resume(
                    project_id,
                    job_id,
                )
                resume_requested = True
            except (
                PipelineJobConflict,
                PipelineJobNotFound,
                PipelineJobUnavailable,
                ValueError,
            ) as error:
                raise pipeline_http_error(error) from error
        return {
            "schema_version": (
                "trafficvisson.report-deidentified-evidence-review-"
                "response.v1"
            ),
            "preparation": dict(snapshot),
            "all_required_derivatives_verified": all_verified,
            "pipeline_resume_requested": resume_requested,
            "pipeline_job": resumed_job,
        }

    @router.post(
        "/projects/{project_id}/pipeline/jobs/{job_id}/cancel",
        response_model=PipelineJobResponse,
    )
    def cancel_pipeline_job(
        project_id: UUID,
        job_id: UUID,
        _request: EmptyRequest,
    ) -> PipelineJobResponse:
        app.state.store.get_project(project_id)
        try:
            row = app.state.pipeline_jobs.cancel(project_id, job_id)
        except (
            PipelineJobConflict,
            PipelineJobNotFound,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        return PipelineJobResponse(**row)

    @router.post(
        "/projects/{project_id}/pipeline/jobs/{job_id}/resume",
        response_model=PipelineJobResponse,
        status_code=202,
    )
    def resume_pipeline_job(
        project_id: UUID,
        job_id: UUID,
        _request: EmptyRequest,
    ) -> PipelineJobResponse:
        app.state.store.get_project(project_id)
        try:
            row = app.state.pipeline_jobs.resume(project_id, job_id)
        except (
            PipelineJobConflict,
            PipelineJobNotFound,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        return PipelineJobResponse(**row)

    @router.post(
        (
            "/projects/{project_id}/pipeline/jobs/{job_id}/"
            "review-complete"
        ),
        response_model=PipelineJobResponse,
        status_code=202,
    )
    def complete_pipeline_review(
        project_id: UUID,
        job_id: UUID,
        request: PipelineReviewCompleteRequest,
    ) -> PipelineJobResponse:
        app.state.store.get_project(project_id)
        try:
            row = app.state.pipeline_jobs.complete_review(
                project_id,
                job_id,
                review_completion_id=request.review_completion_id,
                final_qa_choice=request.final_qa_choice,
            )
        except (
            PipelineJobConflict,
            PipelineJobNotFound,
            PipelineJobUnavailable,
            ValueError,
        ) as error:
            raise pipeline_http_error(error) from error
        return PipelineJobResponse(**row)

    @router.get(
        "/projects/{project_id}/analysis/jobs/{job_id}/label-questions",
        response_model=CandidateLabelQuestionList,
    )
    async def list_candidate_label_questions(
        project_id: UUID, job_id: UUID
    ) -> CandidateLabelQuestionList:
        app.state.store.get_project(project_id)
        job = project_analysis_candidate_evidence(
            project_id,
            app.state.analysis_jobs.get(project_id, job_id),
        )
        if job["status"] != "completed" or job.get("result") is None:
            raise HTTPException(
                status_code=409,
                detail="A completed candidate extraction is required for labeling",
            )
        try:
            audit_records = await run_in_threadpool(
                app.state.candidate_labels.audit_records,
                project_id,
            )
            if audit_records:
                materialize_job_ids = (
                    await materializable_analysis_job_ids(
                        project_id,
                        audit_records,
                    )
                )
                await run_in_threadpool(
                    app.state.store.project_label_records,
                    project_id,
                    audit_records,
                    materialize_analysis_job_ids=materialize_job_ids,
                )
            latest = await run_in_threadpool(
                app.state.candidate_labels.latest_answers,
                project_id,
                analysis_job_id=job_id,
            )
            proposals_by_candidate = await run_in_threadpool(
                app.state.experimental_proposals.latest_for_job,
                project_id,
                job_id,
            )
            latest_by_candidate = {
                record["candidate_id"]: record for record in latest
            }
            questions = []
            for candidate in job["result"]["candidates"]:
                identity = app.state.candidate_labels.resolve_question(
                    project_id,
                    job_id,
                    candidate["candidate_id"],
                )
                record = latest_by_candidate.get(candidate["candidate_id"])
                structured_llm_training_assist = None
                if record is None:
                    try:
                        proposal_answer = await run_in_threadpool(
                            resolve_structured_llm_training_answer,
                            project_id,
                            job_id,
                            candidate["candidate_id"],
                        )
                        structured_llm_training_assist = {
                            "provider": proposal_answer["provider"],
                            "proposal_sha256": proposal_answer[
                                "proposal_sha256"
                            ],
                            "suggested_answer": proposal_answer["answer"],
                            "selected_issue_type": proposal_answer[
                                "selected_issue_type"
                            ],
                            "recommended_severity": proposal_answer[
                                "severity"
                            ],
                            "maintenance_action": proposal_answer[
                                "maintenance_action"
                            ],
                            "ocr_text": proposal_answer["ocr_text"],
                            "rationale": proposal_answer["rationale"],
                            "maintenance_required": proposal_answer[
                                "maintenance_required"
                            ],
                            "user_authorization_required": True,
                            "training_only": True,
                            "label_source": "structured_llm_assisted",
                            "incident_truth_allowed": False,
                            "report_truth_allowed": False,
                        }
                    except (
                        CandidateLabelError,
                        StructuredLlmTrainingLabelError,
                    ):
                        # Optional assist is fail-closed. The ordinary human
                        # question remains available and unchanged.
                        structured_llm_training_assist = None
                questions.append(
                    {
                        "project_id": project_id,
                        "analysis_job_id": job_id,
                        "candidate_id": candidate["candidate_id"],
                        "question_id": identity["question_id"],
                        "status": (
                            record["partition"] if record else "unanswered"
                        ),
                        "candidate": candidate,
                        "latest_answer": (
                            record["final_answer"] if record else None
                        ),
                        "answered_at": (
                            record["recorded_at"] if record else None
                        ),
                        "answer_revision": (
                            record["answer_revision"] if record else 0
                        ),
                        "experimental_proposal": (
                            proposals_by_candidate.get(
                                candidate["candidate_id"]
                            )
                        ),
                        "structured_llm_training_assist": (
                            structured_llm_training_assist
                        ),
                    }
                )
        except CandidateLabelCorrupt:
            raise HTTPException(
                status_code=500,
                detail="The candidate label audit log failed verification",
            ) from None
        except CandidateLabelError:
            raise HTTPException(
                status_code=422,
                detail="The candidate label questions could not be validated",
            ) from None
        except LabelProjectionError:
            raise HTTPException(
                status_code=500,
                detail="The normalized label catalog could not be reconciled",
            ) from None
        return CandidateLabelQuestionList(
            project_id=project_id,
            analysis_job_id=job_id,
            questions=questions,
            count=len(questions),
            answered_count=len(latest_by_candidate),
        )

    @router.post(
        (
            "/projects/{project_id}/analysis/jobs/{job_id}/candidates/"
            "{candidate_id}/mask-refinement-capability"
        ),
        response_model=MaskRefinementCapabilityResponse,
    )
    async def mask_refinement_capability(
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        request: MaskRefinementIdentityRequest,
    ) -> MaskRefinementCapabilityResponse:
        app.state.store.get_project(project_id)
        if (
            request.project_id != project_id
            or request.analysis_job_id != job_id
            or request.candidate_id != candidate_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Mask refinement identity does not match the route",
            )
        try:
            row = await run_in_threadpool(
                app.state.sam2_refinements.capability,
                project_id=project_id,
                analysis_job_id=job_id,
                question_id=request.question_id,
                candidate_id=candidate_id,
            )
        except Sam2RefinementNotFound:
            raise HTTPException(
                status_code=404,
                detail="The completed candidate question was not found",
            ) from None
        except Sam2RefinementError:
            raise HTTPException(
                status_code=422,
                detail="The mask refinement capability request is invalid",
            ) from None
        return MaskRefinementCapabilityResponse(**row)

    @router.post(
        (
            "/projects/{project_id}/analysis/jobs/{job_id}/candidates/"
            "{candidate_id}/mask-refinements"
        ),
        response_model=MaskRefinementResponse,
    )
    async def refine_candidate_mask(
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        request: MaskRefinementRequest,
    ) -> MaskRefinementResponse:
        app.state.store.get_project(project_id)
        if (
            request.project_id != project_id
            or request.analysis_job_id != job_id
            or request.candidate_id != candidate_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Mask refinement identity does not match the route",
            )
        try:
            row = await run_in_threadpool(
                app.state.sam2_refinements.refine,
                project_id=project_id,
                analysis_job_id=job_id,
                question_id=request.question_id,
                candidate_id=candidate_id,
                click_x=request.click.x,
                click_y=request.click.y,
            )
        except Sam2RefinementNotFound:
            raise HTTPException(
                status_code=404,
                detail="The completed candidate question was not found",
            ) from None
        except Sam2RefinementError:
            raise HTTPException(
                status_code=422,
                detail="The SAM2 mask refinement result is invalid",
            ) from None
        return MaskRefinementResponse(**row)

    @router.post(
        "/projects/{project_id}/analysis/jobs/{job_id}/candidates/{candidate_id}/answer",
        response_model=CandidateLabelAnswerResponse,
        status_code=201,
    )
    async def answer_candidate_label_question(
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        request: CandidateLabelAnswerRequest,
    ) -> CandidateLabelAnswerResponse:
        app.state.store.get_project(project_id)
        if (
            request.project_id != project_id
            or request.analysis_job_id != job_id
        ):
            raise HTTPException(
                status_code=409,
                detail="Candidate label identity does not match the route",
            )
        review_closed_probe = getattr(
            app.state.pipeline_jobs,
            "is_analysis_review_closed",
            None,
        )
        if callable(review_closed_probe) and await run_in_threadpool(
            review_closed_probe,
            project_id,
            job_id,
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "The pipeline review is sealed; reopen review before "
                    "changing an answer"
                ),
            )
        normalized_box = (
            request.normalized_box.model_dump()
            if request.normalized_box is not None
            else None
        )
        refinement = None
        if request.mask_refinement_id is not None:
            try:
                refinement = await run_in_threadpool(
                    app.state.sam2_refinements.resolve_for_answer,
                    project_id=project_id,
                    analysis_job_id=job_id,
                    question_id=request.question_id,
                    candidate_id=candidate_id,
                    refinement_id=request.mask_refinement_id,
                )
            except Sam2RefinementNotFound:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The confirmed SAM2 refinement is unavailable or "
                        "belongs to another question"
                    ),
                ) from None
            except Sam2RefinementError:
                raise HTTPException(
                    status_code=422,
                    detail="The confirmed SAM2 refinement is invalid",
                ) from None
            normalized_box = refinement["normalized_box"]
        try:
            resolved_for_geometry = resolve_completed_candidate(
                project_id,
                job_id,
                candidate_id,
            )
            geometry_metadata = resolved_for_geometry.get("metadata")
            training_geometry = (
                training_geometry_from_review(
                    candidate_metadata=geometry_metadata,
                    answer=request.answer,
                    answered_by="local_user",
                    final_box=normalized_box,
                    sam2_refinement=refinement,
                )
                if isinstance(geometry_metadata, Mapping)
                else None
            )
        except CandidateResolutionError:
            raise HTTPException(
                status_code=404,
                detail="The completed analysis candidate was not found",
            ) from None
        except TrainingGeometryError:
            raise HTTPException(
                status_code=422,
                detail="The approved training geometry is invalid",
            ) from None
        try:
            record = await run_in_threadpool(
                app.state.candidate_labels.record_answer,
                project_id,
                job_id,
                candidate_id,
                answer=request.answer,
                selected_issue_type=request.selected_issue_type,
                severity=request.severity,
                maintenance_action=request.maintenance_action,
                ocr_text=request.ocr_text,
                normalized_box=normalized_box,
                answered_by="local_user",
                training_geometry=training_geometry,
            )
        except CandidateResolutionError:
            raise HTTPException(
                status_code=404,
                detail="The completed analysis candidate was not found",
            ) from None
        except CandidateLabelConflict:
            raise HTTPException(
                status_code=409,
                detail="The candidate label conflicts with existing project data",
            ) from None
        except CandidateLabelCorrupt:
            raise HTTPException(
                status_code=500,
                detail="The candidate label audit log failed verification",
            ) from None
        except CandidateLabelError:
            raise HTTPException(
                status_code=422,
                detail="The candidate label answer is invalid",
            ) from None
        try:
            # The JSONL audit is authoritative.  Reconcile the full verified
            # project chain after every append, but promote only analyses that
            # have crossed their pipeline review-completion boundary.
            audit_records = await run_in_threadpool(
                app.state.candidate_labels.audit_records,
                project_id,
            )
            materialize_job_ids = (
                await materializable_analysis_job_ids(
                    project_id,
                    audit_records,
                )
            )
            if audit_records:
                await run_in_threadpool(
                    app.state.store.project_label_records,
                    project_id,
                    audit_records,
                    materialize_analysis_job_ids=materialize_job_ids,
                )
        except (CandidateLabelError, LabelProjectionError):
            raise HTTPException(
                status_code=500,
                detail=(
                    "The label audit was saved but normalized projection "
                    "could not be reconciled"
                ),
            ) from None
        return CandidateLabelAnswerResponse(
            project_id=project_id,
            analysis_job_id=job_id,
            candidate_id=candidate_id,
            question_id=record["question_id"],
            record_id=record["record_id"],
            answer_revision=record["answer_revision"],
            partition=record["partition"],
            include_in_training=record["final_answer"][
                "include_in_training"
            ],
            answered_by="local_user",
            label_source="local_user_review",
            answered_at=record["recorded_at"],
            record_hash=record["record_hash"],
        )

    @router.post(
        (
            "/projects/{project_id}/analysis/jobs/{job_id}/candidates/"
            "{candidate_id}/structured-llm-assist"
        ),
        response_model=CandidateLabelAnswerResponse,
        status_code=201,
    )
    async def authorize_structured_llm_training_label(
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        request: CandidateStructuredLlmAssistRequest,
    ) -> CandidateLabelAnswerResponse:
        """Persist an explicit training-only LLM label, never survey truth."""

        app.state.store.get_project(project_id)
        expected_question_id = app.state.candidate_labels.question_id(
            job_id, candidate_id
        )
        if (
            request.project_id != project_id
            or request.analysis_job_id != job_id
            or request.question_id != expected_question_id
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Structured LLM label identity does not match the "
                    "server-owned question"
                ),
            )
        review_closed_probe = getattr(
            app.state.pipeline_jobs,
            "is_analysis_review_closed",
            None,
        )
        if callable(review_closed_probe) and await run_in_threadpool(
            review_closed_probe,
            project_id,
            job_id,
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "The pipeline review is sealed; reopen review before "
                    "changing an answer"
                ),
            )
        try:
            latest = await run_in_threadpool(
                app.state.candidate_labels.latest_answers,
                project_id,
                analysis_job_id=job_id,
            )
            current = next(
                (
                    item
                    for item in latest
                    if item["question_id"] == str(expected_question_id)
                ),
                None,
            )
            if (
                current is not None
                and current["final_answer"]["answered_by"]
                == "local_user"
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Human truth already exists and cannot be "
                        "superseded by a structured LLM label"
                    ),
                )
            proposal_answer = await run_in_threadpool(
                resolve_structured_llm_training_answer,
                project_id,
                job_id,
                candidate_id,
            )
            assist_evidence = proposal_answer.get("assist_evidence")
            if not isinstance(assist_evidence, Mapping):
                raise StructuredAssistProvenanceError(
                    "structured assist execution evidence is unavailable"
                )
            assist_provenance = (
                seal_structured_assist_provenance(
                    {
                        **dict(assist_evidence),
                        "authority": (
                            per_item_user_grant_authority(
                                project_id=project_id,
                                analysis_job_id=job_id,
                                question_id=expected_question_id,
                                candidate_id=candidate_id,
                            )
                        ),
                    }
                )
            )
            resolved_for_geometry = resolve_completed_candidate(
                project_id,
                job_id,
                candidate_id,
            )
            geometry_metadata = resolved_for_geometry.get("metadata")
            training_geometry = (
                training_geometry_from_review(
                    candidate_metadata=geometry_metadata,
                    answer=proposal_answer["answer"],
                    answered_by="structured_llm_assisted",
                )
                if isinstance(geometry_metadata, Mapping)
                else None
            )
            record = await run_in_threadpool(
                app.state.candidate_labels.record_answer,
                project_id,
                job_id,
                candidate_id,
                answer=proposal_answer["answer"],
                selected_issue_type=proposal_answer[
                    "selected_issue_type"
                ],
                severity=proposal_answer["severity"],
                maintenance_action=proposal_answer[
                    "maintenance_action"
                ],
                ocr_text=proposal_answer["ocr_text"],
                normalized_box=(
                    training_geometry.get("normalized_box")
                    if training_geometry is not None
                    else None
                ),
                answered_by="structured_llm_assisted",
                training_geometry=training_geometry,
                assist_provenance=assist_provenance,
            )
        except HTTPException:
            raise
        except CandidateResolutionError:
            raise HTTPException(
                status_code=404,
                detail="The completed analysis candidate was not found",
            ) from None
        except CandidateLabelConflict:
            raise HTTPException(
                status_code=409,
                detail=(
                    "A structured LLM label cannot supersede a human answer"
                ),
            ) from None
        except StructuredLlmTrainingLabelError as error:
            status_code = (
                404
                if error.code == "proposal_unavailable"
                else 409
                if error.code == "mandatory_human_review"
                else 422
            )
            raise HTTPException(
                status_code=status_code,
                detail=str(error),
            ) from None
        except (
            StructuredAssistProvenanceError,
            TrainingGeometryError,
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "The structured LLM training authority or geometry "
                    "provenance is invalid"
                ),
            ) from None
        except CandidateLabelCorrupt:
            raise HTTPException(
                status_code=500,
                detail="The candidate label audit log failed verification",
            ) from None
        except CandidateLabelError:
            raise HTTPException(
                status_code=422,
                detail="The structured LLM label answer is invalid",
            ) from None

        try:
            audit_records = await run_in_threadpool(
                app.state.candidate_labels.audit_records,
                project_id,
            )
            materialize_job_ids = (
                await materializable_analysis_job_ids(
                    project_id,
                    audit_records,
                )
            )
            if audit_records:
                await run_in_threadpool(
                    app.state.store.project_label_records,
                    project_id,
                    audit_records,
                    materialize_analysis_job_ids=materialize_job_ids,
                )
        except (CandidateLabelError, LabelProjectionError):
            raise HTTPException(
                status_code=500,
                detail=(
                    "The LLM label audit was saved but normalized projection "
                    "could not be reconciled"
                ),
            ) from None

        return CandidateLabelAnswerResponse(
            project_id=project_id,
            analysis_job_id=job_id,
            candidate_id=candidate_id,
            question_id=record["question_id"],
            record_id=record["record_id"],
            answer_revision=record["answer_revision"],
            partition=record["partition"],
            include_in_training=record["final_answer"][
                "include_in_training"
            ],
            answered_by="structured_llm_assisted",
            label_source="structured_llm_assisted",
            answered_at=record["recorded_at"],
            record_hash=record["record_hash"],
        )

    async def materializable_analysis_job_ids(
        project_id: UUID,
        audit_records: list[dict],
    ) -> list[UUID]:
        gated_by_analysis_job: dict[UUID, bool] = {}
        for record in audit_records:
            analysis_job_id = UUID(str(record["analysis_job_id"]))
            if analysis_job_id not in gated_by_analysis_job:
                gated_by_analysis_job[
                    analysis_job_id
                ] = await run_in_threadpool(
                    app.state.pipeline_jobs.is_analysis_projection_gated,
                    project_id,
                    analysis_job_id,
                )
        return [
            analysis_job_id
            for analysis_job_id, gated in gated_by_analysis_job.items()
            if not gated
        ]

    async def reconcile_normalized_labels(project_id: UUID) -> None:
        try:
            audit_records = await run_in_threadpool(
                app.state.candidate_labels.audit_records,
                project_id,
            )
            materialize_job_ids = await materializable_analysis_job_ids(
                project_id,
                audit_records,
            )
            if audit_records:
                await run_in_threadpool(
                    app.state.store.project_label_records,
                    project_id,
                    audit_records,
                    materialize_analysis_job_ids=materialize_job_ids,
                )
        except CandidateLabelCorrupt:
            raise HTTPException(
                status_code=500,
                detail="The candidate label audit log failed verification",
            ) from None
        except (CandidateLabelError, LabelProjectionError):
            raise HTTPException(
                status_code=500,
                detail="The normalized label catalog could not be reconciled",
            ) from None

    @router.get(
        "/projects/{project_id}/labels/stats",
        response_model=ProjectLabelStatsResponse,
    )
    async def get_project_label_stats(
        project_id: UUID,
    ) -> ProjectLabelStatsResponse:
        app.state.store.get_project(project_id)
        await reconcile_normalized_labels(project_id)
        return ProjectLabelStatsResponse(
            **await run_in_threadpool(
                app.state.store.current_label_stats,
                project_id,
            )
        )

    @router.get(
        "/projects/{project_id}/incidents",
        response_model=ProjectIncidentListResponse,
    )
    async def list_project_incidents(
        project_id: UUID,
        include_inactive: bool = Query(default=False),
        analysis_job_id: Optional[UUID] = Query(default=None),
    ) -> ProjectIncidentListResponse:
        app.state.store.get_project(project_id)
        await reconcile_normalized_labels(project_id)
        incidents = await run_in_threadpool(
            app.state.store.list_incidents,
            project_id,
            include_inactive=include_inactive,
            analysis_job_id=analysis_job_id,
        )
        return ProjectIncidentListResponse(
            project_id=project_id,
            incidents=incidents,
            count=len(incidents),
        )

    @router.get(
        "/projects/{project_id}/datasets",
        response_model=DatasetVersionListResponse,
    )
    async def list_project_dataset_versions(
        project_id: UUID,
    ) -> DatasetVersionListResponse:
        app.state.store.get_project(project_id)
        await reconcile_normalized_labels(project_id)
        versions = await run_in_threadpool(
            app.state.store.list_dataset_versions,
            project_id,
        )
        return DatasetVersionListResponse(
            project_id=project_id,
            versions=versions,
            count=len(versions),
        )

    @router.post(
        "/projects/{project_id}/datasets",
        response_model=DatasetVersionResponse,
        status_code=201,
    )
    async def create_project_dataset_version(
        project_id: UUID,
        request: DatasetVersionCreateRequest,
    ) -> DatasetVersionResponse:
        app.state.store.get_project(project_id)
        if request.user_confirmed is not True:
            raise HTTPException(
                status_code=422,
                detail="Dataset snapshot confirmation is required",
            )
        await reconcile_normalized_labels(project_id)
        try:
            version = await run_in_threadpool(
                app.state.store.create_dataset_version,
                project_id,
                approved_reference_records=(
                    app.state.weak_case_approvals.list_project(
                        project_id
                    )["records"]
                ),
                fixed_validation_analysis_job_ids=(
                    resolve_fixed_validation_analysis_jobs(
                        project_id,
                        request.fixed_validation_extension_approval,
                    )
                    or None
                ),
            )
        except DatasetVersionError:
            raise HTTPException(
                status_code=409,
                detail="At least one reviewed label is required",
            ) from None
        except ProjectModelLifecycleError as error:
            raise project_model_http_error(error) from error
        return DatasetVersionResponse(**version)

    def resolve_project_model_training_sources(
        project_id: UUID,
        dataset_version_id: UUID,
    ) -> tuple[dict, list[dict], list[dict], str]:
        """Resolve every factual training input from bridge-owned stores."""

        project = app.state.store.get_project(project_id)
        versions = app.state.store.list_dataset_versions(project_id)
        dataset = next(
            (
                version
                for version in versions
                if str(version["dataset_version_id"])
                == str(dataset_version_id)
            ),
            None,
        )
        if dataset is None:
            raise ProjectModelLifecycleError(
                "dataset_version_not_found",
                "DatasetVersion was not found in this project",
            )
        # Keep the renderer outside this boundary: it supplies only the
        # immutable DatasetVersion id, never Annotation rows or hashes.
        current_annotations = (
            app.state.store.list_dataset_annotations(
                project_id,
                dataset_version_id,
            )
        )
        if not current_annotations and dataset["memberships"]:
            raise ProjectModelLifecycleError(
                "durable_catalog_unavailable",
                "project model training requires the durable label catalog",
            )
        dataset_annotation_ids = {
            str(member["annotation_id"])
            for member in dataset["memberships"]
        }
        selected_annotations = [
            annotation
            for annotation in current_annotations
            if str(annotation["annotation_id"])
            in dataset_annotation_ids
        ]
        approved_references = dataset.get(
            "approved_reference_members"
        )
        if not isinstance(approved_references, list):
            raise ProjectModelLifecycleError(
                "dataset_reference_snapshot_missing",
                (
                    "DatasetVersion does not contain its immutable "
                    "approved-reference membership"
                ),
            )
        state = app.state.project_models.get_project_state(project_id)
        base_model_version = (
            state["active_model_version"]
            if state is not None
            else str(project["active_model_version"])
        )
        return (
            dataset,
            selected_annotations,
            approved_references,
            base_model_version,
        )

    def capture_project_model_training_root(
        project_id: UUID,
        idempotency_key: str,
    ) -> Optional[dict[str, Any]]:
        """Capture the current profile root at the explicit training click."""

        supports_bound_roots = bool(
            getattr(
                app.state.project_models,
                "supports_bound_artifact_roots",
                False,
            )
        )
        if not supports_bound_roots:
            if fixture_mode:
                # Only explicit fixture apps retain the old local artifact
                # behavior. A production app must never silently fall back
                # outside the configured project output location.
                return None
            raise ProjectModelLifecycleError(
                "artifact_root_unavailable",
                (
                    "The production model lifecycle store does not "
                    "support project-bound artifact roots"
                ),
            )
        snapshot = app.state.project_models.list_training_jobs(
            project_id
        )
        for existing in snapshot.get("jobs", ()):
            if (
                isinstance(existing, Mapping)
                and existing.get("idempotency_key")
                == idempotency_key
            ):
                request_payload = existing.get("request")
                binding = (
                    request_payload.get("artifact_root_binding")
                    if isinstance(request_payload, Mapping)
                    else None
                )
                return (
                    dict(binding)
                    if isinstance(binding, Mapping)
                    else None
                )
        try:
            profile = app.state.pipeline_jobs.get_profile(project_id)
            resolved = (
                app.state.project_artifact_roots
                .capture_job_binding(
                    project_id,
                    profile_revision=int(profile["revision"]),
                    output_location_id=str(
                        profile["output_location_id"]
                    ),
                )
            )
        except (
            PipelineProfileMissing,
            ProjectArtifactRootNotFound,
            ProjectArtifactRootConflict,
            ProjectArtifactRootUnavailable,
        ) as error:
            raise ProjectModelLifecycleError(
                "artifact_root_unavailable",
                (
                    "The current project output location must be "
                    "configured before model training"
                ),
            ) from error
        return dict(resolved.binding)

    @router.post(
        "/projects/{project_id}/model-training-jobs",
        response_model=ProjectModelTrainingJobResponse,
        status_code=202,
    )
    async def start_project_model_training(
        project_id: UUID,
        request: ProjectModelTrainingStartRequest,
    ) -> ProjectModelTrainingJobResponse:
        app.state.store.get_project(project_id)
        await reconcile_normalized_labels(project_id)
        try:
            (
                dataset,
                current_annotations,
                approved_references,
                base_model_version,
            ) = await run_in_threadpool(
                resolve_project_model_training_sources,
                project_id,
                request.dataset_version_id,
            )
            artifact_root_binding = await run_in_threadpool(
                capture_project_model_training_root,
                project_id,
                request.idempotency_key,
            )
            row = await run_in_threadpool(
                app.state.project_models.start_training,
                project_id,
                dataset_version=dataset,
                current_annotations=current_annotations,
                auxiliary_references=approved_references,
                base_model_version=base_model_version,
                # The lifecycle derives target issue types from the immutable
                # DatasetVersion. Renderer input is intentionally absent.
                required_issue_types=(),
                idempotency_key=request.idempotency_key,
                user_confirmed=request.user_confirmed,
                artifact_root_binding=artifact_root_binding,
                fixed_validation_extension_approval=(
                    request.fixed_validation_extension_approval.model_dump(
                        mode="json"
                    )
                    if request.fixed_validation_extension_approval
                    is not None
                    else None
                ),
            )
        except WeakCaseApprovalError as error:
            raise weak_case_approval_http_error(error) from error
        except ProjectModelLifecycleError as error:
            raise project_model_http_error(error) from error
        return ProjectModelTrainingJobResponse(
            **project_model_job_api_payload(row)
        )

    @router.get(
        "/projects/{project_id}/model-training-jobs",
        response_model=ProjectModelTrainingJobListResponse,
    )
    async def list_project_model_training_jobs(
        project_id: UUID,
    ) -> ProjectModelTrainingJobListResponse:
        app.state.store.get_project(project_id)
        try:
            snapshot = await run_in_threadpool(
                app.state.project_models.list_training_jobs,
                project_id,
            )
        except ProjectModelLifecycleError as error:
            raise project_model_http_error(error) from error
        jobs = [
            project_model_job_api_payload(row)
            for row in snapshot["jobs"]
        ]
        return ProjectModelTrainingJobListResponse(
            schema_version=snapshot["schema_version"],
            project_id=project_id,
            count=len(jobs),
            jobs=jobs,
        )

    @router.get(
        "/projects/{project_id}/fixed-validation-sets",
        response_model=ProjectFixedValidationVersionListResponse,
    )
    async def list_project_fixed_validation_sets(
        project_id: UUID,
    ) -> ProjectFixedValidationVersionListResponse:
        app.state.store.get_project(project_id)
        try:
            snapshot = await run_in_threadpool(
                app.state.project_models.list_fixed_validation_versions,
                project_id,
            )
        except ProjectModelLifecycleError as error:
            raise project_model_http_error(error) from error
        versions = [
            project_fixed_validation_api_payload(row)
            for row in snapshot["versions"]
        ]
        return ProjectFixedValidationVersionListResponse(
            schema_version=snapshot["schema_version"],
            project_id=project_id,
            count=len(versions),
            versions=versions,
        )

    @router.post(
        (
            "/projects/{project_id}/model-training-jobs/"
            "{training_job_id}/resume"
        ),
        response_model=ProjectModelTrainingJobResponse,
        status_code=202,
    )
    async def resume_project_model_training(
        project_id: UUID,
        training_job_id: UUID,
        request: ProjectModelTrainingResumeRequest,
    ) -> ProjectModelTrainingJobResponse:
        app.state.store.get_project(project_id)
        if request.user_confirmed is not True:
            raise HTTPException(
                status_code=422,
                detail="Training resume confirmation is required",
            )
        try:
            row = await run_in_threadpool(
                app.state.project_models.resume_training,
                project_id,
                training_job_id,
                user_confirmed=request.user_confirmed,
            )
        except ProjectModelLifecycleError as error:
            raise project_model_http_error(error) from error
        return ProjectModelTrainingJobResponse(
            **project_model_job_api_payload(row)
        )

    @router.post(
        (
            "/projects/{project_id}/model-training-jobs/"
            "{training_job_id}/cancel"
        ),
        response_model=ProjectModelTrainingJobResponse,
        status_code=202,
    )
    async def cancel_project_model_training(
        project_id: UUID,
        training_job_id: UUID,
        request: ProjectModelTrainingCancelRequest,
    ) -> ProjectModelTrainingJobResponse:
        app.state.store.get_project(project_id)
        try:
            row = await run_in_threadpool(
                app.state.project_models.cancel_training,
                project_id,
                training_job_id,
                user_confirmed=request.user_confirmed,
            )
        except ProjectModelLifecycleError as error:
            raise project_model_http_error(error) from error
        return ProjectModelTrainingJobResponse(
            **project_model_job_api_payload(row)
        )

    @router.get(
        "/projects/{project_id}/model-versions",
        response_model=ProjectModelVersionListResponse,
    )
    async def list_project_model_versions(
        project_id: UUID,
    ) -> ProjectModelVersionListResponse:
        app.state.store.get_project(project_id)
        try:
            snapshot = await run_in_threadpool(
                app.state.project_models.list_model_versions,
                project_id,
            )
        except ProjectModelLifecycleError as error:
            raise project_model_http_error(error) from error
        models = [
            project_model_version_api_payload(row)
            for row in snapshot["models"]
        ]
        return ProjectModelVersionListResponse(
            schema_version=snapshot["schema_version"],
            project_id=project_id,
            count=len(models),
            models=models,
        )

    @router.get(
        "/projects/{project_id}/model-iterations",
        response_model=ProjectModelIterationLedgerResponse,
    )
    async def list_project_model_iterations(
        project_id: UUID,
    ) -> ProjectModelIterationLedgerResponse:
        app.state.store.get_project(project_id)
        try:
            survey_qa = await run_in_threadpool(
                app.state.model_iteration_qa.list_survey_model_qa,
                project_id,
            )
            iterations = await run_in_threadpool(
                app.state.model_iteration_qa.list_model_iterations,
                project_id,
            )
        except ModelIterationQaError as error:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "model_iteration_ledger_invalid",
                    "message": str(error),
                },
            ) from error
        survey_summaries = [
            {
                "schema_version": (
                    "trafficvisson.model-survey-qa-summary.v1"
                ),
                "survey_model_qa_sha256": item[
                    "survey_model_qa_sha256"
                ],
                "pipeline_job_id": item["pipeline_job_id"],
                "analysis_job_id": item["analysis_job_id"],
                "model_version_id": item[
                    "promoted_model_binding"
                ]["model_version_id"],
                "dataset_version_id": item[
                    "promoted_model_binding"
                ]["dataset_version_id"],
                "qa_scope_sha256": item["qa_scope_sha256"],
                "model_candidate_count": item["summary"][
                    "model_candidate_count"
                ],
                "user_discovered_candidate_count": item[
                    "summary"
                ]["user_discovered_candidate_count"],
                "other_detector_candidate_count": item["summary"][
                    "other_detector_candidate_count"
                ],
                "resolved_training_feedback_count": item["summary"][
                    "resolved_training_feedback_count"
                ],
                "true_positive_count": item["summary"][
                    "true_positive_count"
                ],
                "false_positive_count": item["summary"][
                    "false_positive_count"
                ],
                "false_negative_count": item["summary"][
                    "false_negative_count"
                ],
                "held_count": item["summary"]["held_count"],
                "drift_assessed": item["drift"]["assessed"],
                "baseline_survey_model_qa_sha256": item["drift"][
                    "baseline_survey_model_qa_sha256"
                ],
                "promotion_gate_authority": False,
                "starts_training": False,
                "promotes_model": False,
            }
            for item in survey_qa
        ]
        iteration_summaries = [
            {
                "schema_version": (
                    "trafficvisson.model-iteration-summary.v1"
                ),
                "model_iteration_sha256": item[
                    "model_iteration_sha256"
                ],
                "parent_iteration_sha256": item[
                    "parent_iteration_sha256"
                ],
                "survey_model_qa_sha256": item[
                    "survey_model_qa_sha256"
                ],
                "parent_model_version_id": item[
                    "parent_promoted_model_binding"
                ]["model_version_id"],
                "parent_dataset_version_id": item[
                    "parent_dataset"
                ]["dataset_version_id"],
                "next_dataset_version_id": item["next_dataset"][
                    "dataset_version_id"
                ],
                "feedback_annotation_count": len(
                    item["feedback_annotations"]
                ),
                "audit_only_member_count": len(
                    item["audit_only_members"]
                ),
                "fixed_validation_sha256": item[
                    "fixed_validation_sha256"
                ],
                "next_training_requires_user_confirmation": True,
                "automatic_training_start": False,
                "automatic_model_promotion": False,
                "iteration_creates_incident": False,
                "iteration_creates_report_truth": False,
            }
            for item in iterations
        ]
        return ProjectModelIterationLedgerResponse(
            schema_version=(
                "trafficvisson.model-iteration-ledger.v1"
            ),
            project_id=project_id,
            survey_qa_count=len(survey_summaries),
            iteration_count=len(iteration_summaries),
            survey_qa=survey_summaries,
            iterations=iteration_summaries,
        )

    @router.get(
        "/projects/{project_id}/model-state",
        response_model=ProjectModelStateResponse,
    )
    async def get_project_model_state(
        project_id: UUID,
    ) -> ProjectModelStateResponse:
        app.state.store.get_project(project_id)
        try:
            state = await run_in_threadpool(
                app.state.project_models.get_project_state,
                project_id,
            )
        except ProjectModelLifecycleError as error:
            raise project_model_http_error(error) from error
        if state is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "model_state_not_found",
                    "message": "project model state does not exist",
                },
            )
        return ProjectModelStateResponse(**state)

    async def transition_project_model(
        project_id: UUID,
        action: str,
        request: ProjectModelTransitionRequest,
        model_version_id: Optional[UUID] = None,
    ) -> ProjectModelTransitionResponse:
        app.state.store.get_project(project_id)
        try:
            if action == "promote":
                row = await run_in_threadpool(
                    app.state.project_models.promote,
                    project_id,
                    model_version_id,
                    expected_revision=request.expected_revision,
                    idempotency_key=request.idempotency_key,
                    user_confirmed=request.user_confirmed,
                )
            elif action == "reject":
                row = await run_in_threadpool(
                    app.state.project_models.reject,
                    project_id,
                    model_version_id,
                    expected_revision=request.expected_revision,
                    idempotency_key=request.idempotency_key,
                    user_confirmed=request.user_confirmed,
                )
            else:
                row = await run_in_threadpool(
                    app.state.project_models.rollback,
                    project_id,
                    expected_revision=request.expected_revision,
                    idempotency_key=request.idempotency_key,
                    user_confirmed=request.user_confirmed,
                )
        except ProjectModelLifecycleError as error:
            raise project_model_http_error(error) from error
        return ProjectModelTransitionResponse(
            **project_model_transition_api_payload(row)
        )

    @router.post(
        (
            "/projects/{project_id}/model-versions/"
            "{model_version_id}/promote"
        ),
        response_model=ProjectModelTransitionResponse,
    )
    async def promote_project_model(
        project_id: UUID,
        model_version_id: UUID,
        request: ProjectModelTransitionRequest,
    ) -> ProjectModelTransitionResponse:
        return await transition_project_model(
            project_id,
            "promote",
            request,
            model_version_id,
        )

    @router.post(
        (
            "/projects/{project_id}/model-versions/"
            "{model_version_id}/reject"
        ),
        response_model=ProjectModelTransitionResponse,
    )
    async def reject_project_model(
        project_id: UUID,
        model_version_id: UUID,
        request: ProjectModelTransitionRequest,
    ) -> ProjectModelTransitionResponse:
        return await transition_project_model(
            project_id,
            "reject",
            request,
            model_version_id,
        )

    @router.post(
        "/projects/{project_id}/model-state/rollback",
        response_model=ProjectModelTransitionResponse,
    )
    async def rollback_project_model(
        project_id: UUID,
        request: ProjectModelTransitionRequest,
    ) -> ProjectModelTransitionResponse:
        return await transition_project_model(
            project_id,
            "rollback",
            request,
        )

    def compile_auto_label_recipe(
        project_id: UUID,
        command: str,
        issue_type: Optional[str],
    ) -> tuple[dict, str]:
        plan = compile_labeling_command(
            command,
            explicit_issue_type=issue_type,
        )
        recipe_id = _canonical_export_provenance_hash(
            {
                "project_id": str(project_id),
                "command": command.strip(),
                "issue_type": plan["issue_type"],
                "prompt_sha256": plan["prompt_sha256"],
                "mapping_version": plan["mapping_version"],
                "prompt_set_version": plan["prompt_set_version"],
            }
        )
        return plan, recipe_id

    @router.post(
        "/projects/{project_id}/auto-labeling/compile",
        response_model=AutoLabelCompileResponse,
    )
    async def compile_auto_labeling_command(
        project_id: UUID,
        request: AutoLabelCompileRequest,
    ) -> AutoLabelCompileResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Auto-label project identity does not match the route",
            )
        try:
            plan, recipe_id = await run_in_threadpool(
                compile_auto_label_recipe,
                project_id,
                request.command,
                request.issue_type,
            )
        except AutoLabelingError:
            raise HTTPException(
                status_code=422,
                detail=(
                    "The command could not be mapped to exactly one supported "
                    "road issue; select an issue type explicitly"
                ),
            ) from None
        warnings = [
            (
                "AI 결과는 선라벨이며 사용자 확정 라벨이나 실제 유지보수 "
                "판정으로 자동 승격되지 않습니다."
            ),
            (
                "점수는 모델별 상대 점수이며 결함 확률이나 현장 정확도로 "
                "해석하지 않습니다."
            ),
        ]
        if plan["requires_ocr"]:
            warnings.append(
                "이정표·터널명은 OCR과 위치 검증이 필요하므로 항상 사용자가 확인합니다."
            )
        if plan["requires_relationship"]:
            warnings.append(
                "가림·삭초 관계 판단은 객체 존재만으로 확정하지 않고 항상 검토합니다."
            )
        positive_prompts = [
            *plan["positive_prompts"]["grounding_dino"],
            *plan["positive_prompts"]["clip"],
        ]
        return AutoLabelCompileResponse(
            recipe_id=recipe_id,
            project_id=project_id,
            command=request.command.strip(),
            issue_type=plan["issue_type"],
            display_name_ko=_AUTO_LABEL_DISPLAY_NAMES[plan["issue_type"]],
            positive_prompts=positive_prompts,
            negative_prompts=plan["negative_prompts"],
            capabilities={
                "object_detection": True,
                "segmentation_required": plan["requires_segmentation"],
                "ocr_required": plan["requires_ocr"],
                "relationship_reasoning_required": plan[
                    "requires_relationship"
                ],
                "maintenance_judgment_required": (
                    plan["issue_type"] in _MAINTENANCE_JUDGMENT_TYPES
                ),
            },
            default_mode="proposal_only",
            warnings=warnings,
        )

    @router.post(
        "/projects/{project_id}/auto-labeling/runs",
        response_model=AutoLabelRunResponse,
        status_code=201,
    )
    async def run_auto_labeling(
        project_id: UUID,
        request: AutoLabelRunRequest,
    ) -> AutoLabelRunResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Auto-label project identity does not match the route",
            )
        try:
            plan, expected_recipe_id = await run_in_threadpool(
                compile_auto_label_recipe,
                project_id,
                request.command,
                request.issue_type,
            )
        except AutoLabelingError:
            raise HTTPException(
                status_code=422,
                detail="The auto-label command is invalid",
            ) from None
        if request.recipe_id != expected_recipe_id:
            raise HTTPException(
                status_code=409,
                detail="The compiled auto-label recipe changed before execution",
            )

        if request.scope == "current_job":
            assert request.job_id is not None
            job = app.state.analysis_jobs.get(project_id, request.job_id)
            jobs = [job]
        else:
            jobs = app.state.analysis_jobs.list(project_id, limit=100)
        completed_jobs = [
            job
            for job in jobs
            if job["status"] == "completed" and job.get("result") is not None
        ]
        requested_policy_mode = (
            HIGH_CONFIDENCE_AUTO_ACCEPT
            if request.mode == "assisted_auto"
            else PROPOSAL_ONLY
        )
        policy = {
            "mode": requested_policy_mode,
            # Thresholds remain empty until a project validation set has
            # measured a score calibration. This intentionally downgrades
            # today's uncalibrated proposals to review-only.
            "per_class_reviews": {},
            "validated_thresholds": {},
        }
        decisions = []
        candidates_evaluated = 0
        generated_any = False
        cached_any = False
        generation_messages: list[str] = []
        recipe_evaluation_id = f"cmd-{expected_recipe_id}"
        for job in completed_jobs:
            job_id = UUID(str(job["job_id"]))
            proposals = await run_in_threadpool(
                app.state.experimental_proposals.latest_for_job,
                project_id,
                job_id,
            )
            recipe_proposals = await run_in_threadpool(
                app.state.experimental_proposals.latest_for_job,
                project_id,
                job_id,
                evaluation_id=recipe_evaluation_id,
            )

            def has_requested_prediction(proposal: dict) -> bool:
                return any(
                    prediction.get("issue_type") == plan["issue_type"]
                    for prediction in proposal.get("predictions", [])
                    if isinstance(prediction, dict)
                )

            cached_relevant = (
                recipe_proposals
                if recipe_proposals
                else {
                    candidate_id: proposal
                    for candidate_id, proposal in proposals.items()
                    if has_requested_prediction(proposal)
                }
            )
            expected_candidates = job["result"].get("candidates", [])
            complete_recipe_cache = bool(recipe_proposals)
            has_legacy_match = any(
                has_requested_prediction(item)
                for item in cached_relevant.values()
            )
            should_generate = (
                request.scope == "current_job"
                and not complete_recipe_cache
                and not has_legacy_match
                and bool(expected_candidates)
            )
            generated_evaluation_count = 0
            if should_generate:
                try:
                    generation = await run_in_threadpool(
                        app.state.command_proposal_generator.generate,
                        project_id=project_id,
                        analysis_job_id=job_id,
                        recipe_id=expected_recipe_id,
                        command_plan=plan,
                        candidates=expected_candidates,
                        resolve_image=lambda resolved_project_id,
                        resolved_job_id,
                        candidate_id: app.state.analysis_jobs.resolve_artifact(
                            resolved_project_id,
                            resolved_job_id,
                            candidate_id,
                            "image",
                        ),
                    )
                except (CommandProposalGenerationError, HTTPException):
                    generation_messages.append(
                        "로컬 명령형 AI 모델 실행 또는 무결성 검증에 실패했습니다."
                    )
                else:
                    generation_messages.append(generation.message_ko)
                    if generation.available and generation.generated:
                        generated_evaluation_count = (
                            generation.evaluated_candidate_count
                        )
                        # Negative/no-suggestion rows are deliberately not
                        # sidecars: a newer empty row must never hide a valid
                        # proposal from another model or command recipe.
                        if generation.proposals:
                            try:
                                await run_in_threadpool(
                                    app.state.experimental_proposals.record_run,
                                    project_id,
                                    job_id,
                                    model=generation.model,
                                    evaluation=generation.evaluation,
                                    proposals=list(generation.proposals),
                                )
                            except ExperimentalProposalError:
                                raise HTTPException(
                                    status_code=500,
                                    detail=(
                                        "Generated AI proposals could not be "
                                        "persisted inside the review-only store"
                                    ),
                                ) from None
                        generated_any = True
                        cached_relevant = (
                            await run_in_threadpool(
                                app.state.experimental_proposals.latest_for_job,
                                project_id,
                                job_id,
                                evaluation_id=recipe_evaluation_id,
                            )
                            if generation.proposals
                            else {}
                        )
            elif cached_relevant:
                cached_any = True
            elif request.scope == "project":
                generation_messages.append(
                    "프로젝트 전체 새 AI 판독은 현재 동기 실행 범위에서 제외되고 "
                    "검증된 기존 선라벨만 조회했습니다."
                )

            if cached_relevant:
                cached_any = cached_any or not generated_any
            candidates_evaluated += (
                generated_evaluation_count
                if generated_evaluation_count > 0
                else len(cached_relevant)
            )
            if candidates_evaluated > 10_000:
                raise HTTPException(
                    status_code=422,
                    detail="The auto-label project scope exceeds 10,000 candidates",
                )
            for candidate_id, proposal in cached_relevant.items():
                if not has_requested_prediction(proposal):
                    continue
                source = proposal.get("source") or {}
                expected_evidence = {
                    "project_id": str(project_id),
                    "analysis_job_id": str(job_id),
                    "candidate_id": candidate_id,
                    "representative_image_sha256": source.get(
                        "representative_image_sha256"
                    ),
                    "analysis_candidate_sha256": source.get(
                        "analysis_candidate_sha256"
                    ),
                }
                try:
                    decision = await run_in_threadpool(
                        create_provisional_auto_label_decision,
                        command_plan=plan,
                        proposal=proposal,
                        expected_evidence=expected_evidence,
                        policy=policy,
                        urgent=False,
                        maintenance_relation_decision=(
                            plan["issue_type"]
                            in _MAINTENANCE_JUDGMENT_TYPES
                        ),
                    )
                except AutoLabelingError:
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "An AI proposal failed its evidence or provenance "
                            "validation"
                        ),
                    ) from None
                decisions.append(decision)

        proposed_count = len(decisions)
        auto_accepted_count = sum(
            decision["review_status"] == "auto_accepted"
            for decision in decisions
        )
        review_required_count = proposed_count - auto_accepted_count
        if generated_any:
            proposal_origin = "local_model_generated"
            proposal_generation_status = "generated"
        elif cached_any and candidates_evaluated > 0:
            proposal_origin = "cached_existing"
            proposal_generation_status = "cached"
        else:
            proposal_origin = "none"
            proposal_generation_status = "unavailable"
        if candidates_evaluated == 0:
            status = "model_unavailable"
            runner_detail = (
                generation_messages[-1]
                if generation_messages
                else "검증된 로컬 명령형 AI 모델 실행기를 사용할 수 없습니다."
            )
            message = (
                f"{runner_detail} 이 범위에 재사용할 검증 가능한 선라벨도 없어 "
                "빈 실행 감사 기록만 저장했습니다."
            )[:500]
        elif proposed_count == 0:
            status = "no_candidates"
            message = (
                f"{candidates_evaluated}개 프레임의 검증 가능한 AI 결과를 검사했지만 "
                f"‘{_AUTO_LABEL_DISPLAY_NAMES[plan['issue_type']]}’와 일치하는 "
                "후보는 없었습니다. 라벨이나 사건은 만들지 않았고 실행 감사 "
                "기록만 저장했습니다."
            )[:500]
        else:
            status = "completed"
            origin_text = (
                "로컬 AI가 새로 생성한 결과"
                if proposal_origin == "local_model_generated"
                else "검증된 기존 AI 결과"
            )
            message = (
                f"{origin_text} {candidates_evaluated}개에서 선라벨 "
                f"{proposed_count}건을 찾았습니다. 사용자 확인이 필요한 "
                f"항목은 {review_required_count}건입니다. 확정 라벨·사건·학습 "
                "데이터는 자동 생성하지 않았습니다."
            )[:500]
        mode_applied = (
            "assisted_auto"
            if auto_accepted_count > 0
            else "proposal_only"
        )
        run_id = uuid4()
        run_matches = [
            {
                "project_id": decision["project_id"],
                "analysis_job_id": decision["analysis_job_id"],
                "candidate_id": decision["candidate_id"],
                "issue_type": decision["issue_type"],
                "review_status": decision["review_status"],
                "decision_hash": decision["decision_sha256"],
            }
            for decision in decisions
        ]
        try:
            recorded_run = await run_in_threadpool(
                app.state.auto_label_runs.record_run,
                project_id,
                run_id=run_id,
                recipe_id=expected_recipe_id,
                command_sha256=plan["command_sha256"],
                scope=request.scope,
                mode_requested=request.mode,
                mode_applied=mode_applied,
                issue_type=plan["issue_type"],
                job_id=request.job_id,
                matches=run_matches,
            )
        except AutoLabelRunError:
            raise HTTPException(
                status_code=500,
                detail="The auto-label audit record could not be persisted",
            ) from None
        first_match = None
        if recorded_run["matches"]:
            first_decision = recorded_run["matches"][0]
            first_match = {
                "analysis_job_id": first_decision["analysis_job_id"],
                "candidate_id": first_decision["candidate_id"],
                "issue_type": first_decision["issue_type"],
                "review_status": first_decision["review_status"],
                "decision_hash": first_decision["decision_hash"],
            }
        return AutoLabelRunResponse(
            run_id=run_id,
            recipe_id=expected_recipe_id,
            project_id=project_id,
            scope=request.scope,
            mode_requested=request.mode,
            mode_applied=mode_applied,
            status=status,
            proposal_origin=proposal_origin,
            proposal_generation_status=proposal_generation_status,
            jobs_scanned=len(completed_jobs),
            candidates_evaluated=candidates_evaluated,
            proposed_count=proposed_count,
            auto_accepted_count=auto_accepted_count,
            review_required_count=review_required_count,
            training_labels_created=0,
            first_match=first_match,
            message_ko=message,
        )

    @router.post(
        "/projects/{project_id}/review/temporal-incident-proposals",
        response_model=TemporalIncidentReviewResponse,
    )
    async def review_temporal_incident_proposals(
        project_id: UUID,
        request: TemporalIncidentReviewRequest,
    ) -> TemporalIncidentReviewResponse:
        """Group frame detections without creating a product incident."""

        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Temporal review project identity does not match the route"
                ),
            )
        provenance_project = request.provenance.get("project_id")
        if str(provenance_project) != str(project_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Temporal review provenance does not match the project"
                ),
            )
        raw_analysis_job_id = request.provenance.get("analysis_job_id")
        raw_source_sha256 = request.provenance.get("source_file_sha256")
        try:
            provenance_job_id = UUID(str(raw_analysis_job_id))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(
                status_code=422,
                detail="Temporal review provenance requires an analysis job UUID",
            ) from None
        if (
            not isinstance(raw_source_sha256, str)
            or len(raw_source_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in raw_source_sha256
            )
        ):
            raise HTTPException(
                status_code=422,
                detail="Temporal review provenance requires a source SHA-256",
            )
        analysis_job = app.state.analysis_jobs.get(
            project_id,
            provenance_job_id,
        )
        if (
            str(analysis_job.get("project_id")) != str(project_id)
            or str(analysis_job.get("job_id")) != str(provenance_job_id)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Temporal review analysis job identity does not match "
                    "the project provenance"
                ),
            )
        if (
            analysis_job["status"] != "completed"
            or analysis_job.get("result") is None
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Temporal review requires a completed analysis job"
                ),
            )
        if analysis_job.get("source_sha256") != raw_source_sha256:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Temporal review source SHA-256 does not match "
                    "the completed analysis job"
                ),
            )
        detections = [
            item.model_dump(mode="json", exclude_none=True)
            for item in request.detections
        ]
        policy = TemporalGroupingPolicy(
            **request.policy.model_dump(mode="python")
        )
        try:
            proposals = await run_in_threadpool(
                group_temporal_incident_proposals,
                detections,
                provenance=request.provenance,
                policy=policy,
            )
        except TemporalIncidentProposalError as error:
            raise HTTPException(
                status_code=422,
                detail=f"Temporal review input is invalid: {error}",
            ) from None
        return TemporalIncidentReviewResponse(
            schema_version="trafficvisson.temporal-review-api.v1",
            project_id=project_id,
            computation_mode="pure_review_only",
            proposal_count=len(proposals),
            proposals=proposals,
            state_changes_performed=False,
            incidents_created=0,
            annotations_created=0,
            training_members_created=0,
            model_versions_changed=0,
        )

    @router.post(
        "/projects/{project_id}/review/model-validation",
        response_model=ModelValidationReviewResponse,
    )
    async def review_model_validation(
        project_id: UUID,
        request: ModelValidationReviewRequest,
    ) -> ModelValidationReviewResponse:
        """Compute fixed-set metrics without changing model lifecycle state."""

        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Model validation project identity does not match the route"
                ),
            )
        truth_records = [
            item.model_dump(mode="json")
            for item in request.truth_records
        ]
        prediction_records = [
            item.model_dump(mode="json")
            for item in request.prediction_records
        ]
        try:
            report = await run_in_threadpool(
                evaluate_model,
                model_version=request.model_version,
                truth_records=truth_records,
                prediction_records=prediction_records,
            )
        except ValidationMetricsError as error:
            raise HTTPException(
                status_code=422,
                detail=f"Model validation input is invalid: {error}",
            ) from None
        return ModelValidationReviewResponse(
            schema_version=(
                "trafficvisson.model-validation-review-api.v1"
            ),
            project_id=project_id,
            computation_mode="pure_review_only",
            report=report,
            state_changes_performed=False,
            annotations_created=0,
            training_members_created=0,
            model_versions_changed=0,
        )

    @router.post(
        "/projects/{project_id}/review/model-promotion",
        response_model=ModelPromotionReviewResponse,
    )
    async def review_model_promotion(
        project_id: UUID,
        request: ModelPromotionReviewRequest,
    ) -> ModelPromotionReviewResponse:
        """Evaluate promotion gates without promoting the candidate model."""

        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Model promotion project identity does not match the route"
                ),
            )
        truth_records = [
            item.model_dump(mode="json")
            for item in request.truth_records
        ]
        active_predictions = [
            item.model_dump(mode="json")
            for item in request.active_prediction_records
        ]
        candidate_predictions = [
            item.model_dump(mode="json")
            for item in request.candidate_prediction_records
        ]
        required_issue_types = (
            None
            if request.required_issue_types is None
            else list(request.required_issue_types)
        )
        try:
            decision = await run_in_threadpool(
                compare_candidate_to_active,
                active_model_version=request.active_model_version,
                candidate_model_version=request.candidate_model_version,
                truth_records=truth_records,
                active_prediction_records=active_predictions,
                candidate_prediction_records=candidate_predictions,
                required_issue_types=required_issue_types,
                minimum_samples_per_type=(
                    request.minimum_samples_per_type
                ),
                maximum_recall_drop=request.maximum_recall_drop,
            )
        except ValidationMetricsError as error:
            raise HTTPException(
                status_code=422,
                detail=f"Model promotion review input is invalid: {error}",
            ) from None
        return ModelPromotionReviewResponse(
            schema_version=(
                "trafficvisson.model-promotion-review-api.v1"
            ),
            project_id=project_id,
            computation_mode="pure_review_only",
            decision=decision,
            state_changes_performed=False,
            annotations_created=0,
            training_members_created=0,
            model_versions_changed=0,
            automatic_promotion_performed=False,
        )

    @router.get(
        "/projects/{project_id}/questions",
        response_model=QuestionListResponse,
    )
    async def list_questions(
        project_id: UUID,
        status: Optional[QuestionStatus] = Query(default=None),
        policy_mode: PolicyMode = Query(default="learning_assist"),
    ) -> QuestionListResponse:
        questions = app.state.store.list_questions(
            project_id, status, policy_mode
        )
        return QuestionListResponse(
            policy_mode=policy_mode, questions=questions, count=len(questions)
        )

    @router.post(
        "/projects/{project_id}/questions/{question_id}/answer",
        response_model=QuestionAnswerResponse,
    )
    async def answer_question(
        project_id: UUID, question_id: UUID, request: QuestionAnswerRequest
    ) -> QuestionAnswerResponse:
        return QuestionAnswerResponse(
            **app.state.store.answer_question(project_id, question_id, request)
        )

    @router.get(
        "/projects/{project_id}/history",
        response_model=HistoryResponse,
    )
    async def history(
        project_id: UUID, trip_id: UUID = Query(...)
    ) -> HistoryResponse:
        return HistoryResponse(**app.state.store.history(project_id, trip_id))

    @router.post(
        "/projects/{project_id}/trips/{trip_id}/gpx",
        response_model=GpxTrackResponse,
        status_code=201,
    )
    async def import_gpx_track(
        project_id: UUID,
        trip_id: UUID,
        request: GpxTrackImportRequest,
    ) -> GpxTrackResponse:
        try:
            result = await run_in_threadpool(
                app.state.gpx_tracks.import_track,
                project_id,
                trip_id,
                request.selected_path,
                request.video_offset_seconds,
            )
        except GpxTrackError as error:
            raise gpx_http_error(error) from error
        return GpxTrackResponse(**result)

    @router.get(
        "/projects/{project_id}/trips/{trip_id}/gpx",
        response_model=GpxTrackResponse,
    )
    async def get_gpx_track(
        project_id: UUID,
        trip_id: UUID,
    ) -> GpxTrackResponse:
        try:
            result = await run_in_threadpool(
                app.state.gpx_tracks.get_track,
                project_id,
                trip_id,
            )
        except GpxTrackError as error:
            raise gpx_http_error(error) from error
        return GpxTrackResponse(**result)

    @router.post(
        "/projects/{project_id}/trips/{trip_id}/gpx/offset",
        response_model=GpxTrackResponse,
    )
    async def update_gpx_offset(
        project_id: UUID,
        trip_id: UUID,
        request: GpxTrackOffsetRequest,
    ) -> GpxTrackResponse:
        try:
            result = await run_in_threadpool(
                app.state.gpx_tracks.update_video_offset,
                project_id,
                trip_id,
                request.video_offset_seconds,
            )
        except GpxTrackError as error:
            raise gpx_http_error(error) from error
        return GpxTrackResponse(**result)

    @router.get(
        "/runtime/readiness",
        response_model=RuntimeReadinessResponse,
    )
    async def runtime_readiness() -> RuntimeReadinessResponse:
        snapshot = await run_in_threadpool(
            app.state.runtime_readiness.snapshot
        )
        return RuntimeReadinessResponse(**snapshot)

    @router.get("/providers", response_model=ProviderListResponse)
    async def providers() -> ProviderListResponse:
        if fixture_mode:
            return ProviderListResponse(providers=app.state.store.providers())
        provider_rows = []
        for provider_name, display_name in (
            ("codex", "Codex CLI"),
            ("claude", "Claude Code"),
            ("local", "Local Model - 준비 중"),
        ):
            status = app.state.provider_status.get_status(provider_name)
            capabilities = set(status.capabilities)
            if status.state == "ready":
                response_state = "ready"
            elif status.state in {"not_connected", "installed"}:
                response_state = "not_connected"
            elif status.state == "coming_soon":
                response_state = "coming_soon"
            else:
                response_state = "unavailable"
            provider_rows.append(
                {
                    "provider": provider_name,
                    "display_name": display_name,
                    "state": response_state,
                    "authentication_owner": (
                        "not_applicable"
                        if provider_name == "local"
                        else "official_cli"
                    ),
                    "capabilities": {
                        "structured_judgment": (
                            "structured_judgment" in capabilities
                        ),
                        "bulk_frame_detection": False,
                        "report_draft": "report_draft" in capabilities,
                        "document_edit_plan": (
                            "document_edit_plan" in capabilities
                        ),
                        "usage_information": False,
                    },
                    "message": status.message,
                    "diagnostic_state": status.state,
                    "installation_state": status.installation_state,
                    "authentication_state": status.authentication_state,
                    "network_state": status.network_state,
                    "version": status.version,
                }
            )
        return ProviderListResponse(providers=provider_rows)

    @router.post(
        "/providers/{provider}/login",
        response_model=ProviderLoginResponse,
    )
    async def provider_login(
        provider: str, _request: EmptyRequest
    ) -> ProviderLoginResponse:
        if provider not in {"codex", "claude"}:
            raise HTTPException(status_code=404, detail="Provider was not found")
        if not fixture_mode:
            result = app.state.provider_status.launch_login(provider)
            if not result.launched:
                raise HTTPException(status_code=409, detail=result.message)
            message = result.message
        else:
            message = (
                "공식 CLI가 로그인 흐름을 소유합니다. TrafficSuperVision은 인증 "
                "파일이나 토큰을 읽지 않습니다."
            )
        return ProviderLoginResponse(
            provider=provider,
            state="external_cli_owned",
            message=message,
        )

    @router.post(
        "/projects/{project_id}/reports/generate",
        response_model=ReportGenerateResponse,
        status_code=201,
    )
    async def generate_report(
        project_id: UUID, request: ReportGenerateRequest
    ) -> ReportGenerateResponse:
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        app.state.store.get_project(project_id)
        if not request.consent:
            raise HTTPException(
                status_code=412,
                detail="Explicit AI transmission consent is required",
            )

        analysis = await run_in_threadpool(
            app.state.analysis_jobs.get,
            project_id,
            request.analysis_job_id,
        )
        if (
            analysis.get("status") != "completed"
            or not isinstance(analysis.get("result"), dict)
        ):
            raise HTTPException(
                status_code=409,
                detail="The selected analysis job is not completed",
            )
        if analysis.get("source_sha256") != request.source_file_sha256:
            raise HTTPException(
                status_code=409,
                detail="The selected analysis provenance does not match",
            )
        provenance = analysis["result"].get("provenance")
        if (
            not isinstance(provenance, dict)
            or provenance.get("source_sha256")
            != request.source_file_sha256
        ):
            raise HTTPException(
                status_code=500,
                detail="The completed analysis provenance is invalid",
            )

        if request.pipeline_job_id is not None:
            try:
                pipeline_job = await run_in_threadpool(
                    app.state.pipeline_jobs.get,
                    project_id,
                    request.pipeline_job_id,
                )
            except (
                PipelineJobNotFound,
                PipelineJobUnavailable,
                ValueError,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="The selected pipeline provenance does not match",
                ) from None
            pipeline_provenance = pipeline_job.get("provenance")
            pipeline_stages = pipeline_job.get("stages")
            sha_stage = (
                next(
                    (
                        stage
                        for stage in pipeline_stages
                        if isinstance(stage, dict)
                        and stage.get("name") == "sha256"
                    ),
                    None,
                )
                if isinstance(pipeline_stages, list)
                else None
            )
            sha_checkpoint = (
                sha_stage.get("checkpoint")
                if isinstance(sha_stage, dict)
                else None
            )
            if (
                pipeline_job.get("status")
                not in {"awaiting_review", "completed"}
                or str(pipeline_job.get("project_id")) != str(project_id)
                or str(pipeline_job.get("snapshot_id"))
                != str(analysis.get("snapshot_id"))
                or pipeline_job.get("input_fingerprint")
                != request.pipeline_input_fingerprint
                or not isinstance(pipeline_provenance, dict)
                or pipeline_provenance.get("profile_sha256")
                != request.pipeline_profile_sha256
                or not isinstance(sha_checkpoint, dict)
                or sha_checkpoint.get("source_sha256")
                != request.source_file_sha256
            ):
                raise HTTPException(
                    status_code=409,
                    detail="The selected pipeline provenance does not match",
                )

        await reconcile_normalized_labels(project_id)
        active_incidents = await run_in_threadpool(
            app.state.store.list_incidents,
            project_id,
            include_inactive=False,
            analysis_job_id=request.analysis_job_id,
        )
        active_by_id = {}
        for incident in active_incidents:
            incident_id = str(incident.get("incident_id", ""))
            if incident_id in active_by_id:
                raise HTTPException(
                    status_code=500,
                    detail="The active incident catalog contains duplicate IDs",
                )
            active_by_id[incident_id] = incident
        selected_incidents = []
        for incident_id in request.incident_ids:
            incident = active_by_id.get(str(incident_id))
            if incident is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Every selected incident must be an active approved "
                        "incident in the exact project and analysis job"
                    ),
                )
            if (
                incident.get("status") != "active"
                or str(incident.get("project_id")) != str(project_id)
                or str(incident.get("analysis_job_id"))
                != str(request.analysis_job_id)
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Every selected incident must be an active approved "
                        "incident in the exact project and analysis job"
                    ),
                )
            selected_incidents.append(incident)

        try:
            gpx_track = await run_in_threadpool(
                app.state.gpx_tracks.get_track,
                project_id,
                request.analysis_job_id,
            )
        except GpxTrackError as error:
            if error.code not in {"track_not_found", "trip_unavailable"}:
                raise HTTPException(
                    status_code=409,
                    detail="The attached GPS track failed verification",
                ) from error
            gpx_track = None

        provider_status = app.state.provider_status.get_status(request.provider)
        provider_capabilities = set(provider_status.capabilities)
        if (
            provider_status.state != "ready"
            or "report_draft" not in provider_capabilities
            or "document_edit_plan" not in provider_capabilities
        ):
            raise HTTPException(
                status_code=409,
                detail="The selected official CLI provider is not ready",
            )

        approved_incidents = []
        expected_evidence_prefix = (
            f"analysis:{request.analysis_job_id}:"
        )
        for incident in selected_incidents:
            server_evidence_refs = [
                incident.get("evidence_image_ref"),
                incident.get("preview_clip_ref"),
            ]
            if (
                any(
                    not isinstance(reference, str)
                    or not reference.startswith(expected_evidence_prefix)
                    for reference in server_evidence_refs
                )
                or len(set(server_evidence_refs))
                != len(server_evidence_refs)
            ):
                raise HTTPException(
                    status_code=500,
                    detail="The approved incident evidence provenance is invalid",
                )
            # The language model receives only the still-image reference.
            # Preview video stays local and is never placed in the CLI prompt.
            evidence_refs = [server_evidence_refs[0]]
            try:
                incident_payload = ApprovedReportIncident(
                    incident_id=incident["incident_id"],
                    incident_type=incident["issue_type"],
                    severity=incident.get("severity") or "unassessed",
                    maintenance_action=incident.get("maintenance_action"),
                    ocr_text=incident.get("ocr_text"),
                    location_summary=_report_location_summary(
                        gpx_track,
                        incident["representative_time_ms"],
                    ),
                    time_ms=incident["representative_time_ms"],
                    evidence_refs=evidence_refs,
                ).model_dump(mode="json", exclude_none=True)
            except (KeyError, TypeError, ValueError):
                raise HTTPException(
                    status_code=500,
                    detail="The approved incident catalog entry is invalid",
                ) from None
            approved_incidents.append(incident_payload)

        redacted_payload = {
            "approved_incidents": approved_incidents,
            "document_structure_summary": (
                request.document_structure_summary.model_dump(mode="json")
            ),
            "map_refs": request.map_refs,
        }
        transmitted_fields = [
            "approved_incidents",
            "document_structure_summary",
            "map_refs",
        ]
        if request.project_label is not None:
            redacted_payload["project_label"] = request.project_label
            transmitted_fields.insert(0, "project_label")

        try:
            result = await run_in_threadpool(
                app.state.report_generation.generate,
                request.provider,
                redacted_payload,
            )
        except InvalidRedactedPayload:
            raise HTTPException(
                status_code=422,
                detail="The redacted report payload failed safety validation",
            ) from None
        except ProviderNotReadyError:
            raise HTTPException(
                status_code=409,
                detail="The selected official CLI provider is not ready",
            ) from None
        except ProviderAuthenticationExpiredError:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The selected official CLI login has expired; "
                    "sign in again with the official CLI"
                ),
            ) from None
        except ProviderNetworkError:
            raise HTTPException(
                status_code=503,
                detail=(
                    "The selected official CLI could not reach its "
                    "network service"
                ),
            ) from None
        except InvalidModelOutput:
            raise HTTPException(
                status_code=502,
                detail="The report provider returned an invalid structured result",
            ) from None
        except ReportGenerationError:
            raise HTTPException(
                status_code=502,
                detail="The report provider could not generate a safe result",
            ) from None

        return ReportGenerateResponse(
            generation_id=uuid4(),
            provider=result.provider,
            state="generated_preview_required",
            draft=result.draft,
            plan=result.plan,
            model=result.model,
            usage=result.usage,
            transmitted_fields=transmitted_fields,
        )

    @router.post(
        "/projects/{project_id}/reports/draft",
        response_model=ReportDraftResponse,
    )
    async def create_report_draft(
        project_id: UUID, request: ReportDraftRequest
    ) -> ReportDraftResponse:
        if not fixture_mode:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Production report generation requires the combined "
                    "/reports/generate redacted-payload endpoint"
                ),
            )
        return ReportDraftResponse(
            **app.state.store.create_draft(project_id, request)
        )

    @router.post(
        "/projects/{project_id}/reports/plan",
        response_model=ReportPlanResponse,
    )
    async def create_report_plan(
        project_id: UUID, request: ReportPlanRequest
    ) -> ReportPlanResponse:
        if not fixture_mode:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Production edit plans are returned only by the validated "
                    "/reports/generate endpoint"
                ),
            )
        return ReportPlanResponse(
            **app.state.store.create_plan(project_id, request)
        )

    @router.post(
        "/projects/{project_id}/reports/apply",
        response_model=ReportApplyResponse,
    )
    async def apply_report(
        project_id: UUID, request: ReportApplyRequest
    ) -> ReportApplyResponse:
        if not fixture_mode:
            raise HTTPException(
                status_code=409,
                detail="Production document writes require the HWP preview/apply endpoints",
            )
        return ReportApplyResponse(
            **app.state.store.apply_report(project_id, request)
        )

    @router.post(
        "/projects/{project_id}/reports/accept",
        response_model=ReportDecisionResponse,
    )
    async def accept_report(
        project_id: UUID, request: ReportDecisionRequest
    ) -> ReportDecisionResponse:
        if not fixture_mode:
            raise HTTPException(
                status_code=409,
                detail="Production report decisions require an applied HWP report",
            )
        return ReportDecisionResponse(
            **app.state.store.decide_report(project_id, request, "accepted")
        )

    @router.post(
        "/projects/{project_id}/reports/reject",
        response_model=ReportDecisionResponse,
    )
    async def reject_report(
        project_id: UUID, request: ReportDecisionRequest
    ) -> ReportDecisionResponse:
        if not fixture_mode:
            raise HTTPException(
                status_code=409,
                detail="Production report decisions require an applied HWP report",
            )
        return ReportDecisionResponse(
            **app.state.store.decide_report(project_id, request, "rejected")
        )

    def get_hwp_service() -> HwpComService:
        if app.state.hwp_service is None:
            try:
                app.state.hwp_service = HwpComService()
            except HwpAutomationError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
        return app.state.hwp_service

    def convert_hwp_plan(request: HwpDocumentRequest):
        operations = []
        for operation in request.plan.operations:
            if operation.type not in {"fill_text", "fill_cell"}:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Image, map, and table operations require resolved "
                        "project artifact handles before COM preview"
                    ),
                )
            if ":" not in operation.target:
                raise HTTPException(
                    status_code=422,
                    detail="Document targets must use field: or bookmark: identifiers",
                )
            target_kind, target_name = operation.target.split(":", 1)
            if target_kind not in {"field", "bookmark"} or not target_name:
                raise HTTPException(
                    status_code=422,
                    detail="Document target kind is not supported",
                )
            operations.append(
                {
                    "op": operation.type,
                    "target": {"kind": target_kind, "name": target_name},
                    "text": operation.value,
                }
            )
        return {"version": PLAN_VERSION, "operations": operations}

    @router.get("/hwp/preflight", response_model=HwpPreflightResponse)
    def hwp_preflight() -> HwpPreflightResponse:
        result = (
            app.state.hwp_service.preflight()
            if app.state.hwp_service is not None
            else registry_preflight()
        )
        return HwpPreflightResponse(**result.__dict__)

    @router.post(
        (
            "/projects/{project_id}/pipeline-jobs/{pipeline_job_id}"
            "/reports/exact-preview"
        ),
        response_model=CurrentReportExactPreviewResponse,
    )
    def prepare_current_report_exact_preview(
        project_id: UUID,
        pipeline_job_id: UUID,
        request: CurrentReportExactPreviewRequest,
    ) -> CurrentReportExactPreviewResponse:
        app.state.store.get_project(project_id)
        if (
            request.project_id != project_id
            or request.pipeline_job_id != pipeline_job_id
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "current_report_scope_mismatch",
                    "state": "exact_preview_blocked",
                },
            )
        try:
            context = (
                app.state.pipeline_report_previews.load_exact_context(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    preview_id=request.preview_id,
                )
            )
            metadata = app.state.current_report_drafts.prepare(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=context.analysis_job_id,
                source_path=context.source_path,
                binding_manifest=context.binding,
            )
            draft = app.state.current_report_drafts.load(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                draft_id=metadata.draft_id,
            )
            grant = app.state.current_report_approvals.issue(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=context.analysis_job_id,
                binding=draft.binding_manifest,
                plan=draft.exact_plan,
                document_edit_plan=draft.document_edit_plan,
                writer_result=draft.writer_result,
            )
        except PipelineReportPreviewError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": getattr(
                        error,
                        "code",
                        "report_preview_context_invalid",
                    ),
                    "state": "exact_preview_blocked",
                },
            ) from error
        except CurrentReportDraftError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": error.code,
                    "state": "exact_preview_blocked",
                    "provider_invocation_count": 0,
                    "com_invocation_count": 0,
                    "document_published": False,
                },
            ) from error
        except CurrentReportApprovalError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "current_report_approval_failed",
                    "state": "exact_preview_blocked",
                },
            ) from error
        return CurrentReportExactPreviewResponse(
            schema_version=(
                "trafficvisson.current-report-exact-preview.v1"
            ),
            mode="exact_current_report_preview",
            state=metadata.state,
            draft_id=metadata.draft_id,
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=context.analysis_job_id,
            approved_incident_count=(
                metadata.approved_incident_count
            ),
            binding_manifest_sha256=(
                metadata.binding_manifest_sha256
            ),
            exact_plan_sha256=metadata.exact_plan_sha256,
            document_edit_plan_sha256=(
                metadata.document_edit_plan_sha256
            ),
            accept_reject_rollback_binding_sha256=(
                metadata.accept_reject_rollback_binding_sha256
            ),
            document_edit_plan=metadata.document_edit_plan,
            output_document_sha256=(
                metadata.output_document_sha256
            ),
            render_binding_sha256=metadata.render_binding_sha256,
            record_sha256=metadata.record_sha256,
            operation_count=metadata.operation_count,
            expected_rendered_page_count=(
                metadata.expected_rendered_page_count
            ),
            approval_token=grant.token,
            approval_expires_at=datetime.fromtimestamp(
                grant.expires_at_unix,
                tz=timezone.utc,
            ),
            apply_approval_required=True,
            apply_endpoint_available=True,
            remaining_blocker_codes=[],
            exact_writer_completed=True,
            output_render_validation_status=(
                "not_run_com_validation_required"
            ),
            provider_invocation_count=0,
            com_invocation_count=0,
            document_published=False,
            path_exposed=False,
            idempotent_replay=metadata.idempotent_replay,
        )

    def current_report_application_response(
        metadata,
    ) -> CurrentReportApplicationResponse:
        return CurrentReportApplicationResponse(
            **metadata.__dict__
        )

    def current_report_publication_response(
        metadata,
    ) -> CurrentReportPublicationResponse:
        return CurrentReportPublicationResponse(
            **metadata.__dict__
        )

    @router.post(
        (
            "/projects/{project_id}/pipeline-jobs/{pipeline_job_id}"
            "/reports/exact-apply"
        ),
        response_model=CurrentReportApplicationResponse,
    )
    def apply_current_report_exact(
        project_id: UUID,
        pipeline_job_id: UUID,
        request: CurrentReportExactApplyRequest,
    ) -> CurrentReportApplicationResponse:
        app.state.store.get_project(project_id)
        if (
            request.project_id != project_id
            or request.pipeline_job_id != pipeline_job_id
            or request.user_confirmed is not True
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "current_report_scope_mismatch"},
            )
        try:
            draft = app.state.current_report_drafts.load(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                draft_id=request.draft_id,
            )
            authorization = app.state.current_report_approvals.consume(
                request.approval_token,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=draft.analysis_job_id,
                binding=draft.binding_manifest,
                plan=draft.exact_plan,
                document_edit_plan=draft.document_edit_plan,
                expected_document_edit_plan_sha256=(
                    request.document_edit_plan_sha256
                ),
                expected_accept_reject_rollback_binding_sha256=(
                    request
                    .accept_reject_rollback_binding_sha256
                ),
                writer_result=draft.writer_result,
            )
            metadata = app.state.current_report_applications.apply(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                draft=draft,
                authorization=authorization,
                publisher=(
                    get_hwp_service
                    if isinstance(
                        app.state.current_report_applications,
                        CurrentReportApplicationService,
                    )
                    else get_hwp_service()
                ),
            )
        except CurrentReportDraftError as error:
            raise HTTPException(
                status_code=409,
                detail={"code": error.code},
            ) from error
        except CurrentReportApprovalError as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "current_report_approval_failed"},
            ) from error
        except CurrentReportApplicationError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": error.code,
                    "application_id": error.application_id,
                },
            ) from error
        except HwpAutomationError as error:
            raise HTTPException(
                status_code=422,
                detail={"code": "hwp_com_apply_failed"},
            ) from error
        return current_report_application_response(metadata)

    def decide_current_report_exact(
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        request: CurrentReportDecisionRequest,
        accept: bool,
    ) -> CurrentReportApplicationResponse:
        app.state.store.get_project(project_id)
        if (
            request.project_id != project_id
            or request.pipeline_job_id != pipeline_job_id
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "current_report_scope_mismatch"},
            )
        try:
            metadata = app.state.current_report_applications.decide(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                application_id=request.application_id,
                accept=accept,
                document_edit_plan_sha256=(
                    request.document_edit_plan_sha256
                ),
                accept_reject_rollback_binding_sha256=(
                    request
                    .accept_reject_rollback_binding_sha256
                ),
            )
            if accept:
                app.state.current_report_publications.publish_accepted(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    application_id=request.application_id,
                )
        except CurrentReportApplicationError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": error.code,
                    "application_id": error.application_id,
                },
            ) from error
        except CurrentReportPublicationError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": error.code,
                    "application_id": error.application_id,
                    "publication_id": error.publication_id,
                },
            ) from error
        return current_report_application_response(metadata)

    @router.post(
        (
            "/projects/{project_id}/pipeline-jobs/{pipeline_job_id}"
            "/reports/exact-accept"
        ),
        response_model=CurrentReportApplicationResponse,
    )
    def accept_current_report_exact(
        project_id: UUID,
        pipeline_job_id: UUID,
        request: CurrentReportDecisionRequest,
    ) -> CurrentReportApplicationResponse:
        return decide_current_report_exact(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            request=request,
            accept=True,
        )

    @router.post(
        (
            "/projects/{project_id}/pipeline-jobs/{pipeline_job_id}"
            "/reports/exact-reject"
        ),
        response_model=CurrentReportApplicationResponse,
    )
    def reject_current_report_exact(
        project_id: UUID,
        pipeline_job_id: UUID,
        request: CurrentReportDecisionRequest,
    ) -> CurrentReportApplicationResponse:
        return decide_current_report_exact(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            request=request,
            accept=False,
        )

    @router.post(
        (
            "/projects/{project_id}/pipeline-jobs/{pipeline_job_id}"
            "/reports/exact-rollback"
        ),
        response_model=CurrentReportApplicationResponse,
    )
    def rollback_current_report_exact(
        project_id: UUID,
        pipeline_job_id: UUID,
        request: CurrentReportDecisionRequest,
    ) -> CurrentReportApplicationResponse:
        app.state.store.get_project(project_id)
        if (
            request.project_id != project_id
            or request.pipeline_job_id != pipeline_job_id
            or request.user_confirmed is not True
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "current_report_scope_mismatch"},
            )
        try:
            try:
                app.state.current_report_publications.rollback(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    application_id=request.application_id,
                )
            except CurrentReportPublicationError as error:
                # Accepted records created before the publication ledger was
                # introduced have no external output to remove.
                if error.code != "publication_not_found":
                    raise
            metadata = (
                app.state.current_report_applications.rollback_accepted(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    application_id=request.application_id,
                    document_edit_plan_sha256=(
                        request.document_edit_plan_sha256
                    ),
                    accept_reject_rollback_binding_sha256=(
                        request
                        .accept_reject_rollback_binding_sha256
                    ),
                )
            )
        except CurrentReportApplicationError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": error.code,
                    "application_id": error.application_id,
                },
            ) from error
        except CurrentReportPublicationError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": error.code,
                    "application_id": error.application_id,
                    "publication_id": error.publication_id,
                },
            ) from error
        return current_report_application_response(metadata)

    @router.get(
        (
            "/projects/{project_id}/pipeline-jobs/{pipeline_job_id}"
            "/reports/exact-publication/{application_id}"
        ),
        response_model=CurrentReportPublicationResponse,
    )
    def get_current_report_exact_publication(
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
    ) -> CurrentReportPublicationResponse:
        app.state.store.get_project(project_id)
        try:
            metadata = app.state.current_report_publications.metadata(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                application_id=application_id,
            )
        except CurrentReportPublicationError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": error.code,
                    "application_id": error.application_id,
                    "publication_id": error.publication_id,
                },
            ) from error
        return current_report_publication_response(metadata)

    @router.post(
        "/projects/{project_id}/hwp/sample-layout-preview",
        response_model=HwpSampleLayoutPreviewResponse,
    )
    def preview_sample_hwp_layout(
        project_id: UUID, request: HwpDocumentRequest
    ) -> HwpSampleLayoutPreviewResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            result = preview_sample_layout_plan(
                Path(request.template_path),
                request.plan,
            )
        except HwpxSampleLayoutError as error:
            status_code = (
                409
                if error.code
                in {
                    "sample_template_not_recognized",
                    "document_snapshot_mismatch",
                    "sample_layout_baseline_mismatch",
                }
                else 422
            )
            raise HTTPException(
                status_code=status_code, detail=str(error)
            ) from error
        except HwpxStructureError as error:
            raise HTTPException(
                status_code=422,
                detail="HWPX 양식 구조를 안전하게 검증하지 못했습니다.",
            ) from error
        return HwpSampleLayoutPreviewResponse(**result.to_dict())

    @router.post(
        "/projects/{project_id}/hwp/preview",
        response_model=Union[
            HwpPreviewResponse,
            HwpSampleTargetStructurePreviewResponse,
        ],
    )
    def preview_hwp(
        project_id: UUID, request: HwpDocumentRequest
    ) -> Union[
        HwpPreviewResponse,
        HwpSampleTargetStructurePreviewResponse,
    ]:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        template = Path(request.template_path)
        try:
            classification = classify_hwp_template(template)
        except HwpTemplateGuardError as error:
            status_code = (
                409
                if error.code
                in {
                    "audited_sample_hash_mismatch",
                    "audited_sample_structure_mismatch",
                    "hwpx_suffix_mismatch",
                    "sample_identity_inconsistent",
                    "template_changed_during_classification",
                }
                else 422
            )
            raise HTTPException(
                status_code=status_code, detail=str(error)
            ) from error
        except HwpxSampleLayoutError as error:
            raise HTTPException(
                status_code=409,
                detail=(
                    "감사된 3·4분기 표본이 고정 48쪽 구조 계약을 "
                    "통과하지 못해 COM 미리보기를 차단했습니다."
                ),
            ) from error
        except HwpAutomationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        if classification.kind == "exact_sample":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "exact_current_report_context_required",
                    "state": "legacy_sample_plan_rejected",
                    "required_endpoint": (
                        "/projects/{project_id}/pipeline-jobs/"
                        "{pipeline_job_id}/reports/exact-preview"
                    ),
                    "approved_incident_bound": False,
                    "content_provenance_bound": False,
                    "com_invocation_count": 0,
                    "document_write_count": 0,
                },
            )
        try:
            result = get_hwp_service().execute(
                template,
                convert_hwp_plan(request),
                accept=False,
                expected_fingerprint=classification.fingerprint,
            )
        except HwpAutomationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            approval = app.state.hwp_approvals.issue(
                project_id,
                result.document_fingerprint,
                request.plan,
            )
        except (HwpApprovalError, HwpAutomationError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return HwpPreviewResponse(
            mode=result.mode,
            operation_count=result.operation_count,
            operation_types=dict(result.operation_types),
            structure={
                **result.structure.__dict__,
                "field_names": list(result.structure.field_names),
                "bookmark_names": list(result.structure.bookmark_names),
            },
            message=result.message,
            approval_token=approval.token,
            approval_expires_at=datetime.fromtimestamp(
                approval.expires_at_unix, tz=timezone.utc
            ),
            document_sha256=approval.document_sha256,
            plan_sha256=approval.plan_sha256,
            target_summary={
                "operation_count": approval.target_summary.operation_count,
                "operation_types": dict(
                    approval.target_summary.operation_types
                ),
                "targets": list(approval.target_summary.targets),
            },
        )

    @router.post(
        "/projects/{project_id}/hwp/apply",
        response_model=HwpApplyResponse,
    )
    def apply_hwp(
        project_id: UUID, request: HwpApplyDocumentRequest
    ) -> HwpApplyResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        template = Path(request.template_path)
        if app.state.hwp_sample_approvals.owns(
            request.approval_token
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "legacy_sample_approval_invalidated",
                    "state": "exact_current_report_context_required",
                    "required_endpoint": (
                        "/projects/{project_id}/pipeline-jobs/"
                        "{pipeline_job_id}/reports/exact-preview"
                    ),
                    "com_invocation_count": 0,
                    "document_write_count": 0,
                },
            )
        try:
            expected_fingerprint = app.state.hwp_approvals.consume(
                request.approval_token,
                project_id,
                template,
                request.plan,
            )
        except (HwpApprovalError, HwpAutomationError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        output_path = template.with_name(
            f"{template.stem}_TrafficSuperVision_{uuid4().hex[:8]}{template.suffix}"
        )
        try:
            result = get_hwp_service().execute(
                template,
                convert_hwp_plan(request),
                accept=True,
                output_path=output_path,
                expected_fingerprint=expected_fingerprint,
            )
        except HwpAutomationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        try:
            app.state.hwp_rollback_scopes.bind(
                project_id,
                result.rollback_token,
            )
        except HwpApprovalError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return HwpApplyResponse(
            mode=result.mode,
            rollback_token=result.rollback_token,
            output_file_name=result.output_path.name,
            size_bytes=result.size_bytes,
            sha256=result.sha256,
            readback={
                **result.readback.__dict__,
                "field_names": list(result.readback.field_names),
                "bookmark_names": list(result.readback.bookmark_names),
            },
            message=result.message,
        )

    @router.post(
        "/projects/{project_id}/hwp/rollback",
        response_model=HwpRollbackResponse,
    )
    def rollback_hwp(
        project_id: UUID, request: HwpRollbackRequest
    ) -> HwpRollbackResponse:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        try:
            app.state.hwp_rollback_scopes.authorize(
                project_id,
                request.rollback_token,
            )
            result = get_hwp_service().rollback(request.rollback_token)
        except HwpApprovalError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except HwpAutomationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return HwpRollbackResponse(
            rolled_back=True,
            output_file_name=result.output_path.name,
            size_bytes=result.size_bytes,
            sha256=result.sha256,
            message=result.message,
        )

    @router.post(
        "/projects/{project_id}/exports",
        response_model=ExportCreateResponse,
    )
    async def create_export(
        project_id: UUID,
        request: ExportCreateRequest,
        http_request: Request,
    ) -> ExportCreateResponse:
        if not fixture_mode:
            project_record = app.state.store.get_project(project_id)
            if request.project_id != project_id:
                raise HTTPException(
                    status_code=409,
                    detail="Project identity does not match the route",
                )
            required = {
                "raw_detections",
                "reviewed_incidents",
                "label_questions",
                "annotations",
                "ocr_observations",
                "route_points",
                "maintenance_actions",
                "report_payload",
                "manifest",
            }
            if set(request.artifacts) != required:
                raise HTTPException(
                    status_code=409,
                    detail="A traceable project package requires all contracted artifacts",
                )
            if request.analysis_job_id is None:
                raise HTTPException(
                    status_code=409,
                    detail="The exact completed analysis job must be selected for export",
                )
            analysis = app.state.analysis_jobs.get(
                project_id, request.analysis_job_id
            )
            if (
                analysis.get("status") != "completed"
                or analysis.get("result") is None
            ):
                raise HTTPException(
                    status_code=409,
                    detail="The selected analysis job is not completed",
                )
            result_payload = analysis["result"]
            candidates = result_payload["candidates"]
            await reconcile_normalized_labels(project_id)
            try:
                label_audit_records = await run_in_threadpool(
                    app.state.candidate_labels.audit_records,
                    project_id,
                    analysis_job_id=analysis["job_id"],
                )
            except CandidateLabelCorrupt:
                raise HTTPException(
                    status_code=500,
                    detail="The candidate label audit log failed verification",
                ) from None
            latest_by_question = {}
            latest_indexes = {}
            for index, record in enumerate(label_audit_records):
                latest_by_question[record["question_id"]] = record
                latest_indexes[record["question_id"]] = index
            label_records = sorted(
                latest_by_question.values(),
                key=lambda record: latest_indexes[record["question_id"]],
            )
            label_by_candidate = {
                record["candidate_id"]: record for record in label_records
            }
            active_incidents = await run_in_threadpool(
                app.state.store.list_incidents,
                project_id,
                include_inactive=False,
                analysis_job_id=analysis["job_id"],
            )
            candidate_by_id = {
                candidate["candidate_id"]: candidate
                for candidate in candidates
            }
            if (
                len(active_incidents) > MAX_EVIDENCE_INCIDENTS
                or len(active_incidents) * 2 > MAX_EVIDENCE_FILES
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "The selected analysis has too many active incidents "
                        "for one evidence package"
                    ),
                )
            declared_evidence_bytes = 0
            for incident in active_incidents:
                candidate = candidate_by_id.get(incident["candidate_id"])
                if candidate is None:
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "An active incident is not bound to the selected "
                            "analysis"
                        ),
                    )
                image_size = candidate.get(
                    "representative_image", {}
                ).get("size_bytes")
                clip_size = candidate.get("preview_clip", {}).get(
                    "size_bytes"
                )
                if (
                    isinstance(image_size, bool)
                    or not isinstance(image_size, int)
                    or image_size <= 0
                    or isinstance(clip_size, bool)
                    or not isinstance(clip_size, int)
                    or clip_size <= 0
                ):
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "The selected analysis evidence metadata is "
                            "invalid"
                        ),
                    )
                declared_evidence_bytes += image_size + clip_size
                if declared_evidence_bytes > MAX_EVIDENCE_TOTAL_BYTES:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "The selected incident evidence exceeds the "
                            "package byte limit"
                        ),
                    )
            experimental_by_candidate = await run_in_threadpool(
                app.state.experimental_proposals.latest_for_job,
                project_id,
                analysis["job_id"],
            )
            trip_id = str(analysis["job_id"])
            try:
                gpx_export = await run_in_threadpool(
                    app.state.gpx_tracks.export_snapshot,
                    project_id,
                    analysis["job_id"],
                )
            except GpxTrackError as error:
                if error.code != "track_not_found":
                    raise gpx_http_error(error) from error
                route_points = []
                gpx_track_provenance = {
                    "schema_version": GPX_EXPORT_PROVENANCE_SCHEMA,
                    "status": "not_attached",
                    "project_id": str(project_id),
                    "trip_id": trip_id,
                }
            else:
                route_points = gpx_export["route_points"]
                gpx_track_provenance = gpx_export["provenance"]

            empty_proposal_provenance = {
                "proposal_origin": "",
                "review_only": "",
                "automatic_labeling_allowed": "",
                "model_provider": "",
                "model_pack_id": "",
                "model_pack_version": "",
                "model_manifest_sha256": "",
                "dataset_id": "",
                "dataset_version": "",
                "dataset_sha256": "",
                "proposal_run_id": "",
                "evaluation_id": "",
                "evaluation_producer": "",
                "evaluation_producer_version": "",
                "analysis_candidate_sha256": "",
                "representative_image_sha256": "",
            }

            def sidecar_provenance(proposal):
                model = proposal["model"]
                dataset = proposal.get("dataset") or {}
                evaluation = proposal["evaluation"]
                source = proposal["source"]
                return {
                    "proposal_origin": "experimental_sidecar",
                    "review_only": proposal["review_only"],
                    "automatic_labeling_allowed": proposal[
                        "automatic_labeling_allowed"
                    ],
                    "model_provider": model["provider"],
                    "model_pack_id": model["pack_id"],
                    "model_pack_version": model["pack_version"],
                    "model_manifest_sha256": model["manifest_sha256"],
                    "dataset_id": dataset.get("dataset_id", ""),
                    "dataset_version": dataset.get("dataset_version", ""),
                    "dataset_sha256": dataset.get("dataset_sha256", ""),
                    "proposal_run_id": proposal["run_id"],
                    "evaluation_id": evaluation["evaluation_id"],
                    "evaluation_producer": evaluation["producer"],
                    "evaluation_producer_version": evaluation[
                        "producer_version"
                    ],
                    "analysis_candidate_sha256": source[
                        "analysis_candidate_sha256"
                    ],
                    "representative_image_sha256": source[
                        "representative_image_sha256"
                    ],
                }

            def embedded_provenance(inference):
                if inference.get("mode") != "local_model":
                    return dict(empty_proposal_provenance)
                return {
                    **empty_proposal_provenance,
                    "proposal_origin": "embedded_analysis_inference",
                    "review_only": True,
                    "automatic_labeling_allowed": False,
                    "model_provider": inference.get("provider", ""),
                    "model_pack_id": inference.get("pack_id", ""),
                    "model_pack_version": inference.get(
                        "pack_version", ""
                    ),
                    "model_manifest_sha256": inference.get(
                        "manifest_sha256", ""
                    ),
                }

            def score_fields(prediction, *, sidecar):
                calibration = prediction.get("calibration", "")
                semantics = (
                    "zero_shot_relative_not_probability"
                    if calibration == "zero_shot_relative"
                    else (
                        "model_score_not_probability"
                        if calibration == "model_score"
                        else ""
                    )
                )
                if sidecar:
                    value = (
                        prediction.get("relative_score")
                        if calibration == "zero_shot_relative"
                        else prediction.get("model_score")
                    )
                else:
                    value = prediction.get("confidence", "")
                return value if value is not None else "", semantics, calibration

            export_views = {}
            for candidate in candidates:
                candidate_id = candidate["candidate_id"]
                proposal = experimental_by_candidate.get(candidate_id)
                inference = candidate.get("ai_inference") or {}
                if proposal is not None:
                    model = proposal["model"]
                    export_views[candidate_id] = {
                        "sidecar": True,
                        "predictions": proposal["predictions"],
                        "provenance": sidecar_provenance(proposal),
                        "score_semantics": proposal["score_semantics"],
                        "model_version_id": (
                            f"{model['pack_id']}@{model['pack_version']}"
                        ),
                        "source": (
                            f"experimental_sidecar:{model['provider']}"
                        ),
                    }
                else:
                    predictions = (
                        inference.get("predictions", [])
                        if inference.get("mode") == "local_model"
                        else []
                    )
                    export_views[candidate_id] = {
                        "sidecar": False,
                        "predictions": predictions,
                        "provenance": embedded_provenance(inference),
                        "score_semantics": "",
                        "model_version_id": (
                            f"{inference.get('pack_id')}@"
                            f"{inference.get('pack_version')}"
                            if inference.get("pack_id")
                            and inference.get("pack_version")
                            else ""
                        ),
                        "source": (
                            f"local_model:{inference['provider']}"
                            if predictions
                            else "scene_change_and_sharpness_non_classifying"
                        ),
                    }

            raw_detections = []
            candidate_detection_ids = {}
            for candidate in candidates:
                inference = candidate.get("ai_inference") or {}
                view = export_views[candidate["candidate_id"]]
                first_ocr_observation_id = (
                    f"{candidate['candidate_id']}-ocr-1"
                    if inference.get("ocr_observations")
                    else ""
                )
                rows = []
                for index, prediction in enumerate(view["predictions"]):
                    score_value, score_semantics, calibration = score_fields(
                        prediction, sidecar=view["sidecar"]
                    )
                    detection_id = (
                        f"{candidate['candidate_id']}-"
                        f"{'experimental' if view['sidecar'] else 'model'}-"
                        f"{index + 1}"
                    )
                    rows.append(
                        {
                            "detection_id": detection_id,
                            "trip_id": trip_id,
                            "video_id": analysis["source_sha256"],
                            "timestamp_ms": round(
                                candidate[
                                    "representative_timestamp_seconds"
                                ]
                                * 1000
                            ),
                            "frame_index": "",
                            "type": prediction["issue_type"],
                            "confidence": (
                                ""
                                if view["sidecar"]
                                else prediction["confidence"]
                            ),
                            "score_value": score_value,
                            "score_semantics": score_semantics,
                            "score_calibration": calibration,
                            **view["provenance"],
                            "severity": "",
                            "bbox": prediction["box"] or "",
                            "mask_ref": "",
                            "ocr_observation_id": (
                                first_ocr_observation_id
                            ),
                            "latitude": "",
                            "longitude": "",
                            "model_version_id": view["model_version_id"],
                            "source": view["source"],
                        }
                    )
                if not rows:
                    no_suggestion_provenance = (
                        view["provenance"]
                        if view["sidecar"]
                        else empty_proposal_provenance
                    )
                    rows.append(
                        {
                            "detection_id": candidate["candidate_id"],
                            "trip_id": trip_id,
                            "video_id": analysis["source_sha256"],
                            "timestamp_ms": round(
                                candidate[
                                    "representative_timestamp_seconds"
                                ]
                                * 1000
                            ),
                            "frame_index": "",
                            "type": "UNCLASSIFIED_SCENE_CANDIDATE",
                            "confidence": "",
                            "score_value": "",
                            "score_semantics": (
                                view["score_semantics"]
                                if view["sidecar"]
                                else ""
                            ),
                            "score_calibration": "",
                            **no_suggestion_provenance,
                            "severity": "",
                            "bbox": "",
                            "mask_ref": "",
                            "ocr_observation_id": (
                                first_ocr_observation_id
                            ),
                            "latitude": "",
                            "longitude": "",
                            "model_version_id": view["model_version_id"],
                            "source": (
                                (
                                    "experimental_sidecar:no_suggestion"
                                    if view["sidecar"]
                                    else "scene_change_and_sharpness_non_classifying"
                                )
                            ),
                        }
                    )
                raw_detections.extend(rows)
                candidate_detection_ids[candidate["candidate_id"]] = [
                    row["detection_id"] for row in rows
                ]

            label_questions = []
            for item in candidates:
                view = export_views[item["candidate_id"]]
                top_prediction = (
                    view["predictions"][0]
                    if view["predictions"]
                    else None
                )
                if top_prediction is not None:
                    score_value, score_semantics, calibration = score_fields(
                        top_prediction, sidecar=view["sidecar"]
                    )
                else:
                    score_value = ""
                    score_semantics = (
                        view["score_semantics"]
                        if view["sidecar"]
                        else ""
                    )
                    calibration = ""
                question_provenance = (
                    view["provenance"]
                    if top_prediction is not None
                    or view["sidecar"]
                    else empty_proposal_provenance
                )
                label_questions.append(
                    {
                    "question_id": str(
                        CandidateLabelStore.question_id(
                            analysis["job_id"], item["candidate_id"]
                        )
                    ),
                    "project_id": str(project_id),
                    "trip_id": trip_id,
                    "candidate_incident_id": item["candidate_id"],
                    "prompt": "이 장면에 점검이 필요한 도로 문제가 있나요?",
                    "policy": project_record["question_policy"],
                    "proposed_type": (
                        top_prediction["issue_type"]
                        if top_prediction is not None
                        else ""
                    ),
                    "proposed_confidence": (
                        ""
                        if view["sidecar"]
                        else (
                            top_prediction["confidence"]
                            if top_prediction is not None
                            else ""
                        )
                    ),
                    "proposed_score_value": score_value,
                    "proposed_score_semantics": score_semantics,
                    "proposed_score_calibration": calibration,
                    **question_provenance,
                    "proposed_geometry": (
                        top_prediction.get("box") or ""
                        if top_prediction is not None
                        else ""
                    ),
                    "evidence_image_ref": item["representative_image"][
                        "artifact_ref"
                    ],
                    "preview_clip_ref": item["preview_clip"]["artifact_ref"],
                    "gps_latitude": "",
                    "gps_longitude": "",
                    "mandatory": (
                        (
                            top_prediction is not None
                            and top_prediction["issue_type"] == "road_debris"
                        )
                        or (
                            item["candidate_id"] in label_by_candidate
                            and (
                                label_by_candidate[item["candidate_id"]][
                                    "final_answer"
                                ]["selected_issue_type"]
                                == "road_debris"
                                or label_by_candidate[item["candidate_id"]][
                                    "final_answer"
                                ]["severity"]
                                == "urgent"
                            )
                        )
                    ),
                    "created_at": analysis["updated_at"].isoformat(),
                    }
                )
            annotations = []
            reviewed_incidents = []
            ocr_observations = []
            maintenance_actions = []
            evidence_sources = []
            for candidate in candidates:
                inference = candidate.get("ai_inference") or {}
                model_version_id = (
                    f"{inference.get('pack_id')}@"
                    f"{inference.get('pack_version')}"
                    if inference.get("pack_id")
                    and inference.get("pack_version")
                    else ""
                )
                for index, observation in enumerate(
                    inference.get("ocr_observations", [])
                ):
                    ocr_observations.append(
                        {
                            "ocr_observation_id": (
                                f"{candidate['candidate_id']}-ocr-"
                                f"{index + 1}"
                            ),
                            "trip_id": trip_id,
                            "incident_id": "",
                            "timestamp_ms": round(
                                candidate[
                                    "representative_timestamp_seconds"
                                ]
                                * 1000
                            ),
                            "raw_text": observation["text"],
                            "normalized_text": observation["text"],
                            "candidate_name": observation["text"],
                            "kind": observation["kind"],
                            "confidence": observation["confidence"],
                            "verified": False,
                            "verification_source": "local_model_unverified",
                            "region": observation["box"],
                            "model_version_id": model_version_id,
                        }
                    )
            for record in label_records:
                answer = record["final_answer"]
                candidate = candidate_by_id.get(record["candidate_id"])
                if candidate is None:
                    raise HTTPException(
                        status_code=500,
                        detail="A label references a missing analysis candidate",
                    )
                annotations.append(
                    {
                        "annotation_id": record["record_id"],
                        "question_id": record["question_id"],
                        "project_id": str(project_id),
                        "trip_id": trip_id,
                        "type": answer["selected_issue_type"] or "",
                        "geometry": answer["normalized_box"] or "",
                        "ocr_text": answer["ocr_text"] or "",
                        "action_label": answer["maintenance_action"] or "",
                        "severity": answer["severity"] or "",
                        "answer": answer["answer"],
                        "review_status": record["partition"],
                        "label_source": "local_user_review",
                        "created_at": record["recorded_at"],
                        "dataset_split": (
                            "pending_dataset_version"
                            if answer["include_in_training"]
                            else ""
                        ),
                    }
                )

            for incident in active_incidents:
                candidate = candidate_by_id.get(incident["candidate_id"])
                record = label_by_candidate.get(incident["candidate_id"])
                if candidate is None or record is None:
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "An active incident is not bound to the selected "
                            "analysis audit"
                        ),
                    )
                answer = record["final_answer"]
                expected_image_ref = candidate["representative_image"][
                    "artifact_ref"
                ]
                expected_clip_ref = candidate["preview_clip"]["artifact_ref"]
                if (
                    incident["status"] != "active"
                    or incident["project_id"] != str(project_id)
                    or incident["analysis_job_id"] != str(
                        analysis["job_id"]
                    )
                    or incident["question_id"] != record["question_id"]
                    or incident["incident_id"] != record["question_id"]
                    or incident["candidate_id"] != record["candidate_id"]
                    or incident["answer_revision"]
                    != record["answer_revision"]
                    or incident["source_answer_record_id"]
                    != record["record_id"]
                    or incident["source_record_hash"]
                    != record["record_hash"]
                    or record["partition"] != "approved"
                    or answer["answer"] not in {"yes", "edit"}
                    or incident["issue_type"]
                    != answer["selected_issue_type"]
                    or incident["severity"] != answer["severity"]
                    or incident["maintenance_action"]
                    != answer["maintenance_action"]
                    or incident["ocr_text"] != answer["ocr_text"]
                    or incident["evidence_image_ref"]
                    != expected_image_ref
                    or incident["preview_clip_ref"] != expected_clip_ref
                    or incident["start_time_ms"]
                    != round(candidate["start_seconds"] * 1000)
                    or incident["representative_time_ms"]
                    != round(
                        candidate[
                            "representative_timestamp_seconds"
                        ]
                        * 1000
                    )
                    or incident["end_time_ms"]
                    != round(candidate["end_seconds"] * 1000)
                ):
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "The active incident projection failed audit "
                            "verification"
                        ),
                    )
                image_artifact = await run_in_threadpool(
                    app.state.analysis_jobs.resolve_artifact,
                    project_id,
                    analysis["job_id"],
                    incident["candidate_id"],
                    "image",
                )
                clip_artifact = await run_in_threadpool(
                    app.state.analysis_jobs.resolve_artifact,
                    project_id,
                    analysis["job_id"],
                    incident["candidate_id"],
                    "clip",
                )
                image_source = EvidenceExportSource(
                    incident_id=incident["incident_id"],
                    kind="image",
                    path=image_artifact.path,
                    media_type=image_artifact.media_type,
                    size_bytes=image_artifact.size_bytes,
                    sha256=image_artifact.sha256,
                )
                clip_source = EvidenceExportSource(
                    incident_id=incident["incident_id"],
                    kind="clip",
                    path=clip_artifact.path,
                    media_type=clip_artifact.media_type,
                    size_bytes=clip_artifact.size_bytes,
                    sha256=clip_artifact.sha256,
                )
                evidence_sources.extend((image_source, clip_source))
                reviewed_incidents.append(
                    {
                        "incident_id": incident["incident_id"],
                        "trip_id": trip_id,
                        "detection_ids": candidate_detection_ids[
                            candidate["candidate_id"]
                        ],
                        "event_type": answer["selected_issue_type"],
                        "severity": answer["severity"] or "",
                        "maintenance_required": True,
                        "recommended_action": (
                            answer["maintenance_action"] or ""
                        ),
                        "review_status": "approved",
                        "start_ms": round(candidate["start_seconds"] * 1000),
                        "end_ms": round(candidate["end_seconds"] * 1000),
                        "latitude": "",
                        "longitude": "",
                        "evidence_image_ref": image_source.archive_name,
                        "preview_clip_ref": clip_source.archive_name,
                        "ocr_text": answer["ocr_text"] or "",
                        "location_validation": "gps_not_connected",
                        "model_version_id": export_views[
                            candidate["candidate_id"]
                        ]["model_version_id"],
                        "reviewed_at": record["recorded_at"],
                    }
                )
                if answer["ocr_text"]:
                    ocr_observations.append(
                        {
                            "ocr_observation_id": record["record_id"],
                            "trip_id": trip_id,
                            "incident_id": incident["incident_id"],
                            "timestamp_ms": round(
                                candidate[
                                    "representative_timestamp_seconds"
                                ]
                                * 1000
                            ),
                            "raw_text": answer["ocr_text"],
                            "normalized_text": answer["ocr_text"],
                            "candidate_name": answer["ocr_text"],
                            "kind": answer["selected_issue_type"],
                            "confidence": "",
                            "verified": True,
                            "verification_source": "local_user_review",
                            "region": answer["normalized_box"] or "",
                            "model_version_id": "",
                        }
                    )
                if answer["maintenance_action"]:
                    maintenance_actions.append(
                        {
                            "action_id": record["record_id"],
                            "incident_id": incident["incident_id"],
                            "trip_id": trip_id,
                            "action_type": answer["selected_issue_type"],
                            "description": answer["maintenance_action"],
                            "priority": answer["severity"] or "",
                            "severity": answer["severity"] or "",
                            "status": "confirmed",
                            "due_date": "",
                            "latitude": "",
                            "longitude": "",
                            "confirmed_by": "local_user",
                            "confirmed_at": record["recorded_at"],
                        }
                    )

            selected_sidecars = sorted(
                experimental_by_candidate.values(),
                key=lambda proposal: (
                    proposal["run_id"],
                    proposal["candidate_id"],
                ),
            )
            run_groups = {}
            model_identities = {}
            evaluation_identities = {}
            dataset_identities = {}
            selected_proposal_hashes = []
            for proposal in selected_sidecars:
                proposal_hash = _canonical_export_provenance_hash(proposal)
                selected_proposal_hashes.append(
                    {
                        "candidate_id": proposal["candidate_id"],
                        "proposal_sha256": proposal_hash,
                    }
                )
                run_groups.setdefault(proposal["run_id"], []).append(
                    {
                        "candidate_id": proposal["candidate_id"],
                        "proposal_sha256": proposal_hash,
                    }
                )
                model = proposal["model"]
                model_hash = _canonical_export_provenance_hash(model)
                model_identities[model_hash] = {
                    **model,
                    "identity_sha256": model_hash,
                }
                evaluation = proposal["evaluation"]
                evaluation_hash = _canonical_export_provenance_hash(
                    evaluation
                )
                evaluation_identities[evaluation_hash] = {
                    **evaluation,
                    "identity_sha256": evaluation_hash,
                }
                dataset = proposal.get("dataset")
                if dataset is not None:
                    dataset_hash = _canonical_export_provenance_hash(
                        dataset
                    )
                    dataset_identities[dataset_hash] = {
                        **dataset,
                        "identity_sha256": dataset_hash,
                    }
            run_provenance = []
            for run_id, selected in sorted(run_groups.items()):
                selected.sort(key=lambda item: item["candidate_id"])
                run_provenance.append(
                    {
                        "run_id": run_id,
                        "selected_proposal_count": len(selected),
                        "selected_proposals_sha256": (
                            _canonical_export_provenance_hash(selected)
                        ),
                    }
                )
            experimental_sidecar_provenance = {
                "schema_version": (
                    "trafficvisson.experimental-export-provenance.v1"
                ),
                "source": "validated_latest_for_job",
                "selected_proposal_count": len(selected_sidecars),
                "classified_count": sum(
                    proposal["status"] == "classified"
                    for proposal in selected_sidecars
                ),
                "no_suggestion_count": sum(
                    proposal["status"] == "no_suggestion"
                    for proposal in selected_sidecars
                ),
                "prediction_count": sum(
                    len(proposal["predictions"])
                    for proposal in selected_sidecars
                ),
                "selection_sha256": _canonical_export_provenance_hash(
                    selected_proposal_hashes
                ),
                "runs": run_provenance,
                "models": [
                    model_identities[key]
                    for key in sorted(model_identities)
                ],
                "evaluations": [
                    evaluation_identities[key]
                    for key in sorted(evaluation_identities)
                ],
                "datasets": [
                    dataset_identities[key]
                    for key in sorted(dataset_identities)
                ],
                "review_only": True,
                "automatic_labeling_allowed": False,
                "excluded_from_confirmed_outputs_without_user_approval": True,
                "eligible_as_evidence_after_user_approval": True,
            }
            if request.destination_path:
                export_root = Path(request.destination_path)
            else:
                export_root = app.state.bridge_data_root / "exports"
                export_root.mkdir(parents=True, exist_ok=True)
            try:
                completed = await _run_export_with_disconnect_cancellation(
                    http_request,
                    export_root,
                    {
                        "trip": {
                            "trip_id": trip_id,
                            "project_id": str(project_id),
                            "source_file": analysis["file_name"],
                            "source_sha256": analysis["source_sha256"],
                            "analysis_job_id": str(analysis["job_id"]),
                        },
                        "detections": raw_detections,
                        "incidents": reviewed_incidents,
                        "label_questions": label_questions,
                        "annotations": annotations,
                        "ocr_observations": ocr_observations,
                        "route_points": route_points,
                        "maintenance_actions": maintenance_actions,
                        "report_payload": {
                            "state": (
                                "reviewed"
                                if label_records
                                else "candidate_review_required"
                            ),
                            "candidate_count": result_payload[
                                "candidate_count"
                            ],
                            "reviewed_count": len(label_records),
                            "approved_incident_count": len(
                                reviewed_incidents
                            ),
                            "approved_incidents": reviewed_incidents,
                        },
                        "provenance": {
                            "analysis_job_id": str(analysis["job_id"]),
                            "artifact_set_id": str(
                                result_payload["artifact_set_id"]
                            ),
                            "source_sha256": analysis["source_sha256"],
                            "candidate_method": (
                                "local_model_plus_scene_candidate"
                                if result_payload.get(
                                    "vision_inference", {}
                                ).get("mode")
                                == "local_model"
                                else "scene_change_and_sharpness_non_classifying"
                            ),
                            "vision_inference": result_payload.get(
                                "vision_inference"
                            ),
                            "experimental_sidecars": (
                                experimental_sidecar_provenance
                            ),
                            "label_record_hashes": [
                                record["record_hash"]
                                for record in label_audit_records
                            ],
                            "label_audit_record_count": len(
                                label_audit_records
                            ),
                            "label_latest_record_count": len(label_records),
                            "gpx_track": gpx_track_provenance,
                            "incident_projection": {
                                "catalog_schema_version": 4,
                                "selection": (
                                    "active_exact_analysis_job"
                                ),
                                "active_incident_count": len(
                                    active_incidents
                                ),
                                "incidents": [
                                    {
                                        "incident_id": item["incident_id"],
                                        "answer_revision": item[
                                            "answer_revision"
                                        ],
                                        "source_answer_record_id": item[
                                            "source_answer_record_id"
                                        ],
                                        "source_record_hash": item[
                                            "source_record_hash"
                                        ],
                                    }
                                    for item in active_incidents
                                ],
                            },
                        },
                    },
                    tuple(evidence_sources),
                )
            except ArtifactExportError as error:
                raise HTTPException(
                    status_code=500,
                    detail="The export package could not be verified",
                ) from error
            archive_digest = next(
                item
                for item in completed.artifacts
                if item.name == completed.archive_path.name
            )
            artifact_file_names = {
                "raw_detections": "raw_detections.csv",
                "reviewed_incidents": "reviewed_incidents.csv",
                "label_questions": "label_questions.csv",
                "annotations": "annotations.csv",
                "ocr_observations": "ocr_observations.csv",
                "route_points": "route_points.csv",
                "maintenance_actions": "maintenance_actions.csv",
                "report_payload": "report_payload.json",
                "manifest": "manifest.json",
            }
            artifact_row_counts = {
                "raw_detections": len(raw_detections),
                "reviewed_incidents": len(reviewed_incidents),
                "label_questions": len(label_questions),
                "annotations": len(annotations),
                "ocr_observations": len(ocr_observations),
                "route_points": len(route_points),
                "maintenance_actions": len(maintenance_actions),
                "report_payload": 1,
                "manifest": 1,
            }
            digests_by_name = {
                item.name: item for item in completed.artifacts
            }
            return ExportCreateResponse(
                export_id=completed.export_id,
                status="completed",
                artifacts=[
                    {
                        "artifact": item,
                        "file_name": artifact_file_names[item],
                        "row_count": artifact_row_counts[item],
                        "size_bytes": digests_by_name[
                            artifact_file_names[item]
                        ].size_bytes,
                        "sha256": digests_by_name[
                            artifact_file_names[item]
                        ].sha256,
                    }
                    for item in request.artifacts
                ],
                manifest_included=True,
                archive_file_name=completed.archive_path.name,
                archive_size_bytes=archive_digest.size_bytes,
                archive_sha256=archive_digest.sha256,
                artifact_count=len(completed.artifacts),
                evidence_incident_count=len(active_incidents),
                evidence_file_count=len(evidence_sources),
                evidence_size_bytes=sum(
                    item.size_bytes for item in evidence_sources
                ),
            )
        return ExportCreateResponse(
            **app.state.store.create_export(project_id, request)
        )

    app.include_router(router)
    return app
