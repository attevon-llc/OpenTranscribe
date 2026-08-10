"""Media mirror settings API (admin-gated, issue #242).

Mirrors the scheduled-backup settings precedent: DB-backed ``SystemSettings``
(``backup.mirror_*``, coded defaults in ``core/constants.py``, no ``.env`` vars
beyond the physical mount). All routes require an admin user.

- ``GET  /admin/backup/mirror``          — settings + destination status + last run
- ``PUT  /admin/backup/mirror``          — update settings (only provided fields)
- ``POST /admin/backup/mirror/run``      — dispatch a mirror run now (download queue)
- ``POST /admin/backup/mirror/test-s3``  — test destination bucket reachability
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app import models

# Deployment configuration is the super_admin tier: this router
# holds the mirror destination and its stored S3 secret.
from app.api.endpoints.auth import get_current_active_superuser
from app.db.base import get_db
from app.services import backup_service
from app.services import media_mirror_service

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Schemas -----------------------------------------------------------------
class MirrorResultModel(BaseModel):
    """Outcome of one media mirror run (persisted as ``backup.mirror_last_result``)."""

    ok: bool
    status: str  # success | no_destination | error
    error: str | None = None
    objects_scanned: int | None = None
    objects_excluded: int | None = None
    objects_copied: int | None = None
    objects_skipped: int | None = None
    objects_failed: int | None = None
    bytes_copied: int | None = None
    duration_s: float | None = None
    started_at: str | None = None
    # Bounded sample of per-object error messages (counts above stay exact).
    errors: list[str] | None = None


class MirrorDestinationStatus(BaseModel):
    destination: str
    exists: bool
    writable: bool
    mounted: bool


class MirrorS3Status(BaseModel):
    bucket: str
    prefix: str
    endpoint_url: str
    reachable: bool
    error: str | None = None


class MediaMirrorSettings(BaseModel):
    enabled: bool
    schedule: str
    destination_type: str
    destination: str
    throttle_ms: int
    s3_endpoint_url: str
    s3_region: str
    s3_bucket: str
    s3_prefix: str
    s3_access_key_id: str
    # Secret is write-only — only the *_set bool is exposed.
    s3_secret_key_set: bool
    last_run_at: str | None = None
    last_result: MirrorResultModel | None = None
    destination_status: MirrorDestinationStatus
    s3_status: MirrorS3Status | None = None
    # True while a mirror run holds the Redis overlap lock.
    running: bool = False


class MediaMirrorSettingsUpdate(BaseModel):
    enabled: bool | None = None
    schedule: str | None = None
    destination_type: str | None = None
    destination: str | None = None
    throttle_ms: int | None = Field(default=None, ge=0, le=60_000)
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_bucket: str | None = None
    s3_prefix: str | None = None
    s3_access_key_id: str | None = None
    # Write-only: accepted on PUT (encrypted at rest), never returned on GET.
    s3_secret_key: str | None = None


class MirrorRunResponse(BaseModel):
    task_id: str
    status: str
    message: str


class MirrorS3TestResponse(BaseModel):
    ok: bool
    error: str | None = None
    bucket: str | None = None


# --- Helpers -----------------------------------------------------------------
def _s3_status(cfg: dict, db: Session) -> MirrorS3Status | None:
    """Build the S3 reachability status (only when the S3 destination is selected)."""
    if cfg.get("destination_type") != media_mirror_service.DEST_S3:
        return None
    return MirrorS3Status(**media_mirror_service.s3_bucket_status(cfg, db))


def _settings_response(cfg: dict, db: Session) -> MediaMirrorSettings:
    from app.utils.task_lock import task_lock_manager

    return MediaMirrorSettings(
        **{k: v for k, v in cfg.items() if k != "last_result"},
        last_result=cfg.get("last_result"),
        destination_status=MirrorDestinationStatus(
            **backup_service.destination_status(cfg["destination"])
        ),
        s3_status=_s3_status(cfg, db),
        running=task_lock_manager.is_locked(media_mirror_service.MIRROR_LOCK_KEY),
    )


# --- Routes ------------------------------------------------------------------
@router.get("", response_model=MediaMirrorSettings)
def get_mirror_settings(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> MediaMirrorSettings:
    """Return media-mirror settings + destination status + last run (admin only)."""
    return _settings_response(media_mirror_service.get_settings(db), db)


@router.put("", response_model=MediaMirrorSettings)
def update_mirror_settings(
    body: MediaMirrorSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> MediaMirrorSettings:
    """Update media-mirror settings (only provided fields)."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")
    try:
        cfg = media_mirror_service.update_settings(db, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Never log the secret value itself — only the field names.
    logger.info("Admin %s updated media mirror settings: %s", current_user.email, list(updates))
    return _settings_response(cfg, db)


@router.post("/test-s3", response_model=MirrorS3TestResponse)
def test_mirror_s3_connection(
    body: MediaMirrorSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> MirrorS3TestResponse:
    """Test destination-bucket reachability with saved (or just-submitted) credentials.

    S3 fields provided in the body override the stored settings for the probe; a
    just-submitted secret is tested in-place without being persisted. Never raises.
    """
    cfg = media_mirror_service.get_settings(db)
    provided = body.model_dump(exclude_none=True)
    for k in ("s3_endpoint_url", "s3_region", "s3_bucket", "s3_prefix", "s3_access_key_id"):
        if k in provided:
            cfg[k] = provided[k]
    result = media_mirror_service.test_s3_connection(
        cfg, db, override_secret=provided.get("s3_secret_key")
    )
    logger.info(
        "Admin %s tested media mirror S3 connection: ok=%s", current_user.email, result["ok"]
    )
    return MirrorS3TestResponse(
        ok=result["ok"], error=result.get("error"), bucket=result.get("bucket")
    )


@router.post("/run", response_model=MirrorRunResponse)
def run_mirror_now(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> MirrorRunResponse:
    """Dispatch a media mirror run immediately (bypasses the schedule).

    The run task itself holds the Redis overlap lock, so a dispatch while another
    run is active records a no-op skip rather than a second concurrent mirror.
    """
    from app.core.constants import CeleryQueues
    from app.core.constants import DownloadPriority
    from app.tasks.backup_tasks import run_media_mirror

    task = run_media_mirror.apply_async(
        queue=CeleryQueues.DOWNLOAD, priority=DownloadPriority.PLAYLIST
    )
    logger.info("Manual media mirror triggered by admin %s (task %s)", current_user.email, task.id)
    return MirrorRunResponse(
        task_id=str(task.id), status="queued", message="Media mirror task queued successfully."
    )
