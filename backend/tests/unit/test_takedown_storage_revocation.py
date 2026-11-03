"""Takedown <-> presigned-URL-revocation tag wiring (issue #907), no live MinIO.

``quarantine_file``/``release_file`` tag and untag every object of the file through
``media_revocation``. These tests run that real code against
``fixtures.fake_tagging_store`` (real DB via the savepoint ``db_session`` fixture, same
pattern as ``tests/test_takedown_quarantine.py``) — the falsifiable live-MinIO round trip is
``tests/integration/test_presign_revocation.py``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import MagicMock

import pytest
from fixtures.fake_tagging_store import FakeTaggingStore
from fixtures.fake_tagging_store import install
from fixtures.fake_tagging_store import s3_error

from app.auth.audit import AuditOutcome
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services import media_revocation
from app.services.takedown_service import TakedownActor
from app.services.takedown_service import quarantine_file
from app.services.takedown_service import release_file

PUT_TAGS = "s3:PutObjectTagging"


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


@pytest.fixture()
def audits(monkeypatch):
    fake = MagicMock()
    events: list[dict] = []
    fake.log.side_effect = lambda **kw: events.append(kw)
    monkeypatch.setattr("app.services.takedown_notices.audit_logger", fake)
    return events


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(media_revocation, "UNTAG_BACKOFF_SECONDS", 0)


def _store(monkeypatch, file: MediaFile, **kw) -> FakeTaggingStore:
    keys = [k for k in (file.storage_path, file.thumbnail_path) if k]
    return install(monkeypatch, FakeTaggingStore(objects=keys, **kw), native_s3=False)


def _quarantine(db, admin, file) -> MediaFile:
    return quarantine_file(
        db,
        file,
        actor=TakedownActor.platform(admin),
        reason="dmca",
        legal_hold=False,
        operator_action_reason="operator action (test)",
    )


class TestQuarantineTagsBothObjects:
    def test_quarantine_tags_storage_path_and_thumbnail(self, world, monkeypatch):
        db, admin, file = world
        store = _store(monkeypatch, file)

        _quarantine(db, admin, file)

        assert store.quarantined(file.storage_path)
        assert store.quarantined(file.thumbnail_path)
        assert file.presign_revoked is True
        assert file.media_revocation.complete is True

    def test_release_untags_storage_path_and_thumbnail(self, world, monkeypatch):
        db, admin, file = world
        store = _store(monkeypatch, file)
        _quarantine(db, admin, file)

        release_file(db, file, actor=TakedownActor.platform(admin), clear_legal_hold=False)

        assert not store.quarantined(file.storage_path)
        assert not store.quarantined(file.thumbnail_path)
        assert file.presign_tag_cleared is True


class TestQuarantineTaggerFailureIsLoud:
    def test_a_storage_outage_does_not_propagate_and_audits_partial(
        self, world, monkeypatch, audits
    ):
        db, admin, file = world
        store = _store(monkeypatch, file)

        def _unreachable(*_a, **_kw):
            raise ConnectionError("storage unreachable")

        monkeypatch.setattr(store, "get_object_tags", _unreachable)

        # Must not raise: the takedown stands even when storage is down.
        _quarantine(db, admin, file)

        assert file.is_quarantined is True
        assert file.presign_revoked is False
        assert audits, "quarantine_file must still audit even when tagging fails"
        assert audits[0]["details"]["presign_revoked"] is False
        assert audits[0]["outcome"] == AuditOutcome.PARTIAL

    def test_a_refused_tag_write_is_audited_with_the_missing_permission(
        self, world, monkeypatch, audits
    ):
        db, admin, file = world
        _store(monkeypatch, file, deny=[PUT_TAGS])

        _quarantine(db, admin, file)

        event = audits[0]
        assert event["outcome"] == AuditOutcome.PARTIAL
        assert event["error_code"] == media_revocation.REASON_TAG_WRITE_FAILED
        assert event["details"]["media_revocation"] == {
            "complete": False,
            "reason": "tag_write_failed",
            "enforcement": "presign_identity",
            "missing_permissions": [PUT_TAGS],
            "failed_objects": ["original", "thumbnail"],
        }

    def test_a_clean_revocation_audits_success(self, world, monkeypatch, audits):
        db, admin, file = world
        _store(monkeypatch, file)

        _quarantine(db, admin, file)

        assert audits[0]["outcome"] == AuditOutcome.SUCCESS
        assert audits[0]["details"]["media_revocation"]["complete"] is True


class TestReleaseUntagRetries:
    def test_retries_three_times_and_logs_error_on_persistent_failure(
        self, world, caplog, monkeypatch, audits
    ):
        db, admin, file = world
        # Isolate to ONE object so the retry count is exactly UNTAG_RETRIES, not doubled by
        # a thumbnail_path also being retried.
        file.thumbnail_path = None
        db.commit()
        store = _store(monkeypatch, file)
        _quarantine(db, admin, file)
        store.deny.add(PUT_TAGS)
        store.calls.clear()

        with caplog.at_level("ERROR", logger="app.services.media_revocation"):
            release_file(db, file, actor=TakedownActor.platform(admin), clear_legal_hold=False)

        assert [a for a, _ in store.calls].count(PUT_TAGS) == media_revocation.UNTAG_RETRIES == 3
        error_records = [
            r
            for r in caplog.records
            if r.levelname == "ERROR" and r.name == "app.services.media_revocation"
        ]
        assert error_records, "a persistent untag failure must log at ERROR, not WARNING"
        assert PUT_TAGS in error_records[0].getMessage()
        assert file.presign_tag_cleared is False
        assert file.is_quarantined is False
        release_event = audits[-1]
        assert release_event["outcome"] == AuditOutcome.PARTIAL
        assert release_event["error_code"] == media_revocation.REASON_TAG_CLEAR_FAILED

    def test_succeeds_on_a_later_attempt_without_exhausting_retries(self, world, monkeypatch):
        db, admin, file = world
        file.thumbnail_path = None
        db.commit()
        store = _store(monkeypatch, file)
        _quarantine(db, admin, file)
        real_set = store.set_object_tags
        attempts = {"n": 0}

        def _fails_then_succeeds(bucket_name, object_name, tags, version_id=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise s3_error("SlowDown", object_name)
            return real_set(bucket_name, object_name, tags)

        monkeypatch.setattr(store, "set_object_tags", _fails_then_succeeds)

        release_file(db, file, actor=TakedownActor.platform(admin), clear_legal_hold=False)

        assert attempts["n"] == 2
        assert file.presign_tag_cleared is True
        assert not store.quarantined(file.storage_path)
