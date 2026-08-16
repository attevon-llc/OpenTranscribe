"""The query router (#403 Stage 4): labels, tiers, and the invariants that bound a misroute.

Every test here is a claim the router could fail. Two of them exist because the
router **did** fail them against the eval corpus before these were written:

* ``which meeting`` (singular) routed 100 verbatim-control lookups to the
  aggregate tier — 94% of all lookup leakage in the first measured run.
* ``today``/``yesterday`` routed a question about words spoken in a debate to
  the temporal tier.

The rest guard the two invariants that make a misroute cheap rather than
catastrophic: **the chunk tier is never removed**, and **structure never changes
the label**. Both are asserted over the whole label set, not one example, so a
new label cannot be added without honouring them.
"""

from __future__ import annotations

import pytest

from app.services.chat.router import INTENT_AGGREGATE
from app.services.chat.router import INTENT_LOOKUP
from app.services.chat.router import INTENT_SUMMARIZE
from app.services.chat.router import INTENT_TEMPORAL
from app.services.chat.router import INTENTS
from app.services.chat.router import TIER_AGGREGATE
from app.services.chat.router import TIER_CHUNK
from app.services.chat.router import TIER_DIGEST
from app.services.chat.router import TIERS_BY_INTENT
from app.services.chat.router import classify
from app.services.chat.router import extract_temporal
from app.services.chat.router import parse_intent_line
from app.services.chat.router import route

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------- labels


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Aggregate: a counting head AND a corpus-wide marker.
        ("How many meetings discussed the Cypress Hearth compliance audit?", INTENT_AGGREGATE),
        ("Which meetings mention the Slate Viaduct exercise? List them.", INTENT_AGGREGATE),
        ("How many times in total did we defer the headcount request?", INTENT_AGGREGATE),
        (
            "Who attended the most vendor review board sessions for the tooling team?",
            INTENT_AGGREGATE,
        ),
        ("Which speakers discussed the migration?", INTENT_AGGREGATE),
        # Summarize: a strong marker anywhere, or an imperative at the start.
        (
            "Summarise what the architecture forum covered across all of its sessions.",
            INTENT_SUMMARIZE,
        ),
        ("Give me a recap of the Atlas kickoff.", INTENT_SUMMARIZE),
        ("Describe the team's disagreement about the remote.", INTENT_SUMMARIZE),
        ("What were the key decisions?", INTENT_SUMMARIZE),
        # Temporal: a date axis with no counting head.
        ("When did we first agree the latency budget?", INTENT_TEMPORAL),
        ("What has changed since the March review?", INTENT_TEMPORAL),
        ("What did we cover in the most recent retro?", INTENT_TEMPORAL),
        # Lookup: the default.
        ("What was the supplier we selected for the Pewter Cascade programme?", INTENT_LOOKUP),
        (
            "Across the incident review sessions for the billing team, what was the peak "
            "throughput we measured?",
            INTENT_LOOKUP,
        ),
    ],
)
def test_classify_assigns_the_expected_label(text, expected):
    intent, signals = classify(text)
    assert intent == expected
    if expected != INTENT_LOOKUP:
        assert signals, f"{expected!r} must record which signal produced it"


def test_classify_returns_no_signals_for_a_plain_lookup():
    """The default is reached by nothing firing, and says so."""
    intent, signals = classify("What was the supplier we selected?")
    assert intent == INTENT_LOOKUP
    assert signals == ()


def test_empty_query_is_a_lookup_and_not_a_crash():
    assert classify("") == (INTENT_LOOKUP, ())
    assert classify("   ") == (INTENT_LOOKUP, ())


# ------------------------------------------------- the two measured regressions


def test_singular_which_meeting_is_a_lookup_not_an_aggregation():
    """MEASURED REGRESSION: this routed 100 verbatim-control lookups to aggregate.

    "Which meeting recorded 10,000 requests per second?" asks to identify one
    recording by something said in it — the chunk plane answers it with the
    passage. The plural form enumerates the corpus and is a real aggregation.
    """
    singular, _ = classify("Which meeting recorded 10,000 requests per second?")
    plural, _ = classify("Which meetings recorded a latency regression? List them.")
    assert singular == INTENT_LOOKUP
    assert plural == INTENT_AGGREGATE


