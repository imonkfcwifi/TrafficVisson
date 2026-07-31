from __future__ import annotations

import hashlib
import json
import re
import zipfile
from copy import deepcopy
from dataclasses import asdict
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
from app.contracts import DocumentEditPlan
from app.hwp_approval import (
    HwpApprovalRegistry,
    HwpSampleApprovalRegistry,
)
from app.hwpx_sample_layout import (
    bind_sample_reference_case,
    inspect_sample_layout,
)
from app.main import create_app
from app.store import DEMO_PROJECT_ID


TEST_TOKEN = "test-only-hwp-api-token-with-at-least-256-bits-000000"
ROLLBACK_TOKEN = "a" * 32
FAKE_HWP_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
WORKSPACE = Path(__file__).resolve().parents[3]
SAMPLE_3Q = (
    WORKSPACE / "sample" / "3분기 점검 내용 정리.hwpx"
)
SAMPLE_4Q = (
    WORKSPACE / "sample" / "4분기 점검 내용 정리.hwpx"
)


def _fake_hwp_bytes(payload: bytes) -> bytes:
    return FAKE_HWP_MAGIC + payload


def _write_minimal_non_sample_hwpx(path: Path) -> Path:
    section = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="urn:hancom:hwpml:section"
 xmlns:hp="urn:hancom:hwpml:paragraph">
 <hp:p><hp:run><hp:t>사용자 비표본 양식</hp:t></hp:run></hp:p>
