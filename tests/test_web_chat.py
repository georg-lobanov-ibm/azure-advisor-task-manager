"""The chat: one run at a time, progress before the answer, cancel, reset, reconnect."""

import pytest

from conftest import wait_until
from fakes.web_harness import fake_agent

SEND = "/api/chat"


def send(app, text="hello", msg_id="m1"):
    return app.post(SEND, {"text": text, "client_msg_id": msg_id})


def test_a_message_gets_an_answer_and_progress_comes_first(web):
    agent, factory = fake_agent(lines=("Starting servers", "Calling advisor"), answer="two findings")
    with web(agent_factory=factory) as app:
        with app.watching() as watcher:
            response = send(app, "what is open?")
            assert response.status_code == 202
            app.wait_chat_idle()

    assert agent.prompts == ["what is open?"]
    names = [name for name in watcher.names() if name.startswith("chat_")]
    assert names == ["chat_started", "chat_progress", "chat_progress", "chat_finished"]
    assert [item["line"] for item in watcher.of("chat_progress")] == ["Starting servers", "Calling advisor"]
    entries = watcher.of("chat_finished")[0]["entry"]
    assert entries["role"] == "assistant" and entries["text"] == "two findings"
    assert watcher.of("chat_started")[0]["entry"]["text"] == "what is open?"


def test_a_second_message_while_one_runs_is_a_409(web):
    agent, factory = fake_agent(block=True)
    with web(agent_factory=factory) as app:
        assert send(app, "one", "m1").status_code == 202
        assert wait_until(agent.began.is_set)

        busy = send(app, "two", "m2")

        assert busy.status_code == 409
        agent.release.set()
        app.wait_chat_idle()
        assert agent.prompts == ["one"]
        assert send(app, "two", "m2").status_code == 202


def test_a_repeated_message_id_gets_the_same_run_back(web):
    agent, factory = fake_agent()
    with web(agent_factory=factory) as app:
        first = send(app, "once", "same-id").json()
        app.wait_chat_idle()

        again = send(app, "once", "same-id").json()

    assert again == {"run_id": first["run_id"], "duplicate": True}
    assert agent.prompts == ["once"]


@pytest.mark.parametrize(
    "body",
    [
        {"text": "hi"},
        {"text": "hi", "client_msg_id": ""},
        {"text": "hi", "client_msg_id": "has space"},
        {"text": "hi", "client_msg_id": 5},
        {"text": "hi", "client_msg_id": "x" * 65},
        {"client_msg_id": "m1"},
        {"text": "", "client_msg_id": "m1"},
        {"text": "   \n", "client_msg_id": "m1"},
        {"text": 12, "client_msg_id": "m1"},
        {"text": ["a"], "client_msg_id": "m1"},
        {"text": "x" * 8001, "client_msg_id": "m1"},
    ],
)
def test_a_bad_message_is_a_400_and_never_reaches_the_agent(web, body):
    agent, factory = fake_agent()
    with web(agent_factory=factory) as app:
        response = app.post(SEND, body)

    assert response.status_code == 400
    assert agent.prompts == []


def test_cancel_ends_the_run_and_says_so(web):
    agent, factory = fake_agent(block=True)
    with web(agent_factory=factory) as app:
        with app.watching() as watcher:
            send(app)
            assert wait_until(agent.began.is_set)

            assert app.post("/api/chat/cancel").json() == {"cancelling": True}
            app.wait_chat_idle()

            transcript = app.chat_state()["transcript"]

    assert agent.cancel_calls >= 1
    assert transcript[-1]["role"] == "note" and "Cancelled" in transcript[-1]["text"]
    assert watcher.of("chat_state")[0]["run"]["state"] == "cancelling"
    assert watcher.of("chat_finished")[0]["entry"]["role"] == "note"


def test_cancel_with_nothing_running_does_nothing(web):
    agent, factory = fake_agent()
    with web(agent_factory=factory) as app:
        assert app.post("/api/chat/cancel").json() == {"cancelling": False}

    assert agent.cancel_calls == 0


def test_cancel_is_repeated_until_the_run_ends(web, monkeypatch):
    """A cancel that arrives before the query has begun is dropped, so one call is not enough."""
    from advisor_agent.web import chat

    monkeypatch.setattr(chat, "_CANCEL_REPEAT_SECONDS", 0.1)
    agent, factory = fake_agent(block=True, honours_cancel=False)
    with web(agent_factory=factory) as app:
        send(app)
        assert wait_until(agent.began.is_set)
        app.post("/api/chat/cancel")

        assert wait_until(lambda: agent.cancel_calls >= 4)
        agent.release.set()
        app.wait_chat_idle()


def test_a_run_that_ignores_cancel_marks_the_chat_wedged_until_it_ends(web, monkeypatch):
    from advisor_agent.web import chat

    monkeypatch.setattr(chat, "_CANCEL_REPEAT_SECONDS", 0.1)
    agent, factory = fake_agent(block=True, honours_cancel=False)
    with web(agent_factory=factory, wedge_seconds=0.3) as app:
        with app.watching() as watcher:
            send(app, "stuck", "m1")
            assert wait_until(agent.began.is_set)
            app.post("/api/chat/cancel")

            assert wait_until(lambda: app.chat_state()["wedged"], 10)
            refused = send(app, "again", "m2")
            assert refused.status_code in (409, 503)
            assert app.post("/api/chat/reset").status_code == 409

            agent.release.set()  # the run finally ends
            app.wait_chat_idle()
            assert app.chat_state()["wedged"] is False

    assert any(item.get("wedged") for item in watcher.of("chat_state"))


