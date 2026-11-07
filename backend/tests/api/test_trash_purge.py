"""The trash's destroy (issue #1101).

``files.purge_trash`` (``services/trash_purge``) destroys a trashed file whose window has
ended: the row, its object-storage objects and its OpenSearch documents (through the
canonical ``purge_media_file``), and records ``file.purge``. A held or quarantined trashed
file is never purged, overdue or not. GDPR erasure does not wait for the window.

The time a purge runs is moved by writing ``purge_after`` into the past, not by freezing the
Python clock: every trash comparison uses the database clock.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.enums import FileStatus
from app.models.audit_outbox import AuditOutbox
from app.models.media import MediaFile
from app.services import trash_purge
from app.services import trash_service


@pytest.fixture
def destroyed(monkeypatch, db_session):
    """Record the external destroy, and run the purge's own sessions on the test session."""
    calls: list[str] = []

    def _storage(file_id, artifacts, *_args, **_kwargs):
        calls.append(f"storage:{artifacts.get('storage_path')}")
        return True

    def _opensearch(plan, file_uuid):
        calls.append(f"opensearch:{file_uuid}")
        return []

    monkeypatch.setattr("app.services.file_cleanup_service.delete_file_storage_artifacts", _storage)
    monkeypatch.setattr(
        "app.services.file_cleanup_service._cleanup_opensearch_for_file", _opensearch
    )

    @contextlib.contextmanager
    def _same_session():
        yield db_session
        db_session.commit()

    monkeypatch.setattr(trash_purge, "session_scope", _same_session)
    return calls


def _file(db, owner, *, status=FileStatus.COMPLETED, completed_days_ago=0) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"purge_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status=status,
        completed_at=datetime.now(UTC) - timedelta(days=completed_days_ago),
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _trash(db, owner, media_file, *, overdue=False) -> None:
    trash_service.trash_files(db, [media_file], actor=owner, request=None, bypass=None)
    if overdue:
        db.execute(
            text("UPDATE media_file SET purge_after = now() - interval '1 hour' WHERE id = :i"),
            {"i": media_file.id},
        )
        db.commit()


def _exists(db, media_file) -> bool:
    db.expire_all()
    return db.get(MediaFile, media_file.id) is not None


# --------------------------------------------------------------------------- the purge


def test_the_purge_destroys_a_due_trashed_file_everywhere(normal_user, db_session, destroyed):
    media_file = _file(db_session, normal_user)
    _trash(db_session, normal_user, media_file, overdue=True)

    summary = trash_purge.purge_due()

    assert (summary.purged, summary.failed) == (1, 0)
    assert not _exists(db_session, media_file)
    assert f"storage:{media_file.storage_path}" in destroyed
    assert f"opensearch:{media_file.uuid}" in destroyed


def test_the_purge_leaves_a_file_whose_window_is_still_open(normal_user, db_session, destroyed):
    media_file = _file(db_session, normal_user)
    _trash(db_session, normal_user, media_file)

    summary = trash_purge.purge_due()

    assert summary.selected == 0
    assert _exists(db_session, media_file)
    assert destroyed == []


def test_the_purge_never_touches_a_live_file(normal_user, db_session, destroyed):
    media_file = _file(db_session, normal_user)

    trash_purge.purge_due(file_ids=[media_file.id])

    assert _exists(db_session, media_file)
    assert destroyed == []


def test_a_held_trashed_file_is_never_purged(normal_user, db_session, destroyed):
    media_file = _file(db_session, normal_user)
    _trash(db_session, normal_user, media_file, overdue=True)
    db_session.execute(
        text("UPDATE media_file SET legal_hold = true WHERE id = :i"), {"i": media_file.id}
    )
    db_session.commit()

    summary = trash_purge.purge_due()

    assert summary.purged == 0
    assert _exists(db_session, media_file)
    assert destroyed == []


def test_a_quarantined_trashed_file_is_never_purged(normal_user, db_session, destroyed):
    media_file = _file(db_session, normal_user)
    _trash(db_session, normal_user, media_file, overdue=True)
    db_session.execute(
        text("UPDATE media_file SET is_quarantined = true WHERE id = :i"), {"i": media_file.id}
    )
    db_session.commit()

    summary = trash_purge.purge_due()

    assert summary.purged == 0
    assert _exists(db_session, media_file)
    assert destroyed == []


def test_a_hold_placed_between_selection_and_destroy_wins(normal_user, db_session, destroyed):
    """Phase 2 re-checks the predicate on the locked row; a hold that landed in between
    means the file is skipped, not destroyed."""
    media_file = _file(db_session, normal_user)
    _trash(db_session, normal_user, media_file, overdue=True)
    selected = trash_purge.select_due(10)
    assert [file_id for file_id, _ in selected] == [media_file.id]
    db_session.execute(
        text("UPDATE media_file SET legal_hold = true WHERE id = :i"), {"i": media_file.id}
    )
    db_session.commit()

    outcome, _ = trash_purge.purge_one(media_file.id, trigger="due")

    assert outcome == "skipped"
    assert _exists(db_session, media_file)


def test_running_the_purge_twice_is_harmless(normal_user, db_session, destroyed):
    media_file = _file(db_session, normal_user)
    _trash(db_session, normal_user, media_file, overdue=True)

    first = trash_purge.purge_due(file_ids=[media_file.id])
    second = trash_purge.purge_due(file_ids=[media_file.id])

    assert (first.purged, second.selected, second.failed) == (1, 0, 0)


def test_the_purge_records_file_purge_with_no_person_as_actor(
    normal_user, db_session, destroyed, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    monkeypatch.setattr("app.services.audit_outbox.deliver_staged", lambda _db: None)
    media_file = _file(db_session, normal_user)
    _trash(db_session, normal_user, media_file, overdue=True)

    trash_purge.purge_due()

    db_session.expire_all()
    events = [
        r.payload
        for r in db_session.query(AuditOutbox).all()
        if r.payload.get("resource_uuid") == str(media_file.uuid) and r.event_type == "file.purge"
    ]
    assert len(events) == 1
    assert events[0]["user_id"] is None
    assert events[0]["target_user_id"] == normal_user.id
    assert events[0]["details"] == {"file_id": media_file.id, "origin": "user", "trigger": "due"}


def test_the_beat_task_runs_the_purge(normal_user, db_session, destroyed):
    from app.tasks.trash import purge_trash

    media_file = _file(db_session, normal_user)
    _trash(db_session, normal_user, media_file, overdue=True)

    result = purge_trash.run()

    assert result["purged"] == 1
    assert not _exists(db_session, media_file)


# --------------------------------------------------------------------------- erasure wins


@pytest.mark.usefixtures("no_receipt_measurement", "no_erasure_extras")
def test_gdpr_erasure_destroys_a_trashed_file_at_once(db_session, monkeypatch):
    import app.services.account_purge as account_purge
    from app.core.security import get_password_hash
    from app.models.user import User
    from app.services.gdpr_erasure_service import erase_user

    monkeypatch.setattr(account_purge, "purge_account_external_copies", lambda _plan: [])
    subject = User(
        email=f"trash-erase-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=get_password_hash(uuid.uuid4().hex),
        full_name="Trash Erase",
        is_active=True,
        role="user",
    )
    db_session.add(subject)
    db_session.commit()
    media_file = _file(db_session, subject)
    _trash(db_session, subject, media_file)

    summary = erase_user(db_session, int(subject.id))

    assert summary["users_deleted"] == 1, summary
    assert not _exists(db_session, media_file), "erasure waited for the trash window"
