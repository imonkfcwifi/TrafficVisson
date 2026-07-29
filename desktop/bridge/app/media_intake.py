from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import queue
import re
import shutil
import stat as stat_module
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import UUID, uuid4

from fastapi import HTTPException


ALLOWED_VIDEO_EXTENSIONS = frozenset(
    {
        ".avi",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mts",
        ".webm",
    }
)
MAX_DIRECTORY_PATH_LENGTH = 4_096
MAX_MEDIA_FILE_NAME_LENGTH = 255
MAX_MEDIA_FILES = 10_000
MAX_FFPROBE_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_FFPROBE_STREAMS = 128
MAX_MEDIA_DURATION_SECONDS = 31 * 24 * 60 * 60
MAX_VIDEO_DIMENSION = 65_536
MAX_SNAPSHOT_RECORDS = 256
DEFAULT_MIN_STABILITY_INTERVAL_SECONDS = 10.0
HASH_CHUNK_SIZE_BYTES = 4 * 1024 * 1024
MAX_HASH_JOB_RECORDS = 128
MAX_QUEUED_HASH_JOBS = 32
_ACTIVE_HASH_STATES = frozenset({"queued", "running", "cancelling"})
_TERMINAL_HASH_STATES = frozenset({"completed", "cancelled", "failed"})
_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")
_SAFE_CODEC_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAFE_FORMAT_PATTERN = re.compile(r"^[A-Za-z0-9_,.-]{1,256}$")
_SAFE_RATE_PATTERN = re.compile(r"^[0-9]{1,12}/[0-9]{1,12}$")
_SAFE_UNSIGNED_INTEGER_PATTERN = re.compile(r"^[0-9]{1,39}$")


@dataclass(frozen=True)
class MediaFileRecord:
    name: str
    extension: str
    size_bytes: int
    created_time_ns: int
    modified_time_ns: int


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: UUID
    project_id: UUID
    directory: str
    directory_identity: str
    captured_at: datetime
    captured_monotonic_ns: int
    sequence: int
    digest: str
    files: Tuple[MediaFileRecord, ...]

    @property
    def files_by_name(self) -> Mapping[str, MediaFileRecord]:
        return {item.name: item for item in self.files}


@dataclass
class HashJobRecord:
    job_id: UUID
    project_id: UUID
    snapshot: SnapshotRecord
    file_record: MediaFileRecord
    status: str
    bytes_hashed: int
    sha256_lower: Optional[str]
    sha256_upper: Optional[str]
    error_code: Optional[str]
    created_at: datetime
    updated_at: datetime
    cancel_event: threading.Event


@dataclass(frozen=True)
class CompletedMediaGrant:
    """Internal-only authorization for a hash-verified project media file."""

    project_id: UUID
    snapshot_id: UUID
    hash_job_id: UUID
    file_name: str
    media_path: Path
    size_bytes: int
    modified_time_ns: int
    sha256_lower: str


class _HashCancelled(Exception):
    pass


class _HashFileChanged(Exception):
    pass


class _HashReadFailed(Exception):
    pass


def is_allowlisted_video_name(name: str) -> bool:
    """Return True only for a plain filename with a supported video suffix."""
    if not isinstance(name, str) or not name or len(name) > MAX_MEDIA_FILE_NAME_LENGTH:
        return False
    if "\x00" in name or "/" in name or "\\" in name:
        return False
    if name in {".", ".."}:
        return False
    return Path(name).suffix.casefold() in ALLOWED_VIDEO_EXTENSIONS


def validate_explicit_directory_syntax(directory: str) -> str:
    """Validate an explicit local/UNC directory without expanding user syntax."""
    if not isinstance(directory, str):
        raise ValueError("Directory must be a string")
    if not directory or len(directory) > MAX_DIRECTORY_PATH_LENGTH:
        raise ValueError("Directory path length is invalid")
    if directory != directory.strip():
        raise ValueError("Directory path must not contain outer whitespace")
    if any(ord(character) < 32 for character in directory):
        raise ValueError("Directory path contains control characters")

    normalized_slashes = directory.replace("/", "\\")
    lowered = normalized_slashes.casefold()
    if lowered.startswith(tuple(prefix.casefold() for prefix in _DEVICE_PREFIXES)):
        raise ValueError("Device namespace paths are not supported")
    if "globalroot" in lowered.split("\\"):
        raise ValueError("Device namespace paths are not supported")

    # This product is Windows-first. ntpath also lets tests validate UNC syntax
    # consistently when the Python process is hosted by another OS.
    drive, tail = ntpath.splitdrive(normalized_slashes)
    if normalized_slashes.startswith("\\\\"):
        if not drive.startswith("\\\\") or drive.count("\\") < 3:
            raise ValueError("UNC paths must include a server and share")
    elif re.match(r"^[A-Za-z]:$", drive) is None or not tail.startswith("\\"):
        if os.name != "nt" and os.path.isabs(directory):
            # Development/test fallback; packaged TrafficVisson runs on Windows.
            path_parts = Path(directory).parts
            if any(part in {".", ".."} for part in path_parts):
                raise ValueError("Directory path must not contain dot segments")
            return os.path.normpath(directory)
        raise ValueError("Directory must be an absolute local or UNC path")

    path_parts = [part for part in tail.split("\\") if part]
    if any(part in {".", ".."} for part in path_parts):
        raise ValueError("Directory path must not contain dot segments")
    if any(part.endswith((" ", ".")) for part in path_parts):
        raise ValueError("Directory path contains an ambiguous segment")
    if any(":" in part for part in path_parts):
        raise ValueError("Alternate data stream syntax is not supported")
    if any(any(character in part for character in '<>"|?*') for part in path_parts):
        raise ValueError("Directory path contains unsupported wildcard characters")
    return ntpath.normpath(normalized_slashes)


