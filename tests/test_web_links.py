"""Linking a finding to a Jira issue by hand, and unlinking it."""

import json

import pytest

from advisor_agent.web import settings as web_settings
from fakes.web_harness import fake_agent

VM_TYPE = "e10b1381-5f0a-47ff-8c7b-37bd4eaea707"
VM_RESOURCE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-a"
    "/providers/Microsoft.Compute/virtualMachines/vm-a"
)
SA_TYPE = "9a1b2c3d-0000-4000-8000-000000000002"
SA_RESOURCE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-b"
    "/providers/Microsoft.Storage/storageAccounts/sa-b"
)
VM = {"recommendation_type_id": VM_TYPE, "resource_id": VM_RESOURCE}


def link_body(jira_key="abc-1", **extra):
    return {**VM, "jira_key": jira_key, "category": "Cost", "impact": "High", **extra}


def test_link_then_list_then_unlink(web):
    with web() as app:
        app.wait_azure()

        created = app.send("PUT", "/api/links", link_body())
        assert created.status_code == 200
        link = created.json()["link"]
        assert link["jira_key"] == "ABC-1"  # upper-cased
        assert link["recommendation_type_id"] == VM_TYPE and link["resource_id"] == VM_RESOURCE

        assert [item["jira_key"] for item in app.get("/api/links").json()["links"]] == ["ABC-1"]

        rows = {row["resource_name"]: row for row in app.get("/api/recommendations").json()["rows"]}
        assert rows["vm-a"]["link"]["jira_key"] == "ABC-1"
        assert rows["sa-b"]["link"] is None

        removed = app.send("DELETE", "/api/links", VM)
        assert removed.status_code == 200 and removed.json()["removed"] is True
        assert app.get("/api/links").json()["links"] == []
        rows = app.get("/api/recommendations").json()["rows"]
        assert all(row["link"] is None for row in rows)


def test_whitespace_around_a_typed_key_is_dropped_before_it_is_stored(web):
    with web() as app:
        response = app.send("PUT", "/api/links", link_body("  abc-5\n"))

        assert response.status_code == 200
        assert [item["jira_key"] for item in app.get("/api/links").json()["links"]] == ["ABC-5"]


def test_the_resource_id_matches_regardless_of_case(web):
    with web() as app:
        app.send("PUT", "/api/links", link_body())

        response = app.send("PUT", "/api/links", {**link_body("ABC-2"), "resource_id": VM_RESOURCE.upper()})

    assert response.status_code == 409
    assert "already linked to ABC-1" in response.json()["error"]


def test_a_finding_that_is_linked_cannot_be_linked_again(web):
    with web() as app:
        app.send("PUT", "/api/links", link_body("ABC-1"))

        response = app.send("PUT", "/api/links", link_body("ABC-2"))

        assert response.status_code == 409
        assert [item["jira_key"] for item in app.get("/api/links").json()["links"]] == ["ABC-1"]


def test_an_issue_cannot_be_linked_to_two_findings(web):
    with web() as app:
        app.send("PUT", "/api/links", link_body("ABC-1"))

        response = app.send(
            "PUT", "/api/links", {"recommendation_type_id": SA_TYPE, "resource_id": SA_RESOURCE, "jira_key": "ABC-1"}
        )

        assert response.status_code == 409
        assert "already linked to another finding" in response.json()["error"]


@pytest.mark.parametrize(
    "key",
    ["", "ABC", "ABC-", "ABC-0", "ABC-01", "A-1", "1ABC-1", "ABC 1", "ABC-1; DROP", "<script>-1", "ABC-1-2", "é-1", "A" * 70 + "-1"],
)
def test_a_value_that_is_not_a_jira_key_is_a_400(web, key):
    with web() as app:
        response = app.send("PUT", "/api/links", link_body(key))

        assert response.status_code == 400
        assert app.get("/api/links").json()["links"] == []
        assert app.audit("manual_link") == []


@pytest.mark.parametrize(
    "body",
    [
        {"resource_id": VM_RESOURCE, "jira_key": "ABC-1"},
        {"recommendation_type_id": VM_TYPE, "jira_key": "ABC-1"},
        {"recommendation_type_id": VM_TYPE, "resource_id": VM_RESOURCE},
        {"recommendation_type_id": 5, "resource_id": VM_RESOURCE, "jira_key": "ABC-1"},
        {"recommendation_type_id": VM_TYPE, "resource_id": ["x"], "jira_key": "ABC-1"},
        {"recommendation_type_id": VM_TYPE, "resource_id": VM_RESOURCE, "jira_key": 12},
        {"recommendation_type_id": VM_TYPE, "resource_id": "x" * 2000, "jira_key": "ABC-1"},
        {"recommendation_type_id": VM_TYPE, "resource_id": "line\nbreak", "jira_key": "ABC-1"},
        {**VM, "jira_key": "ABC-1", "subscription_id": "not-a-guid"},
    ],
)
def test_incomplete_or_odd_fields_are_a_400(web, body):
    with web() as app:
        response = app.send("PUT", "/api/links", body)

    assert response.status_code == 400


