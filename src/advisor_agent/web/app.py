"""The web UI's Starlette app: what it is made of, its routes, and how it shuts down.

Everything the routes need lives in one ``Services`` object (``app.state.services``).
The pieces that need the running event loop (the approval bridge, the chat, the
Azure connection) are built when the app starts, not when it is created.

Routes only translate between HTTP and those pieces. What a request must prove
before it gets here is in ``security.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ..database import AdvisorDatabase
from ..mcp_tools import McpServerConfig
from .approvals import ApprovalBridge
from .azure_direct import AzureDirect, AzureUnavailable
from .chat import AgentFactory, ChatSession
from .errors import ApiError, clean_line
from .events import EventHub, Subscription
from .recommendations import GUID, link_key
from .security import MAX_BODY_BYTES, SecurityMiddleware, token_matches
from .settings import (
    JIRA_KEY_MAX_LENGTH,
    SESSION_COOKIE,
    Settings,
    valid_jira_key,
)
from .storage import Storage

log = logging.getLogger("advisor_agent.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"
# The only files served under /static, with the type each is served as.
STATIC_FILES = {
    "app.js": "text/javascript",
    "app.css": "text/css",
}

_APPROVAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

LINK_AUDIT_LINK = "manual_link"
LINK_AUDIT_UNLINK = "manual_unlink"


# -- the pieces, and their life ---------------------------------------------------------


class Services:
    """The parts of one web server run, and the order they start and stop in."""

    def __init__(
        self,
        settings: Settings,
        db: AdvisorDatabase,
        agent_factory: AgentFactory,
        azure_config: McpServerConfig,
        azure_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.storage = Storage(db)
        self.hub = EventHub()
        self._agent_factory = agent_factory
        self._azure_config = azure_config
        self._azure_options = dict(azure_options or {})

        self.loop: asyncio.AbstractEventLoop | None = None
        self.approvals: ApprovalBridge
        self.chat: ChatSession
        self.azure: AzureDirect
        # No lock: begin_shutdown() runs in a signal handler, possibly on the
        # thread that holds the lock. Each side stores its own value and then
        # reads the other's, so at least one of them sees both.
        self._shutdown_requested = False

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.loop = loop
        self.approvals = ApprovalBridge(self.settings, self.storage, self.hub, loop)
        self.chat = ChatSession(
            self.settings, self.hub, loop, self._agent_factory, self.approvals.callback
        )
        self.azure = AzureDirect(
            self._azure_config,
            on_status=lambda status: self.hub.publish("azure_status", status),
            **self._azure_options,
        )
        self.azure.start()  # returns at once; the server comes up in the background
        if self._shutdown_requested:
            self._begin_shutdown_on_loop()

    def snapshot(self) -> dict[str, Any]:
        """Everything a page shows, for the first message of an event stream."""
        return {
            "chat": self.chat.snapshot(),
            "approvals": self.approvals.snapshot(),
            "azure": self.azure.status,
            "jira_base_url": self.settings.jira_base_url,
        }

    def begin_shutdown(self) -> None:
        """Stop taking new work and end the long-lived requests. Safe from any thread.

        uvicorn waits for open requests before it runs the lifespan shutdown,
        and an event stream never ends by itself, so the streams are closed
        here, first. Pending approvals are denied and the chat run is cancelled
        so that nothing is left waiting for a person who is no longer there.
        """
        self._shutdown_requested = True
        loop = self.loop
        if loop is None:
            return  # not started: start() will see the flag
        try:
            loop.call_soon_threadsafe(self._begin_shutdown_on_loop)
        except RuntimeError:
            pass  # the loop has already stopped

    def _begin_shutdown_on_loop(self) -> None:
        self.approvals.begin_shutdown()
        self.hub.close_all()
        self.chat.begin_shutdown()

    async def stop(self) -> None:
        self._begin_shutdown_on_loop()  # idempotent
        # Independent of each other and each bounded in time, so run together.
        await asyncio.gather(self.chat.close(), self.azure.aclose(), return_exceptions=True)
        self.storage.close()


# -- request helpers --------------------------------------------------------------------


def _services(request: Request) -> Services:
    return request.app.state.services


async def read_json(request: Request) -> dict[str, Any]:
    """The request body as a JSON object, read with a size limit."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise ApiError(413, "The request is too large.")
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks) or b"null")
    except (ValueError, RecursionError):
        raise ApiError(400, "The request body is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ApiError(400, "Expected a JSON object.")
    return data


def _text_field(body: Mapping[str, Any], name: str, *, limit: int, required: bool = False) -> str:
    value = body.get(name)
    if value is None or value == "":
        value = ""
    elif not isinstance(value, str):
        raise ApiError(400, f"'{name}' must be text.")
    value = value.strip()
    if required and not value:
        raise ApiError(400, f"'{name}' is required.")
    if len(value) > limit or _CONTROL.search(value):
        raise ApiError(400, f"'{name}' is invalid.")
    return value


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


async def _on_api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return _json({"error": exc.message, **exc.extra}, exc.status)


async def _on_azure_unavailable(request: Request, exc: Exception) -> Response:
    # The message was built by AzureDirect from short, clipped pieces.
    return _json({"error": clean_line(str(exc))}, 503)


# -- pages ------------------------------------------------------------------------------


async def index(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


async def static_file(request: Request) -> Response:
    name = request.path_params["name"]
    media_type = STATIC_FILES.get(name)
    if media_type is None:
        raise ApiError(404, "Not found.")
    return FileResponse(STATIC_DIR / name, media_type=media_type)


# -- sign-in and the event stream -------------------------------------------------------


async def login(request: Request) -> Response:
    services = _services(request)
    body = await read_json(request)
    if not token_matches(body.get("token"), services.settings.token):
        raise ApiError(401, "That token is not valid. Open the address the server printed.")
    response = _json({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        services.settings.session_secret,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


async def session_check(request: Request) -> Response:
    """200 when the request carries a valid session cookie (the middleware checked it)."""
    return _json({"ok": True})


class SseResponse(StreamingResponse):
    """An event stream that always lets go of its subscription and its generator."""

    def __init__(self, hub: EventHub, subscription: Subscription) -> None:
        super().__init__(
            hub.stream(subscription),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )
        self._hub = hub
        self._subscription = subscription

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._hub.unsubscribe(self._subscription)
            iterator: AsyncIterator[Any] = self.body_iterator
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()


async def events(request: Request) -> Response:
    services = _services(request)
    # Built and subscribed in the same step (no await between), so no event is
    # missed or shown twice.
    subscription = services.hub.subscribe(services.snapshot())
    if subscription is None:
        raise ApiError(503, "The server is shutting down.")
    return SseResponse(services.hub, subscription)


# -- Azure ------------------------------------------------------------------------------


async def subscriptions(request: Request) -> Response:
    services = _services(request)
    return _json({"subscriptions": await services.azure.list_subscriptions()})


def public_link(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "recommendation_type_id": mapping.get("recommendation_type_id"),
        "resource_id": mapping.get("resource_id"),
        "jira_key": mapping.get("jira_issue_key"),
        "status": mapping.get("status"),
        "created_at": mapping.get("created_at"),
        "category": mapping.get("category"),
        "impact": mapping.get("impact"),
    }


async def recommendations(request: Request) -> Response:
    services = _services(request)
    params = dict(request.query_params)
    refresh = params.get("refresh") in ("1", "true")
    try:
        result = await services.azure.list_recommendations(params, refresh=refresh)
    except ValueError as exc:
        raise ApiError(400, clean_line(str(exc)) or "Invalid filter.") from None
    mappings = await services.storage.run(services.storage.db.get_all_mappings)
    links = {
        link_key(mapping["recommendation_type_id"], mapping["resource_id"]): public_link(mapping)
        for mapping in mappings
    }
    rows = [
        {
            **row,
            "link": links.get(link_key(row["recommendation_type_id"], row["resource_id"]))
            if row["linkable"]
            else None,
        }
        for row in result["rows"]
    ]
    return _json(
        {"rows": rows, "truncated": result["truncated"], "fetched_at": result["fetched_at"]}
    )


async def azure_retry(request: Request) -> Response:
    await read_json(request)
    return _json({"retrying": _services(request).azure.retry()})


# -- Jira links -------------------------------------------------------------------------


def _create_link(
    db: AdvisorDatabase,
    type_id: str,
    resource_id: str,
    jira_key: str,
    advisor_id: str | None,
    category: str | None,
    impact: str | None,
    subscription_id: str | None,
) -> dict[str, Any]:
    """Runs on the database thread."""
    existing = db.get_mapping(type_id, resource_id)
    if existing is not None:
        raise ApiError(
            409, f"This finding is already linked to {existing['jira_issue_key']}. Unlink it first."
        )
    if db.get_mapping_by_jira_key(jira_key) is not None:
        raise ApiError(409, f"{jira_key} is already linked to another finding.")
    try:
        mapping = db.create_mapping(
            type_id,
            resource_id,
            jira_key,
            advisor_id=advisor_id,
            category=category,
            impact=impact,
            subscription_id=subscription_id,
        )
    except sqlite3.IntegrityError:
        raise ApiError(409, "This finding or this Jira issue is already linked.") from None
    try:
        db.log_audit(
            LINK_AUDIT_LINK,
            advisor_id=advisor_id,
            jira_key=jira_key,
            decision="link",
            rationale="Linked by hand in the web UI",
            metadata={"source": "web_ui"},
            recommendation_type_id=type_id,
            resource_id=resource_id,
        )
    except Exception:
        # A link without its record is not kept.
        db.delete_mapping(type_id, resource_id)
        raise
    return mapping


def _remove_link(db: AdvisorDatabase, type_id: str, resource_id: str) -> dict[str, Any]:
    """Runs on the database thread."""
    existing = db.get_mapping(type_id, resource_id)
    if existing is None:
        raise ApiError(404, "This finding is not linked.")
    db.delete_mapping(type_id, resource_id)
    try:
        db.log_audit(
            LINK_AUDIT_UNLINK,
            advisor_id=existing.get("advisor_id"),
            jira_key=existing["jira_issue_key"],
            decision="unlink",
            rationale="Unlinked by hand in the web UI",
            metadata={"source": "web_ui"},
            recommendation_type_id=type_id,
            resource_id=resource_id,
        )
    except Exception:
        log.exception("Could not write the audit record of an unlink.")
        raise
    return existing


async def list_links(request: Request) -> Response:
    storage = _services(request).storage
    mappings = await storage.run(storage.db.get_all_mappings)
    return _json({"links": [public_link(mapping) for mapping in mappings]})


async def put_link(request: Request) -> Response:
    services = _services(request)
    body = await read_json(request)
    type_id = _text_field(body, "recommendation_type_id", limit=200, required=True)
    resource_id = _text_field(body, "resource_id", limit=1024, required=True)
    jira_key = _text_field(body, "jira_key", limit=JIRA_KEY_MAX_LENGTH, required=True).upper()
    if not valid_jira_key(jira_key):
        raise ApiError(400, "That is not a Jira issue key (expected something like ABC-123).")
    category = _text_field(body, "category", limit=100) or None
    impact = _text_field(body, "impact", limit=100) or None
    advisor_id = _text_field(body, "advisor_id", limit=200) or None
    subscription_id = _text_field(body, "subscription_id", limit=36) or None
    if subscription_id is not None and not GUID.fullmatch(subscription_id):
        raise ApiError(400, "'subscription_id' is invalid.")

    mapping = await services.storage.run(
        _create_link,
        services.storage.db,
        type_id,
        resource_id,
        jira_key,
        advisor_id,
        category,
        impact,
        subscription_id,
    )
    link = public_link(mapping)
    services.hub.publish("link_changed", {"link": link, "removed": False})
    return _json({"link": link})


async def delete_link(request: Request) -> Response:
    services = _services(request)
    body = await read_json(request)
    type_id = _text_field(body, "recommendation_type_id", limit=200, required=True)
    resource_id = _text_field(body, "resource_id", limit=1024, required=True)
    removed = await services.storage.run(_remove_link, services.storage.db, type_id, resource_id)
    link = public_link(removed)
    services.hub.publish("link_changed", {"link": link, "removed": True})
    return _json({"removed": True, "link": link})


# -- chat and approvals -----------------------------------------------------------------


async def chat_send(request: Request) -> Response:
    body = await read_json(request)
    result = _services(request).chat.start(body.get("text"), body.get("client_msg_id"))
    return _json(result, 202)


async def chat_cancel(request: Request) -> Response:
    await read_json(request)
    return _json({"cancelling": _services(request).chat.cancel()})


async def chat_reset(request: Request) -> Response:
    await read_json(request)
    _services(request).chat.reset()
    return _json({"ok": True})


async def approval_decision(request: Request) -> Response:
    approval_id = request.path_params["approval_id"]
    body = await read_json(request)
    decision = body.get("decision")
    if decision not in ("approve", "deny"):
        raise ApiError(400, "'decision' must be 'approve' or 'deny'.")
    if not _APPROVAL_ID.fullmatch(approval_id):
        raise ApiError(404, "Unknown approval request.")
    result, outcome = _services(request).approvals.decide(approval_id, decision == "approve")
    if result == "unknown":
        raise ApiError(404, "Unknown approval request.")
    if result == "conflict":
        raise ApiError(409, "This request has already been decided or has ended.", outcome=outcome)
    return _json({"ok": True})


# -- the app ----------------------------------------------------------------------------


def create_app(
    settings: Settings,
    *,
    agent_factory: AgentFactory,
    azure_config: McpServerConfig,
    db: AdvisorDatabase,
    azure_options: Mapping[str, Any] | None = None,
) -> Starlette:
    """Build the app. ``agent_factory(approval_callback, progress)`` makes the chat agent.

    ``azure_options`` are extra keyword arguments for ``AzureDirect`` (timeouts
    and backoff), for tests.
    """
    services = Services(settings, db, agent_factory, azure_config, azure_options)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await services.start()
        try:
            yield
        finally:
            await services.stop()

    routes = [
        Route("/", index),
        Route("/static/{name}", static_file),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/session", session_check),
        Route("/api/events", events),
        Route("/api/subscriptions", subscriptions),
        Route("/api/recommendations", recommendations),
        Route("/api/links", list_links),
        Route("/api/links", put_link, methods=["PUT"]),
        Route("/api/links", delete_link, methods=["DELETE"]),
        Route("/api/chat", chat_send, methods=["POST"]),
        Route("/api/chat/cancel", chat_cancel, methods=["POST"]),
        Route("/api/chat/reset", chat_reset, methods=["POST"]),
        Route("/api/approvals/{approval_id}", approval_decision, methods=["POST"]),
        Route("/api/azure/retry", azure_retry, methods=["POST"]),
    ]
    app = Starlette(
        routes=routes,
        middleware=[Middleware(SecurityMiddleware, settings=settings)],
        exception_handlers={
            ApiError: _on_api_error,
            AzureUnavailable: _on_azure_unavailable,
        },
        lifespan=lifespan,
    )
    app.state.services = services
    return app
