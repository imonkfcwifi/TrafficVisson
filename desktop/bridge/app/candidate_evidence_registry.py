"""Durable registry for immutable candidate-evidence overlays.

``CandidateEvidenceOverlayAdapter`` deliberately binds one exact
``PipelineJob``/overlay pair.  That is the right fail-closed primitive, but a
long-running bridge needs a dynamic resolver that can:

* rebuild those bindings after an app or PC restart;
* add a newly materialized overlay without rebuilding every consumer; and
* continue exposing the existing ``AnalysisJobService`` resolver contract.

The registry scans only the documented directory hierarchy and never derives
an overlay binding from manifest JSON itself.  Every discovered identity is
re-resolved through ``CandidateEvidenceOverlayStore.get`` and then validated by
``CandidateEvidenceOverlayAdapter`` and ``CompositeCandidateEvidence``.  Local
paths remain confined to bridge-internal artifact handles.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

from .analysis_jobs import (
    ResolvedAnalysisArtifact,
    ResolvedAnalysisCandidateImageBatch,
)
from .candidate_evidence_overlay import (
    CANDIDATE_EVIDENCE_EXTERNAL_LOCATOR_FILES,
)
from .composite_candidate_evidence import (
    BaseCandidateEvidenceBackend,
    CandidateEvidenceIntegrityError,
    CandidateEvidenceOverlayAdapter,
    CandidateEvidenceOverlayStoreBackend,
    CompositeCandidateEvidence,
    OverlayCandidateNotFound,
)


_OVERLAY_ID = re.compile(r"^ceo_[a-f0-9]{32}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_LIMIT = 1_000_000
_DEFAULT_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_BATCH_ROOT_BINDING_SCHEMA = (
    "trafficvisson.candidate-inference-batch-root-binding.v1"
)
_BATCH_ROOT_BINDING_FILE = (
    "candidate-inference-batch-root-binding.json"
)


@dataclass(frozen=True)
class CandidateEvidenceRegistryLimits:
    """Bounds for one startup/refresh hierarchy scan."""

    max_projects: int = 512
    max_analyses_per_project: int = 4_096
    max_pipelines_per_analysis: int = 4_096
    max_overlays_per_pipeline: int = 64
    max_overlays_per_analysis: int = 512
    max_total_overlays: int = 16_384
    max_manifest_bytes: int = _DEFAULT_MAX_MANIFEST_BYTES

    def __post_init__(self) -> None:
        for name in (
            "max_projects",
            "max_analyses_per_project",
            "max_pipelines_per_analysis",
            "max_overlays_per_pipeline",
            "max_overlays_per_analysis",
            "max_total_overlays",
            "max_manifest_bytes",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= _MAX_LIMIT * 1024
            ):
                raise ValueError(f"{name} is outside the supported range")
        if self.max_manifest_bytes > 64 * 1024 * 1024:
            raise ValueError(
                "max_manifest_bytes is outside the supported range"
            )


@dataclass(frozen=True, order=True)
class CandidateEvidenceOverlayBinding:
    """Path-free identity and immutable fingerprint of one bound overlay."""

    project_id: UUID
    analysis_job_id: UUID
    pipeline_job_id: UUID
    overlay_id: str
    source_sha256: str
    manifest_sha256: str
    candidate_set_metadata_sha256: str
    candidate_count: int

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "status": "bound",
            "project_id": str(self.project_id),
            "analysis_job_id": str(self.analysis_job_id),
            "pipeline_job_id": str(self.pipeline_job_id),
            "overlay_id": self.overlay_id,
            "source_sha256": self.source_sha256,
            "manifest_sha256": self.manifest_sha256,
            "candidate_set_metadata_sha256": (
                self.candidate_set_metadata_sha256
            ),
            "candidate_count": self.candidate_count,
        }


@dataclass(frozen=True)
class _BoundOverlay:
    binding: CandidateEvidenceOverlayBinding
    adapter: CandidateEvidenceOverlayAdapter
    candidates: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CandidateEvidencePipelineSnapshot:
    """One freshly verified, path-free PipelineJob overlay catalog."""

    project_id: UUID
    analysis_job_id: UUID
    pipeline_job_id: UUID
    active_overlay_id: str
    source_sha256: str
    bindings: tuple[CandidateEvidenceOverlayBinding, ...]
    candidates: tuple[Mapping[str, Any], ...]


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _hash_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _write_atomic(path: Path, payload: bytes) -> None:
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


def _verified_record(value: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(value)
    body["record_sha256"] = _hash_value(body)
    return body


def _write_verified_record(
    path: Path,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    record = _verified_record(value)
    raw = _canonical_json(record)
    digest = hashlib.sha256(raw).hexdigest()
    _write_atomic(path, raw)
    _write_atomic(
        path.with_suffix(path.suffix + ".sha256"),
        f"{digest}\n".encode("ascii"),
    )
    return record


def _read_verified_record(path: Path) -> dict[str, Any]:
    _safe_manifest(path, maximum_bytes=64 * 1024)
    _safe_manifest(
        path.with_suffix(path.suffix + ".sha256"),
        maximum_bytes=128,
    )
    try:
        raw = path.read_bytes()
        sidecar = (
            path.with_suffix(path.suffix + ".sha256")
            .read_text(encoding="ascii")
            .strip()
        )
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CandidateEvidenceIntegrityError(
            "Candidate inference batch-root binding is invalid"
        ) from error
    if (
        not isinstance(value, dict)
        or _SHA256.fullmatch(sidecar or "") is None
        or hashlib.sha256(raw).hexdigest() != sidecar
    ):
        raise CandidateEvidenceIntegrityError(
            "Candidate inference batch-root binding changed"
        )
    record_sha256 = value.get("record_sha256")
    material = dict(value)
    material.pop("record_sha256", None)
    if (
        not isinstance(record_sha256, str)
        or _SHA256.fullmatch(record_sha256) is None
        or _hash_value(material) != record_sha256
    ):
        raise CandidateEvidenceIntegrityError(
            "Candidate inference batch-root binding changed"
        )
    return value


def _canonical_uuid(value: str) -> Optional[UUID]:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if str(parsed) == value else None


def _directory_fingerprint(path: Path) -> tuple[int, int, int]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CandidateEvidenceIntegrityError(
            "Candidate evidence registry directory is unavailable"
        ) from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise CandidateEvidenceIntegrityError(
            "Candidate evidence registry directory is unsafe"
        )
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mtime_ns),
    )


def _safe_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("candidate evidence overlay_root must be absolute")
    _directory_fingerprint(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CandidateEvidenceIntegrityError(
            "Candidate evidence overlay root is unavailable"
        ) from error
    if resolved != path and (
        path.is_symlink() or _is_reparse_point(path)
    ):
        raise CandidateEvidenceIntegrityError(
            "Candidate evidence overlay root is unsafe"
        )
    _directory_fingerprint(resolved)
    return resolved


def _safe_child_directory(parent: Path, name: str) -> Path:
    child = parent / name
    before = _directory_fingerprint(child)
    try:
        resolved = child.resolve(strict=True)
    except OSError as error:
        raise CandidateEvidenceIntegrityError(
            "Candidate evidence registry hierarchy is unavailable"
        ) from error
    if resolved.parent != parent or before != _directory_fingerprint(resolved):
        raise CandidateEvidenceIntegrityError(
            "Candidate evidence registry hierarchy crossed its root"
        )
    return resolved


def _safe_entries(
    directory: Path,
    *,
    maximum: int,
    label: str,
) -> tuple[str, ...]:
    before = _directory_fingerprint(directory)
    names: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > maximum:
                    raise CandidateEvidenceIntegrityError(
                        f"{label} exceeds the supported bound"
                    )
    except CandidateEvidenceIntegrityError:
        raise
    except OSError as error:
        raise CandidateEvidenceIntegrityError(
            f"{label} could not be enumerated"
        ) from error
    if before != _directory_fingerprint(directory):
        raise CandidateEvidenceIntegrityError(
            f"{label} changed during enumeration"
        )
    if len(names) != len(set(names)):
        raise CandidateEvidenceIntegrityError(
            f"{label} contains duplicate identities"
        )
    return tuple(sorted(names))


def _safe_manifest(path: Path, *, maximum_bytes: int) -> None:
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise CandidateEvidenceIntegrityError(
            "Candidate evidence overlay manifest is unavailable"
        ) from error
    if (
        path.is_symlink()
        or _is_reparse_point(path)
        or not stat.S_ISREG(before.st_mode)
        or int(before.st_nlink) != 1
        or not 1 <= int(before.st_size) <= maximum_bytes
        or resolved.parent != path.parent
    ):
        raise CandidateEvidenceIntegrityError(
            "Candidate evidence overlay manifest is unsafe"
        )


class CandidateEvidenceRegistry:
    """Dynamic, restart-safe facade over base and overlay evidence."""

    def __init__(
        self,
        base: BaseCandidateEvidenceBackend,
        overlay_store: CandidateEvidenceOverlayStoreBackend,
        *,
        overlay_root: Path,
        batch_root: Optional[Path] = None,
        limits: Optional[CandidateEvidenceRegistryLimits] = None,
    ) -> None:
        if not callable(getattr(base, "get", None)) or not callable(
            getattr(base, "resolve_artifact", None)
        ):
            raise ValueError("base candidate backend is invalid")
        if not callable(getattr(overlay_store, "get", None)) or not callable(
            getattr(overlay_store, "resolve_artifact", None)
        ):
            raise ValueError("candidate evidence overlay store is invalid")
        self._base = base
        self._store = overlay_store
        self._root = _safe_root(Path(overlay_root))
        self._batch_root = (
            Path(batch_root) if batch_root is not None else None
        )
        self._limits = limits or CandidateEvidenceRegistryLimits()
        self._lock = threading.RLock()
        self._bindings: dict[
            tuple[UUID, UUID, UUID, str], _BoundOverlay
        ] = {}
        self._composites: dict[
            tuple[UUID, UUID], CompositeCandidateEvidence
        ] = {}
        self._external_batch_roots: dict[
            tuple[UUID, UUID, UUID],
            tuple[Path, str],
        ] = {}
        self.refresh_from_disk()

    @property
    def overlay_root(self) -> Path:
        """Bridge-internal root; never include this property in API output."""

        return self._root

    def _leaf(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
    ) -> tuple[Path, bool]:
        parent = self._root
        for identity in (
            str(project_id),
            str(analysis_job_id),
            str(pipeline_job_id),
            overlay_id,
        ):
            parent = _safe_child_directory(parent, identity)
        entries = _safe_entries(
            parent,
            maximum=2,
            label="Candidate evidence overlay directory",
        )
        if entries == ("artifacts", "manifest.json"):
            _safe_manifest(
                parent / "manifest.json",
                maximum_bytes=self._limits.max_manifest_bytes,
            )
            _safe_child_directory(parent, "artifacts")
            return parent, False
        if entries == CANDIDATE_EVIDENCE_EXTERNAL_LOCATOR_FILES:
            _safe_manifest(
                parent / CANDIDATE_EVIDENCE_EXTERNAL_LOCATOR_FILES[0],
                maximum_bytes=self._limits.max_manifest_bytes,
            )
            _safe_manifest(
                parent / CANDIDATE_EVIDENCE_EXTERNAL_LOCATOR_FILES[1],
                maximum_bytes=128,
            )
            return parent, True
        else:
            raise CandidateEvidenceIntegrityError(
                "Candidate evidence overlay directory has unexpected entries"
            )

    def _discover_bindings(
        self,
    ) -> tuple[tuple[UUID, UUID, UUID, str], ...]:
        discovered: list[tuple[UUID, UUID, UUID, str]] = []
        project_names = _safe_entries(
            self._root,
            maximum=self._limits.max_projects,
            label="Candidate evidence project catalog",
        )
        for project_name in project_names:
            project_id = _canonical_uuid(project_name)
            if project_id is None:
                raise CandidateEvidenceIntegrityError(
                    "Candidate evidence project identity is invalid"
                )
            project_root = _safe_child_directory(
                self._root, project_name
            )
            analysis_names = _safe_entries(
                project_root,
                maximum=self._limits.max_analyses_per_project,
                label="Candidate evidence analysis catalog",
            )
            for analysis_name in analysis_names:
                analysis_job_id = _canonical_uuid(analysis_name)
                if analysis_job_id is None:
                    raise CandidateEvidenceIntegrityError(
                        "Candidate evidence analysis identity is invalid"
                    )
                analysis_root = _safe_child_directory(
                    project_root, analysis_name
                )
                pipeline_names = _safe_entries(
                    analysis_root,
                    maximum=self._limits.max_pipelines_per_analysis,
                    label="Candidate evidence PipelineJob catalog",
                )
                analysis_overlay_count = 0
                for pipeline_name in pipeline_names:
                    pipeline_job_id = _canonical_uuid(pipeline_name)
                    if pipeline_job_id is None:
                        raise CandidateEvidenceIntegrityError(
                            "Candidate evidence PipelineJob identity is invalid"
                        )
                    pipeline_root = _safe_child_directory(
                        analysis_root, pipeline_name
                    )
                    overlay_names = _safe_entries(
                        pipeline_root,
                        maximum=self._limits.max_overlays_per_pipeline,
                        label="Candidate evidence overlay catalog",
                    )
                    analysis_overlay_count += len(overlay_names)
                    if (
                        analysis_overlay_count
                        > self._limits.max_overlays_per_analysis
                    ):
                        raise CandidateEvidenceIntegrityError(
                            "Candidate evidence analysis overlays exceed "
                            "the supported bound"
                        )
                    for overlay_id in overlay_names:
                        if _OVERLAY_ID.fullmatch(overlay_id) is None:
                            raise CandidateEvidenceIntegrityError(
                                "Candidate evidence overlay identity is invalid"
                            )
                        self._leaf(
                            project_id,
                            analysis_job_id,
                            pipeline_job_id,
                            overlay_id,
                        )
                        discovered.append(
                            (
                                project_id,
                                analysis_job_id,
                                pipeline_job_id,
                                overlay_id,
                            )
                        )
                        if (
                            len(discovered)
                            > self._limits.max_total_overlays
                        ):
                            raise CandidateEvidenceIntegrityError(
                                "Candidate evidence registry exceeds "
                                "the supported bound"
                            )
        return tuple(discovered)

    def _validated_bound_overlay(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
    ) -> _BoundOverlay:
        self._leaf(
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
        )
        adapter = CandidateEvidenceOverlayAdapter(
            self._store,
            pipeline_job_id=pipeline_job_id,
            overlay_id=overlay_id,
        )
        try:
            # One store.get verifies the immutable manifest and every artifact;
            # all envelopes below are derived from that exact result.
            result, rows = adapter.snapshot_candidates(
                project_id,
                analysis_job_id,
            )
        except OverlayCandidateNotFound:
            raise
        except CandidateEvidenceIntegrityError:
            raise
        except Exception as error:
            raise CandidateEvidenceIntegrityError(
                "Candidate evidence overlay binding could not be verified"
            ) from error
        source_sha256 = getattr(result, "source_sha256", None)
        manifest_sha256 = getattr(result, "manifest_sha256", None)
        candidate_set_metadata_sha256 = getattr(
            result,
            "candidate_metadata_sha256",
            None,
        )
        if (
            not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(manifest_sha256, str)
            or _SHA256.fullmatch(manifest_sha256) is None
            or not isinstance(candidate_set_metadata_sha256, str)
            or _SHA256.fullmatch(
                candidate_set_metadata_sha256
            )
            is None
            or len(rows) != len(getattr(result, "candidates", ()))
        ):
            raise CandidateEvidenceIntegrityError(
                "Candidate evidence overlay fingerprint is invalid"
            )
        binding = CandidateEvidenceOverlayBinding(
            project_id=project_id,
            analysis_job_id=analysis_job_id,
            pipeline_job_id=pipeline_job_id,
            overlay_id=overlay_id,
            source_sha256=source_sha256,
            manifest_sha256=manifest_sha256,
            candidate_set_metadata_sha256=(
                candidate_set_metadata_sha256
            ),
            candidate_count=len(rows),
        )
        return _BoundOverlay(
            binding=binding,
            adapter=adapter,
            candidates=tuple(dict(row) for row in rows),
        )

    def _validated_scope_catalogs(
        self,
        bindings: Mapping[
            tuple[UUID, UUID, UUID, str], _BoundOverlay
        ],
    ) -> tuple[
        dict[tuple[UUID, UUID], CompositeCandidateEvidence],
        dict[tuple[UUID, UUID], tuple[dict[str, Any], ...]],
    ]:
        grouped: dict[
            tuple[UUID, UUID], list[_BoundOverlay]
        ] = {}
        for bound in bindings.values():
            scope = (
                bound.binding.project_id,
                bound.binding.analysis_job_id,
            )
            grouped.setdefault(scope, []).append(bound)

        composites: dict[
            tuple[UUID, UUID], CompositeCandidateEvidence
        ] = {}
        catalogs: dict[
            tuple[UUID, UUID], tuple[dict[str, Any], ...]
        ] = {}
        for scope, values in grouped.items():
            values.sort(key=lambda value: value.binding)
            composite = CompositeCandidateEvidence(
                self._base,
                [value.adapter for value in values],
                batch_root=self._batch_root,
            )
            catalog = composite.validate_candidate_snapshots(
                scope[0],
                scope[1],
                [value.candidates for value in values],
            )
            composites[scope] = composite
            catalogs[scope] = catalog
        return composites, catalogs

    def _refresh_from_disk_locked(
        self,
    ) -> tuple[
        tuple[tuple[UUID, UUID, UUID, str], ...],
        dict[tuple[UUID, UUID], tuple[dict[str, Any], ...]],
    ]:
        discovered = self._discover_bindings()
        fresh: dict[
            tuple[UUID, UUID, UUID, str], _BoundOverlay
        ] = {}
        for identity in discovered:
            try:
                candidate = self._validated_bound_overlay(*identity)
            except OverlayCandidateNotFound:
                _leaf, external = self._leaf(*identity)
                if external and identity not in self._bindings:
                    # A central external locator deliberately stores no path.
                    # Before PipelineJob root recovery it is dormant.  Once a
                    # binding has been live, disappearance is never ignored.
                    continue
                raise CandidateEvidenceIntegrityError(
                    "A bound candidate evidence overlay disappeared"
                )
            existing = self._bindings.get(identity)
            if (
                existing is not None
                and existing.binding != candidate.binding
            ):
                raise CandidateEvidenceIntegrityError(
                    "Candidate evidence overlay changed after it was bound"
                )
            fresh[identity] = candidate

        live_discovered = tuple(fresh)
        missing = set(self._bindings).difference(fresh)
        if missing:
            raise CandidateEvidenceIntegrityError(
                "A bound candidate evidence overlay disappeared"
            )

        # Validate every base/overlay and overlay/overlay collision against the
        # same fresh catalogs before replacing any live resolver state.
        composites, catalogs = self._validated_scope_catalogs(fresh)
        self._bindings = fresh
        self._composites.update(composites)
        return live_discovered, catalogs

    def _composite_with(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        replacement: Optional[_BoundOverlay] = None,
    ) -> CompositeCandidateEvidence:
        values = [
            value
            for key, value in self._bindings.items()
            if key[0] == project_id and key[1] == analysis_job_id
        ]
        if replacement is not None:
            replacement_key = (
                replacement.binding.project_id,
                replacement.binding.analysis_job_id,
                replacement.binding.pipeline_job_id,
                replacement.binding.overlay_id,
            )
            values = [
                value
                for value in values
                if (
                    value.binding.project_id,
                    value.binding.analysis_job_id,
                    value.binding.pipeline_job_id,
                    value.binding.overlay_id,
                )
                != replacement_key
            ]
            values.append(replacement)
        values.sort(key=lambda value: value.binding)
        return CompositeCandidateEvidence(
            self._base,
            [value.adapter for value in values],
            batch_root=self._batch_root,
        )

    def bind_overlay(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        overlay_id: str,
    ) -> dict[str, Any]:
        """Validate and atomically bind one newly materialized overlay.

        Calling this method again with the same immutable binding is
        idempotent.  A changed manifest or any candidate collision fails before
        the live resolver catalog is modified.
        """

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
        ):
            raise ValueError("overlay_id is invalid")
        key = (
            project_id,
            analysis_job_id,
            pipeline_job_id,
            overlay_id,
        )
        with self._lock:
            scope_keys = {
                identity
                for identity in self._bindings
                if (
                    identity[0] == project_id
                    and identity[1] == analysis_job_id
                )
            }
            scope_keys.add(key)
            try:
                fresh = {
                    identity: self._validated_bound_overlay(*identity)
                    for identity in sorted(
                        scope_keys,
                        key=lambda value: (
                            str(value[0]),
                            str(value[1]),
                            str(value[2]),
                            value[3],
                        ),
                    )
                }
            except OverlayCandidateNotFound as error:
                raise CandidateEvidenceIntegrityError(
                    "Candidate evidence overlay binding could not be verified"
                ) from error
            for identity, candidate in fresh.items():
                existing = self._bindings.get(identity)
                if (
                    existing is not None
                    and existing.binding != candidate.binding
                ):
                    raise CandidateEvidenceIntegrityError(
                        "Candidate evidence overlay changed after it was bound"
                    )
            composites, _catalogs = self._validated_scope_catalogs(fresh)
            self._bindings.update(fresh)
            self._composites[(project_id, analysis_job_id)] = (
                composites[(project_id, analysis_job_id)]
            )
            candidate = fresh[key]
            return candidate.binding.to_public_dict()

    def refresh_from_disk(self) -> int:
        """Discover persisted overlays and bind every one fail-closed."""

        with self._lock:
            discovered, _catalogs = self._refresh_from_disk_locked()
            return len(discovered)

    def resolve_pipeline_snapshot(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        active_overlay_id: str,
        source_sha256: str,
    ) -> CandidateEvidencePipelineSnapshot:
        """Return one atomic, freshly verified sibling-overlay snapshot."""

        for name, value in (
            ("project_id", project_id),
            ("analysis_job_id", analysis_job_id),
            ("pipeline_job_id", pipeline_job_id),
        ):
            if not isinstance(value, UUID):
                raise ValueError(f"{name} must be a UUID")
        if (
            not isinstance(active_overlay_id, str)
            or _OVERLAY_ID.fullmatch(active_overlay_id) is None
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
        ):
            raise ValueError("candidate evidence snapshot identity is invalid")
        with self._lock:
            self._refresh_from_disk_locked()
            members = [
                value
                for value in self._bindings.values()
                if (
                    value.binding.project_id == project_id
                    and value.binding.analysis_job_id == analysis_job_id
                    and value.binding.pipeline_job_id == pipeline_job_id
                    and value.binding.source_sha256 == source_sha256
                )
            ]
            members.sort(key=lambda value: value.binding)
            if (
                not members
                or active_overlay_id
                not in {
                    value.binding.overlay_id for value in members
                }
            ):
                raise CandidateEvidenceIntegrityError(
                    "Active candidate evidence overlay is not bound"
                )
            candidates = []
            seen: set[str] = set()
            for member in members:
                for envelope in member.candidates:
                    candidate_id = str(envelope.get("candidate_id", ""))
                    if candidate_id in seen:
                        raise CandidateEvidenceIntegrityError(
                            "Candidate evidence identity collided"
                        )
                    seen.add(candidate_id)
                    candidates.append(dict(envelope))
            candidates.sort(key=lambda row: str(row["candidate_id"]))
            return CandidateEvidencePipelineSnapshot(
                project_id=project_id,
                analysis_job_id=analysis_job_id,
                pipeline_job_id=pipeline_job_id,
                active_overlay_id=active_overlay_id,
                source_sha256=source_sha256,
                bindings=tuple(
                    value.binding for value in members
                ),
                candidates=tuple(candidates),
            )

    def list_bindings(self) -> tuple[dict[str, Any], ...]:
        """Return deterministic, path-free registry metadata."""

        with self._lock:
            return tuple(
                value.binding.to_public_dict()
                for _key, value in sorted(self._bindings.items())
            )

    def _composite(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
    ) -> CompositeCandidateEvidence:
        with self._lock:
            composite = self._composites.get(
                (project_id, analysis_job_id)
            )
            if composite is None:
                composite = CompositeCandidateEvidence(
                    self._base,
                    batch_root=self._batch_root,
                )
                self._composites[
                    (project_id, analysis_job_id)
                ] = composite
            return composite

    def resolve_candidate(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
    ) -> Mapping[str, Any]:
        return self._composite(
            project_id, analysis_job_id
        ).resolve_candidate(
            project_id, analysis_job_id, candidate_id
        )

    def list_candidates(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
    ) -> tuple[dict[str, Any], ...]:
        return self._composite(
            project_id, analysis_job_id
        ).list_candidates(project_id, analysis_job_id)

    def resolve_artifact(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_id: str,
        kind: str,
    ) -> ResolvedAnalysisArtifact:
        # CompositeCandidateEvidence deliberately probes base first and
        # preserves every non-404 base failure before considering an overlay.
        return self._composite(
            project_id, analysis_job_id
        ).resolve_artifact(
            project_id, analysis_job_id, candidate_id, kind
        )

    def resolve_candidate_image_batch(
        self,
        project_id: UUID,
        analysis_job_id: UUID,
        candidate_ids: Sequence[str],
        *,
        pipeline_job_id: Optional[UUID] = None,
        artifact_root: Optional[Path] = None,
    ) -> ResolvedAnalysisCandidateImageBatch:
        if artifact_root is None:
            if pipeline_job_id is not None:
                raise ValueError(
                    "pipeline_job_id requires artifact_root"
                )
            return self._composite(
                project_id, analysis_job_id
            ).resolve_candidate_image_batch(
                project_id, analysis_job_id, candidate_ids
            )
        if (
            not isinstance(project_id, UUID)
            or not isinstance(analysis_job_id, UUID)
            or not isinstance(pipeline_job_id, UUID)
        ):
            raise ValueError(
                "external candidate batch identities must be UUIDs"
            )
        if (
            isinstance(candidate_ids, (str, bytes))
            or not isinstance(candidate_ids, Sequence)
            or not candidate_ids
        ):
            raise ValueError("candidate_ids must be a non-empty sequence")
        with self._lock:
            requested_ids = tuple(str(value) for value in candidate_ids)
            overlay_pipeline_by_candidate: dict[str, UUID] = {}
            adapters = []
            for key, bound in sorted(self._bindings.items()):
                if key[0] != project_id or key[1] != analysis_job_id:
                    continue
                adapters.append(bound.adapter)
                for row in bound.adapter.list_candidates(
                    project_id, analysis_job_id
                ):
                    candidate_id = row.get("candidate_id")
                    if (
                        isinstance(candidate_id, str)
                        and candidate_id in requested_ids
                    ):
                        previous = overlay_pipeline_by_candidate.get(
                            candidate_id
                        )
                        if (
                            previous is not None
                            and previous != key[2]
                        ):
                            raise CandidateEvidenceIntegrityError(
                                "Candidate evidence crossed PipelineJobs"
                            )
                        overlay_pipeline_by_candidate[
                            candidate_id
                        ] = key[2]
            if any(
                value != pipeline_job_id
                for value in overlay_pipeline_by_candidate.values()
            ):
                raise CandidateEvidenceIntegrityError(
                    "Candidate inference batch crossed its PipelineJob root"
                )
            composite = CompositeCandidateEvidence(
                self._base,
                adapters,
                batch_root=self._bind_external_batch_root(
                    project_id=project_id,
                    analysis_job_id=analysis_job_id,
                    pipeline_job_id=pipeline_job_id,
                    artifact_root=Path(artifact_root),
                    source_sha256s=tuple(
                        sorted(
                            {
                                str(
                                    self._composite(
                                        project_id,
                                        analysis_job_id,
                                    )
                                    .resolve_candidate(
                                        project_id,
                                        analysis_job_id,
                                        candidate_id,
                                    )
                                    .get("source_sha256")
                                )
                                for candidate_id in requested_ids
                            }
                        )
                    ),
                ),
            )
            return composite.resolve_candidate_image_batch(
                project_id, analysis_job_id, requested_ids
            )

    def _bind_external_batch_root(
        self,
        *,
        project_id: UUID,
        analysis_job_id: UUID,
        pipeline_job_id: UUID,
        artifact_root: Path,
        source_sha256s: tuple[str, ...],
    ) -> Path:
        if (
            not source_sha256s
            or any(
                _SHA256.fullmatch(value or "") is None
                for value in source_sha256s
            )
        ):
            raise CandidateEvidenceIntegrityError(
                "Candidate inference source provenance is invalid"
            )
        root = _safe_root(artifact_root)
        try:
            metadata = root.stat()
        except OSError as error:
            raise CandidateEvidenceIntegrityError(
                "Candidate inference batch root is unavailable"
            ) from error
        identity = _hash_value(
            {
                "category": "candidate-inference-batches",
                "normalized_absolute_path": os.path.normcase(
                    os.path.abspath(str(root))
                ),
                "device": int(metadata.st_dev),
                "inode": int(metadata.st_ino),
            }
        )
        material = {
            "schema_version": _BATCH_ROOT_BINDING_SCHEMA,
            "category": "candidate-inference-batches",
            "project_id": str(project_id),
            "analysis_job_id": str(analysis_job_id),
            "pipeline_job_id": str(pipeline_job_id),
            "source_sha256s": list(source_sha256s),
            "artifact_root_identity_sha256": identity,
        }
        expected = _verified_record(material)
        binding_path = root / _BATCH_ROOT_BINDING_FILE
        if binding_path.exists():
            persisted = _read_verified_record(binding_path)
            if persisted != expected:
                raise CandidateEvidenceIntegrityError(
                    "Candidate inference batch root is bound to another job"
                )
        else:
            persisted = _write_verified_record(
                binding_path, material
            )
            if persisted != expected:
                raise CandidateEvidenceIntegrityError(
                    "Candidate inference batch-root binding is invalid"
                )
        key = (project_id, analysis_job_id, pipeline_job_id)
        cached = self._external_batch_roots.get(key)
        if cached is not None and (
            cached[0] != root or cached[1] != identity
        ):
            raise CandidateEvidenceIntegrityError(
                "Candidate inference batch root changed after binding"
            )
        self._external_batch_roots[key] = (root, identity)
        return root


__all__ = [
    "CandidateEvidenceOverlayBinding",
    "CandidateEvidencePipelineSnapshot",
    "CandidateEvidenceRegistry",
    "CandidateEvidenceRegistryLimits",
]
