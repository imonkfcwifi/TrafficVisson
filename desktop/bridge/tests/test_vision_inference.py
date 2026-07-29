from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.vision_inference import (
    CANONICAL_ISSUE_TYPES,
    INFERENCE_SCHEMA,
    MODEL_PACK_SCHEMA,
    WORKER_SCHEMA,
    LocalVisionInferenceService,
    VisionInferenceCancelled,
    VisionInferenceTimeout,
    VisionModelPackError,
    load_model_pack,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pack(
    root: Path,
    *,
    commercial_use_confirmed: bool = True,
    digest_override: str | None = None,
) -> Path:
    root.mkdir(parents=True)
    model = root / "road-common.onnx"
    model.write_bytes(b"fixture-only-onnx-model")
    manifest = {
        "schema_version": MODEL_PACK_SCHEMA,
        "pack_id": "trafficvisson-road-common",
        "pack_version": "0.1.0",
        "engine": "onnx_yolo_v8",
        "license": {
            "runtime_spdx": "MIT",
            "weights_spdx": "Apache-2.0",
            "commercial_use_confirmed": commercial_use_confirmed,
            "redistribution_allowed": False,
            "notice": "Fixture weights are not redistributed.",
        },
        "artifacts": [
            {
                "path": model.name,
                "size_bytes": model.stat().st_size,
                "sha256": digest_override or _sha256(model),
            }
        ],
        "model": {
            "file": model.name,
            "input_width": 640,
            "input_height": 640,
            "labels": [
                {
                    "class_id": 0,
                    "issue_type": "pothole",
                    "display_name": "포트홀",
                },
                {
                    "class_id": 1,
                    "issue_type": "road_debris",
                    "display_name": "낙하물",
                },
            ],
            "confidence_threshold": 0.35,
            "nms_iou_threshold": 0.5,
            "max_detections": 8,
            "execution_provider": "auto",
        },
    }
    manifest_path = root / "trafficsupervision-model-pack.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest_path


def _candidate(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    image = root / "candidate.jpg"
    image.write_bytes(b"fixture-image-bytes")
    return {
        "candidate_id": "a" * 20,
        "representative_image": {
            "relative_path": image.name,
            "size_bytes": image.stat().st_size,
            "sha256": _sha256(image),
        },
    }


def test_unconfigured_provider_is_explicit_scene_fallback(tmp_path: Path) -> None:
    service = LocalVisionInferenceService(model_pack_path=None)
    candidate = _candidate(tmp_path)

    results, summary = service.infer_candidates([candidate], tmp_path)

    result = results[candidate["candidate_id"]]
    assert result == {
        "schema_version": INFERENCE_SCHEMA,
        "mode": "scene_fallback",
        "status": "provider_unavailable",
        "provider": "none",
        "pack_id": None,
        "pack_version": None,
        "manifest_sha256": None,
        "capabilities": [],
        "predictions": [],
        "ocr_observations": [],
        "duration_ms": 0,
        "warning_code": "model_pack_not_configured",
    }
    assert summary["mode"] == "scene_fallback"
    assert summary["engine"] == "none"
    assert summary["status_counts"] == {"provider_unavailable": 1}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"commercial_use_confirmed": False}, "commercial-use"),
        ({"digest_override": "0" * 64}, "integrity"),
    ],
)
def test_pack_requires_license_acknowledgement_and_integrity(
    tmp_path: Path, changes: dict, message: str
) -> None:
    manifest = _write_pack(tmp_path / "pack", **changes)
    with pytest.raises(VisionModelPackError, match=message):
        load_model_pack(manifest)


def test_invalid_configured_pack_does_not_disable_scene_review(
    tmp_path: Path,
) -> None:
    manifest = _write_pack(tmp_path / "pack", digest_override="0" * 64)
    service = LocalVisionInferenceService(model_pack_path=manifest)
    candidate = _candidate(tmp_path / "evidence")

    results, summary = service.infer_candidates(
        [candidate], tmp_path / "evidence"
    )

    assert service.status() == {
        "enabled": False,
        "mode": "scene_fallback",
        "warning_code": "model_pack_invalid",
    }
    assert results[candidate["candidate_id"]]["warning_code"] == "model_pack_invalid"
    assert summary["warning_code"] == "model_pack_invalid"


