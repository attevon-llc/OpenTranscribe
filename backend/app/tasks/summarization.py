"""LLM summarization task.

**Three phases, and the split is load-bearing.** The DB session is open only for
the two short DB-only phases (read the transcript + settings, write the result)
and is **closed** across the slow middle phase: an LLM completion over a whole
transcript. That middle phase is multi-minute on a long file and, if the provider
stalls, is bounded only by the HTTP timeout.

**The result lands in ``media_file.summary_data`` and nowhere else (#67).** This
task used to mirror the same dict into a ``transcript_summaries`` OpenSearch
index and stamp the document id onto ``summary_opensearch_id``. That index is
retired — chat grounding moved to the digest plane in the v6 ``transcript_chunks``
index, and the file page always rendered the column — so a second copy bought a
versioning history nothing read, a second GDPR erasure surface, and a store that
could disagree with the column it was copied from.

Before the split, one ``session_scope`` wrapped the entire task body, so Postgres
sat ``idle in transaction`` for the whole provider round trip with its last
statement being the full ``transcript_segment`` SELECT below. Such a transaction
holds ACCESS SHARE on ``transcript_segment``, so every ``ALTER TABLE`` — i.e. any
Alembic upgrade, which dev runs automatically on backend startup — queues behind
it; it pins the vacuum horizon on the largest table in the product; and it
consumes a pool connection for its whole life. Measured: an NLP worker held one
for 1 h 26 m.

See ``app/tasks/CLAUDE.md`` ("The session-lifetime rule") and
``speaker_attribute_task.py`` for the worked example this follows.
"""

import logging
import time
from typing import Any

from celery.exceptions import Retry

from app.core.celery import celery_app
from app.core.enums import PostProcessingState
from app.core.enums import PostProcessingTask
from app.core.queues import NLPPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services.file_provenance import llm_facts
from app.services.file_provenance import record_stage
from app.services.llm_service import LLMService
from app.services.post_processing_status import announce_started
from app.services.post_processing_status import report as report_post_processing
from app.services.post_processing_status import tracks_post_processing
from app.services.redaction.llm_guard import RedactionNotReadyError
from app.services.redaction.llm_guard import defer_for_redaction
from app.tasks.summarization_inputs import _load_summarization_inputs

# Setup logging
logger = logging.getLogger(__name__)


def _llm_is_configured(user_id: int | None) -> bool:
    """Is an LLM provider available for this user?

    Called BEFORE the task announces any progress. ``_handle_no_llm_configured``
    deliberately sends no notification — having no provider is a deployment
    choice, not a per-file failure, and flagging it per file would bury real
    failures. But the task used to emit "Generating AI summary with LLM — 50%"
    and two frames before it, and only then discover there was nothing to call.
    With nothing terminal following, that left the notification panel pinned at
    50% on a file that was completely finished.

    Resolving availability first keeps the silence and removes the stranded
    frame. The service this opens is closed immediately; the summary path
    creates its own moments later, which costs one settings read and keeps
    ownership of the long-lived client in one place.
    """
    service = (
        LLMService.create_from_user_settings(user_id)
        if user_id
        else LLMService.create_from_system_settings()
    )
    if service is None:
        return False
    service.close()
    return True


