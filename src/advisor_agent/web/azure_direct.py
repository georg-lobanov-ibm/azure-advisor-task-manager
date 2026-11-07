"""The web server's own connection to the Azure Advisor MCP server.

The table of recommendations is filled by calling the server's tools directly,
with no model in between. ``AzureDirect`` owns that connection: it starts the
server in the background (a first start can take a minute while ``uvx``
downloads the package), reports its state, retries on failure, and bounds every
call in time and number.

It runs on the web server's event loop. The agent used for chat has a
connection of its own, on its own loop (two server processes in all).

One *owner task* opens the connection and closes it again. The MCP transport
and session use task groups, which must be left by the task that entered them,
so nothing else may open or close the connection.

States: ``starting`` (no calls yet), ``ready``, ``failed`` (with a reason that
is safe to show and whether a retry happens by itself or needs a click).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack
from typing import Any, Callable, Mapping

import anyio
from mcp import MCPError
from mcp_types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from ..azure_mcp import (
    AZURE_ADVISOR_LIST_COMMAND,
    AZURE_ADVISOR_ROUTER_TOOL,
    AZURE_SUBSCRIPTION_LIST_TOOL,
)
from ..mcp_tools import McpServerConfig, McpServerStartError, open_mcp_session
from . import recommendations

log = logging.getLogger("advisor_agent.web")

STARTING = "starting"
READY = "ready"
FAILED = "failed"

DEFAULT_BACKOFF_SECONDS = (5.0, 15.0, 60.0)
DEFAULT_CACHE_SECONDS = 30.0
DEFAULT_CALL_TIMEOUT_SECONDS = 40.0
DEFAULT_OUTER_TIMEOUT_SECONDS = 45.0
DEFAULT_MAX_CONCURRENT_CALLS = 3
# Two calls in a row that time out mean the server is stuck: restart it.
TIMEOUTS_BEFORE_RECYCLE = 2
_MAX_CACHE_ENTRIES = 64
_MAX_RESULT_CHARS = 16_000_000
_MAX_REASON_CHARS = 300

_CONNECTION_LOST = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)

StatusCallback = Callable[[dict[str, Any]], None]


class AzureUnavailable(RuntimeError):
    """A request could not be answered. The message is safe to show in the browser."""


class _ManualRetry(Exception):
    """Startup failed in a way that waiting will not fix (missing command or tool)."""


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_REASON_CHARS else text[:_MAX_REASON_CHARS] + "..."


def _describe(exc: BaseException) -> str:
    """A short reason for the browser. Full detail goes to the log, not the page."""
    if isinstance(exc, (McpServerStartError, _ManualRetry)):
        return _clip(str(exc))
    if isinstance(exc, BaseExceptionGroup):
        inner = exc.exceptions[0] if exc.exceptions else exc
        return _describe(inner)
    return _clip(f"{type(exc).__name__}: {exc}")


class AzureDirect:
    def __init__(
        self,
        config: McpServerConfig,
        *,
        on_status: StatusCallback | None = None,
        startup_timeout: float | None = None,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        outer_timeout: float = DEFAULT_OUTER_TIMEOUT_SECONDS,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        backoff: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
        max_concurrent_calls: int = DEFAULT_MAX_CONCURRENT_CALLS,
    ) -> None:
        self._config = config
        self._on_status = on_status
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout
        self._outer_timeout = outer_timeout
        self._cache_seconds = cache_seconds
        self._backoff = backoff or (5.0,)
        self._semaphore = asyncio.Semaphore(max_concurrent_calls)

        self._status: dict[str, Any] = {"state": STARTING, "reason": None, "retry": None}
        self._session: Any = None
        self._tool_names: frozenset[str] = frozenset()
        self._owner: asyncio.Task[None] | None = None
        self._stopping = False
        self._leave = asyncio.Event()  # set to close the connection (stop or recycle)
        self._retry = asyncio.Event()  # set by retry()
        self._consecutive_timeouts = 0
        self._generation = 0  # counts connections, so a stale failure is not blamed on a new one
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._subscriptions: list[dict[str, str]] | None = None

    # -- state -----------------------------------------------------------------

    @property
    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def _set_status(self, state: str, reason: str | None = None, retry: str | None = None) -> None:
        new = {"state": state, "reason": reason, "retry": retry}
        if new == self._status:
            return
        self._status = new
        if self._on_status is not None:
            try:
                self._on_status(dict(new))
            except Exception:
                log.exception("status callback failed")

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Begin connecting in the background and return at once."""
        if self._owner is None:
            self._owner = asyncio.create_task(self._own_connection(), name="azure-direct-owner")

    def retry(self) -> bool:
        """Retry now if the connection has failed. False if there is nothing to retry."""
        if self._status["state"] != FAILED or self._stopping:
            return False
        self._retry.set()
        return True

    async def aclose(self, timeout: float = 15.0) -> None:
        """Stop the owner (which closes the server process) and any calls in flight."""
        self._stopping = True
        self._leave.set()
        self._retry.set()
        for task in list(self._inflight.values()):
            task.cancel()
        owner = self._owner
        if owner is None:
            return
        cancelled = False
        if self._status["state"] == STARTING:
            # Still connecting (for example uvx is downloading): the owner is inside
            # the startup wait and would not see the flags until that ends, which can
            # take a minute. Cancelling it unwinds the server process.
            owner.cancel()
            cancelled = True
        try:
            await asyncio.wait_for(asyncio.shield(owner), timeout)
        except asyncio.TimeoutError:
            log.warning("The Azure MCP connection did not close within %.0fs.", timeout)
            if not cancelled:
                # Not a second time: a cancel that lands while the process is being
                # terminated would cut that short and leave the process behind.
                owner.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(owner), 10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            self._session = None

    async def _own_connection(self) -> None:
        failures = 0
        while not self._stopping:
            self._set_status(STARTING)
            self._leave.clear()
            self._retry.clear()
            manual = False
            try:
                async with AsyncExitStack() as stack:
                    session, tools = await open_mcp_session(
                        stack,
                        self._config,
                        **({} if self._startup_timeout is None else {"startup_timeout": self._startup_timeout}),
                    )
                    names = frozenset(tool.name for tool in tools)
                    if AZURE_ADVISOR_ROUTER_TOOL not in names:
                        raise _ManualRetry(
                            f"The Azure MCP server has no '{AZURE_ADVISOR_ROUTER_TOOL}' tool "
                            "(its tool list changed); the server version may need checking."
                        )
                    self._session = session
                    self._tool_names = names
                    self._consecutive_timeouts = 0
                    self._generation += 1
                    failures = 0
                    self._set_status(READY)
                    await self._leave.wait()
                    self._session = None
                    if not self._stopping:
                        log.warning("Restarting the Azure MCP connection.")
                continue  # recycled: start again at once
            except asyncio.CancelledError:
                raise
            except _ManualRetry as exc:
                reason, manual = _describe(exc), True
            except McpServerStartError as exc:
                reason = _describe(exc)
                manual = isinstance(exc.__cause__, OSError)  # command not found
                log.warning("Azure MCP server did not start: %s", exc)
            except Exception as exc:
                reason = _describe(exc)
                log.exception("Azure MCP connection failed")
            finally:
                self._session = None

            if self._stopping:
                break
            self._set_status(FAILED, reason, "manual" if manual else "auto")
            if manual:
                await self._retry.wait()
            else:
                delay = self._backoff[min(failures, len(self._backoff) - 1)]
                failures += 1
                try:
                    await asyncio.wait_for(self._retry.wait(), delay)
                except asyncio.TimeoutError:
                    pass

    def _recycle(self, why: str) -> None:
        if self._status["state"] == READY and not self._stopping:
            log.warning("Recycling the Azure MCP connection: %s", why)
            self._leave.set()

    # -- calls -------------------------------------------------------------------

    async def _call(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        """Call one tool and return its decoded JSON. Raises ``AzureUnavailable``."""
        self._require_ready()
        async with self._semaphore:
            self._require_ready()  # the state may have changed while queued
            session, generation = self._session, self._generation
            try:
                async with asyncio.timeout(self._outer_timeout):
                    result = await session.call_tool(
                        tool, dict(arguments), read_timeout_seconds=self._call_timeout
                    )
            except TimeoutError:
                self._note_timeout(generation)
                raise AzureUnavailable("The Azure MCP server did not answer in time.") from None
            except MCPError as exc:
                if exc.code == REQUEST_TIMEOUT:
                    self._note_timeout(generation)
                    raise AzureUnavailable("The Azure MCP server did not answer in time.") from None
                if exc.code == CONNECTION_CLOSED:
                    self._lost(generation)
                    raise AzureUnavailable("The connection to the Azure MCP server was lost.") from None
                log.warning("Azure MCP call %s failed: %s", tool, exc)
                raise AzureUnavailable(f"The Azure MCP server returned an error: {_clip(exc.message)}") from None
            except _CONNECTION_LOST:
                self._lost(generation)
                raise AzureUnavailable("The connection to the Azure MCP server was lost.") from None
        if generation == self._generation:
            self._consecutive_timeouts = 0
        return self._decode(tool, result)

    def _require_ready(self) -> None:
        if self._status["state"] != READY or self._session is None:
            state = self._status["state"]
            if state == FAILED:
                raise AzureUnavailable(f"The Azure MCP server is not available: {self._status['reason']}")
            raise AzureUnavailable("The Azure MCP server is still starting. Try again in a moment.")

    def _note_timeout(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._consecutive_timeouts += 1
        if self._consecutive_timeouts >= TIMEOUTS_BEFORE_RECYCLE:
            self._recycle(f"{self._consecutive_timeouts} calls in a row timed out")

    def _lost(self, generation: int) -> None:
        if generation == self._generation:
            self._recycle("the connection was lost")

    def _decode(self, tool: str, result: Any) -> Any:
        text = "".join(
            block.text for block in getattr(result, "content", []) if getattr(block, "type", "") == "text"
        )
        if getattr(result, "is_error", False):
            log.warning("Azure MCP tool %s returned an error: %s", tool, text[:500])
            raise AzureUnavailable(f"The Azure MCP server returned an error: {_clip(text) or 'no details'}")
        if len(text) > _MAX_RESULT_CHARS:
            raise AzureUnavailable("The Azure MCP server's answer was too large to show.")
        try:
            payload = json.loads(text)
        except ValueError:
            log.warning("Azure MCP tool %s returned text that is not JSON: %s", tool, text[:200])
            raise AzureUnavailable("The Azure MCP server returned an unexpected answer.") from None
        status = payload.get("status") if isinstance(payload, dict) else None
        if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
            message = payload.get("message") if isinstance(payload.get("message"), str) else ""
            raise AzureUnavailable(f"Azure returned status {status}: {_clip(message) or 'no details'}")
        return payload

    # -- what the UI asks for ---------------------------------------------------------

    async def list_recommendations(
        self, params: Mapping[str, str], *, refresh: bool = False
    ) -> dict[str, Any]:
        """``{"rows", "truncated", "fetched_at"}`` for the given filters.

        Raises ``ValueError`` for a bad filter value and ``AzureUnavailable``
        when Azure cannot be asked. Equal requests are cached for a short time
        and share one call while it is running.
        """
        arguments = recommendations.list_arguments(params)
        key = json.dumps(arguments, sort_keys=True)

        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and not refresh and now - cached[0] < self._cache_seconds:
            return cached[1]

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch(key, arguments))
            self._inflight[key] = task
            task.add_done_callback(lambda done, key=key: self._finish(key, done))
        # Shielded: one caller going away must not cancel the call for the others.
        return await asyncio.shield(task)

    def _finish(self, key: str, task: asyncio.Task[dict[str, Any]]) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()  # mark it retrieved; the callers see it through shield()

    async def _fetch(self, key: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = await self._call(
            AZURE_ADVISOR_ROUTER_TOOL,
            {
                "intent": "list recommendations",
                "command": AZURE_ADVISOR_LIST_COMMAND,
                "parameters": arguments,
            },
        )
        try:
            rows, truncated = recommendations.recommendations_from_payload(payload)
        except ValueError as exc:
            raise AzureUnavailable(str(exc)) from None
        entry = {
            "rows": rows,
            "truncated": truncated,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if len(self._cache) >= _MAX_CACHE_ENTRIES:
            self._cache.clear()
        self._cache[key] = (time.monotonic(), entry)
        return entry

    async def list_subscriptions(self) -> list[dict[str, str]]:
        """The subscriptions the server can see; ``[]`` when it offers no such tool."""
        if self._subscriptions is not None:
            return self._subscriptions
        # Before the first connection the tool list is empty, which must not read as
        # "the server offers no such tool".
        self._require_ready()
        if AZURE_SUBSCRIPTION_LIST_TOOL not in self._tool_names:
            return []
        payload = await self._call(AZURE_SUBSCRIPTION_LIST_TOOL, {})
        self._subscriptions = recommendations.subscriptions_from_payload(payload)
        return self._subscriptions
