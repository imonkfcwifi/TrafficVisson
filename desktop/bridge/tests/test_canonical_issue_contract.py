from __future__ import annotations

from pydantic import TypeAdapter

from app import (
    auto_labeling,
    candidate_labels,
    source_registry,
    validation_metrics,
    vision_inference,
)
from app.contracts import VisionIssueType


EXPECTED_CANONICAL_ISSUE_TYPES = (
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


def test_every_bridge_boundary_uses_the_same_twelve_product_types() -> None:
    assert candidate_labels.CANONICAL_ISSUE_TYPES == (
        EXPECTED_CANONICAL_ISSUE_TYPES
    )
    assert auto_labeling.CANONICAL_ISSUE_TYPES == (
        EXPECTED_CANONICAL_ISSUE_TYPES
    )
    assert source_registry.CANONICAL_ISSUE_TYPES == (
        EXPECTED_CANONICAL_ISSUE_TYPES
    )
    assert vision_inference.CANONICAL_ISSUE_TYPES == (
        EXPECTED_CANONICAL_ISSUE_TYPES
    )
    assert validation_metrics.CANONICAL_ISSUE_TYPES == (
        EXPECTED_CANONICAL_ISSUE_TYPES
    )

    expected_with_extension = frozenset(
        (*EXPECTED_CANONICAL_ISSUE_TYPES, "other")
    )
    assert candidate_labels.ISSUE_TYPES == expected_with_extension
    assert frozenset(auto_labeling.ISSUE_TYPES) == expected_with_extension
    assert frozenset(source_registry.ISSUE_TYPES) == expected_with_extension
    assert vision_inference.ISSUE_TYPES == expected_with_extension


def test_contract_ingress_normalizes_legacy_values_before_persistence() -> None:
    adapter = TypeAdapter(VisionIssueType)

    assert adapter.validate_python("sign_identity") == "guide_sign_identity"
    assert adapter.validate_python("color_guidance_line_faded") == (
        "colored_guidance_faded"
    )
    assert adapter.validate_python("colored_guidance_line_faded") == (
        "colored_guidance_faded"
    )
    assert {
        adapter.validate_python(issue_type)
        for issue_type in EXPECTED_CANONICAL_ISSUE_TYPES
    } == set(EXPECTED_CANONICAL_ISSUE_TYPES)
