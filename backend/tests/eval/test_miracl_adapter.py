"""The MIRACL adapter must produce gold a retriever can actually be graded against (#453).

MIRACL is the anchor for every non-English claim — 18 languages, Apache-2.0, **human
pooled judgements with explicit negatives**. Nothing else on the NAS has all three.

Its gold shape differs from every other corpus in this harness, and getting the
reconciliation wrong is silent rather than loud:

* QMSum and the synthetic tier judge an inclusive **turn range** inside a meeting.
* MIRACL judges a whole **passage** — "docid X is relevant to query Y".

The injector writes one passage per file as a single turn, so a document-level
judgement becomes ``GoldSpan(uuid, 0, 0)`` and ``qrels.py``/``metrics.py`` need no
changes. The tests below pin the three ways that can go wrong without anything raising:

1. **A negative treated as gold inverts the metric.** MIRACL ships ``relevance: 0``
   rows; they belong in the index as distractors and must never become gold spans.
2. **Dropping negatives from the SUBSET makes every retriever look perfect**, because
   there is then nothing to rank against.
3. **Scoring an ungraded split** (``test-a``/``test-b`` have topics but no qrels)
   produces a ranking nobody can grade — and reports success.

The unit tests build tiny corpora on disk so they run in the fast suite with no NAS.
The real-data test is marked ``integration`` and states loudly what it needs.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest

from tests.eval.harness import miracl
from tests.eval.harness.corpora import InjectedCorpus
from tests.eval.harness.corpora import load_miracl_queries

_NAS_ROOT = Path("/mnt/nas/opentranscribe-benchmarks/multilingual")


def _write_corpus(root: Path, language: str = "xx") -> None:
    """A miniature MIRACL: 2 queries, positives AND negatives, 3 passages."""
    lang_dir = root / "miracl" / f"miracl-v1.0-{language}"
    (lang_dir / "topics").mkdir(parents=True)
    (lang_dir / "qrels").mkdir(parents=True)

    (lang_dir / "topics" / f"topics.miracl-v1.0-{language}-dev.tsv").write_text(
        "q1#0\thow tall is the tower\nq2#0\twho wrote the book\n", encoding="utf-8"
    )
    # q1: one positive, one negative. q2: one positive.
    (lang_dir / "qrels" / f"qrels.miracl-v1.0-{language}-dev.tsv").write_text(
        "q1#0\tQ0\td1#0\t1\nq1#0\tQ0\td2#0\t0\nq2#0\tQ0\td3#0\t1\n", encoding="utf-8"
    )
    # test-b has topics but NO qrels, exactly as MIRACL ships it.
    (lang_dir / "topics" / f"topics.miracl-v1.0-{language}-test-b.tsv").write_text(
        "q9#0\tungraded question\n", encoding="utf-8"
    )

    shard_dir = root / "miracl-corpus" / f"miracl-corpus-v1.0-{language}"
    shard_dir.mkdir(parents=True)
    with gzip.open(shard_dir / "docs-0.jsonl.gz", "wt", encoding="utf-8") as handle:
        for docid, title, text in (
            ("d1#0", "Tower", "The tower is 324 metres tall."),
            ("d2#0", "Bridge", "An unrelated passage about a bridge."),
            ("d3#0", "Book", "The book was written in 1851."),
        ):
            handle.write(json.dumps({"docid": docid, "title": title, "text": text}) + "\n")


def _corpus(root: Path, docids: list[str]) -> InjectedCorpus:
    return InjectedCorpus(
        key="miracl",
        name="MIRACL",
        version="1.0",
        license_tier="A",
        root=root,
        file_uuid_by_meeting={d: f"uuid-of-{d}" for d in docids},
        extra_by_meeting={},
    )


def test_an_ungraded_split_is_refused(tmp_path) -> None:
    """test-a/test-b ship topics and no qrels — a run over them cannot be scored."""
    _write_corpus(tmp_path)

    with pytest.raises(ValueError, match="no qrels"):
        miracl.load_topics(tmp_path, "xx", "test-b")


def test_graded_relevance_survives_parsing(tmp_path) -> None:
    """Collapsing grades to a set turns a graded metric into a binary one."""
    _write_corpus(tmp_path)

    qrels = miracl.load_qrels(tmp_path, "xx")

    assert qrels["q1#0"] == {"d1#0": 1, "d2#0": 0}
    assert qrels["q2#0"] == {"d3#0": 1}


def test_the_subset_keeps_negatives(tmp_path) -> None:
    """A subset of only positives makes every retriever look perfect."""
    _write_corpus(tmp_path)
    qrels = miracl.load_qrels(tmp_path, "xx")

    wanted = miracl.judged_docids(qrels, ["q1#0"])

    assert wanted == {"d1#0", "d2#0"}, (
        "the negative was dropped from the subset, so nothing would compete with the "
        "gold passage and every retriever would score perfectly"
    )


def test_only_positives_become_gold_spans(tmp_path) -> None:
    """The inversion: a negative in the gold set makes wrong answers score as right."""
    _write_corpus(tmp_path)
    corpus = _corpus(tmp_path, ["d1#0", "d2#0", "d3#0"])

    queries = load_miracl_queries(corpus, "xx")

    by_id = {q.query_id: q for q in queries}
    assert set(by_id) == {"xx:q1#0", "xx:q2#0"}
    gold = {span.file_uuid for span in by_id["xx:q1#0"].spans}
    assert gold == {"uuid-of-d1#0"}, (
        f"expected only the positive passage as gold, got {gold} — a relevance-0 "
        "judgement became gold, which inverts the metric"
    )


def test_a_passage_maps_to_a_single_turn(tmp_path) -> None:
    """The whole reconciliation: document-level gold as GoldSpan(uuid, 0, 0)."""
    _write_corpus(tmp_path)
    corpus = _corpus(tmp_path, ["d1#0", "d2#0", "d3#0"])

    span = load_miracl_queries(corpus, "xx")[0].spans[0]

    assert (span.start_turn, span.end_turn) == (0, 0)
    assert span.turn_indices() == {0}, "the injector writes one passage as one turn"


def test_a_query_whose_positives_were_not_injected_is_skipped(tmp_path) -> None:
    """Scoring it would depress every metric for a non-retrieval reason."""
    _write_corpus(tmp_path)
    # d3#0 (q2's only positive) was not injected.
    corpus = _corpus(tmp_path, ["d1#0", "d2#0"])

    queries = load_miracl_queries(corpus, "xx")

    assert [q.query_id for q in queries] == ["xx:q1#0"]


def test_every_query_is_labelled_lookup(tmp_path) -> None:
    """MIRACL asks factual questions; no other class would mean anything in the table."""
    from tests.eval.harness.corpora import LOOKUP

    _write_corpus(tmp_path)
    corpus = _corpus(tmp_path, ["d1#0", "d2#0", "d3#0"])

    assert {q.query_class for q in load_miracl_queries(corpus, "xx")} == {LOOKUP}


def test_the_subset_cache_is_rebuilt_when_it_does_not_cover_the_request(tmp_path) -> None:
    """A cache that under-covers is worse than no cache.

    It would silently score against fewer documents than the qrels judge, which reads
    as a retrieval failure rather than a caching one.
    """
    _write_corpus(tmp_path)
    cache = tmp_path / "cache"

    _, _, small = miracl.build_subset(tmp_path, "xx", query_count=1, cache_dir=cache)
    _, _, large = miracl.build_subset(tmp_path, "xx", query_count=2, cache_dir=cache)

    assert set(small) == {"d1#0", "d2#0"}
    assert set(large) == {"d1#0", "d2#0", "d3#0"}


@pytest.mark.integration
@pytest.mark.skipif(
    not _NAS_ROOT.is_dir() and os.environ.get("MIRACL_ROOT") is None,
    reason=(
        "MIRACL corpus not reachable. It lives on the NAS at "
        "/mnt/nas/opentranscribe-benchmarks/multilingual (30 GB, sha256-verified by "
        "scripts/fetch-rag-eval-data.sh --verify --only miracl). Set MIRACL_ROOT to "
        "point elsewhere. This is a LOUD skip: the unit tests above use miniature "
        "fixtures and cannot prove the real files still parse."
    ),
)
def test_the_real_corpus_still_parses() -> None:
    """The fixtures above are ours; this proves MIRACL's actual files match them.

    A format change upstream would leave every unit test green while the real adapter
    returned nothing — the failure shape this whole harness exists to prevent.
    """
    root = Path(os.environ.get("MIRACL_ROOT", str(_NAS_ROOT)))

    topics = miracl.load_topics(root, "es")
    qrels = miracl.load_qrels(root, "es")

    assert len(topics) > 100, f"only {len(topics)} Spanish dev topics parsed"
    assert set(topics) == set(qrels), "every dev topic must carry judgements"
    grades = {grade for judged in qrels.values() for grade in judged.values()}
    assert 0 in grades and 1 in grades, (
        f"expected graded relevance including explicit negatives, saw {sorted(grades)} — "
        "MIRACL's negatives are why it is the anchor corpus"
    )


def test_retrieval_per_query_scores_are_emitted() -> None:
    """#461 phase 0: every published retrieval number was a point estimate.

    The per-query scores were already computed by ``metrics.evaluate`` and thrown
    away for retrieval queries, while the *answer* table published them all along.
    That is why the reranker's measured 20-33% nDCG@10 deficit — the one finding
    with direct product impact — sits unactioned: nobody can say whether it is an
    effect or noise, and a mean over N queries cannot answer that.

    This asserts the instrument exists. It deliberately does NOT compute an
    interval: choosing one is a judgement, and hard-coding it here would smuggle
    that judgement into the raw data.
    """
    from tests.eval.harness.corpora import LOOKUP
    from tests.eval.harness.corpora import EvalQuery
    from tests.eval.harness.metrics import EvalResult
    from tests.eval.harness.qrels import GoldSpan
    from tests.eval.harness.report import build_retrieval_per_query

    queries = [
        EvalQuery(
            query_id="es:q1#0",
            text="how tall is the tower",
            query_class=LOOKUP,
            corpus="miracl",
            license_tier="A",
            spans=(GoldSpan("uuid-a", 0, 0), GoldSpan("uuid-b", 0, 0)),
        ),
        # An answer-scored query must NOT appear: it has its own table with its own
        # measures, and mixing them is how "aggregation" once sat in the metric
        # table with an nDCG beside it scoring nothing it asked for.
        EvalQuery(
            query_id="es:q2#0",
            text="count the files",
            query_class=LOOKUP,
            corpus="miracl",
            license_tier="A",
            spans=(),
            scored_on="answer",
        ),
    ]
    result = EvalResult(per_query={"es:q1#0": {"nDCG@10": 0.5123456}, "es:q2#0": {"EM": 1.0}})

    details = build_retrieval_per_query(queries, result)

    assert [d["query_id"] for d in details] == ["es:q1#0"], (
        f"expected only the retrieval-scored query, got {[d['query_id'] for d in details]}"
    )
    row = details[0]
    assert row["scores"]["nDCG@10"] == pytest.approx(0.5123, abs=1e-4), (
        "scores must be rounded like every other number in the report, or metrics.json "
        "stops being byte-identical across runs"
    )
    # gold_count is what makes a per-query score readable: nDCG@10 over 1 gold
    # document and over 40 are different measurements.
    assert row["gold_count"] == 2
    assert row["corpus"] == "miracl" and row["license_tier"] == "A"
