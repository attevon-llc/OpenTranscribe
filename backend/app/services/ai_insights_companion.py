"""Topic extraction that rides along with a manual summary request.

A file transcribed while no LLM provider existed never had topic extraction: the automatic
dispatch ran, ``extract_topics_task`` found no provider and skipped without writing a
``TopicSuggestion``. Adding a provider later and pressing "Generate summary" ran only the
summary, so the file stayed without AI tags and collections for good. This module is the one
decision for filling that gap from the summary endpoint (docs/design/llm_added_regenerate_plan.md).

It is an AUTOMATIC dispatch (the user asked for a summary, not for topics by name), so it
honours the owner's ``auto_topic_extraction`` switch through ``auto_llm_settings`` like every
other automatic site (issue #1148). It never re-runs an extraction that already produced a
suggestion, never stacks a second run on one in flight, and never touches speaker suggestions.
Tags and collections it leads to are applied by ``AutoLabelService.auto_apply_suggestions``,
which only adds; a user's own tags and collections are never removed or replaced.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from enum import StrEnum

from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.media import Task
from app.models.topic import TopicSuggestion
from app.services.auto_llm_settings import resolve_auto_llm_flags
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)

GUARD_KEY = "ai_insights:topic_companion:{file_id}"
# Long enough to cover a queued run on a busy NLP worker; a run that is skipped or fails
# leaves no suggestion behind, and the next summary request may try again after this.
GUARD_TTL_SECONDS = 900
# A topic-extraction Task row older than this that never finished belongs to a dead worker;
# it must not block the file forever.
ACTIVE_TASK_WINDOW = timedelta(hours=1)


class TopicCompanion(StrEnum):
    """What happened to topic extraction when a summary was requested (wire values)."""

    QUEUED = "queued"
    ALREADY_EXTRACTED = "already_extracted"
    IN_PROGRESS = "in_progress"
    DISABLED_BY_SETTING = "disabled_by_setting"
    UNAVAILABLE = "unavailable"


def _extraction_in_flight(db: Session, file_id: int) -> bool:
    cutoff = datetime.now(UTC) - ACTIVE_TASK_WINDOW
    row = (
        db.query(Task.id)
        .filter(
            Task.media_file_id == file_id,
            Task.task_type == "topic_extraction",
            Task.status.in_(("pending", "in_progress")),
            Task.created_at >= cutoff,
        )
        .first()
    )
    return row is not None


def _claim_guard(file_id: int) -> bool | None:
    """Take the per-file dispatch guard. ``None`` means the guard could not be consulted."""
    from app.core.redis import get_redis

    key = GUARD_KEY.format(file_id=file_id)
    try:
        return bool(get_redis().set(key, "1", nx=True, ex=GUARD_TTL_SECONDS))
    except Exception:
        # Fail closed: a duplicate dispatch is a paid LLM call, a skipped companion costs
        # nothing the user cannot redo with the summary or the topics button.
        logger.warning("Topic companion guard unavailable for file %s; not dispatching", file_id)
        return None


def _release_guard(file_id: int) -> None:
    from app.core.redis import get_redis

    try:
        get_redis().delete(GUARD_KEY.format(file_id=file_id))
    except Exception:
        logger.debug("Could not release topic companion guard for file %s", file_id)


def queue_topic_extraction_with_summary(
    db: Session, media_file: MediaFile, *, notify_user_id: int
) -> TopicCompanion:
    """Queue topic extraction for a file whose summary was just requested, when it is missing.

    The caller has already authorized the request (tenant, takedown and editor gates) and
    queued the summary. Every condition below is checked against the FILE OWNER, whose
    pipeline this completes and whose provider the task uses.

    Args:
        db: Request-scoped session (Postgres reads only).
        media_file: The file, as loaded by the permission check.
        notify_user_id: Recipient of the ``queued`` chip; the summary's own queued frame goes
            to the same user.

    Returns:
        The outcome, sent to the client so it can say what happened.
    """
    from app.tasks.topic_extraction import extract_topics_task
    from app.tasks.topic_extraction import send_topic_extraction_notification

    file_id = int(media_file.id)
    owner_id = int(media_file.user_id)

    if db.query(TopicSuggestion.id).filter(TopicSuggestion.media_file_id == file_id).first():
        return TopicCompanion.ALREADY_EXTRACTED
    if not resolve_auto_llm_flags(db, owner_id).topic_extraction:
        return TopicCompanion.DISABLED_BY_SETTING
    if not LLMService.is_configured_for_user(db, owner_id):
        # The task would resolve no provider and skip, leaving a queued chip that never ends.
        return TopicCompanion.UNAVAILABLE
    if _extraction_in_flight(db, file_id):
        return TopicCompanion.IN_PROGRESS

    claimed = _claim_guard(file_id)
    if claimed is None:
        return TopicCompanion.UNAVAILABLE
    if not claimed:
        return TopicCompanion.IN_PROGRESS

    try:
        # force_regenerate=False: a run that slipped past both guards still cannot replace a
        # suggestion another run has just stored.
        extract_topics_task.delay(file_uuid=str(media_file.uuid), force_regenerate=False)
    except Exception:
        logger.exception("Failed to queue topic extraction alongside summary for file %s", file_id)
        _release_guard(file_id)
        return TopicCompanion.UNAVAILABLE

    send_topic_extraction_notification(
        user_id=notify_user_id,
        file_id=file_id,
        status="queued",
        message="AI tag and collection suggestions have been queued",
    )
    logger.info("Queued topic extraction alongside the summary for file %s", file_id)
    return TopicCompanion.QUEUED
