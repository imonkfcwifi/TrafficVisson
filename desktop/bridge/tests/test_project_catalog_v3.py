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
        "answered_by": "local_user",
    }
    record_without_hash = {
        "schema_version": "1.0",
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
