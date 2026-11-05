"""
Azure Advisor MCP server wiring for the Azure Advisor Task Manager.

Kept in its own module, mirroring the Jira/Atlassian MCP wiring in
``jira_mcp.py``, so each MCP server's config and approval logic can be read,
tested, and changed independently. ``agent.py``'s ``build_server_configs``
composes both.

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
Unlike the Jira MCP server (see ``jira_mcp.py``), there is no approval gate
here: every tool this server exposes is read-only, so none of them needs a
human in the loop.

Credentials
-----------
Azure auth is resolved by the server via the standard Azure credential chain
(``az login``, a managed identity, or a service principal). The server is
started with a minimal environment (see ``mcp_tools.py``), so the ``AZURE_*``
variables that are set in this process — ``AZURE_TENANT_ID``,
``AZURE_CLIENT_ID``, ``AZURE_CLIENT_SECRET``, ``AZURE_TOKEN_CREDENTIALS`` and
so on — are forwarded explicitly. ``az login`` needs no forwarding: its token
cache is found through ``HOME``, which is inherited.
"""

from mcp import StdioServerParameters

from .mcp_tools import McpServerConfig, forward_env

# Name this server is registered under. It identifies the server in approval
# prompts and in `AdvisorAgent.get_agent_tools()`.
AZURE_ADVISOR_SERVER_NAME = "azure_advisor"

# The namespace exposes only read-only tools (metadata get/list, recommendation
# list/summary/apply) — see docs/azure-advisor-mcp-server.md in the plan repo.
# `--read-only` is redundant for this namespace today but kept as
# defense-in-depth per PLAN.md section 3.
AZURE_ADVISOR_NAMESPACE = "advisor"


def azure_advisor_server_config() -> McpServerConfig:
    """Build the config that starts the Azure MCP Server.

    This spawns ``npx -y @azure/mcp@latest server start --namespace advisor
    --read-only`` as a local subprocess, matching Microsoft's own sample and
    PLAN.md section 3. Every set ``AZURE_*`` variable is forwarded to it (see
    the module docstring, "Credentials").
    """
    return McpServerConfig(
        name=AZURE_ADVISOR_SERVER_NAME,
        params=StdioServerParameters(
            command="npx",
            args=[
                "-y",
                "@azure/mcp@latest",
                "server",
                "start",
                "--namespace",
                AZURE_ADVISOR_NAMESPACE,
                "--read-only",
            ],
            env=forward_env(prefixes=("AZURE_",)),
        ),
        # Every tool in this namespace is read-only.
        requires_approval=lambda tool_name: False,
        install_hint="Install Node.js (it provides npx) and make sure it is on PATH.",
    )
