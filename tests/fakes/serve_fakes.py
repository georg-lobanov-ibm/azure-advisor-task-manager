"""The real web server wired to fake backends, for the lifecycle tests.

Imported by the tests (``backends``, ``agent_factory``) and also run as a
script, so that a test can start the server as a separate process and stop it
with a real ``SIGINT`` the way Ctrl-C does:

    python serve_fakes.py DIRECTORY PORT [hang-azure-start]

The chat agent is the real ``AdvisorAgent``; its model is a script that asks to
create a Jira issue (which needs a click) and its Jira server is the fake one.
Everything the fake servers do is recorded under ``DIRECTORY``.
"""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
if __name__ == "__main__":  # started as a script: make the same imports work as under pytest
    sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "src")]

from mcp import StdioServerParameters  # noqa: E402

from advisor_agent.agent import AdvisorAgent  # noqa: E402
from advisor_agent.mcp_tools import McpServerConfig  # noqa: E402
from advisor_agent.web.__main__ import build_server, run_server  # noqa: E402
from advisor_agent.web.settings import Settings  # noqa: E402
from fakes.anthropic_mock import ScriptedModel, text, tool_use  # noqa: E402

FAKE_SERVER = HERE / "fake_mcp_server.py"
WRITE_ARGS = {"project_key": "T", "summary": "from the lifecycle test"}
WRITE_TOOL = "jira_create_issue"


@dataclass
class Backends:
    directory: Path
    azure: McpServerConfig
    jira: McpServerConfig

    def pid(self, kind: str) -> int | None:
        path = self.directory / f"{kind}.pid"
        return int(path.read_text()) if path.exists() else None

    def calls(self, kind: str) -> list[str]:
        path = self.directory / f"{kind}.calls"
        return path.read_text().splitlines() if path.exists() else []


def _config(kind: str, directory: Path, name: str, approval, switches: dict[str, str]) -> McpServerConfig:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "FAKE_MCP_CALL_LOG": str(directory / f"{kind}.calls"),
        "FAKE_MCP_PID_FILE": str(directory / f"{kind}.pid"),
        **switches,
    }
    return McpServerConfig(
        name=name,
        params=StdioServerParameters(command=sys.executable, args=[str(FAKE_SERVER), kind], env=env),
        requires_approval=approval,
    )


def backends(directory: Path, *, azure_switches: dict[str, str] | None = None) -> Backends:
    directory.mkdir(parents=True, exist_ok=True)
    return Backends(
        directory,
        azure=_config("azure", directory, "azure_advisor", lambda tool: False, azure_switches or {}),
        jira=_config("jira", directory, "jira", lambda tool: tool == WRITE_TOOL, {}),
    )


def agent_factory(parts: Backends):
    def factory(approval_callback, progress, on_tool_result):
        model = ScriptedModel([tool_use(WRITE_TOOL, WRITE_ARGS), text("done")])
        return AdvisorAgent(
            model="claude-test",
            workspace_dir=str(parts.directory / "agent-workspace"),
            servers=[parts.jira],
            client=model.client(),
            approval_callback=approval_callback,
            progress=progress,
            on_tool_result=on_tool_result,
        )

    return factory


def main(argv: list[str]) -> None:
    directory, port = Path(argv[0]), int(argv[1])
    hang = "hang-azure-start" in argv[2:]
    parts = backends(directory, azure_switches={"FAKE_MCP_HANG_START": "1"} if hang else None)
    settings = Settings(token=secrets.token_urlsafe(16), port=port)
    server = build_server(
        settings,
        model="claude-test",
        db_path=directory / "advisor.db",
        workspace=directory / "workspace",
        agent_factory=agent_factory(parts),
        azure_config=parts.azure,
    )
    run_server(server)


if __name__ == "__main__":
    main(sys.argv[1:])
