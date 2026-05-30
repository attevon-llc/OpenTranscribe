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
from .streaming import get_content_streaming_response
from .streaming import get_enhanced_video_streaming_response
from .streaming import get_thumbnail_streaming_response
from .streaming import get_video_streaming_response
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
    current_user: User = Depends(get_current_active_user),
    request: Request = None,  # type: ignore[assignment]
):
    """Upload a media file for transcription"""
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
    )  # whisper_model auto-read from db_file.requested_whisper_model inside

    # Invalidate caches so gallery picks up the new file
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_user_files(int(current_user.id))
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
    current_user: User = Depends(get_current_active_user),
):
    """List media files for the current user with optional filters and pagination.

    Use ownership param to control scope:
    - 'mine': Only files owned by current user (default, preserves existing behavior)
    - 'shared': Only files accessible via shared collections
    - 'all': Both owned and shared files
    """
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

    user_id = int(current_user.id)

    # Admin users can see all files regardless of ownership param
    if current_user.is_admin:
        base_query = db.query(MediaFile).options(*list_options)
        effective_user_id = None
    elif ownership == "mine":
        # Default: only owned files
        base_query = db.query(MediaFile).options(*list_options).filter(MediaFile.user_id == user_id)
        effective_user_id = user_id
    elif ownership == "shared":
        # Only files from shared collections (not owned by user)
        accessible_subquery = PermissionService.get_accessible_file_ids_subquery(db, user_id)
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
        accessible_subquery = PermissionService.get_accessible_file_ids_subquery(db, user_id)
        base_query = (
            db.query(MediaFile)
            .options(*list_options)
            .filter(MediaFile.id.in_(db.query(accessible_subquery.c.id)))
        )
        effective_user_id = None

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
    current_user: User = Depends(get_current_active_user),
):
    """Get available metadata filters like formats, codecs, etc."""
    return get_metadata_filters(db, int(current_user.id), ownership=ownership)


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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get a specific media file with transcript details.

    For large transcripts, use segment_limit and segment_offset for pagination.
    Default returns first 500 segments. Use segment_limit=0 to get all segments.
    """
    # segment_limit=0 means get all segments
    effective_limit = None if segment_limit == 0 else segment_limit
    return get_media_file_detail(db, str(file_uuid), current_user, effective_limit, segment_offset)


@router.get("/{file_uuid}/info", response_model=MediaFilePublicInfo)
def get_media_file_info(
    file_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get lightweight file metadata without transcript or summary data.

    Returns core identity, status, and technical metadata fields only.
    Useful for integrations that need file context without heavy payloads.
    """
    from app.utils.uuid_helpers import get_file_by_uuid_with_permission

    media_file = get_file_by_uuid_with_permission(
        db, str(file_uuid), int(current_user.id), is_admin=current_user.is_admin
    )

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
    current_user: User = Depends(get_current_active_user),
):
    """Update a media file's metadata"""
    return update_media_file(db, str(file_uuid), media_file_update, current_user)


@router.delete("/{file_uuid}", status_code=204)
def delete_media_file_endpoint(
    file_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete a media file and all associated data"""
    delete_media_file(db, str(file_uuid), current_user)
    return None


@router.get("/{file_uuid}/stream-url")
def get_media_file_stream_url(
    file_uuid: str,
    media_type: str = Query("video", description="Type of media: video, thumbnail, or audio"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
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

    # Verify user has permission (ownership check)
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)

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


@router.get("/{file_uuid}/content")
def get_media_file_content(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get the content of a media file"""
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)
    return get_content_streaming_response(db_file)


