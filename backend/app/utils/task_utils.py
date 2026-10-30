import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session_utils import get_refreshed_object
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.services.task_row_supersede import is_active_row_conflict
from app.services.task_row_supersede import record_superseded
from app.services.task_row_supersede import reopen_if_slot_free
from app.services.task_row_supersede import supersede_active_siblings

logger = logging.getLogger(__name__)

# Task status constants
TASK_STATUS_PENDING = "pending"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
#: A terminal, non-error outcome: the task ran but deliberately did no real work
#: (e.g. an idempotency guard found the result already present, or a duplicate
#: dispatch was caught mid-flight). Distinct from COMPLETED so the Tasks UI can
#: tell "did the work" apart from "correctly decided not to" — see issue #622.
TASK_STATUS_SKIPPED = "skipped"

_ACTIVE_TASK_STATUSES = (TASK_STATUS_PENDING, TASK_STATUS_IN_PROGRESS)
#: Task types whose row is fenced by run id: a replacement run always gets a NEW Task row, so a
#: terminal row of this type never legitimately becomes active again (issue #1178). Other
#: types (search indexing, speaker clustering) mark their row failed and then ``self.retry()``
#: under the same id, and the retry must be able to reopen it.
_RUN_FENCED_TASK_TYPES = frozenset({"transcription"})


#: A concurrent creator can win the index between our sibling-supersede and our insert; each
#: attempt re-supersedes the winner, so two attempts settle it and a third is a bug, not a race.
_CREATE_ATTEMPTS = 3


def create_task_record(
    db: Session, celery_task_id: str, user_id: int, media_file_id: int | None, task_type: str
) -> Task:
    """Create a new task record in the database.

    ``media_file_id`` is ``None`` for a corpus-wide job with no single owning file (e.g.
    issue #626's operator-triggered re-embed, which can span many files across many
    owners) — the column is already ``nullable=True`` and ``update_task_status`` already
    guards its own media-file lookup on ``if media_file_id:``.

    A file has at most one active row per ``task_type`` (``v448``, issue #1185), and the newer
    run wins: the file's other active rows of this step are failed with the reason
    ``superseded by <this id>`` in the same transaction as the insert, so the database never
    holds two. A concurrent creator that lands between the two statements is superseded the
    same way on the next attempt.
    """
    task: Task | None = None
    superseded: list[str] = []
    for attempt in range(1, _CREATE_ATTEMPTS + 1):
        superseded = (
            supersede_active_siblings(
                db, media_file_id=media_file_id, task_type=task_type, by_task_id=celery_task_id
            )
            if media_file_id is not None
            else []
        )
        task = Task(
            id=celery_task_id,
            user_id=user_id,
            media_file_id=media_file_id,
            task_type=task_type,
            status=TASK_STATUS_PENDING,
            progress=0.0,
            # The column has no insert default; a NULL here put a brand-new transcription row
            # into the lost-run query before its queued marker existed (issue #1178).
            updated_at=datetime.now(UTC),
        )
        db.add(task)
        try:
            db.commit()
            break
        except IntegrityError as exc:
            db.rollback()
            if is_active_row_conflict(exc) and attempt < _CREATE_ATTEMPTS:
                logger.info(
                    "Task %s (%s, file %s) lost the active-row index to a concurrent creator; "
                    "superseding it (attempt %s)",
                    celery_task_id,
                    task_type,
                    media_file_id,
                    attempt,
                )
                continue
            if is_active_row_conflict(exc):
                raise
            # Task already exists (race condition or retry), fetch and return it
            task = db.query(Task).filter(Task.id == celery_task_id).first()
            if not task:
                raise ValueError(f"Failed to create or find task: {celery_task_id}") from None
            logger.info(f"Task {celery_task_id} already exists, reusing existing record")
            superseded = []
            break

    if task is None:  # unreachable: the loop commits, reuses, or raises
        raise ValueError(f"Failed to create task: {celery_task_id}")
    if superseded:
        logger.warning(
            "Task %s (%s) superseded %d older active row(s) for file %s: %s",
            celery_task_id,
            task_type,
            len(superseded),
            media_file_id,
            ", ".join(superseded),
        )
        record_superseded([task_type] * len(superseded))

    db.refresh(task)

    # Update the media file with active task tracking — only when this task has one.
    if media_file_id is not None:
        media_file = get_refreshed_object(db, MediaFile, media_file_id)
        if media_file:
            media_file.active_task_id = celery_task_id
            media_file.task_started_at = datetime.now(UTC)
            media_file.task_last_update = datetime.now(UTC)
            media_file.cancellation_requested = False
            if media_file.status == FileStatus.PENDING:
                media_file.status = FileStatus.PROCESSING
            db.commit()

    return task


