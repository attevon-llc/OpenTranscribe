"""Tags belong to a tenant, not to a user (issue #1050).

The owner decision these tests pin: **tags are shared within an organization**.

* Two members of one organization see, and resolve onto, the same tag row —
  neither coins a private duplicate of the other's word.
* A tag created in tenant A is never listed in tenant B (or in the user's
  personal workspace) for the same user.
* AI auto-labeling creates its tags in the **file's** tenant, never in the file
  owner's personal vocabulary and never reusing a same-named personal row.

Requests run as a specific user in a specific tenant by overriding the two auth
dependencies — the same shape ``tests/test_tenant_isolation.py`` uses — so the
org context is exactly the one the test names rather than whatever a token
happens to carry.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager

import pytest

from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
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
        filename=f"tenancy_{str(fuuid)[:8]}.mp3",
        storage_path=f"tenancy/{fuuid}.mp3",
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


@contextmanager
def _acting_as(user: User, org_id: int | None, org_role: str | None = None):
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


def _listed_names(client) -> set[str]:
    response = client.get("/api/tags")
    assert response.status_code == 200, response.text
    return {row["name"] for row in response.json()}


@pytest.fixture()
def org_world(db_session):
    """One organization with two members, plus a second org the first member also joins."""
    db = db_session
    org_a = _mk_org(db, "A")
    org_b = _mk_org(db, "B")
    alice = _mk_user(db, "alice")
    bob = _mk_user(db, "bob")
    _join(db, org_a, alice, role="org:admin")
    _join(db, org_a, bob)
    _join(db, org_b, alice)
    return type("World", (), {"db": db, "org_a": org_a, "org_b": org_b, "alice": alice, "bob": bob})


# --------------------------------------------------------------------------- #
# Two members of one organization share one vocabulary                        #
# --------------------------------------------------------------------------- #


def test_org_members_see_and_reuse_the_same_tag(client, org_world):
    w = org_world
    name = _unique("Board Q3")
    bob_file = _mk_file(w.db, w.bob, w.org_a.id)

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        created = client.post("/api/tags", json={"name": name})
        assert created.status_code == 200, created.text

    with _acting_as(w.bob, w.org_a.id):
        assert name in _listed_names(client), "a member must see the org's tag"
        attached = client.post(f"/api/tags/files/{bob_file.uuid}/tags", json={"name": name})
        assert attached.status_code == 200, attached.text

    # Bob resolved onto Alice's row rather than coining his own.
    assert attached.json()["uuid"] == created.json()["uuid"]
    rows = w.db.query(Tag).filter(Tag.name == name).all()
    assert len(rows) == 1
    assert rows[0].organization_id == w.org_a.id


def test_org_member_bulk_add_reuses_the_org_tag(client, org_world):
    w = org_world
    name = _unique("Pipeline")
    bob_file = _mk_file(w.db, w.bob, w.org_a.id)

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        assert client.post("/api/tags", json={"name": name}).status_code == 200

    with _acting_as(w.bob, w.org_a.id):
        response = client.post(
            "/api/files/management/bulk-action",
            json={"file_uuids": [str(bob_file.uuid)], "action": "add_tag", "tag_name": name},
        )
        assert response.status_code == 200, response.text

    rows = w.db.query(Tag).filter(Tag.name == name).all()
    assert len(rows) == 1
    assert (
        w.db.query(FileTag).filter(FileTag.media_file_id == bob_file.id).one().tag_id == rows[0].id
    )


# --------------------------------------------------------------------------- #
# A tag never crosses a tenant boundary for the same user                      #
# --------------------------------------------------------------------------- #


def test_tag_created_in_one_org_is_not_listed_in_another_or_personal(client, org_world):
    w = org_world
    name = _unique("Falcon Layoffs")

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        assert client.post("/api/tags", json={"name": name}).status_code == 200
        assert name in _listed_names(client)

    with _acting_as(w.alice, w.org_b.id):
        assert name not in _listed_names(client)
        unused = client.get("/api/tags/unused")
        assert unused.status_code == 200
        assert name not in {row["name"] for row in unused.json()}

    with _acting_as(w.alice, None):
        assert name not in _listed_names(client)


def test_personal_tag_is_not_listed_in_an_org(client, org_world):
    w = org_world
    name = _unique("Private Diary")

    with _acting_as(w.alice, None):
        assert client.post("/api/tags", json={"name": name}).status_code == 200
        assert name in _listed_names(client)

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        assert name not in _listed_names(client)


def test_same_name_in_two_tenants_is_two_rows(client, org_world):
    """Typing an existing personal word inside an org creates the org's own row."""
    w = org_world
    name = _unique("Interview")
    org_file = _mk_file(w.db, w.alice, w.org_a.id)

    with _acting_as(w.alice, None):
        personal = client.post("/api/tags", json={"name": name}).json()

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        attached = client.post(f"/api/tags/files/{org_file.uuid}/tags", json={"name": name})
        assert attached.status_code == 200, attached.text

    assert attached.json()["uuid"] != personal["uuid"]
    org_tag = w.db.query(Tag).filter(Tag.uuid == uuid_pkg.UUID(attached.json()["uuid"])).one()
    assert org_tag.organization_id == w.org_a.id


