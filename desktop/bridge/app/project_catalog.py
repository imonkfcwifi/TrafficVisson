from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat as stat_module
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import UUID, uuid5

from .dataset_versions import (
    DEFAULT_VALIDATION_FRACTION,
    DatasetVersionError,
    LabelProjection,
    LabelProjectionError,
    build_dataset_snapshot,
    normalize_label_record,
)


CATALOG_SCHEMA_VERSION = 4
PERSISTED_ANALYSIS_SCHEMA = "trafficvisson.analysis-job.v1"
MAX_PERSISTED_JOB_RECORD_BYTES = 16 * 1024 * 1024
MAX_BACKFILL_RECORDS = 4_096
MAX_BACKFILL_CANDIDATES = 2_000

_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID_PATTERN = re.compile(r"^[a-f0-9]{20}$")
_ARTIFACT_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$"
)
_ISO_6709_LOCATION_PATTERN = re.compile(
    r"^[+-][0-9]{2}(?:\.[0-9]{1,12})?"
    r"[+-][0-9]{3}(?:\.[0-9]{1,12})?"
    r"(?:[+-][0-9]{1,6}(?:\.[0-9]{1,6})?)?/$"
)
_VIDEO_SUFFIXES = frozenset(
    {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mts", ".webm"}
)
_ARTIFACT_CONTRACTS = {
    "representative_image": ("image/jpeg", ".jpg"),
    "preview_clip": ("video/mp4", ".mp4"),
}


@dataclass(frozen=True)
class BackfillSummary:
    scanned_records: int
    indexed_records: int
    isolated_records: int


@dataclass(frozen=True)
class _RecoveredAnalysis:
    project_id: UUID
    job_id: UUID
    snapshot_id: UUID
    hash_job_id: UUID
    file_name: str
    source_sha256: str
    source_size_bytes: int
    source_modified_time_ns: int
    duration_seconds: float
    source_location: Optional[Dict]
    gps_status: str
    record_relative_path: str
    record_sha256: str
    artifact_directory_relative_path: str
    candidate_count: int
    artifact_count: int
    total_artifact_bytes: int
    created_at: datetime
    updated_at: datetime

    @property
    def media_asset_id(self) -> UUID:
        return uuid5(
            self.project_id,
            f"trafficvisson-media:{self.source_sha256}",
        )

    @property
    def trip_id(self) -> UUID:
        # Existing exports already use analysis job identity as their legacy
        # trip identity. Preserve it instead of rewriting 51 completed jobs.
        return self.job_id


class _BackfillValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProjectCatalog:
    """Durable project metadata plus a read-only index of completed analyses.

    Large media, immutable analysis JSON, evidence artifacts, and the
    append-only label audit remain in their existing project directories.
    The v2 catalog only indexes verified records; it never moves or rewrites
    those files.
    """

    def __init__(self, database_path: Path, default_project: Dict) -> None:
        self._database_path = database_path.resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._projects_root = self._database_path.parent / "projects"
        self._lock = RLock()
        self._migrate()
        self._ensure_default_project(default_project)
        self._backfill_summary = self.backfill_completed_analysis_records()

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def backfill_summary(self) -> BackfillSummary:
        return self._backfill_summary

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._database_path),
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _migration_versions(
        connection: sqlite3.Connection,
    ) -> List[int]:
        table = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            """
        ).fetchone()
        if table is None:
            return []
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version ASC"
        ).fetchall()
        versions = [int(row["version"]) for row in rows]
        expected = list(range(1, (versions[-1] if versions else 0) + 1))
        if versions != expected:
            raise RuntimeError("Project catalog migration history is invalid")
        return versions

    def _migrate(self) -> None:
        migrations = {
            1: self._apply_migration_v1,
            2: self._apply_migration_v2,
            3: self._apply_migration_v3,
            4: self._apply_migration_v4,
        }
        with self._lock, self._connect() as connection:
            # Inspect the existing version before applying any catalog DDL.
            versions = self._migration_versions(connection)
            current = versions[-1] if versions else 0
            if current > CATALOG_SCHEMA_VERSION:
                raise RuntimeError(
                    "Project catalog schema is newer than this bridge"
                )

            for version in range(current + 1, CATALOG_SCHEMA_VERSION + 1):
                migration = migrations.get(version)
                if migration is None:
                    raise RuntimeError(
                        f"Project catalog migration {version} is unavailable"
                    )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migration(connection)
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(version, applied_at)
                        VALUES (?, ?)
                        """,
                        (version, datetime.now(timezone.utc).isoformat()),
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise

    @staticmethod
    def _apply_migration_v1(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                question_policy TEXT NOT NULL,
                active_model_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                fixture INTEGER NOT NULL CHECK (fixture IN (0, 1))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS import_inspections (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                local_selection_id TEXT,
                support_tier TEXT NOT NULL,
                source_display TEXT NOT NULL,
                expected_bytes INTEGER,
                duration_seconds REAL,
                rights_confirmation_required INTEGER NOT NULL
                    CHECK (rights_confirmation_required IN (0, 1)),
                inspection_status TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_import_inspections_project
            ON import_inspections(project_id, created_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS imports (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                inspected_source_id TEXT NOT NULL,
                status TEXT NOT NULL,
                source_type TEXT NOT NULL,
                provenance_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (inspected_source_id)
                    REFERENCES import_inspections(id)
                    ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_imports_project
            ON imports(project_id, created_at)
            """
        )

    @staticmethod
    def _apply_migration_v2(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS media_assets (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                source_size_bytes INTEGER NOT NULL
                    CHECK (source_size_bytes > 0),
                source_modified_time_ns INTEGER NOT NULL
                    CHECK (source_modified_time_ns >= 0),
                duration_seconds REAL NOT NULL
                    CHECK (duration_seconds > 0),
                source_location_json TEXT,
                gps_status TEXT NOT NULL
                    CHECK (gps_status IN ('capture_start_only', 'missing')),
                storage_state TEXT NOT NULL,
                provenance_basis TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
                    ON DELETE CASCADE,
                UNIQUE (project_id, source_sha256)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_media_assets_project
            ON media_assets(project_id, created_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trips (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                video_asset_id TEXT NOT NULL,
                source_analysis_job_id TEXT NOT NULL UNIQUE,
                duration_seconds REAL NOT NULL
                    CHECK (duration_seconds > 0),
                gps_status TEXT NOT NULL
                    CHECK (gps_status IN ('capture_start_only', 'missing')),
                capture_start_location_json TEXT,
                recovery_state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (video_asset_id) REFERENCES media_assets(id)
                    ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trips_project
            ON trips(project_id, created_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                trip_id TEXT NOT NULL,
                media_asset_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                hash_job_id TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                persisted_schema_version TEXT NOT NULL,
                record_relative_path TEXT NOT NULL UNIQUE,
                record_sha256 TEXT NOT NULL,
                artifact_directory_relative_path TEXT NOT NULL,
                candidate_count INTEGER NOT NULL
                    CHECK (candidate_count >= 0),
                artifact_count INTEGER NOT NULL
                    CHECK (artifact_count >= 0),
                total_artifact_bytes INTEGER NOT NULL
                    CHECK (total_artifact_bytes >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (trip_id) REFERENCES trips(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (media_asset_id) REFERENCES media_assets(id)
                    ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_analysis_jobs_project_updated
            ON analysis_jobs(project_id, updated_at)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_backfill_issues (
                record_relative_path TEXT PRIMARY KEY,
                project_id TEXT,
                error_code TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _apply_migration_v3(connection: sqlite3.Connection) -> None:
        # Composite parent keys keep every downstream label and dataset row
        # inside the owning project even though legacy IDs are globally unique.
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_analysis_jobs_project_identity
            ON analysis_jobs(project_id, id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS label_questions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                analysis_job_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                ai_original_candidate_json TEXT NOT NULL,
                ai_original_candidate_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (project_id, id),
                UNIQUE (project_id, analysis_job_id, candidate_id),
                FOREIGN KEY (project_id) REFERENCES projects(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (project_id, analysis_job_id)
                    REFERENCES analysis_jobs(project_id, id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_label_questions_project_job
            ON label_questions(project_id, analysis_job_id, candidate_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS label_answers (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                answer_revision INTEGER NOT NULL
                    CHECK (answer_revision > 0),
                answer TEXT NOT NULL
                    CHECK (
                        answer IN ('yes', 'edit', 'no', 'unsure', 'skipped')
                    ),
                partition TEXT NOT NULL
                    CHECK (
                        partition IN (
                            'approved', 'negative', 'held', 'excluded'
                        )
                    ),
                include_in_training INTEGER NOT NULL
                    CHECK (include_in_training IN (0, 1)),
                selected_issue_type TEXT,
                severity TEXT,
                maintenance_action TEXT,
                ocr_text TEXT,
                normalized_box_json TEXT,
                answered_by TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                previous_record_hash TEXT,
                record_hash TEXT NOT NULL,
                record_json TEXT NOT NULL,
                UNIQUE (project_id, record_id),
                UNIQUE (project_id, question_id, answer_revision),
                UNIQUE (project_id, record_hash),
                FOREIGN KEY (project_id, question_id)
                    REFERENCES label_questions(project_id, id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_label_answers_project_question
            ON label_answers(project_id, question_id, answer_revision)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS annotations (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                answer_record_id TEXT NOT NULL,
                answer_revision INTEGER NOT NULL
                    CHECK (answer_revision > 0),
                analysis_job_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                partition TEXT NOT NULL
                    CHECK (
                        partition IN (
                            'approved', 'negative', 'held', 'excluded'
                        )
                    ),
                include_in_training INTEGER NOT NULL
                    CHECK (include_in_training IN (0, 1)),
                selected_issue_type TEXT,
                severity TEXT,
                maintenance_action TEXT,
                ocr_text TEXT,
                normalized_box_json TEXT,
                source_record_hash TEXT NOT NULL,
                annotation_hash TEXT NOT NULL,
                annotation_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (project_id, id),
                UNIQUE (project_id, question_id, answer_revision),
                UNIQUE (project_id, answer_record_id),
                FOREIGN KEY (project_id, question_id)
                    REFERENCES label_questions(project_id, id)
                    ON DELETE CASCADE,
                FOREIGN KEY (project_id, answer_record_id)
                    REFERENCES label_answers(project_id, record_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (project_id, analysis_job_id)
                    REFERENCES analysis_jobs(project_id, id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_annotations_project_question
            ON annotations(project_id, question_id, answer_revision)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_versions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                dataset_hash TEXT NOT NULL,
                split_strategy TEXT NOT NULL,
                validation_fraction REAL NOT NULL
                    CHECK (
                        validation_fraction > 0
                        AND validation_fraction < 1
                    ),
                source_answer_count INTEGER NOT NULL
                    CHECK (source_answer_count >= 0),
                training_member_count INTEGER NOT NULL
                    CHECK (training_member_count >= 0),
                train_count INTEGER NOT NULL
                    CHECK (train_count >= 0),
                validation_count INTEGER NOT NULL
                    CHECK (validation_count >= 0),
                excluded_count INTEGER NOT NULL
                    CHECK (excluded_count >= 0),
                manifest_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (project_id, id),
                UNIQUE (project_id, dataset_hash),
                FOREIGN KEY (project_id) REFERENCES projects(id)
                    ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dataset_versions_project_created
            ON dataset_versions(project_id, created_at, id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_memberships (
                dataset_version_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                annotation_id TEXT NOT NULL,
                member_order INTEGER NOT NULL
                    CHECK (member_order >= 0),
                split TEXT NOT NULL
                    CHECK (split IN ('train', 'validation', 'excluded')),
                include_in_training INTEGER NOT NULL
                    CHECK (include_in_training IN (0, 1)),
                source_record_hash TEXT NOT NULL,
                annotation_hash TEXT NOT NULL,
                PRIMARY KEY (dataset_version_id, annotation_id),
                UNIQUE (dataset_version_id, member_order),
                FOREIGN KEY (project_id, dataset_version_id)
                    REFERENCES dataset_versions(project_id, id)
                    ON DELETE CASCADE,
                FOREIGN KEY (project_id, annotation_id)
                    REFERENCES annotations(project_id, id)
                    ON DELETE RESTRICT
            )
            """
        )
        for table_name in ("dataset_versions", "dataset_memberships"):
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS immutable_{table_name}_update
                BEFORE UPDATE ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} are immutable');
                END
                """
            )
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS immutable_{table_name}_delete
                BEFORE DELETE ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} are immutable');
                END
                """
            )

    @staticmethod
    def _apply_migration_v4(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                analysis_job_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('active', 'superseded')),
                issue_type TEXT NOT NULL,
                severity TEXT,
                maintenance_action TEXT,
                ocr_text TEXT,
                answer_revision INTEGER NOT NULL
                    CHECK (answer_revision > 0),
                source_answer_record_id TEXT NOT NULL,
                source_record_hash TEXT NOT NULL,
                evidence_image_ref TEXT NOT NULL,
                preview_clip_ref TEXT NOT NULL,
                start_time_ms INTEGER NOT NULL
                    CHECK (start_time_ms >= 0),
                representative_time_ms INTEGER NOT NULL
                    CHECK (representative_time_ms >= start_time_ms),
                end_time_ms INTEGER NOT NULL
                    CHECK (end_time_ms >= representative_time_ms),
                created_at TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                superseded_at TEXT,
                superseded_by_answer_revision INTEGER
                    CHECK (
                        superseded_by_answer_revision IS NULL
                        OR superseded_by_answer_revision > 0
                    ),
                superseded_by_answer_record_id TEXT,
                superseded_by_record_hash TEXT,
                CHECK (id = question_id),
                CHECK (
                    (
                        status = 'active'
                        AND superseded_at IS NULL
                        AND superseded_by_answer_revision IS NULL
                        AND superseded_by_answer_record_id IS NULL
                        AND superseded_by_record_hash IS NULL
                    )
                    OR
                    (
                        status = 'superseded'
                        AND superseded_at IS NOT NULL
                        AND superseded_by_answer_revision IS NOT NULL
                        AND superseded_by_answer_revision > answer_revision
                        AND superseded_by_answer_record_id IS NOT NULL
                        AND superseded_by_record_hash IS NOT NULL
                    )
                ),
                UNIQUE (project_id, id),
                UNIQUE (project_id, question_id),
                UNIQUE (project_id, analysis_job_id, candidate_id),
                FOREIGN KEY (project_id) REFERENCES projects(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (project_id, question_id)
                    REFERENCES label_questions(project_id, id)
                    ON DELETE CASCADE,
                FOREIGN KEY (project_id, analysis_job_id)
                    REFERENCES analysis_jobs(project_id, id)
                    ON DELETE CASCADE,
                FOREIGN KEY (project_id, source_answer_record_id)
                    REFERENCES label_answers(project_id, record_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (project_id, superseded_by_answer_record_id)
                    REFERENCES label_answers(project_id, record_id)
                    ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_incidents_project_status_time
            ON incidents(
                project_id,
                status,
                representative_time_ms,
                id
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_incidents_project_job
            ON incidents(project_id, analysis_job_id, candidate_id)
            """
        )

    def _ensure_default_project(self, project: Dict) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO projects(
                        id,
                        name,
                        description,
                        question_policy,
                        active_model_version,
                        created_at,
                        fixture
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._project_values(project),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _project_values(project: Dict) -> tuple:
        created_at = project["created_at"]
        if not isinstance(created_at, datetime):
            raise TypeError("Project created_at must be a datetime")
        return (
            str(project["id"]),
            project["name"],
            project.get("description"),
            project["question_policy"],
            project["active_model_version"],
            created_at.isoformat(),
            1 if project["fixture"] else 0,
        )

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Dict:
        return {
            "id": UUID(row["id"]),
            "name": row["name"],
            "description": row["description"],
            "question_policy": row["question_policy"],
            "active_model_version": row["active_model_version"],
            "created_at": datetime.fromisoformat(row["created_at"]),
            "fixture": bool(row["fixture"]),
        }

    def list_projects(self) -> List[Dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM projects
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [self._project_from_row(row) for row in rows]

    def get_project(self, project_id: UUID) -> Optional[Dict]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?",
                (str(project_id),),
            ).fetchone()
        return self._project_from_row(row) if row is not None else None

    def create_project(self, project: Dict) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._project_values(project),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def save_import_inspection(self, record: Dict) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(record["inspected_source_id"]),
                        str(record["project_id"]),
                        record["source_type"],
                        (
                            str(record["local_selection_id"])
                            if record.get("local_selection_id")
                            else None
                        ),
                        record["support_tier"],
                        record["source_display"],
                        record.get("expected_bytes"),
                        record.get("duration_seconds"),
                        1 if record["rights_confirmation_required"] else 0,
                        record["inspection_status"],
                        json.dumps(
                            record.get("warnings", []),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        record["created_at"].isoformat(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def get_import_inspection(
        self, inspection_id: UUID
    ) -> Optional[Dict]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM import_inspections
                WHERE id = ?
                """,
                (str(inspection_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "project_id": UUID(row["project_id"]),
            "source_type": row["source_type"],
            "local_selection_id": (
                UUID(row["local_selection_id"])
                if row["local_selection_id"]
                else None
            ),
            "inspected_source_id": UUID(row["id"]),
            "support_tier": row["support_tier"],
            "source_display": row["source_display"],
            "expected_bytes": row["expected_bytes"],
            "duration_seconds": row["duration_seconds"],
            "rights_confirmation_required": bool(
                row["rights_confirmation_required"]
            ),
            "inspection_status": row["inspection_status"],
            "warnings": json.loads(row["warnings_json"]),
            "created_at": datetime.fromisoformat(row["created_at"]),
        }

    def save_import(self, record: Dict) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
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
                        str(record["import_id"]),
                        str(record["project_id"]),
                        str(record["inspected_source_id"]),
                        record["status"],
                        record["source_type"],
                        record["provenance_status"],
                        record["created_at"].isoformat(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def count_imports(self, project_id: UUID) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM imports WHERE project_id = ?",
                (str(project_id),),
            ).fetchone()
        return int(row["count"])

    def project_label_record(
        self,
        project_id: UUID | str,
        record: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Idempotently project one validated JSONL audit record."""

        return self.project_label_records(project_id, [record])[0]

    def project_label_records(
        self,
        project_id: UUID | str,
        records: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Atomically project JSONL records in their original append order."""

        project_text = _canonical_uuid_text(project_id, "project_id")
        if isinstance(records, (str, bytes)) or not isinstance(
            records, Sequence
        ):
            raise LabelProjectionError("records must be a sequence")
        projections = [normalize_label_record(record) for record in records]
        if any(item.project_id != project_text for item in projections):
            raise LabelProjectionError(
                "label record crossed the project boundary"
            )
        if not projections:
            return []

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if (
                    connection.execute(
                        "SELECT 1 FROM projects WHERE id = ?",
                        (project_text,),
                    ).fetchone()
                    is None
                ):
                    raise LabelProjectionError("project is unavailable")
                results = [
                    self._project_label_projection(
                        connection, project_text, projection
                    )
                    for projection in projections
                ]
                connection.execute("COMMIT")
                return results
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _project_label_projection(
        connection: sqlite3.Connection,
        project_id: str,
        projection: LabelProjection,
    ) -> Dict[str, Any]:
        job = connection.execute(
            """
            SELECT project_id, status
            FROM analysis_jobs
            WHERE id = ?
            """,
            (projection.analysis_job_id,),
        ).fetchone()
        if (
            job is None
            or job["project_id"] != project_id
            or job["status"] != "completed"
        ):
            raise LabelProjectionError(
                "analysis job is not a completed job in this project"
            )

        existing_record = connection.execute(
            """
            SELECT project_id, record_json
            FROM label_answers
            WHERE record_id = ?
            """,
            (projection.record_id,),
        ).fetchone()
        if existing_record is not None:
            if (
                existing_record["project_id"] != project_id
                or existing_record["record_json"] != projection.record_json
            ):
                raise LabelProjectionError(
                    "record_id is already bound to different audit data"
                )
            ProjectCatalog._sync_incident_projection(
                connection, project_id, projection
            )
            return _projection_result(projection, created=False)

        existing_revision = connection.execute(
            """
            SELECT record_json
            FROM label_answers
            WHERE project_id = ?
              AND question_id = ?
              AND answer_revision = ?
            """,
            (
                project_id,
                projection.question_id,
                projection.answer_revision,
            ),
        ).fetchone()
        if existing_revision is not None:
            if existing_revision["record_json"] != projection.record_json:
                raise LabelProjectionError(
                    "answer revision is already bound to another record"
                )
            ProjectCatalog._sync_incident_projection(
                connection, project_id, projection
            )
            return _projection_result(projection, created=False)

        question = connection.execute(
            """
            SELECT project_id, analysis_job_id, candidate_id,
                   ai_original_candidate_sha256
            FROM label_questions
            WHERE id = ?
            """,
            (projection.question_id,),
        ).fetchone()
        if question is None:
            if projection.answer_revision != 1:
                raise LabelProjectionError(
                    "the first projected answer revision must be 1"
                )
            connection.execute(
                """
                INSERT INTO label_questions(
                    id,
                    project_id,
                    analysis_job_id,
                    candidate_id,
                    ai_original_candidate_json,
                    ai_original_candidate_sha256,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    projection.question_id,
                    project_id,
                    projection.analysis_job_id,
                    projection.candidate_id,
                    projection.candidate_json,
                    projection.candidate_sha256,
                    projection.recorded_at,
                ),
            )
            current_revision = 0
        else:
            if (
                question["project_id"] != project_id
                or question["analysis_job_id"] != projection.analysis_job_id
                or question["candidate_id"] != projection.candidate_id
                or question["ai_original_candidate_sha256"]
                != projection.candidate_sha256
            ):
                raise LabelProjectionError(
                    "question identity or candidate evidence changed"
                )
            revision_row = connection.execute(
                """
                SELECT MAX(answer_revision) AS current_revision
                FROM label_answers
                WHERE project_id = ? AND question_id = ?
                """,
                (project_id, projection.question_id),
            ).fetchone()
            current_revision = int(revision_row["current_revision"] or 0)
        if projection.answer_revision != current_revision + 1:
            raise LabelProjectionError(
                "answer revisions must be projected without gaps"
            )

        previous = connection.execute(
            """
            SELECT record_hash
            FROM label_answers
            WHERE project_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        expected_previous = previous["record_hash"] if previous else None
        if projection.previous_record_hash != expected_previous:
            raise LabelProjectionError(
                "JSONL record chain does not match the projected audit order"
            )

        connection.execute(
            """
            INSERT INTO label_answers(
                record_id,
                project_id,
                question_id,
                answer_revision,
                answer,
                partition,
                include_in_training,
                selected_issue_type,
                severity,
                maintenance_action,
                ocr_text,
                normalized_box_json,
                answered_by,
                recorded_at,
                previous_record_hash,
                record_hash,
                record_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                projection.record_id,
                project_id,
                projection.question_id,
                projection.answer_revision,
                projection.answer,
                projection.partition,
                1 if projection.include_in_training else 0,
                projection.selected_issue_type,
                projection.severity,
                projection.maintenance_action,
                projection.ocr_text,
                projection.normalized_box_json,
                projection.answered_by,
                projection.recorded_at,
                projection.previous_record_hash,
                projection.record_hash,
                projection.record_json,
            ),
        )
        connection.execute(
            """
            INSERT INTO annotations(
                id,
                project_id,
                question_id,
                answer_record_id,
                answer_revision,
                analysis_job_id,
                candidate_id,
                partition,
                include_in_training,
                selected_issue_type,
                severity,
                maintenance_action,
                ocr_text,
                normalized_box_json,
                source_record_hash,
                annotation_hash,
                annotation_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                projection.annotation_id,
                project_id,
                projection.question_id,
                projection.record_id,
                projection.answer_revision,
                projection.analysis_job_id,
                projection.candidate_id,
                projection.partition,
                1 if projection.include_in_training else 0,
                projection.selected_issue_type,
                projection.severity,
                projection.maintenance_action,
                projection.ocr_text,
                projection.normalized_box_json,
                projection.record_hash,
                projection.annotation_hash,
                projection.annotation_json,
                projection.recorded_at,
            ),
        )
        ProjectCatalog._sync_incident_projection(
            connection, project_id, projection
        )
        return _projection_result(projection, created=True)

    @staticmethod
    def _sync_incident_projection(
        connection: sqlite3.Connection,
        project_id: str,
        projection: LabelProjection,
    ) -> None:
        """Project the latest answer state without replacing the JSONL audit."""

        # Existing reviewed_incidents.csv rows already use question identity as
        # incident identity. Preserve that deterministic contract across every
        # answer revision instead of introducing a second event identifier.
        incident_id = projection.question_id
        existing = connection.execute(
            """
            SELECT *
            FROM incidents
            WHERE project_id = ? AND question_id = ?
            """,
            (project_id, projection.question_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["id"] != incident_id
                or existing["analysis_job_id"] != projection.analysis_job_id
                or existing["candidate_id"] != projection.candidate_id
            ):
                raise LabelProjectionError(
                    "incident identity changed for the label question"
                )
            approved_revision = int(existing["answer_revision"])
            superseded_revision = int(
                existing["superseded_by_answer_revision"] or 0
            )
            state_revision = max(approved_revision, superseded_revision)
            if state_revision > projection.answer_revision:
                return
            if (
                state_revision == projection.answer_revision
                and (
                    existing["source_record_hash"]
                    if existing["status"] == "active"
                    else existing["superseded_by_record_hash"]
                )
                != projection.record_hash
            ):
                raise LabelProjectionError(
                    "incident revision is bound to another audit record"
                )

        approved = (
            projection.partition == "approved"
            and projection.answer in {"yes", "edit"}
        )
        if not approved:
            if existing is None:
                return
            connection.execute(
                """
                UPDATE incidents
                SET status = 'superseded',
                    updated_at = ?,
                    superseded_at = ?,
                    superseded_by_answer_revision = ?,
                    superseded_by_answer_record_id = ?,
                    superseded_by_record_hash = ?
                WHERE project_id = ? AND id = ?
                """,
                (
                    projection.recorded_at,
                    projection.recorded_at,
                    projection.answer_revision,
                    projection.record_id,
                    projection.record_hash,
                    project_id,
                    incident_id,
                ),
            )
            return

        if projection.selected_issue_type is None:
            raise LabelProjectionError(
                "approved incident projection requires an issue type"
            )
        evidence = _incident_candidate_fields(projection)
        if existing is None:
            connection.execute(
                """
                INSERT INTO incidents(
                    id,
                    project_id,
                    question_id,
                    analysis_job_id,
                    candidate_id,
                    status,
                    issue_type,
                    severity,
                    maintenance_action,
                    ocr_text,
                    answer_revision,
                    source_answer_record_id,
                    source_record_hash,
                    evidence_image_ref,
                    preview_clip_ref,
                    start_time_ms,
                    representative_time_ms,
                    end_time_ms,
                    created_at,
                    activated_at,
                    updated_at,
                    superseded_at,
                    superseded_by_answer_revision,
                    superseded_by_answer_record_id,
                    superseded_by_record_hash
                ) VALUES (
                    ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL
                )
                """,
                (
                    incident_id,
                    project_id,
                    projection.question_id,
                    projection.analysis_job_id,
                    projection.candidate_id,
                    projection.selected_issue_type,
                    projection.severity,
                    projection.maintenance_action,
                    projection.ocr_text,
                    projection.answer_revision,
                    projection.record_id,
                    projection.record_hash,
                    evidence["evidence_image_ref"],
                    evidence["preview_clip_ref"],
                    evidence["start_time_ms"],
                    evidence["representative_time_ms"],
                    evidence["end_time_ms"],
                    projection.recorded_at,
                    projection.recorded_at,
                    projection.recorded_at,
                ),
            )
            return

        connection.execute(
            """
            UPDATE incidents
            SET status = 'active',
                issue_type = ?,
                severity = ?,
                maintenance_action = ?,
                ocr_text = ?,
                answer_revision = ?,
                source_answer_record_id = ?,
                source_record_hash = ?,
                evidence_image_ref = ?,
                preview_clip_ref = ?,
                start_time_ms = ?,
                representative_time_ms = ?,
                end_time_ms = ?,
                activated_at = ?,
                updated_at = ?,
                superseded_at = NULL,
                superseded_by_answer_revision = NULL,
                superseded_by_answer_record_id = NULL,
                superseded_by_record_hash = NULL
            WHERE project_id = ? AND id = ?
            """,
            (
                projection.selected_issue_type,
                projection.severity,
                projection.maintenance_action,
                projection.ocr_text,
                projection.answer_revision,
                projection.record_id,
                projection.record_hash,
                evidence["evidence_image_ref"],
                evidence["preview_clip_ref"],
                evidence["start_time_ms"],
                evidence["representative_time_ms"],
                evidence["end_time_ms"],
                projection.recorded_at,
                projection.recorded_at,
                project_id,
                incident_id,
            ),
        )

    def list_incidents(
        self,
        project_id: UUID | str,
        *,
        include_inactive: bool = False,
        analysis_job_id: Optional[UUID | str] = None,
    ) -> List[Dict[str, Any]]:
        project_text = _canonical_uuid_text(project_id, "project_id")
        clauses = ["project_id = ?"]
        parameters: List[Any] = [project_text]
        if not include_inactive:
            clauses.append("status = 'active'")
        if analysis_job_id is not None:
            clauses.append("analysis_job_id = ?")
            parameters.append(
                _canonical_uuid_text(analysis_job_id, "analysis_job_id")
            )
        with self._lock, self._connect() as connection:
            self._require_project(connection, project_text)
            rows = connection.execute(
                f"""
                SELECT *
                FROM incidents
                WHERE {" AND ".join(clauses)}
                ORDER BY representative_time_ms, id
                """,
                parameters,
            ).fetchall()
        return [
            {
                "incident_id": row["id"],
                "project_id": row["project_id"],
                "question_id": row["question_id"],
                "analysis_job_id": row["analysis_job_id"],
                "candidate_id": row["candidate_id"],
                "status": row["status"],
                "issue_type": row["issue_type"],
                "severity": row["severity"],
                "maintenance_action": row["maintenance_action"],
                "ocr_text": row["ocr_text"],
                "answer_revision": int(row["answer_revision"]),
                "source_answer_record_id": row[
                    "source_answer_record_id"
                ],
                "source_record_hash": row["source_record_hash"],
                "evidence_image_ref": row["evidence_image_ref"],
                "preview_clip_ref": row["preview_clip_ref"],
                "start_time_ms": int(row["start_time_ms"]),
                "representative_time_ms": int(
                    row["representative_time_ms"]
                ),
                "end_time_ms": int(row["end_time_ms"]),
                "created_at": row["created_at"],
                "activated_at": row["activated_at"],
                "updated_at": row["updated_at"],
                "superseded_at": row["superseded_at"],
                "superseded_by_answer_revision": (
                    int(row["superseded_by_answer_revision"])
                    if row["superseded_by_answer_revision"] is not None
                    else None
                ),
                "superseded_by_answer_record_id": row[
                    "superseded_by_answer_record_id"
                ],
                "superseded_by_record_hash": row[
                    "superseded_by_record_hash"
                ],
            }
            for row in rows
        ]

    def current_label_stats(
        self, project_id: UUID | str
    ) -> Dict[str, Any]:
        project_text = _canonical_uuid_text(project_id, "project_id")
        with self._lock, self._connect() as connection:
            self._require_project(connection, project_text)
            question_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM label_questions
                    WHERE project_id = ?
                    """,
                    (project_text,),
                ).fetchone()["count"]
            )
            revision_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM label_answers
                    WHERE project_id = ?
                    """,
                    (project_text,),
                ).fetchone()["count"]
            )
            current = self._current_annotation_rows(
                connection, project_text
            )
        partitions = {
            name: sum(row["partition"] == name for row in current)
            for name in ("approved", "negative", "held", "excluded")
        }
        return {
            "project_id": project_text,
            "question_count": question_count,
            "answer_revision_count": revision_count,
            "current_answer_count": len(current),
            "training_included_count": sum(
                bool(row["include_in_training"]) for row in current
            ),
            "source_analysis_job_count": len(
                {row["analysis_job_id"] for row in current}
            ),
            "partitions": partitions,
        }

    def list_label_questions(
        self,
        project_id: UUID | str,
        *,
        analysis_job_id: Optional[UUID | str] = None,
    ) -> List[Dict[str, Any]]:
        project_text = _canonical_uuid_text(project_id, "project_id")
        parameters: List[Any] = [project_text]
        job_filter = ""
        if analysis_job_id is not None:
            job_filter = "AND q.analysis_job_id = ?"
            parameters.append(
                _canonical_uuid_text(analysis_job_id, "analysis_job_id")
            )
        with self._lock, self._connect() as connection:
            self._require_project(connection, project_text)
            rows = connection.execute(
                f"""
                WITH latest AS (
                    SELECT project_id, question_id,
                           MAX(answer_revision) AS answer_revision
                    FROM label_answers
                    WHERE project_id = ?
                    GROUP BY project_id, question_id
                )
                SELECT q.*, a.record_id AS current_record_id,
                       a.answer_revision AS current_answer_revision,
                       a.partition AS current_partition,
                       a.include_in_training AS current_training_inclusion
                FROM label_questions AS q
                LEFT JOIN latest AS l
                  ON l.project_id = q.project_id
                 AND l.question_id = q.id
                LEFT JOIN label_answers AS a
                  ON a.project_id = l.project_id
                 AND a.question_id = l.question_id
                 AND a.answer_revision = l.answer_revision
                WHERE q.project_id = ?
                  {job_filter}
                ORDER BY q.created_at, q.id
                """,
                [project_text, *parameters],
            ).fetchall()
        return [
            {
                "question_id": row["id"],
                "project_id": row["project_id"],
                "analysis_job_id": row["analysis_job_id"],
                "candidate_id": row["candidate_id"],
                "ai_original_candidate": json.loads(
                    row["ai_original_candidate_json"]
                ),
                "ai_original_candidate_sha256": row[
                    "ai_original_candidate_sha256"
                ],
                "created_at": row["created_at"],
                "current_record_id": row["current_record_id"],
                "current_answer_revision": row["current_answer_revision"],
                "current_partition": row["current_partition"],
                "current_training_inclusion": (
                    bool(row["current_training_inclusion"])
                    if row["current_training_inclusion"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    def list_label_answers(
        self,
        project_id: UUID | str,
        *,
        latest_only: bool = False,
    ) -> List[Dict[str, Any]]:
        project_text = _canonical_uuid_text(project_id, "project_id")
        with self._lock, self._connect() as connection:
            self._require_project(connection, project_text)
            if latest_only:
                rows = connection.execute(
                    """
                    WITH latest AS (
                        SELECT question_id,
                               MAX(answer_revision) AS answer_revision
                        FROM label_answers
                        WHERE project_id = ?
                        GROUP BY question_id
                    )
                    SELECT a.record_json
                    FROM label_answers AS a
                    JOIN latest AS l
                      ON l.question_id = a.question_id
                     AND l.answer_revision = a.answer_revision
                    WHERE a.project_id = ?
                    ORDER BY a.sequence
                    """,
                    (project_text, project_text),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT record_json
                    FROM label_answers
                    WHERE project_id = ?
                    ORDER BY sequence
                    """,
                    (project_text,),
                ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def list_current_annotations(
        self, project_id: UUID | str
    ) -> List[Dict[str, Any]]:
        project_text = _canonical_uuid_text(project_id, "project_id")
        with self._lock, self._connect() as connection:
            self._require_project(connection, project_text)
            rows = self._current_annotation_rows(connection, project_text)
        return [self._annotation_from_row(row) for row in rows]

    @staticmethod
    def _current_annotation_rows(
        connection: sqlite3.Connection, project_id: str
    ) -> List[sqlite3.Row]:
        return connection.execute(
            """
            WITH latest AS (
                SELECT question_id, MAX(answer_revision) AS answer_revision
                FROM label_answers
                WHERE project_id = ?
                GROUP BY question_id
            )
            SELECT a.*
            FROM annotations AS a
            JOIN latest AS l
              ON l.question_id = a.question_id
             AND l.answer_revision = a.answer_revision
            WHERE a.project_id = ?
            ORDER BY a.question_id, a.answer_revision, a.id
            """,
            (project_id, project_id),
        ).fetchall()

    @staticmethod
    def _annotation_from_row(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "annotation_id": row["id"],
            "project_id": row["project_id"],
            "question_id": row["question_id"],
            "answer_record_id": row["answer_record_id"],
            "answer_revision": int(row["answer_revision"]),
            "analysis_job_id": row["analysis_job_id"],
            "candidate_id": row["candidate_id"],
            "partition": row["partition"],
            "include_in_training": bool(row["include_in_training"]),
            "selected_issue_type": row["selected_issue_type"],
            "severity": row["severity"],
            "maintenance_action": row["maintenance_action"],
            "ocr_text": row["ocr_text"],
            "normalized_box": (
                json.loads(row["normalized_box_json"])
                if row["normalized_box_json"] is not None
                else None
            ),
            "source_record_hash": row["source_record_hash"],
            "annotation_hash": row["annotation_hash"],
            "created_at": row["created_at"],
        }

    def create_dataset_version(
        self,
        project_id: UUID | str,
        *,
        validation_fraction: float = DEFAULT_VALIDATION_FRACTION,
    ) -> Dict[str, Any]:
        """Create or reuse the immutable snapshot for the current labels."""

        project_text = _canonical_uuid_text(project_id, "project_id")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._require_project(connection, project_text)
                current_rows = [
                    self._annotation_from_row(row)
                    for row in self._current_annotation_rows(
                        connection, project_text
                    )
                ]
                snapshot = build_dataset_snapshot(
                    project_text,
                    current_rows,
                    validation_fraction=validation_fraction,
                )
                existing = connection.execute(
                    """
                    SELECT *
                    FROM dataset_versions
                    WHERE project_id = ? AND dataset_hash = ?
                    """,
                    (project_text, snapshot.dataset_hash),
                ).fetchone()
                if existing is not None:
                    memberships = self._dataset_membership_rows(
                        connection, project_text, existing["id"]
                    )
                    connection.execute("COMMIT")
                    return self._dataset_from_rows(
                        existing, memberships, reused=True
                    )

                created_at = datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z")
                connection.execute(
                    """
                    INSERT INTO dataset_versions(
                        id,
                        project_id,
                        schema_version,
                        dataset_hash,
                        split_strategy,
                        validation_fraction,
                        source_answer_count,
                        training_member_count,
                        train_count,
                        validation_count,
                        excluded_count,
                        manifest_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.dataset_id,
                        project_text,
                        json.loads(snapshot.manifest_json)["schema_version"],
                        snapshot.dataset_hash,
                        json.loads(snapshot.manifest_json)["split_strategy"],
                        snapshot.validation_fraction,
                        snapshot.source_answer_count,
                        snapshot.training_member_count,
                        snapshot.train_count,
                        snapshot.validation_count,
                        snapshot.excluded_count,
                        snapshot.manifest_json,
                        created_at,
                    ),
                )
                for index, member in enumerate(snapshot.members):
                    connection.execute(
                        """
                        INSERT INTO dataset_memberships(
                            dataset_version_id,
                            project_id,
                            annotation_id,
                            member_order,
                            split,
                            include_in_training,
                            source_record_hash,
                            annotation_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot.dataset_id,
                            project_text,
                            member.annotation_id,
                            index,
                            member.split,
                            1 if member.include_in_training else 0,
                            member.source_record_hash,
                            member.annotation_hash,
                        ),
                    )
                row = connection.execute(
                    """
                    SELECT *
                    FROM dataset_versions
                    WHERE project_id = ? AND id = ?
                    """,
                    (project_text, snapshot.dataset_id),
                ).fetchone()
                memberships = self._dataset_membership_rows(
                    connection, project_text, snapshot.dataset_id
                )
                connection.execute("COMMIT")
                return self._dataset_from_rows(
                    row, memberships, reused=False
                )
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_dataset_versions(
        self, project_id: UUID | str
    ) -> List[Dict[str, Any]]:
        project_text = _canonical_uuid_text(project_id, "project_id")
        with self._lock, self._connect() as connection:
            self._require_project(connection, project_text)
            rows = connection.execute(
                """
                SELECT *
                FROM dataset_versions
                WHERE project_id = ?
                ORDER BY created_at, id
                """,
                (project_text,),
            ).fetchall()
            values = [
                self._dataset_from_rows(
                    row,
                    self._dataset_membership_rows(
                        connection, project_text, row["id"]
                    ),
                    reused=False,
                )
                for row in rows
            ]
        return values

    @staticmethod
    def _dataset_membership_rows(
        connection: sqlite3.Connection,
        project_id: str,
        dataset_id: str,
    ) -> List[sqlite3.Row]:
        return connection.execute(
            """
            SELECT m.*, a.question_id, a.analysis_job_id, a.candidate_id,
                   a.answer_record_id, a.answer_revision, a.partition
            FROM dataset_memberships AS m
            JOIN annotations AS a
              ON a.project_id = m.project_id
             AND a.id = m.annotation_id
            WHERE m.project_id = ? AND m.dataset_version_id = ?
            ORDER BY m.member_order
            """,
            (project_id, dataset_id),
        ).fetchall()

    @staticmethod
    def _dataset_from_rows(
        row: Mapping[str, Any],
        memberships: Sequence[Mapping[str, Any]],
        *,
        reused: bool,
    ) -> Dict[str, Any]:
        return {
            "dataset_version_id": row["id"],
            "project_id": row["project_id"],
            "schema_version": row["schema_version"],
            "dataset_hash": row["dataset_hash"],
            "split_strategy": row["split_strategy"],
            "validation_fraction": float(row["validation_fraction"]),
            "source_answer_count": int(row["source_answer_count"]),
            "training_member_count": int(row["training_member_count"]),
            "train_count": int(row["train_count"]),
            "validation_count": int(row["validation_count"]),
            "excluded_count": int(row["excluded_count"]),
            "manifest": json.loads(row["manifest_json"]),
            "created_at": row["created_at"],
            "reused": reused,
            "memberships": [
                {
                    "annotation_id": member["annotation_id"],
                    "question_id": member["question_id"],
                    "analysis_job_id": member["analysis_job_id"],
                    "candidate_id": member["candidate_id"],
                    "answer_record_id": member["answer_record_id"],
                    "answer_revision": int(member["answer_revision"]),
                    "partition": member["partition"],
                    "include_in_training": bool(
                        member["include_in_training"]
                    ),
                    "split": member["split"],
                    "source_record_hash": member["source_record_hash"],
                    "annotation_hash": member["annotation_hash"],
                }
                for member in memberships
            ],
        }

    @staticmethod
    def _require_project(
        connection: sqlite3.Connection, project_id: str
    ) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            is None
        ):
            raise DatasetVersionError("project is unavailable")

    def backfill_completed_analysis_records(self) -> BackfillSummary:
        """Index valid immutable v1 records without changing any source file."""

        recovered: List[_RecoveredAnalysis] = []
        issues: List[tuple[str, Optional[str], str]] = []
        scanned = 0
        for project_id, record_path, relative_path in self._record_candidates():
            scanned += 1
            try:
                recovered.append(
                    self._read_recovered_analysis(
                        project_id,
                        record_path,
                        relative_path,
                    )
                )
            except _BackfillValidationError as error:
                issues.append((relative_path, str(project_id), error.code))
            except OSError:
                issues.append((relative_path, str(project_id), "io_error"))

        indexed = 0
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for index, item in enumerate(recovered):
                    savepoint = f"analysis_backfill_{index}"
                    connection.execute(f"SAVEPOINT {savepoint}")
                    try:
                        accepted = self._index_recovered_analysis(
                            connection, item
                        )
                    except sqlite3.IntegrityError:
                        accepted = False
                    if accepted:
                        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                        indexed += 1
                        connection.execute(
                            """
                            DELETE FROM analysis_backfill_issues
                            WHERE record_relative_path = ?
                            """,
                            (item.record_relative_path,),
                        )
                    else:
                        connection.execute(
                            f"ROLLBACK TO SAVEPOINT {savepoint}"
                        )
                        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                        issues.append(
                            (
                                item.record_relative_path,
                                str(item.project_id),
                                "catalog_conflict",
                            )
                        )
                observed_at = datetime.now(timezone.utc).isoformat()
                for relative_path, project_id, error_code in issues:
                    connection.execute(
                        """
                        INSERT INTO analysis_backfill_issues(
                            record_relative_path,
                            project_id,
                            error_code,
                            observed_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(record_relative_path) DO UPDATE SET
                            project_id = excluded.project_id,
                            error_code = excluded.error_code,
                            observed_at = excluded.observed_at
                        """,
                        (
                            relative_path,
                            project_id,
                            error_code,
                            observed_at,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        summary = BackfillSummary(
            scanned_records=scanned,
            indexed_records=indexed,
            isolated_records=len(issues),
        )
        self._backfill_summary = summary
        return summary

    def _record_candidates(
        self,
    ) -> Sequence[tuple[UUID, Path, str]]:
        if not self._projects_root.exists():
            return ()
        if (
            not self._projects_root.is_dir()
            or self._projects_root.is_symlink()
            or _is_reparse_point(self._projects_root)
        ):
            return ()
        candidates: List[tuple[UUID, Path, str]] = []
        try:
            project_entries = sorted(
                os.scandir(self._projects_root),
                key=lambda entry: entry.name,
            )
        except OSError:
            return ()
        for project_entry in project_entries:
            if len(candidates) >= MAX_BACKFILL_RECORDS:
                break
            if (
                not project_entry.is_dir(follow_symlinks=False)
                or project_entry.is_symlink()
            ):
                continue
            try:
                project_id = UUID(project_entry.name)
            except ValueError:
                continue
            project_root = Path(project_entry.path)
            if _is_reparse_point(project_root):
                continue
            records_root = project_root / "analysis-job-records"
            if (
                not records_root.is_dir()
                or records_root.is_symlink()
                or _is_reparse_point(records_root)
            ):
                continue
            try:
                entries = sorted(
                    os.scandir(records_root),
                    key=lambda entry: entry.name,
                )
            except OSError:
                continue
            for entry in entries:
                if len(candidates) >= MAX_BACKFILL_RECORDS:
                    break
                if (
                    not entry.name.endswith(".json")
                    or entry.name.startswith(".")
                    or entry.is_symlink()
                    or not entry.is_file(follow_symlinks=False)
                ):
                    continue
                path = Path(entry.path)
                if _is_reparse_point(path):
                    continue
                relative_path = path.relative_to(
                    self._database_path.parent
                ).as_posix()
                candidates.append((project_id, path, relative_path))
        return tuple(candidates)

    def _read_recovered_analysis(
        self,
        expected_project_id: UUID,
        record_path: Path,
        relative_path: str,
    ) -> _RecoveredAnalysis:
        try:
            record_stat = record_path.stat()
        except OSError as error:
            raise _BackfillValidationError("record_unavailable") from error
        if (
            not stat_module.S_ISREG(record_stat.st_mode)
            or record_stat.st_size <= 0
            or record_stat.st_size > MAX_PERSISTED_JOB_RECORD_BYTES
        ):
            raise _BackfillValidationError("record_size_invalid")
        digest_path = record_path.with_suffix(".sha256")
        if (
            not digest_path.is_file()
            or digest_path.is_symlink()
            or _is_reparse_point(digest_path)
        ):
            raise _BackfillValidationError("digest_missing")
        try:
            raw = record_path.read_bytes()
            expected_digest = digest_path.read_text(
                encoding="ascii", errors="strict"
            ).strip()
        except (OSError, UnicodeError) as error:
            raise _BackfillValidationError("record_unreadable") from error
        actual_digest = hashlib.sha256(raw).hexdigest()
        if (
            not _SHA256_PATTERN.fullmatch(expected_digest)
            or expected_digest != actual_digest
        ):
            raise _BackfillValidationError("digest_mismatch")
        try:
            decoded = json.loads(
                raw.decode("utf-8", errors="strict"),
                parse_constant=_reject_nonfinite_json,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise _BackfillValidationError("invalid_json") from error
        if not isinstance(decoded, Mapping):
            raise _BackfillValidationError("invalid_record")
        if (
            decoded.get("schema_version") != PERSISTED_ANALYSIS_SCHEMA
            or decoded.get("status") != "completed"
        ):
            raise _BackfillValidationError("unsupported_record")
        try:
            project_id = UUID(str(decoded.get("project_id")))
            job_id = UUID(str(decoded.get("job_id")))
            snapshot_id = UUID(str(decoded.get("snapshot_id")))
            hash_job_id = UUID(str(decoded.get("hash_job_id")))
        except ValueError as error:
            raise _BackfillValidationError("invalid_identity") from error
        if (
            project_id != expected_project_id
            or record_path.name != f"{job_id}.json"
        ):
            raise _BackfillValidationError("identity_mismatch")

        file_name = decoded.get("file_name")
        source_sha256 = decoded.get("source_sha256")
        source_size_bytes = decoded.get("source_size_bytes")
        source_modified_time_ns = decoded.get("source_modified_time_ns")
        if (
            not isinstance(file_name, str)
            or not 1 <= len(file_name) <= 255
            or Path(file_name).name != file_name
            or Path(file_name).suffix.casefold() not in _VIDEO_SUFFIXES
            or not isinstance(source_sha256, str)
            or not _SHA256_PATTERN.fullmatch(source_sha256)
            or isinstance(source_size_bytes, bool)
            or not isinstance(source_size_bytes, int)
            or source_size_bytes <= 0
            or isinstance(source_modified_time_ns, bool)
            or not isinstance(source_modified_time_ns, int)
            or source_modified_time_ns < 0
        ):
            raise _BackfillValidationError("invalid_source")
        created_at = _parse_timestamp(decoded.get("created_at"))
        updated_at = _parse_timestamp(decoded.get("updated_at"))
        if updated_at < created_at:
            raise _BackfillValidationError("invalid_timestamps")

        result = decoded.get("result")
        if not isinstance(result, Mapping):
            raise _BackfillValidationError("invalid_result")
        if str(result.get("artifact_set_id")) != str(job_id):
            raise _BackfillValidationError("result_identity_mismatch")
        candidates = result.get("candidates")
        candidate_count = result.get("candidate_count")
        artifact_count = result.get("artifact_count")
        total_artifact_bytes = result.get("total_artifact_bytes")
        if (
            not isinstance(candidates, list)
            or len(candidates) > MAX_BACKFILL_CANDIDATES
            or isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count != len(candidates)
            or isinstance(artifact_count, bool)
            or not isinstance(artifact_count, int)
            or artifact_count != candidate_count * 2
            or isinstance(total_artifact_bytes, bool)
            or not isinstance(total_artifact_bytes, int)
            or total_artifact_bytes < 0
        ):
            raise _BackfillValidationError("invalid_counts")

        provenance = result.get("provenance")
        if not isinstance(provenance, Mapping):
            raise _BackfillValidationError("invalid_provenance")
        duration_seconds = provenance.get("duration_seconds")
        if not _positive_finite(duration_seconds):
            raise _BackfillValidationError("invalid_duration")
        source_location = _validate_source_location(
            provenance.get("source_location")
        )
        gps_status = (
            "capture_start_only" if source_location is not None else "missing"
        )

        artifact_directory_name = decoded.get("artifact_directory_name")
        if (
            not isinstance(artifact_directory_name, str)
            or not artifact_directory_name.startswith("candidate-extraction-")
            or not _ARTIFACT_NAME_PATTERN.fullmatch(artifact_directory_name)
        ):
            raise _BackfillValidationError("invalid_artifact_directory")
        project_root = self._projects_root / str(project_id)
        artifact_directory = (
            project_root / "candidate-extractions" / artifact_directory_name
        )
        if (
            not artifact_directory.is_dir()
            or artifact_directory.is_symlink()
            or _is_reparse_point(artifact_directory)
        ):
            raise _BackfillValidationError("artifact_directory_missing")
        calculated_artifact_bytes = _validate_candidate_artifacts(
            candidates,
            job_id,
            artifact_directory,
        )
        if calculated_artifact_bytes != total_artifact_bytes:
            raise _BackfillValidationError("artifact_bytes_mismatch")

        artifact_relative_path = artifact_directory.relative_to(
            self._database_path.parent
        ).as_posix()
        return _RecoveredAnalysis(
            project_id=project_id,
            job_id=job_id,
            snapshot_id=snapshot_id,
            hash_job_id=hash_job_id,
            file_name=file_name,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
            source_modified_time_ns=source_modified_time_ns,
            duration_seconds=float(duration_seconds),
            source_location=source_location,
            gps_status=gps_status,
            record_relative_path=relative_path,
            record_sha256=actual_digest,
            artifact_directory_relative_path=artifact_relative_path,
            candidate_count=candidate_count,
            artifact_count=artifact_count,
            total_artifact_bytes=total_artifact_bytes,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _index_recovered_analysis(
        self,
        connection: sqlite3.Connection,
        item: _RecoveredAnalysis,
    ) -> bool:
        existing = connection.execute(
            """
            SELECT
                project_id,
                source_sha256,
                record_relative_path,
                record_sha256,
                candidate_count,
                artifact_count
            FROM analysis_jobs
            WHERE id = ?
            """,
            (str(item.job_id),),
        ).fetchone()
        if existing is not None:
            return (
                existing["project_id"] == str(item.project_id)
                and existing["source_sha256"] == item.source_sha256
                and existing["record_relative_path"]
                == item.record_relative_path
                and existing["record_sha256"] == item.record_sha256
                and int(existing["candidate_count"]) == item.candidate_count
                and int(existing["artifact_count"]) == item.artifact_count
            )

        connection.execute(
            """
            INSERT OR IGNORE INTO projects(
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
                str(item.project_id),
                f"복구된 프로젝트 {item.project_id.hex[:8]}",
                "기존 완료 분석 기록에서 복구된 프로젝트",
                "learning_assist",
                "common-initial@1.0.0",
                item.created_at.isoformat(),
            ),
        )
        location_json = (
            json.dumps(
                item.source_location,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if item.source_location is not None
            else None
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO media_assets(
                id,
                project_id,
                source_name,
                source_sha256,
                source_size_bytes,
                source_modified_time_ns,
                duration_seconds,
                source_location_json,
                gps_status,
                storage_state,
                provenance_basis,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.media_asset_id),
                str(item.project_id),
                item.file_name,
                item.source_sha256,
                item.source_size_bytes,
                item.source_modified_time_ns,
                item.duration_seconds,
                location_json,
                item.gps_status,
                "external_source_not_retained",
                PERSISTED_ANALYSIS_SCHEMA,
                item.created_at.isoformat(),
            ),
        )
        media_row = connection.execute(
            """
            SELECT project_id, source_sha256
            FROM media_assets
            WHERE id = ?
            """,
            (str(item.media_asset_id),),
        ).fetchone()
        if (
            media_row is None
            or media_row["project_id"] != str(item.project_id)
            or media_row["source_sha256"] != item.source_sha256
        ):
            return False

        connection.execute(
            """
            INSERT OR IGNORE INTO trips(
                id,
                project_id,
                video_asset_id,
                source_analysis_job_id,
                duration_seconds,
                gps_status,
                capture_start_location_json,
                recovery_state,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.trip_id),
                str(item.project_id),
                str(item.media_asset_id),
                str(item.job_id),
                item.duration_seconds,
                item.gps_status,
                location_json,
                "legacy_completed_analysis",
                item.created_at.isoformat(),
            ),
        )
        trip_row = connection.execute(
            """
            SELECT project_id, video_asset_id, source_analysis_job_id
            FROM trips
            WHERE id = ?
            """,
            (str(item.trip_id),),
        ).fetchone()
        if (
            trip_row is None
            or trip_row["project_id"] != str(item.project_id)
            or trip_row["video_asset_id"] != str(item.media_asset_id)
            or trip_row["source_analysis_job_id"] != str(item.job_id)
        ):
            return False

        connection.execute(
            """
            INSERT INTO analysis_jobs(
                id,
                project_id,
                trip_id,
                media_asset_id,
                snapshot_id,
                hash_job_id,
                source_sha256,
                status,
                persisted_schema_version,
                record_relative_path,
                record_sha256,
                artifact_directory_relative_path,
                candidate_count,
                artifact_count,
                total_artifact_bytes,
                created_at,
                updated_at,
                indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.job_id),
                str(item.project_id),
                str(item.trip_id),
                str(item.media_asset_id),
                str(item.snapshot_id),
                str(item.hash_job_id),
                item.source_sha256,
                "completed",
                PERSISTED_ANALYSIS_SCHEMA,
                item.record_relative_path,
                item.record_sha256,
                item.artifact_directory_relative_path,
                item.candidate_count,
                item.artifact_count,
                item.total_artifact_bytes,
                item.created_at.isoformat(),
                item.updated_at.isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return True


def _canonical_uuid_text(value: UUID | str, label: str) -> str:
    try:
        parsed = value if isinstance(value, UUID) else UUID(value)
    except (ValueError, AttributeError, TypeError) as error:
        raise LabelProjectionError(f"{label} must be a UUID") from error
    text = str(parsed)
    if isinstance(value, str) and value != text:
        raise LabelProjectionError(f"{label} must be canonical")
    return text


def _incident_candidate_fields(
    projection: LabelProjection,
) -> Dict[str, Any]:
    try:
        candidate = json.loads(projection.candidate_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise LabelProjectionError(
            "incident candidate metadata is invalid"
        ) from error
    if not isinstance(candidate, Mapping):
        raise LabelProjectionError("incident candidate metadata is invalid")

    expected_prefix = f"analysis:{projection.analysis_job_id}:"

    def artifact_ref(key: str, media_type: str) -> str:
        artifact = candidate.get(key)
        if not isinstance(artifact, Mapping):
            raise LabelProjectionError("incident evidence metadata is missing")
        reference = artifact.get("artifact_ref")
        relative_path = artifact.get("relative_path")
        if (
            not isinstance(reference, str)
            or len(reference) > 512
            or not reference.startswith(expected_prefix)
            or not isinstance(relative_path, str)
            or not _ARTIFACT_NAME_PATTERN.fullmatch(relative_path)
            or reference != f"{expected_prefix}{relative_path}"
            or artifact.get("media_type") != media_type
        ):
            raise LabelProjectionError("incident evidence reference is invalid")
        return reference

    def timestamp_ms(key: str) -> int:
        value = candidate.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 2_678_400
        ):
            raise LabelProjectionError("incident timestamp is invalid")
        return round(float(value) * 1000)

    start_time_ms = timestamp_ms("start_seconds")
    representative_time_ms = timestamp_ms(
        "representative_timestamp_seconds"
    )
    end_time_ms = timestamp_ms("end_seconds")
    if not start_time_ms <= representative_time_ms <= end_time_ms:
        raise LabelProjectionError("incident timestamps are out of order")
    return {
        "evidence_image_ref": artifact_ref(
            "representative_image", "image/jpeg"
        ),
        "preview_clip_ref": artifact_ref("preview_clip", "video/mp4"),
        "start_time_ms": start_time_ms,
        "representative_time_ms": representative_time_ms,
        "end_time_ms": end_time_ms,
    }


def _projection_result(
    projection: LabelProjection, *, created: bool
) -> Dict[str, Any]:
    return {
        "record_id": projection.record_id,
        "project_id": projection.project_id,
        "analysis_job_id": projection.analysis_job_id,
        "candidate_id": projection.candidate_id,
        "question_id": projection.question_id,
        "answer_revision": projection.answer_revision,
        "partition": projection.partition,
        "include_in_training": projection.include_in_training,
        "annotation_id": projection.annotation_id,
        "annotation_hash": projection.annotation_hash,
        "record_hash": projection.record_hash,
        "created": created,
    }


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _reject_nonfinite_json(_: str) -> object:
    raise ValueError("non-finite JSON number")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise _BackfillValidationError("invalid_timestamps")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _BackfillValidationError("invalid_timestamps") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _BackfillValidationError("invalid_timestamps")
    return parsed.astimezone(timezone.utc)


def _positive_finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _validate_source_location(value: object) -> Optional[Dict]:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "latitude",
        "longitude",
        "altitude_meters",
        "raw_iso6709",
        "scope",
    }:
        raise _BackfillValidationError("invalid_source_location")
    latitude = value.get("latitude")
    longitude = value.get("longitude")
    altitude = value.get("altitude_meters")
    if (
        isinstance(latitude, bool)
        or not isinstance(latitude, (int, float))
        or not math.isfinite(float(latitude))
        or not -90 <= float(latitude) <= 90
        or isinstance(longitude, bool)
        or not isinstance(longitude, (int, float))
        or not math.isfinite(float(longitude))
        or not -180 <= float(longitude) <= 180
        or (float(latitude) == 0 and float(longitude) == 0)
        or (
            altitude is not None
            and (
                isinstance(altitude, bool)
                or not isinstance(altitude, (int, float))
                or not math.isfinite(float(altitude))
                or not -20_000 <= float(altitude) <= 20_000
            )
        )
        or not isinstance(value.get("raw_iso6709"), str)
        or not _ISO_6709_LOCATION_PATTERN.fullmatch(
            value["raw_iso6709"]
        )
        or value.get("scope") != "capture_start_only"
    ):
        raise _BackfillValidationError("invalid_source_location")
    return {
        "latitude": float(latitude),
        "longitude": float(longitude),
        "altitude_meters": (
            float(altitude) if altitude is not None else None
        ),
        "raw_iso6709": value["raw_iso6709"],
        "scope": "capture_start_only",
    }


def _validate_candidate_artifacts(
    candidates: Sequence[object],
    job_id: UUID,
    artifact_directory: Path,
) -> int:
    candidate_ids: set[str] = set()
    total_bytes = 0
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise _BackfillValidationError("invalid_candidate")
        candidate_id = candidate.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or not _CANDIDATE_ID_PATTERN.fullmatch(candidate_id)
            or candidate_id in candidate_ids
        ):
            raise _BackfillValidationError("invalid_candidate")
        candidate_ids.add(candidate_id)
        for numeric_field in (
            "start_seconds",
            "end_seconds",
            "representative_timestamp_seconds",
            "scene_score",
            "sharpness_score",
            "preview_start_seconds",
            "preview_duration_seconds",
        ):
            value = candidate.get(numeric_field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise _BackfillValidationError("invalid_candidate")
        if (
            float(candidate["start_seconds"]) < 0
            or float(candidate["end_seconds"])
            < float(candidate["start_seconds"])
            or not 0 <= float(candidate["scene_score"]) <= 1
            or not 0 <= float(candidate["sharpness_score"]) <= 1
        ):
            raise _BackfillValidationError("invalid_candidate")
        for field_name, (media_type, suffix) in _ARTIFACT_CONTRACTS.items():
            metadata = candidate.get(field_name)
            if not isinstance(metadata, Mapping):
                raise _BackfillValidationError("invalid_artifact")
            relative_path = metadata.get("relative_path")
            size_bytes = metadata.get("size_bytes")
            sha256_value = metadata.get("sha256")
            if (
                not isinstance(relative_path, str)
                or not _ARTIFACT_NAME_PATTERN.fullmatch(relative_path)
                or Path(relative_path).suffix.casefold() != suffix
                or isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes <= 0
                or not isinstance(sha256_value, str)
                or not _SHA256_PATTERN.fullmatch(sha256_value)
                or metadata.get("media_type") != media_type
                or metadata.get("artifact_ref")
                != f"analysis:{job_id}:{relative_path}"
            ):
                raise _BackfillValidationError("invalid_artifact")
            total_bytes += size_bytes
    return total_bytes