def test_today_in_reported_speech_is_not_a_temporal_query():
    """MEASURED REGRESSION: 'today' inside a question about what was said."""
    intent, _ = classify("Why were the thanks expressed to the House of Commons today?")
    assert intent == INTENT_LOOKUP


def test_counting_head_without_a_corpus_noun_stays_a_lookup():
    """The real QMSum query that made the corpus-noun requirement necessary.

    It is the only one of 1,172 human lookup queries carrying an aggregate head,
    and it is a question about what was said in one meeting — answering it with
    a corpus count would replace a good answer with a wrong number.
    """
    intent, _ = classify(
        "How many projects that the provinces had submitted were waiting for "
        "approval from the government?"
    )
    assert intent == INTENT_LOOKUP


def test_weak_summarize_markers_need_a_discourse_noun():
    """'the pragmatic overview of the project' is a lookup; 'overview of the meeting' is not."""
    topic, _ = classify("What did the professor think about the pragmatic overview of the project?")
    discourse, _ = classify("Give me an overview of the meeting.")
    assert topic == INTENT_LOOKUP
    assert discourse == INTENT_SUMMARIZE


# ---------------------------------------------------------------- the invariants


def test_every_intent_keeps_the_chunk_tier():
    """The invariant that bounds the cost of a misroute, over the whole label set."""
    assert set(TIERS_BY_INTENT) == set(INTENTS)
    for intent in INTENTS:
        assert TIER_CHUNK in TIERS_BY_INTENT[intent], f"{intent} dropped the chunk tier"


@pytest.mark.parametrize("speakers", [None, ["Dana Whitfield"]])
@pytest.mark.parametrize(
    "question",
    [
        "Summarise the Atlas kickoff.",
        "How many meetings discussed Atlas?",
        "What was the supplier we selected?",
        "When did we agree the budget?",
    ],
)
def test_structure_narrows_tiers_and_never_changes_the_label(question, speakers):
    """Structure may only REMOVE a non-chunk tier. Promotion is what D5 forbids."""
    plain = route(question)
    structured = route(question, speakers=speakers)
    assert structured.intent == plain.intent
    assert TIER_CHUNK in structured.tiers
    assert set(structured.tiers) <= set(plain.tiers)


def test_a_speaker_filter_removes_the_digest_tier():
    """A digest carries no single-valued speaker field, and its speakers array
    goes stale after a rename until the next reindex."""
    without = route("Summarise the Atlas kickoff.")
    with_speaker = route("Summarise the Atlas kickoff.", speakers=["Dana Whitfield"])
    assert with_speaker.intent == INTENT_SUMMARIZE
    assert without.wants_digest is True
    assert with_speaker.wants_digest is False
    assert TIER_CHUNK in with_speaker.tiers


def test_a_quoted_phrase_removes_the_digest_tier():
    decision = route('Summarise what was said about "the Pewter Cascade programme".')
    assert decision.literal is True
    assert decision.wants_digest is False
    assert decision.intent == INTENT_SUMMARIZE


def test_aggregate_route_asks_for_the_aggregate_tier_and_keeps_chunks():
    decision = route("How many meetings discussed the compliance audit?")
    assert decision.wants_aggregate is True
    assert decision.tiers[0] == TIER_AGGREGATE
    assert TIER_CHUNK in decision.tiers


def test_summarize_route_leads_with_the_digest_tier():
    decision = route("Summarise the architecture forum sessions.")
    assert decision.tiers == (TIER_DIGEST, TIER_CHUNK)


# ------------------------------------------------------- original vs rewritten


def test_the_more_specific_of_original_and_rewritten_wins():
    """A rewrite resolves pronouns and can lose the verb that carried the intent."""
    decision = route(
        "summarize that",
        rewritten="the Q3 revenue discussion in the Atlas kickoff",
    )
    assert decision.intent == INTENT_SUMMARIZE
    assert decision.source == "rules"


