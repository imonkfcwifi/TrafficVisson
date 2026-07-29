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
from typing import Callable, Dict, Mapping, Tuple
from uuid import UUID

from .contracts import DocumentEditPlan
from .hwp_com import HwpDocumentFingerprint, fingerprint_document


APPROVAL_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{64}$")
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
class _ApprovalRecord:
    project_id: UUID
    document: HwpDocumentFingerprint
    plan_sha256: str
    target_summary: HwpTargetSummary
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
        document_path,
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


__all__ = [
    "APPROVAL_TOKEN_PATTERN",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "HwpApprovalError",
    "HwpApprovalGrant",
    "HwpApprovalRegistry",
    "HwpTargetSummary",
    "canonical_plan_sha256",
    "summarize_plan_targets",
]
