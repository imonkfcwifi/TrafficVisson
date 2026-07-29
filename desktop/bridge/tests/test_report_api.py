from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.provider_status import CommandResult, ProviderStatusService
from app.report_generation import (
    GenerationCommandResult,
    ReportGenerationService,
)
from app.store import DEMO_PROJECT_ID


TOKEN = "report-api-test-token-with-at-least-256-bits-000000000"


class GenerationRunner:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, _argv: Any, **kwargs: Any) -> GenerationCommandResult:
        self.calls.append(kwargs)
        return GenerationCommandResult(
            returncode=0,
            stdout=json.dumps(self._result, ensure_ascii=False).encode("utf-8"),
        )


def generation_request(incident_id: str) -> dict[str, Any]:
    return {
        "project_id": str(DEMO_PROJECT_ID),
        "provider": "codex",
        "consent": True,
        "project_label": "3분기 민자고속도로 실사",
        "approved_incidents": [
            {
                "incident_id": incident_id,
                "review_state": "approved",
                "incident_type": "vegetation_occlusion",
                "severity": "major",
                "maintenance_action": "표지판 주변 삭초",
                "ocr_text": "정안알밤휴게소",
                "location_summary": "GPS 검증 구간 12.4 km",
                "time_ms": 123_000,
                "evidence_refs": [f"deid:{incident_id}:representative"],
            }
        ],
        "document_structure_summary": {
            "document_snapshot_id": "DOC-SNAPSHOT-1",
            "field_targets": ["field.summary"],
            "cell_targets": [],
            "table_targets": [],
            "image_targets": [],
            "map_targets": [],
            "section_headings": ["점검 결과"],
        },
        "map_refs": ["map:route-overview"],
    }


def model_result(incident_id: str) -> dict[str, Any]:
    return {
        "draft": {
            "title": "도로 실사 점검보고서",
            "sections": [
                {
                    "heading": "점검 결과",
                    "body": "식생 가림 1건을 확인하여 삭초가 필요합니다.",
                    "incident_ids": [incident_id],
                }
            ],
        },
        "plan": {
            "schema_version": "1.0",
            "plan_id": str(uuid4()),
            "document_snapshot_id": "DOC-SNAPSHOT-1",
            "operations": [
                {
                    "operation_id": str(uuid4()),
                    "type": "fill_text",
                    "target": "field.summary",
                    "value": "식생 가림 1건을 확인했습니다.",
                    "source_ref": f"incident:{incident_id}",
                    "style_policy": "preserve",
                    "preview": "점검 결과 필드에 승인 사건 요약을 입력합니다.",
                }
            ],
        },
    }


def production_client(
    tmp_path: Path,
    *,
    connected: bool = True,
) -> tuple[TestClient, GenerationRunner, str]:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"MZ")
    status_output = b"Logged in" if connected else b"Not logged in"

    class ConfiguredStatusRunner:
        def __call__(self, _argv: Any, **_kwargs: Any) -> CommandResult:
            return CommandResult(returncode=0, stdout=status_output)

    incident_id = str(uuid4())
    generation_runner = GenerationRunner(model_result(incident_id))
    status_service = ProviderStatusService(
        explicit_paths={"codex": executable},
        runner=ConfiguredStatusRunner(),
    )
    generation_service = ReportGenerationService(
        explicit_paths={"codex": executable},
        provider_ready={"codex": True},
        runner=generation_runner,
        environ={"SYSTEMROOT": r"C:\Windows", "SECRET_TOKEN": "do-not-pass"},
        timeout_seconds=5,
    )
    app = create_app(
        TOKEN,
        fixture_mode=False,
        allow_test_client=True,
        bridge_data_root=str(tmp_path / "bridge-data"),
        provider_status_service=status_service,
        report_generation_service=generation_service,
    )
    client = TestClient(app)
    client.__enter__()
    return client, generation_runner, incident_id


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_production_generate_uses_only_validated_redacted_payload(
    tmp_path: Path,
) -> None:
    client, runner, incident_id = production_client(tmp_path)
    try:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=generation_request(incident_id),
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 201
    payload = response.json()
    assert payload["state"] == "generated_preview_required"
    assert payload["provider"] == "codex"
    assert payload["draft"]["sections"][0]["incident_ids"] == [incident_id]
    assert payload["plan"]["operations"][0]["source_ref"] == (
        f"incident:{incident_id}"
    )
    assert payload["transmitted_fields"] == [
        "project_label",
        "approved_incidents",
        "document_structure_summary",
        "map_refs",
    ]

    sent_prompt = runner.calls[0]["stdin"].decode("utf-8")
    assert '"review_state"' not in sent_prompt
    assert '"approved_incidents"' in sent_prompt
    assert '"document_structure_summary"' in sent_prompt
    assert '"evidence_refs"' in sent_prompt
    assert "SECRET_TOKEN" not in sent_prompt
    lowered_response = response.text.casefold()
    assert "path" not in lowered_response
    assert "token" not in lowered_response
    assert "secret" not in lowered_response


def test_generate_requires_consent_before_provider_execution(tmp_path: Path) -> None:
    client, runner, incident_id = production_client(tmp_path)
    try:
        request = generation_request(incident_id)
        request["consent"] = False
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=request,
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 412
    assert runner.calls == []


def test_generate_requires_connected_official_provider(tmp_path: Path) -> None:
    client, runner, incident_id = production_client(tmp_path, connected=False)
    try:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=generation_request(incident_id),
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 409
    assert runner.calls == []


def test_generate_rejects_unredacted_path_without_echoing_it(
    tmp_path: Path,
) -> None:
    client, runner, incident_id = production_client(tmp_path)
    try:
        request = generation_request(incident_id)
        request["approved_incidents"][0]["location_summary"] = (
            r"C:\Users\operator\trip.mp4"
        )
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=request,
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 422
    assert runner.calls == []
    assert "operator" not in response.text
    assert "trip.mp4" not in response.text


def test_production_legacy_report_routes_never_use_demo_results(
    tmp_path: Path,
) -> None:
    client, _runner, _incident_id = production_client(tmp_path)
    try:
        draft = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/draft",
            headers=auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "provider": "codex",
                "incident_ids": [],
                "consent": True,
            },
        )
        plan = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/plan",
            headers=auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "draft_id": str(uuid4()),
                "document_snapshot_id": "DOC-SNAPSHOT-1",
            },
        )
        operation_id = str(uuid4())
        apply = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/apply",
            headers=auth(),
            json={
                "project_id": str(DEMO_PROJECT_ID),
                "plan": {
                    "schema_version": "1.0",
                    "plan_id": str(uuid4()),
                    "document_snapshot_id": "DOC-SNAPSHOT-1",
                    "operations": [
                        {
                            "operation_id": operation_id,
                            "type": "fill_text",
                            "target": "field.summary",
                            "value": "검증 문장",
                            "source_ref": "incident:opaque",
                            "style_policy": "preserve",
                            "preview": "미리보기",
                        }
                    ],
                },
            },
        )
    finally:
        client.__exit__(None, None, None)

    assert draft.status_code == 409
    assert plan.status_code == 409
    assert apply.status_code == 409
    assert "fixture_draft" not in draft.text
    assert "preview_validation_only" not in apply.text
