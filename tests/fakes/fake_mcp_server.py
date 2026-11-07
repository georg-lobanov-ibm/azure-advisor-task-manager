"""A stand-in MCP server for tests: no Azure, no Jira, no network.

Run as ``python fake_mcp_server.py <kind>`` where ``kind`` is ``azure`` or
``jira``. ``azure`` offers the same tools as the real server in namespace mode
(the ``advisor`` router, ``subscription_list``, ...), ``jira`` offers one read
tool and one write tool.

Behaviour is switched through environment variables, so one script covers
every failure the tests need:

``FAKE_MCP_PID_FILE``
    Write this process's pid there at start (the tests check it is gone later).
``FAKE_MCP_HANG_START``
    Sleep for ever instead of serving, so ``initialize`` is never answered.
``FAKE_MCP_CRASH_START``
    Exit with status 3 at once.
``FAKE_MCP_HANG_CALL``
    Every tool call sleeps for ever.
``FAKE_MCP_CALL_LOG``
    Append one line per tool call that reaches the server. A denied Jira
    write must never show up there.
``FAKE_MCP_RECOMMENDATIONS``
    Path of a JSON file whose content is the list returned by
    ``advisor_recommendation_list`` (default: two fixed recommendations).
"""

import json
import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

DEFAULT_RECOMMENDATIONS = [
    {
        "id": "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-a"
        "/providers/Microsoft.Compute/virtualMachines/vm-a/providers/Microsoft.Advisor"
        "/recommendations/11111111-1111-1111-1111-111111111111",
        "name": "11111111-1111-1111-1111-111111111111",
        "type": "Microsoft.Advisor/recommendations",
        "properties": {
            "category": "Cost",
            "impact": "High",
            "impactedField": "Microsoft.Compute/virtualMachines",
            "impactedValue": "vm-a",
            "recommendationStatus": "New",
            "lastUpdated": "2026-01-02T03:04:05Z",
            "recommendationTypeId": "e10b1381-5f0a-47ff-8c7b-37bd4eaea707",
            "shortDescription": {"problem": "VM is underused", "solution": "Resize the VM"},
            "resourceMetadata": {
                "resourceId": "/subscriptions/00000000-0000-0000-0000-000000000001"
                "/resourceGroups/rg-a/providers/Microsoft.Compute/virtualMachines/vm-a"
            },
        },
    },
    {
        "id": "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-b"
        "/providers/Microsoft.Storage/storageAccounts/sa-b/providers/Microsoft.Advisor"
        "/recommendations/22222222-2222-2222-2222-222222222222",
        "name": "22222222-2222-2222-2222-222222222222",
        "type": "Microsoft.Advisor/recommendations",
        "properties": {
            "category": "Security",
            "impact": "Medium",
            "impactedField": "Microsoft.Storage/storageAccounts",
            "impactedValue": "sa-b",
            "recommendationStatus": "New",
            "recommendationTypeId": "9a1b2c3d-0000-4000-8000-000000000002",
            "shortDescription": {"problem": "Public access is on", "solution": "Turn it off"},
            "resourceMetadata": {
                "resourceId": "/subscriptions/00000000-0000-0000-0000-000000000001"
                "/resourceGroups/rg-b/providers/Microsoft.Storage/storageAccounts/sa-b"
            },
        },
    },
]


def _log_call(name: str, arguments: dict) -> None:
    path = os.environ.get("FAKE_MCP_CALL_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"tool": name, "arguments": arguments}, sort_keys=True) + "\n")


def _maybe_hang() -> None:
    if os.environ.get("FAKE_MCP_HANG_CALL"):
        time.sleep(3600)


def build_server(kind: str) -> MCPServer:
    server = MCPServer(f"fake-{kind}")

    if kind == "azure":

        @server.tool()
        def advisor(
            intent: str,
            command: str | None = None,
            parameters: dict | None = None,
            learn: bool = False,
        ) -> str:
            """Router for the Advisor commands."""
            _log_call("advisor", {"intent": intent, "command": command, "parameters": parameters})
            _maybe_hang()
            if learn:
                return json.dumps([{"command": "advisor_recommendation_list"}])
            if command != "advisor_recommendation_list":
                return json.dumps({"status": 400, "message": f"unknown command {command!r}"})
            path = os.environ.get("FAKE_MCP_RECOMMENDATIONS")
            if path:
                items = json.loads(Path(path).read_text(encoding="utf-8"))
            else:
                items = DEFAULT_RECOMMENDATIONS
            top = (parameters or {}).get("top")
            if isinstance(top, int):
                items = items[:top]
            return json.dumps(
                {
                    "status": 200,
                    "message": "Success",
                    "results": {"recommendations": items, "areResultsTruncated": False},
                }
            )

        @server.tool()
        def subscription_list() -> str:
            """List subscriptions."""
            _log_call("subscription_list", {})
            _maybe_hang()
            return json.dumps(
                {
                    "status": 200,
                    "results": {
                        "subscriptions": [
                            {
                                "subscriptionId": "00000000-0000-0000-0000-000000000001",
                                "displayName": "Test subscription",
                            }
                        ]
                    },
                }
            )

    elif kind == "jira":

        @server.tool()
        def jira_search(jql: str) -> str:
            """Search issues (a read tool)."""
            _log_call("jira_search", {"jql": jql})
            _maybe_hang()
            return json.dumps({"issues": []})

        @server.tool()
        def jira_create_issue(project_key: str, summary: str) -> str:
            """Create an issue (a write tool: needs approval)."""
            _log_call("jira_create_issue", {"project_key": project_key, "summary": summary})
            _maybe_hang()
            return json.dumps({"key": "TEST-1"})

    else:
        raise SystemExit(f"unknown kind {kind!r}; use 'azure' or 'jira'")

    return server


def main() -> None:
    kind = sys.argv[1] if len(sys.argv) > 1 else "azure"

    pid_file = os.environ.get("FAKE_MCP_PID_FILE")
    if pid_file:
        Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")

    if os.environ.get("FAKE_MCP_CRASH_START"):
        raise SystemExit(3)
    if os.environ.get("FAKE_MCP_HANG_START"):
        time.sleep(3600)

    build_server(kind).run("stdio")


if __name__ == "__main__":
    main()
