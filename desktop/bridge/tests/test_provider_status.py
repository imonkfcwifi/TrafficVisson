from __future__ import annotations

import subprocess

import pytest

from app.provider_status import CommandResult, ProviderStatusService


def make_cli(tmp_path, provider: str):
    executable = tmp_path / f"{provider}.exe"
    executable.write_bytes(b"MZ")
    return executable


class RecordingRunner:
    def __init__(self, result: CommandResult):
        self.result = result
        self.calls = []

    def __call__(
        self, argv, *, timeout_seconds, output_limit_bytes, env
    ) -> CommandResult:
        self.calls.append(
            {
                "argv": tuple(argv),
                "timeout_seconds": timeout_seconds,
                "output_limit_bytes": output_limit_bytes,
                "env": dict(env),
            }
        )
        return self.result


def test_codex_ready_uses_only_allowlisted_status_command_and_safe_environment(
    tmp_path,
):
    cli = make_cli(tmp_path, "codex")
    runner = RecordingRunner(
        CommandResult(
            returncode=0,
            stdout=b"Logged in using ChatGPT\nuser@example.com\norg_very_secret",
        )
    )
    service = ProviderStatusService(
        explicit_paths={"codex": cli},
        runner=runner,
        environ={
            "USERPROFILE": r"C:\Users\tester",
            "APPDATA": r"C:\Users\tester\AppData\Roaming",
            "PATH": r"C:\bin",
            "OPENAI_API_KEY": "must-not-leak",
            "ANTHROPIC_API_KEY": "must-not-leak",
        },
    )

    status = service.get_status("codex")

    assert status.state == "ready"
    assert status.installed is True
    assert runner.calls[0]["argv"] == (str(cli.resolve()), "login", "status")
    assert runner.calls[0]["timeout_seconds"] == 4.0
    assert runner.calls[0]["output_limit_bytes"] == 16 * 1024
    assert runner.calls[0]["env"] == {
        "USERPROFILE": r"C:\Users\tester",
        "APPDATA": r"C:\Users\tester\AppData\Roaming",
    }
    serialized = repr(status.as_dict())
    assert "user@example.com" not in serialized
    assert "org_very_secret" not in serialized
    assert str(cli) not in serialized


@pytest.mark.parametrize(
    ("output", "returncode", "state"),
    [
        (b"Not logged in", 1, "not_connected"),
        (b"Login required", 0, "not_connected"),
        (b"unexpected but successful response", 0, "installed"),
        (b"internal command failure", 2, "error"),
    ],
)
def test_codex_status_mapping(tmp_path, output, returncode, state):
    cli = make_cli(tmp_path, "codex")
    service = ProviderStatusService(
        explicit_paths={"codex": cli},
        runner=RecordingRunner(CommandResult(returncode=returncode, stdout=output)),
    )

    assert service.get_status("codex").state == state


def test_claude_ready_scrubs_identity_and_only_keeps_safe_version(tmp_path):
    cli = make_cli(tmp_path, "claude")
    secret_payload = (
        b'{"loggedIn":true,"email":"person@example.com",'
        b'"orgId":"org-secret","token":"token-secret","version":"1.2.3"}'
    )
    service = ProviderStatusService(
        explicit_paths={"claude": cli},
        runner=RecordingRunner(CommandResult(returncode=0, stdout=secret_payload)),
    )

    result = service.get_status("claude").as_dict()

    assert result == {
        "provider": "claude",
        "state": "ready",
        "installed": True,
        "capabilities": [
            "connection_status",
            "interactive_login",
            "structured_judgment",
            "report_draft",
            "document_edit_plan",
        ],
        "message": "Claude CLI가 연결되었습니다.",
        "version": "1.2.3",
    }
    assert "person@example.com" not in repr(result)
    assert "org-secret" not in repr(result)
    assert "token-secret" not in repr(result)


def test_claude_not_connected_and_malformed_json(tmp_path):
    cli = make_cli(tmp_path, "claude")
    disconnected = ProviderStatusService(
        explicit_paths={"claude": cli},
        runner=RecordingRunner(
            CommandResult(
                returncode=0,
                stdout=b'{"loggedIn":false,"email":"hidden@example.com"}',
            )
        ),
    )
    malformed = ProviderStatusService(
        explicit_paths={"claude": cli},
        runner=RecordingRunner(CommandResult(returncode=0, stdout=b"{not json")),
    )

    assert disconnected.get_status("claude").state == "not_connected"
    assert malformed.get_status("claude").state == "error"


