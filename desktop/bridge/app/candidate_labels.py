"""Durable, project-scoped review answers for extracted video candidates.

The store deliberately has no FastAPI dependency.  A caller supplies a
``candidate_resolver`` that proves a candidate belongs to a completed analysis
job before any answer is persisted.  Each answer revision is appended to a
hash-chained JSONL audit log; callers can request either the full history or the
latest answer for each deterministic question.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Union,
)
from uuid import UUID, uuid4, uuid5

from .training_geometry import (
    TrainingGeometryError,
    canonical_training_geometry,
)
from .structured_assist_provenance import (
    StructuredAssistProvenanceError,
    canonical_structured_assist_provenance,
)
from .training_provenance import (
    TrainingProvenanceError,
    build_annotation_provenance_graph,
    canonical_annotation_provenance_graph,
)

SCHEMA_VERSION = "1.0"
RECORD_SCHEMA_VERSION = "1.2"
PROVENANCE_RECORD_SCHEMA_VERSION = "1.3"
GEOMETRY_RECORD_SCHEMA_VERSION = "1.1"
LEGACY_RECORD_SCHEMA_VERSION = "1.0"
ANSWER_VALUES = frozenset({"yes", "no", "unsure", "edit", "skipped"})
ANSWERED_BY_VALUES = frozenset(
    {"local_user", "structured_llm_assisted"}
)
CANONICAL_ISSUE_TYPES = (
    "pothole",
    "road_surface_damage",
    "road_debris",
    "median_barrier_damage",
    "sign_damage",
    "sign_vegetation_occlusion",
    "roadside_mowing_required",
    "guide_sign_identity",
    "tunnel_identity",
    "tunnel_name_occlusion",
    "lane_marking_faded",
    "colored_guidance_faded",
)
# ``other`` remains an explicit operator extension, not one of the product's
# twelve road-maintenance classes.
ISSUE_TYPES = frozenset((*CANONICAL_ISSUE_TYPES, "other"))
ISSUE_TYPE_ALIASES = {
    "surface_damage": "road_surface_damage",
    "debris": "road_debris",
    "sign_vegetation": "sign_vegetation_occlusion",
    "mowing": "roadside_mowing_required",
    "sign_tunnel_identity": "guide_sign_identity",
    "sign_identity": "guide_sign_identity",
    "lane_color_line_fading": "colored_guidance_faded",
    "color_guidance_line_faded": "colored_guidance_faded",
    "colored_guidance_line_faded": "colored_guidance_faded",
}
SEVERITIES = frozenset({"minor", "moderate", "major", "urgent"})
PARTITION_BY_ANSWER = {
    "yes": "approved",
    "edit": "approved",
    "no": "negative",
    "unsure": "held",
    "skipped": "excluded",
}
TRAINING_BY_ANSWER = {
    "yes": True,
    "edit": True,
    "no": True,
    "unsure": False,
    "skipped": False,
}

_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_MAX_TEXT = 500
_MAX_ACTOR = 128
_MAX_METADATA_BYTES = 256 * 1024
_MAX_RECORD_BYTES = 384 * 1024
_MAX_METADATA_DEPTH = 10
_MAX_COLLECTION_ITEMS = 2_048
_RESOLVER_UNSET = object()


class CandidateLabelError(ValueError):
    """Base class for candidate-label validation and persistence errors."""


class CandidateResolutionError(CandidateLabelError):
    """The injected resolver did not prove a completed project candidate."""


class CandidateLabelConflict(CandidateLabelError):
    """An identity was already claimed by a different project or candidate."""


class CandidateLabelCorrupt(CandidateLabelError):
    """The append-only audit data failed structural or hash verification."""


def canonical_issue_type(value: object) -> str:
    """Return the canonical product code for a supported issue type.

    Legacy aliases stay input-compatible so existing clients and immutable
    audit records remain readable.  New records always persist the canonical
    code returned here.
    """

    if not isinstance(value, str):
        raise CandidateLabelError("selected_issue_type is not supported")
    canonical = ISSUE_TYPE_ALIASES.get(value, value)
    if canonical not in ISSUE_TYPES:
        raise CandidateLabelError("selected_issue_type is not supported")
    return canonical


@dataclass(frozen=True)
class CompletedCandidateMetadata:
    """Resolver result proving candidate ownership and completed-job status."""

    project_id: UUID
    analysis_job_id: UUID
    candidate_id: str
    metadata: Mapping[str, Any]


CandidateResolver = Callable[
    [UUID, UUID, str],
    Union[CompletedCandidateMetadata, Mapping[str, Any]],
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_uuid(value: UUID | str, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise CandidateLabelError(f"{field_name} must be a UUID")
    try:
        return UUID(value)
    except (ValueError, AttributeError) as error:
        raise CandidateLabelError(f"{field_name} must be a UUID") from error


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _ensure_real_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CandidateLabelError(f"{label} is unavailable") from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not path.is_dir()
        or resolved != path.resolve()
    ):
        raise CandidateLabelError(f"{label} must be a real directory")
    return resolved


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _record_hash(record_without_hash: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(record_without_hash)).hexdigest()


def _candidate_metadata_hash(metadata: Mapping[str, Any]) -> str:
    """Hash the resolver-owned candidate before local review binding."""

    candidate = dict(metadata)
    candidate.pop("review_event_binding", None)
    return hashlib.sha256(_canonical_json(candidate)).hexdigest()


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise CandidateLabelError("durable write made no progress")
        offset += written


def _safe_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise CandidateLabelError("now_factory must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise CandidateLabelError("now_factory must return a timezone-aware datetime")
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_text(
    value: Optional[str],
    field_name: str,
    *,
    maximum: int = _MAX_TEXT,
) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CandidateLabelError(f"{field_name} must be text")
    normalized = value.strip()
    if not normalized:
        raise CandidateLabelError(f"{field_name} must not be blank")
    if len(normalized) > maximum:
        raise CandidateLabelError(f"{field_name} exceeds {maximum} characters")
    if any(
        ord(character) < 32 and character not in {"\n", "\r", "\t"}
        for character in normalized
    ):
        raise CandidateLabelError(f"{field_name} contains unsupported control characters")
    return normalized


def _validate_box(value: Optional[Mapping[str, Any]]) -> Optional[Dict[str, float]]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CandidateLabelError("normalized_box must be an object")
    if set(value) != {"x", "y", "width", "height"}:
        raise CandidateLabelError(
            "normalized_box must contain only x, y, width, and height"
        )
    normalized: Dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = value[key]
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
        ):
            raise CandidateLabelError(f"normalized_box.{key} must be finite")
        normalized[key] = float(number)
    if not 0.0 <= normalized["x"] <= 1.0:
        raise CandidateLabelError("normalized_box.x is outside [0, 1]")
    if not 0.0 <= normalized["y"] <= 1.0:
        raise CandidateLabelError("normalized_box.y is outside [0, 1]")
    if not 0.0 < normalized["width"] <= 1.0:
        raise CandidateLabelError("normalized_box.width is outside (0, 1]")
    if not 0.0 < normalized["height"] <= 1.0:
        raise CandidateLabelError("normalized_box.height is outside (0, 1]")
    epsilon = 1e-12
    if normalized["x"] + normalized["width"] > 1.0 + epsilon:
        raise CandidateLabelError("normalized_box extends beyond the image width")
    if normalized["y"] + normalized["height"] > 1.0 + epsilon:
        raise CandidateLabelError("normalized_box extends beyond the image height")
    return normalized


def _json_safe_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_METADATA_DEPTH:
        raise CandidateResolutionError("candidate metadata is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value) > 32_768:
            raise CandidateResolutionError("candidate metadata text is too long")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CandidateResolutionError("candidate metadata contains a non-finite number")
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CandidateResolutionError(
                "candidate metadata contains a naive datetime"
            )
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise CandidateResolutionError("candidate metadata object is too large")
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise CandidateResolutionError(
                    "candidate metadata object keys must be bounded text"
                )
            if key in result:
                raise CandidateResolutionError(
                    "candidate metadata contains duplicate keys"
                )
            result[key] = _json_safe_metadata(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise CandidateResolutionError("candidate metadata array is too large")
        return [
            _json_safe_metadata(item, depth=depth + 1)
            for item in value
        ]
    raise CandidateResolutionError(
        "candidate metadata contains an unsupported value type"
    )


def _load_json_no_duplicates(raw: str) -> Any:
    def object_pairs(pairs: List[tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CandidateLabelCorrupt("audit JSON contains duplicate keys")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=object_pairs)
    except CandidateLabelCorrupt:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CandidateLabelCorrupt("audit JSON is invalid") from error


def _csv_safe_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    # CSV consumers frequently open exports in spreadsheet applications.
    # Preserve the text while preventing formula interpretation.
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


class CandidateLabelStore:
    """Append-only candidate answers isolated by project UUID."""

    def __init__(
        self,
        data_root: Path,
        candidate_resolver: CandidateResolver,
        *,
        now_factory: Callable[[], datetime] = _utc_now,
        audit_provenance: bool = False,
    ) -> None:
        if not callable(candidate_resolver):
            raise CandidateLabelError("candidate_resolver must be callable")
        if not callable(now_factory):
            raise CandidateLabelError("now_factory must be callable")
        if not isinstance(audit_provenance, bool):
            raise CandidateLabelError(
                "audit_provenance must be a boolean"
            )
        root = Path(data_root)
        if not root.is_absolute():
            raise CandidateLabelError("candidate-label data_root must be absolute")
        root.mkdir(parents=True, exist_ok=True)
        self._data_root = _ensure_real_directory(
            root, "candidate-label data_root"
        )
        self._store_root = self._data_root / "candidate-labels"
        self._projects_root = self._store_root / "projects"
        self._owners_root = self._store_root / "analysis-job-owners"
        for directory, label in (
            (self._store_root, "candidate-label store root"),
            (self._projects_root, "candidate-label projects root"),
            (self._owners_root, "candidate-label owner root"),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            _ensure_real_directory(directory, label)
        if self._store_root.resolve().parent != self._data_root:
            raise CandidateLabelError("candidate-label store escaped its data root")
        self._resolver = candidate_resolver
        self._now = now_factory
        self._audit_provenance = audit_provenance
        self._thread_lock = threading.RLock()

    @staticmethod
    def question_id(
        analysis_job_id: UUID | str,
        candidate_id: str,
    ) -> UUID:
        job_uuid = _coerce_uuid(analysis_job_id, "analysis_job_id")
        if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(
            candidate_id
        ):
            raise CandidateLabelError(
                "candidate_id must be 20 lowercase hexadecimal characters"
            )
        return uuid5(job_uuid, candidate_id)

    def resolve_question(
        self,
        project_id: UUID | str,
        analysis_job_id: UUID | str,
        candidate_id: str,
    ) -> Dict[str, Any]:
        """Resolve a completed candidate without persisting an answer."""
        project_uuid = _coerce_uuid(project_id, "project_id")
        job_uuid = _coerce_uuid(analysis_job_id, "analysis_job_id")
        question_uuid = self.question_id(job_uuid, candidate_id)
        metadata = self._resolve_candidate(
            project_uuid, job_uuid, candidate_id
        )
        return {
            "project_id": str(project_uuid),
            "analysis_job_id": str(job_uuid),
            "candidate_id": candidate_id,
            "question_id": str(question_uuid),
            "ai_original_candidate": metadata,
        }

    def resolve_questions_from_candidates(
        self,
        project_id: UUID | str,
        analysis_job_id: UUID | str,
        candidate_ids: Sequence[str],
        resolved_candidates: Sequence[
            Union[CompletedCandidateMetadata, Mapping[str, Any]]
        ],
    ) -> List[Dict[str, Any]]:
        """Validate and project one already-atomic candidate batch."""

        project_uuid = _coerce_uuid(project_id, "project_id")
        job_uuid = _coerce_uuid(analysis_job_id, "analysis_job_id")
        if (
            isinstance(candidate_ids, (str, bytes))
            or not isinstance(candidate_ids, Sequence)
            or isinstance(resolved_candidates, (str, bytes))
            or not isinstance(resolved_candidates, Sequence)
            or not 1 <= len(candidate_ids) <= _MAX_COLLECTION_ITEMS
            or len(candidate_ids) != len(resolved_candidates)
            or len(candidate_ids) != len(set(candidate_ids))
        ):
            raise CandidateResolutionError(
                "resolved candidate batch is invalid"
            )
        questions = []
        for candidate_id, resolved in zip(
            candidate_ids,
            resolved_candidates,
        ):
            question_uuid = self.question_id(
                job_uuid,
                candidate_id,
            )
            metadata, _source_sha256, _metadata_sha256 = (
                self._resolve_candidate_with_provenance(
                    project_uuid,
                    job_uuid,
                    candidate_id,
                    resolved_override=resolved,
                )
            )
            questions.append(
                {
                    "project_id": str(project_uuid),
                    "analysis_job_id": str(job_uuid),
                    "candidate_id": candidate_id,
                    "question_id": str(question_uuid),
                    "ai_original_candidate": metadata,
                }
            )
        return questions

    def record_answer(
        self,
        project_id: UUID | str,
        analysis_job_id: UUID | str,
        candidate_id: str,
        *,
        answer: str,
        selected_issue_type: Optional[str] = None,
        severity: Optional[str] = None,
        maintenance_action: Optional[str] = None,
        ocr_text: Optional[str] = None,
        normalized_box: Optional[Mapping[str, Any]] = None,
        answered_by: str = "local_user",
        training_geometry: Optional[Mapping[str, Any]] = None,
        assist_provenance: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Validate and append one immutable answer revision."""
        project_uuid = _coerce_uuid(project_id, "project_id")
        job_uuid = _coerce_uuid(analysis_job_id, "analysis_job_id")
        question_uuid = self.question_id(job_uuid, candidate_id)
        geometry: Optional[Dict[str, Any]] = None
        if training_geometry is not None:
            try:
                geometry = canonical_training_geometry(training_geometry)
            except TrainingGeometryError as error:
                raise CandidateLabelError(
                    "training_geometry is invalid"
                ) from error
            if (
                answer in {"yes", "edit"}
                and normalized_box is None
            ):
                normalized_box = geometry.get("normalized_box")
        final_answer = self._validated_answer(
            answer=answer,
            selected_issue_type=selected_issue_type,
            severity=severity,
            maintenance_action=maintenance_action,
            ocr_text=ocr_text,
            normalized_box=normalized_box,
            answered_by=answered_by,
        )
        self._validate_training_geometry_for_answer(
            final_answer=final_answer,
            training_geometry=geometry,
        )
        assist: Optional[Dict[str, Any]] = None
        if assist_provenance is not None:
            try:
                assist = canonical_structured_assist_provenance(
                    assist_provenance
                )
            except StructuredAssistProvenanceError as error:
                raise CandidateLabelError(
                    "assist_provenance is invalid"
                ) from error
        self._validate_assist_provenance_for_answer(
            project_id=project_uuid,
            analysis_job_id=job_uuid,
            question_id=question_uuid,
            candidate_id=candidate_id,
            final_answer=final_answer,
            assist_provenance=assist,
        )
        (
            candidate_metadata,
            source_sha256,
            candidate_metadata_sha256,
        ) = self._resolve_candidate_with_provenance(
            project_uuid,
            job_uuid,
            candidate_id,
        )

        with self._thread_lock:
            project_root = self._project_root(project_uuid)
            lock_path = project_root / "answers.lock"
            with self._interprocess_lock(lock_path, project_root):
                records = self._read_records_unlocked(project_uuid, project_root)
                self._claim_job_owner(project_uuid, job_uuid)
                existing = [
                    record
                    for record in records
                    if record["question_id"] == str(question_uuid)
                ]
                if (
                    answered_by == "structured_llm_assisted"
                    and existing
                    and existing[-1]["final_answer"]["answered_by"]
                    == "local_user"
                ):
                    raise CandidateLabelConflict(
                        "a structured LLM answer cannot supersede human truth"
                    )
                if existing:
                    original = existing[0]["ai_original_candidate"]
                    if _canonical_json(original) != _canonical_json(candidate_metadata):
                        raise CandidateLabelConflict(
                            "the completed candidate metadata changed after first review"
                        )
                revision = len(existing) + 1
                previous_hash = (
                    records[-1]["record_hash"] if records else None
                )
                record_id = str(uuid4())
                provenance_graph: Optional[Dict[str, Any]] = None
                if self._audit_provenance:
                    try:
                        provenance_graph = (
                            build_annotation_provenance_graph(
                                project_id=str(project_uuid),
                                analysis_job_id=str(job_uuid),
                                question_id=str(question_uuid),
                                candidate_id=candidate_id,
                                answer_record_id=record_id,
                                answer_revision=revision,
                                include_in_training=final_answer[
                                    "include_in_training"
                                ],
                                answered_by=answered_by,
                                candidate_metadata=(
                                    candidate_metadata
                                ),
                                source_sha256=source_sha256,
                                candidate_metadata_sha256=(
                                    candidate_metadata_sha256
                                ),
                                training_geometry=geometry,
                                assist_provenance=assist,
                            )
                        )
                    except TrainingProvenanceError as error:
                        raise CandidateResolutionError(
                            "candidate training provenance is invalid"
                        ) from error
                record_without_hash: Dict[str, Any] = {
                    "schema_version": (
                        PROVENANCE_RECORD_SCHEMA_VERSION
                        if self._audit_provenance
                        else RECORD_SCHEMA_VERSION
                    ),
                    "record_id": record_id,
                    "project_id": str(project_uuid),
                    "analysis_job_id": str(job_uuid),
                    "candidate_id": candidate_id,
                    "question_id": str(question_uuid),
                    "answer_revision": revision,
                    "partition": PARTITION_BY_ANSWER[answer],
                    "ai_original_candidate": candidate_metadata,
                    "final_answer": final_answer,
                    "training_geometry": geometry,
                    "assist_provenance": assist,
                    "recorded_at": _safe_timestamp(self._now()),
                    "previous_record_hash": previous_hash,
                }
                if self._audit_provenance:
                    record_without_hash["provenance_graph"] = (
                        provenance_graph
                    )
                record = {
                    **record_without_hash,
                    "record_hash": _record_hash(record_without_hash),
                }
                self._append_record_unlocked(project_root, record)
                return json.loads(_canonical_json(record).decode("utf-8"))

    @staticmethod
    def _validate_training_geometry_for_answer(
        *,
        final_answer: Mapping[str, Any],
        training_geometry: Optional[Mapping[str, Any]],
    ) -> None:
        if training_geometry is None:
            return
        answer = final_answer["answer"]
        answered_by = final_answer["answered_by"]
        source_kind = training_geometry["source_kind"]
        if training_geometry["approval_authority"] != answered_by:
            raise CandidateLabelError(
                "training geometry authority differs from the answer"
            )
        if answer in {"unsure", "skipped"}:
            raise CandidateLabelError(
                "held or excluded answers cannot carry training geometry"
            )
        if answer == "no":
            if source_kind != "negative_evidence":
                raise CandidateLabelError(
                    "negative answer geometry must be negative evidence"
                )
            return
        if source_kind == "negative_evidence":
            raise CandidateLabelError(
                "approved answers cannot carry negative geometry"
            )
        final_box = final_answer.get("normalized_box")
        if (
            final_box is None
            or final_box != training_geometry.get("normalized_box")
        ):
            raise CandidateLabelError(
                "final answer box differs from training geometry"
            )

    @staticmethod
    def _validate_assist_provenance_for_answer(
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        question_id: UUID,
        candidate_id: str,
        final_answer: Mapping[str, Any],
        assist_provenance: Optional[Mapping[str, Any]],
    ) -> None:
        answered_by = final_answer["answered_by"]
        if answered_by == "local_user":
            if assist_provenance is not None:
                raise CandidateLabelError(
                    "human answers cannot carry structured assist provenance"
                )
            return
        if (
            answered_by != "structured_llm_assisted"
            or assist_provenance is None
        ):
            raise CandidateLabelError(
                "structured answers require assist provenance"
            )
        if final_answer["include_in_training"] is not True:
            raise CandidateLabelError(
                "structured assist is valid only for training labels"
            )
        if (
            assist_provenance["project_id"] != str(project_id)
            or assist_provenance["analysis_job_id"]
            != str(analysis_job_id)
            or assist_provenance["question_id"] != str(question_id)
            or assist_provenance["candidate_id"] != candidate_id
        ):
            raise CandidateLabelError(
                "structured assist provenance identity differs from the answer"
            )

    def audit_records(
        self,
        project_id: UUID | str,
        *,
        analysis_job_id: Optional[UUID | str] = None,
    ) -> List[Dict[str, Any]]:
        project_uuid = _coerce_uuid(project_id, "project_id")
        job_uuid = (
            _coerce_uuid(analysis_job_id, "analysis_job_id")
            if analysis_job_id is not None
            else None
        )
        with self._thread_lock:
            project_root = self._project_root(project_uuid)
            with self._interprocess_lock(
                project_root / "answers.lock", project_root
            ):
                records = self._read_records_unlocked(project_uuid, project_root)
        if job_uuid is not None:
            records = [
                record
                for record in records
                if record["analysis_job_id"] == str(job_uuid)
            ]
        return json.loads(_canonical_json(records).decode("utf-8"))

    def latest_answers(
        self,
        project_id: UUID | str,
        *,
        analysis_job_id: Optional[UUID | str] = None,
        partition: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if partition is not None and partition not in set(
            PARTITION_BY_ANSWER.values()
        ):
            raise CandidateLabelError("partition is not supported")
        records = self.audit_records(
            project_id, analysis_job_id=analysis_job_id
        )
        latest_by_question: Dict[str, Dict[str, Any]] = {}
        last_index: Dict[str, int] = {}
        for index, record in enumerate(records):
            latest_by_question[record["question_id"]] = record
            last_index[record["question_id"]] = index
        latest = sorted(
            latest_by_question.values(),
            key=lambda item: last_index[item["question_id"]],
        )
        if partition is not None:
            latest = [
                record for record in latest if record["partition"] == partition
            ]
        return latest

    def csv_rows(
        self,
        project_id: UUID | str,
        *,
        analysis_job_id: Optional[UUID | str] = None,
        latest_only: bool = True,
        partition: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if latest_only:
            records = self.latest_answers(
                project_id,
                analysis_job_id=analysis_job_id,
                partition=partition,
            )
        else:
            if partition is not None and partition not in set(
                PARTITION_BY_ANSWER.values()
            ):
                raise CandidateLabelError("partition is not supported")
            records = self.audit_records(
                project_id, analysis_job_id=analysis_job_id
            )
            if partition is not None:
                records = [
                    record
                    for record in records
                    if record["partition"] == partition
                ]

        rows: List[Dict[str, Any]] = []
        for record in records:
            answer = record["final_answer"]
            box = answer["normalized_box"] or {}
            rows.append(
                {
                    "record_id": record["record_id"],
                    "question_id": record["question_id"],
                    "project_id": record["project_id"],
                    "analysis_job_id": record["analysis_job_id"],
                    "candidate_id": record["candidate_id"],
                    "answer_revision": record["answer_revision"],
                    "answer": answer["answer"],
                    "partition": record["partition"],
                    "selected_issue_type": answer["selected_issue_type"] or "",
                    "severity": answer["severity"] or "",
                    "maintenance_action": _csv_safe_text(
                        answer["maintenance_action"]
                    ),
                    "ocr_text": _csv_safe_text(answer["ocr_text"]),
                    "box_x": box.get("x", ""),
                    "box_y": box.get("y", ""),
                    "box_width": box.get("width", ""),
                    "box_height": box.get("height", ""),
                    "include_in_training": answer["include_in_training"],
                    "answered_by": answer["answered_by"],
                    "recorded_at": record["recorded_at"],
                    "ai_original_candidate_json": _canonical_json(
                        record["ai_original_candidate"]
                    ).decode("utf-8"),
                    "previous_record_hash": record["previous_record_hash"] or "",
                    "record_hash": record["record_hash"],
                }
            )
        return rows

    # Explicit list-style aliases make the intended integration API obvious.
    list_records = audit_records
    list_latest = latest_answers
    list_csv_rows = csv_rows

    def _validated_answer(
        self,
        *,
        answer: str,
        selected_issue_type: Optional[str],
        severity: Optional[str],
        maintenance_action: Optional[str],
        ocr_text: Optional[str],
        normalized_box: Optional[Mapping[str, Any]],
        answered_by: str,
        preserve_legacy_issue_type: bool = False,
    ) -> Dict[str, Any]:
        if not isinstance(answer, str) or answer not in ANSWER_VALUES:
            raise CandidateLabelError("answer is not supported")
        canonical_selected_issue_type = (
            canonical_issue_type(selected_issue_type)
            if selected_issue_type is not None
            else None
        )
        stored_issue_type = (
            selected_issue_type
            if (
                preserve_legacy_issue_type
                and selected_issue_type in ISSUE_TYPE_ALIASES
            )
            else canonical_selected_issue_type
        )
        if severity is not None and severity not in SEVERITIES:
            raise CandidateLabelError("severity is not supported")
        action = _validate_text(maintenance_action, "maintenance_action")
        ocr = _validate_text(ocr_text, "ocr_text")
        box = _validate_box(normalized_box)
        actor = _validate_text(
            answered_by, "answered_by", maximum=_MAX_ACTOR
        )
        if actor not in ANSWERED_BY_VALUES:
            raise CandidateLabelError("answered_by is not supported")

        if answer in {"yes", "edit"} and selected_issue_type is None:
            raise CandidateLabelError(
                "selected_issue_type is required for an approved answer"
            )
        if answer == "edit" and box is None:
            raise CandidateLabelError("normalized_box is required for edit")
        if answer in {"no", "unsure", "skipped"} and any(
            value is not None
            for value in (
                selected_issue_type,
                severity,
                action,
                ocr,
                box,
            )
        ):
            raise CandidateLabelError(
                "negative, held, and excluded answers cannot carry positive details"
            )

        return {
            "answer": answer,
            "selected_issue_type": stored_issue_type,
            "severity": severity,
            "maintenance_action": action,
            "ocr_text": ocr,
            "normalized_box": box,
            "include_in_training": TRAINING_BY_ANSWER[answer],
            "answered_by": actor,
        }

    def _resolve_candidate(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> Dict[str, Any]:
        metadata, _source_sha256, _metadata_sha256 = (
            self._resolve_candidate_with_provenance(
                project_id,
                analysis_job_id,
                candidate_id,
            )
        )
        return metadata

    def _resolve_candidate_with_provenance(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        *,
        resolved_override: Any = _RESOLVER_UNSET,
    ) -> tuple[Dict[str, Any], Optional[str], Optional[str]]:
        if resolved_override is _RESOLVER_UNSET:
            try:
                resolved = self._resolver(
                    project_id, analysis_job_id, candidate_id
                )
            except CandidateLabelError:
                raise
            except Exception as error:
                raise CandidateResolutionError(
                    "the completed analysis candidate could not be resolved"
                ) from error
        else:
            resolved = resolved_override

        if isinstance(resolved, CompletedCandidateMetadata):
            resolved_project = resolved.project_id
            resolved_job = resolved.analysis_job_id
            resolved_candidate = resolved.candidate_id
            metadata_value = resolved.metadata
            source_sha256 = None
            candidate_metadata_sha256 = None
        elif isinstance(resolved, Mapping):
            if resolved.get("status") != "completed":
                raise CandidateResolutionError(
                    "the analysis candidate is not from a completed job"
                )
            try:
                resolved_project = _coerce_uuid(
                    resolved.get("project_id"), "resolved project_id"
                )
                resolved_job = _coerce_uuid(
                    resolved.get("analysis_job_id", resolved.get("job_id")),
                    "resolved analysis_job_id",
                )
            except CandidateLabelError as error:
                raise CandidateResolutionError(str(error)) from error
            resolved_candidate = resolved.get("candidate_id")
            metadata_value = resolved.get("metadata", resolved.get("candidate"))
            source_sha256 = resolved.get("source_sha256")
            candidate_metadata_sha256 = resolved.get(
                "candidate_metadata_sha256"
            )
        else:
            raise CandidateResolutionError(
                "candidate_resolver returned an unsupported result"
            )

        if (
            resolved_project != project_id
            or resolved_job != analysis_job_id
            or resolved_candidate != candidate_id
        ):
            raise CandidateResolutionError(
                "resolved candidate identity does not match the requested project"
            )
        if not isinstance(metadata_value, Mapping):
            raise CandidateResolutionError("resolved candidate metadata is missing")
        metadata = _json_safe_metadata(metadata_value)
        if not isinstance(metadata, dict):
            raise CandidateResolutionError("resolved candidate metadata is invalid")
        metadata_candidate_id = metadata.get("candidate_id")
        if metadata_candidate_id != candidate_id:
            raise CandidateResolutionError(
                "resolved candidate metadata has a mismatched candidate_id"
            )
        encoded = _canonical_json(metadata)
        if len(encoded) > _MAX_METADATA_BYTES:
            raise CandidateResolutionError("candidate metadata is too large")
        for value, label in (
            (source_sha256, "source_sha256"),
            (
                candidate_metadata_sha256,
                "candidate_metadata_sha256",
            ),
        ):
            if value is not None and (
                not isinstance(value, str)
                or _HASH.fullmatch(value) is None
            ):
                raise CandidateResolutionError(
                    f"resolved candidate {label} is invalid"
                )
        if (
            candidate_metadata_sha256 is not None
            and candidate_metadata_sha256
            != _candidate_metadata_hash(metadata)
        ):
            raise CandidateResolutionError(
                "resolved candidate metadata digest does not match"
            )
        # Reparse a canonical encoding to detach from mutable resolver-owned data.
        return (
            json.loads(encoded.decode("utf-8")),
            source_sha256,
            candidate_metadata_sha256,
        )

    def _project_root(self, project_id: UUID) -> Path:
        _ensure_real_directory(
            self._projects_root, "candidate-label projects root"
        )
        project_root = self._projects_root / str(project_id)
        project_root.mkdir(parents=False, exist_ok=True)
        resolved = _ensure_real_directory(
            project_root, "candidate-label project root"
        )
        if resolved.parent != self._projects_root.resolve():
            raise CandidateLabelError("candidate-label project escaped its root")
        return resolved

    def _claim_job_owner(
        self, project_id: UUID, analysis_job_id: UUID
    ) -> None:
        _ensure_real_directory(
            self._owners_root, "candidate-label owner root"
        )
        owner_path = self._owners_root / f"{analysis_job_id}.json"
        payload = {
            "schema_version": SCHEMA_VERSION,
            "analysis_job_id": str(analysis_job_id),
            "project_id": str(project_id),
        }
        encoded = _canonical_json(payload) + b"\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(owner_path, flags, 0o600)
        except FileExistsError:
            existing = self._read_owner(owner_path)
            if existing != payload:
                raise CandidateLabelConflict(
                    "analysis_job_id is already owned by another project"
                )
            return
        except OSError as error:
            raise CandidateLabelError(
                "analysis job ownership could not be persisted"
            ) from error
        try:
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._fsync_directory(self._owners_root)

    def _read_owner(self, owner_path: Path) -> Dict[str, Any]:
        try:
            resolved = owner_path.resolve(strict=True)
            information = owner_path.lstat()
        except OSError as error:
            raise CandidateLabelCorrupt(
                "analysis job ownership record is unavailable"
            ) from error
        if (
            owner_path.is_symlink()
            or _is_reparse_point(owner_path)
            or resolved.parent != self._owners_root.resolve()
            or not stat.S_ISREG(information.st_mode)
            or information.st_size > 4_096
        ):
            raise CandidateLabelCorrupt(
                "analysis job ownership record is invalid"
            )
        try:
            raw = resolved.read_text(encoding="utf-8")
        except OSError as error:
            raise CandidateLabelCorrupt(
                "analysis job ownership record cannot be read"
            ) from error
        parsed = _load_json_no_duplicates(raw)
        if not isinstance(parsed, dict) or set(parsed) != {
            "schema_version",
            "analysis_job_id",
            "project_id",
        }:
            raise CandidateLabelCorrupt(
                "analysis job ownership record has an invalid schema"
            )
        return parsed

    @contextmanager
    def _interprocess_lock(
        self, lock_path: Path, project_root: Path
    ) -> Iterator[None]:
        if lock_path.parent != project_root:
            raise CandidateLabelError("candidate-label lock escaped its project")
        if lock_path.exists():
            try:
                existing = lock_path.lstat()
                existing_resolved = lock_path.resolve(strict=True)
            except OSError as error:
                raise CandidateLabelError(
                    "candidate-label lock is unavailable"
                ) from error
            if (
                lock_path.is_symlink()
                or _is_reparse_point(lock_path)
                or existing_resolved.parent != project_root
                or not stat.S_ISREG(existing.st_mode)
            ):
                raise CandidateLabelError("candidate-label lock is invalid")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise CandidateLabelError("candidate-label lock is unavailable") from error
        try:
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode):
                raise CandidateLabelError(
                    "candidate-label lock must be a regular file"
                )
            try:
                resolved = lock_path.resolve(strict=True)
            except OSError as error:
                raise CandidateLabelError(
                    "candidate-label lock is unavailable"
                ) from error
            if (
                lock_path.is_symlink()
                or _is_reparse_point(lock_path)
                or resolved.parent != project_root
            ):
                raise CandidateLabelError("candidate-label lock is invalid")
            if information.st_size == 0:
                _write_all(descriptor, b"\0")
                os.fsync(descriptor)
            self._lock_descriptor(descriptor)
            try:
                yield
            finally:
                self._unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _lock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _append_record_unlocked(
        self, project_root: Path, record: Mapping[str, Any]
    ) -> None:
        log_path = project_root / "answers.jsonl"
        if log_path.exists():
            self._validate_log_path(log_path, project_root)
        encoded = _canonical_json(record) + b"\n"
        if len(encoded) > _MAX_RECORD_BYTES:
            raise CandidateLabelError("candidate-label audit record is too large")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(log_path, flags, 0o600)
        except OSError as error:
            raise CandidateLabelError(
                "candidate-label audit log is unavailable"
            ) from error
        try:
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode):
                raise CandidateLabelError(
                    "candidate-label audit log must be a regular file"
                )
            try:
                resolved = log_path.resolve(strict=True)
            except OSError as error:
                raise CandidateLabelError(
                    "candidate-label audit log is unavailable"
                ) from error
            if (
                log_path.is_symlink()
                or _is_reparse_point(log_path)
                or resolved.parent != project_root
            ):
                raise CandidateLabelError(
                    "candidate-label audit log path is invalid"
                )
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._validate_log_path(log_path, project_root)
        self._fsync_directory(project_root)

    def _read_records_unlocked(
        self, project_id: UUID, project_root: Path
    ) -> List[Dict[str, Any]]:
        log_path = project_root / "answers.jsonl"
        if not log_path.exists():
            return []
        self._validate_log_path(log_path, project_root)
        try:
            size = log_path.stat().st_size
        except OSError as error:
            raise CandidateLabelCorrupt(
                "candidate-label audit log cannot be inspected"
            ) from error
        if size > 256 * 1024 * 1024:
            raise CandidateLabelCorrupt("candidate-label audit log is too large")
        try:
            raw_lines = log_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            raise CandidateLabelCorrupt(
                "candidate-label audit log cannot be read"
            ) from error

        records: List[Dict[str, Any]] = []
        previous_hash: Optional[str] = None
        revisions: Dict[str, int] = {}
        record_ids: set[str] = set()
        verified_job_owners: set[str] = set()
        for line_number, raw_line in enumerate(raw_lines, start=1):
            if not raw_line:
                raise CandidateLabelCorrupt(
                    f"candidate-label audit line {line_number} is blank"
                )
            if len(raw_line.encode("utf-8")) > _MAX_RECORD_BYTES:
                raise CandidateLabelCorrupt(
                    f"candidate-label audit line {line_number} is too large"
                )
            parsed = _load_json_no_duplicates(raw_line)
            if not isinstance(parsed, dict):
                raise CandidateLabelCorrupt(
                    f"candidate-label audit line {line_number} is not an object"
                )
            self._validate_stored_record(
                parsed,
                project_id=project_id,
                previous_hash=previous_hash,
                revisions=revisions,
                record_ids=record_ids,
                line_number=line_number,
            )
            job_id = parsed["analysis_job_id"]
            if job_id not in verified_job_owners:
                owner_path = self._owners_root / f"{job_id}.json"
                expected_owner = {
                    "schema_version": SCHEMA_VERSION,
                    "analysis_job_id": job_id,
                    "project_id": str(project_id),
                }
                if self._read_owner(owner_path) != expected_owner:
                    raise CandidateLabelCorrupt(
                        "analysis job ownership does not match the audit project"
                    )
                verified_job_owners.add(job_id)
            records.append(parsed)
            previous_hash = parsed["record_hash"]
            revisions[parsed["question_id"]] = parsed["answer_revision"]
            record_ids.add(parsed["record_id"])
        return records

    def _validate_stored_record(
        self,
        record: Dict[str, Any],
        *,
        project_id: UUID,
        previous_hash: Optional[str],
        revisions: Mapping[str, int],
        record_ids: set[str],
        line_number: int,
    ) -> None:
        expected_keys = {
            "schema_version",
            "record_id",
            "project_id",
            "analysis_job_id",
            "candidate_id",
            "question_id",
            "answer_revision",
            "partition",
            "ai_original_candidate",
            "final_answer",
            "recorded_at",
            "previous_record_hash",
            "record_hash",
        }
        schema_version = record.get("schema_version")
        if schema_version == PROVENANCE_RECORD_SCHEMA_VERSION:
            expected_keys.update(
                {
                    "training_geometry",
                    "assist_provenance",
                    "provenance_graph",
                }
            )
        elif schema_version == RECORD_SCHEMA_VERSION:
            expected_keys.update(
                {"training_geometry", "assist_provenance"}
            )
        elif schema_version == GEOMETRY_RECORD_SCHEMA_VERSION:
            expected_keys.add("training_geometry")
        elif schema_version != LEGACY_RECORD_SCHEMA_VERSION:
            raise CandidateLabelCorrupt(
                f"candidate-label audit line {line_number} has an invalid schema"
            )
        if set(record) != expected_keys:
            raise CandidateLabelCorrupt(
                f"candidate-label audit line {line_number} has an invalid schema"
            )
        try:
            record_uuid = UUID(record["record_id"])
            job_uuid = UUID(record["analysis_job_id"])
            question_uuid = UUID(record["question_id"])
            recorded_at = datetime.fromisoformat(
                record["recorded_at"].replace("Z", "+00:00")
            )
        except (ValueError, TypeError, AttributeError) as error:
            raise CandidateLabelCorrupt(
                f"candidate-label audit line {line_number} has invalid identities"
            ) from error
        if str(record_uuid) in record_ids:
            raise CandidateLabelCorrupt("candidate-label record_id is duplicated")
        if record["project_id"] != str(project_id):
            raise CandidateLabelCorrupt(
                "candidate-label audit contains another project's record"
            )
        candidate_id = record["candidate_id"]
        if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(
            candidate_id
        ):
            raise CandidateLabelCorrupt("candidate-label candidate_id is invalid")
        if question_uuid != self.question_id(job_uuid, candidate_id):
            raise CandidateLabelCorrupt("candidate-label question_id is invalid")
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise CandidateLabelCorrupt("candidate-label timestamp is invalid")
        revision = record["answer_revision"]
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision != revisions.get(record["question_id"], 0) + 1
        ):
            raise CandidateLabelCorrupt("candidate-label answer revision is invalid")
        final_answer = record["final_answer"]
        if not isinstance(final_answer, dict):
            raise CandidateLabelCorrupt("candidate-label final answer is invalid")
        if set(final_answer) != {
            "answer",
            "selected_issue_type",
            "severity",
            "maintenance_action",
            "ocr_text",
            "normalized_box",
            "include_in_training",
            "answered_by",
        }:
            raise CandidateLabelCorrupt("candidate-label final answer schema is invalid")
        try:
            validated = self._validated_answer(
                answer=final_answer["answer"],
                selected_issue_type=final_answer["selected_issue_type"],
                severity=final_answer["severity"],
                maintenance_action=final_answer["maintenance_action"],
                ocr_text=final_answer["ocr_text"],
                normalized_box=final_answer["normalized_box"],
                answered_by=final_answer["answered_by"],
                preserve_legacy_issue_type=True,
            )
        except CandidateLabelError as error:
            raise CandidateLabelCorrupt(
                "candidate-label final answer failed validation"
            ) from error
        if validated != final_answer:
            raise CandidateLabelCorrupt(
                "candidate-label final answer is not canonical"
            )
        training_geometry = (
            record.get("training_geometry")
            if schema_version
            in {
                PROVENANCE_RECORD_SCHEMA_VERSION,
                RECORD_SCHEMA_VERSION,
                GEOMETRY_RECORD_SCHEMA_VERSION,
            }
            else None
        )
        if training_geometry is not None:
            try:
                canonical_geometry = canonical_training_geometry(
                    training_geometry
                )
                self._validate_training_geometry_for_answer(
                    final_answer=final_answer,
                    training_geometry=canonical_geometry,
                )
            except (TrainingGeometryError, CandidateLabelError) as error:
                raise CandidateLabelCorrupt(
                    "candidate-label training geometry is invalid"
                ) from error
            if canonical_geometry != training_geometry:
                raise CandidateLabelCorrupt(
                    "candidate-label training geometry is not canonical"
                )
        assist_provenance = (
            record.get("assist_provenance")
            if schema_version
            in {
                PROVENANCE_RECORD_SCHEMA_VERSION,
                RECORD_SCHEMA_VERSION,
            }
            else None
        )
        if assist_provenance is not None:
            try:
                canonical_assist = (
                    canonical_structured_assist_provenance(
                        assist_provenance
                    )
                )
            except StructuredAssistProvenanceError as error:
                raise CandidateLabelCorrupt(
                    "candidate-label assist provenance is invalid"
                ) from error
            if canonical_assist != assist_provenance:
                raise CandidateLabelCorrupt(
                    "candidate-label assist provenance is not canonical"
                )
        try:
            self._validate_assist_provenance_for_answer(
                project_id=project_id,
                analysis_job_id=job_uuid,
                question_id=question_uuid,
                candidate_id=candidate_id,
                final_answer=final_answer,
                assist_provenance=assist_provenance,
            )
        except CandidateLabelError as error:
            if schema_version not in {
                PROVENANCE_RECORD_SCHEMA_VERSION,
                RECORD_SCHEMA_VERSION,
            }:
                # Legacy records remain auditable, but downstream dataset
                # projection must not authorize legacy structured answers.
                if (
                    final_answer["answered_by"]
                    == "structured_llm_assisted"
                    and assist_provenance is None
                ):
                    pass
                else:
                    raise CandidateLabelCorrupt(
                        "candidate-label assist provenance is inconsistent"
                    ) from error
            else:
                raise CandidateLabelCorrupt(
                    "candidate-label assist provenance is inconsistent"
                ) from error
        provenance_graph = (
            record.get("provenance_graph")
            if schema_version == PROVENANCE_RECORD_SCHEMA_VERSION
            else None
        )
        if provenance_graph is not None:
            try:
                canonical_provenance = (
                    canonical_annotation_provenance_graph(
                        provenance_graph
                    )
                )
            except TrainingProvenanceError as error:
                raise CandidateLabelCorrupt(
                    "candidate-label training provenance is invalid"
                ) from error
            if (
                canonical_provenance != provenance_graph
                or canonical_provenance["project_id"]
                != str(project_id)
                or canonical_provenance["analysis_job_id"]
                != str(job_uuid)
                or canonical_provenance["question_id"]
                != str(question_uuid)
                or canonical_provenance["candidate_id"]
                != candidate_id
                or canonical_provenance["answer_record_id"]
                != record["record_id"]
                or canonical_provenance["answer_revision"]
                != revision
                or canonical_provenance["answer_authority"][
                    "answered_by"
                ]
                != final_answer["answered_by"]
                or canonical_provenance["answer_authority"][
                    "include_in_training"
                ]
                != final_answer["include_in_training"]
            ):
                raise CandidateLabelCorrupt(
                    "candidate-label training provenance crossed a record boundary"
                )
        answer = final_answer["answer"]
        if (
            record["partition"] != PARTITION_BY_ANSWER[answer]
            or final_answer["include_in_training"] != TRAINING_BY_ANSWER[answer]
        ):
            raise CandidateLabelCorrupt("candidate-label partition is invalid")
        metadata = record["ai_original_candidate"]
        if not isinstance(metadata, dict) or metadata.get("candidate_id") != candidate_id:
            raise CandidateLabelCorrupt(
                "candidate-label original candidate metadata is invalid"
            )
        try:
            canonical_metadata = _json_safe_metadata(metadata)
        except CandidateResolutionError as error:
            raise CandidateLabelCorrupt(
                "candidate-label original candidate metadata is unsafe"
            ) from error
        if canonical_metadata != metadata:
            raise CandidateLabelCorrupt(
                "candidate-label original candidate metadata is not canonical"
            )
        if (
            provenance_graph is not None
            and provenance_graph["candidate_metadata_sha256"]
            != _candidate_metadata_hash(metadata)
        ):
            raise CandidateLabelCorrupt(
                "candidate-label candidate lineage digest changed"
            )
        if record["previous_record_hash"] != previous_hash:
            raise CandidateLabelCorrupt("candidate-label audit hash chain is broken")
        record_hash = record["record_hash"]
        if not isinstance(record_hash, str) or not _HASH.fullmatch(record_hash):
            raise CandidateLabelCorrupt("candidate-label record hash is invalid")
        unhashed = {key: value for key, value in record.items() if key != "record_hash"}
        if _record_hash(unhashed) != record_hash:
            raise CandidateLabelCorrupt("candidate-label record hash does not match")

    def _validate_log_path(self, log_path: Path, project_root: Path) -> None:
        try:
            resolved = log_path.resolve(strict=True)
            information = log_path.lstat()
        except OSError as error:
            raise CandidateLabelCorrupt(
                "candidate-label audit log is unavailable"
            ) from error
        if (
            log_path.is_symlink()
            or _is_reparse_point(log_path)
            or resolved.parent != project_root
            or not stat.S_ISREG(information.st_mode)
        ):
            raise CandidateLabelCorrupt("candidate-label audit log path is invalid")

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        # Directory handles cannot be opened this way on Windows.  File fsync
        # still guarantees record contents; on POSIX also persist directory
        # entries.
        if os.name == "nt":
            return
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(directory, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)
