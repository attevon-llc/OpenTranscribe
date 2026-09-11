"""Thumbnail task must not mint a presigned URL for a quarantined file (issue #817, item 8b).

``generate_thumbnail_task`` used to call ``get_file_url`` unconditionally after uploading the
derived thumbnail, regardless of whether the source file had been taken down in the meantime.
That is worse than an unsent notification: it signs a URL for a quarantined object even when
nothing goes on to use it. The fix checks quarantine BEFORE minting the URL, not just before
the notification that would have carried it.

The thumbnail upload itself and the ``thumbnail_path`` DB write are deliberately left alone —
the derived thumbnail object is not directly served (that route is already gated by a prior
fix), and skipping the write would leave a later-released file with no thumbnail at all.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.tasks.thumbnail import generate_thumbnail_task


def _mk_user(db) -> User:
    from app.core.security import get_password_hash

    uid = str(uuid_pkg.uuid4())[:8]
    user = User(
        email=f"user_thumb_{uid}@example.com",
        full_name="Thumbnail quarantine test user",
        hashed_password=get_password_hash("password123"),
        is_active=True,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_file(db, *, owner: User) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    f = MediaFile(
        uuid=fuuid,
        filename=f"f_{str(fuuid)[:8]}.mp4",
        storage_path=f"user_{owner.id}/{fuuid}.mp4",
        content_type="video/mp4",
        file_size=1000,
        user_id=owner.id,
        status=FileStatus.QUARANTINED,
        is_quarantined=True,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


@contextmanager
def _yield_session(db):
    yield db


@pytest.fixture
def thumbnail_pipeline_seams(monkeypatch, db_session):
    """Patch every seam the task touches BEFORE the notify block, real DB write included."""
    fake_minio = MagicMock()
    fake_minio.client.presigned_get_object.return_value = "http://minio/fake-presigned"
    monkeypatch.setattr("app.tasks.thumbnail.MinIOService", lambda: fake_minio)
    monkeypatch.setattr(
        "app.tasks.thumbnail.generate_thumbnail_from_url", lambda **_kw: b"fake-webp-bytes"
    )
    monkeypatch.setattr("app.tasks.thumbnail.upload_file", lambda **_kw: None)
    monkeypatch.setattr("app.tasks.thumbnail.session_scope", lambda: _yield_session(db_session))


def test_no_presigned_url_is_minted_for_a_quarantined_file(
    thumbnail_pipeline_seams, monkeypatch, db_session
):
    owner = _mk_user(db_session)
    file = _mk_file(db_session, owner=owner)

    get_file_url = MagicMock()
    monkeypatch.setattr("app.services.minio_service.get_file_url", get_file_url)
    # The quarantine check itself is covered by test_notification_quarantine.py; here we only
    # need it to report "suppressed" so the task's behaviour around that verdict is what's
    # under test.
    monkeypatch.setattr(
        "app.services.takedown_service.is_notification_suppressed", lambda *_a, **_kw: True
    )
    send_task_notification = MagicMock(return_value=False)
    monkeypatch.setattr(
        "app.services.notification_service.send_task_notification", send_task_notification
    )

    result = generate_thumbnail_task(file.id, owner.id, file.storage_path)

    assert result["success"] is True
    assert result["thumbnail_path"] is not None
    get_file_url.assert_not_called()
    send_task_notification.assert_not_called()