def test_valid_pack_accepts_only_strict_model_output(tmp_path: Path) -> None:
    manifest = _write_pack(tmp_path / "pack")
    candidate = _candidate(tmp_path / "evidence")

    def runner(pack, candidates, timeout_seconds, cancel):
        assert timeout_seconds == 30
        assert candidates[0]["candidate_id"] == candidate["candidate_id"]
        assert Path(candidates[0]["image_path"]).is_absolute()
        return {
            "schema_version": WORKER_SCHEMA,
            "pack_id": pack.pack_id,
            "pack_version": pack.pack_version,
            "manifest_sha256": pack.manifest_sha256,
            "results": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "predictions": [
                        {
                            "issue_type": "pothole",
                            "confidence": 0.91,
                            "box": {
                                "x": 0.1,
                                "y": 0.2,
                                "width": 0.3,
                                "height": 0.4,
                            },
                            "source_label": "포트홀",
                            "calibration": "model_score",
                        }
                    ],
                    "ocr_observations": [],
                    "duration_ms": 17,
                }
            ],
        }

    service = LocalVisionInferenceService(
        model_pack_path=manifest,
        timeout_seconds=30,
        runner=runner,
    )
    results, summary = service.infer_candidates(
        [candidate], tmp_path / "evidence"
    )

    result = results[candidate["candidate_id"]]
    assert service.status()["enabled"] is True
    assert result["mode"] == "local_model"
    assert result["status"] == "classified"
    assert result["predictions"][0]["issue_type"] == "pothole"
    assert result["predictions"][0]["box"]["width"] == 0.3
    assert result["warning_code"] is None
    assert summary["mode"] == "local_model"
    assert summary["status_counts"] == {"classified": 1}
    assert summary["runtime_spdx"] == "MIT"
    assert summary["weights_spdx"] == "Apache-2.0"
    assert summary["redistribution_allowed"] is False


def test_canonical_twelve_type_pack_and_legacy_predictions_normalise(
    tmp_path: Path,
) -> None:
    manifest = _write_pack(tmp_path / "pack")
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_payload["model"]["labels"] = [
        {
            "class_id": index,
            "issue_type": issue_type,
            "display_name": issue_type,
        }
        for index, issue_type in enumerate(CANONICAL_ISSUE_TYPES)
    ]
    # Existing installed packs may still use these identifiers.  They remain
    # valid inputs, but no downstream result may persist them.
    manifest_payload["model"]["labels"][7]["issue_type"] = "sign_identity"
    manifest_payload["model"]["labels"][11]["issue_type"] = (
        "color_guidance_line_faded"
    )
    manifest.write_text(
        json.dumps(manifest_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    pack = load_model_pack(manifest)
    assert len(CANONICAL_ISSUE_TYPES) == 12
    assert "sign_damage" in CANONICAL_ISSUE_TYPES
    assert "tunnel_name_occlusion" in CANONICAL_ISSUE_TYPES
    assert pack.manifest["model"]["labels"][7]["issue_type"] == "sign_identity"

    candidate = _candidate(tmp_path / "evidence")

    def runner(loaded_pack, *_):
        return {
            "schema_version": WORKER_SCHEMA,
            "pack_id": loaded_pack.pack_id,
            "pack_version": loaded_pack.pack_version,
            "manifest_sha256": loaded_pack.manifest_sha256,
            "results": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "predictions": [
                        {
                            "issue_type": "sign_identity",
                            "confidence": 0.91,
                            "box": None,
                            "source_label": "이정표",
                            "calibration": "model_score",
                        },
                        {
                            "issue_type": "color_guidance_line_faded",
                            "confidence": 0.82,
                            "box": None,
                            "source_label": "색깔유도선",
                            "calibration": "model_score",
                        },
                        {
                            "issue_type": "sign_damage",
                            "confidence": 0.73,
                            "box": None,
                            "source_label": "표지판 파손",
                            "calibration": "model_score",
                        },
                        {
                            "issue_type": "tunnel_name_occlusion",
                            "confidence": 0.64,
                            "box": None,
                            "source_label": "터널명 가림",
                            "calibration": "model_score",
                        },
                    ],
                    "ocr_observations": [],
                    "duration_ms": 12,
                }
            ],
        }

    results, summary = LocalVisionInferenceService(
        model_pack_path=manifest,
        runner=runner,
    ).infer_candidates([candidate], tmp_path / "evidence")

    assert [
        row["issue_type"]
        for row in results[candidate["candidate_id"]]["predictions"]
    ] == [
        "guide_sign_identity",
        "colored_guidance_faded",
        "sign_damage",
        "tunnel_name_occlusion",
    ]
    assert summary["runtime_spdx"] == "MIT"
    assert summary["weights_spdx"] == "Apache-2.0"
    assert summary["redistribution_allowed"] is False