def test_tag_from_another_tenant_cannot_be_mutated(client, org_world):
    w = org_world
    name = _unique("Secret")

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        created = client.post("/api/tags", json={"name": name}).json()

    with _acting_as(w.alice, w.org_b.id):
        response = client.patch(f"/api/tags/{created['uuid']}", json={"name": name + " renamed"})
        assert response.status_code == 404


def test_member_cannot_rename_a_colleagues_org_tag_but_org_admin_can(client, org_world):
    w = org_world
    name = _unique("Roadmap")

    with _acting_as(w.bob, w.org_a.id):
        created = client.post("/api/tags", json={"name": name}).json()

    carol = _mk_user(w.db, "carol")
    _join(w.db, w.org_a, carol)
    with _acting_as(carol, w.org_a.id, "org:member"):
        denied = client.patch(f"/api/tags/{created['uuid']}", json={"name": name + " x"})
        assert denied.status_code == 404

    with _acting_as(w.alice, w.org_a.id, "org:admin"):
        allowed = client.patch(f"/api/tags/{created['uuid']}", json={"name": name + " y"})
        assert allowed.status_code == 200, allowed.text


# --------------------------------------------------------------------------- #
# AI auto-labeling lands in the file's tenant                                  #
# --------------------------------------------------------------------------- #


def _suggest(db, media_file: MediaFile, name: str) -> TopicSuggestion:
    suggestion = TopicSuggestion(
        media_file_id=media_file.id,
        user_id=media_file.user_id,
        suggested_tags=[{"name": name, "confidence": 0.99}],
        suggested_collections=[],
        status="pending",
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


def test_auto_label_tags_land_in_the_files_tenant(org_world):
    from app.services.auto_label_service import AutoLabelService

    w = org_world
    name = _unique("Acquisition Talks")
    # The owner already has this word privately; the org file must NOT reuse it.
    personal = Tag(name=name, user_id=w.alice.id, normalized_name=name.lower(), source="manual")
    w.db.add(personal)
    w.db.commit()

    org_file = _mk_file(w.db, w.alice, w.org_a.id)
    AutoLabelService(w.db).auto_apply_suggestions(
        media_file=org_file,
        suggestion=_suggest(w.db, org_file, name),
        user_id=w.alice.id,
        confidence_threshold=0.5,
        apply_collections=False,
    )

    link = w.db.query(FileTag).filter(FileTag.media_file_id == org_file.id).one()
    tag = w.db.query(Tag).filter(Tag.id == link.tag_id).one()
    assert tag.id != personal.id
    assert tag.organization_id == w.org_a.id

    # A colleague's org file with the same suggestion converges on that same row.
    bob_file = _mk_file(w.db, w.bob, w.org_a.id)
    AutoLabelService(w.db).auto_apply_suggestions(
        media_file=bob_file,
        suggestion=_suggest(w.db, bob_file, name),
        user_id=w.bob.id,
        confidence_threshold=0.5,
        apply_collections=False,
    )
    bob_link = w.db.query(FileTag).filter(FileTag.media_file_id == bob_file.id).one()
    assert bob_link.tag_id == tag.id


def test_auto_label_on_a_personal_file_stays_personal(org_world):
    from app.services.auto_label_service import AutoLabelService

    w = org_world
    name = _unique("Gardening")
    personal_file = _mk_file(w.db, w.alice, None)
    AutoLabelService(w.db).auto_apply_suggestions(
        media_file=personal_file,
        suggestion=_suggest(w.db, personal_file, name),
        user_id=w.alice.id,
        confidence_threshold=0.5,
        apply_collections=False,
    )
    link = w.db.query(FileTag).filter(FileTag.media_file_id == personal_file.id).one()
    tag = w.db.query(Tag).filter(Tag.id == link.tag_id).one()
    assert tag.organization_id is None
    assert tag.user_id == w.alice.id


def test_upload_import_tags_land_in_the_files_tenant(org_world):
    """The batched importer (upload / URL / watch sources) resolves in the file's tenant."""
    from app.api.endpoints.files.prepare_upload import add_tags_to_file

    w = org_world
    name = _unique("Imported")
    org_file = _mk_file(w.db, w.alice, w.org_a.id)
    add_tags_to_file(w.db, org_file.id, [name], w.alice.id)
    w.db.commit()

    link = w.db.query(FileTag).filter(FileTag.media_file_id == org_file.id).one()
    assert w.db.query(Tag).filter(Tag.id == link.tag_id).one().organization_id == w.org_a.id


# --------------------------------------------------------------------------- #
# Tenant boundary on the destructive paths                                     #
# --------------------------------------------------------------------------- #


def test_merge_refuses_to_fold_a_tag_across_tenants(org_world):
    """A system tag folded into an org tag would attach that org's row to every tenant's files."""
    from app.services.tag_operations import TagTenantMismatchError
    from app.services.tag_operations import merge_tags

    w = org_world
    org_tag = Tag(name=_unique("Org Word"), user_id=w.alice.id, organization_id=w.org_a.id)
    system = Tag(name=_unique("System Word"), user_id=None, organization_id=None)
    personal = Tag(name=_unique("Mine"), user_id=w.alice.id, organization_id=None)
    w.db.add_all([org_tag, system, personal])
    w.db.commit()

    org_tag_id, system_id, personal_id = org_tag.id, system.id, personal.id

    for doomed_id in (system_id, personal_id):
        with pytest.raises(TagTenantMismatchError):
            merge_tags(
                w.db, org_tag_id, [doomed_id], user_id=w.alice.id, organization_id=w.org_a.id
            )

    assert w.db.query(Tag).filter(Tag.id.in_([system_id, personal_id])).count() == 2


def test_erasing_an_organization_removes_its_tags(org_world, monkeypatch):
    """tag.organization_id is a plain FK: the org row cannot go while its tags remain."""
    from app.services import gdpr_erasure_service
    from app.services.gdpr_erasure_service import erase_organization

    # The voiceprint index is OpenSearch; this test is about the SQL plane only.
    monkeypatch.setattr(gdpr_erasure_service, "_erase_speaker_voiceprints", lambda **_kw: 0)

    w = org_world
    name = _unique("Doomed")
    w.db.add(Tag(name=name, user_id=w.alice.id, organization_id=w.org_a.id))
    w.db.commit()
    org_id = w.org_a.id

    summary = erase_organization(w.db, org_id)

    assert summary["errors"] == []
    assert w.db.query(Tag).filter(Tag.organization_id == org_id).count() == 0
    assert w.db.query(Organization).filter(Organization.id == org_id).first() is None


def test_deleting_a_member_keeps_the_orgs_tags_and_drops_their_personal_ones(org_world):
    from app.api.endpoints.admin import _delete_user_owned_records

    w = org_world
    org_tag = Tag(name=_unique("Shared Word"), user_id=w.bob.id, organization_id=w.org_a.id)
    personal = Tag(name=_unique("Bob Private"), user_id=w.bob.id, organization_id=None)
    w.db.add_all([org_tag, personal])
    w.db.commit()
    org_tag_id, personal_id = org_tag.id, personal.id

    _delete_user_owned_records(w.db, w.bob.id)
    w.db.flush()
    w.db.expire_all()

    survivor = w.db.query(Tag).filter(Tag.id == org_tag_id).one()
    assert survivor.organization_id == w.org_a.id
    assert survivor.user_id is None
    assert w.db.query(Tag).filter(Tag.id == personal_id).first() is None


def test_unattributed_org_tag_is_not_system_vocabulary(client, org_world):
    """An org tag whose creator is gone keeps its tenant — it must not leak as a system tag."""
    w = org_world
    name = _unique("Orphaned Org Word")
    w.db.add(Tag(name=name, user_id=None, organization_id=w.org_a.id))
    w.db.commit()

    with _acting_as(w.alice, w.org_b.id):
        assert name not in _listed_names(client)
    with _acting_as(w.alice, None):
        assert name not in _listed_names(client)
    with _acting_as(w.bob, w.org_a.id):
        rows = {r["name"]: r for r in client.get("/api/tags").json()}
        assert rows[name]["ownership"] == "shared_with_me"