def _process_video_download_with_subtitles(
    db: Session,
    db_file: MediaFile,
    user_id: int,
    include_speakers: bool,
    endpoint_name: str = "download",
) -> Optional[StreamingResponse]:
    """
    Process a video file and embed subtitles for download.

    Args:
        db: Database session
        db_file: The media file database object
        user_id: The user ID requesting the download
        include_speakers: Whether to include speaker labels in subtitles
        endpoint_name: Name of the endpoint for logging (e.g., "download" or "token endpoint")

    Returns:
        StreamingResponse with the processed video, or None if processing fails
        (caller should fall back to original file on None)
    """
    from app.services.minio_service import MinIOService
    from app.services.video_processing_service import VideoProcessingService

    file_id = int(db_file.id)
    file_uuid = str(db_file.uuid)

    try:
        logger.info(
            f"Processing video download with subtitles for file {file_uuid} "
            f"(id: {file_id}, {endpoint_name})"
        )

        # Initialize services
        minio_service = MinIOService()
        video_service = VideoProcessingService(minio_service)

        # Check if ffmpeg is available
        if not video_service.check_ffmpeg_availability():
            logger.warning(
                f"ffmpeg not available, serving original file for {file_uuid} "
                f"(id: {file_id}, {endpoint_name})"
            )
            return None

        logger.info(
            f"ffmpeg available, processing video {file_uuid} (id: {file_id}) "
            f"with subtitles ({endpoint_name})"
        )

        # Process video with embedded subtitles
        cache_key = video_service.process_video_with_subtitles(
            db=db,
            file_id=file_id,
            original_object_name=str(db_file.storage_path),
            user_id=user_id,
            include_speakers=include_speakers,
            output_format="mp4",
        )

        logger.info(f"Video processing complete, streaming processed video: {cache_key}")

        # Stream the processed video through backend
        # Note: request object not available here, will handle basic streaming
        file_stream, _, _, total_length = video_service._get_cache_file_stream(
            cache_key,
            "",  # type: ignore[arg-type]
        )

        # Generate proper filename for download
        base_name = (
            db_file.filename.rsplit(".", 1)[0] if "." in db_file.filename else db_file.filename
        )
        download_filename = f"{base_name}_with_subtitles.mp4"

        headers = {
            "Content-Disposition": f'attachment; filename="{download_filename}"',
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
        }

        if total_length:
            headers["Content-Length"] = str(total_length)

        return StreamingResponse(content=file_stream, media_type="video/mp4", headers=headers)

    except Exception as e:
        logger.error(
            f"Failed to process video with subtitles for file {file_id} ({endpoint_name}): {e}",
            exc_info=True,
        )
        return None


def _audio_source_extension(filename: str) -> str:
    """Return the lowercase extension of a filename without the dot."""
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def _process_audio_only_download(
    db: Session,
    db_file: MediaFile,
    audio_format: str,
) -> StreamingResponse:
    """Extract (or pass through) the audio track of a media file for download.

    Re-encodes to MP3/WAV, or stream-copies the source codec for ``original``.
    Audio sources already in the requested format are streamed as-is (no ffmpeg).
    """
    from app.services.minio_service import MinIOService
    from app.services.video_processing_service import NoAudioTrackError
    from app.services.video_processing_service import VideoProcessingService

    normalized = audio_format.lower()
    is_audio_source = bool(db_file.content_type and db_file.content_type.startswith("audio/"))
    source_ext = _audio_source_extension(str(db_file.filename))

    # Short-circuit: audio source already in the requested format -> serve original bytes.
    if is_audio_source and (
        normalized == "original"
        or (normalized == "mp3" and source_ext == "mp3")
        or (normalized == "wav" and source_ext == "wav")
    ):
        return get_content_streaming_response(db_file)

    minio_service = MinIOService()
    video_service = VideoProcessingService(minio_service)

    if not video_service.check_ffmpeg_availability():
        raise HTTPException(status_code=502, detail="Audio extraction is unavailable (no ffmpeg).")

    try:
        cache_key, ext, content_type = video_service.extract_audio(
            db=db,
            file_id=int(db_file.id),
            original_object_name=str(db_file.storage_path),
            audio_format=normalized,
        )
    except NoAudioTrackError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Audio extraction failed for file {db_file.uuid}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to extract audio.") from e

    file_stream, _, _, total_length = video_service._get_cache_file_stream(cache_key, None)

    base_name = db_file.filename.rsplit(".", 1)[0] if "." in db_file.filename else db_file.filename
    download_filename = f"{base_name}.{ext}"
    headers = {
        "Content-Disposition": f'attachment; filename="{download_filename}"',
        "Content-Type": content_type,
    }
    if total_length:
        headers["Content-Length"] = str(total_length)

    return StreamingResponse(content=file_stream, media_type=content_type, headers=headers)


