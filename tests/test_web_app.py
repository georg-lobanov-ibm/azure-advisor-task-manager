"""The web UI's app under a test client: pages, sign-in, the event stream, recommendations."""

from fakes.web_harness import TOKEN, fake_agent


def test_the_app_starts_and_serves_the_page(web):
    with web() as app:
        page = app.get("/")

        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert '<script src="/static/app.js" defer>' in page.text


def test_login_with_the_token_sets_a_session_cookie(web):
    with web(signed_in=False) as app:
        assert app.get("/api/session").status_code == 401

        wrong = app.post("/api/login", {"token": "nope"})
        assert wrong.status_code == 401
        assert app.get("/api/session").status_code == 401

        ok = app.post("/api/login", {"token": TOKEN})
        assert ok.status_code == 200
        cookie = ok.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
        assert app.get("/api/session").status_code == 200


def test_recommendations_come_from_the_fake_azure_server(web):
    with web() as app:
        app.wait_azure()

        response = app.get("/api/recommendations")

        assert response.status_code == 200
        body = response.json()
        assert [row["resource_name"] for row in body["rows"]] == ["vm-a", "sa-b"]
        assert body["truncated"] is False
        assert all(row["link"] is None for row in body["rows"])


def test_the_event_stream_starts_with_a_snapshot(web):
    agent, factory = fake_agent()
    with web(agent_factory=factory) as app:
        app.wait_azure()
        with app.watching() as watcher:
            pass

        assert watcher.status == 200
        assert watcher.headers["content-type"].startswith("text/event-stream")
        first_name, first = watcher.events[0]
        assert first_name == "snapshot"
        assert first["chat"]["enabled"] is True
        assert first["approvals"] == []
        assert first["azure"]["state"] == "ready"
        assert watcher.names()[-1] == "shutdown"