@pytest.mark.parametrize(
    "legacy_issue_type",
    [
        "surface_damage",
        "debris",
        "sign_vegetation",
        "mowing",
        "sign_tunnel_identity",
        "lane_color_line_fading",
        "colored_guidance_line_faded",
    ],
)
def test_all_supported_legacy_pack_issue_types_remain_input_compatible(
    tmp_path: Path,
    legacy_issue_type: str,
) -> None:
    manifest = _write_pack(tmp_path / legacy_issue_type)
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_payload["model"]["labels"][0]["issue_type"] = legacy_issue_type
    manifest.write_text(
        json.dumps(manifest_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    assert load_model_pack(manifest).manifest_sha256


def test_timeout_and_malformed_output_never_become_predictions(
    tmp_path: Path,
) -> None:
    manifest = _write_pack(tmp_path / "pack")
    candidate = _candidate(tmp_path / "evidence")

    def timeout_runner(*_):
        raise VisionInferenceTimeout("bounded timeout")

    timed_out, _ = LocalVisionInferenceService(
        model_pack_path=manifest,
        runner=timeout_runner,
    ).infer_candidates([candidate], tmp_path / "evidence")
    timeout_result = timed_out[candidate["candidate_id"]]
    assert timeout_result["mode"] == "scene_fallback"
    assert timeout_result["status"] == "timed_out"
    assert timeout_result["predictions"] == []
    assert timeout_result["warning_code"] == "model_timeout"

    def malformed_runner(pack, *_):
        return {
            "schema_version": WORKER_SCHEMA,
            "pack_id": pack.pack_id,
            "pack_version": pack.pack_version,
            "manifest_sha256": pack.manifest_sha256,
            "results": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "predictions": [
                        {
                            "issue_type": "pothole",
                            "confidence": 1.4,
                            "box": None,
                            "source_label": "forged",
                            "calibration": "model_score",
                        }
                    ],
                    "ocr_observations": [],
                    "duration_ms": 1,
                }
            ],
        }

    failed, _ = LocalVisionInferenceService(
        model_pack_path=manifest,
        runner=malformed_runner,
    ).infer_candidates([candidate], tmp_path / "evidence")
    failed_result = failed[candidate["candidate_id"]]
    assert failed_result["mode"] == "scene_fallback"
    assert failed_result["status"] == "failed"
    assert failed_result["predictions"] == []
    assert failed_result["warning_code"] == "model_inference_failed"


def test_cancel_is_propagated_without_a_fallback_record(tmp_path: Path) -> None:
    manifest = _write_pack(tmp_path / "pack")
    candidate = _candidate(tmp_path / "evidence")
    called = False

    def runner(*_):
        nonlocal called
        called = True
        raise AssertionError

    service = LocalVisionInferenceService(
        model_pack_path=manifest,
        runner=runner,
    )
    with pytest.raises(VisionInferenceCancelled):
        service.infer_candidates(
            [candidate],
            tmp_path / "evidence",
            cancel=lambda: True,
        )
    assert called is False
