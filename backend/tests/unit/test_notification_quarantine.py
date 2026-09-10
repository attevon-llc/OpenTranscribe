"""Celery/WebSocket notification plane must be quarantine-aware (issue #817, item 8).

``notification_service.send_task_notification`` is the single funnel 10+ task files call
through, and it auto-attaches ``filename``/``content_type``/``file_size`` for any ``file_id``
it is given. Before this fix that funnel had no idea a file was quarantined, so a
``file_updated``/``summarization_status`` toast could still name a taken-down file for a
non-admin recipient — the exact disclosure the read-surface quarantine gate
(``exclude_quarantined``/``is_hidden_for``) exists to prevent, reached through a different
door.

The load-bearing test in this file is ``test_the_dmca_takedown_notice_is_still_delivered``
(and its release counterpart): the new suppression check must NOT swallow the owner's §512(g)
takedown/release notice, which is the owner's only surface for learning about a takedown at
all (the file itself 404s for them). ``_notify_owner_takedown``/``_notify_owner_release`` pass
the file's identity through ``extra`` rather than the ``file_id`` kwarg specifically so this
new check is never consulted for them — verified directly in the plan and pinned here.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.notification_service import send_task_notification
from app.services.takedown_service import is_notification_suppressed
from app.services.takedown_service import quarantine_file
from app.services.takedown_service import release_file


def _mk_user(db, *, admin: bool = False) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"{'admin' if admin else 'user'}_notify_{uid}@example.com",
        full_name="Notification quarantine test user",
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
    """Stand-in for ``session_scope()`` that hands out the test's savepoint session."""
    yield db


@pytest.fixture
def bridge_session(monkeypatch, db_session):
    """``is_notification_suppressed`` opens its OWN session — bridge it to the savepoint."""
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _yield_session(db_session))
    return db_session


@pytest.fixture
def sent(monkeypatch):
    """Capture ``send_ws_event`` calls at the notification funnel's own module."""
    calls: list[dict] = []

    def fake_send(user_id, event_type, data):
        calls.append({"user_id": user_id, "event_type": event_type, "data": data})
        return True

    monkeypatch.setattr("app.services.notification_service.send_ws_event", fake_send)
    return calls


class TestSendTaskNotificationSuppression:
    def test_notification_for_quarantined_file_is_suppressed(self, bridge_session, sent):
        """A non-admin recipient gets nothing — not even a metadata-free frame — and
        the call reports failure so a caller can tell the send did not happen."""
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        result = send_task_notification(
            owner.id, "file_updated", file_id=file.id, extra={"thumbnail_url": "x"}
        )

        assert result is False
        assert sent == []

    def test_clean_file_notification_delivered(self, bridge_session, sent):
        """Control: a never-quarantined file's notification is unaffected."""
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        result = send_task_notification(owner.id, "file_updated", file_id=file.id)

        assert result is True
        assert len(sent) == 1
        assert sent[0]["user_id"] == owner.id
        assert sent[0]["event_type"] == "file_updated"

    def test_admin_recipient_still_gets_it(self, bridge_session, sent):
        """Admins keep review visibility everywhere else in the quarantine plane;
        the notification funnel must not be the one place that changes."""
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        file = _mk_file(db, owner=owner, quarantined=True)

        result = send_task_notification(admin.id, "file_updated", file_id=file.id)

        assert result is True
        assert len(sent) == 1
        assert sent[0]["user_id"] == admin.id


class TestOwnerNoticesAreNeverSuppressed:
    """The load-bearing controls: the DMCA notices must survive this change untouched."""

    def test_the_dmca_takedown_notice_is_still_delivered(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        file = _mk_file(db, owner=owner, quarantined=False)

        quarantine_file(db, file, admin=admin, reason="DMCA notice #817")

        assert len(sent) == 1, "the owner's takedown notice must still be delivered"
        assert sent[0]["user_id"] == owner.id
        assert sent[0]["event_type"] == "file_takedown"

    def test_the_release_notice_is_still_delivered(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        file = _mk_file(db, owner=owner, quarantined=False)

        quarantine_file(db, file, admin=admin, reason="disputed")
        sent.clear()  # only the release notice is under test here

        release_file(db, file, admin=admin)

        assert len(sent) == 1, "the owner's release notice must still be delivered"
        assert sent[0]["user_id"] == owner.id
        assert sent[0]["event_type"] == "file_takedown_released"


class TestIsNotificationSuppressedFailsClosed:
    def test_suppression_fails_closed_on_a_db_error(self, monkeypatch):
        """A DB error while checking quarantine must suppress, not leak."""

        def boom():
            raise RuntimeError("db unreachable")

        monkeypatch.setattr("app.db.session_utils.session_scope", boom)

        assert is_notification_suppressed(file_id=1, recipient_user_id=1) is True
