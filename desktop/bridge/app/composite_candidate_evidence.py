"""Fail-closed candidate evidence resolution across immutable sources.

The completed ``AnalysisJob`` record remains the authoritative base source.
Additional dense/rescan candidates may live in one or more immutable overlay
stores, but they must never be appended to or otherwise mutate that base
record.  This module provides the one resolver surface that existing label,
model, OCR, SAM2 and export services can consume while an overlay store is
introduced independently.

Overlay backends return a path-free candidate envelope with these required
fields::

    {
        "status": "completed",
        "project_id": "<uuid>",
        "analysis_job_id": "<uuid>",
        "source_sha256": "<sha256>",
        "candidate_id": "<20 lowercase hex>",
        "candidate_metadata_sha256": "<sha256>",
        "metadata": {"candidate_id": "...", ...},
    }

``resolve_artifact`` and ``resolve_candidate_image_batch`` are deliberately
bridge-internal.  Their return types carry absolute ``Path`` values and must
not be placed in renderer/API response models.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable
from uuid import UUID, uuid4

from fastapi import HTTPException

from .analysis_jobs import (
    ResolvedAnalysisArtifact,
    ResolvedAnalysisCandidateImage,
    ResolvedAnalysisCandidateImageBatch,
)
from .candidate_evidence_overlay import (
    CandidateEvidenceOverlayNotFound,
)


MAX_CANDIDATES = 2_000
MAX_BATCH_CANDIDATES = 512
MAX_CANDIDATE_METADATA_BYTES = 256 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024

_CANDIDATE_ID = re.compile(r"^[a-f0-9]{20}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ARTIFACT_CONTRACTS = {
    "image": ("representative_image", "image/jpeg"),
    "clip": ("preview_clip", "video/mp4"),
}


class OverlayCandidateNotFound(LookupError):
    """Expected overlay miss used instead of an ambiguous ``None`` result."""


class CandidateEvidenceConflict(HTTPException):
    """The same identity was claimed by more than one immutable source."""

    def __init__(self, detail: str = "Candidate evidence sources conflict") -> None:
        super().__init__(status_code=409, detail=detail)


class CandidateEvidenceIntegrityError(HTTPException):
    """A source returned data that failed identity or integrity validation."""

    def __init__(
        self, detail: str = "Candidate evidence failed integrity verification"
    ) -> None:
        super().__init__(status_code=410, detail=detail)


@runtime_checkable
class BaseCandidateEvidenceBackend(Protocol):
    """Minimum existing ``AnalysisJobService`` surface."""

    def get(self, project_id: UUID, job_id: UUID) -> Mapping[str, Any]: ...

    def resolve_artifact(
        self,
        project_id: UUID,
        job_id: UUID,
        candidate_id: str,
        kind: str,
    ) -> ResolvedAnalysisArtifact: ...

    def resolve_candidate_image_batch(
        self,
        project_id: UUID,
        job_id: UUID,
        candidate_ids: Sequence[str],
    ) -> ResolvedAnalysisCandidateImageBatch: ...


@runtime_checkable
class OverlayCandidateEvidenceBackend(Protocol):
    """Adapter contract for an immutable candidate overlay store."""

    def resolve_candidate(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> Mapping[str, Any]: ...

    def list_candidates(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
    ) -> Sequence[Mapping[str, Any]]: ...

    def resolve_artifact(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        kind: str,
    ) -> ResolvedAnalysisArtifact: ...

    def resolve_candidate_image_batch(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_ids: Sequence[str],
    ) -> ResolvedAnalysisCandidateImageBatch: ...


class CandidateEvidenceOverlayStoreBackend(Protocol):
    """Structural surface of ``CandidateEvidenceOverlayStore``."""

    def get(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
    ) -> Any: ...

    def resolve_artifact(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
        candidate_id: str,
        kind: str,
    ) -> Any: ...


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
        raise CandidateEvidenceIntegrityError(
            "Candidate metadata is not safe JSON"
        ) from error


def _uuid_text(value: Any, expected: UUID, label: str) -> str:
    try:
        parsed = value if isinstance(value, UUID) else UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise CandidateEvidenceIntegrityError(
            f"Overlay {label} is invalid"
        ) from error
    if parsed != expected:
        raise CandidateEvidenceIntegrityError(
            f"Overlay {label} crossed its evidence boundary"
        )
    return str(parsed)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _not_found(error: BaseException) -> bool:
    return isinstance(error, OverlayCandidateNotFound) or (
        isinstance(error, HTTPException) and error.status_code == 404
    )


def _candidate_metadata(
    value: Mapping[str, Any],
    *,
    candidate_id: str,
    declared_sha256: Optional[str] = None,
) -> tuple[dict[str, Any], str]:
    if not isinstance(value, Mapping) or value.get("candidate_id") != candidate_id:
        raise CandidateEvidenceIntegrityError(
            "Candidate metadata identity is invalid"
        )
    encoded = _canonical_json(dict(value))
    if not 1 <= len(encoded) <= MAX_CANDIDATE_METADATA_BYTES:
        raise CandidateEvidenceIntegrityError(
            "Candidate metadata is outside the supported bound"
        )
    digest = hashlib.sha256(encoded).hexdigest()
    if declared_sha256 is not None and (
        _SHA256.fullmatch(declared_sha256) is None
        or declared_sha256 != digest
    ):
        raise CandidateEvidenceIntegrityError(
            "Candidate metadata hash does not match"
        )
    # Round-trip through JSON so custom Mapping values and every possible Path
    # object are removed from the public, path-free result.
    return json.loads(encoded.decode("utf-8")), digest


def _declared_metadata_sha256(value: Mapping[str, Any]) -> str:
    candidates = [
        item
        for item in (
            value.get("candidate_metadata_sha256"),
            value.get("metadata_sha256"),
        )
        if item is not None
    ]
    if (
        len(candidates) != 1
        or not isinstance(candidates[0], str)
        or _SHA256.fullmatch(candidates[0]) is None
    ):
        raise CandidateEvidenceIntegrityError(
            "Overlay candidate metadata hash is invalid"
        )
    return candidates[0]


def _validate_overlay_candidate(
    value: Mapping[str, Any],
    *,
    project_id: UUID,
    analysis_job_id: UUID,
    source_sha256: str,
    candidate_id: Optional[str] = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("status") != "completed":
        raise CandidateEvidenceIntegrityError(
            "Overlay candidate is not a completed immutable record"
        )
    _uuid_text(value.get("project_id"), project_id, "project identity")
    _uuid_text(
        value.get("analysis_job_id", value.get("job_id")),
        analysis_job_id,
        "analysis identity",
    )
    if value.get("source_sha256") != source_sha256:
        raise CandidateEvidenceIntegrityError(
            "Overlay candidate source identity does not match"
        )
    resolved_candidate_id = value.get("candidate_id")
    if (
        not isinstance(resolved_candidate_id, str)
        or _CANDIDATE_ID.fullmatch(resolved_candidate_id) is None
        or (
            candidate_id is not None
            and resolved_candidate_id != candidate_id
        )
    ):
        raise CandidateEvidenceIntegrityError(
            "Overlay candidate identity does not match"
        )
    metadata_value = value.get("metadata", value.get("candidate"))
    metadata, metadata_sha256 = _candidate_metadata(
        metadata_value,
        candidate_id=resolved_candidate_id,
        declared_sha256=_declared_metadata_sha256(value),
    )
    return {
        "status": "completed",
        "project_id": str(project_id),
        "analysis_job_id": str(analysis_job_id),
        "source_sha256": source_sha256,
        "candidate_id": resolved_candidate_id,
        "candidate_source": "overlay",
        "candidate_metadata_sha256": metadata_sha256,
        "metadata": metadata,
    }


def _validate_artifact_file(
    artifact: Any,
    *,
    metadata: Mapping[str, Any],
    kind: str,
) -> ResolvedAnalysisArtifact:
    metadata_key, expected_media_type = _ARTIFACT_CONTRACTS[kind]
    expected = metadata.get(metadata_key)
    if not isinstance(expected, Mapping):
        raise CandidateEvidenceIntegrityError(
            "Overlay candidate artifact metadata is missing"
        )
    expected_size = expected.get("size_bytes")
    expected_sha256 = expected.get("sha256")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or not 1 <= expected_size <= MAX_ARTIFACT_BYTES
        or not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
        or expected.get("media_type") != expected_media_type
    ):
        raise CandidateEvidenceIntegrityError(
            "Overlay candidate artifact metadata is invalid"
        )
    path = getattr(artifact, "path", None)
    media_type = getattr(artifact, "media_type", None)
    size_bytes = getattr(artifact, "size_bytes", None)
    sha256 = getattr(artifact, "sha256", None)
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or media_type != expected_media_type
        or size_bytes != expected_size
        or sha256 != expected_sha256
    ):
        raise CandidateEvidenceIntegrityError(
            "Overlay artifact handle does not match candidate metadata"
        )
    try:
        before = path.lstat()
        if (
            path.is_symlink()
            or _is_reparse_point(path)
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_size) != expected_size
        ):
            raise CandidateEvidenceIntegrityError(
                "Overlay artifact path is invalid"
            )
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            header = handle.read(32)
            digest.update(header)
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            opened_after = os.fstat(handle.fileno())
        after = path.lstat()
    except CandidateEvidenceIntegrityError:
        raise
    except OSError as error:
        raise CandidateEvidenceIntegrityError(
            "Overlay artifact is unavailable"
        ) from error
    if (
        (
            expected_media_type == "image/jpeg"
            and not header.startswith(b"\xff\xd8\xff")
        )
        or (
            expected_media_type == "video/mp4"
            and b"ftyp" not in header
        )
        or digest.hexdigest() != expected_sha256
        or int(opened_after.st_size) != expected_size
        or int(after.st_size) != expected_size
        or int(after.st_mtime_ns) != int(before.st_mtime_ns)
    ):
        raise CandidateEvidenceIntegrityError(
            "Overlay artifact failed integrity verification"
        )
    return ResolvedAnalysisArtifact(
        path=path.resolve(strict=True),
        media_type=expected_media_type,
        size_bytes=expected_size,
        sha256=expected_sha256,
    )


class CandidateEvidenceOverlayAdapter:
    """Bind one PipelineJob overlay to the generic composite protocol.

    ``CandidateEvidenceOverlayStore`` intentionally requires explicit
    ``pipeline_job_id`` and ``overlay_id`` on every lookup.  The rest of the
    application resolves candidates through the completed AnalysisJob
    identity, so this small adapter freezes those two overlay identities and
    emits the generic, path-free candidate envelope.
    """

    def __init__(
        self,
        store: CandidateEvidenceOverlayStoreBackend,
        *,
        pipeline_job_id: UUID,
        overlay_id: str,
    ) -> None:
        if not isinstance(pipeline_job_id, UUID):
            raise ValueError("pipeline_job_id must be a UUID")
        if (
            not isinstance(overlay_id, str)
            or re.fullmatch(r"^ceo_[a-f0-9]{32}$", overlay_id) is None
        ):
            raise ValueError("overlay_id is invalid")
        if not callable(getattr(store, "get", None)) or not callable(
            getattr(store, "resolve_artifact", None)
        ):
            raise ValueError("candidate evidence overlay store is invalid")
        self._store = store
        self._pipeline_job_id = pipeline_job_id
        self._overlay_id = overlay_id

    @staticmethod
    def _artifact_metadata(
        overlay_id: str,
        candidate_id: str,
        kind: str,
        artifact: Any,
    ) -> dict[str, Any]:
        artifact_id = getattr(artifact, "artifact_id", None)
        media_type = getattr(artifact, "media_type", None)
        size_bytes = getattr(artifact, "size_bytes", None)
        sha256 = getattr(artifact, "sha256", None)
        expected_media_type = (
            "image/jpeg" if kind == "image" else "video/mp4"
        )
        expected_suffix = ".jpg" if kind == "image" else ".mp4"
        if (
            not isinstance(artifact_id, str)
            or re.fullmatch(r"^cea_[a-f0-9]{32}$", artifact_id) is None
            or media_type != expected_media_type
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= MAX_ARTIFACT_BYTES
            or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None
        ):
            raise CandidateEvidenceIntegrityError(
                "Candidate overlay artifact metadata is invalid"
            )
        return {
            "artifact_id": artifact_id,
            "relative_path": f"{candidate_id}{expected_suffix}",
            "size_bytes": size_bytes,
            "sha256": sha256,
            "media_type": media_type,
            "artifact_ref": (
                f"candidate-evidence:{overlay_id}:{artifact_id}"
            ),
        }

    def _result(
        self, project_id: UUID, analysis_job_id: UUID
    ) -> Any:
        try:
            result = self._store.get(
                project_id,
                analysis_job_id,
                self._pipeline_job_id,
                self._overlay_id,
            )
        except CandidateEvidenceOverlayNotFound as error:
            raise OverlayCandidateNotFound(self._overlay_id) from error
        if (
            getattr(result, "overlay_id", None) != self._overlay_id
            or getattr(result, "project_id", None) != str(project_id)
            or getattr(result, "analysis_job_id", None)
            != str(analysis_job_id)
            or getattr(result, "pipeline_job_id", None)
            != str(self._pipeline_job_id)
            or not isinstance(getattr(result, "source_sha256", None), str)
            or _SHA256.fullmatch(result.source_sha256) is None
            or not isinstance(getattr(result, "manifest_sha256", None), str)
            or _SHA256.fullmatch(result.manifest_sha256) is None
            or not isinstance(
                getattr(result, "candidate_metadata_sha256", None), str
            )
            or _SHA256.fullmatch(result.candidate_metadata_sha256) is None
            or getattr(result, "review_only", None) is not True
            or getattr(result, "annotations_created", None) != 0
            or getattr(result, "incidents_created", None) != 0
            or getattr(result, "training_members_created", None) != 0
            or getattr(result, "report_truth_records_created", None) != 0
        ):
            raise CandidateEvidenceIntegrityError(
                "Candidate overlay result identity is invalid"
            )
        return result

    def _candidate_envelope(
        self,
        result: Any,
        candidate: Any,
    ) -> dict[str, Any]:
        candidate_id = getattr(candidate, "candidate_id", None)
        if (
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID.fullmatch(candidate_id) is None
        ):
            raise CandidateEvidenceIntegrityError(
                "Candidate overlay candidate identity is invalid"
            )
        image = self._artifact_metadata(
            self._overlay_id,
            candidate_id,
            "image",
            getattr(candidate, "image", None),
        )
        clip = self._artifact_metadata(
            self._overlay_id,
            candidate_id,
            "clip",
            getattr(candidate, "clip", None),
        )
        proposal_sources = list(
            getattr(candidate, "proposal_sources", ())
        )
        if "local_user_discovered_false_negative" in proposal_sources:
            candidate_kind = "user_discovered"
        elif "grounding_dino_review_sidecar" in proposal_sources:
            candidate_kind = (
                "dino_temporal_review_evidence_overlay"
            )
        else:
            candidate_kind = "dense_candidate_evidence_overlay"
        metadata = {
            "candidate_id": candidate_id,
            "candidate_kind": candidate_kind,
            "issue_type": getattr(candidate, "issue_type", None),
            "start_seconds": getattr(candidate, "start_seconds", None),
            "end_seconds": getattr(candidate, "end_seconds", None),
            "representative_timestamp_seconds": getattr(
                candidate, "representative_timestamp_seconds", None
            ),
            "representative_frame_id": getattr(
                candidate, "representative_frame_id", None
            ),
            "representative_frame_sha256": getattr(
                candidate, "representative_frame_sha256", None
            ),
            "score": getattr(candidate, "score", None),
            "normalized_box": getattr(candidate, "normalized_box", None),
            "ocr_text": getattr(candidate, "ocr_text", None),
            "urgent_review": getattr(candidate, "urgent_review", None),
            "supporting_frame_ids": list(
                getattr(candidate, "supporting_frame_ids", ())
            ),
            "proposal_sources": proposal_sources,
            "representative_proposal_provenance": getattr(
                candidate, "representative_proposal_provenance", None
            ),
            "proposal_provenance_sha256": getattr(
                candidate, "proposal_provenance_sha256", None
            ),
            "representative_image": image,
            "preview_clip": clip,
            "evidence_overlay": {
                "schema_version": getattr(result, "schema_version", None),
                "overlay_id": self._overlay_id,
                "pipeline_job_id": str(self._pipeline_job_id),
                "manifest_sha256": result.manifest_sha256,
                "discovery_revision": getattr(
                    result, "discovery_revision", None
                ),
                "dense_index_sha256": getattr(
                    result, "dense_index_sha256", None
                ),
                "provider_provenance_sha256": getattr(
                    result, "provider_provenance_sha256", None
                ),
                "model_provenance_sha256": getattr(
                    result, "model_provenance_sha256", None
                ),
                "candidate_set_metadata_sha256": (
                    result.candidate_metadata_sha256
                ),
                "review_only": True,
            },
        }
        safe_metadata, metadata_sha256 = _candidate_metadata(
            metadata, candidate_id=candidate_id
        )
        return {
            "status": "completed",
            "project_id": result.project_id,
            "analysis_job_id": result.analysis_job_id,
            "source_sha256": result.source_sha256,
            "candidate_id": candidate_id,
            "candidate_metadata_sha256": metadata_sha256,
            "metadata": safe_metadata,
        }

    def resolve_candidate(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> Mapping[str, Any]:
        result = self._result(project_id, analysis_job_id)
        candidate = next(
            (
                value
                for value in result.candidates
                if getattr(value, "candidate_id", None) == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise OverlayCandidateNotFound(candidate_id)
        return self._candidate_envelope(result, candidate)

    def list_candidates(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
    ) -> Sequence[Mapping[str, Any]]:
        _result, candidates = self.snapshot_candidates(
            project_id,
            analysis_job_id,
        )
        return candidates

    def snapshot_candidates(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
    ) -> tuple[Any, tuple[dict[str, Any], ...]]:
        """Freshly verify one immutable overlay and return one catalog.

        The returned envelopes are all derived from the exact same verified
        ``CandidateEvidenceOverlayResult``.  Callers that need a complete
        catalog must use this snapshot instead of resolving every listed
        candidate again, which would re-read and re-hash the whole immutable
        overlay once per candidate.
        """

        result = self._result(project_id, analysis_job_id)
        candidates = tuple(
            self._candidate_envelope(result, candidate)
            for candidate in result.candidates
        )
        if len({row["candidate_id"] for row in candidates}) != len(
            candidates
        ):
            raise CandidateEvidenceConflict(
                "Candidate overlay snapshot contains duplicate identities"
            )
        return result, candidates

    def resolve_artifact(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        kind: str,
    ) -> ResolvedAnalysisArtifact:
        try:
            artifact = self._store.resolve_artifact(
                project_id,
                analysis_job_id,
                self._pipeline_job_id,
                self._overlay_id,
                candidate_id,
                kind,
            )
        except CandidateEvidenceOverlayNotFound as error:
            raise OverlayCandidateNotFound(candidate_id) from error
        return ResolvedAnalysisArtifact(
            path=artifact.path,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
        )

    def resolve_candidate_image_batch(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_ids: Sequence[str],
    ) -> ResolvedAnalysisCandidateImageBatch:
        images = []
        run_directory: Optional[Path] = None
        for candidate_id in candidate_ids:
            artifact = self.resolve_artifact(
                project_id,
                analysis_job_id,
                candidate_id,
                "image",
            )
            parent = artifact.path.parent
            if run_directory is None:
                run_directory = parent
            elif parent != run_directory:
                raise CandidateEvidenceIntegrityError(
                    "Candidate overlay image batch crossed artifact roots"
                )
            images.append(
                ResolvedAnalysisCandidateImage(
                    candidate_id=candidate_id,
                    path=artifact.path,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                )
            )
        if run_directory is None:
            raise OverlayCandidateNotFound(
                "Candidate overlay image batch is empty"
            )
        return ResolvedAnalysisCandidateImageBatch(
            run_directory=run_directory,
            images=tuple(images),
        )


class CompositeCandidateEvidence:
    """Resolve base and overlay candidates without changing AnalysisJob truth."""

    def __init__(
        self,
        base: BaseCandidateEvidenceBackend,
        overlays: Sequence[OverlayCandidateEvidenceBackend] = (),
        *,
        batch_root: Optional[Path] = None,
    ) -> None:
        if not isinstance(overlays, Sequence) or isinstance(
            overlays, (str, bytes)
        ):
            raise ValueError("overlays must be a sequence")
        if not callable(getattr(base, "get", None)) or not callable(
            getattr(base, "resolve_artifact", None)
        ):
            raise ValueError("base candidate backend is invalid")
        checked_overlays = []
        for overlay in overlays:
            if any(
                not callable(getattr(overlay, method, None))
                for method in (
                    "resolve_candidate",
                    "list_candidates",
                    "resolve_artifact",
                )
            ):
                raise ValueError("overlay candidate backend is invalid")
            checked_overlays.append(overlay)
        self._base = base
        self._overlays = tuple(checked_overlays)
        self._batch_root = self._prepare_batch_root(batch_root)
        self._lock = threading.RLock()

    @staticmethod
    def _prepare_batch_root(value: Optional[Path]) -> Optional[Path]:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("composite batch_root must be absolute")
        path.mkdir(parents=True, exist_ok=True)
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ValueError("composite batch_root is unavailable") from error
        if (
            path.is_symlink()
            or _is_reparse_point(path)
            or not resolved.is_dir()
        ):
            raise ValueError("composite batch_root is unsafe")
        return resolved

    @staticmethod
    def _candidate_id(value: Any) -> str:
        if not isinstance(value, str) or _CANDIDATE_ID.fullmatch(value) is None:
            raise HTTPException(
                status_code=404,
                detail="The candidate evidence was not found",
            )
        return value

    def _base_snapshot(
        self, project_id: UUID, analysis_job_id: UUID
    ) -> tuple[str, list[dict[str, Any]]]:
        row = self._base.get(project_id, analysis_job_id)
        result = row.get("result") if isinstance(row, Mapping) else None
        source_sha256 = (
            row.get("source_sha256")
            if isinstance(row, Mapping)
            else None
        )
        candidates = (
            result.get("candidates")
            if isinstance(result, Mapping)
            else None
        )
        provenance = (
            result.get("provenance")
            if isinstance(result, Mapping)
            else None
        )
        if (
            not isinstance(row, Mapping)
            or row.get("status") != "completed"
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(result, Mapping)
            or not isinstance(candidates, list)
            or len(candidates) > MAX_CANDIDATES
            or not isinstance(provenance, Mapping)
            or provenance.get("source_sha256") != source_sha256
        ):
            raise CandidateEvidenceIntegrityError(
                "Completed analysis candidate provenance is invalid"
            )
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in candidates:
            candidate_id = (
                value.get("candidate_id")
                if isinstance(value, Mapping)
                else None
            )
            if (
                not isinstance(candidate_id, str)
                or _CANDIDATE_ID.fullmatch(candidate_id) is None
                or candidate_id in seen
            ):
                raise CandidateEvidenceIntegrityError(
                    "Completed analysis candidate catalog is invalid"
                )
            metadata, _metadata_sha256 = _candidate_metadata(
                value, candidate_id=candidate_id
            )
            normalized.append(metadata)
            seen.add(candidate_id)
        return source_sha256, normalized

    def _overlay_matches(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
        candidate_id: str,
    ) -> list[tuple[OverlayCandidateEvidenceBackend, dict[str, Any]]]:
        matches = []
        for overlay in self._overlays:
            try:
                raw = overlay.resolve_candidate(
                    project_id, analysis_job_id, candidate_id
                )
            except Exception as error:
                if _not_found(error):
                    continue
                if isinstance(error, CandidateEvidenceIntegrityError):
                    raise
                raise CandidateEvidenceIntegrityError(
                    "Overlay candidate resolver failed"
                ) from error
            matches.append(
                (
                    overlay,
                    _validate_overlay_candidate(
                        raw,
                        project_id=project_id,
                        analysis_job_id=analysis_job_id,
                        source_sha256=source_sha256,
                        candidate_id=candidate_id,
                    ),
                )
            )
        return matches

    @staticmethod
    def _base_envelope(
        project_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate_id = str(metadata["candidate_id"])
        safe_metadata, metadata_sha256 = _candidate_metadata(
            metadata, candidate_id=candidate_id
        )
        return {
            "status": "completed",
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "source_sha256": source_sha256,
            "candidate_id": candidate_id,
            "candidate_source": "analysis_job",
            "candidate_metadata_sha256": metadata_sha256,
            "metadata": safe_metadata,
        }

    def _resolve_origin(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> tuple[
        str,
        dict[str, Any],
        Optional[OverlayCandidateEvidenceBackend],
    ]:
        candidate_id = self._candidate_id(candidate_id)
        source_sha256, base_candidates = self._base_snapshot(
            project_id, analysis_job_id
        )
        base_candidate = next(
            (
                candidate
                for candidate in base_candidates
                if candidate["candidate_id"] == candidate_id
            ),
            None,
        )
        overlay_matches = self._overlay_matches(
            project_id,
            analysis_job_id,
            source_sha256,
            candidate_id,
        )
        if base_candidate is not None and overlay_matches:
            raise CandidateEvidenceConflict(
                "Candidate identity exists in both analysis and overlay evidence"
            )
        if len(overlay_matches) > 1:
            raise CandidateEvidenceConflict(
                "Candidate identity exists in multiple overlay evidence sets"
            )
        if base_candidate is not None:
            return (
                "analysis_job",
                self._base_envelope(
                    project_id,
                    analysis_job_id,
                    source_sha256,
                    base_candidate,
                ),
                None,
            )
        if overlay_matches:
            overlay, envelope = overlay_matches[0]
            return "overlay", envelope, overlay
        raise HTTPException(
            status_code=404,
            detail="The candidate evidence was not found",
        )

    def resolve_candidate(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> dict[str, Any]:
        """Return one path-free, canonical candidate resolver envelope."""

        _origin, envelope, _overlay = self._resolve_origin(
            project_id, analysis_job_id, candidate_id
        )
        return envelope

    def list_candidates(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
    ) -> tuple[dict[str, Any], ...]:
        """Return every unique candidate envelope and fail on any collision."""

        snapshots = []
        for overlay in self._overlays:
            try:
                listed = overlay.list_candidates(
                    project_id, analysis_job_id
                )
            except Exception as error:
                raise CandidateEvidenceIntegrityError(
                    "Overlay candidate catalog failed"
                ) from error
            snapshots.append(listed)
        return self.validate_candidate_snapshots(
            project_id,
            analysis_job_id,
            snapshots,
        )

    def validate_candidate_snapshots(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        overlay_snapshots: Sequence[
            Sequence[Mapping[str, Any]]
        ],
    ) -> tuple[dict[str, Any], ...]:
        """Validate already-fresh overlay catalogs as one atomic view.

        Every row is still identity/hash checked and every cross-source
        collision still fails closed.  The method deliberately does not call
        the per-candidate resolver: each supplied catalog is the immutable
        snapshot contract for that overlay operation.
        """

        if (
            isinstance(overlay_snapshots, (str, bytes))
            or not isinstance(overlay_snapshots, Sequence)
            or len(overlay_snapshots) > MAX_CANDIDATES
        ):
            raise CandidateEvidenceIntegrityError(
                "Overlay candidate snapshots are invalid"
            )
        source_sha256, base_candidates = self._base_snapshot(
            project_id, analysis_job_id
        )
        rows = [
            self._base_envelope(
                project_id,
                analysis_job_id,
                source_sha256,
                candidate,
            )
            for candidate in base_candidates
        ]
        by_candidate = {
            row["candidate_id"]: ("analysis_job", row) for row in rows
        }
        overlay_rows: list[tuple[int, dict[str, Any]]] = []
        for overlay_index, listed in enumerate(overlay_snapshots):
            if (
                isinstance(listed, (str, bytes))
                or not isinstance(listed, Sequence)
                or len(listed) > MAX_CANDIDATES
            ):
                raise CandidateEvidenceIntegrityError(
                    "Overlay candidate catalog is invalid"
                )
            seen_in_backend: set[str] = set()
            for raw in listed:
                envelope = _validate_overlay_candidate(
                    raw,
                    project_id=project_id,
                    analysis_job_id=analysis_job_id,
                    source_sha256=source_sha256,
                )
                candidate_id = envelope["candidate_id"]
                if candidate_id in seen_in_backend:
                    raise CandidateEvidenceConflict(
                        "Overlay candidate catalog contains duplicate identities"
                    )
                seen_in_backend.add(candidate_id)
                if candidate_id in by_candidate:
                    raise CandidateEvidenceConflict(
                        "Candidate identity is claimed by multiple evidence sources"
                    )
                by_candidate[candidate_id] = ("overlay", envelope)
                overlay_rows.append((overlay_index, envelope))
        overlay_rows.sort(
            key=lambda item: (item[0], item[1]["candidate_id"])
        )
        rows.extend(row for _index, row in overlay_rows)
        if len(rows) > MAX_CANDIDATES:
            raise CandidateEvidenceIntegrityError(
                "Combined candidate catalog exceeds the supported bound"
            )
        return tuple(rows)

    def resolve_artifact(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        kind: str,
    ) -> ResolvedAnalysisArtifact:
        """Resolve a verified internal artifact, preferring immutable base."""

        candidate_id = self._candidate_id(candidate_id)
        if kind not in _ARTIFACT_CONTRACTS:
            raise HTTPException(
                status_code=404,
                detail="The candidate evidence artifact was not found",
            )
        # Preserve every non-404 base failure.  An unavailable/corrupt base
        # artifact must never be hidden by a same-looking overlay file.
        base_artifact: Optional[ResolvedAnalysisArtifact] = None
        base_not_found: Optional[HTTPException] = None
        try:
            base_artifact = self._base.resolve_artifact(
                project_id, analysis_job_id, candidate_id, kind
            )
        except HTTPException as error:
            if error.status_code != 404:
                raise
            base_not_found = error

        origin, envelope, overlay = self._resolve_origin(
            project_id, analysis_job_id, candidate_id
        )
        if origin == "analysis_job":
            if base_artifact is None:
                assert base_not_found is not None
                raise base_not_found
            return base_artifact
        if base_artifact is not None:
            # ``get`` and ``resolve_artifact`` disagree, so the base record is
            # not safe enough to permit an overlay fallback.
            raise CandidateEvidenceConflict(
                "Base artifact and overlay candidate identities conflict"
            )
        assert overlay is not None
        try:
            artifact = overlay.resolve_artifact(
                project_id, analysis_job_id, candidate_id, kind
            )
        except Exception as error:
            if isinstance(error, CandidateEvidenceIntegrityError):
                raise
            raise CandidateEvidenceIntegrityError(
                "Overlay candidate artifact resolver failed"
            ) from error
        return _validate_artifact_file(
            artifact,
            metadata=envelope["metadata"],
            kind=kind,
        )

    def resolve_candidate_image_batch(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_ids: Sequence[str],
    ) -> ResolvedAnalysisCandidateImageBatch:
        """Resolve one internal OCR/model batch without exposing local paths."""

        if (
            isinstance(candidate_ids, (str, bytes))
            or not isinstance(candidate_ids, Sequence)
            or not 1 <= len(candidate_ids) <= MAX_BATCH_CANDIDATES
        ):
            raise HTTPException(
                status_code=404,
                detail="The candidate image batch was not found",
            )
        ordered_ids: list[str] = []
        origins = []
        for raw_candidate_id in candidate_ids:
            candidate_id = self._candidate_id(raw_candidate_id)
            if candidate_id in ordered_ids:
                raise HTTPException(
                    status_code=404,
                    detail="The candidate image batch was not found",
                )
            ordered_ids.append(candidate_id)
            origins.append(
                self._resolve_origin(
                    project_id, analysis_job_id, candidate_id
                )
            )

        if all(origin == "analysis_job" for origin, _row, _store in origins):
            resolver = getattr(
                self._base, "resolve_candidate_image_batch", None
            )
            if not callable(resolver):
                raise CandidateEvidenceIntegrityError(
                    "Base candidate image batch resolver is unavailable"
                )
            return resolver(project_id, analysis_job_id, ordered_ids)

        overlay_backends = {
            id(store): store
            for origin, _row, store in origins
            if origin == "overlay" and store is not None
        }
        if (
            len(overlay_backends) == 1
            and all(origin == "overlay" for origin, _row, _store in origins)
        ):
            overlay = next(iter(overlay_backends.values()))
            resolver = getattr(
                overlay, "resolve_candidate_image_batch", None
            )
            if callable(resolver):
                try:
                    batch = resolver(
                        project_id, analysis_job_id, ordered_ids
                    )
                except Exception as error:
                    raise CandidateEvidenceIntegrityError(
                        "Overlay candidate image batch resolver failed"
                    ) from error
                return self._validate_batch(batch, ordered_ids, origins)

        if self._batch_root is None:
            raise CandidateEvidenceConflict(
                "Candidate image batch crosses evidence stores without a staging root"
            )
        return self._stage_batch(
            project_id, analysis_job_id, ordered_ids, origins
        )

    def _validate_batch(
        self,
        batch: Any,
        candidate_ids: Sequence[str],
        origins: Sequence[
            tuple[
                str,
                dict[str, Any],
                Optional[OverlayCandidateEvidenceBackend],
            ]
        ],
    ) -> ResolvedAnalysisCandidateImageBatch:
        if not isinstance(batch, ResolvedAnalysisCandidateImageBatch):
            raise CandidateEvidenceIntegrityError(
                "Candidate image batch handle is invalid"
            )
        run_directory = batch.run_directory
        if (
            not isinstance(run_directory, Path)
            or not run_directory.is_absolute()
            or run_directory.is_symlink()
            or _is_reparse_point(run_directory)
            or not run_directory.is_dir()
            or len(batch.images) != len(candidate_ids)
        ):
            raise CandidateEvidenceIntegrityError(
                "Candidate image batch directory is invalid"
            )
        normalized_images = []
        for candidate_id, image, (_origin, envelope, _store) in zip(
            candidate_ids, batch.images, origins
        ):
            if (
                not isinstance(image, ResolvedAnalysisCandidateImage)
                or image.candidate_id != candidate_id
                or image.path.parent != run_directory
            ):
                raise CandidateEvidenceIntegrityError(
                    "Candidate image batch identity is invalid"
                )
            artifact = _validate_artifact_file(
                ResolvedAnalysisArtifact(
                    path=image.path,
                    media_type=image.media_type,
                    size_bytes=image.size_bytes,
                    sha256=image.sha256,
                ),
                metadata=envelope["metadata"],
                kind="image",
            )
            normalized_images.append(
                ResolvedAnalysisCandidateImage(
                    candidate_id=candidate_id,
                    path=artifact.path,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    sha256=artifact.sha256,
                )
            )
        return ResolvedAnalysisCandidateImageBatch(
            run_directory=run_directory.resolve(strict=True),
            images=tuple(normalized_images),
        )

    def _stage_batch(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_ids: Sequence[str],
        origins: Sequence[
            tuple[
                str,
                dict[str, Any],
                Optional[OverlayCandidateEvidenceBackend],
            ]
        ],
    ) -> ResolvedAnalysisCandidateImageBatch:
        assert self._batch_root is not None
        artifacts = [
            self.resolve_artifact(
                project_id, analysis_job_id, candidate_id, "image"
            )
            for candidate_id in candidate_ids
        ]
        material = {
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "images": [
                {
                    "candidate_id": candidate_id,
                    "sha256": artifact.sha256,
                    "candidate_metadata_sha256": envelope[
                        "candidate_metadata_sha256"
                    ],
                }
                for candidate_id, artifact, (_origin, envelope, _store) in zip(
                    candidate_ids, artifacts, origins
                )
            ],
        }
        batch_id = hashlib.sha256(_canonical_json(material)).hexdigest()
        project_root = self._batch_root / str(project_id)
        job_root = project_root / str(analysis_job_id)
        run_directory = job_root / batch_id
        with self._lock:
            for directory in (project_root, job_root, run_directory):
                directory.mkdir(exist_ok=True)
                resolved = directory.resolve(strict=True)
                if (
                    directory.is_symlink()
                    or _is_reparse_point(directory)
                    or not resolved.is_dir()
                    or (
                        directory != project_root
                        and resolved.parent
                        != directory.parent.resolve(strict=True)
                    )
                ):
                    raise CandidateEvidenceIntegrityError(
                        "Composite candidate batch directory is unsafe"
                    )
            images = []
            for index, (candidate_id, artifact) in enumerate(
                zip(candidate_ids, artifacts)
            ):
                destination = (
                    run_directory / f"{index:04d}-{candidate_id}.jpg"
                )
                self._copy_verified_artifact(artifact, destination)
                images.append(
                    ResolvedAnalysisCandidateImage(
                        candidate_id=candidate_id,
                        path=destination.resolve(strict=True),
                        media_type=artifact.media_type,
                        size_bytes=artifact.size_bytes,
                        sha256=artifact.sha256,
                    )
                )
        return ResolvedAnalysisCandidateImageBatch(
            run_directory=run_directory.resolve(strict=True),
            images=tuple(images),
        )

    @staticmethod
    def _copy_verified_artifact(
        artifact: ResolvedAnalysisArtifact,
        destination: Path,
    ) -> None:
        if destination.exists():
            existing = ResolvedAnalysisArtifact(
                path=destination,
                media_type=artifact.media_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
            )
            # Build the minimal metadata shape used by the shared verifier.
            _validate_artifact_file(
                existing,
                metadata={
                    "representative_image": {
                        "media_type": artifact.media_type,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                    }
                },
                kind="image",
            )
            return
        temporary = destination.with_name(
            f".{destination.name}.{uuid4().hex}.tmp"
        )
        try:
            digest = hashlib.sha256()
            size = 0
            with artifact.path.open("rb") as source, temporary.open("xb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                target.flush()
                os.fsync(target.fileno())
            if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
                raise CandidateEvidenceIntegrityError(
                    "Candidate image changed during batch staging"
                )
            os.replace(temporary, destination)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise


__all__ = [
    "BaseCandidateEvidenceBackend",
    "CandidateEvidenceOverlayAdapter",
    "CandidateEvidenceOverlayStoreBackend",
    "CandidateEvidenceConflict",
    "CandidateEvidenceIntegrityError",
    "CompositeCandidateEvidence",
    "OverlayCandidateEvidenceBackend",
    "OverlayCandidateNotFound",
]
