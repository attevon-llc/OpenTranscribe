"""Search results are complete and consistent on a real OpenSearch (#1064, #1078, #1079).

``test_keyword_search_latency_opensearch.py`` proves the #1064 split execution is fast and
identical to the single-request body on pages 1 and 3. This module proves the result set
as a whole:

* **every page** of a multi-page result — including the last, partial one — matches the
  single-request execution for that page, under every filter the Search page sends;
* **every matching file is reachable**: the union of all pages equals the files the
  keyword query matches (ground truth from a ``terms`` agg over the same query), with no
  duplicate and no gap across page boundaries, and with totals that agree. It checks this
  for every sort. #1078: a non-relevance sort used to reach page 1 only;
* a page requested **after the corpus grew** still matches the single-request execution;
* **highlights are exact**: every ``<mark>`` wraps a word the query matched (exact word,
  phrase, typo via fuzziness), and removing the marks gives back the original text;
* the **in-file find bar count** (``count_matches``) equals the number of chunks in the
  file that really contain the word;
* the **digest (summary) plane never surfaces** as a transcript hit or in a count.

Hybrid tests additionally need a deployed text-embedding model. Export its id as
``OPENSEARCH_TEST_MODEL_ID``; they skip otherwise. #1079: hybrid used to drop every
keyword-matching file outside the fused rank window, so 43 of 165 files were reachable.

Point it at a throwaway cluster, never the shared dev one::

    OPENSEARCH_PORT=55691 [OPENSEARCH_TEST_MODEL_ID=<id>] pytest \\
        tests/integration/test_search_completeness_opensearch.py -m integration -o addopts= -p no:xdist
"""

from __future__ import annotations

import html
import os
import random
import re
import time
import uuid
from typing import Any

import pytest

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"
_MODEL_ID = os.environ.get("OPENSEARCH_TEST_MODEL_ID", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group("search_completeness"),
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason="No OpenSearch reachable (SKIP_OPENSEARCH). Start a throwaway cluster and "
        "export OPENSEARCH_PORT — see the module docstring.",
    ),
]
_needs_model = pytest.mark.skipif(
    not _MODEL_ID, reason="Hybrid needs a deployed embedding model: set OPENSEARCH_TEST_MODEL_ID."
)

USER_ID = 1078
ORG_ID = 78
PAGE_SIZE = 20
N_FILES = 200
CHUNKS_PER_FILE = 6
PIPELINE = f"test-completeness-ingest-{uuid.uuid4().hex[:8]}"

TOPICS = {
    "finance": [
        "We reviewed the budget for the next quarter.",
        "The budget meeting ran long because of the forecast.",
        "Finance wants a tighter budget and fewer surprises.",
        "Our quarterly report shows spending above plan.",
    ],
    "space": [
        "The rocket launch was delayed by weather.",
        "Space exploration needs long term funding.",
        "The mission team reviewed the launch window.",
        "Orbital mechanics decided the trajectory.",
    ],
    "hiring": [
        "The hiring plan adds three engineers.",
        "Recruiting will interview candidates next week.",
        "We need a senior designer on the team.",
        "Onboarding takes about two weeks.",
    ],
    "product": [
        "The product launch is scheduled for spring.",
        "Customers asked for a faster export feature.",
        "The roadmap review covered pricing.",
        "Marketing prepared the launch announcement.",
    ],
}
FILLER = [
    "so",
    "um",
    "yeah",
    "right",
    "okay",
    "and",
    "then",
    "we",
    "basically",
    "talked",
    "about",
    "the",
    "other",
    "thing",
]
SPEAKERS = ["Joe Rogan", "Alice Park", "Bob Stone", "Carol Budget", "Dan Ray"]

#: The highlight-precision file: known text, known counts. "zephyrine" appears in chunks
#: 0, 2 and 3 only; chunk 3 has it twice and also carries "zephyrine meeting".
MARKER_UUID = "completeness-marker"
MARKER_CHUNKS = [
    "Our zephyrine budget grew last year.",
    "Nothing relevant was said here at all.",
    "The zephyrine forecast was revised upward.",
    "A zephyrine meeting followed, and the zephyrine plan passed.",
    "Closing remarks and thanks to everyone.",
]
MARKER_TRUE_COUNT = 3

QUERIES = ["budget", "budget meeting", "budgte", '"budget meeting"', "rocket launch", "Joe Rogan"]
FILTERSETS: list[dict[str, Any]] = [
    {},
    {"speakers": ["Joe Rogan"]},
    {"tags": ["finance"]},
    {"collection_id": 2},
    {"date_from": "2026-03-01", "date_to": "2026-06-30"},
    {"organization_id": ORG_ID},
    {"title_filter": "review"},
    {"file_type": ["audio"]},
    {"min_duration": 700.0},
]
SORTS = ["relevance", "upload_time", "filename", "duration", "file_size"]


