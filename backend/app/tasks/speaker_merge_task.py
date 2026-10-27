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
    from app.services.privacy.redact_view import PrivacyView
    from app.utils.uuid_helpers import get_speaker_by_uuid

    with session_scope() as db:
        target_speaker = get_speaker_by_uuid(db, target_speaker_uuid)
        if not target_speaker:
            return None

        return {
            # Privacy policy (#1280): read fresh, at run time, for the surviving speaker's
            # file. A merge that was queued before a layer was switched off must not write
            # an averaged voiceprint after it. Unknown file or unreadable policy = False.
            "voiceprints_allowed": PrivacyView(db)
            .voiceprints(int(target_speaker.media_file_id))
            .enabled,
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
            # The file's tenant: the averaged voiceprint is rewritten as a whole document.
            "organization_id": int(target_speaker.media_file.organization_id)
            if target_speaker.media_file and target_speaker.media_file.organization_id is not None
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
        source_speaker_id: Unused since issue #1214 (the endpoint rebuilds the source's
            profile and clusters itself); accepted so already-queued messages unpack.
        source_profile_id: Unused since issue #1214, same reason.
        target_profile_id: Unused since issue #1214; the target's profile is re-read.
        media_file_ids: Media files touched by the merge (video cache + analytics).

    Returns:
        A status dict. A merge is never rolled back by a downstream search/storage
        hiccup; an aggregate that may still hold the source's voice is logged at ERROR
        by ``speaker_cleanup`` and reported as ``status: partial`` with its stages.
    """
    from app.api.endpoints.speakers import _clear_speaker_video_cache
    from app.api.endpoints.speakers import _merge_speaker_embeddings
    from app.api.endpoints.speakers import _reconcile_speakers_after_merge
    from app.api.endpoints.speakers import _refresh_analytics_after_merge
    from app.api.endpoints.speakers import _update_opensearch_speaker_merge
    from app.utils.websocket_notify import send_ws_event
    from app.utils.websocket_notify import send_ws_event_for_file

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
        if target["voiceprints_allowed"]:
            _merge_speaker_embeddings(source_speaker_uuid, target)
        else:
            logger.info(
                "Speaker merge %s -> %s: voiceprint averaging skipped by privacy policy",
                source_speaker_uuid,
                target_speaker_uuid,
            )

        # 2. Clear video cache for affected media files. One SHORT session per
        #    file, opened inside; nothing is held across the object deletes.
        _clear_speaker_video_cache(affected_media_files)

        # 3. Delete the source document and update the survivor in OpenSearch.
        #    NO session held.
        _update_opensearch_speaker_merge(source_speaker_uuid, target_speaker_uuid)

        # 4 + 5. Postgres-side follow-ups. Their own short session, after every
        #        network round trip above has finished. The target's clusters and
        #        profile are rebuilt around the averaged voiceprint (issue #1214).
        with session_scope() as db:
            cleanup_residual = _reconcile_speakers_after_merge(
                db, source_speaker_uuid, int(target["id"])
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
        _merge_notification_data = {
            "speaker_uuid": target_speaker_uuid,
            "display_name": target["display_name"] or "",
            "processing_status": "complete",
            "auto_applied_count": 0,
            "suggested_count": 0,
            "media_file_id": target["media_file_uuid"],
            "reason": "speaker_merged",
        }
        if target["media_file_uuid"] is not None:
            send_ws_event_for_file(
                user_id,
                "speaker_processing_complete",
                _merge_notification_data,
                file_uuid=target["media_file_uuid"],
            )
        else:
            # A speaker with no media file has nothing a quarantine could hide.
            send_ws_event(user_id, "speaker_processing_complete", _merge_notification_data)

        logger.info(
            f"Background merge processing complete {source_speaker_uuid} -> {target_speaker_uuid}"
        )
        cleanup_stages = sorted({str(r.get("stage")) for r in cleanup_residual or []})
        return {
            "status": "partial" if cleanup_stages else "success",
            "source_speaker_uuid": source_speaker_uuid,
            "target_speaker_uuid": target_speaker_uuid,
            "cleanup_incomplete_stages": cleanup_stages,
            "voiceprint_average": "done" if target["voiceprints_allowed"] else "skipped_by_policy",
        }

    except Exception as e:
        logger.error(
            f"Error in background speaker merge {source_speaker_uuid} -> "
            f"{target_speaker_uuid}: {type(e).__name__}: {e}"
        )
        logger.error("Full traceback:", exc_info=True)
        return {"status": "error", "message": str(e)}