def update_task_status(
    db: Session,
    task_id: str,
    status: str,
    progress: float | None = None,
    error_message: str | None = None,
    completed: bool = False,
) -> Task | None:
    """Update task status in the database.

    Thin wrapper over :func:`update_task_status_with_duration` for the majority of
    callers, which do not care how long the task took. If you need the elapsed time,
    call that function directly — it is the only place the number can be computed.
    """
    task, _ = update_task_status_with_duration(
        db, task_id, status, progress, error_message, completed
    )
    return task


def update_task_status_with_duration(
    db: Session,
    task_id: str,
    status: str,
    progress: float | None = None,
    error_message: str | None = None,
    completed: bool = False,
) -> tuple[Task | None, float | None]:
    """Update task status, and report how long the task ran.

    Returns ``(task, duration_seconds)``. The duration is issue #753's notification
    chip, and it MUST be computed here rather than by a later reader of ``MediaFile``:
    the terminal branch below clears ``media_file.task_started_at`` in this same call,
    which is the only record of when this task began. Once cleared it is gone — there
    is no other timestamp to reconstruct it from, and ``MediaFile.duration`` is the
    length of the RECORDING, a different number that must never be substituted for
    processing time.

    ⚠️ It is **returned**, not stamped onto the ``Task``. An earlier revision set a
    transient ``task.duration_seconds`` attribute, which forced a ``ClassVar``
    declaration on the model to stop SQLAlchemy's Annotated Declarative scanner
    raising ``MappedAnnotationError`` at class-definition time — and mypy then
    correctly rejected assigning to that ``ClassVar`` through an instance. Smuggling a
    computed value out on an ORM object was fighting both tools because it was the
    wrong shape; returning it is simpler and needs no declaration at all.

    A write that makes a transcription run's row ACTIVE (``pending`` / ``in_progress``) lands
    only while the row is still active (issue #1178): recovery may have reclaimed the run and
    dispatched a replacement while a stage of the original was still executing, and that
    stage's next progress write must not move the reclaimed row back to ``in_progress``. The
    check is one conditional ``UPDATE``, so it holds against a reclaim committed by another
    process between this session's read and its write. A dropped write returns
    ``(None, None)``.
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        logger.warning(f"Task {task_id} not found")
        return None, None

    # Log state transition for debugging
    logger.debug(f"Task {task_id} state change: {task.status} -> {status}")

    now = datetime.now(UTC)
    if status in _ACTIVE_TASK_STATUSES and task.task_type in _RUN_FENCED_TASK_TYPES:
        if not _write_if_still_active(db, task, status, progress, error_message, completed, now):
            db.commit()  # whatever else the caller staged commits exactly as it always did
            return None, None
    elif status in _ACTIVE_TASK_STATUSES and task.status not in _ACTIVE_TASK_STATUSES:
        # A retry reopening its own failed row. A newer run of the same step may have taken the
        # file's one active slot meanwhile (v448, issue #1185); that run wins and this write is
        # dropped, exactly as a fenced transcription's is.
        if not _reopen_task_row(db, task, status, progress, error_message, completed, now):
            db.commit()
            return None, None
    else:
        _apply_task_fields(task, status, progress, error_message, completed, now)

    duration_seconds: float | None = None

    # Update media file task tracking
    media_file_id = task.media_file_id
    if media_file_id:
        media_file = get_refreshed_object(db, MediaFile, int(media_file_id))
        if media_file:
            media_file.task_last_update = datetime.now(UTC)
            if error_message:
                media_file.last_error_message = error_message

            # Clear active task once it reaches any terminal state — completed,
            # failed, or skipped. Leaving this set after a skip would misreport
            # the file as still busy to `is_file_safe_to_delete` and friends,
            # even though nothing is running any more (issue #622).
            if status in [TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_SKIPPED]:
                if task.completed_at is not None and media_file.task_started_at is not None:
                    duration_seconds = (
                        task.completed_at - media_file.task_started_at
                    ).total_seconds()
                media_file.active_task_id = None
                media_file.task_started_at = None

    db.commit()
    db.refresh(task)

    # Terminal states re-check the media file's aggregate status.
    task_media_file_id = task.media_file_id
    if (
        status in [TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_SKIPPED]
        and task_media_file_id
    ):
        update_media_file_from_task_status(db, int(task_media_file_id))

    return task, duration_seconds


def _apply_task_fields(
    task: Task,
    status: str,
    progress: float | None,
    error_message: str | None,
    completed: bool,
    now: datetime,
) -> None:
    task.status = status  # type: ignore[assignment]
    if progress is not None:
        task.progress = progress  # type: ignore[assignment]
    if error_message:
        task.error_message = error_message  # type: ignore[assignment]
    if completed:
        task.completed_at = now  # type: ignore[assignment]

    # Always update the timestamp for task state changes
    task.updated_at = now  # type: ignore[assignment]


def _reopen_task_row(
    db: Session,
    task: Task,
    status: str,
    progress: float | None,
    error_message: str | None,
    completed: bool,
    now: datetime,
) -> bool:
    """Reopen a terminal row as active; False when another active row holds the file's slot."""
    reopened = reopen_if_slot_free(
        db,
        task,
        {
            "status": status,
            "updated_at": now,
            "progress": Task.progress if progress is None else progress,
            "error_message": error_message or Task.error_message,
            "completed_at": now if completed else Task.completed_at,
        },
    )
    if not reopened:
        logger.info(
            "Dropped a %r write to task %s: a newer %s run is already active for file %s",
            status,
            task.id,
            task.task_type,
            task.media_file_id,
        )
    return reopened


