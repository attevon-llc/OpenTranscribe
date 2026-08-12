"""Non-ASCII text through the redaction span plane — the fixture gap, closed.

``spans.py`` had **zero** non-ASCII test input. Every offset in this module is a Python
*code point* index: ``len()`` counts code points, ``str.find`` returns a code-point
position, and ``text[a:b]`` slices by code point. A byte-oriented or UTF-16-oriented
implementation of the same arithmetic lands somewhere else — and in a PII masker,
landing somewhere else means masking the wrong characters, or leaving the ones it was
supposed to hide.

That is not hypothetical here: ``services/chat/prompting.py`` documents this exact bug
family as SHIPPED, and is tested against it with "İ" (``tests/unit/test_chat_prompting.py``).
An ASCII-only fixture cannot find any of it — which is also why a mutation survivor found
with an ASCII-only fixture is only as good as that fixture.

These tests pin the code-point contract for all four mask styles and for the word-offset
cursor. They kill no mutant that the ASCII suites do not already kill (verified with
``run-mutation-tests.sh --module spans``), so they are a contract, not a score: if a
consumer ever starts reading these offsets as JS string indices, or the implementation
ever moves to bytes, this file fails instead of the transcript silently mis-masking.

NOTE for whoever next runs mutation testing: this file is NOT in
``scripts/run-mutation-tests.sh``'s ``MODULE_TESTS[spans]`` selection. Add it there if a
future spans.py change makes a non-ASCII path the only thing that can kill a mutant.
"""

from __future__ import annotations

from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import apply_redactions
from app.services.redaction.spans import build_word_offsets


def _pii(start: int, end: int, entity: str = "NAME") -> RedactionSpan:
    return RedactionSpan(char_start=start, char_end=end, category="pii", entity_type=entity)


class TestNonAsciiText:
    """spans.py had ZERO non-ASCII test input before these.

    Offsets here are Python *code point* indices. A byte-oriented or UTF-16-oriented
    implementation of the same arithmetic lands somewhere else, and in this module
    landing somewhere else means masking the wrong characters —
    ``services/chat/prompting.py`` shipped exactly that bug family and is tested with
    "İ" for it (``tests/unit/test_chat_prompting.py``).
    """

    def test_a_cjk_name_is_masked_by_code_point_not_by_byte(self):
        """Four CJK characters are 4 code points and 12 UTF-8 bytes.

        The asterisk run must be 4 long. A byte-length implementation emits 12, which
        both reveals that the name was non-ASCII and shifts everything after it.
        """
        masked, _ = apply_redactions(
            "Call 田中太郎 today", [_pii(5, 9)], style="asterisks", enabled_categories={"pii"}
        )

        assert masked == "Call **** today"

    def test_an_astral_emoji_before_a_span_does_not_shift_it(self):
        """An emoji is ONE Python code point but TWO UTF-16 code units.

        Cached span offsets are Python indices, so the emoji — which sits outside the
        span — must come back untouched through the ``text[cursor:span.char_start]``
        prefix slice, with the span still landing exactly on "John".
        """
        masked, _ = apply_redactions(
            "🎉 party with John", [_pii(13, 17)], enabled_categories={"pii"}
        )

        assert masked == "🎉 party with [NAME]"

    def test_a_decomposed_accented_name_is_masked_whole(self):
        """NFD "José" is FIVE code points — the acute accent is its own character.

        first_letter therefore emits "J" plus four asterisks, not three. What matters
        is that nothing survives except the leading letter the style exists to show:
        the base "e" of "é" must not leak out from under a mask sized by glyph count.
        """
        # J o s e + COMBINING ACUTE ACCENT -- written as an escape because the
        # decomposed and precomposed forms are visually identical in an editor.
        decomposed = "Meet Jose\u0301 tomorrow"
        masked, _ = apply_redactions(
            decomposed, [_pii(5, 10)], style="first_letter", enabled_categories={"pii"}
        )

        assert masked == "Meet J**** tomorrow"

    def test_the_blur_style_passes_non_ascii_through_unescaped(self):
        """``html.escape`` touches only ``& < > " '`` — CJK must survive verbatim.

        blur is the one style that emits HTML, and it emits the ORIGINAL text for the
        UI to blur with CSS. An ASCII-only escaper would corrupt the revealed value for
        every non-English transcript. Also pins the exact markup the frontend
        ``sanitizeHtml`` allowlist has to admit.
        """
        masked, _ = apply_redactions(
            "Call 田中太郎 today", [_pii(5, 9)], style="blur", enabled_categories={"pii"}
        )

        assert masked == 'Call <span class="redacted" data-cat="pii">田中太郎</span> today'


class TestBuildWordOffsetsNonAscii:
    """The moving ``text.find`` cursor, over text Python and JS index differently."""

    def test_cjk_tokens_align_by_code_point_not_by_byte(self):
        """Each of these characters is three UTF-8 bytes, so a byte-oriented cursor puts
        every offset after the first token three times too far along — and the spans
        built from these offsets are what the blur style masks.
        """
        offsets = build_word_offsets(
            "こんにちは 田中太郎 です",
            [{"word": "こんにちは"}, {"word": "田中太郎"}, {"word": "です"}],
        )

        assert offsets == [(0, 5), (6, 10), (11, 13)]

    def test_an_astral_emoji_token_occupies_one_code_point(self):
        """An emoji is one Python code point and TWO UTF-16 code units.

        Any consumer reading these offsets as JS string indices is off by one per
        astral character — the bug family ``services/chat/prompting.py`` documents as
        shipped. Today the only consumer is Python (the frontend receives the applied
        spans but reads only their count), so this pins the contract: a change of
        consumer becomes a test failure rather than a silent mis-blur.
        """
        offsets = build_word_offsets(
            "hi 👋 John", [{"word": "hi"}, {"word": "👋"}, {"word": "John"}]
        )

        assert offsets == [(0, 2), (3, 4), (5, 9)]
