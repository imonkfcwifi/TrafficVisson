import hashlib
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.dataset_versions import LabelProjectionError, canonical_json_bytes
from app.main import create_app
from app.project_catalog import CATALOG_SCHEMA_VERSION, ProjectCatalog
from app.store import DEMO_PROJECT_ID
from app.training_geometry import SCHEMA_VERSION as GEOMETRY_SCHEMA
from app.training_geometry import seal_training_geometry
from test_project_catalog_v2 import _project, _rows, _write_completed_analysis
from test_project_catalog_v3 import (
    _answer_record,
    _catalog_with_jobs,
    _chain_records,
    _seed_v2_database,
)
from structured_assist_fixtures import assist_provenance


TOKEN = "incident-v4-test-token-with-at-least-256-bits-0000000000"


def _approved_record(
    *,
    project_id,
    job_id,
    candidate,
    revision=1,
    previous_hash=None,
    recorded_offset=0,
    answer="yes",
    issue_type="pothole",
    severity="major",
    maintenance_action="포트홀 절삭 후 재포장",
    ocr_text=None,
    answered_by="local_user",
):
    record = _answer_record(
        project_id=project_id,
        job_id=job_id,
        candidate=candidate,
        answer=answer,
        revision=revision,
        previous_hash=previous_hash,
        recorded_offset=recorded_offset,
        answered_by=answered_by,
        assist_provenance_value=(
            assist_provenance(
                project_id,
                job_id,
                candidate["candidate_id"],
            )
            if answered_by == "structured_llm_assisted"
            else None
        ),
    )
    record["final_answer"].update(
        {
            "selected_issue_type": issue_type,
            "severity": severity,
            "maintenance_action": maintenance_action,
            "ocr_text": ocr_text,
        }
    )
    without_hash = dict(record)
    without_hash.pop("record_hash")
    record["record_hash"] = hashlib.sha256(
        canonical_json_bytes(without_hash)
    ).hexdigest()
    return record


def test_structured_and_human_training_labels_do_not_imply_incident_truth(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1],
    )
    fixture = fixtures[0]
    candidate = fixture["payload"]["result"]["candidates"][0]
    assisted = _approved_record(
        project_id=project_id,
        job_id=fixture["job_id"],
        candidate=candidate,
        answered_by="structured_llm_assisted",
    )

    projection = catalog.project_label_record(project_id, assisted)
    annotations = catalog.list_current_annotations(project_id)

    assert projection["answered_by"] == "structured_llm_assisted"
    assert projection["label_source"] == "structured_llm_assisted"
    assert catalog.list_incidents(project_id) == []
    assert len(annotations) == 1
    assert annotations[0]["answered_by"] == "structured_llm_assisted"
    assert annotations[0]["label_source"] == "structured_llm_assisted"
    assert annotations[0]["source_kind"] == "structured_llm_assisted"
    assert annotations[0]["user_confirmed"] is False
    assert annotations[0]["training_authorized"] is True
    assert (
        annotations[0]["assist_provenance"]["authority"]["kind"]
        == "project_profile_delegation"
    )

    dataset = catalog.create_dataset_version(project_id)
    assert dataset["training_member_count"] == 1
    assert dataset["memberships"][0]["split"] == "train"

    human = _approved_record(
        project_id=project_id,
        job_id=fixture["job_id"],
        candidate=candidate,
        revision=2,
        previous_hash=assisted["record_hash"],
        recorded_offset=1,
        answered_by="local_user",
        severity="moderate",
    )
    catalog.project_label_record(project_id, human)

    current = catalog.list_current_annotations(project_id)
    incidents = catalog.list_incidents(project_id)
    assert current[0]["answer_revision"] == 2
    assert current[0]["answered_by"] == "local_user"
    assert current[0]["label_source"] == "local_user_review"
    assert current[0]["user_confirmed"] is True
    assert incidents == []


