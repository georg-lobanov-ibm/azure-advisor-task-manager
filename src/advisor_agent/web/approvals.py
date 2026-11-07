"""Approval of Jira write calls in the browser. Fails closed.

``ApprovalBridge.callback`` is what the agent gets as its ``approval_callback``.
It runs on the agent's event loop, shows the call to the person at the browser
and waits for a click. The page and the HTTP route live on the web server's
loop, so the two sides meet in a ``concurrent.futures.Future`` per request:

* the route calls ``Future.set_result(True | False)``;
* anything else that ends the wait (timeout, cancel, shutdown, no page open)
  calls ``Future.cancel()``.

A future can be finished only once, from either thread, so the first outcome
wins and later ones (a second tab, a double click, a late timeout) are refused.

The callback returns ``True`` only if the person approved *and* the record of
that was written to the audit log. Every other path, including any exception,
returns ``False`` or raises, and the call does not run.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..mcp_tools import ApprovalRequest
from .events import EventHub
from .settings import Settings
from .storage import Storage

log = logging.getLogger("advisor_agent.web")

AUDIT_ACTION = "jira_tool_approval"
# A call with larger arguments cannot be reviewed on a screen: it is denied.
MAX_ARGUMENT_BYTES = 64 * 1024
# How much of the arguments the audit record keeps (the hash covers all of it).
AUDIT_ARGUMENT_CHARS = 4096
_POLL_SECONDS = 0.5
_REMEMBERED_OUTCOMES = 50

APPROVED = "approved"
DENIED = "denied"


@dataclass
class Pending:
    id: str
    server: str
    tool: str
    arguments: Any
    expires_at: float  # time.monotonic()
    future: concurrent.futures.Future[bool] = field(default_factory=concurrent.futures.Future)
    _reason: str | None = None  # why it was closed without a decision
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def close(self, reason: str) -> bool:
        """End the wait without a decision. False if it had already ended (the first end wins)."""
        with self._lock:
            # Future.cancel() reports True for a future that is already cancelled,
            # so check first: the reason that ended the wait must not be replaced.
            if self.future.done():
                return False
            if self.future.cancel():
                self._reason = reason
                return True
            return False

    @property
    def reason(self) -> str | None:
        with self._lock:  # waits for a close() that is still setting it
            return self._reason

    def outcome(self) -> str:
        """How the wait ended: ``approved``, ``denied`` or the reason it was closed."""
        with self._lock:
            if self.future.cancelled():
                return self._reason or "cancelled"
            if self.future.done():
                return APPROVED if self.future.result() is True else DENIED
            return "pending"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "server": self.server,
            "tool": self.tool,
            "arguments": self.arguments,
            "expires_in": max(0.0, round(self.expires_at - time.monotonic(), 1)),
        }


def _canonical(arguments: Any) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class ApprovalBridge:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        hub: EventHub,
        ui_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._settings = settings
        self._storage = storage
        self._hub = hub
        self._ui_loop = ui_loop
        # UI-loop state.
        self._pending: dict[str, Pending] = {}
        self._outcomes: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._shutdown = False

    # -- UI loop side ------------------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        return [pending.public() for pending in self._pending.values()]

    def decide(self, approval_id: str, approve: bool) -> tuple[str, str | None]:
        """Record the person's decision. Returns ``(result, outcome)``.

        ``result`` is ``ok``, ``unknown`` (never seen) or ``conflict`` (already
        ended); for a conflict ``outcome`` says how.
        """
        pending = self._pending.get(approval_id)
        if pending is None:
            outcome = self._outcomes.get(approval_id)
            return ("conflict", outcome) if outcome is not None else ("unknown", None)
        try:
            pending.future.set_result(approve)
        except concurrent.futures.InvalidStateError:
            return "conflict", pending.outcome()
        return "ok", None

    def begin_shutdown(self) -> None:
        """Refuse new requests and deny the ones waiting."""
        self._shutdown = True
        for pending in list(self._pending.values()):
            pending.close("shutdown")

    def _register(self, pending: Pending) -> None:
        if self._shutdown:
            pending.close("shutdown")
            return
        self._pending[pending.id] = pending
        self._hub.publish("approval_requested", pending.public())

    def _unregister(self, approval_id: str, outcome: str) -> None:
        self._outcomes[approval_id] = outcome
        while len(self._outcomes) > _REMEMBERED_OUTCOMES:
            self._outcomes.popitem(last=False)
        if self._pending.pop(approval_id, None) is not None:
            self._hub.publish("approval_resolved", {"id": approval_id, "outcome": outcome})

    # -- agent loop side ---------------------------------------------------------------

    async def callback(self, request: ApprovalRequest) -> bool:
        """The agent's ``approval_callback``. See the module docstring."""
        approval_id = secrets.token_urlsafe(16)
        pending: Pending | None = None
        arguments_text = ""
        outcome = "error"
        cancelled = False
        try:
            arguments_text = _canonical(request.arguments)
            if len(arguments_text.encode()) > MAX_ARGUMENT_BYTES:
                outcome = "too_large"
            elif self._shutdown:
                outcome = "shutdown"
            else:
                pending = Pending(
                    id=approval_id,
                    server=request.server,
                    tool=request.tool,
                    arguments=request.arguments,
                    expires_at=time.monotonic() + self._settings.approval_timeout,
                )
                outcome = await self._ask(pending)
        except asyncio.CancelledError:
            outcome, cancelled = "cancelled", True
        except Exception:
            log.exception("The approval request failed; denying the call.")
            outcome = "error"
        finally:
            if pending is not None:
                pending.close("cancelled")  # nobody can approve it any more
                self._schedule(self._unregister, approval_id, outcome)

        recorded = await self._audit(approval_id, request, arguments_text, outcome)
        if cancelled:
            raise asyncio.CancelledError
        return outcome == APPROVED and recorded

    def _schedule(self, fn: Any, *args: Any) -> None:
        try:
            self._ui_loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass  # the web server's loop has stopped: there is no page to tell

    async def _ask(self, pending: Pending) -> str:
        """Show the request and wait. Returns the outcome name."""
        try:
            self._ui_loop.call_soon_threadsafe(self._register, pending)
        except RuntimeError:
            return "shutdown"  # the web server's loop has stopped
        waiter = asyncio.wrap_future(pending.future)
        try:
            while True:
                remaining = pending.expires_at - time.monotonic()
                if remaining <= 0:
                    pending.close("timeout")
                    break
                done, _ = await asyncio.wait({waiter}, timeout=min(_POLL_SECONDS, remaining))
                if done:
                    break
                if self._shutdown:
                    pending.close("shutdown")
                    break
                if self._hub.seconds_without_viewer() > self._settings.approval_viewer_grace:
                    pending.close("no_viewer")
                    break
        finally:
            # No-op if it already ended. If this task was cancelled, it makes a
            # decision that arrives later fail instead of approving a call that
            # is no longer waiting.
            pending.close("cancelled")
        # The future is the single source of truth, so a click that lands in
        # the same instant as a timeout is honoured exactly once.
        if pending.future.cancelled():
            return pending.reason or "cancelled"
        return APPROVED if pending.future.result() is True else DENIED

    async def _audit(
        self, approval_id: str, request: ApprovalRequest, arguments_text: str, outcome: str
    ) -> bool:
        """Write the record of this request. False if it could not be written."""
        tool = f"{request.server}:{request.tool}"
        try:
            await self._storage.run(
                self._storage.db.log_audit,
                action=AUDIT_ACTION,
                decision=outcome,
                rationale=tool,
                user_approval={APPROVED: True, DENIED: False}.get(outcome),
                metadata={
                    "request_id": approval_id,
                    "tool": tool,
                    "outcome": outcome,
                    "args_sha256": hashlib.sha256(arguments_text.encode()).hexdigest()
                    if arguments_text
                    else None,
                    "arguments": arguments_text[:AUDIT_ARGUMENT_CHARS],
                    "arguments_truncated": len(arguments_text) > AUDIT_ARGUMENT_CHARS,
                },
            )
        except Exception:
            log.exception("Could not write the approval audit record; the call is denied.")
            return False
        return True
