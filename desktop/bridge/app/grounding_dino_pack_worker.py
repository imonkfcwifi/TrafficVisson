"""Disposable offline worker for the verified Grounding DINO evaluation pack."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


BRIDGE_ROOT = Path(__file__).resolve().parent.parent
if str(BRIDGE_ROOT) not in sys.path:
    sys.path.insert(0, str(BRIDGE_ROOT))

from app.command_proposal_generation import (  # noqa: E402
    MAX_RESPONSE_BYTES,
    CommandProposalGenerationError,
    _is_reparse_point,
    _sha256,
)
from app.grounding_dino_evaluation_pack import (  # noqa: E402
    PINNED_EVALUATION_PACK_MANIFEST_SHA256,
    SCORE_SEMANTICS,
    WORKER_REQUEST_SCHEMA,
    WORKER_RESPONSE_SCHEMA,
    load_verified_grounding_dino_pack,
)


_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000


def _optional_module(name: str):
    try:
        return importlib.import_module(name)
    except Exception as error:
        raise CommandProposalGenerationError(
            "Grounding DINO runtime is unavailable"
        ) from error


def _read_request() -> Mapping[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if not 1 <= len(raw) <= MAX_INPUT_BYTES:
        raise CommandProposalGenerationError(
            "Grounding DINO worker request size is invalid"
        )

    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CommandProposalGenerationError(
                    "Grounding DINO worker request contains duplicate keys"
                )
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                CommandProposalGenerationError(
                    "Grounding DINO worker request is non-finite"
                )
            ),
        )
    except CommandProposalGenerationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CommandProposalGenerationError(
            "Grounding DINO worker request is invalid"
        ) from error
    if not isinstance(value, Mapping):
        raise CommandProposalGenerationError(
            "Grounding DINO worker request is invalid"
        )
    return value


def _safe_candidate(path_text: str, size: int, digest: str) -> Path:
    path = Path(path_text)
    try:
        resolved = path.resolve(strict=True)
        actual_size = resolved.stat().st_size
    except OSError as error:
        raise CommandProposalGenerationError(
            "Grounding DINO candidate image is unavailable"
        ) from error
    if (
        not path.is_absolute()
        or path.is_symlink()
        or _is_reparse_point(path)
        or not resolved.is_file()
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 1 <= size <= MAX_IMAGE_BYTES
        or actual_size != size
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or _sha256(resolved, maximum_bytes=MAX_IMAGE_BYTES) != digest
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO candidate image integrity failed"
        )
    return resolved


def _normalized_words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _map_text_label(
    label: str,
    queries: Sequence[Mapping[str, str]],
) -> Mapping[str, str]:
    words = _normalized_words(label)
    ranked = []
    for query in queries:
        query_words = _normalized_words(query["prompt"])
        overlap = len(words & query_words)
        ranked.append((overlap, len(query_words), query))
    overlap, _, query = max(ranked, key=lambda row: (row[0], -row[1]))
    if overlap <= 0:
        raise CommandProposalGenerationError(
            "Grounding DINO output label did not map to a fixed query"
        )
    return query


def run(request: Mapping[str, Any]) -> Mapping[str, Any]:
    required = {
        "schema_version",
        "project_id",
        "analysis_job_id",
        "recipe_id",
        "pack_manifest",
        "pack_manifest_sha256",
        "queries",
        "candidates",
        "box_threshold",
        "text_threshold",
        "max_predictions_per_frame",
        "maximum_cuda_allocator_bytes",
    }
    if (
        set(request) != required
        or request.get("schema_version") != WORKER_REQUEST_SCHEMA
        or not isinstance(request.get("project_id"), str)
        or not isinstance(request.get("analysis_job_id"), str)
        or not isinstance(request.get("recipe_id"), str)
        or _SHA256.fullmatch(str(request.get("recipe_id"))) is None
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO worker request fields are invalid"
        )
    pack = load_verified_grounding_dino_pack(
        Path(str(request["pack_manifest"])),
        trusted_manifest_sha256=(
            PINNED_EVALUATION_PACK_MANIFEST_SHA256
        ),
    )
    if request.get("pack_manifest_sha256") != pack.manifest_sha256:
        raise CommandProposalGenerationError(
            "Grounding DINO pack changed before inference"
        )
    queries = request.get("queries")
    candidates = request.get("candidates")
    if (
        not isinstance(queries, list)
        or not 1 <= len(queries) <= len(pack.queries_by_issue_type)
        or not isinstance(candidates, list)
        or not 1 <= len(candidates) <= pack.maximum_frames_per_run
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO worker scope is invalid"
        )
    expected_queries = {
        json.dumps(
            dict(row),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in pack.queries_by_issue_type.values()
    }
    if (
        len(
            {
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for row in queries
                if isinstance(row, Mapping)
            }
        )
        != len(queries)
        or any(
            not isinstance(row, Mapping)
            or json.dumps(
                dict(row),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            not in expected_queries
            for row in queries
        )
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO worker query contract changed"
        )
    box_threshold = request.get("box_threshold")
    text_threshold = request.get("text_threshold")
    maximum_predictions = request.get("max_predictions_per_frame")
    maximum_cuda_bytes = request.get("maximum_cuda_allocator_bytes")
    if (
        isinstance(box_threshold, bool)
        or not isinstance(box_threshold, (int, float))
        or not 0.01 <= float(box_threshold) <= 0.99
        or isinstance(text_threshold, bool)
        or not isinstance(text_threshold, (int, float))
        or not 0.01 <= float(text_threshold) <= 0.99
        or isinstance(maximum_predictions, bool)
        or not isinstance(maximum_predictions, int)
        or not 1 <= maximum_predictions
        <= pack.maximum_predictions_per_frame
        or maximum_cuda_bytes
        != pack.manifest["device_policy"][
            "maximum_cuda_allocator_bytes"
        ]
    ):
        raise CommandProposalGenerationError(
            "Grounding DINO worker limits are invalid"
        )
    candidate_rows = []
    seen: set[str] = set()
    for row in candidates:
        if (
            not isinstance(row, Mapping)
            or set(row)
            != {
                "candidate_id",
                "image_path",
                "image_size_bytes",
                "image_sha256",
            }
        ):
            raise CommandProposalGenerationError(
                "Grounding DINO worker candidate is invalid"
            )
        candidate_id = row.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID.fullmatch(candidate_id) is None
            or candidate_id in seen
        ):
            raise CommandProposalGenerationError(
                "Grounding DINO worker candidate identity is invalid"
            )
        seen.add(candidate_id)
        candidate_rows.append(
            (
                candidate_id,
                _safe_candidate(
                    str(row["image_path"]),
                    row["image_size_bytes"],
                    str(row["image_sha256"]),
                ),
            )
        )

    torch = _optional_module("torch")
    transformers = _optional_module("transformers")
    image_module = _optional_module("PIL.Image")
    cuda_available = bool(torch.cuda.is_available())
    device = "cuda:0" if cuda_available else "cpu"
    if cuda_available:
        total = int(torch.cuda.get_device_properties(0).total_memory)
        torch.cuda.set_per_process_memory_fraction(
            min(1.0, int(maximum_cuda_bytes) / total), 0
        )
    processor = transformers.AutoProcessor.from_pretrained(
        str(pack.snapshot),
        local_files_only=True,
        trust_remote_code=False,
    )
    model = (
        transformers.AutoModelForZeroShotObjectDetection.from_pretrained(
            str(pack.snapshot),
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            torch_dtype=torch.float32,
        )
        .to(device)
        .eval()
    )
    prompt_text = (
        ". ".join(str(row["prompt"]) for row in queries).casefold()
        + "."
    )
    results = []
    for candidate_id, image_path in candidate_rows:
        with image_module.open(image_path) as opened:
            opened.load()
            if (
                opened.width <= 0
                or opened.height <= 0
                or opened.width * opened.height > MAX_IMAGE_PIXELS
            ):
                raise CommandProposalGenerationError(
                    "Grounding DINO image dimensions are invalid"
                )
            image = opened.convert("RGB")
        inputs = processor(
            images=image,
            text=prompt_text,
            return_tensors="pt",
        ).to(device)
        if "pixel_values" in inputs:
            inputs["pixel_values"] = inputs["pixel_values"].to(
                dtype=torch.float32
            )
        with torch.inference_mode():
            outputs = model(**inputs)
        processed = processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            threshold=float(box_threshold),
            text_threshold=float(text_threshold),
            target_sizes=[(image.height, image.width)],
        )[0]
        scores = processed["scores"].detach().float().cpu().tolist()
        boxes = processed["boxes"].detach().float().cpu().tolist()
        labels = processed.get("text_labels", processed.get("labels"))
        if not isinstance(labels, list):
            labels = list(labels)
        ranked = sorted(
            zip(scores, boxes, labels),
            key=lambda item: float(item[0]),
            reverse=True,
        )[:maximum_predictions]
        predictions = []
        for score, box, label in ranked:
            if (
                isinstance(score, bool)
                or not math.isfinite(float(score))
                or not 0 <= float(score) <= 1
                or not isinstance(box, (list, tuple))
                or len(box) != 4
                or not isinstance(label, str)
                or not label.strip()
            ):
                raise CommandProposalGenerationError(
                    "Grounding DINO prediction is invalid"
                )
            x1, y1, x2, y2 = (float(value) for value in box)
            x1 = min(max(x1, 0.0), float(image.width))
            x2 = min(max(x2, 0.0), float(image.width))
            y1 = min(max(y1, 0.0), float(image.height))
            y2 = min(max(y2, 0.0), float(image.height))
            if x2 <= x1 or y2 <= y1:
                continue
            query = _map_text_label(label, queries)
            predictions.append(
                {
                    "issue_type": query["issue_type"],
                    "relative_score": None,
                    "model_score": round(float(score), 8),
                    "box": {
                        "x": round(x1 / image.width, 8),
                        "y": round(y1 / image.height, 8),
                        "width": round((x2 - x1) / image.width, 8),
                        "height": round((y2 - y1) / image.height, 8),
                    },
                    "source_label": label.strip()[:128],
                    "calibration": "model_score",
                }
            )
        results.append(
            {
                "candidate_id": candidate_id,
                "status": (
                    "classified" if predictions else "no_suggestion"
                ),
                "predictions": predictions,
            }
        )
    return {
        "schema_version": WORKER_RESPONSE_SCHEMA,
        "project_id": request["project_id"],
        "analysis_job_id": request["analysis_job_id"],
        "recipe_id": request["recipe_id"],
        "pack_manifest_sha256": pack.manifest_sha256,
        "score_semantics": SCORE_SEMANTICS,
        "results": results,
    }


def main() -> int:
    try:
        response = run(_read_request())
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if not 1 <= len(encoded) <= MAX_RESPONSE_BYTES:
            return 1
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