def _file_docs(f: int, rng: random.Random) -> list[tuple[str, dict[str, Any]]]:
    topic = list(TOPICS)[f % 4]
    file_uuid = f"cmp-{f:04d}"
    speakers = rng.sample(SPEAKERS, 2)
    title = f"{topic.title()} {'review' if f % 3 == 0 else 'sync'} {f}"
    if f % 7 == 0:
        title = f"Budget meeting notes {f}"
    base: dict[str, Any] = {
        "file_id": -(f + 1),
        "file_uuid": file_uuid,
        "user_id": USER_ID,
        "accessible_user_ids": [USER_ID],
        "title": title,
        "speakers": speakers,
        "tags": [topic],
        "collection_ids": [f % 3 + 1],
        "content_type": "audio/mpeg" if f % 2 else "video/mp4",
        "duration": 600.0 + f,
        "file_size": 10_000 + f,
        "language": "en",
        "upload_time": f"2026-0{1 + f % 9}-1{f % 10}T00:00:00Z",
    }
    if f % 5 == 0:
        base["organization_id"] = ORG_ID
    docs = []
    for n in range(CHUNKS_PER_FILE):
        sentences = rng.sample(TOPICS[topic], 2)
        if rng.random() < 0.4:  # cross-topic mentions spread each query over many files
            sentences.append(rng.choice(TOPICS[rng.choice(list(TOPICS))]))
        text = " ".join(sentences) + " " + " ".join(rng.choice(FILLER) for _ in range(30))
        docs.append(
            (
                f"{file_uuid}_{n}",
                {
                    **base,
                    "chunk_index": n,
                    "content": text,
                    "embedding_text": text,
                    "speaker": speakers[n % 2],
                    "doc_type": "chunk",
                    "start_time": n * 30.0,
                    "end_time": n * 30.0 + 30.0,
                },
            )
        )
    # A digest (summary plane) document stuffed with every query word: it must never
    # surface as a transcript hit or be counted.
    digest_text = "budget meeting rocket launch Joe Rogan zephyrine summary"
    docs.append(
        (
            f"{file_uuid}_digest",
            {**base, "doc_type": "digest", "content": digest_text, "embedding_text": digest_text},
        )
    )
    return docs


def _bulk(client, index: str, docs: list[tuple[str, dict[str, Any]]]) -> None:
    actions: list[dict[str, Any]] = []
    for doc_id, doc in docs:
        action: dict[str, Any] = {"_index": index, "_id": doc_id}
        if _MODEL_ID:
            action["pipeline"] = PIPELINE
        actions += [{"index": action}, doc]
    resp = client.bulk(body=actions, refresh=True, request_timeout=600)
    assert not resp.get("errors"), "bulk seed reported errors"


@pytest.fixture(scope="module")
def corpus():
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"
    if _MODEL_ID:
        client.ingest.put_pipeline(
            id=PIPELINE,
            body={
                "processors": [
                    {
                        "text_embedding": {
                            "model_id": _MODEL_ID,
                            "field_map": {"embedding_text": "embedding"},
                        }
                    }
                ]
            },
        )
    name = f"test_completeness_{uuid.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    rng = random.Random(1078)  # noqa: S311 — deterministic test corpus, not crypto
    docs = [d for f in range(N_FILES) for d in _file_docs(f, rng)]
    marker_base = {
        "file_id": -9999,
        "file_uuid": MARKER_UUID,
        "user_id": USER_ID,
        "accessible_user_ids": [USER_ID],
        "title": "Marker file",
        "speakers": ["Dan Ray"],
        "language": "en",
        "content_type": "audio/mpeg",
        "doc_type": "chunk",
    }
    docs += [
        (
            f"{MARKER_UUID}_{n}",
            {
                **marker_base,
                "chunk_index": n,
                "content": t,
                "embedding_text": t,
                "speaker": "Dan Ray",
            },
        )
        for n, t in enumerate(MARKER_CHUNKS)
    ]
    _bulk(client, name, docs)
    try:
        yield client, name, rng
    finally:
        client.indices.delete(index=name, ignore_unavailable=True)
        if _MODEL_ID:
            client.ingest.delete_pipeline(id=PIPELINE, ignore=[404])


@pytest.fixture
def wired(corpus, monkeypatch):
    from app.core.config import settings
    from app.services.search import hybrid_search_service as hss

    client, name, rng = corpus
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", bool(_MODEL_ID))
    monkeypatch.setattr(hss.HybridSearchService, "_get_neural_model_id", lambda self: _MODEL_ID)
    return client, name, rng


