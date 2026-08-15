"""Chat turn orchestration: retrieval → prompt → stream → persist.

Emits the SSE frames the frontend consumes (``start``, ``status``, ``sources``,
``warning``, ``delta``, ``reasoning``, ``usage``, ``done``, ``error``). The event
names are a frozen contract shared with the frontend implementation.
``reasoning`` is shaped identically to ``delta`` (``{"text": ...}``) but carries a
model's separately streamed reasoning/thinking content, rendered by the frontend
in its own collapsed-by-default block rather than mixed into the answer.

``sources`` is emitted only once the excerpt budget is known, and lists only the
excerpts that actually reached the prompt — see the comment at its yield site.

Threading model: everything except the SSE plumbing is synchronous (OpenSearch,
SQLAlchemy, ``requests``), so the blocking stages run via Starlette's threadpool
and the provider's token stream is bridged with ``iterate_in_threadpool``.

The generator owns its own DB session rather than borrowing the request's: it
outlives the handler's dependency scope, and a stream that is still writing when
the session closes would fail at exactly the moment we most need to persist the
partial answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncGenerator
from typing import Any

import anyio
from starlette.concurrency import iterate_in_threadpool
from starlette.concurrency import run_in_threadpool

from app.core import constants as C  # noqa: N812
from app.models.chat import ROLE_ASSISTANT
from app.models.chat import STATUS_CANCELLED
from app.models.chat import STATUS_COMPLETE
from app.models.chat import STATUS_ERROR
from app.services.chat import citations as citations_mod
from app.services.chat import limits
from app.services.chat.hooks import ChatCompletionContext
from app.services.chat.hooks import fire_message_complete
from app.services.chat.language import METADATA_KEY as LANGUAGE_METADATA_KEY
from app.services.chat.language import WARNING_CODE as LANGUAGE_WARNING_CODE
from app.services.chat.language import describe_context_languages
from app.services.chat.language import warning_payload as language_warning_payload
from app.services.chat.prompting import build_messages
from app.services.chat.redactor import MaskedChunk
from app.services.chat.redactor import mask_chunks
from app.services.chat.retrieval import retrieve_context
from app.services.chat.settings import ChatSettings

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 60

# Floor for a reply budget. Small enough to honour "keep answers short", large
# enough that a clamped request still returns a usable answer rather than a
# sentence cut in half.
MIN_ANSWER_TOKENS = 256


def sse(event: str, payload: dict[str, Any]) -> str:
    """Format one SSE frame (same helper shape as the subtitle export stream)."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def resolve_answer_tokens(
    *,
    requested: int | None,
    tenant_ceiling: int | None,
    default_tokens: int,
    context_window: int,
) -> int:
    """Decide how many tokens the reply may use.

    Three limits apply, in order of authority: a per-tenant cap is a hard
    ceiling the user cannot raise; the context window is a physical one; and the
    user's own preference sits underneath both. Clamping rather than rejecting
    is deliberate — providers disagree about oversized ``max_tokens`` (some 400,
    some silently truncate), and a chat should not fail because someone typed an
    ambitious number.

    The window cap is half the context, not all of it: the prompt and history
    have to fit in the same budget, and ``build_messages`` sizes the excerpt
    block against whatever this returns.

    Args:
        requested: The conversation's override, or None to use the default.
        tenant_ceiling: Per-tenant maximum, or None when no tier cap applies.
        default_tokens: The LLM config's own derived reply budget.
        context_window: The model's total context window in tokens.

    Returns:
        A positive token count safe to send as ``max_tokens``.
    """
    tokens = requested if requested is not None else default_tokens
    if tenant_ceiling is not None:
        tokens = min(tokens, tenant_ceiling)
    window_cap = max(MIN_ANSWER_TOKENS, context_window // 2)
    return max(MIN_ANSWER_TOKENS, min(tokens, window_cap))


def _title_from(question: str) -> str:
    """Derive a conversation title from its opening question."""
    clean = " ".join(question.split())
    if len(clean) <= TITLE_MAX_CHARS:
        return clean
    cut = clean[:TITLE_MAX_CHARS]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


class ChatTurn:
    """One user message and the assistant reply it produces.

    Holds the mutable state a turn accumulates (answer text, token counts,
    diagnostics) so the streaming generator stays readable and the ``finally``
    block has one place to persist from.
    """

    def __init__(self) -> None:
        self.answer_parts: list[str] = []
        # Separate from answer_parts so the collapsible reasoning block in the UI
        # never gets reasoning tokens mixed into the rendered answer.
        self.reasoning_parts: list[str] = []
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        # Cache tokens are priced differently from ordinary input tokens — reads far
        # below, writes above — so they are carried separately rather than folded in.
        self.cache_read_tokens: int | None = None
        self.cache_write_tokens: int | None = None
        self.tokens_estimated = False
        self.finish_reason: str | None = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.offered_citations: list[dict] = []
        self.metadata: dict[str, Any] = {}
        # Filled in by _finalize_turn so the trailing frames can read them.
        self.total_tokens = 0
        self.title: str | None = None

    @property
    def answer(self) -> str:
        return "".join(self.answer_parts)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts)

    def status(self) -> str:
        if self.error:
            return STATUS_ERROR
        if self.finish_reason == "cancelled":
            return STATUS_CANCELLED
        return STATUS_COMPLETE


