"""Audit-log read path: the org-admin visibility clause and the OpenSearch query.

Shared by the super-admin (global) and org-admin (tenant-scoped) read and export endpoints, so
the query and its tenant-isolation clause live in exactly one place.
Rendering an export is ``auth/audit_export.py``; writing events is ``auth/audit.py``.
"""

import logging
from datetime import datetime
from typing import Any

from app.core.config import settings


def _build_audit_opensearch_client():
    """Construct an OpenSearch client for audit-log reads (None on failure)."""
    try:
        from opensearchpy import OpenSearch

        from app.core.opensearch_auth import opensearch_connection_kwargs

        return OpenSearch(**opensearch_connection_kwargs())
    except Exception as e:  # noqa: BLE001 — OpenSearch optional
        logging.getLogger("audit").warning(f"Failed to build audit OpenSearch client: {e}")
        return None


def build_org_scope_clause(scope_org_id: int, scope_user_ids: list[int] | None) -> dict[str, Any]:
    """Build the org-admin visibility clause for the audit-log query.

    Visible to an admin of ``scope_org_id``:
      * events STAMPED with the org (``organization_id == scope_org_id``) —
        including events with ``user_id`` NULL (e.g. failed logins against an
        org surface), which the member-id filter alone could never show; and
      * **legacy-window** events: events written before org stamping existed
        (or by writers with no tenant context, e.g. local login) carry no
        ``organization_id`` field. Those are attributed via the org's member
        user-ids instead. This preserves pre-v372 visibility, with a known
        limitation that shrinks as stamping coverage grows: a SHARED member's
        un-stamped activity from another context is visible to every org that
        member belongs to (exactly the pre-v372 behavior). An event stamped
        with a DIFFERENT org is never visible here — the must_not/exists split
        is exhaustive. Events that carry ``tenant_scope`` (every content and governance
        event since #1277) are excluded from this branch: they name the resource's own
        tenant, so a ``personal`` one is never an org's, whoever acted.

    Extracted so tests can pin the exact filter body without a cluster.
    """
    legacy_branch: dict[str, Any] = {
        "bool": {
            "must_not": [
                {"exists": {"field": "organization_id"}},
                # A personal-scope resource event carries no organization_id by design; it
                # must never be attributed to an org through its actor (issue #1277, F5).
                {"exists": {"field": "tenant_scope"}},
            ]
        }
    }
    if scope_user_ids is not None:
        legacy_branch["bool"]["filter"] = [{"terms": {"user_id": scope_user_ids}}]
    return {
        "bool": {
            "should": [
                {"term": {"organization_id": scope_org_id}},
                legacy_branch,
            ],
            "minimum_should_match": 1,
        }
    }


def query_audit_logs(
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    event_type: str | None = None,
    user_id: int | None = None,
    target_user_id: int | None = None,
    outcome: str | None = None,
    resource_type: str | None = None,
    resource_uuid: str | None = None,
    scope_user_ids: list[int] | None = None,
    scope_org_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    index_pattern: str = "audit-logs-*",
) -> dict[str, Any]:
    """Query the OpenSearch audit-log indices with optional scoping.

    Shared by the super-admin (global) and org-admin (member-scoped) read
    endpoints so the query logic lives in exactly one place.

    Args:
        start_date / end_date: timestamp range filters.
        event_type / user_id / outcome: equality filters. ``user_id`` filters on
            the ACTOR.
        target_user_id: equality filter on the ERASED/AFFECTED SUBJECT (issue
            #443/#828) — a distinct field from ``user_id``, added once every
            administrative emitter agreed on which field means what. "Everything
            done TO user X" is this filter, not ``user_id``; "everything user X
            did" is ``user_id``. Applied the same way as ``user_id`` — an
            additional ``must`` clause — so it can only NARROW whatever
            ``scope_user_ids``/``scope_org_id`` already admit, never widen it.
        resource_type / resource_uuid: equality filters on the typed resource fields (issue
            #1277): "everything that happened to this file". Like ``target_user_id`` they only
            NARROW the scope, never widen it.
        scope_user_ids: when provided (without ``scope_org_id``), restrict
            results to events whose ``user_id`` is in this set. An **empty**
            list returns no results (a removed/empty org sees nothing) rather
            than everything.
        scope_org_id: org-admin tenant scope (issue #262a). When provided, the
            visibility rule becomes: events stamped ``organization_id ==
            scope_org_id`` (including user_id-NULL events such as failed
            logins) OR legacy events with NO org stamp whose ``user_id`` is in
            ``scope_user_ids`` — see :func:`build_org_scope_clause` for the
            legacy-window semantics. Events stamped with another org are never
            returned.
        limit / offset: pagination.
        index_pattern: the indices to read. Only an integration test changes it, to read a
            throwaway index instead of the deployment's own audit history.

    Returns:
        ``{"logs": [...], "total": int, "offset": int, "limit": int}`` or
        ``{"error": str, "logs": [], "total": 0}`` when audit-to-OpenSearch is
        disabled or the query fails.
    """
    if not settings.AUDIT_LOG_TO_OPENSEARCH:
        return {
            "error": "Audit log querying requires AUDIT_LOG_TO_OPENSEARCH=true",
            "logs": [],
            "total": 0,
        }

    if scope_org_id is None and scope_user_ids is not None and len(scope_user_ids) == 0:
        # Member-only scope with no members — nothing is visible. (With an org
        # scope, org-stamped events remain visible even for an empty member set.)
        return {"logs": [], "total": 0, "offset": offset, "limit": limit}

    client = _build_audit_opensearch_client()
    if client is None:
        return {
            "error": "An internal error occurred while querying audit logs.",
            "logs": [],
            "total": 0,
        }

    try:
        must_clauses: list[dict] = []
        if start_date:
            must_clauses.append({"range": {"timestamp": {"gte": start_date.isoformat()}}})
        if end_date:
            must_clauses.append({"range": {"timestamp": {"lte": end_date.isoformat()}}})
        if event_type:
            must_clauses.append({"term": {"event_type": event_type}})
        if user_id is not None:
            must_clauses.append({"term": {"user_id": user_id}})
        if target_user_id is not None:
            must_clauses.append({"term": {"target_user_id": target_user_id}})
        if outcome:
            must_clauses.append({"term": {"outcome": outcome}})
        if resource_type:
            must_clauses.append({"term": {"resource_type": resource_type}})
        if resource_uuid:
            must_clauses.append({"term": {"resource_uuid": resource_uuid}})
        if scope_org_id is not None:
            # Org-admin scope: org-stamped events OR legacy member-attributed.
            must_clauses.append(build_org_scope_clause(scope_org_id, scope_user_ids))
        elif scope_user_ids is not None:
            # Member-only scope (pre-org callers): events acted by the members.
            must_clauses.append({"terms": {"user_id": scope_user_ids}})

        query = {
            "query": {"bool": {"must": must_clauses}} if must_clauses else {"match_all": {}},
            "sort": [{"timestamp": {"order": "desc"}}],
            "from": offset,
            "size": limit,
        }

        response = client.search(index=index_pattern, body=query)
        logs = [hit["_source"] for hit in response["hits"]["hits"]]
        total = response["hits"]["total"]["value"]
        return {"logs": logs, "total": total, "offset": offset, "limit": limit}
    except Exception as e:  # noqa: BLE001
        logging.getLogger("audit").error(f"Error querying audit logs: {e}")
        return {
            "error": "An internal error occurred while querying audit logs.",
            "logs": [],
            "total": 0,
        }
