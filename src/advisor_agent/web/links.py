"""Links between an Advisor finding and a Jira issue: what is written, and how it is shown.

Two things make a link: the Link button (``PUT /api/links`` in ``app.py``) and a
Story the agent created for the recommendation attached to a message
(``chat.py``). Both write through ``create_link``, so they apply the same
conflict checks and leave the same kind of audit record, told apart by the
audit action.

The functions that take a database run on the database thread (``Storage.run``).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Mapping

from ..database import AdvisorDatabase
from .errors import ApiError

log = logging.getLogger("advisor_agent.web")

LINK_AUDIT_LINK = "manual_link"
LINK_AUDIT_UNLINK = "manual_unlink"
LINK_AUDIT_AUTO = "auto_link"

# The longest value each text field of a link may have. A value over its limit is
# refused, never clipped: a clipped ID would name another resource.
LINK_FIELD_LIMITS = {
    "recommendation_type_id": 200,
    "resource_id": 1024,
    "advisor_id": 200,
    "category": 100,
    "impact": 100,
    "subscription_id": 36,
}


def public_link(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a stored mapping that the page may see."""
    return {
        "recommendation_type_id": mapping.get("recommendation_type_id"),
        "resource_id": mapping.get("resource_id"),
        "jira_key": mapping.get("jira_issue_key"),
        "status": mapping.get("status"),
        "created_at": mapping.get("created_at"),
        "category": mapping.get("category"),
        "impact": mapping.get("impact"),
    }


def create_link(
    db: AdvisorDatabase,
    type_id: str,
    resource_id: str,
    jira_key: str,
    advisor_id: str | None,
    category: str | None,
    impact: str | None,
    subscription_id: str | None,
    *,
    action: str,
    rationale: str,
    source: str,
) -> dict[str, Any]:
    """Link the finding to ``jira_key`` and record why. Runs on the database thread.

    Raises ``ApiError(409, ...)`` if the finding already has a link or the issue
    is linked to another finding. A link whose audit record cannot be written is
    not kept.
    """
    existing = db.get_mapping(type_id, resource_id)
    if existing is not None:
        raise ApiError(
            409, f"This finding is already linked to {existing['jira_issue_key']}. Unlink it first."
        )
    if db.get_mapping_by_jira_key(jira_key) is not None:
        raise ApiError(409, f"{jira_key} is already linked to another finding.")
    try:
        mapping = db.create_mapping(
            type_id,
            resource_id,
            jira_key,
            advisor_id=advisor_id,
            category=category,
            impact=impact,
            subscription_id=subscription_id,
        )
    except sqlite3.IntegrityError:
        raise ApiError(409, "This finding or this Jira issue is already linked.") from None
    try:
        db.log_audit(
            action,
            advisor_id=advisor_id,
            jira_key=jira_key,
            decision="link",
            rationale=rationale,
            metadata={"source": source},
            recommendation_type_id=type_id,
            resource_id=resource_id,
        )
    except Exception:
        # A link without its record is not kept.
        db.delete_mapping(type_id, resource_id)
        raise
    return mapping


def remove_link(db: AdvisorDatabase, type_id: str, resource_id: str) -> dict[str, Any]:
    """Unlink the finding and record it. Runs on the database thread."""
    existing = db.get_mapping(type_id, resource_id)
    if existing is None:
        raise ApiError(404, "This finding is not linked.")
    db.delete_mapping(type_id, resource_id)
    try:
        db.log_audit(
            LINK_AUDIT_UNLINK,
            advisor_id=existing.get("advisor_id"),
            jira_key=existing["jira_issue_key"],
            decision="unlink",
            rationale="Unlinked by hand in the web UI",
            metadata={"source": "web_ui"},
            recommendation_type_id=type_id,
            resource_id=resource_id,
        )
    except Exception:
        log.exception("Could not write the audit record of an unlink.")
        raise
    return existing
