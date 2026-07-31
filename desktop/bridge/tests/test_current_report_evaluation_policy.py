from __future__ import annotations

from pathlib import Path

import pytest

from app.current_report_evaluation_policy import (
    CurrentReportEvaluationPolicyError,
    derive_page_pair_evaluation_ratings,
)
from app.hwpx_evaluation_grid import inspect_sample_evaluation_grid


WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q = WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"


@pytest.fixture(scope="module")
def labels():
    audit = inspect_sample_evaluation_grid(SAMPLE_3Q)
    return tuple(
        tuple(row.label for row in table.evaluation_rows)
        for table in audit.inspection_tables
    )


def test_user_final_severity_marks_only_direct_rows_and_road_summary(labels):
    result = derive_page_pair_evaluation_ratings(
        row_labels_by_page_pair=labels,
        incidents_by_page_pair={
            0: (
                {
                    "incident_type": "pothole",
                    "severity": "moderate",
                },
                {
                    "incident_type": "lane_marking_faded",
                    "severity": "major",
                },
            )
        },
    )

    vector = result["0"]
    comfort = next(
        index
        for index, label in enumerate(labels[0])
        if "주행 쾌적성" in label
    )
    lane = next(
        index
        for index, label in enumerate(labels[0])
        if "차선도색 상태" in label
    )
    summaries = [
        index
        for index, label in enumerate(labels[0])
        if "종합 결과" in label
    ]
    assert vector[comfort] == "poor"
    assert vector[lane] == "very_poor"
    assert vector[summaries[0]] == "very_poor"
    assert vector[summaries[1]] == "clear_unassessed"
    assert sum(value != "clear_unassessed" for value in vector) == 3


def test_quick_mode_and_unmapped_issue_do_not_invent_ratings(labels):
    result = derive_page_pair_evaluation_ratings(
        row_labels_by_page_pair=labels,
        incidents_by_page_pair={
            4: (
                {
                    "incident_type": "road_debris",
                    "severity": "unassessed",
                },
                {
                    "incident_type": "roadside_mowing_required",
                    "severity": "major",
                },
            )
        },
    )

    assert result["4"] == ["clear_unassessed"] * 27


def test_multiple_findings_use_worst_user_final_grade(labels):
    result = derive_page_pair_evaluation_ratings(
        row_labels_by_page_pair=labels,
        incidents_by_page_pair={
            2: (
                {
                    "incident_type": "sign_damage",
                    "severity": "minor",
                },
                {
                    "incident_type": "sign_vegetation_occlusion",
                    "severity": "major",
                },
            )
        },
    )
    road_sign = next(
        index
        for index, label in enumerate(labels[2])
        if "도로표지" in label
    )
    assert result["2"][road_sign] == "very_poor"


def test_invalid_or_ambiguous_layout_fails_closed(labels):
    broken = [list(row) for row in labels]
    broken[0][0] = "다른 항목"
    with pytest.raises(CurrentReportEvaluationPolicyError):
        derive_page_pair_evaluation_ratings(
            row_labels_by_page_pair=broken,
            incidents_by_page_pair={
                0: (
                    {
                        "incident_type": "pothole",
                        "severity": "major",
                    },
                )
            },
        )
