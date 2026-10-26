"""The organization audit read returns exactly the organization's events (#1277).

``org_audit_query`` (the scope and member guards the list and the export share) feeds the real
``query_audit_logs`` against a real index: the audit mapping, a uuid-suffixed throwaway name,
deleted in teardown. What is proven is what the cluster hands back: the organization's stamped
events and its legacy member history, and none of another tenant's events, a member's
personal-workspace events, or a stranger's. The HTTP layer around it is
``tests/api/test_org_audit_view.py``.

Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 pytest backend/tests/integration/test_org_audit_export_opensearch.py -m integration
"""

from __future__ import annotations

import os
import uuid as uuid_pkg

import pytest

from app.api.deps_context import RequestContext
from app.api.endpoints.org_admin import org_audit_query
from app.auth.audit import query_audit_logs

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason="No OpenSearch reachable (SKIP_OPENSEARCH): the read is served by a cluster.",
    ),
]


@pytest.fixture
def audit_index():
    from app.auth.audit import _AUDIT_PROPERTIES
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    name = f"test_audit_org_scope_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(
        index=name,
        body={
            "mappings": {"properties": _AUDIT_PROPERTIES},
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        },
    )
    try:
        yield client, name
    finally:
        client.indices.delete(index=name, ignore=[404])


def _event(label: str, **fields) -> dict:
    base = {"timestamp": "2026-10-25T12:00:00Z", "event_type": "x", "outcome": "success"}
    return {**base, "details": {"label": label}, **fields}


def _read(gov, index: str, **filters) -> set[str]:
    ctx = RequestContext(user=gov.admin, org_id=gov.org.id, org_role="org:admin")
    args = {
        "start_date": None,
        "end_date": None,
        "event_type": None,
        "user_id": None,
        "user_uuid": None,
        "target_user_id": None,
        "target_user_uuid": None,
        "outcome": None,
        "resource_type": None,
        "resource_uuid": None,
        **filters,
    }
    result = query_audit_logs(
        **org_audit_query(gov.db, ctx, **args), limit=10_000, offset=0, index_pattern=index
    )
    assert "error" not in result, result
    return {row["details"]["label"] for row in result["logs"]}


def test_the_read_holds_the_organizations_events_and_nobody_elses(gov, audit_index, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AUDIT_LOG_TO_OPENSEARCH", True)
    os_client, index = audit_index
    file_uuid = str(uuid_pkg.uuid4())
    for doc in (
        _event(
            "org-view",
            user_id=gov.bob.id,
            organization_id=gov.org.id,
            tenant_scope="org",
            resource_type="media_file",
            resource_uuid=file_uuid,
        ),
        _event("personal-view", user_id=gov.bob.id, tenant_scope="personal"),
        _event(
            "other-org", user_id=gov.bob.id, organization_id=gov.other_org.id, tenant_scope="org"
        ),
        _event("legacy-login", user_id=gov.bob.id),
        _event("stranger", user_id=gov.outsider.id),
    ):
        os_client.index(index=index, body=doc)
    os_client.indices.refresh(index=index)

    assert _read(gov, index) == {"org-view", "legacy-login"}
    # The resource filter narrows inside that scope and never widens it.
    assert _read(gov, index, resource_uuid=uuid_pkg.UUID(file_uuid)) == {"org-view"}
