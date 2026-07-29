from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
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
    HwpSampleSemanticPreviewResponse,
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
    PipelineJobResponse,
    PipelineJobStartRequest,
    PipelineJobStartResponse,
    PipelineProfileConfigureRequest,
    PipelineProfileResponse,
    PipelineReviewCompleteRequest,
    ProjectCreateRequest,
    ProjectIncidentListResponse,
    ProjectListResponse,
    ProjectLabelStatsResponse,
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
    WeakCaseSearchRequest,
    WeakCaseSearchResponse,
    WeakCaseApprovalRequest,
    WeakCaseApprovalResponse,
    WeakCaseSourceListResponse,
)
from .analysis_jobs import AnalysisJobService
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
from .command_proposal_generation import (
    CommandProposalGenerationError,
    CommandProposalGenerator,
    FixedManifestCommandProposalGenerator,
)
from .dataset_versions import DatasetVersionError, LabelProjectionError
from .dense_index_jobs import (
    DenseIndexJobConflict,
    DenseIndexJobNotFound,
    DenseIndexJobService,
    DenseIndexJobSourceRejected,
    DenseIndexJobUnavailable,
    ResolvedDenseIndexSource,
)
from .derived_outputs import DerivedOutputsService
from .experimental_proposals import (
    ExperimentalProposalError,
    ExperimentalProposalStore,
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
    HwpSampleApprovalRegistry,
)
from .hwpx_sample_layout import (
    HwpxSampleLayoutError,
    identify_sample_layout,
    preview_sample_layout_plan,
)
from .hwpx_structure import HwpxStructureError
from .gpx_tracks import (
    GPX_EXPORT_PROVENANCE_SCHEMA,
    GpxTrackError,
    GpxTrackStore,
)
from .media_intake import MediaIntakeService
from .pipeline_jobs import (
    BridgePipelineRuntime,
    PipelineJobConflict,
    PipelineJobNotFound,
    PipelineJobService,
    PipelineJobUnavailable,
    PipelineProfileMissing,
    PipelineRuntime,
)
from .provider_status import ProviderStatusService
from .report_generation import (
    InvalidModelOutput,
    InvalidRedactedPayload,
    ProviderAuthenticationExpiredError,
    ProviderNetworkError,
    ProviderNotReadyError,
    ReportGenerationError,
    ReportGenerationService,
)
from .runtime_readiness import RuntimeReadinessService
from .sam2_refinement import (
    Sam2MaskRefinementService,
    Sam2RefinementAdapter,
    Sam2RefinementError,
    Sam2RefinementNotFound,
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


def create_app(
    token: str,
    *,
    fixture_mode: bool = False,
    allow_test_client: bool = False,
    ffmpeg_path: Optional[str] = None,
    ffprobe_path: Optional[str] = None,
    bridge_data_root: Optional[str] = None,
    candidate_extraction_limits: Optional[ExtractionLimits] = None,
    media_stability_min_interval_seconds: Optional[float] = None,
    provider_status_service: Optional[ProviderStatusService] = None,
    report_generation_service: Optional[ReportGenerationService] = None,
    hwp_service: Optional[HwpComService] = None,
    hwp_approval_registry: Optional[HwpApprovalRegistry] = None,
    hwp_sample_approval_registry: Optional[
        HwpSampleApprovalRegistry
    ] = None,
    vision_inference_service: Optional[LocalVisionInferenceService] = None,
    source_candidate_probe: Optional[SourceCandidateProbe] = None,
    command_proposal_generator: Optional[CommandProposalGenerator] = None,
    pipeline_runtime: Optional[PipelineRuntime] = None,
    runtime_readiness_service: Optional[RuntimeReadinessService] = None,
    weak_case_catalog: Optional[WeakCaseCatalog] = None,
    weak_case_approval_store: Optional[WeakCaseApprovalStore] = None,
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
    if vision_inference_service is None:
        vision_inference_service = LocalVisionInferenceService(
            model_pack_path=os.environ.get(
                "TRAFFICSUPERVISION_VISION_MODEL_PACK"
            )
        )
    app.state.vision_inference = vision_inference_service
    app.state.analysis_jobs = AnalysisJobService(
        app.state.media_intake,
        data_root=Path(bridge_data_root),
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        extraction_limits=candidate_extraction_limits,
        vision_inference=vision_inference_service,
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

    app.state.dense_index_jobs = DenseIndexJobService(
        resolve_dense_index_source,
        data_root=bridge_data_path / "dense-index",
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
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
    )
    app.state.sam2_refinements = Sam2MaskRefinementService(
        adapter=sam2_refinement_adapter,
        artifact_resolver=lambda project_id, job_id, candidate_id: (
            app.state.analysis_jobs.resolve_artifact(
                project_id,
                job_id,
                candidate_id,
                "image",
            )
        ),
        question_resolver=app.state.candidate_labels.resolve_question,
    )
    app.state.experimental_proposals = ExperimentalProposalStore(
        bridge_data_path,
        resolve_completed_candidate,
    )
    app.state.derived_outputs = DerivedOutputsService(
        data_root=bridge_data_path,
        candidate_labels=app.state.candidate_labels,
        store=app.state.store,
        analysis_jobs=app.state.analysis_jobs,
        experimental_proposals=app.state.experimental_proposals,
        gpx_tracks=app.state.gpx_tracks,
        # Report consent, exact template semantic mapping and the durable
        # preview preparer are separate approval boundaries. CSV/evidence can
        # be generated now; report preparation remains an honest partial.
        report_preparer=None,
    )
    app.state.auto_label_runs = AutoLabelRunStore(bridge_data_path)
    app.state.command_proposal_generator = (
        command_proposal_generator
        if command_proposal_generator is not None
        else FixedManifestCommandProposalGenerator.from_environment_or_program_data()
    )
    if pipeline_runtime is None:
        pipeline_runtime = BridgePipelineRuntime(
            app.state.media_intake,
            data_root=bridge_data_path,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            vision_status=app.state.vision_inference.status,
            analysis_jobs=app.state.analysis_jobs,
            command_proposal_generator=(
                app.state.command_proposal_generator
            ),
            experimental_proposals=app.state.experimental_proposals,
            candidate_labels=app.state.candidate_labels,
            weak_case_catalog=app.state.weak_case_catalog,
            weak_case_approvals=app.state.weak_case_approvals,
            derived_outputs=app.state.derived_outputs,
        )
    app.state.pipeline_jobs = PipelineJobService(
        pipeline_runtime,
        data_root=bridge_data_path,
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
            sam2_status=app.state.sam2_refinements.runtime_status,
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
        app.state.pipeline_jobs.shutdown()
        app.state.dense_index_jobs.shutdown()
        app.state.analysis_jobs.shutdown()
        app.state.media_intake.shutdown()

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
                app.state.weak_case_catalog.resolve_case_image,
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
        return MediaSnapshotResponse(
            **app.state.media_intake.capture_snapshot(
                project_id, request.directory
            )
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
        return MediaSnapshotResponse(
            **app.state.media_intake.register_local_file(
                project_id, request.selected_path
            )
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
        return MediaStabilityResponse(
            **app.state.media_intake.compare_snapshots(
                project_id, request.snapshot_ids
            )
        )

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
        return AnalysisJobListResponse(
            jobs=app.state.analysis_jobs.list(project_id, limit=limit)
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
            **app.state.analysis_jobs.get(project_id, job_id)
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
        artifact = app.state.analysis_jobs.resolve_artifact(
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
                default_template_id=request.default_template_id,
                default_template_sha256=request.default_template_sha256,
                detector_pack_id=request.detector_pack_id,
                ocr_pack_id=request.ocr_pack_id,
                sam2_pack_id=request.sam2_pack_id,
                report_provider=request.report_provider,
                output_location_id=request.output_location_id,
                candidate_rescan_fps=request.candidate_rescan_fps,
            )
        except (
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
        job = app.state.analysis_jobs.get(project_id, job_id)
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
                await run_in_threadpool(
                    app.state.store.project_label_records,
                    project_id,
                    audit_records,
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
        normalized_box = (
            request.normalized_box.model_dump()
            if request.normalized_box is not None
            else None
        )
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
            )
        except DatasetVersionError:
            raise HTTPException(
                status_code=409,
                detail="At least one reviewed label is required",
            ) from None
        return DatasetVersionResponse(**version)

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
            HwpSampleSemanticPreviewResponse,
        ],
    )
    def preview_hwp(
        project_id: UUID, request: HwpDocumentRequest
    ) -> Union[HwpPreviewResponse, HwpSampleSemanticPreviewResponse]:
        app.state.store.get_project(project_id)
        if request.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="Project identity does not match the route",
            )
        template = Path(request.template_path)
        if template.suffix.casefold() == ".hwpx":
            try:
                protected_sample = identify_sample_layout(template)
            except HwpxSampleLayoutError as error:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "감사된 3·4분기 표본 SHA는 확인되었지만 "
                        "현재 구조가 고정 48쪽 layout contract를 "
                        "통과하지 못해 COM 미리보기를 차단했습니다."
                    ),
                ) from error
            except HwpxStructureError:
                # Legacy field/bookmark HWPX files are still handled by the
                # conservative COM preview path. Exact audited samples are
                # intercepted below before any COM call.
                protected_sample = None
            if protected_sample is not None:
                try:
                    approved_references = app.state.weak_case_approvals.list_project(
                        project_id
                    )["records"]
                    approval = app.state.hwp_sample_approvals.issue(
                        project_id,
                        template,
                        request.plan,
                        approved_references,
                    )
                except WeakCaseApprovalError as error:
                    raise weak_case_approval_http_error(error) from error
                except HwpxSampleLayoutError as error:
                    status_code = (
                        409
                        if error.code
                        in {
                            "approved_reference_required",
                            "approved_reference_hash_mismatch",
                            "approved_reference_state_invalid",
                            "approved_reference_structure_mismatch",
                            "approved_reference_slot_mismatch",
                            "approved_reference_marker_mismatch",
                            "approved_reference_semantic_cells_mismatch",
                            "document_snapshot_mismatch",
                            "sample_layout_baseline_mismatch",
                        }
                        else 422
                    )
                    raise HTTPException(
                        status_code=status_code, detail=str(error)
                    ) from error
                except (HwpApprovalError, HwpAutomationError) as error:
                    raise HTTPException(
                        status_code=409, detail=str(error)
                    ) from error
                validation = approval.validation
                semantic_payload = validation.to_dict()
                semantic_payload["state"] = (
                    "semantic_preview_validated_apply_unavailable"
                )
                return HwpSampleSemanticPreviewResponse(
                    **semantic_payload,
                    mode="sample_semantic_preview",
                    approved_references_sha256=(
                        approval.approved_references_sha256
                    ),
                    approval_scope="sample_preview_hash_binding_only",
                    approval_token=approval.token,
                    approval_expires_at=datetime.fromtimestamp(
                        approval.expires_at_unix,
                        tz=timezone.utc,
                    ),
                    apply_eligible=False,
                    writer_state="unavailable",
                    output_render_validation_status="not_run",
                    blocking_reasons=[
                        "exact_hwpml_target_writer_not_implemented",
                        (
                            "output_reopen_and_48_page_render_validation_"
                            "not_implemented"
                        ),
                    ],
                    no_com=True,
                    no_document_write=True,
                )
        try:
            result = get_hwp_service().execute(
                template,
                convert_hwp_plan(request),
                accept=False,
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
            try:
                validation = app.state.hwp_sample_approvals.consume(
                    request.approval_token,
                    project_id,
                    template,
                    request.plan,
                    lambda: app.state.weak_case_approvals.list_project(
                        project_id
                    )["records"],
                )
            except WeakCaseApprovalError as error:
                raise weak_case_approval_http_error(error) from error
            except (
                HwpApprovalError,
                HwpAutomationError,
                HwpxSampleLayoutError,
            ) as error:
                raise HTTPException(
                    status_code=409, detail=str(error)
                ) from error
            raise HTTPException(
                status_code=409,
                detail={
                    "state": "sample_apply_unavailable",
                    "semantic_validation_sha256": (
                        validation.validation_sha256
                    ),
                    "blocking_reasons": [
                        "exact_hwpml_target_writer_not_implemented",
                        (
                            "output_reopen_and_48_page_render_validation_"
                            "not_implemented"
                        ),
                    ],
                    "com_invocation_performed": False,
                    "document_write_performed": False,
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
            result = get_hwp_service().rollback(request.rollback_token)
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
                    **view["provenance"],
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
