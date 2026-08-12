"""Aggregation-class query planning (rules R3-R7).

These are the queries #403 says must be answered from OpenSearch aggs or Postgres rather
than from ranked retrieval, and they are the class with **no publishable real-data ground
truth at all** — QMSum queries are single-meeting by construction and ELITR's cross-meeting
signal is Tier B. Every answer here is an integer or a set computed from the write log and
then re-derived by brute force in ``validate.py``.

Markers are multi-word phrases built on a uniquely-allocated codename ("the Cedar Lantern
compliance audit"), so "how many meetings discussed X" has one true answer and a plain
substring scan can confirm it.
"""

from __future__ import annotations

from .model import MONTHS
from .model import PlannedQuery
from .org import Org
from .rng import Rng

MARKER_FRAMES = (
    "the {code} compliance audit",
    "the {code} readiness review",
    "the {code} data-retention exercise",
    "the {code} cost-recovery programme",
    "the {code} accessibility assessment",
)
EVENT_FRAMES = (
    "the {code} budget line",
    "the {code} headcount request",
    "the {code} tooling renewal",
)


def plan_aggregation(org: Org, rng: Rng, counts: dict) -> list[PlannedQuery]:
    """Plan the aggregation class, cycling through rules R3-R7.

    Args:
        org: The organisation; ``session.plants`` is appended to.
        rng: Planner RNG.
        counts: The ``queries`` block of the config.

    Returns:
        The planned aggregation queries.
    """
    out: list[PlannedQuery] = []
    builders = (_count_files, _list_files, _count_events, _speaker_top, _temporal_count)
    index = 0
    attempts = 0
    while len(out) < counts["aggregation"] and attempts < counts["aggregation"] * 20:
        attempts += 1
        built = builders[index % len(builders)](org, rng, len(out))
        index += 1
        if built is not None:
            out.append(built)
    return out


def _plant_marker(org: Org, rng: Rng, phrase: str, query_id: str, spread: int) -> list[str]:
    """Plant ``phrase`` in ``spread`` distinct sessions; return their uuids, sorted."""
    picks = rng.sample(org.sessions, min(spread, len(org.sessions)))
    for session in picks:
        session.plants.append(
            {"kind": "marker", "query_id": query_id, "phrase": phrase, "repeats": 1}
        )
    return sorted(s.file_uuid for s in picks)


def _count_files(org: Org, rng: Rng, index: int) -> PlannedQuery:
    """Rule R3 — "how many meetings discussed X?" with an exact integer answer."""
    qid = f"ag-{index:05d}"
    phrase = rng.choice(MARKER_FRAMES).format(code=org.allocator.allocate("codename"))
    gold = _plant_marker(org, rng, phrase, qid, rng.randint(3, 12))
    return PlannedQuery(
        query_id=qid,
        query_class="aggregation",
        surface="paraphrase",
        rule="R3-agg-count-files",
        text=f"How many meetings discussed {phrase}?",
        gold_files=gold,
        answer=len(gold),
        answer_kind="integer",
        scored_on="answer",
    )


def _list_files(org: Org, rng: Rng, index: int) -> PlannedQuery:
    """Rule R4 — "which meetings mention X?" with an exact file set as the answer."""
    qid = f"ag-{index:05d}"
    phrase = rng.choice(MARKER_FRAMES).format(code=org.allocator.allocate("codename"))
    gold = _plant_marker(org, rng, phrase, qid, rng.randint(2, 8))
    return PlannedQuery(
        query_id=qid,
        query_class="aggregation",
        surface="paraphrase",
        rule="R4-agg-list-files",
        text=f"Which meetings mention {phrase}? List them.",
        gold_files=gold,
        answer=gold,
        answer_kind="file_set",
        scored_on="answer",
    )


def _count_events(org: Org, rng: Rng, index: int) -> PlannedQuery:
    """Rule R5 — "how many times did we defer X?"; repeats within a file count."""
    qid = f"ag-{index:05d}"
    phrase = rng.choice(EVENT_FRAMES).format(code=org.allocator.allocate("codename"))
    picks = rng.sample(org.sessions, min(rng.randint(2, 6), len(org.sessions)))
    total = 0
    for session in picks:
        repeats = rng.randint(1, 3)
        total += repeats
        session.plants.append(
            {"kind": "event", "query_id": qid, "phrase": phrase, "repeats": repeats}
        )
    return PlannedQuery(
        query_id=qid,
        query_class="aggregation",
        surface="paraphrase",
        rule="R5-agg-count-events",
        text=f"How many times in total did we defer {phrase}?",
        gold_files=sorted(s.file_uuid for s in picks),
        answer=total,
        answer_kind="integer",
        scored_on="answer",
    )


def _speaker_top(org: Org, rng: Rng, index: int) -> PlannedQuery | None:
    """Rule R6 — "who attended the most sessions of this series?"; strict max required.

    A tied maximum has two correct answers, so a series whose attendance ties is skipped
    rather than emitted — that ambiguity is the exact defect this tier exists to avoid.
    Ties are common with small rosters, so the rule scans candidate series in a seeded
    order and takes the first usable one instead of rejecting a single random draw; a
    single draw produced zero R6 queries on a 60-meeting corpus and left V5 unexercised.
    """
    candidates = rng.shuffled(
        sorted(s.series_id for s in org.series.values() if len(s.sessions) >= 6)
    )
    series = None
    ranked: list[tuple[str, int]] = []
    for series_id in candidates:
        if series_id in org.used_series:
            continue
        found = org.series[series_id]
        tally: dict[str, int] = {}
        for session in found.sessions:
            for person_id in session.attendees:
                tally[person_id] = tally.get(person_id, 0) + 1
        ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) >= 2 and ranked[0][1] > ranked[1][1]:
            series = found
            org.used_series.add(series_id)
            break
    if series is None:
        return None
    team = org.teams[series.team_id]
    winner = next(p for p in team.roster if p.person_id == ranked[0][0])
    return PlannedQuery(
        query_id=f"ag-{index:05d}",
        query_class="aggregation",
        surface="paraphrase",
        rule="R6-agg-speaker-top",
        text=f"Who attended the most {series.kind} sessions for {team.label}?",
        gold_files=sorted(s.file_uuid for s in series.sessions),
        answer={"speaker": winner.name, "sessions": ranked[0][1]},
        answer_kind="speaker_count",
        scored_on="answer",
        team_id=team.team_id,
        series_id=series.series_id,
    )


def _temporal_count(org: Org, rng: Rng, index: int) -> PlannedQuery | None:
    """Rule R7 — "how many meetings in <month> discussed X?"; a filtered count."""
    qid = f"ag-{index:05d}"
    phrase = rng.choice(MARKER_FRAMES).format(code=org.allocator.allocate("codename"))
    hits = _plant_marker(org, rng, phrase, qid, rng.randint(4, 14))
    by_month: dict[str, list[str]] = {}
    for file_uuid in hits:
        month_key = org.session_by_uuid(file_uuid).date[:7]
        by_month.setdefault(month_key, []).append(file_uuid)
    if not by_month:
        return None
    month_key = sorted(by_month.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]
    in_month = sorted(by_month[month_key])
    year, month = month_key.split("-")
    return PlannedQuery(
        query_id=qid,
        query_class="aggregation",
        surface="paraphrase",
        rule="R7-agg-temporal-count",
        text=f"How many meetings in {MONTHS[int(month) - 1]} {year} discussed {phrase}?",
        gold_files=in_month,
        answer=len(in_month),
        answer_kind="integer",
        scored_on="answer",
        related_files=sorted(set(hits) - set(in_month)),
    )