def test_training_geometry_survives_annotation_and_dataset_projection(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1],
    )
    fixture = fixtures[0]
    candidate = fixture["payload"]["result"]["candidates"][0]
    box = {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}
    geometry = seal_training_geometry(
        {
            "schema_version": GEOMETRY_SCHEMA,
            "source_kind": "accepted_ai_proposal",
            "approval_authority": "local_user",
            "image_sha256": "a" * 64,
            "image_artifact_ref": "candidate-evidence:overlay:image",
            "normalized_box": box,
            "polygon": None,
            "mask_artifact_ref": None,
            "mask_sha256": None,
            "tracked_frame_refs": [],
            "proposal_sha256": "b" * 64,
            "sam2_output_sha256": None,
            "sam2_model_manifest_sha256": None,
        }
    )
    record = _approved_record(
        project_id=project_id,
        job_id=fixture["job_id"],
        candidate=candidate,
    )
    record["schema_version"] = "1.1"
    record["final_answer"]["normalized_box"] = box
    record["training_geometry"] = geometry
    without_hash = dict(record)
    without_hash.pop("record_hash")
    record["record_hash"] = hashlib.sha256(
        canonical_json_bytes(without_hash)
    ).hexdigest()

    catalog.project_label_record(project_id, record)
    annotation = catalog.list_current_annotations(project_id)[0]
    dataset = catalog.create_dataset_version(project_id)

    assert annotation["training_geometry"] == geometry
    assert annotation["normalized_box"] == box
    assert dataset["memberships"][0]["annotation_hash"] == (
        annotation["annotation_hash"]
    )


def test_v3_to_v4_migration_adds_incident_contract(tmp_path):
    project = _project()
    database_path = tmp_path / "catalog.sqlite3"
    _seed_v2_database(database_path, project)
    with sqlite3.connect(str(database_path), isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        ProjectCatalog._apply_migration_v3(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (3, ?)",
            ("v3",),
        )
        connection.execute("COMMIT")

    ProjectCatalog(database_path, project)

    assert CATALOG_SCHEMA_VERSION == 4
    assert _rows(
        database_path,
        "SELECT version FROM schema_migrations ORDER BY version",
    ) == [{"version": 1}, {"version": 2}, {"version": 3}, {"version": 4}]
    columns = {
        row["name"]
        for row in _rows(database_path, "PRAGMA table_info(incidents)")
    }
    assert {
        "id",
        "project_id",
        "question_id",
        "analysis_job_id",
        "candidate_id",
        "status",
        "issue_type",
        "severity",
        "maintenance_action",
        "ocr_text",
        "answer_revision",
        "source_answer_record_id",
        "source_record_hash",
        "evidence_image_ref",
        "preview_clip_ref",
        "start_time_ms",
        "representative_time_ms",
        "end_time_ms",
        "created_at",
        "activated_at",
        "updated_at",
        "superseded_at",
        "superseded_by_answer_revision",
        "superseded_by_answer_record_id",
        "superseded_by_record_hash",
    } == columns


def test_incident_upsert_is_deterministic_and_tracks_revision_transitions(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1],
    )
    fixture = fixtures[0]
    candidate = fixture["payload"]["result"]["candidates"][0]
    first = _approved_record(
        project_id=project_id,
        job_id=fixture["job_id"],
        candidate=candidate,
        ocr_text="중앙지하차도",
    )

    catalog.project_label_record(
        project_id, first, materialize_incident=True
    )
    replay = catalog.project_label_record(
        project_id, first, materialize_incident=True
    )
    active = catalog.list_incidents(project_id)

    assert replay["created"] is False
    assert len(active) == 1
    incident_id = first["question_id"]
    assert active[0] == {
        "incident_id": incident_id,
        "project_id": str(project_id),
        "question_id": first["question_id"],
        "analysis_job_id": str(fixture["job_id"]),
        "candidate_id": candidate["candidate_id"],
        "status": "active",
        "issue_type": "pothole",
        "severity": "major",
        "maintenance_action": "포트홀 절삭 후 재포장",
        "ocr_text": "중앙지하차도",
        "answer_revision": 1,
        "source_answer_record_id": first["record_id"],
        "source_record_hash": first["record_hash"],
        "evidence_image_ref": candidate["representative_image"][
            "artifact_ref"
        ],
        "preview_clip_ref": candidate["preview_clip"]["artifact_ref"],
        "start_time_ms": 0,
        "representative_time_ms": 500,
        "end_time_ms": 1000,
        "created_at": first["recorded_at"],
        "activated_at": first["recorded_at"],
        "updated_at": first["recorded_at"],
        "superseded_at": None,
        "superseded_by_answer_revision": None,
        "superseded_by_answer_record_id": None,
        "superseded_by_record_hash": None,
    }

    edited = _approved_record(
        project_id=project_id,
        job_id=fixture["job_id"],
        candidate=candidate,
        revision=2,
        previous_hash=first["record_hash"],
        recorded_offset=1,
        answer="edit",
        issue_type="road_surface_damage",
        severity="urgent",
        maintenance_action="파손 구간 긴급 재포장",
    )
    catalog.project_label_record(
        project_id, edited, materialize_incident=True
    )
    updated = catalog.list_incidents(project_id)[0]
    assert updated["incident_id"] == incident_id
    assert updated["answer_revision"] == 2
    assert updated["issue_type"] == "road_surface_damage"
    assert updated["source_record_hash"] == edited["record_hash"]
    assert updated["created_at"] == first["recorded_at"]

    rejected = _answer_record(
        project_id=project_id,
        job_id=fixture["job_id"],
        candidate=candidate,
        answer="no",
        revision=3,
        previous_hash=edited["record_hash"],
        recorded_offset=2,
    )
    catalog.project_label_record(
        project_id, rejected, materialize_incident=True
    )
    assert catalog.list_incidents(project_id) == []
    superseded = catalog.list_incidents(
        project_id, include_inactive=True
    )[0]
    assert superseded["incident_id"] == incident_id
    assert superseded["status"] == "superseded"
    assert superseded["answer_revision"] == 2
    assert superseded["source_answer_record_id"] == edited["record_id"]
    assert superseded["source_record_hash"] == edited["record_hash"]
    assert superseded["superseded_at"] == rejected["recorded_at"]
    assert superseded["superseded_by_answer_revision"] == 3
    assert (
        superseded["superseded_by_answer_record_id"]
        == rejected["record_id"]
    )
    assert (
        superseded["superseded_by_record_hash"] == rejected["record_hash"]
    )
    assert superseded["issue_type"] == "road_surface_damage"

    catalog.project_label_record(
        project_id, first, materialize_incident=True
    )
    assert catalog.list_incidents(
        project_id, include_inactive=True
    ) == [superseded]

    catalog.project_label_records(
        project_id,
        [first, edited, rejected],
        incident_analysis_job_ids=[fixture["job_id"]],
    )
    assert catalog.list_incidents(
        project_id, include_inactive=True
    ) == [superseded]

    reapproved = _approved_record(
        project_id=project_id,
        job_id=fixture["job_id"],
        candidate=candidate,
        revision=4,
        previous_hash=rejected["record_hash"],
        recorded_offset=3,
        issue_type="pothole",
        severity="moderate",
        maintenance_action="포트홀 보수",
    )
    catalog.project_label_record(
        project_id, reapproved, materialize_incident=True
    )
    reactivated = catalog.list_incidents(project_id)[0]
    assert reactivated["incident_id"] == incident_id
    assert reactivated["status"] == "active"
    assert reactivated["answer_revision"] == 4
    assert reactivated["created_at"] == first["recorded_at"]
    assert reactivated["activated_at"] == reapproved["recorded_at"]
    assert reactivated["superseded_at"] is None


