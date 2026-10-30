"""Persist, meter and audit one finished (or torn-down) chat turn.

Runs from ``stream_reply``'s shielded ``finally``, so every step is individually
contained: a failure here must never mask the turn's real outcome. The audit record
carries metadata only, never message content.

``_finalize_turn`` is called through ``service.py``'s module globals (from
``ChatService.stream_reply``), which is where tests patch it.
"""

from __future__ import annotations

import logging
import time

from starlette.concurrency import run_in_threadpool

from app.models.chat import ROLE_ASSISTANT
from app.services.chat import citations as citations_mod
from app.services.chat import limits
from app.services.chat.hooks import ChatCompletionContext
from app.services.chat.hooks import fire_message_complete
from app.services.chat.turn import ChatTurn

logger = logging.getLogger(__name__)


TITLE_MAX_CHARS = 60


def _title_from(question: str) -> str:
    """Derive a conversation title from its opening question."""
    clean = " ".join(question.split())
    if len(clean) <= TITLE_MAX_CHARS:
        return clean
    cut = clean[:TITLE_MAX_CHARS]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


async def _finalize_turn(
    *,
    turn: ChatTurn,
    llm,
    messages: list[dict[str, str]],
    masked_count: int,
    conversation_id: int,
    conversation_uuid: str,
    assistant_message_uuid: str,
    user_id: int,
    organization_id: int | None,
    is_first_exchange: bool,
    question: str,
    started: float,
    use_context: bool,
) -> None:
    """Persist, meter and audit one turn. Safe to call during teardown.

    Called from the streaming generator's ``finally`` so a cancelled or
    disconnected stream still records what it produced. Every step is
    individually contained: a failure here must never propagate into the
    teardown path and mask the original exception.
    """
    # Token fallback for providers that report nothing (notably "custom"
    # OpenAI-clones, which can't be sent stream_options.include_usage).
    if turn.prompt_tokens is None and turn.completion_tokens is None:
        try:
            turn.prompt_tokens = llm.estimate_tokens("".join(m["content"] for m in messages))
            # Reasoning counts, even though it is not the answer (issue #439). The
            # model generated and was billed for those tokens; separating them out
            # of `answer` so they stop rendering must not also delete them from the
            # meter. Providers that report usage already include them.
            turn.completion_tokens = llm.estimate_tokens(turn.answer + turn.reasoning)
            turn.tokens_estimated = True
        except Exception:  # noqa: BLE001
            logger.exception("Token estimation failed")

    # #403: fold in the planner/enrichment/rewrite-extension tokens
    # AFTER the fallback estimate above (which only fires when the provider
    # reported nothing) and BEFORE `total_tokens` — this is the one place
    # every path (real usage or estimated) converges before persistence and
    # the `record_chat_usage` hook, so those bounded calls are metered
    # through the exact same accounting the answer stream already uses.
    if turn.extra_prompt_tokens or turn.extra_completion_tokens:
        turn.prompt_tokens = (turn.prompt_tokens or 0) + turn.extra_prompt_tokens
        turn.completion_tokens = (turn.completion_tokens or 0) + turn.extra_completion_tokens
        turn.tokens_estimated = True

    turn.total_tokens = (turn.prompt_tokens or 0) + (turn.completion_tokens or 0)
    turn.metadata.setdefault("timings_ms", {})["total"] = int((time.monotonic() - started) * 1000)
    used_citations = citations_mod.extract_used_citations(turn.answer, turn.offered_citations)

    try:
        turn.title = await run_in_threadpool(
            _persist_reply,
            conversation_id,
            assistant_message_uuid,
            turn,
            used_citations,
            turn.total_tokens,
            llm,
            is_first_exchange,
            question,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist chat reply %s", assistant_message_uuid)

    limits.clear_cancel(assistant_message_uuid)

    try:
        fire_message_complete(
            ChatCompletionContext(
                conversation_uuid=conversation_uuid,
                message_uuid=assistant_message_uuid,
                user_id=user_id,
                organization_id=organization_id,
                provider=str(llm.config.provider.value),
                model=str(llm.config.model),
                prompt_tokens=turn.prompt_tokens or 0,
                completion_tokens=turn.completion_tokens or 0,
                total_tokens=turn.total_tokens,
                tokens_estimated=turn.tokens_estimated,
                retrieved_chunks=masked_count,
                success=not turn.error,
                cache_read_tokens=turn.cache_read_tokens or 0,
                cache_write_tokens=turn.cache_write_tokens or 0,
                use_context=use_context,
            )
        )
    except Exception:  # noqa: BLE001 — hooks are contained by contract
        logger.exception("Chat completion hook raised past its guard")

    _audit_message(
        user_id=user_id,
        organization_id=organization_id,
        conversation_uuid=conversation_uuid,
        message_uuid=assistant_message_uuid,
        llm=llm,
        turn=turn,
        chunk_count=masked_count,
        total_tokens=turn.total_tokens,
    )


def _persist_reply(
    conversation_id: int,
    assistant_message_uuid: str,
    turn: ChatTurn,
    used_citations: list[dict],
    total_tokens: int,
    llm,
    is_first_exchange: bool,
    question: str,
) -> str | None:
    """Write the assistant message and bump the conversation. Returns a new title, if set.

    A conversation can legitimately vanish while a turn is still streaming — the
    same user deleting it from another tab, or (in tests) a teardown fixture
    deleting it the moment the test that created it finishes, without waiting
    for a still-in-flight SSE stream to settle. Neither is a bug in the
    persistence path; a stream that outlives the conversation it writes to is
    an ordinary consequence of streaming being concurrent with everything else
    a user can do. So this treats "conversation is gone" as an expected abort,
    not an error: it checks first (the common case, avoiding a doomed INSERT
    and its full-transaction rollback entirely) and still degrades gracefully
    if the conversation is deleted in the narrow window between that check and
    commit (the `IntegrityError` safety net below) — never let either surface
    as the unhandled `chat_message_conversation_id_fkey` ForeignKeyViolation
    the caller would otherwise log as an unexplained error.
    """
    import uuid as uuid_pkg
    from datetime import UTC
    from datetime import datetime

    from sqlalchemy.exc import IntegrityError

    from app.db.session_utils import session_scope
    from app.models.chat import ChatConversation
    from app.models.chat import ChatMessage

    title: str | None = None
    try:
        with session_scope() as db:
            conversation = (
                db.query(ChatConversation).filter(ChatConversation.id == conversation_id).first()
            )
            if conversation is None:
                logger.info(
                    "Abandoning persistence of chat reply %s: conversation %s no longer "
                    "exists (deleted while the turn was streaming)",
                    assistant_message_uuid,
                    conversation_id,
                )
                return None

            message = ChatMessage(
                uuid=uuid_pkg.UUID(assistant_message_uuid),
                conversation_id=conversation_id,
                role=ROLE_ASSISTANT,
                content=turn.answer,
                reasoning_content=turn.reasoning or None,
                citations=used_citations or None,
                msg_metadata=turn.metadata or None,
                prompt_tokens=turn.prompt_tokens,
                completion_tokens=turn.completion_tokens,
                total_tokens=total_tokens,
                tokens_estimated=turn.tokens_estimated,
                provider=str(llm.config.provider.value),
                model=str(llm.config.model),
                status=turn.status(),
                error=turn.error,
            )
            db.add(message)

            conversation.last_message_at = datetime.now(UTC)
            if is_first_exchange and not conversation.title:
                title = _title_from(question)
                conversation.title = title
    except IntegrityError as exc:
        # The existence check above closes most of the window, but not a delete
        # landing between that SELECT and this transaction's commit. Narrowed to
        # IntegrityError specifically (not a bare except) so a genuinely
        # unexpected persistence failure still surfaces loudly to the caller.
        logger.info(
            "Abandoning persistence of chat reply %s: conversation %s was deleted mid-persist (%s)",
            assistant_message_uuid,
            conversation_id,
            exc.__class__.__name__,
        )
        return None
    return title


def _audit_message(
    *,
    user_id: int,
    organization_id: int | None,
    conversation_uuid: str,
    message_uuid: str,
    llm,
    turn: ChatTurn,
    chunk_count: int,
    total_tokens: int,
) -> None:
    """Record the exchange on the compliance trail — metadata only, never content."""
    try:
        from app.auth.audit import audit_logger
        from app.auth.audit_events import AuditEventType
        from app.auth.audit_events import AuditOutcome

        audit_logger.log(
            event_type=AuditEventType.CHAT_MESSAGE_SEND,
            outcome=AuditOutcome.FAILURE if turn.error else AuditOutcome.SUCCESS,
            user_id=user_id,
            organization_id=organization_id,
            error_code=turn.error_code,
            details={
                "conversation_uuid": conversation_uuid,
                "message_uuid": message_uuid,
                "provider": str(llm.config.provider.value),
                "model": str(llm.config.model),
                "retrieved_chunks": chunk_count,
                "total_tokens": total_tokens,
                "tokens_estimated": turn.tokens_estimated,
                "finish_reason": turn.finish_reason,
            },
        )
    except Exception:  # noqa: BLE001 — auditing must not break delivery
        logger.exception("Chat audit write failed")
