"""Verified local Transformers SAM 2.1 adapter.

The adapter owns one separately installed, content-verified Model Pack.  It
never downloads at runtime and never turns a mask into an Annotation,
Incident, or training member.  A caller supplies one normalized positive
click; the adapter returns only a bounded polygon proposal.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


PACK_SCHEMA = "trafficvisson.sam2-transformers-model-pack.v1"
DEFAULT_PACK_ID = "trafficvisson-facebook-sam2.1-hiera-tiny"
DEFAULT_PACK_VERSION = "0.1.0"
DEFAULT_PACK_DIRECTORY = (
    "trafficvisson-facebook-sam2.1-hiera-tiny-0.1.0"
)
MANIFEST_NAME = "trafficvisson-sam2-model-pack.json"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 4 * 1024 * 1024 * 1024
MAX_INPUT_IMAGE_BYTES = 64 * 1024 * 1024
MAX_POLYGON_POINTS = 256

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_PACK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PACK_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,31}$")
_REVISION = re.compile(r"^[a-f0-9]{40}$")
_SAFE_RELATIVE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+-]*(?:/[A-Za-z0-9][A-Za-z0-9._+-]*)*$"
)


class TransformersSam2AdapterError(ValueError):
    """The local pack or inference result violated the SAM2 contract."""


MaskPredictor = Callable[[Path, tuple[float, float]], Any]
TrackPredictor = Callable[
    [Sequence[Path], int, tuple[float, float]],
    Mapping[str, Any],
]
RuntimeProbe = Callable[[], Mapping[str, Any]]


def default_program_data_pack_path() -> Path:
    program_data = os.environ.get("PROGRAMDATA")
    if not program_data:
        program_data = r"C:\ProgramData"
    return (
        Path(program_data)
        / "TrafficVisson"
        / "ModelPacks"
        / DEFAULT_PACK_DIRECTORY
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _safe_existing_root(path: Path) -> Path:
    if not path.is_absolute():
        raise TransformersSam2AdapterError(
            "SAM2 model pack path must be absolute"
        )
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise TransformersSam2AdapterError(
            "SAM2 model pack is not installed"
        ) from error
    if not resolved.is_dir() or resolved.is_symlink() or _is_reparse_point(
        resolved
    ):
        raise TransformersSam2AdapterError(
            "SAM2 model pack root is not a regular directory"
        )
    cursor = resolved
    while cursor != cursor.parent:
        if cursor.is_symlink() or _is_reparse_point(cursor):
            raise TransformersSam2AdapterError(
                "SAM2 model pack path contains a link"
            )
        cursor = cursor.parent
    return resolved


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > MAX_ARTIFACT_BYTES:
                raise TransformersSam2AdapterError(
                    "SAM2 model artifact exceeds the safe size limit"
                )
            digest.update(block)
    return digest.hexdigest(), size


def _manifest_bytes(path: Path) -> bytes:
    if (
        not path.is_file()
        or path.is_symlink()
        or _is_reparse_point(path)
    ):
        raise TransformersSam2AdapterError(
            "SAM2 model pack manifest is unavailable"
        )
    size = path.stat().st_size
    if not 2 <= size <= MAX_MANIFEST_BYTES:
        raise TransformersSam2AdapterError(
            "SAM2 model pack manifest size is invalid"
        )
    return path.read_bytes()


def _load_verified_manifest(root: Path) -> tuple[dict[str, Any], str]:
    manifest_path = root / MANIFEST_NAME
    raw = _manifest_bytes(manifest_path)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TransformersSam2AdapterError(
            "SAM2 model pack manifest is invalid JSON"
        ) from error
    expected_keys = {
        "schema_version",
        "pack_id",
        "pack_version",
        "provider",
        "model_name",
        "model_version",
        "model_directory",
        "model_artifact_path",
        "model_artifact_sha256",
        "source_repo_id",
        "source_revision",
        "source_url",
        "weights_spdx",
        "runtime_spdx",
        "commercial_use_confirmed",
        "redistribution_allowed",
        "gpu_required",
        "cpu_fallback",
        "recommended_min_vram_mb",
        "cuda_min_version",
        "review_only",
        "artifacts",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise TransformersSam2AdapterError(
            "SAM2 model pack manifest fields are invalid"
        )
    if (
        value["schema_version"] != PACK_SCHEMA
        or not isinstance(value["pack_id"], str)
        or _PACK_ID.fullmatch(value["pack_id"]) is None
        or value["pack_id"] != DEFAULT_PACK_ID
        or not isinstance(value["pack_version"], str)
        or _PACK_VERSION.fullmatch(value["pack_version"]) is None
        or value["pack_version"] != DEFAULT_PACK_VERSION
        or value["provider"] != "transformers_sam2"
        or value["model_name"] != "facebook/sam2.1-hiera-tiny"
        or value["model_version"] != value["source_revision"]
        or value["source_repo_id"] != "facebook/sam2.1-hiera-tiny"
        or value["source_url"]
        != "https://huggingface.co/facebook/sam2.1-hiera-tiny"
        or not isinstance(value["source_revision"], str)
        or _REVISION.fullmatch(value["source_revision"]) is None
        or value["weights_spdx"] != "Apache-2.0"
        or value["runtime_spdx"] != "Apache-2.0"
        or value["commercial_use_confirmed"] is not True
        or value["redistribution_allowed"] is not True
        or value["gpu_required"] is not True
        or value["cpu_fallback"] is not True
        or value["review_only"] is not True
        or value["cuda_min_version"] != "12.0"
        or value["recommended_min_vram_mb"] != 4096
    ):
        raise TransformersSam2AdapterError(
            "SAM2 model identity, license, or runtime policy is invalid"
        )
    model_directory = value["model_directory"]
    model_artifact_path = value["model_artifact_path"]
    model_artifact_sha256 = value["model_artifact_sha256"]
    if (
        model_directory != "model"
        or model_artifact_path != "model/model.safetensors"
        or not isinstance(model_artifact_sha256, str)
        or _SHA256.fullmatch(model_artifact_sha256) is None
    ):
        raise TransformersSam2AdapterError(
            "SAM2 model artifact identity is invalid"
        )
    artifacts = value["artifacts"]
    if (
        not isinstance(artifacts, list)
        or not 5 <= len(artifacts) <= 16
    ):
        raise TransformersSam2AdapterError(
            "SAM2 model artifact manifest is invalid"
        )
    seen = set()
    total = 0
    for row in artifacts:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise TransformersSam2AdapterError(
                "SAM2 model artifact record is invalid"
            )
        relative = row["path"]
        size = row["size_bytes"]
        expected_sha = row["sha256"]
        if (
            not isinstance(relative, str)
            or _SAFE_RELATIVE.fullmatch(relative) is None
            or "\\" in relative
            or relative in seen
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= MAX_ARTIFACT_BYTES
            or not isinstance(expected_sha, str)
            or _SHA256.fullmatch(expected_sha) is None
        ):
            raise TransformersSam2AdapterError(
                "SAM2 model artifact record is invalid"
            )
        seen.add(relative)
        artifact = (root / Path(*relative.split("/"))).resolve(strict=True)
        if (
            root not in artifact.parents
            or not artifact.is_file()
            or artifact.is_symlink()
            or _is_reparse_point(artifact)
        ):
            raise TransformersSam2AdapterError(
                "SAM2 model artifact escaped its pack"
            )
        actual_sha, actual_size = _sha256_file(artifact)
        if actual_size != size or actual_sha != expected_sha:
            raise TransformersSam2AdapterError(
                "SAM2 model artifact hash mismatch"
            )
        total += actual_size
        if total > MAX_TOTAL_ARTIFACT_BYTES:
            raise TransformersSam2AdapterError(
                "SAM2 model pack exceeds the safe total size"
            )
    required = {
        "model/README.md",
        "model/config.json",
        "model/model.safetensors",
        "model/preprocessor_config.json",
        "model/processor_config.json",
    }
    if not required.issubset(seen):
        raise TransformersSam2AdapterError(
            "SAM2 model pack is incomplete"
        )
    model_record = next(
        row for row in artifacts if row["path"] == model_artifact_path
    )
    if model_record["sha256"] != model_artifact_sha256:
        raise TransformersSam2AdapterError(
            "SAM2 primary model hash is inconsistent"
        )
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    return value, manifest_sha256


def _default_runtime_probe() -> Mapping[str, Any]:
    try:
        import cv2
        import numpy
        import PIL
        import torch
        import transformers
    except Exception:
        return {
            "state": "unavailable",
            "error_code": "sam2_runtime_dependency_missing",
        }
    del cv2, numpy, PIL, transformers
    cuda_ready = bool(torch.cuda.is_available())
    return {
        "state": "ready",
        "error_code": None,
        "device": "cuda" if cuda_ready else "cpu",
        "cuda_ready": cuda_ready,
    }


def _polygon_from_mask(
    mask: Any,
    point: tuple[float, float],
) -> list[list[float]]:
    try:
        import cv2
        import numpy as np
    except Exception as error:
        raise TransformersSam2AdapterError(
            "SAM2 polygon runtime is unavailable"
        ) from error
    array = np.asarray(mask)
    if array.ndim != 2 or array.size < 4:
        raise TransformersSam2AdapterError(
            "SAM2 mask has an invalid shape"
        )
    binary = (array > 0).astype("uint8")
    height, width = binary.shape
    if height < 2 or width < 2 or not binary.any():
        raise TransformersSam2AdapterError(
            "SAM2 mask is empty"
        )
    contours, _hierarchy = cv2.findContours(
        binary,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    if not contours:
        raise TransformersSam2AdapterError(
            "SAM2 mask has no contour"
        )
    click = (
        min(max(point[0] * width, 0.0), width - 1.0),
        min(max(point[1] * height, 0.0), height - 1.0),
    )
    containing = [
        contour
        for contour in contours
        if cv2.pointPolygonTest(contour, click, False) >= 0
    ]
    contour = max(
        containing or contours,
        key=lambda item: float(cv2.contourArea(item)),
    )
    if float(cv2.contourArea(contour)) < 2.0:
        raise TransformersSam2AdapterError(
            "SAM2 mask contour is degenerate"
        )
    perimeter = float(cv2.arcLength(contour, True))
    approximated = contour
    for ratio in (0.002, 0.004, 0.008, 0.016, 0.032):
        approximated = cv2.approxPolyDP(
            contour,
            max(0.5, perimeter * ratio),
            True,
        )
        if len(approximated) <= MAX_POLYGON_POINTS:
            break
    raw_points = approximated.reshape(-1, 2)
    if not 3 <= len(raw_points) <= MAX_POLYGON_POINTS:
        raise TransformersSam2AdapterError(
            "SAM2 mask contour exceeds the polygon limit"
        )
    points: list[list[float]] = []
    for x, y in raw_points:
        nx = min(1.0, max(0.0, float(x) / max(width - 1, 1)))
        ny = min(1.0, max(0.0, float(y) / max(height - 1, 1)))
        point_value = [round(nx, 8), round(ny, 8)]
        if not points or points[-1] != point_value:
            points.append(point_value)
    if len({tuple(item) for item in points}) < 3:
        raise TransformersSam2AdapterError(
            "SAM2 polygon is degenerate"
        )
    return points


class TransformersSam2Adapter:
    """Local SAM2 adapter backed by one verified safetensors model pack."""

    def __init__(
        self,
        model_pack_path: Path,
        *,
        predictor: Optional[MaskPredictor] = None,
        tracker: Optional[TrackPredictor] = None,
        runtime_probe: RuntimeProbe = _default_runtime_probe,
    ) -> None:
        self._pack_path = Path(model_pack_path)
        self._injected_predictor = predictor
        self._injected_tracker = tracker
        self._runtime_probe = runtime_probe
        self._lock = threading.RLock()
        self._model: Any = None
        self._processor: Any = None
        self._video_model: Any = None
        self._video_processor: Any = None
        self._device: Optional[str] = None
        self._verified: Optional[tuple[Path, dict[str, Any], str]] = None

    def _verification(self) -> tuple[Path, dict[str, Any], str]:
        with self._lock:
            if self._verified is not None:
                root, _manifest, _digest = self._verified
                if root.exists():
                    return self._verified
            root = _safe_existing_root(self._pack_path)
            manifest, digest = _load_verified_manifest(root)
            self._verified = (root, manifest, digest)
            return self._verified

    def status(self) -> Mapping[str, Any]:
        try:
            _root, manifest, manifest_sha256 = self._verification()
            runtime = dict(self._runtime_probe())
            if runtime.get("state") != "ready":
                return {
                    "state": "unavailable",
                    "error_code": (
                        runtime.get("error_code")
                        or "sam2_runtime_dependency_missing"
                    ),
                }
            device = runtime.get("device")
            if device not in {"cuda", "cpu"}:
                raise TransformersSam2AdapterError(
                    "SAM2 runtime device is invalid"
                )
        except TransformersSam2AdapterError:
            return {
                "state": "unavailable",
                "error_code": "model_pack_not_configured",
            }
        return {
            "state": "ready",
            "error_code": None,
            "runtime_device": device,
            "model_manifest": {
                "provenance_verified": True,
                "provider": manifest["provider"],
                "pack_id": manifest["pack_id"],
                "pack_version": manifest["pack_version"],
                "manifest_sha256": manifest_sha256,
                "model_name": manifest["model_name"],
                "model_version": manifest["model_version"],
                "model_artifact_sha256": (
                    manifest["model_artifact_sha256"]
                ),
                "source_repo_id": manifest["source_repo_id"],
                "source_revision": manifest["source_revision"],
                "weights_spdx": manifest["weights_spdx"],
                "runtime_spdx": manifest["runtime_spdx"],
                "commercial_use_confirmed": True,
                "redistribution_allowed": True,
                "gpu_required": True,
                "cpu_fallback": True,
                "recommended_min_vram_mb": 4096,
                "cuda_min_version": "12.0",
                "review_only": True,
            },
        }

    def _ready_predictor(self) -> MaskPredictor:
        if self._injected_predictor is not None:
            return self._injected_predictor
        with self._lock:
            root, manifest, _digest = self._verification()
            runtime = dict(self._runtime_probe())
            if runtime.get("state") != "ready":
                raise TransformersSam2AdapterError(
                    "SAM2 runtime dependencies are unavailable"
                )
            device = str(runtime["device"])
            if self._model is None or self._processor is None:
                try:
                    import torch
                    from transformers import Sam2Model, Sam2Processor

                    model_root = root / manifest["model_directory"]
                    self._processor = Sam2Processor.from_pretrained(
                        model_root,
                        local_files_only=True,
                    )
                    self._model = Sam2Model.from_pretrained(
                        model_root,
                        local_files_only=True,
                        use_safetensors=True,
                    ).to(device)
                    self._model.eval()
                    self._device = device
                except Exception as error:
                    self._model = None
                    self._processor = None
                    self._device = None
                    raise TransformersSam2AdapterError(
                        "SAM2 model could not be loaded"
                    ) from error

            def predict(
                image_path: Path,
                point: tuple[float, float],
            ) -> Any:
                from PIL import Image
                import torch

                with Image.open(image_path) as source:
                    image = source.convert("RGB")
                    width, height = image.size
                    input_points = [
                        [[[
                            point[0] * width,
                            point[1] * height,
                        ]]]
                    ]
                    inputs = self._processor(
                        images=image,
                        input_points=input_points,
                        input_labels=[[[1]]],
                        return_tensors="pt",
                    ).to(self._device)
                with torch.inference_mode():
                    if self._device == "cuda":
                        with torch.autocast(
                            device_type="cuda",
                            dtype=torch.float16,
                        ):
                            outputs = self._model(**inputs)
                    else:
                        outputs = self._model(**inputs)
                masks = self._processor.post_process_masks(
                    outputs.pred_masks.detach().cpu(),
                    inputs["original_sizes"].detach().cpu(),
                    binarize=True,
                )[0]
                scores = outputs.iou_scores.detach().cpu()[0, 0]
                best = int(scores.argmax().item())
                return masks[0, best].numpy()

            return predict

    def _ready_tracker(self) -> TrackPredictor:
        if self._injected_tracker is not None:
            return self._injected_tracker
        with self._lock:
            root, manifest, _digest = self._verification()
            runtime = dict(self._runtime_probe())
            if runtime.get("state") != "ready":
                raise TransformersSam2AdapterError(
                    "SAM2 runtime dependencies are unavailable"
                )
            device = str(runtime["device"])
            if self._video_model is None or self._video_processor is None:
                try:
                    import torch
                    from transformers import (
                        Sam2VideoModel,
                        Sam2VideoProcessor,
                    )

                    model_root = root / manifest["model_directory"]
                    dtype = (
                        torch.bfloat16
                        if device == "cuda"
                        else torch.float32
                    )
                    self._video_processor = (
                        Sam2VideoProcessor.from_pretrained(
                            model_root,
                            local_files_only=True,
                        )
                    )
                    self._video_model = (
                        Sam2VideoModel.from_pretrained(
                            model_root,
                            local_files_only=True,
                            use_safetensors=True,
                            dtype=dtype,
                        )
                        .to(device)
                        .eval()
                    )
                    self._device = device
                except Exception as error:
                    self._video_model = None
                    self._video_processor = None
                    self._device = None
                    raise TransformersSam2AdapterError(
                        "SAM2 video model could not be loaded"
                    ) from error

            def track(
                frame_paths: Sequence[Path],
                prompt_frame_index: int,
                point: tuple[float, float],
            ) -> Mapping[str, Any]:
                from PIL import Image
                import torch

                images = []
                try:
                    for frame_path in frame_paths:
                        with Image.open(frame_path) as opened:
                            images.append(opened.convert("RGB").copy())
                    prompt_image = images[prompt_frame_index]
                    width, height = prompt_image.size
                    dtype = (
                        torch.bfloat16
                        if self._device == "cuda"
                        else torch.float32
                    )
                    inference_session = (
                        self._video_processor.init_video_session(
                            video=images,
                            inference_device=self._device,
                            dtype=dtype,
                        )
                    )
                    self._video_processor.add_inputs_to_inference_session(
                        inference_session=inference_session,
                        frame_idx=prompt_frame_index,
                        obj_ids=1,
                        input_points=[
                            [[[
                                point[0] * width,
                                point[1] * height,
                            ]]]
                        ],
                        input_labels=[[[1]]],
                    )
                    tracked = []
                    with torch.inference_mode():
                        # Materialize the prompted frame into the inference
                        # session before propagation.  Without this first
                        # model call the Transformers video iterator has no
                        # authoritative start frame.
                        self._video_model(
                            inference_session=inference_session,
                            frame_idx=prompt_frame_index,
                        )
                        for output in (
                            self._video_model
                            .propagate_in_video_iterator(
                                inference_session,
                            )
                        ):
                            masks = (
                                self._video_processor
                                .post_process_masks(
                                    [output.pred_masks],
                                    original_sizes=[[
                                        inference_session.video_height,
                                        inference_session.video_width,
                                    ]],
                                    binarize=True,
                                )[0]
                            )
                            mask = masks[0]
                            while getattr(mask, "ndim", 0) > 2:
                                mask = mask[0]
                            tracked.append(
                                {
                                    "frame_index": int(
                                        output.frame_idx
                                    ),
                                    "polygon": _polygon_from_mask(
                                        mask.detach().cpu().numpy(),
                                        point,
                                    ),
                                }
                            )
                    return {"frames": tracked}
                finally:
                    for image in images:
                        image.close()

            return track

    def refine(
        self,
        *,
        image_path: Path,
        point: tuple[float, float],
    ) -> Mapping[str, Any]:
        path = Path(image_path)
        if not path.is_absolute():
            raise TransformersSam2AdapterError(
                "SAM2 input image path must be absolute"
            )
        if (
            len(point) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
                for value in point
            )
        ):
            raise TransformersSam2AdapterError(
                "SAM2 point must be normalized"
            )
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise TransformersSam2AdapterError(
                "SAM2 input image is unavailable"
            ) from error
        if (
            not resolved.is_file()
            or resolved.is_symlink()
            or _is_reparse_point(resolved)
            or not 1 <= resolved.stat().st_size <= MAX_INPUT_IMAGE_BYTES
        ):
            raise TransformersSam2AdapterError(
                "SAM2 input image is unsafe"
            )
        predictor = self._ready_predictor()
        try:
            mask = predictor(
                resolved,
                (float(point[0]), float(point[1])),
            )
            polygon = _polygon_from_mask(
                mask,
                (float(point[0]), float(point[1])),
            )
        except TransformersSam2AdapterError:
            raise
        except Exception as error:
            raise TransformersSam2AdapterError(
                "SAM2 mask inference failed"
            ) from error
        return {
            "polygon": polygon,
            "tracked_frame_refs": [],
        }

    def track(
        self,
        *,
        frame_paths: Sequence[Path],
        prompt_frame_index: int,
        point: tuple[float, float],
    ) -> Mapping[str, Any]:
        """Track one point-prompted object through a bounded frame window."""

        if (
            isinstance(frame_paths, (str, bytes, bytearray))
            or not 2 <= len(frame_paths) <= 64
            or isinstance(prompt_frame_index, bool)
            or not isinstance(prompt_frame_index, int)
            or not 0 <= prompt_frame_index < len(frame_paths)
        ):
            raise TransformersSam2AdapterError(
                "SAM2 tracking frame window is invalid"
            )
        if (
            len(point) != 2
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
                for value in point
            )
        ):
            raise TransformersSam2AdapterError(
                "SAM2 point must be normalized"
            )
        resolved_frames = []
        for raw_path in frame_paths:
            path = Path(raw_path)
            if not path.is_absolute():
                raise TransformersSam2AdapterError(
                    "SAM2 tracking frames must use absolute paths"
                )
            try:
                resolved = path.resolve(strict=True)
                metadata = resolved.lstat()
            except (OSError, RuntimeError) as error:
                raise TransformersSam2AdapterError(
                    "SAM2 tracking frame is unavailable"
                ) from error
            if (
                not resolved.is_file()
                or resolved.is_symlink()
                or _is_reparse_point(resolved)
                or not 1 <= metadata.st_size <= MAX_INPUT_IMAGE_BYTES
            ):
                raise TransformersSam2AdapterError(
                    "SAM2 tracking frame is unsafe"
                )
            resolved_frames.append(resolved)
        if len(resolved_frames) != len(set(resolved_frames)):
            raise TransformersSam2AdapterError(
                "SAM2 tracking frames are duplicated"
            )
        tracker = self._ready_tracker()
        try:
            result = tracker(
                tuple(resolved_frames),
                prompt_frame_index,
                (float(point[0]), float(point[1])),
            )
        except TransformersSam2AdapterError:
            raise
        except Exception as error:
            raise TransformersSam2AdapterError(
                "SAM2 tracking inference failed"
            ) from error
        if not isinstance(result, Mapping) or set(result) != {"frames"}:
            raise TransformersSam2AdapterError(
                "SAM2 tracking output is invalid"
            )
        frames = result["frames"]
        if (
            not isinstance(frames, Sequence)
            or isinstance(frames, (str, bytes, bytearray))
            or not frames
            or len(frames) > len(resolved_frames)
        ):
            raise TransformersSam2AdapterError(
                "SAM2 tracking output is invalid"
            )
        normalized = []
        seen = set()
        for raw in frames:
            if not isinstance(raw, Mapping) or set(raw) != {
                "frame_index",
                "polygon",
            }:
                raise TransformersSam2AdapterError(
                    "SAM2 tracking frame output is invalid"
                )
            frame_index = raw["frame_index"]
            if (
                isinstance(frame_index, bool)
                or not isinstance(frame_index, int)
                or not 0 <= frame_index < len(resolved_frames)
                or frame_index in seen
            ):
                raise TransformersSam2AdapterError(
                    "SAM2 tracking frame index is invalid"
                )
            seen.add(frame_index)
            polygon = raw["polygon"]
            if (
                not isinstance(polygon, Sequence)
                or isinstance(polygon, (str, bytes, bytearray))
                or not 3 <= len(polygon) <= MAX_POLYGON_POINTS
            ):
                raise TransformersSam2AdapterError(
                    "SAM2 tracking polygon is invalid"
                )
            normalized_polygon = []
            for raw_point in polygon:
                if (
                    not isinstance(raw_point, Sequence)
                    or isinstance(
                        raw_point, (str, bytes, bytearray)
                    )
                    or len(raw_point) != 2
                ):
                    raise TransformersSam2AdapterError(
                        "SAM2 tracking polygon is invalid"
                    )
                normalized_point = []
                for value in raw_point:
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or not 0.0 <= float(value) <= 1.0
                    ):
                        raise TransformersSam2AdapterError(
                            "SAM2 tracking polygon is invalid"
                        )
                    normalized_point.append(round(float(value), 8))
                normalized_polygon.append(normalized_point)
            if len(
                {tuple(item) for item in normalized_polygon}
            ) < 3:
                raise TransformersSam2AdapterError(
                    "SAM2 tracking polygon is degenerate"
                )
            normalized.append(
                {
                    "frame_index": frame_index,
                    "polygon": normalized_polygon,
                }
            )
        normalized.sort(key=lambda item: item["frame_index"])
        return {"frames": normalized}


__all__ = [
    "DEFAULT_PACK_DIRECTORY",
    "DEFAULT_PACK_ID",
    "DEFAULT_PACK_VERSION",
    "MANIFEST_NAME",
    "PACK_SCHEMA",
    "TransformersSam2Adapter",
    "TransformersSam2AdapterError",
    "default_program_data_pack_path",
]
