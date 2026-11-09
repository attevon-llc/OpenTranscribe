"""Queries behind the task-system API: listing, lookup, health counts and file repair.

``visible_in_tenant`` is the row predicate every task surface shares: the caller's own files
in the active tenant. A bypass that sees every user's rows widens only the owner half; the
tenant stamp still applies, so it never reaches another tenant's files.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import true
from sqlalchemy.orm import Session

from app.core.tenancy import OrgScope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.models.user import User
from app.services.file_availability import exclude_unavailable
from app.services.file_availability import unavailable_list_exempt
from app.services.permission_service import org_scope_pred
from app.services.platform_bypass import PlatformBypass
from app.utils.db_helpers import owned_in_tenant
from app.utils.task_utils import TASK_STATUS_COMPLETED
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import TASK_STATUS_IN_PROGRESS
from app.utils.task_utils import TASK_STATUS_PENDING


def visible_in_tenant(current_user: User, organization_id: OrgScope, bypass: PlatformBypass) -> Any:
    """Row predicate for the task surfaces: the caller's own files in the active tenant."""
    if bypass.sees_all_in_scope:
        tenant = org_scope_pred(MediaFile.organization_id, organization_id)
        return true() if tenant is None else tenant
    return owned_in_tenant(MediaFile, user_id=current_user.id, organization_id=organization_id)


def task_rows_with_active_id(
    db: Session,
    current_user: User,
    organization_id: OrgScope,
    bypass: PlatformBypass,
    file_id: int | None = None,
) -> list[Any]:
    """The visible task rows, each carrying its file's ``active_task_id``, oldest first per file.

    Joins through ``MediaFile`` rather than passing an ``IN`` list of file ids so the cost
    does not scale with the caller's library size. The quarantine exclusion is applied here
    independently of ``visible_files_query``, because this query joins ``MediaFile`` itself.
    """
    query = (
        db.query(
            TaskModel.id,
            TaskModel.media_file_id,
            TaskModel.task_type,
            TaskModel.status,
            TaskModel.progress,
            TaskModel.error_message,
            TaskModel.created_at,
            TaskModel.updated_at,
            TaskModel.completed_at,
            MediaFile.active_task_id,
        )
        .join(MediaFile, TaskModel.media_file_id == MediaFile.id)
        .filter(visible_in_tenant(current_user, organization_id, bypass))
        .order_by(TaskModel.media_file_id, TaskModel.created_at)
    )
    query = exclude_unavailable(query, include_quarantined=unavailable_list_exempt(bypass))
    if file_id is not None:
        query = query.filter(TaskModel.media_file_id == file_id)
    rows: list[Any] = query.all()
    return rows


def visible_file_uuids(
    db: Session,
    candidates: Collection[str],
    current_user: User,
    organization_id: OrgScope,
    bypass: PlatformBypass,
) -> set[str]:
    """The subset of ``candidates`` (well-formed uuids) that name files the caller may see."""
    return {
        str(row.uuid)
        for row in db.query(MediaFile.uuid).filter(
            MediaFile.uuid.in_(list(candidates)),
            visible_in_tenant(current_user, organization_id, bypass),
        )
    }


def find_task_row(
    db: Session, task_id: str, current_user: User, bypass: PlatformBypass
) -> TaskModel | None:
    """The task row with this Celery id that the caller may point at, or ``None``.

    The row is only a pointer: the media-file lookup applies the tenant gate on the file it
    names, so a widened owner filter here cannot reach another tenant.
    """
    query = db.query(TaskModel).filter(TaskModel.id == task_id)
    if not bypass.sees_all_in_scope:
        query = query.filter(TaskModel.user_id == current_user.id)
    return query.first()


def find_visible_media_file(
    db: Session,
    file_id: int,
    current_user: User,
    organization_id: OrgScope,
    bypass: PlatformBypass,
) -> MediaFile | None:
    """The file with this id when the caller may see it; a quarantined file reads as absent."""
    query = db.query(MediaFile).filter(
        MediaFile.id == file_id,
        visible_in_tenant(current_user, organization_id, bypass),
    )
    media_file: MediaFile | None = exclude_unavailable(
        query, include_quarantined=unavailable_list_exempt(bypass)
    ).first()
    return media_file


def get_task(db: Session, task_id: str) -> TaskModel | None:
    """The task row with this id, whoever owns it (admin recovery surface)."""
    return db.query(TaskModel).filter(TaskModel.id == task_id).first()


def task_status_counts(db: Session) -> Any:
    """One row: total tasks and the count in each of the four task states."""
    return db.query(
        func.count(TaskModel.id).label("total"),
        func.count(case((TaskModel.status == TASK_STATUS_PENDING, 1))).label("pending"),
        func.count(case((TaskModel.status == TASK_STATUS_IN_PROGRESS, 1))).label("in_progress"),
        func.count(case((TaskModel.status == TASK_STATUS_COMPLETED, 1))).label("completed"),
        func.count(case((TaskModel.status == TASK_STATUS_FAILED, 1))).label("failed"),
    ).one()


def file_status_counts(db: Session) -> Any:
    """One row: total files and the count in each of the four reported file states."""
    return db.query(
        func.count(MediaFile.id).label("total"),
        func.count(case((MediaFile.status == FileStatus.PENDING, 1))).label("pending"),
        func.count(case((MediaFile.status == FileStatus.PROCESSING, 1))).label("processing"),
        func.count(case((MediaFile.status == FileStatus.COMPLETED, 1))).label("completed"),
        func.count(case((MediaFile.status == FileStatus.ERROR, 1))).label("error"),
    ).one()


def open_tasks_of_file(db: Session, file_id: int) -> list[TaskModel]:
    """The file's tasks that are still pending or in progress."""
    return list(
        db.query(TaskModel)
        .filter(
            TaskModel.media_file_id == file_id,
            TaskModel.status.in_([TASK_STATUS_PENDING, TASK_STATUS_IN_PROGRESS]),
        )
        .all()
    )
