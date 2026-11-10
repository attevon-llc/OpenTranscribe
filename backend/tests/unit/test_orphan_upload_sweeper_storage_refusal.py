"""The orphan-upload sweeper keeps going when one object delete is refused.

``delete_file`` raises ``StorageError`` / ``StorageUnavailableError``; the row delete has already
committed, so a refused object delete is logged and the sweep moves on to the next stale upload.
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.core.enums import FileStatus
from app.core.exceptions import StorageUnavailableError
from app.models.media import MediaFile
from app.services import minio_service
from app.tasks import cleanup


def test_sweeper_continues_past_a_refused_object_delete(db_session, normal_user, monkeypatch):
    monkeypatch.setattr(
        cleanup, "session_scope", lambda: contextlib.nullcontext(db_session), raising=True
    )
    db_session.query(MediaFile).filter(MediaFile.status == FileStatus.PENDING).update(
        {"upload_time": datetime.now(UTC)}, synchronize_session=False
    )
    ids = []
    for _ in range(2):
        row = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename=f"stale-{uuid_pkg.uuid4().hex[:8]}.mp4",
            content_type="video/mp4",
            file_size=1024,
            storage_path=f"sweeper-test/{uuid_pkg.uuid4().hex}",
            status=FileStatus.PENDING,
            upload_time=datetime.now(UTC) - timedelta(minutes=180),
        )
        db_session.add(row)
        db_session.flush()
        ids.append(int(row.id))
    monkeypatch.setattr(minio_service, "object_exists_and_size", lambda path: None)

    def _refuse(path: str) -> None:
        raise StorageUnavailableError("Storage unavailable while deleting a file: ConnectionError")

    monkeypatch.setattr(minio_service, "delete_file", _refuse)

    result = cleanup.orphan_upload_sweeper.run(max_age_minutes=30)

    assert result["deleted_rows"] == 2
    assert result["errors"] == 0
    assert db_session.query(MediaFile).filter(MediaFile.id.in_(ids)).count() == 0
