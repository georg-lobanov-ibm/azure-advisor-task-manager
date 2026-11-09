"""Approving Jira writes in the browser. Anything but a clear, recorded "yes" must be a "no"."""

import asyncio
import json
import threading

import pytest

from advisor_agent.agent import AdvisorAgent
from advisor_agent.mcp_tools import ApprovalRequest
from advisor_agent.web import approvals
from conftest import wait_until
from fakes.anthropic_mock import ScriptedModel, text, tool_use

WRITE_ARGS = {"project_key": "T", "summary": "s"}
ACTION = "jira_tool_approval"


class Asking:
    """The agent's side: ``callback(request)`` running on a loop of its own, as it does in the agent."""

    def __init__(self, app, arguments=None, *, server="jira", tool="jira_create_issue"):
        self.request = ApprovalRequest(server, tool, WRITE_ARGS if arguments is None else arguments)
        self.result = None
        self.error: BaseException | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(app.services.approvals.callback,), daemon=True)
        self.thread.start()
        assert self._ready.wait(5)

    def _run(self, callback) -> None:
        async def main() -> None:
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.current_task()
            self._ready.set()
            try:
                self.result = await callback(self.request)
            except BaseException as exc:  # the tests look at what was raised
                self.error = exc

        asyncio.run(main())

    def cancel(self) -> None:
        assert self.loop is not None and self.task is not None
        self.loop.call_soon_threadsafe(self.task.cancel)

    def finished(self, timeout: float = 20.0) -> bool:
        self.thread.join(timeout)
        return not self.thread.is_alive()


def decide(app, approval_id, decision="approve"):
    return app.post(f"/api/approvals/{approval_id}", {"decision": decision})


def only_audit_row(app):
    rows = app.audit(ACTION)
    assert len(rows) == 1, rows
    return rows[0]


def metadata(row):
    return json.loads(row["metadata"])


# -- the decision -------------------------------------------------------------------------


def test_approve_lets_the_call_run_and_is_recorded(web):
    with web() as app:
        asking = Asking(app)
        pending = app.wait_approval()

        assert (pending["server"], pending["tool"], pending["arguments"]) == ("jira", "jira_create_issue", WRITE_ARGS)
        assert decide(app, pending["id"], "approve").status_code == 200
        assert asking.finished()

        row = only_audit_row(app)
        # The request is taken off the page's list once the callback has returned.
        assert wait_until(lambda: app.pending_approvals() == [])

    assert asking.result is True and asking.error is None
    assert row["decision"] == "approved" and row["rationale"] == "jira:jira_create_issue"
    assert row["user_approval"] in (1, True)
    assert metadata(row)["request_id"] == pending["id"]


def test_deny_stops_the_call_and_is_recorded(web):
    with web() as app:
        asking = Asking(app)
        pending = app.wait_approval()

        assert decide(app, pending["id"], "deny").status_code == 200
        assert asking.finished()
        row = only_audit_row(app)

    assert asking.result is False
    assert row["decision"] == "denied" and row["user_approval"] in (0, False)


def test_an_approval_nobody_answers_times_out_as_a_no(web):
    with web(approval_timeout=0.6) as app:
        asking = Asking(app)
        pending = app.wait_approval()

        assert asking.finished()
        late = decide(app, pending["id"], "approve")
        row = only_audit_row(app)

    assert asking.result is False
    assert row["decision"] == "timeout" and row["user_approval"] is None
    # A click that arrives after the timeout does nothing, and says why.
    assert late.status_code == 409 and late.json()["outcome"] == "timeout"


def test_cancelling_the_wait_is_a_no_and_is_recorded(web):
    with web() as app:
        asking = Asking(app)
        pending = app.wait_approval()

        asking.cancel()
        assert asking.finished()
        late = decide(app, pending["id"], "approve")
        row = only_audit_row(app)

    assert isinstance(asking.error, asyncio.CancelledError) and asking.result is None
    assert row["decision"] == "cancelled"
    assert late.status_code == 409 and late.json()["outcome"] == "cancelled"


