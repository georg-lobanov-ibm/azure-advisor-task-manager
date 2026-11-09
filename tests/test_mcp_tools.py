"""The approval gate: what it lets through, and what the result observer is told."""

import asyncio

import pytest
from anthropic.lib.tools import BetaAsyncBuiltinFunctionTool, ToolError

from advisor_agent.mcp_tools import ApprovalGatedTool, ApprovalRequest

ANSWER = [{"type": "text", "text": "done"}]


class StubTool(BetaAsyncBuiltinFunctionTool):
    """What ``async_mcp_tool`` returns, minus the MCP session."""

    def __init__(self, *, result=ANSWER, fail: Exception | None = None) -> None:
        self.result = result
        self.fail = fail
        self.calls: list[object] = []

    def to_dict(self):
        return {"name": "jira_create_issue", "description": "d", "input_schema": {"type": "object"}}

    async def call(self, input):
        self.calls.append(input)
        if self.fail is not None:
            raise self.fail
        return self.result


def gate(inner, approve, observe=None):
    return ApprovalGatedTool(inner, server="jira", approve=approve, observe=observe)


async def yes(request):
    return True


async def no(request):
    return False


async def broken(request):
    raise RuntimeError("the approval dialog crashed")


def call(tool, arguments=None):
    return asyncio.run(tool.call({"summary": "s"} if arguments is None else arguments))


def test_the_observer_is_told_the_request_and_the_result_of_an_approved_call():
    inner, seen = StubTool(), []

    result = call(gate(inner, yes, lambda request, answer: seen.append((request, answer))))

    assert result is ANSWER  # handed back as it came
    assert seen == [(ApprovalRequest("jira", "jira_create_issue", {"summary": "s"}), ANSWER)]
    assert inner.calls == [{"summary": "s"}]


def test_a_call_without_an_observer_works_as_before():
    assert call(gate(StubTool(), yes)) is ANSWER


def test_a_denied_call_is_not_made_and_not_observed():
    inner, seen = StubTool(), []

    with pytest.raises(ToolError, match="Denied"):
        call(gate(inner, no, lambda *args: seen.append(args)))

    assert inner.calls == [] and seen == []


def test_an_approval_that_fails_denies_the_call_and_is_not_observed():
    inner, seen = StubTool(), []

    with pytest.raises(ToolError, match="Approval could not be obtained"):
        call(gate(inner, broken, lambda *args: seen.append(args)))

    assert inner.calls == [] and seen == []


@pytest.mark.parametrize("answer", [1, "yes", None, [True]])
def test_only_a_literal_true_approves_and_anything_else_is_not_observed(answer):
    async def approve(request):
        return answer

    inner, seen = StubTool(), []

    with pytest.raises(ToolError, match="Denied"):
        call(gate(inner, approve, lambda *args: seen.append(args)))

    assert inner.calls == [] and seen == []


def test_a_call_that_fails_is_not_observed():
    inner, seen = StubTool(fail=ToolError("Jira said no")), []

    with pytest.raises(ToolError, match="Jira said no"):
        call(gate(inner, yes, lambda *args: seen.append(args)))

    assert len(inner.calls) == 1 and seen == []


def test_a_malformed_input_is_refused_before_anything_else():
    inner, seen = StubTool(), []

    async def never(request):
        raise AssertionError("approval must not be asked for an input that is not an object")

    with pytest.raises(ToolError, match="JSON object"):
        call(gate(inner, never, lambda *args: seen.append(args)), arguments=["not", "an", "object"])

    assert inner.calls == [] and seen == []


def test_an_observer_that_raises_does_not_change_the_result(capsys):
    def observe(request, answer):
        raise ValueError("secret detail that should not be printed")

    assert call(gate(StubTool(), yes, observe)) is ANSWER

    warning = capsys.readouterr().err
    assert "jira:jira_create_issue" in warning and "ValueError" in warning
    assert "secret detail" not in warning


def test_the_observer_gets_a_copy_of_the_arguments():
    """What the observer does to its request cannot change what was sent."""
    inner = StubTool()
    sent = {"summary": "s"}

    def observe(request, answer):
        request.arguments["summary"] = "changed"

    call(gate(inner, yes, observe), arguments=sent)

    assert sent == {"summary": "s"} and inner.calls == [{"summary": "s"}]
