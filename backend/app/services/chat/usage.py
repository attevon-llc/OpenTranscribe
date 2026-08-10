"""Record chat LLM usage — a core, open-source feature.

Usage accounting is **not** a billing concern here. Anyone paying an LLM bill
wants to see where it went, and a self-hosted operator running a metered provider
has exactly the same question a cloud tenant does: which conversations, which
models, how many tokens. So the recording, the storage and the per-user view all
live in core; only *enforcement* (quotas, tiers, invoicing) is a cloud concern,
layered on through ``core.tenant_limits`` and the chat hooks.

**Why ``usage_event`` and not ``chat_message``.** ``chat_message`` already carries
token counts and is the right place for the per-message detail a user sees in the
UI. It is the wrong place for the accounting record: ``tasks/chat_retention.py``
deletes conversations, and ``chat_message`` rows cascade with them, so a
deployment that turns on retention would silently destroy its own usage history.
``usage_event`` survives via ``ON DELETE SET NULL``.

**Idempotency.** One event per assistant message, keyed on the message UUID, so a
retried or replayed completion cannot double-count.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.services.chat.hooks import ChatCompletionContext

logger = logging.getLogger(__name__)

#: ``usage_event.event_type`` for a completed chat exchange.
EVENT_TYPE_CHAT_TOKENS = "chat.tokens"

#: ``quantity`` is recorded in tokens rather than currency. ``Numeric(12,3)`` gives
#: only $0.001 resolution, so a cheap message would round to zero; tokens are also
#: provider-neutral and need no repricing when a vendor changes its rates. Cost is
#: derived at read time from the model recorded in the event metadata.
UNIT_TOKENS = "tokens"


def record_chat_usage(ctx: ChatCompletionContext) -> None:
    """Persist one usage event for a finished chat exchange.

    Registered as a message-complete hook. Contained by contract: the hook runner
    already guards against exceptions, and ``record_event`` swallows its own
    failures, because usage accounting must never break the feature that emitted it.

    Opens its **own** session deliberately. ``record_event`` rolls back on a
    duplicate-key skip, which would discard any uncommitted work on a borrowed
    session — and this runs immediately after the reply is persisted.
    """
    from app.db.session_utils import session_scope
    from app.services.usage_service import record_event

    total = ctx.total_tokens or (ctx.prompt_tokens + ctx.completion_tokens)

    try:
        with session_scope() as db:
            record_event(
                db,
                event_type=EVENT_TYPE_CHAT_TOKENS,
                quantity=Decimal(total),
                unit=UNIT_TOKENS,
                user_id=ctx.user_id,
                organization_id=ctx.organization_id,
                idempotency_key=f"chat:{ctx.message_uuid}",
                metadata={
                    "conversation_uuid": ctx.conversation_uuid,
                    "provider": ctx.provider,
                    "model": ctx.model,
                    "prompt_tokens": ctx.prompt_tokens,
                    "completion_tokens": ctx.completion_tokens,
                    # Priced differently from ordinary input tokens, so kept distinct.
                    "cache_read_tokens": ctx.cache_read_tokens,
                    "cache_write_tokens": ctx.cache_write_tokens,
                    # Never price off an estimate — the chars/4 fallback is used when a
                    # provider reports no usage, and a reader must be able to tell the
                    # two apart rather than treating a guess as measured.
                    "tokens_estimated": ctx.tokens_estimated,
                    "retrieved_chunks": ctx.retrieved_chunks,
                    "use_context": ctx.use_context,
                    "success": ctx.success,
                },
            )
    except Exception:  # noqa: BLE001 — accounting never breaks chat
        logger.exception("Failed to record chat usage for message %s", ctx.message_uuid)


def register() -> None:
    """Install the recorder as a message-complete hook.

    Called from application startup in every edition. The cloud edition registers
    its own additional hook for billing; the two are independent, and this one is
    what gives an open-source deployment its usage view.
    """
    from app.services.chat.hooks import register_message_complete

    register_message_complete(record_chat_usage)
