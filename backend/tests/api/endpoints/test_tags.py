"""Tag endpoint tests.

The creation endpoints resolve names through ``app/services/tag_service.py``:
normalized-exact matching, a 50-character clamp, and rejection of names that are
empty once normalized. A near match is deliberately NOT resolved here — the name
came from a person, so a fuzzy hit may only be offered as a suggestion.

The scoping half of this file is a security regression suite for
``v374_add_tag_user_id``: before that revision ``tag`` had no owner column and
``GET /api/tags/unused`` was ``db.query(Tag).filter(~Tag.id.in_(used_tag_ids))``
with no user filter at all, so every authenticated user could read every
unattached tag name in the deployment. ``GET /api/tags`` leaked the same set
through its ``MediaFile.id IS NULL`` arm.
"""

import uuid

import pytest
from fastapi import status

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


def test_create_tag_case_insensitive_returns_existing(client, user_token_headers, db_session):
    """A name differing only by case resolves to the existing tag."""
    name = f"Interview-{uuid.uuid4().hex[:8]}"

    first = client.post("/api/tags", headers=user_token_headers, json={"name": name})
    assert first.status_code == status.HTTP_200_OK

    second = client.post("/api/tags", headers=user_token_headers, json={"name": name.upper()})
    assert second.status_code == status.HTTP_200_OK
    assert second.json()["uuid"] == first.json()["uuid"]
    assert second.json()["name"] == name


def test_create_tag_separator_variants_return_existing(client, user_token_headers, db_session):
    """Hyphen / underscore / whitespace variants collapse onto the same tag."""
    base = f"quarterly{uuid.uuid4().hex[:8]}"

    first = client.post("/api/tags", headers=user_token_headers, json={"name": f"{base}-notes"})
    assert first.status_code == status.HTTP_200_OK

    for variant in (f"{base}_notes", f"{base} notes", f"  {base}   notes "):
        response = client.post("/api/tags", headers=user_token_headers, json={"name": variant})
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["uuid"] == first.json()["uuid"]


def test_create_tag_near_match_is_not_auto_resolved(client, user_token_headers, db_session):
    """A near match is a distinct tag on the manual path — fuzzy only ever suggests."""
    suffix = uuid.uuid4().hex[:8]

    q3 = client.post(
        "/api/tags", headers=user_token_headers, json={"name": f"q3-earnings-{suffix}"}
    )
    assert q3.status_code == status.HTTP_200_OK

    q4 = client.post(
        "/api/tags", headers=user_token_headers, json={"name": f"q4-earnings-{suffix}"}
    )
    assert q4.status_code == status.HTTP_200_OK
    assert q4.json()["uuid"] != q3.json()["uuid"]
    assert q4.json()["name"] == f"q4-earnings-{suffix}"


def test_create_tag_truncates_long_name(client, user_token_headers, db_session):
    """An over-long name is clamped to the stored width instead of erroring."""
    long_name = f"retro-{uuid.uuid4().hex[:8]}-" + ("x" * 80)

    response = client.post("/api/tags", headers=user_token_headers, json={"name": long_name})

    assert response.status_code == status.HTTP_200_OK, response.json()
    assert response.json()["name"] == long_name[:50]


def test_create_tag_rejects_blank_name(client, user_token_headers, db_session):
    """A name that is empty after normalization is rejected, not stored blank."""
    for blank in ("", "   ", "-", "__"):
        response = client.post("/api/tags", headers=user_token_headers, json={"name": blank})
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, blank


# ---------------------------------------------------------------------------
# Rename / merge / delete
#
# The service-layer behavior is covered in tests/unit/test_tag_operations.py;
# these assert only the wiring — UUID path params, the confirmation round trip,
# and the impact shape that reaches the client.
# ---------------------------------------------------------------------------


