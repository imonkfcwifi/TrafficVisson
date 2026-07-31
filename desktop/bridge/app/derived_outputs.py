"""Server-owned derived outputs for one reviewed pipeline provenance.

This module is deliberately independent from FastAPI and Electron.  It turns
an already verified pipeline review ledger into one deterministic export
bundle below the bridge data root.  The returned checkpoint contains only
opaque identities, counts and digests; evidence paths are passed directly to
``artifact_export`` and are never serialized.

Report preparation is an optional, injected boundary.  The service never calls
it without explicit transmission consent, a ready provider and an approved
semantic template map.  It never invokes HWP COM and never invents a
``DocumentEditPlan`` when that boundary is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException

from .approved_incident_authority import (
    ApprovedIncidentAuthorityError,
    build_approved_incident_output_authority,
    canonical_sha256 as _authority_sha256,
    validate_survey_incident_authority,
)
from .artifact_export import (
    ArtifactExportError,
    ArtifactExportResult,
    EvidenceExportSource,
    export_artifacts,
)
from .candidate_labels import CandidateLabelError
from .dataset_versions import DatasetVersionError, LabelProjectionError
from .experimental_proposals import ExperimentalProposalError
from .gpx_tracks import GPX_EXPORT_PROVENANCE_SCHEMA, GpxTrackError
from .report_deidentified_evidence import (
    ReportDeidentifiedEvidenceError,
)


DERIVED_OUTPUT_SCHEMA = "trafficvisson.derived-outputs.v1"
DERIVED_CHECKPOINT_SCHEMA = "trafficvisson.derived-output-checkpoint.v1"
DERIVED_ARTIFACT_ROOT_BINDING_SCHEMA = (
    "trafficvisson.derived-artifact-root-binding.v1"
)
MAX_DERIVED_RECORD_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BUNDLE_ID = re.compile(
    r"^trafficsupervision-export-[0-9a-f-]{36}-[A-Za-z0-9._-]+$"
)
_REPORT_PROVIDERS = frozenset({"codex", "claude", "local", "none"})
_DERIVED_ARTIFACT_ROOT_BINDING_FILE = (
    "derived-artifact-root-binding.json"
)
_NON_MAINTENANCE_OBSERVATION_TYPES = frozenset(
    {
        "guide_sign_identity",
        "tunnel_identity",
    }
)
_REPORT_OBSERVATION_TEXT_BY_TYPE = {
    "guide_sign_identity": "이정표 표기 및 위치 확인",
    "tunnel_identity": "터널명 표기 확인",
}
_LABEL_SOURCE_BY_REVIEW_ACTOR = {
    "local_user": "local_user_review",
    "structured_llm_assisted": "structured_llm_assisted",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _report_incident_action(
    answer: Mapping[str, Any],
) -> Optional[str]:
    """Return bounded report wording without changing maintenance truth."""

    action = answer.get("maintenance_action")
    if isinstance(action, str) and action:
        return action
    issue_type = answer.get("selected_issue_type")
    return (
        _REPORT_OBSERVATION_TEXT_BY_TYPE.get(issue_type)
        if isinstance(issue_type, str)
        else None
    )


_SAFE_REPORT_PREVIEW_ERROR_CODES = frozenset(
    {
        "exact_sample_plan_binding_unavailable",
        "provider_deidentified_evidence_required",
        "provider_report_preview_validation_failed",
        "report_preview_attempt_conflict",
        "current_report_content_invalid",
        "current_report_provenance_invalid",
        "current_report_policy_invalid",
        "sample_template_provenance_conflict",
        "sample_incident_capacity_exceeded",
        "approved_incident_set_invalid",
        "approved_incident_duplicate",
        "approved_incident_provenance_conflict",
        "approved_incident_content_incomplete",
        "approved_incident_binding_incomplete",
        "incident_evidence_missing",
        "incident_evidence_invalid",
        "incident_evidence_provenance_conflict",
        "deidentified_evidence_not_verified",
        "deidentified_evidence_required",
        "evaluation_matrix_incomplete",
        "evaluation_grid_unavailable",
        "stale_sample_content_neutralization_unavailable",
        "exact_writer_operation_capacity_exceeded",
    }
)


class DerivedOutputsError(RuntimeError):
    """Base class for a bounded, caller-safe derived-output failure."""


class DerivedOutputsConflict(DerivedOutputsError):
    """The supplied review or pipeline provenance is inconsistent."""


class DerivedOutputsCorrupt(DerivedOutputsError):
    """A persisted output record or artifact failed integrity verification."""


class ReportPreviewUnavailable(DerivedOutputsError):
    """The optional report preview boundary could not complete this attempt."""


class DatasetSnapshotFailed(DerivedOutputsError):
    """The reviewed truth could not be frozen before export."""


class CandidateLabels(Protocol):
    def audit_records(
        self,
        project_id: UUID,
        *,
        analysis_job_id: Optional[UUID] = None,
    ) -> list[dict[str, Any]]: ...

    def latest_answers(
        self,
        project_id: UUID,
        *,
        analysis_job_id: Optional[UUID] = None,
        partition: Optional[str] = None,
    ) -> list[dict[str, Any]]: ...


class ProjectProjectionStore(Protocol):
    def project_label_records(
        self,
        project_id: UUID,
        records: list[dict[str, Any]],
        *,
        materialize_analysis_job_ids: Optional[list[UUID]] = None,
        incident_analysis_job_ids: Optional[list[UUID]] = None,
    ) -> list[dict[str, Any]]: ...

    def list_incidents(
        self,
        project_id: UUID,
        *,
        include_inactive: bool = False,
        analysis_job_id: Optional[UUID] = None,
    ) -> list[dict[str, Any]]: ...

    def create_dataset_version(
        self,
        project_id: UUID,
        *,
        approved_reference_records: Sequence[Mapping[str, Any]] = (),
        fixed_validation_analysis_job_ids: Optional[
            Sequence[UUID | str]
        ] = None,
    ) -> dict[str, Any]: ...


class AnalysisJobs(Protocol):
    def get(self, project_id: UUID, job_id: UUID) -> dict[str, Any]: ...

    def resolve_artifact(
        self,
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        kind: str,
    ) -> Any: ...


class ExperimentalProposals(Protocol):
    def latest_for_job(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        *,
        evaluation_id: Optional[str] = None,
    ) -> Mapping[str, Mapping[str, Any]]: ...


class GpxTracks(Protocol):
    def export_snapshot(
        self,
        project_id: UUID,
        trip_id: UUID,
    ) -> dict[str, Any]: ...


class ReportPreviewPreparer(Protocol):
    """Persist a validated draft/plan preview and return only safe metadata."""

    def prepare(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
        provider: str,
        template_id: str,
        template_sha256: str,
        template_semantic_map_id: str,
        template_semantic_map_sha256: str,
        approved_incidents: Sequence[Mapping[str, Any]],
        evidence_by_incident_id: Mapping[
            str, Mapping[str, Any]
        ],
        route_name: str,
        report_inspector_name: str,
        inspection_date: str,
        locations_by_incident_id: Mapping[str, str],
        report_policy: Mapping[str, Any],
        explicit_transmission_consent: bool,
        report_attempt: int,
    ) -> Mapping[str, Any]: ...


class ReportDeidentifiedEvidenceRegistry(Protocol):
    """Resolve only a human-approved report derivative for this exact fact."""

    def resolve_verified_descriptor(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
        incident_id: str,
        original_ref: str,
        original_sha256: str,
    ) -> Optional[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class DerivedOutputsRequest:
    project_id: UUID
    pipeline_job_id: UUID
    analysis_job_id: UUID
    source_sha256: str
    pipeline_input_fingerprint: str
    pipeline_profile_sha256: str
    review_completion_id: str
    answer_ledger_sha256: str
    question_queue_revision: str
    question_candidate_ids: tuple[str, ...]
    template_id: str
    template_sha256: str
    report_provider: str
    report_transmission_consent: bool
    template_semantic_map_ready: bool
    report_provider_ready: bool
    survey_incident_authority_sha256: Optional[str] = None
    survey_incident_authority_manifest: Optional[
        Mapping[str, Any]
    ] = None
    output_location_id: str = "project-default"
    report_attempt: int = 0
    completion_mode: str = "reviewed"
    template_semantic_map_id: str = "unconfigured"
    template_semantic_map_sha256: str = "0" * 64
    report_route_name: str = ""
    report_inspector_name: str = "unconfigured"
    report_inspection_date: str = ""
    report_policy: Mapping[str, Any] = field(
        default_factory=lambda: {
            "evidence_mode": "internal_original",
            "evaluation_ratings": [],
        }
    )
    verified_deidentified_evidence_by_incident_id: Mapping[
        str, Mapping[str, Any]
    ] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, label in (
            (self.project_id, "project_id"),
            (self.pipeline_job_id, "pipeline_job_id"),
            (self.analysis_job_id, "analysis_job_id"),
        ):
            if not isinstance(value, UUID):
                raise ValueError(f"{label} must be a UUID")
        for value, label in (
            (self.source_sha256, "source_sha256"),
            (self.pipeline_input_fingerprint, "pipeline_input_fingerprint"),
            (self.pipeline_profile_sha256, "pipeline_profile_sha256"),
            (self.answer_ledger_sha256, "answer_ledger_sha256"),
            (
                self.question_queue_revision,
                "question_queue_revision",
            ),
            (self.template_sha256, "template_sha256"),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{label} must be a lowercase SHA-256")
        if (
            len(self.question_candidate_ids)
            != len(set(self.question_candidate_ids))
            or any(
                not isinstance(item, str)
                or _CANDIDATE_ID.fullmatch(item) is None
                for item in self.question_candidate_ids
            )
        ):
            raise ValueError(
                "question_candidate_ids must be unique candidate identities"
            )
        if self.completion_mode == "reviewed":
            if (
                not self.question_candidate_ids
                or self.review_completion_id
                != f"review:{self.answer_ledger_sha256}"
            ):
                raise ValueError(
                    "reviewed completion must bind a non-empty answer ledger"
                )
        elif self.completion_mode == "zero_detections":
            empty_ledger_sha256 = hashlib.sha256(b"").hexdigest()
            if (
                self.question_candidate_ids
                or self.answer_ledger_sha256 != empty_ledger_sha256
                or self.review_completion_id
                != f"zero-detections:{empty_ledger_sha256}"
            ):
                raise ValueError(
                    "zero-detection completion must bind the empty ledger"
                )
        else:
            raise ValueError("completion_mode is invalid")
        for value, label in (
            (self.template_id, "template_id"),
            (self.output_location_id, "output_location_id"),
            (
                self.template_semantic_map_id,
                "template_semantic_map_id",
            ),
        ):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{label} is invalid")
        if (
            not isinstance(self.template_semantic_map_sha256, str)
            or _SHA256.fullmatch(
                self.template_semantic_map_sha256
            )
            is None
        ):
            raise ValueError(
                "template_semantic_map_sha256 is invalid"
            )
        if self.report_provider not in _REPORT_PROVIDERS:
            raise ValueError("report_provider is invalid")
        if (
            self.survey_incident_authority_sha256 is not None
            and (
                not isinstance(
                    self.survey_incident_authority_sha256, str
                )
                or _SHA256.fullmatch(
                    self.survey_incident_authority_sha256
                )
                is None
                or self.completion_mode != "reviewed"
            )
        ):
            raise ValueError(
                "survey incident authority is invalid"
            )
        if (
            self.survey_incident_authority_manifest is not None
            and (
                not isinstance(
                    self.survey_incident_authority_manifest,
                    Mapping,
                )
                or self.survey_incident_authority_sha256 is None
                or self.completion_mode != "reviewed"
            )
        ):
            raise ValueError(
                "survey incident authority manifest is invalid"
            )
        for value, label in (
            (
                self.report_transmission_consent,
                "report_transmission_consent",
            ),
            (
                self.template_semantic_map_ready,
                "template_semantic_map_ready",
            ),
            (self.report_provider_ready, "report_provider_ready"),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{label} must be boolean")
        if (
            isinstance(self.report_attempt, bool)
            or not isinstance(self.report_attempt, int)
            or not 0 <= self.report_attempt <= 64
        ):
            raise ValueError("report_attempt is invalid")
        if self.template_semantic_map_ready and (
            self.template_semantic_map_id == "unconfigured"
            or self.template_semantic_map_sha256 == "0" * 64
        ):
            raise ValueError(
                "ready template semantic map must have verified provenance"
            )
        if (
            not isinstance(self.report_route_name, str)
            or len(self.report_route_name) > 300
            or self.report_route_name != self.report_route_name.strip()
            or _CONTROL.search(self.report_route_name) is not None
            or re.match(
                r"^(?:[A-Za-z]:[\\/]|\\\\|file:)",
                self.report_route_name,
                flags=re.IGNORECASE,
            )
        ):
            raise ValueError("report_route_name is invalid")
        if (
            not isinstance(self.report_inspector_name, str)
            or not 1 <= len(self.report_inspector_name) <= 100
            or self.report_inspector_name
            != self.report_inspector_name.strip()
            or _CONTROL.search(self.report_inspector_name) is not None
            or re.match(
                r"^(?:[A-Za-z]:[\\/]|\\\\|file:)",
                self.report_inspector_name,
                flags=re.IGNORECASE,
            )
        ):
            raise ValueError("report_inspector_name is invalid")
        if not isinstance(self.report_inspection_date, str):
            raise ValueError("report_inspection_date is invalid")
        if self.report_inspection_date:
            try:
                parsed_date = date.fromisoformat(
                    self.report_inspection_date
                )
            except ValueError as error:
                raise ValueError(
                    "report_inspection_date is invalid"
                ) from error
            if parsed_date.isoformat() != self.report_inspection_date:
                raise ValueError("report_inspection_date is invalid")
        if (
            not isinstance(self.report_policy, Mapping)
            or set(self.report_policy)
            != {"evidence_mode", "evaluation_ratings"}
            or self.report_policy["evidence_mode"]
            not in {"internal_original", "shared_deidentified"}
        ):
            raise ValueError("report_policy is invalid")
        try:
            policy_bytes = _canonical_json(dict(self.report_policy))
        except (TypeError, ValueError) as error:
            raise ValueError("report_policy is invalid") from error
        if len(policy_bytes) > 256 * 1024:
            raise ValueError("report_policy is invalid")
        descriptors = (
            self.verified_deidentified_evidence_by_incident_id
        )
        if not isinstance(descriptors, Mapping):
            raise ValueError(
                "verified deidentified evidence is invalid"
            )
        deidentified_keys = {
            "deidentified_ref",
            "deidentified_sha256",
            "deidentification_record_sha256",
            "deidentified_verified",
        }
        for incident_id, descriptor in descriptors.items():
            try:
                canonical_incident_id = str(UUID(incident_id))
            except (TypeError, ValueError, AttributeError) as error:
                raise ValueError(
                    "verified deidentified evidence is invalid"
                ) from error
            if (
                canonical_incident_id != incident_id
                or not isinstance(descriptor, Mapping)
                or set(descriptor) != deidentified_keys
                or descriptor["deidentified_verified"] is not True
                or not isinstance(
                    descriptor["deidentified_ref"], str
                )
                or _IDENTIFIER.fullmatch(
                    descriptor["deidentified_ref"]
                )
                is None
                or any(
                    not isinstance(descriptor[key], str)
                    or _SHA256.fullmatch(descriptor[key]) is None
                    for key in (
                        "deidentified_sha256",
                        "deidentification_record_sha256",
                    )
                )
            ):
                raise ValueError(
                    "verified deidentified evidence is invalid"
                )


@dataclass(frozen=True)
class DerivedOutputsResult:
    checkpoint: Mapping[str, Any]
    metrics: Mapping[str, int]
    replayed: bool


@dataclass(frozen=True)
class ResolvedDerivedOutputsArtifactRoot:
    """Server-only exact PipelineJob/category-root authorization."""

    category_root: Path
    project_id: UUID
    pipeline_job_id: UUID
    analysis_job_id: UUID
    source_sha256: str
    output_location_id: str
    job_root_identity_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.category_root, Path)
            or not self.category_root.is_absolute()
            or not isinstance(self.project_id, UUID)
            or not isinstance(self.pipeline_job_id, UUID)
            or not isinstance(self.analysis_job_id, UUID)
            or not isinstance(self.source_sha256, str)
            or _SHA256.fullmatch(self.source_sha256) is None
            or not isinstance(self.output_location_id, str)
            or not 1 <= len(self.output_location_id) <= 128
            or _CONTROL.search(self.output_location_id) is not None
            or "/" in self.output_location_id
            or "\\" in self.output_location_id
            or not isinstance(self.job_root_identity_sha256, str)
            or _SHA256.fullmatch(
                self.job_root_identity_sha256
            )
            is None
        ):
            raise ValueError(
                "resolved derived-output artifact root is invalid"
            )


@dataclass(frozen=True)
class _SurveyIncidentEvidencePreflight:
    """Evidence and authority verified before any Incident is materialized."""

    image: Any
    clip: Any
    image_sha256: str
    clip_sha256: str
    output_authority: Mapping[str, Any]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _real_directory(path: Path, label: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DerivedOutputsError(f"{label} is unavailable") from error
    if path.is_symlink() or _is_reparse_point(path) or not path.is_dir():
        raise DerivedOutputsError(f"{label} must be a real directory")
    return resolved


def _authorized_existing_directory(path: Path, label: str) -> Path:
    try:
        requested = Path(path)
        if not requested.is_absolute():
            raise OSError
        resolved = requested.resolve(strict=True)
        if (
            os.path.normcase(os.path.abspath(str(requested)))
            != os.path.normcase(os.path.abspath(str(resolved)))
            or resolved.parent == resolved
            or requested.is_symlink()
            or _is_reparse_point(requested)
            or not resolved.is_dir()
        ):
            raise OSError
    except (OSError, TypeError, ValueError) as error:
        raise DerivedOutputsConflict(
            f"{label} is not an authorized real directory"
        ) from error
    return resolved


def _artifact_root_identity(path: Path) -> str:
    try:
        metadata = path.stat()
    except OSError as error:
        raise DerivedOutputsConflict(
            "derived-output artifact root is unavailable"
        ) from error
    return _sha256_value(
        {
            "category": "derived-outputs",
            "normalized_absolute_path": os.path.normcase(
                os.path.abspath(str(path))
            ),
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
        }
    )


def _write_atomic(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_DERIVED_RECORD_BYTES:
        raise DerivedOutputsError("derived-output record exceeds its bound")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _record_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(value)
    body.pop("record_sha256", None)
    return {**body, "record_sha256": _sha256_value(body)}


def _load_record(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise DerivedOutputsCorrupt(
            "persisted derived-output record is unavailable"
        ) from error
    if len(raw) > MAX_DERIVED_RECORD_BYTES:
        raise DerivedOutputsCorrupt(
            "persisted derived-output record exceeds its bound"
        )
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DerivedOutputsCorrupt(
            "persisted derived-output record is invalid"
        ) from error
    if not isinstance(value, dict):
        raise DerivedOutputsCorrupt(
            "persisted derived-output record is invalid"
        )
    expected = value.get("record_sha256")
    body = dict(value)
    body.pop("record_sha256", None)
    if (
        not isinstance(expected, str)
        or _SHA256.fullmatch(expected) is None
        or expected != _sha256_value(body)
    ):
        raise DerivedOutputsCorrupt(
            "persisted derived-output record failed integrity verification"
        )
    return value


def _write_hashed_record(
    path: Path,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    record = _record_payload(value)
    raw = _canonical_json(record)
    _write_atomic(path, raw)
    _write_atomic(
        path.with_suffix(path.suffix + ".sha256"),
        f"{hashlib.sha256(raw).hexdigest()}\n".encode("ascii"),
    )
    return record


def _load_hashed_record(path: Path) -> dict[str, Any]:
    record = _load_record(path)
    try:
        raw = path.read_bytes()
        sidecar_path = path.with_suffix(path.suffix + ".sha256")
        before = sidecar_path.lstat()
        sidecar = sidecar_path.read_text(encoding="ascii").strip()
        resolved_sidecar = sidecar_path.resolve(strict=True)
    except (OSError, UnicodeError) as error:
        raise DerivedOutputsCorrupt(
            "persisted derived-output artifact-root binding is unavailable"
        ) from error
    if (
        sidecar_path.is_symlink()
        or _is_reparse_point(sidecar_path)
        or not stat.S_ISREG(before.st_mode)
        or resolved_sidecar.parent != path.parent
        or _SHA256.fullmatch(sidecar or "") is None
        or hashlib.sha256(raw).hexdigest() != sidecar
    ):
        raise DerivedOutputsCorrupt(
            "persisted derived-output artifact-root binding changed"
        )
    return record


def _answer_ledger(
    latest: Sequence[Mapping[str, Any]],
    candidate_ids: Sequence[str],
) -> tuple[str, list[Mapping[str, Any]]]:
    latest_by_candidate = {
        str(item.get("candidate_id")): item for item in latest
    }
    selected = []
    proofs = []
    for candidate_id in candidate_ids:
        record = latest_by_candidate.get(candidate_id)
        if record is None:
            raise DerivedOutputsConflict(
                "the exact pipeline question ledger is incomplete"
            )
        record_id = record.get("record_id")
        record_hash = record.get("record_hash")
        revision = record.get("answer_revision")
        if (
            not isinstance(record_id, str)
            or not isinstance(record_hash, str)
            or _SHA256.fullmatch(record_hash) is None
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise DerivedOutputsConflict(
                "the exact pipeline answer proof is invalid"
            )
        proofs.append(
            "\0".join(
                (
                    candidate_id,
                    record_id.lower(),
                    record_hash,
                    str(revision),
                )
            )
        )
        selected.append(record)
    digest = hashlib.sha256(
        "\n".join(sorted(proofs)).encode("utf-8")
    ).hexdigest()
    return digest, selected


def _top_predictions(
    candidate: Mapping[str, Any],
    sidecar: Optional[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], str]:
    if sidecar is not None:
        predictions = sidecar.get("predictions")
        if isinstance(predictions, list):
            return predictions, "experimental_sidecar"
    inference = candidate.get("ai_inference")
    if isinstance(inference, Mapping) and inference.get("mode") == "local_model":
        predictions = inference.get("predictions")
        if isinstance(predictions, list):
            provider = inference.get("provider")
            return predictions, (
                f"local_model:{provider}"
                if isinstance(provider, str) and provider
                else "local_model"
            )
    return [], "scene_change_and_sharpness_non_classifying"


def _candidate_has_review_signal(
    candidate: Mapping[str, Any],
    sidecar: Optional[Mapping[str, Any]],
) -> bool:
    """Return whether an analysis seed contains an actual review proposal.

    The immutable legacy AnalysisJob always keeps at least its initial
    scene/sharpness seed, even when the later full 2 fps provider scan returns
    zero road detections.  That seed is evidence extraction input, not a raw
    detection.  Zero-detection export may therefore ignore it only when no
    detector prediction, OCR observation, dense overlay, or structured
    sidecar proposal is attached.
    """

    predictions, _source = _top_predictions(candidate, sidecar)
    if predictions:
        return True
    if candidate.get("candidate_kind") in {
        "dense_candidate_evidence_overlay",
        "dino_temporal_review_evidence_overlay",
    }:
        return True
    inference = candidate.get("ai_inference")
    if not isinstance(inference, Mapping):
        return False
    ocr_observations = inference.get("ocr_observations")
    return isinstance(ocr_observations, list) and bool(ocr_observations)


def _finite_number(value: Any) -> Optional[float]:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def _raw_detection_rows(
    candidates: Sequence[Mapping[str, Any]],
    proposals: Mapping[str, Mapping[str, Any]],
    *,
    trip_id: str,
    source_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    rows: list[dict[str, Any]] = []
    ids_by_candidate: dict[str, list[str]] = {}
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id", ""))
        timestamp = _finite_number(
            candidate.get("representative_timestamp_seconds")
        )
        if (
            _CANDIDATE_ID.fullmatch(candidate_id) is None
            or timestamp is None
            or timestamp < 0
        ):
            raise DerivedOutputsConflict(
                "the completed analysis candidate is invalid"
            )
        predictions, source = _top_predictions(
            candidate, proposals.get(candidate_id)
        )
        candidate_kind = candidate.get("candidate_kind")
        if not predictions and candidate_kind in {
            "dense_candidate_evidence_overlay",
            "dino_temporal_review_evidence_overlay",
        }:
            issue_type = candidate.get("issue_type")
            score = _finite_number(candidate.get("score"))
            proposal_sources = candidate.get("proposal_sources")
            if (
                not isinstance(issue_type, str)
                or not issue_type
                or score is None
                or not 0 <= score <= 1
                or not isinstance(proposal_sources, list)
                or not proposal_sources
            ):
                raise DerivedOutputsConflict(
                    "the overlay candidate proposal is invalid"
                )
            predictions = [
                {
                    "issue_type": issue_type,
                    "model_score": score,
                    "box": candidate.get("normalized_box"),
                }
            ]
            source = (
                "dino_temporal_review_evidence"
                if candidate_kind
                == "dino_temporal_review_evidence_overlay"
                else "dense_2fps_candidate_evidence"
            )
        candidate_rows = []
        for index, prediction in enumerate(predictions):
            if not isinstance(prediction, Mapping):
                raise DerivedOutputsConflict(
                    "the model proposal is invalid"
                )
            issue_type = prediction.get("issue_type")
            if not isinstance(issue_type, str) or not issue_type:
                raise DerivedOutputsConflict(
                    "the model proposal issue type is invalid"
                )
            detection_id = f"{candidate_id}-proposal-{index + 1}"
            confidence = prediction.get(
                "confidence",
                prediction.get(
                    "relative_score", prediction.get("model_score", "")
                ),
            )
            candidate_rows.append(
                {
                    "detection_id": detection_id,
                    "trip_id": trip_id,
                    "video_id": source_sha256,
                    "timestamp_ms": round(timestamp * 1000),
                    "type": issue_type,
                    "confidence": confidence if confidence is not None else "",
                    "score_value": (
                        confidence if confidence is not None else ""
                    ),
                    "score_semantics": (
                        "model_score_not_probability"
                    ),
                    "proposal_origin": (
                        "experimental_sidecar"
                        if source == "experimental_sidecar"
                        else (
                            "dino_temporal_review_evidence"
                            if source
                            == "dino_temporal_review_evidence"
                            else (
                                "dense_candidate_evidence"
                                if source
                                == "dense_2fps_candidate_evidence"
                                else "embedded_analysis_inference"
                            )
                        )
                    ),
                    "review_only": True,
                    "automatic_labeling_allowed": False,
                    "severity": "",
                    "bbox": prediction.get("box") or "",
                    "mask_ref": "",
                    "latitude": "",
                    "longitude": "",
                    "source": source,
                }
            )
        if not candidate_rows:
            candidate_rows.append(
                {
                    "detection_id": candidate_id,
                    "trip_id": trip_id,
                    "video_id": source_sha256,
                    "timestamp_ms": round(timestamp * 1000),
                    "type": "UNCLASSIFIED_SCENE_CANDIDATE",
                    "confidence": "",
                    "score_value": "",
                    "score_semantics": "",
                    "proposal_origin": "",
                    "review_only": "",
                    "automatic_labeling_allowed": "",
                    "severity": "",
                    "bbox": "",
                    "mask_ref": "",
                    "latitude": "",
                    "longitude": "",
                    "source": source,
                }
            )
        rows.extend(candidate_rows)
        ids_by_candidate[candidate_id] = [
            str(item["detection_id"]) for item in candidate_rows
        ]
    return rows, ids_by_candidate


def _latest_answer_rows(
    request: DerivedOutputsRequest,
    records: Sequence[Mapping[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]],
    proposals: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    question_rows = []
    annotation_rows = []
    for record in records:
        candidate_id = str(record["candidate_id"])
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise DerivedOutputsConflict(
                "a reviewed question is not in the completed analysis"
            )
        answer = record.get("final_answer")
        if not isinstance(answer, Mapping):
            raise DerivedOutputsConflict(
                "a reviewed answer is invalid"
            )
        answered_by = answer.get("answered_by")
        label_source = _LABEL_SOURCE_BY_REVIEW_ACTOR.get(answered_by)
        if label_source is None:
            raise DerivedOutputsConflict(
                "a reviewed answer has an unsupported provenance"
            )
        predictions, source = _top_predictions(
            candidate, proposals.get(candidate_id)
        )
        top = predictions[0] if predictions else {}
        proposed_type = (
            top.get("issue_type")
            if isinstance(top, Mapping)
            else None
        )
        question_rows.append(
            {
                "question_id": record["question_id"],
                "project_id": str(request.project_id),
                "trip_id": str(request.analysis_job_id),
                "candidate_incident_id": candidate_id,
                # The extraction result has no canonical prompt text. Keep
                # the field empty instead of inventing a user-facing question.
                "prompt": "",
                "policy": "pipeline_review",
                "proposed_type": proposed_type or "",
                "proposed_confidence": (
                    top.get("confidence", top.get("model_score", ""))
                    if isinstance(top, Mapping)
                    else ""
                ),
                "proposal_origin": (
                    "experimental_sidecar"
                    if source == "experimental_sidecar"
                    else (
                        "embedded_analysis_inference"
                        if predictions
                        and source.startswith("local_model")
                        else ""
                    )
                ),
                "review_only": True,
                "automatic_labeling_allowed": False,
                "proposed_geometry": (
                    top.get("box") or ""
                    if isinstance(top, Mapping)
                    else ""
                ),
                "evidence_image_ref": candidate.get(
                    "representative_image", {}
                ).get("artifact_ref", ""),
                "preview_clip_ref": candidate.get(
                    "preview_clip", {}
                ).get("artifact_ref", ""),
                "mandatory": True,
                "created_at": record["recorded_at"],
            }
        )
        annotation_rows.append(
            {
                "annotation_id": record["record_id"],
                "question_id": record["question_id"],
                "project_id": str(request.project_id),
                "trip_id": str(request.analysis_job_id),
                "candidate_incident_id": candidate_id,
                "answer": answer.get("answer", ""),
                "type": answer.get("selected_issue_type") or "",
                "severity": answer.get("severity") or "",
                "geometry": answer.get("normalized_box") or "",
                "ocr_text": answer.get("ocr_text") or "",
                "action_label": answer.get("maintenance_action") or "",
                "review_status": record.get("partition", ""),
                "label_source": label_source,
                "created_at": record["recorded_at"],
                "dataset_split": "",
            }
        )
    return question_rows, annotation_rows


def _report_blocking_reasons(
    request: DerivedOutputsRequest,
    *,
    approved_incident_count: int,
    report_preparer: Optional[ReportPreviewPreparer],
) -> tuple[str, ...]:
    if approved_incident_count == 0:
        return ()
    reasons = []
    shared_provider_report = (
        request.report_policy.get("evidence_mode")
        == "shared_deidentified"
    )
    if (
        shared_provider_report
        and request.report_transmission_consent is not True
    ):
        reasons.append("report_transmission_consent_required")
    if request.template_semantic_map_ready is not True:
        reasons.append("template_semantic_map_unavailable")
    if not request.report_route_name:
        reasons.append("report_route_name_required")
    if request.report_inspector_name.casefold() == "unconfigured":
        reasons.append("report_inspector_name_required")
    if not request.report_inspection_date:
        reasons.append("report_inspection_date_required")
    if shared_provider_report and (
        request.report_provider not in {"codex", "claude"}
        or request.report_provider_ready is not True
    ):
        reasons.append("report_provider_unavailable")
    if report_preparer is None:
        reasons.append("report_preview_preparer_unavailable")
    return tuple(reasons)


def _verified_report_image_sha256(image: Any) -> str:
    """Verify the still image before it can enter report provenance.

    Artifact export verifies it again while streaming the evidence bundle.
    This earlier check prevents a Provider/binder call from racing ahead of a
    stale or forged resolved-artifact record.
    """

    try:
        path = Path(image.path)
        expected_size = image.size_bytes
        expected_sha256 = image.sha256
        media_type = image.media_type
        before = path.lstat()
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise DerivedOutputsError(
            "approved incident image evidence is unavailable"
        ) from error
    if (
        not path.is_absolute()
        or path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(before.st_mode)
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size <= 0
        or before.st_size != expected_size
        or media_type != "image/jpeg"
        or not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        raise DerivedOutputsError(
            "approved incident image evidence failed integrity verification"
        )
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            header = source.read(3)
            digest = hashlib.sha256()
            digest.update(header)
            size = len(header)
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            opened_after = os.fstat(source.fileno())
        after = path.lstat()
    except OSError as error:
        raise DerivedOutputsError(
            "approved incident image evidence is unavailable"
        ) from error
    if (
        header != b"\xff\xd8\xff"
        or getattr(opened, "st_dev", None)
        != getattr(before, "st_dev", None)
        or getattr(opened, "st_ino", None)
        != getattr(before, "st_ino", None)
        or getattr(opened_after, "st_dev", None)
        != getattr(opened, "st_dev", None)
        or getattr(opened_after, "st_ino", None)
        != getattr(opened, "st_ino", None)
        or getattr(after, "st_dev", None)
        != getattr(before, "st_dev", None)
        or getattr(after, "st_ino", None)
        != getattr(before, "st_ino", None)
        or opened.st_size != before.st_size
        or opened_after.st_size != expected_size
        or after.st_size != expected_size
        or size != expected_size
        or path.is_symlink()
        or _is_reparse_point(path)
        or digest.hexdigest() != expected_sha256
    ):
        raise DerivedOutputsError(
            "approved incident image evidence failed integrity verification"
        )
    return expected_sha256


def _verified_candidate_artifact_sha256(
    resolved: Any,
    descriptor: Any,
    *,
    expected_media_type: str,
    label: str,
) -> str:
    """Verify resolved evidence against immutable candidate metadata."""

    if not isinstance(descriptor, Mapping):
        raise DerivedOutputsConflict(
            f"the approved Incident {label} metadata is invalid"
        )
    try:
        path = Path(resolved.path)
        media_type = resolved.media_type
        size_bytes = resolved.size_bytes
        sha256 = resolved.sha256
        before = path.lstat()
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise DerivedOutputsError(
            f"approved Incident {label} is unavailable"
        ) from error
    if (
        not path.is_absolute()
        or path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(before.st_mode)
        or media_type != expected_media_type
        or descriptor.get("media_type") != expected_media_type
        or descriptor.get("size_bytes") != size_bytes
        or descriptor.get("sha256") != sha256
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 1
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
    ):
        raise DerivedOutputsConflict(
            f"approved Incident {label} provenance changed"
        )
    try:
        actual_size = path.stat().st_size
        actual_sha256 = _sha256_file(path)
        after = path.lstat()
    except OSError as error:
        raise DerivedOutputsError(
            f"approved Incident {label} could not be verified"
        ) from error
    if (
        actual_size != size_bytes
        or actual_sha256 != sha256
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise DerivedOutputsConflict(
            f"approved Incident {label} failed integrity verification"
        )
    return actual_sha256


def _evidence_descriptor(
    request: DerivedOutputsRequest,
    *,
    incident_id: str,
    candidate_id: str,
    original_sha256: str,
    verified_deidentified: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    original_ref = f"analysis:{candidate_id}:representative"
    deidentified = verified_deidentified
    if deidentified is None:
        deidentified = (
            request.verified_deidentified_evidence_by_incident_id.get(
                incident_id
            )
        )
    if deidentified is None:
        deidentified_fields: dict[str, Any] = {
            "deidentified_ref": None,
            "deidentified_sha256": None,
            "deidentification_record_sha256": None,
            "deidentified_verified": False,
        }
    else:
        expected_keys = {
            "deidentified_ref",
            "deidentified_sha256",
            "deidentification_record_sha256",
            "deidentified_verified",
        }
        if (
            not isinstance(deidentified, Mapping)
            or set(deidentified) != expected_keys
            or deidentified.get("deidentified_verified") is not True
            or not isinstance(deidentified.get("deidentified_ref"), str)
            or _IDENTIFIER.fullmatch(
                str(deidentified.get("deidentified_ref"))
            )
            is None
            or any(
                not isinstance(deidentified.get(key), str)
                or _SHA256.fullmatch(str(deidentified.get(key))) is None
                for key in (
                    "deidentified_sha256",
                    "deidentification_record_sha256",
                )
            )
        ):
            raise DerivedOutputsConflict(
                "verified report evidence descriptor is invalid"
            )
        deidentified_fields = dict(deidentified)
    return {
        "incident_id": incident_id,
        "project_id": str(request.project_id),
        "pipeline_job_id": str(request.pipeline_job_id),
        "analysis_job_id": str(request.analysis_job_id),
        "source_sha256": request.source_sha256,
        "original_ref": original_ref,
        "original_sha256": original_sha256,
        **deidentified_fields,
    }


def _format_video_time(time_ms: int) -> str:
    hours, remainder = divmod(time_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}."
        f"{milliseconds:03d}"
    )


def _report_locations(
    *,
    incident_times_ms: Mapping[str, int],
    route_points: Sequence[Mapping[str, Any]],
    gpx_provenance: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, tuple[float, float]]]:
    """Bind each Incident only to a verified, in-video timed GPX point."""

    verified_timed_gpx = (
        gpx_provenance.get("status") == "attached"
        and gpx_provenance.get("source_integrity_verified") is True
        and gpx_provenance.get("timestamp_basis")
        == "gpx_elapsed_seconds_plus_video_offset"
    )
    duration_seconds = gpx_provenance.get("video_duration_seconds")
    if (
        not verified_timed_gpx
        or isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(float(duration_seconds))
        or float(duration_seconds) < 0
    ):
        eligible_points: list[tuple[int, int, float, float]] = []
    else:
        maximum_ms = int(round(float(duration_seconds) * 1_000))
        eligible_points = []
        for fallback_sequence, point in enumerate(route_points):
            if not isinstance(point, Mapping):
                continue
            timestamp_ms = point.get("timestamp_ms")
            latitude = point.get("latitude")
            longitude = point.get("longitude")
            sequence = point.get("sequence", fallback_sequence)
            if (
                isinstance(timestamp_ms, bool)
                or not isinstance(timestamp_ms, int)
                or not 0 <= timestamp_ms <= maximum_ms
                or isinstance(latitude, bool)
                or not isinstance(latitude, (int, float))
                or not math.isfinite(float(latitude))
                or not -90 <= float(latitude) <= 90
                or isinstance(longitude, bool)
                or not isinstance(longitude, (int, float))
                or not math.isfinite(float(longitude))
                or not -180 <= float(longitude) <= 180
                or isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or point.get("source") != "gpx"
            ):
                continue
            eligible_points.append(
                (
                    timestamp_ms,
                    sequence,
                    float(latitude),
                    float(longitude),
                )
            )
        eligible_points.sort(key=lambda item: (item[0], item[1]))

    locations: dict[str, str] = {}
    coordinates: dict[str, tuple[float, float]] = {}
    for incident_id, time_ms in incident_times_ms.items():
        video_time = _format_video_time(time_ms)
        if not eligible_points:
            locations[incident_id] = (
                f"영상 {video_time} · GPS 미연결"
            )
            continue
        nearest = min(
            eligible_points,
            key=lambda item: (
                abs(item[0] - time_ms),
                item[0],
                item[1],
            ),
        )
        latitude, longitude = nearest[2], nearest[3]
        locations[incident_id] = (
            f"영상 {video_time} · GPS "
            f"{latitude:.7f}, {longitude:.7f}"
        )
        coordinates[incident_id] = (latitude, longitude)
    return locations, coordinates


def _safe_report_preview(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReportPreviewUnavailable(
            "report preview metadata is invalid"
        )
    legacy_keys = {
        "preview_id",
        "plan_sha256",
        "draft_sha256",
        "operation_count",
    }
    durable_keys = {
        "preview_id",
        "state",
        "input_sha256",
        "report_attempt",
        "plan_sha256",
        "semantic_provider_plan_sha256",
        "draft_sha256",
        "operation_count",
        "current_binding_manifest_sha256",
        "binding",
        "provider_called",
        "exact_writer_required",
        "exact_writer_completed",
        "render_validation_completed",
        "com_invoked",
        "document_written",
        "apply_eligible",
    }
    keys = set(value)
    if keys != legacy_keys and keys != durable_keys:
        raise ReportPreviewUnavailable(
            "report preview metadata is invalid"
        )
    preview_id = value["preview_id"]
    plan_sha256 = value["plan_sha256"]
    draft_sha256 = value["draft_sha256"]
    operation_count = value["operation_count"]
    if (
        not isinstance(preview_id, str)
        or _IDENTIFIER.fullmatch(preview_id) is None
        or not isinstance(plan_sha256, str)
        or _SHA256.fullmatch(plan_sha256) is None
        or not isinstance(draft_sha256, str)
        or _SHA256.fullmatch(draft_sha256) is None
        or isinstance(operation_count, bool)
        or not isinstance(operation_count, int)
        or not 1 <= operation_count <= 5_000
    ):
        raise ReportPreviewUnavailable(
            "report preview metadata is invalid"
        )
    provider_called = True
    if keys == durable_keys:
        binding = value["binding"]
        input_sha_valid = (
            isinstance(value["input_sha256"], str)
            and _SHA256.fullmatch(value["input_sha256"]) is not None
        )
        binding_keys = {
            "state",
            "template_id",
            "document_snapshot_id",
            "approved_incident_count",
            "survey_incident_authority_sha256",
            "approved_incident_authority_set_sha256",
            "required_writer_operation_count",
            "exact_hwpx_writer_operation_limit",
            "approved_incident_bound",
            "content_provenance_bound",
            "writer_required",
        }
        if (
            value["state"]
            != "semantic_preview_ready_exact_writer_required"
            or not input_sha_valid
            or not isinstance(value["report_attempt"], int)
            or isinstance(value["report_attempt"], bool)
            or not 0 <= value["report_attempt"] <= 64
            or value["semantic_provider_plan_sha256"]
            != plan_sha256
            or not isinstance(
                value["current_binding_manifest_sha256"], str
            )
            or _SHA256.fullmatch(
                value["current_binding_manifest_sha256"]
            )
            is None
            or not isinstance(binding, Mapping)
            or set(binding) != binding_keys
            or binding["state"]
            != "current_incidents_bound_writer_required"
            or not isinstance(binding["approved_incident_count"], int)
            or isinstance(binding["approved_incident_count"], bool)
            or not 1 <= binding["approved_incident_count"] <= 500
            or not isinstance(
                binding["survey_incident_authority_sha256"], str
            )
            or _SHA256.fullmatch(
                binding["survey_incident_authority_sha256"]
            )
            is None
            or not isinstance(
                binding[
                    "approved_incident_authority_set_sha256"
                ],
                str,
            )
            or _SHA256.fullmatch(
                binding[
                    "approved_incident_authority_set_sha256"
                ]
            )
            is None
            or not isinstance(
                binding["required_writer_operation_count"], int
            )
            or isinstance(
                binding["required_writer_operation_count"], bool
            )
            or binding["required_writer_operation_count"]
            != operation_count
            or binding["exact_hwpx_writer_operation_limit"] != 5_000
            or binding["approved_incident_bound"] is not True
            or binding["content_provenance_bound"] is not True
            or binding["writer_required"] is not True
            or not isinstance(binding["template_id"], str)
            or _IDENTIFIER.fullmatch(binding["template_id"]) is None
            or not isinstance(binding["document_snapshot_id"], str)
            or _IDENTIFIER.fullmatch(
                binding["document_snapshot_id"]
            )
            is None
            or not isinstance(value["provider_called"], bool)
            or value["exact_writer_required"] is not True
            or value["exact_writer_completed"] is not False
            or value["render_validation_completed"] is not False
            or value["com_invoked"] is not False
            or value["document_written"] is not False
            or value["apply_eligible"] is not False
        ):
            raise ReportPreviewUnavailable(
                "report preview metadata is invalid"
            )
        provider_called = value["provider_called"]
    return {
        "state": "validated_preview",
        "preview_id": preview_id,
        "plan_sha256": plan_sha256,
        "draft_sha256": draft_sha256,
        "operation_count": operation_count,
        "blocking_reasons": [],
        "provider_called": provider_called,
        "document_edit_plan_prepared": True,
        "com_invoked": False,
    }


def _assert_checkpoint_safe(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise DerivedOutputsError("derived checkpoint is too deeply nested")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DerivedOutputsError(
                "derived checkpoint contains a non-finite value"
            )
        return
    if isinstance(value, str):
        if (
            "\x00" in value
            or "\\" in value
            or "/" in value
            or re.match(r"^[A-Za-z]:", value)
            or value.startswith("~")
        ):
            raise DerivedOutputsError(
                "derived checkpoint contains a filesystem path"
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or key.casefold() in {
                    "path",
                    "source_path",
                    "output_path",
                    "template_path",
                }
                or key.casefold().endswith("_path")
            ):
                raise DerivedOutputsError(
                    "derived checkpoint contains an unsafe field"
                )
            _assert_checkpoint_safe(item, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_checkpoint_safe(item, depth=depth + 1)
        return
    raise DerivedOutputsError(
        "derived checkpoint contains an unsupported value"
    )


class DerivedOutputsService:
    """Prepare one idempotent, project/job-scoped reviewed output bundle."""

    def __init__(
        self,
        *,
        data_root: Path,
        candidate_labels: CandidateLabels,
        store: ProjectProjectionStore,
        analysis_jobs: AnalysisJobs,
        experimental_proposals: Optional[ExperimentalProposals] = None,
        candidate_evidence: Optional[Any] = None,
        gpx_tracks: Optional[GpxTracks] = None,
        report_preparer: Optional[ReportPreviewPreparer] = None,
        report_deidentified_evidence: Optional[
            ReportDeidentifiedEvidenceRegistry
        ] = None,
        approved_reference_resolver: Optional[
            Callable[[UUID], Sequence[Mapping[str, Any]]]
        ] = None,
        fixed_validation_analysis_jobs_resolver: Optional[
            Callable[[UUID], Sequence[UUID | str]]
        ] = None,
        export_writer: Callable[..., ArtifactExportResult] = export_artifacts,
    ) -> None:
        root = Path(data_root)
        if not root.is_absolute():
            raise ValueError("derived-output data_root must be absolute")
        self._data_root = _real_directory(root, "derived-output data root")
        self._record_root = _real_directory(
            self._data_root / "derived-outputs",
            "derived-output record root",
        )
        self._export_root = _real_directory(
            self._data_root / "exports",
            "derived-output export root",
        )
        self._candidate_labels = candidate_labels
        self._store = store
        self._analysis_jobs = analysis_jobs
        self._experimental_proposals = experimental_proposals
        self._candidate_evidence = candidate_evidence
        self._gpx_tracks = gpx_tracks
        self._report_preparer = report_preparer
        self._report_deidentified_evidence = (
            report_deidentified_evidence
        )
        self._approved_reference_resolver = (
            approved_reference_resolver
        )
        self._fixed_validation_analysis_jobs_resolver = (
            fixed_validation_analysis_jobs_resolver
        )
        self._export_writer = export_writer
        self._lock = threading.RLock()

    def _resolve_export_storage(
        self,
        request: DerivedOutputsRequest,
        artifact_root: Optional[
            ResolvedDerivedOutputsArtifactRoot
        ],
    ) -> tuple[Path, Optional[dict[str, Any]]]:
        if artifact_root is None:
            if request.output_location_id != "project-default":
                raise DerivedOutputsConflict(
                    "the configured output location has no server-owned "
                    "resolver for the exact job binding"
                )
            return self._export_root, None
        if not isinstance(
            artifact_root, ResolvedDerivedOutputsArtifactRoot
        ):
            raise DerivedOutputsConflict(
                "derived-output artifact root authorization is invalid"
            )
        if (
            artifact_root.project_id != request.project_id
            or artifact_root.pipeline_job_id
            != request.pipeline_job_id
            or artifact_root.analysis_job_id
            != request.analysis_job_id
            or artifact_root.source_sha256 != request.source_sha256
            or artifact_root.output_location_id
            != request.output_location_id
        ):
            raise DerivedOutputsConflict(
                "derived-output artifact root does not match the "
                "exact PipelineJob binding"
            )
        root = _authorized_existing_directory(
            artifact_root.category_root,
            "derived-output artifact root",
        )
        root_identity = _artifact_root_identity(root)
        material = {
            "schema_version": DERIVED_ARTIFACT_ROOT_BINDING_SCHEMA,
            "category": "derived-outputs",
            "project_id": str(request.project_id),
            "pipeline_job_id": str(request.pipeline_job_id),
            "analysis_job_id": str(request.analysis_job_id),
            "source_sha256": request.source_sha256,
            "output_location_id": request.output_location_id,
            "job_root_identity_sha256": (
                artifact_root.job_root_identity_sha256
            ),
            "category_root_identity_sha256": root_identity,
            "absolute_paths_exposed": False,
        }
        expected = _record_payload(material)
        binding_path = root / _DERIVED_ARTIFACT_ROOT_BINDING_FILE
        if binding_path.exists():
            try:
                before = binding_path.lstat()
                resolved_binding = binding_path.resolve(strict=True)
            except OSError as error:
                raise DerivedOutputsCorrupt(
                    "persisted derived-output artifact-root binding "
                    "is unavailable"
                ) from error
            if (
                binding_path.is_symlink()
                or _is_reparse_point(binding_path)
                or not stat.S_ISREG(before.st_mode)
                or resolved_binding.parent != root
            ):
                raise DerivedOutputsCorrupt(
                    "persisted derived-output artifact-root binding "
                    "is unsafe"
                )
            persisted = _load_hashed_record(binding_path)
            if persisted != expected:
                raise DerivedOutputsConflict(
                    "derived-output artifact root is bound to another job"
                )
        else:
            persisted = _write_hashed_record(
                binding_path, material
            )
            if persisted != expected:
                raise DerivedOutputsCorrupt(
                    "derived-output artifact-root binding is invalid"
                )
        storage = {
            "schema_version": (
                DERIVED_ARTIFACT_ROOT_BINDING_SCHEMA
            ),
            "mode": "external_category_root",
            "category": "derived-outputs",
            "project_id": str(request.project_id),
            "pipeline_job_id": str(request.pipeline_job_id),
            "analysis_job_id": str(request.analysis_job_id),
            "source_sha256": request.source_sha256,
            "output_location_id": request.output_location_id,
            "job_root_identity_sha256": (
                artifact_root.job_root_identity_sha256
            ),
            "category_root_identity_sha256": root_identity,
            "binding_record_sha256": persisted["record_sha256"],
            "absolute_paths_exposed": False,
        }
        return root, storage

    def _dataset_snapshot(
        self,
        project_id: UUID,
    ) -> dict[str, Any]:
        if self._approved_reference_resolver is None:
            raise DatasetSnapshotFailed(
                "approved reference resolver is unavailable"
            )
        try:
            references = self._approved_reference_resolver(project_id)
            if (
                isinstance(references, (str, bytes, bytearray))
                or not isinstance(references, Sequence)
            ):
                raise DatasetVersionError(
                    "approved references must be a sequence"
                )
            dataset_arguments: dict[str, Any] = {
                "approved_reference_records": tuple(references),
            }
            if (
                self._fixed_validation_analysis_jobs_resolver
                is not None
            ):
                fixed_jobs = (
                    self._fixed_validation_analysis_jobs_resolver(
                        project_id
                    )
                )
                if (
                    isinstance(
                        fixed_jobs, (str, bytes, bytearray)
                    )
                    or not isinstance(fixed_jobs, Sequence)
                ):
                    raise DatasetVersionError(
                        "fixed validation resolver returned invalid data"
                    )
                if fixed_jobs:
                    dataset_arguments[
                        "fixed_validation_analysis_job_ids"
                    ] = tuple(fixed_jobs)
            dataset = self._store.create_dataset_version(
                project_id,
                **dataset_arguments,
            )
        except (
            DatasetVersionError,
            LabelProjectionError,
            HTTPException,
        ) as error:
            raise DatasetSnapshotFailed(
                "reviewed DatasetVersion could not be prepared"
            ) from error
        except DatasetSnapshotFailed:
            raise
        except Exception as error:
            raise DatasetSnapshotFailed(
                "reviewed DatasetVersion could not be prepared"
            ) from error
        manifest = dataset.get("manifest")
        members = dataset.get("approved_reference_members")
        dataset_version_id = str(
            dataset.get("dataset_version_id", "")
        )
        dataset_hash = dataset.get("dataset_hash")
        project_text = str(project_id)
        manifest_counts = (
            manifest.get("counts")
            if isinstance(manifest, Mapping)
            else None
        )
        reference_members_sha256 = _sha256_value(members)
        training_member_count = dataset.get("training_member_count")
        train_member_count = dataset.get("train_count")
        training_gate_member_count = dataset.get(
            "training_gate_member_count"
        )
        reference_training_event_ids = (
            {
                member.get("sample_evidence_group_id")
                for member in members
                if isinstance(member, Mapping)
                and isinstance(
                    member.get("sample_evidence_group_id"), str
                )
                and member.get("sample_evidence_group_id")
            }
            if isinstance(members, list)
            else set()
        )
        approved_reference_training_event_count = dataset.get(
            "approved_reference_training_event_count"
        )
        if (
            not isinstance(manifest, Mapping)
            or not isinstance(members, list)
            or len(reference_training_event_ids) == 0
            and len(members) > 0
            or any(
                not isinstance(member, Mapping)
                or not isinstance(
                    member.get("sample_evidence_group_id"), str
                )
                or not member.get("sample_evidence_group_id")
                for member in members
            )
            or dataset.get("project_id") != project_text
            or dataset.get("schema_version")
            != "trafficvisson.dataset-version.v1"
            or manifest.get("schema_version")
            != "trafficvisson.dataset-version.v1"
            or manifest.get("project_id") != project_text
            or manifest.get("dataset_id") != dataset_version_id
            or manifest.get("dataset_hash") != dataset_hash
            or manifest.get("approved_reference_members") != members
            or manifest.get("approved_reference_members_sha256")
            != reference_members_sha256
            or manifest.get(
                "approved_references_count_toward_training_gate"
            )
            is not False
            or manifest.get(
                "approved_references_count_toward_validation"
            )
            is not False
            or dataset.get("approved_reference_member_count")
            != len(members)
            or dataset.get(
                "approved_references_count_toward_training_gate"
            )
            is not False
            or dataset.get(
                "approved_references_count_toward_validation"
            )
            is not False
            or dataset.get(
                "approved_reference_members_sha256"
            )
            != reference_members_sha256
            or isinstance(training_member_count, bool)
            or not isinstance(training_member_count, int)
            or training_member_count < 0
            or isinstance(train_member_count, bool)
            or not isinstance(train_member_count, int)
            or train_member_count < 0
            or isinstance(training_gate_member_count, bool)
            or not isinstance(training_gate_member_count, int)
            or isinstance(
                approved_reference_training_event_count, bool
            )
            or not isinstance(
                approved_reference_training_event_count, int
            )
            or approved_reference_training_event_count
            != len(reference_training_event_ids)
            or training_gate_member_count
            != train_member_count
            or not isinstance(manifest_counts, Mapping)
            or manifest_counts.get(
                "approved_reference_training_members"
            )
            != len(members)
            or manifest_counts.get(
                "approved_reference_training_events"
            )
            != approved_reference_training_event_count
            or manifest_counts.get("training_members")
            != training_member_count
            or manifest_counts.get("train") != train_member_count
            or manifest_counts.get("training_gate_members")
            != training_gate_member_count
        ):
            raise DatasetSnapshotFailed(
                "reviewed DatasetVersion failed provenance validation"
            )
        provenance = {
            "state": "prepared",
            "dataset_version_id": str(
                dataset_version_id
            ),
            "dataset_hash": dataset_hash,
            "dataset_manifest_sha256": _sha256_value(manifest),
            "approved_reference_members_sha256": dataset.get(
                "approved_reference_members_sha256"
            ),
            "approved_reference_member_count": len(members),
            "approved_reference_training_event_count": (
                approved_reference_training_event_count
            ),
            "current_training_member_count": dataset.get(
                "training_member_count"
            ),
            "training_gate_member_count": dataset.get(
                "training_gate_member_count"
            ),
            "approved_references_count_toward_training_gate": False,
            "approved_references_count_toward_validation": False,
            "fixed_validation_source": (
                "current_video_project_fixed_only"
            ),
            "automatic_training_started": False,
        }
        if (
            not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                provenance["dataset_version_id"],
            )
            or not isinstance(provenance["dataset_hash"], str)
            or _SHA256.fullmatch(provenance["dataset_hash"]) is None
            or not isinstance(
                provenance["approved_reference_members_sha256"],
                str,
            )
            or _SHA256.fullmatch(
                provenance["approved_reference_members_sha256"]
            )
            is None
            or isinstance(
                provenance["training_gate_member_count"], bool
            )
            or not isinstance(
                provenance["training_gate_member_count"], int
            )
            or isinstance(
                provenance[
                    "approved_reference_training_event_count"
                ],
                bool,
            )
            or not isinstance(
                provenance[
                    "approved_reference_training_event_count"
                ],
                int,
            )
        ):
            raise DatasetSnapshotFailed(
                "reviewed DatasetVersion failed provenance validation"
            )
        return provenance

    def _survey_authority_preflight(
        self,
        request: DerivedOutputsRequest,
        *,
        reviewed_records: Sequence[Mapping[str, Any]],
        candidates: Mapping[str, Mapping[str, Any]],
    ) -> tuple[
        Optional[dict[str, Any]],
        dict[str, _SurveyIncidentEvidencePreflight],
    ]:
        """Verify final-survey authority before an Incident write can occur."""

        digest = request.survey_incident_authority_sha256
        raw_manifest = request.survey_incident_authority_manifest
        if digest is None and raw_manifest is None:
            return None, {}
        if digest is None or raw_manifest is None:
            # Legacy digest-only records remain auditable in their PipelineJob,
            # but no longer have enough meaning to authorize final output.
            raise DerivedOutputsConflict(
                "complete promoted-survey Incident authority is required"
            )
        try:
            survey_authority = validate_survey_incident_authority(
                raw_manifest,
                expected_sha256=digest,
                project_id=request.project_id,
                pipeline_job_id=request.pipeline_job_id,
                analysis_job_id=request.analysis_job_id,
                source_sha256=request.source_sha256,
                question_queue_revision=request.question_queue_revision,
                review_completion_id=request.review_completion_id,
                answer_ledger_sha256=request.answer_ledger_sha256,
            )
        except ApprovedIncidentAuthorityError as error:
            raise DerivedOutputsConflict(
                "the promoted-survey Incident authority is invalid"
            ) from error

        result: dict[str, _SurveyIncidentEvidencePreflight] = {}
        evidence_backend = (
            self._candidate_evidence
            if self._candidate_evidence is not None
            else self._analysis_jobs
        )
        for record in reviewed_records:
            answer = record.get("final_answer")
            if not isinstance(answer, Mapping):
                raise DerivedOutputsConflict(
                    "a reviewed answer is invalid"
                )
            is_final_fact = (
                record.get("partition") == "approved"
                and answer.get("answer") in {"yes", "edit"}
                and answer.get("answered_by") == "local_user"
            )
            if not is_final_fact:
                continue
            candidate_id = record.get("candidate_id")
            candidate = candidates.get(str(candidate_id))
            question_id = record.get("question_id")
            record_id = record.get("record_id")
            record_hash = record.get("record_hash")
            answer_revision = record.get("answer_revision")
            try:
                expected_question_id = str(
                    uuid5(request.analysis_job_id, str(candidate_id))
                )
                canonical_question_id = str(UUID(str(question_id)))
                canonical_record_id = str(UUID(str(record_id)))
            except (TypeError, ValueError, AttributeError) as error:
                raise DerivedOutputsConflict(
                    "the survey answer identity is invalid"
                ) from error
            if (
                not isinstance(candidate_id, str)
                or _CANDIDATE_ID.fullmatch(candidate_id) is None
                or not isinstance(candidate, Mapping)
                or canonical_question_id != question_id
                or question_id != expected_question_id
                or canonical_record_id != record_id
                or record.get("project_id") != str(request.project_id)
                or record.get("analysis_job_id")
                != str(request.analysis_job_id)
                or not isinstance(record_hash, str)
                or _SHA256.fullmatch(record_hash) is None
                or isinstance(answer_revision, bool)
                or not isinstance(answer_revision, int)
                or answer_revision < 1
            ):
                raise DerivedOutputsConflict(
                    "the survey question or answer authority is invalid"
                )
            review_event_binding = candidate.get(
                "review_event_binding"
            )
            survey_event = survey_authority["event_binding"]
            if (
                survey_event.get("state") == "materialized"
                and (
                    not isinstance(review_event_binding, Mapping)
                    or review_event_binding.get(
                        "review_event_set_id"
                    )
                    != survey_event.get("review_event_set_id")
                    or review_event_binding.get(
                        "review_event_set_revision"
                    )
                    != survey_event.get(
                        "review_event_set_revision"
                    )
                    or review_event_binding.get(
                        "review_event_set_manifest_sha256"
                    )
                    != survey_event.get(
                        "review_event_set_manifest_sha256"
                    )
                )
            ):
                raise DerivedOutputsConflict(
                    "the survey ReviewEvent authority changed"
                )
            try:
                image = evidence_backend.resolve_artifact(
                    request.project_id,
                    request.analysis_job_id,
                    candidate_id,
                    "image",
                )
                clip = evidence_backend.resolve_artifact(
                    request.project_id,
                    request.analysis_job_id,
                    candidate_id,
                    "clip",
                )
            except HTTPException as error:
                raise DerivedOutputsError(
                    "approved Incident evidence is unavailable"
                ) from error
            image_sha256 = _verified_candidate_artifact_sha256(
                image,
                candidate.get("representative_image"),
                expected_media_type="image/jpeg",
                label="image",
            )
            # Retain the stronger JPEG magic/race check used by report
            # preparation as part of the pre-Incident gate.
            if _verified_report_image_sha256(image) != image_sha256:
                raise DerivedOutputsConflict(
                    "approved Incident image authority changed"
                )
            clip_sha256 = _verified_candidate_artifact_sha256(
                clip,
                candidate.get("preview_clip"),
                expected_media_type="video/mp4",
                label="clip",
            )
            try:
                output_authority = (
                    build_approved_incident_output_authority(
                        survey_authority=survey_authority,
                        survey_incident_authority_sha256=digest,
                        incident_id=question_id,
                        question_id=question_id,
                        candidate_id=candidate_id,
                        review_event_binding=review_event_binding,
                        approval_record_id=record_id,
                        approval_record_sha256=record_hash,
                        answer_revision=answer_revision,
                        candidate_metadata_sha256=(
                            _authority_sha256(candidate)
                        ),
                        representative_image_ref=(
                            f"analysis:{candidate_id}:representative"
                        ),
                        representative_image_sha256=image_sha256,
                        preview_clip_ref=(
                            f"analysis:{candidate_id}:preview"
                        ),
                        preview_clip_sha256=clip_sha256,
                    )
                )
            except ApprovedIncidentAuthorityError as error:
                raise DerivedOutputsConflict(
                    "the approved Incident output authority is invalid"
                ) from error
            result[candidate_id] = _SurveyIncidentEvidencePreflight(
                image=image,
                clip=clip,
                image_sha256=image_sha256,
                clip_sha256=clip_sha256,
                output_authority=output_authority,
            )
        return survey_authority, result

    def generate(
        self,
        request: DerivedOutputsRequest,
        *,
        cancelled: Optional[Callable[[], bool]] = None,
        artifact_root: Optional[
            ResolvedDerivedOutputsArtifactRoot
        ] = None,
    ) -> DerivedOutputsResult:
        if not isinstance(request, DerivedOutputsRequest):
            raise ValueError("request must be DerivedOutputsRequest")
        export_root, artifact_storage = self._resolve_export_storage(
            request, artifact_root
        )
        if cancelled is not None and cancelled():
            raise DerivedOutputsError("derived-output generation was cancelled")

        try:
            full_audit = self._candidate_labels.audit_records(
                request.project_id
            )
            latest = self._candidate_labels.latest_answers(
                request.project_id,
                analysis_job_id=request.analysis_job_id,
            )
        except CandidateLabelError as error:
            raise DerivedOutputsError(
                "the candidate label audit could not be verified"
            ) from error
        ledger_sha256, reviewed_records = _answer_ledger(
            latest, request.question_candidate_ids
        )
        if (
            ledger_sha256 != request.answer_ledger_sha256
            or (
                request.completion_mode == "reviewed"
                and request.review_completion_id
                != f"review:{ledger_sha256}"
            )
            or (
                request.completion_mode == "zero_detections"
                and request.review_completion_id
                != f"zero-detections:{ledger_sha256}"
            )
        ):
            raise DerivedOutputsConflict(
                "the pipeline completion does not match the server answer ledger"
            )
        if request.completion_mode == "zero_detections" and latest:
            raise DerivedOutputsConflict(
                "a zero-detection completion cannot contain reviewed answers"
            )

        try:
            analysis = self._analysis_jobs.get(
                request.project_id, request.analysis_job_id
            )
        except HTTPException as error:
            raise DerivedOutputsConflict(
                "the reviewed analysis job is unavailable"
            ) from error
        if (
            analysis.get("status") != "completed"
            or not isinstance(analysis.get("result"), Mapping)
            or analysis.get("source_sha256") != request.source_sha256
        ):
            raise DerivedOutputsConflict(
                "the reviewed analysis provenance does not match"
            )
        result_payload = analysis["result"]
        provenance = result_payload.get("provenance")
        candidates = result_payload.get("candidates")
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("source_sha256") != request.source_sha256
            or not isinstance(candidates, list)
        ):
            raise DerivedOutputsConflict(
                "the completed analysis result is invalid"
            )
        candidate_by_id = {
            str(item.get("candidate_id")): item
            for item in candidates
            if isinstance(item, Mapping)
        }
        if (
            request.completion_mode == "reviewed"
            and self._candidate_evidence is not None
        ):
            resolved_candidates: dict[str, Mapping[str, Any]] = {}
            try:
                for candidate_id in request.question_candidate_ids:
                    envelope = self._candidate_evidence.resolve_candidate(
                        request.project_id,
                        request.analysis_job_id,
                        candidate_id,
                    )
                    metadata = (
                        envelope.get("metadata")
                        if isinstance(envelope, Mapping)
                        else None
                    )
                    if (
                        not isinstance(metadata, Mapping)
                        or metadata.get("candidate_id")
                        != candidate_id
                    ):
                        raise DerivedOutputsConflict(
                            "candidate evidence metadata is invalid"
                        )
                    resolved_candidates[candidate_id] = metadata
            except HTTPException as error:
                raise DerivedOutputsConflict(
                    "the reviewed candidate evidence is unavailable"
                ) from error
            candidate_by_id.update(resolved_candidates)
            candidates = [
                candidate_by_id[candidate_id]
                for candidate_id in request.question_candidate_ids
            ]
        if any(
            candidate_id not in candidate_by_id
            for candidate_id in request.question_candidate_ids
        ):
            raise DerivedOutputsConflict(
                "the reviewed question set crossed the analysis boundary"
            )
        (
            survey_authority,
            survey_evidence_preflight,
        ) = self._survey_authority_preflight(
            request,
            reviewed_records=reviewed_records,
            candidates=candidate_by_id,
        )
        analysis_audit = [
            record
            for record in full_audit
            if record.get("analysis_job_id")
            == str(request.analysis_job_id)
        ]
        if request.completion_mode == "zero_detections":
            if analysis_audit:
                raise DerivedOutputsConflict(
                    "the zero-detection analysis contains answer records"
                )
        else:
            if not analysis_audit:
                raise DerivedOutputsConflict(
                    "the reviewed analysis has no verified answer audit"
                )
            try:
                # Projection starts only after the exact source, analysis and
                # reviewed question set above have crossed the server-owned
                # review boundary. The full verified ledger preserves its
                # project-wide chain, while only this analysis may become
                # Annotation/Incident. A zero-detection completion deliberately
                # skips projection because it has no user-approved truth.
                self._store.project_label_records(
                    request.project_id,
                    full_audit,
                    materialize_analysis_job_ids=[
                        request.analysis_job_id
                    ],
                    incident_analysis_job_ids=(
                        [request.analysis_job_id]
                        if survey_authority is not None
                        else []
                    ),
                )
            except LabelProjectionError as error:
                raise DerivedOutputsConflict(
                    "the reviewed analysis projection is unavailable"
                ) from error
        dataset_provenance = (
            {
                "state": "not_applicable_zero_detections",
                "approved_reference_member_count": 0,
                "approved_references_count_toward_validation": False,
                "automatic_training_started": False,
            }
            if request.completion_mode == "zero_detections"
            else self._dataset_snapshot(request.project_id)
        )

        proposals: Mapping[str, Mapping[str, Any]] = {}
        if self._experimental_proposals is not None:
            try:
                proposals = (
                    self._experimental_proposals.latest_for_job(
                        request.project_id, request.analysis_job_id
                    )
                )
            except ExperimentalProposalError as error:
                raise DerivedOutputsError(
                    "the experimental proposal catalog is unavailable"
                ) from error
            if not isinstance(proposals, Mapping):
                raise DerivedOutputsError(
                    "the experimental proposal catalog is invalid"
                )
        if request.completion_mode == "zero_detections":
            if any(
                _candidate_has_review_signal(
                    candidate,
                    proposals.get(str(candidate.get("candidate_id"))),
                )
                for candidate in candidates
                if isinstance(candidate, Mapping)
            ):
                raise DerivedOutputsConflict(
                    "the zero-detection analysis contains review proposals"
                )
            # Legacy scene/sharpness seeds are evidence-extraction inputs, not
            # road detections.  They must not appear as unclassified rows in an
            # otherwise verified zero-detection export.
            candidates = []
            candidate_by_id = {}
        raw_detections, detection_ids = _raw_detection_rows(
            candidates,
            proposals,
            trip_id=str(request.analysis_job_id),
            source_sha256=request.source_sha256,
        )
        label_questions, annotations = _latest_answer_rows(
            request, reviewed_records, candidate_by_id, proposals
        )

        active_incidents = self._store.list_incidents(
            request.project_id,
            include_inactive=False,
            analysis_job_id=request.analysis_job_id,
        )
        if (
            request.completion_mode == "zero_detections"
            and active_incidents
        ):
            raise DerivedOutputsConflict(
                "a zero-detection analysis cannot have active incidents"
            )
        selected_candidate_ids = set(request.question_candidate_ids)
        active_candidate_ids = [
            str(item.get("candidate_id")) for item in active_incidents
        ]
        if (
            any(
                candidate_id not in selected_candidate_ids
                for candidate_id in active_candidate_ids
            )
            or len(active_candidate_ids)
            != len(set(active_candidate_ids))
        ):
            raise DerivedOutputsConflict(
                "active incidents exceed the selected pipeline question set"
            )
        active_by_candidate = {
            str(item.get("candidate_id")): item
            for item in active_incidents
        }
        reviewed_incidents = []
        ocr_observations = []
        maintenance_actions = []
        evidence_sources: list[EvidenceExportSource] = []
        approved_for_report = []
        evidence_by_incident_id: dict[
            str, dict[str, Any]
        ] = {}
        incident_times_ms: dict[str, int] = {}
        approved_record_count = 0
        for record in reviewed_records:
            answer = record["final_answer"]
            approved = (
                record.get("partition") == "approved"
                and answer.get("answer") in {"yes", "edit"}
            )
            incident = active_by_candidate.get(record["candidate_id"])
            survey_truth = (
                approved
                and answer.get("answered_by") == "local_user"
                and record["candidate_id"]
                in survey_evidence_preflight
            )
            if approved and not survey_truth:
                # A human answer on an initial/training video, or an explicitly
                # delegated structured-LLM answer, is training truth only.
                # It remains visible in label_questions.csv / annotations.csv
                # and the immutable DatasetVersion, but without the separately
                # verified promoted-survey authority it must never become an
                # Incident, maintenance action, map marker, or report fact.
                if incident is not None:
                    raise DerivedOutputsConflict(
                        "a training-only label has an active incident"
                    )
                continue
            if not approved:
                if incident is not None:
                    raise DerivedOutputsConflict(
                        "a non-approved answer has an active incident"
                    )
                continue
            approved_record_count += 1
            if incident is None:
                raise DerivedOutputsConflict(
                    "an approved answer has no active incident projection"
                )
            candidate = candidate_by_id[record["candidate_id"]]
            if not self._incident_matches(
                request, record, incident, candidate
            ):
                raise DerivedOutputsConflict(
                    "the active incident failed exact provenance verification"
                )
            preflight = survey_evidence_preflight.get(
                record["candidate_id"]
            )
            if preflight is None:
                raise DerivedOutputsConflict(
                    "approved Incident evidence authority is missing"
                )
            image = preflight.image
            clip = preflight.clip
            image_source = EvidenceExportSource(
                incident_id=incident["incident_id"],
                kind="image",
                path=Path(image.path),
                media_type=image.media_type,
                size_bytes=image.size_bytes,
                sha256=preflight.image_sha256,
            )
            clip_source = EvidenceExportSource(
                incident_id=incident["incident_id"],
                kind="clip",
                path=Path(clip.path),
                media_type=clip.media_type,
                size_bytes=clip.size_bytes,
                sha256=preflight.clip_sha256,
            )
            evidence_sources.extend((image_source, clip_source))
            verified_deidentified = (
                request.verified_deidentified_evidence_by_incident_id.get(
                    incident["incident_id"]
                )
            )
            if (
                verified_deidentified is None
                and request.report_policy["evidence_mode"]
                == "shared_deidentified"
                and self._report_deidentified_evidence is not None
            ):
                try:
                    verified_deidentified = (
                        self._report_deidentified_evidence
                        .resolve_verified_descriptor(
                            project_id=request.project_id,
                            pipeline_job_id=request.pipeline_job_id,
                            analysis_job_id=request.analysis_job_id,
                            source_sha256=request.source_sha256,
                            incident_id=incident["incident_id"],
                            original_ref=(
                                f"analysis:{record['candidate_id']}:"
                                "representative"
                            ),
                            original_sha256=image_source.sha256,
                        )
                    )
                except ReportDeidentifiedEvidenceError as error:
                    raise DerivedOutputsError(
                        "verified report evidence failed integrity "
                        "verification"
                    ) from error
            evidence_descriptor = _evidence_descriptor(
                request,
                incident_id=incident["incident_id"],
                candidate_id=record["candidate_id"],
                original_sha256=image_source.sha256,
                verified_deidentified=verified_deidentified,
            )
            evidence_by_incident_id[
                incident["incident_id"]
            ] = evidence_descriptor
            incident_times_ms[incident["incident_id"]] = incident[
                "representative_time_ms"
            ]
            reviewed_row = {
                "incident_id": incident["incident_id"],
                "trip_id": str(request.analysis_job_id),
                "detection_ids": detection_ids[record["candidate_id"]],
                "event_type": answer["selected_issue_type"],
                "severity": answer.get("severity") or "",
                "maintenance_required": (
                    answer["selected_issue_type"]
                    not in _NON_MAINTENANCE_OBSERVATION_TYPES
                ),
                "recommended_action": (
                    answer.get("maintenance_action") or ""
                ),
                "review_status": "approved",
                "start_ms": incident["start_time_ms"],
                "end_ms": incident["end_time_ms"],
                "latitude": "",
                "longitude": "",
                "evidence_image_ref": image_source.archive_name,
                "preview_clip_ref": clip_source.archive_name,
                "ocr_text": answer.get("ocr_text") or "",
                "location_validation": "gps_not_connected",
                "model_version_id": survey_authority[
                    "model_version_id"
                ],
                "survey_incident_authority_sha256": (
                    request.survey_incident_authority_sha256
                ),
                "approved_incident_authority_sha256": (
                    preflight.output_authority["authority_sha256"]
                ),
                "dataset_version_id": survey_authority[
                    "dataset_version_id"
                ],
                "dataset_hash": survey_authority["dataset_hash"],
                "dataset_provenance_graph_sha256": survey_authority[
                    "dataset_provenance_graph_sha256"
                ],
                "fixed_validation_sha256": survey_authority[
                    "fixed_validation_sha256"
                ],
                "review_event_set_id": (
                    preflight.output_authority[
                        "review_event_binding"
                    ]["review_event_set_id"]
                ),
                "review_event_set_revision": (
                    preflight.output_authority[
                        "review_event_binding"
                    ]["review_event_set_revision"]
                ),
                "question_id": record["question_id"],
                "approval_record_id": record["record_id"],
                "approval_record_sha256": record["record_hash"],
                "evidence_image_sha256": image_source.sha256,
                "preview_clip_sha256": clip_source.sha256,
                "reviewed_at": record["recorded_at"],
            }
            reviewed_incidents.append(reviewed_row)
            # Every user-approved Incident from this exact PipelineJob is a
            # report fact.  Maintenance-only filtering would silently omit
            # approved sign/tunnel identity observations and make the binder's
            # "complete Incident set" claim false.  The binder independently
            # rechecks all scope and answer-record provenance below.
            approved_for_report.append(
                {
                    "incident_id": incident["incident_id"],
                    "project_id": str(request.project_id),
                    "pipeline_job_id": str(request.pipeline_job_id),
                    "analysis_job_id": str(request.analysis_job_id),
                    "source_sha256": request.source_sha256,
                    "review_status": "approved",
                    "approval_actor": "local_user",
                    "approval_record_id": record["record_id"],
                    "approval_record_sha256": record["record_hash"],
                    "incident_type": answer["selected_issue_type"],
                    "severity": (
                        answer.get("severity") or "unassessed"
                    ),
                    # Identity observations are valid approved Incidents but
                    # intentionally have no maintenance action.  The exact
                    # sample still needs deterministic caption/opinion text;
                    # keep CSV maintenance=false while projecting only this
                    # bounded observation wording into the report contract.
                    "maintenance_action": _report_incident_action(answer),
                    "ocr_text": answer.get("ocr_text"),
                    "time_ms": incident["representative_time_ms"],
                    # The binder selects the final Provider reference. This
                    # original ref is stable, opaque, and bound to the
                    # resolved image SHA-256 above; the local path is never
                    # serialized.
                    "evidence_refs": [
                        evidence_descriptor["original_ref"]
                    ],
                    "approved_output_authority": dict(
                        preflight.output_authority
                    ),
                }
            )
            if answer.get("ocr_text"):
                ocr_observations.append(
                    {
                        "ocr_observation_id": record["record_id"],
                        "trip_id": str(request.analysis_job_id),
                        "incident_id": incident["incident_id"],
                        "timestamp_ms": incident[
                            "representative_time_ms"
                        ],
                        "raw_text": answer["ocr_text"],
                        "normalized_text": answer["ocr_text"],
                        "candidate_name": answer["ocr_text"],
                        "kind": answer["selected_issue_type"],
                        "confidence": "",
                        "verified": True,
                        "verification_source": "local_user_review",
                        "region": answer.get("normalized_box") or "",
                        "model_version_id": survey_authority[
                            "model_version_id"
                        ],
                    }
                )
            if (
                reviewed_row["maintenance_required"]
                and answer.get("maintenance_action")
            ):
                maintenance_actions.append(
                    {
                        "action_id": record["record_id"],
                        "incident_id": incident["incident_id"],
                        "trip_id": str(request.analysis_job_id),
                        "action_type": answer["selected_issue_type"],
                        "description": answer["maintenance_action"],
                        "priority": answer.get("severity") or "",
                        "severity": answer.get("severity") or "",
                        "status": "confirmed",
                        "confirmed_by": "local_user",
                        "survey_incident_authority_sha256": (
                            request.survey_incident_authority_sha256
                        ),
                        "approved_incident_authority_sha256": (
                            preflight.output_authority[
                                "authority_sha256"
                            ]
                        ),
                        "model_version_id": survey_authority[
                            "model_version_id"
                        ],
                        "dataset_version_id": survey_authority[
                            "dataset_version_id"
                        ],
                        "dataset_provenance_graph_sha256": (
                            survey_authority[
                                "dataset_provenance_graph_sha256"
                            ]
                        ),
                        "fixed_validation_sha256": survey_authority[
                            "fixed_validation_sha256"
                        ],
                        "confirmed_at": record["recorded_at"],
                    }
                )
        if approved_record_count != len(reviewed_incidents):
            raise DerivedOutputsConflict(
                "approved incident projection is incomplete"
            )
        if {
            row["incident_id"] for row in reviewed_incidents
        } != {
            str(item.get("incident_id")) for item in active_incidents
        }:
            raise DerivedOutputsConflict(
                "the selected pipeline incident set is incomplete"
            )
        deidentified_ids = set(
            request.verified_deidentified_evidence_by_incident_id
        )
        approved_incident_ids = set(evidence_by_incident_id)
        if not deidentified_ids.issubset(approved_incident_ids):
            raise DerivedOutputsConflict(
                "verified deidentified evidence exceeds the approved "
                "pipeline incident set"
            )

        route_points, gpx_provenance = self._route_snapshot(request)
        locations_by_incident_id, coordinates_by_incident_id = (
            _report_locations(
                incident_times_ms=incident_times_ms,
                route_points=route_points,
                gpx_provenance=gpx_provenance,
            )
        )
        for row in reviewed_incidents:
            coordinates = coordinates_by_incident_id.get(
                row["incident_id"]
            )
            if coordinates is not None:
                row["latitude"] = coordinates[0]
                row["longitude"] = coordinates[1]
                row["location_validation"] = "timed_gpx_attached"

        blocking_reasons = _report_blocking_reasons(
            request,
            approved_incident_count=len(reviewed_incidents),
            report_preparer=self._report_preparer,
        )
        report_profile = {
            "provider": request.report_provider,
            "explicit_transmission_consent": (
                request.report_transmission_consent
            ),
            "provider_ready": request.report_provider_ready,
            "template_id": request.template_id,
            "template_sha256": request.template_sha256,
            "template_semantic_map_ready": (
                request.template_semantic_map_ready
            ),
            "template_semantic_map_id": (
                request.template_semantic_map_id
            ),
            "template_semantic_map_sha256": (
                request.template_semantic_map_sha256
            ),
            "route_name": request.report_route_name,
            "report_inspector_name": request.report_inspector_name,
            "inspection_date": request.report_inspection_date,
            "report_policy": dict(request.report_policy),
            "report_evidence_sha256": _sha256_value(
                evidence_by_incident_id
            ),
            "report_attempt": request.report_attempt,
        }
        derived_revision = _sha256_value(
            {
                "schema_version": DERIVED_OUTPUT_SCHEMA,
                "project_id": str(request.project_id),
                "pipeline_job_id": str(request.pipeline_job_id),
                "analysis_job_id": str(request.analysis_job_id),
                "source_sha256": request.source_sha256,
                "pipeline_input_fingerprint": (
                    request.pipeline_input_fingerprint
                ),
                "pipeline_profile_sha256": (
                    request.pipeline_profile_sha256
                ),
                "output_location_id": request.output_location_id,
                "artifact_storage": artifact_storage,
                "review_completion_id": (
                    request.review_completion_id
                ),
                "completion_mode": request.completion_mode,
                "answer_ledger_sha256": (
                    request.answer_ledger_sha256
                ),
                "question_queue_revision": (
                    request.question_queue_revision
                ),
                "survey_incident_authority_sha256": (
                    request.survey_incident_authority_sha256
                ),
                "dataset_version": dataset_provenance,
                "latest_record_hashes": [
                    item["record_hash"] for item in reviewed_records
                ],
                "gpx_provenance": gpx_provenance,
                "report_profile": report_profile,
            }
        )
        record_path = self._record_path(request, derived_revision)
        with self._lock:
            if record_path.exists():
                record = _load_record(record_path)
                if record.get("artifact_storage") != artifact_storage:
                    raise DerivedOutputsCorrupt(
                        "persisted derived-output storage binding changed"
                    )
                self._verify_record_artifacts(
                    record, export_root=export_root
                )
                return DerivedOutputsResult(
                    checkpoint=dict(record["checkpoint"]),
                    metrics=dict(record["metrics"]),
                    replayed=True,
                )

            if not approved_for_report:
                report = {
                    "state": "not_applicable_no_approved_incidents",
                    "blocking_reasons": [],
                    "provider_called": False,
                    "document_edit_plan_prepared": False,
                    "com_invoked": False,
                }
            elif blocking_reasons:
                report = {
                    "state": "blocked",
                    "blocking_reasons": list(blocking_reasons),
                    "provider_called": False,
                    "document_edit_plan_prepared": False,
                    "com_invoked": False,
                }
            else:
                try:
                    prepared = self._report_preparer.prepare(
                        project_id=request.project_id,
                        pipeline_job_id=request.pipeline_job_id,
                        analysis_job_id=request.analysis_job_id,
                        source_sha256=request.source_sha256,
                        provider=request.report_provider,
                        template_id=request.template_id,
                        template_sha256=request.template_sha256,
                        template_semantic_map_id=(
                            request.template_semantic_map_id
                        ),
                        template_semantic_map_sha256=(
                            request.template_semantic_map_sha256
                        ),
                        approved_incidents=tuple(approved_for_report),
                        evidence_by_incident_id={
                            item["incident_id"]: (
                                evidence_by_incident_id[
                                    item["incident_id"]
                                ]
                            )
                            for item in approved_for_report
                        },
                        route_name=request.report_route_name,
                        report_inspector_name=(
                            request.report_inspector_name
                        ),
                        inspection_date=(
                            request.report_inspection_date
                        ),
                        locations_by_incident_id={
                            item["incident_id"]: (
                                locations_by_incident_id[
                                    item["incident_id"]
                                ]
                            )
                            for item in approved_for_report
                        },
                        report_policy=dict(request.report_policy),
                        explicit_transmission_consent=(
                            request.report_transmission_consent
                        ),
                        report_attempt=request.report_attempt,
                    )
                    report = _safe_report_preview(prepared)
                except Exception as error:
                    # Provider/model output and local details never enter the
                    # checkpoint. A later explicit retry uses report_attempt.
                    error_code = getattr(error, "code", None)
                    safe_reason = (
                        error_code
                        if error_code
                        in _SAFE_REPORT_PREVIEW_ERROR_CODES
                        else "report_preview_failed"
                    )
                    report = {
                        "state": "blocked",
                        "blocking_reasons": [safe_reason],
                        "provider_called": bool(
                            getattr(error, "provider_called", False)
                        ),
                        "document_edit_plan_prepared": False,
                        "com_invoked": False,
                    }

            overall_state = (
                "completed"
                if report["state"]
                in {
                    "validated_preview",
                    "not_applicable_no_approved_incidents",
                }
                else "blocked"
            )
            export_payload = {
                "trip": {
                    "trip_id": str(request.analysis_job_id),
                    "project_id": str(request.project_id),
                    "analysis_job_id": str(request.analysis_job_id),
                    "source_sha256": request.source_sha256,
                },
                "detections": raw_detections,
                "incidents": reviewed_incidents,
                "label_questions": label_questions,
                "annotations": annotations,
                "ocr_observations": ocr_observations,
                "route_points": route_points,
                "maintenance_actions": maintenance_actions,
                "report_payload": {
                    "state": overall_state,
                    "completion_mode": request.completion_mode,
                    "reviewed_count": len(reviewed_records),
                    "approved_incident_count": len(reviewed_incidents),
                    "maintenance_incident_count": sum(
                        1
                        for item in reviewed_incidents
                        if item["maintenance_required"]
                    ),
                    "report_incident_count": len(
                        approved_for_report
                    ),
                    "approved_incidents": reviewed_incidents,
                    "approved_incident_authority_sha256s": [
                        item[
                            "approved_incident_authority_sha256"
                        ]
                        for item in reviewed_incidents
                    ],
                    "document_edit_plan_state": report["state"],
                    "document_edit_plan_sha256": report.get(
                        "plan_sha256"
                    ),
                    "dataset_version": dataset_provenance,
                },
                "provenance": {
                    "pipeline_job_id": str(request.pipeline_job_id),
                    "analysis_job_id": str(request.analysis_job_id),
                    "source_sha256": request.source_sha256,
                    "output_location_id": request.output_location_id,
                    "artifact_storage": artifact_storage,
                    "pipeline_input_fingerprint": (
                        request.pipeline_input_fingerprint
                    ),
                    "pipeline_profile_sha256": (
                        request.pipeline_profile_sha256
                    ),
                    "review_completion_id": (
                        request.review_completion_id
                    ),
                    "completion_mode": request.completion_mode,
                    "answer_ledger_sha256": (
                        request.answer_ledger_sha256
                    ),
                    "question_queue_revision": (
                        request.question_queue_revision
                    ),
                    "survey_incident_authority_sha256": (
                        request.survey_incident_authority_sha256
                    ),
                    "dataset_version": dataset_provenance,
                    "derived_output_revision": derived_revision,
                    "label_record_hashes": [
                        item["record_hash"] for item in reviewed_records
                    ],
                    "gpx_track": gpx_provenance,
                    "report": {
                        "state": report["state"],
                        "plan_sha256": report.get("plan_sha256"),
                        "draft_sha256": report.get("draft_sha256"),
                        "blocking_reasons": report[
                            "blocking_reasons"
                        ],
                        "com_invoked": False,
                    },
                },
            }
            try:
                exported = self._export_writer(
                    export_root,
                    export_payload,
                    evidence_sources=tuple(evidence_sources),
                    cancelled=cancelled,
                )
            except ArtifactExportError as error:
                raise DerivedOutputsError(
                    "the reviewed artifact package could not be verified"
                ) from error
            checkpoint, metrics = self._checkpoint(
                request,
                derived_revision,
                exported,
                report,
                raw_detection_count=len(raw_detections),
                reviewed_count=len(reviewed_records),
                incident_count=len(reviewed_incidents),
                annotation_count=len(annotations),
                route_point_count=len(route_points),
                evidence_file_count=len(evidence_sources),
                overall_state=overall_state,
                dataset_provenance=dataset_provenance,
            )
            record_body = {
                    "schema_version": DERIVED_OUTPUT_SCHEMA,
                    "project_id": str(request.project_id),
                    "pipeline_job_id": str(request.pipeline_job_id),
                    "analysis_job_id": str(request.analysis_job_id),
                    "derived_output_revision": derived_revision,
                    "bundle_id": exported.output_directory.name,
                    "artifacts": [
                        asdict(item) for item in exported.artifacts
                    ],
                    "checkpoint": checkpoint,
                    "metrics": metrics,
                }
            if artifact_storage is not None:
                record_body["artifact_storage"] = artifact_storage
            record = _record_payload(record_body)
            _write_atomic(record_path, _canonical_json(record))
            return DerivedOutputsResult(
                checkpoint=checkpoint,
                metrics=metrics,
                replayed=False,
            )

    def _route_snapshot(
        self, request: DerivedOutputsRequest
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self._gpx_tracks is None:
            return [], {
                "schema_version": GPX_EXPORT_PROVENANCE_SCHEMA,
                "status": "not_attached",
                "project_id": str(request.project_id),
                "trip_id": str(request.analysis_job_id),
            }
        try:
            result = self._gpx_tracks.export_snapshot(
                request.project_id, request.analysis_job_id
            )
        except GpxTrackError as error:
            if error.code not in {"track_not_found", "trip_unavailable"}:
                raise DerivedOutputsError(
                    "the attached GPS track failed verification"
                ) from error
            return [], {
                "schema_version": GPX_EXPORT_PROVENANCE_SCHEMA,
                "status": "not_attached",
                "project_id": str(request.project_id),
                "trip_id": str(request.analysis_job_id),
            }
        route_points = result.get("route_points")
        provenance = result.get("provenance")
        if not isinstance(route_points, list) or not isinstance(
            provenance, Mapping
        ):
            raise DerivedOutputsError(
                "the attached GPS export snapshot is invalid"
            )
        return route_points, dict(provenance)

    @staticmethod
    def _incident_matches(
        request: DerivedOutputsRequest,
        record: Mapping[str, Any],
        incident: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> bool:
        answer = record["final_answer"]
        expected_image = candidate.get("representative_image")
        expected_clip = candidate.get("preview_clip")
        if not isinstance(expected_image, Mapping) or not isinstance(
            expected_clip, Mapping
        ):
            return False
        try:
            return (
                incident.get("status") == "active"
                and incident.get("project_id") == str(request.project_id)
                and incident.get("analysis_job_id")
                == str(request.analysis_job_id)
                and incident.get("question_id")
                == record.get("question_id")
                and incident.get("incident_id")
                == record.get("question_id")
                and incident.get("candidate_id")
                == record.get("candidate_id")
                and incident.get("answer_revision")
                == record.get("answer_revision")
                and incident.get("source_answer_record_id")
                == record.get("record_id")
                and incident.get("source_record_hash")
                == record.get("record_hash")
                and incident.get("issue_type")
                == answer.get("selected_issue_type")
                and incident.get("severity") == answer.get("severity")
                and incident.get("maintenance_action")
                == answer.get("maintenance_action")
                and incident.get("ocr_text") == answer.get("ocr_text")
                and incident.get("evidence_image_ref")
                == expected_image.get("artifact_ref")
                and incident.get("preview_clip_ref")
                == expected_clip.get("artifact_ref")
                and incident.get("start_time_ms")
                == round(float(candidate["start_seconds"]) * 1000)
                and incident.get("representative_time_ms")
                == round(
                    float(
                        candidate["representative_timestamp_seconds"]
                    )
                    * 1000
                )
                and incident.get("end_time_ms")
                == round(float(candidate["end_seconds"]) * 1000)
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return False

    def _record_path(
        self,
        request: DerivedOutputsRequest,
        derived_revision: str,
    ) -> Path:
        project_root = _real_directory(
            self._record_root / str(request.project_id),
            "derived-output project root",
        )
        job_root = _real_directory(
            project_root / str(request.pipeline_job_id),
            "derived-output job root",
        )
        return job_root / f"{derived_revision}.json"

    def _verify_record_artifacts(
        self,
        record: Mapping[str, Any],
        *,
        export_root: Optional[Path] = None,
    ) -> None:
        bundle_id = record.get("bundle_id")
        artifacts = record.get("artifacts")
        if (
            not isinstance(bundle_id, str)
            or _BUNDLE_ID.fullmatch(bundle_id) is None
            or not isinstance(artifacts, list)
        ):
            raise DerivedOutputsCorrupt(
                "persisted derived-output bundle identity is invalid"
            )
        root = export_root or self._export_root
        bundle = root / bundle_id
        try:
            resolved = bundle.resolve(strict=True)
        except OSError as error:
            raise DerivedOutputsCorrupt(
                "persisted derived-output bundle is unavailable"
            ) from error
        if (
            bundle.is_symlink()
            or _is_reparse_point(bundle)
            or not bundle.is_dir()
            or resolved.parent != root
        ):
            raise DerivedOutputsCorrupt(
                "persisted derived-output bundle is unsafe"
            )
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise DerivedOutputsCorrupt(
                    "persisted artifact metadata is invalid"
                )
            name = artifact.get("name")
            size_bytes = artifact.get("size_bytes")
            sha256 = artifact.get("sha256")
            # Evidence members live only inside the verified ZIP. Top-level
            # files are rechecked here; archive verification checked members
            # at initial creation.
            if isinstance(name, str) and "/" in name:
                continue
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes < 0
                or not isinstance(sha256, str)
                or _SHA256.fullmatch(sha256) is None
            ):
                raise DerivedOutputsCorrupt(
                    "persisted artifact metadata is invalid"
                )
            path = bundle / name
            try:
                actual_size = path.stat().st_size
            except OSError as error:
                raise DerivedOutputsCorrupt(
                    "persisted artifact is unavailable"
                ) from error
            if actual_size != size_bytes or _sha256_file(path) != sha256:
                raise DerivedOutputsCorrupt(
                    "persisted artifact failed integrity verification"
                )

    @staticmethod
    def _checkpoint(
        request: DerivedOutputsRequest,
        derived_revision: str,
        exported: ArtifactExportResult,
        report: Mapping[str, Any],
        *,
        raw_detection_count: int,
        reviewed_count: int,
        incident_count: int,
        annotation_count: int,
        route_point_count: int,
        evidence_file_count: int,
        overall_state: str,
        dataset_provenance: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, int]]:
        manifest = next(
            item
            for item in exported.artifacts
            if item.name == "manifest.json"
        )
        archive = next(
            item
            for item in exported.artifacts
            if item.name == exported.archive_path.name
        )
        checkpoint = {
            "schema_version": DERIVED_CHECKPOINT_SCHEMA,
            "state": overall_state,
            "project_id": str(request.project_id),
            "pipeline_job_id": str(request.pipeline_job_id),
            "analysis_job_id": str(request.analysis_job_id),
            "review_completion_id": request.review_completion_id,
            "completion_mode": request.completion_mode,
            "answer_ledger_sha256": request.answer_ledger_sha256,
            "question_queue_revision": request.question_queue_revision,
            "survey_incident_authority_sha256": (
                request.survey_incident_authority_sha256
            ),
            "derived_output_revision": derived_revision,
            "export_bundle_id": exported.output_directory.name,
            "export_id": exported.export_id,
            "manifest_sha256": manifest.sha256,
            "archive_file_name": exported.archive_path.name,
            "archive_sha256": archive.sha256,
            "dataset_version": dict(dataset_provenance),
            "report": dict(report),
            "absolute_paths_exposed": False,
            "com_invoked": False,
        }
        metrics = {
            "raw_detection_count": raw_detection_count,
            "reviewed_answer_count": reviewed_count,
            "incident_count": incident_count,
            "annotation_count": annotation_count,
            "route_point_count": route_point_count,
            "evidence_file_count": evidence_file_count,
            "artifact_count": len(exported.artifacts),
            "dataset_training_gate_member_count": int(
                dataset_provenance.get(
                    "training_gate_member_count", 0
                )
            ),
            "dataset_approved_reference_member_count": int(
                dataset_provenance.get(
                    "approved_reference_member_count", 0
                )
            ),
        }
        _assert_checkpoint_safe(checkpoint)
        return checkpoint, metrics


__all__ = [
    "DERIVED_CHECKPOINT_SCHEMA",
    "DERIVED_OUTPUT_SCHEMA",
    "DatasetSnapshotFailed",
    "DerivedOutputsConflict",
    "DerivedOutputsCorrupt",
    "DerivedOutputsError",
    "DerivedOutputsRequest",
    "DerivedOutputsResult",
    "DerivedOutputsService",
    "ResolvedDerivedOutputsArtifactRoot",
    "ReportPreviewPreparer",
    "ReportPreviewUnavailable",
]
