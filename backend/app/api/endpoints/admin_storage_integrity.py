"""Admin routes for the storage-object orphan sweep (issue #1212).

The object-store twin of ``/admin/data-integrity`` (the OpenSearch reconcile in
``admin.py``): a read-only dry run that names what a sweep would remove, and a delete that
needs an explicit ``confirm=true``. Same permission tier as the index reconcile (admin),
same ratio-guard override (``force=true``, which also needs ``confirm=true``).

Responses carry counters and numeric ids only; a failure is a fixed code, never exception
text. The deletion itself runs in a Celery task (``storage_orphan_sweep_task``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from pydantic import BaseModel

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()


class StorageOrphanFile(BaseModel):
    """One file whose row is gone but whose objects remain."""

    file_id: int
    user_id: int
    objects: int
    bytes: int


class StorageOrphanReport(BaseModel):
    """What a storage sweep found (dry run) or did (last run)."""

    dry_run: bool
    force: bool
    refused: str | None = None
    error: str | None = None
    total_objects: int
    total_bytes: int
    orphan_files: int
    orphan_objects: int
    orphan_bytes: int
    orphans: list[StorageOrphanFile]
    orphans_truncated: bool
    skipped_recent_files: int
    rechecked_present: int
    deleted_files: int
    deleted_objects: int
    deleted_bytes: int
    failed_objects: int


class StorageSweepLastRun(BaseModel):
    timestamp: float
    duration_seconds: float
    results: StorageOrphanReport


class StorageSweepStatus(BaseModel):
    running: bool
    last_run: StorageSweepLastRun | None = None


class StorageSweepStarted(BaseModel):
    status: str
    task_id: str | None = None
    force: bool = False


@router.get("/data-integrity/storage/counts", response_model=StorageOrphanReport)
def get_storage_orphan_counts(
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Dry run: list storage objects whose ``media_file`` row no longer exists.

    Deletes nothing. The refusals a real run would apply (``empty_valid_set``,
    ``ratio_guard``) are evaluated and reported in ``refused``.
    """
    from app.tasks.storage_orphan_sweep_task import get_storage_orphan_counts as scan

    return scan()


@router.get("/data-integrity/storage/status", response_model=StorageSweepStatus)
def get_storage_sweep_status(
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Whether a storage sweep is running, and the result of the last one."""
    from app.tasks.storage_orphan_sweep_task import get_storage_sweep_status as read

    return read()


@router.post("/data-integrity/storage", response_model=StorageSweepStarted)
def start_storage_orphan_sweep(
    request: Request,
    confirm: bool = Query(
        False, description="Required: this deletes objects. Review the dry run first."
    ),
    force: bool = Query(
        False,
        description=(
            "Override the ratio guard (a sweep may otherwise delete at most 10% of the "
            "file-shaped objects). Never overrides the empty-database refusal."
        ),
    ),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Delete the storage objects of files that no longer exist.

    Refuses without ``confirm=true``. Review ``GET /admin/data-integrity/storage/counts``
    first. Every file id is re-checked against Postgres right before its objects are
    removed, an empty Postgres side is refused outright, and objects younger than the grace
    period are never touched. Audit-logged as ``admin.data_integrity.storage_purge`` (at
    request, and again on completion with counters).
    """
    ctx.bypass.require_deployment_scope("The storage orphan sweep")
    from app.tasks.storage_orphan_sweep_task import get_storage_sweep_status as read
    from app.tasks.storage_orphan_sweep_task import storage_orphan_sweep_task

    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This deletes storage objects. Review GET /admin/data-integrity/storage/counts, "
                "then retry with confirm=true."
            ),
        )

    if read().get("running"):
        return {"status": "already_running", "force": force}

    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_DATA_INTEGRITY_STORAGE_PURGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"action": "storage_purge_requested", "force": force},
    )

    result = storage_orphan_sweep_task.delay(user_id=int(current_user.id), force=force)
    return {"status": "started", "task_id": str(result.id), "force": force}
