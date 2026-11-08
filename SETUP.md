# Azure Advisor Task Manager - Python Setup with the Anthropic API

## Prerequisites

- Python 3.11+ (the web UI reads `chats.toml` with the standard library's
  `tomllib` and uses `asyncio.timeout`; both arrived in 3.11)
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

To also run the tests, install `requirements-dev.txt` instead (it includes
`requirements.txt` and adds pytest). The VS Code launch configurations do this
for you through `scripts/setup_venv.sh`.

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests run offline: they need no Azure, Jira or Anthropic access.

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

By default the Azure Advisor MCP server uses your `az login` session
(`AZURE_TOKEN_CREDENTIALS` defaults to `AzureCliCredential`), so run `az login`
first. The server finds the token cache through `HOME`.

The credential is pinned on purpose. With it unset, Azure MCP Server 3.0.0 looks
for an X display on Linux (it connects to `:1`, whatever `DISPLAY` says), and if
that connection is never accepted, every Azure tool call hangs with no error.

For a service principal, set `AZURE_TOKEN_CREDENTIALS=EnvironmentCredential`
together with `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET`
(see `.env.example`). Without the first variable the server keeps using
`az login`. Avoid `dev` and an empty value: both bring back the X display lookup.

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
├── database.py             # SQLite: Advisor finding ↔ Jira issue mappings and the audit log
├── audit.py                # JSONL audit logging (TODO)
└── web/                    # The local web UI (see "Web UI" below)
    ├── __main__.py         #   python -m advisor_agent.web: arguments, address with token, shutdown
    ├── app.py              #   Starlette routes and the order the parts start and stop in
    ├── security.py         #   Host / Origin / cookie checks and the security headers
    ├── azure_direct.py     #   the server's own connection to the Azure MCP server (the table)
    ├── recommendations.py  #   checks the filters, turns Advisor records into table rows
    ├── chat.py             #   one chat run at a time, on a thread of its own
    ├── approvals.py        #   Jira write approvals: the browser's "yes" or "no"
    ├── events.py           #   server-sent events to the open page
    ├── storage.py          #   the database, used from one thread
    ├── predefined.py       #   reads chats.toml for the list of predefined chats
    ├── settings.py         #   one run's settings; the Jira key and URL checks
    ├── errors.py           #   errors that are safe to show in the browser
    └── static/             #   index.html, app.js, app.css (no build step)

chats.toml                  # The predefined chats of the web UI, edited by hand

examples/
├── agent_sdk_demo.py       # Agent usage examples (file name kept; launch.json refers to it)

tests/                      # pytest, offline (run: python -m pytest)
```

### The mapping database

`workspace/advisor.db` (git-ignored) links Advisor findings to Jira issues. A
finding is identified by its recommendation type ID plus the ID of the resource
it applies to, compared without regard to case. The `id` GUID that Advisor puts
on each recommendation is not used as the key because it does not stay the same.
One Jira issue can be linked to one finding only.

The file records its schema version. A file made with the earlier layout (keyed
on the GUID) is not converted, since a GUID does not say which recommendation
type it was; `AdvisorDatabase` refuses to open it and tells you to move it away
so a new one is created.

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

## Web UI

A local web page for the same agent: a table of the Advisor recommendations, the
Jira issue each one is linked to, a chat with the agent, and approval of Jira
writes in the browser. It is a server that keeps running; it is not a one-off
HTML export.

```bash
python -m advisor_agent.web            # or: --port 9000 --model claude-sonnet-5-5
```

In VS Code, use the "Advisor Web UI" launch configuration. The server prints an
address such as

```
http://127.0.0.1:8765/#t=<random token>
```

Open exactly that address. The token after `#t=` signs the browser in (it is
exchanged for a cookie and removed from the address bar). The port must be free;
if it is taken the server says so and ends. Stop it with Ctrl-C.

**What to expect**

* The page opens at once. The first start of the Azure MCP server can take about
  a minute while `uvx` downloads it; the top bar says "starting" until then, and
  "failed" with a Retry button if it cannot start.
* Two Azure MCP server processes run: the web server's own, which fills the
  table directly with no model call, and the chat agent's, which starts on the
  first chat message. Chat is switched off (the table still works) when
  `ANTHROPIC_API_KEY` is missing.
