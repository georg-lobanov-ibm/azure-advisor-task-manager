"""AzureDirect against the fake Azure MCP server: start, retry, timeouts, shutdown."""

import asyncio
import json

import pytest

from advisor_agent.web import azure_direct as ad
from advisor_agent.web.azure_direct import AzureDirect, AzureUnavailable
from conftest import pid_alive, wait_until

SUB = "00000000-0000-0000-0000-000000000001"
FAST = dict(backoff=(0.2,), cache_seconds=30.0)


async def until(predicate, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def run(coro):
    return asyncio.run(coro)


def test_start_list_and_close(fake_server):
    azure = fake_server("azure")
    states = []

    async def scenario():
        direct = AzureDirect(azure.config, on_status=lambda s: states.append(s["state"]), **FAST)
        direct.start()
        assert direct.status["state"] == ad.STARTING
        assert await until(lambda: direct.status["state"] == ad.READY)

        result = await direct.list_recommendations({})
        assert [row["category"] for row in result["rows"]] == ["Cost", "Security"]
        assert result["truncated"] is False

        await direct.aclose()

    run(scenario())

    # `starting` is the state from the start, so only the change to `ready` is reported.
    assert states == [ad.READY]
    pid = azure.pid()
    assert pid is not None and wait_until(lambda: not pid_alive(pid))


def test_only_allowlisted_arguments_reach_the_server(fake_server):
    azure = fake_server("azure")

    async def scenario():
        direct = AzureDirect(azure.config, **FAST)
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.READY)
        await direct.list_recommendations({"category": "Cost", "impact": "High", "evil": "1", "tenant": "t"})
        with pytest.raises(ValueError):
            await direct.list_recommendations({"category": "Cost; drop"})
        await direct.aclose()

    run(scenario())

    calls = [json.loads(line) for line in azure.calls()]
    assert len(calls) == 1
    assert calls[0]["arguments"] == {
        "intent": "list recommendations",
        "command": "advisor_recommendation_list",
        "parameters": {"top": 100, "category": "Cost", "impact": "High"},
    }


def test_equal_requests_share_one_call_and_the_cache(fake_server):
    azure = fake_server("azure")

    async def scenario():
        direct = AzureDirect(azure.config, **FAST)
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.READY)

        first = await asyncio.gather(*(direct.list_recommendations({}) for _ in range(5)))
        again = await direct.list_recommendations({})
        assert all(item is first[0] for item in first) and again is first[0]

        await direct.list_recommendations({}, refresh=True)
        await direct.aclose()

    run(scenario())

    assert len(azure.calls()) == 2  # the shared call, then the refresh


def test_calls_before_ready_are_refused_at_once(fake_server):
    azure = fake_server("azure", FAKE_MCP_HANG_START="1")

    async def scenario():
        direct = AzureDirect(azure.config, startup_timeout=60, **FAST)
        direct.start()
        await asyncio.sleep(0.3)
        with pytest.raises(AzureUnavailable, match="starting"):
            await asyncio.wait_for(direct.list_recommendations({}), 2)
        await direct.aclose(timeout=2)

    run(scenario())

    pid = azure.pid()
    assert pid is not None and wait_until(lambda: not pid_alive(pid))


def test_hung_start_fails_then_a_retry_succeeds(fake_server, tmp_path, monkeypatch):
    azure = fake_server("azure", FAKE_MCP_HANG_START="1")

    async def scenario():
        direct = AzureDirect(azure.config, startup_timeout=1.0, backoff=(300.0,), cache_seconds=30.0)
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.FAILED)
        assert "did not finish starting" in direct.status["reason"]
        assert direct.status["retry"] == "auto"
        first_pid = azure.pid()
        assert await until(lambda: not pid_alive(first_pid))

        # The next start is healthy: the environment switch lives in the config.
        direct._config = fake_server("azure").config
        assert direct.retry() is True
        assert await until(lambda: direct.status["state"] == ad.READY)
        assert (await direct.list_recommendations({}))["rows"]
        await direct.aclose()

    run(scenario())


