from __future__ import annotations

from pathlib import Path

import pytest

import command_proposal_runner as runner


EXPECTED_CANONICAL_TYPES = (
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


def _request(tmp_path: Path, issue_type: object) -> dict[str, object]:
    return {
        "schema_version": runner.REQUEST_SCHEMA,
        "project_id": "11111111-1111-4111-8111-111111111111",
        "analysis_job_id": "22222222-2222-4222-8222-222222222222",
        "recipe_id": "a" * 64,
        "command_sha256": "b" * 64,
        "prompt_sha256": "c" * 64,
        "issue_type": issue_type,
        "positive_prompts": ["road maintenance issue"],
        "negative_prompts": ["clean road", "undamaged road"],
        "runner_manifest_sha256": "d" * 64,
        "model_manifest_sha256": "e" * 64,
        "model_pack_root": str(tmp_path.resolve()),
        "candidates": [
            {
                "candidate_id": "0123456789abcdefabcd",
                "image_path": str((tmp_path / "candidate.jpg").resolve()),
                "image_sha256": "f" * 64,
                "size_bytes": 1,
            }
        ],
    }


def test_runner_exposes_exact_twelve_canonical_product_types() -> None:
    assert runner.CANONICAL_ISSUE_TYPES == EXPECTED_CANONICAL_TYPES
    assert runner.ISSUE_TYPES == frozenset(
        (*EXPECTED_CANONICAL_TYPES, "other")
    )
    assert "sign_identity" not in runner.ISSUE_TYPES
    assert "color_guidance_line_faded" not in runner.ISSUE_TYPES


@pytest.mark.parametrize("issue_type", EXPECTED_CANONICAL_TYPES)
def test_validate_request_accepts_and_preserves_canonical_type(
    tmp_path: Path,
    issue_type: str,
) -> None:
    normalized = runner._validate_request(_request(tmp_path, issue_type))

    assert normalized["issue_type"] == issue_type


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    (
        ("sign_identity", "guide_sign_identity"),
        ("color_guidance_line_faded", "colored_guidance_faded"),
    ),
)
def test_validate_request_normalizes_legacy_type_before_output(
    tmp_path: Path,
    legacy: str,
    canonical: str,
) -> None:
    normalized = runner._validate_request(_request(tmp_path, legacy))

    assert normalized["issue_type"] == canonical
    assert normalized["issue_type"] not in runner.ISSUE_TYPE_ALIASES


@pytest.mark.parametrize(
    "issue_type",
    (None, 7, "", "colored_guidance_line_fading", "unknown"),
)
def test_validate_request_rejects_unknown_issue_type(
    tmp_path: Path,
    issue_type: object,
) -> None:
    with pytest.raises(runner.RunnerError, match="issue type is invalid"):
        runner._validate_request(_request(tmp_path, issue_type))
