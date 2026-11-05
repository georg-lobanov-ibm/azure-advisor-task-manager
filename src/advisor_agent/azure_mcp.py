"""
Azure Advisor MCP server wiring for the Azure Advisor Task Manager.

Kept in its own module, mirroring the Jira/Atlassian MCP wiring in
``jira_mcp.py``, so each MCP server's config and approval logic can be read,
tested, and changed independently. ``agent.py``'s ``build_server_configs``
composes both.

Server choice
--------------
Per PLAN.md section 3 and docs/azure-advisor-mcp-server.md in the plan repo,
this project uses Microsoft's own Azure MCP server, restricted to the
``advisor`` namespace and started with ``--read-only``.

The server is installed from PyPI (``msmcp-azure``, a thin Python wrapper
around Microsoft's native ``azmcp`` binary) and run with ``uvx``, so the only
prerequisite is ``uv``, which the Jira server needs as well. Microsoft also
publishes the server on npm as ``@azure/mcp``; that route needs Node.js and
npm (``npx``).
The version is pinned so a new release is never picked up unreviewed; bump
``AZURE_MCP_VERSION`` deliberately and re-check the tool list.

Microsoft's command reference marks ``advisor recommendation update`` as a
write operation, so ``--read-only`` is expected to be what keeps it out of
the tool list. Do not drop the flag.

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

AZURE_ADVISOR_NAMESPACE = "advisor"

# PyPI package that ships the server, and the exact version that was checked
# (the 3.0.0 `azmcp server start --help` lists --namespace and --read-only).
AZURE_MCP_PACKAGE = "msmcp-azure"
AZURE_MCP_VERSION = "3.0.0"


def azure_advisor_server_config() -> McpServerConfig:
    """Build the config that starts the Azure MCP Server.

    This spawns ``uvx --from msmcp-azure==<version> azmcp server start
    --namespace advisor --read-only`` as a local subprocess. Every set
    ``AZURE_*`` variable is forwarded to it (see the module docstring,
    "Credentials").

    The first start downloads the package (about 60 MB); ``uvx`` caches it.
    """
    return McpServerConfig(
        name=AZURE_ADVISOR_SERVER_NAME,
        params=StdioServerParameters(
            command="uvx",
            args=[
                "--from",
                f"{AZURE_MCP_PACKAGE}=={AZURE_MCP_VERSION}",
                "azmcp",
                "server",
                "start",
                "--namespace",
                AZURE_ADVISOR_NAMESPACE,
                "--read-only",
            ],
            env=forward_env(prefixes=("AZURE_",)),
        ),
        # `--read-only` is meant to remove the write tools, so what is left
        # needs no human in the loop. Confirm against the real tool list.
        requires_approval=lambda tool_name: False,
        install_hint="Install uv (it provides uvx) and make sure it is on PATH.",
    )
