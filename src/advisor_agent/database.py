"""
Database layer for tracking Advisor findings ↔ Jira issue mappings.

Uses SQLite for Phase 1-2 with an abstraction layer to support
PostgreSQL or Azure Cosmos DB for production multi-instance deployments.
"""

import sqlite3
import json
from pathlib import Path
from typing import Optional, List
from datetime import datetime


class DatabaseSchema:
    """SQLite schema for the Advisor-Jira mapping database."""

    SCHEMA_VERSION = 1

    CREATE_TABLES_SQL = """
    -- Advistor findings mapped to Jira issues
    CREATE TABLE IF NOT EXISTS advisor_jira_mapping (
        advisor_id TEXT PRIMARY KEY,
        jira_issue_key TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        impact TEXT,
        category TEXT,
        resource_id TEXT,
        subscription_id TEXT,
        fix_evaluation TEXT,
        last_synced TEXT,
        notes TEXT,
        CONSTRAINT valid_status CHECK(status IN ('open', 'closed', 'paused'))
    );

    -- Audit log for all decisions and actions
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        action TEXT NOT NULL,
        advisor_id TEXT,
        jira_key TEXT,
        decision TEXT,
        rationale TEXT,
        user_approval BOOLEAN,
        metadata TEXT
    );

    -- Index for common queries
    CREATE INDEX IF NOT EXISTS idx_advisor_id ON advisor_jira_mapping(advisor_id);
    CREATE INDEX IF NOT EXISTS idx_jira_key ON advisor_jira_mapping(jira_issue_key);
    CREATE INDEX IF NOT EXISTS idx_status ON advisor_jira_mapping(status);
    CREATE INDEX IF NOT EXISTS idx_timestamp ON audit_log(timestamp);
    """


