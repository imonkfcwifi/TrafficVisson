"""Safe, bounded dense-frame indexing for review-only vision workflows.

The application bridge resolves project authorization before calling this
module.  This module then enforces the remaining filesystem and media-tool
contract:

* sampling is restricted to an integer 1--5 FPS;
* the registered source SHA-256 is verified with a read-only file handle;
* source identity is checked before and after every media operation;
* only absolute, allowlisted ``ffmpeg``/``ffprobe`` executables are launched,
  with an argument vector and ``shell=False``;
* all writes are confined to a new run directory below an existing,
  caller-selected subdirectory of the authorized project root;
* process time, captured output, frame count, per-frame bytes, total bytes,
  dimensions, and index bytes are bounded;
* generated frames are evidence candidates for user review only.  This module
  never creates incidents, annotations, maintenance decisions, training
  examples, or model promotions.

The module intentionally has no dependency on ``main.py`` or an analysis job
store.  It is a reusable lower-level contract which those layers may call only
after project and source authorization has succeeded.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple, Union
from uuid import UUID, uuid4

from .candidate_extraction import _media_tool_path


FRAME_INDEX_SCHEMA = "trafficvisson.dense-frame-index.v1"
DENSE_FRAME_CAPACITY_PLAN_SCHEMA = (
    "trafficvisson.dense-frame-capacity-plan.v1"
)
FRAME_ID_PATTERN = re.compile(r"^[a-f0-9]{24}$")
FRAME_FILE_PATTERN = re.compile(r"^frame-(?P<index>[0-9]{8})\.jpg$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RUN_DIRECTORY_PATTERN = re.compile(
    r"^dense-frame-index-[A-Za-z0-9._-]{6,128}$"
)
_ALLOWED_VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mts", ".webm"}
)
_ALLOWED_EXECUTABLE_NAMES = frozenset(
    {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}
)
_MAX_ARGUMENT_LENGTH = 32_768
_MAX_ARGUMENT_VECTOR_LENGTH = 128 * 1024
_MAX_TOOL_VERSION_LENGTH = 256
_READ_CHUNK_BYTES = 4 * 1024 * 1024
_PROCESS_POLL_SECONDS = 0.05
_DENSE_STAGING_ROOT_SCHEMA = "trafficvisson.dense-staging-root.v1"
_DENSE_STAGING_CHILD_SCHEMA = "trafficvisson.dense-staging-child.v1"
_DENSE_STAGING_ROOT_MARKER = ".trafficvisson-dense-staging.json"
_DENSE_STAGING_CHILD_MARKER = "ownership.json"
_DENSE_STAGING_CHILD_PATTERN = re.compile(
    r"^job-(?P<job_id>[a-f0-9-]{36})$"
)
_DENSE_PUBLISH_INCOMING_SCHEMA = (
    "trafficvisson.dense-publish-incoming.v1"
)
_DENSE_PUBLISH_INCOMING_MARKER = "ownership.json"
_MAX_OWNERSHIP_MARKER_BYTES = 16 * 1024


def dense_staging_root_is_short(
    staging_root: Union[str, Path],
    *,
    windows: Optional[bool] = None,
) -> bool:
    """Return whether the deepest FFmpeg staging frame stays below MAX_PATH."""

    windows_runtime = os.name == "nt" if windows is None else windows
    if not windows_runtime:
        return True
    estimated_frame_path_length = (
        len(str(Path(staging_root)))
        + 1
        + len("job-00000000-0000-0000-0000-000000000000")
        + 1
        + len("dense-frame-index-")
        + 8
        + len(r"\frames\frame-00000000.jpg")
    )
    return estimated_frame_path_length < 240


def dense_frame_index_requires_short_staging(
    output_root: Union[str, Path],
    *,
    windows: Optional[bool] = None,
) -> bool:
    """Return whether FFmpeg 4.4 needs an explicit short-path staging root."""

    windows_runtime = os.name == "nt" if windows is None else windows
    if not windows_runtime:
        return False
    estimated_frame_path_length = (
        len(str(Path(output_root)))
        + 1
        + len("dense-frame-index-")
        + 8
        + len(r"\frames\frame-00000000.jpg")
    )
    return estimated_frame_path_length >= 240


class DenseFrameIndexError(RuntimeError):
    """Base class for deterministic dense-frame indexing failures."""


class DenseFrameIndexCancelled(DenseFrameIndexError):
    """Raised when the caller cancels an index build."""


class DenseFrameIndexTimeout(DenseFrameIndexError):
    """Raised when a command or the overall build exceeds its deadline."""


class DenseFrameIndexOutputLimit(DenseFrameIndexError):
    """Raised when an output, count, dimension, or disk limit is exceeded."""


class DenseFrameIndexSourceChanged(DenseFrameIndexError):
    """Raised when the protected source changes or its hash does not match."""


class DenseFrameIndexStagingUnavailable(DenseFrameIndexError):
    """Raised when required short-path staging is missing, unsafe, or too small."""


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_integrity_pair(path: Path, payload: Mapping[str, object]) -> None:
    body = dict(payload)
    wrapped = {
        **body,
        "integrity_sha256": hashlib.sha256(
            _canonical_json(body)
        ).hexdigest(),
    }
    _atomic_write_bytes(
        path,
        _canonical_json(wrapped),
    )


def _read_integrity_pair(path: Path) -> Mapping[str, object]:
    descriptor: Optional[int] = None
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or bool(
                getattr(before, "st_file_attributes", 0)
                & getattr(
                    stat,
                    "FILE_ATTRIBUTE_REPARSE_POINT",
                    0x400,
                )
            )
            or before.st_size < 2
            or before.st_size > _MAX_OWNERSHIP_MARKER_BYTES
        ):
            raise OSError("ownership marker is not a bounded regular file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_opened = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size < 2
            or opened.st_size > _MAX_OWNERSHIP_MARKER_BYTES
            or identity_opened != identity_before
        ):
            raise OSError("ownership marker identity changed")
        chunks = []
        remaining = _MAX_OWNERSHIP_MARKER_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = path.lstat()
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if (
            len(raw) > _MAX_OWNERSHIP_MARKER_BYTES
            or not stat.S_ISREG(after.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or bool(
                getattr(after, "st_file_attributes", 0)
                & getattr(
                    stat,
                    "FILE_ATTRIBUTE_REPARSE_POINT",
                    0x400,
                )
            )
            or identity_after != identity_opened
        ):
            raise OSError("ownership marker identity changed")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DenseFrameIndexStagingUnavailable(
            "Dense staging ownership metadata is invalid"
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if (
        not isinstance(payload, dict)
        or SHA256_PATTERN.fullmatch(
            str(payload.get("integrity_sha256", ""))
        )
        is None
    ):
        raise DenseFrameIndexStagingUnavailable(
            "Dense staging ownership metadata is invalid"
        )
    integrity_sha256 = str(payload.pop("integrity_sha256"))
    if hashlib.sha256(_canonical_json(payload)).hexdigest() != integrity_sha256:
        raise DenseFrameIndexStagingUnavailable(
            "Dense staging ownership metadata is invalid"
        )
    return payload


def initialize_dense_staging_root(value: Union[str, Path]) -> Path:
    """Create or revalidate one server-owned short-path staging root."""

    root = Path(value)
    if (
        not root.is_absolute()
        or root.is_symlink()
        or _path_chain_contains_link(root.parent)
    ):
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging root is unsafe"
        )
    parent = root.parent
    if (
        not parent.exists()
        or not parent.is_dir()
        or parent.is_symlink()
        or _is_reparse_point(parent)
    ):
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging root parent is unsafe"
        )
    root_preexisted = root.exists() or root.is_symlink()
    try:
        if root_preexisted:
            if not root.is_dir():
                raise OSError("dense staging root is not a directory")
        else:
            root.mkdir()
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging root is unavailable"
        ) from error
    if (
        resolved != root.resolve()
        or not resolved.is_dir()
        or resolved.is_symlink()
        or _is_reparse_point(resolved)
        or _path_chain_contains_link(root)
    ):
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging root is unsafe"
        )
    if not dense_staging_root_is_short(resolved):
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging root is not short enough for FFmpeg"
        )
    marker = resolved / _DENSE_STAGING_ROOT_MARKER
    payload = {
        "schema_version": _DENSE_STAGING_ROOT_SCHEMA,
        "owner": "trafficvisson-python-bridge",
        "path_exposed": False,
    }
    if marker.exists() or marker.is_symlink():
        existing = _read_integrity_pair(marker)
        if existing != payload:
            raise DenseFrameIndexStagingUnavailable(
                "The dense staging root ownership changed"
            )
    elif not root_preexisted:
        _write_integrity_pair(marker, payload)
    else:
        raise DenseFrameIndexStagingUnavailable(
            "The existing dense staging root is not owned by the bridge"
        )
    return resolved


def recover_dense_staging_root(value: Union[str, Path]) -> int:
    """Remove only valid direct-child staging leases left by a stopped bridge."""

    root = initialize_dense_staging_root(value)
    removed = 0
    allowed_root_entries = {
        _DENSE_STAGING_ROOT_MARKER,
    }
    for child in tuple(root.iterdir()):
        if child.name in allowed_root_entries:
            continue
        match = _DENSE_STAGING_CHILD_PATTERN.fullmatch(child.name)
        if (
            match is None
            or child.is_symlink()
            or _is_reparse_point(child)
            or not child.is_dir()
        ):
            raise DenseFrameIndexStagingUnavailable(
                "The dense staging root contains an unowned entry"
            )
        owner_id = str(UUID(match.group("job_id")))
        marker = child / _DENSE_STAGING_CHILD_MARKER
        payload = _read_integrity_pair(marker)
        if (
            payload.get("schema_version") != _DENSE_STAGING_CHILD_SCHEMA
            or payload.get("owner_id") != owner_id
            or payload.get("path_exposed") is not False
        ):
            raise DenseFrameIndexStagingUnavailable(
                "The dense staging child ownership is invalid"
            )
        try:
            shutil.rmtree(child)
        except OSError as error:
            raise DenseFrameIndexStagingUnavailable(
                "A stale dense staging child could not be removed"
            ) from error
        removed += 1
    return removed


@dataclass(frozen=True)
class DenseFrameIndexLimits:
    """Resource limits for one dense-frame index build.

    The default 30,000-frame cap supports up to 100 minutes at 5 FPS.  Callers
    may choose a lower cap for interactive work, but cannot exceed the hard
    contract limits validated here.
    """

    sampling_fps: int = 2
    max_frames: int = 30_000
    max_media_seconds: float = 6 * 60 * 60.0
    max_source_duration_seconds: float = 24 * 60 * 60.0
    max_source_bytes: int = 256 * 1024**3
    max_frame_width: int = 1_600
    max_frame_pixels: int = 24_000_000
    max_frame_bytes: int = 24 * 1024 * 1024
    max_total_output_bytes: int = 16 * 1024**3
    min_free_disk_bytes: int = 512 * 1024 * 1024
    max_index_bytes: int = 64 * 1024 * 1024
    max_probe_output_bytes: int = 512 * 1024
    max_process_output_bytes: int = 2 * 1024 * 1024
    probe_timeout_seconds: float = 30.0
    extraction_timeout_seconds: float = 4 * 60 * 60.0
    max_total_seconds: float = 6 * 60 * 60.0

    def __post_init__(self) -> None:
        integer_ranges = (
            ("sampling_fps", self.sampling_fps, 1, 5),
            ("max_frames", self.max_frames, 1, 100_000),
            ("max_source_bytes", self.max_source_bytes, 1, 512 * 1024**3),
            ("max_frame_width", self.max_frame_width, 160, 8_192),
            ("max_frame_pixels", self.max_frame_pixels, 10_000, 67_108_864),
            ("max_frame_bytes", self.max_frame_bytes, 1_024, 256 * 1024 * 1024),
            (
                "max_total_output_bytes",
                self.max_total_output_bytes,
                2_048,
                64 * 1024**3,
            ),
            (
                "min_free_disk_bytes",
                self.min_free_disk_bytes,
                0,
                8 * 1024**4,
            ),
            (
                "max_index_bytes",
                self.max_index_bytes,
                1_024,
                512 * 1024 * 1024,
            ),
            (
                "max_probe_output_bytes",
                self.max_probe_output_bytes,
                1_024,
                16 * 1024 * 1024,
            ),
            (
                "max_process_output_bytes",
                self.max_process_output_bytes,
                1_024,
                64 * 1024 * 1024,
            ),
        )
        for name, value, minimum, maximum in integer_ranges:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")

        numeric_ranges = (
            ("max_media_seconds", self.max_media_seconds, 0.1, 24 * 60 * 60.0),
            (
                "max_source_duration_seconds",
                self.max_source_duration_seconds,
                0.1,
                31 * 24 * 60 * 60.0,
            ),
            ("probe_timeout_seconds", self.probe_timeout_seconds, 0.1, 300.0),
            (
                "extraction_timeout_seconds",
                self.extraction_timeout_seconds,
                1.0,
                24 * 60 * 60.0,
            ),
            ("max_total_seconds", self.max_total_seconds, 1.0, 48 * 60 * 60.0),
        )
        for name, value, minimum, maximum in numeric_ranges:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        if self.max_media_seconds > self.max_source_duration_seconds:
            raise ValueError(
                "max_media_seconds cannot exceed max_source_duration_seconds"
            )
        if self.max_index_bytes >= self.max_total_output_bytes:
            raise ValueError(
                "max_total_output_bytes must leave room for frame artifacts"
            )


@dataclass(frozen=True)
class DenseFrameIndexCapacityPlan:
    """Path-free immutable writer bound computed before any artifact writes."""

    schema_version: str
    source_sha256: str
    source_duration_seconds: float
    indexed_duration_seconds: float
    sampling_fps: int
    planned_frame_count: int
    planned_frame_bytes_upper_bound: int
    planned_output_bytes_upper_bound: int
    hard_ceiling_bytes: int

    def capacity_revision(self) -> dict[str, Union[str, int, float]]:
        payload: dict[str, Union[str, int, float]] = {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "source_duration_seconds": self.source_duration_seconds,
            "indexed_duration_seconds": self.indexed_duration_seconds,
            "sampling_fps": self.sampling_fps,
            "planned_frame_count": self.planned_frame_count,
            "planned_frame_bytes_upper_bound": (
                self.planned_frame_bytes_upper_bound
            ),
            "planned_output_bytes_upper_bound": (
                self.planned_output_bytes_upper_bound
            ),
            "hard_ceiling_bytes": self.hard_ceiling_bytes,
        }
        revision_sha256 = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return {**payload, "revision_sha256": revision_sha256}


def build_dense_frame_capacity_plan(
    *,
    source_sha256: str,
    duration_seconds: float,
    limits: DenseFrameIndexLimits,
) -> DenseFrameIndexCapacityPlan:
    """Return the exact upper bound enforced by ``DenseFrameIndexEngine``.

    The caller may use a conservative intake duration. The writer is then
    capped to ``planned_output_bytes_upper_bound``, so a later probe cannot
    write beyond the amount accepted by the durable capacity reservation.
    """

    if (
        not isinstance(source_sha256, str)
        or SHA256_PATTERN.fullmatch(source_sha256) is None
    ):
        raise ValueError("source_sha256 is invalid")
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
        or not 0 < float(duration_seconds)
        <= float(limits.max_source_duration_seconds)
    ):
        raise ValueError("duration_seconds is outside the supported range")
    source_duration_value = float(duration_seconds)
    indexed_duration_value = min(
        source_duration_value,
        float(limits.max_media_seconds),
    )
    planned_frame_count = min(
        limits.max_frames,
        max(
            1,
            math.ceil(
                indexed_duration_value * limits.sampling_fps
            )
            + 1,
        ),
    )
    planned_frame_bytes = min(
        limits.max_total_output_bytes,
        planned_frame_count * limits.max_frame_bytes,
    )
    planned_output_bytes = min(
        limits.max_total_output_bytes,
        planned_frame_bytes + limits.max_index_bytes,
    )
    return DenseFrameIndexCapacityPlan(
        schema_version=DENSE_FRAME_CAPACITY_PLAN_SCHEMA,
        source_sha256=source_sha256,
        source_duration_seconds=round(source_duration_value, 6),
        indexed_duration_seconds=round(indexed_duration_value, 6),
        sampling_fps=limits.sampling_fps,
        planned_frame_count=planned_frame_count,
        planned_frame_bytes_upper_bound=planned_frame_bytes,
        planned_output_bytes_upper_bound=planned_output_bytes,
        hard_ceiling_bytes=limits.max_total_output_bytes,
    )


@dataclass(frozen=True)
class ReviewOnlyPolicy:
    """Machine-readable guardrails carried into every frame index."""

    review_only: bool = True
    automatic_incident_creation: bool = False
    automatic_annotation_creation: bool = False
    automatic_training_ingest: bool = False
    automatic_maintenance_decision: bool = False
    automatic_model_promotion: bool = False


@dataclass(frozen=True)
class DenseFrameRecord:
    frame_id: str
    frame_index: int
    timestamp_seconds: float
    timestamp_numerator: int
    timestamp_denominator: int
    relative_path: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    media_type: str = "image/jpeg"
    evidence_status: str = "unreviewed_candidate"


@dataclass(frozen=True)
class DenseFrameIndexProvenance:
    schema_version: str
    source_name: str
    source_size_bytes: int
    source_modified_time_ns: int
    source_sha256: str
    source_access: str
    duration_seconds: float
    indexed_duration_seconds: float
    sampling_fps: int
    sampling_filter: str
    timestamp_method: str
    frame_id_method: str
    frame_count: int
    truncated: bool
    ffmpeg_version: str
    ffprobe_version: str
    output_scope: str
    limits: Mapping[str, Union[int, float]]
    review_policy: ReviewOnlyPolicy
    publisher_owner_id: Optional[str] = None


@dataclass(frozen=True)
class DenseFrameIndexResult:
    output_directory: str
    index_relative_path: str
    frames: Tuple[DenseFrameRecord, ...]
    provenance: DenseFrameIndexProvenance

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MediaCommand:
    argv: Tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    capture_limit_bytes: int
    watch_directory: Optional[Path] = None
    watch_limit_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        _validate_argv(self.argv)
        if not self.cwd.is_absolute() or not self.cwd.is_dir():
            raise ValueError("The media command working directory is invalid")
        if self.cwd.is_symlink() or _is_reparse_point(self.cwd):
            raise ValueError("The media command working directory is unsafe")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("The media command timeout is invalid")
        if (
            isinstance(self.capture_limit_bytes, bool)
            or not isinstance(self.capture_limit_bytes, int)
            or self.capture_limit_bytes <= 0
        ):
            raise ValueError("The media command capture limit is invalid")
        if (self.watch_directory is None) != (self.watch_limit_bytes is None):
            raise ValueError("The media command watch bound is incomplete")
        if self.watch_directory is not None:
            if (
                not self.watch_directory.is_absolute()
                or not self.watch_directory.is_dir()
                or self.watch_directory.is_symlink()
                or _is_reparse_point(self.watch_directory)
            ):
                raise ValueError("The media command watch directory is unsafe")
            if (
                isinstance(self.watch_limit_bytes, bool)
                or not isinstance(self.watch_limit_bytes, int)
                or self.watch_limit_bytes <= 0
            ):
                raise ValueError("The media command watch limit is invalid")


@dataclass(frozen=True)
class MediaCommandOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class _SourceIdentity:
    size_bytes: int
    modified_time_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class _VideoProbe:
    duration_seconds: float
    stream_index: int
    width: int
    height: int
    codec_name: str
    format_names: Tuple[str, ...]


CancelCallback = Callable[[], bool]
CommandRunner = Callable[
    [MediaCommand, Optional[CancelCallback]], MediaCommandOutput
]


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _path_chain_contains_link(path: Path) -> bool:
    if not path.is_absolute():
        return True
    parts = path.parts
    if not parts:
        return True
    current = Path(path.anchor)
    for part in parts[1:]:
        current = current / part
        if not current.exists():
            continue
        if current.is_symlink() or _is_reparse_point(current):
            return True
    return False


def _storage_volume_identity(path: Path) -> tuple[str, object]:
    drive = path.drive.casefold()
    if drive:
        return ("drive", drive)
    existing = path
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    resolved = existing.resolve(strict=True)
    return ("device", resolved.stat().st_dev)


def same_storage_volume(first: Path, second: Path) -> bool:
    try:
        return _storage_volume_identity(
            first
        ) == _storage_volume_identity(second)
    except OSError:
        return False


def _cancel_requested(cancel: Optional[CancelCallback]) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception as error:
        raise DenseFrameIndexError("The cancellation callback failed") from error


def _validate_argv(argv: Sequence[str]) -> Tuple[str, ...]:
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("A non-empty media-tool argument vector is required")
    normalized = []
    total_length = 0
    for argument in argv:
        if (
            not isinstance(argument, str)
            or "\x00" in argument
            or len(argument) > _MAX_ARGUMENT_LENGTH
        ):
            raise ValueError("The media-tool argument vector is invalid")
        normalized.append(argument)
        total_length += len(argument)
    executable = Path(normalized[0])
    if (
        not executable.is_absolute()
        or executable.name.casefold() not in _ALLOWED_EXECUTABLE_NAMES
        or not executable.is_file()
        or executable.is_symlink()
        or _is_reparse_point(executable)
    ):
        raise ValueError("The media-tool executable is invalid")
    if total_length > _MAX_ARGUMENT_VECTOR_LENGTH:
        raise ValueError("The media-tool argument vector is too large")
    return tuple(normalized)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2.0)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2.0)
        except Exception:
            pass


def _directory_size(path: Path, maximum: int) -> int:
    total = 0
    for current, directories, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink() or _is_reparse_point(current_path):
            raise DenseFrameIndexError("The frame output contains a linked directory")
        for name in directories:
            child = current_path / name
            if child.is_symlink() or _is_reparse_point(child):
                raise DenseFrameIndexError(
                    "The frame output contains a linked directory"
                )
        for name in files:
            child = current_path / name
            if child.is_symlink() or _is_reparse_point(child):
                raise DenseFrameIndexError("The frame output contains a linked file")
            try:
                total += child.stat().st_size
            except OSError as error:
                raise DenseFrameIndexError(
                    "The frame output changed while it was measured"
                ) from error
            if total > maximum:
                raise DenseFrameIndexOutputLimit(
                    "The dense-frame output exceeded its storage bound"
                )
    return total


def _run_bounded_command(
    command: MediaCommand,
    cancel: Optional[CancelCallback],
) -> MediaCommandOutput:
    """Run one allowlisted media command with bounded capture and writes."""

    argv = _validate_argv(command.argv)
    if _cancel_requested(cancel):
        raise DenseFrameIndexCancelled("Dense-frame indexing was cancelled")
    stdout_path = command.cwd / f".media-{uuid4().hex}.stdout"
    stderr_path = command.cwd / f".media-{uuid4().hex}.stderr"
    process: Optional[subprocess.Popen[bytes]] = None
    deadline = time.monotonic() + float(command.timeout_seconds)
    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    try:
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            process = subprocess.Popen(
                list(argv),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=str(command.cwd),
                close_fds=True,
                creationflags=creation_flags,
            )
            while process.poll() is None:
                if _cancel_requested(cancel):
                    _terminate_process(process)
                    raise DenseFrameIndexCancelled(
                        "Dense-frame indexing was cancelled"
                    )
                if time.monotonic() > deadline:
                    _terminate_process(process)
                    raise DenseFrameIndexTimeout(
                        "A media command exceeded its time bound"
                    )
                stdout.flush()
                stderr.flush()
                if (
                    stdout_path.stat().st_size + stderr_path.stat().st_size
                    > command.capture_limit_bytes
                ):
                    _terminate_process(process)
                    raise DenseFrameIndexOutputLimit(
                        "A media command exceeded its captured-output bound"
                    )
                if (
                    command.watch_directory is not None
                    and command.watch_limit_bytes is not None
                ):
                    _directory_size(
                        command.watch_directory,
                        command.watch_limit_bytes,
                    )
                time.sleep(_PROCESS_POLL_SECONDS)
        stdout_raw = stdout_path.read_bytes()
        stderr_raw = stderr_path.read_bytes()
        if (
            len(stdout_raw) + len(stderr_raw)
            > command.capture_limit_bytes
        ):
            raise DenseFrameIndexOutputLimit(
                "A media command exceeded its captured-output bound"
            )
        return MediaCommandOutput(
            returncode=int(process.returncode),
            stdout=stdout_raw,
            stderr=stderr_raw,
        )
    except OSError as error:
        if process is not None and process.poll() is None:
            _terminate_process(process)
        raise DenseFrameIndexError(
            "A media command could not be executed"
        ) from error
    finally:
        for path in (stdout_path, stderr_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _resolve_executable(
    value: Optional[Union[str, Path]], tool_name: str
) -> str:
    if value is None:
        resolved_text = shutil.which(tool_name)
    else:
        candidate = Path(value)
        resolved_text = (
            str(candidate)
            if candidate.is_absolute()
            else shutil.which(str(value))
        )
    if not resolved_text:
        raise DenseFrameIndexError(f"{tool_name} is unavailable")
    resolved = Path(resolved_text)
    try:
        resolved = resolved.resolve(strict=True)
    except OSError as error:
        raise DenseFrameIndexError(f"{tool_name} is unavailable") from error
    try:
        _validate_argv((str(resolved), "-version"))
    except ValueError as error:
        raise DenseFrameIndexError(f"{tool_name} is unsafe") from error
    return str(resolved)


def _source_identity(path: Path) -> _SourceIdentity:
    try:
        metadata = path.stat()
    except OSError as error:
        raise DenseFrameIndexSourceChanged(
            "The protected source is unavailable"
        ) from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size <= 0
        or metadata.st_nlink != 1
    ):
        raise DenseFrameIndexSourceChanged(
            "The protected source is not a private regular file"
        )
    return _SourceIdentity(
        size_bytes=int(metadata.st_size),
        modified_time_ns=int(metadata.st_mtime_ns),
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
    )


def _validated_source(
    value: Union[str, Path], maximum_bytes: int
) -> tuple[Path, _SourceIdentity]:
    path = Path(value)
    if (
        not path.is_absolute()
        or path.suffix.casefold() not in _ALLOWED_VIDEO_EXTENSIONS
        or path.is_symlink()
        or _is_reparse_point(path)
    ):
        raise ValueError("An absolute, non-linked source video is required")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError("The source video is unavailable") from error
    identity = _source_identity(resolved)
    if identity.size_bytes > maximum_bytes:
        raise DenseFrameIndexOutputLimit(
            "The source video exceeds its configured size bound"
        )
    return resolved, identity


def _validated_project_output(
    project_root_value: Union[str, Path],
    output_directory_value: Union[str, Path],
) -> tuple[Path, Path]:
    project_root = Path(project_root_value)
    output_directory = Path(output_directory_value)
    if (
        not project_root.is_absolute()
        or not output_directory.is_absolute()
        or project_root.is_symlink()
        or output_directory.is_symlink()
        or _is_reparse_point(project_root)
        or _is_reparse_point(output_directory)
    ):
        raise ValueError(
            "Absolute, non-linked project and output directories are required"
        )
    try:
        resolved_project = project_root.resolve(strict=True)
        resolved_output = output_directory.resolve(strict=True)
    except OSError as error:
        raise ValueError("The project output directory is unavailable") from error
    if not resolved_project.is_dir() or not resolved_output.is_dir():
        raise ValueError("The project output path must be an existing directory")
    try:
        relative = resolved_output.relative_to(resolved_project)
    except ValueError as error:
        raise ValueError(
            "The output directory must be inside the authorized project root"
        ) from error
    if not relative.parts:
        raise ValueError(
            "The output directory must be a project subdirectory, not the root"
        )
    current = resolved_project
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or _is_reparse_point(current):
            raise ValueError("The project output path contains a linked directory")
    return resolved_project, resolved_output


def _validated_short_staging_root(
    value: Optional[Union[str, Path]],
    *,
    required_free_bytes: int,
) -> Path:
    if value is None:
        raise DenseFrameIndexStagingUnavailable(
            "Short-path dense staging is required but not configured"
        )
    root = Path(value)
    if (
        not root.is_absolute()
        or root.is_symlink()
        or _is_reparse_point(root)
        or _path_chain_contains_link(root)
    ):
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging root is unsafe"
        )
    try:
        resolved = root.resolve(strict=True)
        free_bytes = int(shutil.disk_usage(resolved).free)
    except (OSError, TypeError, ValueError) as error:
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging root is unavailable"
        ) from error
    if (
        not resolved.is_dir()
        or resolved.is_symlink()
        or _is_reparse_point(resolved)
        or _path_chain_contains_link(root)
    ):
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging root is unsafe"
        )
    if not dense_staging_root_is_short(resolved):
        raise DenseFrameIndexStagingUnavailable(
            "The configured dense staging root is not short enough for FFmpeg"
        )
    if free_bytes < required_free_bytes:
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging volume cannot fit the accepted writer bound"
        )
    marker = resolved / _DENSE_STAGING_ROOT_MARKER
    payload = _read_integrity_pair(marker)
    if (
        payload.get("schema_version") != _DENSE_STAGING_ROOT_SCHEMA
        or payload.get("owner") != "trafficvisson-python-bridge"
        or payload.get("path_exposed") is not False
    ):
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging root ownership is invalid"
        )
    return resolved


def _create_owned_staging_child(root: Path, owner_id: str) -> Path:
    try:
        canonical_owner = str(UUID(owner_id))
    except (TypeError, ValueError) as error:
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging owner is invalid"
        ) from error
    child = root / f"job-{canonical_owner}"
    if child.exists() or child.is_symlink():
        raise DenseFrameIndexStagingUnavailable(
            "A dense staging lease already exists for this job"
        )
    try:
        child.mkdir()
        _write_integrity_pair(
            child / _DENSE_STAGING_CHILD_MARKER,
            {
                "schema_version": _DENSE_STAGING_CHILD_SCHEMA,
                "owner_id": canonical_owner,
                "path_exposed": False,
            },
        )
    except OSError as error:
        try:
            child.rmdir()
        except OSError:
            pass
        raise DenseFrameIndexStagingUnavailable(
            "The dense staging lease could not be created"
        ) from error
    return child


def _cleanup_owned_staging_child(
    child: Optional[Path],
    root: Optional[Path],
    owner_id: Optional[str],
) -> None:
    if child is None or root is None or owner_id is None:
        return
    try:
        canonical_owner = str(UUID(owner_id))
        if (
            child.parent != root
            or child.name != f"job-{canonical_owner}"
            or child.is_symlink()
            or _is_reparse_point(child)
            or not child.is_dir()
        ):
            return
        payload = _read_integrity_pair(
            child / _DENSE_STAGING_CHILD_MARKER
        )
        if (
            payload.get("schema_version") != _DENSE_STAGING_CHILD_SCHEMA
            or payload.get("owner_id") != canonical_owner
            or payload.get("path_exposed") is not False
        ):
            return
        shutil.rmtree(child)
    except (OSError, TypeError, ValueError, DenseFrameIndexStagingUnavailable):
        return


def _remaining_timeout(
    deadline: float, configured_seconds: float
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DenseFrameIndexTimeout(
            "Dense-frame indexing exceeded its total time bound"
        )
    return min(float(configured_seconds), remaining)


def _verify_source_sha256(
    path: Path,
    expected_sha256: str,
    expected_identity: _SourceIdentity,
    *,
    deadline: float,
    cancel: Optional[CancelCallback],
) -> None:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(_READ_CHUNK_BYTES):
                if _cancel_requested(cancel):
                    raise DenseFrameIndexCancelled(
                        "Dense-frame indexing was cancelled"
                    )
                if time.monotonic() > deadline:
                    raise DenseFrameIndexTimeout(
                        "Source verification exceeded the total time bound"
                    )
                total += len(block)
                digest.update(block)
    except OSError as error:
        raise DenseFrameIndexSourceChanged(
            "The protected source could not be read"
        ) from error
    if (
        total != expected_identity.size_bytes
        or _source_identity(path) != expected_identity
        or digest.hexdigest() != expected_sha256
    ):
        raise DenseFrameIndexSourceChanged(
            "The protected source does not match its registered SHA-256"
        )


def _strict_json_object(raw: bytes, label: str) -> dict[str, object]:
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DenseFrameIndexError(
                    f"{label} contains duplicate JSON keys"
                )
            result[key] = value
        return result

    def reject_constant(_):
        raise DenseFrameIndexError(
            f"{label} contains a non-finite JSON number"
        )

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except DenseFrameIndexError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DenseFrameIndexError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise DenseFrameIndexError(f"{label} must be a JSON object")
    return value


def _parse_tool_version(
    output: MediaCommandOutput, tool_name: str
) -> str:
    if output.returncode != 0:
        raise DenseFrameIndexError(f"{tool_name} version check failed")
    lines = output.stdout.decode("utf-8", errors="replace").splitlines()
    if not lines:
        raise DenseFrameIndexError(f"{tool_name} version output is invalid")
    value = lines[0].strip()
    if (
        not value
        or len(value) > _MAX_TOOL_VERSION_LENGTH
        or not value.casefold().startswith(f"{tool_name} version")
    ):
        raise DenseFrameIndexError(f"{tool_name} version output is invalid")
    return value


def _parse_probe(
    raw: bytes,
    source_identity: _SourceIdentity,
    limits: DenseFrameIndexLimits,
) -> _VideoProbe:
    payload = _strict_json_object(raw, "ffprobe output")
    raw_format = payload.get("format")
    streams = payload.get("streams")
    if not isinstance(raw_format, dict) or not isinstance(streams, list):
        raise DenseFrameIndexError("ffprobe returned an invalid video envelope")
    if len(streams) != 1 or not isinstance(streams[0], dict):
        raise DenseFrameIndexError(
            "The source must expose exactly one selected video stream"
        )
    stream = streams[0]
    if stream.get("codec_type") not in (None, "video"):
        raise DenseFrameIndexError("ffprobe did not select a video stream")
    try:
        reported_size = int(raw_format["size"])
        duration = float(raw_format["duration"])
        stream_index = int(stream["index"])
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise DenseFrameIndexError("ffprobe returned invalid numeric metadata") from error
    codec_name = stream.get("codec_name")
    format_name = raw_format.get("format_name")
    if (
        isinstance(reported_size, bool)
        or reported_size != source_identity.size_bytes
        or not math.isfinite(duration)
        or not 0 < duration <= limits.max_source_duration_seconds
        or isinstance(stream_index, bool)
        or not 0 <= stream_index <= 127
        or isinstance(width, bool)
        or isinstance(height, bool)
        or not 1 <= width <= 8_192
        or not 1 <= height <= 8_192
        # The source may be larger than the indexed frame.  The ffmpeg scale
        # contract below applies ``max_frame_pixels`` to emitted evidence;
        # probing only enforces the bridge's hard 8K/64-megapixel decode cap.
        or width * height > 67_108_864
        or not isinstance(codec_name, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", codec_name)
        or not isinstance(format_name, str)
        or len(format_name) > 256
    ):
        raise DenseFrameIndexError("ffprobe returned unsupported video metadata")
    format_names = tuple(
        item for item in dict.fromkeys(format_name.split(",")) if item
    )
    if not format_names or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item)
        for item in format_names
    ):
        raise DenseFrameIndexError("ffprobe returned an invalid format name")
    return _VideoProbe(
        duration_seconds=round(duration, 6),
        stream_index=stream_index,
        width=width,
        height=height,
        codec_name=codec_name,
        format_names=format_names,
    )


def _jpeg_dimensions(path: Path, maximum_bytes: int) -> tuple[int, int]:
    """Read bounded JPEG marker data and return width/height."""

    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    inspected = 0
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"\xff\xd8":
                raise DenseFrameIndexError("ffmpeg created a non-JPEG frame")
            inspected = 2
            while inspected < maximum_bytes:
                prefix = stream.read(1)
                inspected += len(prefix)
                if not prefix:
                    break
                if prefix != b"\xff":
                    continue
                marker_raw = stream.read(1)
                inspected += len(marker_raw)
                while marker_raw == b"\xff":
                    marker_raw = stream.read(1)
                    inspected += len(marker_raw)
                if not marker_raw:
                    break
                marker = marker_raw[0]
                if marker == 0xD9:
                    break
                if marker in {0x01, *range(0xD0, 0xD9)}:
                    continue
                length_raw = stream.read(2)
                inspected += len(length_raw)
                if len(length_raw) != 2:
                    break
                segment_length = int.from_bytes(length_raw, "big")
                if segment_length < 2 or inspected + segment_length - 2 > maximum_bytes:
                    raise DenseFrameIndexError(
                        "ffmpeg created an invalid JPEG frame"
                    )
                if marker in sof_markers:
                    header = stream.read(5)
                    inspected += len(header)
                    if len(header) != 5:
                        break
                    height = int.from_bytes(header[1:3], "big")
                    width = int.from_bytes(header[3:5], "big")
                    if width <= 0 or height <= 0:
                        break
                    return width, height
                stream.seek(segment_length - 2, os.SEEK_CUR)
                inspected += segment_length - 2
    except OSError as error:
        raise DenseFrameIndexError("A frame artifact could not be read") from error
    raise DenseFrameIndexError("ffmpeg created a JPEG without dimensions")


def _sha256_regular_file(
    path: Path, expected_size: int
) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(_READ_CHUNK_BYTES):
                total += len(block)
                digest.update(block)
        after = path.stat()
    except OSError as error:
        raise DenseFrameIndexError("A frame artifact could not be hashed") from error
    if (
        total != expected_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise DenseFrameIndexError(
            "A frame artifact changed while it was inspected"
        )
    return digest.hexdigest()


def _frame_id(source_sha256: str, fps: int, frame_index: int) -> str:
    material = (
        f"{FRAME_INDEX_SCHEMA}\0{source_sha256}\0"
        f"fps={fps}\0frame={frame_index}"
    )
    return hashlib.sha256(material.encode("ascii")).hexdigest()[:24]


def _frame_count_bounds(
    duration_seconds: float, fps: int, max_frames: int
) -> tuple[int, int]:
    nominal = duration_seconds * fps
    minimum_count = max(1, math.floor(nominal) - 1)
    if minimum_count >= max_frames:
        return max_frames, max_frames
    return (
        minimum_count,
        min(max_frames, math.ceil(nominal) + 1),
    )


def _validate_frames(
    frames_root: Path,
    *,
    source_sha256: str,
    indexed_duration_seconds: float,
    limits: DenseFrameIndexLimits,
) -> tuple[Tuple[DenseFrameRecord, ...], int]:
    try:
        children = list(frames_root.iterdir())
    except OSError as error:
        raise DenseFrameIndexError(
            "The frame output directory is unavailable"
        ) from error
    if any(child.is_dir() for child in children):
        raise DenseFrameIndexError(
            "ffmpeg created an unexpected frame output directory"
        )
    indexed: dict[int, Path] = {}
    for child in children:
        if child.is_symlink() or _is_reparse_point(child):
            raise DenseFrameIndexError("ffmpeg created a linked frame artifact")
        match = FRAME_FILE_PATTERN.fullmatch(child.name)
        if match is None:
            raise DenseFrameIndexError(
                "ffmpeg created an unexpected frame artifact"
            )
        frame_index = int(match.group("index"))
        if frame_index in indexed:
            raise DenseFrameIndexError("ffmpeg created duplicate frame indexes")
        indexed[frame_index] = child
    if not indexed:
        raise DenseFrameIndexError("ffmpeg created no frame artifacts")
    if len(indexed) > limits.max_frames:
        raise DenseFrameIndexOutputLimit(
            "ffmpeg exceeded the configured frame count"
        )
    expected_indexes = list(range(len(indexed)))
    if sorted(indexed) != expected_indexes:
        raise DenseFrameIndexError(
            "ffmpeg created a non-contiguous frame sequence"
        )
    minimum_count, maximum_count = _frame_count_bounds(
        indexed_duration_seconds,
        limits.sampling_fps,
        limits.max_frames,
    )
    if not minimum_count <= len(indexed) <= maximum_count:
        raise DenseFrameIndexError(
            "ffmpeg returned an implausible frame count for the requested cadence"
        )

    records = []
    total_bytes = 0
    for frame_index in expected_indexes:
        path = indexed[frame_index]
        try:
            metadata = path.stat()
        except OSError as error:
            raise DenseFrameIndexError(
                "A frame artifact is unavailable"
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 4 <= metadata.st_size <= limits.max_frame_bytes
        ):
            raise DenseFrameIndexOutputLimit(
                "A frame artifact violates its file-size contract"
            )
        total_bytes += int(metadata.st_size)
        if total_bytes > limits.max_total_output_bytes:
            raise DenseFrameIndexOutputLimit(
                "The dense-frame output exceeded its storage bound"
            )
        width, height = _jpeg_dimensions(path, int(metadata.st_size))
        if (
            width > limits.max_frame_width
            or width * height > limits.max_frame_pixels
        ):
            raise DenseFrameIndexOutputLimit(
                "A frame artifact violates its dimension contract"
            )
        timestamp = frame_index / limits.sampling_fps
        if timestamp > indexed_duration_seconds + (1 / limits.sampling_fps):
            raise DenseFrameIndexError(
                "A frame timestamp exceeds the indexed media range"
            )
        frame_identifier = _frame_id(
            source_sha256,
            limits.sampling_fps,
            frame_index,
        )
        if FRAME_ID_PATTERN.fullmatch(frame_identifier) is None:
            raise AssertionError("The deterministic frame ID contract failed")
        records.append(
            DenseFrameRecord(
                frame_id=frame_identifier,
                frame_index=frame_index,
                timestamp_seconds=round(timestamp, 6),
                timestamp_numerator=frame_index,
                timestamp_denominator=limits.sampling_fps,
                relative_path=f"frames/{path.name}",
                width=width,
                height=height,
                size_bytes=int(metadata.st_size),
                sha256=_sha256_regular_file(path, int(metadata.st_size)),
            )
        )
    return tuple(records), total_bytes


def _atomic_write_index(
    run_directory: Path,
    payload: Mapping[str, object],
    *,
    maximum_index_bytes: int,
    maximum_total_bytes: int,
    frame_bytes: int,
) -> int:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if (
        len(encoded) > maximum_index_bytes
        or frame_bytes + len(encoded) > maximum_total_bytes
    ):
        raise DenseFrameIndexOutputLimit(
            "The dense-frame index exceeds its storage bound"
        )
    target = run_directory / "frame-index.json"
    temporary = run_directory / f".frame-index-{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return len(encoded)


def _dense_publish_revision(
    *,
    source_sha256: str,
    owner_id: str,
    limits: DenseFrameIndexLimits,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "source_sha256": source_sha256,
                "owner_id": owner_id,
                "limits": asdict(limits),
            }
        )
    ).hexdigest()


def _load_completed_dense_run(
    directory: Path,
    *,
    source_identity: _SourceIdentity,
    source_sha256: str,
    owner_id: str,
    limits: DenseFrameIndexLimits,
) -> DenseFrameIndexResult:
    if (
        directory.is_symlink()
        or _is_reparse_point(directory)
        or not directory.is_dir()
        or _RUN_DIRECTORY_PATTERN.fullmatch(directory.name) is None
        or {path.name for path in directory.iterdir()}
        != {"frames", "frame-index.json"}
    ):
        raise DenseFrameIndexStagingUnavailable(
            "The completed dense publication is invalid"
        )
    index_path = directory / "frame-index.json"
    try:
        if (
            index_path.is_symlink()
            or _is_reparse_point(index_path)
            or not index_path.is_file()
            or index_path.stat().st_size > limits.max_index_bytes
        ):
            raise DenseFrameIndexStagingUnavailable(
                "The completed dense publication is invalid"
            )
        payload = _strict_json_object(
            index_path.read_bytes(),
            "completed dense-frame index",
        )
        raw_frames = payload.get("frames")
        raw_provenance = payload.get("provenance")
        if (
            payload.get("schema_version") != FRAME_INDEX_SCHEMA
            or not isinstance(raw_frames, list)
            or payload.get("frame_count") != len(raw_frames)
            or not isinstance(raw_provenance, dict)
        ):
            raise ValueError("completed index envelope is invalid")
        raw_policy = raw_provenance.get("review_policy")
        if not isinstance(raw_policy, dict):
            raise ValueError("completed review policy is invalid")
        provenance_payload = dict(raw_provenance)
        provenance_payload["review_policy"] = ReviewOnlyPolicy(
            **raw_policy
        )
        provenance = DenseFrameIndexProvenance(
            **provenance_payload
        )
        frames = tuple(DenseFrameRecord(**item) for item in raw_frames)
    except (
        DenseFrameIndexError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        raise DenseFrameIndexStagingUnavailable(
            "The completed dense publication is invalid"
        ) from error
    if (
        provenance.source_sha256 != source_sha256
        or provenance.publisher_owner_id != owner_id
        or provenance.source_size_bytes != source_identity.size_bytes
        or provenance.source_modified_time_ns
        != source_identity.modified_time_ns
        or dict(provenance.limits) != asdict(limits)
        or provenance.frame_count != len(frames)
    ):
        raise DenseFrameIndexStagingUnavailable(
            "The completed dense publication ownership changed"
        )
    verified_frames, _ = _validate_frames(
        directory / "frames",
        source_sha256=source_sha256,
        indexed_duration_seconds=(
            provenance.indexed_duration_seconds
        ),
        limits=limits,
    )
    if verified_frames != frames:
        raise DenseFrameIndexStagingUnavailable(
            "The completed dense publication content changed"
        )
    _directory_size(directory, limits.max_total_output_bytes)
    return DenseFrameIndexResult(
        output_directory=str(directory),
        index_relative_path="frame-index.json",
        frames=frames,
        provenance=provenance,
    )


def _prepare_dense_publish_incoming(
    output_root: Path,
    *,
    owner_id: str,
    source_sha256: str,
    final_name: str,
) -> Path:
    incoming = output_root / f".dense-incoming-{owner_id}"
    expected = {
        "schema_version": _DENSE_PUBLISH_INCOMING_SCHEMA,
        "owner_id": owner_id,
        "source_sha256": source_sha256,
        "final_name": final_name,
        "path_exposed": False,
    }
    if incoming.exists() or incoming.is_symlink():
        if (
            incoming.parent != output_root
            or incoming.is_symlink()
            or _is_reparse_point(incoming)
            or not incoming.is_dir()
        ):
            raise DenseFrameIndexStagingUnavailable(
                "The dense publication incoming directory is unsafe"
            )
        existing = _read_integrity_pair(
            incoming / _DENSE_PUBLISH_INCOMING_MARKER
        )
        if existing != expected:
            raise DenseFrameIndexStagingUnavailable(
                "The dense publication incoming ownership changed"
            )
        try:
            shutil.rmtree(incoming)
        except OSError as error:
            raise DenseFrameIndexStagingUnavailable(
                "The stale dense publication incoming directory is busy"
            ) from error
    try:
        incoming.mkdir()
        _write_integrity_pair(
            incoming / _DENSE_PUBLISH_INCOMING_MARKER,
            expected,
        )
    except OSError as error:
        raise DenseFrameIndexStagingUnavailable(
            "The dense publication incoming directory is unavailable"
        ) from error
    return incoming


def _cleanup_dense_publish_incoming(
    incoming: Optional[Path],
    output_root: Path,
    *,
    owner_id: Optional[str],
    source_sha256: str,
    final_name: Optional[str],
) -> None:
    if incoming is None or owner_id is None or final_name is None:
        return
    try:
        if (
            incoming.parent != output_root
            or incoming.name != f".dense-incoming-{owner_id}"
            or incoming.is_symlink()
            or _is_reparse_point(incoming)
            or not incoming.is_dir()
        ):
            return
        payload = _read_integrity_pair(
            incoming / _DENSE_PUBLISH_INCOMING_MARKER
        )
        if payload != {
            "schema_version": _DENSE_PUBLISH_INCOMING_SCHEMA,
            "owner_id": owner_id,
            "source_sha256": source_sha256,
            "final_name": final_name,
            "path_exposed": False,
        }:
            return
        shutil.rmtree(incoming)
    except (OSError, DenseFrameIndexStagingUnavailable):
        return


def _cleanup_run_directory(
    run_directory: Optional[Path], output_directory: Path
) -> None:
    if run_directory is None:
        return
    if (
        run_directory.parent != output_directory
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


class DenseFrameIndexEngine:
    """Build a verified, review-only dense-frame index."""

    def __init__(
        self,
        *,
        ffmpeg_path: Optional[Union[str, Path]] = None,
        ffprobe_path: Optional[Union[str, Path]] = None,
        limits: Optional[DenseFrameIndexLimits] = None,
        staging_root: Optional[Union[str, Path]] = None,
        staging_owner_id: Optional[str] = None,
        command_runner: Optional[CommandRunner] = None,
    ) -> None:
        self._ffmpeg = _resolve_executable(ffmpeg_path, "ffmpeg")
        self._ffprobe = _resolve_executable(ffprobe_path, "ffprobe")
        self._limits = limits or DenseFrameIndexLimits()
        self._staging_root = (
            Path(staging_root) if staging_root is not None else None
        )
        self._staging_owner_id = staging_owner_id
        self._runner = command_runner or _run_bounded_command

    def _run(
        self,
        command: MediaCommand,
        cancel: Optional[CancelCallback],
    ) -> MediaCommandOutput:
        result = self._runner(command, cancel)
        if (
            not isinstance(result, MediaCommandOutput)
            or isinstance(result.returncode, bool)
            or not isinstance(result.returncode, int)
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
        ):
            raise DenseFrameIndexError(
                "The media command runner returned invalid data"
            )
        if (
            len(result.stdout) + len(result.stderr)
            > command.capture_limit_bytes
        ):
            raise DenseFrameIndexOutputLimit(
                "The media command runner exceeded its output contract"
            )
        return result

    def build(
        self,
        media_path: Union[str, Path],
        *,
        source_sha256: str,
        project_root: Union[str, Path],
        output_directory: Union[str, Path],
        cancel: Optional[CancelCallback] = None,
    ) -> DenseFrameIndexResult:
        """Create one immutable frame index below an authorized project folder."""

        if (
            not isinstance(source_sha256, str)
            or SHA256_PATTERN.fullmatch(source_sha256) is None
        ):
            raise ValueError(
                "source_sha256 must be a lowercase registered SHA-256 value"
            )
        source, source_identity = _validated_source(
            media_path, self._limits.max_source_bytes
        )
        _, output_root = _validated_project_output(
            project_root, output_directory
        )
        if _cancel_requested(cancel):
            raise DenseFrameIndexCancelled("Dense-frame indexing was cancelled")
        deadline = time.monotonic() + self._limits.max_total_seconds
        _verify_source_sha256(
            source,
            source_sha256,
            source_identity,
            deadline=deadline,
            cancel=cancel,
        )

        publisher_owner_id: Optional[str] = None
        if self._staging_owner_id is not None:
            try:
                publisher_owner_id = str(
                    UUID(self._staging_owner_id)
                )
            except (TypeError, ValueError) as error:
                raise DenseFrameIndexStagingUnavailable(
                    "The dense publication owner is invalid"
                ) from error
        run_directory: Optional[Path] = None
        published_directory: Optional[Path] = None
        publish_incoming: Optional[Path] = None
        publish_final_name: Optional[str] = None
        staging_root: Optional[Path] = None
        staging_directory: Optional[Path] = None
        try:
            use_short_windows_staging = (
                dense_frame_index_requires_short_staging(output_root)
            )
            run_parent = output_root
            output_scope = (
                "new_run_directory_under_authorized_project_subfolder"
            )
            if use_short_windows_staging:
                # ffmpeg 4.4 still uses legacy Win32 path handling for image
                # sequences.  Python can safely publish a verified directory
                # to a longer project path, while ffmpeg cannot create the
                # same JPEGs there directly.  Keep the execution CWD private
                # and short, then revalidate every frame after publication.
                staging_root = _validated_short_staging_root(
                    self._staging_root,
                    required_free_bytes=(
                        self._limits.max_total_output_bytes
                        + self._limits.min_free_disk_bytes
                    ),
                )
                if self._staging_owner_id is None:
                    raise DenseFrameIndexStagingUnavailable(
                        "Short-path dense staging has no durable job owner"
                    )
                assert publisher_owner_id is not None
                publish_revision = _dense_publish_revision(
                    source_sha256=source_sha256,
                    owner_id=publisher_owner_id,
                    limits=self._limits,
                )
                publish_final_name = (
                    f"dense-frame-index-{publish_revision[:24]}"
                )
                published_directory = (
                    output_root / publish_final_name
                )
                competing_publications = [
                    child
                    for child in output_root.iterdir()
                    if child != published_directory
                    and _RUN_DIRECTORY_PATTERN.fullmatch(
                        child.name
                    )
                    is not None
                ]
                if competing_publications:
                    raise DenseFrameIndexStagingUnavailable(
                        "Multiple dense publications exist for one job"
                    )
                existing_incoming = (
                    output_root
                    / f".dense-incoming-{publisher_owner_id}"
                )
                if existing_incoming.exists():
                    publish_incoming = existing_incoming
                if published_directory.exists():
                    adopted = _load_completed_dense_run(
                        published_directory,
                        source_identity=source_identity,
                        source_sha256=source_sha256,
                        owner_id=publisher_owner_id,
                        limits=self._limits,
                    )
                    _cleanup_dense_publish_incoming(
                        publish_incoming,
                        output_root,
                        owner_id=publisher_owner_id,
                        source_sha256=source_sha256,
                        final_name=publish_final_name,
                    )
                    publish_incoming = None
                    published_directory = None
                    return adopted
                staging_directory = _create_owned_staging_child(
                    staging_root,
                    self._staging_owner_id,
                )
                run_parent = staging_directory
                output_scope = (
                    "private_short_staging_then_verified_project_publish"
                )
            run_directory = Path(
                tempfile.mkdtemp(
                    prefix="dense-frame-index-",
                    dir=str(run_parent),
                )
            )
            try:
                os.chmod(run_directory, 0o700)
            except OSError:
                pass

            ffmpeg_version = _parse_tool_version(
                self._run(
                    MediaCommand(
                        argv=(self._ffmpeg, "-version"),
                        cwd=run_directory,
                        timeout_seconds=_remaining_timeout(
                            deadline, self._limits.probe_timeout_seconds
                        ),
                        capture_limit_bytes=16 * 1024,
                    ),
                    cancel,
                ),
                "ffmpeg",
            )
            ffprobe_version = _parse_tool_version(
                self._run(
                    MediaCommand(
                        argv=(self._ffprobe, "-version"),
                        cwd=run_directory,
                        timeout_seconds=_remaining_timeout(
                            deadline, self._limits.probe_timeout_seconds
                        ),
                        capture_limit_bytes=16 * 1024,
                    ),
                    cancel,
                ),
                "ffprobe",
            )
            probe_output = self._run(
                MediaCommand(
                    argv=(
                        self._ffprobe,
                        "-v",
                        "error",
                        "-select_streams",
                        "v:0",
                        "-show_entries",
                        (
                            "format=duration,size,format_name:"
                            "stream=index,codec_type,codec_name,width,height"
                        ),
                        "-of",
                        "json",
                        _media_tool_path(source),
                    ),
                    cwd=run_directory,
                    timeout_seconds=_remaining_timeout(
                        deadline, self._limits.probe_timeout_seconds
                    ),
                    capture_limit_bytes=self._limits.max_probe_output_bytes,
                ),
                cancel,
            )
            if probe_output.returncode != 0:
                raise DenseFrameIndexError(
                    "ffprobe rejected the protected source video"
                )
            probe = _parse_probe(
                probe_output.stdout,
                source_identity,
                self._limits,
            )
            if _source_identity(source) != source_identity:
                raise DenseFrameIndexSourceChanged(
                    "The protected source changed while it was probed"
                )

            capacity_plan = build_dense_frame_capacity_plan(
                source_sha256=source_sha256,
                duration_seconds=probe.duration_seconds,
                limits=self._limits,
            )
            indexed_duration = capacity_plan.indexed_duration_seconds
            frame_limit = capacity_plan.planned_frame_count
            bounded_frame_bytes = (
                capacity_plan.planned_frame_bytes_upper_bound
            )
            bounded_total_bytes = (
                capacity_plan.planned_output_bytes_upper_bound
            )
            required_free = (
                bounded_total_bytes
                + self._limits.min_free_disk_bytes
            )
            same_volume_publish = (
                run_parent != output_root
                and same_storage_volume(run_parent, output_root)
            )
            output_available = int(
                shutil.disk_usage(output_root).free
            )
            if same_volume_publish:
                combined_peak = (
                    2 * bounded_total_bytes
                    + self._limits.min_free_disk_bytes
                )
                if output_available < combined_peak:
                    raise DenseFrameIndexOutputLimit(
                        "The shared staging and output volume cannot fit "
                        "both the bounded run and atomic publication copy"
                    )
            else:
                if output_available < required_free:
                    raise DenseFrameIndexOutputLimit(
                        "The project output disk cannot fit this media's "
                        "bounded index and reserve"
                    )
                if (
                    run_parent != output_root
                    and int(shutil.disk_usage(run_parent).free)
                    < required_free
                ):
                    raise DenseFrameIndexOutputLimit(
                        "The private staging disk cannot fit this media's "
                        "bounded index and reserve"
                    )
            frames_root = run_directory / "frames"
            frames_root.mkdir(exist_ok=False)
            if frames_root.is_symlink() or _is_reparse_point(frames_root):
                raise DenseFrameIndexError(
                    "The frame output directory is unsafe"
                )
            sampling_filter = (
                f"fps=fps={self._limits.sampling_fps}:"
                "start_time=0:round=near,"
                f"scale=w=min(iw\\,{self._limits.max_frame_width}):"
                "h=-2:flags=lanczos"
            )
            # Keep the ffmpeg output argument relative to the already
            # validated run-directory CWD.  Windows builds of ffmpeg 4.4 can
            # fail to open an otherwise valid JPEG sequence when the absolute
            # UTF-16 path crosses the legacy MAX_PATH boundary.  The relative
            # argument is still confined because shell execution is disabled,
            # the CWD is the fresh run directory, and the absolute
            # ``watch_directory`` below remains the bounded output guard.
            frame_pattern = Path("frames") / "frame-%08d.jpg"
            extraction_argv = (
                self._ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                _media_tool_path(source),
                "-map",
                f"0:{probe.stream_index}",
                "-an",
                "-sn",
                "-dn",
                "-t",
                f"{indexed_duration:.6f}",
                "-vf",
                sampling_filter,
                "-frames:v",
                str(frame_limit),
                "-q:v",
                "4",
                "-start_number",
                "0",
                # ffmpeg 4.4 (the supported Windows bridge baseline) predates
                # the output-scoped ``-fps_mode`` option.  ``-vsync 0`` is the
                # equivalent non-duplicating image-sequence contract on both
                # 4.4 and current ffmpeg releases.
                "-vsync",
                "0",
                "-n",
                _media_tool_path(frame_pattern),
            )
            extraction_output = self._run(
                MediaCommand(
                    argv=extraction_argv,
                    cwd=run_directory,
                    timeout_seconds=_remaining_timeout(
                        deadline,
                        self._limits.extraction_timeout_seconds,
                    ),
                    capture_limit_bytes=self._limits.max_process_output_bytes,
                    watch_directory=frames_root,
                    watch_limit_bytes=bounded_frame_bytes,
                ),
                cancel,
            )
            if extraction_output.returncode != 0:
                message = extraction_output.stderr[:2_048].decode(
                    "utf-8", errors="replace"
                ).strip()
                raise DenseFrameIndexError(
                    "ffmpeg could not build the dense-frame index"
                    + (f": {message}" if message else "")
                )
            if _source_identity(source) != source_identity:
                raise DenseFrameIndexSourceChanged(
                    "The protected source changed while frames were indexed"
                )

            frames, frame_bytes = _validate_frames(
                frames_root,
                source_sha256=source_sha256,
                indexed_duration_seconds=indexed_duration,
                limits=self._limits,
            )
            nominal_frames = math.ceil(
                indexed_duration * self._limits.sampling_fps
            )
            truncated = (
                probe.duration_seconds > indexed_duration + 0.000001
                or nominal_frames > self._limits.max_frames
            )
            provenance = DenseFrameIndexProvenance(
                schema_version=FRAME_INDEX_SCHEMA,
                source_name=source.name,
                source_size_bytes=source_identity.size_bytes,
                source_modified_time_ns=source_identity.modified_time_ns,
                source_sha256=source_sha256,
                source_access="read_only_sha256_and_identity_verified",
                duration_seconds=probe.duration_seconds,
                indexed_duration_seconds=indexed_duration,
                sampling_fps=self._limits.sampling_fps,
                sampling_filter=sampling_filter,
                timestamp_method=(
                    "zero_based_frame_index_divided_by_integer_sampling_fps"
                ),
                frame_id_method=(
                    "sha256(schema,source_sha256,fps,zero_based_frame_index)"
                    "_first_24_hex"
                ),
                frame_count=len(frames),
                truncated=truncated,
                ffmpeg_version=ffmpeg_version,
                ffprobe_version=ffprobe_version,
                output_scope=(
                    output_scope
                ),
                limits=asdict(self._limits),
                review_policy=ReviewOnlyPolicy(),
                publisher_owner_id=publisher_owner_id,
            )
            index_payload = {
                "schema_version": FRAME_INDEX_SCHEMA,
                "frame_count": len(frames),
                "frames": [asdict(frame) for frame in frames],
                "provenance": asdict(provenance),
            }
            _atomic_write_index(
                run_directory,
                index_payload,
                maximum_index_bytes=self._limits.max_index_bytes,
                maximum_total_bytes=self._limits.max_total_output_bytes,
                frame_bytes=frame_bytes,
            )
            if _source_identity(source) != source_identity:
                raise DenseFrameIndexSourceChanged(
                    "The protected source changed before the index was finalized"
                )
            expected_root_entries = {"frames", "frame-index.json"}
            if {path.name for path in run_directory.iterdir()} != expected_root_entries:
                raise DenseFrameIndexError(
                    "The dense-frame run contains unexpected artifacts"
                )
            _directory_size(
                run_directory, self._limits.max_total_output_bytes
            )
            if use_short_windows_staging:
                assert publisher_owner_id is not None
                assert publish_final_name is not None
                assert published_directory is not None
                index_bytes_before_publish = (
                    run_directory / "frame-index.json"
                ).read_bytes()
                if published_directory.exists():
                    raise DenseFrameIndexError(
                        "The dense-frame publication target already exists"
                    )
                publish_incoming = (
                    _prepare_dense_publish_incoming(
                        output_root,
                        owner_id=publisher_owner_id,
                        source_sha256=source_sha256,
                        final_name=publish_final_name,
                    )
                )
                incoming_payload = publish_incoming / "payload"
                shutil.copytree(
                    run_directory,
                    incoming_payload,
                    copy_function=shutil.copy2,
                )
                published_frames, published_frame_bytes = _validate_frames(
                    incoming_payload / "frames",
                    source_sha256=source_sha256,
                    indexed_duration_seconds=indexed_duration,
                    limits=self._limits,
                )
                if (
                    published_frames != frames
                    or published_frame_bytes != frame_bytes
                ):
                    raise DenseFrameIndexError(
                        "The published dense-frame index changed in transit"
                    )
                published_index = run_directory / "frame-index.json"
                if (
                    (incoming_payload / "frame-index.json").read_bytes()
                    != index_bytes_before_publish
                ):
                    raise DenseFrameIndexError(
                        "The published dense-frame manifest changed in transit"
                    )
                _directory_size(
                    incoming_payload,
                    self._limits.max_total_output_bytes,
                )
                os.replace(
                    incoming_payload,
                    published_directory,
                )
                run_directory = published_directory
                _cleanup_dense_publish_incoming(
                    publish_incoming,
                    output_root,
                    owner_id=publisher_owner_id,
                    source_sha256=source_sha256,
                    final_name=publish_final_name,
                )
                publish_incoming = None
            result = DenseFrameIndexResult(
                output_directory=str(run_directory),
                index_relative_path="frame-index.json",
                frames=frames,
                provenance=provenance,
            )
            run_directory = None
            published_directory = None
            return result
        except DenseFrameIndexError:
            raise
        except OSError as error:
            raise DenseFrameIndexError(
                "The dense-frame publication failed"
            ) from error
        except ValueError:
            raise
        finally:
            _cleanup_run_directory(run_directory, output_root)
            _cleanup_run_directory(published_directory, output_root)
            _cleanup_dense_publish_incoming(
                publish_incoming,
                output_root,
                owner_id=publisher_owner_id,
                source_sha256=source_sha256,
                final_name=publish_final_name,
            )
            _cleanup_owned_staging_child(
                staging_directory,
                staging_root,
                self._staging_owner_id,
            )


__all__ = [
    "DENSE_FRAME_CAPACITY_PLAN_SCHEMA",
    "DenseFrameIndexCapacityPlan",
    "DenseFrameIndexCancelled",
    "DenseFrameIndexEngine",
    "DenseFrameIndexError",
    "DenseFrameIndexLimits",
    "DenseFrameIndexOutputLimit",
    "DenseFrameIndexProvenance",
    "DenseFrameIndexResult",
    "DenseFrameIndexSourceChanged",
    "DenseFrameIndexStagingUnavailable",
    "DenseFrameIndexTimeout",
    "DenseFrameRecord",
    "FRAME_ID_PATTERN",
    "FRAME_INDEX_SCHEMA",
    "MediaCommand",
    "MediaCommandOutput",
    "ReviewOnlyPolicy",
    "build_dense_frame_capacity_plan",
    "dense_frame_index_requires_short_staging",
    "dense_staging_root_is_short",
    "initialize_dense_staging_root",
    "recover_dense_staging_root",
    "same_storage_volume",
]
