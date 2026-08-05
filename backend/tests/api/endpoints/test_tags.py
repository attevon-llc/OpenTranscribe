"""Tag endpoint tests.

The scoping half of this file is a security regression suite for
``v374_add_tag_user_id``: before that revision ``tag`` had no owner column and
``GET /api/tags/unused`` was ``db.query(Tag).filter(~Tag.id.in_(used_tag_ids))``
with no user filter at all, so every authenticated user could read every
unattached tag name in the deployment. ``GET /api/tags`` leaked the same set
through its ``MediaFile.id IS NULL`` arm.
"""

import uuid

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.sharing import CollectionShare

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _make_tag(db, *, name, user_id):
    tag = Tag(name=name, user_id=user_id, source="manual", normalized_name=name.lower())
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def _make_file(db, user) -> MediaFile:
    media_file = MediaFile(
        filename=f"{_unique('tagtest')}.mp3",
        storage_path=f"tagtest/{uuid.uuid4().hex}",
        file_size=1,
        content_type="audio/mpeg",
        user_id=user.id,
        status="completed",
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _attach(db, media_file, tag):
    db.add(FileTag(media_file_id=media_file.id, tag_id=tag.id, source="manual"))
    db.commit()


def _share_file(db, owner, target_user, media_file):
    """Share ``media_file`` with ``target_user`` through a shared collection."""
    collection = Collection(name=_unique("shared-coll"), user_id=owner.id)
    db.add(collection)
    db.commit()
    db.refresh(collection)

    db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=target_user.id,
            permission="viewer",
        )
    )
    db.commit()
    return collection


def _tag_names(response):
    assert response.status_code == 200, response.text
    return {t["name"] for t in response.json()}


# ---------------------------------------------------------------------------
# Baseline behaviour
# ---------------------------------------------------------------------------


def test_list_tags(client, user_token_headers):
    """Test listing all tags"""
    response = client.get("/api/tags", headers=user_token_headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_list_tags_unauthorized(client):
    """Test that unauthorized users cannot list tags"""
    response = client.get("/api/tags")
    assert response.status_code == 401  # Unauthorized


def test_create_tag(client, user_token_headers, db_session):
    """Test creating a new tag"""
    tag_data = {"name": _unique("test_tag")}
    response = client.post("/api/tags", headers=user_token_headers, json=tag_data)
    assert response.status_code == 200, f"Create tag failed: {response.json()}"
    tag = response.json()
    assert "uuid" in tag or "id" in tag
    assert tag["name"] == tag_data["name"]


def test_create_duplicate_tag(client, user_token_headers, db_session):
    """Test creating a duplicate tag returns existing tag"""
    tag_data = {"name": _unique("duplicate_tag")}

    # First create a tag
    first_response = client.post("/api/tags", headers=user_token_headers, json=tag_data)
    assert first_response.status_code == 200

    # Try to create the same tag again - should return existing tag
    response = client.post("/api/tags", headers=user_token_headers, json=tag_data)
    assert response.status_code == 200
    assert response.json()["uuid"] == first_response.json()["uuid"]


def test_created_tag_is_owned_by_creator(client, user_token_headers, db_session, normal_user):
    """A tag created through the API must not be ownerless (= visible to all)."""
    name = _unique("owned_tag")
    assert client.post("/api/tags", headers=user_token_headers, json={"name": name}).status_code

    tag = db_session.query(Tag).filter(Tag.name == name).one()
    assert tag.user_id == normal_user.id


def test_two_users_can_create_the_same_tag_name(
    client, user_token_headers, other_user_auth_headers, db_session
):
    """Per-user uniqueness: both users get their OWN row, not a shared one."""
    name = _unique("Meeting")

    first = client.post("/api/tags", headers=user_token_headers, json={"name": name})
    second = client.post("/api/tags", headers=other_user_auth_headers, json={"name": name})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["uuid"] != second.json()["uuid"]

    owners = {t.user_id for t in db_session.query(Tag).filter(Tag.name == name).all()}
    assert len(owners) == 2


# ---------------------------------------------------------------------------
# Cross-user disclosure (the bug this suite exists for)
# ---------------------------------------------------------------------------


def test_unused_tags_do_not_leak_across_users(client, user_token_headers, other_user, db_session):
    """User A must not see user B's unattached tag via GET /api/tags/unused."""
    secret = _unique("Project Falcon Layoffs")
    _make_tag(db_session, name=secret, user_id=other_user.id)

    response = client.get("/api/tags/unused", headers=user_token_headers)
    assert secret not in _tag_names(response)


def test_unused_tags_include_own_tags(client, user_token_headers, normal_user, db_session):
    """...but the caller's own unattached tags are still listed."""
    mine = _unique("MyOwnTag")
    _make_tag(db_session, name=mine, user_id=normal_user.id)

    response = client.get("/api/tags/unused", headers=user_token_headers)
    assert mine in _tag_names(response)


def test_list_tags_does_not_leak_other_users_unattached_tags(
    client, user_token_headers, other_user, db_session
):
    """GET /api/tags leaked the same set through its MediaFile.id IS NULL arm."""
    secret = _unique("Client Merger Notes")
    _make_tag(db_session, name=secret, user_id=other_user.id)

    response = client.get("/api/tags", headers=user_token_headers)
    assert secret not in _tag_names(response)


def test_list_tags_does_not_leak_other_users_attached_tags(
    client, user_token_headers, other_user, db_session
):
    """A tag on a file that is NOT shared with the caller stays invisible."""
    private = _unique("PrivateCaseNumber")
    tag = _make_tag(db_session, name=private, user_id=other_user.id)
    their_file = _make_file(db_session, other_user)
    _attach(db_session, their_file, tag)

    response = client.get("/api/tags", headers=user_token_headers)
    assert private not in _tag_names(response)


# ---------------------------------------------------------------------------
# Visibility rule: system tags + sharing
# ---------------------------------------------------------------------------


def test_system_tags_are_visible_to_everyone(
    client, user_token_headers, other_user_auth_headers, db_session
):
    """user_id IS NULL = shared vocabulary; both users see it."""
    system_name = _unique("SystemVocab")
    _make_tag(db_session, name=system_name, user_id=None)

    assert system_name in _tag_names(client.get("/api/tags", headers=user_token_headers))
    assert system_name in _tag_names(client.get("/api/tags", headers=other_user_auth_headers))


def test_seeded_default_tags_are_system_tags(client, user_token_headers, db_session):
    """The seeded picker defaults survive the split as ownerless rows."""
    from app.initial_data import _ensure_default_tags

    _ensure_default_tags(db_session)

    names = _tag_names(client.get("/api/tags", headers=user_token_headers))
    assert {"Important", "Meeting", "Interview", "Personal"} <= names


def test_tag_on_shared_file_is_visible_to_the_recipient(
    client, user_token_headers, normal_user, other_user, db_session
):
    """Sharing works through get_accessible_file_ids_subquery — no parallel rule."""
    shared_tag_name = _unique("SharedProjectTag")
    tag = _make_tag(db_session, name=shared_tag_name, user_id=other_user.id)
    their_file = _make_file(db_session, other_user)
    _attach(db_session, their_file, tag)

    # Not visible before the share...
    assert shared_tag_name not in _tag_names(client.get("/api/tags", headers=user_token_headers))

    _share_file(db_session, other_user, normal_user, their_file)

    # ...and visible after it.
    assert shared_tag_name in _tag_names(client.get("/api/tags", headers=user_token_headers))


def test_unattached_tag_of_a_sharing_user_stays_private(
    client, user_token_headers, normal_user, other_user, db_session
):
    """Sharing one file does not hand over the sharer's whole vocabulary."""
    their_file = _make_file(db_session, other_user)
    _share_file(db_session, other_user, normal_user, their_file)

    unrelated = _unique("UnrelatedPrivateTag")
    _make_tag(db_session, name=unrelated, user_id=other_user.id)

    assert unrelated not in _tag_names(client.get("/api/tags", headers=user_token_headers))
    assert unrelated not in _tag_names(client.get("/api/tags/unused", headers=user_token_headers))


# ---------------------------------------------------------------------------
# Mutation paths
# ---------------------------------------------------------------------------


def test_add_tag_to_file_creates_an_owned_tag(client, user_token_headers, normal_user, db_session):
    """The file-tag endpoint must attribute the tag, not leave it ownerless."""
    media_file = _make_file(db_session, normal_user)
    name = _unique("AttachedTag")

    response = client.post(
        f"/api/tags/files/{media_file.uuid}/tags",
        headers=user_token_headers,
        json={"name": name},
    )
    assert response.status_code == 200, response.text

    tag = db_session.query(Tag).filter(Tag.name == name).one()
    assert tag.user_id == normal_user.id


def test_remove_tag_resolves_through_the_file_not_the_name(
    client, user_token_headers, normal_user, other_user, db_session
):
    """Two users own 'Budget'; deleting mine must not touch theirs."""
    name = _unique("Budget")
    mine = _make_tag(db_session, name=name, user_id=normal_user.id)
    theirs = _make_tag(db_session, name=name, user_id=other_user.id)

    my_file = _make_file(db_session, normal_user)
    their_file = _make_file(db_session, other_user)
    _attach(db_session, my_file, mine)
    _attach(db_session, their_file, theirs)

    response = client.delete(
        f"/api/tags/files/{my_file.uuid}/tags/{name}", headers=user_token_headers
    )
    assert response.status_code == 204

    assert (
        db_session.query(FileTag)
        .filter(FileTag.media_file_id == my_file.id, FileTag.tag_id == mine.id)
        .count()
        == 0
    )
    assert (
        db_session.query(FileTag)
        .filter(FileTag.media_file_id == their_file.id, FileTag.tag_id == theirs.id)
        .count()
        == 1
    )


def test_cleanup_unused_tags_requires_admin(client, user_token_headers):
    response = client.delete("/api/tags/cleanup", headers=user_token_headers)
    assert response.status_code == 403
