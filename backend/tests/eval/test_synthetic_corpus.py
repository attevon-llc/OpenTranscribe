"""Tests for the synthetic corpus generator (#403 Stage 1, synthetic tier).

Three things are being proved here, and each has a paired negative case so the test
cannot pass vacuously:

1. **Determinism** — the same seed produces byte-identical files, and a different seed
   does not (otherwise "identical" could just mean "empty").
2. **Ground truth is correct** — the validator passes on a clean corpus *and* fails on a
   deliberately corrupted one. A validator that cannot fail proves nothing.
3. **Metrics are invariant to document-id naming** — the survey §1.6 finding, where
   ``{uuid}_digest`` outranks every chunk of the same file at an identical score because
   trec_eval breaks ties by docid descending.

No mocks: everything runs against a real generated corpus in ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.eval.synthetic.bm25 import RunItem
from tests.eval.synthetic.bm25 import evaluate
from tests.eval.synthetic.bm25 import normalise_run
from tests.eval.synthetic.bm25 import reciprocal_rank
from tests.eval.synthetic.cli import main as cli_main
from tests.eval.synthetic.cli import verify_checksums
from tests.eval.synthetic.corpus import build_corpus
from tests.eval.synthetic.corpus import default_config
from tests.eval.synthetic.corpus import iter_deterministic_files
from tests.eval.synthetic.rng import Rng
from tests.eval.synthetic.rng import derive_seed
from tests.eval.synthetic.textindex import Corpus
from tests.eval.synthetic.textindex import load_jsonl
from tests.eval.synthetic.textindex import phrase_pattern
from tests.eval.synthetic.validate import validate_corpus

# One xdist group so the module-scoped corpus is generated ONCE. Without it every worker
# that receives a test from this file builds its own 60-meeting corpus, which turned a 23 s
# module into 48 s of wall clock and several times that in CPU.
pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("synthetic_corpus")]

SMALL = {"meetings": 60, "meetings_per_team": 30, "shard_size": 25}


def _config(**overrides) -> dict:
    merged = dict(SMALL)
    merged.update(overrides)
    return default_config(**merged)


@pytest.fixture(scope="module")
def corpus_dir(tmp_path_factory) -> Path:
    """A generated corpus shared by the read-only tests in this module."""
    out = Path(tmp_path_factory.mktemp("otsynth"))
    build_corpus(_config(), out)
    return out


@pytest.fixture(scope="module")
def corpus(corpus_dir: Path) -> Corpus:
    """The loaded meeting text of :func:`corpus_dir`."""
    return Corpus.load(corpus_dir)


@pytest.fixture(scope="module")
def queries(corpus_dir: Path) -> list[dict]:
    """The generated query set."""
    return load_jsonl(corpus_dir / "queries.jsonl")


def _digests(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in iter_deterministic_files(root)
    }


# --- 1. Determinism ---------------------------------------------------------------


def test_same_seed_regenerates_a_byte_identical_corpus(tmp_path: Path) -> None:
    """The published reproducibility claim, exercised end to end."""
    first, second = tmp_path / "a", tmp_path / "b"
    build_corpus(_config(), first)
    build_corpus(_config(), second)
    left, right = _digests(first), _digests(second)
    assert left, "generated no deterministic files at all"
    assert left == right
    assert (first / "SHA256SUMS").read_bytes() == (second / "SHA256SUMS").read_bytes()


def test_a_different_seed_produces_a_different_corpus(tmp_path: Path) -> None:
    """Guard for the test above: 'identical' must not be achievable by emptiness."""
    first, second = tmp_path / "a", tmp_path / "b"
    build_corpus(_config(seed=1), first)
    build_corpus(_config(seed=2), second)
    assert set(_digests(first)) == set(_digests(second))
    assert _digests(first) != _digests(second)


def test_rng_stream_is_pinned() -> None:
    """SplitMix64 is frozen: a refactor must not silently move every corpus."""
    stream = Rng(12345)
    assert [stream.next_u64() for _ in range(3)] == [
        2454886589211414944,
        3778200017661327597,
        2205171434679333405,
    ]
    assert round(Rng(1).random(), 12) == 0.566561575172
    assert derive_seed("a", 1) == 3195065712535284873


def test_checksums_detect_a_modified_file(tmp_path: Path) -> None:
    """``verify`` must notice a byte change, or the determinism claim is unenforced."""
    out = tmp_path / "c"
    build_corpus(_config(meetings=24, meetings_per_team=24, shard_size=24), out)
    assert verify_checksums(out) == []
    shard = next(iter(sorted((out / "meetings").glob("*.jsonl"))))
    shard.write_bytes(shard.read_bytes() + b"\n")
    assert verify_checksums(out) == ["meetings/part-0000.jsonl"]


# --- 2. Ground truth --------------------------------------------------------------


def test_every_validation_check_runs_and_passes(corpus_dir: Path) -> None:
    """All ten checks must execute; a check that never runs reports like a clean one."""
    report = validate_corpus(corpus_dir)
    assert report.failures == []
    executed = {name.split("-")[0] for name in report.checks}
    assert executed == {"V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9", "V10"}
    assert all(count > 0 for count in report.checks.values())


def test_validator_fails_when_an_anchor_is_planted_in_a_second_file(tmp_path: Path) -> None:
    """V1 must fail if an answer leaks into a meeting that is not in the gold set."""
    out = tmp_path / "tampered"
    build_corpus(_config(), out)
    facts = load_jsonl(out / "facts.jsonl")
    victim = next(f for f in facts if f["kind"] == "fact")
    shard = out / "meetings" / "part-0000.jsonl"
    rows = [json.loads(line) for line in shard.read_text().splitlines()]
    other = next(r for r in rows if r["file_uuid"] != victim["file_uuid"])
    other["turns"][0]["content"] += f" Also worth noting: {victim['anchor']}."
    shard.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

    report = validate_corpus(out)
    assert not report.ok
    assert any(f.startswith("V1-anchor-exclusivity") for f in report.failures)


def test_validator_fails_when_an_aggregation_count_is_wrong(tmp_path: Path) -> None:
    """V4 must fail if a recorded count disagrees with the text on disk."""
    out = tmp_path / "miscounted"
    build_corpus(_config(), out)
    path = out / "queries.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    target = next(r for r in rows if r["rule"] == "R3-agg-count-files")
    target["answer"] = target["answer"] + 1
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

    report = validate_corpus(out)
    assert not report.ok
    assert any(f.startswith("V4-aggregation-exact") for f in report.failures)


def test_multi_file_answers_need_every_gold_file(corpus: Corpus, queries: list[dict]) -> None:
    """Independent re-derivation of V3: no proper subset of the gold set suffices."""
    multi = [q for q in queries if q["query_class"] == "multi_file"]
    assert len(multi) >= 5
    for query in multi[:8]:  # a full linear scan per anchor; 8 is enough to catch a leak
        anchors = [c["anchor"] for c in query["components"]]
        assert len(anchors) >= 2
        holders = [sorted(corpus.find_phrase_naive(anchor)) for anchor in anchors]
        assert all(len(h) == 1 for h in holders), query["query_id"]
        assert len({h[0] for h in holders}) == len(anchors), query["query_id"]
        assert sorted({h[0] for h in holders}) == sorted(query["gold_files"])


def test_aggregation_counts_match_a_full_linear_scan(
    corpus: Corpus, corpus_dir: Path, queries: list[dict]
) -> None:
    """Recount every R3/R5 answer with the naive scanner, not the narrowed one."""
    phrases = {
        f["query_id"]: f["anchor"]
        for f in load_jsonl(corpus_dir / "facts.jsonl")
        if f["kind"] in ("marker", "event")
    }
    checked = 0
    for query in queries:
        if query["rule"] not in ("R3-agg-count-files", "R5-agg-count-events"):
            continue
        hits = corpus.find_phrase_naive(phrases[query["query_id"]])
        expected = len(hits) if query["rule"] == "R3-agg-count-files" else sum(hits.values())
        assert expected == query["answer"], query["query_id"]
        checked += 1
    assert checked >= 2


def test_narrowed_search_agrees_with_the_naive_scan(corpus: Corpus, corpus_dir: Path) -> None:
    """The index-narrowed matcher must not miss an occurrence the linear scan finds."""
    anchors = sorted({f["anchor"] for f in load_jsonl(corpus_dir / "facts.jsonl")})
    assert len(anchors) >= 20
    for anchor in anchors[:25]:
        assert corpus.find_phrase(anchor) == corpus.find_phrase_naive(anchor)


def test_lookup_paraphrase_queries_never_quote_their_answer(queries: list[dict]) -> None:
    """A planted token the query repeats verbatim would make the set trivially easy."""
    paraphrase = [
        q for q in queries if q["query_class"] == "lookup" and q["surface"] == "paraphrase"
    ]
    verbatim = [q for q in queries if q["surface"] == "verbatim"]
    assert paraphrase and verbatim
    for query in paraphrase:
        assert not phrase_pattern(str(query["answer"])).search(query["text"]), query["query_id"]
    for query in verbatim:
        assert phrase_pattern(str(query["answer"])).search(query["text"]), query["query_id"]


def test_summarize_gold_is_exactly_the_series(corpus: Corpus, queries: list[dict]) -> None:
    """A summarize query must cover N/N sessions — the #403 Stage 4 gate."""
    summaries = [q for q in queries if q["query_class"] == "summarize"]
    assert summaries
    for query in summaries:
        members = sorted(
            uid for uid, doc in corpus.docs.items() if doc.series_id == query["series_id"]
        )
        assert sorted(query["gold_files"]) == members
        assert len(members) >= 4


