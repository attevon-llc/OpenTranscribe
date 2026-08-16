"""The routed harness stage (#403 Stage 4): proving it measures something new.

The stage exists because the harness drove ``retrieve_chunks`` directly, with no
router in the loop — so a run named "the digest leg" would have measured the
chunk plane unchanged and produced a null delta that reads, to anyone who finds
the baseline later, as *"the digest tier is neutral"*.

The failure mode this file guards is therefore **the stage silently falling
through to the control**. A `route` stage that forgot to call the digest leg
would still run, still produce numbers, and still write a baseline — one
byte-identical to `stage='retrieve'` under a different name. So the first test
does not check that routing works; it checks that the digest leg is *reached*,
and it fails if the call never happens.

Nothing here touches OpenSearch. The two retrieval functions are substituted at
their import site so the test can assert **which tiers were queried**, which is
the half a live run cannot observe once the engine has answered.
"""

from __future__ import annotations

import pytest

from tests.eval.harness.corpora import EvalQuery
from tests.eval.harness.qrels import GoldSpan
from tests.eval.harness.routing import build_digest_leg_report
from tests.eval.harness.runner import RetrievalConfig
from tests.eval.harness.runner import RouteRecord
from tests.eval.harness.runner import execute

pytestmark = pytest.mark.unit

GOLD = "gold-file-uuid"
OTHER = "other-file-uuid"

SUMMARIZE_TEXT = "Summarise what the architecture forum covered across all of its sessions."
LOOKUP_TEXT = "What was the supplier we selected for the Pewter Cascade programme?"


class _Hit:
    """The two attributes the runner reads off a retrieval result."""

    def __init__(self, file_uuid: str, chunk_index: int, score: float = 1.0) -> None:
        self.file_uuid = file_uuid
        self.chunk_index = chunk_index
        self.score = score


def _query(query_id: str, text: str, query_class: str) -> EvalQuery:
    return EvalQuery(
        query_id=query_id,
        text=text,
        query_class=query_class,
        corpus="synthetic",
        license_tier="A",
        spans=(GoldSpan(GOLD, 1, 2),),
    )


@pytest.fixture
def legs(monkeypatch):
    """Substitute both retrieval legs and record what each was asked for."""
    calls: dict[str, list[str]] = {"chunks": [], "digests": []}
    returns: dict[str, list] = {
        "chunks": [_Hit(OTHER, 0), _Hit(OTHER, 1)],
        "digests": [_Hit(GOLD, -1)],
    }

    def _chunks(text, **_kwargs):
        calls["chunks"].append(text)
        return list(returns["chunks"])

    def _digests(text, **_kwargs):
        calls["digests"].append(text)
        return list(returns["digests"])

    monkeypatch.setattr("app.services.search.chunk_retrieval.retrieve_chunks", _chunks)
    monkeypatch.setattr("app.services.search.chunk_retrieval.retrieve_digests", _digests)
    return calls, returns


def _run(queries, legs, stage="route", **kwargs):
    records: dict[str, RouteRecord] = {}
    run = execute(
        queries,
        user_id=1,
        config=RetrievalConfig(stage=stage, workers=1, **kwargs),
        records=records if stage == "route" else None,
    )
    return run, records


# --------------------------------------------------- the fall-through guard


def test_a_summarize_query_actually_reaches_the_digest_leg(legs):
    """MUST-FIRE. If the stage falls through to the control, this is the test that dies."""
    calls, _returns = legs
    _run([_query("s-1", SUMMARIZE_TEXT, "summarize")], legs)

    assert calls["digests"] == [SUMMARIZE_TEXT], (
        "the digest leg was never queried — stage='route' has fallen through to "
        "stage='retrieve' and every number it produces is the control renamed"
    )
    assert calls["chunks"] == [SUMMARIZE_TEXT], "the chunk leg must still run alongside it"


def test_a_lookup_query_does_not_pay_for_the_digest_leg(legs):
    """The routed run must differ from the control ONLY where the router says so."""
    calls, _returns = legs
    _run([_query("l-1", LOOKUP_TEXT, "lookup")], legs)

    assert calls["digests"] == []
    assert calls["chunks"] == [LOOKUP_TEXT]


def test_the_unrouted_stage_never_touches_the_digest_leg(legs):
    """The control's definition, pinned: this is what makes the comparison mean something."""
    calls, _returns = legs
    _run([_query("s-1", SUMMARIZE_TEXT, "summarize")], legs, stage="retrieve")

    assert calls["digests"] == []
    assert calls["chunks"] == [SUMMARIZE_TEXT]


def test_route_without_a_records_dict_is_refused(legs):
    """A caller that drops the evidence gets the control under a new name. Refuse it."""
    with pytest.raises(RuntimeError, match="byte-identical"):
        execute(
            [_query("s-1", SUMMARIZE_TEXT, "summarize")],
            user_id=1,
            config=RetrievalConfig(stage="route", workers=1),
            records=None,
        )


# ------------------------------------------------------- the judgement space


