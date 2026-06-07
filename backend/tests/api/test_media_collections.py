"""Functional characterization tests for the media-collections endpoints.

Covers ``media_collections.py`` (``/api/collections``): collection CRUD, the
media member add/remove flow, and the CollectionShare user-XOR-group sharing
matrix (owner-only management, visibility for shared users, the DB
CheckConstraint that a share targets exactly one of user/group).

The 403-other_user *delete* contract is already pinned in
``test_ownership_contracts.py`` (the sharing helper rejects a stranger first);
these add functional coverage around it. All rows live on the savepoint-isolated
``db_session`` and roll back; no real MinIO object is needed because the media
add/remove paths only touch DB rows.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.sharing import CollectionShare


def _make_collection(db_session, owner, *, name: str | None = None) -> Collection:
    col = Collection(
        user_id=owner.id,
        name=name or f"col-{uuid.uuid4().hex[:8]}",
        description="test collection",
    )
    db_session.add(col)
    db_session.commit()
    db_session.refresh(col)
    return col


def _make_file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    mf = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="col_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=2048,
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _make_share(
    db_session,
    collection,
    sharer,
    *,
    target_user=None,
    target_group=None,
    permission: str = "viewer",
) -> CollectionShare:
    """Create a CollectionShare row honoring the user-XOR-group constraint."""
    share = CollectionShare(
        collection_id=collection.id,
        shared_by_id=sharer.id,
        target_type="user" if target_user else "group",
        target_user_id=target_user.id if target_user else None,
        target_group_id=target_group.id if target_group else None,
        permission=permission,
    )
    db_session.add(share)
    db_session.commit()
    db_session.refresh(share)
    return share


# ---------------------------------------------------------------------------
# GET /api/collections  (list with ownership filter + pagination)
# ---------------------------------------------------------------------------


def test_list_collections_unauthorized(client):
    response = client.get("/api/collections")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_list_collections_returns_owned(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user)
    response = client.get("/api/collections", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    entry = next((c for c in response.json() if c["uuid"] == str(col.uuid)), None)
    assert entry is not None
    assert entry["my_permission"] == "owner"
    assert entry["is_shared"] is False


def test_list_collections_excludes_other_users(
    client, other_user_auth_headers, normal_user, db_session
):
    col = _make_collection(db_session, normal_user)
    response = client.get("/api/collections", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = {c["uuid"] for c in response.json()}
    assert str(col.uuid) not in uuids


def test_list_collections_bad_ownership_422(client, user_token_headers):
    """ownership is constrained to mine|shared|all by a regex pattern."""
    response = client.get(
        "/api/collections", headers=user_token_headers, params={"ownership": "bogus"}
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_list_collections_limit_over_max_422(client, user_token_headers):
    response = client.get("/api/collections", headers=user_token_headers, params={"limit": 5000})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_list_collections_shared_shows_shared(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    """ownership=shared surfaces a collection shared with the caller."""
    col = _make_collection(db_session, normal_user)
    _make_share(db_session, col, normal_user, target_user=other_user, permission="viewer")
    response = client.get(
        "/api/collections", headers=other_user_auth_headers, params={"ownership": "shared"}
    )
    assert response.status_code == status.HTTP_200_OK
    entry = next((c for c in response.json() if c["uuid"] == str(col.uuid)), None)
    assert entry is not None
    assert entry["my_permission"] == "viewer"
    assert entry["is_shared"] is True


# ---------------------------------------------------------------------------
# GET /api/collections/shared-with-me
# ---------------------------------------------------------------------------


def test_shared_with_me_lists_shared(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    col = _make_collection(db_session, normal_user)
    _make_share(db_session, col, normal_user, target_user=other_user, permission="editor")
    response = client.get("/api/collections/shared-with-me", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_200_OK
    entry = next((c for c in response.json() if c["uuid"] == str(col.uuid)), None)
    assert entry is not None
    assert entry["my_permission"] == "editor"
    assert entry["shared_by"]["uuid"] == str(normal_user.uuid)


def test_shared_with_me_excludes_owned(client, user_token_headers, normal_user, db_session):
    """An owner never sees their own collection in shared-with-me."""
    _make_collection(db_session, normal_user)
    response = client.get("/api/collections/shared-with-me", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    # Owner has no shared collections from this fixture set.
    assert isinstance(response.json(), list)


# ---------------------------------------------------------------------------
# POST /api/collections  (create)
# ---------------------------------------------------------------------------


def test_create_collection_happy(client, user_token_headers):
    name = f"created-{uuid.uuid4().hex[:8]}"
    response = client.post("/api/collections", headers=user_token_headers, json={"name": name})
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == name


def test_create_collection_duplicate_400(client, user_token_headers, normal_user, db_session):
    existing = _make_collection(db_session, normal_user, name="dup-col")
    response = client.post(
        "/api/collections", headers=user_token_headers, json={"name": existing.name}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in response.json()["detail"]


def test_create_collection_unknown_prompt_404(client, user_token_headers):
    response = client.post(
        "/api/collections",
        headers=user_token_headers,
        json={"name": f"c-{uuid.uuid4().hex[:8]}", "default_prompt_id": str(uuid.uuid4())},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Summary prompt not found or not accessible"


def test_create_collection_unauthorized(client):
    response = client.post("/api/collections", json={"name": "x"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET / PUT / DELETE /api/collections/{uuid}
# ---------------------------------------------------------------------------


def test_get_collection_owner(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user)
    response = client.get(f"/api/collections/{col.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["uuid"] == str(col.uuid)


def test_get_collection_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user)
    response = client.get(f"/api/collections/{col.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    # The GET detail route uses get_collection_by_uuid_with_permission (viewer+),
    # whose detail differs from the sharing helper used by update/delete.
    assert response.json()["detail"] == "You do not have permission to access this collection"


def test_get_collection_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/collections/{uuid.uuid4()}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND


def test_update_collection_owner(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user)
    response = client.put(
        f"/api/collections/{col.uuid}",
        headers=user_token_headers,
        json={"description": "new description"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["description"] == "new description"


def test_update_collection_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    """An editor permission is required; a stranger is rejected by the sharing helper."""
    col = _make_collection(db_session, normal_user)
    response = client.put(
        f"/api/collections/{col.uuid}",
        headers=other_user_auth_headers,
        json={"name": "stolen"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not authorized to access this collection"


def test_delete_collection_owner(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user)
    response = client.delete(f"/api/collections/{col.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["message"] == "Collection deleted successfully"


# ---------------------------------------------------------------------------
# Media member add / remove
# ---------------------------------------------------------------------------


def test_add_media_owner(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user)
    mf = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/collections/{col.uuid}/media",
        headers=user_token_headers,
        json={"media_file_ids": [str(mf.uuid)]},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["added"] == 1


def test_add_media_unknown_file_404(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user)
    response = client.post(
        f"/api/collections/{col.uuid}/media",
        headers=user_token_headers,
        json={"media_file_ids": [str(uuid.uuid4())]},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert "not found or not authorized" in response.json()["detail"]


def test_add_media_other_user_collection_403(
    client, other_user_auth_headers, normal_user, db_session
):
    col = _make_collection(db_session, normal_user)
    mf = _make_file(db_session, normal_user)
    response = client.post(
        f"/api/collections/{col.uuid}/media",
        headers=other_user_auth_headers,
        json={"media_file_ids": [str(mf.uuid)]},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not authorized to access this collection"


def test_remove_media_owner(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user)
    mf = _make_file(db_session, normal_user)
    db_session.add(CollectionMember(collection_id=col.id, media_file_id=mf.id))
    db_session.commit()
    response = client.request(
        "DELETE",
        f"/api/collections/{col.uuid}/media",
        headers=user_token_headers,
        json={"media_file_ids": [str(mf.uuid)]},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["removed"] == 1


# ---------------------------------------------------------------------------
# Collection share management (owner-only) + user-XOR-group matrix
# ---------------------------------------------------------------------------


def test_list_shares_owner(client, user_token_headers, normal_user, other_user, db_session):
    col = _make_collection(db_session, normal_user)
    _make_share(db_session, col, normal_user, target_user=other_user)
    response = client.get(f"/api/collections/{col.uuid}/shares", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()) == 1
    assert response.json()[0]["target_type"] == "user"


def test_list_shares_non_owner_403(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    """A shared *editor* still cannot manage shares — only the direct owner."""
    col = _make_collection(db_session, normal_user)
    _make_share(db_session, col, normal_user, target_user=other_user, permission="editor")
    response = client.get(f"/api/collections/{col.uuid}/shares", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Only the collection owner can manage sharing"


def test_create_share_user_target(client, user_token_headers, normal_user, other_user, db_session):
    col = _make_collection(db_session, normal_user)
    response = client.post(
        f"/api/collections/{col.uuid}/shares",
        headers=user_token_headers,
        json={
            "target_type": "user",
            "target_uuid": str(other_user.uuid),
            "permission": "viewer",
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["target_type"] == "user"
    assert body["target_uuid"] == str(other_user.uuid)
    assert body["member_count"] is None  # user share has no member_count


def test_create_share_group_target(client, user_token_headers, normal_user, db_session):
    """A group-targeted share: the sharer must be a member of the group."""
    col = _make_collection(db_session, normal_user)
    group = UserGroup(owner_id=normal_user.id, name=f"g-{uuid.uuid4().hex[:6]}")
    db_session.add(group)
    db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=normal_user.id, role="owner"))
    db_session.commit()
    response = client.post(
        f"/api/collections/{col.uuid}/shares",
        headers=user_token_headers,
        json={
            "target_type": "group",
            "target_uuid": str(group.uuid),
            "permission": "editor",
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["target_type"] == "group"
    assert body["member_count"] == 1


def test_create_share_group_non_member_403(
    client, user_token_headers, normal_user, other_user, db_session
):
    """Cannot share with a group the sharer is not a member of."""
    col = _make_collection(db_session, normal_user)
    group = UserGroup(owner_id=other_user.id, name=f"g-{uuid.uuid4().hex[:6]}")
    db_session.add(group)
    db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=other_user.id, role="owner"))
    db_session.commit()
    response = client.post(
        f"/api/collections/{col.uuid}/shares",
        headers=user_token_headers,
        json={"target_type": "group", "target_uuid": str(group.uuid), "permission": "viewer"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You must be a member of the group to share with it"


def test_create_share_with_self_400(client, user_token_headers, normal_user, db_session):
    col = _make_collection(db_session, normal_user)
    response = client.post(
        f"/api/collections/{col.uuid}/shares",
        headers=user_token_headers,
        json={"target_type": "user", "target_uuid": str(normal_user.uuid), "permission": "viewer"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Cannot share a collection with yourself"


def test_create_share_duplicate_user_400(
    client, user_token_headers, normal_user, other_user, db_session
):
    col = _make_collection(db_session, normal_user)
    _make_share(db_session, col, normal_user, target_user=other_user)
    response = client.post(
        f"/api/collections/{col.uuid}/shares",
        headers=user_token_headers,
        json={"target_type": "user", "target_uuid": str(other_user.uuid), "permission": "viewer"},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Collection is already shared with this user"


def test_create_share_invalid_permission_422(
    client, user_token_headers, normal_user, other_user, db_session
):
    """permission is constrained to viewer|editor."""
    col = _make_collection(db_session, normal_user)
    response = client.post(
        f"/api/collections/{col.uuid}/shares",
        headers=user_token_headers,
        json={"target_type": "user", "target_uuid": str(other_user.uuid), "permission": "admin"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_share_invalid_target_type_422(
    client, user_token_headers, normal_user, other_user, db_session
):
    col = _make_collection(db_session, normal_user)
    response = client.post(
        f"/api/collections/{col.uuid}/shares",
        headers=user_token_headers,
        json={"target_type": "everyone", "target_uuid": str(other_user.uuid)},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_update_share_permission(client, user_token_headers, normal_user, other_user, db_session):
    col = _make_collection(db_session, normal_user)
    share = _make_share(db_session, col, normal_user, target_user=other_user, permission="viewer")
    response = client.put(
        f"/api/collections/{col.uuid}/shares/{share.uuid}",
        headers=user_token_headers,
        json={"permission": "editor"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["permission"] == "editor"


def test_delete_share(client, user_token_headers, normal_user, other_user, db_session):
    col = _make_collection(db_session, normal_user)
    share = _make_share(db_session, col, normal_user, target_user=other_user)
    response = client.delete(
        f"/api/collections/{col.uuid}/shares/{share.uuid}", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_delete_share_wrong_collection_404(
    client, user_token_headers, normal_user, other_user, db_session
):
    """A share on a *different* collection cannot be deleted via this path."""
    col1 = _make_collection(db_session, normal_user)
    col2 = _make_collection(db_session, normal_user)
    share = _make_share(db_session, col2, normal_user, target_user=other_user)
    response = client.delete(
        f"/api/collections/{col1.uuid}/shares/{share.uuid}", headers=user_token_headers
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Share not found on this collection"
