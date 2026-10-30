"""Delete-safety checks and the retry reset for a media file."""

import logging
import time

from celery.result import AsyncResult
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.env import env_bool
from app.db.session_utils import get_refreshed_object
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.media import Task
from app.utils.task_cancel import cancel_active_task

logger = logging.getLogger(__name__)


def is_file_safe_to_delete(db: Session, file_id: int) -> tuple[bool, str]:
    """Check if a file is safe to delete (no active processing).

    Args:
        db: Database session
        file_id: ID of the file to check

    Returns:
        Tuple of (is_safe, reason)
    """
    media_file = get_refreshed_object(db, MediaFile, file_id)
    if not media_file:
        return False, "File not found"

    # Check if file has an active task. Deliberately NOT gated on
    # `media_file.status == FileStatus.PROCESSING` — a selective reprocess of an
    # already-completed file (search_indexing/analytics/speaker_llm/summarization/
    # topic_extraction/speaker_clustering) sets `active_task_id` via
    # `create_task_record`, but that function only flips status to PROCESSING when
    # the file was previously PENDING, so an in-flight downstream stage on a
    # completed file leaves status="completed" with a genuinely live task. Requiring
    # both conditions let `purge_media_file` run concurrently with that task, which
    # could delete the file (and its OpenSearch chunks, via a delete-by-query that
    # finds nothing yet) moments before the task's own async write landed — leaving
    # a permanently orphaned chunk with no MediaFile or Speaker row left to clean it
    # up. `active_task_id` alone is enough to warrant the Celery check below; the
    # check itself is what actually confirms liveness, so this only adds coverage,
    # it never skips a check that used to run.
    active_task_id = media_file.active_task_id
    if active_task_id:
        # Double-check with Celery
        try:
            task_result = AsyncResult(str(active_task_id))
            if task_result.state in ["PENDING", "STARTED", "RETRY"]:
                return (
                    False,
                    f"File is currently being processed (task: {active_task_id})",
                )
        except Exception as e:
            # If we can't check task status, assume it's safe
            logger.warning(
                f"Could not check Celery task status for {active_task_id}: {e}. "
                "Assuming file is safe to delete."
            )

    # Safe to delete in these states
    safe_states = [
        FileStatus.COMPLETED,
        FileStatus.ERROR,
        FileStatus.CANCELLED,
        FileStatus.ORPHANED,
    ]
    if media_file.status in safe_states:
        return True, "File is not actively processing"

    # Pending files might be picked up soon
    if media_file.status == FileStatus.PENDING:
        return True, "File is pending but not yet processing"

    return False, f"File status is {media_file.status}"


#: SQLSTATE 40P01 (deadlock_detected) — the standard Postgres code, checked instead of a
#: driver-specific exception class so this survives a DB-driver swap. A deadlock is a
#: transient, order-of-execution artifact (Postgres picks a victim transaction to abort so
#: the others can proceed) — retrying it is the correct response, not a workaround.
_DEADLOCK_SQLSTATE = "40P01"


_DEADLOCK_RETRY_ATTEMPTS = 3


_DEADLOCK_RETRY_DELAY_S = 0.5


def media_object_is_resolvable(media_file: MediaFile) -> bool:
    """Whether this row's media actually resolves to an object in storage.

    Fails **closed**: only a positive "the object is there" answers True. The
    caller uses this to decide whether deleting a transcript is recoverable, and
    the two error directions are not symmetric — reading a storage outage as
    "present" destroys the transcript, reading it as "absent" merely refuses a
    reprocess the user can repeat once storage is back.

    Args:
        media_file: The file whose ``storage_path`` should be resolved.

    Returns:
        True only when the object was confirmed present.
    """
    storage_path = media_file.storage_path
    if not storage_path:
        # The repo's own "this row has no media" convention (four writers, eight
        # readers) — there is nothing to look up.
        return False

    if env_bool("SKIP_S3"):
        # No object storage is in play in this deployment mode, so there is no
        # absence to confirm and nothing this check could learn. Same switch the
        # upload/streaming/thumbnail paths already read.
        return True

    from app.services.minio_service import object_exists_and_size

    try:
        return object_exists_and_size(str(storage_path)) is not None
    except Exception as e:
        # object_exists_and_size only returns None for a genuine "no such key";
        # everything else (outage, credentials, network) raises, and none of
        # those is evidence the object is gone.
        logger.warning(
            f"Could not confirm media object {storage_path!r} for file {media_file.id}: "
            f"{type(e).__name__}: {e}. Treating it as unresolvable."
        )
        return False


def transcript_is_regenerable(db: Session, media_file: MediaFile) -> bool:
    """Whether re-running the pipeline could reproduce this file's transcript.

    False only when the file **has** transcript segments and its media object
    cannot be found — the state in which clearing the transcript destroys the
    only copy (issue #872). A file with no segments has nothing to lose, so it
    stays retryable: rows created by the upload, URL-ingestion, media-download
    and watch-source paths legitimately carry no storage object yet, and
    retrying one is how the download re-runs.

    Args:
        db: Database session.
        media_file: The file about to have its transcript cleared.

    Returns:
        True when clearing the transcript is safe.
    """
    from app.models.media import TranscriptSegment

    has_segments = (
        db.query(TranscriptSegment.id)
        .filter(TranscriptSegment.media_file_id == media_file.id)
        .first()
        is not None
    )
    if not has_segments:
        return True

    return media_object_is_resolvable(media_file)


