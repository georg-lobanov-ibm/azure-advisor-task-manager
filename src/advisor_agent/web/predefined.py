"""The predefined chats: starter prompts the person picks in the chat panel.

They are written by hand in ``chats.toml`` at the repository root and read again
on every request, so an edit shows after a page reload. This module never
raises: a file that cannot be used gives no chats and a *problem*, a line the
page shows, and a single bad entry is skipped while the others still load.

A problem names the file (not its path) and may repeat the parser's message,
which is the person's own text. Nothing else from the file reaches an error.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

from .chat import MAX_PROMPT_CHARS
from .errors import clean_line

log = logging.getLogger("advisor_agent.web")

MAX_FILE_BYTES = 256 * 1024
MAX_CHATS = 50
MAX_PROBLEMS = 5
MAX_TITLE_CHARS = 80


def load_predefined_chats(path: Path | None) -> dict[str, Any]:
    """``{"chats": [{"title", "prompt"}, ...], "problems": [str, ...]}`` for the file at ``path``.

    ``None`` (the feature is off) and a missing file give no chats and no problem.
    """
    if path is None:
        return {"chats": [], "problems": []}

    name = path.name
    problems: list[str] = []
    chats: list[dict[str, str]] = []
    try:
        data = _read_toml(path)
    except FileNotFoundError:
        return {"chats": [], "problems": []}
    except _Unusable as exc:
        return {"chats": [], "problems": [f"{name}: {exc}"]}

    entries = data.get("chat")
    if entries is None:
        problems.append(f"{name}: no [[chat]] entries found.")
    elif not isinstance(entries, list):
        problems.append(f"{name}: 'chat' must be written as [[chat]], once per chat.")
    else:
        for number, entry in enumerate(entries, start=1):
            if len(chats) >= MAX_CHATS:
                problems.append(f"{name}: only the first {MAX_CHATS} chats are used.")
                break
            chat = _check_entry(entry, number, name, problems)
            if chat is not None:
                chats.append(chat)

    if len(problems) > MAX_PROBLEMS:
        hidden = len(problems) - (MAX_PROBLEMS - 1)
        problems = problems[: MAX_PROBLEMS - 1] + [f"{name}: and {hidden} more problems."]
    for problem in problems:
        log.warning("Predefined chats: %s", problem)
    return {"chats": chats, "problems": problems}


class _Unusable(Exception):
    """The file cannot be used at all. The message is safe to show."""


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _Unusable(f"cannot be read ({type(exc).__name__}).") from None
    if len(raw) > MAX_FILE_BYTES:
        raise _Unusable(f"is too large (more than {MAX_FILE_BYTES // 1024} KB).")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unusable("is not UTF-8 text.") from None
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        # The parser's message names the line and column, which is what the person needs.
        raise _Unusable(f"is not valid TOML: {clean_line(str(exc), 200)}") from None


def _check_entry(entry: Any, number: int, name: str, problems: list[str]) -> dict[str, str] | None:
    """The chat for one ``[[chat]]`` table, or None (with a problem added) if it cannot be used."""
    if not isinstance(entry, dict):
        problems.append(f"{name}: chat {number} is not a [[chat]] table.")
        return None

    title = entry.get("title")
    prompt = entry.get("prompt")
    if not isinstance(title, str) or not clean_line(title):
        problems.append(f"{name}: chat {number} has no title.")
        return None
    if not isinstance(prompt, str) or not prompt.strip():
        problems.append(f"{name}: chat {number} has no prompt.")
        return None

    prompt = prompt.replace("\r\n", "\n").strip()
    if len(prompt) > MAX_PROMPT_CHARS:
        problems.append(f"{name}: chat {number} has a prompt longer than {MAX_PROMPT_CHARS} characters.")
        return None
    return {"title": clean_line(title, MAX_TITLE_CHARS), "prompt": prompt}
