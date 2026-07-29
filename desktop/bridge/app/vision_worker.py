"""Disposable offline worker for :mod:`app.vision_inference`.

This file is an internal process entry point, not a public plug-in API.  It
imports only fixed inference engines and never imports Python from a model
pack.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence


BRIDGE_ROOT = Path(__file__).resolve().parent.parent
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from app.vision_inference import (  # noqa: E402
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_OCR_PER_CANDIDATE,
    MAX_PREDICTIONS_PER_CANDIDATE,
    MAX_REQUEST_BYTES,
    MAX_WORKER_OUTPUT_BYTES,
    WORKER_SCHEMA,
    VisionInferenceError,
    _CANDIDATE_ID,
    _SHA256,
    _hash_file,
    _is_reparse_point,
    _load_json_object,
    load_model_pack,
)


def _optional_module(name: str):
    """Load a fixed runtime only after a validated pack selects its engine.

    Keeping optional ML stacks behind this boundary is intentional.  The
    minimal frozen bridge can run candidate extraction without accidentally
    collecting every Torch/Qt/Ultralytics package installed on the build
    workstation.  A production vision-enabled build must explicitly include
    the selected runtime and its native libraries.
    """
    try:
        return importlib.import_module(name)
    except Exception as error:
        raise VisionInferenceError(
            "The selected local vision runtime is unavailable"
        ) from error


def _request(path: Path) -> Dict[str, Any]:
    request, _ = _load_json_object(path, MAX_REQUEST_BYTES, "Vision worker request")
    if set(request) != {
        "schema_version",
        "manifest_path",
        "manifest_sha256",
        "candidates",
        "limits",
    }:
        raise VisionInferenceError("The vision worker request fields are invalid")
    if request["schema_version"] != WORKER_SCHEMA:
        raise VisionInferenceError("The vision worker schema is unsupported")
    limits = request["limits"]
    if not isinstance(limits, Mapping) or set(limits) != {
        "max_image_bytes",
        "max_image_pixels",
        "max_predictions",
        "max_ocr_observations",
    }:
        raise VisionInferenceError("The vision worker limits are invalid")
    expected_limits = {
        "max_image_bytes": MAX_IMAGE_BYTES,
        "max_image_pixels": MAX_IMAGE_PIXELS,
        "max_predictions": MAX_PREDICTIONS_PER_CANDIDATE,
        "max_ocr_observations": MAX_OCR_PER_CANDIDATE,
    }
    if dict(limits) != expected_limits:
        raise VisionInferenceError("The vision worker limits did not match")
    candidates = request["candidates"]
    if not isinstance(candidates, list) or len(candidates) > 512:
        raise VisionInferenceError("The vision worker candidate batch is invalid")
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "candidate_id",
            "image_path",
            "image_size_bytes",
            "image_sha256",
        }:
            raise VisionInferenceError("A vision worker candidate is invalid")
        candidate_id = candidate["candidate_id"]
        image_path = candidate["image_path"]
        size = candidate["image_size_bytes"]
        sha256 = candidate["image_sha256"]
        if (
            not isinstance(candidate_id, str)
            or not _CANDIDATE_ID.fullmatch(candidate_id)
            or candidate_id in seen
            or not isinstance(image_path, str)
            or not Path(image_path).is_absolute()
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= MAX_IMAGE_BYTES
            or not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)
        ):
            raise VisionInferenceError("A vision worker candidate value is invalid")
        seen.add(candidate_id)
        path_value = Path(image_path)
        try:
            resolved = path_value.resolve(strict=True)
        except OSError as error:
            raise VisionInferenceError("A candidate image is unavailable") from error
        if (
            path_value.is_symlink()
            or _is_reparse_point(path_value)
            or not resolved.is_file()
        ):
            raise VisionInferenceError("A candidate image is invalid")
        actual_size, actual_sha256 = _hash_file(resolved, MAX_IMAGE_BYTES)
        if actual_size != size or actual_sha256 != sha256:
            raise VisionInferenceError("A candidate image failed integrity checks")
    return request


def _device(preference: str) -> str:
    if preference == "cpu":
        return "cpu"
    try:
        torch = _optional_module("torch")
        available = bool(torch.cuda.is_available())
    except Exception:
        available = False
    if preference == "cuda" and not available:
        raise VisionInferenceError("The requested CUDA provider is unavailable")
    return "cuda" if available else "cpu"


def _read_rgb(path: Path):
    try:
        image_module = _optional_module("PIL.Image")

        with image_module.open(path) as source:
            source.load()
            if (
                source.width <= 0
                or source.height <= 0
                or source.width * source.height > MAX_IMAGE_PIXELS
            ):
                raise VisionInferenceError("Candidate image dimensions are invalid")
            return source.convert("RGB")
    except VisionInferenceError:
        raise
    except Exception as error:
        raise VisionInferenceError("Candidate image decoding failed") from error


def _iou(box: Sequence[float], boxes):
    np = _optional_module("numpy")

    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])
    intersection = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    first_area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
    areas = np.maximum(0.0, boxes[:, 2] - boxes[:, 0]) * np.maximum(
        0.0, boxes[:, 3] - boxes[:, 1]
    )
    union = first_area + areas - intersection
    return np.divide(
        intersection,
        union,
        out=np.zeros_like(intersection),
        where=union > 0,
    )


class _OnnxYolo:
    def __init__(self, pack) -> None:
        ort = _optional_module("onnxruntime")
        model = pack.manifest["model"]
        model_path = pack.artifacts[model["file"]].path
        preference = model.get("execution_provider", "auto")
        available = set(ort.get_available_providers())
        if preference == "cuda" and "CUDAExecutionProvider" not in available:
            raise VisionInferenceError("The requested ONNX CUDA provider is unavailable")
        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if preference != "cpu" and "CUDAExecutionProvider" in available
            else ["CPUExecutionProvider"]
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.enable_mem_pattern = True
        try:
            self.session = ort.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=providers,
            )
        except Exception as error:
            raise VisionInferenceError("The ONNX model could not be loaded") from error
        inputs = self.session.get_inputs()
        outputs = self.session.get_outputs()
        if len(inputs) != 1 or len(outputs) < 1:
            raise VisionInferenceError("The ONNX model IO contract is unsupported")
        self.input_name = inputs[0].name
        self.output_name = outputs[0].name
        self.width = int(model["input_width"])
        self.height = int(model["input_height"])
        self.threshold = float(model["confidence_threshold"])
        self.nms_threshold = float(model["nms_iou_threshold"])
        self.max_detections = int(model["max_detections"])
        self.labels = {
            int(row["class_id"]): row for row in model["labels"]
        }

    def infer(self, image) -> List[Dict[str, Any]]:
        cv2 = _optional_module("cv2")
        np = _optional_module("numpy")

        rgb = np.asarray(image)
        original_height, original_width = rgb.shape[:2]
        scale = min(self.width / original_width, self.height / original_height)
        resized_width = max(1, round(original_width * scale))
        resized_height = max(1, round(original_height * scale))
        resized = cv2.resize(
            rgb,
            (resized_width, resized_height),
            interpolation=cv2.INTER_LINEAR,
        )
        canvas = np.full((self.height, self.width, 3), 114, dtype=np.uint8)
        left = (self.width - resized_width) // 2
        top = (self.height - resized_height) // 2
        canvas[top : top + resized_height, left : left + resized_width] = resized
        tensor = np.transpose(canvas.astype(np.float32) / 255.0, (2, 0, 1))[None]
        try:
            output = self.session.run(
                [self.output_name], {self.input_name: tensor}
            )[0]
        except Exception as error:
            raise VisionInferenceError("ONNX inference failed") from error
        array = np.asarray(output)
        if array.ndim != 3 or array.shape[0] != 1:
            raise VisionInferenceError("The ONNX detection output shape is unsupported")
        rows = array[0]
        class_count = len(self.labels)
        expected_channels = 4 + class_count
        if rows.shape[0] == expected_channels:
            rows = rows.T
        elif rows.shape[1] != expected_channels:
            raise VisionInferenceError("The ONNX class output shape is unsupported")
        if rows.shape[0] > 1_000_000:
            raise VisionInferenceError("The ONNX output is too large")
        class_scores = rows[:, 4:]
        class_ids = np.argmax(class_scores, axis=1)
        confidences = class_scores[np.arange(rows.shape[0]), class_ids]
        selected = confidences >= self.threshold
        rows = rows[selected]
        class_ids = class_ids[selected]
        confidences = confidences[selected]
        if not len(rows):
            return []
        cx, cy, width, height = rows[:, 0], rows[:, 1], rows[:, 2], rows[:, 3]
        boxes = np.stack(
            (
                (cx - width / 2 - left) / scale,
                (cy - height / 2 - top) / scale,
                (cx + width / 2 - left) / scale,
                (cy + height / 2 - top) / scale,
            ),
            axis=1,
        )
        boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0, original_width)
        boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0, original_height)
        order = np.argsort(-confidences)
        kept: List[int] = []
        while order.size and len(kept) < self.max_detections:
            index = int(order[0])
            kept.append(index)
            if order.size == 1:
                break
            remaining = order[1:]
            overlaps = _iou(boxes[index], boxes[remaining])
            same_class = class_ids[remaining] == class_ids[index]
            order = remaining[
                np.logical_or(~same_class, overlaps <= self.nms_threshold)
            ]
        results: List[Dict[str, Any]] = []
        for index in kept:
            x1, y1, x2, y2 = boxes[index]
            normalized_width = max(0.0, float(x2 - x1) / original_width)
            normalized_height = max(0.0, float(y2 - y1) / original_height)
            if normalized_width <= 0 or normalized_height <= 0:
                continue
            label = self.labels[int(class_ids[index])]
            results.append(
                {
                    "issue_type": label["issue_type"],
                    "confidence": float(confidences[index]),
                    "box": {
                        "x": float(x1) / original_width,
                        "y": float(y1) / original_height,
                        "width": normalized_width,
                        "height": normalized_height,
                    },
                    "source_label": label["display_name"],
                    "calibration": "model_score",
                }
            )
        return results


class _ClipZeroShot:
    def __init__(self, pack) -> None:
        try:
            torch = _optional_module("torch")
            transformers = _optional_module("transformers")
        except Exception as error:
            raise VisionInferenceError(
                "The local CLIP runtime is unavailable"
            ) from error
        model_config = pack.manifest["model"]
        directory = pack.root / model_config["directory"]
        self.device = _device(model_config.get("device", "auto"))
        try:
            self.processor = transformers.AutoProcessor.from_pretrained(
                str(directory),
                local_files_only=True,
                trust_remote_code=False,
            )
            self.model = transformers.CLIPModel.from_pretrained(
                str(directory),
                local_files_only=True,
                trust_remote_code=False,
                torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
            )
            self.model.to(self.device)
            self.model.eval()
        except Exception as error:
            raise VisionInferenceError("The local CLIP model could not be loaded") from error
        self.torch = torch
        self.threshold = float(model_config["confidence_threshold"])
        self.top_k = int(model_config["top_k"])
        prompts: List[str] = []
        prompt_types: List[str | None] = []
        for issue_type, values in model_config["prompts"].items():
            for value in values:
                prompts.append(value)
                prompt_types.append(issue_type)
        for value in model_config["negative_prompts"]:
            prompts.append(value)
            prompt_types.append(None)
        self.prompts = prompts
        self.prompt_types = prompt_types

    def infer(self, image) -> List[Dict[str, Any]]:
        try:
            inputs = self.processor(
                text=self.prompts,
                images=image,
                return_tensors="pt",
                padding=True,
            )
            inputs = {
                key: value.to(self.device)
                for key, value in inputs.items()
            }
            with self.torch.inference_mode():
                output = self.model(**inputs)
                probabilities = output.logits_per_image[0].float().softmax(dim=-1)
            scores: Dict[str, tuple[float, str]] = {}
            for index, issue_type in enumerate(self.prompt_types):
                if issue_type is None:
                    continue
                score = float(probabilities[index].item())
                previous = scores.get(issue_type)
                if previous is None or score > previous[0]:
                    scores[issue_type] = (score, self.prompts[index])
            ranked = sorted(scores.items(), key=lambda item: item[1][0], reverse=True)
            return [
                {
                    "issue_type": issue_type,
                    "confidence": score,
                    "box": None,
                    "source_label": prompt[:128],
                    "calibration": "zero_shot_relative",
                }
                for issue_type, (score, prompt) in ranked[: self.top_k]
                if score >= self.threshold
            ]
        except VisionInferenceError:
            raise
        except Exception as error:
            raise VisionInferenceError("CLIP inference failed") from error


class _EasyOcr:
    def __init__(self, pack) -> None:
        config = pack.manifest.get("ocr")
        if config is None:
            self.reader = None
            return
        easyocr = _optional_module("easyocr")
        device = _device(config.get("device", "auto"))
        model_directory = pack.root / config["model_storage_directory"]
        try:
            self.reader = easyocr.Reader(
                config["languages"],
                gpu=device == "cuda",
                model_storage_directory=str(model_directory),
                user_network_directory=str(model_directory),
                download_enabled=False,
                verbose=False,
            )
        except Exception as error:
            raise VisionInferenceError("The local OCR model could not be loaded") from error
        self.minimum = float(config["minimum_confidence"])
        self.maximum = int(config["max_observations"])

    def infer(self, image) -> List[Dict[str, Any]]:
        if self.reader is None:
            return []
        np = _optional_module("numpy")

        width, height = image.size
        try:
            values = self.reader.readtext(
                np.asarray(image),
                detail=1,
                paragraph=False,
                batch_size=1,
                workers=0,
            )
        except Exception as error:
            raise VisionInferenceError("OCR inference failed") from error
        observations: List[Dict[str, Any]] = []
        for value in values:
            if (
                not isinstance(value, (list, tuple))
                or len(value) != 3
                or not isinstance(value[1], str)
                or not value[1].strip()
                or len(value[1]) > 500
                or isinstance(value[2], bool)
                or not isinstance(value[2], (int, float))
                or not math.isfinite(float(value[2]))
                or float(value[2]) < self.minimum
            ):
                continue
            points = value[0]
            try:
                xs = [float(point[0]) for point in points]
                ys = [float(point[1]) for point in points]
            except (TypeError, ValueError, IndexError):
                continue
            if not xs or not ys or any(not math.isfinite(item) for item in xs + ys):
                continue
            x1 = min(max(0.0, min(xs)), float(width))
            y1 = min(max(0.0, min(ys)), float(height))
            x2 = min(max(0.0, max(xs)), float(width))
            y2 = min(max(0.0, max(ys)), float(height))
            if x2 <= x1 or y2 <= y1:
                continue
            observations.append(
                {
                    "text": value[1].strip(),
                    "confidence": min(1.0, max(0.0, float(value[2]))),
                    "box": {
                        "x": x1 / width,
                        "y": y1 / height,
                        "width": (x2 - x1) / width,
                        "height": (y2 - y1) / height,
                    },
                    "kind": "sign_or_tunnel_text_unverified",
                }
            )
            if len(observations) >= self.maximum:
                break
        return observations


def _engines(pack):
    detector = None
    if pack.engine == "onnx_yolo_v8":
        detector = _OnnxYolo(pack)
    elif pack.engine == "transformers_clip_zero_shot":
        detector = _ClipZeroShot(pack)
    ocr = _EasyOcr(pack)
    return detector, ocr


def run(request: Mapping[str, Any]) -> Dict[str, Any]:
    pack = load_model_pack(Path(request["manifest_path"]))
    if request["manifest_sha256"] != pack.manifest_sha256:
        raise VisionInferenceError("The model-pack manifest changed")
    detector, ocr = _engines(pack)
    results: List[Dict[str, Any]] = []
    for candidate in request["candidates"]:
        started = time.monotonic()
        image = _read_rgb(Path(candidate["image_path"]))
        predictions = detector.infer(image) if detector is not None else []
        observations = ocr.infer(image)
        duration_ms = min(3_600_000, max(0, round((time.monotonic() - started) * 1000)))
        results.append(
            {
                "candidate_id": candidate["candidate_id"],
                "predictions": predictions[:MAX_PREDICTIONS_PER_CANDIDATE],
                "ocr_observations": observations[:MAX_OCR_PER_CANDIDATE],
                "duration_ms": duration_ms,
            }
        )
    return {
        "schema_version": WORKER_SCHEMA,
        "pack_id": pack.pack_id,
        "pack_version": pack.pack_version,
        "manifest_sha256": pack.manifest_sha256,
        "results": results,
    }


def _write_output(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not 1 <= len(encoded) <= MAX_WORKER_OUTPUT_BYTES:
        raise VisionInferenceError("The vision worker output is too large")
    parent = path.parent.resolve(strict=True)
    if path.exists() or path.is_symlink() or _is_reparse_point(parent):
        raise VisionInferenceError("The vision worker output path is invalid")
    temporary = parent / f".{path.name}.{os.getpid()}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    request_path = Path(arguments.request)
    output_path = Path(arguments.output)
    if not request_path.is_absolute() or not output_path.is_absolute():
        return 2
    try:
        request = _request(request_path)
        result = run(request)
        _write_output(output_path, result)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
