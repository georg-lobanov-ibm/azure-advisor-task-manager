# Azure Advisor Task Manager - Python Setup with the Anthropic API

## Prerequisites

- Python 3.10+
- pip or poetry
- `uv` / `uvx` on PATH. It spawns the Azure MCP Server subprocess (the
  `msmcp-azure` package from PyPI, which bundles Microsoft's `azmcp` binary;
  the first start downloads about 60 MB) and the Jira MCP server subprocess,
  if Jira is configured — see "Jira credentials" below. Node.js is not needed.

## Installation

### 1. Create a Virtual Environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Set Up Anthropic API Key

Get your API key from [console.anthropic.com](https://console.anthropic.com):

```bash
export ANTHROPIC_API_KEY='sk-ant-...'
```

Or create a `.env` file from the template (`.env` is git-ignored):

```bash
cp .env.example .env
# then edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

The entry points (`python -m advisor_agent.main` and
`examples/agent_sdk_demo.py`) load the repo-root `.env` themselves at startup
with `python-dotenv`, so this works the same from VS Code, a terminal, or
anywhere else. A missing `.env` is skipped without an error, and a variable
already set in the environment (for example exported in your shell) takes
precedence over the file. If the agent reports that `ANTHROPIC_API_KEY` is not
set, check that `.env` exists in the repo root and that the line has the form
`ANTHROPIC_API_KEY=sk-ant-...`.

The `envFile` entries in `.vscode/launch.json` are not relied on for this.

The agent always calls `https://api.anthropic.com` with `ANTHROPIC_API_KEY`.
`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_MODEL` in your
environment (for example a company gateway set up for Claude Code) are
deliberately ignored. Pick the model with `--model` or the `model` argument of
`AdvisorAgent`.

To export everything in it to your shell (a plain `source .env` would not
export the variables to child processes):

```bash
set -a; source .env; set +a
```

### 4. Set Up Jira Credentials (optional)

The Jira MCP server (`sooperset/mcp-atlassian`, spawned via `uvx mcp-atlassian`)
is attached automatically if its credentials are present in the environment.
Set `JIRA_URL` plus either `JIRA_USERNAME` + `JIRA_API_TOKEN` (Jira Cloud) or
`JIRA_PERSONAL_TOKEN` (Jira Server/Data Center):

```bash
export JIRA_URL='https://your-site.atlassian.net'
export JIRA_USERNAME='you@example.com'
export JIRA_API_TOKEN='...'
```

If these aren't set, `AdvisorAgent` prints a warning and runs with only the
Azure Advisor MCP server attached — Jira is optional, not required. If they are
set but the server fails to start, `AdvisorAgent` raises an error instead of
quietly running without Jira.

### 5. Azure credentials (optional)

The Azure Advisor MCP server finds credentials through the standard Azure
credential chain. `az login` works as is, because the server finds its token
cache through `HOME`. For a service principal, set `AZURE_TENANT_ID`,
`AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` (see `.env.example`).

The MCP servers are started with a minimal environment (`HOME`, `PATH`, proxy
and CA settings, `TERM`, and so on). Only the set `AZURE_*` variables (Azure
server) or the chosen `JIRA_*` variables (Jira server) are added to that, so
`ANTHROPIC_API_KEY` and everything else in your shell never reach them.

## Project Structure

```
src/advisor_agent/
├── __init__.py
├── agent.py               # Main agent: Anthropic API + tool runner; composes both MCP servers (ACTIVE)
├── mcp_tools.py            # MCP stdio plumbing and the human-approval gate for tools (ACTIVE)
├── azure_mcp.py            # Azure Advisor MCP server wiring (ACTIVE)
├── jira_mcp.py             # Jira/Atlassian MCP server wiring + which tools need approval (ACTIVE)
├── main.py                # CLI entrypoint (Phase 0 spike)
├── policy.py               # Decision rules and thresholds
├── approval.py             # Approval UI
├── database.py             # Database schema and queries
└── audit.py                # JSONL audit logging (TODO)

examples/
├── agent_sdk_demo.py       # Agent usage examples (file name kept; launch.json refers to it)
```

### Running the Phase 0 spike

Requires `uvx` on PATH to spawn the Azure MCP Server, and an Azure
identity with Reader access on the target subscription(s) resolved via the
standard Azure credential chain (e.g. `az login`, or a managed identity /
service principal in the environment):

```bash
python -m advisor_agent.main --prompt "List Azure Advisor recommendations"
```

A run can take a minute or more before anything is answered (the first start
downloads the Azure server, and the model may call several tools). While it
works, the agent prints progress lines to stderr, each prefixed with
`[advisor-agent]`: server start, every model turn, every tool call and how
long it has been running. The answer itself goes to stdout at the end. Pass
`progress=` to `AdvisorAgent` to send the lines elsewhere or to silence them.

## Anthropic API with MCP tools

The agent calls the Claude API directly through the `anthropic` Python SDK. Its
tool loop is the SDK's tool runner (`client.beta.messages.tool_runner`), and the
tools are those of the two MCP servers, which the `mcp` package starts as local
stdio subprocesses. This does not use Claude Code or the Claude Agent SDK, so
settings in `~/.claude/settings.json` have no effect on it.

✅ **Agent loop**: Claude decides when to call tools; the tool runner executes them and feeds the results back until Claude is done (at most 25 model turns per query)
✅ **MCP tools only**: Azure Advisor (`azure_mcp.py`) and, if configured, Jira (`jira_mcp.py`); `agent.py` composes both. The model has no file, shell or web tools
✅ **Sessions**: `AdvisorAgent` starts the MCP servers on the first query and keeps them, and the conversation, for its lifetime. A follow-up `query()` sees earlier answers; `reset_conversation()` forgets them
✅ **Approval gates**: Azure Advisor tools are read-only and run freely. Of the Jira tools only `jira_search`, `jira_get_issue` and `jira_get_transitions` run freely; every other Jira tool, including any the agent doesn't know about, asks for approval on the terminal for each call and is not sent to the server without a "yes" (`mcp_tools.py`, `jira_mcp.py`)
✅ **Fails closed**: if stdin or stderr is not a terminal (a scheduled run, CI), approval-gated calls are denied, and the model is told so
✅ **Audit Trail**: Structured logging and decision tracking

### Usage

Use the agent as a context manager so the MCP servers and the background
thread that owns them are cleanly stopped when you're done (or call
`agent.close()` yourself):

