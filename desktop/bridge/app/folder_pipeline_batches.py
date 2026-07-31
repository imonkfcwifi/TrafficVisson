from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import shutil
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional, Protocol
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .media_intake import MediaIntakeService
from .dense_frame_index import (
    DenseFrameIndexLimits,
    build_dense_frame_capacity_plan,
    dense_frame_index_requires_short_staging,
)
from .project_artifact_roots import (
    DEFAULT_OUTPUT_LOCATION_ID,
    ProjectArtifactRootError,
    ProjectArtifactRootNotFound,
    ProjectArtifactRootRegistry,
)


SCHEMA_VERSION = "trafficvisson.folder-pipeline-batch.v5"
LEGACY_SCHEMA_VERSION = "trafficvisson.folder-pipeline-batch.v4"
OLDER_LEGACY_SCHEMA_VERSION = "trafficvisson.folder-pipeline-batch.v3"
OLDEST_LEGACY_SCHEMA_VERSION = (
    "trafficvisson.folder-pipeline-batch.v2"
)
MAX_BATCHES = 128
MAX_BATCH_FILES = 128
MAX_RECORD_BYTES = 1024 * 1024
DEFAULT_DENSE_FPS = 2
ESTIMATED_DENSE_JPEG_BYTES = 112 * 1024
DERIVED_OUTPUT_OVERHEAD_RATIO = 1.0
MINIMUM_OUTPUT_RESERVE_BYTES = 4 * 1024**3
CAPACITY_MODEL_VERSION = "trafficvisson.pipeline-capacity-plan.v2"
AVERAGE_FRAME_BYTES_CEILING = 256 * 1024
MAX_DENSE_FRAMES = 30_000
MAX_RESCAN_FRAMES = 30_000
MAX_RESCAN_WINDOW_SECONDS = 60 * 60
MAX_REVIEW_CANDIDATES = 512
PLANNED_CANDIDATE_WINDOW_RATIO_BASIS_POINTS = 1_500
PLANNED_CANDIDATE_DENSITY_SECONDS = 60
MINIMUM_PLANNED_CANDIDATES = 4
MAXIMUM_PLANNED_WINDOW_SECONDS = 15 * 60
MAX_DENSE_OUTPUT_BYTES = 16 * 1024**3
MAX_RESCAN_OUTPUT_BYTES = 8 * 1024**3
MAX_CANDIDATE_EVIDENCE_BYTES = 8 * 1024**3
MAX_DERIVED_EVIDENCE_BYTES = 8 * 1024**3
DERIVED_STRUCTURED_OVERHEAD_BYTES = 512 * 1024**2
MAX_PROVIDER_IMAGE_BYTES = 5 * 1024**2
PROVIDER_IMAGES_PER_CANDIDATE = 6
MAX_REPORT_IMAGE_BYTES = 16 * 1024**2
PLANNED_CANDIDATE_EVIDENCE_BYTES = 8 * 1024**2
PLANNED_PROVIDER_IMAGE_BYTES = 1 * 1024**2
PLANNED_REPORT_IMAGE_BYTES = 2 * 1024**2
PLANNED_DERIVED_EVIDENCE_BYTES = 8 * 1024**2
STAGE_METADATA_RESERVE_BYTES = 64 * 1024**2
SEPARATE_CAPACITY_PREFLIGHTS = (
    "project_model_training",
    "hwp_com_application_and_48_page_render",
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{15,127}$")
_PENDING_ENTRY_STATES = frozenset(
    {"pending_validation", "validating", "validated", "enqueueing"}
)


class FolderBatchError(RuntimeError):
    pass


class FolderBatchNotFound(FolderBatchError):
    pass


class FolderBatchConflict(FolderBatchError):
    pass


class FolderBatchUnavailable(FolderBatchError):
    pass


class FolderBatchStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    snapshot_id: UUID
    rights_confirmed: Literal[True]
    idempotency_key: str = Field(min_length=16, max_length=128)

    @field_validator("rights_confirmed", mode="before")
    @classmethod
    def explicit_rights_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("rights_confirmed must be explicitly true")
        return value


class FolderBatchRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    user_confirmed: Literal[True]

    @field_validator("user_confirmed", mode="before")
    @classmethod
    def explicit_retry_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("user_confirmed must be explicitly true")
        return value


class _PipelineJobs(Protocol):
    def get_profile(self, project_id: UUID) -> dict[str, Any]: ...

    def start(
        self,
        project_id: UUID,
        *,
        snapshot_id: UUID,
        file_name: str,
        rights_confirmed: bool,
        idempotency_key: str,
        storage_plan: Optional[Mapping[str, Any]] = None,
    ) -> tuple[dict[str, Any], bool]: ...

    def get(self, project_id: UUID, job_id: UUID) -> dict[str, Any]: ...

    def resume(self, project_id: UUID, job_id: UUID) -> dict[str, Any]: ...

    def dense_staging_status(self) -> dict[str, Any]: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _storage_stage(
    stage: str,
    *,
    categories: tuple[str, ...],
    planned_frame_count: int = 0,
    average_bytes_per_frame_ceiling: int = 0,
    fixed_bytes: int = 0,
    hard_ceiling_bytes: Optional[int] = None,
) -> dict[str, Any]:
    estimated = (
        planned_frame_count * average_bytes_per_frame_ceiling
        + fixed_bytes
    )
    if (
        hard_ceiling_bytes is not None
        and estimated > hard_ceiling_bytes
    ):
        raise ValueError(
            f"{stage} planned storage exceeds its absolute engine cap"
        )
    return {
        "stage": stage,
        "categories": list(categories),
        "planned_frame_count": planned_frame_count,
        "average_bytes_per_frame_ceiling": (
            average_bytes_per_frame_ceiling
        ),
        "fixed_bytes": fixed_bytes,
        "hard_ceiling_bytes": hard_ceiling_bytes,
        "estimated_bytes": estimated,
    }


def build_pipeline_storage_plan(
    *,
    duration_seconds: float,
    candidate_rescan_fps: int,
) -> dict[str, Any]:
    """Return a conservative, bounded and path-free per-video storage plan."""

    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
        or duration_seconds <= 0
        or isinstance(candidate_rescan_fps, bool)
        or not isinstance(candidate_rescan_fps, int)
        or not 5 <= candidate_rescan_fps <= 10
    ):
        raise ValueError("pipeline storage-plan input is invalid")
    dense_frames = min(
        MAX_DENSE_FRAMES,
        math.ceil(float(duration_seconds) * DEFAULT_DENSE_FPS),
    )
    planned_window_seconds = min(
        float(duration_seconds),
        float(MAXIMUM_PLANNED_WINDOW_SECONDS),
        max(
            0.5,
            float(duration_seconds)
            * PLANNED_CANDIDATE_WINDOW_RATIO_BASIS_POINTS
            / 10_000,
        ),
    )
    rescan_frames = min(
        MAX_RESCAN_FRAMES,
        max(1, math.ceil(planned_window_seconds * candidate_rescan_fps)),
    )
    candidate_count = min(
        MAX_REVIEW_CANDIDATES,
        max(
            MINIMUM_PLANNED_CANDIDATES,
            math.ceil(
                float(duration_seconds)
                / PLANNED_CANDIDATE_DENSITY_SECONDS
            ),
        ),
    )
    stages = [
        _storage_stage(
            "intake_validation",
            categories=("intake-metadata",),
            fixed_bytes=STAGE_METADATA_RESERVE_BYTES,
        ),
        _storage_stage(
            "sha256",
            categories=("source-provenance",),
            fixed_bytes=STAGE_METADATA_RESERVE_BYTES,
        ),
        _storage_stage(
            "metadata_gps_preflight",
            categories=("metadata-gps",),
            fixed_bytes=STAGE_METADATA_RESERVE_BYTES,
        ),
        _storage_stage(
            "dense_index_2fps",
            categories=("dense",),
            planned_frame_count=dense_frames,
            average_bytes_per_frame_ceiling=(
                AVERAGE_FRAME_BYTES_CEILING
            ),
            fixed_bytes=STAGE_METADATA_RESERVE_BYTES,
            hard_ceiling_bytes=MAX_DENSE_OUTPUT_BYTES,
        ),
        _storage_stage(
            "candidate_rescan_5_10fps",
            categories=(
                "candidate-window-rescans",
                "candidate-evidence",
            ),
            planned_frame_count=rescan_frames,
            average_bytes_per_frame_ceiling=(
                AVERAGE_FRAME_BYTES_CEILING
            ),
            fixed_bytes=(
                STAGE_METADATA_RESERVE_BYTES
                + candidate_count
                * PLANNED_CANDIDATE_EVIDENCE_BYTES
            ),
            hard_ceiling_bytes=(
                STAGE_METADATA_RESERVE_BYTES
                + MAX_RESCAN_OUTPUT_BYTES
                + MAX_CANDIDATE_EVIDENCE_BYTES
            ),
        ),
        _storage_stage(
            "detection_ocr",
            categories=("detection-ocr",),
            fixed_bytes=(
                STAGE_METADATA_RESERVE_BYTES
                + candidate_count * 256 * 1024
            ),
        ),
        _storage_stage(
            "sam2_segmentation_tracking",
            categories=("sam2-masks",),
            planned_frame_count=min(candidate_count, 16) * 12,
            average_bytes_per_frame_ceiling=1024**2,
            fixed_bytes=STAGE_METADATA_RESERVE_BYTES,
        ),
        _storage_stage(
            "temporal_merge",
            categories=("temporal-incidents",),
            fixed_bytes=STAGE_METADATA_RESERVE_BYTES,
        ),
        _storage_stage(
            "similar_case_search",
            categories=("similar-case-search",),
            fixed_bytes=STAGE_METADATA_RESERVE_BYTES,
        ),
        _storage_stage(
            "vision_proposal",
            categories=("provider-visual-evidence",),
            fixed_bytes=(
                STAGE_METADATA_RESERVE_BYTES
                + candidate_count
                * PROVIDER_IMAGES_PER_CANDIDATE
                * PLANNED_PROVIDER_IMAGE_BYTES
            ),
            hard_ceiling_bytes=(
                STAGE_METADATA_RESERVE_BYTES
                + MAX_REVIEW_CANDIDATES
                * PROVIDER_IMAGES_PER_CANDIDATE
                * MAX_PROVIDER_IMAGE_BYTES
            ),
        ),
        _storage_stage(
            "question_queue",
            categories=("question-evidence",),
            fixed_bytes=STAGE_METADATA_RESERVE_BYTES,
        ),
        _storage_stage(
            "derived_outputs",
            categories=(
                "derived-outputs",
                "report-deidentified-evidence",
            ),
            fixed_bytes=(
                candidate_count
                * PLANNED_DERIVED_EVIDENCE_BYTES
                + DERIVED_STRUCTURED_OVERHEAD_BYTES
                + candidate_count * PLANNED_REPORT_IMAGE_BYTES
            ),
            hard_ceiling_bytes=(
                MAX_DERIVED_EVIDENCE_BYTES
                + DERIVED_STRUCTURED_OVERHEAD_BYTES
                + MAX_REVIEW_CANDIDATES * MAX_REPORT_IMAGE_BYTES
            ),
        ),
    ]
    estimated_bytes = sum(
        int(stage["estimated_bytes"]) for stage in stages
    )
    # Hard ceilings are fail-closed bounds, not bytes that every job will
    # consume.  Initial preflight reserves the practical estimate plus the
    # fixed 4 GiB safety floor.  Each immutable exact-stage replan later adds
    # only its newly accepted positive delta through the same-volume atomic
    # reservation ledger.
    safety_reserve_bytes = MINIMUM_OUTPUT_RESERVE_BYTES
    return {
        "schema_version": CAPACITY_MODEL_VERSION,
        "candidate_rescan_fps": candidate_rescan_fps,
        "planning_assumptions": {
            "candidate_window_ratio_basis_points": (
                PLANNED_CANDIDATE_WINDOW_RATIO_BASIS_POINTS
            ),
            "candidate_density_seconds": (
                PLANNED_CANDIDATE_DENSITY_SECONDS
            ),
            "minimum_planned_candidates": (
                MINIMUM_PLANNED_CANDIDATES
            ),
            "maximum_planned_candidates": MAX_REVIEW_CANDIDATES,
            "maximum_planned_window_seconds": (
                MAXIMUM_PLANNED_WINDOW_SECONDS
            ),
        },
        "planned_candidate_window_seconds": (
            round(planned_window_seconds, 6)
        ),
        "planned_candidate_count": candidate_count,
        "dense_frame_count": dense_frames,
        "candidate_rescan_frame_count": rescan_frames,
        "average_frame_bytes_ceiling": (
            AVERAGE_FRAME_BYTES_CEILING
        ),
        "stage_estimates": stages,
        "separate_capacity_preflights": list(
            SEPARATE_CAPACITY_PREFLIGHTS
        ),
        "estimated_artifact_bytes": estimated_bytes,
        "safety_reserve_bytes": safety_reserve_bytes,
        "required_free_bytes": (
            estimated_bytes + safety_reserve_bytes
        ),
    }


