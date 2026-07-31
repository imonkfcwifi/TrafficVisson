"""One-shot approval for publishing an exact current-report HWPX draft.

The deterministic XML writer is allowed to create a server-owned preview
before user approval.  Hangul COM and publication are not.  This registry
binds the user's later approval to the exact project/job/Incident manifest,
exact-writer plan, path-free ``DocumentEditPlan`` decision binding, and
immutable preview file that were shown.

Records are intentionally in-memory and restart-invalidated.  Every consume
attempt burns the token before revalidation so an expired, changed, failed, or
cancelled apply cannot be replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat as stat_module
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, Optional
from uuid import UUID

from .current_incident_report_binder import (
    CurrentIncidentReportBindingManifest,
)
from .current_report_document_edit_plan import (
    CurrentReportDocumentEditPlan,
    CurrentReportDocumentEditPlanError,
    validate_current_report_document_edit_plan,
)
from .hwp_com import HwpDocumentFingerprint, fingerprint_document
from .hwp_render_validation import ExplicitApplyApproval
from .hwpx_exact_writer import (
    ExactHwpxWritePlan,
    ExactHwpxWriteResult,
    HWPX_EXACT_WRITER_SCHEMA,
)


CURRENT_REPORT_APPROVAL_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_DEFAULT_TTL_SECONDS = 5 * 60
_MAX_RECORDS = 1_024


class CurrentReportApprovalError(RuntimeError):
    """A caller-safe failure raised before COM or publication."""

    com_invoked = False
    document_published = False
    apply_blocked = True


@dataclass(frozen=True)
class CurrentReportApprovalGrant:
    token: str
    expires_at_unix: float
    project_id: str
    pipeline_job_id: str
    binding_manifest_sha256: str
    exact_plan_sha256: str
    document_edit_plan_sha256: str
    accept_reject_rollback_binding_sha256: str
    exact_preview_sha256: str
    operation_count: int


@dataclass(frozen=True)
class CurrentReportApplyAuthorization:
    approval: ExplicitApplyApproval
    project_id: str
    pipeline_job_id: str
    analysis_job_id: str
    binding_manifest_sha256: str
    exact_plan_sha256: str
    document_edit_plan_sha256: str
    accept_reject_rollback_binding_sha256: str
    exact_preview_fingerprint: HwpDocumentFingerprint


@dataclass(frozen=True)
class _ApprovalRecord:
    project_id: UUID
    pipeline_job_id: UUID
    analysis_job_id: UUID
    binding_manifest_sha256: str
    exact_plan_sha256: str
    exact_plan_id: str
    document_edit_plan_sha256: str
    document_edit_plan_id: str
    accept_reject_rollback_binding_sha256: str
    exact_preview: HwpDocumentFingerprint
    structure_projection_sha256: str
    operation_count: int
    expires_at_unix: float


class CurrentReportApprovalRegistry:
    """Bind one explicit user approval to one exact writer preview."""

    def __init__(
        self,
        *,
        owned_preview_root: Path,
        preview_root_resolver: Optional[
            Callable[[UUID, UUID, UUID], Path]
        ] = None,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(32),
        max_records: int = _MAX_RECORDS,
    ) -> None:
        if ttl_seconds <= 0 or ttl_seconds > 3_600:
            raise ValueError("approval ttl must be between zero and one hour")
        if max_records < 1 or max_records > _MAX_RECORDS:
            raise ValueError("approval record limit is outside the safe range")
        self._owned_preview_root = _real_directory(
            Path(owned_preview_root),
            "current-report preview root",
        )
        if (
            preview_root_resolver is not None
            and not callable(preview_root_resolver)
        ):
            raise TypeError("preview_root_resolver must be callable")
        self._preview_root_resolver = preview_root_resolver
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory
        self._max_records = max_records
        self._records: Dict[str, _ApprovalRecord] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        binding: CurrentIncidentReportBindingManifest,
        plan: ExactHwpxWritePlan,
        document_edit_plan: CurrentReportDocumentEditPlan,
        writer_result: ExactHwpxWriteResult,
    ) -> CurrentReportApprovalGrant:
        preview = self._revalidate(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            binding=binding,
            plan=plan,
            document_edit_plan=document_edit_plan,
            writer_result=writer_result,
        )
        plan_sha256 = canonical_exact_hwpx_plan_sha256(plan)
        now = self._clock()
        expires_at = now + self._ttl_seconds
        token = self._token_factory()
        if (
            not isinstance(token, str)
            or CURRENT_REPORT_APPROVAL_TOKEN_PATTERN.fullmatch(token) is None
        ):
            raise RuntimeError(
                "current-report approval token factory returned an invalid token"
            )
        record = _ApprovalRecord(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            binding_manifest_sha256=binding.binding_manifest_sha256,
            exact_plan_sha256=plan_sha256,
            exact_plan_id=plan.plan_id,
            document_edit_plan_sha256=(
                document_edit_plan.document_edit_plan_sha256
            ),
            document_edit_plan_id=document_edit_plan.plan_id,
            accept_reject_rollback_binding_sha256=(
                document_edit_plan
                .accept_reject_rollback_binding_sha256
            ),
            exact_preview=preview,
            structure_projection_sha256=(
                writer_result.structure_projection_sha256
            ),
            operation_count=len(plan.operations),
            expires_at_unix=expires_at,
        )
        with self._lock:
            self._discard_expired_locked(now)
            if token in self._records:
                raise RuntimeError(
                    "current-report approval token factory returned a duplicate token"
                )
            while len(self._records) >= self._max_records:
                oldest = next(iter(self._records))
                self._records.pop(oldest, None)
            self._records[token] = record
        return CurrentReportApprovalGrant(
            token=token,
            expires_at_unix=expires_at,
            project_id=str(project_id),
            pipeline_job_id=str(pipeline_job_id),
            binding_manifest_sha256=binding.binding_manifest_sha256,
            exact_plan_sha256=plan_sha256,
            document_edit_plan_sha256=(
                document_edit_plan.document_edit_plan_sha256
            ),
            accept_reject_rollback_binding_sha256=(
                document_edit_plan
                .accept_reject_rollback_binding_sha256
            ),
            exact_preview_sha256=preview.sha256,
            operation_count=len(plan.operations),
        )

    def consume(
        self,
        token: str,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        binding: CurrentIncidentReportBindingManifest,
        plan: ExactHwpxWritePlan,
        document_edit_plan: CurrentReportDocumentEditPlan,
        expected_document_edit_plan_sha256: str,
        expected_accept_reject_rollback_binding_sha256: str,
        writer_result: ExactHwpxWriteResult,
    ) -> CurrentReportApplyAuthorization:
        if (
            not isinstance(token, str)
            or CURRENT_REPORT_APPROVAL_TOKEN_PATTERN.fullmatch(token) is None
        ):
            raise CurrentReportApprovalError(
                "유효한 현재 보고서 적용 승인이 아닙니다."
            )
        now = self._clock()
        # Burn first.  Every accepted request is one-shot even if a later
        # fingerprint, COM, render, publication, or cancellation step fails.
        with self._lock:
            record = self._records.pop(token, None)
        if record is None:
            raise CurrentReportApprovalError(
                "현재 보고서 적용 승인이 없거나 이미 사용되었습니다. 다시 미리보세요."
            )
        if now >= record.expires_at_unix:
            raise CurrentReportApprovalError(
                "현재 보고서 적용 승인이 만료되었습니다. 다시 미리보세요."
            )
        if (
            not isinstance(
                expected_document_edit_plan_sha256, str
            )
            or _SHA256.fullmatch(
                expected_document_edit_plan_sha256
            )
            is None
            or not isinstance(
                expected_accept_reject_rollback_binding_sha256,
                str,
            )
            or _SHA256.fullmatch(
                expected_accept_reject_rollback_binding_sha256
            )
            is None
        ):
            raise CurrentReportApprovalError(
                "사용자가 확인한 현재 보고서 편집 계획 해시가 유효하지 않습니다."
            )
        if (
            record.project_id != project_id
            or record.pipeline_job_id != pipeline_job_id
            or record.analysis_job_id != analysis_job_id
        ):
            raise CurrentReportApprovalError(
                "현재 보고서 적용 승인과 프로젝트 분석 작업이 일치하지 않습니다."
            )
        preview = self._revalidate(
            project_id=project_id,
            pipeline_job_id=pipeline_job_id,
            analysis_job_id=analysis_job_id,
            binding=binding,
            plan=plan,
            document_edit_plan=document_edit_plan,
            writer_result=writer_result,
        )
        plan_sha256 = canonical_exact_hwpx_plan_sha256(plan)
        if (
            not hmac.compare_digest(
                binding.binding_manifest_sha256,
                record.binding_manifest_sha256,
            )
            or not hmac.compare_digest(
                plan_sha256,
                record.exact_plan_sha256,
            )
            or plan.plan_id != record.exact_plan_id
            or not hmac.compare_digest(
                document_edit_plan.document_edit_plan_sha256,
                record.document_edit_plan_sha256,
            )
            or not hmac.compare_digest(
                expected_document_edit_plan_sha256,
                document_edit_plan.document_edit_plan_sha256,
            )
            or document_edit_plan.plan_id
            != record.document_edit_plan_id
            or not hmac.compare_digest(
                document_edit_plan
                .accept_reject_rollback_binding_sha256,
                record.accept_reject_rollback_binding_sha256,
            )
            or not hmac.compare_digest(
                expected_accept_reject_rollback_binding_sha256,
                document_edit_plan
                .accept_reject_rollback_binding_sha256,
            )
            or writer_result.structure_projection_sha256
            != record.structure_projection_sha256
            or len(plan.operations) != record.operation_count
            or not _fingerprints_match(preview, record.exact_preview)
        ):
            raise CurrentReportApprovalError(
                "미리보기 이후 현재 보고서 내용이나 파일이 변경되었습니다. 다시 미리보세요."
            )
        return CurrentReportApplyAuthorization(
            approval=ExplicitApplyApproval(
                approval_id=token,
                output_hwpx_sha256=preview.sha256,
                granted=True,
            ),
            project_id=str(project_id),
            pipeline_job_id=str(pipeline_job_id),
            analysis_job_id=str(analysis_job_id),
            binding_manifest_sha256=(
                binding.binding_manifest_sha256
            ),
            exact_plan_sha256=plan_sha256,
            document_edit_plan_sha256=(
                document_edit_plan.document_edit_plan_sha256
            ),
            accept_reject_rollback_binding_sha256=(
                document_edit_plan
                .accept_reject_rollback_binding_sha256
            ),
            exact_preview_fingerprint=preview,
        )

    def _revalidate(
        self,
        *,
        project_id: UUID,
        pipeline_job_id: UUID,
        analysis_job_id: UUID,
        binding: CurrentIncidentReportBindingManifest,
        plan: ExactHwpxWritePlan,
        document_edit_plan: CurrentReportDocumentEditPlan,
        writer_result: ExactHwpxWriteResult,
    ) -> HwpDocumentFingerprint:
        if (
            not isinstance(binding, CurrentIncidentReportBindingManifest)
            or binding.project_id != str(project_id)
            or binding.pipeline_job_id != str(pipeline_job_id)
            or binding.analysis_job_id != str(analysis_job_id)
            or binding.approved_incident_bound is not True
            or binding.content_provenance_bound is not True
            or binding.writer_required is not True
            or binding.apply_eligible is not False
            or binding.document_written is not False
            or binding.writer_batching_required is not False
            or _SHA256.fullmatch(
                binding.binding_manifest_sha256
            )
            is None
        ):
            raise CurrentReportApprovalError(
                "현재 보고서 승인에 필요한 사건 결속이 완전하지 않습니다."
            )
        incident_refs = tuple(
            f"incident:{incident_id}"
            for incident_id in binding.approved_incident_ids
        )
        if (
            not isinstance(plan, ExactHwpxWritePlan)
            or plan.source_document_sha256 != binding.template_sha256
            or plan.source_target_summary_sha256
            != binding.structure_target_summary_sha256
            or plan.incident_source_refs != incident_refs
            or len(plan.operations)
            != binding.required_writer_operation_count
            or not isinstance(writer_result, ExactHwpxWriteResult)
            or writer_result.schema_version
            != HWPX_EXACT_WRITER_SCHEMA
            or writer_result.state
            != "writer_completed_com_validation_required"
            or writer_result.plan_id != plan.plan_id
            or writer_result.source_document_sha256
            != plan.source_document_sha256
            or writer_result.operation_count != len(plan.operations)
            or len(writer_result.target_verifications)
            != len(plan.operations)
            or writer_result.expected_rendered_page_count != 48
            or writer_result.output_rendered_page_count is not None
            or writer_result.page_contract_proxy_verified is not True
            or writer_result.output_render_validation_status
            != "not_run_com_validation_required"
            or writer_result.source_unchanged is not True
            or writer_result.mimetype_first_and_stored is not True
            or writer_result.com_invocation_count != 0
            or _SHA256.fullmatch(
                writer_result.output_document_sha256
            )
            is None
            or _SHA256.fullmatch(
                writer_result.structure_projection_sha256
            )
            is None
        ):
            raise CurrentReportApprovalError(
                "정확 HWPX 미리보기 검증이 완료되지 않았습니다."
            )
        try:
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
            raise CurrentReportApprovalError(
                "현재 보고서 편집 계획의 결속 해시가 유효하지 않습니다."
            ) from error
        preview_root = self._owned_preview_root
        if self._preview_root_resolver is not None:
            try:
                preview_root = _real_directory(
                    Path(
                        self._preview_root_resolver(
                            project_id,
                            pipeline_job_id,
                            analysis_job_id,
                        )
                    ),
                    "current-report PipelineJob preview root",
                )
            except CurrentReportApprovalError:
                raise
            except Exception as error:
                raise CurrentReportApprovalError(
                    "현재 보고서 미리보기 저장소 결속을 확인할 수 없습니다."
                ) from error
        output = _owned_regular_file(
            Path(writer_result.output_path),
            root=preview_root,
        )
        preview = fingerprint_document(output)
        if not hmac.compare_digest(
            preview.sha256,
            writer_result.output_document_sha256,
        ):
            raise CurrentReportApprovalError(
                "정확 HWPX 미리보기 파일이 변경되었습니다."
            )
        return preview

    def _discard_expired_locked(self, now: float) -> None:
        for token, record in tuple(self._records.items()):
            if now >= record.expires_at_unix:
                self._records.pop(token, None)


def canonical_exact_hwpx_plan_sha256(
    plan: ExactHwpxWritePlan,
) -> str:
    if not isinstance(plan, ExactHwpxWritePlan):
        raise CurrentReportApprovalError(
            "정확 HWPX 편집 계획이 필요합니다."
        )
    return hashlib.sha256(
        json.dumps(
            asdict(plan),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _real_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        stat_result = os.stat(absolute, follow_symlinks=False)
    except OSError as error:
        raise ValueError(f"{label} is unavailable") from error
    if (
        not stat_module.S_ISDIR(stat_result.st_mode)
        or absolute.is_symlink()
        or _is_reparse(stat_result)
    ):
        raise ValueError(f"{label} must be a real directory")
    return absolute


def _owned_regular_file(path: Path, *, root: Path) -> Path:
    if (
        not path.is_absolute()
        or path.suffix.casefold() != ".hwpx"
        or "\x00" in os.fspath(path)
    ):
        raise CurrentReportApprovalError(
            "현재 보고서 미리보기 경로가 올바르지 않습니다."
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        absolute.relative_to(root)
    except ValueError as error:
        raise CurrentReportApprovalError(
            "현재 보고서 미리보기가 서버 소유 경로 밖에 있습니다."
        ) from error
    current = absolute
    while True:
        try:
            stat_result = os.stat(current, follow_symlinks=False)
        except OSError as error:
            raise CurrentReportApprovalError(
                "현재 보고서 미리보기 파일을 확인하지 못했습니다."
            ) from error
        if current.is_symlink() or _is_reparse(stat_result):
            raise CurrentReportApprovalError(
                "현재 보고서 미리보기 경로에 링크를 사용할 수 없습니다."
            )
        if current == root:
            break
        current = current.parent
    final_stat = os.stat(absolute, follow_symlinks=False)
    if not stat_module.S_ISREG(final_stat.st_mode):
        raise CurrentReportApprovalError(
            "현재 보고서 미리보기가 일반 파일이 아닙니다."
        )
    return absolute


def _is_reparse(stat_result: os.stat_result) -> bool:
    return bool(
        getattr(stat_result, "st_file_attributes", 0)
        & getattr(
            stat_module,
            "FILE_ATTRIBUTE_REPARSE_POINT",
            0x400,
        )
    )


def _fingerprints_match(
    actual: HwpDocumentFingerprint,
    expected: HwpDocumentFingerprint,
) -> bool:
    return (
        actual.path == expected.path
        and actual.device == expected.device
        and actual.inode == expected.inode
        and actual.size == expected.size
        and actual.modified_ns == expected.modified_ns
        and hmac.compare_digest(actual.sha256, expected.sha256)
    )


__all__ = [
    "CURRENT_REPORT_APPROVAL_TOKEN_PATTERN",
    "CurrentReportApplyAuthorization",
    "CurrentReportApprovalError",
    "CurrentReportApprovalGrant",
    "CurrentReportApprovalRegistry",
    "canonical_exact_hwpx_plan_sha256",
]
