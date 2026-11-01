"""
Files API module - refactored for modularity.

This module contains the refactored files endpoint split into modular components:
- upload.py: File upload functionality
- crud.py: Basic CRUD operations
- filtering.py: Complex filtering logic for file listing
- streaming.py: Video/audio streaming endpoints
- listing.py: The gallery list, its metadata filters and the owner facet
- single_file.py: Single-file detail, info, update and the presigned stream URL
- media_delivery.py: Download preparation, the download SSE stream and the thumbnail
"""

import logging
from typing import Any
from typing import NamedTuple

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.core.capabilities import require_capability
from app.core.env import is_truthy
from app.core.exceptions import DomainError
from app.core.locked_settings import effective_whisper_model
from app.db.base import get_db
from app.models.user import User
from app.schemas.media import MediaFile as MediaFileSchema
from app.schemas.media import PaginatedMediaFileResponse
from app.schemas.processing_request import ReprocessRequest
from app.schemas.transcript import TranscriptSegment
from app.schemas.transcript import TranscriptSegmentUpdate
from app.services.request_context import RequestContext
from app.utils.whisper_model_choice import require_servable_whisper_model

from . import cancel_upload
from . import complete_upload
from . import listing
from . import media_delivery
from . import multipart
from . import prepare_upload
from . import single_file
from .captions import router as captions_router
from .crud import delete_media_file
from .crud import get_media_file_by_uuid
from .crud import get_stream_url_info
from .crud import update_media_file
from .crud import update_single_transcript_segment
from .detail_response import _get_or_compute_analytics
from .detail_response import get_media_file_detail
from .filtering import apply_all_filters
from .filtering import get_metadata_filters
from .listing import list_media_files
from .post_processing import router as post_processing_router
from .reprocess import process_file_reprocess
from .segments import router as segments_router
from .streaming import get_thumbnail_streaming_response
from .streaming import validate_file_exists
from .subtitles import router as subtitles_router
from .summary_status import router as summary_status_router
from .transcript_document_export import router as transcript_document_export_router
from .transcript_export import router as transcript_export_router
from .upload import process_file_upload
from .url_processing import router as url_processing_router
from .waveform import router as waveform_router

# Create the router
router = APIRouter()
logger = logging.getLogger(__name__)


class SpeakerParams(NamedTuple):
    """Speaker diarization parameters parsed from request headers."""

    min_speakers: int | None
    max_speakers: int | None
    num_speakers: int | None


def _parse_speaker_params_from_headers(request: Request | None) -> SpeakerParams:
    """
    Parse speaker diarization parameters from request headers.

    Returns SpeakerParams with validated values. Invalid values are logged and set to None.
    If min_speakers > max_speakers, both are reset to None.
    """
    if not request:
        return SpeakerParams(None, None, None)

    def parse_int_header(header_name: str) -> int | None:
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
router.include_router(multipart.router, prefix="", tags=["files"])
router.include_router(subtitles_router, prefix="", tags=["subtitles"])
router.include_router(transcript_export_router, prefix="", tags=["files"])
router.include_router(transcript_document_export_router, prefix="", tags=["files"])
router.include_router(waveform_router, prefix="", tags=["waveform"])
# URL import is a deployment decision (legal exposure, egress), so no account
# bypasses it when the capability is off.
router.include_router(
    url_processing_router,
    prefix="",
    tags=["url-processing"],
    dependencies=[Depends(require_capability("url_ingest", platform_admin_bypass=False))],
)
router.include_router(segments_router, prefix="", tags=["files"])
router.include_router(captions_router, prefix="", tags=["files"])
router.include_router(summary_status_router, prefix="", tags=["summary"])
router.include_router(post_processing_router, prefix="", tags=["files"])


@router.post("", response_model=MediaFileSchema, dependencies=[Depends(refuse_under_support_grant)])
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
        skip_summary = is_truthy(skip_summary_header)

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


# Registration order is the original route order, which FastAPI matches in: the empty
# path, the literal listing paths (/metadata-filters, /owners), then the /{file_uuid}
# routes. An empty path cannot be included with an empty prefix, so it is registered here.
router.get("", response_model=PaginatedMediaFileResponse)(list_media_files)
router.include_router(listing.router)
router.include_router(single_file.router)
router.include_router(media_delivery.router)


