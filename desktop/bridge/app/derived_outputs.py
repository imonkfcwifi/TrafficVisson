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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from uuid import UUID, uuid4

from fastapi import HTTPException

from .artifact_export import (
    ArtifactExportError,
    ArtifactExportResult,
    EvidenceExportSource,
    export_artifacts,
)
from .candidate_labels import CandidateLabelError
from .dataset_versions import LabelProjectionError
from .experimental_proposals import ExperimentalProposalError
from .gpx_tracks import GPX_EXPORT_PROVENANCE_SCHEMA, GpxTrackError


DERIVED_OUTPUT_SCHEMA = "trafficvisson.derived-outputs.v1"
DERIVED_CHECKPOINT_SCHEMA = "trafficvisson.derived-output-checkpoint.v1"
MAX_DERIVED_RECORD_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BUNDLE_ID = re.compile(
    r"^trafficsupervision-export-[0-9a-f-]{36}-[A-Za-z0-9._-]+$"
)
_REPORT_PROVIDERS = frozenset({"codex", "claude", "local", "none"})


class DerivedOutputsError(RuntimeError):
    """Base class for a bounded, caller-safe derived-output failure."""


class DerivedOutputsConflict(DerivedOutputsError):
    """The supplied review or pipeline provenance is inconsistent."""


class DerivedOutputsCorrupt(DerivedOutputsError):
    """A persisted output record or artifact failed integrity verification."""


