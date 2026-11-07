"""SQLite access for the web server: one worker thread, awaited from any event loop.

Both the UI loop (links) and the agent's loop (approval audit rows) write, and
sqlite3 calls block. Every call therefore goes through one single-thread
executor, so the loops never wait on the disk and writes happen one at a time.
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from ..database import AdvisorDatabase

T = TypeVar("T")


class Storage:
    def __init__(self, db: AdvisorDatabase):
        self.db = db
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="advisor-db")

    async def run(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Run ``fn(*args, **kwargs)`` on the database thread and await the result."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args, **kwargs))

    def close(self) -> None:
        """Stop accepting work. Does not wait for a call that is still running."""
        self._executor.shutdown(wait=False, cancel_futures=True)
