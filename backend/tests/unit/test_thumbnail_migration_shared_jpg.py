"""thumbnail_migration must not delete a .jpg another media row references (issue #1357).

Originally two URL-import rows could name one thumbnail object, and the migration deleted
the shared .jpg out from under the other row. v456 made that state unrepresentable
(``uq_media_file_thumbnail_path``), so the tests now pin the constraint and that a migration
only ever deletes its own row's object.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

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


def test_a_second_row_cannot_name_the_same_thumbnail_object(db_session, normal_user):
    """The state the old shared-.jpg guard defended against is no longer representable.

    Two rows naming one thumbnail object is what made the migration's delete break the other
    row. ``uq_media_file_thumbnail_path`` rejects it at write time, so the guard was removed
    from the migration and this pins the constraint it now relies on.
    """
    shared = f"user_1/youtube_{uuid.uuid4().hex[:8]}/thumbnail.jpg"
    _row(db_session, normal_user, thumbnail_path=shared, source_url=None)

    with pytest.raises(IntegrityError, match="uq_media_file_thumbnail_path"):
        _row(db_session, normal_user, thumbnail_path=shared, source_url="https://example.com/v")
    db_session.rollback()


def test_another_rows_jpg_is_kept_when_this_row_migrates(db_session, normal_user, migration_env):
    mine_path = f"user_1/file_{uuid.uuid4().hex[:8]}/thumbnail.jpg"
    other_path = f"user_1/file_{uuid.uuid4().hex[:8]}/thumbnail.jpg"
    mine = _row(db_session, normal_user, thumbnail_path=mine_path, source_url=None)
    _row(db_session, normal_user, thumbnail_path=other_path, source_url="https://example.com/v")

    outcome, new_path = tm._migrate_single_thumbnail(
        {"id": mine.id, "thumbnail_path": mine_path, "storage_path": mine.storage_path}
    )

    assert outcome == "migrated"
    assert new_path == mine_path[:-4] + ".webp"
    assert migration_env == [mine_path], "the migration must delete only its own row's .jpg"


def test_unshared_jpg_is_still_deleted(db_session, normal_user, migration_env):
    own = f"user_1/file_{uuid.uuid4().hex[:8]}/thumbnail.jpg"
    mine = _row(db_session, normal_user, thumbnail_path=own, source_url=None)

    outcome, _ = tm._migrate_single_thumbnail(
        {"id": mine.id, "thumbnail_path": own, "storage_path": mine.storage_path}
    )

    assert outcome == "migrated"
    assert migration_env == [own]
