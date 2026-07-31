"""Durable, project-scoped training and model-promotion lifecycle.

This module owns the explicit approval boundary between an immutable
``DatasetVersion`` and a model that may be used for a later road survey.
It deliberately does not expose an automatic promotion path:

* only current-video confirmed training Annotation members satisfy the
  minimum-label gate;
* approved references remain lower-weight auxiliary seeds; multiple photos
  from one historical event share one total seed weight, and never count as
  current-video labels, gate credit, or fixed-validation samples;
* a trainer must create a real, non-empty artifact and predictions over the
  exact fixed validation set before a durable candidate is recorded;
* promotion, rejection, and rollback are explicit user-confirmed transitions.

The production default is unavailable.  A production adapter must implement
``ProjectModelTrainer`` and produce the artifact contract described by
``MODEL_ARTIFACT_SCHEMA_VERSION``.  ``DeterministicTestTrainer`` is guarded by
``allow_test_trainer=True`` so it cannot silently become a production model.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)
from uuid import UUID, uuid5

from .dataset_versions import (
    DATASET_SCHEMA_VERSION,
    DatasetVersionError,
    canonical_approved_reference_member_value,
    canonical_json_bytes,
)
from .training_geometry import (
    TrainingGeometryError,
    canonical_training_geometry,
)
from .structured_assist_provenance import (
    StructuredAssistProvenanceError,
    canonical_structured_assist_provenance,
)
from .training_provenance import (
    TrainingProvenanceError,
    canonical_annotation_provenance_graph,
    canonical_dataset_provenance_graph,
)
from .validation_metrics import (
    CANONICAL_ISSUE_TYPES,
    DEFAULT_MAX_RECALL_DROP,
    DEFAULT_MINIMUM_CANDIDATE_PRECISION,
    DEFAULT_MINIMUM_CANDIDATE_RECALL,
    DEFAULT_MINIMUM_SAMPLES_PER_TYPE,
    ValidationMetricsError,
    compare_candidate_to_active,
    evaluate_model,
)


TRAINING_JOB_SCHEMA_VERSION = "trafficvisson.training-job.v1"
MODEL_VERSION_SCHEMA_VERSION = "trafficvisson.project-model-version.v1"
MODEL_STATE_SCHEMA_VERSION = "trafficvisson.project-model-state.v1"
MODEL_EVENT_SCHEMA_VERSION = "trafficvisson.project-model-event.v1"
TRAINER_CONTRACT_VERSION = "trafficvisson.project-model-trainer.v1"
TRAINING_CHECKPOINT_SCHEMA_VERSION = (
    "trafficvisson.project-model-training-checkpoint.v1"
)
MODEL_ARTIFACT_SCHEMA_VERSION = (
    "trafficvisson.project-model-artifact-manifest.v1"
)
MODEL_ARTIFACT_LOCATOR_SCHEMA_VERSION = (
    "trafficvisson.project-model-artifact-locator.v1"
)
PROJECT_ARTIFACT_JOB_BINDING_SCHEMA = (
    "trafficvisson.project-artifact-root-job-binding.v1"
)
PROJECT_MODEL_ARTIFACT_CATEGORY = "project-models"
VISUAL_CENTROID_MODEL_FORMAT = (
    "trafficvisson.visual-embedding-centroid-head.v1"
)
LOCAL_OBJECT_DETECTOR_MODEL_FORMAT = (
    "trafficvisson.local-object-detector.v1"
)
DETECTOR_MODEL_PACK_SCHEMA_VERSION = (
    "trafficvisson.local-object-detector-training-pack.v1"
)
FIXED_VALIDATION_SET_SCHEMA_VERSION = (
    "trafficvisson.project-fixed-validation.v2"
)
FIXED_VALIDATION_EXTENSION_APPROVAL_SCHEMA_VERSION = (
    "trafficvisson.fixed-validation-extension-approval.v1"
)
LEGACY_FIXED_VALIDATION_SET_SCHEMA_VERSION = (
    "trafficvisson.project-fixed-validation.v1"
)

MINIMUM_CURRENT_CONFIRMED_LABELS = 100
DEFAULT_AUXILIARY_REFERENCE_WEIGHT = 0.25
MAXIMUM_AUXILIARY_REFERENCE_WEIGHT = 0.5
MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_FILES = 128
MAX_ARTIFACT_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARTIFACT_TOTAL_BYTES = 4 * 1024 * 1024 * 1024

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_MODEL_PACK_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,191}$"
)
_MODEL_PACK_VERSION = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
)
_MODEL_SOURCE_REVISION = re.compile(
    r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$"
)
_SPDX_IDENTIFIER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$"
)
_HTTPS_URL = re.compile(r"^https://[^\s]{1,1000}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,127}$")
_APPROVAL_ID = re.compile(r"^approved-reference-[a-f0-9]{24}$")
_APPROVED_REFERENCE_SCHEMA = "trafficvisson.approved-reference.v2"
_SAMPLE_LAYOUT_BINDING_SCHEMA = (
    "trafficvisson.hwpx-sample-layout.v2"
)
_SAMPLE_LAYOUT_BINDING_REVISION = 2
_SEMANTIC_PROJECTION_REVISION = (
    "audited-photo-caption-evidence-group-v2"
)
_SAMPLE_SOURCE_ID = re.compile(
    r"^sample-hwpx-[34]q-[a-f0-9]{16}$"
)
_SAMPLE_CASE_ID = re.compile(
    r"^hwpx-weak-case-[a-f0-9]{32}$"
)
_PHOTO_SLOT_ID = re.compile(
    r"^sample-photo-slot-[a-f0-9]{24}$"
)
_SAMPLE_EVIDENCE_GROUP_ID = re.compile(
    r"^sample-evidence-group-[a-f0-9]{24}$"
)
_DUPLICATE_IMAGE_CASE_REF = re.compile(
    r"^sample-hwpx-[34]q-[a-f0-9]{16}/"
    r"hwpx-weak-case-[a-f0-9]{32}$"
)
_WINDOWS_REPARSE_ATTRIBUTE = 0x400


class ProjectModelLifecycleError(RuntimeError):
    """Fail-closed lifecycle error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProjectModelTrainerUnavailable(ProjectModelLifecycleError):
    def __init__(self, message: str = "project model trainer is unavailable") -> None:
        super().__init__("trainer_unavailable", message)


@dataclass(frozen=True)
class TrainerInvocation:
    """Server-owned, immutable input passed to a project trainer adapter."""

    schema_version: str
    project_id: str
    training_job_id: str
    request_manifest_sha256: str
    dataset_version_id: str
    dataset_hash: str
    base_model_version: str
    required_issue_types: Tuple[str, ...]
    current_training_annotations: Tuple[Dict[str, Any], ...]
    fixed_validation_annotations: Tuple[Dict[str, Any], ...]
    auxiliary_reference_seeds: Tuple[Dict[str, Any], ...]
    fixed_validation_truth: Tuple[Dict[str, Any], ...]
    fixed_validation_sha256: str
    fixed_validation_membership_sha256: str
    output_directory: Path
    fixed_validation_set_version: int = 1
    progress_callback: Optional[
        Callable[[Mapping[str, Any]], None]
    ] = field(default=None, repr=False, compare=False)
    cancel_callback: Optional[Callable[[], bool]] = field(
        default=None,
        repr=False,
        compare=False,
    )
    checkpoint_callback: Optional[Callable[[Path], None]] = field(
        default=None,
        repr=False,
        compare=False,
    )
    resume_checkpoint_path: Optional[Path] = field(
        default=None,
        repr=False,
        compare=False,
    )
    resume_checkpoint_sha256: Optional[str] = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class TrainerResult:
    """A trainer result is only provisional until artifact verification."""

    model_version: str
    model_format: str
    primary_artifact_relative_path: str
    active_prediction_records: Tuple[Dict[str, Any], ...]
    candidate_prediction_records: Tuple[Dict[str, Any], ...]
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class ResolvedActiveProjectModel:
    """Verified internal handle for later PipelineJob inference.

    Absolute paths in this object are bridge-internal and must not be forwarded
    to the Electron renderer.
    """

    project_id: str
    state_revision: int
    model_version_id: str
    model_version: str
    model_format: str
    dataset_version_id: str
    dataset_hash: str
    fixed_validation_sha256: str
    primary_artifact_path: Path
    primary_artifact_sha256: str
    artifact_manifest_path: Path
    artifact_manifest_sha256: str
    training_analysis_job_ids: Tuple[str, ...] = ()
    fixed_validation_analysis_job_ids: Tuple[str, ...] = ()
    source_analysis_lineage_sha256: str = ""
    dataset_provenance_graph_sha256: str = ""
    output_location_id: str = "project-default"
    root_identity_sha256: str = ""
    fixed_validation_set_version: int = 1
    model_pack_identity_sha256: str = ""
    training_runtime_provenance_sha256: str = ""


@dataclass(frozen=True)
class ResolvedModelArtifactRoot:
    """Server-only model category-root handle.

    The path-free registry ``binding`` is persisted and re-used to rebind the
    exact root after restart.  ``root`` is never serialized to the renderer.
    """

    root: Path
    binding: Mapping[str, Any]


class ProjectModelTrainer(Protocol):
    """Injected trainer/evaluator contract.

    A production implementation may train a visual-embedding linear or
    centroid head, or a dedicated detector.  It must write its real artifact
    inside ``invocation.output_directory`` and evaluate both the active and
    candidate model on ``invocation.fixed_validation_truth``.
    """

    trainer_id: str
    trainer_version: str
    available: bool
    test_only: bool

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        ...


class VisualEmbeddingCentroidHeadTrainerAdapter:
    """Production adapter slot for a project visual-embedding centroid head.

    This class is intentionally unavailable until the real embedding extractor,
    feature cache, evaluator, and license-pinned Model Pack are supplied.  A
    compliant implementation writes a non-empty centroid/linear-head artifact,
    declares ``VISUAL_CENTROID_MODEL_FORMAT``, and returns fixed-set active and
    candidate predictions.  It must never report success from metrics alone.
    """

    trainer_id = "visual-embedding-centroid-head"
    trainer_version = "unconfigured"
    available = False
    test_only = False

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        del invocation
        raise ProjectModelTrainerUnavailable(
            "visual-embedding centroid trainer Model Pack is not configured"
        )


class DeterministicTestTrainer:
    """Test-only adapter that creates a small, hash-verifiable artifact."""

    trainer_id = "deterministic-centroid-test"
    trainer_version = "1.0"
    available = True
    test_only = True

    def __init__(
        self,
        *,
        candidate_mode: str = "perfect",
        model_version_prefix: str = "test-centroid",
    ) -> None:
        if candidate_mode not in {"perfect", "empty"}:
            raise ValueError("candidate_mode must be perfect or empty")
        self.candidate_mode = candidate_mode
        self.model_version_prefix = model_version_prefix
        self.calls = 0

    @staticmethod
    def _perfect_predictions(
        truth: Sequence[Mapping[str, Any]],
        *,
        prefix: str,
    ) -> Tuple[Dict[str, Any], ...]:
        values = []
        for record in truth:
            for index, truth_event_id in enumerate(record["truth_event_ids"]):
                values.append(
                    {
                        "prediction_event_id": (
                            f"{prefix}-{record['sample_id'][:24]}-{index}"
                        ),
                        "sample_id": record["sample_id"],
                        "issue_type": record["issue_type"],
                        "matched_truth_event_id": truth_event_id,
                    }
                )
        return tuple(values)

    def train(self, invocation: TrainerInvocation) -> TrainerResult:
        self.calls += 1
        model_version = (
            f"{self.model_version_prefix}@"
            f"{invocation.request_manifest_sha256[:12]}"
        )
        payload = {
            "schema_version": VISUAL_CENTROID_MODEL_FORMAT,
            "project_id": invocation.project_id,
            "dataset_hash": invocation.dataset_hash,
            "fixed_validation_sha256": invocation.fixed_validation_sha256,
            "current_training_annotation_hashes": [
                item["annotation_hash"]
                for item in invocation.current_training_annotations
            ],
            "auxiliary_reference_hashes": [
                item["record_sha256"]
                for item in invocation.auxiliary_reference_seeds
            ],
            "test_only": True,
        }
        artifact = invocation.output_directory / "centroid-head.json"
        artifact.write_bytes(canonical_json_bytes(payload))
        active = self._perfect_predictions(
            invocation.fixed_validation_truth,
            prefix="active",
        )
        candidate = (
            active
            if self.candidate_mode == "perfect"
            else ()
        )
        if candidate is active:
            candidate = tuple(
                {
                    **record,
                    "prediction_event_id": record[
                        "prediction_event_id"
                    ].replace("active-", "candidate-", 1),
                }
                for record in active
            )
        return TrainerResult(
            model_version=model_version,
            model_format=VISUAL_CENTROID_MODEL_FORMAT,
            primary_artifact_relative_path="centroid-head.json",
            active_prediction_records=active,
            candidate_prediction_records=candidate,
            metadata={
                "adapter_role": "deterministic_test_only",
                "candidate_mode": self.candidate_mode,
            },
        )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _canonical_detector_model_pack_identity(
    value: object,
) -> Dict[str, Any]:
    """Verify the immutable detector pack, source, and license identity.

    The trainer verifies files before CUDA execution.  The lifecycle repeats
    the bounded identity validation so an arbitrary production trainer cannot
    create a promotable detector by reporting only ``accelerator=cuda``.
    """

    expected = {
        "schema_version",
        "pack_id",
        "pack_version",
        "architecture",
        "model_id",
        "model_version",
        "source_repository",
        "source_revision",
        "weights_spdx",
        "runtime_spdx",
        "commercial_use_confirmed",
        "redistribution_allowed",
        "manifest_sha256",
        "artifact_count",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ProjectModelLifecycleError(
            "model_pack_provenance_invalid",
            "production detector Model Pack identity is missing or invalid",
        )
    identity = dict(value)
    identifier_fields = (
        "pack_id",
        "architecture",
        "model_id",
    )
    version_fields = ("pack_version", "model_version")
    if (
        identity.get("schema_version")
        != DETECTOR_MODEL_PACK_SCHEMA_VERSION
        or any(
            not isinstance(identity.get(field), str)
            or _MODEL_PACK_IDENTIFIER.fullmatch(
                identity[field]
            )
            is None
            for field in identifier_fields
        )
        or any(
            not isinstance(identity.get(field), str)
            or _MODEL_PACK_VERSION.fullmatch(identity[field]) is None
            for field in version_fields
        )
        or not isinstance(identity.get("source_repository"), str)
        or _HTTPS_URL.fullmatch(identity["source_repository"]) is None
        or not identity["source_repository"].startswith(
            "https://huggingface.co/"
        )
        or not isinstance(identity.get("source_revision"), str)
        or _MODEL_SOURCE_REVISION.fullmatch(
            identity["source_revision"]
        )
        is None
        or any(
            not isinstance(identity.get(field), str)
            or _SPDX_IDENTIFIER.fullmatch(identity[field]) is None
            for field in ("weights_spdx", "runtime_spdx")
        )
        or identity.get("commercial_use_confirmed") is not True
        or not isinstance(identity.get("redistribution_allowed"), bool)
        or not isinstance(identity.get("manifest_sha256"), str)
        or _SHA256.fullmatch(identity["manifest_sha256"]) is None
        or isinstance(identity.get("artifact_count"), bool)
        or not isinstance(identity.get("artifact_count"), int)
        or not 1 <= identity["artifact_count"] <= 512
    ):
        raise ProjectModelLifecycleError(
            "model_pack_provenance_invalid",
            (
                "production detector Model Pack hash, source revision, "
                "or license provenance is invalid"
            ),
        )
    return json.loads(
        canonical_json_bytes(identity).decode("utf-8")
    )


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, (str, UUID)):
        raise ProjectModelLifecycleError(
            "invalid_identifier",
            f"{label} must be a canonical UUID",
        )
    text = str(value)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise ProjectModelLifecycleError(
            "invalid_identifier",
            f"{label} must be a canonical UUID",
        ) from error
    if str(parsed) != text:
        raise ProjectModelLifecycleError(
            "invalid_identifier",
            f"{label} must be a canonical UUID",
        )
    return text


def _canonical_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ProjectModelLifecycleError(
            "invalid_identifier",
            f"{label} is not a canonical identifier",
        )
    return value


def _canonical_idempotency_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or _IDEMPOTENCY_KEY.fullmatch(value) is None
    ):
        raise ProjectModelLifecycleError(
            "invalid_idempotency_key",
            "idempotency_key must be 8-128 safe identifier characters",
        )
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProjectModelLifecycleError(
            "invalid_hash",
            f"{label} must be a lowercase SHA-256 value",
        )
    return value


def _is_link_or_reparse(path: Path) -> bool:
    value = os.lstat(str(path))
    return stat.S_ISLNK(value.st_mode) or bool(
        int(getattr(value, "st_file_attributes", 0))
        & _WINDOWS_REPARSE_ATTRIBUTE
    )


def _canonical_training_progress(
    value: Mapping[str, Any],
) -> Dict[str, Any]:
    """Bound a trainer progress event before it enters the durable ledger."""

    if not isinstance(value, Mapping):
        raise ProjectModelLifecycleError(
            "trainer_progress_invalid",
            "trainer progress must be an object",
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
            "trainer_progress_invalid",
            "trainer progress contains unsupported fields",
        )
    stage = value.get("stage")
    if (
        not isinstance(stage, str)
        or not stage
        or len(stage) > 64
        or _IDENTIFIER.fullmatch(stage) is None
    ):
        raise ProjectModelLifecycleError(
            "trainer_progress_invalid",
            "trainer progress stage is invalid",
        )
    result: Dict[str, Any] = {"stage": stage}
    for name in ("epoch", "epochs", "current", "total"):
        item = value.get(name)
        if item is None:
            continue
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            or item > 10_000_000
        ):
            raise ProjectModelLifecycleError(
                "trainer_progress_invalid",
                "trainer progress counter is invalid",
            )
        result[name] = item
    if (
        "current" in result
        and "total" in result
        and result["current"] > result["total"]
    ) or (
        "epoch" in result
        and "epochs" in result
        and result["epoch"] > result["epochs"]
    ):
        raise ProjectModelLifecycleError(
            "trainer_progress_invalid",
            "trainer progress exceeds its declared total",
        )
    loss = value.get("loss")
    if loss is not None:
        if (
            isinstance(loss, bool)
            or not isinstance(loss, (int, float))
            or not math.isfinite(float(loss))
        ):
            raise ProjectModelLifecycleError(
                "trainer_progress_invalid",
                "trainer progress loss is invalid",
            )
        result["loss"] = round(float(loss), 8)
    return result


def _hash_checkpoint_file(path: Path) -> Tuple[str, int]:
    try:
        before = path.stat()
        if (
            not path.is_file()
            or _is_link_or_reparse(path)
            or before.st_size <= 0
            or before.st_size > MAX_ARTIFACT_FILE_BYTES
        ):
            raise OSError("checkpoint is not a bounded regular file")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        after = path.stat()
    except OSError as error:
        raise ProjectModelLifecycleError(
            "training_checkpoint_invalid",
            "training checkpoint could not be verified",
        ) from error
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ProjectModelLifecycleError(
            "training_checkpoint_invalid",
            "training checkpoint changed while it was verified",
        )
    return digest.hexdigest(), after.st_size


def _ensure_private_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("model lifecycle data_root must be absolute")
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    if resolved != path:
        raise ValueError("model lifecycle data_root cannot be a link")
    for component in (resolved, *resolved.parents):
        if str(component) == component.anchor:
            continue
        if _is_link_or_reparse(component):
            raise ValueError(
                "model lifecycle data_root cannot contain links"
            )
    return resolved


def _normalize_model_artifact_root_binding(
    project_id: str,
    binding: Mapping[str, Any],
) -> Dict[str, Any]:
    expected_keys = {
        "schema_version",
        "project_id",
        "profile_revision",
        "output_location_id",
        "registry_revision",
        "root_identity_sha256",
        "marker_sha256",
        "configured_free_bytes",
        "configured_at",
        "job_start_free_bytes",
        "job_start_at",
        "path_exposed",
    }
    if not isinstance(binding, Mapping) or set(binding) != expected_keys:
        raise ProjectModelLifecycleError(
            "artifact_root_binding_invalid",
            "project model artifact-root binding is invalid",
        )
    normalized = dict(binding)
    if (
        normalized.get("schema_version")
        != PROJECT_ARTIFACT_JOB_BINDING_SCHEMA
        or normalized.get("project_id") != project_id
        or normalized.get("path_exposed") is not False
        or not isinstance(normalized.get("profile_revision"), int)
        or isinstance(normalized.get("profile_revision"), bool)
        or normalized["profile_revision"] < 1
        or not isinstance(normalized.get("registry_revision"), int)
        or isinstance(normalized.get("registry_revision"), bool)
        or normalized["registry_revision"] < 1
        or not isinstance(normalized.get("output_location_id"), str)
        or re.fullmatch(
            r"(?:project-default|project-output-[a-f0-9]{32})",
            normalized["output_location_id"],
        )
        is None
        or not isinstance(normalized.get("root_identity_sha256"), str)
        or _SHA256.fullmatch(normalized["root_identity_sha256"]) is None
        or not isinstance(normalized.get("marker_sha256"), str)
        or _SHA256.fullmatch(normalized["marker_sha256"]) is None
    ):
        raise ProjectModelLifecycleError(
            "artifact_root_binding_invalid",
            "project model artifact-root binding is invalid",
        )
    for key in ("configured_free_bytes", "job_start_free_bytes"):
        value = normalized.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProjectModelLifecycleError(
                "artifact_root_binding_invalid",
                "project model artifact-root binding is invalid",
            )
    for key in ("configured_at", "job_start_at"):
        value = normalized.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise ProjectModelLifecycleError(
                "artifact_root_binding_invalid",
                "project model artifact-root binding is invalid",
            )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ProjectModelLifecycleError(
                "artifact_root_binding_invalid",
                "project model artifact-root binding is invalid",
            ) from error
        if parsed.tzinfo is None:
            raise ProjectModelLifecycleError(
                "artifact_root_binding_invalid",
                "project model artifact-root binding is invalid",
            )
    try:
        encoded = canonical_json_bytes(normalized)
    except (TypeError, ValueError) as error:
        raise ProjectModelLifecycleError(
            "artifact_root_binding_invalid",
            "project model artifact-root binding is invalid",
        ) from error
    if len(encoded) > 64 * 1024:
        raise ProjectModelLifecycleError(
            "artifact_root_binding_invalid",
            "project model artifact-root binding is too large",
        )
    return normalized


