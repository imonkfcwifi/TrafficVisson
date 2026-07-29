from __future__ import annotations

import copy
import math

import pytest

from app.temporal_incidents import (
    GROUPING_ALGORITHM,
    PROPOSAL_SCHEMA,
    TemporalGroupingPolicy,
    TemporalIncidentProposalError,
    group_temporal_incident_proposals,
)


PROVENANCE = {
    "project_id": "11111111-1111-4111-8111-111111111111",
    "analysis_job_id": "22222222-2222-4222-8222-222222222222",
    "model": {
        "provider": "onnx_yolo_v8",
        "version": "review-only-0.1.0",
    },
}


def _detection(
    detection_id: str,
    timestamp_seconds: float,
    *,
    issue_type: str = "road_debris",
    score: float = 0.5,
    box=None,
    track_hint: str | None = None,
):
    result = {
        "detection_id": detection_id,
        "timestamp_seconds": timestamp_seconds,
        "issue_type": issue_type,
        "score": score,
        "review_only": True,
        "automatic_labeling_allowed": False,
        "score_semantics": "model_score_not_probability",
    }
    if box is not None:
        result["normalized_box"] = box
    if track_hint is not None:
        result["track_hint"] = track_hint
    return result


def _box(x: float, y: float, width: float = 0.2, height: float = 0.2):
    return {"x": x, "y": y, "width": width, "height": height}


def test_grouping_is_order_independent_and_emits_only_review_proposals():
    detections = [
        _detection(
            "det-b",
            11.0,
            score=0.92,
            box=_box(0.21, 0.20),
            track_hint="track-7",
        ),
        _detection(
            "det-a",
            10.0,
            score=0.70,
            box=_box(0.20, 0.20),
            track_hint="track-7",
        ),
        _detection(
            "det-c",
            12.5,
            score=0.92,
            box=_box(0.22, 0.20),
            track_hint="track-7",
        ),
    ]

    forward = group_temporal_incident_proposals(
        detections,
        provenance=PROVENANCE,
    )
    reverse = group_temporal_incident_proposals(
        list(reversed(detections)),
        provenance=copy.deepcopy(PROVENANCE),
    )

    assert forward == reverse
    assert len(forward) == 1
    proposal = forward[0]
    assert proposal["schema_version"] == PROPOSAL_SCHEMA
    assert proposal["proposal_id"].startswith("tip_")
    assert len(proposal["proposal_id"]) == 28
    assert proposal["proposal_status"] == "awaiting_user_review"
    assert proposal["start_seconds"] == 10.0
    assert proposal["end_seconds"] == 12.5
    assert proposal["duration_seconds"] == 2.5
    assert proposal["evidence_detection_ids"] == ["det-a", "det-b", "det-c"]
    # Equal peak scores choose the earliest timestamp deterministically.
    assert proposal["peak_evidence"]["detection_id"] == "det-b"
    assert proposal["merge_reasons"] == ["same_track_hint"]
    assert proposal["review_only"] is True
    assert proposal["automatic_incident_creation_allowed"] is False
    assert proposal["automatic_annotation_allowed"] is False
    assert proposal["automatic_maintenance_action_allowed"] is False
    assert proposal["automatic_training_allowed"] is False
    assert proposal["provenance"]["algorithm"] == GROUPING_ALGORITHM
    assert proposal["provenance"]["source"] == PROVENANCE
    assert len(proposal["provenance"]["source_fingerprint_sha256"]) == 64
    assert len(proposal["provenance"]["evidence_fingerprint_sha256"]) == 64


def test_type_gap_track_and_spatial_gates_keep_unrelated_detections_separate():
    detections = [
        _detection("debris-a", 0.0, box=_box(0.05, 0.2), track_hint="left"),
        # Same type and time, but a conflicting track hint is a hard boundary.
        _detection("debris-b", 0.5, box=_box(0.06, 0.2), track_hint="right"),
        # No track hint, but spatially far from both active proposals.
        _detection("debris-c", 1.0, box=_box(0.75, 0.7)),
        # Even an overlapping box cannot cross an issue-type boundary.
        _detection(
            "pothole-a",
            1.1,
            issue_type="pothole",
            box=_box(0.75, 0.7),
        ),
        # The gap to every debris proposal exceeds the default two seconds.
        _detection("debris-d", 4.0, box=_box(0.75, 0.7)),
    ]

    proposals = group_temporal_incident_proposals(
        detections,
        provenance=PROVENANCE,
    )

    assert [item["evidence_detection_ids"] for item in proposals] == [
        ["debris-a"],
        ["debris-b"],
        ["debris-c"],
        ["pothole-a"],
        ["debris-d"],
    ]
    assert all(item["merge_reasons"] == ["single_detection"] for item in proposals)


