from __future__ import annotations

import copy

import pytest

from app.training_geometry import (
    SCHEMA_VERSION,
    TrainingGeometryError,
    canonical_training_geometry,
    seal_training_geometry,
    training_geometry_from_review,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _ai_box() -> dict:
    return seal_training_geometry(
        {
            "schema_version": SCHEMA_VERSION,
            "source_kind": "accepted_ai_proposal",
            "approval_authority": "local_user",
            "image_sha256": SHA_A,
            "image_artifact_ref": "candidate-evidence:overlay:frame-1",
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            },
            "polygon": None,
            "mask_artifact_ref": None,
            "mask_sha256": None,
            "tracked_frame_refs": [],
            "proposal_sha256": SHA_B,
            "sam2_output_sha256": None,
            "sam2_model_manifest_sha256": None,
        }
    )


def _sam2_mask() -> dict:
    return seal_training_geometry(
        {
            "schema_version": SCHEMA_VERSION,
            "source_kind": "user_sam2_refinement",
            "approval_authority": "local_user",
            "image_sha256": SHA_A,
            "image_artifact_ref": "candidate-evidence:overlay:frame-1",
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            },
            "polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.6]],
            "mask_artifact_ref": "sam2-mask:refinement-1",
            "mask_sha256": SHA_C,
            "tracked_frame_refs": [
                {
                    "frame_id": "frame-1",
                    "image_sha256": SHA_A,
                    "timestamp_seconds": 10.0,
                    "normalized_box": {
                        "x": 0.1,
                        "y": 0.2,
                        "width": 0.3,
                        "height": 0.4,
                    },
                    "polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.6]],
                    "mask_sha256": SHA_C,
                },
                {
                    "frame_id": "frame-2",
                    "image_sha256": SHA_D,
                    "timestamp_seconds": 10.125,
                    "normalized_box": {
                        "x": 0.11,
                        "y": 0.2,
                        "width": 0.3,
                        "height": 0.4,
                    },
                    "polygon": [[0.11, 0.2], [0.41, 0.2], [0.41, 0.6]],
                    "mask_sha256": SHA_E,
                },
            ],
            "proposal_sha256": SHA_B,
            "sam2_output_sha256": SHA_D,
            "sam2_model_manifest_sha256": SHA_E,
        }
    )


def test_ai_box_is_canonical_and_path_free():
    value = _ai_box()

    assert canonical_training_geometry(value) == value
    assert value["geometry_sha256"]
    assert value["normalized_box"]["width"] == 0.3


def test_sam2_mask_preserves_polygon_track_and_provenance():
    value = _sam2_mask()

    assert value["source_kind"] == "user_sam2_refinement"
    assert len(value["tracked_frame_refs"]) == 2
    assert value["mask_sha256"] == SHA_C
    assert value["sam2_model_manifest_sha256"] == SHA_E


def test_digest_tamper_is_rejected():
    value = _ai_box()
    value["normalized_box"]["width"] = 0.2

    with pytest.raises(TrainingGeometryError, match="digest"):
        canonical_training_geometry(value)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        (
            "image_artifact_ref",
            r"C:\private\frame.jpg",
            "path-free",
        ),
        ("proposal_sha256", None, "proposal provenance"),
    ],
)
def test_ai_geometry_rejects_unsafe_or_incomplete_provenance(
    field: str,
    replacement: object,
    message: str,
):
    value = _ai_box()
    value[field] = replacement
    value = {key: item for key, item in value.items() if key != "geometry_sha256"}

    with pytest.raises(TrainingGeometryError, match=message):
        seal_training_geometry(value)


def test_sam2_geometry_requires_mask_and_model_digests():
    value = _sam2_mask()
    value["sam2_model_manifest_sha256"] = None
    value.pop("geometry_sha256")

    with pytest.raises(TrainingGeometryError, match="SAM2 geometry"):
        seal_training_geometry(value)


def test_structured_llm_authority_is_limited_to_delegated_geometry():
    value = _ai_box()
    value["approval_authority"] = "structured_llm_assisted"
    value.pop("geometry_sha256")

    with pytest.raises(TrainingGeometryError, match="disagree"):
        seal_training_geometry(value)

    delegated = copy.deepcopy(value)
    delegated["source_kind"] = "delegated_ai_proposal"
    sealed = seal_training_geometry(delegated)
    assert sealed["approval_authority"] == "structured_llm_assisted"


def test_negative_evidence_cannot_carry_positive_geometry():
    value = _ai_box()
    value["source_kind"] = "negative_evidence"
    value["proposal_sha256"] = None
    value.pop("geometry_sha256")

    with pytest.raises(TrainingGeometryError, match="negative evidence"):
        seal_training_geometry(value)


def _candidate_metadata(*, box: object = None) -> dict:
    return {
        "candidate_id": "1" * 20,
        "normalized_box": box,
        "proposal_provenance_sha256": SHA_B,
        "representative_image": {
            "sha256": SHA_A,
            "artifact_ref": "candidate-evidence:overlay:frame-1",
        },
    }


def test_server_review_builder_freezes_local_and_delegated_boxes():
    box = {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}

    local = training_geometry_from_review(
        candidate_metadata=_candidate_metadata(box=box),
        answer="yes",
        answered_by="local_user",
    )
    delegated = training_geometry_from_review(
        candidate_metadata=_candidate_metadata(box=box),
        answer="yes",
        answered_by="structured_llm_assisted",
    )

    assert local is not None
    assert local["source_kind"] == "accepted_ai_proposal"
    assert delegated is not None
    assert delegated["source_kind"] == "delegated_ai_proposal"
    assert delegated["approval_authority"] == "structured_llm_assisted"


def test_server_review_builder_does_not_invent_missing_box():
    value = training_geometry_from_review(
        candidate_metadata=_candidate_metadata(box=None),
        answer="yes",
        answered_by="local_user",
    )

    assert value is None


def test_server_review_builder_freezes_sam2_polygon():
    value = training_geometry_from_review(
        candidate_metadata=_candidate_metadata(box=None),
        answer="edit",
        answered_by="local_user",
        sam2_refinement={
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            },
            "polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.6]],
            "mask_artifact_ref": "sam2mask_1234567890abcdef12345678",
            "output_sha256": SHA_D,
            "model_manifest": {"manifest_sha256": SHA_E},
        },
    )

    assert value is not None
    assert value["source_kind"] == "user_sam2_refinement"
    assert value["polygon"][2] == [0.4, 0.6]
    assert value["tracked_frame_refs"] == []


def test_server_review_builder_preserves_negative_image_truth():
    value = training_geometry_from_review(
        candidate_metadata=_candidate_metadata(box=None),
        answer="no",
        answered_by="local_user",
    )

    assert value is not None
    assert value["source_kind"] == "negative_evidence"
    assert value["normalized_box"] is None

    delegated = training_geometry_from_review(
        candidate_metadata=_candidate_metadata(box=None),
        answer="no",
        answered_by="structured_llm_assisted",
    )
    assert delegated is not None
    assert delegated["approval_authority"] == "structured_llm_assisted"
