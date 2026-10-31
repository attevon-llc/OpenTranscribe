"""Task-system API: task listing and lookup (read model).

The admin health report, the recovery triggers and the file repair/retry routes live in
``tasks_system_health``, ``tasks_recovery`` and ``tasks_file_repair``; they are included
below, before the ``/{task_id}`` catch-all, so registration order is unchanged.

Handlers are declared ``def``, not ``async def`` (issue #284 A2.5). Their bodies are
pure blocking work — SQLAlchemy aggregates over ``Task``/``MediaFile``, the detection
and recovery services, Celery ``.delay()`` — with no ``await`` anywhere, so running
them as coroutines pinned the event loop for the duration of each admin sweep. The
same applies to the nested ``BackgroundTasks`` callables: they call the synchronous
``dispatch_transcription_pipeline``, so they too are ``def`` and get threadpooled.
"""

import logging
import math
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy import Row
from sqlalchemy import true
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints import tasks_file_repair
from app.api.endpoints import tasks_recovery
from app.api.endpoints import tasks_system_health
from app.core.exceptions import DomainError
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task as TaskModel
from app.models.user import User
from app.schemas.media import PaginatedTaskResponse
from app.schemas.media import Task
from app.services.permission_service import org_scope_pred
from app.services.platform_bypass import PlatformBypass
from app.services.request_context import RequestContext
from app.services.takedown_service import exclude_quarantined
from app.services.task_filtering_service import TaskFilteringService
from app.utils.db_helpers import owned_in_tenant
from app.utils.uuid_parse import parse_uuid_or_none

logger = logging.getLogger(__name__)

router = APIRouter()


def _visible_in_tenant(
    current_user: User, organization_id: OrgScope, bypass: PlatformBypass
) -> Any:
    """Row predicate for the task surfaces: the caller's own files in the active tenant.

    A bypass that sees every user's rows widens only the owner half; the tenant stamp
    still applies, so it never reaches another tenant's files.
    """
    if bypass.sees_all_in_scope:
        tenant = org_scope_pred(MediaFile.organization_id, organization_id)
        return true() if tenant is None else tenant
    return owned_in_tenant(MediaFile, user_id=current_user.id, organization_id=organization_id)


def _get_user_media_files(
    db: Session,
    current_user: User,
    organization_id: OrgScope = UNSCOPED,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> list[Row]:
    """Get media files based on user permissions.

    Projects only the columns needed for task list display, avoiding
    loading large JSONB columns (summary_data, metadata_raw, waveform_data).
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
    query = db.query(*columns).filter(_visible_in_tenant(current_user, organization_id, bypass))
    # A quarantined file must 404 everywhere under `files/`, so it must not surface
    # in the task list either — the two are the same "does this file exist for you"
    # question. Admin "see all" keeps every row (review needs the quarantined ones).
    query = exclude_quarantined(query, include_quarantined=bypass.user_is_admin)
    return query.all()


def _latest_task_by_file(
    db: Session,
    current_user: User,
    file_id: int | None = None,
    organization_id: OrgScope = UNSCOPED,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> dict[int, Any]:
    """Map ``media_file_id`` to the task row that represents the file's current work.

    A file accumulates one task row per pipeline stage (transcription,
    summarization, speaker_embedding, ...), so "the" task for a file is the one
    the pipeline is currently running — ``MediaFile.active_task_id`` — falling
    back to the most recently created row once that clears.

    Joins through ``MediaFile`` rather than passing an ``IN`` list of file ids so
    the query cost does not scale with the caller's library size.
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
        .filter(_visible_in_tenant(current_user, organization_id, bypass))
        .order_by(TaskModel.media_file_id, TaskModel.created_at)
    )
    # Same quarantine exclusion as `_get_user_media_files` — this query is joined
    # through `MediaFile` independently, so it needs its own copy of the filter.
    query = exclude_quarantined(query, include_quarantined=bypass.user_is_admin)
    if file_id is not None:
        query = query.filter(TaskModel.media_file_id == file_id)

    best: dict[int, Any] = {}
    for row in query.all():
        incumbent = best.get(row.media_file_id)
        if incumbent is None or row.id == row.active_task_id:
            best[row.media_file_id] = row
        elif incumbent.id != incumbent.active_task_id:
            # Rows arrive in created_at order, so a later row is the newer one.
            best[row.media_file_id] = row
    return best


def _map_file_status_to_task_status(file_status: FileStatus) -> str:
    """Map media file status to task status."""
    status_mapping = {
        FileStatus.PENDING: "pending",
        FileStatus.PROCESSING: "in_progress",
        FileStatus.COMPLETED: "completed",
        FileStatus.ERROR: "failed",
    }
    return status_mapping.get(file_status, "pending")


def _extract_file_format(content_type: str, filename: str) -> str | None:
    """Extract file format from content type or filename."""
    if content_type and "/" in content_type:
        return content_type.split("/")[1]
    elif filename and "." in filename:
        return filename.split(".")[-1]
    return None


