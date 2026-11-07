"""The event stream: what a new page is sent first, who is cut off, and how it all ends."""

import asyncio
import json

from advisor_agent.web import events
from advisor_agent.web.events import EventHub, frame
from conftest import wait_until


def run(coro):
    return asyncio.run(coro)


async def drain(hub, subscription, count):
    """The next ``count`` messages of a stream."""
    out = []
    stream = hub.stream(subscription)
    async for item in stream:
        out.append(item)
        if len(out) == count:
            break
    await stream.aclose()
    return out


def test_a_frame_is_one_event_with_its_data_on_one_line():
    text = frame("chat_progress", {"line": "first\nsecond", "n": 1})

    assert text.startswith("event: chat_progress\ndata: ")
    assert text.endswith("\n\n")
    data_lines = [line for line in text.splitlines() if line.startswith("data: ")]
    assert len(data_lines) == 1  # a newline in the value is escaped, not sent raw
    assert json.loads(data_lines[0][len("data: ") :]) == {"line": "first\nsecond", "n": 1}


def test_a_new_stream_starts_with_the_snapshot_then_gets_what_is_published():
    async def scenario():
        hub = EventHub()
        subscription = hub.subscribe({"hello": "world"})
        hub.publish("link_changed", {"n": 1})
        return await drain(hub, subscription, 2)

    first, second = run(scenario())

    assert first == frame("snapshot", {"hello": "world"})
    assert second == frame("link_changed", {"n": 1})


def test_every_open_stream_gets_every_event():
    async def scenario():
        hub = EventHub()
        one, two = hub.subscribe({}), hub.subscribe({})
        hub.publish("chat_reset", {})
        return await drain(hub, one, 2), await drain(hub, two, 2)

    one, two = run(scenario())

    assert one == two and one[1] == frame("chat_reset", {})


def test_a_stream_that_falls_behind_is_cut_off_and_the_others_carry_on():
    async def scenario():
        hub = EventHub()
        slow, quick = hub.subscribe({}), hub.subscribe({})
        received = []

        async def keep_up():
            stream = hub.stream(quick)
            async for item in stream:
                received.append(item)

        reader = asyncio.create_task(keep_up())
        for number in range(events.QUEUE_SIZE + 10):  # nobody reads `slow`
            hub.publish("chat_progress", {"n": number})
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        viewers_after = hub.viewer_count
        slow_items = await drain(hub, slow, 1)  # ends at once: what it missed was thrown away
        hub.publish("chat_reset", {})
        await asyncio.sleep(0.05)
        hub.close_all()
        await asyncio.wait_for(reader, 2)
        return viewers_after, slow_items, received

    viewers_after, slow_items, received = run(scenario())

    assert viewers_after == 1
    assert slow_items == []
    assert frame("chat_progress", {"n": events.QUEUE_SIZE + 9}) in received  # the other kept up
    assert frame("chat_reset", {}) in received
    assert received[-1] == frame("shutdown", {})


def test_close_all_sends_shutdown_to_each_stream_and_then_ends_it():
    async def scenario():
        hub = EventHub()
        subscription = hub.subscribe({})
        hub.publish("chat_reset", {})
        hub.close_all()
        items = [item async for item in hub.stream(subscription)]
        return hub, items

    hub, items = run(scenario())

    # Frames that had not been sent yet are dropped; the last word is `shutdown`.
    assert items == [frame("shutdown", {})]
    assert hub.viewer_count == 0


def test_after_close_all_nobody_can_subscribe_and_publishing_does_nothing():
    hub = EventHub()
    hub.close_all()

    assert hub.subscribe({}) is None
    hub.publish("chat_reset", {})  # does not raise


def test_a_quiet_stream_sends_a_comment_line_so_the_connection_stays_open():
    async def scenario():
        hub = EventHub(heartbeat=0.05)
        subscription = hub.subscribe({})
        return await drain(hub, subscription, 3)

    items = run(scenario())

    assert items[1:] == [": ping\n\n", ": ping\n\n"]


def test_the_hub_knows_how_long_no_page_has_been_open():
    async def scenario():
        hub = EventHub()
        await asyncio.sleep(0.2)
        waited = hub.seconds_without_viewer()
        subscription = hub.subscribe({})
        open_now = hub.seconds_without_viewer()
        hub.unsubscribe(subscription)
        hub.unsubscribe(subscription)  # twice is harmless
        await asyncio.sleep(0.05)
        return waited, open_now, hub.seconds_without_viewer()

    waited, open_now, after = run(scenario())

    assert waited >= 0.2
    assert open_now == 0.0
    assert 0.0 < after < waited  # the clock restarted when the page went away


# -- through the app ----------------------------------------------------------------------


def test_a_page_that_goes_away_is_forgotten(web):
    with web() as app:
        with app.watching():
            assert app.services.hub.viewer_count == 1

        assert wait_until(lambda: app.services.hub.viewer_count == 0)


def test_the_stream_is_refused_once_the_server_is_shutting_down(web):
    with web() as app:
        app.on_loop(app.services.begin_shutdown)

        response = app.get("/api/events")

    assert response.status_code == 503
    assert "shutting down" in response.json()["error"]
