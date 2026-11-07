"""Retention and duplicate detection beside the trash (issue #1101).

* Retention expiry keeps HARD-deleting at N days (owner decision OD-1 B): it never sets
  ``deleted_at``, never takes a file already in the trash (that would shorten the window the
  person who deleted it was promised), and its preview applies the same guard.
* A trashed file is not a duplicate: re-uploading or re-importing it creates a new file. A
  watch-source path imported once is not re-imported after a trash, exactly as after a
  hard delete.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.enums import FileStatus
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


# --------------------------------------------------------------------------- retention (OD-1 B)


@pytest.fixture
def retention_sessions(monkeypatch, db_session):
    @contextlib.contextmanager
    def _same_session():
        yield db_session
        db_session.commit()

    monkeypatch.setattr("app.tasks.cleanup.session_scope", _same_session)


def _expired(db_session, cutoff_days=30):
    from app.tasks.cleanup import _select_expired_files

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=cutoff_days)
    return _select_expired_files(
        db_session, {"delete_error_files": False}, cutoff, cutoff, lambda _org: None
    )


def test_retention_still_hard_deletes_and_never_uses_the_trash(
    normal_user, db_session, destroyed, retention_sessions
):
    from app.tasks.cleanup import _purge_expired_files

    media_file = _file(db_session, normal_user, completed_days_ago=60)
    expired = _expired(db_session)
    assert (media_file.id, str(media_file.uuid)) in expired

    deleted, failed = _purge_expired_files([(media_file.id, str(media_file.uuid))])

    assert (deleted, failed) == (1, 0)
    assert not _exists(db_session, media_file), "retention moved the file to the trash"
    assert f"storage:{media_file.storage_path}" in destroyed
    assert f"opensearch:{media_file.uuid}" in destroyed


def test_retention_never_takes_a_file_already_in_the_trash(normal_user, db_session):
    """It would shorten the window the person who deleted the file was promised."""
    media_file = _file(db_session, normal_user, completed_days_ago=60)
    _trash(db_session, normal_user, media_file)

    assert (media_file.id, str(media_file.uuid)) not in _expired(db_session)


def test_the_retention_preview_agrees_with_the_sweep(normal_user, db_session):
    from app.api.endpoints.admin_settings import _get_retention_eligible_files

    trashed = _file(db_session, normal_user, completed_days_ago=60)
    held = _file(db_session, normal_user, completed_days_ago=60)
    live = _file(db_session, normal_user, completed_days_ago=60)
    _trash(db_session, normal_user, trashed)
    held.legal_hold = True
    db_session.commit()

    previewed = {f.id for f in _get_retention_eligible_files(db_session, 30, False)}

    assert live.id in previewed
    assert trashed.id not in previewed
    assert held.id not in previewed


# --------------------------------------------------------------------------- duplicates


def test_a_trashed_file_is_not_a_duplicate_of_a_re_upload(normal_user, db_session):
    from app.utils.file_hash import check_duplicate_by_fingerprint

    media_file = _file(db_session, normal_user)
    media_file.file_hash = "ab" * 32
    db_session.commit()
    assert (
        check_duplicate_by_fingerprint(
            db_session, media_file.file_hash, normal_user.id, organization_id=None
        )
        is not None
    ), "control: the live file must count as a duplicate"

    _trash(db_session, normal_user, media_file)

    assert (
        check_duplicate_by_fingerprint(
            db_session, media_file.file_hash, normal_user.id, organization_id=None
        )
        is None
    )


def test_a_trashed_url_import_is_not_a_duplicate(normal_user, db_session):
    from app.api.endpoints.files.url_video_import import _check_duplicate_video

    url = f"https://example.com/watch?v={uuid.uuid4().hex[:11]}"
    media_file = _file(db_session, normal_user)
    media_file.source_url = url
    db_session.commit()
    with pytest.raises(HTTPException) as live_dup:
        _check_duplicate_video(db_session, normal_user.id, "vid", url, None)
    assert live_dup.value.status_code == 409, "control: the live copy is a duplicate"

    _trash(db_session, normal_user, media_file)

    _check_duplicate_video(db_session, normal_user.id, "vid", url, None)


def test_a_watch_source_path_is_not_re_imported_after_a_trash_as_after_a_hard_delete(
    normal_user, db_session
):
    """Parity with today: an imported path's tracking row is terminal either way."""
    from app.models.watch_source import WatchSource
    from app.models.watch_source import WatchSourceFile
    from app.services.watch_sources.base import RemoteFileInfo
    from app.services.watch_sources.processing import _get_or_create_tracking_row

    source = WatchSource(
        uuid=uuid.uuid4(),
        user_id=normal_user.id,
        name=f"trash-parity-{uuid.uuid4().hex[:6]}",
        source_type="local",
        local_path="watch/trash-parity",
    )
    db_session.add(source)
    db_session.commit()
    trashed = _file(db_session, normal_user)
    for path, media_file_id in (("a.wav", trashed.id), ("b.wav", None)):
        db_session.add(
            WatchSourceFile(
                uuid=uuid.uuid4(),
                watch_source_id=source.id,
                remote_path=path,
                filename=path,
                status="imported",
                media_file_id=media_file_id,
            )
        )
    db_session.commit()
    _trash(db_session, normal_user, trashed)

    for path in ("a.wav", "b.wav"):
        info = RemoteFileInfo(path=path, name=path, size=1, modified_time=datetime.now(UTC))
        assert _get_or_create_tracking_row(db_session, source, info) is None, path
