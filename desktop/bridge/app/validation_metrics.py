"""Deterministic validation metrics and model-promotion review policy.

This module deliberately performs no inference and makes no model-quality
claims.  It consumes an already-fixed validation truth set and already-matched
prediction events, validates their schema and relationships, and computes
event-level metrics.  A candidate can only become *eligible for user review*;
this module never authorizes automatic promotion.

Truth records enumerate every validation sample for every evaluated issue
type.  ``truth_event_ids`` is empty for a negative sample and contains one or
more event identifiers for a positive sample.  Prediction records use
``matched_truth_event_id`` only when an upstream, fixed matching procedure has
assigned the prediction to a truth event.  Additional predictions assigned to
the same truth event are counted as both duplicate predictions and false
positives.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, Literal, Mapping, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


VALIDATION_SCHEMA_VERSION = "trafficvisson.validation-metrics.v1"
PROMOTION_POLICY_VERSION = "trafficvisson.promotion-review-policy.v1"
DEFAULT_MINIMUM_SAMPLES_PER_TYPE = 30
DEFAULT_MAX_RECALL_DROP = 0.02
MAX_TRUTH_RECORDS = 1_000_000
MAX_PREDICTION_RECORDS = 2_000_000
MAX_TRUTH_EVENTS_PER_SAMPLE = 256

# Master-plan canonical issue-type contract.  ``other`` is intentionally not a
# promotable production type.
CANONICAL_ISSUE_TYPES: Tuple[str, ...] = (
    "pothole",
    "road_surface_damage",
    "road_debris",
    "median_barrier_damage",
    "sign_damage",
    "sign_vegetation_occlusion",
    "roadside_mowing_required",
    "guide_sign_identity",
    "tunnel_identity",
    "tunnel_name_occlusion",
    "lane_marking_faded",
    "colored_guidance_faded",
)

CanonicalIssueType = Literal[
    "pothole",
    "road_surface_damage",
    "road_debris",
    "median_barrier_damage",
    "sign_damage",
    "sign_vegetation_occlusion",
    "roadside_mowing_required",
    "guide_sign_identity",
    "tunnel_identity",
    "tunnel_name_occlusion",
    "lane_marking_faded",
    "colored_guidance_faded",
]

_ISSUE_ORDER = {
    issue_type: index for index, issue_type in enumerate(CANONICAL_ISSUE_TYPES)
}
# Model versions elsewhere in TrafficVisson use the canonical
# ``pack-name@version`` form.  Keep validation identifiers compatible with
# that existing product contract while retaining a small, path-free alphabet.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ValidationMetricsError(ValueError):
    """The fixed validation records or policy are invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )


def _canonical_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(
            f"{label} must be 1-128 canonical identifier characters"
        )
    return value


