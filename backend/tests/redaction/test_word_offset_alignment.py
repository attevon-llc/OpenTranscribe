"""``build_word_offsets`` / ``map_char_span_to_words``, pinned branch by branch.

Mutation testing (issue #431) reported **29 surviving mutants in
``build_word_offsets`` and 7 in ``map_char_span_to_words``** — more than any other
function in the redaction plane. A survivor means mutmut changed the line, re-ran the
tests, and nothing failed: the line runs, and nothing checks it.

These two functions decide which *word tokens* a character span covers, which is what
the ``blur`` redaction style masks and what the player seeks to. Get them wrong and the
UI blurs the wrong word — or leaves the PII word visible while blurring its neighbour —
while every existing test still passes, because ``test_apply_redactions.py`` asserts
final masked TEXT against a frozen fixture whose words happen to align trivially.

Every test below names the branch it covers, so a survivor reappearing has an obvious home.
"""

from __future__ import annotations

import pytest

from app.services.redaction.spans import build_word_offsets
from app.services.redaction.spans import map_char_span_to_words


class TestBuildWordOffsets:
    def test_simple_alignment_returns_exact_char_ranges(self):
        text = "hello world"
        offsets = build_word_offsets(text, [{"word": "hello"}, {"word": "world"}])

        assert offsets == [(0, 5), (6, 11)]
        for (start, end), expected in zip(offsets, ["hello", "world"], strict=True):
            assert text[start:end] == expected

    def test_missing_words_returns_empty(self):
        """The `if not words` guard — separate from the empty-list case below."""
        assert build_word_offsets("hello", None) == []

    def test_empty_word_list_returns_empty(self):
        assert build_word_offsets("hello", []) == []

    def test_attached_punctuation_does_not_break_alignment(self):
        """The moving cursor exists for this: tokens are stripped, text is not."""
        text = "Hi, Bob. Bye!"
        offsets = build_word_offsets(text, [{"word": "Hi,"}, {"word": "Bob."}, {"word": "Bye!"}])

        assert [text[s:e] for s, e in offsets] == ["Hi,", "Bob.", "Bye!"]

    def test_a_whitespace_only_token_collapses_to_a_zero_width_offset(self):
        """Kills the mutants on `token.strip()` and the empty-token branch.

        A zero-width offset at the cursor keeps the returned list index-aligned with
        ``words`` — which is the whole contract, since callers index into it.
        """
        offsets = build_word_offsets("a b", [{"word": "a"}, {"word": "   "}, {"word": "b"}])

        assert len(offsets) == 3, "the result must stay index-aligned with `words`"
        assert offsets[1][0] == offsets[1][1], "an empty token must be zero-width"

    def test_a_missing_word_key_is_treated_as_empty(self):
        """``w.get("word", "")`` — the default must be an EMPTY token, not a placeholder.

        The fixture text deliberately contains the two literals a wrong default takes:
        ``"None"`` (what ``str(w.get("word"))`` produces when the default is dropped)
        and a filler run. A non-empty default is a *searchable* token — ``text.find``
        locates it, the word gets a real offset pointing at unrelated text, and the blur
        style then masks characters no detector ever flagged. With the text free of both
        (the original fixture was ``"a b"``) any wrong default merely fails to match and
        the bug is invisible.
        """
        offsets = build_word_offsets("None XXXX here", [{"start": 1.0}, {"word": "here"}])

        assert offsets[0] == (0, 0)

    def test_the_cursor_starts_at_zero_when_the_first_token_is_empty(self):
        """Kills the `cursor = 0` -> `cursor = None` mutant.

        Only the FIRST word can observe the initialiser — after one real token the
        cursor is an int either way, which is why the empty-token test above (whose
        blank is second) missed it. A ``(None, None)`` offset is worse than a wrong one:
        ``map_char_span_to_words`` compares it with ``>`` and raises TypeError.
        """
        offsets = build_word_offsets("hi there", [{"word": "  "}, {"word": "hi"}])

        assert offsets[0] == (0, 0)

    def test_the_restart_fallback_finds_the_first_occurrence_not_the_last(self):
        """Kills the ``text.find`` -> ``text.rfind`` mutant in the fallback.

        The fallback runs when the token sits behind the cursor. It takes two
        occurrences before the cursor for the two functions to disagree, and the
        existing fallback test has one — so it could not tell them apart. ``rfind``
        aligns the word to the LAST occurrence, blurring a different "Bob" than the
        detector flagged.
        """
        offsets = build_word_offsets("Bob and Bob and Sue", [{"word": "Sue"}, {"word": "Bob"}])

        assert offsets[1] == (0, 3)

    def test_an_unalignable_token_does_not_truncate_the_offset_list(self):
        """Kills the `continue` -> `break` mutant on the unalignable branch.

        Callers index ``offsets`` by word position, so a short list silently re-points
        every later word at the wrong one, or raises IndexError. The existing
        absent-token test puts the bad token LAST, where a break is invisible.
        """
        offsets = build_word_offsets(
            "only this", [{"word": "only"}, {"word": "ABSENT"}, {"word": "this"}]
        )

        assert offsets == [(0, 4), (4, 4), (5, 9)]

    def test_a_stutter_advances_past_every_occurrence(self):
        """Kills the `cursor = idx + len(token)` -> `idx - len(token)` mutant.

        Two occurrences cannot catch it: a backward cursor makes the forward search
        miss, and the restart-from-zero fallback then repairs the answer by accident —
        which is why ``test_a_repeated_word_advances...`` above survived this mutant.
        Three occurrences (a stutter, which real transcripts are full of) cannot be
        repaired: the second and third token both collapse onto the third.
        """
        offsets = build_word_offsets("no no no", [{"word": "no"}, {"word": "no"}, {"word": "no"}])

        assert offsets == [(0, 2), (3, 5), (6, 8)]

    def test_a_token_absent_from_the_text_yields_a_zero_width_offset(self):
        """Both `idx < 0` branches: the cursor search AND the from-the-start retry."""
        offsets = build_word_offsets("only this", [{"word": "only"}, {"word": "ABSENT"}])

        assert len(offsets) == 2
        assert offsets[1][0] == offsets[1][1], "an unalignable token must be zero-width"

    def test_a_token_appearing_earlier_is_found_by_the_restart_fallback(self):
        """The second ``text.find(token)`` — the reordering fallback.

        With the cursor already past it, the forward search fails and only the
        restart-from-zero retry can locate it. Deleting that fallback (a mutant) turns
        this into a zero-width offset, so the word would never be blurred.
        """
        text = "alpha beta"
        offsets = build_word_offsets(text, [{"word": "beta"}, {"word": "alpha"}])

        assert offsets[0] == (6, 10)
        assert offsets[1] == (0, 5), "the restart fallback must locate an earlier token"

    def test_a_repeated_word_advances_rather_than_rematching_the_first(self):
        """The cursor advance (`cursor = idx + len(token)`).

        Without it every occurrence resolves to the FIRST one, so redacting the second
        "Bob" would blur the first — the exact off-by-one class this module cannot afford.
        """
        text = "Bob met Bob"
        offsets = build_word_offsets(text, [{"word": "Bob"}, {"word": "met"}, {"word": "Bob"}])

        assert offsets == [(0, 3), (4, 7), (8, 11)]
        assert offsets[2][0] != offsets[0][0], "the second occurrence must not rematch the first"


