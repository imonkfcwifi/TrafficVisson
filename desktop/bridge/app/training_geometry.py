"""Canonical, path-free geometry truth used by project vision trainers.

Candidate proposals and SAM2 outputs are review evidence until an answer is
accepted.  This module freezes the exact representative image, final box,
optional mask polygon, and temporal tracking references that were accepted for
training.  The resulting object is safe to embed in the hash-chained label
record and later in an Annotation/DatasetVersion.

The contract deliberately contains no filesystem paths.  Trainers resolve the
server-owned ``image_artifact_ref`` and ``mask_artifact_ref`` only after the
DatasetVersion and project boundary have been verified.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional


SCHEMA_VERSION = "trafficvisson.training-geometry.v1"
TRACKED_FRAME_BINDING_SCHEMA = (
    "trafficvisson.tracked-frame-training-reference.v1"
)
SOURCE_KINDS = frozenset(
    {
        "accepted_ai_proposal",
        "user_sam2_refinement",
        "delegated_ai_proposal",
        "negative_evidence",
    }
)
APPROVAL_AUTHORITIES = frozenset(
    {"local_user", "structured_llm_assisted"}
)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ARTIFACT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,511}$")
_FRAME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_TRACK_REFS = 256
_MAX_POLYGON_POINTS = 256
_MAX_CANONICAL_BYTES = 256 * 1024


class TrainingGeometryError(ValueError):
    """Training geometry is incomplete, unsafe, or non-canonical."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object, field: str, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise TrainingGeometryError(f"{field} must be a SHA-256 digest")
    return value


def _artifact_ref(
    value: object,
    field: str,
    *,
    optional: bool = False,
) -> Optional[str]:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or _ARTIFACT_REF.fullmatch(value) is None
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
        or any(segment in {".", ".."} for segment in value.split("/"))
    ):
        raise TrainingGeometryError(f"{field} must be a path-free artifact ref")
    return value


def _unit(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise TrainingGeometryError(f"{field} must be finite")
    result = float(value)
    if result < 0.0 or result > 1.0:
        raise TrainingGeometryError(f"{field} must be inside [0, 1]")
    return round(result, 8)


def canonical_box(value: object, *, optional: bool = False) -> Optional[dict[str, float]]:
    """Validate a normalised ``x, y, width, height`` box."""

    if value is None and optional:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise TrainingGeometryError("normalized_box is invalid")
    box = {
        "x": _unit(value["x"], "normalized_box.x"),
        "y": _unit(value["y"], "normalized_box.y"),
        "width": _unit(value["width"], "normalized_box.width"),
        "height": _unit(value["height"], "normalized_box.height"),
    }
    if box["width"] <= 0.0 or box["height"] <= 0.0:
        raise TrainingGeometryError("normalized_box has no area")
    if box["x"] + box["width"] > 1.00000001:
        raise TrainingGeometryError("normalized_box exceeds image width")
    if box["y"] + box["height"] > 1.00000001:
        raise TrainingGeometryError("normalized_box exceeds image height")
    return box


def canonical_polygon(
    value: object,
    *,
    optional: bool = False,
) -> Optional[list[list[float]]]:
    """Validate a bounded normalised polygon."""

    if value is None and optional:
        return None
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not 3 <= len(value) <= _MAX_POLYGON_POINTS
    ):
        raise TrainingGeometryError("polygon must contain 3 to 256 points")
    points: list[list[float]] = []
    for index, point in enumerate(value):
        if (
            not isinstance(point, Sequence)
            or isinstance(point, (str, bytes, bytearray))
            or len(point) != 2
        ):
            raise TrainingGeometryError(f"polygon[{index}] is invalid")
        points.append(
            [
                _unit(point[0], f"polygon[{index}].x"),
                _unit(point[1], f"polygon[{index}].y"),
            ]
        )
    if len({(point[0], point[1]) for point in points}) < 3:
        raise TrainingGeometryError("polygon is degenerate")
    return points


