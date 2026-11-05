# Azure Advisor Task Manager - Python Setup with Claude Agent SDK

## Prerequisites

- Python 3.10+
- pip or poetry
- Node.js / `npx` on PATH (used to spawn the Azure MCP Server subprocess)
- `uv` / `uvx` on PATH (used to spawn the Jira MCP server subprocess, if Jira
  is configured — see "Jira credentials" below)

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
Azure Advisor MCP server attached — Jira is optional, not required.

## Project Structure

```
src/advisor_agent/
├── __init__.py
├── agent.py               # Main Agent SDK implementation; composes both MCP servers (ACTIVE)
├── azure_mcp.py            # Azure Advisor MCP server wiring (ACTIVE)
├── jira_mcp.py             # Jira/Atlassian MCP server wiring + write-tool approval hook (ACTIVE)
├── main.py                # CLI entrypoint (Phase 0 spike)
├── policy.py               # Decision rules and thresholds
├── approval.py             # Approval UI
├── database.py             # Database schema and queries
└── audit.py                # JSONL audit logging (TODO)

examples/
├── agent_sdk_demo.py       # Agent SDK usage examples
```

### Running the Phase 0 spike

Requires `npx` (Node.js) on PATH to spawn the Azure MCP Server, and an Azure
identity with Reader access on the target subscription(s) resolved via the
standard Azure credential chain (e.g. `az login`, or a managed identity /
service principal in the environment):

```bash
python -m advisor_agent.main --prompt "List Azure Advisor recommendations"
```

## Claude Agent SDK

This project is built exclusively on the **Claude Agent SDK**, which is
purpose-built for autonomous agents and provides:

✅ **Built-in Tools**: File read/write, bash execution, grep, web search (out of the box)
✅ **Full Agent Loop**: Claude automatically decides when to use tools and executes them
✅ **Context Management**: Handles conversation history, memory, and session state
✅ **MCP Integration**: Connects to the Azure Advisor MCP server (`azure_mcp.py`) and, if configured, the Jira MCP server (`jira_mcp.py`); `agent.py` composes both
✅ **Audit Trail**: Structured logging and decision tracking
✅ **Sessions**: Persistent agent state — `AdvisorAgent` keeps one `ClaudeSDKClient` connected for its lifetime and reuses it across calls, instead of reconnecting per query
✅ **Approval Gates**: Jira write tools (`mcp__jira__*`) are blocked until a human approves them, via a `PreToolUse` hook (`jira_mcp.py`) — not just an `allowed_tools` omission, since hooks run before `allowed_tools` is evaluated

### Agent SDK Usage

The agent keeps a persistent `ClaudeSDKClient` connection (and its Azure MCP
subprocess) open for its lifetime, reusing it across every `.query()` call
on the same instance instead of reconnecting each time. Use it as a context
manager so the connection is cleanly closed when you're done (or call
`agent.close()` yourself):

```python
from advisor_agent.agent import AdvisorAgent

with AdvisorAgent(model="claude-opus-5-5") as agent:
    # Query the agent (it handles everything: tools, loops, decisions)
    response = agent.query("Process Azure Advisor findings for this subscription")

    # Dry-run analysis (no actual changes) — reuses the same connection
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
3. Available built-in tools
4. Decision logging and audit trail
5. The agent's system prompt

## Key Features Implemented

✅ Claude Agent SDK integration (`agent.py`)
✅ Azure Advisor MCP server connection, spawned as a local stdio subprocess (`azure_mcp.py`)
✅ Jira MCP server connection, spawned as a local stdio subprocess if configured, with write tools gated behind approval (`jira_mcp.py`)
✅ Decision policy engine with impact thresholds (`policy.py`)
✅ SQLite persistence and audit trail (`database.py`)
✅ Human-in-the-loop approval workflow (`approval.py`)

## Next Steps

### Phase 0 - Spike (Current)
- [x] Connect to real Azure MCP server
- [x] Connect to real Jira MCP server
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
- [Claude Agent SDK](https://docs.anthropic.com/claude/docs/claude-agent-sdk)
- [Claude Models Overview](https://docs.anthropic.com/claude/reference/models-overview)
- [MCP (Model Context Protocol)](https://docs.anthropic.com/claude/docs/mcp)
