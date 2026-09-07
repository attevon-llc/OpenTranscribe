"""
Background speaker update task.

Handles heavy operations after a speaker's display_name is updated:
profile embedding updates, OpenSearch synchronization, retroactive
cross-media speaker matching, video cache clearing, and WebSocket
notification.

**Session lifetime.** This task used to wrap its entire body — three OpenSearch
fan-outs, a MinIO cache purge and the retroactive-matching pass — in ONE
``session_scope``. A plain SELECT holds ACCESS SHARE for the life of its
transaction, so that single hold queued every ``ALTER TABLE`` (i.e. an Alembic
upgrade, which dev runs on backend startup), pinned the vacuum horizon on
``transcript_segment``, and burned a pool connection for the whole run. It is now
a sequence of short DB phases with the network work between them, holding
nothing. ``tasks/speaker_attribute_task.py`` is the worked example of the pattern
and ``tasks/CLAUDE.md`` documents the rule.
"""

import logging

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.models.media import Speaker

logger = logging.getLogger(__name__)


def _load_update_plan(
    speaker_uuid: str, old_profile_id: int | None, display_name_changed: bool
) -> dict | None:
    """Read everything the OpenSearch phases need, then release the session.

    Returns **plain data only** — no ORM instances. An instance escaping the scope
    can lazy-load later and silently reopen a transaction underneath a network
    call, which is the bug this split removes. ``None`` means the speaker is gone.

    The display name and profile id are re-read here rather than trusted from the
    task arguments, exactly as the pre-split code did, so a second edit that landed
    while this task was queued still wins. ``profile_sync`` is ``None`` when the
    search document needs no profile write — the same early return the pre-split
    ``_update_opensearch_profile_info`` made.
    """
    from app.api.endpoints.speakers import _load_speaker_profile_sync_payload
    from app.utils.uuid_helpers import get_speaker_by_uuid

    with session_scope() as db:
        speaker = get_speaker_by_uuid(db, speaker_uuid)
        if not speaker:
            return None

        return {
            "display_name": str(speaker.display_name) if speaker.display_name else "",
            "profile_id": int(speaker.profile_id) if speaker.profile_id else None,
            # Resolved while the session is still open; pushed after it closes.
            "profile_sync": _load_speaker_profile_sync_payload(
                db, speaker, old_profile_id, display_name_changed
            ),
        }


def _load_completion_notification(speaker_uuid: str) -> dict:
    """Read the identifiers the completion WebSocket event carries.

    Runs after the labeling workflow, which can assign a profile, so the profile
    UUID has to be re-read rather than reused from the plan.
    """
    from app.utils.uuid_helpers import get_speaker_by_uuid

    with session_scope() as db:
        speaker = get_speaker_by_uuid(db, speaker_uuid)
        if not speaker:
            return {"profile_uuid": None, "media_file_uuid": None}
        return {
            "profile_uuid": str(speaker.profile.uuid) if speaker.profile else None,
            "media_file_uuid": str(speaker.media_file.uuid) if speaker.media_file else None,
        }


def _should_rescore_after_profile_change(
    *,
    display_name_changed: bool,
    display_name: str | None,
    old_profile_id: int | None,
    new_profile_id: int | None,
) -> bool:
    """Should this update re-score the library against a profile's voiceprint?

    A ``SpeakerProfile`` has no voiceprint until a speaker is attached to it, so
    an assignment is the moment the profile first becomes matchable. Nothing
    scored at that moment: ``trigger_retroactive_matching`` was reachable only
    behind the rename gate below, so a profile could be created, one cluster
    attached, and every other recording of the same voice left unmatched with an
    empty Inbox.

    Deliberately FALSE for a rename (that keeps the labeling workflow, which also
    runs ``auto_create_or_assign_profile`` — needed there, and wrong to re-run
    here), for a detach (no profile gained a voiceprint), and for an unchanged
    profile (a no-op update must not queue a full-library similarity pass).
    """
    if display_name_changed and display_name and display_name.strip():
        return False
    return new_profile_id is not None and old_profile_id != new_profile_id


