import asyncio
import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.celery import celery_app
from app.core.constants import NLPPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.services.llm_service import LLMService
from app.services.opensearch_summary_service import OpenSearchSummaryService
from app.utils.transcript_builders import build_transcript_and_stats
from app.utils.user_settings_helpers import get_user_llm_output_language

# Setup logging
logger = logging.getLogger(__name__)


def _handle_force_regeneration(media_file: MediaFile, db: Session) -> None:
    """Handle force regeneration by clearing existing summaries."""
    logger.info(
        f"Force regenerate requested - clearing existing summaries for file {int(media_file.id)}"
    )

    # Clear OpenSearch summary if it exists
    if media_file.summary_opensearch_id:
        try:
            summary_service = OpenSearchSummaryService()
            asyncio.run(summary_service.delete_summary(str(media_file.summary_opensearch_id)))
            logger.info(f"Cleared OpenSearch document {media_file.summary_opensearch_id}")
        except Exception as e:
            logger.warning(f"Could not clear OpenSearch summary: {e}")

    # Clear PostgreSQL summary fields
    media_file.summary_data = None  # type: ignore[assignment]
    media_file.summary_opensearch_id = None  # type: ignore[assignment]


def _handle_no_llm_configured(
    media_file: MediaFile, file_id: int, task_id: str, db: Session
) -> dict[str, Any]:
    """Handle case when no LLM provider is configured."""
    from app.utils.task_utils import update_task_status

    logger.info("No LLM provider configured - skipping AI summary generation")

    media_file.summary_status = "not_configured"  # type: ignore[assignment]
    media_file.summary_data = None  # type: ignore[assignment]
    db.commit()

    send_summary_notification(
        int(media_file.user_id),
        file_id,
        "not_configured",
        "AI summary not available - no LLM provider configured in settings",
        0,
    )

    update_task_status(db, task_id, "completed", progress=1.0, completed=True)

    logger.info(
        f"Transcription completed for file {str(media_file.filename)} (no LLM summary generated)"
    )
    return {
        "status": "success",
        "file_id": file_id,
        "message": "Transcription completed successfully. AI summary not available - no LLM provider configured.",
    }


def _create_user_friendly_error(error_msg: str) -> str:
    """Create a user-friendly error message from an exception message."""
    if "timeout" in error_msg.lower():
        return "Request timed out. Try reducing video length or contact support."
    if "context" in error_msg.lower() or "token" in error_msg.lower():
        return "Content too long for model. Try shorter videos or contact support."
    if "connection" in error_msg.lower() or "network" in error_msg.lower():
        return "Network connection failed. Please try again."
    if not error_msg.strip():
        return "Unknown error occurred during summary generation"
    return error_msg


def _handle_llm_error(
    e: Exception,
    media_file: MediaFile,
    file_id: int,
    full_transcript: str,
    llm_provider: str | None,
    llm_model: str | None,
    db: Session,
) -> None:
    """Handle LLM summarization errors."""
    error_type = type(e).__name__
    error_msg = str(e)
    logger.error(f"LLM summarization failed with {error_type}: {error_msg}")
    logger.error(f"Full error details: {repr(e)}")
    logger.error(f"Transcript length: {len(full_transcript)} chars")
    logger.error(f"Provider: {llm_provider or 'unknown'}, Model: {llm_model or 'unknown'}")
    logger.error(f"User ID: {int(media_file.user_id)}")

    media_file.summary_status = "failed"  # type: ignore[assignment]
    db.commit()

    detailed_error = _create_user_friendly_error(error_msg)
    send_summary_notification(
        int(media_file.user_id),
        file_id,
        "failed",
        f"AI summary generation failed: {detailed_error}",
        0,
    )

    raise Exception(
        f"LLM summarization failed: {detailed_error}. No fallback summary will be generated."
    ) from e


