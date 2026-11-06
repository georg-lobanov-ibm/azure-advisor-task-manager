"""Tests for the Advisor ↔ Jira mapping database (``advisor_agent.database``)."""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from advisor_agent.database import AdvisorDatabase, DatabaseSchema, DatabaseSchemaError

TYPE_ID = "e2f5a1b3-1111-4c2d-9a7e-0123456789ab"
RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-demo"
    "/providers/Microsoft.Compute/virtualMachines/vm-demo"
)
OTHER_RESOURCE_ID = RESOURCE_ID.replace("vm-demo", "vm-other")

# The schema the code shipped before the composite key: keyed on the Advisor
# GUID, and `PRAGMA user_version` never set.
V1_SCHEMA = """
CREATE TABLE advisor_jira_mapping (
    advisor_id TEXT PRIMARY KEY,
    jira_issue_key TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    impact TEXT, category TEXT, resource_id TEXT, subscription_id TEXT,
    fix_evaluation TEXT, last_synced TEXT, notes TEXT
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, action TEXT NOT NULL,
    advisor_id TEXT, jira_key TEXT, decision TEXT, rationale TEXT, user_approval BOOLEAN,
    metadata TEXT
);
"""

UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "advisor.db"


@pytest.fixture
def db(db_path):
    return AdvisorDatabase(str(db_path))


def _parse_utc(value: str) -> datetime:
    assert UTC_TIMESTAMP.match(value), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_new_file_gets_current_schema_version(db, db_path):
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DatabaseSchema.SCHEMA_VERSION


def test_reopening_a_current_file_keeps_its_data(db, db_path):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1")

    reopened = AdvisorDatabase(str(db_path))

    assert reopened.get_mapping(TYPE_ID, RESOURCE_ID)["jira_issue_key"] == "AZURE-1"


def test_create_and_get_mapping_round_trip(db):
    created = db.create_mapping(
        TYPE_ID,
        RESOURCE_ID,
        "AZURE-123",
        advisor_id="guid-1",
        category="Cost",
        impact="High",
        subscription_id="00000000-0000-0000-0000-000000000000",
        notes="first",
    )

    assert created == db.get_mapping(TYPE_ID, RESOURCE_ID)
    assert created["recommendation_type_id"] == TYPE_ID
    assert created["resource_id"] == RESOURCE_ID
    assert created["advisor_id"] == "guid-1"
    assert created["jira_issue_key"] == "AZURE-123"
    assert created["status"] == "open"
    assert created["category"] == "Cost"
    assert created["impact"] == "High"
    assert created["notes"] == "first"
    assert created["created_at"] == created["last_synced"]


def test_get_mapping_returns_none_when_missing(db):
    assert db.get_mapping(TYPE_ID, RESOURCE_ID) is None
    assert db.get_mapping_by_jira_key("AZURE-1") is None


def test_key_is_case_insensitive(db):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1")

    found = db.get_mapping(TYPE_ID.upper(), RESOURCE_ID.lower())

    assert found is not None
    assert found["jira_issue_key"] == "AZURE-1"
    with pytest.raises(sqlite3.IntegrityError):
        db.create_mapping(TYPE_ID.upper(), RESOURCE_ID.upper(), "AZURE-2")


def test_same_type_on_another_resource_is_a_separate_finding(db):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1")
    db.create_mapping(TYPE_ID, OTHER_RESOURCE_ID, "AZURE-2")

    assert db.get_mapping(TYPE_ID, RESOURCE_ID)["jira_issue_key"] == "AZURE-1"
    assert db.get_mapping(TYPE_ID, OTHER_RESOURCE_ID)["jira_issue_key"] == "AZURE-2"


def test_duplicate_finding_is_rejected(db):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1", advisor_id="guid-1")

    # Advisor handed out a new GUID for the same finding: still a duplicate.
    with pytest.raises(sqlite3.IntegrityError):
        db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-2", advisor_id="guid-2")

    assert db.get_mapping(TYPE_ID, RESOURCE_ID)["jira_issue_key"] == "AZURE-1"


def test_duplicate_jira_key_is_rejected(db):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1")

    with pytest.raises(sqlite3.IntegrityError):
        db.create_mapping(TYPE_ID, OTHER_RESOURCE_ID, "AZURE-1")

    assert db.get_mapping(TYPE_ID, OTHER_RESOURCE_ID) is None


@pytest.mark.parametrize(
    "type_id, resource_id",
    [("", RESOURCE_ID), ("   ", RESOURCE_ID), (TYPE_ID, ""), (TYPE_ID, None)],
)
def test_blank_key_part_is_rejected(db, type_id, resource_id):
    with pytest.raises(ValueError):
        db.create_mapping(type_id, resource_id, "AZURE-1")

    assert db.get_all_mappings() == []


def test_invalid_status_is_rejected(db):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1")

    with pytest.raises(sqlite3.IntegrityError):
        db.update_mapping(TYPE_ID, RESOURCE_ID, status="bogus")

    assert db.get_mapping(TYPE_ID, RESOURCE_ID)["status"] == "open"


