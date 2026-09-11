"""``send_ws_event_for_file`` and its takedown_service predicates (issue #908).

Covers the wrapper's own contract (selector arity, suppression routing, the
fail-closed batch drop) plus the two new predicates it is built on:
``is_notification_suppressed_for_uuid`` and ``filter_suppressed_file_uuids``.
Follows the session-bridging fixture pattern from ``test_notification_quarantine.py``
so ``is_notification_suppressed*``'s own ``session_scope()`` sees the test's
uncommitted rows.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.takedown_service import filter_suppressed_file_uuids
from app.services.takedown_service import is_notification_suppressed_for_uuid
from app.utils.websocket_notify import send_ws_event_for_file


def _mk_user(db, *, admin: bool = False) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"{'admin' if admin else 'user'}_wswrap_{uid}@example.com",
        full_name="WS wrapper test user",
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
    """``is_notification_suppressed*`` open their OWN session — bridge it to the savepoint."""
    monkeypatch.setattr("app.db.session_utils.session_scope", lambda: _yield_session(db_session))
    return db_session


@pytest.fixture
def sent(monkeypatch):
    """Capture ``send_ws_event`` calls at the module the wrapper actually calls."""
    calls: list[dict] = []

    def fake_send(user_id, event_type, data):
        calls.append({"user_id": user_id, "event_type": event_type, "data": data})
        return True

    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event", fake_send)
    return calls


class TestSelectorArity:
    def test_no_selector_raises_type_error(self):
        with pytest.raises(TypeError):
            send_ws_event_for_file(1, "file_updated", {})

    def test_two_selectors_raises_type_error(self):
        with pytest.raises(TypeError):
            send_ws_event_for_file(1, "file_updated", {}, file_id=1, file_uuid="x")

    def test_all_three_selectors_raises_type_error(self):
        with pytest.raises(TypeError):
            send_ws_event_for_file(
                1, "file_updated", {}, file_id=1, file_uuid="x", file_uuids=["y"]
            )


class TestFileIdSelector:
    def test_quarantined_by_id_is_suppressed(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        result = send_ws_event_for_file(
            owner.id, "file_updated", {"file_id": str(file.uuid)}, file_id=file.id
        )

        assert result is False
        assert sent == []

    def test_clean_file_by_id_is_delivered(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        result = send_ws_event_for_file(
            owner.id, "file_updated", {"file_id": str(file.uuid)}, file_id=file.id
        )

        assert result is True
        assert len(sent) == 1
        assert sent[0]["user_id"] == owner.id

    def test_admin_recipient_by_id_still_delivered(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        file = _mk_file(db, owner=owner, quarantined=True)

        result = send_ws_event_for_file(
            admin.id, "file_updated", {"file_id": str(file.uuid)}, file_id=file.id
        )

        assert result is True
        assert len(sent) == 1


class TestFileUuidSelector:
    def test_quarantined_by_uuid_is_suppressed(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        result = send_ws_event_for_file(
            owner.id, "file_updated", {"file_id": str(file.uuid)}, file_uuid=file.uuid
        )

        assert result is False
        assert sent == []

    def test_clean_file_by_uuid_is_delivered(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        result = send_ws_event_for_file(
            owner.id, "file_updated", {"file_id": str(file.uuid)}, file_uuid=file.uuid
        )

        assert result is True
        assert len(sent) == 1

    def test_clean_file_by_string_uuid_is_delivered(self, bridge_session, sent):
        """The selector also accepts a plain string, not just a UUID instance."""
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        result = send_ws_event_for_file(
            owner.id, "file_updated", {"file_id": str(file.uuid)}, file_uuid=str(file.uuid)
        )

        assert result is True
        assert len(sent) == 1

    def test_admin_recipient_by_uuid_still_delivered(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        file = _mk_file(db, owner=owner, quarantined=True)

        result = send_ws_event_for_file(
            admin.id, "file_updated", {"file_id": str(file.uuid)}, file_uuid=file.uuid
        )

        assert result is True

    def test_unknown_uuid_is_suppressed(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)

        result = send_ws_event_for_file(
            owner.id, "file_updated", {}, file_uuid=str(uuid_pkg.uuid4())
        )

        assert result is False
        assert sent == []

    def test_uuid_selector_fails_closed_on_db_error(self, monkeypatch, sent):
        def boom():
            raise RuntimeError("db unreachable")

        monkeypatch.setattr("app.db.session_utils.session_scope", boom)

        result = send_ws_event_for_file(1, "file_updated", {}, file_uuid=str(uuid_pkg.uuid4()))

        assert result is False
        assert sent == []

    def test_malformed_uuid_is_suppressed(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)

        result = send_ws_event_for_file(owner.id, "file_updated", {}, file_uuid="not-a-uuid")

        assert result is False
        assert sent == []


class TestFileUuidsListSelector:
    def test_all_visible_is_delivered(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        f1 = _mk_file(db, owner=owner, quarantined=False)
        f2 = _mk_file(db, owner=owner, quarantined=False)

        result = send_ws_event_for_file(
            owner.id,
            "speaker_rename_propagation",
            {"file_uuids": [str(f1.uuid), str(f2.uuid)]},
            file_uuids=[f1.uuid, f2.uuid],
        )

        assert result is True
        assert len(sent) == 1

    def test_one_suppressed_member_drops_the_whole_event(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        f1 = _mk_file(db, owner=owner, quarantined=False)
        f2 = _mk_file(db, owner=owner, quarantined=True)

        result = send_ws_event_for_file(
            owner.id,
            "speaker_rename_propagation",
            {"file_uuids": [str(f1.uuid), str(f2.uuid)]},
            file_uuids=[f1.uuid, f2.uuid],
        )

        assert result is False
        assert sent == []

    def test_admin_recipient_gets_the_full_list(self, bridge_session, sent):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        f1 = _mk_file(db, owner=owner, quarantined=False)
        f2 = _mk_file(db, owner=owner, quarantined=True)

        result = send_ws_event_for_file(
            admin.id,
            "speaker_rename_propagation",
            {"file_uuids": [str(f1.uuid), str(f2.uuid)]},
            file_uuids=[f1.uuid, f2.uuid],
        )

        assert result is True
        assert len(sent) == 1


class TestFilterSuppressedFileUuids:
    def test_preserves_input_order_and_drops_quarantined(self, bridge_session):
        db = bridge_session
        owner = _mk_user(db)
        f1 = _mk_file(db, owner=owner, quarantined=False)
        f2 = _mk_file(db, owner=owner, quarantined=True)
        f3 = _mk_file(db, owner=owner, quarantined=False)

        visible = filter_suppressed_file_uuids([f1.uuid, f2.uuid, f3.uuid], owner.id)

        assert visible == [str(f1.uuid), str(f3.uuid)]

    def test_returns_all_for_admin(self, bridge_session):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        f1 = _mk_file(db, owner=owner, quarantined=False)
        f2 = _mk_file(db, owner=owner, quarantined=True)

        visible = filter_suppressed_file_uuids([f1.uuid, f2.uuid], admin.id)

        assert visible == [str(f1.uuid), str(f2.uuid)]

    def test_unknown_uuid_is_dropped(self, bridge_session):
        db = bridge_session
        owner = _mk_user(db)
        f1 = _mk_file(db, owner=owner, quarantined=False)
        unknown = str(uuid_pkg.uuid4())

        visible = filter_suppressed_file_uuids([f1.uuid, unknown], owner.id)

        assert visible == [str(f1.uuid)]

    def test_empty_input_returns_empty(self, bridge_session):
        db = bridge_session
        owner = _mk_user(db)

        assert filter_suppressed_file_uuids([], owner.id) == []

    def test_fails_closed_on_db_error(self, monkeypatch):
        def boom():
            raise RuntimeError("db unreachable")

        monkeypatch.setattr("app.db.session_utils.session_scope", boom)

        assert filter_suppressed_file_uuids([str(uuid_pkg.uuid4())], 1) == []


class TestIsNotificationSuppressedForUuid:
    def test_fails_closed_on_db_error(self, monkeypatch):
        def boom():
            raise RuntimeError("db unreachable")

        monkeypatch.setattr("app.db.session_utils.session_scope", boom)

        assert is_notification_suppressed_for_uuid(str(uuid_pkg.uuid4()), 1) is True

    def test_malformed_uuid_is_suppressed(self):
        assert is_notification_suppressed_for_uuid("not-a-uuid", 1) is True

    def test_clean_file_is_not_suppressed(self, bridge_session):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=False)

        assert is_notification_suppressed_for_uuid(file.uuid, owner.id) is False

    def test_quarantined_file_is_suppressed_for_non_admin(self, bridge_session):
        db = bridge_session
        owner = _mk_user(db)
        file = _mk_file(db, owner=owner, quarantined=True)

        assert is_notification_suppressed_for_uuid(file.uuid, owner.id) is True

    def test_quarantined_file_is_not_suppressed_for_admin(self, bridge_session):
        db = bridge_session
        owner = _mk_user(db)
        admin = _mk_user(db, admin=True)
        file = _mk_file(db, owner=owner, quarantined=True)

        assert is_notification_suppressed_for_uuid(file.uuid, admin.id) is False