def _store_summary_to_opensearch(
    summary_data: dict[str, Any], media_file: MediaFile, file_id: int
) -> str | None:
    """Store summary to OpenSearch and return document ID."""
    summary_service = OpenSearchSummaryService()

    # Get the latest version number for proper versioning
    max_version = asyncio.run(summary_service.get_max_version(file_id, int(media_file.user_id)))

    # Make a copy for OpenSearch indexing with tracking fields
    opensearch_data = summary_data.copy()
    opensearch_data.update(
        {
            "file_id": file_id,
            "user_id": int(media_file.user_id),
            "summary_version": max_version + 1,
            "provider": summary_data["metadata"].get("provider", "unknown"),
            "model": summary_data["metadata"].get("model", "unknown"),
        }
    )

    # Index in OpenSearch
    document_id = asyncio.run(summary_service.index_summary(opensearch_data))

    return document_id


def _send_completion_notification(
    media_file: MediaFile,
    file_id: int,
    summary_data: dict[str, Any],
    document_id: str | None,
    message: str,
) -> None:
    """Send completion notification with summary preview."""
    summary_preview = (
        summary_data.get("brief_summary")
        or summary_data.get("bluf")
        or "Summary generated successfully"
    )
    send_summary_notification(
        int(media_file.user_id),
        file_id,
        "completed",
        message,
        100,
        summary_data=summary_preview,
        summary_opensearch_id=document_id,
    )


def _finalize_summary_storage(
    summary_data: dict[str, Any], media_file: MediaFile, file_id: int, db: Session
) -> str | None:
    """Store summary to OpenSearch and handle all completion scenarios."""
    try:
        document_id = _store_summary_to_opensearch(summary_data, media_file, file_id)

        if document_id:
            media_file.summary_opensearch_id = document_id  # type: ignore[assignment]
            logger.info(f"Summary indexed in OpenSearch: {document_id}")
            media_file.summary_status = "completed"  # type: ignore[assignment]
            db.commit()
            _send_completion_notification(
                media_file,
                file_id,
                summary_data,
                document_id,
                "AI summary generation completed successfully",
            )
        else:
            logger.warning("OpenSearch client not available, summary saved to PostgreSQL only")
            media_file.summary_status = "completed"  # type: ignore[assignment]
            db.commit()
            _send_completion_notification(
                media_file,
                file_id,
                summary_data,
                None,
                "AI summary generation completed (search not available)",
            )
        return document_id

    except Exception as e:
        logger.error(f"Failed to store summary in OpenSearch: {e}")
        logger.info("Summary generated successfully but OpenSearch indexing failed")
        media_file.summary_status = "completed"  # type: ignore[assignment]
        db.commit()
        _send_completion_notification(
            media_file,
            file_id,
            summary_data,
            None,
            "AI summary generation completed (search indexing failed)",
        )
        return None


def _log_error_context(media_file: MediaFile | None) -> None:
    """Log additional context for debugging errors."""
    try:
        if media_file:
            logger.error(
                f"Media file details: ID={int(media_file.id)}, filename={str(media_file.filename)}, "
                f"user_id={int(media_file.user_id)}"
            )
            if hasattr(media_file, "duration"):
                logger.error(
                    f"Media duration: {getattr(media_file, 'duration', 'unknown')} seconds"
                )
    except Exception as ctx_e:
        logger.error(f"Error logging context: {ctx_e}")


def send_summary_notification(
    user_id: int,
    file_id: int,
    status: str,
    message: str,
    progress: int = 0,
    summary_data: dict[str, Any] | str | None = None,
    summary_opensearch_id: str | None = None,
) -> bool:
    """Send summary status notification via WebSocket."""
    from app.services.notification_service import send_task_notification

    extra: dict[str, Any] = {}
    if status == "completed" and summary_data:
        extra["summary"] = summary_data
    if status == "completed" and summary_opensearch_id:
        extra["summary_opensearch_id"] = summary_opensearch_id

    return send_task_notification(
        user_id,
        "summarization_status",
        status=status,
        message=message,
        file_id=file_id,
        progress=progress,
        extra=extra,
    )


