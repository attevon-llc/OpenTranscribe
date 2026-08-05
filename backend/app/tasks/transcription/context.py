"""Transcription task context and the shared failure/validation handlers.

``TranscriptionContext`` is the state bundle every stage of the pipeline
passes around; the helpers here own the error paths that mark a file
FAILED and notify the SPA.
"""

import logging
from dataclasses import dataclass

from app.db.session_utils import get_refreshed_object
from app.db.session_utils import session_scope
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.utils.error_classification import categorize_error
from app.utils.task_utils import update_media_file_status
from app.utils.task_utils import update_task_status

from .notifications import send_error_notification

logger = logging.getLogger(__name__)


@dataclass
class TranscriptionContext:
    """Context holder for transcription task state."""

    task_id: str
    file_id: int
    file_uuid: str
    user_id: int
    file_path: str
    file_name: str
    content_type: str
    # Tenant scope (cloud-edition seam; None = personal / community)
    organization_id: int | None = None


def _get_media_file_context(file_uuid: str, task_id: str) -> TranscriptionContext | None:
    """Get media file and create transcription context."""
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        media_file = get_file_by_uuid(db, file_uuid)
        if not media_file:
            logger.error(f"Media file with UUID {file_uuid} not found")
            return None

        ctx = TranscriptionContext(
            task_id=task_id,
            file_id=int(media_file.id),
            file_uuid=file_uuid,
            user_id=int(media_file.user_id),
            file_path=str(media_file.storage_path),
            file_name=str(media_file.filename),
            content_type=str(media_file.content_type),
            organization_id=media_file.organization_id,
        )
        update_media_file_status(db, ctx.file_id, FileStatus.PROCESSING)
        return ctx


def _handle_transcription_failure(
    ctx: TranscriptionContext, task_id: str, error_msg: str, error_type: str
) -> dict:
    """Handle transcription failure by updating status and sending notification."""
    with session_scope() as db:
        update_task_status(db, task_id, "failed", error_message=error_msg, completed=True)
        update_media_file_status(db, ctx.file_id, FileStatus.ERROR)
        media_file = get_refreshed_object(db, MediaFile, ctx.file_id)
        if media_file:
            media_file.last_error_message = error_msg
            media_file.error_category = categorize_error(error_msg).value
            db.commit()

        # Cloud-edition seam: a FAILED run must still fire the completion hook
        # (success=False) so the quota layer releases the reservation taken at
        # dispatch — otherwise crashed jobs permanently consume quota headroom.
        # No-op in community; failures contained by the hook registry.
        try:
            from .hooks import CompletionContext
            from .hooks import fire_transcription_complete

            fire_transcription_complete(
                CompletionContext(
                    file_id=ctx.file_id,
                    file_uuid=str(ctx.file_uuid),
                    user_id=ctx.user_id,
                    organization_id=media_file.organization_id if media_file else None,
                    audio_duration_s=0.0,
                    run_id=task_id,
                    provider="local",
                    success=False,
                )
            )
        except Exception:  # pragma: no cover — hook layer already contains
            logger.exception("Failure-path completion hook raised (contained)")

    send_error_notification(ctx.user_id, ctx.file_id, error_msg)
    return {"status": "error", "message": error_msg, "error_type": error_type}


def _validate_transcription_result(
    result: dict, ctx: TranscriptionContext, task_id: str
) -> dict | None:
    """Validate transcription result has valid content. Returns error dict if invalid, None if valid."""
    if not result or not result.get("segments") or len(result["segments"]) == 0:
        error_msg = (
            "No audio content could be detected in this file. "
            "The file may be corrupted, contain only silence, or be in an unsupported format. "
            "Please check the file and try uploading again."
        )
        logger.warning(f"No valid audio content found in file {ctx.file_id}: {ctx.file_name}")
        return _handle_transcription_failure(ctx, task_id, error_msg, "no_valid_audio")

    # Check if segments contain actual transcribable content
    has_content = any(segment.get("text", "").strip() for segment in result["segments"])
    if not has_content:
        error_msg = (
            "No speech could be detected in this file. "
            "The file may contain only music, background noise, or silence. "
            "Please verify the file contains clear speech and try again."
        )
        logger.warning(f"No speech content found in file {ctx.file_id}: {ctx.file_name}")
        return _handle_transcription_failure(ctx, task_id, error_msg, "no_speech_content")

    return None


def _get_user_friendly_error_message(error_message: str) -> str:
    """Convert technical error to user-friendly message."""
    error_lower = error_message.lower()

    if "libcudnn" in error_lower:
        return (
            "Audio processing failed due to a system library compatibility issue. "
            "The transcription service requires updated dependencies. "
            "Please contact support for assistance."
        )
    if "cuda" in error_lower and "out of memory" in error_lower:
        return (
            "GPU out of memory error. The audio file may be too large for available GPU resources. "
            "Please try with a shorter audio file or contact support."
        )
    if "cuda" in error_lower or "gpu" in error_lower:
        return (
            "GPU processing error occurred during transcription. "
            "The system may need reconfiguration. "
            "Please try again or contact support if the issue persists."
        )
    if "model" in error_lower and ("download" in error_lower or "load" in error_lower):
        return (
            "Failed to download or load AI models. "
            "Please check your internet connection and try again. "
            "If the problem persists, contact support."
        )
    return error_message


def _handle_outer_exception(
    ctx: TranscriptionContext | None, task_id: str, error: Exception
) -> dict:
    """Handle top-level exception in transcription task."""
    file_id = ctx.file_id if ctx else None
    user_id = ctx.user_id if ctx else None
    error_msg = str(error)

    logger.error(f"Error processing file {file_id}: {error_msg}")

    try:
        with session_scope() as db:
            if file_id:
                update_media_file_status(db, file_id, FileStatus.ERROR)
                media_file = get_refreshed_object(db, MediaFile, file_id)
                if media_file:
                    media_file.error_category = categorize_error(error_msg).value
                    db.commit()
            update_task_status(db, task_id, "failed", error_message=error_msg, completed=True)

        if user_id and file_id:
            send_error_notification(user_id, file_id, error_msg)
    except Exception as update_err:
        logger.error(f"Error updating task status: {update_err}")

    return {"status": "error", "message": error_msg}
