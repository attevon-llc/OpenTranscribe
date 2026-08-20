"""W2.5 — the router's recurrence intent, gated by ``chat.recurrence_enabled``.

``INTENT_RECURRENCE``/``TIER_RECURRENCE`` on STRONG markers only. The flag
must gate the LEXICON, not just the downstream shape: with it off,
``classify``/``route`` must never produce the label at all, so every other
route stays byte-identical to before this feature existed.
"""

from __future__ import annotations

import pytest

from app.services.chat.router import INTENT_LOOKUP
from app.services.chat.router import INTENT_RECURRENCE
from app.services.chat.router import TIER_CHUNK
from app.services.chat.router import TIER_RECURRENCE
from app.services.chat.router import classify
from app.services.chat.router import route

pytestmark = pytest.mark.unit

_STRONG_QUESTIONS = [
    "What keeps coming up across our meetings?",
    "Is there a recurring theme in these calls?",
    "What's a common thread across these recordings?",
    "Are there any repeated action items from our last few meetings?",
    "I keep hearing the same issue again — what is it?",
]


@pytest.mark.parametrize("question", _STRONG_QUESTIONS)
def test_strong_markers_classify_as_recurrence_when_the_flag_is_on(question):
    intent, signals = classify(question, recurrence_enabled=True)

    assert intent == INTENT_RECURRENCE
    assert signals


@pytest.mark.parametrize("question", _STRONG_QUESTIONS)
def test_the_same_markers_never_fire_with_the_flag_off(question):
    """The flag gates the PATTERNS, not just the shape: `classify` must not
    even test the recurrence lexicon when the flag is off."""
    intent, _signals = classify(question, recurrence_enabled=False)

    assert intent != INTENT_RECURRENCE


def test_classify_default_flag_value_is_off():
    """The default parameter value itself must be off — a caller that forgets
    to pass the flag must not accidentally enable the feature."""
    intent, _signals = classify("What keeps coming up across our meetings?")

    assert intent != INTENT_RECURRENCE


def test_route_produces_the_recurrence_tier_and_always_keeps_chunk():
    result = route("What keeps coming up across our meetings?", recurrence_enabled=True)

    assert result.intent == INTENT_RECURRENCE
    assert TIER_RECURRENCE in result.tiers
    assert TIER_CHUNK in result.tiers, "the chunk tier must never be removed"
    assert result.wants_recurrence is True


def test_route_flag_off_is_byte_identical_to_before_the_feature_existed():
    """Same question, flag off: must route exactly as it would have with no
    recurrence lexicon in the module at all — i.e. fall through to whatever
    the OTHER patterns would have produced (here: lookup, since none of the
    aggregate/summarize/temporal lexicons match either)."""
    question = "What keeps coming up across our meetings?"

    off = route(question, recurrence_enabled=False)
    default = route(question)  # recurrence_enabled defaults to False

    assert off.intent == INTENT_LOOKUP
    assert off.tiers == (TIER_CHUNK,)
    assert off.wants_recurrence is False
    assert off == default


def test_llm_intent_tiebreak_is_also_gated_by_the_flag():
    """The rewrite's free-text `INTENT: recurrence` line is a SECOND path to
    `INTENT_RECURRENCE` (the LLM tiebreak, consulted only when the rules found
    nothing) — it must be gated too, or a model's guess could produce the
    label with the flag off."""
    # No rule signals fire on this question at all.
    question = "tell me about the widget"

    with_flag = route(question, llm_intent=INTENT_RECURRENCE, recurrence_enabled=True)
    without_flag = route(question, llm_intent=INTENT_RECURRENCE, recurrence_enabled=False)

    assert with_flag.intent == INTENT_RECURRENCE
    assert without_flag.intent != INTENT_RECURRENCE


def test_weak_generic_words_do_not_fire_recurrence():
    """ "Repeat"/"again" alone are common in ordinary transcript questions
    ("can you repeat that") and must not trigger recurrence — only the
    explicit strong markers do."""
    intent, _signals = classify("Can you repeat what she said?", recurrence_enabled=True)

    assert intent != INTENT_RECURRENCE


def test_recurrence_outranks_aggregate_when_both_could_apply():
    """A question phrased as a count that also names recurrence explicitly is
    still fundamentally a recurrence question — see `classify`'s precedence
    docstring."""
    intent, _signals = classify(
        "How many times has this recurring issue come up across our meetings?",
        recurrence_enabled=True,
    )

    assert intent == INTENT_RECURRENCE
