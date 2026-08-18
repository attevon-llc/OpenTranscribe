"""Chat conversation retention (issue #52).

Chat threads quote transcript content back to the user, so an operator may want
them aged out on a schedule rather than kept indefinitely. The window is a
DB-backed setting (``chat.retention_days``) with a coded default of 0, meaning
"keep forever" — retention is opt-in, and this task is a no-op until an admin
turns it on.

Deletion goes through the ORM rather than a bulk SQL DELETE so the
``chat_message`` cascade and any future per-conversation cleanup run normally.
"""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.core.celery import celery_app
from app.core.constants import UtilityPriority

logger = logging.getLogger(__name__)

# Bound the work per tick: a first run after enabling retention on a busy
# deployment could otherwise try to delete an unbounded number of rows in one
# transaction. The task simply picks up the rest on the next tick.
MAX_DELETIONS_PER_RUN = 500


@celery_app.task(
    bind=True,
    name="chat.retention_sweep",
    priority=UtilityPriority.BACKGROUND,
)
def chat_retention_sweep(self) -> dict:  # noqa: ARG001 — bind=True signature
    """Delete conversations older than the configured retention window.

    Returns:
        Summary dict with the resolved window and how many conversations were
        deleted (``{"status": "disabled"}`` when retention is off).
    """
    from app.db.session_utils import session_scope
    from app.models.chat import ChatConversation
    from app.services.chat.settings import get_chat_settings

    with session_scope() as db:
        settings = get_chat_settings(db)
        if settings.retention_days <= 0:
            return {"status": "disabled", "retention_days": 0}

        cutoff = datetime.now(UTC) - timedelta(days=settings.retention_days)

        # Fall back to created_at for conversations that never received a reply.
        stale = (
            db.query(ChatConversation)
            .filter(
                ChatConversation.last_message_at.isnot(None),
                ChatConversation.last_message_at < cutoff,
            )
            .limit(MAX_DELETIONS_PER_RUN)
            .all()
        )
        empty = (
            db.query(ChatConversation)
            .filter(
                ChatConversation.last_message_at.is_(None),
                ChatConversation.created_at < cutoff,
            )
            .limit(max(0, MAX_DELETIONS_PER_RUN - len(stale)))
            .all()
        )

        deleted = 0
        for conversation in [*stale, *empty]:
            db.delete(conversation)
            deleted += 1

    if deleted:
        logger.info(
            "Chat retention: deleted %d conversation(s) older than %d day(s)",
            deleted,
            settings.retention_days,
        )
    return {
        "status": "ok",
        "retention_days": settings.retention_days,
        "deleted": deleted,
        "truncated": deleted >= MAX_DELETIONS_PER_RUN,
    }
