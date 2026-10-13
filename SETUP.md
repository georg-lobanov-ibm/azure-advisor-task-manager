# Azure Advisor Task Manager - Python Setup with Claude Agent SDK

## Prerequisites

- Python 3.10+
- pip or poetry

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

Or create a `.env` file:

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

Then load it:

```bash
source .env
```

## Project Structure

```
src/advisor_agent/
├── __init__.py
├── agent.py               # Main Agent SDK implementation (ACTIVE)
├── policy.py               # Decision rules and thresholds
├── approval.py             # Approval UI
├── database.py             # Database schema and queries
└── audit.py                # JSONL audit logging (TODO)

examples/
├── agent_sdk_demo.py       # Agent SDK usage examples
```

## Claude Agent SDK

This project is built exclusively on the **Claude Agent SDK**, which is
purpose-built for autonomous agents and provides:

✅ **Built-in Tools**: File read/write, bash execution, grep, web search (out of the box)
✅ **Full Agent Loop**: Claude automatically decides when to use tools and executes them
✅ **Context Management**: Handles conversation history, memory, and session state
✅ **MCP Integration**: Ready to connect to MCP servers (Azure Advisor, Jira)
✅ **Audit Trail**: Structured logging and decision tracking
✅ **Sessions**: Persistent agent state across runs
✅ **Approval Gates**: Built-in hooks for human-in-the-loop workflows

### Agent SDK Usage

```python
from advisor_agent.agent import AdvisorAgent

# Initialize agent
agent = AdvisorAgent(model="claude-opus-5-5")

# Query the agent (it handles everything: tools, loops, decisions)
response = agent.query("Process Azure Advisor findings for this subscription")

# Dry-run analysis (no actual changes)
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
✅ Decision policy engine with impact thresholds (`policy.py`)
✅ SQLite persistence and audit trail (`database.py`)
✅ Human-in-the-loop approval workflow (`approval.py`)

## Next Steps

### Phase 0 - Spike (Current)
- [ ] Connect to real Azure MCP server
- [ ] Connect to real Jira MCP server
- [ ] Verify tool names and capabilities

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
