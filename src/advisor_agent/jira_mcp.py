"""
Jira/Atlassian MCP server wiring for the Azure Advisor Task Manager.

Kept in its own module, separate from the Azure Advisor MCP wiring in
``azure_mcp.py``, so each MCP server's config and approval logic can be read,
tested, and changed independently.

Server choice
--------------
Per ``azure-advisor-task-manager-plan/docs/jira-mcp-server.md``, this project
uses ``sooperset/mcp-atlassian`` (self-hosted, stdio), not the official
Atlassian Remote MCP Server. The deciding factor is auth fit: the official
server's primary auth is interactive OAuth 2.1, which this agent does not
drive, a poor fit for the unattended/scheduled agent in PLAN.md Phase 4.
``mcp-atlassian`` takes a plain API token/PAT via env var instead, with no
interactive step, and runs as a local stdio subprocess — the same pattern as
the Azure MCP server in ``azure_mcp.py``.

Approval gating
----------------
Per PLAN.md section 3, Jira *write* tools (create/update/transition/comment/
delete) must not run until a human approves them. The gate fails closed: only
the tools in ``JIRA_READ_TOOLS`` run without a prompt, and every other Jira
tool — including any this module has never heard of, such as a batch-create
tool added by a newer ``mcp-atlassian`` — needs approval for each call. A
wrong or outdated name in ``JIRA_READ_TOOLS`` therefore costs an extra prompt,
never an unguarded write. The gate itself is ``ApprovalGatedTool`` in
``mcp_tools.py``, which wraps the tool so the MCP server is not reached
without a "yes".

Credentials are optional at this layer: if they aren't configured,
``jira_server_config()`` returns ``None`` and it's up to the caller
(``agent.py``) to decide whether to warn and fall back to running without
this server.
"""

import json
import os
from typing import Any

from mcp import StdioServerParameters

from .mcp_tools import McpServerConfig, forward_env

# Name this server is registered under. It identifies the server in approval
# prompts and in `AdvisorAgent.get_agent_tools()`.
JIRA_SERVER_NAME = "jira"

# The read tools of sooperset/mcp-atlassian that this agent's system prompt
# describes using — see docs/jira-mcp-server.md. Everything not listed here is
# treated as a write and needs human approval (fail closed). Revisit this set
# once connected to a live server.
JIRA_READ_TOOLS = frozenset(
    {
        "jira_search",
        "jira_get_issue",
        "jira_get_transitions",
    }
)


def jira_requires_approval(tool_name: str) -> bool:
    """True for every Jira tool that is not a known read tool."""
    return tool_name not in JIRA_READ_TOOLS


# The mcp-atlassian tool that creates one issue. Its arguments include
# ``issue_type`` ("Story", "Task", ...); its answer is JSON text like
# ``{"message": "Issue created successfully", "issue": {"key": "ABC-123", ...}}``.
JIRA_CREATE_ISSUE_TOOL = "jira_create_issue"

# A create answer is a few KB; anything far larger is not one and is not parsed.
_MAX_RESULT_CHARS = 1_000_000


def created_issue_key(result: Any) -> str | None:
    """The key of the issue in the answer of ``jira_create_issue``, or ``None``.

    ``result`` is what the tool runner got back from the server: the JSON
    text itself, or a list of content blocks (``{"type": "text", "text": ...}``)
    holding it. The key is returned as it is, unchecked; anything that does not
    look like a create answer gives ``None``.
    """
    if isinstance(result, str):
        text = result
    elif isinstance(result, list):
        parts = [
            block.get("text")
            for block in result
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if not all(isinstance(part, str) for part in parts):
            return None
        text = "".join(parts)
    else:
        return None
    if len(text) > _MAX_RESULT_CHARS:
        return None
    try:
        answer = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(answer, dict):
        return None
    issue = answer.get("issue", answer)  # a bare issue object is accepted too
    if not isinstance(issue, dict):
        return None
    key = issue.get("key")
    return key if isinstance(key, str) else None


def jira_server_config() -> McpServerConfig | None:
    """Build the config that starts the Jira MCP server.

    Spawns ``uvx mcp-atlassian`` as a local subprocess (the launch command
    resolved in docs/jira-mcp-server.md). Requires ``JIRA_URL`` plus either
    (``JIRA_USERNAME`` and ``JIRA_API_TOKEN``) for Jira Cloud, or
    (``JIRA_PERSONAL_TOKEN``) for Jira Server/Data Center — read from the
    environment and passed through explicitly via the server's ``env``, per
    the exact config shape recommended in docs/jira-mcp-server.md. Secrets
    are never hardcoded here or passed as CLI args (per PLAN.md section 3's
    "secrets from env vars or a secret store, never the repo").

    Returns ``None`` if the required credentials aren't configured, leaving
    it to the caller to decide whether to warn and run without this server.
    """
    jira_url = os.environ.get("JIRA_URL")
    username = os.environ.get("JIRA_USERNAME")
    api_token = os.environ.get("JIRA_API_TOKEN")
    personal_token = os.environ.get("JIRA_PERSONAL_TOKEN")

    if not jira_url:
        return None

    env: dict[str, str] = {"JIRA_URL": jira_url}
    if username and api_token:
        env["JIRA_USERNAME"] = username
        env["JIRA_API_TOKEN"] = api_token
    elif personal_token:
        env["JIRA_PERSONAL_TOKEN"] = personal_token
    else:
        return None

    return McpServerConfig(
        name=JIRA_SERVER_NAME,
        params=StdioServerParameters(
            command="uvx",
            args=["mcp-atlassian"],
            # Only the Jira variables chosen above, plus proxy/CA settings.
            env={**forward_env(), **env},
        ),
        requires_approval=jira_requires_approval,
        install_hint="Install uv (it provides uvx) and make sure it is on PATH.",
    )
