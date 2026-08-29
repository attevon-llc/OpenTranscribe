"""Optional LLM-based detector — reuses the user's configured LLM provider.

Off by default (not in DEFAULT_REDACTION_DETECTORS). When enabled, sends a chunk of
the transcript to the user's LLM with a strict-JSON prompt asking for PII / toxic /
profane phrases, then resolves each returned phrase back to char offsets. Time-boxed
and fully tolerant — any failure returns ``[]`` and never blocks the pipeline.
"""

from __future__ import annotations

import contextlib
import json
import logging

from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import build_word_offsets
from app.services.redaction.spans import map_char_span_to_words

logger = logging.getLogger(__name__)

_PROMPT = (
    "You are a content-moderation assistant. Identify spans of text that are "
    "personally identifiable information (PII), profanity, or toxic/abusive language. "
    "Return STRICT JSON: a list of objects with keys "
    '"text" (the exact substring), "category" (one of "pii","profanity","toxicity"), '
    'and "entity_type" (e.g. NAME, EMAIL, PHONE, SSN, CREDIT_CARD, ADDRESS, PROFANITY, TOXIC). '
    "Return [] if none. Do not include any prose, only the JSON array.\n\nTEXT:\n"
)

_VALID_CATEGORIES = {"pii", "profanity", "toxicity", "custom"}


def detect_with_llm(
    segments: list[dict],
    user_id: int,
    *,
    max_chars: int = 6000,
    failures: list[str] | None = None,
) -> dict[int, list[RedactionSpan]]:
    """Run LLM detection over segments. Returns {segment_index: [spans]}.

    Best-effort: returns ``{}`` if no LLM is configured or anything fails, and never raises —
    the pipeline must not block on an opt-in detector.

    ⚠️ **That makes the return value ambiguous, which is what ``failures`` is for.** ``{}`` is
    what a working model that found nothing returns AND what a provider outage returns, so
    ``detect_and_store`` used to append ``"llm"`` to ``media_file.redaction_coverage`` whenever
    the owner had selected the detector — a misconfigured provider recorded as a completed scan
    with zero findings, and a coverage surface reporting clean. Same shape as the PII/toxicity
    sinks of issue #324, and the same remedy: report the fault out of band.

    Args:
        segments: ``{"text": ..., "words": ...}`` per transcript segment.
        user_id: Whose LLM provider to use.
        max_chars: Per-segment prompt budget.
        failures: Optional sink. ``"llm"`` is appended when no provider could be resolved or
            when a segment's call raised, so the caller can tell "examined and found nothing"
            from "never examined". Left alone when the detector genuinely ran clean.

    Returns:
        ``{segment_index: [spans]}`` for the segments that produced spans.
    """
    try:
        from app.services.llm_service import LLMService

        service = LLMService.create_from_settings(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("LLM redaction detector: no LLM service for user %s (%s)", user_id, exc)
        _report(failures)
        return {}
    if service is None:
        logger.info("LLM redaction detector: no LLM configured for user %s", user_id)
        _report(failures)
        return {}

    results: dict[int, list[RedactionSpan]] = {}
    try:
        for idx, seg in enumerate(segments):
            text = str(seg.get("text", ""))
            if not text.strip():
                continue
            spans = _detect_one(service, text, seg.get("words"), max_chars, failures)
            if spans:
                results[idx] = spans
    finally:
        with contextlib.suppress(Exception):
            service.close()
    return results


def _report(failures: list[str] | None) -> None:
    """Record the detector as not having examined the text, at most once per scan."""
    if failures is not None and "llm" not in failures:
        failures.append("llm")


def _detect_one(
    service,
    text: str,
    words: list[dict] | None,
    max_chars: int,
    failures: list[str] | None = None,
) -> list[RedactionSpan]:
    try:
        resp = service.chat_completion(
            [{"role": "user", "content": _PROMPT + text[:max_chars]}],
            temperature=0.0,
        )
        items = _parse_json_array(resp.content)
    except Exception as exc:  # noqa: BLE001
        logger.debug("LLM redaction detection failed for a segment: %s", exc)
        _report(failures)
        return []

    word_offsets = build_word_offsets(text, words)
    spans: list[RedactionSpan] = []
    for item in items:
        phrase = str(item.get("text", "")).strip()
        category = str(item.get("category", "")).lower()
        if not phrase or category not in _VALID_CATEGORIES:
            continue
        start = text.find(phrase)
        if start < 0:
            continue
        end = start + len(phrase)
        ws, we = map_char_span_to_words(word_offsets, start, end)
        spans.append(
            RedactionSpan(
                char_start=start,
                char_end=end,
                word_start=ws,
                word_end=we,
                category=category,
                entity_type=str(item.get("entity_type", category.upper()) or category.upper()),
                detector="llm",
                confidence=0.8,
            )
        )
    return spans


def _parse_json_array(content: str) -> list[dict]:
    """Tolerant JSON-array parse from an LLM response (handles code fences / prose)."""
    if not content:
        return []
    text = content.strip()
    if "```" in text:
        # Strip markdown fences.
        parts = text.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("["):
                text = p
                break
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        val = json.loads(text[start : end + 1])
        return [x for x in val if isinstance(x, dict)] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []
