"""A restricted organization collection is visible only to the people named on it (#1275).

``collection.visibility = 'restricted'`` limits an organization collection to its creator, the
organization's admins and the people it is explicitly shared with. Everyone else in the
organization must not learn it exists, from ANY surface: the lists, the detail routes, a file's
own collection list, the gallery, chat scope, upload targets, the AI auto-labeller, search, and
platform support access. Each test below is one of those surfaces with an unnamed member (bob)
as the caller, and keeps a control (the named people still see it) so a pass cannot mean
"nothing is visible to anybody".
"""

from __future__ import annotations

import pytest

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.topic import TopicSuggestion


@pytest.fixture()
def restricted(gov):
    """A restricted collection created by alice, holding bob's file and one of alice's."""
    collection = gov.collection(gov.alice, name="Matter 7 review", visibility="restricted")
    bob_file = gov.file(gov.bob)
    alice_file = gov.file(gov.alice)
    gov.put(collection, bob_file, alice_file)
    return type(
        "Restricted",
        (),
        {"collection": collection, "bob_file": bob_file, "alice_file": alice_file},
    )


def _names(client, ownership: str) -> set[str]:
    response = client.get("/api/collections", params={"ownership": ownership})
    assert response.status_code == 200, response.text
    return {row["name"] for row in response.json()}


@pytest.mark.parametrize("ownership", ["mine", "shared", "all"])
def test_restricted_collection_absent_from_every_list_for_unnamed_member(
    client, gov, restricted, ownership
):
    with gov.acting_as(gov.bob):
        assert restricted.collection.name not in _names(client, ownership)
        shared = client.get("/api/collections/shared-with-me")
        assert shared.status_code == 200, shared.text
        assert restricted.collection.name not in {row["name"] for row in shared.json()}
    # Controls: its creator and an organization admin see it in their own list.
    for person in (gov.alice, gov.admin):
        with gov.acting_as(person):
            assert restricted.collection.name in _names(client, "mine")


def test_restricted_detail_media_shares_404_for_unnamed_member(client, gov, restricted):
    base = f"/api/collections/{restricted.collection.uuid}"
    with gov.acting_as(gov.bob):
        for method, path, body in (
            ("GET", base, None),
            ("GET", f"{base}/media", None),
            ("GET", f"{base}/shares", None),
            ("PUT", base, {"name": "renamed"}),
            ("PATCH", f"{base}/visibility", {"visibility": "organization"}),
            ("POST", f"{base}/media", {"media_file_ids": [str(restricted.bob_file.uuid)]}),
            ("DELETE", f"{base}/media", {"media_file_ids": [str(restricted.bob_file.uuid)]}),
            ("DELETE", base, None),
        ):
            response = client.request(method, path, json=body)
            assert response.status_code == 404, f"{method} {path}: {response.text}"
            assert response.json()["detail"] == "Collection not found"
    with gov.acting_as(gov.alice):
        assert client.get(base).status_code == 200


def test_unknown_and_hidden_collections_answer_identically(client, gov, restricted):
    import uuid

    with gov.acting_as(gov.bob):
        hidden = client.get(f"/api/collections/{restricted.collection.uuid}")
        missing = client.get(f"/api/collections/{uuid.uuid4()}")
    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())


def test_named_people_hold_the_expected_permission(gov, restricted):
    from app.services.permission_service import PermissionService

    gov.share(restricted.collection, gov.bob, "viewer")
    perms = {
        name: PermissionService.get_collection_permission(
            gov.db, restricted.collection.id, person.id
        )
        for name, person in (
            ("creator", gov.alice),
            ("admin", gov.admin),
            ("named", gov.bob),
            ("outsider", gov.outsider),
        )
    }
    assert perms == {"creator": "owner", "admin": "owner", "named": "viewer", "outsider": None}


def test_a_named_member_sees_it_and_the_files_in_it(client, gov, restricted):
    gov.share(restricted.collection, gov.bob, "viewer")
    with gov.acting_as(gov.bob):
        assert restricted.collection.name in _names(client, "mine")
        opened = client.get(f"/api/collections/{restricted.collection.uuid}/media")
        assert opened.status_code == 200, opened.text
        assert {f["uuid"] for f in opened.json()["items"]} == {
            str(restricted.bob_file.uuid),
            str(restricted.alice_file.uuid),
        }