def _is_reparse_point(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _creation_time_ns(stat_result: os.stat_result) -> int:
    birth_time_ns = getattr(stat_result, "st_birthtime_ns", None)
    if birth_time_ns is not None:
        return int(birth_time_ns)
    return int(stat_result.st_ctime_ns)


def _metadata_digest(files: Iterable[MediaFileRecord]) -> str:
    digest = hashlib.sha256()
    digest.update(b"trafficvisson-media-metadata-v1\x00")
    for item in files:
        name_bytes = item.name.encode("utf-8", errors="strict")
        digest.update(len(name_bytes).to_bytes(4, "big", signed=False))
        digest.update(name_bytes)
        for value in (
            item.size_bytes,
            item.created_time_ns,
            item.modified_time_ns,
        ):
            digest.update(int(value).to_bytes(16, "big", signed=False))
    return digest.hexdigest()


def _directory_identity(directory: str) -> str:
    if os.name == "nt":
        return ntpath.normcase(ntpath.normpath(directory))
    return os.path.normcase(os.path.normpath(directory))


def _record_from_stat(name: str, stat_result: os.stat_result) -> MediaFileRecord:
    return MediaFileRecord(
        name=name,
        extension=Path(name).suffix.casefold(),
        size_bytes=int(stat_result.st_size),
        created_time_ns=_creation_time_ns(stat_result),
        modified_time_ns=int(stat_result.st_mtime_ns),
    )


def _snapshot_payload(record: SnapshotRecord) -> Dict:
    return {
        "snapshot_id": record.snapshot_id,
        "captured_at": record.captured_at,
        "snapshot_digest": record.digest,
        "digest_basis": "name_size_creation_mtime_sha256_v1",
        "content_hash_status": "not_computed",
        "file_count": len(record.files),
        "total_size_bytes": sum(item.size_bytes for item in record.files),
        "files": [
            {
                "name": item.name,
                "extension": item.extension,
                "size_bytes": item.size_bytes,
                "created_time_ns": item.created_time_ns,
                "modified_time_ns": item.modified_time_ns,
            }
            for item in record.files
        ],
    }


def _same_metadata(
    record: MediaFileRecord, stat_result: os.stat_result
) -> bool:
    current = _record_from_stat(record.name, stat_result)
    return current == record


def _safe_float(value: object, *, field_name: str) -> float:
    if isinstance(value, str) and len(value) > 64:
        raise ValueError(f"ffprobe {field_name} is invalid")
    if isinstance(value, bool):
        raise ValueError(f"ffprobe {field_name} is invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"ffprobe {field_name} is invalid") from error
    if not math.isfinite(parsed):
        raise ValueError(f"ffprobe {field_name} is invalid")
    return parsed


def _safe_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"ffprobe {field_name} is invalid")
    if isinstance(value, str) and not _SAFE_UNSIGNED_INTEGER_PATTERN.fullmatch(
        value
    ):
        raise ValueError(f"ffprobe {field_name} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"ffprobe {field_name} is invalid") from error
    if parsed <= 0:
        raise ValueError(f"ffprobe {field_name} is invalid")
    return parsed


class MediaIntakeService:
    """Read-only metadata snapshots and bounded ffprobe for selected media."""

    def __init__(
        self,
        *,
        ffprobe_path: Optional[str] = None,
        minimum_interval_seconds: float = DEFAULT_MIN_STABILITY_INTERVAL_SECONDS,
    ) -> None:
        if (
            not math.isfinite(minimum_interval_seconds)
            or not 0 <= minimum_interval_seconds <= 3_600
        ):
            raise ValueError(
                "minimum_interval_seconds must be between 0 and 3600"
            )
        self._configured_ffprobe_path = ffprobe_path
        self._minimum_interval_seconds = float(minimum_interval_seconds)
        self._snapshots: "OrderedDict[UUID, SnapshotRecord]" = OrderedDict()
        self._stable_snapshot_ids: set[UUID] = set()
        self._hash_jobs: "OrderedDict[UUID, HashJobRecord]" = OrderedDict()
        self._hash_queue: "queue.Queue[Optional[UUID]]" = queue.Queue(
            maxsize=MAX_QUEUED_HASH_JOBS
        )
        self._hash_worker_thread: Optional[threading.Thread] = None
        self._hash_stop_event = threading.Event()
        self._sequence = 0
        self._lock = threading.RLock()

    def capture_snapshot(self, project_id: UUID, directory: str) -> Dict:
        try:
            normalized = validate_explicit_directory_syntax(directory)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        directory_path = Path(normalized)
        try:
            directory_stat = directory_path.lstat()
        except (OSError, ValueError) as error:
            raise HTTPException(
                status_code=422,
                detail="The selected media directory is unavailable",
            ) from error
        if not directory_path.is_dir():
            raise HTTPException(
                status_code=422,
                detail="The selected media source is not a directory",
            )
        if directory_path.is_symlink() or _is_reparse_point(directory_stat):
            raise HTTPException(
                status_code=422,
                detail="Symbolic links and reparse-point directories are not supported",
            )

        file_records: List[MediaFileRecord] = []
        try:
            with os.scandir(normalized) as entries:
                for entry in entries:
                    if not is_allowlisted_video_name(entry.name):
                        continue
                    if len(file_records) >= MAX_MEDIA_FILES:
                        raise HTTPException(
                            status_code=413,
                            detail="The directory contains too many supported media files",
                        )
                    try:
                        if entry.is_symlink() or not entry.is_file(
                            follow_symlinks=False
                        ):
                            continue
                        stat_result = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise HTTPException(
                            status_code=409,
                            detail="The media directory changed during inspection",
                        ) from error
                    if _is_reparse_point(stat_result):
                        continue
                    file_records.append(
                        _record_from_stat(entry.name, stat_result)
                    )
        except HTTPException:
            raise
        except OSError as error:
            raise HTTPException(
                status_code=422,
                detail="The selected media directory could not be read",
            ) from error

        file_records.sort(key=lambda item: (item.name.casefold(), item.name))
        digest = _metadata_digest(file_records)
        captured_at = datetime.now(timezone.utc)
        captured_monotonic_ns = time.monotonic_ns()

        with self._lock:
            self._sequence += 1
            snapshot_id = uuid4()
            record = SnapshotRecord(
                snapshot_id=snapshot_id,
                project_id=project_id,
                directory=normalized,
                directory_identity=_directory_identity(normalized),
                captured_at=captured_at,
                captured_monotonic_ns=captured_monotonic_ns,
                sequence=self._sequence,
                digest=digest,
                files=tuple(file_records),
            )
            self._snapshots[snapshot_id] = record
            while len(self._snapshots) > MAX_SNAPSHOT_RECORDS:
                evicted_id, _ = self._snapshots.popitem(last=False)
                self._stable_snapshot_ids.discard(evicted_id)

        return _snapshot_payload(record)

    def register_local_file(
        self, project_id: UUID, selected_path: str
    ) -> Dict:
        """Register one Electron-selected file as a stable, opaque snapshot.

        Electron's native picker has already converted the renderer's opaque
        selection ID to a canonical path. This method repeats all filesystem
        checks at the Python trust boundary, stores the path only in the
        in-memory snapshot registry, and returns metadata without the path.

        Unlike a watched upload directory, a native file selection does not
        need three timed directory snapshots. Probe and SHA-256 operations
        still re-check the same file metadata and fail closed on any change.
        """
        if not isinstance(selected_path, str):
            raise HTTPException(
                status_code=422,
                detail="The selected media path is invalid",
            )
        if (
            not selected_path
            or len(selected_path) > MAX_DIRECTORY_PATH_LENGTH
            or selected_path != selected_path.strip()
            or any(ord(character) < 32 for character in selected_path)
        ):
            raise HTTPException(
                status_code=422,
                detail="The selected media path is invalid",
            )

        candidate = Path(selected_path)
        if not candidate.is_absolute() or not is_allowlisted_video_name(
            candidate.name
        ):
            raise HTTPException(
                status_code=422,
                detail="The selected filename or video extension is not supported",
            )
        if (
            candidate.name.endswith((" ", "."))
            or any(character in candidate.name for character in '<>:"|?*')
        ):
            raise HTTPException(
                status_code=422,
                detail="The selected filename contains unsupported characters",
            )

        try:
            normalized_directory = validate_explicit_directory_syntax(
                str(candidate.parent)
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        candidate = Path(normalized_directory) / candidate.name

        try:
            directory_stat = candidate.parent.lstat()
            file_stat = os.stat(candidate, follow_symlinks=False)
        except (OSError, ValueError) as error:
            raise HTTPException(
                status_code=422,
                detail="The selected media file is no longer available",
            ) from error
        if (
            not candidate.parent.is_dir()
            or candidate.parent.is_symlink()
            or _is_reparse_point(directory_stat)
        ):
            raise HTTPException(
                status_code=422,
                detail="Symbolic links and reparse-point directories are not supported",
            )
        if (
            candidate.is_symlink()
            or _is_reparse_point(file_stat)
            or not stat_module.S_ISREG(file_stat.st_mode)
        ):
            raise HTTPException(
                status_code=422,
                detail="The selected media source is not a regular file",
            )

        file_record = _record_from_stat(candidate.name, file_stat)
        digest = _metadata_digest((file_record,))
        captured_at = datetime.now(timezone.utc)
        captured_monotonic_ns = time.monotonic_ns()
        with self._lock:
            self._sequence += 1
            snapshot_id = uuid4()
            record = SnapshotRecord(
                snapshot_id=snapshot_id,
                project_id=project_id,
                directory=normalized_directory,
                directory_identity=_directory_identity(normalized_directory),
                captured_at=captured_at,
                captured_monotonic_ns=captured_monotonic_ns,
                sequence=self._sequence,
                digest=digest,
                files=(file_record,),
            )
            self._snapshots[snapshot_id] = record
            self._stable_snapshot_ids.add(snapshot_id)
            while len(self._snapshots) > MAX_SNAPSHOT_RECORDS:
                evicted_id, _ = self._snapshots.popitem(last=False)
                self._stable_snapshot_ids.discard(evicted_id)

        return _snapshot_payload(record)

    def compare_snapshots(
        self, project_id: UUID, snapshot_ids: Sequence[UUID]
    ) -> Dict:
        if len(snapshot_ids) != 3 or len(set(snapshot_ids)) != 3:
            raise HTTPException(
                status_code=422,
                detail="Exactly three distinct snapshots are required",
            )
        with self._lock:
            records = [self._snapshot(project_id, item) for item in snapshot_ids]
            if not (
                records[0].sequence < records[1].sequence < records[2].sequence
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Snapshots must be supplied in capture order",
                )
            if len({item.directory_identity for item in records}) != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Snapshots must refer to the same selected directory",
                )

            first_unchanged = records[0].digest == records[1].digest
            second_unchanged = records[1].digest == records[2].digest
            observed_intervals = (
                (
                    records[1].captured_monotonic_ns
                    - records[0].captured_monotonic_ns
                )
                / 1_000_000_000,
                (
                    records[2].captured_monotonic_ns
                    - records[1].captured_monotonic_ns
                )
                / 1_000_000_000,
            )
            intervals_long_enough = all(
                interval >= self._minimum_interval_seconds
                for interval in observed_intervals
            )
            has_media = bool(records[2].files)
            stable = (
                first_unchanged
                and second_unchanged
                and intervals_long_enough
                and has_media
            )
            if stable:
                self._stable_snapshot_ids.add(records[2].snapshot_id)

        if not has_media:
            reason = "no_supported_media"
        elif not intervals_long_enough:
            reason = "interval_too_short"
        elif stable:
            reason = "stable"
        elif not first_unchanged and not second_unchanged:
            reason = "both_intervals_changed"
        elif not first_unchanged:
            reason = "first_interval_changed"
        else:
            reason = "second_interval_changed"

        return {
            "snapshot_ids": list(snapshot_ids),
            "first_interval_unchanged": first_unchanged,
            "second_interval_unchanged": second_unchanged,
            "minimum_interval_seconds": self._minimum_interval_seconds,
            "observed_interval_seconds": observed_intervals,
            "stable": stable,
            "reason": reason,
            "file_count": len(records[2].files),
            "total_size_bytes": sum(item.size_bytes for item in records[2].files),
            "stable_snapshot_id": records[2].snapshot_id if stable else None,
        }

    def probe(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        timeout_seconds: int,
    ) -> Dict:
        snapshot, file_record, candidate = self._select_stable_file(
            project_id, snapshot_id, file_name
        )

        ffprobe = self._resolve_ffprobe()
        argv = [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-i",
            str(candidate),
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as error:
            raise HTTPException(
                status_code=504,
                detail="ffprobe timed out while inspecting the selected media",
            ) from error
        except OSError as error:
            raise HTTPException(
                status_code=503,
                detail="ffprobe could not be started",
            ) from error

        if completed.returncode != 0:
            raise HTTPException(
                status_code=422,
                detail="ffprobe rejected the selected media",
            )
        stdout = completed.stdout
        if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > MAX_FFPROBE_OUTPUT_BYTES:
            raise HTTPException(
                status_code=422,
                detail="ffprobe returned an invalid metadata response",
            )
        try:
            raw_probe = json.loads(stdout)
            validated = self._validate_probe_output(raw_probe, file_record)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=422,
                detail="ffprobe returned invalid media metadata",
            ) from error

        try:
            after_stat = os.stat(candidate, follow_symlinks=False)
        except OSError as error:
            raise HTTPException(
                status_code=409,
                detail="The selected media file changed during ffprobe",
            ) from error
        if not self._candidate_matches_record(
            candidate, file_record, after_stat
        ):
            raise HTTPException(
                status_code=409,
                detail="The selected media file changed during ffprobe",
            )

        return {
            "snapshot_id": snapshot_id,
            "snapshot_digest": snapshot.digest,
            "file_name": file_record.name,
            "size_bytes": file_record.size_bytes,
            "duration_seconds": validated["duration_seconds"],
            "format_names": validated["format_names"],
            "video_streams": validated["video_streams"],
            "ffprobe_status": "validated",
            "content_hash_status": "not_computed",
        }

    def start_hash_job(
        self, project_id: UUID, snapshot_id: UUID, file_name: str
    ) -> Dict:
        snapshot, file_record, _ = self._select_stable_file(
            project_id, snapshot_id, file_name
        )
        now = datetime.now(timezone.utc)
        with self._lock:
            for existing in self._hash_jobs.values():
                if (
                    existing.project_id == project_id
                    and existing.snapshot.snapshot_id == snapshot_id
                    and existing.file_record.name == file_record.name
                    and existing.status in _ACTIVE_HASH_STATES
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="A content-hash job is already active for this media file",
                    )
            self._evict_terminal_hash_jobs_locked()
            if len(self._hash_jobs) >= MAX_HASH_JOB_RECORDS:
                raise HTTPException(
                    status_code=429,
                    detail="The content-hash job registry is full",
                )

            job_id = uuid4()
            job = HashJobRecord(
                job_id=job_id,
                project_id=project_id,
                snapshot=snapshot,
                file_record=file_record,
                status="queued",
                bytes_hashed=0,
                sha256_lower=None,
                sha256_upper=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                cancel_event=threading.Event(),
            )
            self._hash_jobs[job_id] = job
            self._ensure_hash_worker_locked()
            try:
                self._hash_queue.put_nowait(job_id)
            except queue.Full as error:
                self._hash_jobs.pop(job_id, None)
                raise HTTPException(
                    status_code=429,
                    detail="The content-hash queue is full",
                ) from error
            return self._serialize_hash_job(job)

    def get_hash_job(self, project_id: UUID, job_id: UUID) -> Dict:
        with self._lock:
            job = self._hash_job(project_id, job_id)
            return self._serialize_hash_job(job)

    def cancel_hash_job(self, project_id: UUID, job_id: UUID) -> Dict:
        with self._lock:
            job = self._hash_job(project_id, job_id)
            if job.status == "queued":
                job.cancel_event.set()
                job.status = "cancelled"
                job.updated_at = datetime.now(timezone.utc)
            elif job.status == "running":
                job.cancel_event.set()
                job.status = "cancelling"
                job.updated_at = datetime.now(timezone.utc)
            elif job.status == "cancelling":
                job.cancel_event.set()
            return self._serialize_hash_job(job)

    def resolve_completed_media(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        hash_job_id: UUID,
        sha256_lower: str,
    ) -> CompletedMediaGrant:
        """Resolve a renderer-safe identity to a currently unchanged local file.

        The absolute path never crosses the HTTP boundary.  Analysis code must
        call this method instead of accepting a path from a request.
        """
        normalized_hash = (
            sha256_lower.casefold() if isinstance(sha256_lower, str) else ""
        )
        if not re.fullmatch(r"[a-f0-9]{64}", normalized_hash):
            raise HTTPException(
                status_code=422,
                detail="The completed media digest is invalid",
            )
        with self._lock:
            job = self._hash_job(project_id, hash_job_id)
            if (
                job.status != "completed"
                or job.snapshot.snapshot_id != snapshot_id
                or job.file_record.name != file_name
                or job.sha256_lower != normalized_hash
                or snapshot_id not in self._stable_snapshot_ids
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Candidate extraction requires the matching completed "
                        "SHA-256 job for this stable media snapshot"
                    ),
                )
            candidate = self._validate_hash_candidate(job)
            return CompletedMediaGrant(
                project_id=project_id,
                snapshot_id=snapshot_id,
                hash_job_id=hash_job_id,
                file_name=file_name,
                media_path=candidate,
                size_bytes=job.file_record.size_bytes,
                modified_time_ns=job.file_record.modified_time_ns,
                sha256_lower=normalized_hash,
            )

    def shutdown(self) -> None:
        self._hash_stop_event.set()
        with self._lock:
            for job in self._hash_jobs.values():
                if job.status in _ACTIVE_HASH_STATES:
                    job.cancel_event.set()
                    job.status = "cancelled"
                    job.updated_at = datetime.now(timezone.utc)
        try:
            self._hash_queue.put_nowait(None)
        except queue.Full:
            pass

    def _select_stable_file(
        self, project_id: UUID, snapshot_id: UUID, file_name: str
    ) -> Tuple[SnapshotRecord, MediaFileRecord, Path]:
        if not is_allowlisted_video_name(file_name):
            raise HTTPException(
                status_code=422,
                detail="The selected filename or video extension is not supported",
            )
        with self._lock:
            snapshot = self._snapshot(project_id, snapshot_id)
            if snapshot_id not in self._stable_snapshot_ids:
                raise HTTPException(
                    status_code=409,
                    detail="The media snapshot has not passed the three-snapshot stability check",
                )
            file_record = snapshot.files_by_name.get(file_name)
        if file_record is None:
            raise HTTPException(
                status_code=404,
                detail="The selected media file was not found in this snapshot",
            )

        candidate = Path(snapshot.directory) / file_record.name
        try:
            before_stat = os.stat(candidate, follow_symlinks=False)
        except (OSError, ValueError) as error:
            raise HTTPException(
                status_code=409,
                detail="The selected media file is no longer available",
            ) from error
        if not self._candidate_matches_record(
            candidate, file_record, before_stat
        ):
            raise HTTPException(
                status_code=409,
                detail="The selected media file changed after the stable snapshot",
            )
        return snapshot, file_record, candidate

    @staticmethod
    def _candidate_matches_record(
        candidate: Path,
        file_record: MediaFileRecord,
        stat_result: os.stat_result,
    ) -> bool:
        return (
            not candidate.is_symlink()
            and not _is_reparse_point(stat_result)
            and stat_module.S_ISREG(stat_result.st_mode)
            and _same_metadata(file_record, stat_result)
        )

    def _ensure_hash_worker_locked(self) -> None:
        if (
            self._hash_worker_thread is not None
            and self._hash_worker_thread.is_alive()
        ):
            return
        if self._hash_stop_event.is_set():
            raise HTTPException(
                status_code=503,
                detail="The content-hash worker is shutting down",
            )
        self._hash_worker_thread = threading.Thread(
            target=self._hash_worker_loop,
            name="trafficvisson-media-hash",
            daemon=True,
        )
        self._hash_worker_thread.start()

    def _hash_worker_loop(self) -> None:
        while not self._hash_stop_event.is_set():
            try:
                job_id = self._hash_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if job_id is None:
                    return
                with self._lock:
                    job = self._hash_jobs.get(job_id)
                    if job is None or job.status == "cancelled":
                        continue
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                        job.updated_at = datetime.now(timezone.utc)
                        continue
                    job.status = "running"
                    job.updated_at = datetime.now(timezone.utc)
                try:
                    self._run_hash_job(job)
                except _HashCancelled:
                    self._finish_hash_job(
                        job.job_id, status="cancelled", error_code=None
                    )
                except _HashFileChanged:
                    self._finish_hash_job(
                        job.job_id,
                        status="failed",
                        error_code="file_changed",
                    )
                except _HashReadFailed:
                    self._finish_hash_job(
                        job.job_id,
                        status="failed",
                        error_code="read_failed",
                    )
                except Exception:
                    # The local bridge returns a bounded code and never a path,
                    # traceback, or exception message to the renderer.
                    self._finish_hash_job(
                        job.job_id,
                        status="failed",
                        error_code="internal_error",
                    )
            finally:
                self._hash_queue.task_done()

    def _run_hash_job(self, job: HashJobRecord) -> None:
        if job.cancel_event.is_set():
            raise _HashCancelled()
        candidate = self._validate_hash_candidate(job)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(str(candidate), flags)
            with os.fdopen(descriptor, "rb", buffering=0) as stream:
                descriptor = None
                opened_stat = os.fstat(stream.fileno())
                if not self._candidate_matches_record(
                    candidate, job.file_record, opened_stat
                ):
                    raise _HashFileChanged()

                digest = hashlib.sha256()
                buffer = bytearray(HASH_CHUNK_SIZE_BYTES)
                buffer_view = memoryview(buffer)
                bytes_hashed = 0
                while True:
                    if job.cancel_event.is_set():
                        raise _HashCancelled()
                    count = stream.readinto(buffer)
                    if count is None:
                        raise _HashReadFailed()
                    if count == 0:
                        break
                    next_bytes_hashed = bytes_hashed + count
                    if next_bytes_hashed > job.file_record.size_bytes:
                        raise _HashFileChanged()
                    digest.update(buffer_view[:count])
                    bytes_hashed = next_bytes_hashed
                    self._update_hash_progress(job.job_id, bytes_hashed)
                if bytes_hashed != job.file_record.size_bytes:
                    raise _HashFileChanged()
        except _HashCancelled:
            raise
        except _HashFileChanged:
            raise
        except OSError as error:
            raise _HashReadFailed() from error
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        if job.cancel_event.is_set():
            raise _HashCancelled()
        self._validate_hash_candidate(job)
        if job.cancel_event.is_set():
            raise _HashCancelled()
        digest_lower = digest.hexdigest()
        self._finish_hash_job(
            job.job_id,
            status="completed",
            error_code=None,
            sha256_lower=digest_lower,
        )

    def _validate_hash_candidate(self, job: HashJobRecord) -> Path:
        candidate = Path(job.snapshot.directory) / job.file_record.name
        try:
            stat_result = os.stat(candidate, follow_symlinks=False)
        except (OSError, ValueError) as error:
            raise _HashFileChanged() from error
        if not self._candidate_matches_record(
            candidate, job.file_record, stat_result
        ):
            raise _HashFileChanged()
        return candidate

    def _update_hash_progress(self, job_id: UUID, bytes_hashed: int) -> None:
        with self._lock:
            job = self._hash_jobs.get(job_id)
            if job is None:
                raise _HashReadFailed()
            job.bytes_hashed = bytes_hashed
            job.updated_at = datetime.now(timezone.utc)

    def _finish_hash_job(
        self,
        job_id: UUID,
        *,
        status: str,
        error_code: Optional[str],
        sha256_lower: Optional[str] = None,
    ) -> None:
        with self._lock:
            job = self._hash_jobs.get(job_id)
            if job is None:
                return
            if job.cancel_event.is_set() or job.status == "cancelled":
                job.status = "cancelled"
                job.sha256_lower = None
                job.sha256_upper = None
                job.error_code = None
            else:
                job.status = status
                job.error_code = error_code
                job.sha256_lower = sha256_lower
                job.sha256_upper = (
                    sha256_lower.upper() if sha256_lower is not None else None
                )
            job.updated_at = datetime.now(timezone.utc)

    def _hash_job(self, project_id: UUID, job_id: UUID) -> HashJobRecord:
        job = self._hash_jobs.get(job_id)
        if job is None or job.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail="The content-hash job was not found in this project",
            )
        return job

    def _evict_terminal_hash_jobs_locked(self) -> None:
        if len(self._hash_jobs) < MAX_HASH_JOB_RECORDS:
            return
        for job_id, job in list(self._hash_jobs.items()):
            if job.status in _TERMINAL_HASH_STATES:
                self._hash_jobs.pop(job_id, None)
                if len(self._hash_jobs) < MAX_HASH_JOB_RECORDS:
                    return

    @staticmethod
    def _serialize_hash_job(job: HashJobRecord) -> Dict:
        if job.status == "completed":
            content_hash_status = "computed"
        elif job.status in {"cancelled", "failed"}:
            content_hash_status = "not_available"
        else:
            content_hash_status = "pending"
        return {
            "job_id": job.job_id,
            "snapshot_id": job.snapshot.snapshot_id,
            "file_name": job.file_record.name,
            "status": job.status,
            "hash_algorithm": "sha256",
            "chunk_size_bytes": HASH_CHUNK_SIZE_BYTES,
            "bytes_hashed": job.bytes_hashed,
            "total_bytes": job.file_record.size_bytes,
            "sha256_lower": job.sha256_lower,
            "sha256_upper": job.sha256_upper,
            "content_hash_status": content_hash_status,
            "error_code": job.error_code,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }

    def _snapshot(self, project_id: UUID, snapshot_id: UUID) -> SnapshotRecord:
        record = self._snapshots.get(snapshot_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail="The media snapshot was not found in this project",
            )
        return record

    def _resolve_ffprobe(self) -> str:
        candidate = self._configured_ffprobe_path or shutil.which("ffprobe")
        if not candidate:
            raise HTTPException(
                status_code=503,
                detail="ffprobe is not installed or configured",
            )
        resolved = Path(candidate)
        if not resolved.is_absolute():
            found = shutil.which(candidate)
            if not found:
                raise HTTPException(
                    status_code=503,
                    detail="ffprobe is not installed or configured",
                )
            resolved = Path(found)
        if resolved.name.casefold() not in {"ffprobe", "ffprobe.exe"}:
            raise HTTPException(
                status_code=503,
                detail="The configured media probe executable is invalid",
            )
        if not resolved.is_file():
            raise HTTPException(
                status_code=503,
                detail="ffprobe is not installed or configured",
            )
        return str(resolved)

    def _validate_probe_output(
        self, raw_probe: object, file_record: MediaFileRecord
    ) -> Dict:
        if not isinstance(raw_probe, dict):
            raise ValueError("ffprobe root must be an object")
        raw_format = raw_probe.get("format")
        raw_streams = raw_probe.get("streams")
        if not isinstance(raw_format, dict) or not isinstance(raw_streams, list):
            raise ValueError("ffprobe format and streams are required")
        if not 1 <= len(raw_streams) <= MAX_FFPROBE_STREAMS:
            raise ValueError("ffprobe stream count is invalid")

        reported_size = _safe_positive_int(
            raw_format.get("size"), field_name="size"
        )
        if reported_size != file_record.size_bytes:
            raise ValueError("ffprobe size does not match the stable file")

        duration_candidates: List[float] = []
        if raw_format.get("duration") is not None:
            duration_candidates.append(
                _safe_float(raw_format.get("duration"), field_name="duration")
            )
        video_streams = []
        for raw_stream in raw_streams:
            if not isinstance(raw_stream, dict):
                raise ValueError("ffprobe stream is invalid")
            if raw_stream.get("codec_type") != "video":
                continue
            codec_name = raw_stream.get("codec_name")
            if (
                not isinstance(codec_name, str)
                or not _SAFE_CODEC_PATTERN.fullmatch(codec_name)
            ):
                raise ValueError("ffprobe video codec is invalid")
            width = _safe_positive_int(
                raw_stream.get("width"), field_name="video width"
            )
            height = _safe_positive_int(
                raw_stream.get("height"), field_name="video height"
            )
            if width > MAX_VIDEO_DIMENSION or height > MAX_VIDEO_DIMENSION:
                raise ValueError("ffprobe video dimensions are invalid")
            if raw_stream.get("duration") is not None:
                duration_candidates.append(
                    _safe_float(
                        raw_stream.get("duration"),
                        field_name="stream duration",
                    )
                )
            frame_rate = raw_stream.get("avg_frame_rate") or raw_stream.get(
                "r_frame_rate"
            )
            if not isinstance(frame_rate, str) or not _SAFE_RATE_PATTERN.fullmatch(
                frame_rate
            ):
                raise ValueError("ffprobe frame rate is invalid")
            numerator, denominator = (int(item) for item in frame_rate.split("/"))
            if numerator <= 0 or denominator <= 0:
                raise ValueError("ffprobe frame rate is invalid")
            raw_stream_index = raw_stream.get("index", 0)
            if isinstance(raw_stream_index, bool) or (
                isinstance(raw_stream_index, str)
                and not _SAFE_UNSIGNED_INTEGER_PATTERN.fullmatch(
                    raw_stream_index
                )
            ):
                raise ValueError("ffprobe stream index is invalid")
            try:
                stream_index = int(raw_stream_index)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("ffprobe stream index is invalid") from error
            if not 0 <= stream_index < MAX_FFPROBE_STREAMS:
                raise ValueError("ffprobe stream index is invalid")
            video_streams.append(
                {
                    "stream_index": stream_index,
                    "codec_name": codec_name,
                    "width": width,
                    "height": height,
                    "frame_rate": frame_rate,
                }
            )
        if not video_streams:
            raise ValueError("ffprobe did not find a video stream")

        if not duration_candidates or any(
            value <= 0 or value > MAX_MEDIA_DURATION_SECONDS
            for value in duration_candidates
        ):
            raise ValueError("ffprobe duration is invalid")
        duration_seconds = max(duration_candidates)

        format_name = raw_format.get("format_name")
        if (
            not isinstance(format_name, str)
            or not _SAFE_FORMAT_PATTERN.fullmatch(format_name)
        ):
            raise ValueError("ffprobe format name is invalid")
        format_names = list(dict.fromkeys(format_name.split(",")))
        if (
            not format_names
            or len(format_names) > 16
            or any(
                not name or not _SAFE_CODEC_PATTERN.fullmatch(name)
                for name in format_names
            )
        ):
            raise ValueError("ffprobe format name is invalid")

        return {
            "duration_seconds": duration_seconds,
            "format_names": format_names,
            "video_streams": video_streams,
        }
