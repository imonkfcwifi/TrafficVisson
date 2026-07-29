from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from app.contracts import DocumentEditPlan
from app.hwp_approval import (
    HwpApprovalError,
    HwpApprovalRegistry,
    canonical_plan_sha256,
    summarize_plan_targets,
)
from app.hwp_com import fingerprint_document


PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
TOKEN = "b" * 64
WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q = (
    WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"
)


def plan(value: str = "점검 결과") -> DocumentEditPlan:
    return DocumentEditPlan.model_validate(
        {
            "schema_version": "1.0",
            "plan_id": "22222222-2222-4222-8222-222222222222",
            "document_snapshot_id": "snapshot:approval-test",
            "operations": [
                {
                    "operation_id": "33333333-3333-4333-8333-333333333333",
                    "type": "fill_text",
                    "target": "bookmark:점검_요약",
                    "expected_value": None,
                    "value": value,
                    "source_ref": "report:draft:summary",
                    "style_policy": "preserve",
                    "preview": "점검 결과 문장 삽입",
                }
            ],
        }
    )


def test_plan_hash_and_target_summary_cover_full_canonical_plan():
    original = plan()
    changed = plan("다른 점검 결과")

    assert canonical_plan_sha256(original) != canonical_plan_sha256(changed)
    assert summarize_plan_targets(original).operation_types == {"fill_text": 1}
    assert summarize_plan_targets(original).targets == ("bookmark:점검_요약",)


def test_consumed_token_stays_burned_when_caller_cancels_before_com(
    tmp_path: Path,
):
    document = tmp_path / "cancelled-apply.hwpx"
    document.write_bytes(b"previewed-document")
    registry = HwpApprovalRegistry(
        ttl_seconds=60,
        clock=lambda: 1_000.0,
        token_factory=lambda: TOKEN,
    )
    approved_plan = plan()
    grant = registry.issue(
        PROJECT_ID,
        fingerprint_document(document),
        approved_plan,
    )

    # The bridge consumes first.  Simulate cancellation by intentionally doing
    # no COM work after the verified fingerprint is returned.
    registry.consume(grant.token, PROJECT_ID, document, approved_plan)

    with pytest.raises(HwpApprovalError, match="이미 사용"):
        registry.consume(grant.token, PROJECT_ID, document, approved_plan)


def test_document_hash_detects_same_size_same_mtime_tampering(tmp_path: Path):
    document = tmp_path / "same-metadata-tamper.hwpx"
    document.write_bytes(b"AAAA")
    original_stat = document.stat()
    registry = HwpApprovalRegistry(
        ttl_seconds=60,
        clock=lambda: 1_000.0,
        token_factory=lambda: TOKEN,
    )
    approved_plan = plan()
    grant = registry.issue(
        PROJECT_ID,
        fingerprint_document(document),
        approved_plan,
    )
    document.write_bytes(b"BBBB")
    os.utime(
        document,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    with pytest.raises(HwpApprovalError, match="문서 양식이 변경"):
        registry.consume(grant.token, PROJECT_ID, document, approved_plan)


def test_exact_sample_never_receives_apply_token_before_48_page_validator():
    registry = HwpApprovalRegistry(
        ttl_seconds=60,
        clock=lambda: 1_000.0,
        token_factory=lambda: TOKEN,
    )

    with pytest.raises(HwpApprovalError, match="48쪽"):
        registry.issue(
            PROJECT_ID,
            fingerprint_document(SAMPLE_3Q),
            plan(),
        )