def _write_if_still_active(
    db: Session,
    task: Task,
    status: str,
    progress: float | None,
    error_message: str | None,
    completed: bool,
    now: datetime,
) -> bool:
    """Apply an active-status write to ``task`` only if its row is still active in the DB.

    Returns False (and changes nothing) when the row has meanwhile reached a terminal state.
    """
    # A column left as itself (``SET progress = progress``) is what "not given" means here.
    matched = (
        db.query(Task)
        .filter(Task.id == task.id, Task.status.in_(_ACTIVE_TASK_STATUSES))
        .update(
            {
                "status": status,
                "updated_at": now,
                "progress": Task.progress if progress is None else progress,
                "error_message": error_message or Task.error_message,
                "completed_at": now if completed else Task.completed_at,
            },
            synchronize_session=False,
        )
    )
    # The in-memory object may predate a reclaim; re-read it either way.
    db.expire(task)
    if not matched:
        logger.debug(
            "Dropped a %r write to task %s: the run is no longer active (reclaimed or finished)",
            status,
            task.id,
        )
        return False
    return True


def update_media_file_status(db: Session, file_id: int, status: FileStatus) -> MediaFile | None:
    """Update media file status."""
    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file:
        logger.warning(f"Media file {file_id} not found")
        return None

    # Log state transition for debugging
    logger.debug(f"Media file {file_id} state change: {media_file.status} -> {status}")

    # Issue #824: this is the SECOND writer of status/completed_at (the first is
    # transcription/storage.py's update_media_file_transcription_status) and it
    # used to write both fields unconditionally -- with no quarantine/legal-hold
    # awareness at all -- reached via a separate chain
    # (postprocess.py -> update_task_status -> update_media_file_from_task_status
    # -> here). That made the first writer's guard vacuous: it would decline to
    # write, and this function would clobber QUARANTINED with COMPLETED/PROCESSING
    # moments later on the very same request. Function-local import: see
    # storage.py's identical note on why takedown_service isn't a module-level import.
    from app.services.takedown_service import apply_processing_status
    from app.services.takedown_service import stamp_completion_time

    apply_processing_status(media_file, status)

    # Set completed_at timestamp if status is COMPLETED or ERROR
    if status in [FileStatus.COMPLETED, FileStatus.ERROR]:
        stamp_completion_time(media_file, datetime.now(UTC))

    db.commit()
    db.refresh(media_file)
    return media_file  # type: ignore[no-any-return]


