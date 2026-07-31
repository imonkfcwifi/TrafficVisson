from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from app.sam2_transformers_adapter import (
    DEFAULT_PACK_ID,
    DEFAULT_PACK_VERSION,
    MANIFEST_NAME,
    PACK_SCHEMA,
    TransformersSam2Adapter,
)


REVISION = "d" * 40


def _sha(path: Path) -> tuple[str, int]:
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _pack(tmp_path: Path) -> Path:
    root = tmp_path / "sam2-pack"
    model = root / "model"
    model.mkdir(parents=True)
    payloads = {
        "README.md": b"Apache-2.0 SAM2 fixture",
        "config.json": b"{}",
        "model.safetensors": b"safe-test-weights",
        "preprocessor_config.json": b"{}",
        "processor_config.json": b"{}",
    }
    artifacts = []
    for name, payload in payloads.items():
        path = model / name
        path.write_bytes(payload)
        digest, size = _sha(path)
        artifacts.append(
            {
                "path": f"model/{name}",
                "size_bytes": size,
                "sha256": digest,
            }
        )
    model_sha = next(
        row["sha256"]
        for row in artifacts
        if row["path"] == "model/model.safetensors"
    )
    manifest = {
        "schema_version": PACK_SCHEMA,
        "pack_id": DEFAULT_PACK_ID,
        "pack_version": DEFAULT_PACK_VERSION,
        "provider": "transformers_sam2",
        "model_name": "facebook/sam2.1-hiera-tiny",
        "model_version": REVISION,
        "model_directory": "model",
        "model_artifact_path": "model/model.safetensors",
        "model_artifact_sha256": model_sha,
        "source_repo_id": "facebook/sam2.1-hiera-tiny",
        "source_revision": REVISION,
        "source_url": (
            "https://huggingface.co/facebook/sam2.1-hiera-tiny"
        ),
        "weights_spdx": "Apache-2.0",
        "runtime_spdx": "Apache-2.0",
        "commercial_use_confirmed": True,
        "redistribution_allowed": True,
        "gpu_required": True,
        "cpu_fallback": True,
        "recommended_min_vram_mb": 4096,
        "cuda_min_version": "12.0",
        "review_only": True,
        "artifacts": sorted(artifacts, key=lambda row: row["path"]),
    }
    (root / MANIFEST_NAME).write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return root


def _runtime():
    return {
        "state": "ready",
        "error_code": None,
        "device": "cpu",
        "cuda_ready": False,
    }


def test_verified_pack_status_is_path_free_and_review_only(tmp_path):
    pack = _pack(tmp_path)
    adapter = TransformersSam2Adapter(
        pack,
        predictor=lambda _path, _point: np.ones((8, 8), dtype=np.uint8),
        runtime_probe=_runtime,
    )
    status = adapter.status()
    assert status["state"] == "ready"
    assert status["runtime_device"] == "cpu"
    assert status["model_manifest"]["pack_id"] == DEFAULT_PACK_ID
    assert status["model_manifest"]["review_only"] is True
    assert status["model_manifest"]["provenance_verified"] is True
    assert "path" not in json.dumps(status).casefold()


def test_refine_returns_bounded_polygon_without_training_or_incident(tmp_path):
    pack = _pack(tmp_path)
    image = tmp_path / "candidate.png"
    Image.new("RGB", (64, 48), "black").save(image)

    def predictor(_path, _point):
        mask = np.zeros((48, 64), dtype=np.uint8)
        mask[12:37, 18:51] = 1
        return mask

    adapter = TransformersSam2Adapter(
        pack,
        predictor=predictor,
        runtime_probe=_runtime,
    )
    result = adapter.refine(image_path=image, point=(0.5, 0.5))
    assert 3 <= len(result["polygon"]) <= 256
    assert result["tracked_frame_refs"] == []
    assert all(
        0 <= coordinate <= 1
        for point in result["polygon"]
        for coordinate in point
    )
    assert "annotation" not in result
    assert "incident" not in result
    assert "training" not in result


def test_tampered_model_pack_is_unavailable(tmp_path):
    pack = _pack(tmp_path)
    (pack / "model" / "model.safetensors").write_bytes(b"tampered")
    adapter = TransformersSam2Adapter(
        pack,
        predictor=lambda _path, _point: np.ones((8, 8), dtype=np.uint8),
        runtime_probe=_runtime,
    )
    assert adapter.status() == {
        "state": "unavailable",
        "error_code": "model_pack_not_configured",
    }


def test_empty_or_degenerate_mask_fails_closed(tmp_path):
    pack = _pack(tmp_path)
    image = tmp_path / "candidate.png"
    Image.new("RGB", (32, 32), "black").save(image)
    adapter = TransformersSam2Adapter(
        pack,
        predictor=lambda _path, _point: np.zeros((32, 32), dtype=np.uint8),
        runtime_probe=_runtime,
    )
    try:
        adapter.refine(image_path=image, point=(0.5, 0.5))
    except ValueError as error:
        assert "mask is empty" in str(error)
    else:
        raise AssertionError("empty SAM2 masks must fail closed")


def test_track_returns_review_only_polygons_for_verified_frame_window(
    tmp_path,
):
    pack = _pack(tmp_path)
    frames = []
    for index in range(3):
        path = tmp_path / f"frame-{index}.png"
        Image.new("RGB", (64, 48), (index, index, index)).save(path)
        frames.append(path)

    def tracker(paths, prompt_frame_index, point):
        assert tuple(paths) == tuple(frames)
        assert prompt_frame_index == 1
        assert point == (0.5, 0.5)
        return {
            "frames": [
                {
                    "frame_index": 2,
                    "polygon": [
                        [0.2, 0.2],
                        [0.7, 0.2],
                        [0.7, 0.7],
                    ],
                },
                {
                    "frame_index": 0,
                    "polygon": [
                        [0.1, 0.1],
                        [0.6, 0.1],
                        [0.6, 0.6],
                    ],
                },
            ]
        }

    adapter = TransformersSam2Adapter(
        pack,
        predictor=lambda _path, _point: np.ones(
            (8, 8), dtype=np.uint8
        ),
        tracker=tracker,
        runtime_probe=_runtime,
    )
    result = adapter.track(
        frame_paths=frames,
        prompt_frame_index=1,
        point=(0.5, 0.5),
    )
    assert [
        item["frame_index"] for item in result["frames"]
    ] == [0, 2]
    assert "annotation" not in result
    assert "incident" not in result
    assert "training" not in result


def test_track_rejects_duplicate_frames_and_invalid_tracker_output(
    tmp_path,
):
    pack = _pack(tmp_path)
    first = tmp_path / "frame-0.png"
    second = tmp_path / "frame-1.png"
    Image.new("RGB", (32, 32), "black").save(first)
    Image.new("RGB", (32, 32), "white").save(second)
    adapter = TransformersSam2Adapter(
        pack,
        predictor=lambda _path, _point: np.ones(
            (8, 8), dtype=np.uint8
        ),
        tracker=lambda _paths, _index, _point: {
            "frames": [
                {
                    "frame_index": 9,
                    "polygon": [
                        [0.1, 0.1],
                        [0.5, 0.1],
                        [0.5, 0.5],
                    ],
                }
            ]
        },
        runtime_probe=_runtime,
    )
    with pytest.raises(ValueError, match="duplicated"):
        adapter.track(
            frame_paths=[first, first],
            prompt_frame_index=0,
            point=(0.5, 0.5),
        )
    with pytest.raises(ValueError, match="frame index"):
        adapter.track(
            frame_paths=[first, second],
            prompt_frame_index=0,
            point=(0.5, 0.5),
        )
