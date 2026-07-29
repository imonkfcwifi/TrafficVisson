from __future__ import annotations

import pytest

from app.validation_metrics import (
    CANONICAL_ISSUE_TYPES,
    ValidationMetricsError,
    compare_candidate_to_active,
    evaluate_model,
)


def _truth(
    issue_type: str = "pothole",
    *,
    sample_count: int = 30,
    positive_count: int | None = None,
):
    positives = sample_count if positive_count is None else positive_count
    return [
        {
            "sample_id": f"sample-{index:03d}",
            "issue_type": issue_type,
            "truth_event_ids": (
                [f"truth-{index:03d}"] if index < positives else []
            ),
        }
        for index in range(sample_count)
    ]


def _matched_predictions(
    count: int,
    issue_type: str = "pothole",
):
    return [
        {
            "prediction_event_id": f"prediction-{index:03d}",
            "sample_id": f"sample-{index:03d}",
            "issue_type": issue_type,
            "matched_truth_event_id": f"truth-{index:03d}",
        }
        for index in range(count)
    ]


def test_master_plan_canonical_contract_has_exactly_twelve_types():
    assert CANONICAL_ISSUE_TYPES == (
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


def test_metrics_count_duplicates_as_false_positives_without_double_recall():
    truth = _truth(sample_count=2)
    predictions = _matched_predictions(2)
    predictions.append(
        {
            "prediction_event_id": "prediction-duplicate",
            "sample_id": "sample-000",
            "issue_type": "pothole",
            "matched_truth_event_id": "truth-000",
        }
    )
    predictions.append(
        {
            "prediction_event_id": "prediction-unmatched",
            "sample_id": "sample-001",
            "issue_type": "pothole",
            "matched_truth_event_id": None,
        }
    )

    report = evaluate_model(
        model_version="candidate-v1",
        truth_records=truth,
        prediction_records=predictions,
    )
    metric = report.per_type[0]

    assert metric.true_positive_count == 2
    assert metric.false_positive_count == 2
    assert metric.false_negative_count == 0
    assert metric.duplicate_prediction_count == 1
    assert metric.precision == 0.5
    assert metric.recall == 1.0
    assert metric.false_positive_rate == 0.5
    assert metric.duplicate_burden == 0.25


def test_event_identity_is_scoped_to_sample_type_and_event_id():
    truth = [
        {
            "sample_id": sample_id,
            "issue_type": "pothole",
            "truth_event_ids": ["shared-event-id"],
        }
        for sample_id in ("sample-a", "sample-b")
    ]
    predictions = [
        {
            # Reusing an upstream-local event id in another sample is valid.
            "prediction_event_id": "shared-prediction-id",
            "sample_id": sample_id,
            "issue_type": "pothole",
            "matched_truth_event_id": "shared-event-id",
        }
        for sample_id in ("sample-a", "sample-b")
    ]

    report = evaluate_model(
        model_version="candidate-v1",
        truth_records=truth,
        prediction_records=predictions,
    )
    metric = report.per_type[0]

    assert metric.truth_event_count == 2
    assert metric.prediction_event_count == 2
    assert metric.true_positive_count == 2
    assert metric.false_positive_count == 0
    assert metric.false_negative_count == 0
    assert metric.duplicate_prediction_count == 0

    with pytest.raises(ValidationMetricsError, match="sample/type/event"):
        evaluate_model(
            model_version="candidate-v1",
            truth_records=truth,
            prediction_records=[predictions[0], dict(predictions[0])],
        )


def test_two_percentage_point_recall_drop_is_rejected_at_equality():
    truth = _truth(sample_count=50)
    decision = compare_candidate_to_active(
        active_model_version="active-v1",
        candidate_model_version="candidate-v2",
        truth_records=truth,
        active_prediction_records=_matched_predictions(50),
        candidate_prediction_records=_matched_predictions(49),
        required_issue_types=["pothole"],
        minimum_samples_per_type=30,
        maximum_recall_drop=0.02,
    )

    assert decision.decision == "rejected"
    assert {
        reason.code for reason in decision.reasons
    } == {"recall_drop_at_or_above_limit"}
    assert decision.auto_promotion_allowed is False
    assert decision.requires_user_review is True


def test_false_positive_and_duplicate_increase_reject_candidate():
    truth = _truth(sample_count=30)
    active = _matched_predictions(30)
    candidate = _matched_predictions(30)
    candidate.extend(
        [
            {
                "prediction_event_id": "candidate-duplicate",
                "sample_id": "sample-000",
                "issue_type": "pothole",
                "matched_truth_event_id": "truth-000",
            },
            {
                "prediction_event_id": "candidate-unmatched",
                "sample_id": "sample-001",
                "issue_type": "pothole",
                "matched_truth_event_id": None,
            },
        ]
    )

    decision = compare_candidate_to_active(
        active_model_version="active-v1",
        candidate_model_version="candidate-v2",
        truth_records=truth,
        active_prediction_records=active,
        candidate_prediction_records=candidate,
        required_issue_types=["pothole"],
    )

    assert decision.decision == "rejected"
    assert {
        reason.code for reason in decision.reasons
    } == {
        "false_positive_rate_increased",
        "duplicate_burden_increased",
    }


def test_absolute_and_per_sample_burden_increase_rejects_lower_ratio():
    truth = _truth(sample_count=30)
    active = _matched_predictions(10)
    active.extend(
        [
            {
                "prediction_event_id": "active-duplicate",
                "sample_id": "sample-000",
                "issue_type": "pothole",
                "matched_truth_event_id": "truth-000",
            },
        ]
    )
    candidate = _matched_predictions(30)
    candidate.extend(
        [
            {
                "prediction_event_id": f"candidate-duplicate-{index}",
                "sample_id": "sample-000",
                "issue_type": "pothole",
                "matched_truth_event_id": "truth-000",
            }
            for index in range(2)
        ]
    )

    decision = compare_candidate_to_active(
        active_model_version="active-v1",
        candidate_model_version="candidate-v2",
        truth_records=truth,
        active_prediction_records=active,
        candidate_prediction_records=candidate,
        required_issue_types=["pothole"],
    )

    active_metric = decision.active_report.per_type[0]
    candidate_metric = decision.candidate_report.per_type[0]
    assert (
        candidate_metric.false_positive_rate
        < active_metric.false_positive_rate
    )
    assert candidate_metric.duplicate_burden < active_metric.duplicate_burden
    assert decision.decision == "rejected"
    assert {
        reason.code for reason in decision.reasons
    } == {
        "false_positive_rate_increased",
        "duplicate_burden_increased",
    }
    assert all(
        "per-validation-sample burden" in reason.message
        for reason in decision.reasons
    )


def test_eligible_still_requires_explicit_user_promotion():
    truth = _truth(sample_count=30)
    predictions = _matched_predictions(30)
    decision = compare_candidate_to_active(
        active_model_version="active-v1",
        candidate_model_version="candidate-v2",
        truth_records=reversed(truth),
        active_prediction_records=reversed(predictions),
        candidate_prediction_records=predictions,
        required_issue_types=["pothole"],
    )

    assert decision.decision == "eligible"
    assert decision.reasons == ()
    assert decision.auto_promotion_allowed is False
    assert decision.requires_user_review is True
    assert (
        decision.active_report.validation_set_sha256
        == decision.candidate_report.validation_set_sha256
    )


@pytest.mark.parametrize(
    "truth_records,prediction_records,message",
    [
        (
            [
                {
                    "sample_id": "sample-000",
                    "issue_type": "other",
                    "truth_event_ids": [],
                }
            ],
            [],
            "schema validation",
        ),
        (
            _truth(sample_count=1),
            [
                {
                    "prediction_event_id": "prediction-outside",
                    "sample_id": "sample-999",
                    "issue_type": "pothole",
                    "matched_truth_event_id": None,
                }
            ],
            "outside the fixed validation",
        ),
        (
            _truth(sample_count=1),
            [
                {
                    "prediction_event_id": "prediction-wrong-match",
                    "sample_id": "sample-000",
                    "issue_type": "pothole",
                    "matched_truth_event_id": "truth-999",
                }
            ],
            "does not belong",
        ),
    ],
)
def test_invalid_or_cross_sample_validation_records_fail_closed(
    truth_records,
    prediction_records,
    message,
):
    with pytest.raises(ValidationMetricsError, match=message):
        evaluate_model(
            model_version="candidate-v1",
            truth_records=truth_records,
            prediction_records=prediction_records,
        )
