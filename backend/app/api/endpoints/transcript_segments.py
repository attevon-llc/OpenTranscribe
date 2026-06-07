"""API endpoints for transcript segment operations."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.user import User
from app.schemas.media import TranscriptSegment as TranscriptSegmentSchema
from app.schemas.transcript import SegmentSpeakerUpdate
from app.utils.time_format import format_timestamp_simple as format_timestamp
from app.utils.uuid_helpers import get_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


def _cleanup_orphaned_speaker(db: Session, speaker_id: int) -> bool:
    """
    Delete a speaker if it has no remaining segments.

    This is called after a segment is reassigned to clean up speakers
    that are no longer used. This keeps the speaker list clean and
    prevents accumulation of unused speakers.

    Args:
        db: Database session
        speaker_id: ID of the speaker to check

    Returns:
        True if the speaker was deleted, False otherwise
    """
    if not speaker_id:
        return False

    # Check if speaker has any remaining segments
    segment_count = (
        db.query(TranscriptSegment).filter(TranscriptSegment.speaker_id == speaker_id).count()
    )

    if segment_count == 0:
        # Speaker has no segments - delete it
        speaker = db.query(Speaker).filter(Speaker.id == speaker_id).first()
        if speaker:
            speaker_uuid = str(speaker.uuid)
            speaker_name = speaker.name
            db.delete(speaker)
            db.commit()

            # Also remove embedding from OpenSearch
            try:
                from app.services.opensearch_service import remove_speaker_embedding

                remove_speaker_embedding(speaker_uuid)
            except Exception as e:
                logger.warning(
                    f"Failed to remove OpenSearch embedding for orphaned speaker "
                    f"{speaker_uuid}: {e}"
                )

            logger.info(
                f"Deleted orphaned speaker {speaker_uuid} ({speaker_name}) - no remaining segments"
            )
            return True

    return False


def _get_new_speaker_id(
    db: Session, update: SegmentSpeakerUpdate, segment: TranscriptSegment, current_user: User
) -> int | None:
    """
    Resolve and validate the new speaker ID from the update request.

    Args:
        db: Database session
        update: SegmentSpeakerUpdate containing the new speaker UUID (or null)
        segment: The segment being updated
        current_user: Currently authenticated user

    Returns:
        The speaker ID if valid, None if unassigning

    Raises:
        HTTPException 400: Invalid speaker UUID or speaker doesn't belong to same file
        HTTPException 403: User doesn't own the speaker
    """
    if update.speaker_uuid is None:
        return None

    # Look up the speaker by UUID
    speaker = get_by_uuid(db, Speaker, update.speaker_uuid, error_message="Speaker not found")

    # Verify the speaker belongs to the same media file
    if speaker.media_file_id != segment.media_file_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Speaker does not belong to the same media file as this segment",
        )

    # Verify the user owns this speaker (admins and shared editors bypass)
    if not current_user.is_admin and speaker.user_id != current_user.id:
        from app.services.permission_service import PermissionService

        perm = PermissionService.get_file_permission(
            db, int(speaker.media_file_id), current_user.id
        )
        if not perm or perm == "viewer":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to use this speaker",
            )

    return speaker.id


def _handle_speaker_change(
    db: Session,
    original_speaker_id: int | None,
    new_speaker_id: int | None,
    media_file_id: int,
    segment_uuid: str | None = None,
    media_file_uuid: str | None = None,
    user_id: int | None = None,
) -> None:
    """
    Handle side effects of speaker assignment change.

    Cleans up orphaned speakers, refreshes analytics, and dispatches
    a background task to update speaker embeddings.

    Args:
        db: Database session
        original_speaker_id: Previous speaker ID (or None)
        new_speaker_id: New speaker ID (or None)
        media_file_id: ID of the media file for analytics refresh
        segment_uuid: UUID of the reassigned segment (for embedding update)
        media_file_uuid: UUID of the media file (for embedding update)
        user_id: ID of the current user (for embedding update)
    """
    if original_speaker_id == new_speaker_id:
        return

    # Capture source speaker UUID before orphan cleanup might delete it
    source_speaker_uuid: str | None = None
    if original_speaker_id:
        original_speaker = db.query(Speaker).filter(Speaker.id == original_speaker_id).first()
        if original_speaker:
            source_speaker_uuid = str(original_speaker.uuid)

    # Capture target speaker UUID for embedding task
    target_speaker_uuid: str | None = None
    if new_speaker_id:
        target_speaker = db.query(Speaker).filter(Speaker.id == new_speaker_id).first()
        if target_speaker:
            target_speaker_uuid = str(target_speaker.uuid)

    # Check if the old speaker is now orphaned (no remaining segments)
    # and delete it if so - this keeps the speaker list clean
    orphan_deleted = False
    if original_speaker_id:
        orphan_deleted = _cleanup_orphaned_speaker(db, original_speaker_id)

    try:
        from app.services.analytics_service import AnalyticsService

        AnalyticsService.refresh_analytics(db, media_file_id)
        logger.info(f"Refreshed analytics for file {media_file_id} after segment speaker change")
    except Exception as e:
        # Don't fail the operation if analytics refresh fails
        logger.warning(f"Failed to refresh analytics after segment speaker change: {e}")

    # Dispatch background task to update speaker embeddings
    if segment_uuid and media_file_uuid and user_id and target_speaker_uuid:
        try:
            from app.tasks.speaker_tasks import update_speaker_embedding_on_reassignment

            update_speaker_embedding_on_reassignment.delay(
                segment_uuid=segment_uuid,
                media_file_uuid=media_file_uuid,
                target_speaker_uuid=target_speaker_uuid,
                source_speaker_uuid=source_speaker_uuid if not orphan_deleted else None,
                user_id=user_id,
            )
            logger.info(
                f"Dispatched embedding update task for segment {segment_uuid} "
                f"-> speaker {target_speaker_uuid}"
            )
        except Exception as e:
            # Don't fail the operation if task dispatch fails
            logger.warning(f"Failed to dispatch embedding update task: {e}")


@router.put("/segments/{segment_uuid}/speaker", response_model=TranscriptSegmentSchema)
def update_segment_speaker(
    segment_uuid: str,
    update: SegmentSpeakerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Update the speaker assignment for a transcript segment.

    This endpoint allows users to reassign a transcript segment to a different speaker
    or unassign it completely. The segment must belong to a media file owned by the user.

    Args:
        segment_uuid: UUID of the transcript segment to update
        update: SegmentSpeakerUpdate containing the new speaker UUID (or null)
        db: Database session
        current_user: Currently authenticated user

    Returns:
        Updated TranscriptSegment with speaker information

    Raises:
        HTTPException 404: Segment not found
        HTTPException 403: User does not own the media file
        HTTPException 400: Invalid speaker UUID or speaker doesn't belong to same file
    """
    # Get the segment by UUID
    segment = get_by_uuid(
        db, TranscriptSegment, segment_uuid, error_message="Transcript segment not found"
    )

    # Get the media file to verify ownership
    media_file = db.query(MediaFile).filter(MediaFile.id == segment.media_file_id).first()
    if not media_file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found")

    # Verify the user owns this file
    if media_file.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to modify this transcript segment",
        )

    # Track original speaker_id for change detection
    original_speaker_id: int | None = int(segment.speaker_id) if segment.speaker_id else None

    # Resolve and validate the new speaker
    new_speaker_id = _get_new_speaker_id(db, update, segment, current_user)

    # Update the segment's speaker
    segment.speaker_id = new_speaker_id  # type: ignore[assignment]
    db.commit()
    db.refresh(segment)

    # Handle side effects of speaker change (cleanup orphans, refresh analytics, embeddings)
    _handle_speaker_change(
        db,
        original_speaker_id,
        new_speaker_id,
        media_file.id,
        segment_uuid=segment_uuid,
        media_file_uuid=str(media_file.uuid),
        user_id=current_user.id,
    )

    # Format the response with speaker details
    response_data = TranscriptSegmentSchema(
        uuid=segment.uuid,  # type: ignore[arg-type]
        media_file_id=media_file.uuid,  # type: ignore[arg-type]
        start_time=float(segment.start_time),
        end_time=float(segment.end_time),
        text=str(segment.text),
        speaker_id=segment.speaker.uuid if segment.speaker else None,
        speaker=segment.speaker,  # type: ignore[arg-type]  # ORM Speaker coerced via from_attributes
        formatted_timestamp=format_timestamp(float(segment.start_time)),
        display_timestamp=format_timestamp(float(segment.start_time)),
        speaker_label=(segment.speaker.name if segment.speaker else None),  # Original speaker ID
        resolved_speaker_name=(
            segment.speaker.display_name or segment.speaker.name if segment.speaker else None
        ),
    )

    logger.info(
        f"Updated segment {segment_uuid} speaker assignment to "
        f"{'speaker ' + update.speaker_uuid if update.speaker_uuid else 'no speaker'}"
    )

    return response_data
