"""Keyword (BM25) search latency and result identity on a real OpenSearch (issue #1064).

Keyword search used to be 3-10x SLOWER than neural search on the same index. The body
used ``collapse.inner_hits``, which OpenSearch runs as one sub-search per collapsed
group, and each sub-search rewrote the fuzzy clauses and set up the highlighter again.
That is a fixed cost per group, multiplied by the 200-group relevance over-fetch, so even
a tiny index was slow. Measured here (opensearch 3.4, ``--cpus 2``, this module's
corpus) before the fix, as p50: minimal ``match`` 2-6 ms, keyword search 0.4-2.0 s.

``_execute_split_bm25_collapse`` / ``_hydrate_page_highlights`` now answer it with three
flat requests. This module proves two things against a real engine:

* **identity** — the displayed page (file order, scores, every snippet and its
  highlight, match sources) and the totals match the single-request body exactly, so the
  speed-up costs no result quality;
* **latency** — keyword search p50 stays within a fixed multiple of a minimal ``match``
  query on the same index. The multiple sits between the two measured regimes with >2x
  margin on each side (see ``_LATENCY_RATIO``), and a failure message carries the
  ``profile: true`` breakdown of the slowest query so it says where the time went.
  (A quoted phrase is exercised on page 1 only: it matches too few files for a page 3.)

Point it at a throwaway cluster, never the shared dev one::

    docker run -d --rm --name kwlat-os --cpus 2 -e discovery.type=single-node \\
        -e DISABLE_SECURITY_PLUGIN=true -e DISABLE_INSTALL_DEMO_CONFIG=true \\
        -p 127.0.0.1:55691:9200 opensearchproject/opensearch:3.4.0
    OPENSEARCH_PORT=55691 pytest tests/integration/test_keyword_search_latency_opensearch.py \\
        -m integration -o addopts= -p no:xdist -s
"""

from __future__ import annotations

import os
import random
import statistics
import time
import uuid
from typing import Any

import pytest

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group("keyword_search_latency"),
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason="No OpenSearch reachable (SKIP_OPENSEARCH). Start a throwaway cluster and "
        "export OPENSEARCH_PORT — see the module docstring.",
    ),
]

N_FILES = 100
CHUNKS_PER_FILE = 12
WORDS_PER_CHUNK = 180
USER_ID = 1064
PAGE_SIZE = 20

#: Common words sprinkled into the random text so every query matches many files.
_TOPICAL = ["budget", "meeting", "customer", "report", "deadline", "quarter", "launch"]

#: Queries covering each ``_build_text_query`` shape: single word (fuzzy), multi-word
#: (cross-fields + phrase-slop + AND-fuzzy), a typo, and a quoted phrase.
QUERIES = ["budget", "budget meeting", "customer report deadline", "budgte", '"budget meeting"']

#: Sum of keyword p50 over QUERIES <= _LATENCY_RATIO x max(median minimal p50,
#: _MIN_BASELINE_MS). Summed, because one query's ratio is at the mercy of a 2-11 ms
#: minimal baseline that is mostly HTTP noise. Measured on this corpus with ``--cpus 2``
#: and a ~7 ms baseline: the split path sums to ~770 ms (~110x), the single-request body
#: to ~3,900-4,200 ms (~560-600x). 300 sits with ~2x margin on both sides.
_LATENCY_RATIO = 300
_MIN_BASELINE_MS = 5.0


def _vocabulary(rng: random.Random, size: int) -> list[str]:
    syllables = [c + v for c in "bcdfghklmnprstvz" for v in "aeiou"]
    words: set[str] = set()
    while len(words) < size:
        words.add("".join(rng.choice(syllables) for _ in range(rng.randint(2, 4))))
    return sorted(words)


@pytest.fixture(scope="module")
def keyword_index():
    """A seeded throwaway chunks index with the REAL mapping; deleted afterwards."""
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    name = f"test_kw_latency_{uuid.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    rng = random.Random(1064)  # noqa: S311 — deterministic test corpus, not crypto
    vocab = _vocabulary(rng, 6000)
    actions: list[dict[str, Any]] = []
    for f in range(N_FILES):
        file_uuid = f"kwlat-{f:04d}"
        for n in range(CHUNKS_PER_FILE):
            words = [
                rng.choice(_TOPICAL) if rng.random() < 0.03 else rng.choice(vocab)
                for _ in range(WORDS_PER_CHUNK)
            ]
            actions.append({"index": {"_index": name, "_id": f"{file_uuid}_{n}"}})
            actions.append(
                {
                    "file_id": -(f + 1),
                    "file_uuid": file_uuid,
                    "user_id": USER_ID,
                    "accessible_user_ids": [USER_ID],
                    "chunk_index": n,
                    "content": " ".join(words),
                    "title": f"Weekly sync {f}",
                    "speaker": f"SPEAKER_{n % 4:02d}",
                    "speakers": [f"SPEAKER_{i:02d}" for i in range(4)],
                    "language": "en",
                    "doc_type": "chunk",
                    "start_time": n * 30.0,
                    "end_time": n * 30.0 + 30.0,
                }
            )
    resp = client.bulk(body=actions, refresh=True)
    assert not resp.get("errors"), "bulk seed reported errors"
    client.indices.forcemerge(index=name, max_num_segments=1)
    client.indices.refresh(index=name)
    assert client.count(index=name)["count"] == N_FILES * CHUNKS_PER_FILE
    try:
        yield client, name
    finally:
        client.indices.delete(index=name, ignore_unavailable=True)


