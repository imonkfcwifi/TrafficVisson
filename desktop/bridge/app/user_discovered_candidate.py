"""Review-only feedback for promoted-model false negatives.

This module is deliberately downstream of a completed, independent survey
``AnalysisJob`` and its immutable 2 FPS dense-frame index.  A local user may
point at a missed road issue by timestamp plus one normalized point or box.
The service selects the nearest *actual* indexed frame, creates a deterministic
``DenseCandidateDiscoveryResult``, and asks the existing immutable
``CandidateEvidenceOverlayStore`` to materialize the representative JPEG and
2--5 second preview clip.

The result is still only candidate evidence.  This module never creates an
Annotation, Incident, DatasetVersion membership, maintenance decision, report
fact, or model promotion.  A later composite-candidate adapter may add the
returned overlay to a new ``ReviewEventSet`` revision.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID

from .candidate_labels import CandidateLabelError, canonical_issue_type
from .candidate_evidence_overlay import (
    CandidateEvidenceArtifact,
    CandidateEvidenceOverlayCandidate,
    CandidateEvidenceOverlayResult,
)
from .dense_candidate_discovery import (
    DENSE_CANDIDATE_DISCOVERY_SCHEMA,
    DenseCandidateDiscoveryIntegrityError,
    DenseCandidateDiscoveryResult,
    DenseCandidateSeed,
    DenseFrameEvidence,
    resolve_dense_candidate_frame,
)


USER_DISCOVERED_CANDIDATE_SCHEMA = (
    "trafficvisson.user-discovered-candidate.v1"
)
USER_DISCOVERED_REVIEW_FRAME_SCHEMA = (
    "trafficvisson.user-discovered-review-frame.v1"
)
USER_DISCOVERED_PROPOSAL_SOURCE = (
    "local_user_discovered_false_negative"
)
SAM2_USER_DISCOVERY_REQUEST_SCHEMA = (
    "trafficvisson.sam2-user-discovery-request.v1"
)
UNCLASSIFIED_REVIEW_ISSUE_TYPE = "issue_type_unclassified"

_DENSE_INDEX_SCHEMA = "trafficvisson.dense-frame-index.v1"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_FRAME_ID = re.compile(r"^[a-f0-9]{24}$")
_ISSUE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_FORBIDDEN_METADATA_KEY = re.compile(
    r"(?:^|_)(?:path|token|secret|password|credential|auth)(?:_|$)",
    re.IGNORECASE,
)
_MAX_DENSE_INDEX_BYTES = 64 * 1024 * 1024
_MAX_METADATA_BYTES = 64 * 1024
_MAX_SOURCE_DURATION_SECONDS = 24 * 60 * 60
_FALLBACK_ISSUE_TYPE = UNCLASSIFIED_REVIEW_ISSUE_TYPE
_PREVIEW_CLIP_DURATION_SECONDS = 3.0


class UserDiscoveredCandidateError(RuntimeError):
    """Fail-closed error with a stable bridge-facing code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedIndependentSurvey:
    """Server-owned binding for one completed promoted-model survey.

    ``dense_index_path`` is bridge-internal and must never be serialized.
    Every identity beside the path is copied into the immutable candidate
    provenance so a later QA/training projection can prove which promoted
    model missed the user-discovered example.
    """

    project_id: UUID
    analysis_job_id: UUID
    pipeline_job_id: UUID
    analysis_status: str
    survey_purpose: str
    source_sha256: str
    source_duration_seconds: float
    dense_index_path: Path
    dense_index_sha256: str
    dense_sampling_fps: int
    promoted_model_state: str
    promoted_model_version_id: UUID
    promoted_model_artifact_manifest_sha256: str
    promoted_model_inference_sha256: str
    dataset_version_id: UUID
    dataset_hash: str
    training_analysis_job_ids: tuple[UUID, ...]
    fixed_validation_analysis_job_ids: tuple[UUID, ...]
    source_analysis_lineage_sha256: str


@dataclass(frozen=True)
class UserDiscoveredCandidateRequest:
    """Optimistically bound local-user feedback.

    The expected hashes/IDs are renderer-safe concurrency tokens.  They are
    matched against the server-owned ``ResolvedIndependentSurvey`` and are
    never trusted as lineage on their own.
    """

    project_id: UUID
    analysis_job_id: UUID
    pipeline_job_id: UUID
    timestamp_seconds: float
    selection: Mapping[str, Any]
    expected_dense_index_sha256: str
    expected_model_version_id: UUID
    expected_model_artifact_manifest_sha256: str
    expected_model_inference_sha256: str
    expected_source_analysis_lineage_sha256: str
    issue_type_proposal: Optional[str] = None
    request_sam2_mask: bool = True


@dataclass(frozen=True)
class UserDiscoveredCandidateEvidence:
    """Path-free deterministic handoff for overlay/ReviewEventSet integration."""

    schema_version: str
    candidate_kind: str
    request_sha256: str
    project_id: str
    analysis_job_id: str
    pipeline_job_id: str
    source_sha256: str
    dense_index_sha256: str
    selected_frame_id: str
    selected_frame_timestamp_seconds: float
    selected_frame_sha256: str
    requested_timestamp_seconds: float
    selection: Mapping[str, Any]
    issue_type_proposal: Optional[str]
    review_issue_type: str
    promoted_model_version_id: str
    promoted_model_artifact_manifest_sha256: str
    promoted_model_inference_sha256: str
    dataset_version_id: str
    dataset_hash: str
    source_analysis_lineage_sha256: str
    overlay_id: str
    overlay_manifest_sha256: str
    discovery_revision: str
    candidate_id: str
    representative_image: Mapping[str, Any]
    preview_clip: Mapping[str, Any]
    sam2_refinement_request: Optional[Mapping[str, Any]]
    idempotent_replay: bool
    review_only: bool = True
    annotations_created: int = 0
    incidents_created: int = 0
    training_members_created: int = 0
    report_truth_records_created: int = 0

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "selection": dict(self.selection),
            "representative_image": dict(self.representative_image),
            "preview_clip": dict(self.preview_clip),
            "sam2_refinement_request": (
                dict(self.sam2_refinement_request)
                if self.sam2_refinement_request is not None
                else None
            ),
            "review_only": True,
            "annotations_created": 0,
            "incidents_created": 0,
            "training_members_created": 0,
            "report_truth_records_created": 0,
        }


