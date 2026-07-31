"""Bounded, deterministic evidence-candidate extraction for trusted local media.

This module does not classify road defects.  It only finds representative
visual changes at a bounded cadence and materializes review evidence.  The
bridge must resolve project authorization before passing ``media_path`` and
``output_directory`` here.

Security properties:

* only absolute ``ffmpeg``/``ffprobe`` executables are launched;
* every process uses an argument vector with ``shell=False``;
* command time, captured output, candidate count, artifact size, and total
  output size are bounded;
* the source is never loaded into memory and is checked for mutation;
* artifacts are written to a new private run directory below the caller-owned
  output directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat as stat_module
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union


_ALLOWED_VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mts", ".webm"}
)
_ALLOWED_EXECUTABLE_NAMES = frozenset(
    {"ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FRAME_LINE_PATTERN = re.compile(
    r"^frame:\s*(?P<frame>[0-9]+).*?\bpts_time:(?P<time>-?[0-9]+(?:\.[0-9]+)?)"
)
_METADATA_LINE_PATTERN = re.compile(
    r"^(?P<key>[A-Za-z0-9_.-]{1,128})=(?P<value>[^\r\n]{0,256})$"
)
_MAX_TOOL_VERSION_LENGTH = 256
_MAX_ARGUMENT_LENGTH = 32_768
_MAX_ARGUMENT_VECTOR_LENGTH = 128 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class CandidateExtractionError(RuntimeError):
    """Base exception for a safe extraction failure."""


class CandidateExtractionCancelled(CandidateExtractionError):
    """Raised after a caller requests cancellation."""


class CandidateExtractionTimeout(CandidateExtractionError):
    """Raised after a bounded tool or overall extraction deadline expires."""


class CandidateExtractionOutputLimit(CandidateExtractionError):
    """Raised when command or artifact output exceeds a configured limit."""


@dataclass(frozen=True)
class ExtractionLimits:
    """Resource and sampling limits for one extraction run."""

    cadence_seconds: float = 15.0
    scene_threshold: float = 0.32
    group_gap_seconds: float = 3.0
    preview_duration_seconds: float = 3.0
    max_sample_frames: int = 32
    max_candidate_groups: int = 32
    max_frame_width: int = 1_600
    max_frame_bytes: int = 12 * 1024 * 1024
    max_clip_bytes: int = 96 * 1024 * 1024
    max_output_bytes: int = 2 * 1024 * 1024 * 1024
    min_free_disk_bytes: int = 256 * 1024 * 1024
    max_probe_output_bytes: int = 512 * 1024
    max_scan_output_bytes: int = 2 * 1024 * 1024
    probe_timeout_seconds: float = 20.0
    scan_timeout_seconds: float = 30 * 60.0
    artifact_timeout_seconds: float = 2 * 60.0
    max_total_seconds: float = 60 * 60.0
    max_media_duration_seconds: float = 31 * 24 * 60 * 60.0

    def __post_init__(self) -> None:
        numeric_ranges = (
            ("cadence_seconds", self.cadence_seconds, 0.25, 86_400.0),
            ("scene_threshold", self.scene_threshold, 0.0, 1.0),
            ("group_gap_seconds", self.group_gap_seconds, 0.0, 300.0),
            ("preview_duration_seconds", self.preview_duration_seconds, 2.0, 5.0),
            ("probe_timeout_seconds", self.probe_timeout_seconds, 0.1, 300.0),
            ("scan_timeout_seconds", self.scan_timeout_seconds, 1.0, 24 * 60 * 60.0),
            (
                "artifact_timeout_seconds",
                self.artifact_timeout_seconds,
                1.0,
                60 * 60.0,
            ),
            ("max_total_seconds", self.max_total_seconds, 1.0, 48 * 60 * 60.0),
            (
                "max_media_duration_seconds",
                self.max_media_duration_seconds,
                0.1,
                31 * 24 * 60 * 60.0,
            ),
        )
        for name, value, minimum, maximum in numeric_ranges:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")

        integer_ranges = (
            ("max_sample_frames", self.max_sample_frames, 1, 512),
            ("max_candidate_groups", self.max_candidate_groups, 1, 512),
            ("max_frame_width", self.max_frame_width, 160, 8_192),
            ("max_frame_bytes", self.max_frame_bytes, 1_024, 256 * 1024 * 1024),
            ("max_clip_bytes", self.max_clip_bytes, 1_024, 2 * 1024 * 1024 * 1024),
            ("max_output_bytes", self.max_output_bytes, 2_048, 8 * 1024**3),
            ("min_free_disk_bytes", self.min_free_disk_bytes, 0, 8 * 1024**4),
            (
                "max_probe_output_bytes",
                self.max_probe_output_bytes,
                1_024,
                16 * 1024 * 1024,
            ),
            (
                "max_scan_output_bytes",
                self.max_scan_output_bytes,
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
        if self.max_candidate_groups > self.max_sample_frames:
            raise ValueError(
                "max_candidate_groups cannot exceed max_sample_frames"
            )
        minimum_possible_artifacts = self.max_frame_bytes + self.max_clip_bytes
        if self.max_output_bytes < minimum_possible_artifacts:
            raise ValueError(
                "max_output_bytes must fit at least one frame and preview clip"
            )


@dataclass(frozen=True)
class FrameObservation:
    timestamp_seconds: float
    scene_score: float
    sharpness_score: float
    triggers: Tuple[str, ...]


@dataclass(frozen=True)
class ArtifactRecord:
    relative_path: str
    size_bytes: int
    sha256: str
    media_type: str


@dataclass(frozen=True)
class CandidateGroup:
    candidate_id: str
    start_seconds: float
    end_seconds: float
    representative_timestamp_seconds: float
    scene_score: float
    sharpness_score: float
    triggers: Tuple[str, ...]
    observations: Tuple[FrameObservation, ...]
    representative_image: ArtifactRecord
    preview_clip: ArtifactRecord
    preview_start_seconds: float
    preview_duration_seconds: float


@dataclass(frozen=True)
class SourceLocation:
    """One ISO 6709 coordinate embedded in the source container metadata.

    Phone videos commonly expose this as a format-level ``location`` tag.  It
    is evidence for the recording start only; it is never treated as a route
    or interpolated across candidate timestamps.
    """

    latitude: float
    longitude: float
    altitude_meters: Optional[float]
    raw_iso6709: str
    scope: str = "capture_start_only"


@dataclass(frozen=True)
class ExtractionProvenance:
    source_name: str
    source_size_bytes: int
    source_modified_time_ns: int
    source_fingerprint: str
    source_sha256: Optional[str]
    duration_seconds: float
    format_names: Tuple[str, ...]
    video_stream_index: int
    video_codec: str
    width: int
    height: int
    frame_rate: str
    ffmpeg_version: str
    ffprobe_version: str
    scene_score_method: str
    sharpness_score_method: str
    effective_cadence_seconds: float
    minimum_selection_gap_seconds: float
    limits: Mapping[str, Union[int, float]]
    source_location: Optional[SourceLocation] = None


@dataclass(frozen=True)
class ExtractionResult:
    output_directory: str
    candidates: Tuple[CandidateGroup, ...]
    provenance: ExtractionProvenance

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-serializable result for a bridge contract."""
        return asdict(self)


