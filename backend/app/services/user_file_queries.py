"""Queries behind the caller's own file-status dashboard and retry routes.

Every list is the caller's OWN files in the ACTIVE tenant; quarantined files are excluded
unless the caller's bypass is exempt, and files the caller hid stay out of the two lists.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session
from sqlalchemy.orm import defer

from app.core.tenancy import OrgScope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.services.file_availability import exclude_unavailable
from app.services.hidden_media_service import exclude_hidden_for
from app.utils.db_helpers import owned_in_tenant

#: A processing file older than this, or a pending one older than the second, counts as stuck.
STUCK_PROCESSING_AFTER = timedelta(hours=1)
STUCK_PENDING_AFTER = timedelta(hours=2)
RECENT_WINDOW = timedelta(hours=24)
RECENT_LIMIT = 10


def _without_heavy_columns(db: Session) -> Query:
    """A ``MediaFile`` query that skips the large JSON columns a status list never shows."""
    return db.query(MediaFile).options(
        defer(MediaFile.metadata_raw),
        defer(MediaFile.waveform_data),
        defer(MediaFile.metadata_important),
        defer(MediaFile.summary_data),
    )


def status_count_rows(
    db: Session, *, user_id: int, organization_id: OrgScope, include_quarantined: bool
) -> list[Any]:
    """``(status, count)`` rows over the caller's files.

    Quarantine exclusion is applied BEFORE the GROUP BY, not by filtering the result: a
    quarantined file must not inflate a total either. Typed as the plain ``Query`` that
    ``exclude_unavailable`` takes and returns, because mypy would otherwise infer the
    narrower row-returning query from the multi-column ``db.query(...)`` and reject the
    reassignment.
    """
    own = owned_in_tenant(MediaFile, user_id=user_id, organization_id=organization_id)
    query: Query = db.query(MediaFile.status, func.count(MediaFile.id)).filter(own)
    query = exclude_unavailable(query, include_quarantined=include_quarantined)
    rows: list[Any] = query.group_by(MediaFile.status).all()
    return rows


def problem_files(
    db: Session,
    *,
    user_id: int,
    organization_id: OrgScope,
    include_quarantined: bool,
    now: datetime,
) -> list[MediaFile]:
    """Files stuck processing or pending too long, or in error, newest upload first.

    The ERROR arm places no predicate on ``upload_time``, so a NULL upload time can reach
    the caller. Quarantine overwrites ``status`` with the distinct ``QUARANTINED`` value, so
    the exclusion matches nothing today; it is applied for uniformity and stays correct if
    the status filter is ever loosened. A file the caller hid stays out of their dashboard
    lists too (issue #1102); the status counts stay whole, because hiding is not deleting.
    """
    own = owned_in_tenant(MediaFile, user_id=user_id, organization_id=organization_id)
    query = (
        _without_heavy_columns(db)
        .filter(
            own,
            (
                (MediaFile.status == FileStatus.PROCESSING)
                & (MediaFile.upload_time < now - STUCK_PROCESSING_AFTER)
            )
            | (
                (MediaFile.status == FileStatus.PENDING)
                & (MediaFile.upload_time < now - STUCK_PENDING_AFTER)
            )
            | (MediaFile.status == FileStatus.ERROR),
        )
        .order_by(MediaFile.upload_time.desc())
    )
    query = exclude_unavailable(query, include_quarantined=include_quarantined)
    return list(exclude_hidden_for(query, user_id).all())


def recent_files(
    db: Session,
    *,
    user_id: int,
    organization_id: OrgScope,
    include_quarantined: bool,
    now: datetime,
) -> list[MediaFile]:
    """The caller's last ``RECENT_LIMIT`` uploads from the past day, newest first.

    A file quarantined within a day of upload must not report its filename, status or
    duration here. The exclusions run BEFORE ``.limit()``: SQLAlchemy refuses ``.filter()``
    after a LIMIT.
    """
    own = owned_in_tenant(MediaFile, user_id=user_id, organization_id=organization_id)
    query = (
        _without_heavy_columns(db)
        .filter(own, MediaFile.upload_time >= now - RECENT_WINDOW)
        .order_by(MediaFile.upload_time.desc())
    )
    query = exclude_hidden_for(
        exclude_unavailable(query, include_quarantined=include_quarantined), user_id
    )
    return list(query.limit(RECENT_LIMIT).all())


def tasks_of_file(db: Session, file_id: int) -> list[TaskModel]:
    """Every task row recorded for the file."""
    return list(db.query(TaskModel).filter(TaskModel.media_file_id == file_id).all())


def latest_task_created_since(db: Session, file_id: int, since: datetime) -> TaskModel | None:
    """The most recently created task of the file after ``since``, or ``None``.

    The row, not a count, so a rate-limit refusal can derive a real ``Retry-After`` from it.
    """
    return (
        db.query(TaskModel)
        .filter(TaskModel.media_file_id == file_id, TaskModel.created_at > since)
        .order_by(TaskModel.created_at.desc())
        .first()
    )


def tasks_in_status(db: Session, file_id: int, statuses: list[str]) -> list[TaskModel]:
    """The file's task rows whose status is one of ``statuses``."""
    return list(
        db.query(TaskModel)
        .filter(TaskModel.media_file_id == file_id, TaskModel.status.in_(statuses))
        .all()
    )
