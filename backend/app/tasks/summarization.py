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
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.core.celery import celery_app
from app.core.constants import NLPPriority
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.services.ingest_artifacts.service import source_fingerprint
from app.services.llm_service import LLMService
from app.services.redaction.llm_guard import RedactionNotReadyError
from app.services.redaction.llm_guard import defer_for_redaction
from app.services.redaction.llm_guard import resolve_llm_masking
from app.utils.transcript_builders import build_transcript_and_stats
from app.utils.transcript_builders import get_speaker_name
from app.utils.user_settings_helpers import get_user_llm_output_language

# Setup logging
logger = logging.getLogger(__name__)


def fingerprint_transcript_segments(transcript_segments) -> str:
    """The #464 staleness key: identical shape to ``file_facts.source_fingerprint``.

    ``ingest_artifacts.service.source_fingerprint`` hashes id/timings/resolved
    speaker/text over a list of plain dicts; this builds that same list from the
    ORM rows this task already loaded (ordered by ``start_time, end_time, id`` —
    the same total order ``ingest_artifacts.service.load_ordered_segments`` uses,
    which is what makes the two fingerprints comparable at all). Extracted to a
    top-level function so it is directly unit-testable without a database or a
    bound Celery task — the transformation is the thing that must stay in lockstep
    with ``load_ordered_segments``, not the query around it.

    Args:
        transcript_segments: ``TranscriptSegment`` rows (or any duck-typed
            stand-in exposing ``id``/``text``/``start_time``/``end_time``/
            ``speaker``), already ordered by ``(start_time, end_time, id)``.

    Returns:
        The SHA-256 hex digest ``chat/mapreduce.scope_digest_hits`` compares
        against ``file_facts.source_fingerprint`` to decide whether this file's
        LLM summary is fresh enough to trust in the map tier.
    """
    return source_fingerprint(
        [
            {
                "id": int(segment.id),
                "text": str(segment.text or ""),
                "start_time": float(segment.start_time or 0.0),
                "end_time": float(segment.end_time or 0.0),
                "speaker": get_speaker_name(segment),
            }
            for segment in transcript_segments
        ]
    )


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
    )


def send_summary_notification(
    user_id: int,
    file_id: int,
    status: str,
    message: str,
    progress: int = 0,
    summary_data: dict[str, Any] | str | None = None,
) -> bool:
    """Send summary status notification via WebSocket."""
    from app.services.notification_service import send_task_notification

    extra: dict[str, Any] = {}
    if status == "completed" and summary_data:
        extra["summary"] = summary_data

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


