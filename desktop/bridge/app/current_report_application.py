"""Durable exact-current-report COM apply and decision/rollback ledger."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID, uuid4

from .current_report_approval import CurrentReportApplyAuthorization
from .current_report_document_edit_plan import (
    CurrentReportDocumentEditPlanError,
    validate_current_report_document_edit_plan,
)
from .current_report_draft import CurrentReportDraft
from .hwp_com import (
    ExactHwpxPublishResult,
    HwpDocumentFingerprint,
    fingerprint_document,
)
from .hwp_render_validation import (
    EXPECTED_PAGE_COUNT,
    HwpRenderValidationResult,
    RenderValidationLimits,
    RenderedPageEvidence,
    rendered_page_manifest_sha256,
    validate_saved_hwpx_pdf_render,
)
from .project_artifact_roots import (
    JOB_BINDING_SCHEMA,
    ProjectArtifactRootRegistry,
)


CURRENT_REPORT_APPLICATION_SCHEMA = (
    "trafficvisson.current-report-application.v1"
)
CURRENT_REPORT_CAPACITY_PLAN_SCHEMA = (
    "trafficvisson.current-report-capacity-plan.v1"
)
CURRENT_REPORT_CAPACITY_SAFETY_RESERVE_BYTES = 4 * 1024**3
_CURRENT_REPORT_WORKING_OVERHEAD_BYTES = 512 * 1024**2
_ARTIFACT_CATEGORY = "reports"
_APPLICATION_ID = re.compile(
    r"^current-report-application-[a-f0-9]{32}$"
)
_CURRENT_REPORT_RESERVATION_ID = re.compile(
    r"^current-report:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_RENDER_DIRECTORY_NAME = re.compile(
    r"^trafficvisson-render-[a-f0-9]{32}$"
)
_RENDERED_PAGE_NAME = re.compile(r"^page-(\d{2})\.png$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_RELATIVE_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$"
)
_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS = (
    0.025,
    0.05,
    0.1,
    0.2,
    0.4,
)
_RENDERED_PAGE_RECORD_KEYS = {
    "page_number",
    "file_name",
    "png_sha256",
    "width_pixels",
    "height_pixels",
    "size_bytes",
    "significant_photo_count",
    "neutral_picture_count",
    "caption_proxy_count",
}
_RECORD_KEYS = {
    "schema_version",
    "state",
    "application_id",
    "project_id",
    "pipeline_job_id",
    "analysis_job_id",
    "draft_id",
    "approval_id",
    "binding_manifest_sha256",
    "render_binding_sha256",
    "exact_plan_sha256",
    "document_edit_plan_sha256",
    "accept_reject_rollback_binding_sha256",
    "preview_sha256",
    "artifact_category",
    "artifact_relative_root",
    "category_root_identity_sha256",
    "output_path",
    "output_file_name",
    "output_sha256",
    "output_size_bytes",
    "pdf_path",
    "pdf_file_name",
    "pdf_sha256",
    "render_directory",
    "render_validation_status",
    "rendered_page_count",
    "rendered_pages",
    "render_failure_codes",
    "capacity_plan",
    "capacity_reservation",
    "capacity_reservation_released",
    "output_reopen_structure_sha256",
    "raster_artifact_manifest_sha256",
    "com_publish_completed",
    "pdf_export_completed",
    "render_validation_completed",
    "rollback_completed",
    "accept_reject_required",
    "accepted",
    "rejected",
    "error_code",
    "record_sha256",
}


class CurrentReportApplicationError(RuntimeError):
    """Caller-safe application failure with durable rollback intent."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        application_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.application_id = application_id


class ExactReportPublisher(Protocol):
    def publish_approved_exact_hwpx(
        self,
        *,
        approval_id: str,
        source_hwpx: Path,
        destination_hwpx: Path,
    ) -> ExactHwpxPublishResult: ...

    def export_applied_hwpx_to_pdf(
        self,
        *,
        approval_id: str,
        source_hwpx: Path,
        destination_pdf: Path,
    ) -> Path: ...


@dataclass(frozen=True)
class CurrentReportApplicationMetadata:
    application_id: str
    state: str
    project_id: str
    pipeline_job_id: str
    analysis_job_id: str
    draft_id: str
    binding_manifest_sha256: str
    exact_plan_sha256: str
    document_edit_plan_sha256: str
    accept_reject_rollback_binding_sha256: str
    output_file_name: str
    output_sha256: str | None
    pdf_file_name: str
    pdf_sha256: str | None
    render_validation_status: str
    rendered_page_count: int | None
    render_failure_codes: tuple[str, ...]
    output_reopen_structure_sha256: str | None
    raster_artifact_manifest_sha256: str | None
    com_publish_completed: bool
    pdf_export_completed: bool
    render_validation_completed: bool
    rollback_completed: bool
    accept_reject_required: bool
    accepted: bool
    rejected: bool
    path_exposed: bool = False
    schema_version: str = CURRENT_REPORT_APPLICATION_SCHEMA


@dataclass(frozen=True)
class AcceptedCurrentReportPublicationSource:
    """Bridge-internal, hash-verified source for project publication."""

    application_id: str
    project_id: UUID
    pipeline_job_id: UUID
    analysis_job_id: UUID
    draft_id: str
    output_path: Path
    output_file_name: str
    output_sha256: str
    output_size_bytes: int
    pdf_path: Path
    pdf_file_name: str
    pdf_sha256: str
    pdf_size_bytes: int
    binding_manifest_sha256: str
    exact_plan_sha256: str
    document_edit_plan_sha256: str
    accept_reject_rollback_binding_sha256: str
    output_reopen_structure_sha256: str
    raster_artifact_manifest_sha256: str
    rendered_page_count: int


@dataclass(frozen=True)
class _OwnedRenderArtifacts:
    render_root: Path
    render_directory: Path | None
    page_files: tuple[tuple[Path, str | None], ...]


