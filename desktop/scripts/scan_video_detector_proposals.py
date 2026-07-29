r"""Bounded, resumable dense-video detector evaluation for TrafficVisson.

This operator tool samples a source video with ffmpeg and evaluates the sampled
frames with an explicitly installed local ONNX model pack.  It is deliberately
separate from the application analysis, labeling, incident, and model-promotion
stores.

Safety contract
---------------

* The source video and model pack are opened read-only and SHA-256 verified.
* The only writable location is a new run directory under
  ``%PROGRAMDATA%\TrafficVisson\ModelEvaluations``.
* No analysis JSON, label, SQLite, incident, report, promotion, or experimental
  sidecar is read or written.
* Frame count, media time, wall time, temporary bytes, final bytes, subprocess
  output, inference batch size, and evidence count are bounded.
* Detector ``model_score`` values are uncalibrated model outputs.  They are not
  probabilities, accuracy measurements, labels, or maintenance decisions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from uuid import uuid4


DESKTOP_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = DESKTOP_ROOT / "bridge"
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from app.vision_inference import (  # noqa: E402
    LocalVisionInferenceService,
    VisionInferenceError,
    load_model_pack,
)


SCAN_SCHEMA = "trafficvisson.dense-detector-scan.v1"
CHECKPOINT_SCHEMA = "trafficvisson.dense-detector-checkpoint.v1"
MANIFEST_SCHEMA = "trafficvisson.dense-detector-artifacts.v1"
RUN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
FRAME_NAME_PATTERN = re.compile(r"^frame-(?P<index>[0-9]{6})\.jpg$")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
MAX_SOURCE_BYTES = 256 * 1024**3
MAX_MANIFEST_BYTES = 128 * 1024
MAX_FRAME_BYTES = 24 * 1024 * 1024
MAX_FRAME_PIXELS = 24_000_000
MAX_FRAMES = 10_000
MAX_BATCH_SIZE = 512
MAX_CAPTURE_BYTES = 1 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024 * 1024
DEFAULT_TEMP_BYTES = 512 * 1024 * 1024
DEFAULT_OUTPUT_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_WALL_SECONDS = 900.0

DISCLAIMER_KO = (
    "이 결과는 사용자 검토를 위한 비파괴 실험 후보입니다. model_score는 "
    "현재 TrafficVisson 한국 도로 고정 검증 세트에서 보정되지 않은 모델 "
    "출력값이며 실제 결함 확률, 정확도, 확정 라벨 또는 유지보수 판정이 "
    "아닙니다. 자동 라벨, 사건 생성, 보고서 확정 입력, 모델 승격에 "
    "사용되지 않았습니다."
)


class DenseScanError(RuntimeError):
    """Deterministic operator-facing dense-scan failure."""


class SourceMutationError(DenseScanError):
    """Raised when a protected source changes during the scan."""


class ResourceLimitError(DenseScanError):
    """Raised when a configured wall-time or storage bound is reached."""


@dataclass(frozen=True)
class FileFingerprint:
    size_bytes: int
    modified_time_ns: int
    sha256: str


@dataclass(frozen=True)
class VideoProbe:
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    format_name: str


@dataclass(frozen=True)
class SampledFrame:
    frame_id: str
    frame_index: int
    timestamp_seconds: float
    relative_path: str
    size_bytes: int
    sha256: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    extensions: Optional[set[str]] = None,
) -> Path:
    if not path.is_absolute():
        raise DenseScanError("A protected input path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except OSError as error:
        raise DenseScanError("A protected input file is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not resolved.is_file()
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum_bytes
        or (
            extensions is not None
            and resolved.suffix.casefold() not in extensions
        )
    ):
        raise DenseScanError("A protected input file is invalid")
    return resolved


def _safe_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DenseScanError(f"{label} is unavailable") from error
    if path.is_symlink() or _is_reparse_point(path) or not resolved.is_dir():
        raise DenseScanError(f"{label} is unsafe")
    return resolved


def _sha256_file(
    path: Path,
    *,
    maximum_bytes: Optional[int] = None,
    deadline: Optional[float] = None,
) -> str:
    try:
        before = path.stat()
    except OSError as error:
        raise DenseScanError("A protected file is unavailable") from error
    if maximum_bytes is not None and before.st_size > maximum_bytes:
        raise ResourceLimitError("A protected file exceeds its size bound")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(4 * 1024 * 1024):
                if deadline is not None and time.monotonic() > deadline:
                    raise ResourceLimitError("The dense scan exceeded its wall-time bound")
                total += len(block)
                if maximum_bytes is not None and total > maximum_bytes:
                    raise ResourceLimitError("A protected file exceeds its size bound")
                digest.update(block)
        after = path.stat()
    except OSError as error:
        raise DenseScanError("A protected file could not be read") from error
    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise SourceMutationError("A protected file changed while it was hashed")
    return digest.hexdigest()


def _fingerprint(
    path: Path,
    *,
    maximum_bytes: Optional[int] = None,
    deadline: Optional[float] = None,
) -> FileFingerprint:
    metadata = path.stat()
    return FileFingerprint(
        size_bytes=int(metadata.st_size),
        modified_time_ns=int(metadata.st_mtime_ns),
        sha256=_sha256_file(
            path,
            maximum_bytes=maximum_bytes,
            deadline=deadline,
        ),
    )


def _verify_fingerprint(
    path: Path,
    expected: FileFingerprint,
    *,
    maximum_bytes: Optional[int] = None,
    deadline: Optional[float] = None,
) -> None:
    actual = _fingerprint(
        path,
        maximum_bytes=maximum_bytes,
        deadline=deadline,
    )
    if actual != expected:
        raise SourceMutationError("A protected source changed during the dense scan")


def _strict_json_object(raw: bytes, label: str) -> dict[str, Any]:
    def object_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DenseScanError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> object:
        raise DenseScanError(f"{label} contains a non-finite JSON number")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except DenseScanError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DenseScanError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise DenseScanError(f"{label} must be a JSON object")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAX_REPORT_BYTES:
        raise ResourceLimitError("A dense-scan JSON artifact exceeded its bound")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _directory_size(path: Path, *, maximum: Optional[int] = None) -> int:
    total = 0
    if not path.exists():
        return 0
    for current, directories, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink() or _is_reparse_point(current_path):
            raise DenseScanError("The scan output contains an unsafe directory")
        for name in directories:
            nested = current_path / name
            if nested.is_symlink() or _is_reparse_point(nested):
                raise DenseScanError("The scan output contains a linked directory")
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink() or _is_reparse_point(candidate):
                raise DenseScanError("The scan output contains a linked file")
            total += candidate.stat().st_size
            if maximum is not None and total > maximum:
                raise ResourceLimitError("The scan storage bound was exceeded")
    return total


def _published_size(path: Path, *, maximum: Optional[int] = None) -> int:
    """Return bytes outside the transient ``.staging`` tree."""
    total = 0
    if not path.exists():
        return 0
    for current, directories, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        if current_path == path:
            directories[:] = [
                name for name in directories if name != ".staging"
            ]
        if current_path.is_symlink() or _is_reparse_point(current_path):
            raise DenseScanError("The scan output contains an unsafe directory")
        for name in files:
            candidate = current_path / name
            if candidate.is_symlink() or _is_reparse_point(candidate):
                raise DenseScanError("The scan output contains a linked file")
            total += candidate.stat().st_size
            if maximum is not None and total > maximum:
                raise ResourceLimitError("The final output bound was exceeded")
    return total


def _remaining_seconds(deadline: float, *, minimum: float = 0.1) -> float:
    remaining = deadline - time.monotonic()
    if remaining < minimum:
        raise ResourceLimitError("The dense scan exceeded its wall-time bound")
    return remaining


def _resolve_tool(value: str, label: str) -> Path:
    candidate = Path(value)
    resolved_text = (
        str(candidate)
        if candidate.is_absolute()
        else shutil.which(value)
    )
    if not resolved_text:
        raise DenseScanError(f"{label} is unavailable")
    return _safe_regular_file(
        Path(resolved_text),
        maximum_bytes=256 * 1024 * 1024,
    )


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.terminate()
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass


def _run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    capture_root: Path,
    deadline: float,
    storage_watch: Optional[Path] = None,
    storage_limit_bytes: Optional[int] = None,
    capture_limit_bytes: int = MAX_CAPTURE_BYTES,
) -> tuple[bytes, bytes]:
    stdout_path = capture_root / f".process-{uuid4().hex}.stdout"
    stderr_path = capture_root / f".process-{uuid4().hex}.stderr"
    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                cwd=str(cwd),
                close_fds=True,
                creationflags=creation_flags,
            )
            while process.poll() is None:
                if time.monotonic() > deadline:
                    _terminate_process(process)
                    raise ResourceLimitError(
                        "A media subprocess exceeded the wall-time bound"
                    )
                stdout.flush()
                stderr.flush()
                captured = stdout_path.stat().st_size + stderr_path.stat().st_size
                if captured > capture_limit_bytes:
                    _terminate_process(process)
                    raise ResourceLimitError(
                        "A media subprocess exceeded its captured-output bound"
                    )
                if storage_watch is not None and storage_limit_bytes is not None:
                    _directory_size(storage_watch, maximum=storage_limit_bytes)
                time.sleep(0.05)
        if process.returncode != 0:
            message = stderr_path.read_bytes()[:4_096].decode(
                "utf-8", errors="replace"
            )
            raise DenseScanError(
                "A media subprocess failed"
                + (f": {message.strip()}" if message.strip() else "")
            )
        stdout_raw = stdout_path.read_bytes()
        stderr_raw = stderr_path.read_bytes()
        if len(stdout_raw) + len(stderr_raw) > capture_limit_bytes:
            raise ResourceLimitError(
                "A media subprocess exceeded its captured-output bound"
            )
        return stdout_raw, stderr_raw
    except OSError as error:
        if process is not None and process.poll() is None:
            _terminate_process(process)
        raise DenseScanError("A media subprocess could not be executed") from error
    finally:
        for path in (stdout_path, stderr_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _probe_video(
    source_video: Path,
    *,
    ffprobe: Path,
    working_root: Path,
    deadline: float,
) -> VideoProbe:
    stdout, _ = _run_bounded_process(
        [
            str(ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            (
                "format=duration,format_name:"
                "stream=index,codec_type,codec_name,width,height"
            ),
            "-i",
            str(source_video),
        ],
        cwd=working_root,
        capture_root=working_root,
        deadline=deadline,
        capture_limit_bytes=512 * 1024,
    )
    payload = _strict_json_object(stdout, "ffprobe output")
    streams = payload.get("streams")
    format_row = payload.get("format")
    if not isinstance(streams, list) or not isinstance(format_row, Mapping):
        raise DenseScanError("ffprobe returned an invalid video envelope")
    video_streams = [
        row
        for row in streams
        if isinstance(row, Mapping) and row.get("codec_type") == "video"
    ]
    if len(video_streams) != 1:
        raise DenseScanError("The source must contain exactly one video stream")
    video = video_streams[0]
    try:
        duration = float(format_row["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise DenseScanError("The video duration is invalid") from error
    width = video.get("width")
    height = video.get("height")
    codec = video.get("codec_name")
    format_name = format_row.get("format_name")
    if (
        not math.isfinite(duration)
        or not 0 < duration <= 24 * 60 * 60
        or isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or not 1 <= width <= 8_192
        or not 1 <= height <= 8_192
        or width * height > MAX_FRAME_PIXELS
        or not isinstance(codec, str)
        or not 1 <= len(codec) <= 64
        or not isinstance(format_name, str)
        or not 1 <= len(format_name) <= 128
    ):
        raise DenseScanError("ffprobe returned unsupported video properties")
    return VideoProbe(
        duration_seconds=round(duration, 6),
        width=width,
        height=height,
        video_codec=codec,
        format_name=format_name,
    )


def _tool_version(
    tool: Path,
    *,
    working_root: Path,
    deadline: float,
) -> str:
    stdout, _ = _run_bounded_process(
        [str(tool), "-version"],
        cwd=working_root,
        capture_root=working_root,
        deadline=deadline,
        capture_limit_bytes=256 * 1024,
    )
    first = stdout.decode("utf-8", errors="replace").splitlines()
    if not first or not first[0].strip():
        raise DenseScanError("A media tool version could not be determined")
    return first[0].strip()[:500]


def _sampling_rate(
    *,
    fps: Optional[float],
    interval_seconds: Optional[float],
) -> tuple[float, str, float]:
    if (fps is None) == (interval_seconds is None):
        raise DenseScanError("Choose exactly one of fps or interval_seconds")
    if fps is not None:
        if (
            isinstance(fps, bool)
            or not isinstance(fps, (int, float))
            or not math.isfinite(float(fps))
            or not 0.01 <= float(fps) <= 30
        ):
            raise DenseScanError("fps must be between 0.01 and 30")
        rate = float(fps)
        return rate, f"fps={rate:.12g}", 1.0 / rate
    assert interval_seconds is not None
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(float(interval_seconds))
        or not 1 / 30 <= float(interval_seconds) <= 600
    ):
        raise DenseScanError(
            "interval_seconds must be between 1/30 and 600 seconds"
        )
    interval = float(interval_seconds)
    rate = 1.0 / interval
    return rate, f"fps=1/{interval:.12g}", interval


def _sample_frames(
    source_video: Path,
    staging_root: Path,
    *,
    ffmpeg: Path,
    rate_filter: str,
    sample_interval_seconds: float,
    max_frames: int,
    max_video_seconds: float,
    max_temporary_bytes: int,
    deadline: float,
) -> list[SampledFrame]:
    frames_root = staging_root / "frames"
    frames_root.mkdir(exist_ok=False)
    if frames_root.is_symlink() or _is_reparse_point(frames_root):
        raise DenseScanError("The temporary frame directory is unsafe")
    _run_bounded_process(
        [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source_video),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-t",
            f"{max_video_seconds:.6f}",
            "-vf",
            rate_filter,
            "-frames:v",
            str(max_frames),
            "-q:v",
            "4",
            "-start_number",
            "1",
            str(frames_root / "frame-%06d.jpg"),
        ],
        cwd=staging_root,
        capture_root=staging_root,
        deadline=deadline,
        storage_watch=staging_root,
        storage_limit_bytes=max_temporary_bytes,
    )
    paths = sorted(frames_root.glob("frame-*.jpg"), key=lambda value: value.name)
    if not paths or len(paths) > max_frames:
        raise DenseScanError("ffmpeg returned an invalid sampled-frame count")
    rows: list[SampledFrame] = []
    total = 0
    for expected_index, path in enumerate(paths, start=1):
        match = FRAME_NAME_PATTERN.fullmatch(path.name)
        if match is None or int(match.group("index")) != expected_index:
            raise DenseScanError("Sampled frame numbering is not contiguous")
        resolved = _safe_regular_file(path, maximum_bytes=MAX_FRAME_BYTES)
        size = resolved.stat().st_size
        total += size
        if total > max_temporary_bytes:
            raise ResourceLimitError("Sampled frames exceeded the temporary bound")
        digest = _sha256_file(
            resolved,
            maximum_bytes=MAX_FRAME_BYTES,
            deadline=deadline,
        )
        frame_id = hashlib.sha256(
            f"{expected_index}\0{digest}".encode("ascii")
        ).hexdigest()[:20]
        rows.append(
            SampledFrame(
                frame_id=frame_id,
                frame_index=expected_index,
                timestamp_seconds=round(
                    (expected_index - 1) * sample_interval_seconds,
                    6,
                ),
                relative_path=f".staging/frames/{path.name}",
                size_bytes=size,
                sha256=digest,
            )
        )
    return rows


def _checkpoint_config_digest(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        config,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_checkpoint(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise DenseScanError("The resume checkpoint is unavailable") from error
    if len(raw) > MAX_REPORT_BYTES:
        raise DenseScanError("The resume checkpoint is too large")
    value = _strict_json_object(raw, "Dense-scan checkpoint")
    if value.get("schema_version") != CHECKPOINT_SCHEMA:
        raise DenseScanError("The resume checkpoint schema is unsupported")
    return value


def _validate_resume_frames(
    run_root: Path,
    rows: Any,
    *,
    max_frames: int,
    max_temporary_bytes: int,
    deadline: float,
) -> list[SampledFrame]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= max_frames:
        raise DenseScanError("The resume checkpoint frame inventory is invalid")
    frames: list[SampledFrame] = []
    total = 0
    for expected_index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise DenseScanError("A resume checkpoint frame is invalid")
        try:
            frame = SampledFrame(
                frame_id=str(row["frame_id"]),
                frame_index=int(row["frame_index"]),
                timestamp_seconds=float(row["timestamp_seconds"]),
                relative_path=str(row["relative_path"]),
                size_bytes=int(row["size_bytes"]),
                sha256=str(row["sha256"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DenseScanError("A resume checkpoint frame is invalid") from error
        relative = Path(frame.relative_path)
        if (
            frame.frame_index != expected_index
            or not re.fullmatch(r"[a-f0-9]{20}", frame.frame_id)
            or not SHA256_PATTERN.fullmatch(frame.sha256)
            or relative.is_absolute()
            or relative.parts[:2] != (".staging", "frames")
            or ".." in relative.parts
            or not math.isfinite(frame.timestamp_seconds)
            or frame.timestamp_seconds < 0
            or not 1 <= frame.size_bytes <= MAX_FRAME_BYTES
        ):
            raise DenseScanError("A resume checkpoint frame value is invalid")
        path = (run_root / relative).resolve(strict=True)
        frames_root = (run_root / ".staging" / "frames").resolve(strict=True)
        if path.parent != frames_root:
            raise DenseScanError("A resume frame escaped the run directory")
        verified = _safe_regular_file(path, maximum_bytes=MAX_FRAME_BYTES)
        if (
            verified.stat().st_size != frame.size_bytes
            or _sha256_file(
                verified,
                maximum_bytes=MAX_FRAME_BYTES,
                deadline=deadline,
            )
            != frame.sha256
        ):
            raise DenseScanError("A resume frame failed integrity checks")
        total += frame.size_bytes
        if total > max_temporary_bytes:
            raise ResourceLimitError("Resume frames exceed the temporary bound")
        frames.append(frame)
    return frames


def _normalize_inference(
    frame: SampledFrame,
    value: Any,
    *,
    expected_model: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DenseScanError("A detector result is invalid")
    if (
        value.get("mode") != "local_model"
        or value.get("provider") != "onnx_yolo_v8"
        or value.get("pack_id") != expected_model.get("pack_id")
        or value.get("pack_version") != expected_model.get("pack_version")
        or value.get("manifest_sha256")
        != expected_model.get("manifest_sha256")
        or value.get("status") not in {"classified", "no_detection"}
        or value.get("warning_code") is not None
    ):
        raise DenseScanError("A detector result identity or status is invalid")
    raw_predictions = value.get("predictions")
    if not isinstance(raw_predictions, list) or len(raw_predictions) > 32:
        raise DenseScanError("Detector predictions are invalid")
    predictions: list[dict[str, Any]] = []
    for prediction in raw_predictions:
        if not isinstance(prediction, Mapping):
            raise DenseScanError("A detector prediction is invalid")
        score = prediction.get("confidence")
        box = prediction.get("box")
        issue_type = prediction.get("issue_type")
        source_label = prediction.get("source_label")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(float(score))
            or not 0 <= float(score) <= 1
            or prediction.get("calibration") != "model_score"
            or not isinstance(issue_type, str)
            or not 1 <= len(issue_type) <= 64
            or not isinstance(source_label, str)
            or not 1 <= len(source_label) <= 128
            or not isinstance(box, Mapping)
            or set(box) != {"x", "y", "width", "height"}
        ):
            raise DenseScanError("A detector prediction violates the ONNX contract")
        normalized_box: dict[str, float] = {}
        for key in ("x", "y", "width", "height"):
            number = box[key]
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(float(number))
            ):
                raise DenseScanError("A detector box value is invalid")
            normalized_box[key] = round(float(number), 8)
        if (
            not 0 <= normalized_box["x"] <= 1
            or not 0 <= normalized_box["y"] <= 1
            or not 0 < normalized_box["width"] <= 1
            or not 0 < normalized_box["height"] <= 1
            or normalized_box["x"] + normalized_box["width"] > 1.00000001
            or normalized_box["y"] + normalized_box["height"] > 1.00000001
        ):
            raise DenseScanError("A detector box is outside the image")
        predictions.append(
            {
                "issue_type": issue_type,
                "model_score": round(float(score), 8),
                "score_semantics": "model_score_not_probability",
                "is_probability": False,
                "box": normalized_box,
                "source_label": source_label,
                "calibration": "model_score",
                "temporal_group_id": None,
            }
        )
    predictions.sort(key=lambda row: row["model_score"], reverse=True)
    if (value.get("status") == "classified") != bool(predictions):
        raise DenseScanError("A detector status does not match its predictions")
    duration_ms = value.get("duration_ms")
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or not 0 <= duration_ms <= 3_600_000
    ):
        raise DenseScanError("A detector duration is invalid")
    return {
        "frame_id": frame.frame_id,
        "frame_index": frame.frame_index,
        "timestamp_seconds": frame.timestamp_seconds,
        "sampled_frame_ref": frame.relative_path,
        "sampled_frame_size_bytes": frame.size_bytes,
        "sampled_frame_sha256": frame.sha256,
        "status": value["status"],
        "duration_ms": duration_ms,
        "predictions": predictions,
        "review_required": bool(predictions),
        "review_state": "unreviewed",
        "automatic_label_created": False,
        "incident_created": False,
        "model_promoted": False,
        "experimental_sidecar_created": False,
    }


def _box_iou(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    lx1 = float(left["x"])
    ly1 = float(left["y"])
    lx2 = lx1 + float(left["width"])
    ly2 = ly1 + float(left["height"])
    rx1 = float(right["x"])
    ry1 = float(right["y"])
    rx2 = rx1 + float(right["width"])
    ry2 = ry1 + float(right["height"])
    intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
        0.0, min(ly2, ry2) - max(ly1, ry1)
    )
    union = (
        float(left["width"]) * float(left["height"])
        + float(right["width"]) * float(right["height"])
        - intersection
    )
    return 0.0 if union <= 0 else intersection / union


def _assign_temporal_groups(
    results: list[dict[str, Any]],
    *,
    source_sha256: str,
    max_gap_seconds: float,
    minimum_iou: float,
) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    for result in sorted(results, key=lambda row: row["timestamp_seconds"]):
        timestamp = float(result["timestamp_seconds"])
        for prediction_index, prediction in enumerate(result["predictions"]):
            matches = [
                group
                for group in active
                if group["issue_type"] == prediction["issue_type"]
                and timestamp - group["last_timestamp_seconds"]
                <= max_gap_seconds
                and _box_iou(group["last_box"], prediction["box"])
                >= minimum_iou
            ]
            group: Optional[dict[str, Any]] = None
            if matches:
                group = max(
                    matches,
                    key=lambda row: _box_iou(row["last_box"], prediction["box"]),
                )
            if group is None:
                group_id = hashlib.sha256(
                    (
                        f"{source_sha256}\0{result['frame_id']}\0"
                        f"{prediction['issue_type']}\0{prediction_index}"
                    ).encode("utf-8")
                ).hexdigest()[:20]
                group = {
                    "temporal_group_id": group_id,
                    "issue_type": prediction["issue_type"],
                    "start_timestamp_seconds": timestamp,
                    "end_timestamp_seconds": timestamp,
                    "last_timestamp_seconds": timestamp,
                    "last_box": prediction["box"],
                    "sampled_frame_count": 0,
                    "detection_count": 0,
                    "frame_ids": [],
                    "max_model_score": -1.0,
                    "representative": None,
                    "score_semantics": "model_score_not_probability",
                    "review_required": True,
                    "review_state": "unreviewed",
                    "automatic_label_created": False,
                    "incident_created": False,
                    "model_promoted": False,
                    "experimental_sidecar_created": False,
                }
                groups.append(group)
                active.append(group)
            prediction["temporal_group_id"] = group["temporal_group_id"]
            group["end_timestamp_seconds"] = timestamp
            group["last_timestamp_seconds"] = timestamp
            group["last_box"] = prediction["box"]
            group["detection_count"] += 1
            if result["frame_id"] not in group["frame_ids"]:
                group["frame_ids"].append(result["frame_id"])
                group["sampled_frame_count"] += 1
            if prediction["model_score"] > group["max_model_score"]:
                group["max_model_score"] = prediction["model_score"]
                group["representative"] = {
                    "frame_id": result["frame_id"],
                    "frame_index": result["frame_index"],
                    "timestamp_seconds": timestamp,
                    "sampled_frame_ref": result["sampled_frame_ref"],
                    "sampled_frame_sha256": result["sampled_frame_sha256"],
                    "model_score": prediction["model_score"],
                    "box": prediction["box"],
                }
        active = [
            group
            for group in active
            if timestamp - group["last_timestamp_seconds"] <= max_gap_seconds
        ]
    safe_groups: list[dict[str, Any]] = []
    for group in groups:
        safe_groups.append(
            {
                key: value
                for key, value in group.items()
                if key not in {"last_timestamp_seconds", "last_box"}
            }
        )
    return safe_groups


def _write_scores_csv(path: Path, results: Sequence[Mapping[str, Any]]) -> None:
    fields = [
        "frame_id",
        "frame_index",
        "timestamp_seconds",
        "status",
        "review_required",
        "rank",
        "issue_type",
        "model_score",
        "score_semantics",
        "is_probability",
        "box_json",
        "temporal_group_id",
        "sampled_frame_sha256",
        "automatic_label_created",
        "incident_created",
        "model_promoted",
        "experimental_sidecar_created",
    ]
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            predictions = result["predictions"]
            rows = predictions if predictions else [None]
            for rank, prediction in enumerate(rows, start=1):
                writer.writerow(
                    {
                        "frame_id": result["frame_id"],
                        "frame_index": result["frame_index"],
                        "timestamp_seconds": result["timestamp_seconds"],
                        "status": result["status"],
                        "review_required": result["review_required"],
                        "rank": rank if prediction is not None else "",
                        "issue_type": (
                            prediction["issue_type"]
                            if prediction is not None
                            else ""
                        ),
                        "model_score": (
                            prediction["model_score"]
                            if prediction is not None
                            else ""
                        ),
                        "score_semantics": (
                            prediction["score_semantics"]
                            if prediction is not None
                            else "model_score_not_probability"
                        ),
                        "is_probability": False,
                        "box_json": (
                            json.dumps(
                                prediction["box"],
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                            if prediction is not None
                            else ""
                        ),
                        "temporal_group_id": (
                            prediction["temporal_group_id"]
                            if prediction is not None
                            else ""
                        ),
                        "sampled_frame_sha256": result[
                            "sampled_frame_sha256"
                        ],
                        "automatic_label_created": False,
                        "incident_created": False,
                        "model_promoted": False,
                        "experimental_sidecar_created": False,
                    }
                )


def _copy_evidence(
    run_root: Path,
    results: Sequence[Mapping[str, Any]],
    *,
    max_evidence_frames: int,
    max_output_bytes: int,
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    evidence_root = run_root / "evidence"
    evidence_root.mkdir(exist_ok=False)
    ranked = sorted(
        (row for row in results if row["predictions"]),
        key=lambda row: row["predictions"][0]["model_score"],
        reverse=True,
    )
    selection_kind = "detector_proposal"
    selected = ranked[:max_evidence_frames]
    if not selected:
        selection_kind = "no_detection_sample"
        selected = list(results[:1])
    evidence: list[dict[str, Any]] = []
    paths: dict[str, Path] = {}
    output_total = _published_size(run_root)
    for result in selected:
        source = (run_root / result["sampled_frame_ref"]).resolve(strict=True)
        target = evidence_root / (
            f"frame-{int(result['frame_index']):06d}-{result['frame_id']}.jpg"
        )
        size = source.stat().st_size
        if output_total + size > max_output_bytes:
            raise ResourceLimitError("Evidence frames exceed the output bound")
        shutil.copyfile(source, target)
        copied = _safe_regular_file(target, maximum_bytes=MAX_FRAME_BYTES)
        digest = _sha256_file(copied, maximum_bytes=MAX_FRAME_BYTES)
        if size != copied.stat().st_size or digest != result["sampled_frame_sha256"]:
            raise DenseScanError("An evidence copy failed integrity checks")
        output_total += size
        relative = target.relative_to(run_root).as_posix()
        paths[result["frame_id"]] = target
        evidence.append(
            {
                "frame_id": result["frame_id"],
                "frame_index": result["frame_index"],
                "timestamp_seconds": result["timestamp_seconds"],
                "selection_kind": selection_kind,
                "relative_path": relative,
                "size_bytes": size,
                "sha256": digest,
                "review_state": "unreviewed",
            }
        )
    return evidence, paths


def _write_contact_sheet(
    path: Path,
    results: Sequence[Mapping[str, Any]],
    evidence_paths: Mapping[str, Path],
) -> None:
    try:
        from PIL import Image, ImageDraw
    except Exception as error:
        raise DenseScanError("Pillow is required for the contact sheet") from error
    rows = [row for row in results if row["frame_id"] in evidence_paths]
    if not rows:
        raise DenseScanError("Contact-sheet evidence is unavailable")
    tile_width = 480
    tile_height = 350
    columns = min(3, len(rows))
    row_count = math.ceil(len(rows) / columns)
    sheet = Image.new(
        "RGB",
        (columns * tile_width, row_count * tile_height),
        color=(18, 24, 32),
    )
    draw = ImageDraw.Draw(sheet)
    for index, row in enumerate(rows):
        left = (index % columns) * tile_width
        top = (index // columns) * tile_height
        with Image.open(evidence_paths[row["frame_id"]]) as source:
            source.load()
            image = source.convert("RGB")
        image.thumbnail((tile_width - 20, tile_height - 70))
        image_left = left + (tile_width - image.width) // 2
        image_top = top + 10
        sheet.paste(image, (image_left, image_top))
        overlay = ImageDraw.Draw(sheet)
        for prediction in row["predictions"]:
            box = prediction["box"]
            x1 = image_left + round(float(box["x"]) * image.width)
            y1 = image_top + round(float(box["y"]) * image.height)
            x2 = x1 + round(float(box["width"]) * image.width)
            y2 = y1 + round(float(box["height"]) * image.height)
            overlay.rectangle((x1, y1, x2, y2), outline=(255, 90, 76), width=3)
        label = f"frame {row['frame_index']}  t={row['timestamp_seconds']:.2f}s"
        if row["predictions"]:
            first = row["predictions"][0]
            label += (
                f"  {first['issue_type']} model_score="
                f"{first['model_score']:.4f}"
            )
        else:
            label += "  no detection"
        draw.text((left + 10, top + tile_height - 45), label, fill=(238, 244, 250))
        draw.text(
            (left + 10, top + tile_height - 25),
            "model_score is not probability",
            fill=(255, 196, 92),
        )
    sheet.save(path, format="JPEG", quality=88, optimize=True)


def _markdown_report(envelope: Mapping[str, Any]) -> str:
    source = envelope["source"]
    model = envelope["model"]
    summary = envelope["summary"]
    sampling = envelope["sampling"]
    return f"""# TrafficVisson dense detector smoke

