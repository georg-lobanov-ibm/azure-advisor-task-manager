"""The chat with the agent: one conversation, one run at a time.

``AdvisorAgent.query`` blocks, so each run gets a daemon thread of its own (not
``asyncio.to_thread``, whose non-daemon workers are joined at exit and would
hang the shutdown of the server while a query is stuck). The thread hands its
results back to the web server's loop with ``call_soon_threadsafe``; all state
below is touched on that loop only.

Progress lines are queued by the agent's loop thread *before* ``query`` returns,
and the end of the run is queued after it, so a page always sees the lines of a
run before its answer.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..agent import QueryCancelledError
from .context import attached_context
from .errors import ApiError, clean_line, safe_error
from .events import EventHub
from .settings import Settings

log = logging.getLogger("advisor_agent.web")

MAX_PROMPT_CHARS = 8000
MAX_PROGRESS_LINES = 500
MAX_PROGRESS_CHARS = 500
MAX_TRANSCRIPT_ENTRIES = 200
MAX_ANSWER_CHARS = 200_000
_CANCEL_REPEAT_SECONDS = 1.0
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

# (approval_callback, progress) -> an AdvisorAgent (or anything shaped like one)
AgentFactory = Callable[[Any, Callable[[str], None]], Any]


@dataclass
class Run:
    id: int
    client_msg_id: str
    state: str = "running"  # running | cancelling
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    cancelled_at: float | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state}


class ChatSession:
    def __init__(
        self,
        settings: Settings,
        hub: EventHub,
        ui_loop: asyncio.AbstractEventLoop,
        agent_factory: AgentFactory,
        approval_callback: Any,
    ) -> None:
        self._settings = settings
        self._hub = hub
        self._ui_loop = ui_loop
        self._agent: Any = None
        self.disabled_reason: str | None = None
        self._transcript: list[dict[str, Any]] = []
        self._progress: collections.deque[str] = collections.deque(maxlen=MAX_PROGRESS_LINES)
        self._run: Run | None = None
        self._run_ids = 0
        self._entry_ids = 0
        self._last_message: tuple[str, int] | None = None  # (client_msg_id, run id)
        self.wedged = False
        self._shutdown = False

        try:
            self._agent = agent_factory(approval_callback, self._on_agent_progress)
        except Exception as exc:
            log.warning("Chat is disabled: %s", exc)
            self.disabled_reason = clean_line(str(exc)) or safe_error(exc)

    # -- what the page shows -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._agent is not None

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.disabled_reason,
            "wedged": self.wedged,
            "transcript": list(self._transcript),
            "run": self._run.public() if self._run else None,
            "progress": list(self._progress),
        }

    def _entry(self, role: str, text: str, context: str | None = None) -> dict[str, Any]:
        self._entry_ids += 1
        entry: dict[str, Any] = {"id": self._entry_ids, "role": role, "text": text[:MAX_ANSWER_CHARS]}
        if context:
            entry["context"] = context  # what was attached, as a short label for the page
        self._transcript.append(entry)
        del self._transcript[:-MAX_TRANSCRIPT_ENTRIES]
        return entry

    # -- starting a run ----------------------------------------------------------------

    def start(self, text: Any, client_msg_id: Any, context: Any = None) -> dict[str, Any]:
        """Begin a run for ``text``. Raises ``ApiError`` if that is not possible now.

        ``context`` is what the page attaches to this message (see ``context.py``).
        The agent gets it in front of ``text``; the transcript shows ``text`` and
        a short label for it. Like the text, it is part of this one message and
        is not kept anywhere else.
        """
        if not isinstance(client_msg_id, str) or not _MESSAGE_ID.fullmatch(client_msg_id):
            raise ApiError(400, "Missing or invalid message id.")
        # A repeated request (a retry after a lost response) gets the same run back.
        if self._last_message is not None and self._last_message[0] == client_msg_id:
            return {"run_id": self._last_message[1], "duplicate": True}
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "Write a message first.")
        if len(text) > MAX_PROMPT_CHARS:
            raise ApiError(400, f"The message is too long (at most {MAX_PROMPT_CHARS} characters).")
        attached = attached_context(context)
        if self._shutdown:
            raise ApiError(503, "The server is shutting down.")
        if self._agent is None:
            raise ApiError(503, f"Chat is not available: {self.disabled_reason}")
        if self.wedged:
            raise ApiError(
                503,
                "The previous run did not stop after a cancel. Restart the server to chat again.",
            )
        if self._run is not None:
            raise ApiError(409, "The agent is still working on the previous message.")

        self._run_ids += 1
        run = Run(id=self._run_ids, client_msg_id=client_msg_id)
        self._run = run
        self._last_message = (client_msg_id, run.id)
        self._progress.clear()
        entry = self._entry("user", text.strip(), context=attached[1] if attached else None)
        self._hub.publish("chat_started", {"run": run.public(), "entry": entry})

        prompt = f"{attached[0]}\n\n{text.strip()}" if attached else text.strip()
        threading.Thread(
            target=self._work,
            args=(run, self._agent, prompt),
            name=f"advisor-chat-{run.id}",
            daemon=True,
        ).start()
        return {"run_id": run.id, "duplicate": False}

    def _work(self, run: Run, agent: Any, text: str) -> None:
        """The run's thread. Never touches chat state: it only reports back."""
        kind: str
        payload: str | None = None
        try:
            if run.cancel_requested.is_set():
                kind = "cancelled"  # cancelled before the query began
            else:
                payload = agent.query(text)
                kind = "answer"
        except QueryCancelledError:
            kind = "cancelled"
        except BaseException as exc:
            log.exception("The chat run failed")
            kind, payload = "error", safe_error(exc)
        try:
            self._ui_loop.call_soon_threadsafe(self._finish, run.id, kind, payload)
        except RuntimeError:
            pass  # the web server's loop has stopped

    def _finish(self, run_id: int, kind: str, payload: str | None) -> None:
        run = self._run
        if run is None or run.id != run_id:
            return
        self._run = None
        self.wedged = False  # a run that ends frees the agent again, even a late one
        if kind == "answer":
            entry = self._entry("assistant", payload or "(no answer)")
        elif kind == "cancelled":
            entry = self._entry("note", "Cancelled. Tool calls made before the cancel may have taken effect.")
        else:
            entry = self._entry("error", payload or "The run failed.")
        self._hub.publish("chat_finished", {"run_id": run_id, "entry": entry})

    # -- progress ----------------------------------------------------------------------

    def _on_agent_progress(self, line: str) -> None:
        """Called on the agent's loop thread."""
        try:
            self._ui_loop.call_soon_threadsafe(self._add_progress, str(line))
        except RuntimeError:
            pass

    def _add_progress(self, line: str) -> None:
        if self._run is None:
            return  # a straggler from a run that has ended
        line = clean_line(line, MAX_PROGRESS_CHARS)
        if line:
            self._progress.append(line)
            self._hub.publish("chat_progress", {"run_id": self._run.id, "line": line})

    # -- cancel, reset, shutdown --------------------------------------------------------

    def cancel(self) -> bool:
        """Ask the running query to stop. False if nothing is running."""
        run = self._run
        if run is None:
            return False
        if run.state != "cancelling":
            run.state = "cancelling"
            run.cancelled_at = time.monotonic()
            run.cancel_requested.set()
            self._hub.publish("chat_state", {"run": run.public()})
            self._repeat_cancel(run.id)
        return True

    def _repeat_cancel(self, run_id: int) -> None:
        """Cancel again every second until the run ends or is declared wedged.

        A cancel that reaches the agent before its query has begun is taken to
        be meant for an earlier query and is dropped, so one call is not enough
        for a run that was cancelled the moment it started.
        """
        run = self._run
        if run is None or run.id != run_id or self._agent is None:
            return
        self._agent.cancel_query()
        if time.monotonic() - (run.cancelled_at or 0.0) > self._settings.wedge_seconds:
            if not self.wedged:
                self.wedged = True
                log.error("A chat run did not end within %.0fs of a cancel.", self._settings.wedge_seconds)
                self._hub.publish("chat_state", {"run": run.public(), "wedged": True})
            return
        self._ui_loop.call_later(_CANCEL_REPEAT_SECONDS, self._repeat_cancel, run_id)

    def reset(self) -> None:
        """Forget the conversation. Raises ``ApiError`` while a run is going."""
        if self._run is not None:
            raise ApiError(409, "Wait for the running message to finish, or cancel it, first.")
        if self._agent is not None:
            self._agent.reset_conversation()
        self._transcript.clear()
        self._progress.clear()
        self._last_message = None
        self._hub.publish("chat_reset", {})

    def begin_shutdown(self) -> None:
        """Refuse new runs and cancel the one going, repeating the cancel until it ends."""
        self._shutdown = True
        if self._run is not None:
            self.cancel()

    async def close(self) -> None:
        """Close the agent (its servers and loop) without blocking this loop for ever."""
        agent, self._agent = self._agent, None
        if agent is None:
            return
        done: concurrent.futures.Future[None] = concurrent.futures.Future()

        def close_agent() -> None:
            try:
                agent.close()
            except Exception:
                log.exception("Closing the agent failed")
            finally:
                try:
                    done.set_result(None)
                except concurrent.futures.InvalidStateError:
                    pass

        threading.Thread(target=close_agent, name="advisor-agent-close", daemon=True).start()
        try:
            await asyncio.wait_for(asyncio.wrap_future(done), self._settings.agent_close_timeout)
        except asyncio.TimeoutError:
            log.warning("The agent did not close within %.0fs; leaving it.", self._settings.agent_close_timeout)
