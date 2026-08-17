"""Periodic directory reconciliation (LDAP deprovisioning sweep) settings API (issue #484).

Mirrors ``backup_settings.py``'s pattern: DB-backed ``SystemSettings`` (no ``.env`` vars),
coded defaults in ``core/constants.py``. All routes require a super_admin — same tier as
``admin_group_mappings.py``, since this sweep also reconciles group membership and can
promote/demote privilege, not just disable accounts.

- ``GET  /admin/directory-sync``         — current settings
- ``PUT  /admin/directory-sync``         — update settings (only provided fields)
- ``GET  /admin/directory-sync/status``  — last run / result / next-due
- ``POST /admin/directory-sync/run``     — dispatch a reconciliation pass now
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_superuser
from app.db.base import get_db
from app.services import backup_service
from app.services import directory_sync_service

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Schemas -----------------------------------------------------------------
class DirectorySyncResultModel(BaseModel):
    """Mirrors ``directory_sync_service.sweep_ldap``'s return dict.

    ``extra: allow`` because the service also returns ``actions``/``reconciliations``
    (per-account detail lists) that this summary panel doesn't render.
    """

    status: str | None = None  # ok | directory_unavailable
    error: str | None = None
    dry_run: bool | None = None
    candidates: int | None = None
    checked: int | None = None
    disabled: int | None = None
    would_disable: int | None = None
    capped: bool | None = None
    reconciled: int | None = None
    max_disables_per_run: int | None = None
    started_at: str | None = None
    finished_at: str | None = None

    model_config = {"extra": "allow"}


class DirectorySyncSettings(BaseModel):
    enabled: bool
    schedule: str
    dry_run: bool
    max_disables_per_run: int
    last_run_at: str | None = None
    last_result: DirectorySyncResultModel | None = None


class DirectorySyncSettingsUpdate(BaseModel):
    enabled: bool | None = None
    schedule: str | None = None
    dry_run: bool | None = None
    max_disables_per_run: int | None = Field(default=None, ge=1, le=100_000)


class DirectorySyncStatus(BaseModel):
    enabled: bool
    schedule: str
    dry_run: bool
    max_disables_per_run: int
    last_run_at: str | None = None
    last_result: DirectorySyncResultModel | None = None
    next_due: bool


class DirectorySyncRunResponse(BaseModel):
    task_id: str
    status: str
    message: str


# --- Helpers -----------------------------------------------------------------
def _settings_response(cfg: dict) -> DirectorySyncSettings:
    return DirectorySyncSettings(**cfg)


# --- Routes ------------------------------------------------------------------
@router.get("", response_model=DirectorySyncSettings)
def get_directory_sync_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> DirectorySyncSettings:
    """Return periodic directory-reconciliation settings (super_admin only)."""
    return _settings_response(directory_sync_service.get_settings(db))


@router.put("", response_model=DirectorySyncSettings)
def update_directory_sync_settings(
    body: DirectorySyncSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> DirectorySyncSettings:
    """Update periodic directory-reconciliation settings (only provided fields)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    try:
        cfg = directory_sync_service.update_settings(db, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Admin %s updated directory-sync settings: %s", current_user.email, list(updates))
    return _settings_response(cfg)


@router.get("/status", response_model=DirectorySyncStatus)
def get_directory_sync_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> DirectorySyncStatus:
    """Return last run / result and whether the schedule is currently due."""
    cfg = directory_sync_service.get_settings(db)
    next_due = False
    if cfg["enabled"]:
        try:
            next_due = backup_service.is_due(cfg["schedule"], cfg["last_run_at"], datetime.now(UTC))
        except ValueError:
            next_due = False
    return DirectorySyncStatus(
        enabled=cfg["enabled"],
        schedule=cfg["schedule"],
        dry_run=cfg["dry_run"],
        max_disables_per_run=cfg["max_disables_per_run"],
        last_run_at=cfg["last_run_at"],
        last_result=cfg.get("last_result"),
        next_due=next_due,
    )


@router.post("/run", response_model=DirectorySyncRunResponse)
def run_directory_sync_now(
    current_user: models.User = Depends(get_current_active_superuser),
) -> DirectorySyncRunResponse:
    """Dispatch a reconciliation pass immediately (bypasses the schedule).

    Uses the configured ``dry_run`` setting — same as the scheduled beat dispatch —
    so this is a "run the configured sweep now" action, not a way to bypass dry-run.
    """
    from app.core.constants import CeleryQueues
    from app.core.constants import CPUPriority
    from app.tasks.directory_sync_task import run_directory_sync

    task = run_directory_sync.apply_async(queue=CeleryQueues.CPU, priority=CPUPriority.MAINTENANCE)
    logger.info(
        "Manual directory reconciliation triggered by admin %s (task %s)",
        current_user.email,
        task.id,
    )
    return DirectorySyncRunResponse(
        task_id=str(task.id),
        status="queued",
        message="Directory reconciliation task queued successfully.",
    )