def _aggregate_storage_plans(
    plans: list[Mapping[str, Any]],
    *,
    candidate_rescan_fps: int,
) -> dict[str, Any]:
    if not plans:
        raise ValueError("at least one storage plan is required")
    assumptions = dict(plans[0]["planning_assumptions"])
    if any(
        dict(plan["planning_assumptions"]) != assumptions
        for plan in plans
    ):
        raise ValueError("storage-plan assumptions are inconsistent")
    aggregate_stages: list[dict[str, Any]] = []
    for stage_index, stage_name in enumerate(
        tuple(stage["stage"] for stage in plans[0]["stage_estimates"])
    ):
        rows = [
            plan["stage_estimates"][stage_index] for plan in plans
        ]
        if any(row["stage"] != stage_name for row in rows):
            raise ValueError("storage-plan stage order is invalid")
        hard_ceilings = [
            row["hard_ceiling_bytes"]
            for row in rows
            if row["hard_ceiling_bytes"] is not None
        ]
        aggregate_stages.append(
            {
                "stage": stage_name,
                "categories": list(rows[0]["categories"]),
                "planned_frame_count": sum(
                    int(row["planned_frame_count"]) for row in rows
                ),
                "average_bytes_per_frame_ceiling": max(
                    int(row["average_bytes_per_frame_ceiling"])
                    for row in rows
                ),
                "fixed_bytes": sum(
                    int(row["fixed_bytes"]) for row in rows
                ),
                "hard_ceiling_bytes": (
                    sum(int(value) for value in hard_ceilings)
                    if len(hard_ceilings) == len(rows)
                    else None
                ),
                "estimated_bytes": sum(
                    int(row["estimated_bytes"]) for row in rows
                ),
            }
        )
    estimated = sum(
        int(plan["estimated_artifact_bytes"]) for plan in plans
    )
    safety_reserve = max(
        int(plan["safety_reserve_bytes"]) for plan in plans
    )
    return {
        "capacity_model_version": CAPACITY_MODEL_VERSION,
        "candidate_rescan_fps": candidate_rescan_fps,
        "planning_assumptions": assumptions,
        "planned_candidate_window_seconds": round(
            sum(
                float(plan["planned_candidate_window_seconds"])
                for plan in plans
            ),
            6,
        ),
        "planned_candidate_count": sum(
            int(plan["planned_candidate_count"]) for plan in plans
        ),
        "candidate_rescan_frame_count": sum(
            int(plan["candidate_rescan_frame_count"])
            for plan in plans
        ),
        "average_frame_bytes_ceiling": (
            AVERAGE_FRAME_BYTES_CEILING
        ),
        "stage_estimates": aggregate_stages,
        "separate_capacity_preflights": list(
            SEPARATE_CAPACITY_PREFLIGHTS
        ),
        "estimated_artifact_bytes": estimated,
        "safety_reserve_bytes": safety_reserve,
        "required_free_bytes": (
            estimated + safety_reserve
        ),
    }


