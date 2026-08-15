"""``cleanup.orphan_upload_sweeper`` — deleting abandoned uploads without deleting live ones.

Two defects, both destructive: the MinIO object was deleted *before* the row that
points at it, and nothing exempted an upload still in flight. The browser
multipart path accepts objects up to 15 GB and holds its ``MediaFile`` at PENDING
for the whole transfer, so a fixed 30-minute window deleted a running upload's
object and row out from under it.

The task FUNCTION BODY runs against the savepoint-rolled-back ``db_session``;
``minio_service.delete_file`` is stubbed, so no object is ever really removed.
"""

import contextlib
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.services import minio_service
from app.tasks import cleanup

pytestmark = pytest.mark.xdist_group("orphan_upload_sweeper")

_GIGABYTE = 1024**3


@pytest.fixture
def sweeper_env(db_session, normal_user, monkeypatch):
    """Bind the sweeper to the test transaction and take other PENDING rows out of scope.

    The sweeper is deployment-wide, so any other stale PENDING row in the dev
    database would make its counters ambiguous. Their ``upload_time`` is moved to
    now (undone with the savepoint) rather than their status, so nothing about
    them changes except that this pass no longer considers them.

    Returns:
        A callable creating a PENDING file of a given size and age; returns its id.
    """
    monkeypatch.setattr(
        cleanup, "session_scope", lambda: contextlib.nullcontext(db_session), raising=True
    )
    db_session.query(MediaFile).filter(MediaFile.status == FileStatus.PENDING).update(
        {"upload_time": datetime.now(UTC)}, synchronize_session=False
    )

    def _make_pending(*, file_size: int, age_minutes: int) -> int:
        pending = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=normal_user.id,
            filename=f"sweeper-{uuid_pkg.uuid4().hex[:8]}.mp4",
            content_type="video/mp4",
            file_size=file_size,
            storage_path=f"sweeper-test/{uuid_pkg.uuid4().hex}",
            status=FileStatus.PENDING,
            upload_time=datetime.now(UTC) - timedelta(minutes=age_minutes),
        )
        db_session.add(pending)
        db_session.flush()
        return int(pending.id)

    return _make_pending


def test_large_upload_still_in_flight_is_left_alone(db_session, sweeper_env, monkeypatch):
    """Defect: a 15 GB upload 45 minutes in was swept as abandoned.

    Nothing exempted an upload in progress: the row sits at PENDING until
    ``/files/complete``, and the sweeper deleted both it and the parts already
    stored, so the user's upload failed at the very end with no explanation.
    """
    monkeypatch.setattr(minio_service, "delete_file", lambda path: None, raising=True)
    file_id = sweeper_env(file_size=15 * _GIGABYTE, age_minutes=45)

    result = cleanup.orphan_upload_sweeper.run(max_age_minutes=30)

    assert result["deleted_rows"] == 0
    assert result["skipped_in_progress"] == 1
    assert db_session.query(MediaFile).filter(MediaFile.id == file_id).first() is not None


def test_genuinely_abandoned_upload_is_swept(db_session, sweeper_env, monkeypatch):
    """Control: a small, long-dead PENDING row must still be cleaned up.

    Without this the grace window above could be satisfied by a sweeper that
    never deletes anything, and abandoned rows would accumulate forever.
    """
    monkeypatch.setattr(minio_service, "delete_file", lambda path: None, raising=True)
    file_id = sweeper_env(file_size=1024, age_minutes=180)

    result = cleanup.orphan_upload_sweeper.run(max_age_minutes=30)

    assert result["deleted_rows"] == 1
    assert db_session.query(MediaFile).filter(MediaFile.id == file_id).first() is None


def test_row_is_gone_before_the_object_is_deleted(db_session, sweeper_env, monkeypatch):
    """Defect: the object was deleted first, then the row.

    If the row delete then failed — or the upload completed in the gap — the user
    kept a file they could see and never open, pointing at storage that no longer
    existed. The row must be gone (and committed) before its object is touched.
    """
    file_id = sweeper_env(file_size=1024, age_minutes=180)
    observed: dict[str, object] = {}

    def _record_delete(path: str) -> None:
        observed["row_present"] = (
            db_session.query(MediaFile).filter(MediaFile.id == file_id).first() is not None
        )

    monkeypatch.setattr(minio_service, "delete_file", _record_delete, raising=True)

    result = cleanup.orphan_upload_sweeper.run(max_age_minutes=30)

    assert observed["row_present"] is False
    assert result["deleted_objects"] == 1


def test_grace_window_scales_with_size_and_is_capped():
    """Defect: the window was a flat 30 minutes regardless of upload size.

    Pins the derived window: 15 GB gets hours, an unknown size gets the caller's
    floor, and an absurd size cannot make a row permanently un-sweepable.
    """
    assert cleanup._upload_grace_minutes(15 * _GIGABYTE, 30) == 1920
    assert cleanup._upload_grace_minutes(None, 30) == 30
    assert cleanup._upload_grace_minutes(1024, 30) == 30
    assert cleanup._upload_grace_minutes(10**15, 30) == cleanup._MAX_UPLOAD_GRACE_MINUTES
