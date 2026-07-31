"""Durable, server-owned exact HWPX draft preparation.

This module is the final deterministic *preview* boundary after the complete
user-approved current-Incident set has already been bound to an audited 3Q/4Q
sample.  It:

* compiles the server-owned binding into one exact-writer plan;
* writes a new, content-addressed HWPX below a project/job-owned root;
* independently reopens the result and binds the 48-page render expectations;
* derives a path-free, exact-address ``DocumentEditPlan`` before writing;
* persists the binding, exact plan, review plan, writer result, and render
  binding in one hash-bound JSON record; and
* returns path-free metadata to callers while providing a typed internal load
  API for the later one-shot approval boundary.

It never calls a language-model Provider, HWP COM, render/PDF validation, or a
publication API.  A successful draft therefore means only
``apply_approval_required``.  It is not an end-to-end completion claim.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import threading
import zipfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol
from uuid import UUID, uuid4

from .current_incident_hwpx_plan import (
    ServerOwnedEvidenceFile,
    build_exact_hwpx_write_plan,
)
from .current_incident_report_binder import (
    CurrentIncidentReportBindingManifest,
    current_incident_report_binding_from_dict,
)
from .current_report_document_edit_plan import (
    CurrentReportDocumentEditPlan,
    CurrentReportDocumentEditPlanError,
    build_current_report_document_edit_plan,
    validate_current_report_document_edit_plan,
)
from .current_report_render_binding import build_report_render_binding
from .hwp_render_validation import (
    ExpectedPagePairSlot,
    ReportBindingManifest,
)
from .hwpx_exact_writer import (
    HWPX_EXACT_WRITER_SCHEMA,
    ExactHwpxOperation,
    ExactHwpxParagraphTextOperation,
    ExactHwpxPictureOperation,
    ExactHwpxTargetVerification,
    ExactHwpxTextOperation,
    ExactHwpxWritePlan,
    ExactHwpxWriteResult,
    write_exact_hwpx,
)
from .hwpx_sample_layout import (
    EXPECTED_RENDERED_PAGE_COUNT,
    EXPECTED_TABLE_COUNT,
)
from .hwpx_stale_factual_targets import XmlParagraphAddress
from .hwpx_structure import inspect_hwpx_structure


CURRENT_REPORT_DRAFT_SCHEMA = "trafficvisson.current-report-draft.v1"
CURRENT_REPORT_DRAFT_STATE = (
    "exact_current_report_draft_ready_apply_approval_required"
)
MAX_CURRENT_REPORT_DRAFT_RECORD_BYTES = 64 * 1024 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_DRAFT_ID = re.compile(r"^current-report-[a-f0-9]{32}$")
_SAFE_RELATIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_ARTIFACT_CATEGORY = "reports"
_REQUEST_KEYS = {
    "schema_version",
    "project_id",
    "pipeline_job_id",
    "analysis_job_id",
    "source_document_sha256",
    "source_target_summary_sha256",
    "binding_manifest_sha256",
    "binding_payload_sha256",
}
_RECORD_KEYS = {
    "schema_version",
    "state",
    "draft_id",
    "project_id",
    "pipeline_job_id",
    "analysis_job_id",
    "request_sha256",
    "request",
    "artifact_category",
    "artifact_relative_root",
    "category_root_identity_sha256",
    "source_path",
    "source_document_sha256",
    "transcode_root",
    "binding_manifest",
    "binding_manifest_sha256",
    "binding_payload_sha256",
    "exact_plan",
    "exact_plan_sha256",
    "document_edit_plan",
    "document_edit_plan_sha256",
    "accept_reject_rollback_binding_sha256",
    "writer_result",
    "output_file_name",
    "output_document_sha256",
    "output_size_bytes",
    "render_binding",
    "render_binding_sha256",
    "writer_invocation_count",
    "provider_invocation_count",
    "com_invocation_count",
    "exact_writer_completed",
    "render_validation_completed",
    "apply_approval_required",
    "publication_completed",
    "full_e2e_completed",
    "record_sha256",
}


class CurrentReportDraftError(RuntimeError):
    """A caller-safe, apply-blocking exact-draft failure."""

    provider_called = False
    com_invoked = False
    render_validation_completed = False
    publication_completed = False
    full_e2e_completed = False
    apply_blocked = True

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CurrentReportDraftConflict(CurrentReportDraftError):
    """A content address already belongs to a different immutable request."""


class CurrentReportDraftCorrupt(CurrentReportDraftError):
    """A persisted draft record or output failed integrity verification."""


class CurrentReportDraftNotFound(CurrentReportDraftError):
    """No exact draft exists at the requested project/job address."""


@dataclass(frozen=True)
class ReopenedHwpxVerification:
    """Independent output facts collected after the exact writer returns."""

    document_sha256: str
    table_count: int
    mimetype_first_and_stored: bool


class ExactPlanBuilder(Protocol):
    def __call__(
        self,
        *,
        source_path: Path,
        binding_manifest: CurrentIncidentReportBindingManifest,
        evidence_by_ref: Mapping[str, ServerOwnedEvidenceFile],
        server_evidence_root: Path,
        transcode_root: Path,
    ) -> ExactHwpxWritePlan: ...


class ExactWriter(Protocol):
    def __call__(
        self,
        source_path: Path,
        output_path: Path,
        plan: ExactHwpxWritePlan,
    ) -> ExactHwpxWriteResult: ...


class DocumentEditPlanBuilder(Protocol):
    def __call__(
        self,
        *,
        source_path: Path,
        binding_manifest: CurrentIncidentReportBindingManifest,
        exact_plan: ExactHwpxWritePlan,
        staged_evidence_root: Path,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        source_sha256: str,
    ) -> CurrentReportDocumentEditPlan: ...


class ReopenVerifier(Protocol):
    def __call__(self, path: Path) -> ReopenedHwpxVerification: ...


class ReportEvidenceResolver(Protocol):
    def __call__(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        selected_ref: str,
        expected_sha256: str,
    ) -> ServerOwnedEvidenceFile: ...


class ReportArtifactRootResolver(Protocol):
    def __call__(
        self,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
    ) -> Path: ...


class AuditedTemplateSourceResolver(Protocol):
    def __call__(
        self,
        template_id: str,
        template_sha256: str,
    ) -> Path: ...


@dataclass(frozen=True)
class CurrentReportDraftMetadata:
    """Path-free status safe for bridge/API metadata responses."""

    draft_id: str
    state: str
    project_id: str
    pipeline_job_id: str
    analysis_job_id: str
    approved_incident_count: int
    binding_manifest_sha256: str
    exact_plan_sha256: str
    document_edit_plan_sha256: str
    accept_reject_rollback_binding_sha256: str
    document_edit_plan: dict[str, Any]
    output_document_sha256: str
    render_binding_sha256: str
    record_sha256: str
    operation_count: int
    expected_rendered_page_count: int
    idempotent_replay: bool
    provider_called: bool = False
    com_invoked: bool = False
    exact_writer_completed: bool = True
    render_validation_completed: bool = False
    apply_approval_required: bool = True
    publication_completed: bool = False
    full_e2e_completed: bool = False
    path_exposed: bool = False
    schema_version: str = CURRENT_REPORT_DRAFT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CurrentReportDraft:
    """Trusted internal record consumed by the later approval API."""

    draft_id: str
    project_id: UUID
    pipeline_job_id: UUID
    analysis_job_id: UUID
    source_path: Path
    output_path: Path
    transcode_root: Path
    request_sha256: str
    binding_manifest: CurrentIncidentReportBindingManifest
    exact_plan: ExactHwpxWritePlan
    document_edit_plan: CurrentReportDocumentEditPlan
    writer_result: ExactHwpxWriteResult
    render_binding: ReportBindingManifest
    exact_plan_sha256: str
    render_binding_sha256: str
    record_sha256: str
    record_path: Path

    def public_metadata(
        self, *, idempotent_replay: bool = True
    ) -> CurrentReportDraftMetadata:
        return CurrentReportDraftMetadata(
            draft_id=self.draft_id,
            state=CURRENT_REPORT_DRAFT_STATE,
            project_id=str(self.project_id),
            pipeline_job_id=str(self.pipeline_job_id),
            analysis_job_id=str(self.analysis_job_id),
            approved_incident_count=(
                self.binding_manifest.approved_incident_count
            ),
            binding_manifest_sha256=(
                self.binding_manifest.binding_manifest_sha256
            ),
            exact_plan_sha256=self.exact_plan_sha256,
            document_edit_plan_sha256=(
                self.document_edit_plan.document_edit_plan_sha256
            ),
            accept_reject_rollback_binding_sha256=(
                self.document_edit_plan
                .accept_reject_rollback_binding_sha256
            ),
            document_edit_plan=self.document_edit_plan.to_dict(),
            output_document_sha256=(
                self.writer_result.output_document_sha256
            ),
            render_binding_sha256=self.render_binding_sha256,
            record_sha256=self.record_sha256,
            operation_count=len(self.exact_plan.operations),
            expected_rendered_page_count=(
                self.writer_result.expected_rendered_page_count
                or EXPECTED_RENDERED_PAGE_COUNT
            ),
            idempotent_replay=idempotent_replay,
        )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _plan_sha256(plan: ExactHwpxWritePlan) -> str:
    return _canonical_sha256(asdict(plan))


def _render_binding_sha256(binding: ReportBindingManifest) -> str:
    return _canonical_sha256(asdict(binding))


def _is_reparse(stat_result: os.stat_result) -> bool:
    return bool(
        getattr(stat_result, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _real_directory(
    path: Path,
    *,
    label: str,
    create: bool,
) -> Path:
    if not path.is_absolute() or "\x00" in os.fspath(path):
        raise CurrentReportDraftError(
            "unsafe_directory", f"{label} must be an absolute directory"
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    if create:
        try:
            absolute.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CurrentReportDraftError(
                "directory_unavailable", f"{label} is unavailable"
            ) from error
    try:
        details = os.stat(absolute, follow_symlinks=False)
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise CurrentReportDraftError(
            "directory_unavailable", f"{label} is unavailable"
        ) from error
    if (
        not stat.S_ISDIR(details.st_mode)
        or absolute.is_symlink()
        or _is_reparse(details)
        or not _same_path(absolute, resolved)
    ):
        raise CurrentReportDraftError(
            "unsafe_directory", f"{label} must be a real directory"
        )
    return absolute


def _directory_identity_sha256(path: Path) -> str:
    directory = _real_directory(
        Path(path),
        label="report artifact category",
        create=False,
    )
    try:
        value = os.stat(directory, follow_symlinks=False)
    except OSError as error:
        raise CurrentReportDraftError(
            "draft_artifact_root_unavailable",
            "the report artifact category identity is unavailable",
        ) from error
    return _canonical_sha256(
        {
            "device": int(value.st_dev),
            "inode": int(value.st_ino),
            "category": _ARTIFACT_CATEGORY,
        }
    )


def _relative_artifact_directory(
    category_root: Path,
    relative: str,
    *,
    create: bool,
    label: str,
) -> Path:
    parts = relative.split("/") if isinstance(relative, str) else []
    if (
        not parts
        or len(parts) > 8
        or any(
            _SAFE_RELATIVE_NAME.fullmatch(part) is None
            for part in parts
        )
    ):
        raise CurrentReportDraftCorrupt(
            "draft_artifact_locator_invalid",
            "the persisted exact draft artifact locator is invalid",
        )
    current = category_root
    for part in parts:
        child = current / part
        if create:
            try:
                child.mkdir(exist_ok=True)
            except OSError as error:
                raise CurrentReportDraftError(
                    "draft_artifact_root_unavailable",
                    f"{label} is unavailable",
                ) from error
        current = _real_directory(
            child,
            label=label,
            create=False,
        )
    try:
        current.relative_to(category_root)
    except ValueError as error:
        raise CurrentReportDraftCorrupt(
            "draft_artifact_locator_invalid",
            "the persisted exact draft artifact locator escaped its category",
        ) from error
    return current


def _plan_locator_dict(
    plan: ExactHwpxWritePlan,
    *,
    transcode_root: Path,
) -> dict[str, Any]:
    payload = asdict(plan)
    operations = payload.get("operations")
    if not isinstance(operations, tuple) and not isinstance(
        operations, list
    ):
        raise CurrentReportDraftError(
            "exact_plan_operation_invalid",
            "the exact plan cannot be persisted",
        )
    sanitized: list[dict[str, Any]] = []
    for raw in operations:
        operation = dict(raw)
        if operation.get("kind") == "replace_picture":
            evidence_path = _owned_path(
                Path(str(operation.get("evidence_path", ""))),
                root=transcode_root,
                label="transcoded exact-plan evidence",
            )
            relative = evidence_path.relative_to(transcode_root)
            if (
                len(relative.parts) != 1
                or _SAFE_RELATIVE_NAME.fullmatch(relative.name) is None
            ):
                raise CurrentReportDraftError(
                    "exact_plan_evidence_invalid",
                    "the exact-plan evidence locator is invalid",
                )
            operation["evidence_path"] = relative.name
        sanitized.append(operation)
    payload["operations"] = sanitized
    return payload


def _writer_result_locator_dict(
    result: ExactHwpxWriteResult,
    *,
    output_path: Path,
) -> dict[str, Any]:
    if not _same_path(Path(result.output_path), output_path):
        raise CurrentReportDraftError(
            "exact_writer_result_invalid",
            "the exact writer result cannot be persisted",
        )
    payload = result.to_dict()
    payload["output_path"] = output_path.name
    return payload


@dataclass(frozen=True)
class _FileFingerprint:
    size: int
    modified_ns: int
    device: int
    inode: int
    sha256: str


def _fingerprint_regular_file(
    path: Path,
    *,
    label: str,
    suffix: str | None = None,
) -> _FileFingerprint:
    if (
        not path.is_absolute()
        or "\x00" in os.fspath(path)
        or (suffix is not None and path.suffix.casefold() != suffix)
    ):
        raise CurrentReportDraftError(
            "unsafe_file", f"{label} path is invalid"
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        before = os.stat(absolute, follow_symlinks=False)
    except OSError as error:
        raise CurrentReportDraftError(
            "file_unavailable", f"{label} is unavailable"
        ) from error
    if (
        not stat.S_ISREG(before.st_mode)
        or absolute.is_symlink()
        or _is_reparse(before)
    ):
        raise CurrentReportDraftError(
            "unsafe_file", f"{label} must be a regular non-link file"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    try:
        descriptor = os.open(str(absolute), flags)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            opened = os.fstat(stream.fileno())
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size != before.st_size
            ):
                raise CurrentReportDraftError(
                    "file_changed", f"{label} changed while opening"
                )
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except CurrentReportDraftError:
        raise
    except OSError as error:
        raise CurrentReportDraftError(
            "file_unavailable", f"{label} could not be read"
        ) from error
    if (
        after.st_dev != opened.st_dev
        or after.st_ino != opened.st_ino
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
    ):
        raise CurrentReportDraftError(
            "file_changed", f"{label} changed while hashing"
        )
    return _FileFingerprint(
        size=after.st_size,
        modified_ns=after.st_mtime_ns,
        device=after.st_dev,
        inode=after.st_ino,
        sha256=digest.hexdigest(),
    )


def _owned_path(path: Path, *, root: Path, label: str) -> Path:
    if not path.is_absolute() or "\x00" in os.fspath(path):
        raise CurrentReportDraftError(
            "path_escape", f"{label} path is invalid"
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        absolute.relative_to(root)
    except ValueError as error:
        raise CurrentReportDraftError(
            "path_escape", f"{label} escaped its server-owned root"
        ) from error
    parent = absolute.parent
    try:
        parent_details = os.stat(parent, follow_symlinks=False)
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise CurrentReportDraftError(
            "directory_unavailable", f"{label} parent is unavailable"
        ) from error
    if (
        not stat.S_ISDIR(parent_details.st_mode)
        or parent.is_symlink()
        or _is_reparse(parent_details)
        or not _same_path(parent, resolved_parent)
    ):
        raise CurrentReportDraftError(
            "path_escape", f"{label} parent is not server-owned"
        )
    return absolute


def _default_reopen_verifier(path: Path) -> ReopenedHwpxVerification:
    snapshot = inspect_hwpx_structure(path)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            mimetype_ok = bool(
                infos
                and infos[0].filename == "mimetype"
                and infos[0].compress_type == zipfile.ZIP_STORED
            )
    except (OSError, zipfile.BadZipFile) as error:
        raise CurrentReportDraftError(
            "output_reopen_failed",
            "the exact HWPX draft could not be reopened",
        ) from error
    return ReopenedHwpxVerification(
        document_sha256=snapshot.document_sha256,
        table_count=len(snapshot.tables),
        mimetype_first_and_stored=mimetype_ok,
    )


def _write_atomic_new(path: Path, payload: bytes) -> None:
    if not 1 <= len(payload) <= MAX_CURRENT_REPORT_DRAFT_RECORD_BYTES:
        raise CurrentReportDraftError(
            "record_capacity_exceeded",
            "the exact draft record exceeds its bounded size",
        )
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise CurrentReportDraftConflict(
                "draft_record_conflict",
                "an exact draft record already exists",
            ) from error
        except OSError as error:
            # NTFS and ordinary local POSIX filesystems support hard links.
            # Fail closed instead of replacing a possibly concurrent record.
            raise CurrentReportDraftError(
                "record_persist_failed",
                "the exact draft record could not be committed atomically",
            ) from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class CurrentReportDraftService:
    """Prepare and durably load exact current-report HWPX drafts."""

    def __init__(
        self,
        *,
        owned_draft_root: Path,
        plan_builder: ExactPlanBuilder = build_exact_hwpx_write_plan,
        document_edit_plan_builder: DocumentEditPlanBuilder = (
            build_current_report_document_edit_plan
        ),
        writer: ExactWriter = write_exact_hwpx,
        render_binding_builder: Callable[
            [CurrentIncidentReportBindingManifest], ReportBindingManifest
        ] = build_report_render_binding,
        reopen_verifier: ReopenVerifier = _default_reopen_verifier,
        report_evidence_resolver: Optional[
            ReportEvidenceResolver
        ] = None,
        server_evidence_root: Optional[Path] = None,
        artifact_root_resolver: Optional[
            ReportArtifactRootResolver
        ] = None,
        template_source_resolver: Optional[
            AuditedTemplateSourceResolver
        ] = None,
    ) -> None:
        self._root = _real_directory(
            Path(owned_draft_root),
            label="current-report draft root",
            create=True,
        )
        self._plan_builder = plan_builder
        self._document_edit_plan_builder = (
            document_edit_plan_builder
        )
        self._writer = writer
        self._render_binding_builder = render_binding_builder
        self._reopen_verifier = reopen_verifier
        if (report_evidence_resolver is None) != (
            server_evidence_root is None
        ):
            raise ValueError(
                "report evidence resolver and root must be configured together"
            )
        self._report_evidence_resolver = report_evidence_resolver
        self._server_evidence_root = (
            None
            if server_evidence_root is None
            else _real_directory(
                Path(server_evidence_root),
                label="server evidence root",
                create=False,
            )
        )
        if (
            artifact_root_resolver is not None
            and not callable(artifact_root_resolver)
        ):
            raise TypeError("artifact_root_resolver must be callable")
        if (
            template_source_resolver is not None
            and not callable(template_source_resolver)
        ):
            raise TypeError("template_source_resolver must be callable")
        self._artifact_root_resolver = artifact_root_resolver
        self._template_source_resolver = template_source_resolver
        self._source_path_cache: dict[tuple[str, str], Path] = {}
        self._lock = threading.RLock()

    def prepare(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        source_path: Path,
        binding_manifest: CurrentIncidentReportBindingManifest,
        evidence_by_ref: Optional[
            Mapping[str, ServerOwnedEvidenceFile]
        ] = None,
        server_evidence_root: Optional[Path] = None,
    ) -> CurrentReportDraftMetadata:
        """Create one idempotent exact draft for approved current Incidents."""

        self._validate_binding_scope(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            binding=binding_manifest,
        )
        source = Path(source_path)
        source_fingerprint = _fingerprint_regular_file(
            source, label="audited source HWPX", suffix=".hwpx"
        )
        if source_fingerprint.sha256 != binding_manifest.template_sha256:
            raise CurrentReportDraftError(
                "source_template_hash_mismatch",
                "the audited source HWPX no longer matches the binding",
            )
        source = Path(os.path.abspath(os.fspath(source)))
        self._source_path_cache[
            (
                binding_manifest.template_id,
                binding_manifest.template_sha256,
            )
        ] = source
        resolved_evidence, evidence_root = self._resolve_evidence(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            binding=binding_manifest,
            evidence_by_ref=evidence_by_ref,
            server_evidence_root=server_evidence_root,
        )
        binding_payload = binding_manifest.to_dict()
        binding_payload_sha256 = _canonical_sha256(binding_payload)
        request_payload = {
            "schema_version": CURRENT_REPORT_DRAFT_SCHEMA,
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(analysis_job_id),
            "source_document_sha256": source_fingerprint.sha256,
            "source_target_summary_sha256": (
                binding_manifest.structure_target_summary_sha256
            ),
            "binding_manifest_sha256": (
                binding_manifest.binding_manifest_sha256
            ),
            "binding_payload_sha256": binding_payload_sha256,
        }
        request_sha256 = _canonical_sha256(request_payload)
        draft_id = f"current-report-{request_sha256[:32]}"
        draft_root = self._draft_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            draft_id=draft_id,
            create=True,
        )
        record_path = draft_root / "record.json"

        with self._lock:
            if _path_lexists(record_path):
                loaded = self._load_record(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    draft_id=draft_id,
                    record_path=record_path,
                )
                if (
                    loaded.analysis_job_id != analysis_job_id
                    or loaded.request_sha256 != request_sha256
                    or loaded.binding_manifest.binding_manifest_sha256
                    != binding_manifest.binding_manifest_sha256
                    or _canonical_sha256(
                        loaded.binding_manifest.to_dict()
                    )
                    != binding_payload_sha256
                    or not _same_path(loaded.source_path, source)
                ):
                    raise CurrentReportDraftConflict(
                        "draft_request_conflict",
                        "the content address belongs to a different exact draft request",
                    )
                return loaded.public_metadata(idempotent_replay=True)

            (
                artifact_draft_root,
                artifact_relative_root,
                category_root_identity_sha256,
            ) = self._artifact_draft_root(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                draft_id=draft_id,
                create=True,
            )
            transcode_root = _real_directory(
                artifact_draft_root / "transcoded",
                label="current-report transcode root",
                create=True,
            )
            plan = self._plan_builder(
                source_path=source,
                binding_manifest=binding_manifest,
                evidence_by_ref=resolved_evidence,
                server_evidence_root=evidence_root,
                transcode_root=transcode_root,
            )
            self._validate_plan(
                plan=plan,
                binding=binding_manifest,
                transcode_root=transcode_root,
            )
            exact_plan_sha256 = _plan_sha256(plan)
            try:
                document_edit_plan = (
                    self._document_edit_plan_builder(
                        source_path=source,
                        binding_manifest=binding_manifest,
                        exact_plan=plan,
                        staged_evidence_root=transcode_root,
                        project_id=project_id,
                        pipeline_job_id=pipeline_job_id,
                        analysis_job_id=analysis_job_id,
                        source_sha256=binding_manifest.source_sha256,
                    )
                )
                validate_current_report_document_edit_plan(
                    document_edit_plan,
                    binding_manifest=binding_manifest,
                    exact_plan=plan,
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    analysis_job_id=analysis_job_id,
                    source_sha256=binding_manifest.source_sha256,
                )
            except CurrentReportDocumentEditPlanError as error:
                raise CurrentReportDraftError(
                    error.code,
                    "the exact current-report DocumentEditPlan is invalid",
                ) from error
            output_path = _owned_path(
                artifact_draft_root
                / f"exact-{exact_plan_sha256}.hwpx",
                root=artifact_draft_root,
                label="exact draft output",
            )
            if _path_lexists(output_path):
                raise CurrentReportDraftConflict(
                    "stale_existing_output",
                    "an unbound exact draft output already exists",
                )

            writer_returned = False
            try:
                writer_result = self._writer(source, output_path, plan)
                writer_returned = True
                self._validate_writer_result(
                    result=writer_result,
                    plan=plan,
                    binding=binding_manifest,
                    expected_output=output_path,
                )
                output_fingerprint = _fingerprint_regular_file(
                    output_path, label="exact draft output", suffix=".hwpx"
                )
                reopened = self._reopen_verifier(output_path)
                self._validate_reopened_output(
                    reopened=reopened,
                    fingerprint=output_fingerprint,
                    writer_result=writer_result,
                )
                render_binding = self._render_binding_builder(
                    binding_manifest
                )
                render_binding_sha256 = _render_binding_sha256(
                    render_binding
                )
                record_without_hash = {
                    "schema_version": CURRENT_REPORT_DRAFT_SCHEMA,
                    "state": CURRENT_REPORT_DRAFT_STATE,
                    "draft_id": draft_id,
                    "project_id": str(project_id),
                    "pipeline_job_id": str(pipeline_job_id),
                    "analysis_job_id": str(analysis_job_id),
                    "request_sha256": request_sha256,
                    "request": request_payload,
                    "artifact_category": _ARTIFACT_CATEGORY,
                    "artifact_relative_root": artifact_relative_root,
                    "category_root_identity_sha256": (
                        category_root_identity_sha256
                    ),
                    "source_path": (
                        "audited-template:"
                        + binding_manifest.template_id
                    ),
                    "source_document_sha256": source_fingerprint.sha256,
                    "transcode_root": "transcoded",
                    "binding_manifest": binding_payload,
                    "binding_manifest_sha256": (
                        binding_manifest.binding_manifest_sha256
                    ),
                    "binding_payload_sha256": binding_payload_sha256,
                    "exact_plan": _plan_locator_dict(
                        plan,
                        transcode_root=transcode_root,
                    ),
                    "exact_plan_sha256": exact_plan_sha256,
                    "document_edit_plan": (
                        document_edit_plan.to_dict()
                    ),
                    "document_edit_plan_sha256": (
                        document_edit_plan
                        .document_edit_plan_sha256
                    ),
                    "accept_reject_rollback_binding_sha256": (
                        document_edit_plan
                        .accept_reject_rollback_binding_sha256
                    ),
                    "writer_result": _writer_result_locator_dict(
                        writer_result,
                        output_path=output_path,
                    ),
                    "output_file_name": output_path.name,
                    "output_document_sha256": (
                        writer_result.output_document_sha256
                    ),
                    "output_size_bytes": output_fingerprint.size,
                    "render_binding": asdict(render_binding),
                    "render_binding_sha256": render_binding_sha256,
                    "writer_invocation_count": 1,
                    "provider_invocation_count": 0,
                    "com_invocation_count": 0,
                    "exact_writer_completed": True,
                    "render_validation_completed": False,
                    "apply_approval_required": True,
                    "publication_completed": False,
                    "full_e2e_completed": False,
                }
                record = {
                    **record_without_hash,
                    "record_sha256": _canonical_sha256(
                        record_without_hash
                    ),
                }
                _write_atomic_new(record_path, _canonical_json(record))
            except BaseException:
                # The exact writer owns its own failure cleanup.  If it
                # returned an invalid result, this service owns the expected
                # path and removes only that bounded, unpublished draft.
                if writer_returned and _path_lexists(output_path):
                    try:
                        output_path.unlink()
                    except OSError:
                        pass
                raise

            loaded = self._load_record(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                draft_id=draft_id,
                record_path=record_path,
            )
            return loaded.public_metadata(idempotent_replay=False)

    def _resolve_evidence(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        binding: CurrentIncidentReportBindingManifest,
        evidence_by_ref: Optional[
            Mapping[str, ServerOwnedEvidenceFile]
        ],
        server_evidence_root: Optional[Path],
    ) -> tuple[Mapping[str, ServerOwnedEvidenceFile], Path]:
        explicitly_supplied = (
            evidence_by_ref is not None
            or server_evidence_root is not None
        )
        if explicitly_supplied:
            if (
                evidence_by_ref is None
                or server_evidence_root is None
            ):
                raise CurrentReportDraftError(
                    "report_evidence_configuration_invalid",
                    "the evidence mapping and server root must be supplied together",
                )
            return (
                evidence_by_ref,
                _real_directory(
                    Path(server_evidence_root),
                    label="server evidence root",
                    create=False,
                ),
            )
        if (
            self._report_evidence_resolver is None
            or self._server_evidence_root is None
        ):
            raise CurrentReportDraftError(
                "report_evidence_resolver_unavailable",
                "the server report-evidence resolver is unavailable",
            )

        selected: dict[str, str] = {}
        for incident in binding.incident_bindings:
            current = selected.setdefault(
                incident.selected_evidence_ref,
                incident.selected_evidence_sha256,
            )
            if current != incident.selected_evidence_sha256:
                raise CurrentReportDraftError(
                    "report_evidence_provenance_conflict",
                    "one selected evidence reference has conflicting hashes",
                )
        resolved = {
            selected_ref: self._report_evidence_resolver(
                project_id,
                pipeline_job_id,
                analysis_job_id,
                selected_ref,
                expected_sha256,
            )
            for selected_ref, expected_sha256 in sorted(
                selected.items()
            )
        }
        evidence_root = self._server_evidence_root
        if self._artifact_root_resolver is not None:
            try:
                report_category = _real_directory(
                    Path(
                        self._artifact_root_resolver(
                            project_id,
                            pipeline_job_id,
                            analysis_job_id,
                        )
                    ),
                    label="PipelineJob report artifact category",
                    create=False,
                )
                evidence_root = _real_directory(
                    report_category.parent,
                    label="PipelineJob evidence root",
                    create=False,
                )
            except CurrentReportDraftError:
                raise
            except Exception as error:
                raise CurrentReportDraftError(
                    "report_evidence_root_unavailable",
                    "the PipelineJob evidence root is unavailable",
                ) from error
        assert evidence_root is not None
        return resolved, evidence_root

    def load(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        draft_id: str,
    ) -> CurrentReportDraft:
        """Load and revalidate a trusted draft for the approval boundary."""

        if not isinstance(draft_id, str) or _DRAFT_ID.fullmatch(draft_id) is None:
            raise CurrentReportDraftNotFound(
                "draft_not_found", "the exact current-report draft was not found"
            )
        draft_root = self._draft_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            draft_id=draft_id,
            create=False,
        )
        record_path = draft_root / "record.json"
        with self._lock:
            return self._load_record(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                draft_id=draft_id,
                record_path=record_path,
            )

    def metadata(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        draft_id: str,
    ) -> CurrentReportDraftMetadata:
        """Return a path-free, integrity-checked metadata view."""

        return self.load(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            draft_id=draft_id,
        ).public_metadata(idempotent_replay=True)

    def _draft_root(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        draft_id: str,
        create: bool,
    ) -> Path:
        if not isinstance(project_id, UUID) or not isinstance(
            pipeline_job_id, UUID
        ):
            raise CurrentReportDraftError(
                "project_job_scope_invalid",
                "the exact draft requires UUID project and PipelineJob identities",
            )
        if _DRAFT_ID.fullmatch(draft_id) is None:
            raise CurrentReportDraftError(
                "draft_identity_invalid",
                "the exact draft identity is invalid",
            )
        path = self._root / str(project_id) / str(pipeline_job_id) / draft_id
        if not create and not path.exists():
            raise CurrentReportDraftNotFound(
                "draft_not_found", "the exact current-report draft was not found"
            )
        return _real_directory(
            path,
            label="project/job exact draft root",
            create=create,
        )

    def _artifact_draft_root(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        draft_id: str,
        create: bool,
        expected_relative_root: Optional[str] = None,
        expected_category_identity: Optional[str] = None,
    ) -> tuple[Path, str, str]:
        try:
            if self._artifact_root_resolver is None:
                category_root = _real_directory(
                    self._root
                    / str(project_id)
                    / str(pipeline_job_id)
                    / "_report-artifacts",
                    label="test current-report artifact category",
                    create=create,
                )
            else:
                category_root = _real_directory(
                    Path(
                        self._artifact_root_resolver(
                            project_id,
                            pipeline_job_id,
                            analysis_job_id,
                        )
                    ),
                    label="PipelineJob report artifact category",
                    create=False,
                )
        except CurrentReportDraftError:
            raise
        except Exception as error:
            raise CurrentReportDraftError(
                "draft_artifact_root_unavailable",
                "the PipelineJob report artifact category is unavailable",
            ) from error
        relative_root = f"drafts/{draft_id}"
        if (
            expected_relative_root is not None
            and expected_relative_root != relative_root
        ):
            raise CurrentReportDraftCorrupt(
                "draft_artifact_locator_invalid",
                "the persisted exact draft artifact locator is invalid",
            )
        identity = _directory_identity_sha256(category_root)
        if (
            expected_category_identity is not None
            and not hmac.compare_digest(
                expected_category_identity,
                identity,
            )
        ):
            raise CurrentReportDraftCorrupt(
                "draft_artifact_root_changed",
                "the PipelineJob report artifact root changed",
            )
        candidate = (
            category_root
            / "drafts"
            / draft_id
        )
        if create and _path_lexists(candidate):
            raise CurrentReportDraftConflict(
                "stale_existing_output",
                "an unbound exact draft artifact root already exists",
            )
        artifact_root = _relative_artifact_directory(
            category_root,
            relative_root,
            create=create,
            label="exact draft artifact root",
        )
        return artifact_root, relative_root, identity

    def _resolve_template_source(
        self,
        *,
        template_id: str,
        template_sha256: str,
    ) -> Path:
        key = (template_id, template_sha256)
        cached = self._source_path_cache.get(key)
        if cached is not None:
            try:
                if (
                    _fingerprint_regular_file(
                        cached,
                        label="cached audited source HWPX",
                        suffix=".hwpx",
                    ).sha256
                    == template_sha256
                ):
                    return cached
            except CurrentReportDraftError:
                pass
            self._source_path_cache.pop(key, None)
        if self._template_source_resolver is None:
            raise CurrentReportDraftCorrupt(
                "draft_source_unavailable",
                "the audited source locator cannot be resolved after restart",
            )
        try:
            source = Path(
                self._template_source_resolver(
                    template_id,
                    template_sha256,
                )
            )
            fingerprint = _fingerprint_regular_file(
                source,
                label="resolved audited source HWPX",
                suffix=".hwpx",
            )
        except CurrentReportDraftError:
            raise
        except Exception as error:
            raise CurrentReportDraftCorrupt(
                "draft_source_unavailable",
                "the audited source locator is unavailable",
            ) from error
        if fingerprint.sha256 != template_sha256:
            raise CurrentReportDraftCorrupt(
                "draft_source_changed",
                "the audited source changed after draft creation",
            )
        resolved = Path(os.path.abspath(os.fspath(source)))
        self._source_path_cache[key] = resolved
        return resolved

    @staticmethod
    def _validate_binding_scope(
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        binding: CurrentIncidentReportBindingManifest,
    ) -> None:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(pipeline_job_id, UUID)
            or not isinstance(analysis_job_id, UUID)
            or not isinstance(
                binding, CurrentIncidentReportBindingManifest
            )
            or binding.project_id != str(project_id)
            or binding.pipeline_job_id != str(pipeline_job_id)
            or binding.analysis_job_id != str(analysis_job_id)
        ):
            raise CurrentReportDraftError(
                "cross_job_binding",
                "the current-Incident binding crossed its project or PipelineJob",
            )
        if (
            not binding.approved_incident_ids
            or binding.approved_incident_count
            != len(binding.approved_incident_ids)
            or binding.approved_incident_count
            != len(binding.incident_bindings)
            or tuple(
                row.incident_id for row in binding.incident_bindings
            )
            != binding.approved_incident_ids
            or binding.approved_incident_bound is not True
            or binding.content_provenance_bound is not True
            or binding.writer_required is not True
            or binding.apply_eligible is not False
            or binding.provider_called is not False
            or binding.com_invoked is not False
            or binding.document_written is not False
            or binding.writer_batching_required is not False
            or _SHA256.fullmatch(
                binding.binding_manifest_sha256
            )
            is None
        ):
            raise CurrentReportDraftError(
                "approved_incident_binding_required",
                "only the complete user-approved current-Incident binding may create a draft",
            )

    @staticmethod
    def _validate_plan(
        *,
        plan: ExactHwpxWritePlan,
        binding: CurrentIncidentReportBindingManifest,
        transcode_root: Path,
    ) -> None:
        expected_refs = tuple(
            f"incident:{incident_id}"
            for incident_id in binding.approved_incident_ids
        )
        if (
            not isinstance(plan, ExactHwpxWritePlan)
            or plan.schema_version != HWPX_EXACT_WRITER_SCHEMA
            or plan.source_document_sha256 != binding.template_sha256
            or plan.source_target_summary_sha256
            != binding.structure_target_summary_sha256
            or plan.incident_source_refs != expected_refs
            or len(plan.operations)
            != binding.required_writer_operation_count
            or len(plan.operations) != len(
                {operation.operation_id for operation in plan.operations}
            )
        ):
            raise CurrentReportDraftError(
                "exact_plan_binding_invalid",
                "the exact plan does not match the complete approved-Incident binding",
            )
        for operation in plan.operations:
            if not isinstance(
                operation,
                (
                    ExactHwpxTextOperation,
                    ExactHwpxParagraphTextOperation,
                    ExactHwpxPictureOperation,
                ),
            ):
                raise CurrentReportDraftError(
                    "exact_plan_operation_invalid",
                    "the exact plan contains an unsupported operation",
                )
            if isinstance(operation, ExactHwpxPictureOperation):
                evidence_path = _owned_path(
                    Path(operation.evidence_path),
                    root=transcode_root,
                    label="transcoded exact-plan evidence",
                )
                fingerprint = _fingerprint_regular_file(
                    evidence_path, label="transcoded exact-plan evidence"
                )
                if fingerprint.sha256 != operation.evidence_sha256:
                    raise CurrentReportDraftError(
                        "transcoded_evidence_hash_mismatch",
                        "exact-plan evidence changed before the writer ran",
                    )

    @staticmethod
    def _validate_writer_result(
        *,
        result: ExactHwpxWriteResult,
        plan: ExactHwpxWritePlan,
        binding: CurrentIncidentReportBindingManifest,
        expected_output: Path,
    ) -> None:
        operation_ids = tuple(
            operation.operation_id for operation in plan.operations
        )
        verification_by_id = (
            {
                item.operation_id: item
                for item in result.target_verifications
            }
            if isinstance(result, ExactHwpxWriteResult)
            else {}
        )
        exact_verification_binding = (
            len(verification_by_id) == len(operation_ids)
            and set(verification_by_id) == set(operation_ids)
            and all(
                (
                    verification_by_id[operation.operation_id].target_id
                    == operation.target_id
                    and verification_by_id[
                        operation.operation_id
                    ].kind
                    == operation.kind
                    and verification_by_id[
                        operation.operation_id
                    ].source_ref
                    == operation.source_ref
                )
                for operation in plan.operations
            )
        )
        if (
            not isinstance(result, ExactHwpxWriteResult)
            or result.schema_version != HWPX_EXACT_WRITER_SCHEMA
            or result.state
            != "writer_completed_com_validation_required"
            or result.plan_id != plan.plan_id
            or result.source_document_sha256
            != binding.template_sha256
            or not _same_path(Path(result.output_path), expected_output)
            or _SHA256.fullmatch(
                result.output_document_sha256
            )
            is None
            or result.operation_count != len(plan.operations)
            or exact_verification_binding is not True
            or result.table_count != EXPECTED_TABLE_COUNT
            or result.expected_rendered_page_count
            != EXPECTED_RENDERED_PAGE_COUNT
            or result.output_rendered_page_count is not None
            or result.page_contract_proxy_verified is not True
            or result.output_render_validation_status
            != "not_run_com_validation_required"
            or _SHA256.fullmatch(
                result.structure_projection_sha256
            )
            is None
            or result.source_unchanged is not True
            or result.mimetype_first_and_stored is not True
            or result.com_invocation_count != 0
        ):
            raise CurrentReportDraftError(
                "exact_writer_result_invalid",
                "the exact writer did not return a verified unpublished 48-page draft",
            )

    @staticmethod
    def _validate_reopened_output(
        *,
        reopened: ReopenedHwpxVerification,
        fingerprint: _FileFingerprint,
        writer_result: ExactHwpxWriteResult,
    ) -> None:
        if (
            not isinstance(reopened, ReopenedHwpxVerification)
            or reopened.document_sha256 != fingerprint.sha256
            or reopened.document_sha256
            != writer_result.output_document_sha256
            or reopened.table_count != EXPECTED_TABLE_COUNT
            or reopened.table_count != writer_result.table_count
            or reopened.mimetype_first_and_stored is not True
        ):
            raise CurrentReportDraftError(
                "output_reopen_verification_failed",
                "the exact HWPX draft failed independent reopen verification",
            )

    def _load_record(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        draft_id: str,
        record_path: Path,
    ) -> CurrentReportDraft:
        if not _path_lexists(record_path):
            raise CurrentReportDraftNotFound(
                "draft_not_found", "the exact current-report draft was not found"
            )
        _owned_path(
            record_path,
            root=self._root,
            label="exact draft record",
        )
        fingerprint = _fingerprint_regular_file(
            record_path, label="exact draft record"
        )
        if not 1 <= fingerprint.size <= MAX_CURRENT_REPORT_DRAFT_RECORD_BYTES:
            raise CurrentReportDraftCorrupt(
                "draft_record_corrupt",
                "the exact draft record size is invalid",
            )
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise CurrentReportDraftCorrupt(
                "draft_record_corrupt",
                "the exact draft record is unreadable",
            ) from error
        if not isinstance(payload, dict):
            raise CurrentReportDraftCorrupt(
                "draft_record_corrupt",
                "the exact draft record is invalid",
            )
        supplied_record_sha = payload.get("record_sha256")
        record_without_hash = dict(payload)
        record_without_hash.pop("record_sha256", None)
        if (
            set(payload) != _RECORD_KEYS
            or _SHA256.fullmatch(str(supplied_record_sha)) is None
            or supplied_record_sha
            != _canonical_sha256(record_without_hash)
            or payload.get("schema_version")
            != CURRENT_REPORT_DRAFT_SCHEMA
            or payload.get("state") != CURRENT_REPORT_DRAFT_STATE
            or payload.get("draft_id") != draft_id
            or payload.get("project_id") != str(project_id)
            or payload.get("pipeline_job_id") != str(pipeline_job_id)
            or payload.get("provider_invocation_count") != 0
            or payload.get("com_invocation_count") != 0
            or payload.get("writer_invocation_count") != 1
            or payload.get("exact_writer_completed") is not True
            or payload.get("render_validation_completed") is not False
            or payload.get("apply_approval_required") is not True
            or payload.get("publication_completed") is not False
            or payload.get("full_e2e_completed") is not False
        ):
            raise CurrentReportDraftCorrupt(
                "draft_record_corrupt",
                "the exact draft record failed integrity verification",
            )
        try:
            analysis_job_id = UUID(str(payload["analysis_job_id"]))
            binding = current_incident_report_binding_from_dict(
                payload["binding_manifest"]
            )
            self._validate_binding_scope(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                binding=binding,
            )
            if (
                payload.get("artifact_category")
                != _ARTIFACT_CATEGORY
                or payload.get("source_path")
                != f"audited-template:{binding.template_id}"
                or payload.get("transcode_root") != "transcoded"
                or _SHA256.fullmatch(
                    str(
                        payload.get(
                            "category_root_identity_sha256"
                        )
                    )
                )
                is None
            ):
                raise CurrentReportDraftCorrupt(
                    "draft_artifact_locator_invalid",
                    "the persisted exact draft locator is invalid",
                )
            source_path = self._resolve_template_source(
                template_id=binding.template_id,
                template_sha256=binding.template_sha256,
            )
            (
                artifact_draft_root,
                _artifact_relative_root,
                _category_identity,
            ) = self._artifact_draft_root(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                draft_id=draft_id,
                create=False,
                expected_relative_root=str(
                    payload["artifact_relative_root"]
                ),
                expected_category_identity=str(
                    payload["category_root_identity_sha256"]
                ),
            )
            transcode_root = _real_directory(
                artifact_draft_root / "transcoded",
                label="persisted transcode root",
                create=False,
            )
            plan = _plan_from_dict(
                payload["exact_plan"],
                transcode_root=transcode_root,
            )
            output_file_name = str(payload["output_file_name"])
            if (
                _SAFE_RELATIVE_NAME.fullmatch(output_file_name)
                is None
                or Path(output_file_name).suffix.casefold()
                != ".hwpx"
            ):
                raise CurrentReportDraftCorrupt(
                    "draft_artifact_locator_invalid",
                    "the exact draft output locator is invalid",
                )
            expected_output = _owned_path(
                artifact_draft_root / output_file_name,
                root=artifact_draft_root,
                label="persisted exact draft output",
            )
            writer_result = _writer_result_from_dict(
                payload["writer_result"],
                output_path=expected_output,
            )
            render_binding = _render_binding_from_dict(
                payload["render_binding"]
            )
        except (
            CurrentReportDraftError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            if isinstance(error, CurrentReportDraftCorrupt):
                raise
            raise CurrentReportDraftCorrupt(
                "draft_record_corrupt",
                "the exact draft record contains invalid typed data",
            ) from error
        request = payload.get("request")
        if (
            not isinstance(request, dict)
            or set(request) != _REQUEST_KEYS
            or payload.get("request_sha256")
            != _canonical_sha256(request)
            or payload.get("binding_manifest_sha256")
            != binding.binding_manifest_sha256
            or payload.get("binding_payload_sha256")
            != _canonical_sha256(binding.to_dict())
            or payload.get("source_document_sha256")
            != binding.template_sha256
            or request.get("project_id") != str(project_id)
            or request.get("pipeline_job_id") != str(pipeline_job_id)
            or request.get("analysis_job_id") != str(analysis_job_id)
            or request.get("binding_manifest_sha256")
            != binding.binding_manifest_sha256
        ):
            raise CurrentReportDraftCorrupt(
                "draft_record_corrupt",
                "the exact draft request binding is invalid",
            )
        if draft_id != (
            f"current-report-{str(payload['request_sha256'])[:32]}"
        ):
            raise CurrentReportDraftConflict(
                "draft_content_address_conflict",
                "the persisted request does not match its content address",
            )
        source_fingerprint = _fingerprint_regular_file(
            source_path, label="persisted audited source", suffix=".hwpx"
        )
        if source_fingerprint.sha256 != binding.template_sha256:
            raise CurrentReportDraftCorrupt(
                "draft_source_changed",
                "the audited source changed after draft creation",
            )
        self._validate_plan(
            plan=plan,
            binding=binding,
            transcode_root=transcode_root,
        )
        exact_plan_sha256 = _plan_sha256(plan)
        if payload.get("exact_plan_sha256") != exact_plan_sha256:
            raise CurrentReportDraftCorrupt(
                "draft_plan_changed",
                "the persisted exact plan failed integrity verification",
            )
        try:
            document_edit_plan = (
                self._document_edit_plan_builder(
                    source_path=source_path,
                    binding_manifest=binding,
                    exact_plan=plan,
                    staged_evidence_root=transcode_root,
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    analysis_job_id=analysis_job_id,
                    source_sha256=binding.source_sha256,
                )
            )
            validate_current_report_document_edit_plan(
                document_edit_plan,
                binding_manifest=binding,
                exact_plan=plan,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                source_sha256=binding.source_sha256,
            )
        except CurrentReportDocumentEditPlanError as error:
            raise CurrentReportDraftCorrupt(
                error.code,
                "the persisted DocumentEditPlan failed verification",
            ) from error
        if (
            not isinstance(payload.get("document_edit_plan"), dict)
            or _canonical_sha256(payload["document_edit_plan"])
            != _canonical_sha256(document_edit_plan.to_dict())
            or payload.get("document_edit_plan_sha256")
            != document_edit_plan.document_edit_plan_sha256
            or payload.get(
                "accept_reject_rollback_binding_sha256"
            )
            != document_edit_plan
            .accept_reject_rollback_binding_sha256
        ):
            raise CurrentReportDraftCorrupt(
                "draft_document_edit_plan_changed",
                "the persisted DocumentEditPlan or decision binding changed",
            )
        if output_file_name != f"exact-{exact_plan_sha256}.hwpx":
            raise CurrentReportDraftCorrupt(
                "draft_artifact_locator_invalid",
                "the exact draft output locator changed",
            )
        self._validate_writer_result(
            result=writer_result,
            plan=plan,
            binding=binding,
            expected_output=expected_output,
        )
        output_fingerprint = _fingerprint_regular_file(
            expected_output,
            label="persisted exact draft output",
            suffix=".hwpx",
        )
        reopened = self._reopen_verifier(expected_output)
        try:
            self._validate_reopened_output(
                reopened=reopened,
                fingerprint=output_fingerprint,
                writer_result=writer_result,
            )
        except CurrentReportDraftError as error:
            raise CurrentReportDraftCorrupt(
                error.code,
                "the persisted exact draft output failed integrity verification",
            ) from error
        expected_render_binding = self._render_binding_builder(binding)
        render_sha = _render_binding_sha256(render_binding)
        if (
            render_binding != expected_render_binding
            or payload.get("render_binding_sha256") != render_sha
            or payload.get("output_document_sha256")
            != writer_result.output_document_sha256
            or payload.get("output_size_bytes")
            != output_fingerprint.size
        ):
            raise CurrentReportDraftCorrupt(
                "draft_render_binding_changed",
                "the persisted render binding or output changed",
            )
        return CurrentReportDraft(
            draft_id=draft_id,
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            source_path=source_path,
            output_path=expected_output,
            transcode_root=transcode_root,
            request_sha256=str(payload["request_sha256"]),
            binding_manifest=binding,
            exact_plan=plan,
            document_edit_plan=document_edit_plan,
            writer_result=writer_result,
            render_binding=render_binding,
            exact_plan_sha256=exact_plan_sha256,
            render_binding_sha256=render_sha,
            record_sha256=str(supplied_record_sha),
            record_path=record_path,
        )


def _require_exact_keys(
    value: Any,
    *,
    dataclass_type: type,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    expected = {field.name for field in fields(dataclass_type)}
    if set(value) != expected:
        raise ValueError(f"{label} fields are invalid")
    return dict(value)


def _paragraph_address_from_dict(value: Any) -> XmlParagraphAddress:
    payload = _require_exact_keys(
        value,
        dataclass_type=XmlParagraphAddress,
        label="paragraph address",
    )
    payload["xml_child_path"] = tuple(payload["xml_child_path"])
    payload["selected_text_run_ordinals"] = tuple(
        payload["selected_text_run_ordinals"]
    )
    payload["selected_text_run_paths"] = tuple(
        tuple(path) for path in payload["selected_text_run_paths"]
    )
    payload["direct_linesegarray_child_indexes"] = tuple(
        payload["direct_linesegarray_child_indexes"]
    )
    payload["direct_linesegarray_sha256s"] = tuple(
        payload["direct_linesegarray_sha256s"]
    )
    return XmlParagraphAddress(**payload)


def _plan_from_dict(
    value: Any,
    *,
    transcode_root: Path,
) -> ExactHwpxWritePlan:
    payload = _require_exact_keys(
        value,
        dataclass_type=ExactHwpxWritePlan,
        label="exact HWPX plan",
    )
    operations: list[ExactHwpxOperation] = []
    for item in payload["operations"]:
        if not isinstance(item, dict):
            raise ValueError("exact HWPX operation must be an object")
        kind = item.get("kind")
        if kind == "replace_cell_text":
            operation_payload = _require_exact_keys(
                item,
                dataclass_type=ExactHwpxTextOperation,
                label="exact text operation",
            )
            operation_payload.pop("kind")
            operations.append(ExactHwpxTextOperation(**operation_payload))
        elif kind == "replace_paragraph_text":
            operation_payload = _require_exact_keys(
                item,
                dataclass_type=ExactHwpxParagraphTextOperation,
                label="exact paragraph operation",
            )
            operation_payload.pop("kind")
            operation_payload["address"] = _paragraph_address_from_dict(
                operation_payload["address"]
            )
            operations.append(
                ExactHwpxParagraphTextOperation(**operation_payload)
            )
        elif kind == "replace_picture":
            operation_payload = _require_exact_keys(
                item,
                dataclass_type=ExactHwpxPictureOperation,
                label="exact picture operation",
            )
            operation_payload.pop("kind")
            relative_name = operation_payload.get("evidence_path")
            if (
                not isinstance(relative_name, str)
                or _SAFE_RELATIVE_NAME.fullmatch(relative_name)
                is None
                or Path(relative_name).name != relative_name
            ):
                raise ValueError(
                    "exact picture evidence locator is invalid"
                )
            operation_payload["evidence_path"] = str(
                _owned_path(
                    transcode_root / relative_name,
                    root=transcode_root,
                    label="persisted exact-plan evidence",
                )
            )
            operations.append(
                ExactHwpxPictureOperation(**operation_payload)
            )
        else:
            raise ValueError("exact HWPX operation kind is invalid")
    payload["incident_source_refs"] = tuple(
        payload["incident_source_refs"]
    )
    payload["operations"] = tuple(operations)
    return ExactHwpxWritePlan(**payload)


def _writer_result_from_dict(
    value: Any,
    *,
    output_path: Path,
) -> ExactHwpxWriteResult:
    payload = _require_exact_keys(
        value,
        dataclass_type=ExactHwpxWriteResult,
        label="exact writer result",
    )
    payload["target_verifications"] = tuple(
        ExactHwpxTargetVerification(
            **_require_exact_keys(
                item,
                dataclass_type=ExactHwpxTargetVerification,
                label="exact target verification",
            )
        )
        for item in payload["target_verifications"]
    )
    if payload.get("output_path") != output_path.name:
        raise ValueError("exact writer output locator is invalid")
    payload["output_path"] = str(output_path)
    return ExactHwpxWriteResult(**payload)


def _render_binding_from_dict(value: Any) -> ReportBindingManifest:
    payload = _require_exact_keys(
        value,
        dataclass_type=ReportBindingManifest,
        label="render binding",
    )
    payload["expected_page_pair_slots"] = tuple(
        ExpectedPagePairSlot(
            **{
                **_require_exact_keys(
                    item,
                    dataclass_type=ExpectedPagePairSlot,
                    label="expected page-pair slot",
                ),
                "incident_ids": tuple(item["incident_ids"]),
                "expected_text_evidence": tuple(
                    item["expected_text_evidence"]
                ),
            }
        )
        for item in payload["expected_page_pair_slots"]
    )
    payload["current_incident_ids"] = tuple(
        payload["current_incident_ids"]
    )
    return ReportBindingManifest(**payload)


__all__ = [
    "CURRENT_REPORT_DRAFT_SCHEMA",
    "CURRENT_REPORT_DRAFT_STATE",
    "CurrentReportDraft",
    "CurrentReportDraftConflict",
    "CurrentReportDraftCorrupt",
    "CurrentReportDraftError",
    "CurrentReportDraftMetadata",
    "CurrentReportDraftNotFound",
    "CurrentReportDraftService",
    "AuditedTemplateSourceResolver",
    "ReportArtifactRootResolver",
    "ReportEvidenceResolver",
    "ReopenedHwpxVerification",
]
