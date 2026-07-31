"""Immutable, question-ready review events for one PipelineJob.

``CandidateEvidenceOverlay`` is the immutable media/evidence layer.  This
module adds a second immutable layer after detector/OCR, SAM2, temporal merge,
approved-reference search, and structured semantic proposal stages have
finished.  It deliberately stores no media and no filesystem path: a review
event only reuses the already verified ``candidate-evidence:`` image/clip
references and binds every upstream checkpoint by canonical SHA-256.

Nothing in this store is approved truth.  Annotation, Incident, training, and
report counts are fixed at zero until the separate user-review projection
boundary runs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4


REVIEW_EVENT_SET_SCHEMA = "trafficvisson.review-event-set.v1"

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_OVERLAY_ID = re.compile(r"^ceo_[a-f0-9]{32}$")
_ARTIFACT_ID = re.compile(r"^cea_[a-f0-9]{32}$")
_SET_ID = re.compile(r"^res_[a-f0-9]{32}$")
_EVENT_ID = re.compile(r"^rve_[a-f0-9]{32}$")
_ISSUE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_SAFE_ERROR = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_FORBIDDEN_TOP_LEVEL_INPUT_KEYS = frozenset(
    {
        "path",
        "image_path",
        "clip_path",
        "media_path",
        "renderer_path",
        "renderer_geometry",
        "final_geometry",
        "user_geometry",
        "client_geometry",
    }
)
_TRUTH_COUNT_FIELDS = frozenset(
    {
        "annotation_count",
        "annotations_created",
        "incident_count",
        "incidents_created",
        "training_membership_count",
        "training_members_created",
        "report_incident_count",
        "report_truth_records_created",
    }
)
_AUTOMATIC_TRUTH_FIELDS = frozenset(
    {
        "automatic_labeling_allowed",
        "automatic_annotation_allowed",
        "automatic_incident_allowed",
        "automatic_incident_creation_allowed",
        "automatic_training_membership_allowed",
        "training_membership_allowed",
        "automatic_report_truth_allowed",
    }
)
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 16 * 1024 * 1024
_MAX_PIPELINE_OVERLAY_SCOPES = 64
_MAX_PIPELINE_REVIEW_EVENT_SETS = 256


class ReviewEventSetError(RuntimeError):
    """Base error for immutable review-event persistence."""


class ReviewEventSetIntegrityError(ReviewEventSetError):
    """An identity, revision, manifest, or evidence hash did not match."""


class ReviewEventSetConflict(ReviewEventSetError):
    """The requested identities cross project/job/evidence boundaries."""


class ReviewEventSetNotFound(ReviewEventSetError):
    """A review-event set or event does not exist in the requested scope."""


class ReviewEventSetLimitError(ReviewEventSetError):
    """A bounded count or serialized-size budget was exceeded."""


@dataclass(frozen=True)
class ReviewEventSetLimits:
    max_evidence_candidates: int = 2_000
    max_events: int = 512
    max_group_candidates: int = 512
    max_ocr_observations_per_event: int = 32
    max_similar_cases_per_event: int = 5
    max_image_bytes: int = 64 * 1024**2
    max_clip_bytes: int = 512 * 1024**2
    max_text_length: int = 4_096
    max_video_seconds: float = 7 * 24 * 60 * 60

    def __post_init__(self) -> None:
        integer_bounds = (
            ("max_evidence_candidates", self.max_evidence_candidates, 1, 4_096),
            ("max_events", self.max_events, 1, 1_024),
            ("max_group_candidates", self.max_group_candidates, 1, 1_024),
            (
                "max_ocr_observations_per_event",
                self.max_ocr_observations_per_event,
                1,
                128,
            ),
            (
                "max_similar_cases_per_event",
                self.max_similar_cases_per_event,
                1,
                5,
            ),
            ("max_image_bytes", self.max_image_bytes, 1, 256 * 1024**2),
            ("max_clip_bytes", self.max_clip_bytes, 1, 2 * 1024**3),
            ("max_text_length", self.max_text_length, 32, 16_384),
        )
        for name, value, minimum, maximum in integer_bounds:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} is outside the supported range")
        if (
            isinstance(self.max_video_seconds, bool)
            or not isinstance(self.max_video_seconds, (int, float))
            or not math.isfinite(float(self.max_video_seconds))
            or not 1 <= float(self.max_video_seconds) <= 31 * 24 * 60 * 60
        ):
            raise ValueError("max_video_seconds is outside the supported range")


@dataclass(frozen=True)
class ResolvedReviewCandidateEvidence:
    """Path-free candidate envelope returned by a fresh bridge resolver."""

    candidate_id: str
    candidate_metadata_sha256: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedReviewCandidateEvidenceSet:
    """Freshly verified candidate-evidence snapshot.

    A production resolver should load ``CandidateEvidenceOverlayStore`` on
    every call; that load re-hashes the representative JPEG and MP4 before
    this object is returned.
    """

    project_id: UUID
    analysis_job_id: UUID
    pipeline_job_id: UUID
    overlay_id: str
    source_sha256: str
    overlay_manifest_sha256: str
    candidate_set_metadata_sha256: str
    candidates: tuple[ResolvedReviewCandidateEvidence, ...]
    overlay_members: tuple[Mapping[str, Any], ...] = ()


class ReviewCandidateEvidenceResolver(Protocol):
    def __call__(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        source_sha256: str,
    ) -> ResolvedReviewCandidateEvidenceSet: ...


@dataclass(frozen=True)
class ReviewEventArtifactReference:
    artifact_ref: str
    artifact_id: str
    media_type: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ReviewEventEvidenceDigest:
    candidate_id: str
    candidate_metadata_sha256: str
    image_sha256: str
    clip_sha256: str


@dataclass(frozen=True)
class ReviewEvent:
    event_id: str
    representative_candidate_id: str
    evidence_candidate_ids: tuple[str, ...]
    issue_type: str
    start_seconds: float
    end_seconds: float
    merged_frame_count: int
    urgent_review: bool
    representative_image: ReviewEventArtifactReference
    preview_clip: ReviewEventArtifactReference
    evidence_digests: tuple[ReviewEventEvidenceDigest, ...]
    detector_summary: Mapping[str, Any]
    ocr_observations: tuple[Mapping[str, Any], ...]
    sam2_summary: Mapping[str, Any]
    similar_cases: tuple[Mapping[str, Any], ...]
    structured_proposal: Mapping[str, Any]
    checkpoint_revisions: Mapping[str, str]
    event_revision: str
    review_only: bool = True
    annotations_created: int = 0
    incidents_created: int = 0
    training_members_created: int = 0
    report_truth_records_created: int = 0


@dataclass(frozen=True)
class ReviewEventSetResult:
    schema_version: str
    review_event_set_id: str
    project_id: str
    analysis_job_id: str
    pipeline_job_id: str
    overlay_id: str
    source_sha256: str
    overlay_manifest_sha256: str
    candidate_set_metadata_sha256: str
    candidate_evidence_revision: str
    status: str
    error_code: Optional[str]
    checkpoint_revisions: Mapping[str, str]
    checkpoint_record_sha256: Mapping[str, str]
    events: tuple[ReviewEvent, ...]
    review_event_set_revision: str
    manifest_sha256: str
    created_at: str
    idempotent_replay: bool
    review_only: bool = True
    annotations_created: int = 0
    incidents_created: int = 0
    training_members_created: int = 0
    report_truth_records_created: int = 0

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "review_event_set_id": self.review_event_set_id,
            "project_id": self.project_id,
            "analysis_job_id": self.analysis_job_id,
            "pipeline_job_id": self.pipeline_job_id,
            "overlay_id": self.overlay_id,
            "source_sha256": self.source_sha256,
            "overlay_manifest_sha256": self.overlay_manifest_sha256,
            "candidate_set_metadata_sha256": (
                self.candidate_set_metadata_sha256
            ),
            "candidate_evidence_revision": self.candidate_evidence_revision,
            "status": self.status,
            "error_code": self.error_code,
            "event_count": len(self.events),
            "event_ids": [event.event_id for event in self.events],
            "checkpoint_revisions": dict(self.checkpoint_revisions),
            "checkpoint_record_sha256": dict(
                self.checkpoint_record_sha256
            ),
            "review_event_set_revision": self.review_event_set_revision,
            "manifest_sha256": self.manifest_sha256,
            "created_at": self.created_at,
            "idempotent_replay": self.idempotent_replay,
            "review_only": True,
            "annotations_created": 0,
            "incidents_created": 0,
            "training_members_created": 0,
            "report_truth_records_created": 0,
        }


@dataclass(frozen=True)
class ReviewCandidateBatchBinding:
    """One candidate and ReviewEvent bound by one fresh set verification."""

    event_set: ReviewEventSetResult
    event: ReviewEvent
    candidate_metadata: Mapping[str, Any]
    candidate_metadata_sha256: str


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
        raise ReviewEventSetIntegrityError(
            "review-event data is not canonical JSON"
        ) from error


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _strict_json(raw: bytes) -> dict[str, Any]:
    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReviewEventSetIntegrityError(
                    "review-event manifest contains duplicate JSON keys"
                )
            result[key] = value
        return result

    def reject_constant(_value):
        raise ReviewEventSetIntegrityError(
            "review-event manifest contains a non-finite number"
        )

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except ReviewEventSetIntegrityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReviewEventSetIntegrityError(
            "review-event manifest is invalid UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise ReviewEventSetIntegrityError(
            "review-event manifest must be a JSON object"
        )
    return value


def _safe_json(
    value: Any,
    *,
    label: str,
    max_text: int,
    depth: int = 0,
) -> Any:
    if depth > 10:
        raise ReviewEventSetLimitError(f"{label} is too deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if "\x00" in value or len(value) > max_text:
            raise ReviewEventSetLimitError(f"{label} contains oversized text")
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ReviewEventSetIntegrityError(
                f"{label} contains a non-finite number"
            )
        return value
    if isinstance(value, Path):
        raise ReviewEventSetIntegrityError(
            f"{label} cannot contain filesystem paths"
        )
    if isinstance(value, Mapping):
        if len(value) > 2_048:
            raise ReviewEventSetLimitError(f"{label} contains too many fields")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 128
                or "\x00" in key
            ):
                raise ReviewEventSetIntegrityError(
                    f"{label} contains an invalid key"
                )
            result[key] = _safe_json(
                item,
                label=label,
                max_text=max_text,
                depth=depth + 1,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        if len(value) > 8_192:
            raise ReviewEventSetLimitError(f"{label} contains too many rows")
        return [
            _safe_json(
                item,
                label=label,
                max_text=max_text,
                depth=depth + 1,
            )
            for item in value
        ]
    raise ReviewEventSetIntegrityError(f"{label} is not safe JSON")


def _checkpoint_json(
    value: Mapping[str, Any],
    *,
    label: str,
    limits: ReviewEventSetLimits,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewEventSetIntegrityError(f"{label} must be a mapping")
    forbidden = _FORBIDDEN_TOP_LEVEL_INPUT_KEYS.intersection(value)
    if forbidden:
        raise ReviewEventSetIntegrityError(
            f"{label} cannot accept renderer paths or geometry"
        )
    safe = _safe_json(
        value,
        label=label,
        max_text=limits.max_text_length,
    )
    body = _canonical_json(safe)
    if len(body) > _MAX_CHECKPOINT_BYTES:
        raise ReviewEventSetLimitError(
            f"{label} exceeded its serialized-size bound"
        )
    for key in _TRUTH_COUNT_FIELDS:
        if key in safe and safe[key] != 0:
            raise ReviewEventSetConflict(
                f"{label} attempted to cross the user-review truth boundary"
            )
    for key in _AUTOMATIC_TRUTH_FIELDS:
        if key in safe and safe[key] is not False:
            raise ReviewEventSetConflict(
                f"{label} attempted automatic truth projection"
            )
    if "review_only" in safe and safe["review_only"] is not True:
        raise ReviewEventSetConflict(f"{label} is not review-only")
    return safe


def _required_sha(value: Mapping[str, Any], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or _SHA256.fullmatch(result) is None:
        raise ReviewEventSetIntegrityError(
            f"{label} {key} is not a canonical SHA-256"
        )
    return result


def _optional_sha(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReviewEventSetIntegrityError(
            f"{label} is not a canonical SHA-256"
        )
    return value


def _candidate_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _CANDIDATE_ID.fullmatch(value) is None:
        raise ReviewEventSetIntegrityError(f"{label} is invalid")
    return value


def _finite_time(
    value: Any,
    label: str,
    limits: ReviewEventSetLimits,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= float(limits.max_video_seconds)
    ):
        raise ReviewEventSetIntegrityError(f"{label} is invalid")
    return round(float(value), 6)


def _validate_identity_fields(
    checkpoint: Mapping[str, Any],
    *,
    label: str,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    overlay_id: str,
    source_sha256: str,
) -> None:
    expected = {
        "analysis_job_id": str(analysis_job_id),
        "pipeline_job_id": str(pipeline_job_id),
        "overlay_id": overlay_id,
        "source_sha256": source_sha256,
    }
    for key, wanted in expected.items():
        if key in checkpoint and checkpoint[key] != wanted:
            raise ReviewEventSetConflict(
                f"{label} crossed its {key} binding"
            )


def _artifact(
    raw: Any,
    *,
    overlay_id: str,
    kind: str,
    limits: ReviewEventSetLimits,
) -> ReviewEventArtifactReference:
    if not isinstance(raw, Mapping):
        raise ReviewEventSetIntegrityError(
            f"candidate {kind} evidence metadata is missing"
        )
    artifact_id = raw.get("artifact_id")
    artifact_ref = raw.get("artifact_ref")
    expected_media = "image/jpeg" if kind == "image" else "video/mp4"
    size = raw.get("size_bytes")
    digest = raw.get("sha256")
    maximum = (
        limits.max_image_bytes if kind == "image" else limits.max_clip_bytes
    )
    if (
        not isinstance(artifact_id, str)
        or _ARTIFACT_ID.fullmatch(artifact_id) is None
        or artifact_ref
        != f"candidate-evidence:{overlay_id}:{artifact_id}"
        or raw.get("media_type") != expected_media
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 1 <= size <= maximum
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        raise ReviewEventSetIntegrityError(
            f"candidate {kind} evidence metadata is invalid"
        )
    return ReviewEventArtifactReference(
        artifact_ref=artifact_ref,
        artifact_id=artifact_id,
        media_type=expected_media,
        size_bytes=size,
        sha256=digest,
    )


def _validated_evidence(
    value: Any,
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    overlay_id: str,
    source_sha256: str,
    limits: ReviewEventSetLimits,
) -> tuple[
    ResolvedReviewCandidateEvidenceSet,
    dict[str, dict[str, Any]],
    str,
]:
    if not isinstance(value, ResolvedReviewCandidateEvidenceSet):
        raise ReviewEventSetIntegrityError(
            "candidate evidence resolver returned an invalid snapshot"
        )
    if (
        value.project_id != project_id
        or value.analysis_job_id != analysis_job_id
        or value.pipeline_job_id != pipeline_job_id
        or value.overlay_id != overlay_id
        or value.source_sha256 != source_sha256
    ):
        raise ReviewEventSetConflict(
            "candidate evidence crossed project/job/source binding"
        )
    if (
        _OVERLAY_ID.fullmatch(value.overlay_id) is None
        or _SHA256.fullmatch(value.source_sha256) is None
        or _SHA256.fullmatch(value.overlay_manifest_sha256) is None
        or _SHA256.fullmatch(value.candidate_set_metadata_sha256) is None
        or not 0
        <= len(value.candidates)
        <= limits.max_evidence_candidates
    ):
        raise ReviewEventSetIntegrityError(
            "candidate evidence set metadata is invalid"
        )
    member_by_id: dict[str, dict[str, Any]] = {}
    if value.overlay_members:
        if (
            not isinstance(value.overlay_members, tuple)
            or not 2 <= len(value.overlay_members) <= 32
        ):
            raise ReviewEventSetIntegrityError(
                "candidate evidence overlay members are invalid"
            )
        for raw_member in value.overlay_members:
            if not isinstance(raw_member, Mapping):
                raise ReviewEventSetIntegrityError(
                    "candidate evidence overlay member is invalid"
                )
            member = dict(raw_member)
            member_id = member.get("overlay_id")
            count = member.get("candidate_count")
            if (
                set(member)
                != {
                    "overlay_id",
                    "manifest_sha256",
                    "candidate_set_metadata_sha256",
                    "candidate_count",
                }
                or not isinstance(member_id, str)
                or _OVERLAY_ID.fullmatch(member_id) is None
                or member_id in member_by_id
                or not isinstance(
                    member.get("manifest_sha256"), str
                )
                or _SHA256.fullmatch(
                    member["manifest_sha256"]
                )
                is None
                or not isinstance(
                    member.get(
                        "candidate_set_metadata_sha256"
                    ),
                    str,
                )
                or _SHA256.fullmatch(
                    member["candidate_set_metadata_sha256"]
                )
                is None
                or isinstance(count, bool)
                or not isinstance(count, int)
                or not 1 <= count <= limits.max_evidence_candidates
            ):
                raise ReviewEventSetIntegrityError(
                    "candidate evidence overlay member is invalid"
                )
            member_by_id[member_id] = member
        canonical_members = [
            member_by_id[key]
            for key in sorted(member_by_id)
        ]
        if (
            value.overlay_id not in member_by_id
            or _sha256_json(canonical_members)
            != value.overlay_manifest_sha256
            or sum(
                int(member["candidate_count"])
                for member in canonical_members
            )
            != len(value.candidates)
        ):
            raise ReviewEventSetIntegrityError(
                "candidate evidence composite fingerprint is invalid"
            )
    candidates: dict[str, dict[str, Any]] = {}
    fingerprint_rows = []
    artifact_refs: set[str] = set()
    for candidate in value.candidates:
        if not isinstance(candidate, ResolvedReviewCandidateEvidence):
            raise ReviewEventSetIntegrityError(
                "candidate evidence row is invalid"
            )
        candidate_id = _candidate_id(
            candidate.candidate_id, "candidate evidence identity"
        )
        if candidate_id in candidates:
            raise ReviewEventSetIntegrityError(
                "candidate evidence contains duplicate identities"
            )
        safe_metadata = _safe_json(
            candidate.metadata,
            label="candidate evidence metadata",
            max_text=limits.max_text_length,
        )
        if (
            not isinstance(safe_metadata, dict)
            or safe_metadata.get("candidate_id") != candidate_id
            or not isinstance(candidate.candidate_metadata_sha256, str)
            or _SHA256.fullmatch(candidate.candidate_metadata_sha256) is None
            or _sha256_json(safe_metadata)
            != candidate.candidate_metadata_sha256
        ):
            raise ReviewEventSetIntegrityError(
                "candidate evidence metadata hash does not match"
            )
        evidence_overlay = safe_metadata.get("evidence_overlay")
        candidate_overlay_id = (
            evidence_overlay.get("overlay_id")
            if isinstance(evidence_overlay, Mapping)
            else None
        )
        member = (
            member_by_id.get(str(candidate_overlay_id))
            if member_by_id
            else {
                "overlay_id": overlay_id,
                "manifest_sha256": (
                    value.overlay_manifest_sha256
                ),
                "candidate_set_metadata_sha256": (
                    value.candidate_set_metadata_sha256
                ),
            }
        )
        if (
            not isinstance(evidence_overlay, Mapping)
            or member is None
            or evidence_overlay.get("overlay_id")
            != member["overlay_id"]
            or evidence_overlay.get("pipeline_job_id")
            != str(pipeline_job_id)
            or evidence_overlay.get("manifest_sha256")
            != member["manifest_sha256"]
            or evidence_overlay.get("candidate_set_metadata_sha256")
            != member["candidate_set_metadata_sha256"]
            or evidence_overlay.get("review_only") is not True
        ):
            raise ReviewEventSetConflict(
                "candidate metadata crossed its evidence overlay binding"
            )
        image = _artifact(
            safe_metadata.get("representative_image"),
            overlay_id=str(candidate_overlay_id),
            kind="image",
            limits=limits,
        )
        clip = _artifact(
            safe_metadata.get("preview_clip"),
            overlay_id=str(candidate_overlay_id),
            kind="clip",
            limits=limits,
        )
        if (
            image.artifact_ref in artifact_refs
            or clip.artifact_ref in artifact_refs
            or image.artifact_ref == clip.artifact_ref
        ):
            raise ReviewEventSetIntegrityError(
                "candidate evidence artifact references are not unique"
            )
        artifact_refs.update((image.artifact_ref, clip.artifact_ref))
        start = _finite_time(
            safe_metadata.get("start_seconds"),
            "candidate start time",
            limits,
        )
        end = _finite_time(
            safe_metadata.get("end_seconds"),
            "candidate end time",
            limits,
        )
        timestamp = _finite_time(
            safe_metadata.get("representative_timestamp_seconds"),
            "candidate representative time",
            limits,
        )
        issue_type = safe_metadata.get("issue_type")
        urgent = safe_metadata.get("urgent_review")
        if (
            end < start
            or not start <= timestamp <= end
            or not isinstance(issue_type, str)
            or _ISSUE_TYPE.fullmatch(issue_type) is None
            or not isinstance(urgent, bool)
        ):
            raise ReviewEventSetIntegrityError(
                "candidate evidence timing or classification is invalid"
            )
        row = {
            "candidate_id": candidate_id,
            "candidate_metadata_sha256": (
                candidate.candidate_metadata_sha256
            ),
            # Kept only in the freshly verified in-memory snapshot so the
            # question projector can atomically bind complete candidate
            # metadata without reloading the overlay for every event.
            "metadata": safe_metadata,
            "issue_type": issue_type,
            "start_seconds": start,
            "end_seconds": end,
            "representative_timestamp_seconds": timestamp,
            "urgent_review": urgent,
            "overlay_id": str(candidate_overlay_id),
            "image": image,
            "clip": clip,
            "representative_proposal_provenance": (
                safe_metadata.get(
                    "representative_proposal_provenance"
                )
            ),
        }
        candidates[candidate_id] = row
        fingerprint_rows.append(
            {
                "candidate_id": candidate_id,
                "candidate_metadata_sha256": (
                    candidate.candidate_metadata_sha256
                ),
                "image": asdict(image),
                "clip": asdict(clip),
            }
        )
    fingerprint_rows.sort(key=lambda item: item["candidate_id"])
    if (
        member_by_id
        and _sha256_json(
            [
                {
                    "candidate_id": item["candidate_id"],
                    "candidate_metadata_sha256": (
                        item["candidate_metadata_sha256"]
                    ),
                }
                for item in fingerprint_rows
            ]
        )
        != value.candidate_set_metadata_sha256
    ):
        raise ReviewEventSetIntegrityError(
            "candidate evidence composite metadata hash does not match"
        )
    revision = _sha256_json(
        {
            "overlay_manifest_sha256": value.overlay_manifest_sha256,
            "candidate_set_metadata_sha256": (
                value.candidate_set_metadata_sha256
            ),
            "candidates": fingerprint_rows,
        }
    )
    return value, candidates, revision


def _candidate_list(
    value: Any,
    *,
    label: str,
    known_candidates: set[str],
    maximum: int,
) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or len(value) != len(set(value))
    ):
        raise ReviewEventSetIntegrityError(f"{label} is invalid")
    result = [_candidate_id(item, label) for item in value]
    if not set(result).issubset(known_candidates):
        raise ReviewEventSetConflict(
            f"{label} references foreign candidate evidence"
        )
    return result


def _normalize_dino(
    checkpoint: Mapping[str, Any],
    *,
    candidates: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    known_candidates = set(candidates)
    detection_revision = _required_sha(
        checkpoint, "detection_revision", "detector checkpoint"
    )
    dino_revision = _required_sha(
        checkpoint, "dino_revision", "detector checkpoint"
    )
    dino_review_rows = checkpoint.get(
        "dino_review_proposals_by_candidate", {}
    )
    dino_review_rows_revision = _optional_sha(
        checkpoint.get("dino_review_proposals_revision"),
        "detector DINO frame proposal revision",
    )
    parent_bindings: dict[str, dict[str, set[str]]] = {}
    synthetic_lineage: dict[str, dict[str, tuple[str, ...] | str]] = {}
    if dino_review_rows_revision is not None:
        for synthetic_candidate_id, candidate in candidates.items():
            provenance = candidate.get(
                "representative_proposal_provenance"
            )
            if not isinstance(provenance, Mapping):
                continue
            schema_version = provenance.get("schema_version")
            if schema_version != (
                "trafficvisson.dino-temporal-evidence.v1"
            ):
                continue
            parent_ids = provenance.get("parent_candidate_ids")
            proposal_ids = provenance.get("evidence_proposal_ids")
            peak_proposal_id = provenance.get(
                "peak_dino_proposal_id"
            )
            if (
                provenance.get("review_only") is not True
                or provenance.get("detection_revision")
                != detection_revision
                or provenance.get(
                    "dino_review_proposals_revision"
                )
                != dino_review_rows_revision
                or not isinstance(parent_ids, list)
                or not 1 <= len(parent_ids) <= 512
                or len(parent_ids) != len(set(parent_ids))
                or any(
                    not isinstance(parent_id, str)
                    or _CANDIDATE_ID.fullmatch(parent_id) is None
                    for parent_id in parent_ids
                )
                or not isinstance(proposal_ids, list)
                or not 1 <= len(proposal_ids) <= 4_096
                or len(proposal_ids) != len(set(proposal_ids))
                or any(
                    not isinstance(proposal_id, str)
                    or re.fullmatch(
                        r"dino_[a-f0-9]{24}", proposal_id
                    )
                    is None
                    for proposal_id in proposal_ids
                )
                or peak_proposal_id not in proposal_ids
            ):
                raise ReviewEventSetIntegrityError(
                    "DINO temporal candidate parent lineage is invalid"
                )
            synthetic_lineage[synthetic_candidate_id] = {
                "parent_candidate_ids": tuple(parent_ids),
                "evidence_proposal_ids": tuple(proposal_ids),
                "peak_dino_proposal_id": str(peak_proposal_id),
            }
            for parent_id in parent_ids:
                binding = parent_bindings.setdefault(
                    parent_id,
                    {
                        "synthetic_candidate_ids": set(),
                        "proposal_ids": set(),
                    },
                )
                binding["synthetic_candidate_ids"].add(
                    synthetic_candidate_id
                )
                binding["proposal_ids"].update(proposal_ids)
    allowed_detector_candidates = (
        known_candidates | set(parent_bindings)
    )
    candidate_ids = _candidate_list(
        checkpoint.get("proposal_candidate_ids", []),
        label="detector candidate ids",
        known_candidates=allowed_detector_candidates,
        maximum=len(allowed_detector_candidates),
    )
    status = checkpoint.get("dino_box_proposal_status")
    detector_status = checkpoint.get("dedicated_detector_status")
    allowed = {"completed", "zero_detections", "model_unavailable", "failed"}
    if status not in allowed or detector_status not in allowed:
        raise ReviewEventSetIntegrityError(
            "detector checkpoint status is invalid"
        )
    sources = checkpoint.get("proposal_sources", [])
    if (
        not isinstance(sources, list)
        or len(sources) > 16
        or any(
            not isinstance(item, str)
            or _SAFE_IDENTIFIER.fullmatch(item) is None
            for item in sources
        )
        or len(sources) != len(set(sources))
    ):
        raise ReviewEventSetIntegrityError(
            "detector proposal sources are invalid"
        )
    local_revision = _required_sha(
        checkpoint,
        "local_inference_revision",
        "detector checkpoint",
    )
    evidence_source = checkpoint.get("dino_evidence_source")
    evidence_count = checkpoint.get("high_frequency_evidence_count")
    evidence_manifest = _optional_sha(
        checkpoint.get("high_frequency_manifest_sha256"),
        "detector high-frequency manifest SHA-256",
    )
    issue_types = checkpoint.get("detected_issue_types")
    ocr_candidate_ids = checkpoint.get("ocr_candidate_ids")
    if (
        not isinstance(evidence_source, str)
        or _SAFE_IDENTIFIER.fullmatch(evidence_source) is None
        or isinstance(evidence_count, bool)
        or not isinstance(evidence_count, int)
        or not 0 <= evidence_count <= 4_096
        or not isinstance(issue_types, list)
        or len(issue_types) > 64
        or len(issue_types) != len(set(issue_types))
        or any(
            not isinstance(item, str)
            or _ISSUE_TYPE.fullmatch(item) is None
            for item in issue_types
        )
        or not isinstance(ocr_candidate_ids, list)
        or len(ocr_candidate_ids) != len(set(ocr_candidate_ids))
        or any(
            not isinstance(item, str)
            or _CANDIDATE_ID.fullmatch(item) is None
            or item not in known_candidates
            for item in ocr_candidate_ids
        )
    ):
        raise ReviewEventSetIntegrityError(
            "detector checkpoint fingerprint is invalid"
        )
    if dino_review_rows_revision is not None:
        if (
            not isinstance(dino_review_rows, Mapping)
            or _sha256_json(dino_review_rows)
            != dino_review_rows_revision
            or any(
                not isinstance(candidate_id, str)
                or candidate_id
                not in allowed_detector_candidates
                or not isinstance(rows, list)
                for candidate_id, rows
                in dino_review_rows.items()
            )
        ):
            raise ReviewEventSetIntegrityError(
                "detector DINO frame proposal fingerprint is invalid"
            )
        for candidate_id, rows in dino_review_rows.items():
            if candidate_id in known_candidates:
                continue
            binding = parent_bindings.get(candidate_id)
            row_proposal_ids = {
                row.get("proposal_id")
                for row in rows
                if isinstance(row, Mapping)
            }
            if (
                binding is None
                or not rows
                or len(row_proposal_ids) != len(rows)
                or any(
                    not isinstance(proposal_id, str)
                    or proposal_id
                    not in binding["proposal_ids"]
                    for proposal_id in row_proposal_ids
                )
            ):
                raise ReviewEventSetConflict(
                    "detector candidate ids are not bound to "
                    "the exact DINO temporal evidence overlay"
                )
    elif dino_review_rows:
        raise ReviewEventSetIntegrityError(
            "detector DINO frame proposal revision is missing"
        )
    detection_payload = {
        "analysis_job_id": checkpoint.get("analysis_job_id"),
        "source_sha256": checkpoint.get("source_sha256"),
        "local_inference_revision": local_revision,
        "dino_revision": dino_revision,
        "dino_evidence_source": evidence_source,
        "high_frequency_evidence_count": evidence_count,
        "high_frequency_manifest_sha256": evidence_manifest,
        "proposal_candidate_ids": candidate_ids,
        "detected_issue_types": issue_types,
        "ocr_candidate_ids": sorted(ocr_candidate_ids),
    }
    visual_quality_keys = {
        "visual_evidence_quality_state",
        "visual_evidence_quality_revision",
        "visual_quality_trusted_original_candidate_ids",
        "visual_quality_blocked_parent_candidate_ids",
    }
    visual_quality_contract_present = any(
        key in checkpoint for key in visual_quality_keys
    )
    visual_quality_state = "legacy_not_evaluated"
    visual_quality_revision = None
    trusted_original_candidate_ids: list[str] = []
    blocked_parent_candidate_ids: list[str] = []
    if visual_quality_contract_present:
        visual_quality_state = checkpoint.get(
            "visual_evidence_quality_state"
        )
        visual_quality_revision = _optional_sha(
            checkpoint.get(
                "visual_evidence_quality_revision"
            ),
            "detector visual-evidence quality revision",
        )
        trusted_value = checkpoint.get(
            "visual_quality_trusted_original_candidate_ids"
        )
        blocked_value = checkpoint.get(
            "visual_quality_blocked_parent_candidate_ids"
        )
        if (
            visual_quality_state
            not in {"usable", "partial", "legacy_not_evaluated"}
            or not isinstance(trusted_value, list)
            or not isinstance(blocked_value, list)
            or len(trusted_value) > 4_096
            or len(blocked_value) > 4_096
            or any(
                not isinstance(candidate_id, str)
                or _CANDIDATE_ID.fullmatch(candidate_id) is None
                for candidate_id in [
                    *trusted_value,
                    *blocked_value,
                ]
            )
            or trusted_value != sorted(set(trusted_value))
            or blocked_value != sorted(set(blocked_value))
            or set(trusted_value).intersection(blocked_value)
            or (
                visual_quality_state == "legacy_not_evaluated"
                and visual_quality_revision is not None
            )
            or (
                visual_quality_state != "legacy_not_evaluated"
                and visual_quality_revision is None
            )
        ):
            raise ReviewEventSetIntegrityError(
                "detector visual-evidence quality binding is invalid"
            )
        trusted_original_candidate_ids = list(trusted_value)
        blocked_parent_candidate_ids = list(blocked_value)
        detection_payload.update(
            {
                "visual_evidence_quality_state": (
                    visual_quality_state
                ),
                "visual_evidence_quality_revision": (
                    visual_quality_revision
                ),
                "visual_quality_trusted_original_candidate_ids": (
                    trusted_original_candidate_ids
                ),
                "visual_quality_blocked_parent_candidate_ids": (
                    blocked_parent_candidate_ids
                ),
            }
        )
    if dino_review_rows_revision is not None:
        detection_payload[
            "dino_review_proposals_revision"
        ] = dino_review_rows_revision
    expected_detection_revision = _sha256_json(
        detection_payload
    )
    if expected_detection_revision != detection_revision:
        raise ReviewEventSetIntegrityError(
            "detector checkpoint revision does not match"
        )
    return (
        {
            "detection_revision": detection_revision,
            "dino_revision": dino_revision,
            "dino_status": status,
            "dedicated_detector_status": detector_status,
            "candidate_ids": candidate_ids,
            "proposal_sources": sources,
            "local_inference_revision": local_revision,
            "dino_review_proposals_revision": (
                dino_review_rows_revision
            ),
            "dino_review_proposals_by_candidate": (
                dict(dino_review_rows)
                if isinstance(dino_review_rows, Mapping)
                else {}
            ),
            "visual_evidence_quality_state": (
                visual_quality_state
            ),
            "visual_evidence_quality_revision": (
                visual_quality_revision
            ),
            "visual_quality_trusted_original_candidate_ids": (
                trusted_original_candidate_ids
            ),
            "visual_quality_blocked_parent_candidate_ids": (
                blocked_parent_candidate_ids
            ),
            # Internal, hash-verified aliases used only to bind the immutable
            # detector/SAM2 checkpoints to the synthetic temporal evidence
            # candidates exposed by the final review overlay.
            "synthetic_candidate_lineage": synthetic_lineage,
        },
        detection_revision,
    )


def _normalize_ocr(
    checkpoint: Mapping[str, Any],
    *,
    known_candidates: set[str],
    limits: ReviewEventSetLimits,
) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], str, str]:
    revision = _required_sha(checkpoint, "ocr_revision", "OCR checkpoint")
    provenance = checkpoint.get(
        "ocr_provenance", checkpoint.get("provenance")
    )
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("schema_version")
        != "trafficvisson.pipeline-ocr-provenance.v1"
        or provenance.get("engine_role") != "ocr_only"
        or provenance.get("result_revision") != revision
        or provenance.get("review_only") is not True
        or provenance.get("path_exposed") is not False
        or not isinstance(provenance.get("input_revision"), str)
        or _SHA256.fullmatch(provenance["input_revision"]) is None
    ):
        raise ReviewEventSetIntegrityError(
            "OCR checkpoint provenance does not match its revision"
        )
    status = checkpoint.get("ocr_status", checkpoint.get("status"))
    if status not in {
        "completed",
        "zero_detections",
        "model_unavailable",
        "failed",
    }:
        raise ReviewEventSetIntegrityError("OCR checkpoint status is invalid")
    candidate_ids = _candidate_list(
        checkpoint.get("ocr_candidate_ids", []),
        label="OCR candidate ids",
        known_candidates=known_candidates,
        maximum=len(known_candidates),
    )
    raw_by_candidate = checkpoint.get(
        "ocr_observations_by_candidate",
        checkpoint.get("observations_by_candidate", {}),
    )
    if not isinstance(raw_by_candidate, Mapping):
        raise ReviewEventSetIntegrityError(
            "OCR observations must be grouped by candidate"
        )
    normalized: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for raw_candidate_id, raw_rows in raw_by_candidate.items():
        candidate_id = _candidate_id(
            raw_candidate_id, "OCR observation candidate identity"
        )
        if candidate_id not in known_candidates:
            raise ReviewEventSetConflict(
                "OCR observation references foreign candidate evidence"
            )
        if (
            not isinstance(raw_rows, list)
            or len(raw_rows) > limits.max_ocr_observations_per_event
        ):
            raise ReviewEventSetLimitError(
                "OCR observation count exceeded its event bound"
            )
        rows = []
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                raise ReviewEventSetIntegrityError(
                    "OCR observation is invalid"
                )
            text = raw.get("text")
            confidence = raw.get("confidence")
            kind = raw.get("kind")
            if (
                not isinstance(text, str)
                or not text.strip()
                or "\x00" in text
                or len(text) > min(512, limits.max_text_length)
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
                or not 0 <= float(confidence) <= 1
                or (
                    kind is not None
                    and (
                        not isinstance(kind, str)
                        or len(kind) > 64
                        or "\x00" in kind
                    )
                )
            ):
                raise ReviewEventSetIntegrityError(
                    "OCR observation is invalid"
                )
            # Detector/OCR geometry remains in its signed server checkpoint,
            # but it is never accepted as editable review-event geometry.
            rows.append(
                {
                    "text": text.strip(),
                    "confidence": round(float(confidence), 8),
                    "kind": kind,
                }
            )
        normalized[candidate_id] = tuple(rows)
    if not set(normalized).issubset(set(candidate_ids)):
        raise ReviewEventSetIntegrityError(
            "OCR observations and candidate index disagree"
        )
    return normalized, revision, status


def _normalize_sam2(
    checkpoint: Mapping[str, Any],
    *,
    known_candidates: set[str],
    dino_candidate_lineage: Mapping[str, Mapping[str, Any]],
    dino_temporal_groups: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], str]:
    declared = checkpoint.get("sam2_revision")
    if declared is not None:
        revision = _required_sha(checkpoint, "sam2_revision", "SAM2 checkpoint")
        payload = dict(checkpoint)
        payload.pop("sam2_revision")
        # PipelineJob persistence adds its independently verified storage
        # binding after the semantic SAM2 checkpoint has been hashed.  That
        # owner/root envelope is not SAM2 model output and therefore must not
        # change the semantic revision.
        payload.pop("artifact_root_binding", None)
        if _sha256_json(payload) != revision:
            raise ReviewEventSetIntegrityError(
                "SAM2 checkpoint revision does not match"
            )
    else:
        revision = _sha256_json(checkpoint)
    raw_dino_bindings = checkpoint.get(
        "dino_prompt_bindings", []
    )
    if not isinstance(raw_dino_bindings, list):
        raise ReviewEventSetIntegrityError(
            "SAM2 DINO prompt bindings are invalid"
        )
    dino_bindings: dict[str, Mapping[str, Any]] = {}
    for raw in raw_dino_bindings:
        if not isinstance(raw, Mapping):
            raise ReviewEventSetIntegrityError(
                "SAM2 DINO prompt binding is invalid"
            )
        prompt_id = _candidate_id(
            raw.get("prompt_candidate_id"),
            "SAM2 DINO prompt identity",
        )
        parent_id = _candidate_id(
            raw.get("parent_candidate_id"),
            "SAM2 DINO parent identity",
        )
        evidence_id = _candidate_id(
            raw.get("evidence_candidate_id"),
            "SAM2 DINO evidence identity",
        )
        proposal_id = raw.get("dino_proposal_id")
        issue_type = raw.get("issue_type")
        timestamp = raw.get("evidence_timestamp_seconds")
        score = raw.get("score")
        image_sha256 = raw.get("evidence_image_sha256")
        status = raw.get("sam2_status")
        proposal_sha256 = raw.get("sam2_proposal_sha256")
        error_code = raw.get("sam2_error_code")
        box = raw.get("normalized_box")
        matching_temporal_candidates = []
        for synthetic_candidate_id, lineage in (
            dino_candidate_lineage.items()
        ):
            temporal_group = dino_temporal_groups.get(
                synthetic_candidate_id
            )
            if (
                isinstance(temporal_group, Mapping)
                and parent_id
                in lineage.get("parent_candidate_ids", ())
                and proposal_id
                in lineage.get("evidence_proposal_ids", ())
                and proposal_id
                in temporal_group.get("evidence_proposal_ids", ())
                and prompt_id
                in temporal_group.get(
                    "sam2_prompt_candidate_ids", ()
                )
                and evidence_id
                in temporal_group.get("evidence_frame_ids", ())
            ):
                matching_temporal_candidates.append(
                    synthetic_candidate_id
                )
        if (
            prompt_id in dino_bindings
            or prompt_id in known_candidates
            or len(matching_temporal_candidates) > 1
            or (
                parent_id not in known_candidates
                and len(matching_temporal_candidates) != 1
            )
            or not isinstance(proposal_id, str)
            or re.fullmatch(r"dino_[a-f0-9]{24}", proposal_id)
            is None
            or not isinstance(issue_type, str)
            or _ISSUE_TYPE.fullmatch(issue_type) is None
            or isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or float(timestamp) < 0
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= float(score) <= 1
            or not isinstance(image_sha256, str)
            or _SHA256.fullmatch(image_sha256) is None
            or not isinstance(box, Mapping)
            or set(box) != {"x", "y", "width", "height"}
            or status not in {"available", "failed"}
            or raw.get("review_only") is not True
            or (
                status == "available"
                and (
                    not isinstance(proposal_sha256, str)
                    or _SHA256.fullmatch(proposal_sha256)
                    is None
                    or error_code is not None
                )
            )
            or (
                status == "failed"
                and (
                    proposal_sha256 is not None
                    or not isinstance(error_code, str)
                    or _SAFE_ERROR.fullmatch(error_code) is None
                )
            )
        ):
            raise ReviewEventSetIntegrityError(
                "SAM2 DINO prompt binding is invalid"
            )
        dino_bindings[prompt_id] = {
            **dict(raw),
            "synthetic_candidate_id": (
                matching_temporal_candidates[0]
                if matching_temporal_candidates
                else None
            ),
        }
    declared_dino_prompt_count = checkpoint.get(
        "dino_prompt_count", 0
    )
    declared_dino_mask_count = checkpoint.get(
        "dino_mask_count", 0
    )
    if (
        isinstance(declared_dino_prompt_count, bool)
        or not isinstance(declared_dino_prompt_count, int)
        or declared_dino_prompt_count != len(dino_bindings)
        or isinstance(declared_dino_mask_count, bool)
        or not isinstance(declared_dino_mask_count, int)
        or declared_dino_mask_count
        != sum(
            binding["sam2_status"] == "available"
            for binding in dino_bindings.values()
        )
    ):
        raise ReviewEventSetIntegrityError(
            "SAM2 DINO prompt metrics do not match bindings"
        )
    proposals = checkpoint.get("proposals", [])
    failures = checkpoint.get("failures", [])
    allowed_prompt_ids = set(known_candidates) | set(
        dino_bindings
    )
    if (
        not isinstance(proposals, list)
        or not isinstance(failures, list)
        or len(proposals) > len(allowed_prompt_ids)
        or len(failures) > len(allowed_prompt_ids)
    ):
        raise ReviewEventSetIntegrityError("SAM2 checkpoint rows are invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for raw in proposals:
        if not isinstance(raw, Mapping):
            raise ReviewEventSetIntegrityError("SAM2 proposal is invalid")
        candidate_id = _candidate_id(
            raw.get("candidate_id"), "SAM2 candidate identity"
        )
        if (
            candidate_id not in allowed_prompt_ids
            or candidate_id in result
        ):
            raise ReviewEventSetConflict(
                "SAM2 proposal crossed or duplicated candidate evidence"
            )
        proposal_sha = _required_sha(raw, "proposal_sha256", "SAM2 proposal")
        payload = dict(raw)
        payload.pop("proposal_sha256")
        if _sha256_json(payload) != proposal_sha:
            raise ReviewEventSetIntegrityError(
                "SAM2 proposal hash does not match"
            )
        tracked = raw.get("tracked_frames")
        tracking = raw.get("tracking_performed")
        if (
            not isinstance(tracked, list)
            or len(tracked) > 256
            or not isinstance(tracking, bool)
            or raw.get("review_only") is not True
            or raw.get("requires_user_confirmation") is not True
        ):
            raise ReviewEventSetIntegrityError("SAM2 proposal is invalid")
        result[candidate_id] = {
            "status": "available",
            "proposal_ref": f"sam2-proposal:{proposal_sha}",
            "proposal_sha256": proposal_sha,
            "tracked_frame_count": len(tracked),
            "tracking_performed": tracking,
        }
    failure_ids = set()
    for raw in failures:
        if not isinstance(raw, Mapping):
            raise ReviewEventSetIntegrityError("SAM2 failure row is invalid")
        candidate_id = _candidate_id(
            raw.get("candidate_id"), "SAM2 failure candidate identity"
        )
        if (
            candidate_id not in allowed_prompt_ids
            or candidate_id in failure_ids
            or candidate_id in result
        ):
            raise ReviewEventSetConflict(
                "SAM2 failure crossed or duplicated candidate evidence"
            )
        failure_ids.add(candidate_id)
        result[candidate_id] = {
            "status": "failed",
            "proposal_ref": None,
            "proposal_sha256": None,
            "tracked_frame_count": 0,
            "tracking_performed": False,
        }
    state = checkpoint.get("state")
    fallback_status = (
        "model_unavailable"
        if state == "model_unavailable"
        else "not_requested"
    )
    for candidate_id in known_candidates:
        result.setdefault(
            candidate_id,
            {
                "status": fallback_status,
                "proposal_ref": None,
                "proposal_sha256": None,
                "tracked_frame_count": 0,
                "tracking_performed": False,
            },
        )
    for prompt_id, binding in dino_bindings.items():
        prompt_status = result.get(prompt_id)
        if (
            prompt_status is None
            or (
                binding["sam2_status"] == "available"
                and (
                    prompt_status["status"] != "available"
                    or prompt_status["proposal_sha256"]
                    != binding["sam2_proposal_sha256"]
                )
            )
            or (
                binding["sam2_status"] == "failed"
                and prompt_status["status"] != "failed"
            )
        ):
            raise ReviewEventSetIntegrityError(
                "SAM2 DINO prompt result does not match binding"
            )
    expected_temporal_prompt_ids = {
        prompt_id
        for group in dino_temporal_groups.values()
        for prompt_id in group.get("sam2_prompt_candidate_ids", ())
    }
    temporal_prompt_ids = {
        prompt_id
        for prompt_id, binding in dino_bindings.items()
        if binding.get("synthetic_candidate_id") is not None
    }
    if temporal_prompt_ids != expected_temporal_prompt_ids:
        raise ReviewEventSetIntegrityError(
            "SAM2 DINO prompt bindings do not match temporal evidence"
        )
    return result, revision


def _normalize_temporal(
    checkpoint: Mapping[str, Any],
    *,
    analysis_job_id: UUID,
    dino_revision: str,
    known_candidates: set[str],
    limits: ReviewEventSetLimits,
) -> tuple[
    list[str],
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
    str,
]:
    revision = _required_sha(
        checkpoint, "temporal_merge_revision", "temporal checkpoint"
    )
    if (
        checkpoint.get("analysis_job_id") != str(analysis_job_id)
        or checkpoint.get("detection_revision") != dino_revision
    ):
        raise ReviewEventSetConflict(
            "temporal checkpoint revision chain does not match detection"
        )
    question_ids = _candidate_list(
        checkpoint.get("question_candidate_ids", []),
        label="temporal question candidate ids",
        known_candidates=known_candidates,
        maximum=limits.max_events,
    )
    raw_groups = checkpoint.get("groups", [])
    if not isinstance(raw_groups, list) or len(raw_groups) > limits.max_events:
        raise ReviewEventSetLimitError(
            "temporal group count exceeded its bound"
        )
    groups: dict[str, Mapping[str, Any]] = {}
    grouped_evidence: set[str] = set()
    normalized_groups = []
    for raw in raw_groups:
        if not isinstance(raw, Mapping):
            raise ReviewEventSetIntegrityError(
                "temporal group must be a mapping"
            )
        representative = _candidate_id(
            raw.get("representative_candidate_id"),
            "temporal representative candidate",
        )
        evidence_ids = _candidate_list(
            raw.get("evidence_candidate_ids"),
            label="temporal group evidence ids",
            known_candidates=known_candidates,
            maximum=limits.max_group_candidates,
        )
        issue_type = raw.get("issue_type")
        start = _finite_time(
            raw.get("start_seconds"), "temporal group start", limits
        )
        end = _finite_time(
            raw.get("end_seconds"), "temporal group end", limits
        )
        detection_count = raw.get("detection_count")
        proposal_id = raw.get("proposal_id")
        if (
            representative not in question_ids
            or representative not in evidence_ids
            or representative in groups
            or grouped_evidence.intersection(evidence_ids)
            or not isinstance(issue_type, str)
            or _ISSUE_TYPE.fullmatch(issue_type) is None
            or end < start
            or isinstance(detection_count, bool)
            or not isinstance(detection_count, int)
            or detection_count != len(evidence_ids)
            or not isinstance(proposal_id, str)
            or _SAFE_IDENTIFIER.fullmatch(proposal_id) is None
        ):
            raise ReviewEventSetIntegrityError(
                "temporal group contract is invalid"
            )
        row = {
            "proposal_id": proposal_id,
            "representative_candidate_id": representative,
            "evidence_candidate_ids": evidence_ids,
            "issue_type": issue_type,
            "start_seconds": start,
            "end_seconds": end,
            "detection_count": detection_count,
        }
        groups[representative] = row
        grouped_evidence.update(evidence_ids)
        normalized_groups.append(row)
    if any(
        candidate_id in grouped_evidence and candidate_id not in groups
        for candidate_id in question_ids
    ):
        raise ReviewEventSetIntegrityError(
            "temporal queue contains a non-representative merged candidate"
        )
    core = {
        "analysis_job_id": str(analysis_job_id),
        "evaluation_id": checkpoint.get("evaluation_id"),
        "detection_revision": dino_revision,
        "question_candidate_ids": question_ids,
        "groups": normalized_groups,
    }
    raw_dino_groups = checkpoint.get(
        "dino_temporal_groups_by_candidate", {}
    )
    raw_dino_overlay = checkpoint.get(
        "dino_candidate_evidence_overlay"
    )
    dino_groups: dict[str, Mapping[str, Any]] = {}
    dino_evidence_proposals: set[str] = set()
    if raw_dino_groups:
        if (
            not isinstance(raw_dino_groups, Mapping)
            or not isinstance(raw_dino_overlay, Mapping)
            or raw_dino_overlay.get("overlay_id")
            != checkpoint.get(
                "dino_candidate_evidence_overlay_id"
            )
            or raw_dino_overlay.get("analysis_job_id")
            != str(analysis_job_id)
            or raw_dino_overlay.get("review_only") is not True
            or raw_dino_overlay.get("candidate_count")
            != len(raw_dino_groups)
        ):
            raise ReviewEventSetIntegrityError(
                "DINO temporal evidence overlay is invalid"
            )
        for raw_candidate_id, raw in raw_dino_groups.items():
            candidate_id = _candidate_id(
                raw_candidate_id,
                "DINO temporal candidate identity",
            )
            if (
                candidate_id not in question_ids
                or candidate_id not in groups
                or not isinstance(raw, Mapping)
                or raw.get("synthetic_candidate_id")
                != candidate_id
                or raw.get("review_only") is not True
                or raw.get(
                    "automatic_annotation_allowed"
                )
                is not False
                or raw.get(
                    "automatic_incident_creation_allowed"
                )
                is not False
            ):
                raise ReviewEventSetIntegrityError(
                    "DINO temporal group identity is invalid"
                )
            evidence_proposal_ids = raw.get(
                "evidence_proposal_ids"
            )
            evidence_frame_ids = raw.get(
                "evidence_frame_ids"
            )
            sam2_prompt_ids = raw.get(
                "sam2_prompt_candidate_ids"
            )
            peak = raw.get("peak_evidence")
            merged_frame_count = raw.get(
                "merged_frame_count"
            )
            group = groups[candidate_id]
            cross_source_proof = raw.get(
                "cross_source_merge_proof"
            )
            cross_source_local_ids = raw.get(
                "cross_source_local_candidate_ids"
            )
            if cross_source_proof is not None:
                if (
                    not isinstance(cross_source_proof, Mapping)
                    or not isinstance(
                        cross_source_local_ids, list
                    )
                    or not cross_source_local_ids
                    or len(cross_source_local_ids)
                    != len(set(cross_source_local_ids))
                    or group["evidence_candidate_ids"]
                    != [
                        *cross_source_local_ids,
                        candidate_id,
                    ]
                    or cross_source_proof.get("schema_version")
                    != (
                        "trafficvisson."
                        "cross-source-sign-merge-proof.v1"
                    )
                    or cross_source_proof.get("algorithm")
                    != "orb-affine-ransac-adjacent-sign-v1"
                    or cross_source_proof.get("issue_type")
                    != "guide_sign_identity"
                    or cross_source_proof.get(
                        "local_candidate_id"
                    )
                    not in cross_source_local_ids
                    or cross_source_proof.get(
                        "local_representative_candidate_id"
                    )
                    not in cross_source_local_ids
                    or cross_source_proof.get(
                        "dino_parent_candidate_ids"
                    )
                    != [
                        cross_source_proof.get(
                            "local_candidate_id"
                        )
                    ]
                    or cross_source_proof.get(
                        "dino_temporal_proposal_id"
                    )
                    != raw.get("temporal_proposal_id")
                    or cross_source_proof.get(
                        "dino_evidence_proposal_ids"
                    )
                    != evidence_proposal_ids
                    or cross_source_proof.get(
                        "peak_dino_proposal_id"
                    )
                    != peak.get("dino_proposal_id")
                    or cross_source_proof.get(
                        "review_only"
                    )
                    is not True
                    or cross_source_proof.get(
                        "automatic_annotation_allowed"
                    )
                    is not False
                    or cross_source_proof.get(
                        "automatic_incident_creation_allowed"
                    )
                    is not False
                    or not isinstance(
                        cross_source_proof.get(
                            "proof_revision"
                        ),
                        str,
                    )
                    or _SHA256.fullmatch(
                        str(
                            cross_source_proof.get(
                                "proof_revision"
                            )
                        )
                    )
                    is None
                    or _sha256_json(
                        {
                            key: value
                            for key, value
                            in cross_source_proof.items()
                            if key != "proof_revision"
                        }
                    )
                    != cross_source_proof.get(
                        "proof_revision"
                    )
                ):
                    raise ReviewEventSetIntegrityError(
                        "cross-source sign merge proof is invalid"
                    )
            elif cross_source_local_ids is not None:
                raise ReviewEventSetIntegrityError(
                    "cross-source sign merge evidence has no proof"
                )
            if (
                not isinstance(evidence_proposal_ids, list)
                or not evidence_proposal_ids
                or len(evidence_proposal_ids)
                != len(set(evidence_proposal_ids))
                or any(
                    not isinstance(item, str)
                    or re.fullmatch(
                        r"dino_[a-f0-9]{24}", item
                    )
                    is None
                    or item in dino_evidence_proposals
                    for item in evidence_proposal_ids
                )
                or not isinstance(evidence_frame_ids, list)
                or not evidence_frame_ids
                or len(evidence_frame_ids)
                != len(set(evidence_frame_ids))
                or any(
                    _CANDIDATE_ID.fullmatch(str(item)) is None
                    for item in evidence_frame_ids
                )
                or not isinstance(sam2_prompt_ids, list)
                or len(sam2_prompt_ids)
                != len(set(sam2_prompt_ids))
                or any(
                    _CANDIDATE_ID.fullmatch(str(item)) is None
                    for item in sam2_prompt_ids
                )
                or isinstance(merged_frame_count, bool)
                or not isinstance(merged_frame_count, int)
                or merged_frame_count
                != len(evidence_proposal_ids)
                or not isinstance(peak, Mapping)
                or peak.get("dino_proposal_id")
                not in evidence_proposal_ids
                or not isinstance(
                    peak.get("image_sha256"), str
                )
                or _SHA256.fullmatch(
                    peak["image_sha256"]
                )
                is None
                or not isinstance(
                    peak.get("normalized_box"), Mapping
                )
                or raw.get("issue_type")
                != group["issue_type"]
                or _finite_time(
                    raw.get("start_seconds"),
                    "DINO temporal start",
                    limits,
                )
                != group["start_seconds"]
                or _finite_time(
                    raw.get("end_seconds"),
                    "DINO temporal end",
                    limits,
                )
                != group["end_seconds"]
            ):
                raise ReviewEventSetIntegrityError(
                    "DINO temporal group evidence is invalid"
                )
            dino_evidence_proposals.update(
                evidence_proposal_ids
            )
            dino_groups[candidate_id] = dict(raw)
        core[
            "dino_temporal_groups_by_candidate"
        ] = dict(raw_dino_groups)
        core[
            "dino_candidate_evidence_overlay"
        ] = dict(raw_dino_overlay)
    elif raw_dino_overlay is not None:
        raise ReviewEventSetIntegrityError(
            "DINO temporal overlay has no groups"
        )
    if _sha256_json(core) != revision:
        raise ReviewEventSetIntegrityError(
            "temporal checkpoint revision does not match"
        )
    return question_ids, groups, dino_groups, revision


def _normalize_case(
    value: Mapping[str, Any],
    *,
    limits: ReviewEventSetLimits,
) -> Optional[dict[str, Any]]:
    approved = (
        value.get("approved_reference") is True
        and value.get("reference_state") == "approved_reference"
    )
    weak = (
        value.get("approved_reference") is False
        and value.get("reference_state") == "weak_unconfirmed"
        and value.get("review_required") is True
        and value.get("training_eligible") is False
        and value.get("approval_record_sha256") is None
    )
    if not approved and not weak:
        return None
    source_id = value.get("source_id")
    case_id = value.get("case_id")
    approval_sha = value.get("approval_record_sha256")
    document_sha = value.get("document_sha256")
    photo_sha = value.get("photo_sha256", value.get("image_sha256"))
    quarter = value.get("quarter")
    if quarter in {3, "3", "3Q"}:
        quarter = "3Q"
    elif quarter in {4, "4", "4Q"}:
        quarter = "4Q"
    else:
        raise ReviewEventSetIntegrityError(
            "approved similar case quarter is invalid"
        )
    if (
        not isinstance(source_id, str)
        or _SAFE_IDENTIFIER.fullmatch(source_id) is None
        or not isinstance(case_id, str)
        or _SAFE_IDENTIFIER.fullmatch(case_id) is None
        or (
            approved
            and (
                not isinstance(approval_sha, str)
                or _SHA256.fullmatch(approval_sha) is None
            )
        )
        or not isinstance(document_sha, str)
        or _SHA256.fullmatch(document_sha) is None
        or not isinstance(photo_sha, str)
        or _SHA256.fullmatch(photo_sha) is None
    ):
        raise ReviewEventSetIntegrityError(
            "similar case provenance is invalid"
        )
    index_sha = value.get("index_manifest_sha256")
    if (
        weak
        and (
            not isinstance(index_sha, str)
            or _SHA256.fullmatch(index_sha) is None
        )
    ):
        raise ReviewEventSetIntegrityError(
            "weak similar case index provenance is invalid"
        )
    result = {
        "source_id": source_id,
        "case_id": case_id,
        "quarter": quarter,
        "reference_state": (
            "approved_reference"
            if approved
            else "weak_unconfirmed"
        ),
        "approved_reference": approved,
        "review_required": not approved,
        "training_eligible": approved,
        "document_sha256": document_sha,
        "photo_sha256": photo_sha,
    }
    if approved:
        result["approval_record_sha256"] = approval_sha
    else:
        result["index_manifest_sha256"] = index_sha
    for key in (
        "route",
        "location",
        "caption",
        "opinion",
        "inspection_opinion",
        "approved_issue_type",
        "approved_action",
    ):
        raw = value.get(key)
        if raw is not None:
            if (
                not isinstance(raw, str)
                or "\x00" in raw
                or len(raw) > min(2_000, limits.max_text_length)
            ):
                raise ReviewEventSetIntegrityError(
                    "similar case text is invalid"
                )
            result[key] = raw
    for key in (
        "visual_cosine_score",
        "combined_similarity_score",
    ):
        raw = value.get(key)
        if raw is not None:
            if (
                isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or not math.isfinite(float(raw))
                or not -1 <= float(raw) <= 1
            ):
                raise ReviewEventSetIntegrityError(
                    "similar case score is invalid"
                )
            result[key] = round(float(raw), 8)
    text_structure_score = value.get("text_structure_score")
    if text_structure_score is not None:
        if (
            isinstance(text_structure_score, bool)
            or not isinstance(
                text_structure_score, (int, float)
            )
            or not math.isfinite(float(text_structure_score))
            or not 0 <= float(text_structure_score) <= 10_000
        ):
            raise ReviewEventSetIntegrityError(
                "similar case text score is invalid"
            )
        result["text_structure_score"] = round(
            float(text_structure_score), 8
        )
    for key in (
        "picture_target_id",
        "cell_target_id",
    ):
        raw = value.get(key)
        if raw is not None:
            if (
                not isinstance(raw, str)
                or _SAFE_IDENTIFIER.fullmatch(raw) is None
            ):
                raise ReviewEventSetIntegrityError(
                    "weak similar case target identity is invalid"
                )
            result[key] = raw
    page_number = value.get("photo_page_number")
    if page_number is not None:
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or not 1 <= page_number <= 10_000
        ):
            raise ReviewEventSetIntegrityError(
                "weak similar case page is invalid"
            )
        result["photo_page_number"] = page_number
    return result


def _normalize_similar(
    checkpoint: Mapping[str, Any],
    *,
    event_candidates: set[str],
    known_candidates: set[str],
    limits: ReviewEventSetLimits,
) -> tuple[dict[str, tuple[Mapping[str, Any], ...]], str, Optional[str]]:
    declared = _optional_sha(
        checkpoint.get("visual_reference_revision"),
        "visual reference revision",
    )
    if declared is not None:
        index_sha = _required_sha(
            checkpoint,
            "visual_index_manifest_sha256",
            "visual reference checkpoint",
        )
        matches = checkpoint.get("matches_by_candidate")
        if not isinstance(matches, Mapping):
            raise ReviewEventSetIntegrityError(
                "visual reference matches are invalid"
            )
        if _sha256_json(
            {
                "visual_index_manifest_sha256": index_sha,
                "matches_by_candidate": matches,
            }
        ) != declared:
            raise ReviewEventSetIntegrityError(
                "visual reference checkpoint revision does not match"
            )
    record_sha = _sha256_json(checkpoint)
    raw_matches = checkpoint.get("matches_by_candidate", {})
    if not isinstance(raw_matches, Mapping):
        raise ReviewEventSetIntegrityError(
            "similar cases must be grouped by candidate"
        )
    result: dict[str, tuple[Mapping[str, Any], ...]] = {}
    for raw_candidate_id, raw_cases in raw_matches.items():
        candidate_id = _candidate_id(
            raw_candidate_id, "similar-case candidate identity"
        )
        if candidate_id not in known_candidates:
            raise ReviewEventSetConflict(
                "similar cases reference foreign candidate evidence"
            )
        if (
            not isinstance(raw_cases, list)
            or len(raw_cases) > limits.max_similar_cases_per_event
        ):
            raise ReviewEventSetLimitError(
                "similar-case count exceeded its event bound"
            )
        cases = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_cases:
            if not isinstance(raw, Mapping):
                raise ReviewEventSetIntegrityError(
                    "similar case is invalid"
                )
            normalized = _normalize_case(raw, limits=limits)
            if normalized is None:
                continue
            identity = (
                str(normalized["source_id"]),
                str(normalized["case_id"]),
            )
            if identity in seen:
                raise ReviewEventSetIntegrityError(
                    "similar cases contain duplicate approved identities"
                )
            seen.add(identity)
            cases.append(normalized)
        if candidate_id in event_candidates:
            result[candidate_id] = tuple(cases)
    for candidate_id in event_candidates:
        result.setdefault(candidate_id, ())
    return result, record_sha, declared


def _normalize_structured_proposal(
    judgment: Mapping[str, Any],
    *,
    candidate_id: str,
    limits: ReviewEventSetLimits,
) -> dict[str, Any]:
    if (
        judgment.get("candidate_id") != candidate_id
        or judgment.get("review_only") is not True
        or judgment.get("automatic_annotation_allowed") is not False
        or judgment.get("automatic_incident_allowed") is not False
        or judgment.get("automatic_training_membership_allowed") is not False
        or judgment.get("automatic_report_truth_allowed") is not False
    ):
        raise ReviewEventSetConflict(
            "structured proposal crossed the review-only boundary"
        )
    status = judgment.get("status")
    provider = judgment.get("provider")
    if status not in {"proposed", "not_run", "failed"}:
        raise ReviewEventSetIntegrityError(
            "structured proposal status is invalid"
        )
    if (
        not isinstance(provider, str)
        or _SAFE_IDENTIFIER.fullmatch(provider) is None
    ):
        raise ReviewEventSetIntegrityError(
            "structured proposal provider is invalid"
        )
    proposal = judgment.get("proposal")
    proposal_sha = judgment.get("proposal_sha256")
    execution_fields = {
        "input_sha256",
        "prompt_sha256",
        "idempotency_key",
        "request_provenance_sha256",
        "provenance_sha256",
        "attachment_manifest_sha256",
        "attachment_sha256s",
        "attachment_verification_record_sha256s",
        "provider_command_invoked",
        "model",
        "comparison_mode",
    }
    execution_present = any(
        field in judgment
        for field in (
            "input_sha256",
            "prompt_sha256",
            "idempotency_key",
            "request_provenance_sha256",
            "provenance_sha256",
            "attachment_manifest_sha256",
            "attachment_sha256s",
            "attachment_verification_record_sha256s",
        )
    )
    execution_provenance = None
    if execution_present:
        hashes = {
            field: judgment.get(field)
            for field in (
                "input_sha256",
                "prompt_sha256",
                "idempotency_key",
                "request_provenance_sha256",
                "provenance_sha256",
                "attachment_manifest_sha256",
            )
        }
        attachment_sha256s = judgment.get("attachment_sha256s")
        attachment_record_sha256s = judgment.get(
            "attachment_verification_record_sha256s"
        )
        model = judgment.get("model")
        comparison_mode = judgment.get("comparison_mode")
        if (
            any(
                not isinstance(value, str)
                or _SHA256.fullmatch(value) is None
                for value in hashes.values()
            )
            or not isinstance(attachment_sha256s, list)
            or not isinstance(attachment_record_sha256s, list)
            or len(attachment_sha256s)
            != len(attachment_record_sha256s)
            or len(attachment_sha256s) > 6
            or any(
                not isinstance(value, str)
                or _SHA256.fullmatch(value) is None
                for value in [
                    *attachment_sha256s,
                    *attachment_record_sha256s,
                ]
            )
            or len(attachment_sha256s)
            != len(set(attachment_sha256s))
            or len(attachment_record_sha256s)
            != len(set(attachment_record_sha256s))
            or comparison_mode
            not in {
                "metadata_comparison_only",
                "direct_visual_comparison",
            }
            or (
                comparison_mode == "metadata_comparison_only"
                and attachment_sha256s
            )
            or (
                comparison_mode == "direct_visual_comparison"
                and not 4 <= len(attachment_sha256s) <= 6
            )
            or judgment.get("provider_command_invoked") is not True
            or not isinstance(model, str)
            or not model
            or model != model.strip()
            or len(model) > 200
            or any(ord(character) < 32 for character in model)
        ):
            raise ReviewEventSetIntegrityError(
                "structured proposal execution provenance is invalid"
            )
        execution_provenance = {
            "provider": provider,
            "model": model,
            "input_sha256": hashes["input_sha256"],
            "prompt_sha256": hashes["prompt_sha256"],
            "idempotency_key": hashes["idempotency_key"],
            "request_provenance_sha256": hashes[
                "request_provenance_sha256"
            ],
            "execution_provenance_sha256": hashes[
                "provenance_sha256"
            ],
            "attachment_manifest_sha256": hashes[
                "attachment_manifest_sha256"
            ],
            "attachment_sha256s": list(attachment_sha256s),
            "attachment_verification_record_sha256s": list(
                attachment_record_sha256s
            ),
            "comparison_mode": comparison_mode,
        }
    if status == "proposed":
        if not isinstance(proposal, Mapping):
            raise ReviewEventSetIntegrityError(
                "structured proposal payload is missing"
            )
        safe_proposal = _safe_json(
            proposal,
            label="structured proposal",
            max_text=min(limits.max_text_length, 2_000),
        )
        expected_proposal_keys = {
            "schema_version",
            "candidate_id",
            "issue_type",
            "maintenance_required",
            "action",
            "recommended_severity",
            "rationale",
            "ocr_verification",
            "uncertainty",
            "source_refs",
            "comparison_mode",
            "review_state",
            "review_only",
            "automatic_annotation_allowed",
            "automatic_incident_allowed",
            "automatic_training_membership_allowed",
            "automatic_report_truth_allowed",
        }
        issue_type = safe_proposal.get("issue_type")
        action = safe_proposal.get("action")
        rationale = safe_proposal.get("rationale")
        severity = safe_proposal.get("recommended_severity")
        ocr = safe_proposal.get("ocr_verification")
        uncertainty = safe_proposal.get("uncertainty")
        source_refs = safe_proposal.get("source_refs")
        maintenance_required = safe_proposal.get(
            "maintenance_required"
        )
        ocr_status = (
            ocr.get("status") if isinstance(ocr, Mapping) else None
        )
        normalized_ocr = (
            ocr.get("normalized_text")
            if isinstance(ocr, Mapping)
            else None
        )
        observation_refs = (
            ocr.get("observation_refs")
            if isinstance(ocr, Mapping)
            else None
        )
        if (
            set(safe_proposal) != expected_proposal_keys
            or safe_proposal.get("schema_version")
            != "trafficvisson.structured-vision-proposal.v1"
            or safe_proposal.get("candidate_id") != candidate_id
            or not isinstance(issue_type, str)
            or _ISSUE_TYPE.fullmatch(issue_type) is None
            or not isinstance(maintenance_required, bool)
            or not isinstance(action, str)
            or not action
            or len(action) > 500
            or not isinstance(rationale, str)
            or not rationale
            or len(rationale) > 1_500
            or severity
            not in {
                "minor",
                "moderate",
                "major",
                "urgent",
                "unassessed",
            }
            or (
                maintenance_required
                and (action == "none" or severity == "unassessed")
            )
            or (
                not maintenance_required
                and (action != "none" or severity != "unassessed")
            )
            or not isinstance(ocr, Mapping)
            or set(ocr)
            != {"status", "normalized_text", "observation_refs"}
            or ocr_status
            not in {
                "verified",
                "conflict",
                "uncertain",
                "not_applicable",
            }
            or (
                normalized_ocr is not None
                and (
                    not isinstance(normalized_ocr, str)
                    or not normalized_ocr
                    or len(normalized_ocr) > 500
                    or "\x00" in normalized_ocr
                )
            )
            or not isinstance(observation_refs, list)
            or len(observation_refs) > 32
            or len(observation_refs) != len(set(observation_refs))
            or any(
                not isinstance(item, str)
                or _SAFE_IDENTIFIER.fullmatch(item) is None
                for item in observation_refs
            )
            or (
                ocr_status == "not_applicable"
                and (
                    normalized_ocr is not None
                    or bool(observation_refs)
                )
            )
            or (
                ocr_status != "not_applicable"
                and not observation_refs
            )
            or not isinstance(uncertainty, Mapping)
            or set(uncertainty) != {"score", "reasons"}
            or isinstance(uncertainty.get("score"), bool)
            or not isinstance(uncertainty.get("score"), (int, float))
            or not math.isfinite(float(uncertainty["score"]))
            or not 0 <= float(uncertainty["score"]) <= 1
            or not isinstance(uncertainty.get("reasons"), list)
            or not 1 <= len(uncertainty["reasons"]) <= 8
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 300
                for item in uncertainty["reasons"]
            )
            or safe_proposal.get("comparison_mode")
            != "metadata_comparison_only"
            or not isinstance(source_refs, list)
            or not 1 <= len(source_refs) <= 64
            or len(source_refs) != len(set(source_refs))
            or any(
                not isinstance(item, str)
                or _SAFE_IDENTIFIER.fullmatch(item) is None
                for item in source_refs
            )
            or safe_proposal.get("review_state") != "proposed"
            or safe_proposal.get("review_only") is not True
            or safe_proposal.get("automatic_annotation_allowed") is not False
            or safe_proposal.get("automatic_incident_allowed") is not False
            or safe_proposal.get("automatic_training_membership_allowed")
            is not False
            or safe_proposal.get("automatic_report_truth_allowed") is not False
            or not isinstance(proposal_sha, str)
            or _SHA256.fullmatch(proposal_sha) is None
            or _sha256_json(safe_proposal) != proposal_sha
        ):
            raise ReviewEventSetIntegrityError(
                "structured proposal hash or contract is invalid"
            )
        # The semantic proposal is retained; detector/SAM2 geometry is not.
        return {
            "status": "proposed",
            "provider": provider,
            "proposal_sha256": proposal_sha,
            "proposal": safe_proposal,
            "execution_provenance": execution_provenance,
            "error_code": None,
        }
    if proposal is not None or proposal_sha is not None:
        raise ReviewEventSetIntegrityError(
            "non-proposed structured result cannot contain a proposal"
        )
    error_code = judgment.get("error_code")
    if (
        not isinstance(error_code, str)
        or _SAFE_ERROR.fullmatch(error_code) is None
    ):
        raise ReviewEventSetIntegrityError(
            "structured proposal error code is invalid"
        )
    return {
        "status": status,
        "provider": provider,
        "proposal_sha256": None,
        "proposal": None,
        "execution_provenance": execution_provenance,
        "error_code": error_code,
    }


def _normalize_provider_visual_evidence(
    raw: Any,
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    event_candidates: set[str],
) -> dict[str, dict[str, Any]]:
    """Validate the exact path-free privacy snapshot hashed by the pipeline."""

    if not isinstance(raw, Mapping) or len(raw) > len(event_candidates):
        raise ReviewEventSetIntegrityError(
            "structured provider visual evidence is invalid"
        )
    normalized: dict[str, dict[str, Any]] = {}
    required_fields = {
        "schema_version",
        "project_id",
        "pipeline_job_id",
        "analysis_job_id",
        "candidate_id",
        "preparation_id",
        "input_revision",
        "state",
        "manifest_sha256",
        "engine_identity_sha256",
        "attachment_count",
        "attachments",
        "automatic_redaction_performed",
        "automatic_privacy_verification",
        "human_verified",
        "review_required",
        "review_record_sha256",
        "path_exposed",
        "preparation_revision",
    }
    attachment_fields = {
        "order",
        "role",
        "source_ref",
        "source_sha256",
        "output_sha256",
        "media_type",
        "size_bytes",
        "width",
        "height",
        "face_detection_count",
        "plate_detection_count",
    }
    for candidate_id, value in sorted(raw.items()):
        if (
            not isinstance(candidate_id, str)
            or candidate_id not in event_candidates
            or _CANDIDATE_ID.fullmatch(candidate_id) is None
            or not isinstance(value, Mapping)
            or set(value) != required_fields
        ):
            raise ReviewEventSetIntegrityError(
                "structured provider visual evidence is invalid"
            )
        row = dict(value)
        state = row.get("state")
        attachments = row.get("attachments")
        attachment_count = row.get("attachment_count")
        review_record_sha256 = row.get("review_record_sha256")
        if (
            row.get("schema_version")
            != "trafficvisson.provider-visual-evidence-preparation.v1"
            or row.get("project_id") != str(project_id)
            or row.get("pipeline_job_id") != str(pipeline_job_id)
            or row.get("analysis_job_id") != str(analysis_job_id)
            or row.get("candidate_id") != candidate_id
            or not isinstance(row.get("preparation_id"), str)
            or re.fullmatch(
                r"[a-f0-9]{24}", row["preparation_id"]
            )
            is None
            or any(
                not isinstance(row.get(field), str)
                or _SHA256.fullmatch(row[field]) is None
                for field in (
                    "input_revision",
                    "manifest_sha256",
                    "engine_identity_sha256",
                    "preparation_revision",
                )
            )
            or state
            not in {
                "awaiting_deidentification_review",
                "verified_for_provider_transmission",
                "rejected_by_privacy_review",
            }
            or isinstance(attachment_count, bool)
            or not isinstance(attachment_count, int)
            or not 4 <= attachment_count <= 6
            or not isinstance(attachments, list)
            or len(attachments) != attachment_count
            or not isinstance(
                row.get("automatic_redaction_performed"), bool
            )
            or row.get("automatic_privacy_verification") is not False
            or not isinstance(row.get("human_verified"), bool)
            or not isinstance(row.get("review_required"), bool)
            or row.get("path_exposed") is not False
        ):
            raise ReviewEventSetIntegrityError(
                "structured provider visual evidence is invalid"
            )
        if (
            state == "awaiting_deidentification_review"
            and (
                row["human_verified"] is not False
                or row["review_required"] is not True
                or review_record_sha256 is not None
            )
        ) or (
            state == "verified_for_provider_transmission"
            and (
                row["human_verified"] is not True
                or row["review_required"] is not False
                or not isinstance(review_record_sha256, str)
                or _SHA256.fullmatch(review_record_sha256) is None
            )
        ) or (
            state == "rejected_by_privacy_review"
            and (
                row["human_verified"] is not False
                or row["review_required"] is not False
                or not isinstance(review_record_sha256, str)
                or _SHA256.fullmatch(review_record_sha256) is None
            )
        ):
            raise ReviewEventSetIntegrityError(
                "structured provider visual evidence review is invalid"
            )
        normalized_attachments = []
        for index, attachment in enumerate(attachments):
            if (
                not isinstance(attachment, Mapping)
                or set(attachment) != attachment_fields
            ):
                raise ReviewEventSetIntegrityError(
                    "structured provider visual attachment is invalid"
                )
            item = dict(attachment)
            role = (
                "candidate_representative"
                if index == 0
                else "similar_case"
            )
            if (
                item.get("order") != index
                or item.get("role") != role
                or not isinstance(item.get("source_ref"), str)
                or _SAFE_IDENTIFIER.fullmatch(item["source_ref"])
                is None
                or any(
                    not isinstance(item.get(field), str)
                    or _SHA256.fullmatch(item[field]) is None
                    for field in (
                        "source_sha256",
                        "output_sha256",
                    )
                )
                or item.get("media_type") != "image/png"
                or any(
                    isinstance(item.get(field), bool)
                    or not isinstance(item.get(field), int)
                    for field in (
                        "size_bytes",
                        "width",
                        "height",
                        "face_detection_count",
                        "plate_detection_count",
                    )
                )
                or not 1 <= item["size_bytes"] <= 5 * 1024 * 1024
                or not 1 <= item["width"] <= 1_024
                or not 1 <= item["height"] <= 1_024
                or not 0 <= item["face_detection_count"] <= 10_000
                or not 0 <= item["plate_detection_count"] <= 10_000
            ):
                raise ReviewEventSetIntegrityError(
                    "structured provider visual attachment is invalid"
                )
            normalized_attachments.append(item)
        row["attachments"] = normalized_attachments
        declared_revision = row.pop("preparation_revision")
        if _sha256_json(row) != declared_revision:
            raise ReviewEventSetIntegrityError(
                "structured provider visual evidence revision does not match"
            )
        row["preparation_revision"] = declared_revision
        normalized[candidate_id] = row
    return normalized


def _normalize_structured(
    checkpoint: Mapping[str, Any],
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    pipeline_job_id: UUID,
    source_sha256: str,
    detection_revision: str,
    temporal_revision: str,
    visual_revision: Optional[str],
    event_candidates: set[str],
    limits: ReviewEventSetLimits,
) -> tuple[dict[str, Mapping[str, Any]], str]:
    revision = _required_sha(
        checkpoint,
        "structured_vision_revision",
        "structured vision checkpoint",
    )
    if (
        checkpoint.get("analysis_job_id") != str(analysis_job_id)
        or checkpoint.get("source_sha256") != source_sha256
        or checkpoint.get("detection_revision") != detection_revision
        or checkpoint.get("temporal_merge_revision") != temporal_revision
    ):
        raise ReviewEventSetConflict(
            "structured proposal revision chain does not match"
        )
    judgments = checkpoint.get("structured_judgments_by_candidate")
    if (
        not isinstance(judgments, Mapping)
        or set(judgments) != event_candidates
    ):
        raise ReviewEventSetIntegrityError(
            "structured proposals do not match final review events"
        )
    provider = checkpoint.get("selected_provider")
    provider_state = checkpoint.get("provider_state")
    consent = checkpoint.get("transmission_consent")
    if (
        not isinstance(provider, str)
        or len(provider) > 64
        or not isinstance(provider_state, str)
        or len(provider_state) > 64
        or not isinstance(consent, bool)
    ):
        raise ReviewEventSetIntegrityError(
            "structured provider checkpoint is invalid"
        )
    core = {
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "detection_revision": detection_revision,
        "temporal_merge_revision": temporal_revision,
        "visual_reference_revision": visual_revision,
        "provider": provider,
        "provider_state": provider_state,
        "transmission_consent": consent,
        "judgments": judgments,
    }
    if "provider_visual_evidence_by_candidate" in checkpoint:
        provider_visual_evidence = (
            _normalize_provider_visual_evidence(
                checkpoint.get(
                    "provider_visual_evidence_by_candidate"
                ),
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                pipeline_job_id=pipeline_job_id,
                event_candidates=event_candidates,
            )
        )
        core["provider_visual_evidence"] = (
            provider_visual_evidence
        )
    if _sha256_json(core) != revision:
        raise ReviewEventSetIntegrityError(
            "structured vision checkpoint revision does not match"
        )
    result = {
        candidate_id: _normalize_structured_proposal(
            judgment,
            candidate_id=candidate_id,
            limits=limits,
        )
        for candidate_id, judgment in judgments.items()
        if isinstance(judgment, Mapping)
    }
    if set(result) != event_candidates:
        raise ReviewEventSetIntegrityError(
            "structured proposal rows are invalid"
        )
    return result, revision


def _event_id(set_identity: Mapping[str, Any], candidate_id: str) -> str:
    digest = hashlib.sha256(
        _canonical_json([set_identity, candidate_id])
    ).hexdigest()
    return f"rve_{digest[:32]}"


def _set_id(identity: Mapping[str, Any]) -> str:
    return f"res_{_sha256_json(identity)[:32]}"


def _event_row(event: ReviewEvent) -> dict[str, Any]:
    return asdict(event)


def _parse_artifact(
    raw: Any,
    *,
    overlay_id: str,
    kind: str,
    limits: ReviewEventSetLimits,
) -> ReviewEventArtifactReference:
    if not isinstance(raw, Mapping) or set(raw) != {
        "artifact_ref",
        "artifact_id",
        "media_type",
        "size_bytes",
        "sha256",
    }:
        raise ReviewEventSetIntegrityError(
            "review-event artifact metadata is invalid"
        )
    artifact_id = raw["artifact_id"]
    artifact_ref = raw["artifact_ref"]
    expected_media = "image/jpeg" if kind == "image" else "video/mp4"
    maximum = (
        limits.max_image_bytes if kind == "image" else limits.max_clip_bytes
    )
    artifact_ref_match = (
        re.fullmatch(
            r"candidate-evidence:(ceo_[a-f0-9]{32}):"
            r"(cea_[a-f0-9]{32})",
            str(artifact_ref),
        )
        if isinstance(artifact_ref, str)
        else None
    )
    if (
        not isinstance(artifact_id, str)
        or _ARTIFACT_ID.fullmatch(artifact_id) is None
        or artifact_ref_match is None
        or artifact_ref_match.group(2) != artifact_id
        or raw["media_type"] != expected_media
        or isinstance(raw["size_bytes"], bool)
        or not isinstance(raw["size_bytes"], int)
        or not 1 <= raw["size_bytes"] <= maximum
        or not isinstance(raw["sha256"], str)
        or _SHA256.fullmatch(raw["sha256"]) is None
    ):
        raise ReviewEventSetIntegrityError(
            "review-event artifact metadata is invalid"
        )
    return ReviewEventArtifactReference(**dict(raw))


def _parse_event(
    raw: Any,
    set_identity: Mapping[str, Any],
    *,
    limits: ReviewEventSetLimits,
) -> ReviewEvent:
    expected = {
        "event_id",
        "representative_candidate_id",
        "evidence_candidate_ids",
        "issue_type",
        "start_seconds",
        "end_seconds",
        "merged_frame_count",
        "urgent_review",
        "representative_image",
        "preview_clip",
        "evidence_digests",
        "detector_summary",
        "ocr_observations",
        "sam2_summary",
        "similar_cases",
        "structured_proposal",
        "checkpoint_revisions",
        "event_revision",
        "review_only",
        "annotations_created",
        "incidents_created",
        "training_members_created",
        "report_truth_records_created",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise ReviewEventSetIntegrityError("review-event row is invalid")
    representative = _candidate_id(
        raw["representative_candidate_id"],
        "review-event representative identity",
    )
    evidence_ids_raw = raw["evidence_candidate_ids"]
    issue_type = raw["issue_type"]
    start = raw["start_seconds"]
    end = raw["end_seconds"]
    merged_count = raw["merged_frame_count"]
    if (
        raw["event_id"] != _event_id(set_identity, representative)
        or _EVENT_ID.fullmatch(str(raw["event_id"])) is None
        or raw["review_only"] is not True
        or raw["annotations_created"] != 0
        or raw["incidents_created"] != 0
        or raw["training_members_created"] != 0
        or raw["report_truth_records_created"] != 0
        or not isinstance(evidence_ids_raw, list)
        or not 1 <= len(evidence_ids_raw) <= limits.max_group_candidates
        or representative not in evidence_ids_raw
        or len(evidence_ids_raw) != len(set(evidence_ids_raw))
        or not isinstance(issue_type, str)
        or _ISSUE_TYPE.fullmatch(issue_type) is None
        or isinstance(start, bool)
        or not isinstance(start, (int, float))
        or not math.isfinite(float(start))
        or not 0 <= float(start) <= limits.max_video_seconds
        or isinstance(end, bool)
        or not isinstance(end, (int, float))
        or not math.isfinite(float(end))
        or not float(start) <= float(end) <= limits.max_video_seconds
        or isinstance(merged_count, bool)
        or not isinstance(merged_count, int)
        or not len(evidence_ids_raw)
        <= merged_count
        <= 100_000
        or not isinstance(raw["urgent_review"], bool)
        or not isinstance(raw["evidence_digests"], list)
        or not isinstance(raw["ocr_observations"], list)
        or not isinstance(raw["similar_cases"], list)
        or not isinstance(raw["checkpoint_revisions"], Mapping)
    ):
        raise ReviewEventSetIntegrityError("review-event row is invalid")
    evidence_ids = tuple(
        _candidate_id(item, "review-event evidence identity")
        for item in raw["evidence_candidate_ids"]
    )
    digest_rows = []
    for item in raw["evidence_digests"]:
        if not isinstance(item, Mapping):
            raise ReviewEventSetIntegrityError(
                "review-event evidence digest is invalid"
            )
        try:
            digest_row = ReviewEventEvidenceDigest(**dict(item))
        except TypeError as error:
            raise ReviewEventSetIntegrityError(
                "review-event evidence digest is invalid"
            ) from error
        if (
            digest_row.candidate_id not in evidence_ids
            or any(
                _SHA256.fullmatch(value) is None
                for value in (
                    digest_row.candidate_metadata_sha256,
                    digest_row.image_sha256,
                    digest_row.clip_sha256,
                )
            )
        ):
            raise ReviewEventSetIntegrityError(
                "review-event evidence digest is invalid"
            )
        digest_rows.append(digest_row)
    if (
        len(digest_rows) != len(evidence_ids)
        or {item.candidate_id for item in digest_rows} != set(evidence_ids)
        or not isinstance(raw["event_revision"], str)
        or _SHA256.fullmatch(raw["event_revision"]) is None
    ):
        raise ReviewEventSetIntegrityError("review-event row is invalid")
    detector_summary = raw["detector_summary"]
    detector_summary_keys = {
        "candidate_was_proposed",
        "dino_status",
        "dedicated_detector_status",
        "ocr_status",
        "proposal_sources",
    }
    detector_summary_variants = {
        frozenset(detector_summary_keys),
        frozenset(detector_summary_keys | {"dino_evidence"}),
        frozenset(
            detector_summary_keys
            | {
                "dino_evidence",
                "cross_source_sign_merge",
            }
        ),
    }
    if (
        not isinstance(detector_summary, Mapping)
        or frozenset(detector_summary)
        not in detector_summary_variants
        or not isinstance(
            detector_summary["candidate_was_proposed"], bool
        )
        or detector_summary["dino_status"]
        not in {
            "completed",
            "zero_detections",
            "model_unavailable",
            "failed",
        }
        or detector_summary["dedicated_detector_status"]
        not in {
            "completed",
            "zero_detections",
            "model_unavailable",
            "failed",
        }
        or detector_summary["ocr_status"]
        not in {
            "completed",
            "zero_detections",
            "model_unavailable",
            "failed",
        }
        or not isinstance(detector_summary["proposal_sources"], list)
        or len(detector_summary["proposal_sources"]) > 16
        or len(detector_summary["proposal_sources"])
        != len(set(detector_summary["proposal_sources"]))
        or any(
            not isinstance(item, str)
            or _SAFE_IDENTIFIER.fullmatch(item) is None
            for item in detector_summary["proposal_sources"]
        )
    ):
        raise ReviewEventSetIntegrityError(
            "review-event detector summary is invalid"
        )
    cross_source_sign_merge = detector_summary.get(
        "cross_source_sign_merge"
    )
    if cross_source_sign_merge is not None:
        if (
            not isinstance(cross_source_sign_merge, Mapping)
            or cross_source_sign_merge.get("schema_version")
            != "trafficvisson.cross-source-sign-merge-proof.v1"
            or cross_source_sign_merge.get("algorithm")
            != "orb-affine-ransac-adjacent-sign-v1"
            or cross_source_sign_merge.get("review_only") is not True
            or cross_source_sign_merge.get(
                "automatic_annotation_allowed"
            )
            is not False
            or cross_source_sign_merge.get(
                "automatic_incident_creation_allowed"
            )
            is not False
            or not isinstance(
                cross_source_sign_merge.get("proof_revision"), str
            )
            or _SHA256.fullmatch(
                cross_source_sign_merge["proof_revision"]
            )
            is None
            or _sha256_json(
                {
                    key: value
                    for key, value in cross_source_sign_merge.items()
                    if key != "proof_revision"
                }
            )
            != cross_source_sign_merge["proof_revision"]
        ):
            raise ReviewEventSetIntegrityError(
                "review-event cross-source merge proof is invalid"
            )
    dino_evidence = detector_summary.get("dino_evidence")
    if dino_evidence is not None:
        peak = (
            dino_evidence.get("peak_evidence")
            if isinstance(dino_evidence, Mapping)
            else None
        )
        evidence_frame_ids = (
            dino_evidence.get("evidence_frame_ids")
            if isinstance(dino_evidence, Mapping)
            else None
        )
        if (
            not isinstance(dino_evidence, Mapping)
            or set(dino_evidence)
            != {
                "temporal_proposal_id",
                "merged_frame_count",
                "evidence_frame_ids",
                "peak_evidence",
            }
            or not isinstance(
                dino_evidence.get(
                    "temporal_proposal_id"
                ),
                str,
            )
            or not isinstance(
                dino_evidence.get("merged_frame_count"),
                int,
            )
            or (
                cross_source_sign_merge is None
                and dino_evidence["merged_frame_count"]
                != merged_count
            )
            or (
                cross_source_sign_merge is not None
                and not 1
                <= dino_evidence["merged_frame_count"]
                <= merged_count
            )
            or not isinstance(evidence_frame_ids, list)
            or not evidence_frame_ids
            or len(evidence_frame_ids)
            != len(set(evidence_frame_ids))
            or any(
                _CANDIDATE_ID.fullmatch(str(item)) is None
                for item in evidence_frame_ids
            )
            or not isinstance(peak, Mapping)
            or not isinstance(peak.get("image_sha256"), str)
            or _SHA256.fullmatch(peak["image_sha256"])
            is None
            or not isinstance(
                peak.get("normalized_box"), Mapping
            )
        ):
            raise ReviewEventSetIntegrityError(
                "review-event DINO evidence summary is invalid"
            )
    ocr_rows = []
    for item in raw["ocr_observations"]:
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {
                "text",
                "confidence",
                "kind",
                "source_candidate_ids",
            }
        ):
            raise ReviewEventSetIntegrityError(
                "review-event OCR observation is invalid"
            )
        text = item["text"]
        confidence = item["confidence"]
        kind = item["kind"]
        source_ids = item["source_candidate_ids"]
        if (
            not isinstance(text, str)
            or not text
            or "\x00" in text
            or len(text) > 512
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0 <= float(confidence) <= 1
            or (
                kind is not None
                and (
                    not isinstance(kind, str)
                    or "\x00" in kind
                    or len(kind) > 64
                )
            )
            or not isinstance(source_ids, list)
            or not source_ids
            or len(source_ids) != len(set(source_ids))
            or any(
                _candidate_id(
                    source_id,
                    "review-event OCR source candidate",
                )
                not in evidence_ids
                for source_id in source_ids
            )
        ):
            raise ReviewEventSetIntegrityError(
                "review-event OCR observation is invalid"
            )
        ocr_rows.append(dict(item))
    if len(ocr_rows) > limits.max_ocr_observations_per_event:
        raise ReviewEventSetLimitError(
            "review-event OCR observation count is invalid"
        )
    sam2_summary = raw["sam2_summary"]
    if (
        not isinstance(sam2_summary, Mapping)
        or set(sam2_summary)
        != {
            "status",
            "proposal_ref",
            "proposal_sha256",
            "tracked_frame_count",
            "tracking_performed",
        }
        or sam2_summary["status"]
        not in {
            "available",
            "failed",
            "model_unavailable",
            "not_requested",
        }
        or isinstance(sam2_summary["tracked_frame_count"], bool)
        or not isinstance(sam2_summary["tracked_frame_count"], int)
        or not 0 <= sam2_summary["tracked_frame_count"] <= 256
        or not isinstance(sam2_summary["tracking_performed"], bool)
        or (
            sam2_summary["status"] == "available"
            and (
                not isinstance(sam2_summary["proposal_sha256"], str)
                or _SHA256.fullmatch(
                    sam2_summary["proposal_sha256"]
                )
                is None
                or sam2_summary["proposal_ref"]
                != (
                    "sam2-proposal:"
                    f"{sam2_summary['proposal_sha256']}"
                )
                or sam2_summary["tracked_frame_count"] < 1
            )
        )
        or (
            sam2_summary["status"] != "available"
            and (
                sam2_summary["proposal_ref"] is not None
                or sam2_summary["proposal_sha256"] is not None
                or sam2_summary["tracked_frame_count"] != 0
                or sam2_summary["tracking_performed"] is not False
            )
        )
    ):
        raise ReviewEventSetIntegrityError(
            "review-event SAM2 summary is invalid"
        )
    similar_rows = []
    seen_cases: set[tuple[str, str]] = set()
    for item in raw["similar_cases"]:
        if not isinstance(item, Mapping):
            raise ReviewEventSetIntegrityError(
                "review-event similar case is invalid"
            )
        normalized_case = _normalize_case(
            item,
            limits=limits,
        )
        if (
            normalized_case is None
            or normalized_case != dict(item)
        ):
            raise ReviewEventSetIntegrityError(
                "review-event similar case normalization changed"
            )
        identity = (
            normalized_case["source_id"],
            normalized_case["case_id"],
        )
        if identity in seen_cases:
            raise ReviewEventSetIntegrityError(
                "review-event similar cases contain duplicates"
            )
        seen_cases.add(identity)
        similar_rows.append(
            _safe_json(
                normalized_case,
                label="review-event similar case",
                max_text=limits.max_text_length,
            )
        )
    if len(similar_rows) > limits.max_similar_cases_per_event:
        raise ReviewEventSetLimitError(
            "review-event similar-case count is invalid"
        )
    structured_summary = raw["structured_proposal"]
    structured_keys = {
        "status",
        "provider",
        "proposal_sha256",
        "proposal",
        "error_code",
    }
    if (
        not isinstance(structured_summary, Mapping)
        or frozenset(structured_summary)
        not in {
            frozenset(structured_keys),
            frozenset(structured_keys | {"execution_provenance"}),
        }
    ):
        raise ReviewEventSetIntegrityError(
            "review-event structured proposal is invalid"
        )
    structured_input = {
            "candidate_id": representative,
            "status": structured_summary["status"],
            "provider": structured_summary["provider"],
            "proposal_sha256": structured_summary["proposal_sha256"],
            "proposal": structured_summary["proposal"],
            "error_code": structured_summary["error_code"],
            "review_only": True,
            "automatic_annotation_allowed": False,
            "automatic_incident_allowed": False,
            "automatic_training_membership_allowed": False,
            "automatic_report_truth_allowed": False,
        }
    execution_provenance = structured_summary.get(
        "execution_provenance"
    )
    if execution_provenance is not None:
        if not isinstance(execution_provenance, Mapping):
            raise ReviewEventSetIntegrityError(
                "review-event structured execution provenance is invalid"
            )
        for field in (
            "input_sha256",
            "prompt_sha256",
            "idempotency_key",
            "request_provenance_sha256",
            "attachment_manifest_sha256",
            "attachment_sha256s",
            "attachment_verification_record_sha256s",
            "model",
            "comparison_mode",
        ):
            structured_input[field] = execution_provenance.get(field)
        structured_input["provenance_sha256"] = (
            execution_provenance.get("execution_provenance_sha256")
        )
        structured_input["provider_command_invoked"] = True
    structured_row = _normalize_structured_proposal(
        structured_input,
        candidate_id=representative,
        limits=limits,
    )
    if structured_row != dict(structured_summary):
        raise ReviewEventSetIntegrityError(
            "review-event structured proposal normalization changed"
        )
    expected_revision_keys = {
        "detection_revision",
        "dino_revision",
        "ocr_revision",
        "sam2_revision",
        "temporal_merge_revision",
        "visual_reference_revision",
        "structured_vision_revision",
    }
    if (
        set(raw["checkpoint_revisions"]) != expected_revision_keys
        or any(
            not isinstance(value, str)
            or _SHA256.fullmatch(value) is None
            for value in raw["checkpoint_revisions"].values()
        )
    ):
        raise ReviewEventSetIntegrityError(
            "review-event checkpoint revisions are invalid"
        )
    payload = dict(raw)
    payload.pop("event_revision")
    if _sha256_json(payload) != raw["event_revision"]:
        raise ReviewEventSetIntegrityError(
            "review-event revision does not match"
        )
    return ReviewEvent(
        event_id=raw["event_id"],
        representative_candidate_id=representative,
        evidence_candidate_ids=evidence_ids,
        issue_type=issue_type,
        start_seconds=float(start),
        end_seconds=float(end),
        merged_frame_count=merged_count,
        urgent_review=raw["urgent_review"],
        representative_image=_parse_artifact(
            raw["representative_image"],
            overlay_id=str(set_identity["overlay_id"]),
            kind="image",
            limits=limits,
        ),
        preview_clip=_parse_artifact(
            raw["preview_clip"],
            overlay_id=str(set_identity["overlay_id"]),
            kind="clip",
            limits=limits,
        ),
        evidence_digests=tuple(digest_rows),
        detector_summary=dict(detector_summary),
        ocr_observations=tuple(ocr_rows),
        sam2_summary=dict(sam2_summary),
        similar_cases=tuple(dict(item) for item in similar_rows),
        structured_proposal=structured_row,
        checkpoint_revisions=dict(raw["checkpoint_revisions"]),
        event_revision=raw["event_revision"],
    )


class ReviewEventSetStore:
    """Persist and re-verify immutable, path-free final review events."""

    def __init__(
        self,
        root: Path,
        *,
        evidence_resolver: ReviewCandidateEvidenceResolver,
        limits: Optional[ReviewEventSetLimits] = None,
    ) -> None:
        path = Path(root)
        if not path.is_absolute():
            raise ValueError("review-event root must be absolute")
        path.mkdir(parents=True, exist_ok=True)
        try:
            resolved = path.resolve(strict=True)
            metadata = resolved.stat()
        except OSError as error:
            raise ReviewEventSetIntegrityError(
                "review-event root is unavailable"
            ) from error
        if (
            path.is_symlink()
            or _is_reparse_point(path)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise ReviewEventSetIntegrityError(
                "review-event root is unsafe"
            )
        if not callable(evidence_resolver):
            raise ValueError("evidence_resolver must be callable")
        self._root = resolved
        self._evidence_resolver = evidence_resolver
        self._limits = limits or ReviewEventSetLimits()
        self._lock = threading.RLock()

    def materialize(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        source_sha256: str,
        dino_checkpoint: Mapping[str, Any],
        ocr_checkpoint: Mapping[str, Any],
        sam2_checkpoint: Mapping[str, Any],
        temporal_checkpoint: Mapping[str, Any],
        similar_case_checkpoint: Mapping[str, Any],
        structured_proposal_checkpoint: Mapping[str, Any],
        status: str = "completed",
        error_code: Optional[str] = None,
    ) -> ReviewEventSetResult:
        for name, value in (
            ("project_id", project_id),
            ("analysis_job_id", analysis_job_id),
            ("pipeline_job_id", pipeline_job_id),
        ):
            if not isinstance(value, UUID):
                raise ValueError(f"{name} must be a UUID")
        if (
            not isinstance(overlay_id, str)
            or _OVERLAY_ID.fullmatch(overlay_id) is None
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or status not in {"completed", "zero", "failed"}
            or (
                status == "failed"
                and (
                    not isinstance(error_code, str)
                    or _SAFE_ERROR.fullmatch(error_code) is None
                )
            )
            or (status != "failed" and error_code is not None)
        ):
            raise ValueError("review-event materialization identity is invalid")

        raw_checkpoints = {
            "dino": dino_checkpoint,
            "ocr": ocr_checkpoint,
            "sam2": sam2_checkpoint,
            "temporal": temporal_checkpoint,
            "similar_cases": similar_case_checkpoint,
            "structured_proposal": structured_proposal_checkpoint,
        }
        checkpoints = {}
        checkpoint_record_sha256 = {}
        for name, raw in raw_checkpoints.items():
            safe = _checkpoint_json(
                raw,
                label=f"{name} checkpoint",
                limits=self._limits,
            )
            _validate_identity_fields(
                safe,
                label=f"{name} checkpoint",
                analysis_job_id=analysis_job_id,
                pipeline_job_id=pipeline_job_id,
                overlay_id=overlay_id,
                source_sha256=source_sha256,
            )
            checkpoints[name] = safe
            checkpoint_record_sha256[name] = _sha256_json(safe)

        evidence = self._resolve_evidence(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
            source_sha256,
        )
        evidence_set, candidates, evidence_revision = _validated_evidence(
            evidence,
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            overlay_id=overlay_id,
            source_sha256=source_sha256,
            limits=self._limits,
        )
        known_candidates = set(candidates)
        dino, detection_revision = _normalize_dino(
            checkpoints["dino"],
            candidates=candidates,
        )
        ocr_by_candidate, ocr_revision, ocr_status = _normalize_ocr(
            checkpoints["ocr"],
            known_candidates=known_candidates,
            limits=self._limits,
        )
        (
            question_ids,
            groups,
            dino_temporal_groups,
            temporal_revision,
        ) = _normalize_temporal(
            checkpoints["temporal"],
            analysis_job_id=analysis_job_id,
            dino_revision=detection_revision,
            known_candidates=known_candidates,
            limits=self._limits,
        )
        dino_candidate_lineage = dino[
            "synthetic_candidate_lineage"
        ]
        if set(dino_candidate_lineage) != set(
            dino_temporal_groups
        ):
            raise ReviewEventSetConflict(
                "DINO temporal groups do not match the exact "
                "candidate evidence overlay"
            )
        for candidate_id, lineage in (
            dino_candidate_lineage.items()
        ):
            temporal_group = dino_temporal_groups[candidate_id]
            if (
                tuple(
                    temporal_group["evidence_proposal_ids"]
                )
                != tuple(lineage["evidence_proposal_ids"])
                or temporal_group["peak_evidence"].get(
                    "dino_proposal_id"
                )
                != lineage["peak_dino_proposal_id"]
                or temporal_group["peak_evidence"].get(
                    "parent_candidate_id"
                )
                not in lineage["parent_candidate_ids"]
                or not set(
                    lineage["parent_candidate_ids"]
                ).issubset(set(dino["candidate_ids"]))
            ):
                raise ReviewEventSetConflict(
                    "DINO temporal group lineage does not match "
                    "the exact detector evidence"
                )
        sam2_by_candidate, sam2_revision = _normalize_sam2(
            checkpoints["sam2"],
            known_candidates=known_candidates,
            dino_candidate_lineage=dino_candidate_lineage,
            dino_temporal_groups=dino_temporal_groups,
        )
        event_candidate_set = set(question_ids)
        similar_by_candidate, similar_record_sha, visual_revision = (
            _normalize_similar(
                checkpoints["similar_cases"],
                event_candidates=event_candidate_set,
                known_candidates=known_candidates,
                limits=self._limits,
            )
        )
        structured_by_candidate, structured_revision = _normalize_structured(
            checkpoints["structured_proposal"],
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            source_sha256=source_sha256,
            detection_revision=detection_revision,
            temporal_revision=temporal_revision,
            visual_revision=visual_revision,
            event_candidates=event_candidate_set,
            limits=self._limits,
        )
        checkpoint_record_sha256["similar_cases"] = similar_record_sha
        checkpoint_revisions = {
            "detection_revision": detection_revision,
            "dino_revision": dino["dino_revision"],
            "ocr_revision": ocr_revision,
            "sam2_revision": sam2_revision,
            "temporal_merge_revision": temporal_revision,
            "visual_reference_revision": (
                visual_revision or similar_record_sha
            ),
            "structured_vision_revision": structured_revision,
        }
        degraded = bool(
            status == "completed"
            and (
                dino["dino_status"]
                in {"model_unavailable", "failed"}
                or dino["dedicated_detector_status"]
                in {"model_unavailable", "failed"}
                or ocr_status in {"model_unavailable", "failed"}
                or any(
                    sam2_by_candidate[candidate_id]["status"]
                    != "available"
                    for candidate_id in event_candidate_set
                )
                or any(
                    structured_by_candidate[candidate_id]["status"]
                    != "proposed"
                    for candidate_id in event_candidate_set
                )
                or any(
                    not 3
                    <= len(similar_by_candidate[candidate_id])
                    <= self._limits.max_similar_cases_per_event
                    for candidate_id in event_candidate_set
                )
            )
        )
        if degraded:
            status = "partial"
            error_code = "degraded_review_capabilities"
        if status == "completed" and not question_ids:
            raise ReviewEventSetConflict(
                "completed review-event set requires at least one event"
            )
        if status == "partial" and not question_ids:
            raise ReviewEventSetConflict(
                "partial review-event set requires at least one event"
            )
        if status in {"zero", "failed"} and question_ids:
            raise ReviewEventSetConflict(
                f"{status} review-event set cannot contain events"
            )

        set_identity = {
            "schema_version": REVIEW_EVENT_SET_SCHEMA,
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "pipeline_job_id": str(pipeline_job_id),
            "overlay_id": overlay_id,
            "source_sha256": source_sha256,
            "overlay_manifest_sha256": (
                evidence_set.overlay_manifest_sha256
            ),
            "candidate_set_metadata_sha256": (
                evidence_set.candidate_set_metadata_sha256
            ),
            "candidate_evidence_revision": evidence_revision,
            "status": status,
            "error_code": error_code,
            "checkpoint_revisions": checkpoint_revisions,
            "checkpoint_record_sha256": checkpoint_record_sha256,
        }
        set_id = _set_id(set_identity)
        events = (
            self._build_events(
                set_identity=set_identity,
                question_ids=question_ids,
                groups=groups,
                candidates=candidates,
                dino=dino,
                dino_temporal_groups=dino_temporal_groups,
                ocr_by_candidate=ocr_by_candidate,
                ocr_status=ocr_status,
                sam2_by_candidate=sam2_by_candidate,
                similar_by_candidate=similar_by_candidate,
                structured_by_candidate=structured_by_candidate,
                checkpoint_revisions=checkpoint_revisions,
            )
            if status in {"completed", "partial"}
            else ()
        )
        set_revision = _sha256_json(
            {
                **set_identity,
                "events": [_event_row(event) for event in events],
                "review_only": True,
                "annotations_created": 0,
                "incidents_created": 0,
                "training_members_created": 0,
                "report_truth_records_created": 0,
            }
        )
        manifest = {
            **set_identity,
            "review_event_set_id": set_id,
            "event_count": len(events),
            "events": [_event_row(event) for event in events],
            "review_event_set_revision": set_revision,
            "created_at": _utc_now(),
            "review_only": True,
            "annotations_created": 0,
            "incidents_created": 0,
            "training_members_created": 0,
            "report_truth_records_created": 0,
        }
        manifest["record_sha256"] = _sha256_json(manifest)
        body = _canonical_json(manifest)
        if len(body) > _MAX_MANIFEST_BYTES:
            raise ReviewEventSetLimitError(
                "review-event manifest exceeded its size bound"
            )
        with self._lock:
            parent = self._scope_directory(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                overlay_id,
                create=True,
            )
            target = parent / set_id
            if target.exists():
                return self._load(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    overlay_id,
                    set_id,
                    idempotent_replay=True,
                )
            staging = parent / f".building-{uuid4().hex}"
            try:
                staging.mkdir(exist_ok=False)
                self._atomic_write(staging / "manifest.json", body)
                try:
                    staging.rename(target)
                except FileExistsError:
                    (staging / "manifest.json").unlink()
                    staging.rmdir()
                if staging.exists():
                    raise ReviewEventSetIntegrityError(
                        "review-event staging directory was not committed"
                    )
            except Exception:
                try:
                    manifest_path = staging / "manifest.json"
                    manifest_path.unlink(missing_ok=True)
                    staging.rmdir()
                except OSError:
                    pass
                raise
            return self._load(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                overlay_id,
                set_id,
                idempotent_replay=False,
            )

    def get(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        review_event_set_id: str,
    ) -> ReviewEventSetResult:
        with self._lock:
            return self._load(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                overlay_id,
                review_event_set_id,
                idempotent_replay=True,
            )

    def list(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
    ) -> tuple[ReviewEventSetResult, ...]:
        with self._lock:
            parent = self._scope_directory(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                overlay_id,
                create=False,
            )
            try:
                children = list(parent.iterdir())
            except OSError as error:
                raise ReviewEventSetIntegrityError(
                    "review-event scope could not be enumerated"
                ) from error
            set_ids = []
            for child in children:
                if (
                    not child.is_dir()
                    or child.is_symlink()
                    or _is_reparse_point(child)
                    or _SET_ID.fullmatch(child.name) is None
                ):
                    raise ReviewEventSetIntegrityError(
                        "review-event scope contains an unexpected entry"
                    )
                set_ids.append(child.name)
            results = [
                self._load(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    overlay_id,
                    set_id,
                    idempotent_replay=True,
                )
                for set_id in sorted(set_ids)
            ]
            results.sort(
                key=lambda item: (item.created_at, item.review_event_set_id)
            )
            return tuple(results)

    def find_candidate_binding(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        candidate_id: str,
    ) -> Optional[tuple[ReviewEventSetResult, ReviewEvent]]:
        """Resolve one candidate across all immutable sets in one pipeline.

        A composite review-event set is stored below its active overlay, but it
        may legitimately contain candidates whose original evidence remains in
        another member overlay.  Looking only below the candidate's own overlay
        therefore loses the binding.  This lookup stays inside the exact
        project/analysis/pipeline scope, re-verifies every discovered set, and
        fails closed if a candidate is present in more than one immutable set.
        """

        if (
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID.fullmatch(candidate_id) is None
        ):
            raise ReviewEventSetNotFound(
                "review-event candidate binding was not found"
            )
        with self._lock:
            try:
                parent = self._pipeline_directory(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    create=False,
                )
            except ReviewEventSetNotFound:
                return None
            try:
                children = list(parent.iterdir())
            except OSError as error:
                raise ReviewEventSetIntegrityError(
                    "review-event pipeline scope could not be enumerated"
                ) from error
            if len(children) > _MAX_PIPELINE_OVERLAY_SCOPES:
                raise ReviewEventSetLimitError(
                    "review-event pipeline has too many overlay scopes"
                )
            overlay_ids = []
            for child in children:
                if (
                    not child.is_dir()
                    or child.is_symlink()
                    or _is_reparse_point(child)
                    or _OVERLAY_ID.fullmatch(child.name) is None
                ):
                    raise ReviewEventSetIntegrityError(
                        "review-event pipeline contains an unexpected entry"
                    )
                overlay_ids.append(child.name)

            matches: list[
                tuple[ReviewEventSetResult, ReviewEvent]
            ] = []
            set_count = 0
            for overlay_id in sorted(overlay_ids):
                event_sets = self.list(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    overlay_id,
                )
                set_count += len(event_sets)
                if set_count > _MAX_PIPELINE_REVIEW_EVENT_SETS:
                    raise ReviewEventSetLimitError(
                        "review-event pipeline has too many immutable sets"
                    )
                for event_set in event_sets:
                    for event in event_set.events:
                        if (
                            event.representative_candidate_id
                            == candidate_id
                        ):
                            matches.append((event_set, event))
                            if len(matches) > 1:
                                raise ReviewEventSetConflict(
                                    "candidate is bound to multiple "
                                    "review event sets"
                                )
            return matches[0] if matches else None

    def resolve_candidate_bindings_batch(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        candidate_ids: Sequence[str],
    ) -> tuple[ReviewCandidateBatchBinding, ...]:
        """Resolve a queue against each immutable set exactly once.

        The complete candidate metadata and ReviewEvent lineage come from the
        same fresh evidence verification.  Missing, duplicate, foreign, or
        multiply-bound candidates fail the entire batch before any question
        catalog can be projected.
        """

        if (
            isinstance(candidate_ids, (str, bytes))
            or not isinstance(candidate_ids, Sequence)
            or not 1 <= len(candidate_ids) <= self._limits.max_events
            or len(candidate_ids) != len(set(candidate_ids))
            or any(
                not isinstance(candidate_id, str)
                or _CANDIDATE_ID.fullmatch(candidate_id) is None
                for candidate_id in candidate_ids
            )
        ):
            raise ReviewEventSetNotFound(
                "review-event candidate batch was not found"
            )
        requested = tuple(candidate_ids)
        requested_set = set(requested)
        with self._lock:
            try:
                parent = self._pipeline_directory(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    create=False,
                )
            except ReviewEventSetNotFound:
                raise ReviewEventSetNotFound(
                    "review-event candidate batch was not found"
                )
            try:
                overlay_children = list(parent.iterdir())
            except OSError as error:
                raise ReviewEventSetIntegrityError(
                    "review-event pipeline scope could not be enumerated"
                ) from error
            if len(overlay_children) > _MAX_PIPELINE_OVERLAY_SCOPES:
                raise ReviewEventSetLimitError(
                    "review-event pipeline has too many overlay scopes"
                )
            overlay_ids = []
            for child in overlay_children:
                if (
                    not child.is_dir()
                    or child.is_symlink()
                    or _is_reparse_point(child)
                    or _OVERLAY_ID.fullmatch(child.name) is None
                ):
                    raise ReviewEventSetIntegrityError(
                        "review-event pipeline contains an unexpected entry"
                    )
                overlay_ids.append(child.name)

            matches: dict[str, ReviewCandidateBatchBinding] = {}
            set_count = 0
            for overlay_id in sorted(overlay_ids):
                scope = self._scope_directory(
                    project_id,
                    analysis_job_id,
                    pipeline_job_id,
                    overlay_id,
                    create=False,
                )
                try:
                    set_children = list(scope.iterdir())
                except OSError as error:
                    raise ReviewEventSetIntegrityError(
                        "review-event scope could not be enumerated"
                    ) from error
                set_ids = []
                for child in set_children:
                    if (
                        not child.is_dir()
                        or child.is_symlink()
                        or _is_reparse_point(child)
                        or _SET_ID.fullmatch(child.name) is None
                    ):
                        raise ReviewEventSetIntegrityError(
                            "review-event scope contains an unexpected entry"
                        )
                    set_ids.append(child.name)
                set_count += len(set_ids)
                if set_count > _MAX_PIPELINE_REVIEW_EVENT_SETS:
                    raise ReviewEventSetLimitError(
                        "review-event pipeline has too many immutable sets"
                    )
                for set_id in sorted(set_ids):
                    event_set, fresh_candidates = (
                        self._load_with_evidence(
                            project_id,
                            analysis_job_id,
                            pipeline_job_id,
                            overlay_id,
                            set_id,
                            idempotent_replay=True,
                        )
                    )
                    for event in event_set.events:
                        candidate_id = (
                            event.representative_candidate_id
                        )
                        if candidate_id not in requested_set:
                            continue
                        if candidate_id in matches:
                            raise ReviewEventSetConflict(
                                "candidate is bound to multiple "
                                "review event sets"
                            )
                        candidate = fresh_candidates.get(candidate_id)
                        metadata = (
                            candidate.get("metadata")
                            if isinstance(candidate, Mapping)
                            else None
                        )
                        metadata_sha256 = (
                            candidate.get(
                                "candidate_metadata_sha256"
                            )
                            if isinstance(candidate, Mapping)
                            else None
                        )
                        if (
                            not isinstance(metadata, Mapping)
                            or metadata.get("candidate_id")
                            != candidate_id
                            or not isinstance(
                                metadata_sha256, str
                            )
                            or _SHA256.fullmatch(
                                metadata_sha256
                            )
                            is None
                            or _sha256_json(metadata)
                            != metadata_sha256
                        ):
                            raise ReviewEventSetIntegrityError(
                                "review-event candidate metadata "
                                "does not match fresh evidence"
                            )
                        matches[candidate_id] = (
                            ReviewCandidateBatchBinding(
                                event_set=event_set,
                                event=event,
                                candidate_metadata=dict(metadata),
                                candidate_metadata_sha256=(
                                    metadata_sha256
                                ),
                            )
                        )
            missing = [
                candidate_id
                for candidate_id in requested
                if candidate_id not in matches
            ]
            if missing:
                raise ReviewEventSetNotFound(
                    "review-event candidate batch was not found"
                )
            return tuple(matches[candidate_id] for candidate_id in requested)

    def resolve_event(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        review_event_set_id: str,
        event_id: str,
    ) -> ReviewEvent:
        result = self.get(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
            review_event_set_id,
        )
        event = next(
            (item for item in result.events if item.event_id == event_id),
            None,
        )
        if event is None:
            raise ReviewEventSetNotFound(
                "review event was not found in the requested set"
            )
        return event

    def _build_events(
        self,
        *,
        set_identity: Mapping[str, Any],
        question_ids: Sequence[str],
        groups: Mapping[str, Mapping[str, Any]],
        candidates: Mapping[str, Mapping[str, Any]],
        dino: Mapping[str, Any],
        dino_temporal_groups: Mapping[
            str, Mapping[str, Any]
        ],
        ocr_by_candidate: Mapping[
            str, tuple[Mapping[str, Any], ...]
        ],
        ocr_status: str,
        sam2_by_candidate: Mapping[str, Mapping[str, Any]],
        similar_by_candidate: Mapping[
            str, tuple[Mapping[str, Any], ...]
        ],
        structured_by_candidate: Mapping[str, Mapping[str, Any]],
        checkpoint_revisions: Mapping[str, str],
    ) -> tuple[ReviewEvent, ...]:
        events = []
        detector_candidate_ids = set(dino["candidate_ids"])
        for representative in question_ids:
            group = groups.get(representative)
            dino_temporal_group = dino_temporal_groups.get(
                representative
            )
            candidate = candidates[representative]
            evidence_ids = (
                tuple(group["evidence_candidate_ids"])
                if group is not None
                else (representative,)
            )
            issue_type = (
                str(group["issue_type"])
                if group is not None
                else str(candidate["issue_type"])
            )
            start = (
                float(group["start_seconds"])
                if group is not None
                else float(candidate["start_seconds"])
            )
            end = (
                float(group["end_seconds"])
                if group is not None
                else float(candidate["end_seconds"])
            )
            urgent = any(
                bool(candidates[item]["urgent_review"])
                for item in evidence_ids
            )
            evidence_digests = tuple(
                ReviewEventEvidenceDigest(
                    candidate_id=item,
                    candidate_metadata_sha256=candidates[item][
                        "candidate_metadata_sha256"
                    ],
                    image_sha256=candidates[item]["image"].sha256,
                    clip_sha256=candidates[item]["clip"].sha256,
                )
                for item in evidence_ids
            )
            detector_summary = {
                "candidate_was_proposed": (
                    representative in detector_candidate_ids
                    or dino_temporal_group is not None
                ),
                "dino_status": dino["dino_status"],
                "dedicated_detector_status": (
                    dino["dedicated_detector_status"]
                ),
                "ocr_status": ocr_status,
                "proposal_sources": list(dino["proposal_sources"]),
            }
            if dino_temporal_group is not None:
                detector_summary["dino_evidence"] = {
                    "temporal_proposal_id": (
                        dino_temporal_group[
                            "temporal_proposal_id"
                        ]
                    ),
                    "merged_frame_count": (
                        dino_temporal_group[
                            "merged_frame_count"
                        ]
                    ),
                    "evidence_frame_ids": list(
                        dino_temporal_group[
                            "evidence_frame_ids"
                        ]
                    ),
                    "peak_evidence": dict(
                        dino_temporal_group["peak_evidence"]
                    ),
                }
                if isinstance(
                    dino_temporal_group.get(
                        "cross_source_merge_proof"
                    ),
                    Mapping,
                ):
                    detector_summary[
                        "cross_source_sign_merge"
                    ] = dict(
                        dino_temporal_group[
                            "cross_source_merge_proof"
                        ]
                    )
            merged_ocr: dict[str, dict[str, Any]] = {}
            for source_candidate_id in evidence_ids:
                for observation in ocr_by_candidate.get(
                    source_candidate_id, ()
                ):
                    observation_key = _sha256_json(observation)
                    existing = merged_ocr.get(observation_key)
                    if existing is None:
                        existing = {
                            **dict(observation),
                            "source_candidate_ids": [],
                        }
                        merged_ocr[observation_key] = existing
                    existing["source_candidate_ids"].append(
                        source_candidate_id
                    )
            if (
                len(merged_ocr)
                > self._limits.max_ocr_observations_per_event
            ):
                raise ReviewEventSetLimitError(
                    "merged OCR observations exceeded the event bound"
                )
            event_ocr = [
                {
                    **row,
                    "source_candidate_ids": sorted(
                        set(row["source_candidate_ids"])
                    ),
                }
                for _key, row in sorted(merged_ocr.items())
            ]
            sam2_summary = dict(
                sam2_by_candidate[representative]
            )
            if dino_temporal_group is not None:
                dino_prompt_ids = list(
                    dino_temporal_group[
                        "sam2_prompt_candidate_ids"
                    ]
                )
                available_sam2 = [
                    sam2_by_candidate[prompt_id]
                    for prompt_id in dino_prompt_ids
                    if prompt_id in sam2_by_candidate
                    and sam2_by_candidate[prompt_id][
                        "status"
                    ]
                    == "available"
                ]
                failed_sam2 = [
                    sam2_by_candidate[prompt_id]
                    for prompt_id in dino_prompt_ids
                    if prompt_id in sam2_by_candidate
                    and sam2_by_candidate[prompt_id][
                        "status"
                    ]
                    == "failed"
                ]
                if available_sam2:
                    sam2_summary = dict(available_sam2[0])
                elif failed_sam2:
                    sam2_summary = {
                        "status": "failed",
                        "proposal_ref": None,
                        "proposal_sha256": None,
                        "tracked_frame_count": 0,
                        "tracking_performed": False,
                    }
            merged_frame_count = (
                max(
                    int(
                        dino_temporal_group[
                            "merged_frame_count"
                        ]
                    ),
                    int(group["detection_count"])
                    if group is not None
                    else 1,
                )
                if dino_temporal_group is not None
                else len(evidence_ids)
            )
            event_without_revision = {
                "event_id": _event_id(set_identity, representative),
                "representative_candidate_id": representative,
                "evidence_candidate_ids": list(evidence_ids),
                "issue_type": issue_type,
                "start_seconds": round(start, 6),
                "end_seconds": round(end, 6),
                "merged_frame_count": merged_frame_count,
                "urgent_review": urgent,
                "representative_image": asdict(candidate["image"]),
                "preview_clip": asdict(candidate["clip"]),
                "evidence_digests": [
                    asdict(item) for item in evidence_digests
                ],
                "detector_summary": detector_summary,
                "ocr_observations": event_ocr,
                "sam2_summary": sam2_summary,
                "similar_cases": [
                    dict(item)
                    for item in similar_by_candidate[representative]
                ],
                "structured_proposal": dict(
                    structured_by_candidate[representative]
                ),
                "checkpoint_revisions": dict(checkpoint_revisions),
                "review_only": True,
                "annotations_created": 0,
                "incidents_created": 0,
                "training_members_created": 0,
                "report_truth_records_created": 0,
            }
            event_revision = _sha256_json(event_without_revision)
            events.append(
                ReviewEvent(
                    event_id=event_without_revision["event_id"],
                    representative_candidate_id=representative,
                    evidence_candidate_ids=evidence_ids,
                    issue_type=issue_type,
                    start_seconds=event_without_revision["start_seconds"],
                    end_seconds=event_without_revision["end_seconds"],
                    merged_frame_count=merged_frame_count,
                    urgent_review=urgent,
                    representative_image=candidate["image"],
                    preview_clip=candidate["clip"],
                    evidence_digests=evidence_digests,
                    detector_summary=detector_summary,
                    ocr_observations=tuple(
                        dict(item) for item in event_ocr
                    ),
                    sam2_summary=sam2_summary,
                    similar_cases=tuple(
                        dict(item)
                        for item in similar_by_candidate[representative]
                    ),
                    structured_proposal=dict(
                        structured_by_candidate[representative]
                    ),
                    checkpoint_revisions=dict(checkpoint_revisions),
                    event_revision=event_revision,
                )
            )
        return tuple(events)

    def _resolve_evidence(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        source_sha256: str,
    ) -> ResolvedReviewCandidateEvidenceSet:
        try:
            return self._evidence_resolver(
                project_id,
                analysis_job_id,
                pipeline_job_id,
                overlay_id,
                source_sha256,
            )
        except ReviewEventSetError:
            raise
        except Exception as error:
            raise ReviewEventSetIntegrityError(
                "candidate evidence could not be freshly verified"
            ) from error

    def _scope_directory(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        *,
        create: bool,
    ) -> Path:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(analysis_job_id, UUID)
            or not isinstance(pipeline_job_id, UUID)
            or not isinstance(overlay_id, str)
            or _OVERLAY_ID.fullmatch(overlay_id) is None
        ):
            raise ReviewEventSetNotFound(
                "review-event scope was not found"
            )
        parent = self._pipeline_directory(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            create=create,
        )
        child = parent / overlay_id
        if create:
            child.mkdir(exist_ok=True)
        try:
            resolved = child.resolve(strict=True)
        except OSError as error:
            raise ReviewEventSetNotFound(
                "review-event scope was not found"
            ) from error
        if (
            resolved.parent != parent
            or child.is_symlink()
            or _is_reparse_point(child)
            or not child.is_dir()
        ):
            raise ReviewEventSetIntegrityError(
                "review-event scope hierarchy is unsafe"
            )
        return resolved

    def _pipeline_directory(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        *,
        create: bool,
    ) -> Path:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(analysis_job_id, UUID)
            or not isinstance(pipeline_job_id, UUID)
        ):
            raise ReviewEventSetNotFound(
                "review-event pipeline scope was not found"
            )
        parent = self._root
        for name in (
            str(project_id),
            str(analysis_job_id),
            str(pipeline_job_id),
        ):
            child = parent / name
            if create:
                child.mkdir(exist_ok=True)
            try:
                resolved = child.resolve(strict=True)
            except OSError as error:
                raise ReviewEventSetNotFound(
                    "review-event scope was not found"
                ) from error
            if (
                resolved.parent != parent
                or child.is_symlink()
                or _is_reparse_point(child)
                or not child.is_dir()
            ):
                raise ReviewEventSetIntegrityError(
                    "review-event scope hierarchy is unsafe"
                )
            parent = resolved
        return parent

    def _set_directory(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        set_id: str,
    ) -> Path:
        if not isinstance(set_id, str) or _SET_ID.fullmatch(set_id) is None:
            raise ReviewEventSetNotFound(
                "review-event set was not found"
            )
        parent = self._scope_directory(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
            create=False,
        )
        path = parent / set_id
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ReviewEventSetNotFound(
                "review-event set was not found"
            ) from error
        if (
            resolved.parent != parent
            or path.is_symlink()
            or _is_reparse_point(path)
            or not path.is_dir()
        ):
            raise ReviewEventSetIntegrityError(
                "review-event set directory is unsafe"
            )
        return resolved

    @staticmethod
    def _atomic_write(path: Path, body: bytes) -> None:
        temporary = path.with_name(
            f".{path.name}.{uuid4().hex}.tmp"
        )
        try:
            with temporary.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as error:
            raise ReviewEventSetIntegrityError(
                "review-event manifest could not be persisted"
            ) from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_manifest(self, path: Path) -> tuple[dict[str, Any], str]:
        try:
            before = path.stat()
        except OSError as error:
            raise ReviewEventSetIntegrityError(
                "review-event manifest is unavailable"
            ) from error
        if (
            path.is_symlink()
            or _is_reparse_point(path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _MAX_MANIFEST_BYTES
        ):
            raise ReviewEventSetIntegrityError(
                "review-event manifest identity is unsafe"
            )
        try:
            body = path.read_bytes()
            after = path.stat()
        except OSError as error:
            raise ReviewEventSetIntegrityError(
                "review-event manifest could not be read"
            ) from error
        if (
            len(body) != before.st_size
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ReviewEventSetIntegrityError(
                "review-event manifest changed while being read"
            )
        return _strict_json(body), hashlib.sha256(body).hexdigest()

    def _load(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        set_id: str,
        *,
        idempotent_replay: bool,
    ) -> ReviewEventSetResult:
        result, _fresh_candidates = self._load_with_evidence(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
            set_id,
            idempotent_replay=idempotent_replay,
        )
        return result

    def _load_with_evidence(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        set_id: str,
        *,
        idempotent_replay: bool,
    ) -> tuple[
        ReviewEventSetResult,
        dict[str, dict[str, Any]],
    ]:
        directory = self._set_directory(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
            set_id,
        )
        try:
            children = list(directory.iterdir())
        except OSError as error:
            raise ReviewEventSetIntegrityError(
                "review-event set could not be enumerated"
            ) from error
        if (
            len(children) != 1
            or children[0].name != "manifest.json"
            or not children[0].is_file()
        ):
            raise ReviewEventSetIntegrityError(
                "review-event set contains unexpected entries"
            )
        manifest, manifest_sha = self._read_manifest(
            directory / "manifest.json"
        )
        record_sha = manifest.get("record_sha256")
        if (
            not isinstance(record_sha, str)
            or _SHA256.fullmatch(record_sha) is None
        ):
            raise ReviewEventSetIntegrityError(
                "review-event manifest record hash is invalid"
            )
        payload = dict(manifest)
        payload.pop("record_sha256")
        if _sha256_json(payload) != record_sha:
            raise ReviewEventSetIntegrityError(
                "review-event manifest record hash does not match"
            )
        expected_keys = {
            "schema_version",
            "review_event_set_id",
            "project_id",
            "analysis_job_id",
            "pipeline_job_id",
            "overlay_id",
            "source_sha256",
            "overlay_manifest_sha256",
            "candidate_set_metadata_sha256",
            "candidate_evidence_revision",
            "status",
            "error_code",
            "checkpoint_revisions",
            "checkpoint_record_sha256",
            "event_count",
            "events",
            "review_event_set_revision",
            "created_at",
            "review_only",
            "annotations_created",
            "incidents_created",
            "training_members_created",
            "report_truth_records_created",
            "record_sha256",
        }
        if (
            set(manifest) != expected_keys
            or manifest["schema_version"] != REVIEW_EVENT_SET_SCHEMA
            or manifest["review_event_set_id"] != set_id
            or manifest["project_id"] != str(project_id)
            or manifest["analysis_job_id"] != str(analysis_job_id)
            or manifest["pipeline_job_id"] != str(pipeline_job_id)
            or manifest["overlay_id"] != overlay_id
            or manifest["status"]
            not in {"completed", "partial", "zero", "failed"}
            or not isinstance(manifest["events"], list)
            or isinstance(manifest["event_count"], bool)
            or manifest["event_count"] != len(manifest["events"])
            or not 0 <= len(manifest["events"]) <= self._limits.max_events
            or not isinstance(manifest["checkpoint_revisions"], Mapping)
            or not isinstance(
                manifest["checkpoint_record_sha256"], Mapping
            )
            or not _is_timestamp(manifest["created_at"])
            or manifest["review_only"] is not True
            or manifest["annotations_created"] != 0
            or manifest["incidents_created"] != 0
            or manifest["training_members_created"] != 0
            or manifest["report_truth_records_created"] != 0
            or any(
                _SHA256.fullmatch(str(manifest.get(key, ""))) is None
                for key in (
                    "source_sha256",
                    "overlay_manifest_sha256",
                    "candidate_set_metadata_sha256",
                    "candidate_evidence_revision",
                    "review_event_set_revision",
                )
            )
            or any(
                not isinstance(value, str)
                or _SHA256.fullmatch(value) is None
                for value in manifest["checkpoint_revisions"].values()
            )
            or any(
                not isinstance(value, str)
                or _SHA256.fullmatch(value) is None
                for value in manifest[
                    "checkpoint_record_sha256"
                ].values()
            )
            or (
                manifest["status"] in {"partial", "failed"}
                and (
                    not isinstance(manifest["error_code"], str)
                    or _SAFE_ERROR.fullmatch(manifest["error_code"]) is None
                )
            )
            or (
                manifest["status"] not in {"partial", "failed"}
                and manifest["error_code"] is not None
            )
            or (
                manifest["status"] in {"completed", "partial"}
                and not manifest["events"]
            )
            or (
                manifest["status"] in {"zero", "failed"}
                and manifest["events"]
            )
        ):
            raise ReviewEventSetIntegrityError(
                "review-event manifest contract is invalid"
            )
        set_identity = {
            key: manifest[key]
            for key in (
                "schema_version",
                "project_id",
                "analysis_job_id",
                "pipeline_job_id",
                "overlay_id",
                "source_sha256",
                "overlay_manifest_sha256",
                "candidate_set_metadata_sha256",
                "candidate_evidence_revision",
                "status",
                "error_code",
                "checkpoint_revisions",
                "checkpoint_record_sha256",
            )
        }
        if _set_id(set_identity) != set_id:
            raise ReviewEventSetIntegrityError(
                "review-event set identity does not match"
            )
        events = tuple(
            _parse_event(raw, set_identity, limits=self._limits)
            for raw in manifest["events"]
        )
        if len({event.event_id for event in events}) != len(events):
            raise ReviewEventSetIntegrityError(
                "review-event manifest contains duplicate events"
            )
        evidence_membership: set[str] = set()
        for event in events:
            if evidence_membership.intersection(
                event.evidence_candidate_ids
            ):
                raise ReviewEventSetIntegrityError(
                    "review events reuse temporal evidence candidates"
                )
            evidence_membership.update(event.evidence_candidate_ids)
        revision_payload = {
            **set_identity,
            "events": [_event_row(event) for event in events],
            "review_only": True,
            "annotations_created": 0,
            "incidents_created": 0,
            "training_members_created": 0,
            "report_truth_records_created": 0,
        }
        if (
            _sha256_json(revision_payload)
            != manifest["review_event_set_revision"]
        ):
            raise ReviewEventSetIntegrityError(
                "review-event set revision does not match"
            )

        # A get/list/resolve is not allowed to trust yesterday's artifact
        # hashes.  The resolver must freshly reload and verify the immutable
        # candidate overlay, then this layer re-binds its digest.
        fresh = self._resolve_evidence(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
            manifest["source_sha256"],
        )
        fresh_set, fresh_candidates, fresh_revision = _validated_evidence(
            fresh,
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            overlay_id=overlay_id,
            source_sha256=manifest["source_sha256"],
            limits=self._limits,
        )
        if (
            fresh_set.overlay_manifest_sha256
            != manifest["overlay_manifest_sha256"]
            or fresh_set.candidate_set_metadata_sha256
            != manifest["candidate_set_metadata_sha256"]
            or fresh_revision != manifest["candidate_evidence_revision"]
        ):
            raise ReviewEventSetIntegrityError(
                "candidate evidence changed after review-event materialization"
            )
        for event in events:
            representative = fresh_candidates.get(
                event.representative_candidate_id
            )
            if (
                representative is None
                or event.representative_image
                != representative["image"]
                or event.preview_clip != representative["clip"]
            ):
                raise ReviewEventSetIntegrityError(
                    "review-event representative artifacts no longer match"
                )
            for digest in event.evidence_digests:
                candidate = fresh_candidates.get(digest.candidate_id)
                if (
                    candidate is None
                    or candidate["candidate_metadata_sha256"]
                    != digest.candidate_metadata_sha256
                    or candidate["image"].sha256 != digest.image_sha256
                    or candidate["clip"].sha256 != digest.clip_sha256
                ):
                    raise ReviewEventSetIntegrityError(
                        "review-event artifact hash no longer matches"
                    )
        result = ReviewEventSetResult(
            schema_version=REVIEW_EVENT_SET_SCHEMA,
            review_event_set_id=set_id,
            project_id=str(project_id),
            analysis_job_id=str(analysis_job_id),
            pipeline_job_id=str(pipeline_job_id),
            overlay_id=overlay_id,
            source_sha256=manifest["source_sha256"],
            overlay_manifest_sha256=manifest[
                "overlay_manifest_sha256"
            ],
            candidate_set_metadata_sha256=manifest[
                "candidate_set_metadata_sha256"
            ],
            candidate_evidence_revision=manifest[
                "candidate_evidence_revision"
            ],
            status=manifest["status"],
            error_code=manifest["error_code"],
            checkpoint_revisions=dict(
                manifest["checkpoint_revisions"]
            ),
            checkpoint_record_sha256=dict(
                manifest["checkpoint_record_sha256"]
            ),
            events=events,
            review_event_set_revision=manifest[
                "review_event_set_revision"
            ],
            manifest_sha256=manifest_sha,
            created_at=manifest["created_at"],
            idempotent_replay=idempotent_replay,
        )
        return result, fresh_candidates


__all__ = [
    "REVIEW_EVENT_SET_SCHEMA",
    "ResolvedReviewCandidateEvidence",
    "ResolvedReviewCandidateEvidenceSet",
    "ReviewCandidateBatchBinding",
    "ReviewCandidateEvidenceResolver",
    "ReviewEvent",
    "ReviewEventArtifactReference",
    "ReviewEventEvidenceDigest",
    "ReviewEventSetConflict",
    "ReviewEventSetError",
    "ReviewEventSetIntegrityError",
    "ReviewEventSetLimitError",
    "ReviewEventSetLimits",
    "ReviewEventSetNotFound",
    "ReviewEventSetResult",
    "ReviewEventSetStore",
]