class AdvisorDatabase:
    """SQLite database for advisor-to-Jira mappings."""

    def __init__(self, db_path: str = "workspace/advisor.db"):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(DatabaseSchema.CREATE_TABLES_SQL)
            conn.commit()

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Access columns by name
        return conn

    def get_mapping(self, advisor_id: str) -> Optional[dict]:
        """
        Get the Jira mapping for an Advisor finding.

        Args:
            advisor_id: The Azure Advisor recommendation ID.

        Returns:
            Mapping dict if found, None otherwise.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM advisor_jira_mapping WHERE advisor_id = ?",
            (advisor_id,),
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        return dict(row)

    def get_mapping_by_jira_key(self, jira_key: str) -> Optional[dict]:
        """
        Get the Advisor mapping for a Jira issue.

        Args:
            jira_key: The Jira issue key (e.g., AZURE-123).

        Returns:
            Mapping dict if found, None otherwise.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM advisor_jira_mapping WHERE jira_issue_key = ?",
            (jira_key,),
        )
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        return dict(row)

    def create_mapping(
        self,
        advisor_id: str,
        jira_issue_key: str,
        category: Optional[str] = None,
        impact: Optional[str] = None,
        resource_id: Optional[str] = None,
        subscription_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict:
        """
        Create a new Advisor-Jira mapping.

        Args:
            advisor_id: Azure Advisor recommendation ID.
            jira_issue_key: Jira issue key (e.g., AZURE-123).
            category: Recommendation category (Cost, Security, etc.).
            impact: Impact level (High, Medium, Low).
            resource_id: Azure resource ID.
            subscription_id: Azure subscription ID.
            notes: Optional notes.

        Returns:
            The created mapping dict.
        """
        now = datetime.utcnow().isoformat() + "Z"

        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO advisor_jira_mapping
            (advisor_id, jira_issue_key, created_at, status, category, impact,
             resource_id, subscription_id, last_synced, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                advisor_id,
                jira_issue_key,
                now,
                "open",
                category,
                impact,
                resource_id,
                subscription_id,
                now,
                notes,
            ),
        )
        conn.commit()
        conn.close()

        return self.get_mapping(advisor_id)

    def update_mapping(
        self,
        advisor_id: str,
        status: Optional[str] = None,
        impact: Optional[str] = None,
        fix_evaluation: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Update an existing mapping.

        Args:
            advisor_id: The Advisor ID to update.
            status: New status (open, closed, paused).
            impact: New impact level.
            fix_evaluation: Evaluation of fix effectiveness.
            notes: Additional notes.

        Returns:
            Updated mapping dict, or None if not found.
        """
        now = datetime.utcnow().isoformat() + "Z"

        conn = self._get_connection()
        cursor = conn.cursor()

        # Build update query
        updates = ["last_synced = ?"]
        params = [now]

        if status:
            updates.append("status = ?")
            params.append(status)

        if impact:
            updates.append("impact = ?")
            params.append(impact)

        if fix_evaluation:
            updates.append("fix_evaluation = ?")
            params.append(fix_evaluation)

        if notes:
            updates.append("notes = ?")
            params.append(notes)

        params.append(advisor_id)

        cursor.execute(
            f"UPDATE advisor_jira_mapping SET {', '.join(updates)} WHERE advisor_id = ?",
            params,
        )
        conn.commit()
        conn.close()

        return self.get_mapping(advisor_id)

    def get_open_mappings(self, subscription_id: Optional[str] = None) -> List[dict]:
        """
        Get all open mappings (optionally filtered by subscription).

        Args:
            subscription_id: Optional Azure subscription ID to filter by.

        Returns:
            List of open mapping dicts.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        if subscription_id:
            cursor.execute(
                """
                SELECT * FROM advisor_jira_mapping
                WHERE status = 'open' AND subscription_id = ?
                ORDER BY created_at DESC
                """,
                (subscription_id,),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM advisor_jira_mapping
                WHERE status = 'open'
                ORDER BY created_at DESC
                """
            )

        rows = cursor.fetchall()
        conn.close()

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
    ) -> int:
        """
        Log an action to the audit trail.

        Args:
            action: Type of action (e.g., "create_ticket", "update_ticket", "skip").
            advisor_id: Associated Advisor ID.
            jira_key: Associated Jira key.
            decision: The decision made (create/update/skip/close).
            rationale: Reason for the decision.
            user_approval: Whether user approved this action.
            metadata: Additional metadata as dict.

        Returns:
            The audit log entry ID.
        """
        now = datetime.utcnow().isoformat() + "Z"

        metadata_json = json.dumps(metadata) if metadata else None

        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO audit_log
            (timestamp, action, advisor_id, jira_key, decision, rationale, user_approval, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                action,
                advisor_id,
                jira_key,
                decision,
                rationale,
                user_approval,
                metadata_json,
            ),
        )
        conn.commit()
        entry_id = cursor.lastrowid
        conn.close()

        return entry_id

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
            List of audit log entries as dicts.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        if advisor_id:
            cursor.execute(
                """
                SELECT * FROM audit_log
                WHERE advisor_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (advisor_id, limit),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM audit_log
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )

        rows = cursor.fetchall()
        conn.close()

        return [dict(row) for row in rows]

    def get_stats(self) -> dict:
        """
        Get database statistics.

        Returns:
            Dict with stats like total mappings, open tickets, etc.
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM advisor_jira_mapping")
        total = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) as open FROM advisor_jira_mapping WHERE status = 'open'")
        open_count = cursor.fetchone()["open"]

        cursor.execute("SELECT COUNT(*) as closed FROM advisor_jira_mapping WHERE status = 'closed'")
        closed_count = cursor.fetchone()["closed"]

        cursor.execute("SELECT COUNT(*) as audited FROM audit_log")
        audit_count = cursor.fetchone()["audited"]

        conn.close()

        return {
            "total_mappings": total,
            "open_tickets": open_count,
            "closed_tickets": closed_count,
            "audit_log_entries": audit_count,
        }
