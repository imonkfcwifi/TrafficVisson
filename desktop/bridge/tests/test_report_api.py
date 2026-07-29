from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.gpx_tracks import GpxTrackError
from app.pipeline_jobs import PipelineJobNotFound
from app.provider_status import CommandResult, ProviderStatusService
from app.report_generation import (
    GenerationCommandResult,
    ReportGenerationService,
)
from app.store import DEMO_PROJECT_ID


TOKEN = "report-api-test-token-with-at-least-256-bits-000000000"
SOURCE_SHA256 = "a" * 64


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


def generation_request(
    incident_id: str,
    analysis_job_id: str,
) -> dict[str, Any]:
    return {
        "project_id": str(DEMO_PROJECT_ID),
        "analysis_job_id": analysis_job_id,
        "source_file_sha256": SOURCE_SHA256,
        "provider": "codex",
        "consent": True,
        "project_label": "3분기 민자고속도로 실사",
        "incident_ids": [incident_id],
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
) -> tuple[TestClient, GenerationRunner, str, str]:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"MZ")
    status_output = b"Logged in" if connected else b"Not logged in"

    class ConfiguredStatusRunner:
        def __call__(self, _argv: Any, **_kwargs: Any) -> CommandResult:
            return CommandResult(returncode=0, stdout=status_output)

    incident_id = str(uuid4())
    analysis_job_id = str(uuid4())
    snapshot_id = str(uuid4())
    pipeline_job_id = str(uuid4())
    pipeline_input_fingerprint = "b" * 64
    pipeline_profile_sha256 = "c" * 64
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
    active_incidents = [
        {
            "incident_id": incident_id,
            "project_id": str(DEMO_PROJECT_ID),
            "question_id": incident_id,
            "analysis_job_id": analysis_job_id,
            "candidate_id": "0123456789abcdefabcd",
            "status": "active",
            "issue_type": "sign_vegetation_occlusion",
            "severity": "major",
            "maintenance_action": "표지판 주변 삭초",
            "ocr_text": "정안알밤휴게소",
            "representative_time_ms": 123_000,
            "evidence_image_ref": (
                f"analysis:{analysis_job_id}:representative.jpg"
            ),
            "preview_clip_ref": (
                f"analysis:{analysis_job_id}:preview.mp4"
            ),
        }
    ]

    def get_analysis_job(project_id: Any, job_id: Any) -> dict[str, Any]:
        if (
            str(project_id) != str(DEMO_PROJECT_ID)
            or str(job_id) != analysis_job_id
        ):
            raise AssertionError("report route crossed the analysis boundary")
        return {
            "job_id": analysis_job_id,
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": snapshot_id,
            "source_sha256": SOURCE_SHA256,
            "status": "completed",
            "result": {
                "provenance": {
                    "source_sha256": SOURCE_SHA256,
                }
            },
        }

    def list_incidents(
        project_id: Any,
        *,
        include_inactive: bool = False,
        analysis_job_id: Any = None,
    ) -> list[dict[str, Any]]:
        if (
            str(project_id) != str(DEMO_PROJECT_ID)
            or include_inactive
            or str(analysis_job_id) != analysis_job_id_text
        ):
            return []
        return [dict(row) for row in active_incidents]

    analysis_job_id_text = analysis_job_id
    app.state.analysis_jobs.get = get_analysis_job
    app.state.store.list_incidents = list_incidents

    def get_pipeline_job(project_id: Any, job_id: Any) -> dict[str, Any]:
        if (
            str(project_id) != str(DEMO_PROJECT_ID)
            or str(job_id) != pipeline_job_id
        ):
            raise PipelineJobNotFound("Pipeline unavailable")
        return {
            "job_id": pipeline_job_id,
            "project_id": str(DEMO_PROJECT_ID),
            "snapshot_id": snapshot_id,
            "input_fingerprint": pipeline_input_fingerprint,
            "status": "awaiting_review",
            "provenance": {
                "profile_sha256": pipeline_profile_sha256,
            },
            "stages": [
                {
                    "name": "sha256",
                    "checkpoint": {
                        "source_sha256": SOURCE_SHA256,
                    },
                }
            ],
        }

    app.state.pipeline_jobs.get = get_pipeline_job

    def get_track(project_id: Any, trip_id: Any) -> dict[str, Any]:
        if (
            str(project_id) != str(DEMO_PROJECT_ID)
            or str(trip_id) != analysis_job_id
        ):
            raise GpxTrackError("trip_unavailable", "Trip unavailable")
        return {
            "route_points": [
                {
                    "video_time_seconds": 120.0,
                    "latitude": 36.100000,
                    "longitude": 127.100000,
                    "gap_from_previous": False,
                },
                {
                    "video_time_seconds": 126.0,
                    "latitude": 36.200000,
                    "longitude": 127.200000,
                    "gap_from_previous": False,
                },
            ]
        }

    app.state.gpx_tracks.get_track = get_track
    app.state.test_report_incidents = active_incidents
    app.state.test_report_pipeline_binding = {
        "pipeline_job_id": pipeline_job_id,
        "pipeline_input_fingerprint": pipeline_input_fingerprint,
        "pipeline_profile_sha256": pipeline_profile_sha256,
    }
    client = TestClient(app)
    client.__enter__()
    return client, generation_runner, incident_id, analysis_job_id


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_production_generate_uses_only_validated_redacted_payload(
    tmp_path: Path,
) -> None:
    client, runner, incident_id, analysis_job_id = production_client(tmp_path)
    try:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=generation_request(incident_id, analysis_job_id),
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
    assert '"analysis_job_id"' not in sent_prompt
    assert '"source_file_sha256"' not in sent_prompt
    assert '"approved_incidents"' in sent_prompt
    assert '"document_structure_summary"' in sent_prompt
    assert '"evidence_refs"' in sent_prompt
    assert '"incident_type":"sign_vegetation_occlusion"' in sent_prompt
    assert '"severity":"major"' in sent_prompt
    assert '"maintenance_action":"표지판 주변 삭초"' in sent_prompt
    assert '"ocr_text":"정안알밤휴게소"' in sent_prompt
    assert '"location_summary":"GPS 36.150000, 127.150000' in sent_prompt
    assert (
        f'"analysis:{analysis_job_id}:representative.jpg"' in sent_prompt
    )
    assert f'"analysis:{analysis_job_id}:preview.mp4"' not in sent_prompt
    assert "SECRET_TOKEN" not in sent_prompt
    lowered_response = response.text.casefold()
    assert "path" not in lowered_response
    assert "token" not in lowered_response
    assert "secret" not in lowered_response


