"""Query planning — the module that *defines* ground truth.

Every query is produced by exactly one numbered rule, recorded in its ``rule`` field, so
a reviewer can trace any gold set back to the code path that created it. Two invariants
make the gold sets exact rather than approximate:

* **Answer uniqueness.** A ``(team, topic, aspect)`` triple is claimed by at most one
  query corpus-wide. Without that, "what deadline did we commit to for the ingest
  gateway?" could have two true answers in different sessions and the qrels would be
  wrong in a way no amount of retrieval tuning could fix.
* **Component disjointness.** For a multi-file query with N components, each component
  goes into a *different* session, so no proper subset of the gold set answers the
  question. This is enforced at plan time and re-checked by brute force in
  ``validate.py``.

No LLM is involved at any point. Every string is a template fill.
"""

from __future__ import annotations

from .aggregation import plan_aggregation
from .facts import ASPECTS
from .model import MONTHS  # noqa: F401  (re-export: historical import site)
from .model import PlannedQuery
from .org import Org
from .org import Session
from .rng import Rng
from .rng import derive_seed


class _Claims:
    """Tracks which ``(team, topic, aspect)`` triples are already spoken for."""

    def __init__(self) -> None:
        self._taken: set[tuple[str, str, str]] = set()

    def take(self, team_id: str, topic: str, aspect: str) -> bool:
        """Claim a triple; return False if another query already owns it."""
        key = (team_id, topic, aspect)
        if key in self._taken:
            return False
        self._taken.add(key)
        return True


def plan_queries(org: Org, config: dict) -> list[PlannedQuery]:
    """Plan every query and attach its plants to the sessions that will carry them.

    Args:
        org: The organisation from :func:`org.build_org`. Mutated: ``session.plants``
            is populated.
        config: The corpus config.

    Returns:
        Queries sorted by ``query_id``.
    """
    rng = Rng(derive_seed(config["seed"], "queries"))
    claims = _Claims()
    counts = config["queries"]
    out: list[PlannedQuery] = []
    out.extend(_plan_lookup(org, rng, claims, counts, config))
    out.extend(_plan_multi_file(org, rng, claims, counts))
    out.extend(plan_aggregation(org, rng, counts))
    out.extend(_plan_summarize(org, rng, counts))
    out.sort(key=lambda q: q.query_id)
    return out


def _eligible_series(org: Org, min_sessions: int) -> list[str]:
    """Series ids with at least ``min_sessions`` sessions, in deterministic order."""
    return sorted(s.series_id for s in org.series.values() if len(s.sessions) >= min_sessions)


def _plant(session: Session, record: dict) -> None:
    session.plants.append(record)


def _plan_lookup(
    org: Org, rng: Rng, claims: _Claims, counts: dict, config: dict
) -> list[PlannedQuery]:
    """Rule R1 — one fact in exactly one meeting; gold is the planting site.

    A ``verbatim`` twin is emitted for a fixed fraction of facts: same gold, but the
    query quotes the anchor. It is the deliberately-easy control that shows how much of
    the paraphrase set's difficulty comes from wording rather than from the corpus.
    """
    out: list[PlannedQuery] = []
    series_ids = _eligible_series(org, 1)
    n = counts["lookup"]
    verbatim_every = max(1, round(1 / max(config["queries"]["verbatim_control_fraction"], 1e-9)))
    attempt = 0
    while len(out) < n and attempt < n * 25:
        attempt += 1
        series = org.series[rng.choice(series_ids)]
        team = org.teams[series.team_id]
        topic = rng.choice(team.topics)
        aspect = rng.choice(ASPECTS)
        if not claims.take(team.team_id, topic, aspect.name):
            continue
        session = rng.choice(series.sessions)
        anchor = org.allocator.allocate(aspect.anchor_kind)
        qid = f"lk-{len(out):05d}"
        fact_id = f"f-{qid}"
        _plant(
            session,
            {
                "kind": "fact",
                "fact_id": fact_id,
                "query_id": qid,
                "aspect": aspect.name,
                "anchor": anchor,
                "topic": topic,
            },
        )
        out.append(
            PlannedQuery(
                query_id=qid,
                query_class="lookup",
                surface="paraphrase",
                rule="R1-lookup-single-fact",
                text=f"In the {series.kind} for {team.label}, what was "
                f"{aspect.query_phrase} for {topic}?",
                gold_files=[session.file_uuid],
                answer=anchor,
                answer_kind=aspect.anchor_kind,
                team_id=team.team_id,
                series_id=series.series_id,
                topic=topic,
                components=[
                    {
                        "fact_id": fact_id,
                        "aspect": aspect.name,
                        "anchor": anchor,
                        "file_uuid": session.file_uuid,
                    }
                ],
            )
        )
        if len(out) % verbatim_every == 0:
            out.append(
                PlannedQuery(
                    query_id=f"lkv-{len(out):05d}",
                    query_class="lookup",
                    surface="verbatim",
                    rule="R1v-lookup-verbatim-control",
                    text=f"Which meeting recorded {anchor}?",
                    gold_files=[session.file_uuid],
                    answer=anchor,
                    answer_kind=aspect.anchor_kind,
                    team_id=team.team_id,
                    series_id=series.series_id,
                    topic=topic,
                    components=[
                        {
                            "fact_id": fact_id,
                            "aspect": aspect.name,
                            "anchor": anchor,
                            "file_uuid": session.file_uuid,
                        }
                    ],
                )
            )
    return out


