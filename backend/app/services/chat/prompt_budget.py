"""The reply's token budget, and the prompt assembled against it.

``resolve_answer_tokens`` is the one place the reply budget is decided (the turn and
``GET /api/chat/answer-budget`` both read it), and it is resolved BEFORE
``build_messages``, which reserves context for the answer. What the budget kept is
recorded on the turn and reported as the trace's last two stages.
"""

from __future__ import annotations

import time
from typing import Any

from app.services.chat.prompting import build_messages
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.chat.trace import Outcome as TraceOutcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceRecorder
from app.services.chat.trace import emit as emit_trace
from app.services.chat.turn import ChatTurn

# Floor for a reply budget. Small enough to honour "keep answers short", large
# enough that a clamped request still returns a usable answer rather than a
# sentence cut in half.
MIN_ANSWER_TOKENS = 256


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


def _assemble_prompt(
    turn: ChatTurn,
    *,
    llm,
    settings: ChatSettings,
    system_prompt: str,
    masked: list[MaskedChunk],
    history: list[dict[str, str]],
    question: str,
    max_tokens: int | None,
    counted_block: str,
    overview_block: str,
    recurrence_block: str,
    synthesis_block: str,
) -> tuple[list[dict[str, str]], list[int], dict[str, Any], int]:
    """Budget the reply, then build the prompt, recording both on the turn.

    Returns:
        ``(messages, excerpt_ids, prompt_diagnostics, answer_tokens)``.
    """
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
    prompt_diagnostics: dict[str, Any] = {}
    messages, excerpt_ids = build_messages(
        system_prompt=system_prompt,
        chunks=masked,
        history=history,
        question=question,
        context_window=llm.user_context_window,
        response_tokens=answer_tokens,
        max_history_turns=settings.history_max_turns,
        diagnostics=prompt_diagnostics,
        counted_block=counted_block,
        overview_block=overview_block,
        recurrence_block=recurrence_block,
        synthesis_block=synthesis_block,
        overview_block_rule=settings.overview_block_rule,
        overview_after_excerpts=settings.overview_after_excerpts,
    )
    chunks_used = len(excerpt_ids)
    turn.metadata["chunks_used"] = chunks_used
    turn.metadata.update(prompt_diagnostics)
    # The window `budget_chars` above was computed against -- after any #833
    # narrowing. Without recording this, a truncated turn reads as a retrieval
    # failure, which is how a too-small window silently invalidated four full
    # evaluation runs before this fix.
    turn.metadata["context_window"] = llm.user_context_window
    return messages, excerpt_ids, prompt_diagnostics, answer_tokens


def _emit_prompt_trace(
    recorder: TraceRecorder | None,
    *,
    chunks_used: int,
    prompt_diagnostics: dict[str, Any],
    started: float,
) -> None:
    """GH #514's last two stages: what the budget kept, and what the model was shown."""
    # GH #514. BUDGETED and PRESENTED fire AFTER the retrieval drain has
    # already returned, so without a flush here they would sit in the
    # queue forever and the last two stages would never reach the client
    # at all — and a live-only trace has no persistence to cover it.
    # ⚠️ INTS ONLY: `excerpt_ids` resolve to real chunks and
    # `turn.offered_citations` carries titles and timestamps.
    emit_trace(
        recorder,
        QueryStage.BUDGETED,
        TraceOutcome.OK if chunks_used else TraceOutcome.EMPTY,
        node_id="budget",
        kept=chunks_used,
        dropped=int(prompt_diagnostics.get("chunks_dropped_for_budget") or 0),
        limit=int(prompt_diagnostics.get("budget_chars") or 0),
    )
    emit_trace(
        recorder,
        QueryStage.PRESENTED,
        TraceOutcome.OK if chunks_used else TraceOutcome.EMPTY,
        node_id="answer",
        count=chunks_used,
        ms=int((time.monotonic() - started) * 1000),
    )
