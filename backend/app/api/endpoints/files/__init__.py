"""
Files API module - refactored for modularity.

This module contains the refactored files endpoint split into modular components:
- upload.py: File upload functionality
- crud.py: Basic CRUD operations
- filtering.py: Complex filtering logic for file listing
- streaming.py: Video/audio streaming endpoints
"""

import logging
from datetime import datetime
from typing import Any
from typing import NamedTuple
from typing import Optional
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import UploadFile
from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_optional_current_user
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.user import User
from app.schemas.media import MediaFile as MediaFileSchema
from app.schemas.media import MediaFileDetail
from app.schemas.media import MediaFilePublicInfo
from app.schemas.media import MediaFileUpdate
from app.schemas.media import PaginatedMediaFileResponse
from app.schemas.media import ReprocessRequest
from app.schemas.media import TranscriptSegment
from app.schemas.media import TranscriptSegmentUpdate
from app.services.formatting_service import FormattingService

from . import cancel_upload
from . import complete_upload
from . import prepare_upload
from .crud import _get_or_compute_analytics
from .crud import delete_media_file
from .crud import get_media_file_by_id
from .crud import get_media_file_by_uuid
from .crud import get_media_file_detail
from .crud import get_stream_url_info
from .crud import set_file_urls
from .crud import update_media_file
from .crud import update_single_transcript_segment
from .filtering import apply_all_filters
from .filtering import get_metadata_filters
from .reprocess import process_file_reprocess
from .segments import router as segments_router
from .streaming import get_thumbnail_streaming_response
from .streaming import validate_file_exists
from .subtitles import router as subtitles_router
from .summary_status import router as summary_status_router
from .upload import process_file_upload
from .url_processing import router as url_processing_router
from .waveform import router as waveform_router

# Create the router
router = APIRouter()
logger = logging.getLogger(__name__)


class SpeakerParams(NamedTuple):
    """Speaker diarization parameters parsed from request headers."""

    min_speakers: Optional[int]
    max_speakers: Optional[int]
    num_speakers: Optional[int]


def _parse_speaker_params_from_headers(request: Optional[Request]) -> SpeakerParams:
    """
    Parse speaker diarization parameters from request headers.

    Returns SpeakerParams with validated values. Invalid values are logged and set to None.
    If min_speakers > max_speakers, both are reset to None.
    """
    if not request:
        return SpeakerParams(None, None, None)

    def parse_int_header(header_name: str) -> Optional[int]:
        """Parse an integer from a request header, returning None on failure."""
        value = request.headers.get(header_name)
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            logger.warning(
                f"Invalid {header_name} header value: '{value}' - must be an integer. Using default."
            )
            return None

    min_speakers = parse_int_header("X-Min-Speakers")
    max_speakers = parse_int_header("X-Max-Speakers")
    num_speakers = parse_int_header("X-Num-Speakers")

    # Validate min <= max if both are provided
    if min_speakers is not None and max_speakers is not None and min_speakers > max_speakers:
        logger.warning(
            f"Invalid speaker range: min_speakers ({min_speakers}) > max_speakers ({max_speakers}). "
            "Ignoring both values and using defaults."
        )
        min_speakers = None
        max_speakers = None

    return SpeakerParams(min_speakers, max_speakers, num_speakers)


# Include all routers
router.include_router(cancel_upload.router, prefix="", tags=["files"])
router.include_router(prepare_upload.router, prefix="", tags=["files"])
router.include_router(complete_upload.router, prefix="", tags=["files"])
router.include_router(subtitles_router, prefix="", tags=["subtitles"])
router.include_router(waveform_router, prefix="", tags=["waveform"])
router.include_router(url_processing_router, prefix="", tags=["url-processing"])
router.include_router(segments_router, prefix="", tags=["files"])
router.include_router(summary_status_router, prefix="", tags=["summary"])


