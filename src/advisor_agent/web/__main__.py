"""``python -m advisor_agent.web``: serve the web UI on this machine.

Prints an address with a one-time launch token in its fragment. Opening that
address signs the browser in; nothing else on this computer can use the server.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from pathlib import Path
from types import FrameType
from typing import Any

import uvicorn
from dotenv import load_dotenv

from ..agent import AdvisorAgent
from ..azure_mcp import azure_advisor_server_config
from ..database import AdvisorDatabase, DatabaseSchemaError
from ..main import ENV_FILE
from ..mcp_tools import McpServerConfig
from .app import Services, create_app
from .chat import AgentFactory
from .settings import Settings, jira_base_url

HOST = "127.0.0.1"  # the only address served; there is deliberately no option for another
DEFAULT_PORT = 8765
WORKSPACE_DIR = ENV_FILE.parent / "workspace"
DATABASE_FILE = WORKSPACE_DIR / "advisor.db"
CHATS_FILE = ENV_FILE.parent / "chats.toml"  # the predefined chats, edited by hand
GRACEFUL_SHUTDOWN_SECONDS = 10


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="advisor-web",
        description="Local web UI for the Azure Advisor Task Manager (127.0.0.1 only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT}). It must be free.",
    )
    parser.add_argument(
        "--model",
        default="claude-opus-5-5",
        help="Claude model for the chat (default: claude-opus-5-5).",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def make_agent_factory(model: str, workspace: Path) -> AgentFactory:
    def factory(approval_callback: Any, progress: Any) -> AdvisorAgent:
        return AdvisorAgent(
            model=model,
            workspace_dir=str(workspace),
            approval_callback=approval_callback,
            progress=progress,
        )

    return factory


def launch_url(settings: Settings) -> str:
    """The address to open. The token is in the fragment, which browsers never send."""
    return f"http://{HOST}:{settings.port}/#t={settings.token}"


class WebServer(uvicorn.Server):
    """uvicorn's server, told to end the long-lived requests as soon as it is asked to stop."""

    def __init__(self, config: uvicorn.Config, services: Services, settings: Settings) -> None:
        super().__init__(config)
        self._services = services
        self._settings = settings

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        # uvicorn would wait for the open event streams before it stops the app,
        # and they never end by themselves: close them (and deny what waits for
        # a click) first.
        self._services.begin_shutdown()
        super().handle_exit(sig, frame)

    async def startup(self, sockets: Any = None) -> None:
        await super().startup(sockets)
        if self.started:
            # Printed once the port is really ours, not before.
            print(f"\nOpen this address in your browser:\n\n  {launch_url(self._settings)}\n", flush=True)


def build_server(
    settings: Settings,
    *,
    model: str,
    db_path: Path = DATABASE_FILE,
    workspace: Path = WORKSPACE_DIR,
    agent_factory: AgentFactory | None = None,
    azure_config: McpServerConfig | None = None,
) -> WebServer:
    """The server for one run. ``agent_factory`` and ``azure_config`` are for tests."""
    db = AdvisorDatabase(str(db_path))
    app = create_app(
        settings,
        agent_factory=agent_factory or make_agent_factory(model, workspace),
        azure_config=azure_config or azure_advisor_server_config(),
        db=db,
    )
    config = uvicorn.Config(
        app,
        host=HOST,
        port=settings.port,
        loop="asyncio",
        ws="none",
        lifespan="on",
        proxy_headers=False,
        server_header=False,
        access_log=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    return WebServer(config, app.state.services, settings)


def run_server(server: uvicorn.Server) -> None:
    """Serve until stopped. Ctrl-C ends the server and is not an error.

    After a clean stop uvicorn sends the signal it caught on to Python, which
    turns it into ``KeyboardInterrupt``; ``uvicorn.run`` swallows that, so this does too.
    """
    try:
        server.run()
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> None:
    # Before anything reads ANTHROPIC_API_KEY / JIRA_* / AZURE_*; variables
    # already in the environment win over the file.
    load_dotenv(ENV_FILE)
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = Settings(
        token=secrets.token_urlsafe(24),
        port=args.port,
        jira_base_url=jira_base_url(os.environ.get("JIRA_URL")),
        chats_file=CHATS_FILE,
    )
    try:
        server = build_server(settings, model=args.model)
    except DatabaseSchemaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    run_server(server)


if __name__ == "__main__":
    main()
