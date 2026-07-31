"""Bridge-internal grants for frames in the active immutable 2 FPS index.

The registry is deliberately memory-only. A PipelineJob registers its verified
index immediately before dense discovery/materialization, including after a
restart. Persisted overlays do not need this registry for later reads because
they already contain copied, hash-verified evidence.
"""

from __future__ import annotations

import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import UUID

from .candidate_evidence_overlay import ResolvedDenseCandidateFrame
from .dense_candidate_discovery import (
    DenseCandidateDiscoveryIntegrityError,
    resolve_dense_candidate_frame,
)
from .project_artifact_roots import (
    ProjectArtifactRootError,
    ProjectArtifactRootRegistry,
)


_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_DENSE_RUN_ID = re.compile(
    r"^dense-frame-index-[A-Za-z0-9._-]{6,128}$"
)


class DenseCandidateFrameRegistryError(RuntimeError):
    pass


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(
        attributes
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


@dataclass(frozen=True)
class _RegisteredDenseIndex:
    source_sha256: str
    path: Path
    root: Path
    pipeline_job_id: Optional[UUID]
    artifact_root_binding: Optional[dict[str, Any]]


class DenseCandidateFrameRegistry:
    """Bind a verified dense-index digest to its server-only index path."""

    def __init__(
        self,
        bridge_data_root: Path,
        *,
        artifact_root_registry: Optional[
            ProjectArtifactRootRegistry
        ] = None,
    ) -> None:
        root = Path(bridge_data_root)
        if not root.is_absolute():
            raise ValueError("bridge_data_root must be absolute")
        root.mkdir(parents=True, exist_ok=True)
        try:
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise DenseCandidateFrameRegistryError(
                "dense frame registry root is unavailable"
            ) from error
        if (
            root.is_symlink()
            or _is_reparse_point(root)
            or not resolved.is_dir()
        ):
            raise DenseCandidateFrameRegistryError(
                "dense frame registry root is unsafe"
            )
        self._root = resolved
        if (
            artifact_root_registry is not None
            and not callable(
                getattr(
                    artifact_root_registry,
                    "resolve_job_binding",
                    None,
                )
            )
        ):
            raise ValueError(
                "artifact_root_registry must resolve job bindings"
            )
        self._artifact_roots = artifact_root_registry
        self._entries: dict[
            tuple[UUID, Optional[UUID], str],
            _RegisteredDenseIndex,
        ] = {}
        self._lock = threading.RLock()

    def register(
        self,
        project_id: UUID,
        source_sha256: str,
        dense_index_sha256: str,
        dense_index_path: Path,
        pipeline_job_id: Optional[UUID] = None,
        artifact_root_binding: Optional[
            Mapping[str, Any]
        ] = None,
    ) -> None:
        if not isinstance(project_id, UUID):
            raise ValueError("project_id must be a UUID")
        if (
            pipeline_job_id is not None
            and not isinstance(pipeline_job_id, UUID)
        ):
            raise ValueError("pipeline_job_id must be a UUID")
        if (
            not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(dense_index_sha256, str)
            or _SHA256.fullmatch(dense_index_sha256) is None
        ):
            raise ValueError("dense index identity is invalid")
        path = Path(dense_index_path)
        if not path.is_absolute():
            raise DenseCandidateFrameRegistryError(
                "dense index path is invalid"
            )
        binding: Optional[dict[str, Any]] = None
        if artifact_root_binding is not None:
            if (
                pipeline_job_id is None
                or self._artifact_roots is None
                or not isinstance(artifact_root_binding, Mapping)
            ):
                raise DenseCandidateFrameRegistryError(
                    "dense index artifact-root binding is invalid"
                )
            try:
                authorized = (
                    self._artifact_roots.resolve_job_binding(
                        project_id,
                        artifact_root_binding,
                    )
                )
            except ProjectArtifactRootError as error:
                raise DenseCandidateFrameRegistryError(
                    "dense index artifact-root binding is invalid"
                ) from error
            root = authorized.root
            binding = dict(artifact_root_binding)
            expected_parts = (
                "pipeline-jobs",
                str(pipeline_job_id),
                "dense",
            )
            expected_length = 5
            outside_error = (
                "dense index path is outside the project artifact root"
            )
        else:
            root = self._root
            expected_parts = (
                "projects",
                str(project_id),
                "pipeline",
                "dense",
            )
            expected_length = 7
            outside_error = (
                "dense index path is outside the bridge data root"
            )
        resolved, relative = self._validate_index_path(
            root=root,
            path=path,
            outside_error=outside_error,
        )
        if (
            len(relative.parts) != expected_length
            or tuple(
                relative.parts[: len(expected_parts)]
            )
            != expected_parts
            or relative.parts[-1] != "frame-index.json"
            or _DENSE_RUN_ID.fullmatch(relative.parts[-2]) is None
        ):
            raise DenseCandidateFrameRegistryError(
                "dense index path is invalid"
            )
        if artifact_root_binding is None:
            try:
                path_job_id = UUID(relative.parts[4])
            except (ValueError, IndexError) as error:
                raise DenseCandidateFrameRegistryError(
                    "dense index path is invalid"
                ) from error
            if (
                pipeline_job_id is not None
                and path_job_id != pipeline_job_id
            ):
                raise DenseCandidateFrameRegistryError(
                    "dense index path is invalid"
                )
        key = (
            project_id,
            pipeline_job_id,
            dense_index_sha256,
        )
        value = _RegisteredDenseIndex(
            source_sha256=source_sha256,
            path=resolved,
            root=root,
            pipeline_job_id=pipeline_job_id,
            artifact_root_binding=binding,
        )
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None and existing != value:
                raise DenseCandidateFrameRegistryError(
                    "dense index digest was rebound"
                )
            self._entries[key] = value

    def resolve(
        self,
        project_id: UUID,
        pipeline_job_id: UUID | str,
        dense_index_sha256: str,
        frame_id: Optional[str] = None,
    ) -> ResolvedDenseCandidateFrame:
        if not isinstance(project_id, UUID):
            raise ValueError("project_id must be a UUID")
        resolved_job_id: Optional[UUID]
        if frame_id is None:
            # Compatibility for bridge-internal legacy callers that registered
            # a pre-artifact-root index without a PipelineJob identity.
            frame_id = dense_index_sha256
            dense_index_sha256 = str(pipeline_job_id)
            resolved_job_id = None
        else:
            if not isinstance(pipeline_job_id, UUID):
                raise ValueError("pipeline_job_id must be a UUID")
            resolved_job_id = pipeline_job_id
        if (
            not isinstance(dense_index_sha256, str)
            or _SHA256.fullmatch(dense_index_sha256) is None
        ):
            raise DenseCandidateFrameRegistryError(
                "dense index identity is invalid"
            )
        with self._lock:
            registered = self._entries.get(
                (
                    project_id,
                    resolved_job_id,
                    dense_index_sha256,
                )
            )
        if registered is None:
            raise DenseCandidateFrameRegistryError(
                "dense index is not registered for this PipelineJob"
            )
        index_path = self._revalidate_registered_path(
            project_id,
            registered,
        )
        try:
            frame = resolve_dense_candidate_frame(
                dense_index_path=index_path,
                source_sha256=registered.source_sha256,
                dense_index_sha256=dense_index_sha256,
                frame_id=frame_id,
            )
        except DenseCandidateDiscoveryIntegrityError as error:
            raise DenseCandidateFrameRegistryError(str(error)) from error
        return ResolvedDenseCandidateFrame(
            project_id=project_id,
            dense_index_sha256=dense_index_sha256,
            frame_id=frame.frame_id,
            frames_root=frame.path.parent,
            path=frame.path,
            media_type=frame.media_type,
            size_bytes=frame.size_bytes,
            sha256=frame.sha256,
        )

    def _revalidate_registered_path(
        self,
        project_id: UUID,
        registered: _RegisteredDenseIndex,
    ) -> Path:
        binding = registered.artifact_root_binding
        if binding is not None:
            if (
                self._artifact_roots is None
                or registered.pipeline_job_id is None
            ):
                raise DenseCandidateFrameRegistryError(
                    "dense index artifact-root binding is unavailable"
                )
            try:
                authorized = (
                    self._artifact_roots.resolve_job_binding(
                        project_id,
                        binding,
                    )
                )
            except ProjectArtifactRootError as error:
                raise DenseCandidateFrameRegistryError(
                    "dense index artifact-root binding changed"
                ) from error
            if authorized.root != registered.root:
                raise DenseCandidateFrameRegistryError(
                    "dense index artifact-root binding changed"
                )
            outside_error = (
                "dense index path is outside the project artifact root"
            )
        else:
            outside_error = (
                "dense index path is outside the bridge data root"
            )
        resolved, _relative = self._validate_index_path(
            root=registered.root,
            path=registered.path,
            outside_error=outside_error,
        )
        if resolved != registered.path:
            raise DenseCandidateFrameRegistryError(
                "dense index path changed"
            )
        return resolved

    @staticmethod
    def _validate_index_path(
        *,
        root: Path,
        path: Path,
        outside_error: str,
    ) -> tuple[Path, Path]:
        try:
            resolved_root = root.resolve(strict=True)
            lexical_relative = path.relative_to(resolved_root)
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(resolved_root)
        except (OSError, ValueError) as error:
            raise DenseCandidateFrameRegistryError(
                outside_error
            ) from error
        if (
            not lexical_relative.parts
            or lexical_relative.parts != relative.parts
            or any(
                part in {"", ".", ".."}
                for part in lexical_relative.parts
            )
        ):
            raise DenseCandidateFrameRegistryError(
                "dense index path is invalid"
            )
        current = resolved_root
        if (
            root.is_symlink()
            or _is_reparse_point(root)
            or not resolved_root.is_dir()
            or not relative.parts
        ):
            raise DenseCandidateFrameRegistryError(
                "dense index path is invalid"
            )
        for index, part in enumerate(lexical_relative.parts):
            current = current / part
            try:
                metadata = current.lstat()
            except OSError as error:
                raise DenseCandidateFrameRegistryError(
                    "dense index path is invalid"
                ) from error
            if current.is_symlink() or _is_reparse_point(current):
                raise DenseCandidateFrameRegistryError(
                    "dense index path is invalid"
                )
            if index < len(relative.parts) - 1:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise DenseCandidateFrameRegistryError(
                        "dense index path is invalid"
                    )
            elif (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise DenseCandidateFrameRegistryError(
                    "dense index path is invalid"
                )
        return resolved, relative


__all__ = [
    "DenseCandidateFrameRegistry",
    "DenseCandidateFrameRegistryError",
]