* The table shows up to 100 recommendations per query (the Azure tool's limit).
  Narrow the query with the status, category, impact and subscription filters;
  text search, resource group and sorting work on what was fetched. Answers are
  kept for 30 seconds; Refresh asks Azure again.
* **Link / Unlink** ties a recommendation to a Jira issue key you type, for
  example `ABC-123`. The link is kept in `workspace/advisor.db` under the
  recommendation type and resource (see "The mapping database"), and every
  change is written to the audit log. The key is not looked up in Jira. If
  `JIRA_URL` is set, the key becomes a link to the issue.
* A Jira write the agent wants to make appears as a dialog with the tool and its
  arguments. Nothing is sent to Jira until you click Approve. The call is denied
  if you click Deny, if five minutes pass, if no page has been open for 30
  seconds, if the server is stopping, or if the decision cannot be written to the
  audit log (`jira_tool_approval` rows, with a hash of the arguments).
* Closing and reopening the page loses nothing: the chat, the progress lines and
  waiting approvals are sent again when it reconnects.

**Predefined chats**

The "Predefined chats" list above the message box holds starter prompts. Picking
one puts its text in the message box; you can edit it, and nothing is sent until
you press Send. The agent still asks for your approval in the dialog before any
Jira change.

The list comes from `chats.toml` in the project root. Edit that file, then reload
the page; the server does not need a restart. Each chat is a `[[chat]]` table
with a title (the name in the list) and a prompt (the text for the message box):

```toml
[[chat]]
title = "High-impact recommendations"
prompt = """
List the High-impact Azure Advisor recommendations and what to do about each.
Do not create or change anything in Jira.
"""
```

* A title is one line of at most 80 characters. A prompt has at most 8000
  characters (the same limit as the message box); a `"""` block can span several
  lines. At most 50 chats are used. Other keys are ignored.
* A chat that is written wrongly is skipped, and the page names the problem under
  the list (for example "chats.toml: chat 3 has no prompt."). The other chats
  still work. If the whole file cannot be read, the list is empty and the page
  says why.
* If `chats.toml` does not exist, the list is not shown.

**Security**

The server holds your Azure sign-in and can approve Jira writes, and some of
what it shows (Advisor text, model answers) comes from outside. So:

* It listens on `127.0.0.1` only. There is no option for another address.
* Other users and programs on the machine need the launch token. Requests must
  carry a matching `Host` and `Origin`, a JSON content type for changes, and the
  session cookie (`HttpOnly`, `SameSite=Strict`). Anything else is refused.
* The page is built with `textContent`, never `innerHTML`. A content security
  policy allows only the server's own scripts and styles. Model answers are shown
  as plain text. Jira links come only from the checked key and `JIRA_URL`.
* Errors shown in the browser are short; the detail goes to the terminal.

Anyone who can read the printed address (for example on a shared screen or in a
copied log) can use the server while it runs.

**Tests**

`python -m pytest` also covers the web UI. Besides the usual requests against the
app, `tests/test_web_lifecycle.py` starts the real server in a process of its
own, with fake Azure and Jira servers and a scripted model, presses Ctrl-C while
a chat run, a waiting approval and an open page exist, and checks that it ends
within 15 seconds and leaves no child process behind. No network access is
needed.

## Anthropic API with MCP tools

The agent calls the Claude API directly through the `anthropic` Python SDK. Its
tool loop is the SDK's tool runner (`client.beta.messages.tool_runner`), and the
tools are those of the two MCP servers, which the `mcp` package starts as local
stdio subprocesses. This does not use Claude Code or the Claude Agent SDK, so
settings in `~/.claude/settings.json` have no effect on it.

✅ **Agent loop**: Claude decides when to call tools; the tool runner executes them and feeds the results back until Claude is done (at most 25 model turns per query)
✅ **MCP tools only**: Azure Advisor (`azure_mcp.py`) and, if configured, Jira (`jira_mcp.py`); `agent.py` composes both. The model has no file, shell or web tools
✅ **Sessions**: `AdvisorAgent` starts the MCP servers on the first query and keeps them, and the conversation, for its lifetime. A follow-up `query()` sees earlier answers; `reset_conversation()` forgets them
✅ **Approval gates**: Azure Advisor tools run freely: the server is started with `--read-only`, and the commands its `advisor` tool lists (checked for version 3.0.0) all read. Of the Jira tools only `jira_search`, `jira_get_issue` and `jira_get_transitions` run freely; every other Jira tool, including any the agent doesn't know about, asks for approval for each call and is not sent to the server without a "yes". The command line asks on the terminal, the web UI in the browser (`mcp_tools.py`, `jira_mcp.py`)
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
