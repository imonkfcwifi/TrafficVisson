"""Fail-closed local CUDA object-detector training adapter.

This module is the production detector counterpart to the auxiliary CLIP
centroid head.  It intentionally has no default registration and never
downloads a model.  A caller must inject:

* a hash-inventoried local D-FINE-small or RT-DETR-v2 Model Pack;
* a server-owned evidence resolver which proves project/job/candidate/ref
  ownership; and
* optionally, progress, cancellation and checkpoint-resolution hooks.

Only current-video Annotation members with canonical ``training_geometry`` are
eligible.  Classification-only labels and approved historical sample
references are not detector gate credit.  Fixed-validation images are never
passed to the fit loop.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID

from .dataset_versions import canonical_json_bytes
from .derived_detector_geometry import (
    DerivedDetectorGeometryError,
    DerivedDetectorGeometrySample,
    derive_detector_geometry_samples,
)
from .project_model_lifecycle import (
    LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    MINIMUM_CURRENT_CONFIRMED_LABELS,
    ProjectModelLifecycleError,
    ProjectModelTrainerUnavailable,
    ResolvedActiveProjectModel,
    TrainerInvocation,
    TrainerResult,
)
from .training_geometry import (
    TrainingGeometryError,
    canonical_box,
    canonical_training_geometry,
)
from .validation_metrics import (
    CANONICAL_ISSUE_TYPES,
    DEFAULT_MINIMUM_SAMPLES_PER_TYPE,
)


MODEL_PACK_SCHEMA_VERSION = (
    "trafficvisson.local-object-detector-training-pack.v1"
)
TRAINER_VERSION = "1.2"
DATASET_MANIFEST_SCHEMA = "trafficvisson.detector-local-dataset.v2"
ADAPTER_ARTIFACT_SCHEMA = "trafficvisson.local-object-detector-artifact.v1"
EXECUTION_PROVENANCE_SCHEMA = (
    "trafficvisson.local-detector-execution-provenance.v1"
)
MAX_MODEL_PACK_BYTES = 8 * 1024 * 1024 * 1024
MAX_MODEL_PACK_FILES = 512
MAX_TRAINING_IMAGES = 100_000
MAX_IMAGE_BYTES = 64 * 1024 * 1024
DEFAULT_IOU_THRESHOLD = 0.5
DEFAULT_SCORE_THRESHOLD = 0.25
_WINDOWS_REPARSE_ATTRIBUTE = 0x400
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,191}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_REVISION = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_SPDX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_HTTPS = re.compile(r"^https://[^\s]{1,1000}$")
_SUPPORTED_MODELS = {
    ("dfine-small", "ustc-community/dfine-small-coco"),
    ("rtdetr-v2-r18", "PekingU/rtdetr_v2_r18vd"),
}
_EXPECTED_TRANSFORMERS_MODEL_TYPES = {
    "dfine-small": "d_fine",
    "rtdetr-v2-r18": "rt_detr_v2",
}
_FORBIDDEN_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".js",
    ".msi",
    ".ps1",
    ".py",
    ".pyc",
    ".pyd",
    ".sh",
    ".so",
}


class DetectorModelPackError(ValueError):
    """A configured local detector pack is unsafe or unverifiable."""


class DetectorTrainingCancelled(ProjectModelLifecycleError):
    def __init__(self) -> None:
        super().__init__(
            "detector_training_cancelled",
            "local detector training was cancelled",
        )


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(details.st_mode):
        return True
    attributes = getattr(details, "st_file_attributes", 0)
    return bool(attributes & _WINDOWS_REPARSE_ATTRIBUTE)


def _sha256_file(path: Path) -> tuple[str, int]:
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
        raise DetectorModelPackError(
            "an inventoried artifact could not be read"
        ) from error
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise DetectorModelPackError(
            "an inventoried artifact changed while it was hashed"
        )
    return digest.hexdigest(), after.st_size


def _strict_json(path: Path) -> Mapping[str, Any]:
    def pairs(values: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise DetectorModelPackError(
                    "the detector Model Pack contains duplicate JSON keys"
                )
            result[key] = value
        return result

    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                DetectorModelPackError(
                    "the detector Model Pack contains a non-finite number"
                )
            ),
        )
    except DetectorModelPackError:
        raise
    except Exception as error:
        raise DetectorModelPackError(
            "the detector Model Pack manifest is invalid"
        ) from error
    if not isinstance(value, Mapping):
        raise DetectorModelPackError(
            "the detector Model Pack manifest must be an object"
        )
    return value


def _exact_fields(
    value: object,
    fields: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise DetectorModelPackError(f"{label} fields are invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or _IDENTIFIER.fullmatch(value) is None
        or "\\" in value
    ):
        raise DetectorModelPackError(f"{label} is invalid")
    return value


def _version(value: object, label: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise DetectorModelPackError(f"{label} is invalid")
    return value


def _relative_file(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise DetectorModelPackError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or path.suffix.lower() in _FORBIDDEN_SUFFIXES
    ):
        raise DetectorModelPackError(f"{label} is unsafe")
    return path


def _positive_int(value: object, label: str, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > maximum
    ):
        raise DetectorModelPackError(f"{label} is invalid")
    return value


def _finite_rate(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0.0 < float(value) <= 1.0
    ):
        raise DetectorModelPackError(f"{label} is invalid")
    return float(value)


def _runtime_version_at_least(
    actual: Optional[str],
    required: str,
) -> bool:
    def parts(value: str) -> tuple[int, ...]:
        match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
        if match is None:
            return ()
        return tuple(
            int(item) if item is not None else 0
            for item in match.groups()
        )

    if actual is None:
        return False
    actual_parts = parts(actual)
    required_parts = parts(required)
    return bool(
        actual_parts
        and required_parts
        and actual_parts >= required_parts
    )


def _pretrained_detector_config_identity(
    pack: "LocalDetectorModelPack",
    config: object,
) -> dict[str, Any]:
    """Validate the pinned base architecture without trusting its COCO head.

    The official D-FINE/RT-DETR checkpoints are COCO-pretrained and therefore
    intentionally carry an 80-class label head.  The project Model Pack label
    map is the *new* TrafficSuperVision fine-tuning head.  Requiring those two
    maps to already match would make a real fine-tune impossible and would
    encourage silently edited base checkpoints.  Instead, pin the immutable
    model bytes/revision in the Model Pack, verify the allowlisted architecture,
    and replace only the task head through Transformers'
    ``ignore_mismatched_sizes`` path at load time.
    """

    expected_model_type = _EXPECTED_TRANSFORMERS_MODEL_TYPES.get(
        pack.architecture
    )
    model_type = getattr(config, "model_type", None)
    raw_id_to_label = getattr(config, "id2label", None)
    if (
        expected_model_type is None
        or model_type != expected_model_type
        or not isinstance(raw_id_to_label, Mapping)
        or not raw_id_to_label
    ):
        raise ValueError("pretrained detector architecture is invalid")
    try:
        source_labels = {
            int(class_id): str(label)
            for class_id, label in raw_id_to_label.items()
        }
    except Exception as error:
        raise ValueError("pretrained detector label head is invalid") from error
    if (
        sorted(source_labels) != list(range(len(source_labels)))
        or any(not label for label in source_labels.values())
    ):
        raise ValueError("pretrained detector label head is invalid")
    return {
        "model_type": model_type,
        "source_label_count": len(source_labels),
        "target_label_count": len(pack.labels),
        "head_reinitialization_required": (
            source_labels
            != {
                class_id: issue_type
                for class_id, issue_type in pack.labels
            }
        ),
    }


@dataclass(frozen=True)
class DetectorPackArtifact:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class LocalDetectorModelPack:
    manifest_path: Path
    root: Path
    manifest_sha256: str
    pack_id: str
    pack_version: str
    architecture: str
    model_id: str
    model_version: str
    source_repository: str
    source_revision: str
    weights_spdx: str
    runtime_spdx: str
    commercial_use_confirmed: bool
    redistribution_allowed: bool
    model_directory: str
    labels: tuple[tuple[int, str], ...]
    artifacts: tuple[DetectorPackArtifact, ...]
    requires_cuda: bool
    minimum_cuda_version: str
    minimum_vram_bytes: int
    image_size: int
    train_batch_size: int
    gradient_accumulation_steps: int
    epochs: int
    learning_rate: float
    score_threshold: float
    iou_threshold: float

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_PACK_SCHEMA_VERSION,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "architecture": self.architecture,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "weights_spdx": self.weights_spdx,
            "runtime_spdx": self.runtime_spdx,
            "commercial_use_confirmed": self.commercial_use_confirmed,
            "redistribution_allowed": self.redistribution_allowed,
            "manifest_sha256": self.manifest_sha256,
            "artifact_count": len(self.artifacts),
        }

    @property
    def model_root(self) -> Path:
        relative = PurePosixPath(self.model_directory)
        return self.root.joinpath(*relative.parts)


def load_local_detector_model_pack(
    manifest_path: Path | str,
) -> LocalDetectorModelPack:
    """Verify an offline detector training pack without importing ML code."""

    path = Path(manifest_path)
    if not path.is_absolute():
        raise DetectorModelPackError(
            "the detector Model Pack manifest path must be absolute"
        )
    if not path.is_file() or _is_link_or_reparse(path):
        raise DetectorModelPackError(
            "the detector Model Pack manifest is unavailable"
        )
    root = path.parent.resolve(strict=True)
    if _is_link_or_reparse(root):
        raise DetectorModelPackError(
            "the detector Model Pack root cannot be linked or reparse-backed"
        )
    manifest = _strict_json(path)
    expected_top = {
        "schema_version",
        "pack_id",
        "pack_version",
        "architecture",
        "model_id",
        "model_version",
        "source",
        "license",
        "artifacts",
        "model",
        "hardware",
        "training",
    }
    if set(manifest) != expected_top:
        raise DetectorModelPackError(
            "the detector Model Pack fields are invalid"
        )
    if manifest["schema_version"] != MODEL_PACK_SCHEMA_VERSION:
        raise DetectorModelPackError(
            "the detector Model Pack schema is unsupported"
        )
    pack_id = _identifier(manifest["pack_id"], "pack_id")
    pack_version = _version(manifest["pack_version"], "pack_version")
    architecture = _identifier(
        manifest["architecture"], "architecture"
    )
    model_id = _identifier(manifest["model_id"], "model_id")
    model_version = _version(manifest["model_version"], "model_version")
    if (architecture, model_id) not in _SUPPORTED_MODELS:
        raise DetectorModelPackError(
            "the detector architecture/model pair is unsupported"
        )

    source = _exact_fields(
        manifest["source"],
        {"repository", "revision"},
        "source",
    )
    repository = source["repository"]
    revision = source["revision"]
    if (
        not isinstance(repository, str)
        or _HTTPS.fullmatch(repository) is None
        or repository.rstrip("/") != f"https://huggingface.co/{model_id}"
        or not isinstance(revision, str)
        or _REVISION.fullmatch(revision) is None
    ):
        raise DetectorModelPackError(
            "the detector source repository or immutable revision is invalid"
        )

    license_value = _exact_fields(
        manifest["license"],
        {
            "weights_spdx",
            "runtime_spdx",
            "commercial_use_confirmed",
            "redistribution_allowed",
            "license_url",
            "notice_artifact",
        },
        "license",
    )
    for field in ("weights_spdx", "runtime_spdx"):
        if (
            not isinstance(license_value[field], str)
            or _SPDX.fullmatch(license_value[field]) is None
        ):
            raise DetectorModelPackError(
                "the detector SPDX identity is invalid"
            )
    if license_value["commercial_use_confirmed"] is not True:
        raise DetectorModelPackError(
            "commercial_use_confirmed must be true"
        )
    if not isinstance(license_value["redistribution_allowed"], bool):
        raise DetectorModelPackError(
            "redistribution_allowed must be boolean"
        )
    if (
        not isinstance(license_value["license_url"], str)
        or _HTTPS.fullmatch(license_value["license_url"]) is None
    ):
        raise DetectorModelPackError("license_url is invalid")
    notice_path = _relative_file(
        license_value["notice_artifact"],
        "notice_artifact",
    )

    raw_artifacts = manifest["artifacts"]
    if (
        not isinstance(raw_artifacts, Sequence)
        or isinstance(raw_artifacts, (str, bytes, bytearray))
        or not 1 <= len(raw_artifacts) <= MAX_MODEL_PACK_FILES
    ):
        raise DetectorModelPackError(
            "the detector Model Pack artifact inventory is invalid"
        )
    artifacts: list[DetectorPackArtifact] = []
    seen: set[str] = set()
    total_size = 0
    for index, raw in enumerate(raw_artifacts):
        artifact = _exact_fields(
            raw,
            {"path", "size_bytes", "sha256"},
            f"artifacts[{index}]",
        )
        relative = _relative_file(
            artifact["path"],
            f"artifacts[{index}].path",
        )
        relative_text = relative.as_posix()
        if relative_text in seen:
            raise DetectorModelPackError(
                "the detector artifact inventory contains duplicate paths"
            )
        seen.add(relative_text)
        expected_size = _positive_int(
            artifact["size_bytes"],
            f"artifacts[{index}].size_bytes",
            maximum=MAX_MODEL_PACK_BYTES,
        )
        expected_hash = artifact["sha256"]
        if (
            not isinstance(expected_hash, str)
            or _SHA256.fullmatch(expected_hash) is None
        ):
            raise DetectorModelPackError(
                "an inventoried detector artifact digest is invalid"
            )
        target = root.joinpath(*relative.parts)
        try:
            resolved = target.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise DetectorModelPackError(
                "an inventoried detector artifact escaped the Model Pack"
            ) from error
        if not resolved.is_file() or _is_link_or_reparse(resolved):
            raise DetectorModelPackError(
                "an inventoried detector artifact is unavailable"
            )
        actual_hash, actual_size = _sha256_file(resolved)
        if actual_size != expected_size or actual_hash != expected_hash:
            raise DetectorModelPackError(
                "an inventoried detector artifact failed integrity checks"
            )
        total_size += actual_size
        if total_size > MAX_MODEL_PACK_BYTES:
            raise DetectorModelPackError(
                "the detector Model Pack is too large"
            )
        artifacts.append(
            DetectorPackArtifact(
                relative_path=relative_text,
                size_bytes=actual_size,
                sha256=actual_hash,
            )
        )
    if notice_path.as_posix() not in seen:
        raise DetectorModelPackError(
            "the detector license notice is not inventoried"
        )

    actual_files: set[str] = set()
    for target in root.rglob("*"):
        if _is_link_or_reparse(target):
            raise DetectorModelPackError(
                "the detector Model Pack contains a linked entry"
            )
        if target.is_dir():
            continue
        if not target.is_file():
            raise DetectorModelPackError(
                "the detector Model Pack contains an unsupported entry"
            )
        relative = target.relative_to(root).as_posix()
        if target.resolve(strict=True) == path.resolve(strict=True):
            continue
        actual_files.add(relative)
    if actual_files != seen:
        raise DetectorModelPackError(
            "the detector Model Pack differs from its artifact inventory"
        )

    model = _exact_fields(
        manifest["model"],
        {"directory", "labels"},
        "model",
    )
    if not isinstance(model["directory"], str):
        raise DetectorModelPackError("model.directory is invalid")
    model_directory = _relative_file(
        f"{model['directory']}/placeholder",
        "model.directory",
    ).parent.as_posix()
    model_root = root.joinpath(*PurePosixPath(model_directory).parts)
    if not model_root.is_dir() or _is_link_or_reparse(model_root):
        raise DetectorModelPackError(
            "the detector model directory is unavailable"
        )
    if not any(
        item.relative_path.startswith(f"{model_directory}/")
        for item in artifacts
    ):
        raise DetectorModelPackError(
            "the detector model directory has no inventoried artifacts"
        )
    raw_labels = model["labels"]
    if (
        not isinstance(raw_labels, Sequence)
        or isinstance(raw_labels, (str, bytes, bytearray))
        or not raw_labels
    ):
        raise DetectorModelPackError("the detector label map is invalid")
    labels: list[tuple[int, str]] = []
    for index, raw_label in enumerate(raw_labels):
        label = _exact_fields(
            raw_label,
            {"class_id", "issue_type"},
            f"model.labels[{index}]",
        )
        class_id = label["class_id"]
        issue_type = label["issue_type"]
        if (
            isinstance(class_id, bool)
            or class_id != index
            or not isinstance(issue_type, str)
            or issue_type not in CANONICAL_ISSUE_TYPES
            or issue_type in {item[1] for item in labels}
        ):
            raise DetectorModelPackError(
                "detector class IDs must be contiguous and issue types unique"
            )
        labels.append((class_id, issue_type))

    hardware = _exact_fields(
        manifest["hardware"],
        {
            "requires_cuda",
            "minimum_cuda_version",
            "minimum_vram_bytes",
        },
        "hardware",
    )
    if hardware["requires_cuda"] is not True:
        raise DetectorModelPackError(
            "production detector training must require CUDA"
        )
    minimum_cuda = _version(
        hardware["minimum_cuda_version"],
        "minimum_cuda_version",
    )
    minimum_vram = _positive_int(
        hardware["minimum_vram_bytes"],
        "minimum_vram_bytes",
        maximum=256 * 1024 * 1024 * 1024,
    )

    training = _exact_fields(
        manifest["training"],
        {
            "image_size",
            "train_batch_size",
            "gradient_accumulation_steps",
            "epochs",
            "learning_rate",
            "score_threshold",
            "iou_threshold",
        },
        "training",
    )
    image_size = _positive_int(
        training["image_size"], "image_size", maximum=2048
    )
    if image_size < 256:
        raise DetectorModelPackError("image_size is too small")
    train_batch_size = _positive_int(
        training["train_batch_size"],
        "train_batch_size",
        maximum=32,
    )
    gradient_accumulation_steps = _positive_int(
        training["gradient_accumulation_steps"],
        "gradient_accumulation_steps",
        maximum=128,
    )
    epochs = _positive_int(training["epochs"], "epochs", maximum=500)
    learning_rate = training["learning_rate"]
    if (
        isinstance(learning_rate, bool)
        or not isinstance(learning_rate, (int, float))
        or not math.isfinite(float(learning_rate))
        or not 0.0 < float(learning_rate) <= 0.1
    ):
        raise DetectorModelPackError("learning_rate is invalid")
    score_threshold = _finite_rate(
        training["score_threshold"], "score_threshold"
    )
    iou_threshold = _finite_rate(
        training["iou_threshold"], "iou_threshold"
    )

    manifest_hash, _size = _sha256_file(path)
    return LocalDetectorModelPack(
        manifest_path=path.resolve(strict=True),
        root=root,
        manifest_sha256=manifest_hash,
        pack_id=pack_id,
        pack_version=pack_version,
        architecture=architecture,
        model_id=model_id,
        model_version=model_version,
        source_repository=repository,
        source_revision=revision,
        weights_spdx=license_value["weights_spdx"],
        runtime_spdx=license_value["runtime_spdx"],
        commercial_use_confirmed=True,
        redistribution_allowed=license_value["redistribution_allowed"],
        model_directory=model_directory,
        labels=tuple(labels),
        artifacts=tuple(artifacts),
        requires_cuda=True,
        minimum_cuda_version=minimum_cuda,
        minimum_vram_bytes=minimum_vram,
        image_size=image_size,
        train_batch_size=train_batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        epochs=epochs,
        learning_rate=float(learning_rate),
        score_threshold=score_threshold,
        iou_threshold=iou_threshold,
    )


@dataclass(frozen=True)
class ResolvedDetectorArtifact:
    """Server-resolved immutable image with explicit ownership binding."""

    path: Path
    media_type: str
    size_bytes: int
    sha256: str
    project_id: str
    analysis_job_id: str
    candidate_id: str
    artifact_ref: str


DetectorArtifactResolver = Callable[
    [UUID, UUID, str, str],
    ResolvedDetectorArtifact,
]


@dataclass(frozen=True)
class ResolvedDerivedDetectorArtifact:
    """Server-resolved tracked frame bound to one immutable event sample."""

    path: Path
    media_type: str
    size_bytes: int
    sha256: str
    project_id: str
    analysis_job_id: str
    candidate_id: str
    evidence_candidate_id: str
    artifact_ref: str
    event_group_id: str
    frame_id: str
    frame_sha256: str
    box_sha256: str
    mask_sha256: Optional[str]


DerivedDetectorArtifactResolver = Callable[
    [UUID, UUID, str, DerivedDetectorGeometrySample],
    ResolvedDerivedDetectorArtifact,
]


@dataclass(frozen=True)
class DetectorRuntimeInfo:
    dependencies_ready: bool
    accelerator: str
    resolved_device: str
    accelerator_used: bool
    torch_version: Optional[str]
    transformers_version: Optional[str]
    torch_cuda_version: Optional[str]
    cuda_device_name: Optional[str]
    cuda_total_memory_bytes: Optional[int]
    error_code: Optional[str] = None


@dataclass(frozen=True)
class DetectorTrainerStatus:
    state: str
    code: str
    message: str
    trainer_id: str
    trainer_version: str
    model_pack_identity: Optional[Mapping[str, Any]]
    runtime: Optional[Mapping[str, Any]]

    def public_value(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "code": self.code,
            "message": self.message,
            "trainer_id": self.trainer_id,
            "trainer_version": self.trainer_version,
            "model_pack_identity": (
                dict(self.model_pack_identity)
                if self.model_pack_identity is not None
                else None
            ),
            "runtime": dict(self.runtime) if self.runtime is not None else None,
        }


@dataclass(frozen=True)
class DetectorSample:
    sample_id: str
    annotation_id: str
    project_id: str
    analysis_job_id: str
    candidate_id: str
    split: str
    partition: str
    issue_type: Optional[str]
    image_path: Path
    image_sha256: str
    image_size_bytes: int
    media_type: str
    artifact_ref: str
    normalized_box: Optional[Mapping[str, float]]
    geometry_sha256: str
    group_hashes: tuple[str, ...]
    event_group_id: Optional[str] = None
    source_frame_kind: str = "representative"
    frame_id: Optional[str] = None
    frame_sha256: Optional[str] = None
    box_sha256: Optional[str] = None
    mask_sha256: Optional[str] = None
    source_geometry_sha256: Optional[str] = None
    leakage_group_sha256: Optional[str] = None
    evidence_candidate_id: Optional[str] = None


@dataclass(frozen=True)
class DetectorBoxPrediction:
    sample_id: str
    issue_type: str
    score: float
    normalized_box: Mapping[str, float]


class ActiveDetectorEvaluationProvider(Protocol):
    def evaluate_fixed_validation(
        self,
        model: ResolvedActiveProjectModel,
        samples: Sequence[DetectorSample],
        *,
        fixed_validation_sha256: str,
    ) -> Sequence[DetectorBoxPrediction]:
        ...


ActiveDetectorModelResolver = Callable[
    [UUID],
    ResolvedActiveProjectModel,
]


@dataclass(frozen=True)
class VerifiedDetectorCheckpoint:
    path: Path
    sha256: str
    project_id: str
    training_job_id: str
    request_manifest_sha256: str


CheckpointResolver = Callable[
    [UUID, UUID, str],
    Optional[VerifiedDetectorCheckpoint],
]
ProgressHook = Callable[[Mapping[str, Any]], None]
CancelHook = Callable[[], bool]


@dataclass(frozen=True)
class DetectorBackendRequest:
    pack: LocalDetectorModelPack
    invocation: TrainerInvocation
    train_samples: tuple[DetectorSample, ...]
    validation_samples: tuple[DetectorSample, ...]
    output_directory: Path
    resume_checkpoint: Optional[VerifiedDetectorCheckpoint]


@dataclass(frozen=True)
class DetectorBackendResult:
    primary_weight_relative_path: str
    active_predictions: tuple[DetectorBoxPrediction, ...]
    candidate_predictions: tuple[DetectorBoxPrediction, ...]
    execution_provenance: Mapping[str, Any]
    resumed_from_checkpoint_sha256: Optional[str] = None


class DetectorTrainingBackend(Protocol):
    backend_id: str
    backend_version: str

    def runtime_info(
        self,
        pack: LocalDetectorModelPack,
    ) -> DetectorRuntimeInfo:
        ...

    def train(
        self,
        request: DetectorBackendRequest,
        *,
        progress: ProgressHook,
        cancelled: CancelHook,
    ) -> DetectorBackendResult:
        ...


def normalized_xywh_to_coco(
    box: Mapping[str, Any],
    *,
    image_width: int,
    image_height: int,
) -> tuple[float, float, float, float]:
    """Convert canonical unit XYWH into clamped absolute COCO XYWH."""

    try:
        value = canonical_box(box)
    except TrainingGeometryError as error:
        raise ProjectModelLifecycleError(
            "annotation_geometry_invalid",
            "detector box is invalid",
        ) from error
    if (
        isinstance(image_width, bool)
        or isinstance(image_height, bool)
        or not isinstance(image_width, int)
        or not isinstance(image_height, int)
        or image_width < 1
        or image_height < 1
    ):
        raise ProjectModelLifecycleError(
            "training_image_invalid",
            "detector image dimensions are invalid",
        )
    x = min(float(image_width), value["x"] * image_width)
    y = min(float(image_height), value["y"] * image_height)
    width = min(
        float(image_width) - x,
        value["width"] * image_width,
    )
    height = min(
        float(image_height) - y,
        value["height"] * image_height,
    )
    if width <= 0.0 or height <= 0.0:
        raise ProjectModelLifecycleError(
            "annotation_geometry_invalid",
            "detector box has no pixel area",
        )
    return (x, y, width, height)


def _normalized_iou(
    left: Mapping[str, float],
    right: Mapping[str, float],
) -> float:
    lx1 = float(left["x"])
    ly1 = float(left["y"])
    lx2 = lx1 + float(left["width"])
    ly2 = ly1 + float(left["height"])
    rx1 = float(right["x"])
    ry1 = float(right["y"])
    rx2 = rx1 + float(right["width"])
    ry2 = ry1 + float(right["height"])
    intersection = max(0.0, min(lx2, rx2) - max(lx1, rx1)) * max(
        0.0,
        min(ly2, ry2) - max(ly1, ry1),
    )
    union = (
        float(left["width"]) * float(left["height"])
        + float(right["width"]) * float(right["height"])
        - intersection
    )
    return intersection / union if union > 0.0 else 0.0


def _canonical_prediction(
    prediction: DetectorBoxPrediction,
    *,
    validation_ids: set[str],
    required_types: set[str],
) -> DetectorBoxPrediction:
    if (
        not isinstance(prediction, DetectorBoxPrediction)
        or prediction.sample_id not in validation_ids
        or prediction.issue_type not in required_types
        or isinstance(prediction.score, bool)
        or not isinstance(prediction.score, (int, float))
        or not math.isfinite(float(prediction.score))
        or not 0.0 <= float(prediction.score) <= 1.0
    ):
        raise ProjectModelLifecycleError(
            "detector_prediction_invalid",
            "detector validation prediction is invalid",
        )
    try:
        box = canonical_box(prediction.normalized_box)
    except TrainingGeometryError as error:
        raise ProjectModelLifecycleError(
            "detector_prediction_invalid",
            "detector validation prediction box is invalid",
        ) from error
    return DetectorBoxPrediction(
        sample_id=prediction.sample_id,
        issue_type=prediction.issue_type,
        score=round(float(prediction.score), 8),
        normalized_box=box,
    )


def _event_predictions(
    *,
    prefix: str,
    predictions: Sequence[DetectorBoxPrediction],
    validation: Sequence[DetectorSample],
    required_types: Sequence[str],
    score_threshold: float,
    iou_threshold: float,
) -> tuple[dict[str, Any], ...]:
    samples = {item.sample_id: item for item in validation}
    required = set(required_types)
    rows: list[dict[str, Any]] = []
    canonical = []
    for item in predictions:
        value = _canonical_prediction(
            item,
            validation_ids=set(samples),
            required_types=required,
        )
        if value.score >= score_threshold:
            canonical.append(value)
    canonical.sort(
        key=lambda item: (
            item.sample_id,
            item.issue_type,
            -item.score,
            canonical_json_bytes(item.normalized_box),
        )
    )
    for index, prediction in enumerate(canonical):
        sample = samples[prediction.sample_id]
        truth_id: Optional[str] = None
        if (
            sample.partition == "approved"
            and sample.issue_type == prediction.issue_type
            and sample.normalized_box is not None
            and _normalized_iou(
                sample.normalized_box,
                prediction.normalized_box,
            )
            >= iou_threshold
        ):
            truth_id = f"event-{sample.candidate_id}"
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "prefix": prefix,
                    "index": index,
                    "sample_id": prediction.sample_id,
                    "issue_type": prediction.issue_type,
                    "score": prediction.score,
                    "normalized_box": prediction.normalized_box,
                }
            )
        ).hexdigest()[:24]
        rows.append(
            {
                "prediction_event_id": f"{prefix}-{digest}",
                "sample_id": prediction.sample_id,
                "issue_type": prediction.issue_type,
                "matched_truth_event_id": truth_id,
            }
        )
    return tuple(rows)


def _average_precision(points: Sequence[tuple[float, float]]) -> float:
    if not points:
        return 0.0
    values = []
    for threshold in range(101):
        recall_threshold = threshold / 100.0
        values.append(
            max(
                (
                    precision
                    for recall, precision in points
                    if recall >= recall_threshold
                ),
                default=0.0,
            )
        )
    return sum(values) / 101.0


def _box_metrics(
    predictions: Sequence[DetectorBoxPrediction],
    validation: Sequence[DetectorSample],
    required_types: Sequence[str],
    *,
    score_threshold: float,
    iou_threshold: float,
) -> dict[str, Any]:
    by_id = {item.sample_id: item for item in validation}
    per_type = []
    aps = []
    for issue_type in required_types:
        truth = {
            item.sample_id: item.normalized_box
            for item in validation
            if (
                item.partition == "approved"
                and item.issue_type == issue_type
                and item.normalized_box is not None
            )
        }
        rows = sorted(
            (
                item
                for item in predictions
                if (
                    item.issue_type == issue_type
                    and item.score >= score_threshold
                )
            ),
            key=lambda item: (-item.score, item.sample_id),
        )
        matched: set[str] = set()
        true_positives = 0
        false_positives = 0
        duplicates = 0
        curve = []
        for prediction in rows:
            sample = by_id[prediction.sample_id]
            target = truth.get(prediction.sample_id)
            is_match = (
                target is not None
                and _normalized_iou(
                    target,
                    prediction.normalized_box,
                )
                >= iou_threshold
            )
            if is_match and sample.sample_id not in matched:
                matched.add(sample.sample_id)
                true_positives += 1
            else:
                false_positives += 1
                if is_match:
                    duplicates += 1
            precision = true_positives / (
                true_positives + false_positives
            )
            recall = (
                true_positives / len(truth) if truth else 0.0
            )
            curve.append((recall, precision))
        average_precision = _average_precision(curve)
        aps.append(average_precision)
        per_type.append(
            {
                "issue_type": issue_type,
                "validation_sample_count": len(validation),
                "truth_box_count": len(truth),
                "prediction_box_count": len(rows),
                "true_positive_count": true_positives,
                "false_positive_count": false_positives,
                "false_negative_count": len(truth) - true_positives,
                "duplicate_prediction_count": duplicates,
                "recall_at_iou": round(
                    true_positives / len(truth) if truth else 0.0,
                    8,
                ),
                "ap_at_iou": round(average_precision, 8),
            }
        )
    return {
        "iou_threshold": iou_threshold,
        "score_threshold": score_threshold,
        "map_at_iou": round(sum(aps) / len(aps), 8) if aps else 0.0,
        "per_type": per_type,
    }


class TransformersCudaDetectorBackend:
    """Offline Hugging Face D-FINE/RT-DETR-v2 training implementation.

    Imports are lazy so the lightweight bridge remains usable without the
    optional Model Pack runtime.  ``local_files_only=True`` and
    ``trust_remote_code=False`` are non-configurable.
    """

    backend_id = "transformers-auto-object-detection-local-cuda"
    backend_version = "1.0"

    def runtime_info(
        self,
        pack: LocalDetectorModelPack,
    ) -> DetectorRuntimeInfo:
        try:
            import torch
            import transformers
            from PIL import Image  # noqa: F401
            from transformers import (  # noqa: F401
                AutoConfig,
                AutoImageProcessor,
                AutoModelForObjectDetection,
            )
        except Exception as error:
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
                error_code=f"dependency_import_failed:{type(error).__name__}",
            )
        try:
            config = AutoConfig.from_pretrained(
                str(pack.model_root),
                local_files_only=True,
                trust_remote_code=False,
            )
            _pretrained_detector_config_identity(pack, config)
        except Exception as error:
            return DetectorRuntimeInfo(
                dependencies_ready=False,
                accelerator="unavailable",
                resolved_device="unavailable",
                accelerator_used=False,
                torch_version=str(torch.__version__),
                transformers_version=str(transformers.__version__),
                torch_cuda_version=(
                    str(torch.version.cuda)
                    if torch.version.cuda is not None
                    else None
                ),
                cuda_device_name=None,
                cuda_total_memory_bytes=None,
                error_code=f"local_config_invalid:{type(error).__name__}",
            )
        cuda_available = bool(torch.cuda.is_available())
        if not cuda_available:
            return DetectorRuntimeInfo(
                dependencies_ready=True,
                accelerator="cpu",
                resolved_device="cpu",
                accelerator_used=False,
                torch_version=str(torch.__version__),
                transformers_version=str(transformers.__version__),
                torch_cuda_version=(
                    str(torch.version.cuda)
                    if torch.version.cuda is not None
                    else None
                ),
                cuda_device_name=None,
                cuda_total_memory_bytes=None,
                error_code="cuda_unavailable",
            )
        try:
            properties = torch.cuda.get_device_properties(0)
            name = str(properties.name)
            memory = int(properties.total_memory)
        except Exception as error:
            return DetectorRuntimeInfo(
                dependencies_ready=True,
                accelerator="cuda",
                resolved_device="cuda:0",
                accelerator_used=False,
                torch_version=str(torch.__version__),
                transformers_version=str(transformers.__version__),
                torch_cuda_version=(
                    str(torch.version.cuda)
                    if torch.version.cuda is not None
                    else None
                ),
                cuda_device_name=None,
                cuda_total_memory_bytes=None,
                error_code=f"cuda_probe_failed:{type(error).__name__}",
            )
        return DetectorRuntimeInfo(
            dependencies_ready=True,
            accelerator="cuda",
            resolved_device="cuda:0",
            accelerator_used=True,
            torch_version=str(torch.__version__),
            transformers_version=str(transformers.__version__),
            torch_cuda_version=(
                str(torch.version.cuda)
                if torch.version.cuda is not None
                else None
            ),
            cuda_device_name=name,
            cuda_total_memory_bytes=memory,
            error_code=None,
        )

    @staticmethod
    def _verified_image(sample: DetectorSample):
        try:
            from PIL import Image

            actual_hash, actual_size = _sha256_file(sample.image_path)
            if (
                actual_hash != sample.image_sha256
                or actual_size != sample.image_size_bytes
            ):
                raise ProjectModelLifecycleError(
                    "training_evidence_integrity_failed",
                    "a detector image changed after dataset preparation",
                )
            with Image.open(sample.image_path) as source:
                source.verify()
            with Image.open(sample.image_path) as source:
                image = source.convert("RGB")
                image.load()
                return image
        except ProjectModelLifecycleError:
            raise
        except Exception as error:
            raise ProjectModelLifecycleError(
                "training_image_invalid",
                "a detector image could not be decoded",
            ) from error

    @staticmethod
    def _target(
        sample: DetectorSample,
        *,
        width: int,
        height: int,
        label_to_id: Mapping[str, int],
    ) -> dict[str, Any]:
        annotations = []
        if sample.partition == "approved":
            if (
                sample.issue_type is None
                or sample.normalized_box is None
            ):
                raise ProjectModelLifecycleError(
                    "annotation_geometry_invalid",
                    "positive detector sample has no box or issue type",
                )
            bbox = normalized_xywh_to_coco(
                sample.normalized_box,
                image_width=width,
                image_height=height,
            )
            annotations.append(
                {
                    "bbox": list(bbox),
                    "category_id": label_to_id[sample.issue_type],
                    "area": bbox[2] * bbox[3],
                    "iscrowd": 0,
                }
            )
        image_id = int(
            hashlib.sha256(sample.sample_id.encode("utf-8")).hexdigest()[:12],
            16,
        )
        return {"image_id": image_id, "annotations": annotations}

    @staticmethod
    def _checkpoint(
        *,
        torch_module: Any,
        path: Path,
        model: Any,
        optimizer: Any,
        epoch: int,
        request_sha256: str,
    ) -> str:
        temporary = path.with_suffix(".tmp")
        torch_module.save(
            {
                "epoch": epoch,
                "request_manifest_sha256": request_sha256,
                "model_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
            },
            temporary,
        )
        os.replace(temporary, path)
        digest, _size = _sha256_file(path)
        return digest

    @staticmethod
    def _resume(
        *,
        torch_module: Any,
        checkpoint: VerifiedDetectorCheckpoint,
        model: Any,
        optimizer: Any,
        request_sha256: str,
    ) -> int:
        actual_hash, _size = _sha256_file(checkpoint.path)
        if (
            actual_hash != checkpoint.sha256
            or checkpoint.request_manifest_sha256 != request_sha256
        ):
            raise ProjectModelLifecycleError(
                "detector_checkpoint_invalid",
                "the detector checkpoint binding changed",
            )
        try:
            value = torch_module.load(
                checkpoint.path,
                map_location="cpu",
                weights_only=True,
            )
            if (
                not isinstance(value, Mapping)
                or value.get("request_manifest_sha256") != request_sha256
                or isinstance(value.get("epoch"), bool)
                or not isinstance(value.get("epoch"), int)
                or value["epoch"] < 0
            ):
                raise ValueError("checkpoint schema")
            model.load_state_dict(value["model_state_dict"])
            optimizer.load_state_dict(value["optimizer_state_dict"])
            return int(value["epoch"]) + 1
        except ProjectModelLifecycleError:
            raise
        except Exception as error:
            raise ProjectModelLifecycleError(
                "detector_checkpoint_invalid",
                "the detector checkpoint could not be resumed",
            ) from error

    @staticmethod
    def _infer(
        *,
        torch_module: Any,
        processor: Any,
        model: Any,
        samples: Sequence[DetectorSample],
        id_to_label: Mapping[int, str],
        device: Any,
        score_threshold: float,
        image_size: int,
        progress: ProgressHook,
        cancelled: CancelHook,
        phase: str,
    ) -> tuple[DetectorBoxPrediction, ...]:
        predictions: list[DetectorBoxPrediction] = []
        model.eval()
        for index, sample in enumerate(samples):
            if cancelled():
                raise DetectorTrainingCancelled()
            image = TransformersCudaDetectorBackend._verified_image(sample)
            inputs = processor(
                images=image,
                return_tensors="pt",
                size={"height": image_size, "width": image_size},
            )
            inputs = {
                key: value.to(device)
                for key, value in inputs.items()
            }
            with torch_module.inference_mode():
                outputs = model(**inputs)
            target_sizes = torch_module.tensor(
                [[image.height, image.width]],
                device=device,
            )
            rows = processor.post_process_object_detection(
                outputs,
                threshold=score_threshold,
                target_sizes=target_sizes,
            )[0]
            for score, label, box in zip(
                rows["scores"],
                rows["labels"],
                rows["boxes"],
            ):
                class_id = int(label.detach().cpu().item())
                issue_type = id_to_label.get(class_id)
                if issue_type is None:
                    raise ProjectModelLifecycleError(
                        "detector_prediction_invalid",
                        "the detector emitted an undeclared class ID",
                    )
                x1, y1, x2, y2 = [
                    float(value)
                    for value in box.detach().cpu().tolist()
                ]
                normalized = {
                    "x": max(0.0, min(1.0, x1 / image.width)),
                    "y": max(0.0, min(1.0, y1 / image.height)),
                    "width": max(
                        0.0,
                        min(1.0, (x2 - x1) / image.width),
                    ),
                    "height": max(
                        0.0,
                        min(1.0, (y2 - y1) / image.height),
                    ),
                }
                try:
                    normalized = canonical_box(normalized)
                except TrainingGeometryError:
                    continue
                predictions.append(
                    DetectorBoxPrediction(
                        sample_id=sample.sample_id,
                        issue_type=issue_type,
                        score=float(score.detach().cpu().item()),
                        normalized_box=normalized,
                    )
                )
            progress(
                {
                    "stage": phase,
                    "current": index + 1,
                    "total": len(samples),
                }
            )
        return tuple(predictions)

    def train(
        self,
        request: DetectorBackendRequest,
        *,
        progress: ProgressHook,
        cancelled: CancelHook,
    ) -> DetectorBackendResult:
        try:
            import torch
            from transformers import (
                AutoConfig,
                AutoImageProcessor,
                AutoModelForObjectDetection,
            )
        except Exception as error:
            raise ProjectModelTrainerUnavailable(
                "the local Transformers detector runtime is unavailable"
            ) from error

        if cancelled():
            raise DetectorTrainingCancelled()
        device = torch.device("cuda:0")
        seed = int(
            request.invocation.request_manifest_sha256[:8],
            16,
        )
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        label_to_id = {
            issue_type: class_id
            for class_id, issue_type in request.pack.labels
        }
        id_to_label = {
            class_id: issue_type
            for class_id, issue_type in request.pack.labels
        }
        missing = set(request.invocation.required_issue_types).difference(
            label_to_id
        )
        if missing:
            raise ProjectModelLifecycleError(
                "detector_model_pack_coverage_missing",
                "the detector Model Pack does not cover every required type",
            )

        # Prevent accidental Hub/network behavior even if a downstream
        # Transformers release changes an internal default.
        previous_offline = {
            key: os.environ.get(key)
            for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
        }
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            config = AutoConfig.from_pretrained(
                str(request.pack.model_root),
                local_files_only=True,
                trust_remote_code=False,
            )
            base_identity = _pretrained_detector_config_identity(
                request.pack,
                config,
            )
            processor = AutoImageProcessor.from_pretrained(
                str(request.pack.model_root),
                local_files_only=True,
                trust_remote_code=False,
            )
            model = AutoModelForObjectDetection.from_pretrained(
                str(request.pack.model_root),
                local_files_only=True,
                trust_remote_code=False,
                id2label=id_to_label,
                label2id=label_to_id,
                ignore_mismatched_sizes=True,
            )
            configured_labels = {
                int(class_id): str(issue_type)
                for class_id, issue_type in model.config.id2label.items()
            }
            if configured_labels != id_to_label:
                raise ProjectModelLifecycleError(
                    "detector_model_pack_label_mismatch",
                    (
                        "the reinitialized detector label head differs "
                        "from its verified Model Pack manifest"
                    ),
                )
        except ProjectModelLifecycleError:
            raise
        except Exception as error:
            raise ProjectModelLifecycleError(
                "detector_model_load_failed",
                "the verified local detector model could not be loaded",
            ) from error
        finally:
            for key, value in previous_offline.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        model.to(device)
        # This is a newly initialized project head over a pinned COCO
        # backbone, not the project's currently promoted detector.  It must
        # never be reported as the active-model baseline.  A compatible
        # promoted detector is evaluated through the lifecycle's explicit
        # active-model evaluator; until then the baseline is honestly empty.
        active_predictions: tuple[DetectorBoxPrediction, ...] = ()

        optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=request.pack.learning_rate,
        )
        start_epoch = 0
        resumed_hash = None
        if request.resume_checkpoint is not None:
            start_epoch = self._resume(
                torch_module=torch,
                checkpoint=request.resume_checkpoint,
                model=model,
                optimizer=optimizer,
                request_sha256=(
                    request.invocation.request_manifest_sha256
                ),
            )
            resumed_hash = request.resume_checkpoint.sha256
        scaler = torch.amp.GradScaler("cuda", enabled=True)
        checkpoint_path = request.output_directory / "checkpoint.pt"
        batch_size = request.pack.train_batch_size
        accumulation = request.pack.gradient_accumulation_steps
        total_batches = math.ceil(
            len(request.train_samples) / batch_size
        )
        optimizer.zero_grad(set_to_none=True)
        for epoch in range(start_epoch, request.pack.epochs):
            model.train()
            for batch_index in range(total_batches):
                if cancelled():
                    self._checkpoint(
                        torch_module=torch,
                        path=checkpoint_path,
                        model=model,
                        optimizer=optimizer,
                        epoch=epoch,
                        request_sha256=(
                            request.invocation.request_manifest_sha256
                        ),
                    )
                    raise DetectorTrainingCancelled()
                batch = request.train_samples[
                    batch_index * batch_size : (batch_index + 1) * batch_size
                ]
                images = [self._verified_image(item) for item in batch]
                targets = [
                    self._target(
                        item,
                        width=image.width,
                        height=image.height,
                        label_to_id=label_to_id,
                    )
                    for item, image in zip(batch, images)
                ]
                encoded = processor(
                    images=images,
                    annotations=targets,
                    return_tensors="pt",
                    size={
                        "height": request.pack.image_size,
                        "width": request.pack.image_size,
                    },
                )
                labels = [
                    {
                        key: value.to(device)
                        if hasattr(value, "to")
                        else value
                        for key, value in label.items()
                    }
                    for label in encoded.pop("labels")
                ]
                encoded = {
                    key: value.to(device)
                    for key, value in encoded.items()
                }
                with torch.amp.autocast(
                    device_type="cuda",
                    dtype=torch.float16,
                ):
                    loss = model(**encoded, labels=labels).loss
                    loss = loss / accumulation
                scaler.scale(loss).backward()
                if (
                    (batch_index + 1) % accumulation == 0
                    or batch_index + 1 == total_batches
                ):
                    scaler.step(optimizer)
                    scaler.update()
                    optimizer.zero_grad(set_to_none=True)
                progress(
                    {
                        "stage": "fine_tune",
                        "epoch": epoch + 1,
                        "epochs": request.pack.epochs,
                        "current": batch_index + 1,
                        "total": total_batches,
                        "loss": round(float(loss.detach().cpu()) * accumulation, 8),
                    }
                )
            self._checkpoint(
                torch_module=torch,
                path=checkpoint_path,
                model=model,
                optimizer=optimizer,
                epoch=epoch,
                request_sha256=request.invocation.request_manifest_sha256,
            )

        candidate_predictions = self._infer(
            torch_module=torch,
            processor=processor,
            model=model,
            samples=request.validation_samples,
            id_to_label=id_to_label,
            device=device,
            score_threshold=request.pack.score_threshold,
            image_size=request.pack.image_size,
            progress=progress,
            cancelled=cancelled,
            phase="candidate_fixed_validation",
        )
        model_output = request.output_directory / "detector"
        model_output.mkdir(parents=False, exist_ok=False)
        model.save_pretrained(
            model_output,
            safe_serialization=True,
        )
        processor.save_pretrained(model_output)
        weight_files = sorted(model_output.glob("*.safetensors"))
        if not weight_files:
            raise ProjectModelLifecycleError(
                "detector_artifact_missing",
                "the trained detector produced no safetensors weights",
            )
        # A successful immutable model artifact does not need the mutable
        # optimizer checkpoint.  Failed/cancelled attempts keep it available
        # for a server-owned checkpoint resolver.
        checkpoint_path.unlink(missing_ok=True)
        primary = weight_files[0].relative_to(
            request.output_directory
        ).as_posix()
        info = self.runtime_info(request.pack)
        return DetectorBackendResult(
            primary_weight_relative_path=primary,
            active_predictions=active_predictions,
            candidate_predictions=candidate_predictions,
            resumed_from_checkpoint_sha256=resumed_hash,
            execution_provenance={
                "schema_version": EXECUTION_PROVENANCE_SCHEMA,
                "backend_id": self.backend_id,
                "backend_version": self.backend_version,
                "accelerator": info.accelerator,
                "resolved_device": info.resolved_device,
                "accelerator_used": info.accelerator_used,
                "torch_version": info.torch_version,
                "transformers_version": info.transformers_version,
                "torch_cuda_version": info.torch_cuda_version,
                "cuda_device_name": info.cuda_device_name,
                "cuda_total_memory_bytes": (
                    info.cuda_total_memory_bytes
                ),
                "automatic_mixed_precision": True,
                "gradient_accumulation_steps": accumulation,
                "deterministic_seed": seed,
                "base_model_type": base_identity["model_type"],
                "base_source_label_count": (
                    base_identity["source_label_count"]
                ),
                "target_label_count": base_identity["target_label_count"],
                "project_head_reinitialized": (
                    base_identity["head_reinitialization_required"]
                ),
                "hub_upload": False,
                "network_model_download": False,
                "trust_remote_code": False,
            },
        )


class LocalCudaObjectDetectorTrainer:
    """Production ``ProjectModelTrainer`` adapter, enabled only when ready."""

    trainer_id = "local-cuda-object-detector"
    test_only = False

    def __init__(
        self,
        *,
        model_pack_manifest: Optional[Path | str],
        artifact_resolver: DetectorArtifactResolver,
        derived_artifact_resolver: Optional[
            DerivedDetectorArtifactResolver
        ] = None,
        backend: Optional[DetectorTrainingBackend] = None,
        active_model_resolver: Optional[
            ActiveDetectorModelResolver
        ] = None,
        active_model_evaluator: Optional[
            ActiveDetectorEvaluationProvider
        ] = None,
        checkpoint_resolver: Optional[CheckpointResolver] = None,
        progress_hook: Optional[ProgressHook] = None,
        cancel_hook: Optional[CancelHook] = None,
        minimum_training_geometry: int = (
            MINIMUM_CURRENT_CONFIRMED_LABELS
        ),
        minimum_validation_geometry_per_type: int = (
            DEFAULT_MINIMUM_SAMPLES_PER_TYPE
        ),
    ) -> None:
        if (
            isinstance(minimum_training_geometry, bool)
            or not isinstance(minimum_training_geometry, int)
            or minimum_training_geometry < 1
            or isinstance(minimum_validation_geometry_per_type, bool)
            or not isinstance(
                minimum_validation_geometry_per_type, int
            )
            or minimum_validation_geometry_per_type < 1
        ):
            raise ValueError("detector geometry gates must be positive")
        self._artifact_resolver = artifact_resolver
        self._derived_artifact_resolver = derived_artifact_resolver
        self._backend = backend or TransformersCudaDetectorBackend()
        if (
            active_model_resolver is None
        ) != (
            active_model_evaluator is None
        ):
            raise ValueError(
                "active detector resolver and evaluator must be configured together"
            )
        self._active_model_resolver = active_model_resolver
        self._active_model_evaluator = active_model_evaluator
        self._checkpoint_resolver = checkpoint_resolver
        self._progress_hook = progress_hook or (lambda _value: None)
        self._cancel_hook = cancel_hook or (lambda: False)
        self._minimum_training_geometry = minimum_training_geometry
        self._minimum_validation_geometry = (
            minimum_validation_geometry_per_type
        )
        self._pack: Optional[LocalDetectorModelPack] = None
        self._runtime: Optional[DetectorRuntimeInfo] = None
        self._status = self._build_status(model_pack_manifest)
        self.trainer_version = self._status.trainer_version
        self.available = self._status.state == "ready"

    def _build_status(
        self,
        manifest_path: Optional[Path | str],
    ) -> DetectorTrainerStatus:
        if manifest_path is None:
            return DetectorTrainerStatus(
                state="unavailable",
                code="detector_model_pack_not_configured",
                message="a local detector training Model Pack is not configured",
                trainer_id=self.trainer_id,
                trainer_version=f"{TRAINER_VERSION}-unavailable",
                model_pack_identity=None,
                runtime=None,
            )
        try:
            pack = load_local_detector_model_pack(manifest_path)
        except DetectorModelPackError as error:
            return DetectorTrainerStatus(
                state="failed",
                code="detector_model_pack_invalid",
                message=str(error),
                trainer_id=self.trainer_id,
                trainer_version=f"{TRAINER_VERSION}-invalid",
                model_pack_identity=None,
                runtime=None,
            )
        self._pack = pack
        backend_binding_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "backend_id": self._backend.backend_id,
                    "backend_version": self._backend.backend_version,
                }
            )
        ).hexdigest()
        bound_trainer_version = (
            f"{TRAINER_VERSION}-{pack.manifest_sha256[:12]}-"
            f"{backend_binding_sha256[:12]}"
        )
        try:
            runtime = self._backend.runtime_info(pack)
        except Exception as error:
            return DetectorTrainerStatus(
                state="failed",
                code="detector_runtime_probe_failed",
                message=type(error).__name__,
                trainer_id=self.trainer_id,
                trainer_version=bound_trainer_version,
                model_pack_identity=pack.identity,
                runtime=None,
            )
        self._runtime = runtime
        version = bound_trainer_version
        public_runtime = {
            "dependencies_ready": runtime.dependencies_ready,
            "accelerator": runtime.accelerator,
            "resolved_device": runtime.resolved_device,
            "accelerator_used": runtime.accelerator_used,
            "torch_version": runtime.torch_version,
            "transformers_version": runtime.transformers_version,
            "torch_cuda_version": runtime.torch_cuda_version,
            "cuda_device_name": runtime.cuda_device_name,
            "cuda_total_memory_bytes": runtime.cuda_total_memory_bytes,
            "error_code": runtime.error_code,
        }
        if not runtime.dependencies_ready:
            code = "detector_runtime_unavailable"
        elif (
            runtime.accelerator != "cuda"
            or runtime.accelerator_used is not True
            or not runtime.resolved_device.startswith("cuda")
        ):
            code = "detector_cuda_required"
        elif (
            runtime.cuda_total_memory_bytes is None
            or runtime.cuda_total_memory_bytes
            < pack.minimum_vram_bytes
        ):
            code = "detector_vram_insufficient"
        elif not _runtime_version_at_least(
            runtime.torch_cuda_version,
            pack.minimum_cuda_version,
        ):
            code = "detector_cuda_version_unsupported"
        else:
            return DetectorTrainerStatus(
                state="ready",
                code="ready",
                message="local CUDA detector trainer is ready",
                trainer_id=self.trainer_id,
                trainer_version=version,
                model_pack_identity=pack.identity,
                runtime=public_runtime,
            )
        return DetectorTrainerStatus(
            state="failed",
            code=code,
            message=(
                "the configured local detector runtime does not satisfy "
                "the CUDA/VRAM contract"
            ),
            trainer_id=self.trainer_id,
            trainer_version=version,
            model_pack_identity=pack.identity,
            runtime=public_runtime,
        )

    def status(self) -> dict[str, Any]:
        return self._status.public_value()

    def _require_ready(self) -> tuple[
        LocalDetectorModelPack,
        DetectorRuntimeInfo,
    ]:
        if (
            not self.available
            or self._pack is None
            or self._runtime is None
        ):
            raise ProjectModelTrainerUnavailable(
                f"{self._status.code}: {self._status.message}"
            )
        return self._pack, self._runtime

    @staticmethod
    def _verify_image_magic(body: bytes, media_type: str) -> bool:
        if media_type == "image/jpeg":
            return body.startswith(b"\xff\xd8\xff")
        if media_type == "image/png":
            return body.startswith(b"\x89PNG\r\n\x1a\n")
        if media_type == "image/webp":
            return (
                len(body) >= 12
                and body.startswith(b"RIFF")
                and body[8:12] == b"WEBP"
            )
        return False

    def _verified_sample_path(
        self,
        resolved: ResolvedDetectorArtifact | ResolvedDerivedDetectorArtifact,
    ) -> Path:
        path = Path(resolved.path)
        if (
            not path.is_absolute()
            or not path.is_file()
            or _is_link_or_reparse(path)
            or isinstance(resolved.size_bytes, bool)
            or not isinstance(resolved.size_bytes, int)
            or not 1 <= resolved.size_bytes <= MAX_IMAGE_BYTES
            or not isinstance(resolved.sha256, str)
            or _SHA256.fullmatch(resolved.sha256) is None
        ):
            raise ProjectModelLifecycleError(
                "training_evidence_integrity_failed",
                "the resolved detector image is unsafe",
            )
        try:
            before = path.stat()
            body = path.read_bytes()
            after = path.stat()
        except OSError as error:
            raise ProjectModelLifecycleError(
                "training_evidence_unavailable",
                "the resolved detector image could not be read",
            ) from error
        actual_hash = hashlib.sha256(body).hexdigest()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(body) != resolved.size_bytes
            or actual_hash != resolved.sha256
            or not self._verify_image_magic(body, resolved.media_type)
        ):
            raise ProjectModelLifecycleError(
                "training_evidence_integrity_failed",
                "the resolved detector image failed hash/magic checks",
            )
        return path.resolve(strict=True)

    def _resolve_derived_sample(
        self,
        *,
        invocation: TrainerInvocation,
        derived: DerivedDetectorGeometrySample,
    ) -> DetectorSample:
        try:
            project_id = UUID(invocation.project_id)
            analysis_job_id = UUID(derived.analysis_job_id)
            if derived.source_frame_kind == "representative":
                resolved = self._artifact_resolver(
                    project_id,
                    analysis_job_id,
                    derived.candidate_id,
                    derived.image_artifact_ref,
                )
            else:
                if self._derived_artifact_resolver is None:
                    raise RuntimeError(
                        "tracked-frame evidence resolver is unavailable"
                    )
                resolved = self._derived_artifact_resolver(
                    project_id,
                    analysis_job_id,
                    derived.candidate_id,
                    derived,
                )
        except Exception as error:
            raise ProjectModelLifecycleError(
                "training_evidence_unavailable",
                "a detector training image could not be resolved",
            ) from error
        if derived.source_frame_kind == "representative":
            if not isinstance(resolved, ResolvedDetectorArtifact):
                raise ProjectModelLifecycleError(
                    "training_evidence_boundary_violation",
                    (
                        "the detector evidence resolver returned an "
                        "unsupported handle"
                    ),
                )
            if (
                resolved.project_id != invocation.project_id
                or resolved.analysis_job_id != derived.analysis_job_id
                or resolved.candidate_id != derived.candidate_id
                or resolved.artifact_ref != derived.image_artifact_ref
                or resolved.sha256 != derived.image_sha256
            ):
                raise ProjectModelLifecycleError(
                    "training_evidence_boundary_violation",
                    (
                        "detector evidence crossed its "
                        "project/job/candidate binding"
                    ),
                )
        else:
            if not isinstance(resolved, ResolvedDerivedDetectorArtifact):
                raise ProjectModelLifecycleError(
                    "training_evidence_boundary_violation",
                    (
                        "the tracked-frame resolver returned an "
                        "unsupported handle"
                    ),
                )
            if (
                resolved.project_id != invocation.project_id
                or resolved.analysis_job_id != derived.analysis_job_id
                or resolved.candidate_id != derived.candidate_id
                or resolved.evidence_candidate_id
                != derived.evidence_candidate_id
                or resolved.artifact_ref != derived.image_artifact_ref
                or resolved.sha256 != derived.image_sha256
                or resolved.event_group_id != derived.event_group_id
                or resolved.frame_id != derived.frame_id
                or resolved.frame_sha256 != derived.frame_sha256
                or resolved.box_sha256 != derived.box_sha256
                or resolved.mask_sha256 != derived.mask_sha256
            ):
                raise ProjectModelLifecycleError(
                    "training_evidence_boundary_violation",
                    (
                        "tracked detector evidence crossed its immutable "
                        "event/frame binding"
                    ),
                )
        path = self._verified_sample_path(resolved)
        return DetectorSample(
            sample_id=derived.sample_id,
            annotation_id=derived.annotation_id,
            project_id=derived.project_id,
            analysis_job_id=derived.analysis_job_id,
            candidate_id=derived.candidate_id,
            split=derived.split,
            partition=derived.partition,
            issue_type=derived.issue_type,
            image_path=path,
            image_sha256=derived.image_sha256,
            image_size_bytes=resolved.size_bytes,
            media_type=resolved.media_type,
            artifact_ref=derived.image_artifact_ref,
            normalized_box=derived.normalized_box,
            geometry_sha256=derived.derived_geometry_sha256,
            group_hashes=tuple(
                sorted(
                    {
                        derived.image_sha256,
                        derived.leakage_group_sha256,
                    }
                )
            ),
            event_group_id=derived.event_group_id,
            source_frame_kind=derived.source_frame_kind,
            frame_id=derived.frame_id,
            frame_sha256=derived.frame_sha256,
            box_sha256=derived.box_sha256,
            mask_sha256=derived.mask_sha256,
            source_geometry_sha256=derived.source_geometry_sha256,
            leakage_group_sha256=derived.leakage_group_sha256,
            evidence_candidate_id=derived.evidence_candidate_id,
        )

    def _resolve_samples(
        self,
        *,
        invocation: TrainerInvocation,
        annotation: Mapping[str, Any],
        expected_split: str,
        required_types: set[str],
    ) -> tuple[DetectorSample, ...]:
        if (
            annotation.get("split") != expected_split
            or annotation.get("include_in_training") is not True
        ):
            raise ProjectModelLifecycleError(
                "detector_dataset_split_invalid",
                "a detector annotation is in an unexpected split",
        )
        geometry_value = annotation.get("training_geometry")
        if geometry_value is None:
            return ()
        try:
            geometry = canonical_training_geometry(geometry_value)
        except TrainingGeometryError as error:
            raise ProjectModelLifecycleError(
                "annotation_geometry_invalid",
                "detector annotation geometry is invalid",
            ) from error
        partition = annotation.get("partition")
        issue_type = annotation.get("selected_issue_type")
        if partition == "approved":
            if (
                issue_type not in required_types
                or geometry["source_kind"] == "negative_evidence"
                or geometry["normalized_box"] is None
            ):
                raise ProjectModelLifecycleError(
                    "annotation_geometry_invalid",
                    "positive detector annotation is inconsistent",
                )
        elif partition == "negative":
            if (
                issue_type is not None
                or geometry["source_kind"] != "negative_evidence"
                or geometry["normalized_box"] is not None
            ):
                raise ProjectModelLifecycleError(
                    "annotation_geometry_invalid",
                    "negative detector annotation is inconsistent",
                )
        else:
            raise ProjectModelLifecycleError(
                "annotation_geometry_invalid",
                "only approved boxes and negative evidence train the detector",
            )
        try:
            derived = derive_detector_geometry_samples(
                annotation,
                include_tracked_fit_frames=expected_split == "train",
            )
        except DerivedDetectorGeometryError as error:
            raise ProjectModelLifecycleError(
                "annotation_geometry_invalid",
                "detector derived geometry is invalid",
            ) from error
        return tuple(
            self._resolve_derived_sample(
                invocation=invocation,
                derived=sample,
            )
            for sample in derived
        )

    def _prepare_dataset(
        self,
        invocation: TrainerInvocation,
    ) -> tuple[
        tuple[DetectorSample, ...],
        tuple[DetectorSample, ...],
        dict[str, Any],
    ]:
        if (
            len(invocation.current_training_annotations)
            + len(invocation.fixed_validation_annotations)
            > MAX_TRAINING_IMAGES
        ):
            raise ProjectModelLifecycleError(
                "detector_dataset_too_large",
                "the local detector dataset exceeds its hard limit",
            )
        required = set(invocation.required_issue_types)
        train = tuple(
            sample
            for annotation in invocation.current_training_annotations
            for sample in self._resolve_samples(
                invocation=invocation,
                annotation=annotation,
                expected_split="train",
                required_types=required,
            )
        )
        validation_rows = []
        missing_validation_geometry = []
        for annotation in invocation.fixed_validation_annotations:
            samples = self._resolve_samples(
                invocation=invocation,
                annotation=annotation,
                expected_split="validation",
                required_types=required,
            )
            if not samples:
                missing_validation_geometry.append(
                    str(annotation["annotation_id"])
                )
            elif len(samples) != 1:
                raise ProjectModelLifecycleError(
                    "fixed_validation_geometry_invalid",
                    "fixed validation cannot contain derived tracked frames",
                )
            else:
                validation_rows.append(samples[0])
        validation = tuple(validation_rows)
        if len(train) + len(validation) > MAX_TRAINING_IMAGES:
            raise ProjectModelLifecycleError(
                "detector_dataset_too_large",
                "the expanded local detector dataset exceeds its hard limit",
            )
        if missing_validation_geometry:
            raise ProjectModelLifecycleError(
                "fixed_validation_geometry_incomplete",
                (
                    "classification-only fixed-validation labels cannot "
                    "validate an object detector"
                ),
            )
        unique_train_annotations = {
            item.annotation_id for item in train
        }
        if len(unique_train_annotations) < self._minimum_training_geometry:
            raise ProjectModelLifecycleError(
                "insufficient_detector_training_geometry",
                (
                    "detector training requires at least "
                    f"{self._minimum_training_geometry} current-video "
                    "unique box/negative Annotation members"
                ),
            )
        if len(validation) < self._minimum_validation_geometry:
            raise ProjectModelLifecycleError(
                "insufficient_detector_validation_geometry",
                (
                    "detector fixed validation requires at least "
                    f"{self._minimum_validation_geometry} geometry members "
                    "per evaluated type"
                ),
            )
        train_positive_types = {
            item.issue_type
            for item in train
            if item.partition == "approved"
        }
        validation_positive_types = {
            item.issue_type
            for item in validation
            if item.partition == "approved"
        }
        if not required.issubset(train_positive_types):
            raise ProjectModelLifecycleError(
                "insufficient_detector_training_positives",
                "every required issue type needs current-video box truth",
            )
        if not required.issubset(validation_positive_types):
            raise ProjectModelLifecycleError(
                "insufficient_detector_validation_positives",
                "every required issue type needs fixed-validation box truth",
            )
        train_ids = {item.sample_id for item in train}
        validation_ids = {item.sample_id for item in validation}
        truth_ids = {
            str(item["sample_id"])
            for item in invocation.fixed_validation_truth
        }
        if validation_ids != truth_ids:
            raise ProjectModelLifecycleError(
                "fixed_validation_evidence_mismatch",
                "detector geometry and fixed-validation truth differ",
            )
        train_analysis = {item.analysis_job_id for item in train}
        validation_analysis = {
            item.analysis_job_id for item in validation
        }
        train_groups = {
            digest for item in train for digest in item.group_hashes
        }
        validation_groups = {
            digest
            for item in validation
            for digest in item.group_hashes
        }
        if (
            train_ids.intersection(validation_ids)
            or train_analysis.intersection(validation_analysis)
            or train_groups.intersection(validation_groups)
        ):
            raise ProjectModelLifecycleError(
                "detector_split_leakage",
                "training and fixed validation share a source/group/image",
            )
        dataset_value = {
            "schema_version": DATASET_MANIFEST_SCHEMA,
            "project_id": invocation.project_id,
            "training_job_id": invocation.training_job_id,
            "dataset_version_id": invocation.dataset_version_id,
            "dataset_hash": invocation.dataset_hash,
            "fixed_validation_sha256": (
                invocation.fixed_validation_sha256
            ),
            "required_issue_types": sorted(required),
            "train": [
                {
                    "sample_id": item.sample_id,
                    "annotation_id": item.annotation_id,
                    "analysis_job_id": item.analysis_job_id,
                    "candidate_id": item.candidate_id,
                    "evidence_candidate_id": item.evidence_candidate_id,
                    "event_group_id": item.event_group_id,
                    "partition": item.partition,
                    "issue_type": item.issue_type,
                    "source_frame_kind": item.source_frame_kind,
                    "frame_id": item.frame_id,
                    "image_sha256": item.image_sha256,
                    "artifact_ref": item.artifact_ref,
                    "geometry_sha256": item.geometry_sha256,
                    "source_geometry_sha256": (
                        item.source_geometry_sha256
                    ),
                    "frame_sha256": item.frame_sha256,
                    "box_sha256": item.box_sha256,
                    "mask_sha256": item.mask_sha256,
                    "leakage_group_sha256": (
                        item.leakage_group_sha256
                    ),
                    "normalized_box": item.normalized_box,
                    "fit_only": True,
                    "counts_toward_training_gate": False,
                }
                for item in sorted(train, key=lambda value: value.sample_id)
            ],
            "validation": [
                {
                    "sample_id": item.sample_id,
                    "annotation_id": item.annotation_id,
                    "analysis_job_id": item.analysis_job_id,
                    "candidate_id": item.candidate_id,
                    "evidence_candidate_id": item.evidence_candidate_id,
                    "event_group_id": item.event_group_id,
                    "partition": item.partition,
                    "issue_type": item.issue_type,
                    "source_frame_kind": item.source_frame_kind,
                    "frame_id": item.frame_id,
                    "image_sha256": item.image_sha256,
                    "artifact_ref": item.artifact_ref,
                    "geometry_sha256": item.geometry_sha256,
                    "source_geometry_sha256": (
                        item.source_geometry_sha256
                    ),
                    "frame_sha256": item.frame_sha256,
                    "box_sha256": item.box_sha256,
                    "mask_sha256": item.mask_sha256,
                    "leakage_group_sha256": (
                        item.leakage_group_sha256
                    ),
                    "normalized_box": item.normalized_box,
                    "fit_only": True,
                    "counts_toward_training_gate": False,
                }
                for item in sorted(
                    validation,
                    key=lambda value: value.sample_id,
                )
            ],
            "classification_only_training_members_excluded": (
                len(invocation.current_training_annotations)
                - len(unique_train_annotations)
            ),
            "unique_training_annotation_count": len(
                unique_train_annotations
            ),
            "detector_fit_sample_count": len(train),
            "derived_tracked_fit_sample_count": sum(
                item.source_frame_kind == "sam2_tracked_frame"
                for item in train
            ),
            "derived_frames_count_toward_training_gate": False,
            "validation_tracked_fit_sample_count": 0,
            "auxiliary_reference_seed_count": len(
                invocation.auxiliary_reference_seeds
            ),
            "auxiliary_references_used_for_detector_fit": False,
            "validation_used_for_fit": False,
        }
        dataset_manifest_sha256 = hashlib.sha256(
            canonical_json_bytes(dataset_value)
        ).hexdigest()
        return train, validation, {
            **dataset_value,
            "dataset_manifest_sha256": dataset_manifest_sha256,
        }

    def _resolve_checkpoint(
        self,
        invocation: TrainerInvocation,
    ) -> Optional[VerifiedDetectorCheckpoint]:
        if (
            invocation.resume_checkpoint_path is None
        ) != (
            invocation.resume_checkpoint_sha256 is None
        ):
            raise ProjectModelLifecycleError(
                "detector_checkpoint_invalid",
                "the lifecycle checkpoint boundary is incomplete",
            )
        if invocation.resume_checkpoint_path is not None:
            value: Optional[VerifiedDetectorCheckpoint] = (
                VerifiedDetectorCheckpoint(
                    path=invocation.resume_checkpoint_path,
                    sha256=str(invocation.resume_checkpoint_sha256),
                    project_id=invocation.project_id,
                    training_job_id=invocation.training_job_id,
                    request_manifest_sha256=(
                        invocation.request_manifest_sha256
                    ),
                )
            )
        elif self._checkpoint_resolver is None:
            return None
        else:
            try:
                value = self._checkpoint_resolver(
                    UUID(invocation.project_id),
                    UUID(invocation.training_job_id),
                    invocation.request_manifest_sha256,
                )
            except Exception as error:
                raise ProjectModelLifecycleError(
                    "detector_checkpoint_unavailable",
                    "the detector checkpoint could not be resolved",
                ) from error
        if value is None:
            return None
        if (
            not isinstance(value, VerifiedDetectorCheckpoint)
            or value.project_id != invocation.project_id
            or value.training_job_id != invocation.training_job_id
            or value.request_manifest_sha256
            != invocation.request_manifest_sha256
            or not value.path.is_absolute()
            or not value.path.is_file()
            or _is_link_or_reparse(value.path)
            or _SHA256.fullmatch(value.sha256) is None
        ):
            raise ProjectModelLifecycleError(
                "detector_checkpoint_invalid",
                "the detector checkpoint boundary is invalid",
            )
        actual_hash, _size = _sha256_file(value.path)
        if actual_hash != value.sha256:
            raise ProjectModelLifecycleError(
                "detector_checkpoint_invalid",
                "the detector checkpoint hash changed",
            )
        return value

    def _evaluate_active_detector(
        self,
        invocation: TrainerInvocation,
        validation: Sequence[DetectorSample],
    ) -> tuple[tuple[DetectorBoxPrediction, ...], str]:
        if (
            self._active_model_resolver is None
            or self._active_model_evaluator is None
        ):
            return (), "no_active_detector_evaluator"
        project_id = UUID(invocation.project_id)
        try:
            active = self._active_model_resolver(project_id)
        except ProjectModelLifecycleError as error:
            if error.code == "active_project_model_artifact_unavailable":
                return (), "no_promoted_project_model"
            raise
        if (
            active.model_format != LOCAL_OBJECT_DETECTOR_MODEL_FORMAT
            or active.model_version != invocation.base_model_version
        ):
            return (), "no_compatible_promoted_detector"
        if active.fixed_validation_sha256 != (
            invocation.fixed_validation_sha256
        ):
            raise ProjectModelLifecycleError(
                "active_detector_validation_mismatch",
                (
                    "the promoted detector and candidate do not share the "
                    "same immutable fixed validation set"
                ),
            )
        values = self._active_model_evaluator.evaluate_fixed_validation(
            active,
            validation,
            fixed_validation_sha256=(
                invocation.fixed_validation_sha256
            ),
        )
        if (
            not isinstance(values, Sequence)
            or isinstance(values, (str, bytes, bytearray))
        ):
            raise ProjectModelLifecycleError(
                "active_detector_evaluation_invalid",
                "the promoted detector evaluator returned invalid predictions",
            )
        return tuple(values), "verified_promoted_detector"

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        pack, runtime = self._require_ready()
        progress_hook = (
            invocation.progress_callback or self._progress_hook
        )
        cancel_hook = (
            invocation.cancel_callback or self._cancel_hook
        )
        if cancel_hook():
            raise DetectorTrainingCancelled()
        train, validation, dataset_manifest = self._prepare_dataset(
            invocation
        )
        active_predictions, active_evaluation_mode = (
            self._evaluate_active_detector(
                invocation,
                validation,
            )
        )
        checkpoint = self._resolve_checkpoint(invocation)
        result = self._backend.train(
            DetectorBackendRequest(
                pack=pack,
                invocation=invocation,
                train_samples=train,
                validation_samples=validation,
                output_directory=invocation.output_directory,
                resume_checkpoint=checkpoint,
            ),
            progress=progress_hook,
            cancelled=cancel_hook,
        )
        if not isinstance(result, DetectorBackendResult):
            raise ProjectModelLifecycleError(
                "trainer_contract_violation",
                "the detector backend returned an unsupported result",
            )
        relative_weight = PurePosixPath(
            result.primary_weight_relative_path
        )
        if (
            relative_weight.is_absolute()
            or not relative_weight.parts
            or any(
                part in {"", ".", ".."}
                for part in relative_weight.parts
            )
        ):
            raise ProjectModelLifecycleError(
                "detector_artifact_invalid",
                "the detector backend weight path is unsafe",
            )
        weight = invocation.output_directory.joinpath(
            *relative_weight.parts
        )
        try:
            weight.resolve(strict=True).relative_to(
                invocation.output_directory.resolve(strict=True)
            )
        except (OSError, ValueError) as error:
            raise ProjectModelLifecycleError(
                "detector_artifact_invalid",
                "the detector backend weight escaped its output directory",
            ) from error
        if (
            not weight.is_file()
            or _is_link_or_reparse(weight)
            or weight.stat().st_size <= 0
        ):
            raise ProjectModelLifecycleError(
                "detector_artifact_missing",
                "the detector backend produced no non-empty weight artifact",
            )
        weight_hash, weight_size = _sha256_file(weight)
        validation_ids = {item.sample_id for item in validation}
        required_types = set(invocation.required_issue_types)
        backend_active_predictions = tuple(
            _canonical_prediction(
                item,
                validation_ids=validation_ids,
                required_types=required_types,
            )
            for item in result.active_predictions
        )
        if active_evaluation_mode == "verified_promoted_detector":
            active_predictions = tuple(
                _canonical_prediction(
                    item,
                    validation_ids=validation_ids,
                    required_types=required_types,
                )
                for item in active_predictions
            )
        else:
            # Production Transformers returns no active baseline.  Keep
            # deterministic test backends compatible while making the
            # provenance explicit.
            active_predictions = backend_active_predictions
            if active_predictions:
                active_evaluation_mode = "backend_supplied_test_baseline"
        candidate_predictions = tuple(
            _canonical_prediction(
                item,
                validation_ids=validation_ids,
                required_types=required_types,
            )
            for item in result.candidate_predictions
        )
        active_records = _event_predictions(
            prefix="active",
            predictions=active_predictions,
            validation=validation,
            required_types=invocation.required_issue_types,
            score_threshold=pack.score_threshold,
            iou_threshold=pack.iou_threshold,
        )
        candidate_records = _event_predictions(
            prefix="candidate",
            predictions=candidate_predictions,
            validation=validation,
            required_types=invocation.required_issue_types,
            score_threshold=pack.score_threshold,
            iou_threshold=pack.iou_threshold,
        )
        active_metrics = _box_metrics(
            active_predictions,
            validation,
            invocation.required_issue_types,
            score_threshold=pack.score_threshold,
            iou_threshold=pack.iou_threshold,
        )
        candidate_metrics = _box_metrics(
            candidate_predictions,
            validation,
            invocation.required_issue_types,
            score_threshold=pack.score_threshold,
            iou_threshold=pack.iou_threshold,
        )
        execution = dict(result.execution_provenance)
        if (
            execution.get("schema_version")
            != EXECUTION_PROVENANCE_SCHEMA
            or execution.get("accelerator") != "cuda"
            or execution.get("accelerator_used") is not True
            or not str(execution.get("resolved_device", "")).startswith(
                "cuda"
            )
            or execution.get("hub_upload") is not False
            or execution.get("network_model_download") is not False
            or execution.get("trust_remote_code") is not False
        ):
            raise ProjectModelLifecycleError(
                "training_runtime_provenance_invalid",
                "the detector backend did not prove offline CUDA execution",
            )
        if (
            runtime.cuda_device_name != execution.get("cuda_device_name")
            or runtime.cuda_total_memory_bytes
            != execution.get("cuda_total_memory_bytes")
            or runtime.torch_cuda_version
            != execution.get("torch_cuda_version")
        ):
            raise ProjectModelLifecycleError(
                "training_runtime_provenance_invalid",
                "the detector runtime identity changed during training",
            )
        artifact_value = {
            "schema_version": ADAPTER_ARTIFACT_SCHEMA,
            "project_id": invocation.project_id,
            "training_job_id": invocation.training_job_id,
            "request_manifest_sha256": (
                invocation.request_manifest_sha256
            ),
            "dataset_version_id": invocation.dataset_version_id,
            "dataset_hash": invocation.dataset_hash,
            "fixed_validation_sha256": (
                invocation.fixed_validation_sha256
            ),
            "model_pack_identity": pack.identity,
            "dataset_manifest": dataset_manifest,
            "primary_weight": {
                "path": relative_weight.as_posix(),
                "size_bytes": weight_size,
                "sha256": weight_hash,
            },
            "execution_provenance": execution,
            "active_box_metrics": active_metrics,
            "candidate_box_metrics": candidate_metrics,
            "active_evaluation_mode": active_evaluation_mode,
            "active_incident_prediction_count": len(active_records),
            "candidate_incident_prediction_count": len(candidate_records),
            "resumed_from_checkpoint_sha256": (
                result.resumed_from_checkpoint_sha256
            ),
            "validation_used_for_fit": False,
            "approved_sample_references_used_for_fit": False,
            "automatic_promotion": False,
        }
        artifact_path = (
            invocation.output_directory / "detector-adapter.json"
        )
        artifact_path.write_bytes(canonical_json_bytes(artifact_value))
        return TrainerResult(
            model_version=(
                "project-detector@"
                f"{invocation.request_manifest_sha256[:12]}"
            ),
            model_format=LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
            primary_artifact_relative_path="detector-adapter.json",
            active_prediction_records=active_records,
            candidate_prediction_records=candidate_records,
            metadata={
                "trainer_version": self.trainer_version,
                "backend_id": self._backend.backend_id,
                "backend_version": self._backend.backend_version,
                "model_pack_identity": pack.identity,
                "execution_provenance": execution,
                "dataset_manifest_sha256": dataset_manifest[
                    "dataset_manifest_sha256"
                ],
                "detector_training_geometry_count": len(
                    {
                        item.annotation_id
                        for item in train
                    }
                ),
                "detector_fit_sample_count": len(train),
                "derived_tracked_fit_sample_count": sum(
                    item.source_frame_kind == "sam2_tracked_frame"
                    for item in train
                ),
                "derived_frames_count_toward_training_gate": False,
                "classification_only_training_member_count": (
                    len(invocation.current_training_annotations)
                    - len(
                        {
                            item.annotation_id
                            for item in train
                        }
                    )
                ),
                "fixed_validation_geometry_count": len(validation),
                "auxiliary_reference_seed_count": len(
                    invocation.auxiliary_reference_seeds
                ),
                "auxiliary_references_used_for_detector_fit": False,
                "active_box_metrics": active_metrics,
                "candidate_box_metrics": candidate_metrics,
                "active_evaluation_mode": active_evaluation_mode,
                "primary_weight_sha256": weight_hash,
            },
        )


__all__ = [
    "ActiveDetectorEvaluationProvider",
    "ActiveDetectorModelResolver",
    "ADAPTER_ARTIFACT_SCHEMA",
    "DATASET_MANIFEST_SCHEMA",
    "DetectorArtifactResolver",
    "DerivedDetectorArtifactResolver",
    "DetectorBackendRequest",
    "DetectorBackendResult",
    "DetectorBoxPrediction",
    "DetectorModelPackError",
    "DetectorPackArtifact",
    "DetectorRuntimeInfo",
    "DetectorSample",
    "DetectorTrainerStatus",
    "DetectorTrainingBackend",
    "DetectorTrainingCancelled",
    "EXECUTION_PROVENANCE_SCHEMA",
    "LocalCudaObjectDetectorTrainer",
    "LOCAL_OBJECT_DETECTOR_MODEL_FORMAT",
    "LocalDetectorModelPack",
    "MODEL_PACK_SCHEMA_VERSION",
    "ResolvedDetectorArtifact",
    "ResolvedDerivedDetectorArtifact",
    "TransformersCudaDetectorBackend",
    "VerifiedDetectorCheckpoint",
    "load_local_detector_model_pack",
    "normalized_xywh_to_coco",
]
