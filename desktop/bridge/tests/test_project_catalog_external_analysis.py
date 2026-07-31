import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from app.project_catalog import ProjectCatalog
from test_project_catalog_v2 import (
    _canonical_json,
    _digest,
    _project,
    _rows,
    _write_completed_analysis,
)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _externalize_completed_analysis(tmp_path, *, project_id):
    fixture = _write_completed_analysis(
        tmp_path,
        project_id=project_id,
        source_seed="external-analysis",
        candidate_count=2,
    )
    pipeline_job_id = uuid4()
    configured_at = datetime(2026, 7, 30, tzinfo=timezone.utc).isoformat()
    root_binding = {
        "schema_version": (
            "trafficvisson.project-artifact-root-job-binding.v1"
        ),
        "project_id": str(project_id),
        "profile_revision": 3,
        "output_location_id": f"project-output-{'a' * 32}",
        "registry_revision": 7,
        "root_identity_sha256": _sha("external-root"),
        "marker_sha256": _sha("external-marker"),
        "configured_free_bytes": 10 * 1024**3,
        "configured_at": configured_at,
        "job_start_free_bytes": 9 * 1024**3,
        "job_start_at": configured_at,
        "path_exposed": False,
    }
    payload = fixture["payload"]
    payload["artifact_locator"] = {
        "schema_version": "trafficvisson.analysis-artifact-locator.v1",
        "mode": "external_category_root",
        "category": "analysis-candidates",
        "project_id": str(project_id),
        "analysis_job_id": str(fixture["job_id"]),
        "artifact_owner_id": str(pipeline_job_id),
        "output_location_id": root_binding["output_location_id"],
        "root_identity_sha256": root_binding["root_identity_sha256"],
        "root_binding": root_binding,
        "root_binding_sha256": hashlib.sha256(
            _canonical_json(root_binding)
        ).hexdigest(),
        "category_root_identity_sha256": _sha(
            "external-category-root"
        ),
        "run_directory_name": payload["artifact_directory_name"],
        "artifact_manifest_sha256": _sha("artifact-manifest"),
    }
    encoded = _canonical_json(payload)
    fixture["record_path"].write_bytes(encoded)
    fixture["digest_path"].write_text(_digest(encoded), encoding="ascii")
    fixture["pipeline_job_id"] = pipeline_job_id
    fixture["root_binding"] = root_binding
    return fixture


def _verified_analysis(fixture):
    payload = fixture["payload"]
    return {
        "job_id": fixture["job_id"],
        "project_id": UUID(payload["project_id"]),
        "snapshot_id": fixture["snapshot_id"],
        "hash_job_id": fixture["hash_job_id"],
        "file_name": payload["file_name"],
        "source_sha256": fixture["source_sha256"],
        "status": "completed",
        "result": payload["result"],
    }


def _questions(fixture):
    return [
        {
            "question_id": str(
                uuid5(
                    UUID(str(fixture["job_id"])),
                    candidate["candidate_id"],
                )
            ),
            "candidate_id": candidate["candidate_id"],
            "ai_original_candidate": candidate,
        }
        for candidate in fixture["payload"]["result"]["candidates"]
    ]


