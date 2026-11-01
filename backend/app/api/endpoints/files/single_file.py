"""Single-file routes: ``GET``/``PUT /{file_uuid}``, ``/{file_uuid}/info`` and
``/{file_uuid}/stream-url`` (the presigned playback / thumbnail URL).
"""

import logging
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.error_handlers import ErrorHandler
from app.auth.audit_events import AuditEventType
from app.core.env import env_bool
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.user import User
from app.schemas.media import MediaFile as MediaFileSchema
from app.schemas.media import MediaFileDetail
from app.schemas.media import MediaFilePublicInfo
from app.schemas.media import MediaFileUpdate
from app.services.content_audit import KIND_STREAM_URL
from app.services.content_audit import record_file_event
from app.services.playback_rendition import resolve_playback
from app.services.request_context import RequestContext

from .crud import get_media_file_by_uuid
from .crud import update_media_file
from .detail_response import get_media_file_detail

router = APIRouter()
logger = logging.getLogger(__name__)


# =============================================================================
# PARAMETERIZED ROUTES: /{file_uuid}/...
# =============================================================================
# IMPORTANT: All routes with path parameters like /{file_uuid} MUST be defined
# AFTER static routes like /metadata-filters. FastAPI matches routes in order,
# so /{file_uuid} would incorrectly capture /metadata-filters as file_uuid.
#
# The UUID type annotation provides validation - requests with invalid UUIDs
# (like "metadata-filters") will return 422 Unprocessable Entity instead of 404.
# =============================================================================


@router.get("/{file_uuid}", response_model=MediaFileDetail)
def get_media_file(
    file_uuid: UUID,
    request: Request,
    segment_limit: int | None = Query(
        500,
        description="Maximum number of transcript segments to return. Use 0 for all segments.",
        ge=0,
    ),
    segment_offset: int = Query(
        0,
        description="Offset for transcript segment pagination",
        ge=0,
    ),
    redact: bool = Query(
        True,
        description=(
            "Apply content redaction to transcript text. Set false to view the original "
            "(owner/admin only, for non-admin-forced categories; audited)."
        ),
    ),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
):
    """Get a specific media file with transcript details.

    For large transcripts, use segment_limit and segment_offset for pagination.
    Default returns first 500 segments. Use segment_limit=0 to get all segments.
    """
    # segment_limit=0 means get all segments
    effective_limit = None if segment_limit == 0 else segment_limit
    detail = get_media_file_detail(
        db,
        str(file_uuid),
        ctx.user,
        effective_limit,
        segment_offset,
        redact=redact,
        organization_id=ctx.org_id,
        bypass=ctx.bypass,
        reveal_hold_uuids=ctx.is_org_admin,
    )
    # After the read succeeded, so a refused or missing file is never recorded as viewed.
    # Thumbnails, captions, the waveform and segment pages are part of this view.
    record_file_event(
        db,
        request,
        event=AuditEventType.CONTENT_FILE_VIEW,
        actor=ctx.user,
        file=file_uuid,
        details={"surface": "detail"},
        bypass=ctx.bypass,
    )
    return detail


@router.get("/{file_uuid}/info", response_model=MediaFilePublicInfo)
def get_media_file_info(
    file_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
):
    """Get lightweight file metadata without transcript or summary data.

    Returns core identity, status, and technical metadata fields only.
    Useful for integrations that need file context without heavy payloads.
    """
    from app.services.uuid_helpers import get_file_by_uuid_with_permission

    current_user = ctx.user
    media_file = get_file_by_uuid_with_permission(
        db,
        str(file_uuid),
        current_user.id,
        bypass=ctx.bypass,
        organization_id=ctx.org_id,
    )

    assert media_file.filename is not None  # required by MediaFilePublicInfo.filename
    return MediaFilePublicInfo(
        uuid=media_file.uuid,
        filename=media_file.filename,
        title=media_file.title,
        user_id=media_file.user.uuid,
        storage_path=media_file.storage_path,
        upload_time=media_file.upload_time,
        file_size=media_file.file_size,
        content_type=media_file.content_type,
        duration=media_file.duration,
        language=media_file.language,
        status=media_file.status,
    )


@router.put("/{file_uuid}", response_model=MediaFileSchema)
def update_media_file_endpoint(
    file_uuid: UUID,
    media_file_update: MediaFileUpdate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
):
    """Update a media file's metadata"""
    return update_media_file(
        db,
        str(file_uuid),
        media_file_update,
        ctx.user,
        organization_id=ctx.org_id,
        bypass=ctx.bypass,
    )


