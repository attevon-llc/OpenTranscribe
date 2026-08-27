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

**Session lifetime.** Moving the work off the request path was only half the job: this
task then wrapped all of it in ONE ``session_scope``, so the OpenSearch voiceprint
average, the MinIO cache purge for both files and the index merge all ran with a
Postgres transaction open. A plain SELECT holds ACCESS SHARE for its transaction's
life, which queues every ``ALTER TABLE`` (an Alembic upgrade), pins the vacuum horizon
on ``transcript_segment`` and burns a pool connection. The phases below therefore
either open their OWN short session or hold none at all. See ``tasks/CLAUDE.md``;
``tasks/speaker_attribute_task.py`` is the worked example.
"""

import logging

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope

logger = logging.getLogger(__name__)


def _load_merge_plan(target_speaker_uuid: str) -> dict | None:
    """Read the surviving speaker as PLAIN DATA, then release the session.

    Everything the OpenSearch and WebSocket phases need is materialised here —
    including ``media_file_uuid``, which is a relationship load — so no ORM instance
    outlives the scope. One that did would lazy-load mid-round-trip and quietly open
    a second transaction, which is exactly the leak this split removes.

    Returns ``None`` when the surviving speaker no longer exists.
    """
    from app.utils.uuid_helpers import get_speaker_by_uuid

    with session_scope() as db:
        target_speaker = get_speaker_by_uuid(db, target_speaker_uuid)
        if not target_speaker:
            return None

        return {
            "uuid": str(target_speaker.uuid),
            "id": int(target_speaker.id),
            "user_id": int(target_speaker.user_id),
            "name": str(target_speaker.name),
            "display_name": str(target_speaker.display_name)
            if target_speaker.display_name
            else None,
            "profile_id": int(target_speaker.profile_id) if target_speaker.profile_id else None,
            "media_file_id": int(target_speaker.media_file_id)
            if target_speaker.media_file_id
            else None,
            "media_file_uuid": str(target_speaker.media_file.uuid)
            if target_speaker.media_file
            else None,
        }


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
    from app.utils.websocket_notify import send_ws_event

    affected_media_files = set(media_file_ids or [])

    try:
        logger.info(
            f"Starting background merge processing {source_speaker_uuid} -> {target_speaker_uuid}"
        )

        # Phase 1 — read (short session, Postgres only). Plain data out.
        target = _load_merge_plan(target_speaker_uuid)
        if target is None:
            logger.error(f"Target speaker {target_speaker_uuid} not found in merge task")
            return {"status": "error", "message": "Target speaker not found"}

        # 1. Average the two voiceprints onto the survivor (reads the source doc).
        #    NO session held — two OpenSearch reads and a write.
        _merge_speaker_embeddings(source_speaker_uuid, target)

        # 2. Clear video cache for affected media files. One SHORT session per
        #    file, opened inside; nothing is held across the object deletes.
        _clear_speaker_video_cache(affected_media_files)

        # 3. Delete the source document and update the survivor in OpenSearch.
        #    NO session held.
        _update_opensearch_speaker_merge(source_speaker_uuid, target_speaker_uuid)

        # 4 + 5. Postgres-side follow-ups. Their own short session, after every
        #        network round trip above has finished.
        with session_scope() as db:
            _update_profile_embeddings_after_merge(
                db, source_profile_id, target_profile_id, source_speaker_id
            )
            _refresh_analytics_after_merge(db, affected_media_files)

        # 6. Tell the UI to reload speakers. Reuses the event the speaker-update
        # task already emits; the frontend treats it as a silent refresh and only
        # toasts when auto_applied_count / suggested_count are non-zero.
        #
        # `reason: "speaker_merged"` (issue #603) is what lets the frontend tell this
        # apart from a plain rename: a merge deletes the SOURCE speaker's Postgres row,
        # so segments still holding that dead uuid client-side can never be patched by
        # `applySpeakerRename` (which only knows the surviving uuid) — the handler must
        # instead refetch segments outright. The rename event carries no such field.
        send_ws_event(
            user_id,
            "speaker_processing_complete",
            {
                "speaker_uuid": target_speaker_uuid,
                "display_name": target["display_name"] or "",
                "processing_status": "complete",
                "auto_applied_count": 0,
                "suggested_count": 0,
                "media_file_id": target["media_file_uuid"],
                "reason": "speaker_merged",
            },
        )

        logger.info(
            f"Background merge processing complete {source_speaker_uuid} -> {target_speaker_uuid}"
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
