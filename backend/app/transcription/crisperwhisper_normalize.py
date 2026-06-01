"""Normalize raw CrisperWhisper word-level output into standard whisper shape.

CrisperWhisper uses a *verbatim* tokenizer (spaces are explicit tokens, used for
pause detection). When run through faster-whisper's ``word_timestamps=True`` path
this yields word tokens that are unusable downstream:

* **Comma/punctuation prefix artifact** — instead of the standard leading-space
  convention (``" not"``), CrisperWhisper prefixes each word with the *separator
  that preceded it*: a comma where a space would be (``",not"``, ``",even"``) or a
  sentence punctuation mark glued onto the *front* of the next word (``".Right"``,
  ``"?How"``). The first word of a segment has no separator (``"Code's"``).
* **No internal spaces in segment text** — joining the raw ``word`` fields produces
  comma-jammed text (``"Code's,not,even,the"``). Every downstream consumer that
  reconstructs text via ``"".join(w["word"] ...)`` or counts words via
  ``text.split()`` then sees a *single* whitespace-token per comma-run. This halves
  the word count and trips ``clean_garbage_words`` (long no-space runs → noise).
* **Filler tokens** — genuine disfluencies are emitted as ``"[UM]"`` / ``"[UH]"``
  bracket tokens. These are real transcription content and are kept (lower-cased to
  a readable ``"[um]"``), never invented or dropped.

This module reshapes — it never fabricates or removes words. Each input word maps
to exactly one output word: only the surface form (leading-space convention, moved
trailing punctuation) and the timestamps (monotonic, non-overlapping) are repaired.
"""

from __future__ import annotations

# Maximum plausible duration for a single word (seconds). Mirrors transcriber.py.
_MAX_WORD_DURATION = 5.0
# Punctuation CrisperWhisper glues onto the FRONT of the next word; it logically
# belongs to the END of the previous word (standard whisper attaches it there).
_TRAILING_PUNCT = ".,!?;:"


def _clean_word_surface(raw: str) -> str:
    """Strip CrisperWhisper tokenization artifacts from a single raw word token.

    Removes the leading comma/space/punctuation separator and any trailing comma the
    verbatim tokenizer appended, then normalizes bracket filler tokens to lower case.
    The returned form carries **no** leading space — the caller re-applies the standard
    leading-space convention so that ``"".join`` reconstruction spaces words correctly.

    Args:
        raw: A raw ``word`` field as emitted by CrisperWhisper (e.g. ``",not"``,
            ``".Right"``, ``"Code's"``, ``",[UM]"``, ``",can,"``).

    Returns:
        The bare word with artifacts removed (e.g. ``"not"``, ``"Right"``, ``"[um]"``).
        May be an empty string if the token was pure separator(s).
    """
    text = raw

    # Drop a single leading separator: comma, space, or sentence punctuation. Only the
    # FIRST character is a separator artifact; any further punctuation is part of the
    # word (e.g. an ellipsis the model genuinely produced). Sentence punctuation glued
    # to the front is handled by the caller (moved onto the previous word), so here we
    # simply remove the artifact prefix from this word's surface form.
    if text[:1] in _TRAILING_PUNCT or text[:1] == " ":
        text = text[1:]

    # The verbatim tokenizer occasionally appends a trailing comma (e.g. ",can,").
    # A real sentence-final period/question mark is preserved; only the artifact comma
    # is stripped.
    text = text.rstrip(",")

    # Normalize bracket filler tokens ("[UM]" -> "[um]") for readable, consistent text.
    # These are genuine transcription content and are kept, only re-cased.
    if text.startswith("[") and text.endswith("]"):
        text = text.lower()

    return text


def _leading_punct(raw: str) -> str:
    """Return the sentence punctuation CrisperWhisper glued onto the word's front.

    Returns the leading ``.``/``?``/``!``/``;``/``:`` (but **not** a comma — commas are
    the plain space-separator artifact, not meaningful sentence punctuation), so the
    caller can re-attach it to the *previous* word where it belongs.
    """
    head = raw[:1]
    if head in ".!?;:":
        return head
    return ""


