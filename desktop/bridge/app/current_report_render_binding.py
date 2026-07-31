"""Adapt the exact current-Incident binder to 48-page render expectations.

The exact HWPX binder and the post-COM PDF validator intentionally use
different contracts.  This module is the narrow, deterministic adapter between
them.  It accepts only the server-owned binding dataclass, preserves the exact
approved-Incident set, and derives one expected photo/caption count for every
audited odd/even page pair.

It does not read renderer input, write a document, invoke a Provider, or call
COM.  A malformed or partially bound manifest fails closed before any of those
later boundaries can run.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Mapping

from .current_incident_report_binder import (
    CURRENT_INCIDENT_REPORT_BINDING_SCHEMA,
    CurrentIncidentReportBindingManifest,
    ReportTargetMutation,
)
from .hwp_render_validation import (
    EXPECTED_PAGE_PAIR_COUNT,
    ExpectedPagePairSlot,
    ReportBindingManifest,
)


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_VISIBLE_TEXT_OPERATIONS = frozenset({"replace_text"})
_NEUTRAL_PHOTO_ROLES = frozenset(
    {
        "overlaid_stale_incident_photo",
        "unassigned_stale_incident_photo",
    }
)


class CurrentReportRenderBindingError(RuntimeError):
    """An apply-blocking current-report/render contract failure."""

    provider_called = False
    com_invoked = False
    document_written = False
    apply_blocked = True

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_report_render_binding(
    binding: CurrentIncidentReportBindingManifest,
) -> ReportBindingManifest:
    """Build all 24 render-validation page-pair expectations.

    Binder page-pair indices are zero-based; render-validation page-pair
    indices are one-based and correspond to physical pages ``1/2`` through
    ``47/48``.  Every approved Incident must resolve exactly once.
    """

    _validate_binding_header(binding)
    incidents_by_pair: dict[int, list[str]] = defaultdict(list)
    seen_incidents: set[str] = set()
    for incident in binding.incident_bindings:
        if (
            incident.incident_id not in binding.approved_incident_ids
            or incident.incident_id in seen_incidents
            or not 0
            <= incident.page_pair_index
            < EXPECTED_PAGE_PAIR_COUNT
            or incident.inspection_page_number
            != incident.page_pair_index * 2 + 1
            or incident.photo_page_number
            != incident.page_pair_index * 2 + 2
        ):
            raise CurrentReportRenderBindingError(
                "approved_incident_page_binding_invalid",
                "an approved Incident is missing or bound to an invalid page pair",
            )
        seen_incidents.add(incident.incident_id)
        incidents_by_pair[incident.page_pair_index].append(
            incident.incident_id
        )
    if (
        tuple(item.incident_id for item in binding.incident_bindings)
        != binding.approved_incident_ids
        or seen_incidents != set(binding.approved_incident_ids)
    ):
        raise CurrentReportRenderBindingError(
            "approved_incident_page_binding_incomplete",
            "every approved Incident must be bound exactly once",
        )

    visible_text_by_pair: dict[int, list[str]] = defaultdict(list)
    for mutation in binding.replacements:
        for text in _visible_text_evidence(mutation):
            visible_text_by_pair[mutation.page_pair_index].append(text)
    neutral_photo_maximum_by_pair = _neutral_photo_maximums(binding)

    slots = []
    for zero_based_pair in range(EXPECTED_PAGE_PAIR_COUNT):
        incident_ids = tuple(incidents_by_pair.get(zero_based_pair, ()))
        slots.append(
            ExpectedPagePairSlot(
                page_pair_index=zero_based_pair + 1,
                inspection_page_number=zero_based_pair * 2 + 1,
                photo_page_number=zero_based_pair * 2 + 2,
                incident_ids=incident_ids,
                expected_photo_count=len(incident_ids),
                expected_caption_count=len(incident_ids),
                expected_text_evidence=tuple(
                    visible_text_by_pair.get(zero_based_pair, ())
                ),
                maximum_neutral_picture_count=(
                    neutral_photo_maximum_by_pair.get(
                        zero_based_pair,
                        0,
                    )
                ),
            )
        )

    return ReportBindingManifest(
        template_id=binding.template_id,
        template_sha256=binding.template_sha256,
        expected_page_pair_slots=tuple(slots),
        current_incident_ids=binding.approved_incident_ids,
        expected_photo_count=len(binding.incident_bindings),
        expected_caption_count=len(binding.incident_bindings),
    )


def _validate_binding_header(
    binding: CurrentIncidentReportBindingManifest,
) -> None:
    if not isinstance(binding, CurrentIncidentReportBindingManifest):
        raise CurrentReportRenderBindingError(
            "current_report_binding_invalid",
            "a server-owned current-report binding is required",
        )
    hashes = (
        binding.source_sha256,
        binding.template_sha256,
        binding.template_semantic_map_sha256,
        binding.structure_target_summary_sha256,
        binding.input_sha256,
        binding.content_sha256,
        binding.slot_manifest_sha256,
        binding.binding_manifest_sha256,
    )
    if (
        binding.schema_version
        != CURRENT_INCIDENT_REPORT_BINDING_SCHEMA
        or binding.state
        != "current_incidents_bound_writer_required"
        or binding.approved_incident_bound is not True
        or binding.content_provenance_bound is not True
        or binding.writer_required is not True
        or binding.apply_eligible is not False
        or binding.provider_called is not False
        or binding.com_invoked is not False
        or binding.document_written is not False
        or any(_SHA256.fullmatch(value) is None for value in hashes)
        or binding.approved_incident_count
        != len(binding.approved_incident_ids)
        or binding.approved_incident_count
        != len(binding.incident_bindings)
        or len(binding.approved_incident_ids)
        != len(set(binding.approved_incident_ids))
        or binding.writer_batching_required is not False
        or binding.required_writer_operation_count
        > binding.exact_hwpx_writer_operation_limit
    ):
        raise CurrentReportRenderBindingError(
            "current_report_binding_invalid",
            "the current-report binding is incomplete or not writer-safe",
        )


def _visible_text_evidence(
    mutation: ReportTargetMutation,
) -> Iterable[str]:
    """Yield bounded text that must survive the HWP/PDF round trip."""

    if (
        mutation.operation not in _VISIBLE_TEXT_OPERATIONS
        or not 0
        <= mutation.page_pair_index
        < EXPECTED_PAGE_PAIR_COUNT
    ):
        return ()
    current: Mapping[str, Any] = mutation.current_value
    value = current.get("text")
    if not isinstance(value, str) or not value.strip():
        return ()
    # PDF text extraction may normalize line boundaries.  Bind each visible
    # line independently and keep the validator's own 500-character bound.
    return tuple(
        line
        for raw_line in value.splitlines()
        if (line := raw_line.strip()) and len(line) <= 500
    )


def _neutral_photo_maximums(
    binding: CurrentIncidentReportBindingManifest,
) -> dict[int, int]:
    """Bind the maximum number of writer-neutralized photo-page pictures."""

    maximums: dict[int, int] = defaultdict(int)
    seen_targets: set[str] = set()
    for mutation in binding.neutralizations:
        if mutation.operation != "remove_picture":
            continue
        is_photo_page = mutation.page_number % 2 == 0
        is_photo_role = mutation.role in _NEUTRAL_PHOTO_ROLES
        if not is_photo_page and not is_photo_role:
            # Inspection-page signature neutralization belongs to another
            # evidence contract and is deliberately outside photo counts.
            continue
        if (
            not is_photo_page
            or not is_photo_role
            or not 0
            <= mutation.page_pair_index
            < EXPECTED_PAGE_PAIR_COUNT
            or mutation.page_number
            != mutation.page_pair_index * 2 + 2
            or not mutation.target_id
            or mutation.target_id in seen_targets
            or mutation.expected_value.get("picture_target_id")
            != mutation.target_id
            or mutation.current_value.get("neutralized") is not True
        ):
            raise CurrentReportRenderBindingError(
                "neutral_photo_binding_invalid",
                "a neutralized photo target is malformed or unbound",
            )
        seen_targets.add(mutation.target_id)
        maximums[mutation.page_pair_index] += 1
    return dict(maximums)


__all__ = [
    "CurrentReportRenderBindingError",
    "build_report_render_binding",
]
