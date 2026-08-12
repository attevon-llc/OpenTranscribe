"""NULL-column reads in the task-recovery plane (``task_utils`` + ``task_recovery_service``).

``MediaFile.retry_count`` and ``MediaFile.recovery_attempts`` are
``Mapped[int | None]`` whose ``0`` is a **Python-side** ``default=``, and
``MediaFile.upload_time`` / ``Task.created_at`` carry a ``server_default`` and no
NOT NULL. A ``default=`` runs only when the ORM builds the INSERT and a
``server_default`` runs only for an INSERT that *omits* the column — so any row
written by raw SQL naming the column, by a migration backfill, or by an explicit
``UPDATE`` holds NULL. ``None += 1``, ``int(None)`` and ``now - None`` all raise,
and ``assert col is not None`` neither prevents that nor survives ``python -O``.

Every recovery entry point below sits behind a broad ``except Exception`` that
*logs and continues*, so the failure has no symptom other than work silently not
happening: the file is never retried, the counter never advances, and the sweep
reports zero recoveries on a row it was built to fix. That is why these are
tested at the entry point rather than at the arithmetic.

**The NULL is written with an explicit ``UPDATE``, never through the
constructor.** Passing ``retry_count=None`` (or ``upload_time=None``) to
``MediaFile(...)`` makes the ORM *omit* the column from the INSERT, the
Python-side default or the ``server_default`` fills it, and the row comes back as
``0``/``now()`` — a test written that way cannot fail. ``_null_out`` therefore
asserts the NULL actually landed, the "guard the guard" pattern established by
``tests/unit/test_task_detection_service.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import update

from app.core.task_config import TaskRecoveryConfig
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services import system_settings_service
from app.services.task_recovery_service import TaskRecoveryService

NOW = datetime.now(UTC)

#: Explicit config so the PENDING-retry threshold is a known number rather than
#: whatever the shipped default happens to be.
CONFIG = TaskRecoveryConfig(PENDING_FILE_RETRY_THRESHOLD=6)

#: A message ``categorize_error`` maps to NETWORK_ERROR, which ``should_retry``
#: allows for the first 3 attempts. Retriability is a *precondition* of these
#: tests, not what they assert.
RETRIABLE_ERROR = "Connection timeout while downloading"


@pytest.fixture
def service() -> TaskRecoveryService:
    return TaskRecoveryService(config=CONFIG)


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


def _null_out(db, model, row_id: int | str, *columns: str) -> None:
    """Set ``columns`` to SQL NULL with an explicit UPDATE, then prove they are NULL.

    The constructor cannot express this: SQLAlchemy omits a None-valued column
    that has a Python-side ``default=`` or a ``server_default`` from the INSERT,
    so the row would be written with the default and the test could not fail.
    The trailing assertion is the guard-the-guard — if the NULL ever stops
    landing, the test says so instead of quietly passing.
    """
    db.execute(update(model).where(model.id == row_id).values(**dict.fromkeys(columns)))
    db.commit()
    row = db.get(model, row_id)
    db.refresh(row)
    for column in columns:
        assert getattr(row, column) is None, f"{model.__name__}.{column} did not land as NULL"


def _assert_retries_allowed(db) -> None:
    """Precondition: the deployment's retry budget permits a first attempt.

    ``should_retry_file`` reads the DB-backed ``transcription.max_retries``
    setting. Stating it here means an ambient settings change reports itself
    instead of looking like a regression in the code under test.
    """
    assert system_settings_service.should_retry_file(db, 0) is True


# =============================================================================
# app/utils/task_utils.py
# =============================================================================
def test_reset_file_for_retry_normalizes_a_null_retry_count(db_session, normal_user):
    """``reset_file_for_retry`` must increment a NULL ``retry_count`` to 1.

    Site: ``task_utils.py`` ``reset_file_for_retry`` — ``media_file.retry_count += 1``.

    This is the **manual retry entry point**: ``POST /api/my-files/{uuid}/retry``
    and the recovery sweeps both land here. On a NULL row ``None += 1`` raises
    TypeError inside the function's own ``except Exception``, which rolls back and
    returns ``False`` — so the endpoint answers "Failed to schedule file retry"
    (500) forever and the user has no way to get the file moving again. Against
    that implementation this test fails on both assertions: ``False`` is returned
    and ``retry_count`` is still NULL.
    """
    from app.utils.task_utils import reset_file_for_retry

    media_file = _file(db_session, normal_user, status=FileStatus.ERROR)
    _null_out(db_session, MediaFile, media_file.id, "retry_count")

    assert reset_file_for_retry(db_session, int(media_file.id), reset_retry_count=False) is True

    db_session.refresh(media_file)
    assert media_file.retry_count == 1
    assert media_file.status == FileStatus.PENDING


def test_recover_stuck_file_reads_a_null_retry_count_as_zero(db_session, normal_user, monkeypatch):
    """``recover_stuck_file`` must treat a NULL ``retry_count`` as 0, not raise.

    Site: ``task_utils.py`` ``recover_stuck_file`` —
    ``should_retry_file(db, int(media_file.retry_count))``.

    ``int(None)`` is a TypeError, raised *before* the retry decision is even
    made, and it is swallowed by the function's ``except Exception`` into a bare
    ``False``. The file is therefore declared unrecoverable for the one reason
    that has nothing to do with whether it can be recovered. Against that
    implementation this test fails: ``recover_stuck_file`` returns ``False`` and
    the file stays in ERROR.

    ``SKIP_CELERY`` suppresses only the final dispatch (``_start_transcription_task``),
    so the whole decision path — retry budget, reset, status transition — is real.
    """
    from app.utils.task_utils import recover_stuck_file

    monkeypatch.setenv("SKIP_CELERY", "true")
    _assert_retries_allowed(db_session)

    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message=RETRIABLE_ERROR,
        active_task_id=None,
    )
    _null_out(db_session, MediaFile, media_file.id, "retry_count")

    assert recover_stuck_file(db_session, int(media_file.id)) is True

    db_session.refresh(media_file)
    assert media_file.retry_count == 1
    assert media_file.status == FileStatus.PENDING


def test_recover_stuck_file_normalizes_a_null_recovery_attempts(
    db_session, normal_user, monkeypatch
):
    """The recovery-tracking write must increment a NULL ``recovery_attempts`` to 1.

    Site: ``task_utils.py`` ``_update_recovery_tracking`` —
    ``media_file.recovery_attempts += 1``.

    A PROCESSING file with no transcript segments takes the fall-through branch,
    which marks it ORPHANED and then records the attempt. ``None += 1`` there is
    a TypeError swallowed into ``False``, so the file is left in PROCESSING with
    its attempt uncounted — and because the counter never advances, the *next*
    sweep makes exactly the same doomed attempt. ``retry_count`` is set to 0
    explicitly so ``recovery_attempts`` is the only NULL under test.
    """
    from app.utils.task_utils import recover_stuck_file

    monkeypatch.setenv("SKIP_CELERY", "true")

    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.PROCESSING,
        retry_count=0,
        active_task_id=None,
    )
    _null_out(db_session, MediaFile, media_file.id, "recovery_attempts")

    assert recover_stuck_file(db_session, int(media_file.id)) is True

    db_session.refresh(media_file)
    assert media_file.recovery_attempts == 1
    assert media_file.status == FileStatus.ORPHANED


# =============================================================================
# app/services/task_recovery_service.py
# =============================================================================
def test_recover_stuck_files_without_celery_tasks_normalizes_a_null_retry_count(
    service, db_session, normal_user
):
    """The no-worker recovery sweep must increment a NULL ``retry_count``.

    Site: ``task_recovery_service.py`` ``recover_stuck_files_without_celery_tasks``
    — ``media_file.retry_count += 1``.

    Every read *around* it already says ``int(media_file.retry_count or 0)`` — the
    two ``should_retry`` gates and the delay calculation — so the row passes all
    three checks and then dies on the write. The per-file ``except`` logs it and
    moves on, so the sweep reports ``files_recovered == 0`` for a file it had just
    decided to recover. That is the assertion that fails against the old code.

    ``schedule_file_retry`` opens its own ``SessionLocal`` and cannot see
    savepointed rows, so the dispatch half legitimately fails here; the counter
    and the status transition are what this test pins.
    """
    _assert_retries_allowed(db_session)

    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.PROCESSING,
        last_error_message=RETRIABLE_ERROR,
    )
    _null_out(db_session, MediaFile, media_file.id, "retry_count")

    stats = service.recover_stuck_files_without_celery_tasks(db_session, [media_file])

    assert stats["files_recovered"] == 1
    db_session.refresh(media_file)
    assert media_file.retry_count == 1


def test_recover_user_files_normalizes_a_null_retry_count_on_a_stuck_processing_file(
    service, db_session, normal_user
):
    """The per-user sweep must increment a NULL ``retry_count`` on a stuck file.

    Site: ``task_recovery_service.py`` ``recover_user_files`` — the
    ``media_file.retry_count += 1`` in the "PROCESSING with incomplete
    transcription" branch. The ``should_retry_file`` gate immediately above reads
    ``retry_count or 0`` and lets the row through; the write then raises TypeError
    into the per-file ``except``, so ``files_recovered`` stays 0 and the file is
    left stuck in PROCESSING with no transcript and no retry.
    """
    _assert_retries_allowed(db_session)

    media_file = _file(db_session, normal_user, status=FileStatus.PROCESSING)
    _null_out(db_session, MediaFile, media_file.id, "retry_count")

    stats = service.recover_user_files(db_session, [media_file])

    assert stats["files_recovered"] == 1
    db_session.refresh(media_file)
    assert media_file.retry_count == 1


def test_recover_user_files_normalizes_a_null_retry_count_on_a_long_pending_file(
    service, db_session, normal_user
):
    """The PENDING-too-long branch must increment a NULL ``retry_count``.

    Site: ``task_recovery_service.py`` ``recover_user_files`` — the second
    ``media_file.retry_count += 1``, reached when a PENDING file is older than
    ``PENDING_FILE_RETRY_THRESHOLD`` (6 h here). Same TypeError, and because this
    branch's only other effect is the dispatch, a NULL row's retry is dropped
    entirely with nothing in the stats to show it.
    """
    _assert_retries_allowed(db_session)

    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.PENDING,
        upload_time=NOW - timedelta(hours=7),
    )
    _null_out(db_session, MediaFile, media_file.id, "retry_count")

    service.recover_user_files(db_session, [media_file])

    # flush() before refresh(), unlike the PROCESSING test above. That branch calls
    # update_media_file_status, which commits internally, so its increment is already
    # in the DB by the time refresh() re-reads. This branch's only other effect is
    # schedule_file_retry, which opens its OWN SessionLocal -- so nothing here writes,
    # and a bare refresh() would DISCARD the pending in-memory increment and read back
    # NULL. Production is fine: the caller in tasks/recovery.py runs inside
    # `with session_scope() as db`, which commits on exit (session_utils.py:25).
    # Flushing reproduces that persistence inside the savepoint.
    db_session.flush()
    db_session.refresh(media_file)
    assert media_file.retry_count == 1


def test_recover_user_files_recovers_a_file_with_a_null_upload_time(
    service, db_session, normal_user
):
    """A NULL ``upload_time`` must not abort the recovery of a stuck file.

    Site: ``task_recovery_service.py`` ``recover_user_files`` —
    ``assert media_file.upload_time is not None  # server_default=now()``.

    The premise is wrong: a ``server_default`` fills the column only for an INSERT
    that omits it, so an explicit ``UPDATE`` (or a backfill) leaves NULL. The
    assert then raises AssertionError, which the per-file ``except Exception``
    catches like any other error — so the *entire* PROCESSING-recovery branch
    below it is skipped and the sweep reports ``files_recovered == 0``. Under
    ``python -O`` the assert vanishes and ``datetime.now(UTC) - None`` raises
    TypeError in its place, so the assert is not NULL-safety in either mode.

    ``retry_count`` is 0 here so the age read is the only defect under test.
    """
    _assert_retries_allowed(db_session)

    media_file = _file(db_session, normal_user, status=FileStatus.PROCESSING, retry_count=0)
    _null_out(db_session, MediaFile, media_file.id, "upload_time")

    stats = service.recover_user_files(db_session, [media_file])

    assert stats["files_recovered"] == 1
    db_session.refresh(media_file)
    assert media_file.status == FileStatus.PENDING


def test_recover_retriable_error_files_normalizes_both_null_counters(
    service, db_session, normal_user
):
    """The retriable-ERROR sweep must increment both NULL counters.

    Sites: ``task_recovery_service.py`` ``recover_retriable_error_files`` —
    ``media_file.retry_count += 1`` and ``media_file.recovery_attempts += 1`` on
    consecutive lines.

    This is the smoking gun for the whole class of defect: the log line
    immediately above them already reads ``int(media_file.retry_count or 0) + 1``,
    so the code knows the column can be NULL and then increments it in place
    anyway. The TypeError lands in the per-file ``except``, which counts the file
    as ``files_failed`` — indistinguishable from a genuine dispatch failure — and
    leaves the file in ERROR with both counters still NULL. This test fails
    against that on all three assertions.
    """
    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.ERROR,
        last_error_message=RETRIABLE_ERROR,
    )
    _null_out(db_session, MediaFile, media_file.id, "retry_count", "recovery_attempts")

    service.recover_retriable_error_files(db_session, [media_file])

    db_session.refresh(media_file)
    assert media_file.retry_count == 1
    assert media_file.recovery_attempts == 1
    assert media_file.status == FileStatus.PENDING


def test_update_media_file_if_no_active_tasks_normalizes_a_null_retry_count(
    service, db_session, normal_user
):
    """The post-task-recovery reset must increment a NULL ``retry_count``.

    Site: ``task_recovery_service.py`` ``_update_media_file_if_no_active_tasks``
    — ``media_file.retry_count += 1``, guarded by a ``should_retry(...,
    int(media_file.retry_count or 0))`` on the line above that normalizes the
    very same column.

    This method has **no** ``try``/``except`` of its own, so against the old code
    the TypeError propagates out of the call and this test fails as an error
    rather than an assertion — which is the honest report, since in production it
    propagates into whichever caller is sweeping.
    """
    media_file = _file(
        db_session,
        normal_user,
        status=FileStatus.PROCESSING,
        last_error_message=RETRIABLE_ERROR,
    )
    _null_out(db_session, MediaFile, media_file.id, "retry_count")

    service._update_media_file_if_no_active_tasks(db_session, media_file)

    db_session.refresh(media_file)
    assert media_file.retry_count == 1


def test_recover_stuck_llm_tasks_marks_a_task_with_a_null_created_at_failed(
    service, db_session, normal_user
):
    """A NULL ``created_at`` must not stop a stuck LLM task being marked failed.

    Site: ``task_recovery_service.py`` ``recover_stuck_llm_tasks`` —
    ``assert task.created_at is not None  # server_default=now()``, whose only
    consumer is the duration in the log message.

    ``task.created_at`` is nullable, so the assert is reachable; it fires *before*
    the four status writes and is caught by the per-task ``except``, which also
    calls ``db.rollback()``. A cosmetic log field therefore prevents the task
    being marked failed at all — and since recovery only retries *failed* tasks,
    the task stays in_progress forever. This test fails against that with
    ``tasks_marked_failed == 0``.
    """
    media_file = _file(db_session, normal_user)
    task = Task(
        id=f"task-{uuid.uuid4()}",
        user_id=normal_user.id,
        media_file_id=media_file.id,
        task_type="summarization",
        status="in_progress",
    )
    db_session.add(task)
    db_session.commit()
    _null_out(db_session, Task, task.id, "created_at")

    stats = service.recover_stuck_llm_tasks(db_session, [task])

    assert stats["tasks_marked_failed"] == 1
    db_session.refresh(task)
    assert task.status == "failed"