def _handle_no_llm_configured(
    file_id: int, user_id: int, filename: str, task_id: str
) -> dict[str, Any]:
    """Handle case when no LLM provider is configured."""
    from app.utils.task_utils import update_task_status

    logger.info("No LLM provider configured - skipping AI summary generation")

    # The DB status is kept: the file detail page reads it to explain, in
    # context, why there is no summary. The push notification is not — having
    # no provider configured is a deployment choice rather than a task outcome,
    # so flagging it per file buries real failures under noise the user cannot
    # act on from a notification. A configured provider that errors or returns
    # nothing still notifies, which is the case worth surfacing.
    with session_scope() as db:
        db.query(MediaFile).filter(MediaFile.id == file_id).update(
            {"summary_status": "not_configured", "summary_data": None},
            synchronize_session=False,
        )
        update_task_status(db, task_id, "completed", progress=1.0, completed=True)

    logger.info(f"Transcription completed for file {filename} (no LLM summary generated)")
    # "skipped", not "success": nothing was summarized, and the post-processing status
    # reports this run as skipped rather than as a finished AI summary.
    return {
        "status": "skipped",
        "reason": "llm_not_configured",
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


def _mark_summary_failed(file_id: int) -> None:
    """Flip ``summary_status`` to ``failed`` in its own short transaction."""
    with session_scope() as db:
        db.query(MediaFile).filter(MediaFile.id == file_id).update(
            {"summary_status": "failed"}, synchronize_session=False
        )


def _handle_llm_error(
    e: Exception,
    file_id: int,
    user_id: int,
    full_transcript: str,
    llm_provider: str | None,
    llm_model: str | None,
) -> None:
    """Handle LLM summarization errors.

    Opens its **own** short session rather than borrowing the caller's: the
    caller is mid-provider-call and holds no session by design.
    """
    error_type = type(e).__name__
    error_msg = str(e)
    logger.error(f"LLM summarization failed with {error_type}: {error_msg}")
    logger.error(f"Full error details: {repr(e)}")
    logger.error(f"Transcript length: {len(full_transcript)} chars")
    logger.error(f"Provider: {llm_provider or 'unknown'}, Model: {llm_model or 'unknown'}")
    logger.error(f"User ID: {user_id}")

    _mark_summary_failed(file_id)

    detailed_error = _create_user_friendly_error(error_msg)
    send_summary_notification(
        user_id,
        file_id,
        "failed",
        f"AI summary generation failed: {detailed_error}",
        0,
    )

    raise Exception(
        f"LLM summarization failed: {detailed_error}. No fallback summary will be generated."
    ) from e


def _send_completion_notification(
    user_id: int,
    file_id: int,
    summary_data: dict[str, Any],
    message: str,
    duration_seconds: float | None = None,
) -> None:
    """Send completion notification with summary preview."""
    summary_preview = (
        summary_data.get("brief_summary")
        or summary_data.get("bluf")
        or "Summary generated successfully"
    )
    send_summary_notification(
        user_id,
        file_id,
        "completed",
        message,
        100,
        summary_data=summary_preview,
        duration_seconds=duration_seconds,
    )


def send_summary_notification(
    user_id: int,
    file_id: int,
    status: str,
    message: str,
    progress: int = 0,
    summary_data: dict[str, Any] | str | None = None,
    duration_seconds: float | None = None,
) -> bool:
    """Send summary status notification via WebSocket."""
    from app.services.notification_service import send_task_notification

    extra: dict[str, Any] = {}
    if status == "completed" and summary_data:
        extra["summary"] = summary_data
    if duration_seconds is not None:
        extra["duration_seconds"] = duration_seconds

    return send_task_notification(
        user_id,
        "summarization_status",
        status=status,
        message=message,
        file_id=file_id,
        progress=progress,
        extra=extra,
    )


def _generate_llm_summary(
    inputs: dict[str, Any],
    prompt_uuid: str | None = None,
) -> dict[str, Any] | None:
    """Phase 2 — generate the LLM summary. **No DB session is held here.**

    ``LLMService.create_from_user_settings`` opens (and closes) its own short
    session internally, which is exactly the shape the rule asks for: a callee
    that needs the DB opens its own scope rather than borrowing one that then
    spans the provider round trip.

    Returns the summary payload, or ``None`` when no LLM provider is configured.
    """
    start_time = time.time()
    llm_provider: str | None = None
    llm_model: str | None = None

    file_id = inputs["file_id"]
    user_id = inputs["user_id"]
    full_transcript = inputs["full_transcript"]
    output_language = inputs["output_language"]

    transcript_length = len(full_transcript)
    speaker_count = len(inputs["speaker_stats"]) if inputs["speaker_stats"] else 0
    logger.info(
        f"Starting LLM summary generation: {transcript_length} chars, {speaker_count} speakers"
    )
    logger.info(f"Estimated input tokens: {transcript_length // 3}")
    logger.info(f"LLM output language: {output_language}")

    if user_id:
        llm_service = LLMService.create_from_user_settings(user_id)
        logger.info(f"Attempted to load user LLM settings for user {user_id}")
    else:
        llm_service = LLMService.create_from_system_settings()
        logger.info("Attempted to load system LLM settings")

    if not llm_service:
        return None  # Signal that no LLM is configured

    llm_provider = llm_service.config.provider
    llm_model = llm_service.config.model
    inputs["llm_provenance"] = llm_facts(llm_service.config)
    logger.info(f"Using LLM: {llm_provider}/{llm_model}")
    logger.info(f"User context window: {llm_service.user_context_window} tokens")

    try:
        summary_data = llm_service.generate_summary(
            transcript=full_transcript,
            speaker_data=inputs["speaker_stats"],
            user_id=user_id,
            output_language=output_language,
            organization_context=inputs["organization_context"],
            prompt_uuid=prompt_uuid,
        )
    except Exception as e:
        _handle_llm_error(e, file_id, user_id, full_transcript, llm_provider, llm_model)
        return None  # unreachable — _handle_llm_error always raises
    finally:
        llm_service.close()

    processing_time = int((time.time() - start_time) * 1000)
    if "metadata" not in summary_data:
        summary_data["metadata"] = {}
    summary_data["metadata"]["processing_time_ms"] = processing_time
    summary_data["metadata"]["output_language"] = output_language
    # #464: the staleness key `chat/mapreduce.scope_digest_hits` compares
    # against `file_facts.source_fingerprint` before trusting this summary in
    # the map tier. Stamped unconditionally — a summary with no fingerprint is
    # exactly what a pre-#464 (legacy) summary looks like, and the map tier
    # treats "no fingerprint" as "stale" on purpose (self-healing rather than
    # trusted on faith).
    summary_data["metadata"]["source_fingerprint"] = inputs.get("source_fingerprint")
    logger.info(f"LLM summarization completed in {processing_time}ms")

    return summary_data


def _persist_summary(
    file_id: int,
    user_id: int,
    task_id: str,
    summary_data: dict[str, Any],
    prompt_uuid: str | None,
    provenance: dict[str, Any] | None = None,
) -> float | None:
    """Phase 3 — write (short session, Postgres only).

    ``summary_data`` is the whole summary and the only copy of it (#67).

    Returns the just-finished task's wall-clock duration in seconds (issue
    #753's duration chip), or ``None`` if unavailable — see
    ``task_utils.update_task_status``'s docstring for why it must be read
    here rather than reconstructed later.
    """
    from app.utils.task_utils import update_task_status_with_duration

    with session_scope() as db:
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if media_file is None:
            raise ValueError(f"Media file {file_id} disappeared during summarization")

        media_file.summary_data = summary_data  # type: ignore[assignment]
        media_file.summary_schema_version = 1  # type: ignore[assignment]
        media_file.summary_status = "completed"  # type: ignore[assignment]
        if provenance:
            record_stage(db, file_id, "summary", **provenance)

        # Bump usage_count on the prompt actually used (best-effort; never
        # fail the task). Makes the shared library's usage ordering and the
        # "most used prompts" metric meaningful.
        try:
            from app.utils.prompt_manager import increment_prompt_usage
            from app.utils.prompt_manager import resolve_active_prompt_record

            used_prompt = resolve_active_prompt_record(user_id or None, db, prompt_uuid)
            if used_prompt is not None:
                increment_prompt_usage(db, int(used_prompt.id))
        except Exception as usage_err:  # noqa: BLE001
            logger.warning(f"Could not increment prompt usage_count: {usage_err}")

        _task, duration_seconds = update_task_status_with_duration(
            db, task_id, "completed", progress=1.0, completed=True
        )
        return duration_seconds


def _handle_task_error(
    e: Exception, file_uuid: str, file_id: int | None, task_id: str
) -> dict[str, Any]:
    """Handle task-level errors in a short session of its own."""
    from app.services.uuid_helpers import get_file_by_uuid
    from app.utils.task_utils import update_task_status

    error_type = type(e).__name__
    error_msg = str(e)
    logger.error(f"Error summarizing file {file_id}: {error_type}: {error_msg}")
    logger.error("Full error traceback:", exc_info=True)

    notify: tuple[int, int, str] | None = None
    try:
        with session_scope() as db:
            media_file = get_file_by_uuid(db, file_uuid)
            if media_file is not None:
                logger.error(
                    f"Media file details: ID={int(media_file.id)}, "
                    f"filename={str(media_file.filename)}, user_id={int(media_file.user_id)}"
                )
                if str(media_file.summary_status) != "failed":
                    notify = (
                        int(media_file.user_id),
                        int(media_file.id),
                        _create_user_friendly_error(error_msg),
                    )
                    media_file.summary_status = "failed"  # type: ignore[assignment]
            update_task_status(db, task_id, "failed", error_message=error_msg, completed=True)
    except Exception as cleanup_e:
        logger.error(
            f"Error during cleanup: {type(cleanup_e).__name__}: {cleanup_e}", exc_info=True
        )

    if notify is not None:
        user_id, notify_file_id, friendly = notify
        send_summary_notification(
            user_id,
            notify_file_id,
            "failed",
            f"AI summary generation failed: {friendly}",
            0,
        )

    return {"status": "error", "message": error_msg}


@celery_app.task(bind=True, name="ai.generate_summary", priority=NLPPriority.USER_TRIGGERED)
@tracks_post_processing(PostProcessingTask.SUMMARY, file_param="file_uuid", announce_start=False)
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
    task_id = self.request.id
    file_id: int | None = None
    start_time = time.time()

    try:
        # Phase 1 — read (DB session open, Postgres only).
        inputs = _load_summarization_inputs(file_uuid, task_id, force_regenerate)
        if "early_result" in inputs:
            return inputs["early_result"]

        file_id = inputs["file_id"]
        user_id = inputs["user_id"]

        # Phase 2 — the slow phase. NO DB session is held from here until the
        # write below: an LLM completion over the whole transcript.
        # Resolve provider availability BEFORE announcing anything: the
        # not-configured path is deliberately silent, so a progress frame sent
        # ahead of it has nothing to close it out. See `_llm_is_configured`.
        if not _llm_is_configured(user_id):
            return _handle_no_llm_configured(file_id, user_id, inputs["filename"], task_id)
        announce_started(PostProcessingTask.SUMMARY, file_id=file_id)

        action = "regeneration" if force_regenerate else "generation"
        send_summary_notification(
            user_id, file_id, "processing", f"AI summary {action} started", 10
        )
        send_summary_notification(
            user_id, file_id, "processing", "Analyzing speakers and content", 30
        )
        logger.info(
            f"Generating LLM summary for file {inputs['filename']} "
            f"(length: {len(inputs['full_transcript'])} chars)"
        )
        send_summary_notification(
            user_id, file_id, "processing", "Generating AI summary with LLM", 50
        )

        summary_data = _generate_llm_summary(inputs, prompt_uuid)
        if summary_data is None:
            return _handle_no_llm_configured(file_id, user_id, inputs["filename"], task_id)

        # Phase 3 — write (DB session reopened, Postgres only).
        duration_seconds = _persist_summary(
            file_id, user_id, task_id, summary_data, prompt_uuid, inputs.get("llm_provenance")
        )

        # Rebuild the OpenSearch summary plane from what was just committed
        # (issue #963). Dispatched AFTER `_persist_summary`'s own session
        # committed — never from inside it, and never with the dict passed
        # along: `_index_summary_plane` re-reads `summary_data` from Postgres
        # itself, which is the whole anti-regression for #67.
        from app.tasks.search_file_field_tasks import index_file_summary

        index_file_summary.delay(file_id)
        # Declared before this task reports done, so the summary's index is never an
        # undeclared tail behind a "finished" summary.
        report_post_processing(
            PostProcessingTask.SUMMARY_INDEXING, PostProcessingState.QUEUED, file_id=file_id
        )

        _send_completion_notification(
            user_id,
            file_id,
            summary_data,
            "AI summary generation completed successfully",
            duration_seconds=duration_seconds,
        )

        logger.info("=== Summarization Task Completed Successfully ===")
        logger.info(f"Total processing time: {int((time.time() - start_time) * 1000)}ms")
        logger.info(f"Final summary data keys: {list(summary_data.keys())}")
        logger.info(f"Successfully generated comprehensive summary for file {inputs['filename']}")

        return {
            "status": "success",
            "file_id": file_id,
            "summary_data": {
                "bluf": summary_data.get("bluf", ""),
                "speakers_analyzed": len(inputs["speaker_stats"]),
                "processing_time_ms": summary_data["metadata"].get("processing_time_ms"),
            },
        }

    except Retry:
        # Celery signals deferral with an exception that subclasses Exception, so
        # the broad handler below would "handle" it and mark the summary failed.
        raise
    except RedactionNotReadyError as not_ready:
        # Detection hasn't cached spans yet. ``defer_for_redaction`` re-queues the
        # task (raising Retry) or, when waiting cannot help, re-raises — in which
        # case this is a terminal failure like any other.
        try:
            defer_for_redaction(self, not_ready)
        except Retry:
            raise
        except Exception as fatal:
            return _handle_task_error(fatal, file_uuid, file_id, task_id)
        raise  # unreachable — defer_for_redaction always raises
    except Exception as e:
        return _handle_task_error(e, file_uuid, file_id, task_id)