def test_unsafe_version_is_dropped(tmp_path):
    cli = make_cli(tmp_path, "claude")
    service = ProviderStatusService(
        explicit_paths={"claude": cli},
        runner=RecordingRunner(
            CommandResult(
                returncode=0,
                stdout=b'{"loggedIn":true,"version":"1.2.3 user@example.com"}',
            )
        ),
    )

    assert "version" not in service.get_status("claude").as_dict()


def test_timeout_and_output_overflow_are_safe_errors(tmp_path):
    cli = make_cli(tmp_path, "codex")
    timed_out = ProviderStatusService(
        explicit_paths={"codex": cli},
        runner=RecordingRunner(CommandResult(returncode=-9, timed_out=True)),
    )
    overflow = ProviderStatusService(
        explicit_paths={"codex": cli},
        runner=RecordingRunner(
            CommandResult(
                returncode=0,
                stdout=b"Logged in\nsecret",
                output_truncated=True,
            )
        ),
    )

    assert timed_out.get_status("codex").state == "error"
    assert overflow.get_status("codex").state == "error"
    assert "secret" not in repr(overflow.get_status("codex").as_dict())


def test_runner_os_error_is_scrubbed(tmp_path):
    cli = make_cli(tmp_path, "codex")

    def failing_runner(*args, **kwargs):
        raise OSError(r"C:\secret\path token=abc")

    result = ProviderStatusService(
        explicit_paths={"codex": cli}, runner=failing_runner
    ).get_status("codex")

    assert result.state == "error"
    assert "secret" not in repr(result.as_dict())
    assert "token" not in repr(result.as_dict()).casefold()


def test_discovery_uses_only_allowlisted_names_and_existing_files(tmp_path):
    malicious = tmp_path / "codex-wrapper.exe"
    malicious.write_bytes(b"MZ")
    missing = tmp_path / "claude.exe"
    which_calls = []

    def fake_which(name):
        which_calls.append(name)
        if name == "claude":
            return str(missing)
        raise AssertionError("unexpected discovery name")

    service = ProviderStatusService(
        explicit_paths={"codex": malicious},
        which=fake_which,
        runner=RecordingRunner(CommandResult(returncode=0)),
    )

    assert service.get_status("codex").state == "not_installed"
    assert service.get_status("claude").state == "not_installed"
    assert which_calls == ["claude"]


def test_discovery_via_which_never_searches_aliases(tmp_path):
    cli = make_cli(tmp_path, "codex")
    calls = []

    def fake_which(name):
        calls.append(name)
        return str(cli)

    status = ProviderStatusService(
        which=fake_which,
        runner=RecordingRunner(CommandResult(returncode=0, stdout=b"Logged in")),
    ).get_status("codex")

    assert status.state == "ready"
    assert calls == ["codex"]


def test_native_official_executable_link_resolves_to_allowlisted_target(
    tmp_path,
):
    target_directory = tmp_path / "installed" / "claude"
    target_directory.mkdir(parents=True)
    target = target_directory / "claude.exe"
    target.write_bytes(b"MZ")
    link = tmp_path / "claude.exe"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows host")
    runner = RecordingRunner(
        CommandResult(
            returncode=0,
            stdout=b'{"loggedIn":true,"version":"2.1.201"}',
        )
    )

    status = ProviderStatusService(
        explicit_paths={"claude": link},
        runner=runner,
    ).get_status("claude")

    assert status.state == "ready"
    assert runner.calls[0]["argv"] == (
        str(target.resolve()),
        "auth",
        "status",
        "--json",
    )


def test_official_windows_cmd_wrapper_is_allowlisted(tmp_path):
    cli = tmp_path / "codex.cmd"
    cli.write_text("@echo off\r\n", encoding="ascii")
    package_entry = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex"
        / "bin"
        / "codex.js"
    )
    package_entry.parent.mkdir(parents=True)
    package_entry.write_text("/* fixed package entry */\n", encoding="ascii")
    node = tmp_path / "node.exe"
    node.write_bytes(b"MZ")
    runner = RecordingRunner(
        CommandResult(returncode=0, stderr=b"Logged in using ChatGPT")
    )
    status = ProviderStatusService(
        explicit_paths={"codex": cli},
        which=lambda name: str(node) if name == "node" else None,
        runner=runner,
    ).get_status("codex")

    assert status.state == "ready"
    assert runner.calls[0]["argv"] == (
        str(node.resolve()),
        str(package_entry.resolve()),
        "login",
        "status",
    )
    assert runner.calls[1]["argv"] == (
        str(node.resolve()),
        str(package_entry.resolve()),
        "--version",
    )