def _create_tag(client, headers, name: str) -> dict:
    response = client.post("/api/tags", headers=headers, json={"name": name})
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def test_rename_tag_endpoint_applies_a_plain_rename(client, user_token_headers, db_session):
    tag = _create_tag(client, user_token_headers, f"before-{uuid.uuid4().hex[:8]}")
    new_name = f"after-{uuid.uuid4().hex[:8]}"

    response = client.patch(
        f"/api/tags/{tag['uuid']}", headers=user_token_headers, json={"name": new_name}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["merged"] is False
    assert body["requires_confirmation"] is False
    assert body["tag"]["name"] == new_name
    assert body["tag"]["uuid"] == tag["uuid"]


def test_rename_onto_existing_tag_asks_for_confirmation_then_merges(
    client, user_token_headers, db_session
):
    """R8: the collision comes back as an impact preview, not a silent merge."""
    suffix = uuid.uuid4().hex[:8]
    target = _create_tag(client, user_token_headers, f"Interview-{suffix}")
    tag = _create_tag(client, user_token_headers, f"interviewing-{suffix}")

    held = client.patch(
        f"/api/tags/{tag['uuid']}", headers=user_token_headers, json={"name": target["name"]}
    )
    assert held.status_code == status.HTTP_200_OK, held.text
    assert held.json()["requires_confirmation"] is True
    assert held.json()["merged"] is False
    assert "accessible_file_count" in held.json()["impact"]
    assert "total_file_count" in held.json()["impact"]

    confirmed = client.patch(
        f"/api/tags/{tag['uuid']}",
        headers=user_token_headers,
        json={"name": target["name"], "confirm_merge": True},
    )
    assert confirmed.status_code == status.HTTP_200_OK, confirmed.text
    assert confirmed.json()["merged"] is True
    assert confirmed.json()["tag"]["uuid"] == target["uuid"]
    assert tag["uuid"] in confirmed.json()["deleted_uuids"]


def test_merge_endpoint_folds_sources_into_the_path_tag(client, user_token_headers, db_session):
    suffix = uuid.uuid4().hex[:8]
    survivor = _create_tag(client, user_token_headers, f"survivor-{suffix}")
    first = _create_tag(client, user_token_headers, f"dupe-a-{suffix}")
    second = _create_tag(client, user_token_headers, f"dupe-b-{suffix}")

    response = client.post(
        f"/api/tags/{survivor['uuid']}/merge",
        headers=user_token_headers,
        json={"source_uuids": [first["uuid"], second["uuid"]]},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["merged"] is True
    assert sorted(body["deleted_uuids"]) == sorted([first["uuid"], second["uuid"]])


def test_merge_endpoint_404s_on_an_unknown_tag(client, user_token_headers, db_session):
    survivor = _create_tag(client, user_token_headers, f"survivor-{uuid.uuid4().hex[:8]}")

    response = client.post(
        f"/api/tags/{survivor['uuid']}/merge",
        headers=user_token_headers,
        json={"source_uuids": [str(uuid.uuid4())]},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_delete_endpoint_removes_the_tags_and_reports_impact(
    client, user_token_headers, db_session
):
    suffix = uuid.uuid4().hex[:8]
    first = _create_tag(client, user_token_headers, f"gone-a-{suffix}")
    second = _create_tag(client, user_token_headers, f"gone-b-{suffix}")

    response = client.request(
        "DELETE",
        "/api/tags",
        headers=user_token_headers,
        params={"tag_uuids": [first["uuid"], second["uuid"]]},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert sorted(body["deleted_uuids"]) == sorted([first["uuid"], second["uuid"]])
    assert body["impact"]["total_file_count"] == 0

    listed = client.get("/api/tags", headers=user_token_headers)
    names = {tag["name"] for tag in listed.json()}
    assert first["name"] not in names
    assert second["name"] not in names


def test_impact_endpoint_reports_accessible_and_global_counts(
    client, user_token_headers, db_session
):
    """The preview cannot understate the operation — both counts are surfaced."""
    tag = _create_tag(client, user_token_headers, f"impact-{uuid.uuid4().hex[:8]}")

    response = client.get(
        "/api/tags/impact", headers=user_token_headers, params={"tag_uuids": [tag["uuid"]]}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["accessible_file_count"] == 0
    assert body["total_file_count"] == 0
    assert body["tags"][0]["uuid"] == tag["uuid"]
    assert body["tags"][0]["name"] == tag["name"]


def test_tag_mutation_endpoints_require_authentication(client, db_session):
    tag_uuid = str(uuid.uuid4())
    assert client.patch(f"/api/tags/{tag_uuid}", json={"name": "x"}).status_code == 401
    assert (
        client.post(f"/api/tags/{tag_uuid}/merge", json={"source_uuids": [tag_uuid]}).status_code
        == 401
    )
    assert client.get("/api/tags/impact", params={"tag_uuids": [tag_uuid]}).status_code == 401
    assert client.post("/api/tags/accept", json={"tag_uuids": [tag_uuid]}).status_code == 401
    assert client.post("/api/tags/reject", json={"tag_uuids": [tag_uuid]}).status_code == 401
    assert (
        client.get(
            "/api/tags/review-impact", params={"tag_uuids": [tag_uuid], "action": "reject"}
        ).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# Accept / reject (R7, R15)
#
# Service behavior lives in tests/unit/test_tag_review.py; these assert the
# wiring — UUID path/query params, the per-tag outcome list, and the removed /
# retained split reaching the client.
# ---------------------------------------------------------------------------


def _auto_tag(db_session, name: str, user_id: int):
    """Create a tag the auto-labeler produced — the API has no path that writes one.

    ``user_id`` is required and is the **file owner**, matching
    ``auto_apply_suggestions``: the auto-labeler runs unattended, so it
    attributes its tags to the owner of the file it labeled rather than leaving
    them ownerless. An ownerless row is a *system* tag, published to every
    account and mutable only by an admin, which is not what this fixture means.
    """
    from app.core.constants import TAG_SOURCE_AUTO_AI
    from app.models.media import Tag
    from app.services.tag_service import normalize_tag_name

    tag = Tag(
        name=name,
        user_id=user_id,
        source=TAG_SOURCE_AUTO_AI,
        normalized_name=normalize_tag_name(name),
    )
    db_session.add(tag)
    db_session.commit()
    return tag


def test_accept_endpoint_marks_the_tag_accepted(
    client, user_token_headers, db_session, normal_user
):
    tag = _auto_tag(db_session, f"accept-me-{uuid.uuid4().hex[:8]}", normal_user.id)

    response = client.post(
        "/api/tags/accept", headers=user_token_headers, json={"tag_uuids": [str(tag.uuid)]}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["applied"] is True
    assert body["tags"][0]["uuid"] == str(tag.uuid)
    assert body["tags"][0]["outcome"] == "accepted"
    db_session.refresh(tag)
    assert tag.source == "ai_accepted"


def test_accept_endpoint_reports_ineligible_tags_without_failing(
    client, user_token_headers, db_session, normal_user
):
    """A mixed multi-select must not 4xx the whole call."""
    suffix = uuid.uuid4().hex[:8]
    auto = _auto_tag(db_session, f"mix-auto-{suffix}", normal_user.id)
    manual = _create_tag(client, user_token_headers, f"mix-manual-{suffix}")

    response = client.post(
        "/api/tags/accept",
        headers=user_token_headers,
        json={"tag_uuids": [str(auto.uuid), manual["uuid"]]},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    outcomes = {entry["uuid"]: entry["outcome"] for entry in response.json()["tags"]}
    assert outcomes[str(auto.uuid)] == "accepted"
    assert outcomes[manual["uuid"]] == "not_applicable"


def _file_for(db_session, owner):
    from app.models.media import MediaFile

    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="tag_review_api.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()
    return media_file


def test_review_impact_endpoint_reports_the_reject_split(
    client, user_token_headers, db_session, normal_user
):
    from app.core.constants import TAG_SOURCE_AUTO_AI
    from app.core.constants import TAG_SOURCE_MANUAL
    from app.models.media import FileTag

    tag = _auto_tag(db_session, f"split-{uuid.uuid4().hex[:8]}", normal_user.id)
    auto_file = _file_for(db_session, normal_user)
    manual_file = _file_for(db_session, normal_user)
    db_session.add(FileTag(media_file_id=auto_file.id, tag_id=tag.id, source=TAG_SOURCE_AUTO_AI))
    db_session.add(FileTag(media_file_id=manual_file.id, tag_id=tag.id, source=TAG_SOURCE_MANUAL))
    db_session.commit()

    response = client.get(
        "/api/tags/review-impact",
        headers=user_token_headers,
        params={"tag_uuids": [str(tag.uuid)], "action": "reject"},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["applied"] is False
    assert body["removed_association_count"] == 1
    assert body["retained_association_count"] == 1
    assert body["tags"][0]["tag_removed"] is False


def test_reject_endpoint_removes_the_tag_when_nothing_human_remains(
    client, user_token_headers, db_session, normal_user
):
    from app.models.media import Tag

    tag = _auto_tag(db_session, f"reject-me-{uuid.uuid4().hex[:8]}", normal_user.id)

    response = client.post(
        "/api/tags/reject", headers=user_token_headers, json={"tag_uuids": [str(tag.uuid)]}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    body = response.json()
    assert body["tags"][0]["outcome"] == "rejected"
    assert body["deleted_uuids"] == [str(tag.uuid)]
    assert db_session.query(Tag).filter(Tag.name == tag.name).first() is None


def test_review_endpoints_404_on_an_unknown_tag(client, user_token_headers, db_session):
    unknown = str(uuid.uuid4())

    for path in ("/api/tags/accept", "/api/tags/reject"):
        response = client.post(path, headers=user_token_headers, json={"tag_uuids": [unknown]})
        assert response.status_code == status.HTTP_404_NOT_FOUND, path


# ---------------------------------------------------------------------------
# Collisions and listing filters (R3, R11, R14)
#
# Clustering, suggestion ranking, and survivor preselection are covered in
# tests/unit/test_tag_collisions.py; these assert the wiring — the query
# parameters, the cluster shape on the wire, and that a broken read errors
# instead of rendering an empty page.
# ---------------------------------------------------------------------------


def _raw_tag(db_session, name: str, *, normalized=..., source="manual"):
    """Insert a tag row directly — the API has no path that writes a collision."""
    from app.models.media import Tag
    from app.services.tag_service import normalize_tag_name

    stored = normalize_tag_name(name) if normalized is ... else normalized
    tag = Tag(name=name, source=source, normalized_name=stored)
    db_session.add(tag)
    db_session.commit()
    return tag


def test_unused_listing_agrees_with_the_usage_count(
    client, user_token_headers, db_session, normal_user, other_user
):
    """A tag whose only files are inaccessible reads as 0 *and* as unused.

    ``/tags`` scoped ``usage_count`` to the caller's accessible files while
    ``/tags/unused`` counted usage globally, so this tag reported ``0`` in one
    place and was missing from the other.
    """
    from app.models.media import FileTag

    tag = _create_tag(client, user_token_headers, f"foreign-{uuid.uuid4().hex[:8]}")
    hidden_file = _file_for(db_session, other_user)
    tag_row = _raw_tag_row(db_session, tag["uuid"])
    db_session.add(FileTag(media_file_id=hidden_file.id, tag_id=tag_row.id, source="manual"))
    db_session.commit()

    listed = client.get("/api/tags", headers=user_token_headers)
    assert listed.status_code == status.HTTP_200_OK, listed.text
    counts = {entry["uuid"]: entry["usage_count"] for entry in listed.json()}
    assert counts[tag["uuid"]] == 0

    unused = client.get("/api/tags/unused", headers=user_token_headers)
    assert unused.status_code == status.HTTP_200_OK, unused.text
    assert tag["uuid"] in {entry["uuid"] for entry in unused.json()}

    filtered = client.get("/api/tags", headers=user_token_headers, params={"unused": True})
    assert filtered.status_code == status.HTTP_200_OK, filtered.text
    assert tag["uuid"] in {entry["uuid"] for entry in filtered.json()}


def _raw_tag_row(db_session, tag_uuid: str):
    from app.models.media import Tag

    return db_session.query(Tag).filter(Tag.uuid == tag_uuid).one()


def test_unused_filter_drops_a_tag_the_caller_uses(
    client, user_token_headers, db_session, normal_user
):
    """The filter narrows — a tag on the caller's own file must not come back."""
    from app.models.media import FileTag

    tag = _create_tag(client, user_token_headers, f"inuse-{uuid.uuid4().hex[:8]}")
    media_file = _file_for(db_session, normal_user)
    tag_row = _raw_tag_row(db_session, tag["uuid"])
    db_session.add(FileTag(media_file_id=media_file.id, tag_id=tag_row.id, source="manual"))
    db_session.commit()

    response = client.get("/api/tags", headers=user_token_headers, params={"unused": True})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert tag["uuid"] not in {entry["uuid"] for entry in response.json()}


def test_awaiting_review_filter_returns_only_auto_labeled_tags(
    client, user_token_headers, db_session, normal_user
):
    suffix = uuid.uuid4().hex[:8]
    auto = _auto_tag(db_session, f"await-auto-{suffix}", normal_user.id)
    manual = _create_tag(client, user_token_headers, f"await-manual-{suffix}")
    accepted = _raw_tag(db_session, f"await-accepted-{suffix}", source="ai_accepted")
    legacy = _raw_tag(db_session, f"await-legacy-{suffix}", source=None)

    response = client.get("/api/tags", headers=user_token_headers, params={"awaiting_review": True})

    assert response.status_code == status.HTTP_200_OK, response.text
    returned = {entry["uuid"] for entry in response.json()}
    assert str(auto.uuid) in returned
    assert manual["uuid"] not in returned
    assert str(accepted.uuid) not in returned
    assert str(legacy.uuid) not in returned


def test_collisions_endpoint_ships_clusters_survivor_and_suggestions(
    client, user_token_headers, db_session
):
    """Grouping, ranking, and preselection arrive pre-computed."""
    suffix = uuid.uuid4().hex[:8]
    first = _raw_tag(db_session, f"q3-earnings-{suffix}")
    second = _raw_tag(db_session, f"Q3 Earnings {suffix}")
    near = _raw_tag(db_session, f"q4-earnings-{suffix}")
    normalized = f"q3 earnings {suffix}"

    response = client.get("/api/tags/collisions", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text
    clusters = [c for c in response.json() if c["normalized_name"] == normalized]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert {member["uuid"] for member in cluster["members"]} == {
        str(first.uuid),
        str(second.uuid),
    }
    assert cluster["suggested_survivor_uuid"] in {str(first.uuid), str(second.uuid)}
    assert sum(member["suggested_survivor"] for member in cluster["members"]) == 1
    assert str(near.uuid) in {s["uuid"] for s in cluster["suggestions"]}

    repeat = client.get("/api/tags/collisions", headers=user_token_headers)
    assert repeat.json() == response.json()


def test_colliding_filter_narrows_the_tag_list(client, user_token_headers, db_session):
    suffix = uuid.uuid4().hex[:8]
    first = _raw_tag(db_session, f"clash-{suffix}")
    second = _raw_tag(db_session, f"CLASH-{suffix}")
    alone = _create_tag(client, user_token_headers, f"solo-{suffix}")

    response = client.get("/api/tags", headers=user_token_headers, params={"colliding": True})

    assert response.status_code == status.HTTP_200_OK, response.text
    returned = {entry["uuid"] for entry in response.json()}
    assert {str(first.uuid), str(second.uuid)} <= returned
    assert alone["uuid"] not in returned


def _break_permission_scoping(monkeypatch):
    """Make the accessible-files gate raise, the way a query bug would."""
    from app.services.permission_service import PermissionService

    def _boom(*args, **kwargs):
        raise RuntimeError("accessible-file subquery is broken")

    monkeypatch.setattr(PermissionService, "get_accessible_file_ids_subquery", staticmethod(_boom))


def test_list_tags_surfaces_a_query_error(client, user_token_headers, db_session, monkeypatch):
    """A broken read must error, not render an empty tag page.

    The request carries a filter so it bypasses the read-through cache — a
    cached hit would prove nothing about the query underneath.
    """
    _break_permission_scoping(monkeypatch)

    with pytest.raises(RuntimeError):
        client.get("/api/tags", headers=user_token_headers, params={"unused": True})


def test_list_unused_tags_surfaces_a_query_error(
    client, user_token_headers, db_session, monkeypatch
):
    """Same for the unused listing — an empty list is not an error report."""
    _break_permission_scoping(monkeypatch)

    with pytest.raises(RuntimeError):
        client.get("/api/tags/unused", headers=user_token_headers)


def test_collision_endpoints_require_authentication(client, db_session):
    assert client.get("/api/tags/collisions").status_code == 401