</hs:sec>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/hwp+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("Contents/section0.xml", section)
    return path


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
        hwp_sample_approval_registry=HwpSampleApprovalRegistry(
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


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class StaticApprovedReferenceStore:
    def __init__(self, records):
        self.records = list(records)
        self.requested_approval_ids = []

    def list_project(self, project_id):
        group_count = len(
            {
                record["sample_evidence_group_id"]
                for record in self.records
            }
        )
        return {
            "schema_version": "trafficvisson.approved-reference-list.v2",
            "project_id": str(project_id),
            "approved_reference_count": len(self.records),
            "approved_reference_event_count": group_count,
            "training_eligible_count": len(self.records),
            "duplicate_image_reference_count": sum(
                record["duplicate_image_review_required"]
                for record in self.records
            ),
            "annotation_count": 0,
            "incident_count": 0,
            "records": deepcopy(self.records),
        }

    def list_by_approval_ids(self, project_id, approval_ids):
        requested = tuple(sorted(set(approval_ids)))
        self.requested_approval_ids.append(requested)
        return [
            deepcopy(record)
            for record in self.records
            if record.get("approval_id") in requested
        ]


def _sample_approved_reference(
    inspection,
    picture_target_id: str,
    *,
    token: str = "a",
):
    binding = bind_sample_reference_case(
        inspection,
        picture_target_id,
    )
    source_id = (
        f"sample-hwpx-{inspection.baseline.quarter}q-"
        + inspection.snapshot.document_sha256[:16]
    )
    case_id = "hwpx-weak-case-" + token * 32
    record = {
        "schema_version": "trafficvisson.approved-reference.v2",
        "approval_id": "approved-reference-" + token * 24,
        "project_id": str(DEMO_PROJECT_ID),
        "source_id": source_id,
        "case_id": case_id,
        "revision": 1,
        "reference_state": "approved_reference",
        "approved_reference": True,
        "training_eligible": True,
        "training_scope": "approved_reference_only",
        "annotation_created": False,
        "incident_created": False,
        "document_name": inspection.baseline.expected_document_name,
        "document_sha256": inspection.snapshot.document_sha256,
        "structure_target_summary_sha256": (
            inspection.snapshot.target_summary_sha256
        ),
        "index_manifest_sha256": (
            inspection.baseline.weak_case_index_manifest_sha256
        ),
        "image_sha256": token * 64,
        "image_media_type": "image/jpeg",
        "image_width": 1920,
        "image_height": 1080,
        "quarter": inspection.baseline.quarter,
        "section_path": "Contents/section0.xml",
        "section_index": 0,
        "picture_target_id": binding.picture_target_id,
        "picture_index": binding.picture_index,
        "picture_xml_id": None,
        "table_id": inspection.snapshot.tables[
            binding.photo_table_index
        ].table_id,
        "table_index": binding.photo_table_index,
        "cell_target_id": binding.photo_cell_target_id,
        "cell_row": binding.photo_cell_row,
        "cell_column": binding.photo_cell_column,
        "page_hint": binding.photo_page_number,
        "page_hint_source": "audited_three_table_pair_contract",
        "photo_slot_id": binding.photo_slot_id,
        "sample_layout_binding_schema": (
            binding.sample_layout_binding_schema
        ),
        "sample_layout_binding_revision": (
            binding.sample_layout_binding_revision
        ),
        "semantic_projection_revision": (
            binding.semantic_projection_revision
        ),
        "semantic_attestation_sha256": "e" * 64,
        "sample_evidence_group_id": (
            binding.sample_evidence_group_id
        ),
        "group_photo_index": binding.group_photo_index,
        "group_photo_count": binding.group_photo_count,
        "duplicate_image_occurrence_count": 1,
        "duplicate_image_review_required": False,
        "duplicate_image_review_confirmed": False,
        "duplicate_image_case_refs": [
            f"{source_id}/{case_id}"
        ],
        "photo_page_number": binding.photo_page_number,
        "inspection_page_number": binding.inspection_page_number,
        "sample_layout_binding_sha256": binding.binding_sha256,
        "square_markers": [
            asdict(marker) for marker in binding.square_markers
        ],
        "semantic_cells": [
            asdict(binding.photo_caption),
            asdict(binding.inspection_route),
            asdict(binding.inspection_opinion_label),
            asdict(binding.inspection_opinion_body),
        ],
        "weak_candidates": {
            "caption": None,
            "route": None,
            "location": None,
            "evaluation": None,
            "opinion": None,
        },
        "issue_type": "roadside_mowing_required",
        "maintenance_action": "삭초 및 수목 제거",
        "caption": "삭초 필요 구간",
        "route": "검증 노선",
        "location": "검증 위치",
        "evaluation": "보통",
        "opinion": "시야 확보를 위한 삭초가 필요함",
        "semantic_sha256": "d" * 64,
        "approved_at": "2026-07-29T00:00:00Z",
        "updated_at": "2026-07-29T00:00:00Z",
        "user_confirmed": True,
    }
    record["record_sha256"] = _canonical_sha256(record)
    return record


def _sample_semantic_request(template: Path):
    inspection = inspect_sample_layout(template)
    picture = next(
        item
        for item in inspection.snapshot.pictures
        if item.nearest_table_index == 2
    )
    record = _sample_approved_reference(
        inspection,
        picture.target_id,
    )
    binding = bind_sample_reference_case(
        inspection,
        picture.target_id,
    )

    def operation(
        operation_type: str,
        target: str,
        expected_value: str,
        value: str,
    ):
        return {
            "operation_id": str(uuid4()),
            "type": operation_type,
            "target": target,
            "expected_value": expected_value,
            "value": value,
            "source_ref": record["approval_id"],
            "style_policy": "preserve",
            "preview": "승인된 sample 의미 위치 미리보기",
        }

    operations = [
        operation(
            "insert_image",
            binding.picture_target_id,
            record["image_sha256"],
            "deid:incident-photo-1",
        ),
        operation(
            "fill_cell",
            binding.photo_caption.cell_target_id,
            binding.photo_caption.text_excerpt,
            "삭초 필요 구간",
        ),
        operation(
            "fill_cell",
            binding.inspection_route.cell_target_id,
            binding.inspection_route.text_excerpt,
            "노선명 : 검증 노선",
        ),
        operation(
            "fill_cell",
            binding.inspection_opinion_body.cell_target_id,
            binding.inspection_opinion_body.text_excerpt,
            "ㅇ 시야 확보를 위한 삭초가 필요함",
        ),
    ]
    operations.extend(
        operation(
            "fill_cell",
            marker.cell_target_id,
            "■",
            "■",
        )
        for marker in binding.square_markers
    )
    plan = DocumentEditPlan.model_validate(
        {
            "schema_version": "1.0",
            "plan_id": str(uuid4()),
            "document_snapshot_id": inspection.document_snapshot_id,
            "operations": operations,
        }
    )
    return (
        {
            "project_id": str(DEMO_PROJECT_ID),
            "template_path": str(template),
            "plan": plan.model_dump(mode="json"),
        },
        record,
    )


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
    template = tmp_path / "user-template.hwp"
    original = _fake_hwp_bytes(b"test-fixture")
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
    assert service.execute_calls[-1]["expected_fingerprint"].sha256 == (
        payload["document_sha256"]
    )
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


def test_invalid_hwpx_structure_never_falls_back_to_com(
    hwp_api,
    tmp_path,
):
    client, service, _now = hwp_api
    template = tmp_path / "invalid-structure.hwpx"
    with zipfile.ZipFile(template, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=valid_hwp_request(template),
    )

    assert response.status_code == 422
    assert "구조" in response.json()["detail"]
    assert service.execute_calls == []


def test_verified_non_sample_hwpx_can_use_legacy_com_preview(
    hwp_api,
    tmp_path,
):
    client, service, _now = hwp_api
    template = _write_minimal_non_sample_hwpx(
        tmp_path / "verified-user-template.hwpx"
    )

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=valid_hwp_request(template),
    )

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "preview"
    assert len(service.execute_calls) == 1