def test_the_ranked_list_stays_chunk_only(legs):
    """Digest docs are unjudged by the qrels; ranking them would score the routed
    run worse than the control by construction, as an artefact of the judgement
    space rather than a fact about retrieval."""
    run, _records = _run([_query("s-1", SUMMARIZE_TEXT, "summarize")], legs)

    doc_ids = [doc.doc_id for doc in run["s-1"]]
    assert doc_ids, "the chunk leg's hits must still be ranked"
    assert not any("digest" in doc_id for doc_id in doc_ids)


def test_the_routed_and_unrouted_ranked_lists_are_identical(legs):
    """The corollary, stated as a test: the digest tier must not move nDCG.

    If this ever fails, the digest leg has leaked into the ranking and every
    phase-over-phase retrieval delta in the epic is contaminated.
    """
    routed, _records = _run([_query("s-1", SUMMARIZE_TEXT, "summarize")], legs)
    control, _none = _run([_query("s-1", SUMMARIZE_TEXT, "summarize")], legs, stage="retrieve")

    assert [d.doc_id for d in routed["s-1"]] == [d.doc_id for d in control["s-1"]]


# -------------------------------------------------------------- the records


def test_the_record_distinguishes_tier_not_asked_from_tier_returned_nothing(legs):
    _calls, returns = legs
    returns["digests"] = []
    _ranked, records = _run_pair(legs)

    asked = records["s-1"]
    not_asked = records["l-1"]
    assert "digest" in asked.tiers
    assert asked.digest_files == ()
    assert "digest" not in not_asked.tiers
    assert not_asked.digest_files == ()


def _run_pair(legs):
    return _run(
        [_query("s-1", SUMMARIZE_TEXT, "summarize"), _query("l-1", LOOKUP_TEXT, "lookup")],
        legs,
    )


def test_the_record_carries_each_legs_files_in_rank_order(legs):
    _calls, returns = legs
    returns["chunks"] = [_Hit(OTHER, 0), _Hit(GOLD, 5), _Hit(OTHER, 1)]
    _run_result, records = _run([_query("s-1", SUMMARIZE_TEXT, "summarize")], legs)

    record = records["s-1"]
    assert record.chunk_files == (OTHER, GOLD), "first occurrence wins, no duplicates"
    assert record.digest_files == (GOLD,)
    assert record.intent == "summarize"


# ---------------------------------------------------------------- the report


def _record(**kwargs) -> RouteRecord:
    defaults = {"intent": "summarize", "tiers": ("digest", "chunk")}
    return RouteRecord(**{**defaults, **kwargs})


def test_a_rescue_is_only_counted_when_the_chunk_leg_missed_it():
    """MUST-FIRE the other way: re-finding what the chunk leg already had is worth nothing."""
    query = _query("s-1", SUMMARIZE_TEXT, "summarize")
    redundant = {"s-1": _record(digest_files=(GOLD,), chunk_files=(GOLD, OTHER))}
    genuine = {"s-1": _record(digest_files=(GOLD,), chunk_files=(OTHER,))}

    assert build_digest_leg_report([query], redundant)["rescued"]["queries"] == 0
    assert build_digest_leg_report([query], genuine)["rescued"]["queries"] == 1


def test_a_gold_file_deep_in_the_chunk_legs_file_order_still_counts_as_rescued():
    """`chunk_files_at` is the depth at which the chunk leg is credited with having it."""
    query = _query("s-1", SUMMARIZE_TEXT, "summarize")
    deep = {
        "s-1": _record(
            digest_files=(GOLD,), chunk_files=tuple(f"f{n}" for n in range(12)) + (GOLD,)
        )
    }

    assert build_digest_leg_report([query], deep, chunk_files_at=10)["rescued"]["queries"] == 1
    assert build_digest_leg_report([query], deep, chunk_files_at=20)["rescued"]["queries"] == 0


def test_a_run_that_routed_nothing_to_the_digest_tier_says_so():
    """The denominator that makes "this measured nothing new" visible."""
    query = _query("l-1", LOOKUP_TEXT, "lookup")
    records = {"l-1": _record(intent="lookup", tiers=("chunk",), chunk_files=(OTHER,))}

    report = build_digest_leg_report([query], records)
    assert report["routed_to_digest_tier"] == 0
    assert report["rescued"]["rate"] == 0.0
    assert "measured nothing new" in report["caveat"]


def test_the_report_breaks_intent_down_by_query_class():
    queries = [
        _query("s-1", SUMMARIZE_TEXT, "summarize"),
        _query("l-1", LOOKUP_TEXT, "lookup"),
    ]
    records = {
        "s-1": _record(digest_files=(GOLD,), chunk_files=(OTHER,)),
        "l-1": _record(intent="lookup", tiers=("chunk",), chunk_files=(OTHER,)),
    }

    report = build_digest_leg_report(queries, records)
    assert report["intent_by_query_class"]["summarize"] == {"n": 1, "summarize": 1}
    assert report["intent_by_query_class"]["lookup"] == {"lookup": 1, "n": 1}
    assert report["queries_scored"] == 2
