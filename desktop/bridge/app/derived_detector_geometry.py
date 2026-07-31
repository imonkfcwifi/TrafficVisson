"""Path-free fit samples derived from one approved detector Annotation.

An Annotation remains the sole user/delegated approval and DatasetVersion
member.  This module may expand its accepted SAM2 track into additional image
geometry rows for detector fitting, but those rows are never labels, gate
credit, validation truth, Incidents, or report facts.

Every returned value is path-free and self-hashed.  Filesystem resolution is a
separate server-only boundary owned by the detector trainer.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from .training_geometry import (
    TRACKED_FRAME_BINDING_SCHEMA,
    TrainingGeometryError,
    canonical_box,
    canonical_polygon,
    canonical_training_geometry,
)


SCHEMA_VERSION = "trafficvisson.derived-detector-geometry-sample.v1"
QUALITY_POLICY = "sam2-temporal-fit-selection.v1"
MAX_TRACKED_FIT_SAMPLES_PER_EVENT = 16
MIN_TRACKED_FRAME_INTERVAL_SECONDS = 0.2
MIN_TRACKED_BOX_EDGE = 0.005
MIN_TRACKED_BOX_AREA = 0.0001
MAX_TRACKED_BOX_AREA = 0.95

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,191}$")
_ARTIFACT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,511}$")
_SOURCE_FRAME_KINDS = frozenset({"representative", "sam2_tracked_frame"})
_SPLITS = frozenset({"train", "validation"})
_PARTITIONS = frozenset({"approved", "negative"})
_MAX_CANONICAL_BYTES = 32 * 1024


class DerivedDetectorGeometryError(ValueError):
    """A derived fit sample is unsafe, inconsistent, or non-canonical."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(value: object, label: str, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DerivedDetectorGeometryError(f"{label} is not a SHA-256 digest")
    return value


def _opaque(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or _OPAQUE_ID.fullmatch(value) is None
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
        or any(segment in {".", ".."} for segment in value.split("/"))
    ):
        raise DerivedDetectorGeometryError(f"{label} is invalid")
    return value


def _artifact_ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or _ARTIFACT_REF.fullmatch(value) is None
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value) is not None
        or any(segment in {".", ".."} for segment in value.split("/"))
    ):
        raise DerivedDetectorGeometryError(
            "image_artifact_ref must be path-free"
        )
    return value


