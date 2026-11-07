"""Per-user hide of media files (issue #1102): the routes and every list that honours it.

Hide is ONE user's view preference, not access control. Each test below pins one half of that
sentence: the file leaves the hider's lists, and nothing changes for anyone else, for a direct
link, or for a trashed file (trash always wins). Every assertion has its control: the same call
before the hide, or the same call by another user.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi import status
from sqlalchemy import text

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import MediaFile
from app.models.sharing import CollectionShare
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services import hidden_media_service

_TITLE = "Quillbarrow standup"


def _file(db_session, owner, **overrides) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{_TITLE}.wav",
        title=_TITLE,
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=overrides.pop("status", "completed"),
        is_public=False,
        user_id=owner.id,
        upload_time=datetime.now(UTC),
        **overrides,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _shared_with(db_session, owner, viewer, *files: MediaFile) -> Collection:
    """A personal collection of ``owner``'s holding ``files``, shared read-only with ``viewer``."""
    collection = Collection(
        uuid=uuid.uuid4(), name=f"Shared {uuid.uuid4().hex[:6]}", user_id=owner.id
    )
    db_session.add(collection)
    db_session.commit()
    for media_file in files:
        db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.add(
        CollectionShare(
            uuid=uuid.uuid4(),
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=viewer.id,
            permission="viewer",
        )
    )
    db_session.commit()
    return collection