def test_a_rewrite_that_recovers_the_intent_is_credited_to_the_rewrite():
    decision = route(
        "and those?",
        rewritten="Which meetings mention the Pewter Cascade programme? List them.",
    )
    assert decision.intent == INTENT_AGGREGATE
    assert decision.source == "rules:rewritten"


def test_a_rewrite_can_never_lower_the_specificity_of_the_original():
    decision = route(
        "Summarise the Atlas kickoff.",
        rewritten="What was the supplier we selected for Atlas?",
    )
    assert decision.intent == INTENT_SUMMARIZE


# --------------------------------------------------------------- the LLM signal


def test_the_llm_intent_line_breaks_a_no_signal_default():
    decision = route("and what about the rest of them?", llm_intent=INTENT_SUMMARIZE)
    assert decision.intent == INTENT_SUMMARIZE
    assert decision.source == "llm"
    assert decision.signals == ("llm-intent-line",)


def test_the_llm_intent_line_cannot_override_a_rule_match():
    """The rules are deterministic evidence; a model's guess is a tiebreak only."""
    decision = route(
        "How many meetings discussed the compliance audit?", llm_intent=INTENT_SUMMARIZE
    )
    assert decision.intent == INTENT_AGGREGATE
    assert decision.source == "rules"


def test_an_unusable_llm_intent_leaves_the_default_standing():
    decision = route("and what about the rest of them?", llm_intent="wibble")
    assert decision.intent == INTENT_LOOKUP
    assert decision.source == "default"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("the rewritten query\nINTENT: aggregate", INTENT_AGGREGATE),
        ("the rewritten query\nintent: summarize", INTENT_SUMMARIZE),
        ("the rewritten query\nINTENT: lookup.", INTENT_LOOKUP),
        ("the rewritten query", None),
        ("the rewritten query\nINTENT: nonsense", None),
        ("INTENT: aggregate", None),  # line 1 is the query, never the intent
        ("", None),
    ],
)
def test_parse_intent_line(raw, expected):
    assert parse_intent_line(raw) == expected


# ------------------------------------------------------------------- temporal


def test_extract_temporal_reads_month_and_year():
    hint = extract_temporal("How many meetings in January 2025 discussed the audit?")
    assert hint is not None
    assert (hint.year, hint.month) == (2025, 1)


def test_extract_temporal_reads_an_iso_date():
    hint = extract_temporal("What did we decide on 2025-03-14?")
    assert hint is not None
    assert (hint.year, hint.month) == (2025, 3)


def test_an_absolute_date_beats_a_relative_one():
    """A question carrying both means the exact one."""
    hint = extract_temporal("What changed since March 2025 in the most recent review?")
    assert hint is not None
    assert (hint.year, hint.month) == (2025, 3)
    assert hint.relative is None


def test_extract_temporal_returns_none_when_nothing_is_time_shaped():
    assert extract_temporal("What was the supplier we selected?") is None


def test_a_temporal_hint_rides_along_on_an_aggregate_route():
    """The whole point of 'how many meetings in March' is the filter."""
    decision = route("How many meetings in March 2025 discussed the audit?")
    assert decision.intent == INTENT_AGGREGATE
    assert decision.temporal is not None
    assert (decision.temporal.year, decision.temporal.month) == (2025, 3)


# ------------------------------------------------------------------- metadata


def test_as_metadata_carries_the_decision_and_its_evidence():
    payload = route("How many meetings in March 2025 discussed the audit?").as_metadata()
    assert payload["intent"] == INTENT_AGGREGATE
    assert payload["tiers"] == [TIER_AGGREGATE, TIER_CHUNK]
    assert payload["source"] == "rules"
    assert payload["signals"], "a rules decision must record which patterns fired"
    assert payload["temporal"]["month"] == 3


def test_as_metadata_omits_absent_facts_rather_than_reporting_nulls():
    payload = route("What was the supplier we selected?").as_metadata()
    assert "temporal" not in payload
    assert "literal" not in payload
    assert payload["intent"] == INTENT_LOOKUP
