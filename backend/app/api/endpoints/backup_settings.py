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


class S3Status(BaseModel):
    bucket: str
    prefix: str
    endpoint_url: str
    reachable: bool
    error: str | None = None


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
    # Destination selection + S3 (secret is write-only — only the *_set bool is exposed).
    destination_type: str
    s3_endpoint_url: str
    s3_region: str
    s3_bucket: str
    s3_prefix: str
    s3_access_key_id: str
    s3_secret_key_set: bool
    last_run_at: str | None = None
    last_result: BackupResultModel | None = None
    destination_status: DestinationStatus
    s3_status: S3Status | None = None


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
    destination_type: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_bucket: str | None = None
    s3_prefix: str | None = None
    s3_access_key_id: str | None = None
    # Write-only: accepted on PUT (encrypted at rest), never returned on GET.
    s3_secret_key: str | None = None


class BackupStatus(BaseModel):
    enabled: bool
    schedule: str
    destination_type: str
    last_run_at: str | None = None
    last_result: BackupResultModel | None = None
    next_due: bool
    destination_status: DestinationStatus
    s3_status: S3Status | None = None
    pg_dump_available: bool


class S3ConnectionTestResponse(BaseModel):
    ok: bool
    error: str | None = None
    bucket: str | None = None


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
    s3_status: S3Status | None = None


# --- Helpers -----------------------------------------------------------------
def _s3_status(cfg: dict, db: Session) -> S3Status | None:
    """Build the S3 reachability status (only when the S3 destination is selected)."""
    if cfg.get("destination_type") != backup_service.DEST_S3:
        return None
    raw = backup_service.s3_bucket_status(cfg, db)
    return S3Status(
        bucket=raw["bucket"],
        prefix=raw["prefix"],
        endpoint_url=raw["endpoint_url"],
        reachable=raw["reachable"],
        error=raw.get("error"),
    )


def _settings_response(cfg: dict, db: Session) -> BackupSettings:
    return BackupSettings(
        **{k: v for k, v in cfg.items() if k != "last_result"},
        last_result=cfg.get("last_result"),
        destination_status=DestinationStatus(
            **backup_service.destination_status(cfg["destination"])
        ),
        s3_status=_s3_status(cfg, db),
    )


# --- Routes ------------------------------------------------------------------
@router.get("", response_model=BackupSettings)
def get_backup_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> BackupSettings:
    """Return scheduled-backup settings + destination mount status (admin only)."""
    return _settings_response(backup_service.get_settings(db), db)


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
    # Never log the secret value itself — only the field name.
    logger.info("Admin %s updated backup settings: %s", current_user.email, list(updates))
    return _settings_response(cfg, db)


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
        destination_type=cfg["destination_type"],
        last_run_at=cfg["last_run_at"],
        last_result=cfg.get("last_result"),
        next_due=next_due,
        destination_status=DestinationStatus(
            **backup_service.destination_status(cfg["destination"])
        ),
        s3_status=_s3_status(cfg, db),
        pg_dump_available=backup_service.pg_dump_available(),
    )


@router.post("/test-s3", response_model=S3ConnectionTestResponse)
def test_s3_connection(
    body: BackupSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_admin_user),
) -> S3ConnectionTestResponse:
    """Test S3 reachability with the saved (or just-submitted) credentials.

    Uses any S3 fields provided in the body, falling back to the stored settings; the
    secret key falls back to the stored encrypted one when omitted. Returns an ok/error
    envelope (never raises). Admin only.
    """
    cfg = backup_service.get_settings(db)
    provided = body.model_dump(exclude_none=True)
    for k in (
        "s3_endpoint_url",
        "s3_region",
        "s3_bucket",
        "s3_prefix",
        "s3_access_key_id",
    ):
        if k in provided:
            cfg[k] = provided[k]
    # A just-submitted secret is tested in-place (not persisted); else use the stored one.
    result = backup_service.test_s3_connection(
        cfg, db, override_secret=provided.get("s3_secret_key")
    )
    logger.info("Admin %s tested S3 backup connection: ok=%s", current_user.email, result["ok"])
    return S3ConnectionTestResponse(
        ok=result["ok"], error=result.get("error"), bucket=result.get("bucket")
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
    if cfg["destination_type"] == backup_service.DEST_S3:
        files = backup_service.list_backups_s3(cfg, db)
    else:
        files = backup_service.list_backups(cfg["destination"])
    return BackupListResponse(
        backups=[BackupFile(**b) for b in files],
        destination_status=DestinationStatus(
            **backup_service.destination_status(cfg["destination"])
        ),
        s3_status=_s3_status(cfg, db),
    )