def _filters(fs: dict[str, Any]) -> list[dict[str, Any]]:
    from app.services.search.hybrid_search_service import HybridSearchService

    fs = dict(fs)
    return HybridSearchService()._build_filters(
        USER_ID,
        fs.pop("speakers", None),
        fs.pop("tags", None),
        fs.pop("date_from", None),
        fs.pop("date_to", None),
        **fs,
    )


def _search(
    query: str, page: int, fs: dict[str, Any], sort_by="relevance", neural=False, pipeline=""
):
    from app.services.search.hybrid_search_service import HybridSearchService

    return HybridSearchService()._search_with_collapse(
        query=query,
        search_query=query,
        filters=_filters(fs),
        page=page,
        page_size=PAGE_SIZE,
        sort_by=sort_by,
        sort_order="desc",
        search_mode="hybrid" if neural else "keyword",
        filters_applied={},
        start_time=time.time(),
        has_speaker_filter=bool(fs.get("speakers")),
        use_neural=neural,
        search_pipeline=pipeline,
    )


def _single_request(client, index: str, query: str, page: int, fs: dict[str, Any]):
    """The pre-#1064 relevance execution: one collapse body, per-group inner_hits."""
    from app.services.search.hybrid_search_service import HybridSearchService

    svc = HybridSearchService()
    body = svc._build_collapsed_bm25_body(
        query, _filters(fs), page, PAGE_SIZE, bool(fs.get("speakers"))
    )
    grouped, _ = svc._process_collapsed_results(client.search(index=index, body=body), query)
    svc._apply_semantic_demotion(grouped)
    result = svc._sort_and_paginate(
        query, grouped, "relevance", "desc", "keyword", page, PAGE_SIZE, {}, time.time()
    )
    result.total_files = len(grouped)
    svc._apply_semantic_highlights(result.results, query)
    return result


def _truth(client, index: str, query: str, fs: dict[str, Any]) -> set[str]:
    """Every file the keyword query matches, straight from a terms agg."""
    from app.services.search.hybrid_search_service import HybridSearchService

    svc = HybridSearchService()
    text = svc._build_text_query(query, svc._get_search_fields(bool(fs.get("speakers")), True))
    resp = client.search(
        index=index,
        body={
            "size": 0,
            "query": {"bool": {"must": [text], "filter": _filters(fs)}},
            "aggs": {"f": {"terms": {"field": "file_uuid", "size": 10_000}}},
        },
    )
    return {b["key"] for b in resp["aggregations"]["f"]["buckets"]}


def _all_pages(query: str, fs: dict[str, Any], **kw) -> list:
    pages, page = [], 1
    while True:
        result = _search(query, page, fs, **kw)
        pages.append(result)
        if page >= result.total_pages or not result.results:
            return pages
        page += 1
        assert page <= 60, "pagination did not terminate"


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


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("fs", FILTERSETS, ids=lambda fs: ",".join(fs) or "nofilter")
def test_every_page_matches_the_single_request_execution(wired, query, fs):
    client, index, _ = wired
    pages = _all_pages(query, fs)
    seen = [h.file_uuid for p in pages for h in p.results]
    assert sorted(seen) == sorted(_truth(client, index, query, fs)), "missing or repeated files"
    for n, page in enumerate(pages, start=1):
        expected = _single_request(client, index, query, n, fs)
        assert _page_view(page) == _page_view(expected), f"page {n} differs"
        assert (page.total_files, page.total_results, page.total_pages) == (
            expected.total_files,
            expected.total_results,
            expected.total_pages,
        )
        for hit in page.results:
            assert all("<mark>" in o.snippet for o in hit.occurrences if o.match_type == "content")


_SORT_KEYS = {
    "upload_time": lambda h: h.upload_time or "",
    "filename": lambda h: (h.title or "").lower(),
    "duration": lambda h: h.duration,
    "file_size": lambda h: h.file_size,
}


@pytest.mark.parametrize("sort_by", SORTS)
@pytest.mark.parametrize("query", ["budget", "rocket launch", "review"])
@pytest.mark.parametrize("fs", [{}, {"tags": ["finance"]}], ids=["nofilter", "tags"])
def test_every_matching_file_is_reachable_across_pages(wired, sort_by, query, fs):
    client, index, _ = wired
    truth = _truth(client, index, query, fs)
    assert len(truth) > PAGE_SIZE or fs, "the corpus must span several pages"
    pages = _all_pages(query, fs, sort_by=sort_by)
    seen = [h.file_uuid for p in pages for h in p.results]

    assert len(seen) == len(set(seen)), "a file appeared on two pages"
    assert set(seen) == truth
    assert {p.total_files for p in pages} == {len(truth)}
    assert all(len(p.results) == PAGE_SIZE for p in pages[:-1])
    if sort_by in _SORT_KEYS:
        keys = [_SORT_KEYS[sort_by](h) for p in pages for h in p.results]
        assert keys == sorted(keys, reverse=True)