```python
from advisor_agent.agent import AdvisorAgent

with AdvisorAgent(model="claude-opus-5-5") as agent:
    # Query the agent (it handles everything: tools, loops, decisions)
    response = agent.query("Process Azure Advisor findings for this subscription")

    # Dry-run analysis (no actual changes) — reuses the same MCP servers
    result = agent.process_findings_dry_run(
        subscription_id="12345678-1234-1234-1234-123456789012",
        jira_project="AZURE",
        dry_run=True
    )

    # Decision logging (automatic audit trail)
    agent.save_decision_log({
        "timestamp": "2024-01-15T10:30:00Z",
        "advisor_id": "rec-123",
        "decision": "CREATE",
        "jira_key": "AZURE-456"
    })
```

## Available Claude Models

- **Claude Opus 5.5** (`claude-opus-5-5`) - Recommended. Most capable, best for complex reasoning.
- **Claude Sonnet 5.5** (`claude-sonnet-5-5`) - Faster, good for most tasks
- **Claude Haiku 5.5** (`claude-haiku-5-5`) - Cheapest, good for simple tasks
- **Claude Fable 5.1** (`claude-fable-5-1`) - Most capable (special access)

Default is Claude Opus 5.5.

## Running Examples

```bash
python examples/agent_sdk_demo.py
```

This runs through:
1. Basic agent interaction
2. Dry-run analysis of Azure Advisor findings
3. The MCP servers (and their approval rules) available to the agent
4. Decision logging and audit trail
5. The agent's system prompt

## Key Features Implemented

✅ Anthropic API integration with the SDK tool runner (`agent.py`, `mcp_tools.py`)
✅ Azure Advisor MCP server wiring, spawned as a local stdio subprocess (`azure_mcp.py`)
✅ Jira MCP server wiring, spawned as a local stdio subprocess if configured, with every non-read tool gated behind approval (`jira_mcp.py`)
✅ Decision policy engine with impact thresholds (`policy.py`)
✅ SQLite persistence and audit trail (`database.py`)
✅ Human-in-the-loop approval workflow (`approval.py`)

## Next Steps

### Phase 0 - Spike (Current)
- [ ] Connect to real Azure MCP server (wired through the Anthropic API; not yet verified live)
- [ ] Connect to real Jira MCP server (wired through the Anthropic API; not yet verified live)
- [ ] Verify tool names and capabilities against a live Jira MCP server (currently based on docs/jira-mcp-server.md research only)

### Phase 1 - MVP
- [x] Implement policy.py (decision rules)
- [x] Build approval UI
- [x] Set up SQLite database
- [ ] Create JSONL audit logging

### Phase 2 - Approval-gated Jira
- [ ] Implement approval flow
- [ ] Database dedupe logic
- [ ] Jira ticket creation

### Phase 3 - Sync
- [ ] Update and close tickets
- [ ] Track finding lifecycle

### Phase 4 - Scheduling
- [ ] Cron-based runs
- [ ] Notifications

## References

- [Anthropic API Documentation](https://docs.anthropic.com)
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) (tool runner and MCP helpers)
- [Claude Models Overview](https://docs.anthropic.com/claude/reference/models-overview)
- [MCP (Model Context Protocol)](https://docs.anthropic.com/claude/docs/mcp)