def _create_task_dict_from_media_file(
    file: Any, current_user: User, task: Any | None = None
) -> dict:
    """Convert a media file row (ORM or named tuple) to a task dictionary.

    When the file's ``task`` row is supplied, every task-shaped field is read
    from it: the pipeline records real Celery ids, real fractional progress and
    real error text there via ``app.utils.task_utils``. Without one, the file's
    own columns are used and progress stays unknown rather than invented — a
    fabricated mid-point renders as a permanently half-full progress bar in the
    UI, which is what this endpoint used to emit for every processing file.
    """
    file_status = file.status
    completed_at = file.completed_at if hasattr(file, "completed_at") else None
    file_format = _extract_file_format(str(file.content_type), str(file.filename))

    if task is not None:
        task_id = task.id
        task_type = task.task_type
        task_status = task.status
        progress = float(task.progress) if task.progress is not None else 0.0
        error_message = task.error_message
        created_at = task.created_at or file.upload_time
        updated_at = task.updated_at or task.created_at or file.upload_time
        completed_at = task.completed_at or completed_at
    else:
        task_id = f"task_{file.id}"
        task_type = "transcription"
        task_status = _map_file_status_to_task_status(file_status)  # type: ignore[arg-type]
        progress = 1.0 if file_status == FileStatus.COMPLETED else 0.0
        error_message = (
            getattr(file, "last_error_message", None) if file_status == FileStatus.ERROR else None
        )
        created_at = file.upload_time
        updated_at = file.upload_time

    # Never put the raw exception text on the wire (issue #786) — a client-facing
    # error_message is a fixed sentence from ErrorCategorizationService, for both the
    # real-task branch (task.error_message) and the synthesized one above.
    from app.services.error_categorization_service import ErrorCategorizationService

    error_message = ErrorCategorizationService.user_message_for(error_message)

    return {
        "id": task_id,
        "user_id": str(current_user.uuid),
        "task_type": task_type,
        "status": task_status,
        "media_file_id": str(file.uuid),
        "progress": progress,
        "created_at": created_at,
        "updated_at": updated_at,
        "completed_at": completed_at,
        "error_message": error_message,
        "media_file": {
            "uuid": str(file.uuid),
            "filename": file.filename,
            "file_size": file.file_size,
            "content_type": file.content_type,
            "duration": file.duration,
            "language": file.language,
            "format": file_format,
            "media_format": file.media_format if hasattr(file, "media_format") else None,
            "codec": file.codec if hasattr(file, "codec") else None,
            "upload_time": file.upload_time,
        },
    }


# =============================================================================
# PROGRESS RECOVERY - For frontend to recover in-flight task progress
# =============================================================================


@router.get("/progress/active", response_model=list[dict[str, Any]])
def get_active_progress(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get all active (running) task progress for the current user.

    Returns ProgressState dicts from Redis so the frontend can recover
    progress bars after page refresh or modal close/reopen. Progress is keyed
    per user, not per tenant, so the per-file detail (``failed_items``, file
    UUIDs) is narrowed to the caller's files in the ACTIVE tenant.
    """
    from app.services.progress_tracker import ProgressTracker

    states = ProgressTracker.get_active_tasks(ctx.user.id)
    if ctx.bypass.sees_all_in_scope:
        return states
    referenced = {u for state in states for u in state.get("failed_items") or []}
    visible: set[str] = set()
    if referenced:
        visible = {
            str(row.uuid)
            for row in db.query(MediaFile.uuid).filter(
                MediaFile.uuid.in_(_valid_uuids(referenced)),
                _visible_in_tenant(ctx.user, ctx.org_id, ctx.bypass),
            )
        }
    for state in states:
        state["failed_items"] = [u for u in state.get("failed_items") or [] if u in visible]
    return states


def _valid_uuids(values: set[str]) -> list[str]:
    """Keep only well-formed UUID strings (``failed_items`` is free-form)."""
    return [str(parsed) for value in values if (parsed := parse_uuid_or_none(value)) is not None]


# =============================================================================
# STATIC AND NESTED ROUTES - Must come before single-param routes
# =============================================================================


@router.get("", response_model=PaginatedTaskResponse)
def list_tasks(
    status: str | None = None,  # Filter by task status
    task_type: str | None = None,  # Filter by task type
    age_filter: str | None = None,  # Filter by age: "today", "week", "month", "older"
    date_from: str | None = None,  # Filter from date (YYYY-MM-DD)
    date_to: str | None = None,  # Filter to date (YYYY-MM-DD)
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    List tasks for the current user with server-side filtering and pagination.

    Scoped to the caller's own files in the ACTIVE tenant (an org's files never
    appear in personal scope, nor another org's in org context).
    """
    current_user = ctx.user
    try:
        # Get media files based on user permissions
        media_files = _get_user_media_files(db, current_user, ctx.org_id, ctx.bypass)

        # Convert media files to task dictionaries, pairing each with its real
        # task row so status/progress/task_type filters act on recorded values.
        tasks_by_file = _latest_task_by_file(
            db, current_user, organization_id=ctx.org_id, bypass=ctx.bypass
        )
        tasks = [
            _create_task_dict_from_media_file(file, current_user, tasks_by_file.get(file.id))
            for file in media_files
        ]

        # Apply server-side filtering
        filtered_tasks = TaskFilteringService.filter_tasks_by_criteria(
            tasks=tasks,
            status=status,
            task_type=task_type,
            age_filter=age_filter,
            date_from=date_from,
            date_to=date_to,
        )

        # Pagination
        total = len(filtered_tasks)
        total_pages = max(1, math.ceil(total / page_size))
        offset = (page - 1) * page_size
        page_items = filtered_tasks[offset : offset + page_size]

        # Convert to Task schema objects
        items = [Task(**task_dict) for task_dict in page_items]

        return PaginatedTaskResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            has_more=page < total_pages,
        )

    except (HTTPException, DomainError):
        # A deliberate status raised below (or by a helper) must reach the client as
        # itself, not be relabelled 500 by the broad handler.
        raise
    except Exception as e:
        # Never answer 200 with an empty page here: a failed query is
        # indistinguishable from "this user has no tasks", so the SPA renders an
        # empty task list and the operator sees nothing wrong (issue #431).
        logger.exception(f"Error in list_tasks: {e}")
        # Literal 500: this handler's `status` query param shadows `fastapi.status`,
        # so `status.HTTP_500_INTERNAL_SERVER_ERROR` here raises AttributeError.
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


