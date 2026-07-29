"""Deterministic, review-only grouping for frame-level road detections.

This module deliberately stops at *proposal* creation.  It does not import any
incident, annotation, maintenance-action, dataset, or model-training store.
Every accepted input and every output carries explicit review-only safety
gates so a caller cannot accidentally treat temporal grouping as approval.

The grouping algorithm is conservative:

* detections with different issue types are never compared;
* the gap to the last detection in a proposal must be bounded;
* conflicting track hints keep detections separate by default;
* comparable boxes must pass either the IoU or centre-distance gate;
* temporal-only merging is allowed only when spatial evidence is unavailable
  and the policy explicitly permits it.

All validation happens before grouping.  Therefore malformed input never
produces a partial set of proposals.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .candidate_labels import CandidateLabelError, canonical_issue_type


PROPOSAL_SCHEMA = "trafficvisson.temporal-incident-proposal.v1"
GROUPING_ALGORITHM = "temporal-spatial-review-grouping.v1"

MAX_DETECTIONS = 100_000
MAX_TIMESTAMP_SECONDS = 2_678_400.0
MAX_PROVENANCE_BYTES = 64 * 1024
MAX_PROVENANCE_DEPTH = 8
MAX_PROVENANCE_ITEMS = 2_048
MAX_TEXT_LENGTH = 4_096

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ISSUE_TYPE_INPUT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SCORE_SEMANTICS = frozenset(
    {
        "model_score_not_probability",
        "zero_shot_relative_not_probability",
    }
)
_REQUIRED_DETECTION_FIELDS = frozenset(
    {
        "detection_id",
        "timestamp_seconds",
        "issue_type",
        "score",
        "review_only",
        "automatic_labeling_allowed",
    }
)
_OPTIONAL_DETECTION_FIELDS = frozenset(
    {
        "normalized_box",
        "box",
        "track_id",
        "track_hint",
        "frame_index",
        "score_semantics",
    }
)
_MERGE_REASON_ORDER = {
    "same_track_hint": 0,
    "spatial_iou": 1,
    "spatial_center_distance": 2,
    "bounded_gap_without_comparable_spatial_hint": 3,
}


class TemporalIncidentProposalError(ValueError):
    """Frame detections or grouping policy failed strict validation."""


@dataclass(frozen=True)
class TemporalGroupingPolicy:
    """Bounded, serializable policy used by the deterministic grouper."""

    max_gap_seconds: float = 2.0
    min_iou: float = 0.10
    max_center_distance: float = 0.15
    strict_track_separation: bool = True
    allow_temporal_only_without_spatial_hints: bool = True

    def __post_init__(self) -> None:
        _finite_number(
            self.max_gap_seconds,
            "policy.max_gap_seconds",
            minimum=0.001,
            maximum=60.0,
        )
        _finite_number(
            self.min_iou,
            "policy.min_iou",
            minimum=0.0,
            maximum=1.0,
        )
        _finite_number(
            self.max_center_distance,
            "policy.max_center_distance",
            minimum=0.0,
            maximum=math.sqrt(2.0),
        )
        if type(self.strict_track_separation) is not bool:
            raise TemporalIncidentProposalError(
                "policy.strict_track_separation must be a boolean"
            )
        if type(self.allow_temporal_only_without_spatial_hints) is not bool:
            raise TemporalIncidentProposalError(
                "policy.allow_temporal_only_without_spatial_hints "
                "must be a boolean"
            )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_gap_seconds": float(self.max_gap_seconds),
            "min_iou": float(self.min_iou),
            "max_center_distance": float(self.max_center_distance),
            "strict_track_separation": self.strict_track_separation,
            "allow_temporal_only_without_spatial_hints": (
                self.allow_temporal_only_without_spatial_hints
            ),
        }


@dataclass(frozen=True)
class _Detection:
    detection_id: str
    timestamp_seconds: float
    issue_type: str
    score: float
    normalized_box: Optional[Dict[str, float]]
    track_hint: Optional[str]
    frame_index: Optional[int]
    score_semantics: Optional[str]

    def evidence_dict(self) -> Dict[str, Any]:
        return {
            "detection_id": self.detection_id,
            "timestamp_seconds": self.timestamp_seconds,
            "score": self.score,
            "normalized_box": (
                dict(self.normalized_box)
                if self.normalized_box is not None
                else None
            ),
            "track_hint": self.track_hint,
            "frame_index": self.frame_index,
            "score_semantics": self.score_semantics,
        }

    def fingerprint_dict(self) -> Dict[str, Any]:
        return {
            **self.evidence_dict(),
            "issue_type": self.issue_type,
            "review_only": True,
            "automatic_labeling_allowed": False,
        }


@dataclass(frozen=True)
class _Match:
    reason: str
    gap_seconds: float
    iou: Optional[float]
    center_distance: Optional[float]
    rank: Tuple[float, ...]


@dataclass
class _ProposalBuilder:
    issue_type: str
    detections: List[_Detection] = field(default_factory=list)
    merge_links: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def last(self) -> _Detection:
        return self.detections[-1]

    @property
    def seed_id(self) -> str:
        return self.detections[0].detection_id


def group_temporal_incident_proposals(
    detections: Sequence[Mapping[str, Any]],
    *,
    provenance: Mapping[str, Any],
    policy: Optional[TemporalGroupingPolicy] = None,
) -> List[Dict[str, Any]]:
    """Return deterministic review-only temporal incident proposals.

    ``detections`` may be in any order.  Each item must contain:
    ``detection_id``, ``timestamp_seconds``, ``issue_type``, ``score``,
    ``review_only=True``, and ``automatic_labeling_allowed=False``.
    ``normalized_box``/``box`` and ``track_hint``/``track_id`` are optional
    aliases.  Supplying both aliases for the same concept is rejected.

    The returned dictionaries are transport-safe proposal records.  They are
    not approved incidents and cannot be projected into training data without
    a separate user-review boundary.
    """

    effective_policy = policy or TemporalGroupingPolicy()
    if not isinstance(effective_policy, TemporalGroupingPolicy):
        raise TemporalIncidentProposalError(
            "policy must be a TemporalGroupingPolicy"
        )
    normalized_provenance = _validated_provenance(provenance)
    validated = _validated_detections(detections)
    if not validated:
        return []

    builders: List[_ProposalBuilder] = []
    by_issue_type: Dict[str, List[_ProposalBuilder]] = {}
    for detection in sorted(
        validated,
        key=lambda item: (
            item.timestamp_seconds,
            item.issue_type,
            item.detection_id,
        ),
    ):
        candidates: List[Tuple[Tuple[float, ...], str, _ProposalBuilder, _Match]] = []
        for builder in by_issue_type.get(detection.issue_type, []):
            match = _match(builder.last, detection, effective_policy)
            if match is not None:
                candidates.append(
                    (match.rank, builder.seed_id, builder, match)
                )

        if not candidates:
            builder = _ProposalBuilder(
                issue_type=detection.issue_type,
                detections=[detection],
            )
            builders.append(builder)
            by_issue_type.setdefault(detection.issue_type, []).append(builder)
            continue

        _, _, builder, match = min(
            candidates,
            key=lambda item: (item[0], item[1]),
        )
        previous = builder.last
        builder.detections.append(detection)
        builder.merge_links.append(
            {
                "from_detection_id": previous.detection_id,
                "to_detection_id": detection.detection_id,
                "gap_seconds": _rounded(match.gap_seconds),
                "reason": match.reason,
                "iou": (
                    _rounded(match.iou)
                    if match.iou is not None
                    else None
                ),
                "center_distance": (
                    _rounded(match.center_distance)
                    if match.center_distance is not None
                    else None
                ),
            }
        )

    proposals = [
        _serialize_proposal(
            builder,
            provenance=normalized_provenance,
            policy=effective_policy,
        )
        for builder in builders
    ]
    return sorted(
        proposals,
        key=lambda item: (
            item["start_seconds"],
            item["end_seconds"],
            item["issue_type"],
            item["proposal_id"],
        ),
    )


def _validated_detections(
    raw_detections: Sequence[Mapping[str, Any]],
) -> List[_Detection]:
    if (
        isinstance(raw_detections, (str, bytes, bytearray, Mapping))
        or not isinstance(raw_detections, Sequence)
    ):
        raise TemporalIncidentProposalError(
            "detections must be a sequence of objects"
        )
    if len(raw_detections) > MAX_DETECTIONS:
        raise TemporalIncidentProposalError(
            f"detections exceeds the {MAX_DETECTIONS} item limit"
        )

    validated: List[_Detection] = []
    seen_ids: set[str] = set()
    allowed_fields = _REQUIRED_DETECTION_FIELDS | _OPTIONAL_DETECTION_FIELDS
    for index, raw in enumerate(raw_detections):
        prefix = f"detections[{index}]"
        if not isinstance(raw, Mapping):
            raise TemporalIncidentProposalError(f"{prefix} must be an object")
        fields = set(raw)
        missing = _REQUIRED_DETECTION_FIELDS - fields
        extra = fields - allowed_fields
        if missing or extra:
            raise TemporalIncidentProposalError(
                f"{prefix} fields are invalid"
            )

        detection_id = _safe_text(
            raw["detection_id"],
            f"{prefix}.detection_id",
            pattern=_SAFE_ID,
        )
        if detection_id in seen_ids:
            raise TemporalIncidentProposalError(
                f"{prefix}.detection_id is duplicated"
            )
        seen_ids.add(detection_id)

        if raw["review_only"] is not True:
            raise TemporalIncidentProposalError(
                f"{prefix}.review_only must be true"
            )
        if raw["automatic_labeling_allowed"] is not False:
            raise TemporalIncidentProposalError(
                f"{prefix}.automatic_labeling_allowed must be false"
            )

        raw_issue_type = _safe_text(
            raw["issue_type"],
            f"{prefix}.issue_type",
            pattern=_ISSUE_TYPE_INPUT,
        )
        try:
            issue_type = canonical_issue_type(raw_issue_type)
        except CandidateLabelError as error:
            raise TemporalIncidentProposalError(
                f"{prefix}.issue_type is not a supported canonical type or alias"
            ) from error
        timestamp = _finite_number(
            raw["timestamp_seconds"],
            f"{prefix}.timestamp_seconds",
            minimum=0.0,
            maximum=MAX_TIMESTAMP_SECONDS,
        )
        score = _finite_number(
            raw["score"],
            f"{prefix}.score",
            minimum=0.0,
            maximum=1.0,
        )

        if "normalized_box" in raw and "box" in raw:
            raise TemporalIncidentProposalError(
                f"{prefix} cannot contain both normalized_box and box"
            )
        box_value = raw.get("normalized_box", raw.get("box"))
        normalized_box = _validated_box(
            box_value,
            f"{prefix}.normalized_box",
        )

        if "track_hint" in raw and "track_id" in raw:
            raise TemporalIncidentProposalError(
                f"{prefix} cannot contain both track_hint and track_id"
            )
        track_value = raw.get("track_hint", raw.get("track_id"))
        track_hint = (
            _safe_text(
                track_value,
                f"{prefix}.track_hint",
                pattern=_SAFE_ID,
            )
            if track_value is not None
            else None
        )

        frame_value = raw.get("frame_index")
        if frame_value is not None and (
            isinstance(frame_value, bool)
            or not isinstance(frame_value, int)
            or frame_value < 0
            or frame_value > 9_007_199_254_740_991
        ):
            raise TemporalIncidentProposalError(
                f"{prefix}.frame_index must be a non-negative safe integer"
            )

        semantics_value = raw.get("score_semantics")
        if semantics_value is not None and semantics_value not in _SCORE_SEMANTICS:
            raise TemporalIncidentProposalError(
                f"{prefix}.score_semantics is invalid"
            )

        validated.append(
            _Detection(
                detection_id=detection_id,
                timestamp_seconds=_rounded(timestamp),
                issue_type=issue_type,
                score=float(score),
                normalized_box=normalized_box,
                track_hint=track_hint,
                frame_index=frame_value,
                score_semantics=semantics_value,
            )
        )
    return validated


def _match(
    previous: _Detection,
    current: _Detection,
    policy: TemporalGroupingPolicy,
) -> Optional[_Match]:
    if previous.issue_type != current.issue_type:
        return None
    gap = current.timestamp_seconds - previous.timestamp_seconds
    if gap < 0 or gap > policy.max_gap_seconds:
        return None

    both_tracks = (
        previous.track_hint is not None and current.track_hint is not None
    )
    if (
        both_tracks
        and previous.track_hint != current.track_hint
        and policy.strict_track_separation
    ):
        return None

    iou: Optional[float] = None
    center_distance: Optional[float] = None
    if previous.normalized_box is not None and current.normalized_box is not None:
        iou = _box_iou(previous.normalized_box, current.normalized_box)
        center_distance = _box_center_distance(
            previous.normalized_box,
            current.normalized_box,
        )

    if both_tracks and previous.track_hint == current.track_hint:
        reason = "same_track_hint"
        priority = 0.0
    elif iou is not None and iou >= policy.min_iou:
        reason = "spatial_iou"
        priority = 1.0
    elif (
        center_distance is not None
        and center_distance <= policy.max_center_distance
    ):
        reason = "spatial_center_distance"
        priority = 2.0
    elif iou is not None:
        return None
    elif policy.allow_temporal_only_without_spatial_hints:
        reason = "bounded_gap_without_comparable_spatial_hint"
        priority = 3.0
    else:
        return None

    # Lower tuple wins.  Stronger evidence, smaller gap, and the most recent
    # compatible proposal are preferred before the stable seed-id tie breaker.
    rank = (
        priority,
        -(iou if iou is not None else -1.0),
        center_distance if center_distance is not None else math.inf,
        gap,
        -previous.timestamp_seconds,
    )
    return _Match(
        reason=reason,
        gap_seconds=gap,
        iou=iou,
        center_distance=center_distance,
        rank=rank,
    )


def _serialize_proposal(
    builder: _ProposalBuilder,
    *,
    provenance: Dict[str, Any],
    policy: TemporalGroupingPolicy,
) -> Dict[str, Any]:
    detections = sorted(
        builder.detections,
        key=lambda item: (item.timestamp_seconds, item.detection_id),
    )
    start = detections[0].timestamp_seconds
    end = detections[-1].timestamp_seconds
    peak = min(
        detections,
        key=lambda item: (
            -item.score,
            item.timestamp_seconds,
            item.detection_id,
        ),
    )
    policy_dict = policy.as_dict()
    evidence_fingerprint = _sha256_json(
        [item.fingerprint_dict() for item in detections]
    )
    source_fingerprint = _sha256_json(provenance)
    identity_payload = {
        "schema_version": PROPOSAL_SCHEMA,
        "algorithm": GROUPING_ALGORITHM,
        "issue_type": builder.issue_type,
        "evidence_detection_ids": [
            item.detection_id for item in detections
        ],
        "evidence_fingerprint": evidence_fingerprint,
        "source_fingerprint": source_fingerprint,
        "grouping_policy": policy_dict,
    }
    proposal_id = f"tip_{_sha256_json(identity_payload)[:24]}"
    unique_reasons = sorted(
        {link["reason"] for link in builder.merge_links},
        key=lambda reason: (_MERGE_REASON_ORDER[reason], reason),
    )
    return {
        "schema_version": PROPOSAL_SCHEMA,
        "proposal_id": proposal_id,
        "proposal_status": "awaiting_user_review",
        "issue_type": builder.issue_type,
        "start_seconds": start,
        "end_seconds": end,
        "duration_seconds": _rounded(end - start),
        "detection_count": len(detections),
        "evidence_detection_ids": [
            item.detection_id for item in detections
        ],
        "peak_evidence": peak.evidence_dict(),
        "merge_reasons": unique_reasons or ["single_detection"],
        "merge_links": list(builder.merge_links),
        "review_only": True,
        "automatic_incident_creation_allowed": False,
        "automatic_annotation_allowed": False,
        "automatic_maintenance_action_allowed": False,
        "automatic_training_allowed": False,
        "provenance": {
            "algorithm": GROUPING_ALGORITHM,
            "source": provenance,
            "source_fingerprint_sha256": source_fingerprint,
            "evidence_fingerprint_sha256": evidence_fingerprint,
            "grouping_policy": policy_dict,
        },
    }


def _validated_box(
    raw: Any,
    field_name: str,
) -> Optional[Dict[str, float]]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or set(raw) != {
        "x",
        "y",
        "width",
        "height",
    }:
        raise TemporalIncidentProposalError(
            f"{field_name} must contain only x, y, width, and height"
        )
    box = {
        key: _finite_number(
            raw[key],
            f"{field_name}.{key}",
            minimum=0.0,
            maximum=1.0,
        )
        for key in ("x", "y", "width", "height")
    }
    if box["width"] <= 0 or box["height"] <= 0:
        raise TemporalIncidentProposalError(
            f"{field_name} width and height must be positive"
        )
    epsilon = 1e-12
    if (
        box["x"] + box["width"] > 1.0 + epsilon
        or box["y"] + box["height"] > 1.0 + epsilon
    ):
        raise TemporalIncidentProposalError(
            f"{field_name} extends outside the normalized image"
        )
    return {key: float(value) for key, value in box.items()}


def _box_iou(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    x1 = max(left["x"], right["x"])
    y1 = max(left["y"], right["y"])
    x2 = min(
        left["x"] + left["width"],
        right["x"] + right["width"],
    )
    y2 = min(
        left["y"] + left["height"],
        right["y"] + right["height"],
    )
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (
        left["width"] * left["height"]
        + right["width"] * right["height"]
        - intersection
    )
    if union <= 0:
        return 0.0
    return intersection / union


def _box_center_distance(
    left: Mapping[str, float],
    right: Mapping[str, float],
) -> float:
    left_x = left["x"] + left["width"] / 2.0
    left_y = left["y"] + left["height"] / 2.0
    right_x = right["x"] + right["width"] / 2.0
    right_y = right["y"] + right["height"] / 2.0
    return math.hypot(left_x - right_x, left_y - right_y)


def _safe_text(value: Any, field_name: str, *, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise TemporalIncidentProposalError(f"{field_name} must be text")
    normalized = value.strip()
    if not pattern.fullmatch(normalized):
        raise TemporalIncidentProposalError(f"{field_name} is invalid")
    return normalized


def _finite_number(
    value: Any,
    field_name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise TemporalIncidentProposalError(
            f"{field_name} must be a finite number"
        )
    normalized = float(value)
    if normalized < minimum or normalized > maximum:
        raise TemporalIncidentProposalError(
            f"{field_name} is outside [{minimum}, {maximum}]"
        )
    return normalized


def _validated_provenance(raw: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, Mapping) or not raw:
        raise TemporalIncidentProposalError(
            "provenance must be a non-empty object"
        )
    normalized = _bounded_json(raw, "provenance", depth=0)
    assert isinstance(normalized, dict)
    if len(_canonical_json(normalized)) > MAX_PROVENANCE_BYTES:
        raise TemporalIncidentProposalError(
            f"provenance exceeds {MAX_PROVENANCE_BYTES} bytes"
        )
    return normalized


def _bounded_json(value: Any, field_name: str, *, depth: int) -> Any:
    if depth > MAX_PROVENANCE_DEPTH:
        raise TemporalIncidentProposalError(
            f"{field_name} is nested too deeply"
        )
    if value is None or type(value) is bool or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TemporalIncidentProposalError(
                f"{field_name} contains a non-finite number"
            )
        return value
    if isinstance(value, str):
        if len(value) > MAX_TEXT_LENGTH:
            raise TemporalIncidentProposalError(
                f"{field_name} contains oversized text"
            )
        if any(ord(character) < 32 for character in value):
            raise TemporalIncidentProposalError(
                f"{field_name} contains control characters"
            )
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_PROVENANCE_ITEMS:
            raise TemporalIncidentProposalError(
                f"{field_name} contains too many fields"
            )
        keys = list(value)
        if any(
            not isinstance(key, str) or not key or len(key) > 128
            for key in keys
        ):
            raise TemporalIncidentProposalError(
                f"{field_name} contains an invalid key"
            )
        normalized: Dict[str, Any] = {}
        for key in sorted(keys):
            normalized[key] = _bounded_json(
                value[key],
                f"{field_name}.{key}",
                depth=depth + 1,
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        if len(value) > MAX_PROVENANCE_ITEMS:
            raise TemporalIncidentProposalError(
                f"{field_name} contains too many items"
            )
        return [
            _bounded_json(
                item,
                f"{field_name}[{index}]",
                depth=depth + 1,
            )
            for index, item in enumerate(value)
        ]
    raise TemporalIncidentProposalError(
        f"{field_name} contains a non-JSON value"
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _rounded(value: float) -> float:
    return round(float(value), 6)


__all__ = [
    "GROUPING_ALGORITHM",
    "PROPOSAL_SCHEMA",
    "TemporalGroupingPolicy",
    "TemporalIncidentProposalError",
    "group_temporal_incident_proposals",
]