def test_a_second_decision_is_a_409_and_the_first_stands(web):
    with web() as app:
        asking = Asking(app)
        pending = app.wait_approval()

        first = decide(app, pending["id"], "deny")
        second = decide(app, pending["id"], "approve")
        assert asking.finished()

    assert first.status_code == 200
    assert second.status_code == 409 and second.json()["outcome"] == "denied"
    assert asking.result is False


def test_two_requests_are_decided_separately(web):
    with web() as app:
        one, two = Asking(app, {"summary": "one"}), Asking(app, {"summary": "two"})
        assert wait_until(lambda: len(app.pending_approvals()) == 2)
        by_summary = {item["arguments"]["summary"]: item["id"] for item in app.pending_approvals()}

        decide(app, by_summary["two"], "approve")
        assert two.finished()
        assert one.thread.is_alive()  # the other one still waits
        decide(app, by_summary["one"], "deny")
        assert one.finished()

    assert (one.result, two.result) == (False, True)


def test_an_unknown_request_is_a_404(web):
    with web() as app:
        unknown = decide(app, "does-not-exist")
        odd = decide(app, "has.dots")

    assert unknown.status_code == 404
    assert odd.status_code == 404


@pytest.mark.parametrize(
    "body",
    [{}, {"decision": "Approve"}, {"decision": "yes"}, {"decision": True}, {"decision": 1}, {"decision": None}, {"approve": True}],
)
def test_only_approve_and_deny_are_decisions(web, body):
    with web() as app:
        asking = Asking(app)
        pending = app.wait_approval()

        response = app.post(f"/api/approvals/{pending['id']}", body)
        still_waiting = app.pending_approvals()
        decide(app, pending["id"], "deny")
        assert asking.finished()

    assert response.status_code == 400
    assert len(still_waiting) == 1


# -- the cases that must never approve ------------------------------------------------------


