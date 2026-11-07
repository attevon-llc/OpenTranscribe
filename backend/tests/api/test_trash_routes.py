"""Delete moves a file to the trash; restore, delete forever and empty trash (issue #1101).

Before #1101 ``DELETE /api/files/{uuid}`` destroyed the file at once: object storage, every
OpenSearch document and the row, with nothing to undo. These tests pin the new contract
through the real routes: the same route and status code now leave the row (marked trashed)
and every stored copy intact, the file disappears from read surfaces, and only the trash
routes reach it. The destroy itself is ``tests/api/test_trash_purge.py``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.enums import FileStatus
from app.models.audit_outbox import AuditOutbox
from app.models.media import MediaFile

BULK = "/api/files/management/bulk-action"


@pytest.fixture(autouse=True)
def external_destroy(monkeypatch):
    """Record every call into the storage / OpenSearch destroy; a trash must make none."""
    calls: list[str] = []

    def _storage(file_id, *_args, **_kwargs):
        calls.append(f"storage:{file_id}")
        return True

    def _opensearch(plan, file_uuid):
        calls.append(f"opensearch:{file_uuid}")
        return []

    monkeypatch.setattr("app.services.file_cleanup_service.delete_file_storage_artifacts", _storage)
    monkeypatch.setattr(
        "app.services.file_cleanup_service._cleanup_opensearch_for_file", _opensearch
    )
    return calls


def _file(db, owner, *, organization_id=None, status=FileStatus.COMPLETED) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"trash_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=2048,
        status=status,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _row_or_none(db: Session, media_file: MediaFile) -> MediaFile | None:
    db.expire_all()
    return db.get(MediaFile, media_file.id)


def _row(db: Session, media_file: MediaFile) -> MediaFile:
    row = _row_or_none(db, media_file)
    assert row is not None, "the row is gone: something destroyed the file"
    return row


def _make_overdue(db, media_file) -> None:
    db.execute(
        text("UPDATE media_file SET purge_after = now() - interval '1 minute' WHERE id = :i"),
        {"i": media_file.id},
    )
    db.commit()


def _outbox_events(db, media_file) -> list[str]:
    db.expire_all()
    rows = db.query(AuditOutbox).all()
    return sorted(
        r.event_type for r in rows if r.payload.get("resource_uuid") == str(media_file.uuid)
    )


# --------------------------------------------------------------------------- delete = trash


def test_delete_moves_the_file_to_the_trash_and_keeps_every_copy(
    client, user_token_headers, normal_user, db_session, external_destroy
):
    media_file = _file(db_session, normal_user)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == 204
    row = _row(db_session, media_file)
    assert row.deleted_at is not None
    assert row.deleted_by_user_id == normal_user.id
    assert row.trash_origin == "user"
    assert row.purge_after is not None
    window = row.purge_after - row.deleted_at
    assert timedelta(days=30) - timedelta(seconds=5) < window <= timedelta(days=30)
    assert row.status == FileStatus.COMPLETED, "trash must not touch the processing status"
    assert external_destroy == [], "a trash touched object storage or OpenSearch"


def test_a_trashed_file_is_gone_from_the_detail_and_the_gallery(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    assert (
        client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers).status_code
        == 204
    )

    assert (
        client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers).status_code == 404
    )
    listed = client.get("/api/files?page_size=100", headers=user_token_headers).json()["items"]
    assert str(media_file.uuid) not in {item["uuid"] for item in listed}


def test_a_file_with_a_task_in_flight_is_trashed_not_refused(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """The old 409 FILE_NOT_SAFE_TO_DELETE is gone: trash is reversible, so the task is
    cancelled and the file trashed."""
    media_file = _file(db_session, normal_user, status=FileStatus.PROCESSING)
    media_file.active_task_id = "task-in-flight"
    db_session.commit()
    cancelled: list[int] = []

    def _cancel(_db: Session, file_id: int) -> bool:
        cancelled.append(file_id)
        return True

    monkeypatch.setattr("app.utils.task_cancel.cancel_active_task", _cancel)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == 204, response.text
    assert _row(db_session, media_file).deleted_at is not None
    assert cancelled == [media_file.id]


def test_trashing_a_held_file_is_refused_409(client, user_token_headers, normal_user, db_session):
    media_file = _file(db_session, normal_user)
    media_file.legal_hold = True
    db_session.commit()

    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "FILE_UNDER_LEGAL_HOLD"
    assert _row(db_session, media_file).deleted_at is None
    assert "file.trash" not in _outbox_events(db_session, media_file)


def test_force_delete_trashes_too(
    client, admin_token_headers, normal_user, db_session, external_destroy
):
    media_file = _file(db_session, normal_user)

    response = client.delete(f"/api/files/{media_file.uuid}/force", headers=admin_token_headers)

    assert response.status_code == 200, response.text
    assert _row(db_session, media_file).deleted_at is not None
    assert external_destroy == []


def test_bulk_delete_trashes_each_file(client, user_token_headers, normal_user, db_session):
    files = [_file(db_session, normal_user) for _ in range(2)]

    response = client.post(
        BULK,
        headers=user_token_headers,
        json={"action": "delete", "file_uuids": [str(f.uuid) for f in files]},
    )

    assert response.status_code == 200
    assert [r["success"] for r in response.json()] == [True, True]
    for f in files:
        assert _row(db_session, f).deleted_at is not None


def test_a_pending_upload_is_still_destroyed_at_once(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    """An aborted upload has nothing to recover: the cancel path keeps its hard purge."""
    monkeypatch.setattr(
        "app.services.multipart_upload.abort_uploads_for_object", lambda *_a, **_k: None
    )
    media_file = _file(db_session, normal_user, status=FileStatus.PENDING)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == 204
    assert _row_or_none(db_session, media_file) is None


# --------------------------------------------------------------------------- restore


def test_restore_within_the_window_brings_the_file_back(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    response = client.post(f"/api/files/{media_file.uuid}/restore", headers=user_token_headers)

    assert response.status_code == 200, response.text
    assert response.json()["uuid"] == str(media_file.uuid)
    row = _row(db_session, media_file)
    assert (row.deleted_at, row.purge_after, row.trash_origin, row.deleted_by_user_id) == (
        None,
        None,
        None,
        None,
    )
    assert (
        client.get(f"/api/files/{media_file.uuid}", headers=user_token_headers).status_code == 200
    )


def test_restore_after_the_purge_is_due_is_refused_409(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    _make_overdue(db_session, media_file)

    response = client.post(f"/api/files/{media_file.uuid}/restore", headers=user_token_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "NOT_RESTORABLE"
    assert _row(db_session, media_file).deleted_at is not None


def test_a_hold_freezes_an_overdue_trashed_file_and_it_stays_restorable(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    _make_overdue(db_session, media_file)
    db_session.execute(
        text("UPDATE media_file SET legal_hold = true WHERE id = :i"), {"i": media_file.id}
    )
    db_session.commit()

    response = client.post(f"/api/files/{media_file.uuid}/restore", headers=user_token_headers)

    assert response.status_code == 200, response.text
    assert _row(db_session, media_file).deleted_at is None


def test_restore_of_a_live_file_is_404(client, user_token_headers, normal_user, db_session):
    media_file = _file(db_session, normal_user)

    response = client.post(f"/api/files/{media_file.uuid}/restore", headers=user_token_headers)

    assert response.status_code == 404


# --------------------------------------------------------------------------- delete forever


def test_delete_forever_makes_the_file_due_and_answers_202(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    response = client.delete(f"/api/files/{media_file.uuid}/purge", headers=user_token_headers)

    assert response.status_code == 202, response.text
    assert response.json() == {"queued": 1}
    db_session.expire_all()
    due = db_session.execute(
        text("SELECT purge_after <= clock_timestamp() FROM media_file WHERE id = :i"),
        {"i": media_file.id},
    ).scalar_one()
    assert due is True
    restore = client.post(f"/api/files/{media_file.uuid}/restore", headers=user_token_headers)
    assert restore.status_code == 409, "a file asked to be deleted forever came back"


def test_delete_forever_of_a_live_file_is_409_not_in_trash(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)

    response = client.delete(f"/api/files/{media_file.uuid}/purge", headers=user_token_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "FILE_NOT_IN_TRASH"


def test_delete_forever_of_a_held_trashed_file_is_409(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    db_session.execute(
        text("UPDATE media_file SET legal_hold = true WHERE id = :i"), {"i": media_file.id}
    )
    db_session.commit()

    response = client.delete(f"/api/files/{media_file.uuid}/purge", headers=user_token_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "FILE_UNDER_LEGAL_HOLD"


def test_empty_trash_marks_every_purgeable_file_and_keeps_held_ones(
    client, user_token_headers, normal_user, db_session
):
    free = [_file(db_session, normal_user) for _ in range(2)]
    held = _file(db_session, normal_user)
    for f in [*free, held]:
        client.delete(f"/api/files/{f.uuid}", headers=user_token_headers)
    db_session.execute(
        text("UPDATE media_file SET legal_hold = true WHERE id = :i"), {"i": held.id}
    )
    db_session.commit()

    response = client.post("/api/files/trash/empty", headers=user_token_headers)

    assert response.status_code == 202, response.text
    assert response.json() == {"queued": 2}
    db_session.expire_all()
    held_due = db_session.execute(
        text("SELECT purge_after <= clock_timestamp() FROM media_file WHERE id = :i"),
        {"i": held.id},
    ).scalar_one()
    assert held_due is False


# --------------------------------------------------------------------------- list


def test_the_trash_lists_the_callers_trashed_files_only(
    client, user_token_headers, other_user_auth_headers, normal_user, other_user, db_session
):
    mine = _file(db_session, normal_user)
    theirs = _file(db_session, other_user)
    live = _file(db_session, normal_user)
    client.delete(f"/api/files/{mine.uuid}", headers=user_token_headers)
    client.delete(f"/api/files/{theirs.uuid}", headers=other_user_auth_headers)

    response = client.get("/api/files/trash", headers=user_token_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    uuids = {item["uuid"] for item in body["items"]}
    assert str(mine.uuid) in uuids
    assert str(theirs.uuid) not in uuids
    assert str(live.uuid) not in uuids
    row = next(item for item in body["items"] if item["uuid"] == str(mine.uuid))
    assert row["trash_origin"] == "user"
    assert row["deleted_by_uuid"] == str(normal_user.uuid)
    assert (row["held"], row["can_restore"], row["can_purge"]) == (False, True, True)
    assert body["retention_days"] == 30


# --------------------------------------------------------------------------- audit


def test_trash_restore_and_purge_request_stage_their_audit_events(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr("app.services.audit_outbox.deliver_staged", lambda _db: None)
    media_file = _file(db_session, normal_user)

    client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    client.post(f"/api/files/{media_file.uuid}/restore", headers=user_token_headers)
    client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)
    client.delete(f"/api/files/{media_file.uuid}/purge", headers=user_token_headers)

    assert _outbox_events(db_session, media_file) == [
        "file.purge.requested",
        "file.restore",
        "file.trash",
        "file.trash",
    ]


def test_a_rolled_back_trash_leaves_no_audit_event_and_no_trash_state(
    normal_user, db_session, monkeypatch
):
    """The event is staged in the change's own transaction: no commit, no event."""
    from app.core.config import settings
    from app.services import trash_service

    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    media_file = _file(db_session, normal_user)

    # The test session's own rollback would undo the whole test transaction, so the trash's
    # transaction is a savepoint here; rolling it back is what a failed request does.
    attempt = db_session.begin_nested()
    with patch.object(db_session, "commit", side_effect=RuntimeError("commit failed")):
        with pytest.raises(RuntimeError):
            trash_service.trash_files(
                db_session, [media_file], actor=normal_user, request=None, bypass=None
            )
    attempt.rollback()

    assert _row(db_session, media_file).deleted_at is None
    assert _outbox_events(db_session, media_file) == []


def test_the_audit_details_carry_no_filename(normal_user, db_session, monkeypatch):
    from app.core.config import settings
    from app.services import trash_service

    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr("app.services.audit_outbox.deliver_staged", lambda _db: None)
    media_file = _file(db_session, normal_user)

    trash_service.trash_files(
        db_session, [media_file], actor=normal_user, request=None, bypass=None
    )

    db_session.expire_all()
    row = next(
        r
        for r in db_session.query(AuditOutbox).all()
        if r.payload.get("resource_uuid") == str(media_file.uuid)
    )
    assert row.payload["details"] == {"file_id": media_file.id, "origin": "user"}
    assert str(media_file.filename) not in str(row.payload)