def _uuid(value: object, label: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise DerivedDetectorGeometryError(f"{label} must be a UUID") from error
    if str(parsed) != str(value).lower():
        raise DerivedDetectorGeometryError(f"{label} must be canonical")
    return str(parsed)


def _timestamp(value: object) -> Optional[float]:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise DerivedDetectorGeometryError("timestamp_seconds is invalid")
    return round(float(value), 6)


def _polygon_box(value: object) -> Optional[dict[str, float]]:
    polygon = canonical_polygon(value, optional=True)
    if polygon is None:
        return None
    minimum_x = min(point[0] for point in polygon)
    minimum_y = min(point[1] for point in polygon)
    maximum_x = max(point[0] for point in polygon)
    maximum_y = max(point[1] for point in polygon)
    return canonical_box(
        {
            "x": minimum_x,
            "y": minimum_y,
            "width": maximum_x - minimum_x,
            "height": maximum_y - minimum_y,
        }
    )


def _fit_quality_box(
    box_value: object,
    polygon_value: object,
) -> Optional[dict[str, float]]:
    box = canonical_box(box_value, optional=True)
    if box is None:
        box = _polygon_box(polygon_value)
    if box is None:
        return None
    area = box["width"] * box["height"]
    if (
        box["width"] < MIN_TRACKED_BOX_EDGE
        or box["height"] < MIN_TRACKED_BOX_EDGE
        or area < MIN_TRACKED_BOX_AREA
        or area > MAX_TRACKED_BOX_AREA
    ):
        return None
    return box


@dataclass(frozen=True)
class DerivedDetectorGeometrySample:
    """One immutable, path-free image/box row presented to the fit resolver."""

    schema_version: str
    sample_id: str
    annotation_id: str
    project_id: str
    analysis_job_id: str
    candidate_id: str
    evidence_candidate_id: str
    event_group_id: str
    split: str
    partition: str
    issue_type: Optional[str]
    source_frame_kind: str
    frame_id: str
    timestamp_seconds: Optional[float]
    image_artifact_ref: str
    image_sha256: str
    frame_sha256: str
    normalized_box: Optional[Mapping[str, float]]
    box_sha256: Optional[str]
    mask_sha256: Optional[str]
    source_geometry_sha256: str
    leakage_group_sha256: str
    quality_policy: str
    counts_toward_gate: bool
    fit_only: bool
    derived_geometry_sha256: str

    def canonical_value(self) -> dict[str, Any]:
        return canonical_derived_detector_geometry_sample(
            {
                "schema_version": self.schema_version,
                "sample_id": self.sample_id,
                "annotation_id": self.annotation_id,
                "project_id": self.project_id,
                "analysis_job_id": self.analysis_job_id,
                "candidate_id": self.candidate_id,
                "evidence_candidate_id": self.evidence_candidate_id,
                "event_group_id": self.event_group_id,
                "split": self.split,
                "partition": self.partition,
                "issue_type": self.issue_type,
                "source_frame_kind": self.source_frame_kind,
                "frame_id": self.frame_id,
                "timestamp_seconds": self.timestamp_seconds,
                "image_artifact_ref": self.image_artifact_ref,
                "image_sha256": self.image_sha256,
                "frame_sha256": self.frame_sha256,
                "normalized_box": (
                    dict(self.normalized_box)
                    if self.normalized_box is not None
                    else None
                ),
                "box_sha256": self.box_sha256,
                "mask_sha256": self.mask_sha256,
                "source_geometry_sha256": self.source_geometry_sha256,
                "leakage_group_sha256": self.leakage_group_sha256,
                "quality_policy": self.quality_policy,
                "counts_toward_gate": self.counts_toward_gate,
                "fit_only": self.fit_only,
                "derived_geometry_sha256": self.derived_geometry_sha256,
            }
        )


def canonical_derived_detector_geometry_sample(
    value: object,
) -> dict[str, Any]:
    expected = {
        "schema_version",
        "sample_id",
        "annotation_id",
        "project_id",
        "analysis_job_id",
        "candidate_id",
        "evidence_candidate_id",
        "event_group_id",
        "split",
        "partition",
        "issue_type",
        "source_frame_kind",
        "frame_id",
        "timestamp_seconds",
        "image_artifact_ref",
        "image_sha256",
        "frame_sha256",
        "normalized_box",
        "box_sha256",
        "mask_sha256",
        "source_geometry_sha256",
        "leakage_group_sha256",
        "quality_policy",
        "counts_toward_gate",
        "fit_only",
        "derived_geometry_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise DerivedDetectorGeometryError(
            "derived detector geometry fields are invalid"
        )
    if value["schema_version"] != SCHEMA_VERSION:
        raise DerivedDetectorGeometryError(
            "derived detector geometry schema is unsupported"
        )
    source_frame_kind = value["source_frame_kind"]
    split = value["split"]
    partition = value["partition"]
    if source_frame_kind not in _SOURCE_FRAME_KINDS:
        raise DerivedDetectorGeometryError("source_frame_kind is invalid")
    if split not in _SPLITS or partition not in _PARTITIONS:
        raise DerivedDetectorGeometryError("fit sample split/partition is invalid")
    if source_frame_kind == "sam2_tracked_frame" and split != "train":
        raise DerivedDetectorGeometryError(
            "SAM2 tracked frames are fit-only train samples"
        )
    issue_type = value["issue_type"]
    if issue_type is not None:
        issue_type = _opaque(issue_type, "issue_type")
    if partition == "approved" and issue_type is None:
        raise DerivedDetectorGeometryError(
            "approved detector geometry requires an issue type"
        )
    if partition == "negative" and issue_type is not None:
        raise DerivedDetectorGeometryError(
            "negative detector geometry cannot carry an issue type"
        )
    box = canonical_box(value["normalized_box"], optional=True)
    box_sha = _sha256(value["box_sha256"], "box_sha256", optional=True)
    if (box is None) != (box_sha is None):
        raise DerivedDetectorGeometryError(
            "box geometry and box digest must be present together"
        )
    if box is not None and _digest(box) != box_sha:
        raise DerivedDetectorGeometryError("box digest does not match")
    if partition == "approved" and box is None:
        raise DerivedDetectorGeometryError(
            "approved detector geometry requires a box"
        )
    if partition == "negative" and box is not None:
        raise DerivedDetectorGeometryError(
            "negative detector geometry cannot carry a box"
        )
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "sample_id": _opaque(value["sample_id"], "sample_id"),
        "annotation_id": _uuid(value["annotation_id"], "annotation_id"),
        "project_id": _uuid(value["project_id"], "project_id"),
        "analysis_job_id": _uuid(
            value["analysis_job_id"], "analysis_job_id"
        ),
        "candidate_id": _opaque(value["candidate_id"], "candidate_id"),
        "evidence_candidate_id": _opaque(
            value["evidence_candidate_id"], "evidence_candidate_id"
        ),
        "event_group_id": _opaque(
            value["event_group_id"], "event_group_id"
        ),
        "split": split,
        "partition": partition,
        "issue_type": issue_type,
        "source_frame_kind": source_frame_kind,
        "frame_id": _opaque(value["frame_id"], "frame_id"),
        "timestamp_seconds": _timestamp(value["timestamp_seconds"]),
        "image_artifact_ref": _artifact_ref(value["image_artifact_ref"]),
        "image_sha256": _sha256(value["image_sha256"], "image_sha256"),
        "frame_sha256": _sha256(value["frame_sha256"], "frame_sha256"),
        "normalized_box": box,
        "box_sha256": box_sha,
        "mask_sha256": _sha256(
            value["mask_sha256"], "mask_sha256", optional=True
        ),
        "source_geometry_sha256": _sha256(
            value["source_geometry_sha256"],
            "source_geometry_sha256",
        ),
        "leakage_group_sha256": _sha256(
            value["leakage_group_sha256"],
            "leakage_group_sha256",
        ),
        "quality_policy": value["quality_policy"],
    }
    if canonical["quality_policy"] != QUALITY_POLICY:
        raise DerivedDetectorGeometryError(
            "derived detector quality policy is unsupported"
        )
    if (
        value["counts_toward_gate"] is not False
        or value["fit_only"] is not True
    ):
        raise DerivedDetectorGeometryError(
            "derived detector geometry must remain fit-only without gate credit"
        )
    canonical["counts_toward_gate"] = False
    canonical["fit_only"] = True
    expected_event_group, expected_leakage_group = _event_identity(canonical)
    if (
        canonical["event_group_id"] != expected_event_group
        or canonical["leakage_group_sha256"] != expected_leakage_group
    ):
        raise DerivedDetectorGeometryError(
            "derived detector event/leakage binding does not match"
        )
    if source_frame_kind == "representative":
        expected_frame_id = (
            "frame_"
            + _digest(
                {
                    "candidate_id": canonical["candidate_id"],
                    "kind": "representative",
                }
            )[:24]
        )
        if (
            canonical["sample_id"] != canonical["annotation_id"]
            or canonical["frame_id"] != expected_frame_id
            or canonical["timestamp_seconds"] is not None
            or canonical["evidence_candidate_id"]
            != canonical["candidate_id"]
        ):
            raise DerivedDetectorGeometryError(
                "representative detector sample binding is invalid"
            )
        expected_frame = _digest(
            {
                "artifact_ref": canonical["image_artifact_ref"],
                "candidate_id": canonical["candidate_id"],
                "evidence_candidate_id": canonical[
                    "evidence_candidate_id"
                ],
                "event_group_id": canonical["event_group_id"],
                "image_sha256": canonical["image_sha256"],
                "kind": source_frame_kind,
            }
        )
    else:
        if (
            canonical["timestamp_seconds"] is None
            or canonical["sample_id"]
            != (
                f"{canonical['annotation_id']}:track:"
                f"{canonical['frame_sha256'][:16]}"
            )
        ):
            raise DerivedDetectorGeometryError(
                "tracked detector sample binding is invalid"
            )
        expected_frame = _digest(
            {
                "box_sha256": canonical["box_sha256"],
                "evidence_candidate_id": canonical[
                    "evidence_candidate_id"
                ],
                "event_group_id": canonical["event_group_id"],
                "frame_id": canonical["frame_id"],
                "image_artifact_ref": canonical[
                    "image_artifact_ref"
                ],
                "image_sha256": canonical["image_sha256"],
                "kind": source_frame_kind,
                "mask_sha256": canonical["mask_sha256"],
                "timestamp_seconds": canonical["timestamp_seconds"],
            }
        )
    if canonical["frame_sha256"] != expected_frame:
        raise DerivedDetectorGeometryError("frame digest does not match")
    digest = _digest(canonical)
    if value["derived_geometry_sha256"] != digest:
        raise DerivedDetectorGeometryError(
            "derived detector geometry digest does not match"
        )
    result = {**canonical, "derived_geometry_sha256": digest}
    if len(_canonical_bytes(result)) > _MAX_CANONICAL_BYTES:
        raise DerivedDetectorGeometryError(
            "derived detector geometry is too large"
        )
    return result


