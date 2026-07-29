from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.hwp_com import (
    HwpApplyResult,
    HwpAutomationError,
    HwpPreflight,
    HwpPreviewResult,
    HwpRollbackResult,
    PLAN_VERSION,
    StructureSummary,
    fingerprint_document,
)
from app.hwp_approval import HwpApprovalRegistry
from app.hwpx_sample_layout import inspect_sample_layout
from app.main import create_app
from app.store import DEMO_PROJECT_ID


TEST_TOKEN = "test-only-hwp-api-token-with-at-least-256-bits-000000"
ROLLBACK_TOKEN = "a" * 32
WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q = (
    WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"
)


class FakeHwpComService:
    """Filesystem-bounded HWP fake; it never starts COM or opens Hancom."""

    def __init__(self) -> None:
        self.execute_calls = []
        self.rollback_calls = []
        self.closed = False
        self.fail_apply = False
        self._rollbacks = {}

    def preflight(self) -> HwpPreflight:
        return HwpPreflight(
            available=True,
            prog_id_registered=True,
            security_module_registered=True,
            message="테스트용 한/글 자동화 준비 상태입니다.",
        )

    def execute(
        self,
        template_path: Path,
        edit_plan,
        *,
        accept: bool = False,
        output_path: Path | None = None,
        expected_fingerprint=None,
    ) -> HwpPreviewResult | HwpApplyResult:
        template_path = Path(template_path)
        self.execute_calls.append(
            {
                "template_path": template_path,
                "edit_plan": edit_plan,
                "accept": accept,
                "output_path": output_path,
                "expected_fingerprint": expected_fingerprint,
            }
        )
        structure = StructureSummary(
            observed_characters=128,
            truncated=False,
            paragraph_count=3,
            table_count=1,
            picture_count=0,
            field_names=("보고서_제목",),
            bookmark_names=("점검_요약",),
        )
        if not accept:
            if output_path is not None:
                raise AssertionError("preview must not receive an output path")
            return HwpPreviewResult(
                mode="preview",
                operation_count=len(edit_plan["operations"]),
                operation_types={"fill_text": len(edit_plan["operations"])},
                structure=structure,
                message="원본을 변경하지 않고 구조를 확인했습니다.",
                document_fingerprint=fingerprint_document(template_path),
            )

        if expected_fingerprint is None:
            raise AssertionError("apply requires the previewed document fingerprint")
        if self.fail_apply:
            raise HwpAutomationError("테스트용 적용 실패")
        if output_path is None:
            raise AssertionError("apply requires a distinct output path")
        output_path = Path(output_path)
        if output_path == template_path:
            raise AssertionError("source document must never be overwritten")
        if output_path.parent != template_path.parent:
            raise AssertionError("output must be a sibling of the source")
        if output_path.exists():
            raise HwpAutomationError("출력 문서가 이미 존재합니다.")

        source_bytes = template_path.read_bytes()
        output_bytes = b"trafficvisson-generated:" + source_bytes
        output_path.write_bytes(output_bytes)
        digest = hashlib.sha256(output_bytes).hexdigest()
        backup_path = template_path.with_name(".test-hwp-api-backup.hwpx")
        self._rollbacks[ROLLBACK_TOKEN] = (
            output_path,
            source_bytes,
            template_path.name,
        )
        return HwpApplyResult(
            mode="applied",
            rollback_token=ROLLBACK_TOKEN,
            output_path=output_path,
            backup_path=backup_path,
            size_bytes=len(output_bytes),
            sha256=digest,
            readback=structure,
            message="새 문서를 저장하고 다시 읽어 확인했습니다.",
        )

    def rollback(self, rollback_token: str) -> HwpRollbackResult:
        self.rollback_calls.append(rollback_token)
        try:
            output_path, source_bytes, _source_name = self._rollbacks.pop(
                rollback_token
            )
        except KeyError as error:
            raise HwpAutomationError(
                "사용 가능한 롤백 기록이 없습니다."
            ) from error
        output_path.write_bytes(source_bytes)
        digest = hashlib.sha256(source_bytes).hexdigest()
        return HwpRollbackResult(
            rolled_back=True,
            output_path=output_path,
            size_bytes=len(source_bytes),
            sha256=digest,
            message="TrafficSuperVision 출력 문서를 원본 양식 상태로 되돌렸습니다.",
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def hwp_api():
    service = FakeHwpComService()
    now = [1_000.0]
    app = create_app(
        TEST_TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        hwp_service=service,
        hwp_approval_registry=HwpApprovalRegistry(
            ttl_seconds=5,
            clock=lambda: now[0],
        ),
    )
    with TestClient(app) as client:
        yield client, service, now
    assert service.closed is True


def auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def valid_hwp_request(template: Path, *, operation_type: str = "fill_text"):
    return {
        "project_id": str(DEMO_PROJECT_ID),
        "template_path": str(template),
        "plan": {
            "schema_version": "1.0",
            "plan_id": str(uuid4()),
            "document_snapshot_id": "snapshot:test-hwp-template",
            "operations": [
                {
                    "operation_id": str(uuid4()),
                    "type": operation_type,
                    "target": "bookmark:점검_요약",
                    "expected_value": None,
                    "value": "도로 점검 결과",
                    "source_ref": "report:draft:summary",
                    "style_policy": "preserve",
                    "preview": "점검 결과 문장 삽입",
                }
            ],
        },
    }


def valid_sample_layout_request(template: Path):
    inspection = inspect_sample_layout(template)
    target = inspection.snapshot.tables[0].cells[0].target_id
    return {
        "project_id": str(DEMO_PROJECT_ID),
        "template_path": str(template),
        "plan": {
            "schema_version": "1.0",
            "plan_id": str(uuid4()),
            "document_snapshot_id": inspection.document_snapshot_id,
            "operations": [
                {
                    "operation_id": str(uuid4()),
                    "type": "fill_cell",
                    "target": target,
                    "expected_value": None,
                    "value": "검증된 점검 의견",
                    "source_ref": "incident:approved-1",
                    "style_policy": "preserve",
                    "preview": "정확한 표 셀 미리보기",
                }
            ],
        },
    }


def test_hwp_preflight_uses_injected_service(hwp_api):
    client, service, _now = hwp_api

    response = client.get("/hwp/preflight", headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "prog_id_registered": True,
        "security_module_registered": True,
        "message": "테스트용 한/글 자동화 준비 상태입니다.",
    }
    assert service.execute_calls == []


def test_hwp_preview_does_not_expose_template_path_or_modify_source(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "user-template.hwpx"
    original = b"not-a-real-hwpx-test-fixture"
    template.write_bytes(original)

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=valid_hwp_request(template),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "preview"
    assert payload["operation_count"] == 1
    assert payload["operation_types"] == {"fill_text": 1}
    assert payload["structure"]["bookmark_names"] == ["점검_요약"]
    assert re.fullmatch(r"[a-f0-9]{64}", payload["approval_token"])
    assert re.fullmatch(r"[a-f0-9]{64}", payload["document_sha256"])
    assert re.fullmatch(r"[a-f0-9]{64}", payload["plan_sha256"])
    assert payload["target_summary"] == {
        "operation_count": 1,
        "operation_types": {"fill_text": 1},
        "targets": ["bookmark:점검_요약"],
    }
    assert str(tmp_path) not in response.text
    assert str(template) not in response.text
    assert template.read_bytes() == original
    assert service.execute_calls[-1]["accept"] is False
    assert service.execute_calls[-1]["output_path"] is None
    assert service.execute_calls[-1]["edit_plan"] == {
        "version": PLAN_VERSION,
        "operations": [
            {
                "op": "fill_text",
                "target": {"kind": "bookmark", "name": "점검_요약"},
                "text": "도로 점검 결과",
            }
        ],
    }


def test_hwp_apply_creates_named_sibling_without_overwriting_source(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "inspection-template.hwpx"
    original = b"original-template-bytes"
    template.write_bytes(original)

    request = valid_hwp_request(template)
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 200, preview.text
    request["approval_token"] = preview.json()["approval_token"]
    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "applied"
    assert payload["rollback_token"] == ROLLBACK_TOKEN
    assert re.fullmatch(
        r"inspection-template_TrafficSuperVision_[0-9a-f]{8}\.hwpx",
        payload["output_file_name"],
    )
    assert str(tmp_path) not in response.text
    assert str(template) not in response.text
    assert template.read_bytes() == original

    call = service.execute_calls[-1]
    output_path = call["output_path"]
    assert call["accept"] is True
    assert output_path is not None
    assert output_path.parent == template.parent
    assert output_path != template
    assert output_path.name == payload["output_file_name"]
    assert output_path.read_bytes() == b"trafficvisson-generated:" + original
    assert payload["size_bytes"] == output_path.stat().st_size
    assert payload["sha256"] == hashlib.sha256(
        output_path.read_bytes()
    ).hexdigest()
    calls_after_apply = len(service.execute_calls)
    replay = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )
    assert replay.status_code == 409
    assert len(service.execute_calls) == calls_after_apply


def test_hwp_rollback_requires_and_uses_apply_token_without_exposing_path(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "rollback-template.hwp"
    original = b"original-hwp-test-fixture"
    template.write_bytes(original)
    request = valid_hwp_request(template)
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 200, preview.text
    request["approval_token"] = preview.json()["approval_token"]
    applied = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )
    assert applied.status_code == 200, applied.text
    output_path = service.execute_calls[-1]["output_path"]

    invalid = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/rollback",
        headers=auth_headers(),
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "rollback_token": "not-a-token",
        },
    )
    assert invalid.status_code == 422
    assert service.rollback_calls == []

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/rollback",
        headers=auth_headers(),
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "rollback_token": applied.json()["rollback_token"],
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["rolled_back"] is True
    assert payload["output_file_name"] == output_path.name
    assert service.rollback_calls == [ROLLBACK_TOKEN]
    assert output_path.read_bytes() == original
    assert str(tmp_path) not in response.text
    assert str(template) not in response.text


