"""Dense 2 FPS candidate discovery for the project-model feedback loop.

This module consumes the immutable frame index produced by
``DenseFrameIndexEngine``.  It deliberately sits before the 5--10 FPS
candidate-window rescan:

* every indexed 2 FPS frame is offered to the configured local proposal
  providers in bounded batches;
* a promoted project model may classify later-video frames, but its output is
  still review-only;
* approved 3Q/4Q visual references may contribute weak similarity proposals;
* local detector/OCR output remains distinct from project-model and reference
  proposals;
* temporally adjacent frame proposals become *candidate windows*, never
  Annotation/Incident/training/report truth.

The resulting manifest contains hashes and opaque evidence identities only.
Absolute paths never cross the bridge boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import local
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID

from .project_model_lifecycle import (
    LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    ProjectModelLifecycleError,
    ResolvedActiveProjectModel,
    VISUAL_CENTROID_MODEL_FORMAT,
)
from .local_object_detector_inference import (
    PromotedLocalObjectDetectorInference,
)
from .vision_inference import (
    LocalVisionInferenceService,
    VisionInferenceCancelled,
)
from .visual_centroid_trainer import (
    VisualEmbeddingCentroidHeadInference,
)
from .weak_case_visual_index import (
    CandidateFrameEvidence,
    WeakCaseVisualIndexError,
    WeakCaseVisualIndexService,
)


DENSE_CANDIDATE_DISCOVERY_SCHEMA = (
    "trafficvisson.dense-candidate-discovery.v1"
)
PROMOTED_MODEL_JOB_BINDING_SCHEMA = (
    "trafficvisson.promoted-model-job-binding.v1"
)
_DENSE_INDEX_SCHEMA = "trafficvisson.dense-frame-index.v1"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_FRAME_ID = re.compile(r"^[a-f0-9]{24}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_ISSUE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FRAME_FILE = re.compile(r"^frame-[0-9]{8}\.jpg$")
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_FORBIDDEN_METADATA_KEY = re.compile(
    r"(?:^|_)(?:path|token|secret|password|credential|auth)(?:_|$)",
    re.IGNORECASE,
)
_MAX_INDEX_BYTES = 64 * 1024 * 1024
_MAX_IMAGE_BYTES = 24 * 1024 * 1024
_READ_CHUNK_BYTES = 4 * 1024 * 1024
_MAX_METADATA_BYTES = 64 * 1024
_FORWARD_ROAD_ROI_ISSUE_TYPES = frozenset(
    {
        "pothole",
        "road_surface_damage",
        "lane_marking_faded",
        "colored_guidance_faded",
    }
)
_KOREAN_SIGN_IDENTITY_MARKERS = (
    "터널",
    "나들목",
    "분기점",
    "휴게소",
    "요금소",
    "방향",
    "출구",
)
_KOREAN_SIGN_WARNING_TERMS = (
    "안개",
    "위험",
    "주의",
    "공사",
    "속도",
    "화물차",
    "졸음",
    "쉬어",
    "전방",
    "안전",
    "여기는",
)
_KOREAN_MAJOR_DESTINATIONS = frozenset(
    {
        "서울",
        "부산",
        "대구",
        "인천",
        "광주",
        "대전",
        "울산",
        "세종",
        "수원",
        "천안",
        "논산",
        "공주",
        "청주",
        "충주",
        "제천",
        "전주",
        "익산",
        "군산",
        "목포",
        "여수",
        "순천",
        "포항",
        "경주",
        "구미",
        "김천",
        "창원",
        "진주",
        "원주",
        "춘천",
        "강릉",
        "속초",
        "제주",
    }
)
_DENSE_FRAME_KEYS = frozenset(
    {
        "frame_id",
        "frame_index",
        "timestamp_seconds",
        "timestamp_numerator",
        "timestamp_denominator",
        "relative_path",
        "width",
        "height",
        "size_bytes",
        "sha256",
        "media_type",
        "evidence_status",
    }
)


def _looks_like_korean_vehicle_plate(value: Any) -> bool:
    """Reject OCR strings that are registration plates, not guide signs."""

    if not isinstance(value, str) or not value.strip():
        return False
    normalized = unicodedata.normalize("NFKC", value)
    compact = re.sub(r"[\s·•._-]+", "", normalized)
    return (
        re.fullmatch(
            r"(?:[가-힣]{2,4})?[0-9]{2,3}[가-힣][0-9]{4}",
            compact,
        )
        is not None
    )


def _box_intersects_forward_road_roi(
    value: Any,
    *,
    horizontal_margin: float = 0.0,
) -> bool:
    """Return whether a detector box has support inside the forward road.

    The local pothole research pack occasionally assigns high model scores to
    vegetation, guardrails and signs at the extreme image edge.  Scores from
    that pack are not calibrated probabilities, so thresholding them more
    aggressively would only hide the problem.  This deterministic trapezoid
    instead asks whether the *centre* of a road-surface proposal lies on the
    part of a normal forward-facing dashcam image where carriageway can occur.

    This is only a proposal-quality gate.  It does not turn a retained
    detection into truth, and non-roadside issue types are never filtered by
    this geometry.
    """

    box = _safe_box(value)
    if box is None:
        return False
    center_x = box["x"] + (box["width"] / 2.0)
    center_y = box["y"] + (box["height"] / 2.0)
    horizon = 0.38
    if center_y < horizon:
        return False
    progress = min(1.0, max(0.0, (center_y - horizon) / (1.0 - horizon)))
    left = 0.34 - (0.30 * progress) - horizontal_margin
    right = 0.66 + (0.20 * progress) + horizontal_margin
    return max(0.0, left) <= center_x <= min(1.0, right)


def _ocr_text_can_identify_korean_road_sign(
    value: Any,
    *,
    confidence: Any = None,
) -> bool:
    if (
        not isinstance(value, str)
        or _looks_like_korean_vehicle_plate(value)
    ):
        return False
    normalized = unicodedata.normalize("NFKC", value).strip()
    hangul = re.findall(r"[가-힣]", normalized)
    compact = re.sub(r"\s+", "", normalized)
    # A single glyph, route number, utility-pole number, or license plate is
    # not enough evidence to identify a Korean guide sign or tunnel. Warning
    # and promotional text remains OCR evidence, but it must not create a
    # location-identity question by itself.
    if len(hangul) < 2 or any(
        term in compact for term in _KOREAN_SIGN_WARNING_TERMS
    ):
        return False
    score = (
        float(confidence)
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(float(confidence))
        )
        else 0.0
    )
    if any(marker in compact for marker in _KOREAN_SIGN_IDENTITY_MARKERS):
        return score >= 0.25
    if re.search(r"(?:^|[^A-Z])(?:IC|JC|JCT|TG)(?:$|[^A-Z])", normalized.upper()):
        return score >= 0.25
    if compact in _KOREAN_MAJOR_DESTINATIONS:
        return score >= 0.70
    # Multi-token place names such as "진영 자유마을" are useful even when
    # the OCR engine has not captured an explicit 방향/IC suffix.  Requiring
    # two substantial Hangul tokens rejects fragments such as "및 건설기" and
    # spaced warning glyphs while retaining paired destinations.
    substantial_tokens = [
        token
        for token in re.findall(r"[가-힣]+", normalized)
        if len(token) >= 2
    ]
    if len(substantial_tokens) >= 2:
        return score >= 0.45
    # Unqualified single-token OCR is too ambiguous to create a location
    # identity question.  It remains in the raw OCR evidence and can still be
    # considered when another detector or a verified sign candidate exists.
    return False


class DenseCandidateDiscoveryError(RuntimeError):
    """Base failure for dense-frame proposal discovery."""


class DenseCandidateDiscoveryCancelled(DenseCandidateDiscoveryError):
    """Raised when the owning PipelineJob is cancelled."""


class DenseCandidateDiscoveryIntegrityError(
    DenseCandidateDiscoveryError
):
    """Raised when immutable dense-frame evidence changed."""


@dataclass(frozen=True)
class DenseDiscoveryLimits:
    batch_size: int = 128
    max_frames: int = 30_000
    max_proposals_per_frame: int = 16
    max_candidates: int = 512
    temporal_gap_seconds: float = 1.5
    minimum_score: float = 0.50

    def __post_init__(self) -> None:
        integer_bounds = (
            ("batch_size", self.batch_size, 1, 512),
            ("max_frames", self.max_frames, 1, 100_000),
            (
                "max_proposals_per_frame",
                self.max_proposals_per_frame,
                1,
                64,
            ),
            ("max_candidates", self.max_candidates, 1, 2_000),
        )
        for name, value, minimum, maximum in integer_bounds:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        numeric_bounds = (
            (
                "temporal_gap_seconds",
                self.temporal_gap_seconds,
                0.0,
                30.0,
            ),
            ("minimum_score", self.minimum_score, 0.0, 1.0),
        )
        for name, value, minimum, maximum in numeric_bounds:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")


@dataclass(frozen=True)
class DenseFrameEvidence:
    frame_id: str
    frame_index: int
    timestamp_seconds: float
    file_name: str
    size_bytes: int
    sha256: str
    media_type: str
    path: Path

    @property
    def candidate_id(self) -> str:
        return hashlib.sha256(
            (
                f"{DENSE_CANDIDATE_DISCOVERY_SCHEMA}\0"
                f"{self.frame_id}"
            ).encode("ascii")
        ).hexdigest()[:20]


@dataclass(frozen=True)
class DenseFrameProposal:
    frame_id: str
    issue_type: str
    score: float
    proposal_source: str
    normalized_box: Optional[Mapping[str, float]] = None
    ocr_text: Optional[str] = None
    urgent_review: bool = False
    provenance: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class DenseCandidateSeed:
    candidate_id: str
    issue_type: str
    start_seconds: float
    end_seconds: float
    representative_timestamp_seconds: float
    representative_frame_id: str
    representative_frame_sha256: str
    score: float
    normalized_box: Optional[Mapping[str, float]]
    ocr_text: Optional[str]
    urgent_review: bool
    supporting_frame_ids: tuple[str, ...]
    proposal_sources: tuple[str, ...]
    representative_proposal_provenance: Optional[Mapping[str, Any]]
    proposal_provenance_sha256: str


@dataclass(frozen=True)
class DenseCandidateDiscoveryResult:
    schema_version: str
    project_id: str
    source_sha256: str
    dense_index_sha256: str
    sampling_fps: int
    scanned_frame_count: int
    candidates: tuple[DenseCandidateSeed, ...]
    provider_states: tuple[Mapping[str, Any], ...]
    discovery_revision: str
    review_only: bool = True
    automatic_annotation_creation: bool = False
    automatic_incident_creation: bool = False
    automatic_training_ingest: bool = False
    automatic_report_truth: bool = False
    annotations_created: int = 0
    incidents_created: int = 0
    training_members_created: int = 0
    report_truth_records_created: int = 0

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "source_sha256": self.source_sha256,
            "dense_index_sha256": self.dense_index_sha256,
            "sampling_fps": self.sampling_fps,
            "scanned_frame_count": self.scanned_frame_count,
            "candidate_count": len(self.candidates),
            "candidates": [
                asdict(candidate) for candidate in self.candidates
            ],
            "provider_states": [
                dict(value) for value in self.provider_states
            ],
            "discovery_revision": self.discovery_revision,
            "review_only": True,
            "automatic_annotation_creation": False,
            "automatic_incident_creation": False,
            "automatic_training_ingest": False,
            "automatic_report_truth": False,
            "annotations_created": 0,
            "incidents_created": 0,
            "training_members_created": 0,
            "report_truth_records_created": 0,
        }


class DenseFrameProposalProvider(Protocol):
    """One local provider applied to verified dense-frame evidence."""

    def status(self, project_id: UUID) -> Mapping[str, Any]:
        ...

    def propose(
        self,
        project_id: UUID,
        frames: Sequence[DenseFrameEvidence],
        *,
        frames_root: Path,
        cancel: Callable[[], bool],
    ) -> Sequence[DenseFrameProposal]:
        ...


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_metadata(
    value: Any,
    *,
    label: str,
    depth: int = 0,
) -> Any:
    if depth > 8:
        raise DenseCandidateDiscoveryError(f"{label} is invalid")
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and (
            len(value) > 4_096 or "\x00" in value
        ):
            raise DenseCandidateDiscoveryError(f"{label} is invalid")
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise DenseCandidateDiscoveryError(f"{label} is invalid")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 1_024:
            raise DenseCandidateDiscoveryError(f"{label} is invalid")
        return [
            _safe_metadata(item, label=label, depth=depth + 1)
            for item in value
        ]
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise DenseCandidateDiscoveryError(f"{label} is invalid")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 128
                or _FORBIDDEN_METADATA_KEY.search(key)
            ):
                raise DenseCandidateDiscoveryError(f"{label} is invalid")
            result[key] = _safe_metadata(
                item,
                label=label,
                depth=depth + 1,
            )
        return result
    raise DenseCandidateDiscoveryError(f"{label} is invalid")


def _promoted_model_binding_base(
    project_id: UUID,
    *,
    state: str,
    model: Optional[ResolvedActiveProjectModel] = None,
    error_code: Optional[str] = None,
) -> dict[str, Any]:
    if state not in {
        "ready",
        "none",
        "failed",
        "resolver_unavailable",
    }:
        raise DenseCandidateDiscoveryError(
            "promoted model binding state is invalid"
        )
    base: dict[str, Any] = {
        "schema_version": PROMOTED_MODEL_JOB_BINDING_SCHEMA,
        "state": state,
        "project_id": str(project_id),
    }
    if state == "ready":
        if (
            model is None
            or model.project_id != str(project_id)
            or isinstance(model.state_revision, bool)
            or model.state_revision < 1
            or isinstance(model.fixed_validation_set_version, bool)
            or model.fixed_validation_set_version < 1
        ):
            raise DenseCandidateDiscoveryError(
                "promoted model binding identity is invalid"
            )
        hash_values = (
            model.dataset_hash,
            model.fixed_validation_sha256,
            model.primary_artifact_sha256,
            model.artifact_manifest_sha256,
            model.source_analysis_lineage_sha256,
            model.dataset_provenance_graph_sha256,
        )
        if any(
            not isinstance(value, str)
            or _SHA256.fullmatch(value) is None
            for value in hash_values
        ):
            raise DenseCandidateDiscoveryError(
                "promoted model binding hashes are invalid"
            )
        identifier_values = (
            model.model_version_id,
            model.dataset_version_id,
            *model.training_analysis_job_ids,
            *model.fixed_validation_analysis_job_ids,
        )
        try:
            if any(
                str(UUID(value)) != value
                for value in identifier_values
            ):
                raise ValueError
        except (TypeError, ValueError) as error:
            raise DenseCandidateDiscoveryError(
                "promoted model binding identifiers are invalid"
            ) from error
        if (
            not model.training_analysis_job_ids
            or not model.fixed_validation_analysis_job_ids
            or len(set(model.training_analysis_job_ids))
            != len(model.training_analysis_job_ids)
            or len(set(model.fixed_validation_analysis_job_ids))
            != len(model.fixed_validation_analysis_job_ids)
            or set(model.training_analysis_job_ids).intersection(
                model.fixed_validation_analysis_job_ids
            )
        ):
            raise DenseCandidateDiscoveryError(
                "promoted model source lineage is invalid"
            )
        source_analysis_lineage = {
            "training_analysis_job_ids": list(
                model.training_analysis_job_ids
            ),
            "fixed_validation_analysis_job_ids": list(
                model.fixed_validation_analysis_job_ids
            ),
        }
        if (
            hashlib.sha256(
                _canonical_json(source_analysis_lineage)
            ).hexdigest()
            != model.source_analysis_lineage_sha256
        ):
            raise DenseCandidateDiscoveryError(
                "promoted model source lineage hash is invalid"
            )
        if (
            not isinstance(model.model_version, str)
            or not 1 <= len(model.model_version) <= 128
            or not isinstance(model.model_format, str)
            or not 1 <= len(model.model_format) <= 128
            or not isinstance(model.output_location_id, str)
            or not 1 <= len(model.output_location_id) <= 128
            or not isinstance(model.root_identity_sha256, str)
            or (
                model.root_identity_sha256
                and _SHA256.fullmatch(model.root_identity_sha256) is None
            )
        ):
            raise DenseCandidateDiscoveryError(
                "promoted model binding metadata is invalid"
            )
        base.update(
            {
                "state_revision": model.state_revision,
                "model_version_id": model.model_version_id,
                "model_version": model.model_version,
                "model_format": model.model_format,
                "dataset_version_id": model.dataset_version_id,
                "dataset_hash": model.dataset_hash,
                "fixed_validation_sha256": (
                    model.fixed_validation_sha256
                ),
                "primary_artifact_sha256": (
                    model.primary_artifact_sha256
                ),
                "artifact_manifest_sha256": (
                    model.artifact_manifest_sha256
                ),
                "training_analysis_job_ids": list(
                    model.training_analysis_job_ids
                ),
                "fixed_validation_analysis_job_ids": list(
                    model.fixed_validation_analysis_job_ids
                ),
                "source_analysis_lineage_sha256": (
                    model.source_analysis_lineage_sha256
                ),
                "dataset_provenance_graph_sha256": (
                    model.dataset_provenance_graph_sha256
                ),
                "output_location_id": model.output_location_id,
                "root_identity_sha256": model.root_identity_sha256,
                "fixed_validation_set_version": (
                    model.fixed_validation_set_version
                ),
            }
        )
    elif state == "failed":
        if (
            not isinstance(error_code, str)
            or re.fullmatch(r"^[a-z][a-z0-9_]{2,63}$", error_code)
            is None
        ):
            raise DenseCandidateDiscoveryError(
                "promoted model binding failure is invalid"
            )
        base["error_code"] = error_code
    return base


def _finish_promoted_model_binding(
    base: Mapping[str, Any],
) -> dict[str, Any]:
    safe = _safe_metadata(base, label="promoted model job binding")
    if not isinstance(safe, dict):
        raise DenseCandidateDiscoveryError(
            "promoted model job binding is invalid"
        )
    return {
        **safe,
        "binding_sha256": hashlib.sha256(
            _canonical_json(safe)
        ).hexdigest(),
    }


def capture_promoted_model_job_binding(
    project_id: UUID,
    resolver: Optional[
        Callable[[UUID], ResolvedActiveProjectModel]
    ],
) -> dict[str, Any]:
    """Capture one path-free active-model identity for a PipelineJob.

    The binding is hashed into the job input fingerprint.  It is intentionally
    separate from a later provider status so a promotion or rollback between
    job start and dense inference cannot silently change the surveyed model.
    """

    if not isinstance(project_id, UUID):
        raise ValueError("project_id must be a UUID")
    if resolver is None:
        return _finish_promoted_model_binding(
            _promoted_model_binding_base(
                project_id,
                state="resolver_unavailable",
            )
        )
    try:
        model = resolver(project_id)
    except ProjectModelLifecycleError as error:
        if error.code == "active_project_model_artifact_unavailable":
            model = None
        else:
            return _finish_promoted_model_binding(
                _promoted_model_binding_base(
                    project_id,
                    state="failed",
                    error_code=error.code,
                )
            )
    if model is None:
        return _finish_promoted_model_binding(
            _promoted_model_binding_base(
                project_id,
                state="none",
            )
        )
    return _finish_promoted_model_binding(
        _promoted_model_binding_base(
            project_id,
            state="ready",
            model=model,
        )
    )


def _validated_promoted_model_job_binding(
    value: Mapping[str, Any],
    *,
    project_id: UUID,
) -> dict[str, Any]:
    safe = _safe_metadata(
        value,
        label="expected promoted model job binding",
    )
    if not isinstance(safe, dict):
        raise DenseCandidateDiscoveryError(
            "expected promoted model job binding is invalid"
        )
    supplied_sha256 = safe.pop("binding_sha256", None)
    state = safe.get("state")
    if (
        safe.get("schema_version")
        != PROMOTED_MODEL_JOB_BINDING_SCHEMA
        or safe.get("project_id") != str(project_id)
        or state not in {
            "ready",
            "none",
            "failed",
            "resolver_unavailable",
        }
        or not isinstance(supplied_sha256, str)
        or _SHA256.fullmatch(supplied_sha256) is None
        or hashlib.sha256(_canonical_json(safe)).hexdigest()
        != supplied_sha256
    ):
        raise DenseCandidateDiscoveryError(
            "expected promoted model job binding is invalid"
        )
    expected_fields = {
        "schema_version",
        "state",
        "project_id",
    }
    if state == "ready":
        expected_fields.update(
            {
                "state_revision",
                "model_version_id",
                "model_version",
                "model_format",
                "dataset_version_id",
                "dataset_hash",
                "fixed_validation_sha256",
                "primary_artifact_sha256",
                "artifact_manifest_sha256",
                "training_analysis_job_ids",
                "fixed_validation_analysis_job_ids",
                "source_analysis_lineage_sha256",
                "dataset_provenance_graph_sha256",
                "output_location_id",
                "root_identity_sha256",
                "fixed_validation_set_version",
            }
        )
    elif state == "failed":
        expected_fields.add("error_code")
    if set(safe) != expected_fields:
        raise DenseCandidateDiscoveryError(
            "expected promoted model job binding is invalid"
        )
    if state == "ready":
        integer_fields = (
            "state_revision",
            "fixed_validation_set_version",
        )
        hash_fields = (
            "dataset_hash",
            "fixed_validation_sha256",
            "primary_artifact_sha256",
            "artifact_manifest_sha256",
            "source_analysis_lineage_sha256",
            "dataset_provenance_graph_sha256",
        )
        identifier_fields = (
            "model_version_id",
            "dataset_version_id",
        )
        training_ids = safe.get("training_analysis_job_ids")
        validation_ids = safe.get(
            "fixed_validation_analysis_job_ids"
        )
        if (
            any(
                isinstance(safe.get(field), bool)
                or not isinstance(safe.get(field), int)
                or safe[field] < 1
                for field in integer_fields
            )
            or any(
                not isinstance(safe.get(field), str)
                or _SHA256.fullmatch(safe[field]) is None
                for field in hash_fields
            )
            or any(
                not isinstance(safe.get(field), str)
                for field in identifier_fields
            )
            or not isinstance(training_ids, list)
            or not training_ids
            or not isinstance(validation_ids, list)
            or not validation_ids
            or any(
                not isinstance(value, str)
                for value in (*training_ids, *validation_ids)
            )
            or len(set(training_ids)) != len(training_ids)
            or len(set(validation_ids)) != len(validation_ids)
            or set(training_ids).intersection(validation_ids)
            or not isinstance(safe.get("model_version"), str)
            or not 1 <= len(safe["model_version"]) <= 128
            or not isinstance(safe.get("model_format"), str)
            or not 1 <= len(safe["model_format"]) <= 128
            or not isinstance(safe.get("output_location_id"), str)
            or not 1 <= len(safe["output_location_id"]) <= 128
            or not isinstance(safe.get("root_identity_sha256"), str)
            or (
                bool(safe["root_identity_sha256"])
                and _SHA256.fullmatch(
                    safe["root_identity_sha256"]
                )
                is None
            )
        ):
            raise DenseCandidateDiscoveryError(
                "expected promoted model job binding is invalid"
            )
        identifier_values = (
            safe["model_version_id"],
            safe["dataset_version_id"],
            *training_ids,
            *validation_ids,
        )
        try:
            if any(
                str(UUID(value)) != value
                for value in identifier_values
            ):
                raise ValueError
        except (TypeError, ValueError) as error:
            raise DenseCandidateDiscoveryError(
                "expected promoted model job binding is invalid"
            ) from error
        source_analysis_lineage = {
            "training_analysis_job_ids": training_ids,
            "fixed_validation_analysis_job_ids": validation_ids,
        }
        if (
            hashlib.sha256(
                _canonical_json(source_analysis_lineage)
            ).hexdigest()
            != safe["source_analysis_lineage_sha256"]
        ):
            raise DenseCandidateDiscoveryError(
                "expected promoted model job binding is invalid"
            )
    elif state == "failed":
        error_code = safe.get("error_code")
        if (
            not isinstance(error_code, str)
            or re.fullmatch(r"^[a-z][a-z0-9_]{2,63}$", error_code)
            is None
        ):
            raise DenseCandidateDiscoveryError(
                "expected promoted model job binding is invalid"
            )
    return {**safe, "binding_sha256": supplied_sha256}


def validate_promoted_model_job_binding(
    value: Mapping[str, Any],
    *,
    project_id: UUID,
) -> dict[str, Any]:
    """Validate one persisted path-free PipelineJob model binding."""

    return _validated_promoted_model_job_binding(
        value,
        project_id=project_id,
    )


def _validated_provider_state(value: Any) -> dict[str, Any]:
    state = _safe_metadata(value, label="dense provider state")
    if (
        not isinstance(state, dict)
        or _PROVIDER_ID.fullmatch(str(state.get("provider", ""))) is None
        or state.get("state") not in {
            "ready",
            "unavailable",
            "failed",
        }
    ):
        raise DenseCandidateDiscoveryError(
            "dense provider state is invalid"
        )
    if len(_canonical_json(state)) > _MAX_METADATA_BYTES:
        raise DenseCandidateDiscoveryError(
            "dense provider state is invalid"
        )
    return state


def _expected_frame_id(
    source_sha256: str,
    frame_index: int,
) -> str:
    material = (
        f"{_DENSE_INDEX_SCHEMA}\0{source_sha256}\0"
        f"fps=2\0frame={frame_index}"
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()[:24]


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _real_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DenseCandidateDiscoveryIntegrityError(
            f"{label}_unavailable"
        ) from error
    if (
        not path.is_dir()
        or path.is_symlink()
        or _is_reparse_point(path)
    ):
        raise DenseCandidateDiscoveryIntegrityError(
            f"{label}_unsafe"
        )
    return resolved


def _read_regular(
    path: Path,
    *,
    maximum_bytes: int,
) -> tuple[bytes, str]:
    try:
        before = path.stat()
    except OSError as error:
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_evidence_unavailable"
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
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_evidence_invalid"
        )
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise DenseCandidateDiscoveryIntegrityError(
                        "dense_evidence_too_large"
                    )
                digest.update(chunk)
                chunks.append(chunk)
        after = path.stat()
    except OSError as error:
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_evidence_unavailable"
        ) from error
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or total != before.st_size
    ):
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_evidence_changed"
        )
    return b"".join(chunks), digest.hexdigest()


def _safe_box(value: Any) -> Optional[dict[str, float]]:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise DenseCandidateDiscoveryError(
            "dense proposal box is invalid"
        )
    result: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = value[key]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
        ):
            raise DenseCandidateDiscoveryError(
                "dense proposal box is invalid"
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
        raise DenseCandidateDiscoveryError(
            "dense proposal box is invalid"
        )
    return result


def _load_dense_frames(
    index_path: Path,
    *,
    source_sha256: str,
    expected_index_sha256: str,
    limits: DenseDiscoveryLimits,
) -> tuple[Path, tuple[DenseFrameEvidence, ...], int]:
    if (
        not _SHA256.fullmatch(source_sha256)
        or not _SHA256.fullmatch(expected_index_sha256)
    ):
        raise ValueError("dense index identity is invalid")
    index_bytes, actual_index_sha256 = _read_regular(
        index_path,
        maximum_bytes=_MAX_INDEX_BYTES,
    )
    if actual_index_sha256 != expected_index_sha256:
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_index_hash_mismatch"
        )
    try:
        payload = json.loads(index_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_index_invalid"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _DENSE_INDEX_SCHEMA
        or not isinstance(payload.get("frames"), list)
        or not isinstance(payload.get("provenance"), dict)
        or payload["provenance"].get("schema_version")
        != _DENSE_INDEX_SCHEMA
        or payload["provenance"].get("source_sha256")
        != source_sha256
        or payload["provenance"].get("sampling_fps") != 2
        or payload["provenance"].get("timestamp_method")
        != "zero_based_frame_index_divided_by_integer_sampling_fps"
        or payload.get("frame_count") != len(payload["frames"])
        or payload["provenance"].get("frame_count")
        != len(payload["frames"])
        or not 1 <= len(payload["frames"])
        or len(payload["frames"]) > limits.max_frames
    ):
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_index_invalid"
        )
    frames_root = _real_directory(
        index_path.parent / "frames",
        "dense_frames_root",
    )
    rows: list[DenseFrameEvidence] = []
    seen_ids: set[str] = set()
    for expected_index, raw in enumerate(payload["frames"]):
        if (
            not isinstance(raw, Mapping)
            or set(raw) != _DENSE_FRAME_KEYS
        ):
            raise DenseCandidateDiscoveryIntegrityError(
                "dense_index_invalid"
            )
        frame_id = raw.get("frame_id")
        frame_index = raw.get("frame_index")
        timestamp = raw.get("timestamp_seconds")
        relative = raw.get("relative_path")
        size_bytes = raw.get("size_bytes")
        digest = raw.get("sha256")
        media_type = raw.get("media_type")
        expected_relative = (
            f"frames/frame-{expected_index:08d}.jpg"
        )
        width = raw.get("width")
        height = raw.get("height")
        if (
            not isinstance(frame_id, str)
            or _FRAME_ID.fullmatch(frame_id) is None
            or frame_id
            != _expected_frame_id(source_sha256, expected_index)
            or frame_id in seen_ids
            or isinstance(frame_index, bool)
            or not isinstance(frame_index, int)
            or frame_index != expected_index
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or abs(float(timestamp) - expected_index / 2) > 0.000001
            or raw.get("timestamp_numerator") != expected_index
            or raw.get("timestamp_denominator") != 2
            or not isinstance(relative, str)
            or relative != expected_relative
            or Path(relative).name != relative.split("/", 1)[1]
            or _FRAME_FILE.fullmatch(Path(relative).name) is None
            or isinstance(width, bool)
            or not isinstance(width, int)
            or not 1 <= width <= 65_535
            or isinstance(height, bool)
            or not isinstance(height, int)
            or not 1 <= height <= 65_535
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= _MAX_IMAGE_BYTES
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or media_type != "image/jpeg"
            or raw.get("evidence_status") != "unreviewed_candidate"
        ):
            raise DenseCandidateDiscoveryIntegrityError(
                "dense_index_invalid"
            )
        unresolved_path = frames_root / Path(relative).name
        try:
            if (
                unresolved_path.is_symlink()
                or _is_reparse_point(unresolved_path)
            ):
                raise DenseCandidateDiscoveryIntegrityError(
                    "dense_frame_path_invalid"
                )
            path = unresolved_path.resolve(strict=True)
        except OSError as error:
            raise DenseCandidateDiscoveryIntegrityError(
                "dense_frame_unavailable"
            ) from error
        if path.parent != frames_root:
            raise DenseCandidateDiscoveryIntegrityError(
                "dense_frame_path_invalid"
            )
        rows.append(
            DenseFrameEvidence(
                frame_id=frame_id,
                frame_index=frame_index,
                timestamp_seconds=float(timestamp),
                file_name=path.name,
                size_bytes=size_bytes,
                sha256=digest,
                media_type=media_type,
                path=path,
            )
        )
        seen_ids.add(frame_id)
    rows.sort(key=lambda item: (item.frame_index, item.frame_id))
    if [item.frame_index for item in rows] != list(range(len(rows))):
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_index_invalid"
        )
    return frames_root, tuple(rows), 2


def _verified_body(frame: DenseFrameEvidence) -> bytes:
    body, digest = _read_regular(
        frame.path,
        maximum_bytes=_MAX_IMAGE_BYTES,
    )
    if len(body) != frame.size_bytes or digest != frame.sha256:
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_frame_hash_mismatch"
        )
    return body


class LocalVisionDenseProposalProvider:
    """Run a dedicated detector or OCR pack across the real 2 FPS index."""

    def __init__(
        self,
        service: LocalVisionInferenceService,
        *,
        role: str,
    ) -> None:
        if role not in {"detector", "ocr"}:
            raise ValueError("dense local vision role is unsupported")
        self._service = service
        self._role = role

    def status(self, project_id: UUID) -> Mapping[str, Any]:
        del project_id
        value = dict(self._service.status())
        return {
            "provider": f"local_{self._role}",
            "state": "ready" if value.get("enabled") else "unavailable",
            "role": self._role,
            "pack_id": value.get("pack_id"),
            "pack_version": value.get("pack_version"),
            "manifest_sha256": value.get("manifest_sha256"),
            "capabilities": list(value.get("capabilities") or ()),
        }

    def propose(
        self,
        project_id: UUID,
        frames: Sequence[DenseFrameEvidence],
        *,
        frames_root: Path,
        cancel: Callable[[], bool],
    ) -> Sequence[DenseFrameProposal]:
        del project_id
        if cancel():
            raise DenseCandidateDiscoveryCancelled(
                "dense candidate discovery was cancelled"
            )
        candidates = [
            {
                "candidate_id": frame.candidate_id,
                "representative_image": {
                    "relative_path": frame.file_name,
                    "size_bytes": frame.size_bytes,
                    "sha256": frame.sha256,
                },
            }
            for frame in frames
        ]
        try:
            results, summary = self._service.infer_candidates(
                candidates,
                frames_root,
                cancel=cancel,
            )
        except VisionInferenceCancelled as error:
            raise DenseCandidateDiscoveryCancelled(
                "dense candidate discovery was cancelled"
            ) from error
        identity = {
            "pack_id": summary.get("pack_id"),
            "pack_version": summary.get("pack_version"),
            "manifest_sha256": summary.get("manifest_sha256"),
            "mode": summary.get("mode"),
        }
        by_candidate = {
            frame.candidate_id: frame for frame in frames
        }
        proposals: list[DenseFrameProposal] = []
        for candidate_id, result in results.items():
            frame = by_candidate.get(candidate_id)
            if frame is None:
                raise DenseCandidateDiscoveryError(
                    "local vision returned an unknown dense frame"
                )
            if result.get("mode") != "local_model":
                continue
            if self._role == "detector":
                for prediction in result.get("predictions", ()):
                    issue_type = str(prediction["issue_type"])
                    normalized_box = _safe_box(
                        prediction.get("box")
                    )
                    if (
                        issue_type in _FORWARD_ROAD_ROI_ISSUE_TYPES
                        and not _box_intersects_forward_road_roi(
                            normalized_box
                        )
                    ):
                        continue
                    if (
                        issue_type == "road_debris"
                        and normalized_box is not None
                        and not _box_intersects_forward_road_roi(
                            normalized_box,
                            # Safety-critical debris gets a wider shoulder
                            # allowance and still remains mandatory review.
                            horizontal_margin=0.08,
                        )
                    ):
                        continue
                    calibration = str(
                        prediction.get("calibration", "")
                    )
                    score_semantics = (
                        "model_score_not_probability"
                        if calibration == "model_score"
                        else "zero_shot_relative_not_probability"
                    )
                    proposals.append(
                        DenseFrameProposal(
                            frame_id=frame.frame_id,
                            issue_type=issue_type,
                            score=float(
                                prediction["confidence"]
                            ),
                            normalized_box=normalized_box,
                            proposal_source=(
                                "dedicated_road_model_dense_2fps"
                            ),
                            urgent_review=(
                                issue_type == "road_debris"
                            ),
                            provenance={
                                **identity,
                                "source_label": prediction.get(
                                    "source_label"
                                ),
                                "calibration": calibration,
                                "score_semantics": score_semantics,
                                "spatial_gate": (
                                    "forward_road_roi_v1"
                                    if issue_type
                                    in (
                                        _FORWARD_ROAD_ROI_ISSUE_TYPES
                                        | {"road_debris"}
                                    )
                                    else "not_applicable"
                                ),
                            },
                        )
                    )
            else:
                for observation in result.get(
                    "ocr_observations", ()
                ):
                    if not _ocr_text_can_identify_korean_road_sign(
                        observation.get("text"),
                        confidence=observation.get("confidence"),
                    ):
                        # Short fragments, numeric utility labels and plates
                        # remain OCR/redaction evidence, but must not become a
                        # guide-sign identity candidate.
                        continue
                    proposals.append(
                        DenseFrameProposal(
                            frame_id=frame.frame_id,
                            issue_type="guide_sign_identity",
                            score=float(
                                observation["confidence"]
                            ),
                            normalized_box=_safe_box(
                                observation.get("box")
                            ),
                            ocr_text=str(observation["text"]),
                            proposal_source=(
                                "korean_ocr_dense_2fps"
                            ),
                            provenance=identity,
                        )
                    )
        return proposals


class ActiveProjectModelDenseProposalProvider:
    """Apply only a hash-verified, explicitly promoted project model."""

    def __init__(
        self,
        resolver: Callable[[UUID], ResolvedActiveProjectModel],
        inference: Optional[
            VisualEmbeddingCentroidHeadInference
        ] = None,
        *,
        detector_inference: Optional[
            PromotedLocalObjectDetectorInference
        ] = None,
    ) -> None:
        self._resolver = resolver
        self._inference = inference
        self._detector_inference = detector_inference
        self._snapshot = local()

    def bind_survey_context(
        self,
        project_id: UUID,
        analysis_job_id: Optional[UUID],
    ) -> None:
        """Bind one later-video analysis identity to this thread-local scan."""

        if not isinstance(project_id, UUID):
            raise DenseCandidateDiscoveryError(
                "project model survey project identity is invalid"
            )
        if analysis_job_id is not None and not isinstance(
            analysis_job_id, UUID
        ):
            raise DenseCandidateDiscoveryError(
                "project model survey analysis identity is invalid"
            )
        self._snapshot.survey_project_id = str(project_id)
        self._snapshot.survey_analysis_job_id = (
            str(analysis_job_id)
            if analysis_job_id is not None
            else None
        )

    def bind_expected_model_context(
        self,
        project_id: UUID,
        expected_binding: Optional[Mapping[str, Any]],
    ) -> None:
        """Bind the exact promoted model captured by the owning PipelineJob."""

        if not isinstance(project_id, UUID):
            raise DenseCandidateDiscoveryError(
                "project model binding project identity is invalid"
            )
        self._snapshot.expected_binding = (
            _validated_promoted_model_job_binding(
                expected_binding,
                project_id=project_id,
            )
            if expected_binding is not None
            else None
        )

    def _resolve(
        self, project_id: UUID
    ) -> Optional[ResolvedActiveProjectModel]:
        try:
            return self._resolver(project_id)
        except ProjectModelLifecycleError as error:
            if error.code == (
                "active_project_model_artifact_unavailable"
            ):
                return None
            raise

    def status(self, project_id: UUID) -> Mapping[str, Any]:
        resolution_error: Optional[ProjectModelLifecycleError] = None
        try:
            model = self._resolve(project_id)
        except ProjectModelLifecycleError as error:
            model = None
            resolution_error = error
        expected_binding = getattr(
            self._snapshot,
            "expected_binding",
            None,
        )
        if expected_binding is not None:
            actual_binding = _finish_promoted_model_binding(
                _promoted_model_binding_base(
                    project_id,
                    state=(
                        "failed"
                        if resolution_error is not None
                        else "ready"
                        if model is not None
                        else "none"
                    ),
                    model=model,
                    error_code=(
                        resolution_error.code
                        if resolution_error is not None
                        else None
                    ),
                )
            )
            if actual_binding != expected_binding:
                self._snapshot.model = None
                raise DenseCandidateDiscoveryIntegrityError(
                    "promoted_project_model_binding_conflict"
                )
        self._snapshot.project_id = str(project_id)
        self._snapshot.model = model
        self._snapshot.inference_kind = None
        if resolution_error is not None:
            return {
                "provider": "promoted_project_model",
                "state": "failed",
                "reason": resolution_error.code,
            }
        if model is None:
            return {
                "provider": "promoted_project_model",
                "state": "unavailable",
                "reason": "no_promoted_project_model",
            }
        survey_analysis_job_id = getattr(
            self._snapshot,
            "survey_analysis_job_id",
            None,
        )
        if (
            getattr(self._snapshot, "survey_project_id", None)
            != str(project_id)
            or survey_analysis_job_id is None
        ):
            self._snapshot.model = None
            return {
                "provider": "promoted_project_model",
                "state": "unavailable",
                "reason": "survey_analysis_job_identity_missing",
            }
        source_analysis_lineage = {
            "training_analysis_job_ids": list(
                model.training_analysis_job_ids
            ),
            "fixed_validation_analysis_job_ids": list(
                model.fixed_validation_analysis_job_ids
            ),
        }
        if (
            not model.training_analysis_job_ids
            or not model.fixed_validation_analysis_job_ids
            or _SHA256.fullmatch(
                model.source_analysis_lineage_sha256
            )
            is None
            or _SHA256.fullmatch(
                model.dataset_provenance_graph_sha256
            )
            is None
            or hashlib.sha256(
                _canonical_json(source_analysis_lineage)
            ).hexdigest()
            != model.source_analysis_lineage_sha256
        ):
            self._snapshot.model = None
            return {
                "provider": "promoted_project_model",
                "state": "unavailable",
                "reason": "active_model_source_lineage_unavailable",
            }
        excluded_source_jobs = set(
            model.training_analysis_job_ids
        ) | set(model.fixed_validation_analysis_job_ids)
        if survey_analysis_job_id in excluded_source_jobs:
            self._snapshot.model = None
            return {
                "provider": "promoted_project_model",
                "state": "unavailable",
                "reason": "survey_analysis_reuses_model_source",
                "survey_analysis_job_id": survey_analysis_job_id,
                "source_analysis_lineage_sha256": (
                    model.source_analysis_lineage_sha256
                ),
            }
        if model.model_format == VISUAL_CENTROID_MODEL_FORMAT:
            if self._inference is None:
                self._snapshot.model = None
                return {
                    "provider": "promoted_project_model",
                    "state": "unavailable",
                    "reason": "centroid_inference_runtime_unavailable",
                    "model_format": model.model_format,
                }
            self._snapshot.inference_kind = "centroid"
        elif model.model_format == LOCAL_OBJECT_DETECTOR_MODEL_FORMAT:
            if self._detector_inference is None:
                self._snapshot.model = None
                return {
                    "provider": "promoted_project_model",
                    "state": "unavailable",
                    "reason": "detector_inference_runtime_unavailable",
                    "model_format": model.model_format,
                }
            runtime_status = _safe_metadata(
                self._detector_inference.status(),
                label="promoted detector runtime status",
            )
            if (
                not isinstance(runtime_status, dict)
                or runtime_status.get("state")
                not in {"ready", "unavailable", "failed"}
            ):
                self._snapshot.model = None
                return {
                    "provider": "promoted_project_model",
                    "state": "failed",
                    "reason": "detector_runtime_status_invalid",
                    "model_format": model.model_format,
                }
            runtime_state = str(runtime_status["state"])
            if runtime_state != "ready":
                self._snapshot.model = None
                return {
                    "provider": "promoted_project_model",
                    "state": runtime_state,
                    "reason": str(
                        runtime_status.get("reason")
                        or (
                            "detector_runtime_unavailable"
                            if runtime_state == "unavailable"
                            else "detector_runtime_failed"
                        )
                    ),
                    "model_format": model.model_format,
                    "runtime_provider": runtime_status.get(
                        "provider",
                        "promoted_local_object_detector",
                    ),
                    "backend_id": runtime_status.get("backend_id"),
                    "backend_version": runtime_status.get(
                        "backend_version"
                    ),
                }
            self._snapshot.inference_kind = "detector"
        else:
            self._snapshot.model = None
            return {
                "provider": "promoted_project_model",
                "state": "failed",
                "reason": "unsupported_promoted_model_format",
                "model_format": model.model_format,
            }
        return {
            "provider": "promoted_project_model",
            "state": "ready",
            "model_version_id": model.model_version_id,
            "model_version": model.model_version,
            "dataset_version_id": model.dataset_version_id,
            "dataset_hash": model.dataset_hash,
            "fixed_validation_sha256": (
                model.fixed_validation_sha256
            ),
            "primary_artifact_sha256": (
                model.primary_artifact_sha256
            ),
            "artifact_manifest_sha256": (
                model.artifact_manifest_sha256
            ),
            "state_revision": model.state_revision,
            "survey_analysis_job_id": survey_analysis_job_id,
            "independent_survey_verified": True,
            "source_analysis_lineage_sha256": (
                model.source_analysis_lineage_sha256
            ),
            "dataset_provenance_graph_sha256": (
                model.dataset_provenance_graph_sha256
            ),
            "model_format": model.model_format,
        }

    def propose(
        self,
        project_id: UUID,
        frames: Sequence[DenseFrameEvidence],
        *,
        frames_root: Path,
        cancel: Callable[[], bool],
    ) -> Sequence[DenseFrameProposal]:
        del frames_root
        if getattr(self._snapshot, "project_id", None) != str(project_id):
            raise DenseCandidateDiscoveryError(
                "promoted project model snapshot is unavailable"
            )
        model = getattr(self._snapshot, "model", None)
        if model is None:
            return ()
        survey_analysis_job_id = getattr(
            self._snapshot,
            "survey_analysis_job_id",
            None,
        )
        if (
            not isinstance(survey_analysis_job_id, str)
            or survey_analysis_job_id
            in (
                set(model.training_analysis_job_ids)
                | set(model.fixed_validation_analysis_job_ids)
            )
        ):
            raise DenseCandidateDiscoveryError(
                "promoted project model survey lineage is invalid"
            )
        inference_kind = getattr(
            self._snapshot,
            "inference_kind",
            None,
        )
        if inference_kind not in {"centroid", "detector"}:
            raise DenseCandidateDiscoveryError(
                "promoted project model inference route is unavailable"
            )
        proposals: list[DenseFrameProposal] = []
        for frame in frames:
            if cancel():
                raise DenseCandidateDiscoveryCancelled(
                    "dense candidate discovery was cancelled"
                )
            if inference_kind == "detector":
                if self._detector_inference is None:
                    raise DenseCandidateDiscoveryError(
                        "promoted detector inference is unavailable"
                    )
                result = self._detector_inference.classify(
                    model,
                    survey_analysis_job_id=survey_analysis_job_id,
                    body=_verified_body(frame),
                    media_type=frame.media_type,
                    expected_sha256=frame.sha256,
                )
            else:
                if self._inference is None:
                    raise DenseCandidateDiscoveryError(
                        "promoted centroid inference is unavailable"
                    )
                result = self._inference.classify(
                    model,
                    body=_verified_body(frame),
                    media_type=frame.media_type,
                    expected_sha256=frame.sha256,
                )
            provenance = {
                "model_version_id": result["model_version_id"],
                "model_version": result["model_version"],
                "dataset_version_id": result["dataset_version_id"],
                "dataset_hash": model.dataset_hash,
                "fixed_validation_sha256": (
                    model.fixed_validation_sha256
                ),
                "primary_artifact_sha256": (
                    model.primary_artifact_sha256
                ),
                "artifact_manifest_sha256": (
                    model.artifact_manifest_sha256
                ),
                "state_revision": model.state_revision,
                "survey_analysis_job_id": (
                    survey_analysis_job_id
                ),
                "independent_survey_verified": True,
                "source_analysis_lineage_sha256": (
                    model.source_analysis_lineage_sha256
                ),
                "dataset_provenance_graph_sha256": (
                    model.dataset_provenance_graph_sha256
                ),
            }
            if inference_kind == "detector":
                provenance["model_format"] = model.model_format
                provenance["candidate_image_sha256"] = result[
                    "candidate_image_sha256"
                ]
            for prediction in result["predictions"]:
                normalized_box = None
                if inference_kind == "detector":
                    normalized_box = _safe_box(
                        prediction.get("box")
                    )
                    if normalized_box is None:
                        raise DenseCandidateDiscoveryError(
                            "promoted detector prediction box is invalid"
                        )
                proposals.append(
                    DenseFrameProposal(
                        frame_id=frame.frame_id,
                        issue_type=str(prediction["issue_type"]),
                        score=float(prediction["score"]),
                        proposal_source=(
                            "promoted_project_model_dense_2fps"
                        ),
                        urgent_review=(
                            prediction["issue_type"] == "road_debris"
                        ),
                        normalized_box=normalized_box,
                        provenance=provenance,
                    )
                )
        return proposals


class ApprovedReferenceDenseProposalProvider:
    """Use only user-approved 3Q/4Q cases as weak dense-frame hints."""

    def __init__(
        self,
        service: WeakCaseVisualIndexService,
        *,
        minimum_similarity: float = 0.78,
    ) -> None:
        if (
            isinstance(minimum_similarity, bool)
            or not isinstance(minimum_similarity, (int, float))
            or not 0.0 <= float(minimum_similarity) <= 1.0
        ):
            raise ValueError(
                "reference similarity threshold is invalid"
            )
        self._service = service
        self._threshold = float(minimum_similarity)

    def status(self, project_id: UUID) -> Mapping[str, Any]:
        try:
            build = self._service.build(project_id)
        except WeakCaseVisualIndexError as error:
            return {
                "provider": "approved_3q_4q_reference_search",
                "state": "unavailable",
                "reason": error.code,
            }
        return {
            "provider": "approved_3q_4q_reference_search",
            "state": "ready",
            "index_manifest_sha256": build.get(
                "index_manifest_sha256"
            ),
            "source_snapshot_sha256": build.get(
                "source_snapshot_sha256"
            ),
            "approved_reference_count": build.get(
                "approved_reference_count", 0
            ),
            "minimum_similarity": self._threshold,
        }

    def propose(
        self,
        project_id: UUID,
        frames: Sequence[DenseFrameEvidence],
        *,
        frames_root: Path,
        cancel: Callable[[], bool],
    ) -> Sequence[DenseFrameProposal]:
        del frames_root
        try:
            build = self._service.build(project_id)
        except WeakCaseVisualIndexError as error:
            raise DenseCandidateDiscoveryIntegrityError(
                f"approved_reference_build_failed:{error.code}"
            ) from error
        approved_count = build.get("approved_reference_count")
        if (
            isinstance(approved_count, bool)
            or not isinstance(approved_count, int)
            or approved_count < 0
        ):
            raise DenseCandidateDiscoveryIntegrityError(
                "approved_reference_build_invalid"
            )
        # The unapproved 3Q/4Q photos remain available later as explicit
        # weak comparison context. They must not trigger 2 fps dense
        # proposals, and embedding every frame here would be wasted work
        # until at least one project-scoped reference is approved.
        if approved_count == 0:
            return ()
        proposals: list[DenseFrameProposal] = []
        for frame in frames:
            if cancel():
                raise DenseCandidateDiscoveryCancelled(
                    "dense candidate discovery was cancelled"
                )
            try:
                result = self._service.query(
                    project_id,
                    CandidateFrameEvidence(
                        project_id=project_id,
                        candidate_id=frame.candidate_id,
                        body=_verified_body(frame),
                        media_type=frame.media_type,
                        expected_sha256=frame.sha256,
                    ),
                    issue_type=None,
                    query_text=None,
                    limit=5,
                )
            except WeakCaseVisualIndexError as error:
                raise DenseCandidateDiscoveryIntegrityError(
                    f"approved_reference_query_failed:{error.code}"
                ) from error
            if (
                not isinstance(result, Mapping)
                or not isinstance(result.get("cases"), Sequence)
                or isinstance(result.get("cases"), (str, bytes))
                or not isinstance(
                    result.get("index_manifest_sha256"), str
                )
                or _SHA256.fullmatch(
                    result["index_manifest_sha256"]
                )
                is None
                or not isinstance(
                    result.get("source_snapshot_sha256"), str
                )
                or _SHA256.fullmatch(
                    result["source_snapshot_sha256"]
                )
                is None
            ):
                raise DenseCandidateDiscoveryIntegrityError(
                    "approved_reference_query_invalid"
                )
            approved = []
            for case in result["cases"]:
                if not isinstance(case, Mapping):
                    raise DenseCandidateDiscoveryIntegrityError(
                        "approved_reference_query_invalid"
                    )
                if case.get("approved_reference") is not True:
                    continue
                score = case.get("combined_similarity_score")
                if (
                    not isinstance(
                        case.get("approved_issue_type"), str
                    )
                    or isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(float(score))
                    or not 0.0 <= float(score) <= 1.0
                    or not isinstance(case.get("source_id"), str)
                    or not isinstance(case.get("case_id"), str)
                    or not isinstance(
                        case.get("approval_record_sha256"), str
                    )
                    or _SHA256.fullmatch(
                        case["approval_record_sha256"]
                    )
                    is None
                ):
                    raise DenseCandidateDiscoveryIntegrityError(
                        "approved_reference_query_invalid"
                    )
                if float(score) >= self._threshold:
                    approved.append(case)
            if not approved:
                continue
            best = max(
                approved,
                key=lambda case: (
                    float(case["combined_similarity_score"]),
                    str(case["case_id"]),
                ),
            )
            proposals.append(
                DenseFrameProposal(
                    frame_id=frame.frame_id,
                    issue_type=str(best["approved_issue_type"]),
                    score=float(
                        best["combined_similarity_score"]
                    ),
                    proposal_source=(
                        "approved_3q_4q_reference_dense_2fps"
                    ),
                    urgent_review=(
                        best["approved_issue_type"] == "road_debris"
                    ),
                    provenance={
                        "index_manifest_sha256": result[
                            "index_manifest_sha256"
                        ],
                        "source_snapshot_sha256": result[
                            "source_snapshot_sha256"
                        ],
                        "reference_source_id": best["source_id"],
                        "reference_case_id": best["case_id"],
                        "approval_record_sha256": best[
                            "approval_record_sha256"
                        ],
                    },
                )
            )
        return proposals


def _validated_proposal(
    value: DenseFrameProposal,
    *,
    known_frames: set[str],
) -> DenseFrameProposal:
    if (
        not isinstance(value, DenseFrameProposal)
        or value.frame_id not in known_frames
        or _FRAME_ID.fullmatch(value.frame_id) is None
        or _ISSUE_TYPE.fullmatch(value.issue_type) is None
        or isinstance(value.score, bool)
        or not isinstance(value.score, (int, float))
        or not math.isfinite(float(value.score))
        or not 0.0 <= float(value.score) <= 1.0
        or not isinstance(value.proposal_source, str)
        or not 1 <= len(value.proposal_source) <= 128
    ):
        raise DenseCandidateDiscoveryError(
            "dense proposal is invalid"
        )
    box = _safe_box(value.normalized_box)
    text = value.ocr_text
    if text is not None and (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > 500
        or "\x00" in text
    ):
        raise DenseCandidateDiscoveryError(
            "dense proposal OCR text is invalid"
        )
    provenance = value.provenance
    if provenance is not None:
        provenance = _safe_metadata(
            provenance,
            label="dense proposal provenance",
        )
        if not isinstance(provenance, dict):
            raise DenseCandidateDiscoveryError(
                "dense proposal provenance is invalid"
            )
        encoded = _canonical_json(provenance)
        if len(encoded) > 64 * 1024:
            raise DenseCandidateDiscoveryError(
                "dense proposal provenance is invalid"
            )
    return DenseFrameProposal(
        frame_id=value.frame_id,
        issue_type=value.issue_type,
        score=round(float(value.score), 8),
        proposal_source=value.proposal_source,
        normalized_box=box,
        ocr_text=text.strip() if text is not None else None,
        urgent_review=bool(value.urgent_review),
        provenance=provenance,
    )


def _merge_candidates(
    frames: Sequence[DenseFrameEvidence],
    proposals: Sequence[DenseFrameProposal],
    *,
    source_sha256: str,
    limits: DenseDiscoveryLimits,
) -> tuple[DenseCandidateSeed, ...]:
    frame_by_id = {frame.frame_id: frame for frame in frames}
    rows = sorted(
        (
            proposal
            for proposal in proposals
            if proposal.score >= limits.minimum_score
        ),
        key=lambda item: (
            item.issue_type,
            frame_by_id[item.frame_id].timestamp_seconds,
            item.frame_id,
            item.proposal_source,
        ),
    )
    groups: list[list[DenseFrameProposal]] = []
    for proposal in rows:
        if (
            groups
            and groups[-1][0].issue_type == proposal.issue_type
            and (
                frame_by_id[proposal.frame_id].timestamp_seconds
                - frame_by_id[
                    groups[-1][-1].frame_id
                ].timestamp_seconds
            )
            <= limits.temporal_gap_seconds
        ):
            groups[-1].append(proposal)
        else:
            groups.append([proposal])

    candidates: list[DenseCandidateSeed] = []
    for group in groups:
        best = max(
            group,
            key=lambda item: (
                item.score,
                bool(item.normalized_box),
                -frame_by_id[item.frame_id].frame_index,
                item.proposal_source,
            ),
        )
        supporting = tuple(
            sorted(
                {item.frame_id for item in group},
                key=lambda frame_id: (
                    frame_by_id[frame_id].frame_index,
                    frame_id,
                ),
            )
        )
        first = frame_by_id[supporting[0]]
        last = frame_by_id[supporting[-1]]
        representative = frame_by_id[best.frame_id]
        sources = tuple(
            sorted({item.proposal_source for item in group})
        )
        provenance_records = [
            {
                "frame_id": item.frame_id,
                "proposal_source": item.proposal_source,
                "provenance": item.provenance,
            }
            for item in group
        ]
        proposal_provenance_sha256 = hashlib.sha256(
            _canonical_json(provenance_records)
        ).hexdigest()
        material = {
            "schema_version": DENSE_CANDIDATE_DISCOVERY_SCHEMA,
            "source_sha256": source_sha256,
            "issue_type": best.issue_type,
            "supporting_frame_ids": supporting,
            "proposal_sources": sources,
            "proposal_provenance_sha256": (
                proposal_provenance_sha256
            ),
        }
        candidate_id = hashlib.sha256(
            _canonical_json(material)
        ).hexdigest()[:20]
        candidates.append(
            DenseCandidateSeed(
                candidate_id=candidate_id,
                issue_type=best.issue_type,
                start_seconds=round(
                    max(0.0, first.timestamp_seconds - 0.25), 6
                ),
                end_seconds=round(
                    last.timestamp_seconds + 0.25, 6
                ),
                representative_timestamp_seconds=round(
                    representative.timestamp_seconds, 6
                ),
                representative_frame_id=representative.frame_id,
                representative_frame_sha256=representative.sha256,
                score=best.score,
                normalized_box=best.normalized_box,
                ocr_text=best.ocr_text,
                urgent_review=any(
                    item.urgent_review for item in group
                ),
                supporting_frame_ids=supporting,
                proposal_sources=sources,
                representative_proposal_provenance=(
                    dict(best.provenance)
                    if best.provenance is not None
                    else None
                ),
                proposal_provenance_sha256=(
                    proposal_provenance_sha256
                ),
            )
        )
    candidates.sort(
        key=lambda item: (
            not item.urgent_review,
            -item.score,
            item.representative_timestamp_seconds,
            item.candidate_id,
        )
    )
    selected = candidates[: limits.max_candidates]
    selected.sort(
        key=lambda item: (
            item.representative_timestamp_seconds,
            item.issue_type,
            item.candidate_id,
        )
    )
    return tuple(selected)


class DenseCandidateDiscoveryEngine:
    """Scan the complete verified 2 FPS index with local providers."""

    def __init__(
        self,
        providers: Sequence[DenseFrameProposalProvider],
        *,
        limits: Optional[DenseDiscoveryLimits] = None,
    ) -> None:
        if not isinstance(providers, Sequence):
            raise ValueError("dense proposal providers are invalid")
        self._providers = tuple(providers)
        self._limits = limits or DenseDiscoveryLimits()

    def discover(
        self,
        *,
        project_id: UUID,
        analysis_job_id: Optional[UUID] = None,
        expected_promoted_model_binding: Optional[
            Mapping[str, Any]
        ] = None,
        source_sha256: str,
        dense_index_path: Path,
        dense_index_sha256: str,
        cancel: Callable[[], bool],
    ) -> DenseCandidateDiscoveryResult:
        if not isinstance(project_id, UUID):
            raise ValueError("project_id must be a UUID")
        if analysis_job_id is not None and not isinstance(
            analysis_job_id, UUID
        ):
            raise ValueError("analysis_job_id must be a UUID")
        if not callable(cancel):
            raise ValueError("cancel must be callable")
        expected_binding = (
            _validated_promoted_model_job_binding(
                expected_promoted_model_binding,
                project_id=project_id,
            )
            if expected_promoted_model_binding is not None
            else None
        )
        index_path = Path(dense_index_path)
        index_root = _real_directory(
            index_path.parent,
            "dense_index_root",
        )
        if index_path.parent.resolve() != index_root:
            raise DenseCandidateDiscoveryIntegrityError(
                "dense_index_path_invalid"
            )
        frames_root, frames, sampling_fps = _load_dense_frames(
            index_path,
            source_sha256=source_sha256,
            expected_index_sha256=dense_index_sha256,
            limits=self._limits,
        )
        provider_states: list[Mapping[str, Any]] = []
        proposals: list[DenseFrameProposal] = []
        for frame in frames:
            if cancel():
                raise DenseCandidateDiscoveryCancelled(
                    "dense candidate discovery was cancelled"
                )
            _verified_body(frame)
        for provider in self._providers:
            if cancel():
                raise DenseCandidateDiscoveryCancelled(
                    "dense candidate discovery was cancelled"
                )
            bind_survey_context = getattr(
                provider,
                "bind_survey_context",
                None,
            )
            if callable(bind_survey_context):
                bind_survey_context(project_id, analysis_job_id)
            bind_expected_model_context = getattr(
                provider,
                "bind_expected_model_context",
                None,
            )
            if callable(bind_expected_model_context):
                bind_expected_model_context(
                    project_id,
                    expected_binding,
                )
            state = _validated_provider_state(
                provider.status(project_id)
            )
            provider_states.append(state)
            if state.get("state") != "ready":
                continue
            for offset in range(0, len(frames), self._limits.batch_size):
                if cancel():
                    raise DenseCandidateDiscoveryCancelled(
                        "dense candidate discovery was cancelled"
                    )
                batch = frames[
                    offset : offset + self._limits.batch_size
                ]
                emitted = provider.propose(
                    project_id,
                    batch,
                    frames_root=frames_root,
                    cancel=cancel,
                )
                if cancel():
                    raise DenseCandidateDiscoveryCancelled(
                        "dense candidate discovery was cancelled"
                    )
                if (
                    not isinstance(emitted, Sequence)
                    or isinstance(emitted, (str, bytes, bytearray))
                ):
                    raise DenseCandidateDiscoveryError(
                        "dense provider output is invalid"
                    )
                if len(emitted) > (
                    len(batch)
                    * self._limits.max_proposals_per_frame
                ):
                    raise DenseCandidateDiscoveryError(
                        "dense provider exceeded its proposal bound"
                    )
                batch_frame_ids = {
                    frame.frame_id for frame in batch
                }
                validated_batch: list[DenseFrameProposal] = []
                per_frame_counts: dict[str, int] = {}
                for value in emitted:
                    proposal = _validated_proposal(
                        value,
                        known_frames=batch_frame_ids,
                    )
                    count = per_frame_counts.get(
                        proposal.frame_id, 0
                    ) + 1
                    if count > (
                        self._limits.max_proposals_per_frame
                    ):
                        raise DenseCandidateDiscoveryError(
                            "dense provider exceeded its per-frame "
                            "proposal bound"
                        )
                    per_frame_counts[proposal.frame_id] = count
                    validated_batch.append(proposal)
                proposals.extend(validated_batch)
        if cancel():
            raise DenseCandidateDiscoveryCancelled(
                "dense candidate discovery was cancelled"
            )
        candidates = _merge_candidates(
            frames,
            proposals,
            source_sha256=source_sha256,
            limits=self._limits,
        )
        revision_payload = {
            "schema_version": DENSE_CANDIDATE_DISCOVERY_SCHEMA,
            "project_id": str(project_id),
            "source_sha256": source_sha256,
            "dense_index_sha256": dense_index_sha256,
            "sampling_fps": sampling_fps,
            "scanned_frame_count": len(frames),
            "provider_states": provider_states,
            "candidates": [
                asdict(candidate) for candidate in candidates
            ],
            "review_only": True,
            "automatic_annotation_creation": False,
            "automatic_incident_creation": False,
            "automatic_training_ingest": False,
            "automatic_report_truth": False,
        }
        return DenseCandidateDiscoveryResult(
            schema_version=DENSE_CANDIDATE_DISCOVERY_SCHEMA,
            project_id=str(project_id),
            source_sha256=source_sha256,
            dense_index_sha256=dense_index_sha256,
            sampling_fps=sampling_fps,
            scanned_frame_count=len(frames),
            candidates=candidates,
            provider_states=tuple(provider_states),
            discovery_revision=hashlib.sha256(
                _canonical_json(revision_payload)
            ).hexdigest(),
        )


def resolve_dense_candidate_frame(
    *,
    dense_index_path: Path,
    source_sha256: str,
    dense_index_sha256: str,
    frame_id: str,
    limits: Optional[DenseDiscoveryLimits] = None,
) -> DenseFrameEvidence:
    """Resolve one verified frame from an immutable 2 FPS index.

    This is the bridge-internal handoff used by the candidate-evidence
    materializer.  It reopens and validates the complete index before
    returning a ``Path``-bearing grant; callers must never serialize that
    grant to the renderer.
    """

    if not isinstance(frame_id, str) or _FRAME_ID.fullmatch(frame_id) is None:
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_frame_identity_invalid"
        )
    _frames_root, frames, _sampling_fps = _load_dense_frames(
        Path(dense_index_path),
        source_sha256=source_sha256,
        expected_index_sha256=dense_index_sha256,
        limits=limits or DenseDiscoveryLimits(),
    )
    match = next(
        (frame for frame in frames if frame.frame_id == frame_id),
        None,
    )
    if match is None:
        raise DenseCandidateDiscoveryIntegrityError(
            "dense_frame_unavailable"
        )
    _verified_body(match)
    return match


__all__ = [
    "ActiveProjectModelDenseProposalProvider",
    "ApprovedReferenceDenseProposalProvider",
    "DENSE_CANDIDATE_DISCOVERY_SCHEMA",
    "DenseCandidateDiscoveryCancelled",
    "DenseCandidateDiscoveryEngine",
    "DenseCandidateDiscoveryError",
    "DenseCandidateDiscoveryIntegrityError",
    "DenseCandidateDiscoveryResult",
    "DenseCandidateSeed",
    "DenseDiscoveryLimits",
    "DenseFrameEvidence",
    "DenseFrameProposal",
    "DenseFrameProposalProvider",
    "LocalVisionDenseProposalProvider",
    "PROMOTED_MODEL_JOB_BINDING_SCHEMA",
    "capture_promoted_model_job_binding",
    "resolve_dense_candidate_frame",
    "validate_promoted_model_job_binding",
]