def _sample(value: Mapping[str, Any]) -> DerivedDetectorGeometrySample:
    raw = dict(value)
    raw["derived_geometry_sha256"] = _digest(raw)
    canonical = canonical_derived_detector_geometry_sample(raw)
    return DerivedDetectorGeometrySample(**canonical)


def _event_identity(annotation: Mapping[str, Any]) -> tuple[str, str]:
    event_group_id = (
        "event_"
        + _digest(
            {
                "analysis_job_id": annotation["analysis_job_id"],
                "candidate_id": annotation["candidate_id"],
                "project_id": annotation["project_id"],
                "schema_version": "trafficvisson.detector-event-group.v1",
            }
        )[:32]
    )
    leakage_group_sha256 = _digest(
        {
            "event_group_id": event_group_id,
            "schema_version": "trafficvisson.detector-leakage-group.v1",
        }
    )
    return event_group_id, leakage_group_sha256


def _bounded_track_rows(
    refs: Sequence[Mapping[str, Any]],
    *,
    representative_image_sha256: str,
) -> tuple[tuple[Mapping[str, Any], Mapping[str, float]], ...]:
    eligible = []
    seen_images = {representative_image_sha256}
    last_timestamp: Optional[float] = None
    for ref in refs:
        timestamp = float(ref["timestamp_seconds"])
        image_sha256 = str(ref["image_sha256"])
        box = _fit_quality_box(
            ref.get("normalized_box"),
            ref.get("polygon"),
        )
        if (
            box is None
            or ref.get("binding_schema") != TRACKED_FRAME_BINDING_SCHEMA
            or not isinstance(ref.get("evidence_candidate_id"), str)
            or not isinstance(ref.get("image_artifact_ref"), str)
            or image_sha256 in seen_images
            or (
                last_timestamp is not None
                and timestamp - last_timestamp
                < MIN_TRACKED_FRAME_INTERVAL_SECONDS
            )
        ):
            continue
        eligible.append((ref, box))
        seen_images.add(image_sha256)
        last_timestamp = timestamp
    if len(eligible) <= MAX_TRACKED_FIT_SAMPLES_PER_EVENT:
        return tuple(eligible)
    maximum = MAX_TRACKED_FIT_SAMPLES_PER_EVENT
    indexes = [
        round(index * (len(eligible) - 1) / (maximum - 1))
        for index in range(maximum)
    ]
    return tuple(eligible[index] for index in indexes)


