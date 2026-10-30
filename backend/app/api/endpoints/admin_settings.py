"""Admin settings routes: retry, garbage cleanup, retention, cache, protected media sources."""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_admin_user
from app.core.queues import CeleryQueues
from app.db.base import get_db
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.admin import CacheClearResponse
from app.schemas.admin import CacheConfig
from app.schemas.admin import CacheConfigUpdate
from app.schemas.admin import GarbageCleanupConfig
from app.schemas.admin import GarbageCleanupConfigUpdate
from app.schemas.admin import MediaSource
from app.schemas.admin import MediaSourceCreate
from app.schemas.admin import MediaSourcesList
from app.schemas.admin import MediaSourceUpdate
from app.schemas.admin import RetentionConfig
from app.schemas.admin import RetentionConfigUpdate
from app.schemas.admin import RetentionPreviewFile
from app.schemas.admin import RetentionPreviewResponse
from app.schemas.admin import RetentionRunResponse
from app.schemas.admin import RetryConfig
from app.schemas.admin import RetryConfigUpdate
from app.services import system_settings_service
from app.services.platform_bypass import audit_metadata_access
from app.services.request_context import RequestContext

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings/retry-config", response_model=RetryConfig)
def get_retry_configuration(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetryConfig:
    """
    Get retry configuration settings (admin only).

    Returns the current retry configuration including:
    - max_retries: Maximum retry attempts (0 = unlimited)
    - retry_limit_enabled: Whether limits are enforced
    """
    logger.info(f"Retry config requested by admin {current_user.email}")
    config = system_settings_service.get_retry_config(db)
    return RetryConfig(**config)


@router.put("/settings/retry-config", response_model=RetryConfig)
def update_retry_configuration(
    config: RetryConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetryConfig:
    """
    Update retry configuration settings (admin only).

    Args:
        config: New configuration values (only provided values are updated)

    Returns:
        Updated retry configuration
    """
    logger.info(f"Retry config update by admin {current_user.email}: {config}")

    updated = system_settings_service.update_retry_config(
        db,
        max_retries=config.max_retries,
        retry_limit_enabled=config.retry_limit_enabled,
    )

    return RetryConfig(**updated)


@router.get("/settings/garbage-cleanup", response_model=GarbageCleanupConfig)
def get_garbage_cleanup_configuration(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> GarbageCleanupConfig:
    """
    Get garbage cleanup configuration settings (admin only).

    Returns the current garbage cleanup configuration including:
    - garbage_cleanup_enabled: Whether garbage cleanup is active
    - max_word_length: Maximum word length threshold (words longer are replaced)
    """
    logger.info(f"Garbage cleanup config requested by admin {current_user.email}")
    config = system_settings_service.get_garbage_cleanup_config(db)
    return GarbageCleanupConfig(**config)


@router.put("/settings/garbage-cleanup", response_model=GarbageCleanupConfig)
def update_garbage_cleanup_configuration(
    config: GarbageCleanupConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> GarbageCleanupConfig:
    """
    Update garbage cleanup configuration settings (admin only).

    Args:
        config: New configuration values (only provided values are updated)

    Returns:
        Updated garbage cleanup configuration
    """
    logger.info(f"Garbage cleanup config update by admin {current_user.email}: {config}")

    updated = system_settings_service.update_garbage_cleanup_config(
        db,
        garbage_cleanup_enabled=config.garbage_cleanup_enabled,
        max_word_length=config.max_word_length,
    )

    return GarbageCleanupConfig(**updated)


# ============== File Retention Settings ==============


def _get_retention_eligible_files(db: Session, retention_days: int, delete_error_files: bool):
    """Query files eligible for deletion under the given retention parameters."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    eligible_statuses = [FileStatus.COMPLETED]
    if delete_error_files:
        eligible_statuses.append(FileStatus.ERROR)

    return (
        db.query(MediaFile)
        .options(selectinload(MediaFile.speakers))
        .filter(
            or_(
                and_(MediaFile.completed_at.isnot(None), MediaFile.completed_at < cutoff),
                and_(MediaFile.completed_at.is_(None), MediaFile.upload_time < cutoff),
            ),
            MediaFile.status.in_([s.value for s in eligible_statuses]),
        )
        .all()
    )


@router.get("/settings/retention-config", response_model=RetentionConfig)
def get_retention_configuration(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionConfig:
    """
    Get file retention configuration settings (admin only).

    Returns the current retention configuration including:
    - retention_enabled: Whether automatic deletion is active
    - retention_days: Files older than this are deleted
    - delete_error_files: Whether error-status files are also deleted
    - run_time: HH:MM daily schedule time
    - timezone: IANA timezone for the schedule
    - last_run: ISO timestamp of last run
    - last_run_deleted: Files deleted in last run
    """
    logger.info(f"Retention config requested by admin {current_user.email}")
    config = system_settings_service.get_retention_config(db)
    return RetentionConfig(**config)


@router.put("/settings/retention-config", response_model=RetentionConfig)
def update_retention_configuration(
    config: RetentionConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionConfig:
    """
    Update file retention configuration settings (admin only).

    Args:
        config: New configuration values (only provided values are updated)

    Returns:
        Updated retention configuration
    """
    logger.info(f"Retention config update by admin {current_user.email}: {config}")

    updated = system_settings_service.update_retention_config(
        db,
        retention_enabled=config.retention_enabled,
        retention_days=config.retention_days,
        delete_error_files=config.delete_error_files,
        run_time=config.run_time,
        timezone=config.timezone,
    )

    return RetentionConfig(**updated)


@router.get("/settings/cache-config", response_model=CacheConfig)
def get_cache_configuration(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> CacheConfig:
    """Get derived-cache retention + current usage (admin only).

    Derived assets (subtitle-embedded videos + extracted audio) are regenerable
    duplicates of the originals. They auto-expire via a MinIO lifecycle rule whose
    retention is set here; this also reports how much disk the cache currently uses.
    """
    from app.services import cache_management_service

    stats = cache_management_service.get_cache_stats()
    return CacheConfig(
        retention_days=cache_management_service.resolve_retention_days(db),
        **stats,
    )


@router.put("/settings/cache-config", response_model=CacheConfig)
def update_cache_configuration(
    config: CacheConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> CacheConfig:
    """Update the derived-cache retention window (admin only).

    Applied live by re-setting the MinIO lifecycle rule — no redeploy needed.
    """
    from app.services import cache_management_service

    logger.info(
        f"Cache retention update by admin {current_user.email}: {config.retention_days} day(s)"
    )
    days = cache_management_service.set_retention_days(db, config.retention_days)
    stats = cache_management_service.get_cache_stats()
    return CacheConfig(retention_days=days, **stats)


@router.post("/settings/cache-config/clear", response_model=CacheClearResponse)
def clear_cache_now(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> CacheClearResponse:
    """Immediately purge all derived cache assets (admin only).

    Safe: derived assets are regenerated on the next download. Originals are untouched.
    """
    ctx.bypass.require_deployment_scope("Clearing the derived cache")
    from app.services import cache_management_service

    logger.info(f"Manual derived-cache clear by admin {current_user.email}")
    deleted = cache_management_service.clear_derived_cache()
    return CacheClearResponse(deleted=deleted)


@router.get("/settings/retention-config/preview", response_model=RetentionPreviewResponse)
def preview_retention_deletion(
    retention_days: int = Query(..., ge=1, le=3650, description="Retention window in days"),
    delete_error_files: bool = Query(False, description="Include error-status files"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionPreviewResponse:
    """
    Dry-run preview of files that would be deleted (admin only).

    Returns count, total size, and a sample list of files that would be deleted
    with the given retention parameters. No files are modified.
    """
    logger.info(
        f"Retention preview requested by admin {current_user.email}: "
        f"{retention_days} days, delete_error_files={delete_error_files}"
    )

    files = _get_retention_eligible_files(db, retention_days, delete_error_files)
    audit_metadata_access(
        db,
        current_user,
        route="/api/admin/settings/retention-config/preview",
        count=len(files),
        organization_ids=(f.organization_id for f in files),
    )

    # Build user lookup for owner names
    user_ids = list({f.user_id for f in files})
    users = db.query(User).filter(User.id.in_(user_ids)).all()
    user_map = {u.id: u.email for u in users}

    now_utc = datetime.now(UTC)
    total_size = sum(f.file_size or 0 for f in files)

    # Build preview list (cap at 100 rows for response size)
    preview_files = []
    for f in files[:100]:
        if f.completed_at:
            ref_dt = (
                f.completed_at.replace(tzinfo=UTC)
                if f.completed_at.tzinfo is None
                else f.completed_at
            )
        else:
            ref_dt = (
                f.upload_time.replace(tzinfo=UTC) if f.upload_time.tzinfo is None else f.upload_time
            )
        age_days = (now_utc - ref_dt).days
        preview_files.append(
            RetentionPreviewFile(
                uuid=str(f.uuid),
                title=f.filename or str(f.uuid),
                owner_email=user_map.get(f.user_id, "unknown"),
                completed_at=f.completed_at.isoformat() if f.completed_at else None,
                age_days=age_days,
                size_bytes=f.file_size or 0,
                status=f.status if isinstance(f.status, str) else f.status.value,
            )
        )

    return RetentionPreviewResponse(
        file_count=len(files),
        total_size_bytes=total_size,
        files=preview_files,
    )


@router.post("/settings/retention-config/run", response_model=RetentionRunResponse)
def trigger_retention_run(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionRunResponse:
    """
    Manually trigger a retention cleanup run (admin only).

    Dispatches the cleanup task immediately with force=True, bypassing
    the time-window check and the retention_enabled flag. Useful for
    on-demand cleanup without enabling the automatic schedule.
    """
    ctx.bypass.require_deployment_scope("Running retention cleanup")
    logger.info(f"Manual retention run triggered by admin {current_user.email}")

    from app.core.celery import celery_app

    task = celery_app.send_task(
        "cleanup_expired_files",
        kwargs={"force": True},
        queue=CeleryQueues.UTILITY,
    )

    return RetentionRunResponse(
        task_id=str(task.id),
        status="queued",
        message="Retention cleanup task queued successfully.",
    )


@router.get("/settings/retention-config/status", response_model=RetentionConfig)
def get_retention_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionConfig:
    """
    Get retention configuration with last-run status (admin only).

    Same as GET /retention-config but intended for status polling after
    a manual run to refresh last_run and last_run_deleted values.
    """
    config = system_settings_service.get_retention_config(db)
    return RetentionConfig(**config)


# ====== Protected Media Sources (one system-wide JSON list; user_settings has per-user rows) ======


@router.get("/settings/media-sources", response_model=MediaSourcesList)
def get_media_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> MediaSourcesList:
    """Get all configured protected media sources (admin only)."""
    sources = system_settings_service.get_system_media_sources(db)
    return MediaSourcesList(sources=[MediaSource(**s) for s in sources])


@router.post("/settings/media-sources", response_model=MediaSource)
def add_media_source(
    source: MediaSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> MediaSource:
    """Add a new protected media source (admin only)."""
    import uuid

    sources = system_settings_service.get_system_media_sources(db)

    # Check for duplicate hostname
    for existing in sources:
        if existing.get("hostname") == source.hostname:
            raise HTTPException(
                status_code=400,
                detail=f"A source with hostname '{source.hostname}' already exists",
            )

    new_source = {
        "id": str(uuid.uuid4())[:8],
        "hostname": source.hostname,
        "provider_type": source.provider_type,
        "username": source.username,
        "password": source.password,
        "verify_ssl": source.verify_ssl,
        "label": source.label,
    }
    sources.append(new_source)
    system_settings_service.set_system_media_sources(db, sources)

    # Reload providers so the new host is recognized immediately
    _reload_protected_media_providers()

    logger.info(
        "Media source added by admin %s: %s (%s)",
        current_user.email,
        source.hostname,
        source.provider_type,
    )
    return MediaSource(
        id=str(new_source["id"]),
        hostname=source.hostname,
        provider_type=source.provider_type,
        username=source.username,
        password=source.password,
        verify_ssl=source.verify_ssl,
        label=source.label,
    )


@router.put("/settings/media-sources/{source_id}", response_model=MediaSource)
def update_media_source(
    source_id: str,
    update: MediaSourceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> MediaSource:
    """Update an existing protected media source (admin only)."""
    sources = system_settings_service.get_system_media_sources(db)

    for i, s in enumerate(sources):
        if s.get("id") == source_id:
            # Check hostname uniqueness if changed
            if update.hostname and update.hostname != s.get("hostname"):
                for other in sources:
                    if other.get("id") != source_id and other.get("hostname") == update.hostname:
                        raise HTTPException(
                            status_code=400,
                            detail=f"A source with hostname '{update.hostname}' already exists",
                        )

            # Apply updates
            update_data = update.model_dump(exclude_unset=True)
            sources[i] = {**s, **update_data}
            system_settings_service.set_system_media_sources(db, sources)

            _reload_protected_media_providers()

            logger.info(
                "Media source %s updated by admin %s",
                source_id,
                current_user.email,
            )
            return MediaSource(**sources[i])

    raise HTTPException(status_code=404, detail="Media source not found")


@router.delete("/settings/media-sources/{source_id}")
def delete_media_source(
    source_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Delete a protected media source (admin only)."""
    sources = system_settings_service.get_system_media_sources(db)
    original_len = len(sources)
    sources = [s for s in sources if s.get("id") != source_id]

    if len(sources) == original_len:
        raise HTTPException(status_code=404, detail="Media source not found")

    system_settings_service.set_system_media_sources(db, sources)

    _reload_protected_media_providers()

    logger.info(
        "Media source %s deleted by admin %s",
        source_id,
        current_user.email,
    )
    return {"success": True}


def _reload_protected_media_providers():
    """Reload the protected media provider registry after config changes."""
    try:
        from app.services import protected_media_providers
        from app.services.protected_media_providers import _load_providers

        protected_media_providers.PROTECTED_MEDIA_PROVIDERS = _load_providers()
    except Exception as e:
        logger.warning("Failed to reload protected media providers: %s", e)
