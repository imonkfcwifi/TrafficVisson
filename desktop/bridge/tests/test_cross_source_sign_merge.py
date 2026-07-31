import hashlib
import json

import cv2
import numpy as np

from app.cross_source_sign_merge import (
    evaluate_cross_source_sign_merge,
)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _images(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(17)
    first = rng.integers(0, 256, (540, 960), dtype=np.uint8)
    first = cv2.GaussianBlur(first, (3, 3), 0)
    second = cv2.warpAffine(
        first,
        np.float32([[1.005, 0.001, -3.0], [-0.001, 1.005, -2.0]]),
        (960, 540),
    )
    first_path = (tmp_path / "local.jpg").resolve()
    second_path = (tmp_path / "dino.jpg").resolve()
    assert cv2.imwrite(str(first_path), first)
    assert cv2.imwrite(str(second_path), second)
    return first_path, second_path


def _evaluate(tmp_path, **overrides):
    local_path, dino_path = _images(tmp_path)
    values = {
        "local_candidate_id": "a" * 20,
        "dino_parent_candidate_ids": ["a" * 20],
        "local_issue_type": "guide_sign_identity",
        "dino_issue_type": "guide_sign_identity",
        "local_timestamp_seconds": 79.0,
        "dino_timestamp_seconds": 79.25,
        "local_box": {
            "x": 0.06,
            "y": 0.60888889,
            "width": 0.025,
            "height": 0.02666667,
        },
        "dino_box": {
            "x": 0.00366222,
            "y": 0.57266554,
            "width": 0.03426578,
            "height": 0.10737515,
        },
        "ocr_observations": [
            {
                "text": "대전",
                "confidence": 0.968,
                "box": {
                    "x": 0.06,
                    "y": 0.60888889,
                    "width": 0.025,
                    "height": 0.02666667,
                },
            }
        ],
        "local_image_path": local_path,
        "local_image_sha256": _sha(local_path),
        "dino_image_path": dino_path,
        "dino_image_sha256": _sha(dino_path),
    }
    values.update(overrides)
    return evaluate_cross_source_sign_merge(**values)


def test_adjacent_partial_sign_boxes_merge_with_camera_motion_proof(
    tmp_path,
):
    proof = _evaluate(tmp_path)

    assert proof is not None
    assert proof["time_gap_seconds"] == 0.25
    assert proof["camera_motion"]["affine_inlier_count"] >= 16
    assert proof["geometry"]["vertical_overlap_ratio"] >= 0.6
    assert proof["geometry"]["horizontal_gap"] <= 0.03
    revision = proof.pop("proof_revision")
    assert revision == hashlib.sha256(
        json.dumps(
            proof,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def test_different_sign_box_time_or_parent_fails_closed(tmp_path):
    assert _evaluate(
        tmp_path / "far",
        dino_box={
            "x": 0.55,
            "y": 0.57,
            "width": 0.04,
            "height": 0.1,
        },
    ) is None
    assert _evaluate(
        tmp_path / "late",
        dino_timestamp_seconds=79.875,
    ) is None
    assert _evaluate(
        tmp_path / "parent",
        dino_parent_candidate_ids=["b" * 20],
    ) is None