def test_iou_center_distance_and_temporal_only_paths_are_provenanced():
    detections = [
        _detection("iou-a", 0.0, box=_box(0.10, 0.10)),
        _detection("iou-b", 0.5, box=_box(0.12, 0.10)),
        # No overlap with the previous box, but close enough by centre.
        _detection(
            "center-c",
            1.0,
            box=_box(0.315, 0.10, width=0.01, height=0.2),
        ),
        # Spatial evidence is unavailable, so the explicit temporal-only policy
        # is recorded instead of pretending there was a spatial match.
        _detection("temporal-d", 1.5),
    ]

    proposal = group_temporal_incident_proposals(
        detections,
        provenance=PROVENANCE,
        policy=TemporalGroupingPolicy(max_center_distance=0.16),
    )[0]

    assert proposal["evidence_detection_ids"] == [
        "iou-a",
        "iou-b",
        "center-c",
        "temporal-d",
    ]
    assert proposal["merge_reasons"] == [
        "spatial_iou",
        "spatial_center_distance",
        "bounded_gap_without_comparable_spatial_hint",
    ]
    assert [link["reason"] for link in proposal["merge_links"]] == [
        "spatial_iou",
        "spatial_center_distance",
        "bounded_gap_without_comparable_spatial_hint",
    ]
    assert proposal["merge_links"][0]["iou"] > 0.1
    assert proposal["merge_links"][1]["center_distance"] <= 0.16
    assert proposal["merge_links"][2]["iou"] is None

    disabled = group_temporal_incident_proposals(
        detections[-2:],
        provenance=PROVENANCE,
        policy=TemporalGroupingPolicy(
            max_center_distance=0.16,
            allow_temporal_only_without_spatial_hints=False,
        ),
    )
    assert len(disabled) == 2


def test_exact_gap_boundary_merges_but_epsilon_over_boundary_does_not():
    proposals = group_temporal_incident_proposals(
        [
            _detection("a", 10.0, track_hint="track"),
            _detection("b", 12.0, track_hint="track"),
            _detection("c", 14.000001, track_hint="track"),
        ],
        provenance=PROVENANCE,
        policy=TemporalGroupingPolicy(max_gap_seconds=2.0),
    )

    assert [item["evidence_detection_ids"] for item in proposals] == [
        ["a", "b"],
        ["c"],
    ]
    assert proposals[0]["merge_links"][0]["gap_seconds"] == 2.0


def test_legacy_issue_aliases_are_normalized_before_grouping():
    proposal = group_temporal_incident_proposals(
        [
            _detection(
                "legacy",
                1.0,
                issue_type="color_guidance_line_faded",
            )
        ],
        provenance=PROVENANCE,
    )[0]

    assert proposal["issue_type"] == "colored_guidance_faded"

    with pytest.raises(TemporalIncidentProposalError, match="not a supported"):
        group_temporal_incident_proposals(
            [_detection("unknown", 1.0, issue_type="unknown_defect")],
            provenance=PROVENANCE,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.update(review_only=False), "review_only"),
        (
            lambda row: row.update(automatic_labeling_allowed=True),
            "automatic_labeling_allowed",
        ),
        (lambda row: row.update(score=float("nan")), "finite number"),
        (lambda row: row.update(timestamp_seconds=-1), "outside"),
        (lambda row: row.update(issue_type=""), "issue_type"),
        (lambda row: row.update(unexpected=True), "fields are invalid"),
        (
            lambda row: row.update(
                normalized_box=_box(0.9, 0.9, width=0.2, height=0.2)
            ),
            "outside",
        ),
    ],
)
def test_malformed_or_unsafe_detection_rejects_the_entire_batch(
    mutation,
    message,
):
    valid = _detection("valid", 1.0)
    invalid = _detection("invalid", 2.0)
    mutation(invalid)

    with pytest.raises(TemporalIncidentProposalError, match=message):
        group_temporal_incident_proposals(
            [valid, invalid],
            provenance=PROVENANCE,
        )


def test_duplicate_ids_alias_conflicts_and_malformed_provenance_are_rejected():
    row = _detection("duplicate", 1.0)
    with pytest.raises(TemporalIncidentProposalError, match="duplicated"):
        group_temporal_incident_proposals(
            [row, copy.deepcopy(row)],
            provenance=PROVENANCE,
        )

    alias_conflict = _detection("alias", 1.0, box=_box(0.1, 0.1))
    alias_conflict["box"] = _box(0.1, 0.1)
    with pytest.raises(TemporalIncidentProposalError, match="both"):
        group_temporal_incident_proposals(
            [alias_conflict],
            provenance=PROVENANCE,
        )

    with pytest.raises(TemporalIncidentProposalError, match="non-empty"):
        group_temporal_incident_proposals([row], provenance={})
    with pytest.raises(TemporalIncidentProposalError, match="non-finite"):
        group_temporal_incident_proposals(
            [row],
            provenance={"bad": math.inf},
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_gap_seconds": 0},
        {"max_gap_seconds": 61},
        {"min_iou": -0.1},
        {"min_iou": 1.1},
        {"max_center_distance": -0.1},
        {"strict_track_separation": 1},
        {"allow_temporal_only_without_spatial_hints": "yes"},
    ],
)
def test_invalid_policy_is_rejected(kwargs):
    with pytest.raises(TemporalIncidentProposalError):
        TemporalGroupingPolicy(**kwargs)


def test_empty_detection_batch_is_valid_but_still_requires_provenance():
    assert (
        group_temporal_incident_proposals([], provenance=PROVENANCE)
        == []
    )
    with pytest.raises(TemporalIncidentProposalError, match="non-empty"):
        group_temporal_incident_proposals([], provenance={})