def test_external_analysis_stays_isolated_until_live_verifier_backfills_path_free(
    tmp_path,
):
    project_id = uuid4()
    fixture = _externalize_completed_analysis(
        tmp_path,
        project_id=project_id,
    )
    database_path = tmp_path / "catalog.sqlite3"
    catalog = ProjectCatalog(
        database_path,
        _project(project_id=project_id),
    )

    assert catalog.backfill_summary.scanned_records == 1
    assert catalog.backfill_summary.indexed_records == 0
    assert catalog.backfill_summary.isolated_records == 1
    assert _rows(database_path, "SELECT id FROM analysis_jobs") == []
    assert _rows(
        database_path,
        """
        SELECT error_code
        FROM analysis_backfill_issues
        WHERE record_relative_path = ?
        """,
        (
            fixture["record_path"]
            .relative_to(tmp_path)
            .as_posix(),
        ),
    ) == [{"error_code": "external_artifact_resolver_unavailable"}]

    resolver_calls = []

    def verified_resolver(resolved_project_id, analysis_job_id):
        resolver_calls.append((resolved_project_id, analysis_job_id))
        assert resolved_project_id == project_id
        assert analysis_job_id == fixture["job_id"]
        return _verified_analysis(fixture)

    recovered = catalog.bind_completed_analysis_resolver(
        verified_resolver
    )

    assert recovered.scanned_records == 1
    assert recovered.indexed_records == 1
    assert recovered.isolated_records == 0
    assert resolver_calls == [(project_id, fixture["job_id"])]
    rows = _rows(
        database_path,
        """
        SELECT id, artifact_directory_relative_path
        FROM analysis_jobs
        """,
    )
    assert rows == [
        {
            "id": str(fixture["job_id"]),
            "artifact_directory_relative_path": (
                "external-analysis-artifacts/"
                f"{fixture['pipeline_job_id']}/"
                "analysis-candidates/"
                f"{fixture['payload']['artifact_directory_name']}"
            ),
        }
    ]
    assert _rows(
        database_path,
        "SELECT * FROM analysis_backfill_issues",
    ) == []
    assert "\\" not in rows[0]["artifact_directory_relative_path"]
    assert ":" not in rows[0]["artifact_directory_relative_path"]


def test_external_analysis_verifier_mismatch_remains_isolated(tmp_path):
    project_id = uuid4()
    fixture = _externalize_completed_analysis(
        tmp_path,
        project_id=project_id,
    )
    verified = _verified_analysis(fixture)
    verified["source_sha256"] = _sha("another-source")

    catalog = ProjectCatalog(
        tmp_path / "catalog.sqlite3",
        _project(project_id=project_id),
        completed_analysis_resolver=lambda *_: verified,
    )

    assert catalog.backfill_summary.scanned_records == 1
    assert catalog.backfill_summary.indexed_records == 0
    assert catalog.backfill_summary.isolated_records == 1
    assert _rows(
        tmp_path / "catalog.sqlite3",
        "SELECT id FROM analysis_jobs",
    ) == []
    assert _rows(
        tmp_path / "catalog.sqlite3",
        "SELECT error_code FROM analysis_backfill_issues",
    ) == [{"error_code": "external_artifact_verification_mismatch"}]


def test_restart_question_card_list_for_external_analysis_creates_no_truth(
    tmp_path,
):
    project_id = uuid4()
    fixture = _externalize_completed_analysis(
        tmp_path,
        project_id=project_id,
    )
    database_path = tmp_path / "catalog.sqlite3"

    first = ProjectCatalog(
        database_path,
        _project(project_id=project_id),
        completed_analysis_resolver=lambda *_: _verified_analysis(fixture),
    )
    registered = first.register_label_questions(
        project_id,
        fixture["job_id"],
        fixture["source_sha256"],
        _questions(fixture),
    )
    assert registered["question_count"] == 2
    assert first.list_label_answers(project_id) == []
    assert first.list_current_annotations(project_id) == []
    assert first.list_incidents(
        project_id,
        analysis_job_id=fixture["job_id"],
    ) == []

    restarted = ProjectCatalog(
        database_path,
        _project(project_id=project_id),
        completed_analysis_resolver=lambda *_: _verified_analysis(fixture),
    )
    first_lookup = restarted.list_label_questions(
        project_id,
        analysis_job_id=fixture["job_id"],
    )
    second_lookup = restarted.list_label_questions(
        project_id,
        analysis_job_id=fixture["job_id"],
    )

    assert first_lookup == second_lookup
    assert len(first_lookup) == 2
    assert {row["current_record_id"] for row in first_lookup} == {None}
    assert restarted.list_label_answers(project_id) == []
    assert restarted.list_current_annotations(project_id) == []
    assert restarted.list_incidents(
        project_id,
        analysis_job_id=fixture["job_id"],
    ) == []
    assert _rows(database_path, "SELECT * FROM label_answers") == []
    assert _rows(database_path, "SELECT * FROM annotations") == []
    assert _rows(database_path, "SELECT * FROM incidents") == []
