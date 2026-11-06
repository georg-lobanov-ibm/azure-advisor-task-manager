"""
Database layer for tracking Advisor findings ↔ Jira issue mappings.

Uses SQLite for Phase 1-2 with an abstraction layer to support
PostgreSQL or Azure Cosmos DB for production multi-instance deployments.

Identity of a finding
---------------------
The ``id`` / ``name`` GUID that Azure Advisor puts on each recommendation is
not stable: the same finding can come back with a different GUID later. A
mapping is therefore keyed on ``(recommendation_type_id, resource_id)``, the
recommendation type plus the resource it applies to. Both are compared without
regard to case, because ARM resource IDs and GUIDs are case-insensitive and
Advisor does not keep their casing consistent. The last GUID seen is stored in
``advisor_id`` for information only.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional


class DatabaseSchemaError(RuntimeError):
    """The database file has a layout this code cannot use."""


def _utc_now() -> str:
    """Current UTC time as ``2026-01-31T12:00:00.123456Z``.

    Always with microseconds, so the strings sort in time order.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class DatabaseSchema:
    """SQLite schema for the Advisor-Jira mapping database."""

    # Stored in `PRAGMA user_version`. Version 1 (keyed on the Advisor GUID)
    # never set it, so such a file reads as 0 and is recognised by its tables.
    SCHEMA_VERSION = 2

    CREATE_TABLES_SQL = """
    -- Advisor findings mapped to Jira issues
    CREATE TABLE IF NOT EXISTS advisor_jira_mapping (
        recommendation_type_id TEXT NOT NULL COLLATE NOCASE,
        resource_id TEXT NOT NULL COLLATE NOCASE,
        advisor_id TEXT,
        jira_issue_key TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        impact TEXT,
        category TEXT,
        subscription_id TEXT,
        fix_evaluation TEXT,
        last_synced TEXT,
        notes TEXT,
        PRIMARY KEY (recommendation_type_id, resource_id),
        CONSTRAINT valid_status CHECK(status IN ('open', 'closed', 'paused'))
    );

    -- Audit log for all decisions and actions
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        action TEXT NOT NULL,
        advisor_id TEXT,
        recommendation_type_id TEXT,
        resource_id TEXT,
        jira_key TEXT,
        decision TEXT,
        rationale TEXT,
        user_approval BOOLEAN,
        metadata TEXT
    );

    -- Index for common queries
    CREATE INDEX IF NOT EXISTS idx_status ON advisor_jira_mapping(status);
    CREATE INDEX IF NOT EXISTS idx_timestamp ON audit_log(timestamp);
    CREATE INDEX IF NOT EXISTS idx_audit_advisor_id ON audit_log(advisor_id);
    """

    TABLES = ("advisor_jira_mapping", "audit_log")


