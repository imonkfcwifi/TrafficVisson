from __future__ import annotations

import hashlib
import re
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
)
from app.main import create_app
from app.store import DEMO_PROJECT_ID


TEST_TOKEN = "test-only-hwp-api-token-with-at-least-256-bits-000000"
ROLLBACK_TOKEN = "a" * 32


class FakeHwpComService:
    """Filesystem-bounded HWP fake; it never starts COM or opens Hancom."""

    def __init__(self) -> None:
        self.execute_calls = []
        self.rollback_calls = []
        self.closed = False
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
    ) -> HwpPreviewResult | HwpApplyResult:
        template_path = Path(template_path)
        self.execute_calls.append(
            {
                "template_path": template_path,
                "edit_plan": edit_plan,
                "accept": accept,
                "output_path": output_path,
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
            )

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
            message="TrafficVisson 출력 문서를 원본 양식 상태로 되돌렸습니다.",
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def hwp_api():
    service = FakeHwpComService()
    app = create_app(
        TEST_TOKEN,
        fixture_mode=True,
        allow_test_client=True,
        hwp_service=service,
    )
    with TestClient(app) as client:
        yield client, service
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


def test_hwp_preflight_uses_injected_service(hwp_api):
    client, service = hwp_api

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
    client, service = hwp_api
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
    client, service = hwp_api
    template = tmp_path / "inspection-template.hwpx"
    original = b"original-template-bytes"
    template.write_bytes(original)

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=valid_hwp_request(template),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "applied"
    assert payload["rollback_token"] == ROLLBACK_TOKEN
    assert re.fullmatch(
        r"inspection-template_TrafficVisson_[0-9a-f]{8}\.hwpx",
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


def test_hwp_rollback_requires_and_uses_apply_token_without_exposing_path(
    hwp_api, tmp_path
):
    client, service = hwp_api
    template = tmp_path / "rollback-template.hwp"
    original = b"original-hwp-test-fixture"
    template.write_bytes(original)
    applied = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=valid_hwp_request(template),
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
    client, service = hwp_api
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
