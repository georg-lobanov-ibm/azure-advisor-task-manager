"""Settings of one web server run, and the few validators that guard what reaches Jira links."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# A Jira issue key: project key, dash, number without leading zero. Used with
# ``fullmatch`` (``$`` would also accept a trailing newline).
JIRA_KEY = re.compile(r"[A-Z][A-Z0-9_]+-[1-9][0-9]*")
JIRA_KEY_MAX_LENGTH = 60

SESSION_COOKIE = "advisor_session"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


@dataclass
class Settings:
    """What the middleware and the routes need to know about this run.

    ``port`` is read at request time, so a test that starts the server on port
    0 can set the real port once it is bound.
    """

    token: str
    port: int
    # The value of the session cookie. The launch token is only exchanged for it.
    session_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # Base URL of the Jira site (``JIRA_URL``), already checked by ``jira_base_url``.
    jira_base_url: str | None = None
    # How long a Jira write may wait for a click, and how long a page reload may take.
    approval_timeout: float = 300.0
    approval_viewer_grace: float = 30.0
    # A cancelled chat run that has not ended after this long marks the chat as wedged.
    wedge_seconds: float = 20.0
    agent_close_timeout: float = 40.0

    def allowed_origins(self) -> frozenset[str]:
        return frozenset(f"http://{host}:{self.port}" for host in LOOPBACK_HOSTS)


def valid_jira_key(value: object) -> bool:
    return isinstance(value, str) and len(value) <= JIRA_KEY_MAX_LENGTH and JIRA_KEY.fullmatch(value) is not None


def jira_base_url(value: str | None) -> str | None:
    """``value`` as a Jira base URL (no trailing slash), or None if it is not a plain http(s) URL."""
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port  # raises for a bad port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"


def jira_issue_url(base_url: str | None, key: object) -> str | None:
    """The browse URL of ``key``, built only from the checked base URL and the checked key."""
    if base_url is None or not valid_jira_key(key):
        return None
    return f"{base_url}/browse/{key}"