router.include_router(tasks_system_health.router)
router.include_router(tasks_recovery.router)
router.include_router(tasks_file_repair.router)


# =============================================================================
# SINGLE-PARAM ROUTES - Must come last
# =============================================================================


def _get_task_row_by_id(
    db: Session,
    task_id: str,
    current_user: User,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> Any | None:
    """Look up a real task row by its Celery id, or ``None`` if there is no such row.

    Returns ``None`` for a row with no ``media_file_id`` so the caller falls
    through to the legacy id path rather than trying to build a file-shaped
    response with no file.
    """
    query = db.query(TaskModel).filter(TaskModel.id == task_id)
    # The row is only a pointer: `_get_media_file_by_id` applies the tenant gate on the
    # file it names, so a widened owner filter here cannot reach another tenant.
    if not bypass.sees_all_in_scope:
        query = query.filter(TaskModel.user_id == current_user.id)
    task = query.first()
    return task if task is not None and task.media_file_id is not None else None


def _parse_task_id(task_id: str) -> int:
    """Parse task ID to extract media file ID."""
    if not task_id.startswith("task_"):
        raise ValueError("Invalid task ID format")
    try:
        return int(task_id.split("_")[1])
    except (ValueError, IndexError) as e:
        raise ValueError("Invalid task ID format") from e


def _get_media_file_by_id(
    db: Session,
    file_id: int,
    current_user: User,
    organization_id: OrgScope = UNSCOPED,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> MediaFile:
    """Get media file by ID with proper permission checking."""
    query = db.query(MediaFile).filter(
        MediaFile.id == file_id,
        _visible_in_tenant(current_user, organization_id, bypass),
    )
    # A quarantined file must 404 on this surface too — see the matching
    # exclusion on `_get_user_media_files`/`_latest_task_by_file` above.
    media_file = exclude_quarantined(query, include_quarantined=bypass.user_is_admin).first()

    if not media_file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    return media_file  # type: ignore[no-any-return]


@router.get("/{task_id}", response_model=Task)
def get_task(
    task_id: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """Get a specific task by its ID.

    Accepts either form of id this API issues: a real Celery task id (what the
    pipeline records, and what ``POST /tasks/system/recover-task/{task_id}``
    expects) or the legacy ``task_<media_file_id>`` form, which is still returned
    for files that have no task row yet. A task whose file belongs to another
    tenant than the active one is reported as not found.
    """
    current_user = ctx.user
    try:
        task_row = _get_task_row_by_id(db, task_id, current_user, ctx.bypass)
        if task_row is not None:
            media_file = _get_media_file_by_id(
                db, task_row.media_file_id, current_user, ctx.org_id, ctx.bypass
            )
            return Task(**_create_task_dict_from_media_file(media_file, current_user, task_row))

        try:
            file_id = _parse_task_id(task_id)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task ID format"
            ) from e

        # Get media file with permission checking
        media_file = _get_media_file_by_id(db, file_id, current_user, ctx.org_id, ctx.bypass)

        # Prefer the file's real task row; the legacy id form only identifies the file.
        task_dict = _create_task_dict_from_media_file(
            media_file,
            current_user,
            _latest_task_by_file(db, current_user, media_file.id, ctx.org_id, ctx.bypass).get(
                media_file.id
            ),
        )
        task_dict["id"] = task_id  # Honour the id form the caller asked with

        return Task(**task_dict)

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.error("Error in get_task: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e