@router.post("", response_model=MediaFileSchema)
async def upload_media_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
    request: Request = None,  # type: ignore[assignment]
):
    """Upload a media file for transcription"""
    current_user = ctx.user
    # Get optional headers from prepare step
    existing_file_uuid = request.headers.get("X-File-ID") if request else None
    file_hash = request.headers.get("X-File-Hash") if request else None

    # Parse speaker diarization parameters from headers
    speaker_params = _parse_speaker_params_from_headers(request)

    # Parse skip_summary from header
    skip_summary = False
    if request:
        skip_summary_header = request.headers.get("X-Skip-Summary", "")
        skip_summary = skip_summary_header.lower() in ("true", "1", "yes")

    # Process the file upload (whisper_model is read from the DB record set during prepare)
    db_file = await process_file_upload(
        file,
        db,
        current_user,
        existing_file_uuid,
        file_hash,
        speaker_params.min_speakers,
        speaker_params.max_speakers,
        speaker_params.num_speakers,
        skip_summary=skip_summary,
        organization_id=ctx.org_id,
    )  # whisper_model auto-read from db_file.requested_whisper_model inside

    # Invalidate caches so gallery picks up the new file
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_user_files(current_user.id)
    except Exception as e:
        logger.debug(f"Cache invalidation failed (non-critical): {e}")

    # Create a response with the file ID in headers
    response = JSONResponse(content=jsonable_encoder(db_file))
    response.headers["X-File-ID"] = str(db_file.uuid)

    return response