def _prepare_context(
    db,
    *,
    user_id: int,
    organization_id: int | None,
    question: str,
    history: list[dict[str, str]],
    settings: ChatSettings,
    file_uuids: list[str] | None,
    speakers: list[str] | None,
    search_mode: str,
    llm,
    rewrite_enabled: bool,
) -> tuple[list[MaskedChunk], dict[str, Any]]:
    """Run the blocking RAG stages: rewrite → retrieve → mask.

    Executed in a worker thread; returns the masked chunks plus diagnostics for
    the message metadata (ids/counts/timings only, never text).
    """
    meta: dict[str, Any] = {}

    effective_query = question
    if rewrite_enabled and history:
        from app.services.chat.query_rewriter import rewrite_query

        rewrite_started = time.monotonic()
        effective_query = rewrite_query(llm, history, question)
        if effective_query != question:
            meta["rewritten_query"] = effective_query
        meta.setdefault("timings_ms", {})["rewrite"] = int(
            (time.monotonic() - rewrite_started) * 1000
        )

    result = retrieve_context(
        query=effective_query,
        user_id=user_id,
        organization_id=organization_id,
        file_uuids=file_uuids,
        speakers=speakers,
        settings=settings,
        search_mode=search_mode,
    )

    masked = mask_chunks(db, result.chunks, user_id)
    masked = [chunk for chunk in masked if chunk.content.strip()]

    # RAG is English-only (see services/chat/language.py). Recording what the turn
    # could draw on is what makes that limit visible instead of silent.
    languages = describe_context_languages(
        db,
        scope_file_uuids=file_uuids,
        grounded_file_uuids=[chunk.source.file_uuid for chunk in masked],
    )
    if languages.total_files:
        meta[LANGUAGE_METADATA_KEY] = languages.as_metadata()
    if languages.has_unsupported:
        meta[LANGUAGE_WARNING_CODE] = True

    meta["retrieved"] = result.retrieved
    meta["reranked"] = result.reranked
    meta["cache_hit"] = result.cache_hit
    meta["files_searched"] = "all" if file_uuids is None else len(file_uuids)
    if speakers:
        meta["speakers_filtered"] = list(speakers)
    timings = meta.setdefault("timings_ms", {})
    timings.update(result.timings_ms)
    return masked, meta


# SSE comment lines are ignored by every client but keep the connection warm.
# Without them, retrieval (OpenSearch + cross-encoder + masking) and then the
# wait for a first token can put ZERO bytes on the wire for well over nginx's
# default 60s proxy_read_timeout, and the proxy closes the stream mid-answer.
_KEEPALIVE = ": keepalive\n\n"
_KEEPALIVE_INTERVAL_S = 15


async def _with_keepalive(awaitable, queue: asyncio.Queue):
    """Await ``awaitable``, pushing a keepalive frame into ``queue`` every 15s."""
    task = asyncio.ensure_future(awaitable)
    while True:
        done, _ = await asyncio.wait({task}, timeout=_KEEPALIVE_INTERVAL_S)
        if done:
            return task.result()
        queue.put_nowait(_KEEPALIVE)


