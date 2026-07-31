"""Bounded SAM2 mask and tracking proposals for one PipelineJob stage.

The stage consumes already verified local frame paths and detector boxes.  It
never creates an Annotation, Incident, training member, or report finding.
Every output remains a review-only proposal until the candidate question is
answered by the user.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


PIPELINE_SAM2_SCHEMA = "trafficvisson.pipeline-sam2-review.v1"
MAX_CANDIDATES = 16
MAX_FRAMES_PER_CANDIDATE = 12
MAX_POLYGON_POINTS = 256

_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_EVIDENCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class PipelineSam2StageError(RuntimeError):
    """The local SAM2 stage failed without exposing a filesystem path."""


class PipelineSam2Adapter(Protocol):
    def status(self) -> Mapping[str, Any]: ...

    def refine(
        self,
        *,
        image_path: Path,
        point: tuple[float, float],
    ) -> Mapping[str, Any]: ...

    def track(
        self,
        *,
        frame_paths: Sequence[Path],
        prompt_frame_index: int,
        point: tuple[float, float],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class Sam2FrameEvidence:
    evidence_ref: str
    path: Path
    sha256: str
    timestamp_seconds: float


@dataclass(frozen=True)
class Sam2CandidatePrompt:
    candidate_id: str
    normalized_box: Mapping[str, float]
    frames: tuple[Sam2FrameEvidence, ...]
    prompt_frame_index: int


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _point_for_box(value: Mapping[str, float]) -> tuple[float, float]:
    if not isinstance(value, Mapping) or set(value) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise PipelineSam2StageError("sam2_prompt_box_invalid")
    normalized = {}
    for key in ("x", "y", "width", "height"):
        raw = value[key]
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
        ):
            raise PipelineSam2StageError("sam2_prompt_box_invalid")
        normalized[key] = float(raw)
    if (
        not 0 <= normalized["x"] <= 1
        or not 0 <= normalized["y"] <= 1
        or not 0 < normalized["width"] <= 1
        or not 0 < normalized["height"] <= 1
        or normalized["x"] + normalized["width"] > 1 + 1e-12
        or normalized["y"] + normalized["height"] > 1 + 1e-12
    ):
        raise PipelineSam2StageError("sam2_prompt_box_invalid")
    return (
        min(1.0, normalized["x"] + normalized["width"] / 2.0),
        min(1.0, normalized["y"] + normalized["height"] / 2.0),
    )


def _polygon(value: object) -> list[list[float]]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not 3 <= len(value) <= MAX_POLYGON_POINTS
    ):
        raise PipelineSam2StageError("sam2_polygon_invalid")
    result = []
    for raw_point in value:
        if (
            not isinstance(raw_point, Sequence)
            or isinstance(raw_point, (str, bytes, bytearray))
            or len(raw_point) != 2
        ):
            raise PipelineSam2StageError("sam2_polygon_invalid")
        point = []
        for raw in raw_point:
            if (
                isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or not math.isfinite(float(raw))
                or not 0 <= float(raw) <= 1
            ):
                raise PipelineSam2StageError("sam2_polygon_invalid")
            point.append(round(float(raw), 8))
        result.append(point)
    if len({tuple(point) for point in result}) < 3:
        raise PipelineSam2StageError("sam2_polygon_invalid")
    return result


def _box_for_polygon(
    polygon: Sequence[Sequence[float]],
) -> dict[str, float]:
    minimum_x = min(point[0] for point in polygon)
    minimum_y = min(point[1] for point in polygon)
    maximum_x = max(point[0] for point in polygon)
    maximum_y = max(point[1] for point in polygon)
    if maximum_x <= minimum_x or maximum_y <= minimum_y:
        raise PipelineSam2StageError("sam2_polygon_invalid")
    return {
        "x": round(minimum_x, 8),
        "y": round(minimum_y, 8),
        "width": round(maximum_x - minimum_x, 8),
        "height": round(maximum_y - minimum_y, 8),
    }


def _manifest_identity(status: Mapping[str, Any]) -> dict[str, str]:
    if (
        not isinstance(status, Mapping)
        or status.get("state") != "ready"
        or not isinstance(status.get("model_manifest"), Mapping)
    ):
        raise PipelineSam2StageError("sam2_model_unavailable")
    manifest = status["model_manifest"]
    identity = {
        "pack_id": manifest.get("pack_id"),
        "pack_version": manifest.get("pack_version"),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "model_artifact_sha256": manifest.get(
            "model_artifact_sha256"
        ),
    }
    if (
        not isinstance(identity["pack_id"], str)
        or not isinstance(identity["pack_version"], str)
        or not isinstance(identity["manifest_sha256"], str)
        or _SHA256.fullmatch(identity["manifest_sha256"]) is None
        or not isinstance(identity["model_artifact_sha256"], str)
        or _SHA256.fullmatch(
            identity["model_artifact_sha256"]
        )
        is None
        or manifest.get("provenance_verified") is not True
        or manifest.get("review_only") is not True
    ):
        raise PipelineSam2StageError("sam2_model_manifest_invalid")
    return identity


def _validate_prompt(
    prompt: Sam2CandidatePrompt,
) -> tuple[tuple[float, float], tuple[Sam2FrameEvidence, ...]]:
    if (
        not isinstance(prompt.candidate_id, str)
        or _CANDIDATE_ID.fullmatch(prompt.candidate_id) is None
        or not 1 <= len(prompt.frames) <= MAX_FRAMES_PER_CANDIDATE
        or isinstance(prompt.prompt_frame_index, bool)
        or not isinstance(prompt.prompt_frame_index, int)
        or not 0 <= prompt.prompt_frame_index < len(prompt.frames)
    ):
        raise PipelineSam2StageError("sam2_candidate_prompt_invalid")
    point = _point_for_box(prompt.normalized_box)
    refs = set()
    paths = set()
    for frame in prompt.frames:
        if (
            not isinstance(frame.evidence_ref, str)
            or _EVIDENCE_REF.fullmatch(frame.evidence_ref) is None
            or frame.evidence_ref in refs
            or not isinstance(frame.path, Path)
            or not frame.path.is_absolute()
            or frame.path in paths
            or not isinstance(frame.sha256, str)
            or _SHA256.fullmatch(frame.sha256) is None
            or isinstance(frame.timestamp_seconds, bool)
            or not isinstance(frame.timestamp_seconds, (int, float))
            or not math.isfinite(float(frame.timestamp_seconds))
            or float(frame.timestamp_seconds) < 0
        ):
            raise PipelineSam2StageError(
                "sam2_candidate_evidence_invalid"
            )
        refs.add(frame.evidence_ref)
        paths.add(frame.path)
    return point, prompt.frames


def run_pipeline_sam2_stage(
    *,
    adapter: PipelineSam2Adapter,
    prompts: Sequence[Sam2CandidatePrompt],
    cancel: Callable[[], bool],
) -> dict[str, Any]:
    """Run bounded mask/tracking proposals and return a durable checkpoint."""

    if (
        isinstance(prompts, (str, bytes, bytearray))
        or len(prompts) > MAX_CANDIDATES
        or not callable(cancel)
    ):
        raise PipelineSam2StageError("sam2_stage_input_invalid")
    manifest_before = _manifest_identity(adapter.status())
    results = []
    failures = []
    seen_candidates = set()
    for prompt in prompts:
        if cancel():
            return {
                "status": "cancelled",
                "error_code": "sam2_stage_cancelled",
            }
        point, frames = _validate_prompt(prompt)
        if prompt.candidate_id in seen_candidates:
            raise PipelineSam2StageError(
                "sam2_candidate_prompt_invalid"
            )
        seen_candidates.add(prompt.candidate_id)
        try:
            if len(frames) >= 2:
                tracked = adapter.track(
                    frame_paths=tuple(
                        frame.path for frame in frames
                    ),
                    prompt_frame_index=prompt.prompt_frame_index,
                    point=point,
                )
                tracked_rows = tracked.get("frames")
                if (
                    not isinstance(tracked_rows, Sequence)
                    or isinstance(
                        tracked_rows, (str, bytes, bytearray)
                    )
                    or not tracked_rows
                ):
                    raise PipelineSam2StageError(
                        "sam2_tracking_output_invalid"
                    )
                track_frames = []
                representative_polygon = None
                for raw in tracked_rows:
                    if not isinstance(raw, Mapping) or set(raw) != {
                        "frame_index",
                        "polygon",
                    }:
                        raise PipelineSam2StageError(
                            "sam2_tracking_output_invalid"
                        )
                    frame_index = raw["frame_index"]
                    if (
                        isinstance(frame_index, bool)
                        or not isinstance(frame_index, int)
                        or not 0 <= frame_index < len(frames)
                    ):
                        raise PipelineSam2StageError(
                            "sam2_tracking_output_invalid"
                        )
                    polygon = _polygon(raw["polygon"])
                    if frame_index == prompt.prompt_frame_index:
                        representative_polygon = polygon
                    track_frames.append(
                        {
                            "evidence_ref": frames[
                                frame_index
                            ].evidence_ref,
                            "timestamp_seconds": round(
                                float(
                                    frames[
                                        frame_index
                                    ].timestamp_seconds
                                ),
                                6,
                            ),
                            "polygon": polygon,
                        }
                    )
                if representative_polygon is None:
                    representative_polygon = _polygon(
                        track_frames[0]["polygon"]
                    )
                tracking_performed = len(track_frames) >= 2
            else:
                refined = adapter.refine(
                    image_path=frames[0].path,
                    point=point,
                )
                representative_polygon = _polygon(
                    refined.get("polygon")
                )
                track_frames = [
                    {
                        "evidence_ref": frames[0].evidence_ref,
                        "timestamp_seconds": round(
                            float(frames[0].timestamp_seconds), 6
                        ),
                        "polygon": representative_polygon,
                    }
                ]
                tracking_performed = False
            proposal_without_hash = {
                "candidate_id": prompt.candidate_id,
                "prompt": {
                    "kind": "detector_box_center_point",
                    "x": round(point[0], 8),
                    "y": round(point[1], 8),
                    "prompt_frame_ref": frames[
                        prompt.prompt_frame_index
                    ].evidence_ref,
                },
                "representative_polygon": representative_polygon,
                "normalized_box": _box_for_polygon(
                    representative_polygon
                ),
                "tracked_frames": track_frames,
                "tracking_performed": tracking_performed,
                "review_only": True,
                "requires_user_confirmation": True,
                "automatic_annotation_allowed": False,
                "automatic_incident_creation_allowed": False,
                "training_membership_allowed": False,
            }
            results.append(
                {
                    **proposal_without_hash,
                    "proposal_sha256": _sha(
                        proposal_without_hash
                    ),
                }
            )
        except PipelineSam2StageError:
            failures.append(
                {
                    "candidate_id": prompt.candidate_id,
                    "error_code": "sam2_output_invalid",
                }
            )
        except Exception:
            failures.append(
                {
                    "candidate_id": prompt.candidate_id,
                    "error_code": "sam2_inference_failed",
                }
            )
    manifest_after = _manifest_identity(adapter.status())
    if manifest_after != manifest_before:
        raise PipelineSam2StageError("sam2_model_changed")
    checkpoint_without_hash = {
        "schema_version": PIPELINE_SAM2_SCHEMA,
        "state": (
            "completed"
            if results and not failures
            else "partial"
            if results
            else "failed"
            if failures
            else "zero_prompts"
        ),
        "model_manifest": manifest_before,
        "proposals": results,
        "failures": failures,
        "mask_generated": bool(results),
        "tracking_performed": any(
            result["tracking_performed"] for result in results
        ),
        "review_only": True,
        "requires_user_confirmation": True,
        "automatic_annotation_allowed": False,
        "automatic_incident_creation_allowed": False,
        "training_membership_allowed": False,
        "annotation_count": 0,
        "incident_count": 0,
    }
    return {
        "status": "success" if results or not failures else "failed",
        "error_code": (
            "sam2_partial_failure"
            if results and failures
            else "sam2_inference_failed"
            if failures
            else "sam2_no_prompt_boxes"
            if not results
            else None
        ),
        "checkpoint": {
            **checkpoint_without_hash,
            "sam2_revision": _sha(checkpoint_without_hash),
        },
        "metrics": {
            "prompt_count": len(prompts),
            "mask_count": len(results),
            "tracked_candidate_count": sum(
                result["tracking_performed"]
                for result in results
            ),
            "tracked_frame_count": sum(
                len(result["tracked_frames"])
                for result in results
            ),
            "failure_count": len(failures),
            "annotation_count": 0,
            "incident_count": 0,
        },
    }


__all__ = [
    "MAX_CANDIDATES",
    "MAX_FRAMES_PER_CANDIDATE",
    "PIPELINE_SAM2_SCHEMA",
    "PipelineSam2Adapter",
    "PipelineSam2StageError",
    "Sam2CandidatePrompt",
    "Sam2FrameEvidence",
    "run_pipeline_sam2_stage",
]