def test_incident_database_constraints_protect_identity_and_revision(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1],
    )
    fixture = fixtures[0]
    candidate = fixture["payload"]["result"]["candidates"][0]
    approved = _approved_record(
        project_id=project_id,
        job_id=fixture["job_id"],
        candidate=candidate,
    )
    rejected = _answer_record(
        project_id=project_id,
        job_id=fixture["job_id"],
        candidate=candidate,
        answer="no",
        revision=2,
        previous_hash=approved["record_hash"],
        recorded_offset=1,
    )
    catalog.project_label_records(
        project_id,
        [approved, rejected],
        incident_analysis_job_ids=[fixture["job_id"]],
    )

    with sqlite3.connect(
        str(tmp_path / "catalog.sqlite3"), isolation_level=None
    ) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                UPDATE incidents
                SET id = ?
                WHERE project_id = ? AND question_id = ?
                """,
                (str(uuid4()), str(project_id), approved["question_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """
                UPDATE incidents
                SET superseded_by_answer_revision = answer_revision
                WHERE project_id = ? AND question_id = ?
                """,
                (str(project_id), approved["question_id"]),
            )


def test_nonapproved_answers_do_not_create_incidents_and_projects_are_isolated(
    tmp_path,
):
    project_a = uuid4()
    project_b = uuid4()
    fixture_a = _write_completed_analysis(
        tmp_path,
        project_id=project_a,
        source_seed="incident-project-a",
    )
    catalog = ProjectCatalog(
        tmp_path / "catalog.sqlite3",
        _project(project_id=project_a),
    )
    catalog.create_project(_project(project_id=project_b))
    fixture_b = _write_completed_analysis(
        tmp_path,
        project_id=project_b,
        source_seed="incident-project-b",
    )
    catalog.backfill_completed_analysis_records()

    candidate_a = fixture_a["payload"]["result"]["candidates"][0]
    negative = _answer_record(
        project_id=project_a,
        job_id=fixture_a["job_id"],
        candidate=candidate_a,
        answer="no",
    )
    held = _answer_record(
        project_id=project_a,
        job_id=fixture_a["job_id"],
        candidate=candidate_a,
        answer="unsure",
        revision=2,
        previous_hash=negative["record_hash"],
        recorded_offset=1,
    )
    catalog.project_label_records(project_a, [negative, held])
    assert catalog.list_incidents(project_a, include_inactive=True) == []

    candidate_b = fixture_b["payload"]["result"]["candidates"][0]
    approved_b = _approved_record(
        project_id=project_b,
        job_id=fixture_b["job_id"],
        candidate=candidate_b,
    )
    catalog.project_label_record(
        project_b, approved_b, materialize_incident=True
    )
    assert catalog.list_incidents(project_a, include_inactive=True) == []
    assert {
        row["project_id"]
        for row in catalog.list_incidents(project_b, include_inactive=True)
    } == {str(project_b)}
    assert (
        catalog.list_incidents(
            project_a,
            include_inactive=True,
            analysis_job_id=fixture_b["job_id"],
        )
        == []
    )
    with pytest.raises(LabelProjectionError, match="project boundary"):
        catalog.project_label_record(project_a, approved_b)


def test_training_answers_preserve_partitions_without_incident_authority(
    tmp_path,
):
    project_id = uuid4()
    catalog, fixtures = _catalog_with_jobs(
        tmp_path,
        project_id=project_id,
        job_candidate_counts=[1, 1, 1, 1],
    )
    records = _chain_records(
        project_id,
        fixtures,
        [
            (fixtures[0], 0, "yes"),
            (fixtures[1], 0, "edit"),
            (fixtures[2], 0, "no"),
            (fixtures[3], 0, "unsure"),
        ],
    )

    catalog.project_label_records(project_id, records)

    annotations = catalog.list_current_annotations(project_id)
    assert len(annotations) == 4
    assert {
        (row["partition"], row["include_in_training"])
        for row in annotations
    } == {
        ("approved", True),
        ("negative", True),
        ("held", False),
    }
    assert catalog.list_incidents(
        project_id, include_inactive=True
    ) == []

    with pytest.raises(
        LabelProjectionError,
        match="must also materialize annotations",
    ):
        catalog.project_label_records(
            project_id,
            records,
            materialize_analysis_job_ids=[],
            incident_analysis_job_ids=[fixtures[0]["job_id"]],
        )


def test_verified_ledger_can_defer_and_later_materialize_one_analysis(
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
            (fixtures[1], 0, "yes"),
        ],
    )

    catalog.project_label_records(
        project_id,
        records,
        materialize_analysis_job_ids=[fixtures[1]["job_id"]],
        incident_analysis_job_ids=[fixtures[1]["job_id"]],
    )

    assert len(catalog.list_label_answers(project_id)) == 2
    annotations = catalog.list_current_annotations(project_id)
    assert {
        row["analysis_job_id"] for row in annotations
    } == {str(fixtures[1]["job_id"])}
    incidents = catalog.list_incidents(project_id)
    assert {
        row["analysis_job_id"] for row in incidents
    } == {str(fixtures[1]["job_id"])}

    catalog.project_label_records(
        project_id,
        records,
        materialize_analysis_job_ids=[
            fixtures[0]["job_id"],
            fixtures[1]["job_id"],
        ],
        incident_analysis_job_ids=[
            fixtures[0]["job_id"],
            fixtures[1]["job_id"],
        ],
    )

    assert {
        row["analysis_job_id"]
        for row in catalog.list_current_annotations(project_id)
    } == {
        str(fixtures[0]["job_id"]),
        str(fixtures[1]["job_id"]),
    }
    assert {
        row["analysis_job_id"]
        for row in catalog.list_incidents(project_id)
    } == {
        str(fixtures[0]["job_id"]),
        str(fixtures[1]["job_id"]),
    }


def test_pipeline_review_gate_materializes_annotation_but_not_incident(
    tmp_path,
):
    data_root = tmp_path / "bridge-data"
    fixture = _write_completed_analysis(
        data_root,
        project_id=DEMO_PROJECT_ID,
        source_seed="pipeline-owned-answer",
    )
    job_id = fixture["job_id"]
    candidate = fixture["payload"]["result"]["candidates"][0]
    candidate_id = candidate["candidate_id"]
    answer_url = (
        f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
        f"/candidates/{candidate_id}/answer"
    )
    incidents_url = f"/projects/{DEMO_PROJECT_ID}/incidents"
    auth = {"Authorization": f"Bearer {TOKEN}"}
    app = create_app(
        TOKEN,
        fixture_mode=False,
        allow_test_client=True,
        bridge_data_root=str(data_root),
    )

    with TestClient(app) as client:
        gate = {"closed": True}
        client.app.state.pipeline_jobs.is_analysis_projection_gated = (
            lambda project_id, analysis_job_id: (
                gate["closed"] and analysis_job_id == job_id
            )
        )
        approved = client.post(
            answer_url,
            headers=auth,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": str(job_id),
                "answer": "yes",
                "selected_issue_type": "road_debris",
                "severity": "major",
                "maintenance_action": "낙하물 긴급 수거",
            },
        )
        assert approved.status_code == 201, approved.text
        audit = client.app.state.candidate_labels.audit_records(
            DEMO_PROJECT_ID,
            analysis_job_id=job_id,
        )
        assert len(audit) == 1
        stats = client.app.state.store.current_label_stats(
            DEMO_PROJECT_ID
        )
        assert stats["answer_revision_count"] == 1
        assert stats["current_answer_count"] == 0
        assert (
            client.get(incidents_url, headers=auth).json()["count"]
            == 0
        )

        gate["closed"] = False
        promoted = client.get(incidents_url, headers=auth)
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["count"] == 0
        stats = client.app.state.store.current_label_stats(
            DEMO_PROJECT_ID
        )
        assert stats["current_answer_count"] == 1


def test_registered_pipeline_queue_is_visible_but_not_approved_truth(
    tmp_path,
):
    data_root = tmp_path / "bridge-data"
    fixture = _write_completed_analysis(
        data_root,
        project_id=DEMO_PROJECT_ID,
        source_seed="registered-pipeline-queue",
    )
    job_id = fixture["job_id"]
    candidate = fixture["payload"]["result"]["candidates"][0]
    auth = {"Authorization": f"Bearer {TOKEN}"}
    app = create_app(
        TOKEN,
        fixture_mode=False,
        allow_test_client=True,
        bridge_data_root=str(data_root),
    )

    with TestClient(app) as client:
        question = (
            client.app.state.candidate_labels.resolve_question(
                DEMO_PROJECT_ID,
                job_id,
                candidate["candidate_id"],
            )
        )
        first = client.app.state.store.register_label_questions(
            DEMO_PROJECT_ID,
            job_id,
            fixture["source_sha256"],
            [question],
        )
        replay = client.app.state.store.register_label_questions(
            DEMO_PROJECT_ID,
            job_id,
            fixture["source_sha256"],
            [question],
        )
        assert first["created_count"] == 1
        assert replay["created_count"] == 0

        stats = client.get(
            f"/projects/{DEMO_PROJECT_ID}/labels/stats",
            headers=auth,
        )
        assert stats.status_code == 200, stats.text
        assert stats.json()["question_count"] == 1
        assert stats.json()["answer_revision_count"] == 0
        assert stats.json()["current_answer_count"] == 0
        incidents = client.get(
            f"/projects/{DEMO_PROJECT_ID}/incidents",
            headers=auth,
        )
        assert incidents.status_code == 200, incidents.text
        assert incidents.json()["count"] == 0


def test_incident_api_reconciles_from_jsonl_and_filters_inactive(tmp_path):
    data_root = tmp_path / "bridge-data"
    fixture = _write_completed_analysis(
        data_root,
        project_id=DEMO_PROJECT_ID,
        source_seed="incident-api",
    )
    job_id = fixture["job_id"]
    candidate = fixture["payload"]["result"]["candidates"][0]
    candidate_id = candidate["candidate_id"]
    answer_url = (
        f"/projects/{DEMO_PROJECT_ID}/analysis/jobs/{job_id}"
        f"/candidates/{candidate_id}/answer"
    )
    incidents_url = f"/projects/{DEMO_PROJECT_ID}/incidents"
    auth = {"Authorization": f"Bearer {TOKEN}"}
    app = create_app(
        TOKEN,
        fixture_mode=False,
        allow_test_client=True,
        bridge_data_root=str(data_root),
    )

    with TestClient(app) as client:
        empty = client.get(incidents_url, headers=auth)
        assert empty.status_code == 200
        assert empty.json() == {
            "project_id": str(DEMO_PROJECT_ID),
            "incidents": [],
            "count": 0,
        }
        approved = client.post(
            answer_url,
            headers=auth,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": str(job_id),
                "answer": "yes",
                "selected_issue_type": "sign_vegetation_occlusion",
                "severity": "major",
                "maintenance_action": "표지판 전면 수목 제거",
                "ocr_text": "서울 12km",
            },
        )
        assert approved.status_code == 201, approved.text
        audit = client.app.state.candidate_labels.audit_records(
            DEMO_PROJECT_ID,
            analysis_job_id=job_id,
        )
        client.app.state.store.project_label_records(
            DEMO_PROJECT_ID,
            audit,
            materialize_analysis_job_ids=[job_id],
            incident_analysis_job_ids=[job_id],
        )
        active = client.get(incidents_url, headers=auth)
        assert active.status_code == 200, active.text
        active_payload = active.json()
        assert active_payload["count"] == 1
        incident_id = active_payload["incidents"][0]["incident_id"]
        assert active_payload["incidents"][0]["status"] == "active"
        assert active_payload["incidents"][0]["answer_revision"] == 1

        other = client.post(
            "/projects",
            headers=auth,
            json={"name": "격리 프로젝트"},
        )
        assert other.status_code == 201
        isolated = client.get(
            f"/projects/{other.json()['id']}/incidents",
            headers=auth,
        )
        assert isolated.status_code == 200
        assert isolated.json()["incidents"] == []

        held = client.post(
            answer_url,
            headers=auth,
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "analysis_job_id": str(job_id),
                "answer": "unsure",
            },
        )
        assert held.status_code == 201, held.text
        assert held.json()["answer_revision"] == 2
        audit = client.app.state.candidate_labels.audit_records(
            DEMO_PROJECT_ID,
            analysis_job_id=job_id,
        )
        client.app.state.store.project_label_records(
            DEMO_PROJECT_ID,
            audit,
            materialize_analysis_job_ids=[job_id],
            incident_analysis_job_ids=[job_id],
        )
        assert client.get(incidents_url, headers=auth).json()["count"] == 0
        inactive = client.get(
            incidents_url,
            headers=auth,
            params={
                "include_inactive": "true",
                "analysis_job_id": str(job_id),
            },
        )
        assert inactive.status_code == 200
        inactive_incident = inactive.json()["incidents"][0]
        assert inactive_incident["incident_id"] == incident_id
        assert inactive_incident["status"] == "superseded"
        assert inactive_incident["answer_revision"] == 1
        assert inactive_incident["superseded_by_answer_revision"] == 2
        audit = client.app.state.candidate_labels.audit_records(
            DEMO_PROJECT_ID,
            analysis_job_id=job_id,
        )
        assert len(audit) == 2
        assert inactive_incident["source_record_hash"] == audit[0]["record_hash"]
        assert (
            inactive_incident["superseded_by_record_hash"]
            == audit[-1]["record_hash"]
        )

    with sqlite3.connect(str(data_root / "catalog.sqlite3")) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("DELETE FROM incidents")
        connection.commit()

    restarted = create_app(
        TOKEN,
        fixture_mode=False,
        allow_test_client=True,
        bridge_data_root=str(data_root),
    )
    with TestClient(restarted) as client:
        reconciled = client.get(
            incidents_url,
            headers=auth,
            params={"include_inactive": "true"},
        )
        assert reconciled.status_code == 200, reconciled.text
        # JSONL reconciliation alone cannot recreate maintenance truth after
        # its trusted survey authority was removed. A durable PipelineJob
        # derived-output replay must re-present that authority.
        assert reconciled.json()["count"] == 0
