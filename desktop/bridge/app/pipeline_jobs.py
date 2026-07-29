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
import stat
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4

from fastapi import HTTPException

from .analysis_jobs import AnalysisJobService
from .auto_labeling import compile_labeling_command
from .candidate_labels import CandidateLabelError, CandidateLabelStore
from .command_proposal_generation import (
    CommandProposalGenerationError,
    CommandProposalGenerator,
)
from .dense_frame_index import (
    DenseFrameIndexCancelled,
    DenseFrameIndexEngine,
    DenseFrameIndexError,
    DenseFrameIndexLimits,
    DenseFrameIndexOutputLimit,
    DenseFrameIndexSourceChanged,
    DenseFrameIndexTimeout,
)
from .derived_outputs import (
    DerivedOutputsConflict,
    DerivedOutputsError,
    DerivedOutputsRequest,
    DerivedOutputsService,
)
from .experimental_proposals import (
    ExperimentalProposalError,
    ExperimentalProposalStore,
)
from .media_intake import (
    CompletedMediaGrant,
    MediaIntakeService,
    is_allowlisted_video_name,
)
from .temporal_incidents import (
    TemporalGroupingPolicy,
    TemporalIncidentProposalError,
    group_temporal_incident_proposals,
)
from .weak_case_catalog import WeakCaseCatalog, WeakCaseCatalogError


PIPELINE_JOB_SCHEMA = "trafficvisson.pipeline-job.v1"
PIPELINE_PROFILE_SCHEMA = "trafficvisson.pipeline-profile.v1"
PRIVATE_MEDIA_GRANT_SCHEMA = "trafficvisson.private-media-grant.v1"
MAX_PIPELINE_JOBS = 128
MAX_PIPELINE_RETRIES = 5
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_PRIVATE_MEDIA_GRANT_BYTES = 16 * 1024
MAX_RECOVERY_ENTRIES = MAX_PIPELINE_JOBS * 8
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

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

_ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancelling"})
_TERMINAL_JOB_STATUSES = frozenset({"completed", "cancelled"})
_RESUMABLE_JOB_STATUSES = frozenset({"blocked", "failed", "cancelled"})
_STAGE_STATUSES = frozenset(
    {"pending", "running", "success", "blocked", "failed", "skipped", "cancelled"}
)
_OUTCOME_CODES = frozenset(
    {
        "success",
        "zero_detections",
        "model_unavailable",
        "blocked_gpx_required",
        "partial",
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


class PipelineStageFailure(PipelineJobError):
    """A stage failed with a stable, non-secret error code."""

    def __init__(self, code: str, message: str = "Pipeline stage failed") -> None:
        if not _SAFE_ERROR_CODE.fullmatch(code):
            raise ValueError("pipeline error code is invalid")
        super().__init__(message)
        self.code = code


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


def _validate_safe_json(value: Any, label: str, *, depth: int = 0) -> None:
    if depth > 8:
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
            _validate_safe_json(item, label, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 10_000:
            raise ValueError(f"{label} has too many items")
        for item in value:
            _validate_safe_json(item, label, depth=depth + 1)
        return
    raise ValueError(f"{label} contains an unsupported value")


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
        os.replace(temporary, path)
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
    default_template_id: str,
    default_template_sha256: str,
    detector_pack_id: str,
    ocr_pack_id: str,
    sam2_pack_id: str,
    report_provider: str,
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
        "default_template_id": default_template_id,
        "default_template_sha256": default_template_sha256,
        "detector_pack_id": detector_pack_id,
        "ocr_pack_id": ocr_pack_id,
        "sam2_pack_id": sam2_pack_id,
        "report_provider": report_provider,
        "output_location_id": output_location_id,
        "candidate_rescan_fps": candidate_rescan_fps,
        "gps_policy": "timed_track_required_for_route",
        "bulk_frame_provider_policy": "local_models_only",
        "revision": revision,
        "updated_at": updated_at.isoformat(),
    }


class PipelineJobService:
    """One-worker durable DAG with project/profile and review approval gates."""

    def __init__(
        self,
        runtime: PipelineRuntime,
        *,
        data_root: Path,
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
            "jobs_requeued": 0,
            "records_rejected": 0,
        }
        recovered = self._load_records()
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
        default_template_id: str,
        default_template_sha256: str,
        detector_pack_id: str,
        ocr_pack_id: str,
        sam2_pack_id: str,
        report_provider: str,
        output_location_id: str,
        candidate_rescan_fps: int,
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
        if report_provider not in {"codex", "claude", "local", "none"}:
            raise ValueError("report_provider is invalid")
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
        with self._lock:
            previous = self._profiles.get(project_id)
            revision = int(previous["revision"]) + 1 if previous else 1
            profile = _profile_payload(
                project_id=project_id,
                question_policy=question_policy,
                review_mode=review_mode,
                default_template_id=default_template_id,
                default_template_sha256=default_template_sha256,
                detector_pack_id=detector_pack_id,
                ocr_pack_id=ocr_pack_id,
                sam2_pack_id=sam2_pack_id,
                report_provider=report_provider,
                output_location_id=output_location_id,
                candidate_rescan_fps=candidate_rescan_fps,
                revision=revision,
                updated_at=_utc_now(),
            )
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

    def start(
        self,
        project_id: UUID,
        *,
        snapshot_id: UUID,
        file_name: str,
        rights_confirmed: bool,
        idempotency_key: str,
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
        with self._lock:
            profile = self._profiles.get(project_id)
            if profile is None:
                raise PipelineProfileMissing(
                    "Configure the project pipeline profile before adding video"
                )
            profile_snapshot = dict(profile)
            capability_snapshot = dict(
                self._runtime.capability_snapshot(profile_snapshot)
            )
            _validate_safe_json(capability_snapshot, "capability snapshot")
            fingerprint = hashlib.sha256(
                _canonical_json(
                    {
                        "project_id": str(project_id),
                        "snapshot_id": str(snapshot_id),
                        "file_name": file_name,
                        "profile": profile_snapshot,
                    }
                )
            ).hexdigest()
            for existing in self._jobs.values():
                if existing.idempotency_key == idempotency_key:
                    if existing.input_fingerprint != fingerprint:
                        raise PipelineJobConflict(
                            "The idempotency key belongs to different input"
                        )
                    return self._serialize(existing), True
                if existing.input_fingerprint == fingerprint:
                    return self._serialize(existing), True
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
                    "stage_order": list(PIPELINE_STAGES),
                    "route_policy": (
                        "timed_gps_only_never_capture_start_interpolation"
                    ),
                    "provider_policy": (
                        "codex_claude_structured_proposal_not_bulk_frames"
                    ),
                },
                created_at=now,
                updated_at=now,
                recovered_after_restart=False,
                cancel_event=threading.Event(),
            )
            self._jobs[job.job_id] = job
            self._persist_job_locked(job)
            self._ensure_worker_locked()
            try:
                self._queue.put_nowait(job.job_id)
            except queue.Full as error:
                self._jobs.pop(job.job_id, None)
                raise PipelineJobUnavailable("pipeline queue is full") from error
            return self._serialize(job), False

    def get(self, project_id: UUID, job_id: UUID) -> dict[str, Any]:
        with self._lock:
            return self._serialize(self._job(project_id, job_id))

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
            capability_snapshot = dict(
                self._runtime.capability_snapshot(job.profile_snapshot)
            )
            _validate_safe_json(
                capability_snapshot, "capability snapshot"
            )
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
            current_index = PIPELINE_STAGES.index(job.current_stage)
            preserve_partial_checkpoint = (
                job.status == "blocked"
                and job.outcome_code == "partial"
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

    def complete_review(
        self,
        project_id: UUID,
        job_id: UUID,
        *,
        review_completion_id: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(review_completion_id, str)
            or _IDENTIFIER.fullmatch(review_completion_id) is None
        ):
            raise ValueError("review_completion_id is invalid")
        with self._lock:
            job = self._job(project_id, job_id)
            if job.status == "completed":
                if job.review_completion_id == review_completion_id:
                    return self._serialize(job)
                raise PipelineJobConflict("pipeline review is already completed")
            if (
                job.review_completion_id == review_completion_id
                and job.current_stage == "derived_outputs"
                and job.status in {"queued", "running"}
            ):
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
        try:
            review_proof = dict(
                self._runtime.validate_review_completion(
                    context,
                    review_completion_id,
                )
            )
            _validate_safe_json(review_proof, "review completion proof")
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
            or review_proof.get("all_required_questions_answered") is not True
            or isinstance(review_proof.get("answered_question_count"), bool)
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
            job.review_completion_id = review_completion_id
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
                self._run_job(job_id)
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: UUID) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.status not in {"queued", "running"}:
                    return
                if job.cancel_event.is_set():
                    if self._stop_event.is_set():
                        return
                    self._mark_cancelled_locked(job)
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
            except PipelineStageFailure as error:
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
                stage["metrics"] = dict(result.metrics)
                stage["outcome"] = result.outcome
                stage["completed_at"] = _utc_now().isoformat()
                stage["error_code"] = result.error_code
                job.updated_at = _utc_now()
                if result.outcome == "success":
                    stage["status"] = "success"
                    if stage_name == "question_queue":
                        question_count = int(result.metrics.get("question_count", 0))
                        if question_count > 0:
                            job.status = "awaiting_review"
                            job.outcome_code = "success"
                            job.error_code = None
                            self._persist_job_locked(job)
                            return
                elif result.outcome == "blocked_gpx_required":
                    stage["status"] = "blocked"
                    job.provenance["route_status"] = "blocked_gpx_required"
                    job.provenance["route_point_source"] = None
                elif result.outcome == "zero_detections":
                    stage["status"] = "success"
                    job.outcome_code = "zero_detections"
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

    @staticmethod
    def _next_pending_index(
        job: PipelineJobRecord, start: int
    ) -> Optional[int]:
        for index in range(start, len(job.stages)):
            if job.stages[index]["status"] == "pending":
                return index
        return None

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
        self._persist_job_locked(job)

    def _finish_success_locked(self, job: PipelineJobRecord) -> None:
        job.status = "completed"
        if job.outcome_code != "zero_detections":
            job.outcome_code = "success"
        job.error_code = None
        job.current_stage = "derived_outputs"
        job.updated_at = _utc_now()
        self._persist_job_locked(job)

    def _runtime_context(self, job: PipelineJobRecord) -> dict[str, Any]:
        checkpoints = {
            stage["name"]: dict(stage["checkpoint"])
            for stage in job.stages
            if stage["checkpoint"]
        }
        return {
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
            "profile_sha256": job.provenance["profile_sha256"],
        }

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
        if len(raw) > MAX_RECORD_BYTES:
            raise PipelineJobUnavailable("pipeline record exceeds its bound")
        digest = hashlib.sha256(raw).hexdigest()
        path = self._job_path(job.project_id, job.job_id)
        _write_atomic(path, raw)
        _write_atomic(path.with_suffix(".json.sha256"), f"{digest}\n".encode("ascii"))
        job.record_sha256 = digest

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
                    if job.status in _ACTIVE_JOB_STATUSES:
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
        return recovered

    @staticmethod
    def _validate_loaded_profile(
        profile: Mapping[str, Any], project_id: UUID
    ) -> None:
        if set(profile) != {
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
        }:
            raise ValueError("pipeline profile fields are invalid")
        if (
            profile.get("schema_version") != PIPELINE_PROFILE_SCHEMA
            or profile.get("project_id") != str(project_id)
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
        _validate_safe_json(value, "pipeline job")
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
        if job is None or job.project_id != project_id:
            raise PipelineJobNotFound(
                "The pipeline job was not found in this project"
            )
        return job

    def _evict_locked(self) -> None:
        if len(self._jobs) < MAX_PIPELINE_JOBS:
            return
        for job_id, job in tuple(self._jobs.items()):
            if job.status in _TERMINAL_JOB_STATUSES:
                self._jobs.pop(job_id, None)
                if len(self._jobs) < MAX_PIPELINE_JOBS:
                    return

    @staticmethod
    def _validate_project_id(project_id: UUID) -> None:
        if not isinstance(project_id, UUID):
            raise ValueError("project_id must be a UUID")


class BridgePipelineRuntime:
    """Honest default bridge adapter for currently implemented local stages.

    Intake, SHA-256, metadata preflight and 2fps dense indexing are real.
    High-frequency rescan/DINO/OCR/SAM2 are capability-gated until their
    dedicated local runtime is installed. A capture-start coordinate never
    becomes a route.
    """

    def __init__(
        self,
        media_intake: MediaIntakeService,
        *,
        data_root: Path,
        ffmpeg_path: Optional[str] = None,
        ffprobe_path: Optional[str] = None,
        vision_status: Optional[Callable[[], Mapping[str, Any]]] = None,
        analysis_jobs: Optional[AnalysisJobService] = None,
        command_proposal_generator: Optional[
            CommandProposalGenerator
        ] = None,
        experimental_proposals: Optional[
            ExperimentalProposalStore
        ] = None,
        candidate_labels: Optional[CandidateLabelStore] = None,
        weak_case_catalog: Optional[WeakCaseCatalog] = None,
        derived_outputs: Optional[DerivedOutputsService] = None,
    ) -> None:
        self._media_intake = media_intake
        self._data_root = Path(data_root).resolve()
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._vision_status = vision_status or (
            lambda: {
                "enabled": False,
                "warning_code": "model_pack_not_configured",
            }
        )
        self._analysis_jobs = analysis_jobs
        self._command_proposal_generator = command_proposal_generator
        self._experimental_proposals = experimental_proposals
        self._candidate_labels = candidate_labels
        self._weak_case_catalog = weak_case_catalog
        self._derived_outputs = derived_outputs
        self._active_hash_jobs: dict[str, UUID] = {}
        self._active_analysis_jobs: dict[str, UUID] = {}
        self._lock = threading.RLock()

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
        vision = dict(self._vision_status())
        command_status = (
            dict(self._command_proposal_generator.status())
            if self._command_proposal_generator is not None
            else {
                "state": "unavailable",
                "error_code": "model_pack_not_configured",
            }
        )
        capabilities = set(vision.get("capabilities") or ())
        installed_pack_id = vision.get("pack_id")
        detector_matches = (
            isinstance(installed_pack_id, str)
            and installed_pack_id == profile["detector_pack_id"]
        )
        ocr_matches = (
            isinstance(installed_pack_id, str)
            and installed_pack_id == profile["ocr_pack_id"]
        )
        sam2_matches = (
            isinstance(installed_pack_id, str)
            and installed_pack_id == profile["sam2_pack_id"]
        )
        return {
            "intake": "ready",
            "sha256": "ready",
            "dense_index_2fps": "ready",
            "candidate_rescan_5_10fps": (
                "ready"
                if detector_matches
                and "candidate_rescan_5_10fps" in capabilities
                else "missing"
            ),
            "detector": (
                "ready"
                if detector_matches and "detection" in capabilities
                else "missing"
            ),
            "ocr": (
                "ready"
                if ocr_matches and "ocr" in capabilities
                else "missing"
            ),
            "sam2_segmentation_tracking": (
                "ready"
                if sam2_matches
                and {"segmentation", "tracking"}.issubset(capabilities)
                else "missing"
            ),
            "similar_case_search": "missing",
            "vision_proposal": "missing",
            "dino_review_only_boxes": (
                "ready"
                if (
                    command_status.get("state") == "ready"
                    and command_status.get("review_only") is True
                    and command_status.get("bulk_frame_detection_provider")
                    == "local_model"
                    and self._analysis_jobs is not None
                    and self._experimental_proposals is not None
                )
                else "missing"
            ),
            "dino_review_only_pack_id": command_status.get("pack_id"),
            "dino_review_only_pack_version": command_status.get(
                "pack_version"
            ),
            "dino_review_only_manifest_sha256": command_status.get(
                "manifest_sha256"
            ),
            "dino_review_only_error_code": command_status.get("error_code"),
            "selected_report_provider": profile["report_provider"],
            "bulk_frame_provider": "local_models_only",
            "installed_vision_pack_id": installed_pack_id,
            "requested_detector_pack_id": profile["detector_pack_id"],
            "requested_ocr_pack_id": profile["ocr_pack_id"],
            "requested_sam2_pack_id": profile["sam2_pack_id"],
            "model_pack_warning_code": vision.get("warning_code"),
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
            probe = self._media_intake.probe(
                project_id, snapshot_id, file_name, 15
            )
            return PipelineStageResult(
                "success",
                checkpoint={
                    "snapshot_digest": probe["snapshot_digest"],
                    "duration_seconds": probe["duration_seconds"],
                    "size_bytes": probe["size_bytes"],
                    "ffprobe_status": probe["ffprobe_status"],
                },
                metrics={
                    "duration_seconds": probe["duration_seconds"],
                    "video_stream_count": len(probe["video_streams"]),
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
            hash_job_id = sha.get("hash_job_id")
            if (
                not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
                or not isinstance(hash_job_id, str)
            ):
                raise PipelineStageFailure("hash_checkpoint_missing")
            grant = self._media_intake.resolve_completed_media(
                project_id,
                snapshot_id,
                file_name,
                UUID(hash_job_id),
                digest,
            )
            dense_project_root = (
                self._data_root
                / "projects"
                / str(project_id)
                / "pipeline"
                / "dense"
            )
            output_root = (
                dense_project_root
                / str(context["job_id"])
            )
            output_root.mkdir(parents=True, exist_ok=True)
            limits = DenseFrameIndexLimits(sampling_fps=2)
            engine = DenseFrameIndexEngine(
                ffmpeg_path=self._ffmpeg_path,
                ffprobe_path=self._ffprobe_path,
                limits=limits,
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
            except DenseFrameIndexError as error:
                raise PipelineStageFailure("dense_index_failed") from error
            run_id = Path(result.output_directory).name
            return PipelineStageResult(
                "success",
                checkpoint={
                    "dense_run_id": run_id,
                    "source_sha256": digest,
                    "sampling_fps": 2,
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
                context["capabilities"].get("candidate_rescan_5_10fps")
                == "ready"
            ):
                return PipelineStageResult(
                    "model_unavailable",
                    error_code="candidate_rescan_runtime_unavailable",
                )
            if (
                context["capabilities"].get("dino_review_only_boxes")
                != "ready"
                or self._analysis_jobs is None
            ):
                return PipelineStageResult(
                    "model_unavailable",
                    error_code="candidate_rescan_model_unavailable",
                )
            return self._prepare_degraded_review_candidates(
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
            # A DINO box is useful review evidence, but it is not a SAM2 mask
            # or track. Continue only as an explicitly degraded question path.
            return PipelineStageResult(
                "success",
                checkpoint={
                    "state": "model_unavailable",
                    "mask_generated": False,
                    "tracking_performed": False,
                    "region_edit_requires_sam2": True,
                    "review_only": True,
                },
                metrics={"mask_count": 0, "track_count": 0},
                error_code="sam2_model_unavailable",
            )
        if stage == "temporal_merge":
            return self._merge_dino_review_proposals(
                project_id, context
            )
        if stage == "similar_case_search":
            return self._search_weak_reference_cases(context)
        if stage == "vision_proposal":
            detection = checkpoints.get("detection_ocr", {})
            proposal_count = detection.get("proposal_candidate_count")
            if (
                isinstance(proposal_count, bool)
                or not isinstance(proposal_count, int)
                or proposal_count < 0
            ):
                raise PipelineStageFailure("dino_checkpoint_missing")
            return PipelineStageResult(
                "success",
                checkpoint={
                    "proposal_source": "grounding_dino_review_sidecar",
                    "proposal_candidate_count": proposal_count,
                    "llm_bulk_frame_detection_used": False,
                    "structured_llm_semantics_status": "not_run",
                    "review_only": True,
                    "automatic_labeling_allowed": False,
                },
                metrics={"proposal_candidate_count": proposal_count},
                error_code="structured_llm_not_run",
            )
        if stage == "question_queue":
            return self._prepare_question_queue(context)
        if stage == "derived_outputs":
            return self._generate_derived_outputs(context, cancel)
        raise PipelineStageFailure("unknown_pipeline_stage")

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

        matching = None
        for row in self._analysis_jobs.list(project_id, limit=128):
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
            try:
                matching = self._analysis_jobs.start(
                    project_id,
                    snapshot_id,
                    file_name,
                    hash_uuid,
                    digest,
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
                    matching = self._analysis_jobs.start(
                        project_id,
                        snapshot_id,
                        file_name,
                        hash_uuid,
                        digest,
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
        candidates = (
            result.get("candidates")
            if isinstance(result, Mapping)
            else None
        )
        if (
            row.get("status") != "completed"
            or row.get("source_sha256") != expected_sha256
            or not isinstance(candidates, list)
            or not candidates
        ):
            raise PipelineStageFailure("analysis_checkpoint_invalid")
        return analysis_job_id, row, candidates

    def _generate_dino_review_proposals(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
        cancel: Callable[[], bool],
    ) -> PipelineStageResult:
        if (
            self._command_proposal_generator is None
            or self._experimental_proposals is None
            or self._analysis_jobs is None
        ):
            return PipelineStageResult(
                "model_unavailable",
                error_code="dino_review_runtime_unavailable",
            )
        status = dict(self._command_proposal_generator.status())
        if (
            status.get("state") != "ready"
            or status.get("review_only") is not True
            or status.get("bulk_frame_detection_provider")
            != "local_model"
        ):
            return PipelineStageResult(
                "model_unavailable",
                error_code="dino_review_model_unavailable",
            )
        analysis_job_id, row, candidates = self._analysis_job_for_context(
            project_id, context
        )
        evaluation_id = (
            f"pipeline_dino_v1_{str(context['job_id']).replace('-', '')}"
        )
        existing = self._experimental_proposals.latest_for_job(
            project_id,
            analysis_job_id,
            evaluation_id=evaluation_id,
        )
        if existing:
            issue_types = sorted(
                {
                    prediction["issue_type"]
                    for proposal in existing.values()
                    for prediction in proposal.get("predictions", ())
                    if isinstance(prediction, Mapping)
                    and isinstance(
                        prediction.get("issue_type"), str
                    )
                }
            )
            return PipelineStageResult(
                "success",
                checkpoint={
                    "analysis_job_id": str(analysis_job_id),
                    "source_sha256": row["source_sha256"],
                    "evaluation_id": evaluation_id,
                    "proposal_candidate_count": len(existing),
                    "prediction_count": sum(
                        len(item.get("predictions", ()))
                        for item in existing.values()
                    ),
                    "detected_issue_types": issue_types,
                    "dino_review_only": True,
                    "dedicated_detector_status": "model_unavailable",
                    "ocr_status": "model_unavailable",
                    "automatic_labeling_allowed": False,
                },
                metrics={
                    "candidate_count": len(candidates),
                    "proposal_candidate_count": len(existing),
                    "cached": True,
                },
                error_code="dedicated_detector_ocr_unavailable",
            )

        proposals_by_candidate: dict[str, list[dict[str, Any]]] = {}
        model = None
        evaluation = None
        evaluated_issue_types: list[str] = []
        for issue_type in _DINO_REVIEW_ONLY_ISSUE_TYPES:
            if cancel():
                return PipelineStageResult(
                    "cancelled", error_code="dino_review_cancelled"
                )
            command_plan = compile_labeling_command(
                f"TrafficSuperVision pipeline review {issue_type}",
                explicit_issue_type=issue_type,
            )
            recipe_id = hashlib.sha256(
                _canonical_json(
                    {
                        "pipeline_job_id": str(context["job_id"]),
                        "project_id": str(project_id),
                        "analysis_job_id": str(analysis_job_id),
                        "issue_type": issue_type,
                        "prompt_sha256": command_plan[
                            "prompt_sha256"
                        ],
                    }
                )
            ).hexdigest()
            try:
                generated = self._command_proposal_generator.generate(
                    project_id=project_id,
                    analysis_job_id=analysis_job_id,
                    recipe_id=recipe_id,
                    command_plan=command_plan,
                    candidates=candidates,
                    resolve_image=(
                        lambda resolved_project_id,
                        resolved_job_id,
                        candidate_id: self._analysis_jobs.resolve_artifact(
                            resolved_project_id,
                            resolved_job_id,
                            candidate_id,
                            "image",
                        )
                    ),
                )
            except (
                CommandProposalGenerationError,
                HTTPException,
            ) as error:
                raise PipelineStageFailure(
                    "dino_review_generation_failed"
                ) from error
            if not generated.available or not generated.generated:
                return PipelineStageResult(
                    "model_unavailable",
                    error_code="dino_review_model_unavailable",
                )
            evaluated_issue_types.append(issue_type)
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
                proposals_by_candidate.setdefault(
                    proposal["candidate_id"], []
                ).extend(dict(item) for item in predictions)

        if model is None or evaluation is None:
            raise PipelineStageFailure("dino_review_result_invalid")
        record_rows = []
        detected_issue_types: set[str] = set()
        prediction_count = 0
        for candidate in candidates:
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
            prediction_count += len(predictions)
            detected_issue_types.update(
                str(item["issue_type"]) for item in predictions
            )
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

        return PipelineStageResult(
            "success",
            checkpoint={
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": row["source_sha256"],
                "evaluation_id": evaluation_id,
                "evaluated_issue_types": evaluated_issue_types,
                "proposal_candidate_count": len(record_rows),
                "prediction_count": prediction_count,
                "detected_issue_types": sorted(
                    detected_issue_types
                ),
                "dino_review_only": True,
                "dino_box_proposal_status": (
                    "completed"
                    if record_rows
                    else "zero_box_proposals"
                ),
                "dedicated_detector_status": "model_unavailable",
                "dedicated_detector_missing_types": list(
                    _DINO_UNSUPPORTED_DEDICATED_TYPES
                ),
                "ocr_status": "model_unavailable",
                "automatic_labeling_allowed": False,
                "annotation_count": 0,
                "incident_count": 0,
            },
            metrics={
                "candidate_count": len(candidates),
                "proposal_candidate_count": len(record_rows),
                "prediction_count": prediction_count,
                "evaluated_issue_type_count": len(
                    evaluated_issue_types
                ),
            },
            error_code=(
                "dedicated_detector_ocr_unavailable"
                if record_rows
                else "dino_zero_box_proposals"
            ),
        )

    def _merge_dino_review_proposals(
        self,
        project_id: UUID,
        context: Mapping[str, Any],
    ) -> PipelineStageResult:
        if self._experimental_proposals is None:
            raise PipelineStageFailure(
                "experimental_proposal_store_unavailable"
            )
        analysis_job_id, row, candidates = self._analysis_job_for_context(
            project_id, context
        )
        detection_checkpoint = context.get("checkpoints", {}).get(
            "detection_ocr", {}
        )
        evaluation_id = detection_checkpoint.get("evaluation_id")
        if not isinstance(evaluation_id, str):
            raise PipelineStageFailure("dino_checkpoint_missing")
        proposals_by_candidate = (
            self._experimental_proposals.latest_for_job(
                project_id,
                analysis_job_id,
                evaluation_id=evaluation_id,
            )
        )
        candidate_by_id = {
            str(candidate["candidate_id"]): candidate
            for candidate in candidates
        }
        detections = []
        for candidate_id, proposal in proposals_by_candidate.items():
            candidate = candidate_by_id.get(candidate_id)
            predictions = proposal.get("predictions")
            if (
                candidate is None
                or not isinstance(predictions, list)
                or not predictions
            ):
                continue
            prediction = predictions[0]
            box = prediction.get("box")
            score = prediction.get("model_score")
            issue_type = prediction.get("issue_type")
            timestamp = candidate.get(
                "representative_timestamp_seconds"
            )
            if (
                not isinstance(box, Mapping)
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not isinstance(issue_type, str)
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
            ):
                raise PipelineStageFailure(
                    "dino_review_result_invalid"
                )
            detections.append(
                {
                    "detection_id": candidate_id,
                    "timestamp_seconds": float(timestamp),
                    "issue_type": issue_type,
                    "score": float(score),
                    "normalized_box": dict(box),
                    "score_semantics": (
                        "model_score_not_probability"
                    ),
                    "review_only": True,
                    "automatic_labeling_allowed": False,
                }
            )
        try:
            temporal = (
                group_temporal_incident_proposals(
                    detections,
                    provenance={
                        "project_id": str(project_id),
                        "analysis_job_id": str(analysis_job_id),
                        "source_file_sha256": row["source_sha256"],
                        "pipeline_job_id": str(context["job_id"]),
                        "evaluation_id": evaluation_id,
                    },
                    policy=TemporalGroupingPolicy(),
                )
                if detections
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
            temporal,
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
        question_candidate_ids = []
        for candidate_id in candidate_order:
            representative = insertion_by_candidate.get(candidate_id)
            if representative is not None:
                question_candidate_ids.append(representative)
            if candidate_id not in grouped_candidate_ids:
                question_candidate_ids.append(candidate_id)
        if (
            not question_candidate_ids
            or len(question_candidate_ids)
            != len(set(question_candidate_ids))
        ):
            raise PipelineStageFailure("temporal_merge_result_invalid")
        revision = hashlib.sha256(
            _canonical_json(
                {
                    "analysis_job_id": str(analysis_job_id),
                    "evaluation_id": evaluation_id,
                    "question_candidate_ids": question_candidate_ids,
                    "groups": group_summaries,
                }
            )
        ).hexdigest()
        return PipelineStageResult(
            "success",
            checkpoint={
                "analysis_job_id": str(analysis_job_id),
                "source_sha256": row["source_sha256"],
                "evaluation_id": evaluation_id,
                "temporal_merge_revision": revision,
                "question_candidate_ids": question_candidate_ids,
                "groups": group_summaries,
                "review_only": True,
                "automatic_incident_creation_allowed": False,
                "automatic_annotation_allowed": False,
            },
            metrics={
                "input_detection_count": len(detections),
                "temporal_group_count": len(group_summaries),
                "question_count": len(question_candidate_ids),
                "incident_count": 0,
                "annotation_count": 0,
            },
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
        source_state = "not_applicable"
        try:
            for issue_type in sorted(set(issue_types)):
                if issue_type == "other":
                    continue
                result = self._weak_case_catalog.search(
                    issue_type=str(issue_type),
                    query_text=None,
                    limit=3,
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
                            "reference_state": (
                                "weak_unconfirmed"
                            ),
                            "review_required": True,
                        }
                    )
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
                "approved_reference_count": 0,
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

    def _prepare_question_queue(
        self, context: Mapping[str, Any]
    ) -> PipelineStageResult:
        temporal = context.get("checkpoints", {}).get(
            "temporal_merge", {}
        )
        candidate_ids = temporal.get("question_candidate_ids")
        analysis_job_id = temporal.get("analysis_job_id")
        source_sha256 = temporal.get("source_sha256")
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
        ):
            raise PipelineStageFailure(
                "question_queue_checkpoint_invalid"
            )
        queue_revision = hashlib.sha256(
            _canonical_json(
                {
                    "pipeline_job_id": str(context["job_id"]),
                    "analysis_job_id": analysis_job_id,
                    "source_sha256": source_sha256,
                    "question_candidate_ids": candidate_ids,
                    "review_mode": context["profile"]["review_mode"],
                    "question_policy": context["profile"][
                        "question_policy"
                    ],
                }
            )
        ).hexdigest()
        return PipelineStageResult(
            "success",
            checkpoint={
                "analysis_job_id": analysis_job_id,
                "source_sha256": source_sha256,
                "question_count": len(candidate_ids),
                "question_candidate_ids": candidate_ids,
                "question_queue_revision": queue_revision,
                "review_only": True,
                "automatic_labeling_allowed": False,
                "annotation_count": 0,
                "incident_count": 0,
                "capability_missing": [
                    "dedicated_road_detector",
                    "high_frequency_candidate_rescan",
                    "korean_ocr",
                    "sam2_segmentation_tracking",
                    "structured_vision_llm",
                ],
                "dino_box_proposals_are_final_truth": False,
            },
            metrics={
                "question_count": len(candidate_ids),
                "annotation_count": 0,
                "incident_count": 0,
            },
            error_code="degraded_review_capabilities",
        )

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
        review = context.get("review_completion")
        if (
            not isinstance(checkpoints, Mapping)
            or not isinstance(profile, Mapping)
            or not isinstance(review, Mapping)
        ):
            raise PipelineStageFailure(
                "derived_outputs_checkpoint_invalid"
            )
        question = checkpoints.get("question_queue")
        sha = checkpoints.get("sha256")
        if not isinstance(question, Mapping) or not isinstance(sha, Mapping):
            raise PipelineStageFailure(
                "derived_outputs_checkpoint_invalid"
            )

        candidate_ids = question.get("question_candidate_ids")
        analysis_job_id = question.get("analysis_job_id")
        question_source_sha256 = question.get("source_sha256")
        question_queue_revision = question.get(
            "question_queue_revision"
        )
        source_sha256 = sha.get("source_sha256")
        review_completion_id = context.get("review_completion_id")
        answer_ledger_sha256 = review.get(
            "server_answer_ledger_revision"
        )
        input_fingerprint = context.get("input_fingerprint")
        profile_sha256 = context.get("profile_sha256")
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
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
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or question_source_sha256 != source_sha256
            or not isinstance(review_completion_id, str)
            or review.get("review_completion_id")
            != review_completion_id
            or review.get("analysis_job_id") != analysis_job_id
            or review.get("question_queue_revision")
            != question_queue_revision
            or review.get("all_required_questions_answered") is not True
            or isinstance(review.get("answered_question_count"), bool)
            or review.get("answered_question_count") != len(candidate_ids)
            or not isinstance(answer_ledger_sha256, str)
            or _SHA256.fullmatch(answer_ledger_sha256) is None
            or review_completion_id
            != f"review:{answer_ledger_sha256}"
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
                question_candidate_ids=tuple(candidate_ids),
                template_id=str(profile["default_template_id"]),
                template_sha256=str(
                    profile["default_template_sha256"]
                ),
                report_provider=str(profile["report_provider"]),
                # Consent and exact semantic-template readiness do not yet
                # exist in the durable project profile. Never infer them.
                report_transmission_consent=False,
                template_semantic_map_ready=False,
                report_provider_ready=False,
                output_location_id=str(profile["output_location_id"]),
                # A generic pipeline resume must replay the same bundle. A
                # future explicit report retry may supply its own attempt.
                report_attempt=0,
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
        try:
            result = self._derived_outputs.generate(
                request,
                cancelled=cancel,
            )
        except DerivedOutputsConflict as error:
            raise PipelineStageFailure(
                "derived_outputs_provenance_conflict"
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

        checkpoint = dict(result.checkpoint)
        metrics = {
            **dict(result.metrics),
            "idempotent_replay": result.replayed,
        }
        if checkpoint.get("state") == "partial":
            return PipelineStageResult(
                "partial",
                checkpoint=checkpoint,
                metrics=metrics,
                error_code="derived_outputs_partial",
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
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or len(candidate_ids) != len(set(candidate_ids))
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
        ledger_sha256 = hashlib.sha256(
            "\n".join(sorted(proofs)).encode("utf-8")
        ).hexdigest()
        expected_completion_id = f"review:{ledger_sha256}"
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
