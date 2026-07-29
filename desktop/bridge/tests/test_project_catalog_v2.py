import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.project_catalog import (
    CATALOG_SCHEMA_VERSION,
    ProjectCatalog,
)


def _project(project_id=None, *, name="기본 프로젝트"):
    return {
        "id": project_id or uuid4(),
        "name": name,
        "description": "SQLite v2 테스트",
        "question_policy": "learning_assist",
        "active_model_version": "common-initial@1.0.0",
        "created_at": datetime(2026, 7, 28, tzinfo=timezone.utc),
        "fixture": False,
    }


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _write_completed_analysis(
    data_root,
    *,
    project_id,
    job_id=None,
    source_seed="source",
    candidate_count=1,
    source_location=None,
):
    job_id = job_id or uuid4()
    snapshot_id = uuid4()
    hash_job_id = uuid4()
    source_sha256 = _digest(source_seed.encode("utf-8"))
    project_root = Path(data_root) / "projects" / str(project_id)
    records_root = project_root / "analysis-job-records"
    run_name = f"candidate-extraction-fixture-{job_id.hex[:8]}"
    artifact_root = project_root / "candidate-extractions" / run_name
    records_root.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)

    candidates = []
    total_artifact_bytes = 0
    artifact_paths = []
    for index in range(candidate_count):
        candidate_id = _digest(f"{job_id}:{index}".encode("ascii"))[:20]
        image_name = f"{candidate_id}.jpg"
        clip_name = f"{candidate_id}.mp4"
        image_bytes = b"\xff\xd8\xff" + bytes([index % 251]) * 29
        clip_bytes = b"\x00\x00\x00\x18ftypmp42" + bytes(
            [(index + 1) % 251]
        ) * 28
        image_path = artifact_root / image_name
        clip_path = artifact_root / clip_name
        image_path.write_bytes(image_bytes)
        clip_path.write_bytes(clip_bytes)
        artifact_paths.extend((image_path, clip_path))
        total_artifact_bytes += len(image_bytes) + len(clip_bytes)
        candidates.append(
            {
                "candidate_id": candidate_id,
                "start_seconds": float(index * 2),
                "end_seconds": float(index * 2 + 1),
                "representative_timestamp_seconds": float(index * 2 + 0.5),
                "scene_score": 0.8,
                "sharpness_score": 0.7,
                "triggers": ["scene_change"],
                "observations": [
                    {
                        "timestamp_seconds": float(index * 2 + 0.5),
                        "scene_score": 0.8,
                        "sharpness_score": 0.7,
                        "triggers": ["scene_change"],
                    }
                ],
                "representative_image": {
                    "relative_path": image_name,
                    "size_bytes": len(image_bytes),
                    "sha256": _digest(image_bytes),
                    "media_type": "image/jpeg",
                    "artifact_ref": f"analysis:{job_id}:{image_name}",
                },
                "preview_clip": {
                    "relative_path": clip_name,
                    "size_bytes": len(clip_bytes),
                    "sha256": _digest(clip_bytes),
                    "media_type": "video/mp4",
                    "artifact_ref": f"analysis:{job_id}:{clip_name}",
                },
                "preview_start_seconds": float(index * 2),
                "preview_duration_seconds": 1.0,
            }
        )

    created_at = datetime(2026, 7, 28, 7, 0, tzinfo=timezone.utc)
    payload = {
        "schema_version": "trafficvisson.analysis-job.v1",
        "job_id": str(job_id),
        "project_id": str(project_id),
        "snapshot_id": str(snapshot_id),
        "hash_job_id": str(hash_job_id),
        "file_name": f"{source_seed}.mp4",
        "source_size_bytes": 1_024 + candidate_count,
        "source_modified_time_ns": 123_456_789,
        "source_sha256": source_sha256,
        "status": "completed",
        "created_at": created_at.isoformat(),
        "updated_at": created_at.replace(minute=1).isoformat(),
        "artifact_directory_name": run_name,
        "result": {
            "artifact_set_id": str(job_id),
            "candidate_count": candidate_count,
            "artifact_count": candidate_count * 2,
            "total_artifact_bytes": total_artifact_bytes,
            "candidates": candidates,
            "provenance": {
                "source_name": f"{source_seed}.mp4",
                "source_size_bytes": 1_024 + candidate_count,
                "source_modified_time_ns": 123_456_789,
                "source_fingerprint": source_sha256,
                "source_sha256": source_sha256,
                "duration_seconds": 12.5 + candidate_count,
                "format_names": ["mov", "mp4"],
                "video_stream_index": 0,
                "video_codec": "h264",
                "width": 1_920,
                "height": 1_080,
                "frame_rate": "30/1",
                "ffmpeg_version": "fixture",
                "ffprobe_version": "fixture",
                "scene_score_method": "fixture",
                "sharpness_score_method": "fixture",
                "effective_cadence_seconds": 1.0,
                "minimum_selection_gap_seconds": 1.0,
                "limits": {"max_candidates": 100},
                "source_location": source_location,
            },
        },
    }
    encoded = _canonical_json(payload)
    record_path = records_root / f"{job_id}.json"
    digest_path = records_root / f"{job_id}.sha256"
    record_path.write_bytes(encoded)
    digest_path.write_text(_digest(encoded), encoding="ascii")
    return {
        "job_id": job_id,
        "snapshot_id": snapshot_id,
        "hash_job_id": hash_job_id,
        "source_sha256": source_sha256,
        "record_path": record_path,
        "digest_path": digest_path,
        "artifact_root": artifact_root,
        "artifact_paths": artifact_paths,
        "payload": payload,
    }