def test_crash_on_start_is_reported_and_retried_by_itself(fake_server):
    azure = fake_server("azure", FAKE_MCP_CRASH_START="1")

    async def scenario():
        direct = AzureDirect(azure.config, startup_timeout=10, backoff=(0.2,), cache_seconds=30.0)
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.FAILED)
        with pytest.raises(AzureUnavailable, match="not available"):
            await direct.list_recommendations({})
        # It keeps trying on its own: the status flips back to starting.
        assert await until(lambda: direct.status["state"] == ad.STARTING, 5)
        await direct.aclose(timeout=5)

    run(scenario())


def test_missing_command_needs_a_manual_retry(fake_server):
    azure = fake_server("azure")
    azure.config.params.command = "/nonexistent/uvx-for-tests"
    azure.config.install_hint  # the attribute exists

    async def scenario():
        direct = AzureDirect(azure.config, startup_timeout=5, backoff=(0.1,), cache_seconds=30.0)
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.FAILED)
        assert direct.status["retry"] == "manual"
        await asyncio.sleep(0.5)  # no automatic retry
        assert direct.status["state"] == ad.FAILED
        assert direct.retry() is True
        await direct.aclose(timeout=5)

    run(scenario())


def test_retry_does_nothing_unless_failed(fake_server):
    azure = fake_server("azure")

    async def scenario():
        direct = AzureDirect(azure.config, **FAST)
        assert direct.retry() is False
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.READY)
        assert direct.retry() is False
        await direct.aclose()

    run(scenario())


def test_slow_calls_time_out_and_two_in_a_row_restart_the_server(fake_server):
    azure = fake_server("azure", FAKE_MCP_HANG_CALL="1")

    async def scenario():
        direct = AzureDirect(
            azure.config, call_timeout=0.5, outer_timeout=1.0, backoff=(0.2,), cache_seconds=0
        )
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.READY)
        first_pid = azure.pid()

        with pytest.raises(AzureUnavailable, match="in time"):
            await direct.list_recommendations({}, refresh=True)
        assert direct.status["state"] == ad.READY  # one timeout is not enough
        with pytest.raises(AzureUnavailable, match="in time"):
            await direct.list_recommendations({}, refresh=True)

        # Two in a row: the server is restarted, and the old process is gone.
        assert await until(lambda: azure.pid() != first_pid or direct.status["state"] != ad.READY, 10)
        assert await until(lambda: not pid_alive(first_pid), 10)
        await direct.aclose(timeout=5)

    run(scenario())


def test_server_error_text_is_reported_without_a_restart(fake_server):
    azure = fake_server("azure")

    async def scenario():
        direct = AzureDirect(azure.config, **FAST)
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.READY)
        with pytest.raises(AzureUnavailable, match="unknown command"):
            await direct._call("advisor", {"intent": "x", "command": "no_such_command"})
        assert direct.status["state"] == ad.READY
        await direct.aclose()

    run(scenario())


def test_close_while_a_call_is_running_returns_and_frees_the_process(fake_server):
    azure = fake_server("azure", FAKE_MCP_HANG_CALL="1")

    async def scenario():
        direct = AzureDirect(azure.config, call_timeout=30, outer_timeout=35, **FAST)
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.READY)
        pending = asyncio.create_task(direct.list_recommendations({}))
        assert await until(lambda: len(azure.calls()) == 1)

        await asyncio.wait_for(direct.aclose(timeout=10), 20)

        with pytest.raises((AzureUnavailable, asyncio.CancelledError)):
            await pending

    run(scenario())

    pid = azure.pid()
    assert pid is not None and wait_until(lambda: not pid_alive(pid))


def test_close_before_start_and_twice_is_harmless(fake_server):
    azure = fake_server("azure")

    async def scenario():
        direct = AzureDirect(azure.config, **FAST)
        await direct.aclose()
        await direct.aclose()

    run(scenario())
    assert azure.pid() is None


def test_subscriptions_are_listed_once(fake_server):
    azure = fake_server("azure")

    async def scenario():
        direct = AzureDirect(azure.config, **FAST)
        direct.start()
        assert await until(lambda: direct.status["state"] == ad.READY)
        first = await direct.list_subscriptions()
        assert first == [{"id": SUB, "name": "Test subscription"}]
        assert await direct.list_subscriptions() is first
        await direct.aclose()

    run(scenario())

    assert sum("subscription_list" in line for line in azure.calls()) == 1