def derive_detector_geometry_samples(
    annotation: Mapping[str, Any],
    *,
    include_tracked_fit_frames: bool,
) -> tuple[DerivedDetectorGeometrySample, ...]:
    """Expand one Annotation into representative plus bounded train-only rows."""

    if not isinstance(annotation, Mapping):
        raise DerivedDetectorGeometryError("annotation must be an object")
    if annotation.get("include_in_training") is not True:
        raise DerivedDetectorGeometryError(
            "excluded annotations cannot produce detector fit samples"
        )
    identity = {
        "annotation_id": _uuid(
            annotation.get("annotation_id"), "annotation_id"
        ),
        "project_id": _uuid(annotation.get("project_id"), "project_id"),
        "analysis_job_id": _uuid(
            annotation.get("analysis_job_id"), "analysis_job_id"
        ),
        "candidate_id": _opaque(
            annotation.get("candidate_id"), "candidate_id"
        ),
        "split": annotation.get("split"),
        "partition": annotation.get("partition"),
        "issue_type": annotation.get("selected_issue_type"),
    }
    if identity["split"] not in _SPLITS:
        raise DerivedDetectorGeometryError("annotation split is invalid")
    if identity["partition"] not in _PARTITIONS:
        raise DerivedDetectorGeometryError("annotation partition is invalid")
    try:
        geometry = canonical_training_geometry(
            annotation.get("training_geometry")
        )
    except TrainingGeometryError as error:
        raise DerivedDetectorGeometryError(
            "annotation training geometry is invalid"
        ) from error
    event_group_id, leakage_group_sha256 = _event_identity(identity)
    representative_box = geometry["normalized_box"]
    representative_box_sha = (
        _digest(representative_box)
        if representative_box is not None
        else None
    )
    representative_frame_id = (
        "frame_"
        + _digest(
            {
                "candidate_id": identity["candidate_id"],
                "kind": "representative",
            }
        )[:24]
    )
    representative_frame_sha = _digest(
        {
            "artifact_ref": geometry["image_artifact_ref"],
            "candidate_id": identity["candidate_id"],
            "evidence_candidate_id": identity["candidate_id"],
            "event_group_id": event_group_id,
            "image_sha256": geometry["image_sha256"],
            "kind": "representative",
        }
    )
    common = {
        "schema_version": SCHEMA_VERSION,
        **identity,
        "evidence_candidate_id": identity["candidate_id"],
        "event_group_id": event_group_id,
        "source_geometry_sha256": geometry["geometry_sha256"],
        "leakage_group_sha256": leakage_group_sha256,
        "quality_policy": QUALITY_POLICY,
        "counts_toward_gate": False,
        "fit_only": True,
    }
    samples = [
        _sample(
            {
                **common,
                "sample_id": identity["annotation_id"],
                "source_frame_kind": "representative",
                "frame_id": representative_frame_id,
                "timestamp_seconds": None,
                "image_artifact_ref": geometry["image_artifact_ref"],
                "image_sha256": geometry["image_sha256"],
                "frame_sha256": representative_frame_sha,
                "normalized_box": representative_box,
                "box_sha256": representative_box_sha,
                "mask_sha256": geometry["mask_sha256"],
            }
        )
    ]
    track_refs = geometry["tracked_frame_refs"]
    if not track_refs or not include_tracked_fit_frames:
        return tuple(samples)
    if identity["split"] != "train":
        return tuple(samples)
    if (
        identity["partition"] != "approved"
        or geometry["source_kind"] != "user_sam2_refinement"
    ):
        raise DerivedDetectorGeometryError(
            "only approved user-confirmed SAM2 tracks can expand detector fit"
        )
    for ref, box in _bounded_track_rows(
        track_refs,
        representative_image_sha256=geometry["image_sha256"],
    ):
        frame_id = str(ref["frame_id"])
        box_sha256 = _digest(box)
        frame_sha256 = _digest(
            {
                "box_sha256": box_sha256,
                "evidence_candidate_id": ref["evidence_candidate_id"],
                "event_group_id": event_group_id,
                "frame_id": frame_id,
                "image_artifact_ref": ref["image_artifact_ref"],
                "image_sha256": ref["image_sha256"],
                "kind": "sam2_tracked_frame",
                "mask_sha256": ref["mask_sha256"],
                "timestamp_seconds": ref["timestamp_seconds"],
            }
        )
        samples.append(
            _sample(
                {
                    **common,
                    "sample_id": (
                        f"{identity['annotation_id']}:track:"
                        f"{frame_sha256[:16]}"
                    ),
                    "source_frame_kind": "sam2_tracked_frame",
                    "frame_id": frame_id,
                    "timestamp_seconds": ref["timestamp_seconds"],
                    "image_artifact_ref": ref["image_artifact_ref"],
                    "image_sha256": ref["image_sha256"],
                    "frame_sha256": frame_sha256,
                    "normalized_box": box,
                    "box_sha256": box_sha256,
                    "mask_sha256": ref["mask_sha256"],
                    "evidence_candidate_id": ref[
                        "evidence_candidate_id"
                    ],
                }
            )
        )
    return tuple(samples)


__all__ = [
    "DerivedDetectorGeometryError",
    "DerivedDetectorGeometrySample",
    "MAX_TRACKED_FIT_SAMPLES_PER_EVENT",
    "QUALITY_POLICY",
    "SCHEMA_VERSION",
    "canonical_derived_detector_geometry_sample",
    "derive_detector_geometry_samples",
]