# NOTE: DELETE /{file_uuid} is handled by cancel_upload.router (included
# earlier, so it always matched first). It cancels PENDING uploads and
# delegates everything else to crud.delete_media_file — a duplicate route
# here would be unreachable.


@router.get("/{file_uuid}/stream-url")
def get_media_file_stream_url(
    file_uuid: str,
    request: Request,
    media_type: str = Query("video", description="Type of media: video, thumbnail, or audio"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Generate a short-lived presigned URL for secure media streaming.

    This follows AWS/GCS best practices for secure content delivery:
    - Bounded expiration (``MEDIA_URL_EXPIRE_SECONDS``, 6 hours by default, for video and audio;
      ``THUMBNAIL_URL_EXPIRE_SECONDS``, 15 minutes, for thumbnails), capped at 5 minutes under a
      support-access grant
    - Cryptographically signed by MinIO (AWS Signature V4)
    - User must be authenticated and authorized

    Args:
        file_uuid: UUID of the media file
        media_type: Type of media to stream - "video", "thumbnail", or "audio"

    Returns:
        url: Presigned URL for direct MinIO access
        expires_in: Seconds until URL expires
        content_type: MIME type of the content
        playback: For media, what the URL serves: "original", "converted" (a
            browser-playable copy of an audio original), or "audio_only" (the audio
            track of a video no browser decodes). None for a thumbnail.
        is_public: Whether the file is public
    """
    from app.core.config import settings
    from app.services.minio_service import get_file_url

    # Validate media_type
    if media_type not in ("video", "thumbnail", "audio"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid media_type: {media_type}. Must be 'video', 'thumbnail', or 'audio'",
        )

    # Verify user has permission (ownership check, tenant-gated via ctx.org_id)
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
    )

    # Determine storage path and expiration based on media type
    playback: str | None = None
    if media_type == "thumbnail":
        storage_path = db_file.thumbnail_path
        expires_seconds = settings.THUMBNAIL_URL_EXPIRE_SECONDS
        content_type = (
            "image/webp" if storage_path and str(storage_path).endswith(".webp") else "image/jpeg"
        )
    else:
        # A file no browser decodes plays its rendition instead (AAC/M4A); the original
        # stays the download. resolve_playback also normalises the stored type: rows from
        # before issue #1044 can hold an alias such as audio/vnd.wave that the player's
        # <source type> rejects.
        source = resolve_playback(db_file.content_type, db_file.storage_path, db_file.playback_path)
        storage_path = source.object_name if source else None
        content_type = source.content_type if source else "application/octet-stream"
        playback = source.mode.value if source else None
        expires_seconds = settings.MEDIA_URL_EXPIRE_SECONDS

    if not storage_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{media_type.title()} not found for this file",
        )

    if media_type != "thumbnail":
        # Issuing a URL for the media itself is the access; thumbnails are part of the view.
        record_file_event(
            db,
            request,
            event=AuditEventType.CONTENT_MEDIA_ACCESS,
            actor=current_user,
            file=db_file,
            details={"kind": KIND_STREAM_URL, "media_type": media_type},
            bypass=ctx.bypass,
        )

    # A presigned URL is a bearer token that survives revocation of a support-access grant,
    # so under one it is capped to a few minutes (issue #1122); otherwise unchanged.
    expires_seconds = ctx.bypass.presign_ttl(expires_seconds)

    # Generate presigned URL (uses existing minio_service function)
    if env_bool("SKIP_S3"):
        # Test environment - return mock URL
        logger.info("Returning mock presigned URL in test environment")
        return {
            "url": f"/api/files/{db_file.uuid}/{media_type}",
            "expires_in": expires_seconds,
            "content_type": content_type,
            "playback": playback,
            "is_public": getattr(db_file, "is_public", False),
        }

    try:
        presigned_url = get_file_url(
            str(storage_path),
            expires=expires_seconds,
            content_type=content_type if media_type != "thumbnail" else None,
        )
        logger.info(
            f"Generated presigned URL for {media_type} (file: {file_uuid}, expires: {expires_seconds}s)"
        )

        return {
            "url": presigned_url,
            "expires_in": expires_seconds,
            "content_type": content_type,
            "playback": playback,
            "is_public": getattr(db_file, "is_public", False),
        }
    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        # The real MinIO/presign failure (host, bucket, credential details) is logged,
        # never returned — the response only ever names the operation (#859).
        logger.exception(f"Error generating presigned URL for file {file_uuid}")
        raise ErrorHandler.internal_error(
            "Could not generate a streaming URL for this file."
        ) from e
