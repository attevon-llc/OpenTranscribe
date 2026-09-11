"""Takedown <-> presigned-URL-revocation tag wiring (issue #907), no live MinIO.

``quarantine_file``/``release_file`` drive ``minio_service.set_object_quarantine_tag``
as a best-effort storage-plane side effect. These tests mock that function entirely
(real DB via the savepoint ``db_session`` fixture, same pattern as
``tests/test_takedown_quarantine.py``) — the falsifiable live-MinIO round trip is
``tests/integration/test_presign_revocation.py``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import patch

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services import takedown_service
from app.services.takedown_service import quarantine_file
from app.services.takedown_service import release_file

_TAG_TARGET = "app.services.minio_service.set_object_quarantine_tag"


def _mk_admin(db) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"admin_{uid}@example.com",
        full_name="Presign revocation test admin",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=True,
        role="super_admin",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_file(db, *, owner: User, with_thumbnail: bool = True) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=f"f_{str(fuuid)[:8]}.mp4",
        storage_path=f"media/{fuuid}/original.mp4",
        thumbnail_path=f"media/{fuuid}/thumb.webp" if with_thumbnail else None,
        content_type="video/mp4",
        file_size=1000,
        user_id=owner.id,
        status=FileStatus.COMPLETED,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


@pytest.fixture()
def world(db_session):
    db = db_session
    admin = _mk_admin(db)
    file = _mk_file(db, owner=admin)
    return db, admin, file


class TestQuarantineTagsBothObjects:
    def test_quarantine_tags_storage_path_and_thumbnail(self, world):
        db, admin, file = world
        calls: list[tuple[str, bool]] = []

        def _record(object_name, quarantined, **_kw):
            calls.append((object_name, quarantined))
            return True

        with patch(_TAG_TARGET, side_effect=_record):
            quarantine_file(db, file, admin=admin, reason="dmca", legal_hold=False)

        assert (file.storage_path, True) in calls
        assert (file.thumbnail_path, True) in calls
        assert file.presign_revoked is True

    def test_release_untags_storage_path_and_thumbnail(self, world):
        db, admin, file = world
        with patch(_TAG_TARGET, return_value=True):
            quarantine_file(db, file, admin=admin, reason="dmca", legal_hold=False)

        calls: list[tuple[str, bool]] = []

        def _record(object_name, quarantined, **_kw):
            calls.append((object_name, quarantined))
            return True

        with patch(_TAG_TARGET, side_effect=_record):
            release_file(db, file, admin=admin, clear_legal_hold=False)

        assert (file.storage_path, False) in calls
        assert (file.thumbnail_path, False) in calls
        assert file.presign_tag_cleared is True


class TestQuarantineTaggerFailureIsBestEffort:
    def test_tagger_exception_does_not_propagate_and_audit_records_false(self, world):
        db, admin, file = world
        audits: list[dict] = []

        with (
            patch(_TAG_TARGET, side_effect=RuntimeError("storage unreachable")),
            patch("app.services.takedown_service.audit_logger") as fake_audit,
        ):
            fake_audit.log.side_effect = lambda **kw: audits.append(kw)
            # Must not raise — the tagger call is wrapped in its own try/except.
            quarantine_file(db, file, admin=admin, reason="dmca", legal_hold=False)

        assert file.presign_revoked is False
        assert audits, "quarantine_file must still audit even when tagging raises"
        assert audits[0]["details"]["presign_revoked"] is False


class TestReleaseUntagRetries:
    def test_retries_three_times_and_logs_error_on_persistent_failure(
        self, world, caplog, monkeypatch
    ):
        db, admin, file = world
        # Isolate to ONE object so the retry count is exactly _PRESIGN_UNTAG_RETRIES,
        # not doubled by a thumbnail_path also being retried.
        file.thumbnail_path = None
        db.commit()

        with patch(_TAG_TARGET, return_value=True):
            quarantine_file(db, file, admin=admin, reason="dmca", legal_hold=False)

        monkeypatch.setattr(takedown_service, "_PRESIGN_UNTAG_BACKOFF_SECONDS", 0)
        call_count = 0

        def _always_fails(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            return False

        with (
            patch(_TAG_TARGET, side_effect=_always_fails),
            caplog.at_level("ERROR", logger="app.services.takedown_service"),
        ):
            release_file(db, file, admin=admin, clear_legal_hold=False)

        assert call_count == takedown_service._PRESIGN_UNTAG_RETRIES == 3
        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error_records, "a persistent untag failure must log at ERROR, not WARNING"
        assert file.presign_tag_cleared is False

    def test_succeeds_on_a_later_attempt_without_exhausting_retries(self, world, monkeypatch):
        db, admin, file = world
        file.thumbnail_path = None
        db.commit()

        with patch(_TAG_TARGET, return_value=True):
            quarantine_file(db, file, admin=admin, reason="dmca", legal_hold=False)

        monkeypatch.setattr(takedown_service, "_PRESIGN_UNTAG_BACKOFF_SECONDS", 0)
        attempts = {"n": 0}

        def _fails_then_succeeds(*_a, **_kw):
            attempts["n"] += 1
            return attempts["n"] >= 2

        with patch(_TAG_TARGET, side_effect=_fails_then_succeeds):
            release_file(db, file, admin=admin, clear_legal_hold=False)

        assert attempts["n"] == 2
        assert file.presign_tag_cleared is True
