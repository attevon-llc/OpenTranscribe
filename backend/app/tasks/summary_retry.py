"""
Summary retry utilities for OpenTranscribe

This module provides functionality to retry failed AI summaries,
similar to the transcription retry mechanism in utils/file_retry.py
"""

import logging

from anyio import to_thread
from kombu.exceptions import OperationalError as BrokerUnavailableError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.services.llm_service import is_llm_available
from app.services.llm_settings_outage import LLM_SETTINGS_UNREADABLE_MESSAGE
from app.services.llm_settings_outage import llm_settings_outage_as_503
from app.tasks.summarization import summarize_transcript_task

logger = logging.getLogger(__name__)


def reset_summary_for_retry(db: Session, file_uuid: str) -> bool:
    """
    Reset a file's summary for retry processing, similar to reset_file_for_retry for transcription.

    Args:
        db: Database session
        file_uuid: UUID of the file to reset summary for

    Returns:
        True if reset was successful, False otherwise
    """
    from app.services.uuid_helpers import get_by_uuid_optional

    # get_by_uuid_optional, NOT get_file_by_uuid: the latter raises
    # a DomainError on a missing/malformed uuid instead of returning a
    # falsy value, which made the `if not media_file` guard below dead code and
    # let a DomainError escape this plain (non-endpoint) helper uncaught.
    media_file = get_by_uuid_optional(db, MediaFile, file_uuid)
    if not media_file:
        logger.error(f"File {file_uuid} not found")
        return False

    file_id = media_file.id  # Get internal ID for logging

    # Only retry if transcription is completed
    if media_file.status != "completed":
        logger.error(
            f"Cannot retry summary for file {file_id} - transcription not completed (status: {media_file.status})"
        )
        return False

    try:
        # Reset summary fields
        media_file.summary_data = None
        media_file.summary_opensearch_id = None
        media_file.summary_status = "pending"

        db.commit()

        # Prune the OpenSearch summary plane to match the now-cleared column
        # (issue #963). A regeneration that follows re-dispatches this on its
        # own success (`tasks/summarization.py`); this call is what covers a
        # reset that is never followed by one (no LLM configured, dispatch
        # failure).
        from app.tasks.search_file_field_tasks import index_file_summary

        index_file_summary.delay(int(file_id))

        logger.info(f"Reset summary status for file {file_id}")
        return True

    except (SQLAlchemyError, BrokerUnavailableError) as e:
        logger.error(f"Error resetting summary for file {file_id}: {e}", exc_info=True)
        db.rollback()
        return False


async def check_llm_availability(user_id: int | None = None) -> bool:
    """
    Check if the given user's LLM service is available for summary generation

    Args:
        user_id: The file owner. Providers are per user, so a check without one can never
            see a configured provider.

    Returns:
        True if LLM is available, False otherwise

    Raises:
        SQLAlchemyError: the settings could not be read; an outage is not "unavailable".
    """
    try:
        return await is_llm_available(user_id=user_id)
    except SQLAlchemyError:
        logger.warning(LLM_SETTINGS_UNREADABLE_MESSAGE)
        raise


def _retry_target(db: Session, file_uuid: str) -> tuple[int, int] | None:
    """``(file_id, owner_id)`` for *file_uuid*, or None; the read transaction is released."""
    from app.services.uuid_helpers import get_by_uuid_optional

    # get_by_uuid_optional, not get_file_by_uuid -- see the matching comment in
    # reset_summary_for_retry above.
    media_file = get_by_uuid_optional(db, MediaFile, file_uuid)
    target = (int(media_file.id), int(media_file.user_id)) if media_file else None
    db.close()
    return target


def _reset_and_queue(db: Session, file_uuid: str, file_id: int) -> bool:
    """Reset the summary, queue the regeneration, and restore the old state on a failed queue."""
    from app.services.uuid_helpers import get_by_uuid_optional

    media_file = get_by_uuid_optional(db, MediaFile, file_uuid)
    if media_file is None:
        return False

    # Capture the pre-reset values so a failed dispatch below can restore
    # them. Dispatching BEFORE resetting was considered and rejected: a fast
    # worker could complete the task and write the new summary before this
    # function's own commit ran, and the reset would then immediately
    # clobber that freshly-written result. Resetting first and rolling back
    # on a failed dispatch avoids that race while still fixing the original
    # defect (issue #474) -- a dispatch failure used to leave summary_data
    # wiped with nothing queued to ever regenerate it.
    previous_summary_data = media_file.summary_data
    previous_summary_opensearch_id = media_file.summary_opensearch_id
    previous_summary_status = media_file.summary_status

    # Reset summary status and clear existing data
    if not reset_summary_for_retry(db, file_uuid):
        return False

    try:
        # Queue summarization task
        summarize_transcript_task.delay(file_uuid)
        logger.info(f"Queued summary retry for file {file_id}")
        return True

    except BrokerUnavailableError as e:
        logger.error(f"Error queuing summary retry for file {file_id}: {e}", exc_info=True)
        try:
            media_file.summary_data = previous_summary_data
            media_file.summary_opensearch_id = previous_summary_opensearch_id
            media_file.summary_status = previous_summary_status
            db.commit()

            from app.tasks.search_file_field_tasks import index_file_summary

            index_file_summary.delay(int(file_id))
        except (SQLAlchemyError, BrokerUnavailableError):
            logger.exception(
                f"Failed to restore prior summary state for file {file_id} after a failed dispatch"
            )
            db.rollback()
        return False


async def retry_summary_if_available(db: Session, file_uuid: str) -> bool:
    """
    Retry summary generation for a specific file if LLM is available

    Called from the async ``retry_summary`` endpoint, so every database step runs
    through the AnyIO threadpool (``to_thread.run_sync``, the primitive Starlette's
    ``run_in_threadpool`` wraps) and the request transaction is released before the
    provider health check; only that check is awaited on the event loop.

    Args:
        db: Database session
        file_uuid: UUID of the file to retry

    Returns:
        True if retry was queued successfully, False otherwise
    """
    # `async def` and `await` here, not `asyncio.run()`: this function's one
    # caller (`retry_summary` in api/endpoints/files/summary_status.py) is
    # itself an async endpoint handler already running inside a live event
    # loop, and `asyncio.run()` cannot start a second one nested inside it —
    # every call from that endpoint raised `RuntimeError: asyncio.run()
    # cannot be called from a running event loop`, so the retry-summary
    # endpoint's success path has never worked (issue #474).
    target = await to_thread.run_sync(_retry_target, db, file_uuid)
    if target is None:
        logger.debug(f"File {file_uuid} not found for retry")
        return False
    file_id, owner_id = target

    # Availability is per user: ask for THIS file's owner. An unreadable settings DB raises
    # a 503 here, before anything is reset, so the retry is skipped for this cycle and no
    # file's status changes.
    with llm_settings_outage_as_503():
        llm_available = await check_llm_availability(owner_id)

    if not llm_available:
        logger.debug(f"LLM not available for retry of file {file_id}")
        return False

    return await to_thread.run_sync(_reset_and_queue, db, file_uuid, file_id)
