"""Server-sent events: one stream per open browser tab, fed by ``publish``.

Runs on the web server's event loop only. A new stream starts with a snapshot
of everything the page shows, so a reload or a dropped connection needs no
replay: the page reconnects and rebuilds itself from the snapshot.

A subscriber that cannot keep up (its queue is full) is cut off instead of
slowing the others down; its browser reconnects and gets a fresh snapshot.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

HEARTBEAT_SECONDS = 15.0
QUEUE_SIZE = 256

_END = object()


def frame(event: str, data: Any) -> str:
    """One SSE message. ``json.dumps`` puts no raw newline in the data line."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class Subscription:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue(QUEUE_SIZE)


class EventHub:
    def __init__(self, *, heartbeat: float = HEARTBEAT_SECONDS) -> None:
        self._heartbeat = heartbeat
        self._subscribers: set[Subscription] = set()
        self._closed = False
        self._empty_since = time.monotonic()

    @property
    def viewer_count(self) -> int:
        return len(self._subscribers)

    def seconds_without_viewer(self) -> float:
        """0 while a page is connected, else how long none has been. Safe to read from any thread."""
        if self._subscribers:
            return 0.0
        return time.monotonic() - self._empty_since

    def subscribe(self, snapshot: dict[str, Any]) -> Subscription | None:
        """Start a stream with ``snapshot`` as its first message. None once the hub is closed.

        The caller builds the snapshot and subscribes without awaiting in
        between, so no event can fall into the gap.
        """
        if self._closed:
            return None
        subscription = Subscription()
        subscription.queue.put_nowait(frame("snapshot", snapshot))
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscribers:
            self._subscribers.discard(subscription)
            if not self._subscribers:
                self._empty_since = time.monotonic()

    def publish(self, event: str, data: Any) -> None:
        if self._closed:
            return
        message = frame(event, data)
        for subscription in list(self._subscribers):
            try:
                subscription.queue.put_nowait(message)
            except asyncio.QueueFull:
                self._cut_off(subscription)

    def _cut_off(self, subscription: Subscription, last: str | None = None) -> None:
        self.unsubscribe(subscription)
        while not subscription.queue.empty():
            subscription.queue.get_nowait()
        if last is not None:
            subscription.queue.put_nowait(last)
        subscription.queue.put_nowait(_END)

    def close_all(self) -> None:
        """End every stream (after a last ``shutdown`` message) and refuse new ones."""
        self._closed = True
        for subscription in list(self._subscribers):
            self._cut_off(subscription, frame("shutdown", {}))

    async def stream(self, subscription: Subscription) -> AsyncIterator[str]:
        """The SSE body: messages as they come, a comment line when quiet."""
        while True:
            try:
                item = await asyncio.wait_for(subscription.queue.get(), self._heartbeat)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is _END:
                return
            yield item
