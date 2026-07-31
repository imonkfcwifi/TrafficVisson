"""Immutable evidence overlays for synthetic dense-discovery candidates.

The persisted analysis job remains untouched.  This store binds a
``DenseCandidateDiscoveryResult`` to one completed analysis source and one
PipelineJob, copies the representative dense JPEG after fresh verification,
and creates a bounded three-second silent H.264 preview clip.

Only manifest metadata and opaque identities are serializable.  Absolute
filesystem paths exist solely on bridge-internal grant and resolver objects.
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
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4

from .candidate_extraction import _media_tool_path
from .dense_candidate_discovery import (
    DENSE_CANDIDATE_DISCOVERY_SCHEMA,
    DenseCandidateDiscoveryResult,
    DenseCandidateSeed,
)
from .media_intake import CompletedMediaGrant, is_allowlisted_video_name


CANDIDATE_EVIDENCE_OVERLAY_SCHEMA = (
    "trafficvisson.candidate-evidence-overlay.v1"
)
CANDIDATE_EVIDENCE_ARTIFACT_ROOT_BINDING_SCHEMA = (
    "trafficvisson.candidate-evidence-artifact-root-binding.v1"
)
CANDIDATE_EVIDENCE_ARTIFACT_LOCATOR_SCHEMA = (
    "trafficvisson.candidate-evidence-artifact-locator.v1"
)
DINO_TEMPORAL_REVIEW_DISCOVERY_SCHEMA = (
    "trafficvisson.dino-temporal-review-discovery.v1"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_FRAME_ID = re.compile(r"^[a-f0-9]{24}$")
_RESCAN_EVIDENCE_ID = re.compile(r"^[a-f0-9]{20}$")
_OVERLAY_ID = re.compile(r"^ceo_[a-f0-9]{32}$")
_ARTIFACT_ID = re.compile(r"^cea_[a-f0-9]{32}$")
_ISSUE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_FORBIDDEN_METADATA_KEY = re.compile(
    r"(?:^|_)(?:path|token|secret|password|credential|auth)(?:_|$)",
    re.IGNORECASE,
)
_READ_CHUNK_BYTES = 4 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_PROCESS_OUTPUT_BYTES = 512 * 1024
_ALLOWED_FFMPEG_NAMES = frozenset({"ffmpeg", "ffmpeg.exe"})
_ARTIFACT_ROOT_BINDING_FILE = (
    "candidate-evidence-artifact-root-binding.json"
)
_ARTIFACT_LOCATOR_FILE = "artifact-locator.json"
_ARTIFACT_LOCATOR_DIGEST_FILE = "artifact-locator.json.sha256"
CANDIDATE_EVIDENCE_EXTERNAL_LOCATOR_FILES = (
    _ARTIFACT_LOCATOR_FILE,
    _ARTIFACT_LOCATOR_DIGEST_FILE,
)


class CandidateEvidenceOverlayError(RuntimeError):
    """Base failure for candidate evidence materialization."""


class CandidateEvidenceOverlayCancelled(CandidateEvidenceOverlayError):
    """Raised when the owning PipelineJob requests cancellation."""


class CandidateEvidenceOverlayIntegrityError(
    CandidateEvidenceOverlayError
):
    """Raised when an input, manifest, or artifact changed."""


class CandidateEvidenceOverlayConflict(CandidateEvidenceOverlayError):
    """Raised when an identity is valid but cannot be combined."""


class CandidateEvidenceOverlayNotFound(CandidateEvidenceOverlayError):
    """Raised when an overlay candidate or artifact does not exist."""


class CandidateEvidenceOverlayOutputLimit(
    CandidateEvidenceOverlayError
):
    """Raised when a bounded resource budget is exceeded."""


@dataclass(frozen=True)
class CandidateEvidenceOverlayLimits:
    max_candidates: int = 512
    max_source_bytes: int = 256 * 1024**3
    max_image_bytes: int = 24 * 1024**2
    max_clip_bytes: int = 64 * 1024**2
    max_total_output_bytes: int = 8 * 1024**3
    clip_duration_seconds: float = 3.0
    clip_timeout_seconds: float = 120.0
    minimum_free_disk_bytes: int = 512 * 1024**2

    def __post_init__(self) -> None:
        integer_limits = (
            ("max_candidates", self.max_candidates, 1, 512),
            (
                "max_source_bytes",
                self.max_source_bytes,
                1,
                1024 * 1024**3,
            ),
            (
                "max_image_bytes",
                self.max_image_bytes,
                1,
                128 * 1024**2,
            ),
            (
                "max_clip_bytes",
                self.max_clip_bytes,
                1,
                512 * 1024**2,
            ),
            (
                "max_total_output_bytes",
                self.max_total_output_bytes,
                1,
                64 * 1024**3,
            ),
            (
                "minimum_free_disk_bytes",
                self.minimum_free_disk_bytes,
                0,
                64 * 1024**3,
            ),
        )
        for name, value, minimum, maximum in integer_limits:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        for name, value, minimum, maximum in (
            (
                "clip_duration_seconds",
                self.clip_duration_seconds,
                2.0,
                5.0,
            ),
            (
                "clip_timeout_seconds",
                self.clip_timeout_seconds,
                1.0,
                1_800.0,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        if self.max_total_output_bytes < (
            self.max_image_bytes + self.max_clip_bytes
        ):
            raise ValueError(
                "max_total_output_bytes cannot hold one candidate"
            )

    def output_capacity_upper_bound(self, candidate_count: int) -> int:
        """Return the exact configured media ceiling before any write."""

        if (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or not 0 <= candidate_count <= self.max_candidates
        ):
            raise ValueError("candidate_count is outside the supported range")
        return min(
            self.max_total_output_bytes,
            candidate_count
            * (self.max_image_bytes + self.max_clip_bytes),
        )


@dataclass(frozen=True)
class ResolvedCandidateEvidenceBinding:
    """Bridge-internal proof of a completed analysis/PipelineJob source."""

    project_id: UUID
    analysis_job_id: UUID
    pipeline_job_id: UUID
    analysis_status: str
    grant: CompletedMediaGrant


@dataclass(frozen=True)
class ResolvedDenseCandidateFrame:
    """Bridge-internal dense-frame grant returned by the dense index store."""

    project_id: UUID
    dense_index_sha256: str
    frame_id: str
    frames_root: Path
    path: Path
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CandidateEvidenceArtifact:
    artifact_id: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CandidateEvidenceOverlayCandidate:
    candidate_id: str
    issue_type: str
    start_seconds: float
    end_seconds: float
    representative_timestamp_seconds: float
    representative_frame_id: str
    representative_frame_sha256: str
    score: float
    normalized_box: Optional[Mapping[str, float]]
    ocr_text: Optional[str]
    urgent_review: bool
    supporting_frame_ids: tuple[str, ...]
    proposal_sources: tuple[str, ...]
    representative_proposal_provenance: Optional[Mapping[str, Any]]
    proposal_provenance_sha256: str
    image: CandidateEvidenceArtifact
    clip: CandidateEvidenceArtifact


@dataclass(frozen=True)
class CandidateEvidenceOverlayResult:
    schema_version: str
    overlay_id: str
    project_id: str
    analysis_job_id: str
    pipeline_job_id: str
    source_sha256: str
    discovery_revision: str
    dense_index_sha256: str
    provider_provenance_sha256: str
    model_provenance_sha256: str
    candidate_metadata_sha256: str
    candidates: tuple[CandidateEvidenceOverlayCandidate, ...]
    manifest_sha256: str
    created_at: str
    idempotent_replay: bool
    review_only: bool = True
    annotations_created: int = 0
    incidents_created: int = 0
    training_members_created: int = 0
    report_truth_records_created: int = 0

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "overlay_id": self.overlay_id,
            "project_id": self.project_id,
            "analysis_job_id": self.analysis_job_id,
            "pipeline_job_id": self.pipeline_job_id,
            "source_sha256": self.source_sha256,
            "discovery_revision": self.discovery_revision,
            "dense_index_sha256": self.dense_index_sha256,
            "provider_provenance_sha256": (
                self.provider_provenance_sha256
            ),
            "model_provenance_sha256": self.model_provenance_sha256,
            "candidate_metadata_sha256": (
                self.candidate_metadata_sha256
            ),
            "candidate_count": len(self.candidates),
            "manifest_sha256": self.manifest_sha256,
            "created_at": self.created_at,
            "idempotent_replay": self.idempotent_replay,
            "review_only": True,
            "annotations_created": 0,
            "incidents_created": 0,
            "training_members_created": 0,
            "report_truth_records_created": 0,
        }


@dataclass(frozen=True)
class ResolvedCandidateEvidenceArtifact:
    """Verified bridge-internal artifact handle; never serialize this object."""

    overlay_id: str
    candidate_id: str
    kind: str
    path: Path
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CandidateEvidenceMediaCommand:
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    capture_limit_bytes: int
    output_path: Path


@dataclass(frozen=True)
class CandidateEvidenceMediaCommandOutput:
    returncode: int
    stdout: bytes
    stderr: bytes


class CandidateEvidenceBindingResolver(Protocol):
    def __call__(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        source_sha256: str,
    ) -> ResolvedCandidateEvidenceBinding: ...


class DenseCandidateFrameResolver(Protocol):
    def __call__(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        dense_index_sha256: str,
        frame_id: str,
    ) -> ResolvedDenseCandidateFrame: ...


class ReviewCandidateFrameResolver(Protocol):
    def __call__(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        dense_index_sha256: str,
        frame_id: str,
        provenance: Mapping[str, Any],
    ) -> ResolvedDenseCandidateFrame: ...


class CandidateEvidenceArtifactRootResolver(Protocol):
    def __call__(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
    ) -> Path: ...


CancelCallback = Callable[[], bool]
CandidateEvidenceCommandRunner = Callable[
    [CandidateEvidenceMediaCommand, Optional[CancelCallback]],
    CandidateEvidenceMediaCommandOutput,
]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (TypeError, ValueError):
        return False


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _cancel_requested(cancel: Optional[CancelCallback]) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception as error:
        raise CandidateEvidenceOverlayError(
            "candidate overlay cancellation callback failed"
        ) from error


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _strict_json(raw: bytes, label: str) -> dict[str, Any]:
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CandidateEvidenceOverlayIntegrityError(
                    f"{label} contains duplicate JSON keys"
                )
            result[key] = value
        return result

    def reject_constant(_value):
        raise CandidateEvidenceOverlayIntegrityError(
            f"{label} contains a non-finite number"
        )

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except CandidateEvidenceOverlayIntegrityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateEvidenceOverlayIntegrityError(
            f"{label} is not valid UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise CandidateEvidenceOverlayIntegrityError(
            f"{label} must be a JSON object"
        )
    return value


def _safe_metadata(value: Any, *, label: str, depth: int = 0) -> Any:
    if depth > 8:
        raise CandidateEvidenceOverlayIntegrityError(
            f"{label} is invalid"
        )
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and (
            len(value) > 4_096 or "\x00" in value
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                f"{label} is invalid"
            )
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise CandidateEvidenceOverlayIntegrityError(
                f"{label} is invalid"
            )
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 4_096:
            raise CandidateEvidenceOverlayIntegrityError(
                f"{label} is invalid"
            )
        return [
            _safe_metadata(item, label=label, depth=depth + 1)
            for item in value
        ]
    if isinstance(value, Mapping):
        if len(value) > 512:
            raise CandidateEvidenceOverlayIntegrityError(
                f"{label} is invalid"
            )
        result: dict[str, Any] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 128
                or _FORBIDDEN_METADATA_KEY.search(key)
            ):
                raise CandidateEvidenceOverlayIntegrityError(
                    f"{label} is invalid"
                )
            result[key] = _safe_metadata(
                item,
                label=label,
                depth=depth + 1,
            )
        return result
    raise CandidateEvidenceOverlayIntegrityError(f"{label} is invalid")


def _safe_root(path: Path, *, create: bool) -> Path:
    if not path.is_absolute():
        raise ValueError("candidate overlay root must be absolute")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate overlay root is unavailable"
        ) from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate overlay root is unsafe"
        )
    return resolved


def _ensure_child_directory(parent: Path, name: str) -> Path:
    if _SAFE_NAME.fullmatch(name) is None:
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate overlay directory identity is invalid"
        )
    child = parent / name
    child.mkdir(exist_ok=True)
    try:
        resolved = child.resolve(strict=True)
    except OSError as error:
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate overlay directory is unavailable"
        ) from error
    if (
        resolved.parent != parent
        or child.is_symlink()
        or _is_reparse_point(child)
        or not child.is_dir()
    ):
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate overlay directory is unsafe"
        )
    return resolved


def _read_regular(
    path: Path,
    *,
    maximum_bytes: int,
    expected_size: Optional[int] = None,
    expected_mtime_ns: Optional[int] = None,
    return_body: bool,
    cancel: Optional[CancelCallback] = None,
) -> tuple[bytes, str, int, int]:
    try:
        before = path.stat()
    except OSError as error:
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence file is unavailable"
        ) from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 1 <= before.st_size <= maximum_bytes
        or (
            expected_size is not None
            and before.st_size != expected_size
        )
        or (
            expected_mtime_ns is not None
            and before.st_mtime_ns != expected_mtime_ns
        )
    ):
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence file identity changed"
        )
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as stream:
            while block := stream.read(_READ_CHUNK_BYTES):
                if _cancel_requested(cancel):
                    raise CandidateEvidenceOverlayCancelled(
                        "candidate evidence overlay was cancelled"
                    )
                total += len(block)
                if total > maximum_bytes:
                    raise CandidateEvidenceOverlayOutputLimit(
                        "candidate evidence file exceeded its size bound"
                    )
                digest.update(block)
                if return_body:
                    chunks.append(block)
        after = path.stat()
    except OSError as error:
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence file could not be read"
        ) from error
    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence file changed while it was read"
        )
    return (
        b"".join(chunks) if return_body else b"",
        digest.hexdigest(),
        int(after.st_size),
        int(after.st_mtime_ns),
    )


def _jpeg_magic(body: bytes) -> bool:
    return (
        len(body) >= 4
        and body.startswith(b"\xff\xd8\xff")
        and body.endswith(b"\xff\xd9")
    )


def _mp4_magic(body: bytes) -> bool:
    return len(body) >= 12 and body[4:8] == b"ftyp"


def _atomic_write(path: Path, body: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate overlay file could not be persisted"
        ) from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _normalized_absolute_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _authorized_artifact_root(path: Path) -> Path:
    try:
        requested = Path(path)
        if not requested.is_absolute():
            raise OSError
        resolved = requested.resolve(strict=True)
        metadata = resolved.stat()
        if (
            _normalized_absolute_path(requested)
            != _normalized_absolute_path(resolved)
            or resolved.parent == resolved
            or requested.is_symlink()
            or _is_reparse_point(requested)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise OSError
    except (OSError, TypeError, ValueError) as error:
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence artifact root is unsafe"
        ) from error
    return resolved


def _artifact_root_identity(path: Path) -> str:
    try:
        metadata = path.stat()
    except OSError as error:
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence artifact root is unavailable"
        ) from error
    return _sha256_bytes(
        _canonical_json(
            {
                "category": "candidate-evidence",
                "normalized_absolute_path": (
                    _normalized_absolute_path(path)
                ),
                "device": int(metadata.st_dev),
                "inode": int(metadata.st_ino),
            }
        )
    )


def _verified_record_payload(value: Mapping[str, Any]) -> bytes:
    body = dict(value)
    body["record_sha256"] = _sha256_bytes(_canonical_json(body))
    return _canonical_json(body)


def _write_verified_record(
    path: Path,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    body = _verified_record_payload(value)
    record = _strict_json(body, "candidate evidence artifact record")
    _atomic_write(path, body)
    _atomic_write(
        path.with_suffix(path.suffix + ".sha256"),
        f"{_sha256_bytes(body)}\n".encode("ascii"),
    )
    return record


def _read_verified_record(
    path: Path,
    *,
    maximum_bytes: int = _MAX_MANIFEST_BYTES,
) -> dict[str, Any]:
    body, digest, _size, _mtime = _read_regular(
        path,
        maximum_bytes=maximum_bytes,
        return_body=True,
    )
    sidecar, _sidecar_digest, _sidecar_size, _sidecar_mtime = (
        _read_regular(
            path.with_suffix(path.suffix + ".sha256"),
            maximum_bytes=128,
            return_body=True,
        )
    )
    try:
        expected_digest = sidecar.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence artifact record digest is invalid"
        ) from error
    if (
        _SHA256.fullmatch(expected_digest or "") is None
        or expected_digest != digest
    ):
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence artifact record changed"
        )
    record = _strict_json(body, "candidate evidence artifact record")
    record_sha256 = record.get("record_sha256")
    material = dict(record)
    material.pop("record_sha256", None)
    if (
        not isinstance(record_sha256, str)
        or _SHA256.fullmatch(record_sha256) is None
        or _sha256_bytes(_canonical_json(material)) != record_sha256
    ):
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence artifact record changed"
        )
    return record


def _artifact_id(
    overlay_id: str,
    candidate_id: str,
    kind: str,
) -> str:
    material = f"{overlay_id}\0{candidate_id}\0{kind}".encode("ascii")
    return f"cea_{hashlib.sha256(material).hexdigest()[:32]}"


def _overlay_id(material: Mapping[str, Any]) -> str:
    return f"ceo_{hashlib.sha256(_canonical_json(material)).hexdigest()[:32]}"


def _validate_ffmpeg(path: Path) -> str:
    if (
        not path.is_absolute()
        or path.name.casefold() not in _ALLOWED_FFMPEG_NAMES
    ):
        raise ValueError("ffmpeg executable is invalid")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise ValueError("ffmpeg executable is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("ffmpeg executable is unsafe")
    return str(resolved)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.kill()
        process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_candidate_evidence_command(
    command: CandidateEvidenceMediaCommand,
    cancel: Optional[CancelCallback],
) -> CandidateEvidenceMediaCommandOutput:
    """Run one bounded ffmpeg argv with ``shell=False``."""

    if (
        not isinstance(command, CandidateEvidenceMediaCommand)
        or not command.argv
        or any(
            not isinstance(argument, str)
            or "\x00" in argument
            or len(argument) > 32_768
            for argument in command.argv
        )
        or sum(len(argument) for argument in command.argv) > 128 * 1024
        or not command.cwd.is_absolute()
        or not command.cwd.is_dir()
        or not command.output_path.is_absolute()
        or command.output_path.parent != command.cwd
        or command.capture_limit_bytes < 1
        or command.capture_limit_bytes > _MAX_PROCESS_OUTPUT_BYTES
        or not 1 <= command.timeout_seconds <= 1_800
    ):
        raise CandidateEvidenceOverlayError(
            "candidate clip command is invalid"
        )
    executable = Path(command.argv[0])
    if (
        not executable.is_absolute()
        or executable.name.casefold() not in _ALLOWED_FFMPEG_NAMES
        or not executable.is_file()
    ):
        raise CandidateEvidenceOverlayError(
            "candidate clip executable is invalid"
        )
    if _cancel_requested(cancel):
        raise CandidateEvidenceOverlayCancelled(
            "candidate evidence overlay was cancelled"
        )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            list(command.argv),
            shell=False,
            # FFmpeg 4.4 and CreateProcess both need the extended-length
            # prefix when a project-scoped evidence root exceeds the legacy
            # Win32 directory limit.  The argv paths already use the same
            # conversion; keep the process working directory consistent.
            cwd=_media_tool_path(command.cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
    except OSError as error:
        raise CandidateEvidenceOverlayError(
            "ffmpeg could not be started"
        ) from error

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
                    remaining = (
                        command.capture_limit_bytes - captured_total[0]
                    )
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
        name="trafficvisson-candidate-ffmpeg-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=read_stream,
        args=(process.stderr, stderr_buffer),
        name="trafficvisson-candidate-ffmpeg-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    deadline = time.monotonic() + command.timeout_seconds
    failure: Optional[CandidateEvidenceOverlayError] = None
    try:
        while process.poll() is None:
            if overflow.is_set():
                failure = CandidateEvidenceOverlayOutputLimit(
                    "ffmpeg output exceeded its capture bound"
                )
                break
            if _cancel_requested(cancel):
                failure = CandidateEvidenceOverlayCancelled(
                    "candidate evidence overlay was cancelled"
                )
                break
            if time.monotonic() >= deadline:
                failure = CandidateEvidenceOverlayError(
                    "ffmpeg exceeded the candidate clip timeout"
                )
                break
            time.sleep(0.02)
    except CandidateEvidenceOverlayError as error:
        failure = error
    finally:
        if failure is not None:
            _terminate_process(process)

    stdout_thread.join(timeout=2.0)
    stderr_thread.join(timeout=2.0)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        _terminate_process(process)
        raise CandidateEvidenceOverlayError(
            "ffmpeg did not close its output pipes"
        )
    if overflow.is_set() and failure is None:
        failure = CandidateEvidenceOverlayOutputLimit(
            "ffmpeg output exceeded its capture bound"
        )
    if failure is not None:
        raise failure
    return CandidateEvidenceMediaCommandOutput(
        returncode=int(process.returncode),
        stdout=bytes(stdout_buffer),
        stderr=bytes(stderr_buffer),
    )


def _validated_discovery(
    result: DenseCandidateDiscoveryResult,
    *,
    project_id: UUID,
    limits: CandidateEvidenceOverlayLimits,
) -> tuple[str, str, str, list[dict[str, Any]]]:
    if (
        not isinstance(result, DenseCandidateDiscoveryResult)
        or result.schema_version
        not in {
            DENSE_CANDIDATE_DISCOVERY_SCHEMA,
            DINO_TEMPORAL_REVIEW_DISCOVERY_SCHEMA,
        }
        or result.project_id != str(project_id)
        or not isinstance(result.source_sha256, str)
        or _SHA256.fullmatch(result.source_sha256) is None
        or not isinstance(result.dense_index_sha256, str)
        or _SHA256.fullmatch(result.dense_index_sha256) is None
        or not isinstance(result.discovery_revision, str)
        or _SHA256.fullmatch(result.discovery_revision) is None
        or isinstance(result.sampling_fps, bool)
        or not isinstance(result.sampling_fps, int)
        or (
            result.schema_version
            == DENSE_CANDIDATE_DISCOVERY_SCHEMA
            and result.sampling_fps != 2
        )
        or (
            result.schema_version
            == DINO_TEMPORAL_REVIEW_DISCOVERY_SCHEMA
            and not 5 <= result.sampling_fps <= 10
        )
        or isinstance(result.scanned_frame_count, bool)
        or not isinstance(result.scanned_frame_count, int)
        or result.scanned_frame_count < 1
        or not isinstance(result.candidates, (list, tuple))
        or not 0 <= len(result.candidates) <= limits.max_candidates
        or not isinstance(result.provider_states, (list, tuple))
        or result.review_only is not True
        or result.automatic_annotation_creation is not False
        or result.automatic_incident_creation is not False
        or result.automatic_training_ingest is not False
        or result.automatic_report_truth is not False
        or result.annotations_created != 0
        or result.incidents_created != 0
        or result.training_members_created != 0
        or result.report_truth_records_created != 0
    ):
        raise CandidateEvidenceOverlayIntegrityError(
            "dense discovery result is invalid"
        )
    provider_states = _safe_metadata(
        result.provider_states,
        label="dense provider provenance",
    )
    provider_provenance_sha256 = _sha256_bytes(
        _canonical_json(provider_states)
    )
    model_provenance: list[Any] = [
        state
        for state in provider_states
        if isinstance(state, dict)
        and state.get("provider") == "promoted_project_model"
    ]
    candidate_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in result.candidates:
        row = _validated_candidate(
            candidate,
            frame_id_pattern=(
                _FRAME_ID
                if result.schema_version
                == DENSE_CANDIDATE_DISCOVERY_SCHEMA
                else _RESCAN_EVIDENCE_ID
            ),
        )
        if row["candidate_id"] in seen:
            raise CandidateEvidenceOverlayIntegrityError(
                "dense discovery contains duplicate candidates"
            )
        seen.add(row["candidate_id"])
        candidate_rows.append(row)
        provenance = row["representative_proposal_provenance"]
        if (
            isinstance(provenance, dict)
            and "model_version_id" in provenance
        ):
            model_provenance.append(
                {
                    "candidate_id": row["candidate_id"],
                    "provenance": provenance,
                }
            )
    model_provenance_sha256 = _sha256_bytes(
        _canonical_json(model_provenance)
    )
    candidate_metadata_sha256 = _sha256_bytes(
        _canonical_json(candidate_rows)
    )
    revision_payload = {
        "schema_version": result.schema_version,
        "project_id": result.project_id,
        "source_sha256": result.source_sha256,
        "dense_index_sha256": result.dense_index_sha256,
        "sampling_fps": result.sampling_fps,
        "scanned_frame_count": result.scanned_frame_count,
        "provider_states": provider_states,
        "candidates": [asdict(candidate) for candidate in result.candidates],
        "review_only": True,
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_ingest": False,
        "automatic_report_truth": False,
    }
    if _sha256_bytes(_canonical_json(revision_payload)) != (
        result.discovery_revision
    ):
        raise CandidateEvidenceOverlayIntegrityError(
            "dense discovery revision does not match its content"
        )
    return (
        provider_provenance_sha256,
        model_provenance_sha256,
        candidate_metadata_sha256,
        candidate_rows,
    )


def _validated_candidate(
    candidate: DenseCandidateSeed,
    *,
    frame_id_pattern: re.Pattern[str] = _FRAME_ID,
) -> dict[str, Any]:
    if not isinstance(candidate, DenseCandidateSeed):
        raise CandidateEvidenceOverlayIntegrityError(
            "dense candidate metadata is invalid"
        )
    numeric_values = (
        candidate.start_seconds,
        candidate.end_seconds,
        candidate.representative_timestamp_seconds,
        candidate.score,
    )
    if (
        not isinstance(candidate.candidate_id, str)
        or _CANDIDATE_ID.fullmatch(candidate.candidate_id) is None
        or not isinstance(candidate.issue_type, str)
        or _ISSUE_TYPE.fullmatch(candidate.issue_type) is None
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in numeric_values
        )
        or not 0 <= candidate.start_seconds
        <= candidate.representative_timestamp_seconds
        <= candidate.end_seconds
        or not 0 <= candidate.score <= 1
        or not isinstance(candidate.representative_frame_id, str)
        or frame_id_pattern.fullmatch(
            candidate.representative_frame_id
        )
        is None
        or not isinstance(
            candidate.representative_frame_sha256, str
        )
        or _SHA256.fullmatch(candidate.representative_frame_sha256)
        is None
        or not isinstance(candidate.proposal_provenance_sha256, str)
        or _SHA256.fullmatch(candidate.proposal_provenance_sha256)
        is None
        or not isinstance(candidate.urgent_review, bool)
        or not isinstance(
            candidate.supporting_frame_ids, (list, tuple)
        )
        or not 1 <= len(candidate.supporting_frame_ids) <= 100_000
        or candidate.representative_frame_id
        not in candidate.supporting_frame_ids
        or any(
            not isinstance(value, str)
            or frame_id_pattern.fullmatch(value) is None
            for value in candidate.supporting_frame_ids
        )
        or len(set(candidate.supporting_frame_ids))
        != len(candidate.supporting_frame_ids)
        or not isinstance(candidate.proposal_sources, (list, tuple))
        or not 1 <= len(candidate.proposal_sources) <= 64
        or any(
            not isinstance(value, str)
            or not 1 <= len(value) <= 128
            for value in candidate.proposal_sources
        )
        or (
            candidate.ocr_text is not None
            and (
                not isinstance(candidate.ocr_text, str)
                or not candidate.ocr_text.strip()
                or len(candidate.ocr_text) > 500
                or "\x00" in candidate.ocr_text
            )
        )
    ):
        raise CandidateEvidenceOverlayIntegrityError(
            "dense candidate metadata is invalid"
        )
    box = candidate.normalized_box
    if box is not None:
        if not isinstance(box, Mapping) or set(box) != {
            "x",
            "y",
            "width",
            "height",
        }:
            raise CandidateEvidenceOverlayIntegrityError(
                "dense candidate box is invalid"
            )
        values = [box[key] for key in ("x", "y", "width", "height")]
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in values
        ) or not (
            0.0 <= float(box["x"]) <= 1.0
            and 0.0 <= float(box["y"]) <= 1.0
            and 0.0 < float(box["width"]) <= 1.0
            and 0.0 < float(box["height"]) <= 1.0
            and float(box["x"]) + float(box["width"])
            <= 1.00000001
            and float(box["y"]) + float(box["height"])
            <= 1.00000001
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "dense candidate box is invalid"
            )
    provenance = _safe_metadata(
        candidate.representative_proposal_provenance,
        label="dense candidate provenance",
    )
    return {
        "candidate_id": candidate.candidate_id,
        "issue_type": candidate.issue_type,
        "start_seconds": round(float(candidate.start_seconds), 6),
        "end_seconds": round(float(candidate.end_seconds), 6),
        "representative_timestamp_seconds": round(
            float(candidate.representative_timestamp_seconds), 6
        ),
        "representative_frame_id": (
            candidate.representative_frame_id
        ),
        "representative_frame_sha256": (
            candidate.representative_frame_sha256
        ),
        "score": round(float(candidate.score), 8),
        "normalized_box": dict(box) if box is not None else None,
        "ocr_text": (
            candidate.ocr_text.strip()
            if candidate.ocr_text is not None
            else None
        ),
        "urgent_review": bool(candidate.urgent_review),
        "supporting_frame_ids": list(candidate.supporting_frame_ids),
        "proposal_sources": list(candidate.proposal_sources),
        "representative_proposal_provenance": provenance,
        "proposal_provenance_sha256": (
            candidate.proposal_provenance_sha256
        ),
    }


class CandidateEvidenceOverlayStore:
    """Materialize and re-verify immutable dense candidate evidence."""

    def __init__(
        self,
        root: Path,
        *,
        binding_resolver: CandidateEvidenceBindingResolver,
        frame_resolver: DenseCandidateFrameResolver,
        review_frame_resolver: Optional[
            ReviewCandidateFrameResolver
        ] = None,
        artifact_root_resolver: Optional[
            CandidateEvidenceArtifactRootResolver
        ] = None,
        ffmpeg_path: Path,
        limits: Optional[CandidateEvidenceOverlayLimits] = None,
        command_runner: Optional[CandidateEvidenceCommandRunner] = None,
    ) -> None:
        self._root = _safe_root(Path(root), create=True)
        self._binding_resolver = binding_resolver
        self._frame_resolver = frame_resolver
        self._review_frame_resolver = review_frame_resolver
        if (
            artifact_root_resolver is not None
            and not callable(artifact_root_resolver)
        ):
            raise TypeError("artifact_root_resolver must be callable")
        self._artifact_root_resolver = artifact_root_resolver
        self._ffmpeg = _validate_ffmpeg(Path(ffmpeg_path))
        self._limits = limits or CandidateEvidenceOverlayLimits()
        self._runner = command_runner or run_candidate_evidence_command
        self._external_roots: dict[
            tuple[UUID, UUID, UUID],
            tuple[Path, str],
        ] = {}
        self._lock = threading.RLock()

    def output_capacity_upper_bound(self, candidate_count: int) -> int:
        """Expose this writer's configured pre-write media ceiling."""

        return self._limits.output_capacity_upper_bound(candidate_count)

    def materialize(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        discovery: DenseCandidateDiscoveryResult,
        cancel: Optional[CancelCallback] = None,
        artifact_root: Optional[Path] = None,
    ) -> CandidateEvidenceOverlayResult:
        for name, value in (
            ("project_id", project_id),
            ("analysis_job_id", analysis_job_id),
            ("pipeline_job_id", pipeline_job_id),
        ):
            if not isinstance(value, UUID):
                raise ValueError(f"{name} must be a UUID")
        if _cancel_requested(cancel):
            raise CandidateEvidenceOverlayCancelled(
                "candidate evidence overlay was cancelled"
            )
        (
            provider_provenance_sha256,
            model_provenance_sha256,
            candidate_metadata_sha256,
            candidate_rows,
        ) = _validated_discovery(
            discovery,
            project_id=project_id,
            limits=self._limits,
        )
        binding = self._resolve_binding(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            discovery.source_sha256,
        )
        source = self._verify_source(binding.grant, cancel=cancel)
        external_storage = self._resolve_external_artifact_root(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            grant=binding.grant,
            explicit_root=artifact_root,
            initialize_binding=artifact_root is not None,
        )
        identity = {
            "schema_version": CANDIDATE_EVIDENCE_OVERLAY_SCHEMA,
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "pipeline_job_id": str(pipeline_job_id),
            "source_sha256": discovery.source_sha256,
            "discovery_revision": discovery.discovery_revision,
            "dense_index_sha256": discovery.dense_index_sha256,
            "provider_provenance_sha256": (
                provider_provenance_sha256
            ),
            "model_provenance_sha256": model_provenance_sha256,
            "candidate_metadata_sha256": candidate_metadata_sha256,
        }
        overlay_id = _overlay_id(identity)
        with self._lock:
            catalog_root = self._overlay_parent(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                create=True,
            )
            storage_root = (
                external_storage[0]
                if external_storage is not None
                else catalog_root
            )
            target = storage_root / overlay_id
            if target.exists():
                if external_storage is not None:
                    self._ensure_external_locator(
                        catalog_root=catalog_root,
                        artifact_root=storage_root,
                        artifact_root_identity=external_storage[1],
                        project_id=project_id,
                        analysis_job_id=analysis_job_id,
                        pipeline_job_id=pipeline_job_id,
                        overlay_id=overlay_id,
                        grant=binding.grant,
                        target=target,
                    )
                return self._load_overlay(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    overlay_id,
                    idempotent_replay=True,
                    expected_grant=binding.grant,
                )
            if shutil.disk_usage(storage_root).free < (
                self._limits.minimum_free_disk_bytes
            ):
                raise CandidateEvidenceOverlayOutputLimit(
                    "insufficient disk space for candidate overlay"
                )
            # Stage media at the authorized category root so FFmpeg never
            # receives a UUID-heavy working directory that can exceed the
            # Win32 process path limit. Final immutable artifacts are still
            # atomically moved into the exact job-bound storage root.
            staging_root = (
                storage_root
                if external_storage is not None
                else self._root
            )
            staging = staging_root / f".ceo-{uuid4().hex[:12]}"
            artifacts_root = staging / "artifacts"
            try:
                staging.mkdir(exist_ok=False)
                artifacts_root.mkdir(exist_ok=False)
                total_bytes = 0
                persisted_candidates: list[dict[str, Any]] = []
                for row in candidate_rows:
                    if _cancel_requested(cancel):
                        raise CandidateEvidenceOverlayCancelled(
                            "candidate evidence overlay was cancelled"
                        )
                    candidate = next(
                        item
                        for item in discovery.candidates
                        if item.candidate_id == row["candidate_id"]
                    )
                    image = self._copy_frame(
                        project_id,
                        analysis_job_id,
                        pipeline_job_id,
                        discovery,
                        candidate,
                        overlay_id,
                        artifacts_root,
                        cancel,
                    )
                    total_bytes += image["size_bytes"]
                    clip = self._create_clip(
                        source,
                        row,
                        overlay_id,
                        artifacts_root,
                        cancel,
                    )
                    total_bytes += clip["size_bytes"]
                    if total_bytes > self._limits.max_total_output_bytes:
                        raise CandidateEvidenceOverlayOutputLimit(
                            "candidate overlay exceeded its output budget"
                        )
                    persisted_candidates.append(
                        {**row, "image": image, "clip": clip}
                    )
                    self._source_stat_unchanged(source, binding.grant)
                self._verify_source(binding.grant, cancel=cancel)
                manifest = {
                    **identity,
                    "overlay_id": overlay_id,
                    "source_snapshot_id": str(
                        binding.grant.snapshot_id
                    ),
                    "source_hash_job_id": str(
                        binding.grant.hash_job_id
                    ),
                    "source_file_name": binding.grant.file_name,
                    "source_size_bytes": binding.grant.size_bytes,
                    "source_modified_time_ns": (
                        binding.grant.modified_time_ns
                    ),
                    "candidate_count": len(persisted_candidates),
                    "candidates": persisted_candidates,
                    "created_at": _utc_now(),
                    "review_only": True,
                    "annotations_created": 0,
                    "incidents_created": 0,
                    "training_members_created": 0,
                    "report_truth_records_created": 0,
                }
                manifest["record_sha256"] = _sha256_bytes(
                    _canonical_json(manifest)
                )
                manifest_body = _canonical_json(manifest)
                if len(manifest_body) > _MAX_MANIFEST_BYTES:
                    raise CandidateEvidenceOverlayOutputLimit(
                        "candidate overlay manifest exceeded its size bound"
                    )
                _atomic_write(staging / "manifest.json", manifest_body)
                if _cancel_requested(cancel):
                    raise CandidateEvidenceOverlayCancelled(
                        "candidate evidence overlay was cancelled"
                    )
                try:
                    staging.rename(target)
                except FileExistsError:
                    pass
                if staging.exists():
                    shutil.rmtree(staging)
                if external_storage is not None:
                    self._ensure_external_locator(
                        catalog_root=catalog_root,
                        artifact_root=storage_root,
                        artifact_root_identity=external_storage[1],
                        project_id=project_id,
                        analysis_job_id=analysis_job_id,
                        pipeline_job_id=pipeline_job_id,
                        overlay_id=overlay_id,
                        grant=binding.grant,
                        target=target,
                    )
                return self._load_overlay(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    overlay_id,
                    idempotent_replay=False,
                    expected_grant=binding.grant,
                )
            except Exception:
                if (
                    staging.exists()
                    and staging.parent == staging_root
                    and staging.name.startswith(".ceo-")
                ):
                    shutil.rmtree(staging, ignore_errors=True)
                raise

    def get(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
    ) -> CandidateEvidenceOverlayResult:
        with self._lock:
            return self._load_overlay(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                overlay_id,
                idempotent_replay=True,
            )

    def resolve_candidate(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        candidate_id: str,
    ) -> CandidateEvidenceOverlayCandidate:
        overlay = self.get(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
        )
        candidate = next(
            (
                value
                for value in overlay.candidates
                if value.candidate_id == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise CandidateEvidenceOverlayNotFound(
                "candidate overlay evidence was not found"
            )
        return candidate

    def resolve_artifact(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        candidate_id: str,
        kind: str,
    ) -> ResolvedCandidateEvidenceArtifact:
        if kind not in {"image", "clip"}:
            raise CandidateEvidenceOverlayNotFound(
                "candidate overlay artifact was not found"
            )
        candidate = self.resolve_candidate(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
            candidate_id,
        )
        metadata = candidate.image if kind == "image" else candidate.clip
        overlay_root = self._overlay_directory(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
        )
        path = (
            overlay_root
            / "artifacts"
            / (
                f"{candidate_id}.jpg"
                if kind == "image"
                else f"{candidate_id}.mp4"
            )
        )
        self._verify_overlay_artifact(
            path,
            overlay_root / "artifacts",
            metadata,
            kind,
        )
        return ResolvedCandidateEvidenceArtifact(
            overlay_id=overlay_id,
            candidate_id=candidate_id,
            kind=kind,
            path=path.resolve(strict=True),
            media_type=metadata.media_type,
            size_bytes=metadata.size_bytes,
            sha256=metadata.sha256,
        )

    def _resolve_binding(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        source_sha256: str,
    ) -> ResolvedCandidateEvidenceBinding:
        try:
            binding = self._binding_resolver(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                source_sha256,
            )
        except CandidateEvidenceOverlayError:
            raise
        except Exception as error:
            raise CandidateEvidenceOverlayConflict(
                "completed analysis source grant could not be resolved"
            ) from error
        if (
            not isinstance(binding, ResolvedCandidateEvidenceBinding)
            or binding.project_id != project_id
            or binding.analysis_job_id != analysis_job_id
            or binding.pipeline_job_id != pipeline_job_id
            or binding.analysis_status != "completed"
            or not isinstance(binding.grant, CompletedMediaGrant)
            or binding.grant.project_id != project_id
            or not isinstance(binding.grant.snapshot_id, UUID)
            or not isinstance(binding.grant.hash_job_id, UUID)
            or not isinstance(binding.grant.file_name, str)
            or not is_allowlisted_video_name(binding.grant.file_name)
            or isinstance(binding.grant.size_bytes, bool)
            or not isinstance(binding.grant.size_bytes, int)
            or not 1
            <= binding.grant.size_bytes
            <= self._limits.max_source_bytes
            or isinstance(binding.grant.modified_time_ns, bool)
            or not isinstance(binding.grant.modified_time_ns, int)
            or binding.grant.modified_time_ns < 0
            or not isinstance(binding.grant.sha256_lower, str)
            or _SHA256.fullmatch(binding.grant.sha256_lower) is None
            or binding.grant.sha256_lower != source_sha256
        ):
            raise CandidateEvidenceOverlayConflict(
                "completed analysis/PipelineJob binding does not match"
            )
        return binding

    def _verify_source(
        self,
        grant: CompletedMediaGrant,
        *,
        cancel: Optional[CancelCallback] = None,
    ) -> Path:
        path = Path(grant.media_path)
        if (
            not path.is_absolute()
            or not is_allowlisted_video_name(grant.file_name)
            or path.name != grant.file_name
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "analysis source grant path is invalid"
            )
        _body, digest, size, modified_time_ns = _read_regular(
            path,
            maximum_bytes=self._limits.max_source_bytes,
            expected_size=grant.size_bytes,
            expected_mtime_ns=grant.modified_time_ns,
            return_body=False,
            cancel=cancel,
        )
        try:
            before = path.stat()
            with path.open("rb") as stream:
                header = stream.read(32)
            after = path.stat()
        except OSError as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "analysis source header could not be read"
            ) from error
        if (
            digest != grant.sha256_lower
            or size != grant.size_bytes
            or modified_time_ns != grant.modified_time_ns
            or not _mp4_magic(header)
            or path.is_symlink()
            or _is_reparse_point(path)
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or after.st_size != grant.size_bytes
            or after.st_mtime_ns != grant.modified_time_ns
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "analysis source grant failed fresh verification"
            )
        return path.resolve(strict=True)

    @staticmethod
    def _source_stat_unchanged(
        path: Path,
        grant: CompletedMediaGrant,
    ) -> None:
        try:
            metadata = path.stat()
        except OSError as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "analysis source changed during clip generation"
            ) from error
        if (
            metadata.st_size != grant.size_bytes
            or metadata.st_mtime_ns != grant.modified_time_ns
            or path.is_symlink()
            or _is_reparse_point(path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "analysis source changed during clip generation"
            )

    def _copy_frame(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        discovery: DenseCandidateDiscoveryResult,
        candidate: DenseCandidateSeed,
        overlay_id: str,
        artifacts_root: Path,
        cancel: Optional[CancelCallback],
    ) -> dict[str, Any]:
        try:
            if (
                discovery.schema_version
                == DINO_TEMPORAL_REVIEW_DISCOVERY_SCHEMA
            ):
                if self._review_frame_resolver is None:
                    raise CandidateEvidenceOverlayIntegrityError(
                        "review frame resolver is unavailable"
                    )
                provenance = (
                    candidate.representative_proposal_provenance
                )
                if not isinstance(provenance, Mapping):
                    raise CandidateEvidenceOverlayIntegrityError(
                        "review frame provenance is unavailable"
                    )
                frame = self._review_frame_resolver(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    discovery.dense_index_sha256,
                    candidate.representative_frame_id,
                    provenance,
                )
            else:
                frame = self._frame_resolver(
                    project_id,
                    pipeline_job_id,
                    discovery.dense_index_sha256,
                    candidate.representative_frame_id,
                )
        except CandidateEvidenceOverlayError:
            raise
        except Exception as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "representative dense frame could not be resolved"
            ) from error
        if (
            not isinstance(frame, ResolvedDenseCandidateFrame)
            or frame.project_id != project_id
            or frame.dense_index_sha256
            != discovery.dense_index_sha256
            or frame.frame_id != candidate.representative_frame_id
            or frame.media_type != "image/jpeg"
            or frame.sha256 != candidate.representative_frame_sha256
            or not isinstance(frame.sha256, str)
            or _SHA256.fullmatch(frame.sha256) is None
            or isinstance(frame.size_bytes, bool)
            or not isinstance(frame.size_bytes, int)
            or frame.size_bytes < 1
            or frame.size_bytes > self._limits.max_image_bytes
            or not isinstance(frame.frames_root, Path)
            or not isinstance(frame.path, Path)
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "representative dense frame grant does not match"
            )
        root = Path(frame.frames_root)
        path = Path(frame.path)
        try:
            resolved_root = root.resolve(strict=True)
            resolved_path = path.resolve(strict=True)
        except OSError as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "representative dense frame is unavailable"
            ) from error
        if (
            not root.is_absolute()
            or not path.is_absolute()
            or root.is_symlink()
            or _is_reparse_point(root)
            or path.is_symlink()
            or _is_reparse_point(path)
            or resolved_path.parent != resolved_root
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "representative dense frame containment failed"
            )
        body, digest, size, _mtime = _read_regular(
            resolved_path,
            maximum_bytes=self._limits.max_image_bytes,
            expected_size=frame.size_bytes,
            return_body=True,
            cancel=cancel,
        )
        if (
            digest != frame.sha256
            or size != frame.size_bytes
            or not _jpeg_magic(body)
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "representative dense JPEG failed fresh verification"
            )
        output = artifacts_root / f"{candidate.candidate_id}.jpg"
        _atomic_write(output, body)
        metadata = CandidateEvidenceArtifact(
            artifact_id=_artifact_id(
                overlay_id, candidate.candidate_id, "image"
            ),
            media_type="image/jpeg",
            size_bytes=size,
            sha256=digest,
        )
        self._verify_overlay_artifact(
            output,
            artifacts_root,
            metadata,
            "image",
        )
        return {
            "artifact_id": metadata.artifact_id,
            "relative_path": f"artifacts/{output.name}",
            "media_type": metadata.media_type,
            "size_bytes": metadata.size_bytes,
            "sha256": metadata.sha256,
        }

    def _create_clip(
        self,
        source: Path,
        candidate: Mapping[str, Any],
        overlay_id: str,
        artifacts_root: Path,
        cancel: Optional[CancelCallback],
    ) -> dict[str, Any]:
        candidate_id = candidate["candidate_id"]
        output = artifacts_root / f"{candidate_id}.mp4"
        start = max(
            0.0,
            float(candidate["representative_timestamp_seconds"])
            - self._limits.clip_duration_seconds / 2.0,
        )
        command = CandidateEvidenceMediaCommand(
            argv=(
                self._ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.6f}",
                "-i",
                _media_tool_path(source),
                "-t",
                f"{self._limits.clip_duration_seconds:.6f}",
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-map_metadata",
                "-1",
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
                str(self._limits.max_clip_bytes),
                "-n",
                _media_tool_path(output),
            ),
            cwd=artifacts_root,
            timeout_seconds=self._limits.clip_timeout_seconds,
            capture_limit_bytes=_MAX_PROCESS_OUTPUT_BYTES,
            output_path=output,
        )
        try:
            result = self._runner(command, cancel)
        except CandidateEvidenceOverlayError:
            raise
        except Exception as error:
            raise CandidateEvidenceOverlayError(
                "candidate clip runner failed"
            ) from error
        if (
            not isinstance(
                result, CandidateEvidenceMediaCommandOutput
            )
            or isinstance(result.returncode, bool)
            or not isinstance(result.returncode, int)
            or not isinstance(result.stdout, bytes)
            or not isinstance(result.stderr, bytes)
            or len(result.stdout) + len(result.stderr)
            > command.capture_limit_bytes
        ):
            raise CandidateEvidenceOverlayError(
                "candidate clip runner returned invalid data"
            )
        if _cancel_requested(cancel):
            raise CandidateEvidenceOverlayCancelled(
                "candidate evidence overlay was cancelled"
            )
        if result.returncode != 0:
            raise CandidateEvidenceOverlayError(
                "ffmpeg could not create candidate preview clip"
            )
        body, digest, size, _mtime = _read_regular(
            output,
            maximum_bytes=self._limits.max_clip_bytes,
            return_body=True,
        )
        if not _mp4_magic(body[:32]):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate preview clip has invalid MP4 magic"
            )
        metadata = CandidateEvidenceArtifact(
            artifact_id=_artifact_id(
                overlay_id, candidate_id, "clip"
            ),
            media_type="video/mp4",
            size_bytes=size,
            sha256=digest,
        )
        self._verify_overlay_artifact(
            output,
            artifacts_root,
            metadata,
            "clip",
        )
        return {
            "artifact_id": metadata.artifact_id,
            "relative_path": f"artifacts/{output.name}",
            "media_type": metadata.media_type,
            "size_bytes": metadata.size_bytes,
            "sha256": metadata.sha256,
            "start_seconds": round(start, 6),
            "duration_seconds": round(
                self._limits.clip_duration_seconds, 6
            ),
            "silent": True,
            "video_codec": "h264",
        }

    @staticmethod
    def _external_binding_material(
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        grant: CompletedMediaGrant,
        artifact_root_identity: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": (
                CANDIDATE_EVIDENCE_ARTIFACT_ROOT_BINDING_SCHEMA
            ),
            "category": "candidate-evidence",
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "pipeline_job_id": str(pipeline_job_id),
            "source_sha256": grant.sha256_lower,
            "source_snapshot_id": str(grant.snapshot_id),
            "source_hash_job_id": str(grant.hash_job_id),
            "source_size_bytes": grant.size_bytes,
            "source_modified_time_ns": grant.modified_time_ns,
            "artifact_root_identity_sha256": artifact_root_identity,
        }

    def _resolve_external_artifact_root(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        grant: CompletedMediaGrant,
        explicit_root: Optional[Path],
        initialize_binding: bool,
    ) -> Optional[tuple[Path, str]]:
        key = (project_id, analysis_job_id, pipeline_job_id)
        if explicit_root is None:
            cached = self._external_roots.get(key)
            if cached is None:
                return None
            root = _authorized_artifact_root(cached[0])
            if _artifact_root_identity(root) != cached[1]:
                raise CandidateEvidenceOverlayIntegrityError(
                    "candidate evidence artifact root changed"
                )
        else:
            root = _authorized_artifact_root(Path(explicit_root))
        identity = _artifact_root_identity(root)
        cached = self._external_roots.get(key)
        if cached is not None and (
            cached[0] != root or cached[1] != identity
        ):
            raise CandidateEvidenceOverlayConflict(
                "candidate evidence artifact root changed after binding"
            )
        material = self._external_binding_material(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            grant=grant,
            artifact_root_identity=identity,
        )
        binding_path = root / _ARTIFACT_ROOT_BINDING_FILE
        expected = _strict_json(
            _verified_record_payload(material),
            "candidate evidence artifact-root binding",
        )
        if binding_path.exists():
            persisted = _read_verified_record(binding_path)
            if persisted != expected:
                raise CandidateEvidenceOverlayConflict(
                    "candidate evidence artifact root is bound to another job"
                )
        elif initialize_binding:
            persisted = _write_verified_record(binding_path, material)
            if persisted != expected:
                raise CandidateEvidenceOverlayIntegrityError(
                    "candidate evidence artifact-root binding is invalid"
                )
        else:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence artifact-root binding is unavailable"
            )
        self._external_roots[key] = (root, identity)
        return root, identity

    def _ensure_external_locator(
        self,
        *,
        catalog_root: Path,
        artifact_root: Path,
        artifact_root_identity: str,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        grant: CompletedMediaGrant,
        target: Path,
    ) -> None:
        try:
            resolved_target = target.resolve(strict=True)
        except OSError as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence external overlay is unavailable"
            ) from error
        if (
            resolved_target.parent != artifact_root
            or target.is_symlink()
            or _is_reparse_point(target)
            or not target.is_dir()
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence external overlay is unsafe"
            )
        _manifest_body, manifest_sha256, _size, _mtime = _read_regular(
            resolved_target / "manifest.json",
            maximum_bytes=_MAX_MANIFEST_BYTES,
            return_body=True,
        )
        material = {
            "schema_version": (
                CANDIDATE_EVIDENCE_ARTIFACT_LOCATOR_SCHEMA
            ),
            "category": "candidate-evidence",
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "pipeline_job_id": str(pipeline_job_id),
            "overlay_id": overlay_id,
            "source_sha256": grant.sha256_lower,
            "source_snapshot_id": str(grant.snapshot_id),
            "source_hash_job_id": str(grant.hash_job_id),
            "source_size_bytes": grant.size_bytes,
            "source_modified_time_ns": grant.modified_time_ns,
            "artifact_root_identity_sha256": artifact_root_identity,
            "manifest_sha256": manifest_sha256,
        }
        expected = _strict_json(
            _verified_record_payload(material),
            "candidate evidence artifact locator",
        )
        locator_root = catalog_root / overlay_id
        if locator_root.exists():
            try:
                resolved_locator_root = locator_root.resolve(strict=True)
            except OSError as error:
                raise CandidateEvidenceOverlayIntegrityError(
                    "candidate evidence artifact locator is unavailable"
                ) from error
            if (
                resolved_locator_root.parent != catalog_root
                or locator_root.is_symlink()
                or _is_reparse_point(locator_root)
                or not locator_root.is_dir()
            ):
                raise CandidateEvidenceOverlayIntegrityError(
                    "candidate evidence artifact locator is unsafe"
                )
            try:
                entries = tuple(
                    sorted(item.name for item in locator_root.iterdir())
                )
            except OSError as error:
                raise CandidateEvidenceOverlayIntegrityError(
                    "candidate evidence artifact locator is unavailable"
                ) from error
            if entries != CANDIDATE_EVIDENCE_EXTERNAL_LOCATOR_FILES:
                raise CandidateEvidenceOverlayConflict(
                    "candidate evidence overlay storage mode changed"
                )
            persisted = _read_verified_record(
                locator_root / _ARTIFACT_LOCATOR_FILE
            )
            if persisted != expected:
                raise CandidateEvidenceOverlayConflict(
                    "candidate evidence artifact locator changed"
                )
            return
        locator_root = _ensure_child_directory(
            catalog_root, overlay_id
        )
        persisted = _write_verified_record(
            locator_root / _ARTIFACT_LOCATOR_FILE,
            material,
        )
        if persisted != expected:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence artifact locator is invalid"
            )

    def _external_directory_from_locator(
        self,
        *,
        locator_root: Path,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
    ) -> Path:
        locator = _read_verified_record(
            locator_root / _ARTIFACT_LOCATOR_FILE
        )
        expected_keys = {
            "schema_version",
            "category",
            "project_id",
            "analysis_job_id",
            "pipeline_job_id",
            "overlay_id",
            "source_sha256",
            "source_snapshot_id",
            "source_hash_job_id",
            "source_size_bytes",
            "source_modified_time_ns",
            "artifact_root_identity_sha256",
            "manifest_sha256",
            "record_sha256",
        }
        if (
            set(locator) != expected_keys
            or locator.get("schema_version")
            != CANDIDATE_EVIDENCE_ARTIFACT_LOCATOR_SCHEMA
            or locator.get("category") != "candidate-evidence"
            or locator.get("project_id") != str(project_id)
            or locator.get("analysis_job_id") != str(analysis_job_id)
            or locator.get("pipeline_job_id") != str(pipeline_job_id)
            or locator.get("overlay_id") != overlay_id
            or _SHA256.fullmatch(
                str(locator.get("source_sha256") or "")
            )
            is None
            or not _is_canonical_uuid(locator.get("source_snapshot_id"))
            or not _is_canonical_uuid(locator.get("source_hash_job_id"))
            or isinstance(locator.get("source_size_bytes"), bool)
            or not isinstance(locator.get("source_size_bytes"), int)
            or locator["source_size_bytes"] <= 0
            or isinstance(
                locator.get("source_modified_time_ns"), bool
            )
            or not isinstance(
                locator.get("source_modified_time_ns"), int
            )
            or locator["source_modified_time_ns"] < 0
            or _SHA256.fullmatch(
                str(
                    locator.get("artifact_root_identity_sha256")
                    or ""
                )
            )
            is None
            or _SHA256.fullmatch(
                str(locator.get("manifest_sha256") or "")
            )
            is None
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence artifact locator is invalid"
            )
        key = (project_id, analysis_job_id, pipeline_job_id)
        cached = self._external_roots.get(key)
        if cached is None:
            if self._artifact_root_resolver is None:
                raise CandidateEvidenceOverlayNotFound(
                    "candidate evidence artifact root must be re-resolved"
                )
            try:
                callback_root = self._artifact_root_resolver(
                    project_id,
                    pipeline_job_id,
                    analysis_job_id,
                )
            except CandidateEvidenceOverlayError:
                raise
            except Exception as error:
                raise CandidateEvidenceOverlayNotFound(
                    "candidate evidence artifact root could not be re-resolved"
                ) from error
            binding = self._resolve_binding(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                str(locator["source_sha256"]),
            )
            grant = binding.grant
            if any(
                locator.get(name) != expected
                for name, expected in (
                    ("source_snapshot_id", str(grant.snapshot_id)),
                    ("source_hash_job_id", str(grant.hash_job_id)),
                    ("source_size_bytes", grant.size_bytes),
                    (
                        "source_modified_time_ns",
                        grant.modified_time_ns,
                    ),
                )
            ):
                raise CandidateEvidenceOverlayIntegrityError(
                    "candidate evidence source binding changed"
                )
            resolved = self._resolve_external_artifact_root(
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                pipeline_job_id=pipeline_job_id,
                grant=grant,
                explicit_root=Path(callback_root),
                initialize_binding=False,
            )
            if resolved is None:
                raise CandidateEvidenceOverlayNotFound(
                    "candidate evidence artifact root could not be re-resolved"
                )
            cached = resolved
        artifact_root = _authorized_artifact_root(cached[0])
        identity = _artifact_root_identity(artifact_root)
        if (
            identity != cached[1]
            or identity
            != locator["artifact_root_identity_sha256"]
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence artifact root changed"
            )
        binding = _read_verified_record(
            artifact_root / _ARTIFACT_ROOT_BINDING_FILE
        )
        expected_binding_keys = {
            "schema_version",
            "category",
            "project_id",
            "analysis_job_id",
            "pipeline_job_id",
            "source_sha256",
            "source_snapshot_id",
            "source_hash_job_id",
            "source_size_bytes",
            "source_modified_time_ns",
            "artifact_root_identity_sha256",
            "record_sha256",
        }
        if (
            set(binding) != expected_binding_keys
            or binding.get("schema_version")
            != CANDIDATE_EVIDENCE_ARTIFACT_ROOT_BINDING_SCHEMA
            or any(
                binding.get(name) != locator.get(name)
                for name in (
                    "category",
                    "project_id",
                    "analysis_job_id",
                    "pipeline_job_id",
                    "source_sha256",
                    "source_snapshot_id",
                    "source_hash_job_id",
                    "source_size_bytes",
                    "source_modified_time_ns",
                    "artifact_root_identity_sha256",
                )
            )
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence artifact-root binding changed"
            )
        directory = artifact_root / overlay_id
        try:
            resolved = directory.resolve(strict=True)
        except OSError as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence external overlay is unavailable"
            ) from error
        if (
            resolved.parent != artifact_root
            or directory.is_symlink()
            or _is_reparse_point(directory)
            or not directory.is_dir()
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence external overlay is unsafe"
            )
        _body, manifest_sha256, _size, _mtime = _read_regular(
            resolved / "manifest.json",
            maximum_bytes=_MAX_MANIFEST_BYTES,
            return_body=True,
        )
        if manifest_sha256 != locator["manifest_sha256"]:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence external manifest changed"
            )
        return resolved

    def _overlay_parent(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        *,
        create: bool,
    ) -> Path:
        parent = self._root
        for value in (
            str(project_id),
            str(analysis_job_id),
            str(pipeline_job_id),
        ):
            if create:
                parent = _ensure_child_directory(parent, value)
            else:
                child = parent / value
                try:
                    resolved = child.resolve(strict=True)
                except OSError as error:
                    raise CandidateEvidenceOverlayNotFound(
                        "candidate evidence overlay was not found"
                    ) from error
                if (
                    resolved.parent != parent
                    or child.is_symlink()
                    or _is_reparse_point(child)
                    or not child.is_dir()
                ):
                    raise CandidateEvidenceOverlayIntegrityError(
                        "candidate overlay hierarchy is unsafe"
                    )
                parent = resolved
        return parent

    def _overlay_directory(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
    ) -> Path:
        if _OVERLAY_ID.fullmatch(overlay_id) is None:
            raise CandidateEvidenceOverlayNotFound(
                "candidate evidence overlay was not found"
            )
        parent = self._overlay_parent(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            create=False,
        )
        directory = parent / overlay_id
        try:
            resolved = directory.resolve(strict=True)
        except OSError as error:
            raise CandidateEvidenceOverlayNotFound(
                "candidate evidence overlay was not found"
            ) from error
        if (
            resolved.parent != parent
            or directory.is_symlink()
            or _is_reparse_point(directory)
            or not directory.is_dir()
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence overlay directory is unsafe"
            )
        try:
            entries = tuple(
                sorted(item.name for item in resolved.iterdir())
            )
        except OSError as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate evidence overlay directory is unavailable"
            ) from error
        if entries == ("artifacts", "manifest.json"):
            return resolved
        if entries == CANDIDATE_EVIDENCE_EXTERNAL_LOCATOR_FILES:
            return self._external_directory_from_locator(
                locator_root=resolved,
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                pipeline_job_id=pipeline_job_id,
                overlay_id=overlay_id,
            )
        raise CandidateEvidenceOverlayIntegrityError(
            "candidate evidence overlay directory has unexpected entries"
        )

    def _load_overlay(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        *,
        idempotent_replay: bool,
        expected_grant: Optional[CompletedMediaGrant] = None,
    ) -> CandidateEvidenceOverlayResult:
        directory = self._overlay_directory(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
        )
        manifest_path = directory / "manifest.json"
        body, manifest_sha256, _size, _mtime = _read_regular(
            manifest_path,
            maximum_bytes=_MAX_MANIFEST_BYTES,
            return_body=True,
        )
        manifest = _strict_json(body, "candidate overlay manifest")
        record_sha256 = manifest.get("record_sha256")
        if (
            not isinstance(record_sha256, str)
            or _SHA256.fullmatch(record_sha256) is None
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay record hash is invalid"
            )
        record_payload = dict(manifest)
        record_payload.pop("record_sha256")
        if _sha256_bytes(_canonical_json(record_payload)) != record_sha256:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay manifest changed"
            )
        expected_top_keys = {
            "schema_version",
            "overlay_id",
            "project_id",
            "analysis_job_id",
            "pipeline_job_id",
            "source_sha256",
            "discovery_revision",
            "dense_index_sha256",
            "provider_provenance_sha256",
            "model_provenance_sha256",
            "candidate_metadata_sha256",
            "source_snapshot_id",
            "source_hash_job_id",
            "source_file_name",
            "source_size_bytes",
            "source_modified_time_ns",
            "candidate_count",
            "candidates",
            "created_at",
            "review_only",
            "annotations_created",
            "incidents_created",
            "training_members_created",
            "report_truth_records_created",
            "record_sha256",
        }
        if (
            set(manifest) != expected_top_keys
            or manifest.get("schema_version")
            != CANDIDATE_EVIDENCE_OVERLAY_SCHEMA
            or manifest.get("overlay_id") != overlay_id
            or manifest.get("project_id") != str(project_id)
            or manifest.get("analysis_job_id")
            != str(analysis_job_id)
            or manifest.get("pipeline_job_id")
            != str(pipeline_job_id)
            or not _is_canonical_uuid(
                manifest.get("source_snapshot_id")
            )
            or not _is_canonical_uuid(
                manifest.get("source_hash_job_id")
            )
            or not isinstance(manifest.get("source_file_name"), str)
            or not is_allowlisted_video_name(
                manifest["source_file_name"]
            )
            or isinstance(manifest.get("source_size_bytes"), bool)
            or not isinstance(manifest.get("source_size_bytes"), int)
            or not 1
            <= manifest["source_size_bytes"]
            <= self._limits.max_source_bytes
            or isinstance(
                manifest.get("source_modified_time_ns"), bool
            )
            or not isinstance(
                manifest.get("source_modified_time_ns"), int
            )
            or manifest["source_modified_time_ns"] < 0
            or not _is_timestamp(manifest.get("created_at"))
            or any(
                _SHA256.fullmatch(str(manifest.get(key, ""))) is None
                for key in (
                    "source_sha256",
                    "discovery_revision",
                    "dense_index_sha256",
                    "provider_provenance_sha256",
                    "model_provenance_sha256",
                    "candidate_metadata_sha256",
                )
            )
            or manifest.get("review_only") is not True
            or manifest.get("annotations_created") != 0
            or manifest.get("incidents_created") != 0
            or manifest.get("training_members_created") != 0
            or manifest.get("report_truth_records_created") != 0
            or not isinstance(manifest.get("candidates"), list)
            or isinstance(manifest.get("candidate_count"), bool)
            or not isinstance(manifest.get("candidate_count"), int)
            or manifest.get("candidate_count")
            != len(manifest["candidates"])
            or not 0
            <= len(manifest["candidates"])
            <= self._limits.max_candidates
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay manifest contract is invalid"
            )
        if expected_grant is not None and (
            not isinstance(expected_grant, CompletedMediaGrant)
            or manifest["source_sha256"]
            != expected_grant.sha256_lower
            or manifest["source_snapshot_id"]
            != str(expected_grant.snapshot_id)
            or manifest["source_hash_job_id"]
            != str(expected_grant.hash_job_id)
            or manifest["source_file_name"] != expected_grant.file_name
            or manifest["source_size_bytes"]
            != expected_grant.size_bytes
            or manifest["source_modified_time_ns"]
            != expected_grant.modified_time_ns
        ):
            raise CandidateEvidenceOverlayConflict(
                "candidate overlay source binding changed"
            )
        identity = {
            key: manifest[key]
            for key in (
                "schema_version",
                "project_id",
                "analysis_job_id",
                "pipeline_job_id",
                "source_sha256",
                "discovery_revision",
                "dense_index_sha256",
                "provider_provenance_sha256",
                "model_provenance_sha256",
                "candidate_metadata_sha256",
            )
        }
        if _overlay_id(identity) != overlay_id:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay identity does not match"
            )
        artifacts_root = directory / "artifacts"
        try:
            resolved_artifacts_root = artifacts_root.resolve(strict=True)
        except OSError as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay artifacts are unavailable"
            ) from error
        if (
            resolved_artifacts_root.parent != directory
            or artifacts_root.is_symlink()
            or _is_reparse_point(artifacts_root)
            or not artifacts_root.is_dir()
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay artifacts directory is unsafe"
            )
        try:
            root_children = {entry.name: entry for entry in directory.iterdir()}
            artifact_children = {
                entry.name: entry for entry in artifacts_root.iterdir()
            }
        except OSError as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay directory could not be enumerated"
            ) from error
        if set(root_children) != {"manifest.json", "artifacts"}:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay contains unexpected entries"
            )
        candidates: list[CandidateEvidenceOverlayCandidate] = []
        expected_files = {"manifest.json"}
        candidate_rows_without_artifacts: list[dict[str, Any]] = []
        for raw in manifest["candidates"]:
            candidate, metadata_row = self._parse_manifest_candidate(
                raw,
                overlay_id,
                resolved_artifacts_root,
            )
            candidates.append(candidate)
            candidate_rows_without_artifacts.append(metadata_row)
            expected_files.add(
                f"artifacts/{candidate.candidate_id}.jpg"
            )
            expected_files.add(
                f"artifacts/{candidate.candidate_id}.mp4"
            )
        if len({item.candidate_id for item in candidates}) != len(
            candidates
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay contains duplicate candidates"
            )
        if _sha256_bytes(
            _canonical_json(candidate_rows_without_artifacts)
        ) != manifest["candidate_metadata_sha256"]:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay metadata hash does not match"
            )
        actual_files = {
            str(path.relative_to(directory)).replace("\\", "/")
            for path in directory.rglob("*")
            if path.is_file()
        }
        expected_artifact_names = {
            value.removeprefix("artifacts/")
            for value in expected_files
            if value.startswith("artifacts/")
        }
        if (
            actual_files != expected_files
            or set(artifact_children) != expected_artifact_names
            or any(
                entry.is_symlink()
                or _is_reparse_point(entry)
                or not entry.is_file()
                for entry in artifact_children.values()
            )
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay contains unexpected artifacts"
            )
        return CandidateEvidenceOverlayResult(
            schema_version=manifest["schema_version"],
            overlay_id=overlay_id,
            project_id=str(project_id),
            analysis_job_id=str(analysis_job_id),
            pipeline_job_id=str(pipeline_job_id),
            source_sha256=manifest["source_sha256"],
            discovery_revision=manifest["discovery_revision"],
            dense_index_sha256=manifest["dense_index_sha256"],
            provider_provenance_sha256=manifest[
                "provider_provenance_sha256"
            ],
            model_provenance_sha256=manifest[
                "model_provenance_sha256"
            ],
            candidate_metadata_sha256=manifest[
                "candidate_metadata_sha256"
            ],
            candidates=tuple(candidates),
            manifest_sha256=manifest_sha256,
            created_at=str(manifest["created_at"]),
            idempotent_replay=idempotent_replay,
        )

    def _parse_manifest_candidate(
        self,
        raw: Any,
        overlay_id: str,
        artifacts_root: Path,
    ) -> tuple[CandidateEvidenceOverlayCandidate, dict[str, Any]]:
        if not isinstance(raw, dict):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay candidate is invalid"
            )
        metadata_keys = {
            "candidate_id",
            "issue_type",
            "start_seconds",
            "end_seconds",
            "representative_timestamp_seconds",
            "representative_frame_id",
            "representative_frame_sha256",
            "score",
            "normalized_box",
            "ocr_text",
            "urgent_review",
            "supporting_frame_ids",
            "proposal_sources",
            "representative_proposal_provenance",
            "proposal_provenance_sha256",
        }
        if set(raw) != metadata_keys | {"image", "clip"}:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay candidate keys are invalid"
            )
        metadata_row = {key: raw[key] for key in metadata_keys}
        try:
            candidate_seed = DenseCandidateSeed(
                candidate_id=metadata_row["candidate_id"],
                issue_type=metadata_row["issue_type"],
                start_seconds=metadata_row["start_seconds"],
                end_seconds=metadata_row["end_seconds"],
                representative_timestamp_seconds=metadata_row[
                    "representative_timestamp_seconds"
                ],
                representative_frame_id=metadata_row[
                    "representative_frame_id"
                ],
                representative_frame_sha256=metadata_row[
                    "representative_frame_sha256"
                ],
                score=metadata_row["score"],
                normalized_box=metadata_row["normalized_box"],
                ocr_text=metadata_row["ocr_text"],
                urgent_review=metadata_row["urgent_review"],
                supporting_frame_ids=tuple(
                    metadata_row["supporting_frame_ids"]
                ),
                proposal_sources=tuple(
                    metadata_row["proposal_sources"]
                ),
                representative_proposal_provenance=metadata_row[
                    "representative_proposal_provenance"
                ],
                proposal_provenance_sha256=metadata_row[
                    "proposal_provenance_sha256"
                ],
            )
            normalized = _validated_candidate(
                candidate_seed,
                frame_id_pattern=(
                    _RESCAN_EVIDENCE_ID
                    if (
                        "grounding_dino_review_sidecar"
                        in candidate_seed.proposal_sources
                    )
                    else _FRAME_ID
                ),
            )
        except CandidateEvidenceOverlayIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay candidate is invalid"
            ) from error
        if normalized != metadata_row:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay candidate normalization changed"
            )
        image = self._parse_artifact_metadata(
            raw["image"],
            overlay_id,
            candidate_seed.candidate_id,
            "image",
        )
        clip = self._parse_artifact_metadata(
            raw["clip"],
            overlay_id,
            candidate_seed.candidate_id,
            "clip",
        )
        self._verify_overlay_artifact(
            artifacts_root / f"{candidate_seed.candidate_id}.jpg",
            artifacts_root,
            image,
            "image",
        )
        self._verify_overlay_artifact(
            artifacts_root / f"{candidate_seed.candidate_id}.mp4",
            artifacts_root,
            clip,
            "clip",
        )
        expected_start = max(
            0.0,
            float(
                normalized["representative_timestamp_seconds"]
            )
            - self._limits.clip_duration_seconds / 2.0,
        )
        if raw["clip"]["start_seconds"] != round(expected_start, 6):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay clip window does not match"
            )
        typed_candidate = dict(normalized)
        typed_candidate["supporting_frame_ids"] = tuple(
            normalized["supporting_frame_ids"]
        )
        typed_candidate["proposal_sources"] = tuple(
            normalized["proposal_sources"]
        )
        return (
            CandidateEvidenceOverlayCandidate(
                **typed_candidate,
                image=image,
                clip=clip,
            ),
            normalized,
        )

    def _parse_artifact_metadata(
        self,
        raw: Any,
        overlay_id: str,
        candidate_id: str,
        kind: str,
    ) -> CandidateEvidenceArtifact:
        if not isinstance(raw, dict):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay artifact metadata is invalid"
            )
        common_keys = {
            "artifact_id",
            "relative_path",
            "media_type",
            "size_bytes",
            "sha256",
        }
        expected_keys = (
            common_keys
            if kind == "image"
            else common_keys
            | {
                "start_seconds",
                "duration_seconds",
                "silent",
                "video_codec",
            }
        )
        expected_suffix = ".jpg" if kind == "image" else ".mp4"
        expected_media = "image/jpeg" if kind == "image" else "video/mp4"
        if (
            set(raw) != expected_keys
            or raw.get("artifact_id")
            != _artifact_id(overlay_id, candidate_id, kind)
            or _ARTIFACT_ID.fullmatch(
                str(raw.get("artifact_id", ""))
            )
            is None
            or raw.get("relative_path")
            != f"artifacts/{candidate_id}{expected_suffix}"
            or raw.get("media_type") != expected_media
            or isinstance(raw.get("size_bytes"), bool)
            or not isinstance(raw.get("size_bytes"), int)
            or not 1 <= raw["size_bytes"] <= (
                self._limits.max_image_bytes
                if kind == "image"
                else self._limits.max_clip_bytes
            )
            or _SHA256.fullmatch(str(raw.get("sha256", ""))) is None
            or (
                kind == "clip"
                and (
                    raw.get("duration_seconds")
                    != round(self._limits.clip_duration_seconds, 6)
                    or raw.get("silent") is not True
                    or raw.get("video_codec") != "h264"
                    or isinstance(raw.get("start_seconds"), bool)
                    or not isinstance(
                        raw.get("start_seconds"), (int, float)
                    )
                    or not math.isfinite(
                        float(raw["start_seconds"])
                    )
                    or raw["start_seconds"] < 0
                )
            )
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay artifact metadata is invalid"
            )
        return CandidateEvidenceArtifact(
            artifact_id=raw["artifact_id"],
            media_type=raw["media_type"],
            size_bytes=raw["size_bytes"],
            sha256=raw["sha256"],
        )

    def _verify_overlay_artifact(
        self,
        path: Path,
        artifacts_root: Path,
        metadata: CandidateEvidenceArtifact,
        kind: str,
    ) -> None:
        try:
            root = artifacts_root.resolve(strict=True)
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay artifact is unavailable"
            ) from error
        if (
            resolved.parent != root
            or path.is_symlink()
            or _is_reparse_point(path)
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay artifact containment failed"
            )
        body, digest, size, _mtime = _read_regular(
            resolved,
            maximum_bytes=(
                self._limits.max_image_bytes
                if kind == "image"
                else self._limits.max_clip_bytes
            ),
            expected_size=metadata.size_bytes,
            return_body=True,
        )
        if (
            digest != metadata.sha256
            or size != metadata.size_bytes
            or (
                kind == "image"
                and (
                    metadata.media_type != "image/jpeg"
                    or not _jpeg_magic(body)
                )
            )
            or (
                kind == "clip"
                and (
                    metadata.media_type != "video/mp4"
                    or not _mp4_magic(body[:32])
                )
            )
        ):
            raise CandidateEvidenceOverlayIntegrityError(
                "candidate overlay artifact failed verification"
            )


__all__ = [
    "CANDIDATE_EVIDENCE_OVERLAY_SCHEMA",
    "CandidateEvidenceArtifact",
    "CandidateEvidenceBindingResolver",
    "CandidateEvidenceArtifactRootResolver",
    "CandidateEvidenceCommandRunner",
    "CandidateEvidenceMediaCommand",
    "CandidateEvidenceMediaCommandOutput",
    "CandidateEvidenceOverlayCancelled",
    "CandidateEvidenceOverlayCandidate",
    "CandidateEvidenceOverlayConflict",
    "CandidateEvidenceOverlayError",
    "CandidateEvidenceOverlayIntegrityError",
    "CandidateEvidenceOverlayLimits",
    "CandidateEvidenceOverlayNotFound",
    "CandidateEvidenceOverlayOutputLimit",
    "CandidateEvidenceOverlayResult",
    "CandidateEvidenceOverlayStore",
    "DenseCandidateFrameResolver",
    "ResolvedCandidateEvidenceArtifact",
    "ResolvedCandidateEvidenceBinding",
    "ResolvedDenseCandidateFrame",
    "run_candidate_evidence_command",
]
