import sqlite3
from pathlib import Path

from app.store import DEMO_PROJECT_ID
from test_analysis_jobs import _auth, _production_client, _tools


def test_project_and_import_catalog_survive_bridge_restart(tmp_path):
    ffmpeg, ffprobe = _tools()
    local_selection_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    with _production_client(tmp_path, ffmpeg, ffprobe) as first_client:
        created = first_client.post(
            "/projects",
            headers=_auth(),
            json={
                "name": "영속 프로젝트",
                "description": "bridge 재시작 후에도 유지",
            },
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]

        inspected = first_client.post(
            f"/projects/{project_id}/imports/inspect",
            headers=_auth(),
            json={
                "project_id": project_id,
                "source_type": "local_selection",
                "local_selection_id": local_selection_id,
            },
        )
        assert inspected.status_code == 200, inspected.text
        inspection_id = inspected.json()["inspected_source_id"]

    with _production_client(tmp_path, ffmpeg, ffprobe) as restarted_client:
        projects = restarted_client.get("/projects", headers=_auth())
        assert projects.status_code == 200, projects.text
        project_ids = [item["id"] for item in projects.json()["projects"]]
        assert project_ids.count(str(DEMO_PROJECT_ID)) == 1
        assert project_id in project_ids

        restored = restarted_client.get(
            f"/projects/{project_id}",
            headers=_auth(),
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["name"] == "영속 프로젝트"
        assert restored.json()["fixture"] is False

        queued = restarted_client.post(
            f"/projects/{project_id}/imports",
            headers=_auth(),
            json={
                "project_id": project_id,
                "inspected_source_id": inspection_id,
                "rights_confirmed": True,
            },
        )
        assert queued.status_code == 202, queued.text
        assert queued.json() == {
            "import_id": queued.json()["import_id"],
            "status": "queued",
            "source_type": "local_selection",
            "provenance_status": "pending_download_hash",
        }

    database_path = Path(tmp_path) / "p" / "catalog.sqlite3"
    with sqlite3.connect(str(database_path)) as connection:
        project_count = connection.execute(
            "SELECT COUNT(*) FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()[0]
        inspection_count = connection.execute(
            "SELECT COUNT(*) FROM import_inspections WHERE id = ?",
            (inspection_id,),
        ).fetchone()[0]
        import_count = connection.execute(
            "SELECT COUNT(*) FROM imports WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]

    assert project_count == 1
    assert inspection_count == 1
    assert import_count == 1


def test_fixture_without_explicit_data_root_stays_in_memory(client, auth):
    created = client.post(
        "/projects",
        headers=auth,
        json={"name": "테스트 전용 메모리 프로젝트"},
    )
    assert created.status_code == 201
    assert client.app.state.store._project_catalog is None