def _get_organization_context(db: Session, user_id: int) -> str:
    """Retrieve organization context for a user, respecting prompt type toggles.

    Resolution order:
    1. If user is using a shared context (org_context_use_shared_from), use that
    2. If user has their own context text, use that
    3. Return empty string
    """
    from app import models
    from app.utils.prompt_manager import get_user_active_prompt_info

    # Check if user is using someone else's shared context
    use_shared_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key == "org_context_use_shared_from",
        )
        .first()
    )

    context_owner_id = user_id
    if use_shared_setting and use_shared_setting.setting_value:
        shared_from_id = int(use_shared_setting.setting_value)
        # Verify the shared context is still shared
        is_still_shared = (
            db.query(models.UserSetting)
            .filter(
                models.UserSetting.user_id == shared_from_id,
                models.UserSetting.setting_key == "org_context_is_shared",
                models.UserSetting.setting_value == "true",
            )
            .first()
        )
        if is_still_shared:
            context_owner_id = shared_from_id
            logger.info(f"Using shared org context from user {shared_from_id}")
        else:
            logger.info("Shared org context no longer available, falling back to own")

    # Get the context text from the resolved owner
    context_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == context_owner_id,
            models.UserSetting.setting_key == "org_context_text",
        )
        .first()
    )

    if not context_setting or not context_setting.setting_value:
        return ""

    context_text = str(context_setting.setting_value).strip()
    if not context_text:
        return ""

    # Toggle checks use the CURRENT user's settings (not the sharer's)
    _, is_system_default = get_user_active_prompt_info(user_id, db)

    if is_system_default:
        toggle_key = "org_context_include_default_prompts"
        default_value = "true"
    else:
        toggle_key = "org_context_include_custom_prompts"
        default_value = "false"

    toggle_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key == toggle_key,
        )
        .first()
    )

    toggle_value = str(toggle_setting.setting_value).lower() if toggle_setting else default_value

    if toggle_value != "true":
        logger.info(
            f"Organization context skipped: {toggle_key}={toggle_value} "
            f"(prompt is {'system default' if is_system_default else 'custom'})"
        )
        return ""

    return context_text


