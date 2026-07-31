"""Opt-in, offline local vision inference for extracted evidence candidates.

The default TrafficSuperVision candidate extractor is deliberately non-classifying.
This module adds a separate model-pack boundary without changing that fallback:

* no model pack path means no model code is imported or executed;
* a pack is a bounded, hash-inventoried directory with an explicit licence
  acknowledgement;
* production inference runs in a disposable child process with network access
  disabled, a hard wall-clock timeout, bounded inputs and bounded JSON output;
* only built-in engines are accepted.  A model pack cannot provide Python code,
  an executable, or ``trust_remote_code`` content;
* failures are converted to an explicit ``scene_fallback`` result.  They never
  become a fabricated defect prediction.

Supported initial engines:

``onnx_yolo_v8``
    A fixed-vocabulary YOLOv8-compatible ONNX detector.  An exported
    YOLO-World model can be used after its road-defect vocabulary is frozen at
    export time.  ONNX Runtime is MIT licensed; the weight licence remains a
    property of the installed pack and must be declared by the installer.

``transformers_clip_zero_shot``
    Candidate-level CLIP zero-shot classification from a *local* Transformers
    directory.  It intentionally returns no box.  At least two normal-road
    negative prompts are required so a frame is not forced into a defect class.

``easyocr_ocr_only``
    Korean/English OCR only.  It can read sign or tunnel text but does not claim
    that the text belongs to a verified named facility.
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
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from uuid import uuid4


MODEL_PACK_SCHEMA = "trafficvisson.vision-model-pack.v1"
INFERENCE_SCHEMA = "trafficvisson.vision-inference.v1"
WORKER_SCHEMA = "trafficvisson.vision-worker.v1"
MODEL_PACK_MANIFEST_NAMES = frozenset(
    {
        "trafficsupervision-model-pack.json",
        "traffic" + "visson-model-pack.json",
    }
)

CANONICAL_ISSUE_TYPES = (
    "pothole",
    "road_surface_damage",
    "road_debris",
    "median_barrier_damage",
    "sign_damage",
    "sign_vegetation_occlusion",
    "roadside_mowing_required",
    "guide_sign_identity",
    "tunnel_identity",
    "tunnel_name_occlusion",
    "lane_marking_faded",
    "colored_guidance_faded",
)
# ``other`` is a bounded operator extension rather than one of the twelve
# maintenance classes.  Legacy identifiers remain input-compatible at the
# model-pack/worker boundary, while every inference result emitted by this
# module uses a canonical identifier.
ISSUE_TYPES = frozenset((*CANONICAL_ISSUE_TYPES, "other"))
ISSUE_TYPE_ALIASES = {
    "surface_damage": "road_surface_damage",
    "debris": "road_debris",
    "sign_vegetation": "sign_vegetation_occlusion",
    "mowing": "roadside_mowing_required",
    "sign_tunnel_identity": "guide_sign_identity",
    "sign_identity": "guide_sign_identity",
    "lane_color_line_fading": "colored_guidance_faded",
    "color_guidance_line_faded": "colored_guidance_faded",
    "colored_guidance_line_faded": "colored_guidance_faded",
}
_ENGINES = frozenset(
    {
        "onnx_yolo_v8",
        "transformers_clip_zero_shot",
        "easyocr_ocr_only",
    }
)
_PACK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")
_SPDX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_RELATIVE_FILE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$"
)
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")

MAX_MANIFEST_BYTES = 128 * 1024
MAX_REQUEST_BYTES = 1 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_PACK_FILES = 512
MAX_PACK_BYTES = 12 * 1024**3
MAX_CANDIDATES = 512
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_PREDICTIONS_PER_CANDIDATE = 32
MAX_OCR_PER_CANDIDATE = 32
MAX_WORKER_PROCESS_MEMORY_BYTES = 8 * 1024**3


class VisionInferenceError(RuntimeError):
    """Safe public failure with no local path or model output in its message."""


class VisionInferenceCancelled(VisionInferenceError):
    """Raised when the owning analysis job is cancelled."""


class VisionInferenceTimeout(VisionInferenceError):
    """Raised when the isolated worker exceeds its wall-clock budget."""


class VisionModelPackError(VisionInferenceError):
    """Raised when an explicitly configured pack fails validation."""


@dataclass(frozen=True)
class VisionArtifact:
    relative_path: str
    size_bytes: int
    sha256: str
    path: Path


@dataclass(frozen=True)
class VisionModelPack:
    manifest_path: Path
    root: Path
    manifest_sha256: str
    pack_id: str
    pack_version: str
    engine: str
    capabilities: tuple[str, ...]
    runtime_spdx: str
    weights_spdx: str
    redistribution_allowed: bool
    manifest: Mapping[str, Any]
    artifacts: Mapping[str, VisionArtifact]

    def public_identity(self) -> Dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "engine": self.engine,
            "capabilities": list(self.capabilities),
            "manifest_sha256": self.manifest_sha256,
            "runtime_spdx": self.runtime_spdx,
            "weights_spdx": self.weights_spdx,
            "redistribution_allowed": self.redistribution_allowed,
        }


WorkerRunner = Callable[
    [VisionModelPack, Sequence[Mapping[str, Any]], float, Optional[Callable[[], bool]]],
    Mapping[str, Any],
]


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_real_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise VisionModelPackError(f"{label} is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not resolved.is_dir()
    ):
        raise VisionModelPackError(f"{label} must be a real directory")
    return resolved


def _load_json_object(path: Path, maximum_bytes: int, label: str) -> tuple[Dict[str, Any], bytes]:
    try:
        before = path.stat()
        if (
            path.is_symlink()
            or _is_reparse_point(path)
            or not path.is_file()
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise VisionModelPackError(f"{label} size is invalid")
        raw = path.read_bytes()
        after = path.stat()
    except OSError as error:
        raise VisionModelPackError(f"{label} is unavailable") from error
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(raw) != before.st_size
    ):
        raise VisionModelPackError(f"{label} changed while it was read")

    def object_pairs(pairs: Iterable[tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise VisionModelPackError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    def reject_constant(_: str) -> object:
        raise VisionModelPackError(f"{label} contains a non-finite number")

    try:
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except VisionModelPackError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VisionModelPackError(f"{label} is not valid JSON") from error
    if not isinstance(decoded, dict):
        raise VisionModelPackError(f"{label} must be an object")
    return decoded, raw


def _exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    keys = set(value)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise VisionModelPackError(f"{label} fields are invalid")


def _bounded_number(
    value: Any, minimum: float, maximum: float, label: str
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise VisionModelPackError(f"{label} is outside the supported range")
    return float(value)


def _bounded_integer(
    value: Any, minimum: int, maximum: int, label: str
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise VisionModelPackError(f"{label} is outside the supported range")
    return value


def _bounded_text(
    value: Any, maximum: int, label: str, pattern: Optional[re.Pattern[str]] = None
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or (pattern is not None and not pattern.fullmatch(value))
    ):
        raise VisionModelPackError(f"{label} is invalid")
    return value


def _canonical_issue_type(
    value: Any,
    label: str,
    *,
    allow_other: bool = True,
) -> str:
    """Validate a model-provided type and return its product canonical code."""

    if not isinstance(value, str):
        raise VisionModelPackError(f"{label} is unsupported")
    canonical = ISSUE_TYPE_ALIASES.get(value, value)
    if canonical not in ISSUE_TYPES or (canonical == "other" and not allow_other):
        raise VisionModelPackError(f"{label} is unsupported")
    return canonical


def _resolve_relative(root: Path, relative: Any, label: str) -> tuple[str, Path]:
    value = _bounded_text(relative, 255, label, _RELATIVE_FILE)
    relative_path = Path(value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise VisionModelPackError(f"{label} must remain inside the model pack")
    candidate = root.joinpath(*relative_path.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise VisionModelPackError(f"{label} is unavailable") from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise VisionModelPackError(f"{label} escaped the model pack") from error
    return value.replace("\\", "/"), resolved


def _hash_file(path: Path, maximum_bytes: int) -> tuple[int, str]:
    try:
        before = path.stat()
    except OSError as error:
        raise VisionModelPackError("A model-pack artifact is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not path.is_file()
        or before.st_nlink != 1
        or before.st_size <= 0
        or before.st_size > maximum_bytes
    ):
        raise VisionModelPackError("A model-pack artifact is invalid")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(4 * 1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > maximum_bytes:
                    raise VisionModelPackError("The model pack is too large")
                digest.update(block)
        after = path.stat()
    except OSError as error:
        raise VisionModelPackError("A model-pack artifact could not be read") from error
    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise VisionModelPackError("A model-pack artifact changed while hashing")
    return total, digest.hexdigest()


def _validate_directory_inventory(
    root: Path,
    directory_relative: str,
    artifacts: Mapping[str, VisionArtifact],
    label: str,
) -> None:
    _, directory = _resolve_relative(root, directory_relative, label)
    directory = _safe_real_directory(directory, label)
    discovered: set[str] = set()
    for current_root, directories, files in os.walk(directory, followlinks=False):
        current = Path(current_root)
        if current.is_symlink() or _is_reparse_point(current):
            raise VisionModelPackError(f"{label} contains a linked directory")
        for name in directories:
            nested = current / name
            if nested.is_symlink() or _is_reparse_point(nested):
                raise VisionModelPackError(f"{label} contains a linked directory")
        for name in files:
            candidate = current / name
            if candidate.is_symlink() or _is_reparse_point(candidate):
                raise VisionModelPackError(f"{label} contains a linked file")
            relative = candidate.relative_to(root).as_posix()
            discovered.add(relative)
    expected = {
        relative
        for relative in artifacts
        if relative == directory_relative
        or relative.startswith(directory_relative.rstrip("/") + "/")
    }
    if not discovered or discovered != expected:
        raise VisionModelPackError(f"{label} does not match its hash inventory")


def load_model_pack(manifest_path: Path | str) -> VisionModelPack:
    """Validate a model pack without importing any inference dependency."""
    raw_path = Path(manifest_path)
    if not raw_path.is_absolute():
        raise VisionModelPackError("The model-pack manifest path must be absolute")
    try:
        manifest_resolved = raw_path.resolve(strict=True)
    except OSError as error:
        raise VisionModelPackError("The model-pack manifest is unavailable") from error
    if (
        raw_path.is_symlink()
        or _is_reparse_point(raw_path)
        or not manifest_resolved.is_file()
        or manifest_resolved.name not in MODEL_PACK_MANIFEST_NAMES
    ):
        raise VisionModelPackError("The model-pack manifest is invalid")
    root = _safe_real_directory(manifest_resolved.parent, "Model-pack directory")
    manifest, raw = _load_json_object(
        manifest_resolved, MAX_MANIFEST_BYTES, "Model-pack manifest"
    )
    _exact_keys(
        manifest,
        {
            "schema_version",
            "pack_id",
            "pack_version",
            "engine",
            "license",
            "artifacts",
        },
        {"model", "ocr"},
        "Model-pack manifest",
    )
    if manifest["schema_version"] != MODEL_PACK_SCHEMA:
        raise VisionModelPackError("The model-pack schema is unsupported")
    pack_id = _bounded_text(manifest["pack_id"], 64, "pack_id", _PACK_ID)
    pack_version = _bounded_text(
        manifest["pack_version"], 32, "pack_version", _VERSION
    )
    engine = _bounded_text(manifest["engine"], 64, "engine")
    if engine not in _ENGINES:
        raise VisionModelPackError("The model-pack engine is unsupported")

    licence = manifest["license"]
    if not isinstance(licence, Mapping):
        raise VisionModelPackError("Model-pack license must be an object")
    _exact_keys(
        licence,
        {
            "runtime_spdx",
            "weights_spdx",
            "commercial_use_confirmed",
            "redistribution_allowed",
            "notice",
        },
        set(),
        "Model-pack license",
    )
    runtime_spdx = _bounded_text(
        licence["runtime_spdx"], 64, "runtime_spdx", _SPDX
    )
    weights_spdx = _bounded_text(
        licence["weights_spdx"], 64, "weights_spdx", _SPDX
    )
    if licence["commercial_use_confirmed"] is not True:
        raise VisionModelPackError(
            "The model-pack installer must confirm commercial-use rights"
        )
    if not isinstance(licence["redistribution_allowed"], bool):
        raise VisionModelPackError("redistribution_allowed must be boolean")
    _bounded_text(licence["notice"], 2_048, "license notice")

    artifact_rows = manifest["artifacts"]
    if (
        not isinstance(artifact_rows, list)
        or not 1 <= len(artifact_rows) <= MAX_PACK_FILES
    ):
        raise VisionModelPackError("Model-pack artifacts are invalid")
    artifacts: Dict[str, VisionArtifact] = {}
    declared_total = 0
    for row in artifact_rows:
        if not isinstance(row, Mapping):
            raise VisionModelPackError("A model-pack artifact is invalid")
        _exact_keys(
            row,
            {"path", "size_bytes", "sha256"},
            set(),
            "Model-pack artifact",
        )
        relative, path = _resolve_relative(root, row["path"], "Artifact path")
        if relative in artifacts:
            raise VisionModelPackError("Model-pack artifact paths must be unique")
        size = _bounded_integer(
            row["size_bytes"], 1, MAX_PACK_BYTES, "Artifact size"
        )
        sha256 = _bounded_text(row["sha256"], 64, "Artifact digest", _SHA256)
        actual_size, actual_sha256 = _hash_file(path, MAX_PACK_BYTES)
        if actual_size != size or actual_sha256 != sha256:
            raise VisionModelPackError("A model-pack artifact failed integrity checks")
        declared_total += size
        if declared_total > MAX_PACK_BYTES:
            raise VisionModelPackError("The model pack is too large")
        artifacts[relative] = VisionArtifact(relative, size, sha256, path)

    model = manifest.get("model")
    capabilities: List[str] = []
    if engine == "onnx_yolo_v8":
        if not isinstance(model, Mapping):
            raise VisionModelPackError("The ONNX model configuration is required")
        _exact_keys(
            model,
            {
                "file",
                "input_width",
                "input_height",
                "labels",
                "confidence_threshold",
                "nms_iou_threshold",
                "max_detections",
            },
            {"execution_provider"},
            "ONNX model configuration",
        )
        model_file = _bounded_text(model["file"], 255, "ONNX model file", _RELATIVE_FILE)
        if model_file not in artifacts or Path(model_file).suffix.casefold() != ".onnx":
            raise VisionModelPackError("The ONNX model file is not inventoried")
        _bounded_integer(model["input_width"], 160, 2_048, "ONNX input width")
        _bounded_integer(model["input_height"], 160, 2_048, "ONNX input height")
        _bounded_number(
            model["confidence_threshold"], 0.01, 0.99, "confidence threshold"
        )
        _bounded_number(model["nms_iou_threshold"], 0.01, 0.99, "NMS threshold")
        _bounded_integer(
            model["max_detections"],
            1,
            MAX_PREDICTIONS_PER_CANDIDATE,
            "max_detections",
        )
        execution_provider = model.get("execution_provider", "auto")
        if execution_provider not in {"auto", "cpu", "cuda"}:
            raise VisionModelPackError("ONNX execution_provider is unsupported")
        labels = model["labels"]
        if not isinstance(labels, list) or not 1 <= len(labels) <= 256:
            raise VisionModelPackError("ONNX labels are invalid")
        class_ids: set[int] = set()
        for row in labels:
            if not isinstance(row, Mapping):
                raise VisionModelPackError("An ONNX label is invalid")
            _exact_keys(
                row,
                {"class_id", "issue_type", "display_name"},
                set(),
                "ONNX label",
            )
            class_id = _bounded_integer(row["class_id"], 0, 255, "class_id")
            if class_id in class_ids:
                raise VisionModelPackError("ONNX class IDs must be unique")
            class_ids.add(class_id)
            _canonical_issue_type(row["issue_type"], "An ONNX issue type")
            _bounded_text(row["display_name"], 128, "ONNX display name")
        if class_ids != set(range(len(labels))):
            raise VisionModelPackError("ONNX class IDs must be contiguous from zero")
        capabilities.append("defect_detection")
    elif engine == "transformers_clip_zero_shot":
        if not isinstance(model, Mapping):
            raise VisionModelPackError("The CLIP model configuration is required")
        _exact_keys(
            model,
            {
                "directory",
                "prompts",
                "negative_prompts",
                "confidence_threshold",
                "top_k",
            },
            {"device"},
            "CLIP model configuration",
        )
        directory = _bounded_text(
            model["directory"], 255, "CLIP model directory", _RELATIVE_FILE
        )
        _validate_directory_inventory(root, directory, artifacts, "CLIP model directory")
        prompts = model["prompts"]
        if not isinstance(prompts, Mapping) or not 1 <= len(prompts) <= len(ISSUE_TYPES):
            raise VisionModelPackError("CLIP prompts are invalid")
        prompt_count = 0
        for issue_type, values in prompts.items():
            _canonical_issue_type(
                issue_type,
                "A CLIP prompt issue type",
                allow_other=False,
            )
            if not isinstance(values, list) or not 1 <= len(values) <= 8:
                raise VisionModelPackError("CLIP prompt variants are invalid")
            for value in values:
                _bounded_text(value, 256, "CLIP prompt")
                prompt_count += 1
        negatives = model["negative_prompts"]
        if not isinstance(negatives, list) or not 2 <= len(negatives) <= 16:
            raise VisionModelPackError("CLIP requires normal-road negative prompts")
        for value in negatives:
            _bounded_text(value, 256, "CLIP negative prompt")
            prompt_count += 1
        if prompt_count > 128:
            raise VisionModelPackError("The CLIP prompt set is too large")
        _bounded_number(
            model["confidence_threshold"], 0.01, 0.99, "confidence threshold"
        )
        _bounded_integer(
            model["top_k"], 1, MAX_PREDICTIONS_PER_CANDIDATE, "top_k"
        )
        if model.get("device", "auto") not in {"auto", "cpu", "cuda"}:
            raise VisionModelPackError("CLIP device is unsupported")
        capabilities.append("candidate_classification")
    elif model is not None:
        raise VisionModelPackError("OCR-only packs cannot declare a model")

    ocr = manifest.get("ocr")
    if engine == "easyocr_ocr_only" and not isinstance(ocr, Mapping):
        raise VisionModelPackError("OCR-only packs require OCR configuration")
    if ocr is not None:
        if not isinstance(ocr, Mapping):
            raise VisionModelPackError("OCR configuration must be an object")
        _exact_keys(
            ocr,
            {
                "engine",
                "model_storage_directory",
                "languages",
                "minimum_confidence",
                "max_observations",
            },
            {"device"},
            "OCR configuration",
        )
        if ocr["engine"] != "easyocr":
            raise VisionModelPackError("The OCR engine is unsupported")
        directory = _bounded_text(
            ocr["model_storage_directory"],
            255,
            "OCR model directory",
            _RELATIVE_FILE,
        )
        _validate_directory_inventory(root, directory, artifacts, "OCR model directory")
        languages = ocr["languages"]
        if (
            not isinstance(languages, list)
            or not 1 <= len(languages) <= 3
            or len(set(languages)) != len(languages)
            or any(value not in {"ko", "en"} for value in languages)
            or "ko" not in languages
        ):
            raise VisionModelPackError("OCR languages must include Korean")
        _bounded_number(
            ocr["minimum_confidence"], 0.01, 0.99, "OCR confidence threshold"
        )
        _bounded_integer(
            ocr["max_observations"],
            1,
            MAX_OCR_PER_CANDIDATE,
            "max_observations",
        )
        if ocr.get("device", "auto") not in {"auto", "cpu", "cuda"}:
            raise VisionModelPackError("OCR device is unsupported")
        capabilities.append("ocr")

    return VisionModelPack(
        manifest_path=manifest_resolved,
        root=root,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        pack_id=pack_id,
        pack_version=pack_version,
        engine=engine,
        capabilities=tuple(capabilities),
        runtime_spdx=runtime_spdx,
        weights_spdx=weights_spdx,
        redistribution_allowed=bool(licence["redistribution_allowed"]),
        manifest=manifest,
        artifacts=artifacts,
    )


def _fallback_inference(
    warning_code: str, pack: Optional[VisionModelPack] = None
) -> Dict[str, Any]:
    return {
        "schema_version": INFERENCE_SCHEMA,
        "mode": "scene_fallback",
        "status": "provider_unavailable",
        "provider": pack.engine if pack is not None else "none",
        "pack_id": pack.pack_id if pack is not None else None,
        "pack_version": pack.pack_version if pack is not None else None,
        "manifest_sha256": pack.manifest_sha256 if pack is not None else None,
        "capabilities": list(pack.capabilities) if pack is not None else [],
        "predictions": [],
        "ocr_observations": [],
        "duration_ms": 0,
        "warning_code": warning_code,
    }


def _safe_box(value: Any) -> Optional[Dict[str, float]]:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise VisionInferenceError("The vision worker returned an invalid box")
    result: Dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = value[key]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
        ):
            raise VisionInferenceError("The vision worker returned an invalid box")
        result[key] = round(float(number), 8)
    if (
        not 0 <= result["x"] <= 1
        or not 0 <= result["y"] <= 1
        or not 0 < result["width"] <= 1
        or not 0 < result["height"] <= 1
        or result["x"] + result["width"] > 1.00000001
        or result["y"] + result["height"] > 1.00000001
    ):
        raise VisionInferenceError("The vision worker returned an invalid box")
    return result


def _validate_worker_result(
    pack: VisionModelPack,
    candidate_ids: Sequence[str],
    payload: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "schema_version",
        "pack_id",
        "pack_version",
        "manifest_sha256",
        "results",
    }:
        raise VisionInferenceError("The vision worker returned an invalid envelope")
    if (
        payload["schema_version"] != WORKER_SCHEMA
        or payload["pack_id"] != pack.pack_id
        or payload["pack_version"] != pack.pack_version
        or payload["manifest_sha256"] != pack.manifest_sha256
        or not isinstance(payload["results"], list)
        or len(payload["results"]) != len(candidate_ids)
    ):
        raise VisionInferenceError("The vision worker identity did not match")
    expected = set(candidate_ids)
    normalized: Dict[str, Dict[str, Any]] = {}
    for result in payload["results"]:
        if not isinstance(result, Mapping) or set(result) != {
            "candidate_id",
            "predictions",
            "ocr_observations",
            "duration_ms",
        }:
            raise VisionInferenceError("The vision worker returned an invalid result")
        candidate_id = result["candidate_id"]
        if (
            not isinstance(candidate_id, str)
            or candidate_id not in expected
            or candidate_id in normalized
        ):
            raise VisionInferenceError("The vision worker candidate identity is invalid")
        duration_ms = result["duration_ms"]
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not 0 <= duration_ms <= 3_600_000
        ):
            raise VisionInferenceError("The vision worker duration is invalid")
        predictions = result["predictions"]
        if (
            not isinstance(predictions, list)
            or len(predictions) > MAX_PREDICTIONS_PER_CANDIDATE
        ):
            raise VisionInferenceError("The vision worker predictions are invalid")
        safe_predictions: List[Dict[str, Any]] = []
        for prediction in predictions:
            if not isinstance(prediction, Mapping) or set(prediction) != {
                "issue_type",
                "confidence",
                "box",
                "source_label",
                "calibration",
            }:
                raise VisionInferenceError("A vision prediction is invalid")
            issue_type = prediction["issue_type"]
            confidence = prediction["confidence"]
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or not 0 <= float(confidence) <= 1
            ):
                raise VisionInferenceError("A vision prediction value is invalid")
            try:
                canonical_issue_type = _canonical_issue_type(
                    issue_type,
                    "A vision prediction issue type",
                )
            except VisionModelPackError as error:
                raise VisionInferenceError(
                    "A vision prediction value is invalid"
                ) from error
            source_label = prediction["source_label"]
            calibration = prediction["calibration"]
            if (
                not isinstance(source_label, str)
                or not source_label
                or len(source_label) > 128
                or not isinstance(calibration, str)
                or calibration not in {"model_score", "zero_shot_relative"}
            ):
                raise VisionInferenceError("A vision prediction label is invalid")
            safe_predictions.append(
                {
                    "issue_type": canonical_issue_type,
                    "confidence": round(float(confidence), 8),
                    "box": _safe_box(prediction["box"]),
                    "source_label": source_label,
                    "calibration": calibration,
                }
            )
        safe_predictions.sort(key=lambda item: item["confidence"], reverse=True)

        observations = result["ocr_observations"]
        if (
            not isinstance(observations, list)
            or len(observations) > MAX_OCR_PER_CANDIDATE
        ):
            raise VisionInferenceError("The vision worker OCR result is invalid")
        safe_ocr: List[Dict[str, Any]] = []
        for observation in observations:
            if not isinstance(observation, Mapping) or set(observation) != {
                "text",
                "confidence",
                "box",
                "kind",
            }:
                raise VisionInferenceError("An OCR observation is invalid")
            text = observation["text"]
            confidence = observation["confidence"]
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 500
                or "\x00" in text
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or not 0 <= float(confidence) <= 1
                or observation["kind"] != "sign_or_tunnel_text_unverified"
            ):
                raise VisionInferenceError("An OCR observation value is invalid")
            safe_ocr.append(
                {
                    "text": text.strip(),
                    "confidence": round(float(confidence), 8),
                    "box": _safe_box(observation["box"]),
                    "kind": "sign_or_tunnel_text_unverified",
                }
            )
        normalized[candidate_id] = {
            "predictions": safe_predictions,
            "ocr_observations": safe_ocr,
            "duration_ms": duration_ms,
        }
    if set(normalized) != expected:
        raise VisionInferenceError("The vision worker omitted a candidate")
    return normalized


class LocalVisionInferenceService:
    """Run an explicitly installed pack or return a labelled scene fallback."""

    def __init__(
        self,
        *,
        model_pack_path: Optional[str | Path],
        timeout_seconds: float = 180.0,
        runner: Optional[WorkerRunner] = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 5 <= float(timeout_seconds) <= 3_600
        ):
            raise ValueError("vision inference timeout is outside the supported range")
        self._timeout_seconds = float(timeout_seconds)
        self._runner = runner or self._run_isolated_worker
        self._pack: Optional[VisionModelPack] = None
        self._warning_code: Optional[str] = None
        if model_pack_path is None or not str(model_pack_path).strip():
            self._warning_code = "model_pack_not_configured"
        else:
            try:
                self._pack = load_model_pack(Path(model_pack_path))
            except VisionModelPackError:
                # The bridge must still start so scene candidate review remains
                # usable.  Details stay in local diagnostics, never in API JSON.
                self._warning_code = "model_pack_invalid"

    def status(self) -> Dict[str, Any]:
        if self._pack is None:
            return {
                "enabled": False,
                "mode": "scene_fallback",
                "warning_code": self._warning_code,
            }
        return {
            "enabled": True,
            "mode": "local_model",
            "warning_code": None,
            **self._pack.public_identity(),
        }

    def infer_candidates(
        self,
        candidates: Sequence[Mapping[str, Any]],
        run_directory: Path,
        *,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
        if not isinstance(candidates, (list, tuple)) or len(candidates) > MAX_CANDIDATES:
            raise VisionInferenceError("The vision candidate batch is invalid")
        candidate_ids: List[str] = []
        for candidate in candidates:
            candidate_id = candidate.get("candidate_id") if isinstance(candidate, Mapping) else None
            if (
                not isinstance(candidate_id, str)
                or not _CANDIDATE_ID.fullmatch(candidate_id)
                or candidate_id in candidate_ids
            ):
                raise VisionInferenceError("A vision candidate identity is invalid")
            candidate_ids.append(candidate_id)
        if self._pack is None:
            warning = self._warning_code or "model_pack_not_configured"
            results = {
                candidate_id: _fallback_inference(warning)
                for candidate_id in candidate_ids
            }
            return results, self._summary(results)

        if cancel is not None and cancel():
            raise VisionInferenceCancelled("Vision inference was cancelled")
        root = _safe_real_directory(Path(run_directory), "Candidate artifact directory")
        request_rows: List[Dict[str, Any]] = []
        try:
            for candidate in candidates:
                image = candidate.get("representative_image")
                if not isinstance(image, Mapping):
                    raise VisionInferenceError("Candidate image metadata is invalid")
                relative = image.get("relative_path")
                size_bytes = image.get("size_bytes")
                sha256 = image.get("sha256")
                if (
                    not isinstance(relative, str)
                    or not _RELATIVE_FILE.fullmatch(relative)
                    or Path(relative).is_absolute()
                    or ".." in Path(relative).parts
                    or isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                    or not 1 <= size_bytes <= MAX_IMAGE_BYTES
                    or not isinstance(sha256, str)
                    or not _SHA256.fullmatch(sha256)
                ):
                    raise VisionInferenceError("Candidate image metadata is invalid")
                image_path = (root / relative).resolve(strict=True)
                if (
                    image_path.parent != root
                    or image_path.is_symlink()
                    or _is_reparse_point(image_path)
                ):
                    raise VisionInferenceError("Candidate image path is invalid")
                actual_size, actual_sha256 = _hash_file(image_path, MAX_IMAGE_BYTES)
                if actual_size != size_bytes or actual_sha256 != sha256:
                    raise VisionInferenceError("Candidate image integrity failed")
                request_rows.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "image_path": str(image_path),
                        "image_size_bytes": size_bytes,
                        "image_sha256": sha256,
                    }
                )
        except OSError as error:
            raise VisionInferenceError("Candidate evidence is unavailable") from error

        try:
            raw = self._runner(
                self._pack,
                request_rows,
                self._timeout_seconds,
                cancel,
            )
            validated = _validate_worker_result(self._pack, candidate_ids, raw)
        except VisionInferenceCancelled:
            raise
        except VisionInferenceTimeout:
            results = {
                candidate_id: {
                    **_fallback_inference("model_timeout", self._pack),
                    "status": "timed_out",
                }
                for candidate_id in candidate_ids
            }
            return results, self._summary(results)
        except Exception:
            results = {
                candidate_id: {
                    **_fallback_inference("model_inference_failed", self._pack),
                    "status": "failed",
                }
                for candidate_id in candidate_ids
            }
            return results, self._summary(results)

        results: Dict[str, Dict[str, Any]] = {}
        for candidate_id in candidate_ids:
            item = validated[candidate_id]
            has_output = bool(item["predictions"] or item["ocr_observations"])
            results[candidate_id] = {
                "schema_version": INFERENCE_SCHEMA,
                "mode": "local_model",
                "status": "classified" if has_output else "no_detection",
                "provider": self._pack.engine,
                "pack_id": self._pack.pack_id,
                "pack_version": self._pack.pack_version,
                "manifest_sha256": self._pack.manifest_sha256,
                "capabilities": list(self._pack.capabilities),
                "predictions": item["predictions"],
                "ocr_observations": item["ocr_observations"],
                "duration_ms": item["duration_ms"],
                "warning_code": None,
            }
        return results, self._summary(results)

    def _summary(self, results: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
        statuses: Dict[str, int] = {}
        for value in results.values():
            status = str(value["status"])
            statuses[status] = statuses.get(status, 0) + 1
        if self._pack is None:
            identity: Dict[str, Any] = {
                "engine": "none",
                "pack_id": None,
                "pack_version": None,
                "manifest_sha256": None,
                "capabilities": [],
                "runtime_spdx": None,
                "weights_spdx": None,
                "redistribution_allowed": None,
            }
            mode = "scene_fallback"
        else:
            identity = self._pack.public_identity()
            mode = (
                "local_model"
                if not results
                or any(value["mode"] == "local_model" for value in results.values())
                else "scene_fallback"
            )
        return {
            "schema_version": INFERENCE_SCHEMA,
            "mode": mode,
            **identity,
            "candidate_count": len(results),
            "status_counts": statuses,
            "warning_code": next(
                (
                    value["warning_code"]
                    for value in results.values()
                    if value.get("warning_code")
                ),
                None,
            ),
        }

    @staticmethod
    def _worker_argv() -> List[str]:
        executable = Path(sys.executable).resolve(strict=True)
        if getattr(sys, "frozen", False):
            return [str(executable), "--vision-worker"]
        worker = Path(__file__).with_name("vision_worker.py").resolve(strict=True)
        return [str(executable), str(worker)]

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.kill()
        except OSError:
            return
        try:
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def _assign_windows_job(process: subprocess.Popen[bytes]):
        """Apply a fail-closed Windows process-memory and lifetime bound."""
        if os.name != "nt":
            return None
        try:
            import win32api
            import win32con
            import win32job

            job = win32job.CreateJobObject(None, "")
            information = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            information["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            )
            information["ProcessMemoryLimit"] = (
                MAX_WORKER_PROCESS_MEMORY_BYTES
            )
            win32job.SetInformationJobObject(
                job,
                win32job.JobObjectExtendedLimitInformation,
                information,
            )
            process_handle = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA
                | win32con.PROCESS_TERMINATE
                | win32con.PROCESS_QUERY_INFORMATION,
                False,
                process.pid,
            )
            try:
                win32job.AssignProcessToJobObject(job, process_handle)
            finally:
                win32api.CloseHandle(process_handle)
            return job
        except Exception as error:
            raise VisionInferenceError(
                "The vision worker resource boundary is unavailable"
            ) from error

    @staticmethod
    def _close_windows_job(job) -> None:
        if job is None:
            return
        try:
            import win32api

            win32api.CloseHandle(job)
        except Exception:
            pass

    def _run_isolated_worker(
        self,
        pack: VisionModelPack,
        candidates: Sequence[Mapping[str, Any]],
        timeout_seconds: float,
        cancel: Optional[Callable[[], bool]],
    ) -> Mapping[str, Any]:
        request = {
            "schema_version": WORKER_SCHEMA,
            "manifest_path": str(pack.manifest_path),
            "manifest_sha256": pack.manifest_sha256,
            "candidates": list(candidates),
            "limits": {
                "max_image_bytes": MAX_IMAGE_BYTES,
                "max_image_pixels": MAX_IMAGE_PIXELS,
                "max_predictions": MAX_PREDICTIONS_PER_CANDIDATE,
                "max_ocr_observations": MAX_OCR_PER_CANDIDATE,
            },
        }
        encoded = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise VisionInferenceError("The vision worker request is too large")
        work_parent = _safe_real_directory(
            Path(tempfile.gettempdir()), "Vision worker temporary directory"
        )
        work_directory = Path(
            tempfile.mkdtemp(prefix="TrafficSuperVisionVision-", dir=work_parent)
        )
        request_path = work_directory / "request.json"
        output_path = work_directory / "result.json"
        request_path.write_bytes(encoded)
        argv = [
            *self._worker_argv(),
            "--request",
            str(request_path),
            "--output",
            str(output_path),
        ]
        allowed_environment = {}
        for name in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "LOCALAPPDATA",
            "APPDATA",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "CUDA_PATH",
            "CUDA_VISIBLE_DEVICES",
            "TRAFFICSUPERVISION_EXTERNAL_MODEL_RUNTIME_PACK",
        ):
            value = os.environ.get(name)
            if value:
                allowed_environment[name] = value
        allowed_environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "DO_NOT_TRACK": "1",
                "NO_PROXY": "*",
                "no_proxy": "*",
                "TOKENIZERS_PARALLELISM": "false",
                "OMP_NUM_THREADS": "2",
                "ORT_NUM_THREADS": "2",
                # The CLIP engine is explicitly PyTorch-only.  Prevent an
                # ambient TensorFlow/Keras installation from being imported
                # by Transformers inside the stripped worker environment;
                # TensorFlow may otherwise traverse user config directories
                # that are intentionally unavailable to the worker.
                "USE_TF": "0",
                "USE_FLAX": "0",
                "USE_TORCH": "1",
            }
        )
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process: Optional[subprocess.Popen[bytes]] = None
        windows_job = None
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                cwd=str(pack.root),
                env=allowed_environment,
                close_fds=True,
                creationflags=creation_flags,
            )
            try:
                windows_job = self._assign_windows_job(process)
            except VisionInferenceError:
                self._terminate(process)
                raise
            while process.poll() is None:
                if cancel is not None and cancel():
                    self._terminate(process)
                    raise VisionInferenceCancelled("Vision inference was cancelled")
                if time.monotonic() - started > timeout_seconds:
                    self._terminate(process)
                    raise VisionInferenceTimeout("Vision inference timed out")
                time.sleep(0.05)
            if process.returncode != 0:
                raise VisionInferenceError("The vision worker failed")
            if (
                not output_path.is_file()
                or output_path.is_symlink()
                or _is_reparse_point(output_path)
            ):
                raise VisionInferenceError("The vision worker output is unavailable")
            output_size = output_path.stat().st_size
            if not 1 <= output_size <= MAX_WORKER_OUTPUT_BYTES:
                raise VisionInferenceError("The vision worker output size is invalid")
            output, _ = _load_json_object(
                output_path,
                MAX_WORKER_OUTPUT_BYTES,
                "Vision worker output",
            )
            return output
        finally:
            if process is not None and process.poll() is None:
                self._terminate(process)
            self._close_windows_job(windows_job)
            try:
                if (
                    work_directory.parent == work_parent
                    and work_directory.name.startswith(
                        "TrafficSuperVisionVision-"
                    )
                ):
                    shutil.rmtree(work_directory)
            except OSError:
                pass
