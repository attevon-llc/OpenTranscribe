"""Background speaker merge task.

``POST /speakers/{uuid}/merge/{target_uuid}`` used to do all of its downstream work
inline, so the HTTP response waited on: two OpenSearch voiceprint reads plus a numpy
mean and an index write, a per-affected-file MinIO cache clear, an OpenSearch delete +
update, a consolidated profile-embedding recompute for **each** side of the merge (one
kNN read per profile member), and two analytics recomputations. Issue #284 A2.6 moved
all of it here; the endpoint now returns as soon as Postgres is consistent.

Ordering inside the task matches the old inline ordering exactly — in particular the
source speaker's OpenSearch document is read (for averaging) before
``merge_speaker_embeddings`` deletes it.
"""

import logging

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True, name="process_speaker_merge_background", priority=CPUPriority.USER_TRIGGERED
)
def process_speaker_merge_background(
    self,
    source_speaker_uuid: str,
    target_speaker_uuid: str,
    user_id: int,
    source_speaker_id: int,
    source_profile_id: int | None,
    target_profile_id: int | None,
    media_file_ids: list[int],
):
    """Finish a speaker merge after the Postgres side has committed.

    Args:
        source_speaker_uuid: UUID of the absorbed speaker. Its Postgres row is already
            gone; only the OpenSearch document remains at this point.
        target_speaker_uuid: UUID of the surviving speaker.
        user_id: Owner, for the completion WebSocket event.
        source_speaker_id: Integer id the absorbed speaker had, needed to detach it
            from its old profile's consolidated embedding.
        source_profile_id: Profile the absorbed speaker belonged to, if any.
        target_profile_id: Profile the surviving speaker belongs to, if any.
        media_file_ids: Media files touched by the merge (video cache + analytics).

    Returns:
        A status dict; failures are logged and swallowed so a merge is never rolled
        back by a downstream search/storage hiccup (matching the previous inline
        behaviour, where every step was individually try/except'd).
    """
    from app.api.endpoints.speakers import _clear_speaker_video_cache
    from app.api.endpoints.speakers import _merge_speaker_embeddings
    from app.api.endpoints.speakers import _refresh_analytics_after_merge
    from app.api.endpoints.speakers import _update_opensearch_speaker_merge
    from app.api.endpoints.speakers import _update_profile_embeddings_after_merge
    from app.utils.uuid_helpers import get_speaker_by_uuid
    from app.utils.websocket_notify import send_ws_event

    affected_media_files = set(media_file_ids or [])

    with session_scope() as db:
        try:
            logger.info(
                f"Starting background merge processing {source_speaker_uuid} -> "
                f"{target_speaker_uuid}"
            )

            target_speaker = get_speaker_by_uuid(db, target_speaker_uuid)
            if not target_speaker:
                logger.error(f"Target speaker {target_speaker_uuid} not found in merge task")
                return {"status": "error", "message": "Target speaker not found"}

            # 1. Average the two voiceprints onto the survivor (reads the source doc)
            _merge_speaker_embeddings(source_speaker_uuid, target_speaker)

            # 2. Clear video cache for affected media files
            _clear_speaker_video_cache(db, affected_media_files)

            # 3. Delete the source document and update the survivor in OpenSearch
            _update_opensearch_speaker_merge(source_speaker_uuid, target_speaker_uuid)

            # 4. Recompute both profiles' consolidated embeddings
            _update_profile_embeddings_after_merge(
                db, source_profile_id, target_profile_id, source_speaker_id
            )

            # 5. Recalculate analytics for affected media files
            _refresh_analytics_after_merge(db, affected_media_files)

            # 6. Tell the UI to reload speakers. Reuses the event the speaker-update
            # task already emits; the frontend treats it as a silent refresh and only
            # toasts when auto_applied_count / suggested_count are non-zero.
            send_ws_event(
                user_id,
                "speaker_processing_complete",
                {
                    "speaker_uuid": target_speaker_uuid,
                    "display_name": str(target_speaker.display_name)
                    if target_speaker.display_name
                    else "",
                    "processing_status": "complete",
                    "auto_applied_count": 0,
                    "suggested_count": 0,
                    "media_file_id": str(target_speaker.media_file.uuid)
                    if target_speaker.media_file
                    else None,
                },
            )

            logger.info(
                f"Background merge processing complete {source_speaker_uuid} -> "
                f"{target_speaker_uuid}"
            )
            return {
                "status": "success",
                "source_speaker_uuid": source_speaker_uuid,
                "target_speaker_uuid": target_speaker_uuid,
            }

        except Exception as e:
            logger.error(
                f"Error in background speaker merge {source_speaker_uuid} -> "
                f"{target_speaker_uuid}: {type(e).__name__}: {e}"
            )
            logger.error("Full traceback:", exc_info=True)
            return {"status": "error", "message": str(e)}
