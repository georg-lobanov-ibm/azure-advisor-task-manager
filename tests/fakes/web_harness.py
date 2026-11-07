"""Helpers for the web UI tests: the app under a test client, a scriptable agent, an event watcher.

``running_web`` builds the real ``create_app`` with a temporary database and
runs its lifespan inside Starlette's ``TestClient``. No port is opened: the
client only has to send the ``Host`` and ``Origin`` the security middleware
expects, which is why ``PORT`` is fixed.

A ``TestClient`` call returns only when the response has ended, and an event
stream does not end by itself. ``Web.watching`` therefore opens the stream on a
helper thread, and ends it (``EventHub.close_all``) when the ``with`` block is
left; the frames it saw are then in ``watcher.events``.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import threading
from typing import Any, Callable, Iterator

from starlette.testclient import TestClient

from advisor_agent.agent import QueryCancelledError
from advisor_agent.database import AdvisorDatabase
from advisor_agent.mcp_tools import McpServerConfig
from advisor_agent.web.app import Services, create_app
from advisor_agent.web.settings import Settings
from conftest import wait_until

PORT = 8765
TOKEN = "test-launch-token"
ORIGIN = f"http://127.0.0.1:{PORT}"
# Fast retries and a long cache: nothing here should wait on a timer.
FAST_AZURE = {"backoff": (0.2,), "cache_seconds": 30.0}


class FakeAgent:
    """Stands in for ``AdvisorAgent`` in the chat tests; the test decides what a query does.

    ``query`` records the prompt, reports ``lines`` as progress, and then either
    returns ``answer``, raises ``fail``, or (with ``block``) waits for
    ``release`` and for a cancel, which ends it with ``QueryCancelledError``
    unless ``honours_cancel`` is false.
    """

    def __init__(
        self,
        *,
        lines: tuple[str, ...] = (),
        answer: str = "fake answer",
        block: bool = False,
        fail: str | None = None,
        honours_cancel: bool = True,
    ) -> None:
        self.lines = lines
        self.answer = answer
        self.block = block
        self.fail = fail
        self.honours_cancel = honours_cancel
        self.prompts: list[str] = []
        self.began = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.cancel_calls = 0
        self.resets = 0
        self.closed = threading.Event()
        self.approval_callback: Any = None
        self.progress: Callable[[str], None] = lambda line: None

    def query(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.began.set()
        for line in self.lines:
            self.progress(line)
        if self.fail:
            raise RuntimeError(self.fail)
        if self.block:
            while not self.release.wait(0.01):
                if self.honours_cancel and self.cancelled.is_set():
                    self.cancelled.clear()
                    raise QueryCancelledError("cancelled")
        return self.answer

    def cancel_query(self) -> None:
        self.cancel_calls += 1
        self.cancelled.set()

    def reset_conversation(self) -> None:
        self.resets += 1

    def close(self) -> None:
        self.release.set()
        self.closed.set()


def fake_agent(**kwargs: Any) -> tuple[FakeAgent, Callable[[Any, Any], FakeAgent]]:
    """A ``FakeAgent`` and the agent factory that hands it to the app."""
    agent = FakeAgent(**kwargs)

    def factory(approval_callback: Any, progress: Callable[[str], None]) -> FakeAgent:
        agent.approval_callback = approval_callback
        agent.progress = progress
        return agent

    return agent, factory


def parse_events(text: str) -> list[tuple[str, Any]]:
    """``[(event, data), ...]`` from the text of an event stream (comments are skipped)."""
    events: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        name: str | None = None
        data: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data.append(line[len("data: ") :])
        if name is not None:
            events.append((name, json.loads("\n".join(data)) if data else None))
    return events


class Watcher:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.status: int | None = None
        self.headers: Any = None

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def of(self, name: str) -> list[Any]:
        return [data for event, data in self.events if event == name]


class Web:
    def __init__(self, client: TestClient, services: Services, settings: Settings, db: AdvisorDatabase):
        self.client = client
        self.services = services
        self.settings = settings
        self.db = db

    # -- requests ----------------------------------------------------------------------

    def sign_in(self) -> None:
        response = self.client.post("/api/login", json={"token": TOKEN}, headers={"Origin": ORIGIN})
        assert response.status_code == 200, response.text

    def get(self, path: str, **kwargs: Any):
        return self.client.get(path, **kwargs)

    def send(self, method: str, path: str, body: Any = None, *, headers: dict[str, str] | None = None):
        """A state-changing request the way the page sends it: JSON, with our Origin."""
        return self.client.request(
            method,
            path,
            json={} if body is None else body,
            headers={"Origin": ORIGIN, **(headers or {})},
        )

    def post(self, path: str, body: Any = None, **kwargs: Any):
        return self.send("POST", path, body, **kwargs)

    # -- the server's loop ---------------------------------------------------------------

    def on_loop(self, fn: Callable[..., Any], *args: Any, timeout: float = 10.0) -> Any:
        """Run ``fn(*args)`` on the server's event loop and return its result."""
        loop = self.services.loop
        assert loop is not None
        done: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def call() -> None:
            try:
                done.set_result(fn(*args))
            except BaseException as exc:
                done.set_exception(exc)

        loop.call_soon_threadsafe(call)
        return done.result(timeout)

    def wait_azure(self, state: str = "ready", timeout: float = 30.0) -> None:
        assert wait_until(lambda: self.services.azure.status["state"] == state, timeout), (
            f"Azure connection is {self.services.azure.status!r}, wanted {state!r}"
        )

    def chat_state(self) -> dict[str, Any]:
        return self.on_loop(self.services.chat.snapshot)

    def wait_chat_idle(self, timeout: float = 20.0) -> None:
        assert wait_until(lambda: self.chat_state()["run"] is None, timeout), "the chat run did not end"

    def pending_approvals(self) -> list[dict[str, Any]]:
        return self.on_loop(self.services.approvals.snapshot)

    def wait_approval(self, timeout: float = 20.0) -> dict[str, Any]:
        assert wait_until(lambda: bool(self.pending_approvals()), timeout), "no approval was requested"
        return self.pending_approvals()[0]

    def audit(self, action: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.get_audit_log(limit=1000)
        return [row for row in rows if action is None or row["action"] == action][::-1]

    # -- the event stream ------------------------------------------------------------------

    @contextlib.contextmanager
    def watching(self) -> Iterator[Watcher]:
        """Behave like an open page: hold an event stream open, and collect it when the block ends."""
        watcher = Watcher()
        errors: list[BaseException] = []

        def read() -> None:
            try:
                response = self.client.get("/api/events")
                watcher.status = response.status_code
                watcher.headers = response.headers
                watcher.events = parse_events(response.text)
            except BaseException as exc:  # reported by the test thread below
                errors.append(exc)

        thread = threading.Thread(target=read, name="test-event-watcher", daemon=True)
        thread.start()
        assert wait_until(lambda: self.services.hub.viewer_count == 1 or not thread.is_alive(), 10)
        try:
            yield watcher
        finally:
            # close_all() throws away frames the stream has not sent yet, so let it catch up first.
            hub = self.services.hub
            wait_until(lambda: all(sub.queue.empty() for sub in list(hub._subscribers)), 10)
            self.on_loop(hub.close_all)
            thread.join(10)
            assert not thread.is_alive(), "the event stream did not end"
        if errors:
            raise errors[0]


@contextlib.contextmanager
def running_web(
    tmp_path,
    *,
    agent_factory: Callable[[Any, Any], Any],
    azure_config: McpServerConfig,
    signed_in: bool = True,
    azure_options: dict[str, Any] | None = None,
    **settings: Any,
) -> Iterator[Web]:
    """The real app with its lifespan running, under a test client."""
    config = Settings(token=TOKEN, port=PORT, **settings)
    db = AdvisorDatabase(str(tmp_path / "advisor.db"))
    app = create_app(
        config,
        agent_factory=agent_factory,
        azure_config=azure_config,
        db=db,
        azure_options=FAST_AZURE if azure_options is None else azure_options,
    )
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        web = Web(client, app.state.services, config, db)
        if signed_in:
            web.sign_in()
        yield web
