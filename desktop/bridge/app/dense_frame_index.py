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
from uuid import uuid4


FRAME_INDEX_SCHEMA = "trafficvisson.dense-frame-index.v1"
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
    if math.ceil(nominal) >= max_frames:
        return max_frames, max_frames
    return (
        max(1, math.floor(nominal) - 1),
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
        command_runner: Optional[CommandRunner] = None,
    ) -> None:
        self._ffmpeg = _resolve_executable(ffmpeg_path, "ffmpeg")
        self._ffprobe = _resolve_executable(ffprobe_path, "ffprobe")
        self._limits = limits or DenseFrameIndexLimits()
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
        required_free = (
            self._limits.max_total_output_bytes
            + self._limits.min_free_disk_bytes
        )
        if shutil.disk_usage(output_root).free < required_free:
            raise DenseFrameIndexOutputLimit(
                "The project output disk cannot fit the bounded index and reserve"
            )

        deadline = time.monotonic() + self._limits.max_total_seconds
        _verify_source_sha256(
            source,
            source_sha256,
            source_identity,
            deadline=deadline,
            cancel=cancel,
        )

        run_directory: Optional[Path] = None
        try:
            run_directory = Path(
                tempfile.mkdtemp(
                    prefix="dense-frame-index-",
                    dir=str(output_root),
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
                        str(source),
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

            indexed_duration = min(
                probe.duration_seconds, self._limits.max_media_seconds
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
            frame_pattern = frames_root / "frame-%08d.jpg"
            extraction_argv = (
                self._ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(source),
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
                str(self._limits.max_frames),
                "-q:v",
                "4",
                "-start_number",
                "0",
                "-fps_mode",
                "passthrough",
                "-n",
                str(frame_pattern),
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
                    watch_limit_bytes=self._limits.max_total_output_bytes,
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
                    "new_run_directory_under_authorized_project_subfolder"
                ),
                limits=asdict(self._limits),
                review_policy=ReviewOnlyPolicy(),
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
            result = DenseFrameIndexResult(
                output_directory=str(run_directory),
                index_relative_path="frame-index.json",
                frames=frames,
                provenance=provenance,
            )
            run_directory = None
            return result
        except (
            DenseFrameIndexError,
            OSError,
            ValueError,
        ):
            raise
        finally:
            _cleanup_run_directory(run_directory, output_root)


__all__ = [
    "DenseFrameIndexCancelled",
    "DenseFrameIndexEngine",
    "DenseFrameIndexError",
    "DenseFrameIndexLimits",
    "DenseFrameIndexOutputLimit",
    "DenseFrameIndexProvenance",
    "DenseFrameIndexResult",
    "DenseFrameIndexSourceChanged",
    "DenseFrameIndexTimeout",
    "DenseFrameRecord",
    "FRAME_ID_PATTERN",
    "FRAME_INDEX_SCHEMA",
    "MediaCommand",
    "MediaCommandOutput",
    "ReviewOnlyPolicy",
]
