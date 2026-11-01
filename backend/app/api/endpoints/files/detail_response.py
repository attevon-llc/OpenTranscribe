"""The media file detail read: ``GET /api/files/{uuid}`` and the pieces of its response.

Tags, collections, URLs, analytics and error info for a file, and the assembly of the
``MediaFileDetail`` response around the redaction gate and the transcript view.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.api.error_handlers import ErrorHandler
from app.core.env import env_bool
from app.core.exceptions import DomainError
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import Analytics
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.speaker import Speaker
from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.user import User
from app.schemas.media import MediaFileDetail
from app.schemas.speaker import Speaker as SpeakerResponseSchema
from app.schemas.tag import Tag as TagSchema
from app.services.content_hold_queries import file_hold_summary
from app.services.file_provenance_view import build_processing_steps
from app.services.formatting_service import FormattingService
from app.services.platform_bypass import PlatformBypass
from app.services.speaker_provenance_view import ProvenanceViewer
from app.services.speaker_status_service import SpeakerStatusService
from app.services.tag_scope import tag_ownership

from .crud import get_media_file_by_uuid
from .redaction_gate import _redaction_pending
from .redaction_gate import _resolve_redaction_for_request
from .transcript_view import _build_grouped_segments
from .transcript_view import _count_speaker_segments
from .transcript_view import _format_transcript_segments
from .transcript_view import _get_transcript_segments
from .transcript_view import redact_response_speakers

logger = logging.getLogger(__name__)


def get_file_tags(db: Session, file_id: int, user_id: int) -> list[TagSchema]:
    """Get the tags attached to a media file as full tag objects.

    Returns the same ``{uuid, name, source}`` shape ``/api/tags`` serves, so the
    file-detail payload and the tag endpoints agree on one definition of a tag.

    Visibility is unchanged from the previous name-only serializer: this returns
    every tag attached to ``file_id``, and the caller has already been gated on
    the file itself. ``endpoints/tags.py:_visible_to`` makes a tag visible when it
    is attached to a file in the caller's accessible-files subquery, so any tag
    reachable here is one ``GET /api/tags`` would already return in full to the
    same caller. Widening the fields therefore discloses nothing new.

    ``ownership`` is classified against ``user_id`` rather than left to the
    schema default. This is the one tag surface where ``shared_with_me`` is
    reachable: a file shared with the caller carries its owner's tags, and
    reporting those as ``mine`` would have the UI offer a rename that can only
    404 (``endpoints/tags.py:_writable_tag_ids``).

    Args:
        db: Database session.
        file_id: Internal media file ID.
        user_id: The caller, for the ownership classification.

    Returns:
        Tag schemas for the file, or an empty list if the query fails.
    """
    try:
        tags = (
            db.query(Tag)
            .join(FileTag, FileTag.tag_id == Tag.id)
            .filter(FileTag.media_file_id == file_id)
            .all()
        )
        return [
            TagSchema.model_validate(
                {
                    "uuid": tag.uuid,
                    "name": tag.name,
                    "source": tag.source,
                    "ownership": tag_ownership(tag, user_id),
                }
            )
            for tag in tags
        ]
    except Exception as tag_error:
        logger.exception(f"Error getting tags: {tag_error}")
        db.rollback()
        return []


def get_file_collections(
    db: Session, file_id: int, user_id: int, organization_id: OrgScope = UNSCOPED
) -> list[dict]:
    """Get the caller's collections, in the file's tenant, that contain a media file.

    That is the caller's personal collections for a personal file and the
    organization's collections (shared by every member, v422) for an org file.
    ``organization_id`` is the request's tenant; ``UNSCOPED`` falls back to the
    file's own, which is the tenant any collection holding it lives in. The rule
    itself is ``collection_visibility.collections_on_files``, shared with the
    search result card so the two surfaces always name the same collections.
    """
    from app.services.collection_visibility import collections_on_files

    try:
        collection_objs = collections_on_files(
            db, [file_id], user_id, organization_id=organization_id
        ).get(file_id, [])

        return [
            {
                "uuid": str(col.uuid),
                "name": col.name,
                "description": col.description,
                "is_public": col.is_public,
                "created_at": col.created_at.isoformat() if col.created_at else None,
                "updated_at": col.updated_at.isoformat() if col.updated_at else None,
            }
            for col in collection_objs
        ]
    except Exception as collection_error:
        logger.exception(f"Error getting collections: {collection_error}")
        db.rollback()
        return []


def set_file_urls(db_file: MediaFile, bypass: PlatformBypass = PlatformBypass.none()) -> None:
    """
    Set download, preview, and thumbnail URLs for a media file.

    Thumbnails use presigned URLs (industry standard: Google, AWS, Apple).
    This embeds authentication in the URL itself, allowing <img> tags to load
    without HTTP auth headers.

    Args:
        db_file: MediaFile object to update
        bypass: The request's platform bypass; under a support-access grant the presigned
            thumbnail URL is capped to a few minutes (issue #1122).
    """
    from app.core.config import settings
    from app.services.minio_service import get_file_url

    if db_file.storage_path:
        # Skip S3 operations in test environment
        if env_bool("SKIP_S3"):
            # download_url/thumbnail_url are response-only fields stapled onto the ORM
            # instance for serialization (declared on schemas.media.MediaFile, not the model)
            db_file.download_url = f"/api/files/{db_file.uuid}/prepare-download"  # type: ignore[attr-defined]
            if db_file.thumbnail_path:
                db_file.thumbnail_url = f"/api/files/{db_file.uuid}/thumbnail"  # type: ignore[attr-defined]
            return

        # download_url is a presence/availability gate for the frontend download dropdown,
        # which performs the actual download via the async prepare-download + presigned-URL
        # flow (see TranscriptDisplay.downloadMedia). It is not a byte-proxy endpoint.
        db_file.download_url = f"/api/files/{db_file.uuid}/prepare-download"  # type: ignore[attr-defined]

        # Set thumbnail URL as presigned URL (industry standard)
        # This allows <img> tags to load without auth headers
        if db_file.thumbnail_path:
            try:
                db_file.thumbnail_url = get_file_url(  # type: ignore[attr-defined]
                    str(db_file.thumbnail_path),
                    expires=bypass.presign_ttl(settings.THUMBNAIL_URL_EXPIRE_SECONDS),
                )
            except Exception as e:
                logger.warning(f"Failed to generate presigned thumbnail URL: {e}")
                # Fallback to API endpoint (requires auth)
                db_file.thumbnail_url = f"/api/files/{db_file.uuid}/thumbnail"  # type: ignore[attr-defined]


def _get_or_compute_analytics(
    db: Session, file_id: int, file_status: FileStatus
) -> Analytics | None:
    """
    Get analytics for a file, computing them on-demand if needed.

    Args:
        db: Database session
        file_id: File ID
        file_status: Current status of the file

    Returns:
        Analytics object or None
    """
    analytics = db.query(Analytics).filter(Analytics.media_file_id == file_id).first()

    # Enum comparison — the old str-based form compared "FileStatus.COMPLETED"
    # against "completed" and was always True, so the on-demand computation
    # below never ran (issue #272).
    if analytics or file_status != FileStatus.COMPLETED:
        return analytics  # type: ignore[no-any-return]

    # Compute analytics on-demand for completed files
    from app.services.analytics_service import AnalyticsService

    logger.info(f"Computing missing analytics on-demand for file {file_id}")
    success = AnalyticsService.compute_and_save_analytics(db, file_id)
    if success:
        analytics = db.query(Analytics).filter(Analytics.media_file_id == file_id).first()

    return analytics  # type: ignore[no-any-return]


def _add_error_info_to_response(response: MediaFileDetail, db_file: MediaFile) -> None:
    """
    Add error categorization information to response for failed files.

    Args:
        response: MediaFileDetail response to update
        db_file: MediaFile object
    """
    from app.services.error_categorization_service import ErrorCategorizationService

    fields = ErrorCategorizationService.error_fields_for(db_file)
    if fields is None:
        return
    response.error_reason = fields["error_reason"]
    response.error_suggestions = fields["error_suggestions"]
    response.user_message = fields["user_message"]
    response.is_retryable = fields["is_retryable"]


def _build_media_file_response(
    db_file: MediaFile,
    tags: list[TagSchema],
    collections: list[dict[Any, Any]],
    speakers: list[Speaker],
    analytics: Analytics | None,
    transcript_segments: list[TranscriptSegment],
    total_segments: int,
    total_speaker_segments: int,
    segment_limit: int | None,
    segment_offset: int,
    redaction_cfg: Any = None,
    reveal_categories: set | None = None,
) -> MediaFileDetail:
    """
    Build the MediaFileDetail response with all formatted fields.

    Args:
        db_file: MediaFile object
        tags: Tag objects attached to the file ({uuid, name, source})
        collections: List of collection dictionaries
        speakers: List of speakers
        analytics: Analytics object or None
        transcript_segments: List of transcript segments
        total_segments: Total segment count for pagination
        segment_limit: Segment pagination limit
        segment_offset: Segment pagination offset

    Returns:
        MediaFileDetail response object
    """
    response = MediaFileDetail.model_validate(db_file)
    response.tags = tags
    response.collections = collections  # type: ignore[assignment]
    # Validated into the response schema here, while the instances are loaded: that is
    # what maps the internal user/file ids to UUIDs. Assigning the ORM rows directly
    # left serialization to whatever the instance held at response time.
    response.speakers = [SpeakerResponseSchema.model_validate(s) for s in speakers]

    # Set lightweight summary indicator and strip heavy JSONB from response
    response.has_summary = bool(
        getattr(db_file, "summary_opensearch_id", None)
        or getattr(db_file, "summary_status", None) == "completed"
        or response.summary_data
    )
    response.summary_data = None  # Full summary fetched via /summary endpoint

    # Convert analytics to schema if it exists
    if analytics:
        from app.schemas.media import Analytics as AnalyticsSchema

        response.analytics = AnalyticsSchema.model_validate(analytics)
    else:
        response.analytics = None

    # Add formatted fields
    # Extract values to ensure type compatibility
    duration_val: float | None = float(db_file.duration) if db_file.duration else None
    upload_time_val: datetime | None = db_file.upload_time if db_file.upload_time else None
    file_size_val: int | None = int(db_file.file_size) if db_file.file_size else None

    response.formatted_duration = FormattingService.format_duration(duration_val)
    response.formatted_upload_date = FormattingService.format_upload_date(upload_time_val)
    response.formatted_file_age = FormattingService.format_file_age(upload_time_val)
    response.formatted_file_size = FormattingService.format_bytes_detailed(file_size_val)
    response.display_status = FormattingService.format_status(FileStatus(db_file.status))
    response.status_badge_class = FormattingService.get_status_badge_class(db_file.status.value)
    response.speaker_summary = FormattingService.create_speaker_summary(speakers)

    # Add error info if applicable
    _add_error_info_to_response(response, db_file)

    # Format and add transcript segments (with optional read-time redaction)
    formatted_segments = _format_transcript_segments(
        transcript_segments, speakers, redaction_cfg, reveal_categories
    )
    response.transcript_segments = formatted_segments  # type: ignore[assignment]

    # Pre-grouped view (overlap groups kept together) for the frontend to render directly.
    response.grouped_segments = _build_grouped_segments(  # type: ignore[assignment]
        formatted_segments, index_offset=segment_offset
    )

    # Add pagination metadata
    response.total_segments = total_segments
    response.total_speaker_segments = total_speaker_segments
    response.segment_limit = segment_limit
    response.segment_offset = segment_offset

    return response  # type: ignore[no-any-return]


def _load_detail_file(
    db: Session,
    file_uuid: str,
    current_user: User,
    *,
    bypass: PlatformBypass,
    organization_id: OrgScope,
) -> MediaFile:
    """Load the file a detail read is for, answering a forbidden file like a missing one.

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user
        bypass: The request's platform bypass (``ctx.bypass``).
        organization_id: The requester's active tenant scope.

    Returns:
        The MediaFile, with its heavy JSONB columns expired.

    Raises:
        HTTPException: 404 when the file is missing or the caller may not see it.
    """
    try:
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, bypass=bypass, organization_id=organization_id
        )
    except (HTTPException, DomainError) as denied:
        if denied.status_code != status.HTTP_403_FORBIDDEN:
            raise
        # A file the caller cannot see must answer like a missing one: 403 would
        # confirm the UUID exists in another user's/tenant's scope.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        ) from None
    # Expire heavy JSONB columns so they're not loaded unless explicitly accessed
    # (waveform_data fetched via /waveform, summary via /summary, metadata_raw rarely needed)
    db.expire(db_file, ["waveform_data", "metadata_raw"])
    return db_file


def _load_file_speakers(db: Session, file_id: int) -> list[Speaker]:
    """Load a file's speakers with their linked profiles.

    The profile is eager-loaded so add_computed_status (which reads speaker.profile)
    doesn't fire one lazy SELECT per speaker.
    """
    return (
        db.query(Speaker)
        .options(selectinload(Speaker.profile))
        .filter(Speaker.media_file_id == file_id)
        .all()
    )


def _redact_and_decorate_speakers(
    db: Session,
    speakers: list[Speaker],
    transcript_segments: list[TranscriptSegment],
    current_user: User,
    *,
    bypass: PlatformBypass,
    organization_id: OrgScope,
    reveal_hold_uuids: bool,
) -> None:
    """Redact every speaker row the detail response serializes, then add its computed status.

    Must run as the last step before serialization: nothing after it commits, so the
    redaction holds. Redact FIRST, then compute the status from what is left: the status
    text ("High confidence match") and the resolved name are derived from the suggestion
    that the redaction withholds, so decorating before it served both while voiceprints
    were off.
    """
    redact_response_speakers(
        db,
        speakers,
        transcript_segments,
        ProvenanceViewer(
            user_id=int(current_user.id),
            admin_org_id=organization_id
            if reveal_hold_uuids and isinstance(organization_id, int)
            else None,
            bypass=bypass,
        ),
    )
    processed_speaker_ids: set[int] = set()
    for speaker in [*speakers, *(s.speaker for s in transcript_segments if s.speaker)]:
        if int(speaker.id) not in processed_speaker_ids:
            SpeakerStatusService.add_computed_status(speaker)
            processed_speaker_ids.add(int(speaker.id))


def get_media_file_detail(
    db: Session,
    file_uuid: str,
    current_user: User,
    segment_limit: int | None = None,
    segment_offset: int = 0,
    redact: bool = True,
    *,
    organization_id: OrgScope = UNSCOPED,
    bypass: PlatformBypass = PlatformBypass.none(),
    reveal_hold_uuids: bool = False,
) -> MediaFileDetail:
    """
    Get detailed media file information including tags, analytics, and formatted fields.

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user
        segment_limit: Maximum number of transcript segments to return (None = all)
        segment_offset: Offset for transcript segment pagination (default 0)
        bypass: The request's platform bypass (``ctx.bypass``).
        reveal_hold_uuids: Include the organization hold uuids in ``hold`` (the caller
            administers the file's organization). Who holds the file is shown to everyone.

    Returns:
        MediaFileDetail object with all computed and formatted data
    """
    try:
        db_file = _load_detail_file(
            db, file_uuid, current_user, bypass=bypass, organization_id=organization_id
        )
        file_id = db_file.id

        # Get related data
        tags = get_file_tags(db, file_id, current_user.id)
        collections = get_file_collections(
            db, file_id, current_user.id, organization_id=organization_id
        )

        # Get analytics (compute on-demand if needed). First, because computing them is
        # the read path's one legitimate write and commits — which expires every loaded
        # instance, including the speakers decorated below (#1152).
        analytics = _get_or_compute_analytics(db, file_id, db_file.status)

        speakers = _load_file_speakers(db, file_id)

        # Get transcript segments with pagination
        transcript_segments, total_segments = _get_transcript_segments(
            db, file_id, segment_limit, segment_offset
        )

        # Count merged speaker segments (adjacent same-speaker groups)
        total_speaker_segments = _count_speaker_segments(db, file_id)

        # Compute caller's effective permission for frontend
        if db_file.user_id == current_user.id:
            my_permission = None  # Actual owner (frontend convention: null = owner)
        elif bypass.allows(
            org_id=db_file.organization_id,
            owner_id=db_file.user_id,
            need="read",
            resource_type="media_file",
            resource_uuid=str(db_file.uuid),
        ):
            my_permission = "owner"  # Admin viewing someone else's file
        else:
            from app.services.permission_service import PermissionService

            my_permission = PermissionService.get_file_permission(
                db, file_id, current_user.id, organization_id=organization_id
            )

        # Set URLs
        set_file_urls(db_file, bypass)

        # Resolve read-time redaction config for the caller.
        redaction_cfg, reveal_categories = _resolve_redaction_for_request(
            db,
            db_file,
            current_user,
            bypass=bypass,
            redact=redact,
            organization_id=organization_id,
        )

        # If redaction is enabled but detection hasn't finished, withhold the transcript
        # until it's ready (no un-redacted display window).
        pending = _redaction_pending(db, redaction_cfg, db_file)
        if pending:
            transcript_segments = []
            total_segments = 0
            total_speaker_segments = 0

        # Last step before serialization: nothing below commits, so the redaction holds.
        _redact_and_decorate_speakers(
            db,
            speakers,
            transcript_segments,
            current_user,
            bypass=bypass,
            organization_id=organization_id,
            reveal_hold_uuids=reveal_hold_uuids,
        )

        # Build the response
        response = _build_media_file_response(
            db_file=db_file,
            tags=tags,
            collections=collections,
            speakers=speakers,
            analytics=analytics,
            transcript_segments=transcript_segments,
            total_segments=total_segments,
            total_speaker_segments=total_speaker_segments,
            segment_limit=segment_limit,
            segment_offset=segment_offset,
            redaction_cfg=redaction_cfg,
            reveal_categories=reveal_categories,
        )
        response.redaction_pending = pending
        response.redaction_enabled = bool(redaction_cfg.masks_anything)
        response.redaction_status = (
            str(db_file.redaction_status) if db_file.redaction_status else None
        )

        # Set caller's permission on the response
        response.my_permission = my_permission
        response.processing_steps = build_processing_steps(db_file)
        response.hold = file_hold_summary(db, int(file_id), reveal_uuids=reveal_hold_uuids)

        return response

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        # The real failure (often a SQL fragment or a DSN) is logged with a
        # traceback, never returned to the caller (#859).
        logger.exception("Error in get_media_file_detail")
        raise ErrorHandler.internal_error("Could not retrieve this media file.") from e
