"""Fail-closed OCR/DINO sign deduplication with camera-motion evidence.

The local OCR proposal usually covers only the recognized glyphs while
Grounding DINO may cover another visible portion of the same sign.  Raw IoU is
therefore insufficient.  A merge is allowed only when both proposals have the
same server-owned parent candidate, are close in time, and a robust global
frame transform places the OCR box vertically inside or beside the DINO box
with a small horizontal gap.  The returned proof is path-free and hashed.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


SCHEMA_VERSION = "trafficvisson.cross-source-sign-merge-proof.v1"
ALGORITHM = "orb-affine-ransac-adjacent-sign-v1"


@dataclass(frozen=True)
class CrossSourceSignMergePolicy:
    max_time_gap_seconds: float = 0.5
    minimum_ocr_confidence: float = 0.5
    minimum_feature_matches: int = 24
    minimum_affine_inliers: int = 16
    minimum_inlier_ratio: float = 0.4
    minimum_vertical_overlap_ratio: float = 0.6
    maximum_horizontal_gap: float = 0.03
    maximum_center_distance: float = 0.12
    minimum_scale: float = 0.8
    maximum_scale: float = 1.25
    maximum_rotation_degrees: float = 8.0


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _box(value: object) -> Optional[dict[str, float]]:
    if not isinstance(value, Mapping):
        return None
    values = []
    for key in ("x", "y", "width", "height"):
        item = value.get(key)
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            return None
        values.append(float(item))
    x, y, width, height = values
    if (
        width <= 0
        or height <= 0
        or x < 0
        or y < 0
        or x + width > 1.000001
        or y + height > 1.000001
    ):
        return None
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
    }


def _rounded(value: float) -> float:
    return round(float(value), 8)


def _edge_gap(
    first_start: float,
    first_end: float,
    second_start: float,
    second_end: float,
) -> float:
    if first_end < second_start:
        return second_start - first_end
    if second_end < first_start:
        return first_start - second_end
    return 0.0


def evaluate_cross_source_sign_merge(
    *,
    local_candidate_id: str,
    dino_parent_candidate_ids: Sequence[str],
    local_issue_type: str,
    dino_issue_type: str,
    local_timestamp_seconds: float,
    dino_timestamp_seconds: float,
    local_box: Mapping[str, Any],
    dino_box: Mapping[str, Any],
    ocr_observations: Sequence[Mapping[str, Any]],
    local_image_path: Path,
    local_image_sha256: str,
    dino_image_path: Path,
    dino_image_sha256: str,
    policy: CrossSourceSignMergePolicy = CrossSourceSignMergePolicy(),
) -> Optional[dict[str, Any]]:
    """Return an immutable merge proof, or ``None`` on any ambiguity."""

    if (
        local_issue_type != "guide_sign_identity"
        or dino_issue_type != "guide_sign_identity"
        or list(dino_parent_candidate_ids) != [local_candidate_id]
        or isinstance(local_timestamp_seconds, bool)
        or isinstance(dino_timestamp_seconds, bool)
        or not isinstance(local_timestamp_seconds, (int, float))
        or not isinstance(dino_timestamp_seconds, (int, float))
    ):
        return None
    time_gap = abs(
        float(dino_timestamp_seconds)
        - float(local_timestamp_seconds)
    )
    if time_gap > policy.max_time_gap_seconds:
        return None
    normalized_local = _box(local_box)
    normalized_dino = _box(dino_box)
    if normalized_local is None or normalized_dino is None:
        return None
    eligible_ocr = [
        {
            "text": str(item.get("text", "")).strip(),
            "confidence": float(item["confidence"]),
            "box": _box(item.get("box")),
        }
        for item in ocr_observations
        if (
            isinstance(item, Mapping)
            and isinstance(item.get("text"), str)
            and str(item["text"]).strip()
            and not isinstance(item.get("confidence"), bool)
            and isinstance(item.get("confidence"), (int, float))
            and float(item["confidence"])
            >= policy.minimum_ocr_confidence
            and _box(item.get("box")) is not None
        )
    ]
    if not eligible_ocr:
        return None
    local_path = Path(local_image_path)
    dino_path = Path(dino_image_path)
    if (
        not local_path.is_absolute()
        or not dino_path.is_absolute()
        or not local_path.is_file()
        or not dino_path.is_file()
        or _sha256_file(local_path) != local_image_sha256
        or _sha256_file(dino_path) != dino_image_sha256
    ):
        return None
    try:
        import cv2
        import numpy as np

        local_bytes = np.fromfile(str(local_path), dtype=np.uint8)
        dino_bytes = np.fromfile(str(dino_path), dtype=np.uint8)
        local_image = cv2.imdecode(local_bytes, cv2.IMREAD_GRAYSCALE)
        dino_image = cv2.imdecode(dino_bytes, cv2.IMREAD_GRAYSCALE)
        if (
            local_image is None
            or dino_image is None
            or local_image.shape != dino_image.shape
            or min(local_image.shape[:2]) < 128
        ):
            return None
        scale_factor = min(
            1.0,
            960.0
            / float(max(local_image.shape[0], local_image.shape[1])),
        )
        if scale_factor < 1.0:
            local_image = cv2.resize(
                local_image,
                None,
                fx=scale_factor,
                fy=scale_factor,
                interpolation=cv2.INTER_AREA,
            )
            dino_image = cv2.resize(
                dino_image,
                None,
                fx=scale_factor,
                fy=scale_factor,
                interpolation=cv2.INTER_AREA,
            )
        cv2.setRNGSeed(0)
        detector = cv2.ORB_create(
            nfeatures=2_000,
            fastThreshold=10,
        )
        local_keypoints, local_descriptors = (
            detector.detectAndCompute(local_image, None)
        )
        dino_keypoints, dino_descriptors = (
            detector.detectAndCompute(dino_image, None)
        )
        if local_descriptors is None or dino_descriptors is None:
            return None
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(
            local_descriptors,
            dino_descriptors,
            k=2,
        )
        matches = [
            first
            for first, second in pairs
            if first.distance < 0.75 * second.distance
        ]
        if len(matches) < policy.minimum_feature_matches:
            return None
        source_points = np.float32(
            [
                local_keypoints[item.queryIdx].pt
                for item in matches
            ]
        )
        target_points = np.float32(
            [
                dino_keypoints[item.trainIdx].pt
                for item in matches
            ]
        )
        affine, inlier_mask = cv2.estimateAffinePartial2D(
            source_points,
            target_points,
            method=cv2.RANSAC,
            ransacReprojThreshold=3.0,
            maxIters=3_000,
            confidence=0.995,
        )
        if affine is None or inlier_mask is None:
            return None
        inlier_count = int(inlier_mask.sum())
        inlier_ratio = inlier_count / len(matches)
        if (
            inlier_count < policy.minimum_affine_inliers
            or inlier_ratio < policy.minimum_inlier_ratio
        ):
            return None
        a, b = float(affine[0, 0]), float(affine[0, 1])
        camera_scale = math.sqrt(a * a + b * b)
        rotation_degrees = abs(math.degrees(math.atan2(b, a)))
        if (
            not policy.minimum_scale
            <= camera_scale
            <= policy.maximum_scale
            or rotation_degrees > policy.maximum_rotation_degrees
        ):
            return None
        height, width = dino_image.shape[:2]
        x1 = normalized_local["x"] * width
        y1 = normalized_local["y"] * height
        x2 = (
            normalized_local["x"] + normalized_local["width"]
        ) * width
        y2 = (
            normalized_local["y"] + normalized_local["height"]
        ) * height
        corners = np.float64(
            [[x1, y1, 1.0], [x2, y1, 1.0], [x2, y2, 1.0], [x1, y2, 1.0]]
        )
        transformed = corners @ affine.T
        transformed_box = {
            "x": float(transformed[:, 0].min() / width),
            "y": float(transformed[:, 1].min() / height),
            "width": float(
                (
                    transformed[:, 0].max()
                    - transformed[:, 0].min()
                )
                / width
            ),
            "height": float(
                (
                    transformed[:, 1].max()
                    - transformed[:, 1].min()
                )
                / height
            ),
        }
    except Exception:
        return None

    tx1, ty1 = transformed_box["x"], transformed_box["y"]
    tx2 = tx1 + transformed_box["width"]
    ty2 = ty1 + transformed_box["height"]
    dx1, dy1 = normalized_dino["x"], normalized_dino["y"]
    dx2 = dx1 + normalized_dino["width"]
    dy2 = dy1 + normalized_dino["height"]
    vertical_overlap = max(0.0, min(ty2, dy2) - max(ty1, dy1))
    vertical_overlap_ratio = vertical_overlap / min(
        transformed_box["height"],
        normalized_dino["height"],
    )
    horizontal_gap = _edge_gap(tx1, tx2, dx1, dx2)
    center_distance = math.hypot(
        (tx1 + tx2) / 2.0 - (dx1 + dx2) / 2.0,
        (ty1 + ty2) / 2.0 - (dy1 + dy2) / 2.0,
    )
    if (
        vertical_overlap_ratio
        < policy.minimum_vertical_overlap_ratio
        or horizontal_gap > policy.maximum_horizontal_gap
        or center_distance > policy.maximum_center_distance
    ):
        return None
    proof = {
        "schema_version": SCHEMA_VERSION,
        "algorithm": ALGORITHM,
        "local_candidate_id": local_candidate_id,
        "dino_parent_candidate_ids": list(
            dino_parent_candidate_ids
        ),
        "issue_type": "guide_sign_identity",
        "local_timestamp_seconds": _rounded(
            local_timestamp_seconds
        ),
        "dino_timestamp_seconds": _rounded(
            dino_timestamp_seconds
        ),
        "time_gap_seconds": _rounded(time_gap),
        "local_image_sha256": local_image_sha256,
        "dino_image_sha256": dino_image_sha256,
        "local_ocr_box": {
            key: _rounded(value)
            for key, value in normalized_local.items()
        },
        "dino_box": {
            key: _rounded(value)
            for key, value in normalized_dino.items()
        },
        "camera_motion": {
            "feature_match_count": len(matches),
            "affine_inlier_count": inlier_count,
            "affine_inlier_ratio": _rounded(inlier_ratio),
            "scale": _rounded(camera_scale),
            "rotation_degrees": _rounded(rotation_degrees),
            "affine_matrix": [
                [_rounded(value) for value in row]
                for row in affine.tolist()
            ],
            "transformed_ocr_box": {
                key: _rounded(value)
                for key, value in transformed_box.items()
            },
        },
        "geometry": {
            "vertical_overlap_ratio": _rounded(
                vertical_overlap_ratio
            ),
            "horizontal_gap": _rounded(horizontal_gap),
            "center_distance": _rounded(center_distance),
        },
        "ocr_evidence": [
            {
                "text": item["text"],
                "confidence": _rounded(item["confidence"]),
                "box": {
                    key: _rounded(value)
                    for key, value in item["box"].items()
                },
            }
            for item in eligible_ocr
        ],
        "policy": asdict(policy),
        "review_only": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
    }
    proof["proof_revision"] = hashlib.sha256(
        _canonical_json(proof)
    ).hexdigest()
    return proof
