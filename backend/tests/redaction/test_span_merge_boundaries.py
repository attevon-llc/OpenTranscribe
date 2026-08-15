"""Boundary behaviour of ``_merge_spans``, pinned because mutation testing proved it wasn't.

``scripts/run-mutation-tests.sh --module spans`` reported 79 surviving mutants in this
module (issue #431). A survivor means mutmut edited the line, re-ran the suite, and
**nothing failed** — the line is executed but nothing asserts anything about it. In PII
masking that matters more than usual: an off-by-one here leaks the character it was
supposed to hide, or masks one it should not.

These tests kill the two survivors whose behaviour is genuinely security-relevant:

* ``x__merge_spans__mutmut_20`` flipped ``span.char_start < last.char_end`` to ``<=``.
  Nothing distinguished the two, so the suite never decided whether spans that merely
  *touch* (``a.char_end == b.char_start``) should merge into one placeholder or stay two.
* ``x__merge_spans__mutmut_10/12/13`` mutated ``_CATEGORY_PRIORITY.get(cat, 0)`` to
  ``None`` / no-default / ``1``. All survived, so no test ever passed a category absent
  from the priority table — meaning a new detector category shipped without a priority
  entry would be unguarded, and with the ``None`` variant the sort key raises TypeError.

The existing ``test_apply_redactions.py`` asserts final masked output against a frozen
fixture, which is why it kills the interior mutants but not these: its fixture happens to
contain no touching spans and no unregistered category.
"""

from __future__ import annotations

import pytest

from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import _merge_spans


def _span(start: int, end: int, category: str = "pii", entity: str = "NAME") -> RedactionSpan:
    return RedactionSpan(char_start=start, char_end=end, category=category, entity_type=entity)


class TestTouchingSpansAreNotMerged:
    """``char_end`` is EXCLUSIVE, so ``a.end == b.start`` means adjacent, not overlapping."""

    def test_two_adjacent_spans_stay_separate(self):
        """Kills the `<` -> `<=` mutant.

        "Bob" at [0,3) and "Sue" at [3,6) are two names, not one six-character one.
        Merging them would emit a single placeholder covering both, so a reader could no
        longer tell two people were mentioned — and the word indices of everything after
        would shift.
        """
        merged = _merge_spans([_span(0, 3), _span(3, 6)])

        assert len(merged) == 2, "touching spans must not merge; char_end is exclusive"
        assert [(s.char_start, s.char_end) for s in merged] == [(0, 3), (3, 6)]

    def test_a_one_character_overlap_does_merge(self):
        """The control: without this, "never merge" would also pass the test above."""
        merged = _merge_spans([_span(0, 4), _span(3, 6)])

        assert len(merged) == 1
        assert (merged[0].char_start, merged[0].char_end) == (0, 6)

    def test_a_contained_span_does_not_shorten_the_outer_one(self):
        """``max(last.char_end, span.char_end)`` — kills the mutant that drops the max()."""
        merged = _merge_spans([_span(0, 20), _span(5, 8)])

        assert len(merged) == 1
        assert (merged[0].char_start, merged[0].char_end) == (0, 20)


class TestUnregisteredCategoriesAreHandled:
    """Every ``_CATEGORY_PRIORITY.get`` needs its ``0`` default exercised."""

    def test_an_unknown_category_sorts_without_raising(self):
        """Kills the mutant that drops the default (``.get(cat)`` -> None).

        ``-None`` in the sort key is a TypeError, so a detector emitting a category that
        nobody added to the table would take down every redaction read for that segment,
        not degrade it.
        """
        spans = [_span(10, 14, category="pii"), _span(0, 4, category="brand_new_category")]

        merged = _merge_spans(spans)

        assert [(s.char_start, s.char_end) for s in merged] == [(0, 4), (10, 14)]

    def test_a_known_category_outranks_an_unregistered_one_on_overlap(self):
        """Kills the ``0`` -> ``1`` mutant: the default must rank BELOW every real category.

        ``custom`` is the lowest registered priority at 0. If the default were 1, an
        unregistered category would outrank ``custom`` and ``profanity`` and win the
        label on an overlap, so the masked output would name the wrong category.
        """
        overlapping = [
            _span(0, 6, category="unregistered", entity="MYSTERY"),
            _span(3, 9, category="profanity", entity="PROFANITY"),
        ]

        merged = _merge_spans(overlapping)

        assert len(merged) == 1
        assert merged[0].category == "profanity", (
            "an unregistered category must not outrank a registered one -- "
            "the priority default has to be lower than every entry in the table"
        )

    @pytest.mark.parametrize(
        ("winner", "loser"),
        [("pii", "toxicity"), ("toxicity", "profanity"), ("profanity", "custom")],
    )
    def test_the_registered_priority_order_decides_the_label(self, winner, loser):
        """Pins the table's ordering itself, so reshuffling it fails here."""
        merged = _merge_spans(
            [_span(0, 6, category=loser), _span(3, 9, category=winner)],
        )

        assert len(merged) == 1
        assert merged[0].category == winner


class TestTieBreakingAtTheSameStart:
    """The sort key's second element (``-priority``) only matters when two spans share a
    ``char_start``. Nothing exercised that, so mutmut could flip its sign, replace the
    lookup key with ``None``, or change the missing-category default — three separate
    mutants — with the suite still green."""

    def test_the_higher_priority_span_leads_at_a_tie(self):
        """Kills the sign flip and the ``.get(None, 0)`` mutant on the sort key.

        The leading span is the one whose ``word_start`` the merged span inherits, so
        the tie order decides which detector's word index survives. Presidio emits PII
        spans with no word indices; the wordlist emits profanity spans with them. At an
        equal ``char_start`` the PII span must lead, which is observable as the merged
        span carrying its ABSENT word_start rather than the profanity span's 0.

        That the merge can lose a known word index this way is a wart (blur/seek falls
        back to char offsets), not a leak — but the ORDER is what is under test, and
        this is the only field through which the order can be observed at all.
        """
        merged = _merge_spans(
            [
                RedactionSpan(
                    char_start=0,
                    char_end=6,
                    word_start=0,
                    word_end=0,
                    category="profanity",
                    entity_type="PROFANITY",
                ),
                RedactionSpan(char_start=0, char_end=10, category="pii", entity_type="NAME"),
            ]
        )

        assert merged[0].word_start is None

    def test_an_unregistered_category_never_leads_a_registered_one_at_a_tie(self):
        """Kills the sort key's `0` -> `1` default mutant.

        The leading span wins the label when neither outranks the other, so a default
        priority of 1 would sort an unknown category ahead of ``custom`` (priority 0)
        and the masked text would name the unknown category instead.
        """
        merged = _merge_spans(
            [
                _span(0, 6, category="custom", entity="CUSTOM"),
                _span(0, 10, category="unregistered", entity="MYSTERY"),
            ]
        )

        assert merged[0].entity_type == "CUSTOM"


class TestTheOverlapWinnerComparison:
    """``get(span.category, 0) > get(last.category, 0)`` — four mutants lived here."""

    def test_an_unregistered_second_span_does_not_take_the_label_from_custom(self):
        """Kills three winner-comparison mutants at once.

        The ``span.category`` lookup is reached with an unregistered category only when
        that span is the SECOND of an overlapping pair. Both existing unknown-category
        tests put it first, which is why all three survived. A ``None`` default raises
        TypeError here — taking down the whole transcript read rather than degrading —
        and a default of 1 hands an unknown category the label over ``custom``.
        """
        merged = _merge_spans(
            [
                _span(0, 6, category="custom", entity="CUSTOM"),
                _span(3, 9, category="unregistered", entity="MYSTERY"),
            ]
        )

        assert merged[0].entity_type == "CUSTOM"

    def test_two_spans_of_equal_priority_keep_the_earlier_label(self):
        """Kills the `>` -> `>=` mutant.

        Presidio routinely emits overlapping PII entities (a NAME inside an EMAIL).
        Both are priority 3, so neither outranks the other and the earlier span's label
        must survive; with `>=` the later one silently takes over and the masked text
        names the wrong entity type.
        """
        merged = _merge_spans(
            [
                _span(0, 6, category="pii", entity="NAME"),
                _span(3, 9, category="pii", entity="EMAIL"),
            ]
        )

        assert merged[0].entity_type == "NAME"