class ReportPreviewUnavailable(DerivedOutputsError):
    """The optional report preview boundary could not complete this attempt."""


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
    ) -> list[dict[str, Any]]: ...

    def list_incidents(
        self,
        project_id: UUID,
        *,
        include_inactive: bool = False,
        analysis_job_id: Optional[UUID] = None,
    ) -> list[dict[str, Any]]: ...


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
        approved_incidents: Sequence[Mapping[str, Any]],
        explicit_transmission_consent: bool,
    ) -> Mapping[str, Any]: ...


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
    question_candidate_ids: tuple[str, ...]
    template_id: str
    template_sha256: str
    report_provider: str
    report_transmission_consent: bool
    template_semantic_map_ready: bool
    report_provider_ready: bool
    output_location_id: str = "project-default"
    report_attempt: int = 0

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
            (self.template_sha256, "template_sha256"),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{label} must be a lowercase SHA-256")
        if self.review_completion_id != (
            f"review:{self.answer_ledger_sha256}"
        ):
            raise ValueError(
                "review_completion_id must bind the answer ledger SHA-256"
            )
        if (
            not self.question_candidate_ids
            or len(self.question_candidate_ids)
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
        for value, label in (
            (self.template_id, "template_id"),
            (self.output_location_id, "output_location_id"),
        ):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise ValueError(f"{label} is invalid")
        if self.report_provider not in _REPORT_PROVIDERS:
            raise ValueError("report_provider is invalid")
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


@dataclass(frozen=True)
class DerivedOutputsResult:
    checkpoint: Mapping[str, Any]
    metrics: Mapping[str, int]
    replayed: bool


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
                        else "embedded_analysis_inference"
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
                        if source.startswith("local_model")
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
                "label_source": "local_user_review",
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
    if request.report_transmission_consent is not True:
        reasons.append("report_transmission_consent_required")
    if request.template_semantic_map_ready is not True:
        reasons.append("template_semantic_map_unavailable")
    if (
        request.report_provider not in {"codex", "claude"}
        or request.report_provider_ready is not True
    ):
        reasons.append("report_provider_unavailable")
    if report_preparer is None:
        reasons.append("report_preview_preparer_unavailable")
    return tuple(reasons)


def _safe_report_preview(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReportPreviewUnavailable(
            "report preview metadata is invalid"
        )
    allowed = {
        "preview_id",
        "plan_sha256",
        "draft_sha256",
        "operation_count",
    }
    if set(value) != allowed:
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
        or not 1 <= operation_count <= 100
    ):
        raise ReportPreviewUnavailable(
            "report preview metadata is invalid"
        )
    return {
        "state": "validated_preview",
        "preview_id": preview_id,
        "plan_sha256": plan_sha256,
        "draft_sha256": draft_sha256,
        "operation_count": operation_count,
        "blocking_reasons": [],
        "provider_called": True,
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
        gpx_tracks: Optional[GpxTracks] = None,
        report_preparer: Optional[ReportPreviewPreparer] = None,
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
        self._gpx_tracks = gpx_tracks
        self._report_preparer = report_preparer
        self._export_writer = export_writer
        self._lock = threading.RLock()

    def generate(
        self,
        request: DerivedOutputsRequest,
        *,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> DerivedOutputsResult:
        if not isinstance(request, DerivedOutputsRequest):
            raise ValueError("request must be DerivedOutputsRequest")
        if request.output_location_id != "project-default":
            raise DerivedOutputsConflict(
                "the configured output location has no server-owned resolver"
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
            or request.review_completion_id != f"review:{ledger_sha256}"
        ):
            raise DerivedOutputsConflict(
                "the review completion does not match the server answer ledger"
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
        if any(
            candidate_id not in candidate_by_id
            for candidate_id in request.question_candidate_ids
        ):
            raise DerivedOutputsConflict(
                "the reviewed question set crossed the analysis boundary"
            )
        if not any(
            record.get("analysis_job_id")
            == str(request.analysis_job_id)
            for record in full_audit
        ):
            raise DerivedOutputsConflict(
                "the reviewed analysis has no verified answer audit"
            )
        try:
            # Projection starts only after the exact source, analysis and
            # reviewed question set above have crossed the server-owned review
            # boundary.  The full verified ledger preserves its project-wide
            # chain, while only this analysis may become Annotation/Incident.
            self._store.project_label_records(
                request.project_id,
                full_audit,
                materialize_analysis_job_ids=[
                    request.analysis_job_id
                ],
            )
        except LabelProjectionError as error:
            raise DerivedOutputsConflict(
                "the reviewed analysis projection is unavailable"
            ) from error

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
        active_by_candidate = {
            str(item.get("candidate_id")): item
            for item in active_incidents
            if str(item.get("candidate_id"))
            in set(request.question_candidate_ids)
        }
        reviewed_incidents = []
        ocr_observations = []
        maintenance_actions = []
        evidence_sources: list[EvidenceExportSource] = []
        approved_for_report = []
        approved_record_count = 0
        for record in reviewed_records:
            answer = record["final_answer"]
            approved = (
                record.get("partition") == "approved"
                and answer.get("answer") in {"yes", "edit"}
            )
            incident = active_by_candidate.get(record["candidate_id"])
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
            try:
                image = self._analysis_jobs.resolve_artifact(
                    request.project_id,
                    request.analysis_job_id,
                    record["candidate_id"],
                    "image",
                )
                clip = self._analysis_jobs.resolve_artifact(
                    request.project_id,
                    request.analysis_job_id,
                    record["candidate_id"],
                    "clip",
                )
            except HTTPException as error:
                raise DerivedOutputsError(
                    "approved incident evidence is unavailable"
                ) from error
            image_source = EvidenceExportSource(
                incident_id=incident["incident_id"],
                kind="image",
                path=Path(image.path),
                media_type=image.media_type,
                size_bytes=image.size_bytes,
                sha256=image.sha256,
            )
            clip_source = EvidenceExportSource(
                incident_id=incident["incident_id"],
                kind="clip",
                path=Path(clip.path),
                media_type=clip.media_type,
                size_bytes=clip.size_bytes,
                sha256=clip.sha256,
            )
            evidence_sources.extend((image_source, clip_source))
            reviewed_row = {
                "incident_id": incident["incident_id"],
                "trip_id": str(request.analysis_job_id),
                "detection_ids": detection_ids[record["candidate_id"]],
                "event_type": answer["selected_issue_type"],
                "severity": answer.get("severity") or "",
                "maintenance_required": True,
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
                "model_version_id": "",
                "reviewed_at": record["recorded_at"],
            }
            reviewed_incidents.append(reviewed_row)
            approved_for_report.append(
                {
                    "incident_id": incident["incident_id"],
                    "incident_type": answer["selected_issue_type"],
                    "severity": answer.get("severity") or "unassessed",
                    "maintenance_action": answer.get(
                        "maintenance_action"
                    ),
                    "ocr_text": answer.get("ocr_text"),
                    "time_ms": incident["representative_time_ms"],
                    # Only the still-image opaque reference is eligible for
                    # the provider boundary. The clip remains local.
                    "evidence_refs": [incident["evidence_image_ref"]],
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
                        "model_version_id": "",
                    }
                )
            if answer.get("maintenance_action"):
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
                        "confirmed_at": record["recorded_at"],
                    }
                )
        if approved_record_count != len(reviewed_incidents):
            raise DerivedOutputsConflict(
                "approved incident projection is incomplete"
            )

        route_points, gpx_provenance = self._route_snapshot(request)
        if route_points:
            for row in reviewed_incidents:
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
                "review_completion_id": (
                    request.review_completion_id
                ),
                "answer_ledger_sha256": (
                    request.answer_ledger_sha256
                ),
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
                self._verify_record_artifacts(record)
                return DerivedOutputsResult(
                    checkpoint=dict(record["checkpoint"]),
                    metrics=dict(record["metrics"]),
                    replayed=True,
                )

            if not reviewed_incidents:
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
                        approved_incidents=tuple(approved_for_report),
                        explicit_transmission_consent=True,
                    )
                    report = _safe_report_preview(prepared)
                except Exception:
                    # Provider/model output and local details never enter the
                    # checkpoint. A later explicit retry uses report_attempt.
                    report = {
                        "state": "blocked",
                        "blocking_reasons": ["report_preview_failed"],
                        "provider_called": True,
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
                else "partial"
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
                    "reviewed_count": len(reviewed_records),
                    "approved_incident_count": len(reviewed_incidents),
                    "approved_incidents": reviewed_incidents,
                    "document_edit_plan_state": report["state"],
                    "document_edit_plan_sha256": report.get(
                        "plan_sha256"
                    ),
                },
                "provenance": {
                    "pipeline_job_id": str(request.pipeline_job_id),
                    "analysis_job_id": str(request.analysis_job_id),
                    "source_sha256": request.source_sha256,
                    "pipeline_input_fingerprint": (
                        request.pipeline_input_fingerprint
                    ),
                    "pipeline_profile_sha256": (
                        request.pipeline_profile_sha256
                    ),
                    "review_completion_id": (
                        request.review_completion_id
                    ),
                    "answer_ledger_sha256": (
                        request.answer_ledger_sha256
                    ),
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
                    self._export_root,
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
            )
            record = _record_payload(
                {
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
            )
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
        self, record: Mapping[str, Any]
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
        bundle = self._export_root / bundle_id
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
            or resolved.parent != self._export_root
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
            "answer_ledger_sha256": request.answer_ledger_sha256,
            "derived_output_revision": derived_revision,
            "export_bundle_id": exported.output_directory.name,
            "export_id": exported.export_id,
            "manifest_sha256": manifest.sha256,
            "archive_file_name": exported.archive_path.name,
            "archive_sha256": archive.sha256,
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
        }
        _assert_checkpoint_safe(checkpoint)
        return checkpoint, metrics


__all__ = [
    "DERIVED_CHECKPOINT_SCHEMA",
    "DERIVED_OUTPUT_SCHEMA",
    "DerivedOutputsConflict",
    "DerivedOutputsCorrupt",
    "DerivedOutputsError",
    "DerivedOutputsRequest",
    "DerivedOutputsResult",
    "DerivedOutputsService",
    "ReportPreviewPreparer",
    "ReportPreviewUnavailable",
]
