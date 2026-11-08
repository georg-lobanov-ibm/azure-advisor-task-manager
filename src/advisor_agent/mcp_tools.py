"""
Generic MCP plumbing for the Anthropic tool runner.

The agent talks to the Claude API directly (``anthropic`` SDK) and runs the
tool loop with ``client.beta.messages.tool_runner``. This module turns a local
stdio MCP server into runner-ready tools and puts a human-approval gate in
front of the ones that can change something. It knows nothing about Azure or
Jira: ``azure_mcp.py`` and ``jira_mcp.py`` describe their servers as
``McpServerConfig`` values and ``agent.py`` composes them.

Approval gating
----------------
Each ``McpServerConfig`` carries a ``requires_approval(tool_name)`` predicate.
A tool it flags is wrapped in ``ApprovalGatedTool``: before the call reaches
the MCP server the ``ApprovalCallback`` is asked, and on anything other than a
literal ``True`` the server is never called and the model gets an error result
telling it the call was denied. The gate lives in the tool object itself, so
it applies to every path the runner can take to the tool.

A gated tool can also be given a ``ToolResultObserver``. It is told about each
call that was approved and that the server answered without an error, with the
answer, so the caller can act on what was written (the web UI links a Story it
created to the recommendation it was created for).

Environment of the server subprocesses
--------------------------------------
``mcp``'s ``stdio_client`` starts the server with only a handful of variables
(HOME, LOGNAME, PATH, SHELL, TERM, USER) plus whatever is put in
``StdioServerParameters.env``. Credentials and proxy settings must therefore
be forwarded explicitly (see ``forward_env``). This is also why the servers
never see ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, TextIO

from anthropic.lib.tools import BetaAsyncBuiltinFunctionTool, BetaAsyncFunctionTool, ToolError
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import PaginatedRequestParams

# How long one server gets to start and list its tools. The first `uvx` run of
# a server downloads its package (about 60 MB for the Azure one), which can
# take a while.
DEFAULT_STARTUP_TIMEOUT_SECONDS = 120.0

# Tool names the Messages API accepts.
_API_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# list_tools() is paginated; a server that keeps returning a cursor forever
# should not hang startup.
_MAX_TOOL_PAGES = 100

# Proxy and CA settings that the minimal subprocess environment would drop.
# Forwarded to every server so package downloads and outbound calls keep
# working behind a corporate proxy.
NETWORK_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)


class McpServerStartError(RuntimeError):
    """An MCP server could not be started or did not answer in time."""


@dataclass(frozen=True)
class McpServerConfig:
    """How to start one stdio MCP server and which of its tools need approval."""

    name: str
    params: StdioServerParameters
    # Called with the bare MCP tool name. Return True to put the tool behind
    # human approval.
    requires_approval: Callable[[str], bool]
    # Shown when the command is not on PATH, e.g. "install uv (uvx)".
    install_hint: str = ""


@dataclass(frozen=True)
class ApprovalRequest:
    """One pending tool call, as shown to the human reviewer."""

    server: str
    tool: str
    arguments: dict[str, Any]


ApprovalCallback = Callable[[ApprovalRequest], Awaitable[bool]]

# Called with an approved request and the tool's result, after the call
# succeeded. It runs on the agent's event loop, so it must return quickly; an
# exception it raises is reported and does not change the result.
ToolResultObserver = Callable[[ApprovalRequest, Any], None]


@dataclass(frozen=True)
class McpTool:
    """A runner-ready tool plus where it came from."""

    server: str
    name: str
    requires_approval: bool
    runnable: BetaAsyncFunctionTool[Any] | "ApprovalGatedTool"


def forward_env(*, prefixes: Iterable[str] = (), names: Iterable[str] = ()) -> dict[str, str]:
    """Copy the set, non-empty variables that match ``prefixes`` or ``names``.

    Plus ``NETWORK_ENV_VARS``. Used to build ``StdioServerParameters.env``.
    """
    prefixes = tuple(prefixes)
    wanted = {*names, *NETWORK_ENV_VARS}
    return {
        key: value
        for key, value in os.environ.items()
        if value and (key in wanted or key.startswith(prefixes))
    }


class ApprovalGatedTool(BetaAsyncBuiltinFunctionTool):
    """Wraps a runner tool so a human must approve each call first."""

    def __init__(
        self,
        inner: BetaAsyncFunctionTool[Any],
        *,
        server: str,
        approve: ApprovalCallback,
        observe: ToolResultObserver | None = None,
    ) -> None:
        self._inner = inner
        self.server = server
        self._approve = approve
        self._observe = observe

    def to_dict(self) -> Any:
        return self._inner.to_dict()

    async def call(self, input: object) -> Any:
        if not isinstance(input, dict):
            raise ToolError("Tool input must be a JSON object; the call was not made.")

        request = ApprovalRequest(server=self.server, tool=self.name, arguments=dict(input))
        try:
            approved = await self._approve(request)
        except Exception as exc:
            # Fail closed: a broken approval path must never let a call through.
            raise ToolError(
                f"Approval could not be obtained ({type(exc).__name__}); the call was not made."
            ) from exc

        # Only a literal True approves; any other return value denies.
        if approved is not True:
            raise ToolError("Denied by human reviewer. The call was not made; do not retry it.")

        result = await self._inner.call(input)
        if self._observe is not None:
            try:
                self._observe(request, result)
            except Exception as exc:
                # The call has happened; what the observer does with it must not undo that.
                print(
                    f"Warning: handling the result of {self.server}:{self.name} failed: "
                    f"{type(exc).__name__}",
                    file=sys.stderr,
                )
        return result


def _server_errlog() -> TextIO:
    """Where the servers' stderr goes: this process's stderr, if it is a real file.

    ``stdio_client`` hands ``errlog`` to the subprocess, which needs a file
    descriptor. ``sys.stderr`` can be replaced by an object without one (an
    IDE console or a test runner capturing output); the original stderr is
    used then, instead of failing with a misleading "cannot start" error.
    """
    for stream in (sys.stderr, sys.__stderr__):
        try:
            stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue
        return stream
    return sys.stderr


_prompt_lock = threading.Lock()


async def cli_approval_callback(request: ApprovalRequest) -> bool:
    """Ask on the terminal whether to allow ``request``. Denies when not interactive.

    The details go to stderr so redirecting the agent's answer (stdout) to a
    file does not hide the question. Arguments are shown through
    ``json.dumps`` with ASCII escaping, so text coming from untrusted
    recommendations cannot smuggle terminal control sequences onto the screen.
    Without a TTY on stdin and stderr (scheduled or CI runs) nobody can answer,
    so the call is denied.
    """
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        print(
            f"Denied {request.server}:{request.tool}: approval needs an interactive terminal.",
            file=sys.stderr,
        )
        return False

    print(
        f"\nApproval required: {request.server}:{request.tool}\n"
        f"{json.dumps(request.arguments, indent=2, ensure_ascii=True)}\n"
        "Allow this call? [y/N] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    answer = await _read_line_on_daemon_thread()
    return answer.strip().lower() in {"y", "yes"}


async def _read_line_on_daemon_thread() -> str:
    """``input()`` on a daemon thread.

    ``run_in_executor`` threads are joined at interpreter exit, so a Ctrl-C
    while waiting at the prompt would leave the process hanging on ``input()``.
    A daemon thread does not.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()

    def deliver(setter: Callable[[Any], None], value: Any) -> None:
        if not future.done():
            setter(value)

    def read() -> None:
        try:
            with _prompt_lock:
                line = input()
        except BaseException as exc:  # EOFError, KeyboardInterrupt, ...
            outcome: tuple[Callable[[Any], None], Any] = (future.set_exception, exc)
        else:
            outcome = (future.set_result, line)
        try:
            loop.call_soon_threadsafe(deliver, *outcome)
        except RuntimeError:
            pass  # the loop is already closed; nobody is waiting any more

    threading.Thread(target=read, name="advisor-approval-prompt", daemon=True).start()
    return await future