def test_file_owner_does_not_see_restricted_collection_holding_their_file(client, gov, restricted):
    with gov.acting_as(gov.bob):
        detail = client.get(f"/api/files/{restricted.bob_file.uuid}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["collections"] == []
        on_selection = client.get(
            "/api/collections/for-files", params={"file_uuids": str(restricted.bob_file.uuid)}
        )
        assert on_selection.status_code == 200, on_selection.text
        assert on_selection.json() == []
    with gov.acting_as(gov.alice):
        named = client.get(f"/api/files/{restricted.bob_file.uuid}")
        assert [c["name"] for c in named.json()["collections"]] == [restricted.collection.name]


def test_files_reachable_only_through_a_restricted_collection_stay_unreadable(
    client, gov, restricted
):
    """Restriction removes the collection as a route to alice's file; it revokes no other."""
    with gov.acting_as(gov.bob):
        assert client.get(f"/api/files/{restricted.alice_file.uuid}").status_code == 404
        listed = client.get("/api/files", params={"ownership": "all"})
        uuids = {row["uuid"] for row in listed.json()["items"]}
    assert str(restricted.alice_file.uuid) not in uuids
    assert str(restricted.bob_file.uuid) in uuids, "the file's own owner still reads it"
    with gov.acting_as(gov.admin):
        assert client.get(f"/api/files/{restricted.alice_file.uuid}").status_code == 200


def test_gallery_counts_and_tag_facets_exclude_restricted_only_files(gov, restricted):
    from sqlalchemy import select

    from app.services.permission_service import PermissionService

    sub = PermissionService.get_accessible_file_ids_subquery(
        gov.db, gov.bob.id, organization_id=gov.org.id
    )
    ids = set(gov.db.execute(select(sub.c.id)).scalars())
    assert restricted.alice_file.id not in ids
    admin_sub = PermissionService.get_accessible_file_ids_subquery(
        gov.db, gov.admin.id, organization_id=gov.org.id
    )
    assert restricted.alice_file.id in set(gov.db.execute(select(admin_sub.c.id)).scalars())


def test_search_with_hidden_collection_id_returns_nothing(gov, restricted):
    from app.services.collection_visibility import NO_VISIBLE_COLLECTION
    from app.services.collection_visibility import resolve_collection_filter

    for kwargs in (
        {"collection_uuid": restricted.collection.uuid},
        {"collection_id": restricted.collection.id},
    ):
        assert (
            resolve_collection_filter(
                gov.db, user_id=gov.bob.id, organization_id=gov.org.id, **kwargs
            )
            == NO_VISIBLE_COLLECTION
        )
        assert (
            resolve_collection_filter(
                gov.db, user_id=gov.alice.id, organization_id=gov.org.id, **kwargs
            )
            == restricted.collection.id
        )


def test_chat_scope_refuses_restricted_collection_for_unnamed_member(gov, restricted):
    from app.api.deps_context import RequestContext
    from app.services.chat.context_resolver import _resolve_collections

    def scope(user, role="org:member"):
        ctx = RequestContext(user=user, org_id=gov.org.id, org_role=role)
        return _resolve_collections(gov.db, ctx, [str(restricted.collection.uuid)])

    assert scope(gov.bob) == set()
    assert scope(gov.alice) == {str(restricted.bob_file.uuid), str(restricted.alice_file.uuid)}


def test_upload_into_hidden_collection_is_skipped_exactly_like_a_missing_one(gov, restricted):
    """The import is not refused, so the answer carries no existence signal either way."""
    from app.api.endpoints.files.prepare_upload import add_file_to_collections

    new_file = gov.file(gov.bob)
    add_file_to_collections(gov.db, new_file.id, gov.bob.id, [restricted.collection.uuid])
    gov.db.commit()
    assert not gov.db.query(CollectionMember).filter_by(media_file_id=new_file.id).count()

    gov.share(restricted.collection, gov.bob, "editor")
    add_file_to_collections(gov.db, new_file.id, gov.bob.id, [restricted.collection.uuid])
    gov.db.commit()
    assert gov.db.query(CollectionMember).filter_by(media_file_id=new_file.id).count() == 1


def test_auto_label_never_matches_or_adds_to_hidden_collection(gov, restricted):
    from app.services.auto_label_service import AutoLabelService

    bob_file = gov.file(gov.bob)
    suggestion = TopicSuggestion(
        media_file_id=bob_file.id,
        user_id=gov.bob.id,
        suggested_tags=[],
        suggested_collections=[
            {"name": "Matter 7 review", "confidence": 0.99},
            {"name": "Matter 7 reviews", "confidence": 0.99},
        ],
    )
    gov.db.add(suggestion)
    gov.db.commit()
    before = gov.db.query(Collection).filter_by(organization_id=gov.org.id).count()

    result = AutoLabelService(gov.db).auto_apply_suggestions(
        bob_file, suggestion, gov.bob.id, confidence_threshold=0.5
    )

    assert result["auto_applied_collections"] == []
    assert set(result["skipped_collections"]) == {"Matter 7 review", "Matter 7 reviews"}
    assert gov.db.query(Collection).filter_by(organization_id=gov.org.id).count() == before
    assert not gov.db.query(CollectionMember).filter_by(media_file_id=bob_file.id).count()


def test_auto_label_still_uses_a_collection_the_owner_can_see(gov):
    from app.services.auto_label_service import AutoLabelService

    visible = gov.collection(gov.alice, name="Weekly sync")
    bob_file = gov.file(gov.bob)
    suggestion = TopicSuggestion(
        media_file_id=bob_file.id,
        user_id=gov.bob.id,
        suggested_tags=[],
        suggested_collections=[{"name": "Weekly sync", "confidence": 0.99}],
    )
    gov.db.add(suggestion)
    gov.db.commit()

    result = AutoLabelService(gov.db).auto_apply_suggestions(
        bob_file, suggestion, gov.bob.id, confidence_threshold=0.5
    )

    assert result["auto_applied_collections"] == ["Weekly sync"]
    assert (
        gov.db.query(CollectionMember)
        .filter_by(media_file_id=bob_file.id, collection_id=visible.id)
        .count()
        == 1
    )


def test_org_grant_cannot_see_restricted_collection(gov, restricted, db_session):
    """A platform bypass is consulted AFTER the need-to-know rule, never before it."""
    from fastapi import HTTPException

    from app.services.platform_access import TenancyMode
    from app.services.platform_bypass import PlatformBypass
    from app.utils.uuid_helpers import get_collection_by_uuid_with_permission
    from app.utils.uuid_helpers import get_collection_by_uuid_with_sharing

    staff = gov.user("staff")
    staff.role = "admin"
    gov.db.commit()
    bypass = PlatformBypass(user=staff, mode=TenancyMode.SINGLE)
    open_collection = gov.collection(gov.alice, name="Open to everyone")
    assert (
        get_collection_by_uuid_with_permission(
            gov.db, open_collection.uuid, staff.id, bypass=bypass
        ).id
        == open_collection.id
    ), "control: this bypass does reach an unrestricted collection"

    for resolver in (get_collection_by_uuid_with_permission, get_collection_by_uuid_with_sharing):
        with pytest.raises(HTTPException) as refused:
            resolver(gov.db, restricted.collection.uuid, staff.id, bypass=bypass)
        assert refused.value.status_code == 404


class _Recorder:
    def __init__(self):
        self.events: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.events.append((args, kwargs))


@pytest.fixture()
def side_effects(monkeypatch):
    from app.api.endpoints import media_collections
    from app.services.chat import retrieval_cache

    ws, audit, bumps = _Recorder(), _Recorder(), _Recorder()
    monkeypatch.setattr(media_collections, "send_ws_event", ws)
    monkeypatch.setattr(media_collections.audit_logger, "log", audit)
    monkeypatch.setattr(retrieval_cache, "bump_corpus_version", bumps)
    return type("Effects", (), {"ws": ws, "audit": audit, "bumps": bumps})


def test_switch_to_restricted_takes_effect_on_next_list_without_ttl_wait(client, gov, side_effects):
    collection = gov.collection(gov.alice, name="Board packet")
    with gov.acting_as(gov.bob):
        assert "Board packet" in _names(client, "mine")
    with gov.acting_as(gov.alice):
        changed = client.patch(
            f"/api/collections/{collection.uuid}/visibility", json={"visibility": "restricted"}
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["visibility"] == "restricted"
    with gov.acting_as(gov.bob):
        assert "Board packet" not in _names(client, "mine")
    with gov.acting_as(gov.alice):
        reopened = client.patch(
            f"/api/collections/{collection.uuid}/visibility", json={"visibility": "organization"}
        )
        assert reopened.status_code == 200, reopened.text
    with gov.acting_as(gov.bob):
        assert "Board packet" in _names(client, "mine")


def test_visibility_ws_payload_has_no_name(client, gov, side_effects):
    collection = gov.collection(gov.alice, name="Confidential name")
    gov.share(collection, gov.admin)
    with gov.acting_as(gov.alice):
        client.patch(
            f"/api/collections/{collection.uuid}/visibility", json={"visibility": "restricted"}
        )
    sent = {(args[0], args[1]): args[2] for args, _kw in side_effects.ws.events}
    assert sent == {
        (gov.bob.id, "collection_access_changed"): {"collection_uuid": str(collection.uuid)}
    }, (
        "only the member who lost sight of it is told; the creator, an admin and a share target are not"
    )
    assert "Confidential" not in repr(side_effects.ws.events)


def test_visibility_change_is_audited_without_the_name_and_clears_the_chat_cache(
    client, gov, side_effects
):
    collection = gov.collection(gov.alice, name="Do not log this name")
    with gov.acting_as(gov.alice):
        client.patch(
            f"/api/collections/{collection.uuid}/visibility", json={"visibility": "restricted"}
        )
    assert len(side_effects.bumps.events) == 1
    ((_, kwargs),) = side_effects.audit.events
    assert kwargs["event_type"].value == "collection.visibility.change"
    assert kwargs["organization_id"] == gov.org.id
    assert kwargs["details"]["to"] == "restricted"
    assert kwargs["details"]["members_who_lost_access"] == 1, "bob; the admin and creator keep it"
    assert "Do not log this name" not in repr(kwargs)


def test_setting_the_same_visibility_again_is_a_no_op(client, gov, side_effects):
    collection = gov.collection(gov.alice)
    with gov.acting_as(gov.alice):
        again = client.patch(
            f"/api/collections/{collection.uuid}/visibility", json={"visibility": "organization"}
        )
    assert again.status_code == 200
    assert not side_effects.audit.events and not side_effects.bumps.events


def test_only_an_owner_changes_visibility(client, gov, side_effects):
    collection = gov.collection(gov.alice, name="Owners only")
    with gov.acting_as(gov.bob):
        member = client.patch(
            f"/api/collections/{collection.uuid}/visibility", json={"visibility": "restricted"}
        )
    assert member.status_code == 403, member.text
    with gov.acting_as(gov.admin):
        admin = client.patch(
            f"/api/collections/{collection.uuid}/visibility", json={"visibility": "restricted"}
        )
    assert admin.status_code == 200, admin.text


def test_a_personal_collection_cannot_be_restricted(client, gov):
    personal = gov.collection(gov.alice, org=None)
    with gov.acting_as(gov.alice, org=None):
        patched = client.patch(
            f"/api/collections/{personal.uuid}/visibility", json={"visibility": "restricted"}
        )
        created = client.post(
            "/api/collections", json={"name": "mine only", "visibility": "restricted"}
        )
    assert patched.status_code == 422, patched.text
    assert created.status_code == 422, created.text


def test_create_restricted_and_the_ui_flag(client, gov):
    with gov.acting_as(gov.bob):
        created = client.post(
            "/api/collections", json={"name": "Bob's matter", "visibility": "restricted"}
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert (body["visibility"], body["can_change_visibility"]) == ("restricted", True)
        listed = {row["name"]: row for row in client.get("/api/collections").json()}
        assert listed["Bob's matter"]["can_change_visibility"] is True
    with gov.acting_as(gov.alice):
        assert "Bob's matter" not in _names(client, "all")
    with gov.acting_as(gov.admin):
        assert "Bob's matter" in _names(client, "all")


def test_a_plain_member_cannot_change_visibility_of_a_colleagues_collection(client, gov):
    collection = gov.collection(gov.alice, name="Alice's open one")
    with gov.acting_as(gov.bob):
        row = {r["name"]: r for r in client.get("/api/collections").json()}["Alice's open one"]
    assert row["can_change_visibility"] is False
    assert row["visibility"] == "organization"
    assert collection.visibility == "organization"
