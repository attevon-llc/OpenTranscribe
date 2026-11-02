"""thumbnail_migration must not delete a .jpg another media row references (issue #1357).

A URL import's thumbnail is keyed by the source video, so two rows can name one object.
The migration re-rendered one row to WebP and then deleted the shared .jpg, breaking the
other row's thumbnail.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest

from app.models.media import MediaFile
from app.tasks import thumbnail_migration as tm


def _row(db_session, user, *, thumbnail_path: str, source_url: str | None) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=user.id,
        filename=f"thumbmig_{file_uuid[:8]}.mp4",
        storage_path=f"media/test/{file_uuid}.mp4",
        thumbnail_path=thumbnail_path,
        source_url=source_url,
        content_type="video/mp4",
        file_size=10,
        status="completed",
    )
    db_session.add(media_file)
    db_session.commit()
    return media_file


@pytest.fixture
def migration_env(monkeypatch, db_session):
    deleted: list[str] = []
    monkeypatch.setattr(tm, "get_file_url", lambda *_a, **_k: "http://x")
    monkeypatch.setattr(tm, "generate_thumbnail_from_url", lambda _u: b"webp-bytes")
    monkeypatch.setattr(tm, "upload_file", lambda **_k: None)
    monkeypatch.setattr(tm, "delete_file", lambda path: deleted.append(path))

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(tm, "session_scope", _scope)
    return deleted


def test_shared_jpg_is_kept_when_another_row_references_it(db_session, normal_user, migration_env):
    shared = f"user_1/youtube_{uuid.uuid4().hex[:8]}/thumbnail.jpg"
    mine = _row(db_session, normal_user, thumbnail_path=shared, source_url=None)
    _row(db_session, normal_user, thumbnail_path=shared, source_url="https://example.com/v")

    outcome, new_path = tm._migrate_single_thumbnail(
        {"id": mine.id, "thumbnail_path": shared, "storage_path": mine.storage_path}
    )

    assert outcome == "migrated"
    assert new_path == shared[:-4] + ".webp"
    assert migration_env == [], "the shared .jpg was deleted while another row still names it"


def test_unshared_jpg_is_still_deleted(db_session, normal_user, migration_env):
    own = f"user_1/file_{uuid.uuid4().hex[:8]}/thumbnail.jpg"
    mine = _row(db_session, normal_user, thumbnail_path=own, source_url=None)

    outcome, _ = tm._migrate_single_thumbnail(
        {"id": mine.id, "thumbnail_path": own, "storage_path": mine.storage_path}
    )

    assert outcome == "migrated"
    assert migration_env == [own]
