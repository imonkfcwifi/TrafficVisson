from __future__ import annotations

from datetime import datetime
import math
import re
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple, Union
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .candidate_labels import CandidateLabelError, canonical_issue_type
from .validation_metrics import (
    ModelValidationReport,
    PromotionReviewDecision,
)


QuestionStatus = Literal[
    "QUEUED",
    "ANSWERED_ACCEPT",
    "ANSWERED_REJECT",
    "ANSWERED_UNSURE",
    "CONFIRMED",
]
PolicyMode = Literal["learning_assist", "full_review", "quick_review"]
SourceType = Literal[
    "local_selection",
    "direct_url",
    "google_drive",
    "dropbox",
    "onedrive",
    "youtube",
    "webpage",
]
ProviderName = Literal["codex", "claude", "local"]
ArtifactName = Literal[
    "raw_detections",
    "reviewed_incidents",
    "label_questions",
    "annotations",
    "ocr_observations",
    "route_points",
    "maintenance_actions",
    "report_payload",
    "manifest",
]
MediaStabilityReason = Literal[
    "stable",
    "no_supported_media",
    "interval_too_short",
    "first_interval_changed",
    "second_interval_changed",
    "both_intervals_changed",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class EmptyRequest(StrictModel):
    pass


class HealthResponse(StrictModel):
    status: Literal["ok"]
    service: Literal["trafficvisson-bridge"]
    api_version: Literal["1.0"]
    fixture_mode: bool


class ProjectCreateRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2_000)


class ProjectResponse(StrictModel):
    id: UUID
    name: str
    description: Optional[str]
    question_policy: PolicyMode
    active_model_version: str
    created_at: datetime
    fixture: bool


class ProjectListResponse(StrictModel):
    projects: List[ProjectResponse]


class ImportInspectRequest(StrictModel):
    project_id: UUID
    source_type: SourceType
    source_url: Optional[AnyHttpUrl] = Field(default=None, max_length=2_048)
    local_selection_id: Optional[UUID] = None

    @model_validator(mode="after")
    def validate_source(self) -> "ImportInspectRequest":
        if self.source_type == "local_selection":
            if self.local_selection_id is None or self.source_url is not None:
                raise ValueError(
                    "local_selection requires only an opaque local_selection_id"
                )
        elif self.source_url is None or self.local_selection_id is not None:
            raise ValueError("URL sources require only source_url")
        return self


class ImportInspectResponse(StrictModel):
    inspected_source_id: UUID
    source_type: SourceType
    support_tier: Literal["official", "conditional", "best_effort"]
    source_display: str
    expected_bytes: Optional[int]
    duration_seconds: Optional[float]
    rights_confirmation_required: bool
    inspection_status: Literal["fixture_ready", "metadata_only"]
    warnings: List[str]


class ImportCreateRequest(StrictModel):
    project_id: UUID
    inspected_source_id: UUID
    rights_confirmed: bool


class ImportCreateResponse(StrictModel):
    import_id: UUID
    status: Literal["queued"]
    source_type: SourceType
    provenance_status: Literal["pending_download_hash"]


class MediaSnapshotRequest(StrictModel):
    project_id: UUID
    directory: str = Field(min_length=3, max_length=4_096)


class MediaLocalFileRegisterRequest(StrictModel):
    """Bridge-internal request populated by Electron main.

    The renderer supplies only an opaque native-picker selection ID. Electron
    resolves that handle and injects this absolute path over the authenticated
    loopback bridge.
    """

    project_id: UUID
    selected_path: str = Field(min_length=3, max_length=4_096)


class MediaFileMetadata(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    extension: Literal[
        ".avi",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mts",
        ".webm",
    ]
    size_bytes: int = Field(ge=0)
    created_time_ns: int = Field(ge=0)
    modified_time_ns: int = Field(ge=0)


class MediaSnapshotResponse(StrictModel):
    snapshot_id: UUID
    captured_at: datetime
    snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    digest_basis: Literal["name_size_creation_mtime_sha256_v1"]
    content_hash_status: Literal["not_computed"]
    file_count: int = Field(ge=0, le=10_000)
    total_size_bytes: int = Field(ge=0)
    files: List[MediaFileMetadata] = Field(max_length=10_000)


class MediaStabilityRequest(StrictModel):
    project_id: UUID
    snapshot_ids: Tuple[UUID, UUID, UUID]

    @field_validator("snapshot_ids")
    @classmethod
    def distinct_snapshots(
        cls, snapshot_ids: Tuple[UUID, UUID, UUID]
    ) -> Tuple[UUID, UUID, UUID]:
        if len(set(snapshot_ids)) != 3:
            raise ValueError("snapshot_ids must contain three distinct snapshots")
        return snapshot_ids


class MediaStabilityResponse(StrictModel):
    snapshot_ids: Tuple[UUID, UUID, UUID]
    first_interval_unchanged: bool
    second_interval_unchanged: bool
    minimum_interval_seconds: float = Field(ge=0, le=3_600)
    observed_interval_seconds: Tuple[float, float]
    stable: bool
    reason: MediaStabilityReason
    file_count: int = Field(ge=0, le=10_000)
    total_size_bytes: int = Field(ge=0)
    stable_snapshot_id: Optional[UUID]

    @field_validator("observed_interval_seconds")
    @classmethod
    def valid_observed_intervals(
        cls, intervals: Tuple[float, float]
    ) -> Tuple[float, float]:
        if any(value < 0 or value > 86_400 for value in intervals):
            raise ValueError("observed intervals are outside the supported range")
        return intervals


class MediaProbeRequest(StrictModel):
    project_id: UUID
    snapshot_id: UUID
    file_name: str = Field(min_length=1, max_length=255)
    timeout_seconds: int = Field(default=15, ge=1, le=30)


class MediaVideoStream(StrictModel):
    stream_index: int = Field(ge=0, le=127)
    codec_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    width: int = Field(gt=0, le=65_536)
    height: int = Field(gt=0, le=65_536)
    frame_rate: str = Field(pattern=r"^[0-9]{1,12}/[0-9]{1,12}$")


class MediaProbeResponse(StrictModel):
    snapshot_id: UUID
    snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    duration_seconds: float = Field(gt=0, le=2_678_400)
    format_names: List[str] = Field(min_length=1, max_length=16)
    video_streams: List[MediaVideoStream] = Field(min_length=1, max_length=128)
    ffprobe_status: Literal["validated"]
    content_hash_status: Literal["not_computed"]


class MediaHashStartRequest(StrictModel):
    project_id: UUID
    snapshot_id: UUID
    file_name: str = Field(min_length=1, max_length=255)


class MediaHashJobResponse(StrictModel):
    job_id: UUID
    snapshot_id: UUID
    file_name: str = Field(min_length=1, max_length=255)
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "completed",
        "cancelled",
        "failed",
    ]
    hash_algorithm: Literal["sha256"]
    chunk_size_bytes: Literal[4_194_304]
    bytes_hashed: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    sha256_lower: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    sha256_upper: Optional[str] = Field(
        default=None, pattern=r"^[A-F0-9]{64}$"
    )
    content_hash_status: Literal["pending", "computed", "not_available"]
    error_code: Optional[
        Literal["file_changed", "read_failed", "internal_error"]
    ] = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_hash_state(self) -> "MediaHashJobResponse":
        if self.bytes_hashed > self.total_bytes:
            raise ValueError("bytes_hashed must not exceed total_bytes")
        if self.status == "completed":
            if (
                self.sha256_lower is None
                or self.sha256_upper is None
                or self.sha256_upper != self.sha256_lower.upper()
                or self.bytes_hashed != self.total_bytes
                or self.content_hash_status != "computed"
                or self.error_code is not None
            ):
                raise ValueError("completed hash jobs require matching digests")
        elif self.status == "failed":
            if (
                self.sha256_lower is not None
                or self.sha256_upper is not None
                or self.content_hash_status != "not_available"
                or self.error_code is None
            ):
                raise ValueError("failed hash jobs require a bounded error code")
        elif self.status == "cancelled":
            if (
                self.sha256_lower is not None
                or self.sha256_upper is not None
                or self.content_hash_status != "not_available"
                or self.error_code is not None
            ):
                raise ValueError("cancelled hash jobs must not expose a digest")
        elif (
            self.sha256_lower is not None
            or self.sha256_upper is not None
            or self.content_hash_status != "pending"
            or self.error_code is not None
        ):
            raise ValueError("active hash jobs must not expose a digest")
        return self


class AnalysisJobStartRequest(StrictModel):
    project_id: UUID
    snapshot_id: UUID
    hash_job_id: UUID
    file_name: str = Field(min_length=1, max_length=255)
    sha256_lower: str = Field(pattern=r"^[a-f0-9]{64}$")


class AnalysisObservation(StrictModel):
    timestamp_seconds: float = Field(ge=0, le=2_678_400)
    scene_score: float = Field(ge=0, le=1)
    sharpness_score: float = Field(ge=0, le=1)
    triggers: List[str] = Field(min_length=1, max_length=8)


class AnalysisArtifact(StrictModel):
    relative_path: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: Literal["image/jpeg", "video/mp4"]
    artifact_ref: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^analysis:[a-f0-9-]{36}:[A-Za-z0-9][A-Za-z0-9._-]*$",
    )


class AnalysisNormalizedBox(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @field_validator("x", "y", "width", "height", mode="before")
    @classmethod
    def strict_finite_coordinate(cls, value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(
                "analysis box coordinates must be finite JSON numbers"
            )
        return float(value)

    @model_validator(mode="after")
    def contained(self) -> "AnalysisNormalizedBox":
        if self.x + self.width > 1.00000001 or self.y + self.height > 1.00000001:
            raise ValueError("analysis box must remain inside the image")
        return self


def _canonical_contract_issue_type(value: object) -> str:
    try:
        return canonical_issue_type(value)
    except CandidateLabelError as error:
        raise ValueError("issue_type is not supported") from error


VisionIssueType = Annotated[
    Literal[
        "pothole",
        "road_surface_damage",
        "road_debris",
        "median_barrier_damage",
        "sign_damage",
        "sign_vegetation_occlusion",
        "roadside_mowing_required",
        "guide_sign_identity",
        "tunnel_identity",
        "tunnel_name_occlusion",
        "lane_marking_faded",
        "colored_guidance_faded",
        "other",
    ],
    BeforeValidator(_canonical_contract_issue_type),
]


class AnalysisVisionPrediction(StrictModel):
    issue_type: VisionIssueType
    confidence: float = Field(ge=0, le=1)
    box: Optional[AnalysisNormalizedBox] = None
    source_label: str = Field(min_length=1, max_length=128)
    calibration: Literal["model_score", "zero_shot_relative"]


class AnalysisOcrObservation(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    box: AnalysisNormalizedBox
    kind: Literal["sign_or_tunnel_text_unverified"]


class AnalysisCandidateInference(StrictModel):
    schema_version: Literal["trafficvisson.vision-inference.v1"]
    mode: Literal["local_model", "scene_fallback"]
    status: Literal[
        "classified",
        "no_detection",
        "provider_unavailable",
        "timed_out",
        "failed",
    ]
    provider: Literal[
        "none",
        "onnx_yolo_v8",
        "transformers_clip_zero_shot",
        "easyocr_ocr_only",
    ]
    pack_id: Optional[str] = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$"
    )
    pack_version: Optional[str] = Field(
        default=None, pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$"
    )
    manifest_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    capabilities: List[
        Literal["defect_detection", "candidate_classification", "ocr"]
    ] = Field(max_length=3)
    predictions: List[AnalysisVisionPrediction] = Field(max_length=32)
    ocr_observations: List[AnalysisOcrObservation] = Field(max_length=32)
    duration_ms: int = Field(ge=0, le=3_600_000)
    warning_code: Optional[
        Literal[
            "model_pack_not_configured",
            "model_pack_invalid",
            "model_timeout",
            "model_inference_failed",
        ]
    ] = None

    @model_validator(mode="after")
    def validate_mode(self) -> "AnalysisCandidateInference":
        if self.mode == "local_model":
            if (
                self.provider == "none"
                or self.pack_id is None
                or self.pack_version is None
                or self.manifest_sha256 is None
                or self.warning_code is not None
                or self.status not in {"classified", "no_detection"}
            ):
                raise ValueError("local-model inference requires model identity")
        else:
            if (
                self.predictions
                or self.ocr_observations
                or self.warning_code is None
                or self.status
                not in {"provider_unavailable", "timed_out", "failed"}
            ):
                raise ValueError("scene fallback cannot contain model predictions")
        return self


class AnalysisVisionSummary(StrictModel):
    schema_version: Literal["trafficvisson.vision-inference.v1"]
    mode: Literal["local_model", "scene_fallback"]
    engine: Literal[
        "none",
        "onnx_yolo_v8",
        "transformers_clip_zero_shot",
        "easyocr_ocr_only",
    ]
    pack_id: Optional[str] = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$"
    )
    pack_version: Optional[str] = Field(
        default=None, pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$"
    )
    manifest_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    capabilities: List[
        Literal["defect_detection", "candidate_classification", "ocr"]
    ] = Field(max_length=3)
    runtime_spdx: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$"
    )
    weights_spdx: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$"
    )
    redistribution_allowed: Optional[bool] = None
    candidate_count: int = Field(ge=0, le=512)
    status_counts: Dict[
        Literal[
            "classified",
            "no_detection",
            "provider_unavailable",
            "timed_out",
            "failed",
        ],
        int,
    ] = Field(max_length=5)
    warning_code: Optional[
        Literal[
            "model_pack_not_configured",
            "model_pack_invalid",
            "model_timeout",
            "model_inference_failed",
        ]
    ] = None

    @model_validator(mode="after")
    def validate_summary(self) -> "AnalysisVisionSummary":
        if sum(self.status_counts.values()) != self.candidate_count or any(
            value < 0 for value in self.status_counts.values()
        ):
            raise ValueError("vision status counts do not match candidates")
        if self.mode == "local_model":
            if (
                self.engine == "none"
                or self.pack_id is None
                or self.pack_version is None
                or self.manifest_sha256 is None
            ):
                raise ValueError("local vision summary requires model identity")
        elif (
            self.engine == "none"
            and any(
                value is not None
                for value in (
                    self.pack_id,
                    self.pack_version,
                    self.manifest_sha256,
                    self.runtime_spdx,
                    self.weights_spdx,
                    self.redistribution_allowed,
                )
            )
        ):
            raise ValueError("unconfigured vision summary cannot claim model identity")
        return self