def test_speaker_aggregation_answer_is_a_strict_maximum(
    corpus: Corpus, queries: list[dict]
) -> None:
    """A tied maximum has two correct answers and must never be emitted."""
    speaker_queries = [q for q in queries if q["rule"] == "R6-agg-speaker-top"]
    assert speaker_queries
    for query in speaker_queries:
        tally: dict[str, int] = {}
        for file_uuid in query["gold_files"]:
            for name in corpus.docs[file_uuid].speakers:
                tally[name] = tally.get(name, 0) + 1
        ranked = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
        assert ranked[0][1] > ranked[1][1], query["query_id"]
        assert ranked[0][0] == query["answer"]["speaker"]


def test_qrels_file_matches_the_query_gold_sets(corpus_dir: Path, queries: list[dict]) -> None:
    """The TREC file is the published artefact; it must not drift from queries.jsonl."""
    rows = [
        line.split("\t")
        for line in (corpus_dir / "qrels-files.tsv").read_text().splitlines()
        if line.strip()
    ]
    assert rows
    from_file: dict[str, set[str]] = {}
    for qid, zero, docid, rel in rows:
        assert zero == "0" and rel == "1"
        from_file.setdefault(qid, set()).add(docid)
    assert from_file == {q["query_id"]: set(q["gold_files"]) for q in queries}