def test_generate_requires_consent_before_provider_execution(tmp_path: Path) -> None:
    client, runner, incident_id, analysis_job_id = production_client(tmp_path)
    try:
        request = generation_request(incident_id, analysis_job_id)
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
    client, runner, incident_id, analysis_job_id = production_client(
        tmp_path,
        connected=False,
    )
    try:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=generation_request(incident_id, analysis_job_id),
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 409
    assert runner.calls == []


def test_generate_rejects_renderer_supplied_incident_content(
    tmp_path: Path,
) -> None:
    client, runner, incident_id, analysis_job_id = production_client(tmp_path)
    try:
        request = generation_request(incident_id, analysis_job_id)
        request["approved_incidents"] = [
            {
                "incident_id": incident_id,
                "severity": "urgent",
                "evidence_refs": ["analysis:renderer-forged"],
            }
        ]
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=request,
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 422
    assert runner.calls == []


def test_generate_rejects_unknown_or_cross_scope_incident_ids(
    tmp_path: Path,
) -> None:
    client, runner, _incident_id, analysis_job_id = production_client(tmp_path)
    try:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=generation_request(str(uuid4()), analysis_job_id),
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 409
    assert "active approved incident" in response.json()["detail"]
    assert runner.calls == []


def test_generate_rejects_inactive_or_wrong_job_catalog_rows(
    tmp_path: Path,
) -> None:
    client, runner, incident_id, analysis_job_id = production_client(tmp_path)
    incidents = client.app.state.test_report_incidents
    incidents[0]["status"] = "superseded"
    try:
        inactive = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=generation_request(incident_id, analysis_job_id),
        )
        incidents[0]["status"] = "active"
        incidents[0]["analysis_job_id"] = str(uuid4())
        wrong_job = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=generation_request(incident_id, analysis_job_id),
        )
    finally:
        client.__exit__(None, None, None)

    assert inactive.status_code == 409
    assert wrong_job.status_code == 409
    assert runner.calls == []


def test_generate_rejects_analysis_source_digest_mismatch(
    tmp_path: Path,
) -> None:
    client, runner, incident_id, analysis_job_id = production_client(tmp_path)
    try:
        request = generation_request(incident_id, analysis_job_id)
        request["source_file_sha256"] = "b" * 64
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=request,
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 409
    assert "provenance" in response.json()["detail"]
    assert runner.calls == []


def test_generate_accepts_only_a_complete_server_matched_pipeline_binding(
    tmp_path: Path,
) -> None:
    client, runner, incident_id, analysis_job_id = production_client(tmp_path)
    try:
        request = generation_request(incident_id, analysis_job_id)
        request.update(client.app.state.test_report_pipeline_binding)
        accepted = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=request,
        )
        request["pipeline_input_fingerprint"] = "d" * 64
        mismatch = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=request,
        )
    finally:
        client.__exit__(None, None, None)

    assert accepted.status_code == 201
    assert mismatch.status_code == 409
    assert len(runner.calls) == 1
    sent_prompt = runner.calls[0]["stdin"].decode("utf-8")
    assert '"pipeline_job_id"' not in sent_prompt
    assert '"pipeline_input_fingerprint"' not in sent_prompt
    assert '"pipeline_profile_sha256"' not in sent_prompt


def test_generate_rejects_partial_pipeline_provenance_binding(
    tmp_path: Path,
) -> None:
    client, runner, incident_id, analysis_job_id = production_client(tmp_path)
    try:
        request = generation_request(incident_id, analysis_job_id)
        request["pipeline_job_id"] = str(uuid4())
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=request,
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 422
    assert runner.calls == []


def test_generate_preserves_server_unassessed_severity_without_invention(
    tmp_path: Path,
) -> None:
    client, runner, incident_id, analysis_job_id = production_client(tmp_path)
    incidents = client.app.state.test_report_incidents
    incidents[0]["severity"] = None
    incidents[0]["maintenance_action"] = None
    try:
        response = client.post(
            f"/projects/{DEMO_PROJECT_ID}/reports/generate",
            headers=auth(),
            json=generation_request(incident_id, analysis_job_id),
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 201
    sent_prompt = runner.calls[0]["stdin"].decode("utf-8")
    assert '"severity":"unassessed"' in sent_prompt
    assert '"maintenance_action"' not in sent_prompt
    assert "moderate" not in sent_prompt


def test_production_legacy_report_routes_never_use_demo_results(
    tmp_path: Path,
) -> None:
    client, _runner, _incident_id, _analysis_job_id = production_client(
        tmp_path
    )
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
