from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from .media_intake import (
    MediaFileRecord,
    MediaIntakeService,
    StableMediaSnapshotGrant,
)


SCHEMA_VERSION = "trafficvisson.stable-media-snapshot.v1"
MAX_RECORD_BYTES = 512 * 1024


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _record_payload(grant: StableMediaSnapshotGrant) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": str(grant.project_id),
        "snapshot_id": str(grant.snapshot_id),
        "directory": grant.directory,
        "directory_identity": grant.directory_identity,
        "snapshot_digest": grant.digest,
        "files": [
            {
                "name": item.name,
                "extension": item.extension,
                "size_bytes": item.size_bytes,
                "created_time_ns": item.created_time_ns,
                "modified_time_ns": item.modified_time_ns,
            }
            for item in grant.files
        ],
    }


def _with_record_hash(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "record_sha256": hashlib.sha256(_canonical_json(payload)).hexdigest(),
    }


class StableMediaSnapshotStore:
    """Private durable registry for user-approved stable folder metadata."""

    def __init__(self, data_root: Path) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("stable media snapshot data_root must be absolute")
        self._root = (root / "stable-media-snapshots").resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if (
            self._root.is_symlink()
            or not self._root.is_dir()
            or self._root.parent != root.resolve()
        ):
            raise ValueError("stable media snapshot root is invalid")

    def persist(
        self,
        media_intake: MediaIntakeService,
        project_id: UUID,
        snapshot_id: UUID,
    ) -> None:
        grant = media_intake.export_stable_snapshot_grant(
            project_id, snapshot_id
        )
        project_root = self._project_root(project_id, create=True)
        target = project_root / f"{snapshot_id}.json"
        payload = _with_record_hash(_record_payload(grant))
        encoded = _canonical_json(payload)
        if len(encoded) > MAX_RECORD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="The stable media snapshot record is too large",
            )
        temporary = project_root / f".{snapshot_id}.{os.getpid()}.tmp"
        temporary.write_bytes(encoded)
        os.replace(temporary, target)

    def restore_all(
        self, media_intake: MediaIntakeService
    ) -> dict[str, int]:
        summary = {"loaded": 0, "rejected": 0}
        for project_root in sorted(self._root.iterdir(), key=lambda p: p.name):
            if (
                not project_root.is_dir()
                or project_root.is_symlink()
                or project_root.parent.resolve() != self._root
            ):
                continue
            try:
                project_id = UUID(project_root.name)
            except ValueError:
                continue
            for record_path in sorted(
                project_root.glob("*.json"), key=lambda p: p.name
            ):
                try:
                    grant = self._read_grant(
                        project_id, record_path
                    )
                    media_intake.restore_stable_snapshot_grant(grant)
                except (HTTPException, OSError, ValueError):
                    summary["rejected"] += 1
                else:
                    summary["loaded"] += 1
        return summary

    def _project_root(self, project_id: UUID, *, create: bool) -> Path:
        if not isinstance(project_id, UUID):
            raise ValueError("project_id must be a UUID")
        project_root = self._root / str(project_id)
        if create:
            project_root.mkdir(exist_ok=True)
        if (
            project_root.is_symlink()
            or not project_root.is_dir()
            or project_root.parent.resolve() != self._root
        ):
            raise ValueError("stable media snapshot project root is invalid")
        return project_root

    def _read_grant(
        self, project_id: UUID, record_path: Path
    ) -> StableMediaSnapshotGrant:
        if (
            record_path.is_symlink()
            or record_path.parent.resolve() != self._project_root(
                project_id, create=False
            )
            or record_path.stat().st_size > MAX_RECORD_BYTES
        ):
            raise ValueError("stable media snapshot record is invalid")
        raw = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("stable media snapshot record is invalid")
        expected_keys = {
            "schema_version",
            "project_id",
            "snapshot_id",
            "directory",
            "directory_identity",
            "snapshot_digest",
            "files",
            "record_sha256",
        }
        if set(raw) != expected_keys or raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError("stable media snapshot record is invalid")
        record_sha256 = raw.pop("record_sha256")
        if (
            not isinstance(record_sha256, str)
            or hashlib.sha256(_canonical_json(raw)).hexdigest()
            != record_sha256
            or raw["project_id"] != str(project_id)
        ):
            raise ValueError("stable media snapshot record integrity failed")
        snapshot_id = UUID(str(raw["snapshot_id"]))
        if record_path.stem != str(snapshot_id):
            raise ValueError("stable media snapshot filename is invalid")
        files_raw = raw["files"]
        if not isinstance(files_raw, list):
            raise ValueError("stable media snapshot files are invalid")
        files = []
        for item in files_raw:
            if not isinstance(item, dict) or set(item) != {
                "name",
                "extension",
                "size_bytes",
                "created_time_ns",
                "modified_time_ns",
            }:
                raise ValueError("stable media snapshot file is invalid")
            files.append(MediaFileRecord(**item))
        return StableMediaSnapshotGrant(
            project_id=project_id,
            snapshot_id=snapshot_id,
            directory=raw["directory"],
            directory_identity=raw["directory_identity"],
            digest=raw["snapshot_digest"],
            files=tuple(files),
        )

