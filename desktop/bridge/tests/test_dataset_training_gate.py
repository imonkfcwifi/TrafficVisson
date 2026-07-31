from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.dataset_training_gate import build_dataset_training_gate
from app.dataset_versions import DatasetVersionError
from test_project_catalog_v3 import _catalog_with_jobs, _chain_records


def _membership(
    index: int,
    *,
    split: str,
    issue_type: str | None,
    partition: str = "approved",
    answered_by: str = "local_user",
):
    annotation_id = str(uuid4())
    question_id = str(uuid4())
    structured = answered_by == "structured_llm_assisted"
    return {
        "annotation_id": annotation_id,
        "question_id": question_id,
        "include_in_training": True,
        "split": split,
        "partition": partition,
        "selected_issue_type": issue_type,
        "annotation_json": json.dumps(
            {
                "annotation_id": annotation_id,
                "question_id": question_id,
                "answered_by": answered_by,
                "label_source": (
                    "structured_llm_assisted"
                    if structured
                    else "local_user_review"
                ),
                "source_kind": (
                    "structured_llm_assisted"
                    if structured
                    else "current_video_confirmed"
                ),
                "training_authorized": True,
                "user_confirmed": not structured,
                "sequence": index,
            },
            sort_keys=True,
        ),
    }


def test_gate_counts_only_unique_current_video_train_and_local_validation():
    dataset_version_id = str(uuid4())
    rows = [
        *[
            _membership(
                index,
                split="train",
                issue_type="pothole",
            )
            for index in range(99)
        ],
        *[
            _membership(
                100 + index,
                split="validation",
                issue_type="pothole",
            )
            for index in range(30)
        ],
    ]
    references = [
        {
            "issue_type": "pothole",
            "approved_reference_member_sha256": f"{index:064x}",
        }
        for index in range(50)
    ]

    gate = build_dataset_training_gate(
        dataset_version_id=dataset_version_id,
        dataset_hash="a" * 64,
        membership_rows=rows,
        approved_reference_members=references,
    )

    assert gate["current_video_train_unique_annotation_count"] == 99
    assert gate["current_video_train_unique_question_count"] == 99
    assert gate["approved_reference_member_count"] == 50
    assert gate["approved_references_count_toward_training_gate"] is False
    assert (
        gate["approved_references_count_toward_fixed_validation"]
        is False
    )
    assert gate["fixed_validation_type_statuses"] == [
        {
            "issue_type": "pothole",
            "sample_count": 30,
            "positive_count": 30,
            "required_sample_count": 30,
            "ready": True,
        }
    ]
    assert gate["ready_for_explicit_training"] is False

    rows.append(
        _membership(
            200,
            split="train",
            issue_type="pothole",
        )
    )
    ready = build_dataset_training_gate(
        dataset_version_id=dataset_version_id,
        dataset_hash="b" * 64,
        membership_rows=rows,
        approved_reference_members=references,
    )
    assert ready["current_video_train_unique_annotation_count"] == 100
    assert ready["ready_for_explicit_training"] is True
    assert ready["gate_snapshot_sha256"] != gate["gate_snapshot_sha256"]


def test_gate_never_accepts_structured_llm_as_fixed_validation():
    rows = [
        _membership(
            0,
            split="validation",
            issue_type="pothole",
            answered_by="structured_llm_assisted",
        )
    ]

    with pytest.raises(
        DatasetVersionError,
        match="local-user-confirmed",
    ):
        build_dataset_training_gate(
            dataset_version_id=str(uuid4()),
            dataset_hash="c" * 64,
            membership_rows=rows,
            approved_reference_members=[],
        )


def test_gate_rejects_duplicate_question_credit():
    first = _membership(
        0,
        split="train",
        issue_type="pothole",
    )
    second = _membership(
        1,
        split="train",
        issue_type="pothole",
    )
    second["question_id"] = first["question_id"]

    with pytest.raises(
        DatasetVersionError,
        match="duplicated",
    ):
        build_dataset_training_gate(
            dataset_version_id=str(uuid4()),
            dataset_hash="d" * 64,
            membership_rows=[first, second],
            approved_reference_members=[],
        )


def test_catalog_creation_and_list_replay_the_same_gate_snapshot(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1, 1],
    )
    records = _chain_records(
        project_id,
        fixtures,
        [
            (fixtures[0], 0, "yes"),
            (fixtures[1], 0, "no"),
        ],
    )
    catalog.project_label_records(project_id, records)

    created = catalog.create_dataset_version(project_id)
    listed = catalog.list_dataset_versions(project_id)

    assert len(listed) == 1
    assert listed[0]["dataset_version_id"] == (
        created["dataset_version_id"]
    )
    assert listed[0]["training_gate"] == created["training_gate"]
    assert (
        created["training_gate"][
            "current_video_train_unique_annotation_count"
        ]
        == created["training_gate_member_count"]
    )
    assert created["training_gate"]["ready_for_explicit_training"] is False