def test_cmd_wrapper_without_fixed_package_entry_is_not_executed(tmp_path):
    cli = tmp_path / "claude.cmd"
    cli.write_text("@echo off\r\n", encoding="ascii")

    def forbidden_runner(*args, **kwargs):
        raise AssertionError("an unverified npm shim must not be executed")

    status = ProviderStatusService(
        explicit_paths={"claude": cli},
        which=lambda name: None,
        runner=forbidden_runner,
    ).get_status("claude")

    assert status.state == "not_installed"


@pytest.mark.parametrize(
    ("provider", "output", "expected_state", "authentication", "network"),
    [
        (
            "codex",
            b"401 Unauthorized: failed to refresh token",
            "expired",
            "expired",
            "unknown",
        ),
        (
            "claude",
            b"network is unreachable",
            "network_unavailable",
            "unknown",
            "offline",
        ),
    ],
)
def test_expired_and_network_failures_are_distinct_without_leaking_output(
    tmp_path,
    provider,
    output,
    expected_state,
    authentication,
    network,
):
    cli = make_cli(tmp_path, provider)
    status = ProviderStatusService(
        explicit_paths={provider: cli},
        runner=RecordingRunner(
            CommandResult(
                returncode=1,
                stderr=output + b" person@example.com token=secret",
            )
        ),
    ).get_status(provider)

    assert status.state == expected_state
    assert status.authentication_state == authentication
    assert status.network_state == network
    assert "person@example.com" not in repr(status.as_dict())
    assert "secret" not in repr(status.as_dict())


def test_version_uses_a_second_exact_allowlisted_command(tmp_path):
    cli = make_cli(tmp_path, "codex")
    results = iter(
        [
            CommandResult(returncode=0, stdout=b"Logged in using ChatGPT"),
            CommandResult(returncode=0, stdout=b"codex-cli 0.136.0"),
        ]
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        return next(results)

    status = ProviderStatusService(
        explicit_paths={"codex": cli},
        runner=runner,
    ).get_status("codex")

    assert status.state == "ready"
    assert status.version == "0.136.0"
    assert calls == [
        (str(cli.resolve()), "login", "status"),
        (str(cli.resolve()), "--version"),
    ]


def test_safe_path_is_rebuilt_only_from_validated_directories(tmp_path):
    cli = make_cli(tmp_path, "codex")
    tool_directory = tmp_path / "tools"
    tool_directory.mkdir()
    runner = RecordingRunner(CommandResult(returncode=0, stdout=b"Logged in"))
    service = ProviderStatusService(
        explicit_paths={"codex": cli},
        runner=runner,
        environ={"PATH": r"C:\untrusted", "OPENAI_API_KEY": "secret"},
        safe_path_directories=[tool_directory],
    )

    assert service.get_status("codex").state == "ready"
    assert runner.calls[0]["env"] == {"PATH": str(tool_directory.resolve())}


def test_login_launch_is_exact_and_only_uses_injected_visible_launcher(tmp_path):
    codex = make_cli(tmp_path, "codex")
    claude = make_cli(tmp_path, "claude")
    launched = []

    def launcher(argv):
        launched.append(tuple(argv))
        return True

    service = ProviderStatusService(
        explicit_paths={"codex": codex, "claude": claude},
        launcher=launcher,
    )

    codex_result = service.launch_login("codex")
    claude_result = service.launch_login("claude")

    assert codex_result.launched is True
    assert claude_result.launched is True
    assert launched == [
        (str(codex.resolve()), "login"),
        (str(claude.resolve()), "auth", "login"),
    ]


def test_login_without_launcher_never_starts_a_process(tmp_path, monkeypatch):
    cli = make_cli(tmp_path, "codex")

    def forbidden(*args, **kwargs):
        raise AssertionError("login must never launch directly")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = ProviderStatusService(
        explicit_paths={"codex": cli}, launcher=None
    ).launch_login("codex")

    assert result.launched is False
    assert result.state == "error"


def test_no_logout_surface_and_local_provider_is_coming_soon():
    service = ProviderStatusService(which=lambda name: None)

    local = service.get_status("local")

    assert local.state == "coming_soon"
    assert local.installed is False
    assert not hasattr(service, "logout")
    assert service.launch_login("local").state == "coming_soon"


def test_unknown_provider_is_rejected():
    service = ProviderStatusService(which=lambda name: None)

    with pytest.raises(ValueError, match="unsupported provider"):
        service.get_status("other")
    with pytest.raises(ValueError, match="unsupported provider"):
        service.launch_login("other")