def _generate_llm_summary(
    media_file: MediaFile,
    file_id: int,
    full_transcript: str,
    speaker_stats: dict[str, Any],
    task_id: str,
    db: Session,
    prompt_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Generate LLM summary and return summary data."""
    start_time = time.time()
    llm_provider: str | None = None
    llm_model: str | None = None

    # Log transcript details for debugging
    transcript_length = len(full_transcript)
    speaker_count = len(speaker_stats) if speaker_stats else 0
    logger.info(
        f"Starting LLM summary generation: {transcript_length} chars, {speaker_count} speakers"
    )
    logger.info(f"Estimated input tokens: {transcript_length // 3}")

    # Get user's LLM output language preference
    output_language = "en"
    if media_file.user_id:
        output_language = get_user_llm_output_language(db, int(media_file.user_id))
    logger.info(f"LLM output language: {output_language}")

    # Get user's organization context if configured
    organization_context = ""
    if media_file.user_id:
        organization_context = _get_organization_context(db, int(media_file.user_id))
        if organization_context:
            logger.info(f"Organization context loaded ({len(organization_context)} chars)")

    # Create LLM service using user settings or system settings
    if media_file.user_id:
        llm_service = LLMService.create_from_user_settings(int(media_file.user_id))
        logger.info(f"Attempted to load user LLM settings for user {int(media_file.user_id)}")
    else:
        llm_service = LLMService.create_from_system_settings()
        logger.info("Attempted to load system LLM settings")

    if not llm_service:
        return None  # Signal that no LLM is configured

    llm_provider = llm_service.config.provider
    llm_model = llm_service.config.model
    logger.info(f"Using LLM: {llm_provider}/{llm_model}")
    logger.info(f"User context window: {llm_service.user_context_window} tokens")

    try:
        summary_data = llm_service.generate_summary(
            transcript=full_transcript,
            speaker_data=speaker_stats,
            user_id=int(media_file.user_id),
            output_language=output_language,
            organization_context=organization_context,
            prompt_uuid=prompt_uuid,
        )
    except Exception as e:
        _handle_llm_error(e, media_file, file_id, full_transcript, llm_provider, llm_model, db)
        return None  # This line won't be reached due to the raise in _handle_llm_error, but satisfies type checker
    finally:
        llm_service.close()

    processing_time = int((time.time() - start_time) * 1000)
    if "metadata" not in summary_data:
        summary_data["metadata"] = {}
    summary_data["metadata"]["processing_time_ms"] = processing_time
    summary_data["metadata"]["output_language"] = output_language
    logger.info(f"LLM summarization completed in {processing_time}ms")

    return summary_data


def _handle_task_error(
    e: Exception, media_file: MediaFile | None, file_id: int, task_id: str, db: Session
) -> dict[str, Any]:
    """Handle task-level errors and return error result."""
    from app.utils.task_utils import update_task_status

    error_type = type(e).__name__
    error_msg = str(e)
    logger.error(f"Error summarizing file {file_id}: {error_type}: {error_msg}")
    logger.error("Full error traceback:", exc_info=True)

    _log_error_context(media_file)

    # Set summary status to failed if not already set
    try:
        if media_file and str(media_file.summary_status) != "failed":
            user_error_msg = _create_user_friendly_error(error_msg)
            send_summary_notification(
                int(media_file.user_id),
                file_id,
                "failed",
                f"AI summary generation failed: {user_error_msg}",
                0,
            )
            media_file.summary_status = "failed"  # type: ignore[assignment]
            db.commit()
    except Exception as cleanup_e:
        logger.error(
            f"Error during cleanup: {type(cleanup_e).__name__}: {cleanup_e}", exc_info=True
        )

    update_task_status(db, task_id, "failed", error_message=error_msg, completed=True)
    return {"status": "error", "message": error_msg}


@celery_app.task(bind=True, name="ai.generate_summary", priority=NLPPriority.USER_TRIGGERED)
def summarize_transcript_task(
    self,
    file_uuid: str,
    force_regenerate: bool = False,
    prompt_uuid: str | None = None,
):
    """
    Generate a comprehensive summary of a transcript using LLM with structured BLUF format

    This task runs AFTER speaker embedding matching has been completed to ensure
    accurate speaker information is available for summarization.

    Args:
        file_uuid: UUID of the MediaFile to summarize
        force_regenerate: If True, clear existing summaries before regenerating
    """
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status
    from app.utils.uuid_helpers import get_file_by_uuid

    task_id = self.request.id
    media_file: MediaFile | None = None
    file_id: int | None = None

    with session_scope() as db:
        try:
            media_file = get_file_by_uuid(db, file_uuid)
            if not media_file:
                raise ValueError(f"Media file with UUID {file_uuid} not found")

            file_id = int(media_file.id)
            create_task_record(db, task_id, int(media_file.user_id), file_id, "summarization")

            # Safety net: skip if file was marked disabled (per-upload flag
            # or system/user setting). Manual trigger endpoint resets status
            # before dispatching, so this only blocks zombie tasks.
            if str(media_file.summary_status) == "disabled":
                logger.info(f"Summary task skipped — file {file_id} has disabled status")
                update_task_status(db, task_id, "completed", progress=1.0, completed=True)
                return {
                    "status": "skipped",
                    "file_id": file_id,
                    "message": "Summary generation is disabled for this file",
                }

            update_task_status(db, task_id, "in_progress", progress=0.1)

            if force_regenerate:
                _handle_force_regeneration(media_file, db)

            media_file.summary_status = "processing"  # type: ignore[assignment]
            db.commit()

            action = "regeneration" if force_regenerate else "generation"
            send_summary_notification(
                int(media_file.user_id), file_id, "processing", f"AI summary {action} started", 10
            )

            # Get and validate transcript segments
            transcript_segments = (
                db.query(TranscriptSegment)
                .filter(TranscriptSegment.media_file_id == file_id)
                .order_by(TranscriptSegment.start_time)
                .all()
            )
            if not transcript_segments:
                raise ValueError(f"No transcript segments found for file {file_id}")

            # Redact PII/profanity before sending to the LLM provider when the
            # owner's (or admin-forced) policy requires it (don't leak to third parties).
            redaction_cfg = None
            try:
                from app.services.redaction.config import resolve_effective_config

                _cfg = resolve_effective_config(db, int(media_file.user_id))
                if _cfg.enabled and _cfg.redact_before_llm:
                    redaction_cfg = _cfg
            except Exception as _redact_err:  # noqa: BLE001
                logger.debug(f"Redaction config for LLM unavailable: {_redact_err}")

            full_transcript, speaker_stats = build_transcript_and_stats(
                transcript_segments, redaction_cfg
            )

            update_task_status(db, task_id, "in_progress", progress=0.3)
            send_summary_notification(
                int(media_file.user_id), file_id, "processing", "Analyzing speakers and content", 30
            )

            logger.info(
                f"Generating LLM summary for file {str(media_file.filename)} "
                f"(length: {len(full_transcript)} chars)"
            )
            send_summary_notification(
                int(media_file.user_id), file_id, "processing", "Generating AI summary with LLM", 50
            )

            start_time = time.time()
            summary_data = _generate_llm_summary(
                media_file, file_id, full_transcript, speaker_stats, task_id, db, prompt_uuid
            )

            if summary_data is None:
                return _handle_no_llm_configured(media_file, file_id, task_id, db)

            update_task_status(db, task_id, "in_progress", progress=0.7)
            media_file.summary_data = summary_data  # type: ignore[assignment]
            media_file.summary_schema_version = 1  # type: ignore[assignment]

            _finalize_summary_storage(summary_data, media_file, file_id, db)

            # Bump usage_count on the prompt actually used (best-effort; never
            # fail the task). Makes the shared library's usage ordering and the
            # "most used prompts" metric meaningful.
            try:
                from app.utils.prompt_manager import increment_prompt_usage
                from app.utils.prompt_manager import resolve_active_prompt_record

                used_prompt = resolve_active_prompt_record(
                    int(media_file.user_id) if media_file.user_id else None, db, prompt_uuid
                )
                if used_prompt is not None:
                    increment_prompt_usage(db, int(used_prompt.id))
            except Exception as usage_err:  # noqa: BLE001
                logger.warning(f"Could not increment prompt usage_count: {usage_err}")

            logger.info("=== Summarization Task Completed Successfully ===")
            logger.info(f"Total processing time: {int((time.time() - start_time) * 1000)}ms")

            # Get summary_data for logging (with type handling)
            summary_data_value = media_file.summary_data
            if summary_data_value and isinstance(summary_data_value, dict):
                logger.info(f"Final summary data keys: {list(summary_data_value.keys())}")
            else:
                logger.info("Final summary data keys: None")

            logger.info(f"Summary status: {str(media_file.summary_status)}")

            update_task_status(db, task_id, "completed", progress=1.0, completed=True)
            logger.info(
                f"Successfully generated comprehensive summary for file {str(media_file.filename)}"
            )

            return {
                "status": "success",
                "file_id": file_id,
                "summary_data": {
                    "bluf": summary_data.get("bluf", ""),
                    "speakers_analyzed": len(speaker_stats),
                    "processing_time_ms": summary_data["metadata"].get("processing_time_ms"),
                    "opensearch_document_id": getattr(media_file, "summary_opensearch_id", None),
                },
            }

        except Exception as e:
            # file_id might be None if error occurred before it was set
            return _handle_task_error(
                e, media_file, file_id if file_id is not None else 0, task_id, db
            )
