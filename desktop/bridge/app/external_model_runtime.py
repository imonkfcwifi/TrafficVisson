"""Verified external ML runtime activation for the frozen Windows bridge.

The default PyInstaller bridge deliberately excludes large optional ML
dependencies.  A separately installed runtime pack may provide those Python
packages and native DLLs, but only after this module verifies a closed,
versioned manifest, every file hash, package metadata, licence declarations,
the running Python ABI, and the declared CUDA/VRAM contract.

The runtime pack is code, not a model.  It never contains model weights and it
does not grant authority to create labels, incidents, train, promote a model,
or apply a report.  Model packs remain independently verified by their
respective loaders.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
from importlib import metadata
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import sys
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence


MODEL_RUNTIME_SCHEMA = "trafficvisson.external-model-runtime-pack.v1"
MODEL_RUNTIME_ENVIRONMENT_KEY = (
    "TRAFFICSUPERVISION_EXTERNAL_MODEL_RUNTIME_PACK"
)
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_RUNTIME_FILES = 200_000
MAX_RUNTIME_BYTES = 32 * 1024 * 1024 * 1024

_WINDOWS_REPARSE_ATTRIBUTE = 0x400
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REVISION = re.compile(r"^[a-f0-9]{40,64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_CACHE_TAG = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
_SPDX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_CUDA_VERSION = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,3}){1,2}$")
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HTTPS_URL = re.compile(r"^https://[^\s]{1,500}$")
_ALLOWED_CAPABILITIES = frozenset(
    {
        "pytorch_cuda",
        "grounding_dino",
        "sam2",
        "opencv",
        "korean_ocr",
        "embedding_search",
        "detector_training",
        "detector_inference",
        "onnx_inference",
    }
)
_TORCH_CAPABILITIES = frozenset(
    {
        "pytorch_cuda",
        "grounding_dino",
        "sam2",
        "korean_ocr",
        "embedding_search",
        "detector_training",
        "detector_inference",
    }
)
_FORBIDDEN_RUNTIME_SUFFIXES = frozenset(
    {
        ".bat",
        ".cmd",
        ".egg-link",
        ".lnk",
        ".ps1",
        ".pth",
    }
)
_REQUIRED_DISTRIBUTIONS = {
    "pytorch_cuda": (("torch",),),
    "grounding_dino": (
        ("torch",),
        ("transformers",),
        ("pillow",),
        ("safetensors",),
        ("tokenizers",),
    ),
    "sam2": (
        ("torch",),
        ("transformers",),
        ("pillow",),
        ("numpy",),
        ("opencv-python", "opencv-python-headless"),
    ),
    "opencv": (
        ("numpy",),
        ("opencv-python", "opencv-python-headless"),
    ),
    "korean_ocr": (
        ("torch",),
        ("easyocr",),
        ("pillow",),
        ("numpy",),
        ("opencv-python", "opencv-python-headless"),
    ),
    "embedding_search": (
        ("torch",),
        ("transformers",),
        ("pillow",),
        ("safetensors",),
        ("tokenizers",),
    ),
    "detector_training": (
        ("torch",),
        ("transformers",),
        ("pillow",),
        ("safetensors",),
    ),
    "detector_inference": (
        ("torch",),
        ("transformers",),
        ("pillow",),
        ("safetensors",),
    ),
    "onnx_inference": (
        ("onnxruntime", "onnxruntime-gpu"),
        ("numpy",),
        ("pillow",),
        ("opencv-python", "opencv-python-headless"),
    ),
}


class ExternalModelRuntimeError(ValueError):
    """Fail-closed runtime-pack error with a scrubbed stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RuntimeArtifact:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RuntimeDistribution:
    name: str
    version: str
    spdx: str
    notice_artifact: str