class TestTheSurvivingSpanCarriesTheRightFields:
    """``_merge_spans`` rebuilds a span from EIGHT fields, and four of them had no test:
    mutmut deleted ``word_start=``, ``word_end=``, ``detector=`` and ``confidence=``
    from the constructor call, or nulled them, and nothing failed — the existing tests
    only ever read ``char_start``/``char_end``/``category``/``entity_type``. The word
    indices are what the blur style masks and what the player seeks to; ``confidence``
    and ``detector`` are what the admin UI reports about a finding."""

    @pytest.fixture
    def merged(self) -> RedactionSpan:
        """One overlap: a low-confidence presidio NAME leading a gliner EMAIL."""
        leading = RedactionSpan(
            char_start=0,
            char_end=6,
            word_start=1,
            word_end=2,
            category="pii",
            entity_type="NAME",
            detector="presidio",
            confidence=0.4,
        )
        trailing = RedactionSpan(
            char_start=3,
            char_end=9,
            word_start=2,
            word_end=4,
            category="pii",
            entity_type="EMAIL",
            detector="gliner",
            confidence=0.9,
        )

        result = _merge_spans([leading, trailing])

        assert len(result) == 1, "fixture precondition: these two spans must overlap"
        return result[0]

    def test_the_merged_span_keeps_the_leading_span_word_start(self, merged):
        """Kills `word_start=None` and the dropped-``word_start=`` mutant.

        Losing it leaves the merged region with no word index, so blur/seek falls back
        to character offsets for a span that had one.
        """
        assert merged.word_start == 1

    def test_the_merged_span_extends_to_the_later_span_word_end(self, merged):
        """Kills `word_end=None`, the dropped ``word_end=``, and the inverted
        conditional. A short word_end leaves the tail words of the merged region
        unblurred — visible PII inside a span that reports itself as masked.
        """
        assert merged.word_end == 4

    def test_the_merged_span_keeps_the_winning_span_detector(self, merged):
        """Kills the dropped ``detector=``, which falls back to the ``"wordlist"``
        field default — misattributing a Presidio finding to the regex list."""
        assert merged.detector == "presidio"

    def test_the_merged_span_confidence_is_the_higher_of_the_two(self, merged):
        """Kills the dropped ``confidence=``, which falls back to the 1.0 field default
        — turning a 0.4/0.9 pair into a certainty neither detector reported."""
        assert merged.confidence == 0.9

    def test_a_later_span_without_word_indices_does_not_erase_the_earlier_ones(self):
        """The ``is not None`` conditional's other arm, and its inversion.

        Presidio spans carry no word indices while wordlist spans do, so a merge of the
        two reaches this branch in production. Inverting the condition propagates the
        missing value instead of keeping the known one.
        """
        merged = _merge_spans(
            [
                RedactionSpan(
                    char_start=0,
                    char_end=6,
                    word_start=1,
                    word_end=2,
                    category="pii",
                    entity_type="NAME",
                ),
                RedactionSpan(char_start=3, char_end=9, category="pii", entity_type="EMAIL"),
            ]
        )

        assert merged[0].word_end == 2


def test_an_empty_span_list_is_returned_unchanged():
    """The early return, which every mutant of the loop body leaves untouched."""
    assert _merge_spans([]) == []
