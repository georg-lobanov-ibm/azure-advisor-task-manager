"""The routes that talk to Azure: recommendations, subscriptions, filters, retry."""

import json

import pytest

from conftest import pid_alive, wait_until

SUB = "00000000-0000-0000-0000-000000000001"


def advisor_calls(azure):
    return [json.loads(line) for line in azure.calls() if '"tool": "advisor"' in line]


def test_the_subscriptions_come_from_the_azure_server(web):
    with web() as app:
        app.wait_azure()

        response = app.get("/api/subscriptions")

    assert response.status_code == 200
    assert response.json() == {"subscriptions": [{"id": SUB, "name": "Test subscription"}]}


def test_filters_are_sent_on_to_the_server_and_nothing_else(web, fake_server):
    azure = fake_server("azure")
    with web(azure=azure) as app:
        app.wait_azure()

        response = app.get(
            "/api/recommendations",
            params={"category": "Cost", "impact": "High", "status": "New", "subscription": SUB, "tenant": "x", "top": "5"},
        )

    assert response.status_code == 200
    assert advisor_calls(azure)[0]["arguments"]["parameters"] == {
        "top": 100,
        "category": "Cost",
        "impact": "High",
        "status": "New",
        "subscription": SUB,
    }


@pytest.mark.parametrize(
    "query",
    [
        {"category": "Cost; drop"},
        {"impact": "../etc"},
        {"status": "x" * 80},
        {"subscription": "not-a-guid"},
        {"subscription": SUB + "-extra"},
    ],
)
def test_a_bad_filter_is_a_400_and_never_reaches_the_server(web, fake_server, query):
    azure = fake_server("azure")
    with web(azure=azure) as app:
        app.wait_azure()

        response = app.get("/api/recommendations", params=query)

    assert response.status_code == 400
    assert advisor_calls(azure) == []


def test_the_same_request_is_answered_from_the_cache_until_a_refresh_asks_again(web, fake_server):
    azure = fake_server("azure")
    with web(azure=azure) as app:
        app.wait_azure()

        app.get("/api/recommendations")
        app.get("/api/recommendations")
        assert len(advisor_calls(azure)) == 1

        app.get("/api/recommendations", params={"refresh": "1"})
        assert len(advisor_calls(azure)) == 2


def test_while_the_azure_server_is_starting_the_answer_is_a_503_at_once(web, fake_server):
    azure = fake_server("azure", FAKE_MCP_HANG_START="1")
    options = {"startup_timeout": 60.0, "backoff": (0.2,), "cache_seconds": 30.0}
    with web(azure=azure, azure_options=options) as app:
        recs = app.get("/api/recommendations")
        subs = app.get("/api/subscriptions")
        # The rest of the page works meanwhile.
        session = app.get("/api/session")

    assert recs.status_code == 503 and "starting" in recs.json()["error"]
    # An empty list would read as "this account has no subscriptions".
    assert subs.status_code == 503 and "starting" in subs.json()["error"]
    assert session.status_code == 200
    # Stopping the web server while Azure is still starting does not leave the process behind.
    pid = azure.pid()
    assert pid is not None and wait_until(lambda: not pid_alive(pid))


def test_a_crashed_azure_server_is_reported_and_retry_brings_it_back(web, fake_server):
    azure = fake_server("azure", FAKE_MCP_CRASH_START="1")
    options = {"startup_timeout": 20.0, "backoff": (300.0,), "cache_seconds": 30.0}
    with web(azure=azure, azure_options=options) as app:
        app.wait_azure("failed")
        status = app.services.azure.status
        assert status["retry"] == "auto" and status["reason"]
        assert app.get("/api/recommendations").status_code == 503

        # Make the next start a healthy one, then ask for it.
        app.services.azure._config = fake_server("azure").config
        with app.watching() as watcher:
            assert app.post("/api/azure/retry").json() == {"retrying": True}
            app.wait_azure("ready")

        ok = app.get("/api/recommendations")

    assert ok.status_code == 200 and len(ok.json()["rows"]) == 2
    states = [item["state"] for item in watcher.of("azure_status")]
    assert states[-1] == "ready"


def test_retry_with_a_working_connection_does_nothing(web):
    with web() as app:
        app.wait_azure()

        assert app.post("/api/azure/retry").json() == {"retrying": False}


def test_a_status_change_is_pushed_to_open_pages(web):
    with web() as app:
        with app.watching() as watcher:
            app.wait_azure()
            assert wait_until(lambda: watcher.events == [] or True)  # the frames are read when the block ends

    first = watcher.events[0][1]["azure"]["state"]
    later = [item["state"] for item in watcher.of("azure_status")]
    assert first in ("starting", "ready")
    assert first == "ready" or later[-1] == "ready"