def _rows(database_path, query, parameters=()):
    with sqlite3.connect(str(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(query, parameters).fetchall()
        ]


def _seed_v1_database(database_path, project):
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(database_path), isolation_level=None) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        ProjectCatalog._apply_migration_v1(connection)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, applied_at)
            VALUES (1, ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        connection.execute(
            """
            INSERT INTO projects(
                id,
                name,
                description,
                question_policy,
                active_model_version,
                created_at,
                fixture
            ) VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (
                str(project["id"]),
                project["name"],
                project["description"],
                project["question_policy"],
                project["active_model_version"],
                project["created_at"].isoformat(),
            ),
        )
        inspection_id = uuid4()
        import_id = uuid4()
        connection.execute(
            """
            INSERT INTO import_inspections(
                id,
                project_id,
                source_type,
                local_selection_id,
                support_tier,
                source_display,
                expected_bytes,
                duration_seconds,
                rights_confirmation_required,
                inspection_status,
                warnings_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                str(inspection_id),
                str(project["id"]),
                "local_selection",
                str(uuid4()),
                "official",
                "선택 영상",
                1_024,
                12.5,
                "fixture_ready",
                "[]",
                project["created_at"].isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO imports(
                id,
                project_id,
                inspected_source_id,
                status,
                source_type,
                provenance_status,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(import_id),
                str(project["id"]),
                str(inspection_id),
                "queued",
                "local_selection",
                "pending_download_hash",
                project["created_at"].isoformat(),
            ),
        )
        connection.execute("COMMIT")
    return inspection_id, import_id


def test_v1_to_current_ordered_migration_preserves_existing_rows(tmp_path):
    database_path = tmp_path / "catalog.sqlite3"
    existing_project = _project(name="기존 v1 프로젝트")
    inspection_id, import_id = _seed_v1_database(
        database_path, existing_project
    )
    before = {
        "project": _rows(
            database_path,
            "SELECT * FROM projects WHERE id = ?",
            (str(existing_project["id"]),),
        ),
        "inspection": _rows(
            database_path,
            "SELECT * FROM import_inspections WHERE id = ?",
            (str(inspection_id),),
        ),
        "import": _rows(
            database_path,
            "SELECT * FROM imports WHERE id = ?",
            (str(import_id),),
        ),
    }

    ProjectCatalog(database_path, _project())

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
        (str(existing_project["id"]),),
    ) == before["project"]
    assert _rows(
        database_path,
        "SELECT * FROM import_inspections WHERE id = ?",
        (str(inspection_id),),
    ) == before["inspection"]
    assert _rows(
        database_path,
        "SELECT * FROM imports WHERE id = ?",
        (str(import_id),),
    ) == before["import"]
    table_names = {
        row["name"]
        for row in _rows(
            database_path,
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    assert {
        "media_assets",
        "trips",
        "analysis_jobs",
        "analysis_backfill_issues",
    }.issubset(table_names)


def test_future_schema_is_rejected_before_catalog_ddl(tmp_path):
    database_path = tmp_path / "future.sqlite3"
    with sqlite3.connect(str(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations(
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE TABLE sentinel(value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO sentinel(value) VALUES ('preserve')")
        for version in range(1, CATALOG_SCHEMA_VERSION + 2):
            connection.execute(
                """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (version, "future"),
            )
    before_schema = _rows(
        database_path,
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """,
    )

    with pytest.raises(RuntimeError, match="newer"):
        ProjectCatalog(database_path, _project())

    assert _rows(
        database_path,
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """,
    ) == before_schema
    assert _rows(database_path, "SELECT value FROM sentinel") == [
        {"value": "preserve"}
    ]


def test_v2_backfill_is_read_only_idempotent_and_preserves_provenance(
    tmp_path,
):
    default_project = _project()
    recovered_project_id = uuid4()
    with_location = _write_completed_analysis(
        tmp_path,
        project_id=recovered_project_id,
        source_seed="located",
        candidate_count=2,
        source_location={
            "latitude": 37.123456,
            "longitude": 127.123456,
            "altitude_meters": 42.0,
            "raw_iso6709": "+37.123456+127.123456+42.0/",
            "scope": "capture_start_only",
        },
    )
    missing_location = _write_completed_analysis(
        tmp_path,
        project_id=recovered_project_id,
        source_seed="missing",
        candidate_count=1,
        source_location=None,
    )
    protected_paths = [
        with_location["record_path"],
        with_location["digest_path"],
        *with_location["artifact_paths"],
        missing_location["record_path"],
        missing_location["digest_path"],
        *missing_location["artifact_paths"],
    ]
    protected = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in protected_paths
    }
    database_path = tmp_path / "catalog.sqlite3"

    first = ProjectCatalog(database_path, default_project)
    first_analysis_rows = _rows(
        database_path,
        "SELECT * FROM analysis_jobs ORDER BY id",
    )
    first_indexed_at = {
        row["id"]: row["indexed_at"] for row in first_analysis_rows
    }
    assert first.backfill_summary.scanned_records == 2
    assert first.backfill_summary.indexed_records == 2
    assert first.backfill_summary.isolated_records == 0
    assert len(_rows(database_path, "SELECT * FROM media_assets")) == 2
    assert len(_rows(database_path, "SELECT * FROM trips")) == 2
    assert len(first_analysis_rows) == 2
    by_job = {row["id"]: row for row in first_analysis_rows}
    for fixture in (with_location, missing_location):
        row = by_job[str(fixture["job_id"])]
        assert row["project_id"] == str(recovered_project_id)
        assert row["trip_id"] == str(fixture["job_id"])
        assert row["source_sha256"] == fixture["source_sha256"]
        assert (
            row["candidate_count"]
            == fixture["payload"]["result"]["candidate_count"]
        )
        assert (
            row["artifact_count"]
            == fixture["payload"]["result"]["artifact_count"]
        )
    trip_statuses = {
        row["source_analysis_job_id"]: row["gps_status"]
        for row in _rows(database_path, "SELECT * FROM trips")
    }
    assert trip_statuses[str(with_location["job_id"])] == "capture_start_only"
    assert trip_statuses[str(missing_location["job_id"])] == "missing"
    assert _rows(
        database_path,
        "SELECT name, fixture FROM projects WHERE id = ?",
        (str(recovered_project_id),),
    ) == [
        {
            "name": f"복구된 프로젝트 {recovered_project_id.hex[:8]}",
            "fixture": 0,
        }
    ]

    second = ProjectCatalog(database_path, default_project)

    assert second.backfill_summary.scanned_records == 2
    assert second.backfill_summary.indexed_records == 2
    assert second.backfill_summary.isolated_records == 0
    assert len(_rows(database_path, "SELECT * FROM media_assets")) == 2
    assert len(_rows(database_path, "SELECT * FROM trips")) == 2
    second_analysis_rows = _rows(
        database_path,
        "SELECT * FROM analysis_jobs ORDER BY id",
    )
    assert len(second_analysis_rows) == 2
    assert {
        row["id"]: row["indexed_at"] for row in second_analysis_rows
    } == first_indexed_at
    assert _rows(
        database_path,
        "SELECT version FROM schema_migrations ORDER BY version",
    ) == [
        {"version": version}
        for version in range(1, CATALOG_SCHEMA_VERSION + 1)
    ]
    assert _rows(
        database_path, "SELECT * FROM analysis_backfill_issues"
    ) == []
    for path, (expected_bytes, expected_mtime) in protected.items():
        assert path.read_bytes() == expected_bytes
        assert path.stat().st_mtime_ns == expected_mtime


def test_backfill_isolates_corrupt_records_without_blocking_good_siblings(
    tmp_path,
):
    project_id = uuid4()
    good = _write_completed_analysis(
        tmp_path,
        project_id=project_id,
        source_seed="good",
    )
    bad_digest = _write_completed_analysis(
        tmp_path,
        project_id=project_id,
        source_seed="bad-digest",
    )
    bad_digest["digest_path"].write_text("0" * 64, encoding="ascii")

    bad_counts = _write_completed_analysis(
        tmp_path,
        project_id=project_id,
        source_seed="bad-counts",
    )
    bad_count_payload = json.loads(
        bad_counts["record_path"].read_text(encoding="utf-8")
    )
    bad_count_payload["result"]["candidate_count"] = 99
    bad_count_bytes = _canonical_json(bad_count_payload)
    bad_counts["record_path"].write_bytes(bad_count_bytes)
    bad_counts["digest_path"].write_text(
        _digest(bad_count_bytes), encoding="ascii"
    )

    missing_directory = _write_completed_analysis(
        tmp_path,
        project_id=project_id,
        source_seed="missing-directory",
    )
    shutil.rmtree(missing_directory["artifact_root"])
    protected_records = {
        fixture["record_path"]: fixture["record_path"].read_bytes()
        for fixture in (
            good,
            bad_digest,
            bad_counts,
            missing_directory,
        )
    }
    database_path = tmp_path / "catalog.sqlite3"

    first = ProjectCatalog(database_path, _project())

    assert first.backfill_summary.scanned_records == 4
    assert first.backfill_summary.indexed_records == 1
    assert first.backfill_summary.isolated_records == 3
    assert _rows(database_path, "SELECT id FROM analysis_jobs") == [
        {"id": str(good["job_id"])}
    ]
    issues = {
        row["record_relative_path"]: row["error_code"]
        for row in _rows(
            database_path,
            """
            SELECT record_relative_path, error_code
            FROM analysis_backfill_issues
            """,
        )
    }
    assert issues[
        bad_digest["record_path"].relative_to(tmp_path).as_posix()
    ] == "digest_mismatch"
    assert issues[
        bad_counts["record_path"].relative_to(tmp_path).as_posix()
    ] == "invalid_counts"
    assert issues[
        missing_directory["record_path"].relative_to(tmp_path).as_posix()
    ] == "artifact_directory_missing"

    second = ProjectCatalog(database_path, _project())

    assert second.backfill_summary.indexed_records == 1
    assert second.backfill_summary.isolated_records == 3
    assert len(_rows(database_path, "SELECT * FROM analysis_jobs")) == 1
    assert (
        len(
            _rows(
                database_path,
                "SELECT * FROM analysis_backfill_issues",
            )
        )
        == 3
    )
    for path, expected_bytes in protected_records.items():
        assert path.read_bytes() == expected_bytes
