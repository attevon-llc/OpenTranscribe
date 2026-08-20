"""Speaker-attribution scoring (#461 W2.E1): SPEAKER_ATTR, SPEAKER_SUMMARY, ATTRIBUTION_PROBE.

A **separate engine** from :mod:`tests.eval.harness.answers`, on purpose — same reason
that module gives aggregation its own engine rather than nDCG: these measures answer a
different question than either retrieval ranking or exact-match counting, and mixing
them into one table would make an attribution accuracy legible as a retrieval score.

All three query classes are carved or planted from data the harness already loads —
QMSum's own human-authored queries, already licensed and already on disk — so nothing
here needs new corpus data:

* **SPEAKER_ATTR** — QMSum ``specific_query_list`` entries matching an attribution
  pattern ("according to X", "what did X say") whose gold turn span is spoken by
  exactly one speaker. Scored on **two sub-measures that are never merged into one
  number**: whether the answer names the right speaker, and whether the excerpts it
  cited are themselves attributed to that speaker. A blended score would hide the
  case that matters most — a model that names the right person while citing the
  wrong speaker's words as evidence.
* **SPEAKER_SUMMARY** — the same carving applied to QMSum's SUMMARIZE-class queries
  ("Summarize X's view on Y"), scored on ``speaker_coverage``: whether the required
  speaker is among the speakers a generated summary actually covers.
* **ATTRIBUTION_PROBE** — a synthetic negative for each SPEAKER_ATTR case: a decoy
  speaker who was genuinely present in the same meeting but did NOT say the quoted
  material. Scored on ``false_attribution_rate`` — whether a submitted answer
  confirms the decoy. Deterministic (no randomness beyond the fixed decoy-selection
  rule in ``corpora.py``) and license-free (reuses the SPEAKER_ATTR gold already
  derived from licensed QMSum data; plants no new text).

Every ``evaluate_*`` function follows the same ``trec_eval -c`` convention as the rest
of this harness: the **gold** query set is the denominator, an unanswered query scores
zero on every measure and is still counted, and the per-query row count is asserted
against the gold count so a silent partial score is a raised error, not a flattering
mean.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from tests.eval.harness.answers import ATTRIBUTION_PROBE_KIND
from tests.eval.harness.answers import SPEAKER
from tests.eval.harness.answers import Answer
from tests.eval.harness.answers import normalise_name

#: SPEAKER_ATTR. Never merged: a model can name the right speaker while citing the
#: wrong one's words, and a blended score would hide exactly that failure.
ATTR_MEASURES = ("answer_names_gold_speaker", "citation_speaker_match", "answered")

#: SPEAKER_SUMMARY.
SUMMARY_MEASURES = ("speaker_coverage", "answered")

#: ATTRIBUTION_PROBE. Lower is better — see ``significance.MEASURE_DIRECTION``.
PROBE_MEASURES = ("false_attribution_rate", "answered")


@dataclass(frozen=True)
class SubmittedAttribution:
    """A system's answer to a SPEAKER_ATTR query.

    Attributes:
        speaker: Who the system says the excerpt is attributed to (its answer's
            named speaker), or ``""`` if it did not name one.
        citation_speakers: The ``speaker`` field of every excerpt the answer cited,
            in citation order. Read from the chunk metadata the citation resolves
            to, never from the model's own claim about who it is quoting.
    """

    speaker: str
    citation_speakers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubmittedCoverage:
    """A system's summary, reduced to the speakers it covered."""

    covered_speakers: tuple[str, ...] = ()


def score_attribution_one(gold: Answer, submitted: SubmittedAttribution | None) -> dict[str, float]:
    """Score one SPEAKER_ATTR query. Returns a value for every :data:`ATTR_MEASURES` name.

    Args:
        gold: ``Answer(SPEAKER, name)`` — the one correct speaker.
        submitted: The system's answer, or ``None`` if it declined.

    Raises:
        ValueError: ``gold.kind`` is not :data:`tests.eval.harness.answers.SPEAKER`.
    """
    if gold.kind != SPEAKER:
        raise ValueError(f"score_attribution_one: gold.kind must be {SPEAKER!r}, got {gold.kind!r}")
    if submitted is None:
        return dict.fromkeys(ATTR_MEASURES, 0.0)

    gold_name = normalise_name(gold.value)
    names_gold = float(normalise_name(submitted.speaker) == gold_name)

    if not submitted.citation_speakers:
        # Nothing cited counts as zero support, same as an aggregation query that
        # answered with an empty file set: absence of evidence scores as no evidence.
        citation_match = 0.0
    else:
        matching = sum(
            1 for speaker in submitted.citation_speakers if normalise_name(speaker) == gold_name
        )
        citation_match = matching / len(submitted.citation_speakers)

    return {
        "answer_names_gold_speaker": names_gold,
        "citation_speaker_match": citation_match,
        "answered": 1.0,
    }


