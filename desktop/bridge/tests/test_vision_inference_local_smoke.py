"""Developer-only smoke test for already-cached EasyOCR files.

Run explicitly with::

    $env:TRAFFICVISSON_RUN_LOCAL_VISION_SMOKE="1"
    python -m pytest -q tests/test_vision_inference_local_smoke.py

The test never downloads a model.  It hard-links the two existing cache files
into a temporary hash-inventoried pack and removes the pack with pytest's
temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from app.vision_inference import (
    MODEL_PACK_SCHEMA,
    LocalVisionInferenceService,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("TRAFFICVISSON_RUN_LOCAL_VISION_SMOKE") != "1",
    reason="local OCR smoke is opt-in and never downloads weights",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def test_cached_easyocr_pack_runs_offline_on_real_tunnel_frame(
    tmp_path: Path,
) -> None:
    source_models = Path.home() / ".EasyOCR" / "model"
    required = [
        source_models / "craft_mlt_25k.pth",
        source_models / "korean_g2.pth",
    ]
    if not all(path.is_file() for path in required):
        pytest.skip("cached Korean EasyOCR files are unavailable")

    pack_root = tmp_path / "pack"
    model_root = pack_root / "ocr"
    model_root.mkdir(parents=True)
    artifacts = []
    for source in required:
        target = model_root / source.name
        # The production pack validator rejects hard links, so the opt-in
        # smoke deliberately exercises the same independent-file boundary.
        shutil.copyfile(source, target)
        artifacts.append(
            {
                "path": target.relative_to(pack_root).as_posix(),
                "size_bytes": target.stat().st_size,
                "sha256": _sha256(target),
            }
        )
    manifest = {
        "schema_version": MODEL_PACK_SCHEMA,
        "pack_id": "easyocr-korean-local-smoke",
        "pack_version": "1.7.1",
        "engine": "easyocr_ocr_only",
        "license": {
            "runtime_spdx": "Apache-2.0",
            "weights_spdx": "Apache-2.0",
            "commercial_use_confirmed": True,
            "redistribution_allowed": False,
            "notice": "Developer-only local smoke; cached files are not redistributed.",
        },
        "artifacts": artifacts,
        "ocr": {
            "engine": "easyocr",
            "model_storage_directory": "ocr",
            "languages": ["ko", "en"],
            "minimum_confidence": 0.01,
            "max_observations": 16,
            "device": "auto",
        },
    }
    manifest_path = pack_root / "trafficvisson-model-pack.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    desktop_root = Path(__file__).resolve().parents[2]
    source_image = (
        desktop_root / "public" / "assets" / "real-trip" / "tunnel-ocr-roi-222s.png"
    )
    if not source_image.is_file():
        pytest.skip("real tunnel OCR crop is unavailable")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    image = evidence / source_image.name
    shutil.copyfile(source_image, image)
    candidate = {
        "candidate_id": "b" * 20,
        "representative_image": {
            "relative_path": image.name,
            "size_bytes": image.stat().st_size,
            "sha256": _sha256(image),
        },
    }

    service = LocalVisionInferenceService(
        model_pack_path=manifest_path,
        timeout_seconds=180,
    )
    results, summary = service.infer_candidates([candidate], evidence)

    result = results[candidate["candidate_id"]]
    assert summary["engine"] == "easyocr_ocr_only"
    assert result["mode"] == "local_model"
    assert result["status"] == "classified"
    assert result["predictions"] == []
    assert result["ocr_observations"]
    assert all(
        row["kind"] == "sign_or_tunnel_text_unverified"
        for row in result["ocr_observations"]
    )
