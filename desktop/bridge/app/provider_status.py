"""Safe discovery and authentication-state checks for report AI providers.

This module deliberately has no FastAPI or store dependencies.  The bridge
integration can inject the platform's visible-terminal launcher without giving
the status probe permission to run arbitrary commands.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Callable, Mapping, Protocol, Sequence


ProviderName = str

_STATUS_ARGV: dict[str, tuple[str, ...]] = {
    "codex": ("login", "status"),
    "claude": ("auth", "status", "--json"),
}
_LOGIN_ARGV: dict[str, tuple[str, ...]] = {
    "codex": ("--login",),
    "claude": ("auth", "login"),
}
_EXECUTABLE_NAMES: dict[str, frozenset[str]] = {
    "codex": frozenset({"codex", "codex.exe", "codex.cmd"}),
    "claude": frozenset({"claude", "claude.exe", "claude.cmd"}),
}
_READY_CAPABILITIES = (
    "connection_status",
    "interactive_login",
    "report_draft",
    "document_edit_plan",
)
_DISCONNECTED_CAPABILITIES = ("connection_status", "interactive_login")
_SAFE_ENV_NAMES = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "LANG",
    "LC_ALL",
)
_VERSION_RE = re.compile(
    r"^[vV]?\d{1,5}(?:\.\d{1,5}){1,3}(?:[-+][0-9A-Za-z.-]{1,48})?$"
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    output_truncated: bool = False


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    state: str
    installed: bool
    capabilities: tuple[str, ...]
    message: str
    version: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "provider": self.provider,
            "state": self.state,
            "installed": self.installed,
            "capabilities": list(self.capabilities),
            "message": self.message,
        }
        if self.version is not None:
            result["version"] = self.version
        return result


@dataclass(frozen=True)
class LoginLaunchResult:
    provider: str
    launched: bool
    state: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "launched": self.launched,
            "state": self.state,
            "message": self.message,
        }


class StatusRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
        env: Mapping[str, str],
    ) -> CommandResult: ...


class VisibleTerminalLauncher(Protocol):
    def __call__(self, argv: Sequence[str]) -> bool | None: ...


class _CappedCollector:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._parts: list[bytes] = []
        self._stored = 0
        self.truncated = False

    def drain(self, stream: object) -> None:
        read = getattr(stream, "read")
        while True:
            chunk = read(4096)
            if not chunk:
                return
            remaining = self._limit - self._stored
            if remaining > 0:
                kept = chunk[:remaining]
                self._parts.append(kept)
                self._stored += len(kept)
            if len(chunk) > max(remaining, 0):
                self.truncated = True

    @property
    def value(self) -> bytes:
        return b"".join(self._parts)


def run_status_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    output_limit_bytes: int,
    env: Mapping[str, str],
) -> CommandResult:
    """Run one already-validated status command with bounded captured output."""

    if output_limit_bytes < 1024:
        raise ValueError("output_limit_bytes must be at least 1024")

    popen_kwargs: dict[str, object] = {
        "args": list(argv),
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": dict(env),
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        popen_kwargs["startupinfo"] = startupinfo

    process = subprocess.Popen(**popen_kwargs)  # type: ignore[arg-type]
    per_stream_limit = max(512, output_limit_bytes // 2)
    stdout = _CappedCollector(per_stream_limit)
    stderr = _CappedCollector(per_stream_limit)
    stdout_thread = threading.Thread(
        target=stdout.drain, args=(process.stdout,), daemon=True
    )
    stderr_thread = threading.Thread(
        target=stderr.drain, args=(process.stderr,), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        returncode = process.wait(timeout=1.0)
    finally:
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)

    return CommandResult(
        returncode=returncode,
        stdout=stdout.value,
        stderr=stderr.value,
        timed_out=timed_out,
        output_truncated=stdout.truncated or stderr.truncated,
    )


class ProviderStatusService:
    def __init__(
        self,
        *,
        explicit_paths: Mapping[str, str | os.PathLike[str]] | None = None,
        which: Callable[[str], str | None] = shutil.which,
        runner: StatusRunner = run_status_command,
        launcher: VisibleTerminalLauncher | None = None,
        environ: Mapping[str, str] | None = None,
        safe_path_directories: Sequence[str | os.PathLike[str]] = (),
        timeout_seconds: float = 4.0,
        output_limit_bytes: int = 16 * 1024,
    ) -> None:
        if not 0.1 <= timeout_seconds <= 10.0:
            raise ValueError("timeout_seconds must be between 0.1 and 10 seconds")
        if not 1024 <= output_limit_bytes <= 64 * 1024:
            raise ValueError("output_limit_bytes must be between 1 KiB and 64 KiB")
        self._explicit_paths = dict(explicit_paths or {})
        self._which = which
        self._runner = runner
        self._launcher = launcher
        self._environ = dict(os.environ if environ is None else environ)
        self._safe_path_directories = tuple(
            self._validated_path_directory(value)
            for value in safe_path_directories
        )
        self._timeout_seconds = timeout_seconds
        self._output_limit_bytes = output_limit_bytes

    def get_status(self, provider: ProviderName) -> ProviderStatus:
        provider = _normalize_provider(provider)
        if provider == "local":
            return ProviderStatus(
                provider="local",
                state="coming_soon",
                installed=False,
                capabilities=(),
                message="로컬 모델 Provider는 아직 준비 중입니다.",
            )

        executable = self._discover_executable(provider)
        if executable is None:
            return ProviderStatus(
                provider=provider,
                state="not_installed",
                installed=False,
                capabilities=(),
                message=f"{_display_name(provider)} CLI가 설치되어 있지 않습니다.",
            )

        argv = (executable, *_STATUS_ARGV[provider])
        try:
            result = self._runner(
                argv,
                timeout_seconds=self._timeout_seconds,
                output_limit_bytes=self._output_limit_bytes,
                env=self._minimal_environment(),
            )
        except (OSError, subprocess.SubprocessError):
            return self._error_status(
                provider,
                "CLI 상태 확인 명령을 실행하지 못했습니다.",
            )

        if result.timed_out:
            return self._error_status(
                provider,
                "CLI 상태 확인 응답 시간이 초과되었습니다.",
            )
        if result.output_truncated:
            return self._error_status(
                provider,
                "CLI 상태 데이터가 허용 크기를 초과했습니다.",
            )

        if provider == "claude":
            return self._parse_claude_status(result)
        return self._parse_codex_status(result)

    def launch_login(self, provider: ProviderName) -> LoginLaunchResult:
        provider = _normalize_provider(provider)
        if provider == "local":
            return LoginLaunchResult(
                provider="local",
                launched=False,
                state="coming_soon",
                message="로컬 모델 Provider는 아직 준비 중입니다.",
            )

        executable = self._discover_executable(provider)
        if executable is None:
            return LoginLaunchResult(
                provider=provider,
                launched=False,
                state="not_installed",
                message=f"{_display_name(provider)} CLI가 설치되어 있지 않습니다.",
            )
        if self._launcher is None:
            return LoginLaunchResult(
                provider=provider,
                launched=False,
                state="error",
                message="로그인 창을 열 수 있도록 터미널 실행기가 구성되지 않았습니다.",
            )

        argv = (executable, *_LOGIN_ARGV[provider])
        try:
            launched = self._launcher(argv)
        except (OSError, subprocess.SubprocessError):
            launched = False
        if launched is False:
            return LoginLaunchResult(
                provider=provider,
                launched=False,
                state="error",
                message="로그인용 터미널 창을 열지 못했습니다.",
            )
        return LoginLaunchResult(
            provider=provider,
            launched=True,
            state="started",
            message="공식 CLI 로그인 창을 열었습니다.",
        )

    def _discover_executable(self, provider: str) -> str | None:
        raw_path = self._explicit_paths.get(provider)
        if raw_path is None:
            raw_path = self._which(provider)
        if raw_path is None:
            return None

        candidate = Path(raw_path).expanduser()
        if candidate.name.casefold() not in _EXECUTABLE_NAMES[provider]:
            return None
        try:
            if not candidate.is_file():
                return None
            return str(candidate.resolve(strict=True))
        except (OSError, RuntimeError):
            return None

    def _minimal_environment(self) -> dict[str, str]:
        environment = {
            name: self._environ[name]
            for name in _SAFE_ENV_NAMES
            if self._environ.get(name)
        }
        if self._safe_path_directories:
            environment["PATH"] = os.pathsep.join(self._safe_path_directories)
        return environment

    @staticmethod
    def _validated_path_directory(value: str | os.PathLike[str]) -> str:
        candidate = Path(value)
        try:
            if not candidate.is_absolute() or candidate.is_symlink():
                raise ValueError("safe PATH directories must be absolute real directories")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("safe PATH directories must be absolute real directories")
            return str(resolved)
        except OSError as error:
            raise ValueError(
                "safe PATH directories must be absolute real directories"
            ) from error

    def _parse_codex_status(self, result: CommandResult) -> ProviderStatus:
        text = _decode_status_output(result)
        lowered = text.casefold()
        disconnected_markers = (
            "not logged in",
            "not authenticated",
            "login required",
            "signed out",
        )
        ready_markers = (
            "logged in",
            "authenticated",
            "signed in",
        )
        if any(marker in lowered for marker in disconnected_markers):
            return self._not_connected_status("codex")
        if result.returncode == 0 and any(marker in lowered for marker in ready_markers):
            return self._ready_status("codex", _extract_safe_version(text))
        if result.returncode == 0:
            return ProviderStatus(
                provider="codex",
                state="installed",
                installed=True,
                capabilities=_DISCONNECTED_CAPABILITIES,
                message="Codex CLI가 설치되어 있지만 로그인 상태를 확인하지 못했습니다.",
            )
        return self._error_status("codex", "Codex CLI 상태를 확인하지 못했습니다.")

    def _parse_claude_status(self, result: CommandResult) -> ProviderStatus:
        if result.returncode != 0:
            return self._error_status("claude", "Claude CLI 상태를 확인하지 못했습니다.")
        try:
            payload = json.loads(result.stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error_status(
                "claude",
                "Claude CLI 상태 응답 형식이 올바르지 않습니다.",
            )
        if not isinstance(payload, dict):
            return self._error_status(
                "claude",
                "Claude CLI 상태 응답 형식이 올바르지 않습니다.",
            )

        connected = payload.get("loggedIn")
        if not isinstance(connected, bool):
            connected = payload.get("authenticated")
        version = _safe_version(payload.get("version"))
        if connected is True:
            return self._ready_status("claude", version)
        if connected is False:
            return self._not_connected_status("claude", version)
        return ProviderStatus(
            provider="claude",
            state="installed",
            installed=True,
            capabilities=_DISCONNECTED_CAPABILITIES,
            message="Claude CLI가 설치되어 있지만 로그인 상태를 확인하지 못했습니다.",
            version=version,
        )

    @staticmethod
    def _ready_status(provider: str, version: str | None = None) -> ProviderStatus:
        return ProviderStatus(
            provider=provider,
            state="ready",
            installed=True,
            capabilities=_READY_CAPABILITIES,
            message=f"{_display_name(provider)} CLI가 연결되었습니다.",
            version=version,
        )

    @staticmethod
    def _not_connected_status(
        provider: str, version: str | None = None
    ) -> ProviderStatus:
        return ProviderStatus(
            provider=provider,
            state="not_connected",
            installed=True,
            capabilities=_DISCONNECTED_CAPABILITIES,
            message=f"{_display_name(provider)} CLI에 로그인되어 있지 않습니다.",
            version=version,
        )

    @staticmethod
    def _error_status(provider: str, message: str) -> ProviderStatus:
        return ProviderStatus(
            provider=provider,
            state="error",
            installed=True,
            capabilities=_DISCONNECTED_CAPABILITIES,
            message=message,
        )


def _normalize_provider(provider: ProviderName) -> str:
    normalized = provider.strip().casefold()
    if normalized not in {"codex", "claude", "local"}:
        raise ValueError("unsupported provider")
    return normalized


def _display_name(provider: str) -> str:
    return {"codex": "Codex", "claude": "Claude", "local": "Local model"}[provider]


def _decode_status_output(result: CommandResult) -> str:
    return (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")


def _safe_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _VERSION_RE.fullmatch(candidate) else None


def _extract_safe_version(text: str) -> str | None:
    for token in re.split(r"\s+", text):
        version = _safe_version(token.strip("()[],:;"))
        if version is not None:
            return version
    return None


__all__ = [
    "CommandResult",
    "LoginLaunchResult",
    "ProviderStatus",
    "ProviderStatusService",
    "run_status_command",
]