def _validated_model_category_root(
    path: Path,
    *,
    training_job_id: str,
) -> Path:
    root = Path(path)
    if not root.is_absolute():
        raise ProjectModelLifecycleError(
            "artifact_root_unavailable",
            "project model artifact category root is not absolute",
        )
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise ProjectModelLifecycleError(
            "artifact_root_unavailable",
            "project model artifact category root is unavailable",
        ) from error
    if (
        root.is_symlink()
        or _is_link_or_reparse(root)
        or not resolved.is_dir()
        or resolved.name != training_job_id
        or resolved.parent.name != PROJECT_MODEL_ARTIFACT_CATEGORY
    ):
        raise ProjectModelLifecycleError(
            "artifact_root_unsafe",
            "project model artifact category root is unsafe",
        )
    current = resolved
    for _ in range(128):
        if _is_link_or_reparse(current):
            raise ProjectModelLifecycleError(
                "artifact_root_unsafe",
                "project model artifact root contains a link or reparse point",
            )
        parent = current.parent
        if parent == current:
            break
        current = parent
    else:
        raise ProjectModelLifecycleError(
            "artifact_root_unsafe",
            "project model artifact category root is too deep",
        )
    return resolved


def _model_category_identity(
    root: Path,
    *,
    project_id: str,
    training_job_id: str,
    dataset_version_id: str,
    binding: Mapping[str, Any],
) -> str:
    return _canonical_sha256(
        {
            "category": PROJECT_MODEL_ARTIFACT_CATEGORY,
            "dataset_version_id": dataset_version_id,
            "normalized_root": os.path.normcase(str(root)),
            "project_id": project_id,
            "root_identity_sha256": binding[
                "root_identity_sha256"
            ],
            "training_job_id": training_job_id,
        }
    )


def _canonical_required_types(values: Iterable[object]) -> Tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        raise ProjectModelLifecycleError(
            "invalid_required_issue_types",
            "required_issue_types must be a non-empty sequence",
        )
    observed = set()
    for value in values:
        if not isinstance(value, str) or value not in CANONICAL_ISSUE_TYPES:
            raise ProjectModelLifecycleError(
                "invalid_required_issue_types",
                "required_issue_types contains an unsupported issue type",
            )
        observed.add(value)
    if not observed:
        raise ProjectModelLifecycleError(
            "invalid_required_issue_types",
            "at least one required issue type is required",
        )
    return tuple(
        issue for issue in CANONICAL_ISSUE_TYPES if issue in observed
    )


def _fixed_validation_extension_approval(
    value: Optional[Mapping[str, Any]],
    *,
    parent_version: int,
    parent_membership_sha256: str,
    parent_fixed_validation_sha256: str,
    added_issue_types: Sequence[str],
) -> Dict[str, Any]:
    """Validate a separate, stale-safe user approval for set expansion."""

    if value is None:
        raise ProjectModelLifecycleError(
            "fixed_validation_extension_approval_required",
            (
                "new issue types require an explicit fixed-validation "
                "set extension approval"
            ),
        )
    required_fields = {
        "schema_version",
        "expected_parent_version",
        "expected_parent_membership_sha256",
        "expected_parent_fixed_validation_sha256",
        "added_issue_types",
        "added_validation_analysis_job_ids",
        "user_confirmed",
    }
    if not isinstance(value, Mapping) or set(value) != required_fields:
        raise ProjectModelLifecycleError(
            "fixed_validation_extension_approval_invalid",
            "fixed-validation extension approval is malformed",
        )
    if (
        value.get("schema_version")
        != FIXED_VALIDATION_EXTENSION_APPROVAL_SCHEMA_VERSION
        or value.get("user_confirmed") is not True
    ):
        raise ProjectModelLifecycleError(
            "fixed_validation_extension_approval_invalid",
            "fixed-validation extension must be explicitly user confirmed",
        )
    raw_parent_version = value.get("expected_parent_version")
    if (
        isinstance(raw_parent_version, bool)
        or not isinstance(raw_parent_version, int)
        or raw_parent_version < 1
    ):
        raise ProjectModelLifecycleError(
            "fixed_validation_extension_approval_invalid",
            "expected fixed-validation parent version is invalid",
        )
    raw_added = value.get("added_issue_types")
    try:
        canonical_added = _canonical_required_types(raw_added)
    except ProjectModelLifecycleError as error:
        raise ProjectModelLifecycleError(
            "fixed_validation_extension_approval_invalid",
            "fixed-validation extension issue types are invalid",
        ) from error
    expected_added = tuple(added_issue_types)
    raw_job_ids = value.get("added_validation_analysis_job_ids")
    if (
        isinstance(raw_job_ids, (str, bytes, bytearray, Mapping))
        or not isinstance(raw_job_ids, Sequence)
        or not raw_job_ids
    ):
        raise ProjectModelLifecycleError(
            "fixed_validation_extension_approval_invalid",
            "fixed-validation extension analysis jobs are invalid",
        )
    canonical_job_ids = []
    for raw_job_id in raw_job_ids:
        canonical_job_ids.append(
            _canonical_uuid(
                raw_job_id,
                "added_validation_analysis_job_id",
            )
        )
    if canonical_job_ids != sorted(set(canonical_job_ids)):
        raise ProjectModelLifecycleError(
            "fixed_validation_extension_approval_invalid",
            (
                "fixed-validation extension analysis jobs must be "
                "sorted and unique"
            ),
        )
    if (
        raw_parent_version != parent_version
        or value.get("expected_parent_membership_sha256")
        != parent_membership_sha256
        or value.get("expected_parent_fixed_validation_sha256")
        != parent_fixed_validation_sha256
        or canonical_added != expected_added
        or list(raw_added) != list(canonical_added)
    ):
        raise ProjectModelLifecycleError(
            "fixed_validation_extension_approval_stale",
            (
                "fixed-validation extension approval does not match the "
                "current parent set and newly derived issue types"
            ),
        )
    return {
        "schema_version": FIXED_VALIDATION_EXTENSION_APPROVAL_SCHEMA_VERSION,
        "expected_parent_version": parent_version,
        "expected_parent_membership_sha256": parent_membership_sha256,
        "expected_parent_fixed_validation_sha256": (
            parent_fixed_validation_sha256
        ),
        "added_issue_types": list(canonical_added),
        "added_validation_analysis_job_ids": canonical_job_ids,
        "user_confirmed": True,
    }


def _strict_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectModelLifecycleError(
            "invalid_state_revision",
            "expected_revision must be a non-negative integer",
        )
    return value


def _record_hash(record: Mapping[str, Any], field: str) -> str:
    stored = _sha256(record.get(field), field)
    without_hash = dict(record)
    without_hash.pop(field, None)
    if _canonical_sha256(without_hash) != stored:
        raise ProjectModelLifecycleError(
            "record_integrity_failed",
            f"{field} does not match its record",
        )
    return stored


def _dataset_member_value(member: Mapping[str, Any]) -> Dict[str, Any]:
    required = (
        "analysis_job_id",
        "annotation_hash",
        "annotation_id",
        "answer_record_id",
        "answer_revision",
        "candidate_id",
        "include_in_training",
        "partition",
        "question_id",
        "source_record_hash",
        "split",
    )
    if any(field not in member for field in required):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "dataset membership is incomplete",
        )
    result = {field: member[field] for field in required}
    if (
        result["partition"] not in {"approved", "negative", "held", "excluded"}
        or result["split"] not in {"train", "validation", "excluded"}
        or not isinstance(result["include_in_training"], bool)
        or isinstance(result["answer_revision"], bool)
        or not isinstance(result["answer_revision"], int)
        or result["answer_revision"] < 1
    ):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "dataset membership state is invalid",
        )
    for field in ("annotation_hash", "source_record_hash"):
        _sha256(result[field], field)
    for field in (
        "annotation_id",
        "answer_record_id",
        "analysis_job_id",
        "question_id",
    ):
        _canonical_uuid(result[field], field)
    if (
        not isinstance(result["candidate_id"], str)
        or re.fullmatch(r"[a-f0-9]{20}", result["candidate_id"]) is None
    ):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "candidate_id is invalid",
        )
    return result


def _dataset_reference_member_value(
    member: Mapping[str, Any],
) -> Dict[str, Any]:
    try:
        value = canonical_approved_reference_member_value(member)
    except DatasetVersionError as error:
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            str(error),
        ) from error
    issue_type = value.get("issue_type")
    if issue_type not in CANONICAL_ISSUE_TYPES:
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion approved reference issue type is invalid",
        )
    return value


def _normalize_dataset(
    project_id: str,
    dataset_version: Mapping[str, Any],
) -> Dict[str, Any]:
    if not isinstance(dataset_version, Mapping):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "dataset_version must be an object",
        )
    dataset_project = _canonical_uuid(
        dataset_version.get("project_id"),
        "dataset.project_id",
    )
    if dataset_project != project_id:
        raise ProjectModelLifecycleError(
            "project_boundary_violation",
            "DatasetVersion belongs to another project",
        )
    dataset_id = _canonical_uuid(
        dataset_version.get("dataset_version_id"),
        "dataset_version_id",
    )
    dataset_hash = _sha256(
        dataset_version.get("dataset_hash"),
        "dataset_hash",
    )
    manifest = dataset_version.get("manifest")
    memberships = dataset_version.get("memberships")
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(memberships, Sequence)
        or isinstance(memberships, (str, bytes, bytearray))
    ):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion manifest or memberships are invalid",
        )
    if (
        manifest.get("schema_version") != DATASET_SCHEMA_VERSION
        or manifest.get("project_id") != project_id
        or manifest.get("dataset_id") != dataset_id
        or manifest.get("dataset_hash") != dataset_hash
    ):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion manifest identity does not match",
        )
    manifest_members = manifest.get("members")
    manifest_reference_members = manifest.get(
        "approved_reference_members"
    )
    response_reference_members = dataset_version.get(
        "approved_reference_members"
    )
    if (
        not isinstance(manifest_members, Sequence)
        or isinstance(manifest_members, (str, bytes, bytearray))
    ):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion manifest members are invalid",
        )
    if (
        not isinstance(manifest_reference_members, Sequence)
        or isinstance(
            manifest_reference_members,
            (str, bytes, bytearray),
        )
        or not isinstance(response_reference_members, Sequence)
        or isinstance(
            response_reference_members,
            (str, bytes, bytearray),
        )
    ):
        raise ProjectModelLifecycleError(
            "dataset_reference_snapshot_missing",
            "DatasetVersion approved references are not immutable",
        )
    normalized_memberships = tuple(
        _dataset_member_value(item) for item in memberships
    )
    normalized_manifest_members = tuple(
        _dataset_member_value(item) for item in manifest_members
    )
    normalized_reference_members = tuple(
        _dataset_reference_member_value(item)
        for item in manifest_reference_members
    )
    normalized_response_references = tuple(
        _dataset_reference_member_value(item)
        for item in response_reference_members
    )
    if normalized_memberships != normalized_manifest_members:
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion membership and manifest differ",
        )
    if normalized_reference_members != normalized_response_references:
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion approved references and manifest differ",
        )
    reference_members_sha256 = _canonical_sha256(
        list(normalized_reference_members)
    )
    if (
        manifest.get("approved_reference_members_sha256")
        != reference_members_sha256
        or manifest.get(
            "approved_references_count_toward_training_gate"
        )
        is not False
        or manifest.get(
            "approved_references_count_toward_validation"
        )
        is not False
        or dataset_version.get(
            "approved_reference_members_sha256"
        )
        != reference_members_sha256
        or dataset_version.get(
            "approved_reference_member_count"
        )
        != len(normalized_reference_members)
        or dataset_version.get(
            "approved_references_count_toward_training_gate"
        )
        is not False
        or dataset_version.get(
            "approved_references_count_toward_validation"
        )
        is not False
    ):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion approved reference provenance is invalid",
        )
    if len(
        {item["annotation_id"] for item in normalized_memberships}
    ) != len(normalized_memberships):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion contains duplicate annotations",
        )
    if (
        len(
            {
                item["approval_id"]
                for item in normalized_reference_members
            }
        )
        != len(normalized_reference_members)
        or len(
            {
                (item["source_id"], item["case_id"])
                for item in normalized_reference_members
            }
        )
        != len(normalized_reference_members)
        or len(
            {
                item["image_sha256"]
                for item in normalized_reference_members
            }
        )
        != len(normalized_reference_members)
    ):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion contains duplicate approved references",
        )
    group_contracts: Dict[str, Tuple[int, str, str]] = {}
    seen_group_photo_indexes = set()
    for item in normalized_reference_members:
        group_id = item["sample_evidence_group_id"]
        photo_identity = (group_id, item["group_photo_index"])
        if photo_identity in seen_group_photo_indexes:
            raise ProjectModelLifecycleError(
                "dataset_integrity_failed",
                "DatasetVersion contains duplicate group photo indexes",
            )
        seen_group_photo_indexes.add(photo_identity)
        group_contract = (
            item["group_photo_count"],
            item["issue_type"],
            item["maintenance_action"],
        )
        previous_group_contract = group_contracts.setdefault(
            group_id,
            group_contract,
        )
        if previous_group_contract != group_contract:
            raise ProjectModelLifecycleError(
                "dataset_integrity_failed",
                "DatasetVersion approved reference group is inconsistent",
            )
    approved_reference_training_event_count = len(group_contracts)
    raw_provenance_graph = manifest.get("provenance_graph")
    provenance_graph: Optional[Dict[str, Any]] = None
    if (
        "provenance_graph" in manifest
        and raw_provenance_graph is None
    ):
        raise ProjectModelLifecycleError(
            "dataset_provenance_invalid",
            "DatasetVersion training provenance is missing",
        )
    if raw_provenance_graph is not None:
        try:
            provenance_graph = canonical_dataset_provenance_graph(
                raw_provenance_graph
            )
        except TrainingProvenanceError as error:
            raise ProjectModelLifecycleError(
                "dataset_provenance_invalid",
                "DatasetVersion training provenance is invalid",
            ) from error
        if (
            provenance_graph != raw_provenance_graph
            or provenance_graph["project_id"] != project_id
        ):
            raise ProjectModelLifecycleError(
                "project_boundary_violation",
                "DatasetVersion provenance belongs to another project",
            )
        training_members_by_id = {
            item["annotation_id"]: item
            for item in normalized_manifest_members
            if item["include_in_training"]
        }
        graph_members_by_id = {
            item["annotation_id"]: item
            for item in provenance_graph["members"]
        }
        if set(training_members_by_id) != set(graph_members_by_id):
            raise ProjectModelLifecycleError(
                "dataset_provenance_invalid",
                (
                    "DatasetVersion provenance does not cover its exact "
                    "training membership"
                ),
            )
        for annotation_id, member in training_members_by_id.items():
            graph_member = graph_members_by_id[annotation_id]
            for field in (
                "analysis_job_id",
                "answer_record_id",
                "answer_revision",
                "candidate_id",
                "question_id",
                "split",
            ):
                if graph_member[field] != member[field]:
                    raise ProjectModelLifecycleError(
                        "dataset_provenance_invalid",
                        (
                            "DatasetVersion provenance crossed a "
                            f"membership {field} boundary"
                        ),
                    )
    fingerprint = {
        "approved_reference_members": list(
            normalized_reference_members
        ),
        "members": list(normalized_manifest_members),
        "project_id": project_id,
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": manifest.get("split_strategy"),
        "validation_fraction": manifest.get("validation_fraction"),
    }
    if provenance_graph is not None:
        fingerprint["provenance_graph"] = provenance_graph
    if _canonical_sha256(fingerprint) != dataset_hash:
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion hash does not match its immutable manifest",
        )
    counts = manifest.get("counts")
    expected_counts = {
        "approved_reference_training_events": (
            approved_reference_training_event_count
        ),
        "approved_reference_training_members": len(
            normalized_reference_members
        ),
        "excluded": sum(
            item["split"] == "excluded"
            for item in normalized_memberships
        ),
        "source_answers": len(normalized_memberships),
        "train": sum(
            item["split"] == "train" for item in normalized_memberships
        ),
        "training_gate_members": (
            sum(
                item["include_in_training"] and item["split"] == "train"
                for item in normalized_memberships
            )
        ),
        "training_members": sum(
            item["include_in_training"]
            for item in normalized_memberships
        ),
        "validation": sum(
            item["split"] == "validation"
            for item in normalized_memberships
        ),
    }
    if counts != expected_counts:
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion counts do not match its members",
        )
    if (
        dataset_version.get("training_gate_member_count")
        != expected_counts["training_gate_members"]
        or dataset_version.get(
            "approved_reference_training_event_count"
        )
        != approved_reference_training_event_count
    ):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "DatasetVersion training gate count is invalid",
        )
    return {
        "dataset_version_id": dataset_id,
        "dataset_hash": dataset_hash,
        "schema_version": DATASET_SCHEMA_VERSION,
        "split_strategy": fingerprint["split_strategy"],
        "validation_fraction": fingerprint["validation_fraction"],
        "memberships": normalized_memberships,
        "approved_reference_members": normalized_reference_members,
        "approved_reference_members_sha256": (
            reference_members_sha256
        ),
        "approved_reference_training_event_count": (
            approved_reference_training_event_count
        ),
        "provenance_graph": provenance_graph,
        "provenance_graph_sha256": (
            provenance_graph["provenance_graph_sha256"]
            if provenance_graph is not None
            else None
        ),
        "manifest_sha256": _canonical_sha256(manifest),
    }


