"""Collections belong to a tenant, not to a user (issue #1051).

The owner decision these tests pin: **inside an organization a collection is
shared by the organization** (the same rule as tags, #1050); a personal-workspace
collection stays its owner's.

* A collection created while working in an org is stamped with that org, so the
  creator — and every other member — sees it there, and nobody sees it in any
  other tenant or in a personal workspace.
* Every member may use an org collection (view it, add and remove the org's
  files); deleting it and managing its explicit shares is for its creator or an
  org admin.
* AI auto-collections are created in the **file's** tenant and never reuse a
  same-named collection from another tenant, so one collection can no longer
  hold files from several tenants.
* Names are unique per tenant: the same user may hold "Board" in two orgs.

Requests run as a specific user in a specific tenant by overriding the two auth
dependencies — the shape ``test_tag_tenancy.py`` uses.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager

import pytest

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.topic import TopicSuggestion
from app.models.user import User


def _unique(prefix: str) -> str:
    return f"{prefix} {uuid_pkg.uuid4().hex[:8]}"


def _mk_user(db, label: str) -> User:
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=f"{label} user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_org(db, label: str) -> Organization:
    org = Organization(
        external_org_id=f"org_{label}_{uuid_pkg.uuid4().hex[:8]}",
        name=f"{label} Org",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _join(db, org: Organization, user: User, role: str = "org:member") -> None:
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
    db.commit()


def _mk_file(db, user: User, org_id: int | None) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=fuuid,
        filename=f"coll_tenancy_{str(fuuid)[:8]}.mp3",
        storage_path=f"coll_tenancy/{fuuid}.mp3",
        content_type="audio/mpeg",
        file_size=1,
        user_id=user.id,
        organization_id=org_id,
        status="completed",
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _mk_collection(db, name: str, user: User | None, org_id: int | None) -> Collection:
    coll = Collection(name=name, user_id=user.id if user else None, organization_id=org_id)
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


@contextmanager
def _acting_as(user: User, org_id: int | None, org_role: str | None = "org:member"):
    """Run requests as ``user`` in tenant ``org_id`` (None = personal workspace)."""
    from app.api.deps_context import RequestContext
    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user
    from app.main import app

    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=user, org_id=org_id, org_role=org_role if org_id is not None else None
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_context, None)


def _listed(client, ownership: str = "mine") -> dict[str, dict]:
    response = client.get("/api/collections", params={"ownership": ownership})
    assert response.status_code == 200, response.text
    return {row["name"]: row for row in response.json()}


@pytest.fixture()
def org_world(db_session):
    """Org A (alice admin, bob member), org B (alice member), and carol outside both."""
    db = db_session
    org_a = _mk_org(db, "A")
    org_b = _mk_org(db, "B")
    alice = _mk_user(db, "alice")
    bob = _mk_user(db, "bob")
    carol = _mk_user(db, "carol")
    _join(db, org_a, alice, role="org:admin")
    _join(db, org_a, bob)
    _join(db, org_b, alice)
    return type(
        "World",
        (),
        {"db": db, "org_a": org_a, "org_b": org_b, "alice": alice, "bob": bob, "carol": carol},
    )


# --------------------------------------------------------------------------- #
# Create stamps the tenant; the org's members all see it                       #
# --------------------------------------------------------------------------- #


def test_collection_created_in_an_org_is_stamped_and_listed_there(client, org_world):
    w = org_world
    name = _unique("Board")

    with _acting_as(w.bob, w.org_a.id):
        created = client.post("/api/collections", json={"name": name})
        assert created.status_code == 200, created.text
        assert name in _listed(client), "the creator must see it where they made it"

    row = w.db.query(Collection).filter(Collection.uuid == uuid_pkg.UUID(created.json()["uuid"]))
    assert row.one().organization_id == w.org_a.id

    with _acting_as(w.bob, None):
        assert name not in _listed(client, "all"), "an org collection is not personal"


def test_second_org_member_sees_and_uses_the_orgs_collection(client, org_world):
    w = org_world
    name = _unique("Customer Calls")
    alice_file = _mk_file(w.db, w.alice, w.org_a.id)
    bob_file = _mk_file(w.db, w.bob, w.org_a.id)

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        created = client.post("/api/collections", json={"name": name}).json()
        added = client.post(
            f"/api/collections/{created['uuid']}/media",
            json={"media_file_ids": [str(alice_file.uuid)]},
        )
        assert added.status_code == 200, added.text

    with _acting_as(w.bob, w.org_a.id):
        listed = _listed(client)
        assert name in listed, "a colleague must see the org's collection"
        assert listed[name]["my_permission"] == "editor"
        assert client.get(f"/api/collections/{created['uuid']}").status_code == 200

        # A member adds their own org file to it ...
        added = client.post(
            f"/api/collections/{created['uuid']}/media",
            json={"media_file_ids": [str(bob_file.uuid)]},
        )
        assert added.status_code == 200, added.text

        # ... and sees every member file in it, not just their own.
        media = client.get(f"/api/collections/{created['uuid']}/media")
        assert media.status_code == 200, media.text
        assert {f["uuid"] for f in media.json()["items"]} == {
            str(alice_file.uuid),
            str(bob_file.uuid),
        }

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        media = client.get(f"/api/collections/{created['uuid']}/media")
        assert media.json()["total"] == 2, "the creator sees the colleague's file too"


def test_collection_in_one_org_is_invisible_in_another_org_and_personal(client, org_world):
    w = org_world
    name = _unique("Falcon")

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        created = client.post("/api/collections", json={"name": name}).json()

    with _acting_as(w.alice, w.org_b.id):
        assert name not in _listed(client, "all")
        assert client.get(f"/api/collections/{created['uuid']}").status_code == 403
    with _acting_as(w.alice, None):
        assert name not in _listed(client, "all")
    with _acting_as(w.carol, None):
        assert client.get(f"/api/collections/{created['uuid']}").status_code == 403


def test_personal_collection_is_not_shared_with_the_org(client, org_world):
    w = org_world
    name = _unique("Diary")

    with _acting_as(w.alice, None):
        assert client.post("/api/collections", json={"name": name}).status_code == 200
        assert name in _listed(client)

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        assert name not in _listed(client, "all")
    with _acting_as(w.bob, w.org_a.id):
        assert name not in _listed(client, "all")


def test_same_name_in_two_tenants_but_unique_inside_one(client, org_world):
    w = org_world
    name = _unique("Board")

    with _acting_as(w.alice, None):
        assert client.post("/api/collections", json={"name": name}).status_code == 200
    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        assert client.post("/api/collections", json={"name": name}).status_code == 200
    with _acting_as(w.alice, w.org_b.id):
        assert client.post("/api/collections", json={"name": name}).status_code == 200
    with _acting_as(w.bob, w.org_a.id):
        dup = client.post("/api/collections", json={"name": name})
        assert dup.status_code == 400, "one org holds one collection of a name"

    tenants = {c.organization_id for c in w.db.query(Collection).filter(Collection.name == name)}
    assert tenants == {None, w.org_a.id, w.org_b.id}


def test_member_cannot_delete_a_colleagues_org_collection_but_admin_can(client, org_world):
    w = org_world
    name = _unique("Roadmap")

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        mine = client.post("/api/collections", json={"name": name}).json()
    with _acting_as(w.bob, w.org_a.id):
        assert client.delete(f"/api/collections/{mine['uuid']}").status_code == 403
        bobs = client.post("/api/collections", json={"name": name + " b"}).json()
        renamed = client.put(f"/api/collections/{mine['uuid']}", json={"description": "x"})
        assert renamed.status_code == 200, "members are editors of the org's collections"
    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        assert client.delete(f"/api/collections/{bobs['uuid']}").status_code == 200
    with _acting_as(w.bob, w.org_a.id):
        assert client.get(f"/api/collections/{bobs['uuid']}").status_code == 404


def test_for_files_is_tenant_scoped(client, org_world):
    w = org_world
    org_file = _mk_file(w.db, w.alice, w.org_a.id)
    org_coll = _mk_collection(w.db, _unique("Org Coll"), w.alice, w.org_a.id)
    stray = _mk_collection(w.db, _unique("Stray Personal"), w.alice, None)
    for coll in (org_coll, stray):
        w.db.add(CollectionMember(collection_id=coll.id, media_file_id=org_file.id))
    w.db.commit()

    with _acting_as(w.bob, w.org_a.id):
        response = client.get("/api/collections/for-files", params={"file_uuids": [org_file.uuid]})
        assert response.status_code == 200, response.text
        assert {r["name"] for r in response.json()} == {org_coll.name}
    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        response = client.get("/api/collections/for-files", params={"file_uuids": [org_file.uuid]})
        assert {r["name"] for r in response.json()} == {org_coll.name}


def test_file_detail_lists_the_orgs_collections(client, org_world):
    w = org_world
    org_file = _mk_file(w.db, w.alice, w.org_a.id)
    org_coll = _mk_collection(w.db, _unique("Detail"), w.alice, w.org_a.id)
    w.db.add(CollectionMember(collection_id=org_coll.id, media_file_id=org_file.id))
    w.db.commit()

    from app.api.endpoints.files.crud import get_file_collections

    names = {c["name"] for c in get_file_collections(w.db, org_file.id, w.bob.id, w.org_a.id)}
    assert names == {org_coll.name}


def test_shared_with_me_is_tenant_gated(client, org_world):
    """An org collection shared with a member is not "shared" in their personal workspace."""
    w = org_world
    name = _unique("Shared Org")
    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        created = client.post("/api/collections", json={"name": name}).json()
        share = client.post(
            f"/api/collections/{created['uuid']}/shares",
            json={"target_type": "user", "target_uuid": str(w.bob.uuid), "permission": "viewer"},
        )
        assert share.status_code == 201, share.text

    with _acting_as(w.bob, None):
        shared = client.get("/api/collections/shared-with-me")
        assert shared.status_code == 200
        assert name not in {r["name"] for r in shared.json()}


def test_colleague_cannot_manage_shares_of_an_org_collection(client, org_world):
    w = org_world
    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        created = client.post("/api/collections", json={"name": _unique("Shares")}).json()
    with _acting_as(w.bob, w.org_a.id):
        assert client.get(f"/api/collections/{created['uuid']}/shares").status_code == 403


# --------------------------------------------------------------------------- #
# Files added through import paths land only in same-tenant collections        #
# --------------------------------------------------------------------------- #


def test_import_adds_files_only_to_collections_of_the_files_tenant(org_world):
    from app.api.endpoints.files.prepare_upload import add_file_to_collections

    w = org_world
    personal = _mk_collection(w.db, _unique("Mine"), w.bob, None)
    org_coll = _mk_collection(w.db, _unique("Theirs"), w.alice, w.org_a.id)
    other_org = _mk_collection(w.db, _unique("Elsewhere"), w.alice, w.org_b.id)
    org_file = _mk_file(w.db, w.bob, w.org_a.id)

    add_file_to_collections(
        w.db, org_file.id, w.bob.id, [personal.uuid, org_coll.uuid, other_org.uuid]
    )
    w.db.commit()

    members = {
        m.collection_id
        for m in w.db.query(CollectionMember).filter(CollectionMember.media_file_id == org_file.id)
    }
    assert members == {org_coll.id}


# --------------------------------------------------------------------------- #
# AI auto-collections land in the file's tenant                                #
# --------------------------------------------------------------------------- #


def _suggest(db, media_file: MediaFile, name: str) -> TopicSuggestion:
    suggestion = TopicSuggestion(
        media_file_id=media_file.id,
        user_id=media_file.user_id,
        suggested_tags=[],
        suggested_collections=[{"name": name, "confidence": 0.99}],
        status="pending",
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


def _collections_of(db, media_file: MediaFile) -> list[Collection]:
    rows: list[Collection] = (
        db.query(Collection)
        .join(CollectionMember, CollectionMember.collection_id == Collection.id)
        .filter(CollectionMember.media_file_id == media_file.id)
        .all()
    )
    return rows


def test_auto_collections_land_in_the_files_tenant(org_world):
    from app.services.auto_label_service import AutoLabelService

    w = org_world
    name = _unique("Acquisition Talks")
    # The owner already has this name privately and in another org: neither may
    # be reused for an org-A recording.
    personal = _mk_collection(w.db, name, w.alice, None)
    elsewhere = _mk_collection(w.db, name, w.alice, w.org_b.id)

    org_file = _mk_file(w.db, w.alice, w.org_a.id)
    AutoLabelService(w.db).auto_apply_suggestions(
        media_file=org_file,
        suggestion=_suggest(w.db, org_file, name),
        user_id=w.alice.id,
        confidence_threshold=0.5,
        apply_tags=False,
    )

    [coll] = _collections_of(w.db, org_file)
    assert coll.id not in (personal.id, elsewhere.id)
    assert coll.organization_id == w.org_a.id

    # A colleague's org file with the same suggestion converges on the org's row.
    bob_file = _mk_file(w.db, w.bob, w.org_a.id)
    AutoLabelService(w.db).auto_apply_suggestions(
        media_file=bob_file,
        suggestion=_suggest(w.db, bob_file, name),
        user_id=w.bob.id,
        confidence_threshold=0.5,
        apply_tags=False,
    )
    assert [c.id for c in _collections_of(w.db, bob_file)] == [coll.id]


def test_auto_collection_on_a_personal_file_does_not_join_an_org_collection(org_world):
    from app.services.auto_label_service import AutoLabelService

    w = org_world
    name = _unique("Gardening")
    org_coll = _mk_collection(w.db, name, w.alice, w.org_a.id)
    personal_file = _mk_file(w.db, w.alice, None)

    AutoLabelService(w.db).auto_apply_suggestions(
        media_file=personal_file,
        suggestion=_suggest(w.db, personal_file, name),
        user_id=w.alice.id,
        confidence_threshold=0.5,
        apply_tags=False,
    )

    [coll] = _collections_of(w.db, personal_file)
    assert coll.id != org_coll.id
    assert coll.organization_id is None
    assert coll.user_id == w.alice.id


def test_batch_grouping_groups_per_tenant(org_world):
    from app.models.upload_batch import UploadBatch
    from app.services.auto_label_service import AutoLabelService

    w = org_world
    topic = _unique("quarterly review")
    batch = UploadBatch(user_id=w.alice.id, source="upload", file_count=4)
    w.db.add(batch)
    w.db.commit()

    files = [_mk_file(w.db, w.alice, org) for org in (w.org_a.id, w.org_a.id, None, None)]
    for f in files:
        f.upload_batch_id = batch.id
        w.db.add(
            TopicSuggestion(
                media_file_id=f.id,
                user_id=w.alice.id,
                suggested_tags=[{"name": topic, "confidence": 0.9}],
                suggested_collections=[],
                status="pending",
            )
        )
    w.db.commit()

    AutoLabelService(w.db).group_batch_by_topics(batch.id, w.alice.id)

    org_colls = {c.id for f in files[:2] for c in _collections_of(w.db, f)}
    personal_colls = {c.id for f in files[2:] for c in _collections_of(w.db, f)}
    assert len(org_colls) == 1 and len(personal_colls) == 1
    assert org_colls != personal_colls
    [org_coll] = w.db.query(Collection).filter(Collection.id.in_(org_colls)).all()
    assert org_coll.organization_id == w.org_a.id


# --------------------------------------------------------------------------- #
# Chat scope resolves an org collection for every member                       #
# --------------------------------------------------------------------------- #


def test_chat_scope_resolves_a_colleagues_org_collection(org_world):
    from app.api.deps_context import RequestContext
    from app.services.chat.context_resolver import _resolve_collections

    w = org_world
    org_file = _mk_file(w.db, w.alice, w.org_a.id)
    coll = _mk_collection(w.db, _unique("Chat Scope"), w.alice, w.org_a.id)
    w.db.add(CollectionMember(collection_id=coll.id, media_file_id=org_file.id))
    w.db.commit()

    bob_ctx = RequestContext(user=w.bob, org_id=w.org_a.id, org_role="org:member")
    assert _resolve_collections(w.db, bob_ctx, [str(coll.uuid)]) == {str(org_file.uuid)}

    alice_elsewhere = RequestContext(user=w.alice, org_id=w.org_b.id, org_role="org:member")
    assert _resolve_collections(w.db, alice_elsewhere, [str(coll.uuid)]) == set()


# --------------------------------------------------------------------------- #
# Deletion and erasure                                                         #
# --------------------------------------------------------------------------- #


def test_deleting_a_member_keeps_the_orgs_collections_and_drops_personal_ones(org_world):
    from app.api.endpoints.admin import _delete_user_owned_records

    w = org_world
    org_coll = _mk_collection(w.db, _unique("Team"), w.bob, w.org_a.id)
    personal = _mk_collection(w.db, _unique("Bob Private"), w.bob, None)
    org_id, personal_id = org_coll.id, personal.id

    _delete_user_owned_records(w.db, w.bob.id)
    w.db.flush()
    w.db.expire_all()

    survivor = w.db.query(Collection).filter(Collection.id == org_id).one()
    assert survivor.organization_id == w.org_a.id
    assert survivor.user_id is None
    assert w.db.query(Collection).filter(Collection.id == personal_id).first() is None


def test_erasing_an_org_member_keeps_the_orgs_collections_de_attributed(org_world, monkeypatch):
    from app.services import gdpr_erasure_service
    from app.services.gdpr_erasure_service import erase_org_member_data

    monkeypatch.setattr(gdpr_erasure_service, "_erase_speaker_voiceprints", lambda **_kw: 0)

    w = org_world
    org_coll = _mk_collection(w.db, _unique("Kept"), w.bob, w.org_a.id)
    personal = _mk_collection(w.db, _unique("Untouched"), w.bob, None)
    coll_id, personal_id = org_coll.id, personal.id

    summary = erase_org_member_data(w.db, w.bob.id, w.org_a.id)
    assert summary["errors"] == []
    w.db.expire_all()

    survivor = w.db.query(Collection).filter(Collection.id == coll_id).one()
    assert survivor.user_id is None
    assert survivor.organization_id == w.org_a.id
    assert w.db.query(Collection).filter(Collection.id == personal_id).one().user_id == w.bob.id


def test_unattributed_org_collection_stays_usable_by_members(client, org_world):
    w = org_world
    coll = _mk_collection(w.db, _unique("Orphaned"), None, w.org_a.id)

    with _acting_as(w.bob, w.org_a.id):
        listed = _listed(client)
        assert coll.name in listed
        assert listed[coll.name]["user_id"] is None
        assert client.get(f"/api/collections/{coll.uuid}").status_code == 200
    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        assert client.delete(f"/api/collections/{coll.uuid}").status_code == 200


def test_erasing_an_organization_removes_its_collections(org_world, monkeypatch):
    from app.services import gdpr_erasure_service
    from app.services.gdpr_erasure_service import erase_organization

    monkeypatch.setattr(gdpr_erasure_service, "_erase_speaker_voiceprints", lambda **_kw: 0)

    w = org_world
    _mk_collection(w.db, _unique("Doomed"), w.alice, w.org_a.id)
    _mk_collection(w.db, _unique("Doomed orphan"), None, w.org_a.id)
    org_id = w.org_a.id

    summary = erase_organization(w.db, org_id)

    assert summary["errors"] == []
    assert w.db.query(Collection).filter(Collection.organization_id == org_id).count() == 0