def normalize_crisperwhisper(segments: list[dict]) -> list[dict]:
    """Reshape raw CrisperWhisper segments into standard whisper word-level output.

    Applies, per segment:

    1. **Surface cleanup** — each word's leading comma/space/punctuation separator and
       trailing-comma artifact are stripped; sentence punctuation glued to a word's
       front is moved onto the *previous* word; bracket fillers (``"[UM]"``) are kept
       but lower-cased. Every cleaned word is then given the standard single leading
       space so ``"".join(w["word"] ...)`` reconstructs correctly spaced text.
    2. **Timestamp repair** — durations longer than ``_MAX_WORD_DURATION`` are capped,
       monotonicity is enforced (each word starts at/after the previous word's end),
       and any ``start > end`` is corrected, all clamped to the segment bounds.
    3. **Text rebuild** — the segment ``text`` is rebuilt from the cleaned words so it
       contains proper spaces (fixing word counts and garbage-word detection).

    This is a pure reshape: word *count* is preserved (a token whose surface form is
    pure separator is dropped only because it carried no actual word). No backchannels
    or words are invented. Non-CrisperWhisper (already-clean) input is safe to pass
    through — words that already use the leading-space convention are unaffected.

    Args:
        segments: List of segment dicts, each with ``text``, ``start``, ``end`` and a
            ``words`` list of dicts (``word``, ``start``, ``end``, ``probability``).

    Returns:
        A new list of normalized segment dicts. Input is not mutated.
    """
    result: list[dict] = []

    for segment in segments:
        seg_start = float(segment.get("start", 0.0))
        seg_end = float(segment.get("end", seg_start))
        raw_words = segment.get("words") or []

        cleaned_words: list[dict] = []
        for raw in raw_words:
            raw_text = str(raw.get("word", ""))

            # Sentence punctuation glued to this word's front belongs to the previous
            # word. Move it before we strip it from this word's surface form.
            moved = _leading_punct(raw_text)
            if moved and cleaned_words:
                cleaned_words[-1]["word"] = cleaned_words[-1]["word"].rstrip() + moved
                # Rebuild leading space (rstrip above removed it for the +punct join).
                stripped = cleaned_words[-1]["word"].lstrip()
                cleaned_words[-1]["word"] = " " + stripped

            surface = _clean_word_surface(raw_text)
            if not surface:
                # Pure-separator token carried no actual word — nothing to keep.
                continue

            cleaned_words.append(
                {
                    "word": " " + surface,
                    "start": float(raw.get("start", seg_start)),
                    "end": float(raw.get("end", seg_start)),
                    "probability": float(raw.get("probability", 0.0)),
                }
            )

        _repair_timestamps(cleaned_words, seg_start, seg_end)

        rebuilt_text = "".join(w["word"] for w in cleaned_words).strip()

        new_segment = dict(segment)
        new_segment["text"] = (
            rebuilt_text if cleaned_words else str(segment.get("text", "")).strip()
        )
        new_segment["start"] = seg_start
        new_segment["end"] = seg_end
        new_segment["words"] = cleaned_words
        result.append(new_segment)

    return result


def _repair_timestamps(words: list[dict], seg_start: float, seg_end: float) -> None:
    """Enforce monotonic, non-overlapping, in-bounds word timestamps in place.

    Mirrors ``transcriber._validate_word_timestamps`` but additionally repairs the
    ``start > end`` inversions CrisperWhisper's cross-attention DTW occasionally emits.
    Each word is clamped to ``[seg_start, seg_end]``, capped at ``_MAX_WORD_DURATION``,
    and forced to begin at or after the previous word ends.

    Args:
        words: Word dicts with ``start``/``end`` keys (mutated in place).
        seg_start: Segment start time (lower bound).
        seg_end: Segment end time (upper bound).
    """
    for i, word in enumerate(words):
        start = max(float(word["start"]), seg_start)
        end = min(float(word["end"]), seg_end)

        # Repair inverted timestamps (start > end) up front.
        if end < start:
            end = start

        # Enforce monotonicity against the previous word.
        if i > 0:
            prev_end = words[i - 1]["end"]
            if start < prev_end:
                start = prev_end
            if end < start:
                end = start

        # Cap implausibly long durations.
        if end - start > _MAX_WORD_DURATION:
            end = min(start + _MAX_WORD_DURATION, seg_end)

        # Guarantee a non-zero (or at least non-negative) span within bounds.
        if end <= start:
            end = min(start + 0.01, seg_end)
            if end < start:  # seg_end itself was at/below start
                end = start

        word["start"] = start
        word["end"] = end
