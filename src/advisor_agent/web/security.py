"""What a request must prove before any handler sees it, and the headers every response carries.

The server holds Azure credentials and can approve Jira writes, and any web page
open in the same browser can send requests to ``127.0.0.1``. So:

* ``Host`` must name this server (this stops DNS rebinding);
* a browser that says the request is cross-site (``Sec-Fetch-Site``) or sends a
  foreign ``Origin`` is refused;
* a request that changes something must be JSON, which a plain HTML form cannot
  send, and must carry our own ``Origin``;
* ``/api/*`` needs the session cookie, which only the launch token earns.

This is a plain ASGI middleware (not ``BaseHTTPMiddleware``) so that streaming
responses (server-sent events) are not buffered.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import safe_error
from .settings import LOOPBACK_HOSTS, SESSION_COOKIE, Settings

log = logging.getLogger("advisor_agent.web")

MAX_BODY_BYTES = 65536
LOGIN_PATH = "/api/login"
API_PREFIX = "/api/"

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "img-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "require-trusted-types-for 'script'",
    ]
)

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
)
_SECURITY_HEADER_NAMES = {name for name, _ in SECURITY_HEADERS}

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SAME_SITE_VALUES = {"same-origin", "none"}


def _refuse(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _is_json(content_type: str | None) -> bool:
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # No WebSocket is served; refuse it rather than pass it on unchecked.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in _SECURITY_HEADER_NAMES
                ]
                headers.extend(SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        refusal = self._check(scope)
        if refusal is not None:
            await refusal(scope, receive, send_with_headers)
            return
        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as exc:
            # Starlette's outer error handler sends its own response without
            # these headers, so answer here, with the headers and a short body.
            log.exception("Request failed: %s %s", scope["method"], scope["path"])
            if started:
                raise  # too late to answer: the connection is closed instead
            body = json.dumps({"error": safe_error(exc)}).encode()
            await send_with_headers(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send_with_headers({"type": "http.response.body", "body": body})

    def _check(self, scope: Scope) -> JSONResponse | None:
        headers = Headers(scope=scope)
        port = self.settings.port

        host = headers.get("host", "")
        if host not in {f"{name}:{port}" for name in LOOPBACK_HOSTS}:
            return _refuse(400, "Invalid host header.")

        path = scope["path"]
        method = scope["method"]
        is_api = path.startswith(API_PREFIX)

        fetch_site = headers.get("sec-fetch-site")
        if fetch_site is not None and fetch_site not in _SAME_SITE_VALUES:
            return _refuse(403, "Cross-site requests are not allowed.")

        origin = headers.get("origin")
        if origin is not None and origin not in self.settings.allowed_origins():
            return _refuse(403, "Foreign origin.")

        if not is_api:
            return None

        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > MAX_BODY_BYTES:
                    return _refuse(413, "The request is too large.")
            except ValueError:
                return _refuse(400, "Invalid content length.")

        if method not in _SAFE_METHODS:
            if origin is None:
                return _refuse(403, "Missing origin.")
            if not _is_json(headers.get("content-type")):
                return _refuse(415, "Content-Type must be application/json.")

        if path != LOGIN_PATH:
            cookie = HTTPConnection(scope).cookies.get(SESSION_COOKIE, "")
            if not cookie or not secrets.compare_digest(
                cookie.encode("utf-8", "replace"), self.settings.session_secret.encode()
            ):
                return _refuse(401, "Not signed in. Open the address printed by the server.")
        return None


def token_matches(supplied: Any, expected: str) -> bool:
    """Compare a supplied launch token with the real one in constant time."""
    if not isinstance(supplied, str) or not supplied or len(supplied) > 256:
        return False
    return secrets.compare_digest(supplied.encode("utf-8", "replace"), expected.encode())
