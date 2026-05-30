import contextlib
import logging
import os
from datetime import datetime
from typing import Any
from typing import cast

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

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
from app.schemas.media import TranscriptSegment as TranscriptSegmentSchema
from app.schemas.media import TranscriptSegmentUpdate
from app.services.formatting_service import FormattingService
from app.services.minio_service import delete_file
from app.services.opensearch_service import update_transcript_title
from app.services.speaker_status_service import SpeakerStatusService
from app.utils.time_format import format_timestamp_simple as format_timestamp
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

logger = logging.getLogger(__name__)


def get_media_file_by_uuid(
    db: Session, file_uuid: str, user_id: int, is_admin: bool = False
) -> MediaFile:
    """
    Get a media file by UUID and user ID.

    Args:
        db: Database session
        file_uuid: File UUID
        user_id: User ID
        is_admin: Whether the current user is an admin (can access any file)

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
        return get_file_by_uuid_with_permission(db, file_uuid, user_id, is_admin=is_admin)


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


def get_file_tags(db: Session, file_id: int) -> list[str]:
    """Get tags for a media file."""
    try:
        tags = db.query(Tag.name).join(FileTag).filter(FileTag.media_file_id == file_id).all()
        return [tag[0] for tag in tags]
    except Exception as tag_error:
        logger.error(f"Error getting tags: {tag_error}")
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
        logger.error(f"Error getting collections: {collection_error}")
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
            db_file.download_url = f"/api/files/{db_file.uuid}/prepare-download"
            if db_file.thumbnail_path:
                db_file.thumbnail_url = f"/api/files/{db_file.uuid}/thumbnail"
            return

        # download_url is a presence/availability gate for the frontend download dropdown,
        # which performs the actual download via the async prepare-download + presigned-URL
        # flow (see TranscriptDisplay.downloadMedia). It is not a byte-proxy endpoint.
        db_file.download_url = f"/api/files/{db_file.uuid}/prepare-download"

        # Set thumbnail URL as presigned URL (industry standard)
        # This allows <img> tags to load without auth headers
        if db_file.thumbnail_path:
            try:
                db_file.thumbnail_url = get_file_url(
                    str(db_file.thumbnail_path),
                    expires=settings.THUMBNAIL_URL_EXPIRE_SECONDS,
                )
            except Exception as e:
                logger.warning(f"Failed to generate presigned thumbnail URL: {e}")
                # Fallback to API endpoint (requires auth)
                db_file.thumbnail_url = f"/api/files/{db_file.uuid}/thumbnail"


def _get_or_compute_analytics(db: Session, file_id: int, file_status: str) -> Analytics | None:
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

    if analytics or file_status != "completed":
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

    segment_query = (
        db.query(TranscriptSegment)
        .options(joinedload(TranscriptSegment.speaker))
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(TranscriptSegment.start_time)
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


def _format_transcript_segments(
    transcript_segments: list[TranscriptSegment], speakers: list[Speaker]
) -> list[Any]:
    """
    Format transcript segments with speaker labels and timestamps.

    Args:
        transcript_segments: List of transcript segments (DB models)
        speakers: List of speakers for name mapping

    Returns:
        List of formatted transcript segment schemas
    """
    speaker_mapping: dict[str, str] = {
        str(speaker.name): FormattingService.format_speaker_name(speaker) for speaker in speakers
    }

    return [
        FormattingService.format_transcript_segment(segment, speaker_mapping)
        for segment in transcript_segments
    ]


def _build_media_file_response(
    db_file: MediaFile,
    tags: list[str],
    collections: list[dict[Any, Any]],
    speakers: list[Speaker],
    analytics: Analytics | None,
    transcript_segments: list[TranscriptSegment],
    total_segments: int,
    total_speaker_segments: int,
    segment_limit: int | None,
    segment_offset: int,
) -> MediaFileDetail:
    """
    Build the MediaFileDetail response with all formatted fields.

    Args:
        db_file: MediaFile object
        tags: List of tag names
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
    upload_time_val: datetime | None = (
        cast(datetime, db_file.upload_time) if db_file.upload_time else None
    )
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

    # Format and add transcript segments
    formatted_segments = _format_transcript_segments(transcript_segments, speakers)
    response.transcript_segments = formatted_segments  # type: ignore[assignment]

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
        db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)
        # Expire heavy JSONB columns so they're not loaded unless explicitly accessed
        # (waveform_data fetched via /waveform, summary via /summary, metadata_raw rarely needed)
        db.expire(db_file, ["waveform_data", "metadata_raw"])
        file_id = int(db_file.id)

        # Get related data
        tags = get_file_tags(db, file_id)
        collections = get_file_collections(db, file_id, int(current_user.id))

        # Get speakers and add computed status
        speakers = db.query(Speaker).filter(Speaker.media_file_id == file_id).all()
        for speaker in speakers:
            SpeakerStatusService.add_computed_status(speaker)

        # Get analytics (compute on-demand if needed)
        analytics = _get_or_compute_analytics(db, file_id, str(db_file.status))

        # Get transcript segments with pagination
        transcript_segments, total_segments = _get_transcript_segments(
            db, file_id, segment_limit, segment_offset
        )

        # Count merged speaker segments (adjacent same-speaker groups)
        total_speaker_segments = _count_speaker_segments(db, file_id)

        # Add computed status to segment speakers (skip already-processed)
        processed_speaker_ids = {int(s.id) for s in speakers}
        for segment in transcript_segments:
            if segment.speaker and int(segment.speaker.id) not in processed_speaker_ids:
                SpeakerStatusService.add_computed_status(segment.speaker)
                processed_speaker_ids.add(int(segment.speaker.id))

        # Compute caller's effective permission for frontend
        if db_file.user_id == int(current_user.id):
            my_permission = None  # Actual owner (frontend convention: null = owner)
        elif is_admin:
            my_permission = "owner"  # Admin viewing someone else's file
        else:
            from app.services.permission_service import PermissionService

            my_permission = PermissionService.get_file_permission(db, file_id, int(current_user.id))

        # Set URLs
        set_file_urls(db_file)

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
        )

        # Set caller's permission on the response
        response.my_permission = my_permission

        db.commit()
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_media_file_detail: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error retrieving media file: {str(e)}",
        ) from e


