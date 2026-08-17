"""
Summary retry utilities for OpenTranscribe

This module provides functionality to retry failed AI summaries,
similar to the transcription retry mechanism in task_utils.py
"""

import logging

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.services.llm_service import is_llm_available
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
    from app.utils.uuid_helpers import get_by_uuid_optional

    # get_by_uuid_optional, NOT get_file_by_uuid: the latter raises
    # fastapi.HTTPException on a missing/malformed uuid instead of returning a
    # falsy value, which made the `if not media_file` guard below dead code and
    # let an HTTPException escape this plain (non-endpoint) helper uncaught.
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
        media_file.summary_data = None  # type: ignore[assignment]
        media_file.summary_opensearch_id = None  # type: ignore[assignment]
        media_file.summary_status = "pending"  # type: ignore[assignment]

        db.commit()
        logger.info(f"Reset summary status for file {file_id}")
        return True

    except Exception as e:
        logger.error(f"Error resetting summary for file {file_id}: {e}")
        db.rollback()
        return False


async def check_llm_availability() -> bool:
    """
    Check if LLM service is available for summary generation

    Returns:
        True if LLM is available, False otherwise
    """
    try:
        return await is_llm_available()
    except Exception as e:
        logger.debug(f"Error checking LLM availability: {e}")
        return False


async def retry_summary_if_available(db: Session, file_uuid: str) -> bool:
    """
    Retry summary generation for a specific file if LLM is available

    Args:
        db: Database session
        file_uuid: UUID of the file to retry

    Returns:
        True if retry was queued successfully, False otherwise
    """
    from app.utils.uuid_helpers import get_by_uuid_optional

    # `async def` and `await` here, not `asyncio.run()`: this function's one
    # caller (`retry_summary` in api/endpoints/files/summary_status.py) is
    # itself an async endpoint handler already running inside a live event
    # loop, and `asyncio.run()` cannot start a second one nested inside it —
    # every call from that endpoint raised `RuntimeError: asyncio.run()
    # cannot be called from a running event loop`, so the retry-summary
    # endpoint's success path has never worked (issue #474).
    llm_available = await check_llm_availability()

    # Get internal ID for logging. get_by_uuid_optional, not get_file_by_uuid --
    # see the matching comment in reset_summary_for_retry above.
    media_file = get_by_uuid_optional(db, MediaFile, file_uuid)
    if not media_file:
        logger.debug(f"File {file_uuid} not found for retry")
        return False
    file_id = media_file.id

    if not llm_available:
        logger.debug(f"LLM not available for retry of file {file_id}")
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

    except Exception as e:
        logger.error(f"Error queuing summary retry for file {file_id}: {e}")
        try:
            media_file.summary_data = previous_summary_data  # type: ignore[assignment]
            media_file.summary_opensearch_id = previous_summary_opensearch_id  # type: ignore[assignment]
            media_file.summary_status = previous_summary_status  # type: ignore[assignment]
            db.commit()
        except Exception as restore_exc:
            logger.error(
                f"Failed to restore prior summary state for file {file_id} "
                f"after a failed dispatch: {restore_exc}"
            )
            db.rollback()
        return False
