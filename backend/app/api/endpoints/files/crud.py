import logging
import os
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import selectinload

from app.core import constants as C  # noqa: N812
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import Analytics
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import FileStatus
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Tag
from app.models.media import TranscriptSegment
from app.models.user import User
from app.schemas.media import MediaFileDetail
from app.schemas.media import MediaFileUpdate
from app.schemas.media import Tag as TagSchema
from app.schemas.media import TranscriptSegment as TranscriptSegmentSchema
from app.schemas.media import TranscriptSegmentUpdate
from app.services.formatting_service import FormattingService
from app.services.opensearch_service import update_transcript_title
from app.services.speaker_status_service import SpeakerStatusService
from app.services.tag_service import tag_ownership
from app.utils.time_format import format_timestamp_simple as format_timestamp
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

logger = logging.getLogger(__name__)


def get_media_file_by_uuid(
    db: Session,
    file_uuid: str,
    user_id: int,
    is_admin: bool = False,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> MediaFile:
    """
    Get a media file by UUID and user ID.

    Args:
        db: Database session
        file_uuid: File UUID
        user_id: User ID
        is_admin: Whether the current user is an admin (can access any file)
        organization_id: Active org id, None for personal, or UNSCOPED (default,
            legacy = no gate). Threaded from ``ctx.org_id`` by request handlers so
            cross-tenant files 404/403 (default-deny).

    Returns:
        MediaFile object

    Raises:
        HTTPException: If file not found or no permission
    """
    # Use UUID helper with admin bypass for permission check
    if is_admin:
        from app.utils.uuid_helpers import get_file_by_uuid

        return get_file_by_uuid(db, file_uuid)
    else:
        return get_file_by_uuid_with_permission(
            db, file_uuid, user_id, is_admin=is_admin, organization_id=organization_id
        )


def get_media_file_by_id(
    db: Session, file_id: int, user_id: int, is_admin: bool = False
) -> MediaFile:
    """
    Get a media file by ID and user ID (legacy - internal use only).

    Args:
        db: Database session
        file_id: File ID
        user_id: User ID
        is_admin: Whether the current user is an admin (can access any file)

    Returns:
        MediaFile object

    Raises:
        HTTPException: If file not found
    """
    # Admin users can access any file, regular users only their own
    query = db.query(MediaFile).filter(MediaFile.id == file_id)
    if not is_admin:
        query = query.filter(MediaFile.user_id == user_id)

    db_file = query.first()

    if not db_file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")

    return db_file  # type: ignore[no-any-return]


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


def get_file_collections(db: Session, file_id: int, user_id: int) -> list[dict]:
    """Get collections that contain a media file."""
    try:
        collection_objs = (
            db.query(Collection)
            .join(CollectionMember)
            .filter(
                CollectionMember.media_file_id == file_id,
                Collection.user_id == user_id,
            )
            .all()
        )

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


def set_file_urls(db_file: MediaFile) -> None:
    """
    Set download, preview, and thumbnail URLs for a media file.

    Thumbnails use presigned URLs (industry standard: Google, AWS, Apple).
    This embeds authentication in the URL itself, allowing <img> tags to load
    without HTTP auth headers.

    Args:
        db_file: MediaFile object to update
    """
    from app.core.config import settings
    from app.services.minio_service import get_file_url

    if db_file.storage_path:
        # Skip S3 operations in test environment
        if os.environ.get("SKIP_S3", "False").lower() == "true":
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
                    expires=settings.THUMBNAIL_URL_EXPIRE_SECONDS,
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


def _get_transcript_segments(
    db: Session, file_id: int, segment_limit: int | None, segment_offset: int
) -> tuple[list[TranscriptSegment], int]:
    """
    Get transcript segments with pagination and compute total count.

    Args:
        db: Database session
        file_id: File ID
        segment_limit: Maximum number of segments to return
        segment_offset: Offset for pagination

    Returns:
        Tuple of (list of transcript segments, total count)
    """
    from sqlalchemy import func
    from sqlalchemy.orm import joinedload

    total_segments = (
        db.query(func.count(TranscriptSegment.id))
        .filter(TranscriptSegment.media_file_id == file_id)
        .scalar()
    )

    # Eager-load each segment's speaker AND that speaker's linked profile so the
    # downstream add_computed_status (which reads speaker.profile) never triggers a
    # lazy SELECT per distinct speaker.
    segment_query = (
        db.query(TranscriptSegment)
        .options(joinedload(TranscriptSegment.speaker).selectinload(Speaker.profile))
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(
            TranscriptSegment.start_time,
            TranscriptSegment.end_time,
            TranscriptSegment.id,
        )
    )

    if segment_offset > 0:
        segment_query = segment_query.offset(segment_offset)
    if segment_limit is not None:
        segment_query = segment_query.limit(segment_limit)

    return segment_query.all(), total_segments


def _count_speaker_segments(db: Session, file_id: int) -> int:
    """
    Count the number of speaker segments (adjacent same-speaker segments merged).

    This uses a SQL window function to detect speaker changes, counting
    the number of groups where consecutive segments share the same speaker.
    """
    from sqlalchemy import text

    result = db.execute(
        text("""
            SELECT COUNT(*) FROM (
                SELECT speaker_id,
                       LAG(speaker_id) OVER (ORDER BY start_time) AS prev_speaker_id
                FROM transcript_segment
                WHERE media_file_id = :file_id
            ) sub
            WHERE speaker_id IS DISTINCT FROM prev_speaker_id
               OR prev_speaker_id IS NULL
        """),
        {"file_id": file_id},
    ).scalar()
    return result or 0


def _add_error_info_to_response(response: MediaFileDetail, db_file: MediaFile) -> None:
    """
    Add error categorization information to response for failed files.

    Args:
        response: MediaFileDetail response to update
        db_file: MediaFile object
    """
    if db_file.status != FileStatus.ERROR or not hasattr(db_file, "last_error_message"):
        return

    from app.services.error_categorization_service import ErrorCategorizationService

    error_message = str(db_file.last_error_message) if db_file.last_error_message else None
    error_info = ErrorCategorizationService.get_error_info(error_message)
    response.error_category = error_info["category"]
    response.error_suggestions = error_info["suggestions"]
    response.is_retryable = error_info["is_retryable"]


def _resolve_segment_speaker_name(speaker: Speaker | None) -> str:
    """Resolve a never-null display name for a segment's speaker.

    Mirrors the frontend's fallback chain so the field can be rendered as-is:
    ``display_name`` → ``name`` (original speaker label) → ``"Unknown speaker"``.

    Args:
        speaker: The segment's linked Speaker, or None.

    Returns:
        A non-empty display name string.
    """
    if speaker is None:
        return "Unknown speaker"
    return str(speaker.display_name or speaker.name or "Unknown speaker")


def _build_grouped_segments(formatted_segments: list[Any], index_offset: int = 0) -> list[Any]:
    """Build the authoritative display grouping for a page of transcript segments.

    Consecutive segments sharing a non-null ``overlap_group_id`` are collapsed into a
    single overlap group **only when the run has more than one member**; every other
    segment becomes its own single-member group. Group ``start_time``/``end_time`` are
    the min/max across the group's segments.

    Groups reference their segments by UUID (``segment_uuids``) rather than embedding
    copies — see ``GroupedTranscriptSegment`` for why (issue #352).

    ``overlap_group_id`` is set on **every** group that has one, including single-member
    groups. A group whose overlap run straddles a pagination boundary arrives as two
    groups sharing that id, and the client stitches them back together by it; without
    the id on the single-member branch the tail of a split run would be unstitchable.

    Args:
        formatted_segments: Formatted ``TranscriptSegment`` schema objects, in order.
        index_offset: Absolute index of ``formatted_segments[0]`` in the full
            transcript. Pass the request's ``segment_offset`` so
            ``start_segment_index`` stays global across pagination — the SPA's
            reading-progress bar divides it by the total segment count, so page-local
            indices make progress jump backwards.

    Returns:
        List of ``GroupedTranscriptSegment`` schema objects.
    """
    from app.schemas.media import GroupedTranscriptSegment

    groups: list[Any] = []
    i = 0
    n = len(formatted_segments)

    while i < n:
        segment = formatted_segments[i]

        if segment.overlap_group_id:
            overlap_group_id = segment.overlap_group_id
            overlap_segments = [segment]
            j = i + 1
            while j < n and formatted_segments[j].overlap_group_id == overlap_group_id:
                overlap_segments.append(formatted_segments[j])
                j += 1

            if len(overlap_segments) > 1:
                groups.append(
                    GroupedTranscriptSegment(
                        is_overlap_group=True,
                        overlap_group_id=overlap_group_id,
                        start_time=min(s.start_time for s in overlap_segments),
                        end_time=max(s.end_time for s in overlap_segments),
                        start_segment_index=index_offset + i,
                        segment_uuids=[s.uuid for s in overlap_segments],
                    )
                )
                i = j
                continue

            # Single segment carrying an overlap flag: renders as a regular group, but
            # keep the id so a run split across a page boundary can be stitched.
            groups.append(
                GroupedTranscriptSegment(
                    is_overlap_group=False,
                    overlap_group_id=overlap_group_id,
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    start_segment_index=index_offset + i,
                    segment_uuids=[segment.uuid],
                )
            )
            i += 1
        else:
            groups.append(
                GroupedTranscriptSegment(
                    is_overlap_group=False,
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    start_segment_index=index_offset + i,
                    segment_uuids=[segment.uuid],
                )
            )
            i += 1

    return groups


def _format_transcript_segments(
    transcript_segments: list[TranscriptSegment],
    speakers: list[Speaker],
    redaction_cfg: Any = None,
    reveal_categories: set | None = None,
) -> list[Any]:
    """
    Format transcript segments with speaker labels and timestamps.

    Args:
        transcript_segments: List of transcript segments (DB models)
        speakers: List of speakers for name mapping
        redaction_cfg: Optional ``EffectiveRedactionConfig`` for read-time masking
        reveal_categories: Categories an authorized owner chose to reveal

    Returns:
        List of formatted transcript segment schemas
    """
    speaker_mapping: dict[str, str] = {
        str(speaker.name): FormattingService.format_speaker_name(speaker) for speaker in speakers
    }

    return [
        FormattingService.format_transcript_segment(
            segment, speaker_mapping, redaction_cfg, reveal_categories
        )
        for segment in transcript_segments
    ]


def _resolve_redaction_for_request(
    db: Session,
    db_file: MediaFile,
    current_user: User,
    *,
    is_admin: bool,
    redact: bool,
) -> tuple[Any, set]:
    """Resolve (effective_cfg, reveal_categories) for a transcript read.

    The owner (and admins, audited) may set ``redact=false`` to reveal NON-forced
    categories; admin-forced categories stay masked. Non-owners never reveal.

    Raises:
        HTTPException: 503 when the redaction policy cannot be resolved.
    """
    try:
        from app.services.redaction.config import resolve_effective_config

        cfg = resolve_effective_config(db, current_user.id)
    except Exception as e:
        # FAIL CLOSED. Returning (None, set()) told every downstream reader that
        # redaction was off: `_apply_redaction` short-circuits on a None config
        # and returned every segment raw, `_redaction_pending` stopped withholding
        # transcripts mid-detection, and no `transcript.view_unredacted` audit
        # event fired because `reveal` was empty — an unredacted read with no
        # compliance trail, including admin-forced categories. Refuse the read.
        logger.exception("Failed to resolve redaction config; refusing the transcript read")
        raise HTTPException(
            status_code=503,
            detail="Redaction policy is temporarily unavailable; transcript withheld.",
        ) from e

    can_reveal = bool(db_file.user_id == current_user.id) or bool(is_admin)
    reveal = cfg.reveal_categories(requested=(redact is False), is_owner=can_reveal)

    if reveal:
        # Audit any unredacted view (compliance trail).
        try:
            from app.auth.audit import AuditOutcome
            from app.auth.audit import audit_logger

            audit_logger.log(
                event_type="transcript.view_unredacted",  # type: ignore[arg-type]
                outcome=AuditOutcome.SUCCESS,
                user_id=current_user.id,
                username=str(current_user.email),
                # Org attribution (issue #262a): the reveal concerns this file's
                # tenant, so its org admins see it in the org audit read.
                organization_id=int(db_file.organization_id) if db_file.organization_id else None,
                details={
                    "file_id": db_file.id,
                    "file_uuid": str(db_file.uuid),
                    "revealed_categories": sorted(reveal),
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Audit log for unredacted view failed: {e}")

    return cfg, reveal


def _lazy_dispatch_redaction(db: Session, db_file: MediaFile) -> None:
    """Kick off redaction detection for a completed file that never had it (legacy).

    Sets status to 'pending' immediately so concurrent reads don't re-dispatch.
    """
    try:
        db_file.redaction_status = C.REDACTION_STATUS_PENDING  # type: ignore[assignment]
        db.commit()
        from app.tasks.redaction_task import redaction_detect_task

        redaction_detect_task.delay(file_id=db_file.id, user_id=int(db_file.user_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Lazy redaction dispatch failed for file {db_file.id}: {e}")


def _redaction_pending(db: Session, cfg: Any, db_file: MediaFile) -> bool:
    """Whether transcript display should be withheld until redaction completes.

    Only applies when redaction is enabled for the viewer. 'done'/'failed' never block
    (failed = redaction couldn't run; don't trap the user). A file that never had
    detection (legacy, status None) is dispatched lazily and treated as pending.
    """
    if cfg is None or not getattr(cfg, "enabled", False):
        return False
    status = getattr(db_file, "redaction_status", None)
    if status in (C.REDACTION_STATUS_DONE, C.REDACTION_STATUS_FAILED):
        return False
    if status is None:
        _lazy_dispatch_redaction(db, db_file)
        return True
    return True  # pending | processing


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
    response.speakers = speakers  # type: ignore[assignment]

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


def get_media_file_detail(
    db: Session,
    file_uuid: str,
    current_user: User,
    segment_limit: int | None = None,
    segment_offset: int = 0,
    redact: bool = True,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> MediaFileDetail:
    """
    Get detailed media file information including tags, analytics, and formatted fields.

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user
        segment_limit: Maximum number of transcript segments to return (None = all)
        segment_offset: Offset for transcript segment pagination (default 0)

    Returns:
        MediaFileDetail object with all computed and formatted data
    """
    try:
        is_admin = current_user.is_admin
        db_file = get_media_file_by_uuid(
            db, file_uuid, current_user.id, is_admin=is_admin, organization_id=organization_id
        )
        # Expire heavy JSONB columns so they're not loaded unless explicitly accessed
        # (waveform_data fetched via /waveform, summary via /summary, metadata_raw rarely needed)
        db.expire(db_file, ["waveform_data", "metadata_raw"])
        file_id = db_file.id

        # Get related data
        tags = get_file_tags(db, file_id, current_user.id)
        collections = get_file_collections(db, file_id, current_user.id)

        # Get speakers and add computed status. Eager-load the linked profile so
        # add_computed_status (which reads speaker.profile) doesn't fire one lazy
        # SELECT per speaker.
        speakers = (
            db.query(Speaker)
            .options(selectinload(Speaker.profile))
            .filter(Speaker.media_file_id == file_id)
            .all()
        )
        for speaker in speakers:
            SpeakerStatusService.add_computed_status(speaker)

        # Get analytics (compute on-demand if needed)
        analytics = _get_or_compute_analytics(db, file_id, db_file.status)

        # Get transcript segments with pagination
        transcript_segments, total_segments = _get_transcript_segments(
            db, file_id, segment_limit, segment_offset
        )

        # Count merged speaker segments (adjacent same-speaker groups)
        total_speaker_segments = _count_speaker_segments(db, file_id)

        # Add computed status to segment speakers (skip already-processed)
        processed_speaker_ids = {s.id for s in speakers}
        for segment in transcript_segments:
            if segment.speaker and int(segment.speaker.id) not in processed_speaker_ids:
                SpeakerStatusService.add_computed_status(segment.speaker)
                processed_speaker_ids.add(int(segment.speaker.id))

        # Compute caller's effective permission for frontend
        if db_file.user_id == current_user.id:
            my_permission = None  # Actual owner (frontend convention: null = owner)
        elif is_admin:
            my_permission = "owner"  # Admin viewing someone else's file
        else:
            from app.services.permission_service import PermissionService

            my_permission = PermissionService.get_file_permission(
                db, file_id, current_user.id, organization_id=organization_id
            )

        # Set URLs
        set_file_urls(db_file)

        # Resolve read-time redaction config for the caller.
        redaction_cfg, reveal_categories = _resolve_redaction_for_request(
            db, db_file, current_user, is_admin=is_admin, redact=redact
        )

        # If redaction is enabled but detection hasn't finished, withhold the transcript
        # until it's ready (no un-redacted display window).
        pending = _redaction_pending(db, redaction_cfg, db_file)
        if pending:
            transcript_segments = []
            total_segments = 0
            total_speaker_segments = 0

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
        response.redaction_status = (
            str(db_file.redaction_status) if db_file.redaction_status else None
        )

        # Set caller's permission on the response
        response.my_permission = my_permission

        db.commit()
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error in get_media_file_detail: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving media file: {str(e)}",
        ) from e


def update_media_file(
    db: Session,
    file_uuid: str,
    media_file_update: MediaFileUpdate,
    current_user: User,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> MediaFile:
    """
    Update a media file's metadata.

    Args:
        db: Database session
        file_uuid: File UUID
        media_file_update: Update data
        current_user: Current user
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).

    Returns:
        Updated MediaFile object
    """
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=is_admin, organization_id=organization_id
    )
    file_id = db_file.id  # Get internal ID for OpenSearch update

    # Track if title was updated for OpenSearch reindexing
    update_data = media_file_update.model_dump(exclude_unset=True)
    title_updated = "title" in update_data and update_data["title"] != db_file.title

    # Update fields
    for field, value in update_data.items():
        setattr(db_file, field, value)

    db.commit()
    db.refresh(db_file)

    # Update OpenSearch index if title was changed
    if title_updated:
        try:
            new_title = str(db_file.title or db_file.filename)
            update_transcript_title(str(db_file.uuid), new_title)  # Use UUID not integer ID
        except Exception as e:
            logger.warning(f"Failed to update OpenSearch title for file {file_id}: {e}")

    # Invalidate caches so gallery reflects the update
    try:
        from app.services.redis_cache_service import redis_cache

        redis_cache.invalidate_user_files(current_user.id)
    except Exception as e:
        logger.debug(f"Cache invalidation failed (non-critical): {e}")

    return db_file


def delete_media_file(
    db: Session,
    file_uuid: str,
    current_user: User,
    force: bool = False,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> None:
    """
    Delete a media file and all associated data with safety checks.

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user
        force: Force deletion even if processing is active (admin only)
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).
    """
    from app.utils.task_utils import cancel_active_task
    from app.utils.task_utils import is_file_safe_to_delete

    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=is_admin, organization_id=organization_id
    )
    file_id = db_file.id  # Get internal ID for task operations

    # Check if file is safe to delete
    is_safe, reason = is_file_safe_to_delete(db, file_id)

    if not is_safe and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "FILE_NOT_SAFE_TO_DELETE",
                "message": f"Cannot delete file: {reason}",
                "file_id": str(db_file.uuid),  # Use UUID for frontend
                "active_task_id": db_file.active_task_id,
                "status": db_file.status,
                "options": {
                    "cancel_and_delete": is_admin,
                    "wait_for_completion": True,
                    "force_delete": is_admin and db_file.force_delete_eligible,
                },
            },
        )

    # If force deletion and file has active task, cancel it first
    if force and db_file.active_task_id:
        logger.info(
            f"Force deleting file {file_id}, cancelling active task {db_file.active_task_id}"
        )
        cancel_active_task(db, file_id)
        # Refresh the file object
        db.refresh(db_file)

    # Delegate the actual destroy to the single canonical implementation so the
    # interactive, bulk, retention, and orphan-cleanup paths all behave identically.
    from app.services.file_cleanup_service import purge_media_file

    result = purge_media_file(db, db_file)
    if not result["deleted"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete file from database: {result.get('error')}",
        )


def update_single_transcript_segment(
    db: Session,
    file_uuid: str,
    segment_uuid: str,
    segment_update: TranscriptSegmentUpdate,
    current_user: User,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> TranscriptSegmentSchema:
    """
    Update a single transcript segment for a media file.

    Args:
        db: Database session
        file_uuid: File UUID
        segment_uuid: Segment UUID
        segment_update: Segment update data
        current_user: Current user

    Returns:
        Updated TranscriptSegmentSchema object with all formatted fields
    """
    # Verify user owns the file or is admin
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=is_admin, organization_id=organization_id
    )
    file_id = db_file.id  # Get internal ID for segment query

    # Find the specific segment by UUID with speaker eagerly loaded
    segment = (
        db.query(TranscriptSegment)
        .options(joinedload(TranscriptSegment.speaker))
        .filter(
            TranscriptSegment.uuid == segment_uuid,
            TranscriptSegment.media_file_id == file_id,
        )
        .first()
    )

    if not segment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Transcript segment not found"
        )

    # Update fields
    update_fields = segment_update.model_dump(exclude_unset=True)
    text_changed = "text" in update_fields and update_fields["text"] != segment.text
    for field, value in update_fields.items():
        setattr(segment, field, value)

    # If the text changed, re-run redaction detection for THIS segment only so the
    # edited text never bypasses masking (cheap inline detectors; ML best-effort).
    if text_changed:
        try:
            from app.services.redaction.config import detection_config_for_all
            from app.services.redaction.service import RedactionService

            det_cfg = detection_config_for_all()
            det_cfg["language"] = db_file.language
            span_dicts, toxicity = RedactionService.detect_segment_spans(
                str(segment.text),
                segment.words,  # type: ignore[arg-type]
                det_cfg,
            )
            segment.redactions = span_dicts or None  # type: ignore[assignment]
            segment.toxicity = toxicity  # type: ignore[assignment]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Re-detection after segment edit failed: {e}")

    db.commit()
    db.refresh(segment)

    # Manually construct Pydantic response with all required fields
    return TranscriptSegmentSchema(
        uuid=segment.uuid,  # type: ignore[arg-type]
        media_file_id=db_file.uuid,  # type: ignore[arg-type]
        start_time=float(segment.start_time),
        end_time=float(segment.end_time),
        text=str(segment.text),
        speaker_id=segment.speaker.uuid if segment.speaker else None,
        speaker=segment.speaker,  # type: ignore[arg-type]  # ORM Speaker coerced via from_attributes
        formatted_timestamp=format_timestamp(float(segment.start_time)),
        display_timestamp=format_timestamp(float(segment.start_time)),
        speaker_label=(segment.speaker.name if segment.speaker else None),
        resolved_speaker_name=_resolve_segment_speaker_name(segment.speaker),
    )


def get_stream_url_info(
    db: Session,
    file_uuid: str,
    current_user: User,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> dict[str, Any]:
    """
    Get streaming URL information for a media file.

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user
        organization_id: Active org id, None for personal, or UNSCOPED (legacy).

    Returns:
        Dictionary with URL and content type information
    """
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, is_admin=is_admin, organization_id=organization_id
    )

    # Skip S3 operations in test environment
    if os.environ.get("SKIP_S3", "False").lower() == "true":
        logger.info("Returning mock URL in test environment")
        return {
            "url": f"/api/files/{db_file.uuid}/content",
            "content_type": db_file.content_type,
        }

    # Return the URL to our video endpoint
    return {
        "url": f"/api/files/{db_file.uuid}/video",  # Video endpoint
        "content_type": db_file.content_type,
        "requires_auth": False,  # No auth required for video endpoint
    }
