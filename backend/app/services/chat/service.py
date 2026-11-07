"""Chat turn orchestration: retrieval → prompt → stream → persist.

Emits the SSE frames the frontend consumes (``start``, ``status``, ``sources``,
``warning``, ``delta``, ``reasoning``, ``usage``, ``done``, ``error``). The event
names are a frozen contract shared with the frontend implementation.
``reasoning`` is shaped identically to ``delta`` (``{"text": ...}``) but carries a
model's separately streamed reasoning/thinking content, rendered by the frontend
in its own collapsed-by-default block rather than mixed into the answer.

``sources`` is emitted only once the excerpt budget is known, and lists only the
excerpts that actually reached the prompt — see ``frames._grounding_frames``.

``warning`` carries a ``code`` naming exactly one way the answer ended up
ungrounded: ``context_dropped`` (excerpts were retrieved and the budget fit none
of them), ``no_context`` (nothing reached masking at all and the search itself
was not the cause — retrieval genuinely matched nothing, or every chunk failed
closed under masking), or ``retrieval_failed`` (the chunk-plane search itself
raised or had no client, so retrieval degraded to empty rather than reporting an
empty library — issue #438's open half, now closed: see
``search/chunk_retrieval.retrieve_chunks``'s ``diagnostics`` out-param). Adding a
code means teaching ``frontend/src/lib/types/chat.ts``'s ``ChatWarningCode`` about
it too.

Threading model: everything except the SSE plumbing is synchronous (OpenSearch,
SQLAlchemy, ``requests``), so the blocking stages run via Starlette's threadpool
and the provider's token stream is bridged with ``iterate_in_threadpool``.

The generator owns its own DB session rather than borrowing the request's: it
outlives the handler's dependency scope, and a stream that is still writing when
the session closes would fail at exactly the moment we most need to persist the
partial answer.

Module map. This file keeps the SSE generator and every streaming call site that tests
patch on this module (see the comment under the imports). The turn's phases live in its
siblings:

* ``turn.py``: ``ChatTurn`` and the ``_TurnCancelled`` cancel signal;
* ``context_prep.py``: ``_prepare_context``, the blocking stages from rewrite to masking;
* ``frames.py``: ``sse``, the failure texts and the frame builders the generator yields;
* ``prompt_budget.py``: the reply budget and the prompt built against it;
* ``planning_steps.py``: phase 1, the rewrite, the #403 plan and speaker names;
* ``fanout.py``: the #403 parallel legs, enrichment and recurrence;
* ``masking.py``: SECURITY, whether excerpt text is masked before the LLM;
* ``evidence.py``: the quarantine drop, the digests-first merge, the fail-closed filter
  that drops what masking emptied, and the grounding diagnostics;
* ``summary_tier.py``: which leg feeds the ``<overview>``, and composing it;
* ``streaming.py``: output redaction and bookkeeping on the token stream;
* ``persistence.py``: persist, meter and audit the finished turn.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

import anyio
from starlette.concurrency import iterate_in_threadpool
from starlette.concurrency import run_in_threadpool

from app.core import chat_defaults
from app.services.chat import limits
from app.services.chat.context_prep import _prepare_context
from app.services.chat.frames import GENERATION_FAILED_MESSAGE
from app.services.chat.frames import _closing_frames
from app.services.chat.frames import _context_warning_frames
from app.services.chat.frames import _grounding_frames
from app.services.chat.frames import _provider_error_frame
from app.services.chat.frames import _text_frame
from app.services.chat.frames import sse
from app.services.chat.output_redactor import OutputRedactor
from app.services.chat.persistence import _finalize_turn
from app.services.chat.planning_steps import _initial_stage
from app.services.chat.prompt_blocks import format_counted_block
from app.services.chat.prompt_budget import _assemble_prompt
from app.services.chat.prompt_budget import _emit_prompt_trace
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.chat.streaming import _flush_redactor
from app.services.chat.streaming import _generation_kwargs
from app.services.chat.streaming import _mark_first_token
from app.services.chat.streaming import _record_output_redaction
from app.services.chat.streaming import _resolve_output_policy
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceRecorder
from app.services.chat.trace import emit as emit_trace
from app.services.chat.trace_stream import DEFAULT_QUEUE_MAXSIZE
from app.services.chat.trace_stream import StreamingTraceRecorder
from app.services.chat.trace_stream import WireEvent
from app.services.chat.trace_stream import drain_available
from app.services.chat.trace_stream import trace_payload
from app.services.chat.turn import ChatTurn
from app.services.chat.turn import _TurnCancelled
from app.services.llm_call_events import chat_call_scope

if TYPE_CHECKING:
    from app.services.llm_types import ChatLLM

# Tests patch `_finalize_turn`, `_resolve_output_policy`, `_prepare_context`,
# `StreamingTraceRecorder` and `_KEEPALIVE_INTERVAL_S` on THIS module, so each one's
# call site lives here and reads it from these globals. Moving a call site into a
# sibling would leave those patches silently inert.

logger = logging.getLogger(__name__)


# Without them, retrieval (OpenSearch + cross-encoder + masking) and then the
# wait for a first token can put ZERO bytes on the wire for well over nginx's
# default 60s proxy_read_timeout, and the proxy closes the stream mid-answer.
_KEEPALIVE = ": keepalive\n\n"


_KEEPALIVE_INTERVAL_S = 15


class _Awaited:
    """Carries a result out of :func:`_keepalive_until_done`.

    An async generator cannot ``return`` a value, and the caller needs both the
    frames *and* what the awaited work produced.
    """

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: Any = None


async def _keepalive_until_done(
    awaitable: Awaitable[object],
    holder: _Awaited,
    trace_q: asyncio.Queue[WireEvent] | None = None,
) -> AsyncGenerator[str, None]:
    """Await ``awaitable``, YIELDING frames while it runs.

    ⚠️ **Yielding is the entire point, and buffering is the bug this replaces.**
    The previous shape pushed keepalives into a queue that the caller drained
    *after* awaiting — but ``stream_reply`` is an async generator, and an async
    generator cannot yield from inside an ``await``. So every keepalive produced
    during retrieval reached the client only once retrieval had finished, in one
    burst, at exactly the moment it was no longer needed. Measured on the old
    code: 11 keepalives, all bearing the same timestamp as the end of the phase
    they were supposed to span.

    The result lands on ``holder`` because an async generator cannot return one.

    The awaited task is deliberately **not cancelled** when this generator is torn
    down (a client disconnect or Stop). That matches the pre-existing contract:
    the work runs in a threadpool and stops at its own next cancellation check,
    not because the socket went away.

    GH #514 rides the same loop. Trace events are produced on the SAME worker
    thread this is awaiting (and on ``legs.py``'s fan-out threads), so this is
    the only place that can put them on the wire while the phase is still
    running — which is the entire point of a *live* trace rather than a summary
    delivered once the work is already over.

    ⚠️ **It waits on EVENTS, never on a poll interval, and that is a measured
    decision.** A 50 ms poll cost +206 ms at p95 (592 -> 799 ms) because the loop
    only noticed ``_prep`` finishing on its next tick, and every tick paid a
    wake-up whether or not anything had happened. Racing the work against the
    queue instead wakes exactly when there is something to do: re-measured, p95
    went to within noise of the untraced arm. Do not reintroduce a poll here.

    Args:
        awaitable: The work to await — typically a ``run_in_threadpool`` call.
        holder: Receives the awaited result on completion.
        trace_q: GH #514's event queue, or ``None`` when the flag is off. ``None``
            keeps the original keepalive-only shape exactly.

    Yields:
        ``_KEEPALIVE`` SSE comment frames, plus ``trace`` frames when tracing.
    """
    task = asyncio.ensure_future(awaitable)
    getter: asyncio.Task | None = None
    last_beat = time.monotonic()
    try:
        while True:
            # One pending read of the queue, recreated only after it resolves —
            # a fresh `get()` per iteration would leak a waiter each time round.
            if trace_q is not None and getter is None:
                getter = asyncio.ensure_future(trace_q.get())

            waiting = {task} | ({getter} if getter is not None else set())
            elapsed = time.monotonic() - last_beat
            done, _ = await asyncio.wait(
                waiting,
                timeout=max(0.0, _KEEPALIVE_INTERVAL_S - elapsed),
                return_when=asyncio.FIRST_COMPLETED,
            )

            if getter is not None and getter in done:
                yield sse("trace", trace_payload(getter.result()))
                getter = None
                # Deliver this event before re-checking `task`: the work
                # finishing must not swallow an event already in hand.
                continue

            if task in done:
                holder.value = task.result()
                return

            if time.monotonic() - last_beat >= _KEEPALIVE_INTERVAL_S:
                last_beat = time.monotonic()
                yield _KEEPALIVE
    finally:
        # The pending read holds a reference to the queue and would otherwise
        # be reported as a task destroyed while pending on teardown.
        if getter is not None and not getter.done():
            getter.cancel()


def _open_trace(
    settings: ChatSettings, scope_files_dropped: int
) -> tuple[asyncio.Queue[WireEvent] | None, TraceRecorder | None]:
    """GH #514: the turn's trace queue and recorder, both ``None`` when tracing is off."""
    # GH #514. `trace_q is None` is the flag-off path and stays a hard
    # branch, not a fast path through shared code, so "free when off" is
    # structural rather than a hope.
    trace_q: asyncio.Queue[WireEvent] | None = None
    recorder: TraceRecorder | None = None
    if settings.trace_enabled:
        trace_q = asyncio.Queue(maxsize=DEFAULT_QUEUE_MAXSIZE)
        recorder = StreamingTraceRecorder(asyncio.get_running_loop(), trace_q)
        emit_trace(recorder, QueryStage.SUBMITTED, node_id="submitted")
        emit_trace(
            recorder,
            QueryStage.VALIDATED,
            node_id="validated",
            dropped=scope_files_dropped or 0,
        )
    return trace_q, recorder


async def _answer_frames(
    llm: ChatLLM,
    messages: list[dict[str, str]],
    kwargs: dict[str, Any],
    *,
    turn: ChatTurn,
    cancel_event: threading.Event,
    answer_redactor: OutputRedactor,
    reasoning_redactor: OutputRedactor,
    started: float,
    assistant_message_uuid: str,
    conversation_uuid: str,
) -> AsyncGenerator[str, None]:
    """Stream the provider's answer as frames, then flush what redaction still holds."""
    first_token_deadline = time.monotonic() + chat_defaults.DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S
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

        if event.type in ("delta", "reasoning"):
            # The watchdog below measures the PROVIDER's first token, so
            # it is satisfied here — before the redactor may hold this
            # delta back. Keying it off emission instead would read a
            # buffered first sentence as a stalled model and kill it.
            # The model IS actively responding once reasoning starts, so a
            # reasoning event satisfies it exactly like an answer delta —
            # otherwise a reasoning-heavy response looks "stalled" and gets
            # killed by the timeout below while tokens are visibly arriving.
            got_first_token = _mark_first_token(turn, got_first_token, started)
            frame = await _text_frame(event, turn, answer_redactor, reasoning_redactor)
            if frame:
                yield frame
        elif event.type == "usage":
            turn.prompt_tokens = event.prompt_tokens
            turn.completion_tokens = event.completion_tokens
            turn.cache_read_tokens = event.cache_read_tokens
            turn.cache_write_tokens = event.cache_write_tokens
        elif event.type == "error":
            yield _provider_error_frame(turn, event, conversation_uuid)
        elif event.type == "done":
            turn.finish_reason = event.finish_reason

        if not got_first_token and time.monotonic() > first_token_deadline:
            cancel_event.set()
            turn.error = "The model did not start responding in time."
            turn.error_code = "timeout"
            turn.metadata["error_code"] = turn.error_code
            yield sse("error", {"code": "timeout", "message": turn.error})
            break

    # The last sentence usually has no trailing whitespace to prove it
    # ended, so it is still in the buffer. Flushing here (rather than
    # only in the shielded teardown) is what puts it on the wire; the
    # teardown flush is idempotent and only covers a torn-down turn.
    tail = await _flush_redactor(answer_redactor)
    if tail:
        turn.answer_parts.append(tail)
        yield sse("delta", {"text": tail})
    reasoning_tail = await _flush_redactor(reasoning_redactor)
    if reasoning_tail:
        turn.reasoning_parts.append(reasoning_tail)
        yield sse("reasoning", {"text": reasoning_tail})


async def _teardown_turn(
    turn: ChatTurn,
    *,
    answer_redactor: OutputRedactor | None,
    reasoning_redactor: OutputRedactor | None,
    on_teardown: Callable[[], None] | None,
    llm: ChatLLM,
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
    """Flush, record, persist and release one turn. Awaited inside a shielded scope."""
    # A turn torn down mid-sentence still holds a buffered tail. It
    # cannot be yielded (there is no client left, and this scope
    # contains no `yield` by design) but it IS part of the answer,
    # so it is masked and persisted — the user sees it on reload.
    # `drain()` is idempotent, so a normal exit does not duplicate it.
    try:
        tail = await _flush_redactor(answer_redactor)
        if tail:
            turn.answer_parts.append(tail)
        reasoning_tail = await _flush_redactor(reasoning_redactor)
        if reasoning_tail:
            turn.reasoning_parts.append(reasoning_tail)
    except Exception:  # noqa: BLE001 — never mask the real outcome
        logger.exception("Chat output redaction teardown flush failed")
    _record_output_redaction(turn, answer_redactor, reasoning_redactor)

    try:
        await _finalize_turn(
            turn=turn,
            llm=llm,
            messages=messages,
            masked_count=masked_count,
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


class ChatService:
    """Streams one assistant reply and persists both sides of the exchange."""

    @staticmethod
    async def stream_reply(
        *,
        conversation_id: int,
        conversation_uuid: str,
        user_id: int,
        organization_id: int | None,
        question: str,
        history: list[dict[str, str]],
        file_uuids: list[str] | None,
        speakers: list[str] | None,
        scope_files_dropped: int = 0,
        settings: ChatSettings,
        use_context: bool,
        system_prompt: str,
        search_mode: str,
        temperature: float | None,
        max_tokens: int | None,
        top_p: float | None,
        enable_thinking: bool | None = None,
        llm: ChatLLM,
        on_teardown: Callable[[], None] | None = None,
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
            scope_files_dropped: How many of the caller's EXPLICIT file picks
                (never collections/tags) `resolve_scope_file_uuids` could not
                resolve into `file_uuids` — inaccessible, deleted, or
                quarantined. Stamped onto `msg_metadata` so a picker offering
                files the scope then silently discards (most visible for an
                admin, whose picker is tenant-wide while scope resolution has
                no admin bypass) is visible in the reply rather than only in
                a smaller `files_searched`.
            settings: Admin-tuned RAG knobs.
            use_context: False skips retrieval entirely (pure-LLM chat).
            system_prompt: Fully layered system prompt.
            search_mode: Retrieval mode for this turn.
            temperature: Per-conversation override, or None for the config default.
            max_tokens: Per-conversation answer-length override, clamped below.
            top_p: Per-conversation nucleus-sampling override, omitted when None.
            enable_thinking: ``False`` sends the measured "reasoning off" arm;
                ``None`` (the default) builds the payload exactly as it is built
                today, keeping vLLM's issue-#439 activation intact. Already
                gated on the model's measured capability by the endpoint --
                never derive it from a raw user preference here.
            on_teardown: Called exactly once inside the shielded finally, however
                the turn ends. Used to release the concurrency slot: a wrapping
                generator's own ``finally`` does NOT reliably run when Starlette
                tears this one down on client disconnect, so a slot released
                there leaks on every Stop.
            llm: The caller's ``ChatLLM``.
            assistant_message_uuid: Pre-allocated id for the reply.
            user_message_uuid: Id of the already-persisted user message.
            is_first_exchange: Whether to derive a title from this question.

        Yields:
            SSE frame strings.
        """
        turn = ChatTurn()
        if scope_files_dropped:
            turn.metadata["scope_files_dropped"] = scope_files_dropped
        cancel_event = threading.Event()
        started = time.monotonic()
        trace_q, recorder = _open_trace(settings, scope_files_dropped)

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
        # A counted answer is not "context" in the excerpt sense: it survives an
        # empty retrieval, so it is initialised here and not inside the branch.
        counted_block = ""
        overview_block = ""
        # #403: same reasoning as `counted_block`/`overview_block` above
        # — initialised here so a `use_context=False` turn (which never runs
        # `_prep()`) still has a defined, empty value to pass to
        # `build_messages`.
        synthesis_block = ""
        recurrence_block = ""
        reached_end = False
        # Declared out here because the shielded `finally` flushes them: a turn
        # torn down mid-sentence still has to persist the buffered tail.
        answer_redactor: OutputRedactor | None = None
        reasoning_redactor: OutputRedactor | None = None
        try:
            if use_context:
                stage = _initial_stage(
                    settings=settings,
                    llm=llm,
                    history=history,
                    question=question,
                    speakers=speakers,
                )
                yield sse("status", {"stage": stage})

                # No `session_scope` here on purpose: `_prepare_context` is
                # phased and opens its own short sessions. Wrapping it would put
                # the turn back to holding one transaction across the rewrite,
                # the counted tier's aggregation and retrieval.
                def _prep() -> tuple[list[MaskedChunk], dict[str, Any], Any, Any, str, str]:
                    # The planner, rewrite, enrichment and overview calls are recorded by the
                    # LLM seam, under this turn's user and tenant.
                    with chat_call_scope(user_id, organization_id):
                        return _prepare_context(
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
                            assistant_message_uuid=assistant_message_uuid,
                            recorder=recorder,
                        )

                prepared = _Awaited()
                async for frame in _keepalive_until_done(
                    run_in_threadpool(_prep), prepared, trace_q=trace_q
                ):
                    yield frame
                (
                    masked,
                    meta,
                    counted,
                    overview,
                    synthesis_block,
                    recurrence_block,
                ) = prepared.value
                turn.metadata.update(meta)
                counted_block = format_counted_block(counted)
                overview_block = overview.block if overview is not None else ""

                for frame in _context_warning_frames(meta):
                    yield frame

            messages, excerpt_ids, prompt_diagnostics, answer_tokens = _assemble_prompt(
                turn,
                llm=llm,
                settings=settings,
                system_prompt=system_prompt,
                masked=masked,
                history=history,
                question=question,
                max_tokens=max_tokens,
                counted_block=counted_block,
                overview_block=overview_block,
                recurrence_block=recurrence_block,
                synthesis_block=synthesis_block,
            )
            _emit_prompt_trace(
                recorder,
                chunks_used=len(excerpt_ids),
                prompt_diagnostics=prompt_diagnostics,
                started=started,
            )
            frames = [sse("trace", _payload) for _payload in await drain_available(trace_q)]
            if use_context:
                frames.extend(
                    _grounding_frames(
                        turn,
                        settings=settings,
                        masked=masked,
                        excerpt_ids=excerpt_ids,
                        overview=overview,
                        overview_block=overview_block,
                        prompt_diagnostics=prompt_diagnostics,
                        llm=llm,
                        answer_tokens=answer_tokens,
                        assistant_message_uuid=assistant_message_uuid,
                    )
                )
            for frame in frames:
                yield frame

            # Redaction of the model's OWN words. Offset masking covers the text
            # we gave it; nothing covered the text it writes about that material
            # until this. Resolved here rather than inside `_prepare_context`
            # because a `use_context=False` turn never runs that stage and its
            # answer is just as visible — which is also why it sits OUTSIDE the
            # use_context block above, unlike the language warning.
            output_policy = await run_in_threadpool(
                _resolve_output_policy, user_id, organization_id
            )
            answer_redactor = OutputRedactor(output_policy)
            reasoning_redactor = OutputRedactor(output_policy)

            yield sse("status", {"stage": "generating"})

            kwargs = _generation_kwargs(
                answer_tokens,
                temperature=temperature,
                top_p=top_p,
                enable_thinking=enable_thinking,
            )
            async for frame in _answer_frames(
                llm,
                messages,
                kwargs,
                turn=turn,
                cancel_event=cancel_event,
                answer_redactor=answer_redactor,
                reasoning_redactor=reasoning_redactor,
                started=started,
                assistant_message_uuid=assistant_message_uuid,
                conversation_uuid=conversation_uuid,
            ):
                yield frame

        except _TurnCancelled:
            # #403: a Stop landed at one of `_prepare_context`'s phase
            # boundaries, between leg submissions, or before the planner/
            # enrichment call it was guarding. No `turn.error` is set —
            # `reached_end` stays False, so the `finally` below records this
            # exactly like a client disconnect: `finish_reason = "cancelled"`,
            # no extra LLM call was ever made.
            logger.info("Chat turn %s cancelled during context preparation", assistant_message_uuid)
        except Exception:  # noqa: BLE001 — surface as a frame, never a 500
            logger.exception("Chat stream failed for conversation %s", conversation_uuid)
            turn.error = GENERATION_FAILED_MESSAGE
            turn.error_code = "provider_error"
            turn.metadata["error_code"] = turn.error_code
            yield sse("error", {"code": "provider_error", "message": turn.error})
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
                await _teardown_turn(
                    turn,
                    answer_redactor=answer_redactor,
                    reasoning_redactor=reasoning_redactor,
                    on_teardown=on_teardown,
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

        for frame in _closing_frames(turn):
            yield frame
