"""``app/tasks/transcription/notifications.py`` must be quarantine-aware (issue #908).

This is the highest-value cluster in the sweep: ~40 upstream call sites route
through these three functions (``send_notification_via_redis`` /
``send_notification_with_retry`` / ``send_transcript_ready_notification`` /
``send_completion_notification``), so a leak here reaches every stage of the
pipeline. Three behaviors are pinned, each with a red-before/green-after shape
and a clean-file control:

  1. The suppression check runs BEFORE the file metadata load, so a
     quarantined file's filename is never read off the DB for this purpose.
  2. A suppressed ``send_notification_via_redis`` still invalidates the
     gallery cache — the deliberate exception, so the SPA's cached file list
     correctly drops the now-hidden file even though no notification fires.
  3. A suppression is never retried: ``send_notification_with_retry`` must
     recognize it after the first failed attempt and return immediately,
     rather than sleeping/retrying 3 times and logging an "after 3 attempts"
     error on every progress tick of a quarantined file.

Uses the session-bridging fixture pattern from ``test_notification_quarantine.py``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.tasks.transcription import notifications as notif


def _mk_user(db, *, admin: bool = False) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"{'admin' if admin else 'user'}_txnotify_{uid}@example.com",
        full_name="Transcription notification quarantine test user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=admin,
        role="super_admin" if admin else "user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_file(db, *, owner: User, quarantined: bool = False) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=f"f_{str(fuuid)[:8]}.mp4",
        storage_path="",
        content_type="video/mp4",
        file_size=1000,
        user_id=owner.id,
        status=FileStatus.COMPLETED,
        is_quarantined=quarantined,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def bridge_session(monkeypatch, db_session):
    """Bridge every session this module (and the suppression check it calls
    into) opens to the test's savepoint session.

    ``is_notification_suppressed`` opens its OWN session via a fresh
    ``from app.db.session_utils import session_scope`` each call, so patching
    that module attribute is enough for it. ``notifications.py`` itself binds
    ``session_scope`` at import time (``from app.db.session_utils import
    session_scope``), so its own reads (``get_file_metadata``, the
    ``file_updated`` block's ``MediaFile`` fetch) need the module-local name
    patched too — otherwise those open a genuinely separate connection that
    cannot see this test's uncommitted (savepoint-only) rows.
    """
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _yield_session(db_session))
    monkeypatch.setattr(notif, "session_scope", lambda: _yield_session(db_session))
    return db_session


@pytest.fixture
def sent(monkeypatch):
    """Capture the final, real ``send_ws_event`` calls (past the suppression check)."""
    calls: list[dict] = []

    def fake_send(user_id, event_type, data):
        calls.append({"user_id": user_id, "event_type": event_type, "data": data})
        return True

    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event", fake_send)
    return calls


@pytest.fixture
def invalidations(monkeypatch):
    """Capture ``redis_cache.invalidate_user_files`` calls."""
    calls: list[int] = []
    monkeypatch.setattr(
        "app.services.redis_cache_service.redis_cache.invalidate_user_files",
        lambda user_id: calls.append(user_id),
    )
    return calls


@pytest.fixture
def no_metadata_load(monkeypatch):
    """Fail loudly if get_file_metadata is ever called — proves the hoist."""

    def _boom(file_id):
        raise AssertionError(
            f"get_file_metadata({file_id}) was called — the suppression check must "
            "run BEFORE the metadata load"
        )

    monkeypatch.setattr(notif, "get_file_metadata", _boom)
    return None


class TestSendNotificationViaRedisSuppression:
    def test_quarantined_file_is_suppressed_without_reading_metadata(
        self, bridge_session, sent, invalidations, no_metadata_load
    ):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        result = notif.send_notification_via_redis(
            owner.id, file.id, FileStatus.PROCESSING, "Transcription started", progress=10
        )

        assert result is False
        assert sent == []

    def test_a_suppressed_notification_still_invalidates_the_gallery_cache(
        self, bridge_session, sent, invalidations
    ):
        """The deliberate exception: the SPA must still learn to re-fetch."""
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        result = notif.send_notification_via_redis(
            owner.id, file.id, FileStatus.PROCESSING, "Transcription started", progress=10
        )

        assert result is False
        assert invalidations == [owner.id]

    def test_clean_file_notification_is_delivered_and_invalidates(
        self, bridge_session, sent, invalidations
    ):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        result = notif.send_notification_via_redis(
            owner.id, file.id, FileStatus.PROCESSING, "Transcription started", progress=10
        )

        assert result is True
        assert len(sent) == 1
        assert sent[0]["event_type"] == "transcription_status"
        assert invalidations == [owner.id]

    def test_admin_recipient_still_gets_it(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        file = _mk_file(db, owner=owner, quarantined=True)

        result = notif.send_notification_via_redis(
            admin.id, file.id, FileStatus.PROCESSING, "Transcription started", progress=10
        )

        assert result is True
        assert len(sent) == 1


class TestSendNotificationWithRetryDoesNotRetryASuppression:
    def test_a_suppressed_notification_is_not_retried(
        self, bridge_session, sent, invalidations, monkeypatch
    ):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

        result = notif.send_notification_with_retry(
            owner.id, file.id, FileStatus.PROCESSING, "Transcription started", progress=10
        )

        assert result is False
        assert sent == [], "the raw send_ws_event must never be reached for a suppression"
        assert sleep_calls == [], "a suppression must not sleep between retry attempts"

    def test_a_genuine_redis_failure_still_retries_three_times(self, bridge_session, monkeypatch):
        """Control: a real transient failure (not a suppression) keeps its 3-attempt retry."""
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        attempts: list[int] = []
        sleep_calls: list[float] = []
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

        def _always_fails(*a, **kw):
            attempts.append(1)
            return False

        monkeypatch.setattr(notif, "send_notification_via_redis", _always_fails)

        result = notif.send_notification_with_retry(
            owner.id, file.id, FileStatus.PROCESSING, "Transcription started", progress=10
        )

        assert result is False
        assert len(attempts) == 3
        assert len(sleep_calls) == 2  # no sleep after the last attempt

    def test_a_clean_file_success_returns_true_on_first_attempt(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        result = notif.send_notification_with_retry(
            owner.id, file.id, FileStatus.PROCESSING, "Transcription started", progress=10
        )

        assert result is True
        assert len(sent) == 1


class TestSendTranscriptReadyNotification:
    def test_quarantined_file_is_suppressed_without_reading_metadata(
        self, bridge_session, sent, no_metadata_load
    ):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        notif.send_transcript_ready_notification(owner.id, file.id)

        assert sent == []

    def test_clean_file_is_delivered(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        notif.send_transcript_ready_notification(owner.id, file.id)

        assert len(sent) == 1
        assert sent[0]["event_type"] == "transcript_ready"

    def test_admin_recipient_still_gets_it(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        file = _mk_file(db, owner=owner, quarantined=True)

        notif.send_transcript_ready_notification(admin.id, file.id)

        assert len(sent) == 1


class TestSendCompletionNotification:
    def test_quarantined_file_file_updated_is_suppressed_without_reading_file_data(
        self, bridge_session, sent, invalidations, monkeypatch
    ):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        # send_notification_with_retry's own suppression path is covered above;
        # here it's stubbed so this test isolates the file_updated half.
        monkeypatch.setattr(notif, "send_notification_with_retry", lambda *a, **kw: False)

        # A call-counting session_scope proves the hoist directly: if the
        # suppression check ran BEFORE the `with session_scope() as db:` block
        # that assembles file_data, that block is never entered at all.
        scope_entries: list[int] = []

        @contextmanager
        def _counting_scope():
            scope_entries.append(1)
            yield db

        monkeypatch.setattr(notif, "session_scope", _counting_scope)

        notif.send_completion_notification(owner.id, file.id)

        assert sent == []
        assert scope_entries == [], (
            "the file_updated block's session_scope must not open at all for a "
            "suppressed (quarantined) file"
        )

    def test_clean_file_sends_both_notifications(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        notif.send_completion_notification(owner.id, file.id)

        event_types = {c["event_type"] for c in sent}
        assert "transcription_status" in event_types
        assert "file_updated" in event_types

    def test_admin_recipient_still_gets_the_file_updated_event(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        file = _mk_file(db, owner=owner, quarantined=True)

        notif.send_completion_notification(admin.id, file.id)

        event_types = {c["event_type"] for c in sent}
        assert "file_updated" in event_types