def test_unlinking_something_that_is_not_linked_is_a_404(web):
    with web() as app:
        response = app.send("DELETE", "/api/links", VM)

    assert response.status_code == 404
    assert app.audit("manual_unlink") == []


def test_linking_and_unlinking_are_written_to_the_audit_log(web):
    with web() as app:
        app.send("PUT", "/api/links", link_body("ABC-7", advisor_id="adv-1"))
        app.send("DELETE", "/api/links", VM)

        linked, unlinked = app.audit("manual_link"), app.audit("manual_unlink")

    assert len(linked) == 1 and len(unlinked) == 1
    assert linked[0]["jira_key"] == "ABC-7" and linked[0]["decision"] == "link"
    assert linked[0]["recommendation_type_id"] == VM_TYPE and linked[0]["resource_id"] == VM_RESOURCE
    assert json.loads(linked[0]["metadata"]) == {"source": "web_ui"}
    assert unlinked[0]["jira_key"] == "ABC-7" and unlinked[0]["decision"] == "unlink"
    assert unlinked[0]["advisor_id"] == "adv-1"


def test_a_link_whose_audit_row_cannot_be_written_is_not_kept(web, monkeypatch):
    with web() as app:

        def no_audit(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(app.services.storage.db, "log_audit", no_audit)

        response = app.send("PUT", "/api/links", link_body())

        assert response.status_code == 500
        assert app.get("/api/links").json()["links"] == []


def test_a_change_is_pushed_to_open_pages(web):
    with web() as app:
        with app.watching() as watcher:
            app.send("PUT", "/api/links", link_body("ABC-3"))
            app.send("DELETE", "/api/links", VM)

    changes = watcher.of("link_changed")
    assert [(item["link"]["jira_key"], item["removed"]) for item in changes] == [("ABC-3", False), ("ABC-3", True)]


def test_a_second_tab_sees_a_link_made_in_the_first(web):
    with web() as app:
        app.send("PUT", "/api/links", link_body("ABC-4"))

        links = app.get("/api/links").json()["links"]

    assert links[0]["jira_key"] == "ABC-4"


# -- the helpers that decide what may become a link ---------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://example.atlassian.net", "https://example.atlassian.net"),
        ("https://example.atlassian.net/", "https://example.atlassian.net"),
        ("  http://jira.local:8080/jira/  ", "http://jira.local:8080/jira"),
        ("https://[::1]:8443", "https://[::1]:8443"),
        ("javascript:alert(1)", None),
        ("data:text/html,<script>", None),
        ("ftp://example.com", None),
        ("//example.com", None),
        ("https://user:pw@example.com", None),
        ("https://example.com?x=1", None),
        ("https://example.com#frag", None),
        ("https://", None),
        ("https://example.com:99999", None),
        ("", None),
        (None, None),
    ],
)
def test_jira_base_url(value, expected):
    assert web_settings.jira_base_url(value) == expected


@pytest.mark.parametrize("key", ["ABC-1", "A1-20", "AB_C-1", "ABC-999999"])
def test_valid_jira_keys(key):
    assert web_settings.valid_jira_key(key)


@pytest.mark.parametrize("key", ["abc-1", "ABC-0", "ABC-01", "ABC-1 ", "ABC-1\n", "-1", "ABC", None, 5, "A-1"])
def test_invalid_jira_keys(key):
    assert not web_settings.valid_jira_key(key)


def test_jira_issue_url_is_built_only_from_checked_parts():
    base = "https://example.atlassian.net"
    assert web_settings.jira_issue_url(base, "ABC-1") == "https://example.atlassian.net/browse/ABC-1"
    assert web_settings.jira_issue_url(base, "ABC-1/../../x") is None
    assert web_settings.jira_issue_url(None, "ABC-1") is None
    assert web_settings.jira_issue_url(base, "bad") is None


def test_the_snapshot_carries_the_jira_base_url_for_links(web):
    _, factory = fake_agent()
    with web(agent_factory=factory, jira_base_url="https://example.atlassian.net") as app:
        with app.watching() as watcher:
            pass

    assert watcher.events[0][1]["jira_base_url"] == "https://example.atlassian.net"
