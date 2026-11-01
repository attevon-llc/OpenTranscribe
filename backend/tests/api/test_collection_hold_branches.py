"""Collection legal-hold branches that no other suite pinned (repo cleanup b11).

Found by the ``media_collections`` split's mutation comparison: a refusal must be audited, and the
hold uuids on the collection list are for the organization's admin only.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver


@pytest.fixture()
def holds_on(monkeypatch):
    monkeypatch.setattr("app.services.minio_service.set_object_legal_hold", lambda *a, **k: True)
    set_capability_resolver(
        lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": True, "org.legal_hold": True}
    )
    yield
    reset_capability_resolver()


def _hold(client, gov, collection) -> None:
    body = {
        "scope": "collection",
        "collection_uuid": str(collection.uuid),
        "reason": "matter 7 hold",
    }
    with gov.acting_as(gov.admin):
        response = client.post("/api/org-admin/holds", json=body)
    assert response.status_code == status.HTTP_201_CREATED, response.text


def test_a_hold_refusal_is_audited(client, gov, holds_on, monkeypatch):
    collection = gov.collection(gov.alice)
    media_file = gov.file(gov.alice)
    gov.put(collection, media_file)
    _hold(client, gov, collection)
    events: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: events.append(kw))
    url = f"/api/collections/{collection.uuid}"
    with gov.acting_as(gov.alice):
        removed = client.request(
            "DELETE", f"{url}/media", json={"media_file_ids": [str(media_file.uuid)]}
        )
        assert removed.status_code == status.HTTP_409_CONFLICT
        assert client.delete(url).status_code == status.HTTP_409_CONFLICT
    refused = [e for e in events if e["event_type"] == AuditEventType.CONTENT_HOLD_REFUSED]
    assert [e["details"]["action"] for e in refused] == [
        "remove_from_collection",
        "delete_collection",
    ]


def test_the_list_reveals_hold_uuids_only_to_the_org_admin(client, gov, holds_on):
    collection = gov.collection(gov.alice)
    _hold(client, gov, collection)

    def hold_of(person) -> dict:
        with gov.acting_as(person):
            rows = client.get("/api/collections").json()
        return next(r["hold"] for r in rows if r["name"] == collection.name)

    member, admin = hold_of(gov.alice), hold_of(gov.admin)
    assert member["held"] is True and member["hold_uuids"] == []
    assert admin["held"] is True and len(admin["hold_uuids"]) == 1


def test_a_file_joining_a_held_collection_gets_the_object_lock(client, gov, holds_on, monkeypatch):
    locked: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "app.services.minio_service.set_object_legal_hold",
        lambda path, on: locked.append((path, on)),
    )
    held, free = gov.collection(gov.alice), gov.collection(gov.alice)
    _hold(client, gov, held)
    joining, other = gov.file(gov.alice), gov.file(gov.alice)
    with gov.acting_as(gov.alice):
        for collection, media_file in ((free, other), (held, joining)):
            response = client.post(
                f"/api/collections/{collection.uuid}/media",
                json={"media_file_ids": [str(media_file.uuid)]},
            )
            assert response.status_code == 200, response.text
    assert locked == [(joining.storage_path, True)]