class ValidationTruthRecord(_StrictModel):
    """Truth for one fixed validation sample and one canonical issue type."""

    sample_id: str
    issue_type: CanonicalIssueType
    truth_event_ids: Tuple[str, ...] = Field(
        default=(),
        max_length=MAX_TRUTH_EVENTS_PER_SAMPLE,
    )

    @field_validator("sample_id")
    @classmethod
    def canonical_sample_id(cls, value: object) -> str:
        return _canonical_identifier(value, "sample_id")

    @field_validator("truth_event_ids")
    @classmethod
    def canonical_truth_event_ids(
        cls,
        value: Tuple[str, ...],
    ) -> Tuple[str, ...]:
        normalized = tuple(
            _canonical_identifier(item, "truth_event_id") for item in value
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("truth_event_ids must not contain duplicates")
        return tuple(sorted(normalized))


class ValidationPredictionRecord(_StrictModel):
    """One fixed prediction event with an optional upstream truth match."""

    prediction_event_id: str
    sample_id: str
    issue_type: CanonicalIssueType
    matched_truth_event_id: Optional[str] = None

    @field_validator("prediction_event_id")
    @classmethod
    def canonical_prediction_event_id(cls, value: object) -> str:
        return _canonical_identifier(value, "prediction_event_id")

    @field_validator("sample_id")
    @classmethod
    def canonical_sample_id(cls, value: object) -> str:
        return _canonical_identifier(value, "sample_id")

    @field_validator("matched_truth_event_id")
    @classmethod
    def canonical_match_id(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        return _canonical_identifier(value, "matched_truth_event_id")


class PerTypeValidationMetrics(_StrictModel):
    issue_type: CanonicalIssueType
    validation_sample_count: int = Field(ge=0)
    positive_sample_count: int = Field(ge=0)
    negative_sample_count: int = Field(ge=0)
    truth_event_count: int = Field(ge=0)
    prediction_event_count: int = Field(ge=0)
    true_positive_count: int = Field(ge=0)
    false_positive_count: int = Field(ge=0)
    false_negative_count: int = Field(ge=0)
    duplicate_prediction_count: int = Field(ge=0)
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    false_positive_rate: float = Field(ge=0, le=1)
    duplicate_burden: float = Field(ge=0, le=1)

    @field_validator(
        "precision",
        "recall",
        "false_positive_rate",
        "duplicate_burden",
    )
    @classmethod
    def finite_rate(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric rates must be finite")
        return value


class ModelValidationReport(_StrictModel):
    schema_version: Literal["trafficvisson.validation-metrics.v1"]
    model_version: str
    validation_set_sha256: str
    prediction_set_sha256: str
    per_type: Tuple[PerTypeValidationMetrics, ...]

    @field_validator("model_version")
    @classmethod
    def canonical_model_version(cls, value: object) -> str:
        return _canonical_identifier(value, "model_version")

    @field_validator("validation_set_sha256", "prediction_set_sha256")
    @classmethod
    def sha256_hex(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("digest must be a lowercase SHA-256 hex string")
        return value

    @field_validator("per_type")
    @classmethod
    def complete_canonical_order(
        cls,
        value: Tuple[PerTypeValidationMetrics, ...],
    ) -> Tuple[PerTypeValidationMetrics, ...]:
        observed = tuple(item.issue_type for item in value)
        if observed != CANONICAL_ISSUE_TYPES:
            raise ValueError(
                "per_type must contain every canonical issue type in order"
            )
        return value


PromotionReasonCode = Literal[
    "insufficient_validation_samples",
    "missing_positive_truth",
    "recall_drop_at_or_above_limit",
    "false_positive_rate_increased",
    "duplicate_burden_increased",
]


class PromotionReviewReason(_StrictModel):
    code: PromotionReasonCode
    issue_type: CanonicalIssueType
    message: str = Field(min_length=1, max_length=500)
    active_value: Optional[float] = None
    candidate_value: Optional[float] = None
    limit_value: Optional[float] = None

    @field_validator("active_value", "candidate_value", "limit_value")
    @classmethod
    def finite_optional_number(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and not math.isfinite(value):
            raise ValueError("promotion reason values must be finite")
        return value


class PromotionReviewDecision(_StrictModel):
    schema_version: Literal["trafficvisson.promotion-review-policy.v1"]
    decision: Literal["eligible", "rejected"]
    active_model_version: str
    candidate_model_version: str
    validation_set_sha256: str
    required_issue_types: Tuple[CanonicalIssueType, ...]
    minimum_samples_per_type: int = Field(ge=1)
    maximum_recall_drop: float = Field(gt=0, le=1)
    active_report: ModelValidationReport
    candidate_report: ModelValidationReport
    reasons: Tuple[PromotionReviewReason, ...]
    auto_promotion_allowed: Literal[False] = False
    requires_user_review: Literal[True] = True

    @field_validator("active_model_version", "candidate_model_version")
    @classmethod
    def canonical_model_version(cls, value: object) -> str:
        return _canonical_identifier(value, "model_version")

    @field_validator("validation_set_sha256")
    @classmethod
    def sha256_hex(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("digest must be a lowercase SHA-256 hex string")
        return value

    @field_validator("maximum_recall_drop")
    @classmethod
    def finite_recall_drop(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("maximum_recall_drop must be finite")
        return value


def _validated_records(
    model: Type[_StrictModel],
    records: Iterable[object],
    *,
    label: str,
    maximum: int,
) -> Tuple[Any, ...]:
    if isinstance(records, (str, bytes, bytearray, Mapping)):
        raise ValidationMetricsError(f"{label} must be an iterable of records")

    validated = []
    try:
        iterator = iter(records)
    except TypeError as error:
        raise ValidationMetricsError(
            f"{label} must be an iterable of records"
        ) from error

    for index, record in enumerate(iterator):
        if index >= maximum:
            raise ValidationMetricsError(
                f"{label} exceeds the maximum of {maximum} records"
            )
        try:
            if isinstance(record, model):
                validated.append(record)
            elif isinstance(record, Mapping):
                validated.append(model.model_validate(dict(record)))
            else:
                raise ValidationMetricsError(
                    f"{label}[{index}] must be an object"
                )
        except ValidationError as error:
            raise ValidationMetricsError(
                f"{label}[{index}] failed schema validation: {error}"
            ) from error
    return tuple(validated)


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _truth_identity(
    record: ValidationTruthRecord,
) -> Tuple[int, str]:
    return (_ISSUE_ORDER[record.issue_type], record.sample_id)


def _prediction_identity(
    record: ValidationPredictionRecord,
) -> Tuple[int, str, str]:
    return (
        _ISSUE_ORDER[record.issue_type],
        record.sample_id,
        record.prediction_event_id,
    )


def _normalize_truth_records(
    records: Iterable[object],
) -> Tuple[ValidationTruthRecord, ...]:
    truth = _validated_records(
        ValidationTruthRecord,
        records,
        label="truth_records",
        maximum=MAX_TRUTH_RECORDS,
    )
    identities = set()
    for record in truth:
        identity = (record.sample_id, record.issue_type)
        if identity in identities:
            raise ValidationMetricsError(
                "truth_records contain duplicate sample/type pairs"
            )
        identities.add(identity)
    return tuple(sorted(truth, key=_truth_identity))


def _normalize_prediction_records(
    records: Iterable[object],
) -> Tuple[ValidationPredictionRecord, ...]:
    predictions = _validated_records(
        ValidationPredictionRecord,
        records,
        label="prediction_records",
        maximum=MAX_PREDICTION_RECORDS,
    )
    prediction_ids = set()
    for record in predictions:
        if record.prediction_event_id in prediction_ids:
            raise ValidationMetricsError(
                "prediction_records contain duplicate prediction_event_id values"
            )
        prediction_ids.add(record.prediction_event_id)
    return tuple(sorted(predictions, key=_prediction_identity))


def _validate_relationships(
    truth: Tuple[ValidationTruthRecord, ...],
    predictions: Tuple[ValidationPredictionRecord, ...],
) -> None:
    truth_by_sample_type = {
        (record.sample_id, record.issue_type): set(record.truth_event_ids)
        for record in truth
    }
    for prediction in predictions:
        key = (prediction.sample_id, prediction.issue_type)
        if key not in truth_by_sample_type:
            raise ValidationMetricsError(
                "prediction record is outside the fixed validation sample/type set"
            )
        match_id = prediction.matched_truth_event_id
        if match_id is not None and match_id not in truth_by_sample_type[key]:
            raise ValidationMetricsError(
                "matched_truth_event_id does not belong to the prediction "
                "sample and issue type"
            )


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def evaluate_model(
    *,
    model_version: str,
    truth_records: Iterable[object],
    prediction_records: Iterable[object],
) -> ModelValidationReport:
    """Validate fixed records and return deterministic metrics for all 12 types."""

    try:
        normalized_model_version = _canonical_identifier(
            model_version,
            "model_version",
        )
    except ValueError as error:
        raise ValidationMetricsError(str(error)) from error

    truth = _normalize_truth_records(truth_records)
    predictions = _normalize_prediction_records(prediction_records)
    _validate_relationships(truth, predictions)

    truth_payload = [
        {
            "issue_type": record.issue_type,
            "sample_id": record.sample_id,
            "truth_event_ids": list(record.truth_event_ids),
        }
        for record in truth
    ]
    prediction_payload = [
        {
            "issue_type": record.issue_type,
            "matched_truth_event_id": record.matched_truth_event_id,
            "prediction_event_id": record.prediction_event_id,
            "sample_id": record.sample_id,
        }
        for record in predictions
    ]

    metrics = []
    for issue_type in CANONICAL_ISSUE_TYPES:
        type_truth = tuple(
            record for record in truth if record.issue_type == issue_type
        )
        type_predictions = tuple(
            record
            for record in predictions
            if record.issue_type == issue_type
        )

        truth_event_ids = {
            truth_event_id
            for record in type_truth
            for truth_event_id in record.truth_event_ids
        }
        matched_event_ids = set()
        duplicate_count = 0
        unmatched_count = 0
        for prediction in type_predictions:
            matched = prediction.matched_truth_event_id
            if matched is None:
                unmatched_count += 1
            elif matched in matched_event_ids:
                duplicate_count += 1
            else:
                matched_event_ids.add(matched)

        true_positive_count = len(matched_event_ids)
        false_positive_count = unmatched_count + duplicate_count
        false_negative_count = len(truth_event_ids) - true_positive_count
        prediction_count = len(type_predictions)
        positive_sample_count = sum(
            bool(record.truth_event_ids) for record in type_truth
        )

        metrics.append(
            PerTypeValidationMetrics(
                issue_type=issue_type,
                validation_sample_count=len(type_truth),
                positive_sample_count=positive_sample_count,
                negative_sample_count=len(type_truth) - positive_sample_count,
                truth_event_count=len(truth_event_ids),
                prediction_event_count=prediction_count,
                true_positive_count=true_positive_count,
                false_positive_count=false_positive_count,
                false_negative_count=false_negative_count,
                duplicate_prediction_count=duplicate_count,
                precision=_safe_ratio(
                    true_positive_count,
                    true_positive_count + false_positive_count,
                ),
                recall=_safe_ratio(
                    true_positive_count,
                    true_positive_count + false_negative_count,
                ),
                # This is the false-discovery share of fixed prediction
                # events, including duplicates, rather than an image-level
                # TN-derived rate.
                false_positive_rate=_safe_ratio(
                    false_positive_count,
                    prediction_count,
                ),
                duplicate_burden=_safe_ratio(
                    duplicate_count,
                    prediction_count,
                ),
            )
        )

    return ModelValidationReport(
        schema_version=VALIDATION_SCHEMA_VERSION,
        model_version=normalized_model_version,
        validation_set_sha256=_canonical_json_sha256(truth_payload),
        prediction_set_sha256=_canonical_json_sha256(prediction_payload),
        per_type=tuple(metrics),
    )


def _required_types(
    values: Optional[Iterable[object]],
) -> Tuple[CanonicalIssueType, ...]:
    if values is None:
        return CANONICAL_ISSUE_TYPES
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        raise ValidationMetricsError(
            "required_issue_types must be an iterable of canonical types"
        )

    observed = set()
    for value in values:
        if not isinstance(value, str) or value not in _ISSUE_ORDER:
            raise ValidationMetricsError(
                "required_issue_types contains a non-canonical type"
            )
        if value in observed:
            raise ValidationMetricsError(
                "required_issue_types must not contain duplicates"
            )
        observed.add(value)
    if not observed:
        raise ValidationMetricsError("required_issue_types must not be empty")
    return tuple(
        issue_type
        for issue_type in CANONICAL_ISSUE_TYPES
        if issue_type in observed
    )


def _strict_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationMetricsError(f"{label} must be a positive integer")
    return value


def _strict_recall_drop(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        or float(value) > 1
    ):
        raise ValidationMetricsError(
            "maximum_recall_drop must be a finite number in (0, 1]"
        )
    return float(value)


def _ratio_increased(
    *,
    candidate_numerator: int,
    candidate_denominator: int,
    active_numerator: int,
    active_denominator: int,
) -> bool:
    """Compare two non-negative ratios without floating-point rounding."""

    if candidate_denominator == 0:
        return False
    if active_denominator == 0:
        return candidate_numerator > 0
    return (
        candidate_numerator * active_denominator
        > active_numerator * candidate_denominator
    )


def compare_candidate_to_active(
    *,
    active_model_version: str,
    candidate_model_version: str,
    truth_records: Iterable[object],
    active_prediction_records: Iterable[object],
    candidate_prediction_records: Iterable[object],
    required_issue_types: Optional[Iterable[object]] = None,
    minimum_samples_per_type: int = DEFAULT_MINIMUM_SAMPLES_PER_TYPE,
    maximum_recall_drop: float = DEFAULT_MAX_RECALL_DROP,
) -> PromotionReviewDecision:
    """Apply the fail-closed promotion policy to one fixed validation set.

    ``eligible`` means only that no configured blocker was found.  Both
    possible decisions require a human review and set
    ``auto_promotion_allowed`` to ``False``.
    """

    try:
        active_version = _canonical_identifier(
            active_model_version,
            "active_model_version",
        )
        candidate_version = _canonical_identifier(
            candidate_model_version,
            "candidate_model_version",
        )
    except ValueError as error:
        raise ValidationMetricsError(str(error)) from error
    if active_version == candidate_version:
        raise ValidationMetricsError(
            "active and candidate model versions must be distinct"
        )

    required_types = _required_types(required_issue_types)
    minimum_samples = _strict_positive_int(
        minimum_samples_per_type,
        "minimum_samples_per_type",
    )
    recall_limit = _strict_recall_drop(maximum_recall_drop)

    # Materialize truth exactly once so generators cannot yield different
    # validation sets to the active and candidate evaluations.
    normalized_truth = _normalize_truth_records(truth_records)
    active_report = evaluate_model(
        model_version=active_version,
        truth_records=normalized_truth,
        prediction_records=active_prediction_records,
    )
    candidate_report = evaluate_model(
        model_version=candidate_version,
        truth_records=normalized_truth,
        prediction_records=candidate_prediction_records,
    )
    if (
        active_report.validation_set_sha256
        != candidate_report.validation_set_sha256
    ):
        # Defensive invariant: both reports were produced from the same
        # materialized records, so a mismatch indicates an implementation
        # error rather than an ordinary rejection.
        raise ValidationMetricsError(
            "active and candidate reports do not share a validation set"
        )

    active_by_type = {
        metric.issue_type: metric for metric in active_report.per_type
    }
    candidate_by_type = {
        metric.issue_type: metric for metric in candidate_report.per_type
    }
    reasons = []
    for issue_type in required_types:
        active = active_by_type[issue_type]
        candidate = candidate_by_type[issue_type]

        if active.validation_sample_count < minimum_samples:
            reasons.append(
                PromotionReviewReason(
                    code="insufficient_validation_samples",
                    issue_type=issue_type,
                    message=(
                        f"{issue_type}: validation samples "
                        f"{active.validation_sample_count} < {minimum_samples}"
                    ),
                    candidate_value=float(active.validation_sample_count),
                    limit_value=float(minimum_samples),
                )
            )
        if active.truth_event_count == 0:
            reasons.append(
                PromotionReviewReason(
                    code="missing_positive_truth",
                    issue_type=issue_type,
                    message=(
                        f"{issue_type}: the fixed validation set has no "
                        "positive truth event"
                    ),
                    candidate_value=0.0,
                    limit_value=1.0,
                )
            )

        # The truth-event denominator is identical for both reports.  Compare
        # the TP count difference against the configured percentage-point
        # limit.  Equality is a blocker (a 2pp drop is rejected).
        if active.truth_event_count > 0:
            recall_drop = active.recall - candidate.recall
            if recall_drop >= recall_limit - 1e-15:
                reasons.append(
                    PromotionReviewReason(
                        code="recall_drop_at_or_above_limit",
                        issue_type=issue_type,
                        message=(
                            f"{issue_type}: recall dropped by "
                            f"{recall_drop:.6f}, at or above "
                            f"{recall_limit:.6f}"
                        ),
                        active_value=active.recall,
                        candidate_value=candidate.recall,
                        limit_value=recall_limit,
                    )
                )

        if _ratio_increased(
            candidate_numerator=candidate.false_positive_count,
            candidate_denominator=candidate.prediction_event_count,
            active_numerator=active.false_positive_count,
            active_denominator=active.prediction_event_count,
        ):
            reasons.append(
                PromotionReviewReason(
                    code="false_positive_rate_increased",
                    issue_type=issue_type,
                    message=(
                        f"{issue_type}: false-positive rate increased from "
                        f"{active.false_positive_rate:.6f} to "
                        f"{candidate.false_positive_rate:.6f}"
                    ),
                    active_value=active.false_positive_rate,
                    candidate_value=candidate.false_positive_rate,
                    limit_value=active.false_positive_rate,
                )
            )

        if _ratio_increased(
            candidate_numerator=candidate.duplicate_prediction_count,
            candidate_denominator=candidate.prediction_event_count,
            active_numerator=active.duplicate_prediction_count,
            active_denominator=active.prediction_event_count,
        ):
            reasons.append(
                PromotionReviewReason(
                    code="duplicate_burden_increased",
                    issue_type=issue_type,
                    message=(
                        f"{issue_type}: duplicate burden increased from "
                        f"{active.duplicate_burden:.6f} to "
                        f"{candidate.duplicate_burden:.6f}"
                    ),
                    active_value=active.duplicate_burden,
                    candidate_value=candidate.duplicate_burden,
                    limit_value=active.duplicate_burden,
                )
            )

    return PromotionReviewDecision(
        schema_version=PROMOTION_POLICY_VERSION,
        decision="rejected" if reasons else "eligible",
        active_model_version=active_version,
        candidate_model_version=candidate_version,
        validation_set_sha256=active_report.validation_set_sha256,
        required_issue_types=required_types,
        minimum_samples_per_type=minimum_samples,
        maximum_recall_drop=recall_limit,
        active_report=active_report,
        candidate_report=candidate_report,
        reasons=tuple(reasons),
        auto_promotion_allowed=False,
        requires_user_review=True,
    )


__all__ = [
    "CANONICAL_ISSUE_TYPES",
    "CanonicalIssueType",
    "DEFAULT_MAX_RECALL_DROP",
    "DEFAULT_MINIMUM_SAMPLES_PER_TYPE",
    "ModelValidationReport",
    "PerTypeValidationMetrics",
    "PromotionReviewDecision",
    "PromotionReviewReason",
    "ValidationMetricsError",
    "ValidationPredictionRecord",
    "ValidationTruthRecord",
    "compare_candidate_to_active",
    "evaluate_model",
]
