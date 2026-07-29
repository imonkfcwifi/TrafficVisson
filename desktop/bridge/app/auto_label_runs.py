"""Durable, review-only audit records for command-driven pre-label runs.

The store records which already-provenanced model proposals matched a compiled
operator command.  It never creates a human answer, Annotation, DatasetVersion,
incident, or training member.  Every stored match is therefore forced to
``review_only=true`` and ``include_in_training=false``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from uuid import UUID, uuid4

from .candidate_labels import (
    CandidateLabelError,
    ISSUE_TYPES as CANDIDATE_ISSUE_TYPES,
    canonical_issue_type,
)


RUN_SCHEMA = "trafficvisson.auto-label-run.v1"
MAX_MATCHES = 512
MAX_RECORD_BYTES = 4 * 1024 * 1024
# Keep the run-store contract aligned with the product-wide twelve canonical
# maintenance classes plus the explicit ``other`` operator extension.
ISSUE_TYPES = frozenset(CANDIDATE_ISSUE_TYPES)

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")


class AutoLabelRunError(ValueError):
    """An auto-label run record is invalid or unavailable."""


class AutoLabelRunConflict(AutoLabelRunError):
    """A run id was reused for different immutable content."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AutoLabelRunError("run record must be finite JSON") from error


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _uuid(value: Any, field_name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise AutoLabelRunError(f"{field_name} must be a UUID") from error


def _digest(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise AutoLabelRunError(f"{field_name} must be a lowercase SHA-256")
    return value


def _issue_type(value: Any, field_name: str) -> str:
    """Accept a legacy ingress alias but return only its canonical code."""

    try:
        return canonical_issue_type(value)
    except CandidateLabelError as error:
        raise AutoLabelRunError(f"{field_name} is invalid") from error


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _require_real_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise AutoLabelRunError(f"{label} is unavailable") from error
    if path.is_symlink() or _is_reparse_point(path) or not resolved.is_dir():
        raise AutoLabelRunError(f"{label} is unsafe")
    return resolved


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise AutoLabelRunError("recorded_at must be an ISO-8601 timestamp")
    source = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(source)
    except ValueError as error:
        raise AutoLabelRunError(
            "recorded_at must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutoLabelRunError("recorded_at must include a timezone")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _exact_keys(
    value: Mapping[str, Any],
    expected: Sequence[str],
    field_name: str,
) -> None:
    if set(value) != set(expected):
        raise AutoLabelRunError(
            f"{field_name} has missing or unknown fields"
        )


def _normalise_matches(
    *,
    project_id: str,
    scope: str,
    job_id: Optional[str],
    issue_type: str,
    matches: Any,
) -> List[Dict[str, Any]]:
    if (
        not isinstance(matches, list)
        or len(matches) > MAX_MATCHES
    ):
        raise AutoLabelRunError("matches must be a bounded list")
    normalised: List[Dict[str, Any]] = []
    seen = set()
    expected_keys = (
        "project_id",
        "analysis_job_id",
        "candidate_id",
        "issue_type",
        "review_status",
        "decision_hash",
    )
    for raw in matches:
        if not isinstance(raw, Mapping):
            raise AutoLabelRunError("auto-label match must be an object")
        _exact_keys(raw, expected_keys, "auto-label match")
        match_project = _uuid(raw["project_id"], "match.project_id")
        match_job = _uuid(raw["analysis_job_id"], "match.analysis_job_id")
        candidate_id = raw["candidate_id"]
        match_issue = _issue_type(
            raw["issue_type"],
            "match.issue_type",
        )
        review_status = raw["review_status"]
        if match_project != project_id:
            raise AutoLabelRunError("auto-label match crossed projects")
        if scope == "current_job" and match_job != job_id:
            raise AutoLabelRunError("auto-label match crossed analysis jobs")
        if (
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID.fullmatch(candidate_id) is None
        ):
            raise AutoLabelRunError("match.candidate_id is invalid")
        if match_issue != issue_type:
            raise AutoLabelRunError("auto-label match changed issue type")
        if review_status not in {"pending", "auto_accepted"}:
            raise AutoLabelRunError("match.review_status is invalid")
        identity = (match_job, candidate_id)
        if identity in seen:
            raise AutoLabelRunError("auto-label matches contain duplicates")
        seen.add(identity)
        normalised.append(
            {
                "project_id": match_project,
                "analysis_job_id": match_job,
                "candidate_id": candidate_id,
                "issue_type": match_issue,
                "review_status": review_status,
                "decision_hash": _digest(
                    raw["decision_hash"],
                    "match.decision_hash",
                ),
                "review_only": True,
                "include_in_training": False,
            }
        )
    return sorted(
        normalised,
        key=lambda row: (
            row["analysis_job_id"],
            row["candidate_id"],
            row["issue_type"],
        ),
    )


def _normalise_record(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AutoLabelRunError("auto-label run must be an object")
    expected_keys = (
        "schema_version",
        "run_id",
        "project_id",
        "recipe_id",
        "command_sha256",
        "scope",
        "mode_requested",
        "mode_applied",
        "issue_type",
        "job_id",
        "recorded_at",
        "review_only",
        "include_in_training",
        "match_count",
        "matches",
        "run_sha256",
    )
    _exact_keys(value, expected_keys, "auto-label run")
    if value["schema_version"] != RUN_SCHEMA:
        raise AutoLabelRunError("auto-label run schema is unsupported")
    run_id = _uuid(value["run_id"], "run_id")
    project_id = _uuid(value["project_id"], "project_id")
    scope = value["scope"]
    mode_requested = value["mode_requested"]
    mode_applied = value["mode_applied"]
    issue_type = _issue_type(value["issue_type"], "issue_type")
    if scope not in {"current_job", "project"}:
        raise AutoLabelRunError("scope is invalid")
    if (
        mode_requested not in {"proposal_only", "assisted_auto"}
        or mode_applied not in {"proposal_only", "assisted_auto"}
        or (
            mode_requested == "proposal_only"
            and mode_applied != "proposal_only"
        )
    ):
        raise AutoLabelRunError("auto-label run modes are invalid")
    job_id = (
        None
        if value["job_id"] is None
        else _uuid(value["job_id"], "job_id")
    )
    if (scope == "current_job") != (job_id is not None):
        raise AutoLabelRunError("job_id does not match scope")
    matches = []
    if not isinstance(value["matches"], list):
        raise AutoLabelRunError("matches must be a list")
    for raw in value["matches"]:
        if not isinstance(raw, Mapping):
            raise AutoLabelRunError("stored match must be an object")
        _exact_keys(
            raw,
            (
                "project_id",
                "analysis_job_id",
                "candidate_id",
                "issue_type",
                "review_status",
                "decision_hash",
                "review_only",
                "include_in_training",
            ),
            "stored match",
        )
        if raw["review_only"] is not True or raw["include_in_training"] is not False:
            raise AutoLabelRunError("stored match crossed the review boundary")
        matches.append(
            {
                key: raw[key]
                for key in (
                    "project_id",
                    "analysis_job_id",
                    "candidate_id",
                    "issue_type",
                    "review_status",
                    "decision_hash",
                )
            }
        )
    normalised_matches = _normalise_matches(
        project_id=project_id,
        scope=scope,
        job_id=job_id,
        issue_type=issue_type,
        matches=matches,
    )
    if (
        isinstance(value["match_count"], bool)
        or not isinstance(value["match_count"], int)
        or value["match_count"] != len(normalised_matches)
    ):
        raise AutoLabelRunError("match_count does not match matches")
    result = {
        "schema_version": RUN_SCHEMA,
        "run_id": run_id,
        "project_id": project_id,
        "recipe_id": _digest(value["recipe_id"], "recipe_id"),
        "command_sha256": _digest(
            value["command_sha256"],
            "command_sha256",
        ),
        "scope": scope,
        "mode_requested": mode_requested,
        "mode_applied": mode_applied,
        "issue_type": issue_type,
        "job_id": job_id,
        "recorded_at": _timestamp(value["recorded_at"]),
        "review_only": True,
        "include_in_training": False,
        "match_count": len(normalised_matches),
        "matches": normalised_matches,
    }
    run_sha256 = _digest(value["run_sha256"], "run_sha256")
    if _sha256_json(result) != run_sha256:
        raise AutoLabelRunError("auto-label run hash does not match")
    result["run_sha256"] = run_sha256
    return result


class AutoLabelRunStore:
    """Project-scoped immutable JSON audit store for provisional runs."""

    def __init__(self, data_root: Path) -> None:
        if not isinstance(data_root, Path) or not data_root.is_absolute():
            raise AutoLabelRunError("data_root must be an absolute Path")
        data_root.mkdir(parents=True, exist_ok=True)
        self._data_root = _require_real_directory(data_root, "data_root")
        self._root = self._data_root / "auto-label-runs"
        self._projects = self._root / "projects"
        self._projects.mkdir(parents=True, exist_ok=True)
        _require_real_directory(self._root, "auto-label run root")
        _require_real_directory(self._projects, "auto-label project root")
        try:
            self._projects.relative_to(self._data_root)
        except ValueError as error:
            raise AutoLabelRunError(
                "auto-label run store escaped data_root"
            ) from error
        self._lock = threading.RLock()

    def record_run(
        self,
        project_id: UUID,
        *,
        run_id: UUID,
        recipe_id: str,
        command_sha256: str,
        scope: str,
        mode_requested: str,
        mode_applied: str,
        issue_type: str,
        job_id: Optional[UUID] = None,
        matches: List[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        project = _uuid(project_id, "project_id")
        run = _uuid(run_id, "run_id")
        job = None if job_id is None else _uuid(job_id, "job_id")
        if scope not in {"current_job", "project"}:
            raise AutoLabelRunError("scope is invalid")
        if (
            mode_requested not in {"proposal_only", "assisted_auto"}
            or mode_applied not in {"proposal_only", "assisted_auto"}
            or (
                mode_requested == "proposal_only"
                and mode_applied != "proposal_only"
            )
        ):
            raise AutoLabelRunError("auto-label run modes are invalid")
        issue_type = _issue_type(issue_type, "issue_type")
        if (scope == "current_job") != (job is not None):
            raise AutoLabelRunError("job_id does not match scope")
        normalised_matches = _normalise_matches(
            project_id=project,
            scope=scope,
            job_id=job,
            issue_type=issue_type,
            matches=matches,
        )
        core = {
            "schema_version": RUN_SCHEMA,
            "run_id": run,
            "project_id": project,
            "recipe_id": _digest(recipe_id, "recipe_id"),
            "command_sha256": _digest(
                command_sha256,
                "command_sha256",
            ),
            "scope": scope,
            "mode_requested": mode_requested,
            "mode_applied": mode_applied,
            "issue_type": issue_type,
            "job_id": job,
            "review_only": True,
            "include_in_training": False,
            "match_count": len(normalised_matches),
            "matches": normalised_matches,
        }
        with self._lock:
            project_root = self._project_root(project, create=True)
            target = project_root / f"{run}.json"
            if target.exists():
                existing = self._read(target, project, run)
                comparable = {
                    key: value
                    for key, value in existing.items()
                    if key not in {"recorded_at", "run_sha256"}
                }
                if comparable != core:
                    raise AutoLabelRunConflict(
                        "run_id is already used by different content"
                    )
                return existing
            record = {
                **core,
                "recorded_at": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            }
            record["run_sha256"] = _sha256_json(record)
            payload = _canonical_bytes(record)
            if len(payload) > MAX_RECORD_BYTES:
                raise AutoLabelRunError("auto-label run is too large")
            temporary = project_root / f".{run}.{uuid4().hex}.tmp"
            try:
                with temporary.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            except OSError as error:
                raise AutoLabelRunError(
                    "auto-label run could not be persisted"
                ) from error
            finally:
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
            return self._read(target, project, run)

    def get(self, project_id: UUID, run_id: UUID) -> Dict[str, Any]:
        project = _uuid(project_id, "project_id")
        run = _uuid(run_id, "run_id")
        with self._lock:
            target = self._project_root(project, create=False) / f"{run}.json"
            return self._read(target, project, run)

    def list(self, project_id: UUID, limit: int = 100) -> List[Dict[str, Any]]:
        project = _uuid(project_id, "project_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AutoLabelRunError("limit is invalid")
        with self._lock:
            if not (self._projects / project).exists():
                return []
            project_root = self._project_root(project, create=False)
            records = []
            for path in project_root.glob("*.json"):
                try:
                    run = UUID(path.stem)
                    records.append(self._read(path, project, str(run)))
                except (ValueError, AutoLabelRunError):
                    continue
            records.sort(
                key=lambda row: (row["recorded_at"], row["run_id"]),
                reverse=True,
            )
            return records[:limit]

    def latest(self, project_id: UUID) -> Optional[Dict[str, Any]]:
        records = self.list(project_id, limit=1)
        return records[0] if records else None

    def _project_root(self, project_id: str, *, create: bool) -> Path:
        root = self._projects / project_id
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if not root.exists():
            raise AutoLabelRunError("project has no auto-label runs")
        resolved = _require_real_directory(root, "auto-label project directory")
        try:
            resolved.relative_to(self._projects)
        except ValueError as error:
            raise AutoLabelRunError(
                "auto-label project directory escaped the store"
            ) from error
        return resolved

    def _read(
        self,
        path: Path,
        expected_project: str,
        expected_run: str,
    ) -> Dict[str, Any]:
        try:
            resolved = path.resolve(strict=True)
            metadata = path.stat()
        except OSError as error:
            raise AutoLabelRunError("auto-label run was not found") from error
        if (
            path.is_symlink()
            or _is_reparse_point(path)
            or not resolved.is_file()
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= MAX_RECORD_BYTES
        ):
            raise AutoLabelRunError("auto-label run file is unsafe")
        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise AutoLabelRunError(
                        "run JSON contains duplicate keys"
                    )
                result[key] = value
            return result
        try:
            resolved.relative_to(self._projects)
            value = json.loads(
                resolved.read_text(encoding="utf-8"),
                object_pairs_hook=object_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    AutoLabelRunError("run JSON contains a non-finite number")
                ),
            )
        except AutoLabelRunError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AutoLabelRunError("auto-label run JSON is invalid") from error
        record = _normalise_record(value)
        if (
            record["project_id"] != expected_project
            or record["run_id"] != expected_run
        ):
            raise AutoLabelRunError("auto-label run identity does not match")
        return record