def score_coverage_one(gold: Answer, submitted: SubmittedCoverage | None) -> dict[str, float]:
    """Score one SPEAKER_SUMMARY query. Returns a value for every :data:`SUMMARY_MEASURES` name."""
    if gold.kind != SPEAKER:
        raise ValueError(f"score_coverage_one: gold.kind must be {SPEAKER!r}, got {gold.kind!r}")
    if submitted is None:
        return dict.fromkeys(SUMMARY_MEASURES, 0.0)
    gold_name = normalise_name(gold.value)
    covered = float(
        any(normalise_name(speaker) == gold_name for speaker in submitted.covered_speakers)
    )
    return {"speaker_coverage": covered, "answered": 1.0}


def score_probe_one(gold: Answer, submitted: Answer | None) -> dict[str, float]:
    """Score one ATTRIBUTION_PROBE case. Returns a value for every :data:`PROBE_MEASURES` name.

    Args:
        gold: ``Answer(ATTRIBUTION_PROBE_KIND, (true_speaker, decoy_speaker))``.
        submitted: ``Answer(SPEAKER, name)`` — who the system claims said the
            quoted material — or ``None`` if it declined to attribute at all.
            **A decline cannot be a false positive**: only a submitted answer that
            actively names the decoy counts against the rate.
    """
    if gold.kind != ATTRIBUTION_PROBE_KIND:
        raise ValueError(
            f"score_probe_one: gold.kind must be {ATTRIBUTION_PROBE_KIND!r}, got {gold.kind!r}"
        )
    if submitted is None:
        return {"false_attribution_rate": 0.0, "answered": 0.0}
    if submitted.kind != SPEAKER:
        raise ValueError(
            f"score_probe_one: submitted.kind must be {SPEAKER!r}, got {submitted.kind!r}"
        )
    _true_speaker, decoy_speaker = gold.value
    confirmed_decoy = normalise_name(submitted.value) == normalise_name(decoy_speaker)
    return {"false_attribution_rate": float(confirmed_decoy), "answered": 1.0}


@dataclass
class AttributionResult:
    """Per-query and aggregate scores, plus the denominator used."""

    per_query: dict[str, dict[str, float]] = field(default_factory=dict)
    aggregate: dict[str, float] = field(default_factory=dict)
    query_count: int = 0
    unanswered: list[str] = field(default_factory=list)


def _evaluate(
    gold: Mapping[str, Answer],
    submitted: Mapping[str, Any],
    *,
    measures: tuple[str, ...],
    score_one,
) -> AttributionResult:
    """Shared ``trec_eval -c``-style evaluation loop for all three query classes."""
    if not gold:
        raise ValueError("Refusing to evaluate against an empty gold set")
    result = AttributionResult(query_count=len(gold))
    for query_id in sorted(gold):
        answer = submitted.get(query_id)
        if answer is None:
            result.unanswered.append(query_id)
        result.per_query[query_id] = score_one(gold[query_id], answer)
    if len(result.per_query) != len(gold):
        raise ValueError(
            f"Scored {len(result.per_query)} answers against {len(gold)} gold — "
            "the zero substitution did not cover the query set"
        )
    for name in measures:
        values = [row[name] for row in result.per_query.values()]
        result.aggregate[name] = sum(values) / len(values)
    return result


def evaluate_attribution(
    gold: Mapping[str, Answer], submitted: Mapping[str, SubmittedAttribution | None]
) -> AttributionResult:
    """Score SPEAKER_ATTR queries. See :func:`score_attribution_one`."""
    return _evaluate(gold, submitted, measures=ATTR_MEASURES, score_one=score_attribution_one)


def evaluate_speaker_summary(
    gold: Mapping[str, Answer], submitted: Mapping[str, SubmittedCoverage | None]
) -> AttributionResult:
    """Score SPEAKER_SUMMARY queries. See :func:`score_coverage_one`."""
    return _evaluate(gold, submitted, measures=SUMMARY_MEASURES, score_one=score_coverage_one)


def evaluate_attribution_probe(
    gold: Mapping[str, Answer], submitted: Mapping[str, Answer | None]
) -> AttributionResult:
    """Score ATTRIBUTION_PROBE cases. See :func:`score_probe_one`."""
    return _evaluate(gold, submitted, measures=PROBE_MEASURES, score_one=score_probe_one)


def subset_attribution(result: AttributionResult, query_ids: set[str]) -> AttributionResult:
    """Re-aggregate over a subset (one corpus, one class), keeping the denominator."""
    rows = {qid: row for qid, row in result.per_query.items() if qid in query_ids}
    out = AttributionResult(
        per_query=rows,
        query_count=len(rows),
        unanswered=[qid for qid in result.unanswered if qid in query_ids],
    )
    if not rows:
        return out
    for name in next(iter(rows.values())):
        out.aggregate[name] = sum(row[name] for row in rows.values()) / len(rows)
    return out