@router.get("", response_model=PaginatedMediaFileResponse)
def list_media_files(
    # Pagination parameters
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    # Ownership filter
    ownership: str = Query(
        "mine",
        pattern="^(mine|shared|all)$",
        description="Filter: 'mine' (owned), 'shared' (via shared collections), 'all' (both)",
    ),
    # Existing filters
    search: Optional[str] = None,
    tag: Optional[list[str]] = Query(None),
    speaker: Optional[list[str]] = Query(None),
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    min_file_size: Optional[int] = None,  # In MB
    max_file_size: Optional[int] = None,  # In MB
    file_type: Optional[list[str]] = Query(None),  # ['audio', 'video']
    status: Optional[list[str]] = Query(
        None
    ),  # ['pending', 'processing', 'completed', 'error', 'cancelling', 'cancelled', 'orphaned']
    transcript_search: Optional[str] = None,  # Search in transcript content
    # Sort parameters (after filters to avoid parameter shifting)
    sort_by: str = Query(
        "upload_time",
        description="Field to sort by: upload_time, completed_at, filename, duration, file_size",
    ),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    # Dependencies
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
):
    """List media files for the current user with optional filters and pagination.

    Use ownership param to control scope:
    - 'mine': Only files owned by current user (default, preserves existing behavior)
    - 'shared': Only files accessible via shared collections
    - 'all': Both owned and shared files
    """
    current_user = ctx.user
    from sqlalchemy import func as sa_func
    from sqlalchemy.orm import defer
    from sqlalchemy.orm import joinedload
    from sqlalchemy.orm import load_only
    from sqlalchemy.orm import selectinload

    from app.services.permission_service import PermissionService

    # Eager-loading strategy for list view:
    # - joinedload for user (many-to-one — no row inflation)
    # - selectinload for speakers (one-to-many — avoids Cartesian product)
    #   with load_only for the two fields the speaker summary needs
    # - defer heavy JSON columns not required by the list Pydantic schema
    list_options = [
        joinedload(MediaFile.user),
        selectinload(MediaFile.speakers).load_only(
            Speaker.uuid,  # type: ignore[arg-type]
            Speaker.name,  # type: ignore[arg-type]
            Speaker.display_name,  # type: ignore[arg-type]
        ),
        defer(MediaFile.metadata_raw),  # type: ignore[arg-type]
        defer(MediaFile.waveform_data),  # type: ignore[arg-type]
    ]

    user_id = current_user.id
    org_scope = ctx.org_id  # int -> org scope, None -> personal scope (org-less rows)

    # Tenant gate for the admin "see all" and the owned-file branches. In org
    # context only same-org rows; in personal scope only org-less rows. Community
    # edition: org_scope is None and rows are org-less, so this is a no-op.
    if org_scope is not None:
        org_pred = MediaFile.organization_id == org_scope
    else:
        org_pred = MediaFile.organization_id.is_(None)

    from app.services.takedown_service import exclude_quarantined

    # Admin users can see all files regardless of ownership param (still org-gated).
    # Admins also see quarantined (taken-down) files so they can review them; for
    # every other caller the abuse/DMCA exclusion hides taken-down files.
    is_admin = current_user.is_admin
    if is_admin:
        base_query = db.query(MediaFile).options(*list_options).filter(org_pred)
        effective_user_id = None
    elif ownership == "mine":
        # Default: only owned files (within tenant scope)
        base_query = (
            db.query(MediaFile)
            .options(*list_options)
            .filter(MediaFile.user_id == user_id, org_pred)
        )
        effective_user_id = user_id
    elif ownership == "shared":
        # Only files from shared collections (not owned by user)
        accessible_subquery = PermissionService.get_accessible_file_ids_subquery(
            db, user_id, organization_id=org_scope
        )
        base_query = (
            db.query(MediaFile)
            .options(*list_options)
            .filter(
                MediaFile.id.in_(db.query(accessible_subquery.c.id)),
                MediaFile.user_id != user_id,  # Exclude owned files
            )
        )
        effective_user_id = None  # Don't filter by user_id in apply_all_filters
    else:
        # All: owned + shared
        accessible_subquery = PermissionService.get_accessible_file_ids_subquery(
            db, user_id, organization_id=org_scope
        )
        base_query = (
            db.query(MediaFile)
            .options(*list_options)
            .filter(MediaFile.id.in_(db.query(accessible_subquery.c.id)))
        )
        effective_user_id = None

    # Abuse/DMCA: hide taken-down files from the gallery for non-admins.
    base_query = exclude_quarantined(base_query, include_quarantined=is_admin)

    # Prepare filters dictionary
    filters = {
        "search": search,
        "tag": tag,
        "speaker": speaker,
        "from_date": from_date,
        "to_date": to_date,
        "min_duration": min_duration,
        "max_duration": max_duration,
        "min_file_size": min_file_size,
        "max_file_size": max_file_size,
        "file_type": file_type,
        "status": status,
        "transcript_search": transcript_search,
        "user_id": effective_user_id,
    }

    # Apply all filters
    filtered_query = apply_all_filters(base_query, filters)

    # Apply sorting
    # Note: MediaFile has upload_time, completed_at, filename, duration, file_size
    sort_field_mapping = {
        "upload_time": MediaFile.upload_time,
        "completed_at": MediaFile.completed_at,
        "filename": MediaFile.filename,
        "duration": MediaFile.duration,
        "file_size": MediaFile.file_size,
    }

    # Get the sort field (default to upload_time if invalid)
    sort_field = sort_field_mapping.get(sort_by, MediaFile.upload_time)

    # Get total count BEFORE sorting/pagination.
    # Use with_entities + func.count to avoid the subquery wrapper that
    # .count() generates — produces a flat SELECT count(media_file.id) …
    # instead of SELECT count(*) FROM (SELECT … ORDER BY …).
    total_count = (filtered_query.with_entities(sa_func.count(MediaFile.id)).scalar()) or 0

    # Apply sort order (only for the data query, not the count)
    if sort_order.lower() == "asc":
        filtered_query = filtered_query.order_by(sort_field.asc())  # type: ignore[attr-defined]
    else:
        filtered_query = filtered_query.order_by(sort_field.desc())  # type: ignore[attr-defined]

    # Apply pagination
    offset = (page - 1) * page_size
    paginated_query = filtered_query.offset(offset).limit(page_size)
    result = paginated_query.all()

    # Format each file with URLs and formatted fields
    formatted_files = []
    for file in result:
        set_file_urls(file)

        # Use the FormattingService method which handles formatting correctly
        # Pass speakers for speaker_summary in list view
        formatted_file = FormattingService.format_media_file(file, file.speakers)
        formatted_files.append(formatted_file)

    # Calculate pagination metadata
    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0
    has_more = page < total_pages

    return PaginatedMediaFileResponse(
        items=formatted_files,
        total=total_count,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        has_more=has_more,
    )