def test_near_duplicate_rate_is_a_working_dial(tmp_path: Path) -> None:
    """rho must actually change corpus structure, or sweeping it measures nothing.

    ``.rag-403/eval-corpus-plan.md`` §4 shows near-duplicate structure, not index size,
    is the dominant confound in QMSum. The synthetic tier's contribution is turning that
    confound into a controlled variable — which requires the control to work.
    """
    low, high = tmp_path / "rho0", tmp_path / "rho9"
    build_corpus(_config(near_duplicate_rate=0.0), low)
    build_corpus(_config(near_duplicate_rate=0.9), high)
    low_stats = json.loads((low / "stats.json").read_text())
    high_stats = json.loads((high / "stats.json").read_text())
    assert low_stats["near_duplicate_fraction_in_clusters"] == 0.0
    assert high_stats["near_duplicate_fraction_in_clusters"] > 0.5
    low_agendas = {tuple(m["agenda"]) for m in _meetings(low)}
    high_agendas = {tuple(m["agenda"]) for m in _meetings(high)}
    assert len(high_agendas) < len(low_agendas)


def _meetings(root: Path) -> list[dict]:
    return [
        row for shard in sorted((root / "meetings").glob("*.jsonl")) for row in load_jsonl(shard)
    ]


# --- 3. Rank hygiene (survey section 1.6) -----------------------------------------


