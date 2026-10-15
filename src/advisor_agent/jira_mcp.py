"""
Jira/Atlassian MCP server wiring for the Azure Advisor Task Manager.

Kept in its own module, separate from the Azure Advisor MCP wiring in
``agent.py``, so each MCP server's config and approval logic can be read,
tested, and changed independently.

Server choice
--------------
Per ``azure-advisor-task-manager-plan/docs/jira-mcp-server.md``, this project
uses ``sooperset/mcp-atlassian`` (self-hosted, stdio), not the official
Atlassian Remote MCP Server. The deciding factor is auth fit: the official
server's primary auth is interactive OAuth 2.1, which the Claude Agent SDK
doesn't drive itself, a poor fit for the unattended/scheduled agent in
PLAN.md Phase 4. ``mcp-atlassian`` takes a plain API token/PAT via env var
instead, with no interactive step, and runs as a local stdio subprocess —
the same pattern as the Azure MCP server in ``agent.py``.

Approval gating
----------------
Per PLAN.md section 3, Jira *read* tools (``jira_search``, ``jira_get_issue``,
``jira_get_transitions``) are allowed outright, but Jira *write* tools
(create/update/transition/comment/delete) must be blocked until a human
approves them. This is done with a ``PreToolUse`` hook rather than simply
leaving the write tools out of ``allowed_tools``: hooks run before
``allowed_tools`` is evaluated, so the gate can't be silently bypassed by the
wildcard allow-list entry below (``jira_allowed_tools``) — both are needed,
not either.

Credentials are optional at this layer: if they aren't configured,
``jira_mcp_server_config()`` returns ``None`` and it's up to the caller
(``agent.py``) to decide whether to warn and fall back to running without
this server.
"""

import os
from typing import Any

from claude_agent_sdk import HookMatcher

# Name this server is registered under in `mcp_servers`. Its tools are
# addressed as `mcp__{JIRA_SERVER_NAME}__<tool_name>`.
JIRA_SERVER_NAME = "jira"

# The real write-tool names exposed by sooperset/mcp-atlassian that this
# agent's system prompt describes using (create/update/skip/close + comments)
# — see docs/jira-mcp-server.md. This is the agent's current write surface,
# not necessarily mcp-atlassian's full tool catalog (e.g. Agile board/sprint
# write tools aren't used by this agent yet and aren't included here) —
# revisit this set once connected to a live server.
JIRA_WRITE_TOOLS = frozenset(
    {
        "jira_create_issue",
        "jira_update_issue",
        "jira_transition_issue",
        "jira_add_comment",
        "jira_edit_comment",
        "jira_delete_issue",
    }
)


def jira_mcp_server_config() -> dict | None:
    """Build the stdio MCP server config that starts the Jira MCP server.

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

    return {
        "type": "stdio",
        "command": "uvx",
        "args": ["mcp-atlassian"],
        "env": env,
    }


async def _gate_jira_writes(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
    """``PreToolUse`` callback: require human approval for Jira write tool calls.

    Matched (see `jira_approval_hook_matcher`) against every
    ``mcp__{JIRA_SERVER_NAME}__*`` tool call. Write tools (`JIRA_WRITE_TOOLS`)
    get `permissionDecision: "ask"`; read tools fall through (`{}`) to the
    normal `allowed_tools` wildcard evaluation.
    """
    tool_name = input_data.get("tool_name", "")
    prefix = f"mcp__{JIRA_SERVER_NAME}__"
    bare_name = tool_name[len(prefix):] if tool_name.startswith(prefix) else tool_name

    if bare_name in JIRA_WRITE_TOOLS:
        return {
            "hookSpecificOutput": {
                "hookEventName": input_data["hook_event_name"],
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    f"Jira write tool '{bare_name}' requires human approval "
                    "before it runs (PLAN.md section 3)."
                ),
            }
        }
    return {}


def jira_approval_hook_matcher() -> HookMatcher:
    """``HookMatcher`` for ``PreToolUse`` that gates Jira write tools.

    Add this to the ``PreToolUse`` list in ``ClaudeAgentOptions.hooks``.
    """
    return HookMatcher(matcher=f"^mcp__{JIRA_SERVER_NAME}__", hooks=[_gate_jira_writes])


def jira_allowed_tools() -> list[str]:
    """``allowed_tools`` entries for the Jira MCP server.

    A single wildcard — read tools are allowed outright by this entry, and
    write tools are additionally gated by `jira_approval_hook_matcher()`.
    Hooks run before `allowed_tools` is evaluated, so the wildcard here does
    not let write tools bypass that gate.
    """
    return [f"mcp__{JIRA_SERVER_NAME}__*"]
