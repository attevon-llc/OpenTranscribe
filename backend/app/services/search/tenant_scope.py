"""OpenSearch tenant-scope filter helpers — cloud-edition seam.

These build the org-aware ``filter`` clauses that every OpenSearch search plane
(transcript chunks + speaker/voiceprint kNN) must apply, mirroring the SQL-plane
``app.api.deps_context.scope_to_context`` default-deny semantics:

* **Org context** (``organization_id`` is not None): only documents stamped with
  that exact ``organization_id`` are visible. Personal documents (no org) and
  other orgs' documents are excluded.
* **Personal scope** (``organization_id is None``): only documents that have NO
  ``organization_id`` field (i.e. personal docs) are visible, scoped to the
  caller's ``user_id``. Org-stamped documents are excluded so they cannot leak
  into personal scope.

Community-edition invariance: in the self-host/community edition every row has
``organization_id`` NULL, so docs are indexed WITHOUT the ``organization_id``
field. ``organization_id`` is therefore always None at every call site, and the
personal branch (``must_not exists organization_id``) is a behavior-preserving
no-op layered on top of the existing ``user_id`` filtering.
"""

from typing import Any

ORG_FIELD = "organization_id"


def org_filter_clauses(organization_id: int | None) -> list[dict[str, Any]]:
    """Return the OpenSearch ``bool.filter`` clauses for the active tenant scope.

    Args:
        organization_id: The active organization id, or None for personal scope.

    Returns:
        A list of OpenSearch filter clauses to AND into a query's ``filter``
        array. Org context -> a single ``term`` on ``organization_id``. Personal
        scope -> a ``must_not exists`` clause so org-stamped docs never match.
    """
    if organization_id is not None:
        return [{"term": {ORG_FIELD: organization_id}}]
    # Personal scope: exclude any document that carries an organization_id.
    return [{"bool": {"must_not": {"exists": {"field": ORG_FIELD}}}}]