def test_exact_sample_renamed_as_hwp_is_blocked_before_com(
    hwp_api,
    tmp_path,
):
    client, service, _now = hwp_api
    template = tmp_path / "disguised-sample.hwp"
    template.write_bytes(SAMPLE_3Q.read_bytes())

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=valid_hwp_request(template),
    )

    assert response.status_code == 409
    assert "위장" in response.json()["detail"]
    assert service.execute_calls == []


def test_renamed_sample_structure_with_changed_sha_is_blocked(
    hwp_api,
    tmp_path,
):
    client, service, _now = hwp_api
    template = tmp_path / "renamed-changed-sample.hwpx"
    template.write_bytes(SAMPLE_3Q.read_bytes() + b"changed-sha")

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=valid_hwp_request(template),
    )

    assert response.status_code == 409
    assert "표본 구조" in response.json()["detail"]
    assert service.execute_calls == []


def test_hwp_apply_creates_named_sibling_without_overwriting_source(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "inspection-template.hwp"
    original = _fake_hwp_bytes(b"original-template-bytes")
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
        r"inspection-template_TrafficSuperVision_[0-9a-f]{8}\.hwp",
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
    original = _fake_hwp_bytes(b"original-hwp-test-fixture")
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


def test_hwp_rollback_token_is_bound_to_creating_project(
    hwp_api,
    tmp_path,
):
    client, service, _now = hwp_api
    template = tmp_path / "project-scoped-rollback.hwp"
    template.write_bytes(_fake_hwp_bytes(b"project-scoped"))
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
    rollback_token = applied.json()["rollback_token"]
    created = client.post(
        "/projects",
        headers=auth_headers(),
        json={"name": "롤백 격리 프로젝트"},
    )
    assert created.status_code == 201, created.text
    other_project_id = created.json()["id"]

    rejected = client.post(
        f"/projects/{other_project_id}/hwp/rollback",
        headers=auth_headers(),
        json={
            "project_id": other_project_id,
            "rollback_token": rollback_token,
        },
    )

    assert rejected.status_code == 409
    assert "현재 프로젝트가 일치하지 않습니다" in (
        rejected.json()["detail"]
    )
    assert service.rollback_calls == []

    rightful = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/rollback",
        headers=auth_headers(),
        json={
            "project_id": str(DEMO_PROJECT_ID),
            "rollback_token": rollback_token,
        },
    )
    assert rightful.status_code == 200, rightful.text
    assert service.rollback_calls == [ROLLBACK_TOKEN]


def test_hwp_rejects_unresolved_operation_before_service_execution(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "unsupported-operation.hwp"
    original = _fake_hwp_bytes(b"original")
    template.write_bytes(original)

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=valid_hwp_request(template, operation_type="insert_image"),
    )

    assert response.status_code == 409
    assert "artifact handles" in response.json()["detail"]
    assert service.execute_calls == []
    assert template.read_bytes() == original


