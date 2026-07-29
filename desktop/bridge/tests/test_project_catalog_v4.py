import hashlib
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.dataset_versions import LabelProjectionError, canonical_json_bytes
from app.main import create_app
from app.project_catalog import CATALOG_SCHEMA_VERSION, ProjectCatalog
from app.store import DEMO_PROJECT_ID
from test_project_catalog_v2 import _project, _rows, _write_completed_analysis
from test_project_catalog_v3 import (
    _answer_record,
    _catalog_with_jobs,
    _seed_v2_database,
)


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
):
    record = _answer_record(
        project_id=project_id,
        job_id=job_id,
        candidate=candidate,
        answer=answer,
        revision=revision,
        previous_hash=previous_hash,
        recorded_offset=recorded_offset,
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

    catalog.project_label_record(project_id, first)
    replay = catalog.project_label_record(project_id, first)
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
    catalog.project_label_record(project_id, edited)
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
    catalog.project_label_record(project_id, rejected)
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

    catalog.project_label_record(project_id, first)
    assert catalog.list_incidents(
        project_id, include_inactive=True
    ) == [superseded]

    catalog.project_label_records(project_id, [first, edited, rejected])
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
    catalog.project_label_record(project_id, reapproved)
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
    catalog.project_label_records(project_id, [approved, rejected])

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
    catalog.project_label_record(project_b, approved_b)
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
        assert reconciled.json()["count"] == 1
        assert reconciled.json()["incidents"][0]["incident_id"] == incident_id
        assert reconciled.json()["incidents"][0]["status"] == "superseded"
