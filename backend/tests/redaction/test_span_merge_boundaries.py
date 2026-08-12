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


def test_an_empty_span_list_is_returned_unchanged():
    """The early return, which every mutant of the loop body leaves untouched."""
    assert _merge_spans([]) == []