def _normalize_annotations(
    project_id: str,
    dataset: Mapping[str, Any],
    current_annotations: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], ...]:
    if (
        not isinstance(current_annotations, Sequence)
        or isinstance(current_annotations, (str, bytes, bytearray))
    ):
        raise ProjectModelLifecycleError(
            "annotation_integrity_failed",
            "current_annotations must be a sequence",
        )
    by_id: Dict[str, Mapping[str, Any]] = {}
    assist_by_id: Dict[str, Optional[Dict[str, Any]]] = {}
    provenance_by_id: Dict[
        str, Optional[Dict[str, Any]]
    ] = {}
    dataset_provenance = dataset.get("provenance_graph")
    dataset_graph_members = (
        {
            member["annotation_id"]: member
            for member in dataset_provenance["members"]
        }
        if isinstance(dataset_provenance, Mapping)
        else {}
    )
    for annotation in current_annotations:
        if not isinstance(annotation, Mapping):
            raise ProjectModelLifecycleError(
                "annotation_integrity_failed",
                "current annotation is not an object",
            )
        annotation_id = _canonical_uuid(
            annotation.get("annotation_id"),
            "annotation_id",
        )
        if annotation_id in by_id:
            raise ProjectModelLifecycleError(
                "annotation_integrity_failed",
                "current annotations contain duplicates",
            )
        if (
            _canonical_uuid(
                annotation.get("project_id"),
                "annotation.project_id",
            )
            != project_id
        ):
            raise ProjectModelLifecycleError(
                "project_boundary_violation",
                "current annotation belongs to another project",
            )
        answered_by = annotation.get("answered_by", "local_user")
        label_source = annotation.get(
            "label_source", "local_user_review"
        )
        source_kind = annotation.get(
            "source_kind", "current_video_confirmed"
        )
        user_confirmed = annotation.get("user_confirmed", True)
        training_authorized = annotation.get(
            "training_authorized", True
        )
        human_label = (
            answered_by == "local_user"
            and label_source == "local_user_review"
            and source_kind == "current_video_confirmed"
            and user_confirmed is True
            and training_authorized is True
        )
        structured_llm_label = (
            answered_by == "structured_llm_assisted"
            and label_source == "structured_llm_assisted"
            and source_kind == "structured_llm_assisted"
            and user_confirmed is False
            and training_authorized is True
        )
        if not human_label and not structured_llm_label:
            raise ProjectModelLifecycleError(
                "unconfirmed_training_data",
                "annotation training provenance is not authorized",
            )
        assist_provenance = annotation.get("assist_provenance")
        if human_label:
            if assist_provenance is not None:
                raise ProjectModelLifecycleError(
                    "annotation_provenance_invalid",
                    "human annotation cannot carry structured assist provenance",
                )
            canonical_assist = None
        else:
            try:
                canonical_assist = (
                    canonical_structured_assist_provenance(
                        assist_provenance
                    )
                )
            except StructuredAssistProvenanceError as error:
                raise ProjectModelLifecycleError(
                    "unconfirmed_training_data",
                    (
                        "structured LLM annotation is missing its "
                        "authority and provider execution provenance"
                    ),
                ) from error
            if (
                canonical_assist["project_id"] != project_id
                or canonical_assist["analysis_job_id"]
                != annotation.get("analysis_job_id")
                or canonical_assist["question_id"]
                != annotation.get("question_id")
                or canonical_assist["candidate_id"]
                != annotation.get("candidate_id")
            ):
                raise ProjectModelLifecycleError(
                    "annotation_provenance_invalid",
                    "structured assist provenance crossed an annotation boundary",
                )
        raw_provenance = annotation.get("provenance_graph")
        canonical_provenance: Optional[Dict[str, Any]] = None
        if raw_provenance is not None:
            try:
                canonical_provenance = (
                    canonical_annotation_provenance_graph(
                        raw_provenance
                    )
                )
            except TrainingProvenanceError as error:
                raise ProjectModelLifecycleError(
                    "annotation_provenance_invalid",
                    "annotation training provenance is invalid",
                ) from error
            if (
                canonical_provenance["project_id"] != project_id
                or canonical_provenance["analysis_job_id"]
                != annotation.get("analysis_job_id")
                or canonical_provenance["question_id"]
                != annotation.get("question_id")
                or canonical_provenance["candidate_id"]
                != annotation.get("candidate_id")
                or canonical_provenance["answer_record_id"]
                != annotation.get("answer_record_id")
                or canonical_provenance["answer_revision"]
                != annotation.get("answer_revision")
                or canonical_provenance["answer_authority"][
                    "answered_by"
                ]
                != answered_by
                or canonical_provenance["answer_authority"][
                    "include_in_training"
                ]
                != annotation.get("include_in_training")
            ):
                raise ProjectModelLifecycleError(
                    "annotation_provenance_invalid",
                    "annotation training provenance crossed a truth boundary",
                )
        graph_member = dataset_graph_members.get(annotation_id)
        if annotation.get("include_in_training") is True:
            if dataset_provenance is not None and (
                canonical_provenance is None
                or graph_member is None
                or graph_member["provenance_graph_sha256"]
                != canonical_provenance[
                    "provenance_graph_sha256"
                ]
            ):
                raise ProjectModelLifecycleError(
                    "annotation_provenance_stale",
                    (
                        "annotation provenance does not match its "
                        "DatasetVersion revision"
                    ),
                )
            if dataset_provenance is None and (
                "provenance_graph" in annotation
            ):
                raise ProjectModelLifecycleError(
                    "dataset_provenance_invalid",
                    (
                        "DatasetVersion did not freeze the annotation "
                        "provenance graph"
                    ),
                )
        by_id[annotation_id] = annotation
        assist_by_id[annotation_id] = canonical_assist
        provenance_by_id[annotation_id] = canonical_provenance

    normalized = []
    for member in dataset["memberships"]:
        annotation = by_id.get(member["annotation_id"])
        if annotation is None:
            raise ProjectModelLifecycleError(
                "annotation_integrity_failed",
                "DatasetVersion annotation is missing from current annotations",
            )
        for field in (
            "annotation_hash",
            "source_record_hash",
            "partition",
            "include_in_training",
        ):
            if annotation.get(field) != member[field]:
                raise ProjectModelLifecycleError(
                    "annotation_integrity_failed",
                    f"annotation {field} differs from DatasetVersion",
                )
        issue_type = annotation.get("selected_issue_type")
        if issue_type is not None and issue_type not in CANONICAL_ISSUE_TYPES:
            raise ProjectModelLifecycleError(
                "annotation_integrity_failed",
                "annotation issue type is not trainable",
            )
        if member["partition"] == "approved" and issue_type is None:
            raise ProjectModelLifecycleError(
                "annotation_integrity_failed",
                "approved annotation requires an issue type",
            )
        training_geometry = annotation.get("training_geometry")
        if training_geometry is not None:
            try:
                training_geometry = canonical_training_geometry(
                    training_geometry
                )
            except TrainingGeometryError as error:
                raise ProjectModelLifecycleError(
                    "annotation_geometry_invalid",
                    "annotation training geometry is invalid",
                ) from error
            if (
                training_geometry["approval_authority"]
                != annotation.get("answered_by", "local_user")
            ):
                raise ProjectModelLifecycleError(
                    "annotation_geometry_invalid",
                    "annotation geometry authority is inconsistent",
                )
            if member["partition"] == "negative":
                if (
                    training_geometry["source_kind"]
                    != "negative_evidence"
                ):
                    raise ProjectModelLifecycleError(
                        "annotation_geometry_invalid",
                        "negative annotation geometry is inconsistent",
                    )
            elif member["partition"] == "approved":
                if (
                    training_geometry["source_kind"]
                    == "negative_evidence"
                    or training_geometry["normalized_box"]
                    != annotation.get("normalized_box")
                ):
                    raise ProjectModelLifecycleError(
                        "annotation_geometry_invalid",
                        "approved annotation geometry is inconsistent",
                    )
            else:
                raise ProjectModelLifecycleError(
                    "annotation_geometry_invalid",
                    "excluded annotation cannot carry training geometry",
                )
        answered_by = annotation.get("answered_by", "local_user")
        label_source = annotation.get(
            "label_source", "local_user_review"
        )
        source_kind = annotation.get(
            "source_kind", "current_video_confirmed"
        )
        user_confirmed = annotation.get("user_confirmed", True)
        training_authorized = annotation.get(
            "training_authorized", True
        )
        if (
            source_kind == "structured_llm_assisted"
            and member["split"] != "train"
        ):
            raise ProjectModelLifecycleError(
                "unconfirmed_validation_data",
                "structured LLM labels cannot enter fixed validation",
            )
        normalized.append(
            {
                **member,
                "project_id": project_id,
                "selected_issue_type": issue_type,
                "severity": annotation.get("severity"),
                "maintenance_action": annotation.get(
                    "maintenance_action"
                ),
                "answered_by": answered_by,
                "label_source": label_source,
                "source_scope": source_kind,
                "source_kind": source_kind,
                "training_authorized": training_authorized,
                "training_geometry": training_geometry,
                "assist_provenance": assist_by_id[
                    member["annotation_id"]
                ],
                "provenance_graph": provenance_by_id[
                    member["annotation_id"]
                ],
                "user_confirmed": user_confirmed,
            }
        )
    if len(by_id) != len(normalized):
        raise ProjectModelLifecycleError(
            "annotation_integrity_failed",
            "current annotations are not the exact DatasetVersion membership",
        )
    return tuple(normalized)


def _normalize_auxiliary_references(
    project_id: str,
    references: Sequence[Mapping[str, Any]],
    *,
    weight: float,
) -> Tuple[Dict[str, Any], ...]:
    if (
        isinstance(weight, bool)
        or not isinstance(weight, (int, float))
        or not math.isfinite(float(weight))
        or not 0 < float(weight) <= MAXIMUM_AUXILIARY_REFERENCE_WEIGHT
    ):
        raise ProjectModelLifecycleError(
            "invalid_auxiliary_reference_weight",
            "auxiliary reference weight must be above 0 and at most 0.5",
        )
    if (
        not isinstance(references, Sequence)
        or isinstance(references, (str, bytes, bytearray))
    ):
        raise ProjectModelLifecycleError(
            "invalid_auxiliary_references",
            "auxiliary references must be a sequence",
        )
    values = []
    seen = set()
    for reference in references:
        if not isinstance(reference, Mapping):
            raise ProjectModelLifecycleError(
                "invalid_auxiliary_references",
                "auxiliary reference is not an object",
            )
        if reference.get("source_kind") == "approved_reference":
            frozen = _dataset_reference_member_value(reference)
            approval_id = frozen["approval_id"]
            if approval_id in seen:
                raise ProjectModelLifecycleError(
                    "invalid_auxiliary_references",
                    "approved reference membership is duplicated",
                )
            seen.add(approval_id)
            values.append(
                {
                    **frozen,
                    "revision": frozen["approval_revision"],
                    "source_scope": (
                        "approved_reference_dataset_member"
                    ),
                    "training_weight": None,
                    "counts_toward_training_gate": False,
                    "counts_toward_fixed_validation": False,
                }
            )
            continue
        if (
            _canonical_uuid(
                reference.get("project_id"),
                "reference.project_id",
            )
            != project_id
        ):
            raise ProjectModelLifecycleError(
                "project_boundary_violation",
                "approved reference belongs to another project",
            )
        approval_id = reference.get("approval_id")
        if (
            not isinstance(approval_id, str)
            or _APPROVAL_ID.fullmatch(approval_id) is None
            or approval_id in seen
        ):
            raise ProjectModelLifecycleError(
                "invalid_auxiliary_references",
                "auxiliary reference approval identity is invalid",
            )
        seen.add(approval_id)
        if any(
            (
                reference.get("reference_state") != "approved_reference",
                reference.get("approved_reference") is not True,
                reference.get("training_eligible") is not True,
                reference.get("training_scope")
                != "approved_reference_only",
                reference.get("user_confirmed") is not True,
                reference.get("annotation_created") is not False,
                reference.get("incident_created") is not False,
            )
        ):
            raise ProjectModelLifecycleError(
                "unconfirmed_training_data",
                "weak or unconfirmed sample cases cannot enter training",
            )
        issue_type = reference.get("issue_type")
        if issue_type not in CANONICAL_ISSUE_TYPES:
            raise ProjectModelLifecycleError(
                "invalid_auxiliary_references",
                "approved reference issue type is not trainable",
            )
        revision = reference.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise ProjectModelLifecycleError(
                "invalid_auxiliary_references",
                "approved reference revision is invalid",
            )
        record_sha256 = _record_hash(reference, "record_sha256")
        image_sha256 = _sha256(
            reference.get("image_sha256"),
            "reference.image_sha256",
        )
        values.append(
            {
                "approval_id": approval_id,
                "revision": revision,
                "issue_type": issue_type,
                "maintenance_action": reference.get(
                    "maintenance_action"
                ),
                "image_sha256": image_sha256,
                "record_sha256": record_sha256,
                "source_id": reference.get("source_id"),
                "case_id": reference.get("case_id"),
                "source_scope": "approved_reference_auxiliary_only",
                "training_weight": round(float(weight), 6),
                "counts_toward_training_gate": False,
                "counts_toward_fixed_validation": False,
            }
        )
    if any(
        value.get("source_scope")
        != "approved_reference_dataset_member"
        for value in values
    ):
        raise ProjectModelLifecycleError(
            "invalid_auxiliary_references",
            (
                "training references must be immutable approved-reference "
                "DatasetVersion members"
            ),
        )
    members_per_group: Dict[str, int] = {}
    for value in values:
        group_id = value["sample_evidence_group_id"]
        members_per_group[group_id] = (
            members_per_group.get(group_id, 0) + 1
        )
    for value in values:
        group_id = value["sample_evidence_group_id"]
        value["training_weight"] = round(
            float(weight) / members_per_group[group_id],
            6,
        )
        value["counts_toward_training_gate"] = False
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item["sample_evidence_group_id"],
                item["group_photo_index"],
                item["approval_id"],
            ),
        )
    )


def _fixed_validation_truth(
    annotations: Sequence[Mapping[str, Any]],
    required_issue_types: Sequence[str],
) -> Tuple[Dict[str, Any], ...]:
    records = []
    for annotation in annotations:
        if (
            annotation["split"] != "validation"
            or not annotation["include_in_training"]
        ):
            continue
        selected_issue_type = annotation["selected_issue_type"]
        for issue_type in required_issue_types:
            records.append(
                {
                    "sample_id": annotation["annotation_id"],
                    "issue_type": issue_type,
                    "truth_event_ids": (
                        [f"event-{annotation['candidate_id']}"]
                        if (
                            annotation["partition"] == "approved"
                            and selected_issue_type == issue_type
                        )
                        else []
                    ),
                }
            )
    return tuple(
        sorted(
            records,
            key=lambda item: (
                CANONICAL_ISSUE_TYPES.index(item["issue_type"]),
                item["sample_id"],
            ),
        )
    )


def _derive_dataset_issue_types(
    annotations: Sequence[Mapping[str, Any]],
    approved_references: Sequence[Mapping[str, Any]],
) -> Tuple[str, ...]:
    observed = {
        annotation["selected_issue_type"]
        for annotation in annotations
        if (
            annotation["include_in_training"]
            and annotation["partition"] == "approved"
            and annotation.get("selected_issue_type")
            in CANONICAL_ISSUE_TYPES
        )
    }
    observed.update(
        reference["issue_type"]
        for reference in approved_references
        if reference.get("issue_type") in CANONICAL_ISSUE_TYPES
    )
    if not observed:
        raise ProjectModelLifecycleError(
            "dataset_issue_types_unavailable",
            (
                "DatasetVersion has no user-confirmed positive issue "
                "type for project model training"
            ),
        )
    return tuple(
        issue_type
        for issue_type in CANONICAL_ISSUE_TYPES
        if issue_type in observed
    )


def _training_evidence_identities(
    annotation: Mapping[str, Any],
) -> frozenset[Tuple[str, str]]:
    """Return path-free evidence identities that must not cross the split."""

    identities: set[Tuple[str, str]] = set()
    for field in (
        "annotation_hash",
        "source_record_hash",
        "candidate_id",
    ):
        value = annotation.get(field)
        if isinstance(value, str) and value:
            identities.add((field, value))

    geometry = annotation.get("training_geometry")
    if isinstance(geometry, Mapping):
        for field in (
            "image_sha256",
            "mask_sha256",
            "proposal_sha256",
            "sam2_output_sha256",
        ):
            value = geometry.get(field)
            if isinstance(value, str) and value:
                identities.add(
                    (f"training_geometry.{field}", value)
                )
        tracked = geometry.get("tracked_frame_refs")
        if isinstance(tracked, Sequence) and not isinstance(
            tracked, (str, bytes, bytearray)
        ):
            for frame in tracked:
                if not isinstance(frame, Mapping):
                    continue
                for field in (
                    "image_sha256",
                    "mask_sha256",
                ):
                    value = frame.get(field)
                    if isinstance(value, str) and value:
                        identities.add(
                            (f"tracked_frame.{field}", value)
                        )
    provenance = annotation.get("provenance_graph")
    if isinstance(provenance, Mapping):
        for field in (
            "source_sha256",
            "candidate_metadata_sha256",
            "image_sha256",
            "mask_sha256",
            "proposal_sha256",
            "geometry_sha256",
        ):
            value = provenance.get(field)
            if isinstance(value, str) and value:
                identities.add(
                    (f"provenance_graph.{field}", value)
                )
    return frozenset(identities)


def _fixed_validation_binding_identity(
    annotation: Mapping[str, Any],
) -> Tuple[str, ...]:
    """Canonical immutable identity for one fixed-validation member."""

    geometry = annotation.get("training_geometry")
    geometry_sha256 = (
        _canonical_sha256(geometry)
        if isinstance(geometry, Mapping)
        else ""
    )
    provenance = annotation.get("provenance_graph")
    provenance_sha256 = (
        str(provenance.get("provenance_graph_sha256", ""))
        if isinstance(provenance, Mapping)
        else ""
    )
    return (
        str(annotation.get("annotation_id", "")),
        str(annotation.get("annotation_hash", "")),
        str(annotation.get("question_id", "")),
        str(annotation.get("candidate_id", "")),
        str(annotation.get("analysis_job_id", "")),
        str(annotation.get("source_record_hash", "")),
        str(annotation.get("partition", "")),
        str(annotation.get("selected_issue_type", "")),
        geometry_sha256,
        provenance_sha256,
    )


def _assert_fixed_validation_training_boundary(
    annotations: Sequence[Mapping[str, Any]],
    fixed_validation_annotations: Sequence[Mapping[str, Any]],
) -> None:
    """Reject split leakage before any trainer or artifact staging runs.

    The persisted project fixed-validation set is authoritative. A later
    DatasetVersion may neither reclassify one of its sources as TRAIN nor
    silently substitute another validation membership.
    """

    train = tuple(
        annotation
        for annotation in annotations
        if (
            annotation.get("include_in_training") is True
            and annotation.get("split") == "train"
        )
    )
    dataset_validation = tuple(
        annotation
        for annotation in annotations
        if (
            annotation.get("include_in_training") is True
            and annotation.get("split") == "validation"
        )
    )
    fixed = tuple(fixed_validation_annotations)

    for field in (
        "annotation_id",
        "question_id",
        "analysis_job_id",
    ):
        train_values = {
            str(annotation.get(field))
            for annotation in train
            if annotation.get(field) is not None
        }
        fixed_values = {
            str(annotation.get(field))
            for annotation in fixed
            if annotation.get(field) is not None
        }
        if train_values.intersection(fixed_values):
            raise ProjectModelLifecycleError(
                "fixed_validation_training_leak",
                (
                    "DatasetVersion TRAIN membership overlaps the project "
                    f"fixed-validation {field} boundary"
                ),
            )

    train_evidence: set[Tuple[str, str]] = set()
    for annotation in train:
        train_evidence.update(
            _training_evidence_identities(annotation)
        )
    fixed_evidence: set[Tuple[str, str]] = set()
    for annotation in fixed:
        fixed_evidence.update(
            _training_evidence_identities(annotation)
        )
    if train_evidence.intersection(fixed_evidence):
        raise ProjectModelLifecycleError(
            "fixed_validation_training_leak",
            (
                "DatasetVersion TRAIN evidence overlaps the project "
                "fixed-validation evidence boundary"
            ),
        )

    dataset_binding = sorted(
        _fixed_validation_binding_identity(annotation)
        for annotation in dataset_validation
    )
    fixed_binding = sorted(
        _fixed_validation_binding_identity(annotation)
        for annotation in fixed
    )
    if dataset_binding != fixed_binding:
        raise ProjectModelLifecycleError(
            "fixed_validation_binding_stale",
            (
                "DatasetVersion validation membership does not match the "
                "project fixed-validation set"
            ),
        )


def _training_request(
    *,
    project_id: str,
    dataset: Mapping[str, Any],
    annotations: Sequence[Mapping[str, Any]],
    auxiliary_references: Sequence[Mapping[str, Any]],
    base_model_version: str,
    required_issue_types: Sequence[str],
    fixed_validation_annotations: Sequence[Mapping[str, Any]],
    fixed_validation_truth: Sequence[Mapping[str, Any]],
    fixed_validation_sha256: str,
    trainer: ProjectModelTrainer,
) -> Dict[str, Any]:
    current_members = [
        dict(annotation)
        for annotation in annotations
        if annotation["include_in_training"]
    ]
    train_members = [
        annotation
        for annotation in current_members
        if annotation["split"] == "train"
    ]
    validation_members = [
        dict(annotation)
        for annotation in fixed_validation_annotations
    ]
    validation_counts = {
        issue_type: sum(
            record["issue_type"] == issue_type
            for record in fixed_validation_truth
        )
        for issue_type in required_issue_types
    }
    approved_reference_training_event_count = len(
        {
            str(reference["sample_evidence_group_id"])
            for reference in auxiliary_references
        }
    )
    if any(
        reference.get("counts_toward_training_gate") is not False
        for reference in auxiliary_references
    ):
        raise ProjectModelLifecycleError(
            "dataset_integrity_failed",
            "approved references cannot satisfy the current-video label gate",
        )
    # Only TRAIN-split, current-video authorized Annotation contributes to
    # the 100-member start gate. Fixed-validation members remain an
    # independent >=30-per-type quality gate.
    training_gate_member_count = len(train_members)
    request = {
        "schema_version": TRAINER_CONTRACT_VERSION,
        "project_id": project_id,
        "dataset_version_id": dataset["dataset_version_id"],
        "dataset_hash": dataset["dataset_hash"],
        "dataset_manifest_sha256": dataset["manifest_sha256"],
        "base_model_version": base_model_version,
        "required_issue_types": list(required_issue_types),
        "minimum_current_confirmed_labels": (
            MINIMUM_CURRENT_CONFIRMED_LABELS
        ),
        "minimum_validation_samples_per_type": (
            DEFAULT_MINIMUM_SAMPLES_PER_TYPE
        ),
        "maximum_recall_drop": DEFAULT_MAX_RECALL_DROP,
        "minimum_candidate_recall": DEFAULT_MINIMUM_CANDIDATE_RECALL,
        "minimum_candidate_precision": (
            DEFAULT_MINIMUM_CANDIDATE_PRECISION
        ),
        "current_confirmed_label_count": training_gate_member_count,
        "approved_reference_training_member_count": len(
            auxiliary_references
        ),
        "approved_reference_training_event_count": (
            approved_reference_training_event_count
        ),
        "training_gate_member_count": training_gate_member_count,
        "current_train_member_count": len(train_members),
        "current_validation_member_count": sum(
            annotation["split"] == "validation"
            for annotation in current_members
        ),
        "current_training_annotations": train_members,
        "fixed_validation_annotations": validation_members,
        "fixed_validation_truth": list(fixed_validation_truth),
        "fixed_validation_sha256": fixed_validation_sha256,
        "validation_sample_counts": validation_counts,
        "auxiliary_reference_seeds": list(auxiliary_references),
        "auxiliary_reference_count": len(auxiliary_references),
        "approved_references_count_toward_training_gate": False,
        "auxiliary_references_count_toward_validation": False,
        "trainer": {
            "contract_version": TRAINER_CONTRACT_VERSION,
            "trainer_id": trainer.trainer_id,
            "trainer_version": trainer.trainer_version,
            "available": trainer.available,
            "test_only": trainer.test_only,
        },
        "automatic_promotion": False,
        "user_promotion_required": True,
    }
    provenance_graph = dataset.get("provenance_graph")
    if provenance_graph is not None:
        request["dataset_provenance_graph"] = provenance_graph
        request["dataset_provenance_graph_sha256"] = (
            provenance_graph["provenance_graph_sha256"]
        )
    return request


