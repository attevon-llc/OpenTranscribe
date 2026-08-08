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

logger = logging.getLogger(__name__)

_REWRITE_SYSTEM = (
    "You rewrite a follow-up question into a standalone search query.\n"
    "Resolve pronouns and references using the conversation, keep the user's own "
    "wording and proper nouns, and add nothing that was not asked.\n"
    "Reply with ONLY the rewritten query on a single line — no preamble, no quotes, "
    "no explanation. If the question already stands alone, repeat it unchanged."
)

MAX_REWRITE_CHARS = 300
_MAX_HISTORY_TURNS = 6
_MAX_TURN_CHARS = 500


def _sanitize(raw: str, fallback: str) -> str:
    """Reduce model output to a single plausible query line.

    The rewriter's output feeds a search query, so anything multi-line, empty or
    suspiciously long is treated as the model ignoring its instructions.
    """
    text = (raw or "").strip()
    if not text:
        return fallback
    text = text.splitlines()[0].strip().strip('"').strip("'")
    if not text or len(text) > MAX_REWRITE_CHARS:
        return fallback
    return text


def rewrite_query(llm, history: list[dict[str, str]], question: str) -> str:
    """Expand a follow-up question into a standalone query.

    Args:
        llm: An ``LLMService`` (the user's configured provider).
        history: Prior turns, oldest first.
        question: The current user message.

    Returns:
        The rewritten query, or ``question`` unchanged when there is no history,
        no LLM, or anything goes wrong.
    """
    if not history or llm is None:
        return question

    recent = history[-_MAX_HISTORY_TURNS:]
    transcript = "\n".join(
        f"{turn.get('role', 'user')}: {(turn.get('content') or '')[:_MAX_TURN_CHARS]}"
        for turn in recent
        if turn.get("content")
    )
    if not transcript:
        return question

    messages = [
        {"role": "system", "content": _REWRITE_SYSTEM},
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
        response = llm.chat_completion(messages, max_tokens=100, temperature=0)
    except Exception as exc:  # noqa: BLE001 — enhancement, never a dependency
        logger.info(f"Query rewrite unavailable, using original question: {exc}")
        return question

    rewritten = _sanitize(getattr(response, "content", ""), question)
    if rewritten != question:
        logger.info("Chat query rewritten (%d -> %d chars)", len(question), len(rewritten))
    return rewritten
