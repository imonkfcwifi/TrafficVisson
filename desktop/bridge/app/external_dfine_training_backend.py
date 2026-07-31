"""Hash-pinned subprocess boundary for an external D-FINE CUDA trainer.

The lightweight/frozen bridge does not have to import PyTorch or Transformers
when an independently installed TrafficSuperVision detector runtime is used.
This module deliberately does not accept an arbitrary command.  A server-owned
runtime manifest pins one allowlisted protocol implementation, its Python
executable, and its entrypoint by SHA-256.  Every process is launched with an
argument vector and ``shell=False``.

The external process receives one ephemeral request file.  It may write model
files only below a staging directory owned by the current training job.  The
bridge verifies CUDA provenance, all request bindings, the returned weight
hash, and the complete staging tree before moving ``detector/`` into the
project-model artifact directory.
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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Optional, Sequence

from .dataset_versions import canonical_json_bytes
from .local_object_detector_trainer import (
    DetectorBackendRequest,
    DetectorBackendResult,
    DetectorBoxPrediction,
    DetectorRuntimeInfo,
    DetectorTrainingCancelled,
    LocalDetectorModelPack,
)
from .project_model_lifecycle import (
    ProjectModelLifecycleError,
    ProjectModelTrainerUnavailable,
)
from .training_geometry import TrainingGeometryError, canonical_box


EXTERNAL_RUNTIME_MANIFEST_SCHEMA = (
    "trafficvisson.external-dfine-runtime-manifest.v1"
)
EXTERNAL_TRAINING_REQUEST_SCHEMA = (
    "trafficvisson.external-dfine-training-request.v1"
)
EXTERNAL_TRAINING_RESPONSE_SCHEMA = (
    "trafficvisson.external-dfine-training-response.v1"
)
EXTERNAL_EXECUTION_PROVENANCE_SCHEMA = (
    "trafficvisson.local-detector-execution-provenance.v1"
)
EXTERNAL_BACKEND_ID = "trafficvisson-external-dfine-local-cuda"
_ALLOWED_RUNTIMES = {
    (
        "trafficvisson-dfine-training-entrypoint",
        "1.0.0",
        "1.0",
    )
}
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_RUNTIME_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,95}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
_WINDOWS_REPARSE_ATTRIBUTE = 0x400
_MAX_RUNTIME_MANIFEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_PROGRESS_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_FILES = 512
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024 * 1024
_MAX_PREDICTIONS = 500_000


class ExternalDfineRuntimeError(ValueError):
    """The configured external runtime is unsafe or unverifiable."""


@dataclass(frozen=True)
class ExternalDfineRuntime:
    manifest_path: Path
    manifest_sha256: str
    runtime_id: str
    runtime_version: str
    protocol_version: str
    executable: Path
    executable_sha256: str
    executable_size_bytes: int
    entrypoint: Path
    entrypoint_sha256: str
    entrypoint_size_bytes: int
    probe_timeout_seconds: int
    training_timeout_seconds: int
    max_log_bytes: int
    poll_interval_ms: int

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": EXTERNAL_RUNTIME_MANIFEST_SCHEMA,
            "runtime_id": self.runtime_id,
            "runtime_version": self.runtime_version,
            "protocol_version": self.protocol_version,
            "manifest_sha256": self.manifest_sha256,
            "executable_sha256": self.executable_sha256,
            "entrypoint_sha256": self.entrypoint_sha256,
        }


def _is_link_or_reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(value.st_mode):
        return True
    return bool(
        getattr(value, "st_file_attributes", 0)
        & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _hash_file(path: Path) -> tuple[str, int]:
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        after = path.stat()
    except OSError as error:
        raise ExternalDfineRuntimeError(
            "an external D-FINE runtime file could not be read"
        ) from error
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ExternalDfineRuntimeError(
            "an external D-FINE runtime file changed while hashing"
        )
    return digest.hexdigest(), int(after.st_size)


def _strict_json_bytes(
    body: bytes,
    *,
    label: str,
) -> Mapping[str, Any]:
    def pairs(values: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ExternalDfineRuntimeError(
                    f"{label} contains duplicate JSON keys"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ExternalDfineRuntimeError(
                    f"{label} contains a non-finite number"
                )
            ),
        )
    except ExternalDfineRuntimeError:
        raise
    except Exception as error:
        raise ExternalDfineRuntimeError(
            f"{label} is not canonical UTF-8 JSON"
        ) from error
    if not isinstance(value, Mapping):
        raise ExternalDfineRuntimeError(f"{label} must be an object")
    return value


def _exact(
    value: object,
    fields: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ExternalDfineRuntimeError(f"{label} fields are invalid")
    return value


def _absolute_regular_file(
    value: object,
    *,
    label: str,
) -> Path:
    if not isinstance(value, str):
        raise ExternalDfineRuntimeError(f"{label} path is invalid")
    path = Path(value)
    if (
        not path.is_absolute()
        or not path.is_file()
        or _is_link_or_reparse(path)
    ):
        raise ExternalDfineRuntimeError(f"{label} is unavailable")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise ExternalDfineRuntimeError(
            f"{label} could not be resolved"
        ) from error


def _positive_integer(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ExternalDfineRuntimeError(f"{label} is invalid")
    return value


def load_external_dfine_runtime_manifest(
    manifest_path: Path | str,
) -> ExternalDfineRuntime:
    """Load and hash-verify the fixed external trainer command boundary."""

    path = Path(manifest_path)
    if (
        not path.is_absolute()
        or not path.is_file()
        or _is_link_or_reparse(path)
    ):
        raise ExternalDfineRuntimeError(
            "the external D-FINE runtime manifest is unavailable"
        )
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ExternalDfineRuntimeError(
            "the external D-FINE runtime manifest could not be read"
        ) from error
    if not raw or len(raw) > _MAX_RUNTIME_MANIFEST_BYTES:
        raise ExternalDfineRuntimeError(
            "the external D-FINE runtime manifest size is invalid"
        )
    value = _strict_json_bytes(
        raw,
        label="external D-FINE runtime manifest",
    )
    value = _exact(
        value,
        {
            "schema_version",
            "runtime_id",
            "runtime_version",
            "protocol_version",
            "executable",
            "entrypoint",
            "limits",
        },
        "external D-FINE runtime manifest",
    )
    if value["schema_version"] != EXTERNAL_RUNTIME_MANIFEST_SCHEMA:
        raise ExternalDfineRuntimeError(
            "the external D-FINE runtime schema is unsupported"
        )
    runtime_id = value["runtime_id"]
    runtime_version = value["runtime_version"]
    protocol_version = value["protocol_version"]
    if (
        not isinstance(runtime_id, str)
        or _RUNTIME_ID.fullmatch(runtime_id) is None
        or not isinstance(runtime_version, str)
        or _VERSION.fullmatch(runtime_version) is None
        or not isinstance(protocol_version, str)
        or _VERSION.fullmatch(protocol_version) is None
        or (runtime_id, runtime_version, protocol_version)
        not in _ALLOWED_RUNTIMES
    ):
        raise ExternalDfineRuntimeError(
            "the external D-FINE runtime identity is not allowlisted"
        )

    def artifact(
        raw_artifact: object,
        *,
        label: str,
    ) -> tuple[Path, str, int]:
        item = _exact(
            raw_artifact,
            {"path", "size_bytes", "sha256"},
            label,
        )
        file_path = _absolute_regular_file(
            item["path"],
            label=label,
        )
        declared_size = _positive_integer(
            item["size_bytes"],
            label=f"{label} size",
            minimum=1,
            maximum=_MAX_OUTPUT_BYTES,
        )
        declared_sha = item["sha256"]
        if (
            not isinstance(declared_sha, str)
            or _SHA256.fullmatch(declared_sha) is None
        ):
            raise ExternalDfineRuntimeError(
                f"{label} SHA-256 is invalid"
            )
        actual_sha, actual_size = _hash_file(file_path)
        if (
            actual_sha != declared_sha
            or actual_size != declared_size
        ):
            raise ExternalDfineRuntimeError(
                f"{label} hash inventory changed"
            )
        return file_path, actual_sha, actual_size

    executable, executable_sha, executable_size = artifact(
        value["executable"],
        label="external D-FINE executable",
    )
    entrypoint, entrypoint_sha, entrypoint_size = artifact(
        value["entrypoint"],
        label="external D-FINE entrypoint",
    )
    if entrypoint.suffix.lower() != ".py":
        raise ExternalDfineRuntimeError(
            "the external D-FINE entrypoint must be a pinned Python file"
        )
    limits = _exact(
        value["limits"],
        {
            "probe_timeout_seconds",
            "training_timeout_seconds",
            "max_log_bytes",
            "poll_interval_ms",
        },
        "external D-FINE limits",
    )
    return ExternalDfineRuntime(
        manifest_path=path.resolve(strict=True),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        runtime_id=runtime_id,
        runtime_version=runtime_version,
        protocol_version=protocol_version,
        executable=executable,
        executable_sha256=executable_sha,
        executable_size_bytes=executable_size,
        entrypoint=entrypoint,
        entrypoint_sha256=entrypoint_sha,
        entrypoint_size_bytes=entrypoint_size,
        probe_timeout_seconds=_positive_integer(
            limits["probe_timeout_seconds"],
            label="probe timeout",
            minimum=1,
            maximum=300,
        ),
        training_timeout_seconds=_positive_integer(
            limits["training_timeout_seconds"],
            label="training timeout",
            minimum=1,
            maximum=7 * 24 * 60 * 60,
        ),
        max_log_bytes=_positive_integer(
            limits["max_log_bytes"],
            label="maximum log bytes",
            minimum=1024,
            maximum=64 * 1024 * 1024,
        ),
        poll_interval_ms=_positive_integer(
            limits["poll_interval_ms"],
            label="poll interval",
            minimum=10,
            maximum=1000,
        ),
    )


def _pack_binding(pack: LocalDetectorModelPack) -> dict[str, Any]:
    try:
        manifest_sha, _manifest_size = _hash_file(pack.manifest_path)
    except ExternalDfineRuntimeError as error:
        raise ProjectModelLifecycleError(
            "detector_model_pack_changed",
            "the detector Model Pack manifest could not be reverified",
        ) from error
    if (
        manifest_sha != pack.manifest_sha256
        or not pack.root.is_dir()
        or _is_link_or_reparse(pack.root)
    ):
        raise ProjectModelLifecycleError(
            "detector_model_pack_changed",
            "the detector Model Pack binding changed",
        )
    expected_files = {
        artifact.relative_path
        for artifact in pack.artifacts
    }
    actual_files: set[str] = set()
    for path in pack.root.rglob("*"):
        if _is_link_or_reparse(path):
            raise ProjectModelLifecycleError(
                "detector_model_pack_changed",
                "the detector Model Pack contains a linked entry",
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ProjectModelLifecycleError(
                "detector_model_pack_changed",
                "the detector Model Pack contains an unsupported entry",
            )
        if path.resolve(strict=True) == pack.manifest_path:
            continue
        actual_files.add(path.relative_to(pack.root).as_posix())
    if actual_files != expected_files:
        raise ProjectModelLifecycleError(
            "detector_model_pack_changed",
            "the detector Model Pack inventory changed",
        )
    for artifact in pack.artifacts:
        path = pack.root.joinpath(
            *PurePosixPath(artifact.relative_path).parts
        )
        try:
            sha256, size = _hash_file(path)
        except ExternalDfineRuntimeError as error:
            raise ProjectModelLifecycleError(
                "detector_model_pack_changed",
                "a detector Model Pack artifact could not be reverified",
            ) from error
        if (
            sha256 != artifact.sha256
            or size != artifact.size_bytes
        ):
            raise ProjectModelLifecycleError(
                "detector_model_pack_changed",
                "a detector Model Pack artifact hash changed",
            )
    value = {
        "manifest_sha256": pack.manifest_sha256,
        "identity": pack.identity,
        "manifest_path": str(pack.manifest_path),
        "model_root": str(pack.model_root),
        "artifacts": [
            {
                "relative_path": artifact.relative_path,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
            for artifact in pack.artifacts
        ],
    }
    value["inventory_sha256"] = hashlib.sha256(
        canonical_json_bytes(value)
    ).hexdigest()
    return value


def _sample_value(sample: Any) -> dict[str, Any]:
    try:
        actual_sha, actual_size = _hash_file(sample.image_path)
    except ExternalDfineRuntimeError as error:
        raise ProjectModelLifecycleError(
            "training_evidence_integrity_failed",
            "a detector image could not be reverified",
        ) from error
    if (
        actual_sha != sample.image_sha256
        or actual_size != sample.image_size_bytes
    ):
        raise ProjectModelLifecycleError(
            "training_evidence_integrity_failed",
            "a detector image changed before external training",
        )
    return {
        "sample_id": sample.sample_id,
        "annotation_id": sample.annotation_id,
        "project_id": sample.project_id,
        "analysis_job_id": sample.analysis_job_id,
        "candidate_id": sample.candidate_id,
        "evidence_candidate_id": sample.evidence_candidate_id,
        "event_group_id": sample.event_group_id,
        "split": sample.split,
        "partition": sample.partition,
        "issue_type": sample.issue_type,
        "source_frame_kind": sample.source_frame_kind,
        "frame_id": sample.frame_id,
        "image_path": str(sample.image_path.resolve(strict=True)),
        "image_sha256": sample.image_sha256,
        "image_size_bytes": sample.image_size_bytes,
        "media_type": sample.media_type,
        "artifact_ref": sample.artifact_ref,
        "normalized_box": sample.normalized_box,
        "geometry_sha256": sample.geometry_sha256,
        "source_geometry_sha256": sample.source_geometry_sha256,
        "frame_sha256": sample.frame_sha256,
        "box_sha256": sample.box_sha256,
        "mask_sha256": sample.mask_sha256,
        "leakage_group_sha256": sample.leakage_group_sha256,
    }


def _safe_relative_weight(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise ProjectModelLifecycleError(
            "external_trainer_response_invalid",
            "the external trainer weight path is invalid",
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or path.parts[0] != "detector"
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or path.suffix.lower() != ".safetensors"
    ):
        raise ProjectModelLifecycleError(
            "external_trainer_response_invalid",
            "the external trainer weight path is unsafe",
        )
    return path


class ExternalDfineCudaDetectorBackend:
    """Execute one hash-pinned TrafficSuperVision D-FINE entrypoint."""

    backend_id = EXTERNAL_BACKEND_ID

    def __init__(
        self,
        runtime_manifest: Path | str,
        *,
        trusted_manifest_sha256: Optional[str],
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._monotonic = monotonic
        self._sleep = sleep
        self._configuration_error: Optional[str] = None
        self._runtime: Optional[ExternalDfineRuntime] = None
        self._probed_runtime: Optional[DetectorRuntimeInfo] = None
        if (
            not isinstance(trusted_manifest_sha256, str)
            or _SHA256.fullmatch(trusted_manifest_sha256) is None
        ):
            self._configuration_error = (
                "the server-owned trusted runtime manifest SHA-256 "
                "is not configured"
            )
        else:
            try:
                runtime = load_external_dfine_runtime_manifest(
                    runtime_manifest
                )
                if (
                    runtime.manifest_sha256
                    != trusted_manifest_sha256
                ):
                    raise ExternalDfineRuntimeError(
                        "the external D-FINE runtime manifest is not trusted"
                    )
                self._runtime = runtime
            except ExternalDfineRuntimeError as error:
                self._configuration_error = str(error)
        self.backend_version = (
            (
                f"{self._runtime.runtime_version}-"
                f"{self._runtime.manifest_sha256[:12]}"
            )
            if self._runtime is not None
            else "invalid"
        )

    def _require_runtime(self) -> ExternalDfineRuntime:
        if self._runtime is None:
            raise ProjectModelTrainerUnavailable(
                "external_d_fine_runtime_invalid: "
                f"{self._configuration_error or 'unknown error'}"
            )
        return self._runtime

    @staticmethod
    def _offline_environment() -> dict[str, str]:
        allowed = (
            "SYSTEMROOT",
            "WINDIR",
            "PATH",
            "PATHEXT",
            "TEMP",
            "TMP",
            "CUDA_PATH",
            "CUDA_VISIBLE_DEVICES",
        )
        env = {
            key: value
            for key in allowed
            if (value := os.environ.get(key))
        }
        env.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": "",
                "CUDA_VISIBLE_DEVICES": "0",
                "TOKENIZERS_PARALLELISM": "false",
            }
        )
        return env

    @staticmethod
    def _terminate(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:
                pass

    @staticmethod
    def _progress_event(value: object) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ProjectModelLifecycleError(
                "external_trainer_progress_invalid",
                "the external trainer emitted invalid progress",
            )
        allowed = {
            "stage",
            "epoch",
            "epochs",
            "current",
            "total",
            "loss",
        }
        if not set(value).issubset(allowed):
            raise ProjectModelLifecycleError(
                "external_trainer_progress_invalid",
                "the external trainer emitted unsupported progress fields",
            )
        stage = value.get("stage")
        if (
            not isinstance(stage, str)
            or not stage
            or len(stage) > 64
        ):
            raise ProjectModelLifecycleError(
                "external_trainer_progress_invalid",
                "the external trainer progress stage is invalid",
            )
        result: dict[str, Any] = {"stage": stage}
        for field in ("epoch", "epochs", "current", "total"):
            item = value.get(field)
            if item is None:
                continue
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or item < 0
                or item > 10_000_000
            ):
                raise ProjectModelLifecycleError(
                    "external_trainer_progress_invalid",
                    "the external trainer progress counter is invalid",
                )
            result[field] = item
        loss = value.get("loss")
        if loss is not None:
            if (
                isinstance(loss, bool)
                or not isinstance(loss, (int, float))
                or not math.isfinite(float(loss))
            ):
                raise ProjectModelLifecycleError(
                    "external_trainer_progress_invalid",
                    "the external trainer loss is invalid",
                )
            result["loss"] = round(float(loss), 8)
        return result

    def _reverify_runtime(self, runtime: ExternalDfineRuntime) -> None:
        if (
            not runtime.manifest_path.is_file()
            or _is_link_or_reparse(runtime.manifest_path)
        ):
            raise ProjectModelLifecycleError(
                "external_trainer_runtime_changed",
                "the external D-FINE runtime manifest is unavailable",
            )
        try:
            manifest_sha, _manifest_size = _hash_file(
                runtime.manifest_path
            )
        except ExternalDfineRuntimeError as error:
            raise ProjectModelLifecycleError(
                "external_trainer_runtime_changed",
                "the external D-FINE runtime manifest could not be reverified",
            ) from error
        if manifest_sha != runtime.manifest_sha256:
            raise ProjectModelLifecycleError(
                "external_trainer_runtime_changed",
                "the external D-FINE runtime manifest hash changed",
            )
        for path, expected_sha, expected_size in (
            (
                runtime.executable,
                runtime.executable_sha256,
                runtime.executable_size_bytes,
            ),
            (
                runtime.entrypoint,
                runtime.entrypoint_sha256,
                runtime.entrypoint_size_bytes,
            ),
        ):
            if not path.is_file() or _is_link_or_reparse(path):
                raise ProjectModelLifecycleError(
                    "external_trainer_runtime_changed",
                    "the external D-FINE runtime is no longer available",
                )
            try:
                actual_sha, actual_size = _hash_file(path)
            except ExternalDfineRuntimeError as error:
                raise ProjectModelLifecycleError(
                    "external_trainer_runtime_changed",
                    "the external D-FINE runtime could not be reverified",
                ) from error
            if (
                actual_sha != expected_sha
                or actual_size != expected_size
            ):
                raise ProjectModelLifecycleError(
                    "external_trainer_runtime_changed",
                    "the external D-FINE runtime hash changed",
                )

    def _run(
        self,
        *,
        mode: str,
        request_value: Mapping[str, Any],
        timeout_seconds: int,
        progress: Callable[[Mapping[str, Any]], None],
        cancelled: Callable[[], bool],
        work_parent: Optional[Path] = None,
    ) -> tuple[Mapping[str, Any], dict[str, Any]]:
        runtime = self._require_runtime()
        self._reverify_runtime(runtime)
        if cancelled():
            raise DetectorTrainingCancelled()
        with tempfile.TemporaryDirectory(
            prefix="trafficvisson-dfine-ipc-",
            dir=str(work_parent) if work_parent is not None else None,
        ) as temporary_name:
            temporary = Path(temporary_name)
            request_path = temporary / "request.json"
            result_path = temporary / "result.json"
            progress_path = temporary / "progress.jsonl"
            log_path = temporary / "process.log"
            request_path.write_bytes(canonical_json_bytes(request_value))
            command = [
                str(runtime.executable),
                str(runtime.entrypoint),
                "--trafficvisson-mode",
                mode,
                "--request",
                str(request_path),
                "--result",
                str(result_path),
                "--progress",
                str(progress_path),
            ]
            creationflags = (
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if os.name == "nt"
                else 0
            )
            started = self._monotonic()
            progress_offset = 0
            progress_buffer = b""
            progress_total = 0
            with log_path.open("wb") as log_handle:
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        cwd=str(runtime.entrypoint.parent),
                        env=self._offline_environment(),
                        shell=False,
                        close_fds=True,
                        creationflags=creationflags,
                    )
                except OSError as error:
                    raise ProjectModelLifecycleError(
                        "external_trainer_start_failed",
                        "the external D-FINE trainer could not be started",
                    ) from error
                # Popen is a context manager on Windows as well as POSIX. Its
                # exit path closes the native process handle; without it a
                # just-terminated child can keep the redirected log locked
                # long enough for TemporaryDirectory cleanup to mask the real
                # bounded-runtime error with WinError 32.
                with process:
                    try:
                        while True:
                            if progress_path.is_file():
                                try:
                                    with progress_path.open("rb") as handle:
                                        handle.seek(progress_offset)
                                        chunk = handle.read()
                                        progress_offset = handle.tell()
                                except OSError as error:
                                    self._terminate(process)
                                    raise ProjectModelLifecycleError(
                                        "external_trainer_progress_invalid",
                                        (
                                            "external trainer progress could "
                                            "not be read"
                                        ),
                                    ) from error
                                progress_total += len(chunk)
                                if progress_total > _MAX_PROGRESS_BYTES:
                                    self._terminate(process)
                                    raise ProjectModelLifecycleError(
                                        (
                                            "external_trainer_progress_limit_"
                                            "exceeded"
                                        ),
                                        (
                                            "external trainer progress "
                                            "exceeded its limit"
                                        ),
                                    )
                                progress_buffer += chunk
                                while b"\n" in progress_buffer:
                                    line, progress_buffer = (
                                        progress_buffer.split(b"\n", 1)
                                    )
                                    if not line.strip():
                                        continue
                                    try:
                                        event = _strict_json_bytes(
                                            line,
                                            label=(
                                                "external trainer progress"
                                            ),
                                        )
                                    except (
                                        ExternalDfineRuntimeError
                                    ) as error:
                                        self._terminate(process)
                                        raise ProjectModelLifecycleError(
                                            (
                                                "external_trainer_progress_"
                                                "invalid"
                                            ),
                                            (
                                                "external trainer progress "
                                                "is invalid"
                                            ),
                                        ) from error
                                    progress(self._progress_event(event))
                            try:
                                log_handle.flush()
                                log_size = log_path.stat().st_size
                            except OSError as error:
                                self._terminate(process)
                                raise ProjectModelLifecycleError(
                                    "external_trainer_log_invalid",
                                    (
                                        "external trainer log could not be "
                                        "measured"
                                    ),
                                ) from error
                            if log_size > runtime.max_log_bytes:
                                self._terminate(process)
                                raise ProjectModelLifecycleError(
                                    (
                                        "external_trainer_log_limit_"
                                        "exceeded"
                                    ),
                                    (
                                        "external trainer log exceeded its "
                                        "hard limit"
                                    ),
                                )
                            if cancelled():
                                self._terminate(process)
                                raise DetectorTrainingCancelled()
                            elapsed = self._monotonic() - started
                            if elapsed > timeout_seconds:
                                self._terminate(process)
                                raise ProjectModelLifecycleError(
                                    "external_trainer_timeout",
                                    (
                                        "the external D-FINE trainer timed "
                                        "out"
                                    ),
                                )
                            return_code = process.poll()
                            if return_code is not None:
                                break
                            self._sleep(
                                runtime.poll_interval_ms / 1000.0
                            )
                        log_handle.flush()
                    except BaseException:
                        # Covers progress hooks and unexpected I/O failures too;
                        # the Popen context must never wait forever on an
                        # unbounded child while unwinding.
                        self._terminate(process)
                        raise
            if progress_buffer.strip():
                raise ProjectModelLifecycleError(
                    "external_trainer_progress_invalid",
                    "external trainer progress ended with partial JSON",
                )
            log_body = log_path.read_bytes()
            if len(log_body) > runtime.max_log_bytes:
                raise ProjectModelLifecycleError(
                    "external_trainer_log_limit_exceeded",
                    "external trainer log exceeded its hard limit",
                )
            if return_code != 0:
                raise ProjectModelLifecycleError(
                    "external_trainer_failed",
                    (
                        "the external D-FINE trainer exited with code "
                        f"{return_code}"
                    ),
                )
            try:
                result_body = result_path.read_bytes()
            except OSError as error:
                raise ProjectModelLifecycleError(
                    "external_trainer_response_missing",
                    "the external trainer returned no result",
                ) from error
            if (
                not result_body
                or len(result_body) > _MAX_RESPONSE_BYTES
            ):
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    "the external trainer result size is invalid",
                )
            try:
                response = _strict_json_bytes(
                    result_body,
                    label="external trainer response",
                )
            except ExternalDfineRuntimeError as error:
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    "the external trainer result is invalid",
                ) from error
            return response, {
                "elapsed_milliseconds": max(
                    0,
                    int((self._monotonic() - started) * 1000),
                ),
                "timeout_seconds": timeout_seconds,
                "log_size_bytes": len(log_body),
                "log_sha256": hashlib.sha256(log_body).hexdigest(),
                "progress_size_bytes": progress_total,
                "subprocess_shell": False,
            }

    @staticmethod
    def _runtime_value(
        value: object,
        *,
        runtime: ExternalDfineRuntime,
        pack: LocalDetectorModelPack,
    ) -> DetectorRuntimeInfo:
        response = _exact(
            value,
            {
                "schema_version",
                "mode",
                "status",
                "runtime_id",
                "runtime_version",
                "runtime_manifest_sha256",
                "model_pack_manifest_sha256",
                "runtime",
            },
            "external D-FINE probe response",
        )
        if (
            response["schema_version"]
            != EXTERNAL_TRAINING_RESPONSE_SCHEMA
            or response["mode"] != "probe"
            or response["status"] != "ready"
            or response["runtime_id"] != runtime.runtime_id
            or response["runtime_version"] != runtime.runtime_version
            or response["runtime_manifest_sha256"]
            != runtime.manifest_sha256
            or response["model_pack_manifest_sha256"]
            != pack.manifest_sha256
        ):
            raise ExternalDfineRuntimeError(
                "external D-FINE probe binding is invalid"
            )
        details = _exact(
            response["runtime"],
            {
                "dependencies_ready",
                "accelerator",
                "resolved_device",
                "accelerator_used",
                "torch_version",
                "transformers_version",
                "torch_cuda_version",
                "cuda_device_name",
                "cuda_total_memory_bytes",
                "error_code",
            },
            "external D-FINE probe runtime",
        )
        if (
            details["dependencies_ready"] is not True
            or details["accelerator"] != "cuda"
            or details["accelerator_used"] is not True
            or not isinstance(details["resolved_device"], str)
            or not details["resolved_device"].startswith("cuda")
            or not isinstance(details["torch_version"], str)
            or not isinstance(details["transformers_version"], str)
            or not isinstance(details["torch_cuda_version"], str)
            or not isinstance(details["cuda_device_name"], str)
            or isinstance(details["cuda_total_memory_bytes"], bool)
            or not isinstance(details["cuda_total_memory_bytes"], int)
            or details["cuda_total_memory_bytes"] < 1
            or details["error_code"] is not None
        ):
            raise ExternalDfineRuntimeError(
                "external D-FINE probe did not prove CUDA readiness"
            )
        return DetectorRuntimeInfo(
            dependencies_ready=True,
            accelerator="cuda",
            resolved_device=details["resolved_device"],
            accelerator_used=True,
            torch_version=details["torch_version"],
            transformers_version=details["transformers_version"],
            torch_cuda_version=details["torch_cuda_version"],
            cuda_device_name=details["cuda_device_name"],
            cuda_total_memory_bytes=details["cuda_total_memory_bytes"],
            error_code=None,
        )

    def runtime_info(
        self,
        pack: LocalDetectorModelPack,
    ) -> DetectorRuntimeInfo:
        if self._runtime is None:
            return DetectorRuntimeInfo(
                dependencies_ready=False,
                accelerator="unavailable",
                resolved_device="unavailable",
                accelerator_used=False,
                torch_version=None,
                transformers_version=None,
                torch_cuda_version=None,
                cuda_device_name=None,
                cuda_total_memory_bytes=None,
                error_code="external_runtime_manifest_invalid",
            )
        runtime = self._runtime
        try:
            request = {
                "schema_version": EXTERNAL_TRAINING_REQUEST_SCHEMA,
                "mode": "probe",
                "runtime": runtime.identity,
                "model_pack": _pack_binding(pack),
            }
            response, _audit = self._run(
                mode="probe",
                request_value=request,
                timeout_seconds=runtime.probe_timeout_seconds,
                progress=lambda _value: None,
                cancelled=lambda: False,
            )
            resolved = self._runtime_value(
                response,
                runtime=runtime,
                pack=pack,
            )
            self._probed_runtime = resolved
            return resolved
        except Exception as error:
            self._probed_runtime = None
            return DetectorRuntimeInfo(
                dependencies_ready=False,
                accelerator="unavailable",
                resolved_device="unavailable",
                accelerator_used=False,
                torch_version=None,
                transformers_version=None,
                torch_cuda_version=None,
                cuda_device_name=None,
                cuda_total_memory_bytes=None,
                error_code=(
                    error.code
                    if isinstance(error, ProjectModelLifecycleError)
                    else "external_runtime_probe_invalid"
                ),
            )

    @staticmethod
    def _prediction_rows(
        value: object,
        *,
        label: str,
        validation_sample_ids: set[str],
        required_issue_types: set[str],
    ) -> tuple[DetectorBoxPrediction, ...]:
        if (
            not isinstance(value, list)
            or len(value) > _MAX_PREDICTIONS
        ):
            raise ProjectModelLifecycleError(
                "external_trainer_response_invalid",
                f"{label} are invalid",
            )
        rows = []
        for item in value:
            try:
                row = _exact(
                    item,
                    {
                        "sample_id",
                        "issue_type",
                        "score",
                        "normalized_box",
                    },
                    label,
                )
            except ExternalDfineRuntimeError as error:
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    f"{label} are invalid",
                ) from error
            score = row["score"]
            if (
                not isinstance(row["sample_id"], str)
                or row["sample_id"] not in validation_sample_ids
                or not isinstance(row["issue_type"], str)
                or row["issue_type"] not in required_issue_types
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
                or not 0.0 <= float(score) <= 1.0
            ):
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    f"{label} contain an invalid prediction",
                )
            try:
                normalized_box = canonical_box(
                    row["normalized_box"]
                )
            except TrainingGeometryError as error:
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    f"{label} contain an invalid box",
                ) from error
            rows.append(
                DetectorBoxPrediction(
                    sample_id=row["sample_id"],
                    issue_type=row["issue_type"],
                    score=round(float(score), 8),
                    normalized_box=normalized_box,
                )
            )
        return tuple(rows)

    @staticmethod
    def _verify_staging_tree(
        stage: Path,
        primary: PurePosixPath,
        declared_sha: str,
        declared_size: int,
    ) -> None:
        children = list(stage.iterdir())
        if (
            len(children) != 1
            or children[0].name != "detector"
            or not children[0].is_dir()
            or _is_link_or_reparse(children[0])
        ):
            raise ProjectModelLifecycleError(
                "external_trainer_artifact_invalid",
                "external trainer output must contain only detector/",
            )
        count = 0
        total = 0
        for path in children[0].rglob("*"):
            if _is_link_or_reparse(path):
                raise ProjectModelLifecycleError(
                    "external_trainer_artifact_invalid",
                    "external trainer output contains a linked artifact",
                )
            if path.is_file():
                count += 1
                total += path.stat().st_size
                if (
                    count > _MAX_OUTPUT_FILES
                    or total > _MAX_OUTPUT_BYTES
                ):
                    raise ProjectModelLifecycleError(
                        "external_trainer_artifact_limit_exceeded",
                        "external trainer output exceeded its hard limit",
                    )
        weight = stage.joinpath(*primary.parts)
        if (
            not weight.is_file()
            or _is_link_or_reparse(weight)
        ):
            raise ProjectModelLifecycleError(
                "external_trainer_artifact_missing",
                "external trainer produced no verified weight",
            )
        try:
            actual_sha, actual_size = _hash_file(weight)
        except ExternalDfineRuntimeError as error:
            raise ProjectModelLifecycleError(
                "external_trainer_artifact_invalid",
                "external trainer weight could not be verified",
            ) from error
        if (
            actual_sha != declared_sha
            or actual_size != declared_size
        ):
            raise ProjectModelLifecycleError(
                "external_trainer_artifact_invalid",
                "external trainer weight hash or size differs",
            )

    def train(
        self,
        request: DetectorBackendRequest,
        *,
        progress: Callable[[Mapping[str, Any]], None],
        cancelled: Callable[[], bool],
    ) -> DetectorBackendResult:
        runtime = self._require_runtime()
        probed_runtime = self._probed_runtime
        if (
            probed_runtime is None
            or probed_runtime.dependencies_ready is not True
            or probed_runtime.accelerator_used is not True
        ):
            raise ProjectModelTrainerUnavailable(
                "the external D-FINE runtime has no verified CUDA preflight"
            )
        if cancelled():
            raise DetectorTrainingCancelled()
        output = request.output_directory.resolve(strict=True)
        if _is_link_or_reparse(output):
            raise ProjectModelLifecycleError(
                "external_trainer_output_invalid",
                "external trainer output root cannot be linked",
            )
        pack_binding = _pack_binding(request.pack)
        training_body = {
            "schema_version": EXTERNAL_TRAINING_REQUEST_SCHEMA,
            "mode": "train",
            "runtime": runtime.identity,
            "binding": {
                "project_id": request.invocation.project_id,
                "training_job_id": request.invocation.training_job_id,
                "request_manifest_sha256": (
                    request.invocation.request_manifest_sha256
                ),
                "dataset_version_id": (
                    request.invocation.dataset_version_id
                ),
                "dataset_hash": request.invocation.dataset_hash,
                "fixed_validation_sha256": (
                    request.invocation.fixed_validation_sha256
                ),
                "fixed_validation_membership_sha256": (
                    request.invocation.fixed_validation_membership_sha256
                ),
                "model_pack_manifest_sha256": (
                    request.pack.manifest_sha256
                ),
                "model_pack_inventory_sha256": pack_binding[
                    "inventory_sha256"
                ],
            },
            "model_pack": pack_binding,
            "configuration": {
                "required_issue_types": list(
                    request.invocation.required_issue_types
                ),
                "image_size": request.pack.image_size,
                "train_batch_size": request.pack.train_batch_size,
                "gradient_accumulation_steps": (
                    request.pack.gradient_accumulation_steps
                ),
                "epochs": request.pack.epochs,
                "learning_rate": request.pack.learning_rate,
                "score_threshold": request.pack.score_threshold,
                "iou_threshold": request.pack.iou_threshold,
            },
            "resume_checkpoint": (
                {
                    "path": str(request.resume_checkpoint.path),
                    "sha256": request.resume_checkpoint.sha256,
                    "request_manifest_sha256": (
                        request.resume_checkpoint.request_manifest_sha256
                    ),
                }
                if request.resume_checkpoint is not None
                else None
            ),
            "train": [
                _sample_value(sample)
                for sample in request.train_samples
            ],
            "validation": [
                _sample_value(sample)
                for sample in request.validation_samples
            ],
        }
        input_sha256 = hashlib.sha256(
            canonical_json_bytes(training_body)
        ).hexdigest()
        with tempfile.TemporaryDirectory(
            prefix=".external-dfine-staging-",
            dir=str(output),
        ) as stage_name:
            stage = Path(stage_name)
            request_value = {
                **training_body,
                "training_input_sha256": input_sha256,
                "output_directory": str(stage),
            }
            response, audit = self._run(
                mode="train",
                request_value=request_value,
                timeout_seconds=runtime.training_timeout_seconds,
                progress=progress,
                cancelled=cancelled,
                work_parent=output,
            )
            try:
                result = _exact(
                    response,
                    {
                        "schema_version",
                        "mode",
                        "status",
                        "runtime_id",
                        "runtime_version",
                        "runtime_manifest_sha256",
                        "binding",
                        "primary_weight",
                        "active_predictions",
                        "candidate_predictions",
                        "execution",
                        "resumed_from_checkpoint_sha256",
                    },
                    "external D-FINE train response",
                )
            except ExternalDfineRuntimeError as error:
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    "external trainer result fields are invalid",
                ) from error
            expected_binding = {
                **training_body["binding"],
                "training_input_sha256": input_sha256,
            }
            if (
                result["schema_version"]
                != EXTERNAL_TRAINING_RESPONSE_SCHEMA
                or result["mode"] != "train"
                or result["status"] != "completed"
                or result["runtime_id"] != runtime.runtime_id
                or result["runtime_version"] != runtime.runtime_version
                or result["runtime_manifest_sha256"]
                != runtime.manifest_sha256
                or result["binding"] != expected_binding
                or result["active_predictions"] != []
            ):
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    "external trainer result binding is invalid",
                )
            try:
                weight_value = _exact(
                    result["primary_weight"],
                    {"path", "size_bytes", "sha256"},
                    "external trainer primary weight",
                )
            except ExternalDfineRuntimeError as error:
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    "external trainer primary weight is invalid",
                ) from error
            primary = _safe_relative_weight(weight_value["path"])
            declared_size = weight_value["size_bytes"]
            declared_sha = weight_value["sha256"]
            if (
                isinstance(declared_size, bool)
                or not isinstance(declared_size, int)
                or declared_size < 1
                or declared_size > _MAX_OUTPUT_BYTES
                or not isinstance(declared_sha, str)
                or _SHA256.fullmatch(declared_sha) is None
            ):
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    "external trainer primary weight inventory is invalid",
                )
            self._verify_staging_tree(
                stage,
                primary,
                declared_sha,
                declared_size,
            )
            try:
                execution = _exact(
                    result["execution"],
                    {
                        "accelerator",
                        "resolved_device",
                        "accelerator_used",
                        "torch_version",
                        "transformers_version",
                        "torch_cuda_version",
                        "cuda_device_name",
                        "cuda_total_memory_bytes",
                        "automatic_mixed_precision",
                        "gradient_accumulation_steps",
                        "hub_upload",
                        "network_model_download",
                        "trust_remote_code",
                    },
                    "external trainer execution",
                )
            except ExternalDfineRuntimeError as error:
                raise ProjectModelLifecycleError(
                    "training_runtime_provenance_invalid",
                    "external trainer execution fields are invalid",
                ) from error
            if (
                execution["accelerator"] != "cuda"
                or execution["accelerator_used"] is not True
                or not isinstance(execution["resolved_device"], str)
                or not execution["resolved_device"].startswith("cuda")
                or execution["resolved_device"]
                != probed_runtime.resolved_device
                or not isinstance(execution["torch_version"], str)
                or execution["torch_version"]
                != probed_runtime.torch_version
                or not isinstance(
                    execution["transformers_version"],
                    str,
                )
                or execution["transformers_version"]
                != probed_runtime.transformers_version
                or not isinstance(execution["torch_cuda_version"], str)
                or execution["torch_cuda_version"]
                != probed_runtime.torch_cuda_version
                or not isinstance(execution["cuda_device_name"], str)
                or execution["cuda_device_name"]
                != probed_runtime.cuda_device_name
                or isinstance(
                    execution["cuda_total_memory_bytes"],
                    bool,
                )
                or not isinstance(
                    execution["cuda_total_memory_bytes"],
                    int,
                )
                or execution["cuda_total_memory_bytes"]
                != probed_runtime.cuda_total_memory_bytes
                or not isinstance(
                    execution["automatic_mixed_precision"],
                    bool,
                )
                or execution["hub_upload"] is not False
                or execution["network_model_download"] is not False
                or execution["trust_remote_code"] is not False
                or isinstance(
                    execution["gradient_accumulation_steps"],
                    bool,
                )
                or execution["gradient_accumulation_steps"]
                != request.pack.gradient_accumulation_steps
            ):
                raise ProjectModelLifecycleError(
                    "training_runtime_provenance_invalid",
                    "external trainer did not prove offline CUDA execution",
                )
            checkpoint_sha = result["resumed_from_checkpoint_sha256"]
            expected_checkpoint_sha = (
                request.resume_checkpoint.sha256
                if request.resume_checkpoint is not None
                else None
            )
            if checkpoint_sha != expected_checkpoint_sha:
                raise ProjectModelLifecycleError(
                    "external_trainer_response_invalid",
                    "external trainer checkpoint binding is invalid",
                )
            candidate_predictions = self._prediction_rows(
                result["candidate_predictions"],
                label="external trainer candidate predictions",
                validation_sample_ids={
                    sample.sample_id
                    for sample in request.validation_samples
                },
                required_issue_types=set(
                    request.invocation.required_issue_types
                ),
            )
            target = output / "detector"
            if target.exists():
                raise ProjectModelLifecycleError(
                    "external_trainer_output_conflict",
                    "the verified detector output already exists",
                )
            os.replace(stage / "detector", target)
        promoted_weight = output.joinpath(*primary.parts)
        try:
            promoted_sha, promoted_size = _hash_file(promoted_weight)
        except ExternalDfineRuntimeError as error:
            raise ProjectModelLifecycleError(
                "external_trainer_artifact_invalid",
                "the promoted detector weight could not be reverified",
            ) from error
        if (
            promoted_sha != declared_sha
            or promoted_size != declared_size
        ):
            raise ProjectModelLifecycleError(
                "external_trainer_artifact_invalid",
                "the promoted detector weight changed",
            )
        return DetectorBackendResult(
            primary_weight_relative_path=primary.as_posix(),
            active_predictions=(),
            candidate_predictions=candidate_predictions,
            resumed_from_checkpoint_sha256=checkpoint_sha,
            execution_provenance={
                "schema_version": EXTERNAL_EXECUTION_PROVENANCE_SCHEMA,
                "backend_id": self.backend_id,
                "backend_version": self.backend_version,
                **execution,
                "external_runtime_id": runtime.runtime_id,
                "external_runtime_version": runtime.runtime_version,
                "external_runtime_manifest_sha256": (
                    runtime.manifest_sha256
                ),
                "external_executable_sha256": (
                    runtime.executable_sha256
                ),
                "external_entrypoint_sha256": (
                    runtime.entrypoint_sha256
                ),
                "model_pack_manifest_sha256": (
                    request.pack.manifest_sha256
                ),
                "model_pack_inventory_sha256": pack_binding[
                    "inventory_sha256"
                ],
                "dataset_hash": request.invocation.dataset_hash,
                "fixed_validation_sha256": (
                    request.invocation.fixed_validation_sha256
                ),
                "training_input_sha256": input_sha256,
                **audit,
            },
        )


__all__ = [
    "EXTERNAL_BACKEND_ID",
    "EXTERNAL_EXECUTION_PROVENANCE_SCHEMA",
    "EXTERNAL_RUNTIME_MANIFEST_SCHEMA",
    "EXTERNAL_TRAINING_REQUEST_SCHEMA",
    "EXTERNAL_TRAINING_RESPONSE_SCHEMA",
    "ExternalDfineCudaDetectorBackend",
    "ExternalDfineRuntime",
    "ExternalDfineRuntimeError",
    "load_external_dfine_runtime_manifest",
]