@dataclass(frozen=True)
class ExternalModelRuntimePack:
    manifest_path: Path
    root: Path
    manifest_sha256: str
    runtime_id: str
    runtime_version: str
    source_repository: str
    source_revision: str
    runtime_spdx: str
    commercial_use_confirmed: bool
    redistribution_allowed: bool
    python_cache_tag: str
    python_version: str
    platform_system: str
    platform_machine: str
    capabilities: tuple[str, ...]
    distributions: tuple[RuntimeDistribution, ...]
    site_packages: Path
    dll_directories: tuple[Path, ...]
    artifacts: tuple[RuntimeArtifact, ...]
    requires_cuda: bool
    cpu_fallback: bool
    minimum_cuda_version: Optional[str]
    minimum_vram_bytes: int

    @property
    def public_identity(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": MODEL_RUNTIME_SCHEMA,
                "runtime_id": self.runtime_id,
                "runtime_version": self.runtime_version,
                "manifest_sha256": self.manifest_sha256,
                "source_revision": self.source_revision,
                "runtime_spdx": self.runtime_spdx,
                "commercial_use_confirmed": (
                    self.commercial_use_confirmed
                ),
                "redistribution_allowed": self.redistribution_allowed,
                "python_cache_tag": self.python_cache_tag,
                "capabilities": list(self.capabilities),
                "requires_cuda": self.requires_cuda,
                "cpu_fallback": self.cpu_fallback,
                "minimum_cuda_version": self.minimum_cuda_version,
                "minimum_vram_bytes": self.minimum_vram_bytes,
                "artifact_count": len(self.artifacts),
            }
        )


_DLL_HANDLES: list[Any] = []
_ACTIVE_SITE_PACKAGES: Optional[str] = None
_CURRENT_STATUS: Mapping[str, Any] = MappingProxyType(
    {
        "state": "unavailable",
        "error_code": "runtime_pack_not_configured",
    }
)