@router.get("/metadata-filters", response_model=dict)
def get_metadata_filters_endpoint(
    ownership: str = Query("all", pattern="^(mine|shared|all)$"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
):
    """Get available metadata filters like formats, codecs, etc."""
    return get_metadata_filters(db, ctx.user.id, ownership=ownership, organization_id=ctx.org_id)


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
    segment_limit: Optional[int] = Query(
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
    return get_media_file_detail(
        db,
        str(file_uuid),
        ctx.user,
        effective_limit,
        segment_offset,
        redact=redact,
        organization_id=ctx.org_id,
    )


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
    from app.utils.uuid_helpers import get_file_by_uuid_with_permission

    current_user = ctx.user
    media_file = get_file_by_uuid_with_permission(
        db,
        str(file_uuid),
        current_user.id,
        is_admin=current_user.is_admin,
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
        db, str(file_uuid), media_file_update, ctx.user, organization_id=ctx.org_id
    )


# NOTE: DELETE /{file_uuid} is handled by cancel_upload.router (included
# earlier, so it always matched first). It cancels PENDING uploads and
# delegates everything else to crud.delete_media_file — a duplicate route
# here would be unreachable.


@router.get("/{file_uuid}/stream-url")
def get_media_file_stream_url(
    file_uuid: str,
    media_type: str = Query("video", description="Type of media: video, thumbnail, or audio"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Generate a short-lived presigned URL for secure media streaming.

    This follows AWS/GCS best practices for secure content delivery:
    - Short expiration (5 minutes for video, 15 minutes for thumbnails)
    - Cryptographically signed by MinIO (AWS Signature V4)
    - User must be authenticated and authorized

    Args:
        file_uuid: UUID of the media file
        media_type: Type of media to stream - "video", "thumbnail", or "audio"

    Returns:
        url: Presigned URL for direct MinIO access
        expires_in: Seconds until URL expires
        content_type: MIME type of the content
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
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=is_admin, organization_id=ctx.org_id
    )

    # Determine storage path and expiration based on media type
    if media_type == "thumbnail":
        storage_path = db_file.thumbnail_path
        expires_seconds = settings.THUMBNAIL_URL_EXPIRE_SECONDS
        content_type = (
            "image/webp" if storage_path and str(storage_path).endswith(".webp") else "image/jpeg"
        )
    else:
        storage_path = db_file.storage_path
        expires_seconds = settings.MEDIA_URL_EXPIRE_SECONDS
        content_type = (
            str(db_file.content_type) if db_file.content_type else "application/octet-stream"
        )

    if not storage_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{media_type.title()} not found for this file",
        )

    # Generate presigned URL (uses existing minio_service function)
    import os

    if os.environ.get("SKIP_S3", "False").lower() == "true":
        # Test environment - return mock URL
        logger.info("Returning mock presigned URL in test environment")
        return {
            "url": f"/api/files/{db_file.uuid}/{media_type}",
            "expires_in": expires_seconds,
            "content_type": content_type,
            "is_public": getattr(db_file, "is_public", False),
        }

    try:
        presigned_url = get_file_url(str(storage_path), expires=expires_seconds)
        logger.info(
            f"Generated presigned URL for {media_type} (file: {file_uuid}, expires: {expires_seconds}s)"
        )

        return {
            "url": presigned_url,
            "expires_in": expires_seconds,
            "content_type": content_type,
            "is_public": getattr(db_file, "is_public", False),
        }
    except Exception as e:
        logger.error(f"Error generating presigned URL for file {file_uuid}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error generating streaming URL: {str(e)}",
        ) from e


def _audio_source_extension(filename: str) -> str:
    """Return the lowercase extension of a filename without the dot."""
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


_DOWNLOAD_MODES = {
    "video_subtitles",
    "video_original",
    "audio_mp3",
    "audio_wav",
    "audio_original",
}


def _audio_passthrough(db_file: MediaFile, mode: str) -> bool:
    """True when an audio mode can serve the original bytes without ffmpeg."""
    if not (db_file.content_type and db_file.content_type.startswith("audio/")):
        return False
    src_ext = _audio_source_extension(str(db_file.filename))
    return (
        mode == "audio_original"
        or (mode == "audio_mp3" and src_ext == "mp3")
        or (mode == "audio_wav" and src_ext == "wav")
    )


def _resolve_ready_download(db_file: MediaFile, mode: str) -> dict | None:
    """Return ``{"url", "filename"}`` if the asset can be served now, else None.

    "Ready" means a direct passthrough (original bytes) or an already-cached derived
    asset — both yield an instant presigned URL with no ffmpeg work.
    """
    from app.services.minio_service import MinIOService
    from app.services.minio_service import get_presigned_download_url
    from app.services.video_processing_service import VideoProcessingService

    filename = str(db_file.filename)
    base_name = filename.rsplit(".", 1)[0] if "." in filename else filename

    if mode == "video_original" or _audio_passthrough(db_file, mode):
        url = get_presigned_download_url(
            str(db_file.storage_path),
            download_filename=filename,
            content_type=str(db_file.content_type) if db_file.content_type else None,
        )
        return {"url": url, "filename": filename}

    service = VideoProcessingService(MinIOService())

    if mode == "video_subtitles":
        if db_file.status != "completed":
            raise HTTPException(status_code=422, detail="Transcript is not ready yet.")
        cache_key = service.generate_cache_key(db_file.id, filename, include_speakers=True)
        if service.is_video_cached(cache_key):
            dl_name = f"{base_name}_with_subtitles.mp4"
            return {
                "url": service.presigned_download_url(cache_key, dl_name, "video/mp4"),
                "filename": dl_name,
            }
        return None

    audio_format = mode.split("_", 1)[1]
    cached = service.peek_cached_audio(filename, audio_format)
    if cached:
        cache_key, ext, content_type = cached
        dl_name = f"{base_name}.{ext}"
        return {
            "url": service.presigned_download_url(cache_key, dl_name, content_type),
            "filename": dl_name,
        }
    return None


def _ensure_prepare_enqueued(db_file: MediaFile, user_id: int, mode: str) -> None:
    """Queue the ffmpeg prep task at most once per (file, mode) in-flight window.

    A short-lived Redis NX guard collapses duplicate requests (double-click, a POST
    plus the SSE stream, multiple tabs) into a single worker task. Once cached, the
    ready path short-circuits before reaching here.
    """
    from app.core.redis import get_redis
    from app.tasks.media_download import prepare_media_download_task

    guard_key = f"download:prep:{db_file.id}:{mode}"
    try:
        first = get_redis().set(guard_key, "1", nx=True, ex=900)
    except Exception:
        first = True  # Redis hiccup → don't block the download
    if first:
        prepare_media_download_task.delay(file_id=db_file.id, user_id=user_id, mode=mode)


@router.post("/{file_uuid}/prepare-download")
def prepare_download(
    file_uuid: str,
    mode: str = Query(
        ..., description="video_subtitles|video_original|audio_mp3|audio_wav|audio_original"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Prepare a media download.

    Returns one of:
    - ``{"status": "ready", "url": <presigned>, "filename": <name>}`` — passthrough or
      cache hit; the browser downloads straight from object storage.
    - ``{"status": "processing", "file_id": <uuid>}`` — ffmpeg work was queued on the
      download worker; the client opens the SSE stream (``download-stream``) to receive
      progress and the final presigned URL.
    """
    if mode not in _DOWNLOAD_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid download mode: {mode}")

    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )

    ready = _resolve_ready_download(db_file, mode)
    if ready:
        return {"status": "ready", **ready}

    _ensure_prepare_enqueued(db_file, current_user.id, mode)
    return {"status": "processing", "file_id": str(db_file.uuid)}


@router.get("/{file_uuid}/download-stream")
async def download_stream(
    file_uuid: str,
    mode: str = Query(
        ..., description="video_subtitles|video_original|audio_mp3|audio_wav|audio_original"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Server-Sent Events stream that pushes download progress + the final URL.

    The browser opens this (EventSource, cookie-authenticated) after a ``processing``
    response. Events: ``progress`` (status text), ``ready`` (``{url, filename}``),
    ``error`` (``{message}``). On (re)connect we re-check readiness first, so a dropped
    connection still delivers once the worker has finished — no client polling.
    """
    import asyncio
    import contextlib
    import json as _json

    import redis.asyncio as aioredis

    from app.core.config import settings
    from app.services.download_events import download_events_channel

    if mode not in _DOWNLOAD_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid download mode: {mode}")

    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    user_id = current_user.id

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {_json.dumps(payload)}\n\n"

    async def event_stream():
        # 1. Already available? deliver immediately (covers reconnects after completion).
        try:
            ready = _resolve_ready_download(db_file, mode)
        except HTTPException as e:
            yield sse("error", {"message": e.detail})
            return
        if ready:
            yield sse("ready", ready)
            return

        # 2. Make sure the work is running, then stream its events.
        _ensure_prepare_enqueued(db_file, user_id, mode)
        yield sse("progress", {"message": "Preparing your download…", "progress": 0})

        from redis.exceptions import RedisError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        client = aioredis.from_url(
            settings.REDIS_URL, health_check_interval=30, socket_keepalive=True
        )
        pubsub = client.pubsub()
        await pubsub.subscribe(download_events_channel(file_uuid))
        try:
            while True:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                except (RedisTimeoutError, asyncio.TimeoutError):
                    yield ": keepalive\n\n"  # benign idle read — keep the SSE open
                    continue
                except RedisError as e:
                    logger.warning(f"Download SSE pubsub error for {file_uuid}: {e}")
                    yield sse("error", {"message": "Connection interrupted."})
                    return
                if msg is None:
                    yield ": keepalive\n\n"  # comment frame keeps proxies from closing
                    continue
                try:
                    data = _json.loads(msg["data"])
                except Exception:
                    logger.debug("Skipping malformed download event payload")
                    continue
                if data.get("mode") != mode:
                    continue
                status = data.get("status")
                if status == "completed" and data.get("url"):
                    yield sse("ready", {"url": data["url"], "filename": data.get("filename", "")})
                    return
                if status == "error":
                    yield sse("error", {"message": data.get("message", "Download failed.")})
                    return
                yield sse(
                    "progress",
                    {
                        "message": data.get("message", ""),
                        "progress": data.get("progress", 0),
                    },
                )
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(download_events_channel(file_uuid))
                await pubsub.close()
                await client.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
        },
    )


@router.get("/{file_uuid}/thumbnail")
def get_thumbnail(
    file_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    """
    Get the thumbnail image for a media file.

    Security: Requires authentication OR file must be public. Authenticated org
    users are gated to their active tenant scope (cross-org thumbnails 403).

    Note: For gallery/list views, use the presigned thumbnail_url returned in the
    file listing response. This endpoint is a fallback for direct access.
    """
    from app.utils.uuid_helpers import get_file_by_uuid

    db_file = get_file_by_uuid(db, file_uuid)
    validate_file_exists(db_file)

    # Security check: must be public OR user must be authenticated and own file (or be admin)
    is_public = getattr(db_file, "is_public", False)
    if not is_public:
        if not current_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Use presigned thumbnail_url from file listing.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        is_admin = current_user.is_admin
        if not is_admin and db_file.user_id != current_user.id:
            from app.api.deps_context import resolve_org_context
            from app.services.permission_service import PermissionService

            # Resolve the caller's tenant scope (None = personal) WITHOUT editing
            # get_optional_current_user (owned by step 1.5), then route the share
            # resolution through the org-aware permission path (default-deny).
            org_id, _ = resolve_org_context(request, db, current_user)
            perm = PermissionService.get_file_permission(
                db, db_file.id, current_user.id, organization_id=org_id
            )
            if not perm:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this file",
                )
        elif not is_admin:
            # Direct owner: still enforce the tenant gate so a personal-scope
            # request can't fetch an org-stamped file's thumbnail (and vice versa).
            from app.api.deps_context import resolve_org_context

            org_id, _ = resolve_org_context(request, db, current_user)
            if getattr(db_file, "organization_id", None) != org_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this file",
                )

    return get_thumbnail_streaming_response(db_file)


@router.put("/{file_uuid}/transcript/segments/{segment_uuid}", response_model=TranscriptSegment)
def update_transcript_segment(
    file_uuid: str,
    segment_uuid: str,
    segment_update: TranscriptSegmentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Update a specific transcript segment"""
    from .crud import update_single_transcript_segment

    # Update the transcript segment (tenant-gated via ctx.org_id)
    result = update_single_transcript_segment(
        db, file_uuid, segment_uuid, segment_update, current_user, organization_id=ctx.org_id
    )

    # Transcript has been updated - subtitles will be regenerated on-demand

    return result


@router.post("/{file_uuid}/reprocess", response_model=MediaFileSchema)
def reprocess_media_file(
    file_uuid: str,
    reprocess_request: Optional[ReprocessRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Reprocess a media file for transcription with optional speaker diarization settings"""
    # Extract speaker parameters from request if provided
    min_speakers = reprocess_request.min_speakers if reprocess_request else None
    max_speakers = reprocess_request.max_speakers if reprocess_request else None
    num_speakers = reprocess_request.num_speakers if reprocess_request else None
    stages: list[str] = list(reprocess_request.stages) if reprocess_request else []
    whisper_model = reprocess_request.whisper_model if reprocess_request else None

    return process_file_reprocess(
        file_uuid,
        db,
        current_user,
        min_speakers,
        max_speakers,
        num_speakers,  # type: ignore[arg-type]
        stages=stages,
        whisper_model=whisper_model,
        organization_id=ctx.org_id,
    )


@router.delete("/{file_uuid}/cache", status_code=204)
def clear_video_cache(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Clear cached processed videos for a file (e.g., after speaker name updates)"""
    try:
        # Verify user owns the file or is admin (tenant-gated via ctx.org_id)
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, is_admin=is_admin, organization_id=ctx.org_id
        )
        file_id = db_file.id  # Get internal ID for cache operations

        # Clear the cache using video processing service
        from app.services.minio_service import MinIOService
        from app.services.video_processing_service import VideoProcessingService

        minio_service = MinIOService()
        video_service = VideoProcessingService(minio_service)

        # Clear cached videos for this file
        video_service.clear_cache_for_media_file(db, file_id)

        logger.info(f"Cleared video cache for file {file_id} after speaker updates")

        return None

    except Exception as e:
        logger.error(f"Error clearing video cache for file {file_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error clearing video cache: {str(e)}",
        ) from e


@router.get("/{file_uuid}/analytics")
def get_file_analytics(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """Get analytics for a media file (lightweight, no transcript/speaker data)."""
    from app.schemas.media import Analytics as AnalyticsSchema

    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=is_admin, organization_id=ctx.org_id
    )
    analytics = _get_or_compute_analytics(db, db_file.id, str(db_file.status))
    return {
        "analytics": AnalyticsSchema.model_validate(analytics) if analytics else None,
    }


@router.post("/{file_uuid}/analytics/refresh", status_code=204)
def refresh_analytics(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Refresh analytics for a media file by recomputing them"""
    try:
        # Verify user owns the file or is admin (tenant-gated via ctx.org_id)
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, is_admin=is_admin, organization_id=ctx.org_id
        )
        file_id = db_file.id  # Get internal ID for analytics refresh

        # Refresh analytics using the analytics service
        from app.services.analytics_service import AnalyticsService

        success = AnalyticsService.refresh_analytics(db, file_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to refresh analytics",
            )

        logger.info(f"Refreshed analytics for file {file_id}")

        return None

    except Exception as e:
        logger.error(f"Error refreshing analytics for file {file_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error refreshing analytics: {str(e)}",
        ) from e


__all__ = [
    "router",
    "process_file_upload",
    "process_file_reprocess",
    "get_media_file_detail",
    "update_media_file",
    "delete_media_file",
    "update_single_transcript_segment",
    "get_stream_url_info",
    "apply_all_filters",
    "get_metadata_filters",
    "validate_file_exists",
    "get_thumbnail_streaming_response",
]