def _track_ref(value: object, index: int) -> dict[str, Any]:
    legacy_fields = {
        "frame_id",
        "image_sha256",
        "timestamp_seconds",
        "normalized_box",
        "polygon",
        "mask_sha256",
    }
    bound_fields = {
        *legacy_fields,
        "binding_schema",
        "evidence_candidate_id",
        "image_artifact_ref",
    }
    if (
        not isinstance(value, Mapping)
        or frozenset(value) not in {
            frozenset(legacy_fields),
            frozenset(bound_fields),
        }
    ):
        raise TrainingGeometryError(f"tracked_frame_refs[{index}] is invalid")
    bound = "binding_schema" in value
    if bound and value["binding_schema"] != TRACKED_FRAME_BINDING_SCHEMA:
        raise TrainingGeometryError(
            f"tracked_frame_refs[{index}].binding_schema is invalid"
        )
    frame_id = value["frame_id"]
    if not isinstance(frame_id, str) or _FRAME_ID.fullmatch(frame_id) is None:
        raise TrainingGeometryError(
            f"tracked_frame_refs[{index}].frame_id is invalid"
        )
    timestamp = value["timestamp_seconds"]
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
        or float(timestamp) < 0.0
    ):
        raise TrainingGeometryError(
            f"tracked_frame_refs[{index}].timestamp_seconds is invalid"
        )
    box = canonical_box(value["normalized_box"], optional=True)
    polygon = canonical_polygon(value["polygon"], optional=True)
    mask_sha256 = _sha256(
        value["mask_sha256"],
        f"tracked_frame_refs[{index}].mask_sha256",
        optional=True,
    )
    if box is None and polygon is None:
        raise TrainingGeometryError(
            f"tracked_frame_refs[{index}] has no geometry"
        )
    if polygon is not None and mask_sha256 is None:
        raise TrainingGeometryError(
            f"tracked_frame_refs[{index}] polygon has no mask digest"
        )
    result = {
        "frame_id": frame_id,
        "image_sha256": _sha256(
            value["image_sha256"],
            f"tracked_frame_refs[{index}].image_sha256",
        ),
        "timestamp_seconds": round(float(timestamp), 6),
        "normalized_box": box,
        "polygon": polygon,
        "mask_sha256": mask_sha256,
    }
    if bound:
        evidence_candidate_id = value["evidence_candidate_id"]
        if (
            not isinstance(evidence_candidate_id, str)
            or _FRAME_ID.fullmatch(evidence_candidate_id) is None
        ):
            raise TrainingGeometryError(
                f"tracked_frame_refs[{index}].evidence_candidate_id is invalid"
            )
        result = {
            "binding_schema": TRACKED_FRAME_BINDING_SCHEMA,
            "evidence_candidate_id": evidence_candidate_id,
            "image_artifact_ref": _artifact_ref(
                value["image_artifact_ref"],
                f"tracked_frame_refs[{index}].image_artifact_ref",
            ),
            **result,
        }
    return result


