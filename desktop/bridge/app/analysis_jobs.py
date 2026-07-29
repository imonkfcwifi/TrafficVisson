"""Single-worker orchestration for hash-authorized candidate extraction."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional
from uuid import UUID, uuid4

from fastapi import HTTPException

from .candidate_extraction import (
    CandidateExtractionCancelled,
    CandidateExtractionEngine,
    CandidateExtractionError,
    CandidateExtractionOutputLimit,
    CandidateExtractionTimeout,
    ExtractionLimits,
    ExtractionResult,
)
from .media_intake import (
    CompletedMediaGrant,
    MediaIntakeService,
    is_allowlisted_video_name,
)
from .vision_inference import (
    LocalVisionInferenceService,
    VisionInferenceCancelled,
)


MAX_ANALYSIS_JOB_RECORDS = 128
MAX_QUEUED_ANALYSIS_JOBS = 16
MAX_PERSISTED_JOB_RECORD_BYTES = 16 * 1024 * 1024
MAX_PERSISTED_CANDIDATES = 2_000
PERSISTED_JOB_SCHEMA = "trafficvisson.analysis-job.v1"
_ACTIVE_STATES = frozenset({"queued", "running", "cancelling"})
_TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})
_CANDIDATE_ID_PATTERN = re.compile(r"^[a-f0-9]{20}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_ARTIFACT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_ISO_6709_LOCATION_PATTERN = re.compile(
    r"^[+-][0-9]{2}(?:\.[0-9]{1,12})?"
    r"[+-][0-9]{3}(?:\.[0-9]{1,12})?"
    r"(?:[+-][0-9]{1,6}(?:\.[0-9]{1,6})?)?/$"
)
_ARTIFACT_KINDS = {
    "image": ("representative_image", "image/jpeg", ".jpg"),
    "clip": ("preview_clip", "video/mp4", ".mp4"),
}


@dataclass
class AnalysisJobRecord:
    job_id: UUID
    grant: CompletedMediaGrant
    status: str
    stage: str
    error_code: Optional[str]
    result: Optional[Dict]
    created_at: datetime
    updated_at: datetime
    cancel_event: threading.Event
    artifact_directory: Optional[Path]


@dataclass(frozen=True)
class ResolvedAnalysisArtifact:
    """A bridge-internal, verified artifact handle.

    The filesystem path is intentionally never included in JSON contracts.
    """

    path: Path
    media_type: str
    size_bytes: int
    sha256: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: object) -> str:
    if isinstance(value, (UUID, datetime)):
        return str(value)
    raise TypeError(f"Unsupported persisted analysis value: {type(value).__name__}")


def _parse_utc_datetime(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("analysis record datetime is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("analysis record datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in {float("inf"), float("-inf")}


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


class AnalysisJobService:
    """Runs at most one candidate extractor against bridge-resolved media."""

    def __init__(
        self,
        media_intake: MediaIntakeService,
        *,
        data_root: Path,
        ffmpeg_path: Optional[str] = None,
        ffprobe_path: Optional[str] = None,
        extraction_limits: Optional[ExtractionLimits] = None,
        vision_inference: Optional[LocalVisionInferenceService] = None,
    ) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("analysis data_root must be absolute")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or _is_reparse_point(root) or not root.is_dir():
            raise ValueError("analysis data_root must be a real directory")
        self._data_root = root.resolve()
        self._projects_root = self._data_root / "projects"
        self._projects_root.mkdir(parents=True, exist_ok=True)
        if (
            self._projects_root.is_symlink()
            or _is_reparse_point(self._projects_root)
            or not self._projects_root.is_dir()
        ):
            raise ValueError("analysis projects root must be a real directory")
        self._media_intake = media_intake
        self._ffmpeg_path = ffmpeg_path
        self._ffprobe_path = ffprobe_path
        self._limits = extraction_limits
        self._vision_inference = vision_inference or LocalVisionInferenceService(
            model_pack_path=None
        )
        self._jobs: "OrderedDict[UUID, AnalysisJobRecord]" = OrderedDict()
        self._queue: "queue.Queue[Optional[UUID]]" = queue.Queue(
            maxsize=MAX_QUEUED_ANALYSIS_JOBS
        )
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._load_completed_records()

    def start(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        file_name: str,
        hash_job_id: UUID,
        sha256_lower: str,
    ) -> Dict:
        # This is the only source-path resolution point.  The request contains
        # opaque identities and a digest, never a filesystem path.
        grant = self._media_intake.resolve_completed_media(
            project_id,
            snapshot_id,
            file_name,
            hash_job_id,
            sha256_lower,
        )
        with self._lock:
            if self._stop_event.is_set():
                raise HTTPException(
                    status_code=503,
                    detail="The candidate-extraction worker is shutting down",
                )
            for existing in self._jobs.values():
                if (
                    existing.grant.project_id == project_id
                    and existing.grant.snapshot_id == snapshot_id
                    and existing.grant.file_name == file_name
                    and existing.status in _ACTIVE_STATES
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A candidate-extraction job is already active for "
                            "this media file"
                        ),
                    )
            self._evict_terminal_locked()
            if len(self._jobs) >= MAX_ANALYSIS_JOB_RECORDS:
                raise HTTPException(
                    status_code=503,
                    detail="The candidate-extraction job registry is full",
                )
            now = _utc_now()
            job = AnalysisJobRecord(
                job_id=uuid4(),
                grant=grant,
                status="queued",
                stage="queued",
                error_code=None,
                result=None,
                created_at=now,
                updated_at=now,
                cancel_event=threading.Event(),
                artifact_directory=None,
            )
            self._jobs[job.job_id] = job
            self._ensure_worker_locked()
            try:
                self._queue.put_nowait(job.job_id)
            except queue.Full as error:
                self._jobs.pop(job.job_id, None)
                raise HTTPException(
                    status_code=503,
                    detail="The candidate-extraction queue is full",
                ) from error
            return self._serialize(job)

    def get(self, project_id: UUID, job_id: UUID) -> Dict:
        with self._lock:
            return self._serialize(self._job(project_id, job_id))

    def list(self, project_id: UUID, *, limit: int = 100) -> List[Dict]:
        if isinstance(limit, bool) or not 1 <= limit <= MAX_ANALYSIS_JOB_RECORDS:
            raise ValueError("analysis job list limit is invalid")
        with self._lock:
            jobs = [
                self._serialize(job)
                for job in reversed(tuple(self._jobs.values()))
                if job.grant.project_id == project_id
            ]
        return jobs[:limit]

    def latest_completed(self, project_id: UUID) -> Optional[Dict]:
        """Return the newest completed result for one project without paths."""
        with self._lock:
            for job in reversed(tuple(self._jobs.values())):
                if (
                    job.grant.project_id == project_id
                    and job.status == "completed"
                    and job.result is not None
                ):
                    return self._serialize(job)
        return None

    def cancel(self, project_id: UUID, job_id: UUID) -> Dict:
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

    def pause(self, project_id: UUID, job_id: UUID) -> None:
        with self._lock:
            self._job(project_id, job_id)
        raise HTTPException(
            status_code=409,
            detail=(
                "Pause is not supported for local candidate extraction; "
                "cancel the job and start a new run"
            ),
        )

    def resolve_artifact(
        self,
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        kind: str,
    ) -> ResolvedAnalysisArtifact:
        """Resolve and re-verify one artifact from a completed analysis job."""
        if not _CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
            raise HTTPException(
                status_code=404,
                detail="The candidate evidence artifact was not found",
            )
        kind_contract = _ARTIFACT_KINDS.get(kind)
        if kind_contract is None:
            raise HTTPException(
                status_code=404,
                detail="The candidate evidence artifact was not found",
            )

        with self._lock:
            job = self._job(project_id, job_id)
            if (
                job.status != "completed"
                or job.result is None
                or job.artifact_directory is None
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Candidate evidence is available only after analysis completes",
                )
            candidate = next(
                (
                    item
                    for item in job.result.get("candidates", ())
                    if item.get("candidate_id") == candidate_id
                ),
                None,
            )
            if candidate is None:
                raise HTTPException(
                    status_code=404,
                    detail="The candidate evidence artifact was not found",
                )
            metadata_key, expected_media_type, expected_suffix = kind_contract
            metadata = candidate.get(metadata_key)
            artifact_directory = job.artifact_directory

        if not isinstance(metadata, dict):
            raise HTTPException(
                status_code=410,
                detail="The candidate evidence artifact is no longer valid",
            )
        relative_path = metadata.get("relative_path")
        size_bytes = metadata.get("size_bytes")
        sha256_lower = metadata.get("sha256")
        media_type = metadata.get("media_type")
        if (
            not isinstance(relative_path, str)
            or not _ARTIFACT_NAME_PATTERN.fullmatch(relative_path)
            or Path(relative_path).suffix.casefold() != expected_suffix
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes <= 0
            or not isinstance(sha256_lower, str)
            or not re.fullmatch(r"[a-f0-9]{64}", sha256_lower)
            or media_type != expected_media_type
        ):
            raise HTTPException(
                status_code=410,
                detail="The candidate evidence artifact is no longer valid",
            )

        try:
            output_root = self._project_output_root(project_id)
            verified_root = artifact_directory.resolve(strict=True)
            candidate_path = verified_root / relative_path
            verified_path = candidate_path.resolve(strict=True)
            before = candidate_path.stat()
        except (OSError, CandidateExtractionError) as error:
            raise HTTPException(
                status_code=410,
                detail="The candidate evidence artifact is no longer available",
            ) from error
        if (
            verified_root.parent != output_root
            or artifact_directory.is_symlink()
            or _is_reparse_point(artifact_directory)
            or verified_path.parent != verified_root
            or candidate_path.is_symlink()
            or _is_reparse_point(candidate_path)
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_size) != size_bytes
        ):
            raise HTTPException(
                status_code=410,
                detail="The candidate evidence artifact is no longer valid",
            )

        digest = hashlib.sha256()
        try:
            with verified_path.open("rb") as handle:
                header = handle.read(32)
                digest.update(header)
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            after = verified_path.stat()
        except OSError as error:
            raise HTTPException(
                status_code=410,
                detail="The candidate evidence artifact is no longer available",
            ) from error
        if (
            (expected_media_type == "image/jpeg" and not header.startswith(b"\xff\xd8\xff"))
            or (expected_media_type == "video/mp4" and b"ftyp" not in header)
            or digest.hexdigest() != sha256_lower
            or int(after.st_size) != int(before.st_size)
            or int(after.st_mtime_ns) != int(before.st_mtime_ns)
        ):
            raise HTTPException(
                status_code=410,
                detail="The candidate evidence artifact failed integrity verification",
            )
        return ResolvedAnalysisArtifact(
            path=verified_path,
            media_type=expected_media_type,
            size_bytes=size_bytes,
            sha256=sha256_lower,
        )

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

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="trafficvisson-candidate-extraction",
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
                    job.stage = "validating_source"
                    job.updated_at = _utc_now()
                self._run(job)
            finally:
                self._queue.task_done()

    def _run(self, job: AnalysisJobRecord) -> None:
        try:
            # Re-resolve immediately before tool execution so a stale grant
            # cannot outlive a changed file.
            grant = self._media_intake.resolve_completed_media(
                job.grant.project_id,
                job.grant.snapshot_id,
                job.grant.file_name,
                job.grant.hash_job_id,
                job.grant.sha256_lower,
            )
            if job.cancel_event.is_set():
                raise CandidateExtractionCancelled(
                    "Candidate extraction was cancelled"
                )
            output_root = self._project_output_root(grant.project_id)
            with self._lock:
                if job.status == "running":
                    job.stage = "extracting_candidates"
                    job.updated_at = _utc_now()
            engine = CandidateExtractionEngine(
                ffmpeg_path=self._ffmpeg_path,
                ffprobe_path=self._ffprobe_path,
                limits=self._limits,
            )
            result = engine.extract(
                grant.media_path,
                output_root,
                source_sha256=grant.sha256_lower,
                cancel=job.cancel_event.is_set,
            )
            artifact_directory = self._validated_run_directory(
                result.output_directory, output_root
            )
            sanitized = self._sanitize_result(
                job.job_id, result, artifact_directory
            )
            inference_by_candidate, inference_summary = (
                self._vision_inference.infer_candidates(
                    sanitized["candidates"],
                    artifact_directory,
                    cancel=job.cancel_event.is_set,
                )
            )
            for candidate in sanitized["candidates"]:
                candidate["ai_inference"] = inference_by_candidate[
                    candidate["candidate_id"]
                ]
            sanitized["vision_inference"] = inference_summary
            with self._lock:
                current = self._jobs.get(job.job_id)
                if current is None:
                    return
                if current.cancel_event.is_set():
                    current.status = "cancelled"
                    current.stage = "cancelled"
                    current.result = None
                    current.artifact_directory = None
                else:
                    completed_at = _utc_now()
                    self._persist_completed_record(
                        current,
                        sanitized,
                        artifact_directory,
                        completed_at,
                    )
                    current.status = "completed"
                    current.stage = "completed"
                    current.result = sanitized
                    current.artifact_directory = artifact_directory
                    current.updated_at = completed_at
                if current.status == "cancelled":
                    current.updated_at = _utc_now()
        except CandidateExtractionCancelled:
            self._finish_failure(job.job_id, "cancelled", None)
        except VisionInferenceCancelled:
            self._finish_failure(job.job_id, "cancelled", None)
        except CandidateExtractionTimeout:
            self._finish_failure(job.job_id, "failed", "timeout")
        except CandidateExtractionOutputLimit:
            self._finish_failure(job.job_id, "failed", "output_limit")
        except CandidateExtractionError:
            self._finish_failure(job.job_id, "failed", "extraction_failed")
        except HTTPException:
            self._finish_failure(job.job_id, "failed", "source_changed")
        except Exception:
            self._finish_failure(job.job_id, "failed", "internal_error")

    def _finish_failure(
        self, job_id: UUID, status_value: str, error_code: Optional[str]
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status_value
            job.stage = status_value
            job.error_code = error_code
            job.result = None
            job.artifact_directory = None
            job.updated_at = _utc_now()

    def _project_output_root(self, project_id: UUID) -> Path:
        project_root = self._projects_root / str(project_id)
        output_root = project_root / "candidate-extractions"
        output_root.mkdir(parents=True, exist_ok=True)
        expected_parent = self._projects_root.resolve()
        if (
            project_root.parent.resolve() != expected_parent
            or project_root.is_symlink()
            or output_root.is_symlink()
            or _is_reparse_point(project_root)
            or _is_reparse_point(output_root)
            or not output_root.is_dir()
        ):
            raise CandidateExtractionError(
                "The project candidate output root is unavailable"
            )
        return output_root.resolve()

    def _project_records_root(
        self, project_id: UUID, *, create: bool
    ) -> Path:
        project_root = self._projects_root / str(project_id)
        records_root = project_root / "analysis-job-records"
        if create:
            records_root.mkdir(parents=True, exist_ok=True)
        try:
            resolved_projects = self._projects_root.resolve(strict=True)
            resolved_project = project_root.resolve(strict=True)
            resolved_records = records_root.resolve(strict=True)
        except OSError as error:
            raise CandidateExtractionError(
                "The analysis job record directory is unavailable"
            ) from error
        if (
            resolved_project.parent != resolved_projects
            or resolved_records.parent != resolved_project
            or project_root.is_symlink()
            or records_root.is_symlink()
            or _is_reparse_point(project_root)
            or _is_reparse_point(records_root)
            or not records_root.is_dir()
        ):
            raise CandidateExtractionError(
                "The analysis job record directory is invalid"
            )
        return resolved_records

    def _persist_completed_record(
        self,
        job: AnalysisJobRecord,
        result: Dict,
        artifact_directory: Path,
        completed_at: datetime,
    ) -> None:
        records_root = self._project_records_root(
            job.grant.project_id, create=True
        )
        payload = {
            "schema_version": PERSISTED_JOB_SCHEMA,
            "job_id": job.job_id,
            "project_id": job.grant.project_id,
            "snapshot_id": job.grant.snapshot_id,
            "hash_job_id": job.grant.hash_job_id,
            "file_name": job.grant.file_name,
            "source_size_bytes": job.grant.size_bytes,
            "source_modified_time_ns": job.grant.modified_time_ns,
            "source_sha256": job.grant.sha256_lower,
            "status": "completed",
            "created_at": job.created_at,
            "updated_at": completed_at,
            "artifact_directory_name": artifact_directory.name,
            "result": result,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_json_default,
        ).encode("utf-8")
        if len(encoded) > MAX_PERSISTED_JOB_RECORD_BYTES:
            raise CandidateExtractionError(
                "The analysis result record is too large to persist"
            )

        record_path = records_root / f"{job.job_id}.json"
        digest_path = records_root / f"{job.job_id}.sha256"
        temporary_path = records_root / (
            f".{job.job_id}.{uuid4().hex}.tmp"
        )
        temporary_digest_path = records_root / (
            f".{job.job_id}.{uuid4().hex}.sha256.tmp"
        )
        descriptor: Optional[int] = None
        digest_descriptor: Optional[int] = None
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            digest_descriptor = os.open(
                temporary_digest_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(digest_descriptor, "wb") as digest_stream:
                digest_descriptor = None
                digest_stream.write(hashlib.sha256(encoded).hexdigest().encode("ascii"))
                digest_stream.flush()
                os.fsync(digest_stream.fileno())
            os.replace(temporary_path, record_path)
            os.replace(temporary_digest_path, digest_path)
        except OSError as error:
            raise CandidateExtractionError(
                "The completed analysis result could not be persisted"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if digest_descriptor is not None:
                os.close(digest_descriptor)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                temporary_digest_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _load_completed_records(self) -> None:
        record_candidates: List[tuple[int, Path, UUID]] = []
        scanned_records = 0
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
            records_root = Path(project_entry.path) / "analysis-job-records"
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
                    scanned_records += 1
                    if scanned_records > MAX_ANALYSIS_JOB_RECORDS * 32:
                        break
                    if (
                        not entry.name.endswith(".json")
                        or entry.is_symlink()
                        or not entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    try:
                        stat_result = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if (
                        stat_result.st_size <= 0
                        or stat_result.st_size > MAX_PERSISTED_JOB_RECORD_BYTES
                    ):
                        continue
                    record_candidates.append(
                        (
                            int(stat_result.st_mtime_ns),
                            Path(entry.path),
                            project_id,
                        )
                    )
            if scanned_records > MAX_ANALYSIS_JOB_RECORDS * 32:
                break

        record_candidates.sort(key=lambda item: item[0], reverse=True)
        loaded: List[AnalysisJobRecord] = []
        for _, record_path, project_id in record_candidates:
            if len(loaded) >= MAX_ANALYSIS_JOB_RECORDS:
                break
            try:
                loaded.append(
                    self._load_completed_record(record_path, project_id)
                )
            except (OSError, ValueError, CandidateExtractionError):
                continue
        loaded.sort(key=lambda item: item.updated_at)
        for job in loaded:
            self._jobs[job.job_id] = job

    def _load_completed_record(
        self, record_path: Path, expected_project_id: UUID
    ) -> AnalysisJobRecord:
        if (
            record_path.is_symlink()
            or _is_reparse_point(record_path)
            or not record_path.is_file()
        ):
            raise ValueError("analysis record is not a regular file")
        raw = record_path.read_bytes()
        if not raw or len(raw) > MAX_PERSISTED_JOB_RECORD_BYTES:
            raise ValueError("analysis record size is invalid")
        digest_path = record_path.with_suffix(".sha256")
        if (
            digest_path.is_symlink()
            or _is_reparse_point(digest_path)
            or not digest_path.is_file()
        ):
            raise ValueError("analysis record digest is unavailable")
        expected_digest = digest_path.read_text(
            encoding="ascii", errors="strict"
        ).strip()
        if (
            not _SHA256_PATTERN.fullmatch(expected_digest)
            or hashlib.sha256(raw).hexdigest() != expected_digest
        ):
            raise ValueError("analysis record digest does not match")

        def reject_constant(_: str) -> object:
            raise ValueError("analysis record contains a non-finite number")

        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=reject_constant,
        )
        if not isinstance(decoded, Mapping):
            raise ValueError("analysis record must be an object")
        if (
            decoded.get("schema_version") != PERSISTED_JOB_SCHEMA
            or decoded.get("status") != "completed"
        ):
            raise ValueError("analysis record schema is invalid")
        project_id = UUID(str(decoded.get("project_id")))
        job_id = UUID(str(decoded.get("job_id")))
        snapshot_id = UUID(str(decoded.get("snapshot_id")))
        hash_job_id = UUID(str(decoded.get("hash_job_id")))
        if (
            project_id != expected_project_id
            or record_path.name != f"{job_id}.json"
        ):
            raise ValueError("analysis record identity is invalid")
        file_name = decoded.get("file_name")
        sha256_lower = decoded.get("source_sha256")
        size_bytes = decoded.get("source_size_bytes")
        modified_time_ns = decoded.get("source_modified_time_ns")
        if (
            not isinstance(file_name, str)
            or not is_allowlisted_video_name(file_name)
            or not isinstance(sha256_lower, str)
            or not _SHA256_PATTERN.fullmatch(sha256_lower)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes <= 0
            or isinstance(modified_time_ns, bool)
            or not isinstance(modified_time_ns, int)
            or modified_time_ns < 0
        ):
            raise ValueError("analysis source provenance is invalid")
        created_at = _parse_utc_datetime(decoded.get("created_at"))
        updated_at = _parse_utc_datetime(decoded.get("updated_at"))
        if updated_at < created_at:
            raise ValueError("analysis record timestamps are invalid")
        artifact_directory_name = decoded.get("artifact_directory_name")
        if (
            not isinstance(artifact_directory_name, str)
            or not artifact_directory_name.startswith(
                "candidate-extraction-"
            )
            or not _ARTIFACT_NAME_PATTERN.fullmatch(
                artifact_directory_name
            )
        ):
            raise ValueError("analysis artifact directory identity is invalid")
        output_root = self._project_output_root(project_id)
        artifact_directory = self._validated_run_directory(
            str(output_root / artifact_directory_name), output_root
        )
        result = self._validate_persisted_result(
            decoded.get("result"), job_id
        )
        grant = CompletedMediaGrant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            hash_job_id=hash_job_id,
            file_name=file_name,
            media_path=artifact_directory / ".source-not-retained",
            size_bytes=size_bytes,
            modified_time_ns=modified_time_ns,
            sha256_lower=sha256_lower,
        )
        return AnalysisJobRecord(
            job_id=job_id,
            grant=grant,
            status="completed",
            stage="completed",
            error_code=None,
            result=result,
            created_at=created_at,
            updated_at=updated_at,
            cancel_event=threading.Event(),
            artifact_directory=artifact_directory,
        )

    @staticmethod
    def _validate_persisted_result(
        raw_result: object, job_id: UUID
    ) -> Dict:
        if not isinstance(raw_result, dict):
            raise ValueError("analysis result is invalid")
        if "output_directory" in raw_result:
            raise ValueError("analysis result contains a forbidden path")
        if str(raw_result.get("artifact_set_id")) != str(job_id):
            raise ValueError("analysis result identity is invalid")
        raw_candidates = raw_result.get("candidates")
        if (
            not isinstance(raw_candidates, list)
            or len(raw_candidates) > MAX_PERSISTED_CANDIDATES
            or raw_result.get("candidate_count") != len(raw_candidates)
            or raw_result.get("artifact_count") != len(raw_candidates) * 2
            or not isinstance(raw_result.get("total_artifact_bytes"), int)
            or raw_result.get("total_artifact_bytes", -1) < 0
            or not isinstance(raw_result.get("provenance"), dict)
        ):
            raise ValueError("analysis result summary is invalid")
        provenance = raw_result["provenance"]
        source_location = provenance.get("source_location")
        if source_location is not None:
            if (
                not isinstance(source_location, dict)
                or set(source_location)
                != {
                    "latitude",
                    "longitude",
                    "altitude_meters",
                    "raw_iso6709",
                    "scope",
                }
                or not _is_finite_number(source_location.get("latitude"))
                or not -90 <= source_location["latitude"] <= 90
                or not _is_finite_number(source_location.get("longitude"))
                or not -180 <= source_location["longitude"] <= 180
                or (
                    source_location["latitude"] == 0
                    and source_location["longitude"] == 0
                )
                or (
                    source_location.get("altitude_meters") is not None
                    and (
                        not _is_finite_number(
                            source_location.get("altitude_meters")
                        )
                        or not -20_000
                        <= source_location["altitude_meters"]
                        <= 20_000
                    )
                )
                or not isinstance(source_location.get("raw_iso6709"), str)
                or not _ISO_6709_LOCATION_PATTERN.fullmatch(
                    source_location["raw_iso6709"]
                )
                or source_location.get("scope") != "capture_start_only"
            ):
                raise ValueError("analysis source location is invalid")
        candidate_ids: set[str] = set()
        total_bytes = 0
        for candidate in raw_candidates:
            if not isinstance(candidate, dict):
                raise ValueError("analysis candidate is invalid")
            candidate_id = candidate.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or not _CANDIDATE_ID_PATTERN.fullmatch(candidate_id)
                or candidate_id in candidate_ids
            ):
                raise ValueError("analysis candidate identity is invalid")
            candidate_ids.add(candidate_id)
            for numeric_field in (
                "start_seconds",
                "end_seconds",
                "representative_timestamp_seconds",
                "scene_score",
                "sharpness_score",
                "preview_start_seconds",
                "preview_duration_seconds",
            ):
                if not _is_finite_number(candidate.get(numeric_field)):
                    raise ValueError("analysis candidate timing is invalid")
            if (
                candidate["start_seconds"] < 0
                or candidate["end_seconds"] < candidate["start_seconds"]
                or not 0 <= candidate["scene_score"] <= 1
                or not 0 <= candidate["sharpness_score"] <= 1
            ):
                raise ValueError("analysis candidate values are invalid")
            for kind, contract in _ARTIFACT_KINDS.items():
                artifact = candidate.get(contract[0])
                if not isinstance(artifact, dict):
                    raise ValueError("analysis artifact metadata is invalid")
                relative_path = artifact.get("relative_path")
                size = artifact.get("size_bytes")
                sha256 = artifact.get("sha256")
                if (
                    not isinstance(relative_path, str)
                    or not _ARTIFACT_NAME_PATTERN.fullmatch(relative_path)
                    or Path(relative_path).suffix.casefold() != contract[2]
                    or artifact.get("media_type") != contract[1]
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or size <= 0
                    or not isinstance(sha256, str)
                    or not _SHA256_PATTERN.fullmatch(sha256)
                    or artifact.get("artifact_ref")
                    != f"analysis:{job_id}:{relative_path}"
                ):
                    raise ValueError("analysis artifact metadata is invalid")
                total_bytes += size
        if total_bytes != raw_result["total_artifact_bytes"]:
            raise ValueError("analysis artifact size summary is invalid")
        return raw_result

    @staticmethod
    def _validated_run_directory(
        output_directory: str, output_root: Path
    ) -> Path:
        run_directory = Path(output_directory)
        try:
            resolved = run_directory.resolve(strict=True)
        except OSError as error:
            raise CandidateExtractionError(
                "The extractor returned an unavailable output directory"
            ) from error
        if (
            resolved.parent != output_root.resolve()
            or run_directory.is_symlink()
            or _is_reparse_point(run_directory)
            or not run_directory.name.startswith("candidate-extraction-")
            or not run_directory.is_dir()
        ):
            raise CandidateExtractionError(
                "The extractor returned an invalid output directory"
            )
        return resolved

    @staticmethod
    def _sanitize_result(
        job_id: UUID, result: ExtractionResult, run_directory: Path
    ) -> Dict:
        candidates = []
        artifact_count = 0
        total_artifact_bytes = 0
        for candidate in result.candidates:
            serialized = {
                "candidate_id": candidate.candidate_id,
                "start_seconds": candidate.start_seconds,
                "end_seconds": candidate.end_seconds,
                "representative_timestamp_seconds": (
                    candidate.representative_timestamp_seconds
                ),
                "scene_score": candidate.scene_score,
                "sharpness_score": candidate.sharpness_score,
                "triggers": list(candidate.triggers),
                "observations": [
                    {
                        "timestamp_seconds": item.timestamp_seconds,
                        "scene_score": item.scene_score,
                        "sharpness_score": item.sharpness_score,
                        "triggers": list(item.triggers),
                    }
                    for item in candidate.observations
                ],
                "representative_image": {
                    **candidate.representative_image.__dict__,
                    "artifact_ref": (
                        f"analysis:{job_id}:"
                        f"{candidate.representative_image.relative_path}"
                    ),
                },
                "preview_clip": {
                    **candidate.preview_clip.__dict__,
                    "artifact_ref": (
                        f"analysis:{job_id}:"
                        f"{candidate.preview_clip.relative_path}"
                    ),
                },
                "preview_start_seconds": candidate.preview_start_seconds,
                "preview_duration_seconds": candidate.preview_duration_seconds,
            }
            for artifact in (
                candidate.representative_image,
                candidate.preview_clip,
            ):
                artifact_count += 1
                total_artifact_bytes += artifact.size_bytes
            candidates.append(serialized)
        provenance = {
            **result.provenance.__dict__,
            "format_names": list(result.provenance.format_names),
            "limits": dict(result.provenance.limits),
            "source_location": (
                dict(result.provenance.source_location.__dict__)
                if result.provenance.source_location is not None
                else None
            ),
        }
        return {
            "artifact_set_id": job_id,
            "candidate_count": len(candidates),
            "artifact_count": artifact_count,
            "total_artifact_bytes": total_artifact_bytes,
            "candidates": candidates,
            "provenance": provenance,
        }

    def _job(self, project_id: UUID, job_id: UUID) -> AnalysisJobRecord:
        job = self._jobs.get(job_id)
        if job is None or job.grant.project_id != project_id:
            raise HTTPException(
                status_code=404,
                detail="The candidate-extraction job was not found in this project",
            )
        return job

    def _evict_terminal_locked(self) -> None:
        if len(self._jobs) < MAX_ANALYSIS_JOB_RECORDS:
            return
        for job_id, job in list(self._jobs.items()):
            if job.status in _TERMINAL_STATES:
                self._jobs.pop(job_id, None)
                if len(self._jobs) < MAX_ANALYSIS_JOB_RECORDS:
                    return

    @staticmethod
    def _serialize(job: AnalysisJobRecord) -> Dict:
        response = {
            "job_id": job.job_id,
            "project_id": job.grant.project_id,
            "snapshot_id": job.grant.snapshot_id,
            "hash_job_id": job.grant.hash_job_id,
            "file_name": job.grant.file_name,
            "source_sha256": job.grant.sha256_lower,
            "status": job.status,
            "stage": job.stage,
            "progress_percent": (
                0 if job.status == "queued" else 100 if job.status == "completed" else None
            ),
            "pause_supported": False,
            "error_code": job.error_code,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "result": job.result,
        }
        return response