class TestMapCharSpanToWords:
    @pytest.fixture
    def offsets(self) -> list[tuple[int, int]]:
        # "Bob met Sue" -> Bob[0,3) met[4,7) Sue[8,11)
        return [(0, 3), (4, 7), (8, 11)]

    def test_a_span_over_one_word_maps_to_that_word_only(self, offsets):
        assert map_char_span_to_words(offsets, 0, 3) == (0, 0)

    def test_a_span_over_two_words_maps_to_the_inclusive_range(self, offsets):
        assert map_char_span_to_words(offsets, 0, 7) == (0, 1)

    def test_a_span_touching_a_word_boundary_does_not_include_the_next_word(self, offsets):
        """char_end is EXCLUSIVE — [0,4) ends where "met" begins, so "met" is untouched.

        Kills the comparison-operator mutants: an off-by-one here blurs a word the
        detector never flagged.
        """
        assert map_char_span_to_words(offsets, 0, 4) == (0, 0)

    def test_a_span_starting_at_a_word_end_boundary_excludes_that_word(self, offsets):
        """Kills the `we > char_start` -> `we >= char_start` mutant — the mirror of the
        test above, on the other side of the intersection test.

        A span over the space and the next word ([3, 7) = " met") must not pull in
        "Bob", whose EXCLUSIVE end is exactly 3. With `>=` the span is credited to a
        word the detector never flagged, so blur masks one word too many.
        """
        assert map_char_span_to_words(offsets, 3, 7) == (1, 1)

    def test_a_span_inside_a_word_still_maps_to_it(self, offsets):
        """A sub-token match (e.g. a digit run inside a longer token)."""
        assert map_char_span_to_words(offsets, 1, 2) == (0, 0)

    def test_a_span_matching_nothing_returns_none(self, offsets):
        """Past the end of the text: callers fall back to char-only masking."""
        assert map_char_span_to_words(offsets, 50, 60) == (None, None)

    def test_no_offsets_returns_none(self):
        """The empty guard — reached whenever words were absent or unalignable."""
        assert map_char_span_to_words([], 0, 3) == (None, None)
