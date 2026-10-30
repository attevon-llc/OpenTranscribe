"""Prompt-injection boundary: the sanitisers every block builder runs text through.

Transcript excerpts, recording titles and speaker names are untrusted input
(see ``prompting.py``). Anything interpolated into the prompt passes through
one of these first: :func:`_sanitize_body_text` for free-running text (no length
cap, tag-defusing only) and :func:`_sanitize_attribute` for short discrete
values placed inside a block tag attribute.
"""

from __future__ import annotations

import re

# Matches the opener of any HIGH-TRUST block tag, in any casing, with optional
# whitespace — "<excerpt", "</overview", "< / SYNTHESIS". Used to defuse
# breakout attempts. Originally excerpt-only; widened to every block the prompt
# assembles from user-influenced text, because a wrapper the model is told to
# trust unconditionally (base rules 10 and 12) is exactly the wrapper worth
# forging. `counted`/`overview` ship today; `recurrence`/`synthesis`/
# `scope_note` are additions defused ahead of the callers that emit them.
_BLOCK_TAG_RE = re.compile(
    r"<\s*/?\s*(?:excerpt|counted|overview|recurrence|synthesis|scope_note)",
    re.IGNORECASE,
)


def _sanitize_body_text(text: str) -> str:
    """Defuse block-tag breakout attempts with NO length cap.

    Body-safe, unlike :func:`_sanitize_attribute`: transcript excerpts, digest
    text, a speaker roster and a keyphrase list can all legitimately run long,
    and truncating them here would silently shred content a caller relies on
    being complete — the excerpt budget already trims deliberately elsewhere,
    and a second, accidental truncation point here would fight it. This
    function only ever neutralizes a tag opener; it never removes or shortens
    anything else.

    Regex-based ON PURPOSE. An earlier version walked the string comparing
    against ``text.lower()`` — which desynchronizes for characters whose
    lowercase form is LONGER than the original (Turkish "İ" lowercases to two
    codepoints). Past a handful of those, the offsets drift far enough that a
    real ``</excerpt>`` slips through intact while unrelated characters get
    clobbered. Never index one string with another string's offsets.
    """
    return _BLOCK_TAG_RE.sub(lambda m: "<\\" + m.group(0)[1:], text or "")


def _sanitize_chunk_text(text: str) -> str:
    """Neutralize attempts to break out of the excerpt wrapper.

    Thin alias over :func:`_sanitize_body_text` — chunk content is exactly the
    body-safe case: it must be defused, never truncated, here.
    """
    return _sanitize_body_text(text)


def _sanitize_attribute(value: str) -> str:
    """Make a metadata value safe to interpolate into a block tag attribute.

    File titles and speaker names are arbitrary user strings, and in a shared or
    org deployment the person who set them is not necessarily the person
    chatting. A title containing a quote plus a closing tag would otherwise end
    the wrapper early and inject instructions above the transcript body.

    Unlike :func:`_sanitize_body_text`, this ALSO strips quote/angle/newline
    characters and caps length at 120 chars — appropriate for a short discrete
    value (a name, a title) that is interpolated as an attribute, and wrong for
    anything long enough to be "body" text.
    """
    cleaned = _BLOCK_TAG_RE.sub("", value or "")
    for ch in ('"', "<", ">", "\n", "\r"):
        cleaned = cleaned.replace(ch, " ")
    return " ".join(cleaned.split())[:120]
