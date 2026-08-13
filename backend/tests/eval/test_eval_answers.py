"""Answer-scoring tests: the aggregation class, which no ranking metric scores.

Each test here corresponds to a way an *answer* benchmark lies: a count scored
loosely, a subset counted as correct, a query the system never answered quietly
dropped out of the mean, and a set serialised in whatever order the process
happened to hash it in.

Nothing here needs a stack, a model or the metric engine — the answer path is
deliberately free of all three (D6).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.eval.harness.answerers import NullAnswerer
from tests.eval.harness.answerers import ReferenceAnswerer
from tests.eval.harness.answerers import parse_intent
from tests.eval.harness.answers import MEASURES
from tests.eval.harness.answers import Answer
from tests.eval.harness.answers import AnswerPolicy
from tests.eval.harness.answers import AnswerResult
from tests.eval.harness.answers import evaluate_answers
from tests.eval.harness.answers import score_one
from tests.eval.harness.answers import subset_answers
from tests.eval.harness.corpora import EvalQuery
from tests.eval.harness.report import build_answer_details
from tests.eval.harness.report import build_answer_rows
from tests.eval.harness.report import build_rows
from tests.eval.harness.report import render_answer_table

BACKEND = Path(__file__).resolve().parents[2]
FILES = tuple(f"{index:08d}-0000-0000-0000-00000000000{index % 10}" for index in range(1, 13))
DEFAULT = AnswerPolicy()


def _gold(queries: list[EvalQuery]) -> dict[str, Answer]:
    """Gold answers by query id, refusing a query that has none."""
    gold: dict[str, Answer] = {}
    for query in queries:
        assert query.gold_answer is not None, f"{query.query_id} has no gold answer"
        gold[query.query_id] = query.gold_answer
    return gold


def _query(query_id: str, text: str, gold: Answer, rule: str = "R3-agg-count-files") -> EvalQuery:
    return EvalQuery(
        query_id=query_id,
        text=text,
        query_class="aggregation",
        corpus="synthetic",
        license_tier="A",
        spans=(),
        scored_on="answer",
        rule=rule,
        gold_answer=gold,
    )


class _StubSearch:
    """Returns a canned response per call and records the bodies it was given.

    Deliberately not a mock: the assertions are about the query body the
    answerer *builds*, so the double has to hand it back for inspection.
    """

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.bodies: list[dict] = []

    def search(self, index: str, body: dict) -> dict:  # noqa: ARG002 - index unused
        self.bodies.append(body)
        return self.responses.pop(0)


def _file_buckets(*keys: str) -> dict:
    return {
        "aggregations": {
            "files": {
                "sum_other_doc_count": 0,
                "buckets": [{"key": key, "doc_count": 1} for key in keys],
            }
        }
    }


# --------------------------------------------------------------------- counts


def test_a_count_is_scored_exactly():
    gold = Answer.integer(12)

    right = score_one(gold, Answer.integer(12), DEFAULT)
    off_by_one = score_one(gold, Answer.integer(11), DEFAULT)

    assert right == {"EM": 1.0, "partial": 1.0, "answered": 1.0}
    assert off_by_one == {"EM": 0.0, "partial": 0.0, "answered": 1.0}, (
        "an off-by-one count earned credit — a count has no principled partial "
        "credit, and 11 is a wrong answer to 'how many meetings'"
    )


def test_count_tolerance_is_a_parameter_and_the_default_is_zero():
    """The default is a *choice*, recorded in the results file. Prove it is wired."""
    gold = Answer.integer(12)
    tolerant = AnswerPolicy(count_tolerance=1)

    assert score_one(gold, Answer.integer(11), DEFAULT)["EM"] == 0.0
    assert score_one(gold, Answer.integer(11), tolerant)["EM"] == 1.0
    assert score_one(gold, Answer.integer(10), tolerant)["EM"] == 0.0
    assert DEFAULT.as_dict()["count_tolerance"] == 0


# ------------------------------------------------------------------ file sets


def test_a_subset_file_set_is_wrong_and_earns_partial_credit_only():
    gold = Answer.file_set(FILES[:4])

    scored = score_one(gold, Answer.file_set(FILES[:3]), DEFAULT)

    assert scored["EM"] == 0.0, "3 of 4 files scored as an exact match"
    # P = 3/3, R = 3/4 -> F1 = 6/7
    assert scored["partial"] == pytest.approx(6 / 7)


def test_a_superset_file_set_is_wrong_too():
    gold = Answer.file_set(FILES[:3])

    scored = score_one(gold, Answer.file_set(FILES[:5]), DEFAULT)

    assert scored["EM"] == 0.0
    # P = 3/5, R = 3/3 -> F1 = 0.75
    assert scored["partial"] == pytest.approx(0.75)


def test_a_disjoint_file_set_scores_zero_on_both_measures():
    scored = score_one(Answer.file_set(FILES[:3]), Answer.file_set(FILES[6:9]), DEFAULT)
    assert scored == {"EM": 0.0, "partial": 0.0, "answered": 1.0}


def test_set_credit_exact_collapses_partial_onto_exact_match():
    """The other half of the parameter: whoever considers F1 unjustified can turn
    it off, and the value in force is written into the results file."""
    gold = Answer.file_set(FILES[:4])
    strict = AnswerPolicy(set_credit="exact")

    lenient_scored = score_one(gold, Answer.file_set(FILES[:3]), DEFAULT)
    strict_scored = score_one(gold, Answer.file_set(FILES[:3]), strict)

    assert lenient_scored["partial"] > 0.0
    assert strict_scored["partial"] == 0.0
    assert strict_scored["EM"] == lenient_scored["EM"] == 0.0
    assert "partial = EM" in strict.as_dict()["set_rule"]


def test_an_unknown_credit_rule_is_refused_rather_than_ignored():
    with pytest.raises(ValueError, match="set_credit"):
        AnswerPolicy(set_credit="jaccard-ish")


# --------------------------------------------------------------- speaker + kind


def test_a_speaker_answer_needs_both_fields():
    gold = Answer.speaker_count("Alina Prentiss", 8)

    both = score_one(gold, Answer.speaker_count("Alina Prentiss", 8), DEFAULT)
    wrong_count = score_one(gold, Answer.speaker_count("Alina Prentiss", 7), DEFAULT)
    wrong_person = score_one(gold, Answer.speaker_count("Teo C. Lindqvist", 8), DEFAULT)

    assert both["EM"] == 1.0
    assert wrong_count == {"EM": 0.0, "partial": 0.5, "answered": 1.0}
    assert wrong_person == {"EM": 0.0, "partial": 0.5, "answered": 1.0}


def test_speaker_names_are_matched_case_and_whitespace_insensitively():
    gold = Answer.speaker_count("Alina Prentiss", 8)
    scored = score_one(gold, Answer.speaker_count("  alina   prentiss ", 8), DEFAULT)
    assert scored["EM"] == 1.0


def test_an_answer_of_the_wrong_shape_counts_as_answered_and_wrong():
    scored = score_one(Answer.integer(3), Answer.file_set(FILES[:3]), DEFAULT)
    assert scored == {"EM": 0.0, "partial": 0.0, "answered": 1.0}


# ----------------------------------------------------------------- the mean


def test_an_unanswered_query_scores_zero_and_stays_in_the_denominator():
    gold = {"q1": Answer.integer(3), "q2": Answer.integer(4)}

    result = evaluate_answers(gold, {"q1": Answer.integer(3)}, policy=DEFAULT)

    assert result.query_count == 2
    assert result.unanswered == ["q2"]
    assert result.per_query["q2"] == dict.fromkeys(MEASURES, 0.0)
    assert result.aggregate["EM"] == pytest.approx(0.5)
    assert result.aggregate["answered"] == pytest.approx(0.5)


def test_a_declined_query_is_unanswered_not_missing_data():
    gold = {"q1": Answer.integer(3), "q2": Answer.integer(4)}

    result = evaluate_answers(gold, {"q1": Answer.integer(3), "q2": None}, policy=DEFAULT)

    assert result.unanswered == ["q2"]
    assert result.aggregate["EM"] == pytest.approx(0.5)


def test_a_mean_over_submitted_answers_only_would_flatter_the_result():
    """The guard that makes the test above worth having.

    This is the exact shape the retrieval side shipped a fix for: a dict
    comprehension over what came back scores 1.0 where the truth is 0.5.
    """
    gold = {"q1": Answer.integer(3), "q2": Answer.integer(4)}
    submitted = {"q1": Answer.integer(3)}

    naive_rows = [score_one(gold[qid], ans, DEFAULT) for qid, ans in submitted.items()]
    naive = sum(row["EM"] for row in naive_rows) / len(naive_rows)
    corrected = evaluate_answers(gold, submitted, policy=DEFAULT).aggregate["EM"]

    assert naive == pytest.approx(1.0)
    assert corrected == pytest.approx(0.5)
    assert corrected < naive


def test_evaluate_refuses_an_empty_gold_set():
    with pytest.raises(ValueError, match="empty gold"):
        evaluate_answers({}, {})


def test_evaluate_ignores_submissions_for_queries_with_no_gold_answer():
    result = evaluate_answers(
        {"q1": Answer.integer(3)},
        {"q1": Answer.integer(3), "ghost": Answer.integer(99)},
        policy=DEFAULT,
    )
    assert result.query_count == 1
    assert set(result.per_query) == {"q1"}


def test_subset_reaggregates_over_the_rules_own_denominator():
    gold = {"q1": Answer.integer(1), "q2": Answer.integer(2), "q3": Answer.integer(3)}
    result = evaluate_answers(
        gold, {"q1": Answer.integer(1), "q3": Answer.integer(9)}, policy=DEFAULT
    )

    scoped = subset_answers(result, {"q1", "q2"})

    assert scoped.query_count == 2
    assert scoped.unanswered == ["q2"]
    assert scoped.aggregate["EM"] == pytest.approx(0.5)


# ------------------------------------------------------------------ answerers


def test_parse_intent_recovers_every_rules_question_frame():
    cases = [
        (
            "How many meetings discussed the Cypress Hearth compliance audit?",
            "count_files",
            {"phrase": "the Cypress Hearth compliance audit"},
        ),
        (
            "Which meetings mention the Slate Viaduct data-retention exercise? List them.",
            "list_files",
            {"phrase": "the Slate Viaduct data-retention exercise"},
        ),
        (
            "How many times in total did we defer the Marble Rampart headcount request?",
            "count_events",
            {"phrase": "the Marble Rampart headcount request"},
        ),
        (
            "How many meetings in January 2025 discussed the Dawn Meridian compliance audit?",
            "temporal_count",
            {"month": "January", "year": "2025", "phrase": "the Dawn Meridian compliance audit"},
        ),
        (
            "Who attended the most vendor review board sessions for the tooling-2 team?",
            "speaker_top",
            {"kind": "vendor review board", "team": "the tooling-2 team"},
        ),
    ]

    parsed = [(parse_intent(text), expected_name, slots) for text, expected_name, slots in cases]

    assert len(parsed) == 5, "the case table lost a rule"
    for intent, expected_name, slots in parsed:
        assert intent is not None, f"no rule matched the {expected_name} frame"
        assert intent.name == expected_name
        assert intent.slots == slots


def test_parse_intent_declines_a_question_no_rule_covers():
    assert parse_intent("What did Dana say about pricing?") is None


def test_the_null_answerer_declines_everything_and_scores_zero():
    """The pre-Stage-4 product floor: no aggregation path exists, so 0.000 EM."""
    queries = [
        _query("q1", "How many meetings discussed the X audit?", Answer.integer(3)),
        _query("q2", "Which meetings mention the Y review? List them.", Answer.file_set(FILES[:2])),
    ]
    answerer = NullAnswerer()

    submitted = {query.query_id: answerer.answer(query) for query in queries}
    result = evaluate_answers(_gold(queries), submitted)

    assert result.query_count == 2
    assert len(result.unanswered) == 2
    assert result.aggregate == {"EM": 0.0, "partial": 0.0, "answered": 0.0}
    assert answerer.describe()["is_production_path"] is False


def test_the_reference_answerer_counts_files_with_a_terms_aggregation():
    client = _StubSearch(_file_buckets(FILES[2], FILES[0], FILES[1]))
    answerer = ReferenceAnswerer(client, "chunks", user_id=7)

    answer = answerer.answer(
        _query("q1", "How many meetings discussed the X audit?", Answer.integer(3))
    )

    assert answer == Answer.integer(3)
    body = client.bodies[0]
    filters = body["query"]["bool"]["filter"]
    assert {"match_phrase": {"content.exact": "the X audit"}} in filters
    assert {"term": {"accessible_user_ids": 7}} in filters
    assert body["aggs"]["files"]["terms"]["field"] == "file_uuid"
    assert "hybrid" not in json.dumps(body), "an agg over a hybrid body crashes OpenSearch 3.4"


def test_the_reference_answerer_returns_a_file_set_in_sorted_order():
    client = _StubSearch(_file_buckets(FILES[5], FILES[1], FILES[3]))
    answerer = ReferenceAnswerer(client, "chunks", user_id=7)

    answer = answerer.answer(
        _query("q1", "Which meetings mention the X review? List them.", Answer.file_set(FILES[:3]))
    )

    assert answer is not None
    assert list(answer.value) == sorted([FILES[5], FILES[1], FILES[3]])


def test_the_reference_answerer_refuses_a_truncated_aggregation():
    """A bucket list cut off at the limit is a wrong answer that looks right."""
    truncated = _file_buckets(FILES[0])
    truncated["aggregations"]["files"]["sum_other_doc_count"] = 4
    answerer = ReferenceAnswerer(_StubSearch(truncated), "chunks", user_id=7)

    with pytest.raises(RuntimeError, match="truncated"):
        answerer.answer(_query("q1", "How many meetings discussed the X audit?", Answer.integer(5)))


def test_the_reference_answerer_declines_the_sql_rules_without_a_database():
    """Declining is the honest outcome; answering from a weaker source is not."""
    answerer = ReferenceAnswerer(
        _StubSearch(_file_buckets(FILES[0])), "chunks", user_id=7, engine=None
    )

    events = answerer.answer(
        _query("q1", "How many times in total did we defer the X budget line?", Answer.integer(2))
    )
    temporal = answerer.answer(
        _query("q2", "How many meetings in March 2025 discussed the X audit?", Answer.integer(1))
    )

    assert events is None
    assert temporal is None


def test_a_tie_for_top_speaker_resolves_deterministically():
    response = {
        "aggregations": {
            "people": {
                "sum_other_doc_count": 0,
                "buckets": [
                    {
                        "key": "Zoe Adler",
                        "files": {"sum_other_doc_count": 0, "buckets": [{"key": FILES[0]}]},
                    },
                    {
                        "key": "Alina Prentiss",
                        "files": {"sum_other_doc_count": 0, "buckets": [{"key": FILES[1]}]},
                    },
                ],
            }
        }
    }
    answerer = ReferenceAnswerer(_StubSearch(response), "chunks", user_id=7)

    answer = answerer.answer(
        _query(
            "q1",
            "Who attended the most weekly sync sessions for the tooling-2 team?",
            Answer.speaker_count("Zoe Adler", 1),
            rule="R6-agg-speaker-top",
        )
    )

    # Tied at one session each: the lexicographic winner, every time. The gold set
    # guarantees a strict maximum, so a tie here simply scores 0 -- what must not
    # happen is that it scores differently on two runs of the same data.
    assert answer == Answer.speaker_count("Alina Prentiss", 1)


# --------------------------------------------------------------------- report


def test_the_retrieval_table_excludes_answer_scored_queries():
    """The defect this whole path exists to fix: 'aggregation' sat in the metric
    table with an nDCG beside it and nothing scoring its actual answer."""
    queries = [
        EvalQuery("r1", "what did Dana say?", "lookup", "synthetic", "A", (), "retrieval"),
        _query("a1", "How many meetings discussed the X audit?", Answer.integer(3)),
    ]
    retrieval_result = AnswerResult()  # placeholder; replaced below by a real one
    del retrieval_result

    from tests.eval.harness.metrics import EvalResult

    scored = EvalResult(
        per_query={"r1": {"nDCG@10": 0.5}, "a1": {"nDCG@10": 0.9}},
        query_count=2,
    )
    rows = build_rows(queries, scored)

    classes = {row["query_class"] for row in rows}
    assert classes == {"lookup", "all"}, (
        f"an answer-scored class leaked into the retrieval table: {classes}"
    )


def test_the_answer_table_shares_no_measure_name_with_the_retrieval_table():
    from tests.eval.harness.metrics import MEASURES as RETRIEVAL_MEASURES

    assert set(MEASURES).isdisjoint(RETRIEVAL_MEASURES)


def test_answer_rows_break_the_class_down_by_rule():
    queries = [
        _query("a1", "How many meetings discussed the X audit?", Answer.integer(3)),
        _query(
            "a2",
            "Which meetings mention the Y review? List them.",
            Answer.file_set(FILES[:2]),
            rule="R4-agg-list-files",
        ),
    ]
    result = evaluate_answers(
        _gold(queries), {"a1": Answer.integer(3), "a2": Answer.file_set(FILES[:1])}
    )

    rows = build_answer_rows(queries, result)

    keyed = {row["rule"]: row for row in rows}
    assert set(keyed) == {"all", "R3-agg-count-files", "R4-agg-list-files"}
    assert keyed["R3-agg-count-files"]["metrics"]["EM"] == 1.0
    assert keyed["R4-agg-list-files"]["metrics"]["EM"] == 0.0
    assert keyed["all"]["metrics"]["EM"] == 0.5
    assert render_answer_table(rows).count("\n") == len(rows) + 2


def test_answer_details_record_gold_beside_what_was_submitted():
    queries = [_query("a1", "How many meetings discussed the X audit?", Answer.integer(12))]
    result = evaluate_answers({"a1": Answer.integer(12)}, {"a1": Answer.integer(3)})

    details = build_answer_details(queries, result)

    assert len(details) == 1
    assert details[0]["gold"] == 12
    assert details[0]["submitted"] == 3
    assert details[0]["scores"]["EM"] == 0.0
    assert details[0]["license_tier"] == "A"


# ---------------------------------------------------------------- determinism

_DETERMINISM_SCRIPT = """
import json
from tests.eval.harness.answers import Answer, evaluate_answers
from tests.eval.harness.corpora import EvalQuery
from tests.eval.harness.report import build_answer_details, build_answer_rows, dumps