def _require_key(recommendation_type_id: str, resource_id: str) -> None:
    """Reject a blank key part: it would make unrelated findings share one mapping."""
    for name, value in (
        ("recommendation_type_id", recommendation_type_id),
        ("resource_id", resource_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")


class AdvisorDatabase:
    """SQLite database for advisor-to-Jira mappings."""

    def __init__(self, db_path: str = "workspace/advisor.db"):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file.

        Raises:
            DatabaseSchemaError: If the file was written with a different
                schema version.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()

    def _init_db(self) -> None:
        """Create the schema in a new file, or check the version of an existing one."""
        conn = sqlite3.connect(self.db_path)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            placeholders = ", ".join("?" for _ in DatabaseSchema.TABLES)
            existing = conn.execute(
                f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
                DatabaseSchema.TABLES,
            ).fetchall()

            is_new_file = version == 0 and not existing
            if not is_new_file and version != DatabaseSchema.SCHEMA_VERSION:
                found = "the first layout (keyed on the Advisor GUID)" if version == 0 else f"schema version {version}"
                raise DatabaseSchemaError(
                    f"{self.db_path} uses {found}, but this code needs schema version "
                    f"{DatabaseSchema.SCHEMA_VERSION}. Existing mappings cannot be converted: "
                    "a finding's GUID does not tell which recommendation type it was. "
                    "Move or delete the file so a new one is created."
                )

            conn.executescript(
                DatabaseSchema.CREATE_TABLES_SQL
                + f"\nPRAGMA user_version = {int(DatabaseSchema.SCHEMA_VERSION)};"
            )
        finally:
            conn.close()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success, rolls back on error and is always closed."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Access columns by name
        try:
            with conn:  # commit, or roll back if the block raised
                yield conn
        finally:
            conn.close()

    def get_mapping(self, recommendation_type_id: str, resource_id: str) -> Optional[dict]:
        """
        Get the Jira mapping for an Advisor finding.

        Args:
            recommendation_type_id: The Advisor recommendation type ID.
            resource_id: The ARM ID of the affected resource.

        Returns:
            Mapping dict if found, None otherwise.
        """
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM advisor_jira_mapping
                WHERE recommendation_type_id = ? AND resource_id = ?
                """,
                (recommendation_type_id, resource_id),
            ).fetchone()

        return dict(row) if row else None

    def get_mapping_by_jira_key(self, jira_key: str) -> Optional[dict]:
        """
        Get the Advisor mapping for a Jira issue.

        Args:
            jira_key: The Jira issue key (e.g., AZURE-123).

        Returns:
            Mapping dict if found, None otherwise.
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM advisor_jira_mapping WHERE jira_issue_key = ?",
                (jira_key,),
            ).fetchone()

        return dict(row) if row else None

    def create_mapping(
        self,
        recommendation_type_id: str,
        resource_id: str,
        jira_issue_key: str,
        advisor_id: Optional[str] = None,
        category: Optional[str] = None,
        impact: Optional[str] = None,
        subscription_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        """
        Create a new Advisor-Jira mapping.

        Args:
            recommendation_type_id: Advisor recommendation type ID.
            resource_id: ARM ID of the affected resource.
            jira_issue_key: Jira issue key (e.g., AZURE-123).
            advisor_id: GUID the finding currently has. Informational only.
            category: Recommendation category (Cost, Security, etc.).
            impact: Impact level (High, Medium, Low).
            subscription_id: Azure subscription ID.
            notes: Optional notes.

        Returns:
            The created mapping dict.

        Raises:
            ValueError: If ``recommendation_type_id`` or ``resource_id`` is blank.
            sqlite3.IntegrityError: If the finding or the Jira issue already has a mapping.
        """
        _require_key(recommendation_type_id, resource_id)
        now = _utc_now()

        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO advisor_jira_mapping
                (recommendation_type_id, resource_id, advisor_id, jira_issue_key,
                 created_at, status, category, impact, subscription_id, last_synced, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recommendation_type_id,
                    resource_id,
                    advisor_id,
                    jira_issue_key,
                    now,
                    "open",
                    category,
                    impact,
                    subscription_id,
                    now,
                    notes,
                ),
            )

        return self.get_mapping(recommendation_type_id, resource_id)

    def update_mapping(
        self,
        recommendation_type_id: str,
        resource_id: str,
        status: Optional[str] = None,
        impact: Optional[str] = None,
        fix_evaluation: Optional[str] = None,
        notes: Optional[str] = None,
        advisor_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Update an existing mapping.

        Only the arguments that are not None are changed; ``last_synced`` is
        always set to now.

        Args:
            recommendation_type_id: The Advisor recommendation type ID.
            resource_id: The ARM ID of the affected resource.
            status: New status (open, closed, paused).
            impact: New impact level.
            fix_evaluation: Evaluation of fix effectiveness.
            notes: Additional notes.
            advisor_id: GUID the finding has now.

        Returns:
            Updated mapping dict, or None if not found.
        """
        updates = ["last_synced = ?"]
        params: list = [_utc_now()]

        for column, value in (
            ("status", status),
            ("impact", impact),
            ("fix_evaluation", fix_evaluation),
            ("notes", notes),
            ("advisor_id", advisor_id),
        ):
            if value is not None:
                updates.append(f"{column} = ?")
                params.append(value)

        params.extend([recommendation_type_id, resource_id])

        with self._connection() as conn:
            conn.execute(
                f"""
                UPDATE advisor_jira_mapping SET {', '.join(updates)}
                WHERE recommendation_type_id = ? AND resource_id = ?
                """,
                params,
            )

        return self.get_mapping(recommendation_type_id, resource_id)

    def delete_mapping(self, recommendation_type_id: str, resource_id: str) -> bool:
        """
        Remove the mapping of a finding.

        Args:
            recommendation_type_id: The Advisor recommendation type ID.
            resource_id: The ARM ID of the affected resource.

        Returns:
            True if a mapping was removed, False if there was none.
        """
        with self._connection() as conn:
            cursor = conn.execute(
                """
                DELETE FROM advisor_jira_mapping
                WHERE recommendation_type_id = ? AND resource_id = ?
                """,
                (recommendation_type_id, resource_id),
            )
            return cursor.rowcount > 0

    def get_all_mappings(self) -> List[dict]:
        """
        Get every mapping, whatever its status.

        Returns:
            List of mapping dicts, newest first.
        """
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM advisor_jira_mapping ORDER BY created_at DESC"
            ).fetchall()

        return [dict(row) for row in rows]

    def get_open_mappings(self, subscription_id: Optional[str] = None) -> List[dict]:
        """
        Get all open mappings (optionally filtered by subscription).

        Args:
            subscription_id: Optional Azure subscription ID to filter by.

        Returns:
            List of open mapping dicts.
        """
        query = "SELECT * FROM advisor_jira_mapping WHERE status = 'open'"
        params: tuple = ()
        if subscription_id:
            query += " AND subscription_id = ?"
            params = (subscription_id,)
        query += " ORDER BY created_at DESC"

        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()

        return [dict(row) for row in rows]

    def log_audit(
        self,
        action: str,
        advisor_id: Optional[str] = None,
        jira_key: Optional[str] = None,
        decision: Optional[str] = None,
        rationale: Optional[str] = None,
        user_approval: Optional[bool] = None,
        metadata: Optional[dict] = None,
        *,
        recommendation_type_id: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> int:
        """
        Log an action to the audit trail.

        Args:
            action: Type of action (e.g., "create_ticket", "update_ticket", "skip").
            advisor_id: GUID of the Advisor finding, if known.
            jira_key: Associated Jira key.
            decision: The decision made (create/update/skip/close).
            rationale: Reason for the decision.
            user_approval: Whether user approved this action.
            metadata: Additional metadata as dict.
            recommendation_type_id: Recommendation type of the finding, if known.
            resource_id: ARM ID of the finding's resource, if known.

        Returns:
            The audit log entry ID.
        """
        metadata_json = json.dumps(metadata) if metadata else None

        with self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_log
                (timestamp, action, advisor_id, recommendation_type_id, resource_id,
                 jira_key, decision, rationale, user_approval, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now(),
                    action,
                    advisor_id,
                    recommendation_type_id,
                    resource_id,
                    jira_key,
                    decision,
                    rationale,
                    user_approval,
                    metadata_json,
                ),
            )
            return cursor.lastrowid

    def get_audit_log(
        self,
        advisor_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[dict]:
        """
        Get audit log entries.

        Args:
            advisor_id: Optional filter by Advisor ID.
            limit: Maximum number of entries to return.

        Returns:
            List of audit log entries as dicts, newest first.
        """
        query = "SELECT * FROM audit_log"
        params: list = []
        if advisor_id:
            query += " WHERE advisor_id = ?"
            params.append(advisor_id)
        query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(limit)

        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()

        return [dict(row) for row in rows]

    def get_stats(self) -> dict:
        """
        Get database statistics.

        Returns:
            Dict with stats like total mappings, open tickets, etc.
        """
        with self._connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM advisor_jira_mapping").fetchone()[0]
            open_count = conn.execute(
                "SELECT COUNT(*) FROM advisor_jira_mapping WHERE status = 'open'"
            ).fetchone()[0]
            closed_count = conn.execute(
                "SELECT COUNT(*) FROM advisor_jira_mapping WHERE status = 'closed'"
            ).fetchone()[0]
            audit_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

        return {
            "total_mappings": total,
            "open_tickets": open_count,
            "closed_tickets": closed_count,
            "audit_log_entries": audit_count,
        }