def canonical_training_geometry(value: object) -> dict[str, Any]:
    """Return canonical approved geometry and verify its self-digest."""

    expected = {
        "schema_version",
        "source_kind",
        "approval_authority",
        "image_sha256",
        "image_artifact_ref",
        "normalized_box",
        "polygon",
        "mask_artifact_ref",
        "mask_sha256",
        "tracked_frame_refs",
        "proposal_sha256",
        "sam2_output_sha256",
        "sam2_model_manifest_sha256",
        "geometry_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise TrainingGeometryError("training geometry fields are invalid")
    if value["schema_version"] != SCHEMA_VERSION:
        raise TrainingGeometryError("training geometry schema is unsupported")
    source_kind = value["source_kind"]
    authority = value["approval_authority"]
    if source_kind not in SOURCE_KINDS:
        raise TrainingGeometryError("training geometry source_kind is invalid")
    if authority not in APPROVAL_AUTHORITIES:
        raise TrainingGeometryError(
            "training geometry approval_authority is invalid"
        )
    if (
        source_kind == "delegated_ai_proposal"
        and authority != "structured_llm_assisted"
    ) or (
        source_kind
        in {"accepted_ai_proposal", "user_sam2_refinement"}
        and authority != "local_user"
    ):
        raise TrainingGeometryError(
            "training geometry source and approval authority disagree"
        )

    box = canonical_box(value["normalized_box"], optional=True)
    polygon = canonical_polygon(value["polygon"], optional=True)
    mask_ref = _artifact_ref(
        value["mask_artifact_ref"],
        "mask_artifact_ref",
        optional=True,
    )
    mask_sha = _sha256(value["mask_sha256"], "mask_sha256", optional=True)
    if polygon is not None and (mask_ref is None or mask_sha is None):
        raise TrainingGeometryError(
            "a polygon requires a mask artifact ref and digest"
        )
    if polygon is None and (mask_ref is not None or mask_sha is not None):
        raise TrainingGeometryError(
            "mask provenance cannot exist without a polygon"
        )
    if source_kind == "negative_evidence":
        if any(item is not None for item in (box, polygon, mask_ref, mask_sha)):
            raise TrainingGeometryError(
                "negative evidence cannot carry positive geometry"
            )
    elif box is None:
        raise TrainingGeometryError(
            "positive training geometry requires a final box"
        )
    if source_kind == "user_sam2_refinement" and polygon is None:
        raise TrainingGeometryError(
            "a SAM2 refinement requires an approved polygon"
        )

    raw_track_refs = value["tracked_frame_refs"]
    if (
        not isinstance(raw_track_refs, Sequence)
        or isinstance(raw_track_refs, (str, bytes, bytearray))
        or len(raw_track_refs) > _MAX_TRACK_REFS
    ):
        raise TrainingGeometryError("tracked_frame_refs is invalid")
    track_refs = [
        _track_ref(item, index)
        for index, item in enumerate(raw_track_refs)
    ]
    timestamps = [item["timestamp_seconds"] for item in track_refs]
    if timestamps != sorted(timestamps):
        raise TrainingGeometryError(
            "tracked_frame_refs must be ordered by timestamp"
        )
    if len({item["frame_id"] for item in track_refs}) != len(track_refs):
        raise TrainingGeometryError("tracked_frame_refs contains duplicates")

    canonical = {
        "schema_version": SCHEMA_VERSION,
        "source_kind": source_kind,
        "approval_authority": authority,
        "image_sha256": _sha256(value["image_sha256"], "image_sha256"),
        "image_artifact_ref": _artifact_ref(
            value["image_artifact_ref"],
            "image_artifact_ref",
        ),
        "normalized_box": box,
        "polygon": polygon,
        "mask_artifact_ref": mask_ref,
        "mask_sha256": mask_sha,
        "tracked_frame_refs": track_refs,
        "proposal_sha256": _sha256(
            value["proposal_sha256"],
            "proposal_sha256",
            optional=True,
        ),
        "sam2_output_sha256": _sha256(
            value["sam2_output_sha256"],
            "sam2_output_sha256",
            optional=True,
        ),
        "sam2_model_manifest_sha256": _sha256(
            value["sam2_model_manifest_sha256"],
            "sam2_model_manifest_sha256",
            optional=True,
        ),
    }
    if source_kind in {"accepted_ai_proposal", "delegated_ai_proposal"}:
        if canonical["proposal_sha256"] is None:
            raise TrainingGeometryError(
                "accepted AI geometry requires proposal provenance"
            )
    if source_kind == "user_sam2_refinement":
        if (
            canonical["sam2_output_sha256"] is None
            or canonical["sam2_model_manifest_sha256"] is None
        ):
            raise TrainingGeometryError(
                "SAM2 geometry requires output and model manifest digests"
            )
    digest = hashlib.sha256(_canonical_bytes(canonical)).hexdigest()
    if value["geometry_sha256"] != digest:
        raise TrainingGeometryError("training geometry digest does not match")
    result = {**canonical, "geometry_sha256": digest}
    if len(_canonical_bytes(result)) > _MAX_CANONICAL_BYTES:
        raise TrainingGeometryError("training geometry is too large")
    return result


def seal_training_geometry(value: Mapping[str, Any]) -> dict[str, Any]:
    """Attach a digest to a server-built geometry object and validate it."""

    if not isinstance(value, Mapping):
        raise TrainingGeometryError("training geometry must be an object")
    raw = dict(value)
    raw.pop("geometry_sha256", None)
    raw["geometry_sha256"] = hashlib.sha256(
        _canonical_bytes(raw)
    ).hexdigest()
    return canonical_training_geometry(raw)


def training_geometry_from_review(
    *,
    candidate_metadata: Mapping[str, Any],
    answer: str,
    answered_by: str,
    final_box: Optional[Mapping[str, Any]] = None,
    sam2_refinement: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Build geometry only from server-resolved review evidence.

    ``None`` means the accepted label is still useful for classification or
    semantics but is not eligible for detector/segmenter ground truth.  The
    caller must not synthesize a box when the reviewed evidence has none.
    """

    if not isinstance(candidate_metadata, Mapping):
        raise TrainingGeometryError("candidate metadata must be an object")
    if answer not in {"yes", "no", "edit", "unsure", "skipped"}:
        raise TrainingGeometryError("answer is unsupported")
    if answered_by not in APPROVAL_AUTHORITIES:
        raise TrainingGeometryError("answer authority is unsupported")
    if answer in {"unsure", "skipped"}:
        return None

    image = candidate_metadata.get("representative_image")
    if not isinstance(image, Mapping):
        return None
    image_sha256 = image.get("sha256")
    image_artifact_ref = image.get("artifact_ref")
    if (
        not isinstance(image_sha256, str)
        or _SHA256.fullmatch(image_sha256) is None
        or not isinstance(image_artifact_ref, str)
    ):
        return None

    proposal_sha256 = candidate_metadata.get(
        "proposal_provenance_sha256"
    )
    if (
        not isinstance(proposal_sha256, str)
        or _SHA256.fullmatch(proposal_sha256) is None
    ):
        proposal_sha256 = None

    if answer == "no":
        return seal_training_geometry(
            {
                "schema_version": SCHEMA_VERSION,
                "source_kind": "negative_evidence",
                "approval_authority": (
                    "structured_llm_assisted"
                    if answered_by == "structured_llm_assisted"
                    else "local_user"
                ),
                "image_sha256": image_sha256,
                "image_artifact_ref": image_artifact_ref,
                "normalized_box": None,
                "polygon": None,
                "mask_artifact_ref": None,
                "mask_sha256": None,
                "tracked_frame_refs": [],
                "proposal_sha256": proposal_sha256,
                "sam2_output_sha256": None,
                "sam2_model_manifest_sha256": None,
            }
        )

    if sam2_refinement is not None:
        if not isinstance(sam2_refinement, Mapping):
            raise TrainingGeometryError("SAM2 refinement must be an object")
        model_manifest = sam2_refinement.get("model_manifest")
        if not isinstance(model_manifest, Mapping):
            raise TrainingGeometryError(
                "SAM2 refinement model manifest is missing"
            )
        box = canonical_box(
            sam2_refinement.get("normalized_box"),
            optional=False,
        )
        if final_box is not None and canonical_box(final_box) != box:
            raise TrainingGeometryError(
                "final box differs from the accepted SAM2 refinement"
            )
        return seal_training_geometry(
            {
                "schema_version": SCHEMA_VERSION,
                "source_kind": "user_sam2_refinement",
                "approval_authority": "local_user",
                "image_sha256": image_sha256,
                "image_artifact_ref": image_artifact_ref,
                "normalized_box": box,
                "polygon": sam2_refinement.get("polygon"),
                "mask_artifact_ref": sam2_refinement.get(
                    "mask_artifact_ref"
                ),
                # The SAM2 semantic output digest commits to the polygon,
                # final box, and any adapter track references.  The embedded
                # polygon remains directly rasterizable after restart.
                "mask_sha256": sam2_refinement.get("output_sha256"),
                "tracked_frame_refs": [],
                "proposal_sha256": proposal_sha256,
                "sam2_output_sha256": sam2_refinement.get(
                    "output_sha256"
                ),
                "sam2_model_manifest_sha256": model_manifest.get(
                    "manifest_sha256"
                ),
            }
        )

    box = canonical_box(
        final_box
        if final_box is not None
        else candidate_metadata.get("normalized_box"),
        optional=True,
    )
    if box is None or proposal_sha256 is None:
        return None
    source_kind = (
        "delegated_ai_proposal"
        if answered_by == "structured_llm_assisted"
        else "accepted_ai_proposal"
    )
    return seal_training_geometry(
        {
            "schema_version": SCHEMA_VERSION,
            "source_kind": source_kind,
            "approval_authority": answered_by,
            "image_sha256": image_sha256,
            "image_artifact_ref": image_artifact_ref,
            "normalized_box": box,
            "polygon": None,
            "mask_artifact_ref": None,
            "mask_sha256": None,
            "tracked_frame_refs": [],
            "proposal_sha256": proposal_sha256,
            "sam2_output_sha256": None,
            "sam2_model_manifest_sha256": None,
        }
    )


__all__ = [
    "APPROVAL_AUTHORITIES",
    "SCHEMA_VERSION",
    "TRACKED_FRAME_BINDING_SCHEMA",
    "SOURCE_KINDS",
    "TrainingGeometryError",
    "canonical_box",
    "canonical_polygon",
    "canonical_training_geometry",
    "seal_training_geometry",
    "training_geometry_from_review",
]
