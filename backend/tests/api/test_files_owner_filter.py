"""Tests for issue #966: the gallery's ownership filter — a repeatable ``owner``
query param on ``GET /files`` / ``GET /files/metadata-filters``, and a new
``GET /files/owners`` closed-enumeration endpoint that feeds the frontend's
``SearchableMultiSelect``.

The load-bearing tests here are the AUTHORIZATION ones: a user who shares
nothing with another user must never see that user in the owners list or be
able to widen the ``owner`` filter to someone else's files, regardless of the
UUID they pass.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.sharing import CollectionShare


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


def _make_file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": f"owner_filt_{file_uuid[:8]}.wav",
        "title": "owner_filt_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": "completed",
        "duration": 60.0,
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _share_file(db_session, media_file, owner, target_user, *, permission: str = "viewer"):
    """Put ``media_file`` in a collection shared with ``target_user``."""
    collection = Collection(
        user_id=owner.id, name=f"owner-filt-shared-{_suffix()}", description="owner filter test"
    )
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=target_user.id,
            permission=permission,
        )
    )
    db_session.commit()
    return collection


def _uuids(response) -> set[str]:
    return {item["uuid"] for item in response.json()["items"]}


# ---------------------------------------------------------------------------
# GET /api/files/owners — closed enumeration, authorization-critical
# ---------------------------------------------------------------------------


def test_owners_endpoint_requires_auth(client):
    response = client.get("/api/files/owners")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_owners_list_excludes_unrelated_user_by_default(
    client, user_token_headers, normal_user, other_user, db_session
):
    """RED-FIRST AUTHORIZATION CASE.

    ``other_user`` shares nothing with ``normal_user`` and has files of their
    own. ``normal_user`` must not see ``other_user`` in the owners list —
    the list is scoped to files the CALLER can already see, never to every
    account that happens to own files.
    """
    _make_file(db_session, normal_user)
    _make_file(db_session, other_user)

    response = client.get("/api/files/owners", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = {row["uuid"] for row in response.json()}

    assert str(normal_user.uuid) in uuids
    assert str(other_user.uuid) not in uuids


def test_owners_list_includes_owner_after_a_real_share(
    client, user_token_headers, normal_user, other_user, db_session
):
    """The complement of the above: sharing a collection is what's supposed to
    make an owner visible — proves the endpoint isn't just failing closed for
    everything."""
    shared_file = _make_file(db_session, other_user)
    _share_file(db_session, shared_file, other_user, normal_user)

    response = client.get("/api/files/owners", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = {row["uuid"] for row in response.json()}
    assert str(other_user.uuid) in uuids


def test_owners_response_shape_is_masked_email_only(
    client, user_token_headers, normal_user, db_session
):
    """Constraint 5: no more per-owner information than the sharing picker
    (``GET /users/search``) already exposes — uuid, full_name, masked_email,
    and nothing else (no raw email, no internal id)."""
    _make_file(db_session, normal_user)

    response = client.get("/api/files/owners", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    rows = response.json()
    assert rows, "expected at least the caller's own file to produce an owner row"
    row = next(r for r in rows if r["uuid"] == str(normal_user.uuid))
    assert set(row.keys()) == {"uuid", "full_name", "masked_email"}
    assert normal_user.email not in row["masked_email"]
    assert "***" in row["masked_email"]


def test_owners_endpoint_ignores_free_text_query_param(
    client, user_token_headers, normal_user, other_user, db_session
):
    """Constraint 2: there is deliberately no ``q``/``search`` parameter. A
    stray ``q`` must not be interpreted as a filter that could probe for
    other accounts — FastAPI drops the unknown param and the closed-list
    behavior is unchanged."""
    _make_file(db_session, normal_user)
    _make_file(db_session, other_user)

    response = client.get(
        "/api/files/owners", headers=user_token_headers, params={"q": other_user.email[:2]}
    )
    assert response.status_code == status.HTTP_200_OK
    uuids = {row["uuid"] for row in response.json()}
    assert str(other_user.uuid) not in uuids


# ---------------------------------------------------------------------------
# GET /api/files?owner=... — the owner filter itself
# ---------------------------------------------------------------------------


def test_owner_filter_scopes_to_specific_owner(
    client, user_token_headers, normal_user, other_user, db_session
):
    """RED-FIRST AUTHORIZATION CASE.

    ``normal_user`` cannot use ``?owner=`` to widen their view onto
    ``other_user``'s unshared file — passing the UUID must not surface it,
    because the owner filter is applied INSIDE the caller's already-scoped
    accessible-file set, never as an independent lookup.
    """
    own_file = _make_file(db_session, normal_user)
    _make_file(db_session, other_user)

    response = client.get(
        "/api/files",
        headers=user_token_headers,
        params={"ownership": "all", "owner": str(other_user.uuid)},
    )
    assert response.status_code == status.HTTP_200_OK
    assert _uuids(response) == set()
    assert str(own_file.uuid) not in _uuids(response)


def test_owner_filter_narrows_to_selected_owner_after_share(
    client, user_token_headers, normal_user, other_user, db_session
):
    own_file = _make_file(db_session, normal_user)
    shared_file = _make_file(db_session, other_user)
    _share_file(db_session, shared_file, other_user, normal_user)

    response = client.get(
        "/api/files",
        headers=user_token_headers,
        params={"ownership": "all", "owner": str(other_user.uuid)},
    )
    assert response.status_code == status.HTTP_200_OK
    uuids = _uuids(response)
    assert str(shared_file.uuid) in uuids
    assert str(own_file.uuid) not in uuids


def test_owner_filter_unknown_uuid_narrows_to_nothing(
    client, user_token_headers, normal_user, db_session
):
    """An owner UUID that doesn't resolve to any real user must not fall
    through to "no filter" (mirrors the file_type fix for issue #871) —
    it should narrow to zero results."""
    _make_file(db_session, normal_user)

    response = client.get(
        "/api/files",
        headers=user_token_headers,
        params={"ownership": "all", "owner": str(uuid.uuid4())},
    )
    assert response.status_code == status.HTTP_200_OK
    assert _uuids(response) == set()


def test_metadata_filters_owner_param_matches_files_scope(
    client, user_token_headers, normal_user, other_user, db_session
):
    """The facet endpoint must agree with ``GET /files`` for the same
    ``owner`` selection, so the count shown beside a selected owner in the UI
    matches the results that selection actually produces."""
    _make_file(db_session, normal_user, duration=10.0)
    other_file = _make_file(db_session, other_user, duration=999.0)
    _share_file(db_session, other_file, other_user, normal_user)

    response = client.get(
        "/api/files/metadata-filters",
        headers=user_token_headers,
        params={"ownership": "all", "owner": str(other_user.uuid)},
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["duration"]["max"] == 999.0
    assert data["duration"]["min"] == 999.0
