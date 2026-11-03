"""Automatic recovery resets a file only through ``reset_file_for_retry`` (#1276, #872).

The two recovery paths that clear a transcript before a retry (``reset_abandoned_files`` at
startup, ``recover_stuck_downloading_files`` in the health check) used to delete
``TranscriptSegment`` rows with their own query, which asked neither whether the file is under
a legal hold nor whether its media could regenerate the transcript. Both now go through the one
guarded reset, so a held transcript, or one whose media is gone, survives recovery and the file
is left in the state recovery found it in.

``schedule_file_retry`` opens its own ``SessionLocal()``, which cannot see rows inside this
suite's savepoint, so it is replaced by a recorder: the assertion is on whether recovery asked
for a retry, not on the dispatch itself.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.services.task_recovery_service import TaskRecoveryService


@pytest.fixture
def service(monkeypatch) -> tuple[TaskRecoveryService, list[int]]:
    scheduled: list[int] = []
    svc = TaskRecoveryService()

    def _record(file_id: int, countdown: int | None = None) -> bool:
        scheduled.append(file_id)
        return True

    monkeypatch.setattr(svc, "schedule_file_retry", _record)
    return svc, scheduled


@pytest.fixture(autouse=True)
def _media_present_and_no_dispatch(monkeypatch):
    """Media resolves and the summary-plane prune is not published, unless a test says otherwise."""
    monkeypatch.setattr("app.utils.task_utils.media_object_is_resolvable", lambda mf: True)
    monkeypatch.setattr("app.tasks.search_indexing_task.index_file_summary.delay", lambda *a: None)


def _file_with_segments(db, user, status: FileStatus, *, held: bool, count: int = 3) -> MediaFile:
    media_file = MediaFile(
        user_id=user.id,
        filename=f"f-{uuid.uuid4().hex[:8]}.wav",
        storage_path=f"user_{user.id}/{uuid.uuid4().hex[:8]}.wav",
        file_size=1024,
        content_type="audio/wav",
        status=status,
        legal_hold=held,
        upload_time=datetime.now(UTC),
    )
    db.add(media_file)
    db.commit()
    for i in range(count):
        db.add(
            TranscriptSegment(
                media_file_id=media_file.id,
                start_time=float(i),
                end_time=float(i) + 1.0,
                text=f"segment {i}",
            )
        )
    db.commit()
    db.refresh(media_file)
    return media_file


def _segment_count(db, media_file: MediaFile) -> int:
    db.expire_all()
    count: int = (
        db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == media_file.id).count()
    )
    return count


def _status(db, media_file: MediaFile) -> FileStatus:
    db.expire_all()
    status: FileStatus = db.get(MediaFile, media_file.id).status
    return status


def test_a_held_abandoned_file_keeps_its_segments(service, db_session, normal_user):
    svc, _ = service
    media_file = _file_with_segments(db_session, normal_user, FileStatus.PROCESSING, held=True)

    stats = svc.reset_abandoned_files(db_session, [media_file])

    assert stats == {"files_reset": 0, "files_completed": 0}
    assert _segment_count(db_session, media_file) == 3
    assert _status(db_session, media_file) == FileStatus.PROCESSING


def test_an_unheld_abandoned_file_is_still_reset(service, db_session, normal_user):
    svc, _ = service
    media_file = _file_with_segments(db_session, normal_user, FileStatus.PROCESSING, held=False)

    stats = svc.reset_abandoned_files(db_session, [media_file])

    assert stats == {"files_reset": 1, "files_completed": 0}
    assert _segment_count(db_session, media_file) == 0
    assert _status(db_session, media_file) == FileStatus.PENDING


def test_an_abandoned_file_whose_media_is_gone_keeps_its_segments(
    service, db_session, normal_user, monkeypatch
):
    monkeypatch.setattr("app.utils.task_utils.media_object_is_resolvable", lambda mf: False)
    svc, _ = service
    media_file = _file_with_segments(db_session, normal_user, FileStatus.PROCESSING, held=False)

    stats = svc.reset_abandoned_files(db_session, [media_file])

    assert stats == {"files_reset": 0, "files_completed": 0}
    assert _segment_count(db_session, media_file) == 3
    assert _status(db_session, media_file) == FileStatus.PROCESSING


def test_a_held_stuck_download_keeps_its_segments_and_is_not_retried(
    service, db_session, normal_user
):
    svc, scheduled = service
    media_file = _file_with_segments(
        db_session, normal_user, FileStatus.DOWNLOADING, held=True, count=2
    )

    stats = svc.recover_stuck_downloading_files(db_session, [media_file])

    assert stats == {"files_recovered": 0, "tasks_retried": 0}
    assert scheduled == []
    assert _segment_count(db_session, media_file) == 2
    assert _status(db_session, media_file) == FileStatus.DOWNLOADING


def test_an_unheld_stuck_download_is_still_reset_and_retried(service, db_session, normal_user):
    svc, scheduled = service
    media_file = _file_with_segments(
        db_session, normal_user, FileStatus.DOWNLOADING, held=False, count=2
    )

    stats = svc.recover_stuck_downloading_files(db_session, [media_file])

    assert stats == {"files_recovered": 1, "tasks_retried": 1}
    assert scheduled == [media_file.id]
    assert _segment_count(db_session, media_file) == 0
    assert _status(db_session, media_file) == FileStatus.QUEUED
