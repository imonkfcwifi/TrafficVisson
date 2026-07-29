"""Immutable, review-only model proposals for completed analysis candidates.

Experimental inference can be run after a video analysis has completed.  The
completed analysis JSON is evidence and must remain immutable, so these
proposals live in a separate, project/job/run-scoped store.  Readers validate
the sidecar hash, strict schema, route identity, and the current candidate
evidence hashes before returning a proposal.

Malformed or stale run files are isolated by this store boundary and ignored.
They never make the label-question endpoint fail, and they are never converted
into answers, annotations, incidents, or active model versions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from uuid import UUID, uuid4

from pydantic import ValidationError

from .contracts import (
    CandidateExperimentalProposal,
    ExperimentalProposalDataset,
    ExperimentalProposalEvaluation,
    ExperimentalProposalModel,
)


RUN_SCHEMA = "trafficvisson.experimental-proposal-run.v1"
PROPOSAL_SCHEMA = "trafficvisson.experimental-candidate-proposal.v1"
MAX_RUN_BYTES = 4 * 1024 * 1024
MAX_RUNS_PER_JOB = 1_024
MAX_PROPOSALS_PER_RUN = 512
MAX_CANDIDATE_BYTES = 256 * 1024

_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_EVALUATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ExperimentalProposalError(ValueError):
    """A proposal could not be validated or persisted."""


class ExperimentalProposalConflict(ExperimentalProposalError):
    """An immutable run id was already used with different content."""


CandidateResolver = Callable[[UUID, UUID, str], Mapping[str, Any]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_uuid(value: UUID | str, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ExperimentalProposalError(f"{field_name} must be a UUID")
    try:
        return UUID(value)
    except (ValueError, AttributeError) as error:
        raise ExperimentalProposalError(
            f"{field_name} must be a UUID"
        ) from error


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _real_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ExperimentalProposalError(f"{label} is unavailable") from error
    if path.is_symlink() or _is_reparse_point(path) or not path.is_dir():
        raise ExperimentalProposalError(f"{label} must be a real directory")
    return resolved


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ExperimentalProposalError(
            "proposal data must be bounded JSON"
        ) from error


def _json_no_duplicates(raw: bytes) -> Any:
    def object_pairs(
        pairs: Iterable[tuple[str, Any]],
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ExperimentalProposalError(
                    "proposal JSON contains duplicate keys"
                )
            result[key] = value
        return result

    def reject_constant(_: str) -> object:
        raise ExperimentalProposalError(
            "proposal JSON contains a non-finite number"
        )

    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except ExperimentalProposalError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExperimentalProposalError("proposal JSON is invalid") from error


def _timestamp(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ExperimentalProposalError(
            "now_factory must return a timezone-aware datetime"
        )
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class ExperimentalProposalStore:
    """Persist immutable experimental runs and resolve their newest valid rows."""

    def __init__(
        self,
        data_root: Path,
        candidate_resolver: CandidateResolver,
        *,
        now_factory: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not callable(candidate_resolver):
            raise ExperimentalProposalError(
                "candidate_resolver must be callable"
            )
        if not callable(now_factory):
            raise ExperimentalProposalError("now_factory must be callable")
        root = Path(data_root)
        if not root.is_absolute():
            raise ExperimentalProposalError(
                "experimental-proposal data_root must be absolute"
            )
        root.mkdir(parents=True, exist_ok=True)
        self._data_root = _real_directory(
            root, "experimental-proposal data root"
        )
        self._store_root = self._data_root / "experimental-proposals"
        self._projects_root = self._store_root / "projects"
        for path, label in (
            (self._store_root, "experimental-proposal store root"),
            (self._projects_root, "experimental-proposal projects root"),
        ):
            path.mkdir(parents=True, exist_ok=True)
            _real_directory(path, label)
        if self._store_root.resolve().parent != self._data_root:
            raise ExperimentalProposalError(
                "experimental-proposal store escaped its data root"
            )
        self._resolver = candidate_resolver
        self._now = now_factory
        self._thread_lock = threading.RLock()

    def record_run(
        self,
        project_id: UUID | str,
        analysis_job_id: UUID | str,
        *,
        model: Mapping[str, Any],
        evaluation: Mapping[str, Any],
        proposals: List[Mapping[str, Any]],
        dataset: Optional[Mapping[str, Any]] = None,
        run_id: Optional[UUID | str] = None,
    ) -> List[Dict[str, Any]]:
        """Persist one immutable review run.

        ``proposals`` accepts only candidate id, status, and relative-score
        predictions.  All route identity, evidence hashes, timestamps, and
        safety flags are generated by this store.
        """

        project_uuid = _coerce_uuid(project_id, "project_id")
        job_uuid = _coerce_uuid(analysis_job_id, "analysis_job_id")
        run_uuid = uuid4() if run_id is None else _coerce_uuid(run_id, "run_id")
        if (
            not isinstance(proposals, list)
            or not proposals
            or len(proposals) > MAX_PROPOSALS_PER_RUN
        ):
            raise ExperimentalProposalError(
                "proposals must contain between 1 and 512 candidates"
            )
        try:
            model_value = ExperimentalProposalModel.model_validate(model)
            evaluation_value = (
                ExperimentalProposalEvaluation.model_validate(evaluation)
            )
            dataset_value = (
                ExperimentalProposalDataset.model_validate(dataset)
                if dataset is not None
                else None
            )
        except ValidationError as error:
            raise ExperimentalProposalError(
                "proposal provenance is invalid"
            ) from error
        score_semantics = (
            "zero_shot_relative_not_probability"
            if model_value.provider == "transformers_clip_zero_shot"
            else "model_score_not_probability"
        )

        recorded_at = _timestamp(self._now())
        rows: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in proposals:
            if not isinstance(raw, Mapping) or set(raw) != {
                "candidate_id",
                "status",
                "predictions",
            }:
                raise ExperimentalProposalError(
                    "proposal candidate fields are invalid"
                )
            candidate_id = raw["candidate_id"]
            if (
                not isinstance(candidate_id, str)
                or not _CANDIDATE_ID.fullmatch(candidate_id)
                or candidate_id in seen
            ):
                raise ExperimentalProposalError(
                    "proposal candidate identity is invalid"
                )
            seen.add(candidate_id)
            source = self._source_identity(
                project_uuid, job_uuid, candidate_id
            )
            proposal_payload = {
                "schema_version": PROPOSAL_SCHEMA,
                "run_id": run_uuid,
                "project_id": project_uuid,
                "analysis_job_id": job_uuid,
                "candidate_id": candidate_id,
                "created_at": recorded_at,
                "status": raw["status"],
                "review_only": True,
                "automatic_labeling_allowed": False,
                "score_semantics": score_semantics,
                "model": model_value,
                "dataset": dataset_value,
                "evaluation": evaluation_value,
                "source": source,
                "predictions": raw["predictions"],
            }
            try:
                validated = CandidateExperimentalProposal.model_validate(
                    proposal_payload
                )
            except ValidationError as error:
                raise ExperimentalProposalError(
                    "proposal candidate output is invalid"
                ) from error
            rows.append(validated.model_dump(mode="json"))

        envelope_without_hash: Dict[str, Any] = {
            "schema_version": RUN_SCHEMA,
            "run_id": str(run_uuid),
            "project_id": str(project_uuid),
            "analysis_job_id": str(job_uuid),
            "recorded_at": recorded_at,
            "proposal_count": len(rows),
            "proposals": rows,
        }
        payload_hash = hashlib.sha256(
            _canonical_json(envelope_without_hash)
        ).hexdigest()
        envelope = {
            **envelope_without_hash,
            "payload_sha256": payload_hash,
        }
        encoded = _canonical_json(envelope) + b"\n"
        if len(encoded) > MAX_RUN_BYTES:
            raise ExperimentalProposalError(
                "experimental proposal run is too large"
            )

        with self._thread_lock:
            job_root = self._job_root(
                project_uuid, job_uuid, create=True
            )
            path = job_root / f"{run_uuid}.json"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor: Optional[int] = None
            created = False
            try:
                descriptor = os.open(path, flags, 0o600)
                created = True
                offset = 0
                while offset < len(encoded):
                    written = os.write(descriptor, encoded[offset:])
                    if written <= 0:
                        raise ExperimentalProposalError(
                            "proposal write made no progress"
                        )
                    offset += written
                os.fsync(descriptor)
            except FileExistsError:
                existing = self._load_run(path, project_uuid, job_uuid)
                if existing is None or existing["payload_sha256"] != payload_hash:
                    raise ExperimentalProposalConflict(
                        "run_id is already used by different proposal data"
                    ) from None
                return list(existing["proposals"])
            except OSError as error:
                raise ExperimentalProposalError(
                    "experimental proposal run could not be persisted"
                ) from error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if created and path.exists():
                    try:
                        information = path.stat()
                    except OSError:
                        information = None
                    if information is not None and information.st_size != len(
                        encoded
                    ):
                        try:
                            path.unlink()
                        except OSError:
                            pass
            return rows

    def latest_for_job(
        self,
        project_id: UUID | str,
        analysis_job_id: UUID | str,
        *,
        evaluation_id: Optional[str] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Return newest valid proposal per candidate; ignore bad sidecars.

        ``evaluation_id`` gives command-driven inference an isolated cache
        view, so a newer run for another recipe cannot hide an older valid
        result.  The unscoped view remains available for the normal question
        card overlay.
        """

        try:
            project_uuid = _coerce_uuid(project_id, "project_id")
            job_uuid = _coerce_uuid(analysis_job_id, "analysis_job_id")
            if evaluation_id is not None and (
                not isinstance(evaluation_id, str)
                or not _EVALUATION_ID.fullmatch(evaluation_id)
            ):
                raise ExperimentalProposalError("evaluation_id is invalid")
            job_root = self._job_root(
                project_uuid, job_uuid, create=False
            )
        except ExperimentalProposalError:
            return {}
        if job_root is None:
            return {}
        try:
            directory_entries = os.scandir(job_root)
        except OSError:
            return {}
        ranked_entries = []
        with directory_entries:
            for entry in directory_entries:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    modified_time_ns = entry.stat(
                        follow_symlinks=False
                    ).st_mtime_ns
                except OSError:
                    continue
                ranked_entries.append((modified_time_ns, entry))
        ranked_entries.sort(key=lambda item: item[0], reverse=True)

        selected: Dict[str, tuple[datetime, str, Dict[str, Any]]] = {}
        validated_run_count = 0
        for _, entry in ranked_entries:
            try:
                run_uuid = UUID(Path(entry.name).stem)
                if (
                    entry.is_symlink()
                    or not entry.is_file(follow_symlinks=False)
                    or entry.name != f"{run_uuid}.json"
                ):
                    continue
                loaded = self._load_run(
                    Path(entry.path), project_uuid, job_uuid
                )
                if loaded is None:
                    continue
                recorded_at = datetime.fromisoformat(
                    loaded["recorded_at"].replace("Z", "+00:00")
                )
            except (OSError, ValueError, ExperimentalProposalError):
                continue
            validated_run_count += 1
            for proposal in loaded["proposals"]:
                if (
                    evaluation_id is not None
                    and proposal["evaluation"]["evaluation_id"]
                    != evaluation_id
                ):
                    continue
                candidate_id = proposal["candidate_id"]
                try:
                    current_source = self._source_identity(
                        project_uuid, job_uuid, candidate_id
                    )
                except ExperimentalProposalError:
                    continue
                if current_source != proposal["source"]:
                    continue
                key = (recorded_at, str(run_uuid))
                previous = selected.get(candidate_id)
                if previous is None or key > (previous[0], previous[1]):
                    selected[candidate_id] = (
                        recorded_at,
                        str(run_uuid),
                        proposal,
                    )
            if validated_run_count >= MAX_RUNS_PER_JOB:
                break
        return {
            candidate_id: value[2]
            for candidate_id, value in selected.items()
        }

    def _source_identity(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> Dict[str, str]:
        try:
            resolved = self._resolver(
                project_id, analysis_job_id, candidate_id
            )
        except Exception as error:
            raise ExperimentalProposalError(
                "completed candidate could not be resolved"
            ) from error
        if not isinstance(resolved, Mapping) or resolved.get("status") != "completed":
            raise ExperimentalProposalError(
                "proposal candidate is not from a completed job"
            )
        try:
            resolved_project = _coerce_uuid(
                resolved.get("project_id"), "resolved project_id"
            )
            resolved_job = _coerce_uuid(
                resolved.get("analysis_job_id", resolved.get("job_id")),
                "resolved analysis_job_id",
            )
        except ExperimentalProposalError:
            raise
        if (
            resolved_project != project_id
            or resolved_job != analysis_job_id
            or resolved.get("candidate_id") != candidate_id
        ):
            raise ExperimentalProposalError(
                "resolved proposal candidate identity does not match"
            )
        metadata = resolved.get("metadata", resolved.get("candidate"))
        if not isinstance(metadata, Mapping) or metadata.get(
            "candidate_id"
        ) != candidate_id:
            raise ExperimentalProposalError(
                "resolved proposal candidate metadata is invalid"
            )
        encoded = _canonical_json(metadata)
        if len(encoded) > MAX_CANDIDATE_BYTES:
            raise ExperimentalProposalError(
                "resolved proposal candidate metadata is too large"
            )
        representative = metadata.get("representative_image")
        representative_sha256 = (
            representative.get("sha256")
            if isinstance(representative, Mapping)
            else None
        )
        if (
            not isinstance(representative_sha256, str)
            or not _SHA256.fullmatch(representative_sha256)
        ):
            raise ExperimentalProposalError(
                "resolved proposal candidate image identity is invalid"
            )
        return {
            "representative_image_sha256": representative_sha256,
            "analysis_candidate_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def _job_root(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        *,
        create: bool,
    ) -> Optional[Path]:
        try:
            projects_root = _real_directory(
                self._projects_root,
                "experimental-proposal projects root",
            )
        except ExperimentalProposalError:
            if create:
                raise
            return None
        project_root = projects_root / str(project_id)
        job_root = project_root / str(analysis_job_id)
        if create:
            project_root.mkdir(parents=False, exist_ok=True)
            _real_directory(
                project_root, "experimental-proposal project root"
            )
            job_root.mkdir(parents=False, exist_ok=True)
        elif not job_root.exists():
            return None
        try:
            resolved_project = _real_directory(
                project_root, "experimental-proposal project root"
            )
            resolved_job = _real_directory(
                job_root, "experimental-proposal job root"
            )
        except ExperimentalProposalError:
            if create:
                raise
            return None
        if (
            resolved_project.parent != projects_root
            or resolved_job.parent != resolved_project
        ):
            if create:
                raise ExperimentalProposalError(
                    "experimental-proposal job escaped its project"
                )
            return None
        return resolved_job

    def _load_run(
        self,
        path: Path,
        expected_project_id: UUID,
        expected_job_id: UUID,
    ) -> Optional[Dict[str, Any]]:
        try:
            before = path.stat()
            resolved = path.resolve(strict=True)
            if (
                path.is_symlink()
                or _is_reparse_point(path)
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size <= 0
                or before.st_size > MAX_RUN_BYTES
            ):
                return None
            expected_root = self._job_root(
                expected_project_id, expected_job_id, create=False
            )
            if expected_root is None or resolved.parent != expected_root:
                return None
            raw = resolved.read_bytes()
            after = path.stat()
        except (OSError, ExperimentalProposalError):
            return None
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(raw) != before.st_size
        ):
            return None
        try:
            parsed = _json_no_duplicates(raw)
            if not isinstance(parsed, dict) or set(parsed) != {
                "schema_version",
                "run_id",
                "project_id",
                "analysis_job_id",
                "recorded_at",
                "proposal_count",
                "proposals",
                "payload_sha256",
            }:
                return None
            if (
                parsed["schema_version"] != RUN_SCHEMA
                or _coerce_uuid(parsed["project_id"], "project_id")
                != expected_project_id
                or _coerce_uuid(parsed["analysis_job_id"], "analysis_job_id")
                != expected_job_id
                or not isinstance(parsed["proposal_count"], int)
                or isinstance(parsed["proposal_count"], bool)
                or not isinstance(parsed["proposals"], list)
                or not 1 <= parsed["proposal_count"] <= MAX_PROPOSALS_PER_RUN
                or parsed["proposal_count"] != len(parsed["proposals"])
                or not isinstance(parsed["payload_sha256"], str)
                or not _SHA256.fullmatch(parsed["payload_sha256"])
            ):
                return None
            run_uuid = _coerce_uuid(parsed["run_id"], "run_id")
            if path.name != f"{run_uuid}.json":
                return None
            recorded_at = datetime.fromisoformat(
                str(parsed["recorded_at"]).replace("Z", "+00:00")
            )
            if (
                recorded_at.tzinfo is None
                or recorded_at.utcoffset() is None
                or not math.isfinite(recorded_at.timestamp())
            ):
                return None
            without_hash = {
                key: value
                for key, value in parsed.items()
                if key != "payload_sha256"
            }
            if (
                hashlib.sha256(_canonical_json(without_hash)).hexdigest()
                != parsed["payload_sha256"]
            ):
                return None
            normalized: List[Dict[str, Any]] = []
            seen: set[str] = set()
            for raw_proposal in parsed["proposals"]:
                proposal = CandidateExperimentalProposal.model_validate(
                    raw_proposal
                )
                if (
                    proposal.run_id != run_uuid
                    or proposal.project_id != expected_project_id
                    or proposal.analysis_job_id != expected_job_id
                    or proposal.created_at != recorded_at
                    or proposal.candidate_id in seen
                ):
                    return None
                seen.add(proposal.candidate_id)
                normalized.append(proposal.model_dump(mode="json"))
        except (
            ExperimentalProposalError,
            ValidationError,
            ValueError,
            OverflowError,
            OSError,
            RecursionError,
        ):
            return None
        return {
            **parsed,
            "proposals": normalized,
        }