@dataclass(frozen=True)
class UserDiscoveredReviewFrame:
    """One hash-verified frame from the full immutable 2 FPS survey index.

    ``path`` is bridge-internal.  ``to_response`` is the only renderer-facing
    representation and intentionally contains no filesystem reference.
    """

    project_id: UUID
    analysis_job_id: UUID
    pipeline_job_id: UUID
    requested_timestamp_seconds: float
    selected_frame_id: str
    selected_frame_index: int
    selected_frame_timestamp_seconds: float
    selected_frame_sha256: str
    selected_frame_size_bytes: int
    source_duration_seconds: float
    indexed_duration_seconds: float
    dense_index_sha256: str
    promoted_model_version_id: UUID
    source_analysis_lineage_sha256: str
    path: Path

    def to_response(self) -> dict[str, Any]:
        core = {
            "schema_version": USER_DISCOVERED_REVIEW_FRAME_SCHEMA,
            "project_id": str(self.project_id),
            "analysis_job_id": str(self.analysis_job_id),
            "pipeline_job_id": str(self.pipeline_job_id),
            "requested_timestamp_seconds": round(
                self.requested_timestamp_seconds, 6
            ),
            "selected_frame_id": self.selected_frame_id,
            "selected_frame_index": self.selected_frame_index,
            "selected_frame_timestamp_seconds": round(
                self.selected_frame_timestamp_seconds, 6
            ),
            "selected_frame_sha256": self.selected_frame_sha256,
            "selected_frame_size_bytes": self.selected_frame_size_bytes,
            "media_type": "image/jpeg",
            "source_duration_seconds": round(
                self.source_duration_seconds, 6
            ),
            "indexed_duration_seconds": round(
                self.indexed_duration_seconds, 6
            ),
            "sampling_fps": 2,
            "dense_index_sha256": self.dense_index_sha256,
            "promoted_model_version_id": str(
                self.promoted_model_version_id
            ),
            "source_analysis_lineage_sha256": (
                self.source_analysis_lineage_sha256
            ),
            "path_exposed": False,
        }
        return {
            **core,
            "review_frame_sha256": _sha256_json(core),
        }


class IndependentSurveyResolver(Protocol):
    def __call__(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
    ) -> ResolvedIndependentSurvey: ...


