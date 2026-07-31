"""Offline inference for an explicitly promoted local object detector.

The lifecycle resolver has already verified and promoted the model.  This
adapter re-verifies the active artifact binding at inference time, rejects any
survey analysis used for training or fixed validation, and returns boxed
review-only proposals.  It never creates Annotation or Incident truth.
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
from typing import Any, Mapping, Optional, Protocol, Sequence
from uuid import UUID

from .local_object_detector_trainer import (
    ADAPTER_ARTIFACT_SCHEMA,
    DetectorBoxPrediction,
    DetectorSample,
)
from .project_model_lifecycle import (
    LOCAL_OBJECT_DETECTOR_MODEL_FORMAT,
    MODEL_ARTIFACT_SCHEMA_VERSION,
    ProjectModelLifecycleError,
    ResolvedActiveProjectModel,
)
from .training_geometry import TrainingGeometryError, canonical_box
from .validation_metrics import CANONICAL_ISSUE_TYPES


INFERENCE_SCHEMA_VERSION = (
    "trafficvisson.promoted-local-object-detector-inference.v1"
)
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_PREDICTIONS_PER_FRAME = 256
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_WINDOWS_REPARSE_ATTRIBUTE = 0x400


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
        raise ProjectModelLifecycleError(
            "project_detector_artifact_unavailable",
            "the promoted detector artifact could not be read",
        ) from error
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise ProjectModelLifecycleError(
            "project_detector_artifact_changed",
            "the promoted detector artifact changed while it was read",
        )
    return digest.hexdigest(), after.st_size


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite")
            ),
        )
    except Exception as error:
        raise ProjectModelLifecycleError(
            "project_detector_artifact_invalid",
            f"the promoted detector {label} is invalid",
        ) from error
    if not isinstance(value, Mapping):
        raise ProjectModelLifecycleError(
            "project_detector_artifact_invalid",
            f"the promoted detector {label} must be an object",
        )
    return value


def _relative_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise ProjectModelLifecycleError(
            "project_detector_artifact_invalid",
            f"{label} is invalid",
        )
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
    ):
        raise ProjectModelLifecycleError(
            "project_detector_artifact_invalid",
            f"{label} is unsafe",
        )
    return path


def _canonical_uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except Exception as error:
        raise ProjectModelLifecycleError(
            "project_detector_survey_invalid",
            f"{label} must be a canonical UUID",
        ) from error


@dataclass(frozen=True)
class DetectorInferenceBox:
    issue_type: str
    score: float
    normalized_box: Mapping[str, float]


@dataclass(frozen=True)
class DenseDetectorFrame:
    frame_id: str
    body: bytes
    media_type: str
    sha256: str


@dataclass(frozen=True)
class LoadedDetectorHandle:
    identity_sha256: str
    value: Any
    id_to_label: Mapping[int, str]


class LocalDetectorInferenceBackend(Protocol):
    backend_id: str
    backend_version: str

    def status(self) -> Mapping[str, Any]:
        ...

    def load(
        self,
        model_directory: Path,
        *,
        identity_sha256: str,
    ) -> LoadedDetectorHandle:
        ...

    def predict(
        self,
        handle: LoadedDetectorHandle,
        *,
        body: bytes,
        media_type: str,
    ) -> Sequence[DetectorInferenceBox]:
        ...


class TransformersLocalDetectorInferenceBackend:
    backend_id = "transformers-local-object-detector-inference"
    backend_version = "1.0"

    def __init__(self, *, device: str = "cuda:0") -> None:
        if device != "cuda:0":
            raise ValueError("production detector inference requires cuda:0")
        self._device = device

    def status(self) -> Mapping[str, Any]:
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
            return {
                "state": "unavailable",
                "reason": f"dependency_import_failed:{type(error).__name__}",
                "backend_id": self.backend_id,
                "backend_version": self.backend_version,
            }
        if not torch.cuda.is_available():
            return {
                "state": "failed",
                "reason": "cuda_unavailable",
                "backend_id": self.backend_id,
                "backend_version": self.backend_version,
                "torch_version": str(torch.__version__),
                "transformers_version": str(transformers.__version__),
            }
        properties = torch.cuda.get_device_properties(0)
        return {
            "state": "ready",
            "reason": None,
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "device": self._device,
            "torch_version": str(torch.__version__),
            "transformers_version": str(transformers.__version__),
            "cuda_version": (
                str(torch.version.cuda)
                if torch.version.cuda is not None
                else None
            ),
            "cuda_device_name": str(properties.name),
            "cuda_total_memory_bytes": int(properties.total_memory),
        }

    def load(
        self,
        model_directory: Path,
        *,
        identity_sha256: str,
    ) -> LoadedDetectorHandle:
        try:
            import torch
            from transformers import (
                AutoConfig,
                AutoImageProcessor,
                AutoModelForObjectDetection,
            )
        except Exception as error:
            raise ProjectModelLifecycleError(
                "project_detector_runtime_unavailable",
                "the local detector inference runtime is unavailable",
            ) from error
        previous = {
            key: os.environ.get(key)
            for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
        }
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        try:
            config = AutoConfig.from_pretrained(
                str(model_directory),
                local_files_only=True,
                trust_remote_code=False,
            )
            processor = AutoImageProcessor.from_pretrained(
                str(model_directory),
                local_files_only=True,
                trust_remote_code=False,
            )
            model = AutoModelForObjectDetection.from_pretrained(
                str(model_directory),
                local_files_only=True,
                trust_remote_code=False,
            )
        except Exception as error:
            raise ProjectModelLifecycleError(
                "project_detector_model_load_failed",
                "the promoted local detector could not be loaded",
            ) from error
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        try:
            id_to_label = {
                int(class_id): str(issue_type)
                for class_id, issue_type in config.id2label.items()
            }
        except Exception as error:
            raise ProjectModelLifecycleError(
                "project_detector_model_load_failed",
                "the promoted detector label map is invalid",
            ) from error
        if (
            not id_to_label
            or any(
                value not in CANONICAL_ISSUE_TYPES
                for value in id_to_label.values()
            )
        ):
            raise ProjectModelLifecycleError(
                "project_detector_model_load_failed",
                "the promoted detector label map is unsupported",
            )
        device = torch.device(self._device)
        model.to(device)
        model.eval()
        return LoadedDetectorHandle(
            identity_sha256=identity_sha256,
            value=(torch, processor, model, device),
            id_to_label=id_to_label,
        )

    def predict(
        self,
        handle: LoadedDetectorHandle,
        *,
        body: bytes,
        media_type: str,
    ) -> Sequence[DetectorInferenceBox]:
        try:
            from io import BytesIO
            from PIL import Image

            torch, processor, model, device = handle.value
            with Image.open(BytesIO(body)) as source:
                image = source.convert("RGB")
                image.load()
            inputs = processor(images=image, return_tensors="pt")
            inputs = {
                key: value.to(device)
                for key, value in inputs.items()
            }
            with torch.inference_mode():
                outputs = model(**inputs)
            target_sizes = torch.tensor(
                [[image.height, image.width]],
                device=device,
            )
            result = processor.post_process_object_detection(
                outputs,
                threshold=0.0,
                target_sizes=target_sizes,
            )[0]
        except Exception as error:
            raise ProjectModelLifecycleError(
                "project_detector_inference_failed",
                "the promoted detector could not evaluate a dense frame",
            ) from error
        boxes = []
        for score, label, box in zip(
            result["scores"],
            result["labels"],
            result["boxes"],
        ):
            class_id = int(label.detach().cpu().item())
            issue_type = handle.id_to_label.get(class_id)
            if issue_type is None:
                raise ProjectModelLifecycleError(
                    "project_detector_inference_failed",
                    "the promoted detector emitted an unknown class",
                )
            x1, y1, x2, y2 = [
                float(value) for value in box.detach().cpu().tolist()
            ]
            raw_box = {
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
                normalized = canonical_box(raw_box)
            except TrainingGeometryError:
                continue
            boxes.append(
                DetectorInferenceBox(
                    issue_type=issue_type,
                    score=float(score.detach().cpu().item()),
                    normalized_box=normalized,
                )
            )
        return tuple(boxes)


@dataclass(frozen=True)
class _VerifiedDetectorArtifact:
    model_directory: Path
    identity_sha256: str
    score_threshold: float
    adapter: Mapping[str, Any]


class PromotedLocalObjectDetectorInference:
    """Re-verify and apply one promoted detector to independent dense frames."""

    def __init__(
        self,
        backend: Optional[LocalDetectorInferenceBackend] = None,
    ) -> None:
        self._backend = (
            backend or TransformersLocalDetectorInferenceBackend()
        )
        self._cache: dict[str, LoadedDetectorHandle] = {}

    def status(self) -> Mapping[str, Any]:
        return {
            "provider": "promoted_local_object_detector",
            **dict(self._backend.status()),
        }

    @staticmethod
    def _verify_model(
        model: ResolvedActiveProjectModel,
        analysis_job_id: str,
        *,
        purpose: str = "survey",
        fixed_validation_sha256: Optional[str] = None,
    ) -> _VerifiedDetectorArtifact:
        if model.model_format != LOCAL_OBJECT_DETECTOR_MODEL_FORMAT:
            raise ProjectModelLifecycleError(
                "project_detector_model_format_mismatch",
                "the promoted model is not a local object detector",
            )
        analysis_id = _canonical_uuid(
            analysis_job_id,
            "analysis_job_id",
        )
        excluded = set(model.training_analysis_job_ids) | set(
            model.fixed_validation_analysis_job_ids
        )
        if purpose == "survey" and (
            not model.training_analysis_job_ids
            or not model.fixed_validation_analysis_job_ids
            or analysis_id in excluded
        ):
            raise ProjectModelLifecycleError(
                "project_detector_survey_lineage_rejected",
                (
                    "the promoted detector cannot survey a training or "
                    "fixed-validation analysis"
                ),
            )
        if purpose == "fixed_validation" and (
            fixed_validation_sha256 != model.fixed_validation_sha256
            or analysis_id
            not in set(model.fixed_validation_analysis_job_ids)
            or analysis_id in set(model.training_analysis_job_ids)
        ):
            raise ProjectModelLifecycleError(
                "project_detector_validation_lineage_rejected",
                (
                    "the active detector fixed-validation evidence does not "
                    "match its immutable validation lineage"
                ),
            )
        if purpose not in {"survey", "fixed_validation"}:
            raise ProjectModelLifecycleError(
                "project_detector_inference_purpose_invalid",
                "the promoted detector inference purpose is unsupported",
            )
        source_lineage = {
            "training_analysis_job_ids": list(
                model.training_analysis_job_ids
            ),
            "fixed_validation_analysis_job_ids": list(
                model.fixed_validation_analysis_job_ids
            ),
        }
        if (
            hashlib.sha256(
                json.dumps(
                    source_lineage,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            != model.source_analysis_lineage_sha256
        ):
            raise ProjectModelLifecycleError(
                (
                    "project_detector_validation_lineage_rejected"
                    if purpose == "fixed_validation"
                    else "project_detector_survey_lineage_rejected"
                ),
                "the promoted detector source lineage digest changed",
            )
        primary = Path(model.primary_artifact_path)
        manifest_path = Path(model.artifact_manifest_path)
        if (
            not primary.is_absolute()
            or not manifest_path.is_absolute()
            or not primary.is_file()
            or not manifest_path.is_file()
            or _is_link_or_reparse(primary)
            or _is_link_or_reparse(manifest_path)
        ):
            raise ProjectModelLifecycleError(
                "project_detector_artifact_unavailable",
                "the promoted detector artifact is unavailable",
            )
        primary_hash, _primary_size = _sha256_file(primary)
        manifest_hash, _manifest_size = _sha256_file(manifest_path)
        if (
            primary_hash != model.primary_artifact_sha256
            or manifest_hash != model.artifact_manifest_sha256
        ):
            raise ProjectModelLifecycleError(
                "project_detector_artifact_changed",
                "the promoted detector artifact binding changed",
            )
        manifest = _read_json(manifest_path, "artifact manifest")
        if (
            manifest.get("schema_version")
            != MODEL_ARTIFACT_SCHEMA_VERSION
            or manifest.get("project_id") != model.project_id
            or manifest.get("model_version_id")
            != model.model_version_id
            or manifest.get("model_version") != model.model_version
            or manifest.get("model_format")
            != LOCAL_OBJECT_DETECTOR_MODEL_FORMAT
            or manifest.get("dataset_version_id")
            != model.dataset_version_id
            or manifest.get("dataset_hash") != model.dataset_hash
            or manifest.get("fixed_validation_sha256")
            != model.fixed_validation_sha256
            or manifest.get("primary_artifact")
            != primary.name
            or manifest.get("source_analysis_lineage_sha256")
            != model.source_analysis_lineage_sha256
        ):
            raise ProjectModelLifecycleError(
                "project_detector_artifact_invalid",
                "the promoted detector artifact manifest binding is invalid",
            )
        adapter = _read_json(primary, "adapter artifact")
        if (
            adapter.get("schema_version") != ADAPTER_ARTIFACT_SCHEMA
            or adapter.get("project_id") != model.project_id
            or adapter.get("dataset_version_id")
            != model.dataset_version_id
            or adapter.get("dataset_hash") != model.dataset_hash
            or adapter.get("fixed_validation_sha256")
            != model.fixed_validation_sha256
        ):
            raise ProjectModelLifecycleError(
                "project_detector_artifact_invalid",
                "the promoted detector adapter binding is invalid",
            )
        raw_weight = adapter.get("primary_weight")
        if not isinstance(raw_weight, Mapping) or set(raw_weight) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise ProjectModelLifecycleError(
                "project_detector_artifact_invalid",
                "the promoted detector weight binding is invalid",
            )
        relative = _relative_path(
            raw_weight["path"],
            "primary_weight.path",
        )
        weight = primary.parent.joinpath(*relative.parts)
        try:
            weight.resolve(strict=True).relative_to(
                primary.parent.resolve(strict=True)
            )
        except (OSError, ValueError) as error:
            raise ProjectModelLifecycleError(
                "project_detector_artifact_invalid",
                "the promoted detector weight escaped its model directory",
            ) from error
        if not weight.is_file() or _is_link_or_reparse(weight):
            raise ProjectModelLifecycleError(
                "project_detector_artifact_unavailable",
                "the promoted detector weight is unavailable",
            )
        weight_hash, weight_size = _sha256_file(weight)
        if (
            raw_weight.get("sha256") != weight_hash
            or raw_weight.get("size_bytes") != weight_size
        ):
            raise ProjectModelLifecycleError(
                "project_detector_artifact_changed",
                "the promoted detector weight binding changed",
            )
        metrics = adapter.get("candidate_box_metrics")
        threshold = (
            metrics.get("score_threshold")
            if isinstance(metrics, Mapping)
            else None
        )
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not math.isfinite(float(threshold))
            or not 0.0 <= float(threshold) <= 1.0
        ):
            raise ProjectModelLifecycleError(
                "project_detector_artifact_invalid",
                "the promoted detector score threshold is invalid",
            )
        identity = hashlib.sha256(
            (
                model.artifact_manifest_sha256
                + model.primary_artifact_sha256
                + weight_hash
            ).encode("ascii")
        ).hexdigest()
        return _VerifiedDetectorArtifact(
            model_directory=weight.parent,
            identity_sha256=identity,
            score_threshold=float(threshold),
            adapter=adapter,
        )

    @staticmethod
    def _verified_image(
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> bytes:
        if (
            not isinstance(body, bytes)
            or not 1 <= len(body) <= MAX_IMAGE_BYTES
            or not isinstance(expected_sha256, str)
            or _SHA256.fullmatch(expected_sha256) is None
            or hashlib.sha256(body).hexdigest() != expected_sha256
        ):
            raise ProjectModelLifecycleError(
                "project_detector_frame_invalid",
                "the dense frame bytes do not match their server digest",
            )
        valid_magic = (
            media_type == "image/jpeg"
            and body.startswith(b"\xff\xd8\xff")
        ) or (
            media_type == "image/png"
            and body.startswith(b"\x89PNG\r\n\x1a\n")
        ) or (
            media_type == "image/webp"
            and len(body) >= 12
            and body.startswith(b"RIFF")
            and body[8:12] == b"WEBP"
        )
        if not valid_magic:
            raise ProjectModelLifecycleError(
                "project_detector_frame_invalid",
                "the dense frame media magic is unsupported",
            )
        return body

    def classify(
        self,
        model: ResolvedActiveProjectModel,
        *,
        survey_analysis_job_id: UUID | str,
        body: bytes,
        media_type: str,
        expected_sha256: str,
        _purpose: str = "survey",
        _fixed_validation_sha256: Optional[str] = None,
    ) -> Mapping[str, Any]:
        survey_id = _canonical_uuid(
            survey_analysis_job_id,
            "survey_analysis_job_id",
        )
        artifact = self._verify_model(
            model,
            survey_id,
            purpose=_purpose,
            fixed_validation_sha256=_fixed_validation_sha256,
        )
        verified = self._verified_image(
            body,
            media_type,
            expected_sha256,
        )
        handle = self._cache.get(artifact.identity_sha256)
        if handle is None:
            status = self._backend.status()
            if status.get("state") != "ready":
                raise ProjectModelLifecycleError(
                    "project_detector_runtime_unavailable",
                    "the local promoted-detector runtime is not ready",
                )
            handle = self._backend.load(
                artifact.model_directory,
                identity_sha256=artifact.identity_sha256,
            )
            if (
                not isinstance(handle, LoadedDetectorHandle)
                or handle.identity_sha256 != artifact.identity_sha256
            ):
                raise ProjectModelLifecycleError(
                    "project_detector_runtime_invalid",
                    "the local detector backend loaded another artifact",
                )
            self._cache[artifact.identity_sha256] = handle
        raw = self._backend.predict(
            handle,
            body=verified,
            media_type=media_type,
        )
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes, bytearray))
            or len(raw) > MAX_PREDICTIONS_PER_FRAME
        ):
            raise ProjectModelLifecycleError(
                "project_detector_prediction_invalid",
                "the local detector prediction set is invalid",
            )
        predictions = []
        for value in raw:
            if (
                not isinstance(value, DetectorInferenceBox)
                or value.issue_type not in CANONICAL_ISSUE_TYPES
                or isinstance(value.score, bool)
                or not isinstance(value.score, (int, float))
                or not math.isfinite(float(value.score))
                or not 0.0 <= float(value.score) <= 1.0
            ):
                raise ProjectModelLifecycleError(
                    "project_detector_prediction_invalid",
                    "a local detector prediction is invalid",
                )
            try:
                box = canonical_box(value.normalized_box)
            except TrainingGeometryError as error:
                raise ProjectModelLifecycleError(
                    "project_detector_prediction_invalid",
                    "a local detector prediction box is invalid",
                ) from error
            if float(value.score) < artifact.score_threshold:
                continue
            predictions.append(
                {
                    "issue_type": value.issue_type,
                    "score": round(float(value.score), 8),
                    "box": box,
                    "calibration": "promoted_project_detector_threshold",
                }
            )
        predictions.sort(
            key=lambda item: (
                -item["score"],
                item["issue_type"],
                json.dumps(item["box"], sort_keys=True),
            )
        )
        return {
            "schema_version": INFERENCE_SCHEMA_VERSION,
            "project_id": model.project_id,
            "survey_analysis_job_id": survey_id,
            "model_version_id": model.model_version_id,
            "model_version": model.model_version,
            "dataset_version_id": model.dataset_version_id,
            "dataset_hash": model.dataset_hash,
            "fixed_validation_sha256": (
                model.fixed_validation_sha256
            ),
            "primary_artifact_sha256": (
                model.primary_artifact_sha256
            ),
            "artifact_manifest_sha256": (
                model.artifact_manifest_sha256
            ),
            "source_analysis_lineage_sha256": (
                model.source_analysis_lineage_sha256
            ),
            "candidate_image_sha256": expected_sha256,
            "predictions": predictions,
            "review_only_until_pipeline_question_answer": True,
            "annotation_created": False,
            "incident_created": False,
            "training_member_created": False,
            "report_truth_created": False,
        }

    def evaluate_fixed_validation(
        self,
        model: ResolvedActiveProjectModel,
        samples: Sequence[DetectorSample],
        *,
        fixed_validation_sha256: str,
    ) -> tuple[DetectorBoxPrediction, ...]:
        """Evaluate the promoted detector on its exact fixed validation set."""

        if (
            fixed_validation_sha256 != model.fixed_validation_sha256
            or not isinstance(samples, Sequence)
            or isinstance(samples, (str, bytes, bytearray))
        ):
            raise ProjectModelLifecycleError(
                "project_detector_validation_lineage_rejected",
                "the active detector validation request is invalid",
            )
        fixed_jobs = set(model.fixed_validation_analysis_job_ids)
        training_jobs = set(model.training_analysis_job_ids)
        predictions: list[DetectorBoxPrediction] = []
        for sample in samples:
            if (
                not isinstance(sample, DetectorSample)
                or sample.split != "validation"
                or sample.project_id != model.project_id
                or sample.analysis_job_id not in fixed_jobs
                or sample.analysis_job_id in training_jobs
            ):
                raise ProjectModelLifecycleError(
                    "project_detector_validation_lineage_rejected",
                    "a fixed-validation sample crossed model lineage",
                )
            try:
                before = sample.image_path.stat()
                body = sample.image_path.read_bytes()
                after = sample.image_path.stat()
            except OSError as error:
                raise ProjectModelLifecycleError(
                    "project_detector_frame_invalid",
                    "a fixed-validation image could not be read",
                ) from error
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or len(body) != sample.image_size_bytes
            ):
                raise ProjectModelLifecycleError(
                    "project_detector_frame_invalid",
                    "a fixed-validation image changed while it was read",
                )
            result = self.classify(
                model,
                survey_analysis_job_id=sample.analysis_job_id,
                body=body,
                media_type=sample.media_type,
                expected_sha256=sample.image_sha256,
                _purpose="fixed_validation",
                _fixed_validation_sha256=fixed_validation_sha256,
            )
            for value in result["predictions"]:
                predictions.append(
                    DetectorBoxPrediction(
                        sample_id=sample.sample_id,
                        issue_type=str(value["issue_type"]),
                        score=float(value["score"]),
                        normalized_box=dict(value["box"]),
                    )
                )
        return tuple(
            sorted(
                predictions,
                key=lambda value: (
                    value.sample_id,
                    -value.score,
                    value.issue_type,
                    json.dumps(
                        value.normalized_box,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        )

    def scan(
        self,
        model: ResolvedActiveProjectModel,
        *,
        survey_analysis_job_id: UUID | str,
        frames: Sequence[DenseDetectorFrame],
    ) -> tuple[Mapping[str, Any], ...]:
        if (
            not isinstance(frames, Sequence)
            or isinstance(frames, (str, bytes, bytearray))
        ):
            raise ProjectModelLifecycleError(
                "project_detector_frame_invalid",
                "dense frames must be a server-resolved sequence",
            )
        results = []
        for frame in frames:
            if not isinstance(frame, DenseDetectorFrame):
                raise ProjectModelLifecycleError(
                    "project_detector_frame_invalid",
                    "a dense frame handle is invalid",
                )
            result = dict(
                self.classify(
                    model,
                    survey_analysis_job_id=survey_analysis_job_id,
                    body=frame.body,
                    media_type=frame.media_type,
                    expected_sha256=frame.sha256,
                )
            )
            result["frame_id"] = frame.frame_id
            results.append(result)
        return tuple(results)


__all__ = [
    "DenseDetectorFrame",
    "DetectorInferenceBox",
    "INFERENCE_SCHEMA_VERSION",
    "LoadedDetectorHandle",
    "LocalDetectorInferenceBackend",
    "PromotedLocalObjectDetectorInference",
    "TransformersLocalDetectorInferenceBackend",
]
