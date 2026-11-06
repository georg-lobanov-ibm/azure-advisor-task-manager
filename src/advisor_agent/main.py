"""CLI entrypoint for the Azure Advisor Task Manager agent.

Phase 0 spike (PLAN.md section 9): start the Azure Advisor MCP server (and
the Jira MCP server, if configured) and send a prompt to Claude through the
Anthropic API, to confirm the end-to-end MCP connection works before any
decision policy or database wiring is exercised against live Azure data.
Jira tools that can change something ask for approval on the terminal first.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from .agent import AdvisorAgent

# The repo-root .env (see .env.example). Resolved from this file rather than
# searched for upwards, so an unrelated .env in a parent directory is never
# picked up.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

DEFAULT_PROMPT = (
    "List the Azure Advisor recommendations you can see using the available "
    "tools, grouped by category and impact."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="advisor-agent",
        description=(
            "Azure Advisor Task Manager agent. Starts the Azure Advisor MCP "
            "server (and the Jira MCP server, if configured) and sends the "
            "agent a prompt."
        ),
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="Prompt to send to the agent once the MCP servers are up.",
    )
    parser.add_argument(
        "--model",
        default="claude-opus-5-5",
        help="Claude model to use (default: claude-opus-5-5).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    # Load .env before AdvisorAgent reads ANTHROPIC_API_KEY / JIRA_* from the
    # environment. A missing file is ignored, and variables already set in the
    # environment (e.g. exported in the shell) take precedence over the file.
    load_dotenv(ENV_FILE)
    args = parse_args(argv)
    with AdvisorAgent(model=args.model) as agent:
        response = agent.query(args.prompt)
    print(response)


if __name__ == "__main__":
    main()