@dataclass(frozen=True)
class _CommandOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class _SourceIdentity:
    size_bytes: int
    modified_time_ns: int


@dataclass(frozen=True)
class _ProbeMetadata:
    duration_seconds: float
    format_names: Tuple[str, ...]
    stream_index: int
    codec_name: str
    width: int
    height: int
    frame_rate: str
    source_location: Optional[SourceLocation]


CommandRunner = Callable[
    [Sequence[str], float, int, Optional[Callable[[], bool]]], _CommandOutput
]


def _cancel_requested(cancel: Optional[Callable[[], bool]]) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception as error:
        raise CandidateExtractionError("The cancellation callback failed") from error


def _validate_argv(argv: Sequence[str]) -> List[str]:
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("A non-empty argument vector is required")
    normalized = []
    total_length = 0
    for argument in argv:
        if (
            not isinstance(argument, str)
            or "\x00" in argument
            or len(argument) > _MAX_ARGUMENT_LENGTH
        ):
            raise ValueError("The tool argument vector is invalid")
        normalized.append(argument)
        total_length += len(argument)
    executable = Path(normalized[0])
    if (
        not executable.is_absolute()
        or executable.name.casefold() not in _ALLOWED_EXECUTABLE_NAMES
        or not executable.is_file()
    ):
        raise ValueError("The media tool executable is invalid")
    if total_length > _MAX_ARGUMENT_VECTOR_LENGTH:
        raise ValueError("The tool argument vector is too large")
    return normalized


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_bounded_command(
    argv: Sequence[str],
    timeout_seconds: float,
    max_output_bytes: int,
    cancel: Optional[Callable[[], bool]],
) -> _CommandOutput:
    """Execute one allowlisted media tool with bounded capture and cancellation."""
    normalized = _validate_argv(argv)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("The process timeout is invalid")
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
    ):
        raise ValueError("The process output limit is invalid")
    if _cancel_requested(cancel):
        raise CandidateExtractionCancelled("Candidate extraction was cancelled")

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            normalized,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
    except OSError as error:
        raise CandidateExtractionError("The media tool could not be started") from error

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    captured_total = [0]
    capture_lock = threading.Lock()
    overflow = threading.Event()

    def read_stream(stream, destination: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    return
                with capture_lock:
                    remaining = max_output_bytes - captured_total[0]
                    if remaining <= 0:
                        overflow.set()
                        return
                    kept = chunk[:remaining]
                    destination.extend(kept)
                    captured_total[0] += len(kept)
                    if len(kept) != len(chunk):
                        overflow.set()
                        return
        except (OSError, ValueError):
            overflow.set()

    stdout_thread = threading.Thread(
        target=read_stream,
        args=(process.stdout, stdout_buffer),
        name="trafficvisson-ffmpeg-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=read_stream,
        args=(process.stderr, stderr_buffer),
        name="trafficvisson-ffmpeg-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    deadline = time.monotonic() + float(timeout_seconds)
    failure: Optional[CandidateExtractionError] = None
    try:
        while process.poll() is None:
            if overflow.is_set():
                failure = CandidateExtractionOutputLimit(
                    "The media tool exceeded its captured-output limit"
                )
                break
            if _cancel_requested(cancel):
                failure = CandidateExtractionCancelled(
                    "Candidate extraction was cancelled"
                )
                break
            if time.monotonic() >= deadline:
                failure = CandidateExtractionTimeout(
                    "The media tool exceeded its time limit"
                )
                break
            time.sleep(0.02)
    except CandidateExtractionError as error:
        failure = error
    finally:
        if failure is not None:
            _terminate_process(process)

    stdout_thread.join(timeout=2.0)
    stderr_thread.join(timeout=2.0)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        _terminate_process(process)
        raise CandidateExtractionError("The media tool did not close its output pipes")
    if overflow.is_set() and failure is None:
        failure = CandidateExtractionOutputLimit(
            "The media tool exceeded its captured-output limit"
        )
    if failure is not None:
        raise failure
    return _CommandOutput(
        returncode=int(process.returncode),
        stdout=bytes(stdout_buffer),
        stderr=bytes(stderr_buffer),
    )


def _resolve_executable(
    configured_path: Optional[Union[str, Path]], expected_name: str
) -> str:
    expected_names = {expected_name, f"{expected_name}.exe"}
    candidate = (
        str(configured_path)
        if configured_path is not None
        else shutil.which(expected_name)
    )
    if not candidate:
        raise CandidateExtractionError(f"{expected_name} is not installed")
    resolved = Path(candidate)
    if not resolved.is_absolute():
        found = shutil.which(candidate)
        if not found:
            raise CandidateExtractionError(f"{expected_name} is not installed")
        resolved = Path(found)
    if (
        resolved.name.casefold() not in expected_names
        or not resolved.is_file()
    ):
        raise CandidateExtractionError(
            f"The configured {expected_name} executable is invalid"
        )
    return str(resolved)


def _media_tool_path(path: Path) -> str:
    """Return a Windows long-path-safe argument for FFmpeg/FFprobe 4.x.

    Python can operate on long-path-enabled absolute paths that the older
    Windows builds of FFmpeg still hand to legacy Win32 APIs without the
    extended-length prefix.  Keep ordinary paths byte-for-byte unchanged for
    command provenance and add the prefix only when the path can exceed the
    Win32 directory limit.
    """

    rendered = str(path)
    if os.name != "nt" or len(rendered) < 248 or rendered.startswith("\\\\?\\"):
        return rendered
    if rendered.startswith("\\\\"):
        return "\\\\?\\UNC\\" + rendered[2:]
    return "\\\\?\\" + rendered


def _validated_source(path_value: Union[str, Path]) -> Tuple[Path, _SourceIdentity]:
    path = Path(path_value)
    if (
        not path.is_absolute()
        or path.suffix.casefold() not in _ALLOWED_VIDEO_EXTENSIONS
        or path.is_symlink()
    ):
        raise ValueError("A validated absolute media file path is required")
    try:
        stat_result = path.stat()
    except OSError as error:
        raise ValueError("The validated media file is unavailable") from error
    if not stat_module.S_ISREG(stat_result.st_mode) or stat_result.st_size <= 0:
        raise ValueError("The validated media path is not a regular non-empty file")
    return path, _SourceIdentity(
        size_bytes=int(stat_result.st_size),
        modified_time_ns=int(stat_result.st_mtime_ns),
    )


def _validated_output_root(path_value: Union[str, Path]) -> Path:
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("An absolute non-symlink project output directory is required")
    try:
        stat_result = path.stat()
    except OSError as error:
        raise ValueError("The project output directory is unavailable") from error
    if not stat_module.S_ISDIR(stat_result.st_mode):
        raise ValueError("The project output path is not a directory")
    return path


def _decode_json(output: _CommandOutput) -> object:
    if output.returncode != 0:
        raise CandidateExtractionError("ffprobe rejected the validated media")
    try:
        return json.loads(output.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateExtractionError("ffprobe returned invalid metadata") from error


def _safe_positive_int(value: object, field_name: str, maximum: int) -> int:
    if isinstance(value, bool):
        raise CandidateExtractionError(f"ffprobe {field_name} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise CandidateExtractionError(
            f"ffprobe {field_name} is invalid"
        ) from error
    if parsed <= 0 or parsed > maximum or str(value).strip() != str(parsed):
        raise CandidateExtractionError(f"ffprobe {field_name} is invalid")
    return parsed


def _safe_float(value: object, field_name: str, maximum: float) -> float:
    if isinstance(value, bool):
        raise CandidateExtractionError(f"ffprobe {field_name} is invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise CandidateExtractionError(
            f"ffprobe {field_name} is invalid"
        ) from error
    if not math.isfinite(parsed) or parsed <= 0 or parsed > maximum:
        raise CandidateExtractionError(f"ffprobe {field_name} is invalid")
    return parsed


_ISO_6709_LOCATION_PATTERN = re.compile(
    r"^([+-])(\d{2}(?:\.\d{1,12})?)"
    r"([+-])(\d{3}(?:\.\d{1,12})?)"
    r"(?:([+-])(\d{1,6}(?:\.\d{1,6})?))?/$"
)


def _parse_source_location(raw_format: Mapping[str, object]) -> Optional[SourceLocation]:
    """Parse a bounded format-level ISO 6709 coordinate, if one is usable."""

    raw_tags = raw_format.get("tags")
    if not isinstance(raw_tags, dict) or len(raw_tags) > 256:
        return None
    raw_value = None
    for key in (
        "location",
        "location-eng",
        "com.apple.quicktime.location.ISO6709",
    ):
        candidate = raw_tags.get(key)
        if isinstance(candidate, str) and candidate:
            raw_value = candidate.strip()
            break
    if raw_value is None or len(raw_value) > 64:
        return None
    match = _ISO_6709_LOCATION_PATTERN.fullmatch(raw_value)
    if match is None:
        return None
    latitude = float(match.group(2))
    longitude = float(match.group(4))
    if match.group(1) == "-":
        latitude *= -1
    if match.group(3) == "-":
        longitude *= -1
    altitude = None
    if match.group(5) is not None and match.group(6) is not None:
        altitude = float(match.group(6))
        if match.group(5) == "-":
            altitude *= -1
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or abs(latitude) > 90
        or abs(longitude) > 180
        or (latitude == 0 and longitude == 0)
        or (altitude is not None and (not math.isfinite(altitude) or abs(altitude) > 20_000))
    ):
        return None
    return SourceLocation(
        latitude=round(latitude, 12),
        longitude=round(longitude, 12),
        altitude_meters=None if altitude is None else round(altitude, 6),
        raw_iso6709=raw_value,
    )


def _parse_probe(
    raw: object, source: _SourceIdentity, limits: ExtractionLimits
) -> _ProbeMetadata:
    if not isinstance(raw, dict):
        raise CandidateExtractionError("ffprobe returned invalid metadata")
    raw_format = raw.get("format")
    raw_streams = raw.get("streams")
    if not isinstance(raw_format, dict) or not isinstance(raw_streams, list):
        raise CandidateExtractionError("ffprobe returned incomplete metadata")
    if not 1 <= len(raw_streams) <= 128:
        raise CandidateExtractionError("ffprobe returned an invalid stream count")
    reported_size = _safe_positive_int(
        raw_format.get("size"), "media size", 2**63 - 1
    )
    if reported_size != source.size_bytes:
        raise CandidateExtractionError("The media changed before extraction")
    duration = _safe_float(
        raw_format.get("duration"),
        "duration",
        limits.max_media_duration_seconds,
    )
    format_name = raw_format.get("format_name")
    if not isinstance(format_name, str) or len(format_name) > 256:
        raise CandidateExtractionError("ffprobe format is invalid")
    format_names = tuple(
        item for item in dict.fromkeys(format_name.split(",")) if item
    )
    if not format_names or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item)
        for item in format_names
    ):
        raise CandidateExtractionError("ffprobe format is invalid")

    video_streams = [
        stream
        for stream in raw_streams
        if isinstance(stream, dict) and stream.get("codec_type", "video") == "video"
    ]
    if not video_streams:
        raise CandidateExtractionError("ffprobe found no video stream")
    stream = video_streams[0]
    index = _safe_positive_int(
        int(stream.get("index", 0)) + 1, "stream index", 128
    ) - 1
    codec_name = stream.get("codec_name")
    if not isinstance(codec_name, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,128}", codec_name
    ):
        raise CandidateExtractionError("ffprobe video codec is invalid")
    width = _safe_positive_int(stream.get("width"), "width", 65_536)
    height = _safe_positive_int(stream.get("height"), "height", 65_536)
    frame_rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    if not isinstance(frame_rate, str) or not re.fullmatch(
        r"[0-9]{1,12}/[0-9]{1,12}", frame_rate
    ):
        raise CandidateExtractionError("ffprobe frame rate is invalid")
    numerator, denominator = (int(item) for item in frame_rate.split("/"))
    if numerator <= 0 or denominator <= 0:
        raise CandidateExtractionError("ffprobe frame rate is invalid")
    return _ProbeMetadata(
        duration_seconds=duration,
        format_names=format_names,
        stream_index=index,
        codec_name=codec_name,
        width=width,
        height=height,
        frame_rate=frame_rate,
        source_location=_parse_source_location(raw_format),
    )


