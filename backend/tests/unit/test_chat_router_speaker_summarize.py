"""W2.3: ``Route.wants_speaker_digest_map`` — the routing gap this closes.

Before this property existed, "summarize what Alice said" was structurally
impossible: ``_apply_structure`` strips ``TIER_DIGEST`` whenever an explicit
speaker filter is active (correctly — the indexed digest carries no
single-valued speaker field), and nothing replaced it. ``decision.wants_digest``
went False and the scope map (``_resolve_summary_tier`` in ``service.py``)
never ran at all for exactly that shape of question.

These tests pin the DERIVED property that closes the gap, and the two cases
that must NOT set it: an unscoped summarize (nothing to route around), and a
speaker-scoped LITERAL quote (the digest is selected sentences, and a literal
quote already has no per-sentence speaker fallback either — quoting is about
finding an exact phrase, not one person's contribution).
"""

from __future__ import annotations

import pytest

from app.services.chat.router import TIER_DIGEST
from app.services.chat.router import route

pytestmark = pytest.mark.unit


def test_a_speaker_scoped_summarize_wants_the_speaker_digest_map():
    decision = route("Summarize the Atlas kickoff.", speakers=["Dana Whitfield"])
    assert decision.wants_speaker_digest_map is True


def test_the_indexed_digest_tier_is_still_removed_for_a_speaker_scope():
    """The map REPLACES what was lost, it does not un-remove it."""
    decision = route("Summarize the Atlas kickoff.", speakers=["Dana Whitfield"])
    assert TIER_DIGEST not in decision.tiers
    assert decision.wants_digest is False


def test_an_unscoped_summarize_does_not_want_the_speaker_map():
    decision = route("Summarize the Atlas kickoff.")
    assert decision.wants_speaker_digest_map is False


def test_a_speaker_scoped_lookup_does_not_want_the_speaker_map():
    """Only a SUMMARIZE-labelled turn routes to the map — a speaker-scoped
    lookup already has an exact answer from the chunk plane."""
    decision = route("What did Dana say about pricing?", speakers=["Dana Whitfield"])
    assert decision.wants_speaker_digest_map is False


def test_a_speaker_scoped_literal_quote_does_not_want_the_speaker_map():
    """A quoted phrase has no per-sentence speaker fallback either — quoting
    needs the exact words, not one person's selected contribution."""
    decision = route('Summarize what Dana said about "project atlas"', speakers=["Dana Whitfield"])
    assert decision.literal is True
    assert decision.wants_speaker_digest_map is False


def test_the_speaker_map_flag_appears_in_metadata_only_when_true():
    on = route("Summarize the Atlas kickoff.", speakers=["Dana Whitfield"])
    off = route("Summarize the Atlas kickoff.")
    assert on.as_metadata()["speaker_digest_map"] is True
    assert "speaker_digest_map" not in off.as_metadata()


def test_retrieve_digests_leg_is_untouched_by_the_new_property():
    """The property is purely additive routing information — it changes
    neither the label nor the tiers, matching every other derived Route
    property in this module."""
    with_map = route("Summarize the Atlas kickoff.", speakers=["Dana Whitfield"])
    without = route("Summarize the Atlas kickoff.")
    assert with_map.intent == without.intent
