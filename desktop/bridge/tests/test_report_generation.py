from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
from typing import Any
from uuid import uuid4

import pytest

from app.contracts import DocumentEditPlan
from app.report_generation import (
    GenerationCommandResult,
    InvalidModelOutput,
    InvalidRedactedPayload,
    ProviderAuthenticationExpiredError,
    ProviderNetworkError,
    ProviderNotReadyError,
    ReportGenerationError,
    ReportGenerationService,
    run_generation_command,
)


def payload() -> dict[str, Any]:
    return {
        "project_label": "2025년 3분기 도로 실사",
        "approved_incidents": [
            {
                "incident_id": "INC-001",
                "incident_type": "VEGETATION_OCCLUSION",
                "severity": "major",
                "maintenance_action": "표지판 주변 삭초",
                "ocr_text": "○○터널",
                "location_summary": "GPS 검증 구간 12.4 km",
                "time_ms": 123_000,
                "evidence_refs": ["deid:INC-001:representative"],
            }
        ],
        "document_structure_summary": {
            "document_snapshot_id": "DOC-SNAPSHOT-1",
            "field_targets": ["field.summary"],
            "cell_targets": ["cell.incident-count"],
            "table_targets": ["table.incidents"],
            "image_targets": ["image.evidence-1"],
            "map_targets": ["map.route"],
            "section_headings": ["점검 개요", "주요 조치"],
        },
        "map_refs": ["map:route-overview"],
    }


def model_output(**operation_changes: Any) -> dict[str, Any]:
    operation = {
        "operation_id": str(uuid4()),
        "type": "fill_text",
        "target": "field.summary",
        "value": "식생 가림 1건을 확인하여 삭초가 필요합니다.",
        "source_ref": "incident:INC-001",
        "style_policy": "preserve",
        "preview": "점검 요약 필드에 확정 결과를 입력합니다.",
    }
    operation.update(operation_changes)
    return {
        "draft": {
            "title": "도로 실사 점검보고서",
            "sections": [
                {
                    "heading": "점검 결과",
                    "body": "식생에 가려진 표지판 1건을 확인했습니다.",
                    "incident_ids": ["INC-001"],
                }
            ],
        },
        "plan": {
            "schema_version": "1.0",
            "plan_id": str(uuid4()),
            "document_snapshot_id": "DOC-SNAPSHOT-1",
            "operations": [operation],
        },
    }


def fake_cli(tmp_path: Path, provider: str) -> Path:
    name = f"{provider}.exe"
    path = tmp_path / name
    path.write_bytes(b"MZ")
    return path


class Runner:
    def __init__(self, result: GenerationCommandResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(self, argv: Any, **kwargs: Any) -> GenerationCommandResult:
        self.calls.append((tuple(argv), kwargs))
        return self.result


def service(
    tmp_path: Path,
    runner: Runner,
    provider: str = "codex",
    *,
    ready: bool = True,
) -> ReportGenerationService:
    return ReportGenerationService(
        explicit_paths={provider: fake_cli(tmp_path, provider)},
        provider_ready={provider: ready},
        runner=runner,
        environ={"SYSTEMROOT": r"C:\Windows", "SECRET_TOKEN": "do-not-pass"},
        timeout_seconds=5,
    )


def test_codex_jsonl_generates_bounded_draft_and_plan(tmp_path: Path) -> None:
    output = model_output()
    events = [
        {"type": "thread.started", "thread_id": "opaque"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps(output, ensure_ascii=False),
                "model": "gpt-5.6-codex",
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 120, "output_tokens": 80, "secret": 99},
        },
    ]
    stdout = ("\n".join(json.dumps(item) for item in events) + "\n").encode()
    runner = Runner(GenerationCommandResult(0, stdout=stdout))
    result = service(tmp_path, runner).generate("codex", payload()).as_dict()

    assert result["provider"] == "codex"
    assert result["model"] == "gpt-5.6-codex"
    assert result["usage"] == {"input_tokens": 120, "output_tokens": 80}
    assert result["draft"]["sections"][0]["incident_ids"] == ["INC-001"]
    assert result["plan"]["operations"][0]["type"] == "fill_text"
    argv, call = runner.calls[0]
    assert argv[1:] == (
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "-",
    )
    assert call["env"] == {"SYSTEMROOT": r"C:\Windows"}
    assert b"raw_video" not in call["stdin"]
    assert b"Never request tools" in call["stdin"]


def test_claude_uses_noninteractive_json_schema_flags(tmp_path: Path) -> None:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(model_output(), ensure_ascii=False),
        "usage": {"input_tokens": 9, "output_tokens": 7},
        "modelUsage": {"claude-sonnet-4-5": {"inputTokens": 9}},
    }
    runner = Runner(
        GenerationCommandResult(0, stdout=json.dumps(envelope).encode("utf-8"))
    )
    result = service(tmp_path, runner, "claude").generate(
        "claude", payload()
    ).as_dict()
    argv, _ = runner.calls[0]
    assert argv[1:4] == ("-p", "--output-format", "json")
    assert "--json-schema" in argv
    assert "--max-turns" not in argv
    assert "--tools" in argv
    assert "--no-session-persistence" in argv
    assert result["model"] == "claude-sonnet-4-5"