class ProjectModelLifecycleStore:
    """Restart-safe SQLite ledger plus hash-verified local model artifacts."""

    def __init__(
        self,
        data_root: Path,
        *,
        trainer: Optional[ProjectModelTrainer] = None,
        allow_test_trainer: bool = False,
        require_auxiliary_reference: bool = True,
        auxiliary_reference_weight: float = (
            DEFAULT_AUXILIARY_REFERENCE_WEIGHT
        ),
        artifact_root_resolver: Optional[
            Callable[
                [
                    UUID,
                    UUID,
                    str,
                    Optional[Mapping[str, Any]],
                ],
                ResolvedModelArtifactRoot,
            ]
        ] = None,
        start_worker: bool = True,
    ) -> None:
        root = _ensure_private_root(Path(data_root))
        self._root = root / "project-model-lifecycle"
        self._root.mkdir(mode=0o700, exist_ok=True)
        if _is_link_or_reparse(self._root):
            raise ValueError("model lifecycle store cannot be a link")
        self._artifact_root = self._root / "artifacts"
        self._artifact_root.mkdir(mode=0o700, exist_ok=True)
        self._database_path = self._root / "lifecycle.sqlite3"
        self._trainer = trainer or VisualEmbeddingCentroidHeadTrainerAdapter()
        if self._trainer.test_only and not allow_test_trainer:
            raise ValueError(
                "test-only project trainer requires allow_test_trainer=True"
            )
        _canonical_identifier(self._trainer.trainer_id, "trainer_id")
        _canonical_identifier(self._trainer.trainer_version, "trainer_version")
        self._require_auxiliary_reference = require_auxiliary_reference
        self._auxiliary_reference_weight = auxiliary_reference_weight
        self._artifact_root_resolver = artifact_root_resolver
        self._lock = threading.RLock()
        self._work_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._worker_enabled = bool(start_worker)
        self._active_job_id: Optional[str] = None
        self._active_cancel_event: Optional[threading.Event] = None
        self._migrate()
        self._recover_interrupted()
        if self._worker_enabled:
            with self._lock:
                self._ensure_worker_locked()
            self._work_event.set()

    @property
    def supports_bound_artifact_roots(self) -> bool:
        """Whether training artifacts require a caller-pinned project root."""

        return self._artifact_root_resolver is not None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._database_path),
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS training_jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, idempotency_key),
                    UNIQUE(project_id, request_sha256)
                );
                CREATE INDEX IF NOT EXISTS training_jobs_project_created
                    ON training_jobs(project_id, created_at, id);

                CREATE TABLE IF NOT EXISTS model_versions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    training_job_id TEXT NOT NULL UNIQUE,
                    model_version TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, model_version),
                    FOREIGN KEY(training_job_id) REFERENCES training_jobs(id)
                );
                CREATE INDEX IF NOT EXISTS model_versions_project_created
                    ON model_versions(project_id, created_at, id);

                CREATE TABLE IF NOT EXISTS project_model_states (
                    project_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_state_events (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS model_events_project_created
                    ON model_state_events(project_id, created_at, id);

                CREATE TABLE IF NOT EXISTS project_fixed_validation_sets (
                    project_id TEXT PRIMARY KEY,
                    membership_sha256 TEXT NOT NULL,
                    fixed_validation_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS project_fixed_validation_set_versions (
                    project_id TEXT NOT NULL,
                    validation_set_version INTEGER NOT NULL,
                    membership_sha256 TEXT NOT NULL,
                    fixed_validation_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, validation_set_version)
                );
                CREATE INDEX IF NOT EXISTS
                    fixed_validation_versions_project_created
                    ON project_fixed_validation_set_versions(
                        project_id, validation_set_version
                    );
                """
            )

    @staticmethod
    def _decode_integrity_row(
        row: Mapping[str, Any],
        *,
        label: str,
    ) -> Dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as error:
            raise ProjectModelLifecycleError(
                "lifecycle_store_corrupt",
                f"{label} JSON is corrupt",
            ) from error
        if (
            not isinstance(payload, dict)
            or _canonical_sha256(payload) != row["payload_sha256"]
        ):
            raise ProjectModelLifecycleError(
                "lifecycle_store_corrupt",
                f"{label} integrity check failed",
            )
        return payload

    @staticmethod
    def _write_payload(
        connection: sqlite3.Connection,
        table: str,
        record_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        text = canonical_json_bytes(payload).decode("utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if table == "training_jobs":
            connection.execute(
                """
                UPDATE training_jobs
                SET payload_json = ?, payload_sha256 = ?, updated_at = ?,
                    status = ?, attempt_count = ?
                WHERE id = ?
                """,
                (
                    text,
                    digest,
                    payload["updated_at"],
                    payload["status"],
                    payload["attempt_count"],
                    record_id,
                ),
            )
        elif table == "model_versions":
            connection.execute(
                """
                UPDATE model_versions
                SET payload_json = ?, payload_sha256 = ?, updated_at = ?,
                    state = ?
                WHERE id = ?
                """,
                (
                    text,
                    digest,
                    payload["updated_at"],
                    payload["state"],
                    record_id,
                ),
            )
        elif table == "project_model_states":
            connection.execute(
                """
            UPDATE project_model_states
            SET payload_json = ?, payload_sha256 = ?, updated_at = ?,
                revision = ?
            WHERE project_id = ?
                """,
                (
                    text,
                    digest,
                    payload["updated_at"],
                    payload["revision"],
                    record_id,
                ),
            )
        else:
            raise ValueError("unsupported lifecycle payload table")

    def _recover_interrupted(self) -> None:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM training_jobs WHERE status = 'running'"
            ).fetchall()
            if not rows:
                return
            connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                payload = self._decode_integrity_row(
                    row,
                    label="training job",
                )
                cancelled = payload.get("cancel_requested") is True
                timestamp = _utc_now()
                payload.update(
                    {
                        "status": (
                            "cancelled" if cancelled else "interrupted"
                        ),
                        "error": {
                            "code": (
                                "training_cancelled"
                                if cancelled
                                else "process_restarted"
                            ),
                            "message": (
                                "training cancellation was recovered after "
                                "process restart"
                                if cancelled
                                else (
                                    "training was interrupted by process "
                                    "restart"
                                )
                            ),
                        },
                        "recovery_count": (
                            int(payload.get("recovery_count", 0)) + 1
                        ),
                        "updated_at": timestamp,
                    }
                )
                self._write_payload(
                    connection,
                    "training_jobs",
                    row["id"],
                    payload,
                )
            connection.execute("COMMIT")

    def _ensure_worker_locked(self) -> None:
        if (
            not self._worker_enabled
            or self._stop_event.is_set()
            or (
                self._worker is not None
                and self._worker.is_alive()
            )
        ):
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="trafficvisson-project-model-training",
            daemon=True,
        )
        self._worker.start()

    def _wake_worker(self) -> None:
        if not self._worker_enabled:
            return
        with self._lock:
            self._ensure_worker_locked()
        self._work_event.set()

    def start_dispatcher(self) -> None:
        """Start recovery dispatch after all trainer dependencies are bound."""

        with self._lock:
            if self._stop_event.is_set():
                raise ProjectModelLifecycleError(
                    "training_dispatcher_stopped",
                    "project model training dispatcher is stopped",
                )
            self._worker_enabled = True
            self._ensure_worker_locked()
        self._work_event.set()

    def _next_queued_job(self) -> Optional[Tuple[str, str]]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT project_id, id
                FROM training_jobs
                WHERE status = 'queued'
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return str(row["project_id"]), str(row["id"])

    def _worker_loop(self) -> None:
        idle_polls = 0
        while not self._stop_event.is_set():
            identity = self._next_queued_job()
            if identity is None:
                self._work_event.clear()
                if self._next_queued_job() is not None:
                    self._work_event.set()
                    idle_polls = 0
                    continue
                signalled = self._work_event.wait(timeout=0.25)
                if signalled:
                    idle_polls = 0
                else:
                    idle_polls += 1
                    if idle_polls >= 2:
                        with self._lock:
                            if self._work_event.is_set():
                                idle_polls = 0
                                continue
                            self._worker = None
                            return
                continue
            idle_polls = 0
            project_id, job_id = identity
            cancel_event = threading.Event()
            with self._lock:
                self._active_job_id = job_id
                self._active_cancel_event = cancel_event
            try:
                self._execute_job(
                    project_id,
                    job_id,
                    cancel_event=cancel_event,
                )
            except BaseException as error:
                # The worker is the supervision boundary for in-process
                # trainers. External CUDA trainers additionally supervise
                # their own child process. A boundary failure must leave a
                # resumable durable record instead of silently losing work.
                self._interrupt_job(
                    project_id,
                    job_id,
                    code="worker_boundary_interrupted",
                    message=(
                        str(error)[:1000]
                        or type(error).__name__
                    ),
                )
            finally:
                with self._lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = None
                        self._active_cancel_event = None

    def _interrupt_job(
        self,
        project_id: str,
        job_id: str,
        *,
        code: str,
        message: str,
    ) -> Dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_job_row(connection, project_id, job_id)
            job = self._decode_integrity_row(row, label="training job")
            if job["status"] != "running":
                connection.execute("COMMIT")
                return job
            cancelled = job.get("cancel_requested") is True
            job.update(
                {
                    "status": (
                        "cancelled" if cancelled else "interrupted"
                    ),
                    "error": {
                        "code": (
                            "training_cancelled" if cancelled else code
                        ),
                        "message": (
                            "training was cancelled by the user"
                            if cancelled
                            else message[:1000]
                        ),
                    },
                    "updated_at": _utc_now(),
                }
            )
            self._write_payload(
                connection,
                "training_jobs",
                job_id,
                job,
            )
            connection.execute("COMMIT")
        return job

    def shutdown(self, *, timeout_seconds: float = 5.0) -> None:
        """Stop dispatching and cooperatively interrupt the active trainer."""

        self._stop_event.set()
        with self._lock:
            active_cancel = self._active_cancel_event
            worker = self._worker
        if active_cancel is not None:
            active_cancel.set()
        self._work_event.set()
        if (
            worker is not None
            and worker.is_alive()
            and worker is not threading.current_thread()
        ):
            worker.join(timeout=max(0.0, float(timeout_seconds)))

    def _get_job_row(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        job_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM training_jobs
            WHERE project_id = ? AND id = ?
            """,
            (project_id, job_id),
        ).fetchone()
        if row is None:
            raise ProjectModelLifecycleError(
                "training_job_not_found",
                "training job was not found in this project",
            )
        return row

    def _get_state(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> Optional[Dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM project_model_states WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return self._decode_integrity_row(row, label="project model state")

    def _ensure_state(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        base_model_version: str,
    ) -> Dict[str, Any]:
        state = self._get_state(connection, project_id)
        if state is not None:
            if state["active_model_version"] != base_model_version:
                raise ProjectModelLifecycleError(
                    "stale_active_model",
                    "base model does not match the project's active model",
                )
            return state
        timestamp = _utc_now()
        state = {
            "schema_version": MODEL_STATE_SCHEMA_VERSION,
            "project_id": project_id,
            "revision": 0,
            "active_model_id": None,
            "active_model_version": base_model_version,
            "candidate_model_id": None,
            "candidate_model_version": None,
            "previous_model_id": None,
            "previous_model_version": None,
            "automatic_promotion": False,
            "updated_at": timestamp,
        }
        payload_text = canonical_json_bytes(state).decode("utf-8")
        connection.execute(
            """
            INSERT INTO project_model_states(
                project_id, revision, payload_json, payload_sha256, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                project_id,
                0,
                payload_text,
                hashlib.sha256(payload_text.encode("utf-8")).hexdigest(),
                timestamp,
            ),
        )
        return state

    def _project_fixed_validation(
        self,
        project_id: str,
        *,
        dataset_version_id: str,
        annotations: Sequence[Mapping[str, Any]],
        required_issue_types: Sequence[str],
        extension_approval: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Resolve, establish, or explicitly extend the held-out set.

        A persisted set is immutable.  Adding a newly derived issue type
        creates a child version only after a stale-safe, separate user
        approval and only when the child DatasetVersion carries at least the
        minimum number of user-confirmed positive validation examples for
        every added type.  Existing truth rows are copied byte-for-byte into
        the child; they are never silently reinterpreted as samples for the
        new type.
        """

        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM project_fixed_validation_sets
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if existing is not None:
                record = self._decode_integrity_row(
                    existing,
                    label="project fixed validation set",
                )
                schema_version = record.get("schema_version")
                if schema_version == LEGACY_FIXED_VALIDATION_SET_SCHEMA_VERSION:
                    validation_set_version = 1
                elif schema_version == FIXED_VALIDATION_SET_SCHEMA_VERSION:
                    raw_version = record.get("validation_set_version")
                    if (
                        isinstance(raw_version, bool)
                        or not isinstance(raw_version, int)
                        or raw_version < 1
                    ):
                        raise ProjectModelLifecycleError(
                            "lifecycle_store_corrupt",
                            "project fixed validation version is invalid",
                        )
                    validation_set_version = raw_version
                else:
                    raise ProjectModelLifecycleError(
                        "lifecycle_store_corrupt",
                        "project fixed validation schema is unsupported",
                    )
                try:
                    current_types = _canonical_required_types(
                        record.get("required_issue_types", ())
                    )
                except ProjectModelLifecycleError as error:
                    raise ProjectModelLifecycleError(
                        "lifecycle_store_corrupt",
                        "project fixed validation issue types are invalid",
                    ) from error
                if (
                    record.get("project_id") != project_id
                    or record.get("membership_sha256")
                    != existing["membership_sha256"]
                    or record.get("fixed_validation_sha256")
                    != existing["fixed_validation_sha256"]
                    or record.get(
                        "approved_reference_member_count"
                    )
                    != 0
                    or not isinstance(
                        record.get("fixed_validation_annotations"), list
                    )
                    or not isinstance(
                        record.get("fixed_validation_truth"), list
                    )
                ):
                    raise ProjectModelLifecycleError(
                        "lifecycle_store_corrupt",
                        "project fixed validation identity is corrupt",
                    )
                requested_types = tuple(required_issue_types)
                if requested_types == current_types:
                    if extension_approval is not None:
                        raise ProjectModelLifecycleError(
                            "fixed_validation_extension_not_required",
                            (
                                "fixed-validation extension approval was "
                                "provided but no new issue type was derived"
                            ),
                        )
                    return {
                        **record,
                        "validation_set_version": validation_set_version,
                        "schema_version": schema_version,
                    }
                if not set(current_types).issubset(requested_types):
                    raise ProjectModelLifecycleError(
                        "fixed_validation_changed",
                        (
                            "a fixed-validation version cannot remove or "
                            "replace an existing issue type"
                        ),
                    )
                added_issue_types = tuple(
                    issue_type
                    for issue_type in requested_types
                    if issue_type not in current_types
                )
                approval = _fixed_validation_extension_approval(
                    extension_approval,
                    parent_version=validation_set_version,
                    parent_membership_sha256=record["membership_sha256"],
                    parent_fixed_validation_sha256=record[
                        "fixed_validation_sha256"
                    ],
                    added_issue_types=added_issue_types,
                )
                parent_annotations = tuple(
                    dict(annotation)
                    for annotation in record[
                        "fixed_validation_annotations"
                    ]
                )
                proposed_annotations = tuple(
                    dict(annotation)
                    for annotation in annotations
                    if (
                        annotation["split"] == "validation"
                        and annotation["include_in_training"]
                    )
                )
                parent_by_id = {
                    str(annotation.get("annotation_id")): annotation
                    for annotation in parent_annotations
                }
                proposed_by_id = {
                    str(annotation.get("annotation_id")): annotation
                    for annotation in proposed_annotations
                }
                if (
                    len(parent_by_id) != len(parent_annotations)
                    or len(proposed_by_id) != len(proposed_annotations)
                ):
                    raise ProjectModelLifecycleError(
                        "fixed_validation_binding_stale",
                        "fixed-validation membership contains duplicate ids",
                    )
                for annotation_id, parent in parent_by_id.items():
                    proposed = proposed_by_id.get(annotation_id)
                    if (
                        proposed is None
                        or _fixed_validation_binding_identity(proposed)
                        != _fixed_validation_binding_identity(parent)
                    ):
                        raise ProjectModelLifecycleError(
                            "fixed_validation_binding_stale",
                            (
                                "the child DatasetVersion changed or removed "
                                "a parent fixed-validation member"
                            ),
                        )
                extension_annotations = tuple(
                    proposed_by_id[annotation_id]
                    for annotation_id in sorted(
                        set(proposed_by_id).difference(parent_by_id)
                    )
                )
                extension_analysis_job_ids = sorted(
                    {
                        _canonical_uuid(
                            annotation.get("analysis_job_id"),
                            "validation.analysis_job_id",
                        )
                        for annotation in extension_annotations
                    }
                )
                if extension_analysis_job_ids != approval[
                    "added_validation_analysis_job_ids"
                ]:
                    raise ProjectModelLifecycleError(
                        "fixed_validation_extension_approval_stale",
                        (
                            "approved extension analysis jobs do not match "
                            "the child DatasetVersion"
                        ),
                    )
                parent_evidence: set[Tuple[str, str]] = set()
                for annotation in parent_annotations:
                    parent_evidence.update(
                        _training_evidence_identities(annotation)
                    )
                extension_evidence: set[Tuple[str, str]] = set()
                for annotation in extension_annotations:
                    extension_evidence.update(
                        _training_evidence_identities(annotation)
                    )
                if parent_evidence.intersection(extension_evidence):
                    raise ProjectModelLifecycleError(
                        "fixed_validation_extension_duplicate_evidence",
                        (
                            "fixed-validation extension evidence overlaps "
                            "the parent validation set"
                        ),
                    )
                positive_counts = {
                    issue_type: sum(
                        annotation.get("partition") == "approved"
                        and annotation.get("selected_issue_type")
                        == issue_type
                        for annotation in extension_annotations
                    )
                    for issue_type in added_issue_types
                }
                insufficient = {
                    issue_type: count
                    for issue_type, count in positive_counts.items()
                    if count < DEFAULT_MINIMUM_SAMPLES_PER_TYPE
                }
                if insufficient:
                    detail = ", ".join(
                        f"{issue_type}={count}"
                        for issue_type, count in insufficient.items()
                    )
                    raise ProjectModelLifecycleError(
                        "fixed_validation_extension_insufficient",
                        (
                            "new fixed-validation issue types require "
                            f"{DEFAULT_MINIMUM_SAMPLES_PER_TYPE} confirmed "
                            f"positive samples each ({detail})"
                        ),
                    )
                extension_truth = _fixed_validation_truth(
                    extension_annotations,
                    added_issue_types,
                )
                truth = tuple(
                    dict(item)
                    for item in record["fixed_validation_truth"]
                ) + extension_truth
                try:
                    report = evaluate_model(
                        model_version=(
                            f"fixed-validation-baseline@"
                            f"{validation_set_version + 1}"
                        ),
                        truth_records=truth,
                        prediction_records=(),
                    )
                except ValidationMetricsError as error:
                    raise ProjectModelLifecycleError(
                        "fixed_validation_invalid",
                        str(error),
                    ) from error
                counts = {
                    issue_type: sum(
                        row["issue_type"] == issue_type
                        for row in truth
                    )
                    for issue_type in requested_types
                }
                if any(
                    count < DEFAULT_MINIMUM_SAMPLES_PER_TYPE
                    for count in counts.values()
                ):
                    raise ProjectModelLifecycleError(
                        "fixed_validation_extension_insufficient",
                        (
                            "the versioned fixed-validation set does not "
                            "satisfy the per-type sample minimum"
                        ),
                    )
                membership_value = [
                    {
                        "annotation_hash": row["annotation_hash"],
                        "annotation_id": row["annotation_id"],
                        "candidate_id": row["candidate_id"],
                        "partition": row["partition"],
                        "source_record_hash": row[
                            "source_record_hash"
                        ],
                        "source_scope": row["source_scope"],
                        "split": row["split"],
                    }
                    for row in proposed_annotations
                ]
                membership_sha256 = _canonical_sha256(membership_value)
                timestamp = _utc_now()
                child_version = validation_set_version + 1
                child_record = {
                    "schema_version": FIXED_VALIDATION_SET_SCHEMA_VERSION,
                    "project_id": project_id,
                    "validation_set_version": child_version,
                    "parent_validation_set_version": validation_set_version,
                    "parent_membership_sha256": record[
                        "membership_sha256"
                    ],
                    "parent_fixed_validation_sha256": record[
                        "fixed_validation_sha256"
                    ],
                    "origin_dataset_version_id": dataset_version_id,
                    "required_issue_types": list(requested_types),
                    "added_issue_types": list(added_issue_types),
                    "fixed_validation_annotations": list(
                        proposed_annotations
                    ),
                    "fixed_validation_truth": list(truth),
                    "validation_sample_counts": counts,
                    "extension_positive_sample_counts": positive_counts,
                    "membership_sha256": membership_sha256,
                    "fixed_validation_sha256": (
                        report.validation_set_sha256
                    ),
                    "extension_approval": approval,
                    "extension_approval_sha256": _canonical_sha256(
                        approval
                    ),
                    "approved_reference_member_count": 0,
                    "approved_references_count_toward_validation": False,
                    "created_at": timestamp,
                    "persisted": True,
                }
                child_text = canonical_json_bytes(child_record).decode(
                    "utf-8"
                )
                child_payload_sha256 = hashlib.sha256(
                    child_text.encode("utf-8")
                ).hexdigest()
                parent_text = canonical_json_bytes(record).decode("utf-8")
                parent_payload_sha256 = hashlib.sha256(
                    parent_text.encode("utf-8")
                ).hexdigest()
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current = connection.execute(
                        """
                        SELECT * FROM project_fixed_validation_sets
                        WHERE project_id = ?
                        """,
                        (project_id,),
                    ).fetchone()
                    if (
                        current is None
                        or current["membership_sha256"]
                        != record["membership_sha256"]
                        or current["fixed_validation_sha256"]
                        != record["fixed_validation_sha256"]
                    ):
                        raise ProjectModelLifecycleError(
                            "fixed_validation_extension_approval_stale",
                            (
                                "the fixed-validation parent changed before "
                                "the approved extension was committed"
                            ),
                        )
                    parent_history = connection.execute(
                        """
                        SELECT *
                        FROM project_fixed_validation_set_versions
                        WHERE project_id = ?
                          AND validation_set_version = ?
                        """,
                        (project_id, validation_set_version),
                    ).fetchone()
                    if parent_history is None:
                        connection.execute(
                            """
                            INSERT INTO
                                project_fixed_validation_set_versions(
                                    project_id, validation_set_version,
                                    membership_sha256,
                                    fixed_validation_sha256, payload_json,
                                    payload_sha256, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                project_id,
                                validation_set_version,
                                record["membership_sha256"],
                                record["fixed_validation_sha256"],
                                parent_text,
                                parent_payload_sha256,
                                record["created_at"],
                            ),
                        )
                    elif (
                        parent_history["membership_sha256"]
                        != record["membership_sha256"]
                        or parent_history["fixed_validation_sha256"]
                        != record["fixed_validation_sha256"]
                        or parent_history["payload_json"] != parent_text
                        or parent_history["payload_sha256"]
                        != parent_payload_sha256
                    ):
                        raise ProjectModelLifecycleError(
                            "lifecycle_store_corrupt",
                            (
                                "fixed-validation parent history conflicts "
                                "with the active head"
                            ),
                        )
                    connection.execute(
                        """
                        INSERT INTO project_fixed_validation_set_versions(
                            project_id, validation_set_version,
                            membership_sha256,
                            fixed_validation_sha256, payload_json,
                            payload_sha256, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            project_id,
                            child_version,
                            membership_sha256,
                            report.validation_set_sha256,
                            child_text,
                            child_payload_sha256,
                            timestamp,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE project_fixed_validation_sets
                        SET membership_sha256 = ?,
                            fixed_validation_sha256 = ?,
                            payload_json = ?, payload_sha256 = ?,
                            created_at = ?
                        WHERE project_id = ?
                        """,
                        (
                            membership_sha256,
                            report.validation_set_sha256,
                            child_text,
                            child_payload_sha256,
                            timestamp,
                            project_id,
                        ),
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
                return child_record

            validation_annotations = tuple(
                dict(annotation)
                for annotation in annotations
                if (
                    annotation["split"] == "validation"
                    and annotation["include_in_training"]
                )
            )
            truth = _fixed_validation_truth(
                annotations,
                required_issue_types,
            )
            try:
                report = evaluate_model(
                    model_version="fixed-validation-baseline@1",
                    truth_records=truth,
                    prediction_records=(),
                )
            except ValidationMetricsError as error:
                raise ProjectModelLifecycleError(
                    "fixed_validation_invalid",
                    str(error),
                ) from error
            counts = {
                issue_type: sum(
                    row["issue_type"] == issue_type
                    for row in truth
                )
                for issue_type in required_issue_types
            }
            membership_value = [
                {
                    "annotation_hash": row["annotation_hash"],
                    "annotation_id": row["annotation_id"],
                    "candidate_id": row["candidate_id"],
                    "partition": row["partition"],
                    "source_record_hash": row[
                        "source_record_hash"
                    ],
                    "source_scope": row["source_scope"],
                    "split": row["split"],
                }
                for row in validation_annotations
            ]
            membership_sha256 = _canonical_sha256(
                membership_value
            )
            timestamp = _utc_now()
            record = {
                "schema_version": FIXED_VALIDATION_SET_SCHEMA_VERSION,
                "project_id": project_id,
                "validation_set_version": 1,
                "parent_validation_set_version": None,
                "parent_membership_sha256": None,
                "parent_fixed_validation_sha256": None,
                "origin_dataset_version_id": dataset_version_id,
                "required_issue_types": list(
                    required_issue_types
                ),
                "added_issue_types": list(required_issue_types),
                "fixed_validation_annotations": list(
                    validation_annotations
                ),
                "fixed_validation_truth": list(truth),
                "validation_sample_counts": counts,
                "membership_sha256": membership_sha256,
                "fixed_validation_sha256": (
                    report.validation_set_sha256
                ),
                "extension_approval": None,
                "extension_approval_sha256": None,
                "approved_reference_member_count": 0,
                "approved_references_count_toward_validation": False,
                "created_at": timestamp,
                "persisted": all(
                    count >= DEFAULT_MINIMUM_SAMPLES_PER_TYPE
                    for count in counts.values()
                ),
            }
            if record["persisted"] is not True:
                return record

            payload_text = canonical_json_bytes(record).decode("utf-8")
            payload_sha256 = hashlib.sha256(
                payload_text.encode("utf-8")
            ).hexdigest()
            connection.execute("BEGIN IMMEDIATE")
            try:
                raced = connection.execute(
                    """
                    SELECT * FROM project_fixed_validation_sets
                    WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone()
                if raced is not None:
                    connection.execute("ROLLBACK")
                    return self._project_fixed_validation(
                        project_id,
                        dataset_version_id=dataset_version_id,
                        annotations=annotations,
                        required_issue_types=required_issue_types,
                        extension_approval=extension_approval,
                    )
                connection.execute(
                    """
                    INSERT INTO project_fixed_validation_sets(
                        project_id, membership_sha256,
                        fixed_validation_sha256, payload_json,
                        payload_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        membership_sha256,
                        report.validation_set_sha256,
                        payload_text,
                        payload_sha256,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO project_fixed_validation_set_versions(
                        project_id, validation_set_version,
                        membership_sha256, fixed_validation_sha256,
                        payload_json, payload_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        1,
                        membership_sha256,
                        report.validation_set_sha256,
                        payload_text,
                        payload_sha256,
                        timestamp,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            return record

    def resolve_fixed_validation_head(
        self,
        project_id: UUID,
    ) -> Optional[Dict[str, Any]]:
        """Resolve the current immutable set without depending on model state."""

        project_text = _canonical_uuid(project_id, "project_id")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM project_fixed_validation_sets
                WHERE project_id = ?
                """,
                (project_text,),
            ).fetchone()
            if row is None:
                return None
            record = self._decode_integrity_row(
                row,
                label="project fixed validation set",
            )
            if (
                record.get("project_id") != project_text
                or record.get("membership_sha256")
                != row["membership_sha256"]
                or record.get("fixed_validation_sha256")
                != row["fixed_validation_sha256"]
            ):
                raise ProjectModelLifecycleError(
                    "lifecycle_store_corrupt",
                    "project fixed validation head is corrupt",
                )
            version = record.get("validation_set_version", 1)
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
            ):
                raise ProjectModelLifecycleError(
                    "lifecycle_store_corrupt",
                    "project fixed validation head version is invalid",
                )
            return {
                **record,
                "validation_set_version": version,
            }

    def list_fixed_validation_versions(
        self,
        project_id: UUID,
    ) -> Dict[str, Any]:
        """Return integrity-checked immutable validation-set history."""

        project_text = _canonical_uuid(project_id, "project_id")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM project_fixed_validation_set_versions
                WHERE project_id = ?
                ORDER BY validation_set_version ASC
                """,
                (project_text,),
            ).fetchall()
            versions = []
            for row in rows:
                record = self._decode_integrity_row(
                    row,
                    label="project fixed validation set version",
                )
                if (
                    record.get("project_id") != project_text
                    or record.get("membership_sha256")
                    != row["membership_sha256"]
                    or record.get("fixed_validation_sha256")
                    != row["fixed_validation_sha256"]
                    or (
                        record.get("validation_set_version", 1)
                        != row["validation_set_version"]
                    )
                ):
                    raise ProjectModelLifecycleError(
                        "lifecycle_store_corrupt",
                        "fixed-validation version history is corrupt",
                    )
                versions.append(record)
            if not versions:
                current = connection.execute(
                    """
                    SELECT * FROM project_fixed_validation_sets
                    WHERE project_id = ?
                    """,
                    (project_text,),
                ).fetchone()
                if current is not None:
                    record = self._decode_integrity_row(
                        current,
                        label="project fixed validation set",
                    )
                    versions.append(
                        {
                            **record,
                            "validation_set_version": int(
                                record.get("validation_set_version", 1)
                            ),
                        }
                    )
        return {
            "schema_version": (
                "trafficvisson.project-fixed-validation-list.v2"
            ),
            "project_id": project_text,
            "count": len(versions),
            "versions": versions,
        }

    def start_training(
        self,
        project_id: UUID,
        *,
        dataset_version: Mapping[str, Any],
        current_annotations: Sequence[Mapping[str, Any]],
        auxiliary_references: Sequence[Mapping[str, Any]],
        base_model_version: str,
        required_issue_types: Sequence[str],
        idempotency_key: str,
        user_confirmed: bool,
        artifact_root_binding: Optional[Mapping[str, Any]] = None,
        fixed_validation_extension_approval: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> Dict[str, Any]:
        """Persist and enqueue an explicitly confirmed project training job.

        ``artifact_root_binding`` is a server-only binding captured from the
        project's current profile at this explicit training boundary.  It is
        intentionally independent of every source PipelineJob in the dataset;
        those job IDs remain lineage only and never authorize model storage.
        """

        if user_confirmed is not True:
            raise ProjectModelLifecycleError(
                "training_confirmation_required",
                "explicit user confirmation is required to start training",
            )
        project_text = _canonical_uuid(project_id, "project_id")
        idempotency = _canonical_idempotency_key(idempotency_key)
        base_version = _canonical_identifier(
            base_model_version,
            "base_model_version",
        )
        dataset = _normalize_dataset(project_text, dataset_version)
        annotations = _normalize_annotations(
            project_text,
            dataset,
            current_annotations,
        )
        if canonical_json_bytes(list(auxiliary_references)) != (
            canonical_json_bytes(
                list(dataset["approved_reference_members"])
            )
        ):
            raise ProjectModelLifecycleError(
                "dataset_integrity_failed",
                (
                    "training references must be the exact immutable "
                    "DatasetVersion membership"
                ),
            )
        references = _normalize_auxiliary_references(
            project_text,
            dataset["approved_reference_members"],
            weight=self._auxiliary_reference_weight,
        )
        target_types = _derive_dataset_issue_types(
            annotations,
            references,
        )
        proposed_validation_annotations = tuple(
            annotation
            for annotation in annotations
            if (
                annotation.get("include_in_training") is True
                and annotation.get("split") == "validation"
            )
        )
        _assert_fixed_validation_training_boundary(
            annotations,
            proposed_validation_annotations,
        )
        fixed_validation = self._project_fixed_validation(
            project_text,
            dataset_version_id=dataset["dataset_version_id"],
            annotations=annotations,
            required_issue_types=target_types,
            extension_approval=fixed_validation_extension_approval,
        )
        truth = tuple(fixed_validation["fixed_validation_truth"])
        fixed_annotations = tuple(
            fixed_validation["fixed_validation_annotations"]
        )
        fixed_hash = fixed_validation["fixed_validation_sha256"]
        _assert_fixed_validation_training_boundary(
            annotations,
            fixed_annotations,
        )
        request = _training_request(
            project_id=project_text,
            dataset=dataset,
            annotations=annotations,
            auxiliary_references=references,
            base_model_version=base_version,
            required_issue_types=target_types,
            fixed_validation_annotations=fixed_annotations,
            fixed_validation_truth=truth,
            fixed_validation_sha256=fixed_hash,
            trainer=self._trainer,
        )
        request["fixed_validation_membership_sha256"] = (
            fixed_validation["membership_sha256"]
        )
        request["fixed_validation_origin_dataset_version_id"] = (
            fixed_validation["origin_dataset_version_id"]
        )
        request["fixed_validation_persisted"] = (
            fixed_validation["persisted"]
        )
        request["fixed_validation_set_version"] = int(
            fixed_validation["validation_set_version"]
        )
        request["fixed_validation_set_schema_version"] = (
            fixed_validation["schema_version"]
        )
        request["fixed_validation_parent_version"] = (
            fixed_validation.get("parent_validation_set_version")
        )
        request["fixed_validation_extension_approval_sha256"] = (
            fixed_validation.get("extension_approval_sha256")
        )
        if artifact_root_binding is not None:
            normalized_root_binding = (
                _normalize_model_artifact_root_binding(
                    project_text,
                    artifact_root_binding,
                )
            )
            if self._artifact_root_resolver is None:
                raise ProjectModelLifecycleError(
                    "artifact_root_unavailable",
                    "project model artifact-root resolver is unavailable",
                )
            request["artifact_root_binding"] = normalized_root_binding
            request["artifact_root_binding_sha256"] = _canonical_sha256(
                normalized_root_binding
            )
            request["output_location_id"] = normalized_root_binding[
                "output_location_id"
            ]
            request["root_identity_sha256"] = normalized_root_binding[
                "root_identity_sha256"
            ]
        request_bytes = canonical_json_bytes(request)
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise ProjectModelLifecycleError(
                "training_request_too_large",
                "training request exceeds the local persistence limit",
            )
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        job_id = str(
            uuid5(
                UUID(project_text),
                f"{TRAINING_JOB_SCHEMA_VERSION}:{request_sha256}",
            )
        )

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_key = connection.execute(
                    """
                    SELECT * FROM training_jobs
                    WHERE project_id = ? AND idempotency_key = ?
                    """,
                    (project_text, idempotency),
                ).fetchone()
                if existing_key is not None:
                    if existing_key["request_sha256"] != request_sha256:
                        raise ProjectModelLifecycleError(
                            "idempotency_conflict",
                            "idempotency key was already used for another request",
                        )
                    existing_payload = self._decode_integrity_row(
                        existing_key,
                        label="training job",
                    )
                    connection.execute("COMMIT")
                    if existing_payload["status"] == "queued":
                        self._wake_worker()
                    return {
                        **existing_payload,
                        "idempotent_replay": True,
                    }
                existing_request = connection.execute(
                    """
                    SELECT * FROM training_jobs
                    WHERE project_id = ? AND request_sha256 = ?
                    """,
                    (project_text, request_sha256),
                ).fetchone()
                if existing_request is not None:
                    existing_payload = self._decode_integrity_row(
                        existing_request,
                        label="training job",
                    )
                    connection.execute("COMMIT")
                    if existing_payload["status"] == "queued":
                        self._wake_worker()
                    return {
                        **existing_payload,
                        "idempotent_replay": True,
                    }
                state = self._ensure_state(
                    connection,
                    project_text,
                    base_version,
                )
                if state["active_model_id"] is not None:
                    active_row = connection.execute(
                        """
                        SELECT * FROM model_versions
                        WHERE project_id = ? AND id = ?
                        """,
                        (project_text, state["active_model_id"]),
                    ).fetchone()
                    if active_row is None:
                        raise ProjectModelLifecycleError(
                            "lifecycle_store_corrupt",
                            "active project model record is missing",
                        )
                    self._verify_model_artifact(
                        self._decode_integrity_row(
                            active_row,
                            label="model version",
                        )
                    )
                blockers = []
                if request["current_confirmed_label_count"] < (
                    MINIMUM_CURRENT_CONFIRMED_LABELS
                ):
                    blockers.append(
                        {
                            "code": "insufficient_current_confirmed_labels",
                            "observed": request[
                                "current_confirmed_label_count"
                            ],
                            "current_video_members": request[
                                "current_confirmed_label_count"
                            ],
                            "approved_reference_members": request[
                                "approved_reference_training_member_count"
                            ],
                            "approved_reference_events": request[
                                "approved_reference_training_event_count"
                            ],
                            "required": MINIMUM_CURRENT_CONFIRMED_LABELS,
                        }
                    )
                for issue_type in target_types:
                    observed = request["validation_sample_counts"][
                        issue_type
                    ]
                    if observed < DEFAULT_MINIMUM_SAMPLES_PER_TYPE:
                        blockers.append(
                            {
                                "code": (
                                    "insufficient_validation_samples"
                                ),
                                "issue_type": issue_type,
                                "observed": observed,
                                "required": (
                                    DEFAULT_MINIMUM_SAMPLES_PER_TYPE
                                ),
                            }
                        )
                if self._require_auxiliary_reference and not references:
                    blockers.append(
                        {
                            "code": "approved_reference_required",
                            "observed": 0,
                            "required": 1,
                        }
                    )
                if state["candidate_model_id"] is not None:
                    blockers.append(
                        {
                            "code": "candidate_review_required",
                            "candidate_model_id": state[
                                "candidate_model_id"
                            ],
                        }
                    )
                if not self._trainer.available:
                    blockers.append(
                        {
                            "code": "trainer_unavailable",
                            "trainer_id": self._trainer.trainer_id,
                        }
                    )
                timestamp = _utc_now()
                status = "blocked" if blockers else "queued"
                payload = {
                    "schema_version": TRAINING_JOB_SCHEMA_VERSION,
                    "training_job_id": job_id,
                    "project_id": project_text,
                    "idempotency_key": idempotency,
                    "request_sha256": request_sha256,
                    "request": request,
                    "status": status,
                    "attempt_count": 0,
                    "blockers": blockers,
                    "error": None,
                    "progress": {
                        "stage": status,
                        "attempt_number": 0,
                        "updated_at": timestamp,
                    },
                    "checkpoint": None,
                    "cancel_requested": False,
                    "resume_count": 0,
                    "recovery_count": 0,
                    "model_version_id": None,
                    "automatic_training_started": False,
                    "user_confirmed_start": True,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                payload_text = canonical_json_bytes(payload).decode("utf-8")
                connection.execute(
                    """
                    INSERT INTO training_jobs(
                        id, project_id, idempotency_key, request_sha256,
                        status, attempt_count, payload_json, payload_sha256,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        project_text,
                        idempotency,
                        request_sha256,
                        status,
                        0,
                        payload_text,
                        hashlib.sha256(
                            payload_text.encode("utf-8")
                        ).hexdigest(),
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        if blockers:
            return {**payload, "idempotent_replay": False}
        self._wake_worker()
        return {**payload, "idempotent_replay": False}

    def _resolve_training_artifact_root(
        self,
        project_id: str,
        training_job_id: str,
        request: Mapping[str, Any],
    ) -> tuple[Path, Optional[Dict[str, Any]]]:
        raw_binding = request.get("artifact_root_binding")
        if raw_binding is None:
            root = self._artifact_root / project_id / training_job_id
            root.mkdir(parents=True, exist_ok=True)
            if _is_link_or_reparse(root):
                raise ProjectModelLifecycleError(
                    "artifact_root_unsafe",
                    "legacy project model artifact root is unsafe",
                )
            return root.resolve(), None
        binding = _normalize_model_artifact_root_binding(
            project_id,
            raw_binding,
        )
        if (
            request.get("artifact_root_binding_sha256")
            != _canonical_sha256(binding)
            or request.get("output_location_id")
            != binding["output_location_id"]
            or request.get("root_identity_sha256")
            != binding["root_identity_sha256"]
        ):
            raise ProjectModelLifecycleError(
                "artifact_root_binding_invalid",
                "persisted project model artifact-root binding changed",
            )
        resolver = self._artifact_root_resolver
        if resolver is None:
            raise ProjectModelLifecycleError(
                "artifact_root_unavailable",
                "project model artifact-root resolver is unavailable",
            )
        project_uuid = UUID(project_id)
        job_uuid = UUID(training_job_id)
        try:
            resolution = resolver(
                project_uuid,
                job_uuid,
                PROJECT_MODEL_ARTIFACT_CATEGORY,
                binding,
            )
        except ProjectModelLifecycleError:
            raise
        except Exception as error:
            raise ProjectModelLifecycleError(
                "artifact_root_unavailable",
                "project model artifact category could not be resolved",
            ) from error
        if not isinstance(resolution, ResolvedModelArtifactRoot):
            raise ProjectModelLifecycleError(
                "artifact_root_unavailable",
                "project model artifact-root resolver returned an invalid handle",
            )
        returned_binding = _normalize_model_artifact_root_binding(
            project_id,
            resolution.binding,
        )
        if canonical_json_bytes(returned_binding) != canonical_json_bytes(
            binding
        ):
            raise ProjectModelLifecycleError(
                "artifact_root_binding_changed",
                "project model artifact-root binding changed after training start",
            )
        root = _validated_model_category_root(
            resolution.root,
            training_job_id=training_job_id,
        )
        dataset_version_id = _canonical_uuid(
            request.get("dataset_version_id"),
            "dataset_version_id",
        )
        locator = {
            "schema_version": MODEL_ARTIFACT_LOCATOR_SCHEMA_VERSION,
            "mode": "external_category_root",
            "category": PROJECT_MODEL_ARTIFACT_CATEGORY,
            "project_id": project_id,
            "training_job_id": training_job_id,
            "dataset_version_id": dataset_version_id,
            "dataset_hash": _sha256(
                request.get("dataset_hash"),
                "dataset_hash",
            ),
            "output_location_id": binding["output_location_id"],
            "root_identity_sha256": binding[
                "root_identity_sha256"
            ],
            "root_binding": binding,
            "root_binding_sha256": _canonical_sha256(binding),
            "category_root_identity_sha256": (
                _model_category_identity(
                    root,
                    project_id=project_id,
                    training_job_id=training_job_id,
                    dataset_version_id=dataset_version_id,
                    binding=binding,
                )
            ),
        }
        return root, locator

    @staticmethod
    def _validate_model_artifact_locator(
        locator: Mapping[str, Any],
        *,
        project_id: str,
        training_job_id: str,
        dataset_version_id: str,
        dataset_hash: str,
        require_directory: bool,
    ) -> Dict[str, Any]:
        base_keys = {
            "schema_version",
            "mode",
            "category",
            "project_id",
            "training_job_id",
            "dataset_version_id",
            "dataset_hash",
            "output_location_id",
            "root_identity_sha256",
            "root_binding",
            "root_binding_sha256",
            "category_root_identity_sha256",
        }
        directory_keys = {"attempt_number", "artifact_directory"}
        keys = set(locator) if isinstance(locator, Mapping) else set()
        if (
            not isinstance(locator, Mapping)
            or keys not in (base_keys, base_keys | directory_keys)
            or (require_directory and keys != base_keys | directory_keys)
            or locator.get("schema_version")
            != MODEL_ARTIFACT_LOCATOR_SCHEMA_VERSION
            or locator.get("mode") != "external_category_root"
            or locator.get("category") != PROJECT_MODEL_ARTIFACT_CATEGORY
            or locator.get("project_id") != project_id
            or locator.get("training_job_id") != training_job_id
            or locator.get("dataset_version_id")
            != dataset_version_id
            or locator.get("dataset_hash") != dataset_hash
        ):
            raise ProjectModelLifecycleError(
                "artifact_root_binding_invalid",
                "project model artifact locator is invalid",
            )
        binding = _normalize_model_artifact_root_binding(
            project_id,
            locator.get("root_binding"),
        )
        if (
            locator.get("output_location_id")
            != binding["output_location_id"]
            or locator.get("root_identity_sha256")
            != binding["root_identity_sha256"]
            or locator.get("root_binding_sha256")
            != _canonical_sha256(binding)
            or not isinstance(
                locator.get("category_root_identity_sha256"), str
            )
            or _SHA256.fullmatch(
                str(locator.get("category_root_identity_sha256"))
            )
            is None
        ):
            raise ProjectModelLifecycleError(
                "artifact_root_binding_invalid",
                "project model artifact locator binding changed",
            )
        if directory_keys.issubset(keys):
            attempt = locator.get("attempt_number")
            relative = PurePosixPath(
                str(locator.get("artifact_directory", ""))
            )
            if (
                isinstance(attempt, bool)
                or not isinstance(attempt, int)
                or attempt < 1
                or relative.is_absolute()
                or relative.parts
                != (f"attempt-{attempt}", "model")
            ):
                raise ProjectModelLifecycleError(
                    "artifact_root_binding_invalid",
                    "project model artifact locator directory is invalid",
                )
        return {**dict(locator), "root_binding": binding}

    def _resolve_model_artifact_directory(
        self,
        model: Mapping[str, Any],
    ) -> tuple[Path, Optional[Dict[str, Any]]]:
        raw_locator = model.get("artifact_storage")
        if raw_locator is None:
            relative = PurePosixPath(
                str(model.get("artifact_directory", ""))
            )
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model artifact directory is unsafe",
                )
            directory = self._root.joinpath(*relative.parts)
            try:
                directory.relative_to(self._root)
            except ValueError as error:
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model artifact escaped the lifecycle root",
                ) from error
            return directory, None
        locator = self._validate_model_artifact_locator(
            raw_locator,
            project_id=_canonical_uuid(
                model.get("project_id"),
                "project_id",
            ),
            training_job_id=_canonical_uuid(
                model.get("training_job_id"),
                "training_job_id",
            ),
            dataset_version_id=_canonical_uuid(
                model.get("dataset_version_id"),
                "dataset_version_id",
            ),
            dataset_hash=_sha256(
                model.get("dataset_hash"),
                "dataset_hash",
            ),
            require_directory=True,
        )
        request_binding = locator["root_binding"]
        resolver = self._artifact_root_resolver
        if resolver is None:
            raise ProjectModelLifecycleError(
                "artifact_root_unavailable",
                "project model artifact-root resolver is unavailable",
            )
        try:
            resolved = resolver(
                UUID(locator["project_id"]),
                UUID(locator["training_job_id"]),
                PROJECT_MODEL_ARTIFACT_CATEGORY,
                request_binding,
            )
        except ProjectModelLifecycleError:
            raise
        except Exception as error:
            raise ProjectModelLifecycleError(
                "artifact_root_unavailable",
                "project model artifact category could not be rebound",
            ) from error
        if not isinstance(resolved, ResolvedModelArtifactRoot):
            raise ProjectModelLifecycleError(
                "artifact_root_unavailable",
                "project model artifact-root resolver returned an invalid handle",
            )
        returned_binding = _normalize_model_artifact_root_binding(
            locator["project_id"],
            resolved.binding,
        )
        if canonical_json_bytes(returned_binding) != canonical_json_bytes(
            request_binding
        ):
            raise ProjectModelLifecycleError(
                "artifact_root_binding_changed",
                "project model artifact-root binding changed after training",
            )
        root = _validated_model_category_root(
            resolved.root,
            training_job_id=locator["training_job_id"],
        )
        if _model_category_identity(
            root,
            project_id=locator["project_id"],
            training_job_id=locator["training_job_id"],
            dataset_version_id=locator["dataset_version_id"],
            binding=returned_binding,
        ) != locator["category_root_identity_sha256"]:
            raise ProjectModelLifecycleError(
                "artifact_root_binding_changed",
                "project model artifact category root changed after training",
            )
        relative = PurePosixPath(locator["artifact_directory"])
        directory = root.joinpath(*relative.parts)
        try:
            directory.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as error:
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact escaped its bound category root",
            ) from error
        if model.get("artifact_directory") != relative.as_posix():
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact locator no longer matches its record",
            )
        return directory, locator

    def _cancel_requested(
        self,
        project_id: str,
        job_id: str,
        cancel_event: threading.Event,
    ) -> bool:
        if cancel_event.is_set() or self._stop_event.is_set():
            return True
        with self._lock, self._connect() as connection:
            row = self._get_job_row(connection, project_id, job_id)
            job = self._decode_integrity_row(row, label="training job")
        requested = job.get("cancel_requested") is True
        if requested:
            cancel_event.set()
        return requested

    def _persist_progress(
        self,
        project_id: str,
        job_id: str,
        attempt: int,
        value: Mapping[str, Any],
    ) -> None:
        progress = {
            **_canonical_training_progress(value),
            "attempt_number": attempt,
            "updated_at": _utc_now(),
        }
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_job_row(connection, project_id, job_id)
            job = self._decode_integrity_row(row, label="training job")
            if (
                job["status"] != "running"
                or int(job["attempt_count"]) != attempt
            ):
                connection.execute("COMMIT")
                return
            job.update(
                {
                    "progress": progress,
                    "updated_at": progress["updated_at"],
                }
            )
            self._write_payload(
                connection,
                "training_jobs",
                job_id,
                job,
            )
            connection.execute("COMMIT")

    def _persist_checkpoint(
        self,
        project_id: str,
        job_id: str,
        attempt: int,
        *,
        category_root: Path,
        staging: Path,
        checkpoint_path: Path,
    ) -> Optional[Dict[str, Any]]:
        try:
            staging_root = staging.resolve(strict=True)
            category = category_root.resolve(strict=True)
            resolved = checkpoint_path.resolve(strict=True)
            resolved.relative_to(staging_root)
            relative = resolved.relative_to(category)
        except (OSError, ValueError) as error:
            raise ProjectModelLifecycleError(
                "training_checkpoint_invalid",
                "training checkpoint escaped its attempt staging root",
            ) from error
        try:
            checkpoint_stat = resolved.stat()
        except OSError as error:
            raise ProjectModelLifecycleError(
                "training_checkpoint_invalid",
                "training checkpoint could not be inspected",
            ) from error
        relative_text = PurePosixPath(relative.as_posix()).as_posix()
        with self._lock, self._connect() as connection:
            row = self._get_job_row(connection, project_id, job_id)
            current_job = self._decode_integrity_row(
                row,
                label="training job",
            )
        current_checkpoint = current_job.get("checkpoint")
        if (
            current_job["status"] == "running"
            and int(current_job["attempt_count"]) == attempt
            and isinstance(current_checkpoint, Mapping)
            and current_checkpoint.get("relative_path") == relative_text
            and current_checkpoint.get("size_bytes")
            == checkpoint_stat.st_size
            and current_checkpoint.get("modified_time_ns")
            == str(checkpoint_stat.st_mtime_ns)
        ):
            return dict(current_checkpoint)
        digest, size = _hash_checkpoint_file(resolved)
        timestamp = _utc_now()
        descriptor = {
            "schema_version": TRAINING_CHECKPOINT_SCHEMA_VERSION,
            "attempt_number": attempt,
            "relative_path": relative_text,
            "sha256": digest,
            "size_bytes": size,
            "modified_time_ns": str(checkpoint_stat.st_mtime_ns),
            "request_manifest_sha256": None,
            "created_at": timestamp,
        }
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_job_row(connection, project_id, job_id)
            job = self._decode_integrity_row(row, label="training job")
            if (
                job["status"] != "running"
                or int(job["attempt_count"]) != attempt
            ):
                connection.execute("COMMIT")
                return None
            descriptor["request_manifest_sha256"] = job[
                "request_sha256"
            ]
            job.update(
                {
                    "checkpoint": descriptor,
                    "updated_at": timestamp,
                }
            )
            self._write_payload(
                connection,
                "training_jobs",
                job_id,
                job,
            )
            connection.execute("COMMIT")
        return descriptor

    def _capture_known_checkpoint(
        self,
        project_id: str,
        job_id: str,
        attempt: int,
        *,
        category_root: Path,
        staging: Path,
    ) -> Optional[Dict[str, Any]]:
        checkpoint = staging / "checkpoint.pt"
        if not checkpoint.is_file():
            return None
        return self._persist_checkpoint(
            project_id,
            job_id,
            attempt,
            category_root=category_root,
            staging=staging,
            checkpoint_path=checkpoint,
        )

    def _resolve_resume_checkpoint(
        self,
        *,
        category_root: Path,
        job: Mapping[str, Any],
    ) -> Tuple[Optional[Path], Optional[str]]:
        raw = job.get("checkpoint")
        if raw is None:
            return None, None
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema_version")
            != TRAINING_CHECKPOINT_SCHEMA_VERSION
            or raw.get("request_manifest_sha256")
            != job["request_sha256"]
            or isinstance(raw.get("attempt_number"), bool)
            or not isinstance(raw.get("attempt_number"), int)
            or raw["attempt_number"] < 1
            or raw["attempt_number"] > int(job["attempt_count"])
            or isinstance(raw.get("size_bytes"), bool)
            or not isinstance(raw.get("size_bytes"), int)
            or raw["size_bytes"] <= 0
            or not isinstance(raw.get("modified_time_ns"), str)
            or not raw["modified_time_ns"].isdigit()
            or len(raw["modified_time_ns"]) > 32
            or _SHA256.fullmatch(str(raw.get("sha256", ""))) is None
        ):
            raise ProjectModelLifecycleError(
                "training_checkpoint_invalid",
                "persisted training checkpoint metadata is invalid",
            )
        relative = PurePosixPath(str(raw.get("relative_path", "")))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ProjectModelLifecycleError(
                "training_checkpoint_invalid",
                "persisted training checkpoint path is invalid",
            )
        root = category_root.resolve(strict=True)
        candidate = root.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise ProjectModelLifecycleError(
                "training_checkpoint_unavailable",
                "persisted training checkpoint is unavailable",
            ) from error
        digest, size = _hash_checkpoint_file(resolved)
        if (
            digest != raw["sha256"]
            or size != raw["size_bytes"]
            or str(resolved.stat().st_mtime_ns)
            != raw["modified_time_ns"]
        ):
            raise ProjectModelLifecycleError(
                "training_checkpoint_invalid",
                "persisted training checkpoint changed",
            )
        return resolved, digest

    def _execute_job(
        self,
        project_id: str,
        job_id: str,
        *,
        cancel_event: threading.Event,
    ) -> Dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_job_row(connection, project_id, job_id)
            job = self._decode_integrity_row(row, label="training job")
            if job["status"] != "queued":
                connection.execute("COMMIT")
                return {**job, "idempotent_replay": True}
            if job.get("cancel_requested") is True:
                job.update(
                    {
                        "status": "cancelled",
                        "error": {
                            "code": "training_cancelled",
                            "message": "training was cancelled by the user",
                        },
                        "updated_at": _utc_now(),
                    }
                )
                self._write_payload(
                    connection,
                    "training_jobs",
                    job_id,
                    job,
                )
                connection.execute("COMMIT")
                return {**job, "idempotent_replay": False}
            if not self._trainer.available:
                job.update(
                    {
                        "status": "blocked",
                        "blockers": [
                            {
                                "code": "trainer_unavailable",
                                "trainer_id": self._trainer.trainer_id,
                            }
                        ],
                        "error": None,
                        "updated_at": _utc_now(),
                    }
                )
                self._write_payload(
                    connection,
                    "training_jobs",
                    job_id,
                    job,
                )
                connection.execute("COMMIT")
                return {**job, "idempotent_replay": False}
            if (
                job["request"]["trainer"]["trainer_id"]
                != self._trainer.trainer_id
                or job["request"]["trainer"]["trainer_version"]
                != self._trainer.trainer_version
            ):
                connection.execute("ROLLBACK")
                raise ProjectModelLifecycleError(
                    "trainer_identity_changed",
                    "the persisted training job requires its original trainer",
                )
            attempt = int(job["attempt_count"]) + 1
            try:
                category_root, root_locator = (
                    self._resolve_training_artifact_root(
                        project_id,
                        job_id,
                        job["request"],
                    )
                )
            except ProjectModelLifecycleError as error:
                connection.execute("ROLLBACK")
                return self._fail_job(
                    project_id,
                    job_id,
                    error.code,
                    str(error),
                )
            job.update(
                {
                    "status": "running",
                    "attempt_count": attempt,
                    "blockers": [],
                    "error": None,
                    "artifact_storage": root_locator,
                    "progress": {
                        "stage": "starting",
                        "attempt_number": attempt,
                        "updated_at": _utc_now(),
                    },
                    "updated_at": _utc_now(),
                }
            )
            self._write_payload(
                connection,
                "training_jobs",
                job_id,
                job,
            )
            connection.execute("COMMIT")

        try:
            resume_checkpoint_path, resume_checkpoint_sha256 = (
                self._resolve_resume_checkpoint(
                    category_root=category_root,
                    job=job,
                )
            )
        except ProjectModelLifecycleError as error:
            return self._fail_job(
                project_id,
                job_id,
                error.code,
                str(error),
            )
        attempt_root = category_root / f"attempt-{attempt}"
        staging = attempt_root / "staging"
        try:
            staging.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            return self._fail_job(
                project_id,
                job_id,
                "training_attempt_conflict",
                (
                    "training attempt staging directory could not be "
                    "created"
                ),
            )
        request = job["request"]

        def progress_callback(value: Mapping[str, Any]) -> None:
            self._persist_progress(
                project_id,
                job_id,
                attempt,
                value,
            )
            self._capture_known_checkpoint(
                project_id,
                job_id,
                attempt,
                category_root=category_root,
                staging=staging,
            )

        def checkpoint_callback(path: Path) -> None:
            self._persist_checkpoint(
                project_id,
                job_id,
                attempt,
                category_root=category_root,
                staging=staging,
                checkpoint_path=Path(path),
            )

        invocation = TrainerInvocation(
            schema_version=TRAINER_CONTRACT_VERSION,
            project_id=project_id,
            training_job_id=job_id,
            request_manifest_sha256=job["request_sha256"],
            dataset_version_id=request["dataset_version_id"],
            dataset_hash=request["dataset_hash"],
            base_model_version=request["base_model_version"],
            required_issue_types=tuple(request["required_issue_types"]),
            current_training_annotations=tuple(
                request["current_training_annotations"]
            ),
            fixed_validation_annotations=tuple(
                request["fixed_validation_annotations"]
            ),
            auxiliary_reference_seeds=tuple(
                request["auxiliary_reference_seeds"]
            ),
            fixed_validation_truth=tuple(
                request["fixed_validation_truth"]
            ),
            fixed_validation_sha256=request[
                "fixed_validation_sha256"
            ],
            fixed_validation_membership_sha256=request[
                "fixed_validation_membership_sha256"
            ],
            fixed_validation_set_version=int(
                request.get("fixed_validation_set_version", 1)
            ),
            output_directory=staging,
            progress_callback=progress_callback,
            cancel_callback=lambda: self._cancel_requested(
                project_id,
                job_id,
                cancel_event,
            ),
            checkpoint_callback=checkpoint_callback,
            resume_checkpoint_path=resume_checkpoint_path,
            resume_checkpoint_sha256=resume_checkpoint_sha256,
        )
        try:
            result = self._trainer.train(invocation)
            if self._cancel_requested(
                project_id,
                job_id,
                cancel_event,
            ):
                self._capture_known_checkpoint(
                    project_id,
                    job_id,
                    attempt,
                    category_root=category_root,
                    staging=staging,
                )
                return self._interrupt_job(
                    project_id,
                    job_id,
                    code="process_shutdown",
                    message="training was interrupted by process shutdown",
                )
            model = self._finalize_candidate(
                job=job,
                invocation=invocation,
                result=result,
                staging=staging,
                attempt_root=attempt_root,
            )
        except ProjectModelLifecycleError as error:
            self._capture_known_checkpoint(
                project_id,
                job_id,
                attempt,
                category_root=category_root,
                staging=staging,
            )
            if self._cancel_requested(
                project_id,
                job_id,
                cancel_event,
            ):
                return self._interrupt_job(
                    project_id,
                    job_id,
                    code="process_shutdown",
                    message="training was interrupted by process shutdown",
                )
            return self._fail_job(
                project_id,
                job_id,
                error.code,
                str(error),
            )
        except Exception as error:
            self._capture_known_checkpoint(
                project_id,
                job_id,
                attempt,
                category_root=category_root,
                staging=staging,
            )
            if self._cancel_requested(
                project_id,
                job_id,
                cancel_event,
            ):
                return self._interrupt_job(
                    project_id,
                    job_id,
                    code="process_shutdown",
                    message="training was interrupted by process shutdown",
                )
            return self._fail_job(
                project_id,
                job_id,
                "trainer_failed",
                str(error)[:1000] or type(error).__name__,
            )
        except BaseException:
            self._capture_known_checkpoint(
                project_id,
                job_id,
                attempt,
                category_root=category_root,
                staging=staging,
            )
            raise

        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    latest_job_row = self._get_job_row(
                        connection,
                        project_id,
                        job_id,
                    )
                    latest_job = self._decode_integrity_row(
                        latest_job_row,
                        label="training job",
                    )
                    if latest_job.get("cancel_requested") is True:
                        raise ProjectModelLifecycleError(
                            "training_cancelled",
                            "training was cancelled by the user",
                        )
                    state = self._get_state(connection, project_id)
                    if state is None:
                        raise ProjectModelLifecycleError(
                            "lifecycle_store_corrupt",
                            "project model state is missing",
                        )
                    if state["candidate_model_id"] is not None:
                        raise ProjectModelLifecycleError(
                            "candidate_review_required",
                            "another candidate is awaiting explicit review",
                        )
                    timestamp = _utc_now()
                    validation_eligible = (
                        model["validation_decision"] == "eligible"
                    )
                    model_state = (
                        "candidate"
                        if validation_eligible
                        else "rejected"
                    )
                    model_payload = {
                        **model,
                        "state": model_state,
                        "automatic_promotion": False,
                        "automatic_rejection": (
                            not validation_eligible
                        ),
                        "user_promotion_required": validation_eligible,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    }
                    model_text = canonical_json_bytes(model_payload).decode(
                        "utf-8"
                    )
                    connection.execute(
                        """
                        INSERT INTO model_versions(
                            id, project_id, training_job_id, model_version,
                            state, payload_json, payload_sha256,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            model_payload["model_version_id"],
                            project_id,
                            job_id,
                            model_payload["model_version"],
                            model_state,
                            model_text,
                            hashlib.sha256(
                                model_text.encode("utf-8")
                            ).hexdigest(),
                            timestamp,
                            timestamp,
                        ),
                    )
                    state.update(
                        {
                            "revision": int(state["revision"]) + 1,
                            "candidate_model_id": (
                                model_payload["model_version_id"]
                                if validation_eligible
                                else None
                            ),
                            "candidate_model_version": (
                                model_payload["model_version"]
                                if validation_eligible
                                else None
                            ),
                            "updated_at": timestamp,
                        }
                    )
                    self._write_payload(
                        connection,
                        "project_model_states",
                        project_id,
                        state,
                    )
                    job_row = self._get_job_row(
                        connection,
                        project_id,
                        job_id,
                    )
                    updated_job = self._decode_integrity_row(
                        job_row,
                        label="training job",
                    )
                    updated_job.update(
                        {
                            "status": "succeeded",
                            "model_version_id": model_payload[
                                "model_version_id"
                            ],
                            "artifact_storage": model_payload.get(
                                "artifact_storage"
                            ),
                            "primary_artifact_sha256": model_payload[
                                "primary_artifact_sha256"
                            ],
                            "artifact_manifest_sha256": model_payload[
                                "artifact_manifest_sha256"
                            ],
                            "progress": {
                                "stage": "completed",
                                "attempt_number": attempt,
                                "updated_at": timestamp,
                            },
                            "checkpoint": None,
                            "error": None,
                            "updated_at": timestamp,
                        }
                    )
                    self._write_payload(
                        connection,
                        "training_jobs",
                        job_id,
                        updated_job,
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
        except ProjectModelLifecycleError as error:
            if error.code == "training_cancelled":
                return self._interrupt_job(
                    project_id,
                    job_id,
                    code="training_cancelled",
                    message=str(error),
                )
            return self._fail_job(
                project_id,
                job_id,
                error.code,
                str(error),
            )
        except Exception as error:
            return self._fail_job(
                project_id,
                job_id,
                "candidate_persistence_failed",
                str(error)[:1000] or type(error).__name__,
            )
        return {**updated_job, "idempotent_replay": False}

    def _finalize_candidate(
        self,
        *,
        job: Mapping[str, Any],
        invocation: TrainerInvocation,
        result: TrainerResult,
        staging: Path,
        attempt_root: Path,
    ) -> Dict[str, Any]:
        if not isinstance(result, TrainerResult):
            raise ProjectModelLifecycleError(
                "trainer_contract_violation",
                "trainer returned an unsupported result",
            )
        model_version = _canonical_identifier(
            result.model_version,
            "candidate_model_version",
        )
        if model_version == invocation.base_model_version:
            raise ProjectModelLifecycleError(
                "trainer_contract_violation",
                "candidate model version must differ from active model",
            )
        if result.model_format not in {
            VISUAL_CENTROID_MODEL_FORMAT,
            LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
        }:
            raise ProjectModelLifecycleError(
                "trainer_contract_violation",
                "trainer returned an unsupported project-model format",
            )
        try:
            metadata_bytes = canonical_json_bytes(result.metadata)
        except Exception as error:
            raise ProjectModelLifecycleError(
                "trainer_contract_violation",
                "trainer metadata is not canonical JSON",
            ) from error
        if len(metadata_bytes) > 64 * 1024:
            raise ProjectModelLifecycleError(
                "trainer_contract_violation",
                "trainer metadata is too large",
            )
        execution_provenance: Optional[Dict[str, Any]] = None
        training_runtime_provenance_sha256: Optional[str] = None
        detector_model_pack_identity: Optional[Dict[str, Any]] = None
        detector_model_pack_identity_sha256: Optional[str] = None
        if not self._trainer.test_only:
            execution = result.metadata.get("execution_provenance")
            if (
                not isinstance(execution, Mapping)
                or execution.get("accelerator") != "cuda"
                or execution.get("accelerator_used") is not True
                or not str(
                    execution.get("resolved_device", "")
                ).startswith("cuda")
            ):
                raise ProjectModelLifecycleError(
                    "training_runtime_provenance_invalid",
                    (
                        "production training did not prove CUDA execution; "
                        "CPU fallback is forbidden"
                    ),
                )
            try:
                execution_provenance = json.loads(
                    canonical_json_bytes(execution).decode("utf-8")
                )
            except Exception as error:
                raise ProjectModelLifecycleError(
                    "training_runtime_provenance_invalid",
                    "production CUDA provenance is not canonical JSON",
                ) from error
            training_runtime_provenance_sha256 = _canonical_sha256(
                execution_provenance
            )
            if result.model_format == LOCAL_OBJECT_DETECTOR_MODEL_FORMAT:
                detector_model_pack_identity = (
                    _canonical_detector_model_pack_identity(
                        result.metadata.get("model_pack_identity")
                    )
                )
                detector_model_pack_identity_sha256 = (
                    _canonical_sha256(detector_model_pack_identity)
                )
                execution_pack_hash = execution_provenance.get(
                    "model_pack_manifest_sha256"
                )
                if (
                    execution_pack_hash is not None
                    and execution_pack_hash
                    != detector_model_pack_identity["manifest_sha256"]
                ):
                    raise ProjectModelLifecycleError(
                        "model_pack_provenance_invalid",
                        (
                            "CUDA execution used a different detector "
                            "Model Pack manifest"
                        ),
                    )
        primary_relative = PurePosixPath(
            result.primary_artifact_relative_path
        )
        if (
            primary_relative.is_absolute()
            or not primary_relative.parts
            or any(part in {"", ".", ".."} for part in primary_relative.parts)
        ):
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "primary artifact path is unsafe",
            )
        primary = staging.joinpath(*primary_relative.parts)
        try:
            primary.relative_to(staging)
        except ValueError as error:
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "primary artifact escaped the staging directory",
            ) from error
        if (
            not primary.is_file()
            or _is_link_or_reparse(primary)
            or primary.stat().st_size <= 0
        ):
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "trainer did not create a real primary model artifact",
            )

        files = []
        total_size = 0
        for path in sorted(
            staging.rglob("*"),
            key=lambda item: item.relative_to(staging).as_posix(),
        ):
            if _is_link_or_reparse(path):
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "model artifacts cannot contain links or reparse points",
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "model artifact contains an unsupported entry",
                )
            relative = path.relative_to(staging).as_posix()
            size = path.stat().st_size
            if size <= 0 or size > MAX_ARTIFACT_FILE_BYTES:
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "model artifact file size is invalid",
                )
            total_size += size
            if total_size > MAX_ARTIFACT_TOTAL_BYTES:
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "model artifact set is too large",
                )
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            files.append(
                {
                    "path": relative,
                    "size_bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )
            if len(files) > MAX_ARTIFACT_FILES:
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "model artifact set contains too many files",
                )
        if not files:
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "trainer produced no model files",
            )
        primary_record = next(
            (
                item
                for item in files
                if item["path"] == primary_relative.as_posix()
            ),
            None,
        )
        if primary_record is None:
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "primary model artifact was not included in the manifest",
            )

        try:
            decision = compare_candidate_to_active(
                active_model_version=invocation.base_model_version,
                candidate_model_version=model_version,
                truth_records=invocation.fixed_validation_truth,
                active_prediction_records=(
                    result.active_prediction_records
                ),
                candidate_prediction_records=(
                    result.candidate_prediction_records
                ),
                required_issue_types=invocation.required_issue_types,
                minimum_samples_per_type=(
                    DEFAULT_MINIMUM_SAMPLES_PER_TYPE
                ),
                maximum_recall_drop=DEFAULT_MAX_RECALL_DROP,
                minimum_candidate_recall=(
                    DEFAULT_MINIMUM_CANDIDATE_RECALL
                ),
                minimum_candidate_precision=(
                    DEFAULT_MINIMUM_CANDIDATE_PRECISION
                ),
            )
        except ValidationMetricsError as error:
            raise ProjectModelLifecycleError(
                "fixed_validation_failed",
                str(error),
            ) from error
        decision_value = decision.model_dump(mode="json")
        if (
            decision.validation_set_sha256
            != invocation.fixed_validation_sha256
        ):
            raise ProjectModelLifecycleError(
                "fixed_validation_changed",
                "trainer evaluation did not use the fixed validation set",
            )
        training_analysis_job_ids = tuple(
            sorted(
                {
                    _canonical_uuid(
                        annotation.get("analysis_job_id"),
                        "training.analysis_job_id",
                    )
                    for annotation in (
                        invocation.current_training_annotations
                    )
                }
            )
        )
        fixed_validation_analysis_job_ids = tuple(
            sorted(
                {
                    _canonical_uuid(
                        annotation.get("analysis_job_id"),
                        "validation.analysis_job_id",
                    )
                    for annotation in (
                        invocation.fixed_validation_annotations
                    )
                }
            )
        )
        if (
            not training_analysis_job_ids
            or not fixed_validation_analysis_job_ids
            or set(training_analysis_job_ids).intersection(
                fixed_validation_analysis_job_ids
            )
        ):
            raise ProjectModelLifecycleError(
                "source_analysis_lineage_invalid",
                (
                    "training and fixed-validation source analyses must "
                    "be non-empty and disjoint"
                ),
            )
        source_analysis_lineage = {
            "training_analysis_job_ids": list(
                training_analysis_job_ids
            ),
            "fixed_validation_analysis_job_ids": list(
                fixed_validation_analysis_job_ids
            ),
        }
        source_analysis_lineage_sha256 = _canonical_sha256(
            source_analysis_lineage
        )
        request = job.get("request")
        raw_dataset_provenance = (
            request.get("dataset_provenance_graph")
            if isinstance(request, Mapping)
            else None
        )
        dataset_provenance: Optional[Dict[str, Any]] = None
        dataset_provenance_sha256: Optional[str] = None
        if raw_dataset_provenance is not None:
            try:
                dataset_provenance = (
                    canonical_dataset_provenance_graph(
                        raw_dataset_provenance
                    )
                )
            except TrainingProvenanceError as error:
                raise ProjectModelLifecycleError(
                    "dataset_provenance_invalid",
                    "training request provenance is invalid",
                ) from error
            dataset_provenance_sha256 = dataset_provenance[
                "provenance_graph_sha256"
            ]
            graph_training_jobs = sorted(
                {
                    member["analysis_job_id"]
                    for member in dataset_provenance["members"]
                    if member["split"] == "train"
                }
            )
            graph_validation_jobs = sorted(
                {
                    member["analysis_job_id"]
                    for member in dataset_provenance["members"]
                    if member["split"] == "validation"
                }
            )
            if (
                dataset_provenance["project_id"]
                != invocation.project_id
                or request.get(
                    "dataset_provenance_graph_sha256"
                )
                != dataset_provenance_sha256
                or graph_training_jobs
                != list(training_analysis_job_ids)
                or graph_validation_jobs
                != list(fixed_validation_analysis_job_ids)
            ):
                raise ProjectModelLifecycleError(
                    "source_analysis_lineage_invalid",
                    (
                        "model provenance does not match the exact "
                        "training and fixed-validation source analyses"
                    ),
                )
        model_id = str(
            uuid5(
                UUID(invocation.project_id),
                (
                    f"{MODEL_VERSION_SCHEMA_VERSION}:"
                    f"{job['request_sha256']}:{model_version}"
                ),
            )
        )
        raw_storage = job.get("artifact_storage")
        artifact_storage: Optional[Dict[str, Any]]
        planned_final_directory = attempt_root / "model"
        if raw_storage is None:
            artifact_storage = None
        else:
            artifact_storage = self._validate_model_artifact_locator(
                raw_storage,
                project_id=invocation.project_id,
                training_job_id=invocation.training_job_id,
                dataset_version_id=invocation.dataset_version_id,
                dataset_hash=invocation.dataset_hash,
                require_directory=False,
            )
            artifact_storage = {
                **artifact_storage,
                "attempt_number": int(job["attempt_count"]),
                "artifact_directory": (
                    planned_final_directory.relative_to(
                        attempt_root.parent
                    ).as_posix()
                ),
            }
        artifact_manifest = {
            "schema_version": MODEL_ARTIFACT_SCHEMA_VERSION,
            "trainer_contract_version": TRAINER_CONTRACT_VERSION,
            "project_id": invocation.project_id,
            "training_job_id": invocation.training_job_id,
            "model_version_id": model_id,
            "model_version": model_version,
            "model_format": result.model_format,
            "base_model_version": invocation.base_model_version,
            "dataset_version_id": invocation.dataset_version_id,
            "dataset_hash": invocation.dataset_hash,
            "request_manifest_sha256": (
                invocation.request_manifest_sha256
            ),
            "fixed_validation_sha256": (
                invocation.fixed_validation_sha256
            ),
            "fixed_validation_membership_sha256": (
                invocation.fixed_validation_membership_sha256
            ),
            "fixed_validation_set_version": (
                invocation.fixed_validation_set_version
            ),
            "source_analysis_lineage": source_analysis_lineage,
            "source_analysis_lineage_sha256": (
                source_analysis_lineage_sha256
            ),
            "primary_artifact": primary_relative.as_posix(),
            "files": files,
            "total_size_bytes": total_size,
            "trainer": {
                "trainer_id": self._trainer.trainer_id,
                "trainer_version": self._trainer.trainer_version,
                "test_only": self._trainer.test_only,
            },
            "current_training_member_count": len(
                invocation.current_training_annotations
            ),
            "auxiliary_reference_count": len(
                invocation.auxiliary_reference_seeds
            ),
            "auxiliary_reference_manifest_sha256": _canonical_sha256(
                list(invocation.auxiliary_reference_seeds)
            ),
            "auxiliary_reference_provenance": list(
                invocation.auxiliary_reference_seeds
            ),
            "approved_references_count_toward_training_gate": False,
            "auxiliary_references_count_toward_validation": False,
            "required_issue_types": list(
                invocation.required_issue_types
            ),
            "promotion_review": decision_value,
            "automatic_promotion": False,
            "metadata": result.metadata,
        }
        if execution_provenance is not None:
            artifact_manifest["training_runtime_provenance"] = (
                execution_provenance
            )
            artifact_manifest[
                "training_runtime_provenance_sha256"
            ] = training_runtime_provenance_sha256
        if detector_model_pack_identity is not None:
            artifact_manifest["model_pack_identity"] = (
                detector_model_pack_identity
            )
            artifact_manifest["model_pack_identity_sha256"] = (
                detector_model_pack_identity_sha256
            )
        if artifact_storage is not None:
            artifact_manifest["artifact_storage"] = artifact_storage
        if dataset_provenance is not None:
            artifact_manifest["dataset_provenance_graph"] = (
                dataset_provenance
            )
            artifact_manifest[
                "dataset_provenance_graph_sha256"
            ] = dataset_provenance_sha256
        manifest_bytes = canonical_json_bytes(artifact_manifest)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        temporary_manifest = staging / ".artifact-manifest.tmp"
        temporary_manifest.write_bytes(manifest_bytes)
        os.replace(
            str(temporary_manifest),
            str(staging / "artifact-manifest.json"),
        )
        final_directory = planned_final_directory
        os.replace(str(staging), str(final_directory))
        final_primary = final_directory.joinpath(*primary_relative.parts)
        final_digest = hashlib.sha256(final_primary.read_bytes()).hexdigest()
        if final_digest != primary_record["sha256"]:
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "model artifact changed during atomic finalization",
            )
        relative_directory = (
            final_directory.relative_to(self._root).as_posix()
            if artifact_storage is None
            else final_directory.relative_to(attempt_root.parent).as_posix()
        )
        model = {
            "schema_version": MODEL_VERSION_SCHEMA_VERSION,
            "model_version_id": model_id,
            "project_id": invocation.project_id,
            "training_job_id": invocation.training_job_id,
            "model_version": model_version,
            "model_format": result.model_format,
            "base_model_version": invocation.base_model_version,
            "dataset_version_id": invocation.dataset_version_id,
            "dataset_hash": invocation.dataset_hash,
            "fixed_validation_sha256": (
                invocation.fixed_validation_sha256
            ),
            "fixed_validation_membership_sha256": (
                invocation.fixed_validation_membership_sha256
            ),
            "fixed_validation_set_version": (
                invocation.fixed_validation_set_version
            ),
            "training_analysis_job_ids": list(
                training_analysis_job_ids
            ),
            "fixed_validation_analysis_job_ids": list(
                fixed_validation_analysis_job_ids
            ),
            "source_analysis_lineage_sha256": (
                source_analysis_lineage_sha256
            ),
            "artifact_directory": relative_directory,
            "artifact_storage": artifact_storage,
            "primary_artifact_relative_path": (
                primary_relative.as_posix()
            ),
            "primary_artifact_sha256": primary_record["sha256"],
            "artifact_manifest_sha256": manifest_sha256,
            "artifact_file_count": len(files),
            "artifact_total_size_bytes": total_size,
            "trainer_id": self._trainer.trainer_id,
            "trainer_version": self._trainer.trainer_version,
            "trainer_test_only": self._trainer.test_only,
            "promotion_review": decision_value,
            "validation_decision": decision.decision,
            "auxiliary_reference_count": len(
                invocation.auxiliary_reference_seeds
            ),
            "auxiliary_reference_manifest_sha256": _canonical_sha256(
                list(invocation.auxiliary_reference_seeds)
            ),
        }
        if execution_provenance is not None:
            model["training_runtime_provenance_sha256"] = (
                training_runtime_provenance_sha256
            )
        if detector_model_pack_identity is not None:
            model["model_pack_identity"] = (
                detector_model_pack_identity
            )
            model["model_pack_identity_sha256"] = (
                detector_model_pack_identity_sha256
            )
        if dataset_provenance is not None:
            model["dataset_provenance_graph"] = dataset_provenance
            model["dataset_provenance_graph_sha256"] = (
                dataset_provenance_sha256
            )
        return model

    def _fail_job(
        self,
        project_id: str,
        job_id: str,
        code: str,
        message: str,
    ) -> Dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_job_row(connection, project_id, job_id)
            job = self._decode_integrity_row(row, label="training job")
            cancelled = job.get("cancel_requested") is True
            job.update(
                {
                    "status": "cancelled" if cancelled else "failed",
                    "error": {
                        "code": (
                            "training_cancelled" if cancelled else code
                        ),
                        "message": (
                            "training was cancelled by the user"
                            if cancelled
                            else message[:1000]
                        ),
                    },
                    "updated_at": _utc_now(),
                }
            )
            self._write_payload(
                connection,
                "training_jobs",
                job_id,
                job,
            )
            connection.execute("COMMIT")
        return {**job, "idempotent_replay": False}

    def resume_training(
        self,
        project_id: UUID,
        training_job_id: UUID,
        *,
        user_confirmed: bool,
    ) -> Dict[str, Any]:
        if user_confirmed is not True:
            raise ProjectModelLifecycleError(
                "training_resume_confirmation_required",
                "explicit user confirmation is required to resume training",
            )
        project_text = _canonical_uuid(project_id, "project_id")
        job_id = _canonical_uuid(training_job_id, "training_job_id")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_job_row(connection, project_text, job_id)
            job = self._decode_integrity_row(row, label="training job")
            if job["status"] not in {
                "interrupted",
                "failed",
                "cancelled",
            }:
                connection.execute("ROLLBACK")
                raise ProjectModelLifecycleError(
                    "training_not_resumable",
                    (
                        "only interrupted, failed, or cancelled training "
                        "can be resumed"
                    ),
                )
            timestamp = _utc_now()
            job.update(
                {
                    "status": "queued",
                    "blockers": [],
                    "error": None,
                    "cancel_requested": False,
                    "resume_count": int(job.get("resume_count", 0)) + 1,
                    "progress": {
                        "stage": "queued",
                        "attempt_number": int(job["attempt_count"]),
                        "updated_at": timestamp,
                    },
                    "updated_at": timestamp,
                }
            )
            self._write_payload(
                connection,
                "training_jobs",
                job_id,
                job,
            )
            connection.execute("COMMIT")
        self._wake_worker()
        return {**job, "idempotent_replay": False}

    def cancel_training(
        self,
        project_id: UUID,
        training_job_id: UUID,
        *,
        user_confirmed: bool,
    ) -> Dict[str, Any]:
        if user_confirmed is not True:
            raise ProjectModelLifecycleError(
                "training_cancellation_confirmation_required",
                "explicit user confirmation is required to cancel training",
            )
        project_text = _canonical_uuid(project_id, "project_id")
        job_id = _canonical_uuid(training_job_id, "training_job_id")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._get_job_row(connection, project_text, job_id)
            job = self._decode_integrity_row(row, label="training job")
            if job["status"] == "cancelled":
                connection.execute("COMMIT")
                return {**job, "idempotent_replay": True}
            if job["status"] not in {"queued", "running"}:
                connection.execute("ROLLBACK")
                raise ProjectModelLifecycleError(
                    "training_not_cancellable",
                    "only queued or running training can be cancelled",
                )
            timestamp = _utc_now()
            was_queued = job["status"] == "queued"
            job.update(
                {
                    "status": (
                        "cancelled" if was_queued else "running"
                    ),
                    "cancel_requested": True,
                    "error": (
                        {
                            "code": "training_cancelled",
                            "message": "training was cancelled by the user",
                        }
                        if was_queued
                        else None
                    ),
                    "progress": (
                        {
                            "stage": "cancelled",
                            "attempt_number": int(job["attempt_count"]),
                            "updated_at": timestamp,
                        }
                        if was_queued
                        else job.get("progress")
                    ),
                    "updated_at": timestamp,
                }
            )
            self._write_payload(
                connection,
                "training_jobs",
                job_id,
                job,
            )
            connection.execute("COMMIT")
            active_cancel = (
                self._active_cancel_event
                if self._active_job_id == job_id
                else None
            )
        if active_cancel is not None:
            active_cancel.set()
        self._work_event.set()
        return {**job, "idempotent_replay": False}

    def get_training_job(
        self,
        project_id: UUID,
        training_job_id: UUID,
    ) -> Dict[str, Any]:
        project_text = _canonical_uuid(project_id, "project_id")
        job_id = _canonical_uuid(training_job_id, "training_job_id")
        with self._lock, self._connect() as connection:
            row = self._get_job_row(connection, project_text, job_id)
            return self._decode_integrity_row(row, label="training job")

    def list_training_jobs(self, project_id: UUID) -> Dict[str, Any]:
        project_text = _canonical_uuid(project_id, "project_id")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM training_jobs
                WHERE project_id = ?
                ORDER BY created_at, id
                """,
                (project_text,),
            ).fetchall()
        jobs = [
            self._decode_integrity_row(row, label="training job")
            for row in rows
        ]
        return {
            "schema_version": "trafficvisson.training-job-list.v1",
            "project_id": project_text,
            "count": len(jobs),
            "jobs": jobs,
        }

    def list_model_versions(self, project_id: UUID) -> Dict[str, Any]:
        project_text = _canonical_uuid(project_id, "project_id")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM model_versions
                WHERE project_id = ?
                ORDER BY created_at, id
                """,
                (project_text,),
            ).fetchall()
        models = [
            self._decode_integrity_row(row, label="model version")
            for row in rows
        ]
        return {
            "schema_version": "trafficvisson.model-version-list.v1",
            "project_id": project_text,
            "count": len(models),
            "models": models,
        }

    def get_project_state(self, project_id: UUID) -> Optional[Dict[str, Any]]:
        project_text = _canonical_uuid(project_id, "project_id")
        with self._lock, self._connect() as connection:
            return self._get_state(connection, project_text)

    def resolve_active_model(
        self,
        project_id: UUID,
    ) -> ResolvedActiveProjectModel:
        """Return a hash-verified active project artifact for inference."""

        project_text = _canonical_uuid(project_id, "project_id")
        with self._lock, self._connect() as connection:
            state = self._get_state(connection, project_text)
            if state is None or state["active_model_id"] is None:
                raise ProjectModelLifecycleError(
                    "active_project_model_artifact_unavailable",
                    "the active model is a baseline without a project artifact",
                )
            row = connection.execute(
                """
                SELECT * FROM model_versions
                WHERE project_id = ? AND id = ?
                """,
                (project_text, state["active_model_id"]),
            ).fetchone()
            if row is None:
                raise ProjectModelLifecycleError(
                    "lifecycle_store_corrupt",
                    "active project model record is missing",
                )
            model = self._decode_integrity_row(
                row,
                label="model version",
            )
            if (
                model["state"] != "active"
                or model["model_version"]
                != state["active_model_version"]
            ):
                raise ProjectModelLifecycleError(
                    "lifecycle_store_corrupt",
                    "active project model state does not match its record",
                )
            self._verify_model_artifact(model)
            directory, artifact_locator = (
                self._resolve_model_artifact_directory(model)
            )
            primary = directory.joinpath(
                *PurePosixPath(
                    model["primary_artifact_relative_path"]
                ).parts
            )
            return ResolvedActiveProjectModel(
                project_id=project_text,
                state_revision=int(state["revision"]),
                model_version_id=model["model_version_id"],
                model_version=model["model_version"],
                model_format=model["model_format"],
                dataset_version_id=model["dataset_version_id"],
                dataset_hash=model["dataset_hash"],
                fixed_validation_sha256=model[
                    "fixed_validation_sha256"
                ],
                primary_artifact_path=primary,
                primary_artifact_sha256=model[
                    "primary_artifact_sha256"
                ],
                artifact_manifest_path=(
                    directory / "artifact-manifest.json"
                ),
                artifact_manifest_sha256=model[
                    "artifact_manifest_sha256"
                ],
                training_analysis_job_ids=tuple(
                    model["training_analysis_job_ids"]
                ),
                fixed_validation_analysis_job_ids=tuple(
                    model["fixed_validation_analysis_job_ids"]
                ),
                source_analysis_lineage_sha256=model[
                    "source_analysis_lineage_sha256"
                ],
                dataset_provenance_graph_sha256=str(
                    model.get(
                        "dataset_provenance_graph_sha256",
                        "",
                    )
                ),
                output_location_id=(
                    artifact_locator["output_location_id"]
                    if artifact_locator is not None
                    else "project-default"
                ),
                root_identity_sha256=(
                    artifact_locator["root_identity_sha256"]
                    if artifact_locator is not None
                    else ""
                ),
                fixed_validation_set_version=int(
                    model.get("fixed_validation_set_version", 1)
                ),
                model_pack_identity_sha256=str(
                    model.get("model_pack_identity_sha256", "")
                ),
                training_runtime_provenance_sha256=str(
                    model.get(
                        "training_runtime_provenance_sha256",
                        "",
                    )
                ),
            )

    def promote(
        self,
        project_id: UUID,
        model_version_id: UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
        user_confirmed: bool,
    ) -> Dict[str, Any]:
        return self._transition(
            project_id,
            action="promote",
            target_model_id=model_version_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            user_confirmed=user_confirmed,
        )

    def reject(
        self,
        project_id: UUID,
        model_version_id: UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
        user_confirmed: bool,
    ) -> Dict[str, Any]:
        return self._transition(
            project_id,
            action="reject",
            target_model_id=model_version_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            user_confirmed=user_confirmed,
        )

    def rollback(
        self,
        project_id: UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
        user_confirmed: bool,
    ) -> Dict[str, Any]:
        return self._transition(
            project_id,
            action="rollback",
            target_model_id=None,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            user_confirmed=user_confirmed,
        )

    def _transition(
        self,
        project_id: UUID,
        *,
        action: str,
        target_model_id: Optional[UUID],
        expected_revision: int,
        idempotency_key: str,
        user_confirmed: bool,
    ) -> Dict[str, Any]:
        if user_confirmed is not True:
            raise ProjectModelLifecycleError(
                "model_transition_confirmation_required",
                f"explicit user confirmation is required to {action}",
            )
        project_text = _canonical_uuid(project_id, "project_id")
        target_id = (
            _canonical_uuid(target_model_id, "model_version_id")
            if target_model_id is not None
            else None
        )
        revision = _strict_revision(expected_revision)
        idempotency = _canonical_idempotency_key(idempotency_key)
        request = {
            "project_id": project_text,
            "action": action,
            "target_model_id": target_id,
            "expected_revision": revision,
            "user_confirmed": True,
        }
        request_sha256 = _canonical_sha256(request)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM model_state_events
                    WHERE project_id = ? AND idempotency_key = ?
                    """,
                    (project_text, idempotency),
                ).fetchone()
                if existing is not None:
                    if existing["request_sha256"] != request_sha256:
                        raise ProjectModelLifecycleError(
                            "idempotency_conflict",
                            "idempotency key was already used for another transition",
                        )
                    event = self._decode_integrity_row(
                        existing,
                        label="model state event",
                    )
                    connection.execute("COMMIT")
                    return {**event, "idempotent_replay": True}
                state = self._get_state(connection, project_text)
                if state is None:
                    raise ProjectModelLifecycleError(
                        "model_state_not_found",
                        "project model state does not exist",
                    )
                if state["revision"] != revision:
                    raise ProjectModelLifecycleError(
                        "model_state_revision_conflict",
                        "project model state changed; reload before transition",
                    )
                before = dict(state)
                timestamp = _utc_now()
                if action in {"promote", "reject"}:
                    if state["candidate_model_id"] != target_id:
                        raise ProjectModelLifecycleError(
                            "candidate_not_found",
                            "target is not this project's current candidate",
                        )
                    model_row = connection.execute(
                        """
                        SELECT * FROM model_versions
                        WHERE project_id = ? AND id = ?
                        """,
                        (project_text, target_id),
                    ).fetchone()
                    if model_row is None:
                        raise ProjectModelLifecycleError(
                            "candidate_not_found",
                            "candidate model was not found",
                        )
                    candidate = self._decode_integrity_row(
                        model_row,
                        label="model version",
                    )
                    if candidate["state"] != "candidate":
                        raise ProjectModelLifecycleError(
                            "candidate_not_found",
                            "target model is not awaiting review",
                        )
                    if action == "promote":
                        if (
                            candidate["promotion_review"]["decision"]
                            != "eligible"
                        ):
                            raise ProjectModelLifecycleError(
                                "candidate_not_eligible",
                                "fixed validation rejected this candidate",
                            )
                        self._verify_model_artifact(candidate)
                        old_previous_id = state["previous_model_id"]
                        if old_previous_id is not None:
                            self._set_model_state(
                                connection,
                                project_text,
                                old_previous_id,
                                "archived",
                                timestamp,
                            )
                        old_active_id = state["active_model_id"]
                        if old_active_id is not None:
                            self._set_model_state(
                                connection,
                                project_text,
                                old_active_id,
                                "previous",
                                timestamp,
                            )
                        self._set_model_state(
                            connection,
                            project_text,
                            target_id,
                            "active",
                            timestamp,
                        )
                        state.update(
                            {
                                "previous_model_id": old_active_id,
                                "previous_model_version": state[
                                    "active_model_version"
                                ],
                                "active_model_id": target_id,
                                "active_model_version": candidate[
                                    "model_version"
                                ],
                                "candidate_model_id": None,
                                "candidate_model_version": None,
                            }
                        )
                    else:
                        self._set_model_state(
                            connection,
                            project_text,
                            target_id,
                            "rejected",
                            timestamp,
                        )
                        state.update(
                            {
                                "candidate_model_id": None,
                                "candidate_model_version": None,
                            }
                        )
                elif action == "rollback":
                    if state["candidate_model_id"] is not None:
                        raise ProjectModelLifecycleError(
                            "candidate_review_required",
                            "review the candidate before rollback",
                        )
                    if state["previous_model_version"] is None:
                        raise ProjectModelLifecycleError(
                            "rollback_unavailable",
                            "no previous model is available",
                        )
                    active_id = state["active_model_id"]
                    active_version = state["active_model_version"]
                    previous_id = state["previous_model_id"]
                    previous_version = state["previous_model_version"]
                    if active_id is not None:
                        self._set_model_state(
                            connection,
                            project_text,
                            active_id,
                            "previous",
                            timestamp,
                        )
                    if previous_id is not None:
                        previous_row = connection.execute(
                            """
                            SELECT * FROM model_versions
                            WHERE project_id = ? AND id = ?
                            """,
                            (project_text, previous_id),
                        ).fetchone()
                        if previous_row is None:
                            raise ProjectModelLifecycleError(
                                "lifecycle_store_corrupt",
                                "previous project model record is missing",
                            )
                        self._verify_model_artifact(
                            self._decode_integrity_row(
                                previous_row,
                                label="model version",
                            )
                        )
                        self._set_model_state(
                            connection,
                            project_text,
                            previous_id,
                            "active",
                            timestamp,
                        )
                    state.update(
                        {
                            "active_model_id": previous_id,
                            "active_model_version": previous_version,
                            "previous_model_id": active_id,
                            "previous_model_version": active_version,
                        }
                    )
                else:
                    raise ProjectModelLifecycleError(
                        "invalid_transition",
                        "unsupported model lifecycle transition",
                    )
                state.update(
                    {
                        "revision": revision + 1,
                        "updated_at": timestamp,
                    }
                )
                self._write_payload(
                    connection,
                    "project_model_states",
                    project_text,
                    state,
                )
                event_id = str(
                    uuid5(
                        UUID(project_text),
                        (
                            f"{MODEL_EVENT_SCHEMA_VERSION}:"
                            f"{idempotency}:{request_sha256}"
                        ),
                    )
                )
                event = {
                    "schema_version": MODEL_EVENT_SCHEMA_VERSION,
                    "event_id": event_id,
                    "project_id": project_text,
                    "action": action,
                    "target_model_id": target_id,
                    "request_sha256": request_sha256,
                    "user_confirmed": True,
                    "automatic_transition": False,
                    "before_state": before,
                    "after_state": state,
                    "created_at": timestamp,
                }
                event_text = canonical_json_bytes(event).decode("utf-8")
                connection.execute(
                    """
                    INSERT INTO model_state_events(
                        id, project_id, idempotency_key, action,
                        request_sha256, payload_json, payload_sha256,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        project_text,
                        idempotency,
                        action,
                        request_sha256,
                        event_text,
                        hashlib.sha256(
                            event_text.encode("utf-8")
                        ).hexdigest(),
                        timestamp,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {**event, "idempotent_replay": False}

    def _verify_model_artifact(self, model: Mapping[str, Any]) -> None:
        """Re-hash every declared file before an activation boundary."""

        directory, artifact_locator = (
            self._resolve_model_artifact_directory(model)
        )
        if (
            not directory.is_dir()
            or _is_link_or_reparse(directory)
        ):
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact directory is unavailable",
            )
        for component in (directory, *directory.parents):
            if (
                artifact_locator is None
                and component == self._root
            ):
                break
            if (
                artifact_locator is not None
                and component.name == PROJECT_MODEL_ARTIFACT_CATEGORY
            ):
                break
            if _is_link_or_reparse(component):
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model artifact path contains a link or reparse point",
                )
        manifest_path = directory / "artifact-manifest.json"
        if (
            not manifest_path.is_file()
            or _is_link_or_reparse(manifest_path)
        ):
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact manifest is unavailable",
            )
        if manifest_path.stat().st_size > MAX_REQUEST_BYTES:
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact manifest is too large",
            )
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != model.get(
            "artifact_manifest_sha256"
        ):
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact manifest hash changed",
            )
        try:
            manifest = json.loads(manifest_bytes)
        except (TypeError, ValueError) as error:
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact manifest is invalid JSON",
            ) from error
        training_analysis_job_ids = model.get(
            "training_analysis_job_ids"
        )
        fixed_validation_analysis_job_ids = model.get(
            "fixed_validation_analysis_job_ids"
        )
        source_analysis_lineage = {
            "training_analysis_job_ids": training_analysis_job_ids,
            "fixed_validation_analysis_job_ids": (
                fixed_validation_analysis_job_ids
            ),
        }
        raw_model_provenance = model.get(
            "dataset_provenance_graph"
        )
        if raw_model_provenance is None:
            provenance_identity_valid = (
                "dataset_provenance_graph" not in manifest
                and "dataset_provenance_graph_sha256"
                not in manifest
                and "dataset_provenance_graph_sha256"
                not in model
            )
        else:
            try:
                canonical_model_provenance = (
                    canonical_dataset_provenance_graph(
                        raw_model_provenance
                    )
                )
            except TrainingProvenanceError as error:
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model provenance is invalid",
                ) from error
            provenance_identity_valid = (
                canonical_model_provenance
                == raw_model_provenance
                and canonical_model_provenance["project_id"]
                == model.get("project_id")
                and model.get(
                    "dataset_provenance_graph_sha256"
                )
                == canonical_model_provenance[
                    "provenance_graph_sha256"
                ]
                and manifest.get("dataset_provenance_graph")
                == canonical_model_provenance
                and manifest.get(
                    "dataset_provenance_graph_sha256"
                )
                == model.get(
                    "dataset_provenance_graph_sha256"
                )
            )
        production_provenance_valid = True
        if model.get("trainer_test_only") is False:
            runtime = manifest.get("training_runtime_provenance")
            runtime_sha256 = manifest.get(
                "training_runtime_provenance_sha256"
            )
            try:
                canonical_runtime = json.loads(
                    canonical_json_bytes(runtime).decode("utf-8")
                )
            except Exception:
                canonical_runtime = None
            production_provenance_valid = (
                isinstance(canonical_runtime, dict)
                and runtime == canonical_runtime
                and runtime.get("accelerator") == "cuda"
                and runtime.get("accelerator_used") is True
                and str(runtime.get("resolved_device", "")).startswith(
                    "cuda"
                )
                and isinstance(runtime_sha256, str)
                and _SHA256.fullmatch(runtime_sha256) is not None
                and runtime_sha256 == _canonical_sha256(runtime)
                and model.get(
                    "training_runtime_provenance_sha256"
                )
                == runtime_sha256
                and isinstance(manifest.get("metadata"), Mapping)
                and manifest["metadata"].get("execution_provenance")
                == runtime
            )
            if (
                production_provenance_valid
                and model.get("model_format")
                == LOCAL_OBJECT_DETECTOR_MODEL_FORMAT
            ):
                try:
                    pack_identity = (
                        _canonical_detector_model_pack_identity(
                            manifest.get("model_pack_identity")
                        )
                    )
                except ProjectModelLifecycleError:
                    production_provenance_valid = False
                else:
                    pack_sha256 = _canonical_sha256(pack_identity)
                    execution_pack_hash = runtime.get(
                        "model_pack_manifest_sha256"
                    )
                    production_provenance_valid = (
                        manifest.get("model_pack_identity")
                        == pack_identity
                        and manifest.get(
                            "model_pack_identity_sha256"
                        )
                        == pack_sha256
                        and model.get("model_pack_identity")
                        == pack_identity
                        and model.get(
                            "model_pack_identity_sha256"
                        )
                        == pack_sha256
                        and manifest["metadata"].get(
                            "model_pack_identity"
                        )
                        == pack_identity
                        and (
                            execution_pack_hash is None
                            or execution_pack_hash
                            == pack_identity["manifest_sha256"]
                        )
                    )
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version")
            != MODEL_ARTIFACT_SCHEMA_VERSION
            or manifest.get("project_id") != model.get("project_id")
            or manifest.get("training_job_id")
            != model.get("training_job_id")
            or manifest.get("model_version_id")
            != model.get("model_version_id")
            or manifest.get("model_version") != model.get("model_version")
            or manifest.get("dataset_hash") != model.get("dataset_hash")
            or manifest.get("fixed_validation_sha256")
            != model.get("fixed_validation_sha256")
            or manifest.get("fixed_validation_membership_sha256")
            != model.get("fixed_validation_membership_sha256")
            or isinstance(
                model.get("fixed_validation_set_version", 1), bool
            )
            or not isinstance(
                model.get("fixed_validation_set_version", 1), int
            )
            or model.get("fixed_validation_set_version", 1) < 1
            or manifest.get("fixed_validation_set_version", 1)
            != model.get("fixed_validation_set_version", 1)
            or not isinstance(training_analysis_job_ids, list)
            or not training_analysis_job_ids
            or any(
                _canonical_uuid(value, "training.analysis_job_id")
                != value
                for value in training_analysis_job_ids
            )
            or training_analysis_job_ids
            != sorted(set(training_analysis_job_ids))
            or not isinstance(
                fixed_validation_analysis_job_ids, list
            )
            or not fixed_validation_analysis_job_ids
            or any(
                _canonical_uuid(value, "validation.analysis_job_id")
                != value
                for value in fixed_validation_analysis_job_ids
            )
            or fixed_validation_analysis_job_ids
            != sorted(set(fixed_validation_analysis_job_ids))
            or set(training_analysis_job_ids).intersection(
                fixed_validation_analysis_job_ids
            )
            or model.get("source_analysis_lineage_sha256")
            != _canonical_sha256(source_analysis_lineage)
            or manifest.get("source_analysis_lineage")
            != source_analysis_lineage
            or manifest.get("source_analysis_lineage_sha256")
            != model.get("source_analysis_lineage_sha256")
            or not provenance_identity_valid
            or (
                artifact_locator is not None
                and manifest.get("artifact_storage")
                != artifact_locator
            )
            or (
                artifact_locator is None
                and "artifact_storage" in manifest
            )
            or not production_provenance_valid
        ):
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact manifest identity changed",
            )
        declared_files = manifest.get("files")
        if (
            not isinstance(declared_files, list)
            or not declared_files
            or len(declared_files) > MAX_ARTIFACT_FILES
        ):
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact file manifest is invalid",
            )
        for entry in directory.rglob("*"):
            if _is_link_or_reparse(entry):
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model artifacts contain a link or reparse point",
                )
        observed_paths = set()
        total_size = 0
        for record in declared_files:
            if not isinstance(record, Mapping):
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model artifact file record is invalid",
                )
            file_relative = PurePosixPath(str(record.get("path", "")))
            if (
                file_relative.is_absolute()
                or not file_relative.parts
                or any(
                    part in {"", ".", ".."}
                    for part in file_relative.parts
                )
                or file_relative.as_posix() in observed_paths
            ):
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model artifact file path is unsafe",
                )
            observed_paths.add(file_relative.as_posix())
            path = directory.joinpath(*file_relative.parts)
            if not path.is_file() or _is_link_or_reparse(path):
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model artifact file is unavailable",
                )
            size = path.stat().st_size
            if (
                isinstance(record.get("size_bytes"), bool)
                or record.get("size_bytes") != size
                or size <= 0
                or size > MAX_ARTIFACT_FILE_BYTES
            ):
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model artifact file size changed",
                )
            total_size += size
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            if digest.hexdigest() != record.get("sha256"):
                raise ProjectModelLifecycleError(
                    "artifact_integrity_failed",
                    "stored model artifact file hash changed",
                )
        actual_paths = {
            path.relative_to(directory).as_posix()
            for path in directory.rglob("*")
            if path.is_file()
        }
        if actual_paths != observed_paths | {"artifact-manifest.json"}:
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact contains undeclared files",
            )
        if (
            total_size != manifest.get("total_size_bytes")
            or total_size != model.get("artifact_total_size_bytes")
        ):
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored model artifact total size changed",
            )
        primary_path = model.get("primary_artifact_relative_path")
        primary_record = next(
            (
                record
                for record in declared_files
                if record.get("path") == primary_path
            ),
            None,
        )
        if (
            primary_record is None
            or primary_record.get("sha256")
            != model.get("primary_artifact_sha256")
            or manifest.get("primary_artifact") != primary_path
        ):
            raise ProjectModelLifecycleError(
                "artifact_integrity_failed",
                "stored primary model artifact binding changed",
            )

    def _set_model_state(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        model_id: str,
        state: str,
        timestamp: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT * FROM model_versions
            WHERE project_id = ? AND id = ?
            """,
            (project_id, model_id),
        ).fetchone()
        if row is None:
            raise ProjectModelLifecycleError(
                "lifecycle_store_corrupt",
                "model state references an unavailable model",
            )
        model = self._decode_integrity_row(row, label="model version")
        model.update({"state": state, "updated_at": timestamp})
        self._write_payload(
            connection,
            "model_versions",
            model_id,
            model,
        )


