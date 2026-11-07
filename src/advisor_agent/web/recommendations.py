"""Shape Azure Advisor results for the UI, and decide which request arguments to forward.

Everything here is a pure function. The input is whatever the Azure MCP server
returned, which is untrusted and has only been seen in small samples (see the
module docstring of ``azure_mcp.py``), so every field below the top level is
treated as optional and anything of an unexpected type is dropped, not trusted.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# How many rows one request asks for. 100 is the tool's maximum for ``top``.
MAX_ROWS = 100

_TEXT_LIMIT = 4000
_ID_LIMIT = 1024

_ADVISOR_ID_MARKER = "/providers/microsoft.advisor/recommendations/"
_SUBSCRIPTION = re.compile(r"^/subscriptions/([0-9a-f-]{36})(?:/|$)", re.IGNORECASE)
_RESOURCE_GROUP = re.compile(r"/resourcegroups/([^/]+)", re.IGNORECASE)

# Both are used with ``fullmatch``: ``$`` would also accept a trailing newline.
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# Values of the status, category and impact filters: words, nothing else.
_FILTER_WORD = re.compile(r"[A-Za-z][A-Za-z ]{0,39}")

FILTER_NAMES = ("status", "category", "impact")


def list_arguments(params: Mapping[str, str]) -> dict[str, Any]:
    """The ``parameters`` for ``advisor_recommendation_list``, from a browser request.

    Only these are ever forwarded; anything else in ``params`` is ignored.
    Raises ``ValueError`` (with a message safe to show) for a bad value.
    """
    arguments: dict[str, Any] = {"top": MAX_ROWS}
    for name in FILTER_NAMES:
        value = params.get(name)
        if value:
            if not _FILTER_WORD.fullmatch(value):
                raise ValueError(f"Invalid value for '{name}'.")
            arguments[name] = value
    subscription = params.get("subscription")
    if subscription:
        if not GUID.fullmatch(subscription):
            raise ValueError("Invalid subscription: expected a subscription ID.")
        arguments["subscription"] = subscription
    return arguments


def _text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    """``value`` if it is a string of at most ``limit`` characters, else a clipped copy / ''."""
    if not isinstance(value, str):
        return ""
    return value if len(value) <= limit else value[:limit]


def _identifier(value: Any, limit: int) -> str:
    """``value`` stripped if it is a string of at most ``limit`` characters, else ''.

    For the parts of a finding's identity: a clipped ID would name a different
    resource, so one that is too long is dropped (the row is then not linkable)
    instead of shortened.
    """
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if len(value) <= limit else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _last_segment(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _resource_from_advisor_id(advisor_resource_id: str) -> str:
    """The resource part of a recommendation's ``id``, or ''.

    An id looks like ``<resource id>/providers/Microsoft.Advisor/recommendations/<guid>``.
    """
    index = advisor_resource_id.lower().find(_ADVISOR_ID_MARKER)
    return advisor_resource_id[:index] if index > 0 else ""


def normalize(raw: Any) -> dict[str, Any] | None:
    """One recommendation as the UI row. ``None`` if ``raw`` is not an object.

    ``recommendation_type_id`` plus ``resource_id`` is the identity of a finding
    (see ``database.py``). A row without both is shown but cannot be linked.
    """
    if not isinstance(raw, dict):
        return None
    props = _dict(raw.get("properties"))
    metadata = _dict(props.get("resourceMetadata"))
    short = _dict(props.get("shortDescription"))

    advisor_resource_id = _identifier(raw.get("id"), _ID_LIMIT)
    type_id = _identifier(props.get("recommendationTypeId"), 200)
    resource_id = (
        _identifier(metadata.get("resourceId"), _ID_LIMIT)
        or _resource_from_advisor_id(advisor_resource_id)
    )
    subscription = _SUBSCRIPTION.match(resource_id) or _SUBSCRIPTION.match(advisor_resource_id)
    group = _RESOURCE_GROUP.search(resource_id)

    return {
        "row_id": "",  # filled in by normalize_all
        "advisor_id": _identifier(raw.get("name"), 200) or _last_segment(advisor_resource_id),
        "recommendation_type_id": type_id,
        "resource_id": resource_id,
        "linkable": bool(type_id and resource_id),
        "subscription_id": subscription.group(1).lower() if subscription else "",
        "resource_group": group.group(1) if group else "",
        "resource_name": _text(props.get("impactedValue"), 300) or _last_segment(resource_id),
        "resource_type": _text(props.get("impactedField"), 300),
        "category": _text(props.get("category"), 100),
        "impact": _text(props.get("impact"), 100),
        "status": _text(props.get("recommendationStatus"), 100),
        "problem": _text(short.get("problem")),
        "solution": _text(short.get("solution")),
        "description": _text(props.get("description")),
        "last_updated": _text(props.get("lastUpdated"), 100),
        "raw": raw,
    }


def link_key(recommendation_type_id: str, resource_id: str) -> tuple[str, str]:
    """The key a mapping is looked up by: both parts, compared without case."""
    return recommendation_type_id.lower(), resource_id.lower()


def normalize_all(items: Any) -> list[dict[str, Any]]:
    """Normalize a list of recommendations and give every row a unique ``row_id``."""
    if not isinstance(items, list):
        raise ValueError("The Azure MCP server returned an unexpected result (no recommendation list).")
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(items):
        row = normalize(item)
        if row is None:
            continue
        if row["linkable"]:
            base = "|".join(link_key(row["recommendation_type_id"], row["resource_id"]))
        else:
            base = row["advisor_id"].lower() or f"row-{index}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        row["row_id"] = base if count == 0 else f"{base}#{count}"
        rows.append(row)
    return rows


def recommendations_from_payload(payload: Any) -> tuple[list[dict[str, Any]], bool]:
    """``(rows, truncated)`` from the JSON of an ``advisor_recommendation_list`` call."""
    results = _dict(_dict(payload).get("results"))
    rows = normalize_all(results.get("recommendations", []))
    return rows, results.get("areResultsTruncated") is True


def subscriptions_from_payload(payload: Any) -> list[dict[str, str]]:
    """``[{"id", "name"}, ...]`` from the JSON of a ``subscription_list`` call.

    The shape of the real result has not been checked, so this looks for the
    obvious spellings and returns an empty list when it finds none; the UI then
    just offers the default subscription.
    """
    results = _dict(payload).get("results")
    items: Any = results
    if isinstance(results, dict):
        items = results.get("subscriptions", results.get("value"))
    if not isinstance(items, list):
        return []
    found: list[dict[str, str]] = []
    for item in items:
        item = _dict(item)
        identifier = _text(item.get("subscriptionId") or item.get("id"), _ID_LIMIT)
        match = _SUBSCRIPTION.match(identifier)
        guid = match.group(1) if match else identifier
        if not GUID.fullmatch(guid):
            continue
        found.append(
            {
                "id": guid.lower(),
                "name": _text(item.get("displayName") or item.get("name"), 200) or guid.lower(),
            }
        )
    return found
