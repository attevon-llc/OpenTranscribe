"""Recovery decisions of ``services/task_recovery_service.py`` (1,087 LOC, previously untested).

Like its detection sibling, this module had **no reference anywhere in
``tests/``** — and it is the half that *writes*. It deletes transcript segments,
flips files between PENDING/PROCESSING/COMPLETED/ERROR, and resets Celery task
rows. Every one of those actions runs only during an incident, so a wrong branch
here is invisible until it has already destroyed a transcript or wedged a file in
a retry loop.

What is pinned:

* the ``_update_file_based_on_tasks`` decision tree, including the rule that a
  completed task **plus transcript segments** is what "completed" means (a
  completed *download* task alone is not);
* ``reset_abandoned_files``: segments are deleted **only** when transcription did
  not finish, and preserved when it did;
* the terminal-marking recoveries (unrecoverable PENDING downloads, timed-out LLM
  tasks, orphaned tasks);
* the false-positive reset and its two-attempt loop guard.

Deliberately NOT covered (see the report): anything reached through
``schedule_file_retry``, which opens its **own** ``SessionLocal()`` and therefore
cannot see rows that live inside this suite's savepoint — so
``recover_stuck_task``, ``recover_user_files``,
``recover_stuck_files_without_celery_tasks``, ``recover_oom_error_files`` and
``recover_retriable_error_files`` would all exercise a permanently-failing
dispatch rather than their real behaviour.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.models.media import TranscriptSegment
from app.services.task_recovery_service import TaskRecoveryService

NOW = datetime.now(UTC)

FALSE_POSITIVE_MESSAGE = "Task recovered after being stuck in processing"


@pytest.fixture
def service() -> TaskRecoveryService:
    return TaskRecoveryService()


def _file(db, user, **kwargs) -> MediaFile:
    """A MediaFile with the NOT NULL columns filled and everything else overridable."""
    defaults = {
        "user_id": user.id,
        "filename": f"f-{uuid.uuid4().hex[:8]}.wav",
        "storage_path": f"user_{user.id}/{uuid.uuid4().hex[:8]}.wav",
        "file_size": 1024,
        "content_type": "audio/wav",
        "status": FileStatus.PROCESSING,
    }
    media_file = MediaFile(**{**defaults, **kwargs})
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _task(db, user, media_file, **kwargs) -> Task:
    defaults = {
        "id": f"task-{uuid.uuid4()}",
        "user_id": user.id,
        "media_file_id": media_file.id,
        "task_type": "transcription",
        "status": "pending",
    }
    task = Task(**{**defaults, **kwargs})
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _segments(db, media_file, count: int = 2) -> None:
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


def _segment_count(db, media_file) -> int:
    count: int = (
        db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == media_file.id).count()
    )
    return count


# =============================================================================
# fix_inconsistent_media_file — the decision tree
# =============================================================================
def test_completed_tasks_only_mean_completed_when_segments_exist(service, db_session, normal_user):
    """A completed task with no transcript is a partial run, not a success.

    The 3-stage pipeline means a *download* task can complete while transcription
    never ran. Catches the ``has_segments`` conjunction being dropped: such a file
    would be shown to the user as COMPLETED with an empty transcript and would
    never be retried, because nothing downstream re-examines a COMPLETED file.
    The with-segments file is the control — it must still reach COMPLETED.
    """
    with_transcript = _file(db_session, normal_user)
    _task(db_session, normal_user, with_transcript, status="completed")
    _segments(db_session, with_transcript)

    without_transcript = _file(db_session, normal_user)
    _task(db_session, normal_user, without_transcript, status="completed")

    assert service.fix_inconsistent_media_file(db_session, with_transcript) is True
    assert service.fix_inconsistent_media_file(db_session, without_transcript) is True
    db_session.refresh(with_transcript)
    db_session.refresh(without_transcript)
    assert with_transcript.status == FileStatus.COMPLETED
    assert without_transcript.status == FileStatus.ERROR


def test_a_failed_task_with_no_completed_sibling_marks_the_file_error(
    service, db_session, normal_user
):
    """Nothing active and a failure on record is a terminal ERROR.

    Catches the failed branch being removed: the file would stay PROCESSING with
    no live task, so the gallery shows a permanent spinner and no retry path ever
    triggers.
    """
    media_file = _file(db_session, normal_user)
    _task(db_session, normal_user, media_file, status="failed")

    service.fix_inconsistent_media_file(db_session, media_file)
    db_session.refresh(media_file)
    assert media_file.status == FileStatus.ERROR


def test_a_pending_file_with_a_running_task_is_promoted_to_processing(
    service, db_session, normal_user
):
    """The file status must catch up with a task that is already running.

    Catches the promotion branch being dropped: a file whose worker picked it up
    but whose status write was lost stays PENDING, so the periodic sweep keeps
    re-dispatching it and the same audio is transcribed repeatedly.
    """
    media_file = _file(db_session, normal_user, status=FileStatus.PENDING)
    _task(db_session, normal_user, media_file, status="in_progress")

    service.fix_inconsistent_media_file(db_session, media_file)
    db_session.refresh(media_file)
    assert media_file.status == FileStatus.PROCESSING


def test_a_processing_file_with_no_tasks_at_all_becomes_error(service, db_session, normal_user):
    """No task row means nothing is or ever was running for this file."""
    media_file = _file(db_session, normal_user)

    assert service.fix_inconsistent_media_file(db_session, media_file) is True
    db_session.refresh(media_file)
    assert media_file.status == FileStatus.ERROR


# =============================================================================
# reset_abandoned_files — the destructive branch
# =============================================================================
def test_abandoned_file_with_a_finished_transcription_keeps_its_segments(
    service, db_session, normal_user
):
    """A finished transcript is adopted, never re-run.

    This is the highest-consequence branch in the module. Catches the
    ``_is_transcription_complete`` check being removed or inverted: an abandoned
    file that had actually finished would have its segments **deleted** and the
    whole GPU transcription redone — losing any speaker labels and manual
    transcript edits the user had already applied.
    """
    media_file = _file(db_session, normal_user, completed_at=NOW - timedelta(minutes=30))
    _segments(db_session, media_file, count=3)

    stats = service.reset_abandoned_files(db_session, [media_file])
    db_session.refresh(media_file)
    assert stats == {"files_reset": 0, "files_completed": 1}
    assert media_file.status == FileStatus.COMPLETED
    assert _segment_count(db_session, media_file) == 3


def test_abandoned_file_with_an_unfinished_transcription_is_reset_and_cleaned(
    service, db_session, normal_user
):
    """A partial transcript is deleted before the retry, or the retry duplicates it.

    ``completed_at`` is unset here, which is the real shape of a worker that died
    part-way through writing segments. Catches the cleanup being dropped: the
    retry appends a second copy of every segment, and the transcript view then
    renders each line twice with no way for the user to tell which is which.
    """
    media_file = _file(db_session, normal_user)
    _segments(db_session, media_file, count=3)

    stats = service.reset_abandoned_files(db_session, [media_file])
    db_session.refresh(media_file)
    assert stats == {"files_reset": 1, "files_completed": 0}
    assert media_file.status == FileStatus.PENDING
    assert _segment_count(db_session, media_file) == 0


# =============================================================================
# Terminal marking
# =============================================================================
def test_unrecoverable_pending_downloads_are_marked_error(service, db_session, normal_user):
    """A download that can never succeed leaves PENDING for ERROR.

    Catches the write being lost: the file would sit in PENDING forever, be
    re-detected every cycle, and the user would see "queued" for a video that no
    longer exists.
    """
    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.PENDING,
        file_size=0,
        storage_path="",
        last_error_message="This is a private video",
    )

    stats = service.recover_stuck_pending_download_files(db_session, [media_file])
    db_session.refresh(media_file)
    assert stats == {"files_marked_error": 1}
    assert media_file.status == FileStatus.ERROR


def test_stuck_llm_tasks_are_failed_with_a_timeout_message(service, db_session, normal_user):
    """The recorded reason is what makes a timeout distinguishable from a crash.

    Catches the task being left ``in_progress`` (the LLM detector would return it
    on every cycle forever) or failed with a generic message, which is what an
    operator reads in the tasks grid when deciding whether to re-run it.
    """
    media_file = _file(db_session, normal_user)
    task = _task(
        db_session,
        normal_user,
        media_file,
        task_type="summarization",
        status="in_progress",
    )

    stats = service.recover_stuck_llm_tasks(db_session, [task])
    db_session.refresh(task)
    assert stats == {"tasks_marked_failed": 1}
    assert task.status == "failed"
    assert task.error_message == "Task timeout - stuck in progress for > 6 hours"
    assert task.completed_at is not None


def test_orphaned_tasks_are_all_failed_and_counted(service, db_session, normal_user):
    """Every task in the batch is marked, and the count reflects the batch.

    Catches a ``break`` or an early return leaving later tasks pending after a
    restart: those files would look busy to the whole pipeline, so nothing would
    retry them and nothing would report them as failed.
    """
    media_file = _file(db_session, normal_user)
    first = _task(db_session, normal_user, media_file, status="in_progress")
    second = _task(db_session, normal_user, media_file, status="pending")

    recovered = service.recover_orphaned_tasks(db_session, [first, second])
    db_session.refresh(first)
    db_session.refresh(second)
    assert recovered == 2
    assert {first.status, second.status} == {"failed"}
    assert first.error_message == "Task interrupted by system restart"
    assert second.completed_at is not None


# =============================================================================
# False-positive resets and the loop guard
# =============================================================================
def test_false_positive_reset_returns_the_task_to_pending_and_stamps_attempt_one(
    service, db_session, normal_user
):
    """A reset clears the completion stamp and records that this was attempt 1.

    Catches ``completed_at`` not being cleared (the task would read as finished
    while pending, and the tasks grid would show a completed task with no result)
    and the attempt marker not being written, which is the only state the loop
    guard below has to read.
    """
    media_file = _file(db_session, normal_user)
    task = _task(
        db_session,
        normal_user,
        media_file,
        status="failed",
        error_message=FALSE_POSITIVE_MESSAGE,
        completed_at=NOW - timedelta(minutes=5),
    )

    stats = service.recover_false_positive_failed_tasks(db_session, [task])
    db_session.refresh(task)
    assert stats == {"tasks_reset": 1}
    assert task.status == "pending"
    assert task.error_message == "recovery_attempt_1_reset"
    assert task.completed_at is None


def test_a_task_already_reset_twice_is_permanently_failed_instead(service, db_session, normal_user):
    """Two resets is the cap; the third attempt is refused.

    Catches the loop guard being removed or its threshold slipping: the task would
    cycle failed → pending → failed indefinitely, re-dispatching real LLM or GPU
    work every cycle for a file that will never succeed. The attempt-1 test above
    is the control that proves the guard is not simply refusing everything.
    """
    media_file = _file(db_session, normal_user)
    task = _task(
        db_session,
        normal_user,
        media_file,
        status="failed",
        error_message="recovery_attempt_2_reset",
    )

    stats = service.recover_false_positive_failed_tasks(db_session, [task])
    db_session.refresh(task)
    assert stats == {"tasks_reset": 0}
    assert task.status == "failed"
    assert task.error_message == "Permanently failed after 2 recovery attempts"


# =============================================================================
# recover_oom_error_files — the ceiling it logs must be the ceiling it enforces
# =============================================================================
def test_oom_exhaustion_log_reports_the_configured_ceiling_not_the_orm_default(
    service, db_session, normal_user, caplog
):
    """The "exhausted" log line must name the admin-configured ceiling.

    ``MediaFile.max_retries`` is a column nothing ever writes (A3) — it always
    reads the ORM default of 3 regardless of what an admin has actually
    configured via SystemSettings. ``should_retry_file`` (the gate itself)
    already reads the real setting; this pins that the *log message* an
    operator reads to diagnose an exhausted retry loop agrees with it, instead
    of silently reporting "max_retries: 3" no matter what the admin set.
    """
    import logging

    from app.services import system_settings_service

    system_settings_service.update_retry_config(db_session, max_retries=9)

    # retry_count 9 >= configured ceiling 9 -> exhausted. MediaFile.max_retries
    # keeps its ORM default of 3, which is exactly the stale value this test
    # must not see quoted back.
    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        retry_count=9,
    )
    assert media_file.max_retries == 3

    with caplog.at_level(logging.WARNING):
        stats = service.recover_oom_error_files(db_session, [media_file])

    assert stats["files_exhausted"] == 1
    warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("max_retries: 9" in msg for msg in warning_messages)
    assert not any("max_retries: 3" in msg for msg in warning_messages)