def test_hwp_apply_rejects_changed_document_before_com_and_burns_token(
    hwp_api, tmp_path
):
    client, service, _now = hwp_api
    template = tmp_path / "changed-after-preview.hwp"
    template.write_bytes(_fake_hwp_bytes(b"previewed-document"))
    request = valid_hwp_request(template)
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 200, preview.text
    request["approval_token"] = preview.json()["approval_token"]
    calls_after_preview = len(service.execute_calls)
    template.write_bytes(_fake_hwp_bytes(b"changed-document"))

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
    template = tmp_path / "changed-plan.hwp"
    template.write_bytes(_fake_hwp_bytes(b"previewed-document"))
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
    template = tmp_path / "cross-project.hwp"
    template.write_bytes(_fake_hwp_bytes(b"previewed-document"))
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
    template = tmp_path / "expired-approval.hwp"
    template.write_bytes(_fake_hwp_bytes(b"previewed-document"))
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
    template = tmp_path / "failed-apply.hwp"
    template.write_bytes(_fake_hwp_bytes(b"previewed-document"))
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


def test_exact_sample_requires_approved_reference_before_semantic_preview(
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
    assert response.json()["detail"]["code"] == (
        "exact_current_report_context_required"
    )
    assert service.execute_calls == []


def test_exact_sample_target_preview_binds_server_approved_reference(
    hwp_api,
):
    client, service, _now = hwp_api
    request, record = _sample_semantic_request(SAMPLE_3Q)
    client.app.state.weak_case_approvals = StaticApprovedReferenceStore(
        [record]
    )
    source_sha256 = hashlib.sha256(SAMPLE_3Q.read_bytes()).hexdigest()

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )

    assert response.status_code == 409, response.text
    payload = response.json()
    detail = payload["detail"]
    assert detail["code"] == "exact_current_report_context_required"
    assert detail["state"] == "legacy_sample_plan_rejected"
    assert detail["approved_incident_bound"] is False
    assert detail["content_provenance_bound"] is False
    assert detail["com_invocation_count"] == 0
    assert detail["document_write_count"] == 0
    assert service.execute_calls == []
    assert hashlib.sha256(SAMPLE_3Q.read_bytes()).hexdigest() == source_sha256
    assert str(SAMPLE_3Q) not in response.text


def test_exact_sample_preview_ignores_unused_other_quarter_reference(
    hwp_api,
):
    client, service, _now = hwp_api
    request, used_record = _sample_semantic_request(SAMPLE_3Q)
    other_inspection = inspect_sample_layout(SAMPLE_4Q)
    other_picture = next(
        item
        for item in other_inspection.snapshot.pictures
        if item.nearest_table_index == 2
    )
    unused_record = _sample_approved_reference(
        other_inspection,
        other_picture.target_id,
        token="b",
    )
    unused_record["record_sha256"] = "0" * 64
    approval_store = StaticApprovedReferenceStore(
        [unused_record, used_record]
    )
    client.app.state.weak_case_approvals = approval_store

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "exact_current_report_context_required"
    )
    assert approval_store.requested_approval_ids == []
    assert service.execute_calls == []


def test_exact_sample_preview_rejects_used_reference_from_other_quarter(
    hwp_api,
):
    client, service, _now = hwp_api
    request, _used_record = _sample_semantic_request(SAMPLE_3Q)
    other_inspection = inspect_sample_layout(SAMPLE_4Q)
    other_picture = next(
        item
        for item in other_inspection.snapshot.pictures
        if item.nearest_table_index == 2
    )
    wrong_record = _sample_approved_reference(
        other_inspection,
        other_picture.target_id,
        token="a",
    )
    client.app.state.weak_case_approvals = StaticApprovedReferenceStore(
        [wrong_record]
    )

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "exact_current_report_context_required"
    )
    assert service.execute_calls == []


def test_exact_sample_target_preview_rejects_cross_project_reference(
    hwp_api,
):
    client, service, _now = hwp_api
    request, record = _sample_semantic_request(SAMPLE_3Q)
    record["project_id"] = str(uuid4())
    record.pop("record_sha256")
    record["record_sha256"] = _canonical_sha256(record)
    client.app.state.weak_case_approvals = StaticApprovedReferenceStore(
        [record]
    )

    response = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "exact_current_report_context_required"
    )
    assert service.execute_calls == []


