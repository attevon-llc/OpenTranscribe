"""Task rows that a newer run of the same step has superseded (issue #1185).

A ``task`` row can stay ``pending`` / ``in_progress`` forever after a newer row of the same
``task_type`` for the same ``media_file_id`` has completed: a late writer from the superseded
run resurrects it. Recovery judges by file state and live work, and the file is completed with
nothing running, so it never acts; monitoring that counts long-running rows alerts for good.

This module is the single definition of that condition and of its resolution:

* :func:`superseded_active_rows` / :func:`count_superseded_active_rows` — the predicate. The
  sweep acts on exactly these rows and the ``task_rows_superseded_active`` gauge counts exactly
  these rows, so an alert always names something the sweep can resolve.
* :func:`resolve_superseded_task_rows` — the sweep body, run first by ``system.health_check``.
* :func:`supersede_active_siblings` — the same resolution at row creation, so the database's
  one-active-row-per-file-step guard (``v448``) is met by the newer row replacing the older.

The migration ``v448_task_one_active_row_per_file_step`` applies the same two rules in SQL to
rows that already exist; keep the three in step.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from app.models.media import TASK_ACTIVE_INDEX
from app.models.media import MediaFile
from app.models.media import Task

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "in_progress")
_STATUS_FAILED = "failed"
_STATUS_COMPLETED = "completed"

#: Rows resolved per sweep. The rest are picked up by the next run; the backlog drains at
#: this rate and a healthy system never has any.
SWEEP_BATCH_SIZE = 500


def is_active_row_conflict(exc: IntegrityError) -> bool:
    """Whether ``exc`` is the one-active-row-per-file-step index refusing a second active row.

    Every other integrity error (the primary key of a re-delivered task, a foreign key) is a
    different situation with a different answer, so the callers that tolerate this one match on
    the index name instead of catching ``IntegrityError`` wholesale.
    """
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None) or ""
    return TASK_ACTIVE_INDEX in (name or str(exc.orig))


def superseded_reason(superseded_by: str) -> str:
    """The ``error_message`` a superseded row carries."""
    return f"superseded by {superseded_by}"


def _newest_completed_after(old: type[Task]):  # type: ignore[no-untyped-def]
    """Scalar subquery: id of the newest ``completed`` row of ``old``'s step created after it."""
    newer = aliased(Task)
    return (
        select(newer.id)
        .where(
            newer.media_file_id == old.media_file_id,
            newer.task_type == old.task_type,
            newer.status == _STATUS_COMPLETED,
            newer.created_at > old.created_at,
        )
        .order_by(newer.created_at.desc(), newer.id.desc())
        .limit(1)
        .correlate(old)
        .scalar_subquery()
    )


