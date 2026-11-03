"""backfill_missing_thumbnails regenerates thumbnails cleared by v456, once per file per run."""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg

from app.models.media import MediaFile
from app.tasks import thumbnail_migration


def _file(db, owner, *, content_type="video/mp4", status="completed", thumb=None):
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"bf_{file_uuid.hex[:8]}",
        storage_path=f"user_{owner.id}/bf_{file_uuid}/original",
        content_type=content_type,
        file_size=1,
        status=status,
        thumbnail_path=thumb,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_only_completed_video_files_without_a_thumbnail_are_dispatched(
    db_session, normal_user, monkeypatch
):
    wanted = _file(db_session, normal_user)
    _file(db_session, normal_user, thumb=f"user_{normal_user.id}/t/{uuid_pkg.uuid4().hex}.webp")
    _file(db_session, normal_user, content_type="audio/mpeg")
    _file(db_session, normal_user, status="processing")

    @contextlib.contextmanager
    def scope():
        yield db_session

    dispatched: list[tuple] = []
    monkeypatch.setattr(thumbnail_migration, "session_scope", scope)
    monkeypatch.setattr(
        "app.tasks.thumbnail.generate_thumbnail_task.delay",
        lambda *args: dispatched.append(args),
    )

    monkeypatch.setattr(thumbnail_migration.backfill_missing_thumbnails, "delay", lambda **kw: None)

    result = thumbnail_migration.backfill_missing_thumbnails.run(
        after_id=int(wanted.id) - 1, batch_size=1
    )

    assert dispatched == [(wanted.id, normal_user.id, wanted.storage_path)]
    assert result["dispatched"] == 1
    assert result["last_id"] == wanted.id
