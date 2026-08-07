"""Watch Sources API — CRUD, test/scan, file history, folder browse, email, settings.

Users manage their own sources; admins additionally see all sources, manage the
shared email-notification configs, and tune the DB-backed global settings (no
restart). Secrets are AES-256-GCM encrypted on write and never returned.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context

# Deployment configuration is the super_admin tier: this router
# holds SMTP/S3/SMB credentials for automated import.
from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_active_user
from app.core.config import settings
from app.db.base import get_db
from app.models.email_notification_config import EmailNotificationConfig
from app.models.email_notification_config import WatchSourceEmail
from app.models.user import User
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.schemas.email_notification import EmailConfigCreate
from app.schemas.email_notification import EmailConfigResponse
from app.schemas.email_notification import EmailConfigsList
from app.schemas.email_notification import EmailConfigUpdate
from app.schemas.email_notification import EmailTestResponse
from app.schemas.watch_source import CapabilitiesResponse
from app.schemas.watch_source import ConnectionTestResponse
from app.schemas.watch_source import DirectoryListResponse
from app.schemas.watch_source import EmailLinkCreate
from app.schemas.watch_source import FsEventsStatus
from app.schemas.watch_source import MultipartRegexTestRequest
from app.schemas.watch_source import MultipartRegexTestResponse
from app.schemas.watch_source import ScanResponse
from app.schemas.watch_source import WatchSourceCreate
from app.schemas.watch_source import WatchSourceFilesList
from app.schemas.watch_source import WatchSourceResponse
from app.schemas.watch_source import WatchSourcesList
from app.schemas.watch_source import WatchSourceStats
from app.schemas.watch_source import WatchSourceUpdate
from app.utils.encryption import encrypt_api_key

logger = logging.getLogger(__name__)
router = APIRouter()

# Secret request-field → encrypted-column mapping (shared by create/update).
_SECRET_FIELDS = {
    "s3_secret_key": "encrypted_s3_secret_key",
    "smb_password": "encrypted_smb_password",
}
_PLAIN_FIELDS = (
    "name",
    "is_enabled",
    "local_path",
    "delete_after_import",
    "s3_endpoint_url",
    "s3_bucket_name",
    "s3_prefix",
    "s3_region",
    "s3_access_key_id",
    "s3_use_ssl",
    "smb_server",
    "smb_share",
    "smb_path",
    "smb_username",
    "smb_domain",
    "smb_port",
    "polling_interval_minutes",
    "use_fs_events",
    "file_extensions",
    "skip_files_older_than_days",
    "recursive",
    "auto_transcribe",
    "min_speakers",
    "max_speakers",
    "collection_ids",
    "tag_names",
    "multipart_enabled",
    "multipart_regex",
    "multipart_time_window_hours",
    "multipart_wait_scans",
    "upload_stitched_to_source",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _fs_events_status(
    source: WatchSource, fs_status_map: dict[int, dict] | None = None
) -> FsEventsStatus | None:
    """Read the live observer status the beat supervisor publishes to Redis.

    ``None`` means nothing is watching this source, so the UI reports the
    Celery poll interval instead. The status key has a short TTL, so a dead
    beat container degrades to that answer on its own rather than lying.
    ``fs_status_map`` is the list endpoint's single-round-trip prefetch.
    """
    if not source.use_fs_events or source.source_type != "local":
        return None
    if fs_status_map is not None:
        raw = fs_status_map.get(int(source.id))
    else:
        from app.services.watch_sources.fs_events import status as fs_status

        raw = fs_status.get(int(source.id))
    if not raw:
        return None
    try:
        parsed: FsEventsStatus = FsEventsStatus.model_validate(raw)
        return parsed
    except Exception as e:  # noqa: BLE001 - a stale blob must not 500 the list
        logger.debug("Ignoring unreadable FS-event status for source %s: %s", source.id, e)
        return None


def _source_to_response(
    source: WatchSource, current_user: User, fs_status_map: dict[int, dict] | None = None
) -> WatchSourceResponse:
    owner = source.user
    return WatchSourceResponse(
        uuid=str(source.uuid),
        name=source.name,
        source_type=source.source_type,
        is_enabled=source.is_enabled,
        local_path=source.local_path,
        delete_after_import=source.delete_after_import,
        s3_endpoint_url=source.s3_endpoint_url,
        s3_bucket_name=source.s3_bucket_name,
        s3_prefix=source.s3_prefix,
        s3_region=source.s3_region,
        s3_access_key_id=source.s3_access_key_id,
        s3_use_ssl=source.s3_use_ssl,
        has_s3_secret_key=source.has_s3_secret_key,
        smb_server=source.smb_server,
        smb_share=source.smb_share,
        smb_path=source.smb_path,
        smb_username=source.smb_username,
        smb_domain=source.smb_domain,
        smb_port=source.smb_port,
        has_smb_password=source.has_smb_password,
        polling_interval_minutes=source.polling_interval_minutes,
        use_fs_events=source.use_fs_events,
        fs_events=_fs_events_status(source, fs_status_map),
        file_extensions=source.file_extensions,
        skip_files_older_than_days=source.skip_files_older_than_days,
        recursive=source.recursive,
        auto_transcribe=source.auto_transcribe,
        min_speakers=source.min_speakers,
        max_speakers=source.max_speakers,
        collection_ids=source.collection_ids,
        tag_names=source.tag_names,
        multipart_enabled=source.multipart_enabled,
        multipart_regex=source.multipart_regex,
        multipart_time_window_hours=source.multipart_time_window_hours,
        multipart_wait_scans=source.multipart_wait_scans,
        upload_stitched_to_source=source.upload_stitched_to_source,
        last_scan_at=source.last_scan_at,
        last_scan_status=source.last_scan_status,
        last_scan_message=source.last_scan_message,
        last_scan_files_found=source.last_scan_files_found,
        last_scan_files_imported=source.last_scan_files_imported,
        last_scan_files_skipped=source.last_scan_files_skipped,
        last_scan_duration_seconds=source.last_scan_duration_seconds,
        total_files_imported=source.total_files_imported,
        owner_name=(owner.full_name or owner.email) if owner else None,
        owner_uuid=str(owner.uuid) if owner else None,
        is_own=bool(owner and owner.id == current_user.id),
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _get_source_or_404(db: Session, source_uuid: str, current_user: User) -> WatchSource:
    source: WatchSource | None = (
        db.query(WatchSource).filter(WatchSource.uuid == source_uuid).first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Watch source not found")
    # Owner or admin may access.
    if source.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Not authorized for this watch source")
    return source


def _apply_fields(source: WatchSource, data: dict) -> None:
    """Copy plain fields and encrypt secrets from a create/update payload dict.

    ``data`` comes from ``model_dump(exclude_unset=True)`` so only fields the
    caller actually provided are present — applying every present field is
    correct for both create and update (and lets an update clear a nullable
    field by sending null).
    """
    for field in _PLAIN_FIELDS:
        if field in data:
            setattr(source, field, data[field])
    for req_field, enc_col in _SECRET_FIELDS.items():
        if data.get(req_field):
            encrypted = encrypt_api_key(data[req_field])
            if not encrypted:
                raise HTTPException(status_code=500, detail="Failed to encrypt credential")
            setattr(source, enc_col, encrypted)


# --------------------------------------------------------------------------- #
# Static routes (declared before /{uuid})
# --------------------------------------------------------------------------- #
@router.get("/capabilities", response_model=CapabilitiesResponse)
def get_capabilities(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CapabilitiesResponse:
    """Feature-gating flags for the UI."""
    from app.services import watch_settings_service

    return CapabilitiesResponse(
        watch_source_enabled=watch_settings_service.is_enabled(db),
        local_enabled=bool(settings.WATCH_FOLDER_PATH),
        fs_events_enabled=watch_settings_service.fs_events_enabled(db),
        fs_events_mode=watch_settings_service.fs_events_mode(db),
    )


@router.get("/browse", response_model=DirectoryListResponse)
def browse_directories(
    path: str = Query("", description="Relative path under the watch root"),
    current_user: User = Depends(get_current_active_user),
) -> DirectoryListResponse:
    """List subdirectories under WATCH_FOLDER_PATH for the local folder picker."""
    from app.services.watch_sources import folder_browser

    if not settings.WATCH_FOLDER_PATH:
        raise HTTPException(status_code=404, detail="Local watch folder is not configured")
    try:
        return folder_browser.list_directories(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/test-multipart-regex", response_model=MultipartRegexTestResponse)
def test_multipart_regex(
    payload: MultipartRegexTestRequest,
    current_user: User = Depends(get_current_active_user),
) -> MultipartRegexTestResponse:
    """Parse a filename with a multipart regex (backend owns the regex)."""
    from app.services.watch_sources import multipart

    try:
        parsed = multipart.parse_part(payload.regex, payload.filename)
    except ValueError as e:
        return MultipartRegexTestResponse(matched=False, error=str(e))
    if not parsed:
        return MultipartRegexTestResponse(matched=False)
    base, part_num, ext = parsed
    return MultipartRegexTestResponse(
        matched=True, base_name=base, part_number=part_num, extension=ext
    )


# --------------------------------------------------------------------------- #
# Global settings (admin)
# --------------------------------------------------------------------------- #
@router.get("/settings")
def get_global_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    from app.services import watch_settings_service

    return watch_settings_service.get_global_settings(db)


@router.put("/settings")
def update_global_settings(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    from app.services import watch_settings_service

    result = watch_settings_service.update_global_settings(
        db,
        enabled=payload.get("enabled"),
        file_stability_seconds=payload.get("file_stability_seconds"),
        # Accept the pre-#295 field name so an older client keeps working; the
        # response only ever carries the new name.
        max_imports_per_scan=payload.get(
            "max_imports_per_scan", payload.get("max_concurrent_imports")
        ),
        fs_events_enabled=payload.get("fs_events_enabled"),
        fs_events_mode=payload.get("fs_events_mode"),
        fs_events_poll_seconds=payload.get("fs_events_poll_seconds"),
    )
    logger.info("Admin %s updated global watch settings", current_user.email)
    return result


# --------------------------------------------------------------------------- #
# Email notification configs (admin)
# --------------------------------------------------------------------------- #
def _email_to_response(cfg: EmailNotificationConfig) -> dict:
    """Build an EmailConfigResponse-compatible dict (never includes secrets)."""
    return {
        "uuid": str(cfg.uuid),
        "name": cfg.name,
        "provider": cfg.provider,
        "is_enabled": cfg.is_enabled,
        "from_address": cfg.from_address,
        "default_recipients": cfg.default_recipients,
        "smtp_host": cfg.smtp_host,
        "smtp_port": cfg.smtp_port,
        "smtp_use_tls": cfg.smtp_use_tls,
        "smtp_username": cfg.smtp_username,
        "has_smtp_password": cfg.has_smtp_password,
        "m365_tenant_id": cfg.m365_tenant_id,
        "m365_client_id": cfg.m365_client_id,
        "has_m365_secret": cfg.has_m365_secret,
        "exchange_server": cfg.exchange_server,
        "exchange_domain": cfg.exchange_domain,
        "exchange_username": cfg.exchange_username,
        "has_exchange_password": cfg.has_exchange_password,
        "last_tested_at": cfg.last_tested_at,
        "test_status": cfg.test_status,
        "test_message": cfg.test_message,
        "created_at": cfg.created_at,
        "updated_at": cfg.updated_at,
    }


@router.get("/email-configs", response_model=EmailConfigsList)
def list_email_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    configs = db.query(EmailNotificationConfig).order_by(EmailNotificationConfig.name).all()
    return {"configs": [_email_to_response(c) for c in configs]}


@router.post("/email-configs", response_model=EmailConfigResponse)
def create_email_config(
    data: EmailConfigCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    cfg = EmailNotificationConfig(
        uuid=uuid_pkg.uuid4(),
        name=data.name,
        provider=data.provider.value,
        is_enabled=data.is_enabled,
        from_address=data.from_address,
        default_recipients=data.default_recipients,
        smtp_host=data.smtp_host,
        smtp_port=data.smtp_port,
        smtp_use_tls=data.smtp_use_tls,
        smtp_username=data.smtp_username,
        encrypted_smtp_password=encrypt_api_key(data.smtp_password) if data.smtp_password else None,
        m365_tenant_id=data.m365_tenant_id,
        m365_client_id=data.m365_client_id,
        encrypted_m365_client_secret=(
            encrypt_api_key(data.m365_client_secret) if data.m365_client_secret else None
        ),
        exchange_server=data.exchange_server,
        exchange_domain=data.exchange_domain,
        exchange_username=data.exchange_username,
        encrypted_exchange_password=(
            encrypt_api_key(data.exchange_password) if data.exchange_password else None
        ),
        created_by=current_user.id,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return _email_to_response(cfg)


@router.put("/email-configs/{config_uuid}", response_model=EmailConfigResponse)
def update_email_config(
    config_uuid: str,
    data: EmailConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    payload = data.model_dump(exclude_unset=True)
    secret_map = {
        "smtp_password": "encrypted_smtp_password",
        "m365_client_secret": "encrypted_m365_client_secret",
        "exchange_password": "encrypted_exchange_password",
    }
    for key, value in payload.items():
        if key in secret_map:
            if value:
                setattr(cfg, secret_map[key], encrypt_api_key(value))
            continue
        setattr(cfg, key, value)
    db.commit()
    db.refresh(cfg)
    return _email_to_response(cfg)


@router.delete("/email-configs/{config_uuid}")
def delete_email_config(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    db.delete(cfg)
    db.commit()
    return {"success": True}


@router.post("/email-configs/{config_uuid}/test", response_model=EmailTestResponse)
def test_email_config(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> EmailTestResponse:
    from app.services import watch_email_service

    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    ok, message = watch_email_service.test_connection(cfg)
    cfg.last_tested_at = datetime.now(UTC)
    cfg.test_status = "success" if ok else "failed"
    cfg.test_message = message
    db.commit()
    return EmailTestResponse(success=ok, message=message)


# --------------------------------------------------------------------------- #
# Watch source CRUD
# --------------------------------------------------------------------------- #
@router.get("", response_model=WatchSourcesList)
def list_watch_sources(
    scope: str = Query("own", description="'own' or 'all' (admin only)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchSourcesList:
    query = db.query(WatchSource)
    if scope == "all" and current_user.is_admin:
        query = query.order_by(WatchSource.created_at.desc())
    else:
        query = query.filter(WatchSource.user_id == current_user.id).order_by(
            WatchSource.created_at.desc()
        )
    sources = query.all()
    # One Redis MGET for every FS-watched source instead of one GET per row.
    from app.services.watch_sources.fs_events import status as fs_status

    fs_status_map = fs_status.get_many(
        [int(s.id) for s in sources if s.use_fs_events and s.source_type == "local"]
    )
    return WatchSourcesList(
        sources=[_source_to_response(s, current_user, fs_status_map) for s in sources]
    )


@router.post("", response_model=WatchSourceResponse)
def create_watch_source(
    data: WatchSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> WatchSourceResponse:
    # local sources require the mount to be configured server-side.
    if data.source_type.value == "local" and not settings.WATCH_FOLDER_PATH:
        raise HTTPException(status_code=400, detail="Local watch folder is not configured")

    # Admins may assign the source (and its imported files) to another user.
    owner_id = current_user.id
    if data.assign_to_user_uuid and current_user.is_admin:
        target = db.query(User).filter(User.uuid == data.assign_to_user_uuid).first()
        if not target:
            raise HTTPException(status_code=404, detail="assign_to_user not found")
        owner_id = target.id

    source = WatchSource(
        uuid=uuid_pkg.uuid4(),
        source_type=data.source_type.value,
        user_id=owner_id,
        created_by=current_user.id,
        # Tenant captured ONCE at creation from the creating request's context
        # (issue #262c) — background scans stamp every import with this org
        # instead of guessing from the owner's memberships. None = personal.
        organization_id=ctx.org_id,
    )
    _apply_fields(source, data.model_dump(exclude_unset=True))
    db.add(source)
    db.commit()
    db.refresh(source)
    return _source_to_response(source, current_user)


@router.get("/{source_uuid}", response_model=WatchSourceResponse)
def get_watch_source(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchSourceResponse:
    source = _get_source_or_404(db, source_uuid, current_user)
    return _source_to_response(source, current_user)


@router.put("/{source_uuid}", response_model=WatchSourceResponse)
def update_watch_source(
    source_uuid: str,
    data: WatchSourceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchSourceResponse:
    source = _get_source_or_404(db, source_uuid, current_user)
    _apply_fields(source, data.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(source)
    return _source_to_response(source, current_user)


@router.delete("/{source_uuid}")
def delete_watch_source(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    source = _get_source_or_404(db, source_uuid, current_user)
    db.delete(source)  # cascades to tracking rows + email links
    db.commit()
    return {"success": True}


# --------------------------------------------------------------------------- #
# Test / scan
# --------------------------------------------------------------------------- #
@router.post("/{source_uuid}/test", response_model=ConnectionTestResponse)
def test_watch_source(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ConnectionTestResponse:
    import time

    from app.services.watch_sources import create_client

    source = _get_source_or_404(db, source_uuid, current_user)
    started = time.perf_counter()
    try:
        with create_client(source) as client:
            ok, message = client.test_connection()
    except Exception as e:  # noqa: BLE001
        # This endpoint's whole purpose is to report whether the connection
        # works, so any failure is a successful *test* with a negative result.
        logger.exception(f"Connection test failed for watch source {source_uuid}")
        return ConnectionTestResponse(success=False, message=str(e))
    return ConnectionTestResponse(
        success=ok, message=message, latency_ms=round((time.perf_counter() - started) * 1000, 1)
    )


@router.post("/{source_uuid}/scan", response_model=ScanResponse)
def scan_watch_source(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ScanResponse:
    from app.tasks.watch_source_tasks import scan_single

    source = _get_source_or_404(db, source_uuid, current_user)
    if not source.is_enabled:
        raise HTTPException(status_code=400, detail="Enable the source before scanning")
    task = scan_single.delay(source.id)
    return ScanResponse(status="started", message="Scan dispatched", task_id=task.id)


# --------------------------------------------------------------------------- #
# File history
# --------------------------------------------------------------------------- #
@router.get("/{source_uuid}/files", response_model=WatchSourceFilesList)
def list_source_files(
    source_uuid: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status_filter: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    source = _get_source_or_404(db, source_uuid, current_user)
    query = db.query(WatchSourceFile).filter(WatchSourceFile.watch_source_id == source.id)
    if status_filter:
        query = query.filter(WatchSourceFile.status == status_filter)
    total = query.count()
    rows = (
        query.order_by(WatchSourceFile.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    files = []
    for r in rows:
        files.append(
            {
                "uuid": str(r.uuid),
                "remote_path": r.remote_path,
                "filename": r.filename,
                "file_size": r.file_size,
                "file_modified_at": r.file_modified_at,
                "imohash": r.imohash,
                "media_file_uuid": str(r.media_file.uuid) if r.media_file else None,
                "status": r.status,
                "skip_reason": r.skip_reason,
                "part_group": r.part_group,
                "part_number": r.part_number,
                "error_message": r.error_message,
                "retry_count": r.retry_count,
                "processed_at": r.processed_at,
                "created_at": r.created_at,
            }
        )
    return {"files": files, "total": total, "page": page, "page_size": page_size}


@router.get("/{source_uuid}/files/stats", response_model=WatchSourceStats)
def source_file_stats(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchSourceStats:
    from sqlalchemy import func

    source = _get_source_or_404(db, source_uuid, current_user)
    counts: dict[str, int] = {
        status: count
        for status, count in db.query(WatchSourceFile.status, func.count(WatchSourceFile.id))
        .filter(WatchSourceFile.watch_source_id == source.id)
        .group_by(WatchSourceFile.status)
        .all()
    }
    skipped = sum(v for k, v in counts.items() if k.startswith("skipped"))
    return WatchSourceStats(
        total=sum(counts.values()),
        imported=counts.get("imported", 0),
        skipped=skipped,
        error=counts.get("error", 0),
        pending=counts.get("pending", 0)
        + counts.get("importing", 0)
        + counts.get("downloading", 0),
        waiting_for_parts=counts.get("waiting_for_parts", 0),
    )


@router.delete("/{source_uuid}/files/{file_uuid}")
def delete_source_file(
    source_uuid: str,
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Remove a tracking row (does NOT delete from the source or the gallery)."""
    source = _get_source_or_404(db, source_uuid, current_user)
    row = (
        db.query(WatchSourceFile)
        .filter(
            WatchSourceFile.uuid == file_uuid,
            WatchSourceFile.watch_source_id == source.id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Tracked file not found")
    db.delete(row)
    db.commit()
    return {"success": True}


# --------------------------------------------------------------------------- #
# Email links
# --------------------------------------------------------------------------- #
@router.post("/{source_uuid}/emails")
def link_email_config(
    source_uuid: str,
    data: EmailLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    source = _get_source_or_404(db, source_uuid, current_user)
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == data.email_config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    existing = (
        db.query(WatchSourceEmail)
        .filter(
            WatchSourceEmail.watch_source_id == source.id,
            WatchSourceEmail.email_config_id == cfg.id,
        )
        .first()
    )
    if existing:
        existing.additional_recipients = data.additional_recipients
        existing.notify_on_success = data.notify_on_success
        existing.notify_on_error = data.notify_on_error
    else:
        db.add(
            WatchSourceEmail(
                watch_source_id=source.id,
                email_config_id=cfg.id,
                additional_recipients=data.additional_recipients,
                notify_on_success=data.notify_on_success,
                notify_on_error=data.notify_on_error,
            )
        )
    db.commit()
    return {"success": True}


@router.delete("/{source_uuid}/emails/{config_uuid}")
def unlink_email_config(
    source_uuid: str,
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    source = _get_source_or_404(db, source_uuid, current_user)
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    link = (
        db.query(WatchSourceEmail)
        .filter(
            WatchSourceEmail.watch_source_id == source.id,
            WatchSourceEmail.email_config_id == cfg.id,
        )
        .first()
    )
    if link:
        db.delete(link)
        db.commit()
    return {"success": True}
