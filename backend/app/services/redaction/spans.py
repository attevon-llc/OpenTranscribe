"""Redaction span model + the read-time masking function.

This module is PURE (no I/O, no heavy deps) so it is trivially unit-testable and
safe to import in the API process. ``apply_redactions`` is the single function
every read surface (API, subtitle/export, search snippet, LLM builder) calls.
"""

from __future__ import annotations

import html
from typing import Any

from pydantic import BaseModel
from pydantic import Field

# Category priority — when two spans overlap, the higher-priority label survives.
_CATEGORY_PRIORITY: dict[str, int] = {
    "pii": 3,
    "toxicity": 2,
    "profanity": 1,
    "custom": 0,
}

VALID_STYLES = ("label", "asterisks", "first_letter", "blur")


class RedactionSpan(BaseModel):
    """A single detected region to redact within a segment's text."""

    char_start: int
    char_end: int  # exclusive
    word_start: int | None = None  # index into segment.words (for blur/seek)
    word_end: int | None = None  # inclusive
    category: str  # "pii" | "toxicity" | "profanity" | "custom"
    entity_type: str  # "NAME" | "EMAIL" | "PHONE" | "PROFANITY" | "CUSTOM" | ...
    detector: str = "wordlist"  # "presidio" | "gliner" | "toxicity" | "wordlist" | "llm"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


def _coerce(span: Any) -> RedactionSpan | None:
    """Coerce a dict or RedactionSpan into a RedactionSpan (None on bad data)."""
    if isinstance(span, RedactionSpan):
        return span
    if isinstance(span, dict):
        try:
            return RedactionSpan(**span)
        except Exception:
            return None
    return None


def _placeholder(text: str, span: RedactionSpan, style: str) -> str:
    """Render the replacement string for one span according to the mask style."""
    original = text[span.char_start : span.char_end]
    if style == "asterisks":
        return "*" * max(1, len(original))
    if style == "first_letter":
        if not original:
            return "*"
        return original[0] + "*" * (len(original) - 1)
    if style == "blur":
        # Sanitizer-safe: escape the original so it can't inject markup. The UI
        # blurs via CSS and reveals (when authorized) on hover/click.
        return f'<span class="redacted" data-cat="{html.escape(span.category)}">{html.escape(original)}</span>'
    # default "label"
    return f"[{span.entity_type}]"


def _merge_spans(spans: list[RedactionSpan]) -> list[RedactionSpan]:
    """Sort, clamp-free de-overlap. Overlapping spans merge; higher priority wins the label."""
    if not spans:
        return []
    # Sort by start, then by descending priority so the higher-priority span leads at a tie.
    ordered = sorted(
        spans,
        key=lambda s: (s.char_start, -_CATEGORY_PRIORITY.get(s.category, 0)),
    )
    merged: list[RedactionSpan] = [ordered[0]]
    for span in ordered[1:]:
        last = merged[-1]
        if span.char_start < last.char_end:  # true overlap
            # Extend range; keep the higher-priority label.
            new_end = max(last.char_end, span.char_end)
            if _CATEGORY_PRIORITY.get(span.category, 0) > _CATEGORY_PRIORITY.get(last.category, 0):
                winner = span
            else:
                winner = last
            merged[-1] = RedactionSpan(
                char_start=last.char_start,
                char_end=new_end,
                word_start=last.word_start,
                word_end=span.word_end if span.word_end is not None else last.word_end,
                category=winner.category,
                entity_type=winner.entity_type,
                detector=winner.detector,
                confidence=max(last.confidence, span.confidence),
            )
        else:
            merged.append(span)
    return merged


def apply_redactions(
    text: str,
    spans: list[Any],
    *,
    style: str = "label",
    enabled_categories: set[str] | None = None,
    reveal_categories: set[str] | None = None,
) -> tuple[str, list[RedactionSpan]]:
    """Mask ``text`` using ``spans``.

    Pure, microsecond-scale. Returns ``(masked_text, applied_spans)``.

    Args:
        text: The original (unmasked) segment text.
        spans: Cached spans (dicts or ``RedactionSpan``); toxicity score blobs are
            ignored here (they carry no char range).
        style: One of ``label`` | ``asterisks`` | ``first_letter`` | ``blur``.
        enabled_categories: Categories to actually mask. ``None`` → mask all.
        reveal_categories: Categories the (authorized) viewer chose to reveal; these
            are NOT masked even if enabled. Admin-forced categories must be excluded
            from this set by the caller.
    """
    if not text or not spans:
        return text, []
    if style not in VALID_STYLES:
        style = "label"
    enabled = enabled_categories if enabled_categories is not None else set(_CATEGORY_PRIORITY)
    reveal = reveal_categories or set()

    n = len(text)
    candidates: list[RedactionSpan] = []
    for raw in spans:
        span = _coerce(raw)
        if span is None:
            continue
        if span.category not in enabled or span.category in reveal:
            continue
        # Clamp to text bounds (defensive — text may have changed after an edit).
        start = max(0, min(span.char_start, n))
        end = max(0, min(span.char_end, n))
        if end <= start:
            continue
        candidates.append(span.model_copy(update={"char_start": start, "char_end": end}))

    applied = _merge_spans(candidates)
    if not applied:
        return text, []

    out: list[str] = []
    cursor = 0
    for span in applied:
        if span.char_start > cursor:
            out.append(text[cursor : span.char_start])
        out.append(_placeholder(text, span, style))
        cursor = span.char_end
    if cursor < n:
        out.append(text[cursor:])
    return "".join(out), applied


def build_word_offsets(text: str, words: list[dict] | None) -> list[tuple[int, int]]:
    """Locate each word token's [start, end) char offset within ``text``.

    Uses a moving cursor + ``str.find`` so attached punctuation / extra spaces don't
    break alignment. Returns ``[]`` if ``words`` is missing or can't be aligned.
    """
    if not words:
        return []
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for w in words:
        token = str(w.get("word", "")).strip()
        if not token:
            offsets.append((cursor, cursor))
            continue
        idx = text.find(token, cursor)
        if idx < 0:
            # Fall back: search from the very start once (handles minor reordering).
            idx = text.find(token)
        if idx < 0:
            offsets.append((cursor, cursor))
            continue
        offsets.append((idx, idx + len(token)))
        cursor = idx + len(token)
    return offsets


def map_char_span_to_words(
    word_offsets: list[tuple[int, int]], char_start: int, char_end: int
) -> tuple[int | None, int | None]:
    """Return (word_start, word_end inclusive) for the words intersecting a char span."""
    if not word_offsets:
        return None, None
    first = last = None
    for i, (ws, we) in enumerate(word_offsets):
        if we > char_start and ws < char_end:  # intersects
            if first is None:
                first = i
            last = i
    return first, last