__all__ = [
    "DEFAULT_AUXILIARY_REFERENCE_WEIGHT",
    "DETECTOR_MODEL_PACK_SCHEMA_VERSION",
    "DeterministicTestTrainer",
    "FIXED_VALIDATION_EXTENSION_APPROVAL_SCHEMA_VERSION",
    "FIXED_VALIDATION_SET_SCHEMA_VERSION",
    "LOCAL_OBJECT_DETECTOR_MODEL_FORMAT",
    "MINIMUM_CURRENT_CONFIRMED_LABELS",
    "MODEL_ARTIFACT_SCHEMA_VERSION",
    "MODEL_STATE_SCHEMA_VERSION",
    "MODEL_VERSION_SCHEMA_VERSION",
    "ProjectModelLifecycleError",
    "ProjectModelLifecycleStore",
    "ProjectModelTrainer",
    "ProjectModelTrainerUnavailable",
    "ResolvedActiveProjectModel",
    "TRAINER_CONTRACT_VERSION",
    "TRAINING_CHECKPOINT_SCHEMA_VERSION",
    "TRAINING_JOB_SCHEMA_VERSION",
    "TrainerInvocation",
    "TrainerResult",
    "VISUAL_CENTROID_MODEL_FORMAT",
    "VisualEmbeddingCentroidHeadTrainerAdapter",
]