def _plan_multi_file(org: Org, rng: Rng, claims: _Claims, counts: dict) -> list[PlannedQuery]:
    """Rule R2 — N components of one composite answer, one per distinct file."""
    out: list[PlannedQuery] = []
    series_ids = _eligible_series(org, 2)
    if not series_ids:
        return out
    n = counts["multi_file"]
    attempt = 0
    while len(out) < n and attempt < n * 25:
        attempt += 1
        series = org.series[rng.choice(series_ids)]
        team = org.teams[series.team_id]
        width = min(len(series.sessions), rng.randint(2, 4))
        topic = rng.choice(team.topics)
        aspects = rng.sample(ASPECTS, width)
        if any(not claims.take(team.team_id, topic, a.name) for a in aspects):
            continue
        picks = rng.sample(series.sessions, width)
        qid = f"mf-{len(out):05d}"
        components = []
        for i, (aspect, session) in enumerate(zip(aspects, picks, strict=True)):
            anchor = org.allocator.allocate(aspect.anchor_kind)
            fact_id = f"f-{qid}-{i}"
            _plant(
                session,
                {
                    "kind": "fact",
                    "fact_id": fact_id,
                    "query_id": qid,
                    "aspect": aspect.name,
                    "anchor": anchor,
                    "topic": topic,
                },
            )
            components.append(
                {
                    "fact_id": fact_id,
                    "aspect": aspect.name,
                    "anchor": anchor,
                    "file_uuid": session.file_uuid,
                }
            )
        phrases = [a.query_phrase for a in aspects]
        joined = ", ".join(phrases[:-1]) + f", and {phrases[-1]}"
        out.append(
            PlannedQuery(
                query_id=qid,
                query_class="multi_file",
                surface="paraphrase",
                rule="R2-multifile-composite",
                text=f"Across the {series.kind} sessions for {team.label}, what was {joined} "
                f"for {topic}?",
                gold_files=sorted(c["file_uuid"] for c in components),
                answer={c["aspect"]: c["anchor"] for c in components},
                answer_kind="component_map",
                team_id=team.team_id,
                series_id=series.series_id,
                topic=topic,
                components=components,
            )
        )
    return out


def _plan_summarize(org: Org, rng: Rng, counts: dict) -> list[PlannedQuery]:
    """Rule R8 — a whole series; gold is every session of it (file level only)."""
    out: list[PlannedQuery] = []
    candidates = _eligible_series(org, 4)
    if not candidates:
        return out
    for i, series_id in enumerate(
        rng.sample(candidates, min(counts["summarize"], len(candidates)))
    ):
        series = org.series[series_id]
        team = org.teams[series.team_id]
        agenda = sorted({topic for s in series.sessions for topic in s.agenda})
        out.append(
            PlannedQuery(
                query_id=f"sm-{i:05d}",
                query_class="summarize",
                surface="paraphrase",
                rule="R8-summarize-series",
                text=f"Summarise what the {series.kind} for {team.label} covered across all "
                f"of its sessions.",
                gold_files=sorted(s.file_uuid for s in series.sessions),
                answer={"agenda": agenda, "sessions": len(series.sessions)},
                answer_kind="series_skeleton",
                team_id=team.team_id,
                series_id=series.series_id,
            )
        )
    return out
