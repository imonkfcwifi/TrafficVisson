"""Bridge-owned execution of optional, review-only command proposal models.

The renderer supplies a natural-language labeling request to the normal
auto-label API.  It never supplies an executable, model path, output path, or
subprocess argument.  This module owns those details and only enables a runner
installed at one fixed ProgramData location after verifying its manifest,
executable, model manifest, and model artifacts.

The runner protocol deliberately returns *proposals*, not labels.  Callers must
persist the result through :class:`ExperimentalProposalStore`, which stamps the
completed-candidate evidence hashes and enforces ``review_only=True`` and
``automatic_labeling_allowed=False``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlsplit
from uuid import UUID

from .analysis_jobs import ResolvedAnalysisArtifact
from .auto_labeling import validate_command_plan
from .contracts import (
    ExperimentalProposalEvaluation,
    ExperimentalProposalModel,
    ExperimentalProposalPrediction,
)


RUNNER_MANIFEST_SCHEMA = "trafficvisson.command-proposal-runner.v1"
RUNNER_REQUEST_SCHEMA = "trafficvisson.command-proposal-request.v1"
RUNNER_RESPONSE_SCHEMA = "trafficvisson.command-proposal-response.v1"
RUNNER_EXECUTABLE_NAME = "trafficsupervision-command-proposal-runner.exe"
RUNNER_MANIFEST_NAME = "runner-manifest.json"
MODEL_DIRECTORY_NAME = "model-pack"
MODEL_MANIFEST_NAME = "manifest.json"
MODEL_MANIFEST_SCHEMA = "trafficvisson.command-proposal-model-pack.v2"

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
MAX_EXECUTABLE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MODEL_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024
MAX_MODEL_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
MAX_GENERATION_CANDIDATES = 512
RUNNER_TIMEOUT_SECONDS = 540.0

# ProgramData discovery is intentionally disabled until a reviewed production
# runner manifest hash is pinned in the bridge build.  A manifest cannot trust
# the executable hash it declares about itself.  Tests and future signed
# installers may inject a runner with an explicit out-of-band pinned hash.
PINNED_PROGRAMDATA_RUNNER_MANIFEST_SHA256: Optional[str] = (
    "dff0d3ab3204c3728245cc7bc4e91fa73d3b08b234ba853916cd518e2e5060c6"
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")
_SAFE_ARTIFACT_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SAFE_LONG_VERSION = re.compile(
    r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,127}$"
)
_SPDX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_CUDA_VERSION = re.compile(r"^[0-9]{1,2}\.[0-9]{1,2}$")


class CommandProposalGenerationError(ValueError):
    """A configured runner or its response violated the safety contract."""


class CommandProposalGenerator(Protocol):
    """Dependency-injection contract for bridge-owned proposal generation."""

    def status(self) -> Mapping[str, Any]:
        """Return scrubbed installation/manifest readiness without execution."""
        ...

    def generate(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        recipe_id: str,
        command_plan: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        resolve_image: Callable[
            [UUID, UUID, str], ResolvedAnalysisArtifact
        ],
    ) -> "CommandProposalGenerationResult":
        """Evaluate one completed analysis job without creating labels."""


@dataclass(frozen=True)
class CommandProposalGenerationResult:
    """A bounded set of record-ready, review-only experimental proposals."""

    available: bool
    generated: bool
    message_ko: str
    evaluated_candidate_count: int = 0
    model: Optional[dict[str, Any]] = None
    evaluation: Optional[dict[str, Any]] = None
    proposals: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.available, bool)
            or not isinstance(self.generated, bool)
            or not isinstance(self.message_ko, str)
            or not 1 <= len(self.message_ko) <= 500
            or isinstance(self.evaluated_candidate_count, bool)
            or not isinstance(self.evaluated_candidate_count, int)
            or not 0
            <= self.evaluated_candidate_count
            <= MAX_GENERATION_CANDIDATES
        ):
            raise CommandProposalGenerationError(
                "proposal generation result is invalid"
            )
        if not self.available:
            if (
                self.generated
                or self.model is not None
                or self.evaluation is not None
                or self.proposals
                or self.evaluated_candidate_count != 0
            ):
                raise CommandProposalGenerationError(
                    "unavailable proposal generator returned data"
                )
            return
        if not self.generated:
            raise CommandProposalGenerationError(
                "available proposal generator did not complete"
            )
        if self.model is None or self.evaluation is None:
            raise CommandProposalGenerationError(
                "generated proposals are missing provenance"
            )
        try:
            ExperimentalProposalModel.model_validate(self.model)
            ExperimentalProposalEvaluation.model_validate(self.evaluation)
        except Exception as error:
            raise CommandProposalGenerationError(
                "generated proposal provenance is invalid"
            ) from error
        if not 1 <= self.evaluated_candidate_count <= MAX_GENERATION_CANDIDATES:
            raise CommandProposalGenerationError(
                "generated evaluation count is invalid"
            )
        if len(self.proposals) > self.evaluated_candidate_count:
            raise CommandProposalGenerationError(
                "generated proposal count is invalid"
            )
        if any(
            item.get("status") != "classified"
            or not item.get("predictions")
            for item in self.proposals
        ):
            raise CommandProposalGenerationError(
                "only classified rows may leave the command runner boundary"
            )


class UnavailableCommandProposalGenerator:
    """Honest default when no verified optional ML runner is installed."""

    def __init__(
        self,
        message_ko: str = (
            "검증된 로컬 AI 명령형 모델 실행기가 설치되어 있지 않습니다."
        ),
    ) -> None:
        self._message_ko = message_ko

    def status(self) -> Mapping[str, Any]:
        return {
            "state": "unavailable",
            "error_code": "model_pack_not_configured",
        }

    def generate(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        recipe_id: str,
        command_plan: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        resolve_image: Callable[
            [UUID, UUID, str], ResolvedAnalysisArtifact
        ],
    ) -> CommandProposalGenerationResult:
        del (
            project_id,
            analysis_job_id,
            recipe_id,
            command_plan,
            candidates,
            resolve_image,
        )
        return CommandProposalGenerationResult(
            available=False,
            generated=False,
            message_ko=self._message_ko,
        )


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _safe_regular_file(
    path: Path,
    *,
    root: Path,
    label: str,
    maximum_bytes: int,
) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        information = path.stat()
    except OSError as error:
        raise CommandProposalGenerationError(f"{label} is unavailable") from error
    if (
        root.is_symlink()
        or _is_reparse_point(root)
        or not root.is_dir()
        or path.is_symlink()
        or _is_reparse_point(path)
        or resolved.parent != resolved_root
        or not stat.S_ISREG(information.st_mode)
        or information.st_nlink != 1
        or information.st_size <= 0
        or information.st_size > maximum_bytes
    ):
        raise CommandProposalGenerationError(f"{label} is unsafe")
    return resolved


def _read_bounded_json(
    path: Path,
    *,
    root: Path,
    label: str,
    maximum_bytes: int = MAX_MANIFEST_BYTES,
) -> tuple[dict[str, Any], bytes]:
    resolved = _safe_regular_file(
        path,
        root=root,
        label=label,
        maximum_bytes=maximum_bytes,
    )
    try:
        before = resolved.stat()
        raw = resolved.read_bytes()
        after = resolved.stat()
    except OSError as error:
        raise CommandProposalGenerationError(f"{label} is unavailable") from error
    if (
        len(raw) != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise CommandProposalGenerationError(f"{label} changed while reading")

    def reject_duplicate_pairs(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise CommandProposalGenerationError(
                    f"{label} contains duplicate keys"
                )
            value[key] = item
        return value

    try:
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                CommandProposalGenerationError(
                    f"{label} contains non-finite numbers"
                )
            ),
        )
    except CommandProposalGenerationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CommandProposalGenerationError(f"{label} is invalid") from error
    if not isinstance(decoded, dict):
        raise CommandProposalGenerationError(f"{label} must be an object")
    return decoded, raw


def _sha256(path: Path, *, maximum_bytes: int) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise CommandProposalGenerationError(
                        "runner artifact exceeds its byte limit"
                    )
                digest.update(chunk)
    except OSError as error:
        raise CommandProposalGenerationError(
            "runner artifact is unavailable"
        ) from error
    return digest.hexdigest()


def _safe_model_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CommandProposalGenerationError(
            "model artifact path is invalid"
        )
    candidate = Path(value)
    if (
        candidate.is_absolute()
        or len(candidate.parts) > 8
        or any(
            part in {"", ".", ".."} or not _SAFE_ARTIFACT_PART.fullmatch(part)
            for part in candidate.parts
        )
    ):
        raise CommandProposalGenerationError(
            "model artifact path is invalid"
        )
    return candidate


def _verify_model_artifacts(
    model_root: Path,
    manifest: Mapping[str, Any],
) -> None:
    try:
        resolved_model_root = model_root.resolve(strict=True)
    except OSError as error:
        raise CommandProposalGenerationError(
            "model pack root is unavailable"
        ) from error
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 256:
        raise CommandProposalGenerationError(
            "model manifest artifacts are invalid"
        )
    seen: set[str] = set()
    total = 0
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise CommandProposalGenerationError(
                "model manifest artifact entry is invalid"
            )
        relative = _safe_model_relative_path(row["path"])
        normalized = relative.as_posix()
        size = row["size_bytes"]
        sha256_lower = row["sha256"]
        if (
            normalized in seen
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= MAX_MODEL_ARTIFACT_BYTES
            or not isinstance(sha256_lower, str)
            or not _SHA256.fullmatch(sha256_lower)
        ):
            raise CommandProposalGenerationError(
                "model manifest artifact entry is invalid"
            )
        seen.add(normalized)
        total += size
        if total > MAX_MODEL_TOTAL_BYTES:
            raise CommandProposalGenerationError(
                "model pack exceeds its byte limit"
            )
        current_directory = model_root
        for directory_part in relative.parts[:-1]:
            current_directory = current_directory / directory_part
            try:
                resolved_directory = current_directory.resolve(strict=True)
            except OSError as error:
                raise CommandProposalGenerationError(
                    "model artifact directory is unavailable"
                ) from error
            try:
                resolved_directory.relative_to(resolved_model_root)
            except ValueError as error:
                raise CommandProposalGenerationError(
                    "model artifact escaped its pack root"
                ) from error
            if (
                current_directory.is_symlink()
                or _is_reparse_point(current_directory)
                or not current_directory.is_dir()
            ):
                raise CommandProposalGenerationError(
                    "model artifact directory is unsafe"
                )
        path = model_root / relative
        resolved = _safe_regular_file(
            path,
            root=path.parent,
            label="model artifact",
            maximum_bytes=MAX_MODEL_ARTIFACT_BYTES,
        )
        try:
            resolved.relative_to(resolved_model_root)
        except ValueError as error:
            raise CommandProposalGenerationError(
                "model artifact escaped its pack root"
            ) from error
        try:
            actual_size = resolved.stat().st_size
        except OSError as error:
            raise CommandProposalGenerationError(
                "model artifact is unavailable"
            ) from error
        if (
            actual_size != size
            or _sha256(
                resolved,
                maximum_bytes=MAX_MODEL_ARTIFACT_BYTES,
            )
            != sha256_lower
        ):
            raise CommandProposalGenerationError(
                "model artifact integrity verification failed"
            )


def _verify_model_pack_metadata(
    manifest: Mapping[str, Any],
    *,
    expected_pack_id: str,
    expected_pack_version: str,
) -> None:
    required = {
        "schema_version",
        "pack_id",
        "pack_version",
        "model",
        "source",
        "license",
        "gpu_requirements",
        "inference",
        "review_policy",
        "artifacts",
    }
    if set(manifest) != required:
        raise CommandProposalGenerationError(
            "command proposal model metadata fields are invalid"
        )
    if (
        manifest["schema_version"] != MODEL_MANIFEST_SCHEMA
        or manifest["pack_id"] != expected_pack_id
        or manifest["pack_version"] != expected_pack_version
    ):
        raise CommandProposalGenerationError(
            "command proposal model identity is invalid"
        )

    model = manifest["model"]
    if (
        not isinstance(model, Mapping)
        or set(model)
        != {"name", "version", "task", "format", "selected_artifact"}
        or not isinstance(model["name"], str)
        or not 1 <= len(model["name"].strip()) <= 128
        or not isinstance(model["version"], str)
        or not _SAFE_LONG_VERSION.fullmatch(model["version"])
        or model["task"] != "zero_shot_object_detection"
        or model["format"] != "onnx"
        or not isinstance(model["selected_artifact"], str)
        or _safe_model_relative_path(
            model["selected_artifact"]
        ).suffix.casefold()
        != ".onnx"
    ):
        raise CommandProposalGenerationError(
            "command proposal model identity metadata is invalid"
        )

    source = manifest["source"]
    if (
        not isinstance(source, Mapping)
        or set(source)
        != {
            "repository_url",
            "repo_id",
            "resolved_revision",
            "selected_model",
        }
        or not isinstance(source["repo_id"], str)
        or not 1 <= len(source["repo_id"]) <= 128
        or not isinstance(source["resolved_revision"], str)
        or not re.fullmatch(r"[a-f0-9]{40,64}", source["resolved_revision"])
        or source["selected_model"] != model["selected_artifact"]
        or not _safe_https_url(source["repository_url"])
    ):
        raise CommandProposalGenerationError(
            "command proposal model source metadata is invalid"
        )

    licence = manifest["license"]
    if (
        not isinstance(licence, Mapping)
        or set(licence)
        != {
            "weights_spdx",
            "runtime_spdx",
            "license_url",
            "commercial_use_confirmed",
            "redistribution_allowed",
            "notice_artifact",
        }
        or not isinstance(licence["weights_spdx"], str)
        or not _SPDX.fullmatch(licence["weights_spdx"])
        or not isinstance(licence["runtime_spdx"], str)
        or not _SPDX.fullmatch(licence["runtime_spdx"])
        or licence["commercial_use_confirmed"] is not True
        or not isinstance(licence["redistribution_allowed"], bool)
        or licence["notice_artifact"] != "THIRD-PARTY-NOTICES.md"
        or not _safe_https_url(licence["license_url"])
    ):
        raise CommandProposalGenerationError(
            "command proposal model license metadata is invalid"
        )

    gpu = manifest["gpu_requirements"]
    if (
        not isinstance(gpu, Mapping)
        or set(gpu)
        != {
            "required",
            "cpu_fallback",
            "recommended_min_vram_mb",
            "cuda_min_version",
        }
        or not isinstance(gpu["required"], bool)
        or not isinstance(gpu["cpu_fallback"], bool)
        or isinstance(gpu["recommended_min_vram_mb"], bool)
        or not isinstance(gpu["recommended_min_vram_mb"], int)
        or not 0 <= gpu["recommended_min_vram_mb"] <= 131_072
        or not (
            gpu["cuda_min_version"] is None
            or (
                isinstance(gpu["cuda_min_version"], str)
                and _CUDA_VERSION.fullmatch(gpu["cuda_min_version"])
            )
        )
        or (gpu["required"] and gpu["cuda_min_version"] is None)
    ):
        raise CommandProposalGenerationError(
            "command proposal model GPU metadata is invalid"
        )

    policy = manifest["review_policy"]
    if (
        not isinstance(policy, Mapping)
        or policy.get("review_only") is not True
        or policy.get("automatic_labeling_allowed") is not False
        or policy.get("incident_creation_allowed") is not False
        or policy.get("model_promotion_allowed") is not False
    ):
        raise CommandProposalGenerationError(
            "command proposal model review boundary is invalid"
        )

    artifact_paths = {
        row.get("path")
        for row in manifest["artifacts"]
        if isinstance(row, Mapping)
    }
    if (
        model["selected_artifact"] not in artifact_paths
        or licence["notice_artifact"] not in artifact_paths
    ):
        raise CommandProposalGenerationError(
            "command proposal model metadata references missing artifacts"
        )


def _safe_https_url(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 2_048:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CommandProposalGenerationError(
            "runner request is not finite JSON"
        ) from error


def _windows_kill_on_close_job(process: subprocess.Popen[bytes]):
    """Assign ``process`` to a kill-on-close Job Object.

    The returned close callback is also the process-tree termination primitive.
    Failure is fatal on Windows: the optional runner must not outlive the
    bridge-owned timeout or cancellation boundary.
    """

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(error, "SetInformationJobObject failed")
        if not kernel32.AssignProcessToJobObject(
            job,
            wintypes.HANDLE(int(process._handle)),  # type: ignore[attr-defined]
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(error, "AssignProcessToJobObject failed")

        def close_job() -> None:
            nonlocal job
            if job:
                kernel32.CloseHandle(job)
                job = None

        return close_job
    except Exception:
        raise


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    close_job: Optional[Callable[[], None]],
) -> None:
    if close_job is not None:
        close_job()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_bounded_process(
    argv: Sequence[str],
    *,
    input: bytes,
    cwd: str,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run the pinned executable with file-backed, size-bounded output."""

    if (
        not isinstance(input, bytes)
        or len(input) > MAX_MANIFEST_BYTES
        or not isinstance(timeout, (int, float))
        or not 1 <= float(timeout) <= RUNNER_TIMEOUT_SECONDS
    ):
        raise CommandProposalGenerationError(
            "command proposal process request is invalid"
        )
    started = time.monotonic()
    with (
        tempfile.TemporaryFile() as stdin_file,
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        stdin_file.write(input)
        stdin_file.seek(0)
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0
        )
        popen_kwargs: dict[str, Any] = {
            "stdin": stdin_file,
            "stdout": stdout_file,
            "stderr": stderr_file,
            "cwd": cwd,
            "env": dict(env),
            "shell": False,
            "close_fds": True,
            "creationflags": creation_flags,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(list(argv), **popen_kwargs)
        except (OSError, ValueError) as error:
            raise CommandProposalGenerationError(
                "command proposal runner could not start"
            ) from error
        close_job: Optional[Callable[[], None]] = None
        try:
            close_job = _windows_kill_on_close_job(process)
        except Exception as error:
            _terminate_process_tree(process, None)
            raise CommandProposalGenerationError(
                "command proposal runner could not enter its process boundary"
            ) from error
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started
                stdout_size = os.fstat(stdout_file.fileno()).st_size
                stderr_size = os.fstat(stderr_file.fileno()).st_size
                if (
                    elapsed > float(timeout)
                    or stdout_size > MAX_RESPONSE_BYTES
                    or stderr_size > MAX_STDERR_BYTES
                ):
                    _terminate_process_tree(process, close_job)
                    close_job = None
                    raise CommandProposalGenerationError(
                        "command proposal runner exceeded a runtime limit"
                    )
                time.sleep(0.05)
            return_code = process.wait(timeout=5)
            if close_job is not None:
                # Closing the job after the protocol process exits also kills
                # any child it attempted to leave running.
                close_job()
                close_job = None
            if (
                os.fstat(stdout_file.fileno()).st_size > MAX_RESPONSE_BYTES
                or os.fstat(stderr_file.fileno()).st_size > MAX_STDERR_BYTES
            ):
                raise CommandProposalGenerationError(
                    "command proposal runner exceeded an output limit"
                )
            stdout_file.seek(0)
            stderr_file.seek(0)
            return subprocess.CompletedProcess(
                args=list(argv),
                returncode=return_code,
                stdout=stdout_file.read(MAX_RESPONSE_BYTES + 1),
                stderr=stderr_file.read(MAX_STDERR_BYTES + 1),
            )
        finally:
            if process.poll() is None:
                _terminate_process_tree(process, close_job)
            elif close_job is not None:
                close_job()


class FixedManifestCommandProposalGenerator:
    """Run a fixed, hash-verified optional ProgramData executable.

    The executable name, model directory, protocol, timeout, and request shape
    are compiled into the bridge.  The renderer cannot override them.
    """

    def __init__(
        self,
        runner_root: Path,
        *,
        trusted_manifest_sha256: str,
        process_runner: Optional[
            Callable[..., subprocess.CompletedProcess[bytes]]
        ] = None,
    ) -> None:
        root = Path(runner_root)
        if not root.is_absolute():
            raise CommandProposalGenerationError(
                "command proposal runner root must be absolute"
            )
        if (
            not isinstance(trusted_manifest_sha256, str)
            or not _SHA256.fullmatch(trusted_manifest_sha256)
        ):
            raise CommandProposalGenerationError(
                "command proposal runner requires a pinned manifest hash"
            )
        self._root = root
        self._trusted_manifest_sha256 = trusted_manifest_sha256
        self._process_runner = process_runner or _run_bounded_process

    @classmethod
    def _from_pinned_root(
        cls,
        runner_root: Path,
        *,
        unavailable_message: str,
    ) -> CommandProposalGenerator:
        if PINNED_PROGRAMDATA_RUNNER_MANIFEST_SHA256 is None:
            return UnavailableCommandProposalGenerator(
                "로컬 AI 실행기 승인 해시가 이 앱 빌드에 고정되어 있지 않습니다."
            )
        try:
            instance = cls(
                runner_root,
                trusted_manifest_sha256=(
                    PINNED_PROGRAMDATA_RUNNER_MANIFEST_SHA256
                ),
            )
            instance._load_verified_runner()
        except CommandProposalGenerationError:
            return UnavailableCommandProposalGenerator(unavailable_message)
        return instance

    @classmethod
    def from_environment_or_program_data(
        cls,
        *,
        program_data: Optional[str] = None,
    ) -> CommandProposalGenerator:
        packaged_root = os.environ.get(
            "TRAFFICSUPERVISION_COMMAND_PROPOSAL_RUNNER_ROOT"
        )
        if packaged_root:
            candidate = Path(packaged_root)
            if not candidate.is_absolute():
                return UnavailableCommandProposalGenerator(
                    "프로그램에 포함된 로컬 AI 실행기 경로가 올바르지 않습니다."
                )
            return cls._from_pinned_root(
                candidate,
                unavailable_message=(
                    "프로그램에 포함된 로컬 AI 실행기가 없거나 "
                    "무결성 검증을 통과하지 못했습니다."
                ),
            )
        return cls.from_program_data(program_data=program_data)

    @classmethod
    def from_program_data(
        cls,
        *,
        program_data: Optional[str] = None,
    ) -> CommandProposalGenerator:
        base = program_data if program_data is not None else os.environ.get(
            "PROGRAMDATA"
        )
        if not base:
            return UnavailableCommandProposalGenerator()
        runner_root = (
            Path(base)
            / "TrafficSuperVision"
            / "CommandProposalRunner"
            / "grounding-dino-v1"
        )
        return cls._from_pinned_root(
            runner_root,
            unavailable_message=(
                "설치된 로컬 AI 실행기가 없거나 무결성 검증을 통과하지 못했습니다."
            ),
        )

    def _load_verified_runner(
        self,
    ) -> tuple[Path, Path, dict[str, Any], str]:
        try:
            resolved_root = self._root.resolve(strict=True)
        except OSError as error:
            raise CommandProposalGenerationError(
                "command proposal runner is not installed"
            ) from error
        if (
            self._root.is_symlink()
            or _is_reparse_point(self._root)
            or not self._root.is_dir()
        ):
            raise CommandProposalGenerationError(
                "command proposal runner root is unsafe"
            )
        manifest, raw_manifest = _read_bounded_json(
            resolved_root / RUNNER_MANIFEST_NAME,
            root=resolved_root,
            label="command proposal runner manifest",
        )
        manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
        if manifest_sha256 != self._trusted_manifest_sha256:
            raise CommandProposalGenerationError(
                "command proposal runner manifest is not trusted by this build"
            )
        required_keys = {
            "schema_version",
            "runner_id",
            "runner_version",
            "protocol_version",
            "executable_sha256",
            "model_manifest_sha256",
            "model",
            "evaluation",
        }
        if set(manifest) != required_keys:
            raise CommandProposalGenerationError(
                "command proposal runner manifest fields are invalid"
            )
        if (
            manifest["schema_version"] != RUNNER_MANIFEST_SCHEMA
            or manifest["protocol_version"] != RUNNER_REQUEST_SCHEMA
            or not isinstance(manifest["runner_id"], str)
            or not _SAFE_IDENTIFIER.fullmatch(manifest["runner_id"])
            or not isinstance(manifest["runner_version"], str)
            or not _SAFE_VERSION.fullmatch(manifest["runner_version"])
            or not isinstance(manifest["executable_sha256"], str)
            or not _SHA256.fullmatch(manifest["executable_sha256"])
            or not isinstance(manifest["model_manifest_sha256"], str)
            or not _SHA256.fullmatch(manifest["model_manifest_sha256"])
        ):
            raise CommandProposalGenerationError(
                "command proposal runner manifest is invalid"
            )
        try:
            model = ExperimentalProposalModel.model_validate(
                manifest["model"]
            )
            evaluation = ExperimentalProposalEvaluation.model_validate(
                manifest["evaluation"]
            )
        except Exception as error:
            raise CommandProposalGenerationError(
                "command proposal runner provenance is invalid"
            ) from error
        if model.provider != "transformers_grounding_dino":
            raise CommandProposalGenerationError(
                "command proposal runner model provider is unsupported"
            )
        executable = _safe_regular_file(
            resolved_root / RUNNER_EXECUTABLE_NAME,
            root=resolved_root,
            label="command proposal runner executable",
            maximum_bytes=MAX_EXECUTABLE_BYTES,
        )
        if (
            _sha256(executable, maximum_bytes=MAX_EXECUTABLE_BYTES)
            != manifest["executable_sha256"]
        ):
            raise CommandProposalGenerationError(
                "command proposal runner executable integrity check failed"
            )
        model_root = resolved_root / MODEL_DIRECTORY_NAME
        try:
            resolved_model_root = model_root.resolve(strict=True)
        except OSError as error:
            raise CommandProposalGenerationError(
                "command proposal model pack is unavailable"
            ) from error
        if (
            model_root.is_symlink()
            or _is_reparse_point(model_root)
            or not model_root.is_dir()
            or resolved_model_root.parent != resolved_root
        ):
            raise CommandProposalGenerationError(
                "command proposal model pack is unsafe"
            )
        model_manifest, raw_model_manifest = _read_bounded_json(
            resolved_model_root / MODEL_MANIFEST_NAME,
            root=resolved_model_root,
            label="command proposal model manifest",
        )
        if (
            hashlib.sha256(raw_model_manifest).hexdigest()
            != manifest["model_manifest_sha256"]
            or manifest["model"]["manifest_sha256"]
            != manifest["model_manifest_sha256"]
        ):
            raise CommandProposalGenerationError(
                "command proposal model manifest integrity check failed"
            )
        _verify_model_artifacts(resolved_model_root, model_manifest)
        _verify_model_pack_metadata(
            model_manifest,
            expected_pack_id=model.pack_id,
            expected_pack_version=model.pack_version,
        )
        return (
            executable,
            resolved_model_root,
            {
                **manifest,
                "model": model.model_dump(mode="json"),
                "evaluation": evaluation.model_dump(mode="json"),
            },
            manifest_sha256,
        )

    def status(self) -> Mapping[str, Any]:
        try:
            _executable, _model_root, manifest, manifest_sha256 = (
                self._load_verified_runner()
            )
        except CommandProposalGenerationError:
            return {
                "state": "failed",
                "error_code": "model_pack_invalid",
            }
        model = manifest["model"]
        return {
            "state": "ready",
            "error_code": None,
            "provider": model["provider"],
            "pack_id": model["pack_id"],
            "pack_version": model["pack_version"],
            "manifest_sha256": model["manifest_sha256"],
            "runner_manifest_sha256": manifest_sha256,
            "capabilities": ["natural_language_box_proposals"],
            "review_only": True,
            "bulk_frame_detection_provider": "local_model",
        }

    def generate(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        recipe_id: str,
        command_plan: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        resolve_image: Callable[
            [UUID, UUID, str], ResolvedAnalysisArtifact
        ],
    ) -> CommandProposalGenerationResult:
        if not isinstance(project_id, UUID) or not isinstance(
            analysis_job_id, UUID
        ):
            raise CommandProposalGenerationError(
                "proposal generation identity is invalid"
            )
        if not isinstance(recipe_id, str) or not _SHA256.fullmatch(recipe_id):
            raise CommandProposalGenerationError(
                "proposal generation recipe is invalid"
            )
        validated_plan = validate_command_plan(command_plan)
        if (
            not isinstance(candidates, Sequence)
            or isinstance(candidates, (str, bytes, bytearray))
            or not 1 <= len(candidates) <= MAX_GENERATION_CANDIDATES
        ):
            raise CommandProposalGenerationError(
                "completed candidate scope exceeds the runner limit"
            )
        if not callable(resolve_image):
            raise CommandProposalGenerationError(
                "proposal image resolver is invalid"
            )
        executable, model_root, manifest, manifest_sha256 = (
            self._load_verified_runner()
        )
        candidate_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise CommandProposalGenerationError(
                    "analysis candidate is invalid"
                )
            candidate_id = candidate.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or not _CANDIDATE_ID.fullmatch(candidate_id)
                or candidate_id in seen
            ):
                raise CommandProposalGenerationError(
                    "analysis candidate identity is invalid"
                )
            seen.add(candidate_id)
            artifact = resolve_image(
                project_id,
                analysis_job_id,
                candidate_id,
            )
            if (
                not isinstance(artifact, ResolvedAnalysisArtifact)
                or artifact.media_type != "image/jpeg"
                or not artifact.path.is_absolute()
                or not _SHA256.fullmatch(artifact.sha256)
                or artifact.size_bytes <= 0
            ):
                raise CommandProposalGenerationError(
                    "analysis candidate image is invalid"
                )
            candidate_rows.append(
                {
                    "candidate_id": candidate_id,
                    "image_path": str(artifact.path),
                    "image_sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                }
            )
        request = {
            "schema_version": RUNNER_REQUEST_SCHEMA,
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "recipe_id": recipe_id,
            "command_sha256": validated_plan["command_sha256"],
            "prompt_sha256": validated_plan["prompt_sha256"],
            "issue_type": validated_plan["issue_type"],
            "positive_prompts": validated_plan["positive_prompts"][
                "grounding_dino"
            ],
            "negative_prompts": validated_plan["negative_prompts"],
            "runner_manifest_sha256": manifest_sha256,
            "model_manifest_sha256": manifest["model_manifest_sha256"],
            "model_pack_root": str(model_root),
            "candidates": candidate_rows,
        }
        encoded_request = _canonical_json(request)
        safe_environment = {
            key: value
            for key, value in {
                "SystemRoot": os.environ.get("SystemRoot"),
                "WINDIR": os.environ.get("WINDIR"),
                "TEMP": os.environ.get("TEMP"),
                "TMP": os.environ.get("TMP"),
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "NO_PROXY": "*",
            }.items()
            if value
        }
        try:
            completed = self._process_runner(
                [str(executable)],
                input=encoded_request,
                cwd=str(self._root.resolve(strict=True)),
                env=safe_environment,
                timeout=RUNNER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise CommandProposalGenerationError(
                "command proposal runner timed out"
            ) from error
        except (OSError, ValueError) as error:
            raise CommandProposalGenerationError(
                "command proposal runner could not start"
            ) from error
        if (
            completed.returncode != 0
            or not isinstance(completed.stdout, bytes)
            or not 1 <= len(completed.stdout) <= MAX_RESPONSE_BYTES
        ):
            raise CommandProposalGenerationError(
                "command proposal runner failed"
            )
        response = self._validate_response(
            completed.stdout,
            expected_project_id=project_id,
            expected_job_id=analysis_job_id,
            expected_recipe_id=recipe_id,
            expected_issue_type=validated_plan["issue_type"],
            expected_candidate_ids=seen,
            expected_runner_manifest_sha256=manifest_sha256,
            expected_model_manifest_sha256=manifest[
                "model_manifest_sha256"
            ],
        )
        evaluation = {
            **manifest["evaluation"],
            # A recipe-specific id lets repeated executions reuse an immutable
            # no-suggestion result without accidentally treating another
            # command's negatives as evidence for this command.
            "evaluation_id": f"cmd-{recipe_id}",
        }
        return CommandProposalGenerationResult(
            available=True,
            generated=True,
            message_ko=(
                f"로컬 명령형 AI 모델이 {len(candidate_rows)}개 프레임을 "
                "검사했고 결과를 사용자 검토용 선라벨로만 저장했습니다."
            ),
            evaluated_candidate_count=len(candidate_rows),
            model=manifest["model"],
            evaluation=evaluation,
            proposals=tuple(
                proposal
                for proposal in response["proposals"]
                if proposal["status"] == "classified"
            ),
        )

    @staticmethod
    def _validate_response(
        raw: bytes,
        *,
        expected_project_id: UUID,
        expected_job_id: UUID,
        expected_recipe_id: str,
        expected_issue_type: str,
        expected_candidate_ids: set[str],
        expected_runner_manifest_sha256: str,
        expected_model_manifest_sha256: str,
    ) -> dict[str, Any]:
        def reject_duplicate_pairs(
            pairs: list[tuple[str, Any]],
        ) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise CommandProposalGenerationError(
                        "runner response contains duplicate keys"
                    )
                value[key] = item
            return value

        try:
            decoded = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=reject_duplicate_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    CommandProposalGenerationError(
                        "runner response contains non-finite numbers"
                    )
                ),
            )
        except CommandProposalGenerationError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CommandProposalGenerationError(
                "command proposal runner response is invalid"
            ) from error
        expected_keys = {
            "schema_version",
            "project_id",
            "analysis_job_id",
            "recipe_id",
            "runner_manifest_sha256",
            "model_manifest_sha256",
            "proposals",
        }
        if not isinstance(decoded, dict) or set(decoded) != expected_keys:
            raise CommandProposalGenerationError(
                "command proposal runner response fields are invalid"
            )
        if (
            decoded["schema_version"] != RUNNER_RESPONSE_SCHEMA
            or decoded["project_id"] != str(expected_project_id)
            or decoded["analysis_job_id"] != str(expected_job_id)
            or decoded["recipe_id"] != expected_recipe_id
            or decoded["runner_manifest_sha256"]
            != expected_runner_manifest_sha256
            or decoded["model_manifest_sha256"]
            != expected_model_manifest_sha256
            or not isinstance(decoded["proposals"], list)
            or len(decoded["proposals"]) != len(expected_candidate_ids)
        ):
            raise CommandProposalGenerationError(
                "command proposal runner crossed its requested scope"
            )
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_proposal in decoded["proposals"]:
            if not isinstance(raw_proposal, dict) or set(raw_proposal) != {
                "candidate_id",
                "status",
                "predictions",
            }:
                raise CommandProposalGenerationError(
                    "runner proposal fields are invalid"
                )
            candidate_id = raw_proposal["candidate_id"]
            predictions = raw_proposal["predictions"]
            status = raw_proposal["status"]
            if (
                candidate_id not in expected_candidate_ids
                or candidate_id in seen
                or status not in {"classified", "no_suggestion"}
                or not isinstance(predictions, list)
                or len(predictions) > 32
                or (status == "classified") != bool(predictions)
            ):
                raise CommandProposalGenerationError(
                    "runner proposal scope is invalid"
                )
            seen.add(candidate_id)
            normalized_predictions: list[dict[str, Any]] = []
            for prediction in predictions:
                try:
                    validated = ExperimentalProposalPrediction.model_validate(
                        prediction
                    )
                except Exception as error:
                    raise CommandProposalGenerationError(
                        "runner prediction is invalid"
                    ) from error
                if (
                    validated.issue_type != expected_issue_type
                    or validated.calibration != "model_score"
                    or validated.model_score is None
                    or not math.isfinite(validated.model_score)
                ):
                    raise CommandProposalGenerationError(
                        "runner prediction changed the requested issue type"
                    )
                normalized_predictions.append(
                    validated.model_dump(mode="json")
                )
            normalized.append(
                {
                    "candidate_id": candidate_id,
                    "status": status,
                    "predictions": normalized_predictions,
                }
            )
        if seen != expected_candidate_ids:
            raise CommandProposalGenerationError(
                "runner response omitted a requested candidate"
            )
        normalized.sort(key=lambda row: row["candidate_id"])
        return {**decoded, "proposals": normalized}
