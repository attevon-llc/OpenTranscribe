"""Collection share and membership gates that no other suite pinned (repo cleanup b11).

Each test kills a mutant of the ``media_collections`` split's HEAD-vs-split comparison that
survived the existing suites, and keeps a control so a refusal cannot be a broken route.
"""

from __future__ import annotations

from app.core.constants import NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED
from app.core.constants import NOTIFICATION_TYPE_COLLECTION_SHARED
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.sharing import CollectionShare


def _share_uuid(gov, collection) -> str:
    share = gov.db.query(CollectionShare).filter_by(collection_id=collection.id).one()
    return str(share.uuid)


def _media(client, collection, media_file, method: str = "DELETE"):
    return client.request(
        method,
        f"/api/collections/{collection.uuid}/media",
        json={"media_file_ids": [str(media_file.uuid)]},
    )


def test_only_the_owner_manages_shares(client, gov):
    collection = gov.collection(gov.alice, org=None)
    gov.share(collection, gov.bob, permission="editor")
    base = f"/api/collections/{collection.uuid}/shares"
    share = f"{base}/{_share_uuid(gov, collection)}"
    body = {"target_type": "user", "target_uuid": str(gov.outsider.uuid), "permission": "viewer"}
    with gov.acting_as(gov.bob, org=None):
        assert client.post(base, json=body).status_code == 403
        assert client.put(share, json={"permission": "viewer"}).status_code == 403
        assert client.delete(share).status_code == 403
    with gov.acting_as(gov.alice, org=None):
        assert client.put(share, json={"permission": "viewer"}).status_code == 200


def test_share_routes_answer_only_in_the_collections_tenant(client, gov):
    collection = gov.collection(gov.alice)
    gov.share(collection, gov.bob)
    base = f"/api/collections/{collection.uuid}/shares"
    share = f"{base}/{_share_uuid(gov, collection)}"
    with gov.acting_as(gov.alice, org=None):
        assert client.get(base).status_code == 403
        assert client.put(share, json={"permission": "editor"}).status_code == 403
    with gov.acting_as(gov.alice):
        assert client.get(base).status_code == 200
        assert client.put(share, json={"permission": "editor"}).status_code == 200


def test_a_group_cannot_be_shared_the_same_collection_twice(client, gov):
    collection = gov.collection(gov.alice, org=None)
    group = UserGroup(name="gate group", owner_id=gov.alice.id)
    gov.db.add(group)
    gov.db.commit()
    for member in (gov.alice, gov.bob):
        gov.db.add(UserGroupMember(group_id=group.id, user_id=member.id))
    gov.db.commit()
    body = {"target_type": "group", "target_uuid": str(group.uuid), "permission": "viewer"}
    with gov.acting_as(gov.alice, org=None):
        url = f"/api/collections/{collection.uuid}/shares"
        assert client.post(url, json=body).status_code == 201
        assert client.post(url, json=body).status_code == 400


def test_each_share_change_reindexes_the_members_and_notifies(client, gov, monkeypatch):
    from app.api.endpoints import media_collections_shares as shares

    reindexed: list[list[int]] = []
    notified: list[tuple[int, str]] = []
    monkeypatch.setattr(
        shares.update_file_access_index, "delay", lambda ids: reindexed.append(list(ids))
    )
    monkeypatch.setattr(
        shares, "send_ws_event", lambda uid, kind, _data: notified.append((uid, kind))
    )
    collection = gov.collection(gov.alice, org=None)
    media_file = gov.file(gov.alice, org=None)
    gov.put(collection, media_file)
    base = f"/api/collections/{collection.uuid}/shares"
    body = {"target_type": "user", "target_uuid": str(gov.bob.uuid), "permission": "viewer"}
    with gov.acting_as(gov.alice, org=None):
        created = client.post(base, json=body)
        assert created.status_code == 201
        assert created.json()["permission"] == "viewer"
        share = f"{base}/{_share_uuid(gov, collection)}"
        assert client.put(share, json={"permission": "editor"}).status_code == 200
        assert client.delete(share).status_code == 204
    assert reindexed == [[media_file.id]] * 3
    assert notified[:2] == [
        (gov.bob.id, NOTIFICATION_TYPE_COLLECTION_SHARED),
        (gov.bob.id, NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED),
    ]


def test_a_share_is_updated_only_through_its_own_collection(client, gov):
    owned, other = gov.collection(gov.alice, org=None), gov.collection(gov.alice, org=None)
    gov.share(owned, gov.bob)
    share_uuid = _share_uuid(gov, owned)
    with gov.acting_as(gov.alice, org=None):
        wrong = f"/api/collections/{other.uuid}/shares/{share_uuid}"
        assert client.put(wrong, json={"permission": "editor"}).status_code == 404
        right = f"/api/collections/{owned.uuid}/shares/{share_uuid}"
        assert client.put(right, json={"permission": "editor"}).status_code == 200


def test_a_personal_collection_takes_only_the_callers_files(client, gov):
    collection = gov.collection(gov.alice, org=None)
    with gov.acting_as(gov.alice, org=None):
        assert _media(client, collection, gov.file(gov.bob, org=None), "POST").status_code == 404
        assert _media(client, collection, gov.file(gov.alice, org=None), "POST").status_code == 200


def test_a_shared_editor_removes_only_their_own_files(client, gov):
    collection = gov.collection(gov.alice, org=None)
    alices = gov.file(gov.alice, org=None)
    gov.put(collection, alices)
    gov.share(collection, gov.bob, permission="editor")
    with gov.acting_as(gov.bob, org=None):
        assert _media(client, collection, alices).json()["removed"] == 0
    with gov.acting_as(gov.alice, org=None):
        assert _media(client, collection, alices).json()["removed"] == 1


def test_any_member_removes_any_file_of_an_org_collection(client, gov):
    collection = gov.collection(gov.alice)
    alices = gov.file(gov.alice)
    gov.put(collection, alices)
    with gov.acting_as(gov.bob):
        assert _media(client, collection, alices).json()["removed"] == 1


def test_the_transcript_search_is_scoped_to_the_tenant_and_owner(client, gov, monkeypatch):
    from app.api.endpoints.files import filtering

    seen: list[dict] = []

    def record(query, transcript_search, user_id=None, *, organization_id):
        seen.append({"user_id": user_id, "organization_id": organization_id})
        return query

    monkeypatch.setattr(filtering, "apply_transcript_search_filter", record)
    org_coll, personal = gov.collection(gov.alice), gov.collection(gov.alice, org=None)
    params = {"transcript_search": "budget"}
    with gov.acting_as(gov.bob):
        assert (
            client.get(f"/api/collections/{org_coll.uuid}/media", params=params).status_code == 200
        )
    with gov.acting_as(gov.alice, org=None):
        assert (
            client.get(f"/api/collections/{personal.uuid}/media", params=params).status_code == 200
        )
    assert seen == [
        {"user_id": None, "organization_id": gov.org.id},
        {"user_id": gov.alice.id, "organization_id": None},
    ]