## 결론

{envelope["disclaimer_ko"]}

- 상태: `{envelope["status"]}`
- 입력 영상: `{source["file_name"]}`
- 표본 프레임: {summary["sampled_frame_count"]}개
- 검지 제안 프레임: {summary["proposal_frame_count"]}개
- 시간 중복 그룹: {summary["temporal_group_count"]}개
- 자동 라벨: 0건
- 사건 생성: 0건
- 모델 승격: 없음
- 실험 사이드카 기록: 0건

## 표본화

- 방식: `{sampling["mode"]}`
- 표본 간격: {sampling["interval_seconds"]:.6f}초
- 영상 분석 상한: {sampling["max_video_seconds"]:.3f}초
- 프레임 상한: {sampling["max_frames"]}개
- 임시 저장 상한: {sampling["max_temporary_bytes"]} bytes
- 최종 출력 상한: {sampling["max_output_bytes"]} bytes
- 타임스탬프 기준: `{sampling["timestamp_basis"]}`

## 출처 및 모델 provenance

- 영상 SHA-256: `{source["sha256"]}`
- 영상 원본 불변 검증: `{source["verified_unchanged"]}`
- 모델 pack: `{model["pack_id"]}` `{model["pack_version"]}`
- 모델 manifest SHA-256: `{model["manifest_sha256"]}`
- 엔진: `{model["engine"]}`
- 점수 의미: `model_score_not_probability`