FILES = [f"{i:08d}-0000-0000-0000-00000000000{i % 10}" for i in range(1, 13)]
gold_files = set(FILES[:8])
queries = [
    EvalQuery(
        query_id=f"synthetic:ag-{i:05d}",
        text="Which meetings mention the X review? List them.",
        query_class="aggregation",
        corpus="synthetic",
        license_tier="A",
        spans=(),
        scored_on="answer",
        rule="R4-agg-list-files",
        gold_answer=Answer.file_set(gold_files),
    )
    for i in range(4)
]
submitted = {q.query_id: Answer.file_set(set(FILES[:6])) for q in queries[:3]}
result = evaluate_answers({q.query_id: q.gold_answer for q in queries}, submitted)
print(dumps({
    "rows": build_answer_rows(queries, result),
    "details": build_answer_details(queries, result),
}), end="")
"""


def _run_with_hash_seed(seed: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", _DETERMINISM_SCRIPT],
        cwd=BACKEND,
        env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin", "PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_answer_artefacts_are_byte_identical_across_processes():
    """Two runs over unchanged data must produce identical bytes.

    Run in **separate interpreters with different hash seeds**, because the thing
    most likely to break it is set iteration order: ``PYTHONHASHSEED`` is unpinned
    in this repo and an unsorted ``list(set())`` has already produced a real bug
    here. A same-process double call would not catch it.
    """
    first = _run_with_hash_seed("0")
    second = _run_with_hash_seed("12345")

    assert first == second, "answer artefacts differ across hash seeds"
    payload = json.loads(first)
    assert payload["details"][0]["gold"] == sorted(payload["details"][0]["gold"])
    assert payload["rows"][0]["metrics"]["EM"] == 0.0
