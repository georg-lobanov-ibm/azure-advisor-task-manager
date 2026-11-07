"""A scripted stand-in for the Anthropic API, for tests. Nothing leaves the machine.

``ScriptedModel`` is an ``httpx2`` mock transport that answers ``POST /v1/messages``
from a list of steps, so the real ``AsyncAnthropic`` client and the real tool
runner run unchanged against it.

    model = ScriptedModel([
        tool_use("jira_create_issue", {"project_key": "T", "summary": "s"}),
        text("Done."),
    ])
    agent = AdvisorAgent(client=model.client(), ...)

Each step is one model response. ``hang()`` is a step that never answers, to
test cancelling while the model is slow. ``model.requests`` holds the JSON body
of every request received, in order, and ``model.request_seen`` is a
``threading.Event`` set when the n-th request arrives (see ``wait_for_requests``).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx2
from anthropic import AsyncAnthropic

_ids = itertools.count(1)


@dataclass
class Step:
    content: list[dict[str, Any]]
    stop_reason: str
    delay: float = 0.0
    never_answers: bool = False
    status_code: int = 200
    extra: dict[str, Any] = field(default_factory=dict)


def text(body: str, *, delay: float = 0.0) -> Step:
    """A final answer."""
    return Step([{"type": "text", "text": body}], "end_turn", delay=delay)


def tool_use(name: str, arguments: dict[str, Any], *, preamble: str = "", delay: float = 0.0) -> Step:
    """A response that asks for one tool call."""
    content: list[dict[str, Any]] = []
    if preamble:
        content.append({"type": "text", "text": preamble})
    content.append(
        {"type": "tool_use", "id": f"toolu_{next(_ids):04d}", "name": name, "input": arguments}
    )
    return Step(content, "tool_use", delay=delay)


def hang() -> Step:
    """A response that never arrives (until the request is cancelled)."""
    return Step([], "end_turn", never_answers=True)


def api_error(status_code: int = 500) -> Step:
    return Step([], "end_turn", status_code=status_code)


class ScriptedModel:
    def __init__(self, steps: list[Step]):
        self._steps = list(steps)
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._waiters: list[tuple[int, threading.Event]] = []

    # -- test side ---------------------------------------------------------

    def client(self) -> AsyncAnthropic:
        """An ``AsyncAnthropic`` wired to this script. Create it in the test thread."""
        return AsyncAnthropic(
            api_key="test-key",
            base_url="http://mock.invalid",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle)),
        )

    def wait_for_requests(self, count: int, timeout: float = 10.0) -> bool:
        """Block until at least ``count`` requests have arrived."""
        with self._lock:
            if len(self.requests) >= count:
                return True
            event = threading.Event()
            self._waiters.append((count, event))
        return event.wait(timeout)

    def add_steps(self, *steps: Step) -> None:
        with self._lock:
            self._steps.extend(steps)

    # -- transport side (runs on the agent's loop) ---------------------------

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content or b"{}")
        with self._lock:
            self.requests.append(body)
            for count, event in list(self._waiters):
                if len(self.requests) >= count:
                    event.set()
                    self._waiters.remove((count, event))
            step = self._steps.pop(0) if self._steps else None

        if step is None:
            return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "script exhausted"}})
        if step.never_answers:
            await asyncio.Event().wait()
        if step.delay:
            await asyncio.sleep(step.delay)
        if step.status_code != 200:
            return httpx2.Response(
                step.status_code,
                json={"type": "error", "error": {"type": "api_error", "message": "scripted failure"}},
            )
        return httpx2.Response(
            200,
            json={
                "id": f"msg_{next(_ids):04d}",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "claude-test"),
                "content": step.content,
                "stop_reason": step.stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
                **step.extra,
            },
        )
