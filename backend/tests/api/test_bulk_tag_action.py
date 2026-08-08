"""Behavioral tests for bulk tag add/remove on ``POST /api/files/management/bulk-action``.

Covers the ``add_tag`` / ``remove_tag`` rail (``files/management.py``) and the
mutation service behind it (``app/services/tag_bulk.py``). The scenarios here
are the ones a naive per-file loop gets wrong:

* **A no-op is not a failure.** Selecting 50 files where 6 already carry the tag
  has to end with all 50 carrying it and the 6 reported as *unchanged* — the
  old ``success | message | error`` envelope could not say that, so the outcome
  enum exists.
* **Read access is not write access.** The bulk rail resolves files through
  ``get_media_file_by_uuid``, which admits any non-None permission — ``viewer``
  included. Without an editor gate a read-only member of a shared collection
  could retag every file in it.
* **One aborted statement must not poison the batch.** Every file runs on the
  *same* session, and the rail's per-file ``except`` continues without a
  rollback. A single database error therefore leaves the psycopg2 transaction
  aborted and every later file fails with "current transaction is aborted"
  rather than with its own outcome. Per-file SAVEPOINTs are what make the
  failure attributable to the one file that caused it.

Tag names are globally unique and the suite runs under ``--dist loadgroup``, so
every name created here carries its own suffix. ``FileTag.media_file_id`` is
NOT NULL in the DDL (nullable only on the ORM model), so the fixtures create
real ``MediaFile`` rows.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

from fastapi import status
from sqlalchemy import event
from sqlalchemy import text

from app.core.constants import TAG_SOURCE_MANUAL
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.sharing import CollectionShare
from app.services.tag_service import normalize_tag_name

BULK_URL = "/api/files/management/bulk-action"


def _suffix() -> str:
    """Tag names are globally unique — every created name needs its own suffix."""
    return uuid.uuid4().hex[:8]


def _make_file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename="bulk_tag_test.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _make_tag(db_session, name: str) -> Tag:
    tag = Tag(name=name, source=TAG_SOURCE_MANUAL, normalized_name=normalize_tag_name(name))
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    return tag


def _attach(db_session, media_file, tag) -> FileTag:
    link = FileTag(media_file_id=media_file.id, tag_id=tag.id, source=TAG_SOURCE_MANUAL)
    db_session.add(link)
    db_session.commit()
    return link


def _links(db_session, media_file, tag) -> list[FileTag]:
    return (
        db_session.query(FileTag)
        .filter(FileTag.media_file_id == media_file.id, FileTag.tag_id == tag.id)
        .all()
    )


def _share_file(db_session, media_file, owner, viewer, *, permission: str) -> Collection:
    """Put ``media_file`` in a collection shared with ``viewer`` at ``permission``."""
    collection = Collection(
        user_id=owner.id, name=f"shared-{_suffix()}", description="bulk tag share"
    )
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=viewer.id,
            permission=permission,
        )
    )
    db_session.commit()
    return collection


def _bulk(client, headers, file_uuids, action, tag_name):
    return client.post(
        BULK_URL,
        headers=headers,
        json={
            "file_uuids": [str(u) for u in file_uuids],
            "action": action,
            "tag_name": tag_name,
        },
    )


def _by_uuid(results) -> dict[str, dict]:
    return {r["file_uuid"]: r for r in results}


# ---------------------------------------------------------------------------
# Add: changed vs unchanged
# ---------------------------------------------------------------------------


def test_bulk_add_reports_already_present_files_as_unchanged(
    client, user_token_headers, normal_user, db_session
):
    """Files that already carry the tag come back ``already_present``, not failed.

    The acceptance case in miniature: every selected file ends up carrying the
    tag, the ones that already did are reported as unchanged, and no file gains
    a duplicate association.
    """
    name = f"quarterly-{_suffix()}"
    tag = _make_tag(db_session, name)
    already = _make_file(db_session, normal_user)
    _attach(db_session, already, tag)
    fresh_one = _make_file(db_session, normal_user)
    fresh_two = _make_file(db_session, normal_user)

    response = _bulk(
        client,
        user_token_headers,
        [already.uuid, fresh_one.uuid, fresh_two.uuid],
        "add_tag",
        name,
    )

    assert response.status_code == status.HTTP_200_OK
    results = _by_uuid(response.json())
    assert results[str(already.uuid)]["outcome"] == "already_present"
    assert results[str(already.uuid)]["success"] is True
    assert results[str(fresh_one.uuid)]["outcome"] == "added"
    assert results[str(fresh_two.uuid)]["outcome"] == "added"

    # Every selected file carries it, exactly once.
    for media_file in (already, fresh_one, fresh_two):
        assert len(_links(db_session, media_file, tag)) == 1


def test_bulk_add_applies_an_existing_tag_rather_than_creating_a_variant(
    client, user_token_headers, normal_user, db_session
):
    """A name that normalizes onto an existing tag reuses that row.

    Resolution goes through ``resolve_or_create_tag``, so ``Q3 Review`` and
    ``q3-review`` are one tag, not two.
    """
    base = f"q3-review-{_suffix()}"
    existing = _make_tag(db_session, base)
    media_file = _make_file(db_session, normal_user)

    response = _bulk(
        client, user_token_headers, [media_file.uuid], "add_tag", base.replace("-", " ").upper()
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()[0]["outcome"] == "added"
    assert len(_links(db_session, media_file, existing)) == 1
    matching = db_session.query(Tag).filter(Tag.normalized_name == normalize_tag_name(base)).count()
    assert matching == 1


def test_bulk_add_resolves_the_tag_once_for_the_whole_batch(
    client, user_token_headers, normal_user, db_session
):
    """A brand-new name creates exactly one tag no matter how many files got it."""
    name = f"newtag-{_suffix()}"
    files = [_make_file(db_session, normal_user) for _ in range(3)]

    response = _bulk(client, user_token_headers, [f.uuid for f in files], "add_tag", name)

    assert response.status_code == status.HTTP_200_OK
    assert [r["outcome"] for r in response.json()] == ["added"] * 3
    created = db_session.query(Tag).filter(Tag.normalized_name == normalize_tag_name(name)).all()
    assert len(created) == 1
    for media_file in files:
        assert len(_links(db_session, media_file, created[0])) == 1


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


def test_bulk_remove_reports_not_present_rather_than_failure(
    client, user_token_headers, normal_user, db_session
):
    """Removing a tag a file never carried is a no-op outcome, not an error."""
    name = f"detach-{_suffix()}"
    tag = _make_tag(db_session, name)
    carrying = _make_file(db_session, normal_user)
    _attach(db_session, carrying, tag)
    untouched = _make_file(db_session, normal_user)

    response = _bulk(
        client, user_token_headers, [carrying.uuid, untouched.uuid], "remove_tag", name
    )

    assert response.status_code == status.HTTP_200_OK
    results = _by_uuid(response.json())
    assert results[str(carrying.uuid)]["outcome"] == "removed"
    assert results[str(untouched.uuid)]["outcome"] == "not_present"
    assert results[str(untouched.uuid)]["success"] is True
    assert _links(db_session, carrying, tag) == []


def test_bulk_remove_of_an_unknown_name_creates_no_tag(
    client, user_token_headers, normal_user, db_session
):
    """Remove looks the tag up; it must never take the create branch."""
    name = f"ghost-{_suffix()}"
    media_file = _make_file(db_session, normal_user)

    response = _bulk(client, user_token_headers, [media_file.uuid], "remove_tag", name)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()[0]["outcome"] == "not_present"
    assert (
        db_session.query(Tag).filter(Tag.normalized_name == normalize_tag_name(name)).count() == 0
    )


def test_bulk_tag_action_requires_a_tag_name(client, user_token_headers, normal_user, db_session):
    """A tag action with no usable name is rejected up front, not per file."""
    media_file = _make_file(db_session, normal_user)

    response = client.post(
        BULK_URL,
        headers=user_token_headers,
        json={"file_uuids": [str(media_file.uuid)], "action": "add_tag"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert response.json()["detail"] == "Tag name is required"


# ---------------------------------------------------------------------------
# Permission: viewer access is read access
# ---------------------------------------------------------------------------


def test_viewer_shared_file_fails_alone_and_the_batch_still_succeeds(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    """A file the caller can only *view* fails for that file only.

    ``get_media_file_by_uuid`` admits any non-None permission, so a read-only
    member of a shared collection reaches the handler. The editor gate is what
    stops the mutation — and it is a per-file failure, not a 403 for the batch.
    """
    name = f"viewergate-{_suffix()}"
    tag = _make_tag(db_session, name)
    read_only = _make_file(db_session, normal_user)
    _share_file(db_session, read_only, normal_user, other_user, permission="viewer")
    own_file = _make_file(db_session, other_user)

    response = _bulk(
        client, other_user_auth_headers, [read_only.uuid, own_file.uuid], "add_tag", name
    )

    assert response.status_code == status.HTTP_200_OK
    results = _by_uuid(response.json())
    assert results[str(read_only.uuid)]["success"] is False
    assert results[str(read_only.uuid)]["outcome"] == "failed"
    assert results[str(own_file.uuid)]["outcome"] == "added"
    # The viewer's mutation never landed; the rest of the batch still did.
    assert _links(db_session, read_only, tag) == []
    assert len(_links(db_session, own_file, tag)) == 1


def test_editor_shared_file_can_be_tagged(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    """The gate is *editor*, not ownership — an editor on a share may retag."""
    name = f"editorgate-{_suffix()}"
    tag = _make_tag(db_session, name)
    shared = _make_file(db_session, normal_user)
    _share_file(db_session, shared, normal_user, other_user, permission="editor")

    response = _bulk(client, other_user_auth_headers, [shared.uuid], "add_tag", name)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()[0]["outcome"] == "added"
    assert len(_links(db_session, shared, tag)) == 1


# ---------------------------------------------------------------------------
# Durability: one file's database error must not poison the rest
# ---------------------------------------------------------------------------


def test_mid_batch_database_error_is_attributable_and_earlier_files_keep_the_tag(
    client, user_token_headers, normal_user, db_session
):
    """A real database error on one file fails that file and nothing else.

    The error is forced with a genuinely failing statement on the request's own
    connection (``SELECT 1/0``) at the moment the poisoned file's association is
    flushed, which leaves Postgres in the aborted-transaction state a real
    constraint violation would. Without per-file SAVEPOINTs every later file
    fails with "current transaction is aborted"; with them, only the poisoned
    file fails and the files processed before it keep the tag.
    """
    name = f"savepoint-{_suffix()}"
    files = [_make_file(db_session, normal_user) for _ in range(5)]
    poisoned = files[2]
    fired = {"count": 0}

    @event.listens_for(db_session, "before_flush")
    def _poison(session, flush_context, instances):  # noqa: ANN001 - SQLAlchemy signature
        pending = [
            obj
            for obj in session.new
            if isinstance(obj, FileTag) and obj.media_file_id == poisoned.id
        ]
        if pending:
            fired["count"] += 1
            session.execute(text("SELECT 1/0"))

    try:
        response = _bulk(client, user_token_headers, [f.uuid for f in files], "add_tag", name)
    finally:
        event.remove(db_session, "before_flush", _poison)

    assert fired["count"] >= 1, "the poison never fired — the test proves nothing"
    assert response.status_code == status.HTTP_200_OK
    results = _by_uuid(response.json())

    assert results[str(poisoned.uuid)]["success"] is False
    assert results[str(poisoned.uuid)]["outcome"] == "failed"
    for media_file in files[:2] + files[3:]:
        assert results[str(media_file.uuid)]["outcome"] == "added", results[str(media_file.uuid)]

    tag = db_session.query(Tag).filter(Tag.normalized_name == normalize_tag_name(name)).one()
    for media_file in files[:2] + files[3:]:
        assert len(_links(db_session, media_file, tag)) == 1
    assert _links(db_session, poisoned, tag) == []


# ---------------------------------------------------------------------------
# Search refresh
# ---------------------------------------------------------------------------


def test_search_refresh_is_one_task_carrying_every_changed_file(
    client, user_token_headers, normal_user, db_session
):
    """One refresh task for the batch, carrying exactly the files that changed.

    Not one task per file (the reindex is a partial update over a list), and not
    the unchanged files (their indexed tag array is already correct).
    """
    name = f"reindex-{_suffix()}"
    tag = _make_tag(db_session, name)
    already = _make_file(db_session, normal_user)
    _attach(db_session, already, tag)
    changed_one = _make_file(db_session, normal_user)
    changed_two = _make_file(db_session, normal_user)

    delay = MagicMock()
    with patch("app.tasks.search_indexing_task.update_file_tags_index.delay", delay):
        response = _bulk(
            client,
            user_token_headers,
            [already.uuid, changed_one.uuid, changed_two.uuid],
            "add_tag",
            name,
        )

    assert response.status_code == status.HTTP_200_OK
    assert delay.call_count == 1, delay.call_args_list
    (enqueued,) = delay.call_args.args
    assert sorted(enqueued) == sorted([changed_one.id, changed_two.id])