class CurrentReportApplicationService:
    """Publish, render-validate, and durably decide exact report outputs."""

    def __init__(
        self,
        *,
        owned_application_root: Path,
        render_limits: RenderValidationLimits = RenderValidationLimits(),
        artifact_root_resolver: Callable[
            [UUID, UUID, UUID], Path
        ]
        | None = None,
        artifact_root_registry: ProjectArtifactRootRegistry
        | None = None,
        pipeline_job_resolver: Callable[
            [UUID, UUID], Mapping[str, Any]
        ]
        | None = None,
        recover_capacity_on_startup: bool = True,
    ) -> None:
        self._root = _real_directory(
            Path(owned_application_root),
            create=True,
        )
        self._render_limits = render_limits
        if (
            artifact_root_resolver is not None
            and not callable(artifact_root_resolver)
        ):
            raise TypeError("artifact_root_resolver must be callable")
        if (
            artifact_root_registry is None
        ) != (pipeline_job_resolver is None):
            raise ValueError(
                "capacity registry and PipelineJob resolver must be configured together"
            )
        if (
            artifact_root_registry is not None
            and not isinstance(
                artifact_root_registry,
                ProjectArtifactRootRegistry,
            )
        ):
            raise TypeError("artifact_root_registry is invalid")
        if (
            pipeline_job_resolver is not None
            and not callable(pipeline_job_resolver)
        ):
            raise TypeError("pipeline_job_resolver must be callable")
        if not isinstance(recover_capacity_on_startup, bool):
            raise TypeError(
                "recover_capacity_on_startup must be a boolean"
            )
        self._artifact_root_resolver = artifact_root_resolver
        self._artifact_roots = artifact_root_registry
        self._pipeline_job_resolver = pipeline_job_resolver
        self._lock = threading.RLock()
        self.startup_capacity_recovery = (
            self.recover_capacity_reservations()
            if recover_capacity_on_startup
            and artifact_root_registry is not None
            else {"restored": 0, "released": 0, "blocked": 0}
        )

    def recover_capacity_reservations(self) -> dict[str, int]:
        """Conservatively recover interrupted report holds after restart.

        A verified render only needs its future-output hold released. Any
        earlier interrupted COM/PDF/render state is hash-boundedly rolled
        back before release; unknown or changed bytes keep the hold and make
        startup fail closed.
        """

        if (
            self._artifact_roots is None
            or self._pipeline_job_resolver is None
        ):
            return {"restored": 0, "released": 0, "blocked": 0}
        restored = 0
        released = 0
        blocked = 0
        for record_path in self._application_record_paths():
            with self._lock:
                record = _load_record(record_path)
                if record["capacity_reservation_released"] is True:
                    continue
                try:
                    project_id = UUID(record["project_id"])
                    pipeline_job_id = UUID(record["pipeline_job_id"])
                    analysis_job_id = UUID(record["analysis_job_id"])
                    application_id = str(record["application_id"])
                    plan = record["capacity_plan"]
                    category_root, category_identity = (
                        self._resolve_artifact_category(
                            project_id=project_id,
                            pipeline_job_id=pipeline_job_id,
                            analysis_job_id=analysis_job_id,
                            create_fallback=False,
                        )
                    )
                    if not hmac.compare_digest(
                        category_identity,
                        str(
                            record[
                                "category_root_identity_sha256"
                            ]
                        ),
                    ):
                        raise CurrentReportApplicationError(
                            "application_artifact_root_changed",
                            "the report artifact root changed during restart",
                            application_id=application_id,
                        )
                    observation = self._reserve_capacity(
                        project_id=project_id,
                        pipeline_job_id=pipeline_job_id,
                        analysis_job_id=analysis_job_id,
                        category_root=category_root,
                        plan=plan,
                    )
                    restored += 1
                    record["capacity_reservation"] = observation
                    relative = str(record["artifact_relative_root"])
                    application_root = (
                        category_root
                        / relative.replace("/", os.sep)
                    )
                    if _path_entry_exists(application_root):
                        application_root = (
                            _relative_artifact_directory(
                                category_root,
                                relative,
                                create=False,
                            )
                        )
                    output_path = application_root / str(
                        record["output_path"]
                    )
                    pdf_path = application_root / str(
                        record["pdf_path"]
                    )
                    state = str(record["state"])
                    stage_writes_complete = state in {
                        "render_validated_accept_or_reject_required",
                        "accepted_render_validated_report",
                        "rejected_report_rolled_back",
                        "accepted_report_rolled_back",
                        "render_validation_failed_rolled_back",
                        "apply_failed_rolled_back",
                    }
                    if not stage_writes_complete:
                        rollback_ok = self._rollback_artifacts(
                            application_root=application_root,
                            record=record,
                            output_path=output_path,
                            output_sha256=record["output_sha256"],
                            pdf_path=pdf_path,
                            pdf_sha256=record["pdf_sha256"],
                        )
                        if not rollback_ok:
                            raise CurrentReportApplicationError(
                                "application_restart_rollback_blocked",
                                "interrupted report bytes changed before restart rollback",
                                application_id=application_id,
                            )
                        record.update(
                            {
                                "state": (
                                    "apply_interrupted_rolled_back"
                                ),
                                "rollback_completed": True,
                                "rejected": True,
                                "accept_reject_required": False,
                                "error_code": (
                                    "application_interrupted"
                                ),
                            }
                        )
                    self._release_capacity(
                        project_id=project_id,
                        pipeline_job_id=pipeline_job_id,
                        analysis_job_id=analysis_job_id,
                        plan=plan,
                    )
                    record["capacity_reservation_released"] = True
                    _replace_record(record_path, record)
                    released += 1
                except BaseException as error:
                    blocked += 1
                    try:
                        record.update(
                            {
                                "state": (
                                    "apply_interrupted_rollback_blocked"
                                ),
                                "rollback_completed": False,
                                "error_code": (
                                    getattr(
                                        error,
                                        "code",
                                        "application_capacity_recovery_failed",
                                    )
                                ),
                            }
                        )
                        _replace_record(record_path, record)
                    except BaseException:
                        pass
                    raise CurrentReportApplicationError(
                        "application_capacity_recovery_failed",
                        "an interrupted current-report reservation could not be recovered",
                        application_id=str(
                            record.get("application_id") or ""
                        )
                        or None,
                    ) from error
        return {
            "restored": restored,
            "released": released,
            "blocked": blocked,
        }

    def _application_record_paths(self) -> tuple[Path, ...]:
        records: list[Path] = []
        try:
            projects = tuple(self._root.iterdir())
        except OSError as error:
            raise CurrentReportApplicationError(
                "application_capacity_recovery_failed",
                "the current-report ledger root is unavailable",
            ) from error
        for project_root in projects:
            try:
                UUID(project_root.name)
            except ValueError:
                continue
            project_root = _real_directory(
                project_root,
                create=False,
            )
            for pipeline_root in tuple(project_root.iterdir()):
                if pipeline_root.name == "_report-artifacts":
                    continue
                try:
                    UUID(pipeline_root.name)
                except ValueError:
                    continue
                pipeline_root = _real_directory(
                    pipeline_root,
                    create=False,
                )
                for application_root in tuple(
                    pipeline_root.iterdir()
                ):
                    if (
                        _APPLICATION_ID.fullmatch(
                            application_root.name
                        )
                        is None
                    ):
                        continue
                    application_root = _real_directory(
                        application_root,
                        create=False,
                    )
                    record = application_root / "record.json"
                    if _path_entry_exists(record):
                        records.append(record)
                    if len(records) > 10_000:
                        raise CurrentReportApplicationError(
                            "application_capacity_recovery_failed",
                            "the current-report recovery ledger exceeds its bound",
                        )
        return tuple(records)

    def apply(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        draft: CurrentReportDraft,
        authorization: CurrentReportApplyAuthorization,
        publisher: ExactReportPublisher
        | Callable[[], ExactReportPublisher],
    ) -> CurrentReportApplicationMetadata:
        self._validate_authorization(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            draft=draft,
            authorization=authorization,
        )
        application_id = (
            f"current-report-application-{uuid4().hex}"
        )
        ledger_root = self._application_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            application_id=application_id,
            create=True,
        )
        category_root, category_root_identity_sha256 = (
            self._resolve_artifact_category(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=draft.analysis_job_id,
                create_fallback=True,
            )
        )
        artifact_relative_root = f"applications/{application_id}"
        output_file_name = "current-report.hwpx"
        pdf_file_name = "current-report.pdf"
        render_root_name = "rendered-pages"
        output_path = (
            category_root
            / artifact_relative_root.replace("/", os.sep)
            / output_file_name
        )
        pdf_path = output_path.with_name(pdf_file_name)
        render_root = output_path.parent / render_root_name
        record_path = ledger_root / "record.json"
        capacity_plan = _current_report_capacity_plan(
            application_id=application_id,
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=draft.analysis_job_id,
            draft=draft,
            render_limits=self._render_limits,
        )
        capacity_enabled = (
            self._artifact_roots is not None
            and self._pipeline_job_resolver is not None
        )
        if self._artifact_root_resolver is not None and not capacity_enabled:
            raise CurrentReportApplicationError(
                "current_report_capacity_preflight_unavailable",
                "the current-report capacity preflight is unavailable",
                application_id=application_id,
            )
        record = {
            "schema_version": CURRENT_REPORT_APPLICATION_SCHEMA,
            "state": (
                "capacity_preflight_required"
                if capacity_enabled
                else "applying_com_reopen_save"
            ),
            "application_id": application_id,
            "project_id": str(project_id),
            "pipeline_job_id": str(pipeline_job_id),
            "analysis_job_id": str(draft.analysis_job_id),
            "draft_id": draft.draft_id,
            "approval_id": authorization.approval.approval_id,
            "binding_manifest_sha256": (
                draft.binding_manifest.binding_manifest_sha256
            ),
            "render_binding_sha256": draft.render_binding_sha256,
            "exact_plan_sha256": draft.exact_plan_sha256,
            "document_edit_plan_sha256": (
                draft.document_edit_plan.document_edit_plan_sha256
            ),
            "accept_reject_rollback_binding_sha256": (
                draft.document_edit_plan
                .accept_reject_rollback_binding_sha256
            ),
            "preview_sha256": (
                authorization.exact_preview_fingerprint.sha256
            ),
            "artifact_category": _ARTIFACT_CATEGORY,
            "artifact_relative_root": artifact_relative_root,
            "category_root_identity_sha256": (
                category_root_identity_sha256
            ),
            "output_path": output_file_name,
            "output_file_name": output_file_name,
            "output_sha256": None,
            "output_size_bytes": None,
            "pdf_path": pdf_file_name,
            "pdf_file_name": pdf_file_name,
            "pdf_sha256": None,
            "render_directory": None,
            "render_validation_status": "not_run",
            "rendered_page_count": None,
            "rendered_pages": [],
            "render_failure_codes": [],
            "capacity_plan": capacity_plan,
            "capacity_reservation": None,
            "capacity_reservation_released": (
                not capacity_enabled
            ),
            "output_reopen_structure_sha256": None,
            "raster_artifact_manifest_sha256": None,
            "com_publish_completed": False,
            "pdf_export_completed": False,
            "render_validation_completed": False,
            "rollback_completed": False,
            "accept_reject_required": False,
            "accepted": False,
            "rejected": False,
            "error_code": None,
        }
        with self._lock:
            _write_new_record(record_path, record)
        if capacity_enabled:
            try:
                reservation = self._reserve_capacity(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    analysis_job_id=draft.analysis_job_id,
                    category_root=category_root,
                    plan=capacity_plan,
                )
            except BaseException as error:
                code = (
                    "current_report_capacity_insufficient"
                    if "cannot fit" in str(error).casefold()
                    else "current_report_capacity_preflight_failed"
                )
                record.update(
                    {
                        "state": "capacity_preflight_blocked",
                        "capacity_reservation_released": True,
                        "error_code": code,
                    }
                )
                _replace_record(record_path, record)
                raise CurrentReportApplicationError(
                    code,
                    "the project output location cannot reserve current-report capacity",
                    application_id=application_id,
                ) from error
            record.update(
                {
                    "state": "applying_com_reopen_save",
                    "capacity_reservation": reservation,
                    "capacity_reservation_released": False,
                }
            )
            _replace_record(record_path, record)
        application_root = (
            category_root
            / artifact_relative_root.replace("/", os.sep)
        )
        output_path = application_root / output_file_name
        pdf_path = application_root / pdf_file_name
        render_root = application_root / render_root_name
        output_fingerprint: HwpDocumentFingerprint | None = None
        try:
            application_root = _relative_artifact_directory(
                category_root,
                artifact_relative_root,
                create=True,
                exclusive_leaf=True,
            )
            output_path = application_root / output_file_name
            pdf_path = application_root / pdf_file_name
            render_root = _real_directory(
                application_root / render_root_name,
                create=True,
            )
            if not hmac.compare_digest(
                _directory_identity_sha256(category_root),
                category_root_identity_sha256,
            ):
                raise CurrentReportApplicationError(
                    "application_artifact_root_changed",
                    "the report artifact root changed before COM apply",
                    application_id=application_id,
                )
            _relative_artifact_directory(
                category_root,
                artifact_relative_root,
                create=False,
            )
            active_publisher = publisher
            if (
                callable(publisher)
                and not hasattr(
                    publisher,
                    "publish_approved_exact_hwpx",
                )
            ):
                active_publisher = publisher()
            if (
                not hasattr(
                    active_publisher,
                    "publish_approved_exact_hwpx",
                )
                or not hasattr(
                    active_publisher,
                    "export_applied_hwpx_to_pdf",
                )
            ):
                raise CurrentReportApplicationError(
                    "exact_report_publisher_unavailable",
                    "the approval-gated HWP publisher is unavailable",
                    application_id=application_id,
                )
            published = active_publisher.publish_approved_exact_hwpx(
                approval_id=authorization.approval.approval_id,
                source_hwpx=draft.output_path,
                destination_hwpx=output_path,
            )
            output_fingerprint = fingerprint_document(output_path)
            if (
                published.output_path != output_path
                or published.size_bytes != output_fingerprint.size
                or not hmac.compare_digest(
                    published.sha256,
                    output_fingerprint.sha256,
                )
            ):
                raise CurrentReportApplicationError(
                    "com_publish_verification_failed",
                    "the COM-published exact HWPX failed verification",
                    application_id=application_id,
                )
            record.update(
                {
                    "state": "exporting_pdf",
                    "output_sha256": output_fingerprint.sha256,
                    "output_size_bytes": output_fingerprint.size,
                    "com_publish_completed": True,
                }
            )
            _replace_record(record_path, record)

            exported = Path(
                active_publisher.export_applied_hwpx_to_pdf(
                    approval_id=authorization.approval.approval_id,
                    source_hwpx=output_path,
                    destination_pdf=pdf_path,
                )
            )
            if exported != pdf_path:
                raise CurrentReportApplicationError(
                    "pdf_export_path_mismatch",
                    "the PDF exporter returned an unowned path",
                    application_id=application_id,
                )
            pdf_fingerprint = _stable_regular_file_fingerprint(
                pdf_path
            )
            if (
                pdf_fingerprint is None
                or pdf_fingerprint[1] <= 0
                or pdf_fingerprint[1]
                > self._render_limits.max_pdf_bytes
            ):
                raise CurrentReportApplicationError(
                    "pdf_export_verification_failed",
                    "the COM-exported PDF failed its bounded fingerprint",
                    application_id=application_id,
                )
            record.update(
                {
                    "state": "validating_48_page_render",
                    "pdf_export_completed": True,
                    "pdf_sha256": pdf_fingerprint[0],
                }
            )
            _replace_record(record_path, record)
            validation = validate_saved_hwpx_pdf_render(
                output_hwpx_fingerprint=output_fingerprint,
                exported_pdf_path=pdf_path,
                binding_manifest=draft.render_binding,
                owned_render_root=render_root,
                limits=self._render_limits,
            )
            self._bind_validation(
                record,
                validation,
                render_root=render_root,
            )
            if not hmac.compare_digest(
                _directory_identity_sha256(category_root),
                category_root_identity_sha256,
            ):
                raise CurrentReportApplicationError(
                    "application_artifact_root_changed",
                    "the report artifact root changed during render validation",
                    application_id=application_id,
                )
            _relative_artifact_directory(
                category_root,
                artifact_relative_root,
                create=False,
            )
            if validation.passed:
                record.update(
                    {
                        "state": (
                            "render_validated_accept_or_reject_required"
                        ),
                        "accept_reject_required": True,
                    }
                )
            else:
                rollback_ok = self._rollback_artifacts(
                    application_root=application_root,
                    record=record,
                    output_path=output_path,
                    output_sha256=output_fingerprint.sha256,
                    pdf_path=pdf_path,
                    pdf_sha256=record["pdf_sha256"],
                )
                record.update(
                    {
                        "state": (
                            "render_validation_failed_rolled_back"
                            if rollback_ok
                            else "render_validation_failed_rollback_blocked"
                        ),
                        "rollback_completed": rollback_ok,
                        "rejected": True,
                        "error_code": "render_validation_failed",
                    }
                )
            if capacity_enabled:
                # The reserved write stage ends once COM, PDF export, and
                # 48-page rendering either validate or roll back. The later
                # accept/reject decision creates no report artifact bytes;
                # publication owns its own destination preflight.
                self._release_capacity(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    analysis_job_id=draft.analysis_job_id,
                    plan=capacity_plan,
                )
                record["capacity_reservation_released"] = True
            _replace_record(record_path, record)
            return _metadata(record)
        except BaseException as error:
            rollback_ok = self._rollback_artifacts(
                application_root=application_root,
                record=record,
                output_path=output_path,
                output_sha256=(
                    None
                    if output_fingerprint is None
                    else output_fingerprint.sha256
                ),
                pdf_path=pdf_path,
                pdf_sha256=_sha256_if_regular(pdf_path),
            )
            record.update(
                {
                    "state": (
                        "apply_failed_rolled_back"
                        if rollback_ok
                        else "apply_failed_rollback_blocked"
                    ),
                    "rollback_completed": rollback_ok,
                    "rejected": True,
                    "error_code": (
                        error.code
                        if isinstance(
                            error,
                            CurrentReportApplicationError,
                        )
                        else "exact_report_apply_failed"
                    ),
                }
            )
            if capacity_enabled and not record.get(
                "capacity_reservation_released"
            ):
                try:
                    self._release_capacity(
                        project_id=project_id,
                        pipeline_job_id=pipeline_job_id,
                        analysis_job_id=draft.analysis_job_id,
                        plan=capacity_plan,
                    )
                    record["capacity_reservation_released"] = True
                except BaseException:
                    rollback_ok = False
                    record.update(
                        {
                            "state": "apply_failed_rollback_blocked",
                            "rollback_completed": False,
                            "error_code": (
                                "current_report_capacity_release_failed"
                            ),
                        }
                    )
            try:
                _replace_record(record_path, record)
            except BaseException:
                pass
            if isinstance(error, CurrentReportApplicationError):
                raise
            raise CurrentReportApplicationError(
                "exact_report_apply_failed",
                "the exact report apply failed and was rolled back",
                application_id=application_id,
            ) from error

    def decide(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
        accept: bool,
        document_edit_plan_sha256: str,
        accept_reject_rollback_binding_sha256: str,
    ) -> CurrentReportApplicationMetadata:
        if not isinstance(accept, bool):
            raise CurrentReportApplicationError(
                "decision_invalid",
                "an explicit accept or reject decision is required",
            )
        ledger_root = self._application_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            application_id=application_id,
            create=False,
        )
        record_path = ledger_root / "record.json"
        with self._lock:
            record = _load_record(record_path)
            artifact_root = (
                self._artifact_application_root_from_record(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    application_id=application_id,
                    record=record,
                    create=False,
                )
            )
            self._validate_decision_binding(
                record=record,
                document_edit_plan_sha256=(
                    document_edit_plan_sha256
                ),
                accept_reject_rollback_binding_sha256=(
                    accept_reject_rollback_binding_sha256
                ),
                application_id=application_id,
            )
            if (
                accept
                and record["state"]
                == "accepted_render_validated_report"
                and record["accept_reject_required"] is False
                and record["accepted"] is True
                and record["rejected"] is False
                and record["rollback_completed"] is False
            ):
                self.resolve_accepted_publication_source(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    application_id=application_id,
                )
                return _metadata(record)
            if (
                record["state"]
                != "render_validated_accept_or_reject_required"
                or record["accept_reject_required"] is not True
                or record["accepted"] is not False
                or record["rejected"] is not False
            ):
                raise CurrentReportApplicationError(
                    "decision_state_conflict",
                    "the exact report is not awaiting a decision",
                    application_id=application_id,
                )
            output_path = _owned_path(
                artifact_root / str(record["output_path"]),
                root=artifact_root,
                suffix=".hwpx",
            )
            pdf_path = _owned_path(
                artifact_root / str(record["pdf_path"]),
                root=artifact_root,
                suffix=".pdf",
            )
            if accept:
                if (
                    _sha256_if_regular(output_path)
                    != record["output_sha256"]
                    or _sha256_if_regular(pdf_path)
                    != record["pdf_sha256"]
                ):
                    raise CurrentReportApplicationError(
                        "accepted_output_changed",
                        "the validated report changed before acceptance",
                        application_id=application_id,
                    )
                try:
                    _prepare_owned_render_artifacts(
                        application_root=artifact_root,
                        record=record,
                        limits=self._render_limits,
                        require_complete=True,
                        verify_hashes=True,
                    )
                except ValueError as error:
                    raise CurrentReportApplicationError(
                        "accepted_render_artifacts_changed",
                        (
                            "the validated 48-page render changed "
                            "before acceptance"
                        ),
                        application_id=application_id,
                    ) from error
                record.update(
                    {
                        "state": "accepted_render_validated_report",
                        "accept_reject_required": False,
                        "accepted": True,
                    }
                )
            else:
                rollback_ok = self._rollback_artifacts(
                    application_root=artifact_root,
                    record=record,
                    output_path=output_path,
                    output_sha256=record["output_sha256"],
                    pdf_path=pdf_path,
                    pdf_sha256=record["pdf_sha256"],
                )
                if not rollback_ok:
                    raise CurrentReportApplicationError(
                        "reject_rollback_blocked",
                        "the rejected report could not be safely rolled back",
                        application_id=application_id,
                    )
                record.update(
                    {
                        "state": "rejected_report_rolled_back",
                        "accept_reject_required": False,
                        "rollback_completed": True,
                        "rejected": True,
                    }
                )
            _replace_record(record_path, record)
            return _metadata(record)

    def metadata(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
    ) -> CurrentReportApplicationMetadata:
        ledger_root = self._application_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            application_id=application_id,
            create=False,
        )
        with self._lock:
            record = _load_record(ledger_root / "record.json")
            self._artifact_application_root_from_record(
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                application_id=application_id,
                record=record,
                create=False,
            )
            return _metadata(record)

    def resolve_accepted_publication_source(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
    ) -> AcceptedCurrentReportPublicationSource:
        """Re-verify one accepted 48-page application for publication."""

        ledger_root = self._application_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            application_id=application_id,
            create=False,
        )
        with self._lock:
            record = _load_record(ledger_root / "record.json")
            artifact_root = (
                self._artifact_application_root_from_record(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    application_id=application_id,
                    record=record,
                    create=False,
                )
            )
            if (
                record["state"]
                != "accepted_render_validated_report"
                or record["accepted"] is not True
                or record["rejected"] is not False
                or record["rollback_completed"] is not False
                or record["accept_reject_required"] is not False
            ):
                raise CurrentReportApplicationError(
                    "accepted_publication_source_unavailable",
                    "the report is not an accepted publication source",
                    application_id=application_id,
                )
            output_path = _owned_path(
                artifact_root / str(record["output_path"]),
                root=artifact_root,
                suffix=".hwpx",
            )
            pdf_path = _owned_path(
                artifact_root / str(record["pdf_path"]),
                root=artifact_root,
                suffix=".pdf",
            )
            output = _stable_regular_file_fingerprint(output_path)
            pdf = _stable_regular_file_fingerprint(pdf_path)
            try:
                _prepare_owned_render_artifacts(
                    application_root=artifact_root,
                    record=record,
                    limits=self._render_limits,
                    require_complete=True,
                    verify_hashes=True,
                )
            except ValueError as error:
                raise CurrentReportApplicationError(
                    "accepted_publication_source_changed",
                    (
                        "the accepted 48-page report changed "
                        "before publication"
                    ),
                    application_id=application_id,
                ) from error
            if (
                output is None
                or pdf is None
                or not hmac.compare_digest(
                    output[0],
                    str(record["output_sha256"]),
                )
                or not hmac.compare_digest(
                    pdf[0],
                    str(record["pdf_sha256"]),
                )
                or not _positive_int(record["output_size_bytes"])
                or output[1] != record["output_size_bytes"]
                or _SHA256.fullmatch(
                    str(record["output_reopen_structure_sha256"])
                )
                is None
                or _SHA256.fullmatch(
                    str(record["raster_artifact_manifest_sha256"])
                )
                is None
            ):
                raise CurrentReportApplicationError(
                    "accepted_publication_source_changed",
                    "the accepted report changed before publication",
                    application_id=application_id,
                )
            try:
                analysis_job_id = UUID(record["analysis_job_id"])
            except (TypeError, ValueError) as error:
                raise CurrentReportApplicationError(
                    "application_record_corrupt",
                    "the accepted report identity is invalid",
                    application_id=application_id,
                ) from error
            return AcceptedCurrentReportPublicationSource(
                application_id=application_id,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=analysis_job_id,
                draft_id=record["draft_id"],
                output_path=output_path,
                output_file_name=record["output_file_name"],
                output_sha256=output[0],
                output_size_bytes=output[1],
                pdf_path=pdf_path,
                pdf_file_name=record["pdf_file_name"],
                pdf_sha256=pdf[0],
                pdf_size_bytes=pdf[1],
                binding_manifest_sha256=record[
                    "binding_manifest_sha256"
                ],
                exact_plan_sha256=record["exact_plan_sha256"],
                document_edit_plan_sha256=record[
                    "document_edit_plan_sha256"
                ],
                accept_reject_rollback_binding_sha256=record[
                    "accept_reject_rollback_binding_sha256"
                ],
                output_reopen_structure_sha256=record[
                    "output_reopen_structure_sha256"
                ],
                raster_artifact_manifest_sha256=record[
                    "raster_artifact_manifest_sha256"
                ],
                rendered_page_count=record["rendered_page_count"],
            )

    def rollback_accepted(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
        document_edit_plan_sha256: str,
        accept_reject_rollback_binding_sha256: str,
    ) -> CurrentReportApplicationMetadata:
        """Rollback one previously accepted, still hash-identical report."""

        ledger_root = self._application_root(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            application_id=application_id,
            create=False,
        )
        record_path = ledger_root / "record.json"
        with self._lock:
            record = _load_record(record_path)
            artifact_root = (
                self._artifact_application_root_from_record(
                    project_id=project_id,
                    pipeline_job_id=pipeline_job_id,
                    application_id=application_id,
                    record=record,
                    create=False,
                )
            )
            self._validate_decision_binding(
                record=record,
                document_edit_plan_sha256=(
                    document_edit_plan_sha256
                ),
                accept_reject_rollback_binding_sha256=(
                    accept_reject_rollback_binding_sha256
                ),
                application_id=application_id,
            )
            if (
                record["state"] == "accepted_report_rolled_back"
                and record["accepted"] is True
                and record["rejected"] is False
                and record["rollback_completed"] is True
                and record["accept_reject_required"] is False
            ):
                output_path = _owned_path(
                    artifact_root / str(record["output_path"]),
                    root=artifact_root,
                    suffix=".hwpx",
                )
                pdf_path = _owned_path(
                    artifact_root / str(record["pdf_path"]),
                    root=artifact_root,
                    suffix=".pdf",
                )
                render_root = artifact_root / "rendered-pages"
                if any(
                    _path_entry_exists(path)
                    for path in (output_path, pdf_path, render_root)
                ):
                    raise CurrentReportApplicationError(
                        "accepted_rollback_state_conflict",
                        (
                            "the rolled-back report still has "
                            "owned artifacts"
                        ),
                        application_id=application_id,
                    )
                return _metadata(record)
            if (
                record["state"]
                != "accepted_render_validated_report"
                or record["accepted"] is not True
                or record["rejected"] is not False
                or record["rollback_completed"] is not False
                or record["accept_reject_required"] is not False
            ):
                raise CurrentReportApplicationError(
                    "accepted_rollback_state_conflict",
                    "the exact report is not an accepted rollback candidate",
                    application_id=application_id,
                )
            output_path = _owned_path(
                artifact_root / str(record["output_path"]),
                root=artifact_root,
                suffix=".hwpx",
            )
            pdf_path = _owned_path(
                artifact_root / str(record["pdf_path"]),
                root=artifact_root,
                suffix=".pdf",
            )
            rollback_ok = self._rollback_artifacts(
                application_root=artifact_root,
                record=record,
                output_path=output_path,
                output_sha256=record["output_sha256"],
                pdf_path=pdf_path,
                pdf_sha256=record["pdf_sha256"],
            )
            if not rollback_ok:
                raise CurrentReportApplicationError(
                    "accepted_rollback_blocked",
                    "the accepted report changed and could not be rolled back",
                    application_id=application_id,
                )
            record.update(
                {
                    "state": "accepted_report_rolled_back",
                    "rollback_completed": True,
                }
            )
            _replace_record(record_path, record)
            return _metadata(record)

    def _application_root(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
        create: bool,
    ) -> Path:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(pipeline_job_id, UUID)
            or not isinstance(application_id, str)
            or _APPLICATION_ID.fullmatch(application_id) is None
        ):
            raise CurrentReportApplicationError(
                "application_identity_invalid",
                "the current-report application identity is invalid",
            )
        current = self._root
        names = (
            str(project_id),
            str(pipeline_job_id),
            application_id,
        )
        for index, name in enumerate(names):
            child = current / name
            if create:
                try:
                    if index == len(names) - 1:
                        child.mkdir()
                    else:
                        child.mkdir(exist_ok=True)
                except OSError as error:
                    raise CurrentReportApplicationError(
                        "application_scope_unavailable",
                        (
                            "the current-report application scope "
                            "is unavailable"
                        ),
                    ) from error
            try:
                value = os.lstat(child)
            except OSError as error:
                raise CurrentReportApplicationError(
                    (
                        "application_not_found"
                        if index == len(names) - 1
                        else "application_scope_unavailable"
                    ),
                    "the current-report application was not found",
                ) from error
            if (
                not stat.S_ISDIR(value.st_mode)
                or child.is_symlink()
                or _is_reparse(value)
                or child.parent != current
            ):
                raise CurrentReportApplicationError(
                    "application_scope_unavailable",
                    "the current-report application scope is unsafe",
                )
            current = child
        return current

    def _resolve_artifact_category(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        create_fallback: bool,
    ) -> tuple[Path, str]:
        try:
            if self._artifact_root_resolver is None:
                category_root = _real_directory(
                    self._root
                    / str(project_id)
                    / str(pipeline_job_id)
                    / "_report-artifacts",
                    create=create_fallback,
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
                    create=False,
                )
        except CurrentReportApplicationError:
            raise
        except Exception as error:
            raise CurrentReportApplicationError(
                "application_artifact_root_unavailable",
                "the PipelineJob report artifact category is unavailable",
            ) from error
        return category_root, _directory_identity_sha256(category_root)

    def _artifact_application_root_from_record(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        application_id: str,
        record: Mapping[str, Any],
        create: bool,
    ) -> Path:
        try:
            analysis_job_id = UUID(str(record["analysis_job_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise CurrentReportApplicationError(
                "application_record_corrupt",
                "the current-report analysis identity is invalid",
                application_id=application_id,
            ) from error
        expected_relative = f"applications/{application_id}"
        if (
            record.get("artifact_category") != _ARTIFACT_CATEGORY
            or record.get("artifact_relative_root")
            != expected_relative
            or _SHA256.fullmatch(
                str(record.get("category_root_identity_sha256"))
            )
            is None
        ):
            raise CurrentReportApplicationError(
                "application_artifact_locator_invalid",
                "the current-report artifact locator is invalid",
                application_id=application_id,
            )
        category_root, identity = self._resolve_artifact_category(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            create_fallback=False,
        )
        if not hmac.compare_digest(
            identity,
            str(record["category_root_identity_sha256"]),
        ):
            raise CurrentReportApplicationError(
                "application_artifact_root_changed",
                "the PipelineJob report artifact root changed",
                application_id=application_id,
            )
        return _relative_artifact_directory(
            category_root,
            expected_relative,
            create=create,
        )

    def _capacity_binding(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        category_root: Path | None = None,
    ) -> Mapping[str, Any]:
        if (
            self._artifact_roots is None
            or self._pipeline_job_resolver is None
        ):
            raise CurrentReportApplicationError(
                "current_report_capacity_preflight_unavailable",
                "the current-report capacity registry is unavailable",
            )
        try:
            job = self._pipeline_job_resolver(
                project_id,
                pipeline_job_id,
            )
            if (
                not isinstance(job, Mapping)
                or UUID(str(job.get("project_id"))) != project_id
                or UUID(str(job.get("job_id"))) != pipeline_job_id
            ):
                raise ValueError("PipelineJob scope changed")
            stages = job.get("stages")
            if (
                not isinstance(stages, (list, tuple))
                or not any(
                    isinstance(stage, Mapping)
                    and isinstance(stage.get("checkpoint"), Mapping)
                    and stage["checkpoint"].get("analysis_job_id")
                    == str(analysis_job_id)
                    for stage in stages
                )
            ):
                raise ValueError("analysis does not belong to PipelineJob")
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
                raise ValueError("PipelineJob artifact binding is invalid")
            resolved = self._artifact_roots.resolve_job_binding(
                project_id,
                binding,
            )
            if category_root is not None:
                expected = (
                    resolved.root
                    / "pipeline-jobs"
                    / str(pipeline_job_id)
                    / _ARTIFACT_CATEGORY
                )
                if not _same_path(expected, category_root):
                    raise ValueError(
                        "capacity and artifact roots are on different scopes"
                    )
            return binding
        except CurrentReportApplicationError:
            raise
        except Exception as error:
            raise CurrentReportApplicationError(
                "current_report_capacity_binding_invalid",
                "the current-report capacity binding is invalid",
            ) from error

    def _reserve_capacity(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        category_root: Path,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        _validate_capacity_plan(plan)
        binding = self._capacity_binding(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            category_root=category_root,
        )
        assert self._artifact_roots is not None
        return self._artifact_roots.adjust_capacity_reservation(
            project_id,
            binding,
            reservation_id=str(plan["reservation_id"]),
            reserved_bytes=int(plan["estimated_bytes"]),
            safety_reserve_bytes=int(
                plan["safety_reserve_bytes"]
            ),
        )

    def _release_capacity(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        plan: Mapping[str, Any],
    ) -> None:
        _validate_capacity_plan(plan)
        binding = self._capacity_binding(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
        )
        assert self._artifact_roots is not None
        self._artifact_roots.release_capacity_reservation(
            project_id,
            binding,
            reservation_id=str(plan["reservation_id"]),
        )

    @staticmethod
    def _validate_authorization(
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        draft: CurrentReportDraft,
        authorization: CurrentReportApplyAuthorization,
    ) -> None:
        if (
            not isinstance(draft, CurrentReportDraft)
            or not isinstance(
                authorization,
                CurrentReportApplyAuthorization,
            )
            or draft.project_id != project_id
            or draft.pipeline_job_id != pipeline_job_id
            or authorization.project_id != str(project_id)
            or authorization.pipeline_job_id
            != str(pipeline_job_id)
            or authorization.analysis_job_id
            != str(draft.analysis_job_id)
            or authorization.binding_manifest_sha256
            != draft.binding_manifest.binding_manifest_sha256
            or authorization.exact_plan_sha256
            != draft.exact_plan_sha256
            or draft.exact_plan_sha256
            != draft.document_edit_plan.exact_plan_sha256
            or draft.binding_manifest.binding_manifest_sha256
            != draft.document_edit_plan.binding_manifest_sha256
            or authorization.document_edit_plan_sha256
            != draft.document_edit_plan.document_edit_plan_sha256
            or authorization
            .accept_reject_rollback_binding_sha256
            != draft.document_edit_plan
            .accept_reject_rollback_binding_sha256
            or authorization.approval.granted is not True
            or authorization.approval.output_hwpx_sha256
            != draft.writer_result.output_document_sha256
            or authorization.exact_preview_fingerprint.path
            != draft.output_path
            or authorization.exact_preview_fingerprint.sha256
            != draft.writer_result.output_document_sha256
        ):
            raise CurrentReportApplicationError(
                "apply_authorization_invalid",
                "the one-shot exact report authorization is invalid",
            )
        try:
            validate_current_report_document_edit_plan(
                draft.document_edit_plan,
                binding_manifest=draft.binding_manifest,
                exact_plan=draft.exact_plan,
                project_id=project_id,
                pipeline_job_id=pipeline_job_id,
                analysis_job_id=draft.analysis_job_id,
                source_sha256=draft.binding_manifest.source_sha256,
            )
        except CurrentReportDocumentEditPlanError as error:
            raise CurrentReportApplicationError(
                "apply_authorization_invalid",
                "the exact report hash quartet is invalid",
            ) from error
        current = fingerprint_document(draft.output_path)
        expected = authorization.exact_preview_fingerprint
        if (
            current.device != expected.device
            or current.inode != expected.inode
            or current.size != expected.size
            or current.modified_ns != expected.modified_ns
            or not hmac.compare_digest(current.sha256, expected.sha256)
        ):
            raise CurrentReportApplicationError(
                "exact_preview_changed",
                "the approved exact preview changed before COM apply",
            )

    @staticmethod
    def _validate_decision_binding(
        *,
        record: dict[str, Any],
        document_edit_plan_sha256: str,
        accept_reject_rollback_binding_sha256: str,
        application_id: str,
    ) -> None:
        if (
            not isinstance(document_edit_plan_sha256, str)
            or _SHA256.fullmatch(document_edit_plan_sha256) is None
            or not isinstance(
                accept_reject_rollback_binding_sha256, str
            )
            or _SHA256.fullmatch(
                accept_reject_rollback_binding_sha256
            )
            is None
            or not hmac.compare_digest(
                document_edit_plan_sha256,
                record["document_edit_plan_sha256"],
            )
            or not hmac.compare_digest(
                accept_reject_rollback_binding_sha256,
                record[
                    "accept_reject_rollback_binding_sha256"
                ],
            )
        ):
            raise CurrentReportApplicationError(
                "decision_binding_invalid",
                "the exact report decision crossed its reviewed edit plan",
                application_id=application_id,
            )

    @staticmethod
    def _bind_validation(
        record: dict[str, Any],
        validation: HwpRenderValidationResult,
        *,
        render_root: Path,
    ) -> None:
        render_directory = _validated_render_directory_address(
            validation.render_directory,
            render_root=render_root,
        )
        pages = tuple(validation.pages)
        rendered_pages = _rendered_page_records(
            pages,
            render_directory=render_directory,
        )
        calculated_manifest = rendered_page_manifest_sha256(pages)
        if (
            calculated_manifest
            != validation.raster_artifact_manifest_sha256
        ):
            raise CurrentReportApplicationError(
                "render_validation_evidence_invalid",
                "the render validator returned an inconsistent page manifest",
                application_id=record["application_id"],
            )
        if (
            validation.exported_pdf_sha256 is not None
            and (
                _SHA256.fullmatch(
                    str(validation.exported_pdf_sha256)
                )
                is None
                or not hmac.compare_digest(
                    str(validation.exported_pdf_sha256),
                    str(record.get("pdf_sha256") or ""),
                )
            )
        ):
            raise CurrentReportApplicationError(
                "render_validation_evidence_invalid",
                "the rendered PDF changed after COM export",
                application_id=record["application_id"],
            )
        if (
            not isinstance(validation.binding_manifest_sha256, str)
            or _SHA256.fullmatch(
                validation.binding_manifest_sha256
            )
            is None
            or not hmac.compare_digest(
                validation.binding_manifest_sha256,
                record["render_binding_sha256"],
            )
        ):
            raise CurrentReportApplicationError(
                "render_validation_binding_conflict",
                (
                    "the 48-page render validation crossed its "
                    "current-report binding"
                ),
                application_id=record["application_id"],
            )
        if validation.passed and (
            validation.status != "validated_48_page_render"
            or validation.failure_codes
            or validation.rendered_page_count != EXPECTED_PAGE_COUNT
            or render_directory is None
            or len(rendered_pages) != EXPECTED_PAGE_COUNT
            or validation.output_hwpx_sha256
            != record["output_sha256"]
            or _SHA256.fullmatch(
                str(validation.exported_pdf_sha256)
            )
            is None
            or _SHA256.fullmatch(
                str(validation.output_reopen_structure_sha256)
            )
            is None
            or _SHA256.fullmatch(
                str(validation.binding_manifest_sha256)
            )
            is None
            or _SHA256.fullmatch(str(calculated_manifest)) is None
        ):
            raise CurrentReportApplicationError(
                "render_validation_evidence_invalid",
                "the passed render evidence is incomplete or inconsistent",
                application_id=record["application_id"],
            )
        record.update(
            {
                "pdf_sha256": (
                    validation.exported_pdf_sha256
                    or record["pdf_sha256"]
                ),
                "render_directory": (
                    None
                    if render_directory is None
                    else render_directory.name
                ),
                "render_validation_status": validation.status,
                "rendered_page_count": validation.rendered_page_count,
                "rendered_pages": rendered_pages,
                "render_failure_codes": list(
                    validation.failure_codes
                ),
                "output_reopen_structure_sha256": (
                    validation.output_reopen_structure_sha256
                ),
                "raster_artifact_manifest_sha256": (
                    validation.raster_artifact_manifest_sha256
                ),
                "render_validation_completed": True,
            }
        )

    def _rollback_artifacts(
        self,
        *,
        application_root: Path,
        record: dict[str, Any],
        output_path: Path,
        output_sha256: str | None,
        pdf_path: Path,
        pdf_sha256: str | None,
    ) -> bool:
        try:
            if not _file_is_safe_to_remove(
                output_path,
                output_sha256,
            ) or not _file_is_safe_to_remove(
                pdf_path,
                pdf_sha256,
            ):
                return False
            render_artifacts = _prepare_owned_render_artifacts(
                application_root=application_root,
                record=record,
                limits=self._render_limits,
                require_complete=False,
                verify_hashes=True,
            )
        except (OSError, ValueError):
            return False

        output_removed = _remove_prevalidated_file(
            output_path,
            output_sha256,
        )
        pdf_removed = _remove_prevalidated_file(
            pdf_path,
            pdf_sha256,
        )
        render_removed = _remove_prevalidated_render_artifacts(
            render_artifacts
        )
        return output_removed and pdf_removed and render_removed


def _metadata(record: dict[str, Any]) -> CurrentReportApplicationMetadata:
    return CurrentReportApplicationMetadata(
        application_id=record["application_id"],
        state=record["state"],
        project_id=record["project_id"],
        pipeline_job_id=record["pipeline_job_id"],
        analysis_job_id=record["analysis_job_id"],
        draft_id=record["draft_id"],
        binding_manifest_sha256=record[
            "binding_manifest_sha256"
        ],
        exact_plan_sha256=record["exact_plan_sha256"],
        document_edit_plan_sha256=record[
            "document_edit_plan_sha256"
        ],
        accept_reject_rollback_binding_sha256=record[
            "accept_reject_rollback_binding_sha256"
        ],
        output_file_name=record["output_file_name"],
        output_sha256=record["output_sha256"],
        pdf_file_name=record["pdf_file_name"],
        pdf_sha256=record["pdf_sha256"],
        render_validation_status=record[
            "render_validation_status"
        ],
        rendered_page_count=record["rendered_page_count"],
        render_failure_codes=tuple(record["render_failure_codes"]),
        output_reopen_structure_sha256=record[
            "output_reopen_structure_sha256"
        ],
        raster_artifact_manifest_sha256=record[
            "raster_artifact_manifest_sha256"
        ],
        com_publish_completed=record["com_publish_completed"],
        pdf_export_completed=record["pdf_export_completed"],
        render_validation_completed=record[
            "render_validation_completed"
        ],
        rollback_completed=record["rollback_completed"],
        accept_reject_required=record["accept_reject_required"],
        accepted=record["accepted"],
        rejected=record["rejected"],
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


_CAPACITY_PLAN_KEYS = {
    "schema_version",
    "application_id",
    "project_id",
    "pipeline_job_id",
    "analysis_job_id",
    "artifact_category",
    "draft_id",
    "draft_output_sha256",
    "draft_output_size_bytes",
    "com_hwpx_bytes_upper_bound",
    "pdf_bytes_upper_bound",
    "rendered_png_bytes_upper_bound",
    "working_overhead_bytes",
    "estimated_bytes",
    "safety_reserve_bytes",
    "reservation_id",
    "path_exposed",
    "capacity_plan_sha256",
}


def _current_report_capacity_plan(
    *,
    application_id: str,
    project_id: UUID,
    pipeline_job_id: UUID,
    analysis_job_id: UUID,
    draft: CurrentReportDraft,
    render_limits: RenderValidationLimits,
) -> dict[str, Any]:
    try:
        draft_size = int(
            os.stat(draft.output_path, follow_symlinks=False).st_size
        )
        reservation_uuid = UUID(
            application_id.removeprefix(
                "current-report-application-"
            )
        )
    except (OSError, TypeError, ValueError) as error:
        raise CurrentReportApplicationError(
            "current_report_capacity_plan_invalid",
            "the exact draft size cannot be capacity-bound",
            application_id=application_id,
        ) from error
    if (
        draft_size <= 0
        or draft_size > 2 * 1024**4
        or _SHA256.fullmatch(
            draft.writer_result.output_document_sha256
        )
        is None
    ):
        raise CurrentReportApplicationError(
            "current_report_capacity_plan_invalid",
            "the exact draft exceeds the current-report capacity bound",
            application_id=application_id,
        )
    # COM may write a temporary/package copy while saving. Reserve four
    # draft-sized HWPX copies, both validator hard ceilings, and a fixed
    # workspace allowance. The registry enforces the additional 4 GiB floor.
    com_hwpx_upper = max(4 * draft_size, 256 * 1024**2)
    estimated = (
        com_hwpx_upper
        + int(render_limits.max_pdf_bytes)
        + int(render_limits.max_rendered_total_bytes)
        + _CURRENT_REPORT_WORKING_OVERHEAD_BYTES
    )
    if estimated <= 0 or estimated > 8 * 1024**4:
        raise CurrentReportApplicationError(
            "current_report_capacity_plan_invalid",
            "the current-report capacity estimate is outside its hard bound",
            application_id=application_id,
        )
    payload = {
        "schema_version": CURRENT_REPORT_CAPACITY_PLAN_SCHEMA,
        "application_id": application_id,
        "project_id": str(project_id),
        "pipeline_job_id": str(pipeline_job_id),
        "analysis_job_id": str(analysis_job_id),
        "artifact_category": _ARTIFACT_CATEGORY,
        "draft_id": draft.draft_id,
        "draft_output_sha256": (
            draft.writer_result.output_document_sha256
        ),
        "draft_output_size_bytes": draft_size,
        "com_hwpx_bytes_upper_bound": com_hwpx_upper,
        "pdf_bytes_upper_bound": int(
            render_limits.max_pdf_bytes
        ),
        "rendered_png_bytes_upper_bound": int(
            render_limits.max_rendered_total_bytes
        ),
        "working_overhead_bytes": (
            _CURRENT_REPORT_WORKING_OVERHEAD_BYTES
        ),
        "estimated_bytes": estimated,
        "safety_reserve_bytes": (
            CURRENT_REPORT_CAPACITY_SAFETY_RESERVE_BYTES
        ),
        "reservation_id": f"current-report:{reservation_uuid}",
        "path_exposed": False,
    }
    return {
        **payload,
        "capacity_plan_sha256": hashlib.sha256(
            _canonical(payload)
        ).hexdigest(),
    }


def _validate_capacity_plan(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _CAPACITY_PLAN_KEYS:
        raise CurrentReportApplicationError(
            "current_report_capacity_plan_invalid",
            "the current-report capacity plan is invalid",
        )
    without_hash = {
        key: item
        for key, item in value.items()
        if key != "capacity_plan_sha256"
    }
    try:
        reservation = str(value["reservation_id"])
        reservation_uuid = UUID(
            reservation.removeprefix("current-report:")
        )
    except (TypeError, ValueError) as error:
        raise CurrentReportApplicationError(
            "current_report_capacity_plan_invalid",
            "the current-report reservation identity is invalid",
        ) from error
    numeric_keys = (
        "draft_output_size_bytes",
        "com_hwpx_bytes_upper_bound",
        "pdf_bytes_upper_bound",
        "rendered_png_bytes_upper_bound",
        "working_overhead_bytes",
        "estimated_bytes",
        "safety_reserve_bytes",
    )
    numeric_values = {
        key: value.get(key) for key in numeric_keys
    }
    components_are_valid = all(
        isinstance(item, int)
        and not isinstance(item, bool)
        and item > 0
        for item in numeric_values.values()
    )
    expected_estimated = (
        int(numeric_values["com_hwpx_bytes_upper_bound"] or 0)
        + int(numeric_values["pdf_bytes_upper_bound"] or 0)
        + int(
            numeric_values[
                "rendered_png_bytes_upper_bound"
            ]
            or 0
        )
        + int(numeric_values["working_overhead_bytes"] or 0)
    )
    if (
        value.get("schema_version")
        != CURRENT_REPORT_CAPACITY_PLAN_SCHEMA
        or value.get("artifact_category") != _ARTIFACT_CATEGORY
        or value.get("path_exposed") is not False
        or reservation != f"current-report:{reservation_uuid}"
        or _CURRENT_REPORT_RESERVATION_ID.fullmatch(
            reservation
        )
        is None
        or not components_are_valid
        or value.get("estimated_bytes") != expected_estimated
        or not 0 < int(value["estimated_bytes"]) <= 8 * 1024**4
        or value.get("safety_reserve_bytes")
        != CURRENT_REPORT_CAPACITY_SAFETY_RESERVE_BYTES
        or _SHA256.fullmatch(
            str(value.get("draft_output_sha256"))
        )
        is None
        or _SHA256.fullmatch(
            str(value.get("capacity_plan_sha256"))
        )
        is None
        or not hmac.compare_digest(
            str(value["capacity_plan_sha256"]),
            hashlib.sha256(_canonical(without_hash)).hexdigest(),
        )
    ):
        raise CurrentReportApplicationError(
            "current_report_capacity_plan_invalid",
            "the current-report capacity plan failed verification",
        )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(
        os.path.abspath(os.fspath(left))
    ) == os.path.normcase(os.path.abspath(os.fspath(right)))


def _directory_identity_sha256(path: Path) -> str:
    directory = _real_directory(Path(path), create=False)
    try:
        value = os.stat(directory, follow_symlinks=False)
    except OSError as error:
        raise CurrentReportApplicationError(
            "application_artifact_root_unavailable",
            "the report artifact category identity is unavailable",
        ) from error
    return hashlib.sha256(
        _canonical(
            {
                "device": int(value.st_dev),
                "inode": int(value.st_ino),
                "category": _ARTIFACT_CATEGORY,
            }
        )
    ).hexdigest()


def _relative_artifact_directory(
    category_root: Path,
    relative: str,
    *,
    create: bool,
    exclusive_leaf: bool = False,
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
        raise CurrentReportApplicationError(
            "application_artifact_locator_invalid",
            "the current-report artifact locator is invalid",
        )
    current = category_root
    for index, part in enumerate(parts):
        child = current / part
        if create:
            try:
                child.mkdir(
                    exist_ok=not (
                        exclusive_leaf
                        and index == len(parts) - 1
                    )
                )
            except OSError as error:
                raise CurrentReportApplicationError(
                    "application_artifact_root_unavailable",
                    "the current-report artifact root is unavailable",
                ) from error
        current = _real_directory(child, create=False)
    try:
        current.relative_to(category_root)
    except ValueError as error:
        raise CurrentReportApplicationError(
            "application_artifact_locator_invalid",
            "the current-report artifact locator escaped its category",
        ) from error
    return current


def _record_with_hash(record: dict[str, Any]) -> dict[str, Any]:
    value = {
        key: item
        for key, item in record.items()
        if key != "record_sha256"
    }
    return {
        **value,
        "record_sha256": hashlib.sha256(_canonical(value)).hexdigest(),
    }


def _write_new_record(path: Path, record: dict[str, Any]) -> None:
    payload = _canonical(_record_with_hash(record))
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_record(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    payload = _canonical(_record_with_hash(record))
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    application_id = (
        str(record.get("application_id"))
        if isinstance(record.get("application_id"), str)
        else None
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if (
            _sha256_if_regular(temporary) != payload_sha256
            or not _path_entry_exists(path)
            or _sha256_if_regular(path) is None
        ):
            raise CurrentReportApplicationError(
                "application_record_write_failed",
                "the current-report application ledger is unsafe",
                application_id=application_id,
            )
        for attempt in range(
            len(_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS) + 1
        ):
            if _sha256_if_regular(temporary) != payload_sha256:
                raise CurrentReportApplicationError(
                    "application_record_write_failed",
                    (
                        "the current-report application ledger "
                        "temporary changed"
                    ),
                    application_id=application_id,
                )
            if (
                not _path_entry_exists(path)
                or _sha256_if_regular(path) is None
            ):
                raise CurrentReportApplicationError(
                    "application_record_write_failed",
                    (
                        "the current-report application ledger "
                        "destination changed"
                    ),
                    application_id=application_id,
                )
            try:
                os.replace(temporary, path)
                break
            except OSError as error:
                # Some Windows scanners/indexers briefly open record.json
                # without FILE_SHARE_DELETE. Retry only that transient class;
                # path, payload, and temporary identity remain revalidated.
                if (
                    _sha256_if_regular(path) == payload_sha256
                    and not _path_entry_exists(temporary)
                ):
                    break
                if (
                    attempt
                    >= len(_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS)
                    or not _retryable_windows_replace_error(error)
                ):
                    raise CurrentReportApplicationError(
                        "application_record_write_failed",
                        (
                            "the current-report application ledger "
                            "could not be atomically replaced"
                        ),
                        application_id=application_id,
                    ) from error
                time.sleep(
                    _WINDOWS_REPLACE_RETRY_DELAYS_SECONDS[attempt]
                )
        if _sha256_if_regular(path) != payload_sha256:
            raise CurrentReportApplicationError(
                "application_record_write_failed",
                (
                    "the current-report application ledger "
                    "failed read-after-write verification"
                ),
                application_id=application_id,
            )
    finally:
        try:
            if (
                _path_entry_exists(temporary)
                and _sha256_if_regular(temporary) == payload_sha256
            ):
                temporary.unlink()
        except OSError:
            pass


def _retryable_windows_replace_error(error: OSError) -> bool:
    return (
        getattr(error, "winerror", None) in {5, 32}
        or (
            os.name == "nt"
            and isinstance(error, PermissionError)
        )
    )


def _load_record(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise CurrentReportApplicationError(
            "application_record_corrupt",
            "the current-report application ledger is unavailable",
        ) from error
    if not isinstance(value, dict) or set(value) != _RECORD_KEYS:
        raise CurrentReportApplicationError(
            "application_record_corrupt",
            "the current-report application ledger is invalid",
        )
    supplied = value.pop("record_sha256")
    calculated = hashlib.sha256(_canonical(value)).hexdigest()
    value["record_sha256"] = supplied
    if (
        value.get("schema_version")
        != CURRENT_REPORT_APPLICATION_SCHEMA
        or not isinstance(supplied, str)
        or not hmac.compare_digest(supplied, calculated)
        or any(
            _SHA256.fullmatch(str(value.get(key))) is None
            for key in (
                "binding_manifest_sha256",
                "render_binding_sha256",
                "exact_plan_sha256",
                "document_edit_plan_sha256",
                "accept_reject_rollback_binding_sha256",
            )
        )
    ):
        raise CurrentReportApplicationError(
            "application_record_corrupt",
            "the current-report application ledger failed verification",
        )
    try:
        _validate_capacity_plan(value["capacity_plan"])
    except CurrentReportApplicationError as error:
        raise CurrentReportApplicationError(
            "application_record_corrupt",
            "the current-report capacity ledger failed verification",
        ) from error
    plan = value["capacity_plan"]
    try:
        expected_reservation_id = (
            "current-report:"
            + str(
                UUID(
                    str(value["application_id"]).removeprefix(
                        "current-report-application-"
                    )
                )
            )
        )
    except (TypeError, ValueError) as error:
        raise CurrentReportApplicationError(
            "application_record_corrupt",
            "the current-report application identity is invalid",
        ) from error
    if (
        plan.get("application_id") != value.get("application_id")
        or plan.get("project_id") != value.get("project_id")
        or plan.get("pipeline_job_id")
        != value.get("pipeline_job_id")
        or plan.get("analysis_job_id")
        != value.get("analysis_job_id")
        or plan.get("draft_id") != value.get("draft_id")
        or plan.get("draft_output_sha256")
        != value.get("preview_sha256")
        or plan.get("reservation_id")
        != expected_reservation_id
    ):
        raise CurrentReportApplicationError(
            "application_record_corrupt",
            "the current-report capacity scope changed",
        )
    reservation = value.get("capacity_reservation")
    if reservation is not None:
        expected_observation_keys = {
            "schema_version",
            "reservation_id",
            "reservation_scope_sha256",
            "reserved_bytes",
            "other_reserved_bytes",
            "available_free_bytes",
            "safety_reserve_bytes",
            "path_exposed",
        }
        if (
            not isinstance(reservation, dict)
            or set(reservation) != expected_observation_keys
            or reservation.get("schema_version")
            != "trafficvisson.capacity-reservation-observation.v1"
            or reservation.get("reservation_id")
            != plan["reservation_id"]
            or reservation.get("reserved_bytes")
            != plan["estimated_bytes"]
            or reservation.get("safety_reserve_bytes")
            != plan["safety_reserve_bytes"]
            or reservation.get("path_exposed") is not False
            or _SHA256.fullmatch(
                str(reservation.get("reservation_scope_sha256"))
            )
            is None
            or any(
                isinstance(reservation.get(key), bool)
                or not isinstance(reservation.get(key), int)
                or int(reservation[key]) < 0
                for key in (
                    "other_reserved_bytes",
                    "available_free_bytes",
                )
            )
        ):
            raise CurrentReportApplicationError(
                "application_record_corrupt",
                "the current-report reservation ledger is invalid",
            )
    if (
        value.get("artifact_category") != _ARTIFACT_CATEGORY
        or value.get("artifact_relative_root")
        != f"applications/{value.get('application_id')}"
        or _SHA256.fullmatch(
            str(value.get("category_root_identity_sha256"))
        )
        is None
        or value.get("output_path")
        != value.get("output_file_name")
        or value.get("pdf_path") != value.get("pdf_file_name")
        or value.get("output_file_name") != "current-report.hwpx"
        or value.get("pdf_file_name") != "current-report.pdf"
        or (
            value.get("render_directory") is not None
            and not isinstance(value["render_directory"], str)
        )
        or not isinstance(
            value.get("capacity_reservation_released"),
            bool,
        )
    ):
        raise CurrentReportApplicationError(
            "application_record_corrupt",
            "the current-report artifact locator ledger is invalid",
        )
    return value


def _real_directory(path: Path, *, create: bool) -> Path:
    if not path.is_absolute():
        raise ValueError("current-report application root must be absolute")
    absolute = Path(os.path.abspath(os.fspath(path)))
    if create:
        absolute.mkdir(parents=True, exist_ok=True)
    value = os.lstat(absolute)
    if (
        not stat.S_ISDIR(value.st_mode)
        or absolute.is_symlink()
        or _is_reparse(value)
    ):
        raise ValueError(
            "current-report application root must be a real directory"
        )
    return absolute


def _owned_path(path: Path, *, root: Path, suffix: str) -> Path:
    if not path.is_absolute() or path.suffix.casefold() != suffix:
        raise CurrentReportApplicationError(
            "application_path_invalid",
            "the current-report application path is invalid",
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        absolute.relative_to(root)
    except ValueError as error:
        raise CurrentReportApplicationError(
            "application_path_invalid",
            "the current-report application escaped its owned root",
        ) from error
    return absolute


def _validated_render_directory_address(
    value: Path | None,
    *,
    render_root: Path,
) -> Path | None:
    if value is None:
        return None
    directory = Path(value)
    if not directory.is_absolute():
        raise CurrentReportApplicationError(
            "render_validation_evidence_invalid",
            "the rendered-page directory is not owned",
        )
    absolute = Path(os.path.abspath(os.fspath(directory)))
    owned_root = Path(os.path.abspath(os.fspath(render_root)))
    if (
        absolute.parent != owned_root
        or _RENDER_DIRECTORY_NAME.fullmatch(absolute.name) is None
    ):
        raise CurrentReportApplicationError(
            "render_validation_evidence_invalid",
            "the rendered-page directory escaped its owned root",
        )
    try:
        value_stat = os.lstat(absolute)
    except OSError as error:
        raise CurrentReportApplicationError(
            "render_validation_evidence_invalid",
            "the rendered-page directory is unavailable",
        ) from error
    if (
        not stat.S_ISDIR(value_stat.st_mode)
        or absolute.is_symlink()
        or _is_reparse(value_stat)
    ):
        raise CurrentReportApplicationError(
            "render_validation_evidence_invalid",
            "the rendered-page directory is not a real directory",
        )
    return absolute


def _rendered_page_records(
    pages: tuple[RenderedPageEvidence, ...],
    *,
    render_directory: Path | None,
) -> list[dict[str, Any]]:
    if pages and render_directory is None:
        raise CurrentReportApplicationError(
            "render_validation_evidence_invalid",
            "rendered pages were returned without an owned directory",
        )
    records: list[dict[str, Any]] = []
    for expected_page_number, page in enumerate(pages, start=1):
        if not isinstance(page, RenderedPageEvidence):
            raise CurrentReportApplicationError(
                "render_validation_evidence_invalid",
                "the render validator returned an invalid page record",
            )
        expected_name = f"page-{expected_page_number:02d}.png"
        page_path = Path(
            os.path.abspath(os.fspath(page.png_path))
        )
        if (
            page.page_number != expected_page_number
            or page_path.name != expected_name
            or page_path.parent != render_directory
            or _SHA256.fullmatch(page.png_sha256) is None
            or not _positive_int(page.width_pixels)
            or not _positive_int(page.height_pixels)
            or not _positive_int(page.size_bytes)
            or not _bounded_nonnegative_int(
                page.significant_photo_count
            )
            or not _bounded_nonnegative_int(
                page.neutral_picture_count
            )
            or not _bounded_nonnegative_int(
                page.caption_proxy_count
            )
        ):
            raise CurrentReportApplicationError(
                "render_validation_evidence_invalid",
                "the render validator returned an invalid page ledger",
            )
        records.append(
            {
                "page_number": page.page_number,
                "file_name": expected_name,
                "png_sha256": page.png_sha256,
                "width_pixels": page.width_pixels,
                "height_pixels": page.height_pixels,
                "size_bytes": page.size_bytes,
                "significant_photo_count": (
                    page.significant_photo_count
                ),
                "neutral_picture_count": (
                    page.neutral_picture_count
                ),
                "caption_proxy_count": page.caption_proxy_count,
            }
        )
    return records


def _rendered_artifacts_from_record(
    records: Any,
    *,
    render_directory: Path | None,
    limits: RenderValidationLimits,
) -> tuple[RenderedPageEvidence, ...]:
    if not isinstance(records, list) or len(records) > EXPECTED_PAGE_COUNT:
        raise ValueError("rendered page ledger is invalid")
    if records and render_directory is None:
        raise ValueError("rendered page ledger has no owned directory")
    artifacts: list[RenderedPageEvidence] = []
    total_bytes = 0
    for expected_page_number, record in enumerate(records, start=1):
        expected_name = f"page-{expected_page_number:02d}.png"
        if (
            not isinstance(record, dict)
            or set(record) != _RENDERED_PAGE_RECORD_KEYS
            or record.get("page_number") != expected_page_number
            or record.get("file_name") != expected_name
            or not isinstance(record.get("png_sha256"), str)
            or _SHA256.fullmatch(record["png_sha256"]) is None
            or not _positive_int(record.get("width_pixels"))
            or not _positive_int(record.get("height_pixels"))
            or not _positive_int(record.get("size_bytes"))
            or not _bounded_nonnegative_int(
                record.get("significant_photo_count")
            )
            or not _bounded_nonnegative_int(
                record.get("neutral_picture_count")
            )
            or not _bounded_nonnegative_int(
                record.get("caption_proxy_count")
            )
            or (
                record["width_pixels"] * record["height_pixels"]
                > limits.max_page_pixels
            )
        ):
            raise ValueError("rendered page ledger is invalid")
        total_bytes += record["size_bytes"]
        if total_bytes > limits.max_rendered_total_bytes:
            raise ValueError("rendered page ledger exceeds its size limit")
        artifacts.append(
            RenderedPageEvidence(
                page_number=expected_page_number,
                png_path=render_directory / expected_name,
                png_sha256=record["png_sha256"],
                width_pixels=record["width_pixels"],
                height_pixels=record["height_pixels"],
                size_bytes=record["size_bytes"],
                nonwhite_ratio=0.0,
                edge_ink_ratio=0.0,
                text_character_count=0,
                significant_photo_count=record[
                    "significant_photo_count"
                ],
                neutral_picture_count=record[
                    "neutral_picture_count"
                ],
                caption_proxy_count=record["caption_proxy_count"],
                extracted_object_count=0,
            )
        )
    return tuple(artifacts)


def _prepare_owned_render_artifacts(
    *,
    application_root: Path,
    record: dict[str, Any],
    limits: RenderValidationLimits,
    require_complete: bool,
    verify_hashes: bool,
) -> _OwnedRenderArtifacts:
    root = Path(os.path.abspath(os.fspath(application_root)))
    render_root = root / "rendered-pages"
    try:
        root_stat = os.lstat(render_root)
    except FileNotFoundError:
        if record.get("render_directory") is not None or record.get(
            "rendered_pages"
        ):
            raise ValueError("owned render root is missing")
        if require_complete:
            raise ValueError("complete render evidence is missing")
        return _OwnedRenderArtifacts(render_root, None, ())
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or render_root.is_symlink()
        or _is_reparse(root_stat)
    ):
        raise ValueError("owned render root is invalid")

    root_entries = tuple(render_root.iterdir())
    stored_directory = record.get("render_directory")
    render_directory: Path | None = None
    if stored_directory is not None:
        if (
            not isinstance(stored_directory, str)
            or _RENDER_DIRECTORY_NAME.fullmatch(
                stored_directory
            )
            is None
        ):
            raise ValueError("render directory ledger is invalid")
        try:
            render_directory = _validated_render_directory_address(
                render_root / stored_directory,
                render_root=render_root,
            )
        except CurrentReportApplicationError as error:
            raise ValueError("render directory ledger is invalid") from error
        if root_entries != (render_directory,):
            raise ValueError("owned render root contains unexpected entries")
    else:
        if record.get("rendered_pages"):
            raise ValueError("page ledger is missing its directory")
        if not root_entries:
            if require_complete:
                raise ValueError("complete render evidence is missing")
            return _OwnedRenderArtifacts(render_root, None, ())
        if len(root_entries) != 1:
            raise ValueError("owned render root contains unexpected entries")
        candidate = root_entries[0]
        try:
            render_directory = _validated_render_directory_address(
                candidate,
                render_root=render_root,
            )
        except CurrentReportApplicationError as error:
            raise ValueError("owned render directory is invalid") from error

    artifacts = _rendered_artifacts_from_record(
        record.get("rendered_pages"),
        render_directory=render_directory,
        limits=limits,
    )
    if artifacts:
        manifest_sha256 = rendered_page_manifest_sha256(artifacts)
        if (
            not isinstance(
                record.get("raster_artifact_manifest_sha256"),
                str,
            )
            or not hmac.compare_digest(
                manifest_sha256 or "",
                record["raster_artifact_manifest_sha256"],
            )
        ):
            raise ValueError("rendered page manifest changed")
    elif record.get("raster_artifact_manifest_sha256") is not None:
        raise ValueError("empty page ledger has a raster manifest")

    expected_by_name = {
        artifact.png_path.name: artifact for artifact in artifacts
    }
    directory_entries = tuple(render_directory.iterdir())
    if artifacts:
        if {item.name for item in directory_entries} != set(
            expected_by_name
        ):
            raise ValueError(
                "owned render directory contains unexpected entries"
            )
    else:
        discovered: dict[int, Path] = {}
        for path in directory_entries:
            match = _RENDERED_PAGE_NAME.fullmatch(path.name)
            if match is None:
                raise ValueError(
                    "owned render directory contains unexpected entries"
                )
            page_number = int(match.group(1))
            if (
                page_number < 1
                or page_number > EXPECTED_PAGE_COUNT
                or page_number in discovered
            ):
                raise ValueError("owned render page name is invalid")
            discovered[page_number] = path
        directory_entries = tuple(
            discovered[number] for number in sorted(discovered)
        )

    page_files: list[tuple[Path, str | None]] = []
    for path in directory_entries:
        artifact = expected_by_name.get(path.name)
        expected_sha256 = (
            None if artifact is None else artifact.png_sha256
        )
        expected_size = None if artifact is None else artifact.size_bytes
        if not _regular_file_matches(
            path,
            expected_sha256=(
                expected_sha256 if verify_hashes else None
            ),
            expected_size=expected_size,
        ):
            raise ValueError("rendered page artifact changed")
        page_files.append((path, expected_sha256))

    if require_complete and (
        record.get("render_validation_status")
        != "validated_48_page_render"
        or record.get("render_validation_completed") is not True
        or record.get("rendered_page_count") != EXPECTED_PAGE_COUNT
        or record.get("render_failure_codes") != []
        or len(artifacts) != EXPECTED_PAGE_COUNT
        or len(page_files) != EXPECTED_PAGE_COUNT
    ):
        raise ValueError("complete render evidence is invalid")
    return _OwnedRenderArtifacts(
        render_root=render_root,
        render_directory=render_directory,
        page_files=tuple(page_files),
    )


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _bounded_nonnegative_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= 500_000
    )


def _regular_file_matches(
    path: Path,
    *,
    expected_sha256: str | None,
    expected_size: int | None,
) -> bool:
    fingerprint = _stable_regular_file_fingerprint(path)
    if fingerprint is None:
        return False
    digest, size = fingerprint
    return (
        (expected_size is None or size == expected_size)
        and (
            expected_sha256 is None
            or (
                _SHA256.fullmatch(expected_sha256) is not None
                and hmac.compare_digest(digest, expected_sha256)
            )
        )
    )


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


def _file_is_safe_to_remove(
    path: Path,
    expected_sha256: str | None,
) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return (
        isinstance(expected_sha256, str)
        and _SHA256.fullmatch(expected_sha256) is not None
        and _regular_file_matches(
            path,
            expected_sha256=expected_sha256,
            expected_size=None,
        )
    )


def _remove_prevalidated_file(
    path: Path,
    expected_sha256: str | None,
) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not _file_is_safe_to_remove(path, expected_sha256):
        return False
    try:
        path.unlink()
        return not path.exists()
    except OSError:
        return False


def _remove_prevalidated_render_artifacts(
    artifacts: _OwnedRenderArtifacts,
) -> bool:
    try:
        for path, expected_sha256 in artifacts.page_files:
            if expected_sha256 is None:
                if not _regular_file_matches(
                    path,
                    expected_sha256=None,
                    expected_size=None,
                ):
                    return False
            elif not _regular_file_matches(
                path,
                expected_sha256=expected_sha256,
                expected_size=None,
            ):
                return False
        for path, _expected_sha256 in artifacts.page_files:
            path.unlink()
        if artifacts.render_directory is not None:
            artifacts.render_directory.rmdir()
        try:
            artifacts.render_root.rmdir()
        except FileNotFoundError:
            pass
        return not artifacts.render_root.exists()
    except OSError:
        return False


def _sha256_if_regular(path: Path) -> str | None:
    try:
        value = os.lstat(path)
        if (
            not stat.S_ISREG(value.st_mode)
            or path.is_symlink()
            or _is_reparse(value)
        ):
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _path_entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _is_reparse(value: os.stat_result) -> bool:
    return bool(
        getattr(value, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


__all__ = [
    "AcceptedCurrentReportPublicationSource",
    "CURRENT_REPORT_APPLICATION_SCHEMA",
    "CURRENT_REPORT_CAPACITY_PLAN_SCHEMA",
    "CURRENT_REPORT_CAPACITY_SAFETY_RESERVE_BYTES",
    "CurrentReportApplicationError",
    "CurrentReportApplicationMetadata",
    "CurrentReportApplicationService",
    "ExactReportPublisher",
]