def _rescore_against_profile(speaker_id: int) -> dict[str, int]:
    """Run retroactive matching for a speaker that was just attached to a profile.

    Calls ``trigger_retroactive_matching`` directly rather than
    ``_handle_speaker_labeling_workflow``: the speaker already carries the
    profile, so the workflow's ``auto_create_or_assign_profile`` step has nothing
    to do and would re-resolve an assignment the user just made explicitly.
    """
    from app.api.endpoints.speaker_update import trigger_retroactive_matching

    with session_scope() as db:
        speaker = db.query(Speaker).filter(Speaker.id == speaker_id).first()
        if speaker is None:
            logger.warning(f"Speaker {speaker_id} disappeared before profile re-scoring")
            return {"auto_applied_count": 0, "suggested_count": 0}
        return trigger_retroactive_matching(speaker, db)


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

    Each numbered step below either opens its OWN short session or holds none at
    all; steps 2, 3 and 3b are OpenSearch round trips and run with zero sessions
    open. See the module docstring for why.

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
    from app.api.endpoints.speakers import _load_profile_speaker_names
    from app.api.endpoints.speakers import _push_speaker_display_names
    from app.api.endpoints.speakers import _push_speaker_profile_info
    from app.api.endpoints.speakers import _update_opensearch_speaker_name
    from app.utils.websocket_notify import send_ws_event

    try:
        logger.info(
            f"Starting background processing for speaker {speaker_uuid} "
            f"(display_name: {display_name})"
        )

        # Phase 1 — read (short session, Postgres only). Plain data out.
        plan = _load_update_plan(speaker_uuid, old_profile_id, display_name_changed)
        if plan is None:
            logger.error(f"Speaker {speaker_uuid} not found in background task")
            return {"status": "error", "message": "Speaker not found"}

        # Use the current display_name from DB in case user updated again before task ran
        display_name = plan["display_name"]
        new_profile_id = plan["profile_id"]
        profile_sync = plan["profile_sync"]

        # 1. Handle profile embedding updates (Postgres writes + OpenSearch reads
        #    inside ProfileEmbeddingService — its own short session, not the task's).
        logger.debug(f"Updating profile embeddings for speaker {speaker_uuid}")
        with session_scope() as db:
            _handle_profile_embedding_updates(
                db,
                speaker_id,
                old_profile_id,
                new_profile_id,
                was_auto_labeled,
                display_name_changed,
            )

        # 2. Update OpenSearch with speaker name — NO session held.
        if display_name_changed and display_name:
            logger.debug(f"Updating OpenSearch speaker name for {speaker_uuid}")
            _update_opensearch_speaker_name(speaker_uuid, display_name)

        # 3. Update OpenSearch profile info — NO session held; the payload was
        #    resolved in the read phase above.
        logger.debug(f"Updating OpenSearch profile info for speaker {speaker_uuid}")
        _push_speaker_profile_info(profile_sync)

        # 3b. Replay an in-place profile rename onto every linked speaker's doc.
        #     Short read, then the fan-out with NO session held.
        if renamed_profile_id:
            logger.debug(f"Syncing renamed profile {renamed_profile_id} to OpenSearch")
            with session_scope() as db:
                rename_rows = _load_profile_speaker_names(db, renamed_profile_id)
            _push_speaker_display_names(rename_rows)

        # 4. Handle speaker labeling workflow (retroactive matching) — its own
        #    short session; the matching pass releases it around its OpenSearch phase.
        auto_applied_count = 0
        suggested_count = 0
        if display_name_changed and display_name and display_name.strip():
            logger.debug(f"Running retroactive matching for speaker {speaker_uuid}")
            with session_scope() as db:
                result = _handle_speaker_labeling_workflow(db, speaker_id, display_name)
            if result:
                auto_applied_count = result.get("auto_applied_count", 0)
                suggested_count = result.get("suggested_count", 0)
        elif _should_rescore_after_profile_change(
            display_name_changed=display_name_changed,
            display_name=display_name,
            old_profile_id=old_profile_id,
            new_profile_id=new_profile_id,
        ):
            logger.debug(
                f"Re-scoring library after speaker {speaker_uuid} was attached to "
                f"profile {new_profile_id}"
            )
            result = _rescore_against_profile(speaker_id)
            if result:
                auto_applied_count = result.get("auto_applied_count", 0)
                suggested_count = result.get("suggested_count", 0)

        # 5. Clear video cache — opens its own short session for the one SELECT it
        #    needs; the storage client is constructed before it.
        logger.debug(f"Clearing video cache for media file {media_file_id}")
        _clear_video_cache_for_speaker(media_file_id)

        # 6. Send WebSocket notification that background processing is complete
        logger.debug(f"Sending WebSocket notification for speaker {speaker_uuid}")
        identifiers = _load_completion_notification(speaker_uuid)

        notification_data = {
            "speaker_uuid": speaker_uuid,
            "display_name": display_name,
            "profile_id": identifiers["profile_uuid"],
            "auto_applied_count": auto_applied_count,
            "suggested_count": suggested_count,
            "processing_status": "complete",
            "media_file_id": identifiers["media_file_uuid"],
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