async def open_mcp_session(
    stack: AsyncExitStack,
    config: McpServerConfig,
    *,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> tuple[ClientSession, list[Any]]:
    """Start ``config``'s server on ``stack``; return its session and raw MCP tools.

    ``stack`` must be entered and closed in the same asyncio task: the
    transport and session use task groups that cannot be left from a
    different task (``AdvisorAgent`` and the web server's ``AzureDirect`` each
    run one owner task for exactly this). Raises ``McpServerStartError`` with
    a readable message on failure; whatever was opened stays on ``stack`` and
    is closed when the caller leaves it.
    """
    try:
        read, write = await stack.enter_async_context(
            stdio_client(config.params, errlog=_server_errlog())
        )
    except OSError as exc:
        hint = f" {config.install_hint}" if config.install_hint else ""
        raise McpServerStartError(
            f"MCP server '{config.name}': cannot start '{config.params.command}' "
            f"({exc.strerror or exc}).{hint}"
        ) from exc

    session = await stack.enter_async_context(ClientSession(read, write))

    try:
        mcp_tools = await asyncio.wait_for(_initialize_and_list_tools(session), startup_timeout)
    except asyncio.TimeoutError as exc:
        raise McpServerStartError(
            f"MCP server '{config.name}' did not finish starting within {startup_timeout:.0f}s."
        ) from exc
    except Exception as exc:
        raise McpServerStartError(
            f"MCP server '{config.name}' failed to start: {type(exc).__name__}: {exc}"
        ) from exc
    return session, mcp_tools


async def start_mcp_server(
    stack: AsyncExitStack,
    config: McpServerConfig,
    approve: ApprovalCallback,
    *,
    observe: ToolResultObserver | None = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> list[McpTool]:
    """Start ``config``'s server, keep it alive on ``stack`` and return its tools.

    Same rules as ``open_mcp_session``, which does the work; this adds the
    runner-ready wrapping and the approval gate. ``observe`` is given to every
    gated tool (see ``ToolResultObserver``).
    """
    session, mcp_tools = await open_mcp_session(stack, config, startup_timeout=startup_timeout)

    tools: list[McpTool] = []
    for mcp_tool in mcp_tools:
        if not _API_TOOL_NAME.match(mcp_tool.name):
            # The API would reject the whole request over one such name.
            print(
                f"Warning: skipping tool {mcp_tool.name!r} from MCP server '{config.name}': "
                "name is not valid for the Claude API.",
                file=sys.stderr,
            )
            continue

        runnable: BetaAsyncFunctionTool[Any] | ApprovalGatedTool = async_mcp_tool(mcp_tool, session)
        gated = bool(config.requires_approval(mcp_tool.name))
        if gated:
            runnable = ApprovalGatedTool(runnable, server=config.name, approve=approve, observe=observe)
        tools.append(
            McpTool(server=config.name, name=mcp_tool.name, requires_approval=gated, runnable=runnable)
        )
    return tools


async def _initialize_and_list_tools(session: ClientSession) -> list[Any]:
    await session.initialize()

    tools: list[Any] = []
    cursor: str | None = None
    for _ in range(_MAX_TOOL_PAGES):
        page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            return tools
    raise RuntimeError(f"server kept returning a pagination cursor after {_MAX_TOOL_PAGES} pages")
