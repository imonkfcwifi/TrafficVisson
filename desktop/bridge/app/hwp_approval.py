"""One-shot approval binding between HWP preview and deterministic COM apply.

Every apply attempt consumes its token before any document revalidation or COM
work begins.  A failed, cancelled, mismatched, expired, or successful attempt
therefore requires a fresh preview and cannot be replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Sequence, Tuple
from uuid import UUID

from .contracts import DocumentEditPlan
from .hwp_com import HwpDocumentFingerprint, fingerprint_document
from .hwpx_sample_layout import (
    SampleLayoutInspection,
    is_protected_sample_document_sha256,
)
from .hwpx_sample_plan_validator import (
    SampleSemanticPlanValidation,
    validate_sample_semantic_plan,
)


APPROVAL_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{64}$")
ROLLBACK_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{32}$")
DEFAULT_APPROVAL_TTL_SECONDS = 5 * 60
MAX_APPROVAL_RECORDS = 1_024


class HwpApprovalError(RuntimeError):
    """A caller-safe approval failure raised before COM is invoked."""


@dataclass(frozen=True)
class HwpTargetSummary:
    operation_count: int
    operation_types: Mapping[str, int]
    targets: Tuple[str, ...]


@dataclass(frozen=True)
class HwpApprovalGrant:
    token: str
    expires_at_unix: float
    document_sha256: str
    plan_sha256: str
    target_summary: HwpTargetSummary


@dataclass(frozen=True)
class HwpSampleApprovalGrant:
    """Hash-bound preview approval for an exact audited sample.

    This is deliberately not a COM apply grant.  The token proves which
    immutable sample, target plan, and project-owned approved-reference set
    the user previewed.  It can be consumed only once by the apply boundary,
    which currently returns an explicit unavailable result before COM.
    """

    token: str
    expires_at_unix: float
    document_sha256: str
    plan_sha256: str
    approved_references_sha256: str
    validation: SampleSemanticPlanValidation


@dataclass(frozen=True)
class _ApprovalRecord:
    project_id: UUID
    document: HwpDocumentFingerprint
    plan_sha256: str
    target_summary: HwpTargetSummary
    expires_at_unix: float


@dataclass(frozen=True)
class _SampleApprovalRecord:
    project_id: UUID
    document: HwpDocumentFingerprint
    plan_sha256: str
    approved_references_sha256: str
    validation_sha256: str
    expires_at_unix: float


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_plan_sha256(plan: DocumentEditPlan) -> str:
    """Hash the full validated DocumentEditPlan, not its reduced COM form."""

    return _canonical_json_sha256(plan.model_dump(mode="json"))


def canonical_approved_references_sha256(
    approved_references: Sequence[Mapping[str, object]],
) -> str:
    """Hash the complete project-owned approval records deterministically."""

    normalized = []
    for raw in approved_references:
        record = dict(raw)
        # This transport-only flag is not part of the durable approval record.
        record.pop("idempotent_replay", None)
        normalized.append(record)
    normalized.sort(
        key=lambda record: (
            str(record.get("approval_id", "")),
            str(record.get("record_sha256", "")),
        )
    )
    return _canonical_json_sha256(normalized)


def select_plan_approved_references(
    plan: DocumentEditPlan,
    inspection: SampleLayoutInspection,
    approved_references: Sequence[Mapping[str, object]],
) -> Tuple[Mapping[str, object], ...]:
    """Select only records referenced by this plan and exact sample snapshot."""

    source_refs = {operation.source_ref for operation in plan.operations}
    referenced = [
        record
        for record in approved_references
        if record.get("approval_id") in source_refs
    ]
    for record in referenced:
        if (
            record.get("document_sha256")
            != inspection.snapshot.document_sha256
            or record.get("structure_target_summary_sha256")
            != inspection.snapshot.target_summary_sha256
        ):
            raise HwpApprovalError(
                "편집 계획이 참조한 승인 사례가 선택한 표본 양식과 일치하지 않습니다."
            )
    selected = tuple(
        sorted(
            referenced,
            key=lambda record: (
                str(record.get("approval_id", "")),
                str(record.get("record_sha256", "")),
            ),
        )
    )
    approval_ids = [
        str(record.get("approval_id", "")) for record in selected
    ]
    if len(approval_ids) != len(set(approval_ids)):
        raise HwpApprovalError(
            "편집 계획이 참조한 승인 사례 식별자가 중복되었습니다."
        )
    return selected


def _validate_approved_reference_scope(
    project_id: UUID,
    approved_references: Sequence[Mapping[str, object]],
) -> None:
    project_text = str(project_id)
    if any(
        str(record.get("project_id", "")) != project_text
        for record in approved_references
    ):
        raise HwpApprovalError(
            "승인 표본 사례와 현재 프로젝트가 일치하지 않습니다."
        )


def summarize_plan_targets(plan: DocumentEditPlan) -> HwpTargetSummary:
    operation_types = dict(
        sorted(Counter(operation.type for operation in plan.operations).items())
    )
    targets = tuple(sorted({operation.target for operation in plan.operations}))
    return HwpTargetSummary(
        operation_count=len(plan.operations),
        operation_types=operation_types,
        targets=targets,
    )


def _target_summaries_match(
    actual: HwpTargetSummary, expected: HwpTargetSummary
) -> bool:
    return (
        actual.operation_count == expected.operation_count
        and dict(actual.operation_types) == dict(expected.operation_types)
        and actual.targets == expected.targets
    )


def _document_fingerprints_match(
    actual: HwpDocumentFingerprint, expected: HwpDocumentFingerprint
) -> bool:
    return (
        actual.path == expected.path
        and actual.device == expected.device
        and actual.inode == expected.inode
        and actual.size == expected.size
        and actual.modified_ns == expected.modified_ns
        and hmac.compare_digest(actual.sha256, expected.sha256)
    )


class HwpApprovalRegistry:
    """In-memory, restart-invalidated, one-shot approval token registry."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(32),
        max_records: int = MAX_APPROVAL_RECORDS,
    ) -> None:
        if ttl_seconds <= 0 or ttl_seconds > 3_600:
            raise ValueError("approval ttl must be between zero and one hour")
        if max_records < 1 or max_records > MAX_APPROVAL_RECORDS:
            raise ValueError("approval record limit is outside the safe range")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory
        self._max_records = max_records
        self._records: Dict[str, _ApprovalRecord] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        project_id: UUID,
        document: HwpDocumentFingerprint,
        plan: DocumentEditPlan,
    ) -> HwpApprovalGrant:
        # Close the gap between the COM preview readback and token issuance.
        current_document = fingerprint_document(document.path)
        if not _document_fingerprints_match(current_document, document):
            raise HwpApprovalError(
                "미리보기 직후 문서 양식이 변경되어 승인을 발급하지 않았습니다."
            )
        if is_protected_sample_document_sha256(
            current_document.sha256
        ):
            raise HwpApprovalError(
                "3·4분기 표본 양식은 정확한 HWPML 대상 편집기와 "
                "저장 후 48쪽 전 페이지 렌더 검증이 연결되기 전에는 "
                "문서 적용 승인을 발급하지 않습니다."
            )
        plan_sha256 = canonical_plan_sha256(plan)
        target_summary = summarize_plan_targets(plan)
        now = self._clock()
        expires_at = now + self._ttl_seconds
        token = self._token_factory()
        if not isinstance(token, str) or not APPROVAL_TOKEN_PATTERN.fullmatch(token):
            raise RuntimeError("approval token factory returned an invalid token")
        record = _ApprovalRecord(
            project_id=project_id,
            document=current_document,
            plan_sha256=plan_sha256,
            target_summary=target_summary,
            expires_at_unix=expires_at,
        )
        with self._lock:
            self._discard_expired_locked(now)
            if token in self._records:
                raise RuntimeError("approval token factory returned a duplicate token")
            while len(self._records) >= self._max_records:
                oldest_token = next(iter(self._records))
                self._records.pop(oldest_token, None)
            self._records[token] = record
        return HwpApprovalGrant(
            token=token,
            expires_at_unix=expires_at,
            document_sha256=current_document.sha256,
            plan_sha256=plan_sha256,
            target_summary=target_summary,
        )

    def consume(
        self,
        token: str,
        project_id: UUID,
        document_path: Path,
        plan: DocumentEditPlan,
    ) -> HwpDocumentFingerprint:
        if not isinstance(token, str) or not APPROVAL_TOKEN_PATTERN.fullmatch(token):
            raise HwpApprovalError("유효한 문서 적용 승인이 아닙니다.")
        now = self._clock()
        # Pop first: every apply attempt is one-shot, including failures and
        # client cancellation after the server accepted the request.
        with self._lock:
            record = self._records.pop(token, None)
        if record is None:
            raise HwpApprovalError(
                "문서 적용 승인이 없거나 이미 사용되었습니다. 다시 미리보세요."
            )
        if now >= record.expires_at_unix:
            raise HwpApprovalError(
                "문서 적용 승인이 만료되었습니다. 다시 미리보세요."
            )
        if record.project_id != project_id:
            raise HwpApprovalError(
                "문서 적용 승인과 현재 프로젝트가 일치하지 않습니다."
            )
        actual_plan_sha256 = canonical_plan_sha256(plan)
        actual_target_summary = summarize_plan_targets(plan)
        if not hmac.compare_digest(actual_plan_sha256, record.plan_sha256):
            raise HwpApprovalError(
                "미리보기 이후 편집 계획이 변경되었습니다. 다시 미리보세요."
            )
        if not _target_summaries_match(
            actual_target_summary, record.target_summary
        ):
            raise HwpApprovalError(
                "미리보기 이후 편집 대상이 변경되었습니다. 다시 미리보세요."
            )
        current_document = fingerprint_document(document_path)
        if not _document_fingerprints_match(current_document, record.document):
            raise HwpApprovalError(
                "미리보기 이후 문서 양식이 변경되었습니다. 다시 미리보세요."
            )
        return record.document

    def _discard_expired_locked(self, now: float) -> None:
        for token, record in tuple(self._records.items()):
            if now >= record.expires_at_unix:
                self._records.pop(token, None)


