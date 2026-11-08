"""Reading the key of a created issue from the answer of ``jira_create_issue``."""

import json

import pytest

from advisor_agent.jira_mcp import created_issue_key

CREATED = {"message": "Issue created successfully", "issue": {"key": "ABC-123", "summary": "s"}}


def blocks(*texts):
    return [{"type": "text", "text": text} for text in texts]


def test_the_key_is_read_from_a_list_of_text_blocks():
    assert created_issue_key(blocks(json.dumps(CREATED))) == "ABC-123"


def test_the_key_is_read_from_a_plain_string():
    assert created_issue_key(json.dumps(CREATED)) == "ABC-123"


def test_text_split_over_several_blocks_is_joined():
    text = json.dumps(CREATED)

    assert created_issue_key(blocks(text[:20], text[20:])) == "ABC-123"


def test_blocks_that_are_not_text_are_skipped():
    result = [{"type": "image", "source": {}}, *blocks(json.dumps(CREATED)), "stray"]

    assert created_issue_key(result) == "ABC-123"


def test_a_bare_issue_object_is_accepted():
    assert created_issue_key(json.dumps({"key": "ABC-1"})) == "ABC-1"


def test_the_key_is_returned_unchecked():
    """Whether it is a Jira key is for the caller to decide."""
    assert created_issue_key(json.dumps({"issue": {"key": "../../etc"}})) == "../../etc"


@pytest.mark.parametrize(
    "result",
    [
        None,
        5,
        {"issue": {"key": "ABC-1"}},  # an object, not the text of one
        [],
        "",
        "Issue created, but this is not JSON.",
        blocks("Issue created", " but not JSON"),
        json.dumps(["ABC-1"]),
        json.dumps("ABC-1"),
        json.dumps(None),
        json.dumps({"issue": "ABC-1"}),
        json.dumps({"issue": ["ABC-1"]}),
        json.dumps({"issue": {}}),
        json.dumps({"issue": {"id": "10001"}}),
        json.dumps({"issue": {"key": 123}}),
        json.dumps({"issue": {"key": None}}),
        json.dumps({"issue": {"key": ["ABC-1"]}}),
        [{"type": "text", "text": None}],
        [{"type": "text"}],
        [{"type": "text", "text": json.dumps(CREATED)}, {"type": "text", "text": 5}],
    ],
)
def test_anything_that_is_not_a_create_answer_gives_none(result):
    assert created_issue_key(result) is None


def test_text_over_the_limit_is_not_parsed():
    padding = "x" * 1_000_000
    text = json.dumps({"issue": {"key": "ABC-1"}, "pad": padding})

    assert created_issue_key(text) is None


def test_text_just_under_the_limit_is_still_read():
    text = json.dumps({"issue": {"key": "ABC-1"}})
    padded = text[:-1] + ',"pad":"' + "x" * (1_000_000 - len(text) - 20) + '"}'

    assert len(padded) <= 1_000_000
    assert created_issue_key(padded) == "ABC-1"


def test_deeply_nested_json_is_not_a_crash():
    text = "[" * 100_000 + "]" * 100_000

    assert created_issue_key(text) is None