@pytest.fixture
def wired(keyword_index, monkeypatch):
    """Route the service at the throwaway index, BM25-only."""
    from app.core.config import settings

    client, name = keyword_index
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    return client, name


def _filters() -> list[dict[str, Any]]:
    from app.services.search.hybrid_search_service import HybridSearchService

    return HybridSearchService()._build_filters(USER_ID, None, None, None, None)


def _keyword_search(query: str, page: int = 1):
    from app.services.search.hybrid_search_service import HybridSearchService

    return HybridSearchService()._search_with_collapse(
        query=query,
        search_query=query,
        filters=_filters(),
        page=page,
        page_size=PAGE_SIZE,
        sort_by="relevance",
        sort_order="desc",
        search_mode="keyword",
        filters_applied={},
        start_time=time.time(),
        has_speaker_filter=False,
        use_neural=False,
        search_pipeline="unused",
    )


def _single_request_search(client, index: str, query: str, page: int = 1):
    """The pre-#1064 execution: one collapse body with per-group inner_hits highlights."""
    from app.services.search.hybrid_search_service import HybridSearchService

    svc = HybridSearchService()
    body = svc._build_collapsed_bm25_body(query, _filters(), page, PAGE_SIZE, False)
    grouped, _ = svc._process_collapsed_results(client.search(index=index, body=body), query)
    svc._apply_semantic_demotion(grouped)
    result = svc._sort_and_paginate(
        query, grouped, "relevance", "desc", "keyword", page, PAGE_SIZE, {}, time.time()
    )
    result.total_files = len(grouped)
    svc._apply_semantic_highlights(result.results, query)
    return result


def _page_view(result) -> list[tuple]:
    return [
        (
            h.file_uuid,
            h.relevance_score,
            h.title_highlighted,
            h.match_sources,
            h.keyword_occurrences,
            h.total_occurrences,
            [
                (o.chunk_index, o.score, o.snippet, o.match_type, o.has_keyword_match)
                for o in h.occurrences
            ],
        )
        for h in result.results
    ]


@pytest.mark.parametrize(
    ("query", "page"), [(q, 1) for q in QUERIES] + [(q, 3) for q in QUERIES if '"' not in q]
)
def test_split_execution_returns_the_single_request_page(wired, query, page):
    client, index = wired
    expected = _single_request_search(client, index, query, page)
    actual = _keyword_search(query, page)

    assert expected.results, "the corpus must match the query or this proves nothing"
    assert _page_view(actual) == _page_view(expected)
    assert (actual.total_files, actual.total_results) == (
        expected.total_files,
        expected.total_results,
    )
    marked = [o for h in actual.results for o in h.occurrences if "<mark>" in o.snippet]
    assert marked, "the page must still render keyword highlights"


def _p50_ms(fn, runs: int = 9, warmup: int = 2) -> float:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def _profile_summary(client, index: str, query: str) -> str:
    """Where one execution of the collapse body spends its time, for a failure message."""
    from app.services.search.hybrid_search_service import HybridSearchService

    body = HybridSearchService()._build_collapsed_bm25_body(query, _filters(), 1, PAGE_SIZE, False)
    body["profile"] = True
    resp = client.search(index=index, body=body)
    shard = resp["profile"]["shards"][0]
    query_ms = sum(q["time_in_nanos"] for q in shard["searches"][0]["query"]) / 1e6
    fetch_ms = sum(f["time_in_nanos"] for f in shard.get("fetch", [])) / 1e6
    return (
        f"single-request body took={resp['took']} ms, shard query={query_ms:.1f} ms, "
        f"fetch={fetch_ms:.1f} ms — the remainder is the unprofiled inner_hits expand phase"
    )


@pytest.mark.slow
def test_keyword_search_latency_is_bounded_by_a_minimal_match(wired):
    client, index = wired
    rows = []
    for query in QUERIES:
        minimal_body = {
            "size": PAGE_SIZE,
            "query": {
                "bool": {"must": [{"match": {"content": query.strip('"')}}], "filter": _filters()}
            },
        }
        minimal = _p50_ms(lambda b=minimal_body: client.search(index=index, body=b))
        keyword = _p50_ms(lambda q=query: _keyword_search(q))
        single = _p50_ms(lambda q=query: _single_request_search(client, index, q), runs=3, warmup=1)
        rows.append((query, minimal, keyword, single))
        print(
            f"\n#1064 {query!r}: minimal match p50 {minimal:.1f} ms | keyword search p50 "
            f"{keyword:.1f} ms | single-request body p50 {single:.1f} ms"
        )

    baseline = max(statistics.median(r[1] for r in rows), _MIN_BASELINE_MS)
    total = sum(r[2] for r in rows)
    budget = _LATENCY_RATIO * baseline
    print(f"#1064 total keyword p50 {total:.0f} ms, budget {budget:.0f} ms")
    slowest = max(rows, key=lambda r: r[2])[0]
    assert total <= budget, (
        f"keyword search p50 summed over {len(rows)} query shapes is {total:.0f} ms, over "
        f"{_LATENCY_RATIO}x a minimal match ({baseline:.1f} ms). Slowest {slowest!r}: "
        f"{_profile_summary(client, index, slowest)}"
    )
