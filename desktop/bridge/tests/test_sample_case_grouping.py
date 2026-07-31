from __future__ import annotations

from collections import defaultdict
from uuid import uuid4

import pytest

from app.weak_case_approvals import (
    WeakCaseApprovalError,
    WeakCaseApprovalStore,
)
from app.weak_case_catalog import (
    WEAK_CASE_VISUAL_SOURCE_SCHEMA,
    WeakCaseCatalog,
    default_sample_root,
)


@pytest.fixture(scope="module")
def actual_catalog():
    return WeakCaseCatalog(
        default_sample_root()
    )


@pytest.fixture(scope="module")
def actual_snapshot(actual_catalog):
    return actual_catalog.visual_index_snapshot()


def test_actual_samples_preserve_272_photos_as_270_review_events(
    actual_snapshot,
):
    snapshot = actual_snapshot

    assert snapshot["schema_version"] == (
        WEAK_CASE_VISUAL_SOURCE_SCHEMA
    )
    assert snapshot["case_count"] == 272
    assert snapshot["evidence_group_count"] == 270
    assert snapshot["duplicate_image_group_count"] == 1
    assert snapshot["duplicate_image_case_count"] == 2
    groups = defaultdict(list)
    images = defaultdict(list)
    for case in snapshot["cases"]:
        groups[case["sample_evidence_group_id"]].append(case)
        images[case["image_sha256"]].append(case)

    multi_photo_groups = [
        rows for rows in groups.values() if len(rows) > 1
    ]
    assert len(multi_photo_groups) == 2
    assert {
        rows[0]["caption"] for rows in multi_photo_groups
    } == {
        "덕소삼패IC 합류부 차선변경금지봉 파손",
        "LCS 고장구간 다수 발견",
    }
    assert all(
        len({row["photo_slot_id"] for row in rows}) == 2
        and len({row["image_sha256"] for row in rows}) == 2
        and {row["group_photo_index"] for row in rows}
        == {1, 2}
        and {row["group_photo_count"] for row in rows}
        == {2}
        for rows in multi_photo_groups
    )

    duplicate_images = [
        rows for rows in images.values() if len(rows) > 1
    ]
    assert len(duplicate_images) == 1
    duplicate_rows = duplicate_images[0]
    assert duplicate_rows[0]["image_sha256"] == (
        "6ca896b529f99351da2ba1b0af32c28a"
        "eea941e659af2a1330b2921b94925232"
    )
    expected_refs = sorted(
        f"{row['source_id']}/{row['case_id']}"
        for row in duplicate_rows
    )
    assert all(
        row["duplicate_image_occurrence_count"] == 2
        and row["duplicate_image_review_required"] is True
        and row["duplicate_image_registry_complete"] is True
        and row["duplicate_image_case_refs"] == expected_refs
        for row in duplicate_rows
    )
    assert all(
        case["reference_state"] == "weak_unconfirmed"
        and case["review_required"] is True
        and case["approved_reference"] is False
        and case["training_eligible"] is False
        for case in snapshot["cases"]
    )


def test_actual_duplicate_image_requires_explicit_duplicate_review(
    actual_catalog,
    actual_snapshot,
    tmp_path,
):
    duplicate = next(
        case
        for case in actual_snapshot["cases"]
        if case["duplicate_image_review_required"]
    )
    project_id = uuid4()
    store = WeakCaseApprovalStore(
        tmp_path.resolve(),
        actual_catalog,
    )
    request = {
        "source_id": duplicate["source_id"],
        "case_id": duplicate["case_id"],
        "expected_document_sha256": duplicate[
            "document_sha256"
        ],
        "expected_index_manifest_sha256": duplicate[
            "index_manifest_sha256"
        ],
        "expected_image_sha256": duplicate["image_sha256"],
        "issue_type": "road_debris",
        "maintenance_action": "낙하물 청소",
        "caption": duplicate["caption"],
        "route": duplicate["route"],
        "location": duplicate["location"],
        "evaluation": duplicate["evaluation"],
        "opinion": duplicate["opinion"],
        "user_confirmed": True,
    }

    with pytest.raises(WeakCaseApprovalError) as caught:
        store.approve(project_id, request)
    assert caught.value.code == (
        "duplicate_image_confirmation_required"
    )

    approved = store.approve(
        project_id,
        {
            **request,
            "duplicate_image_review_confirmed": True,
        },
    )
    assert approved["schema_version"] == (
        "trafficvisson.approved-reference.v2"
    )
    assert approved["duplicate_image_occurrence_count"] == 2
    assert approved["duplicate_image_review_required"] is True
    assert approved["duplicate_image_review_confirmed"] is True
    assert approved["sample_evidence_group_id"] == duplicate[
        "sample_evidence_group_id"
    ]