## 생성물

- `predictions.json`: 프레임별 비확정 제안과 시간 중복 그룹
- `frame_scores.csv`: 원시 검지 행
- `evidence/`: 검토용 원본 추출 프레임
- `contact-sheet-001.jpg`: 제안 박스 또는 무검지 표본
- `checkpoint.json`: 동일 설정으로 재개하기 위한 체크포인트
- `manifest.json`: 출력 파일 SHA-256 목록

이 실행은 TrafficVisson 분석 JSON, 라벨, SQLite, 사건, 보고서 입력,
모델 승격, 실험 사이드카를 읽거나 수정하는 경로를 사용하지 않습니다.
"""


def _artifact_manifest(
    run_root: Path,
    *,
    generated_at: str,
) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    for current, directories, files in os.walk(run_root, followlinks=False):
        current_path = Path(current)
        if current_path.name == ".staging":
            directories[:] = []
            continue
        directories[:] = [
            name for name in directories if name != ".staging"
        ]
        for name in files:
            path = current_path / name
            if path.name == "manifest.json":
                continue
            relative = path.relative_to(run_root).as_posix()
            artifacts.append(
                {
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
    artifacts.sort(key=lambda row: row["path"])
    return {
        "schema_version": MANIFEST_SCHEMA,
        "generated_at": generated_at,
        "purpose": "unreviewed_dense_detector_proposals",
        "score_semantics": "model_score_not_probability",
        "automatic_labels_created": 0,
        "incidents_created": 0,
        "model_promoted": False,
        "experimental_sidecars_created": 0,
        "artifacts": artifacts,
    }


def _prepare_run_directory(
    *,
    program_data: Path,
    run_name: str,
    resume: bool,
) -> tuple[Path, Path]:
    if not RUN_NAME_PATTERN.fullmatch(run_name):
        raise DenseScanError("The run name is invalid")
    if not program_data.is_absolute():
        raise DenseScanError("PROGRAMDATA must be an absolute path")
    program_data.mkdir(parents=True, exist_ok=True)
    base = program_data / "TrafficVisson" / "ModelEvaluations"
    base.mkdir(parents=True, exist_ok=True)
    base = _safe_directory(base, "ModelEvaluations directory")
    run_root = base / run_name
    if resume:
        run_root = _safe_directory(run_root, "Dense-scan run directory")
    else:
        if run_root.exists() or run_root.is_symlink():
            raise DenseScanError("The dense-scan run directory already exists")
        run_root.mkdir(exist_ok=False)
        run_root = _safe_directory(run_root, "Dense-scan run directory")
    if run_root.parent != base:
        raise DenseScanError("The dense-scan run escaped ModelEvaluations")
    return base, run_root


def _validated_limits(
    *,
    max_frames: int,
    max_video_seconds: float,
    max_wall_seconds: float,
    max_temporary_bytes: int,
    max_output_bytes: int,
    batch_size: int,
    max_evidence_frames: int,
    temporal_gap_seconds: float,
    temporal_iou_threshold: float,
) -> None:
    if (
        isinstance(max_frames, bool)
        or not isinstance(max_frames, int)
        or not 1 <= max_frames <= MAX_FRAMES
    ):
        raise DenseScanError(f"max_frames must be between 1 and {MAX_FRAMES}")
    for value, minimum, maximum, label in (
        (max_video_seconds, 0.1, 24 * 60 * 60, "max_video_seconds"),
        (max_wall_seconds, 5.0, 3_600.0, "max_wall_seconds"),
        (temporal_gap_seconds, 0.01, 600.0, "temporal_gap_seconds"),
        (temporal_iou_threshold, 0.0, 1.0, "temporal_iou_threshold"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            raise DenseScanError(f"{label} is outside the supported range")
    for value, minimum, maximum, label in (
        (
            max_temporary_bytes,
            1 * 1024 * 1024,
            8 * 1024**3,
            "max_temporary_bytes",
        ),
        (
            max_output_bytes,
            1 * 1024 * 1024,
            8 * 1024**3,
            "max_output_bytes",
        ),
        (batch_size, 1, MAX_BATCH_SIZE, "batch_size"),
        (max_evidence_frames, 1, 256, "max_evidence_frames"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise DenseScanError(f"{label} is outside the supported range")


def _default_service_factory(
    manifest: Path,
    timeout_seconds: float,
) -> LocalVisionInferenceService:
    return LocalVisionInferenceService(
        model_pack_path=manifest,
        timeout_seconds=timeout_seconds,
    )


def _default_pack_loader(manifest: Path) -> Any:
    return load_model_pack(manifest)


def run_dense_scan(
    *,
    source_video: Path | str,
    model_manifest: Path | str,
    run_name: str,
    fps: Optional[float] = None,
    interval_seconds: Optional[float] = 1.0,
    max_frames: int = 300,
    max_video_seconds: float = 900.0,
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS,
    max_temporary_bytes: int = DEFAULT_TEMP_BYTES,
    max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
    batch_size: int = 64,
    max_evidence_frames: int = 64,
    temporal_gap_seconds: float = 2.5,
    temporal_iou_threshold: float = 0.1,
    resume: bool = False,
    program_data: Optional[Path | str] = None,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    service_factory: Callable[[Path, float], Any] = _default_service_factory,
    model_pack_loader: Callable[[Path], Any] = _default_pack_loader,
    tool_version_function: Callable[..., str] = _tool_version,
    probe_function: Callable[..., VideoProbe] = _probe_video,
    sample_function: Callable[..., list[SampledFrame]] = _sample_frames,
) -> dict[str, Any]:
    """Run one non-destructive dense detector evaluation."""
    _validated_limits(
        max_frames=max_frames,
        max_video_seconds=max_video_seconds,
        max_wall_seconds=max_wall_seconds,
        max_temporary_bytes=max_temporary_bytes,
        max_output_bytes=max_output_bytes,
        batch_size=batch_size,
        max_evidence_frames=max_evidence_frames,
        temporal_gap_seconds=temporal_gap_seconds,
        temporal_iou_threshold=temporal_iou_threshold,
    )
    sample_rate, rate_filter, sample_interval = _sampling_rate(
        fps=fps,
        interval_seconds=interval_seconds,
    )
    deadline = time.monotonic() + float(max_wall_seconds)
    source = _safe_regular_file(
        Path(source_video),
        maximum_bytes=MAX_SOURCE_BYTES,
        extensions=VIDEO_EXTENSIONS,
    )
    manifest = _safe_regular_file(
        Path(model_manifest),
        maximum_bytes=MAX_MANIFEST_BYTES,
        extensions={".json"},
    )
    selected_program_data = (
        Path(program_data)
        if program_data is not None
        else Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    )
    _, run_root = _prepare_run_directory(
        program_data=selected_program_data,
        run_name=run_name,
        resume=resume,
    )
    staging_root = run_root / ".staging"
    checkpoint_path = run_root / "checkpoint.json"
    generated_at = _utc_now()
    source_before = _fingerprint(
        source,
        maximum_bytes=MAX_SOURCE_BYTES,
        deadline=deadline,
    )
    manifest_before = _fingerprint(
        manifest,
        maximum_bytes=MAX_MANIFEST_BYTES,
        deadline=deadline,
    )
    pack = model_pack_loader(manifest)
    if (
        getattr(pack, "engine", None) != "onnx_yolo_v8"
        or getattr(pack, "pack_version", None) != "0.1.1"
        or getattr(pack, "manifest_sha256", None) != manifest_before.sha256
    ):
        raise DenseScanError("The pinned ONNX v0.1.1 model pack is invalid")
    raw_artifacts = getattr(pack, "artifacts", None)
    if not isinstance(raw_artifacts, Mapping) or not raw_artifacts:
        raise DenseScanError("The model-pack artifact inventory is invalid")
    model_artifacts: list[dict[str, Any]] = []
    model_artifact_fingerprints: dict[Path, FileFingerprint] = {}
    for relative, artifact in sorted(raw_artifacts.items()):
        path = getattr(artifact, "path", None)
        sha256 = getattr(artifact, "sha256", None)
        size_bytes = getattr(artifact, "size_bytes", None)
        if (
            not isinstance(relative, str)
            or not isinstance(path, Path)
            or not isinstance(sha256, str)
            or not SHA256_PATTERN.fullmatch(sha256)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes <= 0
        ):
            raise DenseScanError("A model-pack artifact inventory row is invalid")
        fingerprint = _fingerprint(path, deadline=deadline)
        if (
            fingerprint.size_bytes != size_bytes
            or fingerprint.sha256 != sha256
        ):
            raise DenseScanError("A model-pack artifact failed provenance checks")
        model_artifact_fingerprints[path] = fingerprint
        model_artifacts.append(
            {
                "path": relative,
                "size_bytes": size_bytes,
                "sha256": sha256,
            }
        )
    config = {
        "source_path": str(source),
        "source_size_bytes": source_before.size_bytes,
        "source_sha256": source_before.sha256,
        "model_manifest_path": str(manifest),
        "model_manifest_sha256": manifest_before.sha256,
        "model_artifacts": model_artifacts,
        "sampling_rate_fps": round(sample_rate, 12),
        "sample_interval_seconds": round(sample_interval, 12),
        "rate_filter": rate_filter,
        "max_frames": max_frames,
        "max_video_seconds": round(float(max_video_seconds), 6),
        "max_temporary_bytes": max_temporary_bytes,
        "max_output_bytes": max_output_bytes,
        "batch_size": batch_size,
        "max_evidence_frames": max_evidence_frames,
        "temporal_gap_seconds": round(float(temporal_gap_seconds), 6),
        "temporal_iou_threshold": round(float(temporal_iou_threshold), 6),
    }
    config_digest = _checkpoint_config_digest(config)
    results: list[dict[str, Any]] = []
    frames: list[SampledFrame]
    probe: VideoProbe
    model_status: Mapping[str, Any]
    ffmpeg_version = "fixture"
    ffprobe_version = "fixture"
    if resume:
        checkpoint = _load_checkpoint(checkpoint_path)
        if (
            checkpoint.get("config_sha256") != config_digest
            or checkpoint.get("config") != config
        ):
            raise DenseScanError("The resume settings do not match the checkpoint")
        if checkpoint.get("status") == "completed":
            predictions_path = run_root / "predictions.json"
            value = _strict_json_object(
                predictions_path.read_bytes(),
                "Completed dense-scan predictions",
            )
            _verify_fingerprint(
                source,
                source_before,
                maximum_bytes=MAX_SOURCE_BYTES,
                deadline=deadline,
            )
            _verify_fingerprint(
                manifest,
                manifest_before,
                maximum_bytes=MAX_MANIFEST_BYTES,
                deadline=deadline,
            )
            return value
        staging_root = _safe_directory(
            staging_root,
            "Dense-scan staging directory",
        )
        frames = _validate_resume_frames(
            run_root,
            checkpoint.get("frames"),
            max_frames=max_frames,
            max_temporary_bytes=max_temporary_bytes,
            deadline=deadline,
        )
        raw_results = checkpoint.get("results")
        if not isinstance(raw_results, list) or len(raw_results) > len(frames):
            raise DenseScanError("The resume checkpoint results are invalid")
        results = [dict(row) for row in raw_results if isinstance(row, Mapping)]
        if len(results) != len(raw_results):
            raise DenseScanError("A resume checkpoint result is invalid")
        try:
            probe = VideoProbe(**checkpoint["probe"])
        except (KeyError, TypeError, ValueError) as error:
            raise DenseScanError("The resume video probe is invalid") from error
        ffmpeg_version = str(checkpoint.get("ffmpeg_version", "unknown"))[:500]
        ffprobe_version = str(checkpoint.get("ffprobe_version", "unknown"))[:500]
    else:
        staging_root.mkdir(exist_ok=False)
        _safe_directory(staging_root, "Dense-scan staging directory")
        ffmpeg = _resolve_tool(ffmpeg_path, "ffmpeg")
        ffprobe = _resolve_tool(ffprobe_path, "ffprobe")
        ffmpeg_version = tool_version_function(
            ffmpeg,
            working_root=staging_root,
            deadline=deadline,
        )
        ffprobe_version = tool_version_function(
            ffprobe,
            working_root=staging_root,
            deadline=deadline,
        )
        probe = probe_function(
            source,
            ffprobe=ffprobe,
            working_root=staging_root,
            deadline=deadline,
        )
        scan_seconds = min(float(max_video_seconds), probe.duration_seconds)
        frames = sample_function(
            source,
            staging_root,
            ffmpeg=ffmpeg,
            rate_filter=rate_filter,
            sample_interval_seconds=sample_interval,
            max_frames=max_frames,
            max_video_seconds=scan_seconds,
            max_temporary_bytes=max_temporary_bytes,
            deadline=deadline,
        )
        _atomic_write_json(
            checkpoint_path,
            {
                "schema_version": CHECKPOINT_SCHEMA,
                "status": "sampled",
                "created_at": generated_at,
                "updated_at": _utc_now(),
                "config_sha256": config_digest,
                "config": config,
                "probe": asdict(probe),
                "ffmpeg_version": ffmpeg_version,
                "ffprobe_version": ffprobe_version,
                "frames": [asdict(frame) for frame in frames],
                "results": [],
                "completed_frame_count": 0,
                "side_effects": {
                    "automatic_labels_created": 0,
                    "incidents_created": 0,
                    "model_promoted": False,
                    "experimental_sidecars_created": 0,
                },
            },
        )

    service = service_factory(
        manifest,
        max(5.0, min(3_600.0, _remaining_seconds(deadline))),
    )
    model_status = service.status()
    if (
        not isinstance(model_status, Mapping)
        or model_status.get("enabled") is not True
        or model_status.get("mode") != "local_model"
        or model_status.get("engine") != "onnx_yolo_v8"
        or model_status.get("pack_version") != "0.1.1"
        or model_status.get("manifest_sha256") != manifest_before.sha256
        or not isinstance(model_status.get("pack_id"), str)
        or not model_status.get("pack_id")
    ):
        raise DenseScanError(
            "The pinned ONNX v0.1.1 model-pack contract is not enabled"
        )
    completed = len(results)
    if any(
        row.get("frame_id") != frames[index].frame_id
        for index, row in enumerate(results)
    ):
        raise DenseScanError("Resume results do not match sampled frames")
    while completed < len(frames):
        _remaining_seconds(deadline)
        selected = frames[completed : completed + batch_size]
        candidates = [
            {
                "candidate_id": frame.frame_id,
                "representative_image": {
                    "relative_path": Path(frame.relative_path).name,
                    "size_bytes": frame.size_bytes,
                    "sha256": frame.sha256,
                },
            }
            for frame in selected
        ]
        frame_root = run_root / ".staging" / "frames"
        inference_by_id, summary = service.infer_candidates(
            candidates,
            frame_root,
            cancel=lambda: time.monotonic() > deadline,
        )
        if (
            not isinstance(inference_by_id, Mapping)
            or set(inference_by_id) != {frame.frame_id for frame in selected}
            or not isinstance(summary, Mapping)
            or summary.get("engine") != "onnx_yolo_v8"
            or summary.get("pack_id") != model_status.get("pack_id")
            or summary.get("pack_version") != model_status.get("pack_version")
            or summary.get("manifest_sha256")
            != model_status.get("manifest_sha256")
        ):
            raise DenseScanError("Detector batch output identity is invalid")
        for frame in selected:
            results.append(
                _normalize_inference(
                    frame,
                    inference_by_id[frame.frame_id],
                    expected_model=model_status,
                )
            )
        completed = len(results)
        _atomic_write_json(
            checkpoint_path,
            {
                "schema_version": CHECKPOINT_SCHEMA,
                "status": (
                    "inference_complete"
                    if completed == len(frames)
                    else "inferring"
                ),
                "created_at": generated_at,
                "updated_at": _utc_now(),
                "config_sha256": config_digest,
                "config": config,
                "probe": asdict(probe),
                "ffmpeg_version": ffmpeg_version,
                "ffprobe_version": ffprobe_version,
                "frames": [asdict(frame) for frame in frames],
                "results": results,
                "completed_frame_count": completed,
                "side_effects": {
                    "automatic_labels_created": 0,
                    "incidents_created": 0,
                    "model_promoted": False,
                    "experimental_sidecars_created": 0,
                },
            },
        )

    groups = _assign_temporal_groups(
        results,
        source_sha256=source_before.sha256,
        max_gap_seconds=float(temporal_gap_seconds),
        minimum_iou=float(temporal_iou_threshold),
    )
    evidence, evidence_paths = _copy_evidence(
        run_root,
        results,
        max_evidence_frames=max_evidence_frames,
        max_output_bytes=max_output_bytes,
    )
    _write_contact_sheet(
        run_root / "contact-sheet-001.jpg",
        results,
        evidence_paths,
    )
    status_counts = Counter(row["status"] for row in results)
    proposal_frame_count = sum(bool(row["predictions"]) for row in results)
    prediction_count = sum(len(row["predictions"]) for row in results)
    _verify_fingerprint(
        source,
        source_before,
        maximum_bytes=MAX_SOURCE_BYTES,
        deadline=deadline,
    )
    _verify_fingerprint(
        manifest,
        manifest_before,
        maximum_bytes=MAX_MANIFEST_BYTES,
        deadline=deadline,
    )
    for path, fingerprint in model_artifact_fingerprints.items():
        _verify_fingerprint(
            path,
            fingerprint,
            deadline=deadline,
        )
    completed_at = _utc_now()
    envelope = {
        "schema_version": SCAN_SCHEMA,
        "status": "completed",
        "generated_at": generated_at,
        "completed_at": completed_at,
        "purpose": "unreviewed_dense_detector_proposals",
        "disclaimer_ko": DISCLAIMER_KO,
        "score_semantics": {
            "calibration": "model_score",
            "label": "model_score_not_probability",
            "is_probability": False,
            "is_accuracy": False,
            "is_label": False,
            "is_maintenance_decision": False,
        },
        "source": {
            "path": str(source),
            "file_name": source.name,
            "size_bytes": source_before.size_bytes,
            "modified_time_ns": source_before.modified_time_ns,
            "sha256": source_before.sha256,
            "verified_unchanged": True,
            "duration_seconds": probe.duration_seconds,
            "width": probe.width,
            "height": probe.height,
            "video_codec": probe.video_codec,
            "format_name": probe.format_name,
        },
        "model": {
            key: model_status.get(key)
            for key in (
                "pack_id",
                "pack_version",
                "engine",
                "capabilities",
                "manifest_sha256",
                "runtime_spdx",
                "weights_spdx",
                "redistribution_allowed",
            )
        }
        | {
            "artifacts": model_artifacts,
            "artifacts_verified_unchanged": True,
        },
        "sampling": {
            "mode": "fps" if fps is not None else "interval_seconds",
            "rate_fps": round(sample_rate, 12),
            "interval_seconds": round(sample_interval, 12),
            "max_frames": max_frames,
            "max_video_seconds": round(float(max_video_seconds), 6),
            "max_wall_seconds": round(float(max_wall_seconds), 3),
            "max_temporary_bytes": max_temporary_bytes,
            "max_output_bytes": max_output_bytes,
            "batch_size": batch_size,
            "max_evidence_frames": max_evidence_frames,
            "timestamp_basis": "zero_based_sampling_interval_approximation",
            "ffmpeg_version": ffmpeg_version,
            "ffprobe_version": ffprobe_version,
        },
        "temporal_grouping": {
            "maximum_gap_seconds": round(float(temporal_gap_seconds), 6),
            "minimum_normalized_box_iou": round(
                float(temporal_iou_threshold), 6
            ),
            "algorithm": "same_issue_recent_box_iou_v1",
        },
        "summary": {
            "sampled_frame_count": len(results),
            "proposal_frame_count": proposal_frame_count,
            "prediction_count": prediction_count,
            "temporal_group_count": len(groups),
            "evidence_frame_count": len(evidence),
            "status_counts": dict(sorted(status_counts.items())),
        },
        "side_effects": {
            "analysis_records_read": 0,
            "analysis_records_modified": 0,
            "label_records_read": 0,
            "label_records_modified": 0,
            "sqlite_databases_read": 0,
            "sqlite_databases_modified": 0,
            "automatic_labels_created": 0,
            "incidents_created": 0,
            "model_promoted": False,
            "experimental_sidecars_created": 0,
        },
        "results": results,
        "temporal_groups": groups,
        "evidence": evidence,
    }
    _atomic_write_json(run_root / "predictions.json", envelope)
    _write_scores_csv(run_root / "frame_scores.csv", results)
    (run_root / "README.md").write_text(
        _markdown_report(envelope),
        encoding="utf-8",
    )
    _directory_size(run_root, maximum=max_output_bytes + max_temporary_bytes)
    _atomic_write_json(
        checkpoint_path,
        {
            "schema_version": CHECKPOINT_SCHEMA,
            "status": "completed",
            "created_at": generated_at,
            "updated_at": completed_at,
            "config_sha256": config_digest,
            "config": config,
            "probe": asdict(probe),
            "ffmpeg_version": ffmpeg_version,
            "ffprobe_version": ffprobe_version,
            "frames": [asdict(frame) for frame in frames],
            "results": results,
            "completed_frame_count": len(results),
            "side_effects": envelope["side_effects"],
        },
    )
    evidence_by_frame = {
        row["frame_id"]: row["relative_path"] for row in evidence
    }
    for result in results:
        result["evidence_ref"] = evidence_by_frame.get(result["frame_id"])
        result["sampled_frame_ref"] = None
    for group in groups:
        representative = group.get("representative")
        if isinstance(representative, dict):
            representative["evidence_ref"] = evidence_by_frame.get(
                representative["frame_id"]
            )
            representative["sampled_frame_ref"] = None
    # The completed JSON was written before the presentation refs were
    # normalized. Rewrite it atomically with durable evidence references.
    envelope["results"] = results
    envelope["temporal_groups"] = groups
    _atomic_write_json(run_root / "predictions.json", envelope)
    staging_resolved = _safe_directory(
        run_root / ".staging",
        "Dense-scan staging directory",
    )
    if staging_resolved.parent != run_root:
        raise DenseScanError("The staging cleanup target escaped the run directory")
    shutil.rmtree(staging_resolved)
    _published_size(run_root, maximum=max_output_bytes)
    _atomic_write_json(
        run_root / "manifest.json",
        _artifact_manifest(run_root, generated_at=completed_at),
    )
    _published_size(run_root, maximum=max_output_bytes)
    return envelope


def _default_model_manifest() -> Path:
    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return (
        program_data
        / "TrafficVisson"
        / "ModelPacks"
        / "trafficvisson-peterhdd-yolov8s-pothole-research-0.1.1"
        / "trafficvisson-model-pack.json"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sample a video with ffmpeg and export review-only ONNX detector "
            "proposals under ProgramData/TrafficVisson/ModelEvaluations."
        )
    )
    parser.add_argument("--source-video", required=True)
    parser.add_argument(
        "--model-manifest",
        default=str(_default_model_manifest()),
    )
    parser.add_argument("--run-name", required=True)
    sampling = parser.add_mutually_exclusive_group()
    sampling.add_argument("--fps", type=float)
    sampling.add_argument("--interval-seconds", type=float)
    parser.add_argument("--max-frames", type=int, default=300)
    parser.add_argument("--max-video-seconds", type=float, default=900.0)
    parser.add_argument(
        "--max-wall-seconds",
        type=float,
        default=DEFAULT_MAX_WALL_SECONDS,
    )
    parser.add_argument(
        "--max-temporary-bytes",
        type=int,
        default=DEFAULT_TEMP_BYTES,
    )
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=DEFAULT_OUTPUT_BYTES,
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-evidence-frames", type=int, default=64)
    parser.add_argument("--temporal-gap-seconds", type=float, default=2.5)
    parser.add_argument("--temporal-iou-threshold", type=float, default=0.1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    arguments = parser.parse_args(argv)
    interval = arguments.interval_seconds
    if arguments.fps is None and interval is None:
        interval = 1.0
    try:
        envelope = run_dense_scan(
            source_video=Path(arguments.source_video),
            model_manifest=Path(arguments.model_manifest),
            run_name=arguments.run_name,
            fps=arguments.fps,
            interval_seconds=interval,
            max_frames=arguments.max_frames,
            max_video_seconds=arguments.max_video_seconds,
            max_wall_seconds=arguments.max_wall_seconds,
            max_temporary_bytes=arguments.max_temporary_bytes,
            max_output_bytes=arguments.max_output_bytes,
            batch_size=arguments.batch_size,
            max_evidence_frames=arguments.max_evidence_frames,
            temporal_gap_seconds=arguments.temporal_gap_seconds,
            temporal_iou_threshold=arguments.temporal_iou_threshold,
            resume=arguments.resume,
            ffmpeg_path=arguments.ffmpeg,
            ffprobe_path=arguments.ffprobe,
        )
    except (DenseScanError, VisionInferenceError, OSError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "automatic_labels_created": 0,
                    "incidents_created": 0,
                    "model_promoted": False,
                    "experimental_sidecars_created": 0,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "status": envelope["status"],
                "sampled_frame_count": envelope["summary"][
                    "sampled_frame_count"
                ],
                "proposal_frame_count": envelope["summary"][
                    "proposal_frame_count"
                ],
                "temporal_group_count": envelope["summary"][
                    "temporal_group_count"
                ],
                "score_semantics": envelope["score_semantics"]["label"],
                "automatic_labels_created": 0,
                "incidents_created": 0,
                "model_promoted": False,
                "experimental_sidecars_created": 0,
                "output_directory": str(
                    (
                        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
                        / "TrafficVisson"
                        / "ModelEvaluations"
                        / arguments.run_name
                    ).resolve()
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
