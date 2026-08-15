"""Unit tests for the read-time masking function (pure, GPU-free, no network).

Golden-file driven: detection is simulated with known char offsets so the *masking*
layer is verified deterministically. Real model detection is covered by the
``@pytest.mark.models`` / integration suites.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.redaction.detectors import wordlist
from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import _placeholder
from app.services.redaction.spans import apply_redactions

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "redaction"

# Simulated PII entities per segment index (substring → entity_type).
_PII = {
    1: [("John Smith", "NAME"), ("john.smith@example.com", "EMAIL")],
    2: [("555-123-4567", "PHONE"), ("123-45-6789", "SSN")],
    3: [("4111 1111 1111 1111", "CREDIT_CARD")],
}
_CUSTOM_WORDS = ["Bluefin"]


@pytest.fixture(scope="module")
def segments() -> list[dict]:
    segs: list[dict] = json.loads((FIXTURES / "segments.json").read_text())
    return segs


@pytest.fixture(scope="module")
def expected_label() -> dict:
    expected: dict = json.loads((FIXTURES / "expected_label_style.json").read_text())
    return expected


def _all_spans(seg: dict) -> list[RedactionSpan]:
    """Assemble pii (simulated) + profanity + custom spans for one segment."""
    text = seg["text"]
    spans: list[RedactionSpan] = []
    for substr, etype in _PII.get(seg["idx"], []):
        start = text.find(substr)
        assert start >= 0, f"fixture substring not found: {substr}"
        spans.append(
            RedactionSpan(
                char_start=start,
                char_end=start + len(substr),
                category="pii",
                entity_type=etype,
                detector="presidio",
                confidence=0.95,
            )
        )
    spans.extend(wordlist.find_profanity_spans(text, seg.get("words")))
    spans.extend(wordlist.find_custom_spans(text, _CUSTOM_WORDS, seg.get("words")))
    return spans


def test_label_style_matches_golden(segments, expected_label):
    """Every fixture segment masks to the committed golden output (label style)."""
    enabled = {"pii", "profanity", "custom"}
    for seg in segments:
        masked, _ = apply_redactions(seg["text"], _all_spans(seg), enabled_categories=enabled)
        assert masked == expected_label[str(seg["idx"])], f"segment {seg['idx']} mismatch"


def test_scunthorpe_not_masked(segments):
    """Word-boundary matching must not flag 'Scunthorpe' (contains a profanity substring)."""
    seg6 = segments[6]
    spans = wordlist.find_profanity_spans(seg6["text"])
    assert spans == []


def test_styles(segments):
    seg0 = segments[0]
    spans = wordlist.find_profanity_spans(seg0["text"], seg0["words"])
    aster, _ = apply_redactions(
        seg0["text"], spans, style="asterisks", enabled_categories={"profanity"}
    )
    assert aster == "This is ******* ridiculous, I can't believe it."
    first, _ = apply_redactions(
        seg0["text"], spans, style="first_letter", enabled_categories={"profanity"}
    )
    assert first == "This is f****** ridiculous, I can't believe it."
    blur, _ = apply_redactions(seg0["text"], spans, style="blur", enabled_categories={"profanity"})
    assert 'class="redacted"' in blur and 'data-cat="profanity"' in blur


def test_reveal_returns_original(segments):
    seg0 = segments[0]
    spans = wordlist.find_profanity_spans(seg0["text"], seg0["words"])
    masked, _ = apply_redactions(
        seg0["text"], spans, enabled_categories={"profanity"}, reveal_categories={"profanity"}
    )
    assert masked == seg0["text"]


def test_disabled_category_not_masked(segments):
    seg0 = segments[0]
    spans = wordlist.find_profanity_spans(seg0["text"], seg0["words"])
    masked, _ = apply_redactions(seg0["text"], spans, enabled_categories={"pii"})
    assert masked == seg0["text"]  # profanity not in enabled set


def test_out_of_bounds_clamped():
    spans = [RedactionSpan(char_start=100, char_end=200, category="pii", entity_type="NAME")]
    masked, applied = apply_redactions("short text", spans, enabled_categories={"pii"})
    assert masked == "short text"
    assert applied == []


def test_overlap_priority():
    """Overlapping spans merge; PII outranks profanity for the surviving label."""
    text = "the secret word here"
    spans = [
        RedactionSpan(char_start=4, char_end=10, category="profanity", entity_type="PROFANITY"),
        RedactionSpan(char_start=4, char_end=15, category="pii", entity_type="NAME"),
    ]
    masked, applied = apply_redactions(text, spans, enabled_categories={"pii", "profanity"})
    assert masked == "the [NAME] here"
    assert len(applied) == 1 and applied[0].entity_type == "NAME"


# ---------------------------------------------------------------------------
# Edge behaviour the golden fixture structurally cannot reach.
#
# `run-mutation-tests.sh --module spans` reported 19 surviving mutants in
# ``apply_redactions`` and 2 in ``_placeholder`` (issue #431): mutmut edited a line,
# re-ran this file, and nothing failed. The golden fixture kills every INTERIOR mutant
# and none of the edge ones, because it contains no span at character 0, no degenerate
# or out-of-range span, no malformed cached span, no call that omits
# ``enabled_categories``, and no non-ASCII text.
# ---------------------------------------------------------------------------


def _pii(start: int, end: int, entity: str = "NAME") -> RedactionSpan:
    return RedactionSpan(char_start=start, char_end=end, category="pii", entity_type=entity)


def _profanity(start: int, end: int) -> RedactionSpan:
    return RedactionSpan(
        char_start=start, char_end=end, category="profanity", entity_type="PROFANITY"
    )


class TestClampingBoundaries:
    """``start = max(0, min(...))`` / ``end = max(0, min(...))`` and the copy that applies
    them. Every mutant here either leaks a character or masks one nobody flagged."""

    def test_a_span_at_character_zero_masks_the_first_character(self):
        """Kills the `max(0, ...)` -> `max(1, ...)` mutant on ``char_start``.

        With a lower clamp of 1 the mask starts one character late and the "J" of
        "John" is emitted verbatim ahead of the placeholder.
        """
        masked, _ = apply_redactions("John Smith called", [_pii(0, 4)], enabled_categories={"pii"})

        assert masked == "[NAME] Smith called"

    def test_a_negative_char_start_is_clamped_before_the_mask_is_sized(self):
        """A span starting before the text must still mask from offset 0.

        Kills the mutants that mangle the ``char_start`` key of the clamping
        ``model_copy(update=...)``: left unclamped, ``text[-4:4]`` is an EMPTY slice, so
        the asterisk run collapses to one "*" and the mask no longer covers the name.
        """
        masked, _ = apply_redactions(
            "John Smith", [_pii(-4, 4)], style="asterisks", enabled_categories={"pii"}
        )

        assert masked == "**** Smith"

    def test_a_char_end_past_the_text_is_clamped_in_the_returned_spans(self):
        """Kills the mutants that mangle the ``char_end`` key of the clamping copy.

        The masked TEXT is identical either way (Python slicing clamps silently), so
        only the returned spans expose it — and those ship to the client as
        ``segment_dict["redactions"]``. An end offset past the end of the text is a
        broken contract for anything that measures or indexes with it.
        """
        _, applied = apply_redactions("call John", [_pii(5, 99)], enabled_categories={"pii"})

        assert (applied[0].char_start, applied[0].char_end) == (5, 9)

    def test_a_zero_width_span_masks_nothing_at_all(self):
        """Kills the `max(0, ...)` -> `max(1, ...)` mutant on ``char_end``.

        ``char_start == char_end`` covers no characters. Raising that lower clamp to 1
        turns it into [0, 1), replacing the segment's first character with a
        placeholder the detector never asked for.
        """
        assert apply_redactions("hello", [_pii(0, 0)], enabled_categories={"pii"}) == ("hello", [])


class TestOneBadSpanCannotDisableTheRest:
    """Three ``continue``s in the candidate loop. A ``break`` in any of them lets one
    unusable span switch masking off for every span after it — the worst failure mode
    this function has, because the output still looks masked."""

    def test_a_malformed_cached_span_does_not_stop_later_spans(self):
        """Kills the `continue` -> `break` mutant on the ``_coerce`` guard.

        Cached spans are JSON read back from the DB, so one row written by an older
        detector version can fail validation. That must cost only that span.
        """
        masked, _ = apply_redactions(
            "call John now", [{"not": "a span"}, _pii(5, 9)], enabled_categories={"pii"}
        )

        assert masked == "call [NAME] now"

    def test_a_disabled_category_does_not_stop_later_spans(self):
        """Kills the `continue` -> `break` mutant on the category filter.

        A user with profanity masking off but PII on must still get the PII masked.
        With a ``break`` the first profanity span ends the loop and every PII span
        after it goes out in the clear.
        """
        masked, _ = apply_redactions(
            "damn John here", [_profanity(0, 4), _pii(5, 9)], enabled_categories={"pii"}
        )

        assert masked == "damn [NAME] here"

    def test_a_revealed_category_does_not_stop_later_spans(self):
        """The reveal arm of the same filter — the authorized-view path.

        ``?redact=false`` reveals only the categories the viewer may see. With a
        ``break``, revealing profanity would also un-mask the PII that follows it,
        including a category an admin has forced on for everyone.
        """
        masked, _ = apply_redactions(
            "damn John here",
            [_profanity(0, 4), _pii(5, 9)],
            enabled_categories={"pii", "profanity"},
            reveal_categories={"profanity"},
        )

        assert masked == "damn [NAME] here"

    def test_a_degenerate_span_does_not_stop_later_spans(self):
        """Kills the `continue` -> `break` mutant on the ``end <= start`` guard."""
        masked, _ = apply_redactions(
            "call John", [_pii(3, 3), _pii(5, 9)], enabled_categories={"pii"}
        )

        assert masked == "call [NAME]"


def test_omitting_enabled_categories_masks_every_registered_category():
    """``enabled_categories=None`` means "mask all" — kills the `set(None)` mutant.

    Every other test in this file passes ``enabled_categories`` explicitly, so the
    documented default was never executed: replacing ``set(_CATEGORY_PRIORITY)`` with
    ``set(None)`` raises TypeError and nothing noticed.
    """
    spans = [
        RedactionSpan(char_start=0, char_end=3, category="pii", entity_type="NAME"),
        RedactionSpan(char_start=4, char_end=7, category="toxicity", entity_type="TOXIC"),
        RedactionSpan(char_start=8, char_end=13, category="profanity", entity_type="PROFANITY"),
        RedactionSpan(char_start=14, char_end=18, category="custom", entity_type="CUSTOM"),
    ]

    masked, _ = apply_redactions("one two three four", spans)

    assert masked == "[NAME] [TOXIC] [PROFANITY] [CUSTOM]"


class TestPlaceholderRendering:
    """``_placeholder`` builds the visible label. Tested directly for the one branch
    ``apply_redactions`` cannot reach — which is itself the finding."""

    def test_a_single_character_span_yields_exactly_one_asterisk(self):
        """Kills the `max(1, len)` -> `max(2, len)` mutant.

        A one-character PII span (an initial, or a single-character CJK surname) must
        produce one asterisk. A floor of 2 pads the mask, so the masked text is longer
        than the original and every offset after it shifts.
        """
        masked, _ = apply_redactions(
            "Ask B about it", [_pii(4, 5)], style="asterisks", enabled_categories={"pii"}
        )

        assert masked == "Ask * about it"

    def test_first_letter_style_on_an_empty_span_returns_a_bare_asterisk(self):
        """Kills the mutant that rewrites the empty-original guard's return value.

        This branch is UNREACHABLE through ``apply_redactions`` — the ``end <= start``
        filter drops zero-width spans before ``_placeholder`` sees one — so it is
        defensive code protecting ``original[0]`` from IndexError. It is pinned rather
        than deleted because the guard is one line and the alternative, if that filter
        is ever relaxed, is a 500 on a transcript read. What must never happen is the
        guard returning a literal that would show up inside a transcript.
        """
        empty = RedactionSpan(char_start=2, char_end=2, category="pii", entity_type="NAME")

        assert _placeholder("hello", empty, "first_letter") == "*"