async def _drain(queue: asyncio.Queue):
    """Yield everything buffered in ``queue`` without blocking."""
    while not queue.empty():
        yield queue.get_nowait()


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
            turn.completion_tokens = llm.estimate_tokens(turn.answer)
            turn.tokens_estimated = True
        except Exception:  # noqa: BLE001
            logger.exception("Token estimation failed")

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


class ChatService:
    """Streams one assistant reply and persists both sides of the exchange."""

    @staticmethod
    async def stream_reply(  # noqa: C901 — a linear pipeline reads better whole
        *,
        conversation_id: int,
        conversation_uuid: str,
        user_id: int,
        organization_id: int | None,
        question: str,
        history: list[dict[str, str]],
        file_uuids: list[str] | None,
        speakers: list[str] | None,
        settings: ChatSettings,
        use_context: bool,
        system_prompt: str,
        search_mode: str,
        temperature: float | None,
        max_tokens: int | None,
        top_p: float | None,
        llm,
        on_teardown=None,
        assistant_message_uuid: str,
        user_message_uuid: str,
        is_first_exchange: bool,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE frames for one turn.

        Args:
            conversation_id: Owning conversation's primary key.
            conversation_uuid: Public id echoed in the ``start`` frame.
            user_id: Message author (also the redaction-policy subject).
            organization_id: Tenant stamp for metering and audit.
            question: The user's message (already persisted by the endpoint).
            history: Prior turns, oldest first.
            file_uuids: Resolved scope; None = all accessible transcripts.
            speakers: Restrict retrieval to these speakers' turns.
            settings: Admin-tuned RAG knobs.
            use_context: False skips retrieval entirely (pure-LLM chat).
            system_prompt: Fully layered system prompt.
            search_mode: Retrieval mode for this turn.
            temperature: Per-conversation override, or None for the config default.
            max_tokens: Per-conversation answer-length override, clamped below.
            top_p: Per-conversation nucleus-sampling override, omitted when None.
            on_teardown: Called exactly once inside the shielded finally, however
                the turn ends. Used to release the concurrency slot: a wrapping
                generator's own ``finally`` does NOT reliably run when Starlette
                tears this one down on client disconnect, so a slot released
                there leaks on every Stop.
            llm: The caller's ``LLMService``.
            assistant_message_uuid: Pre-allocated id for the reply.
            user_message_uuid: Id of the already-persisted user message.
            is_first_exchange: Whether to derive a title from this question.

        Yields:
            SSE frame strings.
        """
        from app.db.session_utils import session_scope

        turn = ChatTurn()
        keepalive_q: asyncio.Queue = asyncio.Queue()
        cancel_event = threading.Event()
        started = time.monotonic()

        yield sse(
            "start",
            {
                "conversation_uuid": conversation_uuid,
                "user_message_uuid": user_message_uuid,
                "assistant_message_uuid": assistant_message_uuid,
            },
        )

        masked: list[MaskedChunk] = []
        messages: list[dict[str, str]] = []
        reached_end = False
        try:
            if use_context:
                # Query rewriting is a separate LLM round trip, so it is worth
                # its own stage. Retrieval and reranking happen inside one
                # threadpool call and are reported together as "retrieving" —
                # the frontend renders every pre-generation stage with the same
                # label anyway, so splitting them further would add machinery
                # for no visible difference.
                will_rewrite = settings.query_rewrite_enabled and bool(history)
                yield sse("status", {"stage": "rewriting" if will_rewrite else "retrieving"})

                def _prep():
                    with session_scope() as db:
                        return _prepare_context(
                            db,
                            user_id=user_id,
                            organization_id=organization_id,
                            question=question,
                            history=history,
                            settings=settings,
                            file_uuids=file_uuids,
                            speakers=speakers,
                            search_mode=search_mode,
                            llm=llm,
                            rewrite_enabled=settings.query_rewrite_enabled,
                        )

                masked, meta = await _with_keepalive(run_in_threadpool(_prep), keepalive_q)
                async for frame in _drain(keepalive_q):
                    yield frame
                turn.metadata.update(meta)

            # Resolve the answer budget BEFORE building the prompt: build_messages
            # reserves context for the reply, so raising max_tokens after the fact
            # would let prompt + answer overrun the window.
            answer_tokens = resolve_answer_tokens(
                requested=max_tokens,
                tenant_ceiling=settings.max_output_tokens,
                default_tokens=llm.response_tokens,
                context_window=llm.user_context_window,
            )

            # The prompt is assembled BEFORE the `sources` frame goes out, and the
            # frame carries only the excerpts that survived the budget. Emitting
            # it earlier meant the UI could render clickable citations for
            # excerpts the model was never given — an answer that looks sourced
            # but is not grounded in the cited material (issue #384).
            messages, excerpt_ids = build_messages(
                system_prompt=system_prompt,
                chunks=masked,
                history=history,
                question=question,
                context_window=llm.user_context_window,
                response_tokens=answer_tokens,
                max_history_turns=settings.history_max_turns,
            )
            chunks_used = len(excerpt_ids)
            turn.metadata["chunks_used"] = chunks_used

            if use_context:
                turn.offered_citations = citations_mod.build_offered_citations(masked, excerpt_ids)
                yield sse("sources", {"citations": turn.offered_citations})

                if masked and not excerpt_ids:
                    # Retrieval found material and the budget left no room for any
                    # of it. Say so rather than answering ungrounded behind a
                    # normal-looking reply.
                    turn.metadata["context_dropped"] = True
                    logger.warning(
                        "Chat turn %s dropped all %d retrieved excerpts: no room in "
                        "the %d-token context window after %d reserved answer tokens",
                        assistant_message_uuid,
                        len(masked),
                        llm.user_context_window,
                        answer_tokens,
                    )
                    yield sse(
                        "warning",
                        {"code": "context_dropped", "retrieved": len(masked)},
                    )

                # Answering anyway is deliberate: a library is usually mixed, and
                # refusing every question because one recording is Spanish would
                # be worse than useless. The turn says what it could not serve
                # well and answers from the rest.
                language_warning = language_warning_payload(turn.metadata)
                if language_warning is not None:
                    logger.info(
                        "Chat turn %s spans unsupported RAG languages %s "
                        "(%d files, %d of unknown language)",
                        assistant_message_uuid,
                        language_warning["languages"],
                        language_warning["files"],
                        language_warning["unknown_files"],
                    )
                    yield sse("warning", language_warning)

            yield sse("status", {"stage": "generating"})

            kwargs: dict[str, Any] = {"max_tokens": answer_tokens}
            if temperature is not None:
                kwargs["temperature"] = temperature
            # Omitted entirely when unset: not every provider accepts top_p, and
            # sending a "default" would override a provider-side tuned value.
            if top_p is not None:
                kwargs["top_p"] = top_p

            first_token_deadline = time.monotonic() + C.DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S
            got_first_token = False

            stream = llm.chat_completion_stream(messages, cancel_event=cancel_event, **kwargs)
            last_beat = time.monotonic()
            async for event in iterate_in_threadpool(stream):
                # Long gaps between tokens (or before the first) must not look
                # like a dead connection to an intermediary.
                if time.monotonic() - last_beat >= _KEEPALIVE_INTERVAL_S:
                    last_beat = time.monotonic()
                    yield _KEEPALIVE
                # A user Stop may arrive over a separate connection.
                if not cancel_event.is_set() and limits.is_cancelled(assistant_message_uuid):
                    cancel_event.set()

                if event.type == "delta":
                    if not got_first_token:
                        got_first_token = True
                        turn.metadata.setdefault("timings_ms", {})["first_token"] = int(
                            (time.monotonic() - started) * 1000
                        )
                    turn.answer_parts.append(event.text)
                    yield sse("delta", {"text": event.text})
                elif event.type == "reasoning":
                    # The model IS actively responding once reasoning starts, so this
                    # satisfies the first-token watchdog exactly like an answer delta —
                    # otherwise a reasoning-heavy response looks "stalled" and gets
                    # killed by the timeout below while tokens are visibly arriving.
                    if not got_first_token:
                        got_first_token = True
                        turn.metadata.setdefault("timings_ms", {})["first_token"] = int(
                            (time.monotonic() - started) * 1000
                        )
                    turn.reasoning_parts.append(event.text)
                    yield sse("reasoning", {"text": event.text})
                elif event.type == "usage":
                    turn.prompt_tokens = event.prompt_tokens
                    turn.completion_tokens = event.completion_tokens
                    turn.cache_read_tokens = event.cache_read_tokens
                    turn.cache_write_tokens = event.cache_write_tokens
                elif event.type == "error":
                    turn.error = event.message
                    turn.error_code = "provider_error"
                    yield sse("error", {"code": "provider_error", "message": event.message})
                elif event.type == "done":
                    turn.finish_reason = event.finish_reason

                if not got_first_token and time.monotonic() > first_token_deadline:
                    cancel_event.set()
                    turn.error = "The model did not start responding in time."
                    turn.error_code = "timeout"
                    yield sse("error", {"code": "timeout", "message": turn.error})
                    break

        except Exception as exc:  # noqa: BLE001 — surface as a frame, never a 500
            logger.exception("Chat stream failed for conversation %s", conversation_uuid)
            turn.error = str(exc)
            turn.error_code = "provider_error"
            yield sse("error", {"code": "provider_error", "message": "Generation failed."})
        else:
            reached_end = True
        finally:
            cancel_event.set()

            # Persistence, metering and audit MUST run here, not after the try.
            #
            # A client disconnect (closed tab, navigation, or the Stop button —
            # which aborts the fetch before its cancel POST lands) makes Starlette
            # cancel this task, delivering GeneratorExit/CancelledError at a
            # `yield`. Both are BaseException, so `except Exception` above does not
            # catch them and any code placed AFTER the try block would simply never
            # run — silently losing the partial answer, skipping the cloud metering
            # hook (letting a user consume provider tokens unbilled by aborting
            # just before completion), and dropping the audit record.
            #
            # The cancel scope is shielded so the DB write survives the very
            # cancellation that triggered it. It contains no `yield`, which is what
            # makes awaiting here legal during generator teardown.
            if not reached_end and not turn.error:
                # Torn down mid-stream: record it as the cancellation it is.
                turn.finish_reason = "cancelled"

            with anyio.CancelScope(shield=True):
                try:
                    await _finalize_turn(
                        turn=turn,
                        llm=llm,
                        messages=messages,
                        masked_count=len(masked),
                        conversation_id=conversation_id,
                        conversation_uuid=conversation_uuid,
                        assistant_message_uuid=assistant_message_uuid,
                        user_id=user_id,
                        organization_id=organization_id,
                        is_first_exchange=is_first_exchange,
                        question=question,
                        started=started,
                        use_context=use_context,
                    )
                finally:
                    # Its OWN finally: finalisation can raise (the conversation may
                    # have been deleted underneath a disconnected stream), and a
                    # release placed after it would then never run — leaking the
                    # slot exactly in the case this hook exists to cover.
                    if on_teardown is not None:
                        try:
                            on_teardown()
                        except Exception:  # noqa: BLE001 — never mask the real outcome
                            logger.exception("Chat stream teardown hook failed")

        # Only reachable on a normal (non-cancelled) exit — there is no client
        # left to receive these frames otherwise.
        if not turn.error:
            yield sse(
                "usage",
                {
                    "prompt_tokens": turn.prompt_tokens or 0,
                    "completion_tokens": turn.completion_tokens or 0,
                    "total_tokens": turn.total_tokens,
                    "estimated": turn.tokens_estimated,
                },
            )

        done_payload: dict[str, Any] = {"finish_reason": turn.finish_reason or "stop"}
        if turn.title:
            done_payload["title"] = turn.title
        yield sse("done", done_payload)


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
    """Write the assistant message and bump the conversation. Returns a new title, if set."""
    import uuid as uuid_pkg
    from datetime import UTC
    from datetime import datetime

    from app.db.session_utils import session_scope
    from app.models.chat import ChatConversation
    from app.models.chat import ChatMessage

    title: str | None = None
    with session_scope() as db:
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

        conversation = (
            db.query(ChatConversation).filter(ChatConversation.id == conversation_id).first()
        )
        if conversation is not None:
            conversation.last_message_at = datetime.now(UTC)
            if is_first_exchange and not conversation.title:
                title = _title_from(question)
                conversation.title = title
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
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditOutcome
        from app.auth.audit import audit_logger

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
