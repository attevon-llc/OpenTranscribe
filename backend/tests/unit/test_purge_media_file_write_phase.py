"""``purge_media_file``'s write phase: the citation scrub, the rollback, the owner's caches.

A HEAD-vs-split mutation comparison of ``services/file_cleanup_service.py`` (repo cleanup,
wave b10) found these branches pinned by nothing the throwaway-DB suite runs. The only test
that observed the chat-citation scrub was the live-stack residue suite, which skips without
OpenSearch and MinIO, so deleting the scrub outright left every other test green:

* the scrub must remove every citation naming the destroyed file, keep the ones naming
  other files and the ones naming no file, and touch no other user's unrelated messages;
* it must ride the row delete's transaction (run before the commit that deletes the row),
  so a failed delete never leaves the history scrubbed for a file that still exists;
* a failed destroy must roll the session back rather than leave half a write pending;
* the Redis invalidation must name the file's OWNER, whose listings named the file;
* the external-copy phase must be handed every object path the plan read, the thumbnail
  included.

Object storage and OpenSearch are stubbed at the ``_purge_external_copies`` seam (or just
below it), so only Postgres is real.
"""

import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.media import MediaFile
from app.services import file_cleanup_service as fcs


@pytest.fixture
def quiet_stores(monkeypatch):
    """No object storage, OpenSearch or speaker-aggregate work; the row delete stays real."""
    monkeypatch.setattr(fcs, "_purge_external_copies", lambda plan: [])
    monkeypatch.setattr(fcs, "_refresh_surviving_centroids", lambda db, owner_id, plan: [])


def _file(db_session, owner, **extra: Any) -> MediaFile:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        filename=f"purge-write-{uuid_pkg.uuid4().hex[:8]}.mp4",
        storage_path=f"purge-write-test/{uuid_pkg.uuid4().hex}.mp4",
        content_type="video/mp4",
        file_size=1024,
        user_id=owner.id,
        **extra,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _message(db_session, owner, citations: list[dict[str, Any]]) -> int:
    conversation = ChatConversation(user_id=owner.id, title="purge write phase")
    db_session.add(conversation)
    db_session.flush()
    message = ChatMessage(
        conversation_id=conversation.id, role="assistant", content="see [1]", citations=citations
    )
    db_session.add(message)
    db_session.commit()
    return int(message.id)


def _citations(db_session, message_id: int) -> list[dict[str, Any]]:
    db_session.expire_all()
    return list(db_session.get(ChatMessage, message_id).citations or [])


def test_the_purge_scrubs_exactly_the_citations_that_name_the_file(
    db_session, normal_user, other_user, quiet_stores
):
    doomed = _file(db_session, normal_user)
    kept = _file(db_session, normal_user)
    doomed_uuid, kept_uuid = str(doomed.uuid), str(kept.uuid)
    quoting = {"file_uuid": doomed_uuid, "snippet": "what was said", "title": "doomed"}
    other_file = {"file_uuid": kept_uuid, "snippet": "something else", "title": "kept"}
    unkeyed = {"snippet": "a citation that names no file"}
    mine = _message(db_session, normal_user, [quoting, other_file, unkeyed])
    # Another user's chat that cited the (shared) file loses the quote too.
    theirs = _message(db_session, other_user, [dict(quoting)])
    # And a message that never cited it is left exactly as it was.
    untouched = _message(db_session, other_user, [dict(other_file)])

    result = fcs.purge_media_file(db_session, doomed)

    assert result["deleted"] is True, result
    assert _citations(db_session, mine) == [other_file, unkeyed]
    assert _citations(db_session, theirs) == []
    assert _citations(db_session, untouched) == [other_file]


def test_the_scrub_rides_the_row_deletes_transaction(
    db_session, normal_user, quiet_stores, monkeypatch
):
    """Scrub, then delete, then commit: never a commit between the scrub and the delete."""
    media_file = _file(db_session, normal_user)
    events: list[str] = []
    real_scrub, real_delete, real_commit = (
        fcs.scrub_chat_citations,
        db_session.delete,
        db_session.commit,
    )

    def scrub(db, file_uuids):
        events.append("scrub")
        return real_scrub(db, file_uuids)

    def delete(instance):
        events.append("delete")
        return real_delete(instance)

    def commit():
        events.append("commit")
        return real_commit()

    monkeypatch.setattr(fcs, "scrub_chat_citations", scrub)
    monkeypatch.setattr(db_session, "delete", delete)
    monkeypatch.setattr(db_session, "commit", commit)

    result = fcs.purge_media_file(db_session, media_file)

    assert result["deleted"] is True, result
    assert "scrub" in events and "delete" in events, events
    scrub_at, delete_at = events.index("scrub"), events.index("delete")
    assert scrub_at < delete_at, events
    assert "commit" not in events[scrub_at:delete_at], events
    assert "commit" in events[delete_at:], events


def test_a_failed_destroy_rolls_the_session_back(db_session, normal_user, monkeypatch):
    media_file = _file(db_session, normal_user)
    rollbacks: list[bool] = []

    def boom(plan):
        raise RuntimeError("object storage unreachable")

    monkeypatch.setattr(fcs, "_purge_external_copies", boom)
    # Recorded, not called through: the harness's outer transaction must survive the test.
    monkeypatch.setattr(db_session, "rollback", lambda: rollbacks.append(True))

    result = fcs.purge_media_file(db_session, media_file)

    assert result["deleted"] is False
    assert result["error"] == "Delete failed (RuntimeError)"
    assert rollbacks == [True]


def test_the_cache_invalidation_names_the_files_owner(
    db_session, normal_user, quiet_stores, monkeypatch
):
    from app.services.redis_cache_service import redis_cache

    media_file = _file(db_session, normal_user)
    invalidated: list[int] = []
    monkeypatch.setattr(redis_cache, "invalidate_all_for_user", invalidated.append)

    result = fcs.purge_media_file(db_session, media_file)

    assert result["deleted"] is True, result
    assert invalidated == [int(normal_user.id)]


def test_the_external_phase_is_handed_every_object_path(db_session, normal_user, monkeypatch):
    media_file = _file(
        db_session,
        normal_user,
        thumbnail_path=f"thumbnails/{uuid_pkg.uuid4().hex}.webp",
        playback_path=f"playback/{uuid_pkg.uuid4().hex}.m4a",
    )
    expected = {
        "filename": str(media_file.filename),
        "storage_path": str(media_file.storage_path),
        "thumbnail_path": str(media_file.thumbnail_path),
        "playback_path": str(media_file.playback_path),
    }
    handed: list[tuple[int, dict[str, Any]]] = []

    def storage(file_id, artifacts):
        handed.append((file_id, dict(artifacts)))
        return True

    monkeypatch.setattr(fcs, "delete_file_storage_artifacts", storage)
    monkeypatch.setattr(fcs, "_cleanup_opensearch_for_file", lambda target, file_uuid: [])
    monkeypatch.setattr(fcs, "_refresh_surviving_centroids", lambda db, owner_id, plan: [])
    file_id = int(media_file.id)

    result = fcs.purge_media_file(db_session, media_file)

    assert result["deleted"] is True, result
    assert handed == [(file_id, expected)]
