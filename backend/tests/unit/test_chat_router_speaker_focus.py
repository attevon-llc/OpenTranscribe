"""W2.2: ``Route.speaker_focus`` — an AXIS, not a fifth intent.

The router's two hard-won invariants — "the chunk tier is never removed" and
"structure only ever REMOVES a non-chunk tier, it never changes the label" —
are about ``intent``/``tiers``. ``speaker_focus`` is orthogonal to both: it is
carried through unchanged, never derived here (that is
``chat.speaker_resolver``'s job), and must never move ``intent`` or ``tiers``.
"""

from __future__ import annotations

import pytest

from app.services.chat.router import INTENT_AGGREGATE
from app.services.chat.router import INTENT_LOOKUP
from app.services.chat.router import INTENT_SUMMARIZE
from app.services.chat.router import TIER_CHUNK
from app.services.chat.router import Route
from app.services.chat.router import route

pytestmark = pytest.mark.unit


def test_speaker_focus_defaults_to_false():
    decision = route("What was decided about the budget?")
    assert decision.speaker_focus is False


def test_speaker_focus_is_carried_through_unchanged():
    decision = route("What did Dana say about pricing?", speaker_focus=True)
    assert decision.speaker_focus is True


@pytest.mark.parametrize(
    "question",
    [
        "How many meetings discussed pricing?",  # aggregate
        "Summarize the Atlas kickoff.",  # summarize
        "What was decided?",  # lookup
    ],
)
def test_speaker_focus_never_moves_the_intent_or_tiers(question):
    """The property under test: speaker_focus=True must not change what
    speaker_focus=False would have decided for the SAME question — it rides
    alongside the decision, it does not participate in making it."""
    without = route(question, speaker_focus=False)
    with_focus = route(question, speaker_focus=True)

    assert with_focus.intent == without.intent
    assert with_focus.tiers == without.tiers
    assert with_focus.signals == without.signals


def test_chunk_tier_still_never_removed_when_speaker_focus_is_set():
    decision = route("Summarize the Atlas kickoff.", speaker_focus=True)
    assert TIER_CHUNK in decision.tiers


def test_speaker_focus_true_appears_in_metadata():
    decision = route("What did Dana say?", speaker_focus=True)
    assert decision.as_metadata()["speaker_focus"] is True


def test_speaker_focus_false_is_absent_from_metadata():
    """Only rendered when true — a turn that never touched the resolver must
    not grow a permanent 'speaker_focus: false' key, matching every other
    optional field on this payload (literal, temporal, ...)."""
    decision = route("What was decided?")
    assert "speaker_focus" not in decision.as_metadata()


def test_route_dataclass_default_is_false():
    """A Route built with no keyword at all (the shape most callers use, and
    every pre-W2.2 test in this module) must stay exactly as it was."""
    assert Route().speaker_focus is False


@pytest.mark.parametrize(
    ("question", "expected_intent"),
    [
        ("How many meetings discussed pricing? in total", INTENT_AGGREGATE),
        ("Summarize the Atlas kickoff.", INTENT_SUMMARIZE),
        ("What was decided?", INTENT_LOOKUP),
    ],
)
def test_speaker_focus_does_not_leak_into_intent_classification(question, expected_intent):
    """`speaker_focus` is a caller-supplied fact, never something `classify`
    or the lexicon derives — so it must not skew which label a question
    resolves to, no matter its value."""
    assert route(question, speaker_focus=True).intent == expected_intent
    assert route(question, speaker_focus=False).intent == expected_intent
