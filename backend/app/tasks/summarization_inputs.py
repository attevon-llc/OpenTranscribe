"""Phase-1 input loading for the LLM summarization task.

Split out of ``summarization.py`` so the DB-only read phase (transcript + settings +
organization context + the #464 staleness fingerprint) lives apart from the LLM call,
persistence and notification code. The session-lifetime rule is unchanged: the session
opened here closes before the provider round trip and only plain data leaves.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.db.session_utils import session_scope
from app.models.media import TranscriptSegment
from app.services.ingest_artifacts.service import source_fingerprint
from app.services.privacy.gates import voiceprints_allowed
from app.services.redaction.llm_guard import RedactionNotReadyError
from app.services.redaction.llm_guard import resolve_llm_masking
from app.utils.transcript_builders import build_transcript_and_stats
from app.utils.transcript_builders import get_speaker_name
from app.utils.user_settings_helpers import get_user_llm_output_language

logger = logging.getLogger(__name__)


def fingerprint_transcript_segments(transcript_segments, *, voiceprint_names: bool) -> str:
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
        voiceprint_names: The file's voiceprint decision. It is part of the key on purpose:
            a switch flip changes the speaker labels the summary was written under, so the
            summary goes stale (#1280).

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
                "speaker": get_speaker_name(segment, voiceprint_names=voiceprint_names),
            }
            for segment in transcript_segments
        ]
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
    from app.utils.tenant_sharing import owner_shares_tenant_with

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
        # Verify the shared context is still shared, within a tenant both users are
        # in: the stored pointer outlives membership changes.
        is_still_shared = (
            db.query(models.UserSetting)
            .filter(
                models.UserSetting.user_id == shared_from_id,
                models.UserSetting.setting_key == "org_context_is_shared",
                models.UserSetting.setting_value == "true",
                owner_shares_tenant_with(models.UserSetting.user_id, user_id),
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
    from app.services.uuid_helpers import get_file_by_uuid
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status

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

        if force_regenerate:
            # Prune the summary plane now, in case regeneration below fails
            # before reaching `_persist_summary` — the happy path re-runs this
            # anyway (harmless, deterministic ids overwrite) once the new
            # summary is persisted (issue #963).
            from app.tasks.search_file_field_tasks import index_file_summary

            index_file_summary.delay(file_id)

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
        # Decided once for the file (unreadable = hidden) and used for every label the
        # prompt, the stats and the fingerprint carry.
        voiceprint_names = voiceprints_allowed(file_id, db)
        source_fingerprint = fingerprint_transcript_segments(
            transcript_segments, voiceprint_names=voiceprint_names
        )

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
            transcript_segments, redaction_cfg, voiceprint_names=voiceprint_names
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