def _load_summarization_inputs(
    file_uuid: str, task_id: str, force_regenerate: bool
) -> dict[str, Any]:
    """Phase 1 — read everything the LLM phase needs, then release the session.

    Returns **plain data only**; no ORM instance escapes. An escaping instance
    would lazy-load during the provider call and silently reopen a transaction,
    reintroducing the very leak this split exists to remove.

    ``{"early_result": ...}`` means the task is finished (file has summaries
    disabled) and the caller should return it verbatim.

    Raises:
        ValueError: File or transcript missing, or the redaction policy could not
            be resolved (fail closed — never send an unmasked transcript).
        RedactionNotReadyError: Detection has not cached spans yet; the caller
            defers the task.
    """
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status
    from app.utils.uuid_helpers import get_file_by_uuid

    with session_scope() as db:
        media_file = get_file_by_uuid(db, file_uuid)
        if not media_file:
            raise ValueError(f"Media file with UUID {file_uuid} not found")

        file_id = int(media_file.id)
        user_id = int(media_file.user_id)
        filename = str(media_file.filename)

        create_task_record(db, task_id, user_id, file_id, "summarization")

        # Safety net: skip if file was marked disabled (per-upload flag
        # or system/user setting). Manual trigger endpoint resets status
        # before dispatching, so this only blocks zombie tasks.
        if str(media_file.summary_status) == "disabled":
            logger.info(f"Summary task skipped — file {file_id} has disabled status")
            update_task_status(db, task_id, "completed", progress=1.0, completed=True)
            return {
                "early_result": {
                    "status": "skipped",
                    "file_id": file_id,
                    "message": "Summary generation is disabled for this file",
                }
            }

        update_task_status(db, task_id, "in_progress", progress=0.1)

        if force_regenerate:
            logger.info(
                f"Force regenerate requested - clearing existing summaries for file {file_id}"
            )
            media_file.summary_data = None  # type: ignore[assignment]
            # Vestigial pointer into the retired transcript_summaries index (#67);
            # nothing sets it any more, so this only drains upgraded rows.
            media_file.summary_opensearch_id = None  # type: ignore[assignment]

        media_file.summary_status = "processing"  # type: ignore[assignment]
        db.commit()

        # ``joinedload`` rather than letting ``segment.speaker`` lazy-load per
        # row: the builders below read it for every segment.
        transcript_segments = (
            db.query(TranscriptSegment)
            .options(joinedload(TranscriptSegment.speaker))
            .filter(TranscriptSegment.media_file_id == file_id)
            .order_by(
                TranscriptSegment.start_time,
                TranscriptSegment.end_time,
                TranscriptSegment.id,
            )
            .all()
        )
        if not transcript_segments:
            raise ValueError(f"No transcript segments found for file {file_id}")

        # #464: the same "has anything changed?" fingerprint the digest tier
        # stores on `file_facts.source_fingerprint`, computed over the SAME
        # rows already fetched above — no extra query.
        source_fingerprint = fingerprint_transcript_segments(transcript_segments)

        # Redact PII/profanity before sending to the LLM provider when the
        # owner's (or admin-forced) policy requires it (don't leak to third parties).
        # Errors are NOT swallowed: an unresolvable policy or missing detection
        # spans must defer or abort, never fall through to sending raw text.
        try:
            redaction_cfg = resolve_llm_masking(db, media_file)
        except RedactionNotReadyError:
            # Detection hasn't cached spans yet, so masking would be a no-op.
            # The caller defers — ``defer_for_redaction`` needs the bound task and
            # must not run with this session open.
            raise
        except Exception as _redact_err:
            # FAIL CLOSED. Leaving redaction_cfg as None made mask_segment_text
            # take its "redaction disabled" early return, so the full unredacted
            # transcript was posted to an EXTERNAL LLM provider — defeating both
            # `redact_before_llm` and the admin `force_redact_before_llm` floor,
            # and it was logged at debug so the leak was silent. We cannot prove
            # redaction is unnecessary, so we do not send the transcript at all.
            logger.exception(
                "Could not resolve the redaction policy; aborting summarization rather "
                "than sending a possibly unredacted transcript to an external provider"
            )
            raise ValueError(
                "Redaction policy unavailable; summarization aborted to avoid "
                "sending unredacted content to an external provider"
            ) from _redact_err

        # Masking + formatting is pure CPU over rows already fetched, but it
        # reads ``segment.speaker``/``segment.words``, so it stays inside the
        # read scope. What leaves is a string and a dict.
        full_transcript, speaker_stats = build_transcript_and_stats(
            transcript_segments, redaction_cfg
        )

        update_task_status(db, task_id, "in_progress", progress=0.3)

        output_language = get_user_llm_output_language(db, user_id)
        organization_context = _get_organization_context(db, user_id)
        if organization_context:
            logger.info(f"Organization context loaded ({len(organization_context)} chars)")

    return {
        "file_id": file_id,
        "user_id": user_id,
        "filename": filename,
        "full_transcript": full_transcript,
        "speaker_stats": speaker_stats,
        "output_language": output_language,
        "organization_context": organization_context,
        "source_fingerprint": source_fingerprint,
    }


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
) -> None:
    """Phase 3 — write (short session, Postgres only).

    ``summary_data`` is the whole summary and the only copy of it (#67).
    """
    from app.utils.task_utils import update_task_status

    with session_scope() as db:
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if media_file is None:
            raise ValueError(f"Media file {file_id} disappeared during summarization")

        media_file.summary_data = summary_data  # type: ignore[assignment]
        media_file.summary_schema_version = 1  # type: ignore[assignment]
        media_file.summary_status = "completed"  # type: ignore[assignment]

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

        update_task_status(db, task_id, "completed", progress=1.0, completed=True)


def _handle_task_error(
    e: Exception, file_uuid: str, file_id: int | None, task_id: str
) -> dict[str, Any]:
    """Handle task-level errors in a short session of its own."""
    from app.utils.task_utils import update_task_status
    from app.utils.uuid_helpers import get_file_by_uuid

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
        _persist_summary(file_id, user_id, task_id, summary_data, prompt_uuid)

        _send_completion_notification(
            user_id, file_id, summary_data, "AI summary generation completed successfully"
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