def update_media_file_from_task_status(db: Session, file_id: int) -> MediaFile | None:
    """Check task statuses and update media file status accordingly.

    Uses a single SQL query with conditional COUNT to determine the correct
    file status instead of loading every Task object into Python.

    Args:
        db: Database session
        file_id: ID of the media file to check

    Returns:
        Updated MediaFile object or None if not found
    """
    from sqlalchemy import func as sa_func

    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file:
        logger.warning(f"Media file {file_id} not found")
        return None

    # Skip if the file is already in a terminal state
    if media_file.status in [FileStatus.COMPLETED, FileStatus.ERROR]:
        return media_file  # type: ignore[no-any-return]

    # Single SQL query: conditional counts per status
    row = (
        db.query(
            sa_func.count().label("total"),
            sa_func.count().filter(Task.status == TASK_STATUS_PENDING).label("pending"),
            sa_func.count().filter(Task.status == TASK_STATUS_IN_PROGRESS).label("in_progress"),
            sa_func.count().filter(Task.status == TASK_STATUS_COMPLETED).label("completed"),
            sa_func.count().filter(Task.status == TASK_STATUS_FAILED).label("failed"),
        )
        .filter(Task.media_file_id == file_id)
        .first()
    )

    if not row or row.total == 0:
        logger.warning(f"No tasks found for media file {file_id}")
        return media_file  # type: ignore[no-any-return]

    # Determine the appropriate media file status
    if row.pending > 0 or row.in_progress > 0:
        new_status = FileStatus.PROCESSING
    elif row.failed > 0 and row.completed == 0:
        new_status = FileStatus.ERROR
    else:
        new_status = FileStatus.COMPLETED

    # Only update if status is changing
    if media_file.status != new_status:
        return update_media_file_status(db, file_id, new_status)

    return media_file  # type: ignore[no-any-return]


def get_task_summary_for_media_file(db: Session, file_id: int) -> dict[str, Any]:
    """Get a summary of task statuses for a media file.

    Uses a single SQL query with conditional COUNT and AVG instead of
    loading every Task row into Python.

    Args:
        db: Database session
        file_id: ID of the media file to check

    Returns:
        Dictionary with task status counts and overall progress
    """
    from sqlalchemy import func as sa_func

    row = (
        db.query(
            sa_func.count().label("total"),
            sa_func.count().filter(Task.status == TASK_STATUS_PENDING).label("pending"),
            sa_func.count().filter(Task.status == TASK_STATUS_IN_PROGRESS).label("in_progress"),
            sa_func.count().filter(Task.status == TASK_STATUS_COMPLETED).label("completed"),
            sa_func.count().filter(Task.status == TASK_STATUS_FAILED).label("failed"),
            sa_func.coalesce(sa_func.avg(Task.progress), 0.0).label("avg_progress"),
        )
        .filter(Task.media_file_id == file_id)
        .first()
    )

    if not row or row.total == 0:
        return {
            "total": 0,
            "pending": 0,
            "in_progress": 0,
            "completed": 0,
            "failed": 0,
            "overall_progress": 0.0,
        }

    return {
        "total": row.total,
        "pending": row.pending,
        "in_progress": row.in_progress,
        "completed": row.completed,
        "failed": row.failed,
        "overall_progress": float(row.avg_progress),
    }