def _parse_tool_version(output: _CommandOutput, tool_name: str) -> str:
    if output.returncode != 0:
        raise CandidateExtractionError(f"{tool_name} version check failed")
    first_line = output.stdout.decode("utf-8", errors="replace").splitlines()
    if not first_line:
        raise CandidateExtractionError(f"{tool_name} version output is invalid")
    value = first_line[0].strip()
    if not value or len(value) > _MAX_TOOL_VERSION_LENGTH:
        raise CandidateExtractionError(f"{tool_name} version output is invalid")
    return value


def _finite_score(value: str) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _effective_bootstrap_cadence(
    *,
    duration_seconds: float,
    configured_cadence_seconds: float,
    minimum_gap_seconds: float,
) -> float:
    """Keep at least three internal intervals for a short video."""

    values = (
        duration_seconds,
        configured_cadence_seconds,
        minimum_gap_seconds,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        for value in values
    ):
        raise CandidateExtractionError(
            "bootstrap cadence inputs are invalid"
        )
    short_clip_cadence = max(
        0.25, float(duration_seconds) / 3.0
    )
    return max(
        float(minimum_gap_seconds),
        min(
            float(configured_cadence_seconds),
            short_clip_cadence,
        ),
    )


def _parse_observations(
    raw_output: bytes,
    duration_seconds: float,
    scene_threshold: float,
    cadence_seconds: float,
    maximum: int,
) -> List[FrameObservation]:
    text = raw_output.decode("utf-8", errors="replace")
    parsed_frames: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        frame_match = _FRAME_LINE_PATTERN.match(line)
        if frame_match:
            if current is not None:
                parsed_frames.append(current)
            current = {
                "timestamp": _finite_score(frame_match.group("time")),
                "metadata": {},
            }
            continue
        metadata_match = _METADATA_LINE_PATTERN.match(line)
        if metadata_match and current is not None:
            metadata = current["metadata"]
            assert isinstance(metadata, dict)
            metadata[metadata_match.group("key")] = metadata_match.group("value")
    if current is not None:
        parsed_frames.append(current)

    observations: List[FrameObservation] = []
    previous_timestamp: Optional[float] = None
    for parsed_frame in parsed_frames:
        timestamp = parsed_frame.get("timestamp")
        metadata = parsed_frame.get("metadata")
        if (
            not isinstance(timestamp, float)
            or not math.isfinite(timestamp)
            or timestamp < 0
            or timestamp > duration_seconds + 0.25
            or not isinstance(metadata, dict)
        ):
            continue
        timestamp = min(timestamp, duration_seconds)
        if (
            previous_timestamp is not None
            and timestamp <= previous_timestamp + 0.0005
        ):
            continue
        scene_raw = _finite_score(
            str(metadata.get("lavfi.scene_score", "0"))
        )
        edge_raw = _finite_score(
            str(metadata.get("lavfi.signalstats.YAVG", "0"))
        )
        scene_score = max(0.0, min(1.0, scene_raw or 0.0))
        sharpness_score = max(0.0, min(1.0, (edge_raw or 0.0) / 255.0))
        triggers = []
        if previous_timestamp is None:
            triggers.append("initial")
        if scene_score >= scene_threshold:
            triggers.append("scene_change")
        if (
            previous_timestamp is not None
            and timestamp - previous_timestamp >= cadence_seconds * 0.95
        ):
            triggers.append("cadence")
        if not triggers:
            triggers.append("bounded_sample")
        observations.append(
            FrameObservation(
                timestamp_seconds=round(timestamp, 6),
                scene_score=round(scene_score, 6),
                sharpness_score=round(sharpness_score, 6),
                triggers=tuple(triggers),
            )
        )
        previous_timestamp = timestamp
        if len(observations) >= maximum:
            break
    if not observations:
        observations.append(
            FrameObservation(
                timestamp_seconds=0.0,
                scene_score=0.0,
                sharpness_score=0.0,
                triggers=("fallback_initial",),
            )
        )
    return observations