def test_hwp_rejects_unresolved_operation_before_service_execution(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "unsupported-operation.hwpx"
    template.write_bytes(b"original")

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=valid_hwp_request(template, operation_type="insert_image"),
    )

    assert response.status_code == 409
    assert "artifact handles" in response.json()["detail"]
    assert service.execute_calls == []
    assert template.read_bytes() == b"original"


def test_hwp_apply_rejects_changed_document_before_com_and_burns_token(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "changed-after-preview.hwpx"
    template.write_bytes(b"previewed-document")
    request = valid_hwp_request(template)
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 200, preview.text
    request["approval_token"] = preview.json()["approval_token"]
    calls_after_preview = len(service.execute_calls)
    template.write_bytes(b"changed-document")

    rejected = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert rejected.status_code == 409
    assert len(service.execute_calls) == calls_after_preview
    replay = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )
    assert replay.status_code == 409
    assert len(service.execute_calls) == calls_after_preview


def test_hwp_apply_rejects_changed_plan_before_com_and_burns_token(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "changed-plan.hwpx"
    template.write_bytes(b"previewed-document")
    request = valid_hwp_request(template)
    original_request = deepcopy(request)
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 200, preview.text
    token = preview.json()["approval_token"]
    request["approval_token"] = token
    request["plan"]["operations"][0]["value"] = "변경된 문장"
    calls_after_preview = len(service.execute_calls)

    rejected = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert rejected.status_code == 409
    assert len(service.execute_calls) == calls_after_preview
    original_request["approval_token"] = token
    replay = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=original_request,
    )
    assert replay.status_code == 409
    assert len(service.execute_calls) == calls_after_preview


