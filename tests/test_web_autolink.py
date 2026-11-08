"""A Story the agent creates for the attached recommendation is linked to it.

Most tests run the real agent against the fake Jira server, with a scripted
model and the approval dialog; the last ones call the hook the chat gives the
agent directly, for the cases a scripted run cannot reach.
"""

import json

from advisor_agent.mcp_tools import ApprovalRequest
from conftest import wait_until
from fakes.anthropic_mock import ScriptedModel, text, tool_use
from fakes.web_harness import fake_agent
from test_web_approvals import WRITE_ARGS, decide, jira_agent_factory, written
from test_web_chat import ROW, send_with

CREATE = "jira_create_issue"
STORY = {**WRITE_ARGS, "issue_type": "Story"}
TASK = {**WRITE_ARGS, "issue_type": "Task"}
RECOMMENDATION = {"recommendation": ROW}
LINK_BODY = {"recommendation_type_id": "type-1", "resource_id": ROW["resource_id"]}


def notes(app):
    return [entry["text"] for entry in app.chat_state()["transcript"] if entry["role"] == "note"]


def wait_for_note(app, containing, timeout=20.0):
    assert wait_until(lambda: any(containing in note for note in notes(app)), timeout), notes(app)
    return next(note for note in notes(app) if containing in note)


def approve(app, count=1):
    """Approve the next ``count`` requests, one after the other."""
    seen = []
    for _ in range(count):
        assert wait_until(
            lambda: any(item["id"] not in seen for item in app.pending_approvals()), 60
        ), "no approval was requested"
        pending = next(item for item in app.pending_approvals() if item["id"] not in seen)
        seen.append(pending["id"])
        assert decide(app, pending["id"], "approve").status_code == 200


def mapping(app, key=None):
    """The stored link of the attached recommendation (or of ``key``)."""
    if key is not None:
        return app.db.get_mapping_by_jira_key(key)
    return app.db.get_mapping("type-1", ROW["resource_id"])


def settle(app, timeout=10.0):
    """Wait until no link is being written. The Story's call is handled before its run ends, so this is final."""
    assert wait_until(lambda: app.on_loop(lambda: not app.services.chat._link_tasks), timeout)


def with_app(web, fake_server, tmp_path, steps, check, *, context=RECOMMENDATION, approvals=1, before=None, **switches):
    """One message with ``steps`` for the model, approved ``approvals`` times; then ``check(app, jira, watcher)``."""
    jira = fake_server("jira", approval=lambda tool: tool == CREATE, **switches)
    model = ScriptedModel(steps)
    with web(agent_factory=jira_agent_factory(model, jira, tmp_path)) as app:
        if before is not None:
            before(app)
        with app.watching() as watcher:
            assert send_with(app, context).status_code == 202
            approve(app, approvals)
            app.wait_chat_idle(60)
            settle(app)
        check(app, jira, watcher)


# -- the link is made ---------------------------------------------------------------------


