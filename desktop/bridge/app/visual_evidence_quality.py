"""Deterministic, review-only quality gate for candidate-window evidence.

The gate deliberately answers only whether a bounded set of frames contains
enough local visual information to ask a road-maintenance question.  It never
classifies a road issue and never creates Annotation, Incident, training, or
report truth.

The policy is intentionally conservative:

* a single dark, blurred, night, or tunnel frame is never enough to reject a
  window;
* at least three frames and an 80% persistent strong-unusable ratio are
  required;
* blur alone is not a rejection reason;
* a usable frame is deterministically re-selected from the whole candidate
  window instead of trusting the original representative frame.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, Tuple

from .candidate_window_rescan import (
    CandidateWindowFrame,
    CandidateWindowRescanError,
    CandidateWindowRescanResult,
    resolve_candidate_window_frame,
)


VISUAL_EVIDENCE_QUALITY_SCHEMA = (
    "trafficvisson.visual-evidence-quality.v2"
)


class VisualEvidenceQualityError(RuntimeError):
    """Raised when local evidence cannot be measured with integrity."""


class LocalRoadSceneObserver(Protocol):
    """Verified local semantic support for road-view observability only."""

    def observe_road_scene(
        self,
        body: bytes,
        media_type: str,
        expected_sha256: str,
    ) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class VisualEvidenceQualityPolicy:
    maximum_sampled_frames: int = 512
    maximum_sampled_frames_per_window: int = 7
    maximum_selected_frames_per_window: int = 3
    minimum_frames_for_rejection: int = 3
    persistent_unusable_ratio: float = 0.80
    analysis_width: int = 320
    featureless_entropy_max: float = 3.20
    featureless_luma_stddev_max: float = 10.0
    featureless_edge_mean_max: float = 0.90
    clipped_fraction_min: float = 0.985
    clipped_entropy_max: float = 2.50
    obstruction_edge_mean_max: float = 2.20
    obstruction_channel_spread_min: float = 45.0
    obstruction_entropy_max: float = 7.00
    semantic_non_road_probability_min: float = 0.80
    semantic_non_road_margin_min: float = 0.35
    semantic_road_probability_min: float = 0.55
    persistent_non_road_ratio: float = 0.80

    def __post_init__(self) -> None:
        integer_ranges = (
            ("maximum_sampled_frames", self.maximum_sampled_frames, 3, 4096),
            (
                "maximum_sampled_frames_per_window",
                self.maximum_sampled_frames_per_window,
                3,
                15,
            ),
            (
                "maximum_selected_frames_per_window",
                self.maximum_selected_frames_per_window,
                1,
                7,
            ),
            (
                "minimum_frames_for_rejection",
                self.minimum_frames_for_rejection,
                3,
                15,
            ),
            ("analysis_width", self.analysis_width, 64, 1024),
        )
        for name, value, minimum, maximum in integer_ranges:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        if (
            self.maximum_selected_frames_per_window
            > self.maximum_sampled_frames_per_window
        ):
            raise ValueError(
                "selected-frame limit exceeds the per-window sample limit"
            )
        numeric_ranges = (
            ("persistent_unusable_ratio", self.persistent_unusable_ratio, 0.5, 1.0),
            ("featureless_entropy_max", self.featureless_entropy_max, 0.0, 8.0),
            (
                "featureless_luma_stddev_max",
                self.featureless_luma_stddev_max,
                0.0,
                128.0,
            ),
            (
                "featureless_edge_mean_max",
                self.featureless_edge_mean_max,
                0.0,
                64.0,
            ),
            ("clipped_fraction_min", self.clipped_fraction_min, 0.5, 1.0),
            ("clipped_entropy_max", self.clipped_entropy_max, 0.0, 8.0),
            (
                "obstruction_edge_mean_max",
                self.obstruction_edge_mean_max,
                0.0,
                64.0,
            ),
            (
                "obstruction_channel_spread_min",
                self.obstruction_channel_spread_min,
                0.0,
                255.0,
            ),
            (
                "obstruction_entropy_max",
                self.obstruction_entropy_max,
                0.0,
                8.0,
            ),
            (
                "semantic_non_road_probability_min",
                self.semantic_non_road_probability_min,
                0.5,
                1.0,
            ),
            (
                "semantic_non_road_margin_min",
                self.semantic_non_road_margin_min,
                0.0,
                1.0,
            ),
            (
                "semantic_road_probability_min",
                self.semantic_road_probability_min,
                0.5,
                1.0,
            ),
            (
                "persistent_non_road_ratio",
                self.persistent_non_road_ratio,
                0.5,
                1.0,
            ),
        )
        for name, value, minimum, maximum in numeric_ranges:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")


@dataclass(frozen=True)
class FrameQualityObservation:
    evidence_id: str
    window_id: str
    frame_index: int
    timestamp_seconds: float
    image_sha256: str
    luma_mean: float
    luma_stddev: float
    entropy: float
    edge_mean: float
    channel_mean_spread: float
    dark_clipped_fraction: float
    bright_clipped_fraction: float
    road_scene_semantic_status: str
    road_scene_probability: float | None
    non_road_view_probability: float | None
    semantic_non_road_margin: float | None
    semantic_model_identity_sha256: str | None
    semantic_prompt_set_sha256: str | None
    strong_non_road_view: bool
    road_scene_supported: bool
    strong_unusable: bool
    reason_codes: Tuple[str, ...]
    quality_score: float


@dataclass(frozen=True)
class WindowQualityDecision:
    window_id: str
    source_candidate_ids: Tuple[str, ...]
    sampled_frame_count: int
    strong_unusable_frame_count: int
    strong_unusable_ratio: float
    semantic_observed_frame_count: int
    strong_non_road_frame_count: int
    strong_non_road_ratio: float
    road_scene_supported_frame_count: int
    state: str
    representative_evidence_id: str | None
    selected_evidence_ids: Tuple[str, ...]
    reason_codes: Tuple[str, ...]


@dataclass(frozen=True)
class VisualEvidenceQualityResult:
    schema_version: str
    state: str
    source_sha256: str
    rescan_manifest_sha256: str
    policy: Mapping[str, Any]
    frame_observations: Tuple[FrameQualityObservation, ...]
    window_decisions: Tuple[WindowQualityDecision, ...]
    selected_evidence_ids: Tuple[str, ...]
    eligible_parent_candidate_ids: Tuple[str, ...]
    blocked_parent_candidate_ids: Tuple[str, ...]
    road_scene_semantic_status: str
    semantic_model_identity_sha256: str | None
    semantic_prompt_set_sha256: str | None
    road_scene_supported_window_ids: Tuple[str, ...]
    persistent_non_road_window_ids: Tuple[str, ...]
    quality_revision: str

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "source_sha256": self.source_sha256,
            "rescan_manifest_sha256": self.rescan_manifest_sha256,
            "policy": dict(self.policy),
            "evaluated_frame_count": len(self.frame_observations),
            "window_count": len(self.window_decisions),
            "usable_window_count": sum(
                item.state != "unusable_visual_evidence"
                for item in self.window_decisions
            ),
            "unusable_window_count": sum(
                item.state == "unusable_visual_evidence"
                for item in self.window_decisions
            ),
            "frame_observations": [
                asdict(item) for item in self.frame_observations
            ],
            "window_decisions": [
                asdict(item) for item in self.window_decisions
            ],
            "selected_evidence_ids": list(self.selected_evidence_ids),
            "eligible_parent_candidate_ids": list(
                self.eligible_parent_candidate_ids
            ),
            "blocked_parent_candidate_ids": list(
                self.blocked_parent_candidate_ids
            ),
            "road_scene_semantic_status": (
                self.road_scene_semantic_status
            ),
            "semantic_model_identity_sha256": (
                self.semantic_model_identity_sha256
            ),
            "semantic_prompt_set_sha256": (
                self.semantic_prompt_set_sha256
            ),
            "road_scene_supported_window_ids": list(
                self.road_scene_supported_window_ids
            ),
            "persistent_non_road_window_ids": list(
                self.persistent_non_road_window_ids
            ),
            "quality_revision": self.quality_revision,
            "quality_role": (
                "local_multi_window_evidence_usability_only"
            ),
            "review_only": True,
            "automatic_annotation_creation": False,
            "automatic_incident_creation": False,
            "automatic_training_membership": False,
            "automatic_report_truth": False,
        }


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _evenly_spaced(rows: Sequence[CandidateWindowFrame], maximum: int):
    if len(rows) <= maximum:
        return list(rows)
    indexes = {
        min(
            len(rows) - 1,
            round(slot * (len(rows) - 1) / (maximum - 1)),
        )
        for slot in range(maximum)
    }
    return [rows[index] for index in sorted(indexes)]


def _sample_frames(
    result: CandidateWindowRescanResult,
    policy: VisualEvidenceQualityPolicy,
) -> list[CandidateWindowFrame]:
    by_window: dict[str, list[CandidateWindowFrame]] = {}
    for frame in result.frames:
        by_window.setdefault(frame.window_id, []).append(frame)
    per_window = {
        window.window_id: _evenly_spaced(
            sorted(
                by_window.get(window.window_id, ()),
                key=lambda item: item.frame_index,
            ),
            policy.maximum_sampled_frames_per_window,
        )
        for window in result.windows
    }
    if any(not rows for rows in per_window.values()):
        raise VisualEvidenceQualityError(
            "candidate window has no quality evidence"
        )
    selected: list[CandidateWindowFrame] = []
    # Round-robin preserves at least one frame per window before adding
    # temporal support, so large jobs cannot starve late windows.
    for offset in range(policy.maximum_sampled_frames_per_window):
        for window in result.windows:
            rows = per_window[window.window_id]
            if offset < len(rows):
                selected.append(rows[offset])
                if len(selected) == policy.maximum_sampled_frames:
                    return sorted(
                        selected,
                        key=lambda item: (
                            item.timestamp_seconds,
                            item.window_id,
                            item.frame_index,
                        ),
                    )
    return sorted(
        selected,
        key=lambda item: (
            item.timestamp_seconds,
            item.window_id,
            item.frame_index,
        ),
    )


def _measure_frame(
    result: CandidateWindowRescanResult,
    frame: CandidateWindowFrame,
    policy: VisualEvidenceQualityPolicy,
    semantic_observer: LocalRoadSceneObserver | None,
) -> FrameQualityObservation:
    try:
        from PIL import Image, ImageFilter, ImageStat, UnidentifiedImageError
    except ImportError as error:
        raise VisualEvidenceQualityError(
            "Pillow is unavailable for local visual quality analysis"
        ) from error
    try:
        resolved = resolve_candidate_window_frame(
            result, frame.evidence_id
        )
        semantic_observation: Mapping[str, Any] | None = None
        if semantic_observer is not None:
            body = resolved.path.read_bytes()
            if hashlib.sha256(body).hexdigest() != frame.sha256:
                raise VisualEvidenceQualityError(
                    "candidate semantic evidence hash changed"
                )
            try:
                semantic_observation = (
                    semantic_observer.observe_road_scene(
                        body,
                        resolved.media_type,
                        frame.sha256,
                    )
                )
            except VisualEvidenceQualityError:
                raise
            except Exception as error:
                raise VisualEvidenceQualityError(
                    "local road-scene semantic observation failed"
                ) from error
        with Image.open(resolved.path) as opened:
            if opened.format != "JPEG":
                raise VisualEvidenceQualityError(
                    "candidate evidence is not a JPEG"
                )
            rgb = opened.convert("RGB")
            if rgb.width > policy.analysis_width:
                height = max(
                    1,
                    round(rgb.height * policy.analysis_width / rgb.width),
                )
                rgb = rgb.resize(
                    (policy.analysis_width, height),
                    Image.Resampling.BILINEAR,
                )
            gray = rgb.convert("L")
            gray_stat = ImageStat.Stat(gray)
            rgb_stat = ImageStat.Stat(rgb)
            histogram = gray.histogram()
            pixel_count = sum(histogram)
            if pixel_count <= 0:
                raise VisualEvidenceQualityError(
                    "candidate evidence has no pixels"
                )
            edge = gray.filter(ImageFilter.FIND_EDGES)
            if edge.width > 4 and edge.height > 4:
                edge = edge.crop(
                    (2, 2, edge.width - 2, edge.height - 2)
                )
            edge_mean = float(ImageStat.Stat(edge).mean[0])
            entropy = float(gray.entropy())
            luma_mean = float(gray_stat.mean[0])
            luma_stddev = float(gray_stat.stddev[0])
            channel_spread = float(
                max(rgb_stat.mean) - min(rgb_stat.mean)
            )
            dark_fraction = float(sum(histogram[:12]) / pixel_count)
            bright_fraction = float(sum(histogram[244:]) / pixel_count)
    except VisualEvidenceQualityError:
        raise
    except (
        CandidateWindowRescanError,
        OSError,
        UnidentifiedImageError,
        ValueError,
    ) as error:
        raise VisualEvidenceQualityError(
            "candidate evidence quality measurement failed"
        ) from error

    reasons: list[str] = []
    if (
        entropy <= policy.featureless_entropy_max
        and luma_stddev <= policy.featureless_luma_stddev_max
        and edge_mean <= policy.featureless_edge_mean_max
    ):
        reasons.append("featureless_frame")
    if (
        max(dark_fraction, bright_fraction)
        >= policy.clipped_fraction_min
        and entropy <= policy.clipped_entropy_max
    ):
        reasons.append("near_fully_clipped_frame")
    if (
        edge_mean <= policy.obstruction_edge_mean_max
        and channel_spread >= policy.obstruction_channel_spread_min
        and entropy <= policy.obstruction_entropy_max
    ):
        reasons.append("low_edge_tinted_obstruction_pattern")

    semantic_status = "not_configured_degraded"
    road_scene_probability: float | None = None
    non_road_probability: float | None = None
    semantic_margin: float | None = None
    semantic_model_identity_sha256: str | None = None
    semantic_prompt_set_sha256: str | None = None
    if semantic_observation is not None:
        semantic_status = semantic_observation.get("status")
        road_scene_probability = semantic_observation.get(
            "road_scene_probability"
        )
        non_road_probability = semantic_observation.get(
            "non_road_view_probability"
        )
        semantic_model_identity_sha256 = semantic_observation.get(
            "model_identity_sha256"
        )
        semantic_prompt_set_sha256 = semantic_observation.get(
            "prompt_set_sha256"
        )
        if (
            semantic_status != "completed"
            or isinstance(road_scene_probability, bool)
            or not isinstance(road_scene_probability, (int, float))
            or not math.isfinite(float(road_scene_probability))
            or not 0.0 <= float(road_scene_probability) <= 1.0
            or isinstance(non_road_probability, bool)
            or not isinstance(non_road_probability, (int, float))
            or not math.isfinite(float(non_road_probability))
            or not 0.0 <= float(non_road_probability) <= 1.0
            or abs(
                float(road_scene_probability)
                + float(non_road_probability)
                - 1.0
            )
            > 0.0001
            or not isinstance(
                semantic_model_identity_sha256, str
            )
            or len(semantic_model_identity_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in semantic_model_identity_sha256
            )
            or not isinstance(semantic_prompt_set_sha256, str)
            or len(semantic_prompt_set_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in semantic_prompt_set_sha256
            )
            or semantic_observation.get("image_sha256")
            != frame.sha256
            or semantic_observation.get("local_only") is not True
            or semantic_observation.get("review_only") is not True
        ):
            raise VisualEvidenceQualityError(
                "local road-scene semantic observation is invalid"
            )
        road_scene_probability = float(road_scene_probability)
        non_road_probability = float(non_road_probability)
        semantic_margin = (
            non_road_probability - road_scene_probability
        )
    strong_non_road_view = bool(
        semantic_status == "completed"
        and non_road_probability is not None
        and semantic_margin is not None
        and non_road_probability
        >= policy.semantic_non_road_probability_min
        and semantic_margin >= policy.semantic_non_road_margin_min
    )
    road_scene_supported = bool(
        semantic_status == "completed"
        and road_scene_probability is not None
        and road_scene_probability
        >= policy.semantic_road_probability_min
    )
    if strong_non_road_view:
        reasons.append("semantic_non_road_camera_view")
    strong_unusable = bool(reasons)
    quality_score = (
        entropy * 10.0
        + min(edge_mean, 24.0) * 3.0
        + min(luma_stddev, 96.0) * 0.20
        - max(dark_fraction, bright_fraction) * 30.0
        - (18.0 if strong_unusable else 0.0)
    )
    return FrameQualityObservation(
        evidence_id=frame.evidence_id,
        window_id=frame.window_id,
        frame_index=frame.frame_index,
        timestamp_seconds=round(frame.timestamp_seconds, 6),
        image_sha256=frame.sha256,
        luma_mean=round(luma_mean, 6),
        luma_stddev=round(luma_stddev, 6),
        entropy=round(entropy, 6),
        edge_mean=round(edge_mean, 6),
        channel_mean_spread=round(channel_spread, 6),
        dark_clipped_fraction=round(dark_fraction, 8),
        bright_clipped_fraction=round(bright_fraction, 8),
        road_scene_semantic_status=semantic_status,
        road_scene_probability=(
            round(road_scene_probability, 8)
            if road_scene_probability is not None
            else None
        ),
        non_road_view_probability=(
            round(non_road_probability, 8)
            if non_road_probability is not None
            else None
        ),
        semantic_non_road_margin=(
            round(semantic_margin, 8)
            if semantic_margin is not None
            else None
        ),
        semantic_model_identity_sha256=(
            semantic_model_identity_sha256
        ),
        semantic_prompt_set_sha256=semantic_prompt_set_sha256,
        strong_non_road_view=strong_non_road_view,
        road_scene_supported=road_scene_supported,
        strong_unusable=strong_unusable,
        reason_codes=tuple(reasons),
        quality_score=round(quality_score, 6),
    )


def evaluate_candidate_window_visual_quality(
    result: CandidateWindowRescanResult,
    *,
    source_sha256: str,
    policy: VisualEvidenceQualityPolicy = VisualEvidenceQualityPolicy(),
    semantic_observer: LocalRoadSceneObserver | None = None,
) -> VisualEvidenceQualityResult:
    """Measure, persistably summarize, and gate candidate-window frames."""

    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(character not in "0123456789abcdef" for character in source_sha256)
        or not result.frames
        or not result.windows
    ):
        raise VisualEvidenceQualityError(
            "candidate visual quality scope is invalid"
        )
    sampled_frames = _sample_frames(result, policy)
    observations = tuple(
        _measure_frame(
            result,
            frame,
            policy,
            semantic_observer,
        )
        for frame in sampled_frames
    )
    semantic_statuses = {
        item.road_scene_semantic_status for item in observations
    }
    if semantic_observer is None:
        semantic_status = "not_configured_degraded"
    elif semantic_statuses == {"completed"}:
        semantic_status = "completed"
    else:
        raise VisualEvidenceQualityError(
            "local road-scene semantic coverage is incomplete"
        )
    semantic_model_ids = {
        item.semantic_model_identity_sha256
        for item in observations
        if item.semantic_model_identity_sha256 is not None
    }
    semantic_prompt_ids = {
        item.semantic_prompt_set_sha256
        for item in observations
        if item.semantic_prompt_set_sha256 is not None
    }
    if (
        len(semantic_model_ids) > 1
        or len(semantic_prompt_ids) > 1
        or (
            semantic_status == "completed"
            and (
                len(semantic_model_ids) != 1
                or len(semantic_prompt_ids) != 1
            )
        )
    ):
        raise VisualEvidenceQualityError(
            "local road-scene semantic model changed during evaluation"
        )
    observations_by_window: dict[
        str, list[FrameQualityObservation]
    ] = {}
    for observation in observations:
        observations_by_window.setdefault(
            observation.window_id, []
        ).append(observation)

    decisions: list[WindowQualityDecision] = []
    eligible_parents: set[str] = set()
    blocked_parents: set[str] = set()
    selected_evidence_ids: list[str] = []
    road_supported_windows: list[str] = []
    persistent_non_road_windows: list[str] = []
    for window in result.windows:
        rows = observations_by_window.get(window.window_id, [])
        if not rows:
            raise VisualEvidenceQualityError(
                "candidate window quality evidence is missing"
            )
        strong_count = sum(item.strong_unusable for item in rows)
        ratio = strong_count / len(rows)
        strong_pixel_count = sum(
            any(
                reason != "semantic_non_road_camera_view"
                for reason in item.reason_codes
            )
            for item in rows
        )
        strong_pixel_ratio = strong_pixel_count / len(rows)
        semantic_rows = [
            item
            for item in rows
            if item.road_scene_semantic_status == "completed"
        ]
        strong_non_road_count = sum(
            item.strong_non_road_view for item in semantic_rows
        )
        strong_non_road_ratio = (
            strong_non_road_count / len(semantic_rows)
            if semantic_rows
            else 0.0
        )
        road_scene_supported_count = sum(
            item.road_scene_supported for item in semantic_rows
        )
        pixel_quality_rejected = (
            len(rows) >= policy.minimum_frames_for_rejection
            and strong_pixel_count
            >= policy.minimum_frames_for_rejection
            and strong_pixel_ratio
            >= policy.persistent_unusable_ratio
        )
        semantic_non_road_rejected = (
            len(semantic_rows)
            >= policy.minimum_frames_for_rejection
            and strong_non_road_count
            >= policy.minimum_frames_for_rejection
            and strong_non_road_ratio
            >= policy.persistent_non_road_ratio
            and road_scene_supported_count == 0
        )
        rejected = (
            pixel_quality_rejected or semantic_non_road_rejected
        )
        if road_scene_supported_count:
            road_supported_windows.append(window.window_id)
        if semantic_non_road_rejected:
            persistent_non_road_windows.append(window.window_id)
        reason_codes = tuple(
            sorted(
                {
                    reason
                    for item in rows
                    for reason in item.reason_codes
                }
            )
        )
        if rejected:
            state = "unusable_visual_evidence"
            representative = None
            selected: tuple[str, ...] = ()
            blocked_parents.update(window.source_candidate_ids)
        else:
            usable_rows = [
                item for item in rows if not item.strong_unusable
            ]
            if usable_rows:
                state = (
                    "usable"
                    if strong_count == 0
                    else "usable_representative_reselected"
                )
            else:
                # Fewer than three frames is insufficient evidence to reject
                # a window. Keep one review-only frame to preserve recall.
                state = "indeterminate_allowed_for_recall"
                usable_rows = list(rows)
            ranked = sorted(
                usable_rows,
                key=lambda item: (
                    -item.quality_score,
                    item.timestamp_seconds,
                    item.evidence_id,
                ),
            )
            representative = ranked[0].evidence_id
            selected_rows = ranked[
                : policy.maximum_selected_frames_per_window
            ]
            selected = tuple(
                item.evidence_id
                for item in sorted(
                    selected_rows,
                    key=lambda item: (
                        item.timestamp_seconds,
                        item.evidence_id,
                    ),
                )
            )
            selected_evidence_ids.extend(selected)
            eligible_parents.update(window.source_candidate_ids)
        decisions.append(
            WindowQualityDecision(
                window_id=window.window_id,
                source_candidate_ids=window.source_candidate_ids,
                sampled_frame_count=len(rows),
                strong_unusable_frame_count=strong_count,
                strong_unusable_ratio=round(ratio, 8),
                semantic_observed_frame_count=len(semantic_rows),
                strong_non_road_frame_count=(
                    strong_non_road_count
                ),
                strong_non_road_ratio=round(
                    strong_non_road_ratio, 8
                ),
                road_scene_supported_frame_count=(
                    road_scene_supported_count
                ),
                state=state,
                representative_evidence_id=representative,
                selected_evidence_ids=selected,
                reason_codes=reason_codes,
            )
        )

    blocked_parents.difference_update(eligible_parents)
    if not selected_evidence_ids:
        state = "unusable_visual_evidence"
    elif blocked_parents:
        state = "partial"
    else:
        state = "usable"
    policy_payload = asdict(policy)
    revision_payload = {
        "schema_version": VISUAL_EVIDENCE_QUALITY_SCHEMA,
        "state": state,
        "source_sha256": source_sha256,
        "rescan_manifest_sha256": result.manifest_sha256,
        "policy": policy_payload,
        "frame_observations": [asdict(item) for item in observations],
        "window_decisions": [asdict(item) for item in decisions],
        "selected_evidence_ids": selected_evidence_ids,
        "eligible_parent_candidate_ids": sorted(eligible_parents),
        "blocked_parent_candidate_ids": sorted(blocked_parents),
        "road_scene_semantic_status": semantic_status,
        "semantic_model_identity_sha256": (
            next(iter(semantic_model_ids))
            if semantic_model_ids
            else None
        ),
        "semantic_prompt_set_sha256": (
            next(iter(semantic_prompt_ids))
            if semantic_prompt_ids
            else None
        ),
        "road_scene_supported_window_ids": sorted(
            road_supported_windows
        ),
        "persistent_non_road_window_ids": sorted(
            persistent_non_road_windows
        ),
        "quality_role": (
            "local_multi_window_evidence_usability_only"
        ),
        "review_only": True,
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_membership": False,
        "automatic_report_truth": False,
    }
    quality_revision = hashlib.sha256(
        _canonical_json(revision_payload)
    ).hexdigest()
    return VisualEvidenceQualityResult(
        schema_version=VISUAL_EVIDENCE_QUALITY_SCHEMA,
        state=state,
        source_sha256=source_sha256,
        rescan_manifest_sha256=result.manifest_sha256,
        policy=policy_payload,
        frame_observations=observations,
        window_decisions=tuple(decisions),
        selected_evidence_ids=tuple(selected_evidence_ids),
        eligible_parent_candidate_ids=tuple(sorted(eligible_parents)),
        blocked_parent_candidate_ids=tuple(sorted(blocked_parents)),
        road_scene_semantic_status=semantic_status,
        semantic_model_identity_sha256=(
            next(iter(semantic_model_ids))
            if semantic_model_ids
            else None
        ),
        semantic_prompt_set_sha256=(
            next(iter(semantic_prompt_ids))
            if semantic_prompt_ids
            else None
        ),
        road_scene_supported_window_ids=tuple(
            sorted(road_supported_windows)
        ),
        persistent_non_road_window_ids=tuple(
            sorted(persistent_non_road_windows)
        ),
        quality_revision=quality_revision,
    )


__all__ = [
    "VISUAL_EVIDENCE_QUALITY_SCHEMA",
    "FrameQualityObservation",
    "LocalRoadSceneObserver",
    "VisualEvidenceQualityError",
    "VisualEvidenceQualityPolicy",
    "VisualEvidenceQualityResult",
    "WindowQualityDecision",
    "evaluate_candidate_window_visual_quality",
]