def test_hwp_apply_rejects_cross_project_token_before_com(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "cross-project.hwpx"
    template.write_bytes(b"previewed-document")
    request = valid_hwp_request(template)
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 200, preview.text
    created = client.post(
        "/projects",
        headers=auth_headers(),
        json={"name": "분리된 다른 프로젝트"},
    )
    assert created.status_code == 201, created.text
    other_project_id = created.json()["id"]
    request["project_id"] = other_project_id
    request["approval_token"] = preview.json()["approval_token"]
    calls_after_preview = len(service.execute_calls)

    rejected = client.post(
        f"/projects/{other_project_id}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert rejected.status_code == 409
    assert len(service.execute_calls) == calls_after_preview


def test_hwp_apply_rejects_expired_token_before_com(hwp_api, tmp_path):
    client, service, now = hwp_api
    template = tmp_path / "expired-approval.hwpx"
    template.write_bytes(b"previewed-document")
    request = valid_hwp_request(template)
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 200, preview.text
    request["approval_token"] = preview.json()["approval_token"]
    calls_after_preview = len(service.execute_calls)
    now[0] += 6

    rejected = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert rejected.status_code == 409
    assert "만료" in rejected.json()["detail"]
    assert len(service.execute_calls) == calls_after_preview


def test_hwp_apply_failure_consumes_token_and_replay_never_calls_com(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "failed-apply.hwpx"
    template.write_bytes(b"previewed-document")
    request = valid_hwp_request(template)
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 200, preview.text
    request["approval_token"] = preview.json()["approval_token"]
    service.fail_apply = True

    failed = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert failed.status_code == 422
    apply_calls_after_failure = len(service.execute_calls)
    service.fail_apply = False
    replay = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )
    assert replay.status_code == 409
    assert len(service.execute_calls) == apply_calls_after_failure


def test_sample_layout_preview_is_server_verified_and_never_calls_com(
    hwp_api,
):
    client, service, _now = hwp_api
    request = valid_sample_layout_request(SAMPLE_3Q)

    response = client.post(
        (
            f"/projects/{DEMO_PROJECT_ID}/hwp/"
            "sample-layout-preview"
        ),
        headers=auth_headers(),
        json=request,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["state"] == (
        "validated_structure_preview_apply_blocked"
    )
    assert payload["template_id"] == "sample-3q-v1"
    assert payload["expected_rendered_page_count"] == 48
    assert payload["output_rendered_page_count"] is None
    assert payload["output_render_validation_status"] == "not_run"
    assert payload["page_pattern"] == "odd_inspection_even_photo"
    assert re.fullmatch(
        r"[a-f0-9]{64}", payload["baseline_pdf_sha256"]
    )
    assert re.fullmatch(
        r"[a-f0-9]{64}",
        payload["weak_case_index_manifest_sha256"],
    )
    assert payload["counts"]["table_count"] == 72
    assert payload["counts"]["cell_count"] == 6_353
    assert payload["counts"]["picture_count"] == 147
    assert payload["operation_bindings"][0]["page_number"] == 1
    assert payload["operation_bindings"][0]["target_kind"] == "cell"
    assert payload["apply_eligible"] is False
    assert len(payload["blocking_reasons"]) == 3
    assert payload["read_only"] is True
    assert payload["no_com"] is True
    assert payload["no_document_write"] is True
    assert "approval_token" not in payload
    assert str(SAMPLE_3Q) not in response.text
    assert service.execute_calls == []


def test_exact_sample_cannot_fall_back_to_legacy_com_preview(
    hwp_api,
):
    client, service, _now = hwp_api
    request = valid_sample_layout_request(SAMPLE_3Q)

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )

    assert response.status_code == 409
    assert "48쪽" in response.json()["detail"]
    assert "sample-layout-preview" in response.json()["detail"]
    assert service.execute_calls == []