@router.put("/{file_uuid}/transcript/segments/{segment_uuid}", response_model=TranscriptSegment)
def update_transcript_segment(
    file_uuid: str,
    segment_uuid: str,
    segment_update: TranscriptSegmentUpdate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Update a specific transcript segment"""
    from .crud import update_single_transcript_segment

    # Update the transcript segment (tenant-gated via ctx.org_id)
    result = update_single_transcript_segment(
        db,
        file_uuid,
        segment_uuid,
        segment_update,
        current_user,
        organization_id=ctx.org_id,
        bypass=ctx.bypass,
        request=http_request,
    )

    # Transcript has been updated - subtitles will be regenerated on-demand

    return result


@router.post("/{file_uuid}/reprocess", response_model=MediaFileSchema)
def reprocess_media_file(
    file_uuid: str,
    http_request: Request,
    reprocess_request: ReprocessRequest | None = None,
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
    disable_diarization = bool(reprocess_request and reprocess_request.disable_diarization)
    whisper_model = require_servable_whisper_model(
        effective_whisper_model(
            reprocess_request.whisper_model if reprocess_request else None, http_request
        )
    )

    return process_file_reprocess(
        file_uuid,
        db,
        current_user,
        min_speakers,
        max_speakers,
        num_speakers,  # type: ignore[arg-type]
        stages=stages,
        whisper_model=whisper_model,
        disable_diarization=disable_diarization,
        organization_id=ctx.org_id,
        bypass=ctx.bypass,
    )


def _clear_derived_cache(file_id: int, filename: str) -> None:
    """Drop a file's regenerable derived cache. **Touches no database session.**

    Deliberately module-level rather than inline in the handler below: five
    MinIO deletes plus the bucket check in ``VideoProcessingService.__init__``
    are network round trips, and running them from inside the request handler
    kept the request transaction — and ``ACCESS SHARE`` on ``media_file`` —
    open for their whole duration (``app/tasks/CLAUDE.md``).

    Args:
        file_id: Internal media file id.
        filename: The file's original filename; the cache keys derive from it.
    """
    from app.services.minio_bucket_admin import MinIOService
    from app.services.video_processing_service import VideoProcessingService

    VideoProcessingService(MinIOService()).clear_derived_cache(file_id, filename)


@router.delete("/{file_uuid}/cache", status_code=204)
def clear_video_cache(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Clear cached processed videos for a file (e.g., after speaker name updates)"""
    # Authorization runs OUTSIDE the try: get_media_file_by_uuid raises HTTPException
    # 403/404, and a broad `except Exception` around it re-wrapped that as a 500 — hiding
    # the authz result — then referenced the still-unassigned `file_id` in its own log
    # line and crashed a second time with NameError (issue #284 A0.6).
    db_file = get_media_file_by_uuid(
        db,
        file_uuid,
        current_user.id,
        bypass=ctx.bypass,
        organization_id=ctx.org_id,
        min_permission="editor",
    )
    file_id = int(db_file.id)  # Internal ID for cache operations
    filename = str(db_file.filename)  # Plain data: the cache keys derive from it

    # End the authorization read's transaction before the object-storage work
    # below, which touches no database.
    db.commit()

    try:
        # Clear cached videos for this file
        _clear_derived_cache(file_id, filename)

        logger.info(f"Cleared video cache for file {file_id} after speaker updates")

        return None

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error clearing video cache for file {file_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error clearing video cache",
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

    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
    )
    # Enum comparison (issue #272) — str(status) never matched "completed",
    # so this endpoint could also never compute analytics on demand.
    analytics = _get_or_compute_analytics(db, db_file.id, db_file.status)
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
    # Authorization runs OUTSIDE the try — see clear_video_cache above for why
    # (issue #284 A0.6). The `raise HTTPException(500)` below was also swallowed by the
    # broad handler and re-wrapped, doubling the `from e` chain.
    db_file = get_media_file_by_uuid(
        db,
        file_uuid,
        current_user.id,
        bypass=ctx.bypass,
        organization_id=ctx.org_id,
        min_permission="editor",
    )
    file_id = db_file.id  # Internal ID for analytics refresh

    try:
        from app.services.analytics_service import AnalyticsService

        success = AnalyticsService.refresh_analytics(db, file_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to refresh analytics",
            )

        logger.info(f"Refreshed analytics for file {file_id}")

        return None

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"Error refreshing analytics for file {file_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error refreshing analytics",
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
