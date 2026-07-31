"""Bounded 5--10 FPS extraction for candidate-only video windows.

This module is deliberately downstream of the immutable 2 FPS dense index and
upstream of detector/OCR/SAM2 adapters.  It does not run a model and it does
not create labels, annotations, incidents, training examples, or maintenance
decisions.

The caller supplies already-extracted analysis candidates.  Their temporal
ranges are padded and merged, and only those merged windows are decoded from
the protected source video.  Every emitted JPEG is hashed and linked back to
the source candidate(s) in an immutable, content-addressed manifest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple, Union
from uuid import uuid4

from .candidate_extraction import _media_tool_path
from .dense_frame_index import (
    DenseFrameIndexCancelled,
    DenseFrameIndexError,
    DenseFrameIndexOutputLimit,
    DenseFrameIndexSourceChanged,
    DenseFrameIndexTimeout,
    MediaCommand,
    MediaCommandOutput,
    ReviewOnlyPolicy,
    _cancel_requested,
    _directory_size,
    _is_reparse_point,
    _jpeg_dimensions,
    _parse_tool_version,
    _remaining_timeout,
    _resolve_executable,
    _run_bounded_command,
    _sha256_regular_file,
    _source_identity,
    _validated_project_output,
    _validated_source,
    _verify_source_sha256,
)


CANDIDATE_WINDOW_RESCAN_SCHEMA = (
    "trafficvisson.candidate-window-rescan.v1"
)
_DENSE_INDEX_SCHEMA = "trafficvisson.dense-frame-index.v1"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_WINDOW_ID = re.compile(r"^[a-f0-9]{20}$")
_EVIDENCE_ID = re.compile(r"^[a-f0-9]{20}$")
_RUN_DIRECTORY = re.compile(
    r"^candidate-window-rescan-[a-f0-9]{24}$"
)
_TEMP_RUN_PREFIX = ".cwr-"
_FRAME_NAME = re.compile(r"^frame-(?P<index>[0-9]{6})\.jpg$")
_MAX_DENSE_INDEX_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 4 * 1024 * 1024


class CandidateWindowRescanError(RuntimeError):
    """Base class for candidate-window extraction failures."""


class CandidateWindowRescanCancelled(CandidateWindowRescanError):
    """Raised when the caller cancels extraction."""


class CandidateWindowRescanTimeout(CandidateWindowRescanError):
    """Raised when extraction exceeds a configured deadline."""


class CandidateWindowRescanOutputLimit(CandidateWindowRescanError):
    """Raised when a count, time, disk, or artifact bound is exceeded."""


class CandidateWindowRescanSourceChanged(CandidateWindowRescanError):
    """Raised when the registered source no longer matches its digest."""


class CandidateWindowRescanManifestInvalid(CandidateWindowRescanError):
    """Raised when an existing dense/rescan manifest fails closed."""


@dataclass(frozen=True)
class CandidateWindowRescanLimits:
    sampling_fps: int = 8
    padding_before_seconds: float = 1.0
    padding_after_seconds: float = 1.0
    fallback_half_width_seconds: float = 0.75
    merge_gap_seconds: float = 0.25
    minimum_window_seconds: float = 0.5
    max_candidates: int = 2_000
    max_windows: int = 512
    max_total_window_seconds: float = 60 * 60.0
    max_frames: int = 30_000
    max_downstream_frames: int = 512
    max_source_bytes: int = 256 * 1024**3
    max_frame_width: int = 1_600
    max_frame_pixels: int = 24_000_000
    max_frame_bytes: int = 24 * 1024 * 1024
    max_total_output_bytes: int = 8 * 1024**3
    min_free_disk_bytes: int = 512 * 1024 * 1024
    max_manifest_bytes: int = 64 * 1024 * 1024
    max_process_output_bytes: int = 2 * 1024 * 1024
    probe_timeout_seconds: float = 30.0
    extraction_timeout_seconds: float = 4 * 60 * 60.0
    max_total_seconds: float = 6 * 60 * 60.0

    def __post_init__(self) -> None:
        integer_ranges = (
            ("sampling_fps", self.sampling_fps, 5, 10),
            ("max_candidates", self.max_candidates, 1, 10_000),
            ("max_windows", self.max_windows, 1, 2_000),
            ("max_frames", self.max_frames, 1, 100_000),
            (
                "max_downstream_frames",
                self.max_downstream_frames,
                1,
                512,
            ),
            (
                "max_source_bytes",
                self.max_source_bytes,
                1,
                512 * 1024**3,
            ),
            ("max_frame_width", self.max_frame_width, 160, 8_192),
            (
                "max_frame_pixels",
                self.max_frame_pixels,
                10_000,
                67_108_864,
            ),
            (
                "max_frame_bytes",
                self.max_frame_bytes,
                1_024,
                256 * 1024 * 1024,
            ),
            (
                "max_total_output_bytes",
                self.max_total_output_bytes,
                4_096,
                64 * 1024**3,
            ),
            (
                "min_free_disk_bytes",
                self.min_free_disk_bytes,
                0,
                8 * 1024**4,
            ),
            (
                "max_manifest_bytes",
                self.max_manifest_bytes,
                1_024,
                512 * 1024 * 1024,
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
            (
                "padding_before_seconds",
                self.padding_before_seconds,
                0.0,
                30.0,
            ),
            (
                "padding_after_seconds",
                self.padding_after_seconds,
                0.0,
                30.0,
            ),
            (
                "fallback_half_width_seconds",
                self.fallback_half_width_seconds,
                0.05,
                30.0,
            ),
            (
                "merge_gap_seconds",
                self.merge_gap_seconds,
                0.0,
                10.0,
            ),
            (
                "minimum_window_seconds",
                self.minimum_window_seconds,
                0.05,
                60.0,
            ),
            (
                "max_total_window_seconds",
                self.max_total_window_seconds,
                0.1,
                24 * 60 * 60.0,
            ),
            (
                "probe_timeout_seconds",
                self.probe_timeout_seconds,
                0.1,
                300.0,
            ),
            (
                "extraction_timeout_seconds",
                self.extraction_timeout_seconds,
                1.0,
                24 * 60 * 60.0,
            ),
            (
                "max_total_seconds",
                self.max_total_seconds,
                1.0,
                48 * 60 * 60.0,
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
        if self.max_manifest_bytes >= self.max_total_output_bytes:
            raise ValueError(
                "max_total_output_bytes must leave room for frame artifacts"
            )


@dataclass(frozen=True)
class CandidateWindow:
    window_id: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    source_candidate_ids: Tuple[str, ...]
    planned_frame_limit: int


@dataclass(frozen=True)
class CandidateWindowFrame:
    evidence_id: str
    window_id: str
    frame_index: int
    timestamp_seconds: float
    timestamp_numerator: int
    timestamp_denominator: int
    source_candidate_ids: Tuple[str, ...]
    primary_candidate_id: str
    relative_path: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    media_type: str = "image/jpeg"
    evidence_status: str = "unreviewed_candidate"


@dataclass(frozen=True)
class CandidateWindowRescanResult:
    output_directory: str
    manifest_relative_path: str
    manifest_sha256: str
    input_revision: str
    windows: Tuple[CandidateWindow, ...]
    frames: Tuple[CandidateWindowFrame, ...]
    downstream_evidence_ids: Tuple[str, ...]
    cached: bool


@dataclass(frozen=True)
class CandidateWindowRescanPlan:
    """Path-free immutable storage plan computed before FFmpeg writes."""

    schema_version: str
    dense_index_sha256: str
    indexed_duration_seconds: float
    input_revision: str
    windows: Tuple[CandidateWindow, ...]
    planned_frame_count: int
    planned_output_bytes_upper_bound: int
    hard_ceiling_bytes: int

    def capacity_revision(self) -> dict[str, Any]:
        payload = {
            "schema_version": (
                "trafficvisson.candidate-rescan-capacity-revision.v1"
            ),
            "rescan_schema_version": self.schema_version,
            "dense_index_sha256": self.dense_index_sha256,
            "input_revision": self.input_revision,
            "window_count": len(self.windows),
            "planned_frame_count": self.planned_frame_count,
            "planned_output_bytes_upper_bound": (
                self.planned_output_bytes_upper_bound
            ),
            "hard_ceiling_bytes": self.hard_ceiling_bytes,
        }
        return {
            **payload,
            "revision_sha256": hashlib.sha256(
                _canonical_json(payload)
            ).hexdigest(),
        }


@dataclass(frozen=True)
class ResolvedCandidateWindowFrame:
    path: Path
    media_type: str
    size_bytes: int
    sha256: str


CancelCallback = Callable[[], bool]
CommandRunner = Callable[
    [MediaCommand, Optional[CancelCallback]], MediaCommandOutput
]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _strict_json(raw: bytes, label: str) -> dict[str, Any]:
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CandidateWindowRescanManifestInvalid(
                    f"{label} contains duplicate JSON keys"
                )
            result[key] = value
        return result

    def reject_constant(_):
        raise CandidateWindowRescanManifestInvalid(
            f"{label} contains a non-finite number"
        )

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except CandidateWindowRescanManifestInvalid:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateWindowRescanManifestInvalid(
            f"{label} is not valid UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise CandidateWindowRescanManifestInvalid(
            f"{label} must be a JSON object"
        )
    return value


def _read_bounded_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    label: str,
) -> tuple[bytes, str]:
    try:
        before = path.stat()
    except OSError as error:
        raise CandidateWindowRescanManifestInvalid(
            f"{label} is unavailable"
        ) from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= maximum_bytes
    ):
        raise CandidateWindowRescanManifestInvalid(
            f"{label} is not a bounded regular file"
        )
    digest = hashlib.sha256()
    chunks = []
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(_READ_CHUNK_BYTES):
                total += len(block)
                if total > maximum_bytes:
                    raise CandidateWindowRescanManifestInvalid(
                        f"{label} exceeds its size bound"
                    )
                digest.update(block)
                chunks.append(block)
        after = path.stat()
    except OSError as error:
        raise CandidateWindowRescanManifestInvalid(
            f"{label} could not be read"
        ) from error
    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise CandidateWindowRescanManifestInvalid(
            f"{label} changed while it was read"
        )
    return b"".join(chunks), digest.hexdigest()


def _finite_seconds(value: Any) -> Optional[float]:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def _load_dense_contract(
    dense_index_path: Union[str, Path],
    *,
    source_sha256: str,
) -> tuple[dict[str, Any], str, float]:
    path = Path(dense_index_path)
    if not path.is_absolute() or path.name != "frame-index.json":
        raise CandidateWindowRescanManifestInvalid(
            "dense frame index location is invalid"
        )
    raw, digest = _read_bounded_regular_file(
        path,
        maximum_bytes=_MAX_DENSE_INDEX_BYTES,
        label="dense frame index",
    )
    payload = _strict_json(raw, "dense frame index")
    provenance = payload.get("provenance")
    frames = payload.get("frames")
    frame_count = payload.get("frame_count")
    if (
        payload.get("schema_version") != _DENSE_INDEX_SCHEMA
        or not isinstance(provenance, Mapping)
        or provenance.get("schema_version") != _DENSE_INDEX_SCHEMA
        or provenance.get("source_sha256") != source_sha256
        or provenance.get("sampling_fps") != 2
        or not isinstance(frames, list)
        or isinstance(frame_count, bool)
        or not isinstance(frame_count, int)
        or frame_count != len(frames)
        or frame_count <= 0
    ):
        raise CandidateWindowRescanManifestInvalid(
            "dense frame index provenance is invalid"
        )
    duration = _finite_seconds(
        provenance.get("indexed_duration_seconds")
    )
    if duration is None or duration <= 0:
        raise CandidateWindowRescanManifestInvalid(
            "dense frame index duration is invalid"
        )
    return payload, digest, duration


def _candidate_ranges(
    candidates: Sequence[Mapping[str, Any]],
    *,
    indexed_duration_seconds: float,
    limits: CandidateWindowRescanLimits,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    if (
        not isinstance(candidates, Sequence)
        or isinstance(candidates, (str, bytes, bytearray))
        or not 1 <= len(candidates) <= limits.max_candidates
    ):
        raise CandidateWindowRescanError(
            "candidate scope is empty or exceeds its configured bound"
        )
    normalized = []
    representatives: dict[str, float] = {}
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise CandidateWindowRescanError(
                "analysis candidate is invalid"
            )
        candidate_id = candidate.get("candidate_id")
        representative = _finite_seconds(
            candidate.get("representative_timestamp_seconds")
        )
        if (
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID.fullmatch(candidate_id) is None
            or candidate_id in seen
            or representative is None
            or not 0 <= representative <= indexed_duration_seconds
        ):
            raise CandidateWindowRescanError(
                "analysis candidate provenance is invalid"
            )
        seen.add(candidate_id)
        raw_start = _finite_seconds(candidate.get("start_seconds"))
        raw_end = _finite_seconds(candidate.get("end_seconds"))
        if (
            raw_start is None
            or raw_end is None
            or raw_start < 0
            or raw_end < raw_start
            or representative < raw_start - 0.000001
            or representative > raw_end + 0.000001
        ):
            raw_start = max(
                0.0,
                representative - limits.fallback_half_width_seconds,
            )
            raw_end = min(
                indexed_duration_seconds,
                representative + limits.fallback_half_width_seconds,
            )
        start = max(
            0.0, raw_start - limits.padding_before_seconds
        )
        end = min(
            indexed_duration_seconds,
            raw_end + limits.padding_after_seconds,
        )
        if end - start < limits.minimum_window_seconds:
            deficit = limits.minimum_window_seconds - (end - start)
            start = max(0.0, start - deficit / 2)
            end = min(indexed_duration_seconds, end + deficit / 2)
        if end <= start:
            raise CandidateWindowRescanError(
                "analysis candidate window is empty"
            )
        representatives[candidate_id] = representative
        normalized.append(
            {
                "candidate_id": candidate_id,
                "start_seconds": round(start, 6),
                "end_seconds": round(end, 6),
            }
        )
    normalized.sort(
        key=lambda row: (
            row["start_seconds"],
            row["end_seconds"],
            row["candidate_id"],
        )
    )
    return normalized, representatives


def _plan_windows(
    candidates: Sequence[Mapping[str, Any]],
    *,
    source_sha256: str,
    dense_index_sha256: str,
    indexed_duration_seconds: float,
    limits: CandidateWindowRescanLimits,
) -> tuple[Tuple[CandidateWindow, ...], dict[str, float]]:
    normalized, representatives = _candidate_ranges(
        candidates,
        indexed_duration_seconds=indexed_duration_seconds,
        limits=limits,
    )
    merged: list[dict[str, Any]] = []
    for candidate in normalized:
        if (
            merged
            and candidate["start_seconds"]
            <= merged[-1]["end_seconds"]
            + limits.merge_gap_seconds
        ):
            merged[-1]["end_seconds"] = max(
                merged[-1]["end_seconds"],
                candidate["end_seconds"],
            )
            merged[-1]["source_candidate_ids"].append(
                candidate["candidate_id"]
            )
        else:
            merged.append(
                {
                    "start_seconds": candidate["start_seconds"],
                    "end_seconds": candidate["end_seconds"],
                    "source_candidate_ids": [
                        candidate["candidate_id"]
                    ],
                }
            )
    if len(merged) > limits.max_windows:
        raise CandidateWindowRescanOutputLimit(
            "candidate window count exceeds its configured bound"
        )
    windows = []
    total_duration = 0.0
    total_planned_frames = 0
    for row in merged:
        start = round(float(row["start_seconds"]), 6)
        end = round(float(row["end_seconds"]), 6)
        duration = round(end - start, 6)
        source_candidate_ids = tuple(
            sorted(set(row["source_candidate_ids"]))
        )
        planned_frames = max(
            1, math.ceil(duration * limits.sampling_fps) + 1
        )
        total_duration += duration
        total_planned_frames += planned_frames
        material = {
            "schema_version": CANDIDATE_WINDOW_RESCAN_SCHEMA,
            "source_sha256": source_sha256,
            "dense_index_sha256": dense_index_sha256,
            "sampling_fps": limits.sampling_fps,
            "start_seconds": start,
            "end_seconds": end,
            "source_candidate_ids": source_candidate_ids,
        }
        window_id = hashlib.sha256(
            _canonical_json(material)
        ).hexdigest()[:20]
        windows.append(
            CandidateWindow(
                window_id=window_id,
                start_seconds=start,
                end_seconds=end,
                duration_seconds=duration,
                source_candidate_ids=source_candidate_ids,
                planned_frame_limit=planned_frames,
            )
        )
    if total_duration > limits.max_total_window_seconds:
        raise CandidateWindowRescanOutputLimit(
            "candidate windows exceed the total media-time bound"
        )
    if total_planned_frames > limits.max_frames:
        raise CandidateWindowRescanOutputLimit(
            "candidate windows exceed the total frame bound"
        )
    return tuple(windows), representatives


def _input_revision(
    *,
    source_sha256: str,
    dense_index_sha256: str,
    limits: CandidateWindowRescanLimits,
    windows: Sequence[CandidateWindow],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "schema_version": CANDIDATE_WINDOW_RESCAN_SCHEMA,
                "source_sha256": source_sha256,
                "dense_index_sha256": dense_index_sha256,
                "sampling_fps": limits.sampling_fps,
                "window_policy": {
                    "padding_before_seconds": (
                        limits.padding_before_seconds
                    ),
                    "padding_after_seconds": (
                        limits.padding_after_seconds
                    ),
                    "fallback_half_width_seconds": (
                        limits.fallback_half_width_seconds
                    ),
                    "merge_gap_seconds": limits.merge_gap_seconds,
                    "minimum_window_seconds": (
                        limits.minimum_window_seconds
                    ),
                },
                "windows": [asdict(window) for window in windows],
            }
        )
    ).hexdigest()


def _planned_output_bytes_upper_bound(
    windows: Sequence[CandidateWindow],
    limits: CandidateWindowRescanLimits,
) -> int:
    """Return the fail-closed storage bound for this immutable plan.

    The global output cap remains the hard runtime ceiling. Preflight only
    reserves the frames this plan can emit plus its bounded manifest, capped
    by that global ceiling.
    """

    planned_frame_count = sum(
        window.planned_frame_limit for window in windows
    )
    planned_output_bound = (
        planned_frame_count * limits.max_frame_bytes
        + limits.max_manifest_bytes
    )
    return min(
        limits.max_total_output_bytes,
        planned_output_bound,
    )


def _evidence_id(
    *,
    source_sha256: str,
    dense_index_sha256: str,
    fps: int,
    window_id: str,
    frame_index: int,
) -> str:
    return hashlib.sha256(
        (
            f"{CANDIDATE_WINDOW_RESCAN_SCHEMA}\0{source_sha256}\0"
            f"{dense_index_sha256}\0fps={fps}\0{window_id}\0"
            f"frame={frame_index}"
        ).encode("ascii")
    ).hexdigest()[:20]


def _primary_candidate(
    source_candidate_ids: Sequence[str],
    representatives: Mapping[str, float],
    timestamp_seconds: float,
) -> str:
    return min(
        source_candidate_ids,
        key=lambda candidate_id: (
            abs(representatives[candidate_id] - timestamp_seconds),
            candidate_id,
        ),
    )


def _select_downstream_frames(
    frames: Sequence[CandidateWindowFrame],
    *,
    maximum: int,
) -> Tuple[str, ...]:
    """Select a deterministic, candidate-balanced DINO/OCR evidence subset."""

    if len(frames) <= maximum:
        return tuple(frame.evidence_id for frame in frames)
    by_candidate: dict[str, list[CandidateWindowFrame]] = {}
    for frame in frames:
        by_candidate.setdefault(
            frame.primary_candidate_id, []
        ).append(frame)
    selected: list[CandidateWindowFrame] = []
    selected_ids: set[str] = set()
    for candidate_id in sorted(by_candidate):
        rows = by_candidate[candidate_id]
        representative_index = len(rows) // 2
        row = rows[representative_index]
        if row.evidence_id not in selected_ids:
            selected.append(row)
            selected_ids.add(row.evidence_id)
        if len(selected) == maximum:
            break
    if len(selected) < maximum:
        remaining = [
            frame
            for frame in frames
            if frame.evidence_id not in selected_ids
        ]
        slots = maximum - len(selected)
        if len(remaining) <= slots:
            selected.extend(remaining)
        else:
            for slot in range(slots):
                index = min(
                    len(remaining) - 1,
                    math.floor(slot * len(remaining) / slots),
                )
                selected.append(remaining[index])
    selected.sort(
        key=lambda frame: (
            frame.timestamp_seconds,
            frame.window_id,
            frame.frame_index,
        )
    )
    return tuple(frame.evidence_id for frame in selected)


def _safe_remove_run(path: Optional[Path], temporary_root: Path) -> None:
    if path is None or path.parent != temporary_root or not path.exists():
        return
    if not path.name.startswith(_TEMP_RUN_PREFIX):
        return
    try:
        if path.is_symlink() or _is_reparse_point(path):
            os.rmdir(path)
        else:
            shutil.rmtree(path)
    except OSError:
        pass


def _write_manifest(
    run_directory: Path,
    payload: Mapping[str, Any],
    *,
    limits: CandidateWindowRescanLimits,
    frame_bytes: int,
) -> tuple[str, int]:
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
        len(encoded) > limits.max_manifest_bytes
        or frame_bytes + len(encoded)
        > limits.max_total_output_bytes
    ):
        raise CandidateWindowRescanOutputLimit(
            "candidate rescan manifest exceeds its storage bound"
        )
    target = run_directory / "candidate-window-rescan.json"
    temporary = run_directory / f".manifest-{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as error:
        raise CandidateWindowRescanError(
            "candidate rescan manifest could not be persisted"
        ) from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _convert_dense_error(error: Exception) -> CandidateWindowRescanError:
    if isinstance(error, DenseFrameIndexCancelled):
        return CandidateWindowRescanCancelled(str(error))
    if isinstance(error, DenseFrameIndexTimeout):
        return CandidateWindowRescanTimeout(str(error))
    if isinstance(error, DenseFrameIndexOutputLimit):
        return CandidateWindowRescanOutputLimit(str(error))
    if isinstance(error, DenseFrameIndexSourceChanged):
        return CandidateWindowRescanSourceChanged(str(error))
    return CandidateWindowRescanError(str(error))


class CandidateWindowRescanEngine:
    """Create or reuse one immutable candidate-window evidence set."""

    def __init__(
        self,
        *,
        ffmpeg_path: Optional[Union[str, Path]] = None,
        limits: Optional[CandidateWindowRescanLimits] = None,
        command_runner: Optional[CommandRunner] = None,
    ) -> None:
        self._ffmpeg = _resolve_executable(ffmpeg_path, "ffmpeg")
        self._limits = limits or CandidateWindowRescanLimits()
        self._runner = command_runner or _run_bounded_command

    def _run(
        self,
        command: MediaCommand,
        cancel: Optional[CancelCallback],
    ) -> MediaCommandOutput:
        try:
            result = self._runner(command, cancel)
        except DenseFrameIndexError as error:
            raise _convert_dense_error(error) from error
        if (
            not isinstance(result, MediaCommandOutput)
            or isinstance(result.returncode, bool)
            or not isinstance(result.returncode, int)
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
            or len(result.stdout) + len(result.stderr)
            > command.capture_limit_bytes
        ):
            raise CandidateWindowRescanError(
                "media command runner returned invalid data"
            )
        return result

    def _plan_with_representatives(
        self,
        *,
        source_sha256: str,
        dense_index_path: Union[str, Path],
        candidates: Sequence[Mapping[str, Any]],
    ) -> tuple[CandidateWindowRescanPlan, dict[str, float]]:
        if (
            not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
        ):
            raise ValueError(
                "source_sha256 must be a lowercase registered SHA-256"
            )
        _, dense_index_sha256, indexed_duration = (
            _load_dense_contract(
                dense_index_path,
                source_sha256=source_sha256,
            )
        )
        windows, representatives = _plan_windows(
            candidates,
            source_sha256=source_sha256,
            dense_index_sha256=dense_index_sha256,
            indexed_duration_seconds=indexed_duration,
            limits=self._limits,
        )
        input_revision = _input_revision(
            source_sha256=source_sha256,
            dense_index_sha256=dense_index_sha256,
            limits=self._limits,
            windows=windows,
        )
        plan = CandidateWindowRescanPlan(
            schema_version=CANDIDATE_WINDOW_RESCAN_SCHEMA,
            dense_index_sha256=dense_index_sha256,
            indexed_duration_seconds=indexed_duration,
            input_revision=input_revision,
            windows=windows,
            planned_frame_count=sum(
                window.planned_frame_limit for window in windows
            ),
            planned_output_bytes_upper_bound=(
                _planned_output_bytes_upper_bound(
                    windows,
                    self._limits,
                )
            ),
            hard_ceiling_bytes=self._limits.max_total_output_bytes,
        )
        return plan, representatives

    def plan(
        self,
        *,
        source_sha256: str,
        dense_index_path: Union[str, Path],
        candidates: Sequence[Mapping[str, Any]],
    ) -> CandidateWindowRescanPlan:
        """Compute the exact immutable candidate-window bound without writes."""

        plan, _ = self._plan_with_representatives(
            source_sha256=source_sha256,
            dense_index_path=dense_index_path,
            candidates=candidates,
        )
        return plan

    def build(
        self,
        media_path: Union[str, Path],
        *,
        source_sha256: str,
        dense_index_path: Union[str, Path],
        candidates: Sequence[Mapping[str, Any]],
        project_root: Union[str, Path],
        output_directory: Union[str, Path],
        cancel: Optional[CancelCallback] = None,
    ) -> CandidateWindowRescanResult:
        if (
            not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
        ):
            raise ValueError(
                "source_sha256 must be a lowercase registered SHA-256"
            )
        try:
            source, source_identity = _validated_source(
                media_path, self._limits.max_source_bytes
            )
            project_root, output_root = _validated_project_output(
                project_root, output_directory
            )
        except DenseFrameIndexError as error:
            raise _convert_dense_error(error) from error
        except ValueError as error:
            raise CandidateWindowRescanError(str(error)) from error
        if _cancel_requested(cancel):
            raise CandidateWindowRescanCancelled(
                "candidate-window rescan was cancelled"
            )
        plan, representatives = self._plan_with_representatives(
            source_sha256=source_sha256,
            dense_index_path=dense_index_path,
            candidates=candidates,
        )
        dense_index_sha256 = plan.dense_index_sha256
        windows = plan.windows
        input_revision = plan.input_revision
        final_directory = (
            output_root
            / f"candidate-window-rescan-{input_revision[:24]}"
        )
        if final_directory.exists():
            return load_candidate_window_rescan(
                final_directory,
                expected_source_sha256=source_sha256,
                expected_dense_index_sha256=dense_index_sha256,
                expected_input_revision=input_revision,
                maximum_manifest_bytes=self._limits.max_manifest_bytes,
                maximum_total_bytes=self._limits.max_total_output_bytes,
                cached=True,
            )
        required_free = (
            plan.planned_output_bytes_upper_bound
            + self._limits.min_free_disk_bytes
        )
        if shutil.disk_usage(output_root).free < required_free:
            raise CandidateWindowRescanOutputLimit(
                "project output disk cannot fit the bounded rescan and reserve"
            )

        deadline = time.monotonic() + self._limits.max_total_seconds
        try:
            _verify_source_sha256(
                source,
                source_sha256,
                source_identity,
                deadline=deadline,
                cancel=cancel,
            )
        except DenseFrameIndexError as error:
            raise _convert_dense_error(error) from error

        temporary_directory: Optional[Path] = None
        try:
            temporary_directory = Path(
                tempfile.mkdtemp(
                    prefix=(
                        f"{_TEMP_RUN_PREFIX}"
                        f"{input_revision[:8]}-"
                    ),
                    # Keep the FFmpeg cwd and frame destination bounded on
                    # Windows. The immutable final result still lives under
                    # the caller-authorized output root, but UUID-heavy
                    # pipeline paths can otherwise exceed Win32's legacy
                    # process working-directory limit before extraction
                    # starts.
                    dir=str(project_root),
                )
            )
            try:
                os.chmod(temporary_directory, 0o700)
            except OSError:
                pass
            windows_root = temporary_directory / "windows"
            windows_root.mkdir(exist_ok=False)
            if (
                windows_root.is_symlink()
                or _is_reparse_point(windows_root)
            ):
                raise CandidateWindowRescanError(
                    "candidate window output directory is unsafe"
                )
            ffmpeg_version = _parse_tool_version(
                self._run(
                    MediaCommand(
                        argv=(self._ffmpeg, "-version"),
                        cwd=temporary_directory,
                        timeout_seconds=_remaining_timeout(
                            deadline,
                            self._limits.probe_timeout_seconds,
                        ),
                        capture_limit_bytes=16 * 1024,
                    ),
                    cancel,
                ),
                "ffmpeg",
            )
            frames: list[CandidateWindowFrame] = []
            frame_bytes = 0
            actual_windows = []
            sampling_filter = (
                f"fps=fps={self._limits.sampling_fps}:"
                "start_time=0:round=near,"
                f"scale=w=min(iw\\,{self._limits.max_frame_width}):"
                "h=-2:flags=lanczos"
            )
            for window in windows:
                if _cancel_requested(cancel):
                    raise CandidateWindowRescanCancelled(
                        "candidate-window rescan was cancelled"
                    )
                window_root = windows_root / window.window_id
                window_root.mkdir(exist_ok=False)
                frame_pattern = window_root / "frame-%06d.jpg"
                output = self._run(
                    MediaCommand(
                        argv=(
                            self._ffmpeg,
                            "-hide_banner",
                            "-nostdin",
                            "-loglevel",
                            "error",
                            "-ss",
                            f"{window.start_seconds:.6f}",
                            "-i",
                            _media_tool_path(source),
                            "-map",
                            "0:v:0",
                            "-an",
                            "-sn",
                            "-dn",
                            "-t",
                            f"{window.duration_seconds:.6f}",
                            "-vf",
                            sampling_filter,
                            "-frames:v",
                            str(window.planned_frame_limit),
                            "-q:v",
                            "4",
                            "-start_number",
                            "0",
                            "-vsync",
                            "0",
                            "-n",
                            _media_tool_path(frame_pattern),
                        ),
                        cwd=temporary_directory,
                        timeout_seconds=_remaining_timeout(
                            deadline,
                            self._limits.extraction_timeout_seconds,
                        ),
                        capture_limit_bytes=(
                            self._limits.max_process_output_bytes
                        ),
                        watch_directory=window_root,
                        watch_limit_bytes=(
                            self._limits.max_total_output_bytes
                        ),
                    ),
                    cancel,
                )
                if output.returncode != 0:
                    message = output.stderr[:2_048].decode(
                        "utf-8", errors="replace"
                    ).strip()
                    raise CandidateWindowRescanError(
                        "ffmpeg could not extract a candidate window"
                        + (f": {message}" if message else "")
                    )
                if _source_identity(source) != source_identity:
                    raise CandidateWindowRescanSourceChanged(
                        "protected source changed during candidate rescan"
                    )
                children = list(window_root.iterdir())
                indexed: dict[int, Path] = {}
                for child in children:
                    if (
                        child.is_symlink()
                        or _is_reparse_point(child)
                        or not child.is_file()
                    ):
                        raise CandidateWindowRescanError(
                            "candidate window contains an unsafe artifact"
                        )
                    matched = _FRAME_NAME.fullmatch(child.name)
                    if matched is None:
                        raise CandidateWindowRescanError(
                            "candidate window contains an unexpected artifact"
                        )
                    index = int(matched.group("index"))
                    if index in indexed:
                        raise CandidateWindowRescanError(
                            "candidate window contains duplicate frame indexes"
                        )
                    indexed[index] = child
                if not indexed or sorted(indexed) != list(
                    range(len(indexed))
                ):
                    raise CandidateWindowRescanError(
                        "candidate window produced no contiguous frames"
                    )
                if len(indexed) > window.planned_frame_limit:
                    raise CandidateWindowRescanOutputLimit(
                        "candidate window exceeded its frame bound"
                    )
                actual_windows.append(
                    {
                        **asdict(window),
                        "actual_frame_count": len(indexed),
                    }
                )
                for frame_index in range(len(indexed)):
                    path = indexed[frame_index]
                    metadata = path.stat()
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                        or not 4
                        <= metadata.st_size
                        <= self._limits.max_frame_bytes
                    ):
                        raise CandidateWindowRescanOutputLimit(
                            "candidate frame violates its file-size contract"
                        )
                    frame_bytes += int(metadata.st_size)
                    if (
                        frame_bytes
                        > self._limits.max_total_output_bytes
                        or len(frames) + 1 > self._limits.max_frames
                    ):
                        raise CandidateWindowRescanOutputLimit(
                            "candidate rescan exceeded its total output bound"
                        )
                    width, height = _jpeg_dimensions(
                        path, int(metadata.st_size)
                    )
                    if (
                        width > self._limits.max_frame_width
                        or width * height
                        > self._limits.max_frame_pixels
                    ):
                        raise CandidateWindowRescanOutputLimit(
                            "candidate frame violates its dimension contract"
                        )
                    timestamp_numerator = (
                        round(window.start_seconds * 1_000_000)
                        * self._limits.sampling_fps
                        + frame_index * 1_000_000
                    )
                    timestamp_denominator = (
                        1_000_000 * self._limits.sampling_fps
                    )
                    timestamp = round(
                        timestamp_numerator
                        / timestamp_denominator,
                        6,
                    )
                    if (
                        timestamp
                        > window.end_seconds
                        + 1 / self._limits.sampling_fps
                    ):
                        raise CandidateWindowRescanError(
                            "candidate frame timestamp exceeds its window"
                        )
                    evidence_id = _evidence_id(
                        source_sha256=source_sha256,
                        dense_index_sha256=dense_index_sha256,
                        fps=self._limits.sampling_fps,
                        window_id=window.window_id,
                        frame_index=frame_index,
                    )
                    frames.append(
                        CandidateWindowFrame(
                            evidence_id=evidence_id,
                            window_id=window.window_id,
                            frame_index=frame_index,
                            timestamp_seconds=timestamp,
                            timestamp_numerator=timestamp_numerator,
                            timestamp_denominator=timestamp_denominator,
                            source_candidate_ids=(
                                window.source_candidate_ids
                            ),
                            primary_candidate_id=_primary_candidate(
                                window.source_candidate_ids,
                                representatives,
                                timestamp,
                            ),
                            relative_path=(
                                f"windows/{window.window_id}/"
                                f"{path.name}"
                            ),
                            width=width,
                            height=height,
                            size_bytes=int(metadata.st_size),
                            sha256=_sha256_regular_file(
                                path, int(metadata.st_size)
                            ),
                        )
                    )
            if not frames:
                raise CandidateWindowRescanError(
                    "candidate rescan produced no verified frames"
                )
            downstream_evidence_ids = _select_downstream_frames(
                frames,
                maximum=self._limits.max_downstream_frames,
            )
            manifest_payload = {
                "schema_version": CANDIDATE_WINDOW_RESCAN_SCHEMA,
                "input_revision": input_revision,
                "source_sha256": source_sha256,
                "dense_index_sha256": dense_index_sha256,
                "sampling_fps": self._limits.sampling_fps,
                "window_count": len(windows),
                "frame_count": len(frames),
                "downstream_frame_count": len(
                    downstream_evidence_ids
                ),
                "downstream_evidence_ids": list(
                    downstream_evidence_ids
                ),
                "windows": actual_windows,
                "frames": [asdict(frame) for frame in frames],
                "provenance": {
                    "source_access": (
                        "read_only_sha256_and_identity_verified"
                    ),
                    "dense_index_role": (
                        "2fps_temporal_scope_contract"
                    ),
                    "window_method": (
                        "analysis_candidate_ranges_padded_and_merged"
                    ),
                    "extraction_scope": "candidate_windows_only",
                    "whole_video_high_frequency_scan": False,
                    "timestamp_method": (
                        "window_start_plus_zero_based_frame_index_divided"
                        "_by_sampling_fps"
                    ),
                    "ffmpeg_version": ffmpeg_version,
                    "sampling_filter": sampling_filter,
                    "limits": asdict(self._limits),
                    "review_policy": asdict(ReviewOnlyPolicy()),
                },
            }
            manifest_sha256, _ = _write_manifest(
                temporary_directory,
                manifest_payload,
                limits=self._limits,
                frame_bytes=frame_bytes,
            )
            if _source_identity(source) != source_identity:
                raise CandidateWindowRescanSourceChanged(
                    "protected source changed before rescan finalization"
                )
            if {
                child.name for child in temporary_directory.iterdir()
            } != {"windows", "candidate-window-rescan.json"}:
                raise CandidateWindowRescanError(
                    "candidate rescan contains unexpected root artifacts"
                )
            _directory_size(
                temporary_directory,
                self._limits.max_total_output_bytes,
            )
            try:
                os.replace(temporary_directory, final_directory)
            except FileExistsError:
                # A concurrent idempotent builder won.  Validate and reuse
                # its complete immutable result rather than overwriting it.
                _safe_remove_run(temporary_directory, project_root)
            temporary_directory = None
            loaded = load_candidate_window_rescan(
                final_directory,
                expected_source_sha256=source_sha256,
                expected_dense_index_sha256=dense_index_sha256,
                expected_input_revision=input_revision,
                maximum_manifest_bytes=self._limits.max_manifest_bytes,
                maximum_total_bytes=self._limits.max_total_output_bytes,
                cached=False,
            )
            if loaded.manifest_sha256 != manifest_sha256:
                raise CandidateWindowRescanManifestInvalid(
                    "finalized candidate rescan manifest changed"
                )
            return loaded
        except CandidateWindowRescanError:
            raise
        except DenseFrameIndexError as error:
            raise _convert_dense_error(error) from error
        except OSError as error:
            raise CandidateWindowRescanError(
                "candidate rescan filesystem operation failed"
            ) from error
        finally:
            _safe_remove_run(temporary_directory, project_root)


def load_candidate_window_rescan(
    run_directory: Union[str, Path],
    *,
    expected_source_sha256: Optional[str] = None,
    expected_dense_index_sha256: Optional[str] = None,
    expected_input_revision: Optional[str] = None,
    maximum_manifest_bytes: int = 64 * 1024 * 1024,
    maximum_total_bytes: int = 8 * 1024**3,
    cached: bool = True,
) -> CandidateWindowRescanResult:
    """Reopen and fully verify one immutable rescan evidence set."""

    root = Path(run_directory)
    if (
        not root.is_absolute()
        or _RUN_DIRECTORY.fullmatch(root.name) is None
        or root.is_symlink()
        or _is_reparse_point(root)
        or not root.is_dir()
    ):
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan run directory is invalid"
        )
    raw, manifest_sha256 = _read_bounded_regular_file(
        root / "candidate-window-rescan.json",
        maximum_bytes=maximum_manifest_bytes,
        label="candidate rescan manifest",
    )
    payload = _strict_json(raw, "candidate rescan manifest")
    source_sha256 = payload.get("source_sha256")
    dense_index_sha256 = payload.get("dense_index_sha256")
    input_revision = payload.get("input_revision")
    windows_value = payload.get("windows")
    frames_value = payload.get("frames")
    downstream_value = payload.get("downstream_evidence_ids")
    sampling_fps = payload.get("sampling_fps")
    provenance = payload.get("provenance")
    limits_value = (
        provenance.get("limits")
        if isinstance(provenance, Mapping)
        else None
    )
    review_policy = (
        provenance.get("review_policy")
        if isinstance(provenance, Mapping)
        else None
    )
    if (
        payload.get("schema_version")
        != CANDIDATE_WINDOW_RESCAN_SCHEMA
        or not isinstance(source_sha256, str)
        or _SHA256.fullmatch(source_sha256) is None
        or not isinstance(dense_index_sha256, str)
        or _SHA256.fullmatch(dense_index_sha256) is None
        or not isinstance(input_revision, str)
        or _SHA256.fullmatch(input_revision) is None
        or isinstance(sampling_fps, bool)
        or not isinstance(sampling_fps, int)
        or not 5 <= sampling_fps <= 10
        or not isinstance(provenance, Mapping)
        or provenance.get("extraction_scope")
        != "candidate_windows_only"
        or provenance.get("whole_video_high_frequency_scan")
        is not False
        or not isinstance(limits_value, Mapping)
        or not isinstance(review_policy, Mapping)
        or review_policy != asdict(ReviewOnlyPolicy())
        or (
            expected_source_sha256 is not None
            and source_sha256 != expected_source_sha256
        )
        or (
            expected_dense_index_sha256 is not None
            and dense_index_sha256
            != expected_dense_index_sha256
        )
        or (
            expected_input_revision is not None
            and input_revision != expected_input_revision
        )
        or not isinstance(windows_value, list)
        or not isinstance(frames_value, list)
        or not isinstance(downstream_value, list)
        or payload.get("window_count") != len(windows_value)
        or payload.get("frame_count") != len(frames_value)
        or payload.get("downstream_frame_count")
        != len(downstream_value)
        or not windows_value
        or not frames_value
    ):
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan manifest provenance is invalid"
        )
    try:
        manifest_limits = CandidateWindowRescanLimits(
            **dict(limits_value)
        )
    except (TypeError, ValueError) as error:
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan limit provenance is invalid"
        ) from error
    if (
        manifest_limits.sampling_fps != sampling_fps
        or root.name
        != f"candidate-window-rescan-{input_revision[:24]}"
    ):
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan identity does not match its manifest"
        )
    windows: list[CandidateWindow] = []
    window_ids: set[str] = set()
    window_candidates: dict[str, Tuple[str, ...]] = {}
    for raw_window in windows_value:
        if not isinstance(raw_window, Mapping):
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan window is invalid"
            )
        window_id = raw_window.get("window_id")
        source_candidate_ids = raw_window.get(
            "source_candidate_ids"
        )
        start = _finite_seconds(raw_window.get("start_seconds"))
        end = _finite_seconds(raw_window.get("end_seconds"))
        duration = _finite_seconds(raw_window.get("duration_seconds"))
        planned = raw_window.get("planned_frame_limit")
        actual = raw_window.get("actual_frame_count")
        if (
            not isinstance(window_id, str)
            or _WINDOW_ID.fullmatch(window_id) is None
            or window_id in window_ids
            or not isinstance(source_candidate_ids, list)
            or not source_candidate_ids
            or any(
                not isinstance(item, str)
                or _CANDIDATE_ID.fullmatch(item) is None
                for item in source_candidate_ids
            )
            or len(set(source_candidate_ids))
            != len(source_candidate_ids)
            or tuple(source_candidate_ids)
            != tuple(sorted(source_candidate_ids))
            or start is None
            or end is None
            or duration is None
            or start < 0
            or end <= start
            or abs((end - start) - duration) > 0.00001
            or isinstance(planned, bool)
            or not isinstance(planned, int)
            or planned <= 0
            or isinstance(actual, bool)
            or not isinstance(actual, int)
            or not 1 <= actual <= planned
        ):
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan window contract is invalid"
            )
        expected_window_id = hashlib.sha256(
            _canonical_json(
                {
                    "schema_version": (
                        CANDIDATE_WINDOW_RESCAN_SCHEMA
                    ),
                    "source_sha256": source_sha256,
                    "dense_index_sha256": dense_index_sha256,
                    "sampling_fps": sampling_fps,
                    "start_seconds": start,
                    "end_seconds": end,
                    "source_candidate_ids": tuple(
                        source_candidate_ids
                    ),
                }
            )
        ).hexdigest()[:20]
        if window_id != expected_window_id:
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan window identity is invalid"
            )
        window_ids.add(window_id)
        candidate_ids = tuple(source_candidate_ids)
        window_candidates[window_id] = candidate_ids
        windows.append(
            CandidateWindow(
                window_id=window_id,
                start_seconds=start,
                end_seconds=end,
                duration_seconds=duration,
                source_candidate_ids=candidate_ids,
                planned_frame_limit=planned,
            )
        )
    if (
        _input_revision(
            source_sha256=source_sha256,
            dense_index_sha256=dense_index_sha256,
            limits=manifest_limits,
            windows=windows,
        )
        != input_revision
    ):
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan input revision is invalid"
        )
    frames: list[CandidateWindowFrame] = []
    evidence_ids: set[str] = set()
    total_bytes = len(raw)
    counts_by_window = {window_id: 0 for window_id in window_ids}
    indexes_by_window: dict[str, set[int]] = {
        window_id: set() for window_id in window_ids
    }
    window_by_id = {
        window.window_id: window for window in windows
    }
    for raw_frame in frames_value:
        if not isinstance(raw_frame, Mapping):
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan frame is invalid"
            )
        try:
            frame = CandidateWindowFrame(
                evidence_id=str(raw_frame["evidence_id"]),
                window_id=str(raw_frame["window_id"]),
                frame_index=int(raw_frame["frame_index"]),
                timestamp_seconds=float(
                    raw_frame["timestamp_seconds"]
                ),
                timestamp_numerator=int(
                    raw_frame["timestamp_numerator"]
                ),
                timestamp_denominator=int(
                    raw_frame["timestamp_denominator"]
                ),
                source_candidate_ids=tuple(
                    raw_frame["source_candidate_ids"]
                ),
                primary_candidate_id=str(
                    raw_frame["primary_candidate_id"]
                ),
                relative_path=str(raw_frame["relative_path"]),
                width=int(raw_frame["width"]),
                height=int(raw_frame["height"]),
                size_bytes=int(raw_frame["size_bytes"]),
                sha256=str(raw_frame["sha256"]),
                media_type=str(raw_frame.get("media_type", "")),
                evidence_status=str(
                    raw_frame.get("evidence_status", "")
                ),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ) as error:
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan frame contract is invalid"
            ) from error
        expected_relative = (
            f"windows/{frame.window_id}/"
            f"frame-{frame.frame_index:06d}.jpg"
        )
        expected_evidence_id = _evidence_id(
            source_sha256=source_sha256,
            dense_index_sha256=dense_index_sha256,
            fps=sampling_fps,
            window_id=frame.window_id,
            frame_index=frame.frame_index,
        )
        window = window_by_id.get(frame.window_id)
        if (
            _EVIDENCE_ID.fullmatch(frame.evidence_id) is None
            or frame.evidence_id != expected_evidence_id
            or frame.evidence_id in evidence_ids
            or frame.window_id not in window_ids
            or frame.frame_index < 0
            or not math.isfinite(frame.timestamp_seconds)
            or frame.timestamp_seconds < 0
            or frame.timestamp_denominator <= 0
            or frame.timestamp_denominator
            != 1_000_000 * sampling_fps
            or abs(
                frame.timestamp_seconds
                - frame.timestamp_numerator
                / frame.timestamp_denominator
            )
            > 0.000001
            or frame.source_candidate_ids
            != window_candidates[frame.window_id]
            or frame.primary_candidate_id
            not in frame.source_candidate_ids
            or frame.relative_path != expected_relative
            or frame.width <= 0
            or frame.height <= 0
            or frame.size_bytes <= 0
            or _SHA256.fullmatch(frame.sha256) is None
            or frame.media_type != "image/jpeg"
            or frame.evidence_status != "unreviewed_candidate"
            or window is None
            or frame.timestamp_numerator
            != (
                round(window.start_seconds * 1_000_000)
                * sampling_fps
                + frame.frame_index * 1_000_000
            )
        ):
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan frame contract is invalid"
            )
        path = root / Path(frame.relative_path)
        try:
            resolved = path.resolve(strict=True)
            metadata = path.stat()
            actual_sha256 = _sha256_regular_file(
                path, frame.size_bytes
            )
            width, height = _jpeg_dimensions(
                path, frame.size_bytes
            )
        except DenseFrameIndexError as error:
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan frame failed integrity validation"
            ) from error
        except OSError as error:
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan frame is unavailable"
            ) from error
        if (
            resolved.parent.parent.parent != root
            or path.is_symlink()
            or _is_reparse_point(path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != frame.size_bytes
            or actual_sha256 != frame.sha256
        ):
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan frame failed integrity validation"
            )
        if width != frame.width or height != frame.height:
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan frame dimensions do not match"
            )
        total_bytes += frame.size_bytes
        if total_bytes > maximum_total_bytes:
            raise CandidateWindowRescanOutputLimit(
                "candidate rescan exceeds its total storage bound"
            )
        evidence_ids.add(frame.evidence_id)
        counts_by_window[frame.window_id] += 1
        if frame.frame_index in indexes_by_window[frame.window_id]:
            raise CandidateWindowRescanManifestInvalid(
                "candidate rescan contains a duplicate frame index"
            )
        indexes_by_window[frame.window_id].add(frame.frame_index)
        frames.append(frame)
    actual_counts = {
        str(row["window_id"]): int(row["actual_frame_count"])
        for row in windows_value
    }
    if counts_by_window != actual_counts:
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan window frame counts do not match"
        )
    if any(
        indexes != set(range(len(indexes)))
        for indexes in indexes_by_window.values()
    ):
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan frame indexes are not contiguous"
        )
    if (
        len(downstream_value) > 512
        or any(
            not isinstance(item, str)
            or item not in evidence_ids
            for item in downstream_value
        )
        or len(set(downstream_value)) != len(downstream_value)
    ):
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan downstream evidence selection is invalid"
        )
    if tuple(downstream_value) != _select_downstream_frames(
        frames,
        maximum=manifest_limits.max_downstream_frames,
    ):
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan downstream selection is not deterministic"
        )
    if {
        child.name for child in root.iterdir()
    } != {"windows", "candidate-window-rescan.json"}:
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan contains unexpected root artifacts"
        )
    return CandidateWindowRescanResult(
        output_directory=str(root),
        manifest_relative_path="candidate-window-rescan.json",
        manifest_sha256=manifest_sha256,
        input_revision=input_revision,
        windows=tuple(windows),
        frames=tuple(frames),
        downstream_evidence_ids=tuple(downstream_value),
        cached=cached,
    )


def resolve_candidate_window_frame(
    result: CandidateWindowRescanResult,
    evidence_id: str,
) -> ResolvedCandidateWindowFrame:
    """Resolve and re-verify one internal high-frequency JPEG handle."""

    if (
        not isinstance(evidence_id, str)
        or _EVIDENCE_ID.fullmatch(evidence_id) is None
    ):
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan evidence identity is invalid"
        )
    frame = next(
        (
            item
            for item in result.frames
            if item.evidence_id == evidence_id
        ),
        None,
    )
    if frame is None:
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan evidence is unavailable"
        )
    root = Path(result.output_directory)
    path = root / Path(frame.relative_path)
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
        actual_sha256 = _sha256_regular_file(
            path, frame.size_bytes
        )
    except DenseFrameIndexError as error:
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan evidence failed integrity verification"
        ) from error
    except OSError as error:
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan evidence is unavailable"
        ) from error
    if (
        resolved.parent.parent.parent != root
        or path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != frame.size_bytes
        or actual_sha256 != frame.sha256
    ):
        raise CandidateWindowRescanManifestInvalid(
            "candidate rescan evidence failed integrity verification"
        )
    return ResolvedCandidateWindowFrame(
        path=resolved,
        media_type=frame.media_type,
        size_bytes=frame.size_bytes,
        sha256=frame.sha256,
    )


__all__ = [
    "CANDIDATE_WINDOW_RESCAN_SCHEMA",
    "CandidateWindow",
    "CandidateWindowFrame",
    "CandidateWindowRescanCancelled",
    "CandidateWindowRescanEngine",
    "CandidateWindowRescanError",
    "CandidateWindowRescanLimits",
    "CandidateWindowRescanManifestInvalid",
    "CandidateWindowRescanOutputLimit",
    "CandidateWindowRescanPlan",
    "CandidateWindowRescanResult",
    "CandidateWindowRescanSourceChanged",
    "CandidateWindowRescanTimeout",
    "ResolvedCandidateWindowFrame",
    "load_candidate_window_rescan",
    "resolve_candidate_window_frame",
]
