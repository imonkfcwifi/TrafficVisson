"""Project-scoped job orchestration for review-only dense-frame indexes.

This service deliberately owns no HTTP routes.  A future bridge route can
translate its typed errors and dictionaries without giving the renderer a
filesystem path.  The service accepts only an opaque registered-source
identity plus SHA-256 and calls the supplied resolver twice: once before a job
is accepted and again immediately before the worker reads the source.

One bridge-local worker serializes all ffmpeg indexing work.  Records and
queries remain project-scoped, and completed records are persisted below the
matching project directory with a SHA-256 sidecar.  No incident, annotation,
training, maintenance, model-promotion, or inference module is imported here.
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
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Union
from uuid import UUID, uuid4

from .dense_frame_index import (
    DenseFrameIndexCancelled,
    DenseFrameIndexEngine,
    DenseFrameIndexError,
    DenseFrameIndexLimits,
    DenseFrameIndexOutputLimit,
    DenseFrameIndexResult,
    DenseFrameIndexSourceChanged,
    DenseFrameIndexTimeout,
    FRAME_ID_PATTERN,
    FRAME_INDEX_SCHEMA,
    ReviewOnlyPolicy,
)


DENSE_INDEX_JOB_SCHEMA = "trafficvisson.dense-index-job.v1"
MAX_DENSE_INDEX_JOB_RECORDS = 128
MAX_QUEUED_DENSE_INDEX_JOBS = 16
MAX_PERSISTED_DENSE_INDEX_RECORD_BYTES = 64 * 1024 * 1024
MAX_RECOVERY_SCAN_ENTRIES = MAX_DENSE_INDEX_JOB_RECORDS * 32
_ACTIVE_STATES = frozenset({"queued", "running", "cancelling"})
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_OPAQUE_SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_RUN_DIRECTORY_PATTERN = re.compile(
    r"^dense-frame-index-[A-Za-z0-9._-]{6,128}$"
)
_FRAME_RELATIVE_PATTERN = re.compile(r"^frames/frame-[0-9]{8}\.jpg$")
_ALLOWED_VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mts", ".webm"}
)
_ERROR_CODES = frozenset(
    {
        "source_changed",
        "timeout",
        "output_limit",
        "indexing_failed",
        "persistence_failed",
        "internal_error",
    }
)


class DenseIndexJobServiceError(RuntimeError):
    """Base class for job-service contract failures."""


class DenseIndexJobNotFound(DenseIndexJobServiceError):
    """Raised when a job is not present in the requested project."""


class DenseIndexJobConflict(DenseIndexJobServiceError):
    """Raised when an equivalent source/FPS job is already active."""


class DenseIndexJobUnavailable(DenseIndexJobServiceError):
    """Raised when the queue, registry, or worker is unavailable."""


class DenseIndexJobSourceRejected(DenseIndexJobServiceError):
    """Raised when a registered opaque source cannot be safely resolved."""


@dataclass(frozen=True)
class ResolvedDenseIndexSource:
    """Bridge-internal result returned by a registered-source resolver."""

    project_id: UUID
    opaque_source_identity: str
    source_sha256: str
    media_path: Path
    size_bytes: int
    modified_time_ns: int


@dataclass(frozen=True)
class ResolvedDenseIndexArtifact:
    """Verified, bounded artifact handle that never crosses JSON responses."""

    path: Path
    media_type: str
    size_bytes: int
    sha256: str


@dataclass
class DenseIndexJobRecord:
    job_id: UUID
    project_id: UUID
    opaque_source_identity: str
    source_sha256: str
    source_size_bytes: int
    source_modified_time_ns: int
    initial_media_path: Optional[Path]
    sampling_fps: int
    status: str
    stage: str
    error_code: Optional[str]
    result: Optional[dict[str, object]]
    created_at: datetime
    updated_at: datetime
    cancel_event: threading.Event
    artifact_directory: Optional[Path]
    record_sha256: Optional[str]


SourceResolver = Callable[
    [UUID, str, str], ResolvedDenseIndexSource
]
EngineFactory = Callable[
    [DenseFrameIndexLimits], DenseFrameIndexEngine
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: object) -> str:
    if isinstance(value, (UUID, datetime)):
        return str(value)
    raise TypeError(
        f"Unsupported persisted dense-index value: {type(value).__name__}"
    )


def _parse_utc_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("dense-index record datetime is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("dense-index record datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _strict_json_object(raw: bytes, label: str) -> dict[str, object]:
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError(f"{label} contains a non-finite JSON number")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _hash_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    expected_size: Optional[int] = None,
) -> tuple[str, int, int]:
    try:
        before = path.stat()
    except OSError as error:
        raise ValueError("an artifact is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size <= 0
        or before.st_size > maximum_bytes
        or (
            expected_size is not None
            and before.st_size != expected_size
        )
    ):
        raise ValueError("an artifact violates its file contract")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(4 * 1024 * 1024):
                total += len(block)
                if total > maximum_bytes:
                    raise ValueError("an artifact exceeds its size contract")
                digest.update(block)
        after = path.stat()
    except OSError as error:
        raise ValueError("an artifact could not be read") from error
    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ValueError("an artifact changed while it was verified")
    return digest.hexdigest(), total, int(after.st_mtime_ns)


def _review_safety_dict() -> dict[str, bool]:
    return asdict(ReviewOnlyPolicy())


def _valid_safety(value: object) -> bool:
    return value == _review_safety_dict()


class DenseIndexJobService:
    """Serialize dense-frame builds and persist verified completed records."""

    def __init__(
        self,
        source_resolver: SourceResolver,
        *,
        data_root: Union[str, Path],
        ffmpeg_path: Optional[Union[str, Path]] = None,
        ffprobe_path: Optional[Union[str, Path]] = None,
        dense_limits: Optional[DenseFrameIndexLimits] = None,
        engine_factory: Optional[EngineFactory] = None,
    ) -> None:
        if not callable(source_resolver):
            raise ValueError("source_resolver must be callable")
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("dense-index data_root must be absolute")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or _is_reparse_point(root) or not root.is_dir():
            raise ValueError("dense-index data_root must be a real directory")
        self._data_root = root.resolve()
        self._projects_root = self._data_root / "projects"
        self._projects_root.mkdir(exist_ok=True)
        if (
            self._projects_root.is_symlink()
            or _is_reparse_point(self._projects_root)
            or not self._projects_root.is_dir()
        ):
            raise ValueError(
                "dense-index projects root must be a real directory"
            )
        self._resolver = source_resolver
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._base_limits = dense_limits or DenseFrameIndexLimits()
        self._engine_factory = engine_factory
        self._jobs: "OrderedDict[UUID, DenseIndexJobRecord]" = OrderedDict()
        self._queue: "queue.Queue[Optional[UUID]]" = queue.Queue(
            maxsize=MAX_QUEUED_DENSE_INDEX_JOBS
        )
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._recovery_summary = {
            "scanned_records": 0,
            "loaded_records": 0,
            "rejected_records": 0,
        }
        self._load_completed_records()

    def start(
        self,
        project_id: UUID,
        opaque_source_identity: str,
        source_sha256: str,
        *,
        sampling_fps: int,
    ) -> dict[str, object]:
        project_id = self._validate_project_id(project_id)
        identity = self._validate_opaque_identity(opaque_source_identity)
        digest = self._validate_source_sha256(source_sha256)
        fps = self._validate_sampling_fps(sampling_fps)
        initial_grant = self._resolve_source(
            project_id, identity, digest
        )
        self._project_paths(project_id, create=True)

        with self._lock:
            if self._stop_event.is_set():
                raise DenseIndexJobUnavailable(
                    "The dense-index worker is shutting down"
                )
            for existing in self._jobs.values():
                if (
                    existing.project_id == project_id
                    and existing.opaque_source_identity == identity
                    and existing.sampling_fps == fps
                    and existing.status in _ACTIVE_STATES
                ):
                    raise DenseIndexJobConflict(
                        "An equivalent dense-index job is already active"
                    )
            self._evict_terminal_locked()
            if len(self._jobs) >= MAX_DENSE_INDEX_JOB_RECORDS:
                raise DenseIndexJobUnavailable(
                    "The dense-index job registry is full"
                )
            now = _utc_now()
            job = DenseIndexJobRecord(
                job_id=uuid4(),
                project_id=project_id,
                opaque_source_identity=identity,
                source_sha256=digest,
                source_size_bytes=initial_grant.size_bytes,
                source_modified_time_ns=initial_grant.modified_time_ns,
                initial_media_path=initial_grant.media_path,
                sampling_fps=fps,
                status="queued",
                stage="queued",
                error_code=None,
                result=None,
                created_at=now,
                updated_at=now,
                cancel_event=threading.Event(),
                artifact_directory=None,
                record_sha256=None,
            )
            self._jobs[job.job_id] = job
            self._ensure_worker_locked()
            try:
                self._queue.put_nowait(job.job_id)
            except queue.Full as error:
                self._jobs.pop(job.job_id, None)
                raise DenseIndexJobUnavailable(
                    "The dense-index queue is full"
                ) from error
            return self._serialize(job)

    def get(
        self, project_id: UUID, job_id: UUID
    ) -> dict[str, object]:
        with self._lock:
            return self._serialize(self._job(project_id, job_id))

    def list(
        self, project_id: UUID, *, limit: int = 100
    ) -> list[dict[str, object]]:
        project_id = self._validate_project_id(project_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_DENSE_INDEX_JOB_RECORDS
        ):
            raise ValueError("dense-index job list limit is invalid")
        with self._lock:
            rows = [
                self._serialize(job)
                for job in reversed(tuple(self._jobs.values()))
                if job.project_id == project_id
            ]
        return rows[:limit]

    def latest_completed(
        self, project_id: UUID
    ) -> Optional[dict[str, object]]:
        project_id = self._validate_project_id(project_id)
        with self._lock:
            for job in reversed(tuple(self._jobs.values())):
                if (
                    job.project_id == project_id
                    and job.status == "completed"
                    and job.result is not None
                ):
                    return self._serialize(job)
        return None

    def cancel(
        self, project_id: UUID, job_id: UUID
    ) -> dict[str, object]:
        with self._lock:
            job = self._job(project_id, job_id)
            if job.status in _TERMINAL_STATES:
                return self._serialize(job)
            job.cancel_event.set()
            job.updated_at = _utc_now()
            if job.status == "queued":
                job.status = "cancelled"
                job.stage = "cancelled"
            else:
                job.status = "cancelling"
                job.stage = "cancelling"
            return self._serialize(job)

    def resolve_artifact(
        self,
        project_id: UUID,
        job_id: UUID,
        artifact_id: str,
    ) -> ResolvedDenseIndexArtifact:
        """Resolve one completed artifact by project/job/opaque artifact ID.

        Paths are derived exclusively from the verified completed record.
        The selected file is identity-, metadata-, and SHA-256-checked again
        immediately before a bridge FileResponse may read it.
        """

        is_index = artifact_id == "frame-index"
        if (
            not is_index
            and (
                not isinstance(artifact_id, str)
                or FRAME_ID_PATTERN.fullmatch(artifact_id) is None
            )
        ):
            raise DenseIndexJobNotFound(
                "The dense-index artifact was not found in this project"
            )

        with self._lock:
            job = self._job(project_id, job_id)
            if (
                job.status != "completed"
                or job.result is None
                or job.artifact_directory is None
                or job.record_sha256 is None
            ):
                raise DenseIndexJobConflict(
                    "Dense-index artifacts are available only after completion"
                )
            result = job.result
            artifact_directory = job.artifact_directory
            if is_index:
                metadata = result.get("index")
                expected_relative_path = "frame-index.json"
                expected_media_type = "application/json"
                maximum_bytes = self._base_limits.max_index_bytes
                expected_artifact_ref = (
                    f"dense-index:{job.job_id}:frame-index"
                )
            else:
                frames = result.get("frames")
                metadata = next(
                    (
                        frame
                        for frame in frames
                        if isinstance(frame, dict)
                        and frame.get("frame_id") == artifact_id
                    ),
                    None,
                ) if isinstance(frames, list) else None
                if not isinstance(metadata, dict):
                    raise DenseIndexJobNotFound(
                        "The dense-index artifact was not found in this project"
                    )
                frame_index = metadata.get("frame_index")
                if (
                    isinstance(frame_index, bool)
                    or not isinstance(frame_index, int)
                    or frame_index < 0
                ):
                    raise DenseIndexJobUnavailable(
                        "The dense-index artifact metadata is invalid"
                    )
                expected_relative_path = (
                    f"frames/frame-{frame_index:08d}.jpg"
                )
                expected_media_type = "image/jpeg"
                maximum_bytes = self._base_limits.max_frame_bytes
                expected_artifact_ref = (
                    f"dense-index:{job.job_id}:{artifact_id}"
                )

        if not isinstance(metadata, dict):
            raise DenseIndexJobUnavailable(
                "The dense-index artifact metadata is invalid"
            )
        relative_path = metadata.get("relative_path")
        media_type = metadata.get("media_type")
        size_bytes = metadata.get("size_bytes")
        modified_time_ns = metadata.get("modified_time_ns")
        sha256_lower = metadata.get("sha256")
        artifact_ref = metadata.get("artifact_ref")
        if (
            relative_path != expected_relative_path
            or media_type != expected_media_type
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= maximum_bytes
            or isinstance(modified_time_ns, bool)
            or not isinstance(modified_time_ns, int)
            or modified_time_ns < 0
            or not isinstance(sha256_lower, str)
            or _SHA256_PATTERN.fullmatch(sha256_lower) is None
            or artifact_ref != expected_artifact_ref
        ):
            raise DenseIndexJobUnavailable(
                "The dense-index artifact metadata is invalid"
            )

        try:
            _, output_root, _ = self._project_paths(
                project_id, create=False
            )
            verified_root = artifact_directory.resolve(strict=True)
            artifact_path = artifact_directory / expected_relative_path
            verified_path = artifact_path.resolve(strict=True)
            before = verified_path.stat()
        except (OSError, ValueError) as error:
            raise DenseIndexJobUnavailable(
                "The dense-index artifact is unavailable"
            ) from error
        expected_parent = (
            verified_root
            if is_index
            else verified_root / "frames"
        )
        if (
            verified_root.parent != output_root
            or artifact_directory.is_symlink()
            or _is_reparse_point(artifact_directory)
            or verified_path.parent != expected_parent
            or artifact_path.is_symlink()
            or _is_reparse_point(artifact_path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != size_bytes
            or before.st_mtime_ns != modified_time_ns
        ):
            raise DenseIndexJobUnavailable(
                "The dense-index artifact integrity changed"
            )
        try:
            actual_hash, actual_size, actual_mtime = _hash_regular_file(
                verified_path,
                maximum_bytes=maximum_bytes,
                expected_size=size_bytes,
            )
            with verified_path.open("rb") as stream:
                header = stream.read(32)
        except (OSError, ValueError) as error:
            raise DenseIndexJobUnavailable(
                "The dense-index artifact could not be verified"
            ) from error
        if (
            actual_hash != sha256_lower
            or actual_size != size_bytes
            or actual_mtime != modified_time_ns
            or (
                expected_media_type == "image/jpeg"
                and not header.startswith(b"\xff\xd8\xff")
            )
            or (
                expected_media_type == "application/json"
                and not header.lstrip().startswith(b"{")
            )
        ):
            raise DenseIndexJobUnavailable(
                "The dense-index artifact failed integrity verification"
            )
        return ResolvedDenseIndexArtifact(
            path=verified_path,
            media_type=expected_media_type,
            size_bytes=size_bytes,
            sha256=sha256_lower,
        )

    def recovery_summary(self) -> dict[str, int]:
        return dict(self._recovery_summary)

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._lock:
            for job in self._jobs.values():
                if job.status in _ACTIVE_STATES:
                    job.cancel_event.set()
                    job.updated_at = _utc_now()
                    if job.status == "queued":
                        job.status = "cancelled"
                        job.stage = "cancelled"
                    else:
                        job.status = "cancelling"
                        job.stage = "cancelling"
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
            worker.join(timeout=5.0)

    @staticmethod
    def _validate_project_id(project_id: UUID) -> UUID:
        if not isinstance(project_id, UUID):
            raise ValueError("project_id must be a UUID")
        return project_id

    @staticmethod
    def _validate_opaque_identity(value: str) -> str:
        if (
            not isinstance(value, str)
            or _OPAQUE_SOURCE_PATTERN.fullmatch(value) is None
        ):
            raise ValueError("opaque_source_identity is invalid")
        return value

    @staticmethod
    def _validate_source_sha256(value: str) -> str:
        if (
            not isinstance(value, str)
            or _SHA256_PATTERN.fullmatch(value) is None
        ):
            raise ValueError(
                "source_sha256 must be a lowercase SHA-256 value"
            )
        return value

    @staticmethod
    def _validate_sampling_fps(value: int) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 5
        ):
            raise ValueError("sampling_fps must be an integer from 1 to 5")
        return value

    def _resolve_source(
        self,
        project_id: UUID,
        identity: str,
        digest: str,
    ) -> ResolvedDenseIndexSource:
        try:
            grant = self._resolver(project_id, identity, digest)
        except Exception as error:
            raise DenseIndexJobSourceRejected(
                "The registered dense-index source could not be resolved"
            ) from error
        if not isinstance(grant, ResolvedDenseIndexSource):
            raise DenseIndexJobSourceRejected(
                "The registered-source resolver returned an invalid grant"
            )
        if (
            grant.project_id != project_id
            or grant.opaque_source_identity != identity
            or grant.source_sha256 != digest
        ):
            raise DenseIndexJobSourceRejected(
                "The registered-source grant identity does not match"
            )
        path = Path(grant.media_path)
        if (
            not path.is_absolute()
            or path.suffix.casefold() not in _ALLOWED_VIDEO_EXTENSIONS
            or path.is_symlink()
            or _is_reparse_point(path)
        ):
            raise DenseIndexJobSourceRejected(
                "The registered-source path is unsafe"
            )
        try:
            resolved = path.resolve(strict=True)
            metadata = resolved.stat()
        except OSError as error:
            raise DenseIndexJobSourceRejected(
                "The registered source is unavailable"
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or isinstance(grant.size_bytes, bool)
            or not isinstance(grant.size_bytes, int)
            or grant.size_bytes != metadata.st_size
            or isinstance(grant.modified_time_ns, bool)
            or not isinstance(grant.modified_time_ns, int)
            or grant.modified_time_ns != metadata.st_mtime_ns
        ):
            raise DenseIndexJobSourceRejected(
                "The registered source metadata does not match"
            )
        return ResolvedDenseIndexSource(
            project_id=grant.project_id,
            opaque_source_identity=grant.opaque_source_identity,
            source_sha256=grant.source_sha256,
            media_path=resolved,
            size_bytes=int(metadata.st_size),
            modified_time_ns=int(metadata.st_mtime_ns),
        )

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="trafficvisson-dense-frame-index",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if job_id is None:
                    return
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is None or job.status == "cancelled":
                        continue
                    job.status = "running"
                    job.stage = "resolving_source"
                    job.updated_at = _utc_now()
                self._execute_job(job)
            finally:
                self._queue.task_done()

    def _execute_job(self, job: DenseIndexJobRecord) -> None:
        artifact_directory: Optional[Path] = None
        try:
            grant = self._resolve_source(
                job.project_id,
                job.opaque_source_identity,
                job.source_sha256,
            )
            if (
                grant.size_bytes != job.source_size_bytes
                or grant.modified_time_ns != job.source_modified_time_ns
                or grant.media_path != job.initial_media_path
            ):
                raise DenseFrameIndexSourceChanged(
                    "The registered source changed after the job was queued"
                )
            if job.cancel_event.is_set():
                raise DenseFrameIndexCancelled(
                    "Dense-frame indexing was cancelled"
                )
            project_root, output_root, _ = self._project_paths(
                job.project_id, create=True
            )
            with self._lock:
                if job.status == "running":
                    job.stage = "indexing_frames"
                    job.updated_at = _utc_now()
            limits = replace(
                self._base_limits, sampling_fps=job.sampling_fps
            )
            if self._engine_factory is None:
                engine = DenseFrameIndexEngine(
                    ffmpeg_path=self._ffmpeg_path,
                    ffprobe_path=self._ffprobe_path,
                    limits=limits,
                )
            else:
                engine = self._engine_factory(limits)
                if not hasattr(engine, "build") or not callable(engine.build):
                    raise DenseFrameIndexError(
                        "The dense-index engine factory returned invalid data"
                    )
            result = engine.build(
                grant.media_path,
                source_sha256=grant.source_sha256,
                project_root=project_root,
                output_directory=output_root,
                cancel=job.cancel_event.is_set,
            )
            artifact_directory = self._validated_run_directory(
                result.output_directory, output_root
            )
            if job.cancel_event.is_set():
                raise DenseFrameIndexCancelled(
                    "Dense-frame indexing was cancelled"
                )
            with self._lock:
                current = self._jobs.get(job.job_id)
                if current is None:
                    self._discard_run_directory(
                        artifact_directory, output_root
                    )
                    return
                if current.cancel_event.is_set():
                    raise DenseFrameIndexCancelled(
                        "Dense-frame indexing was cancelled"
                    )
                current.stage = "finalizing"
                current.updated_at = _utc_now()

            sanitized = self._sanitize_result(
                job,
                result,
                artifact_directory,
            )
            completed_at = _utc_now()
            with self._lock:
                current = self._jobs.get(job.job_id)
                if current is None:
                    self._discard_run_directory(
                        artifact_directory, output_root
                    )
                    return
                if current.cancel_event.is_set():
                    raise DenseFrameIndexCancelled(
                        "Dense-frame indexing was cancelled"
                    )
                record_sha256 = self._persist_completed_record(
                    current,
                    sanitized,
                    artifact_directory,
                    completed_at,
                )
                current.status = "completed"
                current.stage = "completed"
                current.error_code = None
                current.result = sanitized
                current.updated_at = completed_at
                current.artifact_directory = artifact_directory
                current.record_sha256 = record_sha256
                artifact_directory = None
        except DenseFrameIndexCancelled:
            self._finish_terminal(job.job_id, "cancelled", None)
        except DenseFrameIndexSourceChanged:
            self._finish_terminal(
                job.job_id, "failed", "source_changed"
            )
        except DenseIndexJobSourceRejected:
            self._finish_terminal(
                job.job_id, "failed", "source_changed"
            )
        except DenseFrameIndexTimeout:
            self._finish_terminal(job.job_id, "failed", "timeout")
        except DenseFrameIndexOutputLimit:
            self._finish_terminal(
                job.job_id, "failed", "output_limit"
            )
        except DenseFrameIndexError:
            self._finish_terminal(
                job.job_id, "failed", "indexing_failed"
            )
        except OSError:
            self._finish_terminal(
                job.job_id, "failed", "persistence_failed"
            )
        except Exception:
            self._finish_terminal(
                job.job_id, "failed", "internal_error"
            )
        finally:
            if artifact_directory is not None:
                try:
                    _, output_root, _ = self._project_paths(
                        job.project_id, create=False
                    )
                except Exception:
                    output_root = artifact_directory.parent
                self._discard_run_directory(
                    artifact_directory, output_root
                )

    def _finish_terminal(
        self,
        job_id: UUID,
        status_value: str,
        error_code: Optional[str],
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status == "completed":
                return
            if status_value not in {"cancelled", "failed"}:
                raise AssertionError("invalid terminal dense-index state")
            if error_code is not None and error_code not in _ERROR_CODES:
                raise AssertionError("invalid dense-index error code")
            job.status = status_value
            job.stage = status_value
            job.error_code = error_code
            job.result = None
            job.artifact_directory = None
            job.record_sha256 = None
            job.updated_at = _utc_now()

    def _project_paths(
        self, project_id: UUID, *, create: bool
    ) -> tuple[Path, Path, Path]:
        project_root = self._projects_root / str(project_id)
        output_root = project_root / "dense-frame-indexes"
        records_root = project_root / "dense-index-job-records"
        if create:
            project_root.mkdir(exist_ok=True)
            output_root.mkdir(exist_ok=True)
            records_root.mkdir(exist_ok=True)
        try:
            projects = self._projects_root.resolve(strict=True)
            project = project_root.resolve(strict=True)
            output = output_root.resolve(strict=True)
            records = records_root.resolve(strict=True)
        except OSError as error:
            raise DenseIndexJobServiceError(
                "The project dense-index directories are unavailable"
            ) from error
        if (
            project.parent != projects
            or output.parent != project
            or records.parent != project
            or project_root.is_symlink()
            or output_root.is_symlink()
            or records_root.is_symlink()
            or _is_reparse_point(project_root)
            or _is_reparse_point(output_root)
            or _is_reparse_point(records_root)
            or not project.is_dir()
            or not output.is_dir()
            or not records.is_dir()
        ):
            raise DenseIndexJobServiceError(
                "The project dense-index directory contract is invalid"
            )
        return project, output, records

    @staticmethod
    def _validated_run_directory(
        value: Union[str, Path], output_root: Path
    ) -> Path:
        path = Path(value)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or _is_reparse_point(path)
            or _RUN_DIRECTORY_PATTERN.fullmatch(path.name) is None
        ):
            raise DenseFrameIndexError(
                "The dense-index run directory is invalid"
            )
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise DenseFrameIndexError(
                "The dense-index run directory is unavailable"
            ) from error
        if (
            resolved.parent != output_root
            or not resolved.is_dir()
        ):
            raise DenseFrameIndexError(
                "The dense-index run escaped the project output root"
            )
        return resolved

    def _sanitize_result(
        self,
        job: DenseIndexJobRecord,
        result: DenseFrameIndexResult,
        artifact_directory: Path,
    ) -> dict[str, object]:
        if not isinstance(result, DenseFrameIndexResult):
            raise DenseFrameIndexError(
                "The dense-index engine returned an invalid result"
            )
        if result.index_relative_path != "frame-index.json":
            raise DenseFrameIndexError(
                "The dense-index result has an invalid index path"
            )
        provenance = asdict(result.provenance)
        if (
            provenance.get("schema_version") != FRAME_INDEX_SCHEMA
            or provenance.get("source_sha256") != job.source_sha256
            or provenance.get("source_size_bytes")
            != job.source_size_bytes
            or provenance.get("source_modified_time_ns")
            != job.source_modified_time_ns
            or provenance.get("sampling_fps") != job.sampling_fps
            or provenance.get("frame_count") != len(result.frames)
            or not _valid_safety(provenance.get("review_policy"))
        ):
            raise DenseFrameIndexError(
                "The dense-index provenance contract is invalid"
            )
        if (
            not result.frames
            or len(result.frames) > self._base_limits.max_frames
        ):
            raise DenseFrameIndexError(
                "The dense-index frame count is invalid"
            )

        serialized_frames = []
        index_frames = []
        frame_ids: set[str] = set()
        total_frame_bytes = 0
        for expected_index, frame in enumerate(result.frames):
            if job.cancel_event.is_set():
                raise DenseFrameIndexCancelled(
                    "Dense-frame indexing was cancelled during finalization"
                )
            row = asdict(frame)
            expected_relative = (
                f"frames/frame-{expected_index:08d}.jpg"
            )
            timestamp = expected_index / job.sampling_fps
            if (
                row.get("frame_index") != expected_index
                or row.get("relative_path") != expected_relative
                or not isinstance(row.get("frame_id"), str)
                or FRAME_ID_PATTERN.fullmatch(row["frame_id"]) is None
                or row["frame_id"] in frame_ids
                or row.get("timestamp_numerator") != expected_index
                or row.get("timestamp_denominator") != job.sampling_fps
                or not isinstance(row.get("timestamp_seconds"), (int, float))
                or isinstance(row.get("timestamp_seconds"), bool)
                or not math.isfinite(float(row["timestamp_seconds"]))
                or abs(float(row["timestamp_seconds"]) - timestamp) > 0.000001
                or row.get("media_type") != "image/jpeg"
                or row.get("evidence_status") != "unreviewed_candidate"
                or not isinstance(row.get("width"), int)
                or isinstance(row.get("width"), bool)
                or not 1 <= row["width"] <= self._base_limits.max_frame_width
                or not isinstance(row.get("height"), int)
                or isinstance(row.get("height"), bool)
                or row["height"] <= 0
                or row["width"] * row["height"]
                > self._base_limits.max_frame_pixels
                or not isinstance(row.get("size_bytes"), int)
                or isinstance(row.get("size_bytes"), bool)
                or not 1 <= row["size_bytes"]
                <= self._base_limits.max_frame_bytes
                or not isinstance(row.get("sha256"), str)
                or _SHA256_PATTERN.fullmatch(row["sha256"]) is None
            ):
                raise DenseFrameIndexError(
                    "A dense-index frame record is invalid"
                )
            frame_ids.add(row["frame_id"])
            frame_path = artifact_directory / expected_relative
            try:
                resolved_frame = frame_path.resolve(strict=True)
            except OSError as error:
                raise DenseFrameIndexError(
                    "A dense-index frame artifact is unavailable"
                ) from error
            if resolved_frame.parent != artifact_directory / "frames":
                raise DenseFrameIndexError(
                    "A dense-index frame escaped its run directory"
                )
            try:
                actual_hash, actual_size, modified_time_ns = (
                    _hash_regular_file(
                        resolved_frame,
                        maximum_bytes=self._base_limits.max_frame_bytes,
                        expected_size=row["size_bytes"],
                    )
                )
            except ValueError as error:
                raise DenseFrameIndexError(str(error)) from error
            if actual_hash != row["sha256"]:
                raise DenseFrameIndexError(
                    "A dense-index frame failed integrity verification"
                )
            total_frame_bytes += actual_size
            if total_frame_bytes > self._base_limits.max_total_output_bytes:
                raise DenseFrameIndexOutputLimit(
                    "Dense-index frame artifacts exceed their total bound"
                )
            index_frames.append(dict(row))
            serialized_frames.append(
                {
                    **row,
                    "modified_time_ns": modified_time_ns,
                    "artifact_ref": (
                        f"dense-index:{job.job_id}:{row['frame_id']}"
                    ),
                }
            )

        index_path = artifact_directory / "frame-index.json"
        if job.cancel_event.is_set():
            raise DenseFrameIndexCancelled(
                "Dense-frame indexing was cancelled during finalization"
            )
        try:
            index_hash, index_size, index_mtime = _hash_regular_file(
                index_path,
                maximum_bytes=self._base_limits.max_index_bytes,
            )
            index_payload = _strict_json_object(
                index_path.read_bytes(), "dense-frame index"
            )
        except ValueError as error:
            raise DenseFrameIndexError(str(error)) from error
        expected_index_payload = {
            "schema_version": FRAME_INDEX_SCHEMA,
            "frame_count": len(index_frames),
            "frames": index_frames,
            "provenance": provenance,
        }
        if index_payload != expected_index_payload:
            raise DenseFrameIndexError(
                "The dense-frame index does not match its engine result"
            )
        if (
            total_frame_bytes + index_size
            > self._base_limits.max_total_output_bytes
        ):
            raise DenseFrameIndexOutputLimit(
                "Dense-index artifacts exceed their total bound"
            )
        safety = _review_safety_dict()
        return {
            "artifact_set_id": str(job.job_id),
            "frame_count": len(serialized_frames),
            "total_frame_bytes": total_frame_bytes,
            "index": {
                "relative_path": "frame-index.json",
                "size_bytes": index_size,
                "modified_time_ns": index_mtime,
                "sha256": index_hash,
                "media_type": "application/json",
                "artifact_ref": f"dense-index:{job.job_id}:frame-index",
            },
            "frames": serialized_frames,
            "provenance": provenance,
            "safety": safety,
        }

    def _persist_completed_record(
        self,
        job: DenseIndexJobRecord,
        result: dict[str, object],
        artifact_directory: Path,
        completed_at: datetime,
    ) -> str:
        _, _, records_root = self._project_paths(
            job.project_id, create=True
        )
        payload = {
            "schema_version": DENSE_INDEX_JOB_SCHEMA,
            "job_id": job.job_id,
            "project_id": job.project_id,
            "opaque_source_identity": job.opaque_source_identity,
            "source_sha256": job.source_sha256,
            "source_size_bytes": job.source_size_bytes,
            "source_modified_time_ns": job.source_modified_time_ns,
            "sampling_fps": job.sampling_fps,
            "status": "completed",
            "created_at": job.created_at,
            "updated_at": completed_at,
            "artifact_directory_name": artifact_directory.name,
            "result": result,
            "safety": _review_safety_dict(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_json_default,
            allow_nan=False,
        ).encode("utf-8")
        if (
            not encoded
            or len(encoded) > MAX_PERSISTED_DENSE_INDEX_RECORD_BYTES
        ):
            raise DenseFrameIndexOutputLimit(
                "The completed dense-index record is too large to persist"
            )
        digest = hashlib.sha256(encoded).hexdigest()
        record_path = records_root / f"{job.job_id}.json"
        digest_path = records_root / f"{job.job_id}.sha256"
        temporary_record = records_root / (
            f".{job.job_id}.{uuid4().hex}.json.tmp"
        )
        temporary_digest = records_root / (
            f".{job.job_id}.{uuid4().hex}.sha256.tmp"
        )
        record_descriptor: Optional[int] = None
        digest_descriptor: Optional[int] = None
        try:
            record_descriptor = os.open(
                temporary_record,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(record_descriptor, "wb") as stream:
                record_descriptor = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            digest_descriptor = os.open(
                temporary_digest,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(digest_descriptor, "wb") as stream:
                digest_descriptor = None
                stream.write(digest.encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_digest, digest_path)
            os.replace(temporary_record, record_path)
        except OSError as error:
            raise OSError(
                "The completed dense-index record could not be persisted"
            ) from error
        finally:
            if record_descriptor is not None:
                os.close(record_descriptor)
            if digest_descriptor is not None:
                os.close(digest_descriptor)
            for path in (temporary_record, temporary_digest):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        return digest

    def _load_completed_records(self) -> None:
        candidates: list[tuple[int, Path, UUID]] = []
        scanned = 0
        try:
            project_entries = tuple(os.scandir(self._projects_root))
        except OSError:
            return
        for project_entry in project_entries:
            if (
                not project_entry.is_dir(follow_symlinks=False)
                or project_entry.is_symlink()
            ):
                continue
            try:
                project_id = UUID(project_entry.name)
            except ValueError:
                continue
            records_root = (
                Path(project_entry.path) / "dense-index-job-records"
            )
            try:
                if (
                    not records_root.is_dir()
                    or records_root.is_symlink()
                    or _is_reparse_point(records_root)
                ):
                    continue
                entries = os.scandir(records_root)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_RECOVERY_SCAN_ENTRIES:
                        break
                    if (
                        not entry.name.endswith(".json")
                        or entry.is_symlink()
                        or not entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if (
                        metadata.st_size <= 0
                        or metadata.st_size
                        > MAX_PERSISTED_DENSE_INDEX_RECORD_BYTES
                    ):
                        continue
                    candidates.append(
                        (
                            int(metadata.st_mtime_ns),
                            Path(entry.path),
                            project_id,
                        )
                    )
            if scanned > MAX_RECOVERY_SCAN_ENTRIES:
                break
        self._recovery_summary["scanned_records"] = scanned
        candidates.sort(key=lambda row: row[0], reverse=True)
        loaded: list[DenseIndexJobRecord] = []
        for _, record_path, project_id in candidates:
            if len(loaded) >= MAX_DENSE_INDEX_JOB_RECORDS:
                break
            try:
                loaded.append(
                    self._load_completed_record(record_path, project_id)
                )
            except (
                DenseIndexJobServiceError,
                OSError,
                UnicodeError,
                ValueError,
                json.JSONDecodeError,
            ):
                self._recovery_summary["rejected_records"] += 1
        loaded.sort(key=lambda job: job.updated_at)
        for job in loaded:
            self._jobs[job.job_id] = job
        self._recovery_summary["loaded_records"] = len(loaded)

    def _load_completed_record(
        self, record_path: Path, expected_project_id: UUID
    ) -> DenseIndexJobRecord:
        raw = self._read_record_with_digest(record_path)
        digest = hashlib.sha256(raw).hexdigest()
        decoded = _strict_json_object(raw, "dense-index job record")
        expected_keys = {
            "schema_version",
            "job_id",
            "project_id",
            "opaque_source_identity",
            "source_sha256",
            "source_size_bytes",
            "source_modified_time_ns",
            "sampling_fps",
            "status",
            "created_at",
            "updated_at",
            "artifact_directory_name",
            "result",
            "safety",
        }
        if set(decoded) != expected_keys:
            raise ValueError("dense-index job record keys are invalid")
        if (
            decoded.get("schema_version") != DENSE_INDEX_JOB_SCHEMA
            or decoded.get("status") != "completed"
            or not _valid_safety(decoded.get("safety"))
        ):
            raise ValueError("dense-index job record schema is invalid")
        job_id = UUID(str(decoded["job_id"]))
        project_id = UUID(str(decoded["project_id"]))
        if (
            project_id != expected_project_id
            or record_path.name != f"{job_id}.json"
        ):
            raise ValueError("dense-index job record identity is invalid")
        identity = self._validate_opaque_identity(
            decoded["opaque_source_identity"]
        )
        source_sha256 = self._validate_source_sha256(
            decoded["source_sha256"]
        )
        sampling_fps = self._validate_sampling_fps(
            decoded["sampling_fps"]
        )
        source_size = decoded["source_size_bytes"]
        modified_time = decoded["source_modified_time_ns"]
        if (
            isinstance(source_size, bool)
            or not isinstance(source_size, int)
            or source_size <= 0
            or isinstance(modified_time, bool)
            or not isinstance(modified_time, int)
            or modified_time < 0
        ):
            raise ValueError("dense-index source provenance is invalid")
        created_at = _parse_utc_datetime(decoded["created_at"])
        updated_at = _parse_utc_datetime(decoded["updated_at"])
        if updated_at < created_at:
            raise ValueError("dense-index record timestamps are invalid")
        directory_name = decoded["artifact_directory_name"]
        if (
            not isinstance(directory_name, str)
            or _RUN_DIRECTORY_PATTERN.fullmatch(directory_name) is None
        ):
            raise ValueError(
                "dense-index artifact directory identity is invalid"
            )
        _, output_root, _ = self._project_paths(
            project_id, create=False
        )
        artifact_directory = self._validated_run_directory(
            output_root / directory_name,
            output_root,
        )
        result = self._validate_persisted_result(
            decoded["result"],
            job_id=job_id,
            project_id=project_id,
            artifact_directory=artifact_directory,
            source_sha256=source_sha256,
            source_size_bytes=source_size,
            source_modified_time_ns=modified_time,
            sampling_fps=sampling_fps,
        )
        return DenseIndexJobRecord(
            job_id=job_id,
            project_id=project_id,
            opaque_source_identity=identity,
            source_sha256=source_sha256,
            source_size_bytes=source_size,
            source_modified_time_ns=modified_time,
            initial_media_path=None,
            sampling_fps=sampling_fps,
            status="completed",
            stage="completed",
            error_code=None,
            result=result,
            created_at=created_at,
            updated_at=updated_at,
            cancel_event=threading.Event(),
            artifact_directory=artifact_directory,
            record_sha256=digest,
        )

    @staticmethod
    def _read_record_with_digest(record_path: Path) -> bytes:
        if (
            record_path.is_symlink()
            or _is_reparse_point(record_path)
            or not record_path.is_file()
        ):
            raise ValueError("dense-index record is not a regular file")
        raw = record_path.read_bytes()
        if (
            not raw
            or len(raw) > MAX_PERSISTED_DENSE_INDEX_RECORD_BYTES
        ):
            raise ValueError("dense-index record size is invalid")
        digest_path = record_path.with_suffix(".sha256")
        if (
            digest_path.is_symlink()
            or _is_reparse_point(digest_path)
            or not digest_path.is_file()
        ):
            raise ValueError("dense-index record digest is unavailable")
        expected = digest_path.read_text(
            encoding="ascii", errors="strict"
        ).strip()
        if (
            _SHA256_PATTERN.fullmatch(expected) is None
            or hashlib.sha256(raw).hexdigest() != expected
        ):
            raise ValueError("dense-index record digest does not match")
        return raw

    def _validate_persisted_result(
        self,
        raw: object,
        *,
        job_id: UUID,
        project_id: UUID,
        artifact_directory: Path,
        source_sha256: str,
        source_size_bytes: int,
        source_modified_time_ns: int,
        sampling_fps: int,
    ) -> dict[str, object]:
        del project_id
        if not isinstance(raw, dict):
            raise ValueError("dense-index persisted result is invalid")
        expected_keys = {
            "artifact_set_id",
            "frame_count",
            "total_frame_bytes",
            "index",
            "frames",
            "provenance",
            "safety",
        }
        if set(raw) != expected_keys:
            raise ValueError("dense-index persisted result keys are invalid")
        if (
            raw.get("artifact_set_id") != str(job_id)
            or not _valid_safety(raw.get("safety"))
            or not isinstance(raw.get("frames"), list)
            or not isinstance(raw.get("frame_count"), int)
            or isinstance(raw.get("frame_count"), bool)
            or raw["frame_count"] != len(raw["frames"])
            or not 1 <= raw["frame_count"] <= self._base_limits.max_frames
            or not isinstance(raw.get("total_frame_bytes"), int)
            or isinstance(raw.get("total_frame_bytes"), bool)
            or raw["total_frame_bytes"] <= 0
            or not isinstance(raw.get("provenance"), dict)
            or not isinstance(raw.get("index"), dict)
        ):
            raise ValueError("dense-index persisted result summary is invalid")
        provenance = raw["provenance"]
        if (
            provenance.get("schema_version") != FRAME_INDEX_SCHEMA
            or provenance.get("source_sha256") != source_sha256
            or provenance.get("source_size_bytes") != source_size_bytes
            or provenance.get("source_modified_time_ns")
            != source_modified_time_ns
            or provenance.get("sampling_fps") != sampling_fps
            or provenance.get("frame_count") != raw["frame_count"]
            or not _valid_safety(provenance.get("review_policy"))
        ):
            raise ValueError(
                "dense-index persisted provenance is invalid"
            )
        index = raw["index"]
        expected_index_keys = {
            "relative_path",
            "size_bytes",
            "modified_time_ns",
            "sha256",
            "media_type",
            "artifact_ref",
        }
        if (
            set(index) != expected_index_keys
            or index.get("relative_path") != "frame-index.json"
            or index.get("media_type") != "application/json"
            or index.get("artifact_ref")
            != f"dense-index:{job_id}:frame-index"
            or not isinstance(index.get("size_bytes"), int)
            or isinstance(index.get("size_bytes"), bool)
            or not 1 <= index["size_bytes"] <= self._base_limits.max_index_bytes
            or not isinstance(index.get("modified_time_ns"), int)
            or isinstance(index.get("modified_time_ns"), bool)
            or index["modified_time_ns"] < 0
            or not isinstance(index.get("sha256"), str)
            or _SHA256_PATTERN.fullmatch(index["sha256"]) is None
        ):
            raise ValueError(
                "dense-index persisted index metadata is invalid"
            )
        index_path = artifact_directory / "frame-index.json"
        actual_index_hash, actual_index_size, actual_index_mtime = (
            _hash_regular_file(
                index_path,
                maximum_bytes=self._base_limits.max_index_bytes,
                expected_size=index["size_bytes"],
            )
        )
        if (
            actual_index_hash != index["sha256"]
            or actual_index_size != index["size_bytes"]
            or actual_index_mtime != index["modified_time_ns"]
        ):
            raise ValueError(
                "dense-index persisted index failed integrity verification"
            )

        expected_frame_keys = {
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
            "modified_time_ns",
            "artifact_ref",
        }
        frame_ids = set()
        total_frame_bytes = 0
        index_frames = []
        for expected_index, frame in enumerate(raw["frames"]):
            if not isinstance(frame, dict) or set(frame) != expected_frame_keys:
                raise ValueError(
                    "dense-index persisted frame keys are invalid"
                )
            relative_path = f"frames/frame-{expected_index:08d}.jpg"
            timestamp = expected_index / sampling_fps
            if (
                frame.get("frame_index") != expected_index
                or frame.get("relative_path") != relative_path
                or _FRAME_RELATIVE_PATTERN.fullmatch(relative_path) is None
                or not isinstance(frame.get("frame_id"), str)
                or FRAME_ID_PATTERN.fullmatch(frame["frame_id"]) is None
                or frame["frame_id"] in frame_ids
                or frame.get("timestamp_numerator") != expected_index
                or frame.get("timestamp_denominator") != sampling_fps
                or not isinstance(frame.get("timestamp_seconds"), (int, float))
                or isinstance(frame.get("timestamp_seconds"), bool)
                or not math.isfinite(float(frame["timestamp_seconds"]))
                or abs(float(frame["timestamp_seconds"]) - timestamp) > 0.000001
                or frame.get("media_type") != "image/jpeg"
                or frame.get("evidence_status") != "unreviewed_candidate"
                or not isinstance(frame.get("size_bytes"), int)
                or isinstance(frame.get("size_bytes"), bool)
                or not 1 <= frame["size_bytes"]
                <= self._base_limits.max_frame_bytes
                or not isinstance(frame.get("sha256"), str)
                or _SHA256_PATTERN.fullmatch(frame["sha256"]) is None
                or not isinstance(frame.get("modified_time_ns"), int)
                or isinstance(frame.get("modified_time_ns"), bool)
                or frame["modified_time_ns"] < 0
                or frame.get("artifact_ref")
                != f"dense-index:{job_id}:{frame['frame_id']}"
            ):
                raise ValueError(
                    "dense-index persisted frame metadata is invalid"
                )
            frame_ids.add(frame["frame_id"])
            frame_path = artifact_directory / relative_path
            try:
                resolved_frame = frame_path.resolve(strict=True)
                metadata = resolved_frame.stat()
            except OSError as error:
                raise ValueError(
                    "dense-index persisted frame is unavailable"
                ) from error
            if (
                resolved_frame.parent != artifact_directory / "frames"
                or frame_path.is_symlink()
                or _is_reparse_point(frame_path)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != frame["size_bytes"]
                or metadata.st_mtime_ns != frame["modified_time_ns"]
            ):
                raise ValueError(
                    "dense-index persisted frame integrity changed"
                )
            total_frame_bytes += int(metadata.st_size)
            index_frames.append(
                {
                    key: value
                    for key, value in frame.items()
                    if key not in {"modified_time_ns", "artifact_ref"}
                }
            )
        if (
            total_frame_bytes != raw["total_frame_bytes"]
            or total_frame_bytes + index["size_bytes"]
            > self._base_limits.max_total_output_bytes
        ):
            raise ValueError(
                "dense-index persisted artifact totals are invalid"
            )
        index_payload = _strict_json_object(
            index_path.read_bytes(), "persisted dense-frame index"
        )
        if index_payload != {
            "schema_version": FRAME_INDEX_SCHEMA,
            "frame_count": raw["frame_count"],
            "frames": index_frames,
            "provenance": provenance,
        }:
            raise ValueError(
                "dense-index persisted index content is invalid"
            )
        return raw

    @staticmethod
    def _discard_run_directory(
        run_directory: Path, output_root: Path
    ) -> None:
        if (
            run_directory.parent != output_root
            or _RUN_DIRECTORY_PATTERN.fullmatch(run_directory.name) is None
            or not run_directory.exists()
        ):
            return
        try:
            if run_directory.is_symlink() or _is_reparse_point(run_directory):
                os.rmdir(run_directory)
            else:
                shutil.rmtree(run_directory)
        except OSError:
            pass

    def _job(
        self, project_id: UUID, job_id: UUID
    ) -> DenseIndexJobRecord:
        project_id = self._validate_project_id(project_id)
        if not isinstance(job_id, UUID):
            raise ValueError("job_id must be a UUID")
        job = self._jobs.get(job_id)
        if job is None or job.project_id != project_id:
            raise DenseIndexJobNotFound(
                "The dense-index job was not found in this project"
            )
        return job

    def _evict_terminal_locked(self) -> None:
        if len(self._jobs) < MAX_DENSE_INDEX_JOB_RECORDS:
            return
        for job_id, job in tuple(self._jobs.items()):
            if job.status in _TERMINAL_STATES:
                self._jobs.pop(job_id, None)
                if len(self._jobs) < MAX_DENSE_INDEX_JOB_RECORDS:
                    return

    @staticmethod
    def _serialize(job: DenseIndexJobRecord) -> dict[str, object]:
        if job.status == "queued":
            progress_percent: Optional[int] = 0
            progress_state = "waiting"
        elif job.stage == "resolving_source":
            progress_percent = 5
            progress_state = "determinate"
        elif job.stage == "finalizing":
            progress_percent = 95
            progress_state = "determinate"
        elif job.status == "completed":
            progress_percent = 100
            progress_state = "determinate"
        elif job.stage in {"indexing_frames", "cancelling"}:
            progress_percent = None
            progress_state = "indeterminate"
        else:
            progress_percent = None
            progress_state = "terminal"
        return {
            "job_id": job.job_id,
            "project_id": job.project_id,
            "opaque_source_identity": job.opaque_source_identity,
            "source_sha256": job.source_sha256,
            "sampling_fps": job.sampling_fps,
            "status": job.status,
            "stage": job.stage,
            "progress_percent": progress_percent,
            "progress_state": progress_state,
            "progress_basis": (
                "stage_only_no_fabricated_frame_progress"
            ),
            "pause_supported": False,
            "error_code": job.error_code,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "record_sha256": job.record_sha256,
            "safety": _review_safety_dict(),
            "result": job.result,
        }


__all__ = [
    "DENSE_INDEX_JOB_SCHEMA",
    "DenseIndexJobConflict",
    "DenseIndexJobNotFound",
    "DenseIndexJobService",
    "DenseIndexJobServiceError",
    "DenseIndexJobSourceRejected",
    "DenseIndexJobUnavailable",
    "ResolvedDenseIndexArtifact",
    "ResolvedDenseIndexSource",
]
