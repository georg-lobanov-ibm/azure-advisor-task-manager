"""AdvisorAgent: queries, cancelling, and closing, against fake servers and a scripted model."""

import concurrent.futures
import threading
import time

import pytest

from advisor_agent.agent import AdvisorAgent, QueryCancelledError, _history_after_cancel
from conftest import pid_alive, wait_until
from fakes.anthropic_mock import ScriptedModel, hang, text, tool_use

JOIN_SECONDS = 20


class Running:
    """A ``query()`` running on its own thread."""

    def __init__(self, agent: AdvisorAgent, prompt: str):
        self.result: str | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(agent, prompt), daemon=True)
        self.thread.start()

    def _run(self, agent: AdvisorAgent, prompt: str) -> None:
        try:
            self.result = agent.query(prompt)
        except BaseException as exc:  # the tests look at what was raised
            self.error = exc

    def finished(self, timeout: float = JOIN_SECONDS) -> bool:
        self.thread.join(timeout)
        return not self.thread.is_alive()


def make_agent(model: ScriptedModel, *servers, tmp_path, **kwargs) -> AdvisorAgent:
    return AdvisorAgent(
        model="claude-test",
        workspace_dir=str(tmp_path / "workspace"),
        servers=[server.config for server in servers],
        client=model.client(),
        progress=kwargs.pop("progress", lambda line: None),
        **kwargs,
    )