class AnalysisCandidate(StrictModel):
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")
    start_seconds: float = Field(ge=0, le=2_678_400)
    end_seconds: float = Field(ge=0, le=2_678_400)
    representative_timestamp_seconds: float = Field(
        ge=0, le=2_678_400
    )
    scene_score: float = Field(ge=0, le=1)
    sharpness_score: float = Field(ge=0, le=1)
    triggers: List[str] = Field(min_length=1, max_length=8)
    observations: List[AnalysisObservation] = Field(
        min_length=1, max_length=512
    )
    representative_image: AnalysisArtifact
    preview_clip: AnalysisArtifact
    preview_start_seconds: float = Field(ge=0, le=2_678_400)
    preview_duration_seconds: float = Field(ge=0, le=5)
    ai_inference: Optional[AnalysisCandidateInference] = None


class AnalysisSourceLocation(StrictModel):
    """A single source-container coordinate, never a time-series route."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_meters: Optional[float] = Field(default=None, ge=-20_000, le=20_000)
    raw_iso6709: str = Field(
        min_length=8,
        max_length=64,
        pattern=(
            r"^[+-][0-9]{2}(?:\.[0-9]{1,12})?"
            r"[+-][0-9]{3}(?:\.[0-9]{1,12})?"
            r"(?:[+-][0-9]{1,6}(?:\.[0-9]{1,6})?)?/$"
        ),
    )
    scope: Literal["capture_start_only"]

    @model_validator(mode="after")
    def reject_zero_coordinate(self) -> "AnalysisSourceLocation":
        if self.latitude == 0 and self.longitude == 0:
            raise ValueError("zero coordinates are not usable location evidence")
        return self


class AnalysisProvenance(StrictModel):
    source_name: str = Field(min_length=1, max_length=255)
    source_size_bytes: int = Field(gt=0)
    source_modified_time_ns: int = Field(gt=0)
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    duration_seconds: float = Field(gt=0, le=2_678_400)
    format_names: List[str] = Field(min_length=1, max_length=16)
    video_stream_index: int = Field(ge=0, le=127)
    video_codec: str = Field(min_length=1, max_length=128)
    width: int = Field(gt=0, le=65_536)
    height: int = Field(gt=0, le=65_536)
    frame_rate: str = Field(pattern=r"^[0-9]{1,12}/[0-9]{1,12}$")
    ffmpeg_version: str = Field(min_length=1, max_length=256)
    ffprobe_version: str = Field(min_length=1, max_length=256)
    scene_score_method: str = Field(min_length=1, max_length=256)
    sharpness_score_method: str = Field(min_length=1, max_length=256)
    effective_cadence_seconds: float = Field(gt=0, le=86_400)
    minimum_selection_gap_seconds: float = Field(gt=0, le=86_400)
    limits: Dict[str, Union[float, int]] = Field(
        min_length=1, max_length=32
    )
    source_location: Optional[AnalysisSourceLocation] = None


class AnalysisResult(StrictModel):
    artifact_set_id: UUID
    candidate_count: int = Field(ge=0, le=512)
    artifact_count: int = Field(ge=0, le=1_024)
    total_artifact_bytes: int = Field(ge=0)
    candidates: List[AnalysisCandidate] = Field(max_length=512)
    provenance: AnalysisProvenance
    vision_inference: Optional[AnalysisVisionSummary] = None

    @model_validator(mode="after")
    def validate_counts(self) -> "AnalysisResult":
        if self.candidate_count != len(self.candidates):
            raise ValueError("candidate_count does not match candidates")
        if self.artifact_count != self.candidate_count * 2:
            raise ValueError("each candidate must have two evidence artifacts")
        calculated_bytes = sum(
            item.representative_image.size_bytes + item.preview_clip.size_bytes
            for item in self.candidates
        )
        if calculated_bytes != self.total_artifact_bytes:
            raise ValueError("total_artifact_bytes does not match artifacts")
        if (
            self.vision_inference is not None
            and self.vision_inference.candidate_count != self.candidate_count
        ):
            raise ValueError("vision candidate_count does not match candidates")
        return self


class AnalysisJobResponse(StrictModel):
    job_id: UUID
    project_id: UUID
    snapshot_id: UUID
    hash_job_id: UUID
    file_name: str = Field(min_length=1, max_length=255)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "completed",
        "cancelled",
        "failed",
    ]
    stage: Literal[
        "queued",
        "validating_source",
        "extracting_candidates",
        "cancelling",
        "completed",
        "cancelled",
        "failed",
    ]
    progress_percent: Optional[int] = Field(default=None, ge=0, le=100)
    pause_supported: Literal[False]
    error_code: Optional[
        Literal[
            "source_changed",
            "timeout",
            "output_limit",
            "extraction_failed",
            "internal_error",
        ]
    ] = None
    created_at: datetime
    updated_at: datetime
    result: Optional[AnalysisResult] = None

    @model_validator(mode="after")
    def validate_job_state(self) -> "AnalysisJobResponse":
        if self.status == "completed":
            if (
                self.stage != "completed"
                or self.progress_percent != 100
                or self.result is None
                or self.error_code is not None
            ):
                raise ValueError("completed analysis jobs require a result")
        elif self.status == "failed":
            if (
                self.stage != "failed"
                or self.result is not None
                or self.error_code is None
            ):
                raise ValueError("failed analysis jobs require an error code")
        elif self.status == "cancelled":
            if (
                self.stage != "cancelled"
                or self.result is not None
                or self.error_code is not None
            ):
                raise ValueError("cancelled analysis jobs must not expose output")
        elif self.result is not None or self.error_code is not None:
            raise ValueError("active analysis jobs must not expose output")
        return self


class AnalysisJobListResponse(StrictModel):
    jobs: List[AnalysisJobResponse] = Field(max_length=128)


class DenseIndexJobStartRequest(StrictModel):
    project_id: UUID
    analysis_job_id: UUID
    sampling_fps: int = Field(ge=1, le=5)
    confirm_review_only: Literal[True]

    @field_validator("sampling_fps", mode="before")
    @classmethod
    def strict_sampling_fps(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("sampling_fps must be an integer from 1 to 5")
        return value

    @field_validator("confirm_review_only", mode="before")
    @classmethod
    def require_explicit_review_confirmation(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("confirm_review_only must be explicitly true")
        return True


class DenseIndexReviewSafety(StrictModel):
    review_only: Literal[True]
    automatic_incident_creation: Literal[False]
    automatic_annotation_creation: Literal[False]
    automatic_training_ingest: Literal[False]
    automatic_maintenance_decision: Literal[False]
    automatic_model_promotion: Literal[False]


class DenseIndexResultSummary(StrictModel):
    artifact_set_id: UUID
    frame_count: int = Field(ge=1, le=30_000)
    total_frame_bytes: int = Field(gt=0, le=16 * 1024**3)
    index_artifact_ref: str = Field(
        pattern=(
            r"^dense-index:[0-9a-f]{8}-[0-9a-f]{4}-"
            r"[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-"
            r"[0-9a-f]{12}:frame-index$"
        )
    )


class DenseIndexJobResponse(StrictModel):
    job_id: UUID
    project_id: UUID
    analysis_job_id: UUID
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sampling_fps: int = Field(ge=1, le=5)
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "completed",
        "cancelled",
        "failed",
    ]
    stage: Literal[
        "queued",
        "resolving_source",
        "indexing_frames",
        "finalizing",
        "cancelling",
        "completed",
        "cancelled",
        "failed",
    ]
    progress_percent: Optional[int] = Field(default=None, ge=0, le=100)
    progress_state: Literal[
        "waiting",
        "determinate",
        "indeterminate",
        "terminal",
    ]
    progress_basis: Literal["stage_only_no_fabricated_frame_progress"]
    pause_supported: Literal[False]
    error_code: Optional[
        Literal[
            "source_changed",
            "timeout",
            "output_limit",
            "indexing_failed",
            "persistence_failed",
            "internal_error",
        ]
    ] = None
    created_at: datetime
    updated_at: datetime
    record_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    safety: DenseIndexReviewSafety
    result: Optional[DenseIndexResultSummary] = None

    @model_validator(mode="after")
    def validate_dense_index_state(self) -> "DenseIndexJobResponse":
        if self.status == "completed":
            if (
                self.stage != "completed"
                or self.progress_percent != 100
                or self.progress_state != "determinate"
                or self.error_code is not None
                or self.result is None
                or self.record_sha256 is None
            ):
                raise ValueError(
                    "completed dense-index jobs require verified output"
                )
        elif self.status == "failed":
            if (
                self.stage != "failed"
                or self.error_code is None
                or self.result is not None
            ):
                raise ValueError(
                    "failed dense-index jobs require an error code"
                )
        elif self.status == "cancelled":
            if (
                self.stage != "cancelled"
                or self.error_code is not None
                or self.result is not None
            ):
                raise ValueError(
                    "cancelled dense-index jobs cannot expose output"
                )
        elif (
            self.error_code is not None
            or self.result is not None
            or self.record_sha256 is not None
        ):
            raise ValueError(
                "active dense-index jobs cannot expose terminal output"
            )
        return self


class DenseIndexJobListResponse(StrictModel):
    jobs: List[DenseIndexJobResponse] = Field(max_length=128)


PipelineStageName = Literal[
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
]
PipelineOutcomeCode = Literal[
    "success",
    "zero_detections",
    "model_unavailable",
    "blocked_gpx_required",
    "partial",
    "failed",
    "cancelled",
]


class PipelineProfileConfigureRequest(StrictModel):
    project_id: UUID
    question_policy: PolicyMode
    review_mode: Literal["precision", "quick"]
    default_template_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    default_template_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    detector_pack_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    ocr_pack_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    sam2_pack_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    report_provider: Literal["codex", "claude", "local", "none"]
    output_location_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    candidate_rescan_fps: int = Field(ge=5, le=10)

    @field_validator("candidate_rescan_fps", mode="before")
    @classmethod
    def strict_rescan_fps(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("candidate_rescan_fps must be an integer")
        return value


class PipelineProfileResponse(StrictModel):
    schema_version: Literal["trafficvisson.pipeline-profile.v1"]
    project_id: UUID
    question_policy: PolicyMode
    review_mode: Literal["precision", "quick"]
    default_template_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    default_template_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    detector_pack_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    ocr_pack_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    sam2_pack_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    report_provider: Literal["codex", "claude", "local", "none"]
    output_location_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
    )
    candidate_rescan_fps: int = Field(ge=5, le=10)
    gps_policy: Literal["timed_track_required_for_route"]
    bulk_frame_provider_policy: Literal["local_models_only"]
    revision: int = Field(ge=1)
    updated_at: datetime


class PipelineJobStartRequest(StrictModel):
    project_id: UUID
    snapshot_id: UUID
    file_name: str = Field(min_length=1, max_length=255)
    rights_confirmed: Literal[True]
    idempotency_key: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$",
    )

    @field_validator("rights_confirmed", mode="before")
    @classmethod
    def explicit_rights_confirmation(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("rights_confirmed must be explicitly true")
        return True

    @field_validator("file_name")
    @classmethod
    def plain_file_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or "\x00" in value:
            raise ValueError("file_name must not contain a path")
        return value


class PipelineReviewCompleteRequest(StrictModel):
    review_completion_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )


class PipelineStageResponse(StrictModel):
    name: PipelineStageName
    status: Literal[
        "pending",
        "running",
        "success",
        "blocked",
        "failed",
        "skipped",
        "cancelled",
    ]
    outcome: Optional[PipelineOutcomeCode]
    attempts: int = Field(ge=0, le=64)
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    error_code: Optional[str] = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{2,63}$"
    )
    checkpoint: Dict[str, Any]
    metrics: Dict[str, Any]


class PipelineJobResponse(StrictModel):
    job_id: UUID
    project_id: UUID
    snapshot_id: UUID
    file_name: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$"
    )
    input_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_snapshot: PipelineProfileResponse
    capability_snapshot: Dict[str, Any]
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "awaiting_review",
        "blocked",
        "failed",
        "cancelled",
        "completed",
    ]
    current_stage: PipelineStageName
    progress_percent: int = Field(ge=0, le=100)
    outcome_code: Optional[PipelineOutcomeCode]
    error_code: Optional[str] = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{2,63}$"
    )
    retry_count: int = Field(ge=0, le=5)
    resume_count: int = Field(ge=0, le=5)
    review_completion_id: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    stages: List[PipelineStageResponse] = Field(
        min_length=12, max_length=12
    )
    provenance: Dict[str, Any]
    created_at: datetime
    updated_at: datetime
    recovered_after_restart: bool
    record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_pipeline_state(self) -> "PipelineJobResponse":
        if [stage.name for stage in self.stages] != [
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
        ]:
            raise ValueError("pipeline stages are not in canonical DAG order")
        if self.status == "completed":
            if self.progress_percent != 100 or self.outcome_code not in {
                "success",
                "zero_detections",
            }:
                raise ValueError("completed pipeline state is inconsistent")
        if self.status == "blocked" and self.outcome_code not in {
            "model_unavailable",
            "partial",
        }:
            raise ValueError(
                "blocked pipeline must expose model_unavailable or partial"
            )
        if (
            self.status == "blocked"
            and self.outcome_code == "partial"
            and self.current_stage != "derived_outputs"
        ):
            raise ValueError(
                "partial pipeline must remain at derived_outputs"
            )
        if self.status == "failed" and self.outcome_code != "failed":
            raise ValueError("failed pipeline must expose failed outcome")
        if self.status == "cancelled" and self.outcome_code != "cancelled":
            raise ValueError("cancelled pipeline must expose cancelled outcome")
        if (
            self.status == "awaiting_review"
            and self.current_stage != "question_queue"
        ):
            raise ValueError("review wait must be at question_queue")
        return self


class PipelineJobStartResponse(StrictModel):
    job: PipelineJobResponse
    idempotent_replay: bool


class PipelineJobListResponse(StrictModel):
    jobs: List[PipelineJobResponse] = Field(max_length=128)


CandidateIssueType = VisionIssueType


class CandidateNormalizedBox(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def contained(self) -> "CandidateNormalizedBox":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("normalized box must remain inside the image")
        return self


class CandidateNormalizedPoint(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)

    @field_validator("x", "y", mode="before")
    @classmethod
    def strict_finite_coordinate(cls, value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("mask refinement coordinates must be finite")
        return float(value)


class MaskRefinementIdentityRequest(StrictModel):
    project_id: UUID
    analysis_job_id: UUID
    question_id: UUID
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")


class MaskRefinementRequest(MaskRefinementIdentityRequest):
    click: CandidateNormalizedPoint


class MaskRefinementModelManifest(StrictModel):
    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    pack_version: str = Field(
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$"
    )
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class MaskRefinementCapabilityResponse(StrictModel):
    schema_version: Literal[
        "trafficvisson.sam2-mask-refinement-capability.v1"
    ]
    project_id: UUID
    analysis_job_id: UUID
    question_id: UUID
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")
    state: Literal["ready", "model_unavailable"]
    error_code: Optional[Literal["model_pack_not_configured"]]
    message: str = Field(min_length=1, max_length=500)
    model_manifest: Optional[MaskRefinementModelManifest]
    click_prompt_enabled: bool

    @model_validator(mode="after")
    def consistent_state(self) -> "MaskRefinementCapabilityResponse":
        if self.state == "ready":
            if (
                self.error_code is not None
                or self.model_manifest is None
                or not self.click_prompt_enabled
            ):
                raise ValueError("ready mask refinement capability is invalid")
        elif (
            self.error_code != "model_pack_not_configured"
            or self.model_manifest is not None
            or self.click_prompt_enabled
        ):
            raise ValueError(
                "unavailable mask refinement capability is invalid"
            )
        return self


class MaskRefinementResponse(StrictModel):
    schema_version: Literal["trafficvisson.sam2-mask-refinement.v1"]
    project_id: UUID
    analysis_job_id: UUID
    question_id: UUID
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")
    status: Literal["refined", "model_unavailable"]
    error_code: Optional[Literal["model_pack_not_configured"]]
    message: str = Field(min_length=1, max_length=500)
    refinement_id: Optional[str] = Field(
        default=None,
        pattern=r"^smr_[a-f0-9]{24}$",
    )
    click: CandidateNormalizedPoint
    polygon: Optional[List[Tuple[float, float]]] = Field(
        default=None,
        min_length=3,
        max_length=256,
    )
    normalized_box: Optional[CandidateNormalizedBox]
    mask_artifact_ref: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    tracked_frame_refs: List[str] = Field(max_length=32)
    input_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    output_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    model_manifest: Optional[MaskRefinementModelManifest]
    requires_user_confirmation: Literal[True]
    automatic_annotation_allowed: Literal[False]
    automatic_incident_creation_allowed: Literal[False]
    training_membership_allowed: Literal[False]

    @field_validator("polygon", mode="before")
    @classmethod
    def strict_polygon(
        cls,
        value: object,
    ) -> object:
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("mask refinement polygon must be a list")
        for point in value:
            if (
                not isinstance(point, list)
                or len(point) != 2
                or any(
                    isinstance(coordinate, bool)
                    or not isinstance(coordinate, (int, float))
                    or not math.isfinite(float(coordinate))
                    or not 0 <= float(coordinate) <= 1
                    for coordinate in point
                )
            ):
                raise ValueError(
                    "mask refinement polygon coordinates are invalid"
                )
        return value

    @model_validator(mode="after")
    def consistent_state(self) -> "MaskRefinementResponse":
        refined_fields = (
            self.refinement_id,
            self.polygon,
            self.normalized_box,
            self.mask_artifact_ref,
            self.input_sha256,
            self.output_sha256,
            self.model_manifest,
        )
        if self.status == "refined":
            if (
                self.error_code is not None
                or any(value is None for value in refined_fields)
            ):
                raise ValueError("refined SAM2 response is incomplete")
        elif (
            self.error_code != "model_pack_not_configured"
            or any(value is not None for value in refined_fields)
            or self.tracked_frame_refs
        ):
            raise ValueError("unavailable SAM2 response is inconsistent")
        return self


class CandidateLabelAnswerRequest(StrictModel):
    project_id: UUID
    analysis_job_id: UUID
    answer: Literal["yes", "no", "unsure", "edit", "skipped"]
    selected_issue_type: Optional[CandidateIssueType] = None
    severity: Optional[
        Literal["minor", "moderate", "major", "urgent"]
    ] = None
    maintenance_action: Optional[str] = Field(default=None, max_length=500)
    ocr_text: Optional[str] = Field(default=None, max_length=500)
    normalized_box: Optional[CandidateNormalizedBox] = None
    question_id: Optional[UUID] = None
    mask_refinement_id: Optional[str] = Field(
        default=None,
        pattern=r"^smr_[a-f0-9]{24}$",
    )

    @model_validator(mode="after")
    def refinement_identity_is_bounded(
        self,
    ) -> "CandidateLabelAnswerRequest":
        if self.mask_refinement_id is not None:
            if self.answer != "edit" or self.question_id is None:
                raise ValueError(
                    "mask refinement identity requires an edited answer "
                    "and question_id"
                )
            if self.normalized_box is not None:
                raise ValueError(
                    "renderer geometry cannot accompany mask refinement"
                )
        elif self.question_id is not None:
            raise ValueError(
                "question_id is accepted only with a mask refinement"
            )
        return self


class CandidateLatestAnswer(StrictModel):
    answer: Literal["yes", "no", "unsure", "edit", "skipped"]
    selected_issue_type: Optional[CandidateIssueType]
    severity: Optional[Literal["minor", "moderate", "major", "urgent"]]
    maintenance_action: Optional[str]
    ocr_text: Optional[str]
    normalized_box: Optional[CandidateNormalizedBox]
    include_in_training: bool
    answered_by: str


class ExperimentalProposalModel(StrictModel):
    provider: Literal[
        "onnx_yolo_v8",
        "transformers_clip_zero_shot",
        "transformers_grounding_dino",
    ]
    pack_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    pack_version: str = Field(
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$"
    )
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExperimentalProposalDataset(StrictModel):
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    dataset_version: str = Field(
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$"
    )
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExperimentalProposalEvaluation(StrictModel):
    evaluation_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
    )
    producer: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    )
    producer_version: str = Field(
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$"
    )


class ExperimentalProposalSource(StrictModel):
    representative_image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    analysis_candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExperimentalProposalPrediction(StrictModel):
    issue_type: VisionIssueType
    relative_score: Optional[float] = Field(default=None, ge=0, le=1)
    model_score: Optional[float] = Field(default=None, ge=0, le=1)
    box: Optional[AnalysisNormalizedBox] = None
    source_label: str = Field(min_length=1, max_length=128)
    calibration: Literal["zero_shot_relative", "model_score"]

    @field_validator("relative_score", "model_score", mode="before")
    @classmethod
    def strict_finite_score(cls, value: object) -> Optional[float]:
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(
                "experimental proposal scores must be finite JSON numbers"
            )
        return float(value)

    @model_validator(mode="after")
    def validate_score(self) -> "ExperimentalProposalPrediction":
        if self.calibration == "zero_shot_relative":
            if self.relative_score is None or self.model_score is not None:
                raise ValueError(
                    "zero-shot predictions require only relative_score"
                )
        elif self.model_score is None or self.relative_score is not None:
            raise ValueError(
                "model-score predictions require only model_score"
            )
        return self

    @property
    def sortable_score(self) -> float:
        if self.calibration == "zero_shot_relative":
            assert self.relative_score is not None
            return self.relative_score
        assert self.model_score is not None
        return self.model_score


class CandidateExperimentalProposal(StrictModel):
    schema_version: Literal[
        "trafficvisson.experimental-candidate-proposal.v1"
    ]
    run_id: UUID
    project_id: UUID
    analysis_job_id: UUID
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")
    created_at: datetime
    status: Literal["classified", "no_suggestion"]
    review_only: Literal[True]
    automatic_labeling_allowed: Literal[False]
    score_semantics: Literal[
        "zero_shot_relative_not_probability",
        "model_score_not_probability",
    ]
    model: ExperimentalProposalModel
    dataset: Optional[ExperimentalProposalDataset] = None
    evaluation: ExperimentalProposalEvaluation
    source: ExperimentalProposalSource
    predictions: List[ExperimentalProposalPrediction] = Field(max_length=32)

    @model_validator(mode="after")
    def validate_status(self) -> "CandidateExperimentalProposal":
        if self.status == "classified" and not self.predictions:
            raise ValueError("classified proposals require a prediction")
        if self.status == "no_suggestion" and self.predictions:
            raise ValueError("no_suggestion proposals cannot contain predictions")
        expected_calibration = (
            "zero_shot_relative"
            if self.model.provider == "transformers_clip_zero_shot"
            else "model_score"
        )
        expected_semantics = (
            "zero_shot_relative_not_probability"
            if expected_calibration == "zero_shot_relative"
            else "model_score_not_probability"
        )
        if self.score_semantics != expected_semantics or any(
            prediction.calibration != expected_calibration
            for prediction in self.predictions
        ):
            raise ValueError(
                "proposal score semantics must match its model provider"
            )
        if expected_calibration == "zero_shot_relative" and any(
            prediction.box is not None for prediction in self.predictions
        ):
            raise ValueError("zero-shot proposals cannot claim a box")
        if expected_calibration == "model_score" and any(
            prediction.box is None for prediction in self.predictions
        ):
            raise ValueError("detector proposals require a box")
        if any(
            self.predictions[index].sortable_score
            < self.predictions[index + 1].sortable_score
            for index in range(len(self.predictions) - 1)
        ):
            raise ValueError(
                "experimental predictions must be sorted by relative score"
            )
        return self


class CandidateLabelQuestion(StrictModel):
    project_id: UUID
    analysis_job_id: UUID
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")
    question_id: UUID
    status: Literal["unanswered", "approved", "negative", "held", "excluded"]
    candidate: AnalysisCandidate
    latest_answer: Optional[CandidateLatestAnswer]
    answered_at: Optional[datetime]
    answer_revision: int = Field(ge=0)
    experimental_proposal: Optional[CandidateExperimentalProposal] = None


class CandidateLabelQuestionList(StrictModel):
    project_id: UUID
    analysis_job_id: UUID
    questions: List[CandidateLabelQuestion] = Field(max_length=512)
    count: int = Field(ge=0, le=512)
    answered_count: int = Field(ge=0, le=512)


class CandidateLabelAnswerResponse(StrictModel):
    project_id: UUID
    analysis_job_id: UUID
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")
    question_id: UUID
    record_id: UUID
    answer_revision: int = Field(ge=1)
    partition: Literal["approved", "negative", "held", "excluded"]
    include_in_training: bool
    answered_at: datetime
    record_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AutoLabelCompileRequest(StrictModel):
    project_id: UUID
    command: str = Field(min_length=3, max_length=500)
    issue_type: Optional[CandidateIssueType] = None

    @field_validator("command")
    @classmethod
    def normalize_command(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 3:
            raise ValueError("auto-label command is too short")
        if any(
            ord(character) < 32 and character not in {"\n", "\r", "\t"}
            for character in normalized
        ):
            raise ValueError("auto-label command contains control characters")
        return normalized


class AutoLabelCapabilities(StrictModel):
    object_detection: bool
    segmentation_required: bool
    ocr_required: bool
    relationship_reasoning_required: bool
    maintenance_judgment_required: bool


class AutoLabelCompileResponse(StrictModel):
    recipe_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: UUID
    command: str = Field(min_length=3, max_length=500)
    issue_type: CandidateIssueType
    display_name_ko: str = Field(min_length=1, max_length=80)
    positive_prompts: List[str] = Field(min_length=1, max_length=8)
    negative_prompts: List[str] = Field(min_length=2, max_length=8)
    capabilities: AutoLabelCapabilities
    default_mode: Literal["proposal_only"]
    warnings: List[str] = Field(max_length=12)


class AutoLabelRunRequest(AutoLabelCompileRequest):
    recipe_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    job_id: Optional[UUID] = None
    scope: Literal["current_job", "project"]
    mode: Literal["proposal_only", "assisted_auto"]
    user_confirmed: Literal[True]

    @model_validator(mode="after")
    def require_current_job(self) -> "AutoLabelRunRequest":
        if self.scope == "current_job" and self.job_id is None:
            raise ValueError("job_id is required for current_job scope")
        if self.issue_type is None:
            raise ValueError("issue_type is required for an auto-label run")
        return self


class AutoLabelMatchReference(StrictModel):
    analysis_job_id: UUID
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")
    issue_type: CandidateIssueType
    review_status: Literal["pending", "auto_accepted"]
    decision_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AutoLabelRunResponse(StrictModel):
    run_id: UUID
    recipe_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: UUID
    scope: Literal["current_job", "project"]
    mode_requested: Literal["proposal_only", "assisted_auto"]
    mode_applied: Literal["proposal_only", "assisted_auto"]
    status: Literal["completed", "no_candidates", "model_unavailable"]
    proposal_origin: Literal[
        "local_model_generated", "cached_existing", "none"
    ]
    proposal_generation_status: Literal[
        "generated", "cached", "unavailable"
    ]
    jobs_scanned: int = Field(ge=0, le=100)
    candidates_evaluated: int = Field(ge=0, le=10_000)
    proposed_count: int = Field(ge=0, le=10_000)
    auto_accepted_count: int = Field(ge=0, le=10_000)
    review_required_count: int = Field(ge=0, le=10_000)
    training_labels_created: Literal[0]
    first_match: Optional[AutoLabelMatchReference] = None
    message_ko: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_counts(self) -> "AutoLabelRunResponse":
        if self.auto_accepted_count + self.review_required_count != self.proposed_count:
            raise ValueError("auto-label decision counts do not add up")
        if self.proposed_count > self.candidates_evaluated:
            raise ValueError("auto-label proposals exceed evaluated candidates")
        if (self.proposed_count == 0) != (self.first_match is None):
            raise ValueError("auto-label first match does not match the counts")
        expected_generation_status = {
            "local_model_generated": "generated",
            "cached_existing": "cached",
            "none": "unavailable",
        }[self.proposal_origin]
        if self.proposal_generation_status != expected_generation_status:
            raise ValueError(
                "auto-label proposal generation status does not match its origin"
            )
        if (
            self.proposal_origin == "none"
            and self.status != "model_unavailable"
        ):
            raise ValueError(
                "an auto-label run without proposals must report model unavailable"
            )
        return self


ValidationIssueType = Annotated[
    Literal[
        "pothole",
        "road_surface_damage",
        "road_debris",
        "median_barrier_damage",
        "sign_damage",
        "sign_vegetation_occlusion",
        "roadside_mowing_required",
        "guide_sign_identity",
        "tunnel_identity",
        "tunnel_name_occlusion",
        "lane_marking_faded",
        "colored_guidance_faded",
    ],
    BeforeValidator(_canonical_contract_issue_type),
]

_REVIEW_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"


class TemporalReviewDetection(StrictModel):
    detection_id: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    timestamp_seconds: float = Field(ge=0, le=2_678_400)
    issue_type: CandidateIssueType
    score: float = Field(ge=0, le=1)
    review_only: Literal[True]
    automatic_labeling_allowed: Literal[False]
    normalized_box: Optional[CandidateNormalizedBox] = None
    track_hint: Optional[str] = Field(
        default=None,
        pattern=_REVIEW_IDENTIFIER_PATTERN,
    )
    frame_index: Optional[int] = Field(
        default=None,
        ge=0,
        le=9_007_199_254_740_991,
    )
    score_semantics: Optional[
        Literal[
            "model_score_not_probability",
            "zero_shot_relative_not_probability",
        ]
    ] = None

    @field_validator("timestamp_seconds", "score", mode="before")
    @classmethod
    def strict_finite_number(cls, value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("temporal detection numbers must be finite")
        return float(value)

    @field_validator("frame_index", mode="before")
    @classmethod
    def strict_frame_index(cls, value: object) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("frame_index must be an integer")
        return value


class TemporalGroupingPolicyRequest(StrictModel):
    max_gap_seconds: float = Field(default=2.0, ge=0.001, le=60)
    min_iou: float = Field(default=0.10, ge=0, le=1)
    max_center_distance: float = Field(
        default=0.15,
        ge=0,
        le=math.sqrt(2.0),
    )
    strict_track_separation: bool = True
    allow_temporal_only_without_spatial_hints: bool = False

    @field_validator(
        "max_gap_seconds",
        "min_iou",
        "max_center_distance",
        mode="before",
    )
    @classmethod
    def strict_finite_number(cls, value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("temporal grouping policy numbers must be finite")
        return float(value)

    @field_validator(
        "strict_track_separation",
        "allow_temporal_only_without_spatial_hints",
        mode="before",
    )
    @classmethod
    def strict_boolean(cls, value: object) -> bool:
        if type(value) is not bool:
            raise ValueError("temporal grouping policy flags must be boolean")
        return value


class TemporalIncidentReviewRequest(StrictModel):
    project_id: UUID
    detections: List[TemporalReviewDetection] = Field(max_length=100_000)
    provenance: Dict[str, Any] = Field(min_length=1)
    policy: TemporalGroupingPolicyRequest = Field(
        default_factory=TemporalGroupingPolicyRequest
    )

    @field_validator("provenance")
    @classmethod
    def require_project_provenance(
        cls,
        value: Dict[str, Any],
    ) -> Dict[str, Any]:
        raw_project_id = value.get("project_id")
        try:
            UUID(str(raw_project_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError(
                "temporal provenance requires a project_id UUID"
            ) from error
        return value


class TemporalPeakEvidence(StrictModel):
    detection_id: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    timestamp_seconds: float = Field(ge=0, le=2_678_400)
    score: float = Field(ge=0, le=1)
    normalized_box: Optional[CandidateNormalizedBox]
    track_hint: Optional[str] = Field(
        default=None,
        pattern=_REVIEW_IDENTIFIER_PATTERN,
    )
    frame_index: Optional[int] = Field(
        default=None,
        ge=0,
        le=9_007_199_254_740_991,
    )
    score_semantics: Optional[
        Literal[
            "model_score_not_probability",
            "zero_shot_relative_not_probability",
        ]
    ]


TemporalMergeReason = Literal[
    "same_track_hint",
    "spatial_iou",
    "spatial_center_distance",
    "bounded_gap_without_comparable_spatial_hint",
]


class TemporalMergeLink(StrictModel):
    from_detection_id: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    to_detection_id: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    gap_seconds: float = Field(ge=0, le=60)
    reason: TemporalMergeReason
    iou: Optional[float] = Field(default=None, ge=0, le=1)
    center_distance: Optional[float] = Field(
        default=None,
        ge=0,
        le=math.sqrt(2.0),
    )


class TemporalProposalProvenance(StrictModel):
    algorithm: Literal["temporal-spatial-review-grouping.v1"]
    source: Dict[str, Any]
    source_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_fingerprint_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    grouping_policy: TemporalGroupingPolicyRequest


class TemporalIncidentReviewProposal(StrictModel):
    schema_version: Literal["trafficvisson.temporal-incident-proposal.v1"]
    proposal_id: str = Field(pattern=r"^tip_[a-f0-9]{24}$")
    proposal_status: Literal["awaiting_user_review"]
    issue_type: CandidateIssueType
    start_seconds: float = Field(ge=0, le=2_678_400)
    end_seconds: float = Field(ge=0, le=2_678_400)
    duration_seconds: float = Field(ge=0, le=2_678_400)
    detection_count: int = Field(ge=1, le=100_000)
    evidence_detection_ids: List[str] = Field(
        min_length=1,
        max_length=100_000,
    )
    peak_evidence: TemporalPeakEvidence
    merge_reasons: List[
        Union[TemporalMergeReason, Literal["single_detection"]]
    ] = Field(min_length=1, max_length=4)
    merge_links: List[TemporalMergeLink] = Field(max_length=99_999)
    review_only: Literal[True]
    automatic_incident_creation_allowed: Literal[False]
    automatic_annotation_allowed: Literal[False]
    automatic_maintenance_action_allowed: Literal[False]
    automatic_training_allowed: Literal[False]
    provenance: TemporalProposalProvenance

    @model_validator(mode="after")
    def validate_temporal_bounds(self) -> "TemporalIncidentReviewProposal":
        if self.start_seconds > self.end_seconds:
            raise ValueError("temporal proposal timestamps are out of order")
        if self.detection_count != len(self.evidence_detection_ids):
            raise ValueError(
                "temporal proposal detection count does not match evidence"
            )
        if len(self.merge_links) != self.detection_count - 1:
            raise ValueError(
                "temporal proposal merge links do not match evidence"
            )
        return self


class TemporalIncidentReviewResponse(StrictModel):
    schema_version: Literal["trafficvisson.temporal-review-api.v1"]
    project_id: UUID
    computation_mode: Literal["pure_review_only"]
    proposal_count: int = Field(ge=0, le=100_000)
    proposals: List[TemporalIncidentReviewProposal] = Field(
        max_length=100_000
    )
    state_changes_performed: Literal[False]
    incidents_created: Literal[0]
    annotations_created: Literal[0]
    training_members_created: Literal[0]
    model_versions_changed: Literal[0]

    @model_validator(mode="after")
    def validate_proposal_count(self) -> "TemporalIncidentReviewResponse":
        if self.proposal_count != len(self.proposals):
            raise ValueError(
                "temporal proposal count does not match proposals"
            )
        return self


class ValidationTruthInput(StrictModel):
    sample_id: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    issue_type: ValidationIssueType
    truth_event_ids: List[str] = Field(
        default_factory=list,
        max_length=256,
    )


class ValidationPredictionInput(StrictModel):
    prediction_event_id: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    sample_id: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    issue_type: ValidationIssueType
    matched_truth_event_id: Optional[str] = Field(
        default=None,
        pattern=_REVIEW_IDENTIFIER_PATTERN,
    )


class ModelValidationReviewRequest(StrictModel):
    project_id: UUID
    model_version: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    truth_records: List[ValidationTruthInput] = Field(
        max_length=1_000_000
    )
    prediction_records: List[ValidationPredictionInput] = Field(
        max_length=2_000_000
    )


class ModelValidationReviewResponse(StrictModel):
    schema_version: Literal["trafficvisson.model-validation-review-api.v1"]
    project_id: UUID
    computation_mode: Literal["pure_review_only"]
    report: ModelValidationReport
    state_changes_performed: Literal[False]
    annotations_created: Literal[0]
    training_members_created: Literal[0]
    model_versions_changed: Literal[0]


class ModelPromotionReviewRequest(StrictModel):
    project_id: UUID
    active_model_version: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    candidate_model_version: str = Field(pattern=_REVIEW_IDENTIFIER_PATTERN)
    truth_records: List[ValidationTruthInput] = Field(
        max_length=1_000_000
    )
    active_prediction_records: List[ValidationPredictionInput] = Field(
        max_length=2_000_000
    )
    candidate_prediction_records: List[ValidationPredictionInput] = Field(
        max_length=2_000_000
    )
    required_issue_types: Optional[List[ValidationIssueType]] = Field(
        default=None,
        min_length=1,
        max_length=12,
    )
    minimum_samples_per_type: int = Field(default=30, ge=1)
    maximum_recall_drop: float = Field(default=0.02, gt=0, le=1)

    @field_validator("minimum_samples_per_type", mode="before")
    @classmethod
    def strict_minimum_samples(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("minimum_samples_per_type must be an integer")
        return value

    @field_validator("maximum_recall_drop", mode="before")
    @classmethod
    def strict_recall_drop(cls, value: object) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("maximum_recall_drop must be finite")
        return float(value)


class ModelPromotionReviewResponse(StrictModel):
    schema_version: Literal["trafficvisson.model-promotion-review-api.v1"]
    project_id: UUID
    computation_mode: Literal["pure_review_only"]
    decision: PromotionReviewDecision
    state_changes_performed: Literal[False]
    annotations_created: Literal[0]
    training_members_created: Literal[0]
    model_versions_changed: Literal[0]
    automatic_promotion_performed: Literal[False]


class LabelPartitionCounts(StrictModel):
    approved: int = Field(ge=0)
    negative: int = Field(ge=0)
    held: int = Field(ge=0)
    excluded: int = Field(ge=0)


class ProjectLabelStatsResponse(StrictModel):
    project_id: UUID
    question_count: int = Field(ge=0)
    answer_revision_count: int = Field(ge=0)
    current_answer_count: int = Field(ge=0)
    training_included_count: int = Field(ge=0)
    source_analysis_job_count: int = Field(ge=0)
    partitions: LabelPartitionCounts


class SourceCandidateEvaluation(StrictModel):
    evaluated_frame_count: int = Field(ge=1, le=100_000)
    proposal_frame_count: int = Field(ge=1, le=100_000)
    proposal_count: int = Field(ge=1, le=100_000)
    false_positive_count: Optional[int] = Field(
        default=None,
        ge=0,
        le=100_000,
    )
    verdict: Literal["review_only", "rejected"]

    @model_validator(mode="after")
    def validate_counts(self) -> "SourceCandidateEvaluation":
        if (
            self.proposal_frame_count > self.evaluated_frame_count
            or self.proposal_count < self.proposal_frame_count
        ):
            raise ValueError("source evaluation counts are inconsistent")
        if self.verdict == "review_only":
            if self.false_positive_count is not None:
                raise ValueError(
                    "review-only evaluation cannot assert false positives"
                )
        elif self.false_positive_count != self.proposal_count:
            raise ValueError(
                "rejected evaluation must account for every proposal"
            )
        return self


class SourceCandidateResponse(StrictModel):
    source_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    source_type: Literal["model_pack", "labeled_dataset"]
    publisher: str = Field(min_length=1, max_length=256)
    origin_url: AnyHttpUrl
    license_spdx_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.+-]*$",
    )
    intended_use: Literal[
        "candidate_proposals",
        "mask_refinement",
        "training_seed",
        "evaluation_reference",
        "retrieval_reference",
    ]
    capability: Literal[
        "natural_language_box_proposals",
        "prompted_mask_refinement",
        "semantic_candidate_scoring",
        "pothole_box_proposals",
        "labeled_training_reference",
    ]
    catalog_status: Literal[
        "review_candidate",
        "evaluation_complete",
        "unverified_local",
        "rejected",
    ]
    installation_status: Literal[
        "not_installed",
        "installed_validation_only",
        "local_cache_quarantined",
    ]
    evaluation: Optional[SourceCandidateEvaluation]
    observation_basis: Literal[
        "verified_local_snapshot",
        "unverified_local_files",
        "not_observed",
        "catalog_policy_only",
    ]
    rights_review_required: Literal[True]
    automatic_download: Literal[False]
    automatic_activation: Literal[False]
    weight_execution_enabled: Literal[False]
    project_training_linked: Literal[False]
    human_review_required: Literal[True]
    decision_reason: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def validate_inactive_state(self) -> "SourceCandidateResponse":
        if self.observation_basis == "verified_local_snapshot":
            if (
                self.catalog_status == "evaluation_complete"
                and self.installation_status == "installed_validation_only"
                and self.evaluation is not None
                and self.evaluation.verdict == "review_only"
            ):
                return self
            if (
                self.catalog_status == "rejected"
                and self.installation_status == "local_cache_quarantined"
                and self.evaluation is not None
                and self.evaluation.verdict == "rejected"
            ):
                return self
            raise ValueError("verified local source state is inconsistent")
        if self.observation_basis == "unverified_local_files":
            if (
                self.catalog_status
                not in {"unverified_local", "rejected"}
                or self.installation_status != "local_cache_quarantined"
                or self.evaluation is not None
            ):
                raise ValueError(
                    "unverified local source must remain quarantined"
                )
            return self
        if (
            self.installation_status != "not_installed"
            or self.evaluation is not None
            or self.catalog_status
            not in {"review_candidate", "rejected"}
        ):
            raise ValueError("unobserved source must remain uninstalled")
        return self


class SourceCandidateCatalogResponse(StrictModel):
    schema_version: Literal["trafficvisson.source-candidate-catalog.v1"]
    catalog_mode: Literal["read_only_inactive"]
    network_access_performed: Literal[False]
    project_training_linked: Literal[False]
    automatic_labels_created: Literal[False]
    candidates: List[SourceCandidateResponse] = Field(max_length=32)
    count: int = Field(ge=0, le=32)

    @model_validator(mode="after")
    def validate_catalog(self) -> "SourceCandidateCatalogResponse":
        if self.count != len(self.candidates):
            raise ValueError("source candidate count does not match candidates")
        source_ids = [candidate.source_id for candidate in self.candidates]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source candidate identifiers must be unique")
        return self


WeakCaseSourceStatus = Literal[
    "ready",
    "source_missing",
    "source_sha256_mismatch",
    "source_changed",
    "source_invalid",
    "source_unavailable",
    "source_path_rejected",
    "source_contract_mismatch",
]


class WeakCaseSourceResponse(StrictModel):
    source_id: str = Field(
        pattern=r"^sample-hwpx-[34]q-[a-f0-9]{16}$"
    )
    document_name: str = Field(min_length=1, max_length=120)
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authorization_purpose: Literal[
        "sample_hwpx_read_only_weak_case_index"
    ]
    quarter: Literal[3, 4]
    status: WeakCaseSourceStatus
    case_count: int = Field(ge=0, le=10_000)
    resolved_case_count: int = Field(ge=0, le=10_000)
    review_required_count: int = Field(ge=0, le=10_000)
    approved_reference_count: int = Field(ge=0, le=10_000)
    training_eligible_count: int = Field(ge=0, le=10_000)
    annotation_count: Literal[0]
    incident_count: Literal[0]
    index_manifest_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )

    @model_validator(mode="after")
    def validate_state(self) -> "WeakCaseSourceResponse":
        if self.status == "ready":
            if (
                self.index_manifest_sha256 is None
                or self.case_count
                != (
                    self.review_required_count
                    + self.approved_reference_count
                )
                or self.resolved_case_count > self.case_count
                or self.training_eligible_count
                != self.approved_reference_count
            ):
                raise ValueError("ready weak-case source counts are invalid")
        elif (
            self.index_manifest_sha256 is not None
            or self.case_count != 0
            or self.resolved_case_count != 0
            or self.review_required_count != 0
            or self.approved_reference_count != 0
            or self.training_eligible_count != 0
        ):
            raise ValueError("unavailable weak-case source exposes index data")
        return self


class WeakCaseSourceListResponse(StrictModel):
    schema_version: Literal["trafficvisson.weak-case-catalog.v1"]
    catalog_role: Literal[
        "sample_hwpx_weak_unconfirmed_reference",
        "sample_hwpx_project_scoped_reference",
    ]
    capability_state: Literal[
        "approved_reference_available",
        "weak_reference_only",
        "unavailable",
    ]
    automatic_promotion: Literal[False]
    automatic_annotation_creation: Literal[False]
    automatic_incident_creation: Literal[False]
    automatic_training_ingest: Literal[False]
    sources: List[WeakCaseSourceResponse] = Field(max_length=4)
    ready_source_count: int = Field(ge=0, le=4)
    source_count: int = Field(ge=0, le=4)

    @model_validator(mode="after")
    def validate_counts(self) -> "WeakCaseSourceListResponse":
        if (
            self.source_count != len(self.sources)
            or self.ready_source_count
            != sum(source.status == "ready" for source in self.sources)
        ):
            raise ValueError("weak-case source counts are inconsistent")
        if (
            self.capability_state
            in {"weak_reference_only", "approved_reference_available"}
            and self.ready_source_count < 1
        ) or (
            self.capability_state == "unavailable"
            and self.ready_source_count != 0
        ):
            raise ValueError("weak-case capability state is inconsistent")
        return self


class WeakCaseSearchRequest(StrictModel):
    project_id: UUID
    issue_type: Optional[CandidateIssueType] = None
    query_text: Optional[str] = Field(default=None, max_length=240)
    limit: int = Field(default=5, ge=3, le=5)

    @model_validator(mode="after")
    def validate_query(self) -> "WeakCaseSearchRequest":
        if self.issue_type is None and not self.query_text:
            raise ValueError(
                "weak-case search requires issue_type or query_text"
            )
        return self


class WeakCaseSearchResult(StrictModel):
    source_id: str = Field(
        pattern=r"^sample-hwpx-[34]q-[a-f0-9]{16}$"
    )
    case_id: str = Field(
        pattern=r"^hwpx-weak-case-[a-f0-9]{32}$"
    )
    rank: int = Field(ge=1, le=5)
    similarity_score: int = Field(gt=0, le=10_000)
    matched_terms: List[str] = Field(max_length=8)
    reference_state: Literal[
        "weak_unconfirmed",
        "approved_reference",
    ]
    review_required: bool
    approved_reference: bool
    training_eligible: bool
    annotation_created: Literal[False]
    incident_created: Literal[False]
    quarter: Literal[3, 4]
    document_name: str = Field(min_length=1, max_length=120)
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    structure_target_summary_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    index_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    section_path: str = Field(
        pattern=r"^Contents/section[0-9]{1,5}\.xml$"
    )
    section_index: int = Field(ge=0, le=100_000)
    picture_target_id: str = Field(
        pattern=r"^hwpx-picture-[a-f0-9]{24}$"
    )
    page_hint: int = Field(ge=1, le=10_000)
    page_hint_source: str = Field(min_length=1, max_length=120)
    cell_target_id: Optional[str] = Field(
        default=None,
        pattern=r"^hwpx-cell-[a-f0-9]{24}$",
    )
    table_index: Optional[int] = Field(default=None, ge=0, le=100_000)
    cell_row: Optional[int] = Field(default=None, ge=0, le=100_000)
    cell_column: Optional[int] = Field(default=None, ge=0, le=100_000)
    picture_index: int = Field(ge=0, le=100_000)
    caption: Optional[str] = Field(default=None, max_length=320)
    route: Optional[str] = Field(default=None, max_length=320)
    location: Optional[str] = Field(default=None, max_length=320)
    evaluation: Optional[str] = Field(default=None, max_length=320)
    opinion: Optional[str] = Field(default=None, max_length=320)
    image_available: bool
    image_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    image_media_type: Optional[
        Literal[
            "image/bmp",
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        ]
    ] = None
    image_width: Optional[int] = Field(default=None, ge=1, le=65_535)
    image_height: Optional[int] = Field(default=None, ge=1, le=65_535)
    approval_id: Optional[str] = Field(
        default=None,
        pattern=r"^approved-reference-[a-f0-9]{24}$",
    )
    approval_revision: Optional[int] = Field(default=None, ge=1)
    approval_record_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    approved_issue_type: Optional[CandidateIssueType] = None
    approved_action: Optional[str] = Field(
        default=None, min_length=1, max_length=500
    )

    @model_validator(mode="after")
    def validate_image(self) -> "WeakCaseSearchResult":
        image_fields = (
            self.image_media_type,
            self.image_width,
            self.image_height,
        )
        if self.image_available and any(value is None for value in image_fields):
            raise ValueError("available weak-case image metadata is incomplete")
        approval_fields = (
            self.approval_id,
            self.approval_revision,
            self.approval_record_sha256,
            self.approved_issue_type,
            self.approved_action,
        )
        if self.approved_reference:
            if (
                self.reference_state != "approved_reference"
                or self.review_required
                or not self.training_eligible
                or any(value is None for value in approval_fields)
            ):
                raise ValueError(
                    "approved reference state is incomplete"
                )
        elif (
            self.reference_state != "weak_unconfirmed"
            or not self.review_required
            or self.training_eligible
            or any(value is not None for value in approval_fields)
        ):
            raise ValueError("weak reference state is inconsistent")
        return self


class WeakCaseSearchResponse(StrictModel):
    schema_version: Literal["trafficvisson.weak-case-search.v1"]
    search_mode: Literal["bounded_text_and_structure_metadata"]
    semantic_image_embedding_used: Literal[False]
    capability_state: Literal[
        "approved_reference_available",
        "weak_reference_only",
        "unavailable",
    ]
    issue_type: Optional[CandidateIssueType]
    query_text: Optional[str] = Field(default=None, max_length=240)
    requested_limit: int = Field(ge=3, le=5)
    result_count: int = Field(ge=0, le=5)
    cases: List[WeakCaseSearchResult] = Field(max_length=5)
    source_states: List[WeakCaseSourceResponse] = Field(max_length=4)
    review_required_before_promotion: Literal[True]
    approved_reference_count: int = Field(ge=0, le=5)
    annotation_count: Literal[0]
    incident_count: Literal[0]
    training_eligible_count: int = Field(ge=0, le=5)

    @model_validator(mode="after")
    def validate_results(self) -> "WeakCaseSearchResponse":
        if (
            self.result_count != len(self.cases)
            or self.result_count > self.requested_limit
        ):
            raise ValueError("weak-case search result counts are inconsistent")
        if [case.rank for case in self.cases] != list(
            range(1, len(self.cases) + 1)
        ):
            raise ValueError("weak-case search ranks are invalid")
        ready_count = sum(
            source.status == "ready" for source in self.source_states
        )
        if (
            self.capability_state
            in {"weak_reference_only", "approved_reference_available"}
            and ready_count < 1
        ) or (
            self.capability_state == "unavailable" and ready_count != 0
        ):
            raise ValueError("weak-case search capability is inconsistent")
        if (
            self.approved_reference_count
            != sum(case.approved_reference for case in self.cases)
            or self.training_eligible_count
            != sum(case.training_eligible for case in self.cases)
        ):
            raise ValueError(
                "approved weak-case search counts are inconsistent"
            )
        return self


class WeakCaseApprovalRequest(StrictModel):
    project_id: UUID
    source_id: str = Field(
        pattern=r"^sample-hwpx-[34]q-[a-f0-9]{16}$"
    )
    case_id: str = Field(
        pattern=r"^hwpx-weak-case-[a-f0-9]{32}$"
    )
    expected_document_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    expected_index_manifest_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    expected_image_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    issue_type: CandidateIssueType
    maintenance_action: str = Field(min_length=1, max_length=500)
    caption: Optional[str] = Field(default=None, max_length=320)
    route: Optional[str] = Field(default=None, max_length=320)
    location: Optional[str] = Field(default=None, max_length=320)
    evaluation: Optional[str] = Field(default=None, max_length=320)
    opinion: Optional[str] = Field(default=None, max_length=320)
    expected_revision: Optional[int] = Field(default=None, ge=1)
    user_confirmed: Literal[True]


class ApprovedReferenceSquareMarker(StrictModel):
    cell_target_id: str = Field(
        pattern=r"^hwpx-cell-[a-f0-9]{24}$"
    )
    table_id: str = Field(min_length=1, max_length=120)
    table_index: int = Field(ge=0, le=100_000)
    page_number: int = Field(ge=1, le=10_000)
    row: int = Field(ge=0, le=100_000)
    column: int = Field(ge=0, le=100_000)
    marker: Literal["■"]
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ApprovedReferenceSemanticCell(StrictModel):
    role: Literal[
        "photo_caption",
        "inspection_route",
        "inspection_opinion_label",
        "inspection_opinion_body",
    ]
    cell_target_id: str = Field(
        pattern=r"^hwpx-cell-[a-f0-9]{24}$"
    )
    table_id: str = Field(min_length=1, max_length=120)
    table_index: int = Field(ge=0, le=100_000)
    page_number: int = Field(ge=1, le=48)
    row: int = Field(ge=0, le=100_000)
    column: int = Field(ge=0, le=100_000)
    row_span: int = Field(ge=1, le=100_000)
    column_span: int = Field(ge=1, le=100_000)
    text_excerpt: str = Field(max_length=320)
    text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ApprovedReferenceRecord(StrictModel):
    schema_version: Literal["trafficvisson.approved-reference.v1"]
    approval_id: str = Field(
        pattern=r"^approved-reference-[a-f0-9]{24}$"
    )
    project_id: UUID
    source_id: str = Field(
        pattern=r"^sample-hwpx-[34]q-[a-f0-9]{16}$"
    )
    case_id: str = Field(
        pattern=r"^hwpx-weak-case-[a-f0-9]{32}$"
    )
    revision: int = Field(ge=1)
    reference_state: Literal["approved_reference"]
    approved_reference: Literal[True]
    training_eligible: Literal[True]
    training_scope: Literal["approved_reference_only"]
    annotation_created: Literal[False]
    incident_created: Literal[False]
    document_name: str = Field(min_length=1, max_length=120)
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    structure_target_summary_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    index_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    image_media_type: Literal[
        "image/bmp",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    ]
    image_width: int = Field(ge=1, le=65_535)
    image_height: int = Field(ge=1, le=65_535)
    quarter: Literal[3, 4]
    section_path: str = Field(
        pattern=r"^Contents/section[0-9]{1,5}\.xml$"
    )
    section_index: int = Field(ge=0, le=100_000)
    picture_target_id: str = Field(
        pattern=r"^hwpx-picture-[a-f0-9]{24}$"
    )
    picture_index: int = Field(ge=0, le=100_000)
    picture_xml_id: Optional[str] = Field(default=None, max_length=256)
    table_id: Optional[str] = Field(default=None, max_length=120)
    table_index: Optional[int] = Field(default=None, ge=0, le=100_000)
    cell_target_id: Optional[str] = Field(
        default=None,
        pattern=r"^hwpx-cell-[a-f0-9]{24}$",
    )
    cell_row: Optional[int] = Field(default=None, ge=0, le=100_000)
    cell_column: Optional[int] = Field(default=None, ge=0, le=100_000)
    page_hint: int = Field(ge=1, le=10_000)
    page_hint_source: str = Field(min_length=1, max_length=120)
    photo_slot_id: str = Field(
        pattern=r"^sample-photo-slot-[a-f0-9]{24}$"
    )
    photo_page_number: Literal[
        2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24,
        26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48,
    ]
    inspection_page_number: Literal[
        1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23,
        25, 27, 29, 31, 33, 35, 37, 39, 41, 43, 45, 47,
    ]
    sample_layout_binding_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    square_markers: List[ApprovedReferenceSquareMarker] = Field(
        min_length=1, max_length=64
    )
    semantic_cells: List[ApprovedReferenceSemanticCell] = Field(
        min_length=4, max_length=4
    )
    weak_candidates: Dict[str, Optional[str]] = Field(
        min_length=5, max_length=5
    )
    issue_type: CandidateIssueType
    maintenance_action: str = Field(min_length=1, max_length=500)
    caption: Optional[str] = Field(default=None, max_length=320)
    route: Optional[str] = Field(default=None, max_length=320)
    location: Optional[str] = Field(default=None, max_length=320)
    evaluation: Optional[str] = Field(default=None, max_length=320)
    opinion: Optional[str] = Field(default=None, max_length=320)
    semantic_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_at: datetime
    updated_at: datetime
    user_confirmed: Literal[True]
    record_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_semantic_cells(
        self,
    ) -> "ApprovedReferenceRecord":
        roles = [cell.role for cell in self.semantic_cells]
        if len(roles) != len(set(roles)) or set(roles) != {
            "photo_caption",
            "inspection_route",
            "inspection_opinion_label",
            "inspection_opinion_body",
        }:
            raise ValueError(
                "approved reference semantic cell roles are invalid"
            )
        return self


class WeakCaseApprovalResponse(ApprovedReferenceRecord):
    idempotent_replay: bool


class ApprovedReferenceListResponse(StrictModel):
    schema_version: Literal[
        "trafficvisson.approved-reference-list.v1"
    ]
    project_id: UUID
    approved_reference_count: int = Field(ge=0, le=10_000)
    training_eligible_count: int = Field(ge=0, le=10_000)
    annotation_count: Literal[0]
    incident_count: Literal[0]
    records: List[ApprovedReferenceRecord] = Field(max_length=10_000)

    @model_validator(mode="after")
    def validate_counts(self) -> "ApprovedReferenceListResponse":
        if (
            self.approved_reference_count != len(self.records)
            or self.training_eligible_count != len(self.records)
        ):
            raise ValueError(
                "approved reference list counts are inconsistent"
            )
        return self


class ProjectIncidentResponse(StrictModel):
    incident_id: UUID
    project_id: UUID
    question_id: UUID
    analysis_job_id: UUID
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")
    status: Literal["active", "superseded"]
    issue_type: CandidateIssueType
    severity: Optional[
        Literal["minor", "moderate", "major", "urgent"]
    ] = None
    maintenance_action: Optional[str] = Field(default=None, max_length=500)
    ocr_text: Optional[str] = Field(default=None, max_length=500)
    answer_revision: int = Field(ge=1)
    source_answer_record_id: UUID
    source_record_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_image_ref: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^analysis:[a-f0-9-]{36}:[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    preview_clip_ref: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^analysis:[a-f0-9-]{36}:[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    start_time_ms: int = Field(ge=0)
    representative_time_ms: int = Field(ge=0)
    end_time_ms: int = Field(ge=0)
    created_at: datetime
    activated_at: datetime
    updated_at: datetime
    superseded_at: Optional[datetime] = None
    superseded_by_answer_revision: Optional[int] = Field(default=None, ge=1)
    superseded_by_answer_record_id: Optional[UUID] = None
    superseded_by_record_hash: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )

    @model_validator(mode="after")
    def validate_incident_state(self) -> "ProjectIncidentResponse":
        if not (
            self.start_time_ms
            <= self.representative_time_ms
            <= self.end_time_ms
        ):
            raise ValueError("incident timestamps are out of order")
        supersession_fields = (
            self.superseded_at,
            self.superseded_by_answer_revision,
            self.superseded_by_answer_record_id,
            self.superseded_by_record_hash,
        )
        if self.status == "superseded":
            if any(value is None for value in supersession_fields):
                raise ValueError(
                    "superseded incident is missing audit provenance"
                )
        elif any(value is not None for value in supersession_fields):
            raise ValueError("active incident contains supersession data")
        return self


class ProjectIncidentListResponse(StrictModel):
    project_id: UUID
    incidents: List[ProjectIncidentResponse] = Field(max_length=10_000)
    count: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_count(self) -> "ProjectIncidentListResponse":
        if self.count != len(self.incidents):
            raise ValueError("incident count does not match incidents")
        return self


class DatasetVersionCreateRequest(StrictModel):
    user_confirmed: Literal[True]


class DatasetMembershipResponse(StrictModel):
    annotation_id: UUID
    question_id: UUID
    analysis_job_id: UUID
    candidate_id: str = Field(pattern=r"^[a-f0-9]{20}$")
    answer_record_id: UUID
    answer_revision: int = Field(ge=1)
    partition: Literal["approved", "negative", "held", "excluded"]
    include_in_training: bool
    split: Literal["train", "validation", "excluded"]
    source_record_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    annotation_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class DatasetVersionResponse(StrictModel):
    dataset_version_id: UUID
    project_id: UUID
    schema_version: Literal["trafficvisson.dataset-version.v1"]
    dataset_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    split_strategy: Literal["analysis_job_hash_v1"]
    validation_fraction: float = Field(gt=0, lt=1)
    source_answer_count: int = Field(ge=1)
    training_member_count: int = Field(ge=0)
    train_count: int = Field(ge=0)
    validation_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    manifest: Dict[str, Any]
    created_at: datetime
    reused: bool
    memberships: List[DatasetMembershipResponse]


class DatasetVersionListResponse(StrictModel):
    project_id: UUID
    versions: List[DatasetVersionResponse]
    count: int = Field(ge=0)


class Geometry(StrictModel):
    kind: Literal["box", "polygon", "mask_ref"]
    points: List[Tuple[float, float]] = Field(max_length=256)

    @field_validator("points")
    @classmethod
    def validate_points(
        cls, points: List[Tuple[float, float]]
    ) -> List[Tuple[float, float]]:
        for x, y in points:
            if not (-100_000 <= x <= 100_000 and -100_000 <= y <= 100_000):
                raise ValueError("geometry coordinates are outside the supported range")
        return points


class EvidenceRefs(StrictModel):
    representative_image: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    before_frame: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    after_frame: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    preview_clip: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class QuestionResponse(StrictModel):
    id: UUID
    incident_id: UUID
    trip_id: UUID
    prompt: str
    proposed_class: str
    proposed_severity: Literal["minor", "moderate", "major", "urgent"]
    confidence: float = Field(ge=0, le=1)
    status: QuestionStatus
    emergency_review_required: bool
    proposed_geometry: Geometry
    evidence: EvidenceRefs
    latitude: float
    longitude: float
    time_ms: int = Field(ge=0)
    learning_effect: str


class QuestionListResponse(StrictModel):
    policy_mode: PolicyMode
    questions: List[QuestionResponse]
    count: int = Field(ge=0)


class QuestionAnswerRequest(StrictModel):
    project_id: UUID
    question_id: UUID
    answer: Literal["yes", "no", "unsure", "edit"]
    selected_class: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    selected_severity: Optional[
        Literal["minor", "moderate", "major", "urgent"]
    ] = None
    selected_action: Optional[str] = Field(default=None, max_length=500)
    final_geometry: Optional[Geometry] = None
    ocr_text: Optional[str] = Field(default=None, max_length=500)
    include_in_training: Optional[bool] = None

    @model_validator(mode="after")
    def require_edit_geometry(self) -> "QuestionAnswerRequest":
        if self.answer == "edit" and self.final_geometry is None:
            raise ValueError("final_geometry is required when answer is edit")
        return self


class QuestionAnswerResponse(StrictModel):
    question_id: UUID
    status: QuestionStatus
    annotation_state: Literal["approved", "negative", "held"]
    include_in_training: bool
    final_geometry: Optional[Geometry]
    answered_at: datetime


class RoutePoint(StrictModel):
    sequence: int = Field(ge=0)
    time_ms: int = Field(ge=0)
    latitude: float
    longitude: float
    speed_kph: float = Field(ge=0)


class HistoryIncident(StrictModel):
    id: UUID
    question_id: UUID
    time_ms: int = Field(ge=0)
    latitude: float
    longitude: float
    incident_type: str
    severity: Literal["minor", "moderate", "major", "urgent"]
    maintenance_action: str
    ocr_text: Optional[str]
    evidence: EvidenceRefs


class HistoryResponse(StrictModel):
    project_id: UUID
    trip_id: UUID
    route_points: List[RoutePoint]
    incidents: List[HistoryIncident]
    playback_duration_ms: int = Field(ge=0)


class GpxTrackImportRequest(StrictModel):
    selected_path: str = Field(min_length=3, max_length=4096)
    video_offset_seconds: float = Field(default=0.0, ge=-86_400, le=86_400)

    @field_validator("selected_path")
    @classmethod
    def validate_selected_path(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("selected_path contains a null character")
        return value


class GpxTrackOffsetRequest(StrictModel):
    video_offset_seconds: float = Field(ge=-86_400, le=86_400)


class GpxRoutePoint(StrictModel):
    sequence: int = Field(ge=0)
    source_sequence: int = Field(ge=0)
    recorded_at: datetime
    elapsed_seconds: float = Field(ge=0)
    video_time_seconds: float
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_meters: Optional[float]
    speed_kph: Optional[float] = Field(default=None, ge=0, le=300)
    distance_from_previous_meters: float = Field(ge=0)
    gap_from_previous: bool


class GpxTrackResponse(StrictModel):
    schema_version: Literal["trafficvisson.gpx-track.v1"]
    track_id: UUID
    project_id: UUID
    trip_id: UUID
    source_name: str = Field(min_length=1, max_length=255)
    source_size_bytes: int = Field(gt=0)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    point_count: int = Field(ge=2, le=50_000)
    displayed_point_count: int = Field(ge=2, le=8_000)
    sampled_for_display: bool
    started_at: datetime
    ended_at: datetime
    duration_seconds: float = Field(gt=0)
    total_distance_meters: float = Field(ge=0)
    video_offset_seconds: float = Field(ge=-86_400, le=86_400)
    revision: int = Field(ge=1)
    replaces_track_id: Optional[UUID]
    imported_at: datetime
    updated_at: datetime
    route_points: List[GpxRoutePoint] = Field(min_length=2, max_length=8_000)


class ProviderCapabilities(StrictModel):
    structured_judgment: bool
    bulk_frame_detection: Literal[False]
    report_draft: bool
    document_edit_plan: bool
    usage_information: bool


class ProviderResponse(StrictModel):
    provider: ProviderName
    display_name: str
    state: Literal["not_connected", "ready", "unavailable", "coming_soon"]
    authentication_owner: Literal["official_cli", "not_applicable"]
    capabilities: ProviderCapabilities
    message: str
    diagnostic_state: Literal[
        "not_installed",
        "installed",
        "not_connected",
        "ready",
        "expired",
        "network_unavailable",
        "error",
        "coming_soon",
    ]
    installation_state: Literal["installed", "not_installed", "disabled"]
    authentication_state: Literal[
        "ready",
        "signed_out",
        "expired",
        "unknown",
        "not_applicable",
    ]
    network_state: Literal["not_checked", "offline", "unknown", "not_applicable"]
    version: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[vV]?[0-9][0-9A-Za-z.+-]{0,63}$",
    )


class ProviderListResponse(StrictModel):
    providers: List[ProviderResponse]


class ProviderLoginResponse(StrictModel):
    provider: Literal["codex", "claude"]
    state: Literal["external_cli_owned"]
    message: str


class RuntimeModelPackIdentity(StrictModel):
    provenance_verified: Literal[True]
    provider: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    pack_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    pack_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$",
    )
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    model_artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_repo_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$",
    )
    source_revision: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    weights_spdx: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$"
    )
    runtime_spdx: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$"
    )
    commercial_use_confirmed: Literal[True]
    redistribution_allowed: bool
    gpu_required: bool
    cpu_fallback: bool
    recommended_min_vram_mb: int = Field(ge=0, le=131_072)
    cuda_min_version: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9]{1,3}(?:\.[0-9]{1,3}){1,2}$",
    )
    review_only: Literal[True]


class RuntimeComponentResponse(StrictModel):
    component_id: Literal[
        "electron_host",
        "python_bridge",
        "pytorch_cuda",
        "grounding_dino",
        "sam2",
        "opencv",
        "ffmpeg",
        "korean_ocr",
        "embedding_search",
        "codex_cli",
        "claude_code",
        "hancom_com",
        "local_model_provider",
    ]
    display_name: str = Field(min_length=1, max_length=80)
    state: Literal[
        "ready",
        "incomplete",
        "unavailable",
        "failed",
        "external_required",
        "disabled",
    ]
    management: Literal[
        "installer",
        "installer_or_model_pack",
        "model_pack",
        "official_cli",
        "external_installation",
        "provider_slot",
    ]
    required_for: List[
        str
    ] = Field(min_length=1, max_length=8)
    message: str = Field(min_length=1, max_length=500)
    version: Optional[str] = Field(default=None, min_length=1, max_length=64)
    error_code: Optional[str] = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{2,63}$"
    )
    model_pack: Optional[RuntimeModelPackIdentity] = None


class RuntimeReadinessCounts(StrictModel):
    disabled: int = Field(ge=0, le=13)
    external_required: int = Field(ge=0, le=13)
    failed: int = Field(ge=0, le=13)
    incomplete: int = Field(ge=0, le=13)
    ready: int = Field(ge=0, le=13)
    unavailable: int = Field(ge=0, le=13)


class RuntimeProviderPolicy(StrictModel):
    bulk_frame_detection: Literal["local_models_only"]
    structured_judgment_providers: List[
        Literal["codex", "claude"]
    ] = Field(min_length=2, max_length=2)
    provider_authentication_owner: Literal["official_cli"]
    authentication_files_read_by_app: Literal[False]
    local_model_provider: Literal["disabled"]


class RuntimePipelineOutcomeContract(StrictModel):
    model_unavailable: Literal["unavailable"]
    model_runtime_failed: Literal["failed"]
    successful_zero_predictions: Literal["zero_detections"]


class RuntimeReadinessResponse(StrictModel):
    schema_version: Literal["trafficvisson.runtime-readiness.v1"]
    provider_policy: RuntimeProviderPolicy
    pipeline_outcome_contract: RuntimePipelineOutcomeContract
    counts: RuntimeReadinessCounts
    components: List[RuntimeComponentResponse] = Field(
        min_length=13, max_length=13
    )


class ReportDraftRequest(StrictModel):
    project_id: UUID
    provider: ProviderName
    incident_ids: List[UUID] = Field(default_factory=list, max_length=500)
    consent: bool


class DraftSection(StrictModel):
    heading: str
    body: str
    incident_ids: List[UUID]


class ReportDraftResponse(StrictModel):
    draft_id: UUID
    provider: ProviderName
    state: Literal["fixture_draft"]
    title: str
    sections: List[DraftSection]
    transmitted_fields: List[str]


class ApprovedReportIncident(StrictModel):
    incident_id: UUID
    incident_type: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    severity: Literal[
        "minor", "moderate", "major", "urgent", "unassessed"
    ]
    maintenance_action: Optional[str] = Field(default=None, max_length=1_000)
    ocr_text: Optional[str] = Field(default=None, max_length=1_000)
    location_summary: Optional[str] = Field(default=None, max_length=500)
    time_ms: Optional[int] = Field(default=None, ge=0, le=2_678_400_000)
    evidence_refs: List[str] = Field(min_length=1, max_length=8)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, refs: List[str]) -> List[str]:
        pattern = r"^(?:deid|evidence|analysis):[A-Za-z0-9][A-Za-z0-9._:-]{0,117}$"
        if len(refs) != len(set(refs)):
            raise ValueError("evidence_refs must be unique")
        for ref in refs:
            if not re.fullmatch(pattern, ref):
                raise ValueError(
                    "evidence_refs must be opaque deidentified references"
                )
        return refs


class ReportDocumentStructureSummary(StrictModel):
    document_snapshot_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    field_targets: List[str] = Field(default_factory=list, max_length=100)
    cell_targets: List[str] = Field(default_factory=list, max_length=100)
    table_targets: List[str] = Field(default_factory=list, max_length=100)
    image_targets: List[str] = Field(default_factory=list, max_length=100)
    map_targets: List[str] = Field(default_factory=list, max_length=100)
    section_headings: List[str] = Field(default_factory=list, max_length=100)

    @field_validator(
        "field_targets",
        "cell_targets",
        "table_targets",
        "image_targets",
        "map_targets",
    )
    @classmethod
    def validate_targets(cls, targets: List[str]) -> List[str]:
        if len(targets) != len(set(targets)):
            raise ValueError("document targets must be unique")
        for target in targets:
            if not re.fullmatch(
                r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", target
            ):
                raise ValueError("document targets must be opaque identifiers")
        return targets

    @field_validator("section_headings")
    @classmethod
    def validate_headings(cls, headings: List[str]) -> List[str]:
        if len(headings) != len(set(headings)):
            raise ValueError("section headings must be unique")
        if any(not heading or len(heading) > 200 for heading in headings):
            raise ValueError("section headings are outside the safe range")
        return headings


class ReportGenerateRequest(StrictModel):
    project_id: UUID
    analysis_job_id: UUID
    source_file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pipeline_job_id: Optional[UUID] = None
    pipeline_input_fingerprint: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    pipeline_profile_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    provider: Literal["codex", "claude"]
    consent: bool
    project_label: Optional[str] = Field(default=None, max_length=200)
    incident_ids: List[UUID] = Field(min_length=1, max_length=200)
    document_structure_summary: ReportDocumentStructureSummary
    map_refs: List[str] = Field(default_factory=list, max_length=20)

    @field_validator("incident_ids")
    @classmethod
    def unique_incidents(
        cls, incident_ids: List[UUID]
    ) -> List[UUID]:
        if len(incident_ids) != len(set(incident_ids)):
            raise ValueError("incident IDs must be unique")
        return incident_ids

    @model_validator(mode="after")
    def complete_pipeline_binding(self) -> "ReportGenerateRequest":
        binding = (
            self.pipeline_job_id,
            self.pipeline_input_fingerprint,
            self.pipeline_profile_sha256,
        )
        if any(value is not None for value in binding) and any(
            value is None for value in binding
        ):
            raise ValueError(
                "pipeline provenance must be supplied as one complete binding"
            )
        return self

    @field_validator("map_refs")
    @classmethod
    def validate_map_refs(cls, refs: List[str]) -> List[str]:
        if len(refs) != len(set(refs)):
            raise ValueError("map_refs must be unique")
        for ref in refs:
            if not re.fullmatch(
                r"^map:[A-Za-z0-9][A-Za-z0-9._:-]{0,122}$", ref
            ):
                raise ValueError("map_refs must be opaque map references")
        return refs


class ReportPlanRequest(StrictModel):
    project_id: UUID
    draft_id: UUID
    document_snapshot_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )


class DocumentEditOperation(StrictModel):
    operation_id: UUID
    type: Literal[
        "fill_text",
        "add_table_row",
        "fill_cell",
        "insert_image",
        "insert_map",
    ]
    target: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9가-힣][A-Za-z0-9가-힣._:-]{0,127}$",
    )
    expected_value: Optional[str] = Field(default=None, max_length=8_000)
    value: str = Field(min_length=1, max_length=20_000)
    source_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    style_policy: Literal["preserve", "inherit_target"]
    preview: str = Field(min_length=1, max_length=1_000)


class DocumentEditPlan(StrictModel):
    schema_version: Literal["1.0"]
    plan_id: UUID
    document_snapshot_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    operations: List[DocumentEditOperation] = Field(min_length=1, max_length=100)


class ReportPlanResponse(StrictModel):
    plan: DocumentEditPlan
    state: Literal["preview_required"]


class GeneratedReportDraft(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    sections: List[DraftSection] = Field(min_length=1, max_length=30)


class ReportUsage(StrictModel):
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)


class ReportGenerateResponse(StrictModel):
    generation_id: UUID
    provider: Literal["codex", "claude"]
    state: Literal["generated_preview_required"]
    draft: GeneratedReportDraft
    plan: DocumentEditPlan
    model: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    )
    usage: Optional[ReportUsage] = None
    transmitted_fields: List[
        Literal[
            "project_label",
            "approved_incidents",
            "document_structure_summary",
            "map_refs",
        ]
    ]


class ReportApplyRequest(StrictModel):
    project_id: UUID
    plan: DocumentEditPlan


class ReportApplyResponse(StrictModel):
    report_id: UUID
    state: Literal["preview_validation_only"]
    validated_operation_ids: List[UUID]
    message: str


class ReportDecisionRequest(StrictModel):
    project_id: UUID
    report_id: UUID
    operation_ids: List[UUID] = Field(default_factory=list, max_length=100)


class ReportDecisionResponse(StrictModel):
    report_id: UUID
    state: Literal["accepted", "rejected"]
    operation_ids: List[UUID]


class HwpPreflightResponse(StrictModel):
    available: bool
    prog_id_registered: bool
    security_module_registered: bool
    message: str = Field(min_length=1, max_length=500)


class HwpStructureSummary(StrictModel):
    observed_characters: int = Field(ge=0, le=2_000_000)
    truncated: bool
    paragraph_count: int = Field(ge=0)
    table_count: int = Field(ge=0)
    picture_count: int = Field(ge=0)
    field_names: List[str] = Field(max_length=100)
    bookmark_names: List[str] = Field(max_length=100)


class HwpDocumentRequest(StrictModel):
    project_id: UUID
    template_path: str = Field(min_length=3, max_length=4_096)
    plan: DocumentEditPlan


class HwpApplyDocumentRequest(HwpDocumentRequest):
    approval_token: str = Field(pattern=r"^[a-f0-9]{64}$")


class HwpApprovalTargetSummary(StrictModel):
    operation_count: int = Field(ge=1, le=100)
    operation_types: Dict[str, int]
    targets: List[str] = Field(min_length=1, max_length=100)


class HwpPreviewResponse(StrictModel):
    mode: Literal["preview"]
    operation_count: int = Field(ge=1, le=500)
    operation_types: Dict[str, int]
    structure: HwpStructureSummary
    message: str = Field(min_length=1, max_length=500)
    approval_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_expires_at: datetime
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_summary: HwpApprovalTargetSummary


class HwpSampleSemanticOperationBinding(StrictModel):
    operation_id: UUID
    approval_id: str = Field(
        pattern=r"^approved-reference-[a-f0-9]{24}$"
    )
    operation_type: Literal["fill_cell", "insert_image"]
    target_id: str = Field(
        pattern=r"^hwpx-(?:cell|picture)-[a-f0-9]{24}$"
    )
    semantic_role: Literal[
        "photo_slot_image",
        "photo_caption",
        "inspection_route",
        "inspection_opinion_body",
        "evaluation_square_marker",
    ]
    page_number: int = Field(ge=1, le=48)
    table_index: int = Field(ge=0, le=71)
    row: int = Field(ge=0)
    column: int = Field(ge=0)


class HwpSampleSemanticPreviewResponse(StrictModel):
    """Exact-sample preview proof; never a claim that apply is available."""

    schema_version: Literal[
        "trafficvisson.hwpx-sample-semantic-plan.v1"
    ]
    mode: Literal["sample_semantic_preview"]
    state: Literal["semantic_preview_validated_apply_unavailable"]
    document_snapshot_id: str = Field(
        pattern=r"^hwpx-sample-[34]q-[a-f0-9]{24}$"
    )
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    structure_target_summary_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    weak_case_index_manifest_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    validation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_references_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    approved_reference_count: int = Field(ge=1, le=100)
    operation_count: int = Field(ge=1, le=100)
    page_pair_indices: List[int] = Field(min_length=1, max_length=24)
    touched_pages: List[int] = Field(min_length=1, max_length=48)
    expected_rendered_page_count: Literal[48]
    expected_page_range: Tuple[int, int]
    page_range_preserved_by_structure: Literal[True]
    table_growth_allowed: Literal[False]
    semantic_validation_passed: Literal[True]
    com_invocation_performed: Literal[False]
    operation_bindings: List[
        HwpSampleSemanticOperationBinding
    ] = Field(min_length=1, max_length=100)
    approval_scope: Literal["sample_preview_hash_binding_only"]
    approval_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    approval_expires_at: datetime
    apply_eligible: Literal[False]
    writer_state: Literal["unavailable"]
    output_render_validation_status: Literal["not_run"]
    blocking_reasons: List[str] = Field(min_length=2, max_length=2)
    no_com: Literal[True]
    no_document_write: Literal[True]

    @model_validator(mode="after")
    def validate_unavailable_state(
        self,
    ) -> "HwpSampleSemanticPreviewResponse":
        if self.expected_page_range != (1, 48):
            raise ValueError("sample semantic preview must preserve pages 1-48")
        if self.page_pair_indices != sorted(set(self.page_pair_indices)):
            raise ValueError("sample page-pair indices must be sorted and unique")
        if self.touched_pages != sorted(set(self.touched_pages)):
            raise ValueError("sample touched pages must be sorted and unique")
        expected_blockers = [
            "exact_hwpml_target_writer_not_implemented",
            "output_reopen_and_48_page_render_validation_not_implemented",
        ]
        if self.blocking_reasons != expected_blockers:
            raise ValueError(
                "sample semantic preview blockers must be explicit and complete"
            )
        return self


class HwpSampleLayoutCounts(StrictModel):
    rendered_page_count_baseline: Literal[48]
    page_pair_count: Literal[24]
    section_count: Literal[1]
    table_count: Literal[72]
    cell_count: int = Field(ge=1)
    picture_count: int = Field(ge=1)
    fixed_inspection_picture_count: Literal[24]
    existing_photo_picture_count: int = Field(ge=1)
    explicit_page_break_count: int = Field(ge=1)


class HwpSampleLayoutOperationBinding(StrictModel):
    operation_id: UUID
    operation_type: Literal[
        "fill_cell", "insert_image", "insert_map"
    ]
    target_id: str = Field(
        pattern=r"^hwpx-(?:cell|picture)-[a-f0-9]{24}$"
    )
    target_kind: Literal["cell", "picture"]
    table_index: int = Field(ge=0, le=71)
    page_number: int = Field(ge=1, le=48)
    page_kind: Literal["inspection", "photo"]
    layout_role: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]{2,79}$",
    )
    cell_row: Optional[int] = Field(default=None, ge=0)
    cell_column: Optional[int] = Field(default=None, ge=0)
    source_ref: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )


class HwpSampleLayoutPreviewResponse(StrictModel):
    schema_version: Literal[
        "trafficvisson.hwpx-sample-layout-preview.v1"
    ]
    state: Literal["validated_structure_preview_apply_blocked"]
    template_id: Literal["sample-3q-v1", "sample-4q-v1"]
    quarter: Literal[3, 4]
    document_snapshot_id: str = Field(
        pattern=r"^hwpx-sample-[34]q-[a-f0-9]{24}$"
    )
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    structure_target_summary_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    baseline_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    weak_case_index_manifest_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$"
    )
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    layout_contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_rendered_page_count: Literal[48]
    output_rendered_page_count: Optional[int] = Field(
        default=None, ge=1
    )
    output_render_validation_status: Literal["not_run"]
    page_pattern: Literal["odd_inspection_even_photo"]
    page_mapping: Literal["audited_three_table_pair_contract"]
    counts: HwpSampleLayoutCounts
    operation_bindings: List[
        HwpSampleLayoutOperationBinding
    ] = Field(min_length=1, max_length=100)
    apply_eligible: Literal[False]
    blocking_reasons: List[str] = Field(min_length=3, max_length=3)
    source_renamed: bool
    read_only: Literal[True]
    no_com: Literal[True]
    no_document_write: Literal[True]

    @field_validator("blocking_reasons")
    @classmethod
    def exact_blocking_reasons(cls, values: List[str]) -> List[str]:
        expected = [
            "exact_hwpml_target_writer_not_implemented",
            "semantic_cell_role_map_not_approved",
            "output_reopen_and_48_page_render_validation_not_implemented",
        ]
        if values != expected:
            raise ValueError(
                "sample HWPX apply blockers must be explicit and complete"
            )
        return values


class HwpApplyResponse(StrictModel):
    mode: Literal["applied"]
    rollback_token: str = Field(pattern=r"^[a-f0-9]{32}$")
    output_file_name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    readback: HwpStructureSummary
    message: str = Field(min_length=1, max_length=500)


class HwpRollbackRequest(StrictModel):
    project_id: UUID
    rollback_token: str = Field(pattern=r"^[a-f0-9]{32}$")


class HwpRollbackResponse(StrictModel):
    rolled_back: Literal[True]
    output_file_name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    message: str = Field(min_length=1, max_length=500)


class ExportCreateRequest(StrictModel):
    project_id: UUID
    analysis_job_id: Optional[UUID] = None
    artifacts: List[ArtifactName] = Field(min_length=1, max_length=9)
    destination_selection_id: Optional[UUID] = None
    destination_path: Optional[str] = Field(
        default=None, min_length=3, max_length=4_096
    )

    @field_validator("artifacts")
    @classmethod
    def unique_artifacts(cls, artifacts: List[ArtifactName]) -> List[ArtifactName]:
        if len(set(artifacts)) != len(artifacts):
            raise ValueError("artifacts must be unique")
        return artifacts


class ExportArtifact(StrictModel):
    artifact: ArtifactName
    file_name: str
    row_count: Optional[int] = Field(default=None, ge=0)
    size_bytes: Optional[int] = Field(default=None, gt=0)
    sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )


class ExportCreateResponse(StrictModel):
    export_id: UUID
    status: Literal["fixture_ready", "completed"]
    artifacts: List[ExportArtifact]
    manifest_included: bool
    archive_file_name: Optional[str] = Field(default=None, max_length=255)
    archive_size_bytes: Optional[int] = Field(default=None, ge=0)
    archive_sha256: Optional[str] = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
    artifact_count: Optional[int] = Field(default=None, ge=0, le=1_034)
    evidence_incident_count: Optional[int] = Field(
        default=None, ge=0, le=512
    )
    evidence_file_count: Optional[int] = Field(
        default=None, ge=0, le=1_024
    )
    evidence_size_bytes: Optional[int] = Field(
        default=None, ge=0, le=8 * 1024**3
    )

    @model_validator(mode="after")
    def validate_export_completion(self) -> "ExportCreateResponse":
        completed_fields = (
            self.archive_file_name,
            self.archive_size_bytes,
            self.archive_sha256,
            self.artifact_count,
            self.evidence_incident_count,
            self.evidence_file_count,
            self.evidence_size_bytes,
        )
        if self.status == "completed" and any(
            value is None for value in completed_fields
        ):
            raise ValueError("completed exports require verified archive fields")
        if self.status == "completed" and any(
            artifact.row_count is None
            or artifact.size_bytes is None
            or artifact.sha256 is None
            for artifact in self.artifacts
        ):
            raise ValueError(
                "completed exports require verified artifact metadata"
            )
        if self.status == "fixture_ready" and any(
            value is not None for value in completed_fields
        ):
            raise ValueError("fixture exports cannot expose completion fields")
        if self.status == "completed":
            if self.evidence_file_count != self.evidence_incident_count * 2:
                raise ValueError(
                    "completed export evidence counts do not match"
                )
            if self.artifact_count != (
                len(self.artifacts) + 1 + self.evidence_file_count
            ):
                raise ValueError(
                    "completed export artifact count does not match"
                )
        return self


class ErrorResponse(StrictModel):
    detail: str
