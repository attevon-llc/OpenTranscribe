"""Answer scoring: the aggregation class, which no ranking metric can score.

#403's aggregation class carries ``scored_on: "answer"``. Its ground truth is an
integer, a file set or a speaker with a session count — "how many meetings
mentioned X", "which files discuss Y" — and Stage 4's gate is **exact match on
count/list queries**. nDCG cannot express that, so until this module existed the
class appeared in the metric table with retrieval numbers beside it and nothing
scoring the thing it is actually for.

This is a *separate* engine from :mod:`tests.eval.harness.metrics` on purpose.
The two produce different measures over different query sets, and every row they
emit is labelled with which one produced it — an answer score and a retrieval
score must never be legible as the same number.

Four rules, each a recorded parameter rather than a constant, because the retrieval
side learned that a threshold nobody wrote down is a threshold nobody can
challenge:

1. **A count is exact.** ``count_tolerance`` defaults to 0 and is written into the
   results file. Aggregation is computed by an exact mechanism (a terms
   aggregation, a ``SUM``); a tolerance would hide precisely the defects the class
   exists to catch — a double-counted overlapping chunk, an unrefreshed index, a
   filter that missed one file — and "off by one" is still a wrong answer to a
   user.
2. **A file set is scored exact-match for the gate, with F1 reported beside it as
   a diagnostic.** ``set_credit`` selects which; both are always emitted.
   Subset ≠ correct: "which meetings discuss X" answered with 7 of 8 files is
   wrong. But EM alone cannot separate "the aggregation is right and one file's
   phrase straddled a chunk boundary" from "the marker matched nothing", and
   those have different fixes, so the partial score is reported — never *instead*
   of EM.
3. **A speaker answer is two fields** (name, session count) and both must match
   for EM. Partial credit is the fraction of fields correct, so "right person,
   wrong count" is visibly different from "wrong person".
4. **An unanswered query scores zero and is counted.** :func:`evaluate_answers`
   iterates the **gold** query set, exactly as :func:`metrics.evaluate` iterates
   the qrels — ``trec_eval -c`` semantics. A dict comprehension over what the
   answerer returned would flatter every regression it is meant to catch, which
   is the specific bug the retrieval side shipped a guard test for.

Every set is emitted **sorted**. ``PYTHONHASHSEED`` is unpinned in this repo and
set iteration order varies per process; that has already produced a real bug here
(an unsorted indexed speaker list).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from typing import Any

#: Answer shapes the synthetic tier's aggregation rules produce (R3-R7).
INTEGER = "integer"
FILE_SET = "file_set"
SPEAKER_COUNT = "speaker_count"
KINDS = (INTEGER, FILE_SET, SPEAKER_COUNT)

#: Reported for every answer-scored query. ``EM`` is the gate; ``partial`` is a
#: diagnostic; ``answered`` is the share of the class the system attempted at all
#: — without it a perfect EM over one attempted query out of twenty reads as 0.05
#: with no way to see why.
MEASURES = ("EM", "partial", "answered")

_WHITESPACE = re.compile(r"\s+")


def normalise_name(value: str) -> str:
    """Casefold and collapse whitespace. The only tolerance applied to a name."""
    return _WHITESPACE.sub(" ", str(value)).strip().casefold()


@dataclass(frozen=True)
class Answer:
    """One typed answer — gold or submitted, the same shape for both.

    ``value`` is canonical: ``int`` for a count, a **sorted tuple** for a file
    set, ``(name, sessions)`` for a speaker answer.
    """

    kind: str
    value: Any

    @staticmethod
    def integer(count: int) -> Answer:
        return Answer(INTEGER, int(count))

    @staticmethod
    def file_set(file_uuids) -> Answer:
        return Answer(FILE_SET, tuple(sorted({str(uuid) for uuid in file_uuids})))

    @staticmethod
    def speaker_count(speaker: str, sessions: int) -> Answer:
        return Answer(SPEAKER_COUNT, (str(speaker), int(sessions)))

    @staticmethod
    def from_record(kind: str, value: Any, *, remap: dict[str, str] | None = None) -> Answer:
        """Build from the generator's ``queries.jsonl`` ``answer`` field.

        Args:
            kind: One of :data:`KINDS`.
            value: The generator's raw value.
            remap: Corpus ``file_uuid`` -> the uuid the app assigned, applied to
                file-set answers. A file the map does not cover is left as-is and
                will simply not match, rather than silently dropping out of the
                gold set and shrinking the answer.

        Raises:
            ValueError: Unknown ``kind``, or a value that does not fit it.
        """
        if kind == INTEGER:
            return Answer.integer(int(value))
        if kind == FILE_SET:
            mapping = remap or {}
            return Answer.file_set(mapping.get(str(item), str(item)) for item in value)
        if kind == SPEAKER_COUNT:
            return Answer.speaker_count(str(value["speaker"]), int(value["sessions"]))
        raise ValueError(f"Unknown answer kind {kind!r}; expected one of {KINDS}")

    def as_json(self) -> Any:
        """JSON-safe, deterministic form for the results document."""
        if self.kind == FILE_SET:
            return list(self.value)
        if self.kind == SPEAKER_COUNT:
            return {"speaker": self.value[0], "sessions": self.value[1]}
        return self.value


@dataclass(frozen=True)
class AnswerPolicy:
    """Scoring rules, recorded verbatim in the results file.

    Attributes:
        count_tolerance: Absolute integer slack allowed on a count. **0**, and
            changing it should be as visible as changing the gain function.
        set_credit: ``f1`` (default) or ``exact`` — what ``partial`` means for a
            file set. ``EM`` is unaffected either way.
        speaker_credit: ``fields`` (default) or ``exact``.
    """

    count_tolerance: int = 0
    set_credit: str = "f1"
    speaker_credit: str = "fields"

    def __post_init__(self) -> None:
        if self.set_credit not in ("f1", "exact"):
            raise ValueError(f"set_credit must be 'f1' or 'exact', got {self.set_credit!r}")
        if self.speaker_credit not in ("fields", "exact"):
            raise ValueError(
                f"speaker_credit must be 'fields' or 'exact', got {self.speaker_credit!r}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": "EM (exact match) — the Stage 4 gate. 'partial' is diagnostic only.",
            "count_tolerance": self.count_tolerance,
            "count_rule": (
                f"|submitted - gold| <= {self.count_tolerance}; no interpolated partial credit"
            ),
            "set_credit": self.set_credit,
            "set_rule": (
                "EM = set equality (a subset is WRONG); partial = F1 over the file sets"
                if self.set_credit == "f1"
                else "EM = set equality; partial = EM"
            ),
            "speaker_credit": self.speaker_credit,
            "speaker_rule": (
                "EM = name AND sessions; partial = fraction of the two fields correct"
                if self.speaker_credit == "fields"
                else "EM = name AND sessions; partial = EM"
            ),
            "name_normalisation": "casefold + whitespace collapse",
            "unanswered_policy": "scored 0 on every measure and counted (trec_eval -c semantics)",
            "kind_mismatch_policy": "scored 0; counts as answered, not as unanswered",
        }


def _f1(gold: tuple[str, ...], submitted: tuple[str, ...]) -> float:
    gold_set, submitted_set = set(gold), set(submitted)
    if not gold_set and not submitted_set:
        return 1.0
    overlap = len(gold_set & submitted_set)
    if overlap == 0:
        return 0.0
    precision = overlap / len(submitted_set)
    recall = overlap / len(gold_set)
    return 2 * precision * recall / (precision + recall)


def score_one(gold: Answer, submitted: Answer | None, policy: AnswerPolicy) -> dict[str, float]:
    """Score one answer. Returns a value for every name in :data:`MEASURES`."""
    if submitted is None:
        return dict.fromkeys(MEASURES, 0.0)
    if submitted.kind != gold.kind:
        # An answerer that returned the wrong *shape* answered; it answered wrong.
        return {"EM": 0.0, "partial": 0.0, "answered": 1.0}

    if gold.kind == INTEGER:
        exact = float(abs(int(submitted.value) - int(gold.value)) <= policy.count_tolerance)
        return {"EM": exact, "partial": exact, "answered": 1.0}

    if gold.kind == FILE_SET:
        exact = float(set(submitted.value) == set(gold.value))
        partial = exact if policy.set_credit == "exact" else _f1(gold.value, submitted.value)
        return {"EM": exact, "partial": partial, "answered": 1.0}

    gold_name, gold_sessions = gold.value
    submitted_name, submitted_sessions = submitted.value
    name_ok = float(normalise_name(submitted_name) == normalise_name(gold_name))
    count_ok = float(abs(int(submitted_sessions) - int(gold_sessions)) <= policy.count_tolerance)
    exact = float(name_ok == 1.0 and count_ok == 1.0)
    partial = exact if policy.speaker_credit == "exact" else (name_ok + count_ok) / 2.0
    return {"EM": exact, "partial": partial, "answered": 1.0}


@dataclass
class AnswerResult:
    """Per-query and aggregate answer scores, plus the denominator used."""

    per_query: dict[str, dict[str, float]] = field(default_factory=dict)
    aggregate: dict[str, float] = field(default_factory=dict)
    query_count: int = 0
    unanswered: list[str] = field(default_factory=list)
    submitted: dict[str, Answer | None] = field(default_factory=dict)


def evaluate_answers(
    gold: Mapping[str, Answer],
    submitted: Mapping[str, Answer | None],
    *,
    policy: AnswerPolicy | None = None,
) -> AnswerResult:
    """Score submitted answers against gold, over the **gold** query set.

    Args:
        gold: Query id -> the exact answer. Defines the denominator: every query
            here contributes to the mean, answered or not.
        submitted: Query id -> the system's answer, or ``None`` for a query it
            declined. Keys absent from ``gold`` are ignored — they cannot be
            scored.
        policy: Scoring rules. Defaults to :class:`AnswerPolicy`.

    Returns:
        An :class:`AnswerResult` whose ``query_count`` equals ``len(gold)``.

    Raises:
        ValueError: If ``gold`` is empty, or the scored denominator does not
            equal it — the assertion that makes the zero substitution verifiable
            rather than merely intended.
    """
    if not gold:
        raise ValueError("Refusing to evaluate against an empty gold answer set")
    rules = policy or AnswerPolicy()

    result = AnswerResult(query_count=len(gold))
    for query_id in sorted(gold):
        answer = submitted.get(query_id)
        result.submitted[query_id] = answer
        if answer is None:
            result.unanswered.append(query_id)
        result.per_query[query_id] = score_one(gold[query_id], answer, rules)

    if len(result.per_query) != len(gold):
        raise ValueError(
            f"Scored {len(result.per_query)} answers against {len(gold)} gold — "
            "the zero substitution did not cover the query set"
        )

    for name in MEASURES:
        values = [row[name] for row in result.per_query.values()]
        result.aggregate[name] = sum(values) / len(values)
    return result


def subset_answers(result: AnswerResult, query_ids: set[str]) -> AnswerResult:
    """Re-aggregate over a subset (one class, one rule), keeping the denominator."""
    rows = {qid: row for qid, row in result.per_query.items() if qid in query_ids}
    out = AnswerResult(
        per_query=rows,
        query_count=len(rows),
        unanswered=[qid for qid in result.unanswered if qid in query_ids],
        submitted={qid: ans for qid, ans in result.submitted.items() if qid in query_ids},
    )
    if not rows:
        return out
    for name in MEASURES:
        out.aggregate[name] = sum(row[name] for row in rows.values()) / len(rows)
    return out


def scoring_provenance(policy: AnswerPolicy) -> dict[str, Any]:
    """Name what produced every answer number, for the results file."""
    return {
        "engine": "tests.eval.harness.answers",
        "measures": list(MEASURES),
        "measure_semantics": {
            "EM": "exact match under the policy below — the Stage 4 gate",
            "partial": "diagnostic partial credit; NEVER a substitute for EM",
            "answered": "share of the gold query set the answerer attempted",
        },
        "llm_required": False,
        "policy": policy.as_dict(),
        "set_ordering": "every set is emitted sorted (PYTHONHASHSEED is unpinned here)",
    }