def test_an_approval_whose_audit_row_cannot_be_written_is_a_no(web, monkeypatch):
    with web() as app:

        def no_audit(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(app.services.storage.db, "log_audit", no_audit)
        asking = Asking(app)
        pending = app.wait_approval()

        assert decide(app, pending["id"], "approve").status_code == 200  # the person did say yes
        assert asking.finished()

    assert asking.result is False


def test_arguments_that_cannot_be_shown_are_a_no_and_never_asked(web):
    with web() as app:
        asking = Asking(app, {"summary": {"not json", "serialisable"}})
        assert asking.finished()
        pending = app.pending_approvals()
        row = only_audit_row(app)

    assert asking.result is False and asking.error is None
    assert pending == []
    assert row["decision"] == "error" and metadata(row)["args_sha256"] is None


def test_arguments_too_big_to_review_are_a_no_and_never_asked(web):
    big = {"summary": "x" * (approvals.MAX_ARGUMENT_BYTES + 1)}
    with web() as app:
        asking = Asking(app, big)
        assert asking.finished()
        assert app.pending_approvals() == []
        row = only_audit_row(app)

    assert asking.result is False
    assert row["decision"] == "too_large"
    assert metadata(row)["arguments_truncated"] is True
    assert len(metadata(row)["args_sha256"]) == 64


def test_with_no_page_open_the_request_is_denied_after_the_grace_period(web):
    with web(approval_viewer_grace=0.3) as app:  # nothing is watching
        asking = Asking(app)

        assert asking.finished()
        row = only_audit_row(app)

    assert asking.result is False and row["decision"] == "no_viewer"


def test_an_open_page_keeps_the_request_alive_past_the_grace_period(web):
    with web(approval_viewer_grace=0.3) as app:
        with app.watching():
            asking = Asking(app)
            pending = app.wait_approval()
            assert not asking.finished(1.5)  # longer than the grace

            decide(app, pending["id"], "approve")
            assert asking.finished()

    assert asking.result is True


def test_shutdown_denies_what_is_waiting(web):
    with web() as app:
        asking = Asking(app)
        pending = app.wait_approval()

        app.on_loop(app.services.begin_shutdown)

        assert asking.finished()
        late = decide(app, pending["id"], "approve")
        row = only_audit_row(app)

    assert asking.result is False
    assert row["decision"] == "shutdown"
    assert late.status_code == 409


def test_a_request_that_arrives_during_shutdown_is_denied_without_asking(web):
    with web() as app:
        app.on_loop(app.services.begin_shutdown)

        asking = Asking(app)

        assert asking.finished()
        assert app.pending_approvals() == []
        assert only_audit_row(app)["decision"] == "shutdown"
    assert asking.result is False


def test_a_request_after_the_web_server_has_stopped_is_a_no(web):
    with web() as app:
        bridge = app.services.approvals
    # The lifespan has ended: the loop that would show the request is gone.
    result = asyncio.run(bridge.callback(ApprovalRequest("jira", "jira_create_issue", WRITE_ARGS)))

    assert result is False


# -- what is written down -----------------------------------------------------------------


def test_the_audit_record_names_the_call_and_hashes_the_arguments(web):
    import hashlib

    with web() as app:
        asking = Asking(app, {"summary": "s", "project_key": "T"})
        pending = app.wait_approval()
        decide(app, pending["id"], "approve")
        assert asking.finished()
        row = only_audit_row(app)

    expected_text = json.dumps({"summary": "s", "project_key": "T"}, sort_keys=True, separators=(",", ":"))
    meta = metadata(row)
    assert meta["tool"] == "jira:jira_create_issue" and meta["outcome"] == "approved"
    assert meta["arguments"] == expected_text and meta["arguments_truncated"] is False
    assert meta["args_sha256"] == hashlib.sha256(expected_text.encode()).hexdigest()


def test_long_arguments_are_cut_in_the_record_but_the_hash_covers_all_of_them(web):
    import hashlib

    arguments = {"summary": "y" * 10_000}
    with web() as app:
        asking = Asking(app, arguments)
        decide(app, app.wait_approval()["id"], "deny")
        assert asking.finished()
        meta = metadata(only_audit_row(app))

    full = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    assert len(meta["arguments"]) == approvals.AUDIT_ARGUMENT_CHARS and meta["arguments_truncated"] is True
    assert meta["args_sha256"] == hashlib.sha256(full.encode()).hexdigest()


# -- what the page is told ----------------------------------------------------------------


def test_open_pages_hear_about_a_request_and_how_it_ended(web):
    with web() as app:
        with app.watching() as watcher:
            asking = Asking(app)
            pending = app.wait_approval()
            decide(app, pending["id"], "deny")
            assert asking.finished()

    requested, resolved = watcher.of("approval_requested"), watcher.of("approval_resolved")
    assert [item["id"] for item in requested] == [pending["id"]]
    assert requested[0]["arguments"] == WRITE_ARGS and requested[0]["expires_in"] > 0
    assert resolved == [{"id": pending["id"], "outcome": "denied"}]
    assert watcher.names().index("approval_requested") < watcher.names().index("approval_resolved")


def test_a_page_opened_later_gets_the_waiting_request_in_its_snapshot(web):
    with web() as app:
        asking = Asking(app)
        pending = app.wait_approval()

        with app.watching() as watcher:
            pass

        decide(app, pending["id"], "deny")
        assert asking.finished()

    waiting = watcher.events[0][1]["approvals"]
    assert [item["id"] for item in waiting] == [pending["id"]]


# -- through the real agent -------------------------------------------------------------------


def jira_agent_factory(model, jira, tmp_path):
    def factory(approval_callback, progress, on_tool_result):
        return AdvisorAgent(
            model="claude-test",
            workspace_dir=str(tmp_path / "agent-workspace"),
            servers=[jira.config],
            client=model.client(),
            approval_callback=approval_callback,
            progress=progress,
            on_tool_result=on_tool_result,
        )

    return factory


def jira_server(fake_server):
    return fake_server("jira", approval=lambda tool: tool == "jira_create_issue")


def written(jira):
    return [line for line in jira.calls() if "jira_create_issue" in line]


def send(app, prompt="make a ticket"):
    return app.post("/api/chat", {"text": prompt, "client_msg_id": "m1"})


def test_a_write_the_person_denies_never_reaches_jira(web, fake_server, tmp_path):
    jira = jira_server(fake_server)
    model = ScriptedModel([tool_use("jira_create_issue", WRITE_ARGS), text("understood, not created")])
    with web(agent_factory=jira_agent_factory(model, jira, tmp_path)) as app:
        with app.watching():
            assert send(app).status_code == 202
            pending = app.wait_approval(60)
            assert pending["tool"] == "jira_create_issue"

            assert decide(app, pending["id"], "deny").status_code == 200
            app.wait_chat_idle(60)

        transcript = app.chat_state()["transcript"]
        row = only_audit_row(app)

    assert written(jira) == []
    assert transcript[-1]["text"] == "understood, not created"
    assert row["decision"] == "denied"
    # The model was told it did not happen.
    tool_result = model.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True


def test_a_write_the_person_approves_reaches_jira_once(web, fake_server, tmp_path):
    jira = jira_server(fake_server)
    model = ScriptedModel([tool_use("jira_create_issue", WRITE_ARGS), text("created")])
    with web(agent_factory=jira_agent_factory(model, jira, tmp_path)) as app:
        with app.watching():
            send(app)
            pending = app.wait_approval(60)

            decide(app, pending["id"], "approve")
            app.wait_chat_idle(60)

        row = only_audit_row(app)

    assert len(written(jira)) == 1
    assert row["decision"] == "approved"


def test_a_read_needs_no_approval(web, fake_server, tmp_path):
    jira = jira_server(fake_server)
    model = ScriptedModel([tool_use("jira_search", {"jql": "project = T"}), text("nothing found")])
    with web(agent_factory=jira_agent_factory(model, jira, tmp_path)) as app:
        with app.watching():
            send(app, "search")
            app.wait_chat_idle(60)

        assert app.audit(ACTION) == []
        assert app.chat_state()["transcript"][-1]["text"] == "nothing found"


def test_cancelling_the_chat_while_a_write_waits_denies_it(web, fake_server, tmp_path):
    jira = jira_server(fake_server)
    model = ScriptedModel([tool_use("jira_create_issue", WRITE_ARGS), text("should not be reached")])
    with web(agent_factory=jira_agent_factory(model, jira, tmp_path)) as app:
        with app.watching() as watcher:
            send(app)
            pending = app.wait_approval(60)

            assert app.post("/api/chat/cancel").json() == {"cancelling": True}
            app.wait_chat_idle(60)
            late = decide(app, pending["id"], "approve")

        row = only_audit_row(app)

    assert written(jira) == []
    assert row["decision"] == "cancelled"
    assert late.status_code == 409
    assert [item["outcome"] for item in watcher.of("approval_resolved")] == ["cancelled"]


def test_shutting_down_while_a_write_waits_denies_it(web, fake_server, tmp_path):
    jira = jira_server(fake_server)
    model = ScriptedModel([tool_use("jira_create_issue", WRITE_ARGS), text("should not be reached")])
    with web(agent_factory=jira_agent_factory(model, jira, tmp_path)) as app:
        with app.watching():
            send(app)
            app.wait_approval(60)

            app.on_loop(app.services.begin_shutdown)
            app.wait_chat_idle(60)

        row = only_audit_row(app)

    assert written(jira) == []
    assert row["decision"] in ("shutdown", "cancelled")