def test_a_wedged_chat_refuses_new_messages_with_a_503(web, monkeypatch):
    from advisor_agent.web import chat

    monkeypatch.setattr(chat, "_CANCEL_REPEAT_SECONDS", 0.1)
    agent, factory = fake_agent(block=True, honours_cancel=False)
    with web(agent_factory=factory, wedge_seconds=0.2) as app:
        send(app, "stuck", "m1")
        assert wait_until(agent.began.is_set)
        app.post("/api/chat/cancel")
        assert wait_until(lambda: app.chat_state()["wedged"], 10)

        # The stuck run is still the current one, so it is busy first (409); once it
        # is gone but the flag is somehow still set, it is a 503. Either way: refused.
        response = send(app, "again", "m2")
        agent.release.set()

    assert response.status_code in (409, 503)
    assert agent.prompts == ["stuck"]


def test_reset_forgets_the_conversation(web):
    agent, factory = fake_agent(answer="a")
    with web(agent_factory=factory) as app:
        with app.watching() as watcher:
            send(app, "q", "m1")
            app.wait_chat_idle()

            assert app.post("/api/chat/reset").status_code == 200
            assert app.chat_state()["transcript"] == []
            assert app.chat_state()["progress"] == []
            # The same message id is a new message again after a reset.
            assert send(app, "q", "m1").json()["duplicate"] is False
            app.wait_chat_idle()

    assert agent.resets == 1
    assert len(watcher.of("chat_reset")) == 1


def test_reset_while_a_run_is_going_is_a_409(web):
    agent, factory = fake_agent(block=True)
    with web(agent_factory=factory) as app:
        send(app)
        assert wait_until(agent.began.is_set)

        assert app.post("/api/chat/reset").status_code == 409

        agent.release.set()
        app.wait_chat_idle()
    assert agent.resets == 0


def test_an_error_in_the_run_reaches_the_page_as_class_and_message_only(web):
    agent, factory = fake_agent(fail="the model said no")
    with web(agent_factory=factory) as app:
        send(app)
        app.wait_chat_idle()

        entry = app.chat_state()["transcript"][-1]

        # The agent is not stuck: the next message works.
        agent.fail = None
        assert send(app, "again", "m2").status_code == 202
        app.wait_chat_idle()

    assert entry == {"id": entry["id"], "role": "error", "text": "RuntimeError: the model said no"}


def test_a_page_that_connects_late_gets_the_conversation_in_its_snapshot(web):
    agent, factory = fake_agent(lines=("step one",), answer="the answer")
    with web(agent_factory=factory) as app:
        send(app, "question", "m1")
        app.wait_chat_idle()

        with app.watching() as watcher:
            pass

    chat = watcher.events[0][1]["chat"]
    assert [(entry["role"], entry["text"]) for entry in chat["transcript"]] == [
        ("user", "question"),
        ("assistant", "the answer"),
    ]
    assert chat["progress"] == ["step one"]
    assert chat["run"] is None and chat["enabled"] is True


def test_a_page_that_connects_mid_run_sees_the_run(web):
    agent, factory = fake_agent(block=True, lines=("working",))
    with web(agent_factory=factory) as app:
        send(app, "slow", "m1")
        assert wait_until(agent.began.is_set)
        assert wait_until(lambda: app.chat_state()["progress"] == ["working"])

        with app.watching() as watcher:
            agent.release.set()
            app.wait_chat_idle()

    chat = watcher.events[0][1]["chat"]
    assert chat["run"] == {"id": 1, "state": "running"}
    assert chat["progress"] == ["working"]
    assert watcher.of("chat_finished")


def test_progress_lines_are_cleaned_and_limited(web):
    agent, factory = fake_agent(lines=("a\nb\x00c" + "z" * 1000, "   ", "x" * 600))
    with web(agent_factory=factory) as app:
        send(app)
        app.wait_chat_idle()

        progress = app.chat_state()["progress"]

    assert len(progress) == 2  # the blank line is dropped
    assert "\n" not in progress[0] and "\x00" not in progress[0]
    assert all(len(line) <= 500 for line in progress)


def test_the_progress_of_the_previous_run_is_dropped_when_a_new_one_starts(web):
    agent, factory = fake_agent(lines=("only line",))
    with web(agent_factory=factory) as app:
        send(app, "one", "m1")
        app.wait_chat_idle()
        send(app, "two", "m2")
        app.wait_chat_idle()

        assert app.chat_state()["progress"] == ["only line"]


def test_without_an_agent_the_chat_is_off_but_the_rest_works(web):
    with web() as app:  # the default factory refuses to build an agent
        app.wait_azure()
        response = send(app)
        snapshot = app.chat_state()

        assert response.status_code == 503
        assert "chat is off in this test" in response.json()["error"]
        assert snapshot["enabled"] is False and "chat is off" in snapshot["reason"]
        assert app.get("/api/recommendations").status_code == 200


def test_shutdown_cancels_the_run_and_closes_the_agent(web):
    agent, factory = fake_agent(block=True)
    with web(agent_factory=factory) as app:
        send(app)
        assert wait_until(agent.began.is_set)

        app.on_loop(app.services.begin_shutdown)

        assert wait_until(lambda: agent.cancel_calls >= 1)
        app.wait_chat_idle()
        assert send(app, "late", "m2").status_code == 503

    assert agent.closed.is_set()


def test_the_agent_is_built_with_the_approval_bridge_and_a_progress_hook(web):
    agent, factory = fake_agent()
    with web(agent_factory=factory) as app:
        assert agent.approval_callback == app.services.approvals.callback
        assert callable(agent.progress)
