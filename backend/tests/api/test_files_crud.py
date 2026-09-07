"""Characterization tests for the files CRUD endpoints.

Covers ``files/crud.py`` plus the list/get/update/delete routes wired in
``files/__init__.py``:

- ``GET  /api/files``                     (list, pagination, filters)
- ``GET  /api/files/{uuid}``              (detail, incl. the #326 tag wire contract)
- ``GET  /api/files/{uuid}/info``         (lightweight metadata)
- ``PUT  /api/files/{uuid}``              (metadata update)
- ``DELETE /api/files/{uuid}``            (delete via cancel_upload fall-through)

These pin the CURRENT observable behavior (status code + ``detail``) so the
later model/dedup/perf refactors can't change the API by accident. They never
persist to dev data: rows are created directly on the savepoint-isolated
``db_session`` (rolled back at teardown) and no real MinIO object is required
because the code tolerates a missing object on the delete/stream paths in the
test environment (``SKIP_S3`` mock branch / best-effort purge).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi import status

from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag


def _make_file(db_session, owner, *, file_status: str = "completed", **overrides) -> MediaFile:
    """Create and persist a MediaFile row owned by ``owner`` on the test session.

    The row rolls back with the savepoint. ``storage_path`` points at a unique
    fake key so parallel workers never collide and the best-effort MinIO purge
    on delete is a harmless no-op (no real object exists).
    """
    file_uuid = str(uuid.uuid4())
    defaults = {
        "uuid": file_uuid,
        "filename": "crud_test.wav",
        "title": "crud_test",
        "storage_path": f"media/test/{file_uuid}.wav",
        "content_type": "audio/wav",
        "file_size": 4096,
        "status": file_status,
        "is_public": False,
        "user_id": owner.id,
    }
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


# ---------------------------------------------------------------------------
# GET /api/files  (list + pagination)
# ---------------------------------------------------------------------------


def test_list_files_unauthorized(client):
    """Listing without a token is rejected."""
    response = client.get("/api/files")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_list_files_empty_shape(client, user_token_headers):
    """The list endpoint always returns the paginated envelope."""
    response = client.get("/api/files", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in ("items", "total", "page", "page_size", "total_pages", "has_more"):
        assert key in body, f"missing pagination key {key!r}"
    assert isinstance(body["items"], list)
    assert body["page"] == 1
    assert body["page_size"] == 20


def test_list_files_returns_owned_file(client, user_token_headers, normal_user, db_session):
    """A freshly created file shows up in its owner's listing."""
    media_file = _make_file(db_session, normal_user, filename="listed.wav")
    response = client.get("/api/files", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    uuids = {item["uuid"] for item in body["items"]}
    assert str(media_file.uuid) in uuids
    assert body["total"] >= 1


def test_list_files_excludes_other_users_file(
    client, other_user_auth_headers, normal_user, db_session
):
    """``ownership=mine`` (default) never surfaces another user's file."""
    media_file = _make_file(db_session, normal_user, filename="not_yours.wav")
    response = client.get("/api/files", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = {item["uuid"] for item in response.json()["items"]}
    assert str(media_file.uuid) not in uuids


def test_list_files_pagination_params(client, user_token_headers):
    """Custom page/page_size are echoed back in the envelope."""
    response = client.get(
        "/api/files", headers=user_token_headers, params={"page": 2, "page_size": 5}
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["page"] == 2
    assert body["page_size"] == 5


def test_list_files_page_size_over_max_is_422(client, user_token_headers):
    """page_size has an upper bound of 100 (le=100)."""
    response = client.get("/api/files", headers=user_token_headers, params={"page_size": 500})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_list_files_page_zero_is_422(client, user_token_headers):
    """page is 1-indexed (ge=1)."""
    response = client.get("/api/files", headers=user_token_headers, params={"page": 0})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_list_files_bad_ownership_is_422(client, user_token_headers):
    """ownership is constrained to mine|shared|all by a regex pattern."""
    response = client.get("/api/files", headers=user_token_headers, params={"ownership": "bogus"})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_list_files_status_filter(client, user_token_headers, normal_user, db_session):
    """The status filter narrows results to the requested status only."""
    completed = _make_file(db_session, normal_user, file_status="completed", filename="done.wav")
    _make_file(db_session, normal_user, file_status="error", filename="failed.wav")

    response = client.get("/api/files", headers=user_token_headers, params={"status": "completed"})
    assert response.status_code == status.HTTP_200_OK
    statuses = {item["status"] for item in response.json()["items"]}
    assert statuses <= {"completed"}
    uuids = {item["uuid"] for item in response.json()["items"]}
    assert str(completed.uuid) in uuids


def test_list_files_search_filter_no_match(client, user_token_headers, normal_user, db_session):
    """A search term that matches nothing yields an empty-but-valid envelope."""
    _make_file(db_session, normal_user, filename="alpha.wav", title="alpha")
    response = client.get(
        "/api/files",
        headers=user_token_headers,
        params={"search": f"no-such-term-{uuid.uuid4().hex}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["items"] == []


def test_list_files_sort_param_accepts_known_field(
    client, user_token_headers, normal_user, db_session
):
    """A valid sort field + order returns 200 (sorting is best-effort, never 422)."""
    _make_file(db_session, normal_user, filename="s1.wav")
    response = client.get(
        "/api/files",
        headers=user_token_headers,
        params={"sort_by": "filename", "sort_order": "asc"},
    )
    assert response.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}  (detail)
# ---------------------------------------------------------------------------


def test_get_file_unauthorized(client):
    """Detail without a token is rejected."""
    response = client.get(f"/api/files/{uuid.uuid4()}")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_get_file_owner_200(client, user_token_headers, normal_user, db_session):
    """The owner can read their own file detail."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["uuid"] == str(media_file.uuid)
    # Owner convention: my_permission is null for the actual owner.
    assert body.get("my_permission") is None


def test_get_file_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    """A non-owner gets 403 with the canonical permission detail."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_get_file_admin_can_read_any(client, admin_token_headers, normal_user, db_session):
    """Admins bypass ownership and see ``my_permission='owner'``."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json().get("my_permission") == "owner"


def test_get_file_nonexistent_uuid_404(client, user_token_headers):
    """A well-formed but unknown UUID is a 404 'File not found'."""
    response = client.get(f"/api/files/{uuid.uuid4()}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_get_file_malformed_uuid_422(client, user_token_headers):
    """The detail route declares ``file_uuid: UUID`` → FastAPI 422 on bad input."""
    response = client.get("/api/files/not-a-uuid", headers=user_token_headers)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_get_file_segment_limit_negative_422(client, user_token_headers, normal_user, db_session):
    """segment_limit is constrained ge=0."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        params={"segment_limit": -1},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}  —  tag wire contract (issue #326)
#
# The detail payload used to serialize tags as bare name strings while every
# /api/tags surface returned objects. These pin the single agreed shape so the
# contract can't silently regress to `list[str]`.
# ---------------------------------------------------------------------------


def test_file_detail_tags_are_objects_not_names(
    client, user_token_headers, normal_user, db_session
):
    """`tags` carries `{uuid, name, source}` objects, never bare name strings."""
    media_file = _make_file(db_session, normal_user)
    tag = Tag(name=f"contract-{uuid.uuid4().hex[:8]}", user_id=normal_user.id, source="auto_ai")
    db_session.add(tag)
    db_session.flush()
    db_session.add(FileTag(media_file_id=media_file.id, tag_id=tag.id, source="auto_ai"))
    db_session.commit()

    response = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK

    tags = response.json()["tags"]
    assert len(tags) == 1
    payload = tags[0]
    assert isinstance(payload, dict), "tags must be objects, not strings (#326)"
    assert payload["uuid"] == str(tag.uuid)
    assert payload["name"] == tag.name
    assert payload["source"] == "auto_ai"
    # The hybrid-ID rule: the internal integer id never reaches the wire.
    assert "id" not in payload


def test_file_detail_tags_match_the_tags_endpoint_shape(
    client, user_token_headers, normal_user, db_session
):
    """The detail payload and ``GET /api/tags`` agree field-for-field."""
    media_file = _make_file(db_session, normal_user)
    tag = Tag(name=f"contract-{uuid.uuid4().hex[:8]}", user_id=normal_user.id, source="manual")
    db_session.add(tag)
    db_session.flush()
    db_session.add(FileTag(media_file_id=media_file.id, tag_id=tag.id, source="manual"))
    db_session.commit()

    from_detail = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers).json()[
        "tags"
    ][0]
    listing = client.get("/api/tags", headers=user_token_headers).json()
    from_tags = next(t for t in listing if t["uuid"] == str(tag.uuid))

    # /api/tags adds usage_count + awaiting_review (TagWithCount); everything
    # else is identical. `ownership` is part of the canonical shape, not an
    # extra on one surface: both endpoints classify it against the same caller,
    # so they must agree — a file-detail tag reported `mine` while the list
    # calls it `shared_with_me` would have the UI offer a rename that 404s.
    for field in ("uuid", "name", "source", "ownership"):
        assert from_detail[field] == from_tags[field]
    assert set(from_detail) == {"uuid", "name", "source", "ownership"}


def test_file_list_still_has_no_tags_field(client, user_token_headers, normal_user, db_session):
    """The gallery list endpoint carries no ``tags`` field — deliberately (#326).

    Adding one would need a per-row tag query. Pinned so the two endpoints
    aren't confused for each other when reading the changelog.
    """
    _make_file(db_session, normal_user)
    response = client.get("/api/files", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    items = response.json()["items"]
    assert items, "expected at least the file just created"
    assert all("tags" not in item for item in items)


# ---------------------------------------------------------------------------
# GET /api/files/{uuid}/info  (lightweight metadata)
# ---------------------------------------------------------------------------


def test_get_file_info_owner_200(client, user_token_headers, normal_user, db_session):
    """The info endpoint returns core identity fields for the owner."""
    media_file = _make_file(db_session, normal_user, filename="info.wav")
    response = client.get(f"/api/files/{media_file.uuid}/info", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["uuid"] == str(media_file.uuid)
    assert body["filename"] == "info.wav"
    assert body["status"] == "completed"
    # user_id is exposed as the OWNER's UUID, never the integer PK.
    assert body["user_id"] == str(normal_user.uuid)


def test_get_file_info_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session
):
    """Non-owner info read is 403 with the same permission detail as detail."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/info", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_get_file_info_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/files/{uuid.uuid4()}/info", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


# ---------------------------------------------------------------------------
# PUT /api/files/{uuid}  (metadata update)
# ---------------------------------------------------------------------------


def test_update_file_title(client, user_token_headers, normal_user, db_session):
    """Updating the title round-trips and returns the updated file."""
    media_file = _make_file(db_session, normal_user, title="before")
    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"title": "after"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "after"


def test_update_file_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.put(f"/api/files/{media_file.uuid}", json={"title": "x"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_update_file_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    """A non-owner cannot update someone else's file metadata."""
    media_file = _make_file(db_session, normal_user)
    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=other_user_auth_headers,
        json={"title": "hijack"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_update_file_nonexistent_404(client, user_token_headers):
    response = client.put(
        f"/api/files/{uuid.uuid4()}", headers=user_token_headers, json={"title": "x"}
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_update_file_invalid_status_422(client, user_token_headers, normal_user, db_session):
    """``status`` is a constrained enum on MediaFileUpdate → 422 on garbage."""
    media_file = _make_file(db_session, normal_user)
    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=user_token_headers,
        json={"status": "not-a-real-status"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_update_file_malformed_uuid_422(client, user_token_headers):
    """PUT /{file_uuid} declares ``file_uuid: UUID`` → 422 on bad UUID."""
    response = client.put("/api/files/not-a-uuid", headers=user_token_headers, json={"title": "x"})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ---------------------------------------------------------------------------
# DELETE /api/files/{uuid}  (delete via cancel_upload fall-through)
# ---------------------------------------------------------------------------


def test_delete_file_owner_204(client, user_token_headers, normal_user, db_session):
    """The owner deletes a completed file; the row is gone afterward (204)."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT
    # The DB row is removed (within the savepoint); a follow-up read is 404.
    follow_up = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


def test_delete_file_unauthorized(client, normal_user, db_session):
    media_file = _make_file(db_session, normal_user)
    response = client.delete(f"/api/files/{media_file.uuid}")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_delete_file_other_user_forbidden(client, other_user_auth_headers, normal_user, db_session):
    """A non-owner deleting another user's (non-pending) file hits the full-delete
    permission check → 403 with the canonical detail."""
    media_file = _make_file(db_session, normal_user, file_status="completed")
    response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_delete_file_nonexistent_404(client, user_token_headers):
    """Deleting an unknown UUID falls through to delete_media_file → 404."""
    response = client.delete(f"/api/files/{uuid.uuid4()}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


def test_delete_file_with_live_active_task_409(client, user_token_headers, normal_user, db_session):
    """A file with a genuinely live active task is refused a plain delete.

    ``is_file_safe_to_delete`` (``app/utils/task_utils.py``) is deliberately NOT
    gated on ``status == PROCESSING`` — a follow-on stage on an already-``completed``
    file (summarization/embedding/search_indexing/...) sets ``active_task_id``
    without ever touching ``status``. Found live: a full pipeline run's DELETE right
    after completion 409'd twice, each time against a *different* active_task_id
    from a follow-on task, before ``/force`` was needed to clean up. This pins the
    409 contract that behavior depends on so it can't silently regress into either
    "always blocks" (users could never delete a completed file) or "never blocks"
    (a real live task's output could be deleted out from under it).
    """
    media_file = _make_file(
        db_session,
        normal_user,
        file_status="completed",
        active_task_id="11111111-1111-1111-1111-111111111111",
    )
    with patch("app.utils.task_utils.AsyncResult") as mock_async_result:
        mock_async_result.return_value.state = "STARTED"
        response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_409_CONFLICT
    body = response.json()["detail"]
    assert body["error"] == "FILE_NOT_SAFE_TO_DELETE"
    assert body["active_task_id"] == "11111111-1111-1111-1111-111111111111"
    assert body["options"]["wait_for_completion"] is True
    # The file survives the refused delete — a follow-up read is still 200, not 404.
    follow_up = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert follow_up.status_code == status.HTTP_200_OK


def test_delete_file_with_stale_active_task_204(
    client, user_token_headers, normal_user, db_session
):
    """An ``active_task_id`` Celery no longer reports as live does not block delete.

    Same file shape as the 409 case above, but the Celery-side double-check reports
    the task already finished — proving the 409 is keyed on the task's REAL state,
    not merely on ``active_task_id`` being non-null (a stale/never-cleared column
    must not permanently strand a file as undeletable).
    """
    media_file = _make_file(
        db_session,
        normal_user,
        file_status="completed",
        active_task_id="22222222-2222-2222-2222-222222222222",
    )
    with patch("app.utils.task_utils.AsyncResult") as mock_async_result:
        mock_async_result.return_value.state = "SUCCESS"
        response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_204_NO_CONTENT


def test_delete_file_malformed_uuid_404(client, user_token_headers):
    """DELETE /{file_uuid} declares ``file_uuid: str`` (no FastAPI UUID coercion).

    BUGFIX (this branch, files/cancel_upload.py): a malformed UUID used to reach
    an unparametrized ``MediaFile.uuid == "<garbage>"`` query, which Postgres
    rejected with ``invalid input syntax for type uuid`` — surfacing as an
    unhandled 500 and a poisoned DB transaction. The route now rejects a bad
    UUID up front with 404 'File not found', matching every other delete entry
    point (``get_by_uuid``). Valid-UUID behavior is unchanged.
    """
    response = client.delete("/api/files/not-a-uuid", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "File not found"


# ---------------------------------------------------------------------------
# Duplicate detection (check_duplicate_by_fingerprint branches via /prepare)
# These live with prepare/complete tests; the crud-level hash field is asserted
# here to lock the dedup column contract used by the duplicate-detection branch.
# ---------------------------------------------------------------------------


def test_file_hash_persisted_for_dedup(client, user_token_headers, normal_user, db_session):
    """A row created with a file_hash exposes it so dedup-by-hash can find it.

    This is the data contract the duplicate-detection branch depends on
    (``check_duplicate_by_fingerprint`` filters on ``MediaFile.file_hash`` +
    a real ``storage_path`` + a non-failed status).
    """
    digest = uuid.uuid4().hex
    media_file = _make_file(db_session, normal_user, file_hash=digest)
    response = client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["file_hash"] == digest


@pytest.mark.parametrize("suffix", ["info", "analytics", "status-detail"])
def test_subresource_other_user_forbidden(
    client, other_user_auth_headers, normal_user, db_session, suffix
):
    """Read-only file subresources all reject a non-owner the same way (403)."""
    media_file = _make_file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}/{suffix}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"