class HwpSampleApprovalRegistry:
    """One-shot preview/apply binding for exact audited sample HWPX files.

    Both issuance and consumption execute target/structure validation against
    server-owned approval records. No method in this registry calls COM,
    establishes content truth, or writes a document.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = lambda: secrets.token_hex(32),
        max_records: int = MAX_APPROVAL_RECORDS,
    ) -> None:
        if ttl_seconds <= 0 or ttl_seconds > 3_600:
            raise ValueError("approval ttl must be between zero and one hour")
        if max_records < 1 or max_records > MAX_APPROVAL_RECORDS:
            raise ValueError("approval record limit is outside the safe range")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory
        self._max_records = max_records
        self._records: Dict[str, _SampleApprovalRecord] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        project_id: UUID,
        document_path: Path,
        plan: DocumentEditPlan,
        approved_references: Sequence[Mapping[str, object]],
    ) -> HwpSampleApprovalGrant:
        _validate_approved_reference_scope(
            project_id,
            approved_references,
        )
        validation = validate_sample_semantic_plan(
            document_path,
            plan,
            approved_references=approved_references,
        )
        current_document = fingerprint_document(document_path)
        if not hmac.compare_digest(
            current_document.sha256,
            validation.document_sha256,
        ):
            raise HwpApprovalError(
                "의미 미리보기 직후 표본 양식이 변경되어 승인을 발급하지 않았습니다."
            )
        plan_sha256 = canonical_plan_sha256(plan)
        if not hmac.compare_digest(plan_sha256, validation.plan_sha256):
            raise HwpApprovalError(
                "의미 미리보기 편집 계획 해시가 일치하지 않습니다."
            )
        approved_references_sha256 = canonical_approved_references_sha256(
            approved_references
        )
        now = self._clock()
        expires_at = now + self._ttl_seconds
        token = self._token_factory()
        if not isinstance(token, str) or not APPROVAL_TOKEN_PATTERN.fullmatch(
            token
        ):
            raise RuntimeError("approval token factory returned an invalid token")
        record = _SampleApprovalRecord(
            project_id=project_id,
            document=current_document,
            plan_sha256=plan_sha256,
            approved_references_sha256=approved_references_sha256,
            validation_sha256=validation.validation_sha256,
            expires_at_unix=expires_at,
        )
        with self._lock:
            self._discard_expired_locked(now)
            if token in self._records:
                raise RuntimeError("approval token factory returned a duplicate token")
            while len(self._records) >= self._max_records:
                oldest_token = next(iter(self._records))
                self._records.pop(oldest_token, None)
            self._records[token] = record
        return HwpSampleApprovalGrant(
            token=token,
            expires_at_unix=expires_at,
            document_sha256=current_document.sha256,
            plan_sha256=plan_sha256,
            approved_references_sha256=approved_references_sha256,
            validation=validation,
        )

    def owns(self, token: str) -> bool:
        if not isinstance(token, str) or not APPROVAL_TOKEN_PATTERN.fullmatch(
            token
        ):
            return False
        with self._lock:
            return token in self._records

    def consume(
        self,
        token: str,
        project_id: UUID,
        document_path: Path,
        plan: DocumentEditPlan,
        approved_references_loader: Callable[
            [], Sequence[Mapping[str, object]]
        ],
    ) -> SampleSemanticPlanValidation:
        if not isinstance(token, str) or not APPROVAL_TOKEN_PATTERN.fullmatch(
            token
        ):
            raise HwpApprovalError("유효한 표본 문서 미리보기 승인이 아닙니다.")
        now = self._clock()
        # Burn first. Store read failures, stale approvals, invalid plans,
        # cancellation, and the expected writer-unavailable result cannot be
        # replayed with the same user approval.
        with self._lock:
            record = self._records.pop(token, None)
        if record is None:
            raise HwpApprovalError(
                "표본 문서 미리보기 승인이 없거나 이미 사용되었습니다. 다시 미리보세요."
            )
        if now >= record.expires_at_unix:
            raise HwpApprovalError(
                "표본 문서 미리보기 승인이 만료되었습니다. 다시 미리보세요."
            )
        if record.project_id != project_id:
            raise HwpApprovalError(
                "표본 문서 미리보기 승인과 현재 프로젝트가 일치하지 않습니다."
            )
        actual_plan_sha256 = canonical_plan_sha256(plan)
        if not hmac.compare_digest(actual_plan_sha256, record.plan_sha256):
            raise HwpApprovalError(
                "미리보기 이후 표본 편집 계획이 변경되었습니다. 다시 미리보세요."
            )
        current_document = fingerprint_document(document_path)
        if not _document_fingerprints_match(current_document, record.document):
            raise HwpApprovalError(
                "미리보기 이후 표본 양식이 변경되었습니다. 다시 미리보세요."
            )
        approved_references = tuple(approved_references_loader())
        _validate_approved_reference_scope(
            project_id,
            approved_references,
        )
        actual_references_sha256 = canonical_approved_references_sha256(
            approved_references
        )
        if not hmac.compare_digest(
            actual_references_sha256,
            record.approved_references_sha256,
        ):
            raise HwpApprovalError(
                "미리보기 이후 승인 표본 사례가 변경되었습니다. 다시 미리보세요."
            )
        validation = validate_sample_semantic_plan(
            document_path,
            plan,
            approved_references=approved_references,
        )
        if not hmac.compare_digest(
            validation.validation_sha256,
            record.validation_sha256,
        ):
            raise HwpApprovalError(
                "미리보기 이후 표본 대상·구조 검증 결과가 변경되었습니다. 다시 미리보세요."
            )
        return validation

    def _discard_expired_locked(self, now: float) -> None:
        for token, record in tuple(self._records.items()):
            if now >= record.expires_at_unix:
                self._records.pop(token, None)


class HwpRollbackScopeRegistry:
    """Bind service-owned rollback handles to their creating project."""

    def __init__(self) -> None:
        self._records: Dict[str, UUID] = {}
        self._lock = threading.Lock()

    def bind(self, project_id: UUID, rollback_token: str) -> None:
        if (
            not isinstance(rollback_token, str)
            or ROLLBACK_TOKEN_PATTERN.fullmatch(rollback_token) is None
        ):
            raise HwpApprovalError("유효한 롤백 식별자가 아닙니다.")
        with self._lock:
            existing = self._records.get(rollback_token)
            if existing is not None and existing != project_id:
                raise HwpApprovalError(
                    "롤백 식별자가 다른 프로젝트에 이미 결속되어 있습니다."
                )
            self._records[rollback_token] = project_id

    def authorize(self, project_id: UUID, rollback_token: str) -> None:
        if (
            not isinstance(rollback_token, str)
            or ROLLBACK_TOKEN_PATTERN.fullmatch(rollback_token) is None
        ):
            raise HwpApprovalError("유효한 롤백 식별자가 아닙니다.")
        with self._lock:
            owner = self._records.get(rollback_token)
        if owner is None:
            raise HwpApprovalError(
                "현재 세션에서 사용할 수 있는 롤백 승인이 없습니다."
            )
        if owner != project_id:
            raise HwpApprovalError(
                "롤백 승인과 현재 프로젝트가 일치하지 않습니다."
            )


__all__ = [
    "APPROVAL_TOKEN_PATTERN",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "HwpApprovalError",
    "HwpApprovalGrant",
    "HwpApprovalRegistry",
    "HwpRollbackScopeRegistry",
    "HwpSampleApprovalGrant",
    "HwpSampleApprovalRegistry",
    "HwpTargetSummary",
    "ROLLBACK_TOKEN_PATTERN",
    "canonical_approved_references_sha256",
    "canonical_plan_sha256",
    "select_plan_approved_references",
    "summarize_plan_targets",
]
