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
from sqlalchemy.orm import selectinload

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
from app.schemas.watch_source import RETRYABLE_FILE_STATUSES
from app.schemas.watch_source import CapabilitiesResponse
from app.schemas.watch_source import ConnectionTestResponse
from app.schemas.watch_source import DirectoryListResponse
from app.schemas.watch_source import EmailConfigOption
from app.schemas.watch_source import EmailLinkCreate
from app.schemas.watch_source import EmailLinkResponse
from app.schemas.watch_source import FsEventsStatus
from app.schemas.watch_source import MultipartRegexTestRequest
from app.schemas.watch_source import MultipartRegexTestResponse
from app.schemas.watch_source import ScanResponse
from app.schemas.watch_source import WatchSourceCreate
from app.schemas.watch_source import WatchSourceFileActionRequest
from app.schemas.watch_source import WatchSourceFileActionResponse
from app.schemas.watch_source import WatchSourceFileActionResult
from app.schemas.watch_source import WatchSourceFilesList
from app.schemas.watch_source import WatchSourceResponse
from app.schemas.watch_source import WatchSourcesList
from app.schemas.watch_source import WatchSourceStats
from app.schemas.watch_source import WatchSourceUpdate
from app.services.auth_mail_config_service import IN_USE_MESSAGE
from app.services.auth_mail_config_service import is_designated
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
    """Read the DB-backed global watch-source settings (super_admin only).

    Consumed by the admin Settings UI and by ops scripts checking a deployment's
    import tuning. These are ``SystemSettings`` rows, not ``.env`` vars — there is
    no environment fallback to read instead, and no restart applies them.
    """
    from app.services import watch_settings_service

    return watch_settings_service.get_global_settings(db)


@router.put("/settings")
def update_global_settings(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """Update the global watch-source settings, taking effect without a restart.

    Consumed by the admin Settings UI and by ops automation. super_admin only —
    these knobs govern every user's imports, not just the caller's.

    The body is a loose ``dict`` rather than a schema so an older client keeps
    working: ``max_concurrent_imports`` is accepted as an alias for the post-#295
    ``max_imports_per_scan``. Only the new name is ever returned. Any key omitted
    is left at its current value by the service.
    """
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
        # Deleting a config cascades its links away, silently un-notifying every
        # source that used it. Surfacing the count is what makes that consequence
        # visible at the moment of the decision rather than after the fact.
        #
        # ``or []`` because this helper serialises whatever it is handed. A real ORM
        # row always has a collection here, but its callers include test stands-in
        # whose unset columns are ``None`` — and a response builder that raises on an
        # empty column is a worse contract than one that reports zero.
        "linked_source_count": len(cfg.links or []),
    }


@router.get("/email-configs", response_model=EmailConfigsList)
def list_email_configs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """List every email-notification config, name-ordered (super_admin only).

    Consumed by the admin Settings UI and by ops scripts auditing which mailers a
    deployment has. The configs are deployment-wide, not per-user, which is why
    this is the super_admin tier and why there is no owner filter. Secrets are
    never returned — ``_email_to_response`` emits ``has_*`` booleans instead.
    """
    # ``selectinload`` because ``_email_to_response`` counts ``cfg.links``: without it
    # that is one extra query per config on a list endpoint.
    configs = (
        db.query(EmailNotificationConfig)
        .options(selectinload(EmailNotificationConfig.links))
        .order_by(EmailNotificationConfig.name)
        .all()
    )
    return {"configs": [_email_to_response(c) for c in configs]}


