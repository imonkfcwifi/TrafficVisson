"""Offline ONNX Grounding DINO worker for TrafficVisson.

The executable produced from this module reads one bounded request from stdin
and writes one bounded response to stdout.  It does not accept command-line
paths, download models, persist labels, or write TrafficVisson state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import UUID


REQUEST_SCHEMA = "trafficvisson.command-proposal-request.v1"
RESPONSE_SCHEMA = "trafficvisson.command-proposal-response.v1"
MODEL_MANIFEST_SCHEMA = "trafficvisson.command-proposal-model-pack.v1"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_CANDIDATES = 512
MAX_PREDICTIONS = 32
BOX_THRESHOLD = 0.25
TEXT_THRESHOLD = 0.25
IMAGE_WIDTH = 800
IMAGE_HEIGHT = 800
MODEL_RELATIVE_PATH = Path("onnx") / "model_q4.onnx"
TOKENIZER_RELATIVE_PATH = Path("tokenizer.json")
PREPROCESSOR_RELATIVE_PATH = Path("preprocessor_config.json")

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
# ``other`` is an operator extension rather than one of the twelve product
# classes.  Legacy values remain input-compatible, but are normalized before
# any proposal is produced so runner output never leaks retired codes.
ISSUE_TYPES = frozenset((*CANONICAL_ISSUE_TYPES, "other"))
ISSUE_TYPE_ALIASES = {
    "sign_identity": "guide_sign_identity",
    "color_guidance_line_faded": "colored_guidance_faded",
}
REQUEST_KEYS = {
    "schema_version",
    "project_id",
    "analysis_job_id",
    "recipe_id",
    "command_sha256",
    "prompt_sha256",
    "issue_type",
    "positive_prompts",
    "negative_prompts",
    "runner_manifest_sha256",
    "model_manifest_sha256",
    "model_pack_root",
    "candidates",
}
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
CANDIDATE_ID_PATTERN = re.compile(r"^[a-f0-9]{20}$")


class RunnerError(ValueError):
    """The fixed runner contract was violated."""


def _reject_duplicate_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RunnerError("JSON contains duplicate keys")
        value[key] = item
    return value


def _reject_constant(_: str) -> object:
    raise RunnerError("JSON contains a non-finite number")


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not 1 <= len(raw) <= MAX_REQUEST_BYTES:
        raise RunnerError("request size is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except RunnerError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerError("request JSON is invalid") from error
    if not isinstance(value, dict) or set(value) != REQUEST_KEYS:
        raise RunnerError("request fields are invalid")
    return value


def _canonical_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RunnerError(f"{label} is invalid")
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise RunnerError(f"{label} is invalid") from error
    if canonical != value:
        raise RunnerError(f"{label} is invalid")
    return canonical


def _hash_file(path: Path, maximum_bytes: int) -> tuple[int, str]:
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
                    raise RunnerError("artifact exceeds its byte limit")
                digest.update(chunk)
    except OSError as error:
        raise RunnerError("artifact is unavailable") from error
    return total, digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_file(
    path: Path,
    *,
    root: Path,
    maximum_bytes: int,
    label: str,
) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        information = path.stat()
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise RunnerError(f"{label} is unavailable") from error
    if (
        root.is_symlink()
        or _is_reparse_point(root)
        or not root.is_dir()
        or path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(information.st_mode)
        or information.st_nlink != 1
        or not 1 <= information.st_size <= maximum_bytes
    ):
        raise RunnerError(f"{label} is unsafe")
    return resolved


def _read_json_file(
    path: Path,
    *,
    root: Path,
    maximum_bytes: int,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    resolved = _safe_file(
        path,
        root=root,
        maximum_bytes=maximum_bytes,
        label=label,
    )
    raw = resolved.read_bytes()
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except RunnerError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise RunnerError(f"{label} is invalid")
    return value, raw


def _runner_root() -> Path:
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve(strict=True).parent
    else:
        root = Path(__file__).resolve(strict=True).parent
    if root.is_symlink() or _is_reparse_point(root) or not root.is_dir():
        raise RunnerError("runner root is unsafe")
    return root


def _text_list(value: object, minimum: int, maximum: int) -> list[str]:
    if (
        not isinstance(value, list)
        or not minimum <= len(value) <= maximum
        or any(
            not isinstance(item, str)
            or not 1 <= len(item.strip()) <= 300
            or any(ord(character) < 32 for character in item)
            for item in value
        )
    ):
        raise RunnerError("prompt contract is invalid")
    return [item.strip() for item in value]


def _canonical_issue_type(value: object) -> str:
    if not isinstance(value, str):
        raise RunnerError("issue type is invalid")
    canonical = ISSUE_TYPE_ALIASES.get(value, value)
    if canonical not in ISSUE_TYPES:
        raise RunnerError("issue type is invalid")
    return canonical


def _validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != REQUEST_SCHEMA:
        raise RunnerError("request schema is unsupported")
    project_id = _canonical_uuid(value.get("project_id"), "project id")
    analysis_job_id = _canonical_uuid(
        value.get("analysis_job_id"), "analysis job id"
    )
    for key in (
        "recipe_id",
        "command_sha256",
        "prompt_sha256",
        "runner_manifest_sha256",
        "model_manifest_sha256",
    ):
        item = value.get(key)
        if not isinstance(item, str) or not SHA256_PATTERN.fullmatch(item):
            raise RunnerError(f"{key} is invalid")
    issue_type = _canonical_issue_type(value.get("issue_type"))
    positive_prompts = _text_list(value.get("positive_prompts"), 1, 8)
    negative_prompts = _text_list(value.get("negative_prompts"), 2, 8)
    model_pack_root = value.get("model_pack_root")
    if not isinstance(model_pack_root, str) or not Path(model_pack_root).is_absolute():
        raise RunnerError("model pack root is invalid")
    candidates = value.get("candidates")
    if (
        not isinstance(candidates, list)
        or not 1 <= len(candidates) <= MAX_CANDIDATES
    ):
        raise RunnerError("candidate batch is invalid")
    seen: set[str] = set()
    normalized_candidates: list[dict[str, Any]] = []
    for row in candidates:
        if not isinstance(row, dict) or set(row) != {
            "candidate_id",
            "image_path",
            "image_sha256",
            "size_bytes",
        }:
            raise RunnerError("candidate fields are invalid")
        candidate_id = row["candidate_id"]
        image_path = row["image_path"]
        image_sha256 = row["image_sha256"]
        size_bytes = row["size_bytes"]
        if (
            not isinstance(candidate_id, str)
            or not CANDIDATE_ID_PATTERN.fullmatch(candidate_id)
            or candidate_id in seen
            or not isinstance(image_path, str)
            or not Path(image_path).is_absolute()
            or not isinstance(image_sha256, str)
            or not SHA256_PATTERN.fullmatch(image_sha256)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= MAX_IMAGE_BYTES
        ):
            raise RunnerError("candidate value is invalid")
        seen.add(candidate_id)
        normalized_candidates.append(dict(row))
    return {
        **dict(value),
        "project_id": project_id,
        "analysis_job_id": analysis_job_id,
        "issue_type": issue_type,
        "positive_prompts": positive_prompts,
        "negative_prompts": negative_prompts,
        "candidates": normalized_candidates,
    }


def _load_model_contract(
    request: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    root = _runner_root()
    requested = Path(request["model_pack_root"])
    expected = root / "model-pack"
    try:
        model_root = requested.resolve(strict=True)
        expected_root = expected.resolve(strict=True)
    except OSError as error:
        raise RunnerError("model pack is unavailable") from error
    if (
        model_root != expected_root
        or requested.is_symlink()
        or _is_reparse_point(requested)
        or not requested.is_dir()
    ):
        raise RunnerError("model pack root is unsafe")
    manifest, raw = _read_json_file(
        model_root / "manifest.json",
        root=model_root,
        maximum_bytes=2 * 1024 * 1024,
        label="model manifest",
    )
    if (
        hashlib.sha256(raw).hexdigest() != request["model_manifest_sha256"]
        or manifest.get("schema_version") != MODEL_MANIFEST_SCHEMA
        or manifest.get("pack_id")
        != "trafficvisson-onnx-grounding-dino-tiny"
        or manifest.get("pack_version") != "0.2.0-review"
    ):
        raise RunnerError("model manifest identity is invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 32:
        raise RunnerError("model artifact inventory is invalid")
    seen: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {
            "path",
            "size_bytes",
            "sha256",
        }:
            raise RunnerError("model artifact inventory is invalid")
        relative = artifact["path"]
        size_bytes = artifact["size_bytes"]
        sha256 = artifact["sha256"]
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in seen
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= 8 * 1024 * 1024 * 1024
            or not isinstance(sha256, str)
            or not SHA256_PATTERN.fullmatch(sha256)
        ):
            raise RunnerError("model artifact inventory is invalid")
        seen.add(relative)
        path = _safe_file(
            model_root / Path(relative),
            root=model_root,
            maximum_bytes=8 * 1024 * 1024 * 1024,
            label="model artifact",
        )
        actual_size, actual_sha256 = _hash_file(
            path, 8 * 1024 * 1024 * 1024
        )
        if actual_size != size_bytes or actual_sha256 != sha256:
            raise RunnerError("model artifact integrity verification failed")
    required = {
        MODEL_RELATIVE_PATH.as_posix(),
        TOKENIZER_RELATIVE_PATH.as_posix(),
        PREPROCESSOR_RELATIVE_PATH.as_posix(),
    }
    if not required.issubset(seen):
        raise RunnerError("model pack is incomplete")
    preprocessor, _ = _read_json_file(
        model_root / PREPROCESSOR_RELATIVE_PATH,
        root=model_root,
        maximum_bytes=64 * 1024,
        label="preprocessor configuration",
    )
    if (
        preprocessor.get("size")
        != {"height": IMAGE_HEIGHT, "width": IMAGE_WIDTH}
        or preprocessor.get("image_mean") != [0.485, 0.456, 0.406]
        or preprocessor.get("image_std") != [0.229, 0.224, 0.225]
    ):
        raise RunnerError("preprocessor contract is invalid")
    return model_root, manifest


def _candidate_image(row: Mapping[str, Any]):
    from PIL import Image

    path = Path(row["image_path"])
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RunnerError("candidate image is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not resolved.is_file()
        or not stat.S_ISREG(resolved.stat().st_mode)
    ):
        raise RunnerError("candidate image is unsafe")
    actual_size, actual_sha256 = _hash_file(resolved, MAX_IMAGE_BYTES)
    if (
        actual_size != row["size_bytes"]
        or actual_sha256 != row["image_sha256"]
    ):
        raise RunnerError("candidate image integrity verification failed")
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(resolved) as source:
            source.load()
            if (
                source.format != "JPEG"
                or source.width <= 0
                or source.height <= 0
                or source.width * source.height > MAX_IMAGE_PIXELS
            ):
                raise RunnerError("candidate image format is invalid")
            return source.convert("RGB")
    except RunnerError:
        raise
    except Exception as error:
        raise RunnerError("candidate image decoding failed") from error


def _prepare_image(image):
    import numpy as np
    from PIL import Image

    resized = image.resize(
        (IMAGE_WIDTH, IMAGE_HEIGHT),
        Image.Resampling.BILINEAR,
    )
    values = np.asarray(resized, dtype=np.float32) / np.float32(255.0)
    mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    values = (values - mean) / std
    return np.transpose(values, (2, 0, 1))[None]


def _prompt_inputs(tokenizer, prompts: list[str]):
    import numpy as np

    prompt = ". ".join(item.rstrip(" .") for item in prompts).strip() + "."
    encoded = tokenizer.encode(prompt)
    if not 3 <= len(encoded.ids) <= 256:
        raise RunnerError("compiled prompt token length is invalid")
    return (
        prompt,
        np.asarray([encoded.ids], dtype=np.int64),
        np.asarray([encoded.type_ids], dtype=np.int64),
        np.asarray([encoded.attention_mask], dtype=np.int64),
    )


def _session(model_path: Path):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 12
    options.inter_op_num_threads = 1
    options.enable_mem_pattern = True
    available = set(ort.get_available_providers())
    providers = (
        ["DmlExecutionProvider", "CPUExecutionProvider"]
        if "DmlExecutionProvider" in available
        else ["CPUExecutionProvider"]
    )
    try:
        session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=providers,
        )
    except Exception as error:
        raise RunnerError("ONNX model could not be loaded") from error
    expected_inputs = {
        "pixel_values",
        "input_ids",
        "token_type_ids",
        "attention_mask",
        "pixel_mask",
    }
    if {item.name for item in session.get_inputs()} != expected_inputs:
        raise RunnerError("ONNX model input contract is invalid")
    return session


def _predictions(
    *,
    logits,
    boxes,
    input_ids,
    tokenizer,
    issue_type: str,
) -> list[dict[str, Any]]:
    import numpy as np

    if logits.shape != (1, 900, 256) or boxes.shape != (1, 900, 4):
        raise RunnerError("ONNX model output contract is invalid")
    probabilities = 1.0 / (1.0 + np.exp(-logits[0]))
    scores = probabilities.max(axis=1)
    order = np.argsort(-scores)
    results: list[dict[str, Any]] = []
    sequence_length = int(input_ids.shape[1])
    for index_value in order:
        index = int(index_value)
        score = float(scores[index])
        if not math.isfinite(score) or score <= BOX_THRESHOLD:
            break
        phrase_mask = (
            probabilities[index, :sequence_length] > TEXT_THRESHOLD
        )
        phrase_mask[0] = False
        phrase_mask[-1] = False
        phrase_ids = input_ids[0][phrase_mask].tolist()
        label = tokenizer.decode(
            phrase_ids,
            skip_special_tokens=True,
        ).strip()
        if not label:
            label = issue_type.replace("_", " ")
        cx, cy, width, height = (
            float(value) for value in boxes[0, index]
        )
        if not all(
            math.isfinite(value)
            for value in (cx, cy, width, height)
        ):
            continue
        x1 = min(1.0, max(0.0, cx - width / 2.0))
        y1 = min(1.0, max(0.0, cy - height / 2.0))
        x2 = min(1.0, max(0.0, cx + width / 2.0))
        y2 = min(1.0, max(0.0, cy + height / 2.0))
        if x2 <= x1 or y2 <= y1:
            continue
        results.append(
            {
                "issue_type": issue_type,
                "model_score": round(min(1.0, max(0.0, score)), 8),
                "box": {
                    "x": round(x1, 8),
                    "y": round(y1, 8),
                    "width": round(x2 - x1, 8),
                    "height": round(y2 - y1, 8),
                },
                "source_label": label[:128],
                "calibration": "model_score",
            }
        )
        if len(results) >= MAX_PREDICTIONS:
            break
    return results


def run() -> dict[str, Any]:
    import numpy as np
    from tokenizers import Tokenizer

    request = _validate_request(_read_request())
    model_root, _ = _load_model_contract(request)
    tokenizer = Tokenizer.from_file(
        str(model_root / TOKENIZER_RELATIVE_PATH)
    )
    _, input_ids, token_type_ids, attention_mask = _prompt_inputs(
        tokenizer,
        request["positive_prompts"],
    )
    pixel_mask = np.ones(
        (1, IMAGE_HEIGHT, IMAGE_WIDTH),
        dtype=np.int64,
    )
    session = _session(model_root / MODEL_RELATIVE_PATH)
    proposals: list[dict[str, Any]] = []
    for candidate in request["candidates"]:
        image = _candidate_image(candidate)
        pixel_values = _prepare_image(image)
        try:
            logits, boxes = session.run(
                None,
                {
                    "pixel_values": pixel_values,
                    "input_ids": input_ids,
                    "token_type_ids": token_type_ids,
                    "attention_mask": attention_mask,
                    "pixel_mask": pixel_mask,
                },
            )
        except Exception as error:
            raise RunnerError("ONNX inference failed") from error
        predictions = _predictions(
            logits=logits,
            boxes=boxes,
            input_ids=input_ids,
            tokenizer=tokenizer,
            issue_type=request["issue_type"],
        )
        proposals.append(
            {
                "candidate_id": candidate["candidate_id"],
                "status": (
                    "classified" if predictions else "no_suggestion"
                ),
                "predictions": predictions,
            }
        )
    return {
        "schema_version": RESPONSE_SCHEMA,
        "project_id": request["project_id"],
        "analysis_job_id": request["analysis_job_id"],
        "recipe_id": request["recipe_id"],
        "runner_manifest_sha256": request[
            "runner_manifest_sha256"
        ],
        "model_manifest_sha256": request["model_manifest_sha256"],
        "proposals": proposals,
    }


def main() -> int:
    try:
        result = run()
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if not 1 <= len(encoded) <= MAX_RESPONSE_BYTES:
            raise RunnerError("response size is invalid")
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        return 0
    except Exception as error:
        diagnostic = {
            "status": "failed",
            "error_type": type(error).__name__,
            "message": str(error)[:300],
            "automatic_labels_created": 0,
            "incidents_created": 0,
            "model_promoted": False,
        }
        sys.stderr.write(
            json.dumps(
                diagnostic,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