def test_query_answers_and_keeps_the_conversation(fake_server, tmp_path):
    azure = fake_server("azure")
    model = ScriptedModel([text("first answer"), text("second answer")])

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        assert agent.query("one") == "first answer"
        assert agent.query("two") == "second answer"

    roles = [message["role"] for message in model.requests[1]["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_tool_call_reaches_the_fake_server(fake_server, tmp_path):
    azure = fake_server("azure")
    model = ScriptedModel(
        [
            tool_use("advisor", {"intent": "list", "command": "advisor_recommendation_list"}),
            text("two recommendations"),
        ]
    )

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        assert agent.query("list them") == "two recommendations"

    assert len(azure.calls()) == 1


def test_cancel_while_the_model_is_slow(fake_server, tmp_path):
    azure = fake_server("azure")
    model = ScriptedModel([hang(), text("after the cancel")])

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        running = Running(agent, "slow question")
        assert model.wait_for_requests(1)

        agent.cancel_query()

        assert running.finished()
        assert isinstance(running.error, QueryCancelledError)

        # The agent is still usable, and the model is told about the cancel.
        assert agent.query("next question") == "after the cancel"

    messages = model.requests[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert "cancelled" in messages[1]["content"]


def test_cancel_while_a_tool_call_is_running_names_the_call(fake_server, tmp_path):
    azure = fake_server("azure", FAKE_MCP_HANG_CALL="1")
    model = ScriptedModel([tool_use("advisor", {"intent": "list"}), text("recovered")])

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        running = Running(agent, "list")
        assert wait_until(lambda: len(azure.calls()) == 1)

        agent.cancel_query()

        assert running.finished()
        assert isinstance(running.error, QueryCancelledError)
        assert agent.query("again") == "recovered"

    messages = model.requests[1]["messages"]
    # No unanswered tool_use is kept: the API would reject the next request.
    assert not any(
        isinstance(m["content"], list) and any(b.get("type") == "tool_use" for b in m["content"])
        for m in messages
    )
    assert "advisor" in messages[1]["content"]
    assert "unknown" in messages[1]["content"]


def test_cancel_with_nothing_running_does_nothing(fake_server, tmp_path):
    azure = fake_server("azure")
    model = ScriptedModel([text("fine")])

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        agent.cancel_query()
        agent.cancel_query()

        assert agent.query("hello") == "fine"


def test_cancel_while_the_servers_are_starting_leaves_no_process(fake_server, tmp_path):
    azure = fake_server("azure", FAKE_MCP_HANG_START="1")
    model = ScriptedModel([text("unused")])

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        running = Running(agent, "hello")
        assert wait_until(lambda: azure.pid() is not None)
        pid = azure.pid()

        agent.cancel_query()

        assert running.finished()
        assert isinstance(running.error, QueryCancelledError)
        assert wait_until(lambda: not pid_alive(pid)), "the server process was left running"
    assert model.requests == []


def test_failed_start_is_reported_and_leaves_no_process(fake_server, tmp_path):
    azure = fake_server("azure", FAKE_MCP_CRASH_START="1")
    model = ScriptedModel([text("unused")])

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        with pytest.raises(Exception) as caught:
            agent.query("hello")

    assert not isinstance(caught.value, QueryCancelledError)
    pid = azure.pid()
    assert pid is not None and wait_until(lambda: not pid_alive(pid))


def test_close_while_a_query_is_blocked_returns_and_frees_the_lock(fake_server, tmp_path):
    azure = fake_server("azure")
    model = ScriptedModel([hang()])
    agent = make_agent(model, azure, tmp_path=tmp_path)
    running = Running(agent, "never answered")
    assert model.wait_for_requests(1)

    started = time.monotonic()
    agent.close()
    elapsed = time.monotonic() - started

    assert elapsed < 15, f"close() took {elapsed:.1f}s"
    assert running.finished()
    assert isinstance(running.error, QueryCancelledError)
    pid = azure.pid()
    assert pid is not None and wait_until(lambda: not pid_alive(pid))
    assert agent._query_lock.acquire(blocking=False), "the query lock was left held"
    agent._query_lock.release()


def test_close_is_idempotent_and_refuses_new_queries(fake_server, tmp_path):
    azure = fake_server("azure")
    model = ScriptedModel([text("hi")])
    agent = make_agent(model, azure, tmp_path=tmp_path)
    assert agent.query("hello") == "hi"

    agent.close()
    agent.close()

    with pytest.raises(RuntimeError, match="closed"):
        agent.query("again")


def test_a_cancel_does_not_leak_into_the_next_query(fake_server, tmp_path):
    azure = fake_server("azure")
    model = ScriptedModel([text("one"), text("two")])

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        assert agent.query("first") == "one"
        agent.cancel_query()  # nothing was running
        time.sleep(0.2)

        assert agent.query("second") == "two"


def test_a_cancel_that_came_before_the_query_task_started_is_not_lost(fake_server, tmp_path):
    # query() reads the cancel counter, then hands the work to the loop. A
    # cancel_query() in between finds no task to cancel; the counter is what
    # carries it over. Simulated by passing a counter value that is out of date.
    azure = fake_server("azure")
    model = ScriptedModel([text("connected"), text("unused")])

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        assert agent.query("connect first") == "connected"
        history_before = list(agent._history)
        agent.cancel_query()
        stale = agent._cancel_count - 1

        with pytest.raises(QueryCancelledError):
            agent._run_coroutine(agent._aquery("too late", stale))

        assert agent._history == history_before
        assert len(model.requests) == 1  # nothing was sent for the cancelled query


def test_a_cancel_that_came_before_the_servers_started_leaves_no_process(fake_server, tmp_path):
    azure = fake_server("azure")
    model = ScriptedModel([text("unused")])

    with make_agent(model, azure, tmp_path=tmp_path) as agent:
        agent.cancel_query()
        stale = agent._cancel_count - 1

        with pytest.raises(concurrent.futures.CancelledError):
            agent._ensure_connected(stale)

        assert azure.pid() is None, "a server was started although the query was cancelled"
        assert not agent._connected


# -- approval-gated tools ---------------------------------------------------------

WRITE_ARGS = {"project_key": "T", "summary": "s"}


def jira_server(fake_server):
    return fake_server("jira", approval=lambda tool: tool == "jira_create_issue")


def written(jira) -> list[str]:
    return [line for line in jira.calls() if "jira_create_issue" in line]


def test_approved_write_reaches_the_server(fake_server, tmp_path):
    jira = jira_server(fake_server)
    seen = []

    async def approve(request):
        seen.append((request.server, request.tool, request.arguments))
        return True

    model = ScriptedModel([tool_use("jira_create_issue", WRITE_ARGS), text("created")])
    with make_agent(model, jira, tmp_path=tmp_path, approval_callback=approve) as agent:
        assert agent.query("create it") == "created"

    assert seen == [("jira", "jira_create_issue", WRITE_ARGS)]
    assert len(written(jira)) == 1


def test_read_tool_needs_no_approval(fake_server, tmp_path):
    jira = jira_server(fake_server)

    async def approve(request):
        raise AssertionError("a read tool must not ask")

    model = ScriptedModel([tool_use("jira_search", {"jql": "x"}), text("none found")])
    with make_agent(model, jira, tmp_path=tmp_path, approval_callback=approve) as agent:
        assert agent.query("search") == "none found"

    assert len(jira.calls()) == 1


@pytest.mark.parametrize("answer", [False, None, 1, "yes", "True", {"approved": True}])
def test_anything_but_literal_true_denies(fake_server, tmp_path, answer):
    jira = jira_server(fake_server)

    async def approve(request):
        return answer

    model = ScriptedModel([tool_use("jira_create_issue", WRITE_ARGS), text("denied")])
    with make_agent(model, jira, tmp_path=tmp_path, approval_callback=approve) as agent:
        assert agent.query("create it") == "denied"

    assert written(jira) == []
    result = model.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True


def test_failing_approval_callback_denies(fake_server, tmp_path):
    jira = jira_server(fake_server)

    async def approve(request):
        raise RuntimeError("the approval UI is broken")

    model = ScriptedModel([tool_use("jira_create_issue", WRITE_ARGS), text("could not ask")])
    with make_agent(model, jira, tmp_path=tmp_path, approval_callback=approve) as agent:
        assert agent.query("create it") == "could not ask"

    assert written(jira) == []


def test_cancel_while_an_approval_is_pending_denies_the_call(fake_server, tmp_path):
    jira = jira_server(fake_server)
    asked = threading.Event()
    outcome = []

    async def approve(request):
        import asyncio

        asked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    model = ScriptedModel([tool_use("jira_create_issue", WRITE_ARGS), text("unused")])
    with make_agent(model, jira, tmp_path=tmp_path, approval_callback=approve) as agent:
        running = Running(agent, "create it")
        assert asked.wait(10)

        agent.cancel_query()

        assert running.finished()
        assert isinstance(running.error, QueryCancelledError)

    assert outcome == ["cancelled"]
    assert written(jira) == []


def test_close_while_an_approval_is_pending_denies_the_call(fake_server, tmp_path):
    jira = jira_server(fake_server)
    asked = threading.Event()

    async def approve(request):
        import asyncio

        asked.set()
        await asyncio.Event().wait()

    model = ScriptedModel([tool_use("jira_create_issue", WRITE_ARGS), text("unused")])
    agent = make_agent(model, jira, tmp_path=tmp_path, approval_callback=approve)
    running = Running(agent, "create it")
    assert asked.wait(10)

    agent.close()

    assert running.finished()
    assert isinstance(running.error, QueryCancelledError)
    assert written(jira) == []
    pid = jira.pid()
    assert pid is not None and wait_until(lambda: not pid_alive(pid))


# -- the trimmed history that a cancelled query leaves behind ---------------------


def test_history_after_cancel_drops_unanswered_tool_calls():
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "t1", "name": "jira_create_issue", "input": {}},
            ],
        },
    ]

    kept = _history_after_cancel(messages)

    assert kept[0] == messages[0]
    assert len(kept) == 2
    assert kept[1]["role"] == "assistant" and isinstance(kept[1]["content"], str)
    assert "jira_create_issue" in kept[1]["content"]


def test_history_after_cancel_after_finished_tool_results_adds_a_note():
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]

    kept = _history_after_cancel(messages)

    assert kept[:3] == messages
    assert kept[3]["role"] == "assistant" and "cancelled" in kept[3]["content"]


def test_history_after_cancel_escapes_tool_names():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "evil\x1b[31m\nname", "input": {}}],
        },
    ]

    note = _history_after_cancel(messages)[-1]["content"]

    assert "\x1b" not in note and "\n" not in note
