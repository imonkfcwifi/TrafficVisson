"""Durable project-scoped orchestration for the one-video TrafficSuperVision flow.

The pipeline is intentionally stricter than the older screen-by-screen APIs:

* one immutable project profile snapshot is captured at start;
* one input/profile fingerprint makes repeated starts idempotent;
* every stage has a durable checkpoint, outcome and bounded retry count;
* a restart requeues the exact unfinished stage instead of claiming success;
* missing GPS, models and zero detections are distinct outcomes;
* only approved review completion can unlock derived output generation.

This module does not let Codex CLI or Claude Code act as bulk-frame detectors.
Those providers may be selected for the later structured proposal/report stage,
but detector, OCR and segmentation capabilities must come from local model
runtime stages.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import shutil
import stat
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException

from .approved_incident_authority import (
    SURVEY_INCIDENT_AUTHORITY_SCHEMA,
    canonical_sha256,
)
from .analysis_jobs import (
    AnalysisJobService,
    ResolvedAnalysisArtifact,
    ResolvedAnalysisCandidateImageBatch,
)
from .artifact_export import (
    MAX_EVIDENCE_CLIP_BYTES,
    MAX_EVIDENCE_IMAGE_BYTES,
    MAX_EVIDENCE_TOTAL_BYTES,
)
from .auto_labeling import compile_labeling_command
from .candidate_labels import (
    CandidateLabelError,
    CandidateLabelStore,
    canonical_issue_type,
)
from .candidate_evidence_overlay import (
    CandidateEvidenceOverlayCancelled,
    CandidateEvidenceOverlayError,
    CandidateEvidenceOverlayLimits,
    CandidateEvidenceOverlayStore,
    DINO_TEMPORAL_REVIEW_DISCOVERY_SCHEMA,
)
from .composite_candidate_evidence import (
    CandidateEvidenceOverlayAdapter,
    CompositeCandidateEvidence,
)
from .cross_source_sign_merge import (
    evaluate_cross_source_sign_merge,
)
from .dense_candidate_discovery import (
    PROMOTED_MODEL_JOB_BINDING_SCHEMA,
    DenseCandidateDiscoveryResult,
    DenseCandidateDiscoveryCancelled,
    DenseCandidateDiscoveryEngine,
    DenseCandidateDiscoveryError,
    DenseCandidateDiscoveryIntegrityError,
    DenseCandidateSeed,
    capture_promoted_model_job_binding,
    validate_promoted_model_job_binding,
)
from .candidate_window_rescan import (
    CANDIDATE_WINDOW_RESCAN_SCHEMA,
    CandidateWindowRescanCancelled,
    CandidateWindowRescanEngine,
    CandidateWindowRescanError,
    CandidateWindowRescanLimits,
    CandidateWindowRescanManifestInvalid,
    CandidateWindowRescanOutputLimit,
    CandidateWindowRescanSourceChanged,
    CandidateWindowRescanTimeout,
    load_candidate_window_rescan,
    resolve_candidate_window_frame,
)
from .command_proposal_generation import (
    CommandProposalGenerationError,
    CommandProposalGenerationResult,
    CommandProposalGenerator,
)
from .dense_frame_index import (
    DenseFrameIndexCapacityPlan,
    DenseFrameIndexCancelled,
    DenseFrameIndexEngine,
    DenseFrameIndexError,
    DenseFrameIndexLimits,
    DenseFrameIndexOutputLimit,
    DenseFrameIndexSourceChanged,
    DenseFrameIndexStagingUnavailable,
    DenseFrameIndexTimeout,
    build_dense_frame_capacity_plan,
    dense_frame_index_requires_short_staging,
    initialize_dense_staging_root,
    recover_dense_staging_root,
    same_storage_volume,
)
from .derived_outputs import (
    DatasetSnapshotFailed,
    DerivedOutputsConflict,
    DerivedOutputsError,
    DerivedOutputsRequest,
    DerivedOutputsService,
    ResolvedDerivedOutputsArtifactRoot,
)
from .experimental_proposals import (
    ExperimentalProposalError,
    ExperimentalProposalStore,
)
from .media_intake import (
    CompletedMediaGrant,
    MediaFileRecord,
    MediaIntakeService,
    StableMediaFileGrant,
    is_allowlisted_video_name,
)
from .model_iteration_qa import (
    ModelIterationQaError,
    ModelIterationQaStore,
    build_model_iteration,
    build_reviewed_survey_outcomes,
    build_survey_model_qa,
    same_promoted_model_artifact,
)
from .pipeline_sam2_batch import (
    run_pipeline_sam2_batches,
    select_balanced_sam2_frames,
)
from .pipeline_sam2_stage import (
    MAX_FRAMES_PER_CANDIDATE as MAX_SAM2_FRAMES_PER_CANDIDATE,
    PipelineSam2Adapter,
    PipelineSam2StageError,
    Sam2CandidatePrompt,
    Sam2FrameEvidence,
)
from .provider_candidate_evidence_resolver import (
    MAX_ARTIFACT_BYTES as MAX_PROVIDER_CANDIDATE_ARTIFACT_BYTES,
    ProviderCandidateEvidenceResolutionError,
    ResolvedAliasedCandidateFrameArtifact,
    ResolvedCandidateFrameArtifact,
    resolve_aliased_provider_candidate_evidence,
    resolve_provider_candidate_evidence,
)
from .provider_visual_evidence import (
    MAX_OUTPUT_BYTES as MAX_PROVIDER_VISUAL_OUTPUT_BYTES,
    ProviderVisualEvidenceError,
    ProviderVisualEvidencePreparationService,
)
from .project_artifact_roots import (
    DEFAULT_OUTPUT_LOCATION_ID,
    ProjectArtifactRootConflict,
    ProjectArtifactRootError,
    ProjectArtifactRootNotFound,
    ProjectArtifactRootRegistry,
    ProjectArtifactRootUnavailable,
)
from .review_event_set import (
    ReviewEventSetError,
    ReviewEventSetStore,
)
from .report_deidentified_evidence import (
    MAX_OUTPUT_BYTES as MAX_REPORT_EVIDENCE_OUTPUT_BYTES,
)
from .structured_vision_judgment import (
    COMPARISON_MODE as STRUCTURED_VISION_COMPARISON_MODE,
    DIRECT_VISUAL_COMPARISON_MODE as STRUCTURED_VISION_DIRECT_COMPARISON_MODE,
    REQUEST_SCHEMA_VERSION as STRUCTURED_VISION_REQUEST_SCHEMA,
    InvalidStructuredVisionPayload,
    StructuredVisionJudgmentProvider,
)
from .temporal_incidents import (
    TemporalGroupingPolicy,
    TemporalIncidentProposalError,
    group_temporal_incident_proposals,
)
from .weak_case_catalog import WeakCaseCatalog, WeakCaseCatalogError
from .weak_case_approvals import WeakCaseApprovalStore
from .weak_case_visual_index import (
    CandidateFrameEvidence,
    WeakCaseVisualIndexError,
    WeakCaseVisualIndexService,
)
from .vision_inference import (
    VisionInferenceCancelled,
    VisionInferenceError,
)
from .visual_evidence_quality import (
    VISUAL_EVIDENCE_QUALITY_SCHEMA,
    LocalRoadSceneObserver,
    VisualEvidenceQualityError,
    evaluate_candidate_window_visual_quality,
)


PIPELINE_JOB_SCHEMA = "trafficvisson.pipeline-job.v1"
PIPELINE_JOB_ALIAS_SCHEMA = "trafficvisson.pipeline-job-alias.v1"
PIPELINE_JOB_RERUN_SCHEMA = "trafficvisson.pipeline-job-rerun.v1"
PIPELINE_RERUN_MEDIA_PROOF_SCHEMA = (
    "trafficvisson.pipeline-rerun-media-proof.v1"
)
PIPELINE_PROFILE_SCHEMA = "trafficvisson.pipeline-profile.v4"
LEGACY_PIPELINE_PROFILE_SCHEMA = "trafficvisson.pipeline-profile.v1"
PIPELINE_PROFILE_V2_SCHEMA = "trafficvisson.pipeline-profile.v2"
PIPELINE_PROFILE_V3_SCHEMA = "trafficvisson.pipeline-profile.v3"
PRIVATE_MEDIA_GRANT_SCHEMA = "trafficvisson.private-media-grant.v1"
PRIVATE_MEDIA_SNAPSHOT_SCHEMA = (
    "trafficvisson.private-media-snapshot.v1"
)
PIPELINE_INPUT_RESTART_BINDING_SCHEMA = (
    "trafficvisson.pipeline-input-restart-binding.v1"
)
USER_DISCOVERED_QUEUE_REGISTRATION_SCHEMA = (
    "trafficvisson.user-discovered-queue-registration.v1"
)
SURVEY_QA_COMPLETION_SCHEMA = (
    "trafficvisson.promoted-model-survey-qa-completion.v1"
)
ZERO_CANDIDATE_SURVEY_QUEUE_SCHEMA = (
    "trafficvisson.zero-candidate-promoted-survey-queue.v1"
)
LEGACY_ZERO_DETECTION_QUEUE_REVISION_SCHEMA = (
    "trafficvisson.legacy-zero-detection-queue-revision.v1"
)
MAX_PIPELINE_JOBS = 128
MAX_PIPELINE_RETRIES = 5
# A job carries every bounded stage checkpoint needed for deterministic
# restart. 512 merged review events with three compact reference rows do not
# fit the former 2 MiB envelope, while 8 MiB remains an explicit finite
# recovery/read bound. Large media and full manifests stay external.
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_PIPELINE_FAILURE_RESERVE_BYTES = 64 * 1024
MAX_PRIVATE_MEDIA_GRANT_BYTES = 16 * 1024
MAX_RECOVERY_ENTRIES = MAX_PIPELINE_JOBS * 8
MAX_PIPELINE_OCR_OBSERVATIONS = 512
MAX_PIPELINE_OCR_OBSERVATIONS_PER_CANDIDATE = 16
MAX_VISUAL_REFERENCE_CANDIDATES = 512
MAX_VISUAL_REFERENCE_QUERY_CHUNK = 128
MAX_VISUAL_REFERENCES_PER_CANDIDATE = 3
MAX_VISUAL_REFERENCE_CAPTION_BYTES = 256
MAX_VISUAL_REFERENCE_ROUTE_BYTES = 128
MAX_VISUAL_REFERENCE_LOCATION_BYTES = 128
MAX_VISUAL_REFERENCE_OPINION_BYTES = 96
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_EXPLICIT_NEW_SOURCE_PREFIX = "explicit-new:"
_SERVER_OWNED_RERUN_POLICY = "server_owned_current_profile_rerun"
REPORT_INSPECTION_DATE_POLICY = "source_filename_yyyymmdd_or_block"
PIPELINE_STORAGE_PLAN_SCHEMA = (
    "trafficvisson.pipeline-capacity-plan.v2"
)
PIPELINE_STORAGE_PLANNING_ASSUMPTIONS = {
    "candidate_window_ratio_basis_points": 1_500,
    "candidate_density_seconds": 60,
    "minimum_planned_candidates": 4,
    "maximum_planned_candidates": 512,
    "maximum_planned_window_seconds": 15 * 60,
}
PIPELINE_STORAGE_REPLAN_SCHEMA = (
    "trafficvisson.pipeline-capacity-revision.v1"
)
_PIPELINE_STORAGE_REPLAN_STAGES = frozenset(
    {
        "dense_index_2fps",
        "candidate_rescan_5_10fps",
        "vision_proposal",
        "derived_outputs",
    }
)
_MINIMUM_PIPELINE_OUTPUT_RESERVE_BYTES = 4 * 1024**3
_MAXIMUM_PIPELINE_OUTPUT_RESERVE_BYTES = 128 * 1024**3
_REPLAN_STAGE_METADATA_BYTES = 64 * 1024**2
_REPLAN_PROVIDER_IMAGES_PER_EVENT = 6
_REPLAN_DERIVED_STRUCTURED_BYTES = 512 * 1024**2


def _provider_visual_capacity_upper_bound(event_count: int) -> int:
    if (
        isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or event_count < 0
    ):
        raise ValueError("event_count is invalid")
    return (
        event_count
        * _REPLAN_PROVIDER_IMAGES_PER_EVENT
        * MAX_PROVIDER_VISUAL_OUTPUT_BYTES
    )


def _derived_outputs_capacity_upper_bound(event_count: int) -> int:
    if (
        isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or event_count < 0
    ):
        raise ValueError("event_count is invalid")
    exported_evidence = min(
        MAX_EVIDENCE_TOTAL_BYTES,
        event_count
        * (MAX_EVIDENCE_IMAGE_BYTES + MAX_EVIDENCE_CLIP_BYTES),
    )
    return (
        _REPLAN_DERIVED_STRUCTURED_BYTES
        + exported_evidence
        + event_count * MAX_REPORT_EVIDENCE_OUTPUT_BYTES
    )

# This is deliberately narrower than the complete product taxonomy. Grounding
# DINO can offer review-only object boxes for these visual concepts, while
# potholes, pavement/lane wear, OCR text and SAM2 masks still require their
# dedicated packs. The degraded path must never imply those missing
# capabilities ran.
_DINO_REVIEW_ONLY_ISSUE_TYPES: tuple[str, ...] = (
    "road_debris",
    "median_barrier_damage",
    "sign_damage",
    "sign_vegetation_occlusion",
    "roadside_mowing_required",
    "guide_sign_identity",
    "tunnel_identity",
    "tunnel_name_occlusion",
    "other",
)
_DINO_UNSUPPORTED_DEDICATED_TYPES: tuple[str, ...] = (
    "pothole",
    "road_surface_damage",
    "lane_marking_faded",
    "colored_guidance_faded",
)

_STRUCTURED_VISION_PROJECT_INSTRUCTION = (
    "프로젝트의 도로 유지관리 분류와 사용자 승인 3·4분기 사례를 "
    "기준으로 이 병합 후보의 유형, 유지보수 필요 여부, 조치 및 "
    "심각도를 검토용으로만 제안"
)
_STRUCTURED_VISION_DEFAULT_ACTIONS: Mapping[str, tuple[str, ...]] = {
    "pothole": ("포트홀 보수",),
    "road_surface_damage": ("노면 파손 보수",),
    "road_debris": ("낙하물 제거",),
    "median_barrier_damage": ("중앙분리대 보수",),
    "sign_damage": ("표지판 및 지주 보수",),
    "sign_vegetation_occlusion": ("표지판 주변 삭초",),
    "roadside_mowing_required": ("갓길 및 비탈면 삭초",),
    # These actions describe review/verification only. The structured output
    # validator still requires identity classes to remain non-maintenance.
    "guide_sign_identity": ("이정표 표기 및 위치 확인",),
    "tunnel_identity": ("터널명 표기 확인",),
    "tunnel_name_occlusion": ("터널명 표지 주변 삭초",),
    "lane_marking_faded": ("차선 재도색",),
    "colored_guidance_faded": ("색깔유도선 재도색",),
    "other": ("사용자 검토 후 조치 결정",),
}


def _normalized_dino_box(value: Any) -> Optional[dict[str, float]]:
    if not isinstance(value, Mapping) or set(value) != {
        "x",
        "y",
        "width",
        "height",
    }:
        return None
    result: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = value.get(key)
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
        ):
            return None
        result[key] = float(number)
    if (
        not 0 <= result["x"] <= 1
        or not 0 <= result["y"] <= 1
        or not 0 < result["width"] <= 1
        or not 0 < result["height"] <= 1
        or result["x"] + result["width"] > 1.00000001
        or result["y"] + result["height"] > 1.00000001
    ):
        return None
    return result


def _dino_box_iou(
    left: Mapping[str, float],
    right: Mapping[str, float],
) -> float:
    left_x2 = left["x"] + left["width"]
    left_y2 = left["y"] + left["height"]
    right_x2 = right["x"] + right["width"]
    right_y2 = right["y"] + right["height"]
    intersection_width = max(
        0.0,
        min(left_x2, right_x2) - max(left["x"], right["x"]),
    )
    intersection_height = max(
        0.0,
        min(left_y2, right_y2) - max(left["y"], right["y"]),
    )
    intersection = intersection_width * intersection_height
    union = (
        left["width"] * left["height"]
        + right["width"] * right["height"]
        - intersection
    )
    return intersection / union if union > 0 else 0.0


def _deduplicated_dino_review_rows(
    parent_candidate_id: str,
    predictions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """NMS duplicate frame/type boxes without creating approved truth."""

    if re.fullmatch(r"[a-f0-9]{20}", parent_candidate_id) is None:
        raise ValueError("DINO parent candidate is invalid")
    rows: list[dict[str, Any]] = []
    for prediction in predictions:
        if not isinstance(prediction, Mapping):
            raise ValueError("DINO prediction is invalid")
        try:
            issue_type = canonical_issue_type(
                prediction.get("issue_type")
            )
        except CandidateLabelError as error:
            raise ValueError("DINO prediction type is invalid") from error
        score = prediction.get("model_score")
        box = _normalized_dino_box(prediction.get("box"))
        evidence_candidate_id = prediction.get(
            "evidence_candidate_id"
        )
        timestamp = prediction.get(
            "evidence_timestamp_seconds"
        )
        image_sha256 = prediction.get("evidence_image_sha256")
        evidence_source = prediction.get("evidence_source")
        source_label = prediction.get("source_label")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
            or box is None
            or not isinstance(evidence_candidate_id, str)
            or re.fullmatch(
                r"[a-f0-9]{20}", evidence_candidate_id
            )
            is None
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or not 0 <= float(timestamp) <= 2_678_400
            or not isinstance(image_sha256, str)
            or _SHA256.fullmatch(image_sha256) is None
            or evidence_source
            not in {
                "analysis_representative_frames",
                "candidate_window_rescan_5_10fps",
            }
            or not isinstance(source_label, str)
            or not source_label
            or len(source_label) > 128
        ):
            raise ValueError("DINO frame evidence is invalid")
        identity = {
            "parent_candidate_id": parent_candidate_id,
            "evidence_candidate_id": evidence_candidate_id,
            "evidence_timestamp_seconds": float(timestamp),
            "evidence_image_sha256": image_sha256,
            "evidence_source": evidence_source,
            "issue_type": issue_type,
            "score": float(score),
            "normalized_box": box,
            "source_label": source_label,
        }
        rows.append(
            {
                "proposal_id": (
                    "dino_"
                    + hashlib.sha256(
                        _canonical_json(identity)
                    ).hexdigest()[:24]
                ),
                **identity,
                "score_semantics": (
                    "model_score_not_probability"
                ),
                "review_only": True,
                "automatic_labeling_allowed": False,
            }
        )

    kept: list[dict[str, Any]] = []
    for row in sorted(
        rows,
        key=lambda item: (
            item["evidence_timestamp_seconds"],
            item["evidence_candidate_id"],
            item["issue_type"],
            -item["score"],
            item["proposal_id"],
        ),
    ):
        if any(
            previous["evidence_candidate_id"]
            == row["evidence_candidate_id"]
            and previous["issue_type"] == row["issue_type"]
            and _dino_box_iou(
                previous["normalized_box"],
                row["normalized_box"],
            )
            >= 0.85
            for previous in kept
        ):
            continue
        kept.append(row)
    return kept


PIPELINE_STAGES: tuple[str, ...] = (
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
)

_CANDIDATE_RESCAN_INPUT_ORIGINS_SCHEMA = (
    "trafficvisson.candidate-rescan-input-origins.v1"
)

_ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancelling"})
_TERMINAL_JOB_STATUSES = frozenset({"completed", "cancelled"})
_RESUMABLE_JOB_STATUSES = frozenset({"blocked", "failed", "cancelled"})
_RERUN_SOURCE_JOB_STATUSES = frozenset(
    {"awaiting_review", "blocked", "failed", "cancelled", "completed"}
)
_STAGE_STATUSES = frozenset(
    {"pending", "running", "success", "blocked", "failed", "skipped", "cancelled"}
)
_OUTCOME_CODES = frozenset(
    {
        "success",
        "zero_detections",
        "unusable_visual_evidence",
        "model_unavailable",
        "blocked_gpx_required",
        "partial",
        "report_preview_blocked",
        "failed",
        "cancelled",
    }
)


class PipelineJobError(RuntimeError):
    """Base class for bounded public pipeline errors."""


class PipelineJobNotFound(PipelineJobError):
    pass


class PipelineProfileMissing(PipelineJobError):
    pass


class PipelineJobConflict(PipelineJobError):
    pass


class PipelineJobUnavailable(PipelineJobError):
    pass


class PipelineRecordTooLarge(PipelineJobUnavailable):
    """The attempted record was replaced by a compact durable failure."""


class PipelineStageFailure(PipelineJobError):
    """A stage failed with a stable, non-secret error code."""

    def __init__(
        self,
        code: str,
        message: str = "Pipeline stage failed",
        *,
        observation: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not _SAFE_ERROR_CODE.fullmatch(code):
            raise ValueError("pipeline error code is invalid")
        super().__init__(message)
        self.code = code
        self.observation = (
            dict(observation)
            if isinstance(observation, Mapping)
            else None
        )


@dataclass(frozen=True)
class PipelineStageResult:
    """One runtime result.

    ``blocked_gpx_required`` is the only non-fatal blocked outcome because
    road-defect review can continue without fabricating a route. ``partial``
    preserves verified derived artifacts while stopping at their unresolved
    approval boundary. All other blocked outcomes stop at the exact stage.
    """

    outcome: str
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    non_blocking: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in _OUTCOME_CODES:
            raise ValueError("pipeline stage outcome is unsupported")
        if self.outcome == "blocked_gpx_required":
            if not self.non_blocking:
                raise ValueError("GPS preflight blocking must be branch-scoped")
        elif self.non_blocking:
            raise ValueError("only GPS preflight may be non-blocking")
        if self.error_code is not None and not _SAFE_ERROR_CODE.fullmatch(
            self.error_code
        ):
            raise ValueError("pipeline stage error code is invalid")
        _validate_safe_json(self.checkpoint, "stage checkpoint")
        _validate_safe_json(self.metrics, "stage metrics")


class PipelineRuntime(Protocol):
    """Stage adapter owned by the local bridge, never by the renderer."""

    def capability_snapshot(self, profile: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    def capture_restartable_input(
        self,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def validate_report_profile(
        self,
        *,
        project_id: UUID,
        template_id: str,
        template_sha256: str,
        semantic_map_id: str,
        provider: str,
        explicit_transmission_consent: bool,
    ) -> Mapping[str, Any]:
        ...

    def execute(
        self,
        stage: str,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        ...

    def cancel(self, context: Mapping[str, Any]) -> None:
        ...

    def validate_review_completion(
        self,
        context: Mapping[str, Any],
        review_completion_id: str,
    ) -> Mapping[str, Any]:
        ...

    def reconcile_question_catalog(
        self,
        context: Mapping[str, Any],
    ) -> PipelineStageResult:
        ...

    def preflight_dense_after_intake(
        self,
        context: Mapping[str, Any],
        *,
        duration_seconds: float,
    ) -> Mapping[str, Any]:
        ...

    def validate_server_owned_rerun_source(
        self,
        *,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        sha_checkpoint: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def resolve_completed_media_grant(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        context: Mapping[str, Any],
        expected_source_sha256: str,
    ) -> CompletedMediaGrant:
        ...


class OcrInferenceService(Protocol):
    """Bridge-internal OCR-only model runner."""

    def infer_candidates(
        self,
        candidates: Sequence[Mapping[str, Any]],
        run_directory: Path,
        *,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> tuple[Mapping[str, Mapping[str, Any]], Mapping[str, Any]]:
        ...


@dataclass
class PipelineJobRecord:
    job_id: UUID
    project_id: UUID
    snapshot_id: UUID
    file_name: str
    idempotency_key: str
    input_fingerprint: str
    profile_snapshot: dict[str, Any]
    capability_snapshot: dict[str, Any]
    status: str
    current_stage: str
    outcome_code: Optional[str]
    error_code: Optional[str]
    retry_count: int
    resume_count: int
    review_completion_id: Optional[str]
    stages: list[dict[str, Any]]
    provenance: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    recovered_after_restart: bool
    cancel_event: threading.Event
    record_sha256: Optional[str] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("pipeline datetime is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("pipeline datetime needs a timezone")
    return parsed.astimezone(timezone.utc)


def _validated_rerun_media_proof(
    value: Any,
    *,
    project_id: UUID,
    snapshot_id: UUID,
    file_name: str,
    hash_job_id: UUID,
    source_sha256: str,
    total_bytes: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "project_id",
        "snapshot_id",
        "hash_job_id",
        "file_name",
        "source_sha256",
        "size_bytes",
        "path_exposed",
    }:
        raise ValueError("pipeline rerun media proof fields are invalid")
    _validate_safe_json(value, "pipeline rerun media proof")
    if (
        value.get("schema_version") != PIPELINE_RERUN_MEDIA_PROOF_SCHEMA
        or value.get("project_id") != str(project_id)
        or value.get("snapshot_id") != str(snapshot_id)
        or value.get("hash_job_id") != str(hash_job_id)
        or value.get("file_name") != file_name
        or value.get("source_sha256") != source_sha256
        or value.get("size_bytes") != total_bytes
        or value.get("path_exposed") is not False
        or not is_allowlisted_video_name(file_name)
        or isinstance(total_bytes, bool)
        or not isinstance(total_bytes, int)
        or total_bytes <= 0
    ):
        raise ValueError("pipeline rerun media proof is invalid")
    return dict(value)


def _validate_rerun_lineage(
    value: Any,
    *,
    project_id: UUID,
    job_id: UUID,
    snapshot_id: UUID,
    file_name: str,
    input_fingerprint: str,
    profile_snapshot: Mapping[str, Any],
    capability_snapshot: Mapping[str, Any],
) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "policy",
        "request_sha256",
        "source_job_id",
        "source_record_sha256",
        "source_input_fingerprint",
        "source_snapshot_id",
        "source_file_name",
        "source_sha256",
        "source_hash_job_id",
        "source_total_bytes",
        "source_profile_revision",
        "source_profile_sha256",
        "source_capability_sha256",
        "target_job_id",
        "target_input_fingerprint",
        "target_profile_revision",
        "target_profile_sha256",
        "target_capability_sha256",
        "media_proof",
        "rights_confirmed",
        "user_confirmed",
        "created_at",
        "path_exposed",
    }:
        raise ValueError("pipeline rerun lineage fields are invalid")
    _validate_safe_json(value, "pipeline rerun lineage")
    source_job_id = UUID(str(value.get("source_job_id")))
    source_snapshot_id = UUID(str(value.get("source_snapshot_id")))
    source_hash_job_id = UUID(str(value.get("source_hash_job_id")))
    source_total_bytes = value.get("source_total_bytes")
    source_profile_revision = value.get("source_profile_revision")
    target_profile_revision = value.get("target_profile_revision")
    profile_sha256 = hashlib.sha256(
        _canonical_json(profile_snapshot)
    ).hexdigest()
    capability_sha256 = hashlib.sha256(
        _canonical_json(capability_snapshot)
    ).hexdigest()
    if (
        value.get("schema_version") != PIPELINE_JOB_RERUN_SCHEMA
        or value.get("policy") != _SERVER_OWNED_RERUN_POLICY
        or any(
            not isinstance(value.get(key), str)
            or _SHA256.fullmatch(value[key]) is None
            for key in (
                "request_sha256",
                "source_record_sha256",
                "source_input_fingerprint",
                "source_sha256",
                "source_profile_sha256",
                "source_capability_sha256",
                "target_input_fingerprint",
                "target_profile_sha256",
                "target_capability_sha256",
            )
        )
        or str(source_job_id) != value.get("source_job_id")
        or str(source_snapshot_id) != value.get("source_snapshot_id")
        or str(source_hash_job_id) != value.get("source_hash_job_id")
        or source_snapshot_id != snapshot_id
        or value.get("source_file_name") != file_name
        or value.get("target_job_id") != str(job_id)
        or value.get("target_input_fingerprint") != input_fingerprint
        or isinstance(source_total_bytes, bool)
        or not isinstance(source_total_bytes, int)
        or source_total_bytes <= 0
        or isinstance(source_profile_revision, bool)
        or not isinstance(source_profile_revision, int)
        or source_profile_revision < 1
        or isinstance(target_profile_revision, bool)
        or not isinstance(target_profile_revision, int)
        or target_profile_revision != profile_snapshot.get("revision")
        or value.get("target_profile_sha256") != profile_sha256
        or value.get("target_capability_sha256") != capability_sha256
        or value.get("rights_confirmed") is not True
        or value.get("user_confirmed") is not True
        or value.get("path_exposed") is not False
    ):
        raise ValueError("pipeline rerun lineage is invalid")
    _parse_datetime(value.get("created_at"))
    _validated_rerun_media_proof(
        value.get("media_proof"),
        project_id=project_id,
        snapshot_id=snapshot_id,
        file_name=file_name,
        hash_job_id=source_hash_job_id,
        source_sha256=str(value["source_sha256"]),
        total_bytes=source_total_bytes,
    )


def _normalize_report_context_text(
    value: Any,
    label: str,
    *,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    normalized = value.strip()
    if (
        not 1 <= len(normalized) <= maximum
        or any(
            unicodedata.category(character) == "Cc"
            for character in normalized
        )
    ):
        raise ValueError(f"{label} is invalid")
    return normalized


def _report_inspection_date_from_source_filename(
    file_name: Any,
    *,
    policy: Any,
) -> str:
    """Return an ISO inspection date only from a valid ``YYYYMMDD`` prefix.

    The report date is factual content.  It must never fall back to the
    current clock, file metadata, or a guessed capture date.  An empty result
    is intentional: derived CSV/evidence generation can continue while the
    report preview records ``report_inspection_date_required``.
    """

    if policy != REPORT_INSPECTION_DATE_POLICY:
        return ""
    if (
        not isinstance(file_name, str)
        or not 1 <= len(file_name) <= 255
        or Path(file_name).name != file_name
    ):
        return ""
    matched = re.match(r"^(?P<stamp>[0-9]{8})(?:[^0-9]|$)", file_name)
    if matched is None:
        return ""
    stamp = matched.group("stamp")
    try:
        parsed = date(
            int(stamp[0:4]),
            int(stamp[4:6]),
            int(stamp[6:8]),
        )
    except ValueError:
        return ""
    return parsed.isoformat()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_utf8_text(value: Any, maximum_bytes: int) -> Optional[str]:
    """Return a deterministic UTF-8 prefix without splitting a code point."""

    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes < 1
    ):
        return None
    raw = value.encode("utf-8")
    if len(raw) <= maximum_bytes:
        return value
    bounded = raw[:maximum_bytes].decode("utf-8", errors="ignore")
    return bounded if bounded else None


def _pipeline_start_input_fingerprint(
    *,
    project_id: UUID,
    snapshot_id: UUID,
    file_name: str,
    profile_snapshot: Mapping[str, Any],
    promoted_model_binding: Any,
    storage_plan: Optional[Mapping[str, Any]],
) -> str:
    """Hash the immutable ordinary-start inputs, including the model pin."""

    return hashlib.sha256(
        _canonical_json(
            {
                "project_id": str(project_id),
                "snapshot_id": str(snapshot_id),
                "file_name": file_name,
                "profile": profile_snapshot,
                "promoted_model_binding": promoted_model_binding,
                "storage_plan": storage_plan,
            }
        )
    ).hexdigest()


def _review_completion_id_from_proof_fragments(
    proofs: Sequence[str],
) -> str:
    """Return the one server-owned completion identity for an answer ledger."""

    if (
        isinstance(proofs, (str, bytes, bytearray))
        or not isinstance(proofs, Sequence)
        or not proofs
        or any(not isinstance(item, str) or not item for item in proofs)
    ):
        raise PipelineStageFailure("review_answer_proof_invalid")
    ledger_sha256 = hashlib.sha256(
        "\n".join(sorted(proofs)).encode("utf-8")
    ).hexdigest()
    return f"review:{ledger_sha256}"


def _automatic_structured_training_review_completion_id(
    context: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> Optional[str]:
    """Derive an automatic completion only for a fully delegated safe queue.

    This does not approve a road Incident.  It only closes a question queue
    whose every answer is already an explicitly authorized, structured
    LLM-assisted training label.  Any human/mandatory question or inconsistent
    proof returns to the ordinary human-review boundary or fails closed.
    """

    rescan = context.get("checkpoints", {}).get(
        "candidate_rescan_5_10fps", {}
    )
    discovery = (
        rescan.get("dense_candidate_discovery")
        if isinstance(rescan, Mapping)
        else None
    )
    provider_states = (
        discovery.get("provider_states")
        if isinstance(discovery, Mapping)
        else None
    )
    if isinstance(provider_states, list) and any(
        isinstance(item, Mapping)
        and item.get("provider") == "promoted_project_model"
        and item.get("state") == "ready"
        and item.get("independent_survey_verified") is True
        for item in provider_states
    ):
        # Delegation can authorize training labels, but it cannot replace the
        # operator's full-journey missed-scene QA on an independent survey.
        return None

    if (
        checkpoint.get("label_confirmation_authority")
        != "structured_llm_training_assist"
        or checkpoint.get("structured_label_delegation_state")
        != "completed"
    ):
        return None
    candidate_ids = checkpoint.get("question_candidate_ids")
    delegated_candidate_ids = checkpoint.get(
        "structured_training_label_candidate_ids"
    )
    human_candidate_ids = checkpoint.get(
        "human_question_candidate_ids"
    )
    mandatory_candidate_ids = checkpoint.get(
        "mandatory_human_candidate_ids"
    )
    delegation = checkpoint.get("structured_label_delegation")
    question_count = checkpoint.get("question_count")
    human_question_count = checkpoint.get("human_question_count")
    analysis_job_id = checkpoint.get("analysis_job_id")
    project_id = context.get("project_id")
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"[a-f0-9]{20}", item) is None
            for item in candidate_ids
        )
        or len(candidate_ids) != len(set(candidate_ids))
        or delegated_candidate_ids != candidate_ids
        or human_candidate_ids != []
        or mandatory_candidate_ids != []
        or isinstance(question_count, bool)
        or question_count != len(candidate_ids)
        or isinstance(human_question_count, bool)
        or human_question_count != 0
        or not isinstance(analysis_job_id, str)
        or not isinstance(project_id, str)
        or not isinstance(delegation, Mapping)
    ):
        raise PipelineStageFailure(
            "structured_llm_auto_review_invalid"
        )
    try:
        parsed_project_id = UUID(project_id)
        parsed_analysis_job_id = UUID(analysis_job_id)
    except (TypeError, ValueError) as error:
        raise PipelineStageFailure(
            "structured_llm_auto_review_invalid"
        ) from error
    if (
        str(parsed_project_id) != project_id
        or str(parsed_analysis_job_id) != analysis_job_id
        or checkpoint.get("structured_training_label_count")
        != question_count
        or checkpoint.get("annotation_count") != question_count
        or checkpoint.get("training_membership_count")
        != question_count
        or checkpoint.get("incident_count") != 0
        or checkpoint.get("report_incident_count") != 0
        or checkpoint.get("automatic_labeling_allowed") is not True
        or checkpoint.get("automatic_annotation_allowed") is not True
        or checkpoint.get(
            "automatic_training_membership_allowed"
        )
        is not True
        or checkpoint.get(
            "automatic_fixed_validation_membership_allowed"
        )
        is not False
        or checkpoint.get("automatic_incident_allowed") is not False
        or checkpoint.get("automatic_report_truth_allowed") is not False
        or checkpoint.get("review_only") is not True
    ):
        raise PipelineStageFailure(
            "structured_llm_auto_review_invalid"
        )

    delegated_proofs = delegation.get("delegated_proofs")
    delegation_authority = delegation.get("delegation_authority")
    delegation_revision = delegation.get("delegation_revision")
    identity = {
        "schema_version": delegation.get("schema_version"),
        "project_id": delegation.get("project_id"),
        "analysis_job_id": delegation.get("analysis_job_id"),
        "candidate_ids": delegation.get("candidate_ids"),
        "delegation_authority": delegation_authority,
        "delegated_proofs": delegated_proofs,
        "human_question_reasons": delegation.get(
            "human_question_reasons"
        ),
    }
    if (
        delegation.get("authority")
        != "structured_llm_training_assist"
        or delegation.get("state") != "completed"
        or delegation.get("schema_version")
        != "trafficvisson.pipeline-structured-label-delegation.v1"
        or delegation.get("project_id") != project_id
        or delegation.get("analysis_job_id") != analysis_job_id
        or delegation.get("candidate_ids") != candidate_ids
        or delegation.get("delegated_candidate_ids")
        != candidate_ids
        or delegation.get("human_question_candidate_ids") != []
        or delegation.get("mandatory_human_candidate_ids") != []
        or delegation.get("human_question_reasons") != {}
        or delegation.get("structured_training_label_count")
        != question_count
        or delegation.get("training_annotation_count")
        != question_count
        or delegation.get("incident_count") != 0
        or delegation.get("report_incident_count") != 0
        or delegation.get("fixed_validation_membership_count") != 0
        or delegation.get("training_only") is not True
        or delegation.get("automatic_training_started") is not False
        or not isinstance(delegation_authority, Mapping)
        or not isinstance(delegated_proofs, list)
        or len(delegated_proofs) != question_count
        or not isinstance(delegation_revision, str)
        or _SHA256.fullmatch(delegation_revision) is None
        or hashlib.sha256(_canonical_json(identity)).hexdigest()
        != delegation_revision
        or checkpoint.get("structured_label_delegation_revision")
        != delegation_revision
    ):
        raise PipelineStageFailure(
            "structured_llm_auto_review_invalid"
        )

    proof_fragments: list[str] = []
    proof_candidate_ids: list[str] = []
    for proof in delegated_proofs:
        if not isinstance(proof, Mapping):
            raise PipelineStageFailure(
                "structured_llm_auto_review_invalid"
            )
        candidate_id = proof.get("candidate_id")
        record_id = proof.get("record_id")
        record_hash = proof.get("record_hash")
        answer_revision = proof.get("answer_revision")
        assist_provenance_sha256 = proof.get(
            "assist_provenance_sha256"
        )
        try:
            parsed_record_id = UUID(str(record_id))
        except (TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "structured_llm_auto_review_invalid"
            ) from error
        if (
            not isinstance(candidate_id, str)
            or re.fullmatch(r"[a-f0-9]{20}", candidate_id) is None
            or str(parsed_record_id) != record_id
            or not isinstance(record_hash, str)
            or _SHA256.fullmatch(record_hash) is None
            or isinstance(answer_revision, bool)
            or not isinstance(answer_revision, int)
            or answer_revision < 1
            or not isinstance(assist_provenance_sha256, str)
            or _SHA256.fullmatch(assist_provenance_sha256) is None
        ):
            raise PipelineStageFailure(
                "structured_llm_auto_review_invalid"
            )
        proof_candidate_ids.append(candidate_id)
        proof_fragments.append(
            "\0".join(
                (
                    candidate_id,
                    record_id.lower(),
                    record_hash,
                    str(answer_revision),
                )
            )
        )
    if proof_candidate_ids != candidate_ids:
        raise PipelineStageFailure(
            "structured_llm_auto_review_invalid"
        )
    return _review_completion_id_from_proof_fragments(
        proof_fragments
    )


def _sha256_regular_file(
    path: Path,
    *,
    maximum_bytes: int = 512 * 1024 * 1024,
) -> str:
    """Hash one bounded regular file without following mutable links."""

    try:
        before = path.lstat()
    except OSError as error:
        raise PipelineStageFailure(
            "pipeline_artifact_unavailable"
        ) from error
    if (
        not path.is_file()
        or path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > maximum_bytes
    ):
        raise PipelineStageFailure("pipeline_artifact_invalid")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(4 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise PipelineStageFailure(
                        "pipeline_artifact_invalid"
                    )
                digest.update(chunk)
        after = path.lstat()
    except PipelineStageFailure:
        raise
    except OSError as error:
        raise PipelineStageFailure(
            "pipeline_artifact_unavailable"
        ) from error
    if (
        total != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_dev != before.st_dev
        or after.st_ino != before.st_ino
    ):
        raise PipelineStageFailure("pipeline_artifact_changed")
    return digest.hexdigest()


def _strict_json(raw: bytes, label: str) -> dict[str, Any]:
    def pairs(pairs_value):
        result = {}
        for key, value in pairs_value:
            if key in result:
                raise ValueError(f"{label} has duplicate keys")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError(f"{label} has a non-finite number")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _validate_safe_json(
    value: Any,
    label: str,
    *,
    depth: int = 0,
    maximum_depth: int = 8,
) -> None:
    if depth > maximum_depth:
        raise ValueError(f"{label} is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise ValueError(f"{label} has too many fields")
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 128
                or "\x00" in key
                or key.casefold() in {"path", "media_path", "source_path"}
                or key.casefold().endswith("_path")
            ):
                raise ValueError(f"{label} has an unsafe field")
            _validate_safe_json(
                item,
                label,
                depth=depth + 1,
                maximum_depth=maximum_depth,
            )
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 10_000:
            raise ValueError(f"{label} has too many items")
        for item in value:
            _validate_safe_json(
                item,
                label,
                depth=depth + 1,
                maximum_depth=maximum_depth,
            )
        return
    raise ValueError(f"{label} contains an unsupported value")


def _validated_storage_plan(
    value: Optional[Mapping[str, Any]],
    *,
    candidate_rescan_fps: int,
) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("pipeline storage plan is invalid")
    _validate_safe_json(value, "pipeline storage plan")
    expected_keys = {
        "schema_version",
        "candidate_rescan_fps",
        "planning_assumptions",
        "planned_candidate_window_seconds",
        "planned_candidate_count",
        "dense_frame_count",
        "candidate_rescan_frame_count",
        "average_frame_bytes_ceiling",
        "stage_estimates",
        "separate_capacity_preflights",
        "estimated_artifact_bytes",
        "safety_reserve_bytes",
        "required_free_bytes",
    }
    if (
        set(value) != expected_keys
        or value.get("schema_version")
        != PIPELINE_STORAGE_PLAN_SCHEMA
        or value.get("candidate_rescan_fps")
        != candidate_rescan_fps
        or value.get("planning_assumptions")
        != PIPELINE_STORAGE_PLANNING_ASSUMPTIONS
        or not isinstance(value.get("stage_estimates"), list)
        or len(value["stage_estimates"]) != len(PIPELINE_STAGES)
        or value.get("separate_capacity_preflights")
        != [
            "project_model_training",
            "hwp_com_application_and_48_page_render",
        ]
    ):
        raise ValueError("pipeline storage plan contract is invalid")
    integer_fields = {
        "planned_candidate_count",
        "dense_frame_count",
        "candidate_rescan_frame_count",
        "average_frame_bytes_ceiling",
        "estimated_artifact_bytes",
        "safety_reserve_bytes",
        "required_free_bytes",
    }
    if any(
        isinstance(value.get(key), bool)
        or not isinstance(value.get(key), int)
        or int(value[key]) < 0
        for key in integer_fields
    ):
        raise ValueError("pipeline storage plan values are invalid")
    if (
        isinstance(
            value.get("planned_candidate_window_seconds"), bool
        )
        or not isinstance(
            value.get("planned_candidate_window_seconds"),
            (int, float),
        )
        or not math.isfinite(
            float(value["planned_candidate_window_seconds"])
        )
        or not 0.0
        < float(value["planned_candidate_window_seconds"])
        <= float(
            PIPELINE_STORAGE_PLANNING_ASSUMPTIONS[
                "maximum_planned_window_seconds"
            ]
        )
        or
        not 1 <= int(value["planned_candidate_count"]) <= 512
        or not 1 <= int(value["dense_frame_count"]) <= 30_000
        or not 1
        <= int(value["candidate_rescan_frame_count"])
        <= 30_000
        or not 64 * 1024
        <= int(value["average_frame_bytes_ceiling"])
        <= 24 * 1024**2
        or not 512 * 1024**2
        <= int(value["safety_reserve_bytes"])
        <= _MAXIMUM_PIPELINE_OUTPUT_RESERVE_BYTES
        or int(value["required_free_bytes"])
        != int(value["estimated_artifact_bytes"])
        + int(value["safety_reserve_bytes"])
        or int(value["estimated_artifact_bytes"]) > 8 * 1024**4
    ):
        raise ValueError("pipeline storage plan bounds are invalid")
    normalized_stages: list[dict[str, Any]] = []
    for expected_stage, raw in zip(
        PIPELINE_STAGES, value["stage_estimates"]
    ):
        if (
            not isinstance(raw, Mapping)
            or set(raw)
            != {
                "stage",
                "categories",
                "planned_frame_count",
                "average_bytes_per_frame_ceiling",
                "fixed_bytes",
                "hard_ceiling_bytes",
                "estimated_bytes",
            }
            or raw.get("stage") != expected_stage
            or not isinstance(raw.get("categories"), list)
            or not 1 <= len(raw["categories"]) <= 8
            or len(raw["categories"]) != len(set(raw["categories"]))
            or any(
                not isinstance(category, str)
                or re.fullmatch(
                    r"[a-z0-9][a-z0-9-]{1,63}", category
                )
                is None
                for category in raw["categories"]
            )
        ):
            raise ValueError("pipeline storage stage is invalid")
        numeric_names = {
            "planned_frame_count",
            "average_bytes_per_frame_ceiling",
            "fixed_bytes",
            "estimated_bytes",
        }
        if any(
            isinstance(raw.get(key), bool)
            or not isinstance(raw.get(key), int)
            or int(raw[key]) < 0
            for key in numeric_names
        ):
            raise ValueError(
                "pipeline storage stage values are invalid"
            )
        hard_ceiling = raw.get("hard_ceiling_bytes")
        if hard_ceiling is not None and (
            isinstance(hard_ceiling, bool)
            or not isinstance(hard_ceiling, int)
            or hard_ceiling < 1
        ):
            raise ValueError(
                "pipeline storage stage ceiling is invalid"
            )
        computed = (
            int(raw["planned_frame_count"])
            * int(raw["average_bytes_per_frame_ceiling"])
            + int(raw["fixed_bytes"])
        )
        if (
            computed != int(raw["estimated_bytes"])
            or (
                hard_ceiling is not None
                and computed > int(hard_ceiling)
            )
        ):
            raise ValueError(
                "pipeline storage stage estimate is inconsistent"
            )
        normalized_stages.append(
            {
                **dict(raw),
                "categories": list(raw["categories"]),
            }
        )
    if sum(
        int(stage["estimated_bytes"]) for stage in normalized_stages
    ) != int(value["estimated_artifact_bytes"]):
        raise ValueError("pipeline storage total is inconsistent")
    return {
        **dict(value),
        "stage_estimates": normalized_stages,
    }


def _validated_storage_revisions(
    value: object,
    *,
    plan: Optional[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if (
        plan is None
        or not isinstance(value, list)
        or len(value) > len(_PIPELINE_STORAGE_REPLAN_STAGES)
    ):
        raise ValueError("pipeline storage revisions are invalid")
    plan_sha256 = hashlib.sha256(
        _canonical_json(plan)
    ).hexdigest()
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema_version")
            != PIPELINE_STORAGE_REPLAN_SCHEMA
            or raw.get("state")
            != "same_volume_adjustment_required"
            or raw.get("original_plan_sha256") != plan_sha256
            or raw.get("path_exposed") is not False
            or raw.get("stage") not in _PIPELINE_STORAGE_REPLAN_STAGES
            or not isinstance(raw.get("basis"), Mapping)
        ):
            raise ValueError("pipeline storage revision is invalid")
        stage_name = str(raw["stage"])
        if stage_name in seen:
            raise ValueError("pipeline storage revision stage is duplicated")
        stage = plan["stage_estimates"][
            PIPELINE_STAGES.index(stage_name)
        ]
        original = raw.get("original_planned_bytes")
        required = raw.get("required_bytes")
        hard = raw.get("hard_ceiling_bytes")
        body = {
            key: raw[key]
            for key in raw
            if key != "revision_sha256"
        }
        if (
            isinstance(original, bool)
            or not isinstance(original, int)
            or original != stage["estimated_bytes"]
            or isinstance(required, bool)
            or not isinstance(required, int)
            or required <= original
            or hard != stage["hard_ceiling_bytes"]
            or hard is None
            or required > int(hard)
            or raw.get("revision_sha256")
            != hashlib.sha256(_canonical_json(body)).hexdigest()
        ):
            raise ValueError("pipeline storage revision is invalid")
        _validate_safe_json(
            body,
            "pipeline storage revision",
        )
        normalized.append(dict(raw))
        seen.add(stage_name)
    return normalized


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or _is_reparse_point(path.parent):
        raise PipelineJobUnavailable("pipeline record directory is unsafe")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # On Windows, an indexer or virus scanner can hold the destination for
        # a few milliseconds even though TrafficVisson owns no open handle.
        # Keep the write atomic and retry only this transient sharing error;
        # every other filesystem failure still fails closed immediately.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _profile_payload(
    *,
    project_id: UUID,
    question_policy: str,
    review_mode: str,
    label_confirmation_authority: str,
    default_template_id: str,
    default_template_sha256: str,
    detector_pack_id: str,
    ocr_pack_id: str,
    sam2_pack_id: str,
    report_provider: str,
    report_transmission_consent: bool,
    template_semantic_map_id: str,
    template_semantic_map_sha256: str,
    template_semantic_map_ready: bool,
    report_provider_ready_at_configuration: bool,
    report_route_name: str,
    report_inspector_name: str,
    report_evidence_mode: str,
    report_inspection_date_policy: str,
    output_location_id: str,
    candidate_rescan_fps: int,
    revision: int,
    updated_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": PIPELINE_PROFILE_SCHEMA,
        "project_id": str(project_id),
        "question_policy": question_policy,
        "review_mode": review_mode,
        "label_confirmation_authority": label_confirmation_authority,
        "default_template_id": default_template_id,
        "default_template_sha256": default_template_sha256,
        "detector_pack_id": detector_pack_id,
        "ocr_pack_id": ocr_pack_id,
        "sam2_pack_id": sam2_pack_id,
        "report_provider": report_provider,
        "report_transmission_consent": report_transmission_consent,
        "template_semantic_map_id": template_semantic_map_id,
        "template_semantic_map_sha256": (
            template_semantic_map_sha256
        ),
        "template_semantic_map_ready": template_semantic_map_ready,
        "report_provider_ready_at_configuration": (
            report_provider_ready_at_configuration
        ),
        "report_route_name": report_route_name,
        "report_inspector_name": report_inspector_name,
        "report_evidence_mode": report_evidence_mode,
        "report_inspection_date_policy": (
            report_inspection_date_policy
        ),
        "output_location_id": output_location_id,
        "candidate_rescan_fps": candidate_rescan_fps,
        "gps_policy": "timed_track_required_for_route",
        "bulk_frame_provider_policy": "local_models_only",
        "revision": revision,
        "updated_at": updated_at.isoformat(),
    }


def _migrate_profile_to_v4(
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Upgrade a verified v1-v3 project profile without broadening authority."""

    if profile.get("schema_version") == PIPELINE_PROFILE_SCHEMA:
        return dict(profile)
    return {
        "schema_version": PIPELINE_PROFILE_SCHEMA,
        "project_id": profile["project_id"],
        "question_policy": profile["question_policy"],
        "review_mode": profile["review_mode"],
        # Legacy profiles never delegated training-label confirmation to an
        # LLM. Migration must preserve that fail-closed human-review boundary.
        "label_confirmation_authority": "human_review",
        "default_template_id": profile["default_template_id"],
        "default_template_sha256": profile["default_template_sha256"],
        "detector_pack_id": profile["detector_pack_id"],
        "ocr_pack_id": profile["ocr_pack_id"],
        "sam2_pack_id": profile["sam2_pack_id"],
        "report_provider": profile["report_provider"],
        "report_transmission_consent": profile.get(
            "report_transmission_consent", False
        ),
        "template_semantic_map_id": profile.get(
            "template_semantic_map_id", "unconfigured"
        ),
        "template_semantic_map_sha256": profile.get(
            "template_semantic_map_sha256", "0" * 64
        ),
        "template_semantic_map_ready": profile.get(
            "template_semantic_map_ready", False
        ),
        "report_provider_ready_at_configuration": profile.get(
            "report_provider_ready_at_configuration", False
        ),
        "report_route_name": profile.get(
            "report_route_name", "unconfigured"
        ),
        "report_inspector_name": profile.get(
            "report_inspector_name", "unconfigured"
        ),
        "report_evidence_mode": profile.get(
            "report_evidence_mode", "internal_original"
        ),
        "report_inspection_date_policy": profile.get(
            "report_inspection_date_policy",
            REPORT_INSPECTION_DATE_POLICY,
        ),
        "output_location_id": profile["output_location_id"],
        "candidate_rescan_fps": profile["candidate_rescan_fps"],
        "gps_policy": profile["gps_policy"],
        "bulk_frame_provider_policy": profile[
            "bulk_frame_provider_policy"
        ],
        "revision": profile["revision"],
        "updated_at": profile["updated_at"],
    }


class PipelineJobService:
    """One-worker durable DAG with project/profile and review approval gates."""

    supports_storage_plan = True
    supports_capacity_reservations = True

    def __init__(
        self,
        runtime: PipelineRuntime,
        *,
        data_root: Path,
        artifact_root_registry: Optional[
            ProjectArtifactRootRegistry
        ] = None,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if not callable(getattr(runtime, "execute", None)):
            raise ValueError("pipeline runtime is invalid")
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("pipeline data_root must be absolute")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or _is_reparse_point(root) or not root.is_dir():
            raise ValueError("pipeline data_root must be a real directory")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not 0.01 <= float(poll_interval_seconds) <= 2
        ):
            raise ValueError("pipeline poll interval is invalid")
        self._runtime = runtime
        self._artifact_roots = artifact_root_registry
        self._data_root = root.resolve()
        self._projects_root = self._data_root / "projects"
        self._projects_root.mkdir(exist_ok=True)
        if (
            self._projects_root.is_symlink()
            or _is_reparse_point(self._projects_root)
            or not self._projects_root.is_dir()
            or self._projects_root.resolve().parent != self._data_root
        ):
            raise ValueError("pipeline projects root must be a real directory")
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._jobs: "OrderedDict[UUID, PipelineJobRecord]" = OrderedDict()
        self._aliases: dict[UUID, dict[str, Any]] = {}
        self._profiles: dict[UUID, dict[str, Any]] = {}
        # One inspected folder can contribute up to MAX_PIPELINE_JOBS videos.
        # Keep the durable worker single-threaded, but allow the complete
        # folder selection to be queued instead of silently using only the
        # first 32 entries.
        self._queue: "queue.Queue[Optional[UUID]]" = queue.Queue(
            maxsize=MAX_PIPELINE_JOBS
        )
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._recovery_summary = {
            "profiles_loaded": 0,
            "jobs_loaded": 0,
            "aliases_loaded": 0,
            "jobs_requeued": 0,
            "cancellations_finalized": 0,
            "question_catalogs_registered": 0,
            "question_catalog_migrations_failed": 0,
            "records_rejected": 0,
        }
        recovered = self._load_records()
        if self._artifact_roots is not None:
            with self._lock:
                for job in self._jobs.values():
                    if (
                        job.status
                        in {
                            "completed",
                            "failed",
                            "cancelled",
                        }
                    ):
                        continue
                    if (
                        isinstance(
                            job.provenance.get("artifact_root"),
                            Mapping,
                        )
                        and not isinstance(
                            job.provenance.get("storage_plan"),
                            Mapping,
                        )
                        and not self._recover_bound_storage_plan_locked(
                            job
                        )
                    ):
                        if job.job_id in recovered:
                            recovered.remove(job.job_id)
                        continue
                    if not isinstance(
                        job.provenance.get("storage_plan"),
                        Mapping,
                    ):
                        continue
                    try:
                        self._job_storage_budget_snapshot(
                            job, job.current_stage
                        )
                    except PipelineStageFailure as error:
                        self._block_storage_stage_locked(
                            job,
                            job.current_stage,
                            error_code=error.code,
                            observation=error.observation,
                        )
                        if job.job_id in recovered:
                            recovered.remove(job.job_id)
        if recovered:
            with self._lock:
                self._ensure_worker_locked()
                for job_id in recovered:
                    self._queue.put_nowait(job_id)

    def configure_profile(
        self,
        project_id: UUID,
        *,
        question_policy: str,
        review_mode: str,
        label_confirmation_authority: str = "human_review",
        default_template_id: str,
        default_template_sha256: str,
        detector_pack_id: str,
        ocr_pack_id: str,
        sam2_pack_id: str,
        report_provider: str,
        output_location_id: str,
        candidate_rescan_fps: int,
        report_transmission_consent: bool = False,
        template_semantic_map_id: str = "unconfigured",
        report_route_name: str = "unconfigured",
        report_inspector_name: str = "unconfigured",
        report_evidence_mode: str = "internal_original",
        report_inspection_date_policy: str = (
            REPORT_INSPECTION_DATE_POLICY
        ),
    ) -> dict[str, Any]:
        self._validate_project_id(project_id)
        if question_policy not in {
            "learning_assist",
            "full_review",
            "quick_review",
        }:
            raise ValueError("question_policy is invalid")
        if review_mode not in {"precision", "quick"}:
            raise ValueError("review_mode is invalid")
        if label_confirmation_authority not in {
            "human_review",
            "structured_llm_training_assist",
        }:
            raise ValueError(
                "label_confirmation_authority is invalid"
            )
        if report_provider not in {"codex", "claude", "local", "none"}:
            raise ValueError("report_provider is invalid")
        if not isinstance(report_transmission_consent, bool):
            raise ValueError(
                "report_transmission_consent must be boolean"
            )
        if (
            label_confirmation_authority
            == "structured_llm_training_assist"
            and report_provider not in {"codex", "claude"}
        ):
            raise ValueError(
                "structured_llm_training_assist requires codex or claude"
            )
        if (
            label_confirmation_authority
            == "structured_llm_training_assist"
            and report_transmission_consent is not True
        ):
            raise ValueError(
                "structured_llm_training_assist requires explicit "
                "report transmission consent"
            )
        if (
            not isinstance(template_semantic_map_id, str)
            or _IDENTIFIER.fullmatch(template_semantic_map_id) is None
        ):
            raise ValueError("template_semantic_map_id is invalid")
        report_route_name = _normalize_report_context_text(
            report_route_name,
            "report_route_name",
            maximum=300,
        )
        report_inspector_name = _normalize_report_context_text(
            report_inspector_name,
            "report_inspector_name",
            maximum=120,
        )
        if report_evidence_mode not in {
            "internal_original",
            "shared_deidentified",
        }:
            raise ValueError("report_evidence_mode is invalid")
        if (
            report_inspection_date_policy
            != REPORT_INSPECTION_DATE_POLICY
        ):
            raise ValueError(
                "report_inspection_date_policy is invalid"
            )
        for value, label in (
            (default_template_id, "default_template_id"),
            (detector_pack_id, "detector_pack_id"),
            (ocr_pack_id, "ocr_pack_id"),
            (sam2_pack_id, "sam2_pack_id"),
            (output_location_id, "output_location_id"),
        ):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{label} is invalid")
        if (
            not isinstance(default_template_sha256, str)
            or _SHA256.fullmatch(default_template_sha256) is None
        ):
            raise ValueError("default_template_sha256 is invalid")
        if (
            isinstance(candidate_rescan_fps, bool)
            or not isinstance(candidate_rescan_fps, int)
            or not 5 <= candidate_rescan_fps <= 10
        ):
            raise ValueError("candidate_rescan_fps must be 5 through 10")
        semantic_map_sha256 = "0" * 64
        semantic_map_ready = False
        provider_ready = False
        validator = getattr(
            self._runtime, "validate_report_profile", None
        )
        should_validate_report = (
            report_transmission_consent
            or template_semantic_map_id != "unconfigured"
        )
        if should_validate_report:
            if not callable(validator):
                raise PipelineJobConflict(
                    "Report automation profile validation is unavailable"
                )
            try:
                report_snapshot = dict(
                    validator(
                        project_id=project_id,
                        template_id=default_template_id,
                        template_sha256=default_template_sha256,
                        semantic_map_id=template_semantic_map_id,
                        provider=report_provider,
                        explicit_transmission_consent=(
                            report_transmission_consent
                        ),
                    )
                )
                _validate_safe_json(
                    report_snapshot,
                    "report automation profile snapshot",
                )
            except PipelineJobError:
                raise
            except Exception as error:
                raise PipelineJobConflict(
                    "Report automation profile validation failed"
                ) from error
            semantic_map_sha256 = report_snapshot.get(
                "template_semantic_map_sha256"
            )
            semantic_map_ready = report_snapshot.get(
                "template_semantic_map_ready"
            )
            provider_ready = report_snapshot.get(
                "report_provider_ready"
            )
            if (
                not isinstance(semantic_map_sha256, str)
                or _SHA256.fullmatch(semantic_map_sha256) is None
                or not isinstance(semantic_map_ready, bool)
                or not isinstance(provider_ready, bool)
                or report_snapshot.get(
                    "template_semantic_map_id"
                )
                != template_semantic_map_id
            ):
                raise PipelineJobConflict(
                    "Report automation profile validation is invalid"
                )
            if report_transmission_consent and (
                report_provider not in {"codex", "claude"}
                or semantic_map_ready is not True
                or provider_ready is not True
            ):
                raise PipelineJobConflict(
                    "The selected report provider and sample map must be ready"
                )
        with self._lock:
            previous = self._profiles.get(project_id)
            revision = int(previous["revision"]) + 1 if previous else 1
            profile = _profile_payload(
                project_id=project_id,
                question_policy=question_policy,
                review_mode=review_mode,
                label_confirmation_authority=(
                    label_confirmation_authority
                ),
                default_template_id=default_template_id,
                default_template_sha256=default_template_sha256,
                detector_pack_id=detector_pack_id,
                ocr_pack_id=ocr_pack_id,
                sam2_pack_id=sam2_pack_id,
                report_provider=report_provider,
                report_transmission_consent=(
                    report_transmission_consent
                ),
                template_semantic_map_id=template_semantic_map_id,
                template_semantic_map_sha256=semantic_map_sha256,
                template_semantic_map_ready=semantic_map_ready,
                report_provider_ready_at_configuration=provider_ready,
                report_route_name=report_route_name,
                report_inspector_name=report_inspector_name,
                report_evidence_mode=report_evidence_mode,
                report_inspection_date_policy=(
                    report_inspection_date_policy
                ),
                output_location_id=output_location_id,
                candidate_rescan_fps=candidate_rescan_fps,
                revision=revision,
                updated_at=_utc_now(),
            )
            if self._artifact_roots is not None:
                try:
                    self._artifact_roots.capture_profile_binding(
                        project_id,
                        profile_revision=revision,
                        output_location_id=output_location_id,
                    )
                except ProjectArtifactRootNotFound as error:
                    raise PipelineJobConflict(
                        "The selected project output location is not registered"
                    ) from error
                except ProjectArtifactRootConflict as error:
                    raise PipelineJobConflict(
                        "The selected project output location could not be verified"
                    ) from error
                except ProjectArtifactRootUnavailable as error:
                    raise PipelineJobUnavailable(
                        "The selected project output location is unavailable"
                    ) from error
            self._persist_profile(profile)
            self._profiles[project_id] = profile
            return dict(profile)

    def get_profile(self, project_id: UUID) -> dict[str, Any]:
        self._validate_project_id(project_id)
        with self._lock:
            profile = self._profiles.get(project_id)
            if profile is None:
                raise PipelineProfileMissing(
                    "Configure the project pipeline profile before adding video"
                )
            return dict(profile)

    def dense_staging_status(self) -> dict[str, Any]:
        resolver = getattr(self._runtime, "dense_staging_status", None)
        if not callable(resolver):
            return {
                "state": "unavailable",
                "source": "not_configured",
                "available_free_bytes": 0,
                "recovered_stale_children": 0,
                "error_code": "dense_staging_not_configured",
                "path_exposed": False,
            }
        try:
            status = dict(resolver())
            _validate_safe_json(status, "dense staging status")
        except Exception:
            return {
                "state": "unavailable",
                "source": "status_failed",
                "available_free_bytes": 0,
                "recovered_stale_children": 0,
                "error_code": "dense_staging_status_failed",
                "path_exposed": False,
            }
        return status

    def start(
        self,
        project_id: UUID,
        *,
        snapshot_id: UUID,
        file_name: str,
        rights_confirmed: bool,
        idempotency_key: str,
        expected_profile_revision: Optional[int] = None,
        expected_artifact_root_binding: Optional[
            Mapping[str, Any]
        ] = None,
        storage_plan: Optional[Mapping[str, Any]] = None,
        capacity_reservation_parent_id: Optional[str] = None,
    ) -> tuple[dict[str, Any], bool]:
        self._validate_project_id(project_id)
        if not isinstance(snapshot_id, UUID):
            raise ValueError("snapshot_id must be a UUID")
        if (
            not isinstance(file_name, str)
            or not 1 <= len(file_name) <= 255
            or "\x00" in file_name
            or Path(file_name).name != file_name
        ):
            raise ValueError("file_name is invalid")
        if rights_confirmed is not True:
            raise PipelineJobConflict(
                "Video rights must be confirmed before analysis starts"
            )
        if (
            not isinstance(idempotency_key, str)
            or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
        ):
            raise ValueError("idempotency_key is invalid")
        if (
            capacity_reservation_parent_id is not None
            and (
                not isinstance(
                    capacity_reservation_parent_id, str
                )
                or re.fullmatch(
                    r"folder-batch:"
                    r"[0-9a-f]{8}-[0-9a-f]{4}-"
                    r"[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-"
                    r"[0-9a-f]{12}",
                    capacity_reservation_parent_id,
                )
                is None
            )
        ):
            raise ValueError(
                "capacity reservation parent is invalid"
            )
        with self._lock:
            profile = self._profiles.get(project_id)
            if profile is None:
                raise PipelineProfileMissing(
                    "Configure the project pipeline profile before adding video"
                )
            profile_snapshot = dict(profile)
            normalized_storage_plan = _validated_storage_plan(
                storage_plan,
                candidate_rescan_fps=int(
                    profile_snapshot["candidate_rescan_fps"]
                ),
            )
            if expected_profile_revision is not None and (
                isinstance(expected_profile_revision, bool)
                or not isinstance(expected_profile_revision, int)
                or expected_profile_revision < 1
                or profile_snapshot["revision"]
                != expected_profile_revision
            ):
                raise PipelineJobConflict(
                    "The pipeline profile changed after the input batch was approved"
                )
            artifact_root_binding: Optional[dict[str, Any]] = None
            if self._artifact_roots is not None:
                try:
                    artifact_root_binding = dict(
                        self._capture_job_artifact_root(
                            project_id, profile_snapshot
                        )
                    )
                except ProjectArtifactRootNotFound as error:
                    raise PipelineJobConflict(
                        "The pipeline profile output location is not registered"
                    ) from error
                except ProjectArtifactRootConflict as error:
                    raise PipelineJobConflict(
                        "The pipeline profile output location changed"
                    ) from error
                except ProjectArtifactRootUnavailable as error:
                    raise PipelineJobUnavailable(
                        "The pipeline profile output location is unavailable"
                    ) from error
            if expected_artifact_root_binding is not None:
                if (
                    not isinstance(
                        expected_artifact_root_binding, Mapping
                    )
                    or artifact_root_binding is None
                    or any(
                        expected_artifact_root_binding.get(key)
                        != artifact_root_binding.get(key)
                        for key in (
                            "output_location_id",
                            "registry_revision",
                            "root_identity_sha256",
                            "marker_sha256",
                        )
                    )
                ):
                    raise PipelineJobConflict(
                        "The pipeline artifact root changed after batch preflight"
                    )
            capability_snapshot = dict(
                self._runtime.capability_snapshot(profile_snapshot)
            )
            _validate_safe_json(capability_snapshot, "capability snapshot")
            fingerprint = _pipeline_start_input_fingerprint(
                project_id=project_id,
                snapshot_id=snapshot_id,
                file_name=file_name,
                profile_snapshot=profile_snapshot,
                promoted_model_binding=capability_snapshot.get(
                    "promoted_project_model_binding"
                ),
                storage_plan=normalized_storage_plan,
            )
            for existing in self._jobs.values():
                if existing.idempotency_key == idempotency_key:
                    if existing.input_fingerprint != fingerprint:
                        raise PipelineJobConflict(
                            "The idempotency key belongs to different input"
                        )
                    return self._serialize(existing), True
                if existing.input_fingerprint == fingerprint:
                    return self._serialize(existing), True
            for alias in self._aliases.values():
                if alias["idempotency_key"] != idempotency_key:
                    continue
                if alias["input_fingerprint"] != fingerprint:
                    raise PipelineJobConflict(
                        "The idempotency key belongs to different input"
                    )
                canonical = self._jobs.get(
                    UUID(alias["canonical_job_id"])
                )
                if canonical is None:
                    raise PipelineJobUnavailable(
                        "pipeline source replay target is unavailable"
                    )
                return self._serialize(canonical), True
            self._evict_locked()
            if len(self._jobs) >= MAX_PIPELINE_JOBS:
                raise PipelineJobUnavailable("pipeline registry is full")
            now = _utc_now()
            stages = [
                {
                    "name": name,
                    "status": "pending",
                    "outcome": None,
                    "attempts": 0,
                    "started_at": None,
                    "completed_at": None,
                    "error_code": None,
                    "checkpoint": {},
                    "metrics": {},
                }
                for name in PIPELINE_STAGES
            ]
            job = PipelineJobRecord(
                job_id=uuid4(),
                project_id=project_id,
                snapshot_id=snapshot_id,
                file_name=file_name,
                idempotency_key=idempotency_key,
                input_fingerprint=fingerprint,
                profile_snapshot=profile_snapshot,
                capability_snapshot=capability_snapshot,
                status="queued",
                current_stage=PIPELINE_STAGES[0],
                outcome_code=None,
                error_code=None,
                retry_count=0,
                resume_count=0,
                review_completion_id=None,
                stages=stages,
                provenance={
                    "schema_version": PIPELINE_JOB_SCHEMA,
                    "source": {
                        "snapshot_id": str(snapshot_id),
                        "file_name": file_name,
                        "rights_confirmed": True,
                    },
                    "profile_revision": profile_snapshot["revision"],
                    "profile_sha256": hashlib.sha256(
                        _canonical_json(profile_snapshot)
                    ).hexdigest(),
                    "capability_sha256": hashlib.sha256(
                        _canonical_json(capability_snapshot)
                    ).hexdigest(),
                    "capability_attempts": [],
                    "input_storage_plan": normalized_storage_plan,
                    "stage_order": list(PIPELINE_STAGES),
                    "route_policy": (
                        "timed_gps_only_never_capture_start_interpolation"
                    ),
                    "provider_policy": (
                        "codex_claude_structured_proposal_not_bulk_frames"
                    ),
                    **(
                        {"artifact_root": artifact_root_binding}
                        if artifact_root_binding is not None
                        else {}
                    ),
                    **(
                        {"storage_plan": normalized_storage_plan}
                        if normalized_storage_plan is not None
                        else {}
                    ),
                },
                created_at=now,
                updated_at=now,
                recovered_after_restart=False,
                cancel_event=threading.Event(),
            )
            if (
                normalized_storage_plan is not None
                and self._artifact_roots is not None
                and artifact_root_binding is not None
            ):
                reservation_id = f"pipeline-job:{job.job_id}"
                try:
                    if capacity_reservation_parent_id is not None:
                        reservation_observation = (
                            self._artifact_roots
                            .transfer_capacity_reservation(
                                project_id,
                                artifact_root_binding,
                                from_reservation_id=(
                                    capacity_reservation_parent_id
                                ),
                                to_reservation_id=reservation_id,
                                reserved_bytes=int(
                                    normalized_storage_plan[
                                        "estimated_artifact_bytes"
                                    ]
                                ),
                            )
                        )
                    else:
                        reservation_observation = (
                            self._artifact_roots
                            .adjust_capacity_reservation(
                                project_id,
                                artifact_root_binding,
                                reservation_id=reservation_id,
                                reserved_bytes=int(
                                    normalized_storage_plan[
                                        "estimated_artifact_bytes"
                                    ]
                                ),
                                safety_reserve_bytes=int(
                                    normalized_storage_plan[
                                        "safety_reserve_bytes"
                                    ]
                                ),
                            )
                        )
                except ProjectArtifactRootError as error:
                    raise PipelineJobUnavailable(
                        "pipeline storage reservation is unavailable"
                    ) from error
                job.provenance["capacity_reservation"] = (
                    reservation_observation
                )
            restart_capture = getattr(
                self._runtime,
                "capture_restartable_input",
                None,
            )
            if callable(restart_capture):
                try:
                    raw_restart_binding = restart_capture(
                        {
                            "job_id": str(job.job_id),
                            "project_id": str(job.project_id),
                            "snapshot_id": str(job.snapshot_id),
                            "file_name": job.file_name,
                            "input_fingerprint": job.input_fingerprint,
                            "rights_confirmed": True,
                        }
                    )
                    if not isinstance(
                        raw_restart_binding,
                        Mapping,
                    ):
                        raise ValueError(
                            "restart binding is unavailable"
                        )
                    restart_binding = dict(raw_restart_binding)
                    _validate_safe_json(
                        restart_binding,
                        "pipeline input restart binding",
                    )
                    binding_body = {
                        key: value
                        for key, value in restart_binding.items()
                        if key != "binding_sha256"
                    }
                    if (
                        restart_binding.get("schema_version")
                        != PIPELINE_INPUT_RESTART_BINDING_SCHEMA
                        or restart_binding.get("project_id")
                        != str(job.project_id)
                        or restart_binding.get("job_id")
                        != str(job.job_id)
                        or restart_binding.get("snapshot_id")
                        != str(job.snapshot_id)
                        or restart_binding.get("file_name")
                        != job.file_name
                        or restart_binding.get("input_fingerprint")
                        != job.input_fingerprint
                        or restart_binding.get("rights_confirmed")
                        is not True
                        or restart_binding.get("path_exposed") is not False
                        or restart_binding.get("binding_sha256")
                        != hashlib.sha256(
                            _canonical_json(binding_body)
                        ).hexdigest()
                    ):
                        raise ValueError(
                            "restart binding identity changed"
                        )
                except Exception as error:
                    self._release_job_capacity_reservation(job)
                    raise PipelineJobUnavailable(
                        "The selected media could not be bound for restart"
                    ) from error
                job.provenance["input_restart_binding"] = (
                    restart_binding
                )
            self._jobs[job.job_id] = job
            self._persist_job_locked(job)
            self._ensure_worker_locked()
            try:
                self._queue.put_nowait(job.job_id)
            except queue.Full as error:
                self._jobs.pop(job.job_id, None)
                self._release_job_capacity_reservation(job)
                raise PipelineJobUnavailable("pipeline queue is full") from error
            return self._serialize(job), False

    def _rerun_replay_locked(
        self,
        *,
        project_id: UUID,
        idempotency_key: str,
        request_sha256: str,
    ) -> Optional[dict[str, Any]]:
        for existing in self._jobs.values():
            if existing.idempotency_key != idempotency_key:
                continue
            lineage = existing.provenance.get("rerun")
            if (
                existing.project_id != project_id
                or not isinstance(lineage, Mapping)
                or lineage.get("schema_version")
                != PIPELINE_JOB_RERUN_SCHEMA
                or lineage.get("request_sha256") != request_sha256
            ):
                raise PipelineJobConflict(
                    "The idempotency key belongs to a different pipeline request"
                )
            return self._serialize(existing)
        if any(
            alias.get("idempotency_key") == idempotency_key
            for alias in self._aliases.values()
        ):
            raise PipelineJobConflict(
                "The idempotency key belongs to a different pipeline request"
            )
        return None

    def rerun_with_current_profile(
        self,
        project_id: UUID,
        source_job_id: UUID,
        *,
        expected_source_record_sha256: str,
        expected_profile_revision: int,
        rights_confirmed: bool,
        user_confirmed: bool,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Create one new job from a verified server-owned source identity.

        Only the source media identity and its successful intake/SHA checkpoints
        are reused. The source record is never modified. The current project
        profile, artifact-root binding, storage plan and runtime/model
        capability manifests are captured afresh for the new immutable job.
        """

        self._validate_project_id(project_id)
        if not isinstance(source_job_id, UUID):
            raise ValueError("source_job_id must be a UUID")
        if (
            not isinstance(expected_source_record_sha256, str)
            or _SHA256.fullmatch(expected_source_record_sha256) is None
        ):
            raise ValueError("expected_source_record_sha256 is invalid")
        if (
            isinstance(expected_profile_revision, bool)
            or not isinstance(expected_profile_revision, int)
            or expected_profile_revision < 1
        ):
            raise ValueError("expected_profile_revision is invalid")
        if rights_confirmed is not True:
            raise PipelineJobConflict(
                "Video rights must be confirmed before a pipeline rerun"
            )
        if user_confirmed is not True:
            raise PipelineJobConflict(
                "The current-profile pipeline rerun requires user confirmation"
            )
        if (
            not isinstance(idempotency_key, str)
            or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
            or idempotency_key.startswith(_EXPLICIT_NEW_SOURCE_PREFIX)
        ):
            raise ValueError("idempotency_key is invalid")
        request_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "schema_version": PIPELINE_JOB_RERUN_SCHEMA,
                    "policy": _SERVER_OWNED_RERUN_POLICY,
                    "project_id": str(project_id),
                    "source_job_id": str(source_job_id),
                    "expected_source_record_sha256": (
                        expected_source_record_sha256
                    ),
                    "expected_profile_revision": expected_profile_revision,
                    "rights_confirmed": True,
                    "user_confirmed": True,
                }
            )
        ).hexdigest()

        with self._lock:
            replay = self._rerun_replay_locked(
                project_id=project_id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                return replay, True
            source = self._jobs.get(source_job_id)
            if source is None or source.project_id != project_id:
                raise PipelineJobNotFound(
                    "The source pipeline job was not found in this project"
                )
            if source.record_sha256 is None:
                raise PipelineJobUnavailable(
                    "The source pipeline record digest is unavailable"
                )
            if source.record_sha256 != expected_source_record_sha256:
                raise PipelineJobConflict(
                    "The source pipeline job changed after rerun confirmation"
                )
            if source.status not in _RERUN_SOURCE_JOB_STATUSES:
                raise PipelineJobConflict(
                    "An active pipeline job cannot be used as a rerun source"
                )
            profile = self._profiles.get(project_id)
            if profile is None:
                raise PipelineProfileMissing(
                    "Configure the project pipeline profile before rerunning video"
                )
            if profile.get("revision") != expected_profile_revision:
                raise PipelineJobConflict(
                    "The pipeline profile changed after rerun confirmation"
                )
            profile_snapshot = dict(profile)
            source_profile_sha256 = hashlib.sha256(
                _canonical_json(source.profile_snapshot)
            ).hexdigest()
            source_capability_sha256 = hashlib.sha256(
                _canonical_json(source.capability_snapshot)
            ).hexdigest()
            if (
                source.provenance.get("profile_sha256")
                != source_profile_sha256
                or source.provenance.get("capability_sha256")
                != source_capability_sha256
            ):
                raise PipelineJobConflict(
                    "The source pipeline provenance is inconsistent"
                )
            intake_stage = source.stages[
                PIPELINE_STAGES.index("intake_validation")
            ]
            sha_stage = source.stages[PIPELINE_STAGES.index("sha256")]
            if (
                intake_stage.get("status") != "success"
                or intake_stage.get("outcome") != "success"
                or sha_stage.get("status") != "success"
                or sha_stage.get("outcome") != "success"
                or not isinstance(intake_stage.get("checkpoint"), Mapping)
                or not isinstance(sha_stage.get("checkpoint"), Mapping)
            ):
                raise PipelineJobConflict(
                    "The source pipeline lacks verified intake and SHA checkpoints"
                )
            intake_checkpoint = json.loads(
                json.dumps(intake_stage["checkpoint"])
            )
            sha_checkpoint = json.loads(json.dumps(sha_stage["checkpoint"]))
            intake_checkpoint.pop("artifact_root_binding", None)
            sha_checkpoint.pop("artifact_root_binding", None)
            duration_seconds = intake_checkpoint.get("duration_seconds")
            source_sha256 = sha_checkpoint.get("source_sha256")
            raw_hash_job_id = sha_checkpoint.get("hash_job_id")
            source_total_bytes = sha_checkpoint.get("total_bytes")
            if (
                isinstance(duration_seconds, bool)
                or not isinstance(duration_seconds, (int, float))
                or not math.isfinite(float(duration_seconds))
                or duration_seconds <= 0
                or not isinstance(source_sha256, str)
                or _SHA256.fullmatch(source_sha256) is None
                or not isinstance(raw_hash_job_id, str)
                or isinstance(source_total_bytes, bool)
                or not isinstance(source_total_bytes, int)
                or source_total_bytes <= 0
            ):
                raise PipelineJobConflict(
                    "The source pipeline media checkpoints are invalid"
                )
            try:
                source_hash_job_id = UUID(raw_hash_job_id)
            except ValueError as error:
                raise PipelineJobConflict(
                    "The source pipeline media checkpoints are invalid"
                ) from error
            source_snapshot_id = source.snapshot_id
            source_file_name = source.file_name
            source_input_fingerprint = source.input_fingerprint
            source_profile_revision = source.profile_snapshot["revision"]

        resolver = getattr(
            self._runtime,
            "validate_server_owned_rerun_source",
            None,
        )
        if not callable(resolver):
            raise PipelineJobUnavailable(
                "Server-owned pipeline rerun media validation is unavailable"
            )
        try:
            raw_media_proof = resolver(
                project_id=project_id,
                snapshot_id=source_snapshot_id,
                file_name=source_file_name,
                sha_checkpoint=sha_checkpoint,
            )
        except PipelineStageFailure as error:
            raise PipelineJobConflict(
                "The server-owned source media could not be revalidated"
            ) from error
        except Exception as error:
            raise PipelineJobUnavailable(
                "Server-owned pipeline rerun media validation failed"
            ) from error
        try:
            media_proof = _validated_rerun_media_proof(
                raw_media_proof,
                project_id=project_id,
                snapshot_id=source_snapshot_id,
                file_name=source_file_name,
                hash_job_id=source_hash_job_id,
                source_sha256=source_sha256,
                total_bytes=source_total_bytes,
            )
        except (TypeError, ValueError) as error:
            raise PipelineJobUnavailable(
                "Server-owned pipeline rerun media proof is invalid"
            ) from error

        with self._lock:
            replay = self._rerun_replay_locked(
                project_id=project_id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
            if replay is not None:
                return replay, True
            source = self._jobs.get(source_job_id)
            if (
                source is None
                or source.project_id != project_id
                or source.record_sha256
                != expected_source_record_sha256
                or source.status not in _RERUN_SOURCE_JOB_STATUSES
                or source.snapshot_id != source_snapshot_id
                or source.file_name != source_file_name
                or source.input_fingerprint != source_input_fingerprint
            ):
                raise PipelineJobConflict(
                    "The source pipeline job changed during rerun validation"
                )
            current_profile = self._profiles.get(project_id)
            if (
                current_profile is None
                or current_profile.get("revision")
                != expected_profile_revision
                or hashlib.sha256(
                    _canonical_json(current_profile)
                ).hexdigest()
                != hashlib.sha256(
                    _canonical_json(profile_snapshot)
                ).hexdigest()
            ):
                raise PipelineJobConflict(
                    "The pipeline profile changed during rerun validation"
                )
            try:
                capability_snapshot = dict(
                    self._runtime.capability_snapshot(profile_snapshot)
                )
                _validate_safe_json(
                    capability_snapshot,
                    "pipeline rerun capability snapshot",
                )
            except Exception as error:
                raise PipelineJobUnavailable(
                    "The current runtime capability snapshot is unavailable"
                ) from error
            profile_sha256 = hashlib.sha256(
                _canonical_json(profile_snapshot)
            ).hexdigest()
            capability_sha256 = hashlib.sha256(
                _canonical_json(capability_snapshot)
            ).hexdigest()

            artifact_root_binding: Optional[dict[str, Any]] = None
            storage_plan: Optional[dict[str, Any]] = None
            if self._artifact_roots is not None:
                try:
                    artifact_root_binding = dict(
                        self._capture_job_artifact_root(
                            project_id, profile_snapshot
                        )
                    )
                except ProjectArtifactRootNotFound as error:
                    raise PipelineJobConflict(
                        "The pipeline profile output location is not registered"
                    ) from error
                except ProjectArtifactRootConflict as error:
                    raise PipelineJobConflict(
                        "The pipeline profile output location changed"
                    ) from error
                except ProjectArtifactRootUnavailable as error:
                    raise PipelineJobUnavailable(
                        "The pipeline profile output location is unavailable"
                    ) from error
                from .folder_pipeline_batches import (
                    build_pipeline_storage_plan,
                )

                storage_plan = _validated_storage_plan(
                    build_pipeline_storage_plan(
                        duration_seconds=float(duration_seconds),
                        candidate_rescan_fps=int(
                            profile_snapshot["candidate_rescan_fps"]
                        ),
                    ),
                    candidate_rescan_fps=int(
                        profile_snapshot["candidate_rescan_fps"]
                    ),
                )
                assert storage_plan is not None
            fingerprint = hashlib.sha256(
                _canonical_json(
                    {
                        "schema_version": PIPELINE_JOB_RERUN_SCHEMA,
                        "request_sha256": request_sha256,
                        "snapshot_id": str(source_snapshot_id),
                        "file_name": source_file_name,
                        "source_sha256": source_sha256,
                        "profile": profile_snapshot,
                        "capabilities": capability_snapshot,
                        "storage_plan": storage_plan,
                    }
                )
            ).hexdigest()
            for existing in self._jobs.values():
                if existing.input_fingerprint != fingerprint:
                    continue
                lineage = existing.provenance.get("rerun")
                if (
                    existing.project_id != project_id
                    or not isinstance(lineage, Mapping)
                    or lineage.get("request_sha256") != request_sha256
                ):
                    raise PipelineJobConflict(
                        "The rerun input fingerprint belongs to different input"
                    )
                return self._serialize(existing), True

            self._evict_locked()
            if len(self._jobs) >= MAX_PIPELINE_JOBS:
                raise PipelineJobUnavailable("pipeline registry is full")
            now = _utc_now()
            now_text = now.isoformat()
            stages = [
                {
                    "name": name,
                    "status": "pending",
                    "outcome": None,
                    "attempts": 0,
                    "started_at": None,
                    "completed_at": None,
                    "error_code": None,
                    "checkpoint": {},
                    "metrics": {},
                }
                for name in PIPELINE_STAGES
            ]
            for stage_name, checkpoint in (
                ("intake_validation", intake_checkpoint),
                ("sha256", sha_checkpoint),
            ):
                stage = stages[PIPELINE_STAGES.index(stage_name)]
                stage.update(
                    {
                        "status": "success",
                        "outcome": "success",
                        "started_at": now_text,
                        "completed_at": now_text,
                        "checkpoint": checkpoint,
                        "metrics": {
                            "server_owned_rerun_reused": True,
                            "source_job_id": str(source_job_id),
                        },
                    }
                )
            job_id = uuid4()
            rerun_lineage = {
                "schema_version": PIPELINE_JOB_RERUN_SCHEMA,
                "policy": _SERVER_OWNED_RERUN_POLICY,
                "request_sha256": request_sha256,
                "source_job_id": str(source_job_id),
                "source_record_sha256": expected_source_record_sha256,
                "source_input_fingerprint": source_input_fingerprint,
                "source_snapshot_id": str(source_snapshot_id),
                "source_file_name": source_file_name,
                "source_sha256": source_sha256,
                "source_hash_job_id": str(source_hash_job_id),
                "source_total_bytes": source_total_bytes,
                "source_profile_revision": source_profile_revision,
                "source_profile_sha256": source_profile_sha256,
                "source_capability_sha256": source_capability_sha256,
                "target_job_id": str(job_id),
                "target_input_fingerprint": fingerprint,
                "target_profile_revision": expected_profile_revision,
                "target_profile_sha256": profile_sha256,
                "target_capability_sha256": capability_sha256,
                "media_proof": media_proof,
                "rights_confirmed": True,
                "user_confirmed": True,
                "created_at": now_text,
                "path_exposed": False,
            }
            _validate_rerun_lineage(
                rerun_lineage,
                project_id=project_id,
                job_id=job_id,
                snapshot_id=source_snapshot_id,
                file_name=source_file_name,
                input_fingerprint=fingerprint,
                profile_snapshot=profile_snapshot,
                capability_snapshot=capability_snapshot,
            )
            provenance = {
                "schema_version": PIPELINE_JOB_SCHEMA,
                "source": {
                    "snapshot_id": str(source_snapshot_id),
                    "file_name": source_file_name,
                    "rights_confirmed": True,
                },
                "profile_revision": expected_profile_revision,
                "profile_sha256": profile_sha256,
                "capability_sha256": capability_sha256,
                "capability_attempts": [],
                "input_storage_plan": storage_plan,
                "stage_order": list(PIPELINE_STAGES),
                "route_policy": (
                    "timed_gps_only_never_capture_start_interpolation"
                ),
                "provider_policy": (
                    "codex_claude_structured_proposal_not_bulk_frames"
                ),
                "rerun": rerun_lineage,
                **(
                    {"artifact_root": artifact_root_binding}
                    if artifact_root_binding is not None
                    else {}
                ),
                **(
                    {
                        "storage_plan": storage_plan,
                        "storage_plan_source": (
                            "server_owned_rerun_verified_intake_duration"
                        ),
                    }
                    if storage_plan is not None
                    else {}
                ),
            }
            job = PipelineJobRecord(
                job_id=job_id,
                project_id=project_id,
                snapshot_id=source_snapshot_id,
                file_name=source_file_name,
                idempotency_key=idempotency_key,
                input_fingerprint=fingerprint,
                profile_snapshot=profile_snapshot,
                capability_snapshot=capability_snapshot,
                status="queued",
                current_stage="metadata_gps_preflight",
                outcome_code=None,
                error_code=None,
                retry_count=0,
                resume_count=0,
                review_completion_id=None,
                stages=stages,
                provenance=provenance,
                created_at=now,
                updated_at=now,
                recovered_after_restart=False,
                cancel_event=threading.Event(),
            )
            if (
                storage_plan is not None
                and self._artifact_roots is not None
                and artifact_root_binding is not None
            ):
                try:
                    job.provenance["capacity_reservation"] = (
                        self._artifact_roots.adjust_capacity_reservation(
                            project_id,
                            artifact_root_binding,
                            reservation_id=f"pipeline-job:{job.job_id}",
                            reserved_bytes=int(
                                storage_plan["estimated_artifact_bytes"]
                            ),
                            safety_reserve_bytes=int(
                                storage_plan["safety_reserve_bytes"]
                            ),
                        )
                    )
                except ProjectArtifactRootError as error:
                    raise PipelineJobUnavailable(
                        "pipeline rerun storage reservation is unavailable"
                    ) from error
            self._jobs[job.job_id] = job
            try:
                self._persist_job_locked(job)
            except Exception:
                self._jobs.pop(job.job_id, None)
                self._release_job_capacity_reservation(job)
                raise
            self._ensure_worker_locked()
            try:
                self._queue.put_nowait(job.job_id)
            except queue.Full as error:
                self._jobs.pop(job.job_id, None)
                self._release_job_capacity_reservation(job)
                raise PipelineJobUnavailable("pipeline queue is full") from error
            return self._serialize(job), False

    def start_pinned(
        self,
        project_id: UUID,
        *,
        snapshot_id: UUID,
        file_name: str,
        rights_confirmed: bool,
        idempotency_key: str,
        expected_profile_revision: int,
        expected_artifact_root_binding: Optional[
            Mapping[str, Any]
        ],
        storage_plan: Optional[Mapping[str, Any]] = None,
        capacity_reservation_parent_id: Optional[str] = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reject a batch whose profile/root changed in flight."""

        return self.start(
            project_id,
            snapshot_id=snapshot_id,
            file_name=file_name,
            rights_confirmed=rights_confirmed,
            idempotency_key=idempotency_key,
            expected_profile_revision=expected_profile_revision,
            expected_artifact_root_binding=(
                expected_artifact_root_binding
            ),
            storage_plan=storage_plan,
            capacity_reservation_parent_id=(
                capacity_reservation_parent_id
            ),
        )

    def start_batch_pinned(
        self,
        project_id: UUID,
        *,
        inputs: Sequence[Mapping[str, Any]],
        rights_confirmed: bool,
        expected_profile_revision: int,
        expected_artifact_root_binding: Optional[
            Mapping[str, Any]
        ],
        capacity_reservation_parent_id: Optional[str] = None,
    ) -> list[tuple[dict[str, Any], bool]]:
        if (
            not isinstance(inputs, Sequence)
            or isinstance(inputs, (str, bytes))
            or not 1 <= len(inputs) <= 128
        ):
            raise ValueError("pipeline batch inputs are invalid")
        normalized: list[dict[str, Any]] = []
        for item in inputs:
            if not isinstance(item, Mapping) or set(item) not in {
                frozenset(
                    {
                        "snapshot_id",
                        "file_name",
                        "idempotency_key",
                    }
                ),
                frozenset(
                    {
                        "snapshot_id",
                        "file_name",
                        "idempotency_key",
                        "storage_plan",
                    }
                ),
            }:
                raise ValueError("pipeline batch input is invalid")
            normalized.append(
                {
                    "snapshot_id": item["snapshot_id"],
                    "file_name": item["file_name"],
                    "idempotency_key": item["idempotency_key"],
                    "storage_plan": item.get("storage_plan"),
                }
            )
        # Hold the same profile lock used by configure_profile across every
        # per-file start. A profile/output-root change therefore occurs
        # either before the batch (all rejected) or after it (all pinned).
        with self._lock:
            profile = self._profiles.get(project_id)
            if (
                profile is None
                or profile.get("revision")
                != expected_profile_revision
            ):
                raise PipelineJobConflict(
                    "The pipeline profile changed after batch preflight"
                )
            results: list[tuple[dict[str, Any], bool]] = []
            for item in normalized:
                results.append(
                    self.start(
                        project_id,
                        snapshot_id=item["snapshot_id"],
                        file_name=str(item["file_name"]),
                        rights_confirmed=rights_confirmed,
                        idempotency_key=str(
                            item["idempotency_key"]
                        ),
                        expected_profile_revision=(
                            expected_profile_revision
                        ),
                        expected_artifact_root_binding=(
                            expected_artifact_root_binding
                        ),
                        storage_plan=item["storage_plan"],
                        capacity_reservation_parent_id=(
                            capacity_reservation_parent_id
                        ),
                    )
                )
            return results

    def get(self, project_id: UUID, job_id: UUID) -> dict[str, Any]:
        with self._lock:
            return self._serialize(self._job(project_id, job_id))

    def get_user_discovered_candidate_registration(
        self,
        project_id: UUID,
        job_id: UUID,
        *,
        candidate_id: str,
        request_sha256: str,
    ) -> Optional[dict[str, Any]]:
        """Return an exact replay record without exposing artifact paths."""

        if (
            not isinstance(candidate_id, str)
            or re.fullmatch(r"[a-f0-9]{20}", candidate_id) is None
            or not isinstance(request_sha256, str)
            or _SHA256.fullmatch(request_sha256) is None
        ):
            raise ValueError(
                "user-discovered registration identity is invalid"
            )
        with self._lock:
            job = self._job(project_id, job_id)
            stage = job.stages[
                PIPELINE_STAGES.index("question_queue")
            ]
            checkpoint = stage.get("checkpoint")
            if not isinstance(checkpoint, Mapping):
                return None
            registrations = checkpoint.get(
                "user_discovered_candidate_registrations", []
            )
            if not isinstance(registrations, list):
                raise PipelineJobConflict(
                    "User-discovered queue provenance is invalid"
                )
            for raw in registrations:
                if (
                    isinstance(raw, Mapping)
                    and raw.get("candidate_id") == candidate_id
                ):
                    if raw.get("request_sha256") != request_sha256:
                        raise PipelineJobConflict(
                            "Candidate identity is bound to another request"
                        )
                    return json.loads(
                        _canonical_json(raw).decode("utf-8")
                    )
        return None

    def register_user_discovered_candidate(
        self,
        project_id: UUID,
        job_id: UUID,
        *,
        expected_question_queue_revision: str,
        candidate_evidence: Mapping[str, Any],
        overlay_checkpoint: Mapping[str, Any],
        delta_review_event_set: Mapping[str, Any],
        delta_event_binding: Mapping[str, Any],
        question_id: UUID,
        question_catalog_registration: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Atomically append one review-only false-negative question.

        Existing immutable question/event bindings are never rewritten.  The
        new candidate owns a one-event delta ReviewEventSet; the queue stores
        a signed collection revision across the original set and every delta.
        """

        if (
            not isinstance(expected_question_queue_revision, str)
            or _SHA256.fullmatch(
                expected_question_queue_revision
            )
            is None
            or not isinstance(question_id, UUID)
        ):
            raise ValueError(
                "user-discovered queue concurrency token is invalid"
            )
        for label, value in (
            ("candidate evidence", candidate_evidence),
            ("candidate overlay", overlay_checkpoint),
            ("delta review event set", delta_review_event_set),
            ("delta event binding", delta_event_binding),
            (
                "question catalog registration",
                question_catalog_registration,
            ),
        ):
            if not isinstance(value, Mapping):
                raise ValueError(f"{label} is invalid")
            _validate_safe_json(value, label)

        candidate_id = candidate_evidence.get("candidate_id")
        request_sha256 = candidate_evidence.get("request_sha256")
        analysis_job_id = candidate_evidence.get("analysis_job_id")
        source_sha256 = candidate_evidence.get("source_sha256")
        overlay_id = candidate_evidence.get("overlay_id")
        truth_fields = (
            "annotations_created",
            "incidents_created",
            "training_members_created",
            "report_truth_records_created",
        )
        if (
            candidate_evidence.get("schema_version")
            != "trafficvisson.user-discovered-candidate.v1"
            or candidate_evidence.get("candidate_kind")
            != "user_discovered"
            or candidate_evidence.get("project_id")
            != str(project_id)
            or candidate_evidence.get("pipeline_job_id")
            != str(job_id)
            or not isinstance(candidate_id, str)
            or re.fullmatch(r"[a-f0-9]{20}", candidate_id) is None
            or str(question_id)
            != str(uuid5(UUID(str(analysis_job_id)), candidate_id))
            or not isinstance(request_sha256, str)
            or _SHA256.fullmatch(request_sha256) is None
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or candidate_evidence.get("review_only") is not True
            or any(
                candidate_evidence.get(field) != 0
                for field in truth_fields
            )
            or overlay_checkpoint.get("overlay_id") != overlay_id
            or overlay_checkpoint.get("candidate_count") != 1
            or overlay_checkpoint.get("analysis_job_id")
            != analysis_job_id
            or overlay_checkpoint.get("pipeline_job_id")
            != str(job_id)
            or overlay_checkpoint.get("source_sha256")
            != source_sha256
            or overlay_checkpoint.get("review_only") is not True
            or any(
                overlay_checkpoint.get(field) != 0
                for field in truth_fields
            )
            or delta_review_event_set.get("overlay_id")
            != overlay_id
            or delta_review_event_set.get("analysis_job_id")
            != analysis_job_id
            or delta_review_event_set.get("pipeline_job_id")
            != str(job_id)
            or delta_review_event_set.get("source_sha256")
            != source_sha256
            or delta_review_event_set.get("event_count") != 1
            or delta_review_event_set.get("review_only") is not True
            or any(
                delta_review_event_set.get(field) != 0
                for field in truth_fields
            )
            or delta_event_binding.get(
                "representative_candidate_id"
            )
            != candidate_id
            or delta_event_binding.get("event_id")
            not in delta_review_event_set.get("event_ids", [])
            or not isinstance(
                delta_event_binding.get("event_revision"), str
            )
            or _SHA256.fullmatch(
                str(delta_event_binding.get("event_revision"))
            )
            is None
            or question_catalog_registration.get("project_id")
            != str(project_id)
            or question_catalog_registration.get("analysis_job_id")
            != analysis_job_id
            or question_catalog_registration.get("source_sha256")
            != source_sha256
            or question_catalog_registration.get("question_count")
            != 1
            or not isinstance(
                question_catalog_registration.get(
                    "question_set_sha256"
                ),
                str,
            )
            or _SHA256.fullmatch(
                str(
                    question_catalog_registration.get(
                        "question_set_sha256"
                    )
                )
            )
            is None
        ):
            raise PipelineJobConflict(
                "User-discovered review evidence failed validation"
            )

        with self._lock:
            job = self._job(project_id, job_id)
            if (
                job.status != "awaiting_review"
                or job.current_stage != "question_queue"
            ):
                raise PipelineJobConflict(
                    "The pipeline is not accepting review candidates"
                )
            stage = job.stages[
                PIPELINE_STAGES.index("question_queue")
            ]
            checkpoint = stage.get("checkpoint")
            if (
                stage.get("status") != "success"
                or not isinstance(checkpoint, Mapping)
                or checkpoint.get("analysis_job_id")
                != analysis_job_id
                or checkpoint.get("source_sha256")
                != source_sha256
            ):
                raise PipelineJobConflict(
                    "The prepared question queue changed"
                )
            existing_registration = (
                self.get_user_discovered_candidate_registration(
                    project_id,
                    job_id,
                    candidate_id=candidate_id,
                    request_sha256=request_sha256,
                )
            )
            if existing_registration is not None:
                return existing_registration, True
            if (
                checkpoint.get("question_queue_revision")
                != expected_question_queue_revision
            ):
                raise PipelineJobConflict(
                    "The question queue revision is stale"
                )
            old_candidate_ids = checkpoint.get(
                "question_candidate_ids"
            )
            zero_candidate_survey_queue = (
                checkpoint.get("schema_version")
                == ZERO_CANDIDATE_SURVEY_QUEUE_SCHEMA
                and checkpoint.get("question_count") == 0
            )
            if (
                not isinstance(old_candidate_ids, list)
                or (
                    not old_candidate_ids
                    and not zero_candidate_survey_queue
                )
                or candidate_id in old_candidate_ids
                or len(old_candidate_ids) >= 512
            ):
                raise PipelineJobConflict(
                    "The question queue cannot accept another candidate"
                )
            old_bindings = checkpoint.get(
                "review_event_bindings"
            )
            if (
                checkpoint.get("review_event_set_state")
                not in {
                    "materialized",
                    "materialized_collection",
                    "empty_promoted_survey",
                }
                or (
                    checkpoint.get("review_event_set_state")
                    == "empty_promoted_survey"
                    and not zero_candidate_survey_queue
                )
                or not isinstance(old_bindings, list)
                or len(old_bindings) != len(old_candidate_ids)
            ):
                raise PipelineJobConflict(
                    "The immutable review-event queue is unavailable"
                )

            old_sets = checkpoint.get("review_event_sets")
            if old_sets is None:
                original_set = checkpoint.get("review_event_set")
                if not isinstance(original_set, Mapping):
                    raise PipelineJobConflict(
                        "The original review-event set is unavailable"
                    )
                old_sets = [
                    {
                        "review_event_set_id": checkpoint.get(
                            "review_event_set_id"
                        ),
                        "review_event_set_revision": checkpoint.get(
                            "review_event_set_revision"
                        ),
                        "manifest_sha256": checkpoint.get(
                            "review_event_set_manifest_sha256"
                        ),
                        "overlay_id": original_set.get("overlay_id"),
                        "candidate_ids": list(old_candidate_ids),
                        "event_count": len(old_candidate_ids),
                    }
                ]
                old_bindings = [
                    {
                        **dict(binding),
                        "review_event_set_id": checkpoint.get(
                            "review_event_set_id"
                        ),
                        "review_event_set_revision": checkpoint.get(
                            "review_event_set_revision"
                        ),
                        "review_event_set_manifest_sha256": (
                            checkpoint.get(
                                "review_event_set_manifest_sha256"
                            )
                        ),
                    }
                    for binding in old_bindings
                ]
            if (
                not isinstance(old_sets, list)
                or len(old_sets) >= 65
            ):
                raise PipelineJobConflict(
                    "The review-event collection is full"
                )

            delta_descriptor = {
                "review_event_set_id": (
                    delta_review_event_set[
                        "review_event_set_id"
                    ]
                ),
                "review_event_set_revision": (
                    delta_review_event_set[
                        "review_event_set_revision"
                    ]
                ),
                "manifest_sha256": (
                    delta_review_event_set["manifest_sha256"]
                ),
                "overlay_id": overlay_id,
                "candidate_ids": [candidate_id],
                "event_count": 1,
            }
            extended_binding = {
                "event_id": delta_event_binding["event_id"],
                "representative_candidate_id": candidate_id,
                "event_revision": delta_event_binding[
                    "event_revision"
                ],
                "review_event_set_id": delta_descriptor[
                    "review_event_set_id"
                ],
                "review_event_set_revision": delta_descriptor[
                    "review_event_set_revision"
                ],
                "review_event_set_manifest_sha256": (
                    delta_descriptor["manifest_sha256"]
                ),
            }
            new_candidate_ids = [*old_candidate_ids, candidate_id]
            review_sets = [*old_sets, delta_descriptor]
            review_bindings = [*old_bindings, extended_binding]
            collection_revision = hashlib.sha256(
                _canonical_json(
                    {
                        "pipeline_job_id": str(job_id),
                        "analysis_job_id": analysis_job_id,
                        "source_sha256": source_sha256,
                        "question_candidate_ids": new_candidate_ids,
                        "review_event_sets": review_sets,
                        "review_event_bindings": review_bindings,
                    }
                )
            ).hexdigest()

            catalog_registrations = checkpoint.get(
                "question_catalog_registrations"
            )
            if catalog_registrations is None:
                catalog_registrations = [
                    {
                        "kind": "prepared_queue",
                        "candidate_ids": list(old_candidate_ids),
                        "question_set_sha256": checkpoint.get(
                            "question_catalog_revision"
                        ),
                    }
                ]
            if not isinstance(catalog_registrations, list):
                raise PipelineJobConflict(
                    "Question catalog provenance is invalid"
                )
            catalog_delta = {
                "kind": "user_discovered_delta",
                "candidate_ids": [candidate_id],
                "question_id": str(question_id),
                "question_set_sha256": (
                    question_catalog_registration[
                        "question_set_sha256"
                    ]
                ),
                "evidence_binding_sha256": (
                    question_catalog_registration.get(
                        "evidence_binding_sha256"
                    )
                ),
            }
            catalog_registrations = [
                *catalog_registrations,
                catalog_delta,
            ]
            catalog_revision = hashlib.sha256(
                _canonical_json(catalog_registrations)
            ).hexdigest()

            evidence_snapshot = json.loads(
                _canonical_json(candidate_evidence).decode("utf-8")
            )
            registration_base = {
                "schema_version": (
                    USER_DISCOVERED_QUEUE_REGISTRATION_SCHEMA
                ),
                "candidate_id": candidate_id,
                "question_id": str(question_id),
                "request_sha256": request_sha256,
                "candidate_evidence": evidence_snapshot,
                "overlay": dict(overlay_checkpoint),
                "delta_review_event_set": delta_descriptor,
                "delta_event_binding": extended_binding,
                "question_catalog_delta": catalog_delta,
                "review_only": True,
                "annotations_created": 0,
                "incidents_created": 0,
                "training_members_created": 0,
                "report_truth_records_created": 0,
            }
            existing_registrations = checkpoint.get(
                "user_discovered_candidate_registrations", []
            )
            if (
                not isinstance(existing_registrations, list)
                or len(existing_registrations) >= 64
            ):
                raise PipelineJobConflict(
                    "The user-discovered candidate queue is full"
                )
            registrations_for_hash = [
                *existing_registrations,
                registration_base,
            ]
            queue_revision = hashlib.sha256(
                _canonical_json(
                    {
                        "previous_question_queue_revision": (
                            expected_question_queue_revision
                        ),
                        "question_candidate_ids": new_candidate_ids,
                        "review_event_set_collection_revision": (
                            collection_revision
                        ),
                        "question_catalog_revision": (
                            catalog_revision
                        ),
                        "user_discovered_candidates": (
                            registrations_for_hash
                        ),
                    }
                )
            ).hexdigest()
            registration = {
                **registration_base,
                "question_queue_revision": queue_revision,
                "question_catalog_revision": catalog_delta[
                    "question_set_sha256"
                ],
                "question_catalog_collection_revision": (
                    catalog_revision
                ),
                "review_event_set_collection_revision": (
                    collection_revision
                ),
            }
            registrations = [
                *existing_registrations,
                registration,
            ]
            structured = dict(
                checkpoint.get(
                    "structured_judgments_by_candidate", {}
                )
            )
            structured[candidate_id] = {
                "candidate_id": candidate_id,
                "status": "not_run",
                "provider": "local_user",
                "proposal": None,
                "proposal_sha256": None,
                "error_code": (
                    "user_discovered_pending_review"
                ),
                "user_issue_type_proposal": (
                    candidate_evidence.get(
                        "issue_type_proposal"
                    )
                ),
                "review_only": True,
                "automatic_annotation_allowed": False,
                "automatic_incident_allowed": False,
                "automatic_training_membership_allowed": False,
                "automatic_report_truth_allowed": False,
            }
            human_ids = list(
                checkpoint.get(
                    "human_question_candidate_ids", []
                )
            )
            mandatory_ids = list(
                checkpoint.get(
                    "mandatory_human_candidate_ids", []
                )
            )
            human_ids.append(candidate_id)
            mandatory_ids.append(candidate_id)
            overlays = list(
                checkpoint.get(
                    "user_discovered_candidate_overlays", []
                )
            )
            overlays.append(
                {
                    "overlay_id": overlay_id,
                    "candidate_count": 1,
                    "candidate_id": candidate_id,
                    "manifest_sha256": overlay_checkpoint.get(
                        "manifest_sha256"
                    ),
                    "request_sha256": request_sha256,
                }
            )
            next_checkpoint = {
                **dict(checkpoint),
                "question_count": len(new_candidate_ids),
                "question_candidate_ids": new_candidate_ids,
                "human_question_count": len(human_ids),
                "human_question_candidate_ids": human_ids,
                "mandatory_human_candidate_ids": mandatory_ids,
                "question_queue_revision": queue_revision,
                "review_event_set_state": (
                    "materialized_collection"
                ),
                "review_event_sets": review_sets,
                "review_event_set_collection_revision": (
                    collection_revision
                ),
                "review_event_count": len(review_bindings),
                "review_event_ids": [
                    binding["event_id"]
                    for binding in review_bindings
                ],
                "review_event_bindings": review_bindings,
                "question_catalog_state": "registered",
                "question_catalog_revision": (
                    catalog_delta["question_set_sha256"]
                    if zero_candidate_survey_queue
                    else checkpoint.get(
                        "question_catalog_revision"
                    )
                ),
                "question_catalog_collection_revision": (
                    catalog_revision
                ),
                "question_catalog_registrations": (
                    catalog_registrations
                ),
                "structured_judgments_by_candidate": structured,
                "structured_not_run_count": int(
                    checkpoint.get(
                        "structured_not_run_count", 0
                    )
                )
                + 1,
                "user_discovered_candidate_overlays": overlays,
                "user_discovered_candidate_registrations": (
                    registrations
                ),
                "review_only": True,
                "automatic_incident_allowed": False,
                "automatic_report_truth_allowed": False,
                "incident_count": 0,
                "report_incident_count": 0,
            }
            _validate_safe_json(
                next_checkpoint,
                "user-discovered question queue",
            )
            previous_checkpoint = stage["checkpoint"]
            previous_metrics = stage["metrics"]
            previous_updated_at = job.updated_at
            previous_record_sha256 = job.record_sha256
            previous_provenance = dict(job.provenance)
            stage["checkpoint"] = next_checkpoint
            stage["metrics"] = {
                **dict(stage.get("metrics", {})),
                "question_count": len(new_candidate_ids),
                "human_question_count": len(human_ids),
                "review_event_count": len(review_bindings),
            }
            job.provenance["user_discovered_queue_revision"] = (
                queue_revision
            )
            job.provenance["user_discovered_candidate_count"] = (
                len(registrations)
            )
            if job.outcome_code == "zero_detections":
                job.outcome_code = "success"
                job.provenance["zero_candidate_survey_qa"] = {
                    **dict(
                        job.provenance.get(
                            "zero_candidate_survey_qa", {}
                        )
                    ),
                    "state": "miss_candidate_added",
                    "question_queue_revision": queue_revision,
                    "automatic_completion_allowed": False,
                }
            job.updated_at = _utc_now()
            try:
                self._persist_job_locked(job)
            except Exception:
                stage["checkpoint"] = previous_checkpoint
                stage["metrics"] = previous_metrics
                job.updated_at = previous_updated_at
                job.record_sha256 = previous_record_sha256
                job.provenance = previous_provenance
                raise
            return json.loads(
                _canonical_json(registration).decode("utf-8")
            ), False

    def list(self, project_id: UUID, *, limit: int = 100) -> list[dict[str, Any]]:
        self._validate_project_id(project_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 128:
            raise ValueError("pipeline list limit is invalid")
        with self._lock:
            rows = [
                self._serialize(job)
                for job in reversed(tuple(self._jobs.values()))
                if job.project_id == project_id
            ]
        return rows[:limit]

    def is_analysis_projection_gated(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
    ) -> bool:
        """Keep pipeline-owned labels audit-only until review is complete.

        The analysis job is first persisted by ``candidate_rescan_5_10fps``,
        before the question queue exists.  Inspect every durable checkpoint so
        a legacy answer/incident endpoint cannot bypass the pipeline approval
        boundary during those intermediate stages or after a restart.
        """

        self._validate_project_id(project_id)
        if not isinstance(analysis_job_id, UUID):
            raise ValueError("analysis_job_id must be a UUID")
        expected_analysis_job_id = str(analysis_job_id)
        with self._lock:
            for job in self._jobs.values():
                if (
                    job.project_id != project_id
                    or job.review_completion_id is not None
                ):
                    continue
                for stage in job.stages:
                    checkpoint = stage.get("checkpoint")
                    if (
                        isinstance(checkpoint, Mapping)
                        and checkpoint.get("analysis_job_id")
                        == expected_analysis_job_id
                    ):
                        return True
        return False

    def is_analysis_review_closed(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
    ) -> bool:
        """Return whether a PipelineJob already sealed this answer ledger."""

        self._validate_project_id(project_id)
        if not isinstance(analysis_job_id, UUID):
            raise ValueError("analysis_job_id must be a UUID")
        expected_analysis_job_id = str(analysis_job_id)
        with self._lock:
            for job in self._jobs.values():
                if (
                    job.project_id != project_id
                    or job.review_completion_id is None
                ):
                    continue
                if any(
                    isinstance(stage.get("checkpoint"), Mapping)
                    and stage["checkpoint"].get("analysis_job_id")
                    == expected_analysis_job_id
                    for stage in job.stages
                ):
                    return True
        return False

    def resolve_job_artifact_category(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        category: str,
    ) -> Path:
        """Resolve one server-owned job category without exposing its path.

        This is intentionally a bridge-internal callback for artifact
        services.  The renderer can name only the opaque job and preparation
        identities; only the main process/bridge receives the resulting
        absolute directory.
        """

        self._validate_project_id(project_id)
        if (
            not isinstance(pipeline_job_id, UUID)
            or not isinstance(analysis_job_id, UUID)
        ):
            raise ValueError("pipeline artifact identity is invalid")
        if (
            not isinstance(category, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", category)
            is None
        ):
            raise ValueError("pipeline artifact category is invalid")
        with self._lock:
            job = self._job(project_id, pipeline_job_id)
            analysis_identity = str(analysis_job_id)
            if not any(
                isinstance(stage.get("checkpoint"), Mapping)
                and stage["checkpoint"].get("analysis_job_id")
                == analysis_identity
                for stage in job.stages
            ):
                raise PipelineJobConflict(
                    "The analysis does not belong to this pipeline job"
                )
            self._validate_job_artifact_root(job)
            context = self._runtime_context(job)
        resolver = getattr(
            self._runtime,
            "resolve_job_artifact_category",
            None,
        )
        if not callable(resolver):
            raise PipelineJobUnavailable(
                "The pipeline artifact-root resolver is unavailable"
            )
        try:
            resolved = resolver(project_id, context, category)
        except PipelineJobError:
            raise
        except Exception as error:
            raise PipelineJobUnavailable(
                "The pipeline artifact category is unavailable"
            ) from error
        path = Path(resolved)
        if not path.is_absolute():
            raise PipelineJobUnavailable(
                "The pipeline artifact category is invalid"
            )
        return path

    def resolve_completed_media_grant(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
    ) -> CompletedMediaGrant:
        """Restore the exact private input grant for one persisted analysis.

        Candidate evidence may be reviewed after the bridge or PC restarts.
        The renderer never supplies a path: the durable PipelineJob binds the
        analysis identity to its completed SHA checkpoint, and the runtime
        performs a fresh full-hash revalidation of the bridge-private grant.
        """

        self._validate_project_id(project_id)
        if (
            not isinstance(pipeline_job_id, UUID)
            or not isinstance(analysis_job_id, UUID)
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
        ):
            raise ValueError("pipeline media grant identity is invalid")
        with self._lock:
            job = self._job(project_id, pipeline_job_id)
            analysis_identity = str(analysis_job_id)
            durable_analysis_binding = any(
                isinstance(stage.get("checkpoint"), Mapping)
                and stage["checkpoint"].get("analysis_job_id")
                == analysis_identity
                for stage in job.stages
            )
            candidate_stage = job.stages[
                PIPELINE_STAGES.index("candidate_rescan_5_10fps")
            ]
            in_flight_candidate_binding = (
                job.current_stage == "candidate_rescan_5_10fps"
                and candidate_stage.get("status") == "running"
            )
            if (
                not durable_analysis_binding
                and not in_flight_candidate_binding
            ):
                raise PipelineJobConflict(
                    "The analysis does not belong to this pipeline job"
                )
            sha_stage = job.stages[PIPELINE_STAGES.index("sha256")]
            sha_checkpoint = sha_stage.get("checkpoint")
            if (
                sha_stage.get("status") != "success"
                or not isinstance(sha_checkpoint, Mapping)
                or sha_checkpoint.get("source_sha256")
                != source_sha256
            ):
                raise PipelineJobConflict(
                    "The completed source does not match this pipeline job"
                )
            context = self._runtime_context(job)
            expected_snapshot_id = job.snapshot_id
            expected_file_name = job.file_name
            expected_hash_job_id = sha_checkpoint.get("hash_job_id")
            expected_total_bytes = sha_checkpoint.get("total_bytes")
        resolver = getattr(
            self._runtime,
            "resolve_completed_media_grant",
            None,
        )
        if not callable(resolver):
            raise PipelineJobUnavailable(
                "The completed media grant resolver is unavailable"
            )
        try:
            grant = resolver(
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                context=context,
                expected_source_sha256=source_sha256,
            )
        except PipelineJobError:
            raise
        except Exception as error:
            raise PipelineJobUnavailable(
                "The completed media grant is unavailable"
            ) from error
        if (
            not isinstance(grant, CompletedMediaGrant)
            or grant.project_id != project_id
            or grant.snapshot_id != expected_snapshot_id
            or str(grant.hash_job_id) != expected_hash_job_id
            or grant.file_name != expected_file_name
            or grant.sha256_lower != source_sha256
            or grant.size_bytes != expected_total_bytes
        ):
            raise PipelineJobUnavailable(
                "The completed media grant identity changed"
            )
        return grant

    def cancel(self, project_id: UUID, job_id: UUID) -> dict[str, Any]:
        with self._lock:
            job = self._job(project_id, job_id)
            if job.status in _TERMINAL_JOB_STATUSES:
                return self._serialize(job)
            job.cancel_event.set()
            if job.status == "queued":
                self._mark_cancelled_locked(job)
            elif job.status == "running":
                job.status = "cancelling"
                job.updated_at = _utc_now()
                self._persist_job_locked(job)
            else:
                self._mark_cancelled_locked(job)
            context = self._runtime_context(job)
        try:
            self._runtime.cancel(context)
        except Exception:
            pass
        with self._lock:
            return self._serialize(job)

    def resume(self, project_id: UUID, job_id: UUID) -> dict[str, Any]:
        with self._lock:
            job = self._job(project_id, job_id)
            if job.status not in _RESUMABLE_JOB_STATUSES:
                raise PipelineJobConflict(
                    "Only blocked, failed or cancelled pipelines can resume"
                )
            if job.retry_count >= MAX_PIPELINE_RETRIES:
                raise PipelineJobConflict("pipeline retry limit reached")
            try:
                self._validate_job_artifact_root(job)
            except ProjectArtifactRootError as error:
                raise PipelineJobConflict(
                    "The pipeline artifact root no longer matches its job binding"
                ) from error
            capability_snapshot = dict(
                self._runtime.capability_snapshot(job.profile_snapshot)
            )
            _validate_safe_json(
                capability_snapshot, "capability snapshot"
            )
            captured_binding = job.capability_snapshot.get(
                "promoted_project_model_binding"
            )
            current_binding = capability_snapshot.get(
                "promoted_project_model_binding"
            )
            current_index = PIPELINE_STAGES.index(job.current_stage)
            candidate_index = PIPELINE_STAGES.index(
                "candidate_rescan_5_10fps"
            )
            if (
                current_index <= candidate_index
                and captured_binding != current_binding
            ):
                raise PipelineJobConflict(
                    "The promoted project model changed; start an explicit "
                    "pipeline rerun"
                )
            if (
                current_index > candidate_index
                and captured_binding is not None
            ):
                # Later stages consume the already persisted review evidence.
                # Preserve the model captured at start instead of rewriting
                # historical provenance to whichever model is active now.
                capability_snapshot[
                    "promoted_project_model_binding"
                ] = captured_binding
            job.capability_snapshot = capability_snapshot
            job.provenance["capability_sha256"] = hashlib.sha256(
                _canonical_json(capability_snapshot)
            ).hexdigest()
            history = job.provenance.setdefault(
                "capability_attempts", []
            )
            if not isinstance(history, list) or len(history) >= 16:
                raise PipelineJobConflict(
                    "pipeline capability-attempt history is full"
                )
            history.append(
                {
                    "resume_count": job.resume_count + 1,
                    "captured_at": _utc_now().isoformat(),
                    "sha256": hashlib.sha256(
                        _canonical_json(capability_snapshot)
                    ).hexdigest(),
                }
            )
            preserve_partial_checkpoint = (
                job.status == "blocked"
                and job.outcome_code
                in {"partial", "report_preview_blocked"}
                and job.current_stage == "derived_outputs"
            )
            for stage in job.stages[current_index:]:
                reset = {
                    "status": "pending",
                    "outcome": None,
                    "started_at": None,
                    "completed_at": None,
                    "error_code": None,
                }
                if not (
                    preserve_partial_checkpoint
                    and stage["name"] == "derived_outputs"
                ):
                    reset["checkpoint"] = {}
                    reset["metrics"] = {}
                stage.update(reset)
            try:
                self._job_storage_budget_snapshot(
                    job, job.current_stage
                )
            except PipelineStageFailure as error:
                raise PipelineJobConflict(
                    "Pipeline storage cannot be reserved for resume"
                ) from error
            job.retry_count += 1
            job.resume_count += 1
            job.status = "queued"
            job.outcome_code = None
            job.error_code = None
            job.cancel_event = threading.Event()
            job.updated_at = _utc_now()
            self._persist_job_locked(job)
            self._ensure_worker_locked()
            try:
                self._queue.put_nowait(job.job_id)
            except queue.Full as error:
                job.status = "blocked"
                job.outcome_code = "failed"
                job.error_code = "queue_unavailable"
                self._persist_job_locked(job)
                raise PipelineJobUnavailable("pipeline queue is full") from error
            return self._serialize(job)

    @staticmethod
    def _ready_promoted_survey_provider(
        rescan: Any,
    ) -> Optional[Mapping[str, Any]]:
        if not isinstance(rescan, Mapping):
            return None
        discovery = rescan.get("dense_candidate_discovery")
        provider_states = (
            discovery.get("provider_states")
            if isinstance(discovery, Mapping)
            else None
        )
        if not isinstance(provider_states, list):
            return None
        promoted_states = [
            item
            for item in provider_states
            if isinstance(item, Mapping)
            and item.get("provider") == "promoted_project_model"
            and item.get("state") == "ready"
            and item.get("independent_survey_verified") is True
        ]
        if len(promoted_states) != 1:
            return None
        return promoted_states[0]

    def _await_promoted_zero_candidate_survey_locked(
        self,
        job: PipelineJobRecord,
        *,
        zero_stage_name: str,
    ) -> bool:
        """Turn a promoted-model empty result into explicit full-trip QA."""

        zero_stage_index = PIPELINE_STAGES.index(zero_stage_name)
        question_stage_index = PIPELINE_STAGES.index("question_queue")
        if zero_stage_index >= question_stage_index:
            return False
        rescan_stage = job.stages[
            PIPELINE_STAGES.index("candidate_rescan_5_10fps")
        ]
        rescan = rescan_stage.get("checkpoint")
        promoted = self._ready_promoted_survey_provider(rescan)
        if promoted is None or not isinstance(rescan, Mapping):
            return False
        analysis_job_id = rescan.get("analysis_job_id")
        source_sha256 = rescan.get("source_sha256")
        dense_index_sha256 = rescan.get("dense_index_sha256")
        discovery_revision = rescan.get(
            "dense_candidate_discovery_revision"
        )
        queue_base = {
            "schema_version": ZERO_CANDIDATE_SURVEY_QUEUE_SCHEMA,
            "project_id": str(job.project_id),
            "pipeline_job_id": str(job.job_id),
            "analysis_job_id": analysis_job_id,
            "source_sha256": source_sha256,
            "dense_index_sha256": dense_index_sha256,
            "dense_candidate_discovery_revision": discovery_revision,
            "zero_detection_stage": zero_stage_name,
            "question_count": 0,
            "question_candidate_ids": [],
            "human_question_count": 0,
            "human_question_candidate_ids": [],
            "mandatory_human_candidate_ids": [],
            "review_event_set_state": "empty_promoted_survey",
            "review_event_sets": [],
            "review_event_count": 0,
            "review_event_ids": [],
            "review_event_bindings": [],
            "question_catalog_state": "empty_promoted_survey",
            "question_catalog_registrations": [],
            "user_discovered_candidate_overlays": [],
            "user_discovered_candidate_registrations": [],
            "review_only": True,
            "automatic_incident_allowed": False,
            "automatic_report_truth_allowed": False,
            "incident_count": 0,
            "report_incident_count": 0,
        }
        queue_revision = hashlib.sha256(
            _canonical_json(queue_base)
        ).hexdigest()
        question_stage = job.stages[question_stage_index]
        previous_checkpoint = question_stage["checkpoint"]
        previous_status = question_stage["status"]
        previous_outcome = question_stage["outcome"]
        previous_completed_at = question_stage["completed_at"]
        previous_metrics = question_stage["metrics"]
        question_stage["checkpoint"] = {
            **queue_base,
            "question_queue_revision": queue_revision,
        }
        question_stage["status"] = "success"
        question_stage["outcome"] = "zero_detections"
        question_stage["completed_at"] = _utc_now().isoformat()
        question_stage["metrics"] = {
            "question_count": 0,
            "human_question_count": 0,
            "review_event_count": 0,
            "annotation_count": 0,
            "incident_count": 0,
        }
        try:
            resolved = self._promoted_survey_qa_context_locked(job)
        except PipelineJobConflict:
            question_stage["checkpoint"] = previous_checkpoint
            question_stage["status"] = previous_status
            question_stage["outcome"] = previous_outcome
            question_stage["completed_at"] = previous_completed_at
            question_stage["metrics"] = previous_metrics
            job.status = "failed"
            job.outcome_code = "failed"
            job.error_code = "promoted_survey_qa_provenance_invalid"
            self._persist_job_locked(job)
            return True
        if resolved is None:
            question_stage["checkpoint"] = previous_checkpoint
            question_stage["status"] = previous_status
            question_stage["outcome"] = previous_outcome
            question_stage["completed_at"] = previous_completed_at
            question_stage["metrics"] = previous_metrics
            return False
        for later in job.stages[zero_stage_index + 1 : question_stage_index]:
            later["status"] = "skipped"
            later["outcome"] = "zero_detections"
            later["completed_at"] = _utc_now().isoformat()
        job.current_stage = "question_queue"
        job.status = "awaiting_review"
        job.outcome_code = "zero_detections"
        job.error_code = None
        job.provenance["zero_candidate_survey_qa"] = {
            "state": "awaiting_final_qa",
            "question_queue_revision": queue_revision,
            "analysis_job_id": str(analysis_job_id),
            "dense_index_sha256": dense_index_sha256,
            "model_version_id": promoted.get("model_version_id"),
            "automatic_completion_allowed": False,
        }
        job.updated_at = _utc_now()
        self._persist_job_locked(job)
        return True

    def _promoted_survey_qa_context_locked(
        self,
        job: PipelineJobRecord,
    ) -> Optional[dict[str, Any]]:
        """Resolve the server-owned final-QA binding for a promoted survey.

        The renderer may choose one of the two final actions, but it cannot
        declare that a promoted model ran or supply the model/index lineage.
        Those facts come only from the durable dense-discovery and question
        queue checkpoints owned by this PipelineJob.
        """

        rescan_stage = job.stages[
            PIPELINE_STAGES.index("candidate_rescan_5_10fps")
        ]
        question_stage = job.stages[
            PIPELINE_STAGES.index("question_queue")
        ]
        rescan = rescan_stage.get("checkpoint")
        question = question_stage.get("checkpoint")
        if not isinstance(rescan, Mapping):
            return None
        discovery = rescan.get("dense_candidate_discovery")
        if not isinstance(discovery, Mapping):
            return None
        promoted = self._ready_promoted_survey_provider(rescan)
        if promoted is None:
            return None
        if not isinstance(question, Mapping):
            raise PipelineJobConflict(
                "Promoted-model survey QA provenance is invalid"
            )
        analysis_job_id = rescan.get("analysis_job_id")
        source_sha256 = rescan.get("source_sha256")
        dense_index_sha256 = rescan.get("dense_index_sha256")
        discovery_revision = rescan.get(
            "dense_candidate_discovery_revision"
        )
        question_queue_revision = question.get(
            "question_queue_revision"
        )
        model_version_id = promoted.get("model_version_id")
        dataset_version_id = promoted.get("dataset_version_id")
        primary_artifact_sha256 = promoted.get(
            "primary_artifact_sha256"
        )
        artifact_manifest_sha256 = promoted.get(
            "artifact_manifest_sha256"
        )
        dataset_hash = promoted.get("dataset_hash")
        fixed_validation_sha256 = promoted.get(
            "fixed_validation_sha256"
        )
        source_analysis_lineage_sha256 = promoted.get(
            "source_analysis_lineage_sha256"
        )
        dataset_provenance_graph_sha256 = promoted.get(
            "dataset_provenance_graph_sha256"
        )
        sampling_fps = discovery.get("sampling_fps")
        scanned_frame_count = discovery.get(
            "scanned_frame_count"
        )
        registrations = question.get(
            "user_discovered_candidate_registrations", []
        )
        question_candidate_ids = question.get(
            "question_candidate_ids"
        )
        question_count = question.get("question_count")
        try:
            UUID(str(analysis_job_id))
        except (TypeError, ValueError):
            raise PipelineJobConflict(
                "Promoted-model survey QA provenance is invalid"
            ) from None
        if (
            not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(dense_index_sha256, str)
            or _SHA256.fullmatch(dense_index_sha256) is None
            or discovery.get("dense_index_sha256")
            != dense_index_sha256
            or not isinstance(discovery_revision, str)
            or _SHA256.fullmatch(discovery_revision) is None
            or discovery.get("discovery_revision")
            != discovery_revision
            or discovery.get("source_sha256") != source_sha256
            or question.get("analysis_job_id")
            != str(analysis_job_id)
            or question.get("source_sha256") != source_sha256
            or not isinstance(question_queue_revision, str)
            or _IDENTIFIER.fullmatch(question_queue_revision) is None
            or promoted.get("survey_analysis_job_id")
            != str(analysis_job_id)
            or not isinstance(model_version_id, str)
            or _IDENTIFIER.fullmatch(model_version_id) is None
            or not isinstance(dataset_version_id, str)
            or _IDENTIFIER.fullmatch(dataset_version_id) is None
            or not isinstance(primary_artifact_sha256, str)
            or _SHA256.fullmatch(primary_artifact_sha256) is None
            or not isinstance(artifact_manifest_sha256, str)
            or _SHA256.fullmatch(artifact_manifest_sha256) is None
            or not isinstance(dataset_hash, str)
            or _SHA256.fullmatch(dataset_hash) is None
            or not isinstance(fixed_validation_sha256, str)
            or _SHA256.fullmatch(fixed_validation_sha256) is None
            or not isinstance(
                source_analysis_lineage_sha256, str
            )
            or _SHA256.fullmatch(
                source_analysis_lineage_sha256
            )
            is None
            or not isinstance(
                dataset_provenance_graph_sha256, str
            )
            or _SHA256.fullmatch(
                dataset_provenance_graph_sha256
            )
            is None
            or sampling_fps != 2
            or isinstance(scanned_frame_count, bool)
            or not isinstance(scanned_frame_count, int)
            or scanned_frame_count < 1
            or not isinstance(registrations, list)
            or not isinstance(question_candidate_ids, list)
            or isinstance(question_count, bool)
            or not isinstance(question_count, int)
            or question_count < 0
            or question_count != len(question_candidate_ids)
            or len(question_candidate_ids)
            != len(set(question_candidate_ids))
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in question_candidate_ids
            )
            or any(
                not isinstance(item, Mapping)
                or item.get("schema_version")
                != USER_DISCOVERED_QUEUE_REGISTRATION_SCHEMA
                or item.get("review_only") is not True
                for item in registrations
            )
        ):
            raise PipelineJobConflict(
                "Promoted-model survey QA provenance is invalid"
            )
        promoted_model_inference_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "pipeline_job_id": str(job.job_id),
                    "analysis_job_id": str(analysis_job_id),
                    "dense_candidate_discovery_revision": (
                        discovery_revision
                    ),
                    "provider_state": dict(promoted),
                    "active_model_artifact_manifest_sha256": (
                        artifact_manifest_sha256
                    ),
                }
            )
        ).hexdigest()
        return {
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "dense_index_sha256": dense_index_sha256,
            "dense_candidate_discovery_revision": (
                discovery_revision
            ),
            "question_queue_revision": question_queue_revision,
            "model_version_id": model_version_id,
            "dataset_version_id": dataset_version_id,
            "primary_artifact_sha256": primary_artifact_sha256,
            "artifact_manifest_sha256": (
                artifact_manifest_sha256
            ),
            "dataset_hash": dataset_hash,
            "fixed_validation_sha256": fixed_validation_sha256,
            "source_analysis_lineage_sha256": (
                source_analysis_lineage_sha256
            ),
            "dataset_provenance_graph_sha256": (
                dataset_provenance_graph_sha256
            ),
            "promoted_model_inference_sha256": (
                promoted_model_inference_sha256
            ),
            "sampling_fps": sampling_fps,
            "scanned_frame_count": scanned_frame_count,
            "question_count": question_count,
            "question_candidate_ids": list(
                question_candidate_ids
            ),
            "zero_candidate_survey": (
                question.get("schema_version")
                == ZERO_CANDIDATE_SURVEY_QUEUE_SCHEMA
                and question_count == 0
            ),
            "user_discovered_registrations_sha256": hashlib.sha256(
                _canonical_json(registrations)
            ).hexdigest(),
            "review_event_set_collection_revision": question.get(
                "review_event_set_collection_revision"
            ),
            "question_catalog_collection_revision": question.get(
                "question_catalog_collection_revision"
            ),
            "user_discovered_candidate_count": len(registrations),
        }

    def complete_review(
        self,
        project_id: UUID,
        job_id: UUID,
        *,
        review_completion_id: str,
        final_qa_choice: Optional[str] = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(review_completion_id, str)
            or _IDENTIFIER.fullmatch(review_completion_id) is None
        ):
            raise ValueError("review_completion_id is invalid")
        if final_qa_choice not in {
            None,
            "no_miss_complete",
            "miss_candidates_reviewed",
        }:
            raise ValueError("final_qa_choice is invalid")
        with self._lock:
            job = self._job(project_id, job_id)
            if job.status == "completed":
                if job.review_completion_id == review_completion_id:
                    survey_qa = job.provenance.get(
                        "survey_qa_completion"
                    )
                    if (
                        isinstance(survey_qa, Mapping)
                        and survey_qa.get("final_qa_choice")
                        != final_qa_choice
                    ):
                        raise PipelineJobConflict(
                            "A different final survey QA choice was already recorded"
                        )
                    return self._serialize(job)
                raise PipelineJobConflict("pipeline review is already completed")
            if (
                job.review_completion_id == review_completion_id
                and job.current_stage == "derived_outputs"
                and job.status in {"queued", "running"}
            ):
                survey_qa = job.provenance.get(
                    "survey_qa_completion"
                )
                if (
                    isinstance(survey_qa, Mapping)
                    and survey_qa.get("final_qa_choice")
                    != final_qa_choice
                ):
                    raise PipelineJobConflict(
                        "A different final survey QA choice was already recorded"
                    )
                return self._serialize(job)
            if job.status != "awaiting_review":
                raise PipelineJobConflict(
                    "The pipeline is not waiting for review completion"
                )
            if job.review_completion_id not in {None, review_completion_id}:
                raise PipelineJobConflict(
                    "A different review completion was already recorded"
                )
            context = self._runtime_context(job)
            promoted_survey = self._promoted_survey_qa_context_locked(
                job
            )
        zero_candidate_survey = bool(
            promoted_survey
            and promoted_survey.get("zero_candidate_survey") is True
        )
        if zero_candidate_survey:
            empty_ledger_sha256 = hashlib.sha256(b"").hexdigest()
            expected_completion_id = (
                f"zero-detections:{empty_ledger_sha256}"
            )
            if (
                final_qa_choice != "no_miss_complete"
                or review_completion_id != expected_completion_id
            ):
                raise PipelineJobConflict(
                    "Zero-candidate promoted survey requires the exact no-miss QA decision"
                )
            review_proof = {
                "review_completion_id": review_completion_id,
                "all_required_questions_answered": True,
                "answered_question_count": 0,
                "server_answer_ledger_revision": (
                    empty_ledger_sha256
                ),
                "analysis_job_id": promoted_survey[
                    "analysis_job_id"
                ],
                "question_queue_revision": promoted_survey[
                    "question_queue_revision"
                ],
                "completion_source": (
                    "local_user_zero_candidate_survey_qa"
                ),
            }
        else:
            try:
                review_proof = dict(
                    self._runtime.validate_review_completion(
                        context,
                        review_completion_id,
                    )
                )
                _validate_safe_json(
                    review_proof, "review completion proof"
                )
            except (PipelineJobError, ValueError) as error:
                raise PipelineJobConflict(
                    "Server-owned review completion could not be verified"
                ) from error
            except Exception as error:
                raise PipelineJobUnavailable(
                    "Review completion verification is unavailable"
                ) from error
            if (
                review_proof.get("review_completion_id")
                != review_completion_id
                or review_proof.get("all_required_questions_answered")
                is not True
                or isinstance(
                    review_proof.get("answered_question_count"), bool
                )
                or not isinstance(
                    review_proof.get("answered_question_count"), int
                )
                or review_proof["answered_question_count"] < 1
            ):
                raise PipelineJobConflict(
                    "Required questions are not fully reviewed"
                )
        with self._lock:
            job = self._job(project_id, job_id)
            if job.status != "awaiting_review":
                if (
                    job.review_completion_id == review_completion_id
                    and job.current_stage == "derived_outputs"
                    and job.status in {"queued", "running", "completed"}
                ):
                    return self._serialize(job)
                raise PipelineJobConflict(
                    "The pipeline review state changed during verification"
                )
            current_promoted_survey = (
                self._promoted_survey_qa_context_locked(job)
            )
            if current_promoted_survey != promoted_survey:
                raise PipelineJobConflict(
                    "The promoted-model survey QA state changed during verification"
                )
            survey_qa_completion = None
            if promoted_survey is not None:
                if final_qa_choice is None:
                    raise PipelineJobConflict(
                        "A final promoted-model survey QA choice is required"
                    )
                registration_count = promoted_survey[
                    "user_discovered_candidate_count"
                ]
                if (
                    final_qa_choice == "no_miss_complete"
                    and registration_count != 0
                ):
                    raise PipelineJobConflict(
                        "User-discovered missed candidates require the reviewed choice"
                    )
                if (
                    final_qa_choice == "miss_candidates_reviewed"
                    and registration_count < 1
                ):
                    raise PipelineJobConflict(
                        "No user-discovered missed candidate is registered"
                    )
                survey_qa_base = {
                    "schema_version": SURVEY_QA_COMPLETION_SCHEMA,
                    "project_id": str(project_id),
                    "pipeline_job_id": str(job_id),
                    "analysis_job_id": promoted_survey[
                        "analysis_job_id"
                    ],
                    "source_sha256": promoted_survey[
                        "source_sha256"
                    ],
                    "dense_index_sha256": promoted_survey[
                        "dense_index_sha256"
                    ],
                    "dense_candidate_discovery_revision": (
                        promoted_survey[
                            "dense_candidate_discovery_revision"
                        ]
                    ),
                    "question_queue_revision": promoted_survey[
                        "question_queue_revision"
                    ],
                    "model_version_id": promoted_survey[
                        "model_version_id"
                    ],
                    "dataset_version_id": promoted_survey[
                        "dataset_version_id"
                    ],
                    "primary_artifact_sha256": promoted_survey[
                        "primary_artifact_sha256"
                    ],
                    "artifact_manifest_sha256": promoted_survey[
                        "artifact_manifest_sha256"
                    ],
                    "dataset_hash": promoted_survey["dataset_hash"],
                    "fixed_validation_sha256": promoted_survey[
                        "fixed_validation_sha256"
                    ],
                    "source_analysis_lineage_sha256": (
                        promoted_survey[
                            "source_analysis_lineage_sha256"
                        ]
                    ),
                    "dataset_provenance_graph_sha256": (
                        promoted_survey[
                            "dataset_provenance_graph_sha256"
                        ]
                    ),
                    "promoted_model_inference_sha256": (
                        promoted_survey[
                            "promoted_model_inference_sha256"
                        ]
                    ),
                    "sampling_fps": promoted_survey["sampling_fps"],
                    "scanned_frame_count": promoted_survey[
                        "scanned_frame_count"
                    ],
                    "final_qa_choice": final_qa_choice,
                    "review_completion_id": (
                        review_completion_id
                    ),
                    "server_answer_ledger_revision": (
                        review_proof[
                            "server_answer_ledger_revision"
                        ]
                    ),
                    "review_event_set_collection_revision": (
                        promoted_survey[
                            "review_event_set_collection_revision"
                        ]
                    ),
                    "question_catalog_collection_revision": (
                        promoted_survey[
                            "question_catalog_collection_revision"
                        ]
                    ),
                    "user_discovered_registrations_sha256": (
                        promoted_survey[
                            "user_discovered_registrations_sha256"
                        ]
                    ),
                    "user_discovered_candidate_count": (
                        registration_count
                    ),
                    "actor": "local_user",
                    "recorded_at": _utc_now().isoformat(),
                }
                survey_qa_completion = {
                    **survey_qa_base,
                    "survey_qa_revision": hashlib.sha256(
                        _canonical_json(survey_qa_base)
                    ).hexdigest(),
                }
                _validate_safe_json(
                    survey_qa_completion,
                    "promoted-model survey QA completion",
                )
            try:
                self._job_storage_budget_snapshot(
                    job, "derived_outputs"
                )
            except PipelineStageFailure as error:
                raise PipelineJobConflict(
                    "Derived-output storage cannot be reserved"
                ) from error
            job.review_completion_id = review_completion_id
            if survey_qa_completion is not None:
                review_proof["survey_qa_completion"] = (
                    survey_qa_completion
                )
                job.provenance["survey_qa_completion"] = (
                    survey_qa_completion
                )
            job.provenance["review_completion"] = review_proof
            job.status = "queued"
            job.current_stage = "derived_outputs"
            job.updated_at = _utc_now()
            self._persist_job_locked(job)
            self._ensure_worker_locked()
            try:
                self._queue.put_nowait(job.job_id)
            except queue.Full as error:
                job.status = "awaiting_review"
                self._persist_job_locked(job)
                raise PipelineJobUnavailable("pipeline queue is full") from error
            return self._serialize(job)

    def recovery_summary(self) -> dict[str, int]:
        return dict(self._recovery_summary)

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._lock:
            for job in self._jobs.values():
                if job.status in _ACTIVE_JOB_STATUSES:
                    job.cancel_event.set()
            worker = self._worker
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if (
            worker is not None
            and worker.is_alive()
            and worker is not threading.current_thread()
        ):
            worker.join(timeout=5)

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        if self._stop_event.is_set():
            raise PipelineJobUnavailable("pipeline worker is shutting down")
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="trafficvisson-pipeline",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if job_id is None:
                self._queue.task_done()
                return
            try:
                try:
                    self._run_job(job_id)
                except PipelineRecordTooLarge:
                    # _persist_job_locked has already replaced the attempted
                    # oversized mutation with a compact, hash-bound failed
                    # record. Keep the worker alive for later jobs.
                    pass
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: UUID) -> None:
        while not self._stop_event.is_set():
            automatic_review_completion: Optional[
                tuple[str, dict[str, Any]]
            ] = None
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.status not in {"queued", "running"}:
                    return
                if job.cancel_event.is_set():
                    if self._stop_event.is_set():
                        return
                    self._mark_cancelled_locked(job)
                    return
                try:
                    self._validate_job_artifact_root(job)
                except ProjectArtifactRootError:
                    stage = job.stages[
                        PIPELINE_STAGES.index(job.current_stage)
                    ]
                    if stage["status"] in {"pending", "running"}:
                        stage["status"] = "failed"
                        stage["outcome"] = "failed"
                        stage["error_code"] = (
                            "artifact_root_revalidation_failed"
                        )
                        stage["completed_at"] = _utc_now().isoformat()
                    job.status = "failed"
                    job.outcome_code = "failed"
                    job.error_code = (
                        "artifact_root_revalidation_failed"
                    )
                    job.updated_at = _utc_now()
                    self._persist_job_locked(job)
                    return
                stage_index = PIPELINE_STAGES.index(job.current_stage)
                stage = job.stages[stage_index]
                if stage["status"] in {"success", "blocked", "skipped"}:
                    next_index = self._next_pending_index(job, stage_index + 1)
                    if next_index is None:
                        self._finish_success_locked(job)
                        return
                    stage_index = next_index
                    stage = job.stages[stage_index]
                    job.current_stage = stage["name"]
                try:
                    storage_before = (
                        self._job_storage_budget_snapshot(
                            job, str(stage["name"])
                        )
                    )
                except PipelineStageFailure as error:
                    self._block_storage_stage_locked(
                        job,
                        str(stage["name"]),
                        error_code=error.code,
                        observation=error.observation,
                    )
                    return
                job.status = "running"
                stage["status"] = "running"
                stage["attempts"] = int(stage["attempts"]) + 1
                stage["started_at"] = _utc_now().isoformat()
                stage["completed_at"] = None
                stage["error_code"] = None
                job.updated_at = _utc_now()
                self._persist_job_locked(job)
                context = self._runtime_context(job)
                stage_name = str(stage["name"])
            try:
                post_intake_preflight_duration: Optional[float] = None
                post_intake_context: Optional[dict[str, Any]] = None
                result = self._runtime.execute(
                    stage_name,
                    context,
                    lambda: self._stop_event.is_set()
                    or job.cancel_event.is_set(),
                )
                if not isinstance(result, PipelineStageResult):
                    raise PipelineStageFailure(
                        "invalid_stage_result",
                        "Pipeline runtime returned an invalid result",
                    )
                if (
                    stage_name == "intake_validation"
                    and result.outcome == "success"
                    and self._artifact_roots is not None
                    and isinstance(
                        job.provenance.get("artifact_root"),
                        Mapping,
                    )
                    and not isinstance(
                        context.get("storage_plan"), Mapping
                    )
                ):
                    duration = result.checkpoint.get(
                        "duration_seconds"
                    )
                    if (
                        isinstance(duration, bool)
                        or not isinstance(duration, (int, float))
                        or not math.isfinite(float(duration))
                        or duration <= 0
                    ):
                        raise PipelineStageFailure(
                            "storage_capacity_plan_invalid"
                        )
                    # Shared with folder ingestion so the primary one-video
                    # path cannot drift into an unbounded legacy execution.
                    from .folder_pipeline_batches import (
                        build_pipeline_storage_plan,
                    )

                    generated_plan = build_pipeline_storage_plan(
                        duration_seconds=float(duration),
                        candidate_rescan_fps=int(
                            job.profile_snapshot[
                                "candidate_rescan_fps"
                            ]
                        ),
                    )
                    with self._lock:
                        current = self._jobs.get(job_id)
                        if current is None:
                            return
                        if "storage_plan" not in current.provenance:
                            current.provenance["storage_plan"] = (
                                generated_plan
                            )
                            current.provenance[
                                "storage_plan_source"
                            ] = (
                                "server_intake_duration_preflight"
                            )
                            current.updated_at = _utc_now()
                            self._persist_job_locked(current)
                            job = current
                            post_intake_preflight_duration = float(
                                duration
                            )
                            post_intake_context = self._runtime_context(
                                current
                            )
                if (
                    stage_name == "intake_validation"
                    and result.outcome == "success"
                    and post_intake_context is None
                ):
                    with self._lock:
                        current = self._jobs.get(job_id)
                        if current is None:
                            return
                        if isinstance(
                            current.provenance.get("storage_plan"),
                            Mapping,
                        ):
                            duration = result.checkpoint.get(
                                "duration_seconds"
                            )
                            if (
                                isinstance(duration, bool)
                                or not isinstance(
                                    duration,
                                    (int, float),
                                )
                                or not math.isfinite(
                                    float(duration)
                                )
                                or duration <= 0
                            ):
                                raise PipelineStageFailure(
                                    "storage_capacity_plan_invalid"
                                )
                            job = current
                            post_intake_preflight_duration = float(
                                duration
                            )
                            post_intake_context = self._runtime_context(
                                current
                            )
                storage_after = self._job_storage_budget_snapshot(
                    job, stage_name
                )
                if (
                    post_intake_preflight_duration is not None
                    and post_intake_context is not None
                ):
                    preflight = getattr(
                        self._runtime,
                        "preflight_dense_after_intake",
                        None,
                    )
                    if callable(preflight):
                        staging_observation = preflight(
                            post_intake_context,
                            duration_seconds=(
                                post_intake_preflight_duration
                            ),
                        )
                        if not isinstance(
                            staging_observation, Mapping
                        ):
                            raise PipelineStageFailure(
                                "dense_staging_storage_blocked"
                            )
                        _validate_safe_json(
                            staging_observation,
                            "dense staging preflight",
                        )
                        with self._lock:
                            current = self._jobs.get(job_id)
                            if current is None:
                                return
                            current.provenance[
                                "dense_staging_preflight"
                            ] = dict(staging_observation)
                            current.updated_at = _utc_now()
                            self._persist_job_locked(current)
                if (
                    stage_name == "question_queue"
                    and result.outcome == "success"
                ):
                    automatic_completion_id = (
                        _automatic_structured_training_review_completion_id(
                            context,
                            result.checkpoint,
                        )
                    )
                    if automatic_completion_id is not None:
                        verification_context = {
                            **context,
                            "checkpoints": {
                                **dict(context.get("checkpoints", {})),
                                "question_queue": dict(
                                    result.checkpoint
                                ),
                            },
                            "review_completion_id": (
                                automatic_completion_id
                            ),
                        }
                        try:
                            review_proof = dict(
                                self._runtime.validate_review_completion(
                                    verification_context,
                                    automatic_completion_id,
                                )
                            )
                            _validate_safe_json(
                                review_proof,
                                "automatic structured review completion",
                            )
                        except Exception as error:
                            raise PipelineStageFailure(
                                "structured_llm_auto_review_verification_failed"
                            ) from error
                        expected_answer_count = result.checkpoint.get(
                            "question_count"
                        )
                        expected_ledger_revision = (
                            automatic_completion_id.split(
                                ":", 1
                            )[1]
                        )
                        if (
                            review_proof.get("review_completion_id")
                            != automatic_completion_id
                            or review_proof.get(
                                "all_required_questions_answered"
                            )
                            is not True
                            or isinstance(
                                review_proof.get(
                                    "answered_question_count"
                                ),
                                bool,
                            )
                            or review_proof.get(
                                "answered_question_count"
                            )
                            != expected_answer_count
                            or review_proof.get(
                                "server_answer_ledger_revision"
                            )
                            != expected_ledger_revision
                            or review_proof.get("analysis_job_id")
                            != result.checkpoint.get(
                                "analysis_job_id"
                            )
                            or review_proof.get(
                                "question_queue_revision"
                            )
                            != result.checkpoint.get(
                                "question_queue_revision"
                            )
                        ):
                            raise PipelineStageFailure(
                                "structured_llm_auto_review_verification_failed"
                            )
                        automatic_review_completion = (
                            automatic_completion_id,
                            review_proof,
                        )
            except PipelineStageFailure as error:
                if error.code == "storage_capacity_replan_required":
                    with self._lock:
                        current = self._jobs.get(job_id)
                        if (
                            current is not None
                            and self._accept_storage_replan_locked(
                                current,
                                stage_name,
                                error.observation,
                            )
                        ):
                            continue
                    return
                if error.code in {
                    "storage_blocked",
                    "dense_staging_storage_blocked",
                }:
                    with self._lock:
                        current = self._jobs.get(job_id)
                        if current is not None:
                            self._block_storage_stage_locked(
                                current,
                                stage_name,
                                error_code=error.code,
                                observation=error.observation,
                            )
                    return
                self._fail_stage(job_id, stage_name, error.code)
                return
            except Exception:
                self._fail_stage(job_id, stage_name, "internal_error")
                return
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                if self._stop_event.is_set():
                    # Leave the running checkpoint durable. Recovery will put
                    # the same stage back to pending on the next bridge start.
                    self._persist_job_locked(job)
                    return
                if job.cancel_event.is_set():
                    self._mark_cancelled_locked(job)
                    return
                stage = job.stages[PIPELINE_STAGES.index(stage_name)]
                stage["checkpoint"] = dict(result.checkpoint)
                artifact_root = job.provenance.get("artifact_root")
                if isinstance(artifact_root, Mapping):
                    stage["checkpoint"][
                        "artifact_root_binding"
                    ] = {
                        key: artifact_root[key]
                        for key in (
                            "output_location_id",
                            "registry_revision",
                            "root_identity_sha256",
                            "marker_sha256",
                        )
                    }
                stage["metrics"] = dict(result.metrics)
                if storage_before is not None:
                    stage["metrics"]["storage_before"] = (
                        storage_before
                    )
                if storage_after is not None:
                    stage["metrics"]["storage_after"] = storage_after
                stage["outcome"] = result.outcome
                stage["completed_at"] = _utc_now().isoformat()
                stage["error_code"] = result.error_code
                job.updated_at = _utc_now()
                if (
                    stage_name == "sha256"
                    and result.outcome == "success"
                    and not job.idempotency_key.startswith(
                        _EXPLICIT_NEW_SOURCE_PREFIX
                    )
                ):
                    canonical = self._source_replay_target_locked(
                        job,
                        result.checkpoint,
                    )
                    if canonical is not None:
                        try:
                            self._persist_source_alias_locked(
                                job,
                                canonical,
                                result.checkpoint,
                            )
                        except (OSError, PipelineJobUnavailable):
                            stage["status"] = "failed"
                            stage["outcome"] = "failed"
                            stage["error_code"] = (
                                "source_idempotency_store_unavailable"
                            )
                            job.status = "failed"
                            job.outcome_code = "failed"
                            job.error_code = stage["error_code"]
                            self._persist_job_locked(job)
                            return
                        self._release_job_capacity_reservation(job)
                        self._jobs.pop(job.job_id, None)
                        return
                if result.outcome == "success":
                    stage["status"] = "success"
                    if stage_name == "question_queue":
                        question_count = int(result.metrics.get("question_count", 0))
                        if question_count > 0:
                            if automatic_review_completion is None:
                                job.status = "awaiting_review"
                                job.outcome_code = "success"
                                job.error_code = None
                                self._persist_job_locked(job)
                                return
                            (
                                automatic_completion_id,
                                review_proof,
                            ) = automatic_review_completion
                            job.review_completion_id = (
                                automatic_completion_id
                            )
                            job.provenance["review_completion"] = {
                                **review_proof,
                                "completion_source": (
                                    "structured_llm_training_delegation"
                                ),
                                "training_only": True,
                                "automatic_training_started": False,
                                "automatic_incident_approval": False,
                                "automatic_report_truth": False,
                            }
                            job.provenance[
                                "review_completion_mode"
                            ] = "structured_llm_training_only"
                elif result.outcome == "blocked_gpx_required":
                    stage["status"] = "blocked"
                    job.provenance["route_status"] = "blocked_gpx_required"
                    job.provenance["route_point_source"] = None
                elif result.outcome == "zero_detections":
                    stage["status"] = "success"
                    job.outcome_code = "zero_detections"
                    if self._await_promoted_zero_candidate_survey_locked(
                        job,
                        zero_stage_name=stage_name,
                    ):
                        return
                    for later in job.stages[
                        PIPELINE_STAGES.index(stage_name) + 1 :
                    ]:
                        if later["name"] != "derived_outputs":
                            later["status"] = "skipped"
                            later["outcome"] = "zero_detections"
                            later["completed_at"] = _utc_now().isoformat()
                    job.current_stage = "derived_outputs"
                    self._persist_job_locked(job)
                    continue
                elif result.outcome == "unusable_visual_evidence":
                    stage["status"] = "success"
                    job.outcome_code = "unusable_visual_evidence"
                    job.provenance["visual_evidence_quality"] = {
                        "state": "unusable_visual_evidence",
                        "stage": stage_name,
                        "quality_revision": (
                            result.checkpoint.get(
                                "visual_evidence_quality_revision"
                            )
                        ),
                        "annotation_count": 0,
                        "incident_count": 0,
                        "automatic_training_membership": False,
                        "automatic_report_truth": False,
                    }
                    for later in job.stages[
                        PIPELINE_STAGES.index(stage_name) + 1 :
                    ]:
                        later["status"] = "skipped"
                        later["outcome"] = (
                            "unusable_visual_evidence"
                        )
                        later["completed_at"] = (
                            _utc_now().isoformat()
                        )
                    job.current_stage = "derived_outputs"
                    self._finish_success_locked(job)
                    return
                elif result.outcome == "model_unavailable":
                    stage["status"] = "blocked"
                    job.status = "blocked"
                    job.outcome_code = "model_unavailable"
                    job.error_code = result.error_code or "model_unavailable"
                    self._persist_job_locked(job)
                    return
                elif result.outcome == "partial":
                    stage["status"] = "blocked"
                    job.status = "blocked"
                    job.outcome_code = "partial"
                    job.error_code = (
                        result.error_code or "derived_outputs_partial"
                    )
                    self._persist_job_locked(job)
                    return
                elif result.outcome == "report_preview_blocked":
                    stage["status"] = "blocked"
                    job.status = "blocked"
                    job.outcome_code = "report_preview_blocked"
                    job.error_code = (
                        result.error_code
                        or "report_preview_blocked"
                    )
                    self._persist_job_locked(job)
                    return
                elif result.outcome == "cancelled":
                    stage["status"] = "cancelled"
                    self._mark_cancelled_locked(job)
                    return
                else:
                    stage["status"] = "failed"
                    job.status = "failed"
                    job.outcome_code = "failed"
                    job.error_code = result.error_code or "stage_failed"
                    self._persist_job_locked(job)
                    return
                next_index = self._next_pending_index(
                    job, PIPELINE_STAGES.index(stage_name) + 1
                )
                if next_index is None:
                    self._finish_success_locked(job)
                    return
                job.current_stage = PIPELINE_STAGES[next_index]
                self._persist_job_locked(job)

    def _source_replay_target_locked(
        self,
        job: PipelineJobRecord,
        checkpoint: Mapping[str, Any],
    ) -> Optional[PipelineJobRecord]:
        source_sha256 = checkpoint.get("source_sha256")
        profile_sha256 = job.provenance.get("profile_sha256")
        if (
            not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(profile_sha256, str)
            or _SHA256.fullmatch(profile_sha256) is None
        ):
            return None
        candidates: list[PipelineJobRecord] = []
        for existing in self._jobs.values():
            if (
                existing.job_id == job.job_id
                or existing.project_id != job.project_id
                or existing.status in {"failed", "cancelled"}
                or existing.provenance.get("profile_sha256")
                != profile_sha256
            ):
                continue
            sha_stage = existing.stages[
                PIPELINE_STAGES.index("sha256")
            ]
            if (
                sha_stage.get("status") == "success"
                and isinstance(sha_stage.get("checkpoint"), Mapping)
                and sha_stage["checkpoint"].get("source_sha256")
                == source_sha256
            ):
                candidates.append(existing)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (item.created_at, str(item.job_id)),
        )

    def _persist_source_alias_locked(
        self,
        alias_job: PipelineJobRecord,
        canonical: PipelineJobRecord,
        checkpoint: Mapping[str, Any],
    ) -> None:
        source_sha256 = str(checkpoint["source_sha256"])
        payload = {
            "schema_version": PIPELINE_JOB_ALIAS_SCHEMA,
            "alias_job_id": str(alias_job.job_id),
            "canonical_job_id": str(canonical.job_id),
            "project_id": str(alias_job.project_id),
            "snapshot_id": str(alias_job.snapshot_id),
            "file_name": alias_job.file_name,
            "idempotency_key": alias_job.idempotency_key,
            "input_fingerprint": alias_job.input_fingerprint,
            "source_sha256": source_sha256,
            "profile_sha256": str(
                alias_job.provenance["profile_sha256"]
            ),
            "created_at": alias_job.created_at.isoformat(),
        }
        raw = _canonical_json(payload)
        digest = hashlib.sha256(raw).hexdigest()
        path = self._job_path(
            alias_job.project_id, alias_job.job_id
        )
        _write_atomic(path, raw)
        _write_atomic(
            path.with_suffix(".json.sha256"),
            f"{digest}\n".encode("ascii"),
        )
        self._aliases[alias_job.job_id] = payload

    @staticmethod
    def _next_pending_index(
        job: PipelineJobRecord, start: int
    ) -> Optional[int]:
        for index in range(start, len(job.stages)):
            if job.stages[index]["status"] == "pending":
                return index
        return None

    def _recover_bound_storage_plan_locked(
        self,
        job: PipelineJobRecord,
    ) -> bool:
        """Migrate a pre-v2 bound direct job before it can write again."""

        intake = job.stages[PIPELINE_STAGES.index("intake_validation")]
        if intake["status"] != "success":
            if (
                job.current_stage == "intake_validation"
                and intake["status"] == "pending"
            ):
                return True
            self._fail_recovered_storage_plan_locked(job, intake)
            return False
        duration = intake.get("checkpoint", {}).get(
            "duration_seconds"
        )
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or duration <= 0
        ):
            self._fail_recovered_storage_plan_locked(job, intake)
            return False
        try:
            from .folder_pipeline_batches import (
                build_pipeline_storage_plan,
            )

            plan = build_pipeline_storage_plan(
                duration_seconds=float(duration),
                candidate_rescan_fps=int(
                    job.profile_snapshot["candidate_rescan_fps"]
                ),
            )
            normalized = _validated_storage_plan(
                plan,
                candidate_rescan_fps=int(
                    job.profile_snapshot["candidate_rescan_fps"]
                ),
            )
            assert normalized is not None
        except (AssertionError, TypeError, ValueError):
            self._fail_recovered_storage_plan_locked(job, intake)
            return False
        job.provenance["storage_plan"] = normalized
        job.provenance["storage_plan_source"] = (
            "recovered_intake_duration_preflight"
        )
        job.updated_at = _utc_now()
        self._persist_job_locked(job)
        return True

    def _fail_recovered_storage_plan_locked(
        self,
        job: PipelineJobRecord,
        intake: dict[str, Any],
    ) -> None:
        intake["status"] = "failed"
        intake["outcome"] = "failed"
        intake["error_code"] = "storage_capacity_plan_invalid"
        intake["completed_at"] = _utc_now().isoformat()
        job.status = "failed"
        job.current_stage = "intake_validation"
        job.outcome_code = "failed"
        job.error_code = "storage_capacity_plan_invalid"
        job.updated_at = _utc_now()
        self._persist_job_locked(job)

    def _block_storage_stage_locked(
        self,
        job: PipelineJobRecord,
        stage_name: str,
        *,
        error_code: str = "storage_blocked",
        observation: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if error_code not in {
            "storage_blocked",
            "storage_capacity_replan_required",
            "dense_staging_storage_blocked",
        }:
            error_code = "storage_blocked"
        stage = job.stages[PIPELINE_STAGES.index(stage_name)]
        stage["status"] = "blocked"
        stage["outcome"] = "partial"
        stage["error_code"] = error_code
        if isinstance(observation, Mapping):
            stage["checkpoint"][
                "storage_capacity_observation"
            ] = dict(observation)
        stage["completed_at"] = _utc_now().isoformat()
        job.status = "blocked"
        job.outcome_code = "partial"
        job.error_code = error_code
        job.updated_at = _utc_now()
        self._persist_job_locked(job)

    def _accept_storage_replan_locked(
        self,
        job: PipelineJobRecord,
        stage_name: str,
        observation: Optional[Mapping[str, Any]],
    ) -> bool:
        raw_plan = job.provenance.get("storage_plan")
        if (
            not isinstance(raw_plan, Mapping)
            or not isinstance(observation, Mapping)
            or observation.get("stage") != stage_name
        ):
            self._block_storage_stage_locked(
                job,
                stage_name,
                error_code="storage_capacity_replan_required",
                observation=observation,
            )
            return False
        try:
            plan = _validated_storage_plan(
                raw_plan,
                candidate_rescan_fps=int(
                    job.profile_snapshot["candidate_rescan_fps"]
                ),
            )
            assert plan is not None
            current = _validated_storage_revisions(
                job.provenance.get("storage_plan_revisions"),
                plan=plan,
            )
            same_stage = [
                item
                for item in current
                if item["stage"] == stage_name
            ]
            if same_stage:
                if (
                    same_stage[0].get("revision_sha256")
                    == observation.get("revision_sha256")
                ):
                    return True
                raise ValueError(
                    "immutable storage revision changed"
                )
            proposed = [*current, dict(observation)]
            _validated_storage_revisions(
                proposed,
                plan=plan,
            )
        except (AssertionError, ValueError):
            self._block_storage_stage_locked(
                job,
                stage_name,
                error_code="storage_capacity_replan_required",
                observation=observation,
            )
            return False
        job.provenance["storage_plan_revisions"] = proposed
        try:
            storage_observation = self._job_storage_budget_snapshot(
                job, stage_name
            )
        except PipelineStageFailure as error:
            job.provenance["storage_plan_revisions"] = current
            self._block_storage_stage_locked(
                job,
                stage_name,
                error_code="storage_blocked",
                observation=(
                    error.observation
                    if error.observation is not None
                    else observation
                ),
            )
            return False
        stage = job.stages[PIPELINE_STAGES.index(stage_name)]
        stage["status"] = "pending"
        stage["outcome"] = None
        stage["error_code"] = None
        stage["completed_at"] = None
        stage["checkpoint"][
            "storage_capacity_revision"
        ] = dict(observation)
        stage["metrics"][
            "storage_replan_accepted"
        ] = storage_observation
        job.status = "queued"
        job.error_code = None
        job.updated_at = _utc_now()
        self._persist_job_locked(job)
        return True

    def _fail_stage(self, job_id: UUID, stage_name: str, code: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if self._stop_event.is_set():
                self._persist_job_locked(job)
                return
            stage = job.stages[PIPELINE_STAGES.index(stage_name)]
            stage["status"] = "failed"
            stage["outcome"] = "failed"
            stage["error_code"] = code
            stage["completed_at"] = _utc_now().isoformat()
            job.status = "failed"
            job.outcome_code = "failed"
            job.error_code = code
            job.updated_at = _utc_now()
            self._release_job_capacity_reservation(job)
            self._persist_job_locked(job)

    def _mark_cancelled_locked(self, job: PipelineJobRecord) -> None:
        stage = job.stages[PIPELINE_STAGES.index(job.current_stage)]
        if stage["status"] in {"pending", "running"}:
            stage["status"] = "cancelled"
            stage["outcome"] = "cancelled"
            stage["completed_at"] = _utc_now().isoformat()
        job.status = "cancelled"
        job.outcome_code = "cancelled"
        job.error_code = None
        job.updated_at = _utc_now()
        self._release_job_capacity_reservation(job)
        self._persist_job_locked(job)

    def _finish_success_locked(self, job: PipelineJobRecord) -> None:
        job.status = "completed"
        if job.outcome_code not in {
            "zero_detections",
            "unusable_visual_evidence",
        }:
            job.outcome_code = "success"
        job.error_code = None
        job.current_stage = "derived_outputs"
        job.updated_at = _utc_now()
        self._release_job_capacity_reservation(job)
        self._persist_job_locked(job)

    def _runtime_context(self, job: PipelineJobRecord) -> dict[str, Any]:
        checkpoints = {
            stage["name"]: dict(stage["checkpoint"])
            for stage in job.stages
            if stage["checkpoint"]
        }
        context = {
            "job_id": str(job.job_id),
            "project_id": str(job.project_id),
            "snapshot_id": str(job.snapshot_id),
            "file_name": job.file_name,
            "profile": dict(job.profile_snapshot),
            "capabilities": dict(job.capability_snapshot),
            "checkpoints": checkpoints,
            "review_completion_id": job.review_completion_id,
            "review_completion": dict(
                job.provenance.get("review_completion") or {}
            ),
            "input_fingerprint": job.input_fingerprint,
            "rights_confirmed": (
                isinstance(job.provenance.get("source"), Mapping)
                and job.provenance["source"].get("rights_confirmed")
                is True
            ),
            "profile_sha256": job.provenance["profile_sha256"],
            "stage_attempts": {
                stage["name"]: int(stage["attempts"])
                for stage in job.stages
            },
        }
        artifact_root = job.provenance.get("artifact_root")
        if isinstance(artifact_root, Mapping):
            context["artifact_root"] = dict(artifact_root)
        storage_plan = job.provenance.get("storage_plan")
        if isinstance(storage_plan, Mapping):
            context["storage_plan"] = dict(storage_plan)
        storage_revisions = job.provenance.get(
            "storage_plan_revisions"
        )
        if isinstance(storage_revisions, list):
            context["storage_plan_revisions"] = json.loads(
                json.dumps(storage_revisions)
            )
        input_restart_binding = job.provenance.get(
            "input_restart_binding"
        )
        if isinstance(input_restart_binding, Mapping):
            context["input_restart_binding"] = dict(
                input_restart_binding
            )
        return context

    def _capture_job_artifact_root(
        self,
        project_id: UUID,
        profile: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self._artifact_roots is None:
            return {}
        revision = int(profile["revision"])
        location_id = str(profile["output_location_id"])
        try:
            resolved = self._artifact_roots.capture_job_binding(
                project_id,
                profile_revision=revision,
                output_location_id=location_id,
            )
        except ProjectArtifactRootNotFound:
            # Profiles written before the registry contract have no private
            # binding. Only the server-owned default may be migrated lazily;
            # an old custom identifier must fail closed because its raw path
            # was never registered by the bridge.
            if location_id != DEFAULT_OUTPUT_LOCATION_ID:
                raise
            self._artifact_roots.capture_profile_binding(
                project_id,
                profile_revision=revision,
                output_location_id=location_id,
            )
            resolved = self._artifact_roots.capture_job_binding(
                project_id,
                profile_revision=revision,
                output_location_id=location_id,
            )
        return resolved.binding

    def _validate_job_artifact_root(
        self, job: PipelineJobRecord
    ) -> None:
        if self._artifact_roots is None:
            return
        binding = job.provenance.get("artifact_root")
        if binding is None:
            # Durable jobs created before this registry remain pinned to their
            # original bridge data root. They are never silently redirected
            # by a later profile change.
            return
        if not isinstance(binding, Mapping):
            raise ProjectArtifactRootConflict(
                "pipeline artifact-root binding is invalid"
            )
        self._artifact_roots.resolve_job_binding(
            job.project_id, binding
        )

    @staticmethod
    def _bounded_artifact_usage(roots: Sequence[Path]) -> int:
        total = 0
        entries = 0
        stack = [Path(root) for root in roots if Path(root).exists()]
        seen: set[Path] = set()
        while stack:
            current = stack.pop()
            try:
                resolved = current.resolve(strict=True)
            except OSError as error:
                raise PipelineStageFailure(
                    "storage_blocked"
                ) from error
            if resolved in seen:
                continue
            seen.add(resolved)
            if (
                current.is_symlink()
                or _is_reparse_point(current)
            ):
                raise PipelineStageFailure("storage_blocked")
            try:
                if current.is_file():
                    metadata = current.stat()
                    total += int(metadata.st_size)
                    entries += 1
                    if entries > 250_000 or total > 8 * 1024**4:
                        raise PipelineStageFailure(
                            "storage_blocked"
                        )
                    continue
                if not current.is_dir():
                    raise PipelineStageFailure("storage_blocked")
                children = list(current.iterdir())
            except OSError as error:
                raise PipelineStageFailure(
                    "storage_blocked"
                ) from error
            entries += len(children)
            if entries > 250_000:
                raise PipelineStageFailure("storage_blocked")
            stack.extend(children)
        return total

    @staticmethod
    def _job_capacity_reservation_id(
        job: PipelineJobRecord,
    ) -> str:
        return f"pipeline-job:{job.job_id}"

    def _release_job_capacity_reservation(
        self,
        job: PipelineJobRecord,
    ) -> None:
        binding = job.provenance.get("artifact_root")
        if (
            self._artifact_roots is None
            or not isinstance(binding, Mapping)
        ):
            return
        try:
            self._artifact_roots.release_capacity_reservation(
                job.project_id,
                binding,
                reservation_id=(
                    self._job_capacity_reservation_id(job)
                ),
            )
        except ProjectArtifactRootError:
            # Release is best-effort at a terminal boundary. A stale
            # process-local hold is conservative and disappears on restart.
            return

    def _adjust_job_capacity_reservation(
        self,
        job: PipelineJobRecord,
        *,
        remaining_planned_bytes: int,
        safety_reserve_bytes: int,
    ) -> Optional[dict[str, Any]]:
        binding = job.provenance.get("artifact_root")
        if (
            self._artifact_roots is None
            or not isinstance(binding, Mapping)
        ):
            return None
        try:
            observation = (
                self._artifact_roots.adjust_capacity_reservation(
                    job.project_id,
                    binding,
                    reservation_id=(
                        self._job_capacity_reservation_id(job)
                    ),
                    reserved_bytes=remaining_planned_bytes,
                    safety_reserve_bytes=safety_reserve_bytes,
                )
            )
        except ProjectArtifactRootError as error:
            raise PipelineStageFailure(
                "storage_blocked"
            ) from error
        job.provenance["capacity_reservation"] = observation
        return observation

    def _job_storage_budget_snapshot(
        self,
        job: PipelineJobRecord,
        stage_name: str,
    ) -> Optional[dict[str, Any]]:
        raw_plan = job.provenance.get("storage_plan")
        if raw_plan is None:
            return None
        plan = _validated_storage_plan(
            raw_plan,
            candidate_rescan_fps=int(
                job.profile_snapshot["candidate_rescan_fps"]
            ),
        )
        assert plan is not None
        stage_index = PIPELINE_STAGES.index(stage_name)
        artifact_binding = job.provenance.get("artifact_root")
        if (
            self._artifact_roots is not None
            and isinstance(artifact_binding, Mapping)
        ):
            try:
                resolved = self._artifact_roots.resolve_job_binding(
                    job.project_id, artifact_binding
                )
            except ProjectArtifactRootError as error:
                raise PipelineStageFailure(
                    "storage_blocked"
                ) from error
            volume_root = resolved.root
            roots = (
                resolved.root
                / "pipeline-jobs"
                / str(job.job_id),
            )
        else:
            volume_root = self._data_root
            legacy_pipeline_root = (
                self._data_root
                / "projects"
                / str(job.project_id)
                / "pipeline"
            )
            categories = {
                str(category)
                for stage in plan["stage_estimates"]
                for category in stage["categories"]
            }
            roots = tuple(
                legacy_pipeline_root
                / category
                / str(job.job_id)
                for category in sorted(categories)
            )
        actual_bytes = self._bounded_artifact_usage(roots)
        effective_stage_bytes = [
            int(stage["estimated_bytes"])
            for stage in plan["stage_estimates"]
        ]
        plan_sha256 = hashlib.sha256(
            _canonical_json(plan)
        ).hexdigest()
        try:
            revisions = _validated_storage_revisions(
                job.provenance.get("storage_plan_revisions"),
                plan=plan,
            )
        except ValueError as error:
            raise PipelineStageFailure(
                "storage_blocked"
            ) from error
        for revision in revisions:
            revision_stage_index = PIPELINE_STAGES.index(
                str(revision["stage"])
            )
            effective_stage_bytes[revision_stage_index] = int(
                revision["required_bytes"]
            )
        cumulative_ceiling = sum(
            effective_stage_bytes[: stage_index + 1]
        )
        total_planned = sum(effective_stage_bytes)
        remaining_planned = max(0, total_planned - actual_bytes)
        try:
            available = int(shutil.disk_usage(volume_root).free)
        except (OSError, TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "storage_blocked"
            ) from error
        # Accepted exact-stage growth is already represented in
        # ``effective_stage_bytes`` and therefore in ``remaining_planned``.
        # Keep the safety floor fixed instead of pre-reserving every possible
        # hard-cap delta or counting an accepted delta twice.
        safety = _MINIMUM_PIPELINE_OUTPUT_RESERVE_BYTES
        observation = {
            "schema_version": (
                "trafficvisson.pipeline-storage-observation.v1"
            ),
            "plan_sha256": plan_sha256,
            "stage": stage_name,
            "actual_artifact_bytes": actual_bytes,
            "cumulative_stage_ceiling_bytes": cumulative_ceiling,
            "remaining_planned_bytes": remaining_planned,
            "available_free_bytes": available,
            "safety_reserve_bytes": safety,
            "path_exposed": False,
        }
        if (
            actual_bytes > cumulative_ceiling
            or available < remaining_planned + safety
        ):
            raise PipelineStageFailure(
                "storage_blocked",
                observation=observation,
            )
        try:
            reservation_observation = (
                self._adjust_job_capacity_reservation(
                    job,
                    remaining_planned_bytes=remaining_planned,
                    safety_reserve_bytes=safety,
                )
            )
        except PipelineStageFailure as error:
            raise PipelineStageFailure(
                error.code,
                observation=observation,
            ) from error
        if reservation_observation is not None:
            observation["same_volume_reservation"] = (
                reservation_observation
            )
        return observation

    def _serialize(self, job: PipelineJobRecord) -> dict[str, Any]:
        completed_weight = 0
        for stage in job.stages:
            if stage["status"] in {"success", "blocked", "skipped"}:
                completed_weight += 1
        progress = round(100 * completed_weight / len(PIPELINE_STAGES))
        if job.status == "completed":
            progress = 100
        return {
            "job_id": job.job_id,
            "project_id": job.project_id,
            "snapshot_id": job.snapshot_id,
            "file_name": job.file_name,
            "idempotency_key": job.idempotency_key,
            "input_fingerprint": job.input_fingerprint,
            "profile_snapshot": dict(job.profile_snapshot),
            "capability_snapshot": dict(job.capability_snapshot),
            "status": job.status,
            "current_stage": job.current_stage,
            "progress_percent": progress,
            "outcome_code": job.outcome_code,
            "error_code": job.error_code,
            "retry_count": job.retry_count,
            "resume_count": job.resume_count,
            "review_completion_id": job.review_completion_id,
            "stages": json.loads(json.dumps(job.stages)),
            "provenance": json.loads(json.dumps(job.provenance)),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "recovered_after_restart": job.recovered_after_restart,
            "record_sha256": job.record_sha256,
        }

    def _persist_profile(self, profile: Mapping[str, Any]) -> None:
        project_id = UUID(str(profile["project_id"]))
        path = self._profile_path(project_id)
        payload = _canonical_json(profile)
        digest = hashlib.sha256(payload).hexdigest()
        _write_atomic(path, payload)
        _write_atomic(path.with_suffix(".json.sha256"), f"{digest}\n".encode("ascii"))

    def _persist_job_locked(self, job: PipelineJobRecord) -> None:
        payload = self._job_payload(job)
        raw = _canonical_json(payload)
        failure_reserve = min(
            MAX_PIPELINE_FAILURE_RESERVE_BYTES,
            max(1_024, MAX_RECORD_BYTES // 4),
        )
        normal_record_limit = max(
            1, MAX_RECORD_BYTES - failure_reserve
        )
        if len(raw) > normal_record_limit:
            self._persist_oversized_job_failure_locked(
                job,
                attempted_payload=payload,
                attempted_raw=raw,
            )
            raise PipelineRecordTooLarge(
                "pipeline record exceeds its bound"
            )
        digest = hashlib.sha256(raw).hexdigest()
        path = self._job_path(job.project_id, job.job_id)
        _write_atomic(path, raw)
        _write_atomic(path.with_suffix(".json.sha256"), f"{digest}\n".encode("ascii"))
        job.record_sha256 = digest

    def _persist_oversized_job_failure_locked(
        self,
        job: PipelineJobRecord,
        *,
        attempted_payload: Mapping[str, Any],
        attempted_raw: bytes,
    ) -> None:
        """Replace an unpersistable mutation with a durable compact failure."""

        path = self._job_path(job.project_id, job.job_id)
        base_payload: dict[str, Any]
        if path.is_file() and path.with_suffix(".json.sha256").is_file():
            try:
                base_payload, _digest = self._read_verified(
                    path, "pipeline job"
                )
            except (OSError, ValueError):
                base_payload = json.loads(
                    _canonical_json(attempted_payload).decode("utf-8")
                )
        else:
            base_payload = json.loads(
                _canonical_json(attempted_payload).decode("utf-8")
            )

        stages = base_payload.get("stages")
        if not isinstance(stages, list):
            raise PipelineJobUnavailable(
                "pipeline failure record is invalid"
            )
        failure_stage = next(
            (
                stage
                for stage in stages
                if isinstance(stage, dict)
                and stage.get("status") == "running"
            ),
            None,
        )
        if failure_stage is None:
            current_stage = base_payload.get("current_stage")
            failure_stage = next(
                (
                    stage
                    for stage in stages
                    if isinstance(stage, dict)
                    and stage.get("name") == current_stage
                ),
                None,
            )
        if failure_stage is None:
            raise PipelineJobUnavailable(
                "pipeline failure stage is unavailable"
            )

        attempted_stage = next(
            (
                stage
                for stage in attempted_payload.get("stages", ())
                if isinstance(stage, Mapping)
                and stage.get("name") == failure_stage.get("name")
            ),
            None,
        )
        attempted_checkpoint = (
            attempted_stage.get("checkpoint", {})
            if isinstance(attempted_stage, Mapping)
            else {}
        )
        now = _utc_now()
        failure_stage["status"] = "failed"
        failure_stage["outcome"] = "failed"
        failure_stage["error_code"] = "pipeline_record_too_large"
        failure_stage["completed_at"] = now.isoformat()
        failure_stage["checkpoint"] = {
            "state": "persistence_failed",
            "attempted_record_bytes": len(attempted_raw),
            "attempted_record_sha256": hashlib.sha256(
                attempted_raw
            ).hexdigest(),
            "discarded_stage_checkpoint_sha256": hashlib.sha256(
                _canonical_json(attempted_checkpoint)
            ).hexdigest(),
            "discarded_stage_result_used_for_truth": False,
            "automatic_annotation_allowed": False,
            "automatic_incident_creation_allowed": False,
            "training_membership_allowed": False,
            "automatic_report_truth_allowed": False,
        }
        failure_stage["metrics"] = {
            "attempted_record_bytes": len(attempted_raw),
            "maximum_record_bytes": MAX_RECORD_BYTES,
        }
        base_payload["status"] = "failed"
        base_payload["current_stage"] = failure_stage["name"]
        base_payload["outcome_code"] = "failed"
        base_payload["error_code"] = "pipeline_record_too_large"
        base_payload["updated_at"] = now.isoformat()

        failure_raw = _canonical_json(base_payload)
        if not 1 <= len(failure_raw) <= MAX_RECORD_BYTES:
            raise PipelineJobUnavailable(
                "pipeline failure record exceeds its reserve"
            )
        parsed = self._parse_job(base_payload, job.project_id)
        digest = hashlib.sha256(failure_raw).hexdigest()
        _write_atomic(path, failure_raw)
        _write_atomic(
            path.with_suffix(".json.sha256"),
            f"{digest}\n".encode("ascii"),
        )

        job.status = parsed.status
        job.current_stage = parsed.current_stage
        job.outcome_code = parsed.outcome_code
        job.error_code = parsed.error_code
        job.retry_count = parsed.retry_count
        job.resume_count = parsed.resume_count
        job.review_completion_id = parsed.review_completion_id
        job.stages = parsed.stages
        job.provenance = parsed.provenance
        job.created_at = parsed.created_at
        job.updated_at = parsed.updated_at
        job.recovered_after_restart = parsed.recovered_after_restart
        job.record_sha256 = digest
        self._release_job_capacity_reservation(job)

    @staticmethod
    def _job_payload(job: PipelineJobRecord) -> dict[str, Any]:
        return {
            "schema_version": PIPELINE_JOB_SCHEMA,
            "job_id": str(job.job_id),
            "project_id": str(job.project_id),
            "snapshot_id": str(job.snapshot_id),
            "file_name": job.file_name,
            "idempotency_key": job.idempotency_key,
            "input_fingerprint": job.input_fingerprint,
            "profile_snapshot": job.profile_snapshot,
            "capability_snapshot": job.capability_snapshot,
            "status": job.status,
            "current_stage": job.current_stage,
            "outcome_code": job.outcome_code,
            "error_code": job.error_code,
            "retry_count": job.retry_count,
            "resume_count": job.resume_count,
            "review_completion_id": job.review_completion_id,
            "stages": job.stages,
            "provenance": job.provenance,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "recovered_after_restart": job.recovered_after_restart,
        }

    def _reconcile_recovered_question_catalog(
        self, job: PipelineJobRecord
    ) -> None:
        """Upgrade a durable legacy review queue without reopening analysis.

        The catalog insert happens first and is idempotent. The verified job
        record is then replaced atomically with the returned catalog revision.
        A crash between those writes is safe: the next restart replays the same
        exact question set and receives the same revision.
        """

        if (
            job.status != "awaiting_review"
            or job.current_stage != "question_queue"
        ):
            return
        stage = job.stages[PIPELINE_STAGES.index("question_queue")]
        checkpoint = stage.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            return
        state = checkpoint.get("question_catalog_state")
        if state == "registered":
            return
        if state not in {None, "sidecar_only"}:
            self._recovery_summary[
                "question_catalog_migrations_failed"
            ] += 1
            return
        required = {
            "analysis_job_id": checkpoint.get("analysis_job_id"),
            "source_sha256": checkpoint.get("source_sha256"),
            "question_count": checkpoint.get("question_count"),
            "question_candidate_ids": checkpoint.get(
                "question_candidate_ids"
            ),
            "question_queue_revision": checkpoint.get(
                "question_queue_revision"
            ),
        }
        if (
            not isinstance(required["analysis_job_id"], str)
            or not isinstance(required["source_sha256"], str)
            or _SHA256.fullmatch(required["source_sha256"]) is None
            or isinstance(required["question_count"], bool)
            or not isinstance(required["question_count"], int)
            or required["question_count"] < 1
            or not isinstance(
                required["question_candidate_ids"], list
            )
            or len(required["question_candidate_ids"])
            != required["question_count"]
            or len(required["question_candidate_ids"])
            != len(set(required["question_candidate_ids"]))
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in required["question_candidate_ids"]
            )
            or not isinstance(
                required["question_queue_revision"], str
            )
            or _SHA256.fullmatch(
                required["question_queue_revision"]
            )
            is None
        ):
            self._recovery_summary[
                "question_catalog_migrations_failed"
            ] += 1
            return
        try:
            analysis_job_id = UUID(required["analysis_job_id"])
        except (TypeError, ValueError):
            self._recovery_summary[
                "question_catalog_migrations_failed"
            ] += 1
            return
        if str(analysis_job_id) != required["analysis_job_id"]:
            self._recovery_summary[
                "question_catalog_migrations_failed"
            ] += 1
            return
        migrator = getattr(
            self._runtime, "reconcile_question_catalog", None
        )
        if not callable(migrator):
            return
        try:
            result = migrator(self._runtime_context(job))
            if (
                not isinstance(result, PipelineStageResult)
                or result.outcome != "success"
            ):
                raise ValueError(
                    "question catalog migration did not succeed"
                )
            migrated = dict(result.checkpoint)
            if any(
                migrated.get(key) != value
                for key, value in required.items()
            ) or (
                migrated.get("question_catalog_state")
                != "registered"
                or not isinstance(
                    migrated.get("question_catalog_revision"), str
                )
                or _SHA256.fullmatch(
                    migrated["question_catalog_revision"]
                )
                is None
                or result.metrics.get("catalog_question_count")
                != required["question_count"]
            ):
                raise ValueError(
                    "question catalog migration changed queue provenance"
                )
        except Exception:
            self._recovery_summary[
                "question_catalog_migrations_failed"
            ] += 1
            return

        previous_checkpoint = dict(stage["checkpoint"])
        previous_metrics = dict(stage["metrics"])
        previous_updated_at = job.updated_at
        previous_recovered = job.recovered_after_restart
        previous_record_sha256 = job.record_sha256
        stage["checkpoint"] = migrated
        stage["metrics"] = {
            **previous_metrics,
            **dict(result.metrics),
        }
        job.updated_at = _utc_now()
        job.recovered_after_restart = True
        try:
            self._persist_job_locked(job)
        except (OSError, PipelineJobUnavailable):
            stage["checkpoint"] = previous_checkpoint
            stage["metrics"] = previous_metrics
            job.updated_at = previous_updated_at
            job.recovered_after_restart = previous_recovered
            job.record_sha256 = previous_record_sha256
            self._recovery_summary[
                "question_catalog_migrations_failed"
            ] += 1
            return
        self._recovery_summary["question_catalogs_registered"] += 1

    def _load_records(self) -> list[UUID]:
        recovered: list[UUID] = []
        entries = 0
        if not self._projects_root.exists():
            return recovered
        for project_dir in self._projects_root.iterdir():
            entries += 1
            if entries > MAX_RECOVERY_ENTRIES:
                break
            if (
                project_dir.is_symlink()
                or _is_reparse_point(project_dir)
                or not project_dir.is_dir()
            ):
                continue
            try:
                project_id = UUID(project_dir.name)
            except ValueError:
                continue
            pipeline_root = project_dir / "pipeline"
            if (
                pipeline_root.exists()
                and (
                    pipeline_root.is_symlink()
                    or _is_reparse_point(pipeline_root)
                    or not pipeline_root.is_dir()
                )
            ):
                self._recovery_summary["records_rejected"] += 1
                continue
            profile_path = pipeline_root / "profile.json"
            if profile_path.exists():
                try:
                    profile, _profile_digest = self._read_verified(
                        profile_path, "pipeline profile"
                    )
                    self._validate_loaded_profile(profile, project_id)
                    if (
                        profile.get("schema_version")
                        != PIPELINE_PROFILE_SCHEMA
                    ):
                        profile = _migrate_profile_to_v4(profile)
                        self._validate_loaded_profile(
                            profile, project_id
                        )
                        self._persist_profile(profile)
                    self._profiles[project_id] = profile
                    self._recovery_summary["profiles_loaded"] += 1
                except (OSError, ValueError):
                    self._recovery_summary["records_rejected"] += 1
            jobs_root = pipeline_root / "jobs"
            if not jobs_root.is_dir() or jobs_root.is_symlink():
                continue
            for path in jobs_root.glob("*.json"):
                entries += 1
                if entries > MAX_RECOVERY_ENTRIES:
                    break
                try:
                    payload, record_digest = self._read_verified(
                        path, "pipeline job"
                    )
                    if (
                        payload.get("schema_version")
                        == PIPELINE_JOB_ALIAS_SCHEMA
                    ):
                        alias = self._parse_alias(
                            payload, project_id
                        )
                        alias_job_id = UUID(alias["alias_job_id"])
                        if path.stem != str(alias_job_id):
                            raise ValueError(
                                "pipeline alias filename does not match "
                                "its identity"
                            )
                        self._aliases[alias_job_id] = alias
                        self._recovery_summary[
                            "aliases_loaded"
                        ] += 1
                        continue
                    job = self._parse_job(payload, project_id)
                    job.record_sha256 = record_digest
                    if path.stem != str(job.job_id):
                        raise ValueError(
                            "pipeline job filename does not match its identity"
                        )
                    if job.job_id in self._jobs:
                        raise ValueError(
                            "pipeline job identity is duplicated"
                        )
                    self._reconcile_recovered_question_catalog(job)
                    artifact_root_valid = True
                    try:
                        self._validate_job_artifact_root(job)
                    except ProjectArtifactRootError:
                        artifact_root_valid = False
                        stage = job.stages[
                            PIPELINE_STAGES.index(job.current_stage)
                        ]
                        if stage["status"] in {"pending", "running"}:
                            stage["status"] = "failed"
                            stage["outcome"] = "failed"
                            stage["error_code"] = (
                                "artifact_root_revalidation_failed"
                            )
                            stage["completed_at"] = (
                                _utc_now().isoformat()
                            )
                        job.status = "failed"
                        job.outcome_code = "failed"
                        job.error_code = (
                            "artifact_root_revalidation_failed"
                        )
                        job.recovered_after_restart = True
                        job.cancel_event = threading.Event()
                        self._persist_job_locked(job)
                    if artifact_root_valid and job.status == "cancelling":
                        stage = job.stages[
                            PIPELINE_STAGES.index(job.current_stage)
                        ]
                        if stage["status"] in {"pending", "running"}:
                            stage["status"] = "cancelled"
                            stage["outcome"] = "cancelled"
                            stage["completed_at"] = _utc_now().isoformat()
                        job.status = "cancelled"
                        job.outcome_code = "cancelled"
                        job.error_code = None
                        job.recovered_after_restart = True
                        job.cancel_event = threading.Event()
                        self._persist_job_locked(job)
                        self._recovery_summary[
                            "cancellations_finalized"
                        ] += 1
                    elif artifact_root_valid and job.status in {
                        "queued",
                        "running",
                    }:
                        stage = job.stages[
                            PIPELINE_STAGES.index(job.current_stage)
                        ]
                        if stage["status"] == "running":
                            stage["status"] = "pending"
                            stage["started_at"] = None
                            stage["completed_at"] = None
                        job.status = "queued"
                        job.recovered_after_restart = True
                        job.cancel_event = threading.Event()
                        self._persist_job_locked(job)
                        recovered.append(job.job_id)
                        self._recovery_summary["jobs_requeued"] += 1
                    self._jobs[job.job_id] = job
                    self._recovery_summary["jobs_loaded"] += 1
                except (OSError, ValueError, PipelineJobUnavailable):
                    self._recovery_summary["records_rejected"] += 1
        for alias_job_id, alias in tuple(self._aliases.items()):
            canonical = self._jobs.get(
                UUID(alias["canonical_job_id"])
            )
            if (
                canonical is None
                or canonical.project_id
                != UUID(alias["project_id"])
            ):
                self._aliases.pop(alias_job_id, None)
                self._recovery_summary["aliases_loaded"] -= 1
                self._recovery_summary["records_rejected"] += 1
        return recovered

    @staticmethod
    def _parse_alias(
        value: Mapping[str, Any],
        expected_project_id: UUID,
    ) -> dict[str, Any]:
        if set(value) != {
            "schema_version",
            "alias_job_id",
            "canonical_job_id",
            "project_id",
            "snapshot_id",
            "file_name",
            "idempotency_key",
            "input_fingerprint",
            "source_sha256",
            "profile_sha256",
            "created_at",
        }:
            raise ValueError("pipeline alias fields are invalid")
        _validate_safe_json(value, "pipeline alias")
        try:
            alias_job_id = UUID(str(value["alias_job_id"]))
            canonical_job_id = UUID(
                str(value["canonical_job_id"])
            )
            project_id = UUID(str(value["project_id"]))
            UUID(str(value["snapshot_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "pipeline alias identity is invalid"
            ) from error
        if (
            value.get("schema_version")
            != PIPELINE_JOB_ALIAS_SCHEMA
            or project_id != expected_project_id
            or alias_job_id == canonical_job_id
            or not isinstance(value.get("file_name"), str)
            or Path(value["file_name"]).name != value["file_name"]
            or not isinstance(value.get("idempotency_key"), str)
            or _IDEMPOTENCY_KEY.fullmatch(
                value["idempotency_key"]
            )
            is None
            or not isinstance(value.get("input_fingerprint"), str)
            or _SHA256.fullmatch(value["input_fingerprint"])
            is None
            or not isinstance(value.get("source_sha256"), str)
            or _SHA256.fullmatch(value["source_sha256"]) is None
            or not isinstance(value.get("profile_sha256"), str)
            or _SHA256.fullmatch(value["profile_sha256"]) is None
        ):
            raise ValueError("pipeline alias contract is invalid")
        _parse_datetime(value.get("created_at"))
        return dict(value)

    @staticmethod
    def _validate_loaded_profile(
        profile: Mapping[str, Any], project_id: UUID
    ) -> None:
        legacy_fields = {
            "schema_version",
            "project_id",
            "question_policy",
            "review_mode",
            "default_template_id",
            "default_template_sha256",
            "detector_pack_id",
            "ocr_pack_id",
            "sam2_pack_id",
            "report_provider",
            "output_location_id",
            "candidate_rescan_fps",
            "gps_policy",
            "bulk_frame_provider_policy",
            "revision",
            "updated_at",
        }
        current_fields = {
            *legacy_fields,
            "report_transmission_consent",
            "template_semantic_map_id",
            "template_semantic_map_sha256",
            "template_semantic_map_ready",
            "report_provider_ready_at_configuration",
        }
        v3_fields = {
            *current_fields,
            "report_route_name",
            "report_inspector_name",
            "report_evidence_mode",
            "report_inspection_date_policy",
        }
        v4_fields = {
            *v3_fields,
            "label_confirmation_authority",
        }
        schema_version = profile.get("schema_version")
        if (
            schema_version == LEGACY_PIPELINE_PROFILE_SCHEMA
            and set(profile) != legacy_fields
        ) or (
            schema_version == PIPELINE_PROFILE_V2_SCHEMA
            and set(profile) != current_fields
        ) or (
            schema_version == PIPELINE_PROFILE_V3_SCHEMA
            and set(profile) != v3_fields
        ) or (
            schema_version == PIPELINE_PROFILE_SCHEMA
            and set(profile) != v4_fields
        ) or schema_version not in {
            LEGACY_PIPELINE_PROFILE_SCHEMA,
            PIPELINE_PROFILE_V2_SCHEMA,
            PIPELINE_PROFILE_V3_SCHEMA,
            PIPELINE_PROFILE_SCHEMA,
        }:
            raise ValueError("pipeline profile fields are invalid")
        if (
            profile.get("project_id") != str(project_id)
            or profile.get("question_policy")
            not in {"learning_assist", "full_review", "quick_review"}
            or profile.get("review_mode") not in {"precision", "quick"}
            or profile.get("report_provider")
            not in {"codex", "claude", "local", "none"}
            or isinstance(profile.get("candidate_rescan_fps"), bool)
            or not isinstance(profile.get("candidate_rescan_fps"), int)
            or not 5 <= profile["candidate_rescan_fps"] <= 10
            or isinstance(profile.get("revision"), bool)
            or not isinstance(profile.get("revision"), int)
            or profile["revision"] < 1
            or not isinstance(profile.get("default_template_sha256"), str)
            or _SHA256.fullmatch(profile["default_template_sha256"]) is None
            or profile.get("bulk_frame_provider_policy") != "local_models_only"
            or profile.get("gps_policy") != "timed_track_required_for_route"
        ):
            raise ValueError("pipeline profile contract is invalid")
        if (
            schema_version == PIPELINE_PROFILE_SCHEMA
            and profile.get("label_confirmation_authority")
            == "structured_llm_training_assist"
            and (
                profile.get("report_provider")
                not in {"codex", "claude"}
                or profile.get("report_transmission_consent") is not True
            )
        ):
            raise ValueError(
                "pipeline structured LLM authority is not explicitly ready"
            )
        if schema_version in {
            PIPELINE_PROFILE_V2_SCHEMA,
            PIPELINE_PROFILE_V3_SCHEMA,
            PIPELINE_PROFILE_SCHEMA,
        } and (
            not isinstance(
                profile.get("report_transmission_consent"), bool
            )
            or not isinstance(
                profile.get("template_semantic_map_ready"), bool
            )
            or not isinstance(
                profile.get(
                    "report_provider_ready_at_configuration"
                ),
                bool,
            )
            or not isinstance(
                profile.get("template_semantic_map_id"), str
            )
            or _IDENTIFIER.fullmatch(
                profile["template_semantic_map_id"]
            )
            is None
            or not isinstance(
                profile.get("template_semantic_map_sha256"), str
            )
            or _SHA256.fullmatch(
                profile["template_semantic_map_sha256"]
            )
            is None
            or (
                profile["report_transmission_consent"]
                and (
                    profile.get("report_provider")
                    not in {"codex", "claude"}
                    or profile["template_semantic_map_ready"]
                    is not True
                    or profile[
                        "report_provider_ready_at_configuration"
                    ]
                    is not True
                )
            )
        ):
            raise ValueError(
                "pipeline report automation profile is invalid"
            )
        if schema_version in {
            PIPELINE_PROFILE_V3_SCHEMA,
            PIPELINE_PROFILE_SCHEMA,
        }:
            try:
                route_name = _normalize_report_context_text(
                    profile.get("report_route_name"),
                    "report_route_name",
                    maximum=300,
                )
                inspector_name = _normalize_report_context_text(
                    profile.get("report_inspector_name"),
                    "report_inspector_name",
                    maximum=120,
                )
            except ValueError as error:
                raise ValueError(
                    "pipeline report factual context is invalid"
                ) from error
            if (
                route_name != profile.get("report_route_name")
                or inspector_name
                != profile.get("report_inspector_name")
                or profile.get("report_evidence_mode")
                not in {
                    "internal_original",
                    "shared_deidentified",
                }
                or profile.get("report_inspection_date_policy")
                != REPORT_INSPECTION_DATE_POLICY
            ):
                raise ValueError(
                    "pipeline report factual context is invalid"
                )
        if (
            schema_version == PIPELINE_PROFILE_SCHEMA
            and profile.get("label_confirmation_authority")
            not in {
                "human_review",
                "structured_llm_training_assist",
            }
        ):
            raise ValueError(
                "pipeline label confirmation authority is invalid"
            )
        for field_name in (
            "default_template_id",
            "detector_pack_id",
            "ocr_pack_id",
            "sam2_pack_id",
            "output_location_id",
        ):
            if (
                not isinstance(profile.get(field_name), str)
                or _IDENTIFIER.fullmatch(profile[field_name]) is None
            ):
                raise ValueError("pipeline profile identity is invalid")
        _parse_datetime(profile.get("updated_at"))
        _validate_safe_json(profile, "pipeline profile")

    def _parse_job(
        self, value: Mapping[str, Any], expected_project_id: UUID
    ) -> PipelineJobRecord:
        if set(value) != {
            "schema_version",
            "job_id",
            "project_id",
            "snapshot_id",
            "file_name",
            "idempotency_key",
            "input_fingerprint",
            "profile_snapshot",
            "capability_snapshot",
            "status",
            "current_stage",
            "outcome_code",
            "error_code",
            "retry_count",
            "resume_count",
            "review_completion_id",
            "stages",
            "provenance",
            "created_at",
            "updated_at",
            "recovered_after_restart",
        }:
            raise ValueError("pipeline job fields are invalid")
        if (
            value.get("schema_version") != PIPELINE_JOB_SCHEMA
            or value.get("project_id") != str(expected_project_id)
            or value.get("current_stage") not in PIPELINE_STAGES
            or value.get("status")
            not in {
                "queued",
                "running",
                "cancelling",
                "awaiting_review",
                "blocked",
                "failed",
                "cancelled",
                "completed",
            }
            or not isinstance(value.get("stages"), list)
            or any(
                not isinstance(stage, Mapping)
                for stage in value["stages"]
            )
            or [stage.get("name") for stage in value["stages"]]
            != list(PIPELINE_STAGES)
        ):
            raise ValueError("pipeline job contract is invalid")
        # Stage checkpoints are individually capped at depth 8 when they are
        # created. A persisted job wraps the same checkpoint in
        # ``job -> stages -> stage -> checkpoint``, so recovery needs four
        # structural levels of headroom without relaxing the checkpoint
        # contract itself.
        _validate_safe_json(
            value,
            "pipeline job",
            maximum_depth=12,
        )
        if (
            not isinstance(value.get("file_name"), str)
            or not 1 <= len(value["file_name"]) <= 255
            or Path(value["file_name"]).name != value["file_name"]
            or not isinstance(value.get("idempotency_key"), str)
            or _IDEMPOTENCY_KEY.fullmatch(value["idempotency_key"]) is None
            or not isinstance(value.get("input_fingerprint"), str)
            or _SHA256.fullmatch(value["input_fingerprint"]) is None
            or (
                value.get("outcome_code") is not None
                and value["outcome_code"] not in _OUTCOME_CODES
            )
            or (
                value.get("error_code") is not None
                and (
                    not isinstance(value["error_code"], str)
                    or _SAFE_ERROR_CODE.fullmatch(value["error_code"]) is None
                )
            )
            or isinstance(value.get("retry_count"), bool)
            or not isinstance(value.get("retry_count"), int)
            or not 0 <= value["retry_count"] <= MAX_PIPELINE_RETRIES
            or isinstance(value.get("resume_count"), bool)
            or not isinstance(value.get("resume_count"), int)
            or not 0 <= value["resume_count"] <= MAX_PIPELINE_RETRIES
            or (
                value.get("review_completion_id") is not None
                and (
                    not isinstance(value["review_completion_id"], str)
                    or _IDENTIFIER.fullmatch(
                        value["review_completion_id"]
                    )
                    is None
                )
            )
            or not isinstance(value.get("recovered_after_restart"), bool)
        ):
            raise ValueError("pipeline job values are invalid")
        if (
            not isinstance(value.get("profile_snapshot"), Mapping)
            or not isinstance(value.get("capability_snapshot"), Mapping)
        ):
            raise ValueError("pipeline capability snapshot is invalid")
        self._validate_loaded_profile(
            value["profile_snapshot"], expected_project_id
        )
        if not isinstance(value.get("provenance"), Mapping):
            raise ValueError("pipeline provenance is invalid")
        profile_sha256 = hashlib.sha256(
            _canonical_json(value["profile_snapshot"])
        ).hexdigest()
        capability_sha256 = hashlib.sha256(
            _canonical_json(value["capability_snapshot"])
        ).hexdigest()
        if (
            value["provenance"].get("profile_sha256")
            != profile_sha256
            or value["provenance"].get("capability_sha256")
            != capability_sha256
        ):
            raise ValueError(
                "pipeline profile or capability provenance is invalid"
            )
        promoted_binding = value["capability_snapshot"].get(
            "promoted_project_model_binding"
        )
        if "promoted_project_model_binding" in value[
            "capability_snapshot"
        ]:
            if not isinstance(promoted_binding, Mapping):
                raise ValueError(
                    "pipeline promoted model binding is invalid"
                )
            try:
                validate_promoted_model_job_binding(
                    promoted_binding,
                    project_id=expected_project_id,
                )
            except DenseCandidateDiscoveryError as error:
                raise ValueError(
                    "pipeline promoted model binding is invalid"
                ) from error
        durable_storage_plan = value["provenance"].get(
            "storage_plan"
        )
        normalized_durable_storage_plan = _validated_storage_plan(
            durable_storage_plan,
            candidate_rescan_fps=int(
                value["profile_snapshot"]["candidate_rescan_fps"]
            ),
        )
        _validated_storage_revisions(
            value["provenance"].get("storage_plan_revisions"),
            plan=normalized_durable_storage_plan,
        )
        rerun_lineage = value["provenance"].get("rerun")
        if rerun_lineage is not None:
            _validate_rerun_lineage(
                rerun_lineage,
                project_id=expected_project_id,
                job_id=UUID(str(value["job_id"])),
                snapshot_id=UUID(str(value["snapshot_id"])),
                file_name=str(value["file_name"]),
                input_fingerprint=str(value["input_fingerprint"]),
                profile_snapshot=value["profile_snapshot"],
                capability_snapshot=value["capability_snapshot"],
            )
        elif "promoted_project_model_binding" in value[
            "capability_snapshot"
        ]:
            input_storage_plans: list[
                Optional[Mapping[str, Any]]
            ]
            if "input_storage_plan" in value["provenance"]:
                input_storage_plans = [
                    _validated_storage_plan(
                        value["provenance"].get(
                            "input_storage_plan"
                        ),
                        candidate_rescan_fps=int(
                            value["profile_snapshot"][
                                "candidate_rescan_fps"
                            ]
                        ),
                    )
                ]
            else:
                # Jobs written before the immutable start-input field may
                # have acquired a derived durable plan during intake.  That
                # later plan was never part of their start fingerprint.
                input_storage_plans = [
                    normalized_durable_storage_plan
                ]
                if normalized_durable_storage_plan is not None:
                    input_storage_plans.append(None)
            expected_fingerprints = {
                _pipeline_start_input_fingerprint(
                    project_id=expected_project_id,
                    snapshot_id=UUID(str(value["snapshot_id"])),
                    file_name=str(value["file_name"]),
                    profile_snapshot=value["profile_snapshot"],
                    promoted_model_binding=promoted_binding,
                    storage_plan=input_storage_plan,
                )
                for input_storage_plan in input_storage_plans
            }
            if (
                value["input_fingerprint"]
                not in expected_fingerprints
            ):
                raise ValueError(
                    "pipeline promoted model fingerprint is invalid"
                )
        for stage in value["stages"]:
            if (
                not isinstance(stage, Mapping)
                or set(stage)
                != {
                    "name",
                    "status",
                    "outcome",
                    "attempts",
                    "started_at",
                    "completed_at",
                    "error_code",
                    "checkpoint",
                    "metrics",
                }
                or stage.get("status") not in _STAGE_STATUSES
                or (
                    stage.get("outcome") is not None
                    and stage["outcome"] not in _OUTCOME_CODES
                )
                or isinstance(stage.get("attempts"), bool)
                or not isinstance(stage.get("attempts"), int)
                or not 0 <= stage["attempts"] <= 64
                or (
                    stage.get("error_code") is not None
                    and (
                        not isinstance(stage["error_code"], str)
                        or _SAFE_ERROR_CODE.fullmatch(
                            stage["error_code"]
                        )
                        is None
                    )
                )
                or not isinstance(stage.get("checkpoint"), Mapping)
                or not isinstance(stage.get("metrics"), Mapping)
            ):
                raise ValueError("pipeline stage status is invalid")
            if stage.get("started_at") is not None:
                _parse_datetime(stage["started_at"])
            if stage.get("completed_at") is not None:
                _parse_datetime(stage["completed_at"])
        job = PipelineJobRecord(
            job_id=UUID(str(value["job_id"])),
            project_id=expected_project_id,
            snapshot_id=UUID(str(value["snapshot_id"])),
            file_name=str(value["file_name"]),
            idempotency_key=str(value["idempotency_key"]),
            input_fingerprint=str(value["input_fingerprint"]),
            profile_snapshot=dict(value["profile_snapshot"]),
            capability_snapshot=dict(value["capability_snapshot"]),
            status=str(value["status"]),
            current_stage=str(value["current_stage"]),
            outcome_code=value.get("outcome_code"),
            error_code=value.get("error_code"),
            retry_count=int(value["retry_count"]),
            resume_count=int(value["resume_count"]),
            review_completion_id=value.get("review_completion_id"),
            stages=[dict(stage) for stage in value["stages"]],
            provenance=dict(value["provenance"]),
            created_at=_parse_datetime(value["created_at"]),
            updated_at=_parse_datetime(value["updated_at"]),
            recovered_after_restart=bool(value["recovered_after_restart"]),
            cancel_event=threading.Event(),
        )
        return job

    @staticmethod
    def _read_verified(
        path: Path, label: str
    ) -> tuple[dict[str, Any], str]:
        digest_path = path.with_suffix(".json.sha256")
        if (
            path.is_symlink()
            or digest_path.is_symlink()
            or _is_reparse_point(path)
            or _is_reparse_point(digest_path)
        ):
            raise ValueError(f"{label} link is invalid")
        raw = path.read_bytes()
        if not 1 <= len(raw) <= MAX_RECORD_BYTES:
            raise ValueError(f"{label} size is invalid")
        expected = digest_path.read_text(encoding="ascii").strip()
        if not _SHA256.fullmatch(expected):
            raise ValueError(f"{label} digest is invalid")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f"{label} digest mismatch")
        return _strict_json(raw, label), expected

    def _profile_path(self, project_id: UUID) -> Path:
        return self._project_pipeline_root(
            project_id, create=True
        ) / "profile.json"

    def _job_path(self, project_id: UUID, job_id: UUID) -> Path:
        jobs_root = (
            self._project_pipeline_root(project_id, create=True) / "jobs"
        )
        jobs_root.mkdir(exist_ok=True)
        if (
            jobs_root.is_symlink()
            or _is_reparse_point(jobs_root)
            or not jobs_root.is_dir()
            or jobs_root.resolve().parent
            != self._project_pipeline_root(project_id, create=False)
        ):
            raise PipelineJobUnavailable(
                "pipeline jobs directory is unsafe"
            )
        return jobs_root / f"{job_id}.json"

    def _project_pipeline_root(
        self, project_id: UUID, *, create: bool
    ) -> Path:
        project_root = self._projects_root / str(project_id)
        pipeline_root = project_root / "pipeline"
        if create:
            project_root.mkdir(exist_ok=True)
            pipeline_root.mkdir(exist_ok=True)
        try:
            resolved_project = project_root.resolve(strict=True)
            resolved_pipeline = pipeline_root.resolve(strict=True)
        except OSError as error:
            raise PipelineJobUnavailable(
                "pipeline project directory is unavailable"
            ) from error
        if (
            project_root.is_symlink()
            or pipeline_root.is_symlink()
            or _is_reparse_point(project_root)
            or _is_reparse_point(pipeline_root)
            or not resolved_project.is_dir()
            or not resolved_pipeline.is_dir()
            or resolved_project.parent != self._projects_root
            or resolved_pipeline.parent != resolved_project
        ):
            raise PipelineJobUnavailable(
                "pipeline project directory is unsafe"
            )
        return resolved_pipeline

    def _job(self, project_id: UUID, job_id: UUID) -> PipelineJobRecord:
        self._validate_project_id(project_id)
        if not isinstance(job_id, UUID):
            raise ValueError("job_id must be a UUID")
        job = self._jobs.get(job_id)
        if job is None:
            alias = self._aliases.get(job_id)
            if (
                alias is not None
                and alias.get("project_id") == str(project_id)
            ):
                job = self._jobs.get(
                    UUID(alias["canonical_job_id"])
                )
        if job is None or job.project_id != project_id:
            raise PipelineJobNotFound(
                "The pipeline job was not found in this project"
            )
        return job

    def _evict_locked(self) -> None:
        if len(self._jobs) < MAX_PIPELINE_JOBS:
            return
        aliased_targets = {
            UUID(alias["canonical_job_id"])
            for alias in self._aliases.values()
        }
        for job_id, job in tuple(self._jobs.items()):
            if (
                job.status in _TERMINAL_JOB_STATUSES
                and job_id not in aliased_targets
            ):
                self._jobs.pop(job_id, None)
                if len(self._jobs) < MAX_PIPELINE_JOBS:
                    return

    @staticmethod
    def _validate_project_id(project_id: UUID) -> None:
        if not isinstance(project_id, UUID):
            raise ValueError("project_id must be a UUID")


class BridgePipelineRuntime:
    """Honest default bridge adapter for currently implemented local stages.

    Intake, SHA-256, metadata preflight, 2 FPS dense indexing, and
    candidate-window-only 5--10 FPS extraction are real.  DINO/OCR/SAM2 stay
    capability-gated by their dedicated local runtimes.  A capture-start
    coordinate never becomes a route.
    """

    def __init__(
        self,
        media_intake: MediaIntakeService,
        *,
        data_root: Path,
        ffmpeg_path: Optional[str] = None,
        ffprobe_path: Optional[str] = None,
        dense_staging_root: Optional[Path] = None,
        dense_staging_source: Optional[str] = None,
        vision_status: Optional[Callable[[], Mapping[str, Any]]] = None,
        ocr_inference_service: Optional[OcrInferenceService] = None,
        ocr_status: Optional[Callable[[], Mapping[str, Any]]] = None,
        analysis_jobs: Optional[AnalysisJobService] = None,
        command_proposal_generator: Optional[
            CommandProposalGenerator
        ] = None,
        experimental_proposals: Optional[
            ExperimentalProposalStore
        ] = None,
        candidate_labels: Optional[CandidateLabelStore] = None,
        question_catalog_projector: Optional[
            Callable[
                [UUID, UUID, str, Sequence[Mapping[str, Any]]],
                Mapping[str, Any],
            ]
        ] = None,
        weak_case_catalog: Optional[WeakCaseCatalog] = None,
        weak_case_approvals: Optional[WeakCaseApprovalStore] = None,
        weak_case_visual_index: Optional[
            WeakCaseVisualIndexService
        ] = None,
        visual_evidence_semantic_observer: Optional[
            LocalRoadSceneObserver
        ] = None,
        sam2_status: Optional[
            Callable[[], Mapping[str, Any]]
        ] = None,
        sam2_adapter: Optional[PipelineSam2Adapter] = None,
        derived_outputs: Optional[DerivedOutputsService] = None,
        report_semantic_maps: Optional[Any] = None,
        report_provider_status: Optional[
            Callable[[str], Any]
        ] = None,
        structured_vision_judgment: Optional[
            StructuredVisionJudgmentProvider
        ] = None,
        dense_candidate_discovery: Optional[
            DenseCandidateDiscoveryEngine
        ] = None,
        candidate_evidence_overlays: Optional[
            CandidateEvidenceOverlayStore
        ] = None,
        candidate_evidence_registry: Optional[Any] = None,
        dense_index_registration: Optional[
            Callable[
                [
                    UUID,
                    str,
                    str,
                    Path,
                    UUID,
                    Optional[Mapping[str, Any]],
                ],
                None,
            ]
        ] = None,
        review_event_sets: Optional[ReviewEventSetStore] = None,
        provider_visual_evidence: Optional[
            ProviderVisualEvidencePreparationService
        ] = None,
        artifact_root_registry: Optional[
            ProjectArtifactRootRegistry
        ] = None,
        structured_label_delegator: Optional[
            Callable[
                [UUID, UUID, Sequence[str]],
                Mapping[str, Any],
            ]
        ] = None,
        project_model_resolver: Optional[
            Callable[[UUID], Any]
        ] = None,
        model_iteration_qa: Optional[
            ModelIterationQaStore
        ] = None,
        dataset_version_resolver: Optional[
            Callable[[UUID, UUID], Mapping[str, Any]]
        ] = None,
    ) -> None:
        self._media_intake = media_intake
        self._data_root = Path(data_root).resolve()
        self._data_root.mkdir(parents=True, exist_ok=True)
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        allowed_staging_sources = {
            "configured",
            "app_managed_fixed_volume",
            "bridge_data_fallback",
        }
        inferred_staging_source = (
            "configured"
            if dense_staging_root is not None
            else "bridge_data_fallback"
        )
        self._dense_staging_source = (
            dense_staging_source
            if dense_staging_source in allowed_staging_sources
            else inferred_staging_source
        )
        requested_dense_staging_root = (
            Path(dense_staging_root)
            if dense_staging_root is not None
            else self._data_root / "dense-staging"
        )
        self._dense_staging_root: Optional[Path] = None
        self._dense_staging_recovered_children = 0
        self._dense_staging_error_code: Optional[str] = None
        try:
            self._dense_staging_root = initialize_dense_staging_root(
                requested_dense_staging_root
            )
            # Electron owns a single authenticated bridge process and
            # PipelineJobService owns one durable worker. Under that enforced
            # serialization, any verified direct child present during startup
            # is a crash orphan, never a concurrent writer.
            self._dense_staging_recovered_children = (
                recover_dense_staging_root(self._dense_staging_root)
            )
        except DenseFrameIndexStagingUnavailable:
            self._dense_staging_root = None
            self._dense_staging_error_code = (
                "dense_staging_root_unavailable"
            )
        self._vision_status = vision_status or (
            lambda: {
                "enabled": False,
                "warning_code": "model_pack_not_configured",
            }
        )
        self._ocr_inference_service = ocr_inference_service
        if ocr_status is not None:
            self._ocr_status = ocr_status
        elif (
            ocr_inference_service is not None
            and callable(
                getattr(ocr_inference_service, "status", None)
            )
        ):
            self._ocr_status = getattr(
                ocr_inference_service, "status"
            )
        else:
            self._ocr_status = lambda: {
                "enabled": False,
                "mode": "scene_fallback",
                "warning_code": "model_pack_not_configured",
            }
        self._analysis_jobs = analysis_jobs
        self._command_proposal_generator = command_proposal_generator
        self._experimental_proposals = experimental_proposals
        self._candidate_labels = candidate_labels
        self._question_catalog_projector = question_catalog_projector
        self._weak_case_catalog = weak_case_catalog
        self._weak_case_approvals = weak_case_approvals
        self._weak_case_visual_index = weak_case_visual_index
        self._visual_evidence_semantic_observer = (
            visual_evidence_semantic_observer
        )
        self._sam2_status = sam2_status or (
            lambda: {
                "state": "unavailable",
                "error_code": "model_pack_not_configured",
            }
        )
        self._sam2_adapter = sam2_adapter
        self._derived_outputs = derived_outputs
        self._report_semantic_maps = report_semantic_maps
        self._report_provider_status = report_provider_status
        self._structured_vision_judgment = (
            structured_vision_judgment
        )
        self._dense_candidate_discovery = dense_candidate_discovery
        self._candidate_evidence_overlays = (
            candidate_evidence_overlays
        )
        self._candidate_evidence_registry = (
            candidate_evidence_registry
        )
        self._dense_index_registration = dense_index_registration
        self._review_event_sets = review_event_sets
        self._provider_visual_evidence = provider_visual_evidence
        self._artifact_roots = artifact_root_registry
        self._structured_label_delegator = (
            structured_label_delegator
        )
        self._project_model_resolver = project_model_resolver
        self._model_iteration_qa = model_iteration_qa
        self._dataset_version_resolver = dataset_version_resolver
        self._active_hash_jobs: dict[str, UUID] = {}
        self._active_analysis_jobs: dict[str, UUID] = {}
        self._lock = threading.RLock()

    def _prospective_pipeline_artifact_directory(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
        category: str,
    ) -> tuple[Path, Path]:
        """Resolve the authorized category path without creating directories."""

        if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", category) is None:
            raise PipelineStageFailure(
                "artifact_root_category_invalid"
            )
        try:
            job_id = UUID(str(context["job_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "artifact_root_job_identity_invalid"
            ) from error
        binding = context.get("artifact_root")
        if binding is not None:
            if (
                self._artifact_roots is None
                or not isinstance(binding, Mapping)
            ):
                raise PipelineStageFailure(
                    "artifact_root_revalidation_failed"
                )
            try:
                resolved = self._artifact_roots.resolve_job_binding(
                    project_id, binding
                )
            except ProjectArtifactRootError as error:
                raise PipelineStageFailure(
                    "artifact_root_revalidation_failed"
                ) from error
            project_root = resolved.root
            output_root = (
                project_root
                / "pipeline-jobs"
                / str(job_id)
                / category
            )
        else:
            # Pre-registry jobs stay on their original durable root.
            project_root = (
                self._data_root
                / "projects"
                / str(project_id)
                / "pipeline"
            )
            output_root = project_root / category / str(job_id)
        return project_root, output_root

    def _pipeline_artifact_directory(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
        category: str,
    ) -> tuple[Path, Path]:
        """Return the authorized project root and this job's category root."""

        project_root, output_root = (
            self._prospective_pipeline_artifact_directory(
                project_id,
                context,
                category,
            )
        )
        try:
            project_root.mkdir(parents=True, exist_ok=True)
            output_root.mkdir(parents=True, exist_ok=True)
            resolved_project = project_root.resolve(strict=True)
            resolved_output = output_root.resolve(strict=True)
            relative = resolved_output.relative_to(resolved_project)
        except (OSError, ValueError) as error:
            raise PipelineStageFailure(
                "artifact_root_unavailable"
            ) from error
        current = resolved_project
        if (
            project_root.is_symlink()
            or _is_reparse_point(project_root)
            or not resolved_project.is_dir()
            or not relative.parts
        ):
            raise PipelineStageFailure("artifact_root_unsafe")
        for part in relative.parts:
            current = current / part
            if (
                current.is_symlink()
                or _is_reparse_point(current)
                or not current.is_dir()
            ):
                raise PipelineStageFailure(
                    "artifact_root_unsafe"
                )
        return resolved_project, resolved_output

    def resolve_job_artifact_category(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
        category: str,
    ) -> Path:
        """Bridge-internal adapter used by scoped visual/report services."""

        _, category_root = self._pipeline_artifact_directory(
            project_id,
            context,
            category,
        )
        return category_root

    def resolve_completed_media_grant(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        context: Mapping[str, Any],
        expected_source_sha256: str,
    ) -> CompletedMediaGrant:
        """Freshly restore a persisted PipelineJob input without a UI path."""

        if (
            not isinstance(project_id, UUID)
            or not isinstance(analysis_job_id, UUID)
            or not isinstance(context, Mapping)
            or context.get("project_id") != str(project_id)
            or not isinstance(expected_source_sha256, str)
            or _SHA256.fullmatch(expected_source_sha256) is None
        ):
            raise PipelineStageFailure(
                "completed_media_grant_invalid"
            )
        try:
            snapshot_id = UUID(str(context.get("snapshot_id")))
        except (TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "completed_media_grant_invalid"
            ) from error
        file_name = context.get("file_name")
        checkpoints = context.get("checkpoints")
        sha_checkpoint = (
            checkpoints.get("sha256")
            if isinstance(checkpoints, Mapping)
            else None
        )
        if (
            not isinstance(file_name, str)
            or not is_allowlisted_video_name(file_name)
            or not isinstance(sha_checkpoint, Mapping)
        ):
            raise PipelineStageFailure(
                "completed_media_grant_invalid"
            )
        if self._analysis_jobs is None:
            raise PipelineStageFailure(
                "completed_media_grant_analysis_unavailable"
            )
        try:
            pipeline_job_id = UUID(str(context.get("job_id")))
        except (TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "completed_media_grant_invalid"
            ) from error
        owner_finder = getattr(
            self._analysis_jobs,
            "find_for_artifact_owner",
            None,
        )
        if not callable(owner_finder):
            raise PipelineStageFailure(
                "completed_media_grant_analysis_unavailable"
            )
        analysis = owner_finder(
            project_id,
            pipeline_job_id,
            snapshot_id=snapshot_id,
            file_name=file_name,
            source_sha256=expected_source_sha256,
        )
        if (
            not isinstance(analysis, Mapping)
            or analysis.get("job_id") != analysis_job_id
            or analysis.get("status") != "completed"
            or analysis.get("source_sha256") != expected_source_sha256
        ):
            raise PipelineStageFailure(
                "completed_media_grant_analysis_mismatch"
            )
        return self._resolve_pipeline_media_grant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            file_name=file_name,
            sha_checkpoint=sha_checkpoint,
            cancel=lambda: False,
            expected_source_sha256=expected_source_sha256,
        )

    def dense_staging_status(self) -> dict[str, Any]:
        """Return scrubbed readiness for FFmpeg 4.4 short-path staging."""

        root = self._dense_staging_root
        if root is None:
            return {
                "state": "unavailable",
                "source": self._dense_staging_source,
                "available_free_bytes": 0,
                "recovered_stale_children": (
                    self._dense_staging_recovered_children
                ),
                "error_code": (
                    self._dense_staging_error_code
                    or "dense_staging_root_unavailable"
                ),
                "path_exposed": False,
            }
        try:
            available = int(shutil.disk_usage(root).free)
        except (OSError, TypeError, ValueError):
            return {
                "state": "unavailable",
                "source": self._dense_staging_source,
                "available_free_bytes": 0,
                "recovered_stale_children": (
                    self._dense_staging_recovered_children
                ),
                "error_code": "dense_staging_usage_unavailable",
                "path_exposed": False,
            }
        return {
            "state": "ready",
            "source": self._dense_staging_source,
            "available_free_bytes": max(0, available),
            "recovered_stale_children": (
                self._dense_staging_recovered_children
            ),
            "error_code": None,
            "path_exposed": False,
        }

    @staticmethod
    def _job_scoped_dense_writer_capacity(
        context: Mapping[str, Any],
        *,
        duration_seconds: float,
        source_sha256: str,
    ) -> tuple[
        DenseFrameIndexLimits,
        DenseFrameIndexCapacityPlan,
        Optional[dict[str, Any]],
    ]:
        """Bind the dense writer to this Job's persisted storage estimate.

        The plan's frame count and aggregate per-frame allowance are already
        inside the hash-verified PipelineJob record.  Keep the engine's broad
        16 GiB value only as an absolute product cap; the actual writer gets
        this Job's smaller ``frames * allowance + fixed`` ceiling.
        """

        limits = DenseFrameIndexLimits(sampling_fps=2)
        raw_plan = context.get("storage_plan")
        if not isinstance(raw_plan, Mapping):
            return (
                limits,
                build_dense_frame_capacity_plan(
                    source_sha256=source_sha256,
                    duration_seconds=duration_seconds,
                    limits=limits,
                ),
                None,
            )
        try:
            candidate_rescan_fps = raw_plan["candidate_rescan_fps"]
            if (
                isinstance(candidate_rescan_fps, bool)
                or not isinstance(candidate_rescan_fps, int)
            ):
                raise ValueError("candidate rescan FPS is invalid")
            normalized = _validated_storage_plan(
                raw_plan,
                candidate_rescan_fps=candidate_rescan_fps,
            )
            if normalized is None:
                raise ValueError("storage plan is unavailable")
            from .folder_pipeline_batches import (
                build_pipeline_storage_plan,
            )

            expected = build_pipeline_storage_plan(
                duration_seconds=float(duration_seconds),
                candidate_rescan_fps=candidate_rescan_fps,
            )
            dense_stage = next(
                row
                for row in normalized["stage_estimates"]
                if row["stage"] == "dense_index_2fps"
            )
            expected_dense_stage = next(
                row
                for row in expected["stage_estimates"]
                if row["stage"] == "dense_index_2fps"
            )
            compared_fields = (
                "planned_frame_count",
                "average_bytes_per_frame_ceiling",
                "fixed_bytes",
                "hard_ceiling_bytes",
                "estimated_bytes",
            )
            if (
                any(
                    dense_stage[field] != expected_dense_stage[field]
                    for field in compared_fields
                )
                or normalized["dense_frame_count"]
                != dense_stage["planned_frame_count"]
                or normalized["average_frame_bytes_ceiling"]
                != dense_stage[
                    "average_bytes_per_frame_ceiling"
                ]
                or dense_stage["fixed_bytes"]
                != limits.max_index_bytes
            ):
                raise ValueError("dense storage plan is inconsistent")
            planned_frame_count = int(
                dense_stage["planned_frame_count"]
            )
            average_frame_bytes = int(
                dense_stage["average_bytes_per_frame_ceiling"]
            )
            fixed_allowance_bytes = int(
                dense_stage["fixed_bytes"]
            )
            frame_artifact_bytes = (
                planned_frame_count * average_frame_bytes
            )
            writer_capacity_bytes = int(
                dense_stage["estimated_bytes"]
            )
            absolute_hard_ceiling = int(
                dense_stage["hard_ceiling_bytes"]
            )
            if (
                writer_capacity_bytes
                != frame_artifact_bytes + fixed_allowance_bytes
                or writer_capacity_bytes > absolute_hard_ceiling
                or writer_capacity_bytes <= limits.max_index_bytes
            ):
                raise ValueError("dense writer ceiling is invalid")
            job_id = str(UUID(str(context["job_id"])))
            plan_sha256 = hashlib.sha256(
                _canonical_json(normalized)
            ).hexdigest()
            dense_stage_sha256 = hashlib.sha256(
                _canonical_json(dense_stage)
            ).hexdigest()
            limits = replace(
                limits,
                max_frames=planned_frame_count,
                max_total_output_bytes=writer_capacity_bytes,
            )
            capacity_plan = build_dense_frame_capacity_plan(
                source_sha256=source_sha256,
                duration_seconds=float(duration_seconds),
                limits=limits,
            )
            if (
                capacity_plan.planned_frame_count
                != planned_frame_count
                or capacity_plan.planned_output_bytes_upper_bound
                != writer_capacity_bytes
                or capacity_plan.hard_ceiling_bytes
                != writer_capacity_bytes
            ):
                raise ValueError(
                    "dense engine bound does not match the Job plan"
                )
            binding_body = {
                "schema_version": (
                    "trafficvisson.dense-job-writer-capacity.v1"
                ),
                "job_id": job_id,
                "pipeline_storage_plan_sha256": plan_sha256,
                "dense_stage_sha256": dense_stage_sha256,
                "planned_frame_count": planned_frame_count,
                "average_frame_bytes_ceiling": average_frame_bytes,
                "frame_artifact_bytes_ceiling": frame_artifact_bytes,
                "fixed_allowance_bytes": fixed_allowance_bytes,
                "writer_capacity_bytes": writer_capacity_bytes,
                "absolute_engine_hard_ceiling_bytes": (
                    absolute_hard_ceiling
                ),
                "path_exposed": False,
            }
            binding = {
                **binding_body,
                "binding_sha256": hashlib.sha256(
                    _canonical_json(binding_body)
                ).hexdigest(),
            }
            _validate_safe_json(
                binding,
                "dense Job writer capacity binding",
            )
        except (
            KeyError,
            StopIteration,
            TypeError,
            ValueError,
        ) as error:
            raise PipelineStageFailure(
                "storage_capacity_plan_invalid"
            ) from error
        return limits, capacity_plan, binding

    def preflight_dense_after_intake(
        self,
        context: Mapping[str, Any],
        *,
        duration_seconds: float,
    ) -> Mapping[str, Any]:
        """Check required short-path capacity before the SHA stage starts.

        This is deliberately prospective: it resolves no media grant and
        creates no artifact directory.  The intake-derived output reservation
        is persisted by ``PipelineJobService`` first, then this independent
        staging-volume gate either accepts the same bounded writer plan or
        blocks the intake boundary.
        """

        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(float(duration_seconds))
            or duration_seconds <= 0
        ):
            raise PipelineStageFailure(
                "storage_capacity_plan_invalid"
            )
        try:
            (
                limits,
                capacity_plan,
                writer_capacity_binding,
            ) = self._job_scoped_dense_writer_capacity(
                context,
                source_sha256="0" * 64,
                duration_seconds=float(duration_seconds),
            )
            project_id = UUID(str(context["project_id"]))
            prospective_project_root, prospective_output_root = (
                self._prospective_pipeline_artifact_directory(
                    project_id,
                    context,
                    "dense",
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "storage_capacity_plan_invalid"
            ) from error
        staging_required = dense_frame_index_requires_short_staging(
            prospective_output_root
        )
        same_volume_publish = (
            staging_required
            and self._dense_staging_root is not None
            and same_storage_volume(
                self._dense_staging_root,
                prospective_project_root,
            )
        )
        required_free_bytes = (
            (
                2
                if same_volume_publish
                else 1
            )
            * capacity_plan.planned_output_bytes_upper_bound
            + limits.min_free_disk_bytes
        )
        status = self.dense_staging_status()
        available_free_bytes = int(
            status.get("available_free_bytes", 0)
        )
        observation = {
            "schema_version": (
                "trafficvisson.dense-staging-observation.v1"
            ),
            "state": "ready" if staging_required else "not_required",
            "source": status.get(
                "source",
                "bridge_data_fallback",
            ),
            "required": staging_required,
            "same_volume_atomic_publish": same_volume_publish,
            "required_free_bytes": required_free_bytes,
            "available_free_bytes": max(0, available_free_bytes),
            "writer_capacity_bytes": (
                capacity_plan.planned_output_bytes_upper_bound
            ),
            "writer_capacity_binding": writer_capacity_binding,
            "error_code": None,
            "path_exposed": False,
        }
        if (
            staging_required
            and (
                status.get("state") != "ready"
                or available_free_bytes < required_free_bytes
            )
        ):
            raise PipelineStageFailure(
                "dense_staging_storage_blocked",
                observation={
                    **observation,
                    "state": "insufficient_or_unavailable",
                    "error_code": (
                        status.get("error_code")
                        or "dense_staging_storage_blocked"
                    ),
                },
            )
        return observation

    def _candidate_evidence_capacity_upper_bound(
        self,
        candidate_count: int,
    ) -> int:
        calculator = getattr(
            self._candidate_evidence_overlays,
            "output_capacity_upper_bound",
            None,
        )
        try:
            if callable(calculator):
                value = calculator(candidate_count)
            else:
                value = (
                    CandidateEvidenceOverlayLimits()
                    .output_capacity_upper_bound(candidate_count)
                )
        except (TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "storage_capacity_replan_invalid"
            ) from error
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise PipelineStageFailure(
                "storage_capacity_replan_invalid"
            )
        return value

    @staticmethod
    def _storage_capacity_revision(
        context: Mapping[str, Any],
        *,
        stage: str,
        required_bytes: int,
        basis: Mapping[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Bind an exact post-discovery plan before any stage artifacts write.

        Folder preflight intentionally estimates likely output.  Once actual
        candidate windows or merged event counts exist, this immutable
        revision either fits the original plan, has already been atomically
        accepted by ``PipelineJobService``, or interrupts the stage before
        writes so the service can reserve the larger same-volume amount.
        """

        raw_plan = context.get("storage_plan")
        if not isinstance(raw_plan, Mapping):
            return None
        stage_rows = raw_plan.get("stage_estimates")
        if (
            isinstance(required_bytes, bool)
            or not isinstance(required_bytes, int)
            or required_bytes < 0
            or not isinstance(stage_rows, list)
        ):
            raise PipelineStageFailure(
                "storage_capacity_replan_invalid"
            )
        stage_plan = next(
            (
                row
                for row in stage_rows
                if isinstance(row, Mapping)
                and row.get("stage") == stage
            ),
            None,
        )
        if stage_plan is None:
            raise PipelineStageFailure(
                "storage_capacity_replan_invalid"
            )
        original_bytes = stage_plan.get("estimated_bytes")
        hard_bytes = stage_plan.get("hard_ceiling_bytes")
        if (
            isinstance(original_bytes, bool)
            or not isinstance(original_bytes, int)
            or original_bytes < 0
            or (
                hard_bytes is not None
                and (
                    isinstance(hard_bytes, bool)
                    or not isinstance(hard_bytes, int)
                    or hard_bytes < 1
                )
            )
        ):
            raise PipelineStageFailure(
                "storage_capacity_replan_invalid"
            )
        if hard_bytes is not None and required_bytes > hard_bytes:
            raise PipelineStageFailure(
                "storage_capacity_replan_required",
                observation={
                    "schema_version": PIPELINE_STORAGE_REPLAN_SCHEMA,
                    "stage": stage,
                    "state": "exceeds_absolute_engine_cap",
                    "required_bytes": required_bytes,
                    "original_planned_bytes": original_bytes,
                    "hard_ceiling_bytes": hard_bytes,
                    "path_exposed": False,
                },
            )
        revision_body = {
            "schema_version": PIPELINE_STORAGE_REPLAN_SCHEMA,
            "stage": stage,
            "state": (
                "within_original_plan"
                if required_bytes <= original_bytes
                else "same_volume_adjustment_required"
            ),
            "original_plan_sha256": hashlib.sha256(
                _canonical_json(raw_plan)
            ).hexdigest(),
            "original_planned_bytes": original_bytes,
            "required_bytes": required_bytes,
            "hard_ceiling_bytes": hard_bytes,
            "basis": dict(basis),
            "path_exposed": False,
        }
        try:
            _validate_safe_json(
                revision_body,
                "pipeline storage capacity revision",
            )
        except ValueError as error:
            raise PipelineStageFailure(
                "storage_capacity_replan_invalid"
            ) from error
        revision = {
            **revision_body,
            "revision_sha256": hashlib.sha256(
                _canonical_json(revision_body)
            ).hexdigest(),
        }
        if required_bytes <= original_bytes:
            return revision
        accepted = context.get("storage_plan_revisions", ())
        if (
            isinstance(accepted, list)
            and any(
                isinstance(item, Mapping)
                and item.get("stage") == stage
                and item.get("revision_sha256")
                == revision["revision_sha256"]
                for item in accepted
            )
        ):
            return {
                **revision,
                "state": "same_volume_adjustment_accepted",
            }
        raise PipelineStageFailure(
            "storage_capacity_replan_required",
            observation=revision,
        )

    def validate_report_profile(
        self,
        *,
        project_id: UUID,
        template_id: str,
        template_sha256: str,
        semantic_map_id: str,
        provider: str,
        explicit_transmission_consent: bool,
    ) -> Mapping[str, Any]:
        if not isinstance(project_id, UUID):
            raise ValueError("project_id must be a UUID")
        if self._report_semantic_maps is None:
            raise PipelineJobConflict(
                "The sample template semantic map is unavailable"
            )
        try:
            semantic_map = self._report_semantic_maps.resolve(
                template_id=template_id,
                template_sha256=template_sha256,
                semantic_map_id=semantic_map_id,
            )
            semantic_snapshot = semantic_map.profile_snapshot()
        except Exception as error:
            raise PipelineJobConflict(
                "The sample template semantic map is unavailable"
            ) from error
        provider_ready = False
        provider_state = "disabled"
        provider_capabilities: list[str] = []
        if provider in {"codex", "claude"}:
            if self._report_provider_status is None:
                provider_state = "unavailable"
            else:
                try:
                    status = self._report_provider_status(provider)
                    provider_state = str(
                        getattr(status, "state", "")
                        or (
                            status.get("state")
                            if isinstance(status, Mapping)
                            else ""
                        )
                    )
                    raw_capabilities = (
                        getattr(status, "capabilities", ())
                        if not isinstance(status, Mapping)
                        else status.get("capabilities", ())
                    )
                    if isinstance(raw_capabilities, (list, tuple)):
                        provider_capabilities = [
                            str(item) for item in raw_capabilities
                        ]
                    provider_ready = (
                        provider_state == "ready"
                        and {
                            "report_draft",
                            "document_edit_plan",
                        }.issubset(set(provider_capabilities))
                    )
                except Exception:
                    provider_state = "unavailable"
        if explicit_transmission_consent and not provider_ready:
            raise PipelineJobConflict(
                "The selected official CLI provider is not ready"
            )
        return {
            "template_semantic_map_id": (
                semantic_snapshot["template_semantic_map_id"]
            ),
            "template_semantic_map_sha256": (
                semantic_snapshot["template_semantic_map_sha256"]
            ),
            "template_semantic_map_ready": True,
            "report_provider_ready": provider_ready,
            "report_provider_state": provider_state,
            "report_provider_capabilities": provider_capabilities,
            "explicit_transmission_consent": (
                explicit_transmission_consent
            ),
            "project_id": str(project_id),
            "path_exposed": False,
        }

    def _private_media_grants_root(self, project_id: UUID) -> Path:
        projects_root = self._data_root / "projects"
        project_root = projects_root / str(project_id)
        pipeline_root = project_root / "pipeline"
        grants_root = pipeline_root / "private-media-grants"
        projects_root.mkdir(exist_ok=True)
        project_root.mkdir(exist_ok=True)
        pipeline_root.mkdir(exist_ok=True)
        grants_root.mkdir(exist_ok=True)
        try:
            resolved_projects = projects_root.resolve(strict=True)
            resolved_project = project_root.resolve(strict=True)
            resolved_pipeline = pipeline_root.resolve(strict=True)
            resolved_grants = grants_root.resolve(strict=True)
        except OSError as error:
            raise PipelineStageFailure(
                "private_media_grant_store_unavailable"
            ) from error
        if (
            projects_root.is_symlink()
            or project_root.is_symlink()
            or pipeline_root.is_symlink()
            or grants_root.is_symlink()
            or _is_reparse_point(projects_root)
            or _is_reparse_point(project_root)
            or _is_reparse_point(pipeline_root)
            or _is_reparse_point(grants_root)
            or not resolved_projects.is_dir()
            or not resolved_project.is_dir()
            or not resolved_pipeline.is_dir()
            or not resolved_grants.is_dir()
            or resolved_projects.parent != self._data_root
            or resolved_project.parent != resolved_projects
            or resolved_pipeline.parent != resolved_project
            or resolved_grants.parent != resolved_pipeline
        ):
            raise PipelineStageFailure(
                "private_media_grant_store_unsafe"
            )
        return resolved_grants

    def _private_media_grant_path(
        self,
        project_id: UUID,
        hash_job_id: UUID,
    ) -> Path:
        return (
            self._private_media_grants_root(project_id)
            / f"{hash_job_id}.json"
        )

    def _private_media_snapshot_path(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
    ) -> Path:
        return (
            self._private_media_grants_root(project_id)
            / f"prehash-{pipeline_job_id}.json"
        )

    @staticmethod
    def _validated_input_restart_binding(
        value: Any,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        input_fingerprint: str,
    ) -> dict[str, Any]:
        fields = {
            "schema_version",
            "project_id",
            "job_id",
            "snapshot_id",
            "file_name",
            "input_fingerprint",
            "rights_confirmed",
            "private_record_sha256",
            "snapshot_digest",
            "file_identity_sha256",
            "size_bytes",
            "created_time_ns",
            "modified_time_ns",
            "path_exposed",
            "binding_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("input restart binding fields are invalid")
        _validate_safe_json(value, "input restart binding")
        body = {
            key: item
            for key, item in value.items()
            if key != "binding_sha256"
        }
        if (
            value.get("schema_version")
            != PIPELINE_INPUT_RESTART_BINDING_SCHEMA
            or value.get("project_id") != str(project_id)
            or value.get("job_id") != str(pipeline_job_id)
            or value.get("snapshot_id") != str(snapshot_id)
            or value.get("file_name") != file_name
            or value.get("input_fingerprint") != input_fingerprint
            or value.get("rights_confirmed") is not True
            or value.get("path_exposed") is not False
            or not is_allowlisted_video_name(file_name)
            or _SHA256.fullmatch(input_fingerprint) is None
            or not isinstance(value.get("private_record_sha256"), str)
            or _SHA256.fullmatch(value["private_record_sha256"]) is None
            or not isinstance(value.get("snapshot_digest"), str)
            or _SHA256.fullmatch(value["snapshot_digest"]) is None
            or not isinstance(value.get("file_identity_sha256"), str)
            or _SHA256.fullmatch(value["file_identity_sha256"]) is None
            or any(
                isinstance(value.get(field), bool)
                or not isinstance(value.get(field), int)
                or value[field] <= 0
                for field in (
                    "size_bytes",
                    "created_time_ns",
                    "modified_time_ns",
                )
            )
            or value.get("binding_sha256")
            != hashlib.sha256(_canonical_json(body)).hexdigest()
        ):
            raise ValueError("input restart binding identity is invalid")
        return dict(value)

    def capture_restartable_input(
        self,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Persist path-private pre-hash authority before a Job is queued."""

        try:
            project_id = UUID(str(context["project_id"]))
            pipeline_job_id = UUID(str(context["job_id"]))
            snapshot_id = UUID(str(context["snapshot_id"]))
            file_name = str(context["file_name"])
            input_fingerprint = str(context["input_fingerprint"])
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "media_snapshot_restart_binding_invalid"
            ) from error
        if (
            context.get("rights_confirmed") is not True
            or not is_allowlisted_video_name(file_name)
            or _SHA256.fullmatch(input_fingerprint) is None
        ):
            raise PipelineStageFailure(
                "media_snapshot_restart_binding_invalid"
            )
        try:
            grant = self._media_intake.export_stable_file_grant(
                project_id,
                snapshot_id,
                file_name,
            )
        except HTTPException as error:
            raise PipelineStageFailure(
                "media_snapshot_revalidation_failed"
            ) from error
        record = grant.file_record
        payload = {
            "schema_version": PRIVATE_MEDIA_SNAPSHOT_SCHEMA,
            "project_id": str(project_id),
            "job_id": str(pipeline_job_id),
            "snapshot_id": str(snapshot_id),
            "file_name": file_name,
            "input_fingerprint": input_fingerprint,
            "rights_confirmed": True,
            "media_path": str(
                Path(grant.directory) / record.name
            ),
            "directory_identity": grant.directory_identity,
            "snapshot_digest": grant.snapshot_digest,
            "file_identity_sha256": grant.file_identity_sha256,
            "size_bytes": record.size_bytes,
            "created_time_ns": record.created_time_ns,
            "modified_time_ns": record.modified_time_ns,
            "persisted_at": _utc_now().isoformat(),
        }
        raw = _canonical_json(payload)
        if not 1 <= len(raw) <= MAX_PRIVATE_MEDIA_GRANT_BYTES:
            raise PipelineStageFailure(
                "media_snapshot_restart_binding_invalid"
            )
        private_record_sha256 = hashlib.sha256(raw).hexdigest()
        path = self._private_media_snapshot_path(
            project_id,
            pipeline_job_id,
        )
        try:
            _write_atomic(path, raw)
            _write_atomic(
                path.with_suffix(".json.sha256"),
                f"{private_record_sha256}\n".encode("ascii"),
            )
        except (OSError, PipelineJobUnavailable) as error:
            raise PipelineStageFailure(
                "media_snapshot_restart_store_unavailable"
            ) from error
        binding_body = {
            "schema_version": PIPELINE_INPUT_RESTART_BINDING_SCHEMA,
            "project_id": str(project_id),
            "job_id": str(pipeline_job_id),
            "snapshot_id": str(snapshot_id),
            "file_name": file_name,
            "input_fingerprint": input_fingerprint,
            "rights_confirmed": True,
            "private_record_sha256": private_record_sha256,
            "snapshot_digest": grant.snapshot_digest,
            "file_identity_sha256": grant.file_identity_sha256,
            "size_bytes": record.size_bytes,
            "created_time_ns": record.created_time_ns,
            "modified_time_ns": record.modified_time_ns,
            "path_exposed": False,
        }
        binding = {
            **binding_body,
            "binding_sha256": hashlib.sha256(
                _canonical_json(binding_body)
            ).hexdigest(),
        }
        return self._validated_input_restart_binding(
            binding,
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            snapshot_id=snapshot_id,
            file_name=file_name,
            input_fingerprint=input_fingerprint,
        )

    def _restore_restartable_input(
        self,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            project_id = UUID(str(context["project_id"]))
            pipeline_job_id = UUID(str(context["job_id"]))
            snapshot_id = UUID(str(context["snapshot_id"]))
            file_name = str(context["file_name"])
            input_fingerprint = str(context["input_fingerprint"])
            binding = self._validated_input_restart_binding(
                context.get("input_restart_binding"),
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                snapshot_id=snapshot_id,
                file_name=file_name,
                input_fingerprint=input_fingerprint,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "media_snapshot_restart_binding_invalid"
            ) from error
        if context.get("rights_confirmed") is not True:
            raise PipelineStageFailure(
                "media_snapshot_restart_binding_invalid"
            )
        path = self._private_media_snapshot_path(
            project_id,
            pipeline_job_id,
        )
        digest_path = path.with_suffix(".json.sha256")
        try:
            if (
                not path.is_file()
                or not digest_path.is_file()
                or path.is_symlink()
                or digest_path.is_symlink()
                or _is_reparse_point(path)
                or _is_reparse_point(digest_path)
            ):
                raise ValueError("private snapshot files are unavailable")
            raw = path.read_bytes()
            if not 1 <= len(raw) <= MAX_PRIVATE_MEDIA_GRANT_BYTES:
                raise ValueError("private snapshot size is invalid")
            private_digest = digest_path.read_text(
                encoding="ascii"
            ).strip()
            if (
                _SHA256.fullmatch(private_digest) is None
                or private_digest
                != binding["private_record_sha256"]
                or hashlib.sha256(raw).hexdigest() != private_digest
            ):
                raise ValueError("private snapshot digest mismatch")
            payload = _strict_json(raw, "private media snapshot")
            if set(payload) != {
                "schema_version",
                "project_id",
                "job_id",
                "snapshot_id",
                "file_name",
                "input_fingerprint",
                "rights_confirmed",
                "media_path",
                "directory_identity",
                "snapshot_digest",
                "file_identity_sha256",
                "size_bytes",
                "created_time_ns",
                "modified_time_ns",
                "persisted_at",
            }:
                raise ValueError("private snapshot fields are invalid")
            if (
                payload.get("schema_version")
                != PRIVATE_MEDIA_SNAPSHOT_SCHEMA
                or payload.get("project_id") != str(project_id)
                or payload.get("job_id") != str(pipeline_job_id)
                or payload.get("snapshot_id") != str(snapshot_id)
                or payload.get("file_name") != file_name
                or payload.get("input_fingerprint")
                != input_fingerprint
                or payload.get("rights_confirmed") is not True
                or any(
                    payload.get(field) != binding[field]
                    for field in (
                        "snapshot_digest",
                        "file_identity_sha256",
                        "size_bytes",
                        "created_time_ns",
                        "modified_time_ns",
                    )
                )
            ):
                raise ValueError("private snapshot identity is invalid")
            _parse_datetime(payload.get("persisted_at"))
            media_path_value = payload.get("media_path")
            directory_identity = payload.get("directory_identity")
            if (
                not isinstance(media_path_value, str)
                or not 1 <= len(media_path_value) <= 4_096
                or "\x00" in media_path_value
                or not isinstance(directory_identity, str)
                or not directory_identity
                or len(directory_identity) > 4_096
            ):
                raise ValueError("private snapshot source is invalid")
            media_path = Path(media_path_value)
            if (
                not media_path.is_absolute()
                or media_path.name != file_name
            ):
                raise ValueError("private snapshot source is invalid")
            grant = StableMediaFileGrant(
                project_id=project_id,
                snapshot_id=snapshot_id,
                directory=str(media_path.parent),
                directory_identity=directory_identity,
                snapshot_digest=binding["snapshot_digest"],
                file_record=MediaFileRecord(
                    name=file_name,
                    extension=Path(file_name).suffix.casefold(),
                    size_bytes=binding["size_bytes"],
                    created_time_ns=binding["created_time_ns"],
                    modified_time_ns=binding["modified_time_ns"],
                ),
                file_identity_sha256=binding[
                    "file_identity_sha256"
                ],
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise PipelineStageFailure(
                "media_snapshot_restart_binding_invalid"
            ) from error
        try:
            self._media_intake.restore_stable_file_grant(grant)
        except HTTPException as error:
            raise PipelineStageFailure(
                "media_snapshot_revalidation_failed"
            ) from error
        return binding

    def _persist_private_media_grant(
        self,
        grant: CompletedMediaGrant,
    ) -> None:
        candidate = grant.media_path
        try:
            directory_stat = candidate.parent.lstat()
            file_stat = candidate.lstat()
        except OSError as error:
            raise PipelineStageFailure(
                "private_media_grant_source_unavailable"
            ) from error
        actual_created_time_ns = int(
            getattr(
                file_stat,
                "st_birthtime_ns",
                file_stat.st_ctime_ns,
            )
        )
        created_time_ns = (
            grant.created_time_ns
            if grant.created_time_ns is not None
            else actual_created_time_ns
        )
        if (
            not isinstance(grant.project_id, UUID)
            or not isinstance(grant.snapshot_id, UUID)
            or not isinstance(grant.hash_job_id, UUID)
            or not is_allowlisted_video_name(grant.file_name)
            or not candidate.is_absolute()
            or candidate.name != grant.file_name
            or candidate.is_symlink()
            or candidate.parent.is_symlink()
            or _is_reparse_point(candidate)
            or _is_reparse_point(candidate.parent)
            or not stat.S_ISREG(file_stat.st_mode)
            or not stat.S_ISDIR(directory_stat.st_mode)
            or int(file_stat.st_size) != grant.size_bytes
            or int(file_stat.st_mtime_ns) != grant.modified_time_ns
            or created_time_ns != actual_created_time_ns
            or created_time_ns <= 0
            or _SHA256.fullmatch(grant.sha256_lower) is None
        ):
            raise PipelineStageFailure(
                "private_media_grant_source_changed"
            )
        payload = {
            "schema_version": PRIVATE_MEDIA_GRANT_SCHEMA,
            "project_id": str(grant.project_id),
            "snapshot_id": str(grant.snapshot_id),
            "hash_job_id": str(grant.hash_job_id),
            "file_name": grant.file_name,
            "media_path": str(candidate),
            "size_bytes": grant.size_bytes,
            "created_time_ns": created_time_ns,
            "modified_time_ns": grant.modified_time_ns,
            "source_sha256": grant.sha256_lower,
            "persisted_at": _utc_now().isoformat(),
        }
        raw = _canonical_json(payload)
        if not 1 <= len(raw) <= MAX_PRIVATE_MEDIA_GRANT_BYTES:
            raise PipelineStageFailure(
                "private_media_grant_record_invalid"
            )
        digest = hashlib.sha256(raw).hexdigest()
        path = self._private_media_grant_path(
            grant.project_id,
            grant.hash_job_id,
        )
        try:
            _write_atomic(path, raw)
            _write_atomic(
                path.with_suffix(".json.sha256"),
                f"{digest}\n".encode("ascii"),
            )
        except (OSError, PipelineJobUnavailable) as error:
            raise PipelineStageFailure(
                "private_media_grant_store_unavailable"
            ) from error

    def _restore_private_media_grant(
        self,
        *,
        project_id: UUID,
        snapshot_id: UUID,
        hash_job_id: UUID,
        file_name: str,
        source_sha256: str,
        total_bytes: int,
        cancel: Callable[[], bool],
    ) -> CompletedMediaGrant:
        path = self._private_media_grant_path(project_id, hash_job_id)
        digest_path = path.with_suffix(".json.sha256")
        try:
            if (
                not path.is_file()
                or not digest_path.is_file()
                or path.is_symlink()
                or digest_path.is_symlink()
                or _is_reparse_point(path)
                or _is_reparse_point(digest_path)
            ):
                raise ValueError("private grant files are unavailable")
            raw = path.read_bytes()
            if not 1 <= len(raw) <= MAX_PRIVATE_MEDIA_GRANT_BYTES:
                raise ValueError("private grant size is invalid")
            expected_digest = digest_path.read_text(
                encoding="ascii"
            ).strip()
            if (
                _SHA256.fullmatch(expected_digest) is None
                or hashlib.sha256(raw).hexdigest() != expected_digest
            ):
                raise ValueError("private grant digest mismatch")
            payload = _strict_json(raw, "private media grant")
            if set(payload) != {
                "schema_version",
                "project_id",
                "snapshot_id",
                "hash_job_id",
                "file_name",
                "media_path",
                "size_bytes",
                "created_time_ns",
                "modified_time_ns",
                "source_sha256",
                "persisted_at",
            }:
                raise ValueError("private grant fields are invalid")
            if (
                payload.get("schema_version")
                != PRIVATE_MEDIA_GRANT_SCHEMA
                or payload.get("project_id") != str(project_id)
                or payload.get("snapshot_id") != str(snapshot_id)
                or payload.get("hash_job_id") != str(hash_job_id)
                or payload.get("file_name") != file_name
                or payload.get("source_sha256") != source_sha256
                or payload.get("size_bytes") != total_bytes
                or not is_allowlisted_video_name(file_name)
                or isinstance(payload.get("created_time_ns"), bool)
                or not isinstance(payload.get("created_time_ns"), int)
                or payload["created_time_ns"] <= 0
                or isinstance(payload.get("modified_time_ns"), bool)
                or not isinstance(payload.get("modified_time_ns"), int)
                or payload["modified_time_ns"] <= 0
            ):
                raise ValueError("private grant identity is invalid")
            _parse_datetime(payload.get("persisted_at"))
            media_path_value = payload.get("media_path")
            if (
                not isinstance(media_path_value, str)
                or not 1 <= len(media_path_value) <= 4_096
                or "\x00" in media_path_value
            ):
                raise ValueError("private grant source is invalid")
            media_path = Path(media_path_value)
            if (
                not media_path.is_absolute()
                or media_path.name != file_name
            ):
                raise ValueError("private grant source is invalid")
            grant = CompletedMediaGrant(
                project_id=project_id,
                snapshot_id=snapshot_id,
                hash_job_id=hash_job_id,
                file_name=file_name,
                media_path=media_path,
                size_bytes=total_bytes,
                modified_time_ns=payload["modified_time_ns"],
                sha256_lower=source_sha256,
                created_time_ns=payload["created_time_ns"],
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise PipelineStageFailure(
                "private_media_grant_unavailable"
            ) from error

        try:
            return self._media_intake.restore_completed_media_grant(
                grant,
                cancel=cancel,
            )
        except HTTPException as error:
            raise PipelineStageFailure(
                "private_media_grant_revalidation_failed"
            ) from error

    def validate_server_owned_rerun_source(
        self,
        *,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        sha_checkpoint: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Re-hash one private grant before it can seed a new PipelineJob."""

        if not isinstance(sha_checkpoint, Mapping):
            raise PipelineStageFailure("hash_checkpoint_missing")
        source_sha256 = sha_checkpoint.get("source_sha256")
        raw_hash_job_id = sha_checkpoint.get("hash_job_id")
        total_bytes = sha_checkpoint.get("total_bytes")
        if (
            not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(raw_hash_job_id, str)
            or isinstance(total_bytes, bool)
            or not isinstance(total_bytes, int)
            or total_bytes <= 0
        ):
            raise PipelineStageFailure("hash_checkpoint_missing")
        try:
            hash_job_id = UUID(raw_hash_job_id)
        except ValueError as error:
            raise PipelineStageFailure(
                "hash_checkpoint_missing"
            ) from error
        grant = self._restore_private_media_grant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            hash_job_id=hash_job_id,
            file_name=file_name,
            source_sha256=source_sha256,
            total_bytes=total_bytes,
            cancel=lambda: False,
        )
        proof = {
            "schema_version": PIPELINE_RERUN_MEDIA_PROOF_SCHEMA,
            "project_id": str(grant.project_id),
            "snapshot_id": str(grant.snapshot_id),
            "hash_job_id": str(grant.hash_job_id),
            "file_name": grant.file_name,
            "source_sha256": grant.sha256_lower,
            "size_bytes": grant.size_bytes,
            "path_exposed": False,
        }
        return _validated_rerun_media_proof(
            proof,
            project_id=project_id,
            snapshot_id=snapshot_id,
            file_name=file_name,
            hash_job_id=hash_job_id,
            source_sha256=source_sha256,
            total_bytes=total_bytes,
        )

    def _persist_completed_hash_grant(
        self,
        *,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        hash_job_id: UUID,
        source_sha256: str,
    ) -> None:
        try:
            grant = self._media_intake.resolve_completed_media(
                project_id,
                snapshot_id,
                file_name,
                hash_job_id,
                source_sha256,
            )
        except HTTPException as error:
            raise PipelineStageFailure(
                "completed_media_grant_unavailable"
            ) from error
        self._persist_private_media_grant(grant)

    def _resolve_pipeline_media_grant(
        self,
        *,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        sha_checkpoint: Mapping[str, Any],
        cancel: Callable[[], bool],
        expected_source_sha256: Optional[str] = None,
    ) -> CompletedMediaGrant:
        """Resolve one completed input across the process-restart boundary.

        The MediaIntake registry is intentionally in-memory.  A completed
        PipelineJob SHA checkpoint is durable, so after a process restart the
        bridge may restore only the exact bridge-private grant persisted for
        that checkpoint.  A missing in-memory job (404) is the sole fallback
        condition; conflicts and changed sources fail closed instead of being
        disguised as a successful restore.
        """

        if not isinstance(sha_checkpoint, Mapping):
            raise PipelineStageFailure("hash_checkpoint_missing")
        source_sha256 = sha_checkpoint.get("source_sha256")
        raw_hash_job_id = sha_checkpoint.get("hash_job_id")
        if (
            not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(raw_hash_job_id, str)
            or (
                expected_source_sha256 is not None
                and source_sha256 != expected_source_sha256
            )
        ):
            raise PipelineStageFailure("hash_checkpoint_missing")
        try:
            hash_job_id = UUID(raw_hash_job_id)
        except ValueError as error:
            raise PipelineStageFailure(
                "hash_checkpoint_missing"
            ) from error

        try:
            grant = self._media_intake.resolve_completed_media(
                project_id,
                snapshot_id,
                file_name,
                hash_job_id,
                source_sha256,
            )
        except HTTPException as error:
            if error.status_code != 404:
                raise PipelineStageFailure(
                    "completed_media_grant_unavailable"
                ) from error
            total_bytes = sha_checkpoint.get("total_bytes")
            if (
                isinstance(total_bytes, bool)
                or not isinstance(total_bytes, int)
                or total_bytes <= 0
            ):
                raise PipelineStageFailure(
                    "hash_checkpoint_missing"
                ) from error
            return self._restore_private_media_grant(
                project_id=project_id,
                snapshot_id=snapshot_id,
                hash_job_id=hash_job_id,
                file_name=file_name,
                source_sha256=source_sha256,
                total_bytes=total_bytes,
                cancel=cancel,
            )

        if (
            not isinstance(grant, CompletedMediaGrant)
            or grant.project_id != project_id
            or grant.snapshot_id != snapshot_id
            or grant.hash_job_id != hash_job_id
            or grant.file_name != file_name
            or grant.sha256_lower != source_sha256
        ):
            raise PipelineStageFailure(
                "completed_media_grant_invalid"
            )
        total_bytes = sha_checkpoint.get("total_bytes")
        if total_bytes is not None and (
            isinstance(total_bytes, bool)
            or not isinstance(total_bytes, int)
            or total_bytes <= 0
            or grant.size_bytes != total_bytes
        ):
            raise PipelineStageFailure(
                "hash_checkpoint_missing"
            )
        return grant

    def migrate_legacy_completed_media_grant(
        self,
        *,
        project_id: UUID,
        snapshot_id: UUID,
        hash_job_id: UUID,
        file_name: str,
        authorized_media_path: Path,
        size_bytes: int,
        created_time_ns: int,
        modified_time_ns: int,
        source_sha256: str,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> Mapping[str, Any]:
        """Create a private restart grant for a pre-migration pipeline.

        This operator-only method has no HTTP/renderer route. It is intended
        solely for jobs whose SHA stage completed before private grants were
        introduced and whose exact previously authorized path is still known.
        The normal MediaIntake trust boundary revalidates path, metadata and
        the complete SHA-256 before this method writes any durable grant.
        """
        if cancel is None:
            cancel = lambda: False
        if not callable(cancel):
            raise PipelineStageFailure(
                "legacy_media_grant_revalidation_failed"
            )
        try:
            media_path = Path(authorized_media_path)
        except TypeError as error:
            raise PipelineStageFailure(
                "legacy_media_grant_revalidation_failed"
            ) from error
        grant = CompletedMediaGrant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            hash_job_id=hash_job_id,
            file_name=file_name,
            media_path=media_path,
            size_bytes=size_bytes,
            modified_time_ns=modified_time_ns,
            sha256_lower=source_sha256,
            created_time_ns=created_time_ns,
        )
        try:
            restored = self._media_intake.restore_completed_media_grant(
                grant,
                cancel=cancel,
            )
        except HTTPException as error:
            raise PipelineStageFailure(
                "legacy_media_grant_revalidation_failed"
            ) from error
        self._persist_private_media_grant(restored)
        return {
            "schema_version": PRIVATE_MEDIA_GRANT_SCHEMA,
            "status": "migrated",
            "project_id": str(project_id),
            "snapshot_id": str(snapshot_id),
            "hash_job_id": str(hash_job_id),
            "file_name": file_name,
            "size_bytes": size_bytes,
            "source_sha256": source_sha256,
            "path_exposed": False,
        }

    def capability_snapshot(self, profile: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            raw_project_id = profile.get("project_id")
            project_id = (
                UUID(int=0)
                if (
                    raw_project_id is None
                    and self._project_model_resolver is None
                )
                else UUID(str(raw_project_id))
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "pipeline profile project identity is invalid"
            ) from error
        promoted_model_binding = capture_promoted_model_job_binding(
            project_id,
            self._project_model_resolver,
        )
        vision = dict(self._vision_status())
        ocr_runtime = dict(self._ocr_status())
        command_status = (
            dict(self._command_proposal_generator.status())
            if self._command_proposal_generator is not None
            else {
                "state": "unavailable",
                "error_code": "model_pack_not_configured",
            }
        )
        sam2_runtime = dict(self._sam2_status())
        raw_sam2_manifest = (
            sam2_runtime.get("model_pack")
            or sam2_runtime.get("model_manifest")
        )
        sam2_manifest = (
            dict(raw_sam2_manifest)
            if isinstance(raw_sam2_manifest, Mapping)
            else {}
        )
        capabilities = set(vision.get("capabilities") or ())
        ocr_capabilities = set(
            ocr_runtime.get("capabilities") or ()
        )
        installed_pack_id = vision.get("pack_id")
        installed_ocr_pack_id = ocr_runtime.get("pack_id")
        detector_capability_ready = bool(
            capabilities.intersection(
                {"defect_detection", "detection"}
            )
        )
        detector_matches = (
            isinstance(installed_pack_id, str)
            and installed_pack_id == profile["detector_pack_id"]
        )
        ocr_matches = (
            self._ocr_inference_service is not None
            and ocr_runtime.get("enabled") is True
            and isinstance(installed_ocr_pack_id, str)
            and installed_ocr_pack_id == profile["ocr_pack_id"]
            and "ocr" in ocr_capabilities
        )
        sam2_installed_pack_id = sam2_manifest.get("pack_id")
        sam2_matches = (
            sam2_runtime.get("state") == "ready"
            and isinstance(sam2_installed_pack_id, str)
            and sam2_installed_pack_id == profile["sam2_pack_id"]
            and self._sam2_adapter is not None
            and callable(getattr(self._sam2_adapter, "track", None))
        )
        dino_review_ready = bool(
            command_status.get("state") == "ready"
            and command_status.get("review_only") is True
            and command_status.get("bulk_frame_detection_provider")
            == "local_model"
            and self._analysis_jobs is not None
            and self._experimental_proposals is not None
        )
        dense_discovery_ready = bool(
            self._dense_candidate_discovery is not None
            and self._candidate_evidence_overlays is not None
            and self._analysis_jobs is not None
        )
        provider_visual_evidence_status: dict[str, Any]
        if self._provider_visual_evidence is None:
            provider_visual_evidence_status = {
                "state": "unavailable",
                "error_code": "provider_visual_evidence_unavailable",
                "coverage_guaranteed": False,
                "human_review_required": True,
            }
        else:
            try:
                provider_visual_evidence_status = dict(
                    self._provider_visual_evidence.runtime_status()
                )
            except Exception:
                provider_visual_evidence_status = {
                    "state": "unavailable",
                    "error_code": (
                        "provider_visual_evidence_status_failed"
                    ),
                    "coverage_guaranteed": False,
                    "human_review_required": True,
                }
        report_provider = str(profile.get("report_provider", "none"))
        report_provider_state = "disabled"
        report_provider_ready = False
        structured_provider_ready = False
        report_provider_capabilities: list[str] = []
        if (
            report_provider in {"codex", "claude"}
            and self._report_provider_status is not None
        ):
            try:
                status = self._report_provider_status(report_provider)
                report_provider_state = str(
                    getattr(status, "state", "")
                    or (
                        status.get("state")
                        if isinstance(status, Mapping)
                        else ""
                    )
                )
                raw_capabilities = (
                    getattr(status, "capabilities", ())
                    if not isinstance(status, Mapping)
                    else status.get("capabilities", ())
                )
                if isinstance(raw_capabilities, (list, tuple)):
                    report_provider_capabilities = [
                        str(item) for item in raw_capabilities
                    ]
                report_provider_ready = (
                    report_provider_state == "ready"
                    and {
                        "report_draft",
                        "document_edit_plan",
                    }.issubset(set(report_provider_capabilities))
                )
                structured_provider_ready = (
                    report_provider_state == "ready"
                    and "structured_judgment"
                    in set(report_provider_capabilities)
                    and self._structured_vision_judgment is not None
                )
            except Exception:
                report_provider_state = "unavailable"
        semantic_map_ready = False
        semantic_map_sha256 = profile.get(
            "template_semantic_map_sha256"
        )
        semantic_map_id = profile.get("template_semantic_map_id")
        if (
            profile.get("template_semantic_map_ready") is True
            and isinstance(semantic_map_sha256, str)
            and _SHA256.fullmatch(semantic_map_sha256) is not None
            and isinstance(semantic_map_id, str)
            and self._report_semantic_maps is not None
        ):
            try:
                resolved_map = self._report_semantic_maps.resolve(
                    template_id=str(
                        profile["default_template_id"]
                    ),
                    template_sha256=str(
                        profile["default_template_sha256"]
                    ),
                    semantic_map_id=semantic_map_id,
                )
                semantic_map_ready = (
                    resolved_map.semantic_map_sha256
                    == semantic_map_sha256
                )
            except Exception:
                semantic_map_ready = False
        provider_image_transport_ready = (
            report_provider == "claude"
            or (
                report_provider == "codex"
                and provider_visual_evidence_status.get("state")
                == "ready"
            )
        )
        return {
            "intake": "ready",
            "sha256": "ready",
            "dense_index_2fps": "ready",
            "dense_staging": self.dense_staging_status(),
            "candidate_rescan_5_10fps": (
                "ready"
                if (
                    (
                        detector_matches
                        and detector_capability_ready
                        and (
                            self._analysis_jobs is not None
                            or "candidate_rescan_5_10fps"
                            in capabilities
                        )
                    )
                    or (
                        ocr_matches
                        and (
                            self._analysis_jobs is not None
                            or "candidate_rescan_5_10fps"
                            in ocr_capabilities
                        )
                    )
                    or dino_review_ready
                    or dense_discovery_ready
                )
                else "missing"
            ),
            "dense_candidate_discovery": (
                "ready" if dense_discovery_ready else "missing"
            ),
            "detector": (
                "ready"
                if detector_matches and detector_capability_ready
                else "missing"
            ),
            "ocr": (
                "ready" if ocr_matches else "missing"
            ),
            "sam2_segmentation_tracking": (
                "ready" if sam2_matches else "missing"
            ),
            "similar_case_search": (
                "ready"
                if self._weak_case_visual_index is not None
                else "missing"
            ),
            "vision_proposal": (
                "ready"
                if (
                    structured_provider_ready
                    and provider_image_transport_ready
                    and profile.get(
                        "report_transmission_consent"
                    )
                    is True
                )
                else "missing"
            ),
            "dino_review_only_boxes": (
                "ready" if dino_review_ready else "missing"
            ),
            "dino_review_only_pack_id": command_status.get("pack_id"),
            "dino_review_only_pack_version": command_status.get(
                "pack_version"
            ),
            "dino_review_only_manifest_sha256": command_status.get(
                "manifest_sha256"
            ),
            "dino_review_only_error_code": command_status.get("error_code"),
            "selected_report_provider": report_provider,
            "report_provider_state": report_provider_state,
            "report_provider_ready": report_provider_ready,
            "structured_provider_ready": (
                structured_provider_ready
            ),
            "structured_provider_comparison_mode": (
                STRUCTURED_VISION_COMPARISON_MODE
            ),
            "provider_visual_evidence_state": (
                provider_visual_evidence_status.get("state")
            ),
            "provider_visual_evidence_ready": (
                provider_visual_evidence_status.get("state") == "ready"
            ),
            "provider_visual_evidence_error_code": (
                provider_visual_evidence_status.get("error_code")
            ),
            "provider_visual_evidence_human_review_required": True,
            "provider_visual_evidence_automatic_verification": False,
            "provider_visual_evidence_coverage_guaranteed": False,
            "selected_provider_image_transport": (
                "metadata_only"
                if report_provider == "claude"
                else "verified_minimized_attachments"
                if report_provider == "codex"
                else "disabled"
            ),
            "report_provider_capabilities": (
                report_provider_capabilities
            ),
            "report_transmission_consent": bool(
                profile.get("report_transmission_consent", False)
            ),
            "template_semantic_map_id": semantic_map_id,
            "template_semantic_map_sha256": semantic_map_sha256,
            "template_semantic_map_ready": semantic_map_ready,
            "bulk_frame_provider": "local_models_only",
            "installed_vision_pack_id": installed_pack_id,
            "installed_detector_pack_id": installed_pack_id,
            "installed_detector_pack_version": vision.get(
                "pack_version"
            ),
            "detector_model_manifest_sha256": vision.get(
                "manifest_sha256"
            ),
            "detector_model_warning_code": vision.get(
                "warning_code"
            ),
            "installed_ocr_pack_id": installed_ocr_pack_id,
            "installed_ocr_pack_version": ocr_runtime.get(
                "pack_version"
            ),
            "ocr_model_manifest_sha256": ocr_runtime.get(
                "manifest_sha256"
            ),
            "ocr_model_warning_code": ocr_runtime.get(
                "warning_code"
            ),
            "requested_detector_pack_id": profile["detector_pack_id"],
            "requested_ocr_pack_id": profile["ocr_pack_id"],
            "requested_sam2_pack_id": profile["sam2_pack_id"],
            "installed_sam2_pack_id": sam2_installed_pack_id,
            "sam2_model_manifest_sha256": sam2_manifest.get(
                "manifest_sha256"
            ),
            "sam2_model_error_code": sam2_runtime.get(
                "error_code"
            ),
            "model_pack_warning_code": vision.get("warning_code"),
            "promoted_project_model_binding": (
                promoted_model_binding
            ),
        }

    def execute(
        self,
        stage: str,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        project_id = UUID(str(context["project_id"]))
        snapshot_id = UUID(str(context["snapshot_id"]))
        file_name = str(context["file_name"])
        checkpoints = context.get("checkpoints", {})
        if stage == "intake_validation":
            recovered_after_restart = False
            try:
                probe = self._media_intake.probe(
                    project_id, snapshot_id, file_name, 15
                )
            except HTTPException as error:
                if error.status_code != 404:
                    raise PipelineStageFailure(
                        "media_snapshot_revalidation_failed"
                    ) from error
                if not isinstance(
                    context.get("input_restart_binding"),
                    Mapping,
                ):
                    raise PipelineStageFailure(
                        "media_snapshot_restart_binding_missing"
                    ) from error
                self._restore_restartable_input(context)
                recovered_after_restart = True
                try:
                    probe = self._media_intake.probe(
                        project_id,
                        snapshot_id,
                        file_name,
                        15,
                    )
                except HTTPException as retry_error:
                    raise PipelineStageFailure(
                        "media_snapshot_revalidation_failed"
                    ) from retry_error
            return PipelineStageResult(
                "success",
                checkpoint={
                    "snapshot_digest": probe["snapshot_digest"],
                    "duration_seconds": probe["duration_seconds"],
                    "size_bytes": probe["size_bytes"],
                    "ffprobe_status": probe["ffprobe_status"],
                    "server_snapshot_recovered": (
                        recovered_after_restart
                    ),
                    "input_restart_binding_sha256": (
                        context.get(
                            "input_restart_binding",
                            {},
                        ).get("binding_sha256")
                        if isinstance(
                            context.get("input_restart_binding"),
                            Mapping,
                        )
                        else None
                    ),
                    "path_exposed": False,
                },
                metrics={
                    "duration_seconds": probe["duration_seconds"],
                    "video_stream_count": len(probe["video_streams"]),
                    "server_snapshot_recovered": (
                        recovered_after_restart
                    ),
                },
            )
        if stage == "sha256":
            return self._hash_stage(
                project_id, snapshot_id, file_name, context, cancel
            )
        if stage == "metadata_gps_preflight":
            # Current metadata probe can expose at most a capture-start
            # coordinate. That is explicitly insufficient for a route.
            return PipelineStageResult(
                "blocked_gpx_required",
                checkpoint={
                    "timed_track_status": "missing",
                    "capture_start_coordinate_used_as_route": False,
                },
                metrics={"route_point_count": 0},
                error_code="gpx_required",
                non_blocking=True,
            )
        if stage == "dense_index_2fps":
            sha = checkpoints.get("sha256", {})
            digest = sha.get("source_sha256")
            if (
                not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
            ):
                raise PipelineStageFailure("hash_checkpoint_missing")
            storage_capacity_revision = None
            intake = checkpoints.get("intake_validation", {})
            duration_seconds = (
                intake.get("duration_seconds")
                if isinstance(intake, Mapping)
                else None
            )
            if isinstance(context.get("storage_plan"), Mapping):
                try:
                    (
                        limits,
                        exact_dense_plan,
                        writer_capacity_binding,
                    ) = self._job_scoped_dense_writer_capacity(
                        context,
                        duration_seconds=duration_seconds,
                        source_sha256=digest,
                    )
                except PipelineStageFailure as error:
                    raise PipelineStageFailure(
                        "storage_capacity_replan_invalid"
                    ) from error
                storage_capacity_revision = (
                    self._storage_capacity_revision(
                        context,
                        stage="dense_index_2fps",
                        required_bytes=(
                            exact_dense_plan
                            .planned_output_bytes_upper_bound
                        ),
                        basis={
                            "basis_type": (
                                "persisted_intake_duration_dense_writer_bound"
                            ),
                            "dense_index": (
                                exact_dense_plan.capacity_revision()
                            ),
                            "job_scoped_writer": (
                                writer_capacity_binding
                            ),
                        },
                    )
                )
            else:
                limits = DenseFrameIndexLimits(sampling_fps=2)
                exact_dense_plan = None
                writer_capacity_binding = None
            (
                prospective_dense_project_root,
                prospective_output_root,
            ) = self._prospective_pipeline_artifact_directory(
                project_id,
                context,
                "dense",
            )
            needs_short_staging = (
                dense_frame_index_requires_short_staging(
                    prospective_output_root
                )
            )
            staging_status = self.dense_staging_status()
            if needs_short_staging:
                same_volume_publish = (
                    self._dense_staging_root is not None
                    and same_storage_volume(
                        self._dense_staging_root,
                        prospective_dense_project_root,
                    )
                )
                required_staging_free = (
                    (
                        2
                        if same_volume_publish
                        else 1
                    )
                    * limits.max_total_output_bytes
                    + limits.min_free_disk_bytes
                )
                if (
                    staging_status.get("state") != "ready"
                    or int(
                        staging_status.get("available_free_bytes", 0)
                    )
                    < required_staging_free
                ):
                    raise PipelineStageFailure(
                        "dense_staging_storage_blocked",
                        observation={
                            "schema_version": (
                                "trafficvisson.dense-staging-observation.v1"
                            ),
                            "state": "insufficient_or_unavailable",
                            "source": staging_status.get(
                                "source",
                                "bridge_data_fallback",
                            ),
                            "required_free_bytes": required_staging_free,
                            "available_free_bytes": int(
                                staging_status.get(
                                    "available_free_bytes",
                                    0,
                                )
                            ),
                            "writer_capacity_bytes": (
                                limits.max_total_output_bytes
                            ),
                            "writer_capacity_binding": (
                                writer_capacity_binding
                            ),
                            "same_volume_atomic_publish": (
                                same_volume_publish
                            ),
                            "error_code": (
                                staging_status.get("error_code")
                                or "dense_staging_storage_blocked"
                            ),
                            "path_exposed": False,
                        },
                    )
            grant = self._resolve_pipeline_media_grant(
                project_id=project_id,
                snapshot_id=snapshot_id,
                file_name=file_name,
                sha_checkpoint=sha,
                cancel=cancel,
                expected_source_sha256=digest,
            )
            (
                dense_project_root,
                output_root,
            ) = self._pipeline_artifact_directory(
                project_id,
                context,
                "dense",
            )
            engine = DenseFrameIndexEngine(
                ffmpeg_path=self._ffmpeg_path,
                ffprobe_path=self._ffprobe_path,
                limits=limits,
                staging_root=self._dense_staging_root,
                staging_owner_id=str(context["job_id"]),
            )
            try:
                result = engine.build(
                    grant.media_path,
                    source_sha256=digest,
                    project_root=dense_project_root,
                    output_directory=output_root,
                    cancel=cancel,
                )
            except DenseFrameIndexCancelled:
                return PipelineStageResult(
                    "cancelled", error_code="dense_index_cancelled"
                )
            except DenseFrameIndexSourceChanged as error:
                raise PipelineStageFailure("source_changed") from error
            except DenseFrameIndexTimeout as error:
                raise PipelineStageFailure("dense_index_timeout") from error
            except DenseFrameIndexOutputLimit as error:
                raise PipelineStageFailure("dense_index_output_limit") from error
            except DenseFrameIndexStagingUnavailable as error:
                raise PipelineStageFailure(
                    "dense_staging_storage_blocked",
                    observation={
                        "schema_version": (
                            "trafficvisson.dense-staging-observation.v1"
                        ),
                        "state": "writer_preflight_rejected",
                        "source": self._dense_staging_source,
                        "required_free_bytes": (
                            (
                                2
                                if (
                                    self._dense_staging_root
                                    is not None
                                    and same_storage_volume(
                                        self._dense_staging_root,
                                        dense_project_root,
                                    )
                                )
                                else 1
                            )
                            * limits.max_total_output_bytes
                            + limits.min_free_disk_bytes
                        ),
                        "available_free_bytes": int(
                            self.dense_staging_status().get(
                                "available_free_bytes",
                                0,
                            )
                        ),
                        "writer_capacity_bytes": (
                            limits.max_total_output_bytes
                        ),
                        "writer_capacity_binding": (
                            writer_capacity_binding
                        ),
                        "same_volume_atomic_publish": (
                            self._dense_staging_root is not None
                            and same_storage_volume(
                                self._dense_staging_root,
                                dense_project_root,
                            )
                        ),
                        "error_code": "dense_staging_storage_blocked",
                        "path_exposed": False,
                    },
                ) from error
            except DenseFrameIndexError as error:
                raise PipelineStageFailure("dense_index_failed") from error
            run_id = Path(result.output_directory).name
            dense_index_path = (
                Path(result.output_directory)
                / result.index_relative_path
            )
            dense_index_sha256 = _sha256_regular_file(
                dense_index_path,
                maximum_bytes=512 * 1024 * 1024,
            )
            if self._dense_index_registration is not None:
                try:
                    self._dense_index_registration(
                        project_id,
                        digest,
                        dense_index_sha256,
                        dense_index_path,
                        UUID(str(context["job_id"])),
                        (
                            context["artifact_root"]
                            if isinstance(
                                context.get("artifact_root"),
                                Mapping,
                            )
                            else None
                        ),
                    )
                except Exception as error:
                    raise PipelineStageFailure(
                        "dense_index_registration_failed"
                    ) from error
            return PipelineStageResult(
                "success",
                checkpoint={
                    "dense_run_id": run_id,
                    "source_sha256": digest,
                    "dense_index_sha256": dense_index_sha256,
                    "sampling_fps": 2,
                    "storage_capacity_revision": (
                        storage_capacity_revision
                    ),
                    "writer_capacity_binding": (
                        writer_capacity_binding
                    ),
                    "dense_staging": {
                        "required": needs_short_staging,
                        "source": self._dense_staging_source,
                        "path_exposed": False,
                    },
                    "frame_index_artifact": (
                        f"pipeline-dense:{context['job_id']}:frame-index"
                    ),
                },
                metrics={
                    "frame_count": len(result.frames),
                    "indexed_duration_seconds": (
                        result.provenance.indexed_duration_seconds
                    ),
                    "truncated": result.provenance.truncated,
                },
            )
        if stage == "candidate_rescan_5_10fps":
            if (
                self._analysis_jobs is None
                or (
                    self._dense_candidate_discovery is None
                    and
                    context["capabilities"].get("detector")
                    != "ready"
                    and context["capabilities"].get("ocr") != "ready"
                    and context["capabilities"].get(
                        "dino_review_only_boxes"
                    )
                    != "ready"
                )
            ):
                return PipelineStageResult(
                    "model_unavailable",
                    error_code="candidate_rescan_model_unavailable",
                )
            return self._prepare_candidate_window_rescan(
                project_id,
                snapshot_id,
                file_name,
                context,
                cancel,
            )
        if stage == "detection_ocr":
            return self._generate_dino_review_proposals(
                project_id, context, cancel
            )
        if stage == "sam2_segmentation_tracking":
            return self._generate_sam2_review_proposals(
                project_id,
                context,
                cancel,
            )
        if stage == "temporal_merge":
            return self._merge_dino_review_proposals(
                project_id, context
            )
        if stage == "similar_case_search":
            return self._search_weak_reference_cases(context)
        if stage == "vision_proposal":
            return self._generate_structured_vision_proposals(
                project_id,
                context,
                cancel,
            )
        if stage == "question_queue":
            return self._prepare_question_queue(context)
        if stage == "derived_outputs":
            return self._generate_derived_outputs(context, cancel)
        raise PipelineStageFailure("unknown_pipeline_stage")

    @staticmethod
    def _analysis_seed_rescan_rows(
        candidates: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise PipelineStageFailure(
                    "analysis_seed_candidate_invalid"
                )
            candidate_id = candidate.get("candidate_id")
            representative = candidate.get(
                "representative_timestamp_seconds"
            )
            start = candidate.get("start_seconds")
            end = candidate.get("end_seconds")
            if (
                not isinstance(candidate_id, str)
                or re.fullmatch(r"[a-f0-9]{20}", candidate_id) is None
                or candidate_id in seen
                or isinstance(representative, bool)
                or not isinstance(representative, (int, float))
                or not math.isfinite(float(representative))
                or float(representative) < 0
                or (
                    start is not None
                    and (
                        isinstance(start, bool)
                        or not isinstance(start, (int, float))
                        or not math.isfinite(float(start))
                        or float(start) < 0
                    )
                )
                or (
                    end is not None
                    and (
                        isinstance(end, bool)
                        or not isinstance(end, (int, float))
                        or not math.isfinite(float(end))
                        or float(end) < 0
                    )
                )
            ):
                raise PipelineStageFailure(
                    "analysis_seed_candidate_invalid"
                )
            triggers = candidate.get("triggers", ())
            if (
                not isinstance(triggers, (list, tuple))
                or len(triggers) > 64
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value) > 128
                    for value in triggers
                )
            ):
                raise PipelineStageFailure(
                    "analysis_seed_candidate_invalid"
                )
            representative_image = candidate.get(
                "representative_image"
            )
            representative_image_sha256 = None
            if isinstance(representative_image, Mapping):
                representative_image_sha256 = (
                    representative_image.get("sha256")
                )
                if (
                    representative_image_sha256 is not None
                    and (
                        not isinstance(
                            representative_image_sha256, str
                        )
                        or _SHA256.fullmatch(
                            representative_image_sha256
                        )
                        is None
                    )
                ):
                    raise PipelineStageFailure(
                        "analysis_seed_candidate_invalid"
                    )
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "start_seconds": (
                        round(float(start), 6)
                        if start is not None
                        else None
                    ),
                    "end_seconds": (
                        round(float(end), 6)
                        if end is not None
                        else None
                    ),
                    "representative_timestamp_seconds": round(
                        float(representative), 6
                    ),
                    "scene_score": candidate.get("scene_score"),
                    "sharpness_score": candidate.get(
                        "sharpness_score"
                    ),
                    "triggers": list(triggers),
                    "representative_image_sha256": (
                        representative_image_sha256
                    ),
                }
            )
            seen.add(candidate_id)
        rows.sort(
            key=lambda row: (
                row["representative_timestamp_seconds"],
                row["candidate_id"],
            )
        )
        return rows

    @classmethod
    def _candidate_rescan_input_origins(
        cls,
        *,
        seed_candidates: Sequence[Mapping[str, Any]],
        dense_candidates: Sequence[Mapping[str, Any]],
        dense_discovery_revision: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(dense_discovery_revision, str)
            or _SHA256.fullmatch(dense_discovery_revision) is None
        ):
            raise PipelineStageFailure(
                "dense_candidate_discovery_invalid"
            )
        seed_rows = cls._analysis_seed_rescan_rows(
            seed_candidates
        )
        dense_ids = sorted(
            str(candidate.get("candidate_id"))
            for candidate in dense_candidates
            if isinstance(candidate, Mapping)
        )
        if (
            len(dense_ids) != len(dense_candidates)
            or len(set(dense_ids)) != len(dense_ids)
            or any(
                re.fullmatch(r"[a-f0-9]{20}", candidate_id)
                is None
                for candidate_id in dense_ids
            )
        ):
            raise PipelineStageFailure(
                "dense_candidate_discovery_invalid"
            )
        seed_ids = [
            str(row["candidate_id"]) for row in seed_rows
        ]
        if set(seed_ids).intersection(dense_ids):
            raise PipelineStageFailure(
                "candidate_rescan_identity_collision"
            )
        payload = {
            "schema_version": (
                _CANDIDATE_RESCAN_INPUT_ORIGINS_SCHEMA
            ),
            "analysis_seed": {
                "candidate_count": len(seed_rows),
                "candidate_ids": seed_ids,
                "candidate_rows_sha256": hashlib.sha256(
                    _canonical_json(seed_rows)
                ).hexdigest(),
                "origin": (
                    "bounded_scene_change_and_cadence_extraction"
                ),
                "non_classifying": True,
                "automatic_annotation_creation": False,
                "automatic_incident_creation": False,
            },
            "dense_provider": {
                "candidate_count": len(dense_ids),
                "candidate_ids": dense_ids,
                "dense_discovery_revision": (
                    dense_discovery_revision
                ),
                "origin": "full_dense_2fps_local_provider_scan",
                "review_only": True,
                "automatic_annotation_creation": False,
                "automatic_incident_creation": False,
            },
            "combined_candidate_ids": sorted(
                seed_ids + dense_ids
            ),
            "combined_candidate_count": (
                len(seed_ids) + len(dense_ids)
            ),
            "temporal_overlap_policy": (
                "candidate_window_rescan_deterministic_merge_v1"
            ),
        }
        return {
            **payload,
            "origin_revision": hashlib.sha256(
                _canonical_json(payload)
            ).hexdigest(),
        }

    def _prepare_candidate_window_rescan(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        """Discover candidates across the full 2 FPS index, then rescan them.

        The completed ``AnalysisJob`` remains the immutable source identity,
        but it is no longer allowed to stand in for the dense scan.  When the
        dense discovery and overlay stores are configured, every indexed
        frame is evaluated by the local providers and the resulting
        review-only candidates are materialized in a separate immutable
        evidence set.  Legacy/direct callers without those stores retain the
        explicitly degraded scene-candidate path.
        """

        seed_result = self._prepare_degraded_review_candidates(
            project_id,
            snapshot_id,
            file_name,
            context,
            cancel,
        )
        if seed_result.outcome not in {"success", "zero_detections"}:
            return seed_result
        dense_checkpoint = context.get("checkpoints", {}).get(
            "dense_index_2fps"
        )
        if not isinstance(dense_checkpoint, Mapping):
            return seed_result
        if (
            self._dense_candidate_discovery is None
            or self._candidate_evidence_overlays is None
        ):
            return self._prepare_legacy_candidate_window_rescan(
                project_id=project_id,
                snapshot_id=snapshot_id,
                file_name=file_name,
                context=context,
                cancel=cancel,
                seed_result=seed_result,
            )
        dense_run_id = dense_checkpoint.get("dense_run_id")
        dense_source_sha256 = dense_checkpoint.get("source_sha256")
        dense_index_sha256 = dense_checkpoint.get(
            "dense_index_sha256"
        )
        if (
            not isinstance(dense_run_id, str)
            or re.fullmatch(
                r"dense-frame-index-[A-Za-z0-9._-]{6,128}",
                dense_run_id,
            )
            is None
            or not isinstance(dense_source_sha256, str)
            or _SHA256.fullmatch(dense_source_sha256) is None
            or not isinstance(dense_index_sha256, str)
            or _SHA256.fullmatch(dense_index_sha256) is None
        ):
            raise PipelineStageFailure(
                "dense_index_checkpoint_invalid"
            )
        source_sha256 = seed_result.checkpoint.get("source_sha256")
        analysis_job_id = seed_result.checkpoint.get(
            "analysis_job_id"
        )
        if (
            source_sha256 != dense_source_sha256
            or not isinstance(analysis_job_id, str)
        ):
            raise PipelineStageFailure(
                "dense_index_checkpoint_invalid"
            )
        try:
            survey_analysis_job_id = UUID(analysis_job_id)
        except (ValueError, AttributeError):
            raise PipelineStageFailure(
                "dense_index_checkpoint_invalid"
            ) from None
        sha_checkpoint = context.get("checkpoints", {}).get(
            "sha256", {}
        )
        grant = self._resolve_pipeline_media_grant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            file_name=file_name,
            sha_checkpoint=sha_checkpoint,
            cancel=cancel,
            expected_source_sha256=source_sha256,
        )
        (
            project_pipeline_root,
            dense_output_root,
        ) = self._pipeline_artifact_directory(
            project_id,
            context,
            "dense",
        )
        dense_index_path = (
            dense_output_root
            / dense_run_id
            / "frame-index.json"
        )
        if (
            _sha256_regular_file(
                dense_index_path,
                maximum_bytes=512 * 1024 * 1024,
            )
            != dense_index_sha256
        ):
            raise PipelineStageFailure(
                "dense_index_checkpoint_invalid"
            )
        if self._dense_index_registration is not None:
            try:
                self._dense_index_registration(
                    project_id,
                    source_sha256,
                    dense_index_sha256,
                    dense_index_path,
                    UUID(str(context["job_id"])),
                    (
                        context["artifact_root"]
                        if isinstance(
                            context.get("artifact_root"),
                            Mapping,
                        )
                        else None
                    ),
                )
            except Exception as error:
                raise PipelineStageFailure(
                    "dense_index_registration_failed"
                ) from error
        try:
            raw_promoted_model_binding = context[
                "capabilities"
            ].get("promoted_project_model_binding")
            expected_promoted_model_binding = None
            if self._project_model_resolver is not None:
                if (
                    not isinstance(
                        raw_promoted_model_binding,
                        Mapping,
                    )
                    or raw_promoted_model_binding.get(
                        "schema_version"
                    )
                    != PROMOTED_MODEL_JOB_BINDING_SCHEMA
                ):
                    raise PipelineStageFailure(
                        "promoted_project_model_binding_missing"
                    )
                expected_promoted_model_binding = dict(
                    raw_promoted_model_binding
                )
            discovery = self._dense_candidate_discovery.discover(
                project_id=project_id,
                analysis_job_id=survey_analysis_job_id,
                expected_promoted_model_binding=(
                    expected_promoted_model_binding
                ),
                source_sha256=source_sha256,
                dense_index_path=dense_index_path,
                dense_index_sha256=dense_index_sha256,
                cancel=cancel,
            )
        except DenseCandidateDiscoveryCancelled:
            return PipelineStageResult(
                "cancelled",
                error_code="dense_candidate_discovery_cancelled",
            )
        except DenseCandidateDiscoveryIntegrityError as error:
            raise PipelineStageFailure(
                "dense_candidate_discovery_integrity_failed"
            ) from error
        except DenseCandidateDiscoveryError as error:
            raise PipelineStageFailure(
                "dense_candidate_discovery_failed"
            ) from error
        discovery_checkpoint = discovery.to_checkpoint()
        dense_candidates = [
            asdict(candidate) for candidate in discovery.candidates
        ]
        try:
            seed_analysis_row = self._analysis_jobs.get(
                project_id,
                survey_analysis_job_id,
            )
        except HTTPException as error:
            raise PipelineStageFailure(
                "analysis_checkpoint_missing"
            ) from error
        seed_analysis_result = seed_analysis_row.get("result")
        seed_candidates = (
            seed_analysis_result.get("candidates")
            if isinstance(seed_analysis_result, Mapping)
            else None
        )
        if (
            seed_analysis_row.get("status") != "completed"
            or seed_analysis_row.get("source_sha256")
            != source_sha256
            or not isinstance(seed_candidates, list)
        ):
            raise PipelineStageFailure(
                "analysis_checkpoint_invalid"
            )
        input_origins = self._candidate_rescan_input_origins(
            seed_candidates=seed_candidates,
            dense_candidates=dense_candidates,
            dense_discovery_revision=(
                discovery.discovery_revision
            ),
        )
        candidates = sorted(
            [
                *(dict(candidate) for candidate in seed_candidates),
                *(
                    dict(candidate)
                    for candidate in dense_candidates
                ),
            ],
            key=lambda candidate: (
                float(
                    candidate[
                        "representative_timestamp_seconds"
                    ]
                ),
                str(candidate["candidate_id"]),
            ),
        )
        if not candidates:
            return PipelineStageResult(
                "zero_detections",
                checkpoint={
                    **dict(seed_result.checkpoint),
                    "candidate_count": 0,
                    "analysis_seed_candidate_count": 0,
                    "dense_candidate_count": 0,
                    "candidate_rescan_input_origins": (
                        input_origins
                    ),
                    "dense_candidate_discovery": (
                        discovery_checkpoint
                    ),
                    "dense_candidate_discovery_revision": (
                        discovery.discovery_revision
                    ),
                    "dense_index_sha256": dense_index_sha256,
                    "scanned_dense_frame_count": (
                        discovery.scanned_frame_count
                    ),
                    "actual_candidate_method": (
                        "full_dense_2fps_local_provider_scan"
                    ),
                    "high_frequency_rescan_performed": False,
                    "degraded_review_mode": False,
                    "review_only": True,
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                metrics={
                    "candidate_count": 0,
                    "analysis_seed_candidate_count": 0,
                    "dense_candidate_count": 0,
                    "scanned_dense_frame_count": (
                        discovery.scanned_frame_count
                    ),
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                error_code="dense_candidate_zero_detections",
            )
        (
            rescan_project_root,
            output_directory,
        ) = self._pipeline_artifact_directory(
            project_id,
            context,
            "candidate-window-rescans",
        )
        requested_fps = context["profile"]["candidate_rescan_fps"]
        rescan_engine = CandidateWindowRescanEngine(
            ffmpeg_path=self._ffmpeg_path,
            limits=CandidateWindowRescanLimits(
                sampling_fps=requested_fps
            ),
        )
        storage_capacity_revision = None
        try:
            plan_rescan = getattr(rescan_engine, "plan", None)
            if callable(plan_rescan):
                exact_rescan_plan = plan_rescan(
                    source_sha256=source_sha256,
                    dense_index_path=dense_index_path,
                    candidates=candidates,
                )
                candidate_evidence_capacity = (
                    self._candidate_evidence_capacity_upper_bound(
                        len(dense_candidates)
                    )
                    if dense_candidates
                    else 0
                )
                storage_capacity_revision = (
                    self._storage_capacity_revision(
                        context,
                        stage="candidate_rescan_5_10fps",
                        required_bytes=(
                            exact_rescan_plan
                            .planned_output_bytes_upper_bound
                            + _REPLAN_STAGE_METADATA_BYTES
                            + candidate_evidence_capacity
                        ),
                        basis={
                            "basis_type": (
                                "actual_merged_candidate_windows"
                            ),
                            "candidate_count": len(candidates),
                            "analysis_seed_candidate_count": len(
                                seed_candidates
                            ),
                            "dense_candidate_count": len(
                                dense_candidates
                            ),
                            "candidate_evidence_capacity_bytes": (
                                candidate_evidence_capacity
                            ),
                            "candidate_rescan": (
                                exact_rescan_plan.capacity_revision()
                            ),
                        },
                    )
                )
            result = rescan_engine.build(
                grant.media_path,
                source_sha256=source_sha256,
                dense_index_path=dense_index_path,
                candidates=candidates,
                project_root=rescan_project_root,
                output_directory=output_directory,
                cancel=cancel,
            )
        except CandidateWindowRescanCancelled:
            return PipelineStageResult(
                "cancelled",
                error_code="candidate_rescan_cancelled",
            )
        except CandidateWindowRescanSourceChanged as error:
            raise PipelineStageFailure("source_changed") from error
        except CandidateWindowRescanTimeout as error:
            raise PipelineStageFailure(
                "candidate_rescan_timeout"
            ) from error
        except CandidateWindowRescanOutputLimit as error:
            raise PipelineStageFailure(
                "candidate_rescan_output_limit"
            ) from error
        except CandidateWindowRescanManifestInvalid as error:
            raise PipelineStageFailure(
                "candidate_rescan_manifest_invalid"
            ) from error
        except (CandidateWindowRescanError, HTTPException) as error:
            raise PipelineStageFailure(
                "candidate_rescan_failed"
            ) from error
        frame_count = len(result.frames)
        if frame_count <= 0:
            raise PipelineStageFailure(
                "candidate_rescan_empty_output"
            )
        try:
            visual_quality = (
                evaluate_candidate_window_visual_quality(
                    result,
                    source_sha256=source_sha256,
                    semantic_observer=(
                        self._visual_evidence_semantic_observer
                    ),
                )
            )
        except VisualEvidenceQualityError as error:
            raise PipelineStageFailure(
                "visual_evidence_quality_failed"
            ) from error
        visual_quality_checkpoint = (
            visual_quality.to_checkpoint()
        )
        if (
            visual_quality.state
            == "unusable_visual_evidence"
        ):
            unusable_checkpoint = {
                **dict(seed_result.checkpoint),
                "candidate_count": len(candidates),
                "analysis_seed_candidate_count": len(
                    seed_candidates
                ),
                "dense_candidate_count": len(
                    dense_candidates
                ),
                "candidate_rescan_input_origins": input_origins,
                "dense_candidate_discovery": (
                    discovery_checkpoint
                ),
                "dense_candidate_discovery_revision": (
                    discovery.discovery_revision
                ),
                "dense_index_sha256": dense_index_sha256,
                "scanned_dense_frame_count": (
                    discovery.scanned_frame_count
                ),
                "requested_rescan_fps": requested_fps,
                "actual_rescan_fps": requested_fps,
                "high_frequency_rescan_performed": True,
                "actual_candidate_method": (
                    "analysis_seed_union_full_dense_2fps_then_"
                    "candidate_window_rescan"
                ),
                "whole_video_high_frequency_scan": False,
                "rescan_schema_version": (
                    CANDIDATE_WINDOW_RESCAN_SCHEMA
                ),
                "rescan_run_id": Path(
                    result.output_directory
                ).name,
                "rescan_manifest_sha256": (
                    result.manifest_sha256
                ),
                "rescan_input_revision": result.input_revision,
                "storage_capacity_revision": (
                    storage_capacity_revision
                ),
                "rescan_manifest_artifact": (
                    f"pipeline-rescan:{context['job_id']}:manifest"
                ),
                "rescan_window_count": len(result.windows),
                "rescan_frame_count": frame_count,
                "downstream_evidence_count": len(
                    result.downstream_evidence_ids
                ),
                "visual_evidence_quality": (
                    visual_quality_checkpoint
                ),
                "visual_evidence_quality_state": (
                    "unusable_visual_evidence"
                ),
                "visual_evidence_quality_revision": (
                    visual_quality.quality_revision
                ),
                "quality_selected_evidence_count": 0,
                "road_truth_candidate_count": 0,
                "degraded_review_mode": False,
                "review_only": True,
                "automatic_annotation_creation": False,
                "automatic_incident_creation": False,
                "automatic_training_membership": False,
                "automatic_report_truth": False,
                "annotation_count": 0,
                "incident_count": 0,
            }
            return PipelineStageResult(
                "unusable_visual_evidence",
                checkpoint=unusable_checkpoint,
                metrics={
                    "candidate_count": len(candidates),
                    "rescan_window_count": len(
                        result.windows
                    ),
                    "rescan_frame_count": frame_count,
                    "quality_evaluated_frame_count": len(
                        visual_quality.frame_observations
                    ),
                    "unusable_window_count": len(
                        visual_quality.window_decisions
                    ),
                    "quality_selected_evidence_count": 0,
                    "road_truth_candidate_count": 0,
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                error_code="unusable_visual_evidence",
            )
        try:
            analysis_job_uuid = UUID(analysis_job_id)
        except ValueError as error:
            raise PipelineStageFailure(
                "analysis_checkpoint_missing"
            ) from error
        overlay = None
        if dense_candidates:
            try:
                _, candidate_evidence_root = (
                    self._pipeline_artifact_directory(
                        project_id,
                        context,
                        "candidate-evidence",
                    )
                )
                overlay_kwargs: dict[str, Any] = {
                    "project_id": project_id,
                    "analysis_job_id": analysis_job_uuid,
                    "pipeline_job_id": UUID(
                        str(context["job_id"])
                    ),
                    "discovery": discovery,
                    "cancel": cancel,
                }
                if isinstance(
                    self._candidate_evidence_overlays,
                    CandidateEvidenceOverlayStore,
                ):
                    overlay_kwargs["artifact_root"] = (
                        candidate_evidence_root
                    )
                overlay = (
                    self._candidate_evidence_overlays.materialize(
                        **overlay_kwargs,
                    )
                )
            except CandidateEvidenceOverlayCancelled:
                return PipelineStageResult(
                    "cancelled",
                    error_code=(
                        "candidate_evidence_overlay_cancelled"
                    ),
                )
            except CandidateEvidenceOverlayError as error:
                raise PipelineStageFailure(
                    "candidate_evidence_overlay_failed"
                ) from error
            if len(overlay.candidates) != len(dense_candidates):
                raise PipelineStageFailure(
                    "candidate_evidence_overlay_invalid"
                )
        if (
            overlay is not None
            and self._candidate_evidence_registry is not None
        ):
            binder = getattr(
                self._candidate_evidence_registry,
                "bind_overlay",
                None,
            )
            if not callable(binder):
                raise PipelineStageFailure(
                    "candidate_evidence_registry_invalid"
                )
            try:
                binder(
                    project_id,
                    analysis_job_uuid,
                    UUID(str(context["job_id"])),
                    overlay.overlay_id,
                )
            except Exception as error:
                raise PipelineStageFailure(
                    "candidate_evidence_registry_failed"
                ) from error
        run_id = Path(result.output_directory).name
        checkpoint = {
            **dict(seed_result.checkpoint),
            "candidate_count": len(candidates),
            "analysis_seed_candidate_count": len(
                seed_candidates
            ),
            "dense_candidate_count": len(dense_candidates),
            "candidate_rescan_input_origins": input_origins,
            "dense_candidate_discovery": discovery_checkpoint,
            "dense_candidate_discovery_revision": (
                discovery.discovery_revision
            ),
            "dense_index_sha256": dense_index_sha256,
            "scanned_dense_frame_count": (
                discovery.scanned_frame_count
            ),
            **(
                {
                    "candidate_evidence_overlay": (
                        overlay.to_checkpoint()
                    ),
                    "candidate_evidence_overlay_id": (
                        overlay.overlay_id
                    ),
                    "candidate_evidence_manifest_sha256": (
                        overlay.manifest_sha256
                    ),
                    "candidate_evidence_metadata_sha256": (
                        overlay.candidate_metadata_sha256
                    ),
                }
                if overlay is not None
                else {}
            ),
            "requested_rescan_fps": requested_fps,
            "actual_rescan_fps": requested_fps,
            "high_frequency_rescan_performed": True,
            "actual_candidate_method": (
                "analysis_seed_union_full_dense_2fps_then_"
                "candidate_window_rescan"
            ),
            "whole_video_high_frequency_scan": False,
            "rescan_schema_version": (
                CANDIDATE_WINDOW_RESCAN_SCHEMA
            ),
            "rescan_run_id": run_id,
            "rescan_manifest_sha256": result.manifest_sha256,
            "rescan_input_revision": result.input_revision,
            "storage_capacity_revision": (
                storage_capacity_revision
            ),
            "rescan_manifest_artifact": (
                f"pipeline-rescan:{context['job_id']}:manifest"
            ),
            "rescan_window_count": len(result.windows),
            "rescan_frame_count": frame_count,
            "downstream_evidence_count": len(
                result.downstream_evidence_ids
            ),
            "downstream_evidence_contract": (
                "candidate_window_manifest_selected_frames_v1"
            ),
            "visual_evidence_quality": (
                visual_quality_checkpoint
            ),
            "visual_evidence_quality_state": (
                visual_quality.state
            ),
            "visual_evidence_quality_revision": (
                visual_quality.quality_revision
            ),
            "quality_selected_evidence_count": len(
                visual_quality.selected_evidence_ids
            ),
            "quality_eligible_parent_candidate_count": len(
                visual_quality.eligible_parent_candidate_ids
            ),
            "quality_blocked_parent_candidate_count": len(
                visual_quality.blocked_parent_candidate_ids
            ),
            "degraded_review_mode": False,
            "review_only": True,
            "annotation_count": 0,
            "incident_count": 0,
        }
        return PipelineStageResult(
            "success",
            checkpoint=checkpoint,
            metrics={
                "candidate_count": len(candidates),
                "analysis_seed_candidate_count": len(
                    seed_candidates
                ),
                "dense_candidate_count": len(dense_candidates),
                "scanned_dense_frame_count": (
                    discovery.scanned_frame_count
                ),
                "requested_rescan_fps": requested_fps,
                "actual_rescan_fps": requested_fps,
                "high_frequency_rescan_performed": True,
                "rescan_window_count": len(result.windows),
                "rescan_frame_count": frame_count,
                "downstream_evidence_count": len(
                    result.downstream_evidence_ids
                ),
                "quality_evaluated_frame_count": len(
                    visual_quality.frame_observations
                ),
                "quality_selected_evidence_count": len(
                    visual_quality.selected_evidence_ids
                ),
                "quality_eligible_parent_candidate_count": len(
                    visual_quality.eligible_parent_candidate_ids
                ),
                "quality_blocked_parent_candidate_count": len(
                    visual_quality.blocked_parent_candidate_ids
                ),
                "cached": result.cached,
                "overlay_cached": (
                    overlay.idempotent_replay
                    if overlay is not None
                    else False
                ),
                "storage_capacity_revision_sha256": (
                    storage_capacity_revision.get(
                        "revision_sha256"
                    )
                    if isinstance(
                        storage_capacity_revision, Mapping
                    )
                    else None
                ),
                "annotation_count": 0,
                "incident_count": 0,
            },
        )

    def _prepare_legacy_candidate_window_rescan(
        self,
        *,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
        seed_result: PipelineStageResult,
    ) -> PipelineStageResult:
        """Preserve the pre-dense-provider rescan contract for old callers."""

        if seed_result.outcome != "success":
            return seed_result
        dense_checkpoint = context.get("checkpoints", {}).get(
            "dense_index_2fps"
        )
        if not isinstance(dense_checkpoint, Mapping):
            return seed_result
        dense_run_id = dense_checkpoint.get("dense_run_id")
        dense_source_sha256 = dense_checkpoint.get("source_sha256")
        source_sha256 = seed_result.checkpoint.get(
            "source_sha256"
        )
        analysis_job_id = seed_result.checkpoint.get(
            "analysis_job_id"
        )
        if (
            not isinstance(dense_run_id, str)
            or re.fullmatch(
                r"dense-frame-index-[A-Za-z0-9._-]{6,128}",
                dense_run_id,
            )
            is None
            or not isinstance(dense_source_sha256, str)
            or dense_source_sha256 != source_sha256
            or not isinstance(analysis_job_id, str)
        ):
            raise PipelineStageFailure(
                "dense_index_checkpoint_invalid"
            )
        _, _, candidates = self._analysis_job_for_context(
            project_id,
            {
                **context,
                "checkpoints": {
                    **dict(context.get("checkpoints", {})),
                    "candidate_rescan_5_10fps": dict(
                        seed_result.checkpoint
                    ),
                },
            },
        )
        if any(
            not isinstance(candidate, Mapping)
            or re.fullmatch(
                r"[a-f0-9]{20}",
                str(candidate.get("candidate_id", "")),
            )
            is None
            for candidate in candidates
        ):
            return seed_result
        sha_checkpoint = context.get("checkpoints", {}).get(
            "sha256", {}
        )
        grant = self._resolve_pipeline_media_grant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            file_name=file_name,
            sha_checkpoint=sha_checkpoint,
            cancel=cancel,
            expected_source_sha256=source_sha256,
        )
        (
            project_pipeline_root,
            dense_output_root,
        ) = self._pipeline_artifact_directory(
            project_id,
            context,
            "dense",
        )
        dense_index_path = (
            dense_output_root
            / dense_run_id
            / "frame-index.json"
        )
        (
            rescan_project_root,
            output_directory,
        ) = self._pipeline_artifact_directory(
            project_id,
            context,
            "candidate-window-rescans",
        )
        requested_fps = context["profile"][
            "candidate_rescan_fps"
        ]
        rescan_engine = CandidateWindowRescanEngine(
            ffmpeg_path=self._ffmpeg_path,
            limits=CandidateWindowRescanLimits(
                sampling_fps=requested_fps
            ),
        )
        storage_capacity_revision = None
        try:
            plan_rescan = getattr(rescan_engine, "plan", None)
            if callable(plan_rescan):
                exact_rescan_plan = plan_rescan(
                    source_sha256=source_sha256,
                    dense_index_path=dense_index_path,
                    candidates=candidates,
                )
                storage_capacity_revision = (
                    self._storage_capacity_revision(
                        context,
                        stage="candidate_rescan_5_10fps",
                        required_bytes=(
                            exact_rescan_plan
                            .planned_output_bytes_upper_bound
                            + _REPLAN_STAGE_METADATA_BYTES
                        ),
                        basis={
                            "basis_type": (
                                "actual_legacy_candidate_windows"
                            ),
                            "candidate_count": len(candidates),
                            "candidate_evidence_capacity_bytes": 0,
                            "candidate_rescan": (
                                exact_rescan_plan.capacity_revision()
                            ),
                        },
                    )
                )
            result = rescan_engine.build(
                grant.media_path,
                source_sha256=source_sha256,
                dense_index_path=dense_index_path,
                candidates=candidates,
                project_root=rescan_project_root,
                output_directory=output_directory,
                cancel=cancel,
            )
        except CandidateWindowRescanCancelled:
            return PipelineStageResult(
                "cancelled",
                error_code="candidate_rescan_cancelled",
            )
        except CandidateWindowRescanSourceChanged as error:
            raise PipelineStageFailure("source_changed") from error
        except CandidateWindowRescanTimeout as error:
            raise PipelineStageFailure(
                "candidate_rescan_timeout"
            ) from error
        except CandidateWindowRescanOutputLimit as error:
            raise PipelineStageFailure(
                "candidate_rescan_output_limit"
            ) from error
        except CandidateWindowRescanManifestInvalid as error:
            raise PipelineStageFailure(
                "candidate_rescan_manifest_invalid"
            ) from error
        except (CandidateWindowRescanError, HTTPException) as error:
            raise PipelineStageFailure(
                "candidate_rescan_failed"
            ) from error
        if not result.frames:
            raise PipelineStageFailure(
                "candidate_rescan_empty_output"
            )
        checkpoint = {
            **dict(seed_result.checkpoint),
            "requested_rescan_fps": requested_fps,
            "actual_rescan_fps": requested_fps,
            "high_frequency_rescan_performed": True,
            "actual_candidate_method": (
                "analysis_seeded_candidate_window_rescan"
            ),
            "whole_video_high_frequency_scan": False,
            "rescan_schema_version": (
                CANDIDATE_WINDOW_RESCAN_SCHEMA
            ),
            "rescan_run_id": Path(
                result.output_directory
            ).name,
            "rescan_manifest_sha256": result.manifest_sha256,
            "rescan_input_revision": result.input_revision,
            "storage_capacity_revision": (
                storage_capacity_revision
            ),
            "rescan_manifest_artifact": (
                f"pipeline-rescan:{context['job_id']}:manifest"
            ),
            "rescan_window_count": len(result.windows),
            "rescan_frame_count": len(result.frames),
            "downstream_evidence_count": len(
                result.downstream_evidence_ids
            ),
            "downstream_evidence_contract": (
                "candidate_window_manifest_selected_frames_v1"
            ),
            "degraded_review_mode": False,
            "review_only": True,
        }
        return PipelineStageResult(
            "success",
            checkpoint=checkpoint,
            metrics={
                **dict(seed_result.metrics),
                "requested_rescan_fps": requested_fps,
                "actual_rescan_fps": requested_fps,
                "high_frequency_rescan_performed": True,
                "rescan_window_count": len(result.windows),
                "rescan_frame_count": len(result.frames),
                "downstream_evidence_count": len(
                    result.downstream_evidence_ids
                ),
                "cached": result.cached,
                "storage_capacity_revision_sha256": (
                    storage_capacity_revision.get(
                        "revision_sha256"
                    )
                    if isinstance(
                        storage_capacity_revision, Mapping
                    )
                    else None
                ),
            },
        )

    def _prepare_degraded_review_candidates(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        if self._analysis_jobs is None:
            return PipelineStageResult(
                "model_unavailable",
                error_code="candidate_rescan_runtime_unavailable",
            )
        sha_checkpoint = context.get("checkpoints", {}).get("sha256", {})
        digest = sha_checkpoint.get("source_sha256")
        hash_job_id = sha_checkpoint.get("hash_job_id")
        if (
            not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(hash_job_id, str)
        ):
            raise PipelineStageFailure("hash_checkpoint_missing")
        try:
            hash_uuid = UUID(hash_job_id)
        except ValueError as error:
            raise PipelineStageFailure("hash_checkpoint_missing") from error

        artifact_root_binding = context.get("artifact_root")
        analysis_start_kwargs: dict[str, Any] = {}
        artifact_root_owner_id: Optional[UUID] = None
        if isinstance(artifact_root_binding, Mapping):
            try:
                artifact_root_owner_id = UUID(
                    str(context["job_id"])
                )
            except (KeyError, TypeError, ValueError) as error:
                raise PipelineStageFailure(
                    "artifact_root_job_identity_invalid"
                ) from error
            analysis_start_kwargs = {
                "artifact_root_binding": artifact_root_binding,
                "artifact_root_owner_id": artifact_root_owner_id,
            }
        matching = None
        owner_finder = getattr(
            self._analysis_jobs,
            "find_for_artifact_owner",
            None,
        )
        if (
            artifact_root_owner_id is not None
            and callable(owner_finder)
        ):
            matching = owner_finder(
                project_id,
                artifact_root_owner_id,
                snapshot_id=snapshot_id,
                file_name=file_name,
                source_sha256=digest,
            )
        elif artifact_root_owner_id is None:
            for row in self._analysis_jobs.list(
                project_id, limit=128
            ):
                if (
                    row.get("snapshot_id") == snapshot_id
                    and row.get("file_name") == file_name
                    and row.get("source_sha256") == digest
                    and row.get("status") in {
                        "queued",
                        "running",
                        "cancelling",
                        "completed",
                    }
                ):
                    matching = row
                    break
        if matching is None:
            start_analysis = getattr(
                self._analysis_jobs,
                "start",
                None,
            )
            if not callable(start_analysis):
                # Compatibility for injected read-only analysis fixtures.
                # Production AnalysisJobService exposes both start() and the
                # owner-scoped lookup above, so it never uses this fallback.
                for row in self._analysis_jobs.list(
                    project_id, limit=128
                ):
                    if (
                        row.get("snapshot_id") == snapshot_id
                        and row.get("file_name") == file_name
                        and row.get("source_sha256") == digest
                        and row.get("status") in {
                            "queued",
                            "running",
                            "cancelling",
                            "completed",
                        }
                    ):
                        matching = row
                        break
                if matching is None:
                    raise PipelineStageFailure(
                        "candidate_extraction_start_failed"
                    )
        if matching is None:
            try:
                matching = start_analysis(
                    project_id,
                    snapshot_id,
                    file_name,
                    hash_uuid,
                    digest,
                    **analysis_start_kwargs,
                )
            except HTTPException as error:
                if error.status_code != 404:
                    raise PipelineStageFailure(
                        "candidate_extraction_start_failed"
                    ) from error
                total_bytes = sha_checkpoint.get("total_bytes")
                if (
                    isinstance(total_bytes, bool)
                    or not isinstance(total_bytes, int)
                    or total_bytes <= 0
                ):
                    raise PipelineStageFailure(
                        "hash_checkpoint_missing"
                    ) from error
                self._restore_private_media_grant(
                    project_id=project_id,
                    snapshot_id=snapshot_id,
                    hash_job_id=hash_uuid,
                    file_name=file_name,
                    source_sha256=digest,
                    total_bytes=total_bytes,
                    cancel=cancel,
                )
                try:
                    matching = start_analysis(
                        project_id,
                        snapshot_id,
                        file_name,
                        hash_uuid,
                        digest,
                        **analysis_start_kwargs,
                    )
                except HTTPException as retry_error:
                    raise PipelineStageFailure(
                        "candidate_extraction_start_failed"
                    ) from retry_error

        analysis_job_id = UUID(str(matching["job_id"]))
        with self._lock:
            self._active_analysis_jobs[str(context["job_id"])] = (
                analysis_job_id
            )
        try:
            while True:
                if cancel():
                    self._analysis_jobs.cancel(
                        project_id, analysis_job_id
                    )
                    return PipelineStageResult(
                        "cancelled",
                        error_code="candidate_extraction_cancelled",
                    )
                row = self._analysis_jobs.get(
                    project_id, analysis_job_id
                )
                if row["status"] == "completed":
                    break
                if row["status"] == "cancelled":
                    return PipelineStageResult(
                        "cancelled",
                        error_code="candidate_extraction_cancelled",
                    )
                if row["status"] == "failed":
                    raise PipelineStageFailure(
                        "candidate_extraction_failed"
                    )
                time.sleep(0.1)
        except HTTPException as error:
            raise PipelineStageFailure(
                "candidate_extraction_failed"
            ) from error
        finally:
            with self._lock:
                self._active_analysis_jobs.pop(
                    str(context["job_id"]), None
                )

        result = row.get("result")
        candidates = (
            result.get("candidates")
            if isinstance(result, Mapping)
            else None
        )
        if not isinstance(candidates, list):
            raise PipelineStageFailure("candidate_result_invalid")
        if not candidates:
            return PipelineStageResult(
                "zero_detections",
                checkpoint={
                    "analysis_job_id": str(analysis_job_id),
                    "source_sha256": digest,
                    "candidate_count": 0,
                    "high_frequency_rescan_performed": False,
                    "degraded_review_mode": True,
                },
                metrics={"candidate_count": 0},
            )
        provenance = result.get("provenance")
        cadence = (
            provenance.get("effective_cadence_seconds")
            if isinstance(provenance, Mapping)
            else None
        )
        return PipelineStageResult(
            "success",
            checkpoint={
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": digest,
                "candidate_count": len(candidates),
                "requested_rescan_fps": context["profile"][
                    "candidate_rescan_fps"
                ],
                "high_frequency_rescan_performed": False,
                "actual_candidate_method": (
                    "bounded_scene_change_and_cadence_extraction"
                ),
                "effective_cadence_seconds": cadence,
                "degraded_review_mode": True,
                "review_only": True,
            },
            metrics={
                "candidate_count": len(candidates),
                "requested_rescan_fps": context["profile"][
                    "candidate_rescan_fps"
                ],
                "high_frequency_rescan_performed": False,
            },
            error_code="high_frequency_rescan_unavailable",
        )

    @classmethod
    def _validated_candidate_rescan_origin_scope(
        cls,
        checkpoint: Mapping[str, Any],
        base_candidates: Sequence[Mapping[str, Any]],
    ) -> Optional[tuple[list[dict[str, Any]], int]]:
        origins = checkpoint.get(
            "candidate_rescan_input_origins"
        )
        if origins is None:
            return None
        if not isinstance(origins, Mapping):
            raise PipelineStageFailure(
                "candidate_rescan_origin_provenance_invalid"
            )
        seed_origin = origins.get("analysis_seed")
        dense_origin = origins.get("dense_provider")
        dense_discovery = checkpoint.get(
            "dense_candidate_discovery"
        )
        if (
            origins.get("schema_version")
            != _CANDIDATE_RESCAN_INPUT_ORIGINS_SCHEMA
            or not isinstance(seed_origin, Mapping)
            or not isinstance(dense_origin, Mapping)
            or not isinstance(dense_discovery, Mapping)
        ):
            raise PipelineStageFailure(
                "candidate_rescan_origin_provenance_invalid"
            )
        seed_ids = seed_origin.get("candidate_ids")
        dense_candidates = dense_discovery.get("candidates")
        dense_revision = checkpoint.get(
            "dense_candidate_discovery_revision"
        )
        if (
            not isinstance(seed_ids, list)
            or len(set(seed_ids)) != len(seed_ids)
            or any(
                not isinstance(candidate_id, str)
                or re.fullmatch(r"[a-f0-9]{20}", candidate_id)
                is None
                for candidate_id in seed_ids
            )
            or not isinstance(dense_candidates, list)
        ):
            raise PipelineStageFailure(
                "candidate_rescan_origin_provenance_invalid"
            )
        base_by_id: dict[str, Mapping[str, Any]] = {}
        for candidate in base_candidates:
            candidate_id = (
                candidate.get("candidate_id")
                if isinstance(candidate, Mapping)
                else None
            )
            if (
                not isinstance(candidate_id, str)
                or re.fullmatch(r"[a-f0-9]{20}", candidate_id)
                is None
                or candidate_id in base_by_id
            ):
                raise PipelineStageFailure(
                    "analysis_checkpoint_invalid"
                )
            base_by_id[candidate_id] = candidate
        if any(
            candidate_id not in base_by_id
            for candidate_id in seed_ids
        ):
            raise PipelineStageFailure(
                "candidate_rescan_origin_provenance_invalid"
            )
        selected_seed_candidates = [
            dict(base_by_id[candidate_id])
            for candidate_id in seed_ids
        ]
        expected = cls._candidate_rescan_input_origins(
            seed_candidates=selected_seed_candidates,
            dense_candidates=dense_candidates,
            dense_discovery_revision=str(dense_revision),
        )
        if (
            dict(origins) != expected
            or checkpoint.get("candidate_count")
            != expected["combined_candidate_count"]
            or checkpoint.get("analysis_seed_candidate_count")
            != expected["analysis_seed"]["candidate_count"]
            or checkpoint.get("dense_candidate_count")
            != expected["dense_provider"]["candidate_count"]
        ):
            raise PipelineStageFailure(
                "candidate_rescan_origin_provenance_invalid"
            )
        return (
            selected_seed_candidates,
            int(expected["dense_provider"]["candidate_count"]),
        )

    def _analysis_job_for_context(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
    ) -> tuple[UUID, dict[str, Any], list[dict[str, Any]]]:
        if self._analysis_jobs is None:
            raise PipelineStageFailure("analysis_runtime_unavailable")
        checkpoint = context.get("checkpoints", {}).get(
            "candidate_rescan_5_10fps", {}
        )
        raw_job_id = checkpoint.get("analysis_job_id")
        expected_sha256 = checkpoint.get("source_sha256")
        try:
            analysis_job_id = UUID(str(raw_job_id))
        except (TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "analysis_checkpoint_missing"
            ) from error
        try:
            row = self._analysis_jobs.get(project_id, analysis_job_id)
        except HTTPException as error:
            raise PipelineStageFailure(
                "analysis_checkpoint_missing"
            ) from error
        result = row.get("result")
        base_candidates = (
            result.get("candidates")
            if isinstance(result, Mapping)
            else None
        )
        if (
            row.get("status") != "completed"
            or row.get("source_sha256") != expected_sha256
            or not isinstance(base_candidates, list)
        ):
            raise PipelineStageFailure("analysis_checkpoint_invalid")
        origin_scope = (
            self._validated_candidate_rescan_origin_scope(
                checkpoint,
                base_candidates,
            )
        )
        selected_seed_candidates = (
            origin_scope[0] if origin_scope is not None else None
        )
        overlay_id = checkpoint.get(
            "candidate_evidence_overlay_id"
        )
        temporal_checkpoint = context.get("checkpoints", {}).get(
            "temporal_merge", {}
        )
        dino_overlay_checkpoint = (
            temporal_checkpoint.get(
                "dino_candidate_evidence_overlay"
            )
            if isinstance(temporal_checkpoint, Mapping)
            else None
        )
        overlay_checkpoint = checkpoint.get(
            "candidate_evidence_overlay"
        )
        if (
            overlay_id is None
            and isinstance(dino_overlay_checkpoint, Mapping)
        ):
            overlay_id = dino_overlay_checkpoint.get(
                "overlay_id"
            )
            overlay_checkpoint = dino_overlay_checkpoint
        if overlay_id is None:
            selected = (
                selected_seed_candidates
                if selected_seed_candidates is not None
                else base_candidates
            )
            if not selected:
                raise PipelineStageFailure(
                    "analysis_checkpoint_invalid"
                )
            return analysis_job_id, row, list(selected)
        try:
            pipeline_job_id = UUID(str(context["job_id"]))
        except (TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "candidate_evidence_checkpoint_invalid"
            ) from error
        overlay_checkpoints: list[Mapping[str, Any]] = []
        if isinstance(overlay_checkpoint, Mapping):
            overlay_checkpoints.append(overlay_checkpoint)
        if (
            isinstance(dino_overlay_checkpoint, Mapping)
            and dino_overlay_checkpoint
            not in overlay_checkpoints
        ):
            overlay_checkpoints.append(dino_overlay_checkpoint)
        allowed_overlay_ids = {
            str(item.get("overlay_id"))
            for item in overlay_checkpoints
            if isinstance(item.get("overlay_id"), str)
        }
        if (
            self._candidate_evidence_overlays is None
            or not isinstance(overlay_checkpoint, Mapping)
            or overlay_checkpoint.get("overlay_id") != overlay_id
            or not isinstance(overlay_id, str)
            or any(
                item.get("project_id") != str(project_id)
                or item.get("analysis_job_id")
                != str(analysis_job_id)
                or item.get("pipeline_job_id")
                != str(pipeline_job_id)
                or item.get("source_sha256") != expected_sha256
                or not isinstance(item.get("overlay_id"), str)
                or isinstance(item.get("candidate_count"), bool)
                or not isinstance(item.get("candidate_count"), int)
                or not 1 <= int(item["candidate_count"]) <= 512
                for item in overlay_checkpoints
            )
            or len(allowed_overlay_ids) != len(overlay_checkpoints)
        ):
            raise PipelineStageFailure(
                "candidate_evidence_checkpoint_invalid"
            )
        try:
            if self._candidate_evidence_registry is not None:
                binder = getattr(
                    self._candidate_evidence_registry,
                    "bind_overlay",
                    None,
                )
                lister = getattr(
                    self._candidate_evidence_registry,
                    "list_candidates",
                    None,
                )
                if not callable(binder) or not callable(lister):
                    raise TypeError(
                        "candidate evidence registry is invalid"
                    )
                for item in overlay_checkpoints:
                    binder(
                        project_id,
                        analysis_job_id,
                        pipeline_job_id,
                        str(item["overlay_id"]),
                    )
                envelopes = lister(
                    project_id,
                    analysis_job_id,
                )
            else:
                adapters = [
                    CandidateEvidenceOverlayAdapter(
                        self._candidate_evidence_overlays,
                        pipeline_job_id=pipeline_job_id,
                        overlay_id=str(item["overlay_id"]),
                    )
                    for item in overlay_checkpoints
                ]
                envelopes = CompositeCandidateEvidence(
                    self._analysis_jobs,
                    adapters,
                ).list_candidates(
                    project_id,
                    analysis_job_id,
                )
        except Exception as error:
            if isinstance(error, PipelineStageFailure):
                raise
            raise PipelineStageFailure(
                "candidate_evidence_checkpoint_invalid"
            ) from error
        candidates: list[dict[str, Any]] = []
        for envelope in envelopes:
            metadata = (
                envelope.get("metadata")
                if isinstance(envelope, Mapping)
                else None
            )
            if not isinstance(metadata, Mapping):
                raise PipelineStageFailure(
                    "candidate_evidence_checkpoint_invalid"
                )
            evidence_overlay = metadata.get("evidence_overlay")
            if (
                not isinstance(evidence_overlay, Mapping)
                or evidence_overlay.get("overlay_id")
                not in allowed_overlay_ids
            ):
                continue
            candidates.append(dict(metadata))
        if selected_seed_candidates is not None:
            candidates.extend(
                dict(candidate)
                for candidate in selected_seed_candidates
            )
        expected_candidate_count = sum(
            int(item.get("candidate_count", -1))
            for item in overlay_checkpoints
        ) + (
            len(selected_seed_candidates)
            if selected_seed_candidates is not None
            else 0
        )
        if (
            not candidates
            or expected_candidate_count < 1
            or len(candidates) != expected_candidate_count
        ):
            raise PipelineStageFailure(
                "candidate_evidence_checkpoint_invalid"
            )
        return analysis_job_id, row, candidates

    def _high_frequency_evidence_for_context(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        context: Mapping[str, Any],
    ) -> Optional[
        tuple[
            list[dict[str, Any]],
            dict[str, str],
            Callable[[UUID, UUID, str], ResolvedAnalysisArtifact],
            dict[str, Any],
        ]
    ]:
        """Reopen the internal rescan manifest as a detector/OCR input hook.

        The durable checkpoint contains only opaque IDs and digests.  Paths
        are reconstructed server-side under the project/job scope, and every
        selected JPEG is re-hashed before a local detector receives it.
        """

        checkpoint = context.get("checkpoints", {}).get(
            "candidate_rescan_5_10fps", {}
        )
        if not isinstance(checkpoint, Mapping):
            return None
        if checkpoint.get("high_frequency_rescan_performed") is not True:
            return None
        run_id = checkpoint.get("rescan_run_id")
        manifest_sha256 = checkpoint.get(
            "rescan_manifest_sha256"
        )
        input_revision = checkpoint.get("rescan_input_revision")
        source_sha256 = checkpoint.get("source_sha256")
        expected_count = checkpoint.get("downstream_evidence_count")
        if (
            not isinstance(run_id, str)
            or re.fullmatch(
                r"candidate-window-rescan-[a-f0-9]{24}",
                run_id,
            )
            is None
            or not isinstance(manifest_sha256, str)
            or _SHA256.fullmatch(manifest_sha256) is None
            or not isinstance(input_revision, str)
            or _SHA256.fullmatch(input_revision) is None
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or not 1 <= expected_count <= 512
        ):
            raise PipelineStageFailure(
                "candidate_rescan_checkpoint_invalid"
            )
        (
            _rescan_project_root,
            rescan_output_root,
        ) = self._pipeline_artifact_directory(
            project_id,
            context,
            "candidate-window-rescans",
        )
        run_directory = rescan_output_root / run_id
        try:
            result = load_candidate_window_rescan(
                run_directory,
                expected_source_sha256=source_sha256,
                expected_input_revision=input_revision,
            )
        except CandidateWindowRescanError as error:
            raise PipelineStageFailure(
                "candidate_rescan_manifest_invalid"
            ) from error
        if (
            result.manifest_sha256 != manifest_sha256
            or len(result.downstream_evidence_ids)
            != expected_count
        ):
            raise PipelineStageFailure(
                "candidate_rescan_manifest_invalid"
            )
        declared_quality = checkpoint.get(
            "visual_evidence_quality"
        )
        declared_quality_revision = checkpoint.get(
            "visual_evidence_quality_revision"
        )
        try:
            evaluated_quality = (
                evaluate_candidate_window_visual_quality(
                    result,
                    source_sha256=source_sha256,
                    semantic_observer=(
                        self._visual_evidence_semantic_observer
                    ),
                )
            )
        except VisualEvidenceQualityError as error:
            raise PipelineStageFailure(
                "visual_evidence_quality_failed"
            ) from error
        if (
            not isinstance(declared_quality, Mapping)
            or declared_quality.get("schema_version")
            != VISUAL_EVIDENCE_QUALITY_SCHEMA
            or declared_quality.get("source_sha256")
            != source_sha256
            or declared_quality.get(
                "rescan_manifest_sha256"
            )
            != manifest_sha256
            or declared_quality.get("quality_revision")
            != evaluated_quality.quality_revision
            or declared_quality_revision
            != evaluated_quality.quality_revision
            or checkpoint.get(
                "visual_evidence_quality_state"
            )
            != evaluated_quality.state
            or declared_quality.get(
                "selected_evidence_ids"
            )
            != list(evaluated_quality.selected_evidence_ids)
            or declared_quality.get(
                "eligible_parent_candidate_ids"
            )
            != list(
                evaluated_quality.eligible_parent_candidate_ids
            )
            or declared_quality.get(
                "blocked_parent_candidate_ids"
            )
            != list(
                evaluated_quality.blocked_parent_candidate_ids
            )
            or evaluated_quality.state
            == "unusable_visual_evidence"
            or not evaluated_quality.selected_evidence_ids
        ):
            raise PipelineStageFailure(
                "visual_evidence_quality_checkpoint_invalid"
            )
        frame_by_id = {
            frame.evidence_id: frame for frame in result.frames
        }
        selected_frames = [
            frame_by_id[evidence_id]
            for evidence_id
            in evaluated_quality.selected_evidence_ids
        ]
        candidates = [
            {
                "candidate_id": frame.evidence_id,
                "representative_timestamp_seconds": (
                    frame.timestamp_seconds
                ),
                "source_candidate_ids": list(
                    frame.source_candidate_ids
                ),
                "primary_candidate_id": (
                    frame.primary_candidate_id
                ),
                "evidence_image_sha256": frame.sha256,
                "evidence_source": (
                    CANDIDATE_WINDOW_RESCAN_SCHEMA
                ),
            }
            for frame in selected_frames
        ]
        parent_by_evidence = {
            frame.evidence_id: frame.primary_candidate_id
            for frame in selected_frames
        }

        def resolve(
            requested_project_id: UUID,
            requested_analysis_job_id: UUID,
            evidence_id: str,
        ) -> ResolvedAnalysisArtifact:
            if (
                requested_project_id != project_id
                or requested_analysis_job_id != analysis_job_id
                or evidence_id not in parent_by_evidence
            ):
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "The high-frequency candidate evidence was "
                        "not found in this project"
                    ),
                )
            try:
                artifact = resolve_candidate_window_frame(
                    result, evidence_id
                )
            except CandidateWindowRescanError as error:
                raise HTTPException(
                    status_code=410,
                    detail=(
                        "The high-frequency candidate evidence "
                        "failed integrity verification"
                    ),
                ) from error
            return ResolvedAnalysisArtifact(
                path=artifact.path,
                media_type=artifact.media_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
            )

        metadata = {
            "source": "candidate_window_rescan_5_10fps",
            "manifest_sha256": manifest_sha256,
            "input_revision": hashlib.sha256(
                _canonical_json(
                    {
                        "rescan_input_revision": input_revision,
                        "visual_evidence_quality_revision": (
                            evaluated_quality.quality_revision
                        ),
                    }
                )
            ).hexdigest(),
            "selected_frame_count": len(candidates),
            "sampling_fps": checkpoint.get("actual_rescan_fps"),
            "visual_evidence_quality_state": (
                evaluated_quality.state
            ),
            "visual_evidence_quality_revision": (
                evaluated_quality.quality_revision
            ),
            "eligible_parent_candidate_ids": list(
                evaluated_quality.eligible_parent_candidate_ids
            ),
            "blocked_parent_candidate_ids": list(
                evaluated_quality.blocked_parent_candidate_ids
            ),
            "frame_quality_observations": [
                asdict(item)
                for item in evaluated_quality.frame_observations
            ],
            "frame_parent_scope": {
                frame.evidence_id: {
                    "timestamp_seconds": frame.timestamp_seconds,
                    "source_candidate_ids": list(
                        frame.source_candidate_ids
                    ),
                    "primary_candidate_id": (
                        frame.primary_candidate_id
                    ),
                }
                for frame in result.frames
                if frame.evidence_id
                in {
                    item.evidence_id
                    for item
                    in evaluated_quality.frame_observations
                }
            },
        }
        return candidates, parent_by_evidence, resolve, metadata

    @staticmethod
    def _summarize_embedded_local_vision(
        candidates: Sequence[Mapping[str, Any]],
        profile: Mapping[str, Any],
        *,
        allow_embedded_ocr: bool = True,
    ) -> dict[str, Any]:
        """Validate and summarize immutable per-candidate local inference.

        ``AnalysisJobService`` already runs the configured local vision pack
        and persists its output beside each extracted candidate.  The durable
        PipelineJob must consume that exact evidence instead of inferring pack
        availability from a separate status call or relabelling an empty
        result as success.  This summary deliberately remains review-only.
        """

        detector_pack_id = profile.get("detector_pack_id")
        ocr_pack_id = profile.get("ocr_pack_id")
        if (
            not isinstance(detector_pack_id, str)
            or not detector_pack_id
            or not isinstance(ocr_pack_id, str)
            or not ocr_pack_id
        ):
            raise PipelineStageFailure("pipeline_profile_invalid")

        role_state: dict[str, dict[str, Any]] = {
            "detector": {
                "configured": False,
                "matched_candidate_ids": set(),
                "failed_candidate_ids": set(),
                "unavailable_candidate_ids": set(),
                "output_count": 0,
            },
            "ocr": {
                "configured": False,
                "matched_candidate_ids": set(),
                "failed_candidate_ids": set(),
                "unavailable_candidate_ids": set(),
                "output_count": 0,
            },
        }
        detections_by_candidate: dict[str, dict[str, Any]] = {}
        ocr_candidate_ids: set[str] = set()
        detected_issue_types: set[str] = set()
        fingerprint_rows: list[dict[str, Any]] = []
        identities: dict[str, dict[str, str]] = {}
        candidate_ids: list[str] = []

        def validated_box(
            value: Any,
            *,
            required: bool,
        ) -> Optional[dict[str, float]]:
            if value is None and not required:
                return None
            if not isinstance(value, Mapping) or set(value) != {
                "x",
                "y",
                "width",
                "height",
            }:
                raise PipelineStageFailure(
                    "local_vision_inference_invalid"
                )
            result: dict[str, float] = {}
            for key in ("x", "y", "width", "height"):
                number = value[key]
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                ):
                    raise PipelineStageFailure(
                        "local_vision_inference_invalid"
                    )
                result[key] = float(number)
            if (
                not 0.0 <= result["x"] <= 1.0
                or not 0.0 <= result["y"] <= 1.0
                or not 0.0 < result["width"] <= 1.0
                or not 0.0 < result["height"] <= 1.0
                or result["x"] + result["width"] > 1.00000001
                or result["y"] + result["height"] > 1.00000001
            ):
                raise PipelineStageFailure(
                    "local_vision_inference_invalid"
                )
            return result

        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise PipelineStageFailure(
                    "local_vision_inference_invalid"
                )
            candidate_id = candidate.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or re.fullmatch(r"[a-f0-9]{20}", candidate_id) is None
                or candidate_id in candidate_ids
            ):
                raise PipelineStageFailure(
                    "local_vision_inference_invalid"
                )
            candidate_ids.append(candidate_id)
            inference = candidate.get("ai_inference")
            if (
                inference is None
                and candidate.get("candidate_kind")
                in {
                    "dense_candidate_evidence_overlay",
                    "dino_temporal_review_evidence_overlay",
                }
            ):
                try:
                    issue_type = canonical_issue_type(
                        candidate.get("issue_type")
                    )
                except CandidateLabelError as error:
                    raise PipelineStageFailure(
                        "local_vision_inference_invalid"
                    ) from error
                score = candidate.get("score")
                timestamp = candidate.get(
                    "representative_timestamp_seconds"
                )
                sources_value = candidate.get(
                    "proposal_sources"
                )
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(float(score))
                    or not 0.0 <= float(score) <= 1.0
                    or isinstance(timestamp, bool)
                    or not isinstance(timestamp, (int, float))
                    or not math.isfinite(float(timestamp))
                    or float(timestamp) < 0
                    or not isinstance(sources_value, list)
                    or not sources_value
                    or any(
                        not isinstance(source, str)
                        or not source
                        for source in sources_value
                    )
                ):
                    raise PipelineStageFailure(
                        "local_vision_inference_invalid"
                    )
                normalized_box = validated_box(
                    candidate.get("normalized_box"),
                    required=False,
                )
                sources = sorted(set(sources_value))
                is_dino_temporal = (
                    candidate.get("candidate_kind")
                    == "dino_temporal_review_evidence_overlay"
                )
                detections_by_candidate[candidate_id] = {
                    "detection_id": candidate_id,
                    "timestamp_seconds": float(timestamp),
                    "issue_type": issue_type,
                    "score": float(score),
                    "normalized_box": normalized_box,
                    "score_semantics": (
                        "review_proposal_score_not_probability"
                    ),
                    "review_only": True,
                    "automatic_labeling_allowed": False,
                    "proposal_source": (
                        "grounding_dino_temporal_review"
                        if is_dino_temporal
                        else "dense_2fps_candidate_discovery"
                    ),
                }
                detected_issue_types.add(issue_type)
                fingerprint_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "candidate_kind": (
                            candidate.get("candidate_kind")
                        ),
                        "issue_type": issue_type,
                        "score": float(score),
                        "normalized_box": normalized_box,
                        "proposal_sources": sources,
                        "proposal_provenance_sha256": (
                            candidate.get(
                                "proposal_provenance_sha256"
                            )
                        ),
                    }
                )
                continue
            if inference is None:
                continue
            if not isinstance(inference, Mapping):
                raise PipelineStageFailure(
                    "local_vision_inference_invalid"
                )

            pack_id = inference.get("pack_id")
            pack_version = inference.get("pack_version")
            manifest_sha256 = inference.get("manifest_sha256")
            capabilities_value = inference.get("capabilities")
            if not isinstance(capabilities_value, list) or any(
                not isinstance(item, str)
                for item in capabilities_value
            ):
                raise PipelineStageFailure(
                    "local_vision_inference_invalid"
                )
            capabilities = set(capabilities_value)
            detector_match = (
                pack_id == detector_pack_id
                and bool(
                    capabilities.intersection(
                        {"defect_detection", "detection"}
                    )
                )
            )
            ocr_match = (
                allow_embedded_ocr
                and pack_id == ocr_pack_id
                and "ocr" in capabilities
            )
            if not detector_match and not ocr_match:
                continue
            if (
                not isinstance(pack_id, str)
                or not isinstance(pack_version, str)
                or not pack_version
                or not isinstance(manifest_sha256, str)
                or _SHA256.fullmatch(manifest_sha256) is None
            ):
                raise PipelineStageFailure(
                    "local_vision_provenance_invalid"
                )
            identity = {
                "pack_id": pack_id,
                "pack_version": pack_version,
                "manifest_sha256": manifest_sha256,
            }
            existing_identity = identities.get(pack_id)
            if (
                existing_identity is not None
                and existing_identity != identity
            ):
                raise PipelineStageFailure(
                    "local_vision_provenance_invalid"
                )
            identities[pack_id] = identity

            mode = inference.get("mode")
            status = inference.get("status")
            failed = status in {"failed", "timed_out"}
            unavailable = (
                mode == "scene_fallback"
                and status == "provider_unavailable"
            )
            successful = (
                mode == "local_model"
                and status in {"classified", "no_detection"}
            )
            if not failed and not unavailable and not successful:
                raise PipelineStageFailure(
                    "local_vision_inference_invalid"
                )
            for role, matches in (
                ("detector", detector_match),
                ("ocr", ocr_match),
            ):
                if not matches:
                    continue
                role_state[role]["configured"] = True
                role_state[role]["matched_candidate_ids"].add(
                    candidate_id
                )
                if failed:
                    role_state[role]["failed_candidate_ids"].add(
                        candidate_id
                    )
                if unavailable:
                    role_state[role][
                        "unavailable_candidate_ids"
                    ].add(candidate_id)

            if failed or unavailable:
                fingerprint_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "identity": identity,
                        "capabilities": sorted(capabilities),
                        "mode": mode,
                        "status": status,
                        "warning_code": inference.get("warning_code"),
                    }
                )
                continue

            predictions = inference.get("predictions")
            observations = inference.get("ocr_observations")
            if not isinstance(predictions, list) or not isinstance(
                observations, list
            ):
                raise PipelineStageFailure(
                    "local_vision_inference_invalid"
                )
            safe_predictions: list[dict[str, Any]] = []
            if detector_match:
                for prediction in predictions:
                    if not isinstance(prediction, Mapping):
                        raise PipelineStageFailure(
                            "local_vision_inference_invalid"
                        )
                    try:
                        issue_type = canonical_issue_type(
                            prediction.get("issue_type")
                        )
                    except CandidateLabelError as error:
                        raise PipelineStageFailure(
                            "local_vision_inference_invalid"
                        ) from error
                    confidence = prediction.get("confidence")
                    calibration = prediction.get("calibration")
                    if (
                        isinstance(confidence, bool)
                        or not isinstance(confidence, (int, float))
                        or not math.isfinite(float(confidence))
                        or not 0.0 <= float(confidence) <= 1.0
                        or calibration
                        not in {"model_score", "zero_shot_relative"}
                    ):
                        raise PipelineStageFailure(
                            "local_vision_inference_invalid"
                        )
                    safe_prediction = {
                        "issue_type": issue_type,
                        "score": float(confidence),
                        "normalized_box": validated_box(
                            prediction.get("box"),
                            required=False,
                        ),
                        "score_semantics": (
                            "zero_shot_relative_not_probability"
                            if calibration == "zero_shot_relative"
                            else "model_score_not_probability"
                        ),
                    }
                    safe_predictions.append(safe_prediction)
                    detected_issue_types.add(issue_type)
                role_state["detector"]["output_count"] += len(
                    safe_predictions
                )
                if safe_predictions:
                    timestamp = candidate.get(
                        "representative_timestamp_seconds"
                    )
                    if (
                        isinstance(timestamp, bool)
                        or not isinstance(timestamp, (int, float))
                        or not math.isfinite(float(timestamp))
                        or float(timestamp) < 0
                    ):
                        raise PipelineStageFailure(
                            "local_vision_inference_invalid"
                        )
                    best = sorted(
                        safe_predictions,
                        key=lambda item: (
                            -item["score"],
                            item["issue_type"],
                            _canonical_json(item),
                        ),
                    )[0]
                    detections_by_candidate[candidate_id] = {
                        "detection_id": candidate_id,
                        "timestamp_seconds": float(timestamp),
                        **best,
                        "review_only": True,
                        "automatic_labeling_allowed": False,
                        "proposal_source": "dedicated_road_model",
                    }

            safe_observations: list[dict[str, Any]] = []
            if ocr_match:
                for observation in observations:
                    if not isinstance(observation, Mapping):
                        raise PipelineStageFailure(
                            "local_vision_inference_invalid"
                        )
                    text = observation.get("text")
                    confidence = observation.get("confidence")
                    if (
                        not isinstance(text, str)
                        or not text.strip()
                        or len(text) > 512
                        or isinstance(confidence, bool)
                        or not isinstance(confidence, (int, float))
                        or not math.isfinite(float(confidence))
                        or not 0.0 <= float(confidence) <= 1.0
                    ):
                        raise PipelineStageFailure(
                            "local_vision_inference_invalid"
                        )
                    safe_observations.append(
                        {
                            "text": text.strip(),
                            "confidence": float(confidence),
                            "box": validated_box(
                                observation.get("box"),
                                required=True,
                            ),
                            "kind": observation.get("kind"),
                        }
                    )
                role_state["ocr"]["output_count"] += len(
                    safe_observations
                )
                if safe_observations:
                    ocr_candidate_ids.add(candidate_id)

            fingerprint_rows.append(
                {
                    "candidate_id": candidate_id,
                    "identity": identity,
                    "capabilities": sorted(capabilities),
                    "mode": mode,
                    "status": status,
                    "predictions": safe_predictions,
                    "ocr_observations": safe_observations,
                }
            )

        candidate_id_set = set(candidate_ids)
        for role in role_state.values():
            if role["configured"] and (
                role["matched_candidate_ids"] != candidate_id_set
            ):
                role["failed_candidate_ids"].update(
                    candidate_id_set
                    - role["matched_candidate_ids"]
                )

        def public_role(role: str) -> dict[str, Any]:
            state = role_state[role]
            if not state["configured"]:
                status = "model_unavailable"
            elif state["unavailable_candidate_ids"]:
                status = "model_unavailable"
            elif state["failed_candidate_ids"]:
                status = "failed"
            elif state["output_count"] > 0:
                status = "completed"
            else:
                status = "zero_detections"
            return {
                "status": status,
                "output_count": int(state["output_count"]),
                "failed_candidate_count": len(
                    state["failed_candidate_ids"]
                ),
            }

        detector = public_role("detector")
        ocr = public_role("ocr")
        local_candidate_ids = sorted(
            set(detections_by_candidate) | ocr_candidate_ids
        )
        return {
            "detector": detector,
            "ocr": ocr,
            "detections_by_candidate": detections_by_candidate,
            "ocr_candidate_ids": sorted(ocr_candidate_ids),
            "detected_issue_types": sorted(detected_issue_types),
            "proposal_candidate_ids": local_candidate_ids,
            "proposal_candidate_count": len(local_candidate_ids),
            "model_identities": [
                identities[key] for key in sorted(identities)
            ],
            "inference_revision": hashlib.sha256(
                _canonical_json(
                    {
                        "candidate_ids": candidate_ids,
                        "rows": fingerprint_rows,
                    }
                )
            ).hexdigest(),
            "review_only": True,
            "automatic_labeling_allowed": False,
            "annotation_count": 0,
            "incident_count": 0,
        }

    @staticmethod
    def _pipeline_ocr_result(
        *,
        status: str,
        profile_pack_id: str,
        runtime_status: Mapping[str, Any],
        candidate_ids: Sequence[str],
        observations_by_candidate: Mapping[
            str, Sequence[Mapping[str, Any]]
        ] = {},
        failed_candidate_count: int = 0,
        raw_observation_count: int = 0,
        observations_truncated: bool = False,
        input_rows: Sequence[Mapping[str, Any]] = (),
        error_code: Optional[str] = None,
    ) -> dict[str, Any]:
        if status not in {
            "completed",
            "zero_detections",
            "model_unavailable",
            "failed",
        }:
            raise PipelineStageFailure("ocr_result_invalid")
        safe_observations = {
            str(candidate_id): [
                dict(observation) for observation in observations
            ]
            for candidate_id, observations in sorted(
                observations_by_candidate.items()
            )
            if observations
        }
        observation_count = sum(
            len(rows) for rows in safe_observations.values()
        )
        identity = {
            "pack_id": runtime_status.get("pack_id"),
            "pack_version": runtime_status.get("pack_version"),
            "manifest_sha256": runtime_status.get(
                "manifest_sha256"
            ),
            "engine": runtime_status.get("engine"),
        }
        fingerprint = {
            "status": status,
            "requested_pack_id": profile_pack_id,
            "identity": identity,
            "candidate_ids": list(candidate_ids),
            "input_rows": [dict(row) for row in input_rows],
            "observations": safe_observations,
            "failed_candidate_count": failed_candidate_count,
            "raw_observation_count": raw_observation_count,
            "observations_truncated": observations_truncated,
            "error_code": error_code,
        }
        revision = hashlib.sha256(
            _canonical_json(fingerprint)
        ).hexdigest()
        return {
            "status": status,
            "output_count": observation_count,
            "failed_candidate_count": failed_candidate_count,
            "candidate_count": len(candidate_ids),
            "candidate_ids": list(candidate_ids),
            "ocr_candidate_ids": sorted(safe_observations),
            "observations_by_candidate": safe_observations,
            "raw_observation_count": raw_observation_count,
            "observations_truncated": observations_truncated,
            "pack_id": identity["pack_id"],
            "pack_version": identity["pack_version"],
            "manifest_sha256": identity["manifest_sha256"],
            "engine": identity["engine"],
            "requested_pack_id": profile_pack_id,
            "ocr_revision": revision,
            "provenance": {
                "schema_version": (
                    "trafficvisson.pipeline-ocr-provenance.v1"
                ),
                "engine_role": "ocr_only",
                "requested_pack_id": profile_pack_id,
                **identity,
                "candidate_count": len(candidate_ids),
                "input_revision": hashlib.sha256(
                    _canonical_json(
                        [dict(row) for row in input_rows]
                    )
                ).hexdigest(),
                "result_revision": revision,
                "review_only": True,
                "path_exposed": False,
            },
            "error_code": error_code,
            "review_only": True,
            "automatic_labeling_allowed": False,
            "annotation_count": 0,
            "incident_count": 0,
        }

    def _run_pipeline_ocr(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        candidates: Sequence[Mapping[str, Any]],
        profile: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> dict[str, Any]:
        profile_pack_id = profile.get("ocr_pack_id")
        candidate_ids = [
            str(candidate.get("candidate_id"))
            for candidate in candidates
        ]
        if (
            not isinstance(profile_pack_id, str)
            or not profile_pack_id
            or not candidate_ids
            or any(
                re.fullmatch(r"[a-f0-9]{20}", candidate_id)
                is None
                for candidate_id in candidate_ids
            )
            or len(set(candidate_ids)) != len(candidate_ids)
        ):
            raise PipelineStageFailure("ocr_input_invalid")
        try:
            runtime_status = dict(self._ocr_status())
        except Exception:
            runtime_status = {
                "enabled": False,
                "warning_code": "model_pack_status_failed",
            }
            return self._pipeline_ocr_result(
                status="failed",
                profile_pack_id=profile_pack_id,
                runtime_status=runtime_status,
                candidate_ids=candidate_ids,
                error_code="ocr_model_status_failed",
            )
        runtime_capabilities = set(
            runtime_status.get("capabilities") or ()
        )
        if (
            self._ocr_inference_service is None
            or runtime_status.get("enabled") is not True
            or runtime_status.get("pack_id") != profile_pack_id
            or runtime_status.get("engine")
            != "easyocr_ocr_only"
            or "ocr" not in runtime_capabilities
        ):
            return self._pipeline_ocr_result(
                status="model_unavailable",
                profile_pack_id=profile_pack_id,
                runtime_status=runtime_status,
                candidate_ids=candidate_ids,
                error_code=(
                    str(runtime_status.get("warning_code"))
                    if isinstance(
                        runtime_status.get("warning_code"), str
                    )
                    else "ocr_model_unavailable"
                ),
            )
        if cancel():
            raise VisionInferenceCancelled(
                "OCR inference was cancelled"
            )
        evidence_backend = (
            self._candidate_evidence_registry
            if self._candidate_evidence_registry is not None
            else self._analysis_jobs
        )
        resolver = getattr(
            evidence_backend,
            "resolve_candidate_image_batch",
            None,
        )
        if not callable(resolver):
            return self._pipeline_ocr_result(
                status="failed",
                profile_pack_id=profile_pack_id,
                runtime_status=runtime_status,
                candidate_ids=candidate_ids,
                error_code="ocr_candidate_resolver_unavailable",
            )
        try:
            if self._candidate_evidence_registry is not None:
                _, inference_batch_root = (
                    self._pipeline_artifact_directory(
                        project_id,
                        context,
                        "candidate-inference-batches",
                    )
                )
                batch = resolver(
                    project_id,
                    analysis_job_id,
                    candidate_ids,
                    pipeline_job_id=UUID(
                        str(context["job_id"])
                    ),
                    artifact_root=inference_batch_root,
                )
            else:
                batch = resolver(
                    project_id,
                    analysis_job_id,
                    candidate_ids,
                )
            if not isinstance(
                batch, ResolvedAnalysisCandidateImageBatch
            ):
                raise VisionInferenceError(
                    "OCR candidate batch is invalid"
                )
            input_rows = [
                {
                    "candidate_id": image.candidate_id,
                    "image_sha256": image.sha256,
                    "size_bytes": image.size_bytes,
                }
                for image in batch.images
            ]
            raw_results, _summary = (
                self._ocr_inference_service.infer_candidates(
                    batch.inference_candidates(),
                    batch.run_directory,
                    cancel=cancel,
                )
            )
        except VisionInferenceCancelled:
            raise
        except (HTTPException, OSError, VisionInferenceError):
            return self._pipeline_ocr_result(
                status="failed",
                profile_pack_id=profile_pack_id,
                runtime_status=runtime_status,
                candidate_ids=candidate_ids,
                error_code="ocr_inference_failed",
            )
        except Exception:
            return self._pipeline_ocr_result(
                status="failed",
                profile_pack_id=profile_pack_id,
                runtime_status=runtime_status,
                candidate_ids=candidate_ids,
                error_code="ocr_inference_failed",
            )
        if (
            not isinstance(raw_results, Mapping)
            or set(raw_results) != set(candidate_ids)
        ):
            return self._pipeline_ocr_result(
                status="failed",
                profile_pack_id=profile_pack_id,
                runtime_status=runtime_status,
                candidate_ids=candidate_ids,
                input_rows=input_rows,
                error_code="ocr_result_invalid",
            )

        observations_by_candidate: dict[
            str, list[dict[str, Any]]
        ] = {}
        raw_observation_count = 0
        failed_candidate_count = 0
        unavailable_candidate_count = 0
        truncated = False
        remaining = MAX_PIPELINE_OCR_OBSERVATIONS
        for candidate_id in candidate_ids:
            result = raw_results.get(candidate_id)
            if not isinstance(result, Mapping):
                return self._pipeline_ocr_result(
                    status="failed",
                    profile_pack_id=profile_pack_id,
                    runtime_status=runtime_status,
                    candidate_ids=candidate_ids,
                    input_rows=input_rows,
                    error_code="ocr_result_invalid",
                )
            result_status = result.get("status")
            if result_status in {"failed", "timed_out"}:
                failed_candidate_count += 1
                continue
            if result_status == "provider_unavailable":
                unavailable_candidate_count += 1
                continue
            if (
                result_status not in {"classified", "no_detection"}
                or result.get("mode") != "local_model"
                or result.get("pack_id") != profile_pack_id
                or result.get("pack_version")
                != runtime_status.get("pack_version")
                or result.get("manifest_sha256")
                != runtime_status.get("manifest_sha256")
                or result.get("provider")
                != "easyocr_ocr_only"
            ):
                return self._pipeline_ocr_result(
                    status="failed",
                    profile_pack_id=profile_pack_id,
                    runtime_status=runtime_status,
                    candidate_ids=candidate_ids,
                    input_rows=input_rows,
                    error_code="ocr_provenance_invalid",
                )
            raw_observations = result.get("ocr_observations")
            if not isinstance(raw_observations, list):
                return self._pipeline_ocr_result(
                    status="failed",
                    profile_pack_id=profile_pack_id,
                    runtime_status=runtime_status,
                    candidate_ids=candidate_ids,
                    input_rows=input_rows,
                    error_code="ocr_result_invalid",
                )
            raw_observation_count += len(raw_observations)
            safe_rows: list[dict[str, Any]] = []
            for observation in raw_observations:
                if not isinstance(observation, Mapping):
                    return self._pipeline_ocr_result(
                        status="failed",
                        profile_pack_id=profile_pack_id,
                        runtime_status=runtime_status,
                        candidate_ids=candidate_ids,
                        input_rows=input_rows,
                        error_code="ocr_result_invalid",
                    )
                text = observation.get("text")
                confidence = observation.get("confidence")
                box = observation.get("box")
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or len(text) > 500
                    or "\x00" in text
                    or isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or not math.isfinite(float(confidence))
                    or not 0 <= float(confidence) <= 1
                    or not isinstance(box, Mapping)
                    or set(box)
                    != {"x", "y", "width", "height"}
                    or observation.get("kind")
                    != "sign_or_tunnel_text_unverified"
                ):
                    return self._pipeline_ocr_result(
                        status="failed",
                        profile_pack_id=profile_pack_id,
                        runtime_status=runtime_status,
                        candidate_ids=candidate_ids,
                        input_rows=input_rows,
                        error_code="ocr_result_invalid",
                    )
                safe_box: dict[str, float] = {}
                for key in ("x", "y", "width", "height"):
                    number = box[key]
                    if (
                        isinstance(number, bool)
                        or not isinstance(number, (int, float))
                        or not math.isfinite(float(number))
                    ):
                        return self._pipeline_ocr_result(
                            status="failed",
                            profile_pack_id=profile_pack_id,
                            runtime_status=runtime_status,
                            candidate_ids=candidate_ids,
                            input_rows=input_rows,
                            error_code="ocr_result_invalid",
                        )
                    safe_box[key] = float(number)
                if (
                    not 0 <= safe_box["x"] <= 1
                    or not 0 <= safe_box["y"] <= 1
                    or not 0 < safe_box["width"] <= 1
                    or not 0 < safe_box["height"] <= 1
                    or safe_box["x"] + safe_box["width"]
                    > 1.00000001
                    or safe_box["y"] + safe_box["height"]
                    > 1.00000001
                ):
                    return self._pipeline_ocr_result(
                        status="failed",
                        profile_pack_id=profile_pack_id,
                        runtime_status=runtime_status,
                        candidate_ids=candidate_ids,
                        input_rows=input_rows,
                        error_code="ocr_result_invalid",
                    )
                if (
                    len(safe_rows)
                    >= MAX_PIPELINE_OCR_OBSERVATIONS_PER_CANDIDATE
                    or remaining <= 0
                ):
                    truncated = True
                    continue
                safe_rows.append(
                    {
                        "text": text.strip(),
                        "confidence": round(
                            float(confidence), 8
                        ),
                        "box": safe_box,
                        "kind": (
                            "sign_or_tunnel_text_unverified"
                        ),
                    }
                )
                remaining -= 1
            if safe_rows:
                observations_by_candidate[candidate_id] = safe_rows

        if failed_candidate_count:
            status = "failed"
            error_code = "ocr_inference_failed"
        elif unavailable_candidate_count:
            status = "model_unavailable"
            error_code = "ocr_model_unavailable"
        elif observations_by_candidate:
            status = "completed"
            error_code = None
        else:
            status = "zero_detections"
            error_code = None
        return self._pipeline_ocr_result(
            status=status,
            profile_pack_id=profile_pack_id,
            runtime_status=runtime_status,
            candidate_ids=candidate_ids,
            observations_by_candidate=observations_by_candidate,
            failed_candidate_count=failed_candidate_count,
            raw_observation_count=raw_observation_count,
            observations_truncated=truncated,
            input_rows=input_rows,
            error_code=error_code,
        )

    @staticmethod
    def _merge_pipeline_ocr(
        local: Mapping[str, Any],
        ocr_result: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = dict(local)
        observations = ocr_result.get(
            "observations_by_candidate", {}
        )
        if not isinstance(observations, Mapping):
            raise PipelineStageFailure("ocr_checkpoint_invalid")
        safe_observations = {
            str(candidate_id): [
                dict(row) for row in rows
            ]
            for candidate_id, rows in observations.items()
            if isinstance(rows, list) and rows
        }
        ocr_status = ocr_result.get("status")
        ocr_revision = ocr_result.get("ocr_revision")
        if (
            ocr_status
            not in {
                "completed",
                "zero_detections",
                "model_unavailable",
                "failed",
            }
            or not isinstance(ocr_revision, str)
            or _SHA256.fullmatch(ocr_revision) is None
        ):
            raise PipelineStageFailure("ocr_checkpoint_invalid")
        ocr_candidate_ids = sorted(safe_observations)
        result["ocr"] = {
            "status": ocr_status,
            "output_count": int(
                ocr_result.get("output_count", 0)
            ),
            "failed_candidate_count": int(
                ocr_result.get("failed_candidate_count", 0)
            ),
        }
        result["ocr_candidate_ids"] = ocr_candidate_ids
        result["ocr_observations_by_candidate"] = (
            safe_observations
        )
        result["ocr_provenance"] = dict(
            ocr_result.get("provenance", {})
        )
        result["ocr_revision"] = ocr_revision
        result["ocr_raw_observation_count"] = int(
            ocr_result.get("raw_observation_count", 0)
        )
        result["ocr_observations_truncated"] = bool(
            ocr_result.get("observations_truncated", False)
        )
        detector_ids = set(
            result.get("detections_by_candidate", {})
        )
        proposal_candidate_ids = sorted(
            detector_ids | set(ocr_candidate_ids)
        )
        result["proposal_candidate_ids"] = proposal_candidate_ids
        result["proposal_candidate_count"] = len(
            proposal_candidate_ids
        )
        identities = [
            dict(identity)
            for identity in result.get("model_identities", ())
            if isinstance(identity, Mapping)
            and identity.get("pack_id")
            != ocr_result.get("pack_id")
        ]
        if (
            isinstance(ocr_result.get("pack_id"), str)
            and isinstance(ocr_result.get("pack_version"), str)
            and isinstance(
                ocr_result.get("manifest_sha256"), str
            )
            and _SHA256.fullmatch(
                str(ocr_result["manifest_sha256"])
            )
            is not None
        ):
            identities.append(
                {
                    "pack_id": ocr_result["pack_id"],
                    "pack_version": ocr_result["pack_version"],
                    "manifest_sha256": ocr_result[
                        "manifest_sha256"
                    ],
                    "role": "ocr_only",
                }
            )
        result["model_identities"] = sorted(
            identities,
            key=lambda identity: (
                str(identity.get("pack_id", "")),
                str(identity.get("role", "")),
            ),
        )
        result["inference_revision"] = hashlib.sha256(
            _canonical_json(
                {
                    "embedded_revision": local.get(
                        "inference_revision"
                    ),
                    "ocr_revision": ocr_revision,
                }
            )
        ).hexdigest()
        result["review_only"] = True
        result["automatic_labeling_allowed"] = False
        result["annotation_count"] = 0
        result["incident_count"] = 0
        return result

    def _local_vision_from_detection_checkpoint(
        self,
        candidates: Sequence[Mapping[str, Any]],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        checkpoint = context.get("checkpoints", {}).get(
            "detection_ocr", {}
        )
        if not isinstance(checkpoint, Mapping):
            checkpoint = {}
        ocr_revision = checkpoint.get("ocr_revision")
        if ocr_revision is None:
            # Restart compatibility for checkpoints created before the OCR
            # role was split. New detection stages always persist an explicit
            # OCR revision and never use the detector service as OCR.
            return self._summarize_embedded_local_vision(
                candidates,
                context["profile"],
            )
        local = self._summarize_embedded_local_vision(
            candidates,
            context["profile"],
            allow_embedded_ocr=False,
        )
        return self._merge_pipeline_ocr(
            local,
            {
                "status": checkpoint.get("ocr_status"),
                "output_count": checkpoint.get(
                    "ocr_observation_count", 0
                ),
                "failed_candidate_count": checkpoint.get(
                    "ocr_failed_candidate_count", 0
                ),
                "observations_by_candidate": checkpoint.get(
                    "ocr_observations_by_candidate", {}
                ),
                "raw_observation_count": checkpoint.get(
                    "ocr_raw_observation_count", 0
                ),
                "observations_truncated": checkpoint.get(
                    "ocr_observations_truncated", False
                ),
                "pack_id": checkpoint.get("ocr_pack_id"),
                "pack_version": checkpoint.get(
                    "ocr_pack_version"
                ),
                "manifest_sha256": checkpoint.get(
                    "ocr_manifest_sha256"
                ),
                "engine": checkpoint.get("ocr_engine"),
                "ocr_revision": ocr_revision,
                "provenance": checkpoint.get(
                    "ocr_provenance", {}
                ),
            },
        )

    @staticmethod
    def _detection_stage_result(
        *,
        analysis_job_id: UUID,
        source_sha256: str,
        local: Mapping[str, Any],
        dino_proposals: Mapping[str, Mapping[str, Any]],
        dino_status: str,
        evaluation_id: Optional[str],
        evaluated_issue_types: Sequence[str] = (),
        cached: bool = False,
        dino_evidence_source: str = (
            "analysis_representative_frames"
        ),
        high_frequency_evidence_count: int = 0,
        high_frequency_manifest_sha256: Optional[str] = None,
    ) -> PipelineStageResult:
        if dino_status not in {
            "completed",
            "zero_detections",
            "model_unavailable",
            "failed",
        }:
            raise PipelineStageFailure(
                "dino_review_result_invalid"
            )
        if (
            dino_evidence_source
            not in {
                "analysis_representative_frames",
                "candidate_window_rescan_5_10fps",
            }
            or isinstance(high_frequency_evidence_count, bool)
            or not isinstance(high_frequency_evidence_count, int)
            or not 0 <= high_frequency_evidence_count <= 512
            or (
                high_frequency_manifest_sha256 is not None
                and (
                    not isinstance(
                        high_frequency_manifest_sha256, str
                    )
                    or _SHA256.fullmatch(
                        high_frequency_manifest_sha256
                    )
                    is None
                )
            )
            or (
                dino_evidence_source
                == "candidate_window_rescan_5_10fps"
                and (
                    high_frequency_evidence_count <= 0
                    or high_frequency_manifest_sha256 is None
                )
            )
        ):
            raise PipelineStageFailure(
                "dino_review_evidence_invalid"
            )
        dino_candidate_ids: set[str] = set()
        dino_issue_types: set[str] = set()
        dino_prediction_count = 0
        dino_fingerprint_rows: list[dict[str, Any]] = []
        dino_review_proposals_by_candidate: dict[
            str, list[dict[str, Any]]
        ] = {}
        for candidate_id in sorted(dino_proposals):
            proposal = dino_proposals[candidate_id]
            predictions = proposal.get("predictions")
            if (
                re.fullmatch(r"[a-f0-9]{20}", candidate_id) is None
                or not isinstance(predictions, list)
            ):
                raise PipelineStageFailure(
                    "dino_review_result_invalid"
                )
            safe_predictions: list[dict[str, Any]] = []
            for prediction in predictions:
                if not isinstance(prediction, Mapping):
                    raise PipelineStageFailure(
                        "dino_review_result_invalid"
                    )
                try:
                    issue_type = canonical_issue_type(
                        prediction.get("issue_type")
                    )
                except CandidateLabelError as error:
                    raise PipelineStageFailure(
                        "dino_review_result_invalid"
                    ) from error
                score = prediction.get("model_score")
                box = prediction.get("box")
                if (
                    isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(float(score))
                    or not 0.0 <= float(score) <= 1.0
                    or not isinstance(box, Mapping)
                ):
                    raise PipelineStageFailure(
                        "dino_review_result_invalid"
                    )
                safe_predictions.append(
                    {
                        "issue_type": issue_type,
                        "model_score": float(score),
                        "box": dict(box),
                        **{
                            field_name: prediction.get(
                                field_name
                            )
                            for field_name in (
                                "evidence_candidate_id",
                                "evidence_timestamp_seconds",
                                "evidence_image_sha256",
                                "evidence_source",
                            )
                            if prediction.get(field_name)
                            is not None
                        },
                    }
                )
                dino_issue_types.add(issue_type)
            if safe_predictions:
                provenance_fields = (
                    "evidence_candidate_id",
                    "evidence_timestamp_seconds",
                    "evidence_image_sha256",
                    "evidence_source",
                )
                provenance_presence = [
                    all(
                        prediction.get(field_name)
                        is not None
                        for field_name in provenance_fields
                    )
                    for prediction in predictions
                ]
                if any(provenance_presence):
                    if not all(provenance_presence):
                        raise PipelineStageFailure(
                            "dino_review_evidence_invalid"
                        )
                    try:
                        review_rows = (
                            _deduplicated_dino_review_rows(
                                candidate_id,
                                predictions,
                            )
                        )
                    except ValueError as error:
                        raise PipelineStageFailure(
                            "dino_review_evidence_invalid"
                        ) from error
                    if review_rows:
                        dino_review_proposals_by_candidate[
                            candidate_id
                        ] = review_rows
                dino_candidate_ids.add(candidate_id)
                dino_prediction_count += len(safe_predictions)
                dino_fingerprint_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "predictions": safe_predictions,
                    }
                )

        dino_review_proposals_revision = (
            hashlib.sha256(
                _canonical_json(
                    dino_review_proposals_by_candidate
                )
            ).hexdigest()
            if dino_review_proposals_by_candidate
            else None
        )
        local_candidate_ids = set(
            local.get("proposal_candidate_ids", ())
        )
        local_issue_types = set(
            local.get("detected_issue_types", ())
        )
        ocr_candidate_ids = list(
            local.get("ocr_candidate_ids", ())
        )
        detector = local.get("detector")
        ocr = local.get("ocr")
        inference_revision = local.get("inference_revision")
        ocr_revision = local.get("ocr_revision")
        ocr_observations_by_candidate = local.get(
            "ocr_observations_by_candidate", {}
        )
        ocr_provenance = local.get("ocr_provenance", {})
        visual_quality_state = local.get(
            "visual_evidence_quality_state",
            "legacy_not_evaluated",
        )
        visual_quality_revision = local.get(
            "visual_evidence_quality_revision"
        )
        trusted_original_candidate_ids = list(
            local.get(
                "visual_quality_trusted_original_candidate_ids",
                (),
            )
        )
        blocked_parent_candidate_ids = list(
            local.get(
                "visual_quality_blocked_parent_candidate_ids",
                (),
            )
        )
        visual_quality_original_candidate_count = local.get(
            "visual_quality_original_candidate_count",
            len(trusted_original_candidate_ids),
        )
        if (
            not isinstance(detector, Mapping)
            or not isinstance(ocr, Mapping)
            or not isinstance(inference_revision, str)
            or _SHA256.fullmatch(inference_revision) is None
            or (
                ocr_revision is not None
                and (
                    not isinstance(ocr_revision, str)
                    or _SHA256.fullmatch(ocr_revision) is None
                )
            )
            or not isinstance(
                ocr_observations_by_candidate, Mapping
            )
            or not isinstance(ocr_provenance, Mapping)
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in local_candidate_ids
            )
            or any(
                not isinstance(item, str)
                for item in local_issue_types
            )
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in ocr_candidate_ids
            )
            or visual_quality_state
            not in {
                "usable",
                "partial",
                "legacy_not_evaluated",
            }
            or (
                visual_quality_state != "legacy_not_evaluated"
                and (
                    not isinstance(
                        visual_quality_revision, str
                    )
                    or _SHA256.fullmatch(
                        visual_quality_revision
                    )
                    is None
                )
            )
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in [
                    *trusted_original_candidate_ids,
                    *blocked_parent_candidate_ids,
                ]
            )
            or len(trusted_original_candidate_ids)
            != len(set(trusted_original_candidate_ids))
            or len(blocked_parent_candidate_ids)
            != len(set(blocked_parent_candidate_ids))
            or set(trusted_original_candidate_ids).intersection(
                blocked_parent_candidate_ids
            )
            or isinstance(
                visual_quality_original_candidate_count, bool
            )
            or not isinstance(
                visual_quality_original_candidate_count, int
            )
            or visual_quality_original_candidate_count
            < len(trusted_original_candidate_ids)
        ):
            raise PipelineStageFailure(
                "local_vision_inference_invalid"
            )
        proposal_candidate_ids = sorted(
            local_candidate_ids | dino_candidate_ids
        )
        detected_issue_types = sorted(
            local_issue_types | dino_issue_types
        )
        proposal_sources = []
        if local_candidate_ids:
            proposal_sources.append("embedded_local_vision")
        if dino_candidate_ids:
            proposal_sources.append("grounding_dino_review_sidecar")
        if not proposal_sources:
            proposal_sources.append("scene_candidate_fallback")

        dino_revision = hashlib.sha256(
            _canonical_json(
                {
                    "evaluation_id": evaluation_id,
                    "status": dino_status,
                    "rows": dino_fingerprint_rows,
                }
            )
        ).hexdigest()
        detection_revision_payload = {
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "local_inference_revision": inference_revision,
            "dino_revision": dino_revision,
            "dino_evidence_source": dino_evidence_source,
            "high_frequency_evidence_count": (
                high_frequency_evidence_count
            ),
            "high_frequency_manifest_sha256": (
                high_frequency_manifest_sha256
            ),
            "proposal_candidate_ids": proposal_candidate_ids,
            "detected_issue_types": detected_issue_types,
            "ocr_candidate_ids": sorted(ocr_candidate_ids),
            "visual_evidence_quality_state": (
                visual_quality_state
            ),
            "visual_evidence_quality_revision": (
                visual_quality_revision
            ),
            "visual_quality_trusted_original_candidate_ids": (
                sorted(trusted_original_candidate_ids)
            ),
            "visual_quality_blocked_parent_candidate_ids": (
                sorted(blocked_parent_candidate_ids)
            ),
        }
        if dino_review_proposals_revision is not None:
            detection_revision_payload[
                "dino_review_proposals_revision"
            ] = dino_review_proposals_revision
        detection_revision = hashlib.sha256(
            _canonical_json(detection_revision_payload)
        ).hexdigest()

        detector_status = detector.get("status")
        ocr_status = ocr.get("status")
        if "failed" in {detector_status, ocr_status}:
            error_code = "local_vision_inference_failed"
        elif dino_status == "failed":
            error_code = "dino_review_generation_failed"
        elif (
            not proposal_candidate_ids
            and dino_status == "zero_detections"
            and detector_status
            in {"zero_detections", "model_unavailable"}
            and ocr_status
            in {"zero_detections", "model_unavailable"}
        ):
            error_code = "vision_zero_detections"
        elif (
            detector_status == "model_unavailable"
            and ocr_status == "model_unavailable"
        ):
            error_code = "dedicated_detector_ocr_unavailable"
        elif dino_status == "model_unavailable":
            error_code = "dino_review_model_unavailable"
        else:
            error_code = None

        local_prediction_count = int(
            detector.get("output_count", 0)
        )
        ocr_observation_count = int(ocr.get("output_count", 0))
        return PipelineStageResult(
            "success",
            checkpoint={
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": source_sha256,
                "evaluation_id": evaluation_id,
                "detection_revision": detection_revision,
                "local_inference_revision": inference_revision,
                "dino_revision": dino_revision,
                "proposal_candidate_count": len(
                    proposal_candidate_ids
                ),
                "proposal_candidate_ids": proposal_candidate_ids,
                "prediction_count": (
                    local_prediction_count
                    + dino_prediction_count
                ),
                "detected_issue_types": detected_issue_types,
                "proposal_sources": proposal_sources,
                "dino_review_only": True,
                "dino_evidence_source": dino_evidence_source,
                "high_frequency_evidence_count": (
                    high_frequency_evidence_count
                ),
                "high_frequency_manifest_sha256": (
                    high_frequency_manifest_sha256
                ),
                "visual_evidence_quality_state": (
                    visual_quality_state
                ),
                "visual_evidence_quality_revision": (
                    visual_quality_revision
                ),
                "visual_quality_trusted_original_candidate_ids": (
                    sorted(trusted_original_candidate_ids)
                ),
                "visual_quality_blocked_parent_candidate_ids": (
                    sorted(blocked_parent_candidate_ids)
                ),
                "visual_quality_filtered_original_candidate_count": (
                    visual_quality_original_candidate_count
                    - len(trusted_original_candidate_ids)
                ),
                "dino_box_proposal_status": dino_status,
                "dino_proposal_candidate_count": len(
                    dino_candidate_ids
                ),
                "dino_prediction_count": dino_prediction_count,
                "dino_frame_proposal_count": sum(
                    len(rows)
                    for rows
                    in dino_review_proposals_by_candidate.values()
                ),
                **(
                    {
                        "dino_review_proposals_revision": (
                            dino_review_proposals_revision
                        ),
                        "dino_review_proposals_by_candidate": (
                            dino_review_proposals_by_candidate
                        ),
                    }
                    if dino_review_proposals_revision is not None
                    else {}
                ),
                "evaluated_issue_types": sorted(
                    set(evaluated_issue_types)
                ),
                "dedicated_detector_status": detector_status,
                "dedicated_prediction_count": (
                    local_prediction_count
                ),
                "dedicated_detector_missing_types": (
                    list(_DINO_UNSUPPORTED_DEDICATED_TYPES)
                    if detector_status == "model_unavailable"
                    else []
                ),
                "ocr_status": ocr_status,
                "ocr_observation_count": ocr_observation_count,
                "ocr_candidate_ids": sorted(ocr_candidate_ids),
                "ocr_observations_by_candidate": {
                    str(candidate_id): [
                        dict(observation)
                        for observation in observations
                    ]
                    for candidate_id, observations in sorted(
                        ocr_observations_by_candidate.items()
                    )
                    if isinstance(observations, list)
                    and observations
                },
                "ocr_revision": ocr_revision,
                "ocr_pack_id": ocr_provenance.get("pack_id"),
                "ocr_pack_version": ocr_provenance.get(
                    "pack_version"
                ),
                "ocr_manifest_sha256": ocr_provenance.get(
                    "manifest_sha256"
                ),
                "ocr_engine": ocr_provenance.get("engine"),
                "ocr_failed_candidate_count": int(
                    ocr.get("failed_candidate_count", 0)
                ),
                "ocr_raw_observation_count": int(
                    local.get(
                        "ocr_raw_observation_count",
                        ocr_observation_count,
                    )
                ),
                "ocr_observations_truncated": bool(
                    local.get(
                        "ocr_observations_truncated", False
                    )
                ),
                "ocr_provenance": dict(ocr_provenance),
                "local_model_identities": list(
                    local.get("model_identities", ())
                ),
                "role_separation": {
                    "grounding_dino": "review_only_object_boxes",
                    "dedicated_road_model": (
                        "road_defect_and_marking_predictions"
                    ),
                    "ocr": "unverified_text_observations",
                    "sam2": "mask_and_tracking_only",
                    "llm": "semantic_proposal_only",
                },
                "review_only": True,
                "automatic_labeling_allowed": False,
                "annotation_count": 0,
                "incident_count": 0,
            },
            metrics={
                "proposal_candidate_count": len(
                    proposal_candidate_ids
                ),
                "prediction_count": (
                    local_prediction_count
                    + dino_prediction_count
                ),
                "dedicated_prediction_count": (
                    local_prediction_count
                ),
                "ocr_observation_count": ocr_observation_count,
                "dino_prediction_count": dino_prediction_count,
                "dino_frame_proposal_count": sum(
                    len(rows)
                    for rows
                    in dino_review_proposals_by_candidate.values()
                ),
                "high_frequency_evidence_count": (
                    high_frequency_evidence_count
                ),
                "visual_quality_trusted_original_candidate_count": (
                    len(trusted_original_candidate_ids)
                ),
                "visual_quality_blocked_parent_candidate_count": (
                    len(blocked_parent_candidate_ids)
                ),
                "cached": cached,
                "annotation_count": 0,
                "incident_count": 0,
            },
            error_code=error_code,
        )

    def _generate_dino_review_proposals(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        if self._analysis_jobs is None:
            return PipelineStageResult(
                "model_unavailable",
                error_code="analysis_runtime_unavailable",
            )
        analysis_job_id, row, candidates = self._analysis_job_for_context(
            project_id, context
        )
        high_frequency = self._high_frequency_evidence_for_context(
            project_id, analysis_job_id, context
        )
        original_candidates = list(candidates)
        trusted_original_ids = {
            str(candidate.get("candidate_id"))
            for candidate in original_candidates
        }
        if high_frequency is not None:
            evidence_metadata = high_frequency[3]
            eligible_parent_ids = set(
                evidence_metadata.get(
                    "eligible_parent_candidate_ids", ()
                )
            )
            frame_quality = {
                str(item.get("evidence_id")): item
                for item in evidence_metadata.get(
                    "frame_quality_observations", ()
                )
                if isinstance(item, Mapping)
            }
            frame_scope = evidence_metadata.get(
                "frame_parent_scope", {}
            )
            if (
                not eligible_parent_ids
                or not isinstance(frame_scope, Mapping)
                or any(
                    not isinstance(item, str)
                    or re.fullmatch(r"[a-f0-9]{20}", item)
                    is None
                    for item in eligible_parent_ids
                )
            ):
                raise PipelineStageFailure(
                    "visual_evidence_quality_checkpoint_invalid"
                )
            trusted_original_ids = set()
            for candidate in original_candidates:
                candidate_id = str(candidate.get("candidate_id"))
                if candidate_id not in eligible_parent_ids:
                    continue
                representative = candidate.get(
                    "representative_timestamp_seconds"
                )
                if (
                    isinstance(representative, bool)
                    or not isinstance(
                        representative, (int, float)
                    )
                    or not math.isfinite(float(representative))
                ):
                    raise PipelineStageFailure(
                        "visual_evidence_quality_checkpoint_invalid"
                    )
                relevant = []
                for evidence_id, scope in frame_scope.items():
                    observation = frame_quality.get(str(evidence_id))
                    if (
                        not isinstance(scope, Mapping)
                        or not isinstance(observation, Mapping)
                        or candidate_id
                        not in scope.get(
                            "source_candidate_ids", ()
                        )
                    ):
                        continue
                    timestamp = scope.get("timestamp_seconds")
                    if (
                        isinstance(timestamp, bool)
                        or not isinstance(timestamp, (int, float))
                    ):
                        continue
                    relevant.append(
                        (
                            abs(
                                float(timestamp)
                                - float(representative)
                            ),
                            float(timestamp),
                            str(evidence_id),
                            observation,
                        )
                    )
                if not relevant:
                    raise PipelineStageFailure(
                        "visual_evidence_quality_checkpoint_invalid"
                    )
                nearest = min(relevant)[3]
                if nearest.get("strong_unusable") is False:
                    trusted_original_ids.add(candidate_id)
            candidates = [
                candidate
                for candidate in original_candidates
                if str(candidate.get("candidate_id"))
                in trusted_original_ids
            ]
        local = self._summarize_embedded_local_vision(
            candidates,
            context["profile"],
            allow_embedded_ocr=False,
        )
        local["visual_evidence_quality_state"] = (
            str(
                high_frequency[3][
                    "visual_evidence_quality_state"
                ]
            )
            if high_frequency is not None
            else "legacy_not_evaluated"
        )
        local["visual_evidence_quality_revision"] = (
            str(
                high_frequency[3][
                    "visual_evidence_quality_revision"
                ]
            )
            if high_frequency is not None
            else None
        )
        local[
            "visual_quality_trusted_original_candidate_ids"
        ] = sorted(trusted_original_ids)
        local[
            "visual_quality_blocked_parent_candidate_ids"
        ] = (
            list(
                high_frequency[3][
                    "blocked_parent_candidate_ids"
                ]
            )
            if high_frequency is not None
            else []
        )
        local["visual_quality_original_candidate_count"] = len(
            original_candidates
        )
        if candidates:
            try:
                ocr_result = self._run_pipeline_ocr(
                    project_id=project_id,
                    analysis_job_id=analysis_job_id,
                    candidates=candidates,
                    profile=context["profile"],
                    cancel=cancel,
                )
            except VisionInferenceCancelled:
                return PipelineStageResult(
                    "cancelled",
                    checkpoint={
                        "analysis_job_id": str(analysis_job_id),
                        "source_sha256": row["source_sha256"],
                        "ocr_status": "failed",
                        "review_only": True,
                        "automatic_labeling_allowed": False,
                        "annotation_count": 0,
                        "incident_count": 0,
                    },
                    metrics={
                        "annotation_count": 0,
                        "incident_count": 0,
                    },
                    error_code="ocr_inference_cancelled",
                )
        else:
            ocr_result = self._pipeline_ocr_result(
                status="zero_detections",
                profile_pack_id=str(
                    context["profile"]["ocr_pack_id"]
                ),
                runtime_status={},
                candidate_ids=[],
                error_code="visual_evidence_quality_filtered",
            )
        local = self._merge_pipeline_ocr(local, ocr_result)
        dino_ready = (
            self._command_proposal_generator is not None
            and self._experimental_proposals is not None
        )
        status: dict[str, Any] = {}
        if dino_ready:
            status = dict(
                self._command_proposal_generator.status()
            )
            dino_ready = (
                status.get("state") == "ready"
                and status.get("review_only") is True
                and status.get("bulk_frame_detection_provider")
                == "local_model"
            )
        if not dino_ready:
            if (
                local["detector"]["status"]
                == "model_unavailable"
                and local["ocr"]["status"] == "model_unavailable"
            ):
                unavailable = self._detection_stage_result(
                    analysis_job_id=analysis_job_id,
                    source_sha256=row["source_sha256"],
                    local=local,
                    dino_proposals={},
                    dino_status="model_unavailable",
                    evaluation_id=None,
                )
                return PipelineStageResult(
                    "model_unavailable",
                    checkpoint=unavailable.checkpoint,
                    metrics=unavailable.metrics,
                    error_code="vision_models_unavailable",
                )
            return self._detection_stage_result(
                analysis_job_id=analysis_job_id,
                source_sha256=row["source_sha256"],
                local=local,
                dino_proposals={},
                dino_status="model_unavailable",
                evaluation_id=None,
            )

        assert self._experimental_proposals is not None
        assert self._command_proposal_generator is not None
        if high_frequency is None:
            dino_candidates = list(candidates)
            parent_by_evidence = {
                str(candidate["candidate_id"]): str(
                    candidate["candidate_id"]
                )
                for candidate in candidates
            }

            def resolve_dino_image(
                resolved_project_id: UUID,
                resolved_job_id: UUID,
                candidate_id: str,
            ) -> ResolvedAnalysisArtifact:
                evidence_backend = (
                    self._candidate_evidence_registry
                    if self._candidate_evidence_registry is not None
                    else self._analysis_jobs
                )
                return evidence_backend.resolve_artifact(
                    resolved_project_id,
                    resolved_job_id,
                    candidate_id,
                    "image",
                )

            evidence_metadata = {
                "source": "analysis_representative_frames",
                "manifest_sha256": None,
                "input_revision": "analysis-representative-frames",
                "selected_frame_count": len(dino_candidates),
            }
        else:
            (
                dino_candidates,
                parent_by_evidence,
                resolve_dino_image,
                evidence_metadata,
            ) = high_frequency
        dino_candidate_by_id = {
            str(candidate.get("candidate_id")): candidate
            for candidate in dino_candidates
            if isinstance(candidate, Mapping)
        }
        if set(dino_candidate_by_id) != set(parent_by_evidence):
            raise PipelineStageFailure(
                "dino_review_evidence_invalid"
            )
        dino_image_sha256_by_id: dict[str, str] = {}
        evidence_revision = str(
            evidence_metadata["input_revision"]
        )
        evaluation_id = (
            f"pipeline_dino_v2_"
            f"{str(context['job_id']).replace('-', '')}_"
            f"{hashlib.sha256(evidence_revision.encode('utf-8')).hexdigest()[:12]}"
        )
        dino_evidence_source = str(evidence_metadata["source"])
        high_frequency_evidence_count = (
            int(evidence_metadata["selected_frame_count"])
            if high_frequency is not None
            else 0
        )
        high_frequency_manifest_sha256 = (
            str(evidence_metadata["manifest_sha256"])
            if high_frequency is not None
            else None
        )
        existing = self._experimental_proposals.latest_for_job(
            project_id,
            analysis_job_id,
            evaluation_id=evaluation_id,
        )
        if existing:
            return self._detection_stage_result(
                analysis_job_id=analysis_job_id,
                source_sha256=row["source_sha256"],
                local=local,
                dino_proposals=existing,
                dino_status="completed",
                evaluation_id=evaluation_id,
                cached=True,
                dino_evidence_source=dino_evidence_source,
                high_frequency_evidence_count=(
                    high_frequency_evidence_count
                ),
                high_frequency_manifest_sha256=(
                    high_frequency_manifest_sha256
                ),
            )

        proposals_by_candidate: dict[str, list[dict[str, Any]]] = {}
        model = None
        evaluation = None
        evaluated_issue_types: list[str] = []
        generation_batches: list[
            tuple[list[str], CommandProposalGenerationResult]
        ] = []
        supported_issue_types = status.get(
            "supported_issue_types", ()
        )
        taxonomy_generator = getattr(
            self._command_proposal_generator,
            "generate_taxonomy",
            None,
        )
        use_taxonomy_batch = bool(
            callable(taxonomy_generator)
            and isinstance(supported_issue_types, list)
            and supported_issue_types
            and all(
                isinstance(item, str)
                for item in supported_issue_types
            )
        )
        try:
            if use_taxonomy_batch:
                batch_issue_types = [
                    issue_type
                    for issue_type in _DINO_REVIEW_ONLY_ISSUE_TYPES
                    if issue_type in set(supported_issue_types)
                ]
                command_plans = [
                    compile_labeling_command(
                        (
                            "TrafficSuperVision pipeline review "
                            f"{issue_type}"
                        ),
                        explicit_issue_type=issue_type,
                    )
                    for issue_type in batch_issue_types
                ]
                if not command_plans:
                    raise CommandProposalGenerationError(
                        "dino taxonomy has no supported issue types"
                    )
                if cancel():
                    return PipelineStageResult(
                        "cancelled",
                        error_code="dino_review_cancelled",
                    )
                recipe_id = hashlib.sha256(
                    _canonical_json(
                        {
                            "pipeline_job_id": str(
                                context["job_id"]
                            ),
                            "project_id": str(project_id),
                            "analysis_job_id": str(
                                analysis_job_id
                            ),
                            "issue_types": batch_issue_types,
                            "evidence_revision": evidence_revision,
                            "prompt_sha256s": [
                                plan["prompt_sha256"]
                                for plan in command_plans
                            ],
                        }
                    )
                ).hexdigest()
                generated = taxonomy_generator(
                    project_id=project_id,
                    analysis_job_id=analysis_job_id,
                    recipe_id=recipe_id,
                    command_plans=command_plans,
                    candidates=dino_candidates,
                    resolve_image=resolve_dino_image,
                )
                generation_batches.append(
                    (batch_issue_types, generated)
                )
            else:
                for issue_type in _DINO_REVIEW_ONLY_ISSUE_TYPES:
                    if cancel():
                        return PipelineStageResult(
                            "cancelled",
                            error_code="dino_review_cancelled",
                        )
                    command_plan = compile_labeling_command(
                        (
                            "TrafficSuperVision pipeline review "
                            f"{issue_type}"
                        ),
                        explicit_issue_type=issue_type,
                    )
                    recipe_id = hashlib.sha256(
                        _canonical_json(
                            {
                                "pipeline_job_id": str(
                                    context["job_id"]
                                ),
                                "project_id": str(project_id),
                                "analysis_job_id": str(
                                    analysis_job_id
                                ),
                                "issue_type": issue_type,
                                "evidence_revision": (
                                    evidence_revision
                                ),
                                "prompt_sha256": command_plan[
                                    "prompt_sha256"
                                ],
                            }
                        )
                    ).hexdigest()
                    generated = (
                        self._command_proposal_generator.generate(
                            project_id=project_id,
                            analysis_job_id=analysis_job_id,
                            recipe_id=recipe_id,
                            command_plan=command_plan,
                            candidates=dino_candidates,
                            resolve_image=resolve_dino_image,
                        )
                    )
                    generation_batches.append(
                        ([issue_type], generated)
                    )
        except (
            CommandProposalGenerationError,
            HTTPException,
        ) as error:
            if (
                local["detector"]["status"]
                == "model_unavailable"
                and local["ocr"]["status"]
                == "model_unavailable"
            ):
                raise PipelineStageFailure(
                    "dino_review_generation_failed"
                ) from error
            return self._detection_stage_result(
                analysis_job_id=analysis_job_id,
                source_sha256=row["source_sha256"],
                local=local,
                dino_proposals={},
                dino_status="failed",
                evaluation_id=evaluation_id,
                evaluated_issue_types=evaluated_issue_types,
                dino_evidence_source=dino_evidence_source,
                high_frequency_evidence_count=(
                    high_frequency_evidence_count
                ),
                high_frequency_manifest_sha256=(
                    high_frequency_manifest_sha256
                ),
            )

        for generated_issue_types, generated in generation_batches:
            if not generated.available or not generated.generated:
                if (
                    local["detector"]["status"]
                    == "model_unavailable"
                    and local["ocr"]["status"]
                    == "model_unavailable"
                ):
                    unavailable = self._detection_stage_result(
                        analysis_job_id=analysis_job_id,
                        source_sha256=row["source_sha256"],
                        local=local,
                        dino_proposals={},
                        dino_status="model_unavailable",
                        evaluation_id=evaluation_id,
                        evaluated_issue_types=(
                            evaluated_issue_types
                        ),
                        dino_evidence_source=(
                            dino_evidence_source
                        ),
                        high_frequency_evidence_count=(
                            high_frequency_evidence_count
                        ),
                        high_frequency_manifest_sha256=(
                            high_frequency_manifest_sha256
                        ),
                    )
                    return PipelineStageResult(
                        "model_unavailable",
                        checkpoint=unavailable.checkpoint,
                        metrics=unavailable.metrics,
                        error_code="vision_models_unavailable",
                    )
                return self._detection_stage_result(
                    analysis_job_id=analysis_job_id,
                    source_sha256=row["source_sha256"],
                    local=local,
                    dino_proposals={},
                    dino_status="model_unavailable",
                    evaluation_id=evaluation_id,
                    evaluated_issue_types=evaluated_issue_types,
                    dino_evidence_source=dino_evidence_source,
                    high_frequency_evidence_count=(
                        high_frequency_evidence_count
                    ),
                    high_frequency_manifest_sha256=(
                        high_frequency_manifest_sha256
                    ),
                )
            evaluated_issue_types.extend(generated_issue_types)
            if model is None:
                model = generated.model
                evaluation = generated.evaluation
            elif generated.model != model:
                raise PipelineStageFailure(
                    "dino_review_model_changed"
                )
            for proposal in generated.proposals:
                predictions = proposal.get("predictions")
                if not isinstance(predictions, list):
                    raise PipelineStageFailure(
                        "dino_review_result_invalid"
                    )
                evidence_candidate_id = proposal.get("candidate_id")
                parent_candidate_id = parent_by_evidence.get(
                    str(evidence_candidate_id)
                )
                evidence_candidate = dino_candidate_by_id.get(
                    str(evidence_candidate_id)
                )
                if (
                    parent_candidate_id is None
                    or evidence_candidate is None
                ):
                    raise PipelineStageFailure(
                        "dino_review_result_invalid"
                    )
                timestamp = evidence_candidate.get(
                    "representative_timestamp_seconds"
                )
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, (int, float))
                    or not math.isfinite(float(timestamp))
                    or float(timestamp) < 0
                ):
                    raise PipelineStageFailure(
                        "dino_review_evidence_invalid"
                    )
                evidence_id = str(evidence_candidate_id)
                evidence_image_sha256 = (
                    dino_image_sha256_by_id.get(evidence_id)
                )
                if evidence_image_sha256 is None:
                    try:
                        resolved_evidence = resolve_dino_image(
                            project_id,
                            analysis_job_id,
                            evidence_id,
                        )
                    except (HTTPException, OSError) as error:
                        raise PipelineStageFailure(
                            "dino_review_evidence_invalid"
                        ) from error
                    evidence_image_sha256 = (
                        resolved_evidence.sha256
                    )
                    if (
                        not isinstance(evidence_image_sha256, str)
                        or _SHA256.fullmatch(
                            evidence_image_sha256
                        )
                        is None
                    ):
                        raise PipelineStageFailure(
                            "dino_review_evidence_invalid"
                        )
                    dino_image_sha256_by_id[evidence_id] = (
                        evidence_image_sha256
                    )
                evidence_source = (
                    "candidate_window_rescan_5_10fps"
                    if high_frequency is not None
                    else "analysis_representative_frames"
                )
                proposals_by_candidate.setdefault(
                    parent_candidate_id, []
                ).extend(
                    {
                        **dict(item),
                        "evidence_candidate_id": evidence_id,
                        "evidence_timestamp_seconds": float(
                            timestamp
                        ),
                        "evidence_image_sha256": (
                            evidence_image_sha256
                        ),
                        "evidence_source": evidence_source,
                    }
                    for item in predictions
                )

        if model is None or evaluation is None:
            raise PipelineStageFailure("dino_review_result_invalid")
        record_rows = []
        for candidate in original_candidates:
            candidate_id = candidate["candidate_id"]
            predictions = proposals_by_candidate.get(candidate_id, [])
            predictions.sort(
                key=lambda item: (
                    -float(item.get("model_score", 0.0)),
                    str(item.get("issue_type", "")),
                    _canonical_json(item),
                )
            )
            predictions = predictions[:32]
            if not predictions:
                continue
            record_rows.append(
                {
                    "candidate_id": candidate_id,
                    "status": "classified",
                    "predictions": predictions,
                }
            )
        if record_rows:
            try:
                self._experimental_proposals.record_run(
                    project_id,
                    analysis_job_id,
                    model=model,
                    evaluation={
                        **evaluation,
                        "evaluation_id": evaluation_id,
                    },
                    proposals=record_rows,
                )
            except ExperimentalProposalError as error:
                raise PipelineStageFailure(
                    "dino_review_persistence_failed"
                ) from error

        persisted = (
            self._experimental_proposals.latest_for_job(
                project_id,
                analysis_job_id,
                evaluation_id=evaluation_id,
            )
            if record_rows
            else {}
        )
        return self._detection_stage_result(
            analysis_job_id=analysis_job_id,
            source_sha256=row["source_sha256"],
            local=local,
            dino_proposals=persisted,
            dino_status=(
                "completed"
                if persisted
                else "zero_detections"
            ),
            evaluation_id=evaluation_id,
            evaluated_issue_types=evaluated_issue_types,
            dino_evidence_source=dino_evidence_source,
            high_frequency_evidence_count=(
                high_frequency_evidence_count
            ),
            high_frequency_manifest_sha256=(
                high_frequency_manifest_sha256
            ),
        )

    def _merge_dino_review_proposals(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
    ) -> PipelineStageResult:
        analysis_job_id, row, candidates = self._analysis_job_for_context(
            project_id, context
        )
        all_candidates = list(candidates)
        detection_checkpoint = context.get("checkpoints", {}).get(
            "detection_ocr", {}
        )
        if not isinstance(detection_checkpoint, Mapping):
            raise PipelineStageFailure("dino_checkpoint_missing")
        trusted_value = detection_checkpoint.get(
            "visual_quality_trusted_original_candidate_ids"
        )
        if trusted_value is None:
            trusted_original_candidate_ids = {
                str(candidate["candidate_id"])
                for candidate in all_candidates
            }
        elif (
            isinstance(trusted_value, list)
            and len(trusted_value) == len(set(trusted_value))
            and all(
                isinstance(item, str)
                and re.fullmatch(r"[a-f0-9]{20}", item)
                is not None
                for item in trusted_value
            )
        ):
            trusted_original_candidate_ids = set(
                trusted_value
            )
        else:
            raise PipelineStageFailure(
                "visual_evidence_quality_checkpoint_invalid"
            )
        all_candidate_ids = {
            str(candidate["candidate_id"])
            for candidate in all_candidates
        }
        if not trusted_original_candidate_ids.issubset(
            all_candidate_ids
        ):
            raise PipelineStageFailure(
                "visual_evidence_quality_checkpoint_invalid"
            )
        candidates = [
            candidate
            for candidate in all_candidates
            if str(candidate["candidate_id"])
            in trusted_original_candidate_ids
        ]
        local = self._local_vision_from_detection_checkpoint(
            candidates, context
        )
        evaluation_id = detection_checkpoint.get("evaluation_id")
        detection_revision = detection_checkpoint.get(
            "detection_revision"
        )
        if (
            not isinstance(detection_revision, str)
            or _SHA256.fullmatch(detection_revision) is None
        ):
            raise PipelineStageFailure("dino_checkpoint_missing")
        candidate_by_id = {
            str(candidate["candidate_id"]): candidate
            for candidate in all_candidates
        }
        local_detections = []
        for candidate_id, raw_detection in sorted(
            local["detections_by_candidate"].items()
        ):
            if candidate_id not in candidate_by_id:
                raise PipelineStageFailure(
                    "dino_review_result_invalid"
                )
            detection = dict(raw_detection)
            detection.pop("proposal_source", None)
            local_detections.append(detection)
        local_detection_count = len(local_detections)

        raw_dino_by_parent = detection_checkpoint.get(
            "dino_review_proposals_by_candidate", {}
        )
        declared_dino_rows_revision = detection_checkpoint.get(
            "dino_review_proposals_revision"
        )
        if raw_dino_by_parent:
            if (
                not isinstance(raw_dino_by_parent, Mapping)
                or not isinstance(
                    declared_dino_rows_revision, str
                )
                or _SHA256.fullmatch(
                    declared_dino_rows_revision
                )
                is None
                or hashlib.sha256(
                    _canonical_json(raw_dino_by_parent)
                ).hexdigest()
                != declared_dino_rows_revision
            ):
                raise PipelineStageFailure(
                    "dino_review_checkpoint_invalid"
                )
        elif declared_dino_rows_revision is not None:
            raise PipelineStageFailure(
                "dino_review_checkpoint_invalid"
            )
        dino_row_by_proposal: dict[str, dict[str, Any]] = {}
        dino_detections: list[dict[str, Any]] = []
        for parent_candidate_id, rows in sorted(
            raw_dino_by_parent.items()
        ):
            if (
                parent_candidate_id not in candidate_by_id
                or not isinstance(rows, list)
            ):
                raise PipelineStageFailure(
                    "dino_review_checkpoint_invalid"
                )
            for raw in rows:
                if not isinstance(raw, Mapping):
                    raise PipelineStageFailure(
                        "dino_review_checkpoint_invalid"
                    )
                proposal_id = raw.get("proposal_id")
                issue_type = raw.get("issue_type")
                score = raw.get("score")
                timestamp = raw.get(
                    "evidence_timestamp_seconds"
                )
                box = _normalized_dino_box(
                    raw.get("normalized_box")
                )
                evidence_id = raw.get(
                    "evidence_candidate_id"
                )
                evidence_sha256 = raw.get(
                    "evidence_image_sha256"
                )
                if (
                    not isinstance(proposal_id, str)
                    or re.fullmatch(
                        r"dino_[a-f0-9]{24}", proposal_id
                    )
                    is None
                    or proposal_id in dino_row_by_proposal
                    or not isinstance(issue_type, str)
                    or isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not 0 <= float(score) <= 1
                    or isinstance(timestamp, bool)
                    or not isinstance(timestamp, (int, float))
                    or float(timestamp) < 0
                    or box is None
                    or not isinstance(evidence_id, str)
                    or re.fullmatch(
                        r"[a-f0-9]{20}", evidence_id
                    )
                    is None
                    or not isinstance(evidence_sha256, str)
                    or _SHA256.fullmatch(evidence_sha256)
                    is None
                    or raw.get("review_only") is not True
                    or raw.get(
                        "automatic_labeling_allowed"
                    )
                    is not False
                ):
                    raise PipelineStageFailure(
                        "dino_review_checkpoint_invalid"
                    )
                row_copy = dict(raw)
                dino_row_by_proposal[proposal_id] = row_copy
                dino_detections.append(
                    {
                        "detection_id": proposal_id,
                        "timestamp_seconds": float(timestamp),
                        "issue_type": issue_type,
                        "score": float(score),
                        "normalized_box": box,
                        "score_semantics": (
                            "model_score_not_probability"
                        ),
                        "review_only": True,
                        "automatic_labeling_allowed": False,
                    }
                )
        dino_detection_count = len(dino_detections)
        try:
            local_temporal = (
                group_temporal_incident_proposals(
                    local_detections,
                    provenance={
                        "project_id": str(project_id),
                        "analysis_job_id": str(analysis_job_id),
                        "source_file_sha256": row["source_sha256"],
                        "pipeline_job_id": str(context["job_id"]),
                        "evaluation_id": evaluation_id,
                        "detection_revision": detection_revision,
                        "proposal_source": "local_or_ocr",
                    },
                    policy=TemporalGroupingPolicy(),
                )
                if local_detections
                else []
            )
            dino_temporal = (
                group_temporal_incident_proposals(
                    dino_detections,
                    provenance={
                        "project_id": str(project_id),
                        "analysis_job_id": str(analysis_job_id),
                        "source_file_sha256": row["source_sha256"],
                        "pipeline_job_id": str(context["job_id"]),
                        "evaluation_id": evaluation_id,
                        "detection_revision": detection_revision,
                        "dino_review_proposals_revision": (
                            declared_dino_rows_revision
                        ),
                        "proposal_source": (
                            "grounding_dino_review_sidecar"
                        ),
                    },
                    policy=TemporalGroupingPolicy(),
                )
                if dino_detections
                else []
            )
        except TemporalIncidentProposalError as error:
            raise PipelineStageFailure(
                "temporal_merge_failed"
            ) from error

        insertion_by_candidate: dict[str, str] = {}
        grouped_candidate_ids: set[str] = set()
        group_summaries = []
        candidate_order = [
            str(candidate["candidate_id"]) for candidate in candidates
        ]
        for proposal in sorted(
            local_temporal,
            key=lambda item: (
                float(item["start_seconds"]),
                str(item["proposal_id"]),
            ),
        ):
            evidence_ids = [
                str(item)
                for item in proposal["evidence_detection_ids"]
            ]
            representative = str(
                proposal["peak_evidence"]["detection_id"]
            )
            first = next(
                (
                    candidate_id
                    for candidate_id in candidate_order
                    if candidate_id in evidence_ids
                ),
                None,
            )
            if (
                first is None
                or representative not in evidence_ids
                or any(
                    item in grouped_candidate_ids
                    for item in evidence_ids
                )
            ):
                continue
            insertion_by_candidate[first] = representative
            grouped_candidate_ids.update(evidence_ids)
            group_summaries.append(
                {
                    "proposal_id": proposal["proposal_id"],
                    "representative_candidate_id": representative,
                    "evidence_candidate_ids": evidence_ids,
                    "issue_type": proposal["issue_type"],
                    "start_seconds": proposal["start_seconds"],
                    "end_seconds": proposal["end_seconds"],
                    "detection_count": proposal["detection_count"],
                }
            )
        local_question_candidate_ids = []
        locally_proposed_candidate_ids = {
            str(item["detection_id"])
            for item in local_detections
        } | {
            str(item)
            for item in local.get("ocr_candidate_ids", ())
        }
        has_detector_or_ocr_proposals = bool(
            locally_proposed_candidate_ids
            or dino_detections
        )
        for candidate_id in candidate_order:
            representative = insertion_by_candidate.get(candidate_id)
            if representative is not None:
                local_question_candidate_ids.append(representative)
            if (
                candidate_id not in grouped_candidate_ids
                and (
                    not has_detector_or_ocr_proposals
                    or candidate_id
                    in locally_proposed_candidate_ids
                )
            ):
                local_question_candidate_ids.append(candidate_id)

        dino_group_details: dict[str, dict[str, Any]] = {}
        dino_overlay_checkpoint: Optional[dict[str, Any]] = None
        dino_candidate_rows: list[DenseCandidateSeed] = []
        dino_question_timestamps: dict[str, float] = {}
        sam2_checkpoint = context.get("checkpoints", {}).get(
            "sam2_segmentation_tracking", {}
        )
        sam2_binding_by_proposal = {}
        if isinstance(sam2_checkpoint, Mapping):
            for binding in sam2_checkpoint.get(
                "dino_prompt_bindings", ()
            ):
                if (
                    isinstance(binding, Mapping)
                    and isinstance(
                        binding.get("dino_proposal_id"), str
                    )
                    and isinstance(
                        binding.get("prompt_candidate_id"), str
                    )
                ):
                    sam2_binding_by_proposal[
                        str(binding["dino_proposal_id"])
                    ] = str(binding["prompt_candidate_id"])

        # OCR covers glyphs while DINO may cover only an adjacent visible
        # portion of the same sign.  Bind those sources only when an exact
        # parent link, a short time gap, and independently estimated global
        # camera motion all agree.  Ambiguous many-to-one matches remain as
        # separate questions.
        local_group_by_evidence: dict[str, dict[str, Any]] = {}
        for group in group_summaries:
            for evidence_id in group["evidence_candidate_ids"]:
                local_group_by_evidence[str(evidence_id)] = group
        local_detection_by_id = {
            str(item["detection_id"]): item
            for item in local_detections
        }
        raw_ocr_by_candidate = detection_checkpoint.get(
            "ocr_observations_by_candidate", {}
        )
        high_frequency_for_merge = (
            self._high_frequency_evidence_for_context(
                project_id,
                analysis_job_id,
                context,
            )
        )
        high_frequency_resolver = (
            high_frequency_for_merge[2]
            if high_frequency_for_merge is not None
            else None
        )
        candidate_artifact_backend = (
            self._candidate_evidence_registry
            if self._candidate_evidence_registry is not None
            else self._analysis_jobs
        )
        merge_candidates: list[
            tuple[str, str, dict[str, Any]]
        ] = []
        for proposal in dino_temporal:
            if proposal.get("issue_type") != "guide_sign_identity":
                continue
            peak = proposal.get("peak_evidence")
            peak_proposal_id = (
                str(peak.get("detection_id"))
                if isinstance(peak, Mapping)
                else ""
            )
            peak_row = dino_row_by_proposal.get(
                peak_proposal_id
            )
            evidence_rows = [
                dino_row_by_proposal.get(str(item))
                for item in proposal.get(
                    "evidence_detection_ids", ()
                )
            ]
            if (
                peak_row is None
                or not evidence_rows
                or any(item is None for item in evidence_rows)
            ):
                continue
            parent_ids = sorted(
                {
                    str(item["parent_candidate_id"])
                    for item in evidence_rows
                    if isinstance(item, Mapping)
                }
            )
            if len(parent_ids) != 1:
                continue
            parent_candidate_id = parent_ids[0]
            local_group = local_group_by_evidence.get(
                parent_candidate_id
            )
            local_detection = local_detection_by_id.get(
                parent_candidate_id
            )
            local_candidate = candidate_by_id.get(
                parent_candidate_id
            )
            ocr_rows = (
                raw_ocr_by_candidate.get(
                    parent_candidate_id, ()
                )
                if isinstance(raw_ocr_by_candidate, Mapping)
                else ()
            )
            if (
                local_group is None
                or local_group.get("issue_type")
                != "guide_sign_identity"
                or local_detection is None
                or local_candidate is None
                or not isinstance(ocr_rows, Sequence)
                or isinstance(ocr_rows, (str, bytes))
                or high_frequency_resolver is None
            ):
                continue
            try:
                local_artifact = (
                    candidate_artifact_backend.resolve_artifact(
                        project_id,
                        analysis_job_id,
                        parent_candidate_id,
                        "image",
                    )
                )
                dino_artifact = high_frequency_resolver(
                    project_id,
                    analysis_job_id,
                    str(
                        peak_row["evidence_candidate_id"]
                    ),
                )
            except (HTTPException, OSError, ValueError):
                continue
            if (
                dino_artifact.sha256
                != peak_row["evidence_image_sha256"]
            ):
                continue
            proof = evaluate_cross_source_sign_merge(
                local_candidate_id=parent_candidate_id,
                dino_parent_candidate_ids=parent_ids,
                local_issue_type=str(
                    local_detection.get("issue_type", "")
                ),
                dino_issue_type=str(
                    proposal.get("issue_type", "")
                ),
                local_timestamp_seconds=float(
                    local_candidate[
                        "representative_timestamp_seconds"
                    ]
                ),
                dino_timestamp_seconds=float(
                    peak_row["evidence_timestamp_seconds"]
                ),
                local_box=local_detection.get(
                    "normalized_box", {}
                ),
                dino_box=peak_row["normalized_box"],
                ocr_observations=[
                    dict(item)
                    for item in ocr_rows
                    if isinstance(item, Mapping)
                ],
                local_image_path=local_artifact.path,
                local_image_sha256=local_artifact.sha256,
                dino_image_path=dino_artifact.path,
                dino_image_sha256=dino_artifact.sha256,
            )
            if proof is None:
                continue
            proof_without_revision = {
                key: value
                for key, value in proof.items()
                if key != "proof_revision"
            }
            proof_without_revision.update(
                {
                    "local_temporal_proposal_id": (
                        local_group["proposal_id"]
                    ),
                    "local_representative_candidate_id": (
                        local_group[
                            "representative_candidate_id"
                        ]
                    ),
                    "dino_temporal_proposal_id": (
                        proposal["proposal_id"]
                    ),
                    "dino_evidence_proposal_ids": [
                        str(item)
                        for item in proposal[
                            "evidence_detection_ids"
                        ]
                    ],
                    "peak_dino_proposal_id": (
                        peak_proposal_id
                    ),
                }
            )
            proof_without_revision["proof_revision"] = (
                hashlib.sha256(
                    _canonical_json(proof_without_revision)
                ).hexdigest()
            )
            merge_candidates.append(
                (
                    str(proposal["proposal_id"]),
                    str(
                        local_group[
                            "representative_candidate_id"
                        ]
                    ),
                    proof_without_revision,
                )
            )
        local_match_counts: dict[str, int] = {}
        dino_match_counts: dict[str, int] = {}
        for proposal_id, local_id, _proof in merge_candidates:
            local_match_counts[local_id] = (
                local_match_counts.get(local_id, 0) + 1
            )
            dino_match_counts[proposal_id] = (
                dino_match_counts.get(proposal_id, 0) + 1
            )
        cross_source_match_by_dino_proposal = {
            proposal_id: {
                "local_representative_candidate_id": local_id,
                "proof": proof,
            }
            for proposal_id, local_id, proof in merge_candidates
            if local_match_counts[local_id] == 1
            and dino_match_counts[proposal_id] == 1
        }

        for proposal in sorted(
            dino_temporal,
            key=lambda item: (
                float(item["start_seconds"]),
                str(item["proposal_id"]),
            ),
        ):
            evidence_proposal_ids = [
                str(item)
                for item in proposal["evidence_detection_ids"]
            ]
            peak = proposal.get("peak_evidence")
            peak_proposal_id = (
                str(peak.get("detection_id"))
                if isinstance(peak, Mapping)
                else ""
            )
            peak_row = dino_row_by_proposal.get(
                peak_proposal_id
            )
            evidence_rows = [
                dino_row_by_proposal.get(item)
                for item in evidence_proposal_ids
            ]
            if (
                peak_row is None
                or any(item is None for item in evidence_rows)
            ):
                raise PipelineStageFailure(
                    "dino_temporal_evidence_invalid"
                )
            typed_rows = [
                dict(item)
                for item in evidence_rows
                if isinstance(item, Mapping)
            ]
            supporting_frame_ids = tuple(
                dict.fromkeys(
                    str(item["evidence_candidate_id"])
                    for item in typed_rows
                )
            )
            synthetic_identity = {
                "schema_version": (
                    DINO_TEMPORAL_REVIEW_DISCOVERY_SCHEMA
                ),
                "pipeline_job_id": str(context["job_id"]),
                "detection_revision": detection_revision,
                "temporal_proposal_id": proposal["proposal_id"],
                "issue_type": proposal["issue_type"],
                "evidence_proposal_ids": evidence_proposal_ids,
            }
            synthetic_candidate_id = hashlib.sha256(
                _canonical_json(synthetic_identity)
            ).hexdigest()[:20]
            if (
                synthetic_candidate_id in candidate_by_id
                or synthetic_candidate_id
                in dino_group_details
            ):
                raise PipelineStageFailure(
                    "dino_temporal_candidate_collision"
                )
            rescan_checkpoint = context.get(
                "checkpoints", {}
            ).get("candidate_rescan_5_10fps", {})
            evidence_source = str(
                peak_row.get("evidence_source")
            )
            provenance = {
                "schema_version": (
                    "trafficvisson.dino-temporal-evidence.v1"
                ),
                "source_sha256": row["source_sha256"],
                "detection_revision": detection_revision,
                "dino_review_proposals_revision": (
                    declared_dino_rows_revision
                ),
                "temporal_proposal_id": proposal["proposal_id"],
                "peak_dino_proposal_id": peak_proposal_id,
                "peak_evidence_candidate_id": peak_row[
                    "evidence_candidate_id"
                ],
                "peak_evidence_timestamp_seconds": peak_row[
                    "evidence_timestamp_seconds"
                ],
                "peak_evidence_image_sha256": peak_row[
                    "evidence_image_sha256"
                ],
                "peak_normalized_box": dict(
                    peak_row["normalized_box"]
                ),
                "evidence_source": evidence_source,
                "evidence_proposal_ids": evidence_proposal_ids,
                "parent_candidate_ids": sorted(
                    {
                        str(item["parent_candidate_id"])
                        for item in typed_rows
                    }
                ),
                "review_only": True,
            }
            if evidence_source == (
                "candidate_window_rescan_5_10fps"
            ):
                if not isinstance(rescan_checkpoint, Mapping):
                    raise PipelineStageFailure(
                        "candidate_rescan_checkpoint_invalid"
                    )
                provenance.update(
                    {
                        "rescan_run_id": rescan_checkpoint.get(
                            "rescan_run_id"
                        ),
                        "rescan_input_revision": (
                            rescan_checkpoint.get(
                                "rescan_input_revision"
                            )
                        ),
                        "rescan_manifest_sha256": (
                            rescan_checkpoint.get(
                                "rescan_manifest_sha256"
                            )
                        ),
                        "visual_evidence_quality_state": (
                            rescan_checkpoint.get(
                                "visual_evidence_quality_state"
                            )
                        ),
                        "visual_evidence_quality_revision": (
                            rescan_checkpoint.get(
                                "visual_evidence_quality_revision"
                            )
                        ),
                    }
                )
            else:
                provenance["analysis_candidate_id"] = str(
                    peak_row["parent_candidate_id"]
                )
            provenance_sha256 = hashlib.sha256(
                _canonical_json(provenance)
            ).hexdigest()
            seed = DenseCandidateSeed(
                candidate_id=synthetic_candidate_id,
                issue_type=str(proposal["issue_type"]),
                start_seconds=float(proposal["start_seconds"]),
                end_seconds=float(proposal["end_seconds"]),
                representative_timestamp_seconds=float(
                    peak_row["evidence_timestamp_seconds"]
                ),
                representative_frame_id=str(
                    peak_row["evidence_candidate_id"]
                ),
                representative_frame_sha256=str(
                    peak_row["evidence_image_sha256"]
                ),
                score=float(peak_row["score"]),
                normalized_box=dict(
                    peak_row["normalized_box"]
                ),
                ocr_text=None,
                urgent_review=(
                    str(proposal["issue_type"])
                    == "road_debris"
                ),
                supporting_frame_ids=supporting_frame_ids,
                proposal_sources=(
                    "grounding_dino_review_sidecar",
                    "dino_temporal_spatial_grouping_v1",
                ),
                representative_proposal_provenance=provenance,
                proposal_provenance_sha256=provenance_sha256,
            )
            dino_candidate_rows.append(seed)
            dino_question_timestamps[synthetic_candidate_id] = (
                float(seed.representative_timestamp_seconds)
            )
            sam2_prompt_ids = [
                sam2_binding_by_proposal[item]
                for item in evidence_proposal_ids
                if item in sam2_binding_by_proposal
            ]
            dino_group_details[synthetic_candidate_id] = {
                "temporal_proposal_id": proposal["proposal_id"],
                "synthetic_candidate_id": synthetic_candidate_id,
                "issue_type": proposal["issue_type"],
                "start_seconds": proposal["start_seconds"],
                "end_seconds": proposal["end_seconds"],
                "merged_frame_count": proposal[
                    "detection_count"
                ],
                "evidence_proposal_ids": evidence_proposal_ids,
                "evidence_frame_ids": list(
                    supporting_frame_ids
                ),
                "sam2_prompt_candidate_ids": sam2_prompt_ids,
                "peak_evidence": {
                    "dino_proposal_id": peak_proposal_id,
                    "parent_candidate_id": peak_row[
                        "parent_candidate_id"
                    ],
                    "evidence_candidate_id": peak_row[
                        "evidence_candidate_id"
                    ],
                    "timestamp_seconds": peak_row[
                        "evidence_timestamp_seconds"
                    ],
                    "image_sha256": peak_row[
                        "evidence_image_sha256"
                    ],
                    "score": peak_row["score"],
                    "normalized_box": dict(
                        peak_row["normalized_box"]
                    ),
                },
                "review_only": True,
                "automatic_annotation_allowed": False,
                "automatic_incident_creation_allowed": False,
            }
            cross_source_match = (
                cross_source_match_by_dino_proposal.get(
                    str(proposal["proposal_id"])
                )
            )
            if cross_source_match is not None:
                local_representative = str(
                    cross_source_match[
                        "local_representative_candidate_id"
                    ]
                )
                local_group = next(
                    (
                        item
                        for item in group_summaries
                        if item[
                            "representative_candidate_id"
                        ]
                        == local_representative
                    ),
                    None,
                )
                if local_group is None:
                    raise PipelineStageFailure(
                        "cross_source_sign_merge_invalid"
                    )
                combined_start = min(
                    float(local_group["start_seconds"]),
                    float(proposal["start_seconds"]),
                )
                combined_end = max(
                    float(local_group["end_seconds"]),
                    float(proposal["end_seconds"]),
                )
                dino_group_details[synthetic_candidate_id].update(
                    {
                        "start_seconds": combined_start,
                        "end_seconds": combined_end,
                        "cross_source_merge_proof": dict(
                            cross_source_match["proof"]
                        ),
                        "cross_source_local_candidate_ids": list(
                            local_group[
                                "evidence_candidate_ids"
                            ]
                        ),
                    }
                )

        if dino_candidate_rows:
            if self._candidate_evidence_overlays is None:
                raise PipelineStageFailure(
                    "candidate_evidence_overlay_unavailable"
                )
            rescan_checkpoint = context.get(
                "checkpoints", {}
            ).get("candidate_rescan_5_10fps", {})
            rescan_manifest_sha256 = (
                rescan_checkpoint.get(
                    "rescan_manifest_sha256"
                )
                if isinstance(rescan_checkpoint, Mapping)
                else None
            )
            discovery_evidence_revision = (
                rescan_manifest_sha256
                if isinstance(rescan_manifest_sha256, str)
                and _SHA256.fullmatch(rescan_manifest_sha256)
                else detection_revision
            )
            sampling_fps = (
                int(
                    rescan_checkpoint.get(
                        "actual_rescan_fps", 5
                    )
                )
                if isinstance(rescan_checkpoint, Mapping)
                and not isinstance(
                    rescan_checkpoint.get(
                        "actual_rescan_fps", 5
                    ),
                    bool,
                )
                and isinstance(
                    rescan_checkpoint.get(
                        "actual_rescan_fps", 5
                    ),
                    (int, float),
                )
                else 5
            )
            sampling_fps = min(10, max(5, sampling_fps))
            provider_states = (
                {
                    "provider": (
                        "grounding_dino_temporal_review"
                    ),
                    "state": "ready",
                    "evaluation_id": evaluation_id,
                    "dino_revision": detection_checkpoint.get(
                        "dino_revision"
                    ),
                    "dino_review_proposals_revision": (
                        declared_dino_rows_revision
                    ),
                    "review_only": True,
                },
            )
            discovery_payload = {
                "schema_version": (
                    DINO_TEMPORAL_REVIEW_DISCOVERY_SCHEMA
                ),
                "project_id": str(project_id),
                "source_sha256": row["source_sha256"],
                "dense_index_sha256": (
                    discovery_evidence_revision
                ),
                "sampling_fps": sampling_fps,
                "scanned_frame_count": len(
                    dino_row_by_proposal
                ),
                "provider_states": [
                    dict(item) for item in provider_states
                ],
                "candidates": [
                    asdict(item)
                    for item in dino_candidate_rows
                ],
                "review_only": True,
                "automatic_annotation_creation": False,
                "automatic_incident_creation": False,
                "automatic_training_ingest": False,
                "automatic_report_truth": False,
            }
            discovery = DenseCandidateDiscoveryResult(
                schema_version=(
                    DINO_TEMPORAL_REVIEW_DISCOVERY_SCHEMA
                ),
                project_id=str(project_id),
                source_sha256=row["source_sha256"],
                dense_index_sha256=(
                    discovery_evidence_revision
                ),
                sampling_fps=sampling_fps,
                scanned_frame_count=len(
                    dino_row_by_proposal
                ),
                candidates=tuple(dino_candidate_rows),
                provider_states=provider_states,
                discovery_revision=hashlib.sha256(
                    _canonical_json(discovery_payload)
                ).hexdigest(),
            )
            try:
                _, candidate_evidence_root = (
                    self._pipeline_artifact_directory(
                        project_id,
                        context,
                        "candidate-evidence",
                    )
                )
                overlay_kwargs = {
                    "project_id": project_id,
                    "analysis_job_id": analysis_job_id,
                    "pipeline_job_id": UUID(
                        str(context["job_id"])
                    ),
                    "discovery": discovery,
                }
                if isinstance(
                    self._candidate_evidence_overlays,
                    CandidateEvidenceOverlayStore,
                ):
                    overlay_kwargs["artifact_root"] = (
                        candidate_evidence_root
                    )
                overlay_result = (
                    self._candidate_evidence_overlays.materialize(
                        **overlay_kwargs
                    )
                )
                dino_overlay_checkpoint = (
                    overlay_result.to_checkpoint()
                )
                if self._candidate_evidence_registry is not None:
                    self._candidate_evidence_registry.bind_overlay(
                        project_id,
                        analysis_job_id,
                        UUID(str(context["job_id"])),
                        overlay_result.overlay_id,
                    )
            except (
                CandidateEvidenceOverlayError,
                TypeError,
                ValueError,
            ) as error:
                raise PipelineStageFailure(
                    "dino_temporal_overlay_failed"
                ) from error
            merged_local_candidate_ids = {
                str(
                    detail[
                        "cross_source_merge_proof"
                    ][
                        "local_representative_candidate_id"
                    ]
                )
                for detail in dino_group_details.values()
                if isinstance(
                    detail.get("cross_source_merge_proof"),
                    Mapping,
                )
            }
            local_question_candidate_ids = [
                candidate_id
                for candidate_id in local_question_candidate_ids
                if candidate_id not in merged_local_candidate_ids
            ]
            group_summaries = [
                item
                for item in group_summaries
                if item["representative_candidate_id"]
                not in merged_local_candidate_ids
            ]
            for synthetic_id, detail in dino_group_details.items():
                combined_evidence_ids = [
                    *list(
                        detail.get(
                            "cross_source_local_candidate_ids",
                            (),
                        )
                    ),
                    synthetic_id,
                ]
                group_summaries.append(
                    {
                        "proposal_id": detail[
                            "temporal_proposal_id"
                        ],
                        "representative_candidate_id": (
                            synthetic_id
                        ),
                        "evidence_candidate_ids": (
                            combined_evidence_ids
                        ),
                        "issue_type": detail["issue_type"],
                        "start_seconds": detail[
                            "start_seconds"
                        ],
                        "end_seconds": detail["end_seconds"],
                        "detection_count": len(
                            combined_evidence_ids
                        ),
                    }
                )

        question_entries = [
            (
                float(
                    candidate_by_id[candidate_id][
                        "representative_timestamp_seconds"
                    ]
                ),
                0,
                candidate_id,
            )
            for candidate_id in local_question_candidate_ids
        ]
        question_entries.extend(
            (
                timestamp,
                1,
                candidate_id,
            )
            for candidate_id, timestamp
            in dino_question_timestamps.items()
        )
        question_candidate_ids = [
            item[2] for item in sorted(question_entries)
        ]
        if (
            not question_candidate_ids
            or len(question_candidate_ids)
            != len(set(question_candidate_ids))
        ):
            raise PipelineStageFailure("temporal_merge_result_invalid")
        revision_payload = {
            "analysis_job_id": str(analysis_job_id),
            "evaluation_id": evaluation_id,
            "detection_revision": detection_revision,
            "question_candidate_ids": question_candidate_ids,
            "groups": group_summaries,
        }
        if dino_group_details:
            revision_payload[
                "dino_temporal_groups_by_candidate"
            ] = dino_group_details
            revision_payload[
                "dino_candidate_evidence_overlay"
            ] = dino_overlay_checkpoint
        revision = hashlib.sha256(
            _canonical_json(revision_payload)
        ).hexdigest()
        return PipelineStageResult(
            "success",
            checkpoint={
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": row["source_sha256"],
                "evaluation_id": evaluation_id,
                "detection_revision": detection_revision,
                "temporal_merge_revision": revision,
                "question_candidate_ids": question_candidate_ids,
                "groups": group_summaries,
                **(
                    {
                        "dino_temporal_groups_by_candidate": (
                            dino_group_details
                        ),
                        "dino_candidate_evidence_overlay_id": (
                            dino_overlay_checkpoint["overlay_id"]
                        ),
                        "dino_candidate_evidence_overlay": (
                            dino_overlay_checkpoint
                        ),
                    }
                    if dino_overlay_checkpoint is not None
                    else {}
                ),
                "proposal_sources": list(
                    detection_checkpoint.get(
                        "proposal_sources", ()
                    )
                ),
                "dedicated_detector_status": (
                    detection_checkpoint.get(
                        "dedicated_detector_status"
                    )
                ),
                "ocr_status": detection_checkpoint.get(
                    "ocr_status"
                ),
                "ocr_observation_count": (
                    detection_checkpoint.get(
                        "ocr_observation_count", 0
                    )
                ),
                "ocr_candidate_ids": list(
                    detection_checkpoint.get(
                        "ocr_candidate_ids", ()
                    )
                ),
                "review_only": True,
                "automatic_incident_creation_allowed": False,
                "automatic_annotation_allowed": False,
            },
            metrics={
                "input_detection_count": (
                    len(local_detections)
                    + len(dino_detections)
                ),
                "dedicated_detection_count": local_detection_count,
                "dino_detection_count": dino_detection_count,
                "temporal_group_count": len(group_summaries),
                "dino_temporal_group_count": len(
                    dino_group_details
                ),
                "cross_source_sign_merge_count": sum(
                    1
                    for detail in dino_group_details.values()
                    if isinstance(
                        detail.get(
                            "cross_source_merge_proof"
                        ),
                        Mapping,
                    )
                ),
                "question_count": len(question_candidate_ids),
                "incident_count": 0,
                "annotation_count": 0,
            },
        )

    def _generate_sam2_review_proposals(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        if (
            self._sam2_adapter is None
            or context.get("capabilities", {}).get(
                "sam2_segmentation_tracking"
            )
            != "ready"
        ):
            detection_checkpoint = context.get(
                "checkpoints", {}
            ).get("detection_ocr", {})
            dino_box_count = (
                int(
                    detection_checkpoint.get(
                        "dino_frame_proposal_count", 0
                    )
                )
                if isinstance(detection_checkpoint, Mapping)
                and isinstance(
                    detection_checkpoint.get(
                        "dino_frame_proposal_count", 0
                    ),
                    int,
                )
                and not isinstance(
                    detection_checkpoint.get(
                        "dino_frame_proposal_count", 0
                    ),
                    bool,
                )
                else 0
            )
            return PipelineStageResult(
                "success",
                checkpoint={
                    "state": "model_unavailable",
                    "mask_generated": False,
                    "tracking_performed": False,
                    "region_edit_requires_sam2": True,
                    "review_only": True,
                    "requires_user_confirmation": True,
                    "automatic_annotation_allowed": False,
                    "automatic_incident_creation_allowed": False,
                    "training_membership_allowed": False,
                    "dino_prompt_count": 0,
                    "dino_mask_count": 0,
                    "dino_failure_count": 0,
                    "dino_box_only_review_count": (
                        dino_box_count
                    ),
                    "dino_prompt_bindings": [],
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                metrics={
                    "prompt_count": 0,
                    "mask_count": 0,
                    "tracked_candidate_count": 0,
                    "tracked_frame_count": 0,
                    "failure_count": 0,
                    "dino_prompt_count": 0,
                    "dino_mask_count": 0,
                    "dino_failure_count": 0,
                    "dino_box_only_review_count": (
                        dino_box_count
                    ),
                    "annotation_count": 0,
                    "incident_count": 0,
                },
                error_code="sam2_model_unavailable",
            )
        (
            analysis_job_id,
            _row,
            candidates,
        ) = self._analysis_job_for_context(project_id, context)
        local = self._local_vision_from_detection_checkpoint(
            candidates, context
        )
        boxes: dict[str, tuple[float, Mapping[str, float]]] = {}
        for candidate_id, detection in local[
            "detections_by_candidate"
        ].items():
            box = detection.get("normalized_box")
            score = detection.get("score")
            if (
                isinstance(box, Mapping)
                and not isinstance(score, bool)
                and isinstance(score, (int, float))
            ):
                boxes[str(candidate_id)] = (float(score), dict(box))
        detection_checkpoint = context.get("checkpoints", {}).get(
            "detection_ocr", {}
        )
        raw_dino_by_parent = (
            detection_checkpoint.get(
                "dino_review_proposals_by_candidate", {}
            )
            if isinstance(detection_checkpoint, Mapping)
            else {}
        )
        dino_prompt_rows: list[dict[str, Any]] = []
        if raw_dino_by_parent:
            declared_revision = detection_checkpoint.get(
                "dino_review_proposals_revision"
            )
            if (
                not isinstance(raw_dino_by_parent, Mapping)
                or not isinstance(declared_revision, str)
                or _SHA256.fullmatch(declared_revision) is None
                or hashlib.sha256(
                    _canonical_json(raw_dino_by_parent)
                ).hexdigest()
                != declared_revision
            ):
                raise PipelineStageFailure(
                    "dino_review_checkpoint_invalid"
                )
            for parent_candidate_id, rows in sorted(
                raw_dino_by_parent.items()
            ):
                if (
                    not isinstance(parent_candidate_id, str)
                    or not isinstance(rows, list)
                ):
                    raise PipelineStageFailure(
                        "dino_review_checkpoint_invalid"
                    )
                for raw in rows:
                    if not isinstance(raw, Mapping):
                        raise PipelineStageFailure(
                            "dino_review_checkpoint_invalid"
                        )
                    box = _normalized_dino_box(
                        raw.get("normalized_box")
                    )
                    score = raw.get("score")
                    issue_type = raw.get("issue_type")
                    proposal_id = raw.get("proposal_id")
                    evidence_id = raw.get(
                        "evidence_candidate_id"
                    )
                    timestamp = raw.get(
                        "evidence_timestamp_seconds"
                    )
                    image_sha256 = raw.get(
                        "evidence_image_sha256"
                    )
                    if (
                        box is None
                        or isinstance(score, bool)
                        or not isinstance(score, (int, float))
                        or not 0 <= float(score) <= 1
                        or not isinstance(issue_type, str)
                        or not isinstance(proposal_id, str)
                        or re.fullmatch(
                            r"dino_[a-f0-9]{24}",
                            proposal_id,
                        )
                        is None
                        or not isinstance(evidence_id, str)
                        or re.fullmatch(
                            r"[a-f0-9]{20}", evidence_id
                        )
                        is None
                        or isinstance(timestamp, bool)
                        or not isinstance(
                            timestamp, (int, float)
                        )
                        or float(timestamp) < 0
                        or not isinstance(image_sha256, str)
                        or _SHA256.fullmatch(image_sha256)
                        is None
                    ):
                        raise PipelineStageFailure(
                            "dino_review_checkpoint_invalid"
                        )
                    dino_prompt_rows.append(
                        {
                            **dict(raw),
                            "parent_candidate_id": (
                                parent_candidate_id
                            ),
                            "normalized_box": box,
                            "score": float(score),
                        }
                    )
        candidate_by_id = {
            str(candidate["candidate_id"]): candidate
            for candidate in candidates
        }
        high_frequency = self._high_frequency_evidence_for_context(
            project_id,
            analysis_job_id,
            context,
        )
        evidence_by_parent: dict[
            str, list[tuple[dict[str, Any], ResolvedAnalysisArtifact]]
        ] = {}
        dino_parent_ids = {
            str(item["parent_candidate_id"])
            for item in dino_prompt_rows
        }
        if high_frequency is not None:
            (
                high_frequency_candidates,
                parent_by_evidence,
                resolve_high_frequency,
                _metadata,
            ) = high_frequency
            for evidence in high_frequency_candidates:
                evidence_id = str(evidence["candidate_id"])
                parent_id = parent_by_evidence.get(evidence_id)
                if (
                    parent_id not in boxes
                    and parent_id not in dino_parent_ids
                ):
                    continue
                try:
                    artifact = resolve_high_frequency(
                        project_id,
                        analysis_job_id,
                        evidence_id,
                    )
                except HTTPException as error:
                    raise PipelineStageFailure(
                        "candidate_rescan_manifest_invalid"
                    ) from error
                evidence_by_parent.setdefault(
                    str(parent_id), []
                ).append((dict(evidence), artifact))
        local_prompts: list[Sam2CandidatePrompt] = []
        prompt_temporal_context_by_candidate: dict[
            str, dict[str, Any]
        ] = {}
        for candidate_id, (_score, box) in sorted(
            boxes.items(),
            key=lambda item: (-item[1][0], item[0]),
        ):
            candidate = candidate_by_id.get(candidate_id)
            if candidate is None:
                continue
            candidate_timestamp = candidate.get(
                "representative_timestamp_seconds"
            )
            if (
                isinstance(candidate_timestamp, bool)
                or not isinstance(candidate_timestamp, (int, float))
            ):
                raise PipelineStageFailure(
                    "analysis_checkpoint_invalid"
                )
            candidate_timestamp = float(candidate_timestamp)
            raw_frames = evidence_by_parent.get(candidate_id, [])
            if raw_frames:
                all_frames = tuple(
                    Sam2FrameEvidence(
                        evidence_ref=(
                            f"rescan:{metadata['candidate_id']}"
                        ),
                        path=artifact.path,
                        sha256=artifact.sha256,
                        timestamp_seconds=float(
                            metadata[
                                "representative_timestamp_seconds"
                            ]
                        ),
                    )
                    for metadata, artifact in raw_frames
                )
                (
                    frames,
                    prompt_index,
                    temporal_context,
                ) = select_balanced_sam2_frames(
                    all_frames,
                    target_timestamp_seconds=candidate_timestamp,
                    maximum_frames=(
                        MAX_SAM2_FRAMES_PER_CANDIDATE
                    ),
                )
            else:
                try:
                    evidence_backend = (
                        self._candidate_evidence_registry
                        if self._candidate_evidence_registry is not None
                        else self._analysis_jobs
                    )
                    artifact = evidence_backend.resolve_artifact(
                        project_id,
                        analysis_job_id,
                        candidate_id,
                        "image",
                    )
                except HTTPException as error:
                    raise PipelineStageFailure(
                        "analysis_candidate_artifact_unavailable"
                    ) from error
                (
                    frames,
                    prompt_index,
                    temporal_context,
                ) = select_balanced_sam2_frames(
                    (
                    Sam2FrameEvidence(
                        evidence_ref=(
                            f"candidate:{candidate_id}"
                        ),
                        path=artifact.path,
                        sha256=artifact.sha256,
                        timestamp_seconds=candidate_timestamp,
                    ),
                    ),
                    target_timestamp_seconds=candidate_timestamp,
                    maximum_frames=(
                        MAX_SAM2_FRAMES_PER_CANDIDATE
                    ),
                )
            prompt_temporal_context_by_candidate[candidate_id] = dict(
                temporal_context
            )
            local_prompts.append(
                Sam2CandidatePrompt(
                    candidate_id=candidate_id,
                    normalized_box=box,
                    frames=frames,
                    prompt_frame_index=prompt_index,
                )
            )
        dino_prompts: list[Sam2CandidatePrompt] = []
        dino_binding_by_prompt: dict[str, dict[str, Any]] = {}
        for dino_row in sorted(
            dino_prompt_rows,
            key=lambda item: (
                0
                if item.get("issue_type") == "road_debris"
                else 1,
                -float(item["score"]),
                float(item["evidence_timestamp_seconds"]),
                str(item["proposal_id"]),
            ),
        ):
            parent_candidate_id = str(
                dino_row["parent_candidate_id"]
            )
            evidence_id = str(
                dino_row["evidence_candidate_id"]
            )
            timestamp = float(
                dino_row["evidence_timestamp_seconds"]
            )
            raw_frames = evidence_by_parent.get(
                parent_candidate_id, []
            )
            if raw_frames:
                all_frames = tuple(
                    Sam2FrameEvidence(
                        evidence_ref=(
                            f"rescan:{metadata['candidate_id']}"
                        ),
                        path=artifact.path,
                        sha256=artifact.sha256,
                        timestamp_seconds=float(
                            metadata[
                                "representative_timestamp_seconds"
                            ]
                        ),
                    )
                    for metadata, artifact in raw_frames
                )
                (
                    frames,
                    prompt_index,
                    temporal_context,
                ) = select_balanced_sam2_frames(
                    all_frames,
                    target_timestamp_seconds=timestamp,
                    required_evidence_ref=(
                        f"rescan:{evidence_id}"
                    ),
                    maximum_frames=(
                        MAX_SAM2_FRAMES_PER_CANDIDATE
                    ),
                )
            else:
                try:
                    evidence_backend = (
                        self._candidate_evidence_registry
                        if self._candidate_evidence_registry
                        is not None
                        else self._analysis_jobs
                    )
                    artifact = evidence_backend.resolve_artifact(
                        project_id,
                        analysis_job_id,
                        parent_candidate_id,
                        "image",
                    )
                except HTTPException as error:
                    raise PipelineStageFailure(
                        "dino_sam2_evidence_invalid"
                    ) from error
                (
                    frames,
                    prompt_index,
                    temporal_context,
                ) = select_balanced_sam2_frames(
                    (
                    Sam2FrameEvidence(
                        evidence_ref=(
                            f"candidate:{parent_candidate_id}"
                        ),
                        path=artifact.path,
                        sha256=artifact.sha256,
                        timestamp_seconds=timestamp,
                    ),
                    ),
                    target_timestamp_seconds=timestamp,
                    maximum_frames=(
                        MAX_SAM2_FRAMES_PER_CANDIDATE
                    ),
                )
            if (
                prompt_index < 0
                or frames[prompt_index].sha256
                != dino_row["evidence_image_sha256"]
            ):
                raise PipelineStageFailure(
                    "dino_sam2_evidence_invalid"
                )
            prompt_candidate_id = hashlib.sha256(
                _canonical_json(
                    [
                        "dino-sam2-prompt.v1",
                        str(context["job_id"]),
                        dino_row["proposal_id"],
                    ]
                )
            ).hexdigest()[:20]
            if (
                prompt_candidate_id in candidate_by_id
                or prompt_candidate_id
                in dino_binding_by_prompt
            ):
                raise PipelineStageFailure(
                    "dino_sam2_prompt_collision"
                )
            prompt_temporal_context_by_candidate[
                prompt_candidate_id
            ] = dict(temporal_context)
            dino_prompts.append(
                Sam2CandidatePrompt(
                    candidate_id=prompt_candidate_id,
                    normalized_box=dict(
                        dino_row["normalized_box"]
                    ),
                    frames=frames,
                    prompt_frame_index=prompt_index,
                )
            )
            dino_binding_by_prompt[prompt_candidate_id] = {
                "prompt_candidate_id": prompt_candidate_id,
                "dino_proposal_id": dino_row["proposal_id"],
                "parent_candidate_id": parent_candidate_id,
                "evidence_candidate_id": evidence_id,
                "evidence_timestamp_seconds": timestamp,
                "evidence_image_sha256": dino_row[
                    "evidence_image_sha256"
                ],
                "issue_type": dino_row["issue_type"],
                "score": dino_row["score"],
                "normalized_box": dict(
                    dino_row["normalized_box"]
                ),
                "prompt_evidence_ref": frames[
                    prompt_index
                ].evidence_ref,
                "review_only": True,
            }
        prompts = dino_prompts + local_prompts
        try:
            result = run_pipeline_sam2_batches(
                adapter=self._sam2_adapter,
                prompts=prompts,
                cancel=cancel,
            )
        except PipelineSam2StageError as error:
            raise PipelineStageFailure(
                str(error)
                if _SAFE_ERROR_CODE.fullmatch(str(error))
                else "sam2_stage_failed"
            ) from error
        if result["status"] == "cancelled":
            return PipelineStageResult(
                "cancelled",
                error_code=str(result["error_code"]),
            )
        checkpoint = dict(result["checkpoint"])
        metrics = dict(result["metrics"])
        proposal_sha_by_prompt = {
            str(item.get("candidate_id")): str(
                item.get("proposal_sha256")
            )
            for item in checkpoint.get("proposals", ())
            if isinstance(item, Mapping)
            and isinstance(item.get("candidate_id"), str)
            and isinstance(item.get("proposal_sha256"), str)
        }
        failure_code_by_prompt = {
            str(item.get("candidate_id")): str(
                item.get("error_code")
            )
            for item in checkpoint.get("failures", ())
            if isinstance(item, Mapping)
            and isinstance(item.get("candidate_id"), str)
            and isinstance(item.get("error_code"), str)
        }
        dino_prompt_bindings = []
        for prompt_candidate_id, raw_binding in sorted(
            dino_binding_by_prompt.items()
        ):
            proposal_sha256 = proposal_sha_by_prompt.get(
                prompt_candidate_id
            )
            failure_code = failure_code_by_prompt.get(
                prompt_candidate_id
            )
            status = (
                "available"
                if proposal_sha256 is not None
                else "failed"
            )
            dino_prompt_bindings.append(
                {
                    **raw_binding,
                    "sam2_status": status,
                    "sam2_proposal_sha256": proposal_sha256,
                    "sam2_error_code": (
                        None
                        if status == "available"
                        else (
                            failure_code
                            or "sam2_inference_failed"
                        )
                    ),
                }
            )
        dino_mask_count = sum(
            item["sam2_status"] == "available"
            for item in dino_prompt_bindings
        )
        dino_failure_count = (
            len(dino_prompt_bindings) - dino_mask_count
        )
        temporal_context_revision = hashlib.sha256(
            _canonical_json(
                prompt_temporal_context_by_candidate
            )
        ).hexdigest()
        full_temporal_context_prompt_count = sum(
            item.get("before_context_included") is True
            and item.get("after_context_included") is True
            for item in (
                prompt_temporal_context_by_candidate.values()
            )
        )
        checkpoint.pop("sam2_revision", None)
        checkpoint.update(
            {
                "dino_prompt_count": len(
                    dino_prompt_bindings
                ),
                "dino_mask_count": dino_mask_count,
                "dino_failure_count": dino_failure_count,
                "dino_box_only_review_count": (
                    dino_failure_count
                ),
                "dino_prompt_bindings": dino_prompt_bindings,
                "prompt_temporal_context_by_candidate": {
                    key: dict(value)
                    for key, value in sorted(
                        prompt_temporal_context_by_candidate.items()
                    )
                },
                "prompt_temporal_context_revision": (
                    temporal_context_revision
                ),
            }
        )
        checkpoint["sam2_revision"] = hashlib.sha256(
            _canonical_json(checkpoint)
        ).hexdigest()
        metrics.update(
            {
                "dino_prompt_count": len(
                    dino_prompt_bindings
                ),
                "dino_mask_count": dino_mask_count,
                "dino_failure_count": dino_failure_count,
                "dino_box_only_review_count": (
                    dino_failure_count
                ),
                "full_temporal_context_prompt_count": (
                    full_temporal_context_prompt_count
                ),
                "missing_temporal_context_prompt_count": (
                    len(prompts)
                    - full_temporal_context_prompt_count
                ),
            }
        )
        if (
            result["status"] == "failed"
            and not dino_prompt_bindings
        ):
            return PipelineStageResult(
                "failed",
                checkpoint=checkpoint,
                metrics=metrics,
                error_code=str(result["error_code"]),
            )
        return PipelineStageResult(
            "success",
            checkpoint=checkpoint,
            metrics=metrics,
            error_code=(
                "sam2_box_only_review"
                if (
                    result["status"] == "failed"
                    and dino_prompt_bindings
                )
                else str(result["error_code"])
                if result.get("error_code") is not None
                else None
            ),
        )

    def _search_visual_reference_cases(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
    ) -> PipelineStageResult:
        assert self._weak_case_visual_index is not None
        (
            analysis_job_id,
            _row,
            candidates,
        ) = self._analysis_job_for_context(project_id, context)
        temporal = context.get("checkpoints", {}).get(
            "temporal_merge", {}
        )
        candidate_ids = temporal.get("question_candidate_ids")
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or len(candidate_ids) != len(set(candidate_ids))
        ):
            raise PipelineStageFailure(
                "temporal_merge_checkpoint_missing"
            )
        local = self._local_vision_from_detection_checkpoint(
            candidates, context
        )
        issue_by_candidate = {
            str(candidate_id): str(detection["issue_type"])
            for candidate_id, detection in local[
                "detections_by_candidate"
            ].items()
            if isinstance(detection.get("issue_type"), str)
        }
        detection = context.get("checkpoints", {}).get(
            "detection_ocr", {}
        )
        evaluation_id = detection.get("evaluation_id")
        if (
            isinstance(evaluation_id, str)
            and self._experimental_proposals is not None
        ):
            proposals = self._experimental_proposals.latest_for_job(
                project_id,
                analysis_job_id,
                evaluation_id=evaluation_id,
            )
            for candidate_id, proposal in proposals.items():
                if candidate_id in issue_by_candidate:
                    continue
                predictions = proposal.get("predictions")
                if (
                    isinstance(predictions, list)
                    and predictions
                    and isinstance(predictions[0], Mapping)
                    and isinstance(
                        predictions[0].get("issue_type"), str
                    )
                ):
                    issue_by_candidate[str(candidate_id)] = str(
                        predictions[0]["issue_type"]
                    )
        try:
            build = self._weak_case_visual_index.build(project_id)
        except WeakCaseVisualIndexError:
            return PipelineStageResult(
                "success",
                checkpoint={
                    "state": "capability_missing",
                    "search_mode": "visual_index_unavailable",
                    "semantic_image_embedding_used": False,
                    "matches_by_candidate": {},
                    "approved_reference_count": 0,
                    "review_required_before_promotion": True,
                    "review_only": True,
                },
                metrics={
                    "candidate_search_count": 0,
                    "reference_count": 0,
                },
                error_code="visual_reference_index_unavailable",
            )
        if len(candidate_ids) > MAX_VISUAL_REFERENCE_CANDIDATES:
            raise PipelineStageFailure(
                "visual_reference_candidate_limit_exceeded"
            )

        matches_by_candidate: dict[str, list[dict[str, Any]]] = {}
        reference_count = 0
        approved_reference_count = 0

        def resolve_candidate_evidence(
            candidate_id: str,
        ) -> CandidateFrameEvidence:
            try:
                evidence_backend = (
                    self._candidate_evidence_registry
                    if self._candidate_evidence_registry is not None
                    else self._analysis_jobs
                )
                artifact = evidence_backend.resolve_artifact(
                    project_id,
                    analysis_job_id,
                    candidate_id,
                    "image",
                )
                if not 1 <= artifact.size_bytes <= 64 * 1024 * 1024:
                    raise PipelineStageFailure(
                        "analysis_candidate_artifact_unavailable"
                    )
                body = artifact.path.read_bytes()
            except (HTTPException, OSError) as error:
                raise PipelineStageFailure(
                    "analysis_candidate_artifact_unavailable"
                ) from error
            if (
                len(body) != artifact.size_bytes
                or hashlib.sha256(body).hexdigest()
                != artifact.sha256
            ):
                raise PipelineStageFailure(
                    "analysis_candidate_artifact_changed"
                )
            return CandidateFrameEvidence(
                project_id=project_id,
                candidate_id=candidate_id,
                body=body,
                media_type=artifact.media_type,
                expected_sha256=artifact.sha256,
            )

        def legacy_query(
            candidate_id: str,
            evidence: CandidateFrameEvidence,
        ) -> Mapping[str, Any]:
            try:
                return self._weak_case_visual_index.query(
                    project_id,
                    evidence,
                    issue_type=issue_by_candidate.get(candidate_id),
                    query_text=None,
                    limit=MAX_VISUAL_REFERENCES_PER_CANDIDATE,
                )
            except WeakCaseVisualIndexError as error:
                if error.code == "embedding_batch_oom":
                    raise PipelineStageFailure(
                        "visual_reference_embedding_batch_oom"
                    ) from error
                return {
                    "issue_type": issue_by_candidate.get(candidate_id),
                    "cases": [],
                }

        def compact_case(
            case: Mapping[str, Any],
            *,
            query_issue_type: Any,
        ) -> dict[str, Any]:
            safe = {
                key: case.get(key)
                for key in (
                    "source_id",
                    "case_id",
                    "quarter",
                    "photo_page_number",
                    "document_sha256",
                    "image_sha256",
                    "index_manifest_sha256",
                    "picture_target_id",
                    "cell_target_id",
                    "reference_state",
                    "review_required",
                    "approved_reference",
                    "training_eligible",
                    "approval_record_sha256",
                    "approved_issue_type",
                    "approved_action",
                    "visual_cosine_score",
                    "text_structure_score",
                    "combined_similarity_score",
                )
                if key in case and case.get(key) is not None
            }
            for key, maximum_bytes in (
                ("caption", MAX_VISUAL_REFERENCE_CAPTION_BYTES),
                ("route", MAX_VISUAL_REFERENCE_ROUTE_BYTES),
                ("location", MAX_VISUAL_REFERENCE_LOCATION_BYTES),
            ):
                text = _bounded_utf8_text(
                    case.get(key), maximum_bytes
                )
                if text is not None:
                    safe[key] = text
            opinion = _bounded_utf8_text(
                case.get(
                    "opinion", case.get("inspection_opinion")
                ),
                MAX_VISUAL_REFERENCE_OPINION_BYTES,
            )
            if opinion is not None:
                safe["inspection_opinion"] = opinion
            approved = case.get("approved_reference") is True
            issue_type = (
                case.get("approved_issue_type")
                if approved
                else query_issue_type
            )
            action = (
                case.get("approved_action") if approved else None
            )
            if isinstance(issue_type, str):
                safe["issue_type"] = issue_type
            if isinstance(action, str):
                safe["maintenance_action"] = action
            return safe

        query_many = getattr(
            self._weak_case_visual_index, "query_many", None
        )
        batch_count = 0
        for offset in range(
            0,
            len(candidate_ids),
            MAX_VISUAL_REFERENCE_QUERY_CHUNK,
        ):
            batch_ids = candidate_ids[
                offset : offset + MAX_VISUAL_REFERENCE_QUERY_CHUNK
            ]
            evidences = [
                resolve_candidate_evidence(candidate_id)
                for candidate_id in batch_ids
            ]
            batch_count += 1
            if callable(query_many):
                try:
                    batch_results = query_many(
                        project_id,
                        evidences,
                        issue_types_by_candidate={
                            candidate_id: issue_by_candidate.get(
                                candidate_id
                            )
                            for candidate_id in batch_ids
                        },
                        query_texts_by_candidate=None,
                        limit=(
                            MAX_VISUAL_REFERENCES_PER_CANDIDATE
                        ),
                    )
                except WeakCaseVisualIndexError as error:
                    if error.code == "embedding_batch_oom":
                        raise PipelineStageFailure(
                            "visual_reference_embedding_batch_oom"
                        ) from error
                    raise PipelineStageFailure(
                        "visual_reference_batch_failed"
                    ) from error
                if (
                    not isinstance(batch_results, Mapping)
                    or set(batch_results) != set(batch_ids)
                    or any(
                        not isinstance(
                            batch_results.get(candidate_id), Mapping
                        )
                        for candidate_id in batch_ids
                    )
                ):
                    raise PipelineStageFailure(
                        "visual_reference_batch_invalid"
                    )
            else:
                batch_results = {
                    candidate_id: legacy_query(
                        candidate_id, evidence
                    )
                    for candidate_id, evidence in zip(
                        batch_ids, evidences
                    )
                }

            for candidate_id in batch_ids:
                result = batch_results[candidate_id]
                raw_cases = result.get("cases", ())
                if not isinstance(raw_cases, (list, tuple)):
                    raw_cases = ()
                safe_cases = []
                for case in raw_cases[
                    :MAX_VISUAL_REFERENCES_PER_CANDIDATE
                ]:
                    if not isinstance(case, Mapping):
                        continue
                    safe = compact_case(
                        case,
                        query_issue_type=result.get("issue_type"),
                    )
                    safe_cases.append(safe)
                    reference_count += 1
                    if case.get("approved_reference") is True:
                        approved_reference_count += 1
                matches_by_candidate[candidate_id] = safe_cases
        reference_revision = hashlib.sha256(
            _canonical_json(
                {
                    "visual_index_manifest_sha256": build[
                        "index_manifest_sha256"
                    ],
                    "matches_by_candidate": matches_by_candidate,
                }
            )
        ).hexdigest()
        return PipelineStageResult(
            "success",
            checkpoint={
                "state": "ready",
                "search_mode": (
                    "local_clip_visual_plus_text_structure"
                ),
                "semantic_image_embedding_used": True,
                "visual_index_manifest_sha256": build[
                    "index_manifest_sha256"
                ],
                "visual_embedding_model_identity_sha256": build[
                    "model_identity_sha256"
                ],
                "visual_reference_revision": reference_revision,
                "candidate_count": len(candidate_ids),
                "candidate_order_sha256": hashlib.sha256(
                    _canonical_json(candidate_ids)
                ).hexdigest(),
                "query_chunk_size": (
                    MAX_VISUAL_REFERENCE_QUERY_CHUNK
                ),
                "matches_per_candidate_limit": (
                    MAX_VISUAL_REFERENCES_PER_CANDIDATE
                ),
                "matches_by_candidate": matches_by_candidate,
                "approved_reference_count": approved_reference_count,
                "review_required_before_promotion": True,
                # Approved historical references may inform this proposal,
                # but the current-video result remains review-only.
                "review_only": True,
                "automatic_annotation_allowed": False,
                "automatic_incident_creation_allowed": False,
                "training_membership_allowed": False,
            },
            metrics={
                "candidate_search_count": len(
                    matches_by_candidate
                ),
                "query_chunk_count": batch_count,
                "reference_count": reference_count,
                "approved_reference_count": (
                    approved_reference_count
                ),
                "annotation_count": 0,
                "incident_count": 0,
            },
            error_code=(
                None
                if reference_count
                else "weak_case_matches_unavailable"
            ),
        )

    def _search_weak_reference_cases(
        self, context: Mapping[str, Any]
    ) -> PipelineStageResult:
        if self._weak_case_catalog is None:
            return PipelineStageResult(
                "success",
                checkpoint={
                    "state": "capability_missing",
                    "references": [],
                    "approved_reference_count": 0,
                    "review_only": True,
                },
                metrics={"reference_count": 0},
                error_code="weak_case_catalog_unavailable",
            )
        detection_checkpoint = context.get("checkpoints", {}).get(
            "detection_ocr", {}
        )
        issue_types = detection_checkpoint.get(
            "detected_issue_types", []
        )
        if not isinstance(issue_types, list):
            raise PipelineStageFailure("dino_checkpoint_missing")
        references = []
        approved_reference_count = 0
        source_state = "not_applicable"
        try:
            project_id = UUID(str(context["project_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "pipeline_project_identity_missing"
            ) from error
        if self._weak_case_visual_index is not None:
            return self._search_visual_reference_cases(
                project_id,
                context,
            )
        try:
            for issue_type in sorted(set(issue_types)):
                if issue_type == "other":
                    continue
                result = self._weak_case_catalog.search(
                    issue_type=str(issue_type),
                    query_text=None,
                    limit=3,
                )
                if self._weak_case_approvals is not None:
                    result = self._weak_case_approvals.decorate_search(
                        project_id,
                        result,
                    )
                source_state = str(
                    result.get("capability_state", "unavailable")
                )
                for case in result.get("cases", ()):
                    if not isinstance(case, Mapping):
                        continue
                    identity = (
                        str(case.get("source_id")),
                        str(case.get("case_id")),
                    )
                    if any(
                        (
                            item["source_id"],
                            item["case_id"],
                        )
                        == identity
                        for item in references
                    ):
                        continue
                    references.append(
                        {
                            "source_id": identity[0],
                            "case_id": identity[1],
                            "issue_type": issue_type,
                            "quarter": case.get("quarter"),
                            "document_sha256": case.get(
                                "document_sha256"
                            ),
                            "image_sha256": case.get("image_sha256"),
                            "index_manifest_sha256": case.get(
                                "index_manifest_sha256"
                            ),
                            "picture_target_id": case.get(
                                "picture_target_id"
                            ),
                            "cell_target_id": case.get(
                                "cell_target_id"
                            ),
                            "reference_state": case.get(
                                "reference_state",
                                "weak_unconfirmed",
                            ),
                            "review_required": bool(
                                case.get("review_required", True)
                            ),
                            "approval_record_sha256": case.get(
                                "approval_record_sha256"
                            ),
                            "approved_reference": bool(
                                case.get(
                                    "approved_reference", False
                                )
                            ),
                            "training_eligible": bool(
                                case.get(
                                    "training_eligible", False
                                )
                            ),
                            "approved_issue_type": case.get(
                                "approved_issue_type"
                            ),
                            "approved_action": case.get(
                                "approved_action"
                            ),
                            "caption": case.get("caption"),
                            "route": case.get("route"),
                            "location": case.get("location"),
                            "opinion": case.get("opinion"),
                        }
                    )
                    if case.get("approved_reference") is True:
                        approved_reference_count += 1
                    if len(references) >= 5:
                        break
                if len(references) >= 5:
                    break
        except WeakCaseCatalogError:
            return PipelineStageResult(
                "success",
                checkpoint={
                    "state": "capability_missing",
                    "references": [],
                    "approved_reference_count": 0,
                    "review_only": True,
                },
                metrics={"reference_count": 0},
                error_code="weak_case_search_unavailable",
            )
        return PipelineStageResult(
            "success",
            checkpoint={
                "state": source_state,
                "search_mode": (
                    "bounded_text_and_structure_metadata"
                ),
                "semantic_image_embedding_used": False,
                "references": references,
                "approved_reference_count": approved_reference_count,
                "review_required_before_promotion": True,
                "review_only": True,
            },
            metrics={"reference_count": len(references)},
            error_code=(
                None
                if references
                else "weak_case_matches_unavailable"
            ),
        )

    def _structured_provider_snapshot(
        self,
        profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        provider = str(profile.get("report_provider", "none"))
        consent = (
            profile.get("report_transmission_consent") is True
        )
        state = "disabled"
        capabilities: list[str] = []
        if (
            provider in {"codex", "claude"}
            and self._report_provider_status is not None
        ):
            try:
                status = self._report_provider_status(provider)
                state = str(
                    getattr(status, "state", "")
                    or (
                        status.get("state")
                        if isinstance(status, Mapping)
                        else ""
                    )
                    or "unavailable"
                )
                raw_capabilities = (
                    getattr(status, "capabilities", ())
                    if not isinstance(status, Mapping)
                    else status.get("capabilities", ())
                )
                if isinstance(raw_capabilities, (list, tuple)):
                    capabilities = [
                        str(item) for item in raw_capabilities
                    ]
            except Exception:
                state = "unavailable"
        if self._structured_vision_judgment is None:
            reason = "structured_vision_service_unavailable"
        elif provider not in {"codex", "claude"}:
            reason = "structured_vision_provider_not_configured"
        elif not consent:
            reason = "transmission_consent_required"
        elif state != "ready" or "structured_judgment" not in set(
            capabilities
        ):
            reason = "provider_not_ready"
        else:
            reason = None
        return {
            "provider": provider,
            "consent": consent,
            "state": state,
            "capabilities": capabilities,
            "ready": reason is None,
            "not_run_reason": reason,
        }

    @staticmethod
    def _local_structured_judgment(
        *,
        candidate_id: str,
        provider: str,
        status: str,
        error_code: str,
    ) -> dict[str, Any]:
        if status not in {"not_run", "failed"}:
            raise PipelineStageFailure(
                "structured_judgment_status_invalid"
            )
        return {
            "schema_version": (
                "trafficvisson.structured-vision-proposal.v1"
            ),
            "status": status,
            "provider": provider,
            "candidate_id": candidate_id,
            "comparison_mode": (
                STRUCTURED_VISION_COMPARISON_MODE
            ),
            "review_state": "none",
            "review_only": True,
            "automatic_annotation_allowed": False,
            "automatic_incident_allowed": False,
            "automatic_training_membership_allowed": False,
            "automatic_report_truth_allowed": False,
            "llm_bulk_frame_detection_used": False,
            "direct_visual_inspection_claimed": False,
            "provider_command_invoked": False,
            "proposal": None,
            "proposal_sha256": None,
            "error_code": error_code,
        }

    @staticmethod
    def _validated_structured_judgment(
        value: Any,
        *,
        candidate_id: str,
        provider: str,
    ) -> dict[str, Any]:
        raw = (
            value.as_dict()
            if callable(getattr(value, "as_dict", None))
            else value
        )
        if not isinstance(raw, Mapping):
            raise PipelineStageFailure(
                "structured_judgment_result_invalid"
            )
        result = dict(raw)
        status = result.get("status")
        proposal = result.get("proposal")
        comparison_mode = result.get("comparison_mode")
        direct_visual = result.get(
            "direct_visual_inspection_claimed"
        )
        attachment_count = result.get("attachment_count", 0)
        attachment_sha256s = result.get("attachment_sha256s", [])
        attachment_manifest_sha256 = result.get(
            "attachment_manifest_sha256"
        )
        if comparison_mode == STRUCTURED_VISION_COMPARISON_MODE:
            attachment_contract_valid = (
                direct_visual is False
                and attachment_count == 0
                and attachment_sha256s in ([], ())
            )
        elif (
            comparison_mode
            == STRUCTURED_VISION_DIRECT_COMPARISON_MODE
        ):
            attachment_contract_valid = (
                isinstance(direct_visual, bool)
                and direct_visual == (status == "proposed")
                and isinstance(attachment_count, int)
                and not isinstance(attachment_count, bool)
                and 4 <= attachment_count <= 6
                and isinstance(attachment_sha256s, list)
                and len(attachment_sha256s) == attachment_count
                and all(
                    isinstance(item, str)
                    and _SHA256.fullmatch(item) is not None
                    for item in attachment_sha256s
                )
                and isinstance(
                    attachment_manifest_sha256, str
                )
                and _SHA256.fullmatch(
                    attachment_manifest_sha256
                )
                is not None
            )
        else:
            attachment_contract_valid = False
        if (
            status not in {"proposed", "not_run", "failed"}
            or result.get("candidate_id") != candidate_id
            or result.get("provider") != provider
            or not attachment_contract_valid
            or result.get("review_only") is not True
            or result.get("automatic_annotation_allowed")
            is not False
            or result.get("automatic_incident_allowed")
            is not False
            or result.get(
                "automatic_training_membership_allowed"
            )
            is not False
            or result.get("automatic_report_truth_allowed")
            is not False
            or result.get("llm_bulk_frame_detection_used")
            is not False
            or not isinstance(
                result.get("provider_command_invoked"), bool
            )
        ):
            raise PipelineStageFailure(
                "structured_judgment_result_invalid"
            )
        if status == "proposed":
            if (
                not isinstance(proposal, Mapping)
                or proposal.get("candidate_id") != candidate_id
                or proposal.get("review_state") != "proposed"
                or proposal.get("review_only") is not True
                or proposal.get("automatic_annotation_allowed")
                is not False
                or proposal.get("automatic_incident_allowed")
                is not False
                or proposal.get(
                    "automatic_training_membership_allowed"
                )
                is not False
                or proposal.get("automatic_report_truth_allowed")
                is not False
                or proposal.get("comparison_mode")
                != comparison_mode
                or result.get("error_code") is not None
            ):
                raise PipelineStageFailure(
                    "structured_judgment_result_invalid"
                )
        elif proposal is not None or not isinstance(
            result.get("error_code"), str
        ):
            raise PipelineStageFailure(
                "structured_judgment_result_invalid"
            )
        return json.loads(
            _canonical_json(result).decode("utf-8")
        )

    @staticmethod
    def _structured_detector_rows(
        *,
        candidate_id: str,
        local: Mapping[str, Any],
        dino: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        local_detection = local.get(
            "detections_by_candidate", {}
        )
        if isinstance(local_detection, Mapping):
            detection = local_detection.get(candidate_id)
            if isinstance(detection, Mapping):
                semantics = detection.get("score_semantics")
                if semantics == "zero_shot_relative_not_probability":
                    semantics = "zero_shot_relative"
                rows.append(
                    {
                        "candidate_id": candidate_id,
                        "role": "dedicated_road_model",
                        "issue_type": detection.get("issue_type"),
                        "score": detection.get("score"),
                        "score_semantics": semantics,
                        "box": detection.get("normalized_box"),
                        "source_ref": (
                            f"analysis:{candidate_id}:"
                            "dedicated-detector-1"
                        ),
                    }
                )
        dino_row = dino.get(candidate_id)
        predictions = (
            dino_row.get("predictions")
            if isinstance(dino_row, Mapping)
            else None
        )
        if isinstance(predictions, list):
            for index, prediction in enumerate(
                predictions[:7],
                start=1,
            ):
                if not isinstance(prediction, Mapping):
                    continue
                rows.append(
                    {
                        "candidate_id": candidate_id,
                        "role": "grounding_dino_box",
                        "issue_type": prediction.get("issue_type"),
                        "score": prediction.get("model_score"),
                        "score_semantics": (
                            "model_score_not_probability"
                        ),
                        "box": prediction.get("box"),
                        "source_ref": (
                            f"analysis:{candidate_id}:"
                            f"dino-detector-{index}"
                        ),
                    }
                )
        return rows[:8]

    @staticmethod
    def _structured_ocr_rows(
        *,
        candidate_id: str,
        candidate: Mapping[str, Any],
        local: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        if candidate_id not in set(
            local.get("ocr_candidate_ids", ())
        ):
            return []
        observations_by_candidate = local.get(
            "ocr_observations_by_candidate"
        )
        observations = (
            observations_by_candidate.get(candidate_id)
            if isinstance(observations_by_candidate, Mapping)
            else None
        )
        if observations is None:
            inference = candidate.get("ai_inference")
            observations = (
                inference.get("ocr_observations")
                if isinstance(inference, Mapping)
                else None
            )
        if not isinstance(observations, list):
            return []
        rows = []
        for index, observation in enumerate(
            observations[:16],
            start=1,
        ):
            if not isinstance(observation, Mapping):
                continue
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "text": observation.get("text"),
                    "confidence": observation.get("confidence"),
                    "box": observation.get("box"),
                    "source_ref": (
                        f"analysis:{candidate_id}:ocr-{index}"
                    ),
                }
            )
        return rows

    @staticmethod
    def _sam2_proposal_candidate_for_review(
        *,
        candidate_id: str,
        temporal: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
    ) -> str:
        proposals = checkpoint.get("proposals")
        if isinstance(proposals, list) and any(
            isinstance(item, Mapping)
            and item.get("candidate_id") == candidate_id
            for item in proposals
        ):
            return candidate_id
        groups = temporal.get(
            "dino_temporal_groups_by_candidate"
        )
        group = (
            groups.get(candidate_id)
            if isinstance(groups, Mapping)
            else None
        )
        peak = (
            group.get("peak_evidence")
            if isinstance(group, Mapping)
            else None
        )
        peak_proposal_id = (
            peak.get("dino_proposal_id")
            if isinstance(peak, Mapping)
            else None
        )
        declared = (
            group.get("sam2_prompt_candidate_ids")
            if isinstance(group, Mapping)
            else None
        )
        bindings = checkpoint.get("dino_prompt_bindings")
        if (
            not isinstance(peak_proposal_id, str)
            or not isinstance(declared, list)
            or not isinstance(bindings, list)
        ):
            return candidate_id
        matches = [
            str(binding["prompt_candidate_id"])
            for binding in bindings
            if isinstance(binding, Mapping)
            and binding.get("dino_proposal_id")
            == peak_proposal_id
            and binding.get("prompt_candidate_id") in declared
            and binding.get("sam2_status") == "available"
            and binding.get("sam2_error_code") is None
            and isinstance(
                binding.get("prompt_candidate_id"), str
            )
        ]
        if (
            len(matches) == 1
            and re.fullmatch(r"[a-f0-9]{20}", matches[0])
            is not None
        ):
            return matches[0]
        return candidate_id

    @staticmethod
    def _structured_sam2_summary(
        *,
        candidate_id: str,
        checkpoint: Mapping[str, Any],
        proposal_candidate_id: Optional[str] = None,
    ) -> dict[str, Any]:
        resolved_candidate_id = (
            proposal_candidate_id or candidate_id
        )
        proposals = checkpoint.get("proposals")
        proposal = next(
            (
                item
                for item in proposals
                if isinstance(item, Mapping)
                and item.get("candidate_id")
                == resolved_candidate_id
            ),
            None,
        ) if isinstance(proposals, list) else None
        if isinstance(proposal, Mapping):
            proposal_sha256 = proposal.get("proposal_sha256")
            if (
                not isinstance(proposal_sha256, str)
                or _SHA256.fullmatch(proposal_sha256) is None
            ):
                raise PipelineStageFailure(
                    "sam2_checkpoint_invalid"
                )
            tracked_frames = proposal.get("tracked_frames")
            if not isinstance(tracked_frames, list):
                raise PipelineStageFailure(
                    "sam2_checkpoint_invalid"
                )
            tracking = proposal.get("tracking_performed") is True
            return {
                "candidate_id": candidate_id,
                "status": "available",
                "mask_ref": (
                    f"evidence:{candidate_id}:"
                    f"sam2-mask-{proposal_sha256[:16]}"
                ),
                "track_ref": (
                    f"evidence:{candidate_id}:"
                    f"sam2-track-{proposal_sha256[:16]}"
                    if tracking
                    else None
                ),
                "tracked_frame_count": len(tracked_frames),
                "mask_box": proposal.get("normalized_box"),
            }
        failure_rows = checkpoint.get("failures")
        failed = bool(
            isinstance(failure_rows, list)
            and any(
                isinstance(item, Mapping)
                and item.get("candidate_id")
                == resolved_candidate_id
                for item in failure_rows
            )
        )
        state = checkpoint.get("state")
        status = (
            "failed"
            if failed or state == "failed"
            else "model_unavailable"
            if state == "model_unavailable"
            else "not_requested"
        )
        return {
            "candidate_id": candidate_id,
            "status": status,
            "mask_ref": None,
            "track_ref": None,
            "tracked_frame_count": 0,
            "mask_box": None,
        }

    @staticmethod
    def _structured_temporal_summary(
        *,
        candidate_id: str,
        candidate: Mapping[str, Any],
        temporal: Mapping[str, Any],
        gps: Mapping[str, Any],
    ) -> dict[str, Any]:
        group = next(
            (
                item
                for item in temporal.get("groups", ())
                if isinstance(item, Mapping)
                and item.get("representative_candidate_id")
                == candidate_id
            ),
            None,
        )
        timestamp = candidate.get(
            "representative_timestamp_seconds"
        )
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or float(timestamp) < 0
        ):
            raise PipelineStageFailure(
                "analysis_checkpoint_invalid"
            )
        if isinstance(group, Mapping):
            start_seconds = group.get("start_seconds")
            end_seconds = group.get("end_seconds")
            evidence_ids = group.get("evidence_candidate_ids")
            merged_frame_count = (
                len(evidence_ids)
                if isinstance(evidence_ids, list)
                else group.get("detection_count")
            )
        else:
            start_seconds = float(timestamp)
            end_seconds = float(timestamp)
            merged_frame_count = 1
        timed_revision = gps.get("timed_track_revision")
        timed_available = (
            gps.get("timed_track_status") == "available"
            and isinstance(timed_revision, str)
            and _SHA256.fullmatch(timed_revision) is not None
        )
        return {
            "candidate_id": candidate_id,
            "merged_frame_count": merged_frame_count,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "gps_state": (
                "timed_track_available"
                if timed_available
                else "gpx_required"
            ),
            "timed_gps_ref": (
                f"analysis:{candidate_id}:"
                f"timed-gps-{timed_revision[:16]}"
                if timed_available
                else None
            ),
        }

    @staticmethod
    def _structured_similar_cases(
        *,
        candidate_id: str,
        checkpoint: Mapping[str, Any],
        approved_by_identity: Mapping[
            tuple[str, str], Mapping[str, Any]
        ],
    ) -> list[dict[str, Any]]:
        matches_by_candidate = checkpoint.get(
            "matches_by_candidate"
        )
        if isinstance(matches_by_candidate, Mapping):
            raw_matches = matches_by_candidate.get(
                candidate_id, ()
            )
        else:
            raw_matches = checkpoint.get("references", ())
        if not isinstance(raw_matches, (list, tuple)):
            return []
        result = []
        seen: set[tuple[str, str]] = set()
        for match in raw_matches:
            if not isinstance(match, Mapping):
                continue
            identity = (
                str(match.get("source_id", "")),
                str(match.get("case_id", "")),
            )
            approved = approved_by_identity.get(identity)
            if identity in seen:
                continue
            approved_ready = (
                approved is not None
                and match.get("reference_state")
                == "approved_reference"
                and match.get("approved_reference") is True
                and match.get("approval_record_sha256")
                == approved.get("record_sha256")
                and approved.get("reference_state")
                == "approved_reference"
                and approved.get("approved_reference") is True
                and approved.get("user_confirmed") is True
            )
            weak_ready = (
                approved is None
                and match.get("reference_state")
                == "weak_unconfirmed"
                and match.get("approved_reference") is False
                and match.get("review_required") is True
                and match.get("training_eligible") is False
                and match.get("approval_record_sha256") is None
            )
            if not (approved_ready or weak_ready):
                continue
            source = approved if approved_ready else match
            try:
                issue_type = canonical_issue_type(
                    source.get(
                        "issue_type",
                        match.get("issue_type"),
                    )
                )
            except CandidateLabelError:
                continue
            quarter = source.get("quarter")
            if quarter in {3, "3", "3Q"}:
                quarter_label = "3Q"
            elif quarter in {4, "4", "4Q"}:
                quarter_label = "4Q"
            else:
                continue
            document_sha256 = source.get("document_sha256")
            photo_sha256 = source.get(
                "image_sha256",
                source.get("photo_sha256"),
            )
            if (
                not isinstance(document_sha256, str)
                or _SHA256.fullmatch(document_sha256) is None
                or not isinstance(photo_sha256, str)
                or _SHA256.fullmatch(photo_sha256) is None
            ):
                continue
            action = source.get("maintenance_action")
            if (
                not isinstance(action, str)
                or not action.strip()
            ):
                defaults = _STRUCTURED_VISION_DEFAULT_ACTIONS.get(
                    issue_type,
                    (),
                )
                action = defaults[0] if defaults else None
            if not isinstance(action, str) or not action:
                continue
            seen.add(identity)
            result.append(
                {
                    "source_id": identity[0],
                    "case_id": identity[1],
                    "approval_record_sha256": (
                        source.get("record_sha256")
                        if approved_ready
                        else None
                    ),
                    "document_sha256": document_sha256,
                    "photo_sha256": photo_sha256,
                    "quarter": quarter_label,
                    "route": source.get("route"),
                    "caption": source.get("caption"),
                    "inspection_opinion": source.get(
                        "opinion",
                        source.get("inspection_opinion"),
                    ),
                    "issue_type": issue_type,
                    "maintenance_action": action,
                    "reference_state": (
                        "approved_reference"
                        if approved_ready
                        else "weak_unconfirmed"
                    ),
                    "approved_reference": approved_ready,
                    "user_verified": approved_ready,
                    "source_ref": (
                        (
                            "reference:"
                            if approved_ready
                            else "weak-case:"
                        )
                        + f"{identity[0]}:{identity[1]}"
                    ),
                }
            )
            if len(result) >= 5:
                break
        return result

    @staticmethod
    def _structured_taxonomy(
        similar_cases: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        actions = {
            issue_type: list(defaults)
            for issue_type, defaults
            in _STRUCTURED_VISION_DEFAULT_ACTIONS.items()
        }
        for case in similar_cases:
            issue_type = case.get("issue_type")
            action = case.get("maintenance_action")
            if (
                isinstance(issue_type, str)
                and issue_type in actions
                and isinstance(action, str)
                and action.strip()
                and action.strip() not in actions[issue_type]
                and len(actions[issue_type]) < 8
            ):
                actions[issue_type].append(action.strip())
        return [
            {
                "issue_type": issue_type,
                "allowed_actions": allowed,
            }
            for issue_type, allowed in sorted(actions.items())
        ]

    @staticmethod
    def _provider_visual_target_box(
        *,
        detector_rows: Sequence[Mapping[str, Any]],
        sam2_summary: Mapping[str, Any],
    ) -> Optional[dict[str, float]]:
        candidates: list[tuple[float, Mapping[str, Any]]] = []
        sam2_box = sam2_summary.get("mask_box")
        if isinstance(sam2_box, Mapping):
            candidates.append((2.0, sam2_box))
        for row in detector_rows:
            box = row.get("box")
            score = row.get("score")
            if (
                isinstance(box, Mapping)
                and not isinstance(score, bool)
                and isinstance(score, (int, float))
                and math.isfinite(float(score))
            ):
                candidates.append((float(score), box))
        for _score, box in sorted(
            candidates,
            key=lambda item: item[0],
            reverse=True,
        ):
            if set(box) != {"x", "y", "width", "height"}:
                continue
            normalized: dict[str, float] = {}
            valid = True
            for key in ("x", "y", "width", "height"):
                number = box[key]
                if (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                ):
                    valid = False
                    break
                normalized[key] = float(number)
            if (
                valid
                and 0 <= normalized["x"] < 1
                and 0 <= normalized["y"] < 1
                and 0 < normalized["width"] <= 1
                and 0 < normalized["height"] <= 1
                and normalized["x"] + normalized["width"]
                <= 1.00000001
                and normalized["y"] + normalized["height"]
                <= 1.00000001
            ):
                return normalized
        return None

    def _generate_structured_vision_proposals(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        checkpoints = context.get("checkpoints", {})
        detection = checkpoints.get("detection_ocr", {})
        temporal = checkpoints.get("temporal_merge", {})
        similar = checkpoints.get("similar_case_search", {})
        sam2 = checkpoints.get(
            "sam2_segmentation_tracking", {}
        )
        gps = checkpoints.get("metadata_gps_preflight", {})
        detection_revision = detection.get("detection_revision")
        temporal_revision = temporal.get(
            "temporal_merge_revision"
        )
        candidate_ids = temporal.get(
            "question_candidate_ids"
        )
        if (
            not isinstance(detection_revision, str)
            or _SHA256.fullmatch(detection_revision) is None
            or not isinstance(temporal_revision, str)
            or _SHA256.fullmatch(temporal_revision) is None
            or not isinstance(candidate_ids, list)
            or not candidate_ids
            or len(candidate_ids) != len(set(candidate_ids))
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in candidate_ids
            )
        ):
            raise PipelineStageFailure(
                "structured_vision_checkpoint_missing"
            )
        (
            analysis_job_id,
            row,
            candidates,
        ) = self._analysis_job_for_context(project_id, context)
        if (
            temporal.get("analysis_job_id")
            != str(analysis_job_id)
            or temporal.get("source_sha256")
            != row["source_sha256"]
        ):
            raise PipelineStageFailure(
                "structured_vision_checkpoint_mismatch"
            )
        candidates_by_id = {
            str(candidate["candidate_id"]): candidate
            for candidate in candidates
        }
        local = self._local_vision_from_detection_checkpoint(
            candidates, context
        )
        dino: Mapping[str, Mapping[str, Any]] = {}
        evaluation_id = detection.get("evaluation_id")
        if (
            isinstance(evaluation_id, str)
            and self._experimental_proposals is not None
        ):
            dino = self._experimental_proposals.latest_for_job(
                project_id,
                analysis_job_id,
                evaluation_id=evaluation_id,
            )
        approved_by_identity: dict[
            tuple[str, str], Mapping[str, Any]
        ] = {}
        if self._weak_case_approvals is not None:
            try:
                approved_snapshot = (
                    self._weak_case_approvals.list_project(
                        project_id
                    )
                )
                records = approved_snapshot.get("records", ())
                if isinstance(records, list):
                    approved_by_identity = {
                        (
                            str(record.get("source_id", "")),
                            str(record.get("case_id", "")),
                        ): record
                        for record in records
                        if isinstance(record, Mapping)
                    }
            except Exception:
                approved_by_identity = {}
        provider = self._structured_provider_snapshot(
            context["profile"]
        )
        storage_capacity_revision = (
            self._storage_capacity_revision(
                context,
                stage="vision_proposal",
                required_bytes=(
                    _REPLAN_STAGE_METADATA_BYTES
                    + _provider_visual_capacity_upper_bound(
                        len(candidate_ids)
                    )
                ),
                basis={
                    "basis_type": "actual_temporally_merged_events",
                    "merged_event_count": len(candidate_ids),
                    "provider_visual_capacity_bytes": (
                        _provider_visual_capacity_upper_bound(
                            len(candidate_ids)
                        )
                    ),
                    "temporal_merge_revision": temporal_revision,
                    "provider": provider["provider"],
                },
            )
        )
        try:
            pipeline_job_id = UUID(str(context["job_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "structured_vision_checkpoint_mismatch"
            ) from error
        provider_evidence_backend = (
            self._candidate_evidence_registry
            if self._candidate_evidence_registry is not None
            else self._analysis_jobs
        )
        provider_rescan_metadata_by_id: dict[
            str, Mapping[str, Any]
        ] = {}
        provider_resolve_rescan: Optional[
            Callable[
                [UUID, UUID, str],
                ResolvedAnalysisArtifact,
            ]
        ] = None
        if (
            provider["ready"]
            and provider["provider"] == "codex"
        ):
            try:
                high_frequency = (
                    self._high_frequency_evidence_for_context(
                        project_id,
                        analysis_job_id,
                        context,
                    )
                )
            except PipelineStageFailure:
                # The comparison stage must not reinterpret a broken rescan
                # checkpoint. The per-candidate montage resolver below will
                # record a bounded review-only not-run result instead of
                # invoking the Provider with weaker representative imagery.
                high_frequency = None
            if high_frequency is not None:
                (
                    provider_rescan_candidates,
                    _provider_parent_by_evidence,
                    provider_resolve_rescan,
                    _provider_rescan_manifest,
                ) = high_frequency
                provider_rescan_metadata_by_id = {
                    str(item["candidate_id"]): dict(item)
                    for item in provider_rescan_candidates
                    if isinstance(item, Mapping)
                    and isinstance(
                        item.get("candidate_id"), str
                    )
                }

        def verified_provider_artifact(
            evidence_ref: str,
            *,
            expected_timestamp: float,
        ) -> tuple[ResolvedAnalysisArtifact, bytes]:
            if not isinstance(evidence_ref, str):
                raise ValueError("candidate evidence ref is invalid")
            kind, separator, evidence_id = evidence_ref.partition(
                ":"
            )
            if (
                separator != ":"
                or re.fullmatch(r"[a-f0-9]{20}", evidence_id)
                is None
            ):
                raise ValueError("candidate evidence ref is invalid")
            if kind == "rescan":
                metadata = provider_rescan_metadata_by_id.get(
                    evidence_id
                )
                if (
                    metadata is None
                    or provider_resolve_rescan is None
                ):
                    raise ValueError(
                        "candidate rescan evidence is unavailable"
                    )
                source_timestamp = metadata.get(
                    "representative_timestamp_seconds"
                )
                artifact = provider_resolve_rescan(
                    project_id,
                    analysis_job_id,
                    evidence_id,
                )
            elif kind == "candidate":
                metadata = candidates_by_id.get(evidence_id)
                if (
                    metadata is None
                    or provider_evidence_backend is None
                ):
                    raise ValueError(
                        "candidate evidence is unavailable"
                    )
                source_timestamp = metadata.get(
                    "representative_timestamp_seconds"
                )
                artifact = provider_evidence_backend.resolve_artifact(
                    project_id,
                    analysis_job_id,
                    evidence_id,
                    "image",
                )
            else:
                raise ValueError(
                    "candidate evidence ref is unsupported"
                )
            if (
                isinstance(source_timestamp, bool)
                or not isinstance(
                    source_timestamp, (int, float)
                )
                or not math.isfinite(float(source_timestamp))
                or abs(
                    float(source_timestamp)
                    - float(expected_timestamp)
                )
                > 0.000001
            ):
                raise ValueError(
                    "candidate evidence timestamp changed"
                )
            try:
                path = Path(artifact.path)
                if (
                    not path.is_absolute()
                    or path.is_symlink()
                    or _is_reparse_point(path)
                    or not path.is_file()
                    or not stat.S_ISREG(path.lstat().st_mode)
                    or not 1
                    <= artifact.size_bytes
                    <= MAX_PROVIDER_CANDIDATE_ARTIFACT_BYTES
                ):
                    raise OSError
                before = path.stat()
                body = path.read_bytes()
                after = path.stat()
            except (OSError, TypeError, ValueError) as error:
                raise ValueError(
                    "candidate evidence artifact unavailable"
                ) from error
            if (
                len(body) != artifact.size_bytes
                or hashlib.sha256(body).hexdigest()
                != artifact.sha256
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ino != after.st_ino
            ):
                raise ValueError(
                    "candidate evidence artifact changed"
                )
            return artifact, body

        judgments: dict[str, dict[str, Any]] = {}
        visual_preparations: dict[str, dict[str, Any]] = {}
        provider_invocation_count = 0
        privacy_review_required_count = 0
        privacy_rejected_count = 0
        metadata_downgrade_count = 0
        candidate_evidence_resolution_failure_count = 0
        for candidate_id in candidate_ids:
            if cancel():
                return PipelineStageResult(
                    "cancelled",
                    error_code="structured_vision_cancelled",
                )
            if not provider["ready"]:
                judgments[candidate_id] = (
                    self._local_structured_judgment(
                        candidate_id=candidate_id,
                        provider=provider["provider"],
                        status="not_run",
                        error_code=str(
                            provider["not_run_reason"]
                        ),
                    )
                )
                continue
            candidate = candidates_by_id.get(candidate_id)
            if candidate is None:
                judgments[candidate_id] = (
                    self._local_structured_judgment(
                        candidate_id=candidate_id,
                        provider=provider["provider"],
                        status="failed",
                        error_code="candidate_evidence_unavailable",
                    )
                )
                continue
            detector_rows = self._structured_detector_rows(
                candidate_id=candidate_id,
                local=local,
                dino=dino,
            )
            similar_cases = (
                self._structured_similar_cases(
                    candidate_id=candidate_id,
                    checkpoint=similar,
                    approved_by_identity=approved_by_identity,
                )
            )
            if not detector_rows:
                judgments[candidate_id] = (
                    self._local_structured_judgment(
                        candidate_id=candidate_id,
                        provider=provider["provider"],
                        status="not_run",
                        error_code=(
                            "candidate_detector_proposal_missing"
                        ),
                    )
                )
                continue
            if len(similar_cases) < 3:
                judgments[candidate_id] = (
                    self._local_structured_judgment(
                        candidate_id=candidate_id,
                        provider=provider["provider"],
                        status="not_run",
                        error_code=(
                            "similar_cases_insufficient"
                        ),
                    )
                )
                continue
            try:
                sam2_proposal_candidate_id = (
                    self._sam2_proposal_candidate_for_review(
                        candidate_id=candidate_id,
                        temporal=temporal,
                        checkpoint=sam2,
                    )
                )
                sam2_summary = self._structured_sam2_summary(
                    candidate_id=candidate_id,
                    checkpoint=sam2,
                    proposal_candidate_id=(
                        sam2_proposal_candidate_id
                    ),
                )
                evidence_refs = []
                for artifact_key in (
                    "representative_image",
                    "preview_clip",
                ):
                    artifact = candidate.get(artifact_key)
                    artifact_sha256 = (
                        artifact.get("sha256")
                        if isinstance(artifact, Mapping)
                        else None
                    )
                    if (
                        isinstance(artifact_sha256, str)
                        and _SHA256.fullmatch(artifact_sha256)
                        is not None
                    ):
                        evidence_refs.append(
                            f"evidence:{candidate_id}:"
                            f"{artifact_key}-{artifact_sha256[:16]}"
                        )
                if not evidence_refs:
                    if (
                        candidate.get("candidate_kind")
                        in {
                            "dense_candidate_evidence_overlay",
                            "dino_temporal_review_evidence_overlay",
                        }
                    ):
                        raise PipelineStageFailure(
                            "candidate_evidence_unavailable"
                        )
                    evidence_refs = [
                        f"analysis:{candidate_id}:"
                        "representative-metadata"
                    ]
                provider_attachments = ()
                if provider["provider"] == "codex":
                    if (
                        self._provider_visual_evidence is None
                        or self._weak_case_catalog is None
                    ):
                        privacy_review_required_count += 1
                        judgments[candidate_id] = (
                            self._local_structured_judgment(
                                candidate_id=candidate_id,
                                provider=provider["provider"],
                                status="not_run",
                                error_code=(
                                    "provider_visual_evidence_unavailable"
                                ),
                            )
                        )
                        continue
                    proposals = sam2.get("proposals")
                    sam2_proposal = next(
                        (
                            item
                            for item in proposals
                            if isinstance(item, Mapping)
                            and item.get("candidate_id")
                            == sam2_proposal_candidate_id
                        ),
                        None,
                    ) if isinstance(proposals, list) else None
                    tracked_frames = (
                        sam2_proposal.get("tracked_frames")
                        if isinstance(sam2_proposal, Mapping)
                        else None
                    )
                    if not isinstance(tracked_frames, list):
                        raise ProviderCandidateEvidenceResolutionError(
                            "candidate_evidence_track_invalid",
                            "SAM2 temporal evidence is unavailable",
                        )
                    timestamp_by_ref = {
                        str(item["evidence_ref"]): float(
                            item["timestamp_seconds"]
                        )
                        for item in tracked_frames
                        if isinstance(item, Mapping)
                        and isinstance(
                            item.get("evidence_ref"), str
                        )
                        and not isinstance(
                            item.get("timestamp_seconds"), bool
                        )
                        and isinstance(
                            item.get("timestamp_seconds"),
                            (int, float),
                        )
                    }
                    if len(timestamp_by_ref) != len(tracked_frames):
                        raise ProviderCandidateEvidenceResolutionError(
                            "candidate_evidence_track_invalid",
                            "SAM2 temporal evidence is invalid",
                        )

                    if (
                        sam2_proposal_candidate_id
                        == candidate_id
                    ):

                        def resolve_candidate_frame(
                            *,
                            project_id: UUID,
                            analysis_job_id: UUID,
                            pipeline_job_id: UUID,
                            candidate_id: str,
                            evidence_ref: str,
                        ) -> ResolvedCandidateFrameArtifact:
                            if (
                                project_id
                                != project_id_for_montage
                                or analysis_job_id
                                != analysis_job_id_for_montage
                                or pipeline_job_id
                                != pipeline_job_id_for_montage
                                or candidate_id
                                != review_candidate_id_for_montage
                                or evidence_ref
                                not in timestamp_by_ref_for_montage
                            ):
                                raise ValueError(
                                    "candidate evidence scope changed"
                                )
                            artifact, body = (
                                verified_provider_artifact(
                                    evidence_ref,
                                    expected_timestamp=(
                                        timestamp_by_ref_for_montage[
                                            evidence_ref
                                        ]
                                    ),
                                )
                            )
                            return ResolvedCandidateFrameArtifact(
                                project_id=project_id,
                                analysis_job_id=analysis_job_id,
                                pipeline_job_id=pipeline_job_id,
                                candidate_id=candidate_id,
                                evidence_ref=evidence_ref,
                                timestamp_seconds=(
                                    timestamp_by_ref_for_montage[
                                        evidence_ref
                                    ]
                                ),
                                body=body,
                                media_type=artifact.media_type,
                                size_bytes=artifact.size_bytes,
                                sha256=artifact.sha256,
                            )

                        project_id_for_montage = project_id
                        analysis_job_id_for_montage = analysis_job_id
                        pipeline_job_id_for_montage = pipeline_job_id
                        review_candidate_id_for_montage = candidate_id
                        timestamp_by_ref_for_montage = dict(
                            timestamp_by_ref
                        )
                        resolved_candidate_evidence = (
                            resolve_provider_candidate_evidence(
                                project_id=project_id,
                                analysis_job_id=analysis_job_id,
                                pipeline_job_id=pipeline_job_id,
                                candidate_id=candidate_id,
                                sam2_checkpoint=sam2,
                                artifact_resolver=(
                                    resolve_candidate_frame
                                ),
                            )
                        )
                    else:

                        def resolve_aliased_candidate_frame(
                            *,
                            project_id: UUID,
                            analysis_job_id: UUID,
                            pipeline_job_id: UUID,
                            review_candidate_id: str,
                            sam2_proposal_candidate_id: str,
                            evidence_ref: str,
                        ) -> ResolvedAliasedCandidateFrameArtifact:
                            if (
                                project_id
                                != project_id_for_montage
                                or analysis_job_id
                                != analysis_job_id_for_montage
                                or pipeline_job_id
                                != pipeline_job_id_for_montage
                                or review_candidate_id
                                != review_candidate_id_for_montage
                                or sam2_proposal_candidate_id
                                != sam2_candidate_id_for_montage
                                or evidence_ref
                                not in timestamp_by_ref_for_montage
                            ):
                                raise ValueError(
                                    "candidate alias scope changed"
                                )
                            artifact, body = (
                                verified_provider_artifact(
                                    evidence_ref,
                                    expected_timestamp=(
                                        timestamp_by_ref_for_montage[
                                            evidence_ref
                                        ]
                                    ),
                                )
                            )
                            return (
                                ResolvedAliasedCandidateFrameArtifact(
                                    project_id=project_id,
                                    analysis_job_id=analysis_job_id,
                                    pipeline_job_id=pipeline_job_id,
                                    review_candidate_id=(
                                        review_candidate_id
                                    ),
                                    sam2_proposal_candidate_id=(
                                        sam2_proposal_candidate_id
                                    ),
                                    evidence_ref=evidence_ref,
                                    timestamp_seconds=(
                                        timestamp_by_ref_for_montage[
                                            evidence_ref
                                        ]
                                    ),
                                    body=body,
                                    media_type=artifact.media_type,
                                    size_bytes=artifact.size_bytes,
                                    sha256=artifact.sha256,
                                )
                            )

                        project_id_for_montage = project_id
                        analysis_job_id_for_montage = analysis_job_id
                        pipeline_job_id_for_montage = pipeline_job_id
                        review_candidate_id_for_montage = candidate_id
                        sam2_candidate_id_for_montage = (
                            sam2_proposal_candidate_id
                        )
                        timestamp_by_ref_for_montage = dict(
                            timestamp_by_ref
                        )
                        resolved_candidate_evidence = (
                            resolve_aliased_provider_candidate_evidence(
                                project_id=project_id,
                                analysis_job_id=analysis_job_id,
                                pipeline_job_id=pipeline_job_id,
                                review_candidate_id=candidate_id,
                                sam2_proposal_candidate_id=(
                                    sam2_proposal_candidate_id
                                ),
                                temporal_checkpoint=temporal,
                                sam2_checkpoint=sam2,
                                artifact_resolver=(
                                    resolve_aliased_candidate_frame
                                ),
                            )
                        )
                    montage = resolved_candidate_evidence.montage
                    candidate_source_ref = (
                        f"deid:{candidate_id}:provider-montage-"
                        f"{montage.sha256[:16]}"
                    )
                    evidence_refs.insert(0, candidate_source_ref)
                    _, provider_visual_root = (
                        self._pipeline_artifact_directory(
                            project_id,
                            context,
                            "provider-visual-evidence",
                        )
                    )
                    preparation = dict(
                        self._provider_visual_evidence.prepare_from_body(
                            project_id=project_id,
                            pipeline_job_id=pipeline_job_id,
                            analysis_job_id=analysis_job_id,
                            candidate_id=candidate_id,
                            candidate_body=montage.body,
                            candidate_media_type=(
                                montage.media_type
                            ),
                            candidate_size_bytes=(
                                montage.size_bytes
                            ),
                            candidate_sha256=montage.sha256,
                            candidate_source_ref=(
                                candidate_source_ref
                            ),
                            target_box={
                                "x": 0.0,
                                "y": 0.0,
                                "width": 1.0,
                                "height": 1.0,
                            },
                            similar_cases=similar_cases,
                            resolve_reference=(
                                self._weak_case_catalog
                                .resolve_case_image_for_visual_index
                            ),
                            job_artifact_root=provider_visual_root,
                        )
                    )
                    preparation[
                        "candidate_evidence_resolution"
                    ] = dict(resolved_candidate_evidence.manifest)
                    visual_preparations[candidate_id] = preparation
                    preparation_state = preparation.get("state")
                    if (
                        preparation_state
                        == "awaiting_deidentification_review"
                    ):
                        privacy_review_required_count += 1
                        judgments[candidate_id] = (
                            self._local_structured_judgment(
                                candidate_id=candidate_id,
                                provider=provider["provider"],
                                status="not_run",
                                error_code=(
                                    "awaiting_deidentification_review"
                                ),
                            )
                        )
                        continue
                    if (
                        preparation_state
                        == "rejected_by_privacy_review"
                    ):
                        privacy_rejected_count += 1
                        judgments[candidate_id] = (
                            self._local_structured_judgment(
                                candidate_id=candidate_id,
                                provider=provider["provider"],
                                status="not_run",
                                error_code=(
                                    "deidentification_review_rejected"
                                ),
                            )
                        )
                        continue
                    if (
                        preparation_state
                        != "verified_for_provider_transmission"
                    ):
                        raise ProviderVisualEvidenceError(
                            "visual_evidence_preparation_invalid"
                        )
                    provider_attachments = (
                        self._provider_visual_evidence
                        .resolve_verified_attachments(
                            project_id=project_id,
                            pipeline_job_id=pipeline_job_id,
                            candidate_id=candidate_id,
                            preparation_id=str(
                                preparation["preparation_id"]
                            ),
                            expected_manifest_sha256=str(
                                preparation["manifest_sha256"]
                            ),
                        )
                    )
                elif provider["provider"] == "claude":
                    # Claude Code has no reviewed image attachment flag.
                    # Continue with the already bounded metadata contract
                    # without reading or staging privacy artifacts.
                    metadata_downgrade_count += 1
                payload = {
                    "schema_version": (
                        STRUCTURED_VISION_REQUEST_SCHEMA
                    ),
                    "candidate_id": candidate_id,
                    "project_instruction": (
                        _STRUCTURED_VISION_PROJECT_INSTRUCTION
                    ),
                    "taxonomy": self._structured_taxonomy(
                        similar_cases
                    ),
                    "detector_proposals": detector_rows,
                    "ocr_observations": (
                        self._structured_ocr_rows(
                            candidate_id=candidate_id,
                            candidate=candidate,
                            local=local,
                        )
                    ),
                    "sam2_summary": sam2_summary,
                    "temporal_summary": (
                        self._structured_temporal_summary(
                            candidate_id=candidate_id,
                            candidate=candidate,
                            temporal=temporal,
                            gps=gps,
                        )
                    ),
                    "candidate_evidence_refs": evidence_refs,
                    "similar_cases": similar_cases,
                }

                class CancellationProxy:
                    def is_set(self) -> bool:
                        return bool(cancel())

                assert self._structured_vision_judgment is not None
                if provider_attachments:
                    generated = (
                        self._structured_vision_judgment.propose(
                            provider["provider"],
                            payload,
                            transmission_consent=True,
                            attachments=provider_attachments,
                            cancel_event=CancellationProxy(),
                        )
                    )
                else:
                    generated = (
                        self._structured_vision_judgment.propose(
                        provider["provider"],
                        payload,
                        transmission_consent=True,
                        cancel_event=CancellationProxy(),
                    )
                    )
                validated_judgment = (
                    self._validated_structured_judgment(
                        generated,
                        candidate_id=candidate_id,
                        provider=provider["provider"],
                    )
                )
                if validated_judgment[
                    "provider_command_invoked"
                ]:
                    provider_invocation_count += 1
                judgments[candidate_id] = validated_judgment
            except ProviderCandidateEvidenceResolutionError as error:
                candidate_evidence_resolution_failure_count += 1
                judgments[candidate_id] = (
                    self._local_structured_judgment(
                        candidate_id=candidate_id,
                        provider=provider["provider"],
                        status="not_run",
                        error_code=(
                            error.code
                            if _SAFE_ERROR_CODE.fullmatch(
                                error.code
                            )
                            else (
                                "candidate_evidence_resolution_failed"
                            )
                        ),
                    )
                )
            except ProviderVisualEvidenceError as error:
                privacy_review_required_count += 1
                judgments[candidate_id] = (
                    self._local_structured_judgment(
                        candidate_id=candidate_id,
                        provider=provider["provider"],
                        status="not_run",
                        error_code=(
                            error.code
                            if _SAFE_ERROR_CODE.fullmatch(
                                error.code
                            )
                            else "provider_visual_evidence_failed"
                        ),
                    )
                )
            except InvalidStructuredVisionPayload:
                judgments[candidate_id] = (
                    self._local_structured_judgment(
                        candidate_id=candidate_id,
                        provider=provider["provider"],
                        status="failed",
                        error_code="structured_payload_invalid",
                    )
                )
            except Exception:
                judgments[candidate_id] = (
                    self._local_structured_judgment(
                        candidate_id=candidate_id,
                        provider=provider["provider"],
                        status="failed",
                        error_code="structured_provider_failed",
                    )
                )
        proposed_count = sum(
            item["status"] == "proposed"
            for item in judgments.values()
        )
        not_run_count = sum(
            item["status"] == "not_run"
            for item in judgments.values()
        )
        failure_count = sum(
            item["status"] == "failed"
            for item in judgments.values()
        )
        privacy_error_codes = {
            str(item.get("error_code"))
            for item in judgments.values()
            if isinstance(item.get("error_code"), str)
            and (
                "deidentification" in str(item.get("error_code"))
                or "visual_evidence" in str(item.get("error_code"))
            )
        }
        privacy_blocked = bool(
            privacy_review_required_count
            or privacy_rejected_count
        )
        if privacy_blocked:
            semantics_status = (
                "awaiting_deidentification_review"
            )
            if privacy_rejected_count:
                error_code = "deidentification_review_rejected"
            elif (
                "provider_visual_evidence_unavailable"
                in privacy_error_codes
            ):
                error_code = (
                    "provider_visual_evidence_unavailable"
                )
            else:
                error_code = (
                    "awaiting_deidentification_review"
                )
            stage_outcome = "partial"
        elif proposed_count == len(candidate_ids):
            semantics_status = "completed"
            error_code = None
            stage_outcome = "success"
        elif proposed_count:
            semantics_status = "partial"
            error_code = "structured_llm_partial"
            stage_outcome = "success"
        elif failure_count:
            semantics_status = "failed"
            error_code = "structured_llm_failed"
            stage_outcome = "success"
        else:
            semantics_status = "not_run"
            error_code = "structured_llm_not_run"
            stage_outcome = "success"
        direct_visual_inspection_claimed = any(
            item.get("direct_visual_inspection_claimed") is True
            for item in judgments.values()
        )
        comparison_mode = (
            STRUCTURED_VISION_DIRECT_COMPARISON_MODE
            if direct_visual_inspection_claimed
            else STRUCTURED_VISION_COMPARISON_MODE
        )
        if privacy_rejected_count:
            privacy_verification_status = "rejected"
        elif privacy_review_required_count:
            privacy_verification_status = "awaiting_review"
        elif provider["provider"] == "claude":
            privacy_verification_status = (
                "metadata_only_provider_no_image_transport"
            )
        elif direct_visual_inspection_claimed:
            privacy_verification_status = "verified"
        else:
            privacy_verification_status = "not_applicable"
        judgment_revision = hashlib.sha256(
            _canonical_json(
                {
                    "pipeline_job_id": str(context["job_id"]),
                    "analysis_job_id": str(analysis_job_id),
                    "source_sha256": row["source_sha256"],
                    "detection_revision": detection_revision,
                    "temporal_merge_revision": temporal_revision,
                    "visual_reference_revision": similar.get(
                        "visual_reference_revision"
                    ),
                    "provider": provider["provider"],
                    "provider_state": provider["state"],
                    "transmission_consent": provider["consent"],
                    "provider_visual_evidence": (
                        visual_preparations
                    ),
                    "judgments": judgments,
                }
            )
        ).hexdigest()
        return PipelineStageResult(
            stage_outcome,
            checkpoint={
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": row["source_sha256"],
                "detection_revision": detection_revision,
                "temporal_merge_revision": temporal_revision,
                "proposal_sources": list(
                    detection.get("proposal_sources", ())
                ),
                "proposal_candidate_count": len(candidate_ids),
                "structured_proposal_count": proposed_count,
                "structured_not_run_count": not_run_count,
                "structured_failure_count": failure_count,
                "structured_llm_semantics_status": (
                    semantics_status
                ),
                "structured_vision_revision": judgment_revision,
                "structured_judgments_by_candidate": judgments,
                "provider_visual_evidence_by_candidate": (
                    visual_preparations
                ),
                "privacy_verification_status": (
                    privacy_verification_status
                ),
                "privacy_review_required_count": (
                    privacy_review_required_count
                ),
                "privacy_rejected_count": privacy_rejected_count,
                "metadata_only_provider_downgrade_count": (
                    metadata_downgrade_count
                ),
                "candidate_evidence_resolution_failure_count": (
                    candidate_evidence_resolution_failure_count
                ),
                "selected_provider": provider["provider"],
                "provider_state": provider["state"],
                "transmission_consent": provider["consent"],
                "provider_invocation_count": (
                    provider_invocation_count
                ),
                "storage_capacity_revision": (
                    storage_capacity_revision
                ),
                "comparison_mode": comparison_mode,
                "llm_bulk_frame_detection_used": False,
                "direct_visual_inspection_claimed": (
                    direct_visual_inspection_claimed
                ),
                "review_only": True,
                "automatic_labeling_allowed": False,
                "automatic_annotation_allowed": False,
                "automatic_incident_allowed": False,
                "automatic_training_membership_allowed": False,
                "automatic_report_truth_allowed": False,
                "annotation_count": 0,
                "incident_count": 0,
                "training_membership_count": 0,
                "report_incident_count": 0,
            },
            metrics={
                "proposal_candidate_count": len(candidate_ids),
                "structured_proposal_count": proposed_count,
                "structured_not_run_count": not_run_count,
                "structured_failure_count": failure_count,
                "provider_invocation_count": (
                    provider_invocation_count
                ),
                "privacy_review_required_count": (
                    privacy_review_required_count
                ),
                "privacy_rejected_count": privacy_rejected_count,
                "metadata_only_provider_downgrade_count": (
                    metadata_downgrade_count
                ),
                "candidate_evidence_resolution_failure_count": (
                    candidate_evidence_resolution_failure_count
                ),
                "storage_capacity_revision_sha256": (
                    storage_capacity_revision.get(
                        "revision_sha256"
                    )
                    if isinstance(
                        storage_capacity_revision, Mapping
                    )
                    else None
                ),
                "annotation_count": 0,
                "incident_count": 0,
                "training_membership_count": 0,
                "report_incident_count": 0,
            },
            error_code=error_code,
        )

    def _materialize_review_event_set(
        self,
        context: Mapping[str, Any],
    ) -> Optional[dict[str, Any]]:
        """Bind the final review queue to immutable upstream evidence.

        The resulting set remains review-only.  It is deliberately assembled
        only after detector/OCR, SAM2, temporal merge, approved-reference
        retrieval, and structured proposal stages have all produced durable
        checkpoints.
        """

        if self._review_event_sets is None:
            return None
        checkpoints = context.get("checkpoints", {})
        rescan = checkpoints.get("candidate_rescan_5_10fps", {})
        base_overlay = (
            rescan.get("candidate_evidence_overlay")
            if isinstance(rescan, Mapping)
            else None
        )
        temporal = checkpoints.get("temporal_merge", {})
        dino_overlay = (
            temporal.get("dino_candidate_evidence_overlay")
            if isinstance(temporal, Mapping)
            else None
        )
        overlay = (
            dino_overlay
            if isinstance(dino_overlay, Mapping)
            else base_overlay
        )
        if not isinstance(overlay, Mapping):
            return None
        overlay_id = overlay.get("overlay_id")
        analysis_job_id = overlay.get("analysis_job_id")
        source_sha256 = overlay.get("source_sha256")
        try:
            project_id = UUID(str(context["project_id"]))
            analysis_id = UUID(str(analysis_job_id))
            pipeline_id = UUID(str(context["job_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "review_event_set_identity_invalid"
            ) from error
        if (
            not isinstance(overlay_id, str)
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or overlay.get("project_id") != str(project_id)
            or overlay.get("pipeline_job_id") != str(pipeline_id)
        ):
            raise PipelineStageFailure(
                "review_event_set_identity_invalid"
            )

        required = {
            "dino": checkpoints.get("detection_ocr"),
            "ocr": checkpoints.get("detection_ocr"),
            "sam2": checkpoints.get("sam2_segmentation_tracking"),
            "temporal": checkpoints.get("temporal_merge"),
            "similar_cases": checkpoints.get("similar_case_search"),
            "structured_proposal": checkpoints.get("vision_proposal"),
        }
        if any(
            not isinstance(value, Mapping)
            for value in required.values()
        ):
            raise PipelineStageFailure(
                "review_event_set_checkpoint_missing"
            )
        try:
            result = self._review_event_sets.materialize(
                project_id=project_id,
                analysis_job_id=analysis_id,
                pipeline_job_id=pipeline_id,
                overlay_id=overlay_id,
                source_sha256=source_sha256,
                dino_checkpoint=required["dino"],
                ocr_checkpoint=required["ocr"],
                sam2_checkpoint=required["sam2"],
                temporal_checkpoint=required["temporal"],
                similar_case_checkpoint=required["similar_cases"],
                structured_proposal_checkpoint=required[
                    "structured_proposal"
                ],
                status="completed",
            )
        except ReviewEventSetError as error:
            raise PipelineStageFailure(
                "review_event_set_materialization_failed"
            ) from error
        expected_candidates = required["temporal"].get(
            "question_candidate_ids"
        )
        representative_ids = [
            event.representative_candidate_id
            for event in result.events
        ]
        if (
            result.project_id != str(project_id)
            or result.analysis_job_id != str(analysis_id)
            or result.pipeline_job_id != str(pipeline_id)
            or result.overlay_id != overlay_id
            or result.source_sha256 != source_sha256
            or result.review_only is not True
            or result.annotations_created != 0
            or result.incidents_created != 0
            or result.training_members_created != 0
            or result.report_truth_records_created != 0
            or representative_ids != expected_candidates
        ):
            raise PipelineStageFailure(
                "review_event_set_materialization_invalid"
            )
        checkpoint = result.to_checkpoint()
        checkpoint["event_bindings"] = [
            {
                "event_id": event.event_id,
                "representative_candidate_id": (
                    event.representative_candidate_id
                ),
                "event_revision": event.event_revision,
            }
            for event in result.events
        ]
        return checkpoint

    def _register_question_catalog(
        self,
        context: Mapping[str, Any],
        *,
        analysis_job_id: str,
        source_sha256: str,
        candidate_ids: Sequence[str],
        review_event_set: Optional[Mapping[str, Any]] = None,
        review_event_bindings: Sequence[
            Mapping[str, Any]
        ] = (),
    ) -> tuple[str, Optional[str]]:
        if self._question_catalog_projector is None:
            return "sidecar_only", None
        if self._candidate_labels is None:
            raise PipelineStageFailure(
                "question_catalog_projection_unavailable"
            )
        try:
            project_id = UUID(str(context["project_id"]))
            analysis_id = UUID(analysis_job_id)
            if (
                review_event_set is not None
                and self._review_event_sets is not None
            ):
                pipeline_id = UUID(
                    str(review_event_set["pipeline_job_id"])
                )
                batch = (
                    self._review_event_sets
                    .resolve_candidate_bindings_batch(
                        project_id,
                        analysis_id,
                        pipeline_id,
                        candidate_ids,
                    )
                )
                resolved_candidates = []
                for item in batch:
                    metadata = dict(item.candidate_metadata)
                    metadata["review_event_binding"] = {
                        "schema_version": (
                            "trafficvisson.review-event-binding.v1"
                        ),
                        "review_event_set_id": (
                            item.event_set.review_event_set_id
                        ),
                        "review_event_set_revision": (
                            item.event_set.review_event_set_revision
                        ),
                        "review_event_set_manifest_sha256": (
                            item.event_set.manifest_sha256
                        ),
                        "review_event_id": item.event.event_id,
                        "review_event_revision": (
                            item.event.event_revision
                        ),
                        "representative_candidate_id": (
                            item.event.representative_candidate_id
                        ),
                        "review_only": True,
                    }
                    resolved_candidates.append(
                        {
                            "status": "completed",
                            "project_id": str(project_id),
                            "analysis_job_id": str(analysis_id),
                            "source_sha256": source_sha256,
                            "candidate_id": (
                                item.event
                                .representative_candidate_id
                            ),
                            "candidate_metadata_sha256": (
                                item.candidate_metadata_sha256
                            ),
                            "metadata": metadata,
                        }
                    )
                batch_question_resolver = getattr(
                    self._candidate_labels,
                    "resolve_questions_from_candidates",
                    None,
                )
                if callable(batch_question_resolver):
                    questions = batch_question_resolver(
                        project_id,
                        analysis_id,
                        candidate_ids,
                        resolved_candidates,
                    )
                else:
                    questions = [
                        self._candidate_labels.resolve_question(
                            project_id,
                            analysis_id,
                            candidate_id,
                        )
                        for candidate_id in candidate_ids
                    ]
            else:
                questions = [
                    self._candidate_labels.resolve_question(
                        project_id,
                        analysis_id,
                        candidate_id,
                    )
                    for candidate_id in candidate_ids
                ]
            if review_event_set is not None:
                if (
                    len(review_event_bindings)
                    != len(candidate_ids)
                ):
                    raise ValueError(
                        "review event bindings do not match questions"
                    )
                by_candidate = {
                    str(binding["representative_candidate_id"]): (
                        binding
                    )
                    for binding in review_event_bindings
                }
                for question in questions:
                    candidate = question.get(
                        "ai_original_candidate"
                    )
                    candidate_id = str(
                        question.get("candidate_id", "")
                    )
                    binding = (
                        candidate.get("review_event_binding")
                        if isinstance(candidate, Mapping)
                        else None
                    )
                    event = by_candidate.get(candidate_id)
                    if (
                        not isinstance(binding, Mapping)
                        or not isinstance(event, Mapping)
                        or binding.get("review_event_set_id")
                        != review_event_set.get(
                            "review_event_set_id"
                        )
                        or binding.get(
                            "review_event_set_revision"
                        )
                        != review_event_set.get(
                            "review_event_set_revision"
                        )
                        or binding.get(
                            "review_event_set_manifest_sha256"
                        )
                        != review_event_set.get(
                            "manifest_sha256"
                        )
                        or binding.get("review_event_id")
                        != event.get("event_id")
                        or binding.get("review_event_revision")
                        != event.get("event_revision")
                        or binding.get(
                            "representative_candidate_id"
                        )
                        != candidate_id
                        or binding.get("review_only") is not True
                    ):
                        raise ValueError(
                            "question review-event binding is invalid"
                        )
            registration = dict(
                self._question_catalog_projector(
                    project_id,
                    analysis_id,
                    source_sha256,
                    questions,
                )
            )
        except Exception as error:
            raise PipelineStageFailure(
                "question_catalog_projection_failed"
            ) from error
        question_catalog_revision = registration.get(
            "question_set_sha256"
        )
        if (
            registration.get("project_id") != str(project_id)
            or registration.get("analysis_job_id")
            != str(analysis_id)
            or registration.get("source_sha256") != source_sha256
            or isinstance(registration.get("question_count"), bool)
            or registration.get("question_count") != len(candidate_ids)
            or not isinstance(question_catalog_revision, str)
            or _SHA256.fullmatch(question_catalog_revision) is None
        ):
            raise PipelineStageFailure(
                "question_catalog_projection_invalid"
            )
        return "registered", question_catalog_revision

    def _apply_structured_training_label_delegation(
        self,
        context: Mapping[str, Any],
        *,
        analysis_job_id: str,
        candidate_ids: Sequence[str],
    ) -> dict[str, Any]:
        profile = context.get("profile")
        authority = (
            profile.get("label_confirmation_authority")
            if isinstance(profile, Mapping)
            else None
        )
        explicitly_delegated = (
            isinstance(profile, Mapping)
            and profile.get("schema_version")
            == PIPELINE_PROFILE_SCHEMA
            and authority
            == "structured_llm_training_assist"
        )
        if not explicitly_delegated:
            identity = {
                "authority": "human_review",
                "candidate_ids": list(candidate_ids),
                "delegated_candidate_ids": [],
                "human_question_candidate_ids": list(
                    candidate_ids
                ),
            }
            return {
                **identity,
                "state": "human_review_required",
                "delegation_revision": hashlib.sha256(
                    _canonical_json(identity)
                ).hexdigest(),
                "mandatory_human_candidate_ids": [],
                "structured_training_label_count": 0,
                "training_annotation_count": 0,
                "incident_count": 0,
                "report_incident_count": 0,
                "fixed_validation_membership_count": 0,
                "training_only": True,
                "automatic_training_started": False,
            }
        if self._structured_label_delegator is None:
            raise PipelineStageFailure(
                "structured_llm_training_delegator_unavailable"
            )
        try:
            delegated = dict(
                self._structured_label_delegator(
                    UUID(str(context["project_id"])),
                    UUID(analysis_job_id),
                    list(candidate_ids),
                )
            )
            _validate_safe_json(
                delegated,
                "structured training-label delegation",
            )
        except PipelineStageFailure:
            raise
        except Exception as error:
            raise PipelineStageFailure(
                "structured_llm_training_delegation_failed"
            ) from error
        delegated_ids = delegated.get("delegated_candidate_ids")
        human_ids = delegated.get("human_question_candidate_ids")
        mandatory_ids = delegated.get(
            "mandatory_human_candidate_ids"
        )
        revision = delegated.get("delegation_revision")
        training_count = delegated.get(
            "structured_training_label_count"
        )
        annotation_count = delegated.get(
            "training_annotation_count"
        )
        if (
            not isinstance(delegated_ids, list)
            or not isinstance(human_ids, list)
            or not isinstance(mandatory_ids, list)
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in [
                    *delegated_ids,
                    *human_ids,
                    *mandatory_ids,
                ]
            )
            or len(delegated_ids) != len(set(delegated_ids))
            or len(human_ids) != len(set(human_ids))
            or set(delegated_ids).intersection(human_ids)
            or set(delegated_ids).union(human_ids)
            != set(candidate_ids)
            or not set(mandatory_ids).issubset(human_ids)
            or not isinstance(revision, str)
            or _SHA256.fullmatch(revision) is None
            or isinstance(training_count, bool)
            or not isinstance(training_count, int)
            or training_count != len(delegated_ids)
            or isinstance(annotation_count, bool)
            or not isinstance(annotation_count, int)
            or annotation_count != len(delegated_ids)
            or delegated.get("training_only") is not True
            or delegated.get("automatic_training_started")
            is not False
            or delegated.get("incident_count") != 0
            or delegated.get("report_incident_count") != 0
            or delegated.get(
                "fixed_validation_membership_count"
            )
            != 0
        ):
            raise PipelineStageFailure(
                "structured_llm_training_delegation_invalid"
            )
        return {
            **delegated,
            "authority": "structured_llm_training_assist",
            "state": (
                "completed"
                if not human_ids
                else "partial_human_review_required"
            ),
        }

    def _prepare_question_queue(
        self, context: Mapping[str, Any]
    ) -> PipelineStageResult:
        checkpoints = context.get("checkpoints", {})
        temporal = checkpoints.get(
            "temporal_merge", {}
        )
        detection = checkpoints.get("detection_ocr", {})
        rescan = checkpoints.get(
            "candidate_rescan_5_10fps", {}
        )
        sam2 = checkpoints.get(
            "sam2_segmentation_tracking", {}
        )
        vision = checkpoints.get("vision_proposal", {})
        candidate_ids = temporal.get("question_candidate_ids")
        analysis_job_id = temporal.get("analysis_job_id")
        source_sha256 = temporal.get("source_sha256")
        temporal_revision = temporal.get(
            "temporal_merge_revision"
        )
        detection_revision = detection.get("detection_revision")
        detector_status = detection.get(
            "dedicated_detector_status"
        )
        ocr_status = detection.get("ocr_status")
        ocr_candidate_ids = detection.get(
            "ocr_candidate_ids", []
        )
        ocr_observation_count = detection.get(
            "ocr_observation_count", 0
        )
        structured_status = vision.get(
            "structured_llm_semantics_status"
        )
        structured_revision = vision.get(
            "structured_vision_revision"
        )
        structured_judgments = vision.get(
            "structured_judgments_by_candidate"
        )
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in candidate_ids
            )
            or len(candidate_ids) != len(set(candidate_ids))
            or not isinstance(analysis_job_id, str)
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(temporal_revision, str)
            or _SHA256.fullmatch(temporal_revision) is None
            or not isinstance(detection_revision, str)
            or _SHA256.fullmatch(detection_revision) is None
            or detector_status
            not in {
                "completed",
                "zero_detections",
                "model_unavailable",
                "failed",
            }
            or ocr_status
            not in {
                "completed",
                "zero_detections",
                "model_unavailable",
                "failed",
            }
            or not isinstance(ocr_candidate_ids, list)
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in ocr_candidate_ids
            )
            or isinstance(ocr_observation_count, bool)
            or not isinstance(ocr_observation_count, int)
            or ocr_observation_count < 0
            or structured_status
            not in {"completed", "partial", "not_run", "failed"}
            or not isinstance(structured_revision, str)
            or _SHA256.fullmatch(structured_revision) is None
            or not isinstance(structured_judgments, Mapping)
            or set(structured_judgments) != set(candidate_ids)
            or any(
                not isinstance(item, Mapping)
                or item.get("candidate_id") != candidate_id
                or item.get("review_only") is not True
                or item.get("automatic_annotation_allowed")
                is not False
                or item.get("automatic_incident_allowed")
                is not False
                or item.get(
                    "automatic_training_membership_allowed"
                )
                is not False
                or item.get("automatic_report_truth_allowed")
                is not False
                for candidate_id, item
                in structured_judgments.items()
            )
        ):
            raise PipelineStageFailure(
                "question_queue_checkpoint_invalid"
            )
        overlay_checkpoint = (
            rescan.get("candidate_evidence_overlay")
            if isinstance(rescan, Mapping)
            else None
        )
        if (
            isinstance(overlay_checkpoint, Mapping)
            and self._review_event_sets is None
        ):
            raise PipelineStageFailure(
                "review_event_set_store_unavailable"
            )
        review_event_set = self._materialize_review_event_set(
            context
        )
        review_event_set_revision = (
            review_event_set.get("review_event_set_revision")
            if review_event_set is not None
            else None
        )
        review_event_ids = (
            list(review_event_set.get("event_ids", ()))
            if review_event_set is not None
            else []
        )
        review_event_bindings = (
            list(review_event_set.get("event_bindings", ()))
            if review_event_set is not None
            else []
        )
        if (
            review_event_set is not None
            and (
                review_event_set.get("event_count")
                != len(candidate_ids)
                or review_event_set.get("status")
                not in {"completed", "partial"}
                or not isinstance(
                    review_event_set_revision, str
                )
                or _SHA256.fullmatch(
                    review_event_set_revision
                )
                is None
                or len(review_event_ids) != len(candidate_ids)
                or len(review_event_bindings)
                != len(candidate_ids)
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(
                        r"rve_[a-f0-9]{32}", value
                    )
                    is None
                    for value in review_event_ids
                )
                or [
                    value.get("representative_candidate_id")
                    for value in review_event_bindings
                    if isinstance(value, Mapping)
                ]
                != candidate_ids
                or any(
                    not isinstance(value, Mapping)
                    or value.get("event_id")
                    != review_event_ids[index]
                    or not isinstance(
                        value.get("event_revision"), str
                    )
                    or _SHA256.fullmatch(
                        value["event_revision"]
                    )
                    is None
                    for index, value
                    in enumerate(review_event_bindings)
                )
            )
        ):
            raise PipelineStageFailure(
                "review_event_set_materialization_invalid"
            )
        # Register the immutable server-owned question identities before
        # projecting delegated annotations. Dense/DINO review events may not
        # exist in the base analysis catalog, so reversing this order would
        # persist an answer whose normalized Annotation cannot be reconciled.
        (
            question_catalog_state,
            question_catalog_revision,
        ) = self._register_question_catalog(
            context,
            analysis_job_id=analysis_job_id,
            source_sha256=source_sha256,
            candidate_ids=candidate_ids,
            review_event_set=review_event_set,
            review_event_bindings=review_event_bindings,
        )
        label_delegation = (
            self._apply_structured_training_label_delegation(
                context,
                analysis_job_id=analysis_job_id,
                candidate_ids=candidate_ids,
            )
        )
        delegated_candidate_ids = list(
            label_delegation["delegated_candidate_ids"]
        )
        human_question_candidate_ids = list(
            label_delegation["human_question_candidate_ids"]
        )
        mandatory_human_candidate_ids = list(
            label_delegation[
                "mandatory_human_candidate_ids"
            ]
        )
        training_annotation_count = int(
            label_delegation["training_annotation_count"]
        )
        queue_revision = hashlib.sha256(
            _canonical_json(
                {
                    "pipeline_job_id": str(context["job_id"]),
                    "analysis_job_id": analysis_job_id,
                    "source_sha256": source_sha256,
                    "detection_revision": detection_revision,
                    "temporal_merge_revision": temporal_revision,
                    "structured_vision_revision": (
                        structured_revision
                    ),
                    "review_event_set_revision": (
                        review_event_set_revision
                    ),
                    "review_event_set_id": (
                        review_event_set.get(
                            "review_event_set_id"
                        )
                        if review_event_set is not None
                        else None
                    ),
                    "review_event_set_manifest_sha256": (
                        review_event_set.get(
                            "manifest_sha256"
                        )
                        if review_event_set is not None
                        else None
                    ),
                    "review_event_bindings": (
                        review_event_bindings
                    ),
                    "question_candidate_ids": candidate_ids,
                    "ocr_candidate_ids": sorted(
                        ocr_candidate_ids
                    ),
                    "review_mode": context["profile"]["review_mode"],
                    "question_policy": context["profile"][
                        "question_policy"
                    ],
                    "label_confirmation_authority": (
                        label_delegation["authority"]
                    ),
                    "structured_label_delegation_revision": (
                        label_delegation["delegation_revision"]
                    ),
                }
            )
        ).hexdigest()
        capability_missing = []
        capability_failed = []
        if detector_status == "model_unavailable":
            capability_missing.append("dedicated_road_detector")
        elif detector_status == "failed":
            capability_failed.append("dedicated_road_detector")
        if (
            rescan.get("high_frequency_rescan_performed")
            is not True
        ):
            capability_missing.append(
                "high_frequency_candidate_rescan"
            )
        if ocr_status == "model_unavailable":
            capability_missing.append("korean_ocr")
        elif ocr_status == "failed":
            capability_failed.append("korean_ocr")
        if (
            sam2.get("mask_generated") is not True
            or sam2.get("tracking_performed") is not True
        ):
            capability_missing.append(
                "sam2_segmentation_tracking"
            )
        if structured_status == "not_run":
            capability_missing.append("structured_vision_llm")
        elif structured_status in {"partial", "failed"}:
            capability_failed.append("structured_vision_llm")
        if (
            review_event_set is not None
            and review_event_set.get("status") == "partial"
        ):
            capability_failed.append("review_event_evidence_partial")
        return PipelineStageResult(
            "success",
            checkpoint={
                "analysis_job_id": analysis_job_id,
                "source_sha256": source_sha256,
                "question_count": len(candidate_ids),
                "question_candidate_ids": candidate_ids,
                "human_question_count": len(
                    human_question_candidate_ids
                ),
                "human_question_candidate_ids": (
                    human_question_candidate_ids
                ),
                "structured_training_label_count": len(
                    delegated_candidate_ids
                ),
                "structured_training_label_candidate_ids": (
                    delegated_candidate_ids
                ),
                "mandatory_human_candidate_ids": (
                    mandatory_human_candidate_ids
                ),
                "label_confirmation_authority": (
                    label_delegation["authority"]
                ),
                "structured_label_delegation_state": (
                    label_delegation["state"]
                ),
                "structured_label_delegation_revision": (
                    label_delegation["delegation_revision"]
                ),
                "structured_label_delegation": label_delegation,
                "question_queue_revision": queue_revision,
                "review_event_set_state": (
                    "materialized"
                    if review_event_set is not None
                    else "legacy_candidate_queue"
                ),
                "review_event_set": review_event_set,
                "review_event_set_id": (
                    review_event_set.get(
                        "review_event_set_id"
                    )
                    if review_event_set is not None
                    else None
                ),
                "review_event_set_revision": (
                    review_event_set_revision
                ),
                "review_event_set_manifest_sha256": (
                    review_event_set.get("manifest_sha256")
                    if review_event_set is not None
                    else None
                ),
                "candidate_evidence_revision": (
                    review_event_set.get(
                        "candidate_evidence_revision"
                    )
                    if review_event_set is not None
                    else None
                ),
                "review_event_count": len(review_event_ids),
                "review_event_ids": review_event_ids,
                "review_event_bindings": review_event_bindings,
                "detection_revision": detection_revision,
                "temporal_merge_revision": temporal_revision,
                "structured_vision_revision": structured_revision,
                "structured_llm_semantics_status": (
                    structured_status
                ),
                "structured_judgments_by_candidate": (
                    dict(structured_judgments)
                ),
                "structured_proposal_count": vision.get(
                    "structured_proposal_count", 0
                ),
                "structured_not_run_count": vision.get(
                    "structured_not_run_count", 0
                ),
                "structured_failure_count": vision.get(
                    "structured_failure_count", 0
                ),
                "structured_comparison_mode": vision.get(
                    "comparison_mode"
                ),
                "proposal_sources": list(
                    detection.get("proposal_sources", ())
                ),
                "detected_issue_types": list(
                    detection.get("detected_issue_types", ())
                ),
                "dedicated_detector_status": detector_status,
                "ocr_status": ocr_status,
                "ocr_observation_count": ocr_observation_count,
                "ocr_candidate_ids": sorted(ocr_candidate_ids),
                "question_catalog_state": question_catalog_state,
                "question_catalog_revision": question_catalog_revision,
                "review_only": True,
                "automatic_labeling_allowed": bool(
                    delegated_candidate_ids
                ),
                "automatic_annotation_allowed": bool(
                    delegated_candidate_ids
                ),
                "automatic_incident_allowed": False,
                "automatic_training_membership_allowed": bool(
                    delegated_candidate_ids
                ),
                "automatic_fixed_validation_membership_allowed": False,
                "automatic_report_truth_allowed": False,
                "annotation_count": training_annotation_count,
                "incident_count": 0,
                "training_membership_count": len(
                    delegated_candidate_ids
                ),
                "report_incident_count": 0,
                "capability_missing": capability_missing,
                "capability_failed": capability_failed,
                "dino_box_proposals_are_final_truth": False,
            },
            metrics={
                "question_count": len(candidate_ids),
                "human_question_count": len(
                    human_question_candidate_ids
                ),
                "structured_training_label_count": len(
                    delegated_candidate_ids
                ),
                "ocr_observation_count": ocr_observation_count,
                "catalog_question_count": (
                    len(candidate_ids)
                    if question_catalog_state == "registered"
                    else 0
                ),
                "review_event_count": (
                    len(review_event_ids)
                ),
                "annotation_count": training_annotation_count,
                "incident_count": 0,
                "training_membership_count": len(
                    delegated_candidate_ids
                ),
                "report_incident_count": 0,
            },
            error_code=(
                "degraded_review_capabilities"
                if capability_missing or capability_failed
                else None
            ),
        )

    def reconcile_question_catalog(
        self, context: Mapping[str, Any]
    ) -> PipelineStageResult:
        """Replay only the exact prepared queue into the normalized catalog."""

        checkpoint = context.get("checkpoints", {}).get(
            "question_queue", {}
        )
        analysis_job_id = checkpoint.get("analysis_job_id")
        source_sha256 = checkpoint.get("source_sha256")
        candidate_ids = checkpoint.get("question_candidate_ids")
        if (
            not isinstance(analysis_job_id, str)
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(candidate_ids, list)
            or not candidate_ids
            or len(candidate_ids) != len(set(candidate_ids))
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"[a-f0-9]{20}", item) is None
                for item in candidate_ids
            )
        ):
            raise PipelineStageFailure(
                "question_queue_checkpoint_invalid"
            )
        state, revision = self._register_question_catalog(
            context,
            analysis_job_id=analysis_job_id,
            source_sha256=source_sha256,
            candidate_ids=candidate_ids,
            review_event_set=(
                checkpoint.get("review_event_set")
                if isinstance(
                    checkpoint.get("review_event_set"),
                    Mapping,
                )
                else None
            ),
            review_event_bindings=(
                checkpoint.get("review_event_bindings", ())
                if isinstance(
                    checkpoint.get("review_event_bindings"),
                    list,
                )
                else ()
            ),
        )
        if state != "registered" or revision is None:
            raise PipelineStageFailure(
                "question_catalog_projection_unavailable"
            )
        return PipelineStageResult(
            "success",
            checkpoint={
                **dict(checkpoint),
                "question_catalog_state": state,
                "question_catalog_revision": revision,
            },
            metrics={
                "question_count": len(candidate_ids),
                "catalog_question_count": len(candidate_ids),
                "annotation_count": 0,
                "incident_count": 0,
            },
            error_code=(
                checkpoint.get("error_code")
                if isinstance(checkpoint.get("error_code"), str)
                else None
            ),
        )

    @staticmethod
    def _validated_derived_dataset_checkpoint(
        checkpoint: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        dataset = checkpoint.get("dataset_version")
        if not isinstance(dataset, Mapping):
            raise PipelineStageFailure("dataset_snapshot_failed")
        dataset_version_id = dataset.get("dataset_version_id")
        try:
            canonical_id = str(UUID(str(dataset_version_id)))
        except (TypeError, ValueError, AttributeError):
            canonical_id = None
        if (
            dataset.get("state") != "prepared"
            or not isinstance(dataset_version_id, str)
            or canonical_id != dataset_version_id
            or not isinstance(dataset.get("dataset_hash"), str)
            or _SHA256.fullmatch(dataset["dataset_hash"]) is None
            or not isinstance(
                dataset.get("dataset_manifest_sha256"), str
            )
            or _SHA256.fullmatch(
                dataset["dataset_manifest_sha256"]
            )
            is None
            or not isinstance(
                dataset.get(
                    "approved_reference_members_sha256"
                ),
                str,
            )
            or _SHA256.fullmatch(
                dataset["approved_reference_members_sha256"]
            )
            is None
            or isinstance(
                dataset.get("training_gate_member_count"), bool
            )
            or not isinstance(
                dataset.get("training_gate_member_count"), int
            )
            or dataset.get(
                "approved_references_count_toward_training_gate"
            )
            is not False
            or dataset.get(
                "approved_references_count_toward_validation"
            )
            is not False
            or dataset.get("automatic_training_started") is not False
        ):
            raise PipelineStageFailure("dataset_snapshot_failed")
        return dataset

    @staticmethod
    def _survey_incident_authority(
        *,
        context: Mapping[str, Any],
        question: Mapping[str, Any],
        candidate_rescan: Any,
        review: Mapping[str, Any],
        analysis_job_id: str,
        source_sha256: str,
        question_queue_revision: str,
        review_completion_id: str,
        answer_ledger_sha256: str,
    ) -> Optional[dict[str, Any]]:
        """Verify the independent promoted-survey boundary for Incident truth.

        An ordinary human-reviewed training video deliberately returns no
        authority.  If a survey-QA record is present, every server-owned model,
        dense-index, ReviewEvent, question-catalog and final-QA binding must
        verify; malformed or partial survey provenance fails the stage instead
        of silently degrading into maintenance truth.
        """

        raw = review.get("survey_qa_completion")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise PipelineStageFailure(
                "derived_outputs_survey_authority_invalid"
            )
        required = {
            "schema_version",
            "project_id",
            "pipeline_job_id",
            "analysis_job_id",
            "source_sha256",
            "dense_index_sha256",
            "dense_candidate_discovery_revision",
            "question_queue_revision",
            "model_version_id",
            "dataset_version_id",
            "primary_artifact_sha256",
            "artifact_manifest_sha256",
            "dataset_hash",
            "fixed_validation_sha256",
            "source_analysis_lineage_sha256",
            "dataset_provenance_graph_sha256",
            "promoted_model_inference_sha256",
            "sampling_fps",
            "scanned_frame_count",
            "final_qa_choice",
            "review_completion_id",
            "server_answer_ledger_revision",
            "review_event_set_collection_revision",
            "question_catalog_collection_revision",
            "user_discovered_registrations_sha256",
            "user_discovered_candidate_count",
            "actor",
            "recorded_at",
            "survey_qa_revision",
        }
        if set(raw) != required:
            raise PipelineStageFailure(
                "derived_outputs_survey_authority_invalid"
            )
        survey_qa = dict(raw)
        revision = survey_qa.pop("survey_qa_revision")
        final_qa_choice = survey_qa.get("final_qa_choice")
        user_discovered_count = survey_qa.get(
            "user_discovered_candidate_count"
        )
        if (
            not isinstance(revision, str)
            or _SHA256.fullmatch(revision) is None
            or hashlib.sha256(_canonical_json(survey_qa)).hexdigest()
            != revision
            or survey_qa.get("schema_version")
            != SURVEY_QA_COMPLETION_SCHEMA
            or survey_qa.get("project_id")
            != str(context.get("project_id"))
            or survey_qa.get("pipeline_job_id")
            != str(context.get("job_id"))
            or survey_qa.get("analysis_job_id")
            != analysis_job_id
            or survey_qa.get("source_sha256") != source_sha256
            or survey_qa.get("question_queue_revision")
            != question_queue_revision
            or survey_qa.get("review_completion_id")
            != review_completion_id
            or survey_qa.get("server_answer_ledger_revision")
            != answer_ledger_sha256
            or survey_qa.get("actor") != "local_user"
            or final_qa_choice
            not in {"no_miss_complete", "miss_candidates_reviewed"}
            or isinstance(user_discovered_count, bool)
            or not isinstance(user_discovered_count, int)
            or user_discovered_count < 0
            or (
                final_qa_choice == "no_miss_complete"
                and user_discovered_count != 0
            )
            or (
                final_qa_choice == "miss_candidates_reviewed"
                and user_discovered_count < 1
            )
            or survey_qa.get("sampling_fps") != 2
            or isinstance(
                survey_qa.get("scanned_frame_count"), bool
            )
            or not isinstance(
                survey_qa.get("scanned_frame_count"), int
            )
            or survey_qa["scanned_frame_count"] < 1
            or not isinstance(survey_qa.get("recorded_at"), str)
            or any(
                not isinstance(survey_qa.get(field), str)
                or _SHA256.fullmatch(survey_qa[field]) is None
                for field in (
                    "dense_index_sha256",
                    "dense_candidate_discovery_revision",
                    "primary_artifact_sha256",
                    "artifact_manifest_sha256",
                    "dataset_hash",
                    "fixed_validation_sha256",
                    "source_analysis_lineage_sha256",
                    "dataset_provenance_graph_sha256",
                    "promoted_model_inference_sha256",
                    "user_discovered_registrations_sha256",
                )
            )
            or any(
                not isinstance(survey_qa.get(field), str)
                or _IDENTIFIER.fullmatch(survey_qa[field]) is None
                for field in ("model_version_id", "dataset_version_id")
            )
        ):
            raise PipelineStageFailure(
                "derived_outputs_survey_authority_invalid"
            )

        if not isinstance(candidate_rescan, Mapping):
            raise PipelineStageFailure(
                "derived_outputs_survey_authority_invalid"
            )
        discovery = candidate_rescan.get(
            "dense_candidate_discovery"
        )
        if not isinstance(discovery, Mapping):
            raise PipelineStageFailure(
                "derived_outputs_survey_authority_invalid"
            )
        providers = discovery.get("provider_states")
        matching_providers = [
            provider
            for provider in providers
            if isinstance(provider, Mapping)
            and provider.get("provider")
            == "promoted_project_model"
            and provider.get("state") == "ready"
            and provider.get("independent_survey_verified") is True
            and provider.get("survey_analysis_job_id")
            == analysis_job_id
        ] if isinstance(providers, list) else []
        if len(matching_providers) != 1:
            raise PipelineStageFailure(
                "derived_outputs_survey_authority_invalid"
            )
        promoted = matching_providers[0]
        promoted_field_pairs = (
            ("model_version_id", "model_version_id"),
            ("dataset_version_id", "dataset_version_id"),
            ("primary_artifact_sha256", "primary_artifact_sha256"),
            (
                "artifact_manifest_sha256",
                "artifact_manifest_sha256",
            ),
            ("dataset_hash", "dataset_hash"),
            ("fixed_validation_sha256", "fixed_validation_sha256"),
            (
                "source_analysis_lineage_sha256",
                "source_analysis_lineage_sha256",
            ),
            (
                "dataset_provenance_graph_sha256",
                "dataset_provenance_graph_sha256",
            ),
        )
        expected_inference_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "pipeline_job_id": str(context["job_id"]),
                    "analysis_job_id": analysis_job_id,
                    "dense_candidate_discovery_revision": survey_qa[
                        "dense_candidate_discovery_revision"
                    ],
                    "provider_state": dict(promoted),
                    "active_model_artifact_manifest_sha256": (
                        survey_qa["artifact_manifest_sha256"]
                    ),
                }
            )
        ).hexdigest()
        registrations = question.get(
            "user_discovered_candidate_registrations", []
        )
        if (
            candidate_rescan.get("analysis_job_id")
            != analysis_job_id
            or candidate_rescan.get("source_sha256")
            != source_sha256
            or candidate_rescan.get("dense_index_sha256")
            != survey_qa["dense_index_sha256"]
            or candidate_rescan.get(
                "dense_candidate_discovery_revision"
            )
            != survey_qa["dense_candidate_discovery_revision"]
            or discovery.get("source_sha256") != source_sha256
            or discovery.get("dense_index_sha256")
            != survey_qa["dense_index_sha256"]
            or discovery.get("discovery_revision")
            != survey_qa["dense_candidate_discovery_revision"]
            or discovery.get("sampling_fps") != 2
            or discovery.get("scanned_frame_count")
            != survey_qa["scanned_frame_count"]
            or survey_qa["promoted_model_inference_sha256"]
            != expected_inference_sha256
            or not isinstance(registrations, list)
            or survey_qa["user_discovered_registrations_sha256"]
            != hashlib.sha256(
                _canonical_json(registrations)
            ).hexdigest()
            or len(registrations) != user_discovered_count
            or any(
                promoted.get(provider_field)
                != survey_qa[survey_field]
                for provider_field, survey_field
                in promoted_field_pairs
            )
        ):
            raise PipelineStageFailure(
                "derived_outputs_survey_authority_invalid"
            )

        review_event_state = question.get("review_event_set_state")
        question_catalog_state = question.get(
            "question_catalog_state"
        )
        event_binding: dict[str, Any]
        if review_event_state == "materialized":
            event_binding = {
                "state": review_event_state,
                "review_event_set_id": question.get(
                    "review_event_set_id"
                ),
                "review_event_set_revision": question.get(
                    "review_event_set_revision"
                ),
                "review_event_set_manifest_sha256": question.get(
                    "review_event_set_manifest_sha256"
                ),
            }
            if (
                not isinstance(
                    event_binding["review_event_set_id"], str
                )
                or re.fullmatch(
                    r"res_[a-f0-9]{32}",
                    event_binding["review_event_set_id"],
                )
                is None
                or any(
                    not isinstance(event_binding[field], str)
                    or _SHA256.fullmatch(event_binding[field]) is None
                    or review.get(field) != event_binding[field]
                    for field in (
                        "review_event_set_revision",
                        "review_event_set_manifest_sha256",
                    )
                )
                or review.get("review_event_set_id")
                != event_binding["review_event_set_id"]
            ):
                raise PipelineStageFailure(
                    "derived_outputs_survey_authority_invalid"
                )
        elif review_event_state == "materialized_collection":
            event_binding = {
                "state": review_event_state,
                "review_event_set_collection_revision": (
                    question.get(
                        "review_event_set_collection_revision"
                    )
                ),
            }
            collection_revision = event_binding[
                "review_event_set_collection_revision"
            ]
            if (
                not isinstance(collection_revision, str)
                or _SHA256.fullmatch(collection_revision) is None
                or review.get(
                    "review_event_set_collection_revision"
                )
                != collection_revision
                or survey_qa.get(
                    "review_event_set_collection_revision"
                )
                != collection_revision
            ):
                raise PipelineStageFailure(
                    "derived_outputs_survey_authority_invalid"
                )
        else:
            # Legacy queues may still yield training Annotations, but they do
            # not prove the Survey ReviewEvent state required for Incident.
            raise PipelineStageFailure(
                "derived_outputs_survey_authority_invalid"
            )

        catalog_binding: dict[str, Any]
        if question_catalog_state == "registered":
            collection_revision = question.get(
                "question_catalog_collection_revision"
            )
            if collection_revision is not None:
                catalog_binding = {
                    "state": question_catalog_state,
                    "question_catalog_collection_revision": (
                        collection_revision
                    ),
                }
                if (
                    not isinstance(collection_revision, str)
                    or _SHA256.fullmatch(collection_revision) is None
                    or review.get(
                        "question_catalog_collection_revision"
                    )
                    != collection_revision
                    or survey_qa.get(
                        "question_catalog_collection_revision"
                    )
                    != collection_revision
                ):
                    raise PipelineStageFailure(
                        "derived_outputs_survey_authority_invalid"
                    )
            else:
                catalog_revision = question.get(
                    "question_catalog_revision"
                )
                catalog_binding = {
                    "state": question_catalog_state,
                    "question_catalog_revision": catalog_revision,
                }
                if (
                    not isinstance(catalog_revision, str)
                    or _SHA256.fullmatch(catalog_revision) is None
                    or review.get("question_catalog_revision")
                    != catalog_revision
                ):
                    raise PipelineStageFailure(
                        "derived_outputs_survey_authority_invalid"
                    )
        else:
            raise PipelineStageFailure(
                "derived_outputs_survey_authority_invalid"
            )

        authority = {
            "schema_version": SURVEY_INCIDENT_AUTHORITY_SCHEMA,
            "project_id": str(context["project_id"]),
            "pipeline_job_id": str(context["job_id"]),
            "analysis_job_id": analysis_job_id,
            "source_sha256": source_sha256,
            "question_queue_revision": question_queue_revision,
            "review_completion_id": review_completion_id,
            "answer_ledger_sha256": answer_ledger_sha256,
            "survey_qa_revision": revision,
            "model_version_id": survey_qa["model_version_id"],
            "model_promotion_state": "promoted",
            "dataset_version_id": survey_qa["dataset_version_id"],
            "dataset_hash": survey_qa["dataset_hash"],
            "dataset_provenance_graph_sha256": survey_qa[
                "dataset_provenance_graph_sha256"
            ],
            "fixed_validation_sha256": survey_qa[
                "fixed_validation_sha256"
            ],
            "primary_artifact_sha256": survey_qa[
                "primary_artifact_sha256"
            ],
            "artifact_manifest_sha256": survey_qa[
                "artifact_manifest_sha256"
            ],
            "source_analysis_lineage_sha256": survey_qa[
                "source_analysis_lineage_sha256"
            ],
            "promoted_model_inference_sha256": survey_qa[
                "promoted_model_inference_sha256"
            ],
            "dense_index_sha256": survey_qa["dense_index_sha256"],
            "dense_candidate_discovery_revision": survey_qa[
                "dense_candidate_discovery_revision"
            ],
            "sampling_fps": survey_qa["sampling_fps"],
            "scanned_frame_count": survey_qa[
                "scanned_frame_count"
            ],
            "event_binding": event_binding,
            "question_catalog_binding": catalog_binding,
            "final_qa_choice": final_qa_choice,
            "user_discovered_registrations_sha256": survey_qa[
                "user_discovered_registrations_sha256"
            ],
            "user_discovered_candidate_count": user_discovered_count,
            "actor": "local_user",
            "independent_survey_verified": True,
        }
        return authority

    def _persist_model_iteration_qa(
        self,
        *,
        context: Mapping[str, Any],
        profile: Mapping[str, Any],
        question: Mapping[str, Any],
        review: Mapping[str, Any],
        next_dataset_version_id: str,
    ) -> dict[str, Any]:
        """Freeze promoted-survey QA after derived DatasetVersion projection.

        This path is deliberately reachable only after the independently
        verified survey Incident authority exists.  It records observations
        and the next TRAIN delta; it never starts training or promotes a
        model.
        """

        if (
            self._model_iteration_qa is None
            or self._dataset_version_resolver is None
            or self._candidate_labels is None
        ):
            raise ModelIterationQaError(
                "model iteration QA dependencies are unavailable"
            )
        completion = review.get("survey_qa_completion")
        capabilities = context.get("capabilities")
        promoted = (
            capabilities.get("promoted_project_model_binding")
            if isinstance(capabilities, Mapping)
            else None
        )
        candidate_ids = question.get("question_candidate_ids")
        analysis_job_id = question.get("analysis_job_id")
        if (
            not isinstance(completion, Mapping)
            or not isinstance(promoted, Mapping)
            or not isinstance(candidate_ids, list)
            or not candidate_ids
            or not isinstance(analysis_job_id, str)
        ):
            raise ModelIterationQaError(
                "promoted survey model QA provenance is unavailable"
            )
        project_id = UUID(str(context["project_id"]))
        next_dataset_id = UUID(next_dataset_version_id)
        next_dataset = self._dataset_version_resolver(
            project_id, next_dataset_id
        )
        qa_scope_sha256 = hashlib.sha256(
            _canonical_json(
                {
                    "schema_version": (
                        "trafficvisson.model-survey-qa-scope.v1"
                    ),
                    "project_id": str(project_id),
                    "review_mode": profile.get("review_mode"),
                    "question_policy": profile.get(
                        "question_policy"
                    ),
                    "dense_sampling_fps": 2,
                    "candidate_rescan_fps": profile.get(
                        "candidate_rescan_fps"
                    ),
                }
            )
        ).hexdigest()

        survey_rows = self._model_iteration_qa.list_survey_model_qa(
            project_id
        )
        existing_surveys = [
            item
            for item in survey_rows
            if item["pipeline_job_id"]
            == completion.get("pipeline_job_id")
            and item["survey_qa_revision"]
            == completion.get("survey_qa_revision")
        ]
        if len(existing_surveys) > 1:
            raise ModelIterationQaError(
                "survey QA ledger identity is duplicated"
            )
        if existing_surveys:
            survey = existing_surveys[0]
        else:
            latest = self._candidate_labels.latest_answers(
                project_id,
                analysis_job_id=UUID(analysis_job_id),
            )
            outcomes = build_reviewed_survey_outcomes(
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                question_candidate_ids=candidate_ids,
                latest_answer_records=latest,
                next_dataset_version=next_dataset,
                promoted_model_binding=promoted,
            )
            baselines = [
                item
                for item in survey_rows
                if item["qa_scope_sha256"] == qa_scope_sha256
            ]
            survey = build_survey_model_qa(
                project_id=project_id,
                promoted_model_binding=promoted,
                survey_qa_completion=completion,
                reviewed_outcomes=outcomes,
                qa_scope_sha256=qa_scope_sha256,
                baseline_snapshot=(
                    baselines[-1] if baselines else None
                ),
            )
            survey = self._model_iteration_qa.put_survey_model_qa(
                survey
            )

        iterations = self._model_iteration_qa.list_model_iterations(
            project_id
        )
        current = [
            item
            for item in iterations
            if item["survey_model_qa_sha256"]
            == survey["survey_model_qa_sha256"]
        ]
        if len(current) > 1:
            raise ModelIterationQaError(
                "model iteration survey identity is duplicated"
            )
        if current:
            iteration = current[0]
            if (
                iteration["next_dataset"]["dataset_version_id"]
                != next_dataset_version_id
            ):
                raise ModelIterationQaError(
                    "persisted model iteration DatasetVersion changed"
                )
        else:
            same_model = [
                item
                for item in iterations
                if same_promoted_model_artifact(
                    item["parent_promoted_model_binding"],
                    survey["promoted_model_binding"],
                )
            ]
            prior = same_model[-1] if same_model else None
            parent_dataset_id = (
                prior["next_dataset"]["dataset_version_id"]
                if prior is not None
                else survey["promoted_model_binding"][
                    "dataset_version_id"
                ]
            )
            parent_dataset = self._dataset_version_resolver(
                project_id, UUID(parent_dataset_id)
            )
            iteration = build_model_iteration(
                project_id=project_id,
                parent_promoted_model=promoted,
                parent_dataset_version=parent_dataset,
                survey_model_qa=survey,
                next_dataset_version=next_dataset,
                parent_iteration=prior,
            )
            iteration = (
                self._model_iteration_qa.put_model_iteration(
                    iteration
                )
            )

        summary = survey["summary"]
        return {
            "schema_version": (
                "trafficvisson.model-iteration-checkpoint.v1"
            ),
            "state": "prepared",
            "survey_model_qa_sha256": survey[
                "survey_model_qa_sha256"
            ],
            "model_iteration_sha256": iteration[
                "model_iteration_sha256"
            ],
            "parent_model_version_id": survey[
                "promoted_model_binding"
            ]["model_version_id"],
            "parent_dataset_version_id": iteration[
                "parent_dataset"
            ]["dataset_version_id"],
            "next_dataset_version_id": iteration[
                "next_dataset"
            ]["dataset_version_id"],
            "true_positive_count": summary[
                "true_positive_count"
            ],
            "false_positive_count": summary[
                "false_positive_count"
            ],
            "false_negative_count": summary[
                "false_negative_count"
            ],
            "held_count": summary["held_count"],
            "drift_assessed": survey["drift"]["assessed"],
            "next_training_requires_user_confirmation": True,
            "automatic_training_started": False,
            "automatic_model_promotion": False,
            "iteration_creates_incident": False,
            "iteration_creates_report_truth": False,
        }

    def _generate_derived_outputs(
        self,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        if self._derived_outputs is None:
            return PipelineStageResult(
                "model_unavailable",
                error_code="derived_outputs_runtime_unavailable",
            )
        checkpoints = context.get("checkpoints")
        profile = context.get("profile")
        capabilities = context.get("capabilities")
        review = context.get("review_completion")
        if (
            not isinstance(checkpoints, Mapping)
            or not isinstance(profile, Mapping)
            or not isinstance(capabilities, Mapping)
            or not isinstance(review, Mapping)
        ):
            raise PipelineStageFailure(
                "derived_outputs_checkpoint_invalid"
            )
        question = checkpoints.get("question_queue")
        sha = checkpoints.get("sha256")
        candidate_rescan = checkpoints.get(
            "candidate_rescan_5_10fps"
        )
        if not isinstance(sha, Mapping):
            raise PipelineStageFailure(
                "derived_outputs_checkpoint_invalid"
            )

        source_sha256 = sha.get("source_sha256")
        input_fingerprint = context.get("input_fingerprint")
        profile_sha256 = context.get("profile_sha256")
        if (
            not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(input_fingerprint, str)
            or _SHA256.fullmatch(input_fingerprint) is None
            or not isinstance(profile_sha256, str)
            or _SHA256.fullmatch(profile_sha256) is None
            or hashlib.sha256(_canonical_json(profile)).hexdigest()
            != profile_sha256
        ):
            raise PipelineStageFailure(
                "derived_outputs_provenance_invalid"
            )
        completion_mode = "reviewed"
        if isinstance(question, Mapping):
            zero_candidate_survey_review = (
                question.get("schema_version")
                == ZERO_CANDIDATE_SURVEY_QUEUE_SCHEMA
                and question.get("question_count") == 0
            )
            if zero_candidate_survey_review:
                completion_mode = "zero_detections"
            candidate_ids = question.get("question_candidate_ids")
            analysis_job_id = question.get("analysis_job_id")
            question_source_sha256 = question.get("source_sha256")
            question_queue_revision = question.get(
                "question_queue_revision"
            )
            question_catalog_state = question.get(
                "question_catalog_state"
            )
            question_catalog_revision = question.get(
                "question_catalog_revision"
            )
            question_catalog_collection_revision = question.get(
                "question_catalog_collection_revision"
            )
            review_event_set_state = question.get(
                "review_event_set_state"
            )
            review_event_set_id = question.get(
                "review_event_set_id"
            )
            review_event_set_revision = question.get(
                "review_event_set_revision"
            )
            review_event_set_manifest_sha256 = question.get(
                "review_event_set_manifest_sha256"
            )
            review_event_set_collection_revision = question.get(
                "review_event_set_collection_revision"
            )
            review_event_binding_invalid = (
                review_event_set_state
                not in {
                    None,
                    "legacy_candidate_queue",
                    "materialized",
                    "materialized_collection",
                    "empty_promoted_survey",
                }
                or (
                    review_event_set_state
                    == "empty_promoted_survey"
                    and not zero_candidate_survey_review
                )
                or (
                    review_event_set_state == "materialized"
                    and (
                        not isinstance(
                            review_event_set_id, str
                        )
                        or re.fullmatch(
                            r"res_[a-f0-9]{32}",
                            review_event_set_id,
                        )
                        is None
                        or not isinstance(
                            review_event_set_revision, str
                        )
                        or _SHA256.fullmatch(
                            review_event_set_revision
                        )
                        is None
                        or not isinstance(
                            review_event_set_manifest_sha256,
                            str,
                        )
                        or _SHA256.fullmatch(
                            review_event_set_manifest_sha256
                        )
                        is None
                        or review.get(
                            "review_event_set_id"
                        )
                        != review_event_set_id
                        or review.get(
                            "review_event_set_revision"
                        )
                        != review_event_set_revision
                        or review.get(
                            "review_event_set_manifest_sha256"
                        )
                        != review_event_set_manifest_sha256
                    )
                )
                or (
                    review_event_set_state
                    == "materialized_collection"
                    and (
                        not isinstance(
                            review_event_set_collection_revision,
                            str,
                        )
                        or _SHA256.fullmatch(
                            review_event_set_collection_revision
                        )
                        is None
                        or review.get(
                            "review_event_set_collection_revision"
                        )
                        != review_event_set_collection_revision
                    )
                )
            )
            question_catalog_binding_invalid = (
                question_catalog_state
                not in {
                    None,
                    "sidecar_only",
                    "registered",
                    "empty_promoted_survey",
                }
                or (
                    question_catalog_state
                    == "empty_promoted_survey"
                    and not zero_candidate_survey_review
                )
                or (
                    question_catalog_state == "registered"
                    and (
                        not isinstance(
                            question_catalog_revision, str
                        )
                        or _SHA256.fullmatch(
                            question_catalog_revision
                        )
                        is None
                        or review.get(
                            "question_catalog_revision"
                        )
                        != question_catalog_revision
                    )
                )
                or (
                    review_event_set_state
                    == "materialized_collection"
                    and (
                        not isinstance(
                            question_catalog_collection_revision,
                            str,
                        )
                        or _SHA256.fullmatch(
                            question_catalog_collection_revision
                        )
                        is None
                        or review.get(
                            "question_catalog_collection_revision"
                        )
                        != question_catalog_collection_revision
                    )
                )
                or (
                    question_catalog_state
                    in {None, "sidecar_only"}
                    and question_catalog_revision is not None
                )
            )
            review_completion_id = context.get(
                "review_completion_id"
            )
            answer_ledger_sha256 = review.get(
                "server_answer_ledger_revision"
            )
            if (
                not isinstance(candidate_ids, list)
                or (
                    not candidate_ids
                    and not zero_candidate_survey_review
                )
                or len(candidate_ids) != len(set(candidate_ids))
                or any(
                    not isinstance(item, str)
                    or re.fullmatch(r"[a-f0-9]{20}", item) is None
                    for item in candidate_ids
                )
                or not isinstance(analysis_job_id, str)
                or isinstance(question.get("question_count"), bool)
                or question.get("question_count") != len(candidate_ids)
                or not isinstance(question_queue_revision, str)
                or _SHA256.fullmatch(question_queue_revision) is None
                or question_source_sha256 != source_sha256
                or not isinstance(review_completion_id, str)
                or review.get("review_completion_id")
                != review_completion_id
                or review.get("analysis_job_id") != analysis_job_id
                or review.get("question_queue_revision")
                != question_queue_revision
                or review_event_binding_invalid
                or question_catalog_binding_invalid
                or review.get("all_required_questions_answered")
                is not True
                or isinstance(
                    review.get("answered_question_count"), bool
                )
                or review.get("answered_question_count")
                != len(candidate_ids)
                or not isinstance(answer_ledger_sha256, str)
                or _SHA256.fullmatch(answer_ledger_sha256) is None
                or review_completion_id
                != (
                    f"zero-detections:{answer_ledger_sha256}"
                    if zero_candidate_survey_review
                    else f"review:{answer_ledger_sha256}"
                )
                or (
                    zero_candidate_survey_review
                    and (
                        answer_ledger_sha256
                        != hashlib.sha256(b"").hexdigest()
                        or not isinstance(
                            review.get("survey_qa_completion"),
                            Mapping,
                        )
                        or review["survey_qa_completion"].get(
                            "final_qa_choice"
                        )
                        != "no_miss_complete"
                        or review["survey_qa_completion"].get(
                            "question_queue_revision"
                        )
                        != question_queue_revision
                    )
                )
            ):
                raise PipelineStageFailure(
                    "derived_outputs_provenance_invalid"
                )
        else:
            # No question queue is valid only for an exact, completed
            # zero-candidate analysis. Bind that branch to the analysis/source
            # checkpoint and the cryptographic empty ledger, then still emit
            # empty CSV/manifest artifacts instead of converting 0 detections
            # into a generic stage failure.
            completion_mode = "zero_detections"
            candidate_ids = []
            analysis_job_id = (
                candidate_rescan.get("analysis_job_id")
                if isinstance(candidate_rescan, Mapping)
                else None
            )
            candidate_source_sha256 = (
                candidate_rescan.get("source_sha256")
                if isinstance(candidate_rescan, Mapping)
                else None
            )
            candidate_count = (
                candidate_rescan.get("candidate_count")
                if isinstance(candidate_rescan, Mapping)
                else None
            )
            answer_ledger_sha256 = hashlib.sha256(b"").hexdigest()
            question_queue_revision = hashlib.sha256(
                _canonical_json(
                    {
                        "schema_version": (
                            LEGACY_ZERO_DETECTION_QUEUE_REVISION_SCHEMA
                        ),
                        "project_id": str(context["project_id"]),
                        "pipeline_job_id": str(context["job_id"]),
                        "analysis_job_id": analysis_job_id,
                        "source_sha256": source_sha256,
                        "question_candidate_ids": [],
                    }
                )
            ).hexdigest()
            review_completion_id = (
                f"zero-detections:{answer_ledger_sha256}"
            )
            if (
                not isinstance(analysis_job_id, str)
                or candidate_source_sha256 != source_sha256
                or isinstance(candidate_count, bool)
                or candidate_count != 0
                or context.get("review_completion_id") is not None
                or review
            ):
                raise PipelineStageFailure(
                    "derived_outputs_provenance_invalid"
                )
        survey_incident_authority_manifest = None
        survey_incident_authority_sha256 = None
        if completion_mode == "reviewed":
            survey_incident_authority_manifest = (
                self._survey_incident_authority(
                    context=context,
                    question=question,
                    candidate_rescan=candidate_rescan,
                    review=review,
                    analysis_job_id=analysis_job_id,
                    source_sha256=source_sha256,
                    question_queue_revision=question_queue_revision,
                    review_completion_id=review_completion_id,
                    answer_ledger_sha256=answer_ledger_sha256,
                )
            )
            if survey_incident_authority_manifest is not None:
                survey_incident_authority_sha256 = canonical_sha256(
                    survey_incident_authority_manifest
                )
        storage_capacity_revision = (
            self._storage_capacity_revision(
                context,
                stage="derived_outputs",
                required_bytes=(
                    _derived_outputs_capacity_upper_bound(
                        len(candidate_ids)
                    )
                ),
                basis={
                    "basis_type": (
                        "actual_reviewed_or_zero_event_set"
                    ),
                    "completion_mode": completion_mode,
                    "event_count": len(candidate_ids),
                    "derived_output_capacity_bytes": (
                        _derived_outputs_capacity_upper_bound(
                            len(candidate_ids)
                        )
                    ),
                    "review_completion_id": (
                        review_completion_id
                    ),
                },
            )
        )
        raw_route_name = profile.get("report_route_name", "")
        report_route_name = (
            raw_route_name
            if isinstance(raw_route_name, str)
            and raw_route_name != "unconfigured"
            else ""
        )
        report_inspection_date = (
            _report_inspection_date_from_source_filename(
                context.get("file_name"),
                policy=profile.get(
                    "report_inspection_date_policy",
                    REPORT_INSPECTION_DATE_POLICY,
                ),
            )
        )
        report_evidence_mode = profile.get(
            "report_evidence_mode",
            "internal_original",
        )
        # The pipeline does not own a deidentification catalog yet.  Passing
        # an empty verified set is deliberate: shared report preparation must
        # block rather than treating an original frame or a renamed copy as
        # deidentified evidence.
        verified_deidentified_evidence_by_incident_id: dict[
            str, Mapping[str, Any]
        ] = {}
        try:
            request = DerivedOutputsRequest(
                project_id=UUID(str(context["project_id"])),
                pipeline_job_id=UUID(str(context["job_id"])),
                analysis_job_id=UUID(analysis_job_id),
                source_sha256=source_sha256,
                pipeline_input_fingerprint=input_fingerprint,
                pipeline_profile_sha256=profile_sha256,
                review_completion_id=review_completion_id,
                answer_ledger_sha256=answer_ledger_sha256,
                question_queue_revision=question_queue_revision,
                question_candidate_ids=tuple(candidate_ids),
                template_id=str(profile["default_template_id"]),
                template_sha256=str(
                    profile["default_template_sha256"]
                ),
                report_provider=str(profile["report_provider"]),
                report_transmission_consent=bool(
                    profile.get(
                        "report_transmission_consent", False
                    )
                ),
                template_semantic_map_ready=(
                    profile.get("template_semantic_map_ready")
                    is True
                    and capabilities.get(
                        "template_semantic_map_ready"
                    )
                    is True
                    and capabilities.get(
                        "template_semantic_map_sha256"
                    )
                    == profile.get(
                        "template_semantic_map_sha256"
                    )
                ),
                report_provider_ready=(
                    capabilities.get("report_provider_ready")
                    is True
                    and capabilities.get(
                        "selected_report_provider"
                    )
                    == profile.get("report_provider")
                ),
                survey_incident_authority_sha256=(
                    survey_incident_authority_sha256
                ),
                survey_incident_authority_manifest=(
                    survey_incident_authority_manifest
                ),
                output_location_id=str(profile["output_location_id"]),
                # Every restarted/resumed derived stage gets a new bounded
                # attempt identity. A successful preview record still replays
                # idempotently if export persistence was interrupted.
                report_attempt=max(
                    0,
                    int(
                        context.get("stage_attempts", {}).get(
                            "derived_outputs", 1
                        )
                    )
                    - 1,
                ),
                completion_mode=completion_mode,
                template_semantic_map_id=str(
                    profile.get(
                        "template_semantic_map_id",
                        "unconfigured",
                    )
                ),
                template_semantic_map_sha256=str(
                    profile.get(
                        "template_semantic_map_sha256",
                        "0" * 64,
                    )
                ),
                report_route_name=report_route_name,
                report_inspector_name=str(
                    profile.get(
                        "report_inspector_name",
                        "unconfigured",
                    )
                ),
                report_inspection_date=report_inspection_date,
                report_policy={
                    "evidence_mode": report_evidence_mode,
                    # Checklist grades require an exact audited row and the
                    # user's final Incident severity.  That assignment occurs
                    # inside the exact binder; the durable caller must not
                    # invent a whole-road grade from model proposals.
                    "evaluation_ratings": [
                        "clear_unassessed"
                    ]
                    * 27,
                },
                verified_deidentified_evidence_by_incident_id=(
                    verified_deidentified_evidence_by_incident_id
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PipelineStageFailure(
                "derived_outputs_provenance_invalid"
            ) from error

        if cancel():
            return PipelineStageResult(
                "cancelled",
                error_code="derived_outputs_cancelled",
            )
        derived_artifact_root = None
        artifact_binding = context.get("artifact_root")
        if isinstance(artifact_binding, Mapping):
            output_location_id = artifact_binding.get(
                "output_location_id"
            )
            root_identity_sha256 = artifact_binding.get(
                "root_identity_sha256"
            )
            if (
                output_location_id != request.output_location_id
                or not isinstance(root_identity_sha256, str)
                or _SHA256.fullmatch(root_identity_sha256) is None
            ):
                raise PipelineStageFailure(
                    "derived_outputs_artifact_root_invalid"
                )
            _, derived_root = self._pipeline_artifact_directory(
                request.project_id,
                context,
                "derived-outputs",
            )
            derived_artifact_root = (
                ResolvedDerivedOutputsArtifactRoot(
                    category_root=derived_root,
                    project_id=request.project_id,
                    pipeline_job_id=request.pipeline_job_id,
                    analysis_job_id=request.analysis_job_id,
                    source_sha256=request.source_sha256,
                    output_location_id=str(output_location_id),
                    job_root_identity_sha256=(
                        root_identity_sha256
                    ),
                )
            )
        elif request.output_location_id != DEFAULT_OUTPUT_LOCATION_ID:
            raise PipelineStageFailure(
                "derived_outputs_artifact_root_invalid"
            )
        try:
            generate_kwargs: dict[str, Any] = {
                "cancelled": cancel,
            }
            if isinstance(
                self._derived_outputs, DerivedOutputsService
            ):
                generate_kwargs["artifact_root"] = (
                    derived_artifact_root
                )
            result = self._derived_outputs.generate(
                request,
                **generate_kwargs,
            )
        except DerivedOutputsConflict as error:
            raise PipelineStageFailure(
                "derived_outputs_provenance_conflict"
            ) from error
        except DatasetSnapshotFailed as error:
            raise PipelineStageFailure(
                "dataset_snapshot_failed"
            ) from error
        except DerivedOutputsError as error:
            if cancel():
                return PipelineStageResult(
                    "cancelled",
                    error_code="derived_outputs_cancelled",
                )
            raise PipelineStageFailure(
                "derived_outputs_generation_failed"
            ) from error

        checkpoint = {
            **dict(result.checkpoint),
            "storage_capacity_revision": (
                storage_capacity_revision
            ),
        }
        metrics = {
            **dict(result.metrics),
            "idempotent_replay": result.replayed,
            "storage_capacity_revision_sha256": (
                storage_capacity_revision.get(
                    "revision_sha256"
                )
                if isinstance(
                    storage_capacity_revision, Mapping
                )
                else None
            ),
        }
        if completion_mode == "zero_detections":
            if (
                checkpoint.get("state") != "completed"
                or checkpoint.get("completion_mode")
                != "zero_detections"
                or any(
                    metrics.get(field) != 0
                    for field in (
                        "raw_detection_count",
                        "reviewed_answer_count",
                        "incident_count",
                        "annotation_count",
                        "evidence_file_count",
                    )
                )
            ):
                raise PipelineStageFailure(
                    "zero_detections_output_invalid"
                )
            return PipelineStageResult(
                "zero_detections",
                checkpoint=checkpoint,
                metrics=metrics,
                error_code="zero_detections",
            )
        dataset_checkpoint = (
            self._validated_derived_dataset_checkpoint(checkpoint)
        )
        checkpoint = {
            **checkpoint,
            "dataset_version_id": dataset_checkpoint[
                "dataset_version_id"
            ],
            "dataset_hash": dataset_checkpoint["dataset_hash"],
            "dataset_manifest_sha256": dataset_checkpoint[
                "dataset_manifest_sha256"
            ],
            "approved_reference_members_sha256": (
                dataset_checkpoint[
                    "approved_reference_members_sha256"
                ]
            ),
        }
        if (
            survey_incident_authority_manifest is not None
            and self._model_iteration_qa is not None
        ):
            try:
                model_iteration_qa = (
                    self._persist_model_iteration_qa(
                        context=context,
                        profile=profile,
                        question=question,
                        review=review,
                        next_dataset_version_id=dataset_checkpoint[
                            "dataset_version_id"
                        ],
                    )
                )
            except (
                CandidateLabelError,
                ModelIterationQaError,
                TypeError,
                ValueError,
            ) as error:
                raise PipelineStageFailure(
                    "model_iteration_qa_invalid"
                ) from error
            checkpoint = {
                **checkpoint,
                "model_iteration_qa": model_iteration_qa,
            }
        if checkpoint.get("state") == "blocked":
            report = checkpoint.get("report")
            reasons = (
                report.get("blocking_reasons")
                if isinstance(report, Mapping)
                else None
            )
            error_code = "report_preview_blocked"
            if (
                isinstance(reasons, list)
                and len(reasons) == 1
                and isinstance(reasons[0], str)
                and _SAFE_ERROR_CODE.fullmatch(reasons[0])
                is not None
            ):
                error_code = reasons[0]
            return PipelineStageResult(
                "report_preview_blocked",
                checkpoint=checkpoint,
                metrics=metrics,
                error_code=error_code,
            )
        if checkpoint.get("state") == "partial":
            # Backward-compatible recovery for a pre-v2 derived record. New
            # writes use the explicit blocked state above.
            return PipelineStageResult(
                "report_preview_blocked",
                checkpoint=checkpoint,
                metrics=metrics,
                error_code="report_preview_blocked",
            )
        if checkpoint.get("state") == "completed":
            return PipelineStageResult(
                "success",
                checkpoint=checkpoint,
                metrics=metrics,
            )
        raise PipelineStageFailure("derived_outputs_result_invalid")

    def cancel(self, context: Mapping[str, Any]) -> None:
        job_id = str(context.get("job_id", ""))
        with self._lock:
            hash_job_id = self._active_hash_jobs.get(job_id)
            analysis_job_id = self._active_analysis_jobs.get(job_id)
        if analysis_job_id is not None and self._analysis_jobs is not None:
            try:
                self._analysis_jobs.cancel(
                    UUID(str(context["project_id"])),
                    analysis_job_id,
                )
            except HTTPException:
                pass
        if hash_job_id is None:
            return
        try:
            self._media_intake.cancel_hash_job(
                UUID(str(context["project_id"])), hash_job_id
            )
        except HTTPException:
            pass

    def validate_review_completion(
        self,
        context: Mapping[str, Any],
        review_completion_id: str,
    ) -> Mapping[str, Any]:
        if self._candidate_labels is None:
            raise PipelineStageFailure("review_verifier_unavailable")
        checkpoint = context.get("checkpoints", {}).get(
            "question_queue", {}
        )
        candidate_ids = checkpoint.get("question_candidate_ids")
        raw_analysis_job_id = checkpoint.get("analysis_job_id")
        review_event_set_state = checkpoint.get(
            "review_event_set_state"
        )
        review_event_set_id = checkpoint.get(
            "review_event_set_id"
        )
        review_event_set_revision = checkpoint.get(
            "review_event_set_revision"
        )
        review_event_set_manifest_sha256 = checkpoint.get(
            "review_event_set_manifest_sha256"
        )
        review_event_set_collection_revision = checkpoint.get(
            "review_event_set_collection_revision"
        )
        question_catalog_collection_revision = checkpoint.get(
            "question_catalog_collection_revision"
        )
        question_catalog_registrations = checkpoint.get(
            "question_catalog_registrations"
        )
        computed_question_catalog_collection_revision = (
            hashlib.sha256(
                _canonical_json(question_catalog_registrations)
            ).hexdigest()
            if isinstance(question_catalog_registrations, list)
            else None
        )
        review_event_bindings = checkpoint.get(
            "review_event_bindings", []
        )
        review_event_sets = checkpoint.get("review_event_sets")
        computed_review_collection_revision = None
        if (
            isinstance(candidate_ids, list)
            and isinstance(review_event_sets, list)
            and isinstance(review_event_bindings, list)
            and isinstance(raw_analysis_job_id, str)
            and isinstance(checkpoint.get("source_sha256"), str)
        ):
            computed_review_collection_revision = hashlib.sha256(
                _canonical_json(
                    {
                        "pipeline_job_id": str(context["job_id"]),
                        "analysis_job_id": raw_analysis_job_id,
                        "source_sha256": checkpoint[
                            "source_sha256"
                        ],
                        "question_candidate_ids": candidate_ids,
                        "review_event_sets": review_event_sets,
                        "review_event_bindings": review_event_bindings,
                    }
                )
            ).hexdigest()
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or len(candidate_ids) != len(set(candidate_ids))
            or review_event_set_state
            not in {
                None,
                "legacy_candidate_queue",
                "materialized",
                "materialized_collection",
            }
            or (
                review_event_set_state == "materialized"
                and (
                    not isinstance(review_event_set_id, str)
                    or re.fullmatch(
                        r"res_[a-f0-9]{32}",
                        review_event_set_id,
                    )
                    is None
                    or not isinstance(
                        review_event_set_revision, str
                    )
                    or _SHA256.fullmatch(
                        review_event_set_revision
                    )
                    is None
                    or not isinstance(
                        review_event_set_manifest_sha256,
                        str,
                    )
                    or _SHA256.fullmatch(
                        review_event_set_manifest_sha256
                    )
                    is None
                    or not isinstance(
                        review_event_bindings, list
                    )
                    or len(review_event_bindings)
                    != len(candidate_ids)
                )
            )
            or (
                review_event_set_state
                == "materialized_collection"
                and (
                    not isinstance(
                        review_event_set_collection_revision,
                        str,
                    )
                    or _SHA256.fullmatch(
                        review_event_set_collection_revision
                    )
                    is None
                    or computed_review_collection_revision
                    != review_event_set_collection_revision
                    or not isinstance(review_event_sets, list)
                    or not review_event_sets
                    or not isinstance(
                        question_catalog_collection_revision,
                        str,
                    )
                    or _SHA256.fullmatch(
                        question_catalog_collection_revision
                    )
                    is None
                    or computed_question_catalog_collection_revision
                    != question_catalog_collection_revision
                    or not isinstance(
                        review_event_bindings, list
                    )
                    or len(review_event_bindings)
                    != len(candidate_ids)
                    or any(
                        not isinstance(item, Mapping)
                        or not isinstance(
                            item.get("review_event_set_id"),
                            str,
                        )
                        or re.fullmatch(
                            r"res_[a-f0-9]{32}",
                            str(
                                item.get(
                                    "review_event_set_id"
                                )
                            ),
                        )
                        is None
                        or not isinstance(
                            item.get(
                                "review_event_set_revision"
                            ),
                            str,
                        )
                        or _SHA256.fullmatch(
                            str(
                                item.get(
                                    "review_event_set_revision"
                                )
                            )
                        )
                        is None
                        or not isinstance(
                            item.get(
                                "review_event_set_manifest_sha256"
                            ),
                            str,
                        )
                        or _SHA256.fullmatch(
                            str(
                                item.get(
                                    "review_event_set_manifest_sha256"
                                )
                            )
                        )
                        is None
                        for item in review_event_bindings
                    )
                )
            )
        ):
            raise PipelineStageFailure(
                "question_queue_checkpoint_invalid"
            )
        try:
            analysis_job_id = UUID(str(raw_analysis_job_id))
            latest = self._candidate_labels.latest_answers(
                UUID(str(context["project_id"])),
                analysis_job_id=analysis_job_id,
            )
        except (
            CandidateLabelError,
            TypeError,
            ValueError,
        ) as error:
            raise PipelineStageFailure(
                "review_verifier_unavailable"
            ) from error
        latest_by_candidate = {
            str(item["candidate_id"]): item for item in latest
        }
        expected_event_by_candidate = {
            str(item.get("representative_candidate_id")): item
            for item in review_event_bindings
            if isinstance(item, Mapping)
        }
        proofs = []
        for candidate_id in candidate_ids:
            record = latest_by_candidate.get(str(candidate_id))
            if record is None:
                raise PipelineStageFailure("review_answers_incomplete")
            record_id = record.get("record_id")
            record_hash = record.get("record_hash")
            revision = record.get("answer_revision")
            if (
                not isinstance(record_id, str)
                or not isinstance(record_hash, str)
                or _SHA256.fullmatch(record_hash) is None
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise PipelineStageFailure(
                    "review_answer_proof_invalid"
                )
            if review_event_set_state == "materialized":
                expected_event = expected_event_by_candidate.get(
                    str(candidate_id)
                )
                candidate = record.get("ai_original_candidate")
                binding = (
                    candidate.get("review_event_binding")
                    if isinstance(candidate, Mapping)
                    else None
                )
                if (
                    not isinstance(expected_event, Mapping)
                    or not isinstance(binding, Mapping)
                    or binding.get("review_event_set_id")
                    != review_event_set_id
                    or binding.get(
                        "review_event_set_revision"
                    )
                    != review_event_set_revision
                    or binding.get(
                        "review_event_set_manifest_sha256"
                    )
                    != review_event_set_manifest_sha256
                    or binding.get("review_event_id")
                    != expected_event.get("event_id")
                    or binding.get("review_event_revision")
                    != expected_event.get("event_revision")
                    or binding.get(
                        "representative_candidate_id"
                    )
                    != candidate_id
                    or binding.get("review_only") is not True
                ):
                    raise PipelineStageFailure(
                        "review_answer_event_binding_invalid"
                    )
            elif review_event_set_state == "materialized_collection":
                expected_event = expected_event_by_candidate.get(
                    str(candidate_id)
                )
                candidate = record.get("ai_original_candidate")
                binding = (
                    candidate.get("review_event_binding")
                    if isinstance(candidate, Mapping)
                    else None
                )
                if (
                    not isinstance(expected_event, Mapping)
                    or not isinstance(binding, Mapping)
                    or binding.get("review_event_set_id")
                    != expected_event.get(
                        "review_event_set_id"
                    )
                    or binding.get(
                        "review_event_set_revision"
                    )
                    != expected_event.get(
                        "review_event_set_revision"
                    )
                    or binding.get(
                        "review_event_set_manifest_sha256"
                    )
                    != expected_event.get(
                        "review_event_set_manifest_sha256"
                    )
                    or binding.get("review_event_id")
                    != expected_event.get("event_id")
                    or binding.get("review_event_revision")
                    != expected_event.get("event_revision")
                    or binding.get(
                        "representative_candidate_id"
                    )
                    != candidate_id
                    or binding.get("review_only") is not True
                ):
                    raise PipelineStageFailure(
                        "review_answer_event_binding_invalid"
                    )
            proofs.append(
                "\0".join(
                    (
                        str(candidate_id),
                        record_id.lower(),
                        record_hash,
                        str(revision),
                    )
                )
            )
        expected_completion_id = (
            _review_completion_id_from_proof_fragments(proofs)
        )
        ledger_sha256 = expected_completion_id.split(":", 1)[1]
        if review_completion_id != expected_completion_id:
            raise PipelineStageFailure(
                "review_completion_identity_mismatch"
            )
        return {
            "review_completion_id": review_completion_id,
            "all_required_questions_answered": True,
            "answered_question_count": len(candidate_ids),
            "server_answer_ledger_revision": ledger_sha256,
            "analysis_job_id": str(analysis_job_id),
            "question_queue_revision": checkpoint.get(
                "question_queue_revision"
            ),
            "question_catalog_revision": checkpoint.get(
                "question_catalog_revision"
            ),
            "review_event_set_id": review_event_set_id,
            "review_event_set_revision": (
                review_event_set_revision
            ),
            "review_event_set_manifest_sha256": (
                review_event_set_manifest_sha256
            ),
            "review_event_set_collection_revision": (
                review_event_set_collection_revision
            ),
            "question_catalog_collection_revision": (
                question_catalog_collection_revision
            ),
        }

    def _hash_stage(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        previous = context.get("checkpoints", {}).get("sha256", {})
        hash_job_id: Optional[UUID] = None
        if isinstance(previous, Mapping) and previous.get("hash_job_id"):
            try:
                candidate = UUID(str(previous["hash_job_id"]))
                existing = self._media_intake.get_hash_job(
                    project_id, candidate
                )
                if existing["status"] == "completed":
                    self._persist_completed_hash_grant(
                        project_id=project_id,
                        snapshot_id=snapshot_id,
                        file_name=file_name,
                        hash_job_id=candidate,
                        source_sha256=existing["sha256_lower"],
                    )
                    return PipelineStageResult(
                        "success",
                        checkpoint={
                            "hash_job_id": str(candidate),
                            "source_sha256": existing["sha256_lower"],
                            "total_bytes": existing["total_bytes"],
                        },
                        metrics={"bytes_hashed": existing["bytes_hashed"]},
                    )
                hash_job_id = candidate
            except (HTTPException, TypeError, ValueError):
                hash_job_id = None
        if hash_job_id is None:
            started = self._media_intake.start_hash_job(
                project_id, snapshot_id, file_name
            )
            hash_job_id = UUID(str(started["job_id"]))
        with self._lock:
            self._active_hash_jobs[str(context["job_id"])] = hash_job_id
        try:
            while True:
                if cancel():
                    self._media_intake.cancel_hash_job(
                        project_id, hash_job_id
                    )
                    return PipelineStageResult(
                        "cancelled", error_code="hash_cancelled"
                    )
                row = self._media_intake.get_hash_job(
                    project_id, hash_job_id
                )
                if row["status"] == "completed":
                    self._persist_completed_hash_grant(
                        project_id=project_id,
                        snapshot_id=snapshot_id,
                        file_name=file_name,
                        hash_job_id=hash_job_id,
                        source_sha256=row["sha256_lower"],
                    )
                    return PipelineStageResult(
                        "success",
                        checkpoint={
                            "hash_job_id": str(hash_job_id),
                            "source_sha256": row["sha256_lower"],
                            "total_bytes": row["total_bytes"],
                        },
                        metrics={"bytes_hashed": row["bytes_hashed"]},
                    )
                if row["status"] == "failed":
                    raise PipelineStageFailure(
                        f"hash_{row['error_code'] or 'failed'}"
                    )
                if row["status"] == "cancelled":
                    return PipelineStageResult(
                        "cancelled", error_code="hash_cancelled"
                    )
                time.sleep(0.05)
        finally:
            with self._lock:
                self._active_hash_jobs.pop(str(context["job_id"]), None)


__all__ = [
    "BridgePipelineRuntime",
    "MAX_PIPELINE_RETRIES",
    "PIPELINE_JOB_SCHEMA",
    "PIPELINE_PROFILE_SCHEMA",
    "PIPELINE_STAGES",
    "REPORT_INSPECTION_DATE_POLICY",
    "PipelineJobConflict",
    "PipelineJobError",
    "PipelineJobNotFound",
    "PipelineJobService",
    "PipelineJobUnavailable",
    "PipelineProfileMissing",
    "PipelineRuntime",
    "PipelineStageFailure",
    "PipelineStageResult",
]
