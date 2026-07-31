"""Derive sample evaluation-square proposals from user-final road findings.

The 3Q/4Q checklist contains broad facility ratings.  A confirmed Incident is
not enough to rate unrelated rows, and an AI/YOLO severity proposal is never a
rating authority.  This module therefore marks only a row that has a direct,
deterministic mapping from the user's final issue type and final severity.
Everything else remains ``clear_unassessed`` so historical sample squares
cannot leak into the current report.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


class CurrentReportEvaluationPolicyError(ValueError):
    """The current-report rating input is incomplete or ambiguous."""


_GRADE_ORDER = {
    "very_good": 0,
    "good": 1,
    "normal": 2,
    "poor": 3,
    "very_poor": 4,
}
_SEVERITY_TO_GRADE = {
    "minor": "normal",
    "moderate": "poor",
    "major": "very_poor",
    "urgent": "very_poor",
}
_ISSUE_ROW_TERM = {
    "pothole": "주행 쾌적성",
    "road_surface_damage": "주행 쾌적성",
    "road_debris": "도로 청결성",
    "median_barrier_damage": "차량방호시설",
    "sign_damage": "도로표지",
    "sign_vegetation_occlusion": "도로표지",
    "guide_sign_identity": "도로표지",
    "lane_marking_faded": "차선도색 상태",
    "colored_guidance_faded": "차선도색 상태",
    "tunnel_identity": "터널 관리상태",
    "tunnel_name_occlusion": "터널 관리상태",
}
_SUPPORTED_SEVERITIES = frozenset(
    {"minor", "moderate", "major", "urgent", "unassessed"}
)
_SUPPORTED_ISSUES = frozenset(
    {
        *_ISSUE_ROW_TERM,
        "roadside_mowing_required",
        "other",
    }
)


def _normalized_label(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _one_matching_row(
    labels: Sequence[str],
    term: str,
) -> int:
    matches = [
        index
        for index, label in enumerate(labels)
        if term in _normalized_label(label)
    ]
    if len(matches) != 1:
        raise CurrentReportEvaluationPolicyError(
            f"evaluation row mapping for {term!r} is missing or ambiguous"
        )
    return matches[0]


def _road_summary_row(labels: Sequence[str]) -> int:
    matches = [
        index
        for index, label in enumerate(labels)
        if "종합 결과" in _normalized_label(label)
    ]
    if len(matches) != 2:
        raise CurrentReportEvaluationPolicyError(
            "the audited checklist must contain two summary rows"
        )
    # The first summary belongs to the road/facility section.  The second is
    # the rest-area section and must not be rated from a driving video.
    return matches[0]


def _worse_grade(current: str | None, proposed: str) -> str:
    if current is None:
        return proposed
    return (
        proposed
        if _GRADE_ORDER[proposed] > _GRADE_ORDER[current]
        else current
    )


def derive_page_pair_evaluation_ratings(
    *,
    row_labels_by_page_pair: Sequence[Sequence[str]],
    incidents_by_page_pair: Mapping[int, Sequence[Mapping[str, Any]]],
) -> dict[str, list[str]]:
    """Return one 27-value vector for every occupied page pair.

    ``severity`` must be the user's final value.  ``unassessed`` (quick mode)
    deliberately leaves the matching row blank.  Unmapped issue types such as
    generic mowing/other remain in captions and opinions but do not invent a
    checklist grade.
    """

    if (
        isinstance(row_labels_by_page_pair, (str, bytes))
        or not isinstance(row_labels_by_page_pair, Sequence)
        or len(row_labels_by_page_pair) != 24
        or not isinstance(incidents_by_page_pair, Mapping)
    ):
        raise CurrentReportEvaluationPolicyError(
            "evaluation page-pair input is invalid"
        )
    result: dict[str, list[str]] = {}
    for pair, incidents in sorted(incidents_by_page_pair.items()):
        if (
            isinstance(pair, bool)
            or not isinstance(pair, int)
            or not 0 <= pair < len(row_labels_by_page_pair)
            or isinstance(incidents, (str, bytes, Mapping))
            or not isinstance(incidents, Sequence)
            or not incidents
        ):
            raise CurrentReportEvaluationPolicyError(
                "an occupied page pair is invalid"
            )
        labels = row_labels_by_page_pair[pair]
        if (
            isinstance(labels, (str, bytes))
            or not isinstance(labels, Sequence)
            or len(labels) != 27
            or any(
                not isinstance(label, str) or not label.strip()
                for label in labels
            )
        ):
            raise CurrentReportEvaluationPolicyError(
                "the audited evaluation row vector is invalid"
            )
        selected: dict[int, str] = {}
        for incident in incidents:
            if not isinstance(incident, Mapping):
                raise CurrentReportEvaluationPolicyError(
                    "an assigned Incident is invalid"
                )
            issue_type = incident.get("incident_type")
            severity = incident.get("severity")
            if (
                issue_type not in _SUPPORTED_ISSUES
                or severity not in _SUPPORTED_SEVERITIES
            ):
                raise CurrentReportEvaluationPolicyError(
                    "an assigned Incident type or severity is invalid"
                )
            term = _ISSUE_ROW_TERM.get(str(issue_type))
            grade = _SEVERITY_TO_GRADE.get(str(severity))
            if term is None or grade is None:
                continue
            row_index = _one_matching_row(labels, term)
            selected[row_index] = _worse_grade(
                selected.get(row_index),
                grade,
            )
        if selected:
            summary = _road_summary_row(labels)
            summary_grade = max(
                selected.values(),
                key=lambda value: _GRADE_ORDER[value],
            )
            selected[summary] = _worse_grade(
                selected.get(summary),
                summary_grade,
            )
        vector = ["clear_unassessed"] * len(labels)
        for row_index, grade in selected.items():
            vector[row_index] = grade
        result[str(pair)] = vector
    return result


__all__ = [
    "CurrentReportEvaluationPolicyError",
    "derive_page_pair_evaluation_ratings",
]