class UserDiscoveredEvidenceMaterializer(Protocol):
    """Structural contract implemented by CandidateEvidenceOverlayStore."""

    def materialize(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        discovery: DenseCandidateDiscoveryResult,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> CandidateEvidenceOverlayResult: ...


class Sam2RuntimeProvenanceResolver(Protocol):
    """Return path-free server-owned SAM2 model/runtime provenance."""

    def __call__(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
    ) -> Optional[Mapping[str, Any]]: ...


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
        raise UserDiscoveredCandidateError(
            "user_discovered_metadata_invalid",
            "user-discovered candidate metadata is not canonical JSON",
        ) from error


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(details.st_mode):
        return True
    return bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _read_regular(path: Path, *, maximum_bytes: int) -> tuple[bytes, str]:
    descriptor: Optional[int] = None
    try:
        if not path.is_absolute():
            raise OSError("path must be absolute")
        before = path.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 2
            or before.st_size > maximum_bytes
            or path.is_symlink()
            or _is_link_or_reparse(path)
        ):
            raise OSError("file is not a bounded regular file")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise OSError("file identity changed before read")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        after = path.stat()
        if (
            len(body) != before.st_size
            or len(body) > maximum_bytes
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
        ):
            raise OSError("file identity changed during read")
        return body, digest.hexdigest()
    except OSError as error:
        raise UserDiscoveredCandidateError(
            "dense_index_unavailable",
            "the immutable dense-frame index is unavailable",
        ) from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def pairs_hook(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> Any:
        raise ValueError("non-finite JSON number")

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise UserDiscoveredCandidateError(
            "dense_index_invalid",
            "the immutable dense-frame index is invalid",
        ) from error
    if not isinstance(value, dict):
        raise UserDiscoveredCandidateError(
            "dense_index_invalid",
            "the immutable dense-frame index must be a JSON object",
        )
    return value


def _safe_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise UserDiscoveredCandidateError(
            "user_discovered_metadata_invalid",
            "user-discovered candidate metadata is too deeply nested",
        )
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and (
            len(value) > 4_096 or "\x00" in value
        ):
            raise UserDiscoveredCandidateError(
                "user_discovered_metadata_invalid",
                "user-discovered candidate metadata contains invalid text",
            )
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise UserDiscoveredCandidateError(
                "user_discovered_metadata_invalid",
                "user-discovered candidate metadata is non-finite",
            )
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 1_024:
            raise UserDiscoveredCandidateError(
                "user_discovered_metadata_invalid",
                "user-discovered candidate metadata is too large",
            )
        return [_safe_metadata(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise UserDiscoveredCandidateError(
                "user_discovered_metadata_invalid",
                "user-discovered candidate metadata is too large",
            )
        result: dict[str, Any] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 128
                or _FORBIDDEN_METADATA_KEY.search(key)
            ):
                raise UserDiscoveredCandidateError(
                    "user_discovered_metadata_invalid",
                    "user-discovered candidate metadata contains unsafe keys",
                )
            result[key] = _safe_metadata(item, depth=depth + 1)
        return result
    raise UserDiscoveredCandidateError(
        "user_discovered_metadata_invalid",
        "user-discovered candidate metadata has an unsupported value",
    )


def _selection(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise UserDiscoveredCandidateError(
            "user_discovered_selection_invalid",
            "selection must be a normalized point or box",
        )
    kind = value.get("kind")
    required = (
        {"kind", "x", "y"}
        if kind == "point"
        else {"kind", "x", "y", "width", "height"}
        if kind == "box"
        else set()
    )
    if not required or set(value) != required:
        raise UserDiscoveredCandidateError(
            "user_discovered_selection_invalid",
            "selection must be exactly one normalized point or box",
        )
    numbers: dict[str, float] = {}
    for key in required - {"kind"}:
        raw = value[key]
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
        ):
            raise UserDiscoveredCandidateError(
                "user_discovered_selection_invalid",
                "selection coordinates must be finite numbers",
            )
        numbers[key] = round(float(raw), 8)
    if (
        not 0.0 <= numbers["x"] <= 1.0
        or not 0.0 <= numbers["y"] <= 1.0
    ):
        raise UserDiscoveredCandidateError(
            "user_discovered_selection_invalid",
            "selection coordinates are outside the frame",
        )
    if kind == "box" and (
        not 0.0 < numbers["width"] <= 1.0
        or not 0.0 < numbers["height"] <= 1.0
        or numbers["x"] + numbers["width"] > 1.00000001
        or numbers["y"] + numbers["height"] > 1.00000001
    ):
        raise UserDiscoveredCandidateError(
            "user_discovered_selection_invalid",
            "selection box is outside the frame",
        )
    return {"kind": kind, **numbers}


def _lineage_payload(
    training_ids: Sequence[UUID],
    validation_ids: Sequence[UUID],
) -> dict[str, list[str]]:
    training = sorted(str(value) for value in training_ids)
    validation = sorted(str(value) for value in validation_ids)
    if (
        not training
        or not validation
        or len(training) != len(set(training))
        or len(validation) != len(set(validation))
        or set(training).intersection(validation)
    ):
        raise UserDiscoveredCandidateError(
            "model_source_lineage_invalid",
            "the promoted model source lineage is incomplete or overlapping",
        )
    return {
        "training_analysis_job_ids": training,
        "fixed_validation_analysis_job_ids": validation,
    }


def _validate_request(
    request: UserDiscoveredCandidateRequest,
) -> tuple[dict[str, Any], Optional[str]]:
    if not isinstance(request, UserDiscoveredCandidateRequest):
        raise UserDiscoveredCandidateError(
            "user_discovered_request_invalid",
            "user-discovered candidate request is invalid",
        )
    for name, value in (
        ("project_id", request.project_id),
        ("analysis_job_id", request.analysis_job_id),
        ("pipeline_job_id", request.pipeline_job_id),
        ("expected_model_version_id", request.expected_model_version_id),
    ):
        if not isinstance(value, UUID):
            raise UserDiscoveredCandidateError(
                "user_discovered_request_invalid",
                f"{name} must be a UUID",
            )
    hashes = (
        request.expected_dense_index_sha256,
        request.expected_model_artifact_manifest_sha256,
        request.expected_model_inference_sha256,
        request.expected_source_analysis_lineage_sha256,
    )
    if any(
        not isinstance(value, str) or _SHA256.fullmatch(value) is None
        for value in hashes
    ):
        raise UserDiscoveredCandidateError(
            "user_discovered_request_invalid",
            "expected evidence hashes are invalid",
        )
    if (
        isinstance(request.timestamp_seconds, bool)
        or not isinstance(request.timestamp_seconds, (int, float))
        or not math.isfinite(float(request.timestamp_seconds))
    ):
        raise UserDiscoveredCandidateError(
            "user_discovered_time_invalid",
            "timestamp must be a finite number",
        )
    if not isinstance(request.request_sam2_mask, bool):
        raise UserDiscoveredCandidateError(
            "user_discovered_request_invalid",
            "request_sam2_mask must be boolean",
        )
    issue_type = request.issue_type_proposal
    if issue_type is not None:
        if (
            not isinstance(issue_type, str)
            or _ISSUE_TYPE.fullmatch(issue_type) is None
        ):
            raise UserDiscoveredCandidateError(
                "user_discovered_issue_type_invalid",
                "issue type proposal is invalid",
            )
        try:
            issue_type = canonical_issue_type(issue_type)
        except CandidateLabelError as error:
            raise UserDiscoveredCandidateError(
                "user_discovered_issue_type_invalid",
                "issue type proposal is not a supported review type",
            ) from error
    return _selection(request.selection), issue_type


def _validate_grant(
    grant: ResolvedIndependentSurvey,
    request: UserDiscoveredCandidateRequest,
) -> dict[str, list[str]]:
    if not isinstance(grant, ResolvedIndependentSurvey):
        raise UserDiscoveredCandidateError(
            "independent_survey_unavailable",
            "the independent survey binding is unavailable",
        )
    if (
        grant.project_id != request.project_id
        or grant.analysis_job_id != request.analysis_job_id
        or grant.pipeline_job_id != request.pipeline_job_id
    ):
        raise UserDiscoveredCandidateError(
            "independent_survey_scope_mismatch",
            "the independent survey belongs to another project or job",
        )
    if (
        grant.analysis_status != "completed"
        or grant.survey_purpose != "independent_promoted_model_qa"
        or grant.promoted_model_state != "active"
    ):
        raise UserDiscoveredCandidateError(
            "independent_survey_incomplete",
            "only a completed independent active-model survey may be reviewed",
        )
    if (
        not isinstance(grant.source_sha256, str)
        or _SHA256.fullmatch(grant.source_sha256) is None
        or not isinstance(grant.dense_index_sha256, str)
        or _SHA256.fullmatch(grant.dense_index_sha256) is None
        or grant.dense_sampling_fps != 2
        or not isinstance(grant.dense_index_path, Path)
        or not grant.dense_index_path.is_absolute()
    ):
        raise UserDiscoveredCandidateError(
            "independent_survey_binding_invalid",
            "the independent survey dense-frame binding is invalid",
        )
    if (
        isinstance(grant.source_duration_seconds, bool)
        or not isinstance(grant.source_duration_seconds, (int, float))
        or not math.isfinite(float(grant.source_duration_seconds))
        or not 2.0 <= float(grant.source_duration_seconds)
        <= _MAX_SOURCE_DURATION_SECONDS
    ):
        raise UserDiscoveredCandidateError(
            "independent_survey_binding_invalid",
            "the independent survey duration is invalid",
        )
    if (
        not isinstance(grant.promoted_model_version_id, UUID)
        or not isinstance(grant.dataset_version_id, UUID)
        or not isinstance(grant.training_analysis_job_ids, tuple)
        or not isinstance(
            grant.fixed_validation_analysis_job_ids, tuple
        )
        or any(
            not isinstance(value, UUID)
            for value in (
                *grant.training_analysis_job_ids,
                *grant.fixed_validation_analysis_job_ids,
            )
        )
    ):
        raise UserDiscoveredCandidateError(
            "model_source_lineage_invalid",
            "the promoted model identity is invalid",
        )
    for value in (
        grant.promoted_model_artifact_manifest_sha256,
        grant.promoted_model_inference_sha256,
        grant.dataset_hash,
        grant.source_analysis_lineage_sha256,
    ):
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise UserDiscoveredCandidateError(
                "model_source_lineage_invalid",
                "the promoted model provenance is invalid",
            )
    lineage = _lineage_payload(
        grant.training_analysis_job_ids,
        grant.fixed_validation_analysis_job_ids,
    )
    if _sha256_json(lineage) != grant.source_analysis_lineage_sha256:
        raise UserDiscoveredCandidateError(
            "model_source_lineage_tampered",
            "the promoted model source lineage digest changed",
        )
    source_jobs = set(
        grant.training_analysis_job_ids
    ) | set(grant.fixed_validation_analysis_job_ids)
    if grant.analysis_job_id in source_jobs:
        raise UserDiscoveredCandidateError(
            "model_source_video_reuse_forbidden",
            "a training or fixed-validation analysis cannot be reused for QA",
        )
    if (
        request.expected_dense_index_sha256
        != grant.dense_index_sha256
        or request.expected_model_version_id
        != grant.promoted_model_version_id
        or request.expected_model_artifact_manifest_sha256
        != grant.promoted_model_artifact_manifest_sha256
        or request.expected_model_inference_sha256
        != grant.promoted_model_inference_sha256
        or request.expected_source_analysis_lineage_sha256
        != grant.source_analysis_lineage_sha256
    ):
        raise UserDiscoveredCandidateError(
            "independent_survey_revision_conflict",
            "the displayed survey/model revision is stale",
        )
    return lineage


def _index_frames(
    grant: ResolvedIndependentSurvey,
) -> tuple[list[Mapping[str, Any]], float]:
    body, digest = _read_regular(
        grant.dense_index_path,
        maximum_bytes=_MAX_DENSE_INDEX_BYTES,
    )
    if digest != grant.dense_index_sha256:
        raise UserDiscoveredCandidateError(
            "dense_index_hash_mismatch",
            "the immutable dense-frame index hash changed",
        )
    payload = _strict_json_object(body)
    provenance = payload.get("provenance")
    frames = payload.get("frames")
    if (
        payload.get("schema_version") != _DENSE_INDEX_SCHEMA
        or not isinstance(provenance, Mapping)
        or provenance.get("schema_version") != _DENSE_INDEX_SCHEMA
        or provenance.get("source_sha256") != grant.source_sha256
        or provenance.get("sampling_fps") != 2
        or not isinstance(frames, list)
        or not frames
        or payload.get("frame_count") != len(frames)
        or provenance.get("frame_count") != len(frames)
    ):
        raise UserDiscoveredCandidateError(
            "dense_index_invalid",
            "the immutable dense-frame index binding is invalid",
        )
    indexed_duration = provenance.get("indexed_duration_seconds")
    if (
        isinstance(indexed_duration, bool)
        or not isinstance(indexed_duration, (int, float))
        or not math.isfinite(float(indexed_duration))
        or not 0.0 < float(indexed_duration)
        <= float(grant.source_duration_seconds) + 0.000001
    ):
        raise UserDiscoveredCandidateError(
            "dense_index_invalid",
            "the immutable dense-frame duration is invalid",
        )
    return frames, round(float(indexed_duration), 6)


def _nearest_frame_with_duration(
    grant: ResolvedIndependentSurvey,
    timestamp_seconds: float,
) -> tuple[DenseFrameEvidence, float]:
    frames, indexed_duration = _index_frames(grant)
    requested = float(timestamp_seconds)
    if requested < 0.0 or requested > indexed_duration + 0.000001:
        raise UserDiscoveredCandidateError(
            "user_discovered_time_out_of_bounds",
            "timestamp is outside the immutable dense-frame coverage",
        )
    candidates: list[tuple[float, float, int, str]] = []
    for raw in frames:
        if not isinstance(raw, Mapping):
            raise UserDiscoveredCandidateError(
                "dense_index_invalid",
                "the immutable dense-frame index contains invalid rows",
            )
        frame_id = raw.get("frame_id")
        frame_index = raw.get("frame_index")
        frame_timestamp = raw.get("timestamp_seconds")
        if (
            not isinstance(frame_id, str)
            or _FRAME_ID.fullmatch(frame_id) is None
            or isinstance(frame_index, bool)
            or not isinstance(frame_index, int)
            or frame_index < 0
            or isinstance(frame_timestamp, bool)
            or not isinstance(frame_timestamp, (int, float))
            or not math.isfinite(float(frame_timestamp))
        ):
            raise UserDiscoveredCandidateError(
                "dense_index_invalid",
                "the immutable dense-frame index contains invalid rows",
            )
        candidates.append(
            (
                abs(float(frame_timestamp) - requested),
                float(frame_timestamp),
                frame_index,
                frame_id,
            )
        )
    _, expected_timestamp, _frame_index, frame_id = min(candidates)
    try:
        resolved = resolve_dense_candidate_frame(
            dense_index_path=grant.dense_index_path,
            source_sha256=grant.source_sha256,
            dense_index_sha256=grant.dense_index_sha256,
            frame_id=frame_id,
        )
    except DenseCandidateDiscoveryIntegrityError as error:
        raise UserDiscoveredCandidateError(
            "dense_frame_integrity_failed",
            "the selected dense-frame evidence failed integrity verification",
        ) from error
    if abs(resolved.timestamp_seconds - expected_timestamp) > 0.000001:
        raise UserDiscoveredCandidateError(
            "dense_frame_integrity_failed",
            "the selected dense-frame timestamp changed",
        )
    return resolved, indexed_duration


def _nearest_frame(
    grant: ResolvedIndependentSurvey,
    timestamp_seconds: float,
) -> DenseFrameEvidence:
    return _nearest_frame_with_duration(grant, timestamp_seconds)[0]


def _review_grant_request(
    grant: ResolvedIndependentSurvey,
    *,
    timestamp_seconds: float,
) -> UserDiscoveredCandidateRequest:
    """Build a server-owned self-check request for read-only QA browsing."""

    return UserDiscoveredCandidateRequest(
        project_id=grant.project_id,
        analysis_job_id=grant.analysis_job_id,
        pipeline_job_id=grant.pipeline_job_id,
        timestamp_seconds=timestamp_seconds,
        selection={"kind": "point", "x": 0.5, "y": 0.5},
        expected_dense_index_sha256=grant.dense_index_sha256,
        expected_model_version_id=grant.promoted_model_version_id,
        expected_model_artifact_manifest_sha256=(
            grant.promoted_model_artifact_manifest_sha256
        ),
        expected_model_inference_sha256=(
            grant.promoted_model_inference_sha256
        ),
        expected_source_analysis_lineage_sha256=(
            grant.source_analysis_lineage_sha256
        ),
        issue_type_proposal=None,
        request_sam2_mask=False,
    )


def resolve_user_discovered_review_frame(
    grant: ResolvedIndependentSurvey,
    *,
    timestamp_seconds: float,
) -> UserDiscoveredReviewFrame:
    """Resolve an arbitrary full-journey QA time to the nearest actual frame."""

    if (
        isinstance(timestamp_seconds, bool)
        or not isinstance(timestamp_seconds, (int, float))
        or not math.isfinite(float(timestamp_seconds))
        or not 0.0
        <= float(timestamp_seconds)
        <= _MAX_SOURCE_DURATION_SECONDS
    ):
        raise UserDiscoveredCandidateError(
            "user_discovered_time_out_of_bounds",
            "timestamp is outside the immutable dense-frame coverage",
        )
    request = _review_grant_request(
        grant,
        timestamp_seconds=float(timestamp_seconds),
    )
    _validate_grant(grant, request)
    frame, indexed_duration = _nearest_frame_with_duration(
        grant,
        float(timestamp_seconds),
    )
    if (
        frame.media_type != "image/jpeg"
        or frame.size_bytes <= 0
        or not frame.path.is_absolute()
    ):
        raise UserDiscoveredCandidateError(
            "dense_frame_integrity_failed",
            "the selected dense-frame evidence is invalid",
        )
    return UserDiscoveredReviewFrame(
        project_id=grant.project_id,
        analysis_job_id=grant.analysis_job_id,
        pipeline_job_id=grant.pipeline_job_id,
        requested_timestamp_seconds=float(timestamp_seconds),
        selected_frame_id=frame.frame_id,
        selected_frame_index=frame.frame_index,
        selected_frame_timestamp_seconds=frame.timestamp_seconds,
        selected_frame_sha256=frame.sha256,
        selected_frame_size_bytes=frame.size_bytes,
        source_duration_seconds=float(grant.source_duration_seconds),
        indexed_duration_seconds=indexed_duration,
        dense_index_sha256=grant.dense_index_sha256,
        promoted_model_version_id=grant.promoted_model_version_id,
        source_analysis_lineage_sha256=(
            grant.source_analysis_lineage_sha256
        ),
        path=frame.path,
    )


def resolve_user_discovered_review_frame_artifact(
    grant: ResolvedIndependentSurvey,
    *,
    frame_id: str,
) -> UserDiscoveredReviewFrame:
    """Resolve an exact frame id without accepting renderer paths or hashes."""

    if not isinstance(frame_id, str) or _FRAME_ID.fullmatch(frame_id) is None:
        raise UserDiscoveredCandidateError(
            "dense_frame_identity_invalid",
            "the requested dense-frame identity is invalid",
        )
    request = _review_grant_request(grant, timestamp_seconds=0.0)
    _validate_grant(grant, request)
    frames, indexed_duration = _index_frames(grant)
    selected = next(
        (
            raw
            for raw in frames
            if isinstance(raw, Mapping)
            and raw.get("frame_id") == frame_id
        ),
        None,
    )
    if not isinstance(selected, Mapping):
        raise UserDiscoveredCandidateError(
            "dense_frame_not_found",
            "the requested dense-frame is not part of this survey",
        )
    timestamp = selected.get("timestamp_seconds")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
    ):
        raise UserDiscoveredCandidateError(
            "dense_index_invalid",
            "the immutable dense-frame index contains invalid rows",
        )
    resolved = resolve_user_discovered_review_frame(
        grant,
        timestamp_seconds=float(timestamp),
    )
    if resolved.selected_frame_id != frame_id:
        raise UserDiscoveredCandidateError(
            "dense_frame_integrity_failed",
            "the requested dense-frame identity changed",
        )
    return UserDiscoveredReviewFrame(
        **{
            **resolved.__dict__,
            "indexed_duration_seconds": indexed_duration,
        }
    )


def _sam2_request(
    *,
    request_sha256: str,
    request: UserDiscoveredCandidateRequest,
    selection: Mapping[str, Any],
    grant: ResolvedIndependentSurvey,
    frame: DenseFrameEvidence,
    runtime_provenance: Optional[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    if not request.request_sam2_mask:
        return None
    if selection["kind"] == "point":
        point = {"x": selection["x"], "y": selection["y"]}
    else:
        point = {
            "x": round(selection["x"] + selection["width"] / 2.0, 8),
            "y": round(selection["y"] + selection["height"] / 2.0, 8),
        }
    safe_runtime = (
        _safe_metadata(runtime_provenance)
        if runtime_provenance is not None
        else None
    )
    if safe_runtime is not None and not isinstance(safe_runtime, dict):
        raise UserDiscoveredCandidateError(
            "sam2_runtime_provenance_invalid",
            "SAM2 runtime provenance must be a JSON object",
        )
    payload = {
        "schema_version": SAM2_USER_DISCOVERY_REQUEST_SCHEMA,
        "project_id": str(request.project_id),
        "analysis_job_id": str(request.analysis_job_id),
        "pipeline_job_id": str(request.pipeline_job_id),
        "request_sha256": request_sha256,
        "dense_index_sha256": grant.dense_index_sha256,
        "frame_id": frame.frame_id,
        "frame_sha256": frame.sha256,
        "selection": dict(selection),
        "point_prompt": point,
        "status": (
            "requested_with_runtime_provenance"
            if safe_runtime is not None
            else "requested_pending_runtime_binding"
        ),
        "runtime_provenance": safe_runtime,
        "review_only": True,
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_ingest": False,
    }
    payload["request_revision"] = _sha256_json(payload)
    if len(_canonical_json(payload)) > _MAX_METADATA_BYTES:
        raise UserDiscoveredCandidateError(
            "sam2_runtime_provenance_invalid",
            "SAM2 request provenance is too large",
        )
    return payload


def _discovery(
    *,
    request: UserDiscoveredCandidateRequest,
    selection: Mapping[str, Any],
    issue_type_proposal: Optional[str],
    grant: ResolvedIndependentSurvey,
    lineage: Mapping[str, Any],
    frame: DenseFrameEvidence,
    request_sha256: str,
    sam2_request: Optional[Mapping[str, Any]],
) -> DenseCandidateDiscoveryResult:
    review_issue_type = issue_type_proposal or _FALLBACK_ISSUE_TYPE
    normalized_box = (
        {
            "x": selection["x"],
            "y": selection["y"],
            "width": selection["width"],
            "height": selection["height"],
        }
        if selection["kind"] == "box"
        else None
    )
    proposal_provenance = {
        "schema_version": USER_DISCOVERED_CANDIDATE_SCHEMA,
        "request_sha256": request_sha256,
        "discovery_kind": "promoted_model_false_negative",
        "selection": dict(selection),
        "issue_type_proposal": issue_type_proposal,
        "score_semantics": "local_user_requested_review_not_probability",
        "promoted_model_version_id": str(
            grant.promoted_model_version_id
        ),
        "promoted_model_artifact_manifest_sha256": (
            grant.promoted_model_artifact_manifest_sha256
        ),
        "promoted_model_inference_sha256": (
            grant.promoted_model_inference_sha256
        ),
        "dataset_version_id": str(grant.dataset_version_id),
        "dataset_hash": grant.dataset_hash,
        "source_analysis_lineage": dict(lineage),
        "source_analysis_lineage_sha256": (
            grant.source_analysis_lineage_sha256
        ),
        "sam2_refinement_request": (
            dict(sam2_request) if sam2_request is not None else None
        ),
        "review_only": True,
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_ingest": False,
        "automatic_report_truth": False,
    }
    proposal_provenance_sha256 = _sha256_json(
        [
            {
                "frame_id": frame.frame_id,
                "proposal_source": USER_DISCOVERED_PROPOSAL_SOURCE,
                "provenance": proposal_provenance,
            }
        ]
    )
    candidate_material = {
        "schema_version": USER_DISCOVERED_CANDIDATE_SCHEMA,
        "project_id": str(request.project_id),
        "analysis_job_id": str(request.analysis_job_id),
        "pipeline_job_id": str(request.pipeline_job_id),
        "source_sha256": grant.source_sha256,
        "dense_index_sha256": grant.dense_index_sha256,
        "frame_id": frame.frame_id,
        "frame_sha256": frame.sha256,
        "request_sha256": request_sha256,
        "review_issue_type": review_issue_type,
        "proposal_provenance_sha256": proposal_provenance_sha256,
    }
    candidate_id = _sha256_json(candidate_material)[:20]
    candidate = DenseCandidateSeed(
        candidate_id=candidate_id,
        issue_type=review_issue_type,
        start_seconds=round(
            max(
                0.0,
                frame.timestamp_seconds
                - _PREVIEW_CLIP_DURATION_SECONDS / 2.0,
            ),
            6,
        ),
        end_seconds=round(
            min(
                grant.source_duration_seconds,
                frame.timestamp_seconds
                + _PREVIEW_CLIP_DURATION_SECONDS / 2.0,
            ),
            6,
        ),
        representative_timestamp_seconds=round(
            frame.timestamp_seconds, 6
        ),
        representative_frame_id=frame.frame_id,
        representative_frame_sha256=frame.sha256,
        score=1.0,
        normalized_box=normalized_box,
        ocr_text=None,
        urgent_review=(review_issue_type == "road_debris"),
        supporting_frame_ids=(frame.frame_id,),
        proposal_sources=(USER_DISCOVERED_PROPOSAL_SOURCE,),
        representative_proposal_provenance=proposal_provenance,
        proposal_provenance_sha256=proposal_provenance_sha256,
    )
    provider_states = (
        {
            "provider": "local_user_discovered_candidate",
            "state": "ready",
            "discovery_kind": "promoted_model_false_negative",
            "promoted_model_version_id": str(
                grant.promoted_model_version_id
            ),
            "promoted_model_artifact_manifest_sha256": (
                grant.promoted_model_artifact_manifest_sha256
            ),
            "promoted_model_inference_sha256": (
                grant.promoted_model_inference_sha256
            ),
            "dataset_version_id": str(grant.dataset_version_id),
            "dataset_hash": grant.dataset_hash,
            "source_analysis_lineage_sha256": (
                grant.source_analysis_lineage_sha256
            ),
            "review_only": True,
        },
    )
    revision_payload = {
        "schema_version": DENSE_CANDIDATE_DISCOVERY_SCHEMA,
        "project_id": str(request.project_id),
        "source_sha256": grant.source_sha256,
        "dense_index_sha256": grant.dense_index_sha256,
        "sampling_fps": 2,
        "scanned_frame_count": 1,
        "provider_states": list(provider_states),
        "candidates": [asdict(candidate)],
        "review_only": True,
        "automatic_annotation_creation": False,
        "automatic_incident_creation": False,
        "automatic_training_ingest": False,
        "automatic_report_truth": False,
    }
    return DenseCandidateDiscoveryResult(
        schema_version=DENSE_CANDIDATE_DISCOVERY_SCHEMA,
        project_id=str(request.project_id),
        source_sha256=grant.source_sha256,
        dense_index_sha256=grant.dense_index_sha256,
        sampling_fps=2,
        scanned_frame_count=1,
        candidates=(candidate,),
        provider_states=provider_states,
        discovery_revision=_sha256_json(revision_payload),
    )


def _artifact_reference(
    artifact: CandidateEvidenceArtifact,
    *,
    expected_media_type: str,
) -> dict[str, Any]:
    if (
        not isinstance(artifact, CandidateEvidenceArtifact)
        or not isinstance(artifact.artifact_id, str)
        or _ARTIFACT_ID.fullmatch(artifact.artifact_id) is None
        or artifact.media_type != expected_media_type
        or isinstance(artifact.size_bytes, bool)
        or not isinstance(artifact.size_bytes, int)
        or artifact.size_bytes < 1
        or not isinstance(artifact.sha256, str)
        or _SHA256.fullmatch(artifact.sha256) is None
    ):
        raise UserDiscoveredCandidateError(
            "user_discovered_evidence_invalid",
            "materialized candidate evidence is invalid",
        )
    return {
        "artifact_id": artifact.artifact_id,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


def _validate_materialized(
    result: CandidateEvidenceOverlayResult,
    *,
    request: UserDiscoveredCandidateRequest,
    grant: ResolvedIndependentSurvey,
    discovery: DenseCandidateDiscoveryResult,
    frame: DenseFrameEvidence,
) -> CandidateEvidenceOverlayCandidate:
    if (
        not isinstance(result, CandidateEvidenceOverlayResult)
        or result.project_id != str(request.project_id)
        or result.analysis_job_id != str(request.analysis_job_id)
        or result.pipeline_job_id != str(request.pipeline_job_id)
        or result.source_sha256 != grant.source_sha256
        or result.discovery_revision != discovery.discovery_revision
        or result.dense_index_sha256 != grant.dense_index_sha256
        or not isinstance(result.overlay_id, str)
        or not result.overlay_id
        or not isinstance(result.manifest_sha256, str)
        or _SHA256.fullmatch(result.manifest_sha256) is None
        or len(result.candidates) != 1
        or result.review_only is not True
        or result.annotations_created != 0
        or result.incidents_created != 0
        or result.training_members_created != 0
        or result.report_truth_records_created != 0
    ):
        raise UserDiscoveredCandidateError(
            "user_discovered_evidence_invalid",
            "candidate evidence materialization crossed its immutable scope",
        )
    candidate = result.candidates[0]
    expected = discovery.candidates[0]
    if (
        not isinstance(candidate, CandidateEvidenceOverlayCandidate)
        or candidate.candidate_id != expected.candidate_id
        or candidate.issue_type != expected.issue_type
        or candidate.representative_frame_id != frame.frame_id
        or candidate.representative_frame_sha256 != frame.sha256
        or candidate.representative_timestamp_seconds
        != round(frame.timestamp_seconds, 6)
        or tuple(candidate.proposal_sources)
        != (USER_DISCOVERED_PROPOSAL_SOURCE,)
        or candidate.image.sha256 != frame.sha256
    ):
        raise UserDiscoveredCandidateError(
            "user_discovered_evidence_invalid",
            "candidate evidence does not match the selected dense frame",
        )
    _artifact_reference(
        candidate.image, expected_media_type="image/jpeg"
    )
    _artifact_reference(candidate.clip, expected_media_type="video/mp4")
    return candidate


class UserDiscoveredCandidateService:
    """Create one immutable review candidate from a model QA miss."""

    def __init__(
        self,
        *,
        survey_resolver: IndependentSurveyResolver,
        evidence_materializer: UserDiscoveredEvidenceMaterializer,
        sam2_runtime_provenance_resolver: Optional[
            Sam2RuntimeProvenanceResolver
        ] = None,
    ) -> None:
        if not callable(survey_resolver):
            raise ValueError("survey_resolver must be callable")
        if not hasattr(evidence_materializer, "materialize"):
            raise ValueError(
                "evidence_materializer must implement materialize"
            )
        if (
            sam2_runtime_provenance_resolver is not None
            and not callable(sam2_runtime_provenance_resolver)
        ):
            raise ValueError(
                "sam2_runtime_provenance_resolver must be callable"
            )
        self._survey_resolver = survey_resolver
        self._materializer = evidence_materializer
        self._sam2_provenance = sam2_runtime_provenance_resolver

    def create(
        self,
        request: UserDiscoveredCandidateRequest,
        *,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> UserDiscoveredCandidateEvidence:
        if cancel is not None and not callable(cancel):
            raise ValueError("cancel must be callable")
        if cancel is not None and cancel():
            raise UserDiscoveredCandidateError(
                "user_discovered_candidate_cancelled",
                "user-discovered candidate creation was cancelled",
            )
        selection, issue_type_proposal = _validate_request(request)
        try:
            grant = self._survey_resolver(
                request.project_id,
                request.analysis_job_id,
                request.pipeline_job_id,
            )
        except UserDiscoveredCandidateError:
            raise
        except Exception as error:
            raise UserDiscoveredCandidateError(
                "independent_survey_unavailable",
                "the independent survey binding could not be resolved",
            ) from error
        lineage = _validate_grant(grant, request)
        frame = _nearest_frame(grant, request.timestamp_seconds)
        request_material = {
            "schema_version": USER_DISCOVERED_CANDIDATE_SCHEMA,
            "project_id": str(request.project_id),
            "analysis_job_id": str(request.analysis_job_id),
            "pipeline_job_id": str(request.pipeline_job_id),
            "source_sha256": grant.source_sha256,
            "dense_index_sha256": grant.dense_index_sha256,
            "requested_timestamp_seconds": round(
                float(request.timestamp_seconds), 6
            ),
            "selected_frame_id": frame.frame_id,
            "selected_frame_timestamp_seconds": round(
                frame.timestamp_seconds, 6
            ),
            "selected_frame_sha256": frame.sha256,
            "selection": selection,
            "issue_type_proposal": issue_type_proposal,
            "request_sam2_mask": request.request_sam2_mask,
            "promoted_model_version_id": str(
                grant.promoted_model_version_id
            ),
            "promoted_model_artifact_manifest_sha256": (
                grant.promoted_model_artifact_manifest_sha256
            ),
            "promoted_model_inference_sha256": (
                grant.promoted_model_inference_sha256
            ),
            "dataset_version_id": str(grant.dataset_version_id),
            "dataset_hash": grant.dataset_hash,
            "source_analysis_lineage_sha256": (
                grant.source_analysis_lineage_sha256
            ),
        }
        request_sha256 = _sha256_json(request_material)
        runtime_provenance = None
        if request.request_sam2_mask and self._sam2_provenance is not None:
            try:
                runtime_provenance = self._sam2_provenance(
                    request.project_id,
                    request.analysis_job_id,
                    request.pipeline_job_id,
                )
            except UserDiscoveredCandidateError:
                raise
            except Exception as error:
                raise UserDiscoveredCandidateError(
                    "sam2_runtime_provenance_unavailable",
                    "SAM2 runtime provenance could not be resolved",
                ) from error
        sam2_request = _sam2_request(
            request_sha256=request_sha256,
            request=request,
            selection=selection,
            grant=grant,
            frame=frame,
            runtime_provenance=runtime_provenance,
        )
        discovery = _discovery(
            request=request,
            selection=selection,
            issue_type_proposal=issue_type_proposal,
            grant=grant,
            lineage=lineage,
            frame=frame,
            request_sha256=request_sha256,
            sam2_request=sam2_request,
        )
        if cancel is not None and cancel():
            raise UserDiscoveredCandidateError(
                "user_discovered_candidate_cancelled",
                "user-discovered candidate creation was cancelled",
            )
        try:
            materialized = self._materializer.materialize(
                project_id=request.project_id,
                analysis_job_id=request.analysis_job_id,
                pipeline_job_id=request.pipeline_job_id,
                discovery=discovery,
                cancel=cancel,
            )
        except UserDiscoveredCandidateError:
            raise
        except Exception as error:
            raise UserDiscoveredCandidateError(
                "user_discovered_evidence_materialization_failed",
                "candidate evidence could not be materialized",
            ) from error
        candidate = _validate_materialized(
            materialized,
            request=request,
            grant=grant,
            discovery=discovery,
            frame=frame,
        )
        image = _artifact_reference(
            candidate.image, expected_media_type="image/jpeg"
        )
        clip = {
            **_artifact_reference(
                candidate.clip, expected_media_type="video/mp4"
            ),
            "duration_seconds": _PREVIEW_CLIP_DURATION_SECONDS,
            "duration_contract": "candidate_overlay_limit_2_to_5_seconds",
        }
        return UserDiscoveredCandidateEvidence(
            schema_version=USER_DISCOVERED_CANDIDATE_SCHEMA,
            candidate_kind="user_discovered",
            request_sha256=request_sha256,
            project_id=str(request.project_id),
            analysis_job_id=str(request.analysis_job_id),
            pipeline_job_id=str(request.pipeline_job_id),
            source_sha256=grant.source_sha256,
            dense_index_sha256=grant.dense_index_sha256,
            selected_frame_id=frame.frame_id,
            selected_frame_timestamp_seconds=round(
                frame.timestamp_seconds, 6
            ),
            selected_frame_sha256=frame.sha256,
            requested_timestamp_seconds=round(
                float(request.timestamp_seconds), 6
            ),
            selection=selection,
            issue_type_proposal=issue_type_proposal,
            review_issue_type=(
                issue_type_proposal or _FALLBACK_ISSUE_TYPE
            ),
            promoted_model_version_id=str(
                grant.promoted_model_version_id
            ),
            promoted_model_artifact_manifest_sha256=(
                grant.promoted_model_artifact_manifest_sha256
            ),
            promoted_model_inference_sha256=(
                grant.promoted_model_inference_sha256
            ),
            dataset_version_id=str(grant.dataset_version_id),
            dataset_hash=grant.dataset_hash,
            source_analysis_lineage_sha256=(
                grant.source_analysis_lineage_sha256
            ),
            overlay_id=materialized.overlay_id,
            overlay_manifest_sha256=materialized.manifest_sha256,
            discovery_revision=discovery.discovery_revision,
            candidate_id=candidate.candidate_id,
            representative_image=image,
            preview_clip=clip,
            sam2_refinement_request=sam2_request,
            idempotent_replay=materialized.idempotent_replay,
        )


__all__ = [
    "IndependentSurveyResolver",
    "ResolvedIndependentSurvey",
    "SAM2_USER_DISCOVERY_REQUEST_SCHEMA",
    "UNCLASSIFIED_REVIEW_ISSUE_TYPE",
    "Sam2RuntimeProvenanceResolver",
    "USER_DISCOVERED_CANDIDATE_SCHEMA",
    "USER_DISCOVERED_PROPOSAL_SOURCE",
    "USER_DISCOVERED_REVIEW_FRAME_SCHEMA",
    "UserDiscoveredCandidateError",
    "UserDiscoveredCandidateEvidence",
    "UserDiscoveredCandidateRequest",
    "UserDiscoveredCandidateService",
    "UserDiscoveredEvidenceMaterializer",
    "UserDiscoveredReviewFrame",
    "resolve_user_discovered_review_frame",
    "resolve_user_discovered_review_frame_artifact",
]