def _gallery(client, headers, **params) -> dict[str, dict]:
    response = client.get(
        "/api/files", headers=headers, params={"page_size": 100, "ownership": "all", **params}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return {item["uuid"]: item for item in response.json()["items"]}


def _hide(client, headers, media_file) -> None:
    response = client.put(f"/api/files/{media_file.uuid}/hidden", headers=headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text


def _hidden_count(db_session, user) -> int:
    return int(
        db_session.execute(
            text("SELECT count(*) FROM user_hidden_media WHERE user_id = :u"), {"u": user.id}
        ).scalar_one()
    )


# --- the gallery ------------------------------------------------------------------------------


def test_a_hidden_file_leaves_the_hiders_gallery_and_only_theirs(
    client, user_token_headers, other_user_auth_headers, normal_user, other_user, db_session
):
    """The viewer of a shared file hides it; the owner keeps seeing it."""
    media_file = _file(db_session, normal_user)
    _shared_with(db_session, normal_user, other_user, media_file)
    key = str(media_file.uuid)
    assert key in _gallery(client, other_user_auth_headers), "control: shared and visible"

    _hide(client, other_user_auth_headers, media_file)

    assert key not in _gallery(client, other_user_auth_headers)
    assert key in _gallery(client, user_token_headers), "hide must never reach another user"


def test_the_hidden_filter_includes_or_isolates_hidden_files_and_marks_them(
    client, user_token_headers, normal_user, db_session
):
    hidden_file = _file(db_session, normal_user)
    shown_file = _file(db_session, normal_user)
    _hide(client, user_token_headers, hidden_file)

    included = _gallery(client, user_token_headers, hidden="include")
    assert included[str(hidden_file.uuid)]["is_hidden"] is True
    assert included[str(shown_file.uuid)]["is_hidden"] is False

    only = _gallery(client, user_token_headers, hidden="only")
    assert str(hidden_file.uuid) in only
    assert str(shown_file.uuid) not in only

    excluded = _gallery(client, user_token_headers)
    assert str(hidden_file.uuid) not in excluded
    assert excluded[str(shown_file.uuid)]["is_hidden"] is False


def test_an_unknown_hidden_mode_is_refused(client, user_token_headers):
    response = client.get("/api/files", headers=user_token_headers, params={"hidden": "all"})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_unhide_brings_the_file_back(client, user_token_headers, normal_user, db_session):
    media_file = _file(db_session, normal_user)
    _hide(client, user_token_headers, media_file)
    assert str(media_file.uuid) not in _gallery(client, user_token_headers)

    response = client.delete(f"/api/files/{media_file.uuid}/hidden", headers=user_token_headers)

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    assert str(media_file.uuid) in _gallery(client, user_token_headers)
    assert _hidden_count(db_session, normal_user) == 0


def test_hiding_twice_is_a_no_op(client, user_token_headers, normal_user, db_session):
    media_file = _file(db_session, normal_user)
    _hide(client, user_token_headers, media_file)
    _hide(client, user_token_headers, media_file)
    assert _hidden_count(db_session, normal_user) == 1


def test_the_metadata_facets_follow_the_same_hidden_mode(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user, language="xh")
    url = "/api/files/metadata-filters"
    assert "xh" in client.get(url, headers=user_token_headers).json()["languages"]

    _hide(client, user_token_headers, media_file)

    assert "xh" not in client.get(url, headers=user_token_headers).json()["languages"]
    only = client.get(url, headers=user_token_headers, params={"hidden": "only"}).json()
    assert "xh" in only["languages"]


# --- what hide must NOT affect -----------------------------------------------------------------


def test_a_hidden_file_still_opens_by_direct_link(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    _hide(client, user_token_headers, media_file)

    response = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == status.HTTP_200_OK, response.text


def test_a_file_the_caller_cannot_read_cannot_be_hidden(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    """Not shared with the caller: 404 exactly like a missing file, and no row is written."""
    media_file = _file(db_session, normal_user)

    response = client.put(f"/api/files/{media_file.uuid}/hidden", headers=other_user_auth_headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
    assert _hidden_count(db_session, other_user) == 0
    missing = client.put(f"/api/files/{uuid.uuid4()}/hidden", headers=other_user_auth_headers)
    assert missing.status_code == status.HTTP_404_NOT_FOUND, "control: a missing file"


def test_trash_wins_over_hide_and_a_restore_keeps_the_hide(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    _hide(client, user_token_headers, media_file)
    assert str(media_file.uuid) in _gallery(client, user_token_headers, hidden="only")

    now = datetime.now(UTC)
    media_file.deleted_at = now
    media_file.purge_after = now + timedelta(days=30)
    media_file.trash_origin = "user"
    media_file.deleted_by_user_id = normal_user.id
    db_session.commit()

    for mode in ("only", "include", "exclude"):
        assert str(media_file.uuid) not in _gallery(client, user_token_headers, hidden=mode)
    assert _hidden_count(db_session, normal_user) == 1, "trash keeps the hide row"

    media_file.deleted_at = None
    media_file.purge_after = None
    media_file.trash_origin = None
    media_file.deleted_by_user_id = None
    db_session.commit()

    assert str(media_file.uuid) in _gallery(client, user_token_headers, hidden="only")
    assert str(media_file.uuid) not in _gallery(client, user_token_headers)


def test_purging_a_file_removes_every_users_hide_rows(
    client, user_token_headers, other_user_auth_headers, normal_user, other_user, db_session
):
    media_file = _file(db_session, normal_user)
    _shared_with(db_session, normal_user, other_user, media_file)
    _hide(client, user_token_headers, media_file)
    _hide(client, other_user_auth_headers, media_file)
    assert _hidden_count(db_session, normal_user) == 1
    assert _hidden_count(db_session, other_user) == 1

    db_session.delete(media_file)
    db_session.commit()

    assert _hidden_count(db_session, normal_user) == 0
    assert _hidden_count(db_session, other_user) == 0


# --- the cap ----------------------------------------------------------------------------------


def test_the_cap_is_ten_thousand():
    assert hidden_media_service.HIDDEN_MEDIA_PER_USER_CAP == 10_000


def test_the_hide_past_the_cap_is_refused_with_hidden_limit(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """The cap is lowered to 2 so the boundary is reachable; the rule is the same at 10,000."""
    monkeypatch.setattr(hidden_media_service, "HIDDEN_MEDIA_PER_USER_CAP", 2)
    files = [_file(db_session, normal_user) for _ in range(3)]
    _hide(client, user_token_headers, files[0])
    _hide(client, user_token_headers, files[1])

    response = client.put(f"/api/files/{files[2].uuid}/hidden", headers=user_token_headers)

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert response.json()["detail"]["error"] == "HIDDEN_LIMIT"
    assert _hidden_count(db_session, normal_user) == 2
    _hide(client, user_token_headers, files[0])  # re-hiding a hidden file is not a new slot


# --- the bulk action --------------------------------------------------------------------------


def _bulk(client, headers, action, *files):
    response = client.post(
        "/api/files/management/bulk-action",
        headers=headers,
        json={"action": action, "file_uuids": [str(f.uuid) for f in files]},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return {r["file_uuid"]: r for r in response.json()}


def test_bulk_hide_and_unhide(client, user_token_headers, normal_user, other_user, db_session):
    mine = [_file(db_session, normal_user) for _ in range(2)]
    theirs = _file(db_session, other_user)

    results = _bulk(client, user_token_headers, "hide", *mine, theirs)

    assert all(results[str(f.uuid)]["success"] for f in mine)
    assert results[str(theirs.uuid)]["success"] is False, "a file the caller cannot read"
    assert _hidden_count(db_session, normal_user) == 2
    assert not {str(f.uuid) for f in mine} & set(_gallery(client, user_token_headers))

    results = _bulk(client, user_token_headers, "unhide", *mine)

    assert all(r["success"] for r in results.values())
    assert _hidden_count(db_session, normal_user) == 0


def test_bulk_hide_reports_the_cap_per_file(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    monkeypatch.setattr(hidden_media_service, "HIDDEN_MEDIA_PER_USER_CAP", 1)
    first, second = (_file(db_session, normal_user) for _ in range(2))

    results = _bulk(client, user_token_headers, "hide", first, second)

    assert results[str(first.uuid)]["success"] is True
    assert results[str(second.uuid)]["error"] == "HIDDEN_LIMIT"


# --- the other lists --------------------------------------------------------------------------


def test_the_dashboard_lists_leave_out_a_hidden_file_but_the_counts_do_not(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user, status="error")

    def _status():
        response = client.get("/api/my-files/status", headers=user_token_headers)
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        recent = {f["uuid"] for f in body["recent_files"]["files"]}
        problem = {f["uuid"] for f in body["problem_files"]["files"]}
        return recent, problem, body["status_counts"]["total"]

    recent, problem, total = _status()
    assert str(media_file.uuid) in recent and str(media_file.uuid) in problem

    _hide(client, user_token_headers, media_file)

    recent_after, problem_after, total_after = _status()
    assert str(media_file.uuid) not in recent_after
    assert str(media_file.uuid) not in problem_after
    assert total_after == total, "hiding is not deleting: the counts stay whole"


def test_a_collections_lists_honour_the_hidden_mode(
    client, user_token_headers, normal_user, db_session
):
    hidden_file = _file(db_session, normal_user)
    shown_file = _file(db_session, normal_user)
    collection = Collection(uuid=uuid.uuid4(), name="Mine", user_id=normal_user.id)
    db_session.add(collection)
    db_session.commit()
    for media_file in (hidden_file, shown_file):
        db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.commit()
    _hide(client, user_token_headers, hidden_file)

    def _members(**params) -> set[str]:
        response = client.get(
            f"/api/collections/{collection.uuid}/media", headers=user_token_headers, params=params
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        return {item["uuid"] for item in response.json()["items"]}

    def _detail(**params) -> set[str]:
        response = client.get(
            f"/api/collections/{collection.uuid}", headers=user_token_headers, params=params
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        return {item["uuid"] for item in response.json()["media_files"]}

    both = {str(hidden_file.uuid), str(shown_file.uuid)}
    for read in (_members, _detail):
        assert read() == {str(shown_file.uuid)}
        assert read(hidden="include") == both
        assert read(hidden="only") == {str(hidden_file.uuid)}


def test_a_tags_file_list_honours_the_hidden_mode(
    client, user_token_headers, normal_user, db_session
):
    hidden_file = _file(db_session, normal_user)
    shown_file = _file(db_session, normal_user)
    tag = Tag(uuid=uuid.uuid4(), name=f"hide-{uuid.uuid4().hex[:6]}", user_id=normal_user.id)
    db_session.add(tag)
    db_session.commit()
    for media_file in (hidden_file, shown_file):
        db_session.add(FileTag(media_file_id=media_file.id, tag_id=tag.id))
    db_session.commit()
    _hide(client, user_token_headers, hidden_file)

    def _files(**params) -> tuple[set[str], int]:
        response = client.get(
            f"/api/tags/{tag.uuid}/files", headers=user_token_headers, params=params
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        return {f["uuid"] for f in body["files"]}, body["total"]

    assert _files() == ({str(shown_file.uuid)}, 1)
    assert _files(hidden="include")[1] == 2
    assert _files(hidden="only") == ({str(hidden_file.uuid)}, 1)


@pytest.mark.parametrize("verb", ["put", "delete"])
def test_the_routes_need_a_signed_in_user(client, verb):
    response = getattr(client, verb)(f"/api/files/{uuid.uuid4()}/hidden")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
