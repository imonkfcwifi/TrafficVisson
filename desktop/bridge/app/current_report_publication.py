"""Publish accepted exact reports into a registered project output root.

The renderer never supplies a filesystem path.  This service resolves the
PipelineJob's durable artifact-root binding through the server-owned registry,
stages the accepted HWPX/PDF on the destination volume, publishes with
no-overwrite semantics, and keeps a path-free, hash-verified ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID, uuid4

from .current_report_application import (
    AcceptedCurrentReportPublicationSource,
    CurrentReportApplicationError,
)
from .project_artifact_roots import (
    JOB_BINDING_SCHEMA,
    ProjectArtifactRootRegistry,
    ResolvedProjectArtifactRoot,
)


CURRENT_REPORT_PUBLICATION_SCHEMA = (
    "trafficvisson.current-report-publication.v1"
)
CURRENT_REPORT_PUBLICATION_MANIFEST_SCHEMA = (
    "trafficvisson.current-report-publication-manifest.v1"
)
_PUBLICATION_ID = re.compile(
    r"^current-report-publication-[a-f0-9]{32}$"
)
_APPLICATION_ID = re.compile(
    r"^current-report-application-[a-f0-9]{32}$"
)
_DRAFT_ID = re.compile(r"^current-report-[a-f0-9]{32}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_OUTPUT_LOCATION_ID = re.compile(
    r"^(?:project-default|project-output-[a-f0-9]{32})$"
)
_MAX_SOURCE_FILE_BYTES = 2 * 1024**3
_MAX_RECORD_BYTES = 64 * 1024
_RECORD_KEYS = {
    "schema_version",
    "state",
    "publication_id",
    "application_id",
    "project_id",
    "pipeline_job_id",
    "analysis_job_id",
    "draft_id",
    "output_location_id",
    "registry_revision",
    "root_identity_sha256",
    "marker_sha256",
    "binding_manifest_sha256",
    "exact_plan_sha256",
    "document_edit_plan_sha256",
    "accept_reject_rollback_binding_sha256",
    "output_reopen_structure_sha256",
    "raster_artifact_manifest_sha256",
    "rendered_page_count",
    "hwpx_file_name",
    "hwpx_sha256",
    "hwpx_size_bytes",
    "hwpx_file_identity_sha256",
    "pdf_file_name",
    "pdf_sha256",
    "pdf_size_bytes",
    "pdf_file_identity_sha256",
    "publication_manifest_sha256",
    "created_at",
    "published_at",
    "rolled_back_at",
    "rollback_completed",
    "record_sha256",
}


class CurrentReportPublicationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        application_id: str | None = None,
        publication_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.application_id = application_id
        self.publication_id = publication_id


class AcceptedReportSourceResolver(Protocol):
    def resolve_accepted_publication_source(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
    ) -> AcceptedCurrentReportPublicationSource: ...


@dataclass(frozen=True)
class CurrentReportPublicationMetadata:
    publication_id: str
    application_id: str
    state: str
    project_id: str
    pipeline_job_id: str
    analysis_job_id: str
    output_location_id: str
    registry_revision: int
    hwpx_file_name: str
    hwpx_sha256: str
    hwpx_size_bytes: int
    pdf_file_name: str
    pdf_sha256: str
    pdf_size_bytes: int
    publication_manifest_sha256: str
    rollback_completed: bool
    idempotent_replay: bool
    path_exposed: bool = False
    schema_version: str = CURRENT_REPORT_PUBLICATION_SCHEMA


@dataclass(frozen=True)
class _Destination:
    resolved: ResolvedProjectArtifactRoot
    category_root: Path
    job_binding: dict[str, Any]


class CurrentReportPublicationService:
    """Durable accepted-report publication and hash-identical rollback."""

    def __init__(
        self,
        *,
        owned_publication_root: Path,
        application_service: AcceptedReportSourceResolver,
        artifact_root_registry: ProjectArtifactRootRegistry,
        pipeline_job_resolver: Callable[
            [UUID, UUID], Mapping[str, Any]
        ],
        disk_usage=shutil.disk_usage,
        safety_reserve_bytes: int = 64 * 1024**2,
    ) -> None:
        self._root = _real_directory(
            Path(owned_publication_root),
            create=True,
        )
        if not hasattr(
            application_service,
            "resolve_accepted_publication_source",
        ):
            raise ValueError("accepted report source resolver is invalid")
        if not isinstance(
            artifact_root_registry,
            ProjectArtifactRootRegistry,
        ):
            raise ValueError("project artifact-root registry is invalid")
        if not callable(pipeline_job_resolver) or not callable(disk_usage):
            raise ValueError("report publication resolver is invalid")
        if (
            isinstance(safety_reserve_bytes, bool)
            or not isinstance(safety_reserve_bytes, int)
            or not 0 <= safety_reserve_bytes <= 16 * 1024**3
        ):
            raise ValueError("report publication reserve is invalid")
        self._applications = application_service
        self._artifact_roots = artifact_root_registry
        self._pipeline_job_resolver = pipeline_job_resolver
        self._disk_usage = disk_usage
        self._safety_reserve_bytes = safety_reserve_bytes
        self._lock = threading.RLock()

    def publish_accepted(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
    ) -> CurrentReportPublicationMetadata:
        self._validate_identity(
            project_id,
            pipeline_job_id,
            application_id,
        )
        source = self._resolve_source(
            project_id,
            pipeline_job_id,
            application_id,
        )
        destination = self._resolve_destination(
            project_id,
            pipeline_job_id,
        )
        expected = _expected_record(source, destination)
        record_path = self._record_path(
            project_id,
            pipeline_job_id,
            application_id,
            create=True,
        )
        with self._lock:
            resumed = record_path.exists()
            if record_path.exists():
                record = _load_record(record_path)
                _verify_record_binding(record, expected)
                if record["state"] == "rolled_back":
                    raise CurrentReportPublicationError(
                        "publication_already_rolled_back",
                        "the accepted report publication was rolled back",
                        application_id=application_id,
                        publication_id=record["publication_id"],
                    )
                if record["state"] == "published":
                    self._verify_published_files(
                        destination.category_root,
                        record,
                    )
                    return _metadata(record, replayed=True)
                if record["state"] == "rolling_back":
                    raise CurrentReportPublicationError(
                        "publication_rollback_in_progress",
                        "the accepted report publication is rolling back",
                        application_id=application_id,
                        publication_id=record["publication_id"],
                    )
            else:
                record = {
                    **expected,
                    "state": "publishing",
                    "hwpx_file_identity_sha256": None,
                    "pdf_file_identity_sha256": None,
                    "created_at": _utc_now(),
                    "published_at": None,
                    "rolled_back_at": None,
                    "rollback_completed": False,
                }
                _write_new_record(record_path, record)

            self._publish_files(
                source=source,
                destination=destination,
                record=record,
                allow_existing_final=resumed,
            )
            self._verify_published_hashes(
                destination.category_root,
                record,
            )
            # Re-validate the complete accepted 48-page evidence after the
            # potentially long destination copy and before claiming success.
            source_after_copy = self._resolve_source(
                project_id,
                pipeline_job_id,
                application_id,
            )
            if source_after_copy != source:
                raise CurrentReportPublicationError(
                    "accepted_publication_source_changed",
                    "the accepted report source changed during publication",
                    application_id=application_id,
                    publication_id=record["publication_id"],
                )
            hwpx_pair, pdf_pair = _published_file_pairs(
                destination.category_root,
                record,
            )
            hwpx_identity = _file_identity_sha256(hwpx_pair[0])
            pdf_identity = _file_identity_sha256(pdf_pair[0])
            if hwpx_identity is None or pdf_identity is None:
                raise CurrentReportPublicationError(
                    "publication_verification_failed",
                    "the published report identity is unavailable",
                    application_id=application_id,
                    publication_id=record["publication_id"],
                )
            record.update(
                {
                    "state": "published",
                    "hwpx_file_identity_sha256": hwpx_identity,
                    "pdf_file_identity_sha256": pdf_identity,
                    "published_at": record["published_at"] or _utc_now(),
                }
            )
            _replace_record(record_path, record)
            self._verify_published_files(
                destination.category_root,
                record,
            )
            return _metadata(record, replayed=False)

    def metadata(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
    ) -> CurrentReportPublicationMetadata:
        self._validate_identity(
            project_id,
            pipeline_job_id,
            application_id,
        )
        record_path = self._record_path(
            project_id,
            pipeline_job_id,
            application_id,
            create=False,
        )
        with self._lock:
            record = _load_record(record_path)
            _verify_route_binding(
                record,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                application_id=application_id,
            )
            destination = self._resolve_destination(
                project_id,
                pipeline_job_id,
            )
            _verify_destination_binding(record, destination)
            if record["state"] == "published":
                self._verify_published_files(
                    destination.category_root,
                    record,
                )
            elif record["state"] == "rolled_back":
                _require_publication_files_absent(
                    destination.category_root,
                    record,
                )
                _validate_rollback_quarantine_if_present(
                    destination.category_root,
                    record,
                )
            return _metadata(record, replayed=True)

    def rollback(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
    ) -> CurrentReportPublicationMetadata:
        self._validate_identity(
            project_id,
            pipeline_job_id,
            application_id,
        )
        record_path = self._record_path(
            project_id,
            pipeline_job_id,
            application_id,
            create=False,
        )
        with self._lock:
            record = _load_record(record_path)
            _verify_route_binding(
                record,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                application_id=application_id,
            )
            destination = self._resolve_destination(
                project_id,
                pipeline_job_id,
            )
            _verify_destination_binding(record, destination)
            if record["state"] == "rolled_back":
                _require_publication_files_absent(
                    destination.category_root,
                    record,
                )
                _cleanup_rollback_quarantine(
                    destination.category_root,
                    record,
                )
                return _metadata(record, replayed=True)
            if (
                record["state"] not in {"published", "rolling_back"}
                or record["rollback_completed"] is not False
            ):
                raise CurrentReportPublicationError(
                    "publication_rollback_state_conflict",
                    "the report publication is not a rollback candidate",
                    application_id=application_id,
                    publication_id=record["publication_id"],
                )
            source = self._resolve_source(
                project_id,
                pipeline_job_id,
                application_id,
            )
            _verify_record_binding(
                record,
                _expected_record(source, destination),
            )
            if record["state"] == "published":
                try:
                    _verify_owned_published_files(
                        destination.category_root,
                        record,
                    )
                except CurrentReportPublicationError as error:
                    raise CurrentReportPublicationError(
                        "publication_rollback_blocked",
                        "the published report changed and was retained",
                        application_id=application_id,
                        publication_id=record["publication_id"],
                    ) from error
                record["state"] = "rolling_back"
                _replace_record(record_path, record)
            _continue_owned_rollback(
                destination.category_root,
                record,
            )
            record.update(
                {
                    "state": "rolled_back",
                    "rollback_completed": True,
                    "rolled_back_at": _utc_now(),
                }
            )
            _replace_record(record_path, record)
            _require_publication_files_absent(
                destination.category_root,
                record,
            )
            _cleanup_rollback_quarantine(
                destination.category_root,
                record,
            )
            return _metadata(record, replayed=False)

    def _resolve_source(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
    ) -> AcceptedCurrentReportPublicationSource:
        try:
            source = (
                self._applications.resolve_accepted_publication_source(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    application_id=application_id,
                )
            )
        except CurrentReportApplicationError as error:
            raise CurrentReportPublicationError(
                "accepted_publication_source_unavailable",
                "the accepted report source is unavailable",
                application_id=application_id,
            ) from error
        if (
            not isinstance(
                source,
                AcceptedCurrentReportPublicationSource,
            )
            or source.project_id != project_id
            or source.pipeline_job_id != pipeline_job_id
            or source.application_id != application_id
            or source.rendered_page_count != 48
            or not _bounded_source_size(source.output_size_bytes)
            or not _bounded_source_size(source.pdf_size_bytes)
            or any(
                _SHA256.fullmatch(str(value)) is None
                for value in (
                    source.output_sha256,
                    source.pdf_sha256,
                    source.binding_manifest_sha256,
                    source.exact_plan_sha256,
                    source.document_edit_plan_sha256,
                    source.accept_reject_rollback_binding_sha256,
                    source.output_reopen_structure_sha256,
                    source.raster_artifact_manifest_sha256,
                )
            )
        ):
            raise CurrentReportPublicationError(
                "accepted_publication_source_invalid",
                "the accepted report source contract is invalid",
                application_id=application_id,
            )
        return source

    def _resolve_destination(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
    ) -> _Destination:
        try:
            job = self._pipeline_job_resolver(
                project_id,
                pipeline_job_id,
            )
            if not isinstance(job, Mapping):
                raise ValueError("pipeline job is invalid")
            if (
                UUID(str(job.get("project_id"))) != project_id
                or UUID(str(job.get("job_id"))) != pipeline_job_id
            ):
                raise ValueError("pipeline job scope changed")
            profile = job.get("profile_snapshot")
            provenance = job.get("provenance")
            binding = (
                provenance.get("artifact_root")
                if isinstance(provenance, Mapping)
                else None
            )
            if (
                not isinstance(profile, Mapping)
                or not isinstance(binding, Mapping)
                or binding.get("schema_version")
                != JOB_BINDING_SCHEMA
                or binding.get("path_exposed") is not False
                or profile.get("output_location_id")
                != binding.get("output_location_id")
            ):
                raise ValueError("pipeline artifact-root binding is invalid")
            resolved = self._artifact_roots.resolve_job_binding(
                project_id,
                binding,
            )
        except Exception as error:
            raise CurrentReportPublicationError(
                "publication_output_location_unavailable",
                "the pipeline-bound project output location is unavailable",
            ) from error
        category_root = _publication_category_root(
            resolved.root,
            pipeline_job_id,
        )
        return _Destination(
            resolved=resolved,
            category_root=category_root,
            job_binding=dict(binding),
        )

    def _publish_files(
        self,
        *,
        source: AcceptedCurrentReportPublicationSource,
        destination: _Destination,
        record: dict[str, Any],
        allow_existing_final: bool,
    ) -> None:
        final_files = _published_file_pairs(
            destination.category_root,
            record,
        )
        stage_root = (
            destination.category_root
            / (
                ".current-report-stage-"
                + record["publication_id"].removeprefix(
                    "current-report-publication-"
                )
            )
        )
        stage_sources = (
            (
                source.output_path,
                stage_root / "current-report.hwpx",
                final_files[0][0],
                record["hwpx_sha256"],
                record["hwpx_size_bytes"],
            ),
            (
                source.pdf_path,
                stage_root / "current-report.pdf",
                final_files[1][0],
                record["pdf_sha256"],
                record["pdf_size_bytes"],
            ),
        )
        if _path_entry_exists(stage_root):
            _validate_stage_root(stage_root)
            allowed = {item[1].name for item in stage_sources}
            entries = tuple(stage_root.iterdir())
            for item in entries:
                if item.name in allowed:
                    continue
                if re.fullmatch(
                    r"\.copy-[a-f0-9]{32}\.tmp",
                    item.name,
                ) is None:
                    raise CurrentReportPublicationError(
                        "publication_staging_conflict",
                        "the report publication staging area changed",
                        publication_id=record["publication_id"],
                    )
                _remove_owned_copy_temp(item)

        required_payload_bytes = 0
        for _source, staged, final, sha256, size in stage_sources:
            final_exists = _path_entry_exists(final)
            final_matches = _regular_file_matches(
                final,
                expected_sha256=sha256,
                expected_size=size,
            )
            if final_exists and not final_matches:
                raise CurrentReportPublicationError(
                    "publication_target_conflict",
                    "a different report already occupies the target name",
                    publication_id=record["publication_id"],
                )
            if final_matches:
                if not allow_existing_final:
                    raise CurrentReportPublicationError(
                        "publication_target_conflict",
                        "a pre-existing report cannot be adopted",
                        publication_id=record["publication_id"],
                    )
                continue
            staged_exists = _path_entry_exists(staged)
            staged_matches = _regular_file_matches(
                staged,
                expected_sha256=sha256,
                expected_size=size,
            )
            if staged_exists and not staged_matches:
                raise CurrentReportPublicationError(
                    "publication_staging_conflict",
                    "the report publication staging file changed",
                    publication_id=record["publication_id"],
                )
            if not staged_matches:
                required_payload_bytes += size
        required = required_payload_bytes + self._safety_reserve_bytes
        try:
            available = int(
                self._disk_usage(destination.category_root).free
            )
        except (OSError, TypeError, ValueError) as error:
            raise CurrentReportPublicationError(
                "publication_capacity_unavailable",
                "the project output capacity is unavailable",
                publication_id=record["publication_id"],
            ) from error
        if available < required:
            raise CurrentReportPublicationError(
                "publication_capacity_insufficient",
                "the project output location has insufficient capacity",
                publication_id=record["publication_id"],
            )

        if not _path_entry_exists(stage_root):
            try:
                stage_root.mkdir()
            except OSError as error:
                raise CurrentReportPublicationError(
                    "publication_staging_unavailable",
                    "the report publication staging area is unavailable",
                    publication_id=record["publication_id"],
                ) from error
            _validate_stage_root(stage_root)
        for source_path, staged, final, sha256, size in stage_sources:
            final_fingerprint = _stable_regular_file_fingerprint(final)
            if final_fingerprint is not None:
                if (
                    final_fingerprint[1] != size
                    or not hmac.compare_digest(
                        final_fingerprint[0],
                        sha256,
                    )
                ):
                    raise CurrentReportPublicationError(
                        "publication_target_conflict",
                        "a different report occupies the target name",
                        publication_id=record["publication_id"],
                    )
                if _path_entry_exists(staged):
                    _remove_hash_identical_file(staged, sha256, size)
                continue
            if _path_entry_exists(final):
                raise CurrentReportPublicationError(
                    "publication_target_conflict",
                    "the report publication target changed",
                    publication_id=record["publication_id"],
                )
            if not _path_entry_exists(staged):
                _copy_stable_source(
                    source_path,
                    staged,
                    expected_sha256=sha256,
                    expected_size=size,
                )
            elif not _regular_file_matches(
                staged,
                expected_sha256=sha256,
                expected_size=size,
            ):
                raise CurrentReportPublicationError(
                    "publication_staging_conflict",
                    "the report publication staging file changed",
                    publication_id=record["publication_id"],
                )
            _atomic_publish_no_replace(
                staged,
                final,
                expected_sha256=sha256,
                expected_size=size,
            )
        _remove_empty_stage_root(stage_root)

    @staticmethod
    def _verify_published_hashes(
        category_root: Path,
        record: Mapping[str, Any],
    ) -> None:
        if not all(
            _regular_file_matches(
                path,
                expected_sha256=sha256,
                expected_size=size,
            )
            for path, sha256, size in _published_file_pairs(
                category_root,
                record,
            )
        ):
            raise CurrentReportPublicationError(
                "published_report_changed",
                "the published report no longer matches its ledger",
                application_id=str(record["application_id"]),
                publication_id=str(record["publication_id"]),
            )

    @staticmethod
    def _verify_published_files(
        category_root: Path,
        record: Mapping[str, Any],
    ) -> None:
        _verify_owned_published_files(category_root, record)

    @staticmethod
    def _validate_identity(
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
    ) -> None:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(pipeline_job_id, UUID)
            or not isinstance(application_id, str)
            or _APPLICATION_ID.fullmatch(application_id) is None
        ):
            raise CurrentReportPublicationError(
                "publication_identity_invalid",
                "the report publication identity is invalid",
            )

    def _record_path(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
        *,
        create: bool,
    ) -> Path:
        current = self._root
        for name in (
            str(project_id),
            str(pipeline_job_id),
            application_id,
        ):
            child = current / name
            if create:
                try:
                    child.mkdir(exist_ok=True)
                except OSError as error:
                    raise CurrentReportPublicationError(
                        "publication_scope_unavailable",
                        "the report publication scope is unavailable",
                        application_id=application_id,
                    ) from error
            if not _path_entry_exists(child):
                raise CurrentReportPublicationError(
                    "publication_not_found",
                    "the report publication was not found",
                    application_id=application_id,
                )
            _require_real_directory(child)
            if child.parent != current:
                raise CurrentReportPublicationError(
                    "publication_scope_invalid",
                    "the report publication escaped its owned root",
                    application_id=application_id,
                )
            current = child
        return current / "record.json"


def _expected_record(
    source: AcceptedCurrentReportPublicationSource,
    destination: _Destination,
) -> dict[str, Any]:
    binding = destination.job_binding
    stable = {
        "application_id": source.application_id,
        "project_id": str(source.project_id),
        "pipeline_job_id": str(source.pipeline_job_id),
        "analysis_job_id": str(source.analysis_job_id),
        "draft_id": source.draft_id,
        "output_location_id": binding["output_location_id"],
        "registry_revision": binding["registry_revision"],
        "root_identity_sha256": binding["root_identity_sha256"],
        "marker_sha256": binding["marker_sha256"],
        "binding_manifest_sha256": source.binding_manifest_sha256,
        "exact_plan_sha256": source.exact_plan_sha256,
        "document_edit_plan_sha256": (
            source.document_edit_plan_sha256
        ),
        "accept_reject_rollback_binding_sha256": (
            source.accept_reject_rollback_binding_sha256
        ),
        "output_reopen_structure_sha256": (
            source.output_reopen_structure_sha256
        ),
        "raster_artifact_manifest_sha256": (
            source.raster_artifact_manifest_sha256
        ),
        "rendered_page_count": source.rendered_page_count,
        "hwpx_sha256": source.output_sha256,
        "hwpx_size_bytes": source.output_size_bytes,
        "pdf_sha256": source.pdf_sha256,
        "pdf_size_bytes": source.pdf_size_bytes,
    }
    identity_sha256 = _hash_payload(
        {
            "schema_version": CURRENT_REPORT_PUBLICATION_SCHEMA,
            **stable,
        }
    )
    publication_id = (
        "current-report-publication-" + identity_sha256[:32]
    )
    suffix = source.application_id.removeprefix(
        "current-report-application-"
    )[:16]
    hwpx_file_name = f"current-report-{suffix}.hwpx"
    pdf_file_name = f"current-report-{suffix}.pdf"
    expected = {
        "schema_version": CURRENT_REPORT_PUBLICATION_SCHEMA,
        "publication_id": publication_id,
        **stable,
        "hwpx_file_name": hwpx_file_name,
        "pdf_file_name": pdf_file_name,
    }
    expected["publication_manifest_sha256"] = _hash_payload(
        _publication_manifest_payload(expected)
    )
    return expected


def _publication_category_root(
    artifact_root: Path,
    pipeline_job_id: UUID,
) -> Path:
    root = _real_directory(Path(artifact_root), create=False)
    current = root
    for name in ("pipeline-jobs", str(pipeline_job_id), "reports"):
        child = current / name
        if not child.exists():
            try:
                child.mkdir()
            except FileExistsError:
                pass
            except OSError as error:
                raise CurrentReportPublicationError(
                    "publication_output_location_unavailable",
                    "the report publication directory is unavailable",
                ) from error
        _require_real_directory(child)
        if child.parent != current:
            raise CurrentReportPublicationError(
                "publication_output_location_invalid",
                "the report publication directory escaped its root",
            )
        current = child
    return current


def _published_file_pairs(
    category_root: Path,
    record: Mapping[str, Any],
) -> tuple[tuple[Path, str, int], tuple[Path, str, int]]:
    return (
        (
            _owned_named_file(
                category_root,
                record["hwpx_file_name"],
                ".hwpx",
            ),
            str(record["hwpx_sha256"]),
            int(record["hwpx_size_bytes"]),
        ),
        (
            _owned_named_file(
                category_root,
                record["pdf_file_name"],
                ".pdf",
            ),
            str(record["pdf_sha256"]),
            int(record["pdf_size_bytes"]),
        ),
    )


def _owned_published_file_records(
    category_root: Path,
    record: Mapping[str, Any],
) -> tuple[
    tuple[Path, str, int, str],
    tuple[Path, str, int, str],
]:
    hwpx, pdf = _published_file_pairs(category_root, record)
    return (
        (
            hwpx[0],
            hwpx[1],
            hwpx[2],
            str(record["hwpx_file_identity_sha256"]),
        ),
        (
            pdf[0],
            pdf[1],
            pdf[2],
            str(record["pdf_file_identity_sha256"]),
        ),
    )


def _verify_owned_published_files(
    category_root: Path,
    record: Mapping[str, Any],
) -> None:
    if not all(
        _regular_file_matches_owned(
            path,
            expected_sha256=sha256,
            expected_size=size,
            expected_identity_sha256=identity,
        )
        for path, sha256, size, identity
        in _owned_published_file_records(category_root, record)
    ):
        raise CurrentReportPublicationError(
            "published_report_changed",
            "the published report no longer matches its ownership ledger",
            application_id=str(record["application_id"]),
            publication_id=str(record["publication_id"]),
        )


def _rollback_quarantine_records(
    category_root: Path,
    record: Mapping[str, Any],
) -> tuple[
    tuple[Path, Path, str, int, str],
    tuple[Path, Path, str, int, str],
]:
    suffix = str(record["publication_id"]).removeprefix(
        "current-report-publication-"
    )
    owned = _owned_published_file_records(category_root, record)
    return (
        (
            owned[0][0],
            category_root
            / f".current-report-rollback-{suffix}.hwpx",
            owned[0][1],
            owned[0][2],
            owned[0][3],
        ),
        (
            owned[1][0],
            category_root / f".current-report-rollback-{suffix}.pdf",
            owned[1][1],
            owned[1][2],
            owned[1][3],
        ),
    )


def _continue_owned_rollback(
    category_root: Path,
    record: Mapping[str, Any],
) -> None:
    files = _rollback_quarantine_records(category_root, record)
    for final, quarantine, sha256, size, identity in files:
        final_exists = _path_entry_exists(final)
        quarantine_exists = _path_entry_exists(quarantine)
        final_matches = _regular_file_matches_owned(
            final,
            expected_sha256=sha256,
            expected_size=size,
            expected_identity_sha256=identity,
        )
        quarantine_matches = _regular_file_matches_owned(
            quarantine,
            expected_sha256=sha256,
            expected_size=size,
            expected_identity_sha256=identity,
        )
        if quarantine_exists:
            if not quarantine_matches:
                raise CurrentReportPublicationError(
                    "publication_rollback_blocked",
                    "the rollback quarantine changed",
                    publication_id=str(record["publication_id"]),
                )
            if final_exists:
                if not final_matches:
                    raise CurrentReportPublicationError(
                        "publication_rollback_blocked",
                        "the published report changed during rollback",
                        publication_id=str(record["publication_id"]),
                    )
                if _file_identity_sha256(final) != (
                    _file_identity_sha256(quarantine)
                ):
                    raise CurrentReportPublicationError(
                        "publication_rollback_blocked",
                        "the rollback contains two different files",
                        publication_id=str(record["publication_id"]),
                    )
                _remove_file_with_identity_if_present(
                    final,
                    expected_identity_sha256=identity,
                    expected_sha256=sha256,
                    expected_size=size,
                )
            continue
        if not final_exists or not final_matches:
            raise CurrentReportPublicationError(
                "publication_rollback_blocked",
                "the published report changed and was not removed",
                publication_id=str(record["publication_id"]),
            )

    for final, quarantine, sha256, size, identity in files:
        if _path_entry_exists(quarantine):
            continue
        _atomic_quarantine_owned_file(
            final,
            quarantine,
            expected_sha256=sha256,
            expected_size=size,
            expected_identity_sha256=identity,
        )
    if not all(
        not _path_entry_exists(final)
        and _regular_file_matches_owned(
            quarantine,
            expected_sha256=sha256,
            expected_size=size,
            expected_identity_sha256=identity,
        )
        for final, quarantine, sha256, size, identity in files
    ):
        raise CurrentReportPublicationError(
            "publication_rollback_blocked",
            "the published report rollback is incomplete",
            publication_id=str(record["publication_id"]),
        )


def _atomic_quarantine_owned_file(
    final: Path,
    quarantine: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    expected_identity_sha256: str,
) -> None:
    if _path_entry_exists(quarantine):
        raise CurrentReportPublicationError(
            "publication_rollback_blocked",
            "the rollback quarantine already exists",
        )
    if not _regular_file_matches_owned(
        final,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        expected_identity_sha256=expected_identity_sha256,
    ):
        raise CurrentReportPublicationError(
            "publication_rollback_blocked",
            "the published report changed before rollback",
        )
    try:
        if os.name == "nt":
            os.rename(final, quarantine)
        else:
            os.link(final, quarantine, follow_symlinks=False)
            if not _regular_file_matches_owned(
                quarantine,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
                expected_identity_sha256=expected_identity_sha256,
            ):
                raise OSError("rollback quarantine identity changed")
            _remove_file_with_identity_if_present(
                final,
                expected_identity_sha256=expected_identity_sha256,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
    except FileExistsError as error:
        raise CurrentReportPublicationError(
            "publication_rollback_blocked",
            "the rollback quarantine already exists",
        ) from error
    except CurrentReportPublicationError:
        raise
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_rollback_blocked",
            "the published report could not enter rollback quarantine",
        ) from error
    if not _regular_file_matches_owned(
        quarantine,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
        expected_identity_sha256=expected_identity_sha256,
    ):
        _restore_quarantine_no_replace(quarantine, final)
        raise CurrentReportPublicationError(
            "publication_rollback_blocked",
            "the published report identity changed during rollback",
        )
    _fsync_directory_if_supported(quarantine.parent)


def _restore_quarantine_no_replace(
    quarantine: Path,
    final: Path,
) -> None:
    if not _path_entry_exists(quarantine) or _path_entry_exists(final):
        return
    try:
        if os.name == "nt":
            os.rename(quarantine, final)
        else:
            identity = _file_identity_sha256(quarantine)
            if identity is None:
                return
            os.link(quarantine, final, follow_symlinks=False)
            _remove_file_with_identity_if_present(
                quarantine,
                expected_identity_sha256=identity,
            )
    except OSError:
        return


def _cleanup_rollback_quarantine(
    category_root: Path,
    record: Mapping[str, Any],
) -> None:
    for _final, quarantine, sha256, size, identity in (
        _rollback_quarantine_records(category_root, record)
    ):
        if not _path_entry_exists(quarantine):
            continue
        if not _regular_file_matches_owned(
            quarantine,
            expected_sha256=sha256,
            expected_size=size,
            expected_identity_sha256=identity,
        ):
            raise CurrentReportPublicationError(
                "publication_rollback_state_conflict",
                "the rollback quarantine changed and was retained",
                publication_id=str(record["publication_id"]),
            )
        _remove_file_with_identity_if_present(
            quarantine,
            expected_identity_sha256=identity,
            expected_sha256=sha256,
            expected_size=size,
        )


def _validate_rollback_quarantine_if_present(
    category_root: Path,
    record: Mapping[str, Any],
) -> None:
    for _final, quarantine, sha256, size, identity in (
        _rollback_quarantine_records(category_root, record)
    ):
        if _path_entry_exists(quarantine) and not (
            _regular_file_matches_owned(
                quarantine,
                expected_sha256=sha256,
                expected_size=size,
                expected_identity_sha256=identity,
            )
        ):
            raise CurrentReportPublicationError(
                "publication_rollback_state_conflict",
                "the rollback quarantine changed",
                publication_id=str(record["publication_id"]),
            )


def _owned_named_file(
    root: Path,
    file_name: Any,
    suffix: str,
) -> Path:
    if (
        not isinstance(file_name, str)
        or not 1 <= len(file_name) <= 255
        or Path(file_name).name != file_name
        or Path(file_name).suffix.casefold() != suffix
        or re.search(r"[\x00-\x1f\x7f]", file_name) is not None
    ):
        raise CurrentReportPublicationError(
            "publication_record_corrupt",
            "the report publication file name is invalid",
        )
    return root / file_name


def _copy_stable_source(
    source: Path,
    staged: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    before = _stable_regular_file_fingerprint(source)
    if (
        before is None
        or before[1] != expected_size
        or not hmac.compare_digest(before[0], expected_sha256)
    ):
        raise CurrentReportPublicationError(
            "accepted_publication_source_changed",
            "the accepted report source changed during publication",
        )
    temporary = staged.with_name(f".copy-{uuid4().hex}.tmp")
    owned_identity: str | None = None
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        created = True
        owned_identity = _identity_sha256_from_stat(
            os.fstat(descriptor)
        )
        with os.fdopen(
            descriptor,
            "wb",
        ) as output_stream, source.open("rb") as input_stream:
            while chunk := input_stream.read(1024 * 1024):
                output_stream.write(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        after = _stable_regular_file_fingerprint(source)
        if (
            after != before
            or not _regular_file_matches(
                temporary,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
            or _file_identity_sha256(temporary) != owned_identity
        ):
            raise CurrentReportPublicationError(
                "publication_staging_verification_failed",
                "the staged report failed verification",
            )
        _atomic_publish_no_replace(
            temporary,
            staged,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
    except CurrentReportPublicationError:
        raise
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_staging_unavailable",
            "the report publication could not be staged",
        ) from error
    finally:
        if (
            created
            and owned_identity is not None
            and _path_entry_exists(temporary)
        ):
            _remove_file_with_identity_if_present(
                temporary,
                expected_identity_sha256=owned_identity,
            )


def _atomic_publish_no_replace(
    staged: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_target_unavailable",
            "the report publication target is unavailable",
        ) from error
    else:
        raise CurrentReportPublicationError(
            "publication_target_conflict",
            "the report publication target already exists",
        )
    if not _regular_file_matches(
        staged,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    ):
        raise CurrentReportPublicationError(
            "publication_staging_verification_failed",
            "the staged report changed before publication",
        )
    try:
        if os.name == "nt":
            # On Windows os.rename is an atomic same-volume move and fails
            # when the destination already exists.
            os.rename(staged, destination)
        else:
            # POSIX rename may replace. A hard-link publication is exclusive
            # and atomic; the stage and destination share the same volume.
            os.link(staged, destination)
            staged.unlink()
    except FileExistsError as error:
        raise CurrentReportPublicationError(
            "publication_target_conflict",
            "the report publication target already exists",
        ) from error
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_atomic_create_failed",
            "the report could not be atomically published",
        ) from error
    if not _regular_file_matches(
        destination,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    ):
        raise CurrentReportPublicationError(
            "publication_verification_failed",
            "the published report failed SHA verification",
        )
    _fsync_directory_if_supported(destination.parent)
    if staged.parent != destination.parent:
        _fsync_directory_if_supported(staged.parent)


def _remove_hash_identical_file(
    path: Path,
    sha256: str,
    size: int,
) -> None:
    if not _regular_file_matches(
        path,
        expected_sha256=sha256,
        expected_size=size,
    ):
        raise CurrentReportPublicationError(
            "publication_staging_conflict",
            "the report staging file changed",
        )
    try:
        path.unlink()
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_staging_unavailable",
            "the report staging file could not be removed",
        ) from error


def _remove_empty_stage_root(stage_root: Path) -> None:
    _validate_stage_root(stage_root)
    if tuple(stage_root.iterdir()):
        raise CurrentReportPublicationError(
            "publication_staging_conflict",
            "the report staging area contains unexpected files",
        )
    try:
        stage_root.rmdir()
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_staging_unavailable",
            "the report staging area could not be removed",
        ) from error
    _fsync_directory_if_supported(stage_root.parent)


def _validate_stage_root(path: Path) -> None:
    _require_real_directory(path)
    if not path.name.startswith(".current-report-stage-"):
        raise CurrentReportPublicationError(
            "publication_staging_conflict",
            "the report staging identity is invalid",
        )


def _require_publication_files_absent(
    category_root: Path,
    record: Mapping[str, Any],
) -> None:
    for path, _sha256, _size in _published_file_pairs(
        category_root,
        record,
    ):
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise CurrentReportPublicationError(
                "publication_rollback_state_conflict",
                "the rolled-back report state is unavailable",
            ) from error
        raise CurrentReportPublicationError(
            "publication_rollback_state_conflict",
            "a rolled-back report file still exists",
        )


def _verify_destination_binding(
    record: Mapping[str, Any],
    destination: _Destination,
) -> None:
    for field in (
        "output_location_id",
        "registry_revision",
        "root_identity_sha256",
        "marker_sha256",
    ):
        if record.get(field) != destination.job_binding.get(field):
            raise CurrentReportPublicationError(
                "publication_output_location_changed",
                "the project output location changed after publication",
                application_id=str(record.get("application_id", "")),
                publication_id=str(record.get("publication_id", "")),
            )


def _verify_route_binding(
    record: Mapping[str, Any],
    *,
    project_id: UUID,
    pipeline_job_id: UUID,
    application_id: str,
) -> None:
    if (
        record.get("project_id") != str(project_id)
        or record.get("pipeline_job_id") != str(pipeline_job_id)
        or record.get("application_id") != application_id
    ):
        raise CurrentReportPublicationError(
            "publication_scope_invalid",
            "the report publication ledger has a different scope",
            application_id=application_id,
            publication_id=str(record.get("publication_id", "")),
        )


def _verify_record_binding(
    record: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    for key, value in expected.items():
        if record.get(key) != value:
            raise CurrentReportPublicationError(
                "publication_binding_conflict",
                "the report publication is bound to different evidence",
                application_id=str(expected["application_id"]),
                publication_id=str(expected["publication_id"]),
            )


def _metadata(
    record: Mapping[str, Any],
    *,
    replayed: bool,
) -> CurrentReportPublicationMetadata:
    return CurrentReportPublicationMetadata(
        publication_id=str(record["publication_id"]),
        application_id=str(record["application_id"]),
        state=str(record["state"]),
        project_id=str(record["project_id"]),
        pipeline_job_id=str(record["pipeline_job_id"]),
        analysis_job_id=str(record["analysis_job_id"]),
        output_location_id=str(record["output_location_id"]),
        registry_revision=int(record["registry_revision"]),
        hwpx_file_name=str(record["hwpx_file_name"]),
        hwpx_sha256=str(record["hwpx_sha256"]),
        hwpx_size_bytes=int(record["hwpx_size_bytes"]),
        pdf_file_name=str(record["pdf_file_name"]),
        pdf_sha256=str(record["pdf_sha256"]),
        pdf_size_bytes=int(record["pdf_size_bytes"]),
        publication_manifest_sha256=str(
            record["publication_manifest_sha256"]
        ),
        rollback_completed=bool(record["rollback_completed"]),
        idempotent_replay=replayed,
    )


def _load_record(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if not 1 <= len(raw) <= _MAX_RECORD_BYTES:
            raise ValueError("record size is invalid")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise CurrentReportPublicationError(
            "publication_record_corrupt",
            "the report publication ledger is unavailable",
        ) from error
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise CurrentReportPublicationError(
            "publication_record_corrupt",
            "the report publication ledger is invalid",
        )
    supplied = value.pop("record_sha256")
    calculated = _hash_payload(value)
    value["record_sha256"] = supplied
    expected_hwpx_name = (
        "current-report-"
        + str(value.get("application_id", "")).removeprefix(
            "current-report-application-"
        )[:16]
        + ".hwpx"
    )
    expected_pdf_name = (
        "current-report-"
        + str(value.get("application_id", "")).removeprefix(
            "current-report-application-"
        )[:16]
        + ".pdf"
    )
    expected_identity_fields = {
        key: value.get(key)
        for key in (
            "application_id",
            "project_id",
            "pipeline_job_id",
            "analysis_job_id",
            "draft_id",
            "output_location_id",
            "registry_revision",
            "root_identity_sha256",
            "marker_sha256",
            "binding_manifest_sha256",
            "exact_plan_sha256",
            "document_edit_plan_sha256",
            "accept_reject_rollback_binding_sha256",
            "output_reopen_structure_sha256",
            "raster_artifact_manifest_sha256",
            "rendered_page_count",
            "hwpx_sha256",
            "hwpx_size_bytes",
            "pdf_sha256",
            "pdf_size_bytes",
        )
    }
    expected_publication_id = (
        "current-report-publication-"
        + _hash_payload(
            {
                "schema_version": CURRENT_REPORT_PUBLICATION_SCHEMA,
                **expected_identity_fields,
            }
        )[:32]
    )
    if (
        value.get("schema_version")
        != CURRENT_REPORT_PUBLICATION_SCHEMA
        or value.get("state")
        not in {
            "publishing",
            "published",
            "rolling_back",
            "rolled_back",
        }
        or not isinstance(supplied, str)
        or not hmac.compare_digest(supplied, calculated)
        or _PUBLICATION_ID.fullmatch(
            str(value.get("publication_id"))
        )
        is None
        or _APPLICATION_ID.fullmatch(
            str(value.get("application_id"))
        )
        is None
        or _DRAFT_ID.fullmatch(str(value.get("draft_id"))) is None
        or any(
            not _uuid_text(value.get(field))
            for field in (
                "project_id",
                "pipeline_job_id",
                "analysis_job_id",
            )
        )
        or _OUTPUT_LOCATION_ID.fullmatch(
            str(value.get("output_location_id"))
        )
        is None
        or not _positive_int(value.get("registry_revision"))
        or value.get("rendered_page_count") != 48
        or any(
            _SHA256.fullmatch(str(value.get(field))) is None
            for field in (
                "root_identity_sha256",
                "marker_sha256",
                "binding_manifest_sha256",
                "exact_plan_sha256",
                "document_edit_plan_sha256",
                "accept_reject_rollback_binding_sha256",
                "output_reopen_structure_sha256",
                "raster_artifact_manifest_sha256",
                "hwpx_sha256",
                "pdf_sha256",
                "publication_manifest_sha256",
            )
        )
        or not _bounded_source_size(value.get("hwpx_size_bytes"))
        or not _bounded_source_size(value.get("pdf_size_bytes"))
        or value.get("hwpx_file_name") != expected_hwpx_name
        or value.get("pdf_file_name") != expected_pdf_name
        or value.get("publication_id") != expected_publication_id
        or value.get("publication_manifest_sha256")
        != _hash_payload(_publication_manifest_payload(value))
        or not _valid_timestamp(value.get("created_at"))
        or (
            value["state"] == "publishing"
            and (
                value.get("published_at") is not None
                or value.get("rolled_back_at") is not None
                or value.get("hwpx_file_identity_sha256")
                is not None
                or value.get("pdf_file_identity_sha256")
                is not None
            )
        )
        or (
            value["state"] in {"published", "rolling_back"}
            and (
                not _valid_timestamp(value.get("published_at"))
                or value.get("rolled_back_at") is not None
                or _SHA256.fullmatch(
                    str(value.get("hwpx_file_identity_sha256"))
                )
                is None
                or _SHA256.fullmatch(
                    str(value.get("pdf_file_identity_sha256"))
                )
                is None
            )
        )
        or (
            value["state"] == "rolled_back"
            and (
                not _valid_timestamp(value.get("published_at"))
                or not _valid_timestamp(value.get("rolled_back_at"))
                or _SHA256.fullmatch(
                    str(value.get("hwpx_file_identity_sha256"))
                )
                is None
                or _SHA256.fullmatch(
                    str(value.get("pdf_file_identity_sha256"))
                )
                is None
            )
        )
        or not isinstance(value.get("rollback_completed"), bool)
        or (
            value["state"] == "rolled_back"
            and value["rollback_completed"] is not True
        )
        or (
            value["state"] != "rolled_back"
            and value["rollback_completed"] is not False
        )
    ):
        raise CurrentReportPublicationError(
            "publication_record_corrupt",
            "the report publication ledger failed verification",
        )
    return value


def _publication_manifest_payload(
    record: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": CURRENT_REPORT_PUBLICATION_MANIFEST_SCHEMA,
        "publication_id": record.get("publication_id"),
        **{
            key: record.get(key)
            for key in (
                "application_id",
                "project_id",
                "pipeline_job_id",
                "analysis_job_id",
                "draft_id",
                "output_location_id",
                "registry_revision",
                "root_identity_sha256",
                "marker_sha256",
                "binding_manifest_sha256",
                "exact_plan_sha256",
                "document_edit_plan_sha256",
                "accept_reject_rollback_binding_sha256",
                "output_reopen_structure_sha256",
                "raster_artifact_manifest_sha256",
                "rendered_page_count",
                "hwpx_sha256",
                "hwpx_size_bytes",
                "pdf_sha256",
                "pdf_size_bytes",
                "hwpx_file_name",
                "pdf_file_name",
            )
        },
    }


def _write_new_record(path: Path, record: Mapping[str, Any]) -> None:
    payload = _record_bytes(record)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory_if_supported(path.parent)
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_ledger_unavailable",
            "the report publication ledger could not be created",
        ) from error


def _replace_record(path: Path, record: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    payload = _record_bytes(record)
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory_if_supported(path.parent)
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_ledger_unavailable",
            "the report publication ledger could not be updated",
        ) from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _record_bytes(record: Mapping[str, Any]) -> bytes:
    value = {
        key: item
        for key, item in record.items()
        if key != "record_sha256"
    }
    payload = _canonical_json(
        {
            **value,
            "record_sha256": _hash_payload(value),
        }
    )
    if len(payload) > _MAX_RECORD_BYTES:
        raise CurrentReportPublicationError(
            "publication_record_too_large",
            "the report publication ledger exceeds its bound",
        )
    return payload


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_source_size(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= _MAX_SOURCE_FILE_BYTES
    )


def _positive_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= 2**31 - 1
    )


def _uuid_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() == timezone.utc.utcoffset(parsed)
    )


def _path_entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_path_unavailable",
            "a report publication path is unavailable",
        ) from error


def _identity_sha256_from_stat(value: os.stat_result) -> str:
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("file identity requires a regular file")
    return _hash_payload(
        {
            "device": int(value.st_dev),
            "inode": int(value.st_ino),
            "file_type": int(stat.S_IFMT(value.st_mode)),
        }
    )


def _file_identity_sha256(path: Path) -> str | None:
    try:
        value = os.lstat(path)
    except OSError:
        return None
    if (
        not stat.S_ISREG(value.st_mode)
        or path.is_symlink()
        or _is_reparse(value)
    ):
        return None
    return _identity_sha256_from_stat(value)


def _regular_file_matches_owned(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    expected_identity_sha256: str,
) -> bool:
    return (
        _regular_file_matches(
            path,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
        and hmac.compare_digest(
            _file_identity_sha256(path) or "",
            expected_identity_sha256,
        )
    )


def _remove_file_with_identity_if_present(
    path: Path,
    *,
    expected_identity_sha256: str,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
) -> None:
    if not _path_entry_exists(path):
        return
    identity = _file_identity_sha256(path)
    if (
        identity is None
        or not hmac.compare_digest(
            identity,
            expected_identity_sha256,
        )
        or (
            expected_sha256 is not None
            and expected_size is not None
            and not _regular_file_matches(
                path,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
        )
    ):
        raise CurrentReportPublicationError(
            "publication_owned_file_changed",
            "an owned publication file changed and was retained",
        )
    try:
        path.unlink()
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_owned_file_unavailable",
            "an owned publication file could not be removed",
        ) from error
    _fsync_directory_if_supported(path.parent)


def _remove_owned_copy_temp(path: Path) -> None:
    if re.fullmatch(
        r"\.copy-[a-f0-9]{32}\.tmp",
        path.name,
    ) is None:
        raise CurrentReportPublicationError(
            "publication_staging_conflict",
            "the report staging temporary name is invalid",
        )
    identity = _file_identity_sha256(path)
    if identity is None:
        raise CurrentReportPublicationError(
            "publication_staging_conflict",
            "the report staging temporary entry is unsafe",
        )
    _remove_file_with_identity_if_present(
        path,
        expected_identity_sha256=identity,
    )


def _fsync_directory_if_supported(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_directory_sync_failed",
            "the report publication directory could not be synchronized",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _stable_regular_file_fingerprint(
    path: Path,
) -> tuple[str, int] | None:
    try:
        before_value = os.lstat(path)
        before = (
            int(before_value.st_dev),
            int(before_value.st_ino),
            int(before_value.st_size),
            int(before_value.st_mtime_ns),
        )
        if (
            not stat.S_ISREG(before_value.st_mode)
            or path.is_symlink()
            or _is_reparse(before_value)
        ):
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        after_value = os.lstat(path)
        after = (
            int(after_value.st_dev),
            int(after_value.st_ino),
            int(after_value.st_size),
            int(after_value.st_mtime_ns),
        )
        if before != after:
            return None
        return digest.hexdigest(), before[2]
    except OSError:
        return None


def _regular_file_matches(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> bool:
    value = _stable_regular_file_fingerprint(path)
    return (
        value is not None
        and value[1] == expected_size
        and hmac.compare_digest(value[0], expected_sha256)
    )


def _real_directory(path: Path, *, create: bool) -> Path:
    if not path.is_absolute():
        raise ValueError("report publication root must be absolute")
    absolute = Path(os.path.abspath(os.fspath(path)))
    if create:
        absolute.mkdir(parents=True, exist_ok=True)
    _require_real_directory(absolute)
    return absolute


def _require_real_directory(path: Path) -> None:
    try:
        value = os.lstat(path)
    except OSError as error:
        raise CurrentReportPublicationError(
            "publication_directory_unavailable",
            "the report publication directory is unavailable",
        ) from error
    if (
        not stat.S_ISDIR(value.st_mode)
        or path.is_symlink()
        or _is_reparse(value)
    ):
        raise CurrentReportPublicationError(
            "publication_directory_invalid",
            "the report publication directory is not a real directory",
        )


def _is_reparse(value: os.stat_result) -> bool:
    return bool(
        getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


__all__ = [
    "CURRENT_REPORT_PUBLICATION_SCHEMA",
    "CurrentReportPublicationError",
    "CurrentReportPublicationMetadata",
    "CurrentReportPublicationService",
]