class FolderPipelineBatchService:
    """Durable all-files orchestration for one approved stable folder."""

    def __init__(
        self,
        media_intake: MediaIntakeService,
        pipeline_jobs: _PipelineJobs,
        *,
        data_root: Path,
        artifact_root_registry: Optional[
            ProjectArtifactRootRegistry
        ] = None,
        probe_timeout_seconds: int = 30,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("folder batch data_root must be absolute")
        self._root = (root / "folder-pipeline-batches").resolve()
        self._output_root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if (
            self._root.is_symlink()
            or not self._root.is_dir()
            or self._root.parent != root.resolve()
        ):
            raise ValueError("folder batch data root is invalid")
        if (
            isinstance(probe_timeout_seconds, bool)
            or not isinstance(probe_timeout_seconds, int)
            or not 1 <= probe_timeout_seconds <= 60
        ):
            raise ValueError("folder batch probe timeout is invalid")
        self._media = media_intake
        self._pipelines = pipeline_jobs
        self._artifact_roots = artifact_root_registry
        self._probe_timeout_seconds = probe_timeout_seconds
        if not callable(disk_usage):
            raise ValueError("folder batch disk usage resolver is invalid")
        self._disk_usage = disk_usage
        self._records: "OrderedDict[UUID, dict[str, Any]]" = OrderedDict()
        self._queue: "queue.Queue[Optional[UUID]]" = queue.Queue(
            maxsize=MAX_BATCHES
        )
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._recovery_summary = {
            "batches_loaded": 0,
            "batches_requeued": 0,
            "records_rejected": 0,
        }
        recovered = self._load_records()
        if recovered:
            with self._lock:
                self._ensure_worker_locked()
                for batch_id in recovered:
                    self._queue.put_nowait(batch_id)

    def start(
        self,
        project_id: UUID,
        *,
        snapshot_id: UUID,
        rights_confirmed: bool,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        if not isinstance(project_id, UUID) or not isinstance(snapshot_id, UUID):
            raise ValueError("folder batch identity is invalid")
        if rights_confirmed is not True:
            raise FolderBatchConflict(
                "Video rights must be confirmed before folder analysis starts"
            )
        if (
            not isinstance(idempotency_key, str)
            or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
        ):
            raise ValueError("folder batch idempotency key is invalid")
        profile = self._pipelines.get_profile(project_id)
        artifact_root_binding: Optional[dict[str, Any]] = None
        if self._artifact_roots is not None:
            try:
                resolved = self._artifact_roots.resolve_profile_binding(
                    project_id,
                    profile_revision=int(profile["revision"]),
                    output_location_id=str(
                        profile["output_location_id"]
                    ),
                )
            except ProjectArtifactRootNotFound:
                if (
                    profile["output_location_id"]
                    != DEFAULT_OUTPUT_LOCATION_ID
                ):
                    raise FolderBatchConflict(
                        "The configured project output location is not registered"
                    ) from None
                resolved = (
                    self._artifact_roots.capture_profile_binding(
                        project_id,
                        profile_revision=int(profile["revision"]),
                        output_location_id=DEFAULT_OUTPUT_LOCATION_ID,
                    )
                )
            except ProjectArtifactRootError as error:
                raise FolderBatchUnavailable(
                    "The configured project output location is unavailable"
                ) from error
            artifact_root_binding = {
                key: resolved.binding[key]
                for key in (
                    "output_location_id",
                    "registry_revision",
                    "root_identity_sha256",
                    "marker_sha256",
                )
            }
        grant = self._media.export_stable_snapshot_grant(
            project_id, snapshot_id
        )
        if not 1 <= len(grant.files) <= MAX_BATCH_FILES:
            raise FolderBatchUnavailable(
                "The stable folder has no supported videos or exceeds the batch limit"
            )
        fingerprint = _payload_hash(
            {
                "project_id": str(project_id),
                "snapshot_id": str(snapshot_id),
                "snapshot_digest": grant.digest,
                "profile_sha256": _payload_hash(profile),
                "files": [
                    {
                        "name": item.name,
                        "size_bytes": item.size_bytes,
                        "modified_time_ns": item.modified_time_ns,
                    }
                    for item in grant.files
                ],
            }
        )
        with self._lock:
            for existing in self._records.values():
                if existing["idempotency_key"] == idempotency_key:
                    if existing["input_fingerprint"] != fingerprint:
                        raise FolderBatchConflict(
                            "The folder batch idempotency key belongs to different input"
                        )
                    return self._serialize_locked(existing), True
                if existing["input_fingerprint"] == fingerprint:
                    return self._serialize_locked(existing), True
            if len(self._records) >= MAX_BATCHES:
                raise FolderBatchUnavailable(
                    "The folder batch registry is full"
                )
            batch_id = uuid4()
            now = _utc_now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "batch_id": str(batch_id),
                "project_id": str(project_id),
                "snapshot_id": str(snapshot_id),
                "snapshot_digest": grant.digest,
                "idempotency_key": idempotency_key,
                "input_fingerprint": fingerprint,
                "profile_revision": int(profile["revision"]),
                "output_location_id": profile["output_location_id"],
                "artifact_root_binding": artifact_root_binding,
                "output_preflight": {
                    "state": "pending",
                    "capacity_model_version": (
                        CAPACITY_MODEL_VERSION
                    ),
                    "dense_sampling_fps": DEFAULT_DENSE_FPS,
                    "candidate_rescan_fps": int(
                        profile.get("candidate_rescan_fps", 8)
                    ),
                    "planning_assumptions": {
                        "candidate_window_ratio_basis_points": (
                            PLANNED_CANDIDATE_WINDOW_RATIO_BASIS_POINTS
                        ),
                        "candidate_density_seconds": (
                            PLANNED_CANDIDATE_DENSITY_SECONDS
                        ),
                        "minimum_planned_candidates": (
                            MINIMUM_PLANNED_CANDIDATES
                        ),
                        "maximum_planned_candidates": (
                            MAX_REVIEW_CANDIDATES
                        ),
                        "maximum_planned_window_seconds": (
                            MAXIMUM_PLANNED_WINDOW_SECONDS
                        ),
                    },
                    "planned_candidate_window_seconds": 0.0,
                    "estimated_jpeg_bytes_per_frame": (
                        ESTIMATED_DENSE_JPEG_BYTES
                    ),
                    "derived_output_overhead_ratio": (
                        DERIVED_OUTPUT_OVERHEAD_RATIO
                    ),
                    "duration_seconds": 0.0,
                    "dense_frame_count": 0,
                    "candidate_rescan_frame_count": 0,
                    "planned_candidate_count": 0,
                    "average_frame_bytes_ceiling": (
                        AVERAGE_FRAME_BYTES_CEILING
                    ),
                    "stage_estimates": [],
                    "separate_capacity_preflights": list(
                        SEPARATE_CAPACITY_PREFLIGHTS
                    ),
                    "estimated_artifact_bytes": 0,
                    "safety_reserve_bytes": (
                        MINIMUM_OUTPUT_RESERVE_BYTES
                    ),
                    "required_free_bytes": 0,
                    "available_free_bytes": None,
                    "error_code": None,
                },
                "enqueue_completed": False,
                "recovered_after_restart": False,
                "retry_count": 0,
                "resume_count": 0,
                "created_at": now,
                "updated_at": now,
                "entries": [
                    {
                        "order": index,
                        "file_name": item.name,
                        "size_bytes": item.size_bytes,
                        "state": "pending_validation",
                        "duration_seconds": None,
                        "estimated_dense_frames": 0,
                        "estimated_output_bytes": 0,
                        "storage_plan": None,
                        "validation_attempts": 0,
                        "enqueue_attempts": 0,
                        "pipeline_job_id": None,
                        "pipeline_idempotency_key": (
                            f"folder-batch-{batch_id.hex}-{index:03d}"
                        ),
                        "idempotent_replay": False,
                        "last_error_code": None,
                        "recovered_after_restart": False,
                    }
                    for index, item in enumerate(grant.files, start=1)
                ],
            }
            self._records[batch_id] = record
            self._persist_locked(record)
            self._ensure_worker_locked()
            self._queue.put_nowait(batch_id)
            return self._serialize_locked(record), False

    def get(self, project_id: UUID, batch_id: UUID) -> dict[str, Any]:
        with self._lock:
            return self._serialize_locked(
                self._record_locked(project_id, batch_id)
            )

    def list(self, project_id: UUID, *, limit: int = 128) -> list[dict[str, Any]]:
        if (
            not isinstance(project_id, UUID)
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_BATCHES
        ):
            raise ValueError("folder batch list request is invalid")
        with self._lock:
            matches = [
                record
                for record in reversed(self._records.values())
                if record["project_id"] == str(project_id)
            ][:limit]
            return [self._serialize_locked(record) for record in matches]

    def retry(
        self,
        project_id: UUID,
        batch_id: UUID,
        *,
        user_confirmed: bool,
    ) -> dict[str, Any]:
        if user_confirmed is not True:
            raise FolderBatchConflict(
                "Folder batch retry must be explicitly confirmed"
            )
        needs_worker = False
        with self._lock:
            record = self._record_locked(project_id, batch_id)
            for entry in record["entries"]:
                if entry["state"] in {
                    "validation_failed",
                    "enqueue_failed",
                }:
                    entry["state"] = "pending_validation"
                    entry["last_error_code"] = None
                    needs_worker = True
                    continue
                if entry["state"] == "storage_blocked":
                    entry["state"] = "validated"
                    entry["last_error_code"] = None
                    needs_worker = True
                    continue
                if entry["pipeline_job_id"] is None:
                    continue
                try:
                    job = self._pipelines.get(
                        project_id, UUID(entry["pipeline_job_id"])
                    )
                except Exception:
                    entry["last_error_code"] = "pipeline_status_unavailable"
                    continue
                if job.get("status") not in {
                    "blocked",
                    "failed",
                    "cancelled",
                }:
                    continue
                try:
                    self._pipelines.resume(
                        project_id, UUID(entry["pipeline_job_id"])
                    )
                except Exception:
                    entry["last_error_code"] = "pipeline_resume_failed"
                else:
                    entry["last_error_code"] = None
                    record["resume_count"] += 1
            record["retry_count"] += 1
            record["enqueue_completed"] = not needs_worker
            if needs_worker:
                record["output_preflight"]["state"] = "pending"
                record["output_preflight"]["error_code"] = None
            record["updated_at"] = _utc_now()
            self._persist_locked(record)
            if needs_worker:
                self._ensure_worker_locked()
                self._queue.put_nowait(batch_id)
            return self._serialize_locked(record)

    def recovery_summary(self) -> dict[str, int]:
        with self._lock:
            return dict(self._recovery_summary)

    def shutdown(self) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        if self._stop_event.is_set():
            raise FolderBatchUnavailable(
                "The folder batch worker is shutting down"
            )
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="trafficvisson-folder-batch",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                batch_id = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if batch_id is None:
                self._queue.task_done()
                return
            try:
                self._process_batch(batch_id)
            except ProjectArtifactRootError:
                self._fail_batch_at_worker_boundary(
                    batch_id,
                    entry_state="storage_blocked",
                    error_code="project_storage_unavailable",
                )
            except Exception:
                self._fail_batch_at_worker_boundary(
                    batch_id,
                    entry_state="enqueue_failed",
                    error_code="batch_processing_failed",
                )
            finally:
                self._queue.task_done()

    def _fail_batch_at_worker_boundary(
        self,
        batch_id: UUID,
        *,
        entry_state: str,
        error_code: str,
    ) -> None:
        """Persist a fail-closed batch state and keep the worker alive."""

        try:
            with self._lock:
                record = self._records.get(batch_id)
                if record is None:
                    return
                record["output_preflight"] = {
                    **record["output_preflight"],
                    "state": "blocked",
                    "error_code": error_code,
                }
                for entry in record["entries"]:
                    if entry["state"] in _PENDING_ENTRY_STATES:
                        entry["state"] = entry_state
                        entry["last_error_code"] = error_code
                record["enqueue_completed"] = True
                record["updated_at"] = _utc_now()
                self._persist_locked(record)
        except Exception:
            # The worker must continue to the next queue item even if the
            # durable volume itself disappeared while recording this error.
            return

    def _process_batch(self, batch_id: UUID) -> None:
        if self._reconcile_existing_pipeline_jobs(batch_id):
            return
        with self._lock:
            record = self._records.get(batch_id)
            if record is None:
                return
            project_id = UUID(record["project_id"])
            snapshot_id = UUID(record["snapshot_id"])
            entry_orders = [
                item["order"]
                for item in record["entries"]
                if item["state"] in {
                    "pending_validation",
                    "validating",
                }
            ]
        for order in entry_orders:
            if self._stop_event.is_set():
                return
            with self._lock:
                record = self._records.get(batch_id)
                if record is None:
                    return
                entry = record["entries"][order - 1]
                if entry["state"] not in {
                    "pending_validation",
                    "validating",
                }:
                    continue
                entry["state"] = "validating"
                entry["validation_attempts"] += 1
                entry["last_error_code"] = None
                record["updated_at"] = _utc_now()
                self._persist_locked(record)
                file_name = entry["file_name"]
            try:
                probe = self._media.probe(
                    project_id,
                    snapshot_id,
                    file_name,
                    self._probe_timeout_seconds,
                )
                duration_seconds = float(probe["duration_seconds"])
                if (
                    not math.isfinite(duration_seconds)
                    or duration_seconds <= 0
                ):
                    raise ValueError("media duration is invalid")
            except Exception:
                with self._lock:
                    record = self._records.get(batch_id)
                    if record is None:
                        return
                    entry = record["entries"][order - 1]
                    entry["state"] = "validation_failed"
                    entry["last_error_code"] = "media_probe_failed"
                    record["updated_at"] = _utc_now()
                    self._persist_locked(record)
                continue
            with self._lock:
                record = self._records.get(batch_id)
                if record is None:
                    return
                entry = record["entries"][order - 1]
                storage_plan = build_pipeline_storage_plan(
                    duration_seconds=duration_seconds,
                    candidate_rescan_fps=int(
                        record["output_preflight"][
                            "candidate_rescan_fps"
                        ]
                    ),
                )
                entry["state"] = "validated"
                entry["duration_seconds"] = duration_seconds
                entry["estimated_dense_frames"] = int(
                    storage_plan["dense_frame_count"]
                )
                entry["estimated_output_bytes"] = (
                    int(storage_plan["estimated_artifact_bytes"])
                )
                entry["storage_plan"] = storage_plan
                entry["last_error_code"] = None
                record["updated_at"] = _utc_now()
                self._persist_locked(record)

        with self._lock:
            record = self._records.get(batch_id)
            if record is None:
                return
            # The durable preflight always describes every successfully
            # planned video. After a restart, however, an already reconciled
            # PipelineJob owns its individual reservation, so the temporary
            # folder hold must cover only the entries still being enqueued.
            reservable_entries = [
                item
                for item in record["entries"]
                if item["state"] in {"validated", "storage_blocked"}
            ]
            planned_entries = [
                item
                for item in record["entries"]
                if isinstance(item.get("storage_plan"), Mapping)
            ]
            if not planned_entries:
                record["output_preflight"] = {
                    **record["output_preflight"],
                    "state": "blocked",
                    "error_code": "no_valid_media",
                }
                record["enqueue_completed"] = True
                record["updated_at"] = _utc_now()
                self._persist_locked(record)
                return
            duration_seconds = sum(
                float(item["duration_seconds"] or 0)
                for item in planned_entries
            )
            storage_plans = [
                item["storage_plan"] for item in planned_entries
            ]
            aggregate_plan = _aggregate_storage_plans(
                list(storage_plans),
                candidate_rescan_fps=int(
                    record["output_preflight"][
                        "candidate_rescan_fps"
                    ]
                ),
            )
            dense_frame_count = sum(
                int(item["estimated_dense_frames"])
                for item in planned_entries
            )
            estimated_artifact_bytes = int(
                aggregate_plan["estimated_artifact_bytes"]
            )
            required_free_bytes = int(
                aggregate_plan["required_free_bytes"]
            )
            reservation_artifact_bytes = sum(
                int(item["storage_plan"]["estimated_artifact_bytes"])
                for item in reservable_entries
            )
            reservation_required_free_bytes = (
                reservation_artifact_bytes
                + int(aggregate_plan["safety_reserve_bytes"])
            )
            dense_limits = DenseFrameIndexLimits(
                sampling_fps=DEFAULT_DENSE_FPS
            )
            dense_staging_required_free_bytes = (
                max(
                    build_dense_frame_capacity_plan(
                        source_sha256="0" * 64,
                        duration_seconds=float(
                            item["duration_seconds"]
                        ),
                        limits=dense_limits,
                    ).planned_output_bytes_upper_bound
                    for item in reservable_entries
                )
                + int(dense_limits.min_free_disk_bytes)
                if reservable_entries
                else 0
            )
            output_location_id = record["output_location_id"]
            record["output_preflight"] = {
                "state": "computing",
                "capacity_model_version": (
                    aggregate_plan["capacity_model_version"]
                ),
                "dense_sampling_fps": DEFAULT_DENSE_FPS,
                "candidate_rescan_fps": (
                    aggregate_plan["candidate_rescan_fps"]
                ),
                "planning_assumptions": aggregate_plan[
                    "planning_assumptions"
                ],
                "planned_candidate_window_seconds": (
                    aggregate_plan[
                        "planned_candidate_window_seconds"
                    ]
                ),
                "estimated_jpeg_bytes_per_frame": (
                    ESTIMATED_DENSE_JPEG_BYTES
                ),
                "derived_output_overhead_ratio": (
                    DERIVED_OUTPUT_OVERHEAD_RATIO
                ),
                "duration_seconds": duration_seconds,
                "dense_frame_count": dense_frame_count,
                "candidate_rescan_frame_count": (
                    aggregate_plan[
                        "candidate_rescan_frame_count"
                    ]
                ),
                "planned_candidate_count": (
                    aggregate_plan["planned_candidate_count"]
                ),
                "average_frame_bytes_ceiling": (
                    aggregate_plan[
                        "average_frame_bytes_ceiling"
                    ]
                ),
                "stage_estimates": aggregate_plan[
                    "stage_estimates"
                ],
                "separate_capacity_preflights": (
                    aggregate_plan[
                        "separate_capacity_preflights"
                    ]
                ),
                "estimated_artifact_bytes": estimated_artifact_bytes,
                "safety_reserve_bytes": int(
                    aggregate_plan["safety_reserve_bytes"]
                ),
                "required_free_bytes": required_free_bytes,
                "available_free_bytes": None,
                "error_code": None,
            }
            record["updated_at"] = _utc_now()
            self._persist_locked(record)

        available_free_bytes: Optional[int] = None
        reserved_by_other_batches = 0
        preflight_error: Optional[str] = None
        prospective_project_root: Optional[Path] = None
        if (
            self._artifact_roots is not None
            and isinstance(
                record.get("artifact_root_binding"), dict
            )
        ):
            try:
                resolved = self._artifact_roots.resolve(
                    project_id,
                    output_location_id=output_location_id,
                    expected_binding=record[
                        "artifact_root_binding"
                    ],
                )
                available_free_bytes = (
                    resolved.available_free_bytes
                )
                prospective_project_root = resolved.root
                if reservation_artifact_bytes:
                    reservation = (
                        self._artifact_roots
                        .adjust_capacity_reservation(
                            project_id,
                            record["artifact_root_binding"],
                            reservation_id=(
                                f"folder-batch:{batch_id}"
                            ),
                            reserved_bytes=reservation_artifact_bytes,
                            safety_reserve_bytes=int(
                                aggregate_plan["safety_reserve_bytes"]
                            ),
                        )
                    )
                    reserved_by_other_batches = int(
                        reservation["other_reserved_bytes"]
                    )
                else:
                    self._artifact_roots.release_capacity_reservation(
                        project_id,
                        record["artifact_root_binding"],
                        reservation_id=(
                            f"folder-batch:{batch_id}"
                        ),
                    )
            except ProjectArtifactRootError:
                preflight_error = (
                    "insufficient_project_storage"
                )
        elif output_location_id != DEFAULT_OUTPUT_LOCATION_ID:
            preflight_error = "project_output_location_unavailable"
        else:
            prospective_project_root = self._output_root
            try:
                available_free_bytes = int(
                    self._disk_usage(self._output_root).free
                )
            except (OSError, TypeError, ValueError):
                preflight_error = "project_storage_unavailable"
            else:
                with self._lock:
                    reserved_by_other_batches = (
                        self._reserved_artifact_bytes_locked(
                            batch_id, record
                        )
                    )
                if (
                    max(
                        0,
                        available_free_bytes
                        - reserved_by_other_batches,
                    )
                    < reservation_required_free_bytes
                ):
                    preflight_error = "insufficient_project_storage"

        if prospective_project_root is not None:
            prospective_dense_root = (
                prospective_project_root
                / "pipeline-jobs"
                / "00000000-0000-0000-0000-000000000000"
                / "dense"
            )
            staging_required = (
                dense_frame_index_requires_short_staging(
                    prospective_dense_root
                )
            )
            if staging_required and reservable_entries:
                status_resolver = getattr(
                    self._pipelines,
                    "dense_staging_status",
                    None,
                )
                raw_staging_status = (
                    status_resolver()
                    if callable(status_resolver)
                    else {
                        "state": "unavailable",
                        "source": "not_configured",
                        "available_free_bytes": 0,
                        "error_code": (
                            "dense_staging_not_configured"
                        ),
                        "path_exposed": False,
                    }
                )
                staging_available = int(
                    raw_staging_status.get(
                        "available_free_bytes",
                        0,
                    )
                )
                staging_error = (
                    raw_staging_status.get("error_code")
                    if raw_staging_status.get("state") != "ready"
                    else (
                        "dense_staging_storage_blocked"
                        if staging_available
                        < dense_staging_required_free_bytes
                        else None
                    )
                )
                if preflight_error is None and staging_error:
                    preflight_error = str(staging_error)

        with self._lock:
            record = self._records.get(batch_id)
            if record is None:
                return
            record["output_preflight"] = {
                **record["output_preflight"],
                "state": "blocked" if preflight_error else "ready",
                "available_free_bytes": available_free_bytes,
                "error_code": preflight_error,
            }
            if preflight_error is not None:
                if (
                    self._artifact_roots is not None
                    and isinstance(
                        record.get("artifact_root_binding"),
                        Mapping,
                    )
                ):
                    try:
                        self._artifact_roots.release_capacity_reservation(
                            project_id,
                            record["artifact_root_binding"],
                            reservation_id=(
                                f"folder-batch:{batch_id}"
                            ),
                        )
                    except ProjectArtifactRootError:
                        pass
                for entry in record["entries"]:
                    if entry["state"] in {
                        "validated",
                        "storage_blocked",
                    }:
                        entry["state"] = "storage_blocked"
                        entry["last_error_code"] = preflight_error
                record["enqueue_completed"] = True
                record["updated_at"] = _utc_now()
                self._persist_locked(record)
                return
            enqueue_orders = [
                item["order"]
                for item in record["entries"]
                if item["state"] in {"validated", "enqueueing"}
            ]
            for entry in record["entries"]:
                if entry["state"] == "storage_blocked":
                    entry["state"] = "validated"
                    entry["last_error_code"] = None
            record["updated_at"] = _utc_now()
            self._persist_locked(record)

        batch_start = getattr(
            self._pipelines, "start_batch_pinned", None
        )
        if callable(batch_start) and enqueue_orders:
            with self._lock:
                record = self._records.get(batch_id)
                if record is None:
                    return
                for order in enqueue_orders:
                    entry = record["entries"][order - 1]
                    entry["state"] = "enqueueing"
                    entry["enqueue_attempts"] += 1
                    entry["last_error_code"] = None
                record["updated_at"] = _utc_now()
                self._persist_locked(record)
                pinned_inputs = [
                    {
                        "snapshot_id": snapshot_id,
                        "file_name": record["entries"][
                            order - 1
                        ]["file_name"],
                        "idempotency_key": record["entries"][
                            order - 1
                        ]["pipeline_idempotency_key"],
                        **(
                            {
                                "storage_plan": record["entries"][
                                    order - 1
                                ]["storage_plan"]
                            }
                            if getattr(
                                self._pipelines,
                                "supports_storage_plan",
                                False,
                            )
                            is True
                            else {}
                        ),
                    }
                    for order in enqueue_orders
                ]
                profile_revision = int(
                    record["profile_revision"]
                )
                artifact_root_binding = record.get(
                    "artifact_root_binding"
                )
                capacity_reservation_id = (
                    f"folder-batch:{batch_id}"
                )
            try:
                batch_kwargs = {
                    "inputs": pinned_inputs,
                    "rights_confirmed": True,
                    "expected_profile_revision": profile_revision,
                    "expected_artifact_root_binding": (
                        artifact_root_binding
                    ),
                }
                if (
                    getattr(
                        self._pipelines,
                        "supports_capacity_reservations",
                        False,
                    )
                    is True
                ):
                    batch_kwargs[
                        "capacity_reservation_parent_id"
                    ] = capacity_reservation_id
                results = batch_start(
                    project_id,
                    **batch_kwargs,
                )
                if len(results) != len(enqueue_orders):
                    raise ValueError(
                        "pipeline batch start result count changed"
                    )
            except Exception:
                with self._lock:
                    record = self._records.get(batch_id)
                    if record is None:
                        return
                    for order in enqueue_orders:
                        entry = record["entries"][order - 1]
                        entry["state"] = "enqueue_failed"
                        entry["last_error_code"] = (
                            "pipeline_profile_binding_changed"
                        )
                    record["enqueue_completed"] = True
                    record["updated_at"] = _utc_now()
                    self._persist_locked(record)
                if (
                    self._artifact_roots is not None
                    and isinstance(
                        artifact_root_binding, Mapping
                    )
                ):
                    try:
                        self._artifact_roots.release_capacity_reservation(
                            project_id,
                            artifact_root_binding,
                            reservation_id=capacity_reservation_id,
                        )
                    except ProjectArtifactRootError:
                        pass
                return
            with self._lock:
                record = self._records.get(batch_id)
                if record is None:
                    return
                for order, result in zip(
                    enqueue_orders, results
                ):
                    pipeline_job, replayed = result
                    entry = record["entries"][order - 1]
                    entry["state"] = "enqueued"
                    entry["pipeline_job_id"] = str(
                        UUID(str(pipeline_job["job_id"]))
                    )
                    entry["idempotent_replay"] = bool(replayed)
                    entry["last_error_code"] = None
                record["enqueue_completed"] = True
                record["updated_at"] = _utc_now()
                self._persist_locked(record)
            return

        for order in enqueue_orders:
            if self._stop_event.is_set():
                return
            with self._lock:
                record = self._records.get(batch_id)
                if record is None:
                    return
                entry = record["entries"][order - 1]
                if entry["state"] not in {"validated", "enqueueing"}:
                    continue
                entry["state"] = "enqueueing"
                entry["enqueue_attempts"] += 1
                entry["last_error_code"] = None
                record["updated_at"] = _utc_now()
                self._persist_locked(record)
                file_name = entry["file_name"]
                pipeline_key = entry["pipeline_idempotency_key"]
                storage_plan = entry["storage_plan"]
            try:
                start_kwargs = {
                    "snapshot_id": snapshot_id,
                    "file_name": file_name,
                    "rights_confirmed": True,
                    "idempotency_key": pipeline_key,
                }
                if (
                    getattr(
                        self._pipelines,
                        "supports_storage_plan",
                        False,
                    )
                    is True
                ):
                    start_kwargs["storage_plan"] = storage_plan
                if (
                    getattr(
                        self._pipelines,
                        "supports_capacity_reservations",
                        False,
                    )
                    is True
                ):
                    start_kwargs[
                        "capacity_reservation_parent_id"
                    ] = f"folder-batch:{batch_id}"
                pipeline_job, replayed = self._pipelines.start(
                    project_id, **start_kwargs
                )
                pipeline_job_id = UUID(str(pipeline_job["job_id"]))
            except Exception:
                with self._lock:
                    record = self._records.get(batch_id)
                    if record is None:
                        return
                    entry = record["entries"][order - 1]
                    entry["state"] = "enqueue_failed"
                    entry["last_error_code"] = "pipeline_enqueue_failed"
                    record["updated_at"] = _utc_now()
                    self._persist_locked(record)
                continue
            with self._lock:
                record = self._records.get(batch_id)
                if record is None:
                    return
                entry = record["entries"][order - 1]
                entry["state"] = "enqueued"
                entry["pipeline_job_id"] = str(pipeline_job_id)
                entry["idempotent_replay"] = bool(replayed)
                entry["last_error_code"] = None
                record["updated_at"] = _utc_now()
                self._persist_locked(record)
        with self._lock:
            record = self._records.get(batch_id)
            if record is None:
                return
            record["enqueue_completed"] = not any(
                entry["state"] in _PENDING_ENTRY_STATES
                for entry in record["entries"]
            )
            record["updated_at"] = _utc_now()
            self._persist_locked(record)

    def _reconcile_existing_pipeline_jobs(
        self,
        batch_id: UUID,
    ) -> bool:
        """Close the crash window between PipelineJob start and batch persist."""

        lister = getattr(self._pipelines, "list", None)
        if not callable(lister):
            return False
        with self._lock:
            record = self._records.get(batch_id)
            if record is None:
                return True
            project_id = UUID(str(record["project_id"]))
            snapshot_id = str(record["snapshot_id"])
            expected = {
                str(entry["pipeline_idempotency_key"]): (
                    int(entry["order"]),
                    str(entry["file_name"]),
                )
                for entry in record["entries"]
                if entry["state"] != "enqueued"
            }
        if not expected:
            return False
        try:
            existing_rows = lister(project_id, limit=MAX_BATCH_FILES)
        except Exception:
            return False
        if not isinstance(existing_rows, list):
            return False
        matches: dict[int, Mapping[str, Any]] = {}
        for row in existing_rows:
            if not isinstance(row, Mapping):
                continue
            key = row.get("idempotency_key")
            expected_entry = expected.get(str(key))
            if expected_entry is None:
                continue
            order, file_name = expected_entry
            if (
                str(row.get("snapshot_id")) != snapshot_id
                or row.get("file_name") != file_name
            ):
                continue
            try:
                UUID(str(row["job_id"]))
            except (KeyError, TypeError, ValueError):
                continue
            matches[order] = row
        if not matches:
            return False
        with self._lock:
            record = self._records.get(batch_id)
            if record is None:
                return True
            for order, row in matches.items():
                entry = record["entries"][order - 1]
                if (
                    entry["pipeline_idempotency_key"]
                    != row.get("idempotency_key")
                    or entry["file_name"] != row.get("file_name")
                ):
                    continue
                entry["state"] = "enqueued"
                entry["pipeline_job_id"] = str(
                    UUID(str(row["job_id"]))
                )
                entry["idempotent_replay"] = True
                entry["last_error_code"] = None
            all_enqueued = all(
                entry["state"] == "enqueued"
                for entry in record["entries"]
            )
            if all_enqueued:
                record["enqueue_completed"] = True
            record["updated_at"] = _utc_now()
            self._persist_locked(record)
            binding = record.get("artifact_root_binding")
        if (
            all_enqueued
            and self._artifact_roots is not None
            and isinstance(binding, Mapping)
        ):
            try:
                self._artifact_roots.release_capacity_reservation(
                    project_id,
                    binding,
                    reservation_id=f"folder-batch:{batch_id}",
                )
            except ProjectArtifactRootError:
                pass
        return all_enqueued

    def _reserved_artifact_bytes_locked(
        self,
        current_batch_id: UUID,
        current_record: Mapping[str, Any],
    ) -> int:
        """Conservatively reserve active batch output on the same root.

        The worker is single-threaded, but a second batch can otherwise see
        unchanged filesystem free space immediately after the first batch was
        enqueued and overcommit it before dense extraction starts.
        """

        current_binding = current_record.get(
            "artifact_root_binding"
        )
        if (
            self._artifact_roots is not None
            and isinstance(current_binding, Mapping)
        ):
            current_scope: tuple[str, str] = (
                "volume",
                self._artifact_roots.reservation_scope(
                    UUID(str(current_record["project_id"])),
                    current_binding,
                ),
            )
        else:
            current_scope = (
                "location",
                str(current_record.get("output_location_id")),
            )
        reserved = 0
        for other_id, other in self._records.items():
            if other_id == current_batch_id:
                continue
            other_binding = other.get("artifact_root_binding")
            if (
                self._artifact_roots is not None
                and isinstance(other_binding, Mapping)
            ):
                try:
                    other_scope = (
                        "volume",
                        self._artifact_roots.reservation_scope(
                            UUID(str(other["project_id"])),
                            other_binding,
                        ),
                    )
                except ProjectArtifactRootError:
                    # Same opaque root is conservatively treated as sharing
                    # the current volume even while revalidation is degraded.
                    other_scope = (
                        current_scope
                        if other.get("output_location_id")
                        == current_record.get(
                            "output_location_id"
                        )
                        else ("unavailable", str(other_id))
                    )
            else:
                other_scope = (
                    "location",
                    str(other.get("output_location_id")),
                )
            if other_scope != current_scope:
                continue
            preflight = other.get("output_preflight")
            if (
                not isinstance(preflight, Mapping)
                or preflight.get("state") != "ready"
                or isinstance(
                    preflight.get("estimated_artifact_bytes"),
                    bool,
                )
                or not isinstance(
                    preflight.get("estimated_artifact_bytes"), int
                )
            ):
                continue
            active = False
            for entry in other.get("entries", []):
                if not isinstance(entry, Mapping):
                    continue
                job_id = entry.get("pipeline_job_id")
                if job_id is None:
                    if entry.get("state") in _PENDING_ENTRY_STATES:
                        active = True
                    continue
                try:
                    status = self._pipelines.get(
                        UUID(str(other["project_id"])),
                        UUID(str(job_id)),
                    ).get("status")
                except Exception:
                    # A durable job may still be recovering. Releasing its
                    # reservation on a transient lookup failure is unsafe.
                    active = True
                    continue
                if status not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    active = True
            if active:
                reserved += int(
                    preflight["estimated_artifact_bytes"]
                )
        return reserved

    def _serialize_locked(self, record: dict[str, Any]) -> dict[str, Any]:
        project_id = UUID(record["project_id"])
        entries: list[dict[str, Any]] = []
        counts = {
            "total": len(record["entries"]),
            "pending": 0,
            "validation_failed": 0,
            "enqueue_failed": 0,
            "storage_blocked": 0,
            "queued_or_running": 0,
            "awaiting_review": 0,
            "completed": 0,
            "pipeline_failed": 0,
        }
        for item in record["entries"]:
            pipeline_status = None
            if item["pipeline_job_id"] is not None:
                try:
                    job = self._pipelines.get(
                        project_id, UUID(item["pipeline_job_id"])
                    )
                    pipeline_status = str(job["status"])
                except Exception:
                    pipeline_status = "unavailable"
            if item["state"] in _PENDING_ENTRY_STATES:
                counts["pending"] += 1
            elif item["state"] == "validation_failed":
                counts["validation_failed"] += 1
            elif item["state"] == "enqueue_failed":
                counts["enqueue_failed"] += 1
            elif item["state"] == "storage_blocked":
                counts["storage_blocked"] += 1
            if pipeline_status in {"queued", "running", "cancelling"}:
                counts["queued_or_running"] += 1
            elif pipeline_status == "awaiting_review":
                counts["awaiting_review"] += 1
            elif pipeline_status == "completed":
                counts["completed"] += 1
            elif pipeline_status in {
                "blocked",
                "failed",
                "cancelled",
                "unavailable",
            }:
                counts["pipeline_failed"] += 1
            entries.append(
                {
                    "order": item["order"],
                    "file_name": item["file_name"],
                    "size_bytes": item["size_bytes"],
                    "intake_state": item["state"],
                    "duration_seconds": item["duration_seconds"],
                    "estimated_dense_frames": item[
                        "estimated_dense_frames"
                    ],
                    "estimated_output_bytes": item[
                        "estimated_output_bytes"
                    ],
                    "validation_attempts": item["validation_attempts"],
                    "enqueue_attempts": item["enqueue_attempts"],
                    "pipeline_job_id": item["pipeline_job_id"],
                    "pipeline_status": pipeline_status,
                    "idempotent_replay": item["idempotent_replay"],
                    "last_error_code": item["last_error_code"],
                    "recovered_after_restart": item[
                        "recovered_after_restart"
                    ],
                    "retry_available": (
                        item["state"]
                        in {
                            "validation_failed",
                            "enqueue_failed",
                            "storage_blocked",
                        }
                        or pipeline_status
                        in {"blocked", "failed", "cancelled", "unavailable"}
                    ),
                    "resume_available": pipeline_status
                    in {"blocked", "failed", "cancelled"},
                }
            )
        failure_count = (
            counts["validation_failed"]
            + counts["enqueue_failed"]
            + counts["storage_blocked"]
            + counts["pipeline_failed"]
        )
        if counts["storage_blocked"]:
            batch_status = "blocked_storage"
        elif counts["pending"]:
            batch_status = "enqueuing"
        elif counts["awaiting_review"]:
            batch_status = (
                "partial_failed" if failure_count else "awaiting_review"
            )
        elif counts["queued_or_running"]:
            batch_status = "partial_failed" if failure_count else "active"
        elif counts["completed"] == counts["total"]:
            batch_status = "completed"
        elif failure_count == counts["total"]:
            batch_status = "failed"
        elif failure_count:
            batch_status = "partial_failed"
        else:
            batch_status = "queued"
        return {
            "schema_version": SCHEMA_VERSION,
            "batch_id": record["batch_id"],
            "project_id": record["project_id"],
            "snapshot_id": record["snapshot_id"],
            "snapshot_digest": record["snapshot_digest"],
            "idempotency_key": record["idempotency_key"],
            "input_fingerprint": record["input_fingerprint"],
            "output_location_id": record["output_location_id"],
            "output_preflight": dict(record["output_preflight"]),
            "status": batch_status,
            "enqueue_completed": record["enqueue_completed"],
            "recovered_after_restart": record["recovered_after_restart"],
            "retry_count": record["retry_count"],
            "resume_count": record["resume_count"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "counts": counts,
            "files": entries,
        }

    def _record_locked(
        self, project_id: UUID, batch_id: UUID
    ) -> dict[str, Any]:
        if not isinstance(project_id, UUID) or not isinstance(batch_id, UUID):
            raise ValueError("folder batch identity is invalid")
        record = self._records.get(batch_id)
        if record is None or record["project_id"] != str(project_id):
            raise FolderBatchNotFound(
                "The folder pipeline batch was not found in this project"
            )
        return record

    def _persist_locked(self, record: dict[str, Any]) -> None:
        project_root = self._project_root(
            UUID(record["project_id"]), create=True
        )
        batch_id = UUID(record["batch_id"])
        payload = dict(record)
        encoded = _canonical_json(
            {
                **payload,
                "record_sha256": _payload_hash(payload),
            }
        )
        if len(encoded) > MAX_RECORD_BYTES:
            raise FolderBatchUnavailable(
                "The folder batch record is too large"
            )
        temporary = project_root / f".{batch_id}.{os.getpid()}.tmp"
        temporary.write_bytes(encoded)
        os.replace(temporary, project_root / f"{batch_id}.json")

    def _load_records(self) -> list[UUID]:
        recovered: list[UUID] = []
        for project_root in sorted(self._root.iterdir(), key=lambda p: p.name):
            if not project_root.is_dir() or project_root.is_symlink():
                continue
            try:
                project_id = UUID(project_root.name)
            except ValueError:
                continue
            for record_path in sorted(
                project_root.glob("*.json"), key=lambda p: p.name
            ):
                try:
                    record = self._read_record(project_id, record_path)
                except (OSError, ValueError):
                    self._recovery_summary["records_rejected"] += 1
                    continue
                batch_id = UUID(record["batch_id"])
                pending = False
                for entry in record["entries"]:
                    entry["recovered_after_restart"] = True
                    if entry["state"] in {"validating", "enqueueing"}:
                        entry["state"] = "pending_validation"
                        pending = True
                    elif entry["state"] in {
                        "pending_validation",
                        "validated",
                    }:
                        pending = True
                record["recovered_after_restart"] = True
                record["resume_count"] += 1
                record["updated_at"] = _utc_now()
                if pending:
                    record["enqueue_completed"] = False
                self._records[batch_id] = record
                self._recovery_summary["batches_loaded"] += 1
                self._persist_locked(record)
                if pending:
                    recovered.append(batch_id)
                    self._recovery_summary["batches_requeued"] += 1
        return recovered

    def _read_record(
        self, project_id: UUID, record_path: Path
    ) -> dict[str, Any]:
        if (
            record_path.is_symlink()
            or record_path.parent.resolve()
            != self._project_root(project_id, create=False)
            or record_path.stat().st_size > MAX_RECORD_BYTES
        ):
            raise ValueError("folder batch record is invalid")
        raw = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "record_sha256" not in raw:
            raise ValueError("folder batch record is invalid")
        record_sha256 = raw.pop("record_sha256")
        if (
            not isinstance(record_sha256, str)
            or _payload_hash(raw) != record_sha256
            or raw.get("schema_version")
            not in {
                OLDEST_LEGACY_SCHEMA_VERSION,
                OLDER_LEGACY_SCHEMA_VERSION,
                LEGACY_SCHEMA_VERSION,
                SCHEMA_VERSION,
            }
            or raw.get("project_id") != str(project_id)
        ):
            raise ValueError("folder batch record integrity failed")
        batch_id = UUID(str(raw.get("batch_id")))
        if record_path.stem != str(batch_id):
            raise ValueError("folder batch filename is invalid")
        schema_version = raw["schema_version"]
        if schema_version == OLDEST_LEGACY_SCHEMA_VERSION:
            raw["artifact_root_binding"] = None
        if schema_version in {
            OLDEST_LEGACY_SCHEMA_VERSION,
            OLDER_LEGACY_SCHEMA_VERSION,
            LEGACY_SCHEMA_VERSION,
        }:
            self._migrate_capacity_record(project_id, raw)
        expected_keys = {
            "schema_version",
            "batch_id",
            "project_id",
            "snapshot_id",
            "snapshot_digest",
            "idempotency_key",
            "input_fingerprint",
            "profile_revision",
            "output_location_id",
            "artifact_root_binding",
            "output_preflight",
            "enqueue_completed",
            "recovered_after_restart",
            "retry_count",
            "resume_count",
            "created_at",
            "updated_at",
            "entries",
        }
        if set(raw) != expected_keys:
            raise ValueError("folder batch record fields are invalid")
        UUID(str(raw["snapshot_id"]))
        if (
            re.fullmatch(r"[a-f0-9]{64}", str(raw["snapshot_digest"])) is None
            or re.fullmatch(r"[a-f0-9]{64}", str(raw["input_fingerprint"]))
            is None
            or _IDEMPOTENCY_KEY.fullmatch(str(raw["idempotency_key"])) is None
            or isinstance(raw["profile_revision"], bool)
            or not isinstance(raw["profile_revision"], int)
            or raw["profile_revision"] < 1
            or not isinstance(raw["output_location_id"], str)
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                raw["output_location_id"],
            )
            is None
            or not isinstance(raw["enqueue_completed"], bool)
            or not isinstance(raw["recovered_after_restart"], bool)
            or isinstance(raw["retry_count"], bool)
            or not isinstance(raw["retry_count"], int)
            or not 0 <= raw["retry_count"] <= 1_000_000
            or isinstance(raw["resume_count"], bool)
            or not isinstance(raw["resume_count"], int)
            or not 0 <= raw["resume_count"] <= 1_000_000
            or not isinstance(raw["created_at"], str)
            or not isinstance(raw["updated_at"], str)
            or not isinstance(raw["entries"], list)
            or not 1 <= len(raw["entries"]) <= MAX_BATCH_FILES
        ):
            raise ValueError("folder batch record values are invalid")
        binding = raw["artifact_root_binding"]
        if binding is not None and (
            not isinstance(binding, dict)
            or set(binding)
            != {
                "output_location_id",
                "registry_revision",
                "root_identity_sha256",
                "marker_sha256",
            }
            or binding.get("output_location_id")
            != raw["output_location_id"]
            or isinstance(binding.get("registry_revision"), bool)
            or not isinstance(
                binding.get("registry_revision"), int
            )
            or binding["registry_revision"] < 1
            or any(
                not isinstance(binding.get(key), str)
                or re.fullmatch(
                    r"[a-f0-9]{64}", binding[key]
                )
                is None
                for key in {
                    "root_identity_sha256",
                    "marker_sha256",
                }
            )
        ):
            raise ValueError(
                "folder batch artifact-root binding is invalid"
            )
        preflight = raw["output_preflight"]
        if (
            not isinstance(preflight, dict)
            or set(preflight)
            != {
                "state",
                "capacity_model_version",
                "dense_sampling_fps",
                "candidate_rescan_fps",
                "planning_assumptions",
                "planned_candidate_window_seconds",
                "estimated_jpeg_bytes_per_frame",
                "derived_output_overhead_ratio",
                "duration_seconds",
                "dense_frame_count",
                "candidate_rescan_frame_count",
                "planned_candidate_count",
                "average_frame_bytes_ceiling",
                "stage_estimates",
                "separate_capacity_preflights",
                "estimated_artifact_bytes",
                "safety_reserve_bytes",
                "required_free_bytes",
                "available_free_bytes",
                "error_code",
            }
            or preflight["state"]
            not in {"pending", "computing", "ready", "blocked"}
            or preflight["capacity_model_version"]
            != CAPACITY_MODEL_VERSION
            or isinstance(preflight["dense_sampling_fps"], bool)
            or preflight["dense_sampling_fps"] != DEFAULT_DENSE_FPS
            or isinstance(preflight["candidate_rescan_fps"], bool)
            or not isinstance(preflight["candidate_rescan_fps"], int)
            or not 5 <= preflight["candidate_rescan_fps"] <= 10
            or preflight["planning_assumptions"]
            != {
                "candidate_window_ratio_basis_points": (
                    PLANNED_CANDIDATE_WINDOW_RATIO_BASIS_POINTS
                ),
                "candidate_density_seconds": (
                    PLANNED_CANDIDATE_DENSITY_SECONDS
                ),
                "minimum_planned_candidates": (
                    MINIMUM_PLANNED_CANDIDATES
                ),
                "maximum_planned_candidates": (
                    MAX_REVIEW_CANDIDATES
                ),
                "maximum_planned_window_seconds": (
                    MAXIMUM_PLANNED_WINDOW_SECONDS
                ),
            }
            or isinstance(
                preflight["planned_candidate_window_seconds"], bool
            )
            or not isinstance(
                preflight["planned_candidate_window_seconds"],
                (int, float),
            )
            or not math.isfinite(
                float(
                    preflight[
                        "planned_candidate_window_seconds"
                    ]
                )
            )
            or preflight["planned_candidate_window_seconds"] < 0
            or isinstance(
                preflight["estimated_jpeg_bytes_per_frame"], bool
            )
            or preflight["estimated_jpeg_bytes_per_frame"]
            != ESTIMATED_DENSE_JPEG_BYTES
            or isinstance(
                preflight["derived_output_overhead_ratio"], bool
            )
            or not isinstance(
                preflight["derived_output_overhead_ratio"], (int, float)
            )
            or not math.isfinite(
                float(preflight["derived_output_overhead_ratio"])
            )
            or preflight["derived_output_overhead_ratio"]
            != DERIVED_OUTPUT_OVERHEAD_RATIO
            or isinstance(preflight["duration_seconds"], bool)
            or not isinstance(preflight["duration_seconds"], (int, float))
            or not math.isfinite(float(preflight["duration_seconds"]))
            or preflight["duration_seconds"] < 0
            or any(
                isinstance(preflight[key], bool)
                or not isinstance(preflight[key], int)
                or preflight[key] < 0
                for key in {
                    "dense_frame_count",
                    "candidate_rescan_frame_count",
                    "planned_candidate_count",
                    "average_frame_bytes_ceiling",
                    "estimated_artifact_bytes",
                    "safety_reserve_bytes",
                    "required_free_bytes",
                }
            )
            or preflight["average_frame_bytes_ceiling"]
            != AVERAGE_FRAME_BYTES_CEILING
            or not isinstance(preflight["stage_estimates"], list)
            or len(preflight["stage_estimates"])
            not in {0, 12}
            or preflight["separate_capacity_preflights"]
            != list(SEPARATE_CAPACITY_PREFLIGHTS)
            or (
                preflight["available_free_bytes"] is not None
                and (
                    isinstance(preflight["available_free_bytes"], bool)
                    or not isinstance(
                        preflight["available_free_bytes"], int
                    )
                    or preflight["available_free_bytes"] < 0
                )
            )
            or preflight["error_code"]
            not in {
                None,
                "no_valid_media",
                "insufficient_project_storage",
                "project_storage_unavailable",
                "project_output_location_unavailable",
                "batch_processing_failed",
                "dense_staging_not_configured",
                "dense_staging_root_unavailable",
                "dense_staging_status_failed",
                "dense_staging_storage_blocked",
                "dense_staging_usage_unavailable",
            }
        ):
            raise ValueError("folder batch output preflight is invalid")
        expected_entry_keys = {
            "order",
            "file_name",
            "size_bytes",
            "state",
            "duration_seconds",
            "estimated_dense_frames",
            "estimated_output_bytes",
            "storage_plan",
            "validation_attempts",
            "enqueue_attempts",
            "pipeline_job_id",
            "pipeline_idempotency_key",
            "idempotent_replay",
            "last_error_code",
            "recovered_after_restart",
        }
        for index, entry in enumerate(raw["entries"], start=1):
            pipeline_job_id = entry.get("pipeline_job_id") if isinstance(
                entry, dict
            ) else None
            if pipeline_job_id is not None:
                try:
                    UUID(str(pipeline_job_id))
                except ValueError as error:
                    raise ValueError(
                        "folder batch pipeline identity is invalid"
                    ) from error
            if (
                not isinstance(entry, dict)
                or set(entry) != expected_entry_keys
                or entry["order"] != index
                or isinstance(entry["order"], bool)
                or not isinstance(entry["file_name"], str)
                or Path(entry["file_name"]).name != entry["file_name"]
                or not 1 <= len(entry["file_name"]) <= 255
                or Path(entry["file_name"]).suffix.casefold()
                not in {".mp4", ".mov"}
                or isinstance(entry["size_bytes"], bool)
                or not isinstance(entry["size_bytes"], int)
                or entry["size_bytes"] <= 0
                or (
                    entry["duration_seconds"] is not None
                    and (
                        isinstance(entry["duration_seconds"], bool)
                        or not isinstance(
                            entry["duration_seconds"], (int, float)
                        )
                        or not math.isfinite(
                            float(entry["duration_seconds"])
                        )
                        or entry["duration_seconds"] <= 0
                    )
                )
                or isinstance(entry["estimated_dense_frames"], bool)
                or not isinstance(entry["estimated_dense_frames"], int)
                or entry["estimated_dense_frames"] < 0
                or isinstance(entry["estimated_output_bytes"], bool)
                or not isinstance(entry["estimated_output_bytes"], int)
                or entry["estimated_output_bytes"] < 0
                or (
                    entry["storage_plan"] is not None
                    and not isinstance(
                        entry["storage_plan"], Mapping
                    )
                )
                or entry["state"]
                not in {
                    "pending_validation",
                    "validating",
                    "validated",
                    "validation_failed",
                    "enqueueing",
                    "enqueue_failed",
                    "storage_blocked",
                    "enqueued",
                }
                or isinstance(entry["validation_attempts"], bool)
                or not isinstance(entry["validation_attempts"], int)
                or not 0 <= entry["validation_attempts"] <= 1_000_000
                or isinstance(entry["enqueue_attempts"], bool)
                or not isinstance(entry["enqueue_attempts"], int)
                or not 0 <= entry["enqueue_attempts"] <= 1_000_000
                or (
                    entry["state"] == "enqueued"
                    and pipeline_job_id is None
                )
                or (
                    entry["state"] != "enqueued"
                    and pipeline_job_id is not None
                )
                or (
                    entry["state"]
                    in {"validated", "storage_blocked", "enqueued"}
                    and entry["duration_seconds"] is None
                )
                or not isinstance(
                    entry["pipeline_idempotency_key"], str
                )
                or _IDEMPOTENCY_KEY.fullmatch(
                    entry["pipeline_idempotency_key"]
                )
                is None
                or not isinstance(entry["idempotent_replay"], bool)
                or entry["last_error_code"]
                not in {
                    None,
                    "media_probe_failed",
                    "pipeline_enqueue_failed",
                    "pipeline_status_unavailable",
                    "pipeline_resume_failed",
                    "insufficient_project_storage",
                    "project_storage_unavailable",
                    "project_output_location_unavailable",
                }
                or not isinstance(
                    entry["recovered_after_restart"], bool
                )
            ):
                raise ValueError("folder batch entry is invalid")
            if entry["duration_seconds"] is None:
                if entry["storage_plan"] is not None:
                    raise ValueError(
                        "folder batch storage plan is premature"
                    )
            else:
                expected_plan = build_pipeline_storage_plan(
                    duration_seconds=float(
                        entry["duration_seconds"]
                    ),
                    candidate_rescan_fps=int(
                        preflight["candidate_rescan_fps"]
                    ),
                )
                if entry["storage_plan"] != expected_plan:
                    raise ValueError(
                        "folder batch storage plan is invalid"
                    )
                if (
                    entry["estimated_dense_frames"]
                    != expected_plan["dense_frame_count"]
                    or entry["estimated_output_bytes"]
                    != expected_plan["estimated_artifact_bytes"]
                ):
                    raise ValueError(
                        "folder batch storage estimate drifted"
                    )
        planned_entries = [
            entry["storage_plan"]
            for entry in raw["entries"]
            if isinstance(entry.get("storage_plan"), Mapping)
        ]
        if planned_entries:
            aggregate = _aggregate_storage_plans(
                planned_entries,
                candidate_rescan_fps=int(
                    preflight["candidate_rescan_fps"]
                ),
            )
            for key in (
                "capacity_model_version",
                "candidate_rescan_fps",
                "planning_assumptions",
                "planned_candidate_window_seconds",
                "candidate_rescan_frame_count",
                "planned_candidate_count",
                "average_frame_bytes_ceiling",
                "stage_estimates",
                "separate_capacity_preflights",
                "estimated_artifact_bytes",
                "safety_reserve_bytes",
                "required_free_bytes",
            ):
                if preflight[key] != aggregate[key]:
                    raise ValueError(
                        "folder batch aggregate storage plan drifted"
                    )
            if preflight["dense_frame_count"] != sum(
                int(plan["dense_frame_count"])
                for plan in planned_entries
            ):
                raise ValueError(
                    "folder batch dense-frame estimate drifted"
                )
        return raw

    def _migrate_capacity_record(
        self,
        project_id: UUID,
        raw: dict[str, Any],
    ) -> None:
        """Fail-safe in-place migration from the former dense*2 estimate."""

        try:
            profile = self._pipelines.get_profile(project_id)
        except Exception:
            profile = {}
        candidate_rescan_fps = profile.get(
            "candidate_rescan_fps", 8
        )
        if (
            isinstance(candidate_rescan_fps, bool)
            or not isinstance(candidate_rescan_fps, int)
            or not 5 <= candidate_rescan_fps <= 10
        ):
            raise ValueError(
                "legacy batch candidate-rescan profile is unavailable"
            )
        entries = raw.get("entries")
        preflight = raw.get("output_preflight")
        if not isinstance(entries, list) or not isinstance(
            preflight, Mapping
        ):
            raise ValueError("legacy batch capacity record is invalid")
        plans = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(
                    "legacy batch capacity entry is invalid"
                )
            duration = entry.get("duration_seconds")
            if duration is None:
                entry["storage_plan"] = None
                continue
            plan = build_pipeline_storage_plan(
                duration_seconds=float(duration),
                candidate_rescan_fps=candidate_rescan_fps,
            )
            entry["storage_plan"] = plan
            entry["estimated_dense_frames"] = int(
                plan["dense_frame_count"]
            )
            entry["estimated_output_bytes"] = int(
                plan["estimated_artifact_bytes"]
            )
            plans.append(plan)
        if plans:
            aggregate = _aggregate_storage_plans(
                plans,
                candidate_rescan_fps=candidate_rescan_fps,
            )
            rebuilt = {
                "state": preflight.get("state", "blocked"),
                "capacity_model_version": (
                    CAPACITY_MODEL_VERSION
                ),
                "dense_sampling_fps": DEFAULT_DENSE_FPS,
                "candidate_rescan_fps": candidate_rescan_fps,
                "planning_assumptions": aggregate[
                    "planning_assumptions"
                ],
                "planned_candidate_window_seconds": aggregate[
                    "planned_candidate_window_seconds"
                ],
                "estimated_jpeg_bytes_per_frame": (
                    ESTIMATED_DENSE_JPEG_BYTES
                ),
                "derived_output_overhead_ratio": (
                    DERIVED_OUTPUT_OVERHEAD_RATIO
                ),
                "duration_seconds": sum(
                    float(entry.get("duration_seconds") or 0)
                    for entry in entries
                    if entry.get("storage_plan") is not None
                ),
                "dense_frame_count": sum(
                    int(plan["dense_frame_count"])
                    for plan in plans
                ),
                "candidate_rescan_frame_count": aggregate[
                    "candidate_rescan_frame_count"
                ],
                "planned_candidate_count": aggregate[
                    "planned_candidate_count"
                ],
                "average_frame_bytes_ceiling": aggregate[
                    "average_frame_bytes_ceiling"
                ],
                "stage_estimates": aggregate["stage_estimates"],
                "separate_capacity_preflights": aggregate[
                    "separate_capacity_preflights"
                ],
                "estimated_artifact_bytes": aggregate[
                    "estimated_artifact_bytes"
                ],
                "safety_reserve_bytes": aggregate[
                    "safety_reserve_bytes"
                ],
                "required_free_bytes": aggregate[
                    "required_free_bytes"
                ],
                "available_free_bytes": preflight.get(
                    "available_free_bytes"
                ),
                "error_code": preflight.get("error_code"),
            }
        else:
            rebuilt = {
                "state": preflight.get("state", "pending"),
                "capacity_model_version": (
                    CAPACITY_MODEL_VERSION
                ),
                "dense_sampling_fps": DEFAULT_DENSE_FPS,
                "candidate_rescan_fps": candidate_rescan_fps,
                "planning_assumptions": {
                    "candidate_window_ratio_basis_points": (
                        PLANNED_CANDIDATE_WINDOW_RATIO_BASIS_POINTS
                    ),
                    "candidate_density_seconds": (
                        PLANNED_CANDIDATE_DENSITY_SECONDS
                    ),
                    "minimum_planned_candidates": (
                        MINIMUM_PLANNED_CANDIDATES
                    ),
                    "maximum_planned_candidates": (
                        MAX_REVIEW_CANDIDATES
                    ),
                    "maximum_planned_window_seconds": (
                        MAXIMUM_PLANNED_WINDOW_SECONDS
                    ),
                },
                "planned_candidate_window_seconds": 0.0,
                "estimated_jpeg_bytes_per_frame": (
                    ESTIMATED_DENSE_JPEG_BYTES
                ),
                "derived_output_overhead_ratio": (
                    DERIVED_OUTPUT_OVERHEAD_RATIO
                ),
                "duration_seconds": 0.0,
                "dense_frame_count": 0,
                "candidate_rescan_frame_count": 0,
                "planned_candidate_count": 0,
                "average_frame_bytes_ceiling": (
                    AVERAGE_FRAME_BYTES_CEILING
                ),
                "stage_estimates": [],
                "separate_capacity_preflights": list(
                    SEPARATE_CAPACITY_PREFLIGHTS
                ),
                "estimated_artifact_bytes": 0,
                "safety_reserve_bytes": (
                    MINIMUM_OUTPUT_RESERVE_BYTES
                ),
                "required_free_bytes": 0,
                "available_free_bytes": preflight.get(
                    "available_free_bytes"
                ),
                "error_code": preflight.get("error_code"),
            }
        raw["output_preflight"] = rebuilt
        raw["schema_version"] = SCHEMA_VERSION

    def _project_root(self, project_id: UUID, *, create: bool) -> Path:
        project_root = self._root / str(project_id)
        if create:
            project_root.mkdir(exist_ok=True)
        if (
            project_root.is_symlink()
            or not project_root.is_dir()
            or project_root.parent.resolve() != self._root
        ):
            raise ValueError("folder batch project root is invalid")
        return project_root
