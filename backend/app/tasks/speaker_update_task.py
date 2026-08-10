"""
Background speaker update task.

Handles heavy operations after a speaker's display_name is updated:
profile embedding updates, OpenSearch synchronization, retroactive
cross-media speaker matching, video cache clearing, and WebSocket
notification.
"""

import logging

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, name="process_speaker_update_background", priority=CPUPriority.USER_TRIGGERED
)
def process_speaker_update_background(
    self,
    speaker_uuid: str,
    user_id: int,
    display_name: str,
    speaker_id: int,
    old_profile_id: int | None,
    new_profile_id: int | None,
    was_auto_labeled: bool,
    display_name_changed: bool,
    media_file_id: int,
    renamed_profile_id: int | None = None,
):
    """
    Background processing for speaker updates.

    This task handles heavy operations after a speaker's display_name is updated:
    - Profile embedding updates
    - OpenSearch synchronization
    - Retroactive cross-media speaker matching
    - Video cache clearing
    - WebSocket notification

    The speaker update endpoint returns immediately after saving to PostgreSQL,
    and this task runs in the background to complete the processing.

    Args:
        speaker_uuid: UUID of the speaker being updated
        user_id: ID of the user who owns the speaker
        display_name: The new display name for the speaker
        speaker_id: Database ID of the speaker
        old_profile_id: Previous profile ID (if any)
        new_profile_id: New profile ID (if any)
        was_auto_labeled: Whether the speaker was previously auto-labeled
        display_name_changed: Whether the display_name was changed
        media_file_id: ID of the media file the speaker belongs to
        renamed_profile_id: Set when the request renamed a SpeakerProfile in place
            (``profile_action="update_profile"``). The endpoint writes the new name to
            Postgres and defers the OpenSearch fan-out — one update per linked speaker
            — to step 3b here (issue #284 A2.6). Defaults to None so tasks published by
            an older API replica still unpack.
    """
    from app.api.endpoints.speakers import _clear_video_cache_for_speaker
    from app.api.endpoints.speakers import _handle_profile_embedding_updates
    from app.api.endpoints.speakers import _handle_speaker_labeling_workflow
    from app.api.endpoints.speakers import _sync_profile_rename_to_opensearch
    from app.api.endpoints.speakers import _update_opensearch_profile_info
    from app.api.endpoints.speakers import _update_opensearch_speaker_name
    from app.utils.uuid_helpers import get_speaker_by_uuid
    from app.utils.websocket_notify import send_ws_event

    with session_scope() as db:
        try:
            logger.info(
                f"Starting background processing for speaker {speaker_uuid} "
                f"(display_name: {display_name})"
            )

            # Get the speaker from the database (fresh state in case it was updated again)
            speaker = get_speaker_by_uuid(db, speaker_uuid)
            if not speaker:
                logger.error(f"Speaker {speaker_uuid} not found in background task")
                return {"status": "error", "message": "Speaker not found"}

            # Use the current display_name from DB in case user updated again before task ran
            display_name = str(speaker.display_name) if speaker.display_name else ""
            new_profile_id = int(speaker.profile_id) if speaker.profile_id else None

            # 1. Handle profile embedding updates
            logger.debug(f"Updating profile embeddings for speaker {speaker_uuid}")
            _handle_profile_embedding_updates(
                db,
                speaker_id,
                old_profile_id,
                new_profile_id,
                was_auto_labeled,
                display_name_changed,
            )

            # 2. Update OpenSearch with speaker name
            if display_name_changed and display_name:
                logger.debug(f"Updating OpenSearch speaker name for {speaker_uuid}")
                _update_opensearch_speaker_name(speaker_uuid, display_name)

            # 3. Update OpenSearch profile info
            logger.debug(f"Updating OpenSearch profile info for speaker {speaker_uuid}")
            _update_opensearch_profile_info(speaker, old_profile_id, display_name_changed, db)

            # 3b. Replay an in-place profile rename onto every linked speaker's doc
            if renamed_profile_id:
                logger.debug(f"Syncing renamed profile {renamed_profile_id} to OpenSearch")
                _sync_profile_rename_to_opensearch(db, renamed_profile_id)

            # 4. Handle speaker labeling workflow (retroactive matching)
            auto_applied_count = 0
            suggested_count = 0
            if display_name_changed and display_name and display_name.strip():
                logger.debug(f"Running retroactive matching for speaker {speaker_uuid}")
                result = _handle_speaker_labeling_workflow(speaker, display_name, db)
                if result:
                    auto_applied_count = result.get("auto_applied_count", 0)
                    suggested_count = result.get("suggested_count", 0)

            # 5. Clear video cache
            logger.debug(f"Clearing video cache for media file {media_file_id}")
            _clear_video_cache_for_speaker(db, media_file_id)

            # 6. Send WebSocket notification that background processing is complete
            logger.debug(f"Sending WebSocket notification for speaker {speaker_uuid}")

            notification_data = {
                "speaker_uuid": speaker_uuid,
                "display_name": display_name,
                "profile_id": str(speaker.profile.uuid) if speaker.profile else None,
                "auto_applied_count": auto_applied_count,
                "suggested_count": suggested_count,
                "processing_status": "complete",
                "media_file_id": str(speaker.media_file.uuid) if speaker.media_file else None,
            }

            send_ws_event(user_id, "speaker_processing_complete", notification_data)

            logger.info(
                f"Background processing complete for speaker {speaker_uuid}. "
                f"Auto-applied: {auto_applied_count}, Suggested: {suggested_count}"
            )

            return {
                "status": "success",
                "speaker_uuid": speaker_uuid,
                "auto_applied_count": auto_applied_count,
                "suggested_count": suggested_count,
            }

        except Exception as e:
            logger.error(
                f"Error in background speaker processing for {speaker_uuid}: {type(e).__name__}: {e}"
            )
            logger.error("Full traceback:", exc_info=True)
            return {"status": "error", "message": str(e)}
