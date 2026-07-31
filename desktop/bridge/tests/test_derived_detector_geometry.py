from __future__ import annotations

import copy
from uuid import uuid4

import pytest

from app.derived_detector_geometry import (
    DerivedDetectorGeometryError,
    canonical_derived_detector_geometry_sample,
    derive_detector_geometry_samples,
)
from app.training_geometry import (
    SCHEMA_VERSION,
    TRACKED_FRAME_BINDING_SCHEMA,
    TrainingGeometryError,
    seal_training_geometry,
)


def _geometry(*, bound: bool = True) -> dict:
    refs = []
    for index, timestamp in enumerate((10.25, 10.75), start=1):
        row = {
            "frame_id": f"track-frame-{index}",
            "image_sha256": str(index + 1) * 64,
            "timestamp_seconds": timestamp,
            "normalized_box": {
                "x": round(0.1 + index / 100, 2),
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            },
            "polygon": None,
            "mask_sha256": None,
        }
        if bound:
            row = {
                "binding_schema": TRACKED_FRAME_BINDING_SCHEMA,
                "evidence_candidate_id": f"{index + 1}" * 20,
                "image_artifact_ref": (
                    f"candidate-evidence:overlay:track-{index}"
                ),
                **row,
            }
        refs.append(row)
    return seal_training_geometry(
        {
            "schema_version": SCHEMA_VERSION,
            "source_kind": "user_sam2_refinement",
            "approval_authority": "local_user",
            "image_sha256": "1" * 64,
            "image_artifact_ref": "candidate-evidence:overlay:representative",
            "normalized_box": {
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            },
            "polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.6]],
            "mask_artifact_ref": "sam2-mask:approved",
            "mask_sha256": "a" * 64,
            "tracked_frame_refs": refs,
            "proposal_sha256": "b" * 64,
            "sam2_output_sha256": "c" * 64,
            "sam2_model_manifest_sha256": "d" * 64,
        }
    )


def _annotation(*, split: str = "train", bound: bool = True) -> dict:
    return {
        "annotation_id": str(uuid4()),
        "project_id": str(uuid4()),
        "analysis_job_id": str(uuid4()),
        "candidate_id": "f" * 20,
        "split": split,
        "partition": "approved",
        "include_in_training": True,
        "selected_issue_type": "pothole",
        "training_geometry": _geometry(bound=bound),
    }


def test_bound_sam2_track_expands_fit_only_without_gate_credit():
    rows = derive_detector_geometry_samples(
        _annotation(),
        include_tracked_fit_frames=True,
    )

    assert len(rows) == 3
    assert rows[0].source_frame_kind == "representative"
    assert {
        row.event_group_id for row in rows
    } == {rows[0].event_group_id}
    assert {
        row.leakage_group_sha256 for row in rows
    } == {rows[0].leakage_group_sha256}
    assert {row.split for row in rows} == {"train"}
    assert all(row.counts_toward_gate is False for row in rows)
    assert all(row.fit_only is True for row in rows)
    assert all("path" not in row.canonical_value() for row in rows)
    assert all(
        canonical_derived_detector_geometry_sample(
            row.canonical_value()
        )
        == row.canonical_value()
        for row in rows
    )


def test_validation_and_legacy_unbound_tracks_never_expand():
    validation = derive_detector_geometry_samples(
        _annotation(split="validation"),
        include_tracked_fit_frames=True,
    )
    legacy = derive_detector_geometry_samples(
        _annotation(bound=False),
        include_tracked_fit_frames=True,
    )

    assert len(validation) == 1
    assert len(legacy) == 1
    assert validation[0].source_frame_kind == "representative"
    assert legacy[0].source_frame_kind == "representative"


def test_same_event_identity_is_inherited_and_not_split_dependent():
    train = _annotation()
    validation = {
        **train,
        "annotation_id": str(uuid4()),
        "split": "validation",
    }
    train_rows = derive_detector_geometry_samples(
        train,
        include_tracked_fit_frames=True,
    )
    validation_rows = derive_detector_geometry_samples(
        validation,
        include_tracked_fit_frames=False,
    )

    assert train_rows[0].event_group_id == validation_rows[0].event_group_id
    assert (
        train_rows[0].leakage_group_sha256
        == validation_rows[0].leakage_group_sha256
    )


def test_derived_digest_tamper_fails_closed():
    row = derive_detector_geometry_samples(
        _annotation(),
        include_tracked_fit_frames=True,
    )[1].canonical_value()
    changed = copy.deepcopy(row)
    changed["event_group_id"] = "event_" + "0" * 32
    with pytest.raises(
        DerivedDetectorGeometryError,
        match="event/leakage|digest",
    ):
        canonical_derived_detector_geometry_sample(changed)


@pytest.mark.parametrize(
    "artifact_ref",
    [
        "candidate/../other.jpg",
        "/absolute/frame.jpg",
        "C:/private/frame.jpg",
        r"candidate\frame.jpg",
    ],
)
def test_bound_track_artifact_refs_are_strictly_path_free(artifact_ref):
    raw = _geometry()["tracked_frame_refs"][0]
    unsafe = {
        **raw,
        "image_artifact_ref": artifact_ref,
    }
    geometry = _geometry()
    geometry.pop("geometry_sha256")
    geometry["tracked_frame_refs"] = [
        unsafe,
        *geometry["tracked_frame_refs"][1:],
    ]
    with pytest.raises(TrainingGeometryError, match="path-free"):
        seal_training_geometry(geometry)


def test_track_timestamp_order_and_identity_duplicates_are_rejected():
    geometry = _geometry()
    geometry.pop("geometry_sha256")
    geometry["tracked_frame_refs"] = list(
        reversed(geometry["tracked_frame_refs"])
    )
    with pytest.raises(TrainingGeometryError, match="ordered"):
        seal_training_geometry(geometry)

    geometry = _geometry()
    geometry.pop("geometry_sha256")
    duplicate = copy.deepcopy(geometry["tracked_frame_refs"][0])
    duplicate["timestamp_seconds"] = 11.0
    geometry["tracked_frame_refs"][1] = duplicate
    with pytest.raises(TrainingGeometryError, match="duplicates"):
        seal_training_geometry(geometry)
