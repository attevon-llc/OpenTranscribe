"""Wordlist detector — profanity + per-user/admin custom words.

Cheap (compiled regex, microseconds) so it runs at READ time, which makes custom-word
and allowlist changes instant (no recompute). Word-boundary matching avoids the
"Scunthorpe problem" (substrings of innocent words are never matched).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import build_word_offsets
from app.services.redaction.spans import map_char_span_to_words

_DATA = Path(__file__).resolve().parent.parent / "data" / "profanity_en.txt"


@lru_cache(maxsize=1)
def _load_profanity() -> tuple[str, ...]:
    """Load the curated profanity terms (cached for the process lifetime)."""
    try:
        lines = _DATA.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    terms = [ln.strip().lower() for ln in lines if ln.strip() and not ln.startswith("#")]
    return tuple(sorted(set(terms), key=len, reverse=True))


@lru_cache(maxsize=1)
def _profanity_pattern() -> re.Pattern | None:
    terms = _load_profanity()
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"\b(?:{alts})\b", re.IGNORECASE)


def _custom_pattern(words: tuple[str, ...]) -> re.Pattern | None:
    if not words:
        return None
    alts = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True) if w)
    if not alts:
        return None
    return re.compile(rf"\b(?:{alts})\b", re.IGNORECASE)


def _spans_from_pattern(
    pattern: re.Pattern | None,
    text: str,
    *,
    category: str,
    entity_type: str,
    word_offsets: list[tuple[int, int]],
    allowset: set[str],
) -> list[RedactionSpan]:
    if pattern is None:
        return []
    out: list[RedactionSpan] = []
    for m in pattern.finditer(text):
        matched = m.group(0)
        if matched.lower() in allowset:
            continue
        ws, we = map_char_span_to_words(word_offsets, m.start(), m.end())
        out.append(
            RedactionSpan(
                char_start=m.start(),
                char_end=m.end(),
                word_start=ws,
                word_end=we,
                category=category,
                entity_type=entity_type,
                detector="wordlist",
                confidence=1.0,
            )
        )
    return out


def find_profanity_spans(
    text: str,
    words: list[dict] | None = None,
    allowlist: list[str] | None = None,
) -> list[RedactionSpan]:
    """Curated profanity spans (category='profanity', entity_type='PROFANITY')."""
    if not text:
        return []
    allowset = {a.strip().lower() for a in (allowlist or []) if a.strip()}
    word_offsets = build_word_offsets(text, words)
    return _spans_from_pattern(
        _profanity_pattern(),
        text,
        category="profanity",
        entity_type="PROFANITY",
        word_offsets=word_offsets,
        allowset=allowset,
    )


def find_custom_spans(
    text: str,
    custom_words: list[str] | None,
    words: list[dict] | None = None,
    allowlist: list[str] | None = None,
) -> list[RedactionSpan]:
    """Custom-vocabulary spans (category='custom', entity_type='CUSTOM')."""
    if not text or not custom_words:
        return []
    allowset = {a.strip().lower() for a in (allowlist or []) if a.strip()}
    word_offsets = build_word_offsets(text, words)
    pattern = _custom_pattern(tuple(w.strip() for w in custom_words if w.strip()))
    return _spans_from_pattern(
        pattern,
        text,
        category="custom",
        entity_type="CUSTOM",
        word_offsets=word_offsets,
        allowset=allowset,
    )
