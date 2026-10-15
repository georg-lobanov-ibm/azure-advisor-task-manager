"""
Azure Advisor MCP server wiring for the Azure Advisor Task Manager.

Kept in its own module, mirroring the Jira/Atlassian MCP wiring in
``jira_mcp.py``, so each MCP server's config and tool-allowlist logic can be
read, tested, and changed independently. ``agent.py``'s ``build_agent_options``
composes both into one ``ClaudeAgentOptions``.

Server choice
--------------
Per PLAN.md section 3 and docs/azure-advisor-mcp-server.md in the plan repo,
this project uses Microsoft's own ``@azure/mcp`` server (a Node.js package,
spawned via ``npx``), restricted to the ``advisor`` namespace and started
with ``--read-only``. That namespace exposes only read-only tools (metadata
get/list, recommendation list/summary/apply) — ``--read-only`` is redundant
for this namespace today but kept as defense-in-depth.

Approval gating
----------------
Unlike the Jira MCP server (see ``jira_mcp.py``), there is no write-tool
approval gate here: every tool this server exposes is read-only, so a single
wildcard in ``allowed_tools`` is sufficient and no ``PreToolUse`` hook is
needed.

Azure auth is resolved via the standard Azure credential chain (managed
identity or service principal) in the environment this subprocess inherits —
no explicit ``env`` override is needed here, unlike the Jira server, as long
as the host/container's ``AZURE_*`` credentials are set.
"""

# Name this server is registered under in `mcp_servers`. MCP tools exposed by
# it are addressed as `mcp__{AZURE_ADVISOR_SERVER_NAME}__<tool_name>`.
AZURE_ADVISOR_SERVER_NAME = "azure_advisor"

# The namespace exposes only read-only tools (metadata get/list, recommendation
# list/summary/apply) — see docs/azure-advisor-mcp-server.md in the plan repo.
# `--read-only` is redundant for this namespace today but kept as
# defense-in-depth per PLAN.md section 3.
AZURE_ADVISOR_NAMESPACE = "advisor"


def azure_advisor_mcp_server_config() -> dict:
    """Build the stdio MCP server config that starts the Azure MCP Server.

    This spawns ``npx -y @azure/mcp@latest server start --namespace advisor
    --read-only`` as a local subprocess, matching Microsoft's own sample and
    PLAN.md section 3. Azure auth is resolved via the standard Azure
    credential chain (managed identity or service principal) in the
    environment this subprocess inherits — no explicit ``env`` override is
    needed as long as the host/container's `AZURE_*` credentials are set.
    """
    return {
        "type": "stdio",
        "command": "npx",
        "args": [
            "-y",
            "@azure/mcp@latest",
            "server",
            "start",
            "--namespace",
            AZURE_ADVISOR_NAMESPACE,
            "--read-only",
        ],
    }


def azure_advisor_allowed_tools() -> list[str]:
    """``allowed_tools`` entries for the Azure Advisor MCP server.

    A single wildcard — every tool this server exposes is read-only (see the
    module docstring), so no additional approval gate is needed here, unlike
    ``jira_allowed_tools()`` in ``jira_mcp.py``. Wildcards only match after a
    literal `mcp__<server>__` prefix (a bare `mcp__*` or `*` is ignored), so
    this scopes the allow-list to exactly this server's tools.
    """
    return [f"mcp__{AZURE_ADVISOR_SERVER_NAME}__*"]