def _normal_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(details.st_mode):
        return True
    return bool(
        getattr(details, "st_file_attributes", 0)
        & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _strict_json(path: Path) -> tuple[Mapping[str, Any], bytes]:
    try:
        details = path.stat()
        if (
            not path.is_file()
            or _is_link_or_reparse(path)
            or details.st_nlink != 1
            or not 1 <= details.st_size <= MAX_MANIFEST_BYTES
        ):
            raise ExternalModelRuntimeError(
                "runtime_manifest_unavailable",
                "the external model runtime manifest is unavailable",
            )
        raw = path.read_bytes()
        after = path.stat()
    except ExternalModelRuntimeError:
        raise
    except OSError as error:
        raise ExternalModelRuntimeError(
            "runtime_manifest_unavailable",
            "the external model runtime manifest is unavailable",
        ) from error
    if (
        len(raw) != details.st_size
        or details.st_size != after.st_size
        or details.st_mtime_ns != after.st_mtime_ns
    ):
        raise ExternalModelRuntimeError(
            "runtime_manifest_changed",
            "the external model runtime manifest changed while read",
        )

    def pairs(values: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ExternalModelRuntimeError(
                    "runtime_manifest_invalid",
                    "the external model runtime manifest has duplicate keys",
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ExternalModelRuntimeError(
                    "runtime_manifest_invalid",
                    "the external model runtime manifest is non-finite",
                )
            ),
        )
    except ExternalModelRuntimeError:
        raise
    except Exception as error:
        raise ExternalModelRuntimeError(
            "runtime_manifest_invalid",
            "the external model runtime manifest is invalid",
        ) from error
    if not isinstance(value, Mapping):
        raise ExternalModelRuntimeError(
            "runtime_manifest_invalid",
            "the external model runtime manifest must be an object",
        )
    return value, raw


def _exact(
    value: object,
    fields: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ExternalModelRuntimeError(
            "runtime_manifest_invalid",
            f"{label} fields are invalid",
        )
    return value


def _text(
    value: object,
    pattern: re.Pattern[str],
    label: str,
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ExternalModelRuntimeError(
            "runtime_manifest_invalid",
            f"{label} is invalid",
        )
    return value


def _relative_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise ExternalModelRuntimeError(
            "runtime_manifest_invalid",
            f"{label} is invalid",
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or path.suffix.casefold() in _FORBIDDEN_RUNTIME_SUFFIXES
    ):
        raise ExternalModelRuntimeError(
            "runtime_artifact_unsafe",
            f"{label} is unsafe",
        )
    return path


def _contained_path(
    root: Path,
    relative: PurePosixPath,
    *,
    directory: bool,
    label: str,
) -> Path:
    candidate = root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        details = candidate.stat()
    except OSError as error:
        raise ExternalModelRuntimeError(
            "runtime_artifact_unavailable",
            f"{label} is unavailable",
        ) from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ExternalModelRuntimeError(
            "runtime_artifact_unsafe",
            f"{label} escaped the runtime pack",
        ) from error
    if (
        _is_link_or_reparse(candidate)
        or _is_link_or_reparse(resolved)
        or (directory and not stat.S_ISDIR(details.st_mode))
        or (not directory and not stat.S_ISREG(details.st_mode))
    ):
        raise ExternalModelRuntimeError(
            "runtime_artifact_unsafe",
            f"{label} is unsafe",
        )
    return resolved


def _sha256(path: Path) -> tuple[str, int]:
    try:
        before = path.stat()
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while True:
                block = stream.read(4 * 1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
        after = path.stat()
    except OSError as error:
        raise ExternalModelRuntimeError(
            "runtime_artifact_unavailable",
            "an external model runtime artifact could not be read",
        ) from error
    if (
        size != before.st_size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ExternalModelRuntimeError(
            "runtime_artifact_changed",
            "an external model runtime artifact changed while read",
        )
    return digest.hexdigest(), size


def _version_parts(value: Optional[str]) -> tuple[int, ...]:
    if not isinstance(value, str):
        return ()
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if match is None:
        return ()
    return tuple(
        int(item) if item is not None else 0
        for item in match.groups()
    )


def _expected_machine() -> str:
    machine = platform.machine().casefold()
    if machine in {"amd64", "x86_64"}:
        return "AMD64"
    if machine in {"arm64", "aarch64"}:
        return "ARM64"
    return platform.machine()


def load_external_model_runtime_pack(
    manifest_path: Path | str,
    *,
    expected_cache_tag: Optional[str] = None,
    expected_python_version: Optional[str] = None,
    expected_platform_system: Optional[str] = None,
    expected_platform_machine: Optional[str] = None,
) -> ExternalModelRuntimePack:
    """Verify a closed external runtime pack without importing its code."""

    path = Path(manifest_path)
    if not path.is_absolute():
        raise ExternalModelRuntimeError(
            "runtime_manifest_unavailable",
            "the external model runtime manifest path must be absolute",
        )
    if _is_link_or_reparse(path.parent):
        raise ExternalModelRuntimeError(
            "runtime_root_unsafe",
            "the external model runtime root is unsafe",
        )
    try:
        root = path.parent.resolve(strict=True)
    except OSError as error:
        raise ExternalModelRuntimeError(
            "runtime_manifest_unavailable",
            "the external model runtime root is unavailable",
        ) from error
    if _is_link_or_reparse(root) or not root.is_dir():
        raise ExternalModelRuntimeError(
            "runtime_root_unsafe",
            "the external model runtime root is unsafe",
        )
    manifest, raw = _strict_json(path)
    expected_top = {
        "schema_version",
        "runtime_id",
        "runtime_version",
        "source",
        "license",
        "python",
        "platform",
        "hardware",
        "capabilities",
        "packages",
        "paths",
        "artifacts",
    }
    if set(manifest) != expected_top:
        raise ExternalModelRuntimeError(
            "runtime_manifest_invalid",
            "the external model runtime manifest fields are invalid",
        )
    if manifest["schema_version"] != MODEL_RUNTIME_SCHEMA:
        raise ExternalModelRuntimeError(
            "runtime_schema_unsupported",
            "the external model runtime schema is unsupported",
        )
    runtime_id = _text(
        manifest["runtime_id"], _IDENTIFIER, "runtime_id"
    )
    runtime_version = _text(
        manifest["runtime_version"], _VERSION, "runtime_version"
    )
    source = _exact(
        manifest["source"], {"repository", "revision"}, "source"
    )
    source_repository = _text(
        source["repository"], _HTTPS_URL, "source.repository"
    )
    source_revision = _text(
        source["revision"], _REVISION, "source.revision"
    )
    license_value = _exact(
        manifest["license"],
        {
            "runtime_spdx",
            "commercial_use_confirmed",
            "redistribution_allowed",
            "license_url",
            "notice_artifact",
        },
        "license",
    )
    runtime_spdx = _text(
        license_value["runtime_spdx"],
        _SPDX,
        "license.runtime_spdx",
    )
    if (
        license_value["commercial_use_confirmed"] is not True
        or not isinstance(license_value["redistribution_allowed"], bool)
    ):
        raise ExternalModelRuntimeError(
            "runtime_license_unverified",
            "the external model runtime licence is not approved",
        )
    _text(
        license_value["license_url"],
        _HTTPS_URL,
        "license.license_url",
    )
    notice_relative = _relative_path(
        license_value["notice_artifact"],
        "license.notice_artifact",
    )

    python_value = _exact(
        manifest["python"],
        {"implementation", "cache_tag", "version"},
        "python",
    )
    if python_value["implementation"] != "cpython":
        raise ExternalModelRuntimeError(
            "runtime_python_abi_mismatch",
            "the external model runtime requires CPython",
        )
    python_cache_tag = _text(
        python_value["cache_tag"], _CACHE_TAG, "python.cache_tag"
    )
    python_version = _text(
        python_value["version"], _VERSION, "python.version"
    )
    required_cache_tag = (
        expected_cache_tag
        if expected_cache_tag is not None
        else getattr(sys.implementation, "cache_tag", None)
    )
    required_python_version = (
        expected_python_version
        if expected_python_version is not None
        else f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    if (
        python_cache_tag != required_cache_tag
        or python_version != required_python_version
    ):
        raise ExternalModelRuntimeError(
            "runtime_python_abi_mismatch",
            "the external model runtime Python ABI does not match",
        )

    platform_value = _exact(
        manifest["platform"], {"system", "machine"}, "platform"
    )
    platform_system = _text(
        platform_value["system"], _IDENTIFIER, "platform.system"
    )
    platform_machine = _text(
        platform_value["machine"], _IDENTIFIER, "platform.machine"
    )
    required_system = (
        expected_platform_system
        if expected_platform_system is not None
        else platform.system()
    )
    required_machine = (
        expected_platform_machine
        if expected_platform_machine is not None
        else _expected_machine()
    )
    if (
        platform_system.casefold() != required_system.casefold()
        or platform_machine.casefold() != required_machine.casefold()
    ):
        raise ExternalModelRuntimeError(
            "runtime_platform_mismatch",
            "the external model runtime platform does not match",
        )

    hardware = _exact(
        manifest["hardware"],
        {
            "requires_cuda",
            "cpu_fallback",
            "minimum_cuda_version",
            "minimum_vram_bytes",
        },
        "hardware",
    )
    requires_cuda = hardware["requires_cuda"]
    cpu_fallback = hardware["cpu_fallback"]
    minimum_cuda_version = hardware["minimum_cuda_version"]
    minimum_vram_bytes = hardware["minimum_vram_bytes"]
    if (
        not isinstance(requires_cuda, bool)
        or not isinstance(cpu_fallback, bool)
        or isinstance(minimum_vram_bytes, bool)
        or not isinstance(minimum_vram_bytes, int)
        or not 0 <= minimum_vram_bytes <= 256 * 1024**3
        or not (
            minimum_cuda_version is None
            or (
                isinstance(minimum_cuda_version, str)
                and _CUDA_VERSION.fullmatch(minimum_cuda_version)
                is not None
            )
        )
    ):
        raise ExternalModelRuntimeError(
            "runtime_hardware_invalid",
            "the external model runtime hardware contract is invalid",
        )
    if requires_cuda and (
        cpu_fallback
        or minimum_cuda_version is None
        or minimum_vram_bytes < 1024**3
    ):
        raise ExternalModelRuntimeError(
            "runtime_hardware_invalid",
            "the CUDA runtime must fail closed without CPU fallback",
        )

    capabilities_value = manifest["capabilities"]
    if (
        not isinstance(capabilities_value, list)
        or not 1 <= len(capabilities_value) <= len(_ALLOWED_CAPABILITIES)
        or any(
            not isinstance(value, str)
            or value not in _ALLOWED_CAPABILITIES
            for value in capabilities_value
        )
        or len(set(capabilities_value)) != len(capabilities_value)
    ):
        raise ExternalModelRuntimeError(
            "runtime_capabilities_invalid",
            "the external model runtime capabilities are invalid",
        )
    capabilities = tuple(sorted(capabilities_value))
    if _TORCH_CAPABILITIES.intersection(capabilities) and not requires_cuda:
        raise ExternalModelRuntimeError(
            "runtime_hardware_invalid",
            "the declared PyTorch runtime must require verified CUDA",
        )

    paths_value = _exact(
        manifest["paths"],
        {"site_packages", "dll_directories"},
        "paths",
    )
    site_packages_relative = _relative_path(
        paths_value["site_packages"], "paths.site_packages"
    )
    raw_dll_directories = paths_value["dll_directories"]
    if (
        not isinstance(raw_dll_directories, list)
        or len(raw_dll_directories) > 32
    ):
        raise ExternalModelRuntimeError(
            "runtime_manifest_invalid",
            "paths.dll_directories is invalid",
        )
    dll_relatives = tuple(
        _relative_path(value, "paths.dll_directories")
        for value in raw_dll_directories
    )
    if len(set(dll_relatives)) != len(dll_relatives):
        raise ExternalModelRuntimeError(
            "runtime_manifest_invalid",
            "paths.dll_directories contains duplicates",
        )

    raw_packages = manifest["packages"]
    if (
        not isinstance(raw_packages, list)
        or not 1 <= len(raw_packages) <= 2_048
    ):
        raise ExternalModelRuntimeError(
            "runtime_packages_invalid",
            "the external model runtime package list is invalid",
        )
    distributions: list[RuntimeDistribution] = []
    package_names: set[str] = set()
    for index, raw_package in enumerate(raw_packages):
        package = _exact(
            raw_package,
            {"name", "version", "spdx", "notice_artifact"},
            f"packages[{index}]",
        )
        name = _text(
            package["name"], _PACKAGE_NAME, f"packages[{index}].name"
        )
        normalized_name = _normal_distribution_name(name)
        if normalized_name in package_names:
            raise ExternalModelRuntimeError(
                "runtime_packages_invalid",
                "the external model runtime package list has duplicates",
            )
        package_names.add(normalized_name)
        distributions.append(
            RuntimeDistribution(
                name=name,
                version=_text(
                    package["version"],
                    _VERSION,
                    f"packages[{index}].version",
                ),
                spdx=_text(
                    package["spdx"],
                    _SPDX,
                    f"packages[{index}].spdx",
                ),
                notice_artifact=_relative_path(
                    package["notice_artifact"],
                    f"packages[{index}].notice_artifact",
                ).as_posix(),
            )
        )
    for capability in capabilities:
        for alternatives in _REQUIRED_DISTRIBUTIONS[capability]:
            if not any(
                _normal_distribution_name(value) in package_names
                for value in alternatives
            ):
                raise ExternalModelRuntimeError(
                    "runtime_packages_incomplete",
                    "the external model runtime is missing a required package",
                )

    raw_artifacts = manifest["artifacts"]
    if (
        not isinstance(raw_artifacts, list)
        or not 1 <= len(raw_artifacts) <= MAX_RUNTIME_FILES
    ):
        raise ExternalModelRuntimeError(
            "runtime_artifacts_invalid",
            "the external model runtime artifact inventory is invalid",
        )
    artifacts: list[RuntimeArtifact] = []
    artifact_names: set[str] = set()
    total_size = 0
    for index, raw_artifact in enumerate(raw_artifacts):
        artifact = _exact(
            raw_artifact,
            {"path", "size_bytes", "sha256"},
            f"artifacts[{index}]",
        )
        relative = _relative_path(
            artifact["path"], f"artifacts[{index}].path"
        )
        relative_text = relative.as_posix()
        size_bytes = artifact["size_bytes"]
        sha256 = artifact["sha256"]
        if (
            relative_text in artifact_names
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 0 <= size_bytes <= MAX_RUNTIME_BYTES
            or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None
        ):
            raise ExternalModelRuntimeError(
                "runtime_artifacts_invalid",
                "the external model runtime artifact inventory is invalid",
            )
        artifact_names.add(relative_text)
        total_size += size_bytes
        if total_size > MAX_RUNTIME_BYTES:
            raise ExternalModelRuntimeError(
                "runtime_artifacts_invalid",
                "the external model runtime exceeds its size limit",
            )
        artifacts.append(
            RuntimeArtifact(
                relative_path=relative_text,
                size_bytes=size_bytes,
                sha256=sha256,
            )
        )

    site_packages = _contained_path(
        root,
        site_packages_relative,
        directory=True,
        label="site-packages",
    )
    dll_directories = tuple(
        _contained_path(
            root,
            relative,
            directory=True,
            label="DLL directory",
        )
        for relative in dll_relatives
    )
    try:
        site_packages.relative_to(root)
        for dll_directory in dll_directories:
            dll_directory.relative_to(site_packages)
    except ValueError as error:
        raise ExternalModelRuntimeError(
            "runtime_artifact_unsafe",
            "runtime package paths escaped their allowed roots",
        ) from error

    actual_files: set[str] = set()
    for candidate in root.rglob("*"):
        if _is_link_or_reparse(candidate):
            raise ExternalModelRuntimeError(
                "runtime_artifact_unsafe",
                "the external model runtime contains a link or reparse point",
            )
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ExternalModelRuntimeError(
                "runtime_artifact_unsafe",
                "the external model runtime contains a non-file artifact",
            )
        if candidate.stat().st_nlink != 1:
            raise ExternalModelRuntimeError(
                "runtime_artifact_unsafe",
                "the external model runtime contains a linked artifact",
            )
        if candidate == path:
            continue
        relative_text = candidate.relative_to(root).as_posix()
        if (
            PurePosixPath(relative_text).suffix.casefold()
            in _FORBIDDEN_RUNTIME_SUFFIXES
        ):
            raise ExternalModelRuntimeError(
                "runtime_artifact_unsafe",
                "the external model runtime contains an unsafe startup file",
            )
        actual_files.add(relative_text)
    if actual_files != artifact_names:
        raise ExternalModelRuntimeError(
            "runtime_inventory_mismatch",
            "the external model runtime inventory is not closed",
        )

    artifact_by_name = {
        artifact.relative_path: artifact for artifact in artifacts
    }
    for required_notice in (
        notice_relative.as_posix(),
        *(item.notice_artifact for item in distributions),
    ):
        if required_notice not in artifact_by_name:
            raise ExternalModelRuntimeError(
                "runtime_license_unverified",
                "a declared runtime licence notice is not inventoried",
            )
    for artifact in artifacts:
        artifact_path = _contained_path(
            root,
            PurePosixPath(artifact.relative_path),
            directory=False,
            label="runtime artifact",
        )
        actual_sha256, actual_size = _sha256(artifact_path)
        if (
            actual_size != artifact.size_bytes
            or actual_sha256 != artifact.sha256
        ):
            raise ExternalModelRuntimeError(
                "runtime_artifact_hash_mismatch",
                "an external model runtime artifact failed verification",
            )

    discovered: dict[str, str] = {}
    try:
        for distribution in metadata.distributions(
            path=[str(site_packages)]
        ):
            raw_name = distribution.metadata.get("Name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ExternalModelRuntimeError(
                    "runtime_packages_invalid",
                    "runtime package metadata has no name",
                )
            name = _normal_distribution_name(raw_name)
            if name in discovered:
                raise ExternalModelRuntimeError(
                    "runtime_packages_invalid",
                    "runtime package metadata contains duplicates",
                )
            discovered[name] = str(distribution.version)
    except ExternalModelRuntimeError:
        raise
    except Exception as error:
        raise ExternalModelRuntimeError(
            "runtime_packages_invalid",
            "runtime package metadata could not be read",
        ) from error
    declared = {
        _normal_distribution_name(item.name): item.version
        for item in distributions
    }
    if discovered != declared:
        raise ExternalModelRuntimeError(
            "runtime_packages_invalid",
            "runtime package metadata does not match the manifest",
        )

    return ExternalModelRuntimePack(
        manifest_path=path.resolve(strict=True),
        root=root,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        runtime_id=runtime_id,
        runtime_version=runtime_version,
        source_repository=source_repository,
        source_revision=source_revision,
        runtime_spdx=runtime_spdx,
        commercial_use_confirmed=True,
        redistribution_allowed=license_value["redistribution_allowed"],
        python_cache_tag=python_cache_tag,
        python_version=python_version,
        platform_system=platform_system,
        platform_machine=platform_machine,
        capabilities=capabilities,
        distributions=tuple(distributions),
        site_packages=site_packages,
        dll_directories=dll_directories,
        artifacts=tuple(artifacts),
        requires_cuda=requires_cuda,
        cpu_fallback=cpu_fallback,
        minimum_cuda_version=minimum_cuda_version,
        minimum_vram_bytes=minimum_vram_bytes,
    )


def _default_runtime_probe(
    pack: ExternalModelRuntimePack,
) -> Mapping[str, Any]:
    modules: set[str] = set()
    capabilities = set(pack.capabilities)
    if capabilities & _TORCH_CAPABILITIES:
        modules.add("torch")
    if capabilities & {
        "grounding_dino",
        "sam2",
        "embedding_search",
        "detector_training",
        "detector_inference",
    }:
        modules.update({"transformers", "PIL.Image"})
    if capabilities & {"sam2", "opencv", "korean_ocr", "onnx_inference"}:
        modules.update({"cv2", "numpy"})
    if "korean_ocr" in capabilities:
        modules.add("easyocr")
    if "onnx_inference" in capabilities:
        modules.add("onnxruntime")
    imported = {
        name: importlib.import_module(name)
        for name in sorted(modules)
    }
    transformers = imported.get("transformers")
    if transformers is not None:
        required_attributes = set()
        if "grounding_dino" in capabilities:
            required_attributes.update(
                {"AutoProcessor", "AutoModelForZeroShotObjectDetection"}
            )
        if "sam2" in capabilities:
            required_attributes.update({"Sam2Model", "Sam2Processor"})
        if "embedding_search" in capabilities:
            required_attributes.update({"AutoProcessor", "CLIPModel"})
        if capabilities & {"detector_training", "detector_inference"}:
            required_attributes.update(
                {
                    "AutoConfig",
                    "AutoImageProcessor",
                    "AutoModelForObjectDetection",
                }
            )
        if any(
            not hasattr(transformers, attribute)
            for attribute in required_attributes
        ):
            raise ExternalModelRuntimeError(
                "runtime_capability_probe_failed",
                "the external runtime lacks a required model API",
            )

    result: dict[str, Any] = {
        "cuda_verified": False,
        "cuda_version": None,
        "cuda_total_memory_bytes": None,
    }
    if pack.requires_cuda:
        torch = imported.get("torch")
        try:
            available = bool(torch.cuda.is_available())
            cuda_version = (
                str(torch.version.cuda)
                if torch.version.cuda is not None
                else None
            )
            properties = (
                torch.cuda.get_device_properties(0)
                if available
                else None
            )
            total_memory = (
                int(properties.total_memory)
                if properties is not None
                else None
            )
        except Exception as error:
            raise ExternalModelRuntimeError(
                "runtime_cuda_probe_failed",
                "the external runtime CUDA probe failed",
            ) from error
        if (
            not available
            or not _version_parts(cuda_version)
            or _version_parts(cuda_version)
            < _version_parts(pack.minimum_cuda_version)
            or total_memory is None
            or total_memory < pack.minimum_vram_bytes
        ):
            raise ExternalModelRuntimeError(
                "runtime_cuda_requirements_unsatisfied",
                "the external runtime does not satisfy CUDA and VRAM requirements",
            )
        result.update(
            {
                "cuda_verified": True,
                "cuda_version": cuda_version,
                "cuda_total_memory_bytes": total_memory,
            }
        )
    return result


def activate_external_model_runtime(
    manifest_path: Path | str,
    *,
    runtime_probe: Callable[
        [ExternalModelRuntimePack], Mapping[str, Any]
    ] = _default_runtime_probe,
    add_dll_directory: Optional[Callable[[str], Any]] = None,
) -> Mapping[str, Any]:
    """Activate one verified runtime pack for lazy model imports.

    Activation is process-global and idempotent for the same verified path.
    A different second pack is rejected.  On any failure, newly added search
    paths and DLL handles are rolled back and the scrubbed state is ``failed``.
    """

    global _ACTIVE_SITE_PACKAGES, _CURRENT_STATUS
    pack: Optional[ExternalModelRuntimePack] = None
    inserted = False
    new_handles: list[Any] = []
    try:
        pack = load_external_model_runtime_pack(manifest_path)
        site_packages = str(pack.site_packages)
        if (
            _ACTIVE_SITE_PACKAGES is not None
            and _ACTIVE_SITE_PACKAGES != site_packages
        ):
            raise ExternalModelRuntimeError(
                "runtime_already_activated",
                "a different external model runtime is already active",
            )
        if (
            _ACTIVE_SITE_PACKAGES == site_packages
            and _CURRENT_STATUS.get("state") == "ready"
            and _CURRENT_STATUS.get("manifest_sha256")
            == pack.manifest_sha256
        ):
            return dict(_CURRENT_STATUS)
        if site_packages not in sys.path:
            # Append instead of ``site.addsitedir``: .pth startup code is
            # forbidden and the runtime pack must never shadow bundled bridge
            # or standard-library modules.
            sys.path.append(site_packages)
            inserted = True
        dll_loader = add_dll_directory or getattr(
            os, "add_dll_directory", None
        )
        if pack.dll_directories and dll_loader is None:
            raise ExternalModelRuntimeError(
                "runtime_dll_loader_unavailable",
                "the external runtime DLL loader is unavailable",
            )
        for directory in pack.dll_directories:
            new_handles.append(dll_loader(str(directory)))
        probe = dict(runtime_probe(pack))
        if pack.requires_cuda and probe.get("cuda_verified") is not True:
            raise ExternalModelRuntimeError(
                "runtime_cuda_probe_failed",
                "the external runtime did not prove CUDA execution",
            )
        _DLL_HANDLES.extend(new_handles)
        _ACTIVE_SITE_PACKAGES = site_packages
        _CURRENT_STATUS = MappingProxyType(
            {
                "state": "ready",
                "error_code": None,
                **dict(pack.public_identity),
                "cuda_verified": probe.get("cuda_verified") is True,
                "cuda_version": probe.get("cuda_version"),
                "cuda_total_memory_bytes": probe.get(
                    "cuda_total_memory_bytes"
                ),
            }
        )
    except ExternalModelRuntimeError as error:
        if (
            error.code == "runtime_already_activated"
            and _CURRENT_STATUS.get("state") == "ready"
        ):
            return dict(_CURRENT_STATUS)
        if inserted and pack is not None:
            try:
                sys.path.remove(str(pack.site_packages))
            except ValueError:
                pass
        for handle in reversed(new_handles):
            close = getattr(handle, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        _CURRENT_STATUS = MappingProxyType(
            {"state": "failed", "error_code": error.code}
        )
    except Exception:
        if inserted and pack is not None:
            try:
                sys.path.remove(str(pack.site_packages))
            except ValueError:
                pass
        for handle in reversed(new_handles):
            close = getattr(handle, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        _CURRENT_STATUS = MappingProxyType(
            {
                "state": "failed",
                "error_code": "runtime_activation_failed",
            }
        )
    return dict(_CURRENT_STATUS)


def activate_external_model_runtime_from_environment(
    environ: Optional[Mapping[str, str]] = None,
) -> Mapping[str, Any]:
    values = os.environ if environ is None else environ
    configured = values.get(MODEL_RUNTIME_ENVIRONMENT_KEY)
    if not isinstance(configured, str) or not configured.strip():
        return dict(_CURRENT_STATUS)
    return activate_external_model_runtime(Path(configured))


def external_model_runtime_status() -> Mapping[str, Any]:
    """Return a path-free, credential-free activation snapshot."""

    return dict(_CURRENT_STATUS)


def _reset_external_model_runtime_for_tests() -> None:
    """Reset process-global activation. Tests only; never used by the app."""

    global _ACTIVE_SITE_PACKAGES, _CURRENT_STATUS
    if _ACTIVE_SITE_PACKAGES is not None:
        while _ACTIVE_SITE_PACKAGES in sys.path:
            sys.path.remove(_ACTIVE_SITE_PACKAGES)
    for handle in reversed(_DLL_HANDLES):
        close = getattr(handle, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    _DLL_HANDLES.clear()
    _ACTIVE_SITE_PACKAGES = None
    _CURRENT_STATUS = MappingProxyType(
        {
            "state": "unavailable",
            "error_code": "runtime_pack_not_configured",
        }
    )


__all__ = [
    "ExternalModelRuntimeError",
    "ExternalModelRuntimePack",
    "MODEL_RUNTIME_ENVIRONMENT_KEY",
    "MODEL_RUNTIME_SCHEMA",
    "activate_external_model_runtime",
    "activate_external_model_runtime_from_environment",
    "external_model_runtime_status",
    "load_external_model_runtime_pack",
]
