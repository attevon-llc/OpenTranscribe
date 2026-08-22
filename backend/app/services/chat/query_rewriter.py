"""Conversational query rewriting.

Retrieval sees one query string; conversation carries meaning across turns. Ask
"what did she say about the timeline?" after a turn about Dana and a raw search
for that sentence matches almost nothing useful — "she" and "the timeline" carry
no signal. Rewriting expands the follow-up into a standalone question using the
recent history, which is where most of RAG's multi-turn relevance gain comes from.

Every failure mode falls back to the original question: this is an enhancement in
the hot path, never a dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_REWRITE_SYSTEM = (
    "You rewrite a follow-up question into a standalone search query.\n"
    "Resolve pronouns and references using the conversation, keep the user's own "
    "wording and proper nouns, and add nothing that was not asked.\n"
    "Write the rewritten query in the SAME LANGUAGE as the question. Never "
    "translate it: the query is matched against transcripts in their own language, "
    "so translating it searches for words nobody said.\n"
    "Reply with the rewritten query on the FIRST line — no preamble, no quotes, "
    "no explanation. If the question already stands alone, repeat it unchanged.\n"
    "On a SECOND line, write 'INTENT: ' followed by exactly one of: lookup, "
    "summarize, aggregate, temporal. Use lookup for a question about what someone "
    "said, summarize for a request to recap one or more recordings, aggregate for "
    "counting or listing across recordings, temporal for a question about when "
    "something happened. If unsure, write 'INTENT: lookup'."
)


@dataclass(frozen=True)
class RewriteResult:
    """The standalone query, plus the routing hint that rode along for free.

    ``intent`` is advisory. :mod:`app.services.chat.router` consults it only when
    its own rules found no signal at all, so a model that ignores or fumbles the
    second line costs nothing — which is the whole reason the hint is allowed to
    piggyback on a call that was already being made.

    ``plan`` (#403 W2.6) is the same "free" piggyback applied to the planner: a
    follow-up turn extends this same call with a THIRD line instead of paying
    for a standalone planner round trip. ``None`` unless ``want_plan=True`` was
    passed AND the model produced a well-formed ``PLAN:`` line.
    """

    query: str
    intent: str | None = None
    plan: Any | None = None


MAX_REWRITE_CHARS = 300
_MAX_HISTORY_TURNS = 6
_MAX_TURN_CHARS = 500

#: #403 W2.6. A follow-up turn's rewrite call already pays for one LLM round
#: trip; asking for a third ``PLAN:`` line on it is never a SECOND call, only
#: more tokens on the one already being made — this is the "never a
#: routing-only call" rule extended to the planner. 100 -> 300 in the
#: EXTENDED variant only: the base rewrite call (no plan requested) keeps its
#: original 100-token budget unchanged, so a deployment with the planner off
#: pays nothing extra for this at all.
_REWRITE_MAX_TOKENS = 100
_REWRITE_WITH_PLAN_MAX_TOKENS = 300


def _sanitize(raw: str, fallback: str) -> str:
    """Reduce model output to a single plausible query line.

    The rewriter's output feeds a search query, so anything multi-line, empty or
    suspiciously long is treated as the model ignoring its instructions.

    **The ``INTENT:`` guard is not defensive tidying.** ``strip()`` runs before
    the split, so a response whose first line is blank promotes the second line
    to first — and the second line is the intent declaration. Without this,
    ``"\\nINTENT: lookup"`` searched the corpus for the literal string
    "INTENT: lookup" and returned nothing, which surfaces as a confident "I
    don't have enough information" over a library full of matching material.
    Found by the test that pins it.
    """
    text = (raw or "").strip()
    if not text:
        return fallback
    text = text.splitlines()[0].strip().strip('"').strip("'")
    if not text or len(text) > MAX_REWRITE_CHARS:
        return fallback
    if text.upper().startswith("INTENT:"):
        return fallback
    return text


def rewrite_query(
    llm, history: list[dict[str, str]], question: str, *, want_plan: bool = False
) -> RewriteResult:
    """Expand a follow-up question into a standalone query.

    Args:
        llm: An ``LLMService`` (the user's configured provider).
        history: Prior turns, oldest first.
        question: The current user message.
        want_plan: #403 W2.6. When true, extend the SAME call with a third
            ``PLAN:`` line (see ``planner.PLAN_LINE_INSTRUCTION``) instead of
            making a second, standalone planner call — a follow-up turn's
            plan is never a routing-only round trip. Raises the reply budget
            100 -> 300 tokens for this call only.

    Returns:
        A :class:`RewriteResult`. On any failure — no history, no LLM, a provider
        error, unusable output — the query is ``question`` unchanged and the
        intent is ``None``. **There is never a call made only for the intent**:
        turn 1 has no history and returns here before touching the provider,
        which is exactly where "summarize my meetings this week" lands.
    """
    if not history or llm is None:
        return RewriteResult(question)

    recent = history[-_MAX_HISTORY_TURNS:]
    transcript = "\n".join(
        f"{turn.get('role', 'user')}: {(turn.get('content') or '')[:_MAX_TURN_CHARS]}"
        for turn in recent
        if turn.get("content")
    )
    if not transcript:
        return RewriteResult(question)

    # Imported here rather than at module scope: the router/planner import
    # nothing themselves, but the rewriter is loaded on the request path and
    # a cycle between them would be a startup failure rather than a lint
    # finding.
    from app.services.chat.planner import PLAN_LINE_INSTRUCTION

    system = _REWRITE_SYSTEM + ("\n" + PLAN_LINE_INSTRUCTION if want_plan else "")
    max_tokens = _REWRITE_WITH_PLAN_MAX_TOKENS if want_plan else _REWRITE_MAX_TOKENS

    messages = [
        {"role": "system", "content": system},
        # Concatenation only — history and question are untrusted text.
        {
            "role": "user",
            "content": "Conversation so far:\n"
            + transcript
            + "\n\nFollow-up question:\n"
            + question,
        },
    ]

    try:
        response = llm.chat_completion(messages, max_tokens=max_tokens, temperature=0)
    except Exception as exc:  # noqa: BLE001 — enhancement, never a dependency
        logger.info(f"Query rewrite unavailable, using original question: {exc}")
        return RewriteResult(question)

    raw = getattr(response, "content", "") or ""
    rewritten = _sanitize(raw, question)
    from app.services.chat.router import parse_intent_line

    intent = parse_intent_line(raw)
    plan = None
    if want_plan:
        from app.services.chat.planner import parse_plan_line

        plan = parse_plan_line(raw)
    if rewritten != question:
        logger.info("Chat query rewritten (%d -> %d chars)", len(question), len(rewritten))
    return RewriteResult(rewritten, intent, plan)
