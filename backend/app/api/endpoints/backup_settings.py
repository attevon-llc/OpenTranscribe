"""Scheduled database backup settings API (admin-gated).

Mirrors the redaction-policy / retention precedent: DB-backed ``SystemSettings`` (no
``.env`` vars), coded defaults in ``core/constants.py``. All routes require an admin user.

- ``GET  /admin/backup``            — current settings + destination mount status
- ``PUT  /admin/backup``            — update settings (only provided fields)
- ``GET  /admin/backup/status``     — last run / result / next-due + mount status
- ``POST /admin/backup/run``        — dispatch a backup now
- ``GET  /admin/backup/list``       — list backup files in the destination
"""

from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_admin_user
from app.db.base import get_db
from app.services import backup_service

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Schemas -----------------------------------------------------------------
class BackupResultModel(BaseModel):
    ok: bool
    status: str
    error: str | None = None
    filename: str | None = None
    path: str | None = None
    size_bytes: int | None = None
    duration_s: float | None = None
    encrypted: bool | None = None
    pruned: list[str] | None = None
    started_at: str | None = None


class DestinationStatus(BaseModel):
    destination: str
    exists: bool
    writable: bool
    mounted: bool


class BackupSettings(BaseModel):
    enabled: bool
    schedule: str
    destination: str
    retention_daily: int
    retention_weekly: int
    retention_monthly: int
    encrypt: bool
    passphrase_file: str
    include_opensearch: bool
    last_run_at: str | None = None
    last_result: BackupResultModel | None = None
    destination_status: DestinationStatus


class BackupSettingsUpdate(BaseModel):
    enabled: bool | None = None
    schedule: str | None = None
    destination: str | None = None
    retention_daily: int | None = Field(default=None, ge=0, le=3650)
    retention_weekly: int | None = Field(default=None, ge=0, le=520)
    retention_monthly: int | None = Field(default=None, ge=0, le=600)
    encrypt: bool | None = None
    passphrase_file: str | None = None
    include_opensearch: bool | None = None


class BackupStatus(BaseModel):
    enabled: bool
    schedule: str
    last_run_at: str | None = None
    last_result: BackupResultModel | None = None
    next_due: bool
    destination_status: DestinationStatus
    pg_dump_available: bool


class BackupRunResponse(BaseModel):
    task_id: str
    status: str
    message: str


class BackupFile(BaseModel):
    filename: str
    size_bytes: int
    created_at: str
    encrypted: bool


class BackupListResponse(BaseModel):
    backups: list[BackupFile]
    destination_status: DestinationStatus


# --- Helpers -----------------------------------------------------------------
def _settings_response(cfg: dict) -> BackupSettings:
    return BackupSettings(
        **{k: v for k, v in cfg.items() if k != "last_result"},
        last_result=cfg.get("last_result"),
        destination_status=DestinationStatus(
            **backup_service.destination_status(cfg["destination"])
        ),
    )


# --- Routes ------------------------------------------------------------------
@router.get("", response_model=BackupSettings)
def get_backup_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> BackupSettings:
    """Return scheduled-backup settings + destination mount status (admin only)."""
    return _settings_response(backup_service.get_settings(db))


@router.put("", response_model=BackupSettings)
def update_backup_settings(
    body: BackupSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> BackupSettings:
    """Update scheduled-backup settings (only provided fields)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    try:
        cfg = backup_service.update_settings(db, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Admin %s updated backup settings: %s", current_user.email, list(updates))
    return _settings_response(cfg)


@router.get("/status", response_model=BackupStatus)
def get_backup_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> BackupStatus:
    """Return last run / result and whether the schedule is currently due."""
    cfg = backup_service.get_settings(db)
    next_due = False
    if cfg["enabled"]:
        try:
            next_due = backup_service.is_due(
                cfg["schedule"], cfg["last_run_at"], datetime.now(timezone.utc)
            )
        except ValueError:
            next_due = False
    return BackupStatus(
        enabled=cfg["enabled"],
        schedule=cfg["schedule"],
        last_run_at=cfg["last_run_at"],
        last_result=cfg.get("last_result"),
        next_due=next_due,
        destination_status=DestinationStatus(
            **backup_service.destination_status(cfg["destination"])
        ),
        pg_dump_available=backup_service.pg_dump_available(),
    )


@router.post("/run", response_model=BackupRunResponse)
def run_backup_now(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> BackupRunResponse:
    """Dispatch a backup immediately (bypasses the schedule)."""
    from app.tasks.backup_tasks import run_backup

    task = run_backup.apply_async(queue="utility")
    logger.info("Manual backup triggered by admin %s (task %s)", current_user.email, task.id)
    return BackupRunResponse(
        task_id=str(task.id), status="queued", message="Backup task queued successfully."
    )


@router.get("/list", response_model=BackupListResponse)
def list_backup_files(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> BackupListResponse:
    """List existing backup files in the configured destination, newest first."""
    cfg = backup_service.get_settings(db)
    return BackupListResponse(
        backups=[BackupFile(**b) for b in backup_service.list_backups(cfg["destination"])],
        destination_status=DestinationStatus(
            **backup_service.destination_status(cfg["destination"])
        ),
    )
