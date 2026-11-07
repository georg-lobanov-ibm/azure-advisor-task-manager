"""Errors that carry an HTTP status, and the text that may be shown for any other error."""

from __future__ import annotations

import re

MAX_ERROR_CHARS = 300

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


class ApiError(Exception):
    """A request that is refused. ``message`` is safe to show in the browser."""

    def __init__(self, status: int, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def clean_line(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    """``text`` on one line, without control characters, at most ``limit`` characters."""
    text = _CONTROL.sub(" ", text).strip()
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def safe_error(exc: BaseException) -> str:
    """What the browser may be told about ``exc``: its class and a short message.

    The full detail (traceback, any secret in it) goes to the server log only.
    """
    detail = clean_line(str(exc))
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