def update_media_file(
    db: Session, file_uuid: str, media_file_update: MediaFileUpdate, current_user: User
) -> MediaFile:
    """
    Update a media file's metadata.

    Args:
        db: Database session
        file_uuid: File UUID
        media_file_update: Update data
        current_user: Current user

    Returns:
        Updated MediaFile object
    """
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)
    file_id = int(db_file.id)  # Get internal ID for OpenSearch update

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

        redis_cache.invalidate_user_files(int(current_user.id))
    except Exception as e:
        logger.debug(f"Cache invalidation failed (non-critical): {e}")

    return db_file


def _delete_speaker_embeddings_from_index(
    client: Any, index_name: str, speaker_uuids: list[str], file_id: int
) -> None:
    """Delete speaker embedding documents from a single OpenSearch index."""
    if not client.indices.exists(index=index_name):
        return
    deleted = 0
    for speaker_uuid in speaker_uuids:
        with contextlib.suppress(Exception):
            client.delete(index=index_name, id=speaker_uuid)
            deleted += 1
    if deleted:
        logger.info(
            f"Deleted {deleted}/{len(speaker_uuids)} speaker embeddings "
            f"from {index_name} for file {file_id}"
        )


def _cleanup_opensearch_data(db: Session, file_id: int, file_uuid: str) -> None:
    """Clean up OpenSearch data for a file being deleted.

    Cleans: speakers (v3), speakers_v4, transcripts, transcript_chunks,
    transcript_summaries. Each step is non-fatal — errors are logged and
    do not prevent subsequent cleanup steps.
    """
    try:
        from app.services.opensearch_service import opensearch_client
        from app.services.opensearch_service import settings

        speaker_uuid_rows = db.query(Speaker.uuid).filter(Speaker.media_file_id == file_id).all()
        speaker_uuids = [str(r[0]) for r in speaker_uuid_rows]

        if not opensearch_client:
            logger.warning("OpenSearch client not available for cleanup")
            return

        # Delete speaker embeddings from v3 and v4 indices
        if speaker_uuids:
            from app.core.constants import get_speaker_index
            from app.core.constants import get_speaker_index_v4

            for idx in [
                get_speaker_index(),
                get_speaker_index_v4(),
            ]:
                with contextlib.suppress(Exception):
                    _delete_speaker_embeddings_from_index(
                        opensearch_client, idx, speaker_uuids, file_id
                    )

        # Delete transcript document
        with contextlib.suppress(Exception):
            opensearch_client.delete(index=settings.OPENSEARCH_TRANSCRIPT_INDEX, id=str(file_uuid))
            logger.info(f"Deleted transcript for file {file_uuid} from OpenSearch")

        # Delete transcript chunks
        with contextlib.suppress(Exception):
            from app.services.search.indexing_service import TranscriptIndexingService

            chunks_deleted = TranscriptIndexingService().delete_transcript_chunks(str(file_uuid))
            if chunks_deleted:
                logger.info(f"Deleted {chunks_deleted} transcript chunks for file {file_uuid}")

        # Delete transcript summaries
        with contextlib.suppress(Exception):
            summary_index = settings.OPENSEARCH_SUMMARY_INDEX
            if opensearch_client.indices.exists(index=summary_index):
                resp = opensearch_client.delete_by_query(
                    index=summary_index,
                    body={"query": {"term": {"file_id": str(file_id)}}},
                    refresh=True,
                )
                summary_deleted = resp.get("deleted", 0)
                if summary_deleted:
                    logger.info(f"Deleted {summary_deleted} summaries for file {file_id}")

    except Exception as e:
        logger.warning(f"Error cleaning up OpenSearch data for file {file_id}: {e}")


