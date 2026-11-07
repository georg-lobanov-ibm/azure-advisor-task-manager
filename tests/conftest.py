"""Shared fixtures: fake MCP servers and a scripted model, all local."""

import os
import sys
import time
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from advisor_agent.mcp_tools import McpServerConfig

FAKE_SERVER = Path(__file__).parent / "fakes" / "fake_mcp_server.py"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); read its state instead.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def fake_server(tmp_path):
    """Build ``McpServerConfig`` values for the fake server.

    ``make("azure", approval=lambda n: False, FAKE_MCP_HANG_CALL="1")`` returns a
    config whose subprocess sees the given switches. Call log and pid file go
    under ``tmp_path`` and are returned on the config as ``.call_log`` and
    ``.pid_file`` attributes of a small holder.
    """

    class Made:
        def __init__(self, config, call_log, pid_file):
            self.config = config
            self.call_log = call_log
            self.pid_file = pid_file

        def calls(self) -> list[str]:
            return self.call_log.read_text().splitlines() if self.call_log.exists() else []

        def pid(self) -> int | None:
            return int(self.pid_file.read_text()) if self.pid_file.exists() else None

    counter = {"n": 0}

    def make(kind: str, *, name: str | None = None, approval=lambda tool: False, **switches) -> Made:
        counter["n"] += 1
        call_log = tmp_path / f"calls-{kind}-{counter['n']}.log"
        pid_file = tmp_path / f"pid-{kind}-{counter['n']}.txt"
        env = {
            "PATH": os.environ.get("PATH", ""),
            "FAKE_MCP_CALL_LOG": str(call_log),
            "FAKE_MCP_PID_FILE": str(pid_file),
            **{key: str(value) for key, value in switches.items()},
        }
        config = McpServerConfig(
            name=name or ("azure_advisor" if kind == "azure" else "jira"),
            params=StdioServerParameters(
                command=sys.executable, args=[str(FAKE_SERVER), kind], env=env
            ),
            requires_approval=approval,
        )
        return Made(config, call_log, pid_file)

    return make


@pytest.fixture
def web(tmp_path, fake_server):
    """``with web(agent_factory=..., azure=...) as app:`` runs the web UI's app under a test client.

    The Azure side is the fake server unless ``azure`` (a ``fake_server(...)``
    result) says otherwise; without ``agent_factory`` the chat is disabled.
    """
    from fakes.web_harness import running_web

    def disabled_chat(approval_callback, progress):
        raise ValueError("chat is off in this test")

    def start(*, agent_factory=disabled_chat, azure=None, **kwargs):
        azure = azure or fake_server("azure")
        return running_web(tmp_path, agent_factory=agent_factory, azure_config=azure.config, **kwargs)

    return start
