"""Watch Sources API — source CRUD, connection test and scan; wires the sibling routers.

Users manage their own sources; admins additionally see all sources. Secrets are
AES-256-GCM encrypted on write and never returned. Sibling modules: settings and
static helpers (``watch_sources_settings``), email configs
(``watch_sources_email_configs``), per-file history (``watch_sources_files``),
email links (``watch_sources_email_links``), and the tenant gate
(``watch_sources_access``).
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints import watch_sources_email_configs
from app.api.endpoints import watch_sources_email_links
from app.api.endpoints import watch_sources_files
from app.api.endpoints import watch_sources_settings
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.watch_sources_access import _get_source_or_404
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.models.watch_source import WatchSource
from app.schemas.watch_source import ConnectionTestResponse
from app.schemas.watch_source import FsEventsStatus
from app.schemas.watch_source import ScanResponse
from app.schemas.watch_source import WatchSourceCreate
from app.schemas.watch_source import WatchSourceResponse
from app.schemas.watch_source import WatchSourcesList
from app.schemas.watch_source import WatchSourceUpdate
from app.services.request_context import RequestContext
from app.utils.encryption import encrypt_api_key

logger = logging.getLogger(__name__)
router = APIRouter()

# FastAPI matches in registration order: the static paths (settings, /email-configs) are
# included before this module's ``/{source_uuid}`` routes, the nested per-source routers
# after them. This is the original single-file order; do not reorder.
router.include_router(watch_sources_settings.router)
router.include_router(watch_sources_email_configs.router)

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
# Watch source CRUD
# --------------------------------------------------------------------------- #
@router.get("", response_model=WatchSourcesList)
def list_watch_sources(
    scope: str = Query("own", description="'own' or 'all' (admin only)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
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
    # Own sources in the ACTIVE tenant only (NULL-safe: personal = org-less).
    org_filter = (
        WatchSource.organization_id.is_(None)
        if ctx.org_id is None
        else WatchSource.organization_id == ctx.org_id
    )
    if scope == "all" and ctx.bypass.sees_all_in_scope:
        query = query.filter(org_filter).order_by(WatchSource.created_at.desc())
    else:
        query = query.filter(WatchSource.user_id == current_user.id, org_filter).order_by(
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


@router.post(
    "", response_model=WatchSourceResponse, dependencies=[Depends(refuse_under_support_grant)]
)
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
    user's library. In a multi-tenant deployment that is refused (404, like an
    unknown account) unless the request's bypass allows a write in the target's
    tenant, which a support grant never does on this route. Secrets in the payload are encrypted by ``_apply_fields``.

    ``organization_id`` is captured once here from the creating request's context
    (issue #262c) so later background scans stamp imports with that tenant instead
    of guessing from the owner's memberships; ``None`` means personal scope.
    """
    # local sources require the mount to be configured server-side.
    if data.source_type.value == "local" and not settings.WATCH_FOLDER_PATH:
        raise HTTPException(status_code=400, detail="Local watch folder is not configured")

    # Admins may assign the source (and its imported files) to another user.
    source_uuid = uuid_pkg.uuid4()
    owner_id = current_user.id
    if data.assign_to_user_uuid and ctx.bypass.user_is_admin:
        target = db.query(User).filter(User.uuid == data.assign_to_user_uuid).first()
        # Another account's workspace is its own tenant (a personal one included), so the
        # platform role reaches it only where the bypass allows a write there. The answer is
        # the same 404 as an unknown account, so the target's existence is not confirmed.
        if target is None or (
            int(target.id) != int(current_user.id)
            and not ctx.bypass.allows(
                org_id=ctx.org_id,
                owner_id=int(target.id),
                need="write",
                resource_type="watch_source",
                resource_uuid=str(source_uuid),
            )
        ):
            raise HTTPException(status_code=404, detail="assign_to_user not found")
        owner_id = target.id

    source = WatchSource(
        uuid=source_uuid,
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
    ctx: RequestContext = Depends(get_current_context),
) -> WatchSourceResponse:
    """One watch source with its config and last-scan summary.

    Consumed by the settings page's detail/edit panel and by scripts inspecting a
    single source. Readable by its owner or any admin (``_get_source_or_404``, which
    answers 404 when the row is missing and 403 when it belongs to someone else).
    Credentials are represented only as ``has_s3_secret_key`` / ``has_smb_password``.
    """
    source = _get_source_or_404(db, source_uuid, ctx, need="read")
    return _source_to_response(source, current_user)


@router.put("/{source_uuid}", response_model=WatchSourceResponse)
def update_watch_source(
    source_uuid: str,
    data: WatchSourceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
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
    source = _get_source_or_404(db, source_uuid, ctx)
    _apply_fields(source, data.model_dump(exclude_unset=True))
    db.commit()
    db.refresh(source)
    return _source_to_response(source, current_user)


@router.delete("/{source_uuid}")
def delete_watch_source(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict:
    """Delete a watch source and stop importing from it (owner or admin).

    Consumed by the settings page. Deletes only OpenTranscribe's own record: the
    cascade removes the per-file tracking rows and the email links, but **nothing is
    touched on the remote source and already-imported media stays in the gallery**.
    Losing the tracking rows loses this source's own import history; the MediaFile
    imohash check in ``services/watch_sources/processing.py`` is a separate layer and
    still applies if the source is later re-created.
    """
    source = _get_source_or_404(db, source_uuid, ctx)
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
    ctx: RequestContext = Depends(get_current_context),
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

    source = _get_source_or_404(db, source_uuid, ctx)
    started = time.perf_counter()
    try:
        with create_client(source) as client:
            ok, message = client.test_connection()
    except Exception:  # noqa: BLE001
        # This endpoint's whole purpose is to report whether the connection
        # works, so any failure is a successful *test* with a negative result.
        # But this broad catch also wraps create_client(source) itself — a
        # keyring/decrypt/DNS failure building the client, not a connection-test
        # RESULT — so the caught text can be an internal detail rather than
        # anything about the remote endpoint. Kept generic rather than echoed
        # into the response (#859); the real error is still logged above.
        logger.exception(f"Connection test failed for watch source {source_uuid}")
        return ConnectionTestResponse(
            success=False, message="The connection test failed unexpectedly."
        )
    return ConnectionTestResponse(
        success=ok, message=message, latency_ms=round((time.perf_counter() - started) * 1000, 1)
    )


@router.post("/{source_uuid}/scan", response_model=ScanResponse)
def scan_watch_source(
    source_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
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

    source = _get_source_or_404(db, source_uuid, ctx)
    if not source.is_enabled:
        raise HTTPException(status_code=400, detail="Enable the source before scanning")
    task = scan_single.delay(source.id)
    return ScanResponse(status="started", message="Scan dispatched", task_id=task.id)


router.include_router(watch_sources_files.router)
router.include_router(watch_sources_email_links.router)