def delete_media_file(db: Session, file_uuid: str, current_user: User, force: bool = False) -> None:
    """
    Delete a media file and all associated data with safety checks.

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user
        force: Force deletion even if processing is active (admin only)
    """
    from app.utils.task_utils import cancel_active_task
    from app.utils.task_utils import is_file_safe_to_delete

    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)
    file_id = int(db_file.id)  # Get internal ID for task operations

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

    # Delete from MinIO (if exists)
    storage_deleted = False
    try:
        delete_file(str(db_file.storage_path))
        storage_deleted = True
        logger.info(f"Successfully deleted file from storage: {db_file.storage_path}")
    except Exception as e:
        logger.warning(f"Error deleting file from storage: {e}")
        # Don't fail the entire operation if storage deletion fails

    # Delete associated data from OpenSearch before deleting from database
    _cleanup_opensearch_data(db, file_id, str(db_file.uuid))

    try:
        # Delete from database (cascade will handle related records)
        owner_id = int(db_file.user_id)
        db.delete(db_file)
        db.commit()
        logger.info(f"Successfully deleted file {file_id} from database")

        # Invalidate caches — file list, tags, speakers, metadata all change
        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_all_for_user(owner_id)
        except Exception as cache_err:
            logger.debug(f"Cache invalidation after delete failed: {cache_err}")

        # Clean up orphaned empty clusters (non-promoted) after CASCADE deletes speakers.
        # CASCADE on speaker_cluster_member removes membership rows but does NOT
        # decrement the denormalized member_count column, so we check actual
        # remaining members via a NOT EXISTS subquery instead.
        try:
            from sqlalchemy import exists

            from app.models.media import SpeakerCluster
            from app.models.media import SpeakerClusterMember

            has_members = (
                exists()
                .where(SpeakerClusterMember.cluster_id == SpeakerCluster.id)
                .correlate(SpeakerCluster)
            )
            empty_clusters = (
                db.query(SpeakerCluster)
                .filter(
                    SpeakerCluster.user_id == owner_id,
                    ~has_members,
                    SpeakerCluster.promoted_to_profile_id.is_(None),
                )
                .all()
            )
            if empty_clusters:
                for cluster in empty_clusters:
                    try:
                        from app.services.opensearch_service import delete_cluster_embedding

                        delete_cluster_embedding(str(cluster.uuid))
                    except Exception as embed_err:
                        logger.debug(f"Could not delete cluster embedding: {embed_err}")
                    db.delete(cluster)
                db.commit()
                logger.info(f"Cleaned up {len(empty_clusters)} empty clusters after file deletion")
        except Exception as e:
            db.rollback()
            logger.warning(f"Failed to clean up empty clusters: {e}")

    except Exception as e:
        logger.error(f"Failed to delete file {file_id} from database: {e}")
        db.rollback()

        # If we deleted from storage but DB deletion failed, that's a problem
        if storage_deleted:
            logger.error(f"File {file_id} deleted from storage but not from database - orphaned!")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete file from database: {str(e)}",
        ) from e


def update_single_transcript_segment(
    db: Session,
    file_uuid: str,
    segment_uuid: str,
    segment_update: TranscriptSegmentUpdate,
    current_user: User,
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
    db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)
    file_id = int(db_file.id)  # Get internal ID for segment query

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
    for field, value in segment_update.model_dump(exclude_unset=True).items():
        setattr(segment, field, value)

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
        speaker=segment.speaker,
        formatted_timestamp=format_timestamp(float(segment.start_time)),
        display_timestamp=format_timestamp(float(segment.start_time)),
        speaker_label=(segment.speaker.name if segment.speaker else None),
        resolved_speaker_name=(
            segment.speaker.display_name or segment.speaker.name if segment.speaker else None
        ),
    )


def get_stream_url_info(db: Session, file_uuid: str, current_user: User) -> dict[str, Any]:
    """
    Get streaming URL information for a media file.

    Args:
        db: Database session
        file_uuid: File UUID
        current_user: Current user

    Returns:
        Dictionary with URL and content type information
    """
    is_admin = current_user.is_admin
    db_file = get_media_file_by_uuid(db, file_uuid, int(current_user.id), is_admin=is_admin)

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
