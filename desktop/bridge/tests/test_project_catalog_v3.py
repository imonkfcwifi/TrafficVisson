import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

import pytest

from app.dataset_versions import (
    DATASET_SCHEMA_VERSION,
    DatasetVersionError,
    LabelProjectionError,
    canonical_json_bytes,
)
from app.project_catalog import CATALOG_SCHEMA_VERSION, ProjectCatalog
from test_project_catalog_v2 import (
    _project,
    _rows,
    _write_completed_analysis,
)
from structured_assist_fixtures import assist_provenance


PARTITION = {
    "yes": "approved",
    "edit": "approved",
    "no": "negative",
    "unsure": "held",
    "skipped": "excluded",
}
TRAINING = {
    "yes": True,
    "edit": True,
    "no": True,
    "unsure": False,
    "skipped": False,
}


def _seed_v2_database(database_path, project):
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(database_path), isolation_level=None) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        ProjectCatalog._apply_migration_v1(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
            ("v1",),
        )
        ProjectCatalog._apply_migration_v2(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
            ("v2",),
        )
        connection.execute(
            """
            INSERT INTO projects(
                id, name, description, question_policy,
                active_model_version, created_at, fixture
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(project["id"]),
                project["name"],
                project["description"],
                project["question_policy"],
                project["active_model_version"],
                project["created_at"].isoformat(),
                1 if project["fixture"] else 0,
            ),
        )
        connection.execute("COMMIT")


def _answer_record(
    *,
    project_id,
    job_id,
    candidate,
    answer,
    revision=1,
    previous_hash=None,
    record_id=None,
    recorded_offset=0,
    answered_by="local_user",
    assist_provenance_value=None,
):
    candidate_id = candidate["candidate_id"]
    if answer in {"yes", "edit"}:
        issue_type = "pothole"
        severity = "moderate"
        action = "포트홀 보수"
        box = (
            {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}
            if answer == "edit"
            else None
        )
    else:
        issue_type = severity = action = box = None
    final_answer = {
        "answer": answer,
        "selected_issue_type": issue_type,
        "severity": severity,
        "maintenance_action": action,
        "ocr_text": None,
        "normalized_box": box,
        "include_in_training": TRAINING[answer],
        "answered_by": answered_by,
    }
    record_without_hash = {
        "schema_version": (
            "1.2" if assist_provenance_value is not None else "1.0"
        ),
        "record_id": str(record_id or uuid4()),
        "project_id": str(project_id),
        "analysis_job_id": str(job_id),
        "candidate_id": candidate_id,
        "question_id": str(uuid5(UUID(str(job_id)), candidate_id)),
        "answer_revision": revision,
        "partition": PARTITION[answer],
        "ai_original_candidate": candidate,
        "final_answer": final_answer,
        "recorded_at": (
            datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc)
            + timedelta(seconds=recorded_offset)
        )
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "previous_record_hash": previous_hash,
    }
    if assist_provenance_value is not None:
        record_without_hash["training_geometry"] = None
        record_without_hash["assist_provenance"] = (
            assist_provenance_value
        )
    return {
        **record_without_hash,
        "record_hash": hashlib.sha256(
            canonical_json_bytes(record_without_hash)
        ).hexdigest(),
    }


def _catalog_with_jobs(tmp_path, *, project_id, job_candidate_counts):
    fixtures = [
        _write_completed_analysis(
            tmp_path,
            project_id=project_id,
            source_seed=f"source-{index}",
            candidate_count=candidate_count,
        )
        for index, candidate_count in enumerate(job_candidate_counts)
    ]
    catalog = ProjectCatalog(
        tmp_path / "catalog.sqlite3",
        _project(project_id=project_id),
    )
    return catalog, fixtures


def _chain_records(project_id, fixtures, answers):
    records = []
    previous = None
    offset = 0
    for fixture, candidate_index, answer in answers:
        record = _answer_record(
            project_id=project_id,
            job_id=fixture["job_id"],
            candidate=fixture["payload"]["result"]["candidates"][
                candidate_index
            ],
            answer=answer,
            previous_hash=previous,
            recorded_offset=offset,
        )
        records.append(record)
        previous = record["record_hash"]
        offset += 1
    return records


def test_v2_to_v3_migration_preserves_rows_and_adds_normalized_contracts(
    tmp_path,
):
    project = _project()
    database_path = tmp_path / "catalog.sqlite3"
    _seed_v2_database(database_path, project)
    before = _rows(
        database_path,
        "SELECT * FROM projects WHERE id = ?",
        (str(project["id"]),),
    )

    ProjectCatalog(database_path, project)

    assert _rows(
        database_path,
        "SELECT version FROM schema_migrations ORDER BY version",
    ) == [
        {"version": version}
        for version in range(1, CATALOG_SCHEMA_VERSION + 1)
    ]
    assert _rows(
        database_path,
        "SELECT * FROM projects WHERE id = ?",
        (str(project["id"]),),
    ) == before
    table_names = {
        row["name"]
        for row in _rows(
            database_path,
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    assert {
        "label_questions",
        "label_answers",
        "annotations",
        "dataset_versions",
        "dataset_memberships",
    }.issubset(table_names)
    triggers = {
        row["name"]
        for row in _rows(
            database_path,
            "SELECT name FROM sqlite_master WHERE type = 'trigger'",
        )
    }
    assert {
        "immutable_dataset_versions_update",
        "immutable_dataset_versions_delete",
        "immutable_dataset_memberships_update",
        "immutable_dataset_memberships_delete",
    }.issubset(triggers)


def test_jsonl_projection_is_idempotent_partitioned_and_restart_safe(tmp_path):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1, 1, 1, 1, 1],
    )
    records = _chain_records(
        project_id,
        fixtures,
        [
            (fixtures[0], 0, "yes"),
            (fixtures[1], 0, "edit"),
            (fixtures[2], 0, "no"),
            (fixtures[3], 0, "unsure"),
            (fixtures[4], 0, "skipped"),
        ],
    )

    first = catalog.project_label_records(project_id, records)
    replay = catalog.project_label_records(project_id, records)

    assert [row["created"] for row in first] == [True] * 5
    assert [row["created"] for row in replay] == [False] * 5
    assert [row["partition"] for row in first] == [
        "approved",
        "approved",
        "negative",
        "held",
        "excluded",
    ]
    assert [row["include_in_training"] for row in first] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert catalog.current_label_stats(project_id) == {
        "project_id": str(project_id),
        "question_count": 5,
        "answer_revision_count": 5,
        "current_answer_count": 5,
        "training_included_count": 3,
        "source_analysis_job_count": 5,
        "partitions": {
            "approved": 2,
            "negative": 1,
            "held": 1,
            "excluded": 1,
        },
    }
    assert len(catalog.list_label_questions(project_id)) == 5
    assert len(catalog.list_label_answers(project_id)) == 5
    assert len(catalog.list_current_annotations(project_id)) == 5

    restarted = ProjectCatalog(
        tmp_path / "catalog.sqlite3",
        _project(project_id=project_id),
    )
    assert restarted.current_label_stats(project_id) == (
        catalog.current_label_stats(project_id)
    )
    assert restarted.list_label_answers(project_id) == records
    assert len(
        _rows(
            tmp_path / "catalog.sqlite3",
            "SELECT * FROM label_answers",
        )
    ) == 5
    assert len(
        _rows(
            tmp_path / "catalog.sqlite3",
            "SELECT * FROM annotations",
        )
    ) == 5


def test_pipeline_question_registration_is_idempotent_and_unanswered(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[2],
    )
    fixture = fixtures[0]
    job_id = fixture["job_id"]
    candidates = fixture["payload"]["result"]["candidates"]
    questions = [
        {
            "question_id": str(
                uuid5(UUID(str(job_id)), candidate["candidate_id"])
            ),
            "candidate_id": candidate["candidate_id"],
            "ai_original_candidate": candidate,
        }
        for candidate in candidates
    ]

    first = catalog.register_label_questions(
        project_id, job_id, fixture["source_sha256"], questions
    )
    replay = catalog.register_label_questions(
        project_id, job_id, fixture["source_sha256"], questions
    )

    assert first["question_count"] == 2
    assert first["created_count"] == 2
    assert replay["question_count"] == 2
    assert replay["created_count"] == 0
    assert replay["question_set_sha256"] == first[
        "question_set_sha256"
    ]
    assert catalog.current_label_stats(project_id) == {
        "project_id": str(project_id),
        "question_count": 2,
        "answer_revision_count": 0,
        "current_answer_count": 0,
        "training_included_count": 0,
        "source_analysis_job_count": 0,
        "partitions": {
            "approved": 0,
            "negative": 0,
            "held": 0,
            "excluded": 0,
        },
    }
    assert len(
        catalog.list_label_questions(
            project_id, analysis_job_id=job_id
        )
    ) == 2
    assert catalog.list_label_answers(project_id) == []
    assert catalog.list_current_annotations(project_id) == []
    assert catalog.list_incidents(
        project_id, analysis_job_id=job_id
    ) == []


def test_dataset_hash_reuse_revision_change_and_job_grain_split(tmp_path):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[2, 2, 1],
    )
    records = _chain_records(
        project_id,
        fixtures,
        [
            (fixtures[0], 0, "yes"),
            (fixtures[0], 1, "no"),
            (fixtures[1], 0, "yes"),
            (fixtures[1], 1, "no"),
            (fixtures[2], 0, "unsure"),
        ],
    )
    catalog.project_label_records(project_id, records)

    first = catalog.create_dataset_version(project_id)
    reused = catalog.create_dataset_version(project_id)

    assert first["schema_version"] == DATASET_SCHEMA_VERSION
    assert first["reused"] is False
    assert reused["reused"] is True
    assert reused["dataset_version_id"] == first["dataset_version_id"]
    assert reused["dataset_hash"] == first["dataset_hash"]
    assert first["source_answer_count"] == 5
    assert first["training_member_count"] == 4
    assert first["excluded_count"] == 1
    assert first["train_count"] == 2
    assert first["validation_count"] == 2
    splits_by_job = {}
    for member in first["memberships"]:
        if member["include_in_training"]:
            splits_by_job.setdefault(member["analysis_job_id"], set()).add(
                member["split"]
            )
        else:
            assert member["split"] == "excluded"
    assert set(map(frozenset, splits_by_job.values())) == {
        frozenset({"train"}),
        frozenset({"validation"}),
    }
    assert all(len(values) == 1 for values in splits_by_job.values())
    immutable_first_members = json.dumps(
        first["memberships"], ensure_ascii=False, sort_keys=True
    )

    changed = _answer_record(
        project_id=project_id,
        job_id=fixtures[0]["job_id"],
        candidate=fixtures[0]["payload"]["result"]["candidates"][0],
        answer="edit",
        revision=2,
        previous_hash=records[-1]["record_hash"],
        recorded_offset=10,
    )
    catalog.project_label_record(project_id, changed)
    second = catalog.create_dataset_version(project_id)

    assert second["dataset_version_id"] != first["dataset_version_id"]
    assert second["dataset_hash"] != first["dataset_hash"]
    versions = catalog.list_dataset_versions(project_id)
    assert len(versions) == 2
    assert json.dumps(
        versions[0]["memberships"], ensure_ascii=False, sort_keys=True
    ) == immutable_first_members
    assert {
        member["answer_revision"]
        for member in second["memberships"]
        if member["question_id"] == changed["question_id"]
    } == {2}

    restarted = ProjectCatalog(
        tmp_path / "catalog.sqlite3",
        _project(project_id=project_id),
    )
    after_restart = restarted.create_dataset_version(project_id)
    assert after_restart["reused"] is True
    assert after_restart["dataset_hash"] == second["dataset_hash"]


def test_structured_llm_jobs_are_training_only_not_fixed_validation(tmp_path):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1, 1, 1],
    )
    records = []
    previous = None
    for index, fixture in enumerate(fixtures):
        record = _answer_record(
            project_id=project_id,
            job_id=fixture["job_id"],
            candidate=fixture["payload"]["result"]["candidates"][0],
            answer="yes",
            previous_hash=previous,
            recorded_offset=index,
            answered_by=(
                "structured_llm_assisted"
                if index == 0
                else "local_user"
            ),
            assist_provenance_value=(
                assist_provenance(
                    project_id,
                    fixture["job_id"],
                    fixture["payload"]["result"]["candidates"][0][
                        "candidate_id"
                    ],
                )
                if index == 0
                else None
            ),
        )
        records.append(record)
        previous = record["record_hash"]
    catalog.project_label_records(project_id, records)

    dataset = catalog.create_dataset_version(project_id)
    splits = {
        member["analysis_job_id"]: member["split"]
        for member in dataset["memberships"]
    }

    assert splits[str(fixtures[0]["job_id"])] == "train"
    assert set(splits.values()) == {"train", "validation"}
    assert dataset["training_member_count"] == 3
    assert dataset["training_gate_member_count"] == 2
    assert (
        dataset["approved_references_count_toward_training_gate"]
        is False
    )


def test_next_dataset_keeps_promoted_fixed_validation_and_trains_survey_feedback(
    tmp_path,
):
    """FP/FN survey feedback must not silently re-split held-out trips."""

    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1, 1, 1, 1],
    )
    initial_records = _chain_records(
        project_id,
        fixtures[:3],
        [
            (fixtures[0], 0, "yes"),
            (fixtures[1], 0, "no"),
            (fixtures[2], 0, "yes"),
        ],
    )
    catalog.project_label_records(project_id, initial_records)
    initial = catalog.create_dataset_version(project_id)
    fixed_job_ids = sorted(
        {
            member["analysis_job_id"]
            for member in initial["memberships"]
            if member["split"] == "validation"
        }
    )
    assert fixed_job_ids

    survey_record = _answer_record(
        project_id=project_id,
        job_id=fixtures[3]["job_id"],
        candidate=fixtures[3]["payload"]["result"]["candidates"][0],
        answer="no",
        previous_hash=initial_records[-1]["record_hash"],
        recorded_offset=20,
    )
    catalog.project_label_record(project_id, survey_record)
    next_dataset = catalog.create_dataset_version(
        project_id,
        fixed_validation_analysis_job_ids=fixed_job_ids,
    )

    assert next_dataset["dataset_version_id"] != (
        initial["dataset_version_id"]
    )
    assert next_dataset["split_strategy"] == (
        "project_fixed_analysis_job_v1"
    )
    assert {
        member["analysis_job_id"]
        for member in next_dataset["memberships"]
        if member["split"] == "validation"
    } == set(fixed_job_ids)
    survey_members = [
        member
        for member in next_dataset["memberships"]
        if member["analysis_job_id"]
        == str(fixtures[3]["job_id"])
    ]
    assert len(survey_members) == 1
    assert survey_members[0]["partition"] == "negative"
    assert survey_members[0]["split"] == "train"
    assert survey_members[0]["include_in_training"] is True


def test_next_dataset_fails_closed_when_fixed_validation_truth_changed(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1, 1],
    )
    initial_records = _chain_records(
        project_id,
        fixtures,
        [
            (fixtures[0], 0, "yes"),
            (fixtures[1], 0, "no"),
        ],
    )
    catalog.project_label_records(project_id, initial_records)
    initial = catalog.create_dataset_version(project_id)
    fixed_job_id = next(
        member["analysis_job_id"]
        for member in initial["memberships"]
        if member["split"] == "validation"
    )
    fixed_fixture = next(
        fixture
        for fixture in fixtures
        if str(fixture["job_id"]) == fixed_job_id
    )
    changed = _answer_record(
        project_id=project_id,
        job_id=fixed_fixture["job_id"],
        candidate=fixed_fixture["payload"]["result"]["candidates"][0],
        answer="unsure",
        revision=2,
        previous_hash=initial_records[-1]["record_hash"],
        recorded_offset=30,
    )
    catalog.project_label_record(project_id, changed)

    with pytest.raises(
        DatasetVersionError,
        match="fixed validation evidence is unavailable or changed",
    ):
        catalog.create_dataset_version(
            project_id,
            fixed_validation_analysis_job_ids=[fixed_job_id],
        )


def test_project_mixing_and_foreign_key_crossing_are_rejected(tmp_path):
    project_a = uuid4()
    project_b = uuid4()
    fixture_a = _write_completed_analysis(
        tmp_path,
        project_id=project_a,
        source_seed="project-a",
    )
    fixture_b = _write_completed_analysis(
        tmp_path,
        project_id=project_b,
        source_seed="project-b",
    )
    catalog = ProjectCatalog(
        tmp_path / "catalog.sqlite3",
        _project(project_id=project_a),
    )
    record_a = _chain_records(
        project_a, [fixture_a], [(fixture_a, 0, "yes")]
    )[0]
    record_b = _chain_records(
        project_b, [fixture_b], [(fixture_b, 0, "no")]
    )[0]

    with pytest.raises(LabelProjectionError, match="project boundary"):
        catalog.project_label_record(project_a, record_b)

    forged_cross_job = _answer_record(
        project_id=project_a,
        job_id=fixture_b["job_id"],
        candidate=fixture_b["payload"]["result"]["candidates"][0],
        answer="yes",
    )
    with pytest.raises(LabelProjectionError, match="completed job"):
        catalog.project_label_record(project_a, forged_cross_job)

    catalog.project_label_record(project_a, record_a)
    catalog.project_label_record(project_b, record_b)
    dataset_a = catalog.create_dataset_version(project_a)
    dataset_b = catalog.create_dataset_version(project_b)
    assert {
        member["analysis_job_id"] for member in dataset_a["memberships"]
    } == {str(fixture_a["job_id"])}
    assert {
        member["analysis_job_id"] for member in dataset_b["memberships"]
    } == {str(fixture_b["job_id"])}

    with sqlite3.connect(str(tmp_path / "catalog.sqlite3")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO dataset_memberships(
                    dataset_version_id, project_id, annotation_id,
                    member_order, split, include_in_training,
                    source_record_hash, annotation_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_a["dataset_version_id"],
                    str(project_b),
                    dataset_b["memberships"][0]["annotation_id"],
                    99,
                    "train",
                    1,
                    "a" * 64,
                    "b" * 64,
                ),
            )


def test_dataset_requires_current_labels_and_rejects_chain_gaps(tmp_path):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1],
    )
    with pytest.raises(DatasetVersionError, match="current label"):
        catalog.create_dataset_version(project_id)

    revision_two = _answer_record(
        project_id=project_id,
        job_id=fixtures[0]["job_id"],
        candidate=fixtures[0]["payload"]["result"]["candidates"][0],
        answer="no",
        revision=2,
    )
    with pytest.raises(LabelProjectionError, match="revision must be 1"):
        catalog.project_label_record(project_id, revision_two)


def _approved_reference_record(
    project_id,
    *,
    seed="one",
    revision=1,
    image_sha256=None,
    group_seed=None,
    group_photo_index=1,
    group_photo_count=1,
):
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    group_digest = hashlib.sha256(
        (group_seed or seed).encode("utf-8")
    ).hexdigest()
    source_id = f"sample-hwpx-3q-{digest[:16]}"
    case_id = f"hwpx-weak-case-{digest[:32]}"
    table_id = f"hwpx-table-{digest[:24]}"
    photo_cell_id = f"hwpx-cell-{digest[:24]}"
    semantic_cells = [
        {
            "role": role,
            "cell_target_id": (
                "hwpx-cell-"
                + hashlib.sha256(
                    f"{seed}:{role}".encode("utf-8")
                ).hexdigest()[:24]
            ),
            "table_id": table_id,
            "table_index": 1,
            "page_number": 2 if role == "photo_caption" else 1,
            "row": index,
            "column": 0,
            "row_span": 1,
            "column_span": 1,
            "text_excerpt": role,
            "text_sha256": hashlib.sha256(
                role.encode("utf-8")
            ).hexdigest(),
        }
        for index, role in enumerate(
            (
                "photo_caption",
                "inspection_route",
                "inspection_opinion_label",
                "inspection_opinion_body",
            )
        )
    ]
    square_markers = [
        {
            "cell_target_id": (
                "hwpx-cell-"
                + hashlib.sha256(
                    f"{seed}:marker".encode("utf-8")
                ).hexdigest()[:24]
            ),
            "table_id": table_id,
            "table_index": 1,
            "page_number": 1,
            "row": 4,
            "column": 0,
            "marker": "■",
            "text_sha256": hashlib.sha256(
                f"{seed}:marker-text".encode("utf-8")
            ).hexdigest(),
        }
    ]
    value = {
        "schema_version": "trafficvisson.approved-reference.v2",
        "approval_id": f"approved-reference-{digest[:24]}",
        "project_id": str(project_id),
        "source_id": source_id,
        "case_id": case_id,
        "revision": revision,
        "reference_state": "approved_reference",
        "approved_reference": True,
        "training_eligible": True,
        "training_scope": "approved_reference_only",
        "annotation_created": False,
        "incident_created": False,
        "document_sha256": hashlib.sha256(
            f"document:{seed}".encode("utf-8")
        ).hexdigest(),
        "document_name": "3분기 점검 내용 정리.hwpx",
        "structure_target_summary_sha256": hashlib.sha256(
            f"structure:{seed}".encode("utf-8")
        ).hexdigest(),
        "index_manifest_sha256": hashlib.sha256(
            f"index:{seed}".encode("utf-8")
        ).hexdigest(),
        "image_sha256": (
            image_sha256
            or hashlib.sha256(
                f"image:{seed}".encode("utf-8")
            ).hexdigest()
        ),
        "image_media_type": "image/jpeg",
        "image_width": 1920,
        "image_height": 1080,
        "quarter": 3,
        "section_path": "Contents/section0.xml",
        "section_index": 0,
        "picture_target_id": f"hwpx-picture-{digest[:24]}",
        "picture_index": 1,
        "picture_xml_id": "1",
        "table_id": table_id,
        "table_index": 1,
        "cell_target_id": photo_cell_id,
        "cell_row": 0,
        "cell_column": 0,
        "page_hint": 2,
        "page_hint_source": "fixture",
        "photo_page_number": 2,
        "inspection_page_number": 1,
        "sample_layout_binding_schema": (
            "trafficvisson.hwpx-sample-layout.v2"
        ),
        "sample_layout_binding_revision": 2,
        "sample_layout_binding_sha256": hashlib.sha256(
            f"binding:{seed}".encode("utf-8")
        ).hexdigest(),
        "semantic_projection_revision": (
            "audited-photo-caption-evidence-group-v2"
        ),
        "semantic_attestation_sha256": hashlib.sha256(
            f"semantic:{seed}".encode("utf-8")
        ).hexdigest(),
        "photo_slot_id": f"sample-photo-slot-{digest[:24]}",
        "sample_evidence_group_id": (
            f"sample-evidence-group-{group_digest[:24]}"
        ),
        "group_photo_index": group_photo_index,
        "group_photo_count": group_photo_count,
        "duplicate_image_occurrence_count": 1,
        "duplicate_image_review_required": False,
        "duplicate_image_review_confirmed": False,
        "duplicate_image_case_refs": [f"{source_id}/{case_id}"],
        "square_markers": square_markers,
        "semantic_cells": semantic_cells,
        "weak_candidates": {
            "caption": "포트홀",
            "route": "테스트 노선",
            "location": None,
            "evaluation": "포트홀",
            "opinion": "보수 필요",
        },
        "issue_type": "pothole",
        "maintenance_action": "repair",
        "caption": "포트홀",
        "route": "테스트 노선",
        "location": None,
        "evaluation": "포트홀",
        "opinion": "보수 필요",
        "user_confirmed": True,
    }
    return {
        **value,
        "record_sha256": hashlib.sha256(
            canonical_json_bytes(value)
        ).hexdigest(),
    }


def test_dataset_freezes_approved_reference_training_members(tmp_path):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1],
    )
    record = _chain_records(
        project_id,
        fixtures,
        [(fixtures[0], 0, "yes")],
    )[0]
    catalog.project_label_record(project_id, record)
    reference = _approved_reference_record(project_id)

    first = catalog.create_dataset_version(
        project_id,
        approved_reference_records=[reference],
    )
    replay = catalog.create_dataset_version(
        project_id,
        approved_reference_records=[reference],
    )

    assert replay["dataset_version_id"] == first["dataset_version_id"]
    assert replay["reused"] is True
    assert first["approved_reference_member_count"] == 1
    assert first["training_member_count"] == 1
    assert first["training_gate_member_count"] == 1
    assert first[
        "approved_references_count_toward_training_gate"
    ] is False
    assert first[
        "approved_references_count_toward_validation"
    ] is False
    frozen = first["approved_reference_members"][0]
    assert frozen["approval_id"] == reference["approval_id"]
    assert frozen["approval_revision"] == 1
    assert frozen["document_name"] == reference["document_name"]
    assert frozen["document_sha256"] == reference["document_sha256"]
    assert frozen["structure_target_summary_sha256"] == reference[
        "structure_target_summary_sha256"
    ]
    assert frozen["index_manifest_sha256"] == reference[
        "index_manifest_sha256"
    ]
    assert frozen["image_sha256"] == reference["image_sha256"]
    assert frozen["image_media_type"] == reference["image_media_type"]
    assert frozen["image_width"] == reference["image_width"]
    assert frozen["image_height"] == reference["image_height"]
    assert frozen["quarter"] == 3
    assert frozen["section_path"] == "Contents/section0.xml"
    assert frozen["section_index"] == reference["section_index"]
    assert frozen["table_id"] == reference["table_id"]
    assert frozen["table_index"] == reference["table_index"]
    assert frozen["cell_target_id"] == reference["cell_target_id"]
    assert frozen["cell_row"] == reference["cell_row"]
    assert frozen["cell_column"] == reference["cell_column"]
    assert frozen["photo_page_number"] == 2
    assert frozen["inspection_page_number"] == 1
    assert frozen["picture_target_id"] == reference[
        "picture_target_id"
    ]
    assert frozen["photo_slot_id"] == reference["photo_slot_id"]
    assert frozen["caption"] == reference["caption"]
    assert frozen["route"] == reference["route"]
    assert frozen["opinion"] == reference["opinion"]
    assert frozen["weak_candidates"] == reference["weak_candidates"]
    assert frozen["square_markers"] == reference["square_markers"]
    assert frozen["semantic_cells"] == sorted(
        reference["semantic_cells"],
        key=lambda item: item["role"],
    )
    assert frozen["square_marker_target_ids"] == [
        marker["cell_target_id"]
        for marker in frozen["square_markers"]
    ]
    assert frozen["evaluation_marker_target_ids"] == (
        frozen["square_marker_target_ids"]
    )
    member_hash = frozen["approved_reference_member_sha256"]
    assert member_hash == hashlib.sha256(
        canonical_json_bytes(
            {
                key: value
                for key, value in frozen.items()
                if key != "approved_reference_member_sha256"
            }
        )
    ).hexdigest()
    assert len(frozen["source_address_sha256"]) == 64
    assert first["approved_reference_training_event_count"] == 1
    assert first["manifest"]["counts"]["training_gate_members"] == 1
    assert first["manifest"]["counts"]["validation"] == 0
    assert first["manifest"][
        "approved_references_count_toward_training_gate"
    ] is False
    assert first["manifest"][
        "approved_references_count_toward_validation"
    ] is False

    changed_value = {
        **reference,
        "revision": 2,
        "maintenance_action": "resurface",
    }
    changed_value.pop("record_sha256")
    changed = {
        **changed_value,
        "record_sha256": hashlib.sha256(
            canonical_json_bytes(changed_value)
        ).hexdigest(),
    }
    second = catalog.create_dataset_version(
        project_id,
        approved_reference_records=[changed],
    )
    assert second["dataset_version_id"] != first["dataset_version_id"]
    assert second["dataset_hash"] != first["dataset_hash"]
    assert first["approved_reference_members"][0][
        "approval_revision"
    ] == 1
    assert second["approved_reference_members"][0][
        "approval_revision"
    ] == 2


def test_dataset_rejects_duplicate_approved_reference_evidence(tmp_path):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1],
    )
    catalog.project_label_record(
        project_id,
        _chain_records(
            project_id,
            fixtures,
            [(fixtures[0], 0, "no")],
        )[0],
    )
    shared_image = hashlib.sha256(b"shared-image").hexdigest()
    with pytest.raises(
        DatasetVersionError,
        match="duplicate evidence",
    ):
        catalog.create_dataset_version(
            project_id,
            approved_reference_records=[
                _approved_reference_record(
                    project_id,
                    seed="a",
                    image_sha256=shared_image,
                ),
                _approved_reference_record(
                    project_id,
                    seed="b",
                    image_sha256=shared_image,
                ),
            ],
        )


def test_dataset_rejects_stale_reference_without_corrected_layout_binding(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1],
    )
    catalog.project_label_record(
        project_id,
        _chain_records(
            project_id,
            fixtures,
            [(fixtures[0], 0, "yes")],
        )[0],
    )
    current = _approved_reference_record(project_id)
    stale = {
        **current,
        "schema_version": "trafficvisson.approved-reference.v1",
    }
    stale.pop("record_sha256")
    stale["record_sha256"] = hashlib.sha256(
        canonical_json_bytes(stale)
    ).hexdigest()

    with pytest.raises(
        DatasetVersionError,
        match="identity or training label",
    ):
        catalog.create_dataset_version(
            project_id,
            approved_reference_records=[stale],
        )


def test_dataset_weights_two_reference_photos_as_one_evidence_event(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1],
    )
    catalog.project_label_record(
        project_id,
        _chain_records(
            project_id,
            fixtures,
            [(fixtures[0], 0, "yes")],
        )[0],
    )
    first = _approved_reference_record(
        project_id,
        seed="group-photo-one",
        group_seed="same-evidence-event",
        group_photo_index=1,
        group_photo_count=2,
    )
    second = _approved_reference_record(
        project_id,
        seed="group-photo-two",
        group_seed="same-evidence-event",
        group_photo_index=2,
        group_photo_count=2,
    )

    dataset = catalog.create_dataset_version(
        project_id,
        approved_reference_records=[first, second],
    )

    assert dataset["approved_reference_member_count"] == 2
    assert dataset["approved_reference_training_event_count"] == 1
    assert dataset["training_member_count"] == 1
    assert dataset["training_gate_member_count"] == 1
    assert dataset["manifest"]["counts"][
        "approved_reference_training_events"
    ] == 1