def test_an_approved_story_is_linked_to_the_attached_recommendation(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        assert mapping(app)["jira_issue_key"] == "TEST-1"
        assert wait_for_note(app, "Linked TEST-1")
        rows = app.audit("auto_link")
        assert len(rows) == 1
        assert rows[0]["jira_key"] == "TEST-1" and rows[0]["decision"] == "link"
        assert json.loads(rows[0]["metadata"]) == {"source": "web_ui_agent"}
        assert app.audit("manual_link") == []

        changed = watcher.of("link_changed")
        assert len(changed) == 1 and changed[0]["removed"] is False
        assert changed[0]["link"]["jira_key"] == "TEST-1"
        assert changed[0]["link"]["recommendation_type_id"] == "type-1"
        assert [entry["entry"]["text"] for entry in watcher.of("chat_entry")] == [
            "Linked TEST-1 to the attached recommendation."
        ]

        links = app.get("/api/links").json()["links"]
        assert [link["jira_key"] for link in links] == ["TEST-1"]
        assert links[0]["category"] == "Cost" and links[0]["impact"] == "High"
        assert len(written(jira)) == 1

    with_app(web, fake_server, tmp_path, [tool_use(CREATE, STORY), text("created")], check)


def test_the_issue_type_is_compared_without_regard_to_case(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        assert mapping(app)["jira_issue_key"] == "TEST-1"

    steps = [tool_use(CREATE, {**WRITE_ARGS, "issue_type": " story "}), text("created")]
    with_app(web, fake_server, tmp_path, steps, check)


def test_the_story_is_linked_and_its_tasks_are_not(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        assert mapping(app)["jira_issue_key"] == "TEST-1"
        assert len(app.db.get_all_mappings()) == 1
        assert len(app.audit("auto_link")) == 1
        assert not any("TEST-2" in note or "TEST-3" in note for note in notes(app))
        assert len(written(jira)) == 3

    steps = [tool_use(CREATE, STORY), tool_use(CREATE, TASK), tool_use(CREATE, TASK), text("done")]
    with_app(web, fake_server, tmp_path, steps, check, approvals=3)


def test_a_task_before_the_story_does_not_use_up_the_link(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        assert mapping(app)["jira_issue_key"] == "TEST-2"

    steps = [tool_use(CREATE, TASK), tool_use(CREATE, STORY), text("done")]
    with_app(web, fake_server, tmp_path, steps, check, approvals=2)


# -- nothing to link -----------------------------------------------------------------------


def test_a_task_is_not_linked_and_leaves_no_note(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        assert len(written(jira)) == 1
        assert app.db.get_all_mappings() == []
        assert notes(app) == []
        assert watcher.of("link_changed") == []

    with_app(web, fake_server, tmp_path, [tool_use(CREATE, TASK), text("created")], check)


def test_a_story_without_an_issue_type_is_not_linked(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        assert app.db.get_all_mappings() == []
        assert notes(app) == []

    steps = [tool_use(CREATE, WRITE_ARGS), text("created")]  # the server's default type is a Task
    with_app(web, fake_server, tmp_path, steps, check)


def test_a_denied_story_is_not_created_and_not_linked(web, fake_server, tmp_path):
    jira = fake_server("jira", approval=lambda tool: tool == CREATE)
    model = ScriptedModel([tool_use(CREATE, STORY), text("not created")])
    with web(agent_factory=jira_agent_factory(model, jira, tmp_path)) as app:
        with app.watching() as watcher:
            send_with(app, RECOMMENDATION)
            pending = app.wait_approval(60)
            assert decide(app, pending["id"], "deny").status_code == 200
            app.wait_chat_idle(60)

        assert written(jira) == []
        assert app.db.get_all_mappings() == []
        assert notes(app) == []
        assert watcher.of("link_changed") == []


def test_a_story_is_not_linked_when_no_recommendation_is_attached(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        assert len(written(jira)) == 1
        assert app.db.get_all_mappings() == []
        assert notes(app) == []

    with_app(web, fake_server, tmp_path, [tool_use(CREATE, STORY), text("created")], check, context=None)


# -- links that are already there ----------------------------------------------------------------


def test_an_existing_link_is_kept_and_the_note_names_it(web, fake_server, tmp_path):
    def before(app):
        assert app.send("PUT", "/api/links", {**LINK_BODY, "jira_key": "ABC-1"}).status_code == 200

    def check(app, jira, watcher):
        note = wait_for_note(app, "TEST-1 was created but not linked")
        assert "already linked to ABC-1" in note
        assert mapping(app)["jira_issue_key"] == "ABC-1"
        assert mapping(app, "TEST-1") is None
        assert app.audit("auto_link") == []
        assert len(app.audit("manual_link")) == 1
        assert watcher.of("link_changed") == []
        assert [e["entry"]["text"] for e in watcher.of("chat_entry")] == [note]

    with_app(web, fake_server, tmp_path, [tool_use(CREATE, STORY), text("created")], check, before=before)


def test_a_key_that_is_linked_to_another_finding_is_not_linked_again(web, fake_server, tmp_path):
    def before(app):
        other = {"recommendation_type_id": "type-2", "resource_id": "/other", "jira_key": "TEST-1"}
        assert app.send("PUT", "/api/links", other).status_code == 200

    def check(app, jira, watcher):
        note = wait_for_note(app, "TEST-1 was created but not linked")
        assert "already linked to another finding" in note
        assert mapping(app) is None
        assert app.audit("auto_link") == []

    with_app(web, fake_server, tmp_path, [tool_use(CREATE, STORY), text("created")], check, before=before)


def test_only_the_first_story_of_a_message_is_linked(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        note = wait_for_note(app, "TEST-2 was created but not linked")
        assert "only the first Story" in note
        assert [m["jira_issue_key"] for m in app.db.get_all_mappings()] == ["TEST-1"]
        assert len(app.audit("auto_link")) == 1
        assert len(watcher.of("link_changed")) == 1

    steps = [tool_use(CREATE, STORY), tool_use(CREATE, {**STORY, "summary": "again"}), text("done")]
    with_app(web, fake_server, tmp_path, steps, check, approvals=2)


def test_the_next_message_may_link_its_own_story(web, fake_server, tmp_path):
    jira = fake_server("jira", approval=lambda tool: tool == CREATE)
    model = ScriptedModel([tool_use(CREATE, STORY), text("one"), tool_use(CREATE, STORY), text("two")])
    other = {"recommendation": {**ROW, "recommendation_type_id": "type-2"}}
    with web(agent_factory=jira_agent_factory(model, jira, tmp_path)) as app:
        with app.watching():
            send_with(app, RECOMMENDATION, msg_id="m1")
            approve(app)
            app.wait_chat_idle(60)
            wait_for_note(app, "Linked TEST-1")

            send_with(app, other, msg_id="m2")
            approve(app)
            app.wait_chat_idle(60)
            wait_for_note(app, "Linked TEST-2")

        keys = {m["recommendation_type_id"]: m["jira_issue_key"] for m in app.db.get_all_mappings()}
        assert keys == {"type-1": "TEST-1", "type-2": "TEST-2"}


# -- what cannot be linked -------------------------------------------------------------------------


def test_an_answer_without_a_readable_key_is_not_linked(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        note = wait_for_note(app, "could not be read")
        assert "Link it by hand" in note
        assert app.db.get_all_mappings() == []
        assert len(written(jira)) == 1
        answers = [e["text"] for e in app.chat_state()["transcript"] if e["role"] == "assistant"]
        assert answers == ["created"]

    with_app(
        web,
        fake_server,
        tmp_path,
        [tool_use(CREATE, STORY), text("created")],
        check,
        FAKE_JIRA_CREATE_GARBLED=1,
    )


def test_a_recommendation_without_a_type_id_cannot_be_linked(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        note = wait_for_note(app, "TEST-1 was created but not linked")
        assert "cannot be linked" in note
        assert app.db.get_all_mappings() == []

    context = {"recommendation": {**ROW, "recommendation_type_id": ""}}
    with_app(web, fake_server, tmp_path, [tool_use(CREATE, STORY), text("created")], check, context=context)


def test_a_resource_id_that_is_too_long_is_refused_and_not_clipped(web, fake_server, tmp_path):
    def check(app, jira, watcher):
        assert "cannot be linked" in wait_for_note(app, "TEST-1 was created but not linked")
        assert app.db.get_all_mappings() == []

    context = {"recommendation": {**ROW, "resource_id": "/x" * 600}}
    with_app(web, fake_server, tmp_path, [tool_use(CREATE, STORY), text("created")], check, context=context)


def test_a_link_whose_audit_record_cannot_be_written_is_not_kept(web, fake_server, tmp_path, monkeypatch):
    def before(app):
        real = app.db.log_audit

        def log_audit(action, *args, **kwargs):
            if action == "auto_link":
                raise RuntimeError("disk full")
            return real(action, *args, **kwargs)

        monkeypatch.setattr(app.db, "log_audit", log_audit)

    def check(app, jira, watcher):
        note = wait_for_note(app, "could not be linked")
        assert "TEST-1" in note and "Link it by hand" in note
        assert "disk full" not in note  # the detail goes to the log, not to the page
        assert app.db.get_all_mappings() == []
        assert watcher.of("link_changed") == []
        answers = [e["text"] for e in app.chat_state()["transcript"] if e["role"] == "assistant"]
        assert answers == ["created"]

    with_app(web, fake_server, tmp_path, [tool_use(CREATE, STORY), text("created")], check, before=before)


def test_the_note_is_in_the_transcript_of_a_page_that_connects_later(web, fake_server, tmp_path):
    jira = fake_server("jira", approval=lambda tool: tool == CREATE)
    model = ScriptedModel([tool_use(CREATE, STORY), text("created")])
    with web(agent_factory=jira_agent_factory(model, jira, tmp_path)) as app:
        send_with(app, RECOMMENDATION)
        approve(app)
        app.wait_chat_idle(60)
        wait_for_note(app, "Linked TEST-1")

        with app.watching() as later:
            pass

    snapshot = later.events[0][1]["chat"]["transcript"]
    assert [e["text"] for e in snapshot if e["role"] == "note"] == [
        "Linked TEST-1 to the attached recommendation."
    ]


# -- the hook the chat gives the agent --------------------------------------------------------------


def story_answer(key):
    return json.dumps({"message": "Issue created successfully", "issue": {"key": key}})


def call_hook(app, agent, server, tool, arguments, result):
    assert agent.began.wait(10)
    agent.on_tool_result(ApprovalRequest(server, tool, arguments), result)
    app.on_loop(lambda: None)  # let what the hook queued run


def test_only_a_jira_create_issue_call_is_looked_at(web):
    agent, factory = fake_agent(block=True)
    with web(agent_factory=factory) as app:
        send_with(app, RECOMMENDATION)
        for server, tool, arguments in (
            ("azure_advisor", CREATE, STORY),
            ("jira", "jira_update_issue", STORY),
            ("jira", CREATE, {"issue_type": 5}),
            ("jira", CREATE, {}),
        ):
            call_hook(app, agent, server, tool, arguments, story_answer("TEST-9"))
        assert notes(app) == []
        assert app.db.get_all_mappings() == []

        call_hook(app, agent, "jira", CREATE, STORY, [{"type": "text", "text": story_answer("TEST-9")}])
        wait_for_note(app, "Linked TEST-9")
        assert mapping(app)["jira_issue_key"] == "TEST-9"
        agent.release.set()
        app.wait_chat_idle()


def test_a_key_that_is_not_a_jira_key_is_not_linked(web):
    agent, factory = fake_agent(block=True)
    with web(agent_factory=factory) as app:
        send_with(app, RECOMMENDATION)
        call_hook(app, agent, "jira", CREATE, STORY, story_answer("../../etc"))
        wait_for_note(app, "could not be read")
        assert app.db.get_all_mappings() == []
        agent.release.set()
        app.wait_chat_idle()


def test_a_story_created_after_the_run_ended_is_ignored(web):
    agent, factory = fake_agent()
    with web(agent_factory=factory) as app:
        send_with(app, RECOMMENDATION)
        app.wait_chat_idle()
        agent.on_tool_result(ApprovalRequest("jira", CREATE, STORY), story_answer("TEST-9"))
        app.on_loop(lambda: None)
        assert app.db.get_all_mappings() == []
        assert notes(app) == []


def test_a_link_finished_after_a_reset_leaves_no_note(web):
    agent, factory = fake_agent(block=True)
    with web(agent_factory=factory) as app:
        send_with(app, RECOMMENDATION)
        assert agent.began.wait(10)
        chat = app.services.chat
        # The link is written, but the conversation is forgotten before its note is made
        # (a reset bumps the counter; the real one is refused while a run is going).
        original = app.services.storage.run

        async def run_then_reset(*args, **kwargs):
            result = await original(*args, **kwargs)
            chat._generation += 1
            return result

        app.services.storage.run = run_then_reset
        agent.on_tool_result(ApprovalRequest("jira", CREATE, STORY), story_answer("TEST-9"))
        assert wait_until(lambda: mapping(app) is not None, 10)
        app.on_loop(lambda: None)
        assert notes(app) == []
        agent.release.set()
        app.wait_chat_idle()