def test_exact_sample_apply_consumes_preview_then_returns_unavailable_no_com(
    hwp_api,
):
    client, service, _now = hwp_api
    request, record = _sample_semantic_request(SAMPLE_3Q)
    client.app.state.weak_case_approvals = StaticApprovedReferenceStore(
        [record]
    )
    source_sha256 = hashlib.sha256(SAMPLE_3Q.read_bytes()).hexdigest()
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 409, preview.text
    assert preview.json()["detail"]["code"] == (
        "exact_current_report_context_required"
    )
    assert service.execute_calls == []
    assert hashlib.sha256(SAMPLE_3Q.read_bytes()).hexdigest() == source_sha256
    return
    request["approval_token"] = preview.json()["approval_token"]

    blocked = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["state"] == "sample_apply_unavailable"
    assert detail["target_structure_validation_sha256"] == (
        preview.json()["validation_sha256"]
    )
    assert detail["approved_incident_bound"] is False
    assert detail["content_provenance_bound"] is False
    assert detail["com_invocation_performed"] is False
    assert detail["document_write_performed"] is False
    assert service.execute_calls == []
    assert hashlib.sha256(SAMPLE_3Q.read_bytes()).hexdigest() == source_sha256

    replay = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )
    assert replay.status_code == 409
    assert service.execute_calls == []


def test_exact_sample_apply_rejects_changed_plan_and_burns_token(
    hwp_api,
):
    client, service, _now = hwp_api
    request, record = _sample_semantic_request(SAMPLE_3Q)
    client.app.state.weak_case_approvals = StaticApprovedReferenceStore(
        [record]
    )
    original_request = deepcopy(request)
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 409, preview.text
    assert preview.json()["detail"]["code"] == (
        "exact_current_report_context_required"
    )
    assert service.execute_calls == []
    return
    token = preview.json()["approval_token"]
    request["approval_token"] = token
    request["plan"]["operations"][1]["value"] = "변경된 캡션"

    rejected = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert rejected.status_code == 409
    assert "편집 계획이 변경" in rejected.json()["detail"]
    original_request["approval_token"] = token
    replay = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=original_request,
    )
    assert replay.status_code == 409
    assert service.execute_calls == []


def test_exact_sample_apply_rejects_changed_template_before_com(
    hwp_api,
    tmp_path,
):
    client, service, _now = hwp_api
    template = tmp_path / SAMPLE_3Q.name
    template.write_bytes(SAMPLE_3Q.read_bytes())
    request, record = _sample_semantic_request(template)
    client.app.state.weak_case_approvals = StaticApprovedReferenceStore(
        [record]
    )
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 409, preview.text
    assert preview.json()["detail"]["code"] == (
        "exact_current_report_context_required"
    )
    assert service.execute_calls == []
    return
    request["approval_token"] = preview.json()["approval_token"]
    template.write_bytes(template.read_bytes() + b"tampered")

    rejected = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert rejected.status_code == 409
    assert "표본 양식이 변경" in rejected.json()["detail"]
    assert service.execute_calls == []


def test_exact_sample_apply_rejects_changed_approval_set_before_com(
    hwp_api,
):
    client, service, _now = hwp_api
    request, record = _sample_semantic_request(SAMPLE_3Q)
    approval_store = StaticApprovedReferenceStore([record])
    client.app.state.weak_case_approvals = approval_store
    preview = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/preview",
        headers=auth_headers(),
        json=request,
    )
    assert preview.status_code == 409, preview.text
    assert preview.json()["detail"]["code"] == (
        "exact_current_report_context_required"
    )
    assert service.execute_calls == []
    return
    request["approval_token"] = preview.json()["approval_token"]
    changed = deepcopy(record)
    changed["opinion"] = "승인 후 변경된 의견"
    changed.pop("record_sha256")
    changed["record_sha256"] = _canonical_sha256(changed)
    approval_store.records = [changed]

    rejected = client.post(
        f"/projects/{DEMO_PROJECT_ID}/hwp/apply",
        headers=auth_headers(),
        json=request,
    )

    assert rejected.status_code == 409
    assert "승인 표본 사례가 변경" in rejected.json()["detail"]
    assert service.execute_calls == []
