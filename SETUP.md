# Azure Advisor Task Manager - Python Setup

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
├── client.py              # Main Anthropic API client wrapper
├── agent.py               # Agent SDK setup and MCP config (TODO)
├── policy.py              # Decision rules and thresholds (TODO)
├── approval.py            # Approval UI (TODO)
├── audit.py               # JSONL audit logging (TODO)
└── database.py            # Database schema and queries (TODO)

examples/
├── basic_client.py        # Basic client usage examples
```

## Available APIs

### Core Messages API
The basic interface for all Claude interactions:

```python
from advisor_agent.client import AdvisorAgentClient

client = AdvisorAgentClient()
response = client.send_message("Your question here")
```

### Tool Use (for MCP Server Integration)
Define tools that Claude can call (simulating MCP servers):

```python
tools = [
    {
        "name": "get_advisor_findings",
        "description": "Fetch Azure Advisor recommendations",
        "input_schema": {
            "type": "object",
            "properties": {
                "subscription_id": {"type": "string"},
                "category": {"type": "string"}
            },
            "required": ["subscription_id"]
        }
    }
]

text_response, tool_calls = client.send_message_with_tools(
    user_message="Get cost optimization findings",
    tools=tools
)
```

### Multi-turn Conversations
The client maintains conversation history automatically:

```python
client.send_message("First question")
client.send_message("Follow-up question")  # Has context from first
client.clear_history()  # Clear when starting new conversation
```

## Available Claude Models

- **Claude Opus 5.5** (`claude-opus-5-5`) - Recommended. Most capable, best for complex reasoning.
- **Claude Sonnet 5.5** (`claude-sonnet-5-5`) - Faster, good for most tasks
- **Claude Haiku 5.5** (`claude-haiku-5-5`) - Cheapest, good for simple tasks
- **Claude Fable 5.1** (`claude-fable-5-1`) - Most capable (special access)

Default is Claude Opus 5.5.

## Running Examples

```bash
python examples/basic_client.py
```

This runs three examples:
1. Basic single and multi-turn chat
2. System prompt guidance
3. Tool definitions (MCP server simulation)

## Key Features Implemented

✅ Basic message sending
✅ Multi-turn conversation support
✅ Tool definitions (for MCP servers)
✅ System prompt guidance
✅ Model information retrieval

## Next Steps

### Phase 0 - Spike (Current)
- [ ] Connect to real Azure MCP server
- [ ] Connect to real Jira MCP server
- [ ] Verify tool names and capabilities

### Phase 1 - MVP
- [ ] Implement policy.py (decision rules)
- [ ] Build approval UI
- [ ] Set up SQLite database
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
- [Claude Models Overview](https://docs.anthropic.com/claude/reference/models-overview)
- [Tool Use Guide](https://docs.anthropic.com/claude/docs/tool-use)
- [Prompt Caching](https://docs.anthropic.com/claude/docs/caching)
- [Vision / Files API](https://docs.anthropic.com/claude/reference/files-api)