@pytest.mark.parametrize(
    "raw",
    [
        b"{broken",
        b"Here is the JSON: {}",
        b"```json\n{}\n```",
    ],
)
def test_rejects_malformed_or_prose_wrapped_json(
    tmp_path: Path, raw: bytes
) -> None:
    runner = Runner(GenerationCommandResult(0, stdout=raw))
    with pytest.raises(InvalidModelOutput):
        service(tmp_path, runner).generate("codex", payload())


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "run_macro"},
        {"target": r"C:\Users\operator\report.hwp"},
        {"source_ref": r"C:\evidence.jpg"},
    ],
)
def test_rejects_arbitrary_operation_or_path_fields(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    runner = Runner(
        GenerationCommandResult(
            0, stdout=json.dumps(model_output(**changes)).encode()
        )
    )
    with pytest.raises(InvalidModelOutput):
        service(tmp_path, runner).generate("codex", payload())


def test_rejects_unknown_model_fields(tmp_path: Path) -> None:
    output = model_output()
    output["plan"]["operations"][0]["command"] = "powershell"
    runner = Runner(GenerationCommandResult(0, stdout=json.dumps(output).encode()))
    with pytest.raises(InvalidModelOutput):
        service(tmp_path, runner).generate("codex", payload())


@pytest.mark.parametrize(
    "bad_key,bad_value",
    [
        ("raw_video", "video bytes"),
        ("video_path", r"\\server\share\trip.mp4"),
        ("email", "person@example.com"),
        ("phone_number", "010-0000-0000"),
    ],
)
def test_rejects_raw_video_path_and_contact_fields(
    tmp_path: Path, bad_key: str, bad_value: str
) -> None:
    unsafe = payload()
    unsafe["approved_incidents"][0][bad_key] = bad_value
    runner = Runner(GenerationCommandResult(0, stdout=b"{}"))
    with pytest.raises(InvalidRedactedPayload):
        service(tmp_path, runner).generate("codex", unsafe)
    assert runner.calls == []


def test_rejects_non_deidentified_evidence_reference(tmp_path: Path) -> None:
    unsafe = payload()
    unsafe["approved_incidents"][0]["evidence_refs"] = [
        r"\\server\share\person.jpg"
    ]
    runner = Runner(GenerationCommandResult(0, stdout=b"{}"))
    with pytest.raises(InvalidRedactedPayload):
        service(tmp_path, runner).generate("codex", unsafe)


@pytest.mark.parametrize(
    "value",
    [
        "담당자 person@example.com 확인",
        "연락처 010-1234-5678",
        r"원본 C:\Users\operator\trip.mp4",
        r"원본 \\server\share\trip.mp4",
        "원본 영상 trip.mov",
    ],
)
def test_rejects_sensitive_values_even_under_allowed_text_key(
    tmp_path: Path, value: str
) -> None:
    unsafe = payload()
    unsafe["approved_incidents"][0]["ocr_text"] = value
    runner = Runner(GenerationCommandResult(0, stdout=b"{}"))
    with pytest.raises(InvalidRedactedPayload):
        service(tmp_path, runner).generate("codex", unsafe)
    assert runner.calls == []


def test_rejects_oversized_input_before_runner(tmp_path: Path) -> None:
    unsafe = payload()
    unsafe["approved_incidents"][0]["maintenance_action"] = "가" * 300_000
    runner = Runner(GenerationCommandResult(0, stdout=b"{}"))
    with pytest.raises(InvalidRedactedPayload):
        service(tmp_path, runner).generate("codex", unsafe)
    assert runner.calls == []


def test_rejects_oversized_output(tmp_path: Path) -> None:
    runner = Runner(
        GenerationCommandResult(
            0,
            stdout=b"{" + b"x" * 600_000,
            output_truncated=True,
        )
    )
    with pytest.raises(ReportGenerationError, match="safe limit"):
        service(tmp_path, runner).generate("codex", payload())


def test_timeout_does_not_expose_stderr_or_path(tmp_path: Path) -> None:
    runner = Runner(
        GenerationCommandResult(
            None,
            stderr=b"secret at C:\\Users\\operator\\report.hwp",
            timed_out=True,
        )
    )
    with pytest.raises(ReportGenerationError) as caught:
        service(tmp_path, runner).generate("codex", payload())
    assert "operator" not in str(caught.value)
    assert "report.hwp" not in str(caught.value)


def test_cancelled_is_caller_safe(tmp_path: Path) -> None:
    runner = Runner(GenerationCommandResult(None, cancelled=True))
    with pytest.raises(ReportGenerationError, match="cancelled"):
        service(tmp_path, runner).generate(
            "codex", payload(), cancel_event=threading.Event()
        )


@pytest.mark.parametrize(
    ("stderr", "error_type"),
    [
        (
            b"401 Unauthorized: failed to refresh token user@example.com",
            ProviderAuthenticationExpiredError,
        ),
        (
            b"network is unreachable at C:\\secret\\provider",
            ProviderNetworkError,
        ),
    ],
)
def test_provider_auth_and_network_failures_are_distinct_and_scrubbed(
    tmp_path: Path,
    stderr: bytes,
    error_type,
) -> None:
    runner = Runner(
        GenerationCommandResult(returncode=1, stderr=stderr)
    )

    with pytest.raises(error_type) as caught:
        service(tmp_path, runner).generate("codex", payload())

    assert "example.com" not in str(caught.value)
    assert "secret" not in str(caught.value).casefold()


def test_real_runner_kills_timeout_with_bounded_error_output() -> None:
    result = run_generation_command(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdin=b"",
        timeout_seconds=0.1,
        output_limit_bytes=16 * 1024,
        env={},
        cancel_event=None,
    )
    assert result.timed_out is True
    assert result.returncode is not None


def test_real_runner_kills_when_output_limit_is_exceeded() -> None:
    result = run_generation_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100000)"],
        stdin=b"",
        timeout_seconds=5,
        output_limit_bytes=16 * 1024,
        env={},
        cancel_event=None,
    )
    assert result.output_truncated is True
    assert len(result.stdout) <= 8 * 1024