def _group_observations(
    observations: Sequence[FrameObservation],
    group_gap_seconds: float,
    maximum_groups: int,
) -> List[Tuple[FrameObservation, ...]]:
    groups: List[List[FrameObservation]] = []
    for observation in observations:
        if (
            not groups
            or observation.timestamp_seconds
            - groups[-1][-1].timestamp_seconds
            > group_gap_seconds
        ):
            groups.append([observation])
        else:
            groups[-1].append(observation)
    return [tuple(group) for group in groups[:maximum_groups]]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _validate_artifact(
    path: Path,
    run_directory: Path,
    maximum_bytes: int,
    media_type: str,
) -> ArtifactRecord:
    try:
        stat_result = path.stat()
    except OSError as error:
        raise CandidateExtractionError("ffmpeg did not create an evidence artifact") from error
    if (
        path.is_symlink()
        or path.parent != run_directory
        or not stat_module.S_ISREG(stat_result.st_mode)
        or stat_result.st_size <= 0
    ):
        raise CandidateExtractionError("ffmpeg created an invalid evidence artifact")
    if stat_result.st_size > maximum_bytes:
        raise CandidateExtractionOutputLimit(
            "An evidence artifact exceeded its size limit"
        )
    with path.open("rb") as handle:
        header = handle.read(32)
        if media_type == "image/jpeg":
            if not header.startswith(b"\xff\xd8\xff"):
                raise CandidateExtractionError("ffmpeg created an invalid JPEG artifact")
        elif media_type == "video/mp4":
            if b"ftyp" not in header:
                raise CandidateExtractionError("ffmpeg created an invalid MP4 artifact")
    return ArtifactRecord(
        relative_path=path.name,
        size_bytes=int(stat_result.st_size),
        sha256=_sha256_file(path),
        media_type=media_type,
    )