@router.post("/email-configs", response_model=EmailConfigResponse)
def create_email_config(
    data: EmailConfigCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """Create an email-notification config for watch-source scan reports.

    Consumed by the admin Settings UI; also a scriptable way to provision a mailer
    on a fresh deployment. super_admin only — the payload carries SMTP / M365 /
    Exchange credentials.

    All three secret fields are AES-256-GCM encrypted on write and are absent from
    the response. Nothing here validates the credentials; call
    ``POST /email-configs/{uuid}/test`` for that.
    """
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
    """Patch an email-notification config (super_admin only).

    Consumed by the admin Settings UI. ``exclude_unset`` makes this a true partial
    update, and a secret field sent empty or null is *skipped* rather than cleared —
    so the UI can round-trip a form it never received the secret for.

    Refuses with 409 when it would disable the config designated as the auth mailer;
    see the inline comment for why silently breaking password resets is worse.
    """
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    payload = data.model_dump(exclude_unset=True)
    # Disabling the designated auth mailer silently routes password resets to the
    # env SMTP transport, which is unset in every stock deployment — so it stops
    # them altogether, visible only as an ERROR log. Refuse and name the remedy.
    if payload.get("is_enabled") is False and is_designated(db, str(cfg.uuid)):
        raise HTTPException(status_code=409, detail=IN_USE_MESSAGE)
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
    """Delete an email-notification config (super_admin only).

    Consumed by the admin Settings UI. Refuses with 409 for the designated auth
    mailer — same reasoning as the disable guard in ``update_email_config``, plus
    the encrypted credentials go with the row and cannot be recovered. Any
    watch-source links to this config are removed by cascade.
    """
    cfg = (
        db.query(EmailNotificationConfig)
        .filter(EmailNotificationConfig.uuid == config_uuid)
        .first()
    )
    if not cfg:
        raise HTTPException(status_code=404, detail="Email config not found")
    # Same reasoning as the disable guard, plus the row itself is unrecoverable.
    if is_designated(db, str(cfg.uuid)):
        raise HTTPException(status_code=409, detail=IN_USE_MESSAGE)
    db.delete(cfg)
    db.commit()
    return {"success": True}


@router.post("/email-configs/{config_uuid}/test", response_model=EmailTestResponse)
def test_email_config(
    config_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> EmailTestResponse:
    """Send a live test message through one email config (super_admin only).

    Consumed by the admin Settings UI's "Test" button and by ops verifying a mailer
    after a credential rotation. Talks to the real SMTP / Graph / Exchange endpoint,
    so it is slow and has an external side effect.

    The outcome is persisted onto the config (``last_tested_at``, ``test_status``,
    ``test_message``) so the UI can show the last known state without re-testing.
    A failure is reported as ``success=false`` in a 200 body, not an error status —
    a failed connection is a successful *test*.
    """
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
    """List watch sources — the caller's own, or all of them for an admin.

    Consumed by the Watch Sources settings page; also the natural listing call for a
    script or agent enumerating configured imports. ``scope=all`` is honoured only
    for an admin and is silently downgraded to ``own`` otherwise, so a normal user
    cannot probe for other accounts' sources.

    Live filesystem-watcher state is prefetched in one Redis MGET for every
    FS-watched local source rather than one GET per row (see ``_fs_events_status``).
    """
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
    """Create a watch source (local folder, S3 bucket or SMB share) for auto-import.

    Consumed by the Watch Sources settings page; equally a scriptable way to
    provision imports on a new deployment. Any active user may create their own.

    Two non-obvious rules: a ``local`` source is refused with 400 unless the server
    has ``WATCH_FOLDER_PATH`` mounted — the path is a server-side mount, not a
    client-supplied one — and ``assign_to_user_uuid`` is honoured **only for an
    admin**, letting them stand up a source whose imported files land in another
    user's library. Secrets in the payload are encrypted by ``_apply_fields``.

    ``organization_id`` is captured once here from the creating request's context
    (issue #262c) so later background scans stamp imports with that tenant instead
    of guessing from the owner's memberships; ``None`` means personal scope.
    """
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
    """One watch source with its config and last-scan summary.

    Consumed by the settings page's detail/edit panel and by scripts inspecting a
    single source. Readable by its owner or any admin (``_get_source_or_404``, which
    answers 404 when the row is missing and 403 when it belongs to someone else).
    Credentials are represented only as ``has_s3_secret_key`` / ``has_smb_password``.
    """
    source = _get_source_or_404(db, source_uuid, current_user)
    return _source_to_response(source, current_user)


@router.put("/{source_uuid}", response_model=WatchSourceResponse)
def update_watch_source(
    source_uuid: str,
    data: WatchSourceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchSourceResponse:
    """Patch a watch source's configuration (owner or admin).

    Consumed by the settings page's edit form and by ops scripts retuning an import
    (poll interval, extension filter, multipart rules, auto-transcribe).

    ``exclude_unset`` makes this a genuine partial update: an omitted field is left
    alone, while an explicit null clears a nullable one. A secret sent empty is
    skipped rather than blanked (``_apply_fields``), so the UI can submit a form it
    never received the secret for. Changes apply to the next scan; nothing here
    re-scans or re-validates the connection.
    """
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
    """Delete a watch source and stop importing from it (owner or admin).

    Consumed by the settings page. Deletes only OpenTranscribe's own record: the
    cascade removes the per-file tracking rows and the email links, but **nothing is
    touched on the remote source and already-imported media stays in the gallery**.
    Losing the tracking rows loses this source's own import history; the MediaFile
    imohash check in ``services/watch_sources/processing.py`` is a separate layer and
    still applies if the source is later re-created.
    """
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
    """Probe a watch source's connectivity and report latency (owner or admin).

    Consumed by the settings page's "Test connection" button and by ops verifying a
    credential rotation or a newly mounted share. Read-only: it opens a client and
    calls ``test_connection()``, importing nothing.

    Always answers 200. A failure — including an unexpected exception — is reported
    as ``success=false`` with the message, because reporting whether the connection
    works *is* this endpoint's job. ``latency_ms`` is present only on success.
    """
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
    """Dispatch an out-of-band scan of one watch source (owner or admin).

    Consumed by the settings page's "Scan now" button, and the main automation entry
    point for this subsystem — a script or agent can trigger an import instead of
    waiting for the Celery beat poll interval.

    Returns immediately with the Celery ``task_id``; the scan and any imports happen
    in the worker, so poll ``GET /api/tasks/{task_id}`` (or the source's
    ``last_scan_*`` fields) for the outcome. Refuses with 400 on a disabled source,
    since a scan would import into a source the user has switched off.
    """
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
    q: str | None = Query(None, max_length=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Paginated per-file import history for one watch source (owner or admin).

    Consumed by the settings page's history table and by scripts auditing what a
    source did or did not import. ``?status=`` filters on the raw tracking status
    (``imported``, ``pending``, ``importing``, ``downloading``, ``error``,
    ``waiting_for_parts``, or a ``skipped*`` variant carrying ``skip_reason``);
    ``?q=`` is a case-insensitive substring match on the filename, for a source
    tracking more files than anyone can page through looking for one.

    Rows are the tracking records, not gallery files: ``media_file_uuid`` is null for
    anything skipped, errored or still in flight, and stays populated for an imported
    row. Newest first.
    """
    source = _get_source_or_404(db, source_uuid, current_user)
    # ``selectinload`` because the serialization below reads ``r.media_file`` on every
    # row: lazily that is one query per row, and ``page_size`` goes up to 200.
    query = (
        db.query(WatchSourceFile)
        .options(selectinload(WatchSourceFile.media_file))
        .filter(WatchSourceFile.watch_source_id == source.id)
    )
    if status_filter:
        query = query.filter(WatchSourceFile.status == status_filter)
    if q and q.strip():
        # ``ilike`` with the wildcards escaped, so a filename containing % or _ is
        # searched for literally rather than turning into a match-everything pattern.
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(WatchSourceFile.filename.ilike(f"%{needle}%", escape="\\"))
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
    """Import-status counts for one watch source, as one GROUP BY (owner or admin).

    Consumed by the settings page's summary badges and by monitoring scripts that
    want a source's health without paging through ``/files``.

    The buckets are coarser than the stored statuses on purpose: ``skipped`` sums
    every ``skipped*`` variant (there are several ``skip_reason`` flavours and the
    caller rarely cares which), and ``pending`` folds ``pending`` + ``importing`` +
    ``downloading`` into one "in flight" number.
    """
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


def _rows_for_action(
    db: Session, source: WatchSource, file_uuids: list[str]
) -> tuple[dict[str, WatchSourceFile], list[WatchSourceFileActionResult]]:
    """Resolve requested uuids to rows of this source, reporting misses per row.

    An unknown or foreign uuid is one row's problem, not the batch's — the caller is
    usually acting on a page of results that another scan may have changed underneath
    them, and failing the whole request would discard the work for every valid row.
    """
    rows = (
        db.query(WatchSourceFile)
        .filter(
            WatchSourceFile.watch_source_id == source.id,
            WatchSourceFile.uuid.in_(file_uuids),
        )
        .all()
    )
    by_uuid = {str(r.uuid): r for r in rows}
    missing = [
        WatchSourceFileActionResult(
            file_uuid=requested, success=False, error="Tracked file not found"
        )
        for requested in file_uuids
        if requested not in by_uuid
    ]
    return by_uuid, missing


@router.post("/{source_uuid}/files/retry", response_model=WatchSourceFileActionResponse)
def retry_source_files(
    source_uuid: str,
    data: WatchSourceFileActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchSourceFileActionResponse:
    """Re-queue tracked files for import, then dispatch ONE scan (owner or admin).

    Consumed by the settings page's file table (its per-row Retry is a batch of one)
    and by scripts clearing a backlog of failures. Resets each eligible row to
    ``pending`` and clears its ``skip_reason``/``error_message``, which is what makes
    a *terminal* row importable again: ``_get_or_create_tracking_row`` refuses to
    reuse a row in a terminal state, so before this there was no way to retry a
    skipped file at all. An ``error`` row was already retried by the next scan; this
    just stops the wait.

    **Batch by design.** ``scan_single`` holds a Redis lock per source, so a per-file
    endpoint would dispatch one scan per file and every one after the first would
    silently no-op. One reset pass plus one dispatch is both correct and what the
    table's "retry all failed" needs.

    ``retry_count`` is deliberately NOT incremented: ``_record_error`` already counts
    each failed attempt, and bumping it here would double-count one.

    **The rows are queued, not retried.** The dispatched scan may find a scan already
    running (the lock), or may not reach this file within ``max_imports_per_scan``, and
    it only re-imports files still present at their ``remote_path``. Callers should
    report "queued" and watch the row's status, not assume the import happened.
    """
    source = _get_source_or_404(db, source_uuid, current_user)
    if not source.is_enabled:
        # 409 rather than the /scan endpoint's 400: this is a conflict with the
        # source's STATE, not a malformed request, and resetting rows for a scan that
        # `_load_scan_plan` will refuse to run would be a lie dressed as success.
        raise HTTPException(status_code=409, detail="Enable the source before retrying its files")

    by_uuid, results = _rows_for_action(db, source, data.file_uuids)
    reset_any = False
    for requested in data.file_uuids:
        row = by_uuid.get(requested)
        if row is None:
            continue
        status_value = str(row.status)
        if status_value not in RETRYABLE_FILE_STATUSES:
            results.append(
                WatchSourceFileActionResult(
                    file_uuid=requested,
                    success=False,
                    status=status_value,
                    error=f"A file in state {status_value!r} cannot be retried",
                )
            )
            continue
        warning = None
        if row.skip_reason == "too_old" and source.skip_files_older_than_days is not None:
            warning = (
                "This file was skipped for age and the source still limits imports to the "
                f"last {source.skip_files_older_than_days} days, so the next scan will skip "
                "it again. Clear that limit first."
            )
        row.status = "pending"
        row.skip_reason = None
        row.error_message = None
        reset_any = True
        results.append(
            WatchSourceFileActionResult(
                file_uuid=requested, success=True, status="pending", warning=warning
            )
        )

    if reset_any:
        db.commit()
        from app.tasks.watch_source_tasks import scan_single

        scan_single.delay(source.id)

    return WatchSourceFileActionResponse(results=results, scan_dispatched=reset_any)


@router.post("/{source_uuid}/files/bulk-delete", response_model=WatchSourceFileActionResponse)
def bulk_delete_source_files(
    source_uuid: str,
    data: WatchSourceFileActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchSourceFileActionResponse:
    """Remove many tracking rows at once (owner or admin).

    Same semantics as the single-row ``DELETE`` beside it — the tracking record goes,
    the file stays in the source and in the library — but clearing a few hundred
    skipped records one confirmation at a time is not a usable admin surface.

    Dispatches no scan: deleting a row is not a request to re-import it. (An untracked
    path does become a candidate again on the next scheduled scan, where content dedup
    decides what happens to it.)
    """
    source = _get_source_or_404(db, source_uuid, current_user)
    by_uuid, results = _rows_for_action(db, source, data.file_uuids)
    for requested in data.file_uuids:
        row = by_uuid.get(requested)
        if row is None:
            continue
        db.delete(row)
        results.append(WatchSourceFileActionResult(file_uuid=requested, success=True))
    db.commit()
    return WatchSourceFileActionResponse(results=results, scan_dispatched=False)


# --------------------------------------------------------------------------- #
# Email links
# --------------------------------------------------------------------------- #
@router.get("/{source_uuid}/emails", response_model=list[EmailLinkResponse])
def list_email_links(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[EmailLinkResponse]:
    """The email configs this source notifies, with each link's own options.

    Consumed by the settings page's per-source notification panel. Owner or admin —
    the same tier as the link/unlink routes below, deliberately NOT the super_admin
    tier that governs the configs themselves: subscribing your own source to an
    existing mailer is not the same right as holding its credentials.

    ``notify_on_success``/``notify_on_error`` are evaluated **per scan**, not per
    file: ``send_notification`` classifies a whole scan by whether it recorded any
    error, so ``notify_on_error`` means "a scan in which at least one file failed".
    """
    source = _get_source_or_404(db, source_uuid, current_user)
    links = (
        db.query(WatchSourceEmail)
        .options(selectinload(WatchSourceEmail.email_config))
        .filter(WatchSourceEmail.watch_source_id == source.id)
        .all()
    )
    return [
        EmailLinkResponse(
            email_config_uuid=str(link.email_config.uuid),
            email_config_name=link.email_config.name,
            email_config_provider=link.email_config.provider,
            config_is_enabled=link.email_config.is_enabled,
            config_has_default_recipients=bool(
                link.email_config.default_recipients
                and link.email_config.default_recipients.strip()
            ),
            additional_recipients=link.additional_recipients,
            notify_on_success=link.notify_on_success,
            notify_on_error=link.notify_on_error,
        )
        for link in links
        if link.email_config is not None
    ]


@router.get("/{source_uuid}/emails/available", response_model=list[EmailConfigOption])
def list_available_email_configs(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[EmailConfigOption]:
    """Configs this source could still be linked to (owner or admin).

    Consumed by the notification panel's picker. It exists because of an asymmetry
    that otherwise makes that panel unbuildable: any source owner may link a config,
    but ``GET /email-configs`` is **super_admin** — so an ordinary owner had the right
    to subscribe and no way to discover what to subscribe to.

    Deliberately source-scoped rather than a second listing under ``/email-configs``:
    that prefix is in ``SUPER_ADMIN_PREFIXES``
    (``tests/unit/test_route_privilege_tiers.py``), and a non-super_admin route beneath
    it would fail that gate — correctly, since the gate exists to stop exactly this
    kind of quiet tier drift. Scoping it here also lets the server exclude what is
    already linked instead of making the client subtract two lists.

    The projection is minimal on purpose — see ``EmailConfigOption``. Do not widen it
    to ``EmailConfigResponse``: that carries the deployment's mail hostnames and
    usernames, and every authenticated user can read this.
    """
    source = _get_source_or_404(db, source_uuid, current_user)
    linked_ids = {
        link.email_config_id
        for link in db.query(WatchSourceEmail)
        .filter(WatchSourceEmail.watch_source_id == source.id)
        .all()
    }
    configs = db.query(EmailNotificationConfig).order_by(EmailNotificationConfig.name).all()
    return [
        EmailConfigOption(
            uuid=str(cfg.uuid),
            name=cfg.name,
            provider=cfg.provider,
            is_enabled=cfg.is_enabled,
            has_default_recipients=bool(cfg.default_recipients and cfg.default_recipients.strip()),
        )
        for cfg in configs
        if cfg.id not in linked_ids
    ]


@router.post("/{source_uuid}/emails")
def link_email_config(
    source_uuid: str,
    data: EmailLinkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> dict:
    """Attach an email config to a watch source, or update the existing link.

    Consumed by the settings page's notification panel. Authorized as the *source*
    owner (or an admin) — note the asymmetry: creating and editing the email configs
    themselves is super_admin, but any source owner may subscribe their own source to
    one that already exists, and there is no separate gate on which config they pick.

    Upsert, not insert: re-posting the same ``email_config_uuid`` overwrites the
    recipients and the notify-on-success/error flags rather than 409-ing, so it is
    safe to re-run.
    """
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
    """Detach an email config from a watch source (source owner or admin).

    Consumed by the settings page's notification panel. Removes only the link row;
    the email config itself survives and stays available to other sources.

    Idempotent — a missing link still answers ``{"success": true}``, so a retry after
    a dropped response does not 404. An unknown *config* uuid does 404, because that
    is a caller mistake rather than an already-applied delete.
    """
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