def test_a_page_after_the_corpus_grew_matches_the_single_request_execution(wired):
    client, index, rng = wired
    before = _search("budget", 1, {})
    _bulk(client, index, [d for f in range(N_FILES, N_FILES + 25) for d in _file_docs(f, rng)])
    try:
        for n in (1, 2, _search("budget", 1, {}).total_pages):
            assert _page_view(_search("budget", n, {})) == _page_view(
                _single_request(client, index, "budget", n, {})
            )
        after = _all_pages("budget", {})
        seen = [h.file_uuid for p in after for h in p.results]
        assert set(seen) == _truth(client, index, "budget", {})
        assert len(seen) == len(set(seen))
        assert after[0].total_files > before.total_files
    finally:
        client.delete_by_query(
            index=index,
            body={
                "query": {
                    "terms": {"file_uuid": [f"cmp-{f:04d}" for f in range(N_FILES, N_FILES + 25)]}
                }
            },
            refresh=True,
        )


_MARK = re.compile(r"<mark>(.*?)</mark>", re.DOTALL)


@pytest.mark.parametrize(
    ("query", "marked"),
    [
        ("zephyrine", {"zephyrine"}),
        ("zephyrne", {"zephyrine"}),  # typo, via fuzziness
        ('"zephyrine meeting"', {"zephyrine", "meeting", "zephyrine meeting"}),
    ],
)
def test_highlights_wrap_exactly_the_matched_words(wired, query, marked):
    _, _, _ = wired
    hit = next(h for h in _search(query, 1, {}).results if h.file_uuid == MARKER_UUID)
    by_chunk = {o.chunk_index: o.snippet for o in hit.occurrences}
    expected_chunks = {0, 2, 3} if '"' not in query else {3}
    assert set(by_chunk) == expected_chunks
    for chunk, snippet in by_chunk.items():
        words = [w.lower() for w in _MARK.findall(snippet)]
        assert words, f"chunk {chunk} rendered without a highlight"
        assert set(words) <= marked
        assert html.unescape(_MARK.sub(r"\1", snippet)) in MARKER_CHUNKS[chunk]
    if query == "zephyrine":
        assert _MARK.findall(by_chunk[3]) == ["zephyrine", "zephyrine"]


@pytest.mark.parametrize(("query", "count"), [("zephyrine", MARKER_TRUE_COUNT), ("forecast", 1)])
def test_the_in_file_find_bar_counts_every_matching_chunk(wired, query, count):
    from app.services.search.hybrid_search_service import HybridSearchService

    assert HybridSearchService().count_matches(query, USER_ID, file_uuid=MARKER_UUID) == count


def test_the_digest_plane_never_surfaces(wired):
    assert [h for p in _all_pages("summary", {}) for h in p.results] == []
    snippets = [
        o.snippet
        for p in _all_pages("budget meeting", {})
        for h in p.results
        for o in h.occurrences
    ]
    assert snippets
    assert [s for s in snippets if "summary" in s] == []


@_needs_model
@pytest.mark.parametrize(
    ("query", "deep"),
    [("launch", True), ("rocket launch", True), ("budget", False), ("Joe Rogan", False)],
)
def test_hybrid_reaches_every_keyword_matching_file(wired, query, deep):
    from app.services.search.hybrid_search_service import ensure_fusion_pipeline

    client, index, _ = wired
    pipeline = ensure_fusion_pipeline()
    pages = _all_pages(query, {}, neural=True, pipeline=pipeline)
    seen = [h.file_uuid for p in pages for h in p.results]
    # Past page 5 the over-fetch grows; a fused window that grew with it
    # shifted page boundaries (files shown twice). Make sure that is exercised.
    if deep:
        assert len(pages) > 5

    assert len(seen) == len(set(seen)), "a file appeared on two pages"
    assert _truth(client, index, query, {}) <= set(seen)
    for page in pages:
        for hit in page.results:
            if hit.keyword_occurrences:
                # The word can be in the transcript, the speaker label or the title.
                rendered = [hit.title_highlighted] + [
                    o.snippet + o.speaker_highlighted for o in hit.occurrences
                ]
                assert any("<mark>" in text for text in rendered), hit.file_uuid