def test_update_mapping_changes_only_given_fields(db):
    created = db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1", impact="Low", notes="keep")

    updated = db.update_mapping(TYPE_ID, RESOURCE_ID, status="closed", advisor_id="guid-2")

    assert updated["status"] == "closed"
    assert updated["advisor_id"] == "guid-2"
    assert updated["impact"] == "Low"
    assert updated["notes"] == "keep"
    assert _parse_utc(updated["last_synced"]) >= _parse_utc(created["last_synced"])


def test_update_mapping_returns_none_when_missing(db):
    assert db.update_mapping(TYPE_ID, RESOURCE_ID, status="closed") is None


def test_delete_mapping(db):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1")

    assert db.delete_mapping(TYPE_ID.upper(), RESOURCE_ID) is True
    assert db.get_mapping(TYPE_ID, RESOURCE_ID) is None
    assert db.delete_mapping(TYPE_ID, RESOURCE_ID) is False

    # The Jira key is free again.
    db.create_mapping(TYPE_ID, OTHER_RESOURCE_ID, "AZURE-1")


def test_get_mapping_by_jira_key(db):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-7")

    found = db.get_mapping_by_jira_key("AZURE-7")

    assert found["recommendation_type_id"] == TYPE_ID
    assert found["resource_id"] == RESOURCE_ID


def test_get_all_and_open_mappings(db):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1", subscription_id="sub-a")
    db.create_mapping(TYPE_ID, OTHER_RESOURCE_ID, "AZURE-2", subscription_id="sub-b")
    db.update_mapping(TYPE_ID, OTHER_RESOURCE_ID, status="closed")

    assert {m["jira_issue_key"] for m in db.get_all_mappings()} == {"AZURE-1", "AZURE-2"}
    assert [m["jira_issue_key"] for m in db.get_open_mappings()] == ["AZURE-1"]
    assert [m["jira_issue_key"] for m in db.get_open_mappings("sub-a")] == ["AZURE-1"]
    assert db.get_open_mappings("sub-b") == []


def test_log_audit_stores_the_finding_key(db):
    entry_id = db.log_audit(
        "manual_link",
        advisor_id="guid-1",
        jira_key="AZURE-1",
        decision="link",
        user_approval=True,
        metadata={"source": "test"},
        recommendation_type_id=TYPE_ID,
        resource_id=RESOURCE_ID,
    )

    [entry] = db.get_audit_log()
    assert entry["id"] == entry_id
    assert entry["action"] == "manual_link"
    assert entry["advisor_id"] == "guid-1"
    assert entry["recommendation_type_id"] == TYPE_ID
    assert entry["resource_id"] == RESOURCE_ID
    assert entry["jira_key"] == "AZURE-1"
    assert entry["user_approval"] == 1
    assert json.loads(entry["metadata"]) == {"source": "test"}


def test_get_audit_log_filters_and_orders_newest_first(db):
    first = db.log_audit("a", advisor_id="guid-1")
    second = db.log_audit("b", advisor_id="guid-2")
    third = db.log_audit("c", advisor_id="guid-1")

    assert [e["id"] for e in db.get_audit_log()] == [third, second, first]
    assert [e["id"] for e in db.get_audit_log(advisor_id="guid-1")] == [third, first]
    assert [e["id"] for e in db.get_audit_log(limit=1)] == [third]


def test_timestamps_are_utc_with_z(db):
    before = datetime.now(timezone.utc) - timedelta(seconds=1)

    mapping = db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1")
    db.log_audit("check")
    [entry] = db.get_audit_log()

    after = datetime.now(timezone.utc) + timedelta(seconds=1)
    for value in (mapping["created_at"], mapping["last_synced"], entry["timestamp"]):
        assert before <= _parse_utc(value) <= after


def test_get_stats(db):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1")
    db.create_mapping(TYPE_ID, OTHER_RESOURCE_ID, "AZURE-2")
    db.update_mapping(TYPE_ID, OTHER_RESOURCE_ID, status="closed")
    db.log_audit("x")

    assert db.get_stats() == {
        "total_mappings": 2,
        "open_tickets": 1,
        "closed_tickets": 1,
        "audit_log_entries": 1,
    }


def test_failed_write_is_rolled_back(db, db_path):
    db.create_mapping(TYPE_ID, RESOURCE_ID, "AZURE-1")

    with pytest.raises(sqlite3.IntegrityError):
        db.create_mapping(TYPE_ID, OTHER_RESOURCE_ID, "AZURE-1")

    # No transaction is left open on the file: another connection can write.
    with sqlite3.connect(db_path, timeout=0) as conn:
        conn.execute("INSERT INTO audit_log (timestamp, action) VALUES ('t', 'probe')")


def test_v1_file_is_refused_and_left_untouched(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.executescript(V1_SCHEMA)
        conn.execute(
            "INSERT INTO advisor_jira_mapping (advisor_id, jira_issue_key, created_at) "
            "VALUES ('guid-1', 'AZURE-1', 't')"
        )

    with pytest.raises(DatabaseSchemaError, match="first layout"):
        AdvisorDatabase(str(db_path))

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT advisor_id FROM advisor_jira_mapping").fetchall() == [("guid-1",)]


def test_newer_schema_version_is_refused(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 99")

    with pytest.raises(DatabaseSchemaError, match="schema version 99"):
        AdvisorDatabase(str(db_path))


def test_creates_missing_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "advisor.db"

    AdvisorDatabase(str(path))

    assert path.exists()