def _tied_run(digest_id: str) -> list[RunItem]:
    """Thirteen candidates of one file at an identical score, digest included."""
    items = [RunItem(f"3f2a9c10_{i}", 1.0, "chunk", "3f2a9c10", i) for i in range(12)]
    items.append(RunItem(digest_id, 1.0, "digest", "3f2a9c10", -1))
    return items


def test_tie_break_is_invariant_to_document_id_naming() -> None:
    """Renaming the digest document must not move any metric."""
    gold = {"3f2a9c10_7"}
    default = normalise_run(_tied_run("3f2a9c10_digest"))
    renamed = normalise_run(_tied_run("3f2a9c10_aaa"))
    assert [i.chunk_index for i in default] == [i.chunk_index for i in renamed]
    assert reciprocal_rank(default, gold) == reciprocal_rank(renamed, gold)


def test_docid_tie_break_would_have_moved_the_metric() -> None:
    """Guard for the test above: prove the hazard it defends against is real.

    trec_eval breaks ties by document id **descending**. Under that rule the same gold
    chunk lands at a different rank depending only on what the digest document is called
    — ``_digest`` starts with ``'d'`` and outsorts every digit, while ``_-digest`` starts
    with a hyphen and sorts below them. That is how a Stage-3 "win" could be manufactured
    by naming, and it is why the harness must resolve ties itself.
    """
    gold = {"3f2a9c10_7"}
    high = sorted(_tied_run("3f2a9c10_digest"), key=lambda i: i.doc_id, reverse=True)
    low = sorted(_tied_run("3f2a9c10_-digest"), key=lambda i: i.doc_id, reverse=True)
    assert reciprocal_rank(high, gold) != reciprocal_rank(low, gold)


def test_unanswered_queries_are_scored_zero_not_omitted() -> None:
    """``trec_eval -c`` semantics: the mean is over the qrels, not over the answers."""
    qrels = {"q1": {"d1"}, "q2": {"d2"}}
    runs = {"q1": normalise_run([RunItem("d1", 2.0, "chunk", "d1", 0)])}
    scored = evaluate(qrels, runs, ks=(1,))
    assert scored["recall@1"] == pytest.approx(0.5)
    assert scored["mrr"] == pytest.approx(0.5)
    assert evaluate(
        qrels, {**runs, "q2": normalise_run([RunItem("d2", 2.0, "chunk", "d2", 0)])}, ks=(1,)
    )["recall@1"] == pytest.approx(1.0)


# --- 4. CLI -----------------------------------------------------------------------


def test_cli_generate_validate_and_verify_round_trip(tmp_path: Path) -> None:
    """The documented command sequence must work and return the documented exit codes."""
    out = tmp_path / "cli"
    assert (
        cli_main(
            [
                "generate",
                "--out",
                str(out),
                "--meetings",
                "24",
                "--meetings-per-team",
                "24",
                "--shard-size",
                "24",
            ]
        )
        == 0
    )
    assert (out / "README.md").exists()
    assert (out / "MANIFEST.tsv").read_text().splitlines()[1].split("\t")[1] == "A"
    assert cli_main(["validate", str(out)]) == 0
    assert cli_main(["verify", str(out)]) == 0
    (out / "config.json").write_text("{}\n")
    assert cli_main(["verify", str(out)]) == 1
