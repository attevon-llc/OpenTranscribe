"""One SQL statement pair for ``GET /api/tasks``: filter, order and page in the database.

The endpoint used to read every visible file and every task row of those files, then filter and
slice in Python (issue #1337, K1: 4.3 s for a 20,000-file owner). A file's "current" task is the
one named by ``MediaFile.active_task_id`` while the pipeline runs and the newest row after that,
which is a per-file ``ORDER BY ... LIMIT 1``: a ``LATERAL`` join documents that shape directly
(https://www.postgresql.org/docs/17/queries-table-expressions.html#QUERIES-LATERAL).

The filter semantics mirror ``TaskFilteringService`` (still used for the display fields), so the
two cannot drift silently: ``tests/api/endpoints/test_tasks_list_sql.py`` pins them together.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import Row
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import true
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.services.file_availability import exclude_unavailable

logger = logging.getLogger(__name__)

_AGE_HOURS = {"today": 24, "week": 24 * 7, "month": 24 * 30}
# A file with no task row reports the status its own column implies.
_FILE_STATUS_AS_TASK_STATUS = (
    (FileStatus.PENDING, "pending"),
    (FileStatus.PROCESSING, "in_progress"),
    (FileStatus.COMPLETED, "completed"),
    (FileStatus.ERROR, "failed"),
)


@dataclass(frozen=True)
class TaskListFilters:
    """The query parameters of ``GET /api/tasks`` that narrow the list."""

    status: str | None = None
    task_type: str | None = None
    age_filter: str | None = None
    date_from: str | None = None
    date_to: str | None = None


def _day_bound(value: str | None, label: str, *, end: bool) -> datetime | None:
    """Parse ``YYYY-MM-DD``; an invalid value is ignored with a warning, as it always was."""
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        logger.warning("Invalid %s format: %s", label, value)
        return None
    return day.replace(hour=23, minute=59, second=59, microsecond=999999) if end else day


def _filter_clauses(
    filters: TaskListFilters,
    task_id: Any,
    task_status: Any,
    task_type: Any,
    created_at: Any,
    file_status: Any,
) -> list[ColumnElement[bool]]:
    """SQL form of ``TaskFilteringService.filter_tasks_by_criteria``."""
    now = datetime.now(UTC)
    effective_status = case(
        (task_id.is_not(None), task_status),
        *((file_status == fs, name) for fs, name in _FILE_STATUS_AS_TASK_STATUS),
        else_="pending",
    )
    effective_type = func.coalesce(task_type, literal("transcription"))
    clauses: list[ColumnElement[bool]] = []

    if filters.status == "needs_attention":
        clauses.append(
            or_(
                effective_status.in_(("failed", "error")),
                and_(effective_status == "in_progress", created_at < now - timedelta(hours=1)),
                and_(effective_status == "pending", created_at < now - timedelta(hours=2)),
            )
        )
    elif filters.status:
        clauses.append(effective_status == filters.status)

    if filters.task_type:
        clauses.append(effective_type == filters.task_type)

    if filters.age_filter in _AGE_HOURS:
        clauses.append(created_at >= now - timedelta(hours=_AGE_HOURS[filters.age_filter]))
    elif filters.age_filter == "older":
        clauses.append(created_at < now - timedelta(hours=24 * 30))

    start = _day_bound(filters.date_from, "date_from", end=False)
    stop = _day_bound(filters.date_to, "date_to", end=True)
    if start is not None:
        clauses.append(created_at >= start)
    if stop is not None:
        clauses.append(created_at <= stop)
    return clauses


def list_task_page(
    db: Session,
    base: Query[Any],
    *,
    filters: TaskListFilters,
    page: int,
    page_size: int,
) -> tuple[list[Row[Any]], int]:
    """Return one page of ``(file, current task)`` rows and the filtered total.

    Args:
        db: Session (used only to build the lateral subquery's query).
        base: A ``Query`` over ``MediaFile`` already restricted to the caller's visible files
            (tenant, ownership and quarantine are the endpoint's decisions, not this module's).
        filters: The narrowing query parameters.
        page: 1-based page number.
        page_size: Rows per page.

    Returns:
        ``(rows, total)``. Rows are ordered newest first by the task's creation time (the file's
        upload time when it has no task), then by file id so equal timestamps page stably.
    """
    latest = (
        select(
            TaskModel.id.label("t_id"),
            TaskModel.task_type.label("t_task_type"),
            TaskModel.status.label("t_status"),
            TaskModel.progress.label("t_progress"),
            TaskModel.error_message.label("t_error_message"),
            TaskModel.created_at.label("t_created_at"),
            TaskModel.updated_at.label("t_updated_at"),
            TaskModel.completed_at.label("t_completed_at"),
        )
        .where(TaskModel.media_file_id == MediaFile.id)
        # `id = active_task_id` is NULL when no task is active; NULLS LAST sorts that after true.
        .order_by(
            (TaskModel.id == MediaFile.active_task_id).desc().nulls_last(),
            TaskModel.created_at.desc().nulls_last(),
            TaskModel.id.desc(),
        )
        .limit(1)
        .correlate(MediaFile)
        .lateral("latest_task")
    )
    created_at = func.coalesce(latest.c.t_created_at, MediaFile.upload_time)
    query = base.outerjoin(latest, true()).add_columns(
        *(c for c in latest.c),
        created_at.label("effective_created_at"),
    )
    for clause in _filter_clauses(
        filters,
        latest.c.t_id,
        latest.c.t_status,
        latest.c.t_task_type,
        created_at,
        MediaFile.status,
    ):
        query = query.filter(clause)

    total = query.order_by(None).with_entities(func.count(MediaFile.id)).scalar() or 0
    rows = (
        query.order_by(created_at.desc(), MediaFile.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return rows, int(total)


def visible_files_query(
    db: Session, visible: ColumnElement[bool], *, include_quarantined: bool
) -> Query[Any]:
    """Columns the task list needs from ``MediaFile``, restricted to ``visible`` files.

    Projects only what the list displays, so the large JSONB columns are never loaded. The
    quarantine/trash exclusion is the same "does this file exist for you" question every other
    surface asks.
    """
    columns = [
        MediaFile.id,
        MediaFile.uuid,
        MediaFile.filename,
        MediaFile.file_size,
        MediaFile.content_type,
        MediaFile.duration,
        MediaFile.language,
        MediaFile.status,
        MediaFile.upload_time,
        MediaFile.completed_at,
        MediaFile.media_format,
        MediaFile.codec,
        MediaFile.active_task_id,
        MediaFile.last_error_message,
    ]
    query = db.query(*columns).filter(visible)
    return exclude_unavailable(query, include_quarantined=include_quarantined)