def reset_file_for_retry(db: Session, file_id: int, reset_retry_count: bool = False) -> bool:
    """Reset a file for retry processing.

    Args:
        db: Database session
        file_id: ID of the file to reset
        reset_retry_count: Whether to reset the retry count to 0

    Returns:
        True if reset was successful, False otherwise
    """
    for attempt in range(1, _DEADLOCK_RETRY_ATTEMPTS + 1):
        media_file = get_refreshed_object(db, MediaFile, file_id)
        if not media_file:
            return False

        # Issue #1276: a held file's transcript is evidence. The entry points answer 409 first;
        # this is the backstop for any caller that did not, so none can replace a held transcript.
        from app.services.hold_guard import hold_state

        if hold_state(db, file_id).held:
            logger.error(f"Refusing to reset file {file_id} for retry: it is under a legal hold.")
            return False

        # Issue #872: the deletes below are unconditional and COMMIT, so a file
        # whose media cannot be found loses its only copy of the transcript with
        # nothing left to regenerate it from. The refusal lives here rather than
        # at the callers because every reprocess/retry/recovery entry point
        # reaches this one function.
        if not transcript_is_regenerable(db, media_file):
            logger.error(
                f"Refusing to reset file {file_id} for retry: it has transcript segments "
                f"and its media object ({media_file.storage_path!r}) could not be resolved "
                "in storage, so re-running the pipeline could not reproduce them."
            )
            return False

        try:
            # Cancel any active task first
            if media_file.active_task_id:
                cancel_active_task(db, file_id)

            # Reset retry count if requested. `retry_count` is nullable with a Python-side
            # default of 0, so a NULL row must be normalized rather than incremented in
            # place — this is the manual-retry entry point (POST /my-files/{uuid}/retry),
            # and a bare `+= 1` here turns a NULL row into a 500 the user cannot get past.
            if reset_retry_count:
                media_file.retry_count = 0
            else:
                media_file.retry_count = int(media_file.retry_count or 0) + 1

            # Reset file state
            media_file.status = FileStatus.PENDING
            media_file.active_task_id = None
            media_file.task_started_at = None
            media_file.task_last_update = None
            media_file.cancellation_requested = False
            media_file.last_error_message = None
            # A retry that clears the message but keeps the retry-policy classification
            # would strand a `worker_lost` category on a freshly-PENDING row.
            media_file.error_category = None
            media_file.force_delete_eligible = False

            # Clear existing transcript data for clean retry
            from app.models.media import Analytics
            from app.models.media import TranscriptSegment
            from app.models.speaker import Speaker

            # The user's corrections of the speakers deleted below are kept by the audio
            # they judged, read while the segments still name them (issue #1233).
            from app.services.speaker_correction_carryover import stash_file_corrections

            stash_file_corrections(db, int(file_id))

            # Delete existing transcript segments
            db.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == file_id).delete()

            # Delete existing speakers. What they belonged to is read first: the
            # CASCADE forgets which clusters and profiles averaged their voices in,
            # and their voiceprints were never removed here at all (issue #1214).
            from app.services.speaker_cleanup import capture_speaker_impact

            speaker_impact = capture_speaker_impact(
                db,
                [int(r[0]) for r in db.query(Speaker.id).filter(Speaker.media_file_id == file_id)],
                removed=True,
                reindex=False,
            )
            db.query(Speaker).filter(Speaker.media_file_id == file_id).delete()

            # Delete existing analytics
            db.query(Analytics).filter(Analytics.media_file_id == file_id).delete()

            # Clear related task records for clean slate
            db.query(Task).filter(Task.media_file_id == file_id).delete()

            # Reset summary fields so auto-summary triggers correctly after transcription retry
            media_file.summary_data = None
            media_file.summary_opensearch_id = None
            media_file.summary_status = "pending"

            db.commit()

            # A residual is logged at ERROR by the shared cleanup; the retry proceeds.
            from app.services.speaker_cleanup import reconcile_speaker_impact

            reconcile_speaker_impact(db, speaker_impact)

            # Prune the OpenSearch summary plane to match the cleared column
            # (issue #963) — a transcription retry may never reach a fresh
            # summary generation, so nothing else is guaranteed to.
            from app.tasks.search_indexing_task import index_file_summary

            index_file_summary.delay(int(file_id))

            logger.info(f"Reset file {file_id} for retry (attempt {media_file.retry_count})")
            return True

        except OperationalError as e:
            db.rollback()
            is_deadlock = getattr(getattr(e, "orig", None), "pgcode", None) == _DEADLOCK_SQLSTATE
            if is_deadlock and attempt < _DEADLOCK_RETRY_ATTEMPTS:
                logger.warning(
                    f"Deadlock resetting file {file_id} for retry "
                    f"(attempt {attempt}/{_DEADLOCK_RETRY_ATTEMPTS}); retrying"
                )
                time.sleep(_DEADLOCK_RETRY_DELAY_S)
                continue
            logger.error(f"Failed to reset file {file_id} for retry: {e}")
            return False

        except Exception as e:
            logger.error(f"Failed to reset file {file_id} for retry: {e}")
            db.rollback()
            return False

    return False
