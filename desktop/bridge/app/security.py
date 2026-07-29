import hmac
import ipaddress
from typing import Awaitable, Callable, Iterable, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse

MAX_REQUEST_BYTES = 1 * 1024 * 1024
SAFE_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class BridgeTokenAuth:
    def __init__(self, expected_token: str) -> None:
        if not isinstance(expected_token, str) or len(expected_token) < 43:
            raise ValueError("bridge token must contain at least 256 bits of entropy")
        self._expected_token = expected_token
        self._scheme = HTTPBearer(auto_error=False)

    async def __call__(self, request: Request) -> None:
        credentials: Optional[HTTPAuthorizationCredentials] = await self._scheme(
            request
        )
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(
                credentials.credentials.encode("utf-8"),
                self._expected_token.encode("utf-8"),
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bridge authentication failed",
                headers={"WWW-Authenticate": "Bearer"},
            )


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                await self._reject(scope, receive, send)
                return

        consumed = 0
        exceeded = False

        async def limited_receive():
            nonlocal consumed, exceeded
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    exceeded = True
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
            return message

        response_started = False

        async def guarded_send(message):
            nonlocal response_started
            if exceeded and not response_started:
                response_started = True
                response = JSONResponse(
                    status_code=413, content={"detail": "Request body is too large"}
                )
                await response(scope, receive, send)
                return
            if exceeded:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        await self.app(scope, limited_receive, guarded_send)

    async def _reject(self, scope, receive, send) -> None:
        response = JSONResponse(
            status_code=413, content={"detail": "Request body is too large"}
        )
        await response(scope, receive, send)


class LoopbackOnlyMiddleware:
    def __init__(self, app, allowed_test_clients: Iterable[str] = ()) -> None:
        self.app = app
        self.allowed_test_clients = frozenset(allowed_test_clients)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            client = scope.get("client")
            client_host = client[0] if client else ""
            if client_host not in self.allowed_test_clients:
                try:
                    if not ipaddress.ip_address(client_host).is_loopback:
                        raise ValueError
                except ValueError:
                    response = JSONResponse(
                        status_code=403,
                        content={"detail": "Bridge accepts loopback clients only"},
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        async def add_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SAFE_RESPONSE_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, add_headers)


def validate_public_source_url(source_type: str, source_url: str) -> str:
    parsed = urlsplit(source_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS video sources are supported")
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("Source URL credentials and missing hosts are not allowed")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Source URL uses an unsupported port")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(
        ".local"
    ):
        raise ValueError("Local network source URLs are not allowed")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError("Private or special-use source addresses are not allowed")

    allowed_hosts = {
        "google_drive": ("drive.google.com", "docs.google.com"),
        "dropbox": (
            "dropbox.com",
            "www.dropbox.com",
            "dl.dropboxusercontent.com",
        ),
        "onedrive": ("1drv.ms", "onedrive.live.com", ".sharepoint.com"),
        "youtube": ("youtu.be", "youtube.com", "www.youtube.com"),
    }
    if source_type in allowed_hosts:
        choices = allowed_hosts[source_type]
        if not any(
            hostname == choice or (choice.startswith(".") and hostname.endswith(choice))
            for choice in choices
        ):
            raise ValueError(f"URL host does not match the {source_type} source type")
    return hostname