def superseded_active_rows(db: Session, limit: int | None = None) -> list[tuple[str, str, str]]:
    """Active rows that a newer completed row of the same file and step has superseded.

    Args:
        db: Database session.
        limit: Cap on the rows returned (oldest first); None for all of them.

    Returns:
        ``(task id, task_type, id of the completed row that supersedes it)`` tuples.
    """
    by_id = _newest_completed_after(Task)
    stmt = (
        select(Task.id, Task.task_type, by_id.label("by_id"))
        .where(
            Task.status.in_(ACTIVE_STATUSES),
            Task.media_file_id.is_not(None),
            by_id.is_not(None),
        )
        .order_by(Task.created_at, Task.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return [(str(r[0]), str(r[1]), str(r[2])) for r in db.execute(stmt).all()]


def count_superseded_active_rows(db: Session) -> int:
    """How many rows :func:`resolve_superseded_task_rows` would resolve right now."""
    by_id = _newest_completed_after(Task)
    stmt = select(func.count()).where(
        Task.status.in_(ACTIVE_STATUSES), Task.media_file_id.is_not(None), by_id.is_not(None)
    )
    return int(db.execute(stmt).scalar() or 0)


def _fail_if_still_active(db: Session, task_id: str, superseded_by: str) -> bool:
    """Mark one row ``failed`` — only if it is still active when the UPDATE runs.

    The status predicate is what keeps this from clobbering a row a concurrent writer finished
    (completed, failed, cancelled) between the read and this write.
    """
    now = datetime.now(UTC)
    result = db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status.in_(ACTIVE_STATUSES))
        .values(
            status=_STATUS_FAILED,
            error_message=superseded_reason(superseded_by),
            completed_at=func.coalesce(Task.completed_at, now),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return bool(getattr(result, "rowcount", 0))


def reopen_if_slot_free(db: Session, task: Task, values: dict[str, object]) -> bool:
    """Reopen a finished row as active, unless that would be wrong or pointless.

    One conditional ``UPDATE`` that refuses when

    * another active row of the row's file and step holds the slot (a retry that finds a newer
      run already active is dropped instead of tripping the one-active-row index, ``v448``), or
    * a NEWER ``completed`` row of the same file and step exists: the row is superseded, so
      :func:`resolve_superseded_task_rows` would fail it again on its next run.

    The index is the backstop for two reopens racing: Postgres checks it at the statement, so
    the loser's UPDATE raises; that is the same refusal. The statement runs in a SAVEPOINT, so
    only the loser is rolled back and whatever the caller staged but has not committed (a
    batch of earlier resets, say) is kept. Otherwise commits nothing. Returns False when refused.
    """
    other = aliased(Task)
    slot_taken = (
        select(other.id)
        .where(
            other.media_file_id == Task.media_file_id,
            other.task_type == Task.task_type,
            other.status.in_(ACTIVE_STATUSES),
            other.id != Task.id,
        )
        .correlate(Task)
        .exists()
    )
    newer = aliased(Task)
    superseded = (
        exists()
        .where(
            newer.media_file_id == Task.media_file_id,
            newer.task_type == Task.task_type,
            newer.status == _STATUS_COMPLETED,
            newer.created_at > Task.created_at,
        )
        .correlate(Task)
    )
    nested = db.begin_nested()
    try:
        result = db.execute(
            update(Task)
            .where(Task.id == task.id, ~slot_taken, ~superseded)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
    except IntegrityError as exc:
        nested.rollback()
        if not is_active_row_conflict(exc):
            raise
        return False
    except BaseException:
        nested.rollback()
        raise
    nested.commit()
    db.expire(task)
    return bool(getattr(result, "rowcount", 0))


def record_superseded(task_types: list[str]) -> None:
    """Count rows resolved as superseded. Best effort: a metric must never break the write."""
    try:
        from app.core.metrics import task_rows_superseded_total

        for task_type in task_types:
            task_rows_superseded_total.labels(task_type=task_type).inc()
    except Exception as exc:  # telemetry never fails the recovery it describes
        logger.debug("Could not record task_rows_superseded_total: %s", exc)


def resolve_superseded_task_rows(db: Session, limit: int = SWEEP_BATCH_SIZE) -> dict[str, int]:
    """Fail every active row that a newer completed row of the same step has superseded.

    Commits once. A file's ``active_task_id`` that still names a resolved row is cleared with
    it, as every other terminal write does, so the file does not look busy on account of a row
    that is no longer running.

    Returns:
        ``{"found": n, "resolved": m}``; ``resolved`` is lower than ``found`` only when a
        concurrent writer finished a row first.
    """
    rows = superseded_active_rows(db, limit=limit)
    resolved_types: list[str] = []
    for task_id, task_type, by_id in rows:
        if not _fail_if_still_active(db, task_id, by_id):
            continue
        resolved_types.append(task_type)
        db.execute(
            update(MediaFile)
            .where(MediaFile.active_task_id == task_id)
            .values(active_task_id=None, task_started_at=None)
            .execution_options(synchronize_session=False)
        )
        logger.warning(
            "Task row %s (%s) was still active after %s completed; marked failed: %s",
            task_id,
            task_type,
            by_id,
            superseded_reason(by_id),
        )
    db.commit()
    record_superseded(resolved_types)
    return {"found": len(rows), "resolved": len(resolved_types)}


def supersede_active_siblings(
    db: Session, *, media_file_id: int, task_type: str, by_task_id: str
) -> list[str]:
    """Fail the other active rows of a file's step because ``by_task_id`` is replacing them.

    Used when a new row is about to be inserted, in the same transaction, so the one-active-row
    index admits it. Does not commit and does not count the metric: the caller counts what it
    got back once its own commit has landed.

    Returns:
        The ids of the rows that were failed.
    """
    now = datetime.now(UTC)
    result = db.execute(
        update(Task)
        .where(
            Task.media_file_id == media_file_id,
            Task.task_type == task_type,
            Task.status.in_(ACTIVE_STATUSES),
            Task.id != by_task_id,
        )
        .values(
            status=_STATUS_FAILED,
            error_message=superseded_reason(by_task_id),
            completed_at=func.coalesce(Task.completed_at, now),
            updated_at=now,
        )
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    return [str(r[0]) for r in result.all()]