def _source_unchanged(path: Path, identity: _SourceIdentity) -> bool:
    try:
        current = path.stat()
    except OSError:
        return False
    return (
        not path.is_symlink()
        and stat_module.S_ISREG(current.st_mode)
        and int(current.st_size) == identity.size_bytes
        and int(current.st_mtime_ns) == identity.modified_time_ns
    )


def _cleanup_run_directory(run_directory: Optional[Path], output_root: Path) -> None:
    if run_directory is None:
        return
    try:
        if (
            run_directory.parent == output_root
            and run_directory.name.startswith("candidate-extraction-")
            and run_directory.is_dir()
            and not run_directory.is_symlink()
        ):
            shutil.rmtree(run_directory)
    except OSError:
        pass


class CandidateExtractionEngine:
    """Extract deterministic, non-AI review candidates from trusted local media."""

    def __init__(
        self,
        *,
        ffmpeg_path: Optional[Union[str, Path]] = None,
        ffprobe_path: Optional[Union[str, Path]] = None,
        limits: Optional[ExtractionLimits] = None,
        command_runner: Optional[CommandRunner] = None,
    ) -> None:
        self._ffmpeg = _resolve_executable(ffmpeg_path, "ffmpeg")
        self._ffprobe = _resolve_executable(ffprobe_path, "ffprobe")
        self._limits = limits or ExtractionLimits()
        self._runner = command_runner or _run_bounded_command

    def _remaining_timeout(self, deadline: float, command_timeout: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CandidateExtractionTimeout(
                "Candidate extraction exceeded its total time limit"
            )
        return min(command_timeout, remaining)

    def _run(
        self,
        argv: Sequence[str],
        timeout: float,
        output_limit: int,
        cancel: Optional[Callable[[], bool]],
    ) -> _CommandOutput:
        result = self._runner(argv, timeout, output_limit, cancel)
        if not isinstance(result, _CommandOutput):
            raise CandidateExtractionError("The media command runner returned invalid data")
        return result

    def extract(
        self,
        media_path: Union[str, Path],
        output_directory: Union[str, Path],
        *,
        source_sha256: Optional[str] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> ExtractionResult:
        """Create bounded evidence artifacts below a project-owned directory."""
        if source_sha256 is not None:
            source_sha256 = source_sha256.casefold()
            if not _SHA256_PATTERN.fullmatch(source_sha256):
                raise ValueError("source_sha256 must be a lowercase SHA-256 value")
        source_path, source_identity = _validated_source(media_path)
        output_root = _validated_output_root(output_directory)
        if _cancel_requested(cancel):
            raise CandidateExtractionCancelled("Candidate extraction was cancelled")
        required_free_bytes = (
            self._limits.min_free_disk_bytes + self._limits.max_output_bytes
        )
        if shutil.disk_usage(output_root).free < required_free_bytes:
            raise CandidateExtractionOutputLimit(
                "The project output disk cannot fit the bounded run and reserve"
            )

        deadline = time.monotonic() + self._limits.max_total_seconds
        run_directory: Optional[Path] = None
        try:
            run_directory = Path(
                tempfile.mkdtemp(prefix="candidate-extraction-", dir=str(output_root))
            )
            try:
                os.chmod(run_directory, 0o700)
            except OSError:
                pass

            ffmpeg_version = _parse_tool_version(
                self._run(
                    [self._ffmpeg, "-version"],
                    self._remaining_timeout(
                        deadline, self._limits.probe_timeout_seconds
                    ),
                    16 * 1024,
                    cancel,
                ),
                "ffmpeg",
            )
            ffprobe_version = _parse_tool_version(
                self._run(
                    [self._ffprobe, "-version"],
                    self._remaining_timeout(
                        deadline, self._limits.probe_timeout_seconds
                    ),
                    16 * 1024,
                    cancel,
                ),
                "ffprobe",
            )
            probe_argv = [
                self._ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                (
                    "format=duration,size,format_name:"
                    "format_tags=location,location-eng,"
                    "com.apple.quicktime.location.ISO6709:"
                    "stream=index,codec_type,codec_name,width,height,"
                    "avg_frame_rate,r_frame_rate"
                ),
                "-of",
                "json",
                _media_tool_path(source_path),
            ]
            probe = _parse_probe(
                _decode_json(
                    self._run(
                        probe_argv,
                        self._remaining_timeout(
                            deadline, self._limits.probe_timeout_seconds
                        ),
                        self._limits.max_probe_output_bytes,
                        cancel,
                    )
                ),
                source_identity,
                self._limits,
            )
            if not _source_unchanged(source_path, source_identity):
                raise CandidateExtractionError(
                    "The media changed while metadata was inspected"
                )

            denominator = max(1, self._limits.max_sample_frames - 1)
            minimum_gap = max(0.25, probe.duration_seconds / denominator)
            # Preserve the configured 15-second bootstrap cadence for normal
            # trips, but do not let a short clip contribute only its t=0
            # frame. Three bounded intervals provide at least three internal
            # temporal samples even when the final endpoint is not emitted by
            # FFmpeg. This is still a non-classifying recall seed; the full
            # immutable discovery source remains the 2 FPS dense index.
            effective_cadence = _effective_bootstrap_cadence(
                duration_seconds=probe.duration_seconds,
                configured_cadence_seconds=(
                    self._limits.cadence_seconds
                ),
                minimum_gap_seconds=minimum_gap,
            )
            escaped_select = (
                "select='if(isnan(prev_selected_t)\\,1\\,"
                "if(gte(t-prev_selected_t\\,"
                f"{minimum_gap:.6f})"
                "*gt(gte(scene\\,"
                f"{self._limits.scene_threshold:.6f})"
                "+gte(t-prev_selected_t\\,"
                f"{effective_cadence:.6f})\\,0)\\,1\\,0))'"
            )
            scan_filter = (
                f"{escaped_select},"
                "edgedetect=low=0.100000:high=0.400000,"
                "signalstats,metadata=mode=print:file=-"
            )
            scan_argv = [
                self._ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-i",
                _media_tool_path(source_path),
                "-map",
                f"0:{probe.stream_index}",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                scan_filter,
                "-frames:v",
                str(self._limits.max_sample_frames),
                "-vsync",
                "0",
                "-f",
                "null",
                "-",
            ]
            scan_output = self._run(
                scan_argv,
                self._remaining_timeout(
                    deadline, self._limits.scan_timeout_seconds
                ),
                self._limits.max_scan_output_bytes,
                cancel,
            )
            if scan_output.returncode != 0:
                raise CandidateExtractionError(
                    "ffmpeg could not sample the validated media"
                )
            if not _source_unchanged(source_path, source_identity):
                raise CandidateExtractionError(
                    "The media changed while candidate frames were sampled"
                )
            observations = _parse_observations(
                scan_output.stdout,
                probe.duration_seconds,
                self._limits.scene_threshold,
                effective_cadence,
                self._limits.max_sample_frames,
            )
            grouped = _group_observations(
                observations,
                self._limits.group_gap_seconds,
                self._limits.max_candidate_groups,
            )
            # Keep evidence inside the configured width without upscaling and
            # force FFmpeg's computed height to an even value.  The previous
            # force_original_aspect_ratio form could turn a 1440x1200 frame
            # into 1600x1333 on FFmpeg 4.4.1, which libx264 rejects.
            evidence_scale_filter = (
                f"scale=w=min(iw\\,{self._limits.max_frame_width}):"
                "h=-2:flags=lanczos"
            )

            source_fingerprint_material = (
                source_sha256
                or (
                    f"{source_path.name}\0{source_identity.size_bytes}\0"
                    f"{source_identity.modified_time_ns}"
                )
            )
            source_fingerprint = hashlib.sha256(
                source_fingerprint_material.encode("utf-8")
            ).hexdigest()
            candidates: List[CandidateGroup] = []
            total_output_bytes = 0
            for group_index, group in enumerate(grouped, start=1):
                if _cancel_requested(cancel):
                    raise CandidateExtractionCancelled(
                        "Candidate extraction was cancelled"
                    )
                if not _source_unchanged(source_path, source_identity):
                    raise CandidateExtractionError(
                        "The media changed during candidate extraction"
                    )
                representative = max(
                    group,
                    key=lambda item: (
                        item.scene_score,
                        item.sharpness_score,
                        -item.timestamp_seconds,
                    ),
                )
                identity_text = (
                    f"{source_fingerprint}|{group_index}|"
                    f"{group[0].timestamp_seconds:.6f}|"
                    f"{group[-1].timestamp_seconds:.6f}|"
                    f"{representative.timestamp_seconds:.6f}"
                )
                candidate_id = hashlib.sha256(
                    identity_text.encode("utf-8")
                ).hexdigest()[:20]
                timestamp_ms = int(
                    round(representative.timestamp_seconds * 1000.0)
                )
                prefix = (
                    f"candidate-{group_index:04d}-{timestamp_ms:012d}-"
                    f"{candidate_id[:8]}"
                )
                remaining_output_bytes = (
                    self._limits.max_output_bytes - total_output_bytes
                )
                if remaining_output_bytes <= 0:
                    raise CandidateExtractionOutputLimit(
                        "Candidate artifacts reached the run output limit"
                    )
                image_budget_bytes = min(
                    self._limits.max_frame_bytes, remaining_output_bytes
                )
                image_path = run_directory / f"{prefix}.jpg"
                image_argv = [
                    self._ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    f"{representative.timestamp_seconds:.6f}",
                    "-i",
                    _media_tool_path(source_path),
                    "-map",
                    f"0:{probe.stream_index}",
                    "-frames:v",
                    "1",
                    "-an",
                    "-sn",
                    "-dn",
                    "-vf",
                    evidence_scale_filter,
                    "-q:v",
                    "3",
                    "-fs",
                    str(image_budget_bytes),
                    "-n",
                    _media_tool_path(image_path),
                ]
                image_output = self._run(
                    image_argv,
                    self._remaining_timeout(
                        deadline, self._limits.artifact_timeout_seconds
                    ),
                    512 * 1024,
                    cancel,
                )
                if image_output.returncode != 0:
                    raise CandidateExtractionError(
                        "ffmpeg could not create a representative image"
                    )
                image_record = _validate_artifact(
                    image_path,
                    run_directory,
                    image_budget_bytes,
                    "image/jpeg",
                )
                total_output_bytes += image_record.size_bytes

                actual_preview_duration = min(
                    self._limits.preview_duration_seconds,
                    probe.duration_seconds,
                )
                preview_start = max(
                    0.0,
                    min(
                        representative.timestamp_seconds
                        - actual_preview_duration / 2.0,
                        probe.duration_seconds - actual_preview_duration,
                    ),
                )
                remaining_output_bytes = (
                    self._limits.max_output_bytes - total_output_bytes
                )
                if remaining_output_bytes <= 0:
                    raise CandidateExtractionOutputLimit(
                        "Candidate artifacts reached the run output limit"
                    )
                clip_budget_bytes = min(
                    self._limits.max_clip_bytes, remaining_output_bytes
                )
                clip_path = run_directory / f"{prefix}.mp4"
                clip_argv = [
                    self._ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    f"{preview_start:.6f}",
                    "-i",
                    _media_tool_path(source_path),
                    "-t",
                    f"{actual_preview_duration:.6f}",
                    "-map",
                    f"0:{probe.stream_index}",
                    "-an",
                    "-sn",
                    "-dn",
                    "-vf",
                    evidence_scale_filter,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "26",
                    "-profile:v",
                    "main",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    "-f",
                    "mp4",
                    "-fs",
                    str(clip_budget_bytes),
                    "-n",
                    _media_tool_path(clip_path),
                ]
                clip_output = self._run(
                    clip_argv,
                    self._remaining_timeout(
                        deadline, self._limits.artifact_timeout_seconds
                    ),
                    512 * 1024,
                    cancel,
                )
                if clip_output.returncode != 0:
                    raise CandidateExtractionError(
                        "ffmpeg could not create a preview clip"
                    )
                clip_record = _validate_artifact(
                    clip_path,
                    run_directory,
                    clip_budget_bytes,
                    "video/mp4",
                )
                total_output_bytes += clip_record.size_bytes
                if total_output_bytes > self._limits.max_output_bytes:
                    raise CandidateExtractionOutputLimit(
                        "Candidate artifacts exceeded the run output limit"
                    )
                if shutil.disk_usage(output_root).free < self._limits.min_free_disk_bytes:
                    raise CandidateExtractionOutputLimit(
                        "The project output disk reserve was exhausted"
                    )

                triggers = tuple(
                    dict.fromkeys(
                        trigger
                        for observation in group
                        for trigger in observation.triggers
                    )
                )
                candidates.append(
                    CandidateGroup(
                        candidate_id=candidate_id,
                        start_seconds=group[0].timestamp_seconds,
                        end_seconds=group[-1].timestamp_seconds,
                        representative_timestamp_seconds=(
                            representative.timestamp_seconds
                        ),
                        scene_score=representative.scene_score,
                        sharpness_score=representative.sharpness_score,
                        triggers=triggers,
                        observations=group,
                        representative_image=image_record,
                        preview_clip=clip_record,
                        preview_start_seconds=round(preview_start, 6),
                        preview_duration_seconds=round(
                            actual_preview_duration, 6
                        ),
                    )
                )

            if not _source_unchanged(source_path, source_identity):
                raise CandidateExtractionError(
                    "The media changed during candidate extraction"
                )
            provenance = ExtractionProvenance(
                source_name=source_path.name,
                source_size_bytes=source_identity.size_bytes,
                source_modified_time_ns=source_identity.modified_time_ns,
                source_fingerprint=source_fingerprint,
                source_sha256=source_sha256,
                duration_seconds=round(probe.duration_seconds, 6),
                format_names=probe.format_names,
                video_stream_index=probe.stream_index,
                video_codec=probe.codec_name,
                width=probe.width,
                height=probe.height,
                frame_rate=probe.frame_rate,
                ffmpeg_version=ffmpeg_version,
                ffprobe_version=ffprobe_version,
                scene_score_method="ffmpeg select scene score normalized to 0..1",
                sharpness_score_method=(
                    "edge-map luma mean via edgedetect+signalstats, divided by 255"
                ),
                effective_cadence_seconds=round(effective_cadence, 6),
                minimum_selection_gap_seconds=round(minimum_gap, 6),
                limits=asdict(self._limits),
                source_location=probe.source_location,
            )
            return ExtractionResult(
                output_directory=str(run_directory),
                candidates=tuple(candidates),
                provenance=provenance,
            )
        except Exception:
            _cleanup_run_directory(run_directory, output_root)
            raise


def extract_candidates(
    media_path: Union[str, Path],
    output_directory: Union[str, Path],
    *,
    source_sha256: Optional[str] = None,
    cancel: Optional[Callable[[], bool]] = None,
    ffmpeg_path: Optional[Union[str, Path]] = None,
    ffprobe_path: Optional[Union[str, Path]] = None,
    limits: Optional[ExtractionLimits] = None,
) -> ExtractionResult:
    """Convenience API used by trusted bridge orchestration code."""
    return CandidateExtractionEngine(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        limits=limits,
    ).extract(
        media_path,
        output_directory,
        source_sha256=source_sha256,
        cancel=cancel,
    )