@router.get("/{file_uuid}/download")
def download_media_file(
    file_uuid: str,
    token: str | None = None,
    original: bool = Query(False, description="Download original file without subtitles"),
    include_speakers: bool = Query(True, description="Include speaker labels in subtitles"),
    audio_only: bool = Query(False, description="Download only the audio track"),
    audio_format: str = Query("mp3", description="Audio format: mp3, wav, or original"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Download a media file (with embedded subtitles for videos by default)"""
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)

    # Audio-only takes precedence over the video-subtitle path.
    if audio_only:
        return _process_audio_only_download(db, db_file, audio_format)

    # Check if this is a video file with available subtitles
    is_video = db_file.content_type and db_file.content_type.startswith("video/")
    has_transcript = db_file.status == "completed"

    # Always embed subtitles for videos when available, unless user explicitly requests original
    if is_video and has_transcript and not original:
        response = _process_video_download_with_subtitles(
            db=db,
            db_file=db_file,
            user_id=int(current_user.id),
            include_speakers=include_speakers,
            endpoint_name="download",
        )
        if response is not None:
            return response
        # Fall through to return original file on processing failure

    # Return original file
    return get_content_streaming_response(db_file)


@router.get("/{file_uuid}/download-with-token")
def download_media_file_with_token(
    file_uuid: str,
    token: str,
    original: bool = Query(False, description="Download original file without subtitles"),
    include_speakers: bool = Query(True, description="Include speaker labels in subtitles"),
    audio_only: bool = Query(False, description="Download only the audio track"),
    audio_format: str = Query("mp3", description="Audio format: mp3, wav, or original"),
    db: Session = Depends(get_db),
):
    """
    Download a media file using token parameter (for native browser downloads)
    No authentication required - token is validated manually
    """
    from jose import JWTError
    from jose import jwt

    from app.core.config import settings

    try:
        # Validate JWT token manually
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_uuid: str = payload.get("sub")
        if user_uuid is None:
            raise HTTPException(status_code=401, detail="Invalid token")

        # Get user from database by UUID (token contains UUID, not integer ID)
        user = db.query(User).filter(User.uuid == user_uuid).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="Invalid user")

        # Get file and check ownership
        is_admin = user.is_admin
        db_file = get_media_file_by_uuid(db, file_uuid, int(user.id), is_admin=is_admin)

        # Audio-only takes precedence over the video-subtitle path.
        if audio_only:
            return _process_audio_only_download(db, db_file, audio_format)

        # Check if this is a video file with available subtitles
        is_video = db_file.content_type and db_file.content_type.startswith("video/")
        has_transcript = db_file.status == "completed"

        # Always embed subtitles for videos when available, unless user explicitly requests original
        if is_video and has_transcript and not original:
            response = _process_video_download_with_subtitles(
                db=db,
                db_file=db_file,
                user_id=int(user.id),
                include_speakers=include_speakers,
                endpoint_name="token endpoint",
            )
            if response is not None:
                return response
            # Fall through to return original file on processing failure

        # Return original file
        return get_content_streaming_response(db_file)

    except JWTError as e:
        raise HTTPException(status_code=401, detail="Invalid token") from e
    except HTTPException:
        # Preserve real status codes (e.g. 422 no-audio, 502 no-ffmpeg) rather than
        # masking them as auth failures.
        raise
    except Exception as e:
        logger.error(f"Error in download with token: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed") from e


@router.get("/{file_uuid}/video")
async def video_file(
    file_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    """
    Direct video endpoint for video player.

    Security: Requires authentication OR file must be public.
    For secure access, use GET /{file_uuid}/stream-url to get a presigned URL instead.
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
                detail="Authentication required for private files. Use /stream-url endpoint for presigned URLs.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        is_admin = current_user.is_admin
        if not is_admin and db_file.user_id != current_user.id:
            from app.services.permission_service import PermissionService

            perm = PermissionService.get_file_permission(db, int(db_file.id), int(current_user.id))
            if not perm:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this file",
                )

    range_header = request.headers.get("range") or ""
    return get_video_streaming_response(db_file, range_header)


@router.get("/{file_uuid}/simple-video")
async def simple_video(
    file_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    """
    Enhanced video streaming endpoint with YouTube-like streaming.

    Security: Requires authentication OR file must be public.
    For secure access, use GET /{file_uuid}/stream-url to get a presigned URL instead.
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
                detail="Authentication required for private files. Use /stream-url endpoint for presigned URLs.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        is_admin = current_user.is_admin
        if not is_admin and db_file.user_id != current_user.id:
            from app.services.permission_service import PermissionService

            perm = PermissionService.get_file_permission(db, int(db_file.id), int(current_user.id))
            if not perm:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this file",
                )

    range_header = request.headers.get("range") or ""
    return get_enhanced_video_streaming_response(db_file, range_header)


@router.get("/{file_uuid}/thumbnail")
async def get_thumbnail(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    """
    Get the thumbnail image for a media file.

    Security: Requires authentication OR file must be public.

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
            from app.services.permission_service import PermissionService

            perm = PermissionService.get_file_permission(db, int(db_file.id), int(current_user.id))
            if not perm:
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
):
    """Update a specific transcript segment"""
    from .crud import update_single_transcript_segment

    # Update the transcript segment
    result = update_single_transcript_segment(
        db, file_uuid, segment_uuid, segment_update, current_user
    )

    # Transcript has been updated - subtitles will be regenerated on-demand

    return result


@router.post("/{file_uuid}/reprocess", response_model=MediaFileSchema)
async def reprocess_media_file(
    file_uuid: str,
    reprocess_request: Optional[ReprocessRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Reprocess a media file for transcription with optional speaker diarization settings"""
    # Extract speaker parameters from request if provided
    min_speakers = reprocess_request.min_speakers if reprocess_request else None
    max_speakers = reprocess_request.max_speakers if reprocess_request else None
    num_speakers = reprocess_request.num_speakers if reprocess_request else None
    stages: list[str] = list(reprocess_request.stages) if reprocess_request else []
    whisper_model = reprocess_request.whisper_model if reprocess_request else None

    return await process_file_reprocess(
        file_uuid,
        db,
        current_user,
        min_speakers,
        max_speakers,
        num_speakers,  # type: ignore[arg-type]
        stages=stages,
        whisper_model=whisper_model,
    )


@router.delete("/{file_uuid}/cache", status_code=204)
def clear_video_cache(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Clear cached processed videos for a file (e.g., after speaker name updates)"""
    try:
        # Verify user owns the file or is admin
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)
        file_id = int(db_file.id)  # Get internal ID for cache operations

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
) -> dict[str, Any]:
    """Get analytics for a media file (lightweight, no transcript/speaker data)."""
    from app.schemas.media import Analytics as AnalyticsSchema

    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)
    analytics = _get_or_compute_analytics(db, int(db_file.id), str(db_file.status))
    return {
        "analytics": AnalyticsSchema.model_validate(analytics) if analytics else None,
    }


@router.post("/{file_uuid}/analytics/refresh", status_code=204)
def refresh_analytics(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Refresh analytics for a media file by recomputing them"""
    try:
        # Verify user owns the file or is admin
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)
        file_id = int(db_file.id)  # Get internal ID for analytics refresh

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
    "get_content_streaming_response",
    "get_video_streaming_response",
    "get_enhanced_video_streaming_response",
    "validate_file_exists",
    "get_thumbnail_streaming_response",
]