def test_real_runner_uses_an_empty_ephemeral_working_directory() -> None:
    result = run_generation_command(
        [
            sys.executable,
            "-c",
            (
                "import json, os; "
                "print(json.dumps({'cwd': os.path.basename(os.getcwd()), "
                "'entries': os.listdir('.')}))"
            ),
        ],
        stdin=b"",
        timeout_seconds=5,
        output_limit_bytes=16 * 1024,
        env={},
        cancel_event=None,
    )
    observed = json.loads(result.stdout)

    assert result.returncode == 0
    assert observed["cwd"].startswith("trafficvisson-report-provider-")
    assert observed["entries"] == []


def test_provider_must_be_ready_and_runner_is_not_called(tmp_path: Path) -> None:
    runner = Runner(GenerationCommandResult(0, stdout=b"{}"))
    with pytest.raises(ProviderNotReadyError):
        service(tmp_path, runner, ready=False).generate("codex", payload())
    assert runner.calls == []


def test_executable_must_be_absolute_official_binary(tmp_path: Path) -> None:
    runner = Runner(GenerationCommandResult(0, stdout=b"{}"))
    svc = ReportGenerationService(
        explicit_paths={"codex": "evil.exe"},
        provider_ready={"codex": True},
        runner=runner,
    )
    with pytest.raises(ProviderNotReadyError):
        svc.generate("codex", payload())
    assert runner.calls == []


def test_windows_npm_cmd_is_resolved_to_node_entry_without_shell(
    tmp_path: Path,
) -> None:
    npm_root = tmp_path / "npm"
    entry = npm_root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("/* trusted test entry */", encoding="utf-8")
    shim = npm_root / "codex.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    node = tmp_path / "node.exe"
    node.write_bytes(b"MZ")

    output = model_output()
    runner = Runner(
        GenerationCommandResult(0, stdout=json.dumps(output).encode("utf-8"))
    )

    def which(name: str) -> str | None:
        return str(node) if name == "node" else None

    svc = ReportGenerationService(
        explicit_paths={"codex": shim},
        provider_ready={"codex": True},
        which=which,
        runner=runner,
        environ={"SYSTEMROOT": r"C:\Windows"},
        timeout_seconds=5,
    )
    svc.generate("codex", payload())
    argv, kwargs = runner.calls[0]
    assert argv[:2] == (str(node.resolve()), str(entry.resolve()))
    assert argv[2:] == (
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "-",
    )
    assert kwargs["env"]["SYSTEMROOT"] == r"C:\Windows"


def test_metadata_is_omitted_when_not_safely_parseable(tmp_path: Path) -> None:
    output = model_output()
    envelope = {
        "result": json.dumps(output),
        "model": r"C:\secret\model",
        "usage": {"input_tokens": -1, "output_tokens": "many"},
    }
    runner = Runner(
        GenerationCommandResult(0, stdout=json.dumps(envelope).encode())
    )
    result = service(tmp_path, runner).generate("codex", payload()).as_dict()
    assert "model" not in result
    assert "usage" not in result


def test_plan_references_only_supplied_targets_and_sources(tmp_path: Path) -> None:
    output = model_output(
        type="insert_image",
        target="image.evidence-1",
        source_ref="deid:INC-001:representative",
    )
    runner = Runner(GenerationCommandResult(0, stdout=json.dumps(output).encode()))
    result = service(tmp_path, runner).generate("codex", payload())
    assert result.plan["operations"][0]["source_ref"].startswith("deid:")
    assert DocumentEditPlan.model_validate(result.plan).schema_version == "1.0"


def test_unknown_incident_in_draft_is_rejected(tmp_path: Path) -> None:
    output = model_output()
    output["draft"]["sections"][0]["incident_ids"] = ["INC-999"]
    runner = Runner(GenerationCommandResult(0, stdout=json.dumps(output).encode()))
    with pytest.raises(InvalidModelOutput):
        service(tmp_path, runner).generate("codex", payload())
