"""An org admin reads their organization's events and never a member's personal ones (#1277, F5).

``build_org_scope_clause`` attributed any event with no ``organization_id`` to every
organization its ACTOR belongs to, so ``transcript.view_unredacted`` on a member's personal
file was readable by the admins of every org the member joined. Events now carry
``tenant_scope``, and the legacy branch refuses an event that names its own tenant. This
evaluates the REAL clause against a REAL index (the audit mapping, a uuid-suffixed throwaway
name, deleted in teardown), so what is proven is what OpenSearch returns.

Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 pytest backend/tests/integration/test_audit_org_scope_opensearch.py -m integration
"""

from __future__ import annotations

import os
import uuid as uuid_pkg

import pytest

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason="No OpenSearch reachable (SKIP_OPENSEARCH): the clause is evaluated by a cluster.",
    ),
]

ORG, OTHER_ORG = 7, 8
MEMBER, OTHER_MEMBER = 1, 2


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


def _visible(client, index: str, scope_org: int, members: list[int]) -> set[str]:
    from app.auth.audit import build_org_scope_clause

    client.indices.refresh(index=index)
    body = {"size": 50, "query": {"bool": {"must": [build_org_scope_clause(scope_org, members)]}}}
    hits = client.search(index=index, body=body)["hits"]["hits"]
    return {hit["_source"]["details"]["label"] for hit in hits}


def test_member_personal_file_view_invisible_to_org_admin(audit_index):
    client, index = audit_index
    docs = [
        # Stamped with the org: visible to its admin.
        _event("org-view", user_id=MEMBER, organization_id=ORG, tenant_scope="org"),
        # The same member viewing their OWN personal file: no organization_id by design.
        _event("personal-view", user_id=MEMBER, tenant_scope="personal"),
        # Another org's event: never visible.
        _event("other-org", user_id=MEMBER, organization_id=OTHER_ORG, tenant_scope="org"),
        # Pre-#1277 history with no stamp at all: the legacy rule still attributes it.
        _event("legacy-login", user_id=MEMBER),
        # A legacy event by somebody who is not a member.
        _event("stranger", user_id=999),
    ]
    for doc in docs:
        client.index(index=index, body=doc)

    assert _visible(client, index, ORG, [MEMBER, OTHER_MEMBER]) == {"org-view", "legacy-login"}
